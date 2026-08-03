"""Required W&B project binding and durable implemented-evidence cloud projection.

SQLite plus the local content-addressed store remain authoritative.  A cloud
failure changes only the durable outbox state; it never changes engineering
state, exact SWMM results, fitness, or export eligibility.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .contracts import (
    CloudArtifactManifest,
    CloudSyncState,
    ModelRevision,
    ObservabilityBinding,
    ObservabilityMode,
    ProjectDataPolicy,
    TelemetryEnvelope,
    TelemetryState,
    WandbRunReference,
    WeaveTraceReference,
)
from .repository import ProjectRepository

MAX_METADATA_BYTES = 256 * 1024
MAX_PROBE_BYTES = 4 * 1024
_DESTINATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WANDB_ARTIFACT_REFERENCE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,2}"
    r":v(?:0|[1-9][0-9]*)"
)
_ABSOLUTE_PATH = re.compile(
    r"(?:"
    r"/Users/|"
    r"[A-Za-z]:\\|"
    r"\\\\|"
    r"(?:^|[\s\"'=])/(?:[^\s/\"']+/)+[^\s\"']*|"
    r"(?:^|[\s\"'=])/[^\s/\"']+(?=$|[\"'])"
    r")"
)
_PROHIBITED_KEYS = {
    "apikey",
    "authorization",
    "consolelog",
    "cookie",
    "credential",
    "environment",
    "environmentvariable",
    "localabsolutepath",
    "localpath",
    "password",
    "privatekey",
    "rawconsolelog",
    "secret",
    "stderr",
    "stdout",
    "token",
}
_WANDB_ENV_LOCK = threading.RLock()


class CredentialSession:
    """Process-memory W&B credential initialized only by the native host."""

    def __init__(self) -> None:
        self._api_key: bytearray | None = None
        self._base_url = "https://api.wandb.ai"

    def configure(self, api_key: str, base_url: str) -> None:
        self._validate(api_key)
        if self._api_key is not None:
            self.clear()
        self._api_key = bytearray(api_key.encode("utf-8"))
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def _validate(api_key: str) -> None:
        if not 20 <= len(api_key) <= 512 or any(character.isspace() for character in api_key):
            raise ValueError("W&B credential has an invalid shape")

    @contextmanager
    def candidate(self, api_key: str, base_url: str):
        """Install a candidate credential and roll it back unless validation succeeds.

        A failed account reconfiguration must not destroy a previously verified
        session or leave the rejected credential resident in worker memory.
        """

        self._validate(api_key)
        candidate = bytearray(api_key.encode("utf-8"))
        previous = self._api_key
        previous_base_url = self._base_url
        self._api_key = candidate
        self._base_url = base_url.rstrip("/")
        try:
            yield
        except BaseException:
            candidate[:] = b"\0" * len(candidate)
            self._api_key = previous
            self._base_url = previous_base_url
            raise
        else:
            if previous is not None:
                previous[:] = b"\0" * len(previous)

    @property
    def configured(self) -> bool:
        return self._api_key is not None or bool(os.environ.get("WANDB_API_KEY"))

    @contextmanager
    def environment(self):
        with _WANDB_ENV_LOCK:
            previous_key = os.environ.get("WANDB_API_KEY")
            previous_base = os.environ.get("WANDB_BASE_URL")
            temporary_key: str | None = None
            if self._api_key is not None:
                temporary_key = self._api_key.decode("utf-8")
                os.environ["WANDB_API_KEY"] = temporary_key
                os.environ["WANDB_BASE_URL"] = self._base_url
            try:
                yield
            finally:
                if previous_key is None:
                    os.environ.pop("WANDB_API_KEY", None)
                else:
                    os.environ["WANDB_API_KEY"] = previous_key
                if previous_base is None:
                    os.environ.pop("WANDB_BASE_URL", None)
                else:
                    os.environ["WANDB_BASE_URL"] = previous_base
                temporary_key = None

    def clear(self) -> None:
        if self._api_key is not None:
            self._api_key[:] = b"\0" * len(self._api_key)
            self._api_key = None


@contextmanager
def _wandb_environment(workspace: Path):
    """Temporarily apply quiet, project-scoped W&B SDK directories."""

    root = (workspace / "cloud" / "wandb").resolve()
    values = {
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
        "WANDB_SILENT": "true",
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_DIR": str(root / "run"),
        "WANDB_CACHE_DIR": str(root / "cache"),
        "WANDB_ARTIFACT_DIR": str(root / "artifacts"),
    }
    for key in ("WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_ARTIFACT_DIR"):
        Path(values[key]).mkdir(parents=True, exist_ok=True)
    with _WANDB_ENV_LOCK:
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


@dataclass(frozen=True)
class WandbUploadReceipt:
    run_id: str
    url: str
    artifact_ids: list[str]


class WandbEvidenceAdapter(Protocol):
    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding: ...

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt: ...


class WeaveAssistantAdapter(Protocol):
    def trace_call(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference: ...


class WandbAdapter:
    """Lazy SDK adapter; credentials are read only from the worker environment."""

    def __init__(self, credentials: CredentialSession | None = None) -> None:
        self.credentials = credentials or CredentialSession()

    def _sdk(self) -> Any:
        if not os.environ.get("WANDB_API_KEY"):
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        try:
            import wandb  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WANDB_SDK_UNAVAILABLE") from exc
        return wandb

    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding:
        if binding.mode != ObservabilityMode.ONLINE:
            raise ValueError("initial W&B binding validation requires online mode")
        _validate_destination(binding)
        if binding.deployment != policy.deployment:
            raise ValueError("binding deployment does not match the project data policy")
        with (
            self.credentials.environment(),
            tempfile.TemporaryDirectory(prefix="gi-wandb-validate-") as temporary,
            _wandb_environment(Path(temporary)),
        ):
            return self._validate_binding_online(binding)

    def _validate_binding_online(self, binding: ObservabilityBinding) -> ObservabilityBinding:
        _validate_destination(binding)
        wandb = self._sdk()
        api = wandb.Api()
        try:
            project = api.project(binding.external_project, entity=binding.entity)
        except Exception as exc:
            raise RuntimeError("WANDB_PROJECT_NOT_ACCESSIBLE") from exc
        access = str(
            getattr(project, "access", None)
            or getattr(project, "visibility", None)
            or getattr(project, "access_level", "")
        ).casefold()
        # W&B's legacy PRIVATE access value is not equivalent to its Restricted
        # visibility scope. Fail closed unless an authoritative provider field
        # returns the exact scope required by the data policy.
        if access != "restricted":
            raise ValueError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
        project_id = str(getattr(project, "id", "") or "")
        if not project_id:
            raise RuntimeError("WANDB_PROJECT_ID_UNAVAILABLE")
        if binding.external_project_id not in {"pending-validation", project_id}:
            raise RuntimeError("WANDB_PROJECT_ID_CHANGED")
        payload = binding.model_dump(mode="json")
        payload.update(
            {
                "external_project_id": project_id,
                "verified": True,
                "verified_at": datetime.now(UTC),
            }
        )
        return ObservabilityBinding.model_validate(payload)

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt:
        with _wandb_environment(workspace), self.credentials.environment():
            self._validate_binding_online(binding)
            return self._upload_online(envelope, binding, artifact_paths)

    def _upload_online(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
    ) -> WandbUploadReceipt:
        wandb = self._sdk()
        input_artifact_refs = _wandb_input_artifact_refs(envelope.metadata)
        table_specs = _wandb_table_specs(envelope.metadata)
        config_metadata = dict(envelope.metadata)
        config_metadata.pop("tables", None)
        validated_sources: dict[str, Path] = {}
        for manifest in envelope.artifacts:
            source = artifact_paths.get(manifest.artifact_id)
            if source is None or not source.is_file():
                raise RuntimeError("WANDB_ARTIFACT_SOURCE_MISSING")
            if source.stat().st_size != manifest.bytes or _sha256_path(source) != manifest.sha256:
                raise RuntimeError("WANDB_ARTIFACT_SOURCE_HASH_MISMATCH")
            validated_sources[manifest.artifact_id] = source
        settings = wandb.Settings(
            console="off",
            disable_code=True,
            disable_git=True,
            silent=True,
        )
        run_id = hashlib.sha256(
            f"gi-v2|{envelope.project_id}|{envelope.envelope_id}".encode()
        ).hexdigest()[:24]
        run = wandb.init(
            entity=binding.entity,
            project=binding.external_project,
            id=run_id,
            resume="allow",
            job_type=envelope.event_type,
            config={
                "schema_version": "2.0",
                "category": envelope.category,
                "subject_id": envelope.subject_id,
                "metadata": config_metadata,
                "local_authority": True,
            },
            settings=settings,
            reinit=True,
        )
        if run is None:  # pragma: no cover - defensive around third-party SDK
            raise RuntimeError("WANDB_RUN_CREATION_FAILED")
        artifact_ids: list[str] = []
        artifact_projection: list[dict[str, Any]] = []
        try:
            numeric = _finite_numeric_values(envelope.metadata)
            if numeric:
                run.log(numeric)
            for table_name, table_spec in table_specs.items():
                run.log(
                    {
                        table_name: wandb.Table(
                            columns=table_spec["columns"],
                            data=table_spec["rows"],
                        )
                    }
                )
            run.summary["table_names"] = sorted(table_specs)
            run.summary["envelope_sha256"] = _envelope_sha256(envelope)
            run.summary["local_authority"] = True
            for reference in input_artifact_refs:
                run.use_artifact(reference)
            run.summary["input_artifact_refs"] = input_artifact_refs
            for manifest in envelope.artifacts:
                source = validated_sources[manifest.artifact_id]
                artifact = wandb.Artifact(
                    name=f"{manifest.category}-{manifest.artifact_id}",
                    type=manifest.category,
                    metadata={
                        "sha256": manifest.sha256,
                        "bytes": manifest.bytes,
                        "media_type": manifest.media_type,
                        "source_object_id": manifest.source_object_id,
                    },
                )
                artifact.add_file(
                    local_path=str(source),
                    name=manifest.logical_name,
                )
                logged = run.log_artifact(artifact)
                wait = getattr(logged, "wait", None)
                if not callable(wait):
                    raise TypeError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
                wait()
                _verify_downloaded_wandb_artifact(logged, manifest)
                artifact_id = str(getattr(logged, "id", "") or "")
                if not artifact_id:
                    raise RuntimeError("WANDB_ARTIFACT_ID_UNAVAILABLE")
                artifact_ids.append(artifact_id)
                artifact_projection.append(
                    {
                        "local_artifact_id": manifest.artifact_id,
                        "provider_artifact_id": artifact_id,
                        "provider_artifact_ref": _logged_wandb_artifact_ref(logged, binding),
                        "sha256": manifest.sha256,
                    }
                )
            run.summary["output_artifact_ids"] = artifact_ids
            run.summary["artifact_projection"] = artifact_projection
            run_id = str(run.id)
            url = str(getattr(run, "url", "") or "")
            if not url:
                url = (
                    f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                    f"{binding.external_project}/runs/{run_id}"
                )
        finally:
            run.finish()
        return WandbUploadReceipt(run_id=run_id, url=url, artifact_ids=artifact_ids)

    def roundtrip_probe(
        self,
        binding: ObservabilityBinding,
        payload: bytes,
        expected_sha256: str,
    ) -> WandbRunReference:
        """Env-gated CI probe using the same safe SDK settings as evidence sync."""

        with (
            self.credentials.environment(),
            tempfile.TemporaryDirectory(prefix="gi-wandb-probe-env-") as temporary,
            _wandb_environment(Path(temporary)),
        ):
            verified = self._validate_binding_online(binding)
            return self._roundtrip_probe_online(verified, payload, expected_sha256)

    def _roundtrip_probe_online(
        self,
        binding: ObservabilityBinding,
        payload: bytes,
        expected_sha256: str,
    ) -> WandbRunReference:

        if len(payload) > MAX_PROBE_BYTES:
            raise ValueError("integration probe payload exceeds the safe limit")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256 or _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("integration probe payload hash mismatch")
        wandb = self._sdk()
        probe_id = f"gi-probe-{expected_sha256[:16]}"
        with tempfile.TemporaryDirectory(prefix="gi-wandb-probe-") as temporary:
            root = Path(temporary)
            source = root / "probe.bin"
            source.write_bytes(payload)
            run = wandb.init(
                entity=binding.entity,
                project=binding.external_project,
                id=hashlib.sha256(
                    f"gi-probe|{binding.entity}|{binding.external_project}|{expected_sha256}".encode()
                ).hexdigest()[:24],
                resume="allow",
                job_type="integration-probe",
                settings=wandb.Settings(
                    console="off", disable_code=True, disable_git=True, silent=True
                ),
                reinit=True,
            )
            if run is None:  # pragma: no cover
                raise RuntimeError("WANDB_PROBE_RUN_CREATION_FAILED")
            try:
                artifact = wandb.Artifact(
                    name=probe_id,
                    type="integration-probe",
                    metadata={"sha256": expected_sha256, "bytes": len(payload)},
                )
                artifact.add_file(str(source), name="probe.bin")
                logged = run.log_artifact(artifact)
                wait = getattr(logged, "wait", None)
                if not callable(wait):
                    raise TypeError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
                wait()
                download = getattr(logged, "download", None)
                if not callable(download):
                    raise TypeError("WANDB_ARTIFACT_DOWNLOAD_UNAVAILABLE")
                downloaded = download(root=str(root / "download"), skip_cache=True)
                target = Path(downloaded) / "probe.bin"
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256
                ):
                    raise RuntimeError("WANDB_PROBE_HASH_MISMATCH")
                artifact_id = str(getattr(logged, "id", "") or "")
                if not artifact_id:
                    raise RuntimeError("WANDB_ARTIFACT_ID_UNAVAILABLE")
                run_id = str(run.id)
                run_url = str(getattr(run, "url", "") or "")
            finally:
                run.finish()
        return WandbRunReference(
            project_id=binding.project_id,
            envelope_id=f"probe:{expected_sha256}",
            entity=binding.entity,
            external_project=binding.external_project,
            run_id=run_id,
            url=run_url
            or (
                f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                f"{binding.external_project}/runs/{run_id}"
            ),
            artifact_ids=[artifact_id],
        )


class WeaveAdapter:
    def __init__(self, credentials: CredentialSession | None = None) -> None:
        self.credentials = credentials or CredentialSession()

    def trace_call(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference:
        if binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_REQUIRES_ONLINE_WEAVE")
        if not self.credentials.configured:
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        _validate_cloud_value(inputs, context="assistant trace inputs")
        _validate_cloud_value(output, context="assistant trace output")
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc
        with self.credentials.environment():
            client = weave.init(
                f"{binding.entity}/{binding.external_project}",
                settings={"redact_pii": True},
            )
            from weave.utils import sanitize  # type: ignore[import-not-found]

            for key in sorted(_PROHIBITED_KEYS):
                sanitize.add_redact_key(key)
            call = client.create_call(
                op=f"gi_scenario_{operation}",
                inputs=inputs,
                attributes={"gi_call_id": call_id, "schema_version": "2.0"},
            )
            client.finish_call(call, output=output)
            trace_id = str(getattr(call, "trace_id", "") or "")
            provider_call_id = str(getattr(call, "id", "") or "")
            if not trace_id or not provider_call_id:
                raise RuntimeError("WEAVE_TRACE_ID_UNAVAILABLE")
            client.flush()
            acknowledged_call = client.get_call(provider_call_id)
            if acknowledged_call is None:
                raise RuntimeError("WEAVE_TRACE_NOT_ACKNOWLEDGED")
            provider_url = str(
                getattr(acknowledged_call, "ui_url", "") or getattr(call, "ui_url", "") or ""
            )
        return WeaveTraceReference(
            project_id=binding.project_id,
            call_id=call_id,
            entity=binding.entity,
            external_project=binding.external_project,
            trace_id=trace_id,
            provider_call_id=provider_call_id,
            url=provider_url
            or (
                f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                f"{binding.external_project}/weave/r/call/{provider_call_id}"
            ),
        )


class ObservabilityService:
    def __init__(
        self,
        *,
        wandb_adapter: WandbEvidenceAdapter | None = None,
        weave_adapter: WeaveAssistantAdapter | None = None,
    ) -> None:
        self.credentials = CredentialSession()
        self.wandb = wandb_adapter or WandbAdapter(self.credentials)
        self.weave = weave_adapter or WeaveAdapter(self.credentials)

    def initialize_credentials(self, api_key: str, base_url: str) -> None:
        self.credentials.configure(api_key, base_url)

    @contextmanager
    def candidate_credentials(self, api_key: str, base_url: str):
        with self.credentials.candidate(api_key, base_url):
            yield

    def clear_credentials(self) -> None:
        self.credentials.clear()

    def validate_binding(
        self,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
    ) -> ObservabilityBinding:
        validated_policy = ProjectDataPolicy.model_validate(policy)
        candidate = ObservabilityBinding.model_validate(binding)
        if candidate.project_id != validated_policy.project_id:
            raise ValueError("W&B binding and data policy must identify the same GI project")
        return self.wandb.validate_binding(candidate, validated_policy)

    def connect(
        self,
        repository: ProjectRepository,
        *,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
    ) -> CloudSyncState:
        repository.get_project(ProjectDataPolicy.model_validate(policy).project_id)
        validated = self.validate_binding(policy, binding)
        repository.upsert_project_data_policy(policy)
        repository.connect_observability_binding(validated)
        return self.status(repository, validated.project_id)

    def status(self, repository: ProjectRepository, project_id: str) -> CloudSyncState:
        repository.get_project(project_id)
        state = repository.observability_status(project_id)
        binding = state["binding"]
        counts = state["outbox_counts"]
        if binding is None or not binding.get("verified") or not self.credentials.configured:
            connection_state = "AUTHENTICATION_REQUIRED"
        elif binding.get("mode") == ObservabilityMode.OFFLINE_QUEUE.value:
            connection_state = "SYNC_QUEUED_OFFLINE"
        elif counts.get("uploading", 0) or counts.get("pending", 0):
            connection_state = "SYNCING"
        elif counts.get("failed", 0):
            connection_state = "FAILED_RETRYING"
        else:
            connection_state = "CONNECTED"
        online = bool(
            binding
            and binding.get("mode") == ObservabilityMode.ONLINE.value
            and self.credentials.configured
        )
        verified = bool(binding and binding.get("verified"))
        return CloudSyncState(
            project_id=project_id,
            state=connection_state,
            binding=binding,
            can_enter_home=state["can_enter_home"],
            can_open_project=verified,
            can_run_local=verified,
            can_sync=verified and online,
            can_use_assistant=verified and online and self.credentials.configured,
            online=online,
            outbox_counts=counts,
            pending_artifact_bytes=state["pending_artifact_bytes"],
            last_synced_at=state["last_synced_at"],
            last_error_code=state["last_error_code"],
        )

    def preview_upload(
        self,
        repository: ProjectRepository,
        envelope: TelemetryEnvelope | dict[str, Any],
    ) -> dict[str, Any]:
        validated = TelemetryEnvelope.model_validate(envelope)
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(validated.project_id)
        )
        policy = ProjectDataPolicy.model_validate(
            repository.get_project_data_policy(validated.project_id)
        )
        blockers = _validate_envelope(repository, validated, binding, policy)
        return {
            "project_id": validated.project_id,
            "envelope_id": validated.envelope_id,
            "destination": f"{binding.entity}/{binding.external_project}",
            "access": binding.access,
            "deployment": binding.deployment,
            "category": validated.category,
            "artifact_categories": sorted({item.category for item in validated.artifacts}),
            "artifact_count": len(validated.artifacts),
            "artifact_bytes": sum(item.bytes for item in validated.artifacts),
            "metadata_fields": sorted(validated.metadata),
            "excluded_categories": policy.prohibited_categories,
            "payload_sha256": _envelope_sha256(validated),
            "blockers": blockers,
            "local_authority": True,
        }

    def sync(
        self,
        repository: ProjectRepository,
        *,
        envelope: TelemetryEnvelope | dict[str, Any],
        preview_sha256: str,
    ) -> dict[str, Any]:
        validated = TelemetryEnvelope.model_validate(envelope)
        preview = self.preview_upload(repository, validated)
        if preview["blockers"]:
            raise ValueError("cloud evidence upload is blocked: " + "; ".join(preview["blockers"]))
        if preview_sha256 != preview["payload_sha256"]:
            raise ValueError("cloud evidence changed after upload preview")
        queued = repository.enqueue_telemetry_envelope(validated)
        if queued["state"] == TelemetryState.SYNCED.value:
            return _sync_payload(queued, repository.get_wandb_run_reference(validated.envelope_id))
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(validated.project_id)
        )
        if binding.mode == ObservabilityMode.OFFLINE_QUEUE:
            return _sync_payload(queued, None)
        claimed = repository.claim_telemetry_outbox_item(queued["outbox_id"])
        paths = {
            manifest.artifact_id: repository.root / "blobs" / "sha256" / manifest.local_blob_sha256
            for manifest in validated.artifacts
        }
        try:
            receipt = self.wandb.upload(validated, binding, paths, repository.root)
            reference = repository.add_wandb_run_reference(
                WandbRunReference(
                    project_id=validated.project_id,
                    envelope_id=validated.envelope_id,
                    entity=binding.entity,
                    external_project=binding.external_project,
                    run_id=receipt.run_id,
                    url=receipt.url,
                    artifact_ids=receipt.artifact_ids,
                )
            )
            completed = repository.mark_telemetry_outbox_item(
                claimed["outbox_id"], TelemetryState.SYNCED
            )
            repository.record_first_cloud_upload(validated.project_id)
            return _sync_payload(completed, reference)
        except Exception as exc:  # noqa: BLE001 - normalized; no SDK error text persisted
            code = _safe_error_code(exc, prefix="WANDB_UPLOAD")
            failed = repository.mark_telemetry_outbox_item(
                claimed["outbox_id"], TelemetryState.FAILED, error_code=code
            )
            return _sync_payload(failed, None)

    def retry(self, repository: ProjectRepository, outbox_id: str) -> dict[str, Any]:
        current = repository.get_telemetry_outbox_item(outbox_id)
        if current["state"] == TelemetryState.SYNCED.value:
            return _sync_payload(
                current,
                repository.get_wandb_run_reference(current["envelope"]["envelope_id"]),
            )
        repository.retry_telemetry_outbox_item(outbox_id)
        envelope = TelemetryEnvelope.model_validate(current["envelope"])
        return self.sync(
            repository,
            envelope=envelope,
            preview_sha256=_envelope_sha256(envelope),
        )

    def trace_assistant_call(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference:
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(project_id)
        )
        if not binding.verified or binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_DISABLED_WEAVE_UNAVAILABLE")
        policy = ProjectDataPolicy.model_validate(repository.get_project_data_policy(project_id))
        binding = self.validate_binding(policy, binding)
        trace = self.weave.trace_call(
            binding,
            call_id=call_id,
            operation=operation,
            inputs=inputs,
            output=output,
        )
        repository.add_weave_trace_reference(trace)
        return trace

    def rebind(
        self,
        repository: ProjectRepository,
        *,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
        revision: ModelRevision | dict[str, Any],
    ) -> CloudSyncState:
        validated = self.validate_binding(policy, binding)
        repository.rebind_observability_binding(validated, revision)
        return self.status(repository, validated.project_id)


def _validate_destination(binding: ObservabilityBinding) -> None:
    for label, value in (("entity", binding.entity), ("project", binding.external_project)):
        if _DESTINATION.fullmatch(value) is None:
            raise ValueError(f"W&B {label} contains unsupported characters")
    if binding.access != "Restricted":
        raise ValueError("W&B project access must be Restricted")


def _validate_envelope(
    repository: ProjectRepository,
    envelope: TelemetryEnvelope,
    binding: ObservabilityBinding,
    policy: ProjectDataPolicy,
) -> list[str]:
    blockers: list[str] = []
    if not binding.verified:
        blockers.append("W&B project binding is not verified")
    if envelope.binding_id != binding.binding_id:
        blockers.append("evidence envelope does not use the active W&B binding")
    try:
        _validate_cloud_value(envelope.metadata, context="cloud evidence metadata")
    except ValueError as exc:
        blockers.append(str(exc))
    metadata_bytes = len(_canonical_json(envelope.metadata))
    if metadata_bytes > MAX_METADATA_BYTES:
        blockers.append("cloud evidence metadata exceeds the size limit")
    for manifest in envelope.artifacts:
        if manifest.project_id != envelope.project_id:
            blockers.append(f"artifact {manifest.artifact_id} belongs to another project")
            continue
        if manifest.category not in policy.permitted_categories:
            blockers.append(f"data policy does not permit {manifest.category} artifacts")
        path = repository.root / "blobs" / "sha256" / manifest.local_blob_sha256
        if not path.is_file():
            blockers.append(f"artifact {manifest.artifact_id} is missing from local storage")
        elif path.stat().st_size != manifest.bytes or _sha256_path(path) != manifest.sha256:
            blockers.append(f"artifact {manifest.artifact_id} does not match its local hash")
        elif manifest.media_type.startswith("text/") or manifest.logical_name.casefold().endswith(
            (".inp", ".rpt", ".json", ".csv", ".md", ".txt")
        ):
            text = path.read_text(encoding="utf-8", errors="replace")
            if _contains_local_absolute_path(text):
                blockers.append(
                    f"artifact {manifest.artifact_id} contains a local absolute path; materialize portable references first"
                )
    return sorted(set(blockers))


def _validate_cloud_value(value: Any, *, context: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if any(fragment in normalized for fragment in _PROHIBITED_KEYS):
                raise ValueError(f"{context} contains prohibited field {path}.{key}")
            _validate_cloud_value(item, context=context, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_cloud_value(item, context=context, path=f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{context} contains a non-finite number at {path}")
    elif isinstance(value, str) and _contains_local_absolute_path(value):
        raise ValueError(f"{context} contains a local absolute path at {path}")
    elif not isinstance(value, (str, int, float, bool, type(None), dict, list)):
        raise ValueError(f"{context} contains unsupported data at {path}")


def _contains_local_absolute_path(value: str) -> bool:
    return any(_ABSOLUTE_PATH.search(line.strip()) for line in value.splitlines())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _envelope_sha256(envelope: TelemetryEnvelope) -> str:
    return hashlib.sha256(_canonical_json(envelope.model_dump(mode="json"))).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wandb_input_artifact_refs(metadata: dict[str, Any]) -> list[str]:
    """Return explicit, provider-qualified upstream artifacts for W&B lineage.

    Engineering producers may place immutable W&B references in
    ``metadata.input_artifact_refs`` after a prior upload has been confirmed.
    Local paths and implicit ``latest`` aliases are deliberately not accepted:
    a run must name the exact artifact version it consumed.
    """

    supplied = metadata.get("input_artifact_refs", [])
    if supplied is None:
        return []
    if not isinstance(supplied, list):
        raise TypeError("input_artifact_refs must be an array of W&B artifact references")
    references: list[str] = []
    for item in supplied:
        if isinstance(item, str) and item.casefold().endswith(":latest"):
            raise ValueError("input artifact lineage must pin a W&B artifact version")
        if not isinstance(item, str) or _WANDB_ARTIFACT_REFERENCE.fullmatch(item) is None:
            raise ValueError(
                "input_artifact_refs must use a qualified, versioned W&B artifact reference"
            )
        if item not in references:
            references.append(item)
    return references


def _wandb_table_specs(metadata: dict[str, Any]) -> dict[str, dict[str, list[Any]]]:
    supplied = metadata.get("tables", {})
    if supplied is None:
        return {}
    if not isinstance(supplied, dict):
        raise TypeError("W&B tables metadata must be an object")
    result: dict[str, dict[str, list[Any]]] = {}
    for name, spec in supplied.items():
        if not isinstance(name, str) or _DESTINATION.fullmatch(name) is None:
            raise ValueError("W&B table names contain unsupported characters")
        if not isinstance(spec, dict):
            raise TypeError(f"W&B table {name!r} must be an object")
        columns = spec.get("columns")
        rows = spec.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)
        ):
            raise ValueError(f"W&B table {name!r} requires unique string columns")
        if not isinstance(rows, list) or len(rows) > 100_000:
            raise ValueError(f"W&B table {name!r} has an invalid row collection")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError(f"W&B table {name!r} row width does not match its columns")
        result[name] = {"columns": columns, "rows": rows}
    return result


def _artifact_relative_path(logical_name: str) -> PurePosixPath:
    relative = PurePosixPath(logical_name.replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("WANDB_ARTIFACT_LOGICAL_NAME_UNSAFE")
    return relative


def _logged_wandb_artifact_ref(logged: Any, binding: ObservabilityBinding) -> str:
    try:
        reference = str(getattr(logged, "qualified_name", "") or "")
    except Exception as exc:  # pragma: no cover - normalized provider boundary
        raise RuntimeError("WANDB_ARTIFACT_REFERENCE_UNAVAILABLE") from exc
    expected_prefix = f"{binding.entity}/{binding.external_project}/"
    if _WANDB_ARTIFACT_REFERENCE.fullmatch(reference) is None or not reference.startswith(
        expected_prefix
    ):
        raise RuntimeError("WANDB_ARTIFACT_REFERENCE_UNAVAILABLE")
    return reference


def _verify_downloaded_wandb_artifact(logged: Any, manifest: CloudArtifactManifest) -> None:
    """Download the exact logged version and compare it with the local manifest."""

    download = getattr(logged, "download", None)
    if not callable(download):
        raise TypeError("WANDB_ARTIFACT_DOWNLOAD_UNAVAILABLE")
    relative = _artifact_relative_path(manifest.logical_name)
    with tempfile.TemporaryDirectory(prefix="gi-wandb-verify-") as temporary:
        requested_root = Path(temporary).resolve()
        downloaded_value = download(root=str(requested_root), skip_cache=True)
        if downloaded_value is None:
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        downloaded_root = Path(downloaded_value).resolve()
        if not downloaded_root.is_dir():
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        target = downloaded_root.joinpath(*relative.parts).resolve()
        if (
            target == downloaded_root
            or downloaded_root not in target.parents
            or not target.is_file()
        ):
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        if target.stat().st_size != manifest.bytes or _sha256_path(target) != manifest.sha256:
            raise RuntimeError("WANDB_ARTIFACT_HASH_MISMATCH")


def _finite_numeric_values(value: dict[str, Any], prefix: str = "") -> dict[str, float]:
    result: dict[str, float] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
        ):
            result[name] = float(item)
        elif isinstance(item, dict):
            result.update(_finite_numeric_values(item, name))
    return result


def _safe_error_code(error: Exception, *, prefix: str) -> str:
    message = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", message):
        return message
    return f"{prefix}_{type(error).__name__.upper()}"[:64]


def _sync_payload(item: dict[str, Any], reference: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "outbox_id": item["outbox_id"],
        "envelope_id": item["envelope"]["envelope_id"],
        "state": item["state"],
        "attempts": item["attempts"],
        "payload_sha256": item["payload_sha256"],
        "last_error_code": item.get("last_error_code"),
        "wandb_run": reference,
        "local_authority": True,
    }


def validate_binding_from_environment(
    *,
    entity: str,
    project: str,
    deployment: str = "wandb_cloud",
    project_id: str = "environment-integration",
) -> ObservabilityBinding:
    """Fail closed unless environment credentials resolve an exact Restricted scope."""

    policy = ProjectDataPolicy(
        project_id=project_id,
        classification="standard_engineering",
        deployment=deployment,
        acknowledged_by="ci",
    )
    candidate = ObservabilityBinding(
        project_id=project_id,
        data_policy_id=policy.policy_id,
        entity=entity,
        external_project=project,
        external_project_id="pending-validation",
        api_base_url=os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai"),
        deployment=deployment,
        mode="online",
    )
    return WandbAdapter().validate_binding(candidate, policy)


def validate_and_probe_from_environment(
    *,
    entity: str,
    project: str,
    deployment: str = "wandb_cloud",
) -> dict[str, Any]:
    """Validate Restricted access, then perform a tiny hash roundtrip for CI."""

    verified = validate_binding_from_environment(
        entity=entity,
        project=project,
        deployment=deployment,
        project_id="ci-integration-probe",
    )
    adapter = WandbAdapter()
    payload = b"GI Scenario Studio W&B integration probe v2\n"
    digest = hashlib.sha256(payload).hexdigest()
    reference = adapter.roundtrip_probe(verified, payload, digest)
    return {
        "verified": verified.verified,
        "access": verified.access,
        "entity": verified.entity,
        "project": verified.external_project,
        "external_project_id": verified.external_project_id,
        "probe_sha256": digest,
        "run_id": reference.run_id,
        "url": reference.url,
        "artifact_ids": reference.artifact_ids,
    }
