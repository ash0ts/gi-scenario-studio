from __future__ import annotations

import hashlib
import json

import pytest
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault

SOLVER_SHA256 = "b" * 64


def _dispatcher(tmp_path) -> RPCDispatcher:
    return RPCDispatcher(data_root=tmp_path, allowed_roots=[tmp_path])


def _add_succeeded_job(
    repository: ProjectRepository,
    *,
    analysis_id: str,
    job_id: str = "job_exact_1",
    design_id: str = "design_1",
    storm_id: str = "storm_1",
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
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        },
        job_id=job_id,
    )
    repository.transition_solver_job(job_id, "QUEUED")
    repository.transition_solver_job(job_id, "RUNNING")
    repository.transition_solver_job(job_id, "SUCCEEDED")
    input_artifact = repository.add_run_artifact(job_id, "input", input_bytes)
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


def _seed_project(tmp_path, *, readiness: str = "DEMO") -> tuple[object, str, str]:
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Authoritative project",
        readiness=readiness,
        project_id="project_1",
    )
    analysis = repository.create_analysis("project_1", analysis_id="analysis_1")
    _add_succeeded_job(repository, analysis_id=analysis["analysis_id"])
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
    assert (
        result["hashes"]["input"]
        == hashlib.sha256(b"[TITLE]\n;; authoritative input\n").hexdigest()
    )
    assert result["hashes"]["output"] == hashlib.sha256(b"authoritative binary output").hexdigest()
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


def test_evidence_resolve_rejects_tampered_artifact_bytes(tmp_path):
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(title="Fixture", readiness="DEMO", project_id="project_1")
    analysis = repository.create_analysis("project_1", analysis_id="analysis_1")
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
    repository.create_project(title="Fixture", readiness="DEMO", project_id="project_1")
    repository.create_analysis("project_1", analysis_id="analysis_1")
    repository.create_analysis("project_1", analysis_id="analysis_2")
    _add_succeeded_job(repository, analysis_id="analysis_1")
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
                "extractor_version": "test",
            },
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


@pytest.mark.parametrize(
    ("mode", "readiness"),
    [
        ("learning", "DEMO"),
        ("draft", "ENGINEER_REVIEWED"),
        ("verified", "REFERENCE_MATCHED"),
    ],
)
def test_export_create_rebuilds_results_for_every_export_mode(tmp_path, mode, readiness):
    root, analysis_id, job_id = _seed_project(tmp_path, readiness=readiness)

    exported = _dispatcher(tmp_path).dispatch(
        "export.create",
        {
            "project_directory": str(root),
            "destination": str(tmp_path / f"export-{mode}"),
            "analysis_id": analysis_id,
            "job_ids": [job_id],
            "mode": mode,
            "extractor_version": "test",
            "include_artifacts": True,
        },
    )

    manifest = exported["manifest"]
    assert manifest["project_id"] == "project_1"
    assert manifest["analysis_id"] == analysis_id
    assert manifest["readiness"] == readiness
    assert manifest["solver_sha256"] == SOLVER_SHA256
    assert manifest["input_hashes"] == {
        f"{job_id}:input": hashlib.sha256(b"[TITLE]\n;; authoritative input\n").hexdigest()
    }
    results = json.loads(
        (tmp_path / f"export-{mode}" / "results.json").read_text(encoding="utf-8")
    )["results"]
    assert results[0]["run_id"] == job_id
    assert results[0]["metrics"]["flood_volume"] == 12.5
    assert results[0]["metrics"]["peak_hgl [J1]"] == 101.25
    assert any(name.endswith("/input.inp") for name in exported["files"])
    assert any(name.endswith("/report.rpt") for name in exported["files"])
    assert any(name.endswith("/output.out") for name in exported["files"])
