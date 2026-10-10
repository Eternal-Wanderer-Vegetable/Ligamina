//! Shared, versioned wire contracts for Ligamina Core and Hosts.

pub const PROTOCOL_VERSION: &str = "ligamina/0.1";

use jsonschema::Validator;
use serde_json::Value;
use std::sync::LazyLock;
use thiserror::Error;

const EVENT_SCHEMA_SOURCE: &str =
    include_str!("../../../contracts/v0.1/event-envelope.schema.json");
const OPERATION_SCHEMA_SOURCE: &str =
    include_str!("../../../contracts/v0.1/platform-operation.schema.json");
const CAPABILITY_SCHEMA_SOURCE: &str =
    include_str!("../../../contracts/v0.1/capability-descriptor.schema.json");

static EVENT_VALIDATOR: LazyLock<Validator> = LazyLock::new(|| {
    let schema: Value =
        serde_json::from_str(EVENT_SCHEMA_SOURCE).expect("event schema is valid JSON");
    jsonschema::validator_for(&schema).expect("event schema is valid JSON Schema")
});

static OPERATION_VALIDATOR: LazyLock<Validator> = LazyLock::new(|| {
    let schema: Value =
        serde_json::from_str(OPERATION_SCHEMA_SOURCE).expect("operation schema is valid JSON");
    jsonschema::validator_for(&schema).expect("operation schema is valid JSON Schema")
});
static CAPABILITY_VALIDATOR: LazyLock<Validator> = LazyLock::new(|| {
    let schema: Value =
        serde_json::from_str(CAPABILITY_SCHEMA_SOURCE).expect("capability schema is valid JSON");
    jsonschema::validator_for(&schema).expect("capability schema is valid JSON Schema")
});

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ContractError {
    #[error("protocol version is incompatible")]
    ProtocolMismatch,
    #[error("invalid contract: {0}")]
    Invalid(String),
}

impl ContractError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::ProtocolMismatch => "protocol_mismatch",
            Self::Invalid(_) => "invalid_request",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperationReuse {
    New,
    Idempotent,
    Conflict,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperationSupport {
    Supported,
    Unsupported,
}

pub fn validate_event(value: &Value) -> Result<(), ContractError> {
    if value
        .get("schema_version")
        .and_then(Value::as_str)
        .is_some_and(|version| version != PROTOCOL_VERSION)
    {
        return Err(ContractError::ProtocolMismatch);
    }
    validate_against(&EVENT_VALIDATOR, value)
}

pub fn validate_operation(value: &Value) -> Result<(), ContractError> {
    validate_against(&OPERATION_VALIDATOR, value)
}

pub fn validate_capability_descriptor(value: &Value) -> Result<(), ContractError> {
    validate_against(&CAPABILITY_VALIDATOR, value)
}

fn validate_against(validator: &Validator, value: &Value) -> Result<(), ContractError> {
    let errors: Vec<String> = validator
        .iter_errors(value)
        .map(|error| error.to_string())
        .collect();
    if errors.is_empty() {
        Ok(())
    } else {
        Err(ContractError::Invalid(errors.join("; ")))
    }
}

pub fn classify_operation_reuse(existing: &Value, incoming: &Value) -> OperationReuse {
    let existing_id = existing.get("operation_id");
    let incoming_id = incoming.get("operation_id");
    if existing_id != incoming_id {
        OperationReuse::New
    } else if ["account_id", "operation", "target", "params"]
        .iter()
        .all(|field| existing.get(*field) == incoming.get(*field))
    {
        OperationReuse::Idempotent
    } else {
        OperationReuse::Conflict
    }
}

pub fn classify_operation_support(operation: &str) -> OperationSupport {
    match operation {
        "send_message" | "get_message" | "get_self_info" => OperationSupport::Supported,
        _ => OperationSupport::Unsupported,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{fs, path::PathBuf};

    fn fixture(name: &str) -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../contracts/v0.1/fixtures")
            .join(name);
        serde_json::from_slice(&fs::read(path).expect("fixture is readable"))
            .expect("fixture is JSON")
    }

    #[test]
    fn shared_event_vectors_match_schema_expectations() {
        let schema: Value = serde_json::from_str(EVENT_SCHEMA_SOURCE).unwrap();
        let validator = jsonschema::validator_for(&schema).unwrap();
        let vectors: Value = serde_json::from_str(include_str!(
            "../../../contracts/v0.1/fixtures/vectors.json"
        ))
        .unwrap();

        for vector in vectors["event_schema"].as_array().unwrap() {
            let value = fixture(vector["file"].as_str().unwrap());
            let result = validate_event(&value);
            assert_eq!(
                validator.is_valid(&value),
                vector["valid"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
            assert_eq!(
                result.as_ref().err().map(ContractError::code),
                vector["error_code"].as_str(),
                "{} error code",
                vector["name"]
            );
        }
    }

    #[test]
    fn shared_operation_vectors_define_validation_and_collision_results() {
        let schema: Value = serde_json::from_str(OPERATION_SCHEMA_SOURCE).unwrap();
        let validator = jsonschema::validator_for(&schema).unwrap();
        let vectors: Value = serde_json::from_str(include_str!(
            "../../../contracts/v0.1/fixtures/vectors.json"
        ))
        .unwrap();

        for vector in vectors["operation_schema"].as_array().unwrap() {
            if let Some(file) = vector["file"].as_str() {
                let value = fixture(file);
                assert_eq!(
                    validator.is_valid(&value),
                    vector["valid"].as_bool().unwrap(),
                    "{}",
                    vector["name"]
                );
                if vector["outcome"] == "unsupported_api" {
                    let operation = value["operation"].as_str().unwrap();
                    assert_eq!(
                        classify_operation_support(operation),
                        OperationSupport::Unsupported
                    );
                }
            } else {
                let existing = fixture(vector["existing"].as_str().unwrap());
                let incoming = fixture(vector["incoming"].as_str().unwrap());
                let expected = match vector["outcome"].as_str().unwrap() {
                    "idempotent" => OperationReuse::Idempotent,
                    "operation_conflict" => OperationReuse::Conflict,
                    other => panic!("unexpected vector outcome: {other}"),
                };
                assert!(validator.is_valid(&existing));
                assert!(validator.is_valid(&incoming));
                assert_eq!(classify_operation_reuse(&existing, &incoming), expected);
            }
        }

        assert_eq!(
            classify_operation_reuse(&json!({"operation_id":"a"}), &json!({"operation_id":"b"})),
            OperationReuse::New
        );
    }

    #[test]
    fn shared_capability_descriptor_vectors_match_the_schema() {
        let schema: Value = serde_json::from_str(CAPABILITY_SCHEMA_SOURCE).unwrap();
        let validator = jsonschema::validator_for(&schema).unwrap();
        let vectors: Value = serde_json::from_str(include_str!(
            "../../../contracts/v0.1/fixtures/vectors.json"
        ))
        .unwrap();

        for vector in vectors["capability_descriptor"].as_array().unwrap() {
            let value = fixture(vector["file"].as_str().unwrap());
            let valid = vector["valid"].as_bool().unwrap();
            assert_eq!(validator.is_valid(&value), valid, "{}", vector["name"]);
            assert_eq!(
                validate_capability_descriptor(&value).is_ok(),
                valid,
                "{}",
                vector["name"]
            );
        }
    }
}
