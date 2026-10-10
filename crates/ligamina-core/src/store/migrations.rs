use rusqlite::{Connection, TransactionBehavior};

use super::{StoreError, now_ms};

pub(super) const CURRENT_SCHEMA_VERSION: i64 = 2;

pub(super) fn migrate(connection: &mut Connection) -> Result<(), StoreError> {
    let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_ms INTEGER NOT NULL
        );",
    )?;
    let applied: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = 1)",
        [],
        |row| row.get::<_, i64>(0).map(|value| value != 0),
    )?;
    if !applied {
        tx.execute_batch(
            "CREATE TABLE core_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                current_epoch TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL
            );

            CREATE TABLE grants (
                grant_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                principal_kind TEXT NOT NULL CHECK (principal_kind IN ('client', 'host')),
                principal_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                operations_json TEXT NOT NULL,
                targets_json TEXT,
                active INTEGER NOT NULL CHECK (active IN (0, 1))
            );
            CREATE INDEX grants_scope ON grants(client_id, principal_kind, principal_id, account_id);

            CREATE TABLE gateway_registrations (
                gateway_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                credential_hash BLOB NOT NULL
            );

            CREATE TABLE gateway_leases (
                lease_id TEXT PRIMARY KEY,
                gateway_id TEXT NOT NULL REFERENCES gateway_registrations(gateway_id),
                client_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL CHECK (lease_epoch > 0),
                core_epoch TEXT NOT NULL,
                token_hash BLOB NOT NULL,
                expires_at_ms INTEGER NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                UNIQUE(client_id, account_id, lease_epoch)
            );
            CREATE UNIQUE INDEX one_active_lease_per_account
                ON gateway_leases(client_id, account_id) WHERE active = 1;
            CREATE UNIQUE INDEX one_active_lease_per_gateway
                ON gateway_leases(gateway_id) WHERE active = 1;

            CREATE TABLE operation_ledger (
                client_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                principal_kind TEXT NOT NULL CHECK (principal_kind IN ('client', 'host')),
                principal_id TEXT NOT NULL,
                host_epoch INTEGER NOT NULL CHECK (host_epoch >= 0),
                core_epoch_created TEXT NOT NULL,
                operation_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'succeeded', 'failed', 'unknown')),
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                dispatched_lease_id TEXT REFERENCES gateway_leases(lease_id),
                dispatched_lease_epoch INTEGER,
                dispatched_core_epoch TEXT,
                result_json TEXT,
                PRIMARY KEY(client_id, account_id, operation, operation_id)
            );
            CREATE INDEX operation_ledger_pending
                ON operation_ledger(client_id, account_id, status, created_at_ms);

            INSERT INTO schema_migrations(version, name, applied_at_ms)
                VALUES (1, 'core-grants-gateways-operation-ledger', 0);",
        )?;
        tx.execute(
            "UPDATE schema_migrations SET applied_at_ms = ?1 WHERE version = 1",
            [now_ms()],
        )?;
    }
    tx.commit()?;

    let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let applied: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = 2)",
        [],
        |row| row.get::<_, i64>(0).map(|value| value != 0),
    )?;
    if !applied {
        tx.execute_batch(
            "CREATE TABLE event_receipts (
                client_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                conversation_kind TEXT NOT NULL CHECK (conversation_kind IN ('group', 'private')),
                conversation_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                target_host TEXT NOT NULL,
                event_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('accepted', 'processing', 'completed', 'failed', 'unknown')),
                error TEXT,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY(client_id, platform, account_id, conversation_kind, conversation_id, event_id)
            );
            CREATE INDEX event_receipts_status
                ON event_receipts(client_id, account_id, status, created_at_ms);
            INSERT INTO schema_migrations(version, name, applied_at_ms)
                VALUES (2, 'event-receipts-and-router-state', 0);
            UPDATE schema_migrations SET applied_at_ms = strftime('%s','now') * 1000 WHERE version = 2;",
        )?;
    }
    tx.commit()?;
    Ok(())
}
