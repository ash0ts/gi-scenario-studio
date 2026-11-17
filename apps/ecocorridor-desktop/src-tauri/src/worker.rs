use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Component, Path};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use keyring::Entry;
use scenario_host::{
    verify_release_manifest, AppProfile, ReleaseManifestVerification, SecretMaterial,
    VerifiedResource, WorkerCommand, WorkerResources,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::oneshot;
use zeroize::Zeroize;

const RESPONSE_TIMEOUT_MS: u64 = 120_000;
const REVIEW_BRIEF_RESPONSE_TIMEOUT_MS: u64 = 420_000;
const INSTALLED_HEALTH_TIMEOUT: Duration = Duration::from_secs(120);
const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(7);
const WORKER_EXIT_POLL_INTERVAL: Duration = Duration::from_millis(20);

#[derive(Debug, Serialize)]
pub(crate) struct InstalledHealthReceipt {
    pub schema_version: &'static str,
    pub health_run_id: String,
    pub application_id: String,
    pub application_version: String,
    pub source_commit_sha: String,
    pub release_target: String,
    pub release_manifest_sha256: String,
    pub release_manifest_verified: bool,
    pub worker_started: bool,
    pub system_ping: InstalledPingReceipt,
    pub guided_open: InstalledGuidedReceipt,
    pub keychain: InstalledKeychainReceipt,
}

#[derive(Debug, Serialize)]
pub(crate) struct InstalledPingReceipt {
    pub api_version: String,
    pub schema_version: String,
    pub engine_configured: bool,
    pub method_count: usize,
}

#[derive(Debug, Serialize)]
pub(crate) struct InstalledGuidedReceipt {
    pub state: String,
    pub evidence_manifest_sha256: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct InstalledKeychainReceipt {
    pub write_succeeded: bool,
    pub exact_read_succeeded: bool,
    pub delete_succeeded: bool,
    pub post_delete_absent: bool,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum CredentialKind {
    Wandb,
    ModelProvider,
}

impl CredentialKind {
    fn account_prefix(self) -> &'static str {
        match self {
            Self::Wandb => "wandb",
            Self::ModelProvider => "model-provider",
        }
    }

    fn prompt(self) -> (&'static str, &'static str) {
        match self {
            Self::Wandb => (
                "Connect W&B",
                "Enter the W&B personal or service-account API key. It is stored only in the operating-system keychain.",
            ),
            Self::ModelProvider => (
                "Connect evidence assistant",
                "Enter the model-provider API key. It is stored separately in the operating-system keychain.",
            ),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct HostError {
    code: String,
    user_message: String,
    retryable: bool,
    object_reference: Option<String>,
    remediation: Option<String>,
}

impl HostError {
    pub(crate) fn new(
        code: impl Into<String>,
        user_message: impl Into<String>,
        retryable: bool,
    ) -> Self {
        Self {
            code: code.into(),
            user_message: user_message.into(),
            retryable,
            object_reference: None,
            remediation: None,
        }
    }

    fn resource() -> Self {
        Self::new(
            "RESOURCE_MANIFEST_UNAVAILABLE",
            "Verified EcoCorridor engine and guided evidence resources are not installed. Use the synthetic public replay or install a signed release.",
            false,
        )
    }

    pub(crate) fn staging() -> Self {
        Self::new(
            "APP_DATA_STAGING_FAILED",
            "The selected resource could not be copied into protected EcoCorridor application storage.",
            true,
        )
    }
}

fn read_worker_output_frame<R: BufRead>(
    reader: &mut R,
    maximum_bytes: usize,
) -> Result<Option<Value>, HostError> {
    let read_limit = maximum_bytes.saturating_add(2);
    let mut encoded = Vec::with_capacity(read_limit.min(64 * 1024));
    let count = reader
        .take(read_limit as u64)
        .read_until(b'\n', &mut encoded)
        .map_err(|_| {
            HostError::new(
                "WORKER_OUTPUT_READ_FAILED",
                "The scientific worker response channel could not be read.",
                true,
            )
        })?;
    if count == 0 {
        return Ok(None);
    }
    let terminated = encoded.last() == Some(&b'\n');
    if terminated {
        encoded.pop();
        if encoded.last() == Some(&b'\r') {
            encoded.pop();
        }
    }
    if encoded.len() > maximum_bytes {
        return Err(HostError::new(
            "WORKER_OUTPUT_TOO_LARGE",
            "The scientific worker emitted a response larger than the desktop security limit.",
            false,
        ));
    }
    if !terminated {
        return Err(HostError::new(
            "WORKER_OUTPUT_MALFORMED",
            "The scientific worker emitted an incomplete response frame.",
            true,
        ));
    }
    serde_json::from_slice::<Value>(&encoded)
        .map(Some)
        .map_err(|_| {
            HostError::new(
                "WORKER_OUTPUT_MALFORMED",
                "The scientific worker emitted a malformed response frame.",
                true,
            )
        })
}

/// Close the worker request pipe first, then escalate only after a bounded wait.
///
/// `true` means EOF was sufficient. `false` means the host had to kill the
/// direct worker after the grace period. Solver descendants are handled by the
/// worker's own shutdown path before that escalation.
fn close_stdin_wait_or_kill(
    mut child: Child,
    stdin: Option<ChildStdin>,
    timeout: Duration,
) -> std::io::Result<bool> {
    drop(stdin);
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return Ok(true),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(
                    WORKER_EXIT_POLL_INTERVAL
                        .min(deadline.saturating_duration_since(Instant::now())),
                );
            }
            Ok(None) => break,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        }
    }
    if child.try_wait()?.is_some() {
        return Ok(true);
    }
    child.kill()?;
    child.wait()?;
    Ok(false)
}

fn worker_response_error(value: &Value) -> HostError {
    const FALLBACK_MESSAGE: &str = "The scientific worker rejected the request.";

    let data = value.get("data").and_then(Value::as_object);
    let code = data
        .and_then(|item| item.get("code"))
        .and_then(Value::as_str)
        .filter(|item| valid_worker_error_code(item))
        .unwrap_or("WORKER_REQUEST_FAILED");
    let user_message = data
        .and_then(|item| item.get("user_message"))
        .and_then(Value::as_str)
        .and_then(|item| safe_worker_text(item, 4_096))
        .or_else(|| {
            value
                .get("message")
                .and_then(Value::as_str)
                .and_then(|item| safe_worker_text(item, 4_096))
        })
        .unwrap_or_else(|| FALLBACK_MESSAGE.to_owned());
    let retryable = data
        .and_then(|item| item.get("retryable"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let object_reference = data
        .and_then(|item| item.get("object_reference"))
        .and_then(Value::as_str)
        .and_then(safe_worker_object_reference);
    let remediation = data
        .and_then(|item| item.get("remediation"))
        .and_then(Value::as_str)
        .and_then(|item| safe_worker_text(item, 4_096));

    HostError {
        code: code.to_owned(),
        user_message,
        retryable,
        object_reference,
        remediation,
    }
}

fn valid_worker_error_code(value: &str) -> bool {
    (3..=64).contains(&value.len())
        && value
            .bytes()
            .next()
            .is_some_and(|byte| byte.is_ascii_uppercase())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
}

fn safe_worker_text(value: &str, maximum_bytes: usize) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty()
        || trimmed.len() > maximum_bytes
        || trimmed
            .chars()
            .any(|character| character.is_control() && !matches!(character, '\n' | '\t'))
        || contains_local_path(trimmed)
    {
        return None;
    }
    Some(trimmed.to_owned())
}

fn safe_worker_object_reference(value: &str) -> Option<String> {
    let reference = safe_worker_text(value, 512)?;
    if reference.starts_with('~')
        || reference.contains('\\')
        || Path::new(&reference).components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return None;
    }
    Some(reference)
}

fn contains_local_path(value: &str) -> bool {
    value.split_whitespace().any(|part| {
        let token = part.trim_matches(|character: char| {
            matches!(
                character,
                ',' | ';' | ':' | '(' | ')' | '[' | ']' | '{' | '}' | '\'' | '"'
            )
        });
        let bytes = token.as_bytes();
        token.starts_with('/')
            || token.starts_with("~/")
            || token.starts_with("file://")
            || token.starts_with("\\\\")
            || (bytes.len() >= 3
                && bytes[0].is_ascii_alphabetic()
                && bytes[1] == b':'
                && matches!(bytes[2], b'/' | b'\\'))
    })
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct WorkerRequest {
    pub method: String,
    #[serde(default)]
    pub params: Value,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum WorkerLifecycle {
    Stopped,
    Starting,
    Ready,
    Faulted,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WorkerStatus {
    lifecycle: WorkerLifecycle,
    api_version: String,
    generation: u64,
    resource_manifest_verified: bool,
    last_error: Option<HostError>,
}

struct Runtime {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    status: WorkerStatus,
}

impl Runtime {
    fn new(api_version: String) -> Self {
        Self {
            child: None,
            stdin: None,
            status: WorkerStatus {
                lifecycle: WorkerLifecycle::Stopped,
                api_version,
                generation: 0,
                resource_manifest_verified: false,
                last_error: None,
            },
        }
    }
}

type Pending = oneshot::Sender<Result<Value, HostError>>;

pub(crate) struct WorkerManager {
    profile: AppProfile,
    runtime: Mutex<Runtime>,
    pending: Mutex<HashMap<u64, Pending>>,
    next_id: AtomicU64,
}

impl WorkerManager {
    pub(crate) fn new(profile: AppProfile) -> Self {
        Self {
            runtime: Mutex::new(Runtime::new(profile.api_version.clone())),
            profile,
            pending: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
        }
    }

    pub(crate) fn status(&self) -> WorkerStatus {
        self.runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .status
            .clone()
    }

    pub(crate) fn ensure_started(&self, app: &AppHandle) -> Result<WorkerStatus, HostError> {
        {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if runtime.status.lifecycle == WorkerLifecycle::Ready && runtime.child.is_some() {
                return Ok(runtime.status.clone());
            }
            if runtime.status.lifecycle == WorkerLifecycle::Starting {
                return Err(HostError::new(
                    "WORKER_START_IN_PROGRESS",
                    "The scientific worker is already starting.",
                    true,
                ));
            }
            runtime.status.lifecycle = WorkerLifecycle::Starting;
            runtime.status.last_error = None;
        }
        self.emit_status(app);

        let resource_root = app
            .path()
            .resolve("release", BaseDirectory::Resource)
            .map_err(|_| HostError::resource())?;
        let manifest = resource_root.join("release-manifest.json");
        let signature = resource_root.join("release-manifest.sig");
        if !manifest.is_file() || !signature.is_file() {
            return self.fail_start(app, HostError::resource());
        }
        let verified = verify_release_manifest(ReleaseManifestVerification {
            manifest_path: &manifest,
            signature_path: &signature,
            resource_root: &resource_root,
            expected_application_id: &self.profile.application_id,
            expected_application_version: &self.profile.application_version,
            expected_source_commit_sha: &self.profile.release_source_commit_sha,
            expected_target: &self.profile.release_target,
            public_key_hex: &self.profile.release_public_key_hex,
        })
        .map_err(|_| HostError::resource())?;
        let expected_worker =
            std::fs::canonicalize(resource_root.join(&self.profile.worker_resource))
                .map_err(|_| HostError::resource())?;
        let expected_engine =
            std::fs::canonicalize(resource_root.join(&self.profile.engine_resource))
                .map_err(|_| HostError::resource())?;
        let expected_engine_manifest =
            std::fs::canonicalize(resource_root.join(&self.profile.engine_manifest_resource))
                .map_err(|_| HostError::resource())?;
        let worker = verified
            .iter()
            .find(|entry| entry.executable && entry.path == expected_worker)
            .ok_or_else(HostError::resource)?;
        let engine = verified
            .iter()
            .find(|entry| entry.executable && entry.path == expected_engine)
            .ok_or_else(HostError::resource)?;
        let engine_manifest = verified
            .iter()
            .find(|entry| !entry.executable && entry.path == expected_engine_manifest)
            .ok_or_else(HostError::resource)?;
        let guided = resolve_guided_resources(
            &resource_root,
            &verified,
            &self.profile.guided_projection_resource,
            &self.profile.guided_manifest_resource,
        )?;

        let app_data_root = app.path().app_data_dir().map_err(|_| {
            HostError::new(
                "APP_DATA_UNAVAILABLE",
                "The EcoCorridor application-data directory is unavailable.",
                false,
            )
        })?;
        std::fs::create_dir_all(&app_data_root).map_err(|_| {
            HostError::new(
                "APP_DATA_UNAVAILABLE",
                "The EcoCorridor application-data directory could not be created.",
                false,
            )
        })?;
        let command = WorkerCommand::new(
            worker.path.clone(),
            WorkerResources {
                engine,
                engine_manifest,
                guided_projection: guided.projection,
                guided_manifest: guided.manifest,
                guided_evidence_archive: guided.archive,
            },
            &self.profile.api_version,
            &app_data_root,
            sanitized_environment(),
        )
        .map_err(|_| HostError::resource())?;

        let mut process = Command::new(&command.executable);
        process
            .args(&command.arguments)
            .env_clear()
            .envs(&command.environment)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = process.spawn().map_err(|_| {
            HostError::new(
                "WORKER_START_FAILED",
                "The verified scientific worker could not start.",
                true,
            )
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            HostError::new(
                "WORKER_PROTOCOL_UNAVAILABLE",
                "The scientific worker did not open its request channel.",
                true,
            )
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            HostError::new(
                "WORKER_PROTOCOL_UNAVAILABLE",
                "The scientific worker did not open its response channel.",
                true,
            )
        })?;
        let generation = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            runtime.status.generation = runtime.status.generation.saturating_add(1);
            runtime.status.lifecycle = WorkerLifecycle::Ready;
            runtime.status.resource_manifest_verified = true;
            runtime.status.last_error = None;
            runtime.stdin = Some(stdin);
            runtime.child = Some(child);
            runtime.status.generation
        };
        self.emit_status(app);

        let event_app = app.clone();
        let maximum_output_bytes = self.profile.maximum_rpc_bytes;
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut terminal_error = HostError::new(
                "WORKER_TERMINATED",
                "The scientific worker stopped. Completed direct evidence remains saved.",
                true,
            );
            loop {
                let value = match read_worker_output_frame(&mut reader, maximum_output_bytes) {
                    Ok(Some(value)) => value,
                    Ok(None) => break,
                    Err(error) => {
                        terminal_error = error;
                        break;
                    }
                };
                let manager = event_app.state::<WorkerManager>();
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    if let Some(sender) = manager
                        .pending
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .remove(&id)
                    {
                        let result = if let Some(error) = value.get("error") {
                            Err(worker_response_error(error))
                        } else {
                            Ok(value.get("result").cloned().unwrap_or(Value::Null))
                        };
                        let _ = sender.send(result);
                    }
                } else {
                    let _ = event_app.emit("ecocorridor://worker-event", value);
                }
            }
            let manager = event_app.state::<WorkerManager>();
            manager.mark_faulted(&event_app, generation, terminal_error);
        });
        Ok(self.status())
    }

    fn fail_start(&self, app: &AppHandle, error: HostError) -> Result<WorkerStatus, HostError> {
        self.mark_faulted(app, self.status().generation, error.clone());
        Err(error)
    }

    fn mark_faulted(&self, app: &AppHandle, generation: u64, error: HostError) {
        let should_emit = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if runtime.status.generation != generation {
                false
            } else {
                let child = runtime.child.take();
                let stdin = runtime.stdin.take();
                if let Some(child) = child {
                    let _ = close_stdin_wait_or_kill(child, stdin, WORKER_SHUTDOWN_GRACE);
                } else {
                    drop(stdin);
                }
                runtime.status.lifecycle = WorkerLifecycle::Faulted;
                runtime.status.last_error = Some(error.clone());
                true
            }
        };
        if should_emit {
            let pending = std::mem::take(
                &mut *self
                    .pending
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
            );
            for (_, sender) in pending {
                let _ = sender.send(Err(error.clone()));
            }
            self.emit_status(app);
        }
    }

    pub(crate) async fn request(
        &self,
        app: &AppHandle,
        mut request: WorkerRequest,
    ) -> Result<Value, HostError> {
        let default_timeout_ms = if request.method == "assistant.review_brief.prepare" {
            REVIEW_BRIEF_RESPONSE_TIMEOUT_MS
        } else {
            RESPONSE_TIMEOUT_MS
        };
        let request_bytes = serde_json::to_vec(&request.params)
            .map_err(|_| {
                HostError::new(
                    "RPC_ENCODING_FAILED",
                    "The request could not be encoded.",
                    false,
                )
            })?
            .len();
        self.profile
            .authorize_rpc(&request.method, request_bytes)
            .map_err(|_| {
                HostError::new(
                    "RPC_NOT_ALLOWED",
                    "That operation is not allowed by the EcoCorridor desktop host.",
                    false,
                )
            })?;
        self.ensure_started(app)?;
        self.inject_credential(&request.method, &mut request.params)?;

        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let mut envelope = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": request.method,
            "params": request.params,
        });
        let mut bytes = serde_json::to_vec(&envelope).map_err(|_| {
            HostError::new(
                "RPC_ENCODING_FAILED",
                "The request could not be encoded.",
                false,
            )
        })?;
        zeroize_json(&mut envelope);
        if bytes.len() > self.profile.maximum_rpc_bytes {
            bytes.zeroize();
            return Err(HostError::new(
                "RPC_TOO_LARGE",
                "The request exceeds the desktop security limit.",
                false,
            ));
        }
        bytes.push(b'\n');
        let (sender, receiver) = oneshot::channel();
        self.pending
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(id, sender);
        let write_result = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let stdin = runtime.stdin.as_mut().ok_or_else(|| {
                HostError::new(
                    "WORKER_UNAVAILABLE",
                    "The scientific worker is unavailable.",
                    true,
                )
            })?;
            stdin.write_all(&bytes).and_then(|_| stdin.flush())
        };
        bytes.zeroize();
        if write_result.is_err() {
            self.pending
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .remove(&id);
            return Err(HostError::new(
                "WORKER_WRITE_FAILED",
                "The scientific worker did not accept the request.",
                true,
            ));
        }
        let timeout = request
            .timeout_ms
            .unwrap_or(default_timeout_ms)
            .clamp(1_000, 30 * 60 * 1_000);
        tokio::time::timeout(std::time::Duration::from_millis(timeout), receiver)
            .await
            .map_err(|_| {
                self.pending
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .remove(&id);
                HostError::new(
                    "WORKER_RESPONSE_TIMEOUT",
                    "The operation is still running or the worker stopped responding.",
                    true,
                )
            })?
            .map_err(|_| {
                HostError::new(
                    "WORKER_TERMINATED",
                    "The scientific worker stopped before responding.",
                    true,
                )
            })?
    }

    fn inject_credential(&self, method: &str, params: &mut Value) -> Result<(), HostError> {
        let kind = if matches!(
            method,
            "observability.preflight" | "observability.synthetic_trace.preflight"
        ) {
            Some(CredentialKind::Wandb)
        } else if matches!(
            method,
            "assistant.configure" | "assistant.review_brief.configure_synthetic"
        ) {
            Some(CredentialKind::ModelProvider)
        } else {
            None
        };
        let Some(kind) = kind else { return Ok(()) };
        let credential_id = params
            .get("credential_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                HostError::new(
                    "CREDENTIAL_HANDLE_REQUIRED",
                    "Choose a stored credential before using this operation.",
                    false,
                )
            })?;
        validate_credential_id(credential_id)?;
        let mut password = credential_entry(&self.profile, credential_id, kind)?
            .get_password()
            .map_err(|_| {
                HostError::new(
                    "CREDENTIAL_UNAVAILABLE",
                    "The requested credential is not available in the operating-system keychain.",
                    false,
                )
            })?;
        let guarded = SecretMaterial::new(password.as_bytes()).map_err(|_| {
            HostError::new(
                "CREDENTIAL_INVALID",
                "The stored credential has an invalid shape.",
                false,
            )
        })?;
        password.zeroize();
        let object = params.as_object_mut().ok_or_else(|| {
            HostError::new(
                "RPC_PARAMS_INVALID",
                "The request parameters are invalid.",
                false,
            )
        })?;
        object.remove("credential_id");
        object.insert(
            "api_key".into(),
            Value::String(
                String::from_utf8(guarded.expose_to_worker_initialization().to_vec()).map_err(
                    |_| {
                        HostError::new(
                            "CREDENTIAL_INVALID",
                            "The stored credential has an invalid encoding.",
                            false,
                        )
                    },
                )?,
            ),
        );
        Ok(())
    }

    pub(crate) fn prompt_and_store_credential(
        &self,
        credential_id: &str,
        kind: CredentialKind,
    ) -> Result<(), HostError> {
        validate_credential_id(credential_id)?;
        let (title, message) = kind.prompt();
        let mut secret = tinyfiledialogs::password_box(title, message).ok_or_else(|| {
            HostError::new(
                "CREDENTIAL_ENTRY_CANCELLED",
                "Credential entry was cancelled.",
                false,
            )
        })?;
        SecretMaterial::new(secret.as_bytes()).map_err(|_| {
            secret.zeroize();
            HostError::new(
                "CREDENTIAL_INVALID",
                "The credential has an invalid shape.",
                false,
            )
        })?;
        let result = credential_entry(&self.profile, credential_id, kind)?
            .set_password(&secret)
            .map_err(|_| {
                HostError::new(
                    "KEYCHAIN_WRITE_FAILED",
                    "The credential could not be stored in the operating-system keychain.",
                    true,
                )
            });
        secret.zeroize();
        result
    }

    pub(crate) fn credential_status(
        &self,
        credential_id: &str,
        kind: CredentialKind,
    ) -> Result<bool, HostError> {
        validate_credential_id(credential_id)?;
        match credential_entry(&self.profile, credential_id, kind)?.get_password() {
            Ok(mut value) => {
                value.zeroize();
                Ok(true)
            }
            Err(keyring::Error::NoEntry) => Ok(false),
            Err(_) => Err(HostError::new(
                "KEYCHAIN_READ_FAILED",
                "The operating-system keychain could not be read.",
                true,
            )),
        }
    }

    pub(crate) fn delete_credential(
        &self,
        credential_id: &str,
        kind: CredentialKind,
    ) -> Result<(), HostError> {
        validate_credential_id(credential_id)?;
        match credential_entry(&self.profile, credential_id, kind)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(HostError::new(
                "KEYCHAIN_DELETE_FAILED",
                "The credential could not be removed from the operating-system keychain.",
                true,
            )),
        }
    }

    pub(crate) fn stop(&self) -> Result<WorkerStatus, HostError> {
        let mut runtime = self
            .runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        runtime.status.generation = runtime.status.generation.saturating_add(1);
        let child = runtime.child.take();
        let stdin = runtime.stdin.take();
        let stop_error = if let Some(child) = child {
            close_stdin_wait_or_kill(child, stdin, WORKER_SHUTDOWN_GRACE)
                .err()
                .map(|_| {
                    HostError::new(
                        "WORKER_STOP_FAILED",
                        "The scientific worker could not be stopped cleanly.",
                        true,
                    )
                })
        } else {
            drop(stdin);
            None
        };
        if let Some(error) = &stop_error {
            runtime.status.lifecycle = WorkerLifecycle::Faulted;
            runtime.status.last_error = Some(error.clone());
        } else {
            runtime.status.lifecycle = WorkerLifecycle::Stopped;
            runtime.status.last_error = None;
        }
        runtime.status.resource_manifest_verified = false;
        let status = runtime.status.clone();
        drop(runtime);
        let pending_error = stop_error.clone().unwrap_or_else(|| {
            HostError::new(
                "WORKER_STOPPED",
                "The scientific worker stopped before responding.",
                true,
            )
        });
        let pending = std::mem::take(
            &mut *self
                .pending
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        );
        for (_, sender) in pending {
            let _ = sender.send(Err(pending_error.clone()));
        }
        match stop_error {
            Some(error) => Err(error),
            None => Ok(status),
        }
    }

    fn emit_status(&self, app: &AppHandle) {
        let _ = app.emit("ecocorridor://worker-status", self.status());
    }
}

#[derive(Debug, Deserialize)]
struct GuidedManifestEnvelope {
    evidence_bundle: GuidedBundleDeclaration,
}

#[derive(Debug, Deserialize)]
struct GuidedBundleDeclaration {
    filename: String,
    sha256: String,
    byte_size: u64,
}

struct GuidedResources<'a> {
    projection: &'a VerifiedResource,
    manifest: &'a VerifiedResource,
    archive: &'a VerifiedResource,
}

fn resolve_guided_resources<'a>(
    resource_root: &Path,
    verified: &'a [VerifiedResource],
    projection_resource: &str,
    manifest_resource: &str,
) -> Result<GuidedResources<'a>, HostError> {
    let canonical_root = fs::canonicalize(resource_root).map_err(|_| HostError::resource())?;
    let projection = verified_resource(&canonical_root, verified, projection_resource, false)?;
    let manifest = verified_resource(&canonical_root, verified, manifest_resource, false)?;
    let metadata = fs::symlink_metadata(&manifest.path).map_err(|_| HostError::resource())?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > 8 * 1024 * 1024
        || sha256_file(&manifest.path)? != manifest.sha256
    {
        return Err(HostError::resource());
    }
    let bytes = fs::read(&manifest.path).map_err(|_| HostError::resource())?;
    let declaration: GuidedManifestEnvelope =
        serde_json::from_slice(&bytes).map_err(|_| HostError::resource())?;
    let bundle = declaration.evidence_bundle;
    if !safe_basename(&bundle.filename) || !lowercase_sha256(&bundle.sha256) {
        return Err(HostError::resource());
    }
    let archive_relative = format!("guided/{}", bundle.filename);
    let archive = verified_resource(&canonical_root, verified, &archive_relative, false)?;
    let archive_metadata =
        fs::symlink_metadata(&archive.path).map_err(|_| HostError::resource())?;
    if !archive_metadata.file_type().is_file()
        || archive_metadata.file_type().is_symlink()
        || archive_metadata.len() != bundle.byte_size
        || archive.sha256 != bundle.sha256
    {
        return Err(HostError::resource());
    }
    Ok(GuidedResources {
        projection,
        manifest,
        archive,
    })
}

fn verified_resource<'a>(
    canonical_root: &Path,
    verified: &'a [VerifiedResource],
    relative: &str,
    executable: bool,
) -> Result<&'a VerifiedResource, HostError> {
    let expected =
        fs::canonicalize(canonical_root.join(relative)).map_err(|_| HostError::resource())?;
    if !expected.starts_with(canonical_root) {
        return Err(HostError::resource());
    }
    verified
        .iter()
        .find(|entry| entry.path == expected && entry.executable == executable)
        .ok_or_else(HostError::resource)
}

fn safe_basename(value: &str) -> bool {
    if value.is_empty() || value.len() > 255 || value.contains('/') || value.contains('\\') {
        return false;
    }
    let mut components = Path::new(value).components();
    matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none()
}

fn lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn sha256_file(path: &Path) -> Result<String, HostError> {
    use sha2::{Digest, Sha256};

    let file = fs::File::open(path).map_err(|_| HostError::resource())?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|_| HostError::resource())?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// Exercise the security boundary from the installed native executable.
///
/// This deliberately does not use renderer IPC. It verifies the signed
/// resource inventory, launches the bundled worker without a shell, and asks
/// the authoritative worker for both its versioned ping and the pinned guided
/// replay. The keychain probe uses a fresh, non-production UUID account and
/// deletes its randomized test value before the receipt is written.
pub(crate) fn run_installed_health_check(
    profile: &AppProfile,
    resource_root: &Path,
    app_data_root: &Path,
) -> Result<InstalledHealthReceipt, HostError> {
    profile.validate().map_err(|_| HostError::resource())?;
    if profile.release_public_key_hex.is_empty() || profile.release_source_commit_sha.is_empty() {
        return Err(HostError::resource());
    }
    let resource_root = fs::canonicalize(resource_root).map_err(|_| HostError::resource())?;
    let manifest = resource_root.join("release-manifest.json");
    let signature = resource_root.join("release-manifest.sig");
    let manifest_sha256 = sha256_file(&manifest)?;
    let verified = verify_release_manifest(ReleaseManifestVerification {
        manifest_path: &manifest,
        signature_path: &signature,
        resource_root: &resource_root,
        expected_application_id: &profile.application_id,
        expected_application_version: &profile.application_version,
        expected_source_commit_sha: &profile.release_source_commit_sha,
        expected_target: &profile.release_target,
        public_key_hex: &profile.release_public_key_hex,
    })
    .map_err(|_| HostError::resource())?;

    let worker = verified_resource(&resource_root, &verified, &profile.worker_resource, true)?;
    let engine = verified_resource(&resource_root, &verified, &profile.engine_resource, true)?;
    let engine_manifest = verified_resource(
        &resource_root,
        &verified,
        &profile.engine_manifest_resource,
        false,
    )?;
    let guided = resolve_guided_resources(
        &resource_root,
        &verified,
        &profile.guided_projection_resource,
        &profile.guided_manifest_resource,
    )?;
    let app_data_root = fs::canonicalize(app_data_root).map_err(|_| HostError::resource())?;
    let command = WorkerCommand::new(
        worker.path.clone(),
        WorkerResources {
            engine,
            engine_manifest,
            guided_projection: guided.projection,
            guided_manifest: guided.manifest,
            guided_evidence_archive: guided.archive,
        },
        &profile.api_version,
        &app_data_root,
        sanitized_environment(),
    )
    .map_err(|_| HostError::resource())?;

    let mut process = Command::new(&command.executable);
    process
        .args(&command.arguments)
        .env_clear()
        .envs(&command.environment)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = process.spawn().map_err(|_| {
        HostError::new(
            "WORKER_START_FAILED",
            "The verified scientific worker could not start.",
            true,
        )
    })?;
    let mut stdin = child.stdin.take().ok_or_else(|| {
        HostError::new(
            "WORKER_PROTOCOL_UNAVAILABLE",
            "The scientific worker did not open its request channel.",
            true,
        )
    })?;
    let stdout = child.stdout.take().ok_or_else(|| {
        HostError::new(
            "WORKER_PROTOCOL_UNAVAILABLE",
            "The scientific worker did not open its response channel.",
            true,
        )
    })?;
    for request in [
        json!({"jsonrpc": "2.0", "id": 1, "method": "system.ping", "params": {}}),
        json!({"jsonrpc": "2.0", "id": 2, "method": "connectivity.guided.open", "params": {}}),
    ] {
        let mut encoded = serde_json::to_vec(&request).map_err(|_| HostError::resource())?;
        encoded.push(b'\n');
        let result = stdin.write_all(&encoded).and_then(|_| stdin.flush());
        encoded.zeroize();
        result.map_err(|_| {
            HostError::new(
                "WORKER_WRITE_FAILED",
                "The scientific worker did not accept the installed-host health request.",
                true,
            )
        })?;
    }
    drop(stdin);

    let (sender, receiver) = mpsc::sync_channel::<Result<String, ()>>(8);
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let value = line.map_err(|_| ());
            if sender.send(value).is_err() {
                break;
            }
        }
    });
    let deadline = Instant::now() + INSTALLED_HEALTH_TIMEOUT;
    let mut responses = HashMap::<u64, Value>::new();
    while responses.len() < 2 {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            let _ = child.kill();
            let _ = child.wait();
            return Err(HostError::new(
                "WORKER_RESPONSE_TIMEOUT",
                "The installed scientific worker did not complete its bounded health check.",
                true,
            ));
        }
        let line = receiver.recv_timeout(remaining).map_err(|_| {
            HostError::new(
                "WORKER_TERMINATED",
                "The installed scientific worker stopped before completing its health check.",
                true,
            )
        })?;
        let line = line.map_err(|_| HostError::resource())?;
        if line.len() > profile.maximum_rpc_bytes {
            let _ = child.kill();
            let _ = child.wait();
            return Err(HostError::resource());
        }
        let frame: Value = serde_json::from_str(&line).map_err(|_| HostError::resource())?;
        if let Some(id) = frame.get("id").and_then(Value::as_u64) {
            if matches!(id, 1 | 2) && responses.insert(id, frame).is_some() {
                let _ = child.kill();
                let _ = child.wait();
                return Err(HostError::resource());
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();

    let ping = validate_installed_ping(responses.remove(&1).ok_or_else(HostError::resource)?)?;
    let guided_open =
        validate_installed_guided(responses.remove(&2).ok_or_else(HostError::resource)?)?;
    let keychain = probe_isolated_keychain_roundtrip(profile)?;
    Ok(InstalledHealthReceipt {
        schema_version: "ecocorridor-installed-host-health/v1.0",
        health_run_id: uuid::Uuid::new_v4().to_string(),
        application_id: profile.application_id.clone(),
        application_version: profile.application_version.clone(),
        source_commit_sha: profile.release_source_commit_sha.clone(),
        release_target: profile.release_target.clone(),
        release_manifest_sha256: manifest_sha256,
        release_manifest_verified: true,
        worker_started: true,
        system_ping: ping,
        guided_open,
        keychain,
    })
}

fn validate_installed_ping(frame: Value) -> Result<InstalledPingReceipt, HostError> {
    if frame.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || frame.get("id").and_then(Value::as_u64) != Some(1)
        || frame.get("error").is_some()
    {
        return Err(HostError::resource());
    }
    let result = frame
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(HostError::resource)?;
    let methods = result
        .get("methods")
        .and_then(Value::as_array)
        .ok_or_else(HostError::resource)?;
    if result.get("api_version").and_then(Value::as_str) != Some("1.0")
        || result.get("application").and_then(Value::as_str) != Some("EcoCorridor Scenario Studio")
        || result.get("schema_version").and_then(Value::as_str) != Some("1.0")
        || result.get("engine_configured").and_then(Value::as_bool) != Some(true)
        || !methods
            .iter()
            .any(|value| value.as_str() == Some("system.ping"))
    {
        return Err(HostError::resource());
    }
    Ok(InstalledPingReceipt {
        api_version: "1.0".into(),
        schema_version: "1.0".into(),
        engine_configured: true,
        method_count: methods.len(),
    })
}

fn validate_installed_guided(frame: Value) -> Result<InstalledGuidedReceipt, HostError> {
    if frame.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || frame.get("id").and_then(Value::as_u64) != Some(2)
        || frame.get("error").is_some()
    {
        return Err(HostError::resource());
    }
    let result = frame
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(HostError::resource)?;
    if result.get("api_version").and_then(Value::as_str) != Some("1.0")
        || result.get("method").and_then(Value::as_str) != Some("connectivity.guided.open")
    {
        return Err(HostError::resource());
    }
    let data = result
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(HostError::resource)?;
    let state = data
        .get("state")
        .and_then(Value::as_str)
        .ok_or_else(HostError::resource)?;
    let evidence_manifest_sha256 = data
        .get("evidence_manifest_sha256")
        .and_then(Value::as_str)
        .filter(|value| lowercase_sha256(value))
        .ok_or_else(HostError::resource)?;
    if state != "VERIFIED_FIXTURE_REPLAY" {
        return Err(HostError::resource());
    }
    Ok(InstalledGuidedReceipt {
        state: state.into(),
        evidence_manifest_sha256: evidence_manifest_sha256.into(),
    })
}

fn probe_isolated_keychain_roundtrip(
    profile: &AppProfile,
) -> Result<InstalledKeychainReceipt, HostError> {
    let account = format!("installed-health-self-test:{}", uuid::Uuid::new_v4());
    let entry = Entry::new(&profile.keychain_service, &account).map_err(|_| {
        HostError::new(
            "KEYCHAIN_UNAVAILABLE",
            "The operating-system keychain is unavailable during installed-host verification.",
            true,
        )
    })?;
    match entry.get_password() {
        Err(keyring::Error::NoEntry) => {}
        Ok(mut unexpected) => {
            unexpected.zeroize();
            return Err(installed_keychain_error());
        }
        Err(_) => return Err(installed_keychain_error()),
    }

    let mut secret = zeroize::Zeroizing::new(format!(
        "non-production-installed-health:{}:{}",
        uuid::Uuid::new_v4(),
        uuid::Uuid::new_v4()
    ));
    let result = execute_keychain_roundtrip(&entry, secret.as_str());

    // This account is generated solely for this launch. Any failure after the
    // write gets a best-effort delete and absence check before we return.
    if result.is_err() {
        best_effort_delete_keychain_probe(&entry);
    }
    secret.zeroize();
    result
}

fn execute_keychain_roundtrip(
    entry: &Entry,
    secret: &str,
) -> Result<InstalledKeychainReceipt, HostError> {
    entry
        .set_password(secret)
        .map_err(|_| installed_keychain_error())?;

    let mut observed = entry
        .get_password()
        .map_err(|_| installed_keychain_error())?;
    let exact_match = observed.as_bytes() == secret.as_bytes();
    observed.zeroize();
    if !exact_match {
        return Err(installed_keychain_error());
    }

    entry
        .delete_credential()
        .map_err(|_| installed_keychain_error())?;
    match entry.get_password() {
        Err(keyring::Error::NoEntry) => {}
        Ok(mut unexpected) => {
            unexpected.zeroize();
            return Err(installed_keychain_error());
        }
        Err(_) => return Err(installed_keychain_error()),
    }
    Ok(InstalledKeychainReceipt {
        write_succeeded: true,
        exact_read_succeeded: true,
        delete_succeeded: true,
        post_delete_absent: true,
    })
}

fn best_effort_delete_keychain_probe(entry: &Entry) {
    let _ = entry.delete_credential();
    if let Ok(mut unexpected) = entry.get_password() {
        unexpected.zeroize();
        let _ = entry.delete_credential();
    }
}

fn installed_keychain_error() -> HostError {
    HostError::new(
        "KEYCHAIN_ROUNDTRIP_FAILED",
        "The operating-system keychain did not complete the isolated installed-host roundtrip.",
        true,
    )
}

fn credential_entry(
    profile: &AppProfile,
    credential_id: &str,
    kind: CredentialKind,
) -> Result<Entry, HostError> {
    let account = format!("{}:{credential_id}", kind.account_prefix());
    Entry::new(&profile.keychain_service, &account).map_err(|_| {
        HostError::new(
            "KEYCHAIN_UNAVAILABLE",
            "The operating-system keychain is unavailable.",
            true,
        )
    })
}

fn validate_credential_id(value: &str) -> Result<(), HostError> {
    if value.len() < 8
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(HostError::new(
            "CREDENTIAL_HANDLE_INVALID",
            "The credential handle is invalid.",
            false,
        ));
    }
    Ok(())
}

fn sanitized_environment() -> std::collections::BTreeMap<String, String> {
    [
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TMPDIR",
        "USERPROFILE",
        "WANDB_BASE_URL",
    ]
    .into_iter()
    .filter_map(|key| std::env::var(key).ok().map(|value| (key.to_owned(), value)))
    .collect()
}

fn zeroize_json(value: &mut Value) {
    match value {
        Value::String(item) => item.zeroize(),
        Value::Array(items) => items.iter_mut().for_each(zeroize_json),
        Value::Object(items) => items.values_mut().for_each(zeroize_json),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use uuid::Uuid;

    #[test]
    fn credential_handles_reject_paths() {
        assert!(validate_credential_id("../../common_tools/.env").is_err());
        assert!(validate_credential_id("eco-project-01").is_ok());
    }

    #[test]
    fn recursive_zeroize_removes_secret_strings() {
        let mut value = json!({"api_key": "not-a-real-secret-value", "nested": ["location"]});
        zeroize_json(&mut value);
        assert_eq!(value["api_key"], "");
        assert_eq!(value["nested"][0], "");
    }

    #[test]
    fn worker_output_reader_rejects_malformed_and_oversized_frames() {
        let mut malformed = Cursor::new(b"{not-json}\n".to_vec());
        let malformed_error = read_worker_output_frame(&mut malformed, 128).unwrap_err();
        assert_eq!(malformed_error.code, "WORKER_OUTPUT_MALFORMED");

        let mut oversized = Cursor::new([vec![b'x'; 65], b"\n".to_vec()].concat());
        let oversized_error = read_worker_output_frame(&mut oversized, 64).unwrap_err();
        assert_eq!(oversized_error.code, "WORKER_OUTPUT_TOO_LARGE");

        let mut valid = Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":1}\n".to_vec());
        let frame = read_worker_output_frame(&mut valid, 128)
            .unwrap()
            .expect("one complete frame");
        assert_eq!(frame["id"], 1);
    }

    #[cfg(unix)]
    #[test]
    fn normal_worker_stop_closes_stdin_and_allows_bounded_clean_exit() {
        let root = resource_test_root();
        let marker = root.join("graceful-worker-exit.txt");
        let mut child = Command::new("/bin/sh")
            .arg("-c")
            .arg("cat >/dev/null; printf graceful > \"$1\"")
            .arg("worker-stop-test")
            .arg(&marker)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take();

        assert!(close_stdin_wait_or_kill(child, stdin, Duration::from_secs(2)).unwrap());
        assert_eq!(fs::read_to_string(&marker).unwrap(), "graceful");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn installed_ping_requires_authoritative_engine_configuration() {
        let valid = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "api_version": "1.0",
                "application": "EcoCorridor Scenario Studio",
                "schema_version": "1.0",
                "engine_configured": true,
                "methods": ["system.ping", "connectivity.guided.open"]
            }
        });
        assert!(validate_installed_ping(valid.clone()).is_ok());
        let mut invalid = valid;
        invalid["result"]["engine_configured"] = Value::Bool(false);
        assert!(validate_installed_ping(invalid).is_err());
    }

    #[test]
    fn installed_keychain_receipt_exposes_only_roundtrip_dispositions() {
        let receipt = serde_json::to_value(InstalledKeychainReceipt {
            write_succeeded: true,
            exact_read_succeeded: true,
            delete_succeeded: true,
            post_delete_absent: true,
        })
        .unwrap();
        let fields = receipt
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            fields,
            std::collections::BTreeSet::from([
                "delete_succeeded".to_owned(),
                "exact_read_succeeded".to_owned(),
                "post_delete_absent".to_owned(),
                "write_succeeded".to_owned(),
            ])
        );
        assert!(!receipt.to_string().contains("secret"));
        assert!(!receipt.to_string().contains("account"));
    }

    #[test]
    fn installed_guided_receipt_rejects_unverified_or_unhashed_replay() {
        let digest = "a".repeat(64);
        let valid = json!({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "api_version": "1.0",
                "method": "connectivity.guided.open",
                "data": {
                    "state": "VERIFIED_FIXTURE_REPLAY",
                    "evidence_manifest_sha256": digest,
                },
                "data_sha256": "b".repeat(64),
            }
        });
        assert!(validate_installed_guided(valid.clone()).is_ok());
        let mut invalid = valid;
        invalid["result"]["data"]["state"] = Value::String("PREVIEW".into());
        assert!(validate_installed_guided(invalid).is_err());
    }

    #[test]
    fn typed_worker_error_preserves_only_safe_renderer_fields() {
        let error = worker_response_error(&json!({
            "code": -32000,
            "message": "Generic request failure",
            "data": {
                "code": "MODEL_FITNESS_BLOCKED",
                "category": "MODEL_QUALITY",
                "severity": "BLOCKING",
                "user_message": "The model fitness review is incomplete.",
                "technical_message": "/Users/private/project.sqlite contains internal details",
                "object_reference": "fitness-passport:action-effects",
                "remediation": "Review and attest the action-effect evidence.",
                "retryable": true,
                "diagnostic_id": "diag-private"
            }
        }));

        assert_eq!(error.code, "MODEL_FITNESS_BLOCKED");
        assert_eq!(
            error.user_message,
            "The model fitness review is incomplete."
        );
        assert!(error.retryable);
        assert_eq!(
            error.object_reference.as_deref(),
            Some("fitness-passport:action-effects")
        );
        assert_eq!(
            error.remediation.as_deref(),
            Some("Review and attest the action-effect evidence.")
        );
        let serialized = serde_json::to_string(&error).unwrap();
        assert!(!serialized.contains("technical_message"));
        assert!(!serialized.contains("diagnostic_id"));
        assert!(!serialized.contains("/Users/private"));
    }

    #[test]
    fn typed_worker_error_rejects_local_paths_and_invalid_codes() {
        let error = worker_response_error(&json!({
            "message": "Open /Users/private/project.sqlite to continue.",
            "data": {
                "code": "not-stable",
                "user_message": "Inspect C:\\Users\\private\\project.sqlite.",
                "object_reference": "../../private/project.sqlite",
                "remediation": "Open /Users/private/project.sqlite to continue.",
                "retryable": "yes"
            }
        }));

        assert_eq!(error.code, "WORKER_REQUEST_FAILED");
        assert_eq!(
            error.user_message,
            "The scientific worker rejected the request."
        );
        assert!(!error.retryable);
        assert!(error.object_reference.is_none());
        assert!(error.remediation.is_none());
    }

    fn guided_resource_fixture(
        root: &Path,
        filename: &str,
        declared_sha256: Option<String>,
        declared_byte_size: Option<u64>,
    ) -> Vec<VerifiedResource> {
        let guided = root.join("guided");
        fs::create_dir_all(&guided).unwrap();
        let projection = guided.join("ridgeway-ui-projection.json");
        let manifest = guided.join("direct-evidence-manifest.json");
        let archive = guided.join("ridgeway-evidence.tar.gz");
        fs::write(&projection, b"{\"fixtureId\":\"ridgeway_crossing\"}").unwrap();
        fs::write(&archive, b"signed direct evidence archive").unwrap();
        let archive_sha256 = sha256_file(&archive).unwrap();
        let archive_bytes = fs::metadata(&archive).unwrap().len();
        fs::write(
            &manifest,
            serde_json::to_vec(&json!({
                "schema_version": "ridgeway-direct-evidence/v1.0",
                "evidence_bundle": {
                    "filename": filename,
                    "sha256": declared_sha256.unwrap_or_else(|| archive_sha256.clone()),
                    "byte_size": declared_byte_size.unwrap_or(archive_bytes),
                }
            }))
            .unwrap(),
        )
        .unwrap();
        vec![
            VerifiedResource {
                path: projection.canonicalize().unwrap(),
                sha256: sha256_file(&projection).unwrap(),
                executable: false,
            },
            VerifiedResource {
                path: manifest.canonicalize().unwrap(),
                sha256: sha256_file(&manifest).unwrap(),
                executable: false,
            },
            VerifiedResource {
                path: archive.canonicalize().unwrap(),
                sha256: archive_sha256,
                executable: false,
            },
        ]
    }

    fn resource_test_root() -> std::path::PathBuf {
        let root = std::env::temp_dir().join(format!("eco-guided-host-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn signed_guided_archive_declaration_resolves_exact_verified_resource() {
        let root = resource_test_root();
        let verified = guided_resource_fixture(&root, "ridgeway-evidence.tar.gz", None, None);
        let resources = resolve_guided_resources(
            &root,
            &verified,
            "guided/ridgeway-ui-projection.json",
            "guided/direct-evidence-manifest.json",
        )
        .unwrap();
        assert_eq!(
            resources.archive.path.file_name().unwrap(),
            "ridgeway-evidence.tar.gz"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn signed_guided_archive_filename_must_be_one_safe_basename() {
        for filename in [
            "../ridgeway-evidence.tar.gz",
            "nested/ridgeway-evidence.tar.gz",
            "nested\\ridgeway-evidence.tar.gz",
            "/ridgeway-evidence.tar.gz",
        ] {
            let root = resource_test_root();
            let verified = guided_resource_fixture(&root, filename, None, None);
            assert!(resolve_guided_resources(
                &root,
                &verified,
                "guided/ridgeway-ui-projection.json",
                "guided/direct-evidence-manifest.json",
            )
            .is_err());
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn signed_guided_archive_declaration_rejects_hash_or_size_mismatch() {
        let hash_root = resource_test_root();
        let hash_verified = guided_resource_fixture(
            &hash_root,
            "ridgeway-evidence.tar.gz",
            Some("f".repeat(64)),
            None,
        );
        assert!(resolve_guided_resources(
            &hash_root,
            &hash_verified,
            "guided/ridgeway-ui-projection.json",
            "guided/direct-evidence-manifest.json",
        )
        .is_err());
        fs::remove_dir_all(hash_root).unwrap();

        let size_root = resource_test_root();
        let actual_size = b"signed direct evidence archive".len() as u64;
        let size_verified = guided_resource_fixture(
            &size_root,
            "ridgeway-evidence.tar.gz",
            None,
            Some(actual_size + 1),
        );
        assert!(resolve_guided_resources(
            &size_root,
            &size_verified,
            "guided/ridgeway-ui-projection.json",
            "guided/direct-evidence-manifest.json",
        )
        .is_err());
        fs::remove_dir_all(size_root).unwrap();
    }
}
