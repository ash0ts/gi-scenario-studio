mod bundle;
mod credential;
mod error;
mod rpc;
mod worker;

use serde_json::{json, Value};
use tauri::{Manager, State};

use error::PlatformError;
use rpc::WorkerRequest;
use worker::{WorkerManager, WorkerStatus};

#[tauri::command]
fn worker_status(manager: State<'_, WorkerManager>) -> WorkerStatus {
    manager.status()
}

#[tauri::command]
fn worker_start(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
) -> Result<WorkerStatus, PlatformError> {
    manager.ensure_started(&app)
}

#[tauri::command]
async fn worker_request(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
    mut request: WorkerRequest,
) -> Result<Value, PlatformError> {
    credential::inject_for_worker(&mut request)?;
    manager.request(&app, request).await
}

#[tauri::command]
fn credential_prompt_and_store(
    credential_id: String,
) -> Result<credential::CredentialStatus, PlatformError> {
    credential::prompt_and_store(credential_id)
}

#[tauri::command]
fn credential_status(credential_id: String) -> Result<credential::CredentialStatus, PlatformError> {
    credential::status(credential_id)
}

#[tauri::command]
async fn credential_delete(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
    credential_id: String,
) -> Result<credential::CredentialStatus, PlatformError> {
    let status = credential::delete(credential_id)?;
    let clear_request = WorkerRequest {
        method: "observability.disconnect".to_owned(),
        params: Some(json!({"scope": "account"})),
        timeout_ms: Some(10_000),
    };
    if manager.request(&app, clear_request).await.is_err() {
        // Keychain deletion is authoritative. If the worker cannot confirm its
        // in-memory teardown, terminate it so no credential can remain usable.
        let _ = manager.stop(&app);
    }
    Ok(status)
}

#[tauri::command]
async fn select_swmm_bundle(
    app: tauri::AppHandle,
) -> Result<Option<bundle::StagedBundle>, PlatformError> {
    bundle::select_and_stage(app).await
}

#[tauri::command]
fn worker_stop(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
) -> Result<WorkerStatus, PlatformError> {
    manager.stop(&app)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(WorkerManager::default())
        .invoke_handler(tauri::generate_handler![
            worker_status,
            worker_start,
            worker_request,
            worker_stop,
            credential_prompt_and_store,
            credential_status,
            credential_delete,
            select_swmm_bundle
        ])
        .setup(|app| {
            // Start early so the first project action has no process-launch delay.
            // A missing development sidecar is surfaced as a recoverable status;
            // it never prevents the desktop shell from opening.
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let manager = app_handle.state::<WorkerManager>();
                let _ = manager.ensure_started(&app_handle);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build GI Scenario Studio desktop host");

    application.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            let manager = app_handle.state::<WorkerManager>();
            let _ = manager.stop(app_handle);
        }
    });
}
