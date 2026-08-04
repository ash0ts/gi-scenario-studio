from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.assistants import AssistantRunAssociation
from ecocorridor_core.observability import (
    ECOCORRIDOR_APP_PROFILE,
    EcoRpcObservabilityAdapter,
    SdkWeaveClientFactory,
    WeaveNestedTraceSink,
    derive_cloud_sync_state,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.telemetry import build_telemetry_projection_inventory
from scenario_platform.cloud_sync import (
    ArtifactProjection,
    CloudArtifactReference,
    CloudIntegrityError,
    CloudPolicyError,
    CloudTransportError,
    CloudUploadReceipt,
    DurableOutboxConsumer,
    InMemoryCredentialSession,
    ProjectCloudDirectories,
    TableProjection,
    TelemetryProjection,
    WandbSdkLineageUploader,
    WandbTarget,
)
from scenario_platform.observability import (
    ProjectScope,
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
    UnsupportedWandbCloudScopeProvider,
    redact_sensitive,
)
from scenario_platform.repository import ScenarioRepository


class _ExactScopeProvider:
    def resolve_exact_scope(self, *, entity: str, project: str, deployment: str, api_base_url: str):
        return ProjectScopeEvidence(
            entity=entity,
            project=project,
            external_project_id="provider-project-id",
            deployment=deployment,
            api_base_url=api_base_url,
            scope=ProjectScope.RESTRICTED,
            evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
            provider_field="documented_admin.visibility_scope",
        )


def _resolver() -> ProjectScopeResolver:
    return ProjectScopeResolver(_ExactScopeProvider())


def _target() -> WandbTarget:
    evidence = _resolver().resolve(
        entity="wildlife-team",
        project="ridgeway",
        deployment="DEDICATED",
        api_base_url="https://api.wandb.example",
    )
    return WandbTarget(
        entity=evidence.entity,
        project=evidence.project,
        external_project_id=evidence.external_project_id,
        deployment=evidence.deployment,
        api_base_url="https://api.wandb.example",
        web_base_url="https://wandb.example",
        scope_evidence=evidence,
    )


def _authorized_sync_plan(root: Path) -> dict[str, Any]:
    store = EcoProjectStore(root)
    try:
        bundle = store.effective_bundle()
        fixture = build_ridgeway_fixture()
        for asset in bundle.assets:
            target = root / asset.relative_path
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    json.dumps(
                        fixture.rasters[asset.asset_id].values,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
        plan = {
            "allowed": True,
            "blockers": [],
            "projections": [
                {
                    "source_asset_id": asset.asset_id,
                    "uploaded_asset_id": asset.asset_id,
                    "representation": "RAW",
                    "bytes": asset.byte_size,
                }
                for asset in bundle.assets
            ],
        }
        inventory = build_telemetry_projection_inventory(
            store=store,
            project_root=root,
            sync_plan=plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        )
    finally:
        store.close()
    return {
        **plan,
        "_telemetry_authority_sha256": inventory.authority_sha256,
        "_authorized_outbox_envelopes": [item.as_dict() for item in inventory.authorized_envelopes],
    }


def test_cloud_sync_state_is_derived_from_authoritative_outbox_rows() -> None:
    assert (
        derive_cloud_sync_state(
            binding_state="SYNC_QUEUED_OFFLINE",
            telemetry_rows=[{"state": "SYNCED"}],
        )[0]
        == "CONNECTED"
    )
    assert (
        derive_cloud_sync_state(
            binding_state="CONNECTED",
            telemetry_rows=[{"state": "PENDING"}],
        )[0]
        == "SYNC_QUEUED_OFFLINE"
    )
    assert (
        derive_cloud_sync_state(
            binding_state="CONNECTED",
            telemetry_rows=[{"state": "UPLOADING"}],
        )[0]
        == "SYNCING"
    )
    assert (
        derive_cloud_sync_state(
            binding_state="CONNECTED",
            telemetry_rows=[{"state": "FAILED"}],
        )[0]
        == "FAILED_RETRYING"
    )
    assert (
        derive_cloud_sync_state(
            binding_state="CONNECTED",
            telemetry_rows=[{"state": "BLOCKED"}],
        )[0]
        == "BLOCKED"
    )


def _trace_inputs() -> dict[str, Any]:
    association = AssistantRunAssociation.create(
        project_id="project-1",
        wandb_external_project_id="provider-project-id",
        wandb_run_id="ecocorridor-connectivity-analysis-analysis-1",
        wandb_run_kind="connectivity_analysis",
        run_subject_id="analysis-1",
        analysis_id="analysis-1",
    )
    return {
        "project_id": "project-1",
        "wandb_association": association.model_dump(mode="json"),
    }


def _projection(*, artifact: ArtifactProjection | None = None) -> TelemetryProjection:
    return TelemetryProjection(
        profile_namespace=ECOCORRIDOR_APP_PROFILE.namespace,
        project_id="project_1",
        run_kind="guided_session",
        run_subject_id="guided_1",
        event_type="guided_session.recorded",
        event_sequence=1,
        config={"fixture_sha256": "a" * 64},
        history={"checkpoint_count": 3},
        summary={"verified_fixture_replay": True},
        tables=[
            TableProjection(
                logical_name="guided_checkpoints",
                columns=["checkpoint", "completed"],
                rows=[["overview", True]],
            )
        ],
        input_artifact_refs=["wildlife-team/ridgeway/source-model:v2"],
        artifacts=[] if artifact is None else [artifact],
    )


class _RecordingUploader:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.uploads: list[tuple[str, str]] = []

    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence:
        assert target.scope_evidence.scope == ProjectScope.RESTRICTED
        return target.scope_evidence

    def upload(self, **values: Any) -> CloudUploadReceipt:
        if self.failures:
            self.failures -= 1
            raise CloudTransportError("WANDB_TEMPORARILY_UNAVAILABLE")
        self.uploads.append((values["envelope_id"], values["idempotency_key"]))
        return CloudUploadReceipt(
            run_id="ecocorridor-guided",
            run_url="https://wandb.example/run",
            envelope_id=values["envelope_id"],
        )


def _queued_repository(tmp_path: Path) -> tuple[ScenarioRepository, str]:
    repository = ScenarioRepository(tmp_path / "project.sqlite")
    repository.create_project("Ridgeway", project_id="project_1")
    _, outbox_id = repository.record_producer(
        "project_1",
        "guided_session",
        "guided_1",
        {"checkpoint": "overview"},
        telemetry_payload=_projection().model_dump(mode="json"),
        telemetry_idempotency_key="guided-session:guided_1",
    )
    return repository, outbox_id


def test_in_memory_credential_session_commits_success_and_rolls_back_failure() -> None:
    session = InMemoryCredentialSession(environment_key="TEST_PROVIDER_KEY")
    original = "original-secret-value-123"
    candidate = "candidate-secret-value-456"
    session_value = ""

    with session.candidate(original), session.environment():
        session_value = os.environ["TEST_PROVIDER_KEY"]
    assert session_value == original
    assert "TEST_PROVIDER_KEY" not in os.environ

    with (
        pytest.raises(RuntimeError, match="reject candidate"),
        session.candidate(candidate),
        session.environment(),
    ):
        assert os.environ["TEST_PROVIDER_KEY"] == candidate
        raise RuntimeError("reject candidate")

    with session.environment():
        assert os.environ["TEST_PROVIDER_KEY"] == original
    session.clear()
    assert not session.configured


def _outbox_state(repository: ScenarioRepository, outbox_id: str) -> dict[str, Any]:
    row = repository._connection.execute(
        "SELECT * FROM telemetry_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def test_locked_wandb_sdk_exposes_exact_run_lookup_contract() -> None:
    import wandb
    from scenario_platform import cloud_sync
    from wandb.apis.normalize import normalize_exceptions
    from wandb.errors import CommError

    assert wandb.__version__ == "0.28.1"
    assert callable(wandb.Api.runs)
    assert callable(wandb.Api.artifact)

    reference = "wildlife-team/ridgeway/evidence:outbox-projection-abc"

    @normalize_exceptions
    def missing_membership() -> None:
        raise ValueError(
            "artifact membership 'evidence:outbox-projection-abc' not found in "
            "'wildlife-team/ridgeway'"
        )

    with pytest.raises(CommError) as missing:
        missing_membership()
    assert cloud_sync._is_exact_wandb_artifact_not_found(
        missing.value,
        reference=reference,
    )

    @normalize_exceptions
    def missing_project() -> None:
        raise ValueError("project 'ridgeway' not found under entity 'wildlife-team'")

    with pytest.raises(CommError) as ambiguous:
        missing_project()
    assert not cloud_sync._is_exact_wandb_artifact_not_found(
        ambiguous.value,
        reference=reference,
    )


def test_outbox_stays_durable_offline_and_upload_is_idempotent(tmp_path: Path) -> None:
    repository, outbox_id = _queued_repository(tmp_path)
    uploader = _RecordingUploader()
    consumer = DurableOutboxConsumer(
        repository=repository,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=tmp_path,
    )

    offline = consumer.drain(online=False)
    assert offline.queued_offline
    assert _outbox_state(repository, outbox_id)["state"] == "PENDING"
    assert uploader.uploads == []

    first = consumer.drain(online=True)
    second = consumer.drain(online=True)
    assert (first.synced, second.synced) == (1, 0)
    assert len(uploader.uploads) == 1
    stored = _outbox_state(repository, outbox_id)
    assert stored["state"] == "SYNCED"
    assert stored["attempt_count"] == 1
    repository.close()


def test_retryable_upload_failure_retries_same_idempotency_key(tmp_path: Path) -> None:
    repository, outbox_id = _queued_repository(tmp_path)
    uploader = _RecordingUploader(failures=1)
    consumer = DurableOutboxConsumer(
        repository=repository,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=tmp_path,
    )

    failed = consumer.drain(online=True)
    assert failed.failed == 1
    assert _outbox_state(repository, outbox_id)["state"] == "FAILED"
    succeeded = consumer.drain(online=True)
    assert succeeded.synced == 1
    row = _outbox_state(repository, outbox_id)
    assert row["state"] == "SYNCED"
    assert row["attempt_count"] == 2
    assert uploader.uploads == [(outbox_id, "guided-session:guided_1")]
    repository.close()


def test_outbox_hash_tampering_blocks_before_provider_upload(tmp_path: Path) -> None:
    repository, outbox_id = _queued_repository(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="telemetry evidence is immutable"):
        repository._connection.execute(
            "UPDATE telemetry_outbox SET payload_json='{}' WHERE outbox_id=?", (outbox_id,)
        )
    row = _outbox_state(repository, outbox_id)
    assert row["state"] == "PENDING"
    repository.close()


def test_project_cloud_directories_are_namespaced_and_restore_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WANDB_MODE", "disabled")
    directories = ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE)
    assert directories.root == tmp_path / ".scenario-cloud" / "ecocorridor"
    assert all(path.is_dir() for path in (directories.run, directories.cache, directories.offline))
    with directories.wandb_environment(base_url="https://api.wandb.example", online=False):
        assert Path(os.environ["WANDB_DIR"]).is_relative_to(tmp_path)
        assert os.environ["WANDB_MODE"] == "offline"
    assert os.environ["WANDB_MODE"] == "disabled"


def test_project_cloud_directories_reject_symlink_escape_before_writing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".scenario-cloud").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        ProjectCloudDirectories.create(project, ECOCORRIDOR_APP_PROFILE)
    assert not (outside / ECOCORRIDOR_APP_PROFILE.namespace).exists()


def test_project_cloud_directories_reject_symlinked_projection_receipt_parent(
    tmp_path: Path,
) -> None:
    directories = ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE)
    outside = tmp_path / "outside-projections"
    outside.mkdir()
    directories.telemetry_projections.rmdir()
    directories.telemetry_projections.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE)
    assert list(outside.iterdir()) == []


class _FakeTable:
    def __init__(self, *, columns: list[str], data: list[list[Any]]) -> None:
        self.columns = columns
        self.data = data


class _FakeArtifact:
    def __init__(self, *, name: str, type: str, metadata: dict[str, Any]) -> None:
        self.name = name
        self.type = type
        self.metadata = metadata
        self.files: list[tuple[Path, str]] = []

    def add_file(self, path: str, *, name: str) -> None:
        self.files.append((Path(path), name))


class _LoggedArtifact:
    def __init__(self, artifact: _FakeArtifact, *, corrupt: bool, url: str = "") -> None:
        self.artifact = artifact
        self.id = f"provider-{artifact.name}-v0"
        self.name = artifact.name
        self.version = "v0"
        self.qualified_name = f"wildlife-team/ridgeway/{artifact.name}:v0"
        self.corrupt = corrupt
        self.url = url
        self.waited = False

    @property
    def type(self) -> str:
        return self.artifact.type

    @property
    def metadata(self) -> dict[str, Any]:
        return self.artifact.metadata

    def wait(self) -> None:
        self.waited = True

    def download(self, *, root: str, skip_cache: bool) -> str:
        assert self.waited and skip_cache
        destination = Path(root)
        for source, name in self.artifact.files:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"corrupt" if self.corrupt else source.read_bytes())
        return str(destination)


class _FakeHttpSummary:
    """Dict-like SDK shape that intentionally is not a Mapping."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def items(self):
        return self.values.items()


class _FakeCommError(RuntimeError):
    def __init__(self, message: str, exc: BaseException) -> None:
        super().__init__(message)
        self.exc = exc


class _FakeRun:
    def __init__(
        self,
        values: dict[str, Any],
        *,
        corrupt: bool,
        sdk: _FakeWandb,
        run_url: str | None = None,
        artifact_url: str = "",
    ) -> None:
        self.values = values
        self.id = values["id"]
        self.url = run_url or f"https://wandb.example/run/{self.id}"
        self.summary = sdk.run_summaries.setdefault(self.id, {})
        self.logged: list[tuple[dict[str, Any], int]] = []
        self.used: list[str] = []
        self.linked: list[tuple[str, str]] = []
        self.corrupt = corrupt
        self.sdk = sdk
        self.artifact_url = artifact_url
        self.finished = False
        self.logged_artifacts: list[_LoggedArtifact] = []

    def log(self, values: dict[str, Any], *, step: int) -> None:
        self.logged.append((values, step))

    def use_artifact(self, reference: str) -> None:
        self.used.append(reference)

    def log_artifact(self, artifact: _FakeArtifact, *, aliases: list[str]) -> _LoggedArtifact:
        logged = _LoggedArtifact(
            artifact,
            corrupt=self.corrupt,
            url=self.artifact_url,
        )
        self.logged_artifacts.append(logged)
        for alias in aliases:
            reference = f"wildlife-team/ridgeway/{artifact.name}:{alias}"
            self.sdk.artifacts[reference] = logged
        return logged

    def link_artifact(self, artifact: _LoggedArtifact, *, target_path: str) -> None:
        self.linked.append((artifact.id, target_path))

    def finish(self) -> None:
        self.finished = True
        self.sdk.remote_runs[self.id] = types.SimpleNamespace(
            id=self.id,
            name=self.id,
            url=self.url,
            summary=_FakeHttpSummary(self.summary),
            summary_metrics=self.summary,
        )
        self.sdk.visibility_delays[self.id] = self.sdk.visibility_delay_after_finish
        self.sdk.finish_count += 1
        if self.sdk.finish_count in self.sdk.fail_finish_calls:
            raise RuntimeError("simulated finish acknowledgement loss")


class _FakeWandb:
    Settings = staticmethod(lambda **values: values)
    Table = _FakeTable
    Artifact = _FakeArtifact

    def __init__(
        self,
        *,
        corrupt: bool = False,
        run_url: str | None = None,
        artifact_url: str = "",
        lookup_error: bool = False,
        run_lookup_error: bool = False,
        mismatched_run_lookup: bool = False,
        missing_run_id: bool = False,
        ambiguous_run_lookup: bool = False,
        visibility_delay_after_finish: int = 0,
        artifact_lookup_error: BaseException | None = None,
        fail_finish_calls: set[int] | None = None,
    ) -> None:
        self.corrupt = corrupt
        self.run_url = run_url
        self.artifact_url = artifact_url
        self.lookup_error = lookup_error
        self.run_lookup_error = run_lookup_error
        self.mismatched_run_lookup = mismatched_run_lookup
        self.missing_run_id = missing_run_id
        self.ambiguous_run_lookup = ambiguous_run_lookup
        self.visibility_delay_after_finish = visibility_delay_after_finish
        self.artifact_lookup_error = artifact_lookup_error
        self.fail_finish_calls = set(fail_finish_calls or ())
        self.finish_count = 0
        self.runs: list[_FakeRun] = []
        self.artifacts: dict[str, _LoggedArtifact] = {}
        self.run_summaries: dict[str, dict[str, Any]] = {}
        self.remote_runs: dict[str, Any] = {}
        self.visibility_delays: dict[str, int] = {}
        self.api_instances: list[_FakePublicApi] = []

    def Api(self, **values: Any) -> _FakePublicApi:
        assert values["overrides"]["base_url"] == "https://api.wandb.example"
        api = _FakePublicApi(self)
        self.api_instances.append(api)
        return api

    def init(self, **values: Any) -> _FakeRun:
        run = _FakeRun(
            values,
            corrupt=self.corrupt,
            sdk=self,
            run_url=self.run_url,
            artifact_url=self.artifact_url,
        )
        self.runs.append(run)
        return run


class _FakePublicApi:
    def __init__(self, sdk: _FakeWandb) -> None:
        self.sdk = sdk
        # Match the real SDK's per-Public-API-instance read cache. A checker
        # that reuses this instance cannot observe a later summary write.
        self.run_snapshot = dict(sdk.remote_runs)

    def artifact_exists(self, reference: str, *, type: str) -> bool:
        if self.sdk.lookup_error:
            raise RuntimeError("provider lookup failed")
        artifact = self.sdk.artifacts.get(reference)
        return artifact is not None and artifact.type == type

    def artifact(self, reference: str, *, type: str) -> _LoggedArtifact:
        if self.sdk.artifact_lookup_error is not None:
            raise self.sdk.artifact_lookup_error
        artifact = self.sdk.artifacts.get(reference)
        if artifact is None:
            entity, project, membership = reference.split("/", 2)
            nested = ValueError(
                f"artifact membership {membership!r} not found in {f'{entity}/{project}'!r}"
            )
            raise _FakeCommError(str(nested), nested)
        assert artifact.type == type
        return artifact

    def runs(
        self,
        path: str,
        *,
        filters: dict[str, Any],
        per_page: int,
        lazy: bool,
    ) -> list[Any]:
        assert path == "wildlife-team/ridgeway"
        assert per_page == 2
        assert lazy is False
        if self.sdk.run_lookup_error:
            raise RuntimeError("provider run lookup failed")
        run_id = filters["name"]
        found = self.run_snapshot.get(run_id)
        if found is None:
            return []
        remaining = self.sdk.visibility_delays.get(run_id, 0)
        if remaining > 0:
            self.sdk.visibility_delays[run_id] = remaining - 1
            return []
        if self.sdk.mismatched_run_lookup:
            return [types.SimpleNamespace(**{**vars(found), "id": "different-run"})]
        if self.sdk.missing_run_id:
            values = {**vars(found), "name": run_id}
            values.pop("id", None)
            return [types.SimpleNamespace(**values)]
        if self.sdk.ambiguous_run_lookup:
            return [found, found]
        return [found]


def _artifact(tmp_path: Path) -> ArtifactProjection:
    source = tmp_path / "blobs" / "matrix.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'{"matrix":"direct"}\n')
    return ArtifactProjection(
        local_artifact_id="matrix_1",
        artifact_type="direct_run_batch",
        logical_id="analysis_1_batch_1",
        logical_name="runs/matrix.json",
        source_relative_path="blobs/matrix.json",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        bytes=source.stat().st_size,
        media_type="application/json",
        registry_alias="release",
    )


def test_real_sdk_boundary_logs_tables_lineage_and_hash_verified_artifact(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    receipt = uploader.upload(
        envelope_id="outbox_1",
        idempotency_key="guided-session:guided_1",
        projection=_projection(artifact=manifest),
        target=_target(),
        project_root=tmp_path,
        directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    )

    assert len(sdk.runs) == 2
    assert len(sdk.api_instances) >= 5
    assert len({id(api) for api in sdk.api_instances}) == len(sdk.api_instances)
    run = sdk.runs[1]
    remote = sdk.remote_runs[run.id]
    assert not isinstance(remote.summary, Mapping)
    assert isinstance(remote.summary_metrics, dict)
    assert sdk.runs[0].logged == []
    assert sdk.runs[0].used == []
    assert run.values["resume"] == "allow"
    assert run.values["mode"] == "online"
    assert run.used == ["wildlife-team/ridgeway/source-model:v2"]
    table = run.logged[0][0]["ecocorridor_guided_checkpoints"]
    assert table.data == [["overview", True]]
    assert run.linked == [
        (
            receipt.artifacts[0].provider_artifact_id,
            ECOCORRIDOR_APP_PROFILE.registry_name("release"),
        )
    ]
    assert receipt.artifacts == [
        CloudArtifactReference(
            local_artifact_id="matrix_1",
            provider_artifact_id=receipt.artifacts[0].provider_artifact_id,
            provider_artifact_ref=receipt.artifacts[0].provider_artifact_ref,
            sha256=manifest.sha256,
            registry_target=ECOCORRIDOR_APP_PROFILE.registry_name("release"),
        )
    ]
    assert run.finished


def test_sdk_boundary_reuses_exact_committed_artifact_after_receipt_crash(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-crash-window",
        "idempotency_key": "analysis:direct-evidence",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    first = uploader.upload(**values)
    second = uploader.upload(**values)

    assert len(sdk.runs) == 2
    assert sum(len(run.logged) for run in sdk.runs) == 1
    assert sum(run.used.count("wildlife-team/ridgeway/source-model:v2") for run in sdk.runs) == 1
    assert sum(len(run.logged_artifacts) for run in sdk.runs) == 2
    assert sum(len(run.linked) for run in sdk.runs) == 1
    assert second.artifacts == first.artifacts


@pytest.mark.parametrize("mismatch_field", ["projection_sha256", "target_sha256"])
def test_sdk_boundary_does_not_trust_mutated_summary_acknowledgement(
    tmp_path: Path,
    mismatch_field: str,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-idempotency",
        "idempotency_key": "analysis:direct-evidence",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }
    uploader.upload(**values)
    run = sdk.runs[-1]
    acknowledgement_key = next(key for key in run.summary if "_outbox_ack_" in key)
    run.summary[acknowledgement_key]["intent"][mismatch_field] = "f" * 64
    recovered = uploader.upload(**values)
    assert recovered.projection_artifact is not None
    assert len(sdk.runs) == 2


def test_sdk_boundary_treats_acknowledgement_lookup_error_as_ambiguous_before_writes(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb(run_lookup_error=True)
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )

    with pytest.raises(CloudTransportError, match="ACKNOWLEDGEMENT_LOOKUP_AMBIGUOUS"):
        uploader.upload(
            envelope_id="outbox-lookup-error",
            idempotency_key="analysis:other",
            projection=_projection(artifact=manifest),
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs == []
    assert sdk.artifacts == {}


def test_sdk_boundary_recovers_after_intent_only_transport_failure(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb(fail_finish_calls={1})
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-partial-write",
        "idempotency_key": "analysis:partial-write",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    with pytest.raises(CloudTransportError, match="WRITE_INTENT_FINISH_FAILED"):
        uploader.upload(**values)
    sdk.fail_finish_calls.clear()
    receipt = uploader.upload(**values)

    assert receipt.projection_artifact is not None
    assert len(sdk.runs) == 3
    assert sdk.runs[0].logged == []
    assert sdk.runs[0].used == []
    assert sdk.runs[0].logged_artifacts == []
    assert sdk.runs[0].linked == []


def test_sdk_boundary_recovers_completed_acknowledgement_after_local_receipt_crash(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb(fail_finish_calls={2})
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-acknowledged-crash",
        "idempotency_key": "analysis:acknowledged-crash",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    with pytest.raises(CloudTransportError, match="WANDB_RUN_FINISH_FAILED"):
        uploader.upload(**values)
    sdk.fail_finish_calls.clear()
    receipt = uploader.upload(**values)

    assert receipt.envelope_id == "outbox-acknowledged-crash"
    assert len(sdk.runs) == 2
    assert sum(len(run.logged) for run in sdk.runs) == 1
    assert sum(run.used.count("wildlife-team/ridgeway/source-model:v2") for run in sdk.runs) == 1
    assert sum(len(run.logged_artifacts) for run in sdk.runs) == 2
    assert sum(len(run.linked) for run in sdk.runs) == 1


def test_sdk_boundary_uses_projection_receipt_before_delayed_run_summary_replay(
    tmp_path: Path,
) -> None:
    sdk = _FakeWandb(fail_finish_calls={2}, visibility_delay_after_finish=2)
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-delayed-summary-crash",
        "idempotency_key": "analysis:delayed-summary-crash",
        "projection": _projection(),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    with pytest.raises(CloudTransportError, match="WANDB_RUN_FINISH_FAILED"):
        uploader.upload(**values)
    sdk.fail_finish_calls.clear()
    receipt = uploader.upload(**values)

    assert receipt.projection_artifact is not None
    assert len(sdk.runs) == 2
    assert sum(len(run.logged) for run in sdk.runs) == 1
    assert sum(len(run.logged_artifacts) for run in sdk.runs) == 1


def test_sdk_boundary_reconstructs_receipt_instead_of_trusting_summary_copy(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-forged-artifact-receipt",
        "idempotency_key": "analysis:forged-artifact-receipt",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    uploader.upload(**values)
    run = sdk.runs[-1]
    acknowledgement_key = next(key for key in run.summary if "_outbox_ack_" in key)
    receipt = run.summary[acknowledgement_key]["receipt"]
    receipt["artifacts"][0]["provider_artifact_id"] = "provider-forged-v0"

    recovered = uploader.upload(**values)
    assert recovered.artifacts[0].provider_artifact_id != "provider-forged-v0"
    assert len(sdk.runs) == 2


def test_sdk_boundary_revalidates_acknowledged_artifact_bytes(tmp_path: Path) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-corrupt-acknowledged-artifact",
        "idempotency_key": "analysis:corrupt-acknowledged-artifact",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    uploader.upload(**values)
    sdk.runs[-1].logged_artifacts[0].corrupt = True

    with pytest.raises(CloudIntegrityError, match="ARTIFACT_DOWNLOAD_HASH_MISMATCH"):
        uploader.upload(**values)
    assert len(sdk.runs) == 2


def test_sdk_boundary_rejects_acknowledgement_when_artifact_alias_disappears(
    tmp_path: Path,
) -> None:
    manifest = _artifact(tmp_path)
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-missing-acknowledged-artifact",
        "idempotency_key": "analysis:missing-acknowledged-artifact",
        "projection": _projection(artifact=manifest),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    uploader.upload(**values)
    sdk.artifacts.clear()

    with pytest.raises(
        CloudIntegrityError,
        match="ACKNOWLEDGEMENT_PROJECTION_ARTIFACT_MISSING",
    ):
        uploader.upload(**values)
    assert len(sdk.runs) == 2


def test_sdk_boundary_projection_receipt_authorizes_no_output_artifact_retry(
    tmp_path: Path,
) -> None:
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    values = {
        "envelope_id": "outbox-history-only",
        "idempotency_key": "analysis:history-only",
        "projection": _projection(),
        "target": _target(),
        "project_root": tmp_path,
        "directories": ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    }

    first = uploader.upload(**values)
    second = uploader.upload(**values)

    assert first.projection_artifact is not None
    assert second == first
    assert first.artifacts == []
    assert sum(len(run.logged) for run in sdk.runs) == 1
    assert sum(len(run.logged_artifacts) for run in sdk.runs) == 1


def test_sdk_boundary_polls_bounded_fresh_clients_for_delayed_visibility(
    tmp_path: Path,
) -> None:
    sdk = _FakeWandb(visibility_delay_after_finish=2)
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )

    receipt = uploader.upload(
        envelope_id="outbox-delayed-readback",
        idempotency_key="analysis:delayed-readback",
        projection=_projection(),
        target=_target(),
        project_root=tmp_path,
        directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
    )

    assert receipt.projection_artifact is not None
    assert len(sdk.api_instances) >= 8


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_sdk_boundary_never_treats_http_lookup_failure_as_artifact_absence(
    tmp_path: Path,
    status: int,
) -> None:
    response = types.SimpleNamespace(status_code=status)
    nested = RuntimeError(f"HTTP {status}")
    nested.response = response  # type: ignore[attr-defined]
    sdk = _FakeWandb(artifact_lookup_error=_FakeCommError(str(nested), nested))
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )

    with pytest.raises(CloudTransportError, match="IDEMPOTENCY_LOOKUP_AMBIGUOUS"):
        uploader.upload(
            envelope_id=f"outbox-http-{status}",
            idempotency_key=f"analysis:http-{status}",
            projection=_projection(),
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs == []


def test_sdk_boundary_rejects_local_and_provider_hash_mismatch(tmp_path: Path) -> None:
    manifest = _artifact(tmp_path)
    source = tmp_path / manifest.source_relative_path
    source.write_bytes(b"changed")
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    with pytest.raises(CloudIntegrityError, match="SOURCE_HASH_MISMATCH"):
        uploader.upload(
            envelope_id="outbox_1",
            idempotency_key="one",
            projection=_projection(artifact=manifest),
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs == []

    manifest = _artifact(tmp_path)
    corrupt_sdk = _FakeWandb(corrupt=True)
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=corrupt_sdk,
    )
    with pytest.raises(CloudIntegrityError, match="DOWNLOAD_HASH_MISMATCH"):
        uploader.upload(
            envelope_id="outbox_2",
            idempotency_key="two",
            projection=_projection(artifact=manifest),
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert corrupt_sdk.runs[0].finished


def test_sdk_boundary_rejects_sensitive_metadata_before_starting_run(tmp_path: Path) -> None:
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    projection = _projection()
    projection.summary = {"authorization_header": "Bearer must-not-upload"}
    with pytest.raises(CloudPolicyError, match="REDACTION_REQUIRED"):
        uploader.upload(
            envelope_id="outbox_sensitive",
            idempotency_key="sensitive",
            projection=projection,
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs == []


@pytest.mark.parametrize(
    ("sdk", "with_artifact"),
    [
        (_FakeWandb(artifact_url="https://attacker.example/artifact/escaped"), True),
    ],
)
def test_sdk_boundary_rejects_provider_links_outside_verified_target(
    tmp_path: Path,
    sdk: _FakeWandb,
    with_artifact: bool,
) -> None:
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=_resolver(),
        sdk=sdk,
    )
    projection = _projection(artifact=_artifact(tmp_path) if with_artifact else None)
    with pytest.raises(CloudPolicyError, match="URL_OUTSIDE_VERIFIED_TARGET"):
        uploader.upload(
            envelope_id="outbox-provider-url",
            idempotency_key="provider-url",
            projection=projection,
            target=_target(),
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs[0].finished


def test_provider_contracts_reject_query_bearing_api_web_run_and_artifact_urls() -> None:
    evidence_payload = _target().scope_evidence.model_dump(mode="json")
    evidence_payload["api_base_url"] = "https://api.wandb.example?api_key=must-not-persist"
    with pytest.raises(ValueError, match="API base URL|scope-evidence API base URL"):
        ProjectScopeEvidence.model_validate(evidence_payload)

    target_payload = _target().model_dump(mode="json")
    target_payload["web_base_url"] = "https://wandb.example?token=must-not-persist"
    with pytest.raises(ValueError, match="web base URL"):
        WandbTarget.model_validate(target_payload)

    with pytest.raises(ValueError, match="credential-free"):
        CloudUploadReceipt(
            run_id="run-1",
            run_url="https://wandb.example/run/run-1?token=must-not-persist",
            envelope_id="outbox-1",
        )

    with pytest.raises(ValueError, match="credential-free"):
        CloudArtifactReference(
            local_artifact_id="artifact-1",
            provider_artifact_id="provider-artifact-1",
            provider_artifact_ref="wildlife-team/ridgeway/artifact:v1",
            provider_artifact_url=(
                "https://wandb.example/artifacts/artifact-1?api_key=must-not-persist"
            ),
            sha256="a" * 64,
        )


def test_redaction_detects_arbitrary_absolute_paths_and_inline_credentials() -> None:
    payload = {
        "note": "read /Volumes/ecology/private/model.tif",
        "detail": "api_key=must-not-upload",
        "header": "Bearer must-not-upload-either",
        "safe_url": "https://wandb.example/project/run",
    }
    redacted = redact_sensitive(payload)
    assert redacted["note"] == "read [LOCAL_PATH]"
    assert redacted["detail"] == "[REDACTED]"
    assert redacted["header"] == "[REDACTED]"
    assert redacted["safe_url"] == payload["safe_url"]


def test_cloud_scope_unverifiable_stops_before_sdk_use(tmp_path: Path) -> None:
    sdk = _FakeWandb()
    uploader = WandbSdkLineageUploader(
        profile=ECOCORRIDOR_APP_PROFILE,
        scope_resolver=ProjectScopeResolver(UnsupportedWandbCloudScopeProvider()),
        sdk=sdk,
    )
    target_payload = _target().model_dump(mode="json")
    target_payload["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["evidence_kind"] = "PUBLIC_API_DOCUMENTED_EXACT"
    target = WandbTarget.model_validate(target_payload)
    with pytest.raises(CloudPolicyError, match="SCOPE_UNVERIFIABLE"):
        uploader.upload(
            envelope_id="outbox_1",
            idempotency_key="one",
            projection=_projection(),
            target=target,
            project_root=tmp_path,
            directories=ProjectCloudDirectories.create(tmp_path, ECOCORRIDOR_APP_PROFILE),
        )
    assert sdk.runs == []


def test_rpc_adapter_drains_authoritative_store_and_rechecks_scope(tmp_path: Path) -> None:
    root = tmp_path / "eco-project"
    root.mkdir()
    fixture = build_ridgeway_fixture()
    repository = EcoProjectStore(root)
    repository.initialize(
        project_id=fixture.bundle.project_id,
        title="Ridgeway",
        mode="GUIDED",
        skeleton={"fixture_version": fixture.fixture_version},
    )
    repository.store_bundle(fixture.bundle)
    bundle_outbox = repository.claim_outbox(limit=1)[0]
    bundle_reference = CloudArtifactReference(
        local_artifact_id="bundle:ridgeway",
        provider_artifact_id="provider-bundle-v1",
        provider_artifact_ref="wildlife-team/ridgeway/ecocorridor-connectivity-project:v1",
        provider_artifact_url="https://wandb.example/artifacts/bundle-v1",
        sha256="a" * 64,
    )
    repository.mark_outbox_synced(
        bundle_outbox["outbox_id"],
        CloudUploadReceipt(
            run_id="bundle-run",
            run_url="https://wandb.example/run/bundle",
            envelope_id=bundle_outbox["outbox_id"],
            artifacts=[bundle_reference],
        ).model_dump(mode="json"),
    )
    repository.set_guided("guided_1", "VERIFIED_REPLAY", {"verified_fixture_replay": True})
    outbox_id = repository.pending_outbox()[0]["outbox_id"]
    repository.close()
    uploader = _RecordingUploader()
    target = _target()
    binding = {
        "entity": target.entity,
        "project": target.project,
        "external_project_id": target.external_project_id,
        "deployment": target.deployment,
        "api_base_url": target.api_base_url,
        "web_base_url": target.web_base_url,
        "scope_evidence": target.scope_evidence.model_dump(mode="json"),
        "state": "CONNECTED",
    }
    repository = EcoProjectStore(root)
    repository.set_observability(binding)
    repository.close()
    adapter = EcoRpcObservabilityAdapter(
        scope_resolver=_resolver(),
        store_factory=EcoProjectStore,
        uploader=uploader,
    )
    with adapter.credentials.candidate("not-a-real-wandb-key-value"):
        pass

    assert adapter.validate(binding=binding)["verified"] is True
    assert adapter.open_link(project_directory=root, kind="run", reference_id="bundle-run") == {
        "url": "https://wandb.example/run/bundle"
    }
    assert adapter.open_link(
        project_directory=root,
        kind="artifact",
        reference_id="provider-bundle-v1",
    ) == {"url": "https://wandb.example/artifacts/bundle-v1"}
    with pytest.raises(RuntimeError, match="REFERENCE_NOT_RESOLVED"):
        adapter.open_link(
            project_directory=root,
            kind="run",
            reference_id="renderer-invented-run",
        )
    result = adapter.sync(
        project_directory=root,
        binding=binding,
        sync_plan=_authorized_sync_plan(root),
    )
    assert result["synced"] == 1
    assert result["remaining"] == 0
    assert result["sync_state"] == "CONNECTED"
    reopened = EcoProjectStore(root)
    row = next(item for item in reopened.telemetry_rows() if item["outbox_id"] == outbox_id)
    assert row["state"] == "SYNCED"
    reopened.close()


def test_rpc_adapter_offline_sync_never_claims_or_requires_credentials(tmp_path: Path) -> None:
    root = tmp_path / "offline-eco-project"
    root.mkdir()
    fixture = build_ridgeway_fixture()
    repository = EcoProjectStore(root)
    repository.initialize(
        project_id=fixture.bundle.project_id,
        title="Ridgeway",
        mode="GUIDED",
        skeleton={"fixture_version": fixture.fixture_version},
    )
    repository.store_bundle(fixture.bundle)
    target = _target()
    binding = {
        "entity": target.entity,
        "project": target.project,
        "external_project_id": target.external_project_id,
        "deployment": target.deployment,
        "api_base_url": target.api_base_url,
        "web_base_url": target.web_base_url,
        "scope_evidence": target.scope_evidence.model_dump(mode="json"),
        "state": "SYNC_QUEUED_OFFLINE",
    }
    repository.set_observability(binding)
    pending_ids = [item["outbox_id"] for item in repository.pending_outbox()]
    repository.close()
    uploader = _RecordingUploader()
    adapter = EcoRpcObservabilityAdapter(
        scope_resolver=_resolver(),
        store_factory=EcoProjectStore,
        uploader=uploader,
    )

    report = adapter.sync(
        project_directory=root,
        binding=binding,
        sync_plan=_authorized_sync_plan(root),
        online=False,
    )

    assert report == {
        "online": False,
        "claimed": 0,
        "synced": 0,
        "failed": 0,
        "blocked": 0,
        "queued_offline": True,
        "sync_state": "SYNC_QUEUED_OFFLINE",
        "remaining": len(pending_ids),
        "state_counts": {"PENDING": len(pending_ids)},
    }
    assert uploader.uploads == []
    reopened = EcoProjectStore(root)
    assert [item["outbox_id"] for item in reopened.pending_outbox()] == pending_ids
    reopened.close()


def test_rpc_adapter_offline_state_clears_a_stale_badge_when_outbox_is_synced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "offline-synced-eco-project"
    root.mkdir()
    fixture = build_ridgeway_fixture()
    repository = EcoProjectStore(root)
    repository.initialize(
        project_id=fixture.bundle.project_id,
        title="Ridgeway",
        mode="GUIDED",
        skeleton={"fixture_version": fixture.fixture_version},
    )
    repository.store_bundle(fixture.bundle)
    for row in repository.claim_outbox(limit=100):
        repository.mark_outbox_synced(
            str(row["outbox_id"]),
            {"run_id": "verified-run", "envelope_id": str(row["outbox_id"])},
            lease_token=str(row["lease_token"]),
        )
    target = _target()
    binding = {
        "entity": target.entity,
        "project": target.project,
        "external_project_id": target.external_project_id,
        "deployment": target.deployment,
        "api_base_url": target.api_base_url,
        "web_base_url": target.web_base_url,
        "scope_evidence": target.scope_evidence.model_dump(mode="json"),
        "state": "BLOCKED",
    }
    repository.set_observability(binding)
    synced_count = len(repository.telemetry_rows())
    repository.close()

    report = EcoRpcObservabilityAdapter(
        scope_resolver=_resolver(),
        store_factory=EcoProjectStore,
        uploader=_RecordingUploader(),
    ).sync(
        project_directory=root,
        binding=binding,
        sync_plan=_authorized_sync_plan(root),
        online=False,
    )

    assert report["sync_state"] == "CONNECTED"
    assert report["remaining"] == 0
    assert report["state_counts"] == {"SYNCED": synced_count}


def test_trace_deep_link_requires_retained_provider_readback_url(tmp_path: Path) -> None:
    root = tmp_path / "trace-link-project"
    root.mkdir()
    target = _target()
    binding = {
        "entity": target.entity,
        "project": target.project,
        "external_project_id": target.external_project_id,
        "deployment": target.deployment,
        "api_base_url": target.api_base_url,
        "web_base_url": target.web_base_url,
        "scope_evidence": target.scope_evidence.model_dump(mode="json"),
        "state": "CONNECTED",
    }
    repository = EcoProjectStore(root)
    repository.initialize(
        project_id="trace-link-project",
        title="Trace link",
        mode="PLANNING",
        skeleton={"test": True},
    )
    repository.set_observability(binding)
    repository.store_assistant_answer(
        "assistant-call-1",
        {
            "call_id": "assistant-call-1",
            "trace_id": "trace-provider-1",
            "trace_url": "https://wandb.example/weave/traces/provider-call-1",
            "trace_readback_verified": True,
        },
        "trace-provider-1",
    )
    repository.close()
    adapter = EcoRpcObservabilityAdapter(
        scope_resolver=_resolver(),
        store_factory=EcoProjectStore,
        uploader=_RecordingUploader(),
    )

    assert adapter.open_link(
        project_directory=root,
        kind="trace",
        reference_id="trace-provider-1",
    ) == {"url": "https://wandb.example/weave/traces/provider-call-1"}
    with pytest.raises(RuntimeError, match="REFERENCE_NOT_RESOLVED"):
        adapter.open_link(
            project_directory=root,
            kind="trace",
            reference_id="trace-not-retained",
        )


def test_rpc_adapter_cannot_accept_fake_uploader_as_scope_substitute(tmp_path: Path) -> None:
    target_payload = _target().model_dump(mode="json")
    target_payload["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["evidence_kind"] = "PUBLIC_API_DOCUMENTED_EXACT"
    target = WandbTarget.model_validate(target_payload)
    adapter = EcoRpcObservabilityAdapter(
        scope_resolver=ProjectScopeResolver(UnsupportedWandbCloudScopeProvider()),
        store_factory=lambda directory: ScenarioRepository(directory / "telemetry.sqlite"),
        uploader=_RecordingUploader(),
    )
    with adapter.credentials.candidate("not-a-real-wandb-key-value"):
        pass
    with pytest.raises(RuntimeError, match="SCOPE_UNVERIFIABLE"):
        adapter.validate(
            binding={
                "entity": target.entity,
                "project": target.project,
                "external_project_id": target.external_project_id,
                "deployment": target.deployment,
                "api_base_url": target.api_base_url,
                "web_base_url": target.web_base_url,
                "scope_evidence": target.scope_evidence.model_dump(mode="json"),
            }
        )


class _FakeWeaveClient:
    def __init__(
        self,
        *,
        acknowledge: bool = True,
        corrupt_gate: bool = False,
        fail_flush_calls: set[int] | None = None,
        missing_get_call_numbers: set[int] | None = None,
    ) -> None:
        self.acknowledge = acknowledge
        self.corrupt_gate = corrupt_gate
        self.fail_flush_calls = set(fail_flush_calls or ())
        self.missing_get_call_numbers = set(missing_get_call_numbers or ())
        self.calls: dict[str, Any] = {}
        self.children: list[tuple[Any, Any]] = []
        self.flush_count = 0
        self.get_call_count = 0
        self.finish_counts: dict[str, int] = {}
        self.feedback_records: dict[str, Any] = {}
        self.wandb_run_id: str | None = None

    def set_wandb_run_context(self, *, run_id: str, step: int | None = None) -> None:
        assert step is None
        self.wandb_run_id = run_id

    def create_call(
        self,
        *,
        op: str,
        inputs: dict[str, Any],
        parent: Any = None,
        attributes: dict[str, Any] | None = None,
        use_stack: bool,
    ) -> Any:
        call_id = f"provider-call-{len(self.calls) + 1}"
        trace_id = parent.trace_id if parent is not None else f"trace-{call_id}"
        client = self

        class Feedback:
            def add(self, feedback_type: str, payload: dict[str, Any]) -> Any:
                feedback_id = f"feedback-{len(client.feedback_records) + 1}"
                record = types.SimpleNamespace(
                    id=feedback_id,
                    feedback_type=feedback_type,
                    payload=payload,
                )
                client.feedback_records[feedback_id] = record
                return record

        call = types.SimpleNamespace(
            id=call_id,
            trace_id=trace_id,
            op=op,
            inputs=inputs,
            attributes=attributes,
            output=None,
            ui_url=f"https://wandb.example/weave/traces/{trace_id}",
            feedback=Feedback(),
            wb_run_id=(
                f"wildlife-team/ridgeway/{self.wandb_run_id}" if self.wandb_run_id else None
            ),
        )
        self.calls[call_id] = call
        if parent is not None:
            self.children.append((parent, call))
        return call

    def finish_call(
        self, call: Any, *, output: Any, exception: BaseException | None = None
    ) -> None:
        call_id = str(call.id)
        self.finish_counts[call_id] = self.finish_counts.get(call_id, 0) + 1
        if self.finish_counts[call_id] != 1:
            raise RuntimeError("provider call cannot be finished twice")
        call.output = output
        call.exception = exception

    def flush(self) -> None:
        self.flush_count += 1
        if self.flush_count in self.fail_flush_calls:
            raise RuntimeError("simulated weave flush failure")

    def get_call(self, call_id: str) -> Any:
        self.get_call_count += 1
        if self.get_call_count in self.missing_get_call_numbers:
            return None
        if not self.acknowledge:
            return None
        call = self.calls[call_id]
        if self.corrupt_gate and isinstance(call.output, dict):
            copied = dict(call.output)
            copied["ecocorridor_trace_gate"] = {
                "call_id": "wrong",
                "completed": True,
                "steps": [],
            }
            return types.SimpleNamespace(
                id=call.id,
                trace_id=call.trace_id,
                wb_run_id=call.wb_run_id,
                output=copied,
            )
        return call

    def get_feedback(self, feedback_id: str) -> list[Any]:
        record = self.feedback_records.get(feedback_id)
        return [record] if record is not None else []


class _FakeWeaveFactory:
    def __init__(self, client: _FakeWeaveClient) -> None:
        self.client = client
        self.created = 0

    def create(self, target: WandbTarget) -> _FakeWeaveClient:
        assert target.scope_evidence.scope == ProjectScope.RESTRICTED
        self.created += 1
        return self.client


def test_weave_nested_trace_requires_flush_and_exact_readback(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.step(
        call_id="assistant_1",
        name="authorize_project",
        inputs={"project_id": "p"},
        output={"authorized": True},
    )
    sink.step(
        call_id="assistant_1",
        name="resolve_local_evidence",
        inputs={"evidence_ids": ["claim_1"]},
        output={"resolved": 1},
    )
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    trace_id = sink.flush(call_id="assistant_1")
    readback = sink.readback(trace_id=trace_id)

    assert readback.completed
    assert readback.association.analysis_id == "analysis-1"
    assert client.calls["provider-call-1"].wb_run_id.endswith(
        "/ecocorridor-connectivity-analysis-analysis-1"
    )
    assert readback.steps == ["authorize_project", "resolve_local_evidence"]
    assert len(client.children) == 2
    assert all(parent is client.calls["provider-call-1"] for parent, _ in client.children)
    assert client.flush_count == 1


def test_weave_nested_trace_redacts_identifiers_secrets_and_paths_before_client(
    tmp_path: Path,
) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    root_inputs = {
        **_trace_inputs(),
        "reviewer_contact": "field.team@example.org or 202-555-0182",
        "api_key": "trace-secret-must-not-leave",
    }
    sink.start(call_id="assistant_redacted", operation="assistant.request", inputs=root_inputs)
    sink.step(
        call_id="assistant_redacted",
        name="construct_context",
        inputs={"source": "/Users/example/private/report.txt"},
        output={"record": "123-45-6789"},
    )
    sink.finish(
        call_id="assistant_redacted",
        output={"contact": "field.team@example.org"},
    )

    encoded = json.dumps(
        {
            "root": client.calls["provider-call-1"].inputs,
            "root_output": client.calls["provider-call-1"].output,
            "children": [child.inputs for _, child in client.children],
            "child_outputs": [child.output for _, child in client.children],
        },
        sort_keys=True,
    )
    for raw in (
        "field.team@example.org",
        "202-555-0182",
        "123-45-6789",
        "trace-secret-must-not-leave",
        "/Users/example/private/report.txt",
    ):
        assert raw not in encoded
    assert "analysis-1" in encoded


@pytest.mark.parametrize(
    "local_call_id",
    [
        "field.team@example.org",
        "202-555-0182",
        "/Users/example/private/report.txt",
        "Bearer abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_weave_completed_trace_keeps_hostile_call_id_local(
    tmp_path: Path, local_call_id: str
) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id=local_call_id, operation="assistant.request", inputs=_trace_inputs())
    sink.step(
        call_id=local_call_id,
        name="authorize_project",
        inputs={},
        output={"authorized": True},
    )
    sink.finish(call_id=local_call_id, output={"answer_id": "answer-1"})
    trace_id = sink.flush(call_id=local_call_id)
    readback = sink.readback(trace_id=trace_id)

    root = client.calls["provider-call-1"]
    external_call_id = root.attributes["assistant_call_id"]
    assert external_call_id.startswith("ecocorridor-call-")
    assert root.output["ecocorridor_trace_gate"]["call_id"] == external_call_id
    assert all(
        child.attributes["assistant_call_id"] == external_call_id for _, child in client.children
    )
    assert readback.call_id == local_call_id
    assert local_call_id not in json.dumps(
        {"attributes": root.attributes, "output": root.output}, sort_keys=True
    )


def test_weave_factory_disables_unbundled_presidio_but_keeps_sensitive_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_settings: list[dict[str, Any]] = []
    observed_keys: list[str] = []
    fake_weave = types.ModuleType("weave")
    fake_utils = types.ModuleType("weave.utils")
    fake_utils.sanitize = types.SimpleNamespace(add_redact_key=observed_keys.append)

    def init(_project: str, *, settings: dict[str, Any]) -> object:
        observed_settings.append(settings)
        return object()

    fake_weave.init = init  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave", fake_weave)
    monkeypatch.setitem(sys.modules, "weave.utils", fake_utils)
    monkeypatch.setenv("WANDB_API_KEY", "test-only-key-not-sent")

    client = SdkWeaveClientFactory().create(_target())

    assert client is not None
    assert observed_settings == [{"redact_pii": False}]
    assert {"api_key", "authorization", "local_path", "secret", "token"}.issubset(observed_keys)


def test_weave_feedback_is_bounded_flushed_and_read_back(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    trace_id = sink.flush(call_id="assistant_1")
    sink.readback(trace_id=trace_id)
    receipt = sink.record_feedback(
        call_id="assistant_1",
        trace_id=trace_id,
        rating="HELPFUL",
        comment="The evidence links were useful.",
    )
    assert receipt["verified"] is True
    assert receipt["feedback_id"] in client.feedback_records
    assert client.flush_count == 2

    with pytest.raises(Exception, match="FEEDBACK_SENSITIVE_CONTENT"):
        sink.record_feedback(
            call_id="assistant_1",
            trace_id=trace_id,
            rating="HELPFUL",
            comment="api_key=must-not-replicate",
        )
    assert len(client.feedback_records) == 1


def test_weave_post_finish_flush_failure_never_refinishes_completed_root(
    tmp_path: Path,
) -> None:
    client = _FakeWeaveClient(fail_flush_calls={1})
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})

    with pytest.raises(RuntimeError, match="simulated weave flush failure"):
        sink.flush(call_id="assistant_1")
    sink.abort(call_id="assistant_1", error_code="ASSISTANT_TRACE_FLUSH_FAILED")

    root = client.calls["provider-call-1"]
    assert client.finish_counts[root.id] == 1
    assert root.output["ecocorridor_trace_gate"]["completed"] is True
    assert len(client.calls) == 1
    assert client.flush_count == 1
    with pytest.raises(RuntimeError, match="ABORT_TRACE_READBACK_UNAVAILABLE"):
        sink.aborted_readback(call_id="assistant_1")
    with pytest.raises(RuntimeError, match="TRACE_VERIFICATION_FAILED"):
        sink.flush(call_id="assistant_1")


def test_weave_second_readback_failure_never_refinishes_or_enables_feedback(
    tmp_path: Path,
) -> None:
    client = _FakeWeaveClient(missing_get_call_numbers={2})
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    trace_id = sink.flush(call_id="assistant_1")

    with pytest.raises(RuntimeError, match="TRACE_NOT_ACKNOWLEDGED"):
        sink.readback(trace_id=trace_id)
    sink.abort(call_id="assistant_1", error_code="ASSISTANT_TRACE_READBACK_FAILED")

    root = client.calls["provider-call-1"]
    assert client.finish_counts[root.id] == 1
    assert root.output["ecocorridor_trace_gate"]["completed"] is True
    assert len(client.calls) == 1
    assert client.flush_count == 1
    assert client.get_call_count == 2
    with pytest.raises(RuntimeError, match="ABORT_TRACE_READBACK_UNAVAILABLE"):
        sink.aborted_readback(call_id="assistant_1")
    with pytest.raises(RuntimeError, match="TRACE_FEEDBACK_NOT_READY"):
        sink.record_feedback(
            call_id="assistant_1",
            trace_id=trace_id,
            rating="HELPFUL",
            comment="This answer was not released.",
        )


def test_weave_aborted_trace_is_persisted_flushed_and_read_back(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_rejected", operation="assistant.request", inputs=_trace_inputs())
    sink.step(
        call_id="assistant_rejected",
        name="authorize_project",
        inputs={"project_id": "p"},
        output={"authorized": True},
    )
    sink.abort(
        call_id="assistant_rejected",
        error_code="ASSISTANT_PROMPT_INJECTION_REJECTED",
    )

    evidence = sink.aborted_readback(call_id="assistant_rejected")
    assert evidence["completed"] is False
    assert evidence["persisted"] is True
    assert evidence["trace_readback_verified"] is True
    assert evidence["error_code"] == "ASSISTANT_PROMPT_INJECTION_REJECTED"
    assert evidence["steps"] == ["authorize_project"]
    assert client.flush_count == 1


@pytest.mark.parametrize(
    "local_call_id",
    [
        "field.team@example.org",
        "202-555-0182",
        "/Users/example/private/report.txt",
        "Bearer abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_weave_aborted_trace_keeps_hostile_call_id_local(
    tmp_path: Path, local_call_id: str
) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id=local_call_id, operation="assistant.request", inputs=_trace_inputs())
    sink.abort(call_id=local_call_id, error_code="ASSISTANT_PROMPT_INJECTION_REJECTED")

    evidence = sink.aborted_readback(call_id=local_call_id)
    root = client.calls["provider-call-1"]
    external_call_id = root.attributes["assistant_call_id"]
    assert root.output["ecocorridor_trace_gate"]["call_id"] == external_call_id
    assert evidence["call_id"] == local_call_id
    assert local_call_id not in json.dumps(
        {"attributes": root.attributes, "output": root.output}, sort_keys=True
    )


def test_weave_aborted_trace_cannot_pass_without_exact_provider_readback(
    tmp_path: Path,
) -> None:
    client = _FakeWeaveClient(corrupt_gate=True)
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_rejected", operation="assistant.request", inputs=_trace_inputs())

    with pytest.raises(RuntimeError, match="ABORT_TRACE_READBACK_INVALID"):
        sink.abort(
            call_id="assistant_rejected",
            error_code="ASSISTANT_PROMPT_INJECTION_REJECTED",
        )
    with pytest.raises(RuntimeError, match="ABORT_TRACE_READBACK_UNAVAILABLE"):
        sink.aborted_readback(call_id="assistant_rejected")


def test_weave_calls_receive_wandb_key_only_inside_scoped_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    with credentials.candidate("not-a-real-wandb-key-value"):
        pass

    class CredentialCheckingFactory(_FakeWeaveFactory):
        def create(self, target: WandbTarget) -> _FakeWeaveClient:
            assert os.environ["WANDB_API_KEY"] == "not-a-real-wandb-key-value"
            return super().create(target)

    class CredentialCheckingScopeProvider(_ExactScopeProvider):
        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            assert os.environ["WANDB_API_KEY"] == "not-a-real-wandb-key-value"
            return super().resolve_exact_scope(**request)

    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=ProjectScopeResolver(CredentialCheckingScopeProvider()),
        project_root=tmp_path,
        client_factory=CredentialCheckingFactory(_FakeWeaveClient()),
        credentials=credentials,
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    assert "WANDB_API_KEY" not in os.environ


@pytest.mark.parametrize(
    ("acknowledge", "corrupt_gate"),
    [(False, False), (True, True)],
)
def test_weave_trace_gate_rejects_missing_or_corrupt_provider_readback(
    tmp_path: Path, acknowledge: bool, corrupt_gate: bool
) -> None:
    client = _FakeWeaveClient(acknowledge=acknowledge, corrupt_gate=corrupt_gate)
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    with pytest.raises(RuntimeError, match="ACKNOWLEDGED|READBACK"):
        sink.flush(call_id="assistant_1")


def test_weave_trace_gate_rejects_wrong_wandb_run_association(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    client.calls["provider-call-1"].wb_run_id = "wildlife-team/ridgeway/wrong-run"

    with pytest.raises(RuntimeError, match="READBACK_INVALID"):
        sink.flush(call_id="assistant_1")


def test_weave_trace_link_cannot_downgrade_verified_target_origin(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    trace_id = sink.flush(call_id="assistant_1")
    client.calls["provider-call-1"].ui_url = "http://wandb.example/weave/traces/downgrade"
    with pytest.raises(RuntimeError, match="URL_OUTSIDE_VERIFIED_TARGET"):
        sink.readback(trace_id=trace_id)


def test_weave_trace_link_rejects_query_bearing_provider_url(tmp_path: Path) -> None:
    client = _FakeWeaveClient()
    sink = WeaveNestedTraceSink(
        target=_target(),
        scope_resolver=_resolver(),
        project_root=tmp_path,
        client_factory=_FakeWeaveFactory(client),
    )
    sink.start(call_id="assistant_1", operation="assistant.request", inputs=_trace_inputs())
    sink.finish(call_id="assistant_1", output={"answer_id": "answer_1"})
    trace_id = sink.flush(call_id="assistant_1")
    client.calls[
        "provider-call-1"
    ].ui_url = "https://wandb.example/weave/traces/provider-call-1?token=must-not-persist"
    with pytest.raises(RuntimeError, match="URL_OUTSIDE_VERIFIED_TARGET"):
        sink.readback(trace_id=trace_id)


def test_weave_scope_failure_prevents_client_creation(tmp_path: Path) -> None:
    factory = _FakeWeaveFactory(_FakeWeaveClient())
    target_payload = _target().model_dump(mode="json")
    target_payload["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["deployment"] = "wandb_cloud"
    target_payload["scope_evidence"]["evidence_kind"] = "PUBLIC_API_DOCUMENTED_EXACT"
    sink = WeaveNestedTraceSink(
        target=WandbTarget.model_validate(target_payload),
        scope_resolver=ProjectScopeResolver(UnsupportedWandbCloudScopeProvider()),
        project_root=tmp_path,
        client_factory=factory,
    )
    with pytest.raises(RuntimeError, match="SCOPE_UNVERIFIABLE"):
        sink.start(
            call_id="assistant_1",
            operation="assistant.request",
            inputs=_trace_inputs(),
        )
    assert factory.created == 0
