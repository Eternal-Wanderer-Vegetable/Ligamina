//! Local subprocess launch and process-tree ownership.

use std::{
    collections::BTreeMap, ffi::OsString, io, path::PathBuf, pin::Pin, process::ExitStatus,
    sync::Arc, time::Duration,
};

use serde_json::Value;
use tokio::{
    io::{AsyncWriteExt, copy},
    process::Command,
    sync::watch,
};
use tokio_util::sync::CancellationToken;

#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(windows)]
use tokio::process::Child;

use crate::transport::{HostSession, RpcError};

pub type CoreRequestFuture =
    Pin<Box<dyn Future<Output = Result<Value, RpcError>> + Send + 'static>>;
pub type CoreRequestHandler = Arc<
    dyn Fn(String, u64, CancellationToken, HostSession, String, Value) -> CoreRequestFuture
        + Send
        + Sync
        + 'static,
>;

use std::future::Future;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalCommand {
    pub program: OsString,
    pub args: Vec<OsString>,
    pub current_dir: Option<PathBuf>,
    pub environment: BTreeMap<OsString, OsString>,
    pub clear_environment: bool,
    pub startup_timeout: Duration,
    pub shutdown_timeout: Duration,
}

impl LocalCommand {
    pub fn new(program: impl Into<OsString>) -> Self {
        Self {
            program: program.into(),
            args: Vec::new(),
            current_dir: None,
            environment: BTreeMap::new(),
            clear_environment: false,
            startup_timeout: Duration::from_secs(15),
            shutdown_timeout: Duration::from_secs(10),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct ExitInfo {
    pub code: Option<i32>,
    pub error: Option<String>,
}

impl ExitInfo {
    fn from_status(status: ExitStatus) -> Self {
        Self {
            code: status.code(),
            error: None,
        }
    }

    fn from_error(error: io::Error) -> Self {
        Self {
            code: None,
            error: Some(error.to_string()),
        }
    }

    pub(crate) fn describe(&self) -> String {
        match (&self.error, self.code) {
            (Some(error), _) => format!("child wait failed: {error}"),
            (None, Some(code)) => format!("process exited with status {code}"),
            (None, None) => "process exited without an exit code".to_owned(),
        }
    }
}

pub(crate) struct SpawnedProcess {
    pub pid: u32,
    pub session: HostSession,
    pub process_tree: ProcessTree,
    pub exited: watch::Receiver<Option<ExitInfo>>,
    pub cancellation: CancellationToken,
}

pub(crate) async fn spawn(
    command_spec: &LocalCommand,
    host_id: String,
    epoch: u64,
    handler: CoreRequestHandler,
) -> io::Result<SpawnedProcess> {
    let mut command = Command::new(&command_spec.program);
    command
        .args(&command_spec.args)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED);
    if let Some(current_dir) = &command_spec.current_dir {
        command.current_dir(current_dir);
    }
    if command_spec.clear_environment {
        command.env_clear();
    }
    command.envs(&command_spec.environment);

    #[cfg(unix)]
    command.process_group(0);

    #[cfg(windows)]
    let process_tree = ProcessTree::prepare()?;

    let mut child = command.spawn()?;
    let Some(pid) = child.id() else {
        let _ = child.kill().await;
        let _ = child.wait().await;
        return Err(io::Error::other("spawned process did not expose a PID"));
    };

    #[cfg(unix)]
    let process_tree = ProcessTree::attach(pid);

    #[cfg(windows)]
    if let Err(error) = process_tree.attach(&child) {
        let _ = child.kill().await;
        let _ = child.wait().await;
        return Err(error);
    }

    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("child stdin pipe was not created"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("child stdout pipe was not created"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("child stderr pipe was not created"))?;

    let cancellation = CancellationToken::new();
    let request_cancellation = cancellation.clone();
    let session = HostSession::new(stdout, stdin, move |session, method, params| {
        let handler = handler.clone();
        let host_id = host_id.clone();
        let cancellation = request_cancellation.clone();
        async move {
            let canceled_context = format!("host {host_id} epoch {epoch}");
            let cancellation_waiter = cancellation.clone();
            tokio::select! {
                _ = cancellation_waiter.cancelled() => Err(RpcError::Cancelled(canceled_context)),
                result = handler(host_id, epoch, cancellation, session, method, params) => result,
            }
        }
    });
    let (exit_tx, exited) = watch::channel(None);
    let monitor_tree = process_tree.clone();
    let stderr_task = tokio::spawn(async move {
        let mut stderr = stderr;
        let mut sink = tokio::io::sink();
        let _ = copy(&mut stderr, &mut sink).await;
        let _ = sink.flush().await;
    });
    let child_cancellation = cancellation.clone();
    tokio::spawn(async move {
        let result = match child.wait().await {
            Ok(status) => ExitInfo::from_status(status),
            Err(error) => ExitInfo::from_error(error),
        };
        // A host can exit while leaving browser or worker grandchildren behind.
        monitor_tree.terminate(true);
        child_cancellation.cancel();
        exit_tx.send_replace(Some(result));
        let _ = stderr_task.await;
    });

    Ok(SpawnedProcess {
        pid,
        session,
        process_tree,
        exited,
        cancellation,
    })
}

#[derive(Clone)]
pub(crate) struct ProcessTree(Arc<ProcessTreeInner>);

#[cfg(unix)]
struct ProcessTreeInner {
    process_group: i32,
    cleaned: AtomicBool,
}

#[cfg(windows)]
struct ProcessTreeInner {
    job: std::os::windows::io::OwnedHandle,
}

impl ProcessTree {
    #[cfg(unix)]
    fn attach(pid: u32) -> Self {
        Self(Arc::new(ProcessTreeInner {
            process_group: pid as i32,
            cleaned: AtomicBool::new(false),
        }))
    }

    #[cfg(windows)]
    fn prepare() -> io::Result<Self> {
        use std::{
            mem::size_of,
            os::windows::io::{FromRawHandle, OwnedHandle},
            ptr,
        };
        use windows_sys::Win32::{
            Foundation::HANDLE,
            System::JobObjects::{
                CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
                SetInformationJobObject,
            },
        };

        // The job is configured before launch so any successfully attached process
        // tree is terminated when the last Core-owned handle closes.
        let raw = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
        if raw.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = unsafe { OwnedHandle::from_raw_handle(raw as _) };
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                raw as HANDLE,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(Arc::new(ProcessTreeInner { job })))
    }

    #[cfg(windows)]
    fn attach(&self, child: &Child) -> io::Result<()> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{
            Foundation::HANDLE, System::JobObjects::AssignProcessToJobObject,
        };

        let process = child
            .raw_handle()
            .ok_or_else(|| io::Error::other("child process handle is unavailable"))?;
        let result = unsafe {
            AssignProcessToJobObject(self.0.job.as_raw_handle() as HANDLE, process as HANDLE)
        };
        if result == 0 {
            return Err(io::Error::last_os_error());
        }
        resume_primary_thread(
            child
                .id()
                .ok_or_else(|| io::Error::other("suspended child process did not expose a PID"))?,
        )
    }

    pub(crate) fn terminate(&self, force: bool) {
        #[cfg(unix)]
        {
            if force && self.0.cleaned.swap(true, Ordering::SeqCst) {
                return;
            }
            let signal = if force { libc::SIGKILL } else { libc::SIGTERM };
            let result = unsafe { libc::kill(-self.0.process_group, signal) };
            if result == -1 && io::Error::last_os_error().kind() == io::ErrorKind::NotFound {
                self.0.cleaned.store(true, Ordering::SeqCst);
            }
        }

        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::System::JobObjects::TerminateJobObject;
            let _ = force;
            let _ = unsafe { TerminateJobObject(self.0.job.as_raw_handle() as _, 1) };
        }
    }
}

#[cfg(windows)]
fn resume_primary_thread(process_id: u32) -> io::Result<()> {
    use std::{
        mem::size_of,
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    };
    use windows_sys::Win32::{
        Foundation::{HANDLE, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First,
                Thread32Next,
            },
            Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME},
        },
    };

    let raw_snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw_snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw_snapshot as _) };
    let mut entry = THREADENTRY32 {
        dwSize: size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    let mut found = unsafe { Thread32First(snapshot.as_raw_handle() as HANDLE, &mut entry) != 0 };
    while found {
        if entry.th32OwnerProcessID == process_id {
            let raw_thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if raw_thread.is_null() {
                return Err(io::Error::last_os_error());
            }
            let thread = unsafe { OwnedHandle::from_raw_handle(raw_thread as _) };
            if unsafe { ResumeThread(thread.as_raw_handle() as HANDLE) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            return Ok(());
        }
        found = unsafe { Thread32Next(snapshot.as_raw_handle() as HANDLE, &mut entry) != 0 };
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        format!("could not find a primary thread for process {process_id}"),
    ))
}

#[cfg(unix)]
impl Drop for ProcessTreeInner {
    fn drop(&mut self) {
        if !self.cleaned.swap(true, Ordering::SeqCst) {
            // Runtime teardown may abort Tokio tasks before async shutdown can run.
            unsafe {
                libc::kill(-self.process_group, libc::SIGKILL);
            }
        }
    }
}
