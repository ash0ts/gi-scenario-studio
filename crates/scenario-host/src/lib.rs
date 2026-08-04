//! Security-critical, domain-neutral host primitives for scenario applications.
//!
//! This crate does not understand hydraulics or ecology. It verifies packaged
//! resources, constrains project paths and RPC methods, and constructs a
//! shell-free worker command. Application crates provide an [`AppProfile`].

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{Display, Formatter};
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use zeroize::Zeroizing;

const MAX_RELEASE_MANIFEST_BYTES: u64 = 32 * 1024 * 1024;
const MAX_RELEASE_RESOURCES: usize = 100_000;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AppProfile {
    pub application_id: String,
    pub application_version: String,
    pub api_version: String,
    pub worker_resource: String,
    pub engine_resource: String,
    pub engine_manifest_resource: String,
    pub guided_projection_resource: String,
    pub guided_manifest_resource: String,
    /// Raw Ed25519 public key encoded as lowercase hex. Source and public
    /// replay builds may leave this empty; verified worker startup then fails
    /// closed until a release build injects the real key.
    pub release_public_key_hex: String,
    /// Exact Git commit compiled into a signed release. Development builds may
    /// leave this empty only while no release verification key is present.
    pub release_source_commit_sha: String,
    pub release_target: String,
    pub keychain_service: String,
    pub app_data_directory: String,
    pub allowed_rpc_methods: BTreeSet<String>,
    pub maximum_rpc_bytes: usize,
}

impl AppProfile {
    pub fn validate(&self) -> Result<(), HostError> {
        if self.application_id.trim().is_empty()
            || !is_semver(&self.application_version)
            || self.api_version.trim().is_empty()
            || self.keychain_service.trim().is_empty()
            || self.app_data_directory.trim().is_empty()
        {
            return Err(HostError::InvalidProfile);
        }
        if !is_release_target(&self.release_target)
            || (!self.release_public_key_hex.is_empty()
                && !is_source_commit(&self.release_source_commit_sha))
            || (self.release_public_key_hex.is_empty()
                && !self.release_source_commit_sha.is_empty()
                && !is_source_commit(&self.release_source_commit_sha))
        {
            return Err(HostError::InvalidProfile);
        }
        if !self.release_public_key_hex.is_empty()
            && (self.release_public_key_hex.len() != 64
                || !self
                    .release_public_key_hex
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
        {
            return Err(HostError::InvalidProfile);
        }
        validate_relative_path(Path::new(&self.worker_resource))?;
        validate_relative_path(Path::new(&self.engine_resource))?;
        validate_relative_path(Path::new(&self.engine_manifest_resource))?;
        validate_relative_path(Path::new(&self.guided_projection_resource))?;
        validate_relative_path(Path::new(&self.guided_manifest_resource))?;
        if self.maximum_rpc_bytes == 0 || self.maximum_rpc_bytes > 16 * 1024 * 1024 {
            return Err(HostError::InvalidProfile);
        }
        if self.allowed_rpc_methods.is_empty()
            || self
                .allowed_rpc_methods
                .iter()
                .any(|method| method.trim().is_empty() || method.contains('*'))
        {
            return Err(HostError::InvalidProfile);
        }
        Ok(())
    }

    pub fn authorize_rpc(&self, method: &str, request_bytes: usize) -> Result<(), HostError> {
        if request_bytes > self.maximum_rpc_bytes {
            return Err(HostError::RpcTooLarge);
        }
        if !self.allowed_rpc_methods.contains(method) {
            return Err(HostError::RpcMethodDenied);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceEntry {
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
    pub executable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReleaseManifest {
    pub schema_version: String,
    pub application_id: String,
    pub application_version: String,
    pub source_commit_sha: String,
    pub target: String,
    pub resources: Vec<ResourceEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedResource {
    pub path: PathBuf,
    pub sha256: String,
    pub executable: bool,
}

/// Typed inputs for one signed release-manifest verification.
///
/// Keeping the expected release identity beside the paths prevents callers
/// from accidentally swapping adjacent string arguments at this security
/// boundary.
#[derive(Debug, Clone, Copy)]
pub struct ReleaseManifestVerification<'a> {
    pub manifest_path: &'a Path,
    pub signature_path: &'a Path,
    pub resource_root: &'a Path,
    pub expected_application_id: &'a str,
    pub expected_application_version: &'a str,
    pub expected_source_commit_sha: &'a str,
    pub expected_target: &'a str,
    pub public_key_hex: &'a str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerCommand {
    pub executable: PathBuf,
    pub arguments: Vec<String>,
    pub environment: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Copy)]
pub struct WorkerResources<'a> {
    pub engine: &'a VerifiedResource,
    pub engine_manifest: &'a VerifiedResource,
    pub guided_projection: &'a VerifiedResource,
    pub guided_manifest: &'a VerifiedResource,
    pub guided_evidence_archive: &'a VerifiedResource,
}

impl WorkerCommand {
    /// Returns a command description for direct process invocation. Consumers
    /// must pass these fields to `std::process::Command`; never to a shell.
    pub fn new(
        executable: PathBuf,
        resources: WorkerResources<'_>,
        api_version: &str,
        app_data_root: &Path,
        environment: BTreeMap<String, String>,
    ) -> Result<Self, HostError> {
        let WorkerResources {
            engine,
            engine_manifest,
            guided_projection,
            guided_manifest,
            guided_evidence_archive,
        } = resources;
        if !executable.is_file() || executable.is_symlink() {
            return Err(HostError::ResourceNotRegularFile);
        }
        if !engine.executable || !engine.path.is_file() || engine.path.is_symlink() {
            return Err(HostError::ResourceNotRegularFile);
        }
        if engine_manifest.executable
            || !engine_manifest.path.is_file()
            || engine_manifest.path.is_symlink()
        {
            return Err(HostError::ResourceNotRegularFile);
        }
        for resource in [guided_projection, guided_manifest, guided_evidence_archive] {
            if resource.executable || !resource.path.is_file() || resource.path.is_symlink() {
                return Err(HostError::ResourceNotRegularFile);
            }
        }
        if !app_data_root.is_absolute() {
            return Err(HostError::PathOutsideRoot);
        }
        const ALLOWED_ENVIRONMENT: &[&str] = &[
            "ECOCORRIDOR_CREDENTIAL_HANDLE",
            "ECOCORRIDOR_PROVIDER_CREDENTIAL_HANDLE",
            "LANG",
            "LC_ALL",
            "NO_COLOR",
            "PATH",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "TMPDIR",
            "USERPROFILE",
            "WANDB_BASE_URL",
        ];
        if environment
            .keys()
            .any(|key| !ALLOWED_ENVIRONMENT.contains(&key.as_str()))
        {
            return Err(HostError::EnvironmentVariableDenied);
        }
        Ok(Self {
            executable,
            arguments: vec![
                "--transport".into(),
                "jsonl-stdio".into(),
                "--api-version".into(),
                api_version.into(),
                "--app-data-root".into(),
                app_data_root.display().to_string(),
                "--engine-executable".into(),
                engine.path.display().to_string(),
                "--engine-sha256".into(),
                engine.sha256.clone(),
                "--engine-manifest".into(),
                engine_manifest.path.display().to_string(),
                "--engine-manifest-file-sha256".into(),
                engine_manifest.sha256.clone(),
                "--guided-projection".into(),
                guided_projection.path.display().to_string(),
                "--guided-projection-sha256".into(),
                guided_projection.sha256.clone(),
                "--guided-manifest".into(),
                guided_manifest.path.display().to_string(),
                "--guided-manifest-sha256".into(),
                guided_manifest.sha256.clone(),
                "--guided-evidence-archive".into(),
                guided_evidence_archive.path.display().to_string(),
                "--guided-evidence-archive-sha256".into(),
                guided_evidence_archive.sha256.clone(),
            ],
            environment,
        })
    }
}

/// Keeps a credential in owned memory and zeroes it on drop. The renderer only
/// passes a keychain handle; raw credentials are injected into worker memory by
/// the native application boundary.
#[derive(Debug)]
pub struct SecretMaterial(Zeroizing<Vec<u8>>);

impl SecretMaterial {
    pub fn new(value: &[u8]) -> Result<Self, HostError> {
        if value.len() < 20 || value.len() > 4096 {
            return Err(HostError::InvalidCredential);
        }
        Ok(Self(Zeroizing::new(value.to_vec())))
    }

    pub fn expose_to_worker_initialization(&self) -> &[u8] {
        self.0.as_slice()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostError {
    EnvironmentVariableDenied,
    HashMismatch,
    InvalidCredential,
    InvalidManifest,
    InvalidProfile,
    Io,
    PathOutsideRoot,
    ResourceNotRegularFile,
    RpcMethodDenied,
    RpcTooLarge,
    SignatureInvalid,
    SizeMismatch,
}

impl Display for HostError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::EnvironmentVariableDenied => "environment variable is not allowlisted",
            Self::HashMismatch => "packaged resource hash does not match the release manifest",
            Self::InvalidCredential => "credential has an invalid shape",
            Self::InvalidManifest => "release manifest is invalid",
            Self::InvalidProfile => "application host profile is invalid",
            Self::Io => "resource verification failed",
            Self::PathOutsideRoot => "path escapes its authorized root",
            Self::ResourceNotRegularFile => "resource is not a regular non-symlink file",
            Self::RpcMethodDenied => "RPC method is not allowed by this application",
            Self::RpcTooLarge => "RPC request exceeds the configured byte limit",
            Self::SignatureInvalid => "release manifest signature is absent or invalid",
            Self::SizeMismatch => "packaged resource size does not match the release manifest",
        })
    }
}

impl std::error::Error for HostError {}

pub fn verify_release_manifest(
    verification: ReleaseManifestVerification<'_>,
) -> Result<Vec<VerifiedResource>, HostError> {
    let ReleaseManifestVerification {
        manifest_path,
        signature_path,
        resource_root,
        expected_application_id,
        expected_application_version,
        expected_source_commit_sha,
        expected_target,
        public_key_hex,
    } = verification;
    if !resource_root.is_absolute() {
        return Err(HostError::PathOutsideRoot);
    }
    let canonical_root = resource_root.canonicalize().map_err(|_| HostError::Io)?;
    let metadata = fs::symlink_metadata(manifest_path).map_err(|_| HostError::Io)?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(HostError::ResourceNotRegularFile);
    }
    if metadata.len() == 0 || metadata.len() > MAX_RELEASE_MANIFEST_BYTES {
        return Err(HostError::InvalidManifest);
    }
    let canonical_manifest = manifest_path.canonicalize().map_err(|_| HostError::Io)?;
    if !canonical_manifest.starts_with(&canonical_root) {
        return Err(HostError::PathOutsideRoot);
    }
    let bytes = fs::read(&canonical_manifest).map_err(|_| HostError::Io)?;
    verify_manifest_signature(&bytes, signature_path, resource_root, public_key_hex)?;
    let manifest: ReleaseManifest =
        serde_json::from_slice(&bytes).map_err(|_| HostError::InvalidManifest)?;
    if manifest.schema_version != "1.1"
        || manifest.application_id != expected_application_id
        || manifest.application_version != expected_application_version
        || manifest.source_commit_sha != expected_source_commit_sha
        || manifest.target != expected_target
        || !is_semver(&manifest.application_version)
        || !is_source_commit(&manifest.source_commit_sha)
        || !is_release_target(&manifest.target)
        || manifest.resources.is_empty()
        || manifest.resources.len() > MAX_RELEASE_RESOURCES
    {
        return Err(HostError::InvalidManifest);
    }

    let mut seen = BTreeSet::new();
    let mut verified = Vec::with_capacity(manifest.resources.len());
    for resource in manifest.resources {
        validate_relative_path(Path::new(&resource.path))?;
        if !seen.insert(resource.path.clone()) || !is_sha256(&resource.sha256) {
            return Err(HostError::InvalidManifest);
        }
        let candidate = canonical_root.join(&resource.path);
        let metadata = fs::symlink_metadata(&candidate).map_err(|_| HostError::Io)?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err(HostError::ResourceNotRegularFile);
        }
        let canonical = candidate.canonicalize().map_err(|_| HostError::Io)?;
        if !canonical.starts_with(&canonical_root) {
            return Err(HostError::PathOutsideRoot);
        }
        if metadata.len() != resource.bytes {
            return Err(HostError::SizeMismatch);
        }
        let actual = hash_file(&canonical)?;
        if actual != resource.sha256 {
            return Err(HostError::HashMismatch);
        }
        verified.push(VerifiedResource {
            path: canonical,
            sha256: actual,
            executable: resource.executable,
        });
    }
    let canonical_signature = signature_path.canonicalize().map_err(|_| HostError::Io)?;
    let mut expected_paths = seen;
    expected_paths.insert(relative_resource_path(
        &canonical_root,
        &canonical_manifest,
    )?);
    expected_paths.insert(relative_resource_path(
        &canonical_root,
        &canonical_signature,
    )?);
    if collect_resource_paths(&canonical_root)? != expected_paths {
        return Err(HostError::InvalidManifest);
    }
    Ok(verified)
}

fn relative_resource_path(root: &Path, path: &Path) -> Result<String, HostError> {
    let relative = path
        .strip_prefix(root)
        .map_err(|_| HostError::PathOutsideRoot)?;
    validate_relative_path(relative)?;
    Ok(relative
        .components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/"))
}

fn collect_resource_paths(root: &Path) -> Result<BTreeSet<String>, HostError> {
    fn visit(root: &Path, directory: &Path, paths: &mut BTreeSet<String>) -> Result<(), HostError> {
        for entry in fs::read_dir(directory).map_err(|_| HostError::Io)? {
            let entry = entry.map_err(|_| HostError::Io)?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).map_err(|_| HostError::Io)?;
            if metadata.file_type().is_symlink() {
                return Err(HostError::ResourceNotRegularFile);
            }
            if metadata.file_type().is_dir() {
                visit(root, &path, paths)?;
            } else if metadata.file_type().is_file() {
                paths.insert(relative_resource_path(root, &path)?);
            } else {
                return Err(HostError::ResourceNotRegularFile);
            }
        }
        Ok(())
    }

    let mut paths = BTreeSet::new();
    visit(root, root, &mut paths)?;
    Ok(paths)
}

fn verify_manifest_signature(
    manifest_bytes: &[u8],
    signature_path: &Path,
    resource_root: &Path,
    public_key_hex: &str,
) -> Result<(), HostError> {
    if public_key_hex.len() != 64 {
        return Err(HostError::SignatureInvalid);
    }
    let root = resource_root
        .canonicalize()
        .map_err(|_| HostError::SignatureInvalid)?;
    let metadata = fs::symlink_metadata(signature_path).map_err(|_| HostError::SignatureInvalid)?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(HostError::SignatureInvalid);
    }
    if metadata.len() != 64 {
        return Err(HostError::SignatureInvalid);
    }
    let canonical_signature = signature_path
        .canonicalize()
        .map_err(|_| HostError::SignatureInvalid)?;
    if !canonical_signature.starts_with(&root) {
        return Err(HostError::SignatureInvalid);
    }
    let public_key_bytes = hex::decode(public_key_hex).map_err(|_| HostError::SignatureInvalid)?;
    let public_key_array: [u8; 32] = public_key_bytes
        .try_into()
        .map_err(|_| HostError::SignatureInvalid)?;
    let verifying_key =
        VerifyingKey::from_bytes(&public_key_array).map_err(|_| HostError::SignatureInvalid)?;
    let signature_bytes = fs::read(canonical_signature).map_err(|_| HostError::SignatureInvalid)?;
    let signature =
        Signature::from_slice(&signature_bytes).map_err(|_| HostError::SignatureInvalid)?;
    verifying_key
        .verify(manifest_bytes, &signature)
        .map_err(|_| HostError::SignatureInvalid)
}

pub fn resolve_authorized_project_path(root: &Path, relative: &Path) -> Result<PathBuf, HostError> {
    if !root.is_absolute() {
        return Err(HostError::PathOutsideRoot);
    }
    validate_relative_path(relative)?;
    let canonical_root = root.canonicalize().map_err(|_| HostError::Io)?;
    let candidate = canonical_root.join(relative);
    let canonical_parent = candidate
        .parent()
        .ok_or(HostError::PathOutsideRoot)?
        .canonicalize()
        .map_err(|_| HostError::Io)?;
    if !canonical_parent.starts_with(&canonical_root) {
        return Err(HostError::PathOutsideRoot);
    }
    Ok(candidate)
}

fn validate_relative_path(path: &Path) -> Result<(), HostError> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(HostError::PathOutsideRoot);
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_source_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_semver(value: &str) -> bool {
    let parts = value.split('.').collect::<Vec<_>>();
    parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (*part == "0" || !part.starts_with('0'))
        })
}

fn is_release_target(value: &str) -> bool {
    matches!(
        value,
        "linux-x64" | "macos-arm64" | "macos-x64" | "windows-x64"
    )
}

fn hash_file(path: &Path) -> Result<String, HostError> {
    let mut digest = Sha256::new();
    let mut source = fs::File::open(path).map_err(|_| HostError::Io)?;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = source.read(&mut buffer).map_err(|_| HostError::Io)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(hex::encode(digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    #[test]
    fn profile_denies_wildcards_and_oversized_rpc() {
        let profile = AppProfile {
            application_id: "org.ecocorridor.scenariostudio".into(),
            application_version: "0.1.0".into(),
            api_version: "1.0".into(),
            worker_resource: "bin/ecocorridor-worker".into(),
            engine_resource: "engine/bin/ecocorridor-engine".into(),
            engine_manifest_resource: "engine-manifest.json".into(),
            guided_projection_resource: "guided/ridgeway-ui-projection.json".into(),
            guided_manifest_resource: "guided/direct-evidence-manifest.json".into(),
            release_public_key_hex: String::new(),
            release_source_commit_sha: String::new(),
            release_target: "linux-x64".into(),
            keychain_service: "org.ecocorridor.scenariostudio.credentials".into(),
            app_data_directory: "EcoCorridorScenarioStudio".into(),
            allowed_rpc_methods: BTreeSet::from(["connectivity.guided.open".into()]),
            maximum_rpc_bytes: 1024,
        };
        assert_eq!(profile.validate(), Ok(()));
        assert_eq!(
            profile.authorize_rpc("connectivity.guided.open", 1025),
            Err(HostError::RpcTooLarge)
        );
        assert_eq!(
            profile.authorize_rpc("connectivity.model.delete", 10),
            Err(HostError::RpcMethodDenied)
        );

        let mut signed = profile.clone();
        signed.release_public_key_hex = "a".repeat(64);
        assert_eq!(signed.validate(), Err(HostError::InvalidProfile));
        signed.release_source_commit_sha = "b".repeat(40);
        assert_eq!(signed.validate(), Ok(()));
        signed.release_target = "gi-desktop".into();
        assert_eq!(signed.validate(), Err(HostError::InvalidProfile));
    }

    #[test]
    fn release_manifest_verifies_hash_size_and_containment() {
        let directory = tempfile::tempdir().unwrap();
        let resources = directory.path().join("resources");
        fs::create_dir(&resources).unwrap();
        let worker = resources.join("worker");
        fs::write(&worker, b"verified worker").unwrap();
        let digest = hash_file(&worker).unwrap();
        let manifest = ReleaseManifest {
            schema_version: "1.1".into(),
            application_id: "org.ecocorridor.scenariostudio".into(),
            application_version: "0.1.0".into(),
            source_commit_sha: "a".repeat(40),
            target: "linux-x64".into(),
            resources: vec![ResourceEntry {
                path: "worker".into(),
                sha256: digest.clone(),
                bytes: 15,
                executable: true,
            }],
        };
        let manifest_path = resources.join("release-manifest.json");
        let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
        fs::write(&manifest_path, &manifest_bytes).unwrap();
        let signing_key = SigningKey::from_bytes(&[7_u8; 32]);
        let signature_path = resources.join("release-manifest.sig");
        fs::write(
            &signature_path,
            signing_key.sign(&manifest_bytes).to_bytes(),
        )
        .unwrap();
        let public_key = hex::encode(signing_key.verifying_key().to_bytes());
        let expected_source_commit_sha = "a".repeat(40);
        let verification = ReleaseManifestVerification {
            manifest_path: &manifest_path,
            signature_path: &signature_path,
            resource_root: &resources,
            expected_application_id: "org.ecocorridor.scenariostudio",
            expected_application_version: "0.1.0",
            expected_source_commit_sha: &expected_source_commit_sha,
            expected_target: "linux-x64",
            public_key_hex: &public_key,
        };
        let verified = verify_release_manifest(verification).unwrap();
        assert_eq!(verified[0].sha256, digest);

        let unexpected_source_commit_sha = "b".repeat(40);
        assert_eq!(
            verify_release_manifest(ReleaseManifestVerification {
                expected_source_commit_sha: &unexpected_source_commit_sha,
                ..verification
            }),
            Err(HostError::InvalidManifest)
        );

        let unlisted = resources.join("unlisted-resource");
        fs::write(&unlisted, b"not signed").unwrap();
        assert_eq!(
            verify_release_manifest(verification),
            Err(HostError::InvalidManifest)
        );
        fs::remove_file(unlisted).unwrap();

        fs::write(&manifest_path, b"tampered").unwrap();
        assert_eq!(
            verify_release_manifest(verification),
            Err(HostError::SignatureInvalid)
        );
    }

    #[test]
    fn relative_path_rejects_parent_components() {
        assert_eq!(
            validate_relative_path(Path::new("../gi-scenario-worker")),
            Err(HostError::PathOutsideRoot)
        );
    }

    #[test]
    fn worker_command_pins_every_scientific_and_guided_resource() {
        let directory = tempfile::tempdir().unwrap();
        let app_data = directory.path().join("app-data");
        fs::create_dir(&app_data).unwrap();
        let resource = |name: &str, executable: bool| {
            let path = directory.path().join(name);
            fs::write(&path, name.as_bytes()).unwrap();
            VerifiedResource {
                sha256: hash_file(&path).unwrap(),
                path,
                executable,
            }
        };
        let worker = resource("worker", true);
        let engine = resource("engine", true);
        let engine_manifest = resource("engine-manifest", false);
        let projection = resource("projection", false);
        let guided_manifest = resource("guided-manifest", false);
        let evidence = resource("evidence.tar.gz", false);
        let command = WorkerCommand::new(
            worker.path,
            WorkerResources {
                engine: &engine,
                engine_manifest: &engine_manifest,
                guided_projection: &projection,
                guided_manifest: &guided_manifest,
                guided_evidence_archive: &evidence,
            },
            "1.0",
            &app_data,
            BTreeMap::new(),
        )
        .unwrap();
        for (flag, expected) in [
            ("--guided-projection-sha256", projection.sha256),
            ("--guided-manifest-sha256", guided_manifest.sha256),
            ("--guided-evidence-archive-sha256", evidence.sha256),
        ] {
            let index = command
                .arguments
                .iter()
                .position(|argument| argument == flag)
                .unwrap();
            assert_eq!(command.arguments[index + 1], expected);
        }
    }
}
