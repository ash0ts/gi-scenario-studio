from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    PUBLIC_CONTRACTS,
    AssetKind,
    CloudDisposition,
    ComputationalResourceObservation,
    ConnectivityAsset,
    ConnectivityEvidenceClaim,
    ConnectivityModelFitness,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ConnectivityScenario,
    EngineMode,
    EvidenceEdge,
    EvidenceNode,
    QAStatus,
    RasterSemantics,
    ReferenceComparisonPlan,
    ReferenceComparisonResult,
    ReferenceEvidenceClaimLink,
    ReferenceMetricTolerance,
    ReferencePairComparison,
    ReferenceScenarioBinding,
    ReferenceScenarioComparison,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    SensitiveDataAuthorization,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.schema import render_public_schemas, write_public_schemas
from pydantic import ValidationError

EXPECTED_PUBLIC_CONTRACTS = (
    "ConnectivityProjectBundle",
    "ConnectivityDecisionBrief",
    "ConnectivityModelFitness",
    "ResistanceHypothesis",
    "FocalAreaSet",
    "ConnectivityScenario",
    "ReferenceMetricTolerance",
    "ReferenceScenarioBinding",
    "InterventionSite",
    "InterventionOption",
    "ConnectivityCostBasis",
    "ActionEffectRaster",
    "ActionEffectBasis",
    "SafeguardRecord",
    "RiskReviewMask",
    "ConnectivityDecisionRule",
    "ConnectivityRunPlan",
    "ReferencePairComparison",
    "ReferenceScenarioComparison",
    "ReferenceComparisonPlan",
    "ReferenceEvidenceClaimLink",
    "ReferenceComparisonResult",
    "DirectConnectivityRun",
    "PortfolioDecisionSummary",
    "ConnectivityEvidenceClaim",
    "GridAlignmentPlan",
    "GridAlignmentApproval",
    "SensitiveDataAuthorization",
    "EngineManifest",
    "ResourcePolicy",
    "ConnectivityExportEligibility",
    "ModelRevision",
    "SemanticModelDiff",
)


def test_public_contracts_emit_deterministic_draft_2020_schemas() -> None:
    assert tuple(contract.__name__ for contract in PUBLIC_CONTRACTS) == EXPECTED_PUBLIC_CONTRACTS
    rendered = render_public_schemas()
    expected = {f"{contract.__name__}.schema.json" for contract in PUBLIC_CONTRACTS} | {
        "index.json"
    }
    assert set(rendered) == expected
    assert '"additionalProperties": false' in rendered["ConnectivityProjectBundle.schema.json"]
    assert (
        '"$schema": "https://json-schema.org/draft/2020-12/schema"'
        in rendered["ConnectivityScenario.schema.json"]
    )
    assert rendered == render_public_schemas()


def test_committed_json_schema_and_zod_outputs_have_no_drift() -> None:
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "generate_ecocorridor_schemas.py"),
            "--check",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_schema_writer_removes_only_stale_generator_owned_json(tmp_path: Path) -> None:
    stale = tmp_path / "RemovedContract.schema.json"
    unrelated = tmp_path / "README.txt"
    stale.write_text("{}\n", encoding="utf-8")
    unrelated.write_text("keep\n", encoding="utf-8")

    write_public_schemas(tmp_path)

    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep\n"


def test_generated_schemas_preserve_hash_time_and_engine_fail_closed_rules() -> None:
    schemas = render_public_schemas()
    scenario = json.loads(schemas["ConnectivityScenario.schema.json"])
    scenario_properties = scenario["properties"]
    assert scenario_properties["engine_mode"]["const"] == "RASTER_PAIRWISE"
    assert scenario_properties["raster_semantics"]["const"] == "RESISTANCE"
    manifest = json.loads(schemas["EngineManifest.schema.json"])
    digest = manifest["properties"]["julia_runtime_sha256"]
    assert digest["minLength"] == digest["maxLength"] == 64
    assert digest["pattern"] == "^[0-9a-f]{64}$"
    assert manifest["properties"]["created_at"]["format"] == "date-time"
    typescript = Path("apps/ecocorridor-desktop/src/generated/contracts.ts").read_text(
        encoding="utf-8"
    )
    assert 'z.literal("RASTER_PAIRWISE")' in typescript
    assert 'new RegExp("^[0-9a-f]{64}$")' in typescript
    assert "Expected timezone-aware ISO date-time" in typescript


def test_unsupported_modes_and_conductance_fail_closed() -> None:
    base = {
        "scenario_id": "s1",
        "title": "Unsupported",
        "guild_id": "g1",
        "life_stage": "adult",
        "resistance_hypothesis_id": "h1",
        "focal_set_id": "f1",
        "future_condition": "current",
        "maturity_horizon": "mature",
        "included_pair_ids": ["p1"],
        "grid_id": "grid",
    }
    with pytest.raises(ValidationError, match="RASTER_PAIRWISE"):
        ConnectivityScenario.model_validate({**base, "engine_mode": EngineMode.ONE_TO_ALL})
    with pytest.raises(ValidationError, match="RESISTANCE"):
        ConnectivityScenario.model_validate(
            {**base, "raster_semantics": RasterSemantics.CONDUCTANCE}
        )


def _reference_tolerance() -> ReferenceMetricTolerance:
    return ReferenceMetricTolerance(
        absolute_tolerance=1.0,
        relative_tolerance=0.005,
    )


def _passing_reference_pair() -> ReferencePairComparison:
    return ReferencePairComparison(
        pair_id="pair-a-b",
        reference_effective_resistance=100.0,
        observed_effective_resistance=101.2,
        tolerance=_reference_tolerance(),
        absolute_difference=1.2,
        relative_difference=0.012,
        allowed_difference=1.5,
        within_tolerance=True,
    )


def test_reference_pair_comparison_math_and_current_boundary_are_fail_closed() -> None:
    pair = _passing_reference_pair()
    assert pair.within_tolerance is True
    assert pair.tolerance.comparison_formula == (
        "ABS_OBSERVED_MINUS_REFERENCE_LE_ABSOLUTE_PLUS_RELATIVE_TIMES_ABS_REFERENCE"
    )

    with pytest.raises(ValidationError, match="relative difference"):
        ReferencePairComparison.model_validate(
            {**pair.model_dump(mode="json"), "relative_difference": 0.02}
        )
    with pytest.raises(ValidationError, match="declared comparison formula"):
        ReferencePairComparison.model_validate(
            {**pair.model_dump(mode="json"), "within_tolerance": False}
        )

    common = {
        "binding_id": "reference-binding-a",
        "scenario_id": "scenario-a",
        "reference_matrix_asset_id": "reference-matrix-a",
        "tolerance": _reference_tolerance().model_dump(mode="json"),
        "reference_basis": "Approved direct-model reference output for this scenario.",
        "reviewed_by": "Review ecologist",
        "reviewed_at": datetime(2026, 8, 4, tzinfo=UTC),
        "limitations": ["Reference reproduction is not ecological field validation."],
    }
    without_current = ReferenceScenarioBinding.model_validate(common)
    assert without_current.current_raster_disposition == "NOT_PROVIDED"
    assert without_current.current_raster_numeric_comparison_allowed is False

    with pytest.raises(ValidationError, match="diagnostic-only"):
        ReferenceScenarioBinding.model_validate(
            {**common, "reference_current_asset_id": "reference-current-a"}
        )
    with_current = ReferenceScenarioBinding.model_validate(
        {
            **common,
            "reference_current_asset_id": "reference-current-a",
            "current_raster_disposition": ("DIAGNOSTIC_ONLY_NOT_NUMERICALLY_COMPARED_V0_1"),
        }
    )
    assert with_current.current_raster_numeric_comparison_allowed is False


def test_reference_plan_and_result_require_direct_baseline_matrix_evidence() -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    run_plan = ConnectivityRunPlan(
        analysis_id="analysis-reference",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="a" * 64,
    )
    plan = ReferenceComparisonPlan(
        plan_id="reference-plan-a",
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        model_sha256=fixture.bundle.revision.model_sha256,
        engine_manifest_sha256=run_plan.engine_manifest_sha256,
        binding_ids=["reference-binding-a"],
        bound_scenario_ids=[scenario.scenario_id],
        run_plan=run_plan,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    comparison = ReferenceScenarioComparison(
        comparison_id="reference-comparison-a",
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        model_sha256=fixture.bundle.revision.model_sha256,
        binding_id="reference-binding-a",
        scenario_id=scenario.scenario_id,
        direct_run_id="direct-reference-a",
        reference_matrix_asset_id="reference-matrix-a",
        reference_matrix_sha256="b" * 64,
        observed_matrix_sha256="c" * 64,
        engine_manifest_sha256=run_plan.engine_manifest_sha256,
        tolerance=_reference_tolerance(),
        pair_comparisons=[
            _passing_reference_pair(),
            _passing_reference_pair().model_copy(update={"pair_id": "pair-b-c"}),
        ],
        qa_status=QAStatus.ACCEPTED,
        status="PASSED",
        completed_at=datetime(2026, 8, 4, tzinfo=UTC),
        limitations=[
            "Matrix reproduction is not ecological field validation.",
            CURRENT_RASTER_LIMITATION,
            FITNESS_LIMITATION,
        ],
    )
    result = ReferenceComparisonResult(
        result_id="reference-result-a",
        plan_id=plan.plan_id,
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        model_sha256=plan.model_sha256,
        engine_manifest_sha256=plan.engine_manifest_sha256,
        scenario_comparisons=[comparison],
        status="PASSED",
        evidence_claim_links=[
            ReferenceEvidenceClaimLink(
                claim_id=f"claim-reference-{pair.pair_id}",
                comparison_id=comparison.comparison_id,
                scenario_id=comparison.scenario_id,
                pair_id=pair.pair_id,
                direct_run_id=comparison.direct_run_id,
            )
            for pair in comparison.pair_comparisons
        ],
        completed_at=datetime(2026, 8, 4, tzinfo=UTC),
        limitations=[
            "Direct reference matching does not establish ecological validity.",
            CURRENT_RASTER_LIMITATION,
            FITNESS_LIMITATION,
        ],
    )
    assert result.exact_direct_evidence_only is True
    assert result.current_raster_numeric_comparison_performed is False

    with pytest.raises(ValidationError, match="does not request current-map equivalence"):
        ReferenceComparisonPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "run_plan": {
                    **run_plan.model_dump(mode="json"),
                    "current_maps_for_portfolio_ids": [baseline.portfolio_id],
                },
            }
        )
    with pytest.raises(ValidationError, match="status must derive"):
        ReferenceComparisonResult.model_validate(
            {**result.model_dump(mode="json"), "status": "FAILED"}
        )
    valid_links = result.model_dump(mode="json")["evidence_claim_links"]
    for invalid_links, message in (
        (valid_links[:-1], "one evidence claim link for every scenario pair"),
        (
            [
                {**valid_links[0], "direct_run_id": "swapped-direct-run"},
                valid_links[1],
            ],
            "one evidence claim link for every scenario pair",
        ),
        (
            [
                {**valid_links[0], "pair_id": "unrelated-pair"},
                valid_links[1],
            ],
            "one evidence claim link for every scenario pair",
        ),
        (
            [
                valid_links[0],
                {**valid_links[0], "claim_id": "claim-reference-duplicate-link"},
            ],
            "scenario-pair claim links must be unique",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            ReferenceComparisonResult.model_validate(
                {
                    **result.model_dump(mode="json"),
                    "evidence_claim_links": invalid_links,
                }
            )

    for model, payload in (
        (comparison, comparison.model_dump(mode="json")),
        (result, result.model_dump(mode="json")),
    ):
        with pytest.raises(ValidationError, match="current-raster and model-fitness limitations"):
            type(model).model_validate(
                {
                    **payload,
                    "limitations": ["Direct reference matching is not ecological validation."],
                }
            )

    reference_claim = ConnectivityEvidenceClaim(
        claim_id="claim-reference-pair-a-b",
        claim_kind="DIRECT_REFERENCE_COMPARISON",
        text="Direct Circuitscape result under documented assumptions.",
        value=True,
        units="within_tolerance",
        portfolio_id=baseline.portfolio_id,
        scenario_id=scenario.scenario_id,
        pair_id=comparison.pair_comparisons[0].pair_id,
        direct_run_ids=[comparison.direct_run_id],
        root_node_id="claim-reference-pair-a-b",
        nodes=[
            EvidenceNode(
                node_id="claim-reference-pair-a-b",
                node_type="CLAIM",
                label="Reference comparison claim",
            ),
            EvidenceNode(
                node_id=comparison.direct_run_id,
                node_type="DIRECT_RUN",
                label="Direct reference run",
            ),
        ],
        edges=[
            EvidenceEdge(
                from_node_id="claim-reference-pair-a-b",
                to_node_id=comparison.direct_run_id,
                relation="supported_by",
            )
        ],
        limitations=[CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION],
    )
    assert reference_claim.claim_kind == "DIRECT_REFERENCE_COMPARISON"
    with pytest.raises(ValidationError, match="one scenario, pair, and direct run"):
        ConnectivityEvidenceClaim.model_validate(
            {**reference_claim.model_dump(mode="json"), "pair_id": None}
        )
    with pytest.raises(ValidationError, match="Boolean within_tolerance"):
        ConnectivityEvidenceClaim.model_validate(
            {**reference_claim.model_dump(mode="json"), "value": "PASSED"}
        )


def test_project_bundle_reference_bindings_are_portable_and_semantically_typed() -> None:
    fixture = build_ridgeway_fixture()
    assert "reference_bindings" not in fixture.bundle.model_dump(mode="json")
    scenario = fixture.bundle.scenarios[0]
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    resistance = next(
        item for item in fixture.bundle.assets if item.asset_id == hypothesis.resistance_asset_id
    )
    matrix_id = "reference-matrix-a"
    current_id = "reference-current-a"
    matrix = ConnectivityAsset.model_validate(
        {
            **resistance.model_dump(mode="json"),
            "asset_id": matrix_id,
            "kind": AssetKind.REFERENCE_MATRIX,
            "title": "Approved reference matrix",
            "relative_path": "references/scenario-a-matrix.txt",
            "sha256": "d" * 64,
            "media_type": "text/plain",
            "grid": None,
            "authorization": resistance.authorization.model_copy(
                update={"asset_id": matrix_id}
            ).model_dump(mode="json"),
        }
    )
    current = ConnectivityAsset.model_validate(
        {
            **resistance.model_dump(mode="json"),
            "asset_id": current_id,
            "kind": AssetKind.REFERENCE_CURRENT,
            "title": "Approved diagnostic current raster",
            "relative_path": "references/scenario-a-current.asc",
            "sha256": "e" * 64,
            "authorization": resistance.authorization.model_copy(
                update={"asset_id": current_id}
            ).model_dump(mode="json"),
        }
    )
    binding = ReferenceScenarioBinding(
        binding_id="reference-binding-a",
        scenario_id=scenario.scenario_id,
        reference_matrix_asset_id=matrix.asset_id,
        tolerance=_reference_tolerance(),
        reference_current_asset_id=current.asset_id,
        current_raster_disposition="DIAGNOSTIC_ONLY_NOT_NUMERICALLY_COMPARED_V0_1",
        reference_basis="Approved direct-model reference output for the exact scenario.",
        reviewed_by="Review ecologist",
        reviewed_at=datetime(2026, 8, 4, tzinfo=UTC),
        limitations=["Current raster is retained only as a diagnostic attachment."],
    )
    payload = fixture.bundle.model_dump(mode="json")
    payload["assets"] = [
        *payload["assets"],
        matrix.model_dump(mode="json"),
        current.model_dump(mode="json"),
    ]
    payload["reference_bindings"] = [binding.model_dump(mode="json")]
    bundle = ConnectivityProjectBundle.model_validate(payload)
    assert bundle.reference_bindings == [binding]
    assert bundle.model_dump(mode="json")["reference_bindings"] == [binding.model_dump(mode="json")]

    assert current.grid is not None
    alternate_nodata = current.model_copy(
        update={"grid": current.grid.model_copy(update={"nodata": -32768.0})}
    )
    ConnectivityProjectBundle.model_validate(
        {
            **payload,
            "assets": [
                alternate_nodata.model_dump(mode="json")
                if item["asset_id"] == current.asset_id
                else item
                for item in payload["assets"]
            ],
        }
    )
    transform = current.grid.transform
    shifted_current = current.model_copy(
        update={
            "grid": current.grid.model_copy(
                update={
                    "transform": (
                        transform[0],
                        transform[1],
                        transform[2] + 1.0,
                        transform[3],
                        transform[4],
                        transform[5],
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="exact reviewed continuous grid alignment"):
        ConnectivityProjectBundle.model_validate(
            {
                **payload,
                "assets": [
                    shifted_current.model_dump(mode="json")
                    if item["asset_id"] == current.asset_id
                    else item
                    for item in payload["assets"]
                ],
            }
        )

    # Unbound bytes remain representable so professional validation can return
    # an actionable blocker instead of making the portable bundle unparsable.
    unbound_payload = fixture.bundle.model_dump(mode="json")
    unbound_payload["assets"] = [*unbound_payload["assets"], matrix.model_dump(mode="json")]
    assert not ConnectivityProjectBundle.model_validate(unbound_payload).reference_bindings

    duplicate = binding.model_copy(update={"binding_id": "reference-binding-b"})
    with pytest.raises(ValidationError, match="reference-bound scenario"):
        ConnectivityProjectBundle.model_validate(
            {
                **payload,
                "reference_bindings": [
                    binding.model_dump(mode="json"),
                    duplicate.model_dump(mode="json"),
                ],
            }
        )


def test_generalized_cloud_authorization_requires_a_reviewed_derivative() -> None:
    payload = {
        "asset_id": "sensitive_raster",
        "ownership": "Agency",
        "license_spdx_or_name": "Restricted",
        "sensitivity": "SENSITIVE_LOCATION",
        "cloud_disposition": CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
        "export_allowed": True,
        "retention_policy": "Delete after project",
        "spatial_redaction": "Generalize to 10 km",
        "approved_deployments": ["DEDICATED"],
        "reviewer": "Data steward",
        "reviewed_at": datetime(2026, 8, 3, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="generalized_asset_id"):
        SensitiveDataAuthorization.model_validate(payload)


def test_fitness_passport_requires_every_independent_dimension() -> None:
    fitness = build_ridgeway_fixture().bundle.fitness.model_dump(mode="python")
    fitness["dimensions"] = fitness["dimensions"][:-1]
    with pytest.raises(ValidationError, match="requires every dimension"):
        ConnectivityModelFitness.model_validate(fitness)


def test_audit_timestamps_require_an_explicit_timezone() -> None:
    payload = build_ridgeway_fixture().bundle.fitness.model_dump(mode="python")
    payload["reviewed_at"] = "2026-08-03T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        ConnectivityModelFitness.model_validate(payload)


def test_ridgeway_bundle_cross_references_validate() -> None:
    fixture = build_ridgeway_fixture()
    assert fixture.bundle.schema_version == "1.0"
    assert len(fixture.bundle.resistance_hypotheses) == 4
    assert len(fixture.bundle.focal_area_sets) == 3
    assert len(fixture.bundle.scenarios) == 12
    assert all(
        len(option.effects) == 12
        for option in fixture.bundle.intervention_options
        if option.kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
    )


def test_contracts_reject_unknown_fields() -> None:
    scenario = build_ridgeway_fixture().bundle.scenarios[0].model_dump(mode="python")
    scenario["silent_new_setting"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConnectivityScenario.model_validate(scenario)


def test_resource_observation_never_turns_missing_energy_or_memory_into_zero() -> None:
    observation = ComputationalResourceObservation(
        observation_id="resource:direct-1",
        boundary=ResourceObservationBoundary.DIRECT_SOLVER,
        solver_wall_seconds=1.25,
        peak_memory_status=ResourceObservationStatus.NOT_MEASURED,
        peak_memory_method="NOT_COLLECTED_NO_PER_PROCESS_SAMPLER",
        energy_status=ResourceObservationStatus.NOT_MEASURED,
        energy_method="NOT_COLLECTED_NO_ENERGY_METER",
        hardware="os=test;arch=test;logical_cpus=2",
        boundary_description="One isolated direct solver process.",
    )
    assert observation.peak_memory_bytes is None
    assert observation.energy_kwh is None

    with pytest.raises(ValidationError, match="unmeasured energy"):
        ComputationalResourceObservation.model_validate(
            {**observation.model_dump(mode="python"), "energy_kwh": 0.0}
        )
    with pytest.raises(ValidationError, match="boundary-specific wall time"):
        ComputationalResourceObservation.model_validate(
            {
                **observation.model_dump(mode="python"),
                "boundary": ResourceObservationBoundary.SEARCH_PROPOSER,
            }
        )
