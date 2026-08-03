from __future__ import annotations

from datetime import UTC, datetime

import pytest
from gi_scenario_core.export import (
    CONCEPTUAL_NOTICE,
    DEMO_NOTICE,
    create_evidence_bundle,
    validate_export,
)
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.solver import SolverRunner, parse_report_qa, physics_cache_key


def _project(repo, readiness="CONCEPTUAL"):
    return repo.create_project(title="Fixture", readiness=readiness, project_id="project_1")


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


def test_repository_readiness_rejects_arbitrary_or_skipped_promotion(tmp_path):
    repo = ProjectRepository(tmp_path / "project")
    try:
        _project(repo, "CONCEPTUAL")
        with pytest.raises(ValueError, match="cannot be skipped"):
            repo.record_readiness(
                "project_1",
                "REFERENCE_MATCHED",
                gate_evidence={"reference_comparison": {"matched": True}},
            )
        with pytest.raises(ValueError, match="complete"):
            repo.record_readiness(
                "project_1",
                "ENGINEER_REVIEWED",
                gate_evidence={
                    "validation": {"blocking_count": 0},
                    "attestations": [{"key": "topology", "accepted": True}],
                    "reviewer": "Engineer",
                    "unreviewed_assumption_count": 0,
                },
            )
        evidence = {
            "validation": {"blocking_count": 0},
            "attestations": [
                {"key": key, "accepted": True}
                for key in ("topology", "geometry", "hydrology", "storms", "limitations")
            ],
            "reviewer": "Engineer",
            "unreviewed_assumption_count": 0,
        }
        assert (
            repo.record_readiness("project_1", "ENGINEER_REVIEWED", gate_evidence=evidence)[
                "readiness"
            ]
            == "ENGINEER_REVIEWED"
        )
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


def test_export_gates_professional_readiness_and_repository_verification(exact_result):
    check = validate_export(
        mode="verified",
        readiness="REFERENCE_MATCHED",
        results=[exact_result],
    )
    assert not check.allowed
    assert any("repository" in item for item in check.blockers)
    assert validate_export(
        mode="verified",
        readiness="REFERENCE_MATCHED",
        results=[exact_result],
        results_verified=True,
    ).allowed


@pytest.mark.parametrize(
    ("readiness", "notice"),
    [("CONCEPTUAL", CONCEPTUAL_NOTICE), ("DEMO", DEMO_NOTICE)],
)
def test_export_persists_readiness_notice_in_all_human_outputs(
    tmp_path, exact_result, readiness, notice
):
    result = create_evidence_bundle(
        tmp_path / readiness,
        project_id="project_1",
        project_title="Fixture",
        analysis_id="analysis_1",
        mode="learning",
        readiness=readiness,
        results=[exact_result],
        input_hashes={"base.inp": "a" * 64},
        solver_sha256="b" * 64,
        extractor_version="test",
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
        "readiness": "DEMO",
        "results": [exact_result],
        "input_hashes": {"base.inp": "a" * 64},
        "solver_sha256": "b" * 64,
        "extractor_version": "test",
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
