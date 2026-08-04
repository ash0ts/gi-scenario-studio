from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import (
    ConnectivityDecisionRule,
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    QAStatus,
    RuleMetric,
    RuleOperator,
)
from ecocorridor_core.metrics import build_pair_metric_claim
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from scenario_platform.contracts import AnalysisState
from scenario_platform.storage import ContentAddressedStore


def _store(root: Path) -> tuple[EcoProjectStore, object]:
    fixture = build_ridgeway_fixture()
    store = EcoProjectStore(root)
    store.initialize(
        project_id=fixture.bundle.project_id,
        title=fixture.bundle.title,
        mode="GUIDED",
        skeleton=fixture.bundle.model_dump(mode="json"),
    )
    store.store_bundle(fixture.bundle)
    return store, fixture


def _plan(fixture: object, analysis_id: str, *, two_portfolios: bool = False):
    portfolios = [fixture.portfolios[0]]
    if two_portfolios:
        portfolios.append(fixture.portfolios[1])
    scenarios = fixture.bundle.scenarios[:2] if two_portfolios else fixture.bundle.scenarios[:1]
    return ConnectivityRunPlan(
        analysis_id=analysis_id,
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in scenarios],
        portfolios=portfolios,
        baseline_portfolio_id=fixture.baseline_portfolio_id,
        direct_job_count=len(portfolios) * len(scenarios),
        solver_job_cap=len(portfolios) * len(scenarios),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="a" * 64,
    )


def _run(plan: ConnectivityRunPlan, portfolio_id: str, scenario, value: float):
    direct_run_id = f"direct-{portfolio_id}-{scenario.scenario_id}"
    return DirectConnectivityRun(
        direct_run_id=direct_run_id,
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=plan.engine_manifest_sha256,
        model_run_cache_key=hashlib.sha256(direct_run_id.encode()).hexdigest(),
        input_sha256=hashlib.sha256(f"{direct_run_id}:input".encode()).hexdigest(),
        result_sha256=hashlib.sha256(f"{direct_run_id}:result".encode()).hexdigest(),
        matrix_sha256=hashlib.sha256(f"{direct_run_id}:matrix".encode()).hexdigest(),
        pair_metrics=[
            PairResistanceMetric(pair_id=pair_id, effective_resistance=value + index)
            for index, pair_id in enumerate(scenario.included_pair_ids)
        ],
        qa_status=QAStatus.ACCEPTED,
        completed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


def _claim(
    *,
    subject: DirectConnectivityRun,
    baseline: DirectConnectivityRun,
    scenario,
    pair_id: str,
) -> ConnectivityEvidenceClaim:
    subject_metric = next(item for item in subject.pair_metrics if item.pair_id == pair_id)
    baseline_metric = next(item for item in baseline.pair_metrics if item.pair_id == pair_id)
    metric = subject_metric.model_copy(
        update={
            "baseline_effective_resistance": baseline_metric.effective_resistance,
            "baseline_relative_change_percent": (
                baseline_metric.effective_resistance - subject_metric.effective_resistance
            )
            / baseline_metric.effective_resistance
            * 100,
        }
    )
    return build_pair_metric_claim(
        claim_id="claim-baseline-relative",
        run=subject,
        baseline_run=baseline,
        metric=metric,
        rule=ConnectivityDecisionRule(
            rule_id="rule-target",
            title="Target improvement",
            metric=RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
            operator=RuleOperator.AT_LEAST,
            threshold=1,
            scenario_ids=[scenario.scenario_id],
            pair_ids=[pair_id],
            rationale="Test same-scenario evidence binding.",
        ),
        raster_diff_sha256="d" * 64,
        source_asset_ids=["resistance", "focal"],
        assumption_ids=["assumption"],
        authorization_ids=["authorization-resistance", "authorization-focal"],
    )


def test_semantic_publication_is_atomic_versioned_and_idempotent(tmp_path: Path) -> None:
    store, fixture = _store(tmp_path / "project")
    try:
        parent = store.effective_bundle().revision
        updated = fixture.bundle.decision_brief.model_copy(
            update={"decision_question": "Which reviewed concept remains robust?"}
        )
        publication = store.publish_semantic_change(
            [("decision_brief", updated.brief_id, updated.model_dump(mode="json"))],
            created_by="test ecologist",
            reason="Clarify the decision question",
            expected_parent_revision_id=parent.revision_id,
        )
        assert publication["changed"] is True
        assert publication["revision"]["parent_revision_id"] == parent.revision_id
        assert publication["revision"]["model_sha256"] != parent.model_sha256
        assert store.effective_bundle().decision_brief == updated
        assert (
            store.effective_bundle().revision.revision_id == publication["revision"]["revision_id"]
        )
        event = next(
            item
            for item in store.telemetry_rows(event_type="semantic_revision")
            if item["subject_id"] == publication["revision"]["revision_id"]
        )
        assert event["payload"]["semantic_diff"]["sha256"] == publication["semantic_diff"]["sha256"]

        repeated = store.publish_semantic_change(
            [("decision_brief", updated.brief_id, updated.model_dump(mode="json"))],
            created_by="test ecologist",
            reason="Clarify the decision question",
        )
        assert repeated["changed"] is False
        assert repeated["outbox_id"] is None

        with (
            pytest.raises(sqlite3.IntegrityError, match="record history is immutable"),
            store.transaction() as connection,
        ):
            connection.execute("UPDATE eco_record_history SET payload_json='{}'")
    finally:
        store.close()


def test_outbox_expired_lease_reclaims_without_accepting_stale_owner(tmp_path: Path) -> None:
    store, _ = _store(tmp_path / "project")
    try:
        first = store.claim_outbox(limit=1, lease_seconds=30)[0]
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with store.transaction() as connection:
            connection.execute(
                "UPDATE eco_telemetry_outbox SET lease_expires_at=? WHERE outbox_id=?",
                (expired, first["outbox_id"]),
            )
        second = store.claim_outbox(limit=1, lease_seconds=30)[0]
        assert second["outbox_id"] == first["outbox_id"]
        assert second["lease_token"] != first["lease_token"]
        assert second["attempt_count"] == first["attempt_count"] + 1
        with pytest.raises(ValueError, match="active outbox lease"):
            store.mark_outbox_synced(
                first["outbox_id"], {"run_id": "stale"}, lease_token=first["lease_token"]
            )
        store.mark_outbox_synced(
            second["outbox_id"], {"run_id": "current"}, lease_token=second["lease_token"]
        )
    finally:
        store.close()


def test_prepared_and_failed_direct_job_evidence_are_immutable(tmp_path: Path) -> None:
    store, fixture = _store(tmp_path / "project")
    try:
        plan = _plan(fixture, "analysis-terminal")
        store.create_analysis(plan, bundle=fixture.bundle)
        store.transition_analysis(plan.analysis_id, AnalysisState.RUNNING)
        job_id = "direct-terminal"
        store.schedule_direct_jobs(
            plan.analysis_id,
            [
                {
                    "direct_job_id": job_id,
                    "portfolio_id": plan.baseline_portfolio_id,
                    "scenario_id": plan.scenario_ids[0],
                    "phase": "MATRIX",
                    "sequence_number": 0,
                    "emit_current_map": False,
                }
            ],
        )
        assert store.claim_next_direct_job(plan.analysis_id)["direct_job_id"] == job_id
        store.set_direct_job_prepared(job_id, {"prepared": True}, "b" * 64)
        with pytest.raises(ValueError, match="prepared direct-job evidence is immutable"):
            store.set_direct_job_prepared(job_id, {"prepared": False}, "b" * 64)

        cas = ContentAddressedStore(store.root / "blobs")
        payload = b"direct solver failed\n"
        digest = cas.put_bytes(payload)
        artifact = {
            "role": "stderr",
            "sha256": digest,
            "byte_size": len(payload),
            "cas_relative_path": cas.path_for(digest).relative_to(store.root).as_posix(),
            "media_type": "text/plain",
        }
        store.finalize_direct_job(
            job_id,
            "FAILED",
            artifacts=[artifact],
            error={"code": "DIRECT_ENGINE_FAILED"},
        )
        assert store.direct_jobs(plan.analysis_id)[0]["terminal_artifacts"] == [artifact]
        with (
            pytest.raises(sqlite3.IntegrityError, match="terminal-job artifact is immutable"),
            store.transaction() as connection,
        ):
            connection.execute(
                "UPDATE eco_terminal_job_artifacts SET media_type='application/json' "
                "WHERE direct_job_id=?",
                (job_id,),
            )
    finally:
        store.close()


def test_baseline_relative_claim_requires_exact_same_scenario_pair(tmp_path: Path) -> None:
    store, fixture = _store(tmp_path / "project")
    try:
        plan = _plan(fixture, "analysis-claims", two_portfolios=True)
        scenario, other_scenario = fixture.bundle.scenarios[:2]
        subject_id = plan.portfolios[1].portfolio_id
        baseline = _run(plan, plan.baseline_portfolio_id, scenario, 100)
        subject = _run(plan, subject_id, scenario, 90)
        wrong_baseline = _run(plan, plan.baseline_portfolio_id, other_scenario, 110)
        pair_id = scenario.included_pair_ids[0]
        valid = _claim(
            subject=subject,
            baseline=baseline,
            scenario=scenario,
            pair_id=pair_id,
        )
        store._validate_claim_run_references(
            plan=plan,
            runs=[baseline, subject, wrong_baseline],
            claims=[valid],
        )
        subject_only = valid.model_copy(update={"direct_run_ids": [subject.direct_run_id]})
        with pytest.raises(ValueError, match="subject run and the plan baseline run"):
            store._validate_claim_run_references(
                plan=plan,
                runs=[baseline, subject, wrong_baseline],
                claims=[subject_only],
            )
        wrong_scenario = valid.model_copy(
            update={"direct_run_ids": [subject.direct_run_id, wrong_baseline.direct_run_id]}
        )
        with pytest.raises(ValueError, match="subject run and the plan baseline run"):
            store._validate_claim_run_references(
                plan=plan,
                runs=[baseline, subject, wrong_baseline],
                claims=[wrong_scenario],
            )
    finally:
        store.close()


def test_guided_rerun_completion_is_one_transaction_and_preserves_projection(
    tmp_path: Path,
) -> None:
    store, fixture = _store(tmp_path / "project")
    try:
        matrix_plan = _plan(fixture, "guided-matrix")
        map_plan = _plan(fixture, "guided-maps")
        for plan in (matrix_plan, map_plan):
            store.create_analysis(plan, bundle=fixture.bundle)
            store.transition_analysis(plan.analysis_id, AnalysisState.RUNNING)
            store.transition_analysis(plan.analysis_id, AnalysisState.COMPLETED)
        projection = {"verified_fixture_replay": True, "fixture": "Ridgeway Crossing"}
        store.set_guided("guided-session", "RUNNING", projection)
        metadata = {
            "schema_version": "ridgeway-guided-rerun/v1.0",
            "session_id": "guided-session",
            "analysis_id": matrix_plan.analysis_id,
            "map_analysis_id": map_plan.analysis_id,
            "phase": "DIFFERENTIAL",
            "total_jobs": 1_596,
            "matrix_jobs": 1_536,
            "map_jobs": 60,
            "reference": {"manifest_sha256": "c" * 64},
            "differential": None,
            "failure": None,
        }
        store.put_record("guided_rerun", matrix_plan.analysis_id, metadata, allow_update=False)
        report = {"status": "WITHIN_TOLERANCE", "compared_jobs": 1_596}
        cas = ContentAddressedStore(store.root / "blobs")
        artifact_sha256 = cas.put_json(report)
        differential = {**report, "artifact_sha256": artifact_sha256}
        completed = {**metadata, "phase": "COMPLETE", "differential": differential}
        receipt = store.record_guided_rerun_completion(
            session_id="guided-session",
            analysis_id=matrix_plan.analysis_id,
            map_analysis_id=map_plan.analysis_id,
            completed_metadata=completed,
            differential=differential,
        )
        session = store.guided("guided-session")
        assert session["state"] == "COMPLETED"
        assert session["projection"] == projection
        assert store.get_record("guided_differential", matrix_plan.analysis_id) == differential
        event = next(
            item for item in store.telemetry_rows() if item["outbox_id"] == receipt["outbox_id"]
        )
        assert event["payload"]["projection"] == projection
        assert event["payload"]["rerun_completion"]["differential_sha256"] == artifact_sha256
    finally:
        store.close()


def test_executor_liveness_and_trace_lookup_are_read_only_and_exact(tmp_path: Path) -> None:
    store, _ = _store(tmp_path / "project")
    try:
        store.store_assistant_answer(
            "call-1",
            {
                "call_id": "call-1",
                "trace_id": "trace-1",
                "trace_readback_verified": True,
                "text": "Evidence only",
            },
            "trace-1",
        )
        assert store.assistant_answer_by_trace("missing") is None
        assert store.assistant_answer_by_trace("trace-1") == store.assistant_answer("call-1")
        store.store_assistant_answer(
            "call-2",
            {
                "call_id": "call-2",
                "trace_id": "trace-1",
                "trace_readback_verified": True,
                "text": "Duplicate trace",
            },
            "trace-1",
        )
        with pytest.raises(ValueError, match="multiple retained answers"):
            store.assistant_answer_by_trace("trace-1")

        manifest = EngineManifest(
            manifest_id="liveness-test",
            executable_sha256="0" * 64,
            julia_runtime_sha256="1" * 64,
            julia_manifest_sha256="2" * 64,
            native_artifacts_sha256="3" * 64,
            wrapper_sha256="4" * 64,
            parser_sha256="5" * 64,
            metric_definitions_sha256="6" * 64,
            neighbor_rule="EIGHT",
            resistance_average="ARITHMETIC",
            platform="test",
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
        executor = DurableAnalysisExecutor(manifest, lock_root=tmp_path / "analysis-locks")
        assert executor.is_active(project_directory=store.root, analysis_id="absent") is False
    finally:
        store.close()
