//! MCP 2025-11-25 adapter over the generation-scoped capability Broker.

use std::{borrow::Cow, collections::HashSet, sync::Arc, time::Duration};

use ligamina_core::{
    broker::{BrokerError, CapabilityBroker},
    commands::CommandInfo,
    runtime::{CommandError, CoreRuntime},
};
use rmcp::{
    ErrorData as McpError, RoleServer, ServerHandler,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ListToolsResult,
        ProtocolVersion, ServerCapabilities, ServerConfig, Tool,
    },
    service::{MaybeSendFuture, NotificationContext, RequestContext},
};
use serde_json::{Map, Value, json};
use tokio::sync::broadcast;

const TOOL_DEADLINE: Duration = Duration::from_secs(30);
const PING_TOOL: &str = "_ligamina_ping";
/// Prefix of tools backed by native plugin commands (ADR 0002).
pub const COMMAND_TOOL_PREFIX: &str = ligamina_core::commands::COMMAND_TOOL_PREFIX;
const MAX_TOOL_NAME: usize = 128;

/// Which native plugin commands are exposed as MCP tools.
#[derive(Clone)]
pub struct CommandTools {
    runtime: CoreRuntime,
    /// `None` exposes every command; otherwise exact tool names or `cmd.<host>.*`.
    allow: Option<Arc<HashSet<String>>>,
}

impl CommandTools {
    pub fn new(runtime: CoreRuntime, allow: Option<Vec<String>>) -> Self {
        Self {
            runtime,
            allow: allow.map(|list| Arc::new(list.into_iter().collect())),
        }
    }

    fn allows(&self, tool_name: &str, host_id: &str) -> bool {
        self.allow.as_ref().is_none_or(|allow| {
            allow.contains(tool_name)
                || allow.contains(&format!("{COMMAND_TOOL_PREFIX}{}.*", sanitize(host_id)))
        })
    }

    /// Exposed commands keyed by tool name. Names that collide after sanitizing are
    /// dropped rather than silently routed to the wrong command.
    async fn exposed(&self) -> Vec<(String, CommandInfo)> {
        let mut by_name: std::collections::BTreeMap<String, Vec<CommandInfo>> = Default::default();
        for info in self.runtime.commands().list().await {
            let name = command_tool_name(&info.host_id, &info.descriptor.command_id);
            if name.len() <= MAX_TOOL_NAME && self.allows(&name, &info.host_id) {
                by_name.entry(name).or_default().push(info);
            }
        }
        by_name
            .into_iter()
            .filter_map(|(name, mut infos)| {
                if infos.len() == 1 {
                    Some((name, infos.remove(0)))
                } else {
                    eprintln!("MCP command tool {name} is ambiguous and was not exposed");
                    None
                }
            })
            .collect()
    }
}

/// `cmd.<host>.<command>`; see [`ligamina_core::commands::tool_name_part`].
pub fn command_tool_name(host_id: &str, command_id: &str) -> String {
    ligamina_core::commands::command_tool_name(host_id, command_id)
}

fn sanitize(value: &str) -> String {
    ligamina_core::commands::tool_name_part(value)
}

fn command_tool(name: &str, info: &CommandInfo) -> Tool {
    let descriptor = &info.descriptor;
    let mut description = format!(
        "Run the {} command `{}` from Host {} in a private virtual conversation and return the plugin's replies.",
        descriptor.framework, descriptor.command_id, info.host_id
    );
    if let Some(text) = &descriptor.description {
        description.push_str(&format!(" Plugin description: {text}."));
    }
    if let Some(usage) = &descriptor.usage {
        description.push_str(&format!(" Usage: {usage}."));
    }
    let input_schema = ligamina_core::command_args::input_schema(descriptor);
    let Value::Object(input_schema) = input_schema else {
        unreachable!("schema literal is an object")
    };
    Tool::new(
        Cow::Owned(name.to_owned()),
        Cow::Owned(description),
        Arc::new(input_schema),
    )
}

#[derive(Clone)]
pub struct LigaminaMcp {
    broker: CapabilityBroker,
    allowed_capabilities: Option<Arc<HashSet<String>>>,
    command_tools: Option<CommandTools>,
}

impl LigaminaMcp {
    pub fn new(broker: CapabilityBroker) -> Self {
        Self {
            broker,
            allowed_capabilities: None,
            command_tools: None,
        }
    }

    /// Also exposes native plugin commands as `cmd.*` tools.
    pub fn with_command_tools(mut self, command_tools: CommandTools) -> Self {
        self.command_tools = Some(command_tools);
        self
    }

    pub fn with_allowed_capabilities(
        broker: CapabilityBroker,
        allowed: impl IntoIterator<Item = String>,
    ) -> Self {
        Self {
            broker,
            allowed_capabilities: Some(Arc::new(allowed.into_iter().collect())),
            command_tools: None,
        }
    }

    async fn list_tools_async(&self) -> Vec<Tool> {
        let mut tools = vec![Tool::new(
            PING_TOOL,
            "Check that the local Ligamina MCP endpoint is reachable",
            Arc::new(Map::from_iter([("type".to_owned(), json!("object"))])),
        )];
        for capability in self.broker.list().await {
            if self
                .allowed_capabilities
                .as_ref()
                .is_some_and(|allowed| !allowed.contains(&capability.descriptor.capability_id))
            {
                continue;
            }
            let Value::Object(input_schema) = capability.descriptor.input_schema else {
                continue;
            };
            let mut tool = Tool::new(
                Cow::Owned(capability.descriptor.capability_id.clone()),
                Cow::Owned(format!(
                    "Invoke pure capability {} on active Host {}",
                    capability.descriptor.capability_id, capability.host_id
                )),
                Arc::new(input_schema),
            );
            let output_schema = json!({
                "type": "object",
                "properties": { "result": capability.descriptor.output_schema },
                "required": ["result"],
                "additionalProperties": false,
            });
            if let Value::Object(output_schema) = output_schema {
                tool = tool.with_raw_output_schema(Arc::new(output_schema));
            }
            tools.push(tool);
        }
        if let Some(command_tools) = &self.command_tools {
            let taken = tools
                .iter()
                .map(|tool| tool.name.to_string())
                .collect::<HashSet<_>>();
            for (name, info) in command_tools.exposed().await {
                if taken.contains(&name) {
                    eprintln!(
                        "MCP command tool {name} collides with a capability and was not exposed"
                    );
                    continue;
                }
                tools.push(command_tool(&name, &info));
            }
        }
        tools.sort_by(|left, right| left.name.cmp(&right.name));
        tools
    }
}

impl ServerHandler for LigaminaMcp {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(rmcp::model::Implementation::new("ligamina", "0.1.0"))
            .with_instructions("Ligamina local MCP endpoint")
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        Cow::Borrowed(&[ProtocolVersion::V_2025_11_25])
    }

    async fn list_tools(
        &self,
        _request: Option<rmcp::model::PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult {
            tools: self.list_tools_async().await,
            ..Default::default()
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        if request.name == PING_TOOL {
            return Ok(CallToolResult::success(vec![ContentBlock::text("pong")]).into());
        }
        if let Some(command_tools) = &self.command_tools
            && request.name.starts_with(COMMAND_TOOL_PREFIX)
            && self
                .broker
                .list()
                .await
                .iter()
                .all(|capability| capability.descriptor.capability_id != request.name.as_ref())
        {
            return call_command_tool(command_tools, &request).await;
        }
        if self
            .allowed_capabilities
            .as_ref()
            .is_some_and(|allowed| !allowed.contains(request.name.as_ref()))
        {
            return Err(McpError::invalid_params(
                format!("capability is not granted: {}", request.name),
                None,
            ));
        }
        let arguments = Value::Object(request.arguments.unwrap_or_default());
        let trace_id = format!("mcp:{:?}", context.id);
        match self
            .broker
            .invoke_root_cancellable(
                request.name.as_ref(),
                arguments,
                trace_id,
                TOOL_DEADLINE,
                context.ct,
            )
            .await
        {
            Ok(value) => Ok(CallToolResult::structured(json!({"result": value})).into()),
            Err(BrokerError::InvalidArguments { reason, .. }) => {
                Err(McpError::invalid_params(reason, None))
            }
            Err(BrokerError::CapabilityUnavailable(name)) => Err(McpError::invalid_params(
                format!("unknown or inactive capability: {name}"),
                None,
            )),
            Err(BrokerError::Cancelled) => Err(McpError::internal_error(
                "capability invocation was cancelled",
                None,
            )),
            Err(error) => {
                Ok(CallToolResult::error(vec![ContentBlock::text(error.to_string())]).into())
            }
        }
    }

    fn on_initialized(
        &self,
        context: NotificationContext<RoleServer>,
    ) -> impl std::future::Future<Output = ()> + MaybeSendFuture + '_ {
        let mut changes = self.broker.subscribe_changes();
        let mut command_changes = self
            .command_tools
            .as_ref()
            .map(|tools| tools.runtime.commands().subscribe_changes());
        let peer = context.peer.clone();
        async move {
            tokio::spawn(async move {
                loop {
                    let changed = match command_changes.as_mut() {
                        Some(command_changes) => tokio::select! {
                            change = changes.recv() => change,
                            change = command_changes.recv() => change,
                        },
                        None => changes.recv().await,
                    };
                    match changed {
                        Ok(()) | Err(broadcast::error::RecvError::Lagged(_)) => {
                            if peer.notify_tool_list_changed().await.is_err() {
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Closed) => break,
                    }
                }
            });
        }
    }
}

async fn call_command_tool(
    command_tools: &CommandTools,
    request: &CallToolRequestParams,
) -> Result<CallToolResponse, McpError> {
    let Some((_, info)) = command_tools
        .exposed()
        .await
        .into_iter()
        .find(|(name, _)| name == request.name.as_ref())
    else {
        return Err(McpError::invalid_params(
            format!("unknown or inactive command tool: {}", request.name),
            None,
        ));
    };
    let arguments = request.arguments.clone().unwrap_or_default();
    let text = ligamina_core::command_args::compose(&info.descriptor, &arguments)
        .map_err(|error| McpError::invalid_params(error, None))?;
    match command_tools
        .runtime
        .invoke_command(&info.host_id, &info.descriptor.command_id, &text)
        .await
    {
        Ok(outcome) => {
            let structured = outcome.to_json();
            let reply = outcome.text();
            let reply = if reply.is_empty() {
                "(the command sent no reply)".to_owned()
            } else {
                reply
            };
            // The text content is only the plugin's reply. Bots that forward tool text
            // verbatim must not post a JSON dump into the chat; clients that want the
            // details read `structuredContent`.
            let mut result = CallToolResult::structured(structured);
            result.content = vec![ContentBlock::text(reply)];
            Ok(result.into())
        }
        Err(error @ CommandError::InvalidArguments(_)) => {
            Err(McpError::invalid_params(error.to_string(), None))
        }
        Err(error) => Ok(CallToolResult::error(vec![ContentBlock::text(error.to_string())]).into()),
    }
}
