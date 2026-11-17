from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecocorridor_core.contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    AssetKind,
    ConnectivityAsset,
    ConnectivityProjectBundle,
    ReferenceComparisonResult,
    ReferenceEvidenceClaimLink,
    ReferenceMetricTolerance,
    ReferencePairComparison,
    ReferenceScenarioBinding,
    ReferenceScenarioComparison,
)
from ecocorridor_core.professional_validation import validate_professional_bundle
from ecocorridor_core.reference_matrix_parser import REFERENCE_MATRIX_PARSER_SHA256
from ecocorridor_core.reference_workflow import build_reference_comparison_plan
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_response_contracts import (
    ReferencePlanData,
    ReferenceResultData,
    ReferenceStatusData,
)
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault
from scenario_platform.contracts import canonical_json

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
ENGINE_SHA = "e" * 64


def _bundle_and_result(
    *,
    passed: bool,
) -> tuple[ConnectivityProjectBundle, ReferenceComparisonResult]:
    fixture = build_ridgeway_fixture()
    source = fixture.bundle.assets[0]
    scenario = fixture.bundle.scenarios[0]
    matrix_id = "reviewed-reference-matrix"
    matrix = ConnectivityAsset.model_validate(
        {
            **source.model_dump(mode="json"),
            "asset_id": matrix_id,
            "kind": AssetKind.REFERENCE_MATRIX,
            "title": "Reviewed synthetic reference matrix",
            "relative_path": "references/reviewed-reference-matrix.txt",
            "sha256": "d" * 64,
            "byte_size": 128,
            "media_type": "text/plain",
            "grid": None,
            "authorization": source.authorization.model_copy(
                update={"asset_id": matrix_id}
            ).model_dump(mode="json"),
        }
    )
    tolerance = ReferenceMetricTolerance(
        absolute_tolerance=0.5,
        relative_tolerance=0.01,
    )
    binding = ReferenceScenarioBinding(
        binding_id="binding-reference-scenario",
        scenario_id=scenario.scenario_id,
        reference_matrix_asset_id=matrix.asset_id,
        tolerance=tolerance,
        reference_basis="Reviewed synthetic direct-run matrix for this exact scenario.",
        reviewed_by="Test ecologist",
        reviewed_at=NOW,
        limitations=["Synthetic reference evidence only."],
    )
    bundle = ConnectivityProjectBundle.model_validate(
        {
            **fixture.bundle.model_dump(mode="json"),
            "assets": [
                *[item.model_dump(mode="json") for item in fixture.bundle.assets],
                matrix.model_dump(mode="json"),
            ],
            "reference_bindings": [binding.model_dump(mode="json")],
        }
    )
    comparisons: list[ReferencePairComparison] = []
    for index, pair_id in enumerate(scenario.included_pair_ids):
        reference = 100.0 + index
        observed = reference if passed or index == 0 else reference + 2.0
        absolute = abs(observed - reference)
        allowed = tolerance.absolute_tolerance + tolerance.relative_tolerance * reference
        comparisons.append(
            ReferencePairComparison(
                pair_id=pair_id,
                reference_effective_resistance=reference,
                observed_effective_resistance=observed,
                tolerance=tolerance,
                absolute_difference=absolute,
                relative_difference=absolute / reference,
                allowed_difference=allowed,
                within_tolerance=absolute <= allowed,
            )
        )
    status = "PASSED" if passed else "FAILED"
    scenario_result = ReferenceScenarioComparison(
        comparison_id="reference-comparison-scenario",
        analysis_id="reference-analysis",
        revision_id=bundle.revision.revision_id,
        model_sha256=bundle.revision.model_sha256,
        binding_id=binding.binding_id,
        scenario_id=scenario.scenario_id,
        direct_run_id="reference-direct-run",
        reference_matrix_asset_id=matrix.asset_id,
        reference_matrix_sha256=matrix.sha256,
        observed_matrix_sha256="c" * 64,
        engine_manifest_sha256=ENGINE_SHA,
        tolerance=tolerance,
        pair_comparisons=comparisons,
        qa_status="ACCEPTED",
        status=status,
        completed_at=NOW,
        limitations=[
            *binding.limitations,
            CURRENT_RASTER_LIMITATION,
            FITNESS_LIMITATION,
        ],
    )
    links = [
        ReferenceEvidenceClaimLink(
            claim_id=f"claim-reference-{item.pair_id}",
            comparison_id=scenario_result.comparison_id,
            scenario_id=scenario.scenario_id,
            pair_id=item.pair_id,
            direct_run_id=scenario_result.direct_run_id,
        )
        for item in comparisons
    ]
    result = ReferenceComparisonResult(
        result_id="reference-result",
        plan_id="reference-plan",
        analysis_id=scenario_result.analysis_id,
        revision_id=bundle.revision.revision_id,
        model_sha256=bundle.revision.model_sha256,
        engine_manifest_sha256=ENGINE_SHA,
        scenario_comparisons=[scenario_result],
        status=status,
        evidence_claim_links=links,
        completed_at=NOW,
        limitations=[CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION],
    )
    return bundle, result


def _reference_codes(
    bundle: ConnectivityProjectBundle,
    *results: ReferenceComparisonResult,
    engine_sha256: str = ENGINE_SHA,
) -> set[str]:
    return {
        item.code
        for item in validate_professional_bundle(
            bundle,
            authoritative_reference_results=results,
            expected_engine_manifest_sha256=engine_sha256,
        )
        if item.code.startswith("REFERENCE_COMPARISON_")
    }


def test_reference_plan_is_baseline_only_and_runs_exactly_bound_scenarios() -> None:
    bundle, _ = _bundle_and_result(passed=True)

    plan = build_reference_comparison_plan(
        bundle=bundle,
        analysis_id="reference-analysis-new",
        engine_manifest_sha256=ENGINE_SHA,
        created_at=NOW,
    )

    assert plan.bound_scenario_ids == [bundle.reference_bindings[0].scenario_id]
    assert plan.run_plan.scenario_ids == plan.bound_scenario_ids
    assert plan.run_plan.direct_job_count == 1
    assert len(plan.run_plan.portfolios) == 1
    assert plan.run_plan.current_maps_for_portfolio_ids == []


def test_validation_requires_authoritative_result_and_rejects_stale_or_wrong_engine() -> None:
    bundle, passed = _bundle_and_result(passed=True)

    assert _reference_codes(bundle) == {"REFERENCE_COMPARISON_REQUIRED"}
    stale = passed.model_copy(update={"revision_id": "stale-revision"})
    assert _reference_codes(bundle, stale) == {"REFERENCE_COMPARISON_REQUIRED"}
    assert _reference_codes(bundle, passed, engine_sha256="f" * 64) == {
        "REFERENCE_COMPARISON_REQUIRED"
    }
    assert _reference_codes(bundle, passed) == {"REFERENCE_COMPARISON_PASSED"}


def test_failed_exact_result_remains_a_blocker_and_never_becomes_passed() -> None:
    bundle, failed = _bundle_and_result(passed=False)

    assert _reference_codes(bundle, failed) == {"REFERENCE_COMPARISON_FAILED"}


def test_conflicting_exact_results_fail_closed_instead_of_selecting_pass() -> None:
    bundle, passed = _bundle_and_result(passed=True)
    _, failed = _bundle_and_result(passed=False)
    failed = failed.model_copy(update={"result_id": "reference-result-failed"})

    assert _reference_codes(bundle, passed, failed) == {"REFERENCE_COMPARISON_CONFLICT"}


def test_same_status_scientific_drift_conflicts_but_identical_repeats_do_not() -> None:
    bundle, passed = _bundle_and_result(passed=True)
    identical_repeat = passed.model_copy(update={"result_id": "reference-result-repeat"})
    assert _reference_codes(bundle, passed, identical_repeat) == {"REFERENCE_COMPARISON_PASSED"}

    original = passed.scenario_comparisons[0]
    drifted_comparison = original.model_copy(update={"observed_matrix_sha256": "a" * 64})
    drifted = passed.model_copy(
        update={
            "result_id": "reference-result-scientific-drift",
            "scenario_comparisons": [drifted_comparison],
        }
    )
    assert _reference_codes(bundle, passed, drifted) == {"REFERENCE_COMPARISON_CONFLICT"}


def test_model_fitness_accepts_only_explicitly_cited_passed_reference_evidence() -> None:
    bundle, passed = _bundle_and_result(passed=True)
    cited = bundle.fitness.model_copy(
        update={
            "dimensions": [
                item.model_copy(update={"evidence_ids": [passed.result_id]})
                for item in bundle.fitness.dimensions
            ]
        }
    )
    cited_bundle = bundle.model_copy(update={"fitness": cited})
    fake_store = SimpleNamespace()

    accepted = RPCDispatcher._model_fitness_evidence_findings(
        fake_store,
        cited_bundle,
        authoritative_reference_results=[(passed, [])],
    )
    missing = RPCDispatcher._model_fitness_evidence_findings(
        SimpleNamespace(get_record=lambda *_args: (_ for _ in ()).throw(KeyError("missing"))),
        cited_bundle,
    )

    assert accepted == []
    assert {item.code for item in missing} == {"MODEL_FITNESS_EVIDENCE_ID_UNRESOLVED"}
    assert cited_bundle.fitness.reviewed_by == bundle.fitness.reviewed_by
    assert cited_bundle.fitness.attestations == bundle.fitness.attestations


def test_reference_rpc_methods_have_strict_runtime_request_contracts(tmp_path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)

    assert {
        "connectivity.reference.plan",
        "connectivity.reference.start",
        "connectivity.reference.status",
        "connectivity.reference.result",
    }.issubset(dispatcher.methods)
    with pytest.raises(RPCFault) as missing_plan:
        dispatcher.dispatch(
            "connectivity.reference.status",
            {
                "project_directory": str(tmp_path / "project"),
                "analysis_id": "reference-analysis",
            },
        )
    with pytest.raises(RPCFault) as extra_field:
        dispatcher.dispatch(
            "connectivity.reference.plan",
            {
                "project_directory": str(tmp_path / "project"),
                "analysis_id": "reference-analysis",
                "renderer_result": "PASSED",
            },
        )

    assert missing_plan.value.error.code == "RPC_CONTRACT_REJECTED"
    assert extra_field.value.error.code == "RPC_CONTRACT_REJECTED"


def test_reference_plan_rpc_replay_projects_authoritative_stored_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _ = _bundle_and_result(passed=True)
    dispatcher = RPCDispatcher(data_root=tmp_path)
    dispatcher.engine_manifest = SimpleNamespace(sha256=ENGINE_SHA)

    class ReplayStore:
        def effective_bundle(self) -> ConnectivityProjectBundle:
            return bundle

        def create_reference_analysis(self, candidate, *, bundle):
            payload = canonical_json(candidate.model_dump(mode="json"))
            return candidate, {
                "plan_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
                "replayed": True,
                "state": "COMPLETED",
            }

        def close(self) -> None:
            return None

    replay_store = ReplayStore()
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: replay_store)
    monkeypatch.setattr(dispatcher, "_authoritative_project_context", lambda _store: None)
    monkeypatch.setattr(dispatcher, "_validation_data", lambda *_args, **_kwargs: {"findings": []})
    monkeypatch.setattr(
        dispatcher,
        "_require_engine",
        lambda: (Path("/unused-test-engine"), ENGINE_SHA, object()),
    )

    response = dispatcher.dispatch(
        "connectivity.reference.plan",
        {
            "project_directory": str(tmp_path / "project"),
            "analysis_id": "reference-analysis-replay",
        },
    )["data"]

    assert response["replayed"] is True
    assert response["state"] == "COMPLETED"


@pytest.mark.parametrize(
    ("method", "analysis_kind", "expected_code"),
    [
        (
            "connectivity.analysis.start",
            "REFERENCE",
            "REFERENCE_ANALYSIS_REQUIRES_REFERENCE_START",
        ),
        (
            "connectivity.reference.start",
            "ALTERNATIVES",
            "REFERENCE_START_REQUIRES_REFERENCE_ANALYSIS",
        ),
    ],
)
def test_analysis_start_endpoints_reject_the_wrong_analysis_kind_before_engine_access(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    analysis_kind: str,
    expected_code: str,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    fake_store = SimpleNamespace(
        analysis=lambda _analysis_id: {"analysis_kind": analysis_kind},
        close=lambda: None,
    )
    monkeypatch.setattr(dispatcher, "_store", lambda _project_directory: fake_store)
    monkeypatch.setattr(dispatcher, "_authoritative_project_context", lambda _store: None)

    params = {
        "project_directory": str(tmp_path / "project"),
        "analysis_id": "reference-analysis",
    }
    if method == "connectivity.reference.start":
        params["plan_id"] = "reference-plan"

    with pytest.raises(RPCFault) as rejected:
        dispatcher.dispatch(method, params)

    assert rejected.value.error.code == expected_code


def test_reference_rpc_response_contracts_reject_forged_identity_hash_and_disposition() -> None:
    bundle, source_result = _bundle_and_result(passed=True)
    plan = build_reference_comparison_plan(
        bundle=bundle,
        analysis_id=source_result.analysis_id,
        engine_manifest_sha256=ENGINE_SHA,
        created_at=NOW,
    )
    result = source_result.model_copy(update={"plan_id": plan.plan_id})
    plan_sha256 = hashlib.sha256(
        canonical_json(plan.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_json(result.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    arithmetic = "1 No Action baseline × 1 bound scenarios = 1 direct Circuitscape matrix jobs"
    plan_payload = {
        "analysis_id": plan.analysis_id,
        "plan_id": plan.plan_id,
        "state": "READY",
        "plan": plan,
        "plan_sha256": plan_sha256,
        "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
        "replayed": False,
        "arithmetic": arithmetic,
    }
    ReferencePlanData.model_validate(plan_payload)
    ReferencePlanData.model_validate({**plan_payload, "state": "COMPLETED", "replayed": True})
    with pytest.raises(ValueError, match="newly created"):
        ReferencePlanData.model_validate({**plan_payload, "state": "COMPLETED"})
    with pytest.raises(ValueError, match="hash differs"):
        ReferencePlanData.model_validate({**plan_payload, "plan_sha256": "0" * 64})

    status_payload = {
        "analysis_id": plan.analysis_id,
        "plan_id": plan.plan_id,
        "state": "COMPLETED",
        "plan": plan,
        "completed_run_count": 1,
        "direct_job_counts": {"SUCCEEDED": 1},
        "result_id": result.result_id,
        "result_status": result.status,
        "active_for_current_model": True,
        "gates_model_fitness": True,
        "evidence_disposition": "ACTIVE_PASSED",
        "stale_reasons": [],
        "error": None,
    }
    ReferenceStatusData.model_validate(status_payload)
    with pytest.raises(ValueError, match="disposition is inconsistent"):
        ReferenceStatusData.model_validate(
            {
                **status_payload,
                "active_for_current_model": False,
                "gates_model_fitness": False,
                "evidence_disposition": "HISTORICAL_STALE",
                "stale_reasons": [],
            }
        )

    result_payload = {
        "analysis_id": result.analysis_id,
        "plan_id": result.plan_id,
        "plan": plan,
        "result": result,
        "claim_ids": sorted(item.claim_id for item in result.evidence_claim_links),
        "plan_sha256": plan_sha256,
        "result_sha256": result_sha256,
        "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
        "outbox_id": "outbox-reference-result",
        "replayed": False,
        "active_for_current_model": True,
        "gates_model_fitness": True,
        "evidence_disposition": "ACTIVE_PASSED",
        "stale_reasons": [],
    }
    ReferenceResultData.model_validate(result_payload)
    for forged in (
        {"analysis_id": "other-analysis"},
        {"result_sha256": "0" * 64},
        {"reference_matrix_parser_sha256": "0" * 64},
        {"claim_ids": ["forged-claim"]},
    ):
        with pytest.raises(ValueError):
            ReferenceResultData.model_validate({**result_payload, **forged})


def test_reference_rpc_marks_stale_and_scientifically_conflicting_results_non_gating(
    tmp_path,
) -> None:
    bundle, passed = _bundle_and_result(passed=True)
    plan = build_reference_comparison_plan(
        bundle=bundle,
        analysis_id=passed.analysis_id,
        engine_manifest_sha256=ENGINE_SHA,
        created_at=NOW,
    )
    passed = passed.model_copy(update={"plan_id": plan.plan_id})
    dispatcher = RPCDispatcher(data_root=tmp_path)
    dispatcher.engine_manifest = SimpleNamespace(sha256=ENGINE_SHA)

    stale = passed.model_copy(update={"revision_id": "historical-revision"})
    stale_store = SimpleNamespace(
        effective_bundle=lambda: bundle,
        reference_comparison_results=lambda **_kwargs: [(passed, [])],
    )
    stale_disposition = dispatcher._reference_evidence_disposition(
        store=stale_store,
        plan=plan,
        result=stale,
    )
    assert stale_disposition["evidence_disposition"] == "HISTORICAL_STALE"
    assert stale_disposition["active_for_current_model"] is False
    assert stale_disposition["gates_model_fitness"] is False

    comparison = passed.scenario_comparisons[0]
    scientific_drift = passed.model_copy(
        update={
            "result_id": "reference-result-drift",
            "scenario_comparisons": [
                comparison.model_copy(update={"observed_matrix_sha256": "a" * 64})
            ],
        }
    )
    conflict_store = SimpleNamespace(
        effective_bundle=lambda: bundle,
        reference_comparison_results=lambda **_kwargs: [
            (passed, []),
            (scientific_drift, []),
        ],
    )
    conflict_disposition = dispatcher._reference_evidence_disposition(
        store=conflict_store,
        plan=plan,
        result=passed,
    )
    assert conflict_disposition == {
        "active_for_current_model": False,
        "gates_model_fitness": False,
        "evidence_disposition": "CONFLICT_NON_GATING",
        "stale_reasons": ["CONFLICTING_CURRENT_RESULTS"],
    }
