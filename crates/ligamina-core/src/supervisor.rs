//! Desired-state supervision for local Host processes.

use std::{
    collections::{HashMap, HashSet},
    future::Future,
    sync::{Arc, Weak},
    time::Duration,
};

use serde_json::{Value, json};
use thiserror::Error;
use tokio::{
    sync::{Mutex, watch},
    time::{sleep, timeout},
};
use tokio_util::sync::CancellationToken;

use crate::{
    deployment::local::{self, CoreRequestHandler, ExitInfo, LocalCommand, SpawnedProcess},
    transport::{HostSession, RpcError},
};

const HOST_PROTOCOL_VERSION: &str = "0.1";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RestartPolicy {
    pub max_restarts: u32,
    pub backoff: Duration,
}

impl Default for RestartPolicy {
    fn default() -> Self {
        Self {
            max_restarts: 3,
            backoff: Duration::from_secs(1),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostConfig {
    pub id: String,
    pub enabled: bool,
    pub command: LocalCommand,
    pub restart: RestartPolicy,
}

impl HostConfig {
    pub fn new(id: impl Into<String>, command: LocalCommand) -> Self {
        Self {
            id: id.into(),
            enabled: true,
            command,
            restart: RestartPolicy::default(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HostState {
    Disabled,
    Stopped,
    Starting,
    Active,
    Backoff,
    Failed,
    Stopping,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostStatus {
    pub id: String,
    pub state: HostState,
    /// Monotonically increasing process generation. Handles from older epochs are rejected.
    pub epoch: u64,
    pub pid: Option<u32>,
    pub restart_count: u32,
    pub last_error: Option<String>,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SupervisorError {
    #[error("host id cannot be empty")]
    EmptyHostId,
    #[error("host id is duplicated in the desired set: {0}")]
    DuplicateHostId(String),
    #[error("host {0} is not active")]
    NotActive(String),
    #[error("host handle for {host_id} has stale epoch {epoch}")]
    StaleEpoch { host_id: String, epoch: u64 },
    #[error("one or more hosts failed to apply: {0}")]
    ApplyFailed(String),
    #[error("host {host_id} failed to start: {reason}")]
    StartFailed { host_id: String, reason: String },
    #[error("host request failed: {0}")]
    Rpc(String),
}

impl From<RpcError> for SupervisorError {
    fn from(value: RpcError) -> Self {
        Self::Rpc(value.to_string())
    }
}

type HostSlot = Arc<HostSlotInner>;

struct HostSlotInner {
    operation: Mutex<()>,
    record: Mutex<HostRecord>,
}

struct HostRecord {
    config: HostConfig,
    desired: bool,
    state: HostState,
    epoch: u64,
    restart_count: u32,
    last_error: Option<String>,
    process: Option<RunningProcess>,
    retry_cancel: Option<CancellationToken>,
}

#[derive(Clone)]
struct RunningProcess {
    pid: u32,
    session: HostSession,
    process_tree: local::ProcessTree,
    exited: watch::Receiver<Option<ExitInfo>>,
    cancellation: CancellationToken,
}

struct SupervisorInner {
    slots: Mutex<HashMap<String, HostSlot>>,
    reconcile_lock: Mutex<()>,
    core_handler: CoreRequestHandler,
}

/// Owns the enabled Host set and its child process trees.
#[derive(Clone)]
pub struct HostSupervisor {
    inner: Arc<SupervisorInner>,
}

impl Default for HostSupervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl HostSupervisor {
    /// Create a supervisor whose Host-to-Core requests are rejected as unsupported.
    pub fn new() -> Self {
        Self::with_handler(|_host_id, _epoch, _session, method, _params| async move {
            Err(RpcError::Remote {
                code: -32601,
                message: format!("core method not found: {method}"),
            })
        })
    }

    /// Create a supervisor with a handler for Host-originated Core requests.
    pub fn with_handler<F, Fut>(handler: F) -> Self
    where
        F: Fn(String, u64, HostSession, String, Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, RpcError>> + Send + 'static,
    {
        let core_handler: CoreRequestHandler = Arc::new(
            move |host_id, epoch, _cancellation, session, method, params| {
                Box::pin(handler(host_id, epoch, session, method, params))
            },
        );
        Self {
            inner: Arc::new(SupervisorInner {
                slots: Mutex::new(HashMap::new()),
                reconcile_lock: Mutex::new(()),
                core_handler,
            }),
        }
    }

    /// Reconcile the live process set to the supplied desired Host set.
    pub async fn apply_desired_set(&self, desired: Vec<HostConfig>) -> Result<(), SupervisorError> {
        let _reconcile = self.inner.reconcile_lock.lock().await;
        let mut ids = HashSet::with_capacity(desired.len());
        for config in &desired {
            if config.id.trim().is_empty() {
                return Err(SupervisorError::EmptyHostId);
            }
            if !ids.insert(config.id.clone()) {
                return Err(SupervisorError::DuplicateHostId(config.id.clone()));
            }
        }

        let existing = self.inner.slots.lock().await.clone();
        for (id, slot) in existing {
            if !ids.contains(&id) {
                stop_slot(&slot, true).await;
                self.inner.slots.lock().await.remove(&id);
            }
        }

        let mut failures = Vec::new();
        for config in desired {
            if let Err(error) = apply_one(&self.inner, config).await {
                failures.push(error.to_string());
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(SupervisorError::ApplyFailed(failures.join("; ")))
        }
    }

    pub async fn status(&self) -> Vec<HostStatus> {
        let slots = self.inner.slots.lock().await.clone();
        let mut statuses = Vec::with_capacity(slots.len());
        for (id, slot) in slots {
            let record = slot.record.lock().await;
            statuses.push(HostStatus {
                id,
                state: record.state,
                epoch: record.epoch,
                pid: record.process.as_ref().map(|process| process.pid),
                restart_count: record.restart_count,
                last_error: record.last_error.clone(),
            });
        }
        statuses.sort_by(|left, right| left.id.cmp(&right.id));
        statuses
    }

    pub async fn handle(&self, host_id: &str) -> Result<HostHandle, SupervisorError> {
        let slot = self.slot(host_id).await?;
        let record = slot.record.lock().await;
        if record.state != HostState::Active {
            return Err(SupervisorError::NotActive(host_id.to_owned()));
        }
        Ok(HostHandle {
            inner: Arc::downgrade(&self.inner),
            host_id: host_id.to_owned(),
            epoch: record.epoch,
        })
    }

    /// Stop every Host and cancel pending restarts. Safe to call more than once.
    pub async fn shutdown(&self) {
        let _reconcile = self.inner.reconcile_lock.lock().await;
        let slots = self.inner.slots.lock().await.clone();
        let mut stops = tokio::task::JoinSet::new();
        for slot in slots.into_values() {
            stops.spawn(async move { stop_slot(&slot, true).await });
        }
        while stops.join_next().await.is_some() {}
    }

    async fn slot(&self, host_id: &str) -> Result<HostSlot, SupervisorError> {
        self.inner
            .slots
            .lock()
            .await
            .get(host_id)
            .cloned()
            .ok_or_else(|| SupervisorError::NotActive(host_id.to_owned()))
    }
}

#[derive(Clone)]
pub struct HostHandle {
    inner: Weak<SupervisorInner>,
    host_id: String,
    epoch: u64,
}

impl HostHandle {
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub async fn request(
        &self,
        method: &str,
        params: Value,
        deadline: Duration,
    ) -> Result<Value, SupervisorError> {
        let Some(inner) = self.inner.upgrade() else {
            return Err(SupervisorError::StaleEpoch {
                host_id: self.host_id.clone(),
                epoch: self.epoch,
            });
        };
        let slot = inner
            .slots
            .lock()
            .await
            .get(&self.host_id)
            .cloned()
            .ok_or_else(|| SupervisorError::StaleEpoch {
                host_id: self.host_id.clone(),
                epoch: self.epoch,
            })?;
        let record = slot.record.lock().await;
        if record.epoch != self.epoch {
            return Err(SupervisorError::StaleEpoch {
                host_id: self.host_id.clone(),
                epoch: self.epoch,
            });
        }
        if record.state != HostState::Active {
            return Err(SupervisorError::NotActive(self.host_id.clone()));
        }
        let process = record
            .process
            .as_ref()
            .expect("active Host must own a child process")
            .clone();
        drop(record);
        if process.cancellation.is_cancelled() {
            return Err(SupervisorError::StaleEpoch {
                host_id: self.host_id.clone(),
                epoch: self.epoch,
            });
        }
        process
            .session
            .request_cancellable(method, params, deadline, process.cancellation)
            .await
            .map_err(Into::into)
    }
}

async fn apply_one(
    inner: &Arc<SupervisorInner>,
    config: HostConfig,
) -> Result<(), SupervisorError> {
    let slot = {
        let mut slots = inner.slots.lock().await;
        slots
            .entry(config.id.clone())
            .or_insert_with(|| {
                Arc::new(HostSlotInner {
                    operation: Mutex::new(()),
                    record: Mutex::new(HostRecord {
                        desired: false,
                        state: HostState::Stopped,
                        epoch: 0,
                        restart_count: 0,
                        last_error: None,
                        process: None,
                        retry_cancel: None,
                        config: config.clone(),
                    }),
                })
            })
            .clone()
    };

    {
        let record = slot.record.lock().await;
        if record.config == config
            && record.desired == config.enabled
            && matches!(
                record.state,
                HostState::Active | HostState::Starting | HostState::Backoff
            )
        {
            return Ok(());
        }
        if record.config == config && !config.enabled && record.state == HostState::Disabled {
            return Ok(());
        }
    }

    stop_slot(&slot, true).await;
    let epoch = {
        let mut record = slot.record.lock().await;
        record.config = config.clone();
        record.desired = config.enabled;
        record.restart_count = 0;
        record.last_error = None;
        record.epoch = record.epoch.saturating_add(1);
        record.state = if config.enabled {
            HostState::Starting
        } else {
            HostState::Disabled
        };
        record.epoch
    };
    if !config.enabled {
        return Ok(());
    }
    start_generation(inner.clone(), slot, config, epoch).await
}

async fn start_generation(
    inner: Arc<SupervisorInner>,
    slot: HostSlot,
    config: HostConfig,
    epoch: u64,
) -> Result<(), SupervisorError> {
    let _operation = slot.operation.lock().await;
    {
        let mut record = slot.record.lock().await;
        if !record.desired || record.epoch != epoch {
            return Ok(());
        }
        record.state = HostState::Starting;
    }

    let spawned = match timeout(
        config.command.startup_timeout,
        local::spawn(
            &config.command,
            config.id.clone(),
            epoch,
            authorized_core_handler(&inner),
        ),
    )
    .await
    {
        Ok(Ok(process)) => process,
        Ok(Err(error)) => {
            mark_start_failed(&slot, epoch, error.to_string()).await;
            return Err(SupervisorError::StartFailed {
                host_id: config.id,
                reason: error.to_string(),
            });
        }
        Err(_) => {
            mark_start_failed(&slot, epoch, "process launch timed out".to_owned()).await;
            return Err(SupervisorError::StartFailed {
                host_id: config.id,
                reason: "process launch timed out".to_owned(),
            });
        }
    };
    let running = running_process(spawned);
    {
        let mut record = slot.record.lock().await;
        if !record.desired || record.epoch != epoch {
            drop(record);
            stop_running(&running, &config.command).await;
            return Ok(());
        }
        record.process = Some(running.clone());
    }
    monitor_process(
        Arc::downgrade(&inner),
        slot.clone(),
        config.clone(),
        epoch,
        running.clone(),
    );

    let init = timeout(
        config.command.startup_timeout,
        running.session.request(
            "host.initialize",
            json!({
                "host_id": config.id,
                "epoch": epoch,
                "protocol_version": HOST_PROTOCOL_VERSION,
            }),
            config.command.startup_timeout,
        ),
    )
    .await;
    let init_result = match init {
        Ok(Ok(result))
            if result.get("ready") == Some(&Value::Bool(true))
                && result.get("protocol_version").and_then(Value::as_str)
                    == Some(HOST_PROTOCOL_VERSION) =>
        {
            Ok(())
        }
        Ok(Ok(result)) => Err(format!("Host rejected initialization: {result}")),
        Ok(Err(error)) => Err(error.to_string()),
        Err(_) => Err("Host initialization timed out".to_owned()),
    };
    if let Err(reason) = init_result {
        mark_start_failed(&slot, epoch, reason.clone()).await;
        stop_running(&running, &config.command).await;
        return Err(SupervisorError::StartFailed {
            host_id: config.id,
            reason,
        });
    }

    let mut record = slot.record.lock().await;
    if record.desired && record.epoch == epoch && record.state == HostState::Starting {
        record.state = HostState::Active;
        record.last_error = None;
        Ok(())
    } else {
        Err(SupervisorError::StartFailed {
            host_id: config.id,
            reason: "Host exited or was superseded before activation".to_owned(),
        })
    }
}

fn running_process(process: SpawnedProcess) -> RunningProcess {
    RunningProcess {
        pid: process.pid,
        session: process.session,
        process_tree: process.process_tree,
        exited: process.exited,
        cancellation: process.cancellation,
    }
}

async fn mark_start_failed(slot: &HostSlot, epoch: u64, reason: String) {
    let mut record = slot.record.lock().await;
    if record.epoch == epoch {
        record.process.take();
        record.epoch = record.epoch.saturating_add(1);
        record.state = HostState::Failed;
        record.last_error = Some(reason);
    }
}

fn monitor_process(
    inner: Weak<SupervisorInner>,
    slot: HostSlot,
    config: HostConfig,
    epoch: u64,
    running: RunningProcess,
) {
    tokio::spawn(async move {
        let mut exited = running.exited.clone();
        let info = loop {
            if let Some(info) = exited.borrow().clone() {
                break info;
            }
            if exited.changed().await.is_err() {
                break ExitInfo {
                    code: None,
                    error: Some("child exit channel closed".to_owned()),
                };
            }
        };
        running.cancellation.cancel();
        running.process_tree.terminate(true);
        process_exited(inner, slot, config, epoch, info).await;
    });
}

async fn process_exited(
    inner: Weak<SupervisorInner>,
    slot: HostSlot,
    config: HostConfig,
    epoch: u64,
    info: ExitInfo,
) {
    let _operation = slot.operation.lock().await;
    let retry = {
        let mut record = slot.record.lock().await;
        if record.epoch != epoch {
            return;
        }
        record.process.take();
        let error = info.describe();
        record.last_error = Some(error.clone());
        if !record.desired {
            record.state = HostState::Disabled;
            None
        } else if record.state == HostState::Stopping {
            record.state = HostState::Stopped;
            None
        } else if record.state == HostState::Starting {
            record.epoch = record.epoch.saturating_add(1);
            record.state = HostState::Failed;
            Some((None, error))
        } else if record.restart_count < config.restart.max_restarts {
            record.restart_count += 1;
            record.epoch = record.epoch.saturating_add(1);
            record.state = HostState::Backoff;
            let cancellation = CancellationToken::new();
            record.retry_cancel = Some(cancellation.clone());
            Some((Some((record.epoch, cancellation)), error))
        } else {
            record.epoch = record.epoch.saturating_add(1);
            record.state = HostState::Failed;
            Some((None, error))
        }
    };

    let Some((Some((next_epoch, cancellation)), _error)) = retry else {
        return;
    };
    let Some(inner) = inner.upgrade() else {
        return;
    };
    let slot_for_retry = slot.clone();
    tokio::spawn(async move {
        tokio::select! {
            _ = cancellation.cancelled() => return,
            _ = sleep(config.restart.backoff) => {}
        }
        let _ = start_generation(inner, slot_for_retry, config, next_epoch).await;
    });
}

async fn stop_slot(slot: &HostSlot, disable: bool) {
    let _operation = slot.operation.lock().await;
    let (process, command, stop_epoch) = {
        let mut record = slot.record.lock().await;
        if let Some(retry) = record.retry_cancel.take() {
            retry.cancel();
        }
        if disable {
            record.desired = false;
        }
        let process = record.process.take();
        let command = record.config.command.clone();
        record.epoch = record.epoch.saturating_add(1);
        record.state = if process.is_some() {
            HostState::Stopping
        } else if record.desired {
            HostState::Stopped
        } else {
            HostState::Disabled
        };
        (process, command, record.epoch)
    };

    if let Some(process) = process {
        stop_running(&process, &command).await;
    }
    let mut record = slot.record.lock().await;
    if record.epoch == stop_epoch && record.state == HostState::Stopping {
        record.state = if record.desired {
            HostState::Stopped
        } else {
            HostState::Disabled
        };
    }
}

async fn stop_running(process: &RunningProcess, command: &LocalCommand) {
    process.cancellation.cancel();
    let _ = timeout(
        command.shutdown_timeout,
        process
            .session
            .request("host.shutdown", Value::Null, command.shutdown_timeout),
    )
    .await;

    if !wait_for_exit(process.exited.clone(), command.shutdown_timeout).await {
        process.process_tree.terminate(false);
        if !wait_for_exit(process.exited.clone(), Duration::from_secs(1)).await {
            process.process_tree.terminate(true);
            let _ = wait_for_exit(process.exited.clone(), Duration::from_secs(2)).await;
        }
    }
    // The main process may have exited while descendants remained in its group/job.
    process.process_tree.terminate(true);
}

async fn wait_for_exit(
    mut receiver: watch::Receiver<Option<ExitInfo>>,
    deadline: Duration,
) -> bool {
    timeout(deadline, async move {
        loop {
            if receiver.borrow().is_some() {
                return;
            }
            if receiver.changed().await.is_err() {
                return;
            }
        }
    })
    .await
    .is_ok()
}

fn authorized_core_handler(inner: &Arc<SupervisorInner>) -> CoreRequestHandler {
    let weak = Arc::downgrade(inner);
    let handler = inner.core_handler.clone();
    Arc::new(
        move |host_id, epoch, cancellation, session, method, params| {
            let weak = weak.clone();
            let handler = handler.clone();
            Box::pin(async move {
                if !is_active_generation(&weak, &host_id, epoch).await {
                    return Err(RpcError::Remote {
                        code: -32001,
                        message: "host generation is no longer active".to_owned(),
                    });
                }
                let canceled_context = format!("host {host_id} epoch {epoch}");
                let cancellation_waiter = cancellation.clone();
                tokio::select! {
                _ = cancellation_waiter.cancelled() => Err(RpcError::Cancelled(canceled_context)),
                    result = handler(host_id, epoch, cancellation, session, method, params) => result,
                }
            })
        },
    )
}

async fn is_active_generation(inner: &Weak<SupervisorInner>, host_id: &str, epoch: u64) -> bool {
    let Some(inner) = inner.upgrade() else {
        return false;
    };
    let Some(slot) = inner.slots.lock().await.get(host_id).cloned() else {
        return false;
    };
    let record = slot.record.lock().await;
    record.epoch == epoch && record.state == HostState::Active
}
