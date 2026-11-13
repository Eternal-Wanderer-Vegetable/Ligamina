use std::{collections::BTreeMap, ffi::OsString, path::PathBuf, time::Duration};

use ligamina_core::{
    router::RouterLimits,
    runtime::{CoreRuntime, CoreRuntimeConfig, ManagedHost},
    supervisor::{HostConfig, RestartPolicy},
};
use serde_json::Value;

fn inventory_worker() -> HostConfig {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("workspace root should exist");
    let mut command = ligamina_core::deployment::local::LocalCommand::new("uv");
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
            .join(format!(
                "ligamina-runtime-inventory-{}",
                uuid::Uuid::new_v4()
            ))
            .into_os_string(),
    )]);
    HostConfig {
        id: "inventory-test-host".to_owned(),
        enabled: true,
        command,
        restart: RestartPolicy {
            max_restarts: 0,
            backoff: Duration::from_millis(20),
        },
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn core_retains_host_runtime_inventory_by_epoch_and_clears_it_on_shutdown() {
    let database_path = std::env::temp_dir().join(format!(
        "ligamina-runtime-inventory-{}.sqlite3",
        uuid::Uuid::new_v4()
    ));
    let mut initial_host = inventory_worker();
    initial_host.command.environment.insert(
        OsString::from("LIGAMINA_TEST_INVENTORY_PACKAGE"),
        OsString::from("core-runtime-inventory-fixture"),
    );
    let runtime = CoreRuntime::start(CoreRuntimeConfig {
        database_path,
        hosts: vec![ManagedHost {
            config: initial_host,
            operation_grants: Vec::new(),
        }],
        gateways: Vec::new(),
        client_grants: Vec::new(),
        router_limits: RouterLimits::default(),
    })
    .await
    .expect("Core should start with an optional Host inventory");

    let snapshots = runtime.runtime_inventories().await;
    assert_eq!(snapshots.len(), 1);
    assert_eq!(snapshots[0]["host_id"], "inventory-test-host");
    assert!(snapshots[0]["host_epoch"].as_u64().unwrap() > 0);
    let inventory: &Value = &snapshots[0]["inventory"];
    assert_eq!(inventory["source"]["kind"], "runtime");
    assert_eq!(
        inventory["plugin"]["package"],
        "core-runtime-inventory-fixture"
    );

    let first_epoch = snapshots[0]["host_epoch"].as_u64().unwrap();
    let mut replacement_host = inventory_worker();
    replacement_host.command.environment.insert(
        OsString::from("LIGAMINA_TEST_INVENTORY_PACKAGE"),
        OsString::from("replacement-inventory-fixture"),
    );
    runtime
        .apply_desired_hosts(vec![replacement_host])
        .await
        .expect("Core should replace the Host generation");
    let replaced = runtime.runtime_inventories().await;
    assert_eq!(replaced.len(), 1);
    assert!(replaced[0]["host_epoch"].as_u64().unwrap() > first_epoch);
    assert_eq!(
        replaced[0]["inventory"]["plugin"]["package"],
        "replacement-inventory-fixture"
    );

    runtime.shutdown().await;
    assert!(runtime.runtime_inventories().await.is_empty());
}
