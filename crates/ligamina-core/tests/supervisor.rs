use std::{collections::BTreeMap, ffi::OsString, path::PathBuf, time::Duration};

use ligamina_core::{
    deployment::local::LocalCommand,
    supervisor::{
        HostConfig, HostState, HostStatus, HostSupervisor, RestartPolicy, SupervisorError,
    },
};
use serde_json::{Value, json};
use tokio::time::{sleep, timeout};

fn worker_config(id: &str) -> HostConfig {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("workspace root should exist");
    let mut command = LocalCommand::new("uv");
    command.args = [
        "run",
        "--locked",
        "--project",
        "hosts/nonebot",
        "--python",
        "3.12.14",
        "python",
        "lab/test_workers/supervisor_worker.py",
    ]
    .into_iter()
    .map(OsString::from)
    .collect();
    command.current_dir = Some(root);
    command.startup_timeout = Duration::from_secs(30);
    command.shutdown_timeout = Duration::from_secs(3);
    command.environment = BTreeMap::from([(
        OsString::from("UV_PROJECT_ENVIRONMENT"),
        std::env::temp_dir()
            .join(format!("ligamina-supervisor-{id}"))
            .into_os_string(),
    )]);
    HostConfig {
        id: id.to_owned(),
        enabled: true,
        command,
        restart: RestartPolicy {
            max_restarts: 0,
            backoff: Duration::from_millis(20),
        },
    }
}

fn find_status(statuses: &[HostStatus], id: &str) -> HostStatus {
    statuses
        .iter()
        .find(|status| status.id == id)
        .unwrap_or_else(|| panic!("missing Host status for {id}: {statuses:?}"))
        .clone()
}

async fn wait_for_status(
    supervisor: &HostSupervisor,
    id: &str,
    predicate: impl Fn(&HostStatus) -> bool,
) -> HostStatus {
    let result = timeout(Duration::from_secs(8), async {
        loop {
            let status = find_status(&supervisor.status().await, id);
            if predicate(&status) {
                return status;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    match result {
        Ok(status) => status,
        Err(_) => panic!(
            "timed out waiting for {id}: {:?}",
            supervisor.status().await
        ),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn disabled_host_spawns_no_process_and_initialization_failure_never_activates() {
    let supervisor = HostSupervisor::new();
    let mut disabled = worker_config("disabled");
    disabled.enabled = false;
    disabled.command.program = OsString::from("no-such-host-command");

    supervisor.apply_desired_set(vec![disabled]).await.unwrap();
    let disabled_status = find_status(&supervisor.status().await, "disabled");
    assert_eq!(disabled_status.state, HostState::Disabled);
    assert_eq!(disabled_status.pid, None);

    let mut broken = worker_config("broken-init");
    broken
        .command
        .environment
        .insert("LIGAMINA_TEST_INIT_FAIL".into(), "1".into());
    assert!(matches!(
        supervisor.apply_desired_set(vec![broken]).await,
        Err(SupervisorError::ApplyFailed(_))
    ));
    let failed = find_status(&supervisor.status().await, "broken-init");
    assert_eq!(failed.state, HostState::Failed);
    assert_eq!(failed.pid, None);
    assert!(supervisor.handle("broken-init").await.is_err());
    supervisor.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn one_host_crash_does_not_interrupt_another_host_and_old_epoch_is_rejected() {
    let supervisor = HostSupervisor::new();
    let crashed = worker_config("crashed");
    let mut healthy = worker_config("healthy");
    healthy.restart.max_restarts = 1;
    supervisor
        .apply_desired_set(vec![crashed, healthy])
        .await
        .unwrap();

    let old_handle = supervisor.handle("crashed").await.unwrap();
    let error = old_handle
        .request("host.crash", Value::Null, Duration::from_secs(2))
        .await
        .unwrap_err();
    assert!(matches!(error, SupervisorError::Rpc(_)));
    wait_for_status(&supervisor, "crashed", |status| {
        status.state == HostState::Failed
    })
    .await;

    let healthy_handle = supervisor.handle("healthy").await.unwrap();
    assert_eq!(
        healthy_handle
            .request(
                "host.echo",
                json!({"value":"still alive"}),
                Duration::from_secs(2)
            )
            .await
            .unwrap(),
        json!({"value":"still alive"})
    );
    assert!(matches!(
        old_handle
            .request("host.echo", Value::Null, Duration::from_secs(1))
            .await,
        Err(SupervisorError::StaleEpoch { .. })
    ));

    supervisor.shutdown().await;
    assert!(matches!(
        healthy_handle
            .request("host.echo", Value::Null, Duration::from_secs(1))
            .await,
        Err(SupervisorError::StaleEpoch { .. })
    ));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn shutdown_stops_managed_hosts_concurrently_within_one_host_timeout() {
    let supervisor = HostSupervisor::new();
    let mut first = worker_config("shutdown-first");
    let mut second = worker_config("shutdown-second");
    for host in [&mut first, &mut second] {
        host.command.environment.insert(
            OsString::from("LIGAMINA_TEST_SHUTDOWN_DELAY_SECONDS"),
            OsString::from("2"),
        );
    }
    supervisor
        .apply_desired_set(vec![first, second])
        .await
        .unwrap();
    for host_id in ["shutdown-first", "shutdown-second"] {
        let status = wait_for_status(&supervisor, host_id, |status| {
            status.state == HostState::Active && status.pid.is_some()
        })
        .await;
        assert_eq!(status.state, HostState::Active);
    }

    let started = std::time::Instant::now();
    supervisor.shutdown().await;
    assert!(
        started.elapsed() < Duration::from_millis(3500),
        "two two-second Host shutdowns should overlap: {:?}",
        started.elapsed()
    );
    for host_id in ["shutdown-first", "shutdown-second"] {
        let status = find_status(&supervisor.status().await, host_id);
        assert_eq!(status.state, HostState::Disabled);
        assert_eq!(status.pid, None);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn restart_budget_is_bounded_and_each_restarted_process_gets_a_new_epoch() {
    let supervisor = HostSupervisor::new();
    let mut config = worker_config("restartable");
    config.restart.max_restarts = 1;
    config.restart.backoff = Duration::from_millis(20);
    supervisor.apply_desired_set(vec![config]).await.unwrap();

    let first = supervisor.handle("restartable").await.unwrap();
    let first_epoch = first.epoch();
    let _ = first
        .request("host.crash", Value::Null, Duration::from_secs(2))
        .await;
    let restarted = wait_for_status(&supervisor, "restartable", |status| {
        status.state == HostState::Active && status.restart_count == 1
    })
    .await;
    assert!(restarted.epoch > first_epoch);
    assert!(matches!(
        first
            .request("host.echo", Value::Null, Duration::from_secs(1))
            .await,
        Err(SupervisorError::StaleEpoch { .. })
    ));

    let second = supervisor.handle("restartable").await.unwrap();
    let _ = second
        .request("host.crash", Value::Null, Duration::from_secs(2))
        .await;
    let failed = wait_for_status(&supervisor, "restartable", |status| {
        status.state == HostState::Failed
    })
    .await;
    assert_eq!(failed.restart_count, 1);
    supervisor.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn host_originated_core_requests_carry_the_active_host_epoch() {
    let supervisor =
        HostSupervisor::with_handler(|host_id, epoch, _session, method, params| async move {
            if method != "core.test" {
                return Err(ligamina_core::transport::RpcError::Remote {
                    code: -32601,
                    message: format!("unexpected Core request: {method}"),
                });
            }
            Ok(json!({
                "host_id": host_id,
                "epoch": epoch,
                "value": params.get("value"),
            }))
        });
    supervisor
        .apply_desired_set(vec![worker_config("origin")])
        .await
        .unwrap();
    let handle = supervisor.handle("origin").await.unwrap();
    assert_eq!(
        handle
            .request(
                "host.call-core",
                json!({"value": 42}),
                Duration::from_secs(2),
            )
            .await
            .unwrap(),
        json!({"host_id":"origin", "epoch":handle.epoch(), "value":42})
    );
    supervisor.shutdown().await;
}

#[cfg(any(unix, windows))]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn shutdown_cleans_up_host_grandchildren() {
    let supervisor = HostSupervisor::new();
    supervisor
        .apply_desired_set(vec![worker_config("tree")])
        .await
        .unwrap();
    let handle = supervisor.handle("tree").await.unwrap();
    let child = handle
        .request("host.spawn-child", Value::Null, Duration::from_secs(2))
        .await
        .unwrap()["pid"]
        .as_u64()
        .expect("worker returned the descendant PID") as i32;
    assert!(process_is_running(child));

    supervisor.shutdown().await;
    timeout(Duration::from_secs(3), async {
        while process_is_running(child) {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("Host descendant survived supervisor shutdown");
}

#[cfg(unix)]
fn process_is_running(pid: i32) -> bool {
    let Ok(status) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else {
        return false;
    };
    !status
        .split_whitespace()
        .nth(2)
        .is_some_and(|state| state == "Z")
}

#[cfg(windows)]
fn process_is_running(pid: i32) -> bool {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
    };

    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid as u32) };
    if process.is_null() {
        false
    } else {
        unsafe { CloseHandle(process) };
        true
    }
}
