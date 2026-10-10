//! Loopback management, event, gateway, capability, and MCP HTTP endpoints.

use std::{
    collections::{HashMap, HashSet},
    net::SocketAddr,
    sync::Arc,
    time::Duration,
};

use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Query, State},
    http::{HeaderMap, Request, StatusCode, header::AUTHORIZATION},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use ligamina_core::{
    broker::CapabilityInfo,
    platform::PlatformGateway,
    policy::Principal,
    runtime::{CoreRuntime, event_record_json, operation_record_json},
    store::{EventKey, GatewayOperationResult, GatewayRegistration, LeaseCredential, OperationKey},
};
use rmcp::transport::{
    StreamableHttpServerConfig, StreamableHttpService,
    streamable_http_server::session::local::LocalSessionManager,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Debug)]
pub struct ApiClient {
    pub id: String,
    pub token: String,
    pub capabilities: HashSet<String>,
    pub operation_grants: HashMap<String, String>,
}

#[derive(Clone, Debug)]
pub struct GatewayClient {
    pub registration: GatewayRegistration,
    pub registration_token: String,
}

#[derive(Clone)]
pub struct HttpState {
    pub runtime: CoreRuntime,
    pub listen: SocketAddr,
    pub admin_token: String,
    pub clients: Arc<HashMap<String, ApiClient>>,
    pub gateways: Arc<HashMap<String, GatewayClient>>,
    pub mcp_client_id: String,
    pub shutdown: CancellationToken,
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

impl ApiError {
    fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }

    fn unauthorized() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "valid bearer token required",
        )
    }

    fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "invalid_request", message)
    }

    fn internal(message: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal_error", message)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({
                "error": {"code": self.code, "message": self.message}
            })),
        )
            .into_response()
    }
}

#[derive(Deserialize)]
struct EventQuery {
    platform: String,
    account_id: String,
    conversation_kind: String,
    conversation_id: String,
}

#[derive(Deserialize)]
struct InvocationRequest {
    capability_id: String,
    arguments: Value,
    #[serde(default)]
    trace_id: Option<String>,
}

#[derive(Deserialize)]
struct LeaseRequest {
    ttl_ms: u64,
}

#[derive(Deserialize)]
struct OperationQuery {
    account_id: String,
    operation: String,
}

#[derive(Deserialize)]
struct GatewayResultRequest {
    credential: LeaseCredential,
    result: GatewayOperationResult,
}

#[derive(Serialize)]
struct CapabilityResponse {
    host_id: String,
    host_epoch: u64,
    descriptor: ligamina_core::broker::CapabilityDescriptor,
}

pub fn build_router(state: HttpState) -> Router {
    let mcp_client = state.clients.get(&state.mcp_client_id).cloned();
    let broker = state.runtime.broker().clone();
    let capabilities = mcp_client
        .as_ref()
        .map(|client| client.capabilities.iter().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    let mcp_service = StreamableHttpService::new(
        move || {
            Ok(crate::mcp::LigaminaMcp::with_allowed_capabilities(
                broker.clone(),
                capabilities.clone(),
            ))
        },
        Arc::new(LocalSessionManager::default()),
        StreamableHttpServerConfig::default()
            .with_allowed_hosts([state.listen.to_string()])
            .with_allowed_origins([format!("http://{}", state.listen)])
            .enforce_origin_validation(),
    );
    let mcp_state = state.clone();
    let mcp_router = Router::new()
        .route_service("/mcp", mcp_service)
        .layer(middleware::from_fn_with_state(mcp_state, authorize_mcp));

    Router::new()
        .route("/v1/status", get(status))
        .route("/v1/events", post(submit_event))
        .route("/v1/events/{event_id}", get(get_event))
        .route("/v1/capabilities", get(list_capabilities))
        .route("/v1/invocations", post(invoke_capability))
        .route("/v1/gateways/{gateway_id}/lease", post(acquire_lease))
        .route(
            "/v1/gateways/{gateway_id}/actions/claim",
            post(claim_operation),
        )
        .route(
            "/v1/gateways/{gateway_id}/actions/{operation_id}/result",
            post(report_result),
        )
        .route("/v1/operations/{operation_id}", get(get_operation))
        .route("/v1/shutdown", post(shutdown))
        .merge(mcp_router)
        .with_state(state)
}

async fn authorize_mcp(
    State(state): State<HttpState>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let token = bearer_token(request.headers());
    let authorized = token.is_some_and(|token| {
        state
            .clients
            .get(&state.mcp_client_id)
            .is_some_and(|client| constant_time_eq(token, &client.token))
    });
    if !authorized {
        return ApiError::unauthorized().into_response();
    }
    next.run(request).await
}

async fn status(
    State(state): State<HttpState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    require_admin(&state, &headers)?;
    let hosts = state
        .runtime
        .host_status()
        .await
        .into_iter()
        .map(|host| {
            json!({
                "id": host.id,
                "state": host_state_name(host.state),
                "epoch": host.epoch,
                "pid": host.pid,
                "restart_count": host.restart_count,
                "last_error": host.last_error,
            })
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({
        "core_epoch": state.runtime.store().core_epoch(),
        "schema_version": state.runtime.store().schema_version().await.map_err(|error| ApiError::internal(error.to_string()))?,
        "hosts": hosts,
    })))
}

async fn submit_event(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Json(event): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let client = authenticate_client(&state, &headers)?;
    let (disposition, record) = state
        .runtime
        .dispatch_event(client.id.clone(), event)
        .await
        .map_err(|error| {
            ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "event_rejected",
                error.to_string(),
            )
        })?;
    let disposition = match disposition {
        ligamina_core::store::EventDisposition::Accepted => "accepted",
        ligamina_core::store::EventDisposition::Duplicate => "duplicate",
    };
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({"disposition": disposition, "event": event_record_json(&record)})),
    ))
}

async fn get_event(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Path(event_id): Path<String>,
    Query(query): Query<EventQuery>,
) -> Result<Json<Value>, ApiError> {
    let client = authenticate_client(&state, &headers)?;
    let key = EventKey {
        client_id: client.id.clone(),
        platform: query.platform,
        account_id: query.account_id,
        conversation_kind: query.conversation_kind,
        conversation_id: query.conversation_id,
        event_id,
    };
    let record = state.runtime.event_status(key).await.map_err(|error| {
        ApiError::new(StatusCode::NOT_FOUND, "event_not_found", error.to_string())
    })?;
    Ok(Json(event_record_json(&record)))
}

async fn list_capabilities(
    State(state): State<HttpState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let client = authenticate_client(&state, &headers)?;
    let capabilities = state
        .runtime
        .capabilities()
        .await
        .into_iter()
        .filter(|capability| {
            client
                .capabilities
                .contains(&capability.descriptor.capability_id)
        })
        .map(capability_json)
        .collect::<Vec<_>>();
    Ok(Json(json!({"capabilities": capabilities})))
}

async fn invoke_capability(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Json(request): Json<InvocationRequest>,
) -> Result<Json<Value>, ApiError> {
    let client = authenticate_client(&state, &headers)?;
    if !client.capabilities.contains(&request.capability_id) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "capability_denied",
            "capability is not granted to this client",
        ));
    }
    if !request.arguments.is_object() {
        return Err(ApiError::bad_request("arguments must be a JSON object"));
    }
    let trace_id = request
        .trace_id
        .unwrap_or_else(|| format!("http:{}", uuid::Uuid::new_v4()));
    let result = state
        .runtime
        .invoke_capability(&request.capability_id, request.arguments, trace_id)
        .await
        .map_err(|error| {
            ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invocation_failed",
                error.to_string(),
            )
        })?;
    Ok(Json(json!({"result": result})))
}

async fn acquire_lease(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Path(gateway_id): Path<String>,
    Json(request): Json<LeaseRequest>,
) -> Result<Json<Value>, ApiError> {
    let gateway = state.gateways.get(&gateway_id).ok_or_else(|| {
        ApiError::new(
            StatusCode::NOT_FOUND,
            "gateway_not_found",
            "gateway is not registered",
        )
    })?;
    let token = bearer_token(&headers).ok_or_else(ApiError::unauthorized)?;
    if !constant_time_eq(token, &gateway.registration_token) {
        return Err(ApiError::unauthorized());
    }
    if !(1_000..=300_000).contains(&request.ttl_ms) {
        return Err(ApiError::bad_request(
            "ttl_ms must be between 1000 and 300000",
        ));
    }
    let lease = state
        .runtime
        .platform()
        .acquire_lease(
            gateway_id,
            token.to_owned(),
            Duration::from_millis(request.ttl_ms),
            PlatformGateway::current_time_ms(),
        )
        .await
        .map_err(|error| ApiError::new(StatusCode::FORBIDDEN, "lease_denied", error.to_string()))?;
    Ok(Json(json!(lease)))
}

async fn claim_operation(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Path(gateway_id): Path<String>,
    Json(credential): Json<LeaseCredential>,
) -> Result<Json<Value>, ApiError> {
    validate_gateway_credential(&state, &headers, &gateway_id, &credential)?;
    let record = state
        .runtime
        .platform()
        .claim(credential, PlatformGateway::current_time_ms())
        .await
        .map_err(|error| ApiError::new(StatusCode::FORBIDDEN, "claim_denied", error.to_string()))?;
    Ok(Json(
        record
            .map(|record| operation_record_json(&record))
            .unwrap_or(Value::Null),
    ))
}

async fn report_result(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Path((gateway_id, operation_id)): Path<(String, String)>,
    Json(request): Json<GatewayResultRequest>,
) -> Result<Json<Value>, ApiError> {
    validate_gateway_credential(&state, &headers, &gateway_id, &request.credential)?;
    if request.result.key.operation_id != operation_id {
        return Err(ApiError::bad_request(
            "operation ID in path does not match result",
        ));
    }
    let record = state
        .runtime
        .platform()
        .report_result(
            request.credential,
            request.result,
            PlatformGateway::current_time_ms(),
        )
        .await
        .map_err(|error| {
            ApiError::new(StatusCode::FORBIDDEN, "result_denied", error.to_string())
        })?;
    Ok(Json(operation_record_json(&record)))
}

async fn get_operation(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Path(operation_id): Path<String>,
    Query(query): Query<OperationQuery>,
) -> Result<Json<Value>, ApiError> {
    let client = authenticate_client(&state, &headers)?;
    let grant_id = client
        .operation_grants
        .get(&query.account_id)
        .cloned()
        .ok_or_else(|| {
            ApiError::new(
                StatusCode::FORBIDDEN,
                "grant_missing",
                "client has no operation read grant for this account",
            )
        })?;
    let key = OperationKey {
        client_id: client.id.clone(),
        account_id: query.account_id,
        operation: query.operation,
        operation_id,
    };
    let record = state
        .runtime
        .platform()
        .operation_status(
            key,
            grant_id,
            Principal::Client(client.id.clone()),
            PlatformGateway::current_time_ms(),
        )
        .await
        .map_err(|error| {
            ApiError::new(
                StatusCode::NOT_FOUND,
                "operation_not_found",
                error.to_string(),
            )
        })?;
    Ok(Json(operation_record_json(&record)))
}

async fn shutdown(
    State(state): State<HttpState>,
    headers: HeaderMap,
) -> Result<StatusCode, ApiError> {
    require_admin(&state, &headers)?;
    state.shutdown.cancel();
    Ok(StatusCode::ACCEPTED)
}

fn validate_gateway_credential(
    state: &HttpState,
    headers: &HeaderMap,
    gateway_id: &str,
    credential: &LeaseCredential,
) -> Result<(), ApiError> {
    let gateway = state.gateways.get(gateway_id).ok_or_else(|| {
        ApiError::new(
            StatusCode::NOT_FOUND,
            "gateway_not_found",
            "gateway is not registered",
        )
    })?;
    if gateway.registration.client_id.trim().is_empty()
        || bearer_token(headers).is_none_or(|token| !constant_time_eq(token, &credential.token))
    {
        return Err(ApiError::unauthorized());
    }
    Ok(())
}

fn authenticate_client<'a>(
    state: &'a HttpState,
    headers: &HeaderMap,
) -> Result<&'a ApiClient, ApiError> {
    let token = bearer_token(headers).ok_or_else(ApiError::unauthorized)?;
    state
        .clients
        .values()
        .find(|client| constant_time_eq(token, &client.token))
        .ok_or_else(ApiError::unauthorized)
}

fn require_admin(state: &HttpState, headers: &HeaderMap) -> Result<(), ApiError> {
    let token = bearer_token(headers).ok_or_else(ApiError::unauthorized)?;
    if constant_time_eq(token, &state.admin_token) {
        Ok(())
    } else {
        Err(ApiError::unauthorized())
    }
}

fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(AUTHORIZATION)?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
}

fn constant_time_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    let mut diff = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        diff |= usize::from(
            left.get(index).copied().unwrap_or(0) ^ right.get(index).copied().unwrap_or(0),
        );
    }
    diff == 0
}

fn capability_json(capability: CapabilityInfo) -> CapabilityResponse {
    CapabilityResponse {
        host_id: capability.host_id,
        host_epoch: capability.host_epoch,
        descriptor: capability.descriptor,
    }
}

fn host_state_name(state: ligamina_core::supervisor::HostState) -> &'static str {
    match state {
        ligamina_core::supervisor::HostState::Disabled => "disabled",
        ligamina_core::supervisor::HostState::Stopped => "stopped",
        ligamina_core::supervisor::HostState::Starting => "starting",
        ligamina_core::supervisor::HostState::Active => "active",
        ligamina_core::supervisor::HostState::Backoff => "backoff",
        ligamina_core::supervisor::HostState::Failed => "failed",
        ligamina_core::supervisor::HostState::Stopping => "stopping",
    }
}

pub fn configured_capabilities_are_valid(client: &ApiClient) -> bool {
    !client.id.trim().is_empty() && !client.token.trim().is_empty()
}
