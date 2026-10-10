//! Durable, single-Host event routing with per-conversation ordering and bounded queues.

use std::{
    collections::{HashMap, HashSet},
    future::Future,
    pin::Pin,
    sync::Arc,
    time::Duration,
};

use ligamina_contracts::{ContractError, validate_event};
use serde_json::Value;
use thiserror::Error;
use tokio::{
    sync::{Mutex, OwnedMutexGuard, OwnedSemaphorePermit, Semaphore, mpsc},
    time::timeout,
};
use uuid::Uuid;

use crate::store::{EventDisposition, EventKey, EventRecord, EventStatus, Store, StoreError};

const WORKER_IDLE_TIMEOUT: Duration = Duration::from_secs(2);

type ProcessorFuture = Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'static>>;
type EventProcessor = dyn Fn(String, Value) -> ProcessorFuture + Send + Sync + 'static;

#[derive(Clone, Debug)]
pub struct RouterLimits {
    /// Maximum accepted, queued, or processing events for one conversation.
    pub max_pending_per_conversation: usize,
    /// Maximum accepted, queued, or processing events across all conversations.
    pub max_outstanding_events: usize,
    /// Maximum number of active conversation queue workers held at once.
    pub max_tracked_conversations: usize,
    /// Maximum Host event handlers running at once across different conversations.
    pub max_concurrent_conversations: usize,
    pub processing_timeout: Duration,
}

impl Default for RouterLimits {
    fn default() -> Self {
        Self {
            max_pending_per_conversation: 32,
            max_outstanding_events: 1024,
            max_tracked_conversations: 4096,
            max_concurrent_conversations: 32,
            processing_timeout: Duration::from_secs(30),
        }
    }
}

#[derive(Debug, Error)]
pub enum RouterError {
    #[error("event contract validation failed: {0}")]
    Contract(#[from] ContractError),
    #[error("event store operation failed: {0}")]
    Store(#[from] StoreError),
    #[error("router limits must all be greater than zero")]
    InvalidLimits,
    #[error("event targets an unregistered Host: {0}")]
    UnroutableHost(String),
    #[error("event router is overloaded")]
    Overloaded,
    #[error("event queue worker stopped before accepting the event")]
    WorkerStopped,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct ConversationScope {
    client_id: String,
    platform: String,
    account_id: String,
    conversation_kind: String,
    conversation_id: String,
}

struct ConversationWorker {
    generation: Uuid,
    admission: Arc<Mutex<()>>,
    pending: Arc<Semaphore>,
    sender: mpsc::Sender<QueuedEvent>,
}

struct QueuedEvent {
    record: EventRecord,
    _conversation_permit: OwnedSemaphorePermit,
    _global_permit: OwnedSemaphorePermit,
}

struct RouterInner {
    store: Arc<Store>,
    hosts: HashSet<String>,
    limits: RouterLimits,
    processor: Arc<EventProcessor>,
    workers: Mutex<HashMap<ConversationScope, Arc<ConversationWorker>>>,
    outstanding: Arc<Semaphore>,
    concurrency: Arc<Semaphore>,
}

/// Routes each accepted event to exactly its configured `target_host`.
/// The processor is responsible for resolving that active Host generation.
#[derive(Clone)]
pub struct EventRouter {
    inner: Arc<RouterInner>,
}

impl EventRouter {
    pub fn new<F, Fut>(
        store: Arc<Store>,
        hosts: impl IntoIterator<Item = String>,
        limits: RouterLimits,
        processor: F,
    ) -> Result<Self, RouterError>
    where
        F: Fn(String, Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), String>> + Send + 'static,
    {
        if limits.max_pending_per_conversation == 0
            || limits.max_outstanding_events == 0
            || limits.max_tracked_conversations == 0
            || limits.max_concurrent_conversations == 0
            || limits.processing_timeout.is_zero()
            || limits.max_pending_per_conversation > Semaphore::MAX_PERMITS
            || limits.max_outstanding_events > Semaphore::MAX_PERMITS
            || limits.max_concurrent_conversations > Semaphore::MAX_PERMITS
        {
            return Err(RouterError::InvalidLimits);
        }
        let hosts: HashSet<_> = hosts.into_iter().collect();
        if hosts.is_empty() || hosts.iter().any(|host| host.trim().is_empty()) {
            return Err(RouterError::InvalidLimits);
        }
        Ok(Self {
            inner: Arc::new(RouterInner {
                store,
                hosts,
                outstanding: Arc::new(Semaphore::new(limits.max_outstanding_events)),
                concurrency: Arc::new(Semaphore::new(limits.max_concurrent_conversations)),
                limits,
                processor: Arc::new(move |host, event| Box::pin(processor(host, event))),
                workers: Mutex::new(HashMap::new()),
            }),
        })
    }

    /// Validate, persist, and queue one event. This returns after durable acceptance,
    /// before the Host finishes processing or any platform action returns a receipt.
    pub async fn dispatch(
        &self,
        client_id: String,
        event: Value,
        now_ms: i64,
    ) -> Result<(EventDisposition, EventRecord), RouterError> {
        validate_event(&event)?;
        let target_host = required_string(&event, "target_host")?;
        if !self.inner.hosts.contains(&target_host) {
            return Err(RouterError::UnroutableHost(target_host));
        }
        let key = event_key(&client_id, &event)?;
        let scope = ConversationScope {
            client_id: key.client_id.clone(),
            platform: key.platform.clone(),
            account_id: key.account_id.clone(),
            conversation_kind: key.conversation_kind.clone(),
            conversation_id: key.conversation_id.clone(),
        };

        let (worker, _admission) = self.worker_for(&scope).await?;
        if self.inner.store.find_event(key.clone()).await?.is_some() {
            let (disposition, record) = self
                .inner
                .store
                .accept_event(client_id, event, now_ms)
                .await?;
            return Ok((disposition, record));
        }

        let conversation_permit = worker
            .pending
            .clone()
            .try_acquire_owned()
            .map_err(|_| RouterError::Overloaded)?;
        let global_permit = self
            .inner
            .outstanding
            .clone()
            .try_acquire_owned()
            .map_err(|_| RouterError::Overloaded)?;

        let (disposition, record) = self
            .inner
            .store
            .accept_event(client_id, event, now_ms)
            .await?;
        if disposition == EventDisposition::Duplicate {
            return Ok((disposition, record));
        }

        let queued = QueuedEvent {
            record: record.clone(),
            _conversation_permit: conversation_permit,
            _global_permit: global_permit,
        };
        if let Err(error) = worker.sender.try_send(queued) {
            let event_record = error.into_inner().record;
            self.inner
                .store
                .transition_event(
                    event_record.key,
                    EventStatus::Failed,
                    Some("event queue worker was unavailable".to_owned()),
                    now_ms,
                )
                .await?;
            return Err(RouterError::WorkerStopped);
        }
        Ok((disposition, record))
    }

    pub async fn status(&self, key: EventKey) -> Result<EventRecord, RouterError> {
        Ok(self.inner.store.get_event(key).await?)
    }

    async fn worker_for(
        &self,
        scope: &ConversationScope,
    ) -> Result<(Arc<ConversationWorker>, OwnedMutexGuard<()>), RouterError> {
        let mut workers = self.inner.workers.lock().await;
        let worker = if let Some(worker) = workers.get(scope) {
            worker.clone()
        } else {
            if workers.len() >= self.inner.limits.max_tracked_conversations {
                return Err(RouterError::Overloaded);
            }
            let generation = Uuid::new_v4();
            let (sender, receiver) = mpsc::channel(self.inner.limits.max_pending_per_conversation);
            let worker = Arc::new(ConversationWorker {
                generation,
                admission: Arc::new(Mutex::new(())),
                pending: Arc::new(Semaphore::new(
                    self.inner.limits.max_pending_per_conversation,
                )),
                sender,
            });
            workers.insert(scope.clone(), worker.clone());
            spawn_worker(
                Arc::downgrade(&self.inner),
                scope.clone(),
                generation,
                receiver,
                self.inner.store.clone(),
                self.inner.processor.clone(),
                self.inner.concurrency.clone(),
                self.inner.limits.processing_timeout,
            );
            worker
        };
        let admission = worker.admission.clone().lock_owned().await;
        drop(workers);
        Ok((worker, admission))
    }
}

fn spawn_worker(
    inner: std::sync::Weak<RouterInner>,
    scope: ConversationScope,
    generation: Uuid,
    mut receiver: mpsc::Receiver<QueuedEvent>,
    store: Arc<Store>,
    processor: Arc<EventProcessor>,
    concurrency: Arc<Semaphore>,
    processing_timeout: Duration,
) {
    tokio::spawn(async move {
        loop {
            match timeout(WORKER_IDLE_TIMEOUT, receiver.recv()).await {
                Ok(Some(event)) => {
                    process_event(
                        event,
                        &store,
                        processor.as_ref(),
                        &concurrency,
                        processing_timeout,
                    )
                    .await;
                }
                Ok(None) => break,
                Err(_) => {
                    match retire_idle_worker(&inner, &scope, generation, &mut receiver).await {
                        Some(event) => {
                            process_event(
                                event,
                                &store,
                                processor.as_ref(),
                                &concurrency,
                                processing_timeout,
                            )
                            .await;
                        }
                        None => break,
                    }
                }
            }
        }
    });
}

async fn retire_idle_worker(
    inner: &std::sync::Weak<RouterInner>,
    scope: &ConversationScope,
    generation: Uuid,
    receiver: &mut mpsc::Receiver<QueuedEvent>,
) -> Option<QueuedEvent> {
    let Some(inner) = inner.upgrade() else {
        return None;
    };
    let mut workers = inner.workers.lock().await;
    let Some(worker) = workers.get(scope).cloned() else {
        return None;
    };
    if worker.generation != generation {
        return None;
    }
    let _admission = worker.admission.lock().await;
    match receiver.try_recv() {
        Ok(event) => Some(event),
        Err(mpsc::error::TryRecvError::Empty) => {
            workers.remove(scope);
            None
        }
        Err(mpsc::error::TryRecvError::Disconnected) => {
            workers.remove(scope);
            None
        }
    }
}

async fn process_event(
    queued: QueuedEvent,
    store: &Store,
    processor: &EventProcessor,
    concurrency: &Semaphore,
    processing_timeout: Duration,
) {
    let record = queued.record;
    let Ok(_concurrency) = concurrency.acquire().await else {
        let _ = store
            .transition_event(
                record.key,
                EventStatus::Failed,
                Some("router is shutting down".to_owned()),
                now_ms(),
            )
            .await;
        return;
    };
    if store
        .transition_event(record.key.clone(), EventStatus::Processing, None, now_ms())
        .await
        .is_err()
    {
        return;
    }
    let outcome = timeout(
        processing_timeout,
        processor(record.target_host, record.event),
    )
    .await;
    match outcome {
        Ok(Ok(())) => {
            let _ = store
                .transition_event(record.key, EventStatus::Completed, None, now_ms())
                .await;
        }
        Ok(Err(error)) => {
            let _ = store
                .transition_event(record.key, EventStatus::Failed, Some(error), now_ms())
                .await;
        }
        Err(_) => {
            let _ = store
                .transition_event(
                    record.key,
                    EventStatus::Unknown,
                    Some("Host event processing timed out".to_owned()),
                    now_ms(),
                )
                .await;
        }
    }
}

fn event_key(client_id: &str, event: &Value) -> Result<EventKey, RouterError> {
    let conversation = event
        .get("conversation")
        .ok_or_else(|| StoreError::InvalidInput("event conversation is missing".to_owned()))?;
    Ok(EventKey {
        client_id: client_id.to_owned(),
        platform: required_string(event, "platform")?,
        account_id: required_string(event, "account_id")?,
        conversation_kind: required_string(conversation, "kind")?,
        conversation_id: required_string(conversation, "id")?,
        event_id: required_string(event, "event_id")?,
    })
}

fn required_string(value: &Value, key: &str) -> Result<String, RouterError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| {
            StoreError::InvalidInput(format!("event field {key} must be a string")).into()
        })
}

fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}
