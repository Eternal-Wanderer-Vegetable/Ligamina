use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

use ligamina_cli::mcp::LigaminaMcp;
use ligamina_core::broker::{
    BrokerError, CapabilityBroker, CapabilityDescriptor, InvocationContext,
};
use rmcp::{
    ClientHandler, RoleClient, ServiceExt,
    model::{CallToolRequest, CallToolRequestParams, ClientConfig, ClientRequest, ProtocolVersion},
    service::{MaybeSendFuture, NotificationContext, PeerRequestOptions},
};
use serde_json::{Value, json};
use tokio::sync::Notify;

struct DropFlag(Arc<AtomicBool>);

impl Drop for DropFlag {
    fn drop(&mut self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

#[derive(Clone, Default)]
struct TestClient {
    tools_changed: Arc<Notify>,
}

impl ClientHandler for TestClient {
    fn get_info(&self) -> ClientConfig {
        let mut config = ClientConfig::default();
        config.protocol_version = ProtocolVersion::V_2025_11_25;
        config
    }

    fn on_tool_list_changed(
        &self,
        _context: NotificationContext<RoleClient>,
    ) -> impl std::future::Future<Output = ()> + MaybeSendFuture + '_ {
        self.tools_changed.notify_one();
        std::future::ready(())
    }
}

fn number_descriptor(id: &str) -> CapabilityDescriptor {
    CapabilityDescriptor {
        capability_id: id.to_owned(),
        input_schema: json!({
            "type": "object",
            "properties": { "a": { "type": "integer" }, "b": { "type": "integer" } },
            "required": ["a", "b"],
            "additionalProperties": false
        }),
        output_schema: json!({ "type": "object", "properties": { "value": { "type": "integer" } }, "required": ["value"], "additionalProperties": false }),
        pure: true,
    }
}

#[tokio::test]
async fn legacy_mcp_lists_invokes_cancels_and_notifies_without_side_effect_grants() {
    let slow_started = Arc::new(Notify::new());
    let slow_dropped = Arc::new(AtomicBool::new(false));
    let executor_started = slow_started.clone();
    let executor_dropped = slow_dropped.clone();
    let broker = CapabilityBroker::new(
        move |_host_id, _epoch, capability_id, arguments, _context: InvocationContext| {
            let started = executor_started.clone();
            let dropped = executor_dropped.clone();
            async move {
                match capability_id.as_str() {
                    "ping" | "sample.math.add" | "sample.math.late" => {
                        let a = arguments["a"].as_i64().unwrap();
                        let b = arguments["b"].as_i64().unwrap();
                        Ok(json!({ "value": a + b }))
                    }
                    "sample.fail" => Err(BrokerError::Remote("fixture failure".to_owned())),
                    "sample.slow" => {
                        let _drop_flag = DropFlag(dropped);
                        started.notify_one();
                        std::future::pending::<Result<Value, BrokerError>>().await
                    }
                    _ => Err(BrokerError::CapabilityUnavailable(capability_id)),
                }
            }
        },
    );
    broker.activate_host("fixture", 1).await.unwrap();
    broker
        .register_snapshot(
            "fixture",
            1,
            vec![
                number_descriptor("ping"),
                number_descriptor("sample.math.add"),
                number_descriptor("sample.fail"),
                {
                    let mut descriptor = number_descriptor("sample.slow");
                    descriptor.input_schema = json!({ "type": "object" });
                    descriptor
                },
            ],
        )
        .await
        .unwrap();
    let mut impure = number_descriptor("sample.message.send");
    impure.pure = false;
    assert!(broker.register("fixture", 1, impure).await.is_err());

    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let server = LigaminaMcp::new(broker.clone());
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });

    let client = TestClient::default();
    let tools_changed = client.tools_changed.clone();
    let client_service = client.serve(client_transport).await.unwrap();
    assert_eq!(
        client_service.peer_info().unwrap().protocol_version,
        ProtocolVersion::V_2025_11_25
    );

    let tools = client_service.peer().list_tools(None).await.unwrap().tools;
    let names = tools
        .iter()
        .map(|tool| tool.name.as_ref())
        .collect::<Vec<_>>();
    assert!(names.contains(&"_ligamina_ping"));
    assert!(names.contains(&"ping"));
    assert!(names.contains(&"sample.math.add"));
    assert!(names.contains(&"sample.fail"));
    assert!(!names.contains(&"sample.message.send"));

    let result = client_service
        .peer()
        .call_tool(
            CallToolRequestParams::new("sample.math.add")
                .with_arguments(serde_json::from_value(json!({ "a": 19, "b": 23 })).unwrap()),
        )
        .await
        .unwrap();
    assert_eq!(
        serde_json::to_value(result).unwrap()["structuredContent"]["result"]["value"],
        42
    );

    let dynamic_ping = client_service
        .peer()
        .call_tool(
            CallToolRequestParams::new("ping")
                .with_arguments(serde_json::from_value(json!({ "a": 20, "b": 22 })).unwrap()),
        )
        .await
        .unwrap();
    assert_eq!(
        serde_json::to_value(dynamic_ping).unwrap()["structuredContent"]["result"]["value"],
        42
    );

    let invalid = client_service
        .peer()
        .call_tool(CallToolRequestParams::new("sample.math.add"))
        .await
        .unwrap_err();
    assert!(format!("{invalid:?}").contains("-32602"), "{invalid:?}");

    let failed = client_service
        .peer()
        .call_tool(
            CallToolRequestParams::new("sample.fail")
                .with_arguments(serde_json::from_value(json!({ "a": 1, "b": 2 })).unwrap()),
        )
        .await
        .unwrap();
    assert_eq!(serde_json::to_value(failed).unwrap()["isError"], true);

    let slow = client_service
        .peer()
        .send_cancellable_request(
            ClientRequest::CallToolRequest(CallToolRequest::new(CallToolRequestParams::new(
                "sample.slow",
            ))),
            PeerRequestOptions::no_options(),
        )
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), slow_started.notified())
        .await
        .expect("slow capability did not start");
    slow.cancel(Some("test cancellation".to_owned()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !slow_dropped.load(Ordering::SeqCst) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("MCP cancellation did not cancel the Broker invocation");

    broker
        .register("fixture", 1, number_descriptor("sample.math.late"))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), tools_changed.notified())
        .await
        .expect("tools/list_changed notification was not sent");
    let refreshed = client_service.peer().list_tools(None).await.unwrap().tools;
    assert!(refreshed.iter().any(|tool| tool.name == "sample.math.late"));

    client_service.cancel().await.unwrap();
    server_task.abort();
}
