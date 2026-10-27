use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};

use ligamina_core::broker::{
    BrokerError, CapabilityBroker, CapabilityDescriptor, InvocationContext,
};
use ligamina_core::transport::{HostSession, MAX_FRAME_BYTES, MAX_IN_FLIGHT_REQUESTS, RpcError};
use serde_json::{Value, json};
use tokio::{
    io::AsyncBufReadExt,
    process::{Child, Command},
    sync::RwLock,
    time::{sleep, timeout},
};
use tokio_util::sync::CancellationToken;

struct TestWorker {
    child: Child,
    session: HostSession,
}

impl TestWorker {
    async fn stop(mut self) {
        let _ = self.child.kill().await;
        let _ = self.child.wait().await;
    }
}

async fn start_worker(kind: &str) -> TestWorker {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let mut command = match kind {
        "python" => {
            let mut command = Command::new("uv");
            command.args([
                "run",
                "--locked",
                "--project",
                "hosts/nonebot",
                "--python",
                "3.12.14",
                "python",
                "lab/test_workers/python_worker.py",
            ]);
            command.env(
                "UV_PROJECT_ENVIRONMENT",
                std::env::temp_dir().join("ligamina-host-session-python"),
            );
            command
        }
        "node" => {
            let mut command = Command::new("node");
            command.arg("lab/test_workers/node_worker.mjs");
            command
        }
        other => panic!("unknown worker kind: {other}"),
    };
    command.kill_on_drop(true);
    let mut child = command
        .current_dir(repository_root)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("could not spawn {kind} test worker: {error}"));

    let stdout = child.stdout.take().unwrap();
    let stdin = child.stdin.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(stderr).lines();
        while let Ok(Some(_diagnostic)) = lines.next_line().await {
            // The stderr stream is drained independently; it never reaches the protocol reader.
        }
    });

    let session = HostSession::new(stdout, stdin, |_session, method, params| async move {
        if method == "core.reverse" {
            Ok(json!({"received":params.get("value")}))
        } else {
            Err(RpcError::Remote {
                code: -32601,
                message: format!("core method not found: {method}"),
            })
        }
    });
    TestWorker { child, session }
}

async fn start_native_koishi() -> TestWorker {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let koishi_dir = repository_root.join("hosts/koishi");
    let build = Command::new("node")
        .args(["node_modules/typescript/bin/tsc", "--project", "tsconfig.json"])
        .current_dir(&koishi_dir)
        .status()
        .await
        .expect("Node.js and the locked TypeScript compiler must be installed for the native cross-Host fixture");
    assert!(build.success(), "Koishi Host TypeScript build failed");

    let data_directory =
        std::env::temp_dir().join(format!("ligamina-cross-host-{}", uuid::Uuid::new_v4()));
    let mut child = Command::new("node")
        .arg("dist/worker.js")
        .current_dir(&koishi_dir)
        .env("LIGAMINA_KOISHI_DATA_DIR", data_directory)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("Node.js must be installed for the native cross-Host fixture");
    let stdout = child.stdout.take().unwrap();
    let stdin = child.stdin.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(stderr).lines();
        while let Ok(Some(_diagnostic)) = lines.next_line().await {}
    });
    let session = HostSession::new(stdout, stdin, |_session, method, _params| async move {
        Err(RpcError::Remote {
            code: -32601,
            message: format!("core method not found: {method}"),
        })
    });
    TestWorker { child, session }
}

async fn start_native_nonebot(
    broker: CapabilityBroker,
    operations: Arc<Mutex<Vec<Value>>>,
) -> TestWorker {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let nonebot_dir = repository_root.join("hosts/nonebot");
    let mut child = Command::new("uv")
        .args([
            "run",
            "--locked",
            "--project",
            "hosts/nonebot",
            "--python",
            "3.12.14",
            "python",
            "-m",
            "ligamina_nonebot.worker",
        ])
        .current_dir(&repository_root)
        .env("PYTHONPATH", &nonebot_dir)
        .env(
            "LIGAMINA_NONEBOT_PLUGINS",
            "ligamina_nonebot.cross_host_smoke_plugin",
        )
        .env("LIGAMINA_NONEBOT_NICKNAMES", "Bot")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("uv must be installed for the native cross-Host fixture");
    let stdout = child.stdout.take().unwrap();
    let stdin = child.stdin.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(stderr).lines();
        while let Ok(Some(_diagnostic)) = lines.next_line().await {}
    });
    let session = HostSession::new(stdout, stdin, move |_session, method, params| {
        let broker = broker.clone();
        let operations = operations.clone();
        async move {
            match method.as_str() {
                "service.invoke" => {
                    let trace_id = params
                        .get("trace_id")
                        .and_then(Value::as_str)
                        .unwrap_or("cross-host");
                    broker
                        .invoke_from_host(
                            "nonebot",
                            7,
                            params
                                .get("capability_id")
                                .and_then(Value::as_str)
                                .unwrap_or(""),
                            params.get("arguments").cloned().unwrap_or(Value::Null),
                            InvocationContext::from_host("nonebot", 7, trace_id),
                            Duration::from_secs(5),
                        )
                        .await
                        .map_err(|error| RpcError::Remote {
                            code: -32000,
                            message: error.to_string(),
                        })
                }
                "platform.execute" => {
                    operations.lock().unwrap().push(params);
                    Ok(json!({"status":"succeeded", "platform_message_id":"701"}))
                }
                _ => Err(RpcError::Remote {
                    code: -32601,
                    message: format!("core method not found: {method}"),
                }),
            }
        }
    });
    TestWorker { child, session }
}

async fn wait_for_late_responses(session: &HostSession, expected: usize) {
    timeout(Duration::from_secs(3), async {
        while session.late_response_count() < expected {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("late host response was not observed");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn python_and_node_workers_support_concurrent_bidirectional_requests() {
    for kind in ["python", "node"] {
        let worker = start_worker(kind).await;
        let calls = (0..16).map(|value| {
            let session = worker.session.clone();
            async move {
                session
                    .request(
                        "host.echo",
                        json!({"value": value}),
                        Duration::from_secs(10),
                    )
                    .await
                    .unwrap()
            }
        });
        let results = futures::future::join_all(calls).await;
        for (value, result) in results.iter().enumerate() {
            assert_eq!(result["worker"], kind);
            assert_eq!(result["echo"], value);
            assert_eq!(result["reverse"]["received"], value);
        }
        worker.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn timeout_cancel_and_late_responses_release_the_correlation_slot() {
    let worker = start_worker("node").await;
    let timeout = worker
        .session
        .request(
            "host.delay",
            json!({"milliseconds": 120}),
            Duration::from_millis(20),
        )
        .await;
    assert!(matches!(timeout, Err(RpcError::DeadlineExceeded(_))));
    wait_for_late_responses(&worker.session, 1).await;
    assert_eq!(worker.session.late_response_count(), 1);
    assert_eq!(worker.session.pending_count().await, 0);

    let cancellation = CancellationToken::new();
    let task = {
        let session = worker.session.clone();
        let cancellation = cancellation.clone();
        tokio::spawn(async move {
            session
                .request_cancellable(
                    "host.delay",
                    json!({"milliseconds": 120}),
                    Duration::from_secs(2),
                    cancellation,
                )
                .await
        })
    };
    sleep(Duration::from_millis(20)).await;
    cancellation.cancel();
    assert!(matches!(task.await.unwrap(), Err(RpcError::Cancelled(_))));
    wait_for_late_responses(&worker.session, 2).await;
    assert_eq!(worker.session.late_response_count(), 2);
    worker.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn malformed_and_oversized_frames_close_the_session() {
    for (method, expected) in [
        ("host.malformed", RpcError::Malformed("".to_owned())),
        ("host.oversize", RpcError::FrameTooLarge),
    ] {
        let worker = start_worker("node").await;
        let pending = {
            let session = worker.session.clone();
            tokio::spawn(async move {
                session
                    .request(method, Value::Null, Duration::from_secs(10))
                    .await
            })
        };
        let error = worker.session.wait_closed().await;
        match expected {
            RpcError::Malformed(_) => assert!(matches!(error, RpcError::Malformed(_))),
            RpcError::FrameTooLarge => assert_eq!(error, RpcError::FrameTooLarge),
            other => panic!("unexpected expected error: {other:?}"),
        }
        assert!(pending.await.unwrap().is_err());
        worker.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn oversized_core_request_closes_the_session_and_fails_pending_request() {
    let worker = start_worker("node").await;
    let result = worker
        .session
        .request(
            "host.echo",
            json!({"padding":"x".repeat(MAX_FRAME_BYTES)}),
            Duration::from_secs(2),
        )
        .await;

    assert_eq!(result, Err(RpcError::FrameTooLarge));
    assert_eq!(worker.session.wait_closed().await, RpcError::FrameTooLarge);
    assert_eq!(worker.session.pending_count().await, 0);
    worker.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn process_exit_fails_pending_requests_and_in_flight_limit_is_enforced() {
    let mut worker = start_worker("node").await;
    let calls: Vec<_> = (0..MAX_IN_FLIGHT_REQUESTS)
        .map(|_| {
            let session = worker.session.clone();
            tokio::spawn(async move {
                session
                    .request(
                        "host.delay",
                        json!({"milliseconds": 200}),
                        Duration::from_secs(2),
                    )
                    .await
            })
        })
        .collect();
    for _ in 0..100 {
        if worker.session.pending_count().await == MAX_IN_FLIGHT_REQUESTS {
            break;
        }
        sleep(Duration::from_millis(2)).await;
    }
    assert_eq!(worker.session.pending_count().await, MAX_IN_FLIGHT_REQUESTS);
    assert_eq!(
        worker
            .session
            .request("host.delay", Value::Null, Duration::from_secs(1))
            .await,
        Err(RpcError::Overloaded)
    );

    worker.child.kill().await.unwrap();
    worker.child.wait().await.unwrap();
    // Either the reader observes stdout EOF first or the writer observes the
    // closed child stdin first. Both are valid terminal signals for a killed
    // Host process; every pending request must receive the winning signal.
    let terminal_error = worker.session.wait_closed().await;
    assert!(matches!(
        &terminal_error,
        RpcError::Closed(_) | RpcError::Io(_)
    ));
    for call in calls {
        assert_eq!(call.await.unwrap(), Err(terminal_error.clone()));
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn native_nonebot_to_koishi_capability_call_uses_the_broker_and_current_generations() {
    let target_sessions = Arc::new(RwLock::new(HashMap::<String, HostSession>::new()));
    let sessions_for_invoker = target_sessions.clone();
    let broker = CapabilityBroker::new(
        move |host_id, epoch, capability_id, arguments, _context| {
            let sessions = sessions_for_invoker.clone();
            async move {
                let session = sessions
                    .read()
                    .await
                    .get(&host_id)
                    .cloned()
                    .ok_or_else(|| BrokerError::HostUnavailable(host_id.clone()))?;
                session
                    .request(
                        "capability.invoke",
                        json!({"capability_id": capability_id, "arguments": arguments, "host_epoch": epoch}),
                        Duration::from_secs(5),
                    )
                    .await
                    .map_err(|error| BrokerError::Remote(error.to_string()))
            }
        },
    );

    let koishi = start_native_koishi().await;
    let koishi_ready = koishi
        .session
        .request(
            "host.initialize",
            json!({"host_id":"koishi", "epoch":11, "protocol_version":"0.1"}),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
    assert_eq!(koishi_ready["ready"], true);
    let koishi_capabilities: Vec<CapabilityDescriptor> =
        serde_json::from_value(koishi_ready["capabilities"].clone()).unwrap();
    assert_eq!(
        koishi_capabilities[0].capability_id,
        "ligamina.koishi.math.add"
    );
    broker.activate_host("koishi", 11).await.unwrap();
    broker
        .register_snapshot("koishi", 11, koishi_capabilities)
        .await
        .unwrap();
    target_sessions
        .write()
        .await
        .insert("koishi".to_owned(), koishi.session.clone());

    let operations = Arc::new(Mutex::new(Vec::<Value>::new()));
    let nonebot = start_native_nonebot(broker.clone(), operations.clone()).await;
    let nonebot_ready = nonebot
        .session
        .request(
            "host.initialize",
            json!({"host_id":"nonebot", "epoch":7, "protocol_version":"0.1"}),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
    assert_eq!(nonebot_ready["ready"], true);
    let nonebot_capabilities: Vec<CapabilityDescriptor> =
        serde_json::from_value(nonebot_ready["capabilities"].clone()).unwrap();
    assert_eq!(
        nonebot_capabilities[0].capability_id,
        "ligamina.nonebot.math.add"
    );
    broker.activate_host("nonebot", 7).await.unwrap();
    broker
        .register_snapshot("nonebot", 7, nonebot_capabilities)
        .await
        .unwrap();

    let dispatched = nonebot
        .session
        .request(
            "event.dispatch",
            json!({
                "schema_version":"ligamina/0.1",
                "event_id":"cross-host-event",
                "trace_id":"cross-host-trace",
                "platform":"onebot.v11",
                "account_id":"9000",
                "conversation":{"kind":"group","id":"42"},
                "actor_id":"23",
                "timestamp":"2026-10-10T01:02:03Z",
                "target_host":"nonebot",
                "message":[{"type":"text","text":"cross-host"}],
                "extensions":{"onebot.v11":{"message_id":"78","sender":{"nickname":"Alice"}}}
            }),
            Duration::from_secs(10),
        )
        .await
        .unwrap();
    assert_eq!(dispatched["accepted"], true);

    let operations = operations.lock().unwrap();
    assert_eq!(operations.len(), 1);
    assert_eq!(operations[0]["operation"], "send_message");
    assert_eq!(operations[0]["trace_id"], "cross-host-trace");
    assert_eq!(
        operations[0]["params"]["message"][0]["text"],
        "cross-host:42"
    );
    drop(operations);

    let _ = nonebot
        .session
        .request("host.shutdown", Value::Null, Duration::from_secs(5))
        .await;
    let _ = koishi
        .session
        .request("host.shutdown", Value::Null, Duration::from_secs(5))
        .await;
    nonebot.stop().await;
    koishi.stop().await;
}
