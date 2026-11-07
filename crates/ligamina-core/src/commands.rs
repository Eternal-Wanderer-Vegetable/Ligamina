//! Native plugin commands exposed as tools (ADR 0002).
//!
//! Hosts report the commands registered by their framework plugins. Core keeps one
//! snapshot per Host generation, and runs a command inside an ephemeral virtual
//! private conversation. Platform operations a plugin emits for that conversation
//! are completed by [`ToolSessions`] inside Core and returned as the tool result;
//! they never reach the operation ledger or a customer Gateway.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::{RwLock, broadcast};

/// Account ID used by tool sessions on Hosts that accept string IDs (Koishi).
pub const TOOL_ACCOUNT_ID: &str = "ligamina-tool";
/// Account ID used by tool sessions on OneBot v11 Hosts, which need numeric IDs.
/// Real QQ/OneBot accounts are never 0.
pub const TOOL_ONEBOT_ACCOUNT_ID: &str = "0";
/// Platform name for tool sessions of Hosts that accept arbitrary platforms.
pub const TOOL_PLATFORM: &str = "ligamina.tool";
/// Platform of NoneBot's locked OneBot v11 Bridge Adapter.
pub const ONEBOT_PLATFORM: &str = "onebot.v11";

/// Whether an account is reserved for tool sessions. No grant or Gateway may use one.
pub fn is_tool_account(account_id: &str) -> bool {
    account_id == TOOL_ACCOUNT_ID || account_id == TOOL_ONEBOT_ACCOUNT_ID
}

/// Identity of one tool call's virtual private conversation, shaped for the Host framework.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolIdentity {
    pub platform: &'static str,
    pub account_id: &'static str,
    pub actor_id: String,
    pub message_id: String,
}

/// Builds a fresh identity for a framework, or `None` when tool sessions are unsupported.
pub fn tool_identity(framework: &str, call: uuid::Uuid) -> Option<ToolIdentity> {
    match framework {
        "koishi" => {
            let id = call.simple().to_string();
            Some(ToolIdentity {
                platform: TOOL_PLATFORM,
                account_id: TOOL_ACCOUNT_ID,
                actor_id: format!("tool-actor-{id}"),
                message_id: format!("tool-message-{id}"),
            })
        }
        "nonebot" => {
            // OneBot v11 IDs are non-negative i64 values; keep them large and distinct.
            let bits = call.as_u128();
            let actor = 1_000_000_000_000_000 + (bits as u64 % 4_000_000_000_000_000);
            let message = 5_000_000_000_000_000 + ((bits >> 64) as u64 % 4_000_000_000_000_000);
            Some(ToolIdentity {
                platform: ONEBOT_PLATFORM,
                account_id: TOOL_ONEBOT_ACCOUNT_ID,
                actor_id: actor.to_string(),
                message_id: message.to_string(),
            })
        }
        _ => None,
    }
}
/// Error code returned for operations a tool session cannot perform.
pub const UNSUPPORTED_IN_TOOL_SESSION: &str = "unsupported_in_tool_session";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommandDescriptor {
    pub command_id: String,
    pub invocation: String,
    pub framework: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub aliases: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub arguments: Vec<Value>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub options: Vec<Value>,
}

type HostCommands = HashMap<String, (u64, Vec<CommandDescriptor>)>;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CommandInfo {
    pub host_id: String,
    pub host_epoch: u64,
    pub descriptor: CommandDescriptor,
}

/// Commands of active Host generations. A new generation replaces the old snapshot.
#[derive(Clone)]
pub struct CommandCatalog {
    hosts: Arc<RwLock<HostCommands>>,
    changes: broadcast::Sender<()>,
}

impl Default for CommandCatalog {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandCatalog {
    pub fn new() -> Self {
        let (changes, _) = broadcast::channel(16);
        Self {
            hosts: Arc::new(RwLock::new(HashMap::new())),
            changes,
        }
    }

    /// Replaces the Host's commands with a validated snapshot. Invalid or duplicate
    /// entries are skipped and returned as rejection reasons.
    pub async fn register_snapshot(
        &self,
        host_id: &str,
        epoch: u64,
        snapshot: &[Value],
    ) -> Vec<String> {
        let mut rejected = Vec::new();
        let mut seen = HashSet::new();
        let mut descriptors = Vec::new();
        for value in snapshot {
            if let Err(error) = ligamina_contracts::validate_command_descriptor(value) {
                rejected.push(format!("{}: {error}", command_label(value)));
                continue;
            }
            let descriptor = match serde_json::from_value::<CommandDescriptor>(value.clone()) {
                Ok(descriptor) => descriptor,
                Err(error) => {
                    rejected.push(format!("{}: {error}", command_label(value)));
                    continue;
                }
            };
            if !seen.insert(descriptor.command_id.clone()) {
                rejected.push(format!("{}: duplicate command_id", descriptor.command_id));
                continue;
            }
            descriptors.push(descriptor);
        }
        let changed = {
            let mut hosts = self.hosts.write().await;
            let previous = hosts.insert(host_id.to_owned(), (epoch, descriptors.clone()));
            previous.map(|(_, old)| old) != Some(descriptors)
        };
        if changed {
            let _ = self.changes.send(());
        }
        rejected
    }

    /// Removes a Host generation; a newer generation's snapshot is left untouched.
    pub async fn deactivate_host(&self, host_id: &str, epoch: u64) {
        let removed = {
            let mut hosts = self.hosts.write().await;
            if hosts
                .get(host_id)
                .is_some_and(|(current, _)| *current == epoch)
            {
                hosts
                    .remove(host_id)
                    .is_some_and(|(_, list)| !list.is_empty())
            } else {
                false
            }
        };
        if removed {
            let _ = self.changes.send(());
        }
    }

    pub async fn list(&self) -> Vec<CommandInfo> {
        let hosts = self.hosts.read().await;
        let mut list = hosts
            .iter()
            .flat_map(|(host_id, (epoch, commands))| {
                commands.iter().map(|descriptor| CommandInfo {
                    host_id: host_id.clone(),
                    host_epoch: *epoch,
                    descriptor: descriptor.clone(),
                })
            })
            .collect::<Vec<_>>();
        list.sort_by(|left, right| {
            (&left.host_id, &left.descriptor.command_id)
                .cmp(&(&right.host_id, &right.descriptor.command_id))
        });
        list
    }

    pub async fn get(&self, host_id: &str, command_id: &str) -> Option<CommandInfo> {
        let hosts = self.hosts.read().await;
        let (epoch, commands) = hosts.get(host_id)?;
        commands
            .iter()
            .find(|descriptor| descriptor.command_id == command_id)
            .map(|descriptor| CommandInfo {
                host_id: host_id.to_owned(),
                host_epoch: *epoch,
                descriptor: descriptor.clone(),
            })
    }

    pub fn subscribe_changes(&self) -> broadcast::Receiver<()> {
        self.changes.subscribe()
    }
}

fn command_label(value: &Value) -> String {
    value
        .get("command_id")
        .and_then(Value::as_str)
        .unwrap_or("<unnamed command>")
        .to_owned()
}

/// The virtual conversation of one tool call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolScope {
    pub host_id: String,
    pub host_epoch: u64,
    pub trace_id: String,
    pub account_id: String,
    pub conversation_kind: String,
    pub conversation_id: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ToolTranscript {
    /// `send_message` payloads in emission order.
    pub messages: Vec<Value>,
    /// Operations refused inside the tool session, with their reason.
    pub refused: Vec<Value>,
}

#[derive(Default)]
struct SessionState {
    scope: Option<ToolScope>,
    transcript: ToolTranscript,
}

/// Open tool calls keyed by their Core-generated trace ID.
#[derive(Clone, Default)]
pub struct ToolSessions {
    sessions: Arc<std::sync::Mutex<HashMap<String, SessionState>>>,
    closed_refusals: Arc<std::sync::atomic::AtomicU64>,
    message_counter: Arc<std::sync::atomic::AtomicU64>,
}

impl ToolSessions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn open(&self, scope: ToolScope) {
        let mut sessions = self.sessions.lock().expect("tool session lock");
        sessions.insert(
            scope.trace_id.clone(),
            SessionState {
                scope: Some(scope),
                transcript: ToolTranscript::default(),
            },
        );
    }

    /// Number of tool-account operations refused because their session had already
    /// closed (for example a reply sent after the command deadline).
    pub fn closed_refusals(&self) -> u64 {
        self.closed_refusals
            .load(std::sync::atomic::Ordering::Relaxed)
    }

    /// Closes the session and returns everything it collected.
    pub fn close(&self, trace_id: &str) -> ToolTranscript {
        self.sessions
            .lock()
            .expect("tool session lock")
            .remove(trace_id)
            .map(|state| state.transcript)
            .unwrap_or_default()
    }

    /// Completes a Host `platform.execute` request when it belongs to a tool session.
    ///
    /// Returns `None` for ordinary operations, which keep the normal grant, ledger and
    /// Gateway path. Operations on the tool account, or carrying an open tool trace,
    /// never leave Core: matching sends are collected, anything else is refused.
    pub fn intercept(&self, host_id: &str, host_epoch: u64, operation: &Value) -> Option<Value> {
        let trace_id = operation.get("trace_id").and_then(Value::as_str);
        let account_id = operation.get("account_id").and_then(Value::as_str);
        let mut sessions = self.sessions.lock().expect("tool session lock");
        let state = trace_id.and_then(|trace| sessions.get_mut(trace));
        let Some(state) = state else {
            if !account_id.is_some_and(is_tool_account) {
                return None;
            }
            self.closed_refusals
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            return Some(refused("tool session is closed or unknown"));
        };
        let scope = state.scope.clone().expect("open sessions have a scope");
        let name = operation
            .get("operation")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let target = operation.get("target");
        let in_scope = scope.host_id == host_id
            && scope.host_epoch == host_epoch
            && account_id == Some(scope.account_id.as_str())
            && target.and_then(|t| t.get("kind")).and_then(Value::as_str)
                == Some(scope.conversation_kind.as_str())
            && target.and_then(|t| t.get("id")).and_then(Value::as_str)
                == Some(scope.conversation_id.as_str());

        let result = match name {
            "send_message" if in_scope => {
                let message = operation
                    .get("params")
                    .and_then(|params| params.get("message"))
                    .cloned()
                    .unwrap_or_else(|| json!([]));
                state.transcript.messages.push(message);
                // Numeric so OneBot v11 Hosts can parse it; unique within this Core.
                let sequence = self
                    .message_counter
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                json!({
                    "status": "succeeded",
                    "platform_message_id": (9_000_000_000_000_000_u64 + sequence).to_string(),
                })
            }
            "get_self_info" if account_id == Some(scope.account_id.as_str()) => json!({
                "status": "succeeded",
                "data": { "user_id": scope.account_id, "nickname": "Ligamina tool" },
            }),
            _ => {
                let reason = if in_scope || name == "get_self_info" {
                    format!("{name} needs real platform data")
                } else {
                    format!("{name} targets a conversation outside the tool session")
                };
                state.transcript.refused.push(json!({
                    "operation": name,
                    "target": target.cloned().unwrap_or(Value::Null),
                    "reason": reason,
                }));
                refused(&reason)
            }
        };
        Some(result)
    }
}

fn refused(message: &str) -> Value {
    json!({
        "status": "failed",
        "error": { "code": UNSUPPORTED_IN_TOOL_SESSION, "message": message },
    })
}

/// Concatenated plain text of collected `send_message` payloads.
pub fn transcript_text(transcript: &ToolTranscript) -> String {
    transcript
        .messages
        .iter()
        .map(|message| {
            message
                .as_array()
                .map(|segments| {
                    segments
                        .iter()
                        .map(
                            |segment| match segment.get("type").and_then(Value::as_str) {
                                Some("text") => segment
                                    .get("text")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_owned(),
                                Some("mention") => format!(
                                    "@{}",
                                    segment
                                        .get("user_id")
                                        .and_then(Value::as_str)
                                        .unwrap_or("?")
                                ),
                                Some("image_ref") => format!(
                                    "[image: {}]",
                                    segment
                                        .get("resource_ref")
                                        .and_then(Value::as_str)
                                        .unwrap_or("?")
                                ),
                                _ => String::new(),
                            },
                        )
                        .collect::<String>()
                })
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope() -> ToolScope {
        ToolScope {
            host_id: "koishi".into(),
            host_epoch: 3,
            trace_id: "trace-1".into(),
            account_id: TOOL_ACCOUNT_ID.into(),
            conversation_kind: "private".into(),
            conversation_id: "actor-1".into(),
        }
    }

    fn send(trace: &str, account: &str, target_id: &str, text: &str) -> Value {
        json!({
            "operation_id": "op", "trace_id": trace, "account_id": account,
            "operation": "send_message",
            "target": { "kind": "private", "id": target_id },
            "params": { "message": [{ "type": "text", "text": text }] },
        })
    }

    #[test]
    fn collects_in_scope_sends_and_refuses_everything_else() {
        let sessions = ToolSessions::new();
        sessions.open(scope());
        let ok = sessions
            .intercept(
                "koishi",
                3,
                &send("trace-1", TOOL_ACCOUNT_ID, "actor-1", "hi"),
            )
            .unwrap();
        assert_eq!(ok["status"], "succeeded");
        assert_eq!(ok["platform_message_id"], "9000000000000000");

        let other_target = sessions
            .intercept(
                "koishi",
                3,
                &send("trace-1", TOOL_ACCOUNT_ID, "someone-else", "x"),
            )
            .unwrap();
        assert_eq!(other_target["status"], "failed");
        assert_eq!(other_target["error"]["code"], UNSUPPORTED_IN_TOOL_SESSION);

        let stale_epoch = sessions
            .intercept(
                "koishi",
                2,
                &send("trace-1", TOOL_ACCOUNT_ID, "actor-1", "x"),
            )
            .unwrap();
        assert_eq!(stale_epoch["status"], "failed");

        let read = sessions
            .intercept(
                "koishi",
                3,
                &json!({
                    "operation_id": "op2", "trace_id": "trace-1", "account_id": TOOL_ACCOUNT_ID,
                    "operation": "get_message", "target": { "kind": "private", "id": "actor-1" },
                    "params": { "message_id": "1" },
                }),
            )
            .unwrap();
        assert_eq!(read["status"], "failed");

        let transcript = sessions.close("trace-1");
        assert_eq!(transcript.messages.len(), 1);
        assert_eq!(transcript.refused.len(), 3);
        assert_eq!(transcript_text(&transcript), "hi");
    }

    #[test]
    fn ordinary_operations_pass_through_and_late_tool_sends_are_refused() {
        let sessions = ToolSessions::new();
        assert!(
            sessions
                .intercept("koishi", 3, &send("real-trace", "10001", "42", "hi"))
                .is_none()
        );
        let late = sessions
            .intercept(
                "koishi",
                3,
                &send("trace-closed", TOOL_ACCOUNT_ID, "actor-1", "late"),
            )
            .unwrap();
        assert_eq!(late["status"], "failed");
        assert_eq!(sessions.closed_refusals(), 1);
        // A real account reusing an open tool trace still cannot reach the ledger.
        sessions.open(scope());
        let hijack = sessions
            .intercept("koishi", 3, &send("trace-1", "10001", "42", "x"))
            .unwrap();
        assert_eq!(hijack["status"], "failed");
        assert!(sessions.close("trace-1").messages.is_empty());
    }

    #[test]
    fn tool_identities_fit_each_framework() {
        let call = uuid::Uuid::new_v4();
        let koishi = tool_identity("koishi", call).unwrap();
        assert_eq!(koishi.platform, TOOL_PLATFORM);
        assert!(is_tool_account(koishi.account_id));
        let nonebot = tool_identity("nonebot", call).unwrap();
        assert_eq!(nonebot.platform, ONEBOT_PLATFORM);
        assert!(is_tool_account(nonebot.account_id));
        for id in [&nonebot.actor_id, &nonebot.message_id] {
            let value: u64 = id.parse().expect("numeric OneBot ID");
            assert!(value <= i64::MAX as u64);
        }
        assert_ne!(nonebot.actor_id, nonebot.message_id);
        assert!(tool_identity("astrbot", call).is_none());
        assert!(!is_tool_account("10001"));
    }

    #[tokio::test]
    async fn catalog_replaces_snapshots_per_generation_and_rejects_invalid_entries() {
        let catalog = CommandCatalog::new();
        let mut changes = catalog.subscribe_changes();
        let rejected = catalog
            .register_snapshot(
                "koishi",
                1,
                &[
                    json!({ "command_id": "echo", "invocation": "echo", "framework": "koishi" }),
                    json!({ "command_id": "echo", "invocation": "echo", "framework": "koishi" }),
                    json!({ "command_id": "bad", "invocation": "bad", "framework": "other" }),
                ],
            )
            .await;
        assert_eq!(rejected.len(), 2);
        assert!(changes.try_recv().is_ok());
        assert_eq!(catalog.list().await.len(), 1);
        assert_eq!(catalog.get("koishi", "echo").await.unwrap().host_epoch, 1);

        catalog
            .register_snapshot(
                "koishi",
                2,
                &[json!({ "command_id": "roll", "invocation": "roll", "framework": "koishi" })],
            )
            .await;
        assert!(catalog.get("koishi", "echo").await.is_none());
        catalog.deactivate_host("koishi", 1).await;
        assert!(
            catalog.get("koishi", "roll").await.is_some(),
            "old epoch must not remove newer snapshot"
        );
        catalog.deactivate_host("koishi", 2).await;
        assert!(catalog.list().await.is_empty());
    }
}
