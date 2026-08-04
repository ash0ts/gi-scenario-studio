from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import (
    ConnectivityDecisionRule,
    ConnectivityRunPlan,
    DiagnosticMapSummary,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    RuleDisposition,
    RuleMetric,
    RuleOperator,
)
from ecocorridor_core.cost_basis import with_reviewer_confirmation
from ecocorridor_core.export import (
    EvidenceArtifactSource,
    EvidenceProjectAssetSource,
    assert_safe_export_narratives,
    compute_export_eligibility,
    create_evidence_export,
)
from ecocorridor_core.interventions import canonicalize_portfolio, portfolio_cost
from ecocorridor_core.metrics import (
    assert_safe_ecological_claim,
    attach_baseline_metrics,
    build_pair_metric_claim,
    evaluate_portfolio,
    validate_complete_ensemble,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.search import (
    BenchmarkGateResult,
    ObservedPortfolio,
    make_search_plan,
    propose_extratrees_batch,
)


def _artifact_payload(direct_run_id: str, role: str) -> bytes:
    return f"{direct_run_id}:{role}\n".encode()


def _matrix_payload(metrics: list[PairResistanceMetric]) -> bytes:
    rows: list[str] = []
    for metric in metrics:
        match = re.search(r"_pair_(\d+)_(\d+)$", metric.pair_id)
        assert match is not None
        rows.append(f"{match.group(1)}\t{match.group(2)}\t{metric.effective_resistance:.17g}\n")
    return "".join(rows).encode()


def _engine_manifest() -> EngineManifest:
    return EngineManifest(
        manifest_id="test-export-engine",
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


def _run_for_scenario(
    portfolio_id: str,
    scenario,
    *,
    resistance: float,
    baseline: float | None = None,
    map_change: float | None = None,
) -> DirectConnectivityRun:
    direct_run_id = f"run_{portfolio_id}_{scenario.scenario_id}"
    metrics = [
        PairResistanceMetric(
            pair_id=pair_id,
            effective_resistance=resistance + index,
            baseline_effective_resistance=None if baseline is None else baseline + index,
            baseline_relative_change_percent=(
                None
                if baseline is None
                else (baseline + index - resistance - index) / (baseline + index) * 100
            ),
        )
        for index, pair_id in enumerate(scenario.included_pair_ids)
    ]
    maps = []
    if map_change is not None:
        maps = [
            DiagnosticMapSummary(
                mask_id="road_review_mask",
                baseline_total_current=10,
                portfolio_total_current=10 * (1 + map_change / 100),
                relative_change_percent=map_change,
                review_triggered=map_change > 15,
            )
        ]
    return DirectConnectivityRun(
        direct_run_id=direct_run_id,
        analysis_id="analysis_test",
        revision_id="revision_ridgeway_v1",
        portfolio_id=portfolio_id,
        scenario_id=scenario.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=_engine_manifest().sha256,
        model_run_cache_key=("b" * 60 + f"{len(portfolio_id):04x}")[-64:],
        input_sha256=hashlib.sha256(_artifact_payload(direct_run_id, "request")).hexdigest(),
        result_sha256=hashlib.sha256(
            _artifact_payload(direct_run_id, "result_protocol")
        ).hexdigest(),
        matrix_sha256=hashlib.sha256(_matrix_payload(metrics)).hexdigest(),
        pair_metrics=metrics,
        diagnostic_maps=maps,
        qa_status=QAStatus.ACCEPTED,
        completed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


def _retained_artifacts(
    root: Path, runs: list[DirectConnectivityRun]
) -> list[EvidenceArtifactSource]:
    filenames = {
        "request": "request.json",
        "result_protocol": "result.json",
        "matrix": "matrix.tsv",
        "circuitscape_configuration": "circuitscape.ini",
        "action_raster_diff": "action-raster-diff.json",
        "footprint_proof_document": "footprint-proofs.json",
        "input_resistance": "resistance.asc",
        "input_focal": "focal.asc",
        "input_included_pairs": "pairs.tsv",
    }
    output: list[EvidenceArtifactSource] = []
    for run in runs:
        directory = root / run.direct_run_id
        directory.mkdir(parents=True)
        for role, filename in filenames.items():
            path = directory / filename
            payload = (
                _matrix_payload(run.pair_metrics)
                if role == "matrix"
                else _artifact_payload(run.direct_run_id, role)
            )
            path.write_bytes(payload)
            output.append(
                EvidenceArtifactSource(
                    direct_run_id=run.direct_run_id,
                    role=role,
                    source_path=path,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    byte_size=len(payload),
                    media_type="application/octet-stream",
                )
            )
    return output


def _project_asset_sources(root: Path, fixture) -> list[EvidenceProjectAssetSource]:
    payloads = {
        asset_id: json.dumps(raster.values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for asset_id, raster in fixture.rasters.items()
    }
    output: list[EvidenceProjectAssetSource] = []
    for asset in fixture.bundle.assets:
        payload = payloads[asset.asset_id]
        assert hashlib.sha256(payload).hexdigest() == asset.sha256
        path = root / asset.asset_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        output.append(
            EvidenceProjectAssetSource(
                asset_id=asset.asset_id,
                source_path=path,
                sha256=asset.sha256,
                byte_size=asset.byte_size,
                media_type=asset.media_type,
            )
        )
    return output


def test_baseline_relative_metrics_never_cross_scenarios() -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    baseline = _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100)
    portfolio = _run_for_scenario(fixture.portfolios[1].portfolio_id, scenario, resistance=90)
    attached = attach_baseline_metrics(portfolio, baseline)
    assert attached.pair_metrics[0].baseline_relative_change_percent == pytest.approx(10)
    another = fixture.bundle.scenarios[1]
    with pytest.raises(ValueError, match="within one scenario"):
        attach_baseline_metrics(
            portfolio,
            _run_for_scenario(fixture.baseline_portfolio_id, another, resistance=100),
        )


@pytest.mark.parametrize(
    ("baseline_update", "message"),
    [
        ({"state": DirectRunState.FAILED}, "baseline direct run did not succeed"),
        ({"analysis_id": "analysis_other"}, "within one analysis"),
        ({"revision_id": "revision_other"}, "one model revision"),
        ({"engine_manifest_sha256": "f" * 64}, "one engine manifest"),
    ],
)
def test_baseline_relative_metrics_require_comparable_direct_evidence(
    baseline_update: dict[str, object], message: str
) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    baseline = _run_for_scenario(
        fixture.baseline_portfolio_id, scenario, resistance=100
    ).model_copy(update=baseline_update)
    portfolio = _run_for_scenario(fixture.portfolios[1].portfolio_id, scenario, resistance=90)
    with pytest.raises(ValueError, match=message):
        attach_baseline_metrics(portfolio, baseline)


def test_decision_summary_rejects_a_failed_baseline_direct_run() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[1]
    baselines = [
        _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100)
        for scenario in fixture.bundle.scenarios
    ]
    baselines[0] = baselines[0].model_copy(update={"state": DirectRunState.FAILED})
    runs = [
        _run_for_scenario(portfolio.portfolio_id, scenario, resistance=94)
        for scenario in fixture.bundle.scenarios
    ]
    summary = evaluate_portfolio(
        portfolio=portfolio,
        runs=runs,
        baseline_runs=baselines,
        scenarios=fixture.bundle.scenarios,
        rules=fixture.bundle.decision_rules,
        cost=200_000,
        currency="USD",
        unresolved_feasibility=[],
    )
    assert summary.disposition == PortfolioDisposition.INCOMPLETE
    assert any(
        "Baseline: Direct job did not succeed" in item for item in summary.unresolved_feasibility
    )


@pytest.mark.parametrize(
    "claim",
    [
        "This crossing reduces wildlife-vehicle collisions.",
        "This result predicts wildlife movement through the crossing.",
        "This fence improves crossing use.",
        "This portfolio delivers regulatory benefit.",
        "This is the approved wildlife crossing.",
        "This is a design-ready crossing.",
        "This portfolio improves biodiversity.",
        "This analysis demonstrates wildlife movement.",
        "This package supports crossing use.",
        "This project delivers collision reduction.",
        "This is a regulator-approved portfolio.",
    ],
)
def test_explicit_forbidden_ecological_claims_fail_closed(claim: str) -> None:
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_safe_ecological_claim(claim)


@pytest.mark.parametrize(
    "limitation",
    [
        "This result does not predict wildlife movement.",
        "Current-density maps are diagnostic and do not predict animal use.",
        "This is not an approved wildlife crossing.",
        "The model cannot guarantee collision reduction or population benefit.",
    ],
)
def test_explicit_ecological_limitations_are_not_false_positives(limitation: str) -> None:
    assert_safe_ecological_claim(limitation)


@pytest.mark.parametrize(
    "claim",
    [
        "This is not observed animal movement, but this crossing reduces collisions.",
        "This model is not calibrated and guarantees collision reduction.",
        "This model is not calibrated and is the optimal corridor.",
    ],
)
def test_unrelated_negation_cannot_hide_a_prohibited_claim(claim: str) -> None:
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_safe_ecological_claim(claim)


def test_complete_ensemble_requires_targets_and_safeguards() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[1]
    runs = [
        _run_for_scenario(portfolio.portfolio_id, scenario, resistance=90)
        for scenario in fixture.bundle.scenarios[:-1]
    ]
    complete, findings = validate_complete_ensemble(
        runs, fixture.bundle.scenarios, portfolio_id=portfolio.portfolio_id
    )
    assert complete is False
    assert "Missing required scenarios" in findings[0]


def test_decision_summary_leads_with_worst_hypothesis_and_safeguard() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[1]
    baselines = [
        _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100)
        for scenario in fixture.bundle.scenarios
    ]
    runs = [
        _run_for_scenario(
            portfolio.portfolio_id,
            scenario,
            resistance=94 if not scenario.safeguard else 99,
            map_change=5,
        )
        for scenario in fixture.bundle.scenarios
    ]
    summary = evaluate_portfolio(
        portfolio=portfolio,
        runs=runs,
        baseline_runs=baselines,
        scenarios=fixture.bundle.scenarios,
        rules=fixture.bundle.decision_rules,
        cost=200_000,
        currency="USD",
        unresolved_feasibility=[],
    )
    assert summary.disposition == PortfolioDisposition.PASSES
    assert summary.controlling_scenario_id is not None
    assert summary.worst_resistance_reduction_percent == pytest.approx(100 / 101)
    assert summary.safeguard_passed is True
    assert "feasibility review" in summary.next_action


def test_required_current_rule_fails_when_direct_maps_are_missing() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[1]
    baselines = [
        _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100)
        for scenario in fixture.bundle.scenarios
    ]
    runs = [
        _run_for_scenario(portfolio.portfolio_id, scenario, resistance=95)
        for scenario in fixture.bundle.scenarios
    ]
    current_rule = next(
        item
        for item in fixture.bundle.decision_rules
        if item.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
    ).model_copy(update={"required": True})

    summary = evaluate_portfolio(
        portfolio=portfolio,
        runs=runs,
        baseline_runs=baselines,
        scenarios=fixture.bundle.scenarios,
        rules=[current_rule],
        cost=0,
        currency="USD",
        unresolved_feasibility=[],
    )

    assert summary.disposition == PortfolioDisposition.FAILS
    assert len(summary.rule_dispositions) == len(fixture.bundle.scenarios)
    assert all(not item.passed for item in summary.rule_dispositions)
    assert all("evidence is missing" in item.explanation for item in summary.rule_dispositions)


def test_required_safeguard_rule_cannot_pass_without_a_safeguard_scenario() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[1]
    scenario = next(item for item in fixture.bundle.scenarios if not item.safeguard)
    baseline = _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100)
    run = _run_for_scenario(portfolio.portfolio_id, scenario, resistance=95)
    safeguard_rule = next(
        item
        for item in fixture.bundle.decision_rules
        if item.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
    ).model_copy(update={"scenario_ids": [], "pair_ids": []})

    summary = evaluate_portfolio(
        portfolio=portfolio,
        runs=[run],
        baseline_runs=[baseline],
        scenarios=[scenario],
        rules=[safeguard_rule],
        cost=0,
        currency="USD",
        unresolved_feasibility=[],
    )

    assert summary.disposition == PortfolioDisposition.FAILS
    assert len(summary.rule_dispositions) == 1
    assert summary.rule_dispositions[0].passed is False
    assert "no applicable target or safeguard" in summary.rule_dispositions[0].explanation


def test_evidence_claim_resolves_full_chain_and_forbids_overclaiming() -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    run = _run_for_scenario(
        fixture.portfolios[1].portfolio_id, scenario, resistance=90, baseline=100
    )
    baseline_run = _run_for_scenario(
        fixture.baseline_portfolio_id, scenario, resistance=100, baseline=100
    )
    rule = fixture.bundle.decision_rules[0]
    claim = build_pair_metric_claim(
        claim_id="claim_1",
        run=run,
        baseline_run=baseline_run,
        metric=run.pair_metrics[0],
        rule=rule,
        raster_diff_sha256="d" * 64,
        source_asset_ids=["resistance", "focal"],
        assumption_ids=["resistance_basis"],
        authorization_ids=["auth_resistance", "auth_focal"],
    )
    assert claim.text.startswith("Direct Circuitscape result under documented assumptions.")
    assert {node.node_type for node in claim.nodes} >= {
        "DIRECT_RUN",
        "RASTER_DIFF",
        "ENGINE_MANIFEST",
        "AUTHORIZATION",
    }
    assert claim.direct_run_ids == [run.direct_run_id, baseline_run.direct_run_id]
    diff = next(node for node in claim.nodes if node.node_type == "RASTER_DIFF")
    assert diff.node_id == f"diff:{'d' * 64}"
    assert diff.sha256 == "d" * 64
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_safe_ecological_claim("This is the optimal corridor with collision reduction.")


@pytest.mark.parametrize(
    ("field", "unsafe_text", "expected_path"),
    [
        (
            "rule_explanation",
            "This is a robust candidate under the reviewed rule.",
            r"summaries\[0\]\.rule_dispositions\[0\]\.explanation",
        ),
        (
            "unresolved_feasibility",
            "The recommended corridor still needs a site visit.",
            r"summaries\[0\]\.unresolved_feasibility\[0\]",
        ),
        (
            "claim_text",
            "This portfolio is ecologically robust.",
            r"evidence_claims\[0\]\.text",
        ),
    ],
)
def test_export_strictly_validates_every_user_visible_decision_narrative(
    field: str,
    unsafe_text: str,
    expected_path: str,
) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    run = _run_for_scenario(
        fixture.portfolios[1].portfolio_id, scenario, resistance=90, baseline=100
    )
    baseline_run = _run_for_scenario(
        fixture.baseline_portfolio_id, scenario, resistance=100, baseline=100
    )
    claim = build_pair_metric_claim(
        claim_id="claim_export_copy_policy",
        run=run,
        baseline_run=baseline_run,
        metric=run.pair_metrics[0],
        rule=fixture.bundle.decision_rules[0],
        raster_diff_sha256="d" * 64,
        source_asset_ids=["resistance", "focal"],
        assumption_ids=["resistance_basis"],
        authorization_ids=["auth_resistance", "auth_focal"],
    )
    summary = PortfolioDecisionSummary(
        portfolio_id=run.portfolio_id,
        disposition=PortfolioDisposition.PASSES,
        cost=0,
        currency="USD",
        rule_dispositions=[
            RuleDisposition(
                rule_id="rule_target_improvement",
                scenario_id=scenario.scenario_id,
                pair_id=scenario.included_pair_ids[0],
                observed_value=10.0,
                passed=True,
                explanation="Within-scenario direct modeled result.",
            )
        ],
        unresolved_feasibility=["Field verification remains unresolved."],
        next_action=(
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        ),
    )
    if field == "rule_explanation":
        summary = summary.model_copy(
            update={
                "rule_dispositions": [
                    summary.rule_dispositions[0].model_copy(update={"explanation": unsafe_text})
                ]
            }
        )
    elif field == "unresolved_feasibility":
        summary = summary.model_copy(update={"unresolved_feasibility": [unsafe_text]})
    else:
        claim = claim.model_copy(update={"text": unsafe_text})

    with pytest.raises(ValueError, match=expected_path):
        assert_safe_export_narratives(summaries=[summary], claims=[claim])


def test_search_enumerates_small_space_and_sobol_samples_large_space() -> None:
    fixture = build_ridgeway_fixture()
    first_site = fixture.bundle.intervention_sites[0]
    first_options = [
        item for item in fixture.bundle.intervention_options if item.site_id == first_site.site_id
    ]
    baseline_one = canonicalize_portfolio(
        [first_site], first_options, {first_site.site_id: f"{first_site.site_id}_no_action"}
    )
    enumeration = make_search_plan(
        sites=[first_site],
        options=first_options,
        baseline=baseline_one,
        scenario_count=12,
        max_direct_jobs=36,
        seed=17,
    )
    assert enumeration.method == "ENUMERATION"
    assert len(enumeration.candidates) == 3
    assert enumeration.planned_direct_jobs == 36

    large = make_search_plan(
        sites=fixture.bundle.intervention_sites,
        options=fixture.bundle.intervention_options,
        baseline=fixture.portfolios[0],
        scenario_count=12,
        max_direct_jobs=128 * 12,
        seed=17,
    )
    assert large.method == "SOBOL"
    assert large.qmc_engine == "scipy_sobol_scrambled"
    assert len({item.portfolio.portfolio_id for item in large.candidates}) == 128


def test_adaptive_ml_is_gated_and_never_returns_predictions() -> None:
    fixture = build_ridgeway_fixture()
    observed = [
        ObservedPortfolio(
            portfolio=item,
            objective_values=[float(index), float(128 - index)],
            feasible=index % 3 != 0,
            adverse_found=index % 7 == 0,
            complete_scenario_ensemble=True,
        )
        for index, item in enumerate(fixture.portfolios[:63])
    ]
    with pytest.raises(ValueError, match="at least 64"):
        propose_extratrees_batch(
            sites=fixture.bundle.intervention_sites,
            options=fixture.bundle.intervention_options,
            observed=observed,
            candidates=fixture.portfolios,
            batch_size=8,
            seed=4,
            batch=1,
        )
    failed_gate = BenchmarkGateResult(
        landscape_count=3,
        seeds_per_landscape=10,
        median_hypervolume_improvement_percent_by_budget={"64": 6, "128": 4},
        tenth_percentile_regression_percent=1,
        feasible_recall_delta_percent=0,
        safeguard_adverse_recall_percent=95,
    )
    assert failed_gate.production_enabled is False


def test_export_eligibility_is_derived_from_complete_exact_evidence(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    fixture_basis = fixture.bundle.cost_basis
    assert fixture_basis is not None
    professional_basis = with_reviewer_confirmation(
        fixture_basis.model_copy(
            update={
                "assumptions": [
                    "Analysis horizon years: 30",
                    "Real discount rate percent: 3.0",
                    "CAPEX basis: Reviewer-entered conceptual capital screening values.",
                    "O&M assumptions: Annual inspection and maintenance are included in each reviewed option value.",
                    "Replacement assumptions: Replacement timing and residual value are included in each reviewed option value.",
                ]
            }
        ),
        "I reviewed the complete lifecycle screening cost basis for this export test.",
    )
    professional_bundle = fixture.bundle.model_copy(update={"cost_basis": professional_basis})
    portfolio = fixture.portfolios[1]
    runs = [
        _run_for_scenario(portfolio.portfolio_id, scenario, resistance=90, baseline=100)
        for scenario in fixture.bundle.scenarios
    ]
    baseline_runs = [
        _run_for_scenario(fixture.baseline_portfolio_id, scenario, resistance=100, baseline=100)
        for scenario in fixture.bundle.scenarios
    ]
    evidence_runs = runs + baseline_runs
    cost, currency, _ = portfolio_cost(portfolio, fixture.bundle.intervention_options)
    summary = PortfolioDecisionSummary(
        portfolio_id=portfolio.portfolio_id,
        disposition=PortfolioDisposition.PASSES,
        cost=cost,
        currency=currency,
        rule_dispositions=[
            RuleDisposition(
                rule_id="rule_target_improvement",
                scenario_id=fixture.bundle.scenarios[0].scenario_id,
                pair_id=fixture.bundle.scenarios[0].included_pair_ids[0],
                observed_value=10.0,
                passed=True,
                explanation="Within-scenario result.",
                evidence_claim_id="claim_export",
            )
        ],
        next_action="Candidate to carry forward for ecological, field, and engineering feasibility review.",
        direct_run_ids=[item.direct_run_id for item in runs],
        disposition_evidence_claim_id="claim_disposition",
    )
    eligibility = compute_export_eligibility(
        bundle=professional_bundle,
        current_authorization_bundle=professional_bundle,
        summaries=[summary],
        runs=evidence_runs,
        deployment="WANDB_CLOUD",
        sync_state="SYNC_QUEUED_OFFLINE",
        professional=True,
    )
    assert eligibility.eligible is True
    mismatched_engine = compute_export_eligibility(
        bundle=professional_bundle,
        current_authorization_bundle=professional_bundle,
        summaries=[summary],
        runs=evidence_runs,
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
        expected_engine_manifest_sha256="f" * 64,
    )
    assert mismatched_engine.eligible is False
    assert any(
        "differs from the pinned EngineManifest" in item for item in mismatched_engine.qa_blockers
    )
    first_asset = fixture.bundle.assets[0]
    revoked_asset = first_asset.model_copy(
        update={
            "authorization": first_asset.authorization.model_copy(update={"export_allowed": False})
        }
    )
    revoked_current_bundle = professional_bundle.model_copy(
        update={
            "assets": [
                revoked_asset if item.asset_id == first_asset.asset_id else item
                for item in fixture.bundle.assets
            ]
        }
    )
    revoked = compute_export_eligibility(
        bundle=professional_bundle,
        current_authorization_bundle=revoked_current_bundle,
        summaries=[summary],
        runs=evidence_runs,
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
    )
    assert revoked.eligible is False
    assert any("export is not authorized" in item for item in revoked.authorization_blockers)

    replacement_asset = first_asset.model_copy(update={"sha256": "f" * 64})
    replaced_current_bundle = professional_bundle.model_copy(
        update={
            "assets": [
                replacement_asset if item.asset_id == first_asset.asset_id else item
                for item in fixture.bundle.assets
            ]
        }
    )
    replaced = compute_export_eligibility(
        bundle=professional_bundle,
        current_authorization_bundle=replaced_current_bundle,
        summaries=[summary],
        runs=evidence_runs,
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
    )
    assert replaced.eligible is False
    assert any(
        "ECO_ANALYSIS_ASSET_IDENTITY_STALE" in item for item in replaced.authorization_blockers
    )
    scenario = fixture.bundle.scenarios[0]
    claim = build_pair_metric_claim(
        claim_id="claim_export",
        run=runs[0],
        baseline_run=baseline_runs[0],
        metric=runs[0].pair_metrics[0],
        rule=ConnectivityDecisionRule(
            rule_id="rule_target_improvement",
            title="Target improvement",
            metric=RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
            operator=RuleOperator.AT_LEAST,
            threshold=3,
            scenario_ids=[scenario.scenario_id],
            pair_ids=[scenario.included_pair_ids[0]],
            rationale="Test rule",
        ),
        raster_diff_sha256="d" * 64,
        source_asset_ids=["resistance", "focal"],
        assumption_ids=["assumption"],
        authorization_ids=["authorization_resistance", "authorization_focal"],
    )
    engine_manifest = _engine_manifest()
    run_plan = ConnectivityRunPlan(
        analysis_id=runs[0].analysis_id,
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[
            next(
                item
                for item in fixture.portfolios
                if item.portfolio_id == fixture.baseline_portfolio_id
            ),
            portfolio,
        ],
        baseline_portfolio_id=fixture.baseline_portfolio_id,
        direct_job_count=len(evidence_runs),
        solver_job_cap=len(evidence_runs),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=engine_manifest.sha256,
    )
    cost_rule = next(
        item for item in fixture.bundle.decision_rules if item.metric == RuleMetric.MAX_COST
    )
    cost_claim = DurableAnalysisExecutor._cost_claim(
        claim_id="claim_cost",
        bundle=professional_bundle,
        portfolio=portfolio,
        rule_id=cost_rule.rule_id,
        cost=cost,
        currency=currency,
    )
    qa_claims = [
        DurableAnalysisExecutor._qa_claim(
            claim_id=f"claim_qa_{index}", run=run, rule_id="rule_exact_qa"
        )
        for index, run in enumerate(evidence_runs)
    ]
    disposition_claim = DurableAnalysisExecutor._portfolio_disposition_claim(
        claim_id="claim_disposition",
        summary=summary,
        runs=runs,
        supporting_claim_ids=[claim.claim_id, cost_claim.claim_id]
        + [item.claim_id for item in qa_claims if item.portfolio_id == portfolio.portfolio_id],
    )
    export_claims = [claim, cost_claim, *qa_claims, disposition_claim]
    exported = create_evidence_export(
        output_root=tmp_path,
        export_id="export_1",
        baseline_portfolio_id=fixture.baseline_portfolio_id,
        bundle=professional_bundle,
        current_authorization_bundle=professional_bundle,
        deployment="WANDB_CLOUD",
        engine_manifest=engine_manifest,
        run_plan=run_plan,
        semantic_diff=None,
        eligibility=eligibility,
        summaries=[summary],
        runs=evidence_runs,
        claims=export_claims,
        artifact_sources=_retained_artifacts(tmp_path / "retained", evidence_runs),
        project_asset_sources=_project_asset_sources(tmp_path / "project-assets", fixture),
    )
    with pytest.raises(PermissionError, match="authorization changed"):
        create_evidence_export(
            output_root=tmp_path,
            export_id="revoked_after_eligibility",
            baseline_portfolio_id=fixture.baseline_portfolio_id,
            bundle=professional_bundle,
            current_authorization_bundle=revoked_current_bundle,
            deployment="WANDB_CLOUD",
            engine_manifest=engine_manifest,
            run_plan=run_plan,
            semantic_diff=None,
            eligibility=eligibility,
            summaries=[summary],
            runs=evidence_runs,
            claims=export_claims,
            artifact_sources=_retained_artifacts(
                tmp_path / "revoked-after-eligibility-retained", evidence_runs
            ),
            project_asset_sources=_project_asset_sources(
                tmp_path / "revoked-after-eligibility-project-assets", fixture
            ),
        )
    prohibited_rule = fixture.bundle.decision_rules[0].model_copy(
        update={"rationale": "This guarantees collision reduction and is the optimal corridor."}
    )
    prohibited_bundle = professional_bundle.model_copy(
        update={
            "decision_rules": [
                prohibited_rule if item.rule_id == prohibited_rule.rule_id else item
                for item in fixture.bundle.decision_rules
            ]
        }
    )
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        create_evidence_export(
            output_root=tmp_path,
            export_id="prohibited_purpose",
            baseline_portfolio_id=fixture.baseline_portfolio_id,
            bundle=prohibited_bundle,
            current_authorization_bundle=professional_bundle,
            deployment="WANDB_CLOUD",
            engine_manifest=engine_manifest,
            run_plan=run_plan,
            semantic_diff=None,
            eligibility=eligibility,
            summaries=[summary],
            runs=evidence_runs,
            claims=export_claims,
            artifact_sources=_retained_artifacts(
                tmp_path / "prohibited-purpose-retained", evidence_runs
            ),
            project_asset_sources=_project_asset_sources(
                tmp_path / "prohibited-purpose-assets", fixture
            ),
        )
    assert (exported / "manifest.json").is_file()
    assert "SYNC_QUEUED_OFFLINE" in (exported / "manifest.json").read_text(encoding="utf-8")
    story = (exported / "decision-story.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in story
    assert "Direct Circuitscape result under documented assumptions." in story
    assert fixture.bundle.project_id not in story
    assert portfolio.portfolio_id not in story
    assert fixture.bundle.scenarios[0].scenario_id not in story
    assert fixture.bundle.assets[0].asset_id not in story
    manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["redacted_static_story"] == "decision-story.html"
    assert manifest["sharing_review_required"] is True
    assert set(manifest["artifact_files"]) == {item.direct_run_id for item in evidence_runs}
    first_run = runs[0].direct_run_id
    matrix_relative = manifest["artifact_files"][first_run]["matrix"]
    matrix_path = exported / matrix_relative
    assert matrix_path.is_file()
    assert (
        manifest["files"][matrix_relative] == hashlib.sha256(matrix_path.read_bytes()).hexdigest()
    )

    tampered_claim = claim.model_copy(
        update={
            "value": 11.0,
            "nodes": [
                node.model_copy(update={"attributes": {**node.attributes, "value": 11.0}})
                if node.node_type == "METRIC"
                else node
                for node in claim.nodes
            ],
        }
    )
    tampered_summary = summary.model_copy(
        update={
            "rule_dispositions": [
                item.model_copy(update={"observed_value": 11.0})
                if item.evidence_claim_id == claim.claim_id
                else item
                for item in summary.rule_dispositions
            ]
        }
    )
    with pytest.raises(PermissionError, match="retained raw matrices"):
        create_evidence_export(
            output_root=tmp_path,
            export_id="tampered_mutually_consistent_value",
            baseline_portfolio_id=fixture.baseline_portfolio_id,
            bundle=professional_bundle,
            current_authorization_bundle=professional_bundle,
            deployment="WANDB_CLOUD",
            engine_manifest=engine_manifest,
            run_plan=run_plan,
            semantic_diff=None,
            eligibility=eligibility,
            summaries=[tampered_summary],
            runs=evidence_runs,
            claims=[
                tampered_claim if item.claim_id == claim.claim_id else item
                for item in export_claims
            ],
            artifact_sources=_retained_artifacts(
                tmp_path / "tampered-value-retained", evidence_runs
            ),
            project_asset_sources=_project_asset_sources(
                tmp_path / "tampered-value-project-assets", fixture
            ),
        )

    unsafe_runs = list(evidence_runs)
    unsafe_runs[0] = unsafe_runs[0].model_copy(update={"direct_run_id": "../../escape"})
    unsafe_claims = [
        item.model_copy(
            update={
                "direct_run_ids": [
                    "../../escape" if run_id == evidence_runs[0].direct_run_id else run_id
                    for run_id in item.direct_run_ids
                ]
            }
        )
        for item in export_claims
    ]
    unsafe_sources = _retained_artifacts(tmp_path / "unsafe-retained", evidence_runs)
    unsafe_sources = [
        EvidenceArtifactSource(
            direct_run_id=(
                "../../escape"
                if item.direct_run_id == evidence_runs[0].direct_run_id
                else item.direct_run_id
            ),
            role=item.role,
            source_path=item.source_path,
            sha256=item.sha256,
            byte_size=item.byte_size,
            media_type=item.media_type,
        )
        for item in unsafe_sources
    ]
    unsafe_summary = summary.model_copy(
        update={
            "direct_run_ids": [
                "../../escape" if run_id == evidence_runs[0].direct_run_id else run_id
                for run_id in summary.direct_run_ids
            ]
        }
    )
    with pytest.raises(PermissionError, match="safe opaque IDs"):
        create_evidence_export(
            output_root=tmp_path,
            export_id="unsafe_export",
            baseline_portfolio_id=fixture.baseline_portfolio_id,
            bundle=professional_bundle,
            current_authorization_bundle=professional_bundle,
            deployment="WANDB_CLOUD",
            engine_manifest=engine_manifest,
            run_plan=run_plan,
            semantic_diff=None,
            eligibility=eligibility,
            summaries=[unsafe_summary],
            runs=unsafe_runs,
            claims=unsafe_claims,
            artifact_sources=unsafe_sources,
            project_asset_sources=_project_asset_sources(
                tmp_path / "unsafe-project-assets", fixture
            ),
        )
    assert (
        manifest["files"]["decision-story.html"]
        == hashlib.sha256(story.encode("utf-8")).hexdigest()
    )

    blocked = compute_export_eligibility(
        bundle=professional_bundle,
        current_authorization_bundle=professional_bundle,
        summaries=[summary],
        runs=runs[:-1] + baseline_runs,
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
    )
    assert blocked.eligible is False
    assert blocked.complete_ensemble is False
