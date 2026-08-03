use keyring::Entry;
use serde::Serialize;
use serde_json::Value;
use zeroize::Zeroize;

use crate::error::PlatformError;
use crate::rpc::WorkerRequest;

const SERVICE: &str = "com.gi-scenario-studio.wandb";
const MIN_SECRET_LENGTH: usize = 20;
const MAX_SECRET_LENGTH: usize = 512;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CredentialStatus {
    pub credential_id: String,
    pub stored: bool,
    pub storage: &'static str,
}

fn validate_credential_id(value: &str) -> Result<(), PlatformError> {
    let valid = !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'));
    if valid {
        Ok(())
    } else {
        Err(PlatformError::credential(
            "credential identifier must contain only letters, numbers, '.', '-', or '_'",
            false,
        ))
    }
}

fn entry(credential_id: &str) -> Result<Entry, PlatformError> {
    validate_credential_id(credential_id)?;
    Entry::new(SERVICE, credential_id)
        .map_err(|error| PlatformError::credential(error.to_string(), true))
}

fn store(credential_id: String, mut secret: String) -> Result<CredentialStatus, PlatformError> {
    if !(MIN_SECRET_LENGTH..=MAX_SECRET_LENGTH).contains(&secret.len()) {
        secret.zeroize();
        return Err(PlatformError::credential(
            "credential length is outside the accepted range",
            false,
        ));
    }
    let result = entry(&credential_id)?.set_password(&secret);
    secret.zeroize();
    result.map_err(|error| PlatformError::credential(error.to_string(), true))?;
    Ok(CredentialStatus {
        credential_id,
        stored: true,
        storage: "operating-system-keychain",
    })
}

pub(crate) fn prompt_and_store(credential_id: String) -> Result<CredentialStatus, PlatformError> {
    validate_credential_id(&credential_id)?;
    let secret = tinyfiledialogs::password_box(
        "GI Scenario Studio — W&B connection",
        "Enter your personal W&B API key. It will be stored directly in the operating-system keychain and will not be returned to the application interface.",
    )
    .ok_or_else(|| PlatformError::credential("credential entry was cancelled", false))?;
    store(credential_id, secret)
}

pub(crate) fn status(credential_id: String) -> Result<CredentialStatus, PlatformError> {
    let stored = match entry(&credential_id)?.get_password() {
        Ok(mut secret) => {
            secret.zeroize();
            true
        }
        Err(keyring::Error::NoEntry) => false,
        Err(error) => return Err(PlatformError::credential(error.to_string(), true)),
    };
    Ok(CredentialStatus {
        credential_id,
        stored,
        storage: "operating-system-keychain",
    })
}

pub(crate) fn delete(credential_id: String) -> Result<CredentialStatus, PlatformError> {
    match entry(&credential_id)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(CredentialStatus {
            credential_id,
            stored: false,
            storage: "operating-system-keychain",
        }),
        Err(error) => Err(PlatformError::credential(error.to_string(), true)),
    }
}

/// Resolve a W&B credential inside the trusted host and add it only to the
/// in-memory JSON-RPC request. The secret is never returned to the renderer,
/// placed in a process argument, or inherited as a worker environment variable.
pub(crate) fn inject_for_worker(request: &mut WorkerRequest) -> Result<(), PlatformError> {
    if request.method != "observability.connect" {
        return Ok(());
    }
    let params = request
        .params
        .get_or_insert_with(|| Value::Object(Default::default()))
        .as_object_mut()
        .ok_or_else(|| PlatformError::invalid_request("observability params must be an object"))?;
    if params.get("scope").and_then(Value::as_str) != Some("account") {
        return Ok(());
    }
    if params.contains_key("api_key") {
        return Err(PlatformError::invalid_request(
            "the renderer may not send an API key directly to the worker",
        ));
    }
    let credential_id = params
        .get("credential_id")
        .and_then(Value::as_str)
        .ok_or_else(|| PlatformError::credential("credential_id is required", false))?;
    let deployment = params
        .get("deployment")
        .and_then(Value::as_str)
        .ok_or_else(|| PlatformError::invalid_request("deployment is required"))?;
    let api_base_url = params
        .get("api_base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| PlatformError::invalid_request("api_base_url is required"))?;
    let endpoint = url::Url::parse(api_base_url)
        .map_err(|_| PlatformError::invalid_request("api_base_url is invalid"))?;
    if endpoint.scheme() != "https"
        || endpoint.host_str().is_none()
        || !endpoint.username().is_empty()
        || endpoint.password().is_some()
        || endpoint.query().is_some()
        || endpoint.fragment().is_some()
    {
        return Err(PlatformError::invalid_request(
            "api_base_url must be a credential-free HTTPS origin",
        ));
    }
    let host = endpoint
        .host_str()
        .ok_or_else(|| PlatformError::invalid_request("api_base_url host is required"))?;
    let endpoint_scope = match endpoint.port() {
        Some(port) => format!("{host}_{port}"),
        None => host.to_owned(),
    };
    let expected_id = format!("wandb-{deployment}-{endpoint_scope}");
    if credential_id != expected_id {
        return Err(PlatformError::credential(
            "credential identifier does not match the selected W&B endpoint",
            false,
        ));
    }
    let secret = entry(credential_id)?
        .get_password()
        .map_err(|error| PlatformError::credential(error.to_string(), true))?;
    params.insert("api_key".to_owned(), Value::String(secret));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::validate_credential_id;

    #[test]
    fn credential_identifiers_are_bounded_and_path_free() {
        assert!(validate_credential_id("wandb-main_01").is_ok());
        assert!(validate_credential_id("../wandb").is_err());
        assert!(validate_credential_id("space is unsafe").is_err());
    }
}
