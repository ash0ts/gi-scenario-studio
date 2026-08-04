use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

use serde::Serialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::worker::{HostError, WorkerRequest};

const MAX_STAGED_FILE_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAX_EXPORT_COPY_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const MAX_EXPORT_COPY_FILES: usize = 100_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HandleKind {
    Bundle,
    Asset,
    Project,
}

impl HandleKind {
    fn prefix(self) -> &'static str {
        match self {
            Self::Bundle => "bundle",
            Self::Asset => "asset",
            Self::Project => "project",
        }
    }
}

#[derive(Debug, Clone)]
struct HandleEntry {
    kind: HandleKind,
    path: PathBuf,
}

#[derive(Debug, Clone)]
struct ConfirmedExportReceipt {
    source_directory: PathBuf,
    manifest: Value,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct OpaqueFileHandle {
    pub handle: String,
    pub display_name: String,
    pub byte_size: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct OpaqueProjectHandle {
    pub handle: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct RecentProjectHandle {
    pub handle: String,
    pub last_modified_ms: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ExportCopyReceipt {
    pub export_id: String,
    pub copied: bool,
    pub destination_label: String,
    pub file_count: usize,
    pub byte_size: u64,
}

#[derive(Debug, Default)]
pub(crate) struct PathHandleRegistry {
    entries: Mutex<HashMap<String, HandleEntry>>,
    confirmed_exports: Mutex<HashMap<(String, String), ConfirmedExportReceipt>>,
}

impl PathHandleRegistry {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn stage_bundle(
        &self,
        app_data_root: &Path,
        selected_path: &Path,
    ) -> Result<OpaqueFileHandle, HostError> {
        self.stage_file(app_data_root, selected_path, HandleKind::Bundle)
    }

    pub(crate) fn stage_asset(
        &self,
        app_data_root: &Path,
        selected_path: &Path,
    ) -> Result<OpaqueFileHandle, HostError> {
        self.stage_file(app_data_root, selected_path, HandleKind::Asset)
    }

    fn stage_file(
        &self,
        app_data_root: &Path,
        selected_path: &Path,
        kind: HandleKind,
    ) -> Result<OpaqueFileHandle, HostError> {
        let metadata = fs::symlink_metadata(selected_path).map_err(|_| {
            HostError::new(
                "SELECTED_FILE_UNAVAILABLE",
                "The selected file could not be read.",
                true,
            )
        })?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err(HostError::new(
                "SELECTED_FILE_UNSAFE",
                "Choose a regular file. Symbolic links and special files are not accepted.",
                false,
            ));
        }
        if metadata.len() == 0 || metadata.len() > MAX_STAGED_FILE_BYTES {
            return Err(HostError::new(
                "SELECTED_FILE_SIZE_BLOCKED",
                "The selected file is empty or exceeds the 4 GiB desktop staging limit.",
                false,
            ));
        }

        let nonce = Uuid::new_v4().simple().to_string();
        let handle = format!("{}_{}", kind.prefix(), nonce);
        let stage_root = app_data_root.join("staged-inputs").join(&nonce);
        fs::create_dir_all(&stage_root).map_err(|_| HostError::staging())?;
        let canonical_root = fs::canonicalize(app_data_root).map_err(|_| HostError::staging())?;
        let canonical_stage_root =
            contained_directory(&stage_root, app_data_root, &canonical_root)?;
        let staged_path = canonical_stage_root.join("payload");
        let temporary_path = canonical_stage_root.join(".payload.tmp");
        let copy_result = (|| -> Result<(u64, String), HostError> {
            let mut source_options = OpenOptions::new();
            source_options.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                source_options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW);
            }
            let source = source_options
                .open(selected_path)
                .map_err(|_| HostError::staging())?;
            let opened_metadata = source.metadata().map_err(|_| HostError::staging())?;
            if !opened_metadata.is_file() || opened_metadata.len() != metadata.len() {
                return Err(HostError::new(
                    "SELECTED_FILE_CHANGED",
                    "The selected file changed while it was being staged. Choose it again.",
                    true,
                ));
            }
            let destination = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary_path)
                .map_err(|_| HostError::staging())?;
            let mut reader = BufReader::new(source);
            let mut writer = BufWriter::new(destination);
            let mut hasher = Sha256::new();
            let mut buffer = vec![0_u8; 1024 * 1024];
            let mut copied = 0_u64;
            loop {
                let count =
                    io::Read::read(&mut reader, &mut buffer).map_err(|_| HostError::staging())?;
                if count == 0 {
                    break;
                }
                copied = copied.saturating_add(count as u64);
                if copied > metadata.len() || copied > MAX_STAGED_FILE_BYTES {
                    return Err(HostError::new(
                        "SELECTED_FILE_CHANGED",
                        "The selected file changed while it was being staged. Choose it again.",
                        true,
                    ));
                }
                hasher.update(&buffer[..count]);
                writer
                    .write_all(&buffer[..count])
                    .map_err(|_| HostError::staging())?;
            }
            writer.flush().map_err(|_| HostError::staging())?;
            writer
                .get_ref()
                .sync_all()
                .map_err(|_| HostError::staging())?;
            if copied != metadata.len() {
                return Err(HostError::new(
                    "SELECTED_FILE_CHANGED",
                    "The selected file changed while it was being staged. Choose it again.",
                    true,
                ));
            }
            Ok((copied, format!("{:x}", hasher.finalize())))
        })();
        let (copied, sha256) = match copy_result {
            Ok(result) => result,
            Err(error) => {
                let _ = fs::remove_dir_all(&canonical_stage_root);
                return Err(error);
            }
        };
        fs::rename(&temporary_path, &staged_path).map_err(|_| {
            let _ = fs::remove_dir_all(&canonical_stage_root);
            HostError::staging()
        })?;
        #[cfg(unix)]
        OpenOptions::new()
            .read(true)
            .open(&canonical_stage_root)
            .and_then(|directory| directory.sync_all())
            .map_err(|_| HostError::staging())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&staged_path, fs::Permissions::from_mode(0o600))
                .map_err(|_| HostError::staging())?;
        }
        let canonical_path = fs::canonicalize(&staged_path).map_err(|_| HostError::staging())?;
        if !canonical_path.starts_with(&canonical_root) {
            return Err(HostError::new(
                "STAGED_PATH_OUTSIDE_APP_DATA",
                "The selected file could not be contained in EcoCorridor application storage.",
                false,
            ));
        }
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(
                handle.clone(),
                HandleEntry {
                    kind,
                    path: canonical_path,
                },
            );
        let display_name = selected_path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("selected connectivity bundle")
            .to_string();
        Ok(OpaqueFileHandle {
            handle,
            display_name,
            byte_size: copied,
            sha256,
        })
    }

    pub(crate) fn create_project(
        &self,
        app_data_root: &Path,
    ) -> Result<OpaqueProjectHandle, HostError> {
        let nonce = Uuid::new_v4().simple().to_string();
        let handle = format!("project_{nonce}");
        let project_path = app_data_root.join("projects").join(&nonce);
        fs::create_dir_all(&project_path).map_err(|_| HostError::staging())?;
        let canonical_root = fs::canonicalize(app_data_root).map_err(|_| HostError::staging())?;
        let canonical_path = contained_directory(&project_path, app_data_root, &canonical_root)?;
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(
                handle.clone(),
                HandleEntry {
                    kind: HandleKind::Project,
                    path: canonical_path,
                },
            );
        Ok(OpaqueProjectHandle { handle })
    }

    /// Discover only worker-initialized EcoCorridor projects beneath this
    /// application's own data root. The renderer receives stable opaque
    /// handles, never directories. Each candidate is rechecked for direct-child
    /// containment, symlinks, and a regular SQLite authority file before being
    /// registered for this host process.
    pub(crate) fn list_projects(
        &self,
        app_data_root: &Path,
    ) -> Result<Vec<RecentProjectHandle>, HostError> {
        let canonical_app_root = fs::canonicalize(app_data_root).map_err(|_| HostError::staging())?;
        let projects_root = app_data_root.join("projects");
        fs::create_dir_all(&projects_root).map_err(|_| HostError::staging())?;
        let canonical_projects_root =
            contained_directory(&projects_root, app_data_root, &canonical_app_root)?;
        let mut projects = Vec::new();

        for entry in fs::read_dir(&canonical_projects_root).map_err(|_| HostError::staging())? {
            let entry = entry.map_err(|_| HostError::staging())?;
            let storage_id = match entry.file_name().to_str() {
                Some(value) if valid_project_storage_id(value) => value.to_string(),
                _ => continue,
            };
            let path = entry.path();
            let metadata = match fs::symlink_metadata(&path) {
                Ok(value) if value.file_type().is_dir() && !value.file_type().is_symlink() => value,
                _ => continue,
            };
            let canonical_path = match fs::canonicalize(&path) {
                Ok(value)
                    if value.parent() == Some(canonical_projects_root.as_path())
                        && value.starts_with(&canonical_projects_root) => value,
                _ => continue,
            };
            let authority = canonical_path.join("project.sqlite");
            let authority_metadata = match fs::symlink_metadata(&authority) {
                Ok(value) if value.file_type().is_file() && !value.file_type().is_symlink() => {
                    value
                }
                _ => continue,
            };
            let modified = authority_metadata
                .modified()
                .or_else(|_| metadata.modified())
                .ok()
                .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
                .map(|value| value.as_millis().min(u128::from(u64::MAX)) as u64)
                .unwrap_or(0);
            let handle = project_handle(&storage_id);
            self.entries
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .insert(
                    handle.clone(),
                    HandleEntry {
                        kind: HandleKind::Project,
                        path: canonical_path,
                    },
                );
            projects.push(RecentProjectHandle {
                handle,
                last_modified_ms: modified,
            });
        }
        projects.sort_by(|left, right| {
            right
                .last_modified_ms
                .cmp(&left.last_modified_ms)
                .then_with(|| left.handle.cmp(&right.handle))
        });
        Ok(projects)
    }

    /// Revalidate a stable opaque project handle after restart. This never
    /// accepts an arbitrary path and cannot escape the Eco application root.
    pub(crate) fn open_project(
        &self,
        app_data_root: &Path,
        handle: &str,
    ) -> Result<OpaqueProjectHandle, HostError> {
        let storage_id = project_storage_id(handle).ok_or_else(|| {
            HostError::new(
                "PROJECT_HANDLE_INVALID",
                "The saved EcoCorridor project identity is invalid.",
                false,
            )
        })?;
        let canonical_app_root = fs::canonicalize(app_data_root).map_err(|_| HostError::staging())?;
        let projects_root = app_data_root.join("projects");
        let canonical_projects_root =
            contained_directory(&projects_root, app_data_root, &canonical_app_root)?;
        let project_path = projects_root.join(storage_id);
        let canonical_path =
            contained_directory(&project_path, app_data_root, &canonical_app_root)?;
        if canonical_path.parent() != Some(canonical_projects_root.as_path()) {
            return Err(HostError::new(
                "PROJECT_PATH_OUTSIDE_APP_DATA",
                "The saved project is outside EcoCorridor application storage.",
                false,
            ));
        }
        let authority = canonical_path.join("project.sqlite");
        let metadata = fs::symlink_metadata(&authority).map_err(|_| {
            HostError::new(
                "PROJECT_AUTHORITY_UNAVAILABLE",
                "The saved project has no readable local engineering record.",
                true,
            )
        })?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err(HostError::new(
                "PROJECT_AUTHORITY_UNSAFE",
                "The saved project's local engineering record is not a regular file.",
                false,
            ));
        }
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(
                handle.to_string(),
                HandleEntry {
                    kind: HandleKind::Project,
                    path: canonical_path,
                },
            );
        Ok(OpaqueProjectHandle {
            handle: handle.to_string(),
        })
    }

    pub(crate) fn inject_paths(
        &self,
        request: &mut WorkerRequest,
    ) -> Result<Option<String>, HostError> {
        let object = request.params.as_object_mut().ok_or_else(|| {
            HostError::new(
                "RPC_PARAMS_INVALID",
                "The request parameters are invalid.",
                false,
            )
        })?;
        for forbidden in ["bundle_path", "source_path", "project_directory"] {
            if object.contains_key(forbidden) {
                return Err(HostError::new(
                    "RAW_PATH_ARGUMENT_BLOCKED",
                    "File-system paths cannot be supplied by the interface. Choose the resource through the desktop host.",
                    false,
                ));
            }
        }

        self.replace_handle(object, "bundle_handle", "bundle_path", HandleKind::Bundle)?;
        self.replace_handle(object, "source_handle", "source_path", HandleKind::Asset)?;
        let project_handle = object
            .get("project_handle")
            .and_then(Value::as_str)
            .map(str::to_owned);
        self.replace_handle(
            object,
            "project_handle",
            "project_directory",
            HandleKind::Project,
        )?;

        if request.method == "connectivity.bundle.inspect" && !object.contains_key("bundle_path") {
            return Err(Self::missing_handle("bundle"));
        }
        if request.method == "connectivity.bundle.import"
            && (!object.contains_key("bundle_path") || !object.contains_key("project_directory"))
        {
            return Err(Self::missing_handle("bundle and project"));
        }
        if request.method == "connectivity.planning.create"
            && !object.contains_key("project_directory")
        {
            return Err(Self::missing_handle("project"));
        }
        if request.method == "connectivity.asset.add"
            && (!object.contains_key("source_path") || !object.contains_key("project_directory"))
        {
            return Err(Self::missing_handle("source asset and project"));
        }
        if request.method == "connectivity.asset.inspect"
            && (!object.contains_key("source_path") || !object.contains_key("project_directory"))
        {
            return Err(Self::missing_handle("source asset and project"));
        }
        Ok(project_handle)
    }

    pub(crate) fn register_export_receipt(
        &self,
        project_handle: &str,
        response: &Value,
    ) -> Result<(), HostError> {
        let data = response
            .get("data")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                export_copy_error(
                    "EXPORT_RECEIPT_INVALID",
                    "The worker did not return a valid export receipt.",
                )
            })?;
        let export_id = data
            .get("export_id")
            .and_then(Value::as_str)
            .filter(|value| safe_identifier(value))
            .ok_or_else(|| {
                export_copy_error(
                    "EXPORT_RECEIPT_INVALID",
                    "The worker returned an invalid export identity.",
                )
            })?;
        let relative = data
            .get("project_relative_directory")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                export_copy_error(
                    "EXPORT_RECEIPT_INVALID",
                    "The worker did not return a project-relative export location.",
                )
            })?;
        if relative != format!("exports/{export_id}") {
            return Err(export_copy_error(
                "EXPORT_RECEIPT_INVALID",
                "The export receipt does not identify its authoritative project directory.",
            ));
        }
        let manifest = data.get("manifest").cloned().ok_or_else(|| {
            export_copy_error(
                "EXPORT_RECEIPT_INVALID",
                "The worker did not return the committed export manifest.",
            )
        })?;
        if manifest.get("export_id").and_then(Value::as_str) != Some(export_id) {
            return Err(export_copy_error(
                "EXPORT_RECEIPT_INVALID",
                "The export manifest identity differs from its receipt.",
            ));
        }
        let project_root = self.resolve_project(project_handle)?;
        let source_directory = project_root.join("exports").join(export_id);
        verify_confirmed_export(&source_directory, export_id, &manifest)?;
        self.confirmed_exports
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(
                (project_handle.to_string(), export_id.to_string()),
                ConfirmedExportReceipt {
                    source_directory,
                    manifest,
                },
            );
        Ok(())
    }

    pub(crate) fn copy_confirmed_export(
        &self,
        project_handle: &str,
        export_id: &str,
        destination_parent: &Path,
    ) -> Result<ExportCopyReceipt, HostError> {
        if !safe_identifier(export_id) {
            return Err(export_copy_error(
                "EXPORT_ID_INVALID",
                "The evidence-package identity is invalid.",
            ));
        }
        // Resolve the project before consulting the receipt so a renderer cannot
        // pair an export identity with a handle of another kind or lifetime.
        let _project_root = self.resolve_project(project_handle)?;
        let receipt = self
            .confirmed_exports
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(&(project_handle.to_string(), export_id.to_string()))
            .cloned()
            .ok_or_else(|| export_copy_error("EXPORT_RECEIPT_REQUIRED", "Create and verify the evidence package in this desktop session before saving a copy."))?;
        verify_confirmed_export(&receipt.source_directory, export_id, &receipt.manifest)?;

        let parent_metadata = fs::symlink_metadata(destination_parent).map_err(|_| {
            export_copy_error(
                "EXPORT_DESTINATION_UNAVAILABLE",
                "The selected destination folder is unavailable.",
            )
        })?;
        if !parent_metadata.is_dir() || parent_metadata.file_type().is_symlink() {
            return Err(export_copy_error(
                "EXPORT_DESTINATION_UNSAFE",
                "Choose a regular destination folder. Symbolic links are not accepted.",
            ));
        }
        let canonical_parent = fs::canonicalize(destination_parent).map_err(|_| {
            export_copy_error(
                "EXPORT_DESTINATION_UNAVAILABLE",
                "The selected destination folder is unavailable.",
            )
        })?;
        let destination_label = format!("ecocorridor-evidence-{export_id}");
        let destination = canonical_parent.join(&destination_label);
        if destination.exists() || destination.symlink_metadata().is_ok() {
            return Err(export_copy_error(
                "EXPORT_DESTINATION_EXISTS",
                "A folder with this evidence-package name already exists. Choose another destination.",
            ));
        }
        fs::create_dir(&destination).map_err(|_| {
            export_copy_error(
                "EXPORT_COPY_FAILED",
                "The evidence-package copy could not be created.",
            )
        })?;
        let canonical_destination = match fs::canonicalize(&destination) {
            Ok(value) if value.starts_with(&canonical_parent) => value,
            _ => {
                let _ = fs::remove_dir(&destination);
                return Err(export_copy_error(
                    "EXPORT_DESTINATION_UNSAFE",
                    "The evidence-package destination could not be safely contained.",
                ));
            }
        };

        let copy_result = copy_verified_export_files(
            &receipt.source_directory,
            &canonical_destination,
            export_id,
            &receipt.manifest,
        );
        match copy_result {
            Ok((file_count, byte_size)) => Ok(ExportCopyReceipt {
                export_id: export_id.to_string(),
                copied: true,
                destination_label,
                file_count,
                byte_size,
            }),
            Err(error) => {
                // This directory was created by this exact operation and was
                // confirmed beneath the selected canonical parent above.
                let _ = fs::remove_dir_all(&canonical_destination);
                Err(error)
            }
        }
    }

    fn resolve_project(&self, project_handle: &str) -> Result<PathBuf, HostError> {
        let entries = self
            .entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let entry = entries.get(project_handle).ok_or_else(|| {
            HostError::new(
                "OPAQUE_PATH_HANDLE_UNKNOWN",
                "The selected project is no longer available. Open it again.",
                true,
            )
        })?;
        if entry.kind != HandleKind::Project {
            return Err(HostError::new(
                "OPAQUE_PATH_HANDLE_KIND_MISMATCH",
                "The selected resource is not an EcoCorridor project.",
                false,
            ));
        }
        Ok(entry.path.clone())
    }

    fn replace_handle(
        &self,
        object: &mut Map<String, Value>,
        handle_key: &str,
        path_key: &str,
        expected_kind: HandleKind,
    ) -> Result<(), HostError> {
        let Some(value) = object.remove(handle_key) else {
            return Ok(());
        };
        let handle = value
            .as_str()
            .ok_or_else(|| Self::missing_handle(expected_kind.prefix()))?;
        let entries = self
            .entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let entry = entries.get(handle).ok_or_else(|| {
            HostError::new(
                "OPAQUE_PATH_HANDLE_UNKNOWN",
                "The selected resource is no longer available. Choose it again.",
                true,
            )
        })?;
        if entry.kind != expected_kind {
            return Err(HostError::new(
                "OPAQUE_PATH_HANDLE_KIND_MISMATCH",
                "The selected resource cannot be used for this operation.",
                false,
            ));
        }
        object.insert(
            path_key.to_string(),
            Value::String(entry.path.display().to_string()),
        );
        Ok(())
    }

    fn missing_handle(label: &str) -> HostError {
        HostError::new(
            "OPAQUE_PATH_HANDLE_REQUIRED",
            format!("Choose a {label} through the desktop host before continuing."),
            false,
        )
    }

    pub(crate) fn release_staged(&self, handle: &str) {
        let entry = self
            .entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(handle);
        if let Some(entry) = entry.filter(|entry| entry.kind != HandleKind::Project) {
            remove_staged_entry(&entry);
        }
    }

    pub(crate) fn cleanup_staged(&self) {
        let staged = {
            let mut entries = self
                .entries
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let handles = entries
                .iter()
                .filter_map(|(handle, entry)| {
                    (entry.kind != HandleKind::Project).then_some(handle.clone())
                })
                .collect::<Vec<_>>();
            handles
                .into_iter()
                .filter_map(|handle| entries.remove(&handle))
                .collect::<Vec<_>>()
        };
        for entry in staged {
            remove_staged_entry(&entry);
        }
    }
}

fn safe_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        })
}

fn valid_project_storage_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn project_handle(storage_id: &str) -> String {
    format!("project_{storage_id}")
}

fn project_storage_id(handle: &str) -> Option<&str> {
    handle
        .strip_prefix("project_")
        .filter(|value| valid_project_storage_id(value))
}

fn export_copy_error(code: &'static str, message: &'static str) -> HostError {
    HostError::new(code, message, false)
}

fn manifest_files(manifest: &Value) -> Result<Vec<(PathBuf, String)>, HostError> {
    let files = manifest
        .get("files")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            export_copy_error(
                "EXPORT_MANIFEST_INVALID",
                "The evidence-package manifest has no file index.",
            )
        })?;
    if files.is_empty() || files.len() > MAX_EXPORT_COPY_FILES {
        return Err(export_copy_error(
            "EXPORT_MANIFEST_INVALID",
            "The evidence-package file index is empty or exceeds the desktop copy limit.",
        ));
    }
    let mut result = Vec::with_capacity(files.len());
    for (relative, digest) in files {
        let path = Path::new(relative);
        if path.is_absolute()
            || path
                .components()
                .any(|component| !matches!(component, std::path::Component::Normal(_)))
            || relative == "manifest.json"
        {
            return Err(export_copy_error(
                "EXPORT_MANIFEST_PATH_UNSAFE",
                "The evidence-package manifest contains an unsafe file path.",
            ));
        }
        let digest = digest
            .as_str()
            .filter(|value| {
                value.len() == 64
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            })
            .ok_or_else(|| {
                export_copy_error(
                    "EXPORT_MANIFEST_INVALID",
                    "The evidence-package manifest contains an invalid file digest.",
                )
            })?;
        result.push((path.to_path_buf(), digest.to_string()));
    }
    result.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(result)
}

fn verify_confirmed_export(
    source: &Path,
    export_id: &str,
    expected_manifest: &Value,
) -> Result<(), HostError> {
    let metadata = fs::symlink_metadata(source).map_err(|_| {
        export_copy_error(
            "EXPORT_SOURCE_UNAVAILABLE",
            "The authoritative evidence package is unavailable.",
        )
    })?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(export_copy_error(
            "EXPORT_SOURCE_UNSAFE",
            "The authoritative evidence-package directory is unsafe.",
        ));
    }
    let canonical_source = fs::canonicalize(source).map_err(|_| {
        export_copy_error(
            "EXPORT_SOURCE_UNAVAILABLE",
            "The authoritative evidence package is unavailable.",
        )
    })?;
    let manifest_path = canonical_source.join("manifest.json");
    let manifest_metadata = fs::symlink_metadata(&manifest_path).map_err(|_| {
        export_copy_error(
            "EXPORT_MANIFEST_UNAVAILABLE",
            "The evidence-package manifest is unavailable.",
        )
    })?;
    if !manifest_metadata.is_file() || manifest_metadata.file_type().is_symlink() {
        return Err(export_copy_error(
            "EXPORT_MANIFEST_UNSAFE",
            "The evidence-package manifest is unsafe.",
        ));
    }
    let manifest_bytes = fs::read(&manifest_path).map_err(|_| {
        export_copy_error(
            "EXPORT_MANIFEST_UNAVAILABLE",
            "The evidence-package manifest could not be read.",
        )
    })?;
    let manifest: Value = serde_json::from_slice(&manifest_bytes).map_err(|_| {
        export_copy_error(
            "EXPORT_MANIFEST_INVALID",
            "The evidence-package manifest is invalid.",
        )
    })?;
    if &manifest != expected_manifest
        || manifest.get("export_id").and_then(Value::as_str) != Some(export_id)
    {
        return Err(export_copy_error(
            "EXPORT_MANIFEST_CHANGED",
            "The evidence-package manifest changed after worker verification.",
        ));
    }
    let files = manifest_files(&manifest)?;
    let expected = files
        .iter()
        .map(|(path, _)| path.clone())
        .collect::<std::collections::BTreeSet<_>>();
    let mut discovered = std::collections::BTreeSet::new();
    let mut pending = vec![canonical_source.clone()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "The evidence package could not be inspected.",
            )
        })? {
            let entry = entry.map_err(|_| {
                export_copy_error(
                    "EXPORT_SOURCE_UNAVAILABLE",
                    "The evidence package could not be inspected.",
                )
            })?;
            let path = entry.path();
            let entry_metadata = fs::symlink_metadata(&path).map_err(|_| {
                export_copy_error(
                    "EXPORT_SOURCE_UNAVAILABLE",
                    "The evidence package could not be inspected.",
                )
            })?;
            if entry_metadata.file_type().is_symlink()
                || (!entry_metadata.is_file() && !entry_metadata.is_dir())
            {
                return Err(export_copy_error(
                    "EXPORT_SOURCE_UNSAFE",
                    "The evidence package contains a symbolic link or special file.",
                ));
            }
            if entry_metadata.is_dir() {
                pending.push(path);
            } else {
                let relative = path
                    .strip_prefix(&canonical_source)
                    .map_err(|_| {
                        export_copy_error(
                            "EXPORT_SOURCE_UNSAFE",
                            "An evidence-package file escaped its authoritative directory.",
                        )
                    })?
                    .to_path_buf();
                if relative != Path::new("manifest.json") {
                    discovered.insert(relative);
                }
            }
        }
    }
    if discovered != expected {
        return Err(export_copy_error(
            "EXPORT_SOURCE_CHANGED",
            "The evidence-package files differ from the committed manifest.",
        ));
    }
    let mut verified_bytes = 0_u64;
    for (relative, expected_digest) in &files {
        let source_path = canonical_source.join(relative);
        let source_canonical = fs::canonicalize(&source_path).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "An evidence-package file is unavailable.",
            )
        })?;
        if !source_canonical.starts_with(&canonical_source) {
            return Err(export_copy_error(
                "EXPORT_SOURCE_UNSAFE",
                "An evidence-package file escaped its authoritative directory.",
            ));
        }
        let (byte_size, actual_digest) = hash_export_source(&source_path)?;
        verified_bytes = verified_bytes.checked_add(byte_size).ok_or_else(|| {
            export_copy_error(
                "EXPORT_COPY_LIMIT",
                "The evidence package exceeds the desktop copy limit.",
            )
        })?;
        if verified_bytes > MAX_EXPORT_COPY_BYTES {
            return Err(export_copy_error(
                "EXPORT_COPY_LIMIT",
                "The evidence package exceeds the 16 GiB desktop copy limit.",
            ));
        }
        if &actual_digest != expected_digest {
            return Err(export_copy_error(
                "EXPORT_SOURCE_CHANGED",
                "An evidence-package file differs from the committed manifest.",
            ));
        }
    }
    Ok(())
}

fn hash_export_source(path: &Path) -> Result<(u64, String), HostError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| {
        export_copy_error(
            "EXPORT_SOURCE_UNAVAILABLE",
            "An evidence-package file is unavailable.",
        )
    })?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(export_copy_error(
            "EXPORT_SOURCE_UNSAFE",
            "An evidence-package file is unsafe.",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW);
    }
    let mut reader = BufReader::new(options.open(path).map_err(|_| {
        export_copy_error(
            "EXPORT_SOURCE_UNAVAILABLE",
            "An evidence-package file could not be opened safely.",
        )
    })?);
    let mut hasher = Sha256::new();
    let mut bytes_read = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = io::Read::read(&mut reader, &mut buffer).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "An evidence-package file could not be read.",
            )
        })?;
        if count == 0 {
            break;
        }
        bytes_read = bytes_read.checked_add(count as u64).ok_or_else(|| {
            export_copy_error(
                "EXPORT_COPY_LIMIT",
                "The evidence package exceeds the desktop copy limit.",
            )
        })?;
        hasher.update(&buffer[..count]);
    }
    if bytes_read != metadata.len() {
        return Err(export_copy_error(
            "EXPORT_SOURCE_CHANGED",
            "An evidence-package file changed while it was verified.",
        ));
    }
    Ok((bytes_read, format!("{:x}", hasher.finalize())))
}

fn copy_verified_export_files(
    source: &Path,
    destination: &Path,
    export_id: &str,
    manifest: &Value,
) -> Result<(usize, u64), HostError> {
    verify_confirmed_export(source, export_id, manifest)?;
    let mut files = manifest_files(manifest)?;
    let manifest_digest = format!(
        "{:x}",
        Sha256::digest(
            fs::read(source.join("manifest.json")).map_err(|_| export_copy_error(
                "EXPORT_MANIFEST_UNAVAILABLE",
                "The evidence-package manifest could not be read."
            ))?
        )
    );
    files.push((PathBuf::from("manifest.json"), manifest_digest));
    let canonical_source = fs::canonicalize(source).map_err(|_| {
        export_copy_error(
            "EXPORT_SOURCE_UNAVAILABLE",
            "The authoritative evidence package is unavailable.",
        )
    })?;
    let mut total = 0_u64;
    for (relative, expected_digest) in &files {
        let source_path = canonical_source.join(relative);
        let source_canonical = fs::canonicalize(&source_path).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "An evidence-package file is unavailable.",
            )
        })?;
        if !source_canonical.starts_with(&canonical_source) {
            return Err(export_copy_error(
                "EXPORT_SOURCE_UNSAFE",
                "An evidence-package file escaped its authoritative directory.",
            ));
        }
        let source_metadata = fs::symlink_metadata(&source_path).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "An evidence-package file is unavailable.",
            )
        })?;
        if !source_metadata.is_file() || source_metadata.file_type().is_symlink() {
            return Err(export_copy_error(
                "EXPORT_SOURCE_UNSAFE",
                "An evidence-package file is unsafe.",
            ));
        }
        total = total.checked_add(source_metadata.len()).ok_or_else(|| {
            export_copy_error(
                "EXPORT_COPY_LIMIT",
                "The evidence package exceeds the desktop copy limit.",
            )
        })?;
        if total > MAX_EXPORT_COPY_BYTES {
            return Err(export_copy_error(
                "EXPORT_COPY_LIMIT",
                "The evidence package exceeds the 16 GiB desktop copy limit.",
            ));
        }
        let destination_path = destination.join(relative);
        if let Some(parent) = destination_path.parent() {
            fs::create_dir_all(parent).map_err(|_| {
                export_copy_error(
                    "EXPORT_COPY_FAILED",
                    "The evidence-package copy could not be created.",
                )
            })?;
            let canonical_parent = fs::canonicalize(parent).map_err(|_| {
                export_copy_error(
                    "EXPORT_COPY_FAILED",
                    "The evidence-package copy could not be contained.",
                )
            })?;
            if !canonical_parent.starts_with(destination) {
                return Err(export_copy_error(
                    "EXPORT_DESTINATION_UNSAFE",
                    "An evidence-package destination escaped the selected folder.",
                ));
            }
        }
        let mut source_options = OpenOptions::new();
        source_options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            source_options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW);
        }
        let mut reader = BufReader::new(source_options.open(&source_path).map_err(|_| {
            export_copy_error(
                "EXPORT_SOURCE_UNAVAILABLE",
                "An evidence-package file could not be opened safely.",
            )
        })?);
        let destination_file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&destination_path)
            .map_err(|_| {
                export_copy_error(
                    "EXPORT_COPY_FAILED",
                    "An evidence-package file could not be created.",
                )
            })?;
        let mut writer = BufWriter::new(destination_file);
        let mut hasher = Sha256::new();
        let mut copied = 0_u64;
        let mut buffer = vec![0_u8; 1024 * 1024];
        loop {
            let count = io::Read::read(&mut reader, &mut buffer).map_err(|_| {
                export_copy_error(
                    "EXPORT_COPY_FAILED",
                    "An evidence-package file could not be read.",
                )
            })?;
            if count == 0 {
                break;
            }
            copied += count as u64;
            hasher.update(&buffer[..count]);
            writer.write_all(&buffer[..count]).map_err(|_| {
                export_copy_error(
                    "EXPORT_COPY_FAILED",
                    "An evidence-package file could not be written.",
                )
            })?;
        }
        writer.flush().map_err(|_| {
            export_copy_error(
                "EXPORT_COPY_FAILED",
                "An evidence-package file could not be written.",
            )
        })?;
        writer.get_ref().sync_all().map_err(|_| {
            export_copy_error(
                "EXPORT_COPY_FAILED",
                "An evidence-package file could not be committed.",
            )
        })?;
        if copied != source_metadata.len() || format!("{:x}", hasher.finalize()) != *expected_digest
        {
            return Err(export_copy_error(
                "EXPORT_SOURCE_CHANGED",
                "An evidence-package file changed while it was copied.",
            ));
        }
    }
    Ok((files.len(), total))
}

fn remove_staged_entry(entry: &HandleEntry) {
    // Staged entries are files created by this process under one random, dedicated
    // directory. Remove only the exact registered file and then its empty parent;
    // never recursively delete a renderer-supplied or unresolved path.
    match fs::symlink_metadata(&entry.path) {
        Ok(metadata) if metadata.file_type().is_file() && !metadata.file_type().is_symlink() => {
            let _ = fs::remove_file(&entry.path);
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        _ => return,
    }
    if let Some(parent) = entry.path.parent() {
        let _ = fs::remove_dir(parent);
    }
}

fn contained_directory(
    path: &Path,
    app_data_root: &Path,
    canonical_root: &Path,
) -> Result<PathBuf, HostError> {
    let canonical_path = fs::canonicalize(path).map_err(|_| HostError::staging())?;
    if !canonical_path.starts_with(canonical_root) {
        return Err(HostError::new(
            "APP_DATA_PATH_OUTSIDE_ROOT",
            "The selected resource could not be contained in EcoCorridor application storage.",
            false,
        ));
    }
    let relative = path.strip_prefix(app_data_root).map_err(|_| {
        HostError::new(
            "APP_DATA_PATH_OUTSIDE_ROOT",
            "The selected resource could not be contained in EcoCorridor application storage.",
            false,
        )
    })?;
    let mut current = app_data_root.to_path_buf();
    for component in relative.components() {
        current.push(component);
        let metadata = fs::symlink_metadata(&current).map_err(|_| HostError::staging())?;
        if metadata.file_type().is_symlink() {
            return Err(HostError::new(
                "APP_DATA_SYMLINK_REJECTED",
                "EcoCorridor application storage contains an unsafe symbolic link.",
                false,
            ));
        }
    }
    Ok(canonical_path)
}

pub(crate) fn redact_worker_paths(
    value: &mut Value,
    project_handle: Option<&str>,
) -> Result<(), HostError> {
    let Some(data) = value.get_mut("data") else {
        return Ok(());
    };
    if !redact_paths_recursive(data, project_handle) {
        return Ok(());
    }
    let canonical = canonical_json(data);
    let digest = format!("{:x}", Sha256::digest(canonical.as_bytes()));
    if let Some(object) = value.as_object_mut() {
        object.insert("data_sha256".into(), Value::String(digest));
    }
    Ok(())
}

fn redact_paths_recursive(value: &mut Value, project_handle: Option<&str>) -> bool {
    match value {
        Value::Array(items) => items.iter_mut().fold(false, |changed, item| {
            redact_paths_recursive(item, project_handle) || changed
        }),
        Value::Object(object) => {
            let mut changed = object.remove("bundle_path").is_some();
            changed = object.remove("source_path").is_some() || changed;
            if object.remove("project_directory").is_some() {
                changed = true;
                if let Some(handle) = project_handle {
                    object.insert("project_handle".into(), Value::String(handle.to_string()));
                }
            }
            object.values_mut().fold(changed, |changed, item| {
                redact_paths_recursive(item, project_handle) || changed
            })
        }
        _ => false,
    }
}

fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(item) => item.to_string(),
        Value::Number(item) => item.to_string(),
        Value::String(item) => {
            serde_json::to_string(item).expect("string serialization is infallible")
        }
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(canonical_json)
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Object(object) => {
            let mut entries = object.iter().collect::<Vec<_>>();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            let body = entries
                .into_iter()
                .map(|(key, item)| {
                    format!(
                        "{}:{}",
                        serde_json::to_string(key).expect("key serialization is infallible"),
                        canonical_json(item)
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{body}}}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stages_bytes_and_never_exposes_the_source_path() {
        let root = std::env::temp_dir().join(format!("eco-path-test-{}", Uuid::new_v4()));
        let source_root = std::env::temp_dir().join(format!("eco-source-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&source_root).unwrap();
        let source = source_root.join("named-after-sensitive-site.ecocorridor");
        fs::write(&source, b"fixture bytes").unwrap();
        let registry = PathHandleRegistry::new();
        let selected = registry.stage_bundle(&root, &source).unwrap();
        assert_eq!(
            selected.display_name,
            "named-after-sensitive-site.ecocorridor"
        );
        assert!(!selected.handle.contains("sensitive"));
        let entry = registry
            .entries
            .lock()
            .unwrap()
            .get(&selected.handle)
            .unwrap()
            .clone();
        assert!(entry.path.starts_with(root.canonicalize().unwrap()));
        assert_eq!(fs::read(entry.path).unwrap(), b"fixture bytes");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(source_root).unwrap();
    }

    #[test]
    fn injects_only_registered_handles_and_rejects_raw_paths() {
        let root = std::env::temp_dir().join(format!("eco-project-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let registry = PathHandleRegistry::new();
        let project = registry.create_project(&root).unwrap();
        let mut request = WorkerRequest {
            method: "connectivity.model.validate".into(),
            params: serde_json::json!({"project_handle": project.handle}),
            timeout_ms: None,
        };
        let handle = registry.inject_paths(&mut request).unwrap();
        assert!(request.params.get("project_directory").is_some());
        assert_eq!(
            handle.as_deref(),
            request
                .params
                .get("project_directory")
                .and_then(Value::as_str)
                .map(|_| project.handle.as_str())
        );

        let mut raw = WorkerRequest {
            method: "connectivity.model.validate".into(),
            params: serde_json::json!({"project_directory": "/tmp/forged"}),
            timeout_ms: None,
        };
        let error = serde_json::to_value(registry.inject_paths(&mut raw).unwrap_err()).unwrap();
        assert_eq!(error["code"], "RAW_PATH_ARGUMENT_BLOCKED");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn redacts_worker_paths_and_rehashes_the_projection() {
        let mut value = serde_json::json!({
            "api_version": "1.0",
            "method": "connectivity.bundle.import",
            "data": {
                "project_directory": "/Users/private/project",
                "state": "IMPORTED",
                "nested": [{"bundle_path": "/Users/private/bundle.zip"}]
            },
            "data_sha256": "0".repeat(64)
        });
        redact_worker_paths(&mut value, Some("project_opaque")).unwrap();
        assert_eq!(value["data"]["project_handle"], "project_opaque");
        assert!(!value.to_string().contains("/Users/private"));
        assert_eq!(value["data_sha256"].as_str().unwrap().len(), 64);
    }

    #[test]
    fn successful_lifetime_cleanup_removes_only_staged_files() {
        let root = std::env::temp_dir().join(format!("eco-lifetime-test-{}", Uuid::new_v4()));
        let source_root =
            std::env::temp_dir().join(format!("eco-lifetime-source-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&source_root).unwrap();
        let source = source_root.join("bundle.ecocorridor");
        fs::write(&source, b"fixture bytes").unwrap();
        let registry = PathHandleRegistry::new();
        let selected = registry.stage_bundle(&root, &source).unwrap();
        let staged = registry
            .entries
            .lock()
            .unwrap()
            .get(&selected.handle)
            .unwrap()
            .path
            .clone();
        registry.release_staged(&selected.handle);
        assert!(!staged.exists());
        assert!(source.exists());
        assert!(!registry
            .entries
            .lock()
            .unwrap()
            .contains_key(&selected.handle));
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(source_root).unwrap();
    }

    #[test]
    fn asset_handles_are_kind_bound_and_cleanup_preserves_projects() {
        let root = std::env::temp_dir().join(format!("eco-asset-test-{}", Uuid::new_v4()));
        let source_root =
            std::env::temp_dir().join(format!("eco-asset-source-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&source_root).unwrap();
        let source = source_root.join("reviewed-layer.tif");
        fs::write(&source, b"reviewed source bytes").unwrap();
        let registry = PathHandleRegistry::new();
        let asset = registry.stage_asset(&root, &source).unwrap();
        let project = registry.create_project(&root).unwrap();
        let project_path = registry
            .entries
            .lock()
            .unwrap()
            .get(&project.handle)
            .unwrap()
            .path
            .clone();
        let mut request = WorkerRequest {
            method: "connectivity.asset.add".into(),
            params: serde_json::json!({
                "source_handle": asset.handle,
                "project_handle": project.handle,
                "asset": {}
            }),
            timeout_ms: None,
        };
        registry.inject_paths(&mut request).unwrap();
        assert!(request.params.get("source_path").is_some());
        assert!(request.params.get("project_directory").is_some());

        let mut raw = WorkerRequest {
            method: "connectivity.asset.add".into(),
            params: serde_json::json!({
                "source_path": "/tmp/forged.tif",
                "project_handle": project.handle,
                "asset": {}
            }),
            timeout_ms: None,
        };
        let error = serde_json::to_value(registry.inject_paths(&mut raw).unwrap_err()).unwrap();
        assert_eq!(error["code"], "RAW_PATH_ARGUMENT_BLOCKED");

        registry.cleanup_staged();
        assert!(project_path.is_dir());
        assert!(!registry.entries.lock().unwrap().contains_key(&asset.handle));
        assert!(registry
            .entries
            .lock()
            .unwrap()
            .contains_key(&project.handle));
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(source_root).unwrap();
    }

    #[test]
    fn persisted_projects_reopen_with_stable_opaque_handles_after_registry_restart() {
        let root = std::env::temp_dir().join(format!("eco-reopen-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let first_registry = PathHandleRegistry::new();
        let created = first_registry.create_project(&root).unwrap();
        let project_root = first_registry.resolve_project(&created.handle).unwrap();
        fs::write(project_root.join("project.sqlite"), b"sqlite authority fixture").unwrap();
        drop(first_registry);

        let reopened_registry = PathHandleRegistry::new();
        let recent = reopened_registry.list_projects(&root).unwrap();
        assert_eq!(recent.len(), 1);
        assert_eq!(recent[0].handle, created.handle);
        let reopened = reopened_registry
            .open_project(&root, &recent[0].handle)
            .unwrap();
        assert_eq!(reopened.handle, created.handle);

        let mut request = WorkerRequest {
            method: "connectivity.project.get_state".into(),
            params: serde_json::json!({"project_handle": reopened.handle}),
            timeout_ms: None,
        };
        let returned_handle = reopened_registry.inject_paths(&mut request).unwrap();
        assert_eq!(returned_handle.as_deref(), Some(created.handle.as_str()));
        let injected = PathBuf::from(
            request
                .params
                .get("project_directory")
                .and_then(Value::as_str)
                .unwrap(),
        );
        assert_eq!(injected, project_root.canonicalize().unwrap());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn project_reopen_rejects_forged_handles_and_non_project_directories() {
        let root = std::env::temp_dir().join(format!("eco-reopen-block-test-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("projects").join("not-a-project")).unwrap();
        let valid_but_empty = "0123456789abcdef0123456789abcdef";
        fs::create_dir_all(root.join("projects").join(valid_but_empty)).unwrap();
        let registry = PathHandleRegistry::new();

        assert!(registry.list_projects(&root).unwrap().is_empty());
        for forged in [
            "project_../../outside",
            "project_0123456789ABCDEF0123456789ABCDEF",
            "bundle_0123456789abcdef0123456789abcdef",
            "project_short",
        ] {
            let error = registry.open_project(&root, forged).unwrap_err();
            assert_eq!(
                serde_json::to_value(error).unwrap()["code"],
                "PROJECT_HANDLE_INVALID"
            );
        }
        let unavailable = registry
            .open_project(&root, &project_handle(valid_but_empty))
            .unwrap_err();
        assert_eq!(
            serde_json::to_value(unavailable).unwrap()["code"],
            "PROJECT_AUTHORITY_UNAVAILABLE"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn persisted_project_scan_rejects_symlinked_directories_and_authority_files() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!("eco-reopen-link-test-{}", Uuid::new_v4()));
        let outside =
            std::env::temp_dir().join(format!("eco-reopen-link-outside-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("projects")).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("project.sqlite"), b"outside").unwrap();
        symlink(
            &outside,
            root.join("projects").join("0123456789abcdef0123456789abcdef"),
        )
        .unwrap();
        let second = root
            .join("projects")
            .join("fedcba9876543210fedcba9876543210");
        fs::create_dir_all(&second).unwrap();
        symlink(outside.join("project.sqlite"), second.join("project.sqlite")).unwrap();

        let registry = PathHandleRegistry::new();
        assert!(registry.list_projects(&root).unwrap().is_empty());
        assert!(registry
            .open_project(
                &root,
                "project_0123456789abcdef0123456789abcdef"
            )
            .is_err());
        assert!(registry
            .open_project(
                &root,
                "project_fedcba9876543210fedcba9876543210"
            )
            .is_err());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(outside).unwrap();
    }

    #[test]
    fn saves_only_a_worker_confirmed_hash_verified_export_copy() {
        let root = std::env::temp_dir().join(format!("eco-export-project-test-{}", Uuid::new_v4()));
        let destination_parent =
            std::env::temp_dir().join(format!("eco-export-copy-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&destination_parent).unwrap();
        let registry = PathHandleRegistry::new();
        let project = registry.create_project(&root).unwrap();
        let project_root = registry.resolve_project(&project.handle).unwrap();
        let export_id = "export_verified_01";
        let export_root = project_root.join("exports").join(export_id);
        fs::create_dir_all(export_root.join("artifacts")).unwrap();
        let decision = b"{\"decision\":\"screening only\"}";
        let artifact = b"direct model artifact";
        fs::write(export_root.join("decision-board.json"), decision).unwrap();
        fs::write(export_root.join("artifacts").join("run.rst"), artifact).unwrap();
        let manifest = serde_json::json!({
            "schema_version": "ecocorridor-export/v1.0",
            "export_id": export_id,
            "project_id": "project-1",
            "revision_id": "revision-1",
            "conceptual": true,
            "notice": "screening only",
            "files": {
                "decision-board.json": format!("{:x}", Sha256::digest(decision)),
                "artifacts/run.rst": format!("{:x}", Sha256::digest(artifact)),
            },
            "direct_run_ids": ["run-1"],
            "evidence_claim_ids": ["claim-1"],
            "artifact_files": {},
            "sync_state": "CONNECTED",
            "redacted_static_story": "decision-story.html",
            "sharing_review_required": true,
        });
        fs::write(
            export_root.join("manifest.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        let response = serde_json::json!({
            "api_version": "1.0",
            "method": "connectivity.export.create",
            "data": {
                "export_id": export_id,
                "project_relative_directory": format!("exports/{export_id}"),
                "manifest": manifest,
            },
            "data_sha256": "0".repeat(64),
        });
        registry
            .register_export_receipt(&project.handle, &response)
            .unwrap();
        let saved = registry
            .copy_confirmed_export(&project.handle, export_id, &destination_parent)
            .unwrap();
        assert!(saved.copied);
        assert_eq!(saved.file_count, 3);
        assert!(!saved
            .destination_label
            .contains(destination_parent.to_string_lossy().as_ref()));
        let copied = destination_parent.join(&saved.destination_label);
        assert_eq!(
            fs::read(copied.join("decision-board.json")).unwrap(),
            decision
        );
        assert_eq!(
            fs::read(copied.join("artifacts").join("run.rst")).unwrap(),
            artifact
        );

        fs::write(export_root.join("decision-board.json"), b"changed").unwrap();
        let changed = registry
            .copy_confirmed_export(
                &project.handle,
                export_id,
                &destination_parent.join("other"),
            )
            .unwrap_err();
        assert_eq!(
            serde_json::to_value(changed).unwrap()["code"],
            "EXPORT_SOURCE_CHANGED"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(destination_parent).unwrap();
    }
}
