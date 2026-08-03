from __future__ import annotations

import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import gi_scenario_core.rpc_worker as rpc_module
import pytest
from gi_scenario_core.contracts import (
    CloudArtifactManifest,
    ObservabilityBinding,
    ProjectDataPolicy,
    TelemetryEnvelope,
)
from gi_scenario_core.observability import WandbUploadReceipt
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault


class FailingWandb:
    def __init__(self) -> None:
        self.uploads: list[TelemetryEnvelope] = []

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt:
        assert binding.verified
        assert workspace.is_dir()
        assert all(path.is_file() for path in artifact_paths.values())
        self.uploads.append(envelope)
        raise RuntimeError("simulated W&B outage with /Users/private-account and token material")


def _bind(repository: ProjectRepository, project_id: str) -> None:
    policy = ProjectDataPolicy(
        policy_id=f"policy_{project_id}",
        project_id=project_id,
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="Engineer A",
    )
    repository.upsert_project_data_policy(policy)
    repository.connect_observability_binding(
        ObservabilityBinding(
            binding_id=f"binding_{project_id}",
            project_id=project_id,
            data_policy_id=policy.policy_id,
            entity="stormwater-team",
            external_project=f"gi-{project_id}",
            external_project_id=f"wandb-{project_id}",
            deployment="wandb_cloud",
            verified=True,
            verified_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
    )


def _new_bound_project(root: Path, *, kind: str = "existing") -> ProjectRepository:
    repository = ProjectRepository(root)
    repository.create_project(
        title="Automatic telemetry fixture",
        project_kind=kind,
        project_id="project_1",
    )
    _bind(repository, "project_1")
    return repository


def _engineering_snapshot(repository: ProjectRepository) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "projects",
        "project_assets",
        "analyses",
        "designs",
        "solver_jobs",
        "run_artifacts",
        "run_metrics",
        "qa_findings",
        "model_revisions",
        "model_fitness_passports",
        "decision_briefs",
        "guided_checkpoints",
    )
    snapshot: dict[str, list[tuple[object, ...]]] = {}
    for table in tables:
        columns = [
            row["name"]
            for row in repository._connection.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        order = ",".join(columns)
        rows = repository._connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        snapshot[table] = [tuple(row[column] for column in columns) for row in rows]
    return snapshot


def _seed_completed_analysis(
    repository: ProjectRepository,
    *,
    job_count: int,
    analysis_id: str = "analysis_1",
    analysis_payload: dict[str, object] | None = None,
) -> None:
    repository.create_analysis("project_1", analysis_id=analysis_id, payload=analysis_payload or {})
    designs = [
        {
            "design_id": f"design_{index:03d}",
            "vector": [index],
            "source": "baseline" if index == 0 else "exact_plan",
            "sequence": index,
        }
        for index in range(job_count)
    ]
    repository.add_designs(analysis_id, designs)
    for index, design in enumerate(designs):
        job_id = f"job_{index:03d}"
        input_bytes = f"[TITLE]\n;; exact input {index}\n".encode()
        report_bytes = f"exact report {index}\n".encode()
        output_bytes = f"exact output {index}\n".encode()
        repository.create_solver_job(
            analysis_id=analysis_id,
            design_id=design["design_id"],
            storm_id="storm_1",
            physics_cache_key=hashlib.sha256(f"physics-{index}".encode()).hexdigest(),
            payload={"qa_status": "accepted", "solver_sha256": "a" * 64},
            job_id=job_id,
        )
        repository.transition_solver_job(job_id, "QUEUED")
        repository.transition_solver_job(job_id, "RUNNING")
        repository.transition_solver_job(job_id, "SUCCEEDED")
        repository.add_run_artifact(job_id, "input", input_bytes)
        repository.add_run_artifact(job_id, "report", report_bytes)
        repository.add_run_artifact(job_id, "output", output_bytes)
        repository.add_run_metric(
            job_id,
            "outfall_volume_million_gallons",
            1.0 + index,
            "million_gallons",
        )
        repository.add_run_metric(job_id, "outfall_peak_cfs", 2.0 + index, "cfs")
    repository.transition_analysis(analysis_id, "VALIDATING")
    repository.transition_analysis(analysis_id, "READY")
    repository.transition_analysis(analysis_id, "RUNNING")
    repository.transition_analysis(analysis_id, "COMPLETED")


def _wait_for_upload(dispatcher: RPCDispatcher, outbox_id: str) -> None:
    background = dispatcher._background[f"telemetry:{outbox_id}"]
    background["future"].result(timeout=20)


def test_bundle_import_queues_every_source_file_with_hashes(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    repository.close()
    archive = tmp_path / "bundle.zip"
    model = Path("benchmarks/cc0_reference/reference.inp").read_bytes()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("model/base.inp", model)
        bundle.writestr("model/site-notes.txt", "portable source note")

    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])
    imported = dispatcher.dispatch(
        "bundle.import",
        {
            "archive_path": str(archive),
            "expected_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expected_bytes": archive.stat().st_size,
            "destination": str(root / "sources" / "imported-bundle"),
            "project_directory": str(root),
            "project_id": "project_1",
        },
    )

    assert imported["project_state"]["bundle_imported"] is True
    assert imported["project_state"]["bundle_file_count"] == 2
    assert imported["project_state"]["current_model_revision_id"].startswith("revision_")
    assert imported["project_state"]["model_fitness_passport_id"].startswith("fitness_")
    assert imported["cloud_sync"]["state"] == "pending"
    repository = ProjectRepository(root)
    try:
        revision = repository.get_model_revision(
            imported["project_state"]["current_model_revision_id"]
        )
        passport = repository.get_model_fitness_passport("project_1")
        assert revision["model_sha256"] == next(
            entry["sha256"] for entry in imported["files"] if entry["path"] == "model/base.inp"
        )
        assert passport is not None
        assert passport["disposition"] == "not_assessed"
        assert passport["model_revision_id"] == revision["revision_id"]
        assert passport["reviewer_attested"] is False
        item = repository.get_telemetry_outbox_item(imported["cloud_sync"]["outbox_id"])
        assert item["envelope"]["event_type"] == "bundle_import"
        assert item["envelope"]["metadata"]["tables"]["source_files"]["rows"] == [
            [entry["path"], entry["role"], entry["size"], entry["sha256"]]
            for entry in imported["files"]
        ]
        assert len(item["envelope"]["artifacts"]) == 2
        for artifact in item["envelope"]["artifacts"]:
            blob = root / "blobs" / "sha256" / artifact["local_blob_sha256"]
            assert blob.is_file()
            assert hashlib.sha256(blob.read_bytes()).hexdigest() == artifact["sha256"]
    finally:
        repository.close()


def test_bundle_import_persists_project_contained_dependency_mapping(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    repository.close()
    archive = tmp_path / "bundle-with-rain.zip"
    model = """[RAINGAGES]
RG1 INTENSITY 0:05 1 FILE "rain\\event.dat" STA MM
[OPTIONS]
FLOW_UNITS CFS
"""
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("model/base.inp", model)
        bundle.writestr("model/rain/event.dat", "rain")

    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])
    imported = dispatcher.dispatch(
        "bundle.import",
        {
            "archive_path": str(archive),
            "expected_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expected_bytes": archive.stat().st_size,
            "destination": str(root / "sources" / "imported-bundle"),
            "project_directory": str(root),
            "project_id": "project_1",
        },
    )

    assert imported["project_state"]["selected_model_dependencies"] == {
        r"rain\event.dat": "sources/imported-bundle/model/rain/event.dat"
    }
    repository = ProjectRepository(root)
    try:
        revision = repository.get_model_revision(
            imported["project_state"]["current_model_revision_id"]
        )
        assert revision["dependency_hashes"] == {
            r"rain\event.dat": hashlib.sha256(b"rain").hexdigest()
        }
    finally:
        repository.close()


def test_multi_model_import_requires_and_persists_explicit_base_selection(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    repository.close()
    archive = tmp_path / "multi-model-bundle.zip"
    model_without_dependency = """[OPTIONS]
FLOW_UNITS CFS
[RAINGAGES]
RG1 INTENSITY 0:05 1 TIMESERIES TS1
"""
    selected_model = """[RAINGAGES]
RG1 INTENSITY 0:05 1 FILE "rain\\event.dat" STA MM
[OPTIONS]
FLOW_UNITS CFS
"""
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("model/base.inp", model_without_dependency)
        bundle.writestr("model/storm.inp", selected_model)
        bundle.writestr("model/rain/event.dat", "rain")

    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])
    imported = dispatcher.dispatch(
        "bundle.import",
        {
            "archive_path": str(archive),
            "expected_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expected_bytes": archive.stat().st_size,
            "destination": str(root / "sources" / "imported-bundle"),
            "project_directory": str(root),
            "project_id": "project_1",
        },
    )

    assert imported["project_state"]["base_model_selection_required"] is True
    assert "selected_base_model" not in imported["project_state"]
    assert "current_model_revision_id" not in imported["project_state"]
    assert "bundle.select_base_model" in dispatcher.methods
    with pytest.raises(RPCFault, match="not one of this project's imported candidates"):
        dispatcher.dispatch(
            "bundle.select_base_model",
            {
                "project_directory": str(root),
                "project_id": "project_1",
                "model_path": "sources/imported-bundle/model/not-imported.inp",
            },
        )

    selected = dispatcher.dispatch(
        "bundle.select_base_model",
        {
            "project_directory": str(root),
            "project_id": "project_1",
            "model_path": "sources/imported-bundle/model/storm.inp",
        },
    )

    state = selected["project_state"]
    assert state["base_model_selection_required"] is False
    assert state["selected_base_model"] == "sources/imported-bundle/model/storm.inp"
    assert state["imported_model_roles"] == {
        "sources/imported-bundle/model/base.inp": "unclassified_companion",
        "sources/imported-bundle/model/storm.inp": "authoritative_base",
    }
    assert state["selected_model_dependencies"] == {
        r"rain\event.dat": "sources/imported-bundle/model/rain/event.dat"
    }
    assert selected["model_validation"] == state["import_preflight"]
    assert selected["model_validation"]["blocking_count"] > 0
    repeated = dispatcher.dispatch(
        "bundle.select_base_model",
        {
            "project_directory": str(root),
            "project_id": "project_1",
            "model_path": "sources/imported-bundle/model/storm.inp",
        },
    )
    assert (
        repeated["project_state"]["current_model_revision_id"] == state["current_model_revision_id"]
    )
    repository = ProjectRepository(root)
    try:
        revision = repository.get_model_revision(state["current_model_revision_id"])
        passport = repository.get_model_fitness_passport("project_1")
        assert revision["created_by"] == "bundle.select_base_model"
        assert revision["model_sha256"] == hashlib.sha256(selected_model.encode()).hexdigest()
        assert revision["dependency_hashes"] == {
            r"rain\event.dat": hashlib.sha256(b"rain").hexdigest()
        }
        assert passport is not None
        assert passport["model_revision_id"] == revision["revision_id"]
        assert passport["disposition"] == "not_assessed"
        selection_item = repository.get_telemetry_outbox_item(selected["cloud_sync"]["outbox_id"])
        metadata = selection_item["envelope"]["metadata"]
        assert selection_item["envelope"]["event_type"] == "bundle_import"
        assert metadata["event_phase"] == "base_model_selection"
        assert metadata["model_revision_id"] == revision["revision_id"]
        assert metadata["model_fitness_passport_id"] == passport["passport_id"]
        assert metadata["selected_base_model"] == state["selected_base_model"]
        assert metadata["tables"]["imported_models"]["rows"] == [
            [
                "sources/imported-bundle/model/base.inp",
                "unclassified_companion",
                hashlib.sha256(model_without_dependency.encode()).hexdigest(),
            ],
            [
                "sources/imported-bundle/model/storm.inp",
                "authoritative_base",
                hashlib.sha256(selected_model.encode()).hexdigest(),
            ],
        ]
        assert len(selection_item["envelope"]["artifacts"]) == 2
    finally:
        repository.close()


def test_base_selection_rejects_source_changed_after_import(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    repository.close()
    archive = tmp_path / "multi-model-bundle.zip"
    model = "[OPTIONS]\nFLOW_UNITS CFS\n"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("one.inp", model)
        bundle.writestr("two.inp", model)

    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])
    dispatcher.dispatch(
        "bundle.import",
        {
            "archive_path": str(archive),
            "expected_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expected_bytes": archive.stat().st_size,
            "destination": str(root / "sources" / "imported-bundle"),
            "project_directory": str(root),
            "project_id": "project_1",
        },
    )
    (root / "sources" / "imported-bundle" / "two.inp").write_text(
        f"{model}; changed after import\n",
        encoding="utf-8",
    )

    with pytest.raises(RPCFault, match="changed after bundle import"):
        dispatcher.dispatch(
            "bundle.select_base_model",
            {
                "project_directory": str(root),
                "project_id": "project_1",
                "model_path": "sources/imported-bundle/two.inp",
            },
        )


def test_model_attestation_must_target_the_active_imported_revision(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    repository.close()
    archive = tmp_path / "bundle.zip"
    model = Path("benchmarks/cc0_reference/reference.inp").read_bytes()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("model/base.inp", model)

    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])
    imported = dispatcher.dispatch(
        "bundle.import",
        {
            "archive_path": str(archive),
            "expected_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expected_bytes": archive.stat().st_size,
            "destination": str(root / "sources" / "imported-bundle"),
            "project_directory": str(root),
            "project_id": "project_1",
        },
    )
    revision_id = imported["project_state"]["current_model_revision_id"]
    attested = dispatcher.dispatch(
        "model.attest",
        {
            "project_directory": str(root),
            "passport": {
                "project_id": "project_1",
                "model_revision_id": revision_id,
                "intended_use": "Determine whether the model is unsuitable for GI screening.",
                "disposition": "not_fit",
                "criteria": [
                    {
                        "criterion_id": "reference_reproduction",
                        "label": "Reference reproduction",
                        "status": "block",
                        "finding": "No approved reference results were supplied.",
                        "remediation": "Supply and reconcile approved reference results.",
                    }
                ],
                "permitted_outputs": ["learning evidence package"],
                "prohibited_uses": ["professional alternatives screening"],
                "unresolved_limitations": ["Reference reproduction is unresolved."],
                "reviewer": "Engineer A",
                "reviewer_attested": True,
            },
        },
    )

    assert attested["passport"]["disposition"] == "not_fit"
    assert attested["passport"]["model_revision_id"] == revision_id
    state = dispatcher.dispatch(
        "project.get_state",
        {"project_directory": str(root), "project_id": "project_1"},
    )
    assert state["state"]["model_fitness_passport_id"] == attested["passport"]["passport_id"]


def test_guided_session_upload_failure_does_not_change_replay_evidence(tmp_path) -> None:
    wandb = FailingWandb()
    events: list[dict[str, object]] = []
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        allowed_roots=[tmp_path],
        observability_adapters={"wandb": wandb},
        event_sink=events.append,
    )
    project = dispatcher.dispatch("project.create", {"kind": "demo"})
    root = Path(project["projectDirectory"])
    repository = ProjectRepository(root)
    try:
        _bind(repository, project["id"])
    finally:
        repository.close()
    dispatcher.observability.initialize_credentials("a" * 32, "https://api.wandb.ai")

    guided = dispatcher.dispatch(
        "guided.open",
        {"project_directory": str(root), "project_id": project["id"]},
    )
    assert guided["current_step_id"] == "overview"
    assert guided["guided_session_id"].startswith("guidedsession_")
    repository = ProjectRepository(root)
    try:
        before = _engineering_snapshot(repository)
        outbox = [
            item
            for item in repository.list_telemetry_outbox(project["id"])
            if item["envelope"]["event_type"] == "guided_session"
        ]
        assert len(outbox) == 1
        outbox_id = outbox[0]["outbox_id"]
        preview = dispatcher.observability.preview_upload(
            repository,
            TelemetryEnvelope.model_validate(outbox[0]["envelope"]),
        )
        assert preview["blockers"] == []
    finally:
        repository.close()
    _wait_for_upload(dispatcher, outbox_id)

    repository = ProjectRepository(root)
    try:
        assert _engineering_snapshot(repository) == before
        failed = repository.get_telemetry_outbox_item(outbox_id)
        assert failed["state"] == "failed", events
        assert failed["last_error_code"] == "WANDB_UPLOAD_RUNTIMEERROR"
        assert len(repository.list_guided_checkpoints(project["id"])) == len(guided["checkpoints"])
    finally:
        repository.close()
    scheduled: list[tuple[Path, str]] = []

    def record_retry(project_root: Path, retry_outbox_id: str) -> None:
        scheduled.append((project_root, retry_outbox_id))

    dispatcher._schedule_telemetry_sync = record_retry  # type: ignore[method-assign]
    retry = dispatcher.dispatch(
        "observability.retry",
        {"project_directory": str(root), "project_id": project["id"]},
    )
    assert retry["state"] == "retry_scheduled"
    assert retry["scheduled_outbox_ids"] == [outbox_id]
    assert scheduled == [(root.resolve(), outbox_id)]
    repository = ProjectRepository(root)
    try:
        assert repository.get_telemetry_outbox_item(outbox_id)["state"] == "pending"
    finally:
        repository.close()
    assert [upload.event_type for upload in wandb.uploads] == ["guided_session"]


def test_completed_analysis_uses_128_job_batches_and_exact_distinct_counts(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    _seed_completed_analysis(
        repository,
        job_count=129,
        analysis_payload={
            "model_revision_id": "revision_selected",
            "model_sha256": "a" * 64,
            "model_fitness_passport_id": "fitness_selected",
            "authoritative_base_model": "sources/imported/model/base.inp",
        },
    )
    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])

    queued = dispatcher._queue_completed_analysis_telemetry(repository, "project_1", "analysis_1")
    repeated = dispatcher._queue_completed_analysis_telemetry(repository, "project_1", "analysis_1")

    assert [item["envelope"]["metadata"]["job_count"] for item in queued] == [128, 1]
    expected_lineage = {
        "model_revision_id": "revision_selected",
        "model_sha256": "a" * 64,
        "model_fitness_passport_id": "fitness_selected",
        "authoritative_base_model": "sources/imported/model/base.inp",
    }
    metadata = queued[0]["envelope"]["metadata"]
    assert {key: metadata[key] for key in expected_lineage} == expected_lineage
    assert [item["outbox_id"] for item in repeated] == [item["outbox_id"] for item in queued]
    assert repository.table_counts()["telemetry_outbox"] == 2
    alternatives = queued[0]["envelope"]["metadata"]["tables"]["evaluated_alternatives"]
    assert len(alternatives["rows"]) == 129
    assert all(row[3:6] == [1, 1, 0] for row in alternatives["rows"])
    assert queued[1]["envelope"]["metadata"]["tables"]["evaluated_alternatives"]["rows"] == []

    changed = queued[0]["envelope"] | {
        "metadata": {**queued[0]["envelope"]["metadata"], "job_count": 127}
    }
    with pytest.raises(ValueError, match="different evidence"):
        dispatcher._enqueue_and_schedule_telemetry(
            repository, TelemetryEnvelope.model_validate(changed)
        )
    repository.close()


def test_completed_analysis_respects_byte_batch_boundary(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    _seed_completed_analysis(repository, job_count=2)
    monkeypatch.setattr(rpc_module, "TELEMETRY_BATCH_BYTE_LIMIT", 1)
    dispatcher = RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])

    queued = dispatcher._queue_completed_analysis_telemetry(repository, "project_1", "analysis_1")

    assert [item["envelope"]["metadata"]["job_count"] for item in queued] == [1, 1]
    repository.close()


def test_failed_automatic_analysis_upload_changes_only_outbox_state(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    _seed_completed_analysis(repository, job_count=2)
    before = _engineering_snapshot(repository)
    wandb = FailingWandb()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        allowed_roots=[tmp_path],
        observability_adapters={"wandb": wandb},
    )
    dispatcher.observability.initialize_credentials("a" * 32, "https://api.wandb.ai")

    queued = dispatcher._queue_completed_analysis_telemetry(repository, "project_1", "analysis_1")
    repository.close()
    for item in queued:
        _wait_for_upload(dispatcher, item["outbox_id"])

    repository = ProjectRepository(root)
    try:
        assert _engineering_snapshot(repository) == before
        assert all(
            repository.get_telemetry_outbox_item(item["outbox_id"])["state"] == "failed"
            for item in queued
        )
        assert repository.get_analysis("analysis_1")["state"] == "COMPLETED"
        assert repository.table_counts()["run_metrics"] == 4
        assert repository.table_counts()["run_artifacts"] == 6
    finally:
        repository.close()
    assert len(wandb.uploads) == len(queued)


def test_abandoned_upload_recovery_preserves_engineering_and_respects_active_set(tmp_path) -> None:
    root = tmp_path / "project"
    repository = _new_bound_project(root)
    content = b"portable exact evidence"
    digest, _ = repository.store_blob(content)
    binding = repository.get_observability_binding("project_1")
    envelope = TelemetryEnvelope(
        envelope_id="envelope_recovery",
        project_id="project_1",
        binding_id=binding["binding_id"],
        category="engineering_evidence",
        event_type="bundle_import",
        subject_id="recovery-fixture",
        idempotency_key="recovery-fixture",
        artifacts=[
            CloudArtifactManifest(
                artifact_id="artifact_recovery",
                project_id="project_1",
                category="model",
                logical_name="source/model.inp",
                sha256=digest,
                bytes=len(content),
                media_type="text/plain",
                source_object_id="recovery-fixture",
                local_blob_sha256=digest,
            )
        ],
    )
    queued = repository.enqueue_telemetry_envelope(envelope)
    uploading = repository.claim_telemetry_outbox_item(queued["outbox_id"])
    before = _engineering_snapshot(repository)

    assert repository.recover_abandoned_telemetry_uploads({uploading["outbox_id"]}) == []
    assert repository.get_telemetry_outbox_item(uploading["outbox_id"])["state"] == "uploading"
    due = repository.recover_abandoned_telemetry_uploads()

    assert [item["outbox_id"] for item in due] == [uploading["outbox_id"]]
    recovered = repository.get_telemetry_outbox_item(uploading["outbox_id"])
    assert recovered["state"] == "failed"
    assert recovered["last_error_code"] == "UPLOAD_INTERRUPTED"
    assert _engineering_snapshot(repository) == before
    repository.close()
