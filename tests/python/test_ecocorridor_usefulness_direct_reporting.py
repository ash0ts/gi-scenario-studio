from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from ecocorridor_core import usefulness_direct_reporting as reporting
from ecocorridor_core.usefulness_direct_reporting import (
    DIRECT_UPLOAD_AUTHORIZATION,
    DirectEvidenceCloudReceipt,
    DirectEvidencePublicationError,
    GitPushedSourceVerifier,
    PushedSourceReceipt,
    WandbDirectEvidencePublisher,
    append_direct_evidence_section,
    build_direct_evidence_report_section,
    prepare_direct_evidence_publication,
    publish_verified_direct_evidence,
)
from ecocorridor_core.usefulness_direct_study import UsefulnessEvidencePackageReceipt

SOURCE_REPOSITORY = "https://github.com/ash0ts/gi-scenario-studio"
SOURCE_COMMIT = "a" * 40
ENGINE_SHA256 = "e" * 64
ENGINE_MANIFEST_SHA256 = "b" * 64


def _job(index: int) -> dict[str, object]:
    token = f"{index:064x}"
    resource = {
        "observation_id": f"resource-{index:03d}",
        "boundary": "DIRECT_SOLVER",
        "solver_wall_seconds": 0.125 + index / 1000,
        "proposer_wall_seconds": None,
        "cache_lookup_wall_seconds": None,
        "peak_memory_status": "MEASURED",
        "peak_memory_bytes": 1_000_000 + index,
        "peak_memory_method": "test sampler",
        "energy_status": "NOT_MEASURED",
        "energy_kwh": None,
        "energy_method": "not measured",
        "peak_memory_sample_count": 2,
        "peak_memory_sampling_interval_seconds": 0.1,
        "peak_memory_is_lower_bound": True,
        "energy_estimate_watts": None,
        "energy_estimate_lower_watts": None,
        "energy_estimate_upper_watts": None,
        "energy_estimate_lower_kwh": None,
        "energy_estimate_upper_kwh": None,
        "hardware": "synthetic test hardware",
        "boundary_description": "isolated direct solver",
        "assumptions": [],
        "exclusions": ["no carbon inference"],
    }
    return {
        "direct_run_id": f"run-{index:03d}",
        "family": f"landscape-{index % 5}",
        "portfolio_id": f"portfolio-{index // 20:02d}",
        "scenario_id": f"scenario-{index % 4}",
        "safeguard": index % 3 == 0,
        "state": "SUCCEEDED",
        "exact": True,
        "qa_status": "ACCEPTED",
        "qa_findings": [],
        "engine_name": "Circuitscape",
        "engine_version": "5.17.1",
        "engine_executable_sha256": ENGINE_SHA256,
        "engine_manifest_sha256": ENGINE_MANIFEST_SHA256,
        "model_run_cache_key": token,
        "request_sha256": token,
        "result_sha256": token,
        "matrix_sha256": token,
        "current_map_sha256": token,
        "effective_configuration_sha256": token,
        "artifacts": [{"role": "request", "sha256": token}],
        "matrix_current_coherence": {"qa_sha256": token},
        "process_exit_evidence": {"tree_exit_confirmed": True},
        "resource_observation": resource,
        "producer_validator_sha256": token,
    }


def _deterministic_archive(path: Path, index: dict[str, object]) -> None:
    payload = (
        json.dumps(index, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        info = tarfile.TarInfo("evidence-index.json")
        info.size = len(payload)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(payload))


def _package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine_manifest = tmp_path / "engine-manifest.json"
    engine_manifest.write_text('{"engine":"exact"}\n', encoding="utf-8")
    engine_manifest_file_sha = hashlib.sha256(engine_manifest.read_bytes()).hexdigest()
    engine_provenance = tmp_path / "engine-provenance.json"
    engine_provenance.write_text('{"attested":"test fixture"}\n', encoding="utf-8")
    engine_provenance_sha = hashlib.sha256(engine_provenance.read_bytes()).hexdigest()
    attestation_verification_sha = "d" * 64
    index = {
        "schema_version": "ecocorridor-usefulness-evidence-index/v1.0",
        "evidence_index_content_sha256": "f" * 64,
        "job_count": 160,
        "jobs": [_job(index) for index in range(160)],
    }
    archive = tmp_path / "usefulness-direct-evidence-test.tar.gz"
    _deterministic_archive(archive, index)
    index_sha = reporting.sha256_file_from_archive_index(archive)
    manifest_payload = {
        "schema_version": "ecocorridor-usefulness-direct-evidence/v1.0",
        "license": "CC0-1.0",
        "engine_executable_sha256": ENGINE_SHA256,
        "engine_manifest_sha256": ENGINE_MANIFEST_SHA256,
        "engine_manifest_file_sha256": engine_manifest_file_sha,
        "engine_trust_profile": "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "engine_provenance_receipt_sha256": engine_provenance_sha,
        "engine_attestation_verification_sha256": attestation_verification_sha,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "source_tree_clean": True,
        "job_count": 160,
        "qa_accepted_jobs": 160,
        "complete_target_and_safeguard_ensembles": True,
        "direct_evidence_complete": True,
        "usefulness_claims_promoted": False,
        "release_eligible": False,
        "outcome_promotion": "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION",
    }
    manifest = tmp_path / "direct-evidence-manifest.json"
    manifest.write_text(json.dumps(manifest_payload, sort_keys=True), encoding="utf-8")
    calls: list[dict[str, object]] = []

    def verifier(**kwargs):
        calls.append(kwargs)
        return UsefulnessEvidencePackageReceipt(
            manifest_path=manifest.resolve(),
            archive_path=archive.resolve(),
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            evidence_index_sha256=index_sha,
            engine_manifest_sha256=ENGINE_MANIFEST_SHA256,
            engine_provenance_receipt_sha256=engine_provenance_sha,
            engine_attestation_verification_sha256=attestation_verification_sha,
            engine_provenance_path=engine_provenance.resolve(),
            source_repository=SOURCE_REPOSITORY,
            source_commit=SOURCE_COMMIT,
        )

    monkeypatch.setattr(reporting, "verify_usefulness_evidence_package", verifier)
    return manifest, archive, engine_manifest, engine_provenance, calls


class _SourceVerifier:
    def __init__(self, *, commit: str = SOURCE_COMMIT) -> None:
        self.commit = commit
        self.calls: list[dict[str, object]] = []

    def verify(self, **kwargs) -> PushedSourceReceipt:
        self.calls.append(kwargs)
        return PushedSourceReceipt.create(
            source_repository=SOURCE_REPOSITORY,
            source_commit=self.commit,
            upstream_ref="refs/heads/codex/ecocorridor-scenario-studio",
            upstream_commit=self.commit,
        )


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, archive, engine_manifest, engine_provenance, verifier_calls = _package(
        tmp_path, monkeypatch
    )
    source = _SourceVerifier()
    plan = prepare_direct_evidence_publication(
        manifest_path=manifest,
        archive_path=archive,
        engine_manifest_path=engine_manifest,
        engine_provenance_path=engine_provenance,
        expected_engine_sha256=ENGINE_SHA256,
        repository_root=tmp_path,
        upload_authorization=DIRECT_UPLOAD_AUTHORIZATION,
        source_verifier=source,
    )
    return plan, verifier_calls, source


def test_prepare_reverifies_exact_package_and_projects_only_qa_resource_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, verifier_calls, source = _plan(tmp_path, monkeypatch)
    assert len(verifier_calls) == len(source.calls) == 1
    assert verifier_calls[0]["expected_engine_sha256"] == ENGINE_SHA256
    assert plan.run_kind == "usefulness_fixture_generation"
    assert [len(table.rows) for table in plan.tables] == [160, 160]
    assert plan.run_config["direct_evidence_complete"] is True
    assert plan.run_config["outcome_analysis"] == "BLOCKED"
    assert plan.run_config["adjudication"] == "BLOCKED"
    assert plan.run_config["usefulness_claims"] == "BLOCKED"
    assert plan.run_config["ecological_outcome_value_count"] == 0
    assert plan.run_config["new_result_media_logged"] is False
    columns = {column for table in plan.tables for column in table.columns}
    assert "effective_resistance" not in columns
    assert "current_sum" not in columns
    assert "portfolio_disposition" not in columns
    serialized = plan.model_dump(mode="json")
    assert "manifest_path" not in serialized
    assert "archive_path" not in serialized
    assert "engine_manifest_path" not in serialized


def test_prepare_requires_exact_authorization_and_matching_pushed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, archive, engine_manifest, engine_provenance, calls = _package(
        tmp_path, monkeypatch
    )
    with pytest.raises(PermissionError, match="exact public CC0"):
        prepare_direct_evidence_publication(
            manifest_path=manifest,
            archive_path=archive,
            engine_manifest_path=engine_manifest,
            engine_provenance_path=engine_provenance,
            expected_engine_sha256=ENGINE_SHA256,
            repository_root=tmp_path,
            upload_authorization="yes",
            source_verifier=_SourceVerifier(),
        )
    assert calls == []
    with pytest.raises((PermissionError, ValueError), match="commit"):
        prepare_direct_evidence_publication(
            manifest_path=manifest,
            archive_path=archive,
            engine_manifest_path=engine_manifest,
            engine_provenance_path=engine_provenance,
            expected_engine_sha256=ENGINE_SHA256,
            repository_root=tmp_path,
            upload_authorization=DIRECT_UPLOAD_AUTHORIZATION,
            source_verifier=_SourceVerifier(commit="c" * 40),
        )


def test_git_source_verifier_requires_clean_exact_remote_head(tmp_path: Path) -> None:
    answers = {
        ("git", "rev-parse", "HEAD"): SOURCE_COMMIT,
        ("git", "status", "--porcelain=v1", "--untracked-files=all"): "",
        ("git", "symbolic-ref", "--short", "HEAD"): "codex/direct-evidence",
        (
            "git",
            "config",
            "--get",
            "branch.codex/direct-evidence.remote",
        ): "origin",
        (
            "git",
            "config",
            "--get",
            "branch.codex/direct-evidence.merge",
        ): "refs/heads/codex/direct-evidence",
        ("git", "remote", "get-url", "origin"): (
            "git@github.com:ash0ts/gi-scenario-studio.git"
        ),
        (
            "git",
            "ls-remote",
            "--exit-code",
            SOURCE_REPOSITORY,
            "refs/heads/codex/direct-evidence",
        ): f"{SOURCE_COMMIT}\trefs/heads/codex/direct-evidence",
    }

    def runner(arguments, _root):
        return answers[tuple(arguments)]

    receipt = GitPushedSourceVerifier(runner=runner).verify(
        repository_root=tmp_path,
        source_repository=SOURCE_REPOSITORY,
        source_commit=SOURCE_COMMIT,
    )
    assert receipt.source_tree_clean is True
    assert receipt.pushed_to_upstream is True
    dirty = dict(answers)
    dirty[("git", "status", "--porcelain=v1", "--untracked-files=all")] = "?? local"
    with pytest.raises(PermissionError, match="clean source tree"):
        GitPushedSourceVerifier(runner=lambda arguments, _root: dirty[tuple(arguments)]).verify(
            repository_root=tmp_path,
            source_repository=SOURCE_REPOSITORY,
            source_commit=SOURCE_COMMIT,
        )


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Reports:
    MarkdownBlock = _Block
    CalloutBlock = _Block
    H1 = _Block
    H2 = _Block


class _FakePublisher:
    def __init__(self) -> None:
        self.published: dict[str, DirectEvidenceCloudReceipt] = {}
        self.publish_calls = 0
        self.report_calls = 0

    def publish_plan(self, plan):
        self.publish_calls += 1
        return self.published.setdefault(
            plan.plan_sha256,
            DirectEvidenceCloudReceipt(
                plan_sha256=plan.plan_sha256,
                run_id=plan.run_id,
                run_url=f"https://wandb.ai/team/project/runs/{plan.run_id}",
                artifact_ref="team/project/direct-evidence:v0",
                artifact_url="https://wandb.ai/team/project/artifacts/direct-evidence/v0",
                artifact_manifest_sha256=plan.artifact_manifest_sha256,
            ),
        )

    def append_report_section(self, *, report_url, plan, cloud):
        self.report_calls += 1
        return report_url


def test_publish_receipt_and_report_section_are_idempotent_and_block_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _, _ = _plan(tmp_path, monkeypatch)
    publisher = _FakePublisher()
    report_url = "https://wandb.ai/team/project/reports/existing"
    first = publish_verified_direct_evidence(
        plan=plan, report_url=report_url, publisher=publisher
    )
    second = publish_verified_direct_evidence(
        plan=plan, report_url=report_url, publisher=publisher
    )
    assert first == second
    assert first.direct_job_count == 160
    assert first.outcome_analysis == first.adjudication == first.usefulness_claims == "BLOCKED"
    cloud = publisher.published[plan.plan_sha256]
    section = build_direct_evidence_report_section(_Reports, plan, cloud)
    text = "\n".join(block.text for block in section)
    assert "160/160 direct Circuitscape jobs" in text
    assert "usefulness claims: BLOCKED" in text
    assert "ecological outcome values were logged" in text
    existing = [_Block("historical report content")]
    once = append_direct_evidence_section(existing, section)
    assert append_direct_evidence_section(once, section) == once
    different = list(section)
    different[0] = _Block(different[0].text.replace(plan.plan_sha256, "0" * 64))
    with pytest.raises(DirectEvidencePublicationError, match="different current"):
        append_direct_evidence_section(once, different)


class _PublicArtifact:
    def __init__(self, plan, root: Path) -> None:
        self.metadata = WandbDirectEvidencePublisher._artifact_metadata(plan)
        self.url = "https://wandb.ai/team/project/artifacts/direct-evidence/v0"
        self._plan = plan
        self._root = root

    def download(self, *, root: str) -> str:
        target = Path(root)
        for logical_name, source in self._plan.artifact_paths.items():
            shutil.copyfile(source, target / logical_name)
        return str(target)


class _PublicRun:
    def __init__(self, plan) -> None:
        self.id = plan.run_id
        self.state = "finished"
        self.url = f"https://wandb.ai/team/project/runs/{plan.run_id}"
        self.config = plan.run_config
        self.summary = plan.expected_summary(
            artifact_ref="team/project/direct-evidence:v0"
        )


class _Api:
    def __init__(self, run, artifact) -> None:
        self._run = run
        self._artifact = artifact

    def run(self, _path):
        return self._run

    def artifact(self, _reference):
        return self._artifact


class _Wandb:
    def __init__(self, api) -> None:
        self._api = api
        self.init_called = False

    def Api(self, **_kwargs):
        return self._api

    def init(self, **_kwargs):
        self.init_called = True
        raise AssertionError("idempotent reuse must not create another run")


def test_wandb_adapter_reuses_only_exact_finished_run_with_artifact_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _, _ = _plan(tmp_path, monkeypatch)
    artifact = _PublicArtifact(plan, tmp_path)
    public_run = _PublicRun(plan)
    wandb = _Wandb(_Api(public_run, artifact))
    publisher = WandbDirectEvidencePublisher(
        entity="team",
        project="project",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        upload_authorization=DIRECT_UPLOAD_AUTHORIZATION,
        wandb_module=wandb,
        reports_module=_Reports,
    )
    receipt = publisher.publish_plan(plan)
    assert receipt.run_id == plan.run_id
    assert receipt.artifact_readback_verified is True
    assert wandb.init_called is False


class _NotFound(Exception):
    status_code = 404


class _LoggedArtifact:
    qualified_name = "team/project/direct-evidence:v0"

    def wait(self):
        return self


class _CreatingArtifact:
    def __init__(self, name, *, type, metadata) -> None:
        self.name = name
        self.type = type
        self.metadata = metadata
        self.url = "https://wandb.ai/team/project/artifacts/direct-evidence/v0"
        self.files: dict[str, Path] = {}

    def add_file(self, local_path, *, name, policy):
        assert policy == "immutable"
        self.files[name] = Path(local_path)

    def download(self, *, root):
        target = Path(root)
        for logical_name, source in self.files.items():
            shutil.copyfile(source, target / logical_name)
        return str(target)


class _CreatingRun:
    def __init__(self, owner, **kwargs) -> None:
        self.owner = owner
        self.id = kwargs["id"]
        self.url = f"https://wandb.ai/team/project/runs/{self.id}"
        self.config = kwargs["config"]
        self.summary: dict[str, object] = {}
        self.state = "running"
        self.logged_tables: dict[str, object] = {}
        self.exit_code = None

    def log(self, values):
        self.logged_tables = values

    def log_artifact(self, artifact):
        self.owner.artifact = artifact
        return _LoggedArtifact()

    def finish(self, *, exit_code):
        self.exit_code = exit_code
        self.state = "finished" if exit_code == 0 else "failed"


class _CreatingApi:
    def __init__(self, owner) -> None:
        self.owner = owner

    def run(self, _path):
        if self.owner.run is None:
            raise _NotFound
        return self.owner.run

    def artifact(self, _reference):
        assert self.owner.artifact is not None
        return self.owner.artifact


class _CreatingWandb:
    def __init__(self) -> None:
        self.run = None
        self.artifact = None

    def Api(self, **_kwargs):
        return _CreatingApi(self)

    @staticmethod
    def Settings(**kwargs):
        return kwargs

    @staticmethod
    def Table(*, columns, data):
        return {"columns": columns, "data": data}

    def Artifact(self, name, *, type, metadata):
        return _CreatingArtifact(name, type=type, metadata=metadata)

    def init(self, **kwargs):
        self.run = _CreatingRun(self, **kwargs)
        return self.run


def test_wandb_adapter_creates_one_run_two_tables_and_exact_three_file_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _, _ = _plan(tmp_path, monkeypatch)
    wandb = _CreatingWandb()
    publisher = WandbDirectEvidencePublisher(
        entity="team",
        project="project",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        upload_authorization=DIRECT_UPLOAD_AUTHORIZATION,
        wandb_module=wandb,
        reports_module=_Reports,
    )
    receipt = publisher.publish_plan(plan)
    assert receipt.run_readback_verified is True
    assert wandb.run.exit_code == 0
    assert set(wandb.run.logged_tables) == {
        "usefulness_direct_jobs",
        "usefulness_direct_resource_qa",
    }
    assert all(len(table["data"]) == 160 for table in wandb.run.logged_tables.values())
    assert set(wandb.artifact.files) == set(plan.artifact_paths)
    assert wandb.run.summary["direct_evidence/outcome_analysis"] == "BLOCKED"
    assert wandb.run.summary["direct_evidence/ecological_outcome_value_count"] == 0
