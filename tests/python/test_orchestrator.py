from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from gi_scenario_core.contracts import CandidateSlot, ProjectKind
from gi_scenario_core.orchestrator import (
    AnalysisOrchestrator,
    MetricExtractionError,
    StormModel,
)
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.solver import QAFinding, SolverOutcome
from gi_scenario_core.swmm import SwmmDocument


class FakeExactSolver:
    """Writes deterministic SWMM-shaped evidence without invoking a binary."""

    solver_sha256 = "5" * 64

    def __init__(self, *, omit_outfall: bool = False) -> None:
        self.calls: list[str] = []
        self.omit_outfall = omit_outfall

    def run(
        self,
        *,
        model_bytes,
        work_directory,
        dependencies=None,
        qa_policy=None,
        cancel_event=None,
    ) -> SolverOutcome:
        work = Path(work_directory)
        work.mkdir(parents=True, exist_ok=True)
        (work / "input.inp").write_bytes(model_bytes)
        document = _document(model_bytes)
        units = next(
            int(row.tokens[2])
            for row in document.data_rows("LID_USAGE")
            if row.tokens[:2] == ["S1", "BIOCELL"]
        )
        is_alternative = units > 1
        is_storm_b = "TEST_STORM_B" in model_bytes.decode("utf-8")
        peak = (
            4.2
            if is_alternative and is_storm_b
            else 2.8
            if is_alternative
            else 4.0
            if is_storm_b
            else 3.0
        )
        volume = (
            0.18
            if is_alternative and is_storm_b
            else 0.09
            if is_alternative
            else 0.20
            if is_storm_b
            else 0.10
        )
        outfall_row = "" if self.omit_outfall else f"OUT1 100.0 1.0 {peak} {volume}\n"
        report = (
            "Runoff Quantity Continuity\n"
            "Continuity Error (%) = 0.10\n"
            "Flow Routing Continuity\n"
            "Continuity Error (%) = 0.20\n"
            "Flooding Loss 0.000\n"
            "Outfall Loading Summary\n"
            f"{outfall_row}"
        ).encode()
        output = b"binary-output:" + hashlib.sha256(model_bytes).digest()
        (work / "report.rpt").write_bytes(report)
        (work / "output.out").write_bytes(output)
        storm = "storm_b" if is_storm_b else "storm_a"
        self.calls.append(f"{storm}:{units}")
        return SolverOutcome(
            return_code=0,
            input_sha256=hashlib.sha256(model_bytes).hexdigest(),
            solver_sha256=self.solver_sha256,
            report_sha256=hashlib.sha256(report).hexdigest(),
            output_sha256=hashlib.sha256(output).hexdigest(),
            qa_status="accepted",
            findings=[
                QAFinding(
                    code="direct_fixture",
                    severity="info",
                    message="Fake exact solver evidence used only by the unit test.",
                )
            ],
        )


def _document(content: bytes) -> SwmmDocument:
    return SwmmDocument.parse(content.decode("utf-8"))


def _model(storm: str) -> bytes:
    source = Path("benchmarks/cc0_reference/reference.inp").read_bytes()
    return source + f"\n; TEST_STORM_{storm.upper()}\n".encode()


def _slot() -> CandidateSlot:
    return CandidateSlot(
        slot_id="S1_BIO",
        subcatchment_id="S1",
        lid_control="BIOCELL",
        lid_type="BC",
        base_units=1,
        min_additional_units=0,
        max_additional_units=1,
        unit_area=200,
        surface_width=20,
        from_impervious_percent=15,
        drain_to="J1",
        feasibility_basis="Self-authored exact-orchestration test fixture",
        engineer_approved=True,
    )


def _storms() -> list[StormModel]:
    # Deliberately reversed: planning must normalize storm order.
    return [
        StormModel("storm_b", _model("b")),
        StormModel("storm_a", _model("a")),
    ]


def _repository(tmp_path) -> ProjectRepository:
    repository = ProjectRepository(tmp_path / "project")
    repository.create_project(
        title="Exact fixture", project_kind=ProjectKind.DEMO, project_id="project_1"
    )
    return repository


def test_exact_analysis_persists_direct_evidence_and_projects_adverse_changes(tmp_path):
    repository = _repository(tmp_path)
    solver = FakeExactSolver()
    try:
        analysis = repository.create_analysis("project_1", analysis_id="analysis_1")
        orchestrator = AnalysisOrchestrator(repository, solver)
        plan = orchestrator.plan(
            analysis["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[[1]],
            storms=_storms(),
        )

        assert plan == {
            "analysis_id": "analysis_1",
            "state": "READY",
            "exact": True,
            "design_count": 2,
            "alternative_count": 1,
            "storm_count": 2,
            "planned_solver_jobs": 4,
            "job_ids": plan["job_ids"],
        }
        assert len(plan["job_ids"]) == len(set(plan["job_ids"])) == 4
        assert repository.table_counts()["designs"] == 2
        assert repository.table_counts()["solver_jobs"] == 4
        assert {repository.get_solver_job(job_id)["state"] for job_id in plan["job_ids"]} == {
            "QUEUED"
        }

        events: list[dict] = []
        result = orchestrator.run(
            "analysis_1",
            candidate_slots=[_slot()],
            storms=_storms(),
            on_progress=events.append,
        )

        assert result["state"] == "COMPLETED"
        assert result["job_state_counts"] == {"SUCCEEDED": 4}
        assert solver.calls == ["storm_a:1", "storm_b:1", "storm_a:2", "storm_b:2"]
        assert repository.table_counts()["run_artifacts"] == 12
        assert repository.table_counts()["run_metrics"] == 28
        assert repository._connection.execute("SELECT count(*) FROM qa_findings").fetchone()[0] == 4
        assert events[-1]["event"] == "analysis.completed"

        for job_id in plan["job_ids"]:
            roles = {
                row["role"]
                for row in repository._connection.execute(
                    "SELECT role FROM run_artifacts WHERE job_id=?", (job_id,)
                ).fetchall()
            }
            assert roles == {"input", "report", "output"}

        board = orchestrator.decision_board("analysis_1")
        assert board["exact_only"] is True
        assert board["blockers"] == []
        assert board["excluded_designs"] == []
        assert board["baseline"]["vector"] == [0]
        assert len(board["alternatives"]) == 1
        alternative = board["alternatives"][0]
        assert alternative["vector"] == [1]
        assert {
            (item["storm_id"], item["object_id"]) for item in alternative["adverse_peak_changes"]
        } == {("storm_b", ""), ("storm_b", "OUT1")}
        assert all(
            item["delta"] == pytest.approx(0.2) for item in alternative["adverse_peak_changes"]
        )
        assert all(
            item["baseline_job_id"] and item["alternative_job_id"]
            for item in alternative["adverse_peak_changes"]
        )

        # A completed analysis is idempotently reused; no fake solver call repeats.
        repeated = orchestrator.run(
            "analysis_1",
            candidate_slots=[_slot()],
            storms=_storms(),
        )
        assert repeated["cache_hits"] == 4
        assert len(solver.calls) == 4

        cached_analysis = repository.create_analysis("project_1", analysis_id="analysis_cached")
        cached_plan = orchestrator.plan(
            cached_analysis["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[[1]],
            storms=_storms(),
        )
        cached = orchestrator.run(
            cached_analysis["analysis_id"],
            candidate_slots=[_slot()],
            storms=_storms(),
        )
        assert cached["state"] == "COMPLETED"
        assert cached["cache_hits"] == 4
        assert len(solver.calls) == 4
        assert all(
            repository.get_solver_job(job_id)["payload"]["cache_hit"]
            for job_id in cached_plan["job_ids"]
        )
    finally:
        repository.close()


def test_pause_resume_cancel_and_abandoned_job_recovery_are_durable(tmp_path):
    repository = _repository(tmp_path)
    solver = FakeExactSolver()
    try:
        paused = repository.create_analysis("project_1", analysis_id="analysis_paused")
        orchestrator = AnalysisOrchestrator(repository, solver)
        orchestrator.plan(
            paused["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[[1]],
            storms=_storms(),
        )
        paused_status = orchestrator.run(
            paused["analysis_id"],
            candidate_slots=[_slot()],
            storms=_storms(),
            should_pause=lambda: True,
        )
        assert paused_status["state"] == "PAUSED"
        assert solver.calls == []
        pre_run_board = orchestrator.decision_board(paused["analysis_id"])
        assert pre_run_board["alternatives"] == []
        assert pre_run_board["blockers"] == ["No complete exact baseline ensemble is available."]

        jobs = orchestrator._jobs(paused["analysis_id"])
        repository.transition_analysis(paused["analysis_id"], "RUNNING")
        repository.transition_solver_job(jobs[0]["job_id"], "RUNNING")
        recovery = orchestrator.recover(paused["analysis_id"])
        assert recovery["requeued_job_ids"] == [jobs[0]["job_id"]]
        assert repository.get_solver_job(jobs[0]["job_id"])["state"] == "QUEUED"

        completed = orchestrator.run(
            paused["analysis_id"],
            candidate_slots=[_slot()],
            storms=_storms(),
        )
        assert completed["state"] == "COMPLETED"

        cancelled = repository.create_analysis("project_1", analysis_id="analysis_cancelled")
        orchestrator.plan(
            cancelled["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[[1]],
            storms=_storms(),
        )
        calls_before_cancel = len(solver.calls)
        cancelled_status = orchestrator.run(
            cancelled["analysis_id"],
            candidate_slots=[_slot()],
            storms=_storms(),
            should_cancel=lambda: True,
        )
        assert cancelled_status["state"] == "CANCELLED"
        assert cancelled_status["job_state_counts"] == {"CANCELLED": 4}
        assert len(solver.calls) == calls_before_cancel
    finally:
        repository.close()


def test_cache_does_not_reuse_metrics_across_extractor_versions(tmp_path):
    repository = _repository(tmp_path)
    solver = FakeExactSolver()
    try:
        first = repository.create_analysis(
            "project_1",
            analysis_id="analysis_extractor_v1",
            payload={"metric_extractor_version": "extractor-v1"},
        )
        orchestrator = AnalysisOrchestrator(repository, solver)
        orchestrator.plan(
            first["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[],
            storms=[StormModel("storm_a", _model("a"))],
        )
        first_status = orchestrator.run(
            first["analysis_id"],
            candidate_slots=[_slot()],
            storms=[StormModel("storm_a", _model("a"))],
        )
        assert first_status["cache_hits"] == 0
        assert len(solver.calls) == 1

        second = repository.create_analysis(
            "project_1",
            analysis_id="analysis_extractor_v2",
            payload={"metric_extractor_version": "extractor-v2"},
        )
        orchestrator.plan(
            second["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[],
            storms=[StormModel("storm_a", _model("a"))],
        )
        second_status = orchestrator.run(
            second["analysis_id"],
            candidate_slots=[_slot()],
            storms=[StormModel("storm_a", _model("a"))],
        )

        assert second_status["cache_hits"] == 0
        assert len(solver.calls) == 2
    finally:
        repository.close()


def test_missing_direct_report_metrics_fail_closed_and_never_become_claims(tmp_path):
    repository = _repository(tmp_path)
    try:
        analysis = repository.create_analysis("project_1", analysis_id="analysis_bad_report")
        orchestrator = AnalysisOrchestrator(repository, FakeExactSolver(omit_outfall=True))
        plan = orchestrator.plan(
            analysis["analysis_id"],
            candidate_slots=[_slot()],
            design_vectors=[],
            storms=[StormModel("storm_a", _model("a"))],
        )
        with pytest.raises(MetricExtractionError, match="outfall rows"):
            orchestrator.run(
                analysis["analysis_id"],
                candidate_slots=[_slot()],
                storms=[StormModel("storm_a", _model("a"))],
            )
        job = repository.get_solver_job(plan["job_ids"][0])
        assert job["state"] == "FAILED"
        assert repository.get_analysis(analysis["analysis_id"])["state"] == "FAILED"
        assert repository.table_counts()["run_metrics"] == 0
        assert repository.table_counts()["run_artifacts"] == 3
        assert orchestrator.decision_board(analysis["analysis_id"])["alternatives"] == []
    finally:
        repository.close()
