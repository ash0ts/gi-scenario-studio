mod path_handles;
mod worker;

use std::collections::BTreeSet;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use scenario_host::AppProfile;
use serde_json::Value;
use tauri::{Manager, State};

use path_handles::{
    redact_worker_paths, ExportCopyReceipt, OpaqueFileHandle, OpaqueProjectHandle,
    PathHandleRegistry, RecentProjectHandle,
};
use worker::{
    run_installed_health_check, CredentialKind, HostError, WorkerManager, WorkerRequest,
    WorkerStatus,
};

fn application_profile() -> AppProfile {
    let executable_suffix = if cfg!(windows) { ".exe" } else { "" };
    let release_target = if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "macos-arm64"
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
        "macos-x64"
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        "windows-x64"
    } else {
        "linux-x64"
    };
    AppProfile {
        application_id: "org.ecocorridor.scenariostudio".into(),
        application_version: env!("CARGO_PKG_VERSION").into(),
        api_version: "1.0".into(),
        worker_resource: format!("bin/ecocorridor-worker{executable_suffix}"),
        engine_resource: format!("engine/bin/ecocorridor-engine{executable_suffix}"),
        engine_manifest_resource: "engine-manifest.json".into(),
        guided_projection_resource: "guided/ridgeway-ui-projection.json".into(),
        guided_manifest_resource: "guided/direct-evidence-manifest.json".into(),
        release_public_key_hex: option_env!("ECOCORRIDOR_RELEASE_PUBLIC_KEY_HEX")
            .unwrap_or("")
            .into(),
        release_source_commit_sha: option_env!("ECOCORRIDOR_RELEASE_SOURCE_SHA")
            .unwrap_or("")
            .into(),
        release_target: release_target.into(),
        keychain_service: "org.ecocorridor.scenariostudio.credentials".into(),
        app_data_directory: "EcoCorridorScenarioStudio".into(),
        allowed_rpc_methods: BTreeSet::from([
            "assistant.explain".into(),
            "assistant.configure".into(),
            "assistant.feedback".into(),
            "assistant.follow_up".into(),
            "assistant.get_trace".into(),
            "assistant.status".into(),
            "assistant.clear".into(),
            "assistant.intake.confirm".into(),
            "assistant.intake.extract".into(),
            "assistant.intake.get".into(),
            "connectivity.analysis.cancel".into(),
            "connectivity.analysis.pause".into(),
            "connectivity.analysis.plan".into(),
            "connectivity.analysis.prepare".into(),
            "connectivity.analysis.resume".into(),
            "connectivity.analysis.start".into(),
            "connectivity.analysis.status".into(),
            "connectivity.asset.add".into(),
            "connectivity.asset.download".into(),
            "connectivity.asset.download.preview".into(),
            "connectivity.asset.inspect".into(),
            "connectivity.bundle.import".into(),
            "connectivity.bundle.inspect".into(),
            "connectivity.evidence.resolve".into(),
            "connectivity.digitization.preview".into(),
            "connectivity.digitization.publish".into(),
            "connectivity.export.create".into(),
            "connectivity.export.validate".into(),
            "connectivity.focal_areas.save".into(),
            "connectivity.grid.approve".into(),
            "connectivity.grid.preview".into(),
            "connectivity.guided.cancel".into(),
            "connectivity.guided.checkpoint".into(),
            "connectivity.guided.open".into(),
            "connectivity.guided.pause".into(),
            "connectivity.guided.rerun".into(),
            "connectivity.guided.resume".into(),
            "connectivity.guided.status".into(),
            "connectivity.hypothesis.save".into(),
            "connectivity.intervention.save".into(),
            "connectivity.intervention.preview".into(),
            "connectivity.project.get_state".into(),
            "connectivity.scenario.save".into(),
            "connectivity.safeguard.save".into(),
            "connectivity.review_mask.save".into(),
            "connectivity.decision_rule.save".into(),
            "connectivity.decision_brief.save".into(),
            "connectivity.cost_basis.get".into(),
            "connectivity.cost_basis.save".into(),
            "connectivity.resource_policy.save".into(),
            "connectivity.model.attest".into(),
            "connectivity.model.validate".into(),
            "connectivity.planning.create".into(),
            "connectivity.results.decision_board".into(),
            "connectivity.results.scenario_detail".into(),
            "observability.connect".into(),
            "observability.get_status".into(),
            "observability.open_artifact".into(),
            "observability.open_run".into(),
            "observability.open_trace".into(),
            "observability.preflight".into(),
            "observability.preview_upload".into(),
            "observability.retry".into(),
            "observability.sync".into(),
            "observability.validate_binding".into(),
            "system.ping".into(),
        ]),
        maximum_rpc_bytes: 8 * 1024 * 1024,
    }
}

/// Runs only from an installed, key-injected release executable. The receipt
/// must be a new regular file below the operating-system temporary directory;
/// arbitrary application or user paths are intentionally not accepted.
pub fn run_release_health_check(receipt_path: &Path) -> Result<(), ()> {
    let profile = application_profile();
    if profile.release_public_key_hex.is_empty() || profile.release_source_commit_sha.is_empty() {
        return Err(());
    }
    let receipt_path = validate_health_receipt_path(receipt_path)?;
    let receipt_parent = receipt_path.parent().ok_or(())?;
    let app_data_root = receipt_parent.join("ecocorridor-installed-health-data");
    if let Ok(metadata) = std::fs::symlink_metadata(&app_data_root) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(());
        }
    } else {
        std::fs::create_dir(&app_data_root).map_err(|_| ())?;
    }
    let app_data_root = std::fs::canonicalize(&app_data_root).map_err(|_| ())?;
    if !app_data_root.starts_with(receipt_parent) {
        return Err(());
    }
    let resource_root = installed_release_resource_root()?;
    let receipt = run_installed_health_check(&profile, &resource_root, &app_data_root)
        .map_err(|_| ())?;
    let mut bytes = serde_json::to_vec_pretty(&receipt).map_err(|_| ())?;
    bytes.push(b'\n');
    if bytes.len() > 16 * 1024 {
        return Err(());
    }
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&receipt_path)
        .map_err(|_| ())?;
    if output.write_all(&bytes).and_then(|_| output.sync_all()).is_err() {
        let _ = std::fs::remove_file(&receipt_path);
        return Err(());
    }
    Ok(())
}

fn validate_health_receipt_path(value: &Path) -> Result<PathBuf, ()> {
    if !value.is_absolute() || value.exists() {
        return Err(());
    }
    let filename = value.file_name().and_then(|item| item.to_str()).ok_or(())?;
    if filename.len() > 128
        || !filename.starts_with("ecocorridor-installed-health-")
        || !filename.ends_with(".json")
        || !filename.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
        })
    {
        return Err(());
    }
    let parent = std::fs::canonicalize(value.parent().ok_or(())?).map_err(|_| ())?;
    let temporary = std::fs::canonicalize(std::env::temp_dir()).map_err(|_| ())?;
    if !parent.starts_with(&temporary) {
        return Err(());
    }
    Ok(parent.join(filename))
}

fn installed_release_resource_root() -> Result<PathBuf, ()> {
    let executable = std::env::current_exe().map_err(|_| ())?;
    let executable = std::fs::canonicalize(executable).map_err(|_| ())?;
    #[cfg(target_os = "macos")]
    let candidate = executable
        .parent()
        .and_then(Path::parent)
        .map(|contents| contents.join("Resources").join("release"))
        .ok_or(())?;
    #[cfg(target_os = "windows")]
    let candidate = executable
        .parent()
        .map(|root| root.join("release"))
        .ok_or(())?;
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    return Err(());
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    {
        let candidate = std::fs::canonicalize(candidate).map_err(|_| ())?;
        if candidate.is_dir() {
            Ok(candidate)
        } else {
            Err(())
        }
    }
}

#[tauri::command]
fn worker_status(manager: State<'_, WorkerManager>) -> WorkerStatus {
    manager.status()
}

#[tauri::command]
fn worker_start(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
) -> Result<WorkerStatus, HostError> {
    manager.ensure_started(&app)
}

#[tauri::command]
async fn worker_request(
    app: tauri::AppHandle,
    manager: State<'_, WorkerManager>,
    paths: State<'_, PathHandleRegistry>,
    mut request: WorkerRequest,
) -> Result<Value, HostError> {
    let method = request.method.clone();
    let staged_bundle_handle = request
        .params
        .get("bundle_handle")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let staged_asset_handle = request
        .params
        .get("source_handle")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let project_handle = paths.inject_paths(&mut request)?;
    let mut value = manager.request(&app, request).await?;
    redact_worker_paths(&mut value, project_handle.as_deref())?;
    if method == "connectivity.export.create" {
        let handle = project_handle.as_deref().ok_or_else(|| {
            HostError::new(
                "OPAQUE_PATH_HANDLE_REQUIRED",
                "Choose the authoritative project before creating an evidence package.",
                false,
            )
        })?;
        paths.register_export_receipt(handle, &value)?;
    }
    if method == "connectivity.bundle.import" {
        if let Some(handle) = staged_bundle_handle {
            paths.release_staged(&handle);
        }
    }
    if method == "connectivity.asset.add" {
        if let Some(handle) = staged_asset_handle {
            paths.release_staged(&handle);
        }
    }
    Ok(value)
}

#[tauri::command]
fn choose_connectivity_bundle(
    app: tauri::AppHandle,
    paths: State<'_, PathHandleRegistry>,
) -> Result<Option<OpaqueFileHandle>, HostError> {
    let Some(selected) = tinyfiledialogs::open_file_dialog(
        "Choose a portable EcoCorridor bundle",
        "",
        Some((&["*.zip", "*.ecocorridor"], "EcoCorridor bundles")),
    ) else {
        return Ok(None);
    };
    let app_data_root = app
        .path()
        .app_data_dir()
        .map_err(|_| HostError::staging())?;
    std::fs::create_dir_all(&app_data_root).map_err(|_| HostError::staging())?;
    paths
        .stage_bundle(&app_data_root, std::path::Path::new(&selected))
        .map(Some)
}

#[tauri::command]
fn choose_connectivity_asset(
    app: tauri::AppHandle,
    paths: State<'_, PathHandleRegistry>,
) -> Result<Option<OpaqueFileHandle>, HostError> {
    let Some(selected) =
        tinyfiledialogs::open_file_dialog("Choose a reviewed EcoCorridor source asset", "", None)
    else {
        return Ok(None);
    };
    let app_data_root = app
        .path()
        .app_data_dir()
        .map_err(|_| HostError::staging())?;
    std::fs::create_dir_all(&app_data_root).map_err(|_| HostError::staging())?;
    paths
        .stage_asset(&app_data_root, std::path::Path::new(&selected))
        .map(Some)
}

#[tauri::command]
fn create_project_workspace(
    app: tauri::AppHandle,
    paths: State<'_, PathHandleRegistry>,
) -> Result<OpaqueProjectHandle, HostError> {
    let app_data_root = app
        .path()
        .app_data_dir()
        .map_err(|_| HostError::staging())?;
    std::fs::create_dir_all(&app_data_root).map_err(|_| HostError::staging())?;
    paths.create_project(&app_data_root)
}

#[tauri::command]
fn list_project_workspaces(
    app: tauri::AppHandle,
    paths: State<'_, PathHandleRegistry>,
) -> Result<Vec<RecentProjectHandle>, HostError> {
    let app_data_root = app
        .path()
        .app_data_dir()
        .map_err(|_| HostError::staging())?;
    std::fs::create_dir_all(&app_data_root).map_err(|_| HostError::staging())?;
    paths.list_projects(&app_data_root)
}

#[tauri::command]
fn open_project_workspace(
    app: tauri::AppHandle,
    paths: State<'_, PathHandleRegistry>,
    project_handle: String,
) -> Result<OpaqueProjectHandle, HostError> {
    let app_data_root = app
        .path()
        .app_data_dir()
        .map_err(|_| HostError::staging())?;
    std::fs::create_dir_all(&app_data_root).map_err(|_| HostError::staging())?;
    paths.open_project(&app_data_root, &project_handle)
}

#[tauri::command]
fn save_export_copy(
    paths: State<'_, PathHandleRegistry>,
    project_handle: String,
    export_id: String,
) -> Result<Option<ExportCopyReceipt>, HostError> {
    let Some(selected_parent) = tinyfiledialogs::select_folder_dialog(
        "Save a copy of the verified EcoCorridor evidence package",
        "",
    ) else {
        return Ok(None);
    };
    paths
        .copy_confirmed_export(
            &project_handle,
            &export_id,
            std::path::Path::new(&selected_parent),
        )
        .map(Some)
}

fn validated_provider_url(value: &str) -> Result<url::Url, HostError> {
    let parsed = url::Url::parse(value).map_err(|_| {
        HostError::new(
            "PROVIDER_URL_INVALID",
            "The confirmed observability link is not a valid URL.",
            false,
        )
    })?;
    let host = parsed.host_str().ok_or_else(|| {
        HostError::new(
            "PROVIDER_URL_INVALID",
            "The confirmed observability link has no provider host.",
            false,
        )
    })?;
    let loopback = matches!(host, "localhost" | "127.0.0.1" | "::1");
    if !matches!(parsed.scheme(), "http" | "https")
        || (parsed.scheme() != "https" && !loopback)
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().is_some()
    {
        return Err(HostError::new(
            "PROVIDER_URL_BLOCKED",
            "The confirmed observability link does not meet the native URL safety policy.",
            false,
        ));
    }
    Ok(parsed)
}

#[tauri::command]
fn open_confirmed_provider_url(url: String) -> Result<(), HostError> {
    let parsed = validated_provider_url(&url)?;
    let rendered = parsed.as_str();
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("/usr/bin/open");
        command.arg(rendered);
        command
    };
    #[cfg(target_os = "windows")]
    let mut command = {
        // No command shell is involved; FileProtocolHandler delegates the
        // already-validated URL directly to the registered system browser.
        let mut command = Command::new("rundll32.exe");
        command.arg("url.dll,FileProtocolHandler").arg(rendered);
        command
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut command = Command::new("xdg-open");
        command.arg(rendered);
        command
    };
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| {
            HostError::new(
                "PROVIDER_URL_OPEN_FAILED",
                "The operating system could not open the confirmed observability link.",
                true,
            )
        })?;
    Ok(())
}

#[tauri::command]
fn credential_store(
    manager: State<'_, WorkerManager>,
    credential_id: String,
    kind: CredentialKind,
) -> Result<(), HostError> {
    manager.prompt_and_store_credential(&credential_id, kind)
}

#[tauri::command]
fn credential_status(
    manager: State<'_, WorkerManager>,
    credential_id: String,
    kind: CredentialKind,
) -> Result<bool, HostError> {
    manager.credential_status(&credential_id, kind)
}

#[tauri::command]
fn credential_delete(
    manager: State<'_, WorkerManager>,
    credential_id: String,
    kind: CredentialKind,
) -> Result<(), HostError> {
    manager.delete_credential(&credential_id, kind)
}

#[tauri::command]
fn worker_stop(manager: State<'_, WorkerManager>) -> Result<WorkerStatus, HostError> {
    manager.stop()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let profile = application_profile();
    profile
        .validate()
        .expect("EcoCorridor native host profile must be valid");
    let application = tauri::Builder::default()
        .setup(|_app| {
            #[cfg(any(target_os = "macos", target_os = "windows"))]
            _app.handle()
                .plugin(tauri_plugin_updater::Builder::new().build())?;
            Ok(())
        })
        .manage(WorkerManager::new(profile))
        .manage(PathHandleRegistry::new())
        .invoke_handler(tauri::generate_handler![
            worker_status,
            worker_start,
            worker_request,
            worker_stop,
            credential_store,
            credential_status,
            credential_delete,
            choose_connectivity_bundle,
            choose_connectivity_asset,
            create_project_workspace,
            list_project_workspaces,
            open_project_workspace,
            save_export_copy,
            open_confirmed_provider_url
        ])
        .build(tauri::generate_context!())
        .expect("failed to build EcoCorridor Scenario Studio native host");

    application.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            let manager = app_handle.state::<WorkerManager>();
            let _ = manager.stop();
            let paths = app_handle.state::<PathHandleRegistry>();
            paths.cleanup_staged();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{validate_health_receipt_path, validated_provider_url};

    #[test]
    fn provider_links_require_credential_free_https_or_loopback_http() {
        assert!(validated_provider_url("https://wandb.example/team/project/runs/run-1").is_ok());
        assert!(validated_provider_url("http://127.0.0.1:8080/weave/trace-1").is_ok());
        for blocked in [
            "http://wandb.example/run-1",
            "https://user:secret@wandb.example/run-1",
            "file:///tmp/evidence",
            "javascript:alert(1)",
            "https://wandb.example/run-1#credential",
        ] {
            assert!(validated_provider_url(blocked).is_err(), "accepted {blocked}");
        }
    }

    #[test]
    fn installed_health_receipts_are_new_bounded_files_under_temp() {
        let accepted = std::env::temp_dir().join(format!(
            "ecocorridor-installed-health-{}.json",
            uuid::Uuid::new_v4()
        ));
        assert_eq!(validate_health_receipt_path(&accepted).unwrap(), accepted);
        assert!(validate_health_receipt_path(
            &std::env::temp_dir().join("untrusted-health.json")
        )
        .is_err());
        assert!(validate_health_receipt_path(std::path::Path::new(
            "ecocorridor-installed-health-relative.json"
        ))
        .is_err());
    }
}
