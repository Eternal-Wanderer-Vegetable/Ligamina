//! Customer-owned gateway boundary for durable platform side effects.

use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use crate::{
    policy::Principal,
    store::{
        GatewayOperationResult, GatewayRegistration, LeaseCredential, OperationKey,
        OperationRecord, OperationSubmission, PrepareDisposition, Store, StoreError,
    },
};

#[derive(Clone)]
pub struct PlatformGateway {
    store: Arc<Store>,
}

impl PlatformGateway {
    pub fn new(store: Arc<Store>) -> Self {
        Self { store }
    }

    pub async fn register_gateway(
        &self,
        registration: GatewayRegistration,
        registration_token: String,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        self.store
            .register_gateway(registration, registration_token, now_ms)
            .await
    }

    pub async fn acquire_lease(
        &self,
        gateway_id: String,
        registration_token: String,
        ttl: Duration,
        now_ms: i64,
    ) -> Result<LeaseCredential, StoreError> {
        self.store
            .acquire_lease(gateway_id, registration_token, ttl, now_ms)
            .await
    }

    pub async fn submit(
        &self,
        submission: OperationSubmission,
    ) -> Result<(PrepareDisposition, OperationRecord), StoreError> {
        self.store.prepare_operation(submission).await
    }

    pub async fn claim(
        &self,
        credential: LeaseCredential,
        now_ms: i64,
    ) -> Result<Option<OperationRecord>, StoreError> {
        self.store.claim_pending(credential, now_ms).await
    }

    pub async fn report_result(
        &self,
        credential: LeaseCredential,
        result: GatewayOperationResult,
        now_ms: i64,
    ) -> Result<OperationRecord, StoreError> {
        self.store
            .finish_operation(credential, result, now_ms)
            .await
    }

    pub async fn operation_status(
        &self,
        key: OperationKey,
        grant_id: String,
        principal: Principal,
        now_ms: i64,
    ) -> Result<OperationRecord, StoreError> {
        self.store
            .get_operation(key, grant_id, principal, now_ms)
            .await
    }

    pub fn core_epoch(&self) -> &str {
        self.store.core_epoch()
    }

    pub fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min(i64::MAX as u128) as i64
    }
}
