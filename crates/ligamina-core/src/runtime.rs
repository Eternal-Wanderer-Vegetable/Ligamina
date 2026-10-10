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

#[derive(Clone)]
pub struct CoreRuntime {
    store: Arc<Store>,
    platform: PlatformGateway,
    supervisor: HostSupervisor,
    broker: CapabilityBroker,
    router: EventRouter,
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
        }
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

fn remote_error(code: i64, message: impl Into<String>) -> RpcError {
    RpcError::Remote {
        code,
        message: message.into(),
    }
}
