use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::error::PlatformError;

pub(crate) const API_VERSION: &str = "1.0";
pub(crate) const MAX_MESSAGE_BYTES: usize = 8 * 1024 * 1024;
pub(crate) const DEFAULT_TIMEOUT_MS: u64 = 30_000;
pub(crate) const MAX_TIMEOUT_MS: u64 = 300_000;

const ALLOWED_METHODS: &[&str] = &[
    "system.ping",
    "schema.list",
    "schema.get",
    "project.create",
    "project.open",
    "project.get_state",
    "bundle.inspect",
    "bundle.import",
    "data_asset.add",
    "data_asset.download",
    "concept.preview",
    "concept.generate",
    "concept.update_object",
    "model.validate",
    "model.attest",
    "baseline.plan",
    "baseline.start",
    "analysis.plan",
    "analysis.start",
    "analysis.pause",
    "analysis.resume",
    "analysis.cancel",
    "results.query",
    "results.compare",
    "evidence.resolve",
    "export.validate",
    "export.create",
    "diagnostics.create_bundle",
    "demo.create",
];

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct WorkerRequest {
    pub method: String,
    #[serde(default)]
    pub params: Option<Value>,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

impl WorkerRequest {
    pub(crate) fn validate(&self) -> Result<(), PlatformError> {
        if !ALLOWED_METHODS.contains(&self.method.as_str()) {
            return Err(PlatformError::invalid_request(format!(
                "method '{}' is not in the desktop host allowlist",
                self.method
            )));
        }

        if let Some(params) = &self.params {
            if !params.is_object() && !params.is_null() {
                return Err(PlatformError::invalid_request(
                    "JSON-RPC params must be an object or null",
                ));
            }
        }

        Ok(())
    }

    pub(crate) fn timeout_ms(&self) -> u64 {
        self.timeout_ms
            .unwrap_or(DEFAULT_TIMEOUT_MS)
            .clamp(1_000, MAX_TIMEOUT_MS)
    }

    pub(crate) fn into_envelope(self, request_id: u64) -> Value {
        let mut envelope = Map::from_iter([
            ("jsonrpc".to_owned(), Value::String("2.0".to_owned())),
            ("id".to_owned(), Value::Number(request_id.into())),
            ("method".to_owned(), Value::String(self.method)),
        ]);
        envelope.insert(
            "params".to_owned(),
            self.params.unwrap_or_else(|| json!({})),
        );
        Value::Object(envelope)
    }
}

#[derive(Debug)]
pub(crate) enum InboundMessage {
    Response { request_key: String, value: Value },
    Notification(Value),
}

pub(crate) fn parse_inbound(bytes: &[u8]) -> Result<InboundMessage, PlatformError> {
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(PlatformError::protocol(format!(
            "worker message exceeds the {MAX_MESSAGE_BYTES}-byte protocol limit"
        )));
    }

    let text = std::str::from_utf8(bytes)
        .map_err(|error| PlatformError::protocol(format!("stdout was not UTF-8: {error}")))?;
    let text = text.trim();
    if text.is_empty() {
        return Err(PlatformError::protocol(
            "worker emitted an empty stdout line",
        ));
    }

    let value: Value = serde_json::from_str(text)
        .map_err(|error| PlatformError::protocol(format!("invalid JSON: {error}")))?;
    let object = value
        .as_object()
        .ok_or_else(|| PlatformError::protocol("JSON-RPC message was not an object"))?;

    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err(PlatformError::protocol(
            "worker message does not declare jsonrpc=2.0",
        ));
    }

    if let Some(id) = object.get("id") {
        if !object.contains_key("result") && !object.contains_key("error") {
            return Err(PlatformError::protocol(
                "JSON-RPC response has neither result nor error",
            ));
        }
        return Ok(InboundMessage::Response {
            request_key: request_key(id)?,
            value,
        });
    }

    if object.get("method").and_then(Value::as_str).is_none() {
        return Err(PlatformError::protocol(
            "JSON-RPC notification has no method",
        ));
    }

    Ok(InboundMessage::Notification(value))
}

pub(crate) fn request_key(id: &Value) -> Result<String, PlatformError> {
    match id {
        Value::String(value) => Ok(format!("s:{value}")),
        Value::Number(value) => Ok(format!("n:{value}")),
        _ => Err(PlatformError::protocol(
            "JSON-RPC id must be a string or number",
        )),
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WorkerEvent {
    pub sequence: u64,
    pub worker_generation: u64,
    pub payload: Value,
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{parse_inbound, InboundMessage, WorkerRequest, MAX_TIMEOUT_MS};

    #[test]
    fn parses_numeric_response_id() {
        let parsed = parse_inbound(br#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#)
            .expect("valid response");
        match parsed {
            InboundMessage::Response { request_key, value } => {
                assert_eq!(request_key, "n:7");
                assert_eq!(value["result"]["ok"], true);
            }
            InboundMessage::Notification(_) => panic!("expected response"),
        }
    }

    #[test]
    fn parses_notification_without_exposing_stdout_text() {
        let parsed = parse_inbound(
            br#"{"jsonrpc":"2.0","method":"solver_job.completed","params":{"sequence":4}}"#,
        )
        .expect("valid notification");
        match parsed {
            InboundMessage::Notification(value) => {
                assert_eq!(value["method"], "solver_job.completed")
            }
            InboundMessage::Response { .. } => panic!("expected notification"),
        }
    }

    #[test]
    fn rejects_stdout_logs_as_protocol_violations() {
        assert!(parse_inbound(b"loading project").is_err());
    }

    #[test]
    fn validates_allowlisted_methods_and_object_params() {
        let valid = WorkerRequest {
            method: "project.open".to_owned(),
            params: Some(json!({"path": "/project"})),
            timeout_ms: None,
        };
        assert!(valid.validate().is_ok());

        let unknown = WorkerRequest {
            method: "shell.execute".to_owned(),
            params: None,
            timeout_ms: None,
        };
        assert!(unknown.validate().is_err());
    }

    #[test]
    fn clamps_untrusted_timeout() {
        let request = WorkerRequest {
            method: "project.get_state".to_owned(),
            params: None,
            timeout_ms: Some(u64::MAX),
        };
        assert_eq!(request.timeout_ms(), MAX_TIMEOUT_MS);
    }
}
