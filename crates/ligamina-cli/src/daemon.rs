//! Local HTTP daemon bootstrap and its versioned runtime configuration.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    ffi::OsString,
    net::SocketAddr,
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, bail};
use ligamina_core::{
    deployment::local::LocalCommand,
    policy::{Grant, OperationTarget, Principal},
    router::RouterLimits,
    runtime::{CoreRuntime, CoreRuntimeConfig, ManagedGateway, ManagedHost},
    store::GatewayRegistration,
    supervisor::{HostConfig, RestartPolicy},
};
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;

use crate::http::{ApiClient, GatewayClient, HttpState, build_router};

#[derive(Debug, Deserialize)]
pub struct DaemonConfig {
    pub listen: SocketAddr,
    #[serde(default)]
    pub allow_non_loopback_listen: bool,
    pub database_path: PathBuf,
    pub admin_token: String,
    pub mcp_client_id: String,
    pub clients: Vec<ClientConfig>,
    pub gateways: Vec<GatewayConfig>,
    pub hosts: Vec<HostConfigFile>,
    #[serde(default)]
    pub router: RouterConfig,
}

#[derive(Debug, Deserialize)]
pub struct ClientConfig {
    pub id: String,
    pub token: String,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default)]
    pub operation_grants: Vec<ClientOperationGrant>,
}

#[derive(Debug, Deserialize)]
pub struct ClientOperationGrant {
    pub id: String,
    pub account_id: String,
    pub operations: Vec<String>,
    #[serde(default)]
    pub targets: Option<Vec<OperationTarget>>,
}

#[derive(Debug, Deserialize)]
pub struct GatewayConfig {
    pub id: String,
    pub client_id: String,
    pub account_id: String,
    pub registration_token: String,
}

#[derive(Debug, Deserialize)]
pub struct HostConfigFile {
    pub id: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    pub program: String,
    #[serde(default)]
    pub args: Vec<String>,
    pub current_dir: Option<PathBuf>,
    #[serde(default)]
    pub environment: BTreeMap<String, String>,
    #[serde(default = "default_max_restarts")]
    pub max_restarts: u32,
    #[serde(default = "default_restart_backoff_ms")]
    pub restart_backoff_ms: u64,
    #[serde(default = "default_startup_timeout_ms")]
    pub startup_timeout_ms: u64,
    #[serde(default = "default_shutdown_timeout_ms")]
    pub shutdown_timeout_ms: u64,
    #[serde(default)]
    pub operation_grants: Vec<HostOperationGrant>,
}

#[derive(Debug, Deserialize)]
pub struct HostOperationGrant {
    pub id: String,
    pub client_id: String,
    pub account_id: String,
    pub operations: Vec<String>,
    #[serde(default)]
    pub targets: Option<Vec<OperationTarget>>,
}

#[derive(Debug, Deserialize)]
pub struct RouterConfig {
    #[serde(default = "default_conversation_limit")]
    pub max_pending_per_conversation: usize,
    #[serde(default = "default_global_limit")]
    pub max_outstanding_events: usize,
    #[serde(default = "default_tracked_conversation_limit")]
    pub max_tracked_conversations: usize,
    #[serde(default = "default_concurrency")]
    pub max_concurrent_conversations: usize,
    #[serde(default = "default_event_timeout_ms")]
    pub processing_timeout_ms: u64,
}

impl Default for RouterConfig {
    fn default() -> Self {
        Self {
            max_pending_per_conversation: default_conversation_limit(),
            max_outstanding_events: default_global_limit(),
            max_tracked_conversations: default_tracked_conversation_limit(),
            max_concurrent_conversations: default_concurrency(),
            processing_timeout_ms: default_event_timeout_ms(),
        }
    }
}

pub async fn run(config_path: PathBuf) -> anyhow::Result<()> {
    let contents = tokio::fs::read(&config_path).await.with_context(|| {
        format!(
            "could not read daemon configuration {}",
            config_path.display()
        )
    })?;
    let config: DaemonConfig = serde_json::from_slice(&contents)
        .with_context(|| format!("invalid daemon configuration {}", config_path.display()))?;
    validate_config(&config)?;

    if let Some(parent) = config.database_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("could not create database directory {}", parent.display()))?;
    }
    let listener = TcpListener::bind(config.listen)
        .await
        .with_context(|| format!("could not bind local Core at {}", config.listen))?;
    let listen = listener.local_addr()?;

    let mut clients = HashMap::new();
    let mut client_grants = Vec::new();
    for client in &config.clients {
        for grant in &client.operation_grants {
            client_grants.push(Grant {
                id: grant.id.clone(),
                client_id: client.id.clone(),
                principal: Principal::Client(client.id.clone()),
                account_id: grant.account_id.clone(),
                operations: grant.operations.clone(),
                targets: grant.targets.clone(),
                active: true,
            });
        }
        let operation_grants = client
            .operation_grants
            .iter()
            .map(|grant| (grant.account_id.clone(), grant.id.clone()))
            .collect();
        clients.insert(
            client.id.clone(),
            ApiClient {
                id: client.id.clone(),
                token: client.token.clone(),
                capabilities: client.capabilities.iter().cloned().collect(),
                operation_grants,
            },
        );
    }
    let mut gateways = HashMap::new();
    let runtime_gateways = config
        .gateways
        .iter()
        .map(|gateway| {
            let registration = GatewayRegistration {
                gateway_id: gateway.id.clone(),
                client_id: gateway.client_id.clone(),
                account_id: gateway.account_id.clone(),
            };
            gateways.insert(
                gateway.id.clone(),
                GatewayClient {
                    registration: registration.clone(),
                    registration_token: gateway.registration_token.clone(),
                },
            );
            ManagedGateway {
                registration,
                registration_token: gateway.registration_token.clone(),
            }
        })
        .collect::<Vec<_>>();

    let mut managed_hosts = Vec::new();
    for host in config.hosts {
        let command = LocalCommand {
            program: OsString::from(host.program),
            args: host.args.into_iter().map(OsString::from).collect(),
            current_dir: host.current_dir,
            environment: host
                .environment
                .into_iter()
                .map(|(key, value)| (OsString::from(key), OsString::from(value)))
                .collect(),
            clear_environment: false,
            startup_timeout: Duration::from_millis(host.startup_timeout_ms),
            shutdown_timeout: Duration::from_millis(host.shutdown_timeout_ms),
        };
        let mut host_config = HostConfig::new(host.id.clone(), command);
        host_config.enabled = host.enabled;
        host_config.restart = RestartPolicy {
            max_restarts: host.max_restarts,
            backoff: Duration::from_millis(host.restart_backoff_ms),
        };
        let operation_grants = host
            .operation_grants
            .into_iter()
            .map(|grant| Grant {
                id: grant.id,
                client_id: grant.client_id,
                principal: Principal::Host(host.id.clone()),
                account_id: grant.account_id,
                operations: grant.operations,
                targets: grant.targets,
                active: true,
            })
            .collect();
        managed_hosts.push(ManagedHost {
            config: host_config.clone(),
            operation_grants,
        });
    }
    let host_count = managed_hosts.len();

    // Host grants must belong to the client and account registered to the gateway.
    // ManagedHost grants are checked by CoreRuntime and the database authorizes the
    // account/operation/target on each platform.execute call.
    let runtime = CoreRuntime::start(CoreRuntimeConfig {
        database_path: config.database_path,
        hosts: managed_hosts,
        gateways: runtime_gateways,
        client_grants,
        router_limits: RouterLimits {
            max_pending_per_conversation: config.router.max_pending_per_conversation,
            max_outstanding_events: config.router.max_outstanding_events,
            max_tracked_conversations: config.router.max_tracked_conversations,
            max_concurrent_conversations: config.router.max_concurrent_conversations,
            processing_timeout: Duration::from_millis(config.router.processing_timeout_ms),
        },
    })
    .await?;

    let shutdown = CancellationToken::new();
    let state = HttpState {
        runtime: runtime.clone(),
        listen,
        admin_token: config.admin_token,
        clients: Arc::new(clients),
        gateways: Arc::new(gateways),
        mcp_client_id: config.mcp_client_id,
        shutdown: shutdown.clone(),
    };
    println!(
        "{}",
        serde_json::json!({"ready": true, "listen": listen, "host_count": host_count})
    );
    let server = axum::serve(listener, build_router(state));
    server
        .with_graceful_shutdown(wait_for_shutdown(shutdown))
        .await
        .context("Core HTTP server failed")?;
    runtime.shutdown().await;
    Ok(())
}

async fn wait_for_shutdown(shutdown: CancellationToken) {
    tokio::select! {
        _ = shutdown.cancelled() => {},
        _ = tokio::signal::ctrl_c() => {},
    }
}

fn validate_config(config: &DaemonConfig) -> anyhow::Result<()> {
    if !config.listen.ip().is_loopback() && !config.allow_non_loopback_listen {
        bail!(
            "non-loopback listening requires allow_non_loopback_listen=true and an externally restricted network"
        );
    }
    if config.admin_token.len() < 24 {
        bail!("admin_token must contain at least 24 bytes");
    }
    if config.clients.is_empty() || config.hosts.is_empty() || config.gateways.is_empty() {
        bail!("at least one client, Host, and gateway must be configured");
    }
    if !config
        .clients
        .iter()
        .any(|client| client.id == config.mcp_client_id)
    {
        bail!("mcp_client_id does not match a configured client");
    }
    let client_ids = config
        .clients
        .iter()
        .map(|client| client.id.as_str())
        .collect::<HashSet<_>>();
    let host_ids = config
        .hosts
        .iter()
        .map(|host| host.id.as_str())
        .collect::<HashSet<_>>();
    let gateway_ids = config
        .gateways
        .iter()
        .map(|gateway| gateway.id.as_str())
        .collect::<HashSet<_>>();
    if client_ids.len() != config.clients.len()
        || host_ids.len() != config.hosts.len()
        || gateway_ids.len() != config.gateways.len()
    {
        bail!("client, Host, and gateway IDs must be unique");
    }
    for client in &config.clients {
        if client.id.trim().is_empty() || client.token.len() < 24 {
            bail!("client IDs must not be empty and client tokens must contain at least 24 bytes");
        }
        for grant in &client.operation_grants {
            if grant.id.trim().is_empty() || grant.operations.is_empty() {
                bail!("client operation grants require an ID and at least one operation");
            }
        }
    }
    for gateway in &config.gateways {
        if !client_ids.contains(gateway.client_id.as_str())
            || gateway.registration_token.len() < 32
            || gateway.id.trim().is_empty()
            || gateway.account_id.trim().is_empty()
        {
            bail!(
                "gateway registration must reference a client and use non-empty IDs and a 32-byte token"
            );
        }
    }
    for host in &config.hosts {
        if host.id.trim().is_empty()
            || host.program.is_empty()
            || host.startup_timeout_ms == 0
            || host.shutdown_timeout_ms == 0
        {
            bail!("Host IDs, programs, and startup/shutdown deadlines must be valid");
        }
        for grant in &host.operation_grants {
            let registered = config.gateways.iter().any(|gateway| {
                gateway.account_id == grant.account_id && gateway.client_id == grant.client_id
            });
            if grant.id.trim().is_empty() || grant.operations.is_empty() || !registered {
                bail!("Host operation grant must have an ID, operations, and a registered account");
            }
        }
    }
    Ok(())
}

fn default_true() -> bool {
    true
}
fn default_max_restarts() -> u32 {
    3
}
fn default_restart_backoff_ms() -> u64 {
    500
}
fn default_startup_timeout_ms() -> u64 {
    15_000
}
fn default_shutdown_timeout_ms() -> u64 {
    10_000
}
fn default_conversation_limit() -> usize {
    32
}
fn default_global_limit() -> usize {
    1_024
}
fn default_tracked_conversation_limit() -> usize {
    4_096
}
fn default_concurrency() -> usize {
    32
}
fn default_event_timeout_ms() -> u64 {
    30_000
}

#[cfg(test)]
mod tests {
    use super::{DaemonConfig, validate_config};

    #[test]
    fn non_loopback_listen_requires_explicit_opt_in() {
        let token = "x".repeat(32);
        let mut config: DaemonConfig = serde_json::from_value(serde_json::json!({
            "listen": "0.0.0.0:26701",
            "database_path": "/tmp/ligamina.sqlite",
            "admin_token": token,
            "mcp_client_id": "client",
            "clients": [{"id": "client", "token": "client-token-long-enough-for-validation"}],
            "gateways": [{
                "id": "gateway",
                "client_id": "client",
                "account_id": "account",
                "registration_token": "gateway-registration-token-long-enough"
            }],
            "hosts": [{"id": "host", "program": "python"}]
        }))
        .expect("minimal valid daemon config should deserialize");

        assert!(validate_config(&config).is_err());
        config.allow_non_loopback_listen = true;
        assert!(validate_config(&config).is_ok());
    }
}
