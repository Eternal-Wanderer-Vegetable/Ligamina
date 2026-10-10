//! SQLite-backed grants, gateway leases, and platform operation ledger.

mod migrations;

use std::{
    path::Path,
    str::FromStr,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use ligamina_contracts::{
    ContractError, OperationSupport, classify_operation_support, validate_event, validate_operation,
};
use rusqlite::{Connection, OptionalExtension, Row, Transaction, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::task;
use uuid::Uuid;

use crate::policy::{Grant, OperationTarget, PolicyError, Principal, authorize_operation};

use self::migrations::CURRENT_SCHEMA_VERSION;

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("SQLite operation failed: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("contract validation failed: {0}")]
    Contract(#[from] ContractError),
    #[error("authorization denied: {0}")]
    Policy(#[from] PolicyError),
    #[error("blocking storage task failed: {0}")]
    Worker(String),
    #[error("storage connection lock was poisoned")]
    Poisoned,
    #[error("invalid storage input: {0}")]
    InvalidInput(String),
    #[error("platform operation is unsupported: {0}")]
    UnsupportedOperation(String),
    #[error("no active gateway lease is available for this account")]
    GatewayUnavailable,
    #[error("gateway lease is invalid, expired, revoked, or from a previous Core epoch")]
    StaleLease,
    #[error("operation ID was reused with a different payload")]
    OperationConflict,
    #[error("operation was not found in this customer/account scope")]
    OperationNotFound,
    #[error("event was not found in this client scope")]
    EventNotFound,
    #[error("operation result is not valid for the operation's current state")]
    InvalidResultTransition,
    #[error("event ID was reused with a different payload")]
    EventConflict,
    #[error("event is not in a state that permits this transition")]
    InvalidEventTransition,
    #[error("persisted data could not be decoded: {0}")]
    CorruptData(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OperationStatus {
    Pending,
    Dispatched,
    Succeeded,
    Failed,
    Unknown,
}

impl OperationStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Dispatched => "dispatched",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Unknown => "unknown",
        }
    }
}

impl FromStr for OperationStatus {
    type Err = StoreError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "pending" => Ok(Self::Pending),
            "dispatched" => Ok(Self::Dispatched),
            "succeeded" => Ok(Self::Succeeded),
            "failed" => Ok(Self::Failed),
            "unknown" => Ok(Self::Unknown),
            other => Err(StoreError::CorruptData(format!(
                "unknown operation status: {other}"
            ))),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrepareDisposition {
    Created,
    Idempotent,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperationKey {
    pub client_id: String,
    pub account_id: String,
    pub operation: String,
    pub operation_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct EventKey {
    pub client_id: String,
    pub platform: String,
    pub account_id: String,
    pub conversation_kind: String,
    pub conversation_id: String,
    pub event_id: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventStatus {
    Accepted,
    Processing,
    Completed,
    Failed,
    Unknown,
}

impl EventStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Processing => "processing",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Unknown => "unknown",
        }
    }
}

impl FromStr for EventStatus {
    type Err = StoreError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "accepted" => Ok(Self::Accepted),
            "processing" => Ok(Self::Processing),
            "completed" => Ok(Self::Completed),
            "failed" => Ok(Self::Failed),
            "unknown" => Ok(Self::Unknown),
            other => Err(StoreError::CorruptData(format!(
                "unknown event status: {other}"
            ))),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventDisposition {
    Accepted,
    Duplicate,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EventRecord {
    pub key: EventKey,
    pub fingerprint: String,
    pub trace_id: String,
    pub target_host: String,
    pub event: Value,
    pub status: EventStatus,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationRecord {
    pub key: OperationKey,
    pub fingerprint: String,
    pub trace_id: String,
    pub grant_id: String,
    pub principal: Principal,
    pub host_epoch: u64,
    pub core_epoch_created: String,
    pub operation_payload: Value,
    pub status: OperationStatus,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub dispatched_lease_id: Option<String>,
    pub dispatched_lease_epoch: Option<u64>,
    pub dispatched_core_epoch: Option<String>,
    pub result: Option<Value>,
}

#[derive(Clone, Debug)]
pub struct OperationSubmission {
    pub grant_id: String,
    pub principal: Principal,
    pub host_epoch: u64,
    pub operation: Value,
    pub now_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GatewayRegistration {
    pub gateway_id: String,
    pub client_id: String,
    pub account_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeaseCredential {
    pub lease_id: String,
    /// Returned once to the gateway. Only its SHA-256 digest is persisted.
    pub token: String,
    pub lease_epoch: u64,
    pub core_epoch: String,
    pub expires_at_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GatewayResultStatus {
    Succeeded,
    Failed,
    Unknown,
}

impl GatewayResultStatus {
    fn as_operation_status(&self) -> OperationStatus {
        match self {
            Self::Succeeded => OperationStatus::Succeeded,
            Self::Failed => OperationStatus::Failed,
            Self::Unknown => OperationStatus::Unknown,
        }
    }

    fn as_str(&self) -> &'static str {
        self.as_operation_status().as_str()
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GatewayOperationResult {
    pub key: OperationKey,
    pub fingerprint: String,
    pub status: GatewayResultStatus,
    pub platform_message_id: Option<String>,
    /// Structured value returned by read-only platform operations such as `get_message`.
    pub data: Option<Value>,
    pub error: Option<String>,
    pub observed_at_ms: i64,
    pub evidence_ref: Option<String>,
}

#[derive(Clone)]
pub struct Store {
    connection: Arc<Mutex<Connection>>,
    core_epoch: String,
}

impl Store {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        Self::from_connection(Connection::open(path)?)
    }

    pub fn open_in_memory() -> Result<Self, StoreError> {
        Self::from_connection(Connection::open_in_memory()?)
    }

    fn from_connection(mut connection: Connection) -> Result<Self, StoreError> {
        connection.busy_timeout(Duration::from_secs(5))?;
        connection.execute_batch(
            "PRAGMA foreign_keys = ON;
             PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;",
        )?;
        migrations::migrate(&mut connection)?;
        let core_epoch = Uuid::new_v4().to_string();
        begin_core_epoch(&mut connection, &core_epoch, now_ms())?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
            core_epoch,
        })
    }

    pub fn core_epoch(&self) -> &str {
        &self.core_epoch
    }

    pub async fn schema_version(&self) -> Result<i64, StoreError> {
        self.run(|connection| {
            let version: i64 = connection
                .query_row("SELECT MAX(version) FROM schema_migrations", [], |row| {
                    row.get(0)
                })
                .map_err(StoreError::from)?;
            if version != CURRENT_SCHEMA_VERSION {
                return Err(StoreError::CorruptData(format!(
                    "unsupported schema version {version}; expected {CURRENT_SCHEMA_VERSION}"
                )));
            }
            Ok(version)
        })
        .await
    }

    /// Durably accept an event once per client/platform/account/conversation/event ID scope.
    pub async fn accept_event(
        &self,
        client_id: String,
        event: Value,
        now_ms: i64,
    ) -> Result<(EventDisposition, EventRecord), StoreError> {
        validate_event(&event)?;
        validate_id("client_id", &client_id)?;
        let key = event_key(&client_id, &event)?;
        let fingerprint = event_fingerprint(&event)?;
        let trace_id = required_string(&event, "trace_id")?;
        let target_host = required_string(&event, "target_host")?;
        let encoded = serde_json::to_string(&event)
            .map_err(|error| StoreError::CorruptData(error.to_string()))?;
        self.run(move |connection| {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            if let Some(existing) = read_event(&tx, &key)? {
                if existing.fingerprint != fingerprint {
                    return Err(StoreError::EventConflict);
                }
                tx.commit()?;
                return Ok((EventDisposition::Duplicate, existing));
            }
            tx.execute(
                "INSERT INTO event_receipts(
                    client_id, platform, account_id, conversation_kind, conversation_id,
                    event_id, fingerprint, trace_id, target_host, event_json, status,
                    created_at_ms, updated_at_ms
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'accepted', ?11, ?11)",
                rusqlite::params![
                    key.client_id,
                    key.platform,
                    key.account_id,
                    key.conversation_kind,
                    key.conversation_id,
                    key.event_id,
                    fingerprint,
                    trace_id,
                    target_host,
                    encoded,
                    now_ms,
                ],
            )?;
            let record = read_event(&tx, &key)?.ok_or_else(|| {
                StoreError::CorruptData("inserted event receipt disappeared".to_owned())
            })?;
            tx.commit()?;
            Ok((EventDisposition::Accepted, record))
        })
        .await
    }

    /// Read a stored event receipt. API callers must authorize access before exposing it.
    pub async fn get_event(&self, key: EventKey) -> Result<EventRecord, StoreError> {
        self.run(move |connection| read_event(connection, &key)?.ok_or(StoreError::EventNotFound))
            .await
    }

    pub(crate) async fn find_event(
        &self,
        key: EventKey,
    ) -> Result<Option<EventRecord>, StoreError> {
        self.run(move |connection| read_event(connection, &key))
            .await
    }

    pub(crate) async fn transition_event(
        &self,
        key: EventKey,
        next: EventStatus,
        error: Option<String>,
        now_ms: i64,
    ) -> Result<EventRecord, StoreError> {
        self.run(move |connection| {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let existing = read_event(&tx, &key)?.ok_or(StoreError::EventNotFound)?;
            if existing.status == next {
                tx.commit()?;
                return Ok(existing);
            }
            let permitted = matches!(
                (existing.status, next),
                (EventStatus::Accepted, EventStatus::Processing)
                    | (EventStatus::Accepted, EventStatus::Failed)
                    | (EventStatus::Accepted, EventStatus::Unknown)
                    | (EventStatus::Processing, EventStatus::Completed)
                    | (EventStatus::Processing, EventStatus::Failed)
                    | (EventStatus::Processing, EventStatus::Unknown)
            );
            if !permitted {
                return Err(StoreError::InvalidEventTransition);
            }
            tx.execute(
                "UPDATE event_receipts SET status = ?1, error = ?2, updated_at_ms = ?3
                 WHERE client_id = ?4 AND platform = ?5 AND account_id = ?6
                   AND conversation_kind = ?7 AND conversation_id = ?8 AND event_id = ?9",
                rusqlite::params![
                    next.as_str(),
                    error,
                    now_ms,
                    key.client_id,
                    key.platform,
                    key.account_id,
                    key.conversation_kind,
                    key.conversation_id,
                    key.event_id,
                ],
            )?;
            let updated = read_event(&tx, &key)?.ok_or_else(|| {
                StoreError::CorruptData("transitioned event receipt disappeared".to_owned())
            })?;
            tx.commit()?;
            Ok(updated)
        })
        .await
    }

    pub async fn put_grant(&self, grant: Grant) -> Result<(), StoreError> {
        validate_id("grant_id", &grant.id)?;
        validate_id("client_id", &grant.client_id)?;
        validate_id("account_id", &grant.account_id)?;
        let (principal_kind, principal_id) = grant.principal.kind_and_id();
        let principal_kind = principal_kind.to_owned();
        let principal_id = principal_id.to_owned();
        validate_id("principal_id", &principal_id)?;
        let operations_json = serde_json::to_string(&grant.operations)
            .map_err(|error| StoreError::CorruptData(error.to_string()))?;
        let targets_json = grant
            .targets
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|error| StoreError::CorruptData(error.to_string()))?;
        self.run(move |connection| {
            connection.execute(
                "INSERT INTO grants(
                    grant_id, client_id, principal_kind, principal_id, account_id,
                    operations_json, targets_json, active
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                ON CONFLICT(grant_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    principal_kind = excluded.principal_kind,
                    principal_id = excluded.principal_id,
                    account_id = excluded.account_id,
                    operations_json = excluded.operations_json,
                    targets_json = excluded.targets_json,
                    active = excluded.active",
                params![
                    grant.id,
                    grant.client_id,
                    principal_kind,
                    principal_id,
                    grant.account_id,
                    operations_json,
                    targets_json,
                    i64::from(grant.active),
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn revoke_grant(&self, grant_id: String) -> Result<bool, StoreError> {
        self.run(move |connection| {
            let changed = connection.execute(
                "UPDATE grants SET active = 0 WHERE grant_id = ?1 AND active = 1",
                [grant_id],
            )?;
            Ok(changed > 0)
        })
        .await
    }

    /// Register a trusted customer gateway. The long-lived credential is never stored as text.
    pub async fn register_gateway(
        &self,
        registration: GatewayRegistration,
        registration_token: String,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        validate_id("gateway_id", &registration.gateway_id)?;
        validate_id("client_id", &registration.client_id)?;
        validate_id("account_id", &registration.account_id)?;
        if registration_token.len() < 32 {
            return Err(StoreError::InvalidInput(
                "gateway registration token must contain at least 32 bytes".to_owned(),
            ));
        }
        let credential_hash = hash_secret(&registration_token);
        self.run(move |connection| {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let existing: Option<(String, String, Vec<u8>)> = tx
                .query_row(
                    "SELECT client_id, account_id, credential_hash
                     FROM gateway_registrations WHERE gateway_id = ?1",
                    [&registration.gateway_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()?;
            if let Some((client_id, account_id, existing_hash)) = existing {
                if client_id != registration.client_id
                    || account_id != registration.account_id
                    || !constant_time_eq(&existing_hash, &credential_hash)
                {
                    let lease_ids = active_lease_ids_for_gateway(&tx, &registration.gateway_id)?;
                    for lease_id in lease_ids {
                        revoke_lease_tx(&tx, &lease_id, now_ms, "gateway_registration_changed")?;
                    }
                }
            }
            tx.execute(
                "INSERT INTO gateway_registrations(gateway_id, client_id, account_id, credential_hash)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(gateway_id) DO UPDATE SET
                    client_id = excluded.client_id,
                    account_id = excluded.account_id,
                    credential_hash = excluded.credential_hash",
                params![
                    registration.gateway_id,
                    registration.client_id,
                    registration.account_id,
                    credential_hash,
                ],
            )?;
            tx.commit()?;
            Ok(())
        })
        .await
    }

    /// Replace the account's single active gateway lease and return a fresh opaque token.
    pub async fn acquire_lease(
        &self,
        gateway_id: String,
        registration_token: String,
        ttl: Duration,
        now_ms: i64,
    ) -> Result<LeaseCredential, StoreError> {
        validate_id("gateway_id", &gateway_id)?;
        let ttl_ms = i64::try_from(ttl.as_millis())
            .map_err(|_| StoreError::InvalidInput("lease TTL is too large".to_owned()))?;
        if ttl_ms <= 0 {
            return Err(StoreError::InvalidInput(
                "lease TTL must be greater than zero".to_owned(),
            ));
        }
        let expires_at_ms = now_ms
            .checked_add(ttl_ms)
            .ok_or_else(|| StoreError::InvalidInput("lease expiration overflowed".to_owned()))?;
        let credential_hash = hash_secret(&registration_token);
        let core_epoch = self.core_epoch.clone();
        self.run(move |connection| {
            reap_expired(connection, &core_epoch, now_ms)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let registration: Option<(String, String, Vec<u8>)> = tx
                .query_row(
                    "SELECT client_id, account_id, credential_hash
                     FROM gateway_registrations WHERE gateway_id = ?1",
                    [&gateway_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()?;
            let Some((client_id, account_id, expected_hash)) = registration else {
                return Err(StoreError::StaleLease);
            };
            if !constant_time_eq(&expected_hash, &credential_hash) {
                return Err(StoreError::StaleLease);
            }
            let current_epoch = read_core_epoch(&tx)?;
            let old_lease: Option<String> = tx
                .query_row(
                    "SELECT lease_id FROM gateway_leases
                     WHERE client_id = ?1 AND account_id = ?2 AND active = 1",
                    params![client_id, account_id],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(old_lease) = old_lease {
                revoke_lease_tx(&tx, &old_lease, now_ms, "lease_replaced")?;
            }
            let lease_epoch: i64 = tx.query_row(
                "SELECT COALESCE(MAX(lease_epoch), 0) + 1
                 FROM gateway_leases WHERE client_id = ?1 AND account_id = ?2",
                params![client_id, account_id],
                |row| row.get(0),
            )?;
            let lease_id = Uuid::new_v4().to_string();
            let token = format!("{}{}", Uuid::new_v4(), Uuid::new_v4());
            let token_hash = hash_secret(&token);
            tx.execute(
                "INSERT INTO gateway_leases(
                    lease_id, gateway_id, client_id, account_id, lease_epoch,
                    core_epoch, token_hash, expires_at_ms, active
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1)",
                params![
                    lease_id,
                    gateway_id,
                    client_id,
                    account_id,
                    lease_epoch,
                    current_epoch,
                    token_hash,
                    expires_at_ms,
                ],
            )?;
            tx.commit()?;
            Ok(LeaseCredential {
                lease_id,
                token,
                lease_epoch: u64::try_from(lease_epoch).map_err(|_| {
                    StoreError::CorruptData("negative gateway lease epoch".to_owned())
                })?,
                core_epoch: current_epoch,
                expires_at_ms,
            })
        })
        .await
    }

    /// Persist a side effect before it can be claimed by a customer gateway.
    pub async fn prepare_operation(
        &self,
        submission: OperationSubmission,
    ) -> Result<(PrepareDisposition, OperationRecord), StoreError> {
        validate_operation(&submission.operation)?;
        let Some(operation_name) = submission
            .operation
            .get("operation")
            .and_then(Value::as_str)
        else {
            return Err(StoreError::InvalidInput(
                "operation name is missing".to_owned(),
            ));
        };
        let operation_name = operation_name.to_owned();
        if classify_operation_support(&operation_name) == OperationSupport::Unsupported {
            return Err(StoreError::UnsupportedOperation(operation_name));
        }
        let target = operation_target(&submission.operation)?;
        let account_id = required_string(&submission.operation, "account_id")?;
        let operation_id = required_string(&submission.operation, "operation_id")?;
        let trace_id = required_string(&submission.operation, "trace_id")?;
        let payload = submission.operation.clone();
        let fingerprint = operation_fingerprint(&payload)?;
        let grant_id = submission.grant_id.clone();
        let principal = submission.principal.clone();
        let host_epoch = i64::try_from(submission.host_epoch).map_err(|_| {
            StoreError::InvalidInput("host epoch exceeds the SQLite integer range".to_owned())
        })?;
        let now = submission.now_ms;
        let core_epoch = self.core_epoch.clone();
        self.run(move |connection| {
            reap_expired(connection, &core_epoch, now)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let grant = get_grant_tx(&tx, &grant_id)?.ok_or(PolicyError::InactiveGrant)?;
            authorize_operation(&grant, &principal, &account_id, &operation_name, &target)?;
            let key = OperationKey {
                client_id: grant.client_id.clone(),
                account_id: account_id.clone(),
                operation: operation_name.clone(),
                operation_id: operation_id.clone(),
            };
            if let Some(existing) = read_operation(&tx, &key)? {
                if existing.fingerprint != fingerprint {
                    return Err(StoreError::OperationConflict);
                }
                tx.commit()?;
                return Ok((PrepareDisposition::Idempotent, existing));
            }
            let lease_available: bool = tx.query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM gateway_leases
                    WHERE client_id = ?1 AND account_id = ?2 AND active = 1
                      AND core_epoch = ?3 AND expires_at_ms > ?4
                )",
                params![grant.client_id, account_id, core_epoch, now],
                |row| row.get::<_, i64>(0).map(|value| value != 0),
            )?;
            if !lease_available {
                return Err(StoreError::GatewayUnavailable);
            }
            let (principal_kind, principal_id) = principal.kind_and_id();
            let payload_json = serde_json::to_string(&payload)
                .map_err(|error| StoreError::CorruptData(error.to_string()))?;
            tx.execute(
                "INSERT INTO operation_ledger(
                    client_id, account_id, operation, operation_id, fingerprint, trace_id,
                    grant_id, principal_kind, principal_id, host_epoch, core_epoch_created,
                    operation_json, status, created_at_ms, updated_at_ms
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, 'pending', ?13, ?13)",
                params![
                    grant.client_id,
                    account_id,
                    operation_name,
                    operation_id,
                    fingerprint,
                    trace_id,
                    grant_id,
                    principal_kind,
                    principal_id,
                    host_epoch,
                    core_epoch,
                    payload_json,
                    now,
                ],
            )?;
            let record = read_operation(&tx, &key)?.ok_or_else(|| {
                StoreError::CorruptData("inserted operation disappeared".to_owned())
            })?;
            tx.commit()?;
            Ok((PrepareDisposition::Created, record))
        })
        .await
    }

    /// Claim one pending operation and durably mark it dispatched before returning it.
    pub async fn claim_pending(
        &self,
        credential: LeaseCredential,
        now_ms: i64,
    ) -> Result<Option<OperationRecord>, StoreError> {
        let core_epoch = self.core_epoch.clone();
        self.run(move |connection| {
            reap_expired(connection, &core_epoch, now_ms)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let lease = validate_lease_tx(&tx, &credential, now_ms, &core_epoch)?;
            let lease_epoch = i64::try_from(lease.lease_epoch).map_err(|_| {
                StoreError::CorruptData("lease epoch exceeds SQLite range".to_owned())
            })?;
            let key: Option<OperationKey> = tx
                .query_row(
                    "SELECT client_id, account_id, operation, operation_id
                     FROM operation_ledger
                     WHERE client_id = ?1 AND account_id = ?2 AND status = 'pending'
                     ORDER BY created_at_ms, operation_id LIMIT 1",
                    params![lease.client_id, lease.account_id],
                    |row| {
                        Ok(OperationKey {
                            client_id: row.get(0)?,
                            account_id: row.get(1)?,
                            operation: row.get(2)?,
                            operation_id: row.get(3)?,
                        })
                    },
                )
                .optional()?;
            let Some(key) = key else {
                tx.commit()?;
                return Ok(None);
            };
            let changed = tx.execute(
                "UPDATE operation_ledger SET
                    status = 'dispatched', updated_at_ms = ?1,
                    dispatched_lease_id = ?2, dispatched_lease_epoch = ?3,
                    dispatched_core_epoch = ?4
                 WHERE client_id = ?5 AND account_id = ?6 AND operation = ?7
                   AND operation_id = ?8 AND status = 'pending'",
                params![
                    now_ms,
                    lease.lease_id,
                    lease_epoch,
                    lease.core_epoch,
                    key.client_id,
                    key.account_id,
                    key.operation,
                    key.operation_id,
                ],
            )?;
            if changed != 1 {
                return Err(StoreError::InvalidResultTransition);
            }
            let record = read_operation(&tx, &key)?.ok_or_else(|| {
                StoreError::CorruptData("claimed operation disappeared".to_owned())
            })?;
            tx.commit()?;
            Ok(Some(record))
        })
        .await
    }

    /// Persist the gateway's observed result. Stale leases cannot complete an operation.
    pub async fn finish_operation(
        &self,
        credential: LeaseCredential,
        result: GatewayOperationResult,
        now_ms: i64,
    ) -> Result<OperationRecord, StoreError> {
        let core_epoch = self.core_epoch.clone();
        self.run(move |connection| {
            reap_expired(connection, &core_epoch, now_ms)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let lease = validate_lease_tx(&tx, &credential, now_ms, &core_epoch)?;
            let lease_epoch = i64::try_from(lease.lease_epoch).map_err(|_| {
                StoreError::CorruptData("lease epoch exceeds SQLite range".to_owned())
            })?;
            if result.key.client_id != lease.client_id || result.key.account_id != lease.account_id
            {
                return Err(StoreError::StaleLease);
            }
            let existing =
                read_operation(&tx, &result.key)?.ok_or(StoreError::OperationNotFound)?;
            if existing.fingerprint != result.fingerprint {
                return Err(StoreError::OperationConflict);
            }
            if existing.dispatched_lease_id.as_deref() != Some(lease.lease_id.as_str())
                || existing.dispatched_lease_epoch != Some(lease.lease_epoch)
            {
                return Err(StoreError::StaleLease);
            }
            let result_value = gateway_result_value(&result);
            if matches!(
                existing.status,
                OperationStatus::Succeeded | OperationStatus::Failed | OperationStatus::Unknown
            ) {
                if existing.result.as_ref() == Some(&result_value) {
                    tx.commit()?;
                    return Ok(existing);
                }
                return Err(StoreError::OperationConflict);
            }
            if existing.status != OperationStatus::Dispatched {
                return Err(StoreError::InvalidResultTransition);
            }
            let encoded_result = serde_json::to_string(&result_value)
                .map_err(|error| StoreError::CorruptData(error.to_string()))?;
            tx.execute(
                "UPDATE operation_ledger SET status = ?1, result_json = ?2, updated_at_ms = ?3
                 WHERE client_id = ?4 AND account_id = ?5 AND operation = ?6
                   AND operation_id = ?7 AND status = 'dispatched'
                   AND dispatched_lease_id = ?8 AND dispatched_lease_epoch = ?9",
                params![
                    result.status.as_str(),
                    encoded_result,
                    now_ms,
                    result.key.client_id,
                    result.key.account_id,
                    result.key.operation,
                    result.key.operation_id,
                    lease.lease_id,
                    lease_epoch,
                ],
            )?;
            let updated = read_operation(&tx, &result.key)?.ok_or_else(|| {
                StoreError::CorruptData("completed operation disappeared".to_owned())
            })?;
            tx.commit()?;
            Ok(updated)
        })
        .await
    }

    /// Read an operation only after rechecking the caller's grant against its original scope.
    pub async fn get_operation(
        &self,
        key: OperationKey,
        grant_id: String,
        principal: Principal,
        now_ms: i64,
    ) -> Result<OperationRecord, StoreError> {
        let core_epoch = self.core_epoch.clone();
        self.run(move |connection| {
            reap_expired(connection, &core_epoch, now_ms)?;
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let record = read_operation(&tx, &key)?.ok_or(StoreError::OperationNotFound)?;
            let grant = get_grant_tx(&tx, &grant_id)?.ok_or(PolicyError::InactiveGrant)?;
            if grant.client_id != key.client_id {
                return Err(PolicyError::PrincipalMismatch.into());
            }
            let target = operation_target(&record.operation_payload)?;
            authorize_operation(&grant, &principal, &key.account_id, &key.operation, &target)?;
            tx.commit()?;
            Ok(record)
        })
        .await
    }

    async fn run<T, F>(&self, operation: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T, StoreError> + Send + 'static,
    {
        let connection = self.connection.clone();
        task::spawn_blocking(move || {
            let mut connection = connection.lock().map_err(|_| StoreError::Poisoned)?;
            operation(&mut connection)
        })
        .await
        .map_err(|error| StoreError::Worker(error.to_string()))?
    }
}

struct LeaseRow {
    lease_id: String,
    client_id: String,
    account_id: String,
    lease_epoch: u64,
    core_epoch: String,
}

struct RawOperation {
    key: OperationKey,
    fingerprint: String,
    trace_id: String,
    grant_id: String,
    principal_kind: String,
    principal_id: String,
    host_epoch: i64,
    core_epoch_created: String,
    operation_json: String,
    status: String,
    created_at_ms: i64,
    updated_at_ms: i64,
    dispatched_lease_id: Option<String>,
    dispatched_lease_epoch: Option<i64>,
    dispatched_core_epoch: Option<String>,
    result_json: Option<String>,
}

struct RawEvent {
    key: EventKey,
    fingerprint: String,
    trace_id: String,
    target_host: String,
    event_json: String,
    status: String,
    error: Option<String>,
    created_at_ms: i64,
    updated_at_ms: i64,
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

fn begin_core_epoch(
    connection: &mut Connection,
    core_epoch: &str,
    now: i64,
) -> Result<(), StoreError> {
    let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    mark_unknown_tx(&tx, None, now, "core_epoch_changed")?;
    tx.execute(
        "UPDATE event_receipts SET status = 'unknown', error = 'core_epoch_changed', updated_at_ms = ?1
         WHERE status IN ('accepted', 'processing')",
        [now],
    )?;
    tx.execute("UPDATE gateway_leases SET active = 0 WHERE active = 1", [])?;
    tx.execute(
        "INSERT INTO core_state(singleton, current_epoch, started_at_ms)
         VALUES (1, ?1, ?2)
         ON CONFLICT(singleton) DO UPDATE SET
            current_epoch = excluded.current_epoch,
            started_at_ms = excluded.started_at_ms",
        params![core_epoch, now],
    )?;
    tx.commit()?;
    Ok(())
}

fn read_core_epoch(connection: &Connection) -> Result<String, StoreError> {
    connection
        .query_row(
            "SELECT current_epoch FROM core_state WHERE singleton = 1",
            [],
            |row| row.get(0),
        )
        .map_err(Into::into)
}

fn reap_expired(connection: &mut Connection, core_epoch: &str, now: i64) -> Result<(), StoreError> {
    let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let expired: Vec<String> = {
        let mut statement = tx.prepare(
            "SELECT lease_id FROM gateway_leases
             WHERE active = 1 AND (expires_at_ms <= ?1 OR core_epoch != ?2)",
        )?;
        statement
            .query_map(params![now, core_epoch], |row| row.get(0))?
            .collect::<Result<Vec<_>, _>>()?
    };
    for lease_id in expired {
        revoke_lease_tx(&tx, &lease_id, now, "gateway_lease_expired")?;
    }
    let result = unknown_result("core_epoch_changed", now);
    let result_json = serde_json::to_string(&result)
        .map_err(|error| StoreError::CorruptData(error.to_string()))?;
    tx.execute(
        "UPDATE operation_ledger SET status = 'unknown', result_json = ?1, updated_at_ms = ?2
         WHERE status = 'dispatched' AND dispatched_core_epoch != ?3",
        params![result_json, now, core_epoch],
    )?;
    tx.commit()?;
    Ok(())
}

fn mark_unknown_tx(
    tx: &Transaction<'_>,
    lease_id: Option<&str>,
    now: i64,
    reason: &str,
) -> Result<(), StoreError> {
    let result_json = serde_json::to_string(&unknown_result(reason, now))
        .map_err(|error| StoreError::CorruptData(error.to_string()))?;
    match lease_id {
        Some(lease_id) => {
            tx.execute(
                "UPDATE operation_ledger SET status = 'unknown', result_json = ?1, updated_at_ms = ?2
                 WHERE status = 'dispatched' AND dispatched_lease_id = ?3",
                params![result_json, now, lease_id],
            )?;
        }
        None => {
            tx.execute(
                "UPDATE operation_ledger SET status = 'unknown', result_json = ?1, updated_at_ms = ?2
                 WHERE status = 'dispatched'",
                params![result_json, now],
            )?;
        }
    }
    Ok(())
}

fn unknown_result(reason: &str, now: i64) -> Value {
    json!({
        "status": "unknown",
        "error": reason,
        "observed_at_ms": now,
    })
}

fn revoke_lease_tx(
    tx: &Transaction<'_>,
    lease_id: &str,
    now: i64,
    reason: &str,
) -> Result<(), StoreError> {
    mark_unknown_tx(tx, Some(lease_id), now, reason)?;
    tx.execute(
        "UPDATE gateway_leases SET active = 0 WHERE lease_id = ?1",
        [lease_id],
    )?;
    Ok(())
}

fn active_lease_ids_for_gateway(
    tx: &Transaction<'_>,
    gateway_id: &str,
) -> Result<Vec<String>, StoreError> {
    let mut statement =
        tx.prepare("SELECT lease_id FROM gateway_leases WHERE gateway_id = ?1 AND active = 1")?;
    statement
        .query_map([gateway_id], |row| row.get(0))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(Into::into)
}

fn validate_lease_tx(
    tx: &Transaction<'_>,
    credential: &LeaseCredential,
    now: i64,
    current_core_epoch: &str,
) -> Result<LeaseRow, StoreError> {
    let raw: Option<(String, String, String, i64, String, Vec<u8>, i64, i64)> = tx
        .query_row(
            "SELECT client_id, account_id, gateway_id, lease_epoch, core_epoch,
                    token_hash, expires_at_ms, active
             FROM gateway_leases WHERE lease_id = ?1",
            [&credential.lease_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                ))
            },
        )
        .optional()?;
    let Some((
        client_id,
        account_id,
        _gateway_id,
        lease_epoch,
        core_epoch,
        expected,
        expires,
        active,
    )) = raw
    else {
        return Err(StoreError::StaleLease);
    };
    if active == 0
        || expires <= now
        || core_epoch != current_core_epoch
        || !constant_time_eq(&expected, &hash_secret(&credential.token))
    {
        return Err(StoreError::StaleLease);
    }
    Ok(LeaseRow {
        lease_id: credential.lease_id.clone(),
        client_id,
        account_id,
        lease_epoch: u64::try_from(lease_epoch)
            .map_err(|_| StoreError::CorruptData("negative gateway lease epoch".to_owned()))?,
        core_epoch,
    })
}

fn get_grant_tx(tx: &Transaction<'_>, grant_id: &str) -> Result<Option<Grant>, StoreError> {
    let raw: Option<(String, String, String, String, String, Option<String>, i64)> = tx
        .query_row(
            "SELECT client_id, principal_kind, principal_id, account_id,
                    operations_json, targets_json, active
             FROM grants WHERE grant_id = ?1",
            [grant_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                ))
            },
        )
        .optional()?;
    raw.map(
        |(client_id, kind, id, account_id, operations, targets, active)| {
            let principal = match kind.as_str() {
                "client" => Principal::Client(id),
                "host" => Principal::Host(id),
                _ => {
                    return Err(StoreError::CorruptData(format!(
                        "unknown grant principal kind: {kind}"
                    )));
                }
            };
            let operations = serde_json::from_str(&operations).map_err(|error| {
                StoreError::CorruptData(format!("invalid grant operations: {error}"))
            })?;
            let targets = targets
                .map(|value| {
                    serde_json::from_str(&value).map_err(|error| {
                        StoreError::CorruptData(format!("invalid grant targets: {error}"))
                    })
                })
                .transpose()?;
            Ok(Grant {
                id: grant_id.to_owned(),
                client_id,
                principal,
                account_id,
                operations,
                targets,
                active: active != 0,
            })
        },
    )
    .transpose()
}

fn read_operation(
    connection: &Connection,
    key: &OperationKey,
) -> Result<Option<OperationRecord>, StoreError> {
    let raw: Option<RawOperation> = connection
        .query_row(
            "SELECT fingerprint, trace_id, grant_id, principal_kind, principal_id, host_epoch,
                    core_epoch_created, operation_json, status, created_at_ms, updated_at_ms,
                    dispatched_lease_id, dispatched_lease_epoch, dispatched_core_epoch, result_json
             FROM operation_ledger
             WHERE client_id = ?1 AND account_id = ?2 AND operation = ?3 AND operation_id = ?4",
            params![
                key.client_id,
                key.account_id,
                key.operation,
                key.operation_id
            ],
            |row| raw_operation(row, key),
        )
        .optional()?;
    raw.map(raw_to_operation).transpose()
}

fn read_event(connection: &Connection, key: &EventKey) -> Result<Option<EventRecord>, StoreError> {
    let raw: Option<RawEvent> = connection
        .query_row(
            "SELECT fingerprint, trace_id, target_host, event_json, status, error,
                    created_at_ms, updated_at_ms
             FROM event_receipts
             WHERE client_id = ?1 AND platform = ?2 AND account_id = ?3
               AND conversation_kind = ?4 AND conversation_id = ?5 AND event_id = ?6",
            rusqlite::params![
                key.client_id,
                key.platform,
                key.account_id,
                key.conversation_kind,
                key.conversation_id,
                key.event_id,
            ],
            |row| {
                Ok(RawEvent {
                    key: key.clone(),
                    fingerprint: row.get(0)?,
                    trace_id: row.get(1)?,
                    target_host: row.get(2)?,
                    event_json: row.get(3)?,
                    status: row.get(4)?,
                    error: row.get(5)?,
                    created_at_ms: row.get(6)?,
                    updated_at_ms: row.get(7)?,
                })
            },
        )
        .optional()?;
    raw.map(|raw| {
        Ok(EventRecord {
            key: raw.key,
            fingerprint: raw.fingerprint,
            trace_id: raw.trace_id,
            target_host: raw.target_host,
            event: serde_json::from_str(&raw.event_json)
                .map_err(|error| StoreError::CorruptData(format!("invalid event JSON: {error}")))?,
            status: EventStatus::from_str(&raw.status)?,
            error: raw.error,
            created_at_ms: raw.created_at_ms,
            updated_at_ms: raw.updated_at_ms,
        })
    })
    .transpose()
}

fn raw_operation(row: &Row<'_>, key: &OperationKey) -> rusqlite::Result<RawOperation> {
    Ok(RawOperation {
        key: key.clone(),
        fingerprint: row.get(0)?,
        trace_id: row.get(1)?,
        grant_id: row.get(2)?,
        principal_kind: row.get(3)?,
        principal_id: row.get(4)?,
        host_epoch: row.get(5)?,
        core_epoch_created: row.get(6)?,
        operation_json: row.get(7)?,
        status: row.get(8)?,
        created_at_ms: row.get(9)?,
        updated_at_ms: row.get(10)?,
        dispatched_lease_id: row.get(11)?,
        dispatched_lease_epoch: row.get(12)?,
        dispatched_core_epoch: row.get(13)?,
        result_json: row.get(14)?,
    })
}

fn raw_to_operation(raw: RawOperation) -> Result<OperationRecord, StoreError> {
    let principal = match raw.principal_kind.as_str() {
        "client" => Principal::Client(raw.principal_id),
        "host" => Principal::Host(raw.principal_id),
        _ => {
            return Err(StoreError::CorruptData(format!(
                "unknown operation principal kind: {}",
                raw.principal_kind
            )));
        }
    };
    Ok(OperationRecord {
        key: raw.key,
        fingerprint: raw.fingerprint,
        trace_id: raw.trace_id,
        grant_id: raw.grant_id,
        principal,
        host_epoch: u64::try_from(raw.host_epoch)
            .map_err(|_| StoreError::CorruptData("negative Host epoch".to_owned()))?,
        core_epoch_created: raw.core_epoch_created,
        operation_payload: serde_json::from_str(&raw.operation_json)
            .map_err(|error| StoreError::CorruptData(format!("invalid operation JSON: {error}")))?,
        status: OperationStatus::from_str(&raw.status)?,
        created_at_ms: raw.created_at_ms,
        updated_at_ms: raw.updated_at_ms,
        dispatched_lease_id: raw.dispatched_lease_id,
        dispatched_lease_epoch: raw
            .dispatched_lease_epoch
            .map(|epoch| u64::try_from(epoch))
            .transpose()
            .map_err(|_| StoreError::CorruptData("negative lease epoch".to_owned()))?,
        dispatched_core_epoch: raw.dispatched_core_epoch,
        result: raw
            .result_json
            .map(|value| {
                serde_json::from_str(&value).map_err(|error| {
                    StoreError::CorruptData(format!("invalid result JSON: {error}"))
                })
            })
            .transpose()?,
    })
}

fn operation_target(operation: &Value) -> Result<OperationTarget, StoreError> {
    let target = operation
        .get("target")
        .ok_or_else(|| StoreError::InvalidInput("operation target is missing".to_owned()))?;
    Ok(OperationTarget {
        kind: required_string(target, "kind")?,
        id: required_string(target, "id")?,
    })
}

fn required_string(value: &Value, key: &str) -> Result<String, StoreError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| StoreError::InvalidInput(format!("{key} must be a string")))
}

fn event_key(client_id: &str, event: &Value) -> Result<EventKey, StoreError> {
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

fn event_fingerprint(event: &Value) -> Result<String, StoreError> {
    let mut semantic_event = event.clone();
    if let Some(object) = semantic_event.as_object_mut() {
        object.remove("trace_id");
    }
    let bytes = serde_json::to_vec(&semantic_event)
        .map_err(|error| StoreError::CorruptData(error.to_string()))?;
    Ok(hex_digest(Sha256::digest(bytes).as_slice()))
}

fn operation_fingerprint(operation: &Value) -> Result<String, StoreError> {
    let fingerprint_fields = json!({
        "account_id": operation.get("account_id"),
        "operation": operation.get("operation"),
        "target": operation.get("target"),
        "params": operation.get("params"),
    });
    let bytes = serde_json::to_vec(&fingerprint_fields)
        .map_err(|error| StoreError::CorruptData(error.to_string()))?;
    Ok(hex_digest(Sha256::digest(bytes).as_slice()))
}

fn gateway_result_value(result: &GatewayOperationResult) -> Value {
    json!({
        "status": result.status.as_str(),
        "platform_message_id": result.platform_message_id,
        "data": result.data,
        "error": result.error,
        "observed_at_ms": result.observed_at_ms,
        "evidence_ref": result.evidence_ref,
    })
}

fn validate_id(name: &str, value: &str) -> Result<(), StoreError> {
    if value.trim().is_empty() || value.len() > 256 {
        return Err(StoreError::InvalidInput(format!(
            "{name} must contain 1 to 256 bytes"
        )));
    }
    Ok(())
}

fn hash_secret(value: &str) -> Vec<u8> {
    Sha256::digest(value.as_bytes()).to_vec()
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

fn hex_digest(digest: &[u8]) -> String {
    use std::fmt::Write;
    let mut output = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(event_id: &str, text: &str) -> Value {
        json!({
            "schema_version": ligamina_contracts::PROTOCOL_VERSION,
            "event_id": event_id,
            "trace_id": format!("trace-{event_id}"),
            "platform": "virtual.onebot",
            "account_id": "account-1",
            "conversation": {"kind": "group", "id": "room-1"},
            "actor_id": "user-1",
            "timestamp": "2026-10-10T00:00:00Z",
            "target_host": "worker-A",
            "message": [{"type": "text", "text": text}]
        })
    }

    #[tokio::test]
    async fn core_restart_marks_unfinished_events_unknown_but_preserves_terminal_receipts() {
        let path = std::env::temp_dir().join(format!("ligamina-event-{}.sqlite", Uuid::new_v4()));
        let store = Store::open(&path).expect("open file-backed event store");
        assert_eq!(
            store.schema_version().await.unwrap(),
            CURRENT_SCHEMA_VERSION
        );
        let accepted = event("evt-accepted", "first");
        let processing = event("evt-processing", "second");
        let completed = event("evt-completed", "third");
        for value in [&accepted, &processing, &completed] {
            store
                .accept_event("client-A".to_owned(), value.clone(), 1)
                .await
                .expect("persist event receipt");
        }
        store
            .transition_event(
                event_key("client-A", &processing).unwrap(),
                EventStatus::Processing,
                None,
                2,
            )
            .await
            .unwrap();
        store
            .transition_event(
                event_key("client-A", &completed).unwrap(),
                EventStatus::Processing,
                None,
                2,
            )
            .await
            .unwrap();
        store
            .transition_event(
                event_key("client-A", &completed).unwrap(),
                EventStatus::Completed,
                None,
                3,
            )
            .await
            .unwrap();
        drop(store);

        let reopened = Store::open(&path).expect("reopen Core database");
        for value in [&accepted, &processing] {
            let record = reopened
                .get_event(event_key("client-A", value).unwrap())
                .await
                .expect("unfinished receipt remains queryable");
            assert_eq!(record.status, EventStatus::Unknown);
            assert_eq!(record.error.as_deref(), Some("core_epoch_changed"));
        }
        assert_eq!(
            reopened
                .get_event(event_key("client-A", &completed).unwrap())
                .await
                .unwrap()
                .status,
            EventStatus::Completed
        );
        let (disposition, duplicate) = reopened
            .accept_event("client-A".to_owned(), accepted, 4)
            .await
            .expect("retry finds original unknown receipt");
        assert_eq!(disposition, EventDisposition::Duplicate);
        assert_eq!(duplicate.status, EventStatus::Unknown);
        drop(reopened);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(format!("{}-wal", path.display()));
        let _ = std::fs::remove_file(format!("{}-shm", path.display()));
    }
}
