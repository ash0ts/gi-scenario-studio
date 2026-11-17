from __future__ import annotations

import hashlib
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gi_scenario_core.contracts import (
    CloudArtifactManifest,
    ObservabilityBinding,
    ProjectDataPolicy,
    TelemetryEnvelope,
)
from gi_scenario_core.observability import CredentialSession, WandbAdapter


class _Settings:
    def __init__(self, **values: object) -> None:
        self.values = values


class _Table:
    def __init__(self, *, columns: list[str], data: list[list[object]]) -> None:
        self.columns = columns
        self.data = data


class _Artifact:
    def __init__(
        self,
        *,
        name: str,
        type: str,
        metadata: dict[str, object],
    ) -> None:
        self.name = name
        self.type = type
        self.metadata = metadata
        self.files: list[tuple[Path, str]] = []

    def add_file(self, local_path: str, name: str) -> None:
        self.files.append((Path(local_path), name))


class _LoggedArtifact:
    def __init__(self, artifact: _Artifact, *, corrupt_download: bool) -> None:
        self.artifact = artifact
        self.id = f"provider-{artifact.name}-v0"
        self.qualified_name = f"team/project/{artifact.name}:v0"
        self.corrupt_download = corrupt_download
        self.waited = False
        self.downloaded = False

    def wait(self) -> None:
        self.waited = True

    def download(self, *, root: str, skip_cache: bool) -> str:
        assert self.waited, "download verification must follow provider upload completion"
        assert skip_cache is True, "provider verification must not reuse the local artifact cache"
        destination = Path(root)
        destination.mkdir(parents=True, exist_ok=True)
        for source, logical_name in self.artifact.files:
            target = destination / logical_name
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            target.write_bytes(b"provider-corruption" if self.corrupt_download else content)
        self.downloaded = True
        return str(destination)


class _Run:
    def __init__(self, init_values: dict[str, object], *, corrupt_download: bool) -> None:
        self.init_values = init_values
        self.id = str(init_values["id"])
        self.url = f"https://wandb.example/team/project/runs/{self.id}"
        self.summary: dict[str, object] = {}
        self.logged_values: list[dict[str, object]] = []
        self.used_artifacts: list[str] = []
        self.logged_artifacts: list[_LoggedArtifact] = []
        self.corrupt_download = corrupt_download
        self.finished = False

    def log(self, values: dict[str, object]) -> None:
        self.logged_values.append(values)

    def use_artifact(self, reference: str) -> object:
        self.used_artifacts.append(reference)
        return object()

    def log_artifact(self, artifact: _Artifact) -> _LoggedArtifact:
        logged = _LoggedArtifact(artifact, corrupt_download=self.corrupt_download)
        self.logged_artifacts.append(logged)
        return logged

    def finish(self) -> None:
        self.finished = True


class _Api:
    def __init__(self, access: str | None, project_id: str) -> None:
        self.access = access
        self.project_id = project_id

    def project(self, project: str, *, entity: str) -> object:
        assert project == "project"
        assert entity == "team"
        return types.SimpleNamespace(access=self.access, id=self.project_id)


class _FakeWandb(types.ModuleType):
    def __init__(
        self,
        *,
        corrupt_download: bool = False,
        project_access: str | None = "RESTRICTED",
        project_id: str = "provider-project-id",
    ) -> None:
        super().__init__("wandb")
        self.Settings = _Settings
        self.Artifact = _Artifact
        self.Table = _Table
        self.corrupt_download = corrupt_download
        self.project_access = project_access
        self.project_id = project_id
        self.runs: list[_Run] = []

    def Api(self) -> _Api:
        return _Api(self.project_access, self.project_id)

    def init(self, **values: object) -> _Run:
        run = _Run(values, corrupt_download=self.corrupt_download)
        self.runs.append(run)
        return run


def _binding() -> ObservabilityBinding:
    return ObservabilityBinding(
        project_id="project_1",
        data_policy_id="policy_1",
        entity="team",
        external_project="project",
        external_project_id="provider-project-id",
        deployment="wandb_cloud",
        verified=True,
        verified_at=datetime.now(UTC),
    )


def _evidence(tmp_path: Path) -> tuple[TelemetryEnvelope, dict[str, Path]]:
    content = b"exact engineering evidence\n"
    source = tmp_path / "local" / "baseline.inp"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest = CloudArtifactManifest(
        artifact_id="artifact_1",
        project_id="project_1",
        category="model",
        logical_name="models/baseline.inp",
        sha256=digest,
        bytes=len(content),
        media_type="text/plain",
        source_object_id="revision_1",
        local_blob_sha256=digest,
    )
    envelope = TelemetryEnvelope(
        envelope_id="envelope_1",
        project_id="project_1",
        binding_id="binding_1",
        category="engineering_evidence",
        event_type="normalized_model",
        subject_id="revision_1",
        idempotency_key="normalized-model-revision-1",
        metadata={
            "input_artifact_refs": [
                "team/project/source-bundle:v3",
                "team/project/source-bundle:v3",
                "wandb-registry-Datasets/approved-storms:v7",
            ],
            "tables": {
                "solver_jobs": {
                    "columns": ["job_id", "state"],
                    "rows": [["job_1", "SUCCEEDED"]],
                }
            },
        },
        artifacts=[manifest],
    )
    return envelope, {manifest.artifact_id: source}


def _adapter() -> WandbAdapter:
    credentials = CredentialSession()
    credentials.configure("x" * 40, "https://api.wandb.ai")
    return WandbAdapter(credentials)


def _policy() -> ProjectDataPolicy:
    return ProjectDataPolicy(
        project_id="project_1",
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="engineer",
    )


@pytest.mark.parametrize("access", [None, "PRIVATE", "TEAM", "PUBLIC", "USER_READ"])
def test_wandb_binding_fails_closed_without_exact_restricted_scope(
    monkeypatch: pytest.MonkeyPatch,
    access: str | None,
) -> None:
    sdk = _FakeWandb(project_access=access)
    monkeypatch.setitem(sys.modules, "wandb", sdk)

    with pytest.raises(ValueError, match="WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"):
        _adapter().validate_binding(_binding(), _policy())


def test_wandb_binding_accepts_only_authoritative_restricted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeWandb(project_access="RESTRICTED")
    monkeypatch.setitem(sys.modules, "wandb", sdk)

    validated = _adapter().validate_binding(_binding(), _policy())

    assert validated.verified is True
    assert validated.external_project_id == "provider-project-id"


def test_wandb_upload_projects_lineage_and_verifies_exact_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = _FakeWandb()
    monkeypatch.setitem(sys.modules, "wandb", sdk)
    envelope, artifact_paths = _evidence(tmp_path)

    receipt = _adapter().upload(envelope, _binding(), artifact_paths, tmp_path)

    assert len(sdk.runs) == 1
    run = sdk.runs[0]
    expected_run_id = hashlib.sha256(b"gi-v2|project_1|envelope_1").hexdigest()[:24]
    assert run.init_values["id"] == expected_run_id
    assert run.init_values["resume"] == "allow"
    settings = run.init_values["settings"]
    assert isinstance(settings, _Settings)
    assert settings.values["x_disable_machine_info"] is True
    assert settings.values["x_disable_meta"] is True
    assert settings.values["x_disable_stats"] is True
    assert settings.values["x_save_requirements"] is False
    assert run.used_artifacts == [
        "team/project/source-bundle:v3",
        "wandb-registry-Datasets/approved-storms:v7",
    ]
    assert run.summary["input_artifact_refs"] == run.used_artifacts
    assert run.summary["table_names"] == ["solver_jobs"]
    logged_table = next(item["solver_jobs"] for item in run.logged_values if "solver_jobs" in item)
    assert isinstance(logged_table, _Table)
    assert logged_table.data == [["job_1", "SUCCEEDED"]]
    assert "tables" not in run.init_values["config"]["metadata"]
    assert run.summary["output_artifact_ids"] == ["provider-model-artifact_1-v0"]
    assert run.summary["artifact_projection"] == [
        {
            "local_artifact_id": "artifact_1",
            "provider_artifact_id": "provider-model-artifact_1-v0",
            "provider_artifact_ref": "team/project/model-artifact_1:v0",
            "sha256": envelope.artifacts[0].sha256,
        }
    ]
    assert run.logged_artifacts[0].waited is True
    assert run.logged_artifacts[0].downloaded is True
    assert run.finished is True
    assert receipt.run_id == expected_run_id
    assert receipt.artifact_ids == ["provider-model-artifact_1-v0"]


def test_wandb_upload_rejects_provider_content_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = _FakeWandb(corrupt_download=True)
    monkeypatch.setitem(sys.modules, "wandb", sdk)
    envelope, artifact_paths = _evidence(tmp_path)

    with pytest.raises(RuntimeError, match="WANDB_ARTIFACT_HASH_MISMATCH"):
        _adapter().upload(envelope, _binding(), artifact_paths, tmp_path)

    assert sdk.runs[0].logged_artifacts[0].waited is True
    assert sdk.runs[0].logged_artifacts[0].downloaded is True
    assert sdk.runs[0].finished is True


def test_wandb_upload_revalidates_restricted_scope_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = _FakeWandb(project_access="PRIVATE")
    monkeypatch.setitem(sys.modules, "wandb", sdk)
    envelope, artifact_paths = _evidence(tmp_path)

    with pytest.raises(ValueError, match="WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"):
        _adapter().upload(envelope, _binding(), artifact_paths, tmp_path)

    assert sdk.runs == []


def test_wandb_probe_verifies_the_exact_logged_version_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeWandb()
    monkeypatch.setitem(sys.modules, "wandb", sdk)
    payload = b"bounded integration probe\n"
    digest = hashlib.sha256(payload).hexdigest()

    reference = _adapter().roundtrip_probe(_binding(), payload, digest)

    logged = sdk.runs[0].logged_artifacts[0]
    assert logged.waited is True
    assert logged.downloaded is True
    assert reference.artifact_ids == [logged.id]
    assert sdk.runs[0].finished is True


@pytest.mark.parametrize(
    "reference",
    [
        "team/project/source-bundle:latest",
        "team/project/source-bundle:production",
        "source-bundle:v3",
        "/local/source-bundle:v3",
    ],
)
def test_wandb_upload_requires_pinned_qualified_input_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    sdk = _FakeWandb()
    monkeypatch.setitem(sys.modules, "wandb", sdk)
    envelope, artifact_paths = _evidence(tmp_path)
    envelope.metadata["input_artifact_refs"] = [reference]

    with pytest.raises(ValueError, match=r"input[_ ]artifact"):
        _adapter().upload(envelope, _binding(), artifact_paths, tmp_path)

    assert sdk.runs == []
