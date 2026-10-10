//! MCP 2025-11-25 adapter over the generation-scoped capability Broker.

use std::{borrow::Cow, collections::HashSet, sync::Arc, time::Duration};

use ligamina_core::broker::{BrokerError, CapabilityBroker};
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

#[derive(Clone)]
pub struct LigaminaMcp {
    broker: CapabilityBroker,
    allowed_capabilities: Option<Arc<HashSet<String>>>,
}

impl LigaminaMcp {
    pub fn new(broker: CapabilityBroker) -> Self {
        Self {
            broker,
            allowed_capabilities: None,
        }
    }

    pub fn with_allowed_capabilities(
        broker: CapabilityBroker,
        allowed: impl IntoIterator<Item = String>,
    ) -> Self {
        Self {
            broker,
            allowed_capabilities: Some(Arc::new(allowed.into_iter().collect())),
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
        let peer = context.peer.clone();
        async move {
            tokio::spawn(async move {
                loop {
                    match changes.recv().await {
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
