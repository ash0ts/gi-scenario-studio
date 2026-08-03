mod error;
mod rpc;
mod worker;

use serde_json::Value;
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
    request: WorkerRequest,
) -> Result<Value, PlatformError> {
    manager.request(&app, request).await
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
            worker_stop
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
