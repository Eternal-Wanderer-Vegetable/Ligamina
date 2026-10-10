//! Deterministic grant checks for platform side effects.

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "id", rename_all = "snake_case")]
pub enum Principal {
    Client(String),
    Host(String),
}

impl Principal {
    pub(crate) fn kind_and_id(&self) -> (&'static str, &str) {
        match self {
            Self::Client(id) => ("client", id),
            Self::Host(id) => ("host", id),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct OperationTarget {
    pub kind: String,
    pub id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Grant {
    pub id: String,
    pub client_id: String,
    pub principal: Principal,
    pub account_id: String,
    pub operations: Vec<String>,
    /// `None` grants all conversations for this account; `Some` is an exact allowlist.
    pub targets: Option<Vec<OperationTarget>>,
    pub active: bool,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum PolicyError {
    #[error("grant is missing or inactive")]
    InactiveGrant,
    #[error("grant does not belong to this principal")]
    PrincipalMismatch,
    #[error("operation account is outside the grant")]
    AccountDenied,
    #[error("operation is outside the grant")]
    OperationDenied,
    #[error("target conversation is outside the grant")]
    TargetDenied,
}

pub fn authorize_operation(
    grant: &Grant,
    principal: &Principal,
    account_id: &str,
    operation: &str,
    target: &OperationTarget,
) -> Result<(), PolicyError> {
    if !grant.active {
        return Err(PolicyError::InactiveGrant);
    }
    if &grant.principal != principal {
        return Err(PolicyError::PrincipalMismatch);
    }
    if grant.account_id != account_id {
        return Err(PolicyError::AccountDenied);
    }
    if !grant.operations.iter().any(|allowed| allowed == operation) {
        return Err(PolicyError::OperationDenied);
    }
    if grant
        .targets
        .as_ref()
        .is_some_and(|targets| !targets.iter().any(|allowed| allowed == target))
    {
        return Err(PolicyError::TargetDenied);
    }
    Ok(())
}
