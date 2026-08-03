from __future__ import annotations

from datetime import UTC, datetime

import pytest
from gi_scenario_core.contracts import (
    EvidenceClaim,
    ModelFitnessCriterion,
    ModelFitnessPassport,
    ProjectKind,
)
from gi_scenario_core.export import (
    create_evidence_bundle,
    validate_export,
)
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.solver import SolverRunner, parse_report_qa, physics_cache_key


def _project(repo, project_kind=ProjectKind.CONCEPTUAL):
    return repo.create_project(title="Fixture", project_kind=project_kind, project_id="project_1")


def _fitness_passport(
    disposition: str = "fit_for_stated_use",
    *,
    reference_comparison: bool = False,
) -> ModelFitnessPassport:
    claims = [
        EvidenceClaim(
            claim_id="claim_fitness",
            project_id="project_1",
            claim_type="fitness",
            statement="Model checks support the stated screening use.",
            evidence_ids=["validation:model"],
        )
    ]
    if reference_comparison:
        claims.append(
            EvidenceClaim(
                claim_id="claim_reference",
                project_id="project_1",
                claim_type="reference_comparison",
                statement="Approved reference comparison passed stated tolerances.",
                evidence_ids=["reference:comparison"],
            )
        )
    if disposition == "not_assessed":
        return ModelFitnessPassport(
            passport_id="passport_1",
            project_id="project_1",
            model_revision_id="revision_1",
            intended_use="GI alternatives screening",
        )
    status = "block" if disposition == "not_fit" else "pass"
    return ModelFitnessPassport(
        passport_id="passport_1",
        project_id="project_1",
        model_revision_id="revision_1",
        intended_use="GI alternatives screening",
        disposition=disposition,
        criteria=[
            ModelFitnessCriterion(
                criterion_id="model_quality",
                label="Model quality",
                status=status,
                finding="Engineer review recorded.",
                evidence_claim_ids=["claim_fitness"],
            )
        ],
        evidence_claims=claims,
        reviewer="Engineer A",
        reviewer_attested=True,
    )


def test_repository_state_transitions_are_guarded_and_idempotent(tmp_path):
    repo = ProjectRepository(tmp_path / "project")
    try:
        _project(repo)
        first = repo.create_analysis("project_1", idempotency_key="request-1")
        second = repo.create_analysis("project_1", idempotency_key="request-1")
        assert first["analysis_id"] == second["analysis_id"]
        with pytest.raises(ValueError, match="invalid"):
            repo.transition_analysis(first["analysis_id"], "RUNNING")
        repo.transition_analysis(first["analysis_id"], "VALIDATING")
        repo.transition_analysis(first["analysis_id"], "READY")
        assert repo.transition_analysis(first["analysis_id"], "RUNNING")["state"] == "RUNNING"
    finally:
        repo.close()


def test_project_kind_and_model_fitness_are_independent_repository_records(tmp_path):
    repo = ProjectRepository(tmp_path / "project")
    try:
        project = _project(repo, ProjectKind.CONCEPTUAL)
        assert project["project_kind"] == "conceptual"
        assert repo.get_model_fitness_passport("project_1") is None

        # The repository requires a versioned model revision before it will
        # accept an independently signed fitness passport. The full revision
        # boundary is exercised by the observability tests; this test verifies
        # that project kind itself never implies a fitness disposition.
        assert "fitness" not in project
        assert "disposition" not in project
    finally:
        repo.close()


def test_run_metrics_require_succeeded_job_and_recovery_requeues(tmp_path):
    repo = ProjectRepository(tmp_path / "project")
    try:
        _project(repo)
        analysis = repo.create_analysis("project_1")
        job = repo.create_solver_job(
            analysis_id=analysis["analysis_id"],
            design_id="design_1",
            storm_id="storm_1",
            physics_cache_key="f" * 64,
        )
        with pytest.raises(Exception, match="succeeded"):
            repo.add_run_metric(job["job_id"], "flood", 1.0, "m3")
        repo.transition_solver_job(job["job_id"], "QUEUED")
        repo.transition_solver_job(job["job_id"], "RUNNING")
        assert repo.recover_abandoned_jobs() == [job["job_id"]]
        repo.transition_solver_job(job["job_id"], "RUNNING")
        repo.transition_solver_job(job["job_id"], "SUCCEEDED")
        repo.add_run_metric(job["job_id"], "flood", 1.0, "m3")
        assert repo.table_counts()["run_metrics"] == 1
    finally:
        repo.close()


def test_content_addressed_blob_is_deduplicated(tmp_path):
    repo = ProjectRepository(tmp_path / "project")
    try:
        one = repo.store_blob(b"same")
        two = repo.store_blob(b"same")
        assert one == two
        assert (repo.root / one[1]).read_bytes() == b"same"
    finally:
        repo.close()


def test_export_gates_model_fitness_reference_evidence_and_repository_verification(exact_result):
    check = validate_export(
        project_id="project_1",
        analysis_id="analysis_1",
        mode="verified_alternatives",
        model_fitness_passport=_fitness_passport(reference_comparison=True),
        results=[exact_result],
        full_network_adverse_effects_reviewed=True,
    )
    assert not check.allowed
    assert any("repository" in item for item in check.blockers)
    verified = validate_export(
        project_id="project_1",
        analysis_id="analysis_1",
        mode="verified_alternatives",
        model_fitness_passport=_fitness_passport(reference_comparison=True),
        results=[exact_result],
        results_verified=True,
        full_network_adverse_effects_reviewed=True,
    )
    assert not verified.allowed
    assert any("full-network" in item for item in verified.blockers)
    assert any("reference comparison" in item for item in verified.blockers)


def test_export_persists_fitness_notice_in_all_human_outputs(tmp_path, exact_result):
    notice = "Model fitness not assessed — learning evidence only."
    result = create_evidence_bundle(
        tmp_path / "learning",
        project_id="project_1",
        project_title="Fixture",
        analysis_id="analysis_1",
        mode="learning",
        model_fitness_passport=_fitness_passport("not_assessed"),
        results=[exact_result],
        input_hashes={"base.inp": "a" * 64},
        solver_sha256="b" * 64,
        extractor_version="test",
        results_verified=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for filename in ("results.csv", "screening-appendix.md", "screening-appendix.html"):
        with open(result.files[filename], encoding="utf-8") as output:
            assert notice in output.read()


def test_export_is_deterministic_for_fixed_timestamp(tmp_path, exact_result):
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    kwargs = {
        "project_id": "project_1",
        "project_title": "Fixture",
        "analysis_id": "analysis_1",
        "mode": "learning",
        "model_fitness_passport": _fitness_passport("not_assessed"),
        "results": [exact_result],
        "input_hashes": {"base.inp": "a" * 64},
        "solver_sha256": "b" * 64,
        "extractor_version": "test",
        "results_verified": True,
        "created_at": timestamp,
    }
    first = create_evidence_bundle(tmp_path / "one", **kwargs)
    second = create_evidence_bundle(tmp_path / "two", **kwargs)
    for name in first.manifest.files:
        with (
            open(first.files[name], "rb") as first_file,
            open(second.files[name], "rb") as second_file,
        ):
            assert first_file.read() == second_file.read()


def test_report_qa_and_missing_output_are_blocking(tmp_path):
    findings = parse_report_qa(
        "Runoff Quantity Continuity\n"
        "Inflow Loss Outflow Storage Error\n"
        "Continuity Error (%) = 3.5\n"
        "WARNING 01: fixture"
    )
    assert any(
        item.code == "runoff_continuity_error" and item.severity == "warning" for item in findings
    )
    assert not any(item.code == "swmm_error" for item in findings)
    assert any(
        item.code == "swmm_error" and item.severity == "blocking"
        for item in parse_report_qa("ERROR 103: the rainfall file could not be opened")
    )

    executable = tmp_path / "fake-swmm"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        "pathlib.Path(sys.argv[2]).write_text('Flow Routing Continuity\\nContinuity Error (%) = 0.1\\n')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    outcome = SolverRunner(executable).run(
        model_bytes=b"[OPTIONS]\nFLOW_UNITS CFS\n", work_directory=tmp_path / "run"
    )
    assert outcome.qa_status == "rejected"
    assert any(item.code == "missing_output" for item in outcome.findings)


def test_physics_cache_key_is_order_independent():
    first = physics_cache_key(
        model_bytes=b"m", dependency_hashes={"b": "2", "a": "1"}, solver_sha256="s"
    )
    second = physics_cache_key(
        model_bytes=b"m", dependency_hashes={"a": "1", "b": "2"}, solver_sha256="s"
    )
    assert first == second
