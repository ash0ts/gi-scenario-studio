use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde::Serialize;
use serde_json::Value;
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;
use zeroize::Zeroize;

use crate::error::PlatformError;
use crate::rpc::{
    parse_inbound, request_key, InboundMessage, WorkerEvent, WorkerRequest, API_VERSION,
    MAX_MESSAGE_BYTES,
};

const SIDECAR_NAME: &str = "gi-scenario-worker";
const SIDECAR_ARGS: &[&str] = &["--transport", "jsonl-stdio", "--api-version", API_VERSION];
const INHERITED_ENVIRONMENT: &[&str] = &[
    "APPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "SystemRoot",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
];

type PendingSender = oneshot::Sender<Result<Value, PlatformError>>;

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
    pub lifecycle: WorkerLifecycle,
    pub api_version: &'static str,
    pub transport: &'static str,
    pub generation: u64,
    pub launch_mode: Option<String>,
    pub last_error: Option<PlatformError>,
}

impl Default for WorkerStatus {
    fn default() -> Self {
        Self {
            lifecycle: WorkerLifecycle::Stopped,
            api_version: API_VERSION,
            transport: "jsonl-stdio",
            generation: 0,
            launch_mode: None,
            last_error: None,
        }
    }
}

#[derive(Default)]
struct RuntimeState {
    child: Option<CommandChild>,
    status: WorkerStatus,
}

pub(crate) struct WorkerManager {
    runtime: Mutex<RuntimeState>,
    pending: Mutex<HashMap<String, PendingSender>>,
    next_request_id: AtomicU64,
    next_event_sequence: AtomicU64,
}

impl Default for WorkerManager {
    fn default() -> Self {
        Self {
            runtime: Mutex::new(RuntimeState::default()),
            pending: Mutex::new(HashMap::new()),
            next_request_id: AtomicU64::new(1),
            next_event_sequence: AtomicU64::new(1),
        }
    }
}

impl WorkerManager {
    pub(crate) fn status(&self) -> WorkerStatus {
        self.runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .status
            .clone()
    }

    pub(crate) fn ensure_started(&self, app: &AppHandle) -> Result<WorkerStatus, PlatformError> {
        {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if runtime.status.lifecycle == WorkerLifecycle::Ready && runtime.child.is_some() {
                return Ok(runtime.status.clone());
            }
            if runtime.status.lifecycle == WorkerLifecycle::Starting {
                return Err(PlatformError::worker_unavailable(
                    "worker start is already in progress",
                ));
            }
            runtime.status.lifecycle = WorkerLifecycle::Starting;
            runtime.status.last_error = None;
        }
        self.emit_status(app);

        let mut sidecar_args = SIDECAR_ARGS
            .iter()
            .map(|item| (*item).to_owned())
            .collect::<Vec<_>>();
        let data_root = crate::bundle::project_data_root(app)?;
        sidecar_args.extend([
            "--data-root".to_owned(),
            data_root.to_string_lossy().into_owned(),
        ]);
        let guided_projection = app
            .path()
            .resolve("guided/guidedFixture.json", BaseDirectory::Resource)
            .ok();
        let guided_evidence = app
            .path()
            .resolve("guided/evidence", BaseDirectory::Resource)
            .ok();
        if let (Some(projection), Some(evidence)) = (guided_projection, guided_evidence) {
            if projection.is_file() && evidence.join("manifest.json").is_file() {
                sidecar_args.extend([
                    "--guided-fixture".to_owned(),
                    projection.to_string_lossy().into_owned(),
                    "--guided-evidence-directory".to_owned(),
                    evidence.to_string_lossy().into_owned(),
                ]);
            }
        }

        let spawn_result = app
            .shell()
            .sidecar(SIDECAR_NAME)
            .map_err(|error| error.to_string())
            .and_then(|command| {
                command
                    .env_clear()
                    .envs(sanitized_environment())
                    .env("PYTHONUTF8", "1")
                    .env("PYTHONUNBUFFERED", "1")
                    .args(sidecar_args)
                    .spawn()
                    .map_err(|error| error.to_string())
            });

        let (mut receiver, child) = match spawn_result {
            Ok(process) => process,
            Err(technical_message) => {
                let error = PlatformError::worker_unavailable(technical_message);
                self.mark_faulted(app, error.clone());
                return Err(error);
            }
        };

        let generation = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            runtime.status.generation = runtime.status.generation.saturating_add(1);
            runtime.status.lifecycle = WorkerLifecycle::Ready;
            runtime.status.launch_mode = Some("bundled-sidecar".to_owned());
            runtime.child = Some(child);
            runtime.status.generation
        };
        self.emit_status(app);

        let event_app = app.clone();
        tauri::async_runtime::spawn(async move {
            while let Some(event) = receiver.recv().await {
                let manager = event_app.state::<WorkerManager>();
                match event {
                    CommandEvent::Stdout(bytes) => {
                        manager.handle_stdout(&event_app, generation, &bytes)
                    }
                    CommandEvent::Stderr(_) => {
                        // Worker stderr may contain project paths or model content. It is
                        // intentionally not forwarded to the webview. The Python worker's
                        // diagnostics service owns opt-in support bundles.
                    }
                    CommandEvent::Terminated(_) => {
                        manager.handle_termination(&event_app, generation);
                        break;
                    }
                    CommandEvent::Error(message) => {
                        manager.mark_faulted(
                            &event_app,
                            PlatformError::worker_unavailable(format!(
                                "worker process stream failed: {message}"
                            )),
                        );
                        break;
                    }
                    _ => {}
                }
            }

            let manager = event_app.state::<WorkerManager>();
            manager.handle_termination(&event_app, generation);
        });

        Ok(self.status())
    }

    pub(crate) async fn request(
        &self,
        app: &AppHandle,
        request: WorkerRequest,
    ) -> Result<Value, PlatformError> {
        request.validate()?;
        let method = request.method.clone();
        let timeout_ms = request.timeout_ms();
        self.ensure_started(app)?;

        let request_id = self.next_request_id.fetch_add(1, Ordering::Relaxed);
        let mut envelope = request.into_envelope(request_id);
        let encoded = serde_json::to_vec(&envelope);
        zeroize_embedded_secrets(&mut envelope);
        let mut line = encoded.map_err(|error| {
            PlatformError::protocol(format!("request encoding failed: {error}"))
        })?;
        if line.len() > MAX_MESSAGE_BYTES {
            let message_size = line.len();
            line.zeroize();
            return Err(PlatformError::message_too_large(
                "desktop-to-worker",
                message_size,
                MAX_MESSAGE_BYTES,
            ));
        }
        line.push(b'\n');

        let key = request_key(&Value::Number(request_id.into()))?;
        let (sender, receiver) = oneshot::channel();
        self.pending
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(key.clone(), sender);

        let write_result = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            match runtime.child.as_mut() {
                Some(child) => child.write(&line).map_err(|error| error.to_string()),
                None => Err("worker process has no stdin handle".to_owned()),
            }
        };
        line.zeroize();

        if let Err(technical_message) = write_result {
            self.remove_pending(&key);
            let error = PlatformError::worker_unavailable(technical_message);
            self.mark_faulted(app, error.clone());
            return Err(error);
        }

        match tokio::time::timeout(std::time::Duration::from_millis(timeout_ms), receiver).await {
            Ok(Ok(response)) => response,
            Ok(Err(_)) => Err(PlatformError::stopped()),
            Err(_) => {
                self.remove_pending(&key);
                Err(PlatformError::timeout(&method, timeout_ms))
            }
        }
    }

    pub(crate) fn stop(&self, app: &AppHandle) -> Result<WorkerStatus, PlatformError> {
        let kill_result = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            runtime.status.lifecycle = WorkerLifecycle::Stopped;
            runtime.status.launch_mode = None;
            runtime.status.last_error = None;
            runtime.child.take().map(|child| child.kill())
        };

        if let Some(Err(error)) = kill_result {
            let platform_error = PlatformError::worker_unavailable(error.to_string());
            self.mark_faulted(app, platform_error.clone());
            return Err(platform_error);
        }

        self.fail_pending(PlatformError::stopped());
        self.emit_status(app);
        Ok(self.status())
    }

    fn handle_stdout(&self, app: &AppHandle, generation: u64, bytes: &[u8]) {
        // `tauri-plugin-shell` defaults to `raw_out = false`. Its documented
        // CommandEvent contract is one logical line (bytes until `\n` or `\r`),
        // so this is already a JSONL frame. Do not enable `set_raw_out(true)`
        // without adding an incremental byte framer here.
        match parse_inbound(bytes) {
            Ok(InboundMessage::Response { request_key, value }) => {
                if let Some(sender) = self.remove_pending(&request_key) {
                    let _ = sender.send(Ok(value));
                }
            }
            Ok(InboundMessage::Notification(payload)) => {
                let sequence = self.next_event_sequence.fetch_add(1, Ordering::Relaxed);
                let event = WorkerEvent {
                    sequence,
                    worker_generation: generation,
                    payload,
                };
                let _ = app.emit("worker://event", event);
            }
            Err(error) => {
                self.mark_faulted(app, error);
            }
        }
    }

    fn handle_termination(&self, app: &AppHandle, generation: u64) {
        let should_fail_pending = {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if runtime.status.generation != generation
                || runtime.status.lifecycle != WorkerLifecycle::Ready
            {
                false
            } else {
                runtime.child = None;
                runtime.status.lifecycle = WorkerLifecycle::Faulted;
                runtime.status.launch_mode = None;
                runtime.status.last_error = Some(PlatformError::stopped());
                true
            }
        };

        if should_fail_pending {
            self.fail_pending(PlatformError::stopped());
            self.emit_status(app);
        }
    }

    fn mark_faulted(&self, app: &AppHandle, error: PlatformError) {
        {
            let mut runtime = self
                .runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if let Some(child) = runtime.child.take() {
                let _ = child.kill();
            }
            runtime.status.lifecycle = WorkerLifecycle::Faulted;
            runtime.status.launch_mode = None;
            runtime.status.last_error = Some(error.clone());
        }
        self.fail_pending(error);
        self.emit_status(app);
    }

    fn emit_status(&self, app: &AppHandle) {
        let _ = app.emit("worker://status", self.status());
    }

    fn remove_pending(&self, key: &str) -> Option<PendingSender> {
        self.pending
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(key)
    }

    fn fail_pending(&self, error: PlatformError) {
        let pending = {
            let mut guard = self
                .pending
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            std::mem::take(&mut *guard)
        };
        for (_, sender) in pending {
            let _ = sender.send(Err(error.clone()));
        }
    }
}

fn sanitized_environment() -> Vec<(String, String)> {
    INHERITED_ENVIRONMENT
        .iter()
        .filter_map(|name| {
            std::env::var(name)
                .ok()
                .map(|value| ((*name).to_owned(), value))
        })
        .collect()
}

fn zeroize_embedded_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            for (key, item) in object {
                if matches!(
                    key.as_str(),
                    "api_key" | "authorization" | "access_token" | "password" | "secret"
                ) {
                    if let Value::String(secret) = item {
                        secret.zeroize();
                    }
                    *item = Value::Null;
                } else {
                    zeroize_embedded_secrets(item);
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                zeroize_embedded_secrets(item);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{sanitized_environment, zeroize_embedded_secrets, INHERITED_ENVIRONMENT};

    #[test]
    fn worker_inherits_only_allowlisted_environment_names() {
        for (name, _) in sanitized_environment() {
            assert!(INHERITED_ENVIRONMENT.contains(&name.as_str()));
            assert_ne!(name, "PYTHONPATH");
            assert_ne!(name, "LD_PRELOAD");
            assert_ne!(name, "DYLD_INSERT_LIBRARIES");
        }
    }

    #[test]
    fn serialized_secret_fields_are_cleared_from_the_request_value() {
        let mut request = json!({
            "params": {
                "api_key": "secret-key-material",
                "nested": {"authorization": "Bearer secret", "project": "safe"}
            }
        });

        zeroize_embedded_secrets(&mut request);

        assert!(request["params"]["api_key"].is_null());
        assert!(request["params"]["nested"]["authorization"].is_null());
        assert_eq!(request["params"]["nested"]["project"], "safe");
        assert!(!request.to_string().contains("secret-key-material"));
    }
}
