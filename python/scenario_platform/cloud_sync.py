"""Durable, application-scoped W&B evidence projection.

The local scenario repository is authoritative.  This module consumes already
committed outbox rows and mirrors them to W&B; it never participates in a
scientific transaction or decides which scenario is evaluated next.

The SDK boundary is deliberately lazy and injectable.  Unit tests do not need
network access or credentials, while packaged workers can use the real W&B SDK
after the native host has supplied a process-memory credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from .contracts import ContractModel, Field, canonical_json, require_sha256
from .observability import AppProfile, ProjectScopeEvidence, ProjectScopeResolver, redact_sensitive

_REFERENCE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,2}:v(?:0|[1-9][0-9]*)\Z"
)
_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}\Z")
_ENVIRONMENT_LOCK = threading.RLock()


class InMemoryCredentialSession:
    """Keep a provider credential only in worker memory.

    The native host supplies a candidate credential over the private worker
    pipe.  Failed validation restores the prior session, while successful
    validation replaces and zeroes it.  SDK calls see the key only inside the
    scoped environment context; it is never part of repository or telemetry
    state.
    """

    def __init__(self, *, environment_key: str, minimum_length: int = 20) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", environment_key):
            raise ValueError("credential environment key is invalid")
        if minimum_length < 1 or minimum_length > 512:
            raise ValueError("credential minimum length is invalid")
        self.environment_key = environment_key
        self.minimum_length = minimum_length
        self._secret: bytearray | None = None
        self._lock = threading.RLock()

    def _validated(self, value: str) -> bytearray:
        if not self.minimum_length <= len(value) <= 512 or any(
            character.isspace() for character in value
        ):
            raise ValueError("provider credential has an invalid shape")
        return bytearray(value.encode("utf-8"))

    @property
    def configured(self) -> bool:
        with self._lock:
            return self._secret is not None

    @contextmanager
    def candidate(self, value: str) -> Iterator[None]:
        """Temporarily install ``value`` and commit it only on success."""

        candidate = self._validated(value)
        with self._lock:
            previous = self._secret
            self._secret = candidate
            try:
                yield
            except BaseException:
                candidate[:] = b"\0" * len(candidate)
                self._secret = previous
                raise
            else:
                if previous is not None:
                    previous[:] = b"\0" * len(previous)

    @contextmanager
    def environment(self) -> Iterator[None]:
        """Expose the configured key to an SDK for the shortest useful scope."""

        with self._lock, _ENVIRONMENT_LOCK:
            if self._secret is None:
                raise CloudPolicyError("PROVIDER_CREDENTIAL_UNAVAILABLE")
            previous = os.environ.get(self.environment_key)
            temporary = self._secret.decode("utf-8")
            os.environ[self.environment_key] = temporary
            try:
                yield
            finally:
                if previous is None:
                    os.environ.pop(self.environment_key, None)
                else:
                    os.environ[self.environment_key] = previous
                temporary = ""

    def clear(self) -> None:
        with self._lock:
            if self._secret is not None:
                self._secret[:] = b"\0" * len(self._secret)
                self._secret = None


class CloudPolicyError(RuntimeError):
    """A security or authorization rule blocks synchronization."""


class CloudIntegrityError(RuntimeError):
    """Local or downloaded evidence differs from its committed digest."""


class CloudTransportError(RuntimeError):
    """A retryable provider or network operation failed."""


class WandbTarget(ContractModel):
    entity: str
    project: str
    external_project_id: str
    deployment: str
    api_base_url: str
    web_base_url: str
    scope_evidence: ProjectScopeEvidence

    def _validate_contract(self) -> None:
        if not self.entity or not self.project or not self.external_project_id:
            raise ValueError("W&B target requires entity, project, and immutable project ID")
        for label, value in (
            ("API base URL", self.api_base_url),
            ("web base URL", self.web_base_url),
        ):
            parsed = urlsplit(value.rstrip("/"))
            is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.scheme != "https" and not is_loopback)
            ):
                raise ValueError(f"{label} must be HTTPS or loopback HTTP without credentials")
        if (
            self.scope_evidence.entity != self.entity
            or self.scope_evidence.project != self.project
            or self.scope_evidence.external_project_id != self.external_project_id
            or self.scope_evidence.deployment.casefold().replace("-", "_")
            != self.deployment.casefold().replace("-", "_")
            or self.scope_evidence.api_base_url != self.api_base_url.rstrip("/")
        ):
            raise ValueError("W&B target identity differs from its exact-scope evidence")


class TableProjection(ContractModel):
    logical_name: str
    columns: list[str] = Field(min_length=1)
    rows: list[list[Any]] = Field(default_factory=list)

    def _validate_contract(self) -> None:
        if len(set(self.columns)) != len(self.columns) or any(
            not column.strip() for column in self.columns
        ):
            raise ValueError("W&B Table columns must be unique and non-empty")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("every W&B Table row must match the declared columns")
        canonical_json(self.rows)


class ArtifactProjection(ContractModel):
    local_artifact_id: str
    artifact_type: str
    logical_id: str
    logical_name: str
    source_relative_path: str
    sha256: str
    bytes: int = Field(ge=0)
    media_type: str
    registry_alias: str | None = None

    def _validate_contract(self) -> None:
        if not self.local_artifact_id or not self.logical_id or not self.media_type:
            raise ValueError("cloud artifact identity and media type are required")
        require_sha256(self.sha256, label="cloud artifact sha256")
        source = PurePosixPath(self.source_relative_path)
        logical = PurePosixPath(self.logical_name)
        for label, path in (("source", source), ("logical", logical)):
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError(f"cloud artifact {label} path must be contained and relative")
        if not _LOGICAL_NAME.fullmatch(self.logical_name):
            raise ValueError("cloud artifact logical name contains unsupported characters")


class TelemetryProjection(ContractModel):
    profile_namespace: str
    project_id: str
    run_kind: str
    run_subject_id: str
    event_type: str
    event_sequence: int = Field(ge=1)
    config: dict[str, Any] = Field(default_factory=dict)
    history: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    tables: list[TableProjection] = Field(default_factory=list)
    input_artifact_refs: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactProjection] = Field(default_factory=list)

    def _validate_contract(self) -> None:
        if not self.project_id or not self.run_subject_id or not self.event_type:
            raise ValueError("telemetry project, run subject, and event type are required")
        if len({table.logical_name for table in self.tables}) != len(self.tables):
            raise ValueError("telemetry table logical names must be unique")
        if len(set(self.input_artifact_refs)) != len(self.input_artifact_refs):
            raise ValueError("input artifact references must be deduplicated before enqueue")
        for reference in self.input_artifact_refs:
            if not _REFERENCE.fullmatch(reference):
                raise ValueError("input artifact references must be qualified and pinned to :vN")
        canonical_json(self.config)
        canonical_json(self.history)
        canonical_json(self.summary)


class CloudArtifactReference(ContractModel):
    local_artifact_id: str
    provider_artifact_id: str
    provider_artifact_ref: str
    provider_artifact_url: str | None = None
    sha256: str
    registry_target: str | None = None

    def _validate_contract(self) -> None:
        require_sha256(self.sha256, label="uploaded artifact sha256")
        if not self.provider_artifact_id or not _REFERENCE.fullmatch(self.provider_artifact_ref):
            raise ValueError("provider artifact must have an immutable ID and pinned version")
        if self.provider_artifact_url is not None:
            _require_safe_provider_url(self.provider_artifact_url, label="artifact URL")


class CloudUploadReceipt(ContractModel):
    run_id: str
    run_url: str
    envelope_id: str
    artifacts: list[CloudArtifactReference] = Field(default_factory=list)

    def _validate_contract(self) -> None:
        if not self.run_id or not self.run_url or not self.envelope_id:
            raise ValueError("cloud upload receipt requires run, URL, and envelope identity")
        _require_safe_provider_url(self.run_url, label="run URL")


def _require_safe_provider_url(value: str, *, label: str) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not loopback)
    ):
        raise ValueError(f"provider {label} must be credential-free HTTPS or loopback HTTP")
    return value


def _effective_origin(value: str) -> tuple[str, str, int]:
    """Return a normalized origin for a previously validated provider URL."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("provider URL has no origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider URL has an invalid port") from exc
    if port is None:
        port = 443 if parsed.scheme.casefold() == "https" else 80
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def _require_target_provider_url(value: str, *, target: WandbTarget, label: str) -> str:
    """Bind SDK-returned evidence links to the verified W&B web endpoint."""

    _require_safe_provider_url(value, label=label)
    if _effective_origin(value) != _effective_origin(target.web_base_url):
        raise CloudPolicyError("WANDB_PROVIDER_URL_OUTSIDE_VERIFIED_TARGET")
    return value


@dataclass(frozen=True, slots=True)
class ProjectCloudDirectories:
    """W&B directories isolated beneath one local scenario project."""

    root: Path
    run: Path
    cache: Path
    artifacts: Path
    offline: Path
    verification: Path

    @classmethod
    def create(cls, project_root: str | Path, profile: AppProfile) -> ProjectCloudDirectories:
        project = Path(project_root).resolve()
        if not project.is_dir():
            raise ValueError("project-local cloud directories require an existing project root")
        cloud_root = project / ".scenario-cloud"
        root = cloud_root / profile.namespace
        directories = cls(
            root=root,
            run=root / "run",
            cache=root / "cache",
            artifacts=root / "artifacts",
            offline=root / "offline",
            verification=root / "verification",
        )
        for directory in (
            cloud_root,
            directories.root,
            directories.run,
            directories.cache,
            directories.artifacts,
            directories.offline,
            directories.verification,
        ):
            if directory.exists() and directory.is_symlink():
                raise ValueError("cloud SDK directory cannot be a symlink")
            parent = directory.parent.resolve()
            if parent != project and project not in parent.parents:
                raise ValueError("cloud SDK directory escaped the local project root")
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or project not in directory.resolve().parents:
                raise ValueError("cloud SDK directory escaped the local project root")
        return directories

    @contextmanager
    def wandb_environment(self, *, base_url: str, online: bool) -> Iterator[None]:
        values = {
            "WANDB_BASE_URL": base_url.rstrip("/"),
            "WANDB_CONSOLE": "off",
            "WANDB_DISABLE_CODE": "true",
            "WANDB_DISABLE_GIT": "true",
            "WANDB_ERROR_REPORTING": "false",
            "WANDB_SHOW_RUN": "false",
            "WANDB_SILENT": "true",
            "WANDB_DIR": str(self.run),
            "WANDB_CACHE_DIR": str(self.cache),
            "WANDB_ARTIFACT_DIR": str(self.artifacts),
            "WANDB_DATA_DIR": str(self.offline),
            "WANDB_MODE": "online" if online else "offline",
        }
        with _ENVIRONMENT_LOCK:
            previous = {key: os.environ.get(key) for key in values}
            os.environ.update(values)
            try:
                yield
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class OutboxRepository(Protocol):
    def claim_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def mark_outbox_synced(
        self,
        outbox_id: str,
        external_reference: dict[str, Any],
        *,
        lease_token: str | None = None,
    ) -> None: ...

    def mark_outbox_failed(
        self,
        outbox_id: str,
        error_code: str,
        *,
        blocked: bool = False,
        lease_token: str | None = None,
    ) -> None: ...


class LineageUploader(Protocol):
    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence: ...

    def upload(
        self,
        *,
        envelope_id: str,
        idempotency_key: str,
        projection: TelemetryProjection,
        target: WandbTarget,
        project_root: Path,
        directories: ProjectCloudDirectories,
    ) -> CloudUploadReceipt: ...


class TelemetryProjector(Protocol):
    """Convert a domain producer payload into the cloud contract."""

    def __call__(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection: ...


class OutboxDrainReport(ContractModel):
    online: bool
    claimed: int = Field(ge=0)
    synced: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    queued_offline: bool


class DurableOutboxConsumer:
    """Project-local retry loop that cannot mutate scientific state."""

    def __init__(
        self,
        *,
        repository: OutboxRepository,
        uploader: LineageUploader,
        profile: AppProfile,
        target: WandbTarget,
        project_root: str | Path,
        projector: TelemetryProjector | None = None,
    ) -> None:
        self.repository = repository
        self.uploader = uploader
        self.profile = profile
        self.target = target
        self.project_root = Path(project_root).resolve()
        self.directories = ProjectCloudDirectories.create(self.project_root, profile)
        self.projector = projector

    def drain(self, *, online: bool, limit: int = 32) -> OutboxDrainReport:
        if limit < 1:
            raise ValueError("outbox drain limit must be positive")
        if not online:
            return OutboxDrainReport(
                online=False,
                claimed=0,
                synced=0,
                failed=0,
                blocked=0,
                queued_offline=True,
            )
        # Scope is checked before rows become process-owned.  A policy failure
        # therefore leaves every item pending and cannot strand an upload.
        self.uploader.verify_target(self.target)
        rows = self.repository.claim_outbox(limit=limit)
        synced = failed = blocked = 0
        halted_streams: set[str] = set()
        for row in rows:
            outbox_id = str(row["outbox_id"])
            lease_token = str(row["lease_token"]) if row.get("lease_token") else None
            stream_id = str(row.get("stream_id") or f"outbox:{outbox_id}")
            if stream_id in halted_streams:
                release = getattr(self.repository, "release_outbox", None)
                if not callable(release):
                    raise RuntimeError("ordered outbox repository cannot release a later lease")
                release(outbox_id, lease_token=lease_token)
                continue
            try:
                projection = self._projection(row)
                receipt = self.uploader.upload(
                    envelope_id=outbox_id,
                    idempotency_key=str(row["idempotency_key"]),
                    projection=projection,
                    target=self.target,
                    project_root=self.project_root,
                    directories=self.directories,
                )
            except (CloudPolicyError, CloudIntegrityError, ValueError, KeyError) as exc:
                self.repository.mark_outbox_failed(
                    outbox_id,
                    _safe_error_code(exc, fallback="CLOUD_PROJECTION_BLOCKED"),
                    blocked=True,
                    lease_token=lease_token,
                )
                blocked += 1
                halted_streams.add(stream_id)
            except Exception as exc:  # noqa: BLE001 - provider errors are retryable state
                self.repository.mark_outbox_failed(
                    outbox_id,
                    _safe_error_code(exc, fallback="CLOUD_PROVIDER_UNAVAILABLE"),
                    lease_token=lease_token,
                )
                failed += 1
                halted_streams.add(stream_id)
            else:
                self.repository.mark_outbox_synced(
                    outbox_id,
                    receipt.model_dump(mode="json"),
                    lease_token=lease_token,
                )
                synced += 1
        return OutboxDrainReport(
            online=True,
            claimed=len(rows),
            synced=synced,
            failed=failed,
            blocked=blocked,
            queued_offline=False,
        )

    def _projection(self, row: dict[str, Any]) -> TelemetryProjection:
        raw = str(row["payload_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise CloudIntegrityError("CLOUD_OUTBOX_PAYLOAD_HASH_MISMATCH")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CloudIntegrityError("CLOUD_OUTBOX_PAYLOAD_INVALID") from exc
        if redact_sensitive(payload) != payload:
            raise CloudPolicyError("CLOUD_PAYLOAD_CONTAINS_SENSITIVE_DATA")
        projection = (
            self.projector(row, payload)
            if self.projector is not None
            else TelemetryProjection.model_validate(payload)
        )
        projection = TelemetryProjection.model_validate(projection)
        if projection.profile_namespace != self.profile.namespace:
            raise CloudPolicyError("CLOUD_PROFILE_NAMESPACE_MISMATCH")
        if projection.project_id != row["project_id"]:
            raise CloudPolicyError("CLOUD_PROJECT_IDENTITY_MISMATCH")
        if projection.event_type != row["event_type"]:
            raise CloudIntegrityError("CLOUD_OUTBOX_EVENT_MISMATCH")
        if projection.run_kind not in self.profile.run_kinds:
            raise CloudPolicyError("CLOUD_RUN_KIND_UNSUPPORTED")
        for artifact in projection.artifacts:
            if artifact.artifact_type not in self.profile.artifact_types:
                raise CloudPolicyError("CLOUD_ARTIFACT_TYPE_UNSUPPORTED")
            if artifact.registry_alias is not None:
                self.profile.registry_name(artifact.registry_alias)
        return projection


class WandbSdkLineageUploader:
    """Real W&B Runs/Tables/Artifacts adapter, loaded only when used."""

    def __init__(
        self,
        *,
        profile: AppProfile,
        scope_resolver: ProjectScopeResolver,
        sdk: Any | None = None,
    ) -> None:
        self.profile = profile
        self.scope_resolver = scope_resolver
        self._injected_sdk = sdk

    def _sdk(self) -> Any:
        if self._injected_sdk is not None:
            return self._injected_sdk
        if not os.environ.get("WANDB_API_KEY"):
            raise CloudTransportError("WANDB_CREDENTIAL_UNAVAILABLE")
        try:
            import wandb  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise CloudTransportError("WANDB_SDK_UNAVAILABLE") from exc
        return wandb

    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence:
        try:
            evidence = self.scope_resolver.resolve(
                entity=target.entity,
                project=target.project,
                deployment=target.deployment,
                api_base_url=target.api_base_url,
            )
        except Exception as exc:
            raise CloudPolicyError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE") from exc
        if evidence.external_project_id != target.external_project_id or evidence.model_dump(
            mode="json"
        ) != target.scope_evidence.model_dump(mode="json"):
            raise CloudPolicyError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")
        return evidence

    def upload(
        self,
        *,
        envelope_id: str,
        idempotency_key: str,
        projection: TelemetryProjection,
        target: WandbTarget,
        project_root: Path,
        directories: ProjectCloudDirectories,
    ) -> CloudUploadReceipt:
        self.verify_target(target)
        projection_payload = projection.model_dump(mode="json")
        if redact_sensitive(projection_payload) != projection_payload:
            raise CloudPolicyError("WANDB_PROJECTION_REDACTION_REQUIRED")
        sources = {
            artifact.local_artifact_id: _verified_source(project_root, artifact)
            for artifact in projection.artifacts
        }
        sdk = self._sdk()
        safe_config = redact_sensitive(projection.config)
        if safe_config != projection.config:
            raise CloudPolicyError("WANDB_CONFIG_REDACTION_REQUIRED")
        run_id = self.profile.run_id(projection.run_kind, projection.run_subject_id)
        with directories.wandb_environment(base_url=target.api_base_url, online=True):
            try:
                public_api = sdk.Api(overrides={"base_url": target.api_base_url.rstrip("/")})
                settings = sdk.Settings(
                    console="off", disable_code=True, disable_git=True, silent=True
                )
                run = sdk.init(
                    entity=target.entity,
                    project=target.project,
                    id=run_id,
                    resume="allow",
                    job_type=projection.run_kind,
                    mode="online",
                    force=True,
                    reinit=True,
                    dir=str(directories.run),
                    config={
                        "app_namespace": self.profile.namespace,
                        "api_version": self.profile.api_version,
                        "schema_version": self.profile.schema_version,
                        "local_authority": True,
                        "run_subject_id": projection.run_subject_id,
                        "config": safe_config,
                    },
                    settings=settings,
                )
            except Exception as exc:
                raise CloudTransportError("WANDB_RUN_INITIALIZATION_FAILED") from exc
            if run is None:
                raise CloudTransportError("WANDB_RUN_INITIALIZATION_FAILED")
            references: list[CloudArtifactReference] = []
            try:
                logged_values = dict(projection.history)
                for table in projection.tables:
                    logged_values[self.profile.table_name(table.logical_name)] = sdk.Table(
                        columns=table.columns,
                        data=table.rows,
                    )
                if logged_values:
                    run.log(logged_values, step=projection.event_sequence)
                for key, value in projection.summary.items():
                    run.summary[key] = value
                run.summary["local_authority"] = True
                run.summary["last_outbox_envelope_id"] = envelope_id
                run.summary["last_idempotency_key_sha256"] = hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest()
                for reference in projection.input_artifact_refs:
                    run.use_artifact(reference)
                for manifest in projection.artifacts:
                    source = sources[manifest.local_artifact_id]
                    artifact_name = self.profile.artifact_name(
                        manifest.artifact_type, manifest.logical_id
                    )
                    artifact_type = f"{self.profile.namespace}-{manifest.artifact_type}"
                    alias = _artifact_idempotency_alias(
                        profile=self.profile,
                        envelope_id=envelope_id,
                        idempotency_key=idempotency_key,
                        manifest=manifest,
                    )
                    metadata = _artifact_idempotency_metadata(
                        profile=self.profile,
                        envelope_id=envelope_id,
                        idempotency_key=idempotency_key,
                        manifest=manifest,
                        alias=alias,
                    )
                    logged = _lookup_committed_artifact(
                        public_api=public_api,
                        target=target,
                        artifact_name=artifact_name,
                        artifact_type=artifact_type,
                        alias=alias,
                    )
                    if logged is None:
                        artifact = sdk.Artifact(
                            name=artifact_name,
                            type=artifact_type,
                            metadata=metadata,
                        )
                        artifact.add_file(str(source), name=manifest.logical_name)
                        logged = run.log_artifact(artifact, aliases=[alias])
                        wait = getattr(logged, "wait", None)
                        if not callable(wait):
                            raise CloudTransportError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
                        wait()
                    else:
                        artifact = logged
                    _verify_committed_artifact_identity(
                        logged,
                        target=target,
                        artifact_name=artifact_name,
                        artifact_type=artifact_type,
                        expected_metadata=metadata,
                    )
                    _verify_download(logged, manifest, directories)
                    provider_id = str(getattr(logged, "id", "") or "")
                    provider_ref = _provider_artifact_ref(logged, target, artifact)
                    if not provider_id:
                        raise CloudTransportError("WANDB_ARTIFACT_ID_UNAVAILABLE")
                    registry_target = None
                    if manifest.registry_alias is not None:
                        registry_target = self.profile.registry_name(manifest.registry_alias)
                        link = getattr(run, "link_artifact", None)
                        if not callable(link):
                            raise CloudTransportError("WANDB_REGISTRY_LINK_UNAVAILABLE")
                        link(logged, target_path=registry_target)
                    references.append(
                        CloudArtifactReference(
                            local_artifact_id=manifest.local_artifact_id,
                            provider_artifact_id=provider_id,
                            provider_artifact_ref=provider_ref,
                            provider_artifact_url=(
                                _require_target_provider_url(
                                    str(getattr(logged, "url", "") or ""),
                                    target=target,
                                    label="artifact URL",
                                )
                                if str(getattr(logged, "url", "") or "")
                                else None
                            ),
                            sha256=manifest.sha256,
                            registry_target=registry_target,
                        )
                    )
                run.summary["output_artifact_ids"] = [
                    item.provider_artifact_id for item in references
                ]
                run_url = str(getattr(run, "url", "") or "") or (
                    f"{target.web_base_url.rstrip('/')}/{target.entity}/"
                    f"{target.project}/runs/{run_id}"
                )
                _require_target_provider_url(run_url, target=target, label="run URL")
            except (CloudPolicyError, CloudIntegrityError, CloudTransportError):
                raise
            except Exception as exc:
                raise CloudTransportError("WANDB_UPLOAD_FAILED") from exc
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    run.finish()
                except Exception as exc:
                    if not active_error:
                        raise CloudTransportError("WANDB_RUN_FINISH_FAILED") from exc
        return CloudUploadReceipt(
            run_id=run_id,
            run_url=run_url,
            envelope_id=envelope_id,
            artifacts=references,
        )


def _verified_source(project_root: Path, manifest: ArtifactProjection) -> Path:
    root = project_root.resolve()
    source = (root / PurePosixPath(manifest.source_relative_path)).resolve()
    if root not in source.parents or not source.is_file() or source.is_symlink():
        raise CloudIntegrityError("WANDB_ARTIFACT_SOURCE_MISSING_OR_UNSAFE")
    if source.stat().st_size != manifest.bytes or _sha256_path(source) != manifest.sha256:
        raise CloudIntegrityError("WANDB_ARTIFACT_SOURCE_HASH_MISMATCH")
    return source


def _artifact_idempotency_alias(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    manifest: ArtifactProjection,
) -> str:
    """Return a provider-safe alias unique to one committed output intent."""

    digest = hashlib.sha256(
        canonical_json(
            {
                "profile_namespace": profile.namespace,
                "envelope_id": envelope_id,
                "idempotency_key": idempotency_key,
                "local_artifact_id": manifest.local_artifact_id,
                "sha256": manifest.sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"outbox-{digest[:40]}"


def _artifact_idempotency_metadata(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    manifest: ArtifactProjection,
    alias: str,
) -> dict[str, Any]:
    return {
        "app_namespace": profile.namespace,
        "outbox_envelope_id": envelope_id,
        "outbox_idempotency_key_sha256": hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest(),
        "outbox_artifact_alias": alias,
        "local_artifact_id": manifest.local_artifact_id,
        "logical_name": manifest.logical_name,
        "sha256": manifest.sha256,
        "bytes": manifest.bytes,
        "media_type": manifest.media_type,
    }


def _lookup_committed_artifact(
    *,
    public_api: Any,
    target: WandbTarget,
    artifact_name: str,
    artifact_type: str,
    alias: str,
) -> Any | None:
    """Resolve an exact idempotency alias without treating errors as absence."""

    reference = f"{target.entity}/{target.project}/{artifact_name}:{alias}"
    exists = getattr(public_api, "artifact_exists", None)
    fetch = getattr(public_api, "artifact", None)
    if not callable(exists) or not callable(fetch):
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_UNAVAILABLE")
    try:
        found = exists(reference, type=artifact_type)
    except Exception as exc:
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_AMBIGUOUS") from exc
    if not isinstance(found, bool):
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_AMBIGUOUS")
    if not found:
        return None
    try:
        return fetch(reference, type=artifact_type)
    except Exception as exc:
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_AMBIGUOUS") from exc


def _verify_committed_artifact_identity(
    artifact: Any,
    *,
    target: WandbTarget,
    artifact_name: str,
    artifact_type: str,
    expected_metadata: dict[str, Any],
) -> None:
    """Fail closed if an idempotency alias resolves to different evidence."""

    actual_type = str(getattr(artifact, "type", "") or "")
    if actual_type != artifact_type:
        raise CloudIntegrityError("WANDB_ARTIFACT_IDEMPOTENCY_TYPE_MISMATCH")
    actual_metadata = getattr(artifact, "metadata", None)
    if not isinstance(actual_metadata, dict) or any(
        actual_metadata.get(key) != value for key, value in expected_metadata.items()
    ):
        raise CloudIntegrityError("WANDB_ARTIFACT_IDEMPOTENCY_METADATA_MISMATCH")
    provider_ref = _provider_artifact_ref(artifact, target, artifact)
    expected_prefix = f"{target.entity}/{target.project}/{artifact_name}:v"
    if not provider_ref.startswith(expected_prefix):
        raise CloudIntegrityError("WANDB_ARTIFACT_IDEMPOTENCY_REFERENCE_MISMATCH")


def _verify_download(
    logged: Any,
    manifest: ArtifactProjection,
    directories: ProjectCloudDirectories,
) -> None:
    download = getattr(logged, "download", None)
    if not callable(download):
        raise CloudTransportError("WANDB_ARTIFACT_DOWNLOAD_UNAVAILABLE")
    with tempfile.TemporaryDirectory(
        prefix="wandb-verify-", dir=directories.verification
    ) as temporary:
        downloaded = Path(download(root=temporary, skip_cache=True)).resolve()
        target = (downloaded / PurePosixPath(manifest.logical_name)).resolve()
        if (
            downloaded not in target.parents
            or not target.is_file()
            or target.stat().st_size != manifest.bytes
            or _sha256_path(target) != manifest.sha256
        ):
            raise CloudIntegrityError("WANDB_ARTIFACT_DOWNLOAD_HASH_MISMATCH")


def _provider_artifact_ref(logged: Any, target: WandbTarget, artifact: Any) -> str:
    reference = str(getattr(logged, "qualified_name", "") or "")
    if not reference:
        name = str(getattr(logged, "name", "") or getattr(artifact, "name", "") or "")
        version = str(getattr(logged, "version", "") or "")
        if name and version:
            reference = f"{target.entity}/{target.project}/{name}:{version}"
    if not _REFERENCE.fullmatch(reference):
        raise CloudTransportError("WANDB_ARTIFACT_PINNED_REFERENCE_UNAVAILABLE")
    return reference


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error_code(error: BaseException, *, fallback: str) -> str:
    rendered = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", rendered):
        return rendered
    return fallback


__all__: Sequence[str] = (
    "ArtifactProjection",
    "CloudArtifactReference",
    "CloudIntegrityError",
    "CloudPolicyError",
    "CloudTransportError",
    "CloudUploadReceipt",
    "DurableOutboxConsumer",
    "LineageUploader",
    "OutboxDrainReport",
    "OutboxRepository",
    "ProjectCloudDirectories",
    "TableProjection",
    "TelemetryProjection",
    "TelemetryProjector",
    "WandbSdkLineageUploader",
    "WandbTarget",
)
