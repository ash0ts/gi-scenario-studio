from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from gi_scenario_core.contracts import (
    AlternativeDecisionSummary,
    DecisionBrief,
    DecisionCriterion,
    EvidenceClaim,
    ModelFitnessCriterion,
    ModelFitnessPassport,
    ModelRevision,
    ObservabilityBinding,
    ProjectDataPolicy,
    ProjectKind,
)
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault

SOLVER_SHA256 = "b" * 64
MODEL_SHA256 = "a" * 64
MODEL_REVISION_ID = "revision_1"
EXTRACTOR_VERSION = "test-report-extractor-v2"


def _analysis_payload() -> dict[str, str]:
    return {
        "contract": "exact-swmm-analysis-v2",
        "model_revision_id": MODEL_REVISION_ID,
        "model_sha256": MODEL_SHA256,
        "metric_extractor_version": EXTRACTOR_VERSION,
    }


def _add_v2_export_gate(repository: ProjectRepository, analysis_id: str) -> None:
    policy = ProjectDataPolicy(
        policy_id="policy_1",
        project_id="project_1",
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="Engineer A",
    )
    repository.upsert_project_data_policy(policy)
    binding = ObservabilityBinding(
        binding_id="binding_1",
        project_id="project_1",
        data_policy_id=policy.policy_id,
        entity="example-team",
        external_project="project-one-evidence",
        external_project_id="wandb-project-1",
        deployment="wandb_cloud",
        verified=True,
        verified_at="2026-01-01T00:00:00+00:00",
    )
    repository.connect_observability_binding(binding)
    revision = ModelRevision(
        revision_id=MODEL_REVISION_ID,
        project_id="project_1",
        reason="import",
        model_sha256=MODEL_SHA256,
        observability_binding_id=binding.binding_id,
        created_by="Engineer A",
    )
    repository.store_model_revision(revision)
    fitness_claim = EvidenceClaim(
        claim_id="claim_fitness",
        project_id="project_1",
        claim_type="fitness",
        statement="Reviewed model supports alternatives screening.",
        evidence_ids=["validation:model"],
    )
    reference_claim = EvidenceClaim(
        claim_id="claim_reference",
        project_id="project_1",
        claim_type="reference_comparison",
        statement="Approved reference results matched stated tolerances.",
        evidence_ids=["reference:comparison"],
    )
    passport = ModelFitnessPassport(
        passport_id="passport_1",
        project_id="project_1",
        model_revision_id=revision.revision_id,
        intended_use="GI alternatives screening",
        disposition="fit_for_stated_use",
        criteria=[
            ModelFitnessCriterion(
                criterion_id="model_quality",
                label="Model quality",
                status="pass",
                finding="Engineer review complete.",
                evidence_claim_ids=[fitness_claim.claim_id],
            )
        ],
        evidence_claims=[fitness_claim, reference_claim],
        reviewer="Engineer A",
        reviewer_attested=True,
    )
    repository.store_model_fitness_passport(passport)
    brief = DecisionBrief(
        brief_id=f"brief_{analysis_id}",
        project_id="project_1",
        analysis_id=analysis_id,
        model_revision_id=revision.revision_id,
        title="Alternatives decision brief",
        status="review_ready",
        baseline_design_id="baseline",
        intended_decision="Select alternatives for field investigation",
        model_fitness_passport_id=passport.passport_id,
        criteria=[
            DecisionCriterion(
                label="Flood volume",
                category="hydraulic",
                units="m3",
                direction="minimize",
                rationale="Reduce flooding under selected storms.",
            )
        ],
        alternatives=[
            AlternativeDecisionSummary(
                design_id="design_1",
                label="Selected alternative",
                qa_status="accepted",
                exact_run_ids=["job_exact_1"],
                evidence_claim_ids=[fitness_claim.claim_id],
            )
        ],
        full_network_adverse_effects_reviewed=True,
        evidence_claim_ids=[fitness_claim.claim_id],
    )
    repository.store_decision_brief(brief)


def _dispatcher(tmp_path) -> RPCDispatcher:
    return RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])


def _add_succeeded_job(
    repository: ProjectRepository,
    *,
    analysis_id: str,
    job_id: str = "job_exact_1",
    design_id: str = "design_1",
    storm_id: str = "storm_1",
    include_report: bool = True,
    job_model_revision_id: str = MODEL_REVISION_ID,
) -> dict[str, object]:
    input_bytes = b"[TITLE]\n;; authoritative input\n"
    output_bytes = b"authoritative binary output"
    report_bytes = b"Flow Routing Continuity\nContinuity Error (%) = 2.5\n"
    job = repository.create_solver_job(
        analysis_id=analysis_id,
        design_id=design_id,
        storm_id=storm_id,
        physics_cache_key=hashlib.sha256(job_id.encode()).hexdigest(),
        payload={
            "solver_sha256": SOLVER_SHA256,
            "qa_status": "accepted",
            "cache_hit": False,
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "model_revision_id": job_model_revision_id,
            "model_revision_sha256": MODEL_SHA256,
            "metric_extractor_version": EXTRACTOR_VERSION,
        },
        job_id=job_id,
    )
    repository.transition_solver_job(job_id, "QUEUED")
    repository.transition_solver_job(job_id, "RUNNING")
    repository.transition_solver_job(job_id, "SUCCEEDED")
    input_artifact = repository.add_run_artifact(job_id, "input", input_bytes)
    if include_report:
        repository.add_run_artifact(job_id, "report", report_bytes)
    repository.add_run_artifact(job_id, "output", output_bytes)
    repository.add_run_metric(job_id, "flood_volume", 12.5, "m3")
    repository.add_run_metric(job_id, "peak_hgl", 101.25, "m", object_id="J1")
    repository._connection.execute(
        "INSERT INTO qa_findings(job_id,severity,code,payload_json) VALUES(?,?,?,?)",
        (
            job_id,
            "warning",
            "flow_continuity_error",
            json.dumps({"message": "Flow continuity error is 2.5%."}),
        ),
    )
    return {"job": job, "input_artifact": input_artifact}


def _seed_project(
    tmp_path, *, project_kind: ProjectKind = ProjectKind.DEMO
) -> tuple[object, str, str]:
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Authoritative project",
        project_kind=project_kind,
        project_id="project_1",
    )
    analysis = repository.create_analysis(
        "project_1", analysis_id="analysis_1", payload=_analysis_payload()
    )
    _add_succeeded_job(repository, analysis_id=analysis["analysis_id"])
    _add_v2_export_gate(repository, analysis["analysis_id"])
    repository.close()
    return root, "analysis_1", "job_exact_1"


def test_evidence_resolve_reconstructs_only_authoritative_repository_state(tmp_path):
    root, analysis_id, job_id = _seed_project(tmp_path)

    result = _dispatcher(tmp_path).dispatch(
        "evidence.resolve",
        {"project_directory": str(root), "job_id": job_id},
    )

    assert result["run_id"] == job_id
    assert result["analysis_id"] == analysis_id
    assert result["model_revision_id"] == MODEL_REVISION_ID
    assert result["design_id"] == "design_1"
    assert result["storm_id"] == "storm_1"
    assert result["state"] == "SUCCEEDED"
    assert result["exact"] is True
    assert result["qa_status"] == "warning"
    assert result["metrics"] == {
        "flood_volume": 12.5,
        "peak_hgl [J1]": 101.25,
    }
    assert {record["units"] for record in result["metric_records"]} == {"m", "m3"}
    assert result["hashes"]["solver"] == SOLVER_SHA256
    assert result["extractor_version"] == EXTRACTOR_VERSION
    assert (
        result["hashes"]["input"]
        == hashlib.sha256(b"[TITLE]\n;; authoritative input\n").hexdigest()
    )
    assert result["hashes"]["output"] == hashlib.sha256(b"authoritative binary output").hexdigest()
    assert (
        result["hashes"]["report"]
        == hashlib.sha256(b"Flow Routing Continuity\nContinuity Error (%) = 2.5\n").hexdigest()
    )
    assert {artifact["role"] for artifact in result["artifacts"]} == {
        "input",
        "report",
        "output",
    }


def test_evidence_resolve_rejects_renderer_supplied_claims_and_unknown_jobs(tmp_path):
    root, _, job_id = _seed_project(tmp_path)
    dispatcher = _dispatcher(tmp_path)

    with pytest.raises(RPCFault, match="unexpected parameters"):
        dispatcher.dispatch(
            "evidence.resolve",
            {
                "project_directory": str(root),
                "job_id": job_id,
                "result": {
                    "design_id": "forged",
                    "metrics": {"flood_volume": 0},
                    "input_sha256": "0" * 64,
                },
            },
        )
    with pytest.raises(RPCFault, match="was not found"):
        dispatcher.dispatch(
            "evidence.resolve",
            {"project_directory": str(root), "job_id": "job_does_not_exist"},
        )


def test_local_analysis_plan_persists_and_rejects_reuse_after_model_revision_changes(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Revision-pinned planning fixture",
        project_kind=ProjectKind.EXISTING,
        project_id="project_1",
    )
    first_revision = ModelRevision(
        revision_id=MODEL_REVISION_ID,
        project_id="project_1",
        reason="import",
        model_sha256=MODEL_SHA256,
        created_by="Engineer A",
    )
    repository.store_model_revision(first_revision)
    repository.set_project_state(
        "project_1",
        {
            "current_model_revision_id": first_revision.revision_id,
            "current_model_sha256": first_revision.model_sha256,
            "selected_base_model": "storm.inp",
            "selected_model_dependencies": {},
        },
    )
    model_path = root / "storm.inp"
    model_path.write_bytes(Path("benchmarks/cc0_reference/reference.inp").read_bytes())
    (root / "companion.inp").write_bytes(model_path.read_bytes())
    repository.close()

    solver = tmp_path / "runswmm"
    solver.write_bytes(b"#!/bin/sh\nexit 0\n")
    solver.chmod(0o755)
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        allowed_roots=[tmp_path],
        solver_executable=solver,
        solver_sha256=hashlib.sha256(solver.read_bytes()).hexdigest(),
    )
    params = {
        "project_directory": str(root),
        "project_id": "project_1",
        "candidate_slots": [
            {
                "slot_id": "S1_BIO",
                "subcatchment_id": "S1",
                "lid_control": "BIOCELL",
                "lid_type": "BC",
                "base_units": 1,
                "min_additional_units": 0,
                "max_additional_units": 1,
                "unit_area": 200,
                "surface_width": 20,
                "initial_saturation_percent": 0,
                "from_impervious_percent": 15,
                "from_pervious": False,
                "route_to_pervious": False,
                "displaced_surface_area": 0,
                "feasibility_basis": "Exact planning test fixture",
                "engineer_approved": True,
            }
        ],
        "design_vectors": [[1]],
        "storm_cases": [{"storm_id": "storm_1", "model_path": "storm.inp", "dependencies": {}}],
        "budget": {"max_solver_jobs": 4},
        "validation": {"blocking_count": 0},
        "idempotency_key": "same-user-request",
    }
    with pytest.raises(RPCFault, match="unclassified companion INP"):
        dispatcher.dispatch(
            "analysis.plan",
            params
            | {
                "storm_cases": [
                    {
                        "storm_id": "companion",
                        "model_path": "companion.inp",
                        "dependencies": {},
                    }
                ],
                "idempotency_key": "unclassified-companion-request",
            },
        )
    first_plan = dispatcher.dispatch("analysis.plan", params)
    assert first_plan["run_plan"]["model_revision_id"] == MODEL_REVISION_ID

    repository = ProjectRepository(root)
    second_revision = ModelRevision(
        revision_id="revision_2",
        project_id="project_1",
        parent_revision_id=MODEL_REVISION_ID,
        reason="import",
        model_sha256="c" * 64,
        created_by="Engineer B",
    )
    repository.store_model_revision(second_revision)
    repository.set_project_state(
        "project_1",
        {
            "current_model_revision_id": second_revision.revision_id,
            "current_model_sha256": second_revision.model_sha256,
            "selected_base_model": "storm.inp",
            "selected_model_dependencies": {},
        },
    )
    repository.close()

    with pytest.raises(RPCFault, match="idempotency key was already used"):
        dispatcher.dispatch("analysis.plan", params)
    dispatcher._executor.shutdown(wait=True)


def test_evidence_resolve_rejects_tampered_artifact_bytes(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Fixture", project_kind=ProjectKind.DEMO, project_id="project_1"
    )
    analysis = repository.create_analysis(
        "project_1", analysis_id="analysis_1", payload=_analysis_payload()
    )
    _add_v2_export_gate(repository, analysis["analysis_id"])
    seeded = _add_succeeded_job(repository, analysis_id=analysis["analysis_id"])
    artifact_path = repository.root / seeded["input_artifact"]["relative_path"]
    repository.close()
    artifact_path.write_bytes(b"tampered after the solver completed")

    with pytest.raises(RPCFault, match="bytes do not match"):
        _dispatcher(tmp_path).dispatch(
            "evidence.resolve",
            {"project_directory": str(root), "job_id": "job_exact_1"},
        )


def test_export_create_rejects_cross_analysis_job_selection(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Fixture", project_kind=ProjectKind.DEMO, project_id="project_1"
    )
    repository.create_analysis("project_1", analysis_id="analysis_1", payload=_analysis_payload())
    repository.create_analysis("project_1", analysis_id="analysis_2", payload=_analysis_payload())
    _add_succeeded_job(repository, analysis_id="analysis_1")
    _add_v2_export_gate(repository, "analysis_2")
    repository.close()

    with pytest.raises(RPCFault, match="belongs to analysis"):
        _dispatcher(tmp_path).dispatch(
            "export.create",
            {
                "project_directory": str(root),
                "destination": str(tmp_path / "exports"),
                "analysis_id": "analysis_2",
                "job_ids": ["job_exact_1"],
                "mode": "learning",
            },
        )


def test_evidence_resolve_requires_retained_report_artifact(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Fixture", project_kind=ProjectKind.DEMO, project_id="project_1"
    )
    analysis = repository.create_analysis(
        "project_1", analysis_id="analysis_1", payload=_analysis_payload()
    )
    _add_v2_export_gate(repository, analysis["analysis_id"])
    _add_succeeded_job(
        repository,
        analysis_id=analysis["analysis_id"],
        include_report=False,
    )
    repository.close()

    with pytest.raises(RPCFault, match="missing required artifacts: report"):
        _dispatcher(tmp_path).dispatch(
            "evidence.resolve",
            {"project_directory": str(root), "job_id": "job_exact_1"},
        )
    with pytest.raises(RPCFault, match="missing required artifacts: report"):
        _dispatcher(tmp_path).dispatch(
            "results.decision_board",
            {"project_directory": str(root), "analysis_id": "analysis_1"},
        )


def test_evidence_resolve_rejects_job_bound_to_different_revision(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Fixture", project_kind=ProjectKind.DEMO, project_id="project_1"
    )
    analysis = repository.create_analysis(
        "project_1", analysis_id="analysis_1", payload=_analysis_payload()
    )
    _add_v2_export_gate(repository, analysis["analysis_id"])
    _add_succeeded_job(
        repository,
        analysis_id=analysis["analysis_id"],
        job_model_revision_id="revision_from_another_analysis",
    )
    repository.close()

    with pytest.raises(RPCFault, match="not bound to its analysis evidence versions"):
        _dispatcher(tmp_path).dispatch(
            "evidence.resolve",
            {"project_directory": str(root), "job_id": "job_exact_1"},
        )


def test_export_create_rejects_renderer_supplied_exact_results(tmp_path):
    root, analysis_id, job_id = _seed_project(tmp_path)

    with pytest.raises(RPCFault, match="derives authoritative evidence fields"):
        _dispatcher(tmp_path).dispatch(
            "export.create",
            {
                "project_directory": str(root),
                "destination": str(tmp_path / "exports"),
                "analysis_id": analysis_id,
                "job_ids": [job_id],
                "mode": "learning",
                "extractor_version": "test",
                "results": [{"metrics": {"flood_volume": 0}}],
            },
        )


def test_export_create_rebuilds_results_for_learning_export(tmp_path):
    root, analysis_id, job_id = _seed_project(tmp_path)
    mode = "learning"

    exported = _dispatcher(tmp_path).dispatch(
        "export.create",
        {
            "project_directory": str(root),
            "destination": str(tmp_path / f"export-{mode}"),
            "analysis_id": analysis_id,
            "job_ids": [job_id],
            "mode": mode,
            "include_artifacts": True,
        },
    )

    manifest = exported["manifest"]
    assert manifest["project_id"] == "project_1"
    assert manifest["analysis_id"] == analysis_id
    assert manifest["model_revision_id"] == "revision_1"
    assert manifest["model_fitness_passport_id"] == "passport_1"
    assert manifest["package_type"] == mode
    assert manifest["solver_sha256"] == SOLVER_SHA256
    assert manifest["extractor_version"] == EXTRACTOR_VERSION
    assert manifest["input_hashes"] == {
        f"{job_id}:input": hashlib.sha256(b"[TITLE]\n;; authoritative input\n").hexdigest()
    }
    results = json.loads(
        (tmp_path / f"export-{mode}" / "results.json").read_text(encoding="utf-8")
    )["results"]
    assert results[0]["run_id"] == job_id
    assert results[0]["metrics"]["flood_volume"] == 12.5
    assert results[0]["metrics"]["peak_hgl [J1]"] == 101.25
    csv_header = (
        (tmp_path / f"export-{mode}" / "results.csv").read_text(encoding="utf-8").splitlines()[0]
    )
    assert "model_revision_id" in csv_header
    assert "report_sha256" in csv_header
    assert "extractor_version" in csv_header
    assert any(name.endswith("/input.inp") for name in exported["files"])
    assert any(name.endswith("/report.rpt") for name in exported["files"])
    assert any(name.endswith("/output.out") for name in exported["files"])
    assert exported["cloud_sync"]["state"] == "pending"
    repository = ProjectRepository(root)
    try:
        assert repository.table_counts()["exports"] == 1
        queued = repository.get_telemetry_outbox_item(exported["cloud_sync"]["outbox_id"])
        assert queued["envelope"]["event_type"] == "evidence_export"
        assert queued["envelope"]["subject_id"] == manifest["export_id"]
        assert queued["envelope"]["metadata"]["file_count"] == len(exported["files"])
        assert {artifact["logical_name"] for artifact in queued["envelope"]["artifacts"]} == {
            f"exports/{manifest['export_id']}/{name}" for name in exported["files"]
        }
        for artifact in queued["envelope"]["artifacts"]:
            blob = root / "blobs" / "sha256" / artifact["local_blob_sha256"]
            assert hashlib.sha256(blob.read_bytes()).hexdigest() == artifact["sha256"]
    finally:
        repository.close()


def test_export_uses_passport_for_analysis_revision_not_latest_project_passport(tmp_path):
    root, analysis_id, job_id = _seed_project(tmp_path)
    repository = ProjectRepository(root)
    try:
        repository.store_model_revision(
            ModelRevision(
                revision_id="revision_2",
                project_id="project_1",
                parent_revision_id=MODEL_REVISION_ID,
                reason="import",
                model_sha256="c" * 64,
                observability_binding_id="binding_1",
                created_by="Engineer B",
            )
        )
        repository.store_model_fitness_passport(
            ModelFitnessPassport(
                passport_id="passport_2",
                project_id="project_1",
                model_revision_id="revision_2",
                intended_use="Future model review",
                disposition="not_assessed",
            )
        )
    finally:
        repository.close()

    exported = _dispatcher(tmp_path).dispatch(
        "export.create",
        {
            "project_directory": str(root),
            "destination": str(tmp_path / "revision-pinned-export"),
            "analysis_id": analysis_id,
            "job_ids": [job_id],
            "mode": "learning",
        },
    )

    assert exported["manifest"]["model_revision_id"] == MODEL_REVISION_ID
    assert exported["manifest"]["model_fitness_passport_id"] == "passport_1"


def test_export_manifest_persistence_failure_is_not_reported_as_cloud_failure(
    tmp_path, monkeypatch
):
    root, analysis_id, job_id = _seed_project(tmp_path)

    def reject_manifest(*_args, **_kwargs):
        raise ValueError("authoritative export manifest write failed")

    monkeypatch.setattr(ProjectRepository, "store_export_manifest", reject_manifest)
    with pytest.raises(RPCFault, match="authoritative export manifest write failed"):
        _dispatcher(tmp_path).dispatch(
            "export.create",
            {
                "project_directory": str(root),
                "destination": str(tmp_path / "unregistered-export"),
                "analysis_id": analysis_id,
                "job_ids": [job_id],
                "mode": "learning",
            },
        )

    repository = ProjectRepository(root)
    try:
        assert repository.table_counts()["exports"] == 0
        assert repository.table_counts()["telemetry_outbox"] == 0
    finally:
        repository.close()


@pytest.mark.parametrize("mode", ["screening", "verified_alternatives"])
def test_export_create_fails_closed_for_professional_modes(tmp_path, mode):
    root, analysis_id, job_id = _seed_project(tmp_path)

    with pytest.raises(RPCFault, match="repository-backed full-network"):
        _dispatcher(tmp_path).dispatch(
            "export.create",
            {
                "project_directory": str(root),
                "destination": str(tmp_path / f"export-{mode}"),
                "analysis_id": analysis_id,
                "job_ids": [job_id],
                "mode": mode,
            },
        )


def _draft_decision_brief_payload() -> dict[str, object]:
    return {
        "brief_id": "brief_rpc_draft",
        "project_id": "project_1",
        "analysis_id": "analysis_1",
        "model_revision_id": MODEL_REVISION_ID,
        "title": "Alternatives decision brief",
        "status": "draft",
        "baseline_design_id": "baseline",
        "intended_decision": "Select alternatives for field investigation",
        "model_fitness_passport_id": "passport_1",
        "criteria": [
            {
                "label": "Flood volume",
                "category": "hydraulic",
                "units": "m3",
                "direction": "minimize",
                "rationale": "Reduce flooding under selected storms.",
            }
        ],
        "alternatives": [
            {
                "design_id": "design_1",
                "label": "Selected alternative",
                "qa_status": "warning",
                "exact_run_ids": ["job_exact_1"],
                "evidence_claim_ids": ["claim_fitness", "job_exact_1"],
            }
        ],
        "evidence_claim_ids": ["claim_fitness", "job_exact_1"],
    }


def test_decision_brief_update_resolves_revision_passport_and_exact_jobs(tmp_path):
    root, _, _ = _seed_project(tmp_path)

    stored = _dispatcher(tmp_path).dispatch(
        "decision_brief.update",
        {
            "project_directory": str(root),
            "brief": _draft_decision_brief_payload(),
        },
    )

    assert stored["model_revision_id"] == MODEL_REVISION_ID
    assert stored["alternatives"][0]["exact_run_ids"] == ["job_exact_1"]
    assert stored["alternatives"][0]["qa_status"] == "warning"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda brief: brief.update({"project_id": "another_project"}),
            "different projects",
        ),
        (
            lambda brief: brief.update({"model_revision_id": "forged_revision"}),
            "does not match its analysis",
        ),
        (
            lambda brief: brief["alternatives"][0].update({"exact_run_ids": ["forged_job"]}),
            "complete design-by-storm job set",
        ),
        (
            lambda brief: brief["alternatives"][0].update({"qa_status": "accepted"}),
            "QA status does not match",
        ),
        (
            lambda brief: brief.update({"full_network_adverse_effects_reviewed": True}),
            "repository-backed full-network",
        ),
    ],
)
def test_decision_brief_update_rejects_forged_authority(tmp_path, mutate, message):
    root, _, _ = _seed_project(tmp_path)
    brief = _draft_decision_brief_payload()
    mutate(brief)

    with pytest.raises(RPCFault, match=message):
        _dispatcher(tmp_path).dispatch(
            "decision_brief.update",
            {"project_directory": str(root), "brief": brief},
        )


def test_model_attest_cannot_claim_fit_without_reference_comparison_service(tmp_path):
    root, _, _ = _seed_project(tmp_path)
    repository = ProjectRepository(root)
    try:
        passport = repository.get_model_fitness_passport_by_id("passport_1")
    finally:
        repository.close()
    passport["passport_id"] = "passport_renderer_fit_claim"

    with pytest.raises(RPCFault, match="repository-backed successful reference-comparison"):
        _dispatcher(tmp_path).dispatch(
            "model.attest",
            {"project_directory": str(root), "passport": passport},
        )
