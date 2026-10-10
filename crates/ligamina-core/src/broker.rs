//! Generation-scoped routing for explicitly declared Host capabilities.

use std::{collections::HashMap, future::Future, pin::Pin, sync::Arc, time::Duration};

use jsonschema::Validator;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use tokio::sync::{RwLock, broadcast};
use tokio_util::sync::CancellationToken;

pub const MAX_CALL_DEPTH: usize = 8;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct CapabilityDescriptor {
    pub capability_id: String,
    pub input_schema: Value,
    pub output_schema: Value,
    pub pure: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CapabilityInfo {
    pub host_id: String,
    pub host_epoch: u64,
    pub descriptor: CapabilityDescriptor,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct InvocationFrame {
    pub host_id: String,
    pub host_epoch: u64,
    pub capability_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct InvocationContext {
    pub trace_id: String,
    pub call_chain: Vec<InvocationFrame>,
}

impl InvocationContext {
    pub fn from_host(
        host_id: impl Into<String>,
        host_epoch: u64,
        trace_id: impl Into<String>,
    ) -> Self {
        Self {
            trace_id: trace_id.into(),
            call_chain: vec![InvocationFrame {
                host_id: host_id.into(),
                host_epoch,
                capability_id: "$host".to_owned(),
            }],
        }
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum BrokerError {
    #[error("host {0} is not active")]
    HostUnavailable(String),
    #[error("host {host_id} epoch {epoch} is stale")]
    StaleEpoch { host_id: String, epoch: u64 },
    #[error("capability {0} is not registered")]
    CapabilityUnavailable(String),
    #[error("capability {0} is already registered")]
    DuplicateCapability(String),
    #[error("capability descriptor is invalid: {0}")]
    InvalidDescriptor(String),
    #[error("arguments do not match capability {capability_id}: {reason}")]
    InvalidArguments {
        capability_id: String,
        reason: String,
    },
    #[error("result does not match capability {capability_id}: {reason}")]
    InvalidResult {
        capability_id: String,
        reason: String,
    },
    #[error("invocation loop detected at {0}")]
    LoopDetected(String),
    #[error("invocation call chain exceeds {MAX_CALL_DEPTH} frames")]
    MaxCallDepth,
    #[error("capability {0} exceeded its deadline")]
    DeadlineExceeded(String),
    #[error("capability invocation was cancelled")]
    Cancelled,
    #[error("capability invocation failed: {0}")]
    Remote(String),
}

type InvokeFuture = Pin<Box<dyn Future<Output = Result<Value, BrokerError>> + Send>>;
type InvokeHandler =
    dyn Fn(String, u64, String, Value, InvocationContext) -> InvokeFuture + Send + Sync;

struct HostGeneration {
    epoch: u64,
    active: bool,
}

struct RegisteredCapability {
    host_id: String,
    host_epoch: u64,
    descriptor: CapabilityDescriptor,
    input_validator: Validator,
    output_validator: Validator,
}

#[derive(Default)]
struct BrokerState {
    hosts: HashMap<String, HostGeneration>,
    capabilities: HashMap<String, RegisteredCapability>,
}

/// Routes calls only to capabilities registered by the current active Host generation.
#[derive(Clone)]
pub struct CapabilityBroker {
    state: Arc<RwLock<BrokerState>>,
    invoke: Arc<InvokeHandler>,
    changes: broadcast::Sender<()>,
}

impl CapabilityBroker {
    pub fn new<F, Fut>(invoke: F) -> Self
    where
        F: Fn(String, u64, String, Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, BrokerError>> + Send + 'static,
    {
        let (changes, _) = broadcast::channel(16);
        Self {
            state: Arc::new(RwLock::new(BrokerState::default())),
            invoke: Arc::new(move |host_id, epoch, capability_id, arguments, context| {
                Box::pin(invoke(host_id, epoch, capability_id, arguments, context))
            }),
            changes,
        }
    }

    /// Replace the generation for a Host and discard descriptors from older generations.
    pub async fn activate_host(
        &self,
        host_id: impl Into<String>,
        epoch: u64,
    ) -> Result<(), BrokerError> {
        let host_id = host_id.into();
        let mut state = self.state.write().await;
        if let Some(current) = state.hosts.get(&host_id) {
            if epoch < current.epoch || (epoch == current.epoch && !current.active) {
                return Err(BrokerError::StaleEpoch { host_id, epoch });
            }
            if epoch == current.epoch {
                return Ok(());
            }
        }
        state
            .capabilities
            .retain(|_, capability| capability.host_id != host_id);
        state.hosts.insert(
            host_id,
            HostGeneration {
                epoch,
                active: true,
            },
        );
        self.notify_changed();
        Ok(())
    }

    /// Deactivate a Host only when the supplied generation is still current.
    pub async fn deactivate_host(&self, host_id: &str, epoch: u64) {
        let mut state = self.state.write().await;
        if let Some(generation) = state.hosts.get_mut(host_id) {
            if generation.epoch == epoch {
                generation.active = false;
                state
                    .capabilities
                    .retain(|_, capability| capability.host_id != host_id);
                drop(state);
                self.notify_changed();
            }
        }
    }

    pub fn subscribe_changes(&self) -> broadcast::Receiver<()> {
        self.changes.subscribe()
    }

    /// Return the active descriptor snapshot in stable capability-ID order.
    pub async fn list(&self) -> Vec<CapabilityInfo> {
        let state = self.state.read().await;
        let mut capabilities = state
            .capabilities
            .values()
            .filter_map(|capability| {
                state.hosts.get(&capability.host_id).and_then(|generation| {
                    (generation.active && generation.epoch == capability.host_epoch).then(|| {
                        CapabilityInfo {
                            host_id: capability.host_id.clone(),
                            host_epoch: capability.host_epoch,
                            descriptor: capability.descriptor.clone(),
                        }
                    })
                })
            })
            .collect::<Vec<_>>();
        capabilities.sort_by(|left, right| {
            left.descriptor
                .capability_id
                .cmp(&right.descriptor.capability_id)
        });
        capabilities
    }

    /// Bind a descriptor to the trusted Host identity supplied by Core.
    pub async fn register(
        &self,
        host_id: &str,
        epoch: u64,
        descriptor: CapabilityDescriptor,
    ) -> Result<(), BrokerError> {
        validate_descriptor(&descriptor)?;
        let input_validator = compile_schema(&descriptor.input_schema)?;
        let output_validator = compile_schema(&descriptor.output_schema)?;
        let mut state = self.state.write().await;
        let Some(generation) = state.hosts.get(host_id) else {
            return Err(BrokerError::HostUnavailable(host_id.to_owned()));
        };
        if !generation.active {
            return Err(BrokerError::HostUnavailable(host_id.to_owned()));
        }
        if generation.epoch != epoch {
            return Err(BrokerError::StaleEpoch {
                host_id: host_id.to_owned(),
                epoch,
            });
        }
        if state.capabilities.contains_key(&descriptor.capability_id) {
            return Err(BrokerError::DuplicateCapability(descriptor.capability_id));
        }
        state.capabilities.insert(
            descriptor.capability_id.clone(),
            RegisteredCapability {
                host_id: host_id.to_owned(),
                host_epoch: epoch,
                descriptor,
                input_validator,
                output_validator,
            },
        );
        drop(state);
        self.notify_changed();
        Ok(())
    }

    /// Atomically publish the capability snapshot returned by one Host handshake.
    pub async fn register_snapshot(
        &self,
        host_id: &str,
        epoch: u64,
        descriptors: Vec<CapabilityDescriptor>,
    ) -> Result<(), BrokerError> {
        let mut prepared = Vec::with_capacity(descriptors.len());
        let mut ids = std::collections::HashSet::with_capacity(descriptors.len());
        for descriptor in descriptors {
            validate_descriptor(&descriptor)?;
            if !ids.insert(descriptor.capability_id.clone()) {
                return Err(BrokerError::DuplicateCapability(descriptor.capability_id));
            }
            prepared.push((
                descriptor.clone(),
                compile_schema(&descriptor.input_schema)?,
                compile_schema(&descriptor.output_schema)?,
            ));
        }

        let mut state = self.state.write().await;
        let Some(generation) = state.hosts.get(host_id) else {
            return Err(BrokerError::HostUnavailable(host_id.to_owned()));
        };
        if !generation.active {
            return Err(BrokerError::HostUnavailable(host_id.to_owned()));
        }
        if generation.epoch != epoch {
            return Err(BrokerError::StaleEpoch {
                host_id: host_id.to_owned(),
                epoch,
            });
        }
        for (descriptor, _, _) in &prepared {
            if state
                .capabilities
                .get(&descriptor.capability_id)
                .is_some_and(|existing| existing.host_id != host_id)
            {
                return Err(BrokerError::DuplicateCapability(
                    descriptor.capability_id.clone(),
                ));
            }
        }
        state
            .capabilities
            .retain(|_, capability| capability.host_id != host_id);
        for (descriptor, input_validator, output_validator) in prepared {
            state.capabilities.insert(
                descriptor.capability_id.clone(),
                RegisteredCapability {
                    host_id: host_id.to_owned(),
                    host_epoch: epoch,
                    descriptor,
                    input_validator,
                    output_validator,
                },
            );
        }
        drop(state);
        self.notify_changed();
        Ok(())
    }

    pub async fn invoke_from_host(
        &self,
        caller_host: &str,
        caller_epoch: u64,
        capability_id: &str,
        arguments: Value,
        context: InvocationContext,
        deadline: Duration,
    ) -> Result<Value, BrokerError> {
        let Some(caller) = context.call_chain.last() else {
            return Err(BrokerError::InvalidDescriptor(
                "Host invocation has no trusted call frame".to_owned(),
            ));
        };
        if caller.host_id != caller_host || caller.host_epoch != caller_epoch {
            return Err(BrokerError::StaleEpoch {
                host_id: caller_host.to_owned(),
                epoch: caller_epoch,
            });
        }
        self.ensure_active(caller_host, caller_epoch).await?;
        self.invoke_inner(
            capability_id,
            arguments,
            context,
            deadline,
            CancellationToken::new(),
        )
        .await
    }

    pub async fn invoke_root(
        &self,
        capability_id: &str,
        arguments: Value,
        trace_id: impl Into<String>,
        deadline: Duration,
    ) -> Result<Value, BrokerError> {
        self.invoke_root_cancellable(
            capability_id,
            arguments,
            trace_id,
            deadline,
            CancellationToken::new(),
        )
        .await
    }

    pub async fn invoke_root_cancellable(
        &self,
        capability_id: &str,
        arguments: Value,
        trace_id: impl Into<String>,
        deadline: Duration,
        cancellation: CancellationToken,
    ) -> Result<Value, BrokerError> {
        self.invoke_inner(
            capability_id,
            arguments,
            InvocationContext {
                trace_id: trace_id.into(),
                call_chain: Vec::new(),
            },
            deadline,
            cancellation,
        )
        .await
    }

    async fn invoke_inner(
        &self,
        capability_id: &str,
        arguments: Value,
        mut context: InvocationContext,
        deadline: Duration,
        cancellation: CancellationToken,
    ) -> Result<Value, BrokerError> {
        let (host_id, host_epoch, input_validator, output_validator) = {
            let state = self.state.read().await;
            let Some(capability) = state.capabilities.get(capability_id) else {
                return Err(BrokerError::CapabilityUnavailable(capability_id.to_owned()));
            };
            let Some(generation) = state.hosts.get(&capability.host_id) else {
                return Err(BrokerError::HostUnavailable(capability.host_id.clone()));
            };
            if !generation.active {
                return Err(BrokerError::HostUnavailable(capability.host_id.clone()));
            }
            if generation.epoch != capability.host_epoch {
                return Err(BrokerError::StaleEpoch {
                    host_id: capability.host_id.clone(),
                    epoch: capability.host_epoch,
                });
            }
            (
                capability.host_id.clone(),
                capability.host_epoch,
                capability.input_validator.clone(),
                capability.output_validator.clone(),
            )
        };
        let argument_errors = input_validator
            .iter_errors(&arguments)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        if !argument_errors.is_empty() {
            return Err(BrokerError::InvalidArguments {
                capability_id: capability_id.to_owned(),
                reason: argument_errors.join("; "),
            });
        }
        if context.call_chain.len() >= MAX_CALL_DEPTH {
            return Err(BrokerError::MaxCallDepth);
        }
        if context.call_chain.iter().any(|frame| {
            frame.host_id == host_id
                && frame.host_epoch == host_epoch
                && frame.capability_id == capability_id
        }) {
            return Err(BrokerError::LoopDetected(capability_id.to_owned()));
        }
        context.call_chain.push(InvocationFrame {
            host_id: host_id.clone(),
            host_epoch,
            capability_id: capability_id.to_owned(),
        });

        let call = (self.invoke)(
            host_id.clone(),
            host_epoch,
            capability_id.to_owned(),
            arguments,
            context,
        );
        let result = tokio::select! {
            _ = cancellation.cancelled() => return Err(BrokerError::Cancelled),
            result = tokio::time::timeout(deadline, call) => {
                result.map_err(|_| BrokerError::DeadlineExceeded(capability_id.to_owned()))??
            }
        };

        self.ensure_active(&host_id, host_epoch).await?;
        let result_errors = output_validator
            .iter_errors(&result)
            .map(|error| error.to_string())
            .collect::<Vec<_>>();
        if !result_errors.is_empty() {
            return Err(BrokerError::InvalidResult {
                capability_id: capability_id.to_owned(),
                reason: result_errors.join("; "),
            });
        }
        Ok(result)
    }

    async fn ensure_active(&self, host_id: &str, epoch: u64) -> Result<(), BrokerError> {
        let state = self.state.read().await;
        match state.hosts.get(host_id) {
            Some(generation) if !generation.active => {
                Err(BrokerError::HostUnavailable(host_id.to_owned()))
            }
            Some(generation) if generation.epoch != epoch => Err(BrokerError::StaleEpoch {
                host_id: host_id.to_owned(),
                epoch,
            }),
            Some(_) => Ok(()),
            None => Err(BrokerError::HostUnavailable(host_id.to_owned())),
        }
    }

    fn notify_changed(&self) {
        let _ = self.changes.send(());
    }
}

fn validate_descriptor(descriptor: &CapabilityDescriptor) -> Result<(), BrokerError> {
    if descriptor.capability_id.trim().is_empty() {
        return Err(BrokerError::InvalidDescriptor(
            "capability_id cannot be empty".to_owned(),
        ));
    }
    if !descriptor.pure {
        return Err(BrokerError::InvalidDescriptor(
            "T10 capability mappings must be pure functions".to_owned(),
        ));
    }
    let value = serde_json::to_value(descriptor)
        .map_err(|error| BrokerError::InvalidDescriptor(error.to_string()))?;
    ligamina_contracts::validate_capability_descriptor(&value)
        .map_err(|error| BrokerError::InvalidDescriptor(error.to_string()))?;
    Ok(())
}

fn compile_schema(schema: &Value) -> Result<Validator, BrokerError> {
    jsonschema::validator_for(schema)
        .map_err(|error| BrokerError::InvalidDescriptor(format!("invalid JSON Schema: {error}")))
}
