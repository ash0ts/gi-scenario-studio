use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;

use crate::error::PlatformError;

const MAX_ARCHIVE_BYTES: u64 = 2 * 1024 * 1024 * 1024;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct StagedBundle {
    pub staged_path: String,
    pub display_name: String,
    pub sha256: String,
    pub bytes: u64,
}

pub(crate) fn project_data_root(app: &AppHandle) -> Result<PathBuf, PlatformError> {
    app.path()
        .app_data_dir()
        .map(|path| path.join("projects"))
        .map_err(|error| PlatformError::worker_unavailable(error.to_string()))
}

fn safe_source(path: &Path) -> Result<(fs::Metadata, String), PlatformError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| PlatformError::invalid_request(error.to_string()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(PlatformError::invalid_request(
            "the selected project bundle must be a regular file, not a symbolic link",
        ));
    }
    if metadata.len() == 0 || metadata.len() > MAX_ARCHIVE_BYTES {
        return Err(PlatformError::invalid_request(
            "the selected project bundle is empty or exceeds the 2 GiB staging limit",
        ));
    }
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    if !extension.eq_ignore_ascii_case("zip") {
        return Err(PlatformError::invalid_request(
            "select a ZIP bundle containing the SWMM INP and every referenced dependency",
        ));
    }
    let display_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("swmm-project.zip")
        .to_owned();
    Ok((metadata, display_name))
}

fn verify_staged_file(
    path: &Path,
    expected_bytes: u64,
    expected_sha256: &str,
) -> Result<(), PlatformError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() != expected_bytes
    {
        return Err(PlatformError::worker_unavailable(
            "an existing staged bundle failed integrity verification",
        ));
    }
    let mut input =
        File::open(path).map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
    let mut digest = Sha256::new();
    let mut read_bytes = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = input
            .read(&mut buffer)
            .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
        if read == 0 {
            break;
        }
        read_bytes = read_bytes.saturating_add(read as u64);
        if read_bytes > expected_bytes {
            return Err(PlatformError::worker_unavailable(
                "an existing staged bundle failed integrity verification",
            ));
        }
        digest.update(&buffer[..read]);
    }
    if read_bytes != expected_bytes || hex::encode(digest.finalize()) != expected_sha256 {
        return Err(PlatformError::worker_unavailable(
            "an existing staged bundle failed integrity verification",
        ));
    }
    Ok(())
}

fn stage(app: &AppHandle, source: &Path) -> Result<StagedBundle, PlatformError> {
    let (metadata, display_name) = safe_source(source)?;
    let imports = project_data_root(app)?.join("_imports");
    fs::create_dir_all(&imports)
        .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?
        .as_nanos();
    let temporary = imports.join(format!(".staging-{}-{nonce}", std::process::id()));
    let mut input =
        File::open(source).map_err(|error| PlatformError::invalid_request(error.to_string()))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
    let mut digest = Sha256::new();
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    let result = (|| -> Result<(), PlatformError> {
        loop {
            let read = input
                .read(&mut buffer)
                .map_err(|error| PlatformError::invalid_request(error.to_string()))?;
            if read == 0 {
                break;
            }
            copied = copied.saturating_add(read as u64);
            if copied > MAX_ARCHIVE_BYTES || copied > metadata.len() {
                return Err(PlatformError::invalid_request(
                    "the selected bundle changed or exceeded its staging limit while being read",
                ));
            }
            digest.update(&buffer[..read]);
            output
                .write_all(&buffer[..read])
                .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
        }
        if copied != metadata.len() {
            return Err(PlatformError::invalid_request(
                "the selected bundle changed while it was being staged",
            ));
        }
        output
            .sync_all()
            .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
        Ok(())
    })();
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    let sha256 = hex::encode(digest.finalize());
    let destination = imports.join(format!("{sha256}.zip"));
    if destination.exists() {
        if let Err(error) = verify_staged_file(&destination, copied, &sha256) {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        let _ = fs::remove_file(&temporary);
    } else {
        fs::rename(&temporary, &destination)
            .map_err(|error| PlatformError::worker_unavailable(error.to_string()))?;
    }
    Ok(StagedBundle {
        staged_path: destination.to_string_lossy().into_owned(),
        display_name,
        sha256,
        bytes: copied,
    })
}

pub(crate) async fn select_and_stage(
    app: AppHandle,
) -> Result<Option<StagedBundle>, PlatformError> {
    let selected = app
        .dialog()
        .file()
        .add_filter("Portable SWMM project", &["zip"])
        .set_title("Open a complete SWMM project bundle")
        .blocking_pick_file();
    let Some(selected) = selected else {
        return Ok(None);
    };
    let path = selected
        .into_path()
        .map_err(|error| PlatformError::invalid_request(error.to_string()))?;
    stage(&app, &path).map(Some)
}

#[cfg(test)]
mod tests {
    use sha2::{Digest, Sha256};

    use super::{safe_source, verify_staged_file};

    #[test]
    fn staging_rejects_non_zip_sources() {
        let temporary = std::env::temp_dir().join("gi-bundle-reject.txt");
        std::fs::write(&temporary, b"not a portable bundle").expect("temporary fixture");
        let result = safe_source(&temporary);
        let _ = std::fs::remove_file(&temporary);
        assert!(result.is_err());
    }

    #[test]
    fn staged_reuse_rehashes_existing_content() {
        let temporary = std::env::temp_dir().join("gi-bundle-staged-integrity.zip");
        let content = b"portable bundle fixture";
        std::fs::write(&temporary, content).expect("temporary fixture");
        let expected = hex::encode(Sha256::digest(content));

        assert!(verify_staged_file(&temporary, content.len() as u64, &expected).is_ok());
        std::fs::write(&temporary, b"tampered bundle fixture").expect("tampered fixture");
        assert!(verify_staged_file(&temporary, content.len() as u64, &expected).is_err());
        let _ = std::fs::remove_file(&temporary);
    }
}
