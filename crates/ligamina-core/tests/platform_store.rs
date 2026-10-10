use std::{fs, sync::Arc, time::Duration};

use ligamina_core::{
    platform::PlatformGateway,
    policy::{Grant, OperationTarget, PolicyError, Principal},
    store::{
        GatewayOperationResult, GatewayRegistration, GatewayResultStatus, LeaseCredential,
        OperationKey, OperationStatus, OperationSubmission, PrepareDisposition, Store, StoreError,
    },
};
use serde_json::{Value, json};
use uuid::Uuid;

const REGISTRATION_TOKEN: &str = "test-registration-token-with-at-least-32-bytes";
const CLIENT_ID: &str = "client-A";
const HOST_ID: &str = "worker-A";
const ACCOUNT_ID: &str = "10001";
const GRANT_ID: &str = "grant-A";

fn send_message() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/v0.1/fixtures/operation.send-message.json"
    ))
    .expect("operation fixture is valid JSON")
}

fn submission(operation: Value, now_ms: i64) -> OperationSubmission {
    OperationSubmission {
        grant_id: GRANT_ID.to_owned(),
        principal: Principal::Host(HOST_ID.to_owned()),
        host_epoch: 1,
        operation,
        now_ms,
    }
}

fn registration() -> GatewayRegistration {
    GatewayRegistration {
        gateway_id: "gateway-A".to_owned(),
        client_id: CLIENT_ID.to_owned(),
        account_id: ACCOUNT_ID.to_owned(),
    }
}

fn grant() -> Grant {
    Grant {
        id: GRANT_ID.to_owned(),
        client_id: CLIENT_ID.to_owned(),
        principal: Principal::Host(HOST_ID.to_owned()),
        account_id: ACCOUNT_ID.to_owned(),
        operations: vec!["send_message".to_owned()],
        targets: Some(vec![OperationTarget {
            kind: "group".to_owned(),
            id: "18446744073709551616000004".to_owned(),
        }]),
        active: true,
    }
}

async fn configured_gateway() -> (PlatformGateway, LeaseCredential, Arc<Store>) {
    let store = Arc::new(Store::open_in_memory().expect("open in-memory SQLite"));
    let gateway = PlatformGateway::new(store.clone());
    gateway
        .register_gateway(registration(), REGISTRATION_TOKEN.to_owned(), 10)
        .await
        .expect("register trusted gateway");
    store.put_grant(grant()).await.expect("persist grant");
    let lease = gateway
        .acquire_lease(
            "gateway-A".to_owned(),
            REGISTRATION_TOKEN.to_owned(),
            Duration::from_secs(60),
            11,
        )
        .await
        .expect("acquire account lease");
    (gateway, lease, store)
}

fn operation_key(operation: &Value) -> OperationKey {
    OperationKey {
        client_id: CLIENT_ID.to_owned(),
        account_id: operation["account_id"].as_str().unwrap().to_owned(),
        operation: operation["operation"].as_str().unwrap().to_owned(),
        operation_id: operation["operation_id"].as_str().unwrap().to_owned(),
    }
}

fn result_for(record: &ligamina_core::store::OperationRecord) -> GatewayOperationResult {
    GatewayOperationResult {
        key: record.key.clone(),
        fingerprint: record.fingerprint.clone(),
        status: GatewayResultStatus::Succeeded,
        platform_message_id: Some("platform-message-77".to_owned()),
        data: Some(json!({"fixture": "gateway-result-payload"})),
        error: None,
        observed_at_ms: 30,
        evidence_ref: Some("test-receipt-77".to_owned()),
    }
}

#[tokio::test]
async fn persists_before_claim_and_enforces_idempotency_and_grants() {
    let (gateway, lease, store) = configured_gateway().await;
    let first = send_message();
    let (disposition, pending) = gateway
        .submit(submission(first.clone(), 20))
        .await
        .expect("authorized operation should persist");
    assert_eq!(disposition, PrepareDisposition::Created);
    assert_eq!(pending.status, OperationStatus::Pending);

    let mut same_payload = first.clone();
    same_payload["trace_id"] = json!("trace-retry");
    let (disposition, retry) = gateway
        .submit(submission(same_payload, 21))
        .await
        .expect("same operation payload should be idempotent");
    assert_eq!(disposition, PrepareDisposition::Idempotent);
    assert_eq!(retry.fingerprint, pending.fingerprint);
    assert_eq!(retry.key, pending.key);

    let mut changed_payload = first.clone();
    changed_payload["params"]["message"][0]["text"] = json!("changed");
    assert!(matches!(
        gateway.submit(submission(changed_payload, 22)).await,
        Err(StoreError::OperationConflict)
    ));

    let mut unauthorized_target = first.clone();
    unauthorized_target["operation_id"] = json!("op-denied-target");
    unauthorized_target["target"]["id"] = json!("another-group");
    assert!(matches!(
        gateway
            .submit(submission(unauthorized_target.clone(), 22))
            .await,
        Err(StoreError::Policy(PolicyError::TargetDenied))
    ));

    let mut unauthorized_operation = first.clone();
    unauthorized_operation["operation_id"] = json!("op-denied-operation");
    unauthorized_operation["operation"] = json!("get_message");
    assert!(matches!(
        gateway.submit(submission(unauthorized_operation, 22)).await,
        Err(StoreError::Policy(PolicyError::OperationDenied))
    ));

    let mut unsupported = first.clone();
    unsupported["operation_id"] = json!("op-unsupported");
    unsupported["operation"] = json!("delete_message");
    assert!(matches!(
        gateway.submit(submission(unsupported, 22)).await,
        Err(StoreError::UnsupportedOperation(_))
    ));

    let claimed = gateway
        .claim(lease.clone(), 23)
        .await
        .expect("claim should succeed")
        .expect("pending operation should be claimed");
    assert_eq!(claimed.status, OperationStatus::Dispatched);
    assert_eq!(
        claimed.dispatched_lease_id.as_deref(),
        Some(lease.lease_id.as_str())
    );
    assert!(
        gateway
            .claim(lease.clone(), 24)
            .await
            .expect("second claim should succeed")
            .is_none()
    );

    let result = result_for(&claimed);
    let completed = gateway
        .report_result(lease.clone(), result.clone(), 30)
        .await
        .expect("gateway result should persist");
    assert_eq!(completed.status, OperationStatus::Succeeded);
    assert_eq!(
        completed.result.as_ref().expect("terminal result")["data"],
        json!({"fixture": "gateway-result-payload"})
    );
    let duplicate_result = gateway
        .report_result(lease, result, 31)
        .await
        .expect("duplicate matching result should be idempotent");
    assert_eq!(duplicate_result.status, OperationStatus::Succeeded);
    assert_eq!(duplicate_result.result, completed.result);

    let mut allow_second_group = grant();
    allow_second_group.targets = None;
    store
        .put_grant(allow_second_group)
        .await
        .expect("update explicit target allowlist");
    let (disposition, authorized_after_update) = gateway
        .submit(submission(unauthorized_target, 32))
        .await
        .expect("rejected target did not create a ledger row");
    assert_eq!(disposition, PrepareDisposition::Created);
    assert_eq!(authorized_after_update.status, OperationStatus::Pending);
}

#[tokio::test]
async fn replacing_lease_marks_dispatched_unknown_and_rejects_old_owner() {
    let (gateway, old_lease, _) = configured_gateway().await;
    let mut operation = send_message();
    operation["operation_id"] = json!("op-lease-rotation");
    gateway
        .submit(submission(operation.clone(), 20))
        .await
        .expect("persist before dispatch");
    let dispatched = gateway
        .claim(old_lease.clone(), 21)
        .await
        .expect("claim pending operation")
        .expect("operation should dispatch");

    let new_lease = gateway
        .acquire_lease(
            "gateway-A".to_owned(),
            REGISTRATION_TOKEN.to_owned(),
            Duration::from_secs(60),
            22,
        )
        .await
        .expect("new lease replaces the old one");
    assert!(new_lease.lease_epoch > old_lease.lease_epoch);
    let status = gateway
        .operation_status(
            operation_key(&operation),
            GRANT_ID.to_owned(),
            Principal::Host(HOST_ID.to_owned()),
            23,
        )
        .await
        .expect("host may inspect operation status");
    assert_eq!(status.status, OperationStatus::Unknown);
    assert!(
        gateway
            .claim(new_lease.clone(), 23)
            .await
            .expect("new lease can poll")
            .is_none()
    );
    assert!(matches!(
        gateway.claim(old_lease.clone(), 23).await,
        Err(StoreError::StaleLease)
    ));
    assert!(matches!(
        gateway
            .report_result(old_lease, result_for(&dispatched), 24)
            .await,
        Err(StoreError::StaleLease)
    ));
}

#[tokio::test]
async fn core_restart_marks_dispatched_unknown_and_revokes_old_lease() {
    let path = std::env::temp_dir().join(format!("ligamina-store-{}.sqlite", Uuid::new_v4()));
    let store = Arc::new(Store::open(&path).expect("open file-backed SQLite"));
    let gateway = PlatformGateway::new(store.clone());
    gateway
        .register_gateway(registration(), REGISTRATION_TOKEN.to_owned(), 10)
        .await
        .expect("register gateway");
    store.put_grant(grant()).await.expect("persist grant");
    let lease = gateway
        .acquire_lease(
            "gateway-A".to_owned(),
            REGISTRATION_TOKEN.to_owned(),
            Duration::from_secs(60),
            11,
        )
        .await
        .expect("acquire lease");
    let mut operation = send_message();
    operation["operation_id"] = json!("op-core-restart");
    gateway
        .submit(submission(operation.clone(), 20))
        .await
        .expect("persist before dispatch");
    let dispatched = gateway
        .claim(lease.clone(), 21)
        .await
        .expect("claim pending operation")
        .expect("operation should dispatch");
    assert_eq!(dispatched.status, OperationStatus::Dispatched);
    let old_epoch = gateway.core_epoch().to_owned();

    drop(gateway);
    drop(store);
    let reopened_store = Arc::new(Store::open(&path).expect("reopen Core database"));
    let reopened_gateway = PlatformGateway::new(reopened_store.clone());
    assert_ne!(reopened_gateway.core_epoch(), old_epoch);
    let status = reopened_gateway
        .operation_status(
            operation_key(&operation),
            GRANT_ID.to_owned(),
            Principal::Host(HOST_ID.to_owned()),
            22,
        )
        .await
        .expect("status remains queryable after restart");
    assert_eq!(status.status, OperationStatus::Unknown);
    assert!(matches!(
        reopened_gateway.claim(lease, 22).await,
        Err(StoreError::StaleLease)
    ));
    assert!(
        reopened_gateway
            .claim(
                reopened_gateway
                    .acquire_lease(
                        "gateway-A".to_owned(),
                        REGISTRATION_TOKEN.to_owned(),
                        Duration::from_secs(60),
                        23,
                    )
                    .await
                    .expect("new Core lease"),
                24,
            )
            .await
            .expect("unknown operation is not requeued")
            .is_none()
    );

    drop(reopened_gateway);
    drop(reopened_store);
    let _ = fs::remove_file(&path);
    let _ = fs::remove_file(format!("{}-wal", path.display()));
    let _ = fs::remove_file(format!("{}-shm", path.display()));
}
