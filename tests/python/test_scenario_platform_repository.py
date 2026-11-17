from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scenario_platform.contracts import DirectJobState
from scenario_platform.repository import ScenarioRepository


def _planned_repository(tmp_path: Path, *, scenarios: int = 1):
    repository = ScenarioRepository(tmp_path / "project.sqlite")
    project_id = repository.create_project("Audited landscape", project_id="project-1")
    scenario_ids = []
    for index in range(scenarios):
        scenario_id = f"scenario-{index + 1}"
        scenario_ids.append(scenario_id)
        repository.add_scenario(
            project_id,
            scenario_id,
            {"hypothesis": index + 1},
            safeguard=index == scenarios - 1,
        )
    portfolio_id = repository.add_portfolio(
        project_id,
        ["site-a:none", "site-b:survey"],
        portfolio_id="portfolio-1",
    )
    analysis_id = repository.create_analysis(
        project_id,
        scenario_ids,
        analysis_id="analysis-1",
    )
    jobs = []
    for index, scenario_id in enumerate(scenario_ids):
        job_id = repository.plan_direct_job(
            analysis_id,
            portfolio_id,
            scenario_id,
            f"{index + 1:064x}",
            job_id=f"job-{index + 1}",
        )
        repository.transition_direct_job(job_id, DirectJobState.QUEUED)
        repository.transition_direct_job(job_id, DirectJobState.RUNNING)
        jobs.append(job_id)
    return repository, project_id, analysis_id, portfolio_id, jobs


def _success_evidence(job_id: str, marker: int = 1) -> dict:
    run_node = f"{job_id}-run"
    claim_node = f"{job_id}-claim"
    return {
        "artifacts": [
            {
                "role": "matrix",
                "sha256": f"{marker:064x}",
                "relative_path": f"runs/{job_id}/matrix.out",
                "media_type": "text/plain",
                "bytes": 123,
            }
        ],
        "metrics": [
            {
                "metric_id": "effective_resistance",
                "object_id": "core-a--core-b",
                "value": 12.5,
                "units": "model resistance units",
            }
        ],
        "qa_findings": [{"code": "SOLVER_OK", "severity": "INFO", "payload": {}}],
        "evidence_nodes": [
            {"node_id": run_node, "node_type": "direct_run", "payload": {}},
            {
                "node_id": claim_node,
                "node_type": "claim",
                "is_claim": True,
                "payload": {"metric_id": "effective_resistance"},
            },
        ],
        "evidence_edges": [
            {
                "source_node_id": claim_node,
                "target_node_id": run_node,
                "relation": "SUPPORTED_BY",
            }
        ],
        "telemetry_payload": {"job_id": job_id, "disposition": "SUCCEEDED"},
        "telemetry_idempotency_key": f"finalize:{job_id}",
    }


def test_finalize_direct_job_is_producer_atomic_idempotent_and_cached(tmp_path: Path) -> None:
    repository, _, _, _, jobs = _planned_repository(tmp_path)
    try:
        evidence = _success_evidence(jobs[0])
        outbox_id = repository.finalize_direct_job(jobs[0], DirectJobState.SUCCEEDED, **evidence)
        assert repository.get_job(jobs[0])["state"] == "SUCCEEDED"
        assert repository.count("direct_job_metrics") == 1
        assert repository.count("evidence_nodes") == 2
        assert repository.count("model_run_cache_entries") == 1
        assert repository.count("telemetry_outbox") == 1
        assert repository.cache_source(f"{1:064x}")["source_job_id"] == jobs[0]
        assert (
            repository.finalize_direct_job(jobs[0], DirectJobState.SUCCEEDED, **evidence)
            == outbox_id
        )

        changed = dict(evidence)
        changed["telemetry_payload"] = {"job_id": jobs[0], "disposition": "CHANGED"}
        with pytest.raises(ValueError, match="different evidence"):
            repository.finalize_direct_job(jobs[0], DirectJobState.SUCCEEDED, **changed)
    finally:
        repository.close()


def test_failed_finalization_rolls_back_science_cache_and_outbox(tmp_path: Path) -> None:
    repository, _, _, _, jobs = _planned_repository(tmp_path)
    try:
        evidence = _success_evidence(jobs[0])
        evidence["evidence_edges"] = [
            {
                "source_node_id": f"{jobs[0]}-claim",
                "target_node_id": "missing-node",
                "relation": "SUPPORTED_BY",
            }
        ]
        with pytest.raises(ValueError, match="same direct job"):
            repository.finalize_direct_job(jobs[0], DirectJobState.SUCCEEDED, **evidence)
        assert repository.get_job(jobs[0])["state"] == "RUNNING"
        for table in (
            "direct_job_artifacts",
            "direct_job_metrics",
            "evidence_nodes",
            "model_run_cache_entries",
            "telemetry_outbox",
        ):
            assert repository.count(table) == 0

        with pytest.raises(ValueError, match="only successful"):
            repository.finalize_direct_job(
                jobs[0],
                DirectJobState.FAILED,
                **_success_evidence(jobs[0]),
            )
    finally:
        repository.close()


def test_portfolio_is_comparable_only_after_complete_required_ensemble(tmp_path: Path) -> None:
    repository, _, analysis_id, portfolio_id, jobs = _planned_repository(tmp_path, scenarios=2)
    try:
        repository.finalize_direct_job(
            jobs[0], DirectJobState.SUCCEEDED, **_success_evidence(jobs[0], 1)
        )
        assert repository.comparable_portfolio_ids(analysis_id) == []
        repository.finalize_direct_job(
            jobs[1], DirectJobState.SUCCEEDED, **_success_evidence(jobs[1], 2)
        )
        assert repository.comparable_portfolio_ids(analysis_id) == [portfolio_id]
    finally:
        repository.close()


def test_cache_key_is_immutable_and_crash_recovery_requeues_owned_states(tmp_path: Path) -> None:
    repository, _, _, _, jobs = _planned_repository(tmp_path)
    try:
        with (
            pytest.raises(sqlite3.IntegrityError, match="cache key is immutable"),
            repository.transaction() as connection,
        ):
            connection.execute(
                "UPDATE direct_jobs SET model_run_cache_key=? WHERE job_id=?",
                ("f" * 64, jobs[0]),
            )
        record_id, outbox_id = repository.record_producer(
            "project-1",
            "bundle_import",
            "bundle-1",
            {"bundle_sha256": "a" * 64},
            telemetry_payload={"bundle": "bundle-1"},
            telemetry_idempotency_key="bundle:bundle-1",
        )
        claimed = repository.claim_outbox()
        assert claimed[0]["outbox_id"] == outbox_id
        recovery = repository.recover_abandoned()
        assert recovery == {"direct_jobs": 1, "outbox_items": 1}
        assert repository.get_job(jobs[0])["state"] == "QUEUED"
        assert repository.get_job(jobs[0])["attempt"] == 1

        repeated_record, repeated_outbox = repository.record_producer(
            "project-1",
            "bundle_import",
            "bundle-1",
            {"bundle_sha256": "a" * 64},
            telemetry_payload={"bundle": "bundle-1"},
            telemetry_idempotency_key="bundle:bundle-1",
        )
        assert (repeated_record, repeated_outbox) == (record_id, outbox_id)
    finally:
        repository.close()


def test_repository_event_sequence_is_transactional(tmp_path: Path) -> None:
    repository = ScenarioRepository(tmp_path / "events.sqlite")
    try:
        assert [repository.next_event_sequence("worker") for _ in range(3)] == [1, 2, 3]
        assert repository.next_event_sequence("other") == 1
    finally:
        repository.close()


def test_analysis_completion_and_telemetry_are_one_producer_transaction(tmp_path: Path) -> None:
    repository, project_id, analysis_id, _, _ = _planned_repository(tmp_path)
    try:
        repository.transition_analysis(analysis_id, "VALIDATING")
        repository.transition_analysis(analysis_id, "READY")
        repository.transition_analysis(analysis_id, "RUNNING")
        repository.record_producer(
            project_id,
            "bundle_import",
            "bundle-conflict",
            {"state": "imported"},
            telemetry_payload={"event": "imported"},
            telemetry_idempotency_key="shared-key",
        )
        with pytest.raises(ValueError, match="idempotency key"):
            repository.record_producer(
                project_id,
                "analysis_completion",
                analysis_id,
                {"state": "COMPLETED"},
                telemetry_payload={"event": "completed"},
                telemetry_idempotency_key="shared-key",
                analysis_id=analysis_id,
            )
        assert repository.get_analysis(analysis_id)["state"] == "RUNNING"

        repository.record_producer(
            project_id,
            "analysis_completion",
            analysis_id,
            {"state": "COMPLETED"},
            telemetry_payload={"event": "completed"},
            telemetry_idempotency_key=f"complete:{analysis_id}",
            analysis_id=analysis_id,
        )
        assert repository.get_analysis(analysis_id)["state"] == "COMPLETED"
    finally:
        repository.close()


def test_outbox_lease_expiry_reclaims_without_accepting_stale_owner(tmp_path: Path) -> None:
    repository, project_id, _, _, _ = _planned_repository(tmp_path)
    try:
        _, outbox_id = repository.record_producer(
            project_id,
            "bundle_import",
            "bundle-lease",
            {"bundle_sha256": "a" * 64},
            telemetry_payload={"bundle": "bundle-lease"},
            telemetry_idempotency_key="bundle:lease",
        )
        first = repository.claim_outbox(limit=1, lease_seconds=30)[0]
        assert first["outbox_id"] == outbox_id
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET lease_expires_at=? WHERE outbox_id=?",
                (expired, outbox_id),
            )
        second = repository.claim_outbox(limit=1, lease_seconds=30)[0]
        assert second["outbox_id"] == outbox_id
        assert second["lease_token"] != first["lease_token"]
        with pytest.raises(ValueError, match="active outbox lease"):
            repository.mark_outbox_synced(
                outbox_id, {"run_id": "stale"}, lease_token=first["lease_token"]
            )
        repository.mark_outbox_synced(
            outbox_id, {"run_id": "current"}, lease_token=second["lease_token"]
        )
    finally:
        repository.close()


def test_version_one_database_migrates_outbox_lease_columns(tmp_path: Path) -> None:
    database = tmp_path / "version-one.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE telemetry_outbox ("
            "outbox_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, analysis_id TEXT, "
            "event_type TEXT NOT NULL, subject_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "payload_sha256 TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, external_reference_json TEXT, "
            "last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "UNIQUE(project_id,idempotency_key))"
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()

    repository = ScenarioRepository(database)
    try:
        columns = {
            str(row[1])
            for row in repository._connection.execute("PRAGMA table_info(telemetry_outbox)")
        }
        assert {"lease_token", "lease_expires_at"}.issubset(columns)
        assert repository._connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        repository.close()
