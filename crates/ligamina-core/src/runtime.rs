//! Process-wide Core wiring for local Hosts, capabilities, events, and gateways.

use std::{collections::HashMap, path::PathBuf, sync::Arc, time::Duration};

use serde_json::{Value, json};
use thiserror::Error;
use tokio::sync::{Mutex, RwLock};
use tokio_util::sync::CancellationToken;

use crate::{
    broker::{
        BrokerError, CapabilityBroker, CapabilityDescriptor, CapabilityInfo, InvocationContext,
    },
    commands::{
        CommandCatalog, CommandInfo, ToolScope, ToolSessions, ToolTranscript, is_tool_account,
        tool_identity, transcript_text,
    },
    platform::PlatformGateway,
    policy::{Grant, Principal},
    router::{EventRouter, RouterError, RouterLimits},
    store::{
        EventDisposition, EventKey, EventRecord, EventStatus, GatewayRegistration, OperationRecord,
        OperationStatus, Store, StoreError,
    },
    supervisor::{HostConfig, HostState, HostStatus, HostSupervisor, SupervisorError},
    transport::RpcError,
};

const HOST_REQUEST_DEADLINE: Duration = Duration::from_secs(30);
const OPERATION_WAIT_DEADLINE: Duration = Duration::from_secs(60);
const GENERATION_POLL_INTERVAL: Duration = Duration::from_millis(100);
const MAX_COMMAND_ARGUMENTS_BYTES: usize = 4096;

#[derive(Clone)]
pub struct ManagedHost {
    pub config: HostConfig,
    pub operation_grants: Vec<Grant>,
}

#[derive(Clone)]
pub struct ManagedGateway {
    pub registration: GatewayRegistration,
    pub registration_token: String,
}

pub struct CoreRuntimeConfig {
    pub database_path: PathBuf,
    pub hosts: Vec<ManagedHost>,
    pub gateways: Vec<ManagedGateway>,
    pub client_grants: Vec<Grant>,
    pub router_limits: RouterLimits,
}

#[derive(Debug, Error)]
pub enum CoreRuntimeError {
    #[error("Core storage initialization failed: {0}")]
    Store(#[from] StoreError),
    #[error("Host supervision failed: {0}")]
    Supervisor(#[from] SupervisorError),
    #[error("event router initialization failed: {0}")]
    Router(#[from] RouterError),
    #[error("capability Broker initialization failed: {0}")]
    Broker(#[from] BrokerError),
    #[error("invalid runtime configuration: {0}")]
    Invalid(String),
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CommandError {
    #[error("unknown or inactive command {command_id} on Host {host_id}")]
    UnknownCommand { host_id: String, command_id: String },
    #[error("command arguments are invalid: {0}")]
    InvalidArguments(String),
    #[error("tool sessions are not yet supported for {0} Hosts")]
    UnsupportedFramework(String),
    #[error("Host {0} is unavailable")]
    HostUnavailable(String),
    #[error("Host {0} restarted while the command was running")]
    StaleEpoch(String),
    #[error("command did not finish within {0:?}")]
    TimedOut(Duration),
    #[error("command dispatch failed: {0}")]
    Dispatch(String),
}

/// Result of running a native command in a virtual tool conversation.
#[derive(Debug, Clone, PartialEq)]
pub struct CommandOutcome {
    pub host_id: String,
    pub command_id: String,
    pub trace_id: String,
    pub transcript: ToolTranscript,
}

impl CommandOutcome {
    pub fn text(&self) -> String {
        transcript_text(&self.transcript)
    }

    pub fn to_json(&self) -> Value {
        let messages = self
            .transcript
            .messages
            .iter()
            .map(|segments| {
                let single = ToolTranscript {
                    messages: vec![segments.clone()],
                    refused: Vec::new(),
                };
                json!({ "segments": segments, "text": transcript_text(&single) })
            })
            .collect::<Vec<_>>();
        json!({
            "host_id": self.host_id,
            "command_id": self.command_id,
            "trace_id": self.trace_id,
            "dispatch": "completed",
            "messages": messages,
            "refused_operations": self.transcript.refused,
            "note": "Only replies sent while the Host handled the command are collected; later timer or callback sends are not included.",
        })
    }
}

#[derive(Clone)]
pub struct CoreRuntime {
    store: Arc<Store>,
    platform: PlatformGateway,
    supervisor: HostSupervisor,
    broker: CapabilityBroker,
    router: EventRouter,
    commands: CommandCatalog,
    runtime_inventories: Arc<RwLock<HashMap<String, (u64, Value)>>>,
    tool_sessions: ToolSessions,
    active_generations: Arc<Mutex<HashMap<String, u64>>>,
    lifecycle_stop: CancellationToken,
}

impl CoreRuntime {
    pub async fn start(config: CoreRuntimeConfig) -> Result<Self, CoreRuntimeError> {
        if config.hosts.is_empty() {
            return Err(CoreRuntimeError::Invalid(
                "at least one managed Host is required".to_owned(),
            ));
        }
        let mut host_ids = Vec::with_capacity(config.hosts.len());
        let mut host_grants = HashMap::new();
        let mut grants = Vec::new();
        if config
            .client_grants
            .iter()
            .chain(config.hosts.iter().flat_map(|host| &host.operation_grants))
            .any(|grant| is_tool_account(&grant.account_id))
            || config
                .gateways
                .iter()
                .any(|gateway| is_tool_account(&gateway.registration.account_id))
        {
            return Err(CoreRuntimeError::Invalid(format!(
                "accounts {} and {} are reserved for command tool sessions",
                crate::commands::TOOL_ACCOUNT_ID,
                crate::commands::TOOL_ONEBOT_ACCOUNT_ID
            )));
        }
        for host in &config.hosts {
            host_ids.push(host.config.id.clone());
            for grant in &host.operation_grants {
                if grant.principal != Principal::Host(host.config.id.clone()) {
                    return Err(CoreRuntimeError::Invalid(format!(
                        "operation grant {} is not bound to Host {}",
                        grant.id, host.config.id
                    )));
                }
                host_grants.insert(
                    (host.config.id.clone(), grant.account_id.clone()),
                    grant.id.clone(),
                );
                grants.push(grant.clone());
            }
        }
        if host_ids.iter().any(|host_id| host_id.trim().is_empty()) {
            return Err(CoreRuntimeError::Invalid(
                "Host IDs must not be empty".to_owned(),
            ));
        }
        if host_ids
            .iter()
            .collect::<std::collections::HashSet<_>>()
            .len()
            != host_ids.len()
        {
            return Err(CoreRuntimeError::Invalid(
                "Host IDs must be unique".to_owned(),
            ));
        }

        let store = Arc::new(Store::open(&config.database_path)?);
        store.schema_version().await?;
        let platform = PlatformGateway::new(store.clone());
        let now_ms = PlatformGateway::current_time_ms();
        for grant in grants {
            store.put_grant(grant).await?;
        }
        for grant in config.client_grants {
            store.put_grant(grant).await?;
        }
        for gateway in &config.gateways {
            platform
                .register_gateway(
                    gateway.registration.clone(),
                    gateway.registration_token.clone(),
                    now_ms,
                )
                .await?;
        }

        let supervisor_slot = Arc::new(RwLock::new(None::<HostSupervisor>));
        let broker_slot = Arc::new(RwLock::new(None::<CapabilityBroker>));
        let supervisor_for_broker = supervisor_slot.clone();
        let broker = CapabilityBroker::new(
            move |host_id, epoch, capability_id, arguments, context: InvocationContext| {
                let supervisor_slot = supervisor_for_broker.clone();
                async move {
                    let supervisor = supervisor_slot
                        .read()
                        .await
                        .clone()
                        .ok_or_else(|| BrokerError::HostUnavailable(host_id.clone()))?;
                    let handle = supervisor
                        .handle(&host_id)
                        .await
                        .map_err(|_| BrokerError::HostUnavailable(host_id.clone()))?;
                    if handle.epoch() != epoch {
                        return Err(BrokerError::StaleEpoch { host_id, epoch });
                    }
                    handle
                        .request(
                            "capability.invoke",
                            json!({
                                "capability_id": capability_id,
                                "arguments": arguments,
                                "context": context,
                            }),
                            HOST_REQUEST_DEADLINE,
                        )
                        .await
                        .map_err(|error| BrokerError::Remote(error.to_string()))
                }
            },
        );

        let tool_sessions = ToolSessions::new();
        let tool_sessions_for_hosts = tool_sessions.clone();
        let broker_for_hosts = broker_slot.clone();
        let store_for_hosts = store.clone();
        let platform_for_hosts = platform.clone();
        let host_grants = Arc::new(host_grants);
        let grants_for_hosts = host_grants.clone();
        let supervisor =
            HostSupervisor::with_handler(move |host_id, epoch, _session, method, params| {
                let broker_slot = broker_for_hosts.clone();
                let store = store_for_hosts.clone();
                let platform = platform_for_hosts.clone();
                let host_grants = grants_for_hosts.clone();
                let tool_sessions = tool_sessions_for_hosts.clone();
                async move {
                    match method.as_str() {
                        "service.invoke" => {
                            let broker = broker_slot
                                .read()
                                .await
                                .clone()
                                .ok_or_else(|| remote_error(-32603, "Broker is unavailable"))?;
                            let capability_id = required_string(&params, "capability_id")
                                .map_err(|error| remote_error(-32602, error))?;
                            let trace_id = params
                                .get("trace_id")
                                .and_then(Value::as_str)
                                .unwrap_or("host-service-call")
                                .to_owned();
                            let arguments = params
                                .get("arguments")
                                .cloned()
                                .unwrap_or_else(|| json!({}));
                            broker
                                .invoke_from_host(
                                    &host_id,
                                    epoch,
                                    &capability_id,
                                    arguments,
                                    InvocationContext::from_host(&host_id, epoch, trace_id),
                                    HOST_REQUEST_DEADLINE,
                                )
                                .await
                                .map_err(|error| remote_error(-32000, error.to_string()))
                        }
                        "platform.execute" => {
                            let operation = params;
                            if let Some(result) =
                                tool_sessions.intercept(&host_id, epoch, &operation)
                            {
                                return Ok(result);
                            }
                            let account_id = required_string(&operation, "account_id")
                                .map_err(|error| remote_error(-32602, error))?;
                            let grant_id = host_grants
                                .get(&(host_id.clone(), account_id.clone()))
                                .cloned()
                                .ok_or_else(|| {
                                    remote_error(-32003, "Host has no grant for this account")
                                })?;
                            let (_disposition, record) = platform
                                .submit(crate::store::OperationSubmission {
                                    grant_id: grant_id.clone(),
                                    principal: Principal::Host(host_id.clone()),
                                    host_epoch: epoch,
                                    operation: operation.clone(),
                                    now_ms: PlatformGateway::current_time_ms(),
                                })
                                .await
                                .map_err(|error| remote_error(-32003, error.to_string()))?;
                            wait_for_operation(
                                &store,
                                record,
                                grant_id,
                                Principal::Host(host_id.clone()),
                            )
                            .await
                            .map_err(|error| remote_error(-32000, error))
                        }
                        _ => Err(remote_error(
                            -32601,
                            format!("Core method not found: {method}"),
                        )),
                    }
                }
            });
        *broker_slot.write().await = Some(broker.clone());
        *supervisor_slot.write().await = Some(supervisor.clone());

        let processor_supervisor = supervisor.clone();
        let router = EventRouter::new(
            store.clone(),
            host_ids,
            config.router_limits,
            move |host_id, event| {
                let supervisor = processor_supervisor.clone();
                async move {
                    let handle = supervisor
                        .handle(&host_id)
                        .await
                        .map_err(|error| error.to_string())?;
                    let result = handle
                        .request(
                            "event.dispatch",
                            json!({ "event": event }),
                            HOST_REQUEST_DEADLINE,
                        )
                        .await
                        .map_err(|error| error.to_string())?;
                    if result.get("accepted") != Some(&Value::Bool(true)) {
                        return Err(format!("Host did not accept event: {result}"));
                    }
                    Ok(())
                }
            },
        )?;

        let runtime = Self {
            store,
            platform,
            supervisor,
            broker,
            router,
            commands: CommandCatalog::new(),
            runtime_inventories: Arc::new(RwLock::new(HashMap::new())),
            tool_sessions,
            active_generations: Arc::new(Mutex::new(HashMap::new())),
            lifecycle_stop: CancellationToken::new(),
        };
        let desired = config.hosts.into_iter().map(|host| host.config).collect();
        runtime.supervisor.apply_desired_set(desired).await?;
        runtime.sync_host_generations().await?;
        runtime.start_generation_monitor();
        Ok(runtime)
    }

    pub fn store(&self) -> &Arc<Store> {
        &self.store
    }

    pub fn platform(&self) -> &PlatformGateway {
        &self.platform
    }

    pub fn broker(&self) -> &CapabilityBroker {
        &self.broker
    }

    pub fn commands(&self) -> &CommandCatalog {
        &self.commands
    }

    /// Returns the latest optional runtime inventory from each active Host.
    /// Inventories remain separate from MCP command descriptors and are scoped
    /// to the Host generation that reported them.
    pub async fn runtime_inventories(&self) -> Vec<Value> {
        let mut inventories = self
            .runtime_inventories
            .read()
            .await
            .iter()
            .map(|(host_id, (epoch, inventory))| {
                json!({ "host_id": host_id, "host_epoch": epoch, "inventory": inventory })
            })
            .collect::<Vec<_>>();
        inventories.sort_by(|left, right| {
            left.get("host_id")
                .and_then(Value::as_str)
                .cmp(&right.get("host_id").and_then(Value::as_str))
        });
        inventories
    }

    /// Tool-session operations refused after their command had already returned.
    pub fn late_tool_refusals(&self) -> u64 {
        self.tool_sessions.closed_refusals()
    }

    /// Runs a native plugin command in a fresh virtual private conversation and
    /// returns the replies the plugin sent while handling it (ADR 0002).
    pub async fn invoke_command(
        &self,
        host_id: &str,
        command_id: &str,
        arguments: &str,
    ) -> Result<CommandOutcome, CommandError> {
        self.invoke_command_with_deadline(host_id, command_id, arguments, HOST_REQUEST_DEADLINE)
            .await
    }

    /// [`Self::invoke_command`] with an explicit Host deadline. Replies sent after the
    /// deadline reach a closed tool session and are refused, never delivered.
    pub async fn invoke_command_with_deadline(
        &self,
        host_id: &str,
        command_id: &str,
        arguments: &str,
        deadline: Duration,
    ) -> Result<CommandOutcome, CommandError> {
        if arguments.len() > MAX_COMMAND_ARGUMENTS_BYTES {
            return Err(CommandError::InvalidArguments(format!(
                "arguments exceed {MAX_COMMAND_ARGUMENTS_BYTES} bytes"
            )));
        }
        let info: CommandInfo = self
            .commands
            .get(host_id, command_id)
            .await
            .ok_or_else(|| CommandError::UnknownCommand {
                host_id: host_id.to_owned(),
                command_id: command_id.to_owned(),
            })?;
        let call = uuid::Uuid::new_v4();
        let identity = tool_identity(&info.descriptor.framework, call)
            .ok_or_else(|| CommandError::UnsupportedFramework(info.descriptor.framework.clone()))?;
        let handle = self
            .supervisor
            .handle(host_id)
            .await
            .map_err(|_| CommandError::HostUnavailable(host_id.to_owned()))?;
        if handle.epoch() != info.host_epoch {
            return Err(CommandError::StaleEpoch(host_id.to_owned()));
        }

        let call_id = call.simple().to_string();
        let trace_id = format!("tool-{call_id}");
        let actor_id = identity.actor_id.clone();
        let text = if arguments.trim().is_empty() {
            info.descriptor.invocation.clone()
        } else {
            format!("{} {}", info.descriptor.invocation, arguments.trim())
        };
        let mut extensions = serde_json::Map::new();
        extensions.insert(
            identity.platform.to_owned(),
            json!({ "message_id": identity.message_id }),
        );
        let event = json!({
            "schema_version": ligamina_contracts::PROTOCOL_VERSION,
            "event_id": format!("tool-event-{call_id}"),
            "trace_id": trace_id,
            "platform": identity.platform,
            "account_id": identity.account_id,
            "conversation": { "kind": "private", "id": actor_id },
            "actor_id": actor_id,
            "timestamp": current_timestamp(),
            "target_host": host_id,
            "message": [{ "type": "text", "text": text }],
            "extensions": extensions,
        });
        ligamina_contracts::validate_event(&event)
            .map_err(|error| CommandError::InvalidArguments(error.to_string()))?;

        self.tool_sessions.open(ToolScope {
            host_id: host_id.to_owned(),
            host_epoch: info.host_epoch,
            trace_id: trace_id.clone(),
            account_id: identity.account_id.to_owned(),
            conversation_kind: "private".to_owned(),
            conversation_id: actor_id,
        });
        let result = handle
            .request("event.dispatch", json!({ "event": event }), deadline)
            .await;
        let transcript = self.tool_sessions.close(&trace_id);
        let result = result.map_err(|error| match error {
            // HostHandle flattens RpcError::DeadlineExceeded into its display text.
            SupervisorError::Rpc(message) if message.ends_with("exceeded its deadline") => {
                CommandError::TimedOut(deadline)
            }
            other => CommandError::Dispatch(other.to_string()),
        })?;
        if result.get("accepted") != Some(&Value::Bool(true)) {
            return Err(CommandError::Dispatch(format!(
                "Host did not accept the command event: {result}"
            )));
        }
        Ok(CommandOutcome {
            host_id: host_id.to_owned(),
            command_id: command_id.to_owned(),
            trace_id,
            transcript,
        })
    }

    pub async fn host_status(&self) -> Vec<HostStatus> {
        self.supervisor.status().await
    }

    pub async fn capabilities(&self) -> Vec<CapabilityInfo> {
        self.broker.list().await
    }

    pub async fn invoke_capability(
        &self,
        capability_id: &str,
        arguments: Value,
        trace_id: impl Into<String>,
    ) -> Result<Value, BrokerError> {
        self.broker
            .invoke_root(capability_id, arguments, trace_id, HOST_REQUEST_DEADLINE)
            .await
    }

    pub async fn dispatch_event(
        &self,
        client_id: String,
        event: Value,
    ) -> Result<(EventDisposition, EventRecord), RouterError> {
        self.router
            .dispatch(client_id, event, PlatformGateway::current_time_ms())
            .await
    }

    pub async fn event_status(&self, key: EventKey) -> Result<EventRecord, StoreError> {
        self.store.get_event(key).await
    }

    pub async fn apply_desired_hosts(
        &self,
        desired: Vec<HostConfig>,
    ) -> Result<(), SupervisorError> {
        self.supervisor.apply_desired_set(desired).await?;
        self.sync_host_generations()
            .await
            .map_err(|error| SupervisorError::Rpc(error.to_string()))
    }

    pub async fn shutdown(&self) {
        self.lifecycle_stop.cancel();
        self.supervisor.shutdown().await;
        let active = self
            .active_generations
            .lock()
            .await
            .drain()
            .collect::<Vec<_>>();
        for (host_id, epoch) in active {
            self.broker.deactivate_host(&host_id, epoch).await;
            self.commands.deactivate_host(&host_id, epoch).await;
        }
        self.runtime_inventories.write().await.clear();
    }

    async fn sync_host_generations(&self) -> Result<(), CoreRuntimeError> {
        let statuses = self.supervisor.status().await;
        let active_ids = statuses
            .iter()
            .filter(|status| status.state == HostState::Active)
            .map(|status| status.id.clone())
            .collect::<std::collections::HashSet<_>>();
        {
            let mut generations = self.active_generations.lock().await;
            let inactive = generations
                .iter()
                .filter(|(host_id, _)| !active_ids.contains(*host_id))
                .map(|(host_id, epoch)| (host_id.clone(), *epoch))
                .collect::<Vec<_>>();
            for (host_id, epoch) in inactive {
                generations.remove(&host_id);
                self.broker.deactivate_host(&host_id, epoch).await;
                self.commands.deactivate_host(&host_id, epoch).await;
                self.runtime_inventories.write().await.remove(&host_id);
            }
        }

        for status in statuses
            .into_iter()
            .filter(|status| status.state == HostState::Active)
        {
            let current = self
                .active_generations
                .lock()
                .await
                .get(&status.id)
                .copied();
            if current == Some(status.epoch) {
                continue;
            }
            if let Some(old_epoch) = current {
                self.broker.deactivate_host(&status.id, old_epoch).await;
                self.commands.deactivate_host(&status.id, old_epoch).await;
                self.runtime_inventories.write().await.remove(&status.id);
            }
            self.broker.activate_host(&status.id, status.epoch).await?;
            let handle = self.supervisor.handle(&status.id).await?;
            let result = handle
                .request("runtime.capabilities", Value::Null, Duration::from_secs(5))
                .await
                .map_err(|error| SupervisorError::Rpc(error.to_string()))?;
            let descriptors = serde_json::from_value::<Vec<CapabilityDescriptor>>(
                result
                    .get("capabilities")
                    .cloned()
                    .unwrap_or_else(|| json!([])),
            )
            .map_err(|error| {
                CoreRuntimeError::Invalid(format!(
                    "Host {} returned an invalid capability snapshot: {error}",
                    status.id
                ))
            })?;
            self.broker
                .register_snapshot(&status.id, status.epoch, descriptors)
                .await?;
            // Hosts before contract v0.2 omit `commands`; they simply expose none.
            let commands = result
                .get("commands")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for rejected in self
                .commands
                .register_snapshot(&status.id, status.epoch, &commands)
                .await
            {
                eprintln!("Host {} command omitted: {rejected}", status.id);
            }
            if let Some(inventory) = result
                .get("inventory")
                .filter(|value| is_runtime_inventory(value))
            {
                self.runtime_inventories
                    .write()
                    .await
                    .insert(status.id.clone(), (status.epoch, inventory.clone()));
            } else {
                self.runtime_inventories.write().await.remove(&status.id);
            }
            self.active_generations
                .lock()
                .await
                .insert(status.id, status.epoch);
        }
        Ok(())
    }

    fn start_generation_monitor(&self) {
        let runtime = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = runtime.lifecycle_stop.cancelled() => break,
                    _ = tokio::time::sleep(GENERATION_POLL_INTERVAL) => {
                        if let Err(error) = runtime.sync_host_generations().await {
                            eprintln!("Host capability synchronization failed: {error}");
                        }
                    }
                }
            }
        });
    }
}

fn is_runtime_inventory(value: &Value) -> bool {
    value.get("schema_version").and_then(Value::as_u64) == Some(1)
        && value
            .get("source")
            .and_then(|source| source.get("kind"))
            .and_then(Value::as_str)
            == Some("runtime")
        && value
            .get("source")
            .and_then(|source| source.get("confidence"))
            .and_then(Value::as_str)
            == Some("observed")
        && value
            .get("plugin")
            .and_then(|plugin| plugin.get("package"))
            .and_then(Value::as_str)
            .is_some_and(|package| !package.trim().is_empty())
        && [
            "commands",
            "handlers",
            "services",
            "schedulers",
            "configuration",
        ]
        .iter()
        .all(|key| {
            value
                .get("capabilities")
                .and_then(|capabilities| capabilities.get(key))
                .and_then(Value::as_object)
                .is_some()
        })
}

#[cfg(test)]
mod inventory_tests {
    use super::is_runtime_inventory;
    use serde_json::json;

    #[test]
    fn accepts_only_observed_runtime_inventory_shapes() {
        let inventory = json!({
            "schema_version": 1,
            "plugin": { "package": "fixture" },
            "source": { "kind": "runtime", "confidence": "observed" },
            "capabilities": {
                "commands": {}, "handlers": {}, "services": {},
                "schedulers": {}, "configuration": {}
            }
        });
        assert!(is_runtime_inventory(&inventory));

        let mut static_record = inventory.clone();
        static_record["source"]["kind"] = json!("static");
        assert!(!is_runtime_inventory(&static_record));

        let mut incomplete = inventory;
        incomplete["capabilities"]
            .as_object_mut()
            .unwrap()
            .remove("handlers");
        assert!(!is_runtime_inventory(&incomplete));
    }
}

async fn wait_for_operation(
    store: &Store,
    initial: OperationRecord,
    grant_id: String,
    principal: Principal,
) -> Result<Value, String> {
    let key = initial.key.clone();
    let until = tokio::time::Instant::now() + OPERATION_WAIT_DEADLINE;
    loop {
        let record = store
            .get_operation(
                key.clone(),
                grant_id.clone(),
                principal.clone(),
                PlatformGateway::current_time_ms(),
            )
            .await
            .map_err(|error| error.to_string())?;
        match record.status {
            OperationStatus::Succeeded | OperationStatus::Failed | OperationStatus::Unknown => {
                return Ok(record.result.unwrap_or_else(|| {
                    json!({
                        "status": operation_status_name(record.status),
                        "error": "operation finished without a result receipt",
                    })
                }));
            }
            OperationStatus::Pending | OperationStatus::Dispatched => {}
        }
        if tokio::time::Instant::now() >= until {
            return Ok(json!({
                "status": "unknown",
                "error": "operation receipt was not returned before the Core deadline",
            }));
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

pub fn event_record_json(record: &EventRecord) -> Value {
    json!({
        "key": {
            "client_id": record.key.client_id,
            "platform": record.key.platform,
            "account_id": record.key.account_id,
            "conversation_kind": record.key.conversation_kind,
            "conversation_id": record.key.conversation_id,
            "event_id": record.key.event_id,
        },
        "fingerprint": record.fingerprint,
        "trace_id": record.trace_id,
        "target_host": record.target_host,
        "event": record.event,
        "status": event_status_name(record.status),
        "error": record.error,
        "created_at_ms": record.created_at_ms,
        "updated_at_ms": record.updated_at_ms,
    })
}

pub fn operation_record_json(record: &OperationRecord) -> Value {
    json!({
        "key": {
            "client_id": record.key.client_id,
            "account_id": record.key.account_id,
            "operation": record.key.operation,
            "operation_id": record.key.operation_id,
        },
        "fingerprint": record.fingerprint,
        "trace_id": record.trace_id,
        "grant_id": record.grant_id,
        "principal": record.principal,
        "host_epoch": record.host_epoch,
        "core_epoch_created": record.core_epoch_created,
        "operation_payload": record.operation_payload,
        "status": operation_status_name(record.status),
        "created_at_ms": record.created_at_ms,
        "updated_at_ms": record.updated_at_ms,
        "dispatched_lease_id": record.dispatched_lease_id,
        "dispatched_lease_epoch": record.dispatched_lease_epoch,
        "dispatched_core_epoch": record.dispatched_core_epoch,
        "result": record.result,
    })
}

pub fn event_key(client_id: String, event: &Value) -> Result<EventKey, String> {
    let conversation = event
        .get("conversation")
        .ok_or_else(|| "event conversation is required".to_owned())?;
    Ok(EventKey {
        client_id,
        platform: required_string(event, "platform")?,
        account_id: required_string(event, "account_id")?,
        conversation_kind: required_string(conversation, "kind")?,
        conversation_id: required_string(conversation, "id")?,
        event_id: required_string(event, "event_id")?,
    })
}

pub fn required_string(value: &Value, field: &str) -> Result<String, String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .ok_or_else(|| format!("{field} must be a non-empty string"))
}

fn event_status_name(status: EventStatus) -> &'static str {
    match status {
        EventStatus::Accepted => "accepted",
        EventStatus::Processing => "processing",
        EventStatus::Completed => "completed",
        EventStatus::Failed => "failed",
        EventStatus::Unknown => "unknown",
    }
}

fn operation_status_name(status: OperationStatus) -> &'static str {
    match status {
        OperationStatus::Pending => "pending",
        OperationStatus::Dispatched => "dispatched",
        OperationStatus::Succeeded => "succeeded",
        OperationStatus::Failed => "failed",
        OperationStatus::Unknown => "unknown",
    }
}

/// Current UTC time as an RFC 3339 timestamp with millisecond precision.
fn current_timestamp() -> String {
    let millis = PlatformGateway::current_time_ms();
    let secs = millis.div_euclid(1000);
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // civil_from_days (Howard Hinnant), proleptic Gregorian calendar.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60,
        millis.rem_euclid(1000)
    )
}

fn remote_error(code: i64, message: impl Into<String>) -> RpcError {
    RpcError::Remote {
        code,
        message: message.into(),
    }
}
