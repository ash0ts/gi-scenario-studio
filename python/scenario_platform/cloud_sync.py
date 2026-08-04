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
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from .contracts import ContractModel, Field, canonical_json, require_sha256
from .observability import AppProfile, ProjectScopeEvidence, ProjectScopeResolver, redact_sensitive
from .storage import atomic_write_bytes

_REFERENCE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,2}:v(?:0|[1-9][0-9]*)\Z"
)
_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}\Z")
_ENVIRONMENT_LOCK = threading.RLock()
_ACKNOWLEDGEMENT_SCHEMA = "scenario-platform-wandb-envelope-ack/v1.1"
_ACKNOWLEDGEMENT_KEYS = frozenset({"schema_version", "intent", "receipt"})
_WRITE_INTENT_SCHEMA = "scenario-platform-wandb-write-intent/v1.0"
_WRITE_INTENT_KEYS = frozenset({"schema_version", "intent"})
_ACKNOWLEDGEMENT_INTENT_KEYS = frozenset(
    {
        "profile_namespace",
        "envelope_id",
        "idempotency_key_sha256",
        "projection_sha256",
        "target_sha256",
        "run_id",
    }
)
_READBACK_DELAYS_SECONDS = (0.0, 0.05, 0.15, 0.3, 0.5)
_PROJECTION_ARTIFACT_TYPE = "telemetry-envelope"
_PROJECTION_LOGICAL_NAME = "telemetry-projection.json"


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
    projection_artifact: CloudArtifactReference | None = None

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
    telemetry_projections: Path

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
            telemetry_projections=root / "run" / "telemetry-projections",
        )
        for directory in (
            cloud_root,
            directories.root,
            directories.run,
            directories.cache,
            directories.artifacts,
            directories.offline,
            directories.verification,
            directories.telemetry_projections,
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

    def mark_outbox_batch_synced(
        self,
        members: Sequence[tuple[str, str]],
        external_reference: dict[str, Any],
    ) -> None: ...

    def mark_outbox_batch_failed(
        self,
        members: Sequence[tuple[str, str]],
        error_code: str,
        *,
        blocked: bool = False,
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


@dataclass(frozen=True, slots=True)
class OutboxBatchPolicy:
    """Bounded batching for one explicitly selected producer event.

    Generic consumers remain one-envelope-per-row unless a domain adapter
    opts in.  A batch contains only consecutive rows from one ordered stream;
    byte accounting is based on the immutable output artifacts represented by
    the merged projection.
    """

    event_type: str
    max_rows: int = 128
    max_artifact_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("outbox batch event type is required")
        if self.max_rows < 2 or self.max_rows > 128:
            raise ValueError("outbox batches must contain at most 128 rows")
        if self.max_artifact_bytes < 1 or self.max_artifact_bytes > 512 * 1024 * 1024:
            raise ValueError("outbox batch artifact limit must not exceed 512 MiB")


@dataclass(frozen=True, slots=True)
class _PreparedOutboxEnvelope:
    rows: tuple[dict[str, Any], ...]
    projection: TelemetryProjection
    envelope_id: str
    idempotency_key: str

    @property
    def stream_id(self) -> str:
        first = self.rows[0]
        return str(first.get("stream_id") or f"outbox:{first['outbox_id']}")

    @property
    def members(self) -> tuple[tuple[str, str], ...]:
        members: list[tuple[str, str]] = []
        for row in self.rows:
            token = str(row.get("lease_token") or "")
            if not token:
                raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_LEASE_MISSING")
            members.append((str(row["outbox_id"]), token))
        return tuple(members)


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
        batch_policy: OutboxBatchPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.uploader = uploader
        self.profile = profile
        self.target = target
        self.project_root = Path(project_root).resolve()
        self.directories = ProjectCloudDirectories.create(self.project_root, profile)
        self.projector = projector
        self.batch_policy = batch_policy
        if batch_policy is not None:
            for method_name in (
                "mark_outbox_batch_synced",
                "mark_outbox_batch_failed",
            ):
                if not callable(getattr(repository, method_name, None)):
                    raise TypeError(f"batched outbox repository is missing {method_name}()")

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
        claim_limit = (
            limit if self.batch_policy is None else min(limit * self.batch_policy.max_rows, 4096)
        )
        rows = self.repository.claim_outbox(limit=claim_limit)
        synced = failed = blocked = 0
        halted_streams: set[str] = set()
        index = 0
        while index < len(rows):
            row = rows[index]
            row_stream_id = str(row.get("stream_id") or f"outbox:{row['outbox_id']}")
            if row_stream_id in halted_streams:
                release = getattr(self.repository, "release_outbox", None)
                if not callable(release):
                    raise RuntimeError("ordered outbox repository cannot release a later lease")
                release(
                    str(row["outbox_id"]),
                    lease_token=str(row["lease_token"]) if row.get("lease_token") else None,
                )
                index += 1
                continue
            try:
                prepared, next_index = self._prepare_next_envelope(rows, index)
            except (CloudPolicyError, CloudIntegrityError, ValueError, KeyError) as exc:
                row = rows[index]
                self.repository.mark_outbox_failed(
                    str(row["outbox_id"]),
                    _safe_error_code(exc, fallback="CLOUD_PROJECTION_BLOCKED"),
                    blocked=True,
                    lease_token=str(row["lease_token"]) if row.get("lease_token") else None,
                )
                blocked += 1
                halted_streams.add(str(row.get("stream_id") or f"outbox:{row['outbox_id']}"))
                index += 1
                continue
            except Exception as exc:  # noqa: BLE001 - projection/provider state is retryable
                row = rows[index]
                self.repository.mark_outbox_failed(
                    str(row["outbox_id"]),
                    _safe_error_code(exc, fallback="CLOUD_PROVIDER_UNAVAILABLE"),
                    lease_token=str(row["lease_token"]) if row.get("lease_token") else None,
                )
                failed += 1
                halted_streams.add(str(row.get("stream_id") or f"outbox:{row['outbox_id']}"))
                index += 1
                continue
            index = next_index
            try:
                receipt = self.uploader.upload(
                    envelope_id=prepared.envelope_id,
                    idempotency_key=prepared.idempotency_key,
                    projection=prepared.projection,
                    target=self.target,
                    project_root=self.project_root,
                    directories=self.directories,
                )
            except (CloudPolicyError, CloudIntegrityError, ValueError, KeyError) as exc:
                self._fail_prepared(prepared, exc, blocked=True)
                blocked += len(prepared.rows)
                halted_streams.add(prepared.stream_id)
            except Exception as exc:  # noqa: BLE001 - provider errors are retryable state
                self._fail_prepared(prepared, exc, blocked=False)
                failed += len(prepared.rows)
                halted_streams.add(prepared.stream_id)
            else:
                if len(prepared.rows) == 1 or self.batch_policy is None:
                    row = prepared.rows[0]
                    self.repository.mark_outbox_synced(
                        str(row["outbox_id"]),
                        receipt.model_dump(mode="json"),
                        lease_token=(str(row["lease_token"]) if row.get("lease_token") else None),
                    )
                else:
                    self.repository.mark_outbox_batch_synced(
                        prepared.members,
                        receipt.model_dump(mode="json"),
                    )
                synced += len(prepared.rows)
        return OutboxDrainReport(
            online=True,
            claimed=len(rows),
            synced=synced,
            failed=failed,
            blocked=blocked,
            queued_offline=False,
        )

    def _prepare_next_envelope(
        self,
        rows: Sequence[dict[str, Any]],
        index: int,
    ) -> tuple[_PreparedOutboxEnvelope, int]:
        first = rows[index]
        first_projection = self._projection(first)
        if self.batch_policy is None or first["event_type"] != self.batch_policy.event_type:
            return (
                _PreparedOutboxEnvelope(
                    rows=(first,),
                    projection=first_projection,
                    envelope_id=str(first["outbox_id"]),
                    idempotency_key=str(first["idempotency_key"]),
                ),
                index + 1,
            )

        selected_rows = [first]
        projections = [first_projection]
        artifact_identities, artifact_bytes = _projection_artifact_identities(first_projection)
        # The byte ceiling is a batching boundary, not an authority to discard
        # one already-approved direct job.  Oversized jobs have already passed
        # the domain resource policy and therefore remain a one-row envelope.
        oversized_single = artifact_bytes > self.batch_policy.max_artifact_bytes
        cursor = index + 1
        while (
            not oversized_single
            and cursor < len(rows)
            and len(selected_rows) < self.batch_policy.max_rows
        ):
            candidate = rows[cursor]
            if not _rows_are_batch_consecutive(selected_rows[-1], candidate, self.batch_policy):
                break
            try:
                candidate_projection = self._projection(candidate)
            except Exception:  # noqa: BLE001 - surface after the valid prefix is durable
                break
            candidate_identities, _ = _projection_artifact_identities(candidate_projection)
            combined = dict(artifact_identities)
            for local_id, identity in candidate_identities.items():
                existing = combined.get(local_id)
                if existing is not None and existing != identity:
                    raise CloudIntegrityError("CLOUD_BATCH_ARTIFACT_IDENTITY_CONFLICT")
                combined[local_id] = identity
            combined_bytes = sum(int(value["bytes"]) for value in combined.values())
            if combined_bytes > self.batch_policy.max_artifact_bytes:
                break
            selected_rows.append(candidate)
            projections.append(candidate_projection)
            artifact_identities = combined
            artifact_bytes = combined_bytes
            cursor += 1

        projection = _merge_telemetry_projections(selected_rows, projections)
        batch_identity = {
            "schema_version": "scenario-platform-outbox-batch/v1.0",
            "profile_namespace": self.profile.namespace,
            "project_id": projection.project_id,
            "run_kind": projection.run_kind,
            "run_subject_id": projection.run_subject_id,
            "event_type": projection.event_type,
            "members": [
                {
                    "outbox_id": str(row["outbox_id"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "payload_sha256": str(row["payload_sha256"]),
                    "stream_sequence": int(row["stream_sequence"]),
                }
                for row in selected_rows
            ],
        }
        digest = hashlib.sha256(canonical_json(batch_identity).encode("utf-8")).hexdigest()
        return (
            _PreparedOutboxEnvelope(
                rows=tuple(selected_rows),
                projection=projection,
                envelope_id=f"outbox_batch_{digest[:32]}",
                idempotency_key=f"outbox-batch:{digest}",
            ),
            cursor,
        )

    def _fail_prepared(
        self,
        prepared: _PreparedOutboxEnvelope,
        error: Exception,
        *,
        blocked: bool,
    ) -> None:
        fallback = "CLOUD_PROJECTION_BLOCKED" if blocked else "CLOUD_PROVIDER_UNAVAILABLE"
        if len(prepared.rows) == 1 or self.batch_policy is None:
            row = prepared.rows[0]
            self.repository.mark_outbox_failed(
                str(row["outbox_id"]),
                _safe_error_code(error, fallback=fallback),
                blocked=blocked,
                lease_token=str(row["lease_token"]) if row.get("lease_token") else None,
            )
            return
        self.repository.mark_outbox_batch_failed(
            prepared.members,
            _safe_error_code(error, fallback=fallback),
            blocked=blocked,
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


def _rows_are_batch_consecutive(
    previous: dict[str, Any],
    candidate: dict[str, Any],
    policy: OutboxBatchPolicy,
) -> bool:
    """Keep retries and concurrent producer tails in stable batch identities."""

    if (
        previous.get("event_type") != policy.event_type
        or candidate.get("event_type") != policy.event_type
        or candidate.get("project_id") != previous.get("project_id")
        or candidate.get("analysis_id") != previous.get("analysis_id")
        or candidate.get("stream_id") != previous.get("stream_id")
        or candidate.get("attempt_count") != previous.get("attempt_count")
    ):
        return False
    try:
        return int(candidate["stream_sequence"]) == int(previous["stream_sequence"]) + 1
    except (KeyError, TypeError, ValueError):
        return False


def _projection_artifact_identities(
    projection: TelemetryProjection,
) -> tuple[dict[str, dict[str, Any]], int]:
    identities: dict[str, dict[str, Any]] = {}
    for artifact in projection.artifacts:
        value = artifact.model_dump(mode="json")
        existing = identities.get(artifact.local_artifact_id)
        if existing is not None and existing != value:
            raise CloudIntegrityError("CLOUD_BATCH_ARTIFACT_IDENTITY_CONFLICT")
        identities[artifact.local_artifact_id] = value
    return identities, sum(int(value["bytes"]) for value in identities.values())


def _merge_telemetry_projections(
    rows: Sequence[dict[str, Any]],
    projections: Sequence[TelemetryProjection],
) -> TelemetryProjection:
    if not rows or len(rows) != len(projections):
        raise ValueError("telemetry batch requires one projection per outbox row")
    first = projections[0]
    identity = (
        first.profile_namespace,
        first.project_id,
        first.run_kind,
        first.run_subject_id,
        first.event_type,
    )
    if any(
        (
            item.profile_namespace,
            item.project_id,
            item.run_kind,
            item.run_subject_id,
            item.event_type,
        )
        != identity
        for item in projections
    ):
        raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_RUN_IDENTITY_MISMATCH")
    if any(item.config != first.config for item in projections[1:]):
        raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_CONFIG_MISMATCH")
    event_sequences = [item.event_sequence for item in projections]
    try:
        committed_sequences = [int(row["stream_sequence"]) for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_SEQUENCE_MISMATCH") from exc
    if event_sequences != committed_sequences:
        raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_SEQUENCE_MISMATCH")

    table_columns: dict[str, list[str]] = {}
    table_rows: dict[str, list[list[Any]]] = {}
    table_row_hashes: dict[str, set[str]] = {}
    for projection in projections:
        for table in projection.tables:
            columns = table_columns.setdefault(table.logical_name, list(table.columns))
            if columns != table.columns:
                raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_TABLE_SCHEMA_MISMATCH")
            output_rows = table_rows.setdefault(table.logical_name, [])
            observed = table_row_hashes.setdefault(table.logical_name, set())
            for row in table.rows:
                row_key = canonical_json(row)
                if row_key not in observed:
                    output_rows.append(list(row))
                    observed.add(row_key)

    artifact_values: dict[str, dict[str, Any]] = {}
    for projection in projections:
        identities, _ = _projection_artifact_identities(projection)
        for local_id, value in identities.items():
            existing = artifact_values.get(local_id)
            if existing is not None and existing != value:
                raise CloudIntegrityError("CLOUD_BATCH_ARTIFACT_IDENTITY_CONFLICT")
            artifact_values[local_id] = value

    batch_count = len(projections)
    history = dict(projections[-1].history)
    summary = dict(projections[-1].summary)
    reserved = {
        "outbox_batch_event_count": batch_count,
        "outbox_batch_first_sequence": event_sequences[0],
        "outbox_batch_last_sequence": event_sequences[-1],
    }
    if any(key in history or key in summary for key in reserved):
        raise CloudIntegrityError("CLOUD_OUTBOX_BATCH_METADATA_CONFLICT")
    history.update(reserved)
    summary.update(reserved)
    return TelemetryProjection(
        profile_namespace=first.profile_namespace,
        project_id=first.project_id,
        run_kind=first.run_kind,
        run_subject_id=first.run_subject_id,
        event_type=first.event_type,
        event_sequence=event_sequences[-1],
        config=dict(first.config),
        history=history,
        summary=summary,
        tables=[
            TableProjection(
                logical_name=logical_name,
                columns=table_columns[logical_name],
                rows=table_rows[logical_name],
            )
            for logical_name in sorted(table_columns)
        ],
        input_artifact_refs=sorted(
            {reference for item in projections for reference in item.input_artifact_refs}
        ),
        artifacts=[
            ArtifactProjection.model_validate(artifact_values[local_id])
            for local_id in sorted(artifact_values)
        ],
    )


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
        acknowledgement_key, write_intent_key, acknowledgement_intent = (
            _wandb_acknowledgement_identity(
                profile=self.profile,
                envelope_id=envelope_id,
                idempotency_key=idempotency_key,
                projection=projection,
                target=target,
                run_id=run_id,
            )
        )
        projection_manifest, projection_source = _materialize_projection_receipt(
            profile=self.profile,
            envelope_id=envelope_id,
            idempotency_key=idempotency_key,
            projection=projection,
            directories=directories,
        )
        with directories.wandb_environment(base_url=target.api_base_url, online=True):
            completed = _lookup_projection_completion(
                public_api=_new_wandb_public_api(sdk, target),
                profile=self.profile,
                target=target,
                run_id=run_id,
                envelope_id=envelope_id,
                idempotency_key=idempotency_key,
                projection=projection,
                manifest=projection_manifest,
                directories=directories,
            )
            if completed is not None:
                return completed
            acknowledged = _lookup_wandb_acknowledgement(
                public_api=_new_wandb_public_api(sdk, target),
                profile=self.profile,
                target=target,
                run_id=run_id,
                acknowledgement_key=acknowledgement_key,
                expected_intent=acknowledgement_intent,
                projection=projection,
                idempotency_key=idempotency_key,
                directories=directories,
            )
            if acknowledged is not None:
                return acknowledged
            write_intent_exists = _lookup_wandb_write_intent(
                public_api=_new_wandb_public_api(sdk, target),
                target=target,
                run_id=run_id,
                write_intent_key=write_intent_key,
                expected_intent=acknowledgement_intent,
            )
            if write_intent_exists:
                completed = _poll_projection_completion(
                    sdk=sdk,
                    profile=self.profile,
                    target=target,
                    run_id=run_id,
                    envelope_id=envelope_id,
                    idempotency_key=idempotency_key,
                    projection=projection,
                    manifest=projection_manifest,
                    directories=directories,
                )
                if completed is not None:
                    return completed
            try:
                settings = sdk.Settings(
                    console="off", disable_code=True, disable_git=True, silent=True
                )
                intent_run = sdk.init(
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
            if intent_run is None:
                raise CloudTransportError("WANDB_RUN_INITIALIZATION_FAILED")
            try:
                intent_run.summary[write_intent_key] = {
                    "schema_version": _WRITE_INTENT_SCHEMA,
                    "intent": acknowledgement_intent,
                }
            except Exception as exc:
                raise CloudTransportError("WANDB_WRITE_INTENT_FAILED") from exc
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    intent_run.finish()
                except Exception as exc:
                    if not active_error:
                        raise CloudTransportError("WANDB_WRITE_INTENT_FINISH_FAILED") from exc
            if not _poll_wandb_write_intent(
                sdk=sdk,
                target=target,
                run_id=run_id,
                write_intent_key=write_intent_key,
                expected_intent=acknowledgement_intent,
            ):
                raise CloudTransportError("WANDB_WRITE_INTENT_READBACK_UNAVAILABLE")
            try:
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
            artifact_api = _new_wandb_public_api(sdk, target)
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
                        public_api=artifact_api,
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
                projection_reference = _log_projection_receipt(
                    sdk=sdk,
                    run=run,
                    public_api=artifact_api,
                    profile=self.profile,
                    target=target,
                    envelope_id=envelope_id,
                    idempotency_key=idempotency_key,
                    manifest=projection_manifest,
                    source=projection_source,
                    directories=directories,
                )
                run_url = _canonical_run_url(target, run_id)
                _require_target_provider_url(run_url, target=target, label="run URL")
                receipt = CloudUploadReceipt(
                    run_id=run_id,
                    run_url=run_url,
                    envelope_id=envelope_id,
                    artifacts=references,
                    projection_artifact=projection_reference,
                )
                run.summary[acknowledgement_key] = {
                    "schema_version": _ACKNOWLEDGEMENT_SCHEMA,
                    "intent": acknowledgement_intent,
                    "receipt": receipt.model_dump(mode="json"),
                }
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
            acknowledged = _poll_projection_completion(
                sdk=sdk,
                profile=self.profile,
                target=target,
                run_id=run_id,
                envelope_id=envelope_id,
                idempotency_key=idempotency_key,
                projection=projection,
                manifest=projection_manifest,
                directories=directories,
            )
            if acknowledged is None:
                raise CloudTransportError("WANDB_ACKNOWLEDGEMENT_READBACK_UNAVAILABLE")
            if acknowledged.model_dump(mode="json") != receipt.model_dump(mode="json"):
                raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_RECEIPT_MISMATCH")
            return acknowledged


def _wandb_acknowledgement_identity(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    projection: TelemetryProjection,
    target: WandbTarget,
    run_id: str,
) -> tuple[str, str, dict[str, str]]:
    """Bind one provider acknowledgement to exact local intent and destination."""

    projection_sha256 = hashlib.sha256(
        canonical_json(projection.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    target_sha256 = hashlib.sha256(
        canonical_json(target.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    key_digest = hashlib.sha256(
        canonical_json(
            {
                "profile_namespace": profile.namespace,
                "envelope_id": envelope_id,
                "target_sha256": target_sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    return (
        f"{profile.namespace}_outbox_ack_{key_digest[:32]}",
        f"{profile.namespace}_outbox_intent_{key_digest[:32]}",
        {
            "profile_namespace": profile.namespace,
            "envelope_id": envelope_id,
            "idempotency_key_sha256": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            "projection_sha256": projection_sha256,
            "target_sha256": target_sha256,
            "run_id": run_id,
        },
    )


def _materialize_projection_receipt(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    projection: TelemetryProjection,
    directories: ProjectCloudDirectories,
) -> tuple[ArtifactProjection, Path]:
    """Persist the canonical cloud projection used as the completion authority."""

    payload = (canonical_json(projection.model_dump(mode="json")) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    source = directories.telemetry_projections / f"{digest}.json"
    resolved_parent = directories.telemetry_projections.resolve()
    if (
        directories.telemetry_projections.is_symlink()
        or directories.run.resolve() not in resolved_parent.parents
    ):
        raise CloudIntegrityError("WANDB_PROJECTION_RECEIPT_DIRECTORY_UNSAFE")
    if source.exists():
        if source.is_symlink() or source.read_bytes() != payload:
            raise CloudIntegrityError("WANDB_PROJECTION_RECEIPT_LOCAL_COLLISION")
    else:
        atomic_write_bytes(source, payload, mode=0o600, overwrite=False)
    project_root = directories.root.parent.parent
    relative_source = source.relative_to(project_root).as_posix()
    manifest = ArtifactProjection(
        local_artifact_id=f"telemetry-projection:{envelope_id}",
        artifact_type=_PROJECTION_ARTIFACT_TYPE,
        logical_id=f"{profile.namespace}:{envelope_id}:{idempotency_key}",
        logical_name=_PROJECTION_LOGICAL_NAME,
        source_relative_path=relative_source,
        sha256=digest,
        bytes=len(payload),
        media_type="application/json",
    )
    if _verified_source(project_root, manifest) != source.resolve():
        raise CloudIntegrityError("WANDB_PROJECTION_RECEIPT_LOCAL_PATH_MISMATCH")
    return manifest, source


def _projection_artifact_name(profile: AppProfile, manifest: ArtifactProjection) -> str:
    return f"{profile.namespace}-telemetry-envelope-{manifest.sha256[:16]}"


def _projection_artifact_alias(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    manifest: ArtifactProjection,
) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "profile_namespace": profile.namespace,
                "envelope_id": envelope_id,
                "idempotency_key": idempotency_key,
                "projection_sha256": manifest.sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"outbox-projection-{digest[:32]}"


def _projection_artifact_metadata(
    *,
    profile: AppProfile,
    envelope_id: str,
    idempotency_key: str,
    manifest: ArtifactProjection,
    alias: str,
) -> dict[str, Any]:
    return {
        "schema_version": "scenario-platform-telemetry-projection/v1.0",
        "app_namespace": profile.namespace,
        "outbox_envelope_id": envelope_id,
        "outbox_idempotency_key_sha256": hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest(),
        "outbox_artifact_alias": alias,
        "sha256": manifest.sha256,
        "bytes": manifest.bytes,
        "media_type": manifest.media_type,
    }


def _log_projection_receipt(
    *,
    sdk: Any,
    run: Any,
    public_api: Any,
    profile: AppProfile,
    target: WandbTarget,
    envelope_id: str,
    idempotency_key: str,
    manifest: ArtifactProjection,
    source: Path,
    directories: ProjectCloudDirectories,
) -> CloudArtifactReference:
    artifact_name = _projection_artifact_name(profile, manifest)
    artifact_type = f"{profile.namespace}-{_PROJECTION_ARTIFACT_TYPE}"
    alias = _projection_artifact_alias(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        manifest=manifest,
    )
    metadata = _projection_artifact_metadata(
        profile=profile,
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
        artifact = sdk.Artifact(name=artifact_name, type=artifact_type, metadata=metadata)
        artifact.add_file(str(source), name=manifest.logical_name)
        logged = run.log_artifact(artifact, aliases=[alias])
        wait = getattr(logged, "wait", None)
        if not callable(wait):
            raise CloudTransportError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
        wait()
    _verify_committed_artifact_identity(
        logged,
        target=target,
        artifact_name=artifact_name,
        artifact_type=artifact_type,
        expected_metadata=metadata,
    )
    _verify_download(logged, manifest, directories)
    provider_id = str(getattr(logged, "id", "") or "")
    if not provider_id:
        raise CloudTransportError("WANDB_ARTIFACT_ID_UNAVAILABLE")
    provider_ref = _provider_artifact_ref(logged, target, logged)
    provider_url = str(getattr(logged, "url", "") or "") or None
    if provider_url is not None:
        _require_target_provider_url(provider_url, target=target, label="artifact URL")
    return CloudArtifactReference(
        local_artifact_id=manifest.local_artifact_id,
        provider_artifact_id=provider_id,
        provider_artifact_ref=provider_ref,
        provider_artifact_url=provider_url,
        sha256=manifest.sha256,
    )


def _canonical_run_url(target: WandbTarget, run_id: str) -> str:
    return f"{target.web_base_url.rstrip('/')}/{target.entity}/{target.project}/runs/{run_id}"


def _lookup_projection_completion(
    *,
    public_api: Any,
    profile: AppProfile,
    target: WandbTarget,
    run_id: str,
    envelope_id: str,
    idempotency_key: str,
    projection: TelemetryProjection,
    manifest: ArtifactProjection,
    directories: ProjectCloudDirectories,
) -> CloudUploadReceipt | None:
    """Resolve the immutable canonical envelope that authorizes local sync."""

    artifact_name = _projection_artifact_name(profile, manifest)
    artifact_type = f"{profile.namespace}-{_PROJECTION_ARTIFACT_TYPE}"
    alias = _projection_artifact_alias(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        manifest=manifest,
    )
    metadata = _projection_artifact_metadata(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        manifest=manifest,
        alias=alias,
    )
    committed = _lookup_committed_artifact(
        public_api=public_api,
        target=target,
        artifact_name=artifact_name,
        artifact_type=artifact_type,
        alias=alias,
    )
    if committed is None:
        return None
    _verify_committed_artifact_identity(
        committed,
        target=target,
        artifact_name=artifact_name,
        artifact_type=artifact_type,
        expected_metadata=metadata,
    )
    _verify_download(committed, manifest, directories)
    projection_reference = _cloud_artifact_reference(
        committed,
        target=target,
        local_artifact_id=manifest.local_artifact_id,
        sha256=manifest.sha256,
        registry_target=None,
    )
    output_references: list[CloudArtifactReference] = []
    for output in projection.artifacts:
        output_name = profile.artifact_name(output.artifact_type, output.logical_id)
        output_type = f"{profile.namespace}-{output.artifact_type}"
        output_alias = _artifact_idempotency_alias(
            profile=profile,
            envelope_id=envelope_id,
            idempotency_key=idempotency_key,
            manifest=output,
        )
        output_metadata = _artifact_idempotency_metadata(
            profile=profile,
            envelope_id=envelope_id,
            idempotency_key=idempotency_key,
            manifest=output,
            alias=output_alias,
        )
        output_artifact = _lookup_committed_artifact(
            public_api=public_api,
            target=target,
            artifact_name=output_name,
            artifact_type=output_type,
            alias=output_alias,
        )
        if output_artifact is None:
            raise CloudIntegrityError("WANDB_PROJECTION_OUTPUT_ARTIFACT_MISSING")
        _verify_committed_artifact_identity(
            output_artifact,
            target=target,
            artifact_name=output_name,
            artifact_type=output_type,
            expected_metadata=output_metadata,
        )
        _verify_download(output_artifact, output, directories)
        registry_target = (
            profile.registry_name(output.registry_alias)
            if output.registry_alias is not None
            else None
        )
        output_references.append(
            _cloud_artifact_reference(
                output_artifact,
                target=target,
                local_artifact_id=output.local_artifact_id,
                sha256=output.sha256,
                registry_target=registry_target,
            )
        )
    run_url = _canonical_run_url(target, run_id)
    _require_target_provider_url(run_url, target=target, label="run URL")
    return CloudUploadReceipt(
        run_id=run_id,
        run_url=run_url,
        envelope_id=envelope_id,
        artifacts=output_references,
        projection_artifact=projection_reference,
    )


def _cloud_artifact_reference(
    artifact: Any,
    *,
    target: WandbTarget,
    local_artifact_id: str,
    sha256: str,
    registry_target: str | None,
) -> CloudArtifactReference:
    provider_id = str(getattr(artifact, "id", "") or "")
    if not provider_id:
        raise CloudTransportError("WANDB_ARTIFACT_ID_UNAVAILABLE")
    provider_ref = _provider_artifact_ref(artifact, target, artifact)
    provider_url = str(getattr(artifact, "url", "") or "") or None
    if provider_url is not None:
        _require_target_provider_url(provider_url, target=target, label="artifact URL")
    return CloudArtifactReference(
        local_artifact_id=local_artifact_id,
        provider_artifact_id=provider_id,
        provider_artifact_ref=provider_ref,
        provider_artifact_url=provider_url,
        sha256=sha256,
        registry_target=registry_target,
    )


def _poll_projection_completion(
    *,
    sdk: Any,
    profile: AppProfile,
    target: WandbTarget,
    run_id: str,
    envelope_id: str,
    idempotency_key: str,
    projection: TelemetryProjection,
    manifest: ArtifactProjection,
    directories: ProjectCloudDirectories,
) -> CloudUploadReceipt | None:
    last_transport_error: CloudTransportError | None = None
    for delay in _READBACK_DELAYS_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            receipt = _lookup_projection_completion(
                public_api=_new_wandb_public_api(sdk, target),
                profile=profile,
                target=target,
                run_id=run_id,
                envelope_id=envelope_id,
                idempotency_key=idempotency_key,
                projection=projection,
                manifest=manifest,
                directories=directories,
            )
        except CloudTransportError as exc:
            last_transport_error = exc
            continue
        if receipt is not None:
            return receipt
    if last_transport_error is not None:
        raise last_transport_error
    return None


def _new_wandb_public_api(sdk: Any, target: WandbTarget) -> Any:
    try:
        return sdk.Api(overrides={"base_url": target.api_base_url.rstrip("/")})
    except Exception as exc:
        raise CloudTransportError("WANDB_ACKNOWLEDGEMENT_LOOKUP_UNAVAILABLE") from exc


def _poll_wandb_write_intent(
    *,
    sdk: Any,
    target: WandbTarget,
    run_id: str,
    write_intent_key: str,
    expected_intent: dict[str, str],
) -> bool:
    last_transport_error: CloudTransportError | None = None
    observed_absence = False
    for delay in _READBACK_DELAYS_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            found = _lookup_wandb_write_intent(
                public_api=_new_wandb_public_api(sdk, target),
                target=target,
                run_id=run_id,
                write_intent_key=write_intent_key,
                expected_intent=expected_intent,
            )
        except CloudTransportError as exc:
            last_transport_error = exc
            continue
        if found:
            return True
        observed_absence = True
    if not observed_absence and last_transport_error is not None:
        raise last_transport_error
    return False


def _lookup_wandb_acknowledgement(
    *,
    public_api: Any,
    profile: AppProfile,
    target: WandbTarget,
    run_id: str,
    acknowledgement_key: str,
    expected_intent: dict[str, str],
    projection: TelemetryProjection,
    idempotency_key: str,
    directories: ProjectCloudDirectories,
) -> CloudUploadReceipt | None:
    """Read back an exact remote acknowledgement before any repeat writes.

    Exact ``Api.runs`` ID filtering and ``Run.summary_metrics`` reads are
    documented W&B Public API primitives. An empty exact-ID result is absence;
    every lookup failure or duplicate is ambiguous and blocks writes that could
    repeat Tables, history, or lineage edges.
    """

    summary = _lookup_wandb_run_summary(
        public_api=public_api,
        target=target,
        run_id=run_id,
    )
    if summary is None:
        return None
    if acknowledgement_key not in summary:
        return None
    marker = summary[acknowledgement_key]
    if not isinstance(marker, Mapping) or set(marker) != _ACKNOWLEDGEMENT_KEYS:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_MARKER_MALFORMED")
    intent = marker.get("intent")
    if (
        marker.get("schema_version") != _ACKNOWLEDGEMENT_SCHEMA
        or not isinstance(intent, Mapping)
        or set(intent) != _ACKNOWLEDGEMENT_INTENT_KEYS
        or dict(intent) != expected_intent
    ):
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_INTENT_MISMATCH")
    try:
        receipt = CloudUploadReceipt.model_validate(marker.get("receipt"))
    except (TypeError, ValueError) as exc:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_RECEIPT_MALFORMED") from exc
    _verify_acknowledged_receipt(
        receipt,
        profile=profile,
        target=target,
        run_id=run_id,
        envelope_id=expected_intent["envelope_id"],
        projection=projection,
        public_api=public_api,
        idempotency_key=idempotency_key,
        directories=directories,
    )
    return receipt


def _lookup_wandb_write_intent(
    *,
    public_api: Any,
    target: WandbTarget,
    run_id: str,
    write_intent_key: str,
    expected_intent: dict[str, str],
) -> bool:
    summary = _lookup_wandb_run_summary(
        public_api=public_api,
        target=target,
        run_id=run_id,
    )
    if summary is None or write_intent_key not in summary:
        return False
    marker = summary[write_intent_key]
    intent = marker.get("intent") if isinstance(marker, Mapping) else None
    if (
        not isinstance(marker, Mapping)
        or set(marker) != _WRITE_INTENT_KEYS
        or marker.get("schema_version") != _WRITE_INTENT_SCHEMA
        or not isinstance(intent, Mapping)
        or set(intent) != _ACKNOWLEDGEMENT_INTENT_KEYS
        or dict(intent) != expected_intent
    ):
        raise CloudIntegrityError("WANDB_WRITE_INTENT_MISMATCH")
    return True


def _lookup_wandb_run_summary(
    *,
    public_api: Any,
    target: WandbTarget,
    run_id: str,
) -> dict[str, Any] | None:
    query = getattr(public_api, "runs", None)
    if not callable(query):
        raise CloudTransportError("WANDB_ACKNOWLEDGEMENT_LOOKUP_UNAVAILABLE")
    try:
        matches = list(
            query(
                f"{target.entity}/{target.project}",
                filters={"name": run_id},
                per_page=2,
                lazy=False,
            )
        )
    except Exception as exc:
        raise CloudTransportError("WANDB_ACKNOWLEDGEMENT_LOOKUP_AMBIGUOUS") from exc
    if len(matches) > 1:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_RUN_AMBIGUOUS")
    if not matches:
        return None
    remote_run = matches[0]
    remote_id = str(getattr(remote_run, "id", "") or "")
    if remote_id != run_id:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_RUN_MISMATCH")
    summary = getattr(remote_run, "summary_metrics", None)
    if not isinstance(summary, dict):
        raise CloudTransportError("WANDB_ACKNOWLEDGEMENT_SUMMARY_UNAVAILABLE")
    return summary


def _verify_acknowledged_receipt(
    receipt: CloudUploadReceipt,
    *,
    profile: AppProfile,
    target: WandbTarget,
    run_id: str,
    envelope_id: str,
    projection: TelemetryProjection,
    public_api: Any,
    idempotency_key: str,
    directories: ProjectCloudDirectories,
) -> None:
    if receipt.run_id != run_id or receipt.envelope_id != envelope_id:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_RECEIPT_IDENTITY_MISMATCH")
    _require_target_provider_url(receipt.run_url, target=target, label="run URL")
    projection_manifest, _ = _materialize_projection_receipt(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        projection=projection,
        directories=directories,
    )
    projection_reference = receipt.projection_artifact
    if projection_reference is None:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_PROJECTION_ARTIFACT_MISSING")
    if (
        projection_reference.local_artifact_id != projection_manifest.local_artifact_id
        or projection_reference.sha256 != projection_manifest.sha256
        or projection_reference.registry_target is not None
    ):
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_PROJECTION_ARTIFACT_MISMATCH")
    projection_name = _projection_artifact_name(profile, projection_manifest)
    projection_type = f"{profile.namespace}-{_PROJECTION_ARTIFACT_TYPE}"
    projection_alias = _projection_artifact_alias(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        manifest=projection_manifest,
    )
    projection_metadata = _projection_artifact_metadata(
        profile=profile,
        envelope_id=envelope_id,
        idempotency_key=idempotency_key,
        manifest=projection_manifest,
        alias=projection_alias,
    )
    committed_projection = _lookup_committed_artifact(
        public_api=public_api,
        target=target,
        artifact_name=projection_name,
        artifact_type=projection_type,
        alias=projection_alias,
    )
    if committed_projection is None:
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_PROJECTION_ARTIFACT_MISSING")
    _verify_committed_artifact_identity(
        committed_projection,
        target=target,
        artifact_name=projection_name,
        artifact_type=projection_type,
        expected_metadata=projection_metadata,
    )
    projection_id = str(getattr(committed_projection, "id", "") or "")
    projection_ref = _provider_artifact_ref(committed_projection, target, committed_projection)
    projection_url = str(getattr(committed_projection, "url", "") or "") or None
    if projection_url is not None:
        _require_target_provider_url(projection_url, target=target, label="artifact URL")
    if (
        projection_reference.provider_artifact_id != projection_id
        or projection_reference.provider_artifact_ref != projection_ref
        or projection_reference.provider_artifact_url != projection_url
    ):
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_PROJECTION_RECEIPT_MISMATCH")
    _verify_download(committed_projection, projection_manifest, directories)
    expected = {item.local_artifact_id: item for item in projection.artifacts}
    observed = {item.local_artifact_id: item for item in receipt.artifacts}
    if len(observed) != len(receipt.artifacts) or set(observed) != set(expected):
        raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_ARTIFACT_SET_MISMATCH")
    for local_id, reference in observed.items():
        manifest = expected[local_id]
        expected_name = profile.artifact_name(manifest.artifact_type, manifest.logical_id)
        expected_prefix = f"{target.entity}/{target.project}/{expected_name}:v"
        expected_registry = (
            profile.registry_name(manifest.registry_alias)
            if manifest.registry_alias is not None
            else None
        )
        if (
            reference.sha256 != manifest.sha256
            or reference.registry_target != expected_registry
            or not reference.provider_artifact_ref.startswith(expected_prefix)
        ):
            raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_ARTIFACT_IDENTITY_MISMATCH")
        if reference.provider_artifact_url is not None:
            _require_target_provider_url(
                reference.provider_artifact_url,
                target=target,
                label="artifact URL",
            )
        artifact_type = f"{profile.namespace}-{manifest.artifact_type}"
        alias = _artifact_idempotency_alias(
            profile=profile,
            envelope_id=envelope_id,
            idempotency_key=idempotency_key,
            manifest=manifest,
        )
        metadata = _artifact_idempotency_metadata(
            profile=profile,
            envelope_id=envelope_id,
            idempotency_key=idempotency_key,
            manifest=manifest,
            alias=alias,
        )
        committed = _lookup_committed_artifact(
            public_api=public_api,
            target=target,
            artifact_name=expected_name,
            artifact_type=artifact_type,
            alias=alias,
        )
        if committed is None:
            raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_ARTIFACT_MISSING")
        _verify_committed_artifact_identity(
            committed,
            target=target,
            artifact_name=expected_name,
            artifact_type=artifact_type,
            expected_metadata=metadata,
        )
        committed_id = str(getattr(committed, "id", "") or "")
        committed_ref = _provider_artifact_ref(committed, target, committed)
        if (
            reference.provider_artifact_id != committed_id
            or reference.provider_artifact_ref != committed_ref
        ):
            raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_ARTIFACT_RECEIPT_MISMATCH")
        committed_url = str(getattr(committed, "url", "") or "") or None
        if committed_url is not None:
            _require_target_provider_url(committed_url, target=target, label="artifact URL")
        if reference.provider_artifact_url != committed_url:
            raise CloudIntegrityError("WANDB_ACKNOWLEDGEMENT_ARTIFACT_URL_MISMATCH")
        _verify_download(committed, manifest, directories)


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
    fetch = getattr(public_api, "artifact", None)
    if not callable(fetch):
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_UNAVAILABLE")
    try:
        return fetch(reference, type=artifact_type)
    except Exception as exc:
        if _is_exact_wandb_artifact_not_found(exc, reference=reference):
            return None
        raise CloudTransportError("WANDB_ARTIFACT_IDEMPOTENCY_LOOKUP_AMBIGUOUS") from exc


def _is_exact_wandb_artifact_not_found(error: BaseException, *, reference: str) -> bool:
    """Recognize only W&B 0.28.1's exact missing-membership exception shape.

    ``Api.artifact_exists`` collapses several authorization and server failures
    into ``False`` in the pinned SDK, so it cannot protect an idempotent write.
    ``Api.artifact`` wraps the SDK's exact membership-missing ``ValueError`` in
    ``CommError.exc``. Every other exception, including HTTP 401/403/404/5xx,
    project absence, and unknown SDK drift, remains ambiguous and fails closed.
    """

    nested = getattr(error, "exc", None)
    if not isinstance(nested, ValueError):
        return False
    entity, project, membership = reference.split("/", 2)
    expected = f"artifact membership {membership!r} not found in {f'{entity}/{project}'!r}"
    return str(nested) == expected


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
    "OutboxBatchPolicy",
    "OutboxDrainReport",
    "OutboxRepository",
    "ProjectCloudDirectories",
    "TableProjection",
    "TelemetryProjection",
    "TelemetryProjector",
    "WandbSdkLineageUploader",
    "WandbTarget",
)
