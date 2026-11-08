//! End-to-end: native Koishi plugin commands exposed as MCP tools (ADR 0002).
//!
//! Starts the production Core runtime with a real Koishi Host and no Gateway, then
//! lists and calls the plugin's commands through the MCP server. Replies can only
//! come back through the in-Core tool sink: without a Gateway lease, any send that
//! reached the operation ledger would be rejected and the plugin would not reply.

use std::{path::PathBuf, time::Duration};

use ligamina_cli::mcp::{CommandTools, LigaminaMcp};
use ligamina_core::{
    deployment::local::LocalCommand,
    router::RouterLimits,
    runtime::{CoreRuntime, CoreRuntimeConfig, ManagedHost},
    supervisor::HostConfig,
};
use rmcp::{
    ClientHandler, ServiceExt,
    model::{CallToolRequestParams, ClientConfig, ProtocolVersion},
};
use serde_json::{Value, json};

const HOST_ID: &str = "koishi-tools";

#[derive(Clone, Default)]
struct TestClient;

impl ClientHandler for TestClient {
    fn get_info(&self) -> ClientConfig {
        let mut config = ClientConfig::default();
        config.protocol_version = ProtocolVersion::V_2025_11_25;
        config
    }
}

fn file_url(path: &std::path::Path) -> String {
    let text = path.to_string_lossy().replace('\\', "/");
    let text = text.strip_prefix("//?/").unwrap_or(&text).to_owned();
    if text.starts_with('/') {
        format!("file://{text}")
    } else {
        format!("file:///{text}")
    }
}

async fn start_runtime(work: &std::path::Path) -> CoreRuntime {
    let koishi_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../hosts/koishi")
        .canonicalize()
        .unwrap();
    let build = std::process::Command::new("node")
        .args([
            "node_modules/typescript/bin/tsc",
            "--project",
            "tsconfig.json",
        ])
        .current_dir(&koishi_dir)
        .status()
        .expect("Node.js and the locked Koishi Host dependencies (npm ci) are required");
    assert!(build.success(), "Koishi Host TypeScript build failed");

    let mut command = LocalCommand::new("node");
    command.args = vec!["dist/worker.js".into()];
    command.current_dir = Some(koishi_dir.clone());
    command.environment.insert(
        "LIGAMINA_KOISHI_PLUGINS".into(),
        file_url(&koishi_dir.join("dist/tool-smoke-plugin.js")).into(),
    );
    command.environment.insert(
        "LIGAMINA_KOISHI_DATA_DIR".into(),
        work.join("koishi-data").into_os_string(),
    );
    command.startup_timeout = Duration::from_secs(30);
    CoreRuntime::start(CoreRuntimeConfig {
        database_path: work.join("core.sqlite3"),
        hosts: vec![ManagedHost {
            config: HostConfig::new(HOST_ID, command),
            operation_grants: Vec::new(),
        }],
        gateways: Vec::new(),
        client_grants: Vec::new(),
        router_limits: RouterLimits {
            max_pending_per_conversation: 8,
            max_outstanding_events: 64,
            max_tracked_conversations: 64,
            max_concurrent_conversations: 4,
            processing_timeout: Duration::from_secs(30),
        },
    })
    .await
    .expect("Core runtime starts with the Koishi Host")
}

async fn call(
    client: &rmcp::service::RunningService<rmcp::RoleClient, TestClient>,
    name: &str,
    arguments: Value,
) -> Result<rmcp::model::CallToolResult, rmcp::ServiceError> {
    let Value::Object(arguments) = arguments else {
        panic!("arguments must be an object")
    };
    client
        .peer()
        .call_tool(CallToolRequestParams::new(name.to_owned()).with_arguments(arguments))
        .await
}

fn first_text(result: &rmcp::model::CallToolResult) -> String {
    result.content[0]
        .as_text()
        .map(|text| text.text.clone())
        .unwrap_or_default()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn koishi_commands_are_listed_and_callable_as_mcp_tools_without_a_gateway() {
    let work =
        std::env::temp_dir().join(format!("ligamina-command-tools-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&work).unwrap();
    let runtime = start_runtime(&work).await;

    let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
    while runtime
        .commands()
        .get(HOST_ID, "ligamina.echo")
        .await
        .is_none()
    {
        assert!(
            tokio::time::Instant::now() < deadline,
            "Koishi commands were not discovered"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    // Default: every discovered command is exposed.
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone())
        .with_command_tools(CommandTools::new(runtime.clone(), None));
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();

    let tools = client.peer().list_tools(None).await.unwrap().tools;
    let echo = tools
        .iter()
        .find(|tool| tool.name == "cmd.koishi-tools.ligamina.echo")
        .expect("echo command is listed as a tool");
    let description = echo.description.as_deref().unwrap_or_default();
    assert!(
        description.contains("Echo the given text back"),
        "{description}"
    );
    assert!(description.contains("[-u, --upper]"), "{description}");
    assert_eq!(
        echo.input_schema["properties"]["arguments"]["type"],
        "string"
    );
    assert!(
        tools
            .iter()
            .any(|tool| tool.name == "cmd.koishi-tools.ligamina.twice")
    );
    assert!(
        tools
            .iter()
            .any(|tool| tool.name == "ligamina.koishi.math.add")
    );

    let result = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "arguments": "-u hello tools" }),
    )
    .await
    .unwrap();
    assert_eq!(result.is_error, Some(false));
    assert_eq!(first_text(&result), "HELLO TOOLS");
    assert_eq!(
        result.content.len(),
        1,
        "tool text carries only the plugin reply, no JSON dump"
    );
    let structured = result.structured_content.clone().unwrap();
    assert_eq!(structured["messages"].as_array().unwrap().len(), 1);
    assert_eq!(
        structured["messages"][0]["segments"],
        json!([{ "type": "text", "text": "HELLO TOOLS" }])
    );
    assert_eq!(structured["dispatch"], "completed");

    // Structured input (TOOL-03): declared arguments and options are composed into
    // command text that Koishi's own parser reads back.
    assert_eq!(echo.input_schema["properties"]["value"]["type"], "string");
    assert_eq!(
        echo.input_schema["properties"]["options"]["properties"]["upper"]["type"],
        "boolean"
    );
    let result = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "value": "structured -u text", "options": { "upper": true } }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "STRUCTURED -U TEXT");
    let mixed = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "arguments": "x", "value": "y" }),
    )
    .await
    .expect("invalid arguments are reported in the tool result");
    assert_eq!(
        mixed.is_error,
        Some(true),
        "raw and structured input together are rejected"
    );

    let result = call(
        &client,
        "cmd.koishi-tools.ligamina.twice",
        json!({ "arguments": "x" }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "first:x\nsecond:x");

    // Each call gets its own virtual conversation, so concurrent calls do not mix.
    let (left, right) = tokio::join!(
        call(
            &client,
            "cmd.koishi-tools.ligamina.echo",
            json!({ "arguments": "left" })
        ),
        call(
            &client,
            "cmd.koishi-tools.ligamina.echo",
            json!({ "arguments": "right" })
        ),
    );
    assert_eq!(first_text(&left.unwrap()), "left");
    assert_eq!(first_text(&right.unwrap()), "right");

    // Arguments outside the input schema are a tool execution error (isError), not a
    // protocol error, so the session stays usable and the model sees why.
    let rejected = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "text": "x" }),
    )
    .await
    .expect("invalid arguments are reported in the tool result");
    assert_eq!(
        rejected.is_error,
        Some(true),
        "unexpected argument fields are rejected"
    );
    assert!(
        first_text(&rejected).contains("unexpected argument field: text"),
        "{}",
        first_text(&rejected)
    );
    let after = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "arguments": "still here" }),
    )
    .await
    .unwrap();
    assert_eq!(
        first_text(&after),
        "still here",
        "the session survives a rejected call"
    );
    assert!(
        call(&client, "cmd.koishi-tools.missing", json!({}))
            .await
            .is_err(),
        "unknown command tools are rejected"
    );
    client.cancel().await.unwrap();
    let _ = server_task.await;

    // Allowlist: only the exact echo tool is exposed or callable.
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone()).with_command_tools(CommandTools::new(
        runtime.clone(),
        Some(vec!["cmd.koishi-tools.ligamina.echo".to_owned()]),
    ));
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();
    let names = client
        .peer()
        .list_tools(None)
        .await
        .unwrap()
        .tools
        .into_iter()
        .map(|tool| tool.name.to_string())
        .collect::<Vec<_>>();
    assert!(names.contains(&"cmd.koishi-tools.ligamina.echo".to_owned()));
    assert!(!names.contains(&"cmd.koishi-tools.ligamina.twice".to_owned()));
    assert!(
        call(
            &client,
            "cmd.koishi-tools.ligamina.twice",
            json!({ "arguments": "x" })
        )
        .await
        .is_err()
    );
    client.cancel().await.unwrap();
    let _ = server_task.await;

    // Without command tools the MCP surface is unchanged.
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone());
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();
    let tools = client.peer().list_tools(None).await.unwrap().tools;
    assert!(tools.iter().all(|tool| !tool.name.starts_with("cmd.")));
    client.cancel().await.unwrap();
    let _ = server_task.await;

    runtime.shutdown().await;
    let _ = std::fs::remove_dir_all(&work);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn command_tools_report_timeouts_refuse_late_replies_and_survive_host_restart() {
    let work =
        std::env::temp_dir().join(format!("ligamina-command-tools-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&work).unwrap();
    let runtime = start_runtime(&work).await;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
    while runtime
        .commands()
        .get(HOST_ID, "ligamina.slow")
        .await
        .is_none()
    {
        assert!(
            tokio::time::Instant::now() < deadline,
            "Koishi commands were not discovered"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    // Timeout: the plugin replies after the deadline. The call fails explicitly and the
    // late reply is refused inside Core instead of being collected or delivered.
    let error = runtime
        .invoke_command_with_deadline(HOST_ID, "ligamina.slow", "1500", Duration::from_millis(300))
        .await
        .unwrap_err();
    assert_eq!(
        error,
        ligamina_core::runtime::CommandError::TimedOut(Duration::from_millis(300))
    );
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while runtime.late_tool_refusals() == 0 {
        assert!(
            tokio::time::Instant::now() < deadline,
            "late reply was not refused"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let outcome = runtime
        .invoke_command(HOST_ID, "ligamina.slow", "0")
        .await
        .unwrap();
    assert_eq!(
        outcome.text(),
        "slow:done",
        "Host keeps working after a timed-out call"
    );

    // Unknown commands are rejected before anything is dispatched.
    assert!(matches!(
        runtime
            .invoke_command(HOST_ID, "ligamina.missing", "")
            .await,
        Err(ligamina_core::runtime::CommandError::UnknownCommand { .. })
    ));

    // Restart: the plugin kills its Host mid-command. The MCP call reports an error,
    // the supervisor restarts the Host, and the new generation's commands work.
    let old_epoch = runtime
        .commands()
        .get(HOST_ID, "ligamina.echo")
        .await
        .unwrap()
        .host_epoch;
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone())
        .with_command_tools(CommandTools::new(runtime.clone(), None));
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();
    let crashed = call(&client, "cmd.koishi-tools.ligamina.crash", json!({}))
        .await
        .unwrap();
    assert_eq!(crashed.is_error, Some(true));
    assert!(crashed.structured_content.is_none());

    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    loop {
        if let Some(info) = runtime.commands().get(HOST_ID, "ligamina.echo").await
            && info.host_epoch > old_epoch
        {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "Host did not restart with commands"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let result = call(
        &client,
        "cmd.koishi-tools.ligamina.echo",
        json!({ "arguments": "after restart" }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "after restart");
    client.cancel().await.unwrap();
    let _ = server_task.await;

    runtime.shutdown().await;
    let _ = std::fs::remove_dir_all(&work);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn nonebot_commands_are_callable_as_mcp_tools_with_onebot_identities() {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let work =
        std::env::temp_dir().join(format!("ligamina-nonebot-tools-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&work).unwrap();

    let mut command = LocalCommand::new("uv");
    command.args = [
        "run",
        "--locked",
        "--project",
        "hosts/nonebot",
        "--python",
        "3.12.14",
        "python",
        "-m",
        "ligamina_nonebot.worker",
    ]
    .into_iter()
    .map(Into::into)
    .collect();
    command.current_dir = Some(repository_root.clone());
    command.environment.insert(
        "PYTHONPATH".into(),
        repository_root.join("hosts/nonebot").into_os_string(),
    );
    command.environment.insert(
        "LIGAMINA_NONEBOT_PLUGINS".into(),
        "ligamina_nonebot.tool_smoke_plugin".into(),
    );
    command.startup_timeout = Duration::from_secs(120);
    let runtime = CoreRuntime::start(CoreRuntimeConfig {
        database_path: work.join("core.sqlite3"),
        hosts: vec![ManagedHost {
            config: HostConfig::new("nonebot-tools", command),
            operation_grants: Vec::new(),
        }],
        gateways: Vec::new(),
        client_grants: Vec::new(),
        router_limits: RouterLimits {
            max_pending_per_conversation: 8,
            max_outstanding_events: 64,
            max_tracked_conversations: 64,
            max_concurrent_conversations: 4,
            processing_timeout: Duration::from_secs(30),
        },
    })
    .await
    .expect("Core runtime starts with the NoneBot Host (requires uv and the locked environment)");

    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    while runtime
        .commands()
        .get("nonebot-tools", "ligamina.echo")
        .await
        .is_none()
    {
        if tokio::time::Instant::now() >= deadline {
            panic!(
                "NoneBot commands were not discovered: hosts={:?} commands={:?}",
                runtime.host_status().await,
                runtime.commands().list().await
            );
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone()).with_command_tools(CommandTools::new(
        runtime.clone(),
        Some(vec!["cmd.nonebot-tools.*".to_owned()]),
    ));
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();

    let tools = client.peer().list_tools(None).await.unwrap().tools;
    let echo = tools
        .iter()
        .find(|tool| tool.name == "cmd.nonebot-tools.ligamina.echo")
        .expect("NoneBot echo command is listed");
    let description = echo.description.as_deref().unwrap_or_default();
    assert!(description.contains("nonebot"), "{description}");
    // Free-text NoneBot commands require `arguments`, so clients never call them with `{}`.
    assert_eq!(echo.input_schema["required"], json!(["arguments"]));
    assert!(
        description.contains("Echo commands for the command tool surface"),
        "{description}"
    );

    let result = call(
        &client,
        "cmd.nonebot-tools.ligamina.echo",
        json!({ "arguments": "hello nonebot" }),
    )
    .await
    .unwrap();
    assert_eq!(result.is_error, Some(false));
    assert_eq!(first_text(&result), "HELLO NONEBOT");

    let result = call(
        &client,
        "cmd.nonebot-tools.ligamina.twice",
        json!({ "arguments": "y" }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "first:y\nsecond:y");

    let (left, right) = tokio::join!(
        call(
            &client,
            "cmd.nonebot-tools.ligamina.echo",
            json!({ "arguments": "left" })
        ),
        call(
            &client,
            "cmd.nonebot-tools.ligamina.echo",
            json!({ "arguments": "right" })
        ),
    );
    assert_eq!(first_text(&left.unwrap()), "LEFT");
    assert_eq!(first_text(&right.unwrap()), "RIGHT");
    client.cancel().await.unwrap();
    let _ = server_task.await;

    let error = runtime
        .invoke_command_with_deadline(
            "nonebot-tools",
            "ligamina.slow",
            "1500",
            Duration::from_millis(300),
        )
        .await
        .unwrap_err();
    assert_eq!(
        error,
        ligamina_core::runtime::CommandError::TimedOut(Duration::from_millis(300))
    );
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while runtime.late_tool_refusals() == 0 {
        assert!(
            tokio::time::Instant::now() < deadline,
            "late NoneBot reply was not refused"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    runtime.shutdown().await;
    let _ = std::fs::remove_dir_all(&work);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn astrbot_commands_are_callable_as_mcp_tools_through_the_real_framework() {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let work =
        std::env::temp_dir().join(format!("ligamina-astrbot-tools-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&work).unwrap();

    let mut command = LocalCommand::new("uv");
    command.args = [
        "run",
        "--locked",
        "--project",
        "hosts/astrbot",
        "--python",
        "3.12.14",
        "python",
        "-m",
        "ligamina_astrbot.worker",
    ]
    .into_iter()
    .map(Into::into)
    .collect();
    command.current_dir = Some(repository_root.clone());
    command.environment.insert(
        "PYTHONPATH".into(),
        repository_root.join("hosts/astrbot").into_os_string(),
    );
    command.environment.insert(
        "LIGAMINA_ASTRBOT_PLUGINS".into(),
        repository_root
            .join("hosts/astrbot/tests/fixtures/ligamina_smoke")
            .into_os_string(),
    );
    command.environment.insert(
        "LIGAMINA_ASTRBOT_DATA_DIR".into(),
        work.join("astrbot-data").into_os_string(),
    );
    command.startup_timeout = Duration::from_secs(300);
    let runtime = CoreRuntime::start(CoreRuntimeConfig {
        database_path: work.join("core.sqlite3"),
        hosts: vec![ManagedHost {
            config: HostConfig::new("astrbot-tools", command),
            operation_grants: Vec::new(),
        }],
        gateways: Vec::new(),
        client_grants: Vec::new(),
        router_limits: RouterLimits {
            max_pending_per_conversation: 8,
            max_outstanding_events: 64,
            max_tracked_conversations: 64,
            max_concurrent_conversations: 4,
            processing_timeout: Duration::from_secs(30),
        },
    })
    .await
    .expect("Core runtime starts with the AstrBot Host (requires uv and the locked environment)");

    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    while runtime
        .commands()
        .get("astrbot-tools", "ligamina_add")
        .await
        .is_none()
    {
        assert!(
            tokio::time::Instant::now() < deadline,
            "AstrBot commands were not discovered"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = LigaminaMcp::new(runtime.broker().clone())
        .with_command_tools(CommandTools::new(runtime.clone(), None));
    let server_task = tokio::spawn(async move { server.serve(server_transport).await });
    let client = TestClient.serve(client_transport).await.unwrap();

    let tools = client.peer().list_tools(None).await.unwrap().tools;
    let names = tools
        .iter()
        .map(|tool| tool.name.to_string())
        .collect::<Vec<_>>();
    assert!(
        names.contains(&"cmd.astrbot-tools.ligamina_echo".to_owned()),
        "{names:?}"
    );
    // AstrBot's built-in admin commands (plugin install, config) are never exposed.
    assert!(
        names
            .iter()
            .all(|name| !name.contains(".plugin") && !name.contains(".help")),
        "{names:?}"
    );
    let add = tools
        .iter()
        .find(|tool| tool.name == "cmd.astrbot-tools.ligamina_add")
        .unwrap();
    assert_eq!(add.input_schema["properties"]["left"]["type"], "integer");
    assert_eq!(add.input_schema["required"], json!(["left", "right"]));

    let result = call(
        &client,
        "cmd.astrbot-tools.ligamina_add",
        json!({ "left": 2, "right": 40 }),
    )
    .await
    .unwrap();
    assert_eq!(result.is_error, Some(false));
    assert_eq!(first_text(&result), "sum:42");

    let result = call(
        &client,
        "cmd.astrbot-tools.ligamina_echo",
        json!({ "text": "hello astrbot" }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "HELLO ASTRBOT");

    let result = call(
        &client,
        "cmd.astrbot-tools.ligamina_twice",
        json!({ "arguments": "y" }),
    )
    .await
    .unwrap();
    assert_eq!(first_text(&result), "first:y\nsecond:y");

    let (left, right) = tokio::join!(
        call(
            &client,
            "cmd.astrbot-tools.ligamina_echo",
            json!({ "text": "left" })
        ),
        call(
            &client,
            "cmd.astrbot-tools.ligamina_echo",
            json!({ "text": "right" })
        ),
    );
    assert_eq!(first_text(&left.unwrap()), "LEFT");
    assert_eq!(first_text(&right.unwrap()), "RIGHT");
    client.cancel().await.unwrap();
    let _ = server_task.await;

    runtime.shutdown().await;
    let _ = std::fs::remove_dir_all(&work);
}
