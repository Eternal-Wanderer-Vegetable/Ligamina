//! Bidirectional JSON-RPC-style NDJSON sessions for local Host processes.

use std::{
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};

use serde_json::{Value, json};
use thiserror::Error;
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader},
    sync::{Mutex, OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch},
    time::Instant,
};
use tokio_util::sync::CancellationToken;

pub const MAX_FRAME_BYTES: usize = 1024 * 1024;
pub const MAX_IN_FLIGHT_REQUESTS: usize = 64;

type HandlerResult = Result<Value, RpcError>;
type HandlerFuture = Pin<Box<dyn Future<Output = HandlerResult> + Send + 'static>>;
type RequestHandler = dyn Fn(HostSession, String, Value) -> HandlerFuture + Send + Sync + 'static;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RpcError {
    #[error("overloaded: maximum of {MAX_IN_FLIGHT_REQUESTS} in-flight requests reached")]
    Overloaded,
    #[error("request {0} exceeded its deadline")]
    DeadlineExceeded(String),
    #[error("request {0} was cancelled")]
    Cancelled(String),
    #[error("host session is closed: {0}")]
    Closed(String),
    #[error("malformed NDJSON/RPC frame: {0}")]
    Malformed(String),
    #[error("NDJSON frame exceeds the {MAX_FRAME_BYTES}-byte limit")]
    FrameTooLarge,
    #[error("transport I/O failed: {0}")]
    Io(String),
    #[error("remote error {code}: {message}")]
    Remote { code: i64, message: String },
    #[error("request writer is unavailable")]
    WriterUnavailable,
}

struct Inner {
    outgoing: mpsc::Sender<Value>,
    pending: Mutex<HashMap<String, oneshot::Sender<HandlerResult>>>,
    in_flight: Arc<Semaphore>,
    next_id: AtomicU64,
    late_responses: AtomicUsize,
    terminal_tx: watch::Sender<Option<RpcError>>,
    terminal_rx: watch::Receiver<Option<RpcError>>,
    handler: Arc<RequestHandler>,
}

#[derive(Clone)]
pub struct HostSession {
    inner: Arc<Inner>,
}

impl HostSession {
    pub fn new<R, W, F, Fut>(reader: R, writer: W, handler: F) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
        F: Fn(HostSession, String, Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = HandlerResult> + Send + 'static,
    {
        let (outgoing, outgoing_rx) = mpsc::channel(MAX_IN_FLIGHT_REQUESTS + 8);
        let (terminal_tx, terminal_rx) = watch::channel(None);
        let handler: Arc<RequestHandler> =
            Arc::new(move |session, method, params| Box::pin(handler(session, method, params)));
        let inner = Arc::new(Inner {
            outgoing,
            pending: Mutex::new(HashMap::new()),
            in_flight: Arc::new(Semaphore::new(MAX_IN_FLIGHT_REQUESTS)),
            next_id: AtomicU64::new(1),
            late_responses: AtomicUsize::new(0),
            terminal_tx,
            terminal_rx,
            handler,
        });

        tokio::spawn(writer_loop(writer, outgoing_rx, inner.clone()));
        tokio::spawn(reader_loop(
            BufReader::new(reader),
            HostSession {
                inner: inner.clone(),
            },
        ));
        Self { inner }
    }

    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> HandlerResult {
        self.request_cancellable(method, params, timeout, CancellationToken::new())
            .await
    }

    pub async fn request_cancellable(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
        cancellation: CancellationToken,
    ) -> HandlerResult {
        let permit = self.acquire_slot()?;
        let id = format!("c-{}", self.inner.next_id.fetch_add(1, Ordering::Relaxed));
        let (result_tx, result_rx) = oneshot::channel();
        self.inner
            .pending
            .lock()
            .await
            .insert(id.clone(), result_tx);

        if let Err(error) = self
            .send_packet(json!({
                "jsonrpc": "2.0",
                "id": id,
                "method": method,
                "params": params,
            }))
            .await
        {
            self.inner.pending.lock().await.remove(&id);
            drop(permit);
            return Err(error);
        }

        let deadline = tokio::time::sleep_until(Instant::now() + timeout);
        tokio::pin!(deadline);
        let result = tokio::select! {
            result = result_rx => result.unwrap_or_else(|_| Err(self.closed_error())),
            _ = cancellation.cancelled() => {
                self.cancel_pending(&id).await;
                Err(RpcError::Cancelled(id.clone()))
            }
            _ = &mut deadline => {
                self.cancel_pending(&id).await;
                Err(RpcError::DeadlineExceeded(id.clone()))
            }
        };
        drop(permit);
        result
    }

    pub async fn notify(&self, method: &str, params: Value) -> Result<(), RpcError> {
        self.send_packet(json!({"jsonrpc":"2.0", "method":method, "params":params}))
            .await
    }

    pub fn late_response_count(&self) -> usize {
        self.inner.late_responses.load(Ordering::Relaxed)
    }

    pub async fn pending_count(&self) -> usize {
        self.inner.pending.lock().await.len()
    }

    pub async fn wait_closed(&self) -> RpcError {
        let mut terminal_rx = self.inner.terminal_rx.clone();
        loop {
            if let Some(error) = terminal_rx.borrow().clone() {
                return error;
            }
            if terminal_rx.changed().await.is_err() {
                return RpcError::Closed("terminal state channel closed".to_owned());
            }
        }
    }

    fn acquire_slot(&self) -> Result<OwnedSemaphorePermit, RpcError> {
        if let Some(error) = self.inner.terminal_rx.borrow().clone() {
            return Err(error);
        }
        self.inner
            .in_flight
            .clone()
            .try_acquire_owned()
            .map_err(|_| RpcError::Overloaded)
    }

    fn closed_error(&self) -> RpcError {
        self.inner
            .terminal_rx
            .borrow()
            .clone()
            .unwrap_or_else(|| RpcError::Closed("host session ended".to_owned()))
    }

    async fn send_packet(&self, packet: Value) -> Result<(), RpcError> {
        if let Some(error) = self.inner.terminal_rx.borrow().clone() {
            return Err(error);
        }
        self.inner
            .outgoing
            .send(packet)
            .await
            .map_err(|_| RpcError::WriterUnavailable)
    }

    async fn cancel_pending(&self, id: &str) {
        self.inner.pending.lock().await.remove(id);
        let _ = self
            .send_packet(json!({
                "jsonrpc": "2.0",
                "method": "request.cancel",
                "params": {"request_id": id},
            }))
            .await;
    }
}

async fn writer_loop<W>(mut writer: W, mut outgoing: mpsc::Receiver<Value>, inner: Arc<Inner>)
where
    W: AsyncWrite + Unpin,
{
    while let Some(packet) = outgoing.recv().await {
        let mut frame = match serde_json::to_vec(&packet) {
            Ok(frame) if frame.len() <= MAX_FRAME_BYTES => frame,
            Ok(_) => {
                finish_session(&inner, RpcError::FrameTooLarge).await;
                return;
            }
            Err(error) => {
                finish_session(&inner, RpcError::Malformed(error.to_string())).await;
                return;
            }
        };
        frame.push(b'\n');
        if let Err(error) = writer.write_all(&frame).await {
            finish_session(&inner, RpcError::Io(error.to_string())).await;
            return;
        }
        if let Err(error) = writer.flush().await {
            finish_session(&inner, RpcError::Io(error.to_string())).await;
            return;
        }
    }
}

async fn reader_loop<R>(mut reader: BufReader<R>, session: HostSession)
where
    R: AsyncRead + Unpin,
{
    loop {
        let frame = match read_frame(&mut reader).await {
            Ok(Some(frame)) => frame,
            Ok(None) => {
                finish_session(
                    &session.inner,
                    RpcError::Closed("host stdout reached EOF".to_owned()),
                )
                .await;
                return;
            }
            Err(error) => {
                finish_session(&session.inner, error).await;
                return;
            }
        };
        let packet: Value = match serde_json::from_slice(&frame) {
            Ok(packet) => packet,
            Err(error) => {
                finish_session(
                    &session.inner,
                    RpcError::Malformed(format!("invalid JSON: {error}")),
                )
                .await;
                return;
            }
        };
        if packet.get("jsonrpc") != Some(&Value::String("2.0".to_owned())) {
            finish_session(
                &session.inner,
                RpcError::Malformed("jsonrpc must equal 2.0".to_owned()),
            )
            .await;
            return;
        }
        if let Some(method) = packet.get("method").and_then(Value::as_str) {
            if let Some(id) = packet.get("id") {
                let Some(id) = id.as_str() else {
                    finish_session(
                        &session.inner,
                        RpcError::Malformed("request id must be a string".to_owned()),
                    )
                    .await;
                    return;
                };
                let method = method.to_owned();
                let params = packet.get("params").cloned().unwrap_or(Value::Null);
                let session = session.clone();
                let id = id.to_owned();
                tokio::spawn(async move {
                    let response =
                        match (session.inner.handler)(session.clone(), method, params).await {
                            Ok(result) => json!({"jsonrpc":"2.0", "id":id, "result":result}),
                            Err(error) => json!({
                                "jsonrpc":"2.0", "id":id,
                                "error":{"code":-32000, "message":error.to_string()}
                            }),
                        };
                    let _ = session.send_packet(response).await;
                });
            }
            // Notifications, including request.cancel, have no response.
            continue;
        }

        let Some(id) = packet.get("id").and_then(Value::as_str) else {
            finish_session(
                &session.inner,
                RpcError::Malformed("response must contain a string id".to_owned()),
            )
            .await;
            return;
        };
        let pending = session.inner.pending.lock().await.remove(id);
        if let Some(sender) = pending {
            let result = if let Some(error) = packet.get("error") {
                let code = error.get("code").and_then(Value::as_i64).unwrap_or(-32000);
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("remote request failed")
                    .to_owned();
                Err(RpcError::Remote { code, message })
            } else if let Some(result) = packet.get("result") {
                Ok(result.clone())
            } else {
                Err(RpcError::Malformed(
                    "response must contain result or error".to_owned(),
                ))
            };
            let _ = sender.send(result);
        } else {
            session.inner.late_responses.fetch_add(1, Ordering::Relaxed);
        }
    }
}

async fn read_frame<R>(reader: &mut R) -> Result<Option<Vec<u8>>, RpcError>
where
    R: AsyncBufRead + Unpin,
{
    let mut frame = Vec::new();
    loop {
        let buffer = reader
            .fill_buf()
            .await
            .map_err(|error| RpcError::Io(error.to_string()))?;
        if buffer.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(RpcError::Malformed(
                    "EOF occurred before the frame terminator".to_owned(),
                ))
            };
        }

        if let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
            if frame.len() + newline > MAX_FRAME_BYTES {
                return Err(RpcError::FrameTooLarge);
            }
            frame.extend_from_slice(&buffer[..newline]);
            reader.consume(newline + 1);
            if frame.last() == Some(&b'\r') {
                frame.pop();
            }
            if frame.is_empty() {
                return Err(RpcError::Malformed("empty frame".to_owned()));
            }
            return Ok(Some(frame));
        }

        if frame.len() + buffer.len() > MAX_FRAME_BYTES {
            return Err(RpcError::FrameTooLarge);
        }
        let length = buffer.len();
        frame.extend_from_slice(buffer);
        reader.consume(length);
    }
}

async fn finish_session(inner: &Inner, error: RpcError) {
    if inner.terminal_tx.borrow().is_some() {
        return;
    }
    inner.terminal_tx.send_replace(Some(error.clone()));
    let mut pending = inner.pending.lock().await;
    for (_, sender) in pending.drain() {
        let _ = sender.send(Err(error.clone()));
    }
}
