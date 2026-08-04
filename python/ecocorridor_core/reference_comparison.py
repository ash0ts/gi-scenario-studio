"""Pure, fail-closed comparison of direct matrices with reviewed references.

This module does not persist evidence, attest model fitness, or compare current
rasters.  It only derives versioned reference-comparison contracts from one
baseline direct run and retained matrix bytes.  Both observed and reference
matrices pass through the same strict Circuitscape parser used by the engine.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    AssetKind,
    ConnectivityAsset,
    ConnectivityScenario,
    DirectConnectivityRun,
    DirectRunState,
    QAStatus,
    RasterGrid,
    RasterValueKind,
    ReferenceComparisonPlan,
    ReferenceComparisonResult,
    ReferenceEvidenceClaimLink,
    ReferencePairComparison,
    ReferenceScenarioBinding,
    ReferenceScenarioComparison,
)
from .engine import (
    EngineProtocolError,
    PreparedPair,
    ProtocolPairMetric,
    parse_circuitscape_matrix,
    sha256_file,
)
from .grid import grid_identity


class ReferenceComparisonError(ValueError):
    """Raised when exact reference-comparison evidence is incomplete or inconsistent."""


def compare_reference_matrix(
    *,
    comparison_id: str,
    plan: ReferenceComparisonPlan,
    binding: ReferenceScenarioBinding,
    scenario: ConnectivityScenario,
    expected_scenario_grid: RasterGrid,
    prepared_pairs: Sequence[PreparedPair],
    observed_run: DirectConnectivityRun,
    observed_matrix_path: Path,
    reference_matrix_asset: ConnectivityAsset,
    reference_matrix_path: Path,
    reference_current_asset: ConnectivityAsset | None,
    completed_at: datetime,
) -> ReferenceScenarioComparison:
    """Compare every included pair for one bound baseline scenario.

    The tolerance is exactly ``absolute + relative * abs(reference)``.  The
    optional current asset contributes identity and a limitation only; this
    function intentionally accepts no current-raster path or observed current
    values, so it cannot perform a numerical current-raster comparison.
    """

    _validate_scenario_identity(
        plan=plan,
        binding=binding,
        scenario=scenario,
        observed_run=observed_run,
    )
    pairs = _canonical_pairs(prepared_pairs, scenario=scenario)
    _validate_reference_assets(
        binding=binding,
        scenario=scenario,
        expected_scenario_grid=expected_scenario_grid,
        reference_matrix_asset=reference_matrix_asset,
        reference_current_asset=reference_current_asset,
    )

    reference_metrics = _strict_parse(
        reference_matrix_path,
        pairs,
        label="reference",
    )
    observed_metrics = _strict_parse(
        observed_matrix_path,
        pairs,
        label="observed",
    )
    reference_sha256 = sha256_file(reference_matrix_path)
    observed_sha256 = sha256_file(observed_matrix_path)
    if reference_sha256 != reference_matrix_asset.sha256:
        raise ReferenceComparisonError(
            "reference matrix bytes differ from the bound reference asset"
        )
    if observed_sha256 != observed_run.matrix_sha256:
        raise ReferenceComparisonError(
            "observed matrix bytes differ from the direct-run matrix evidence"
        )
    _validate_run_metrics(observed_run, observed_metrics, scenario=scenario)

    reference_by_pair = _by_pair(reference_metrics, label="reference")
    observed_by_pair = _by_pair(observed_metrics, label="observed")
    expected_pair_ids = set(scenario.included_pair_ids)
    if set(reference_by_pair) != expected_pair_ids or set(observed_by_pair) != expected_pair_ids:
        raise ReferenceComparisonError(
            "reference comparison does not cover every included scenario pair"
        )

    tolerance = binding.tolerance
    comparisons: list[ReferencePairComparison] = []
    for pair_id in sorted(expected_pair_ids):
        reference_value = reference_by_pair[pair_id].effective_resistance
        observed_value = observed_by_pair[pair_id].effective_resistance
        absolute_difference = abs(observed_value - reference_value)
        relative_difference = absolute_difference / abs(reference_value)
        allowed_difference = tolerance.absolute_tolerance + (
            tolerance.relative_tolerance * abs(reference_value)
        )
        comparisons.append(
            ReferencePairComparison(
                pair_id=pair_id,
                reference_effective_resistance=reference_value,
                observed_effective_resistance=observed_value,
                tolerance=tolerance,
                absolute_difference=absolute_difference,
                relative_difference=relative_difference,
                allowed_difference=allowed_difference,
                within_tolerance=absolute_difference <= allowed_difference,
            )
        )

    limitations = _unique([*binding.limitations, CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION])
    return ReferenceScenarioComparison(
        comparison_id=comparison_id,
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        model_sha256=plan.model_sha256,
        binding_id=binding.binding_id,
        scenario_id=scenario.scenario_id,
        direct_run_id=observed_run.direct_run_id,
        reference_matrix_asset_id=reference_matrix_asset.asset_id,
        reference_matrix_sha256=reference_sha256,
        observed_matrix_sha256=observed_sha256,
        engine_manifest_sha256=observed_run.engine_manifest_sha256,
        tolerance=tolerance,
        pair_comparisons=comparisons,
        qa_status=observed_run.qa_status,
        status=("PASSED" if all(item.within_tolerance for item in comparisons) else "FAILED"),
        reference_current_asset_id=(
            reference_current_asset.asset_id if reference_current_asset is not None else None
        ),
        reference_current_sha256=(
            reference_current_asset.sha256 if reference_current_asset is not None else None
        ),
        current_raster_disposition=binding.current_raster_disposition,
        current_raster_numeric_comparison_performed=False,
        exact_direct_evidence_only=True,
        completed_at=completed_at,
        limitations=limitations,
    )


def assemble_reference_comparison_result(
    *,
    result_id: str,
    plan: ReferenceComparisonPlan,
    scenario_comparisons: Sequence[ReferenceScenarioComparison],
    evidence_claim_links: Sequence[ReferenceEvidenceClaimLink],
    completed_at: datetime,
    limitations: Sequence[str] = (),
) -> ReferenceComparisonResult:
    """Assemble a deterministic, complete result for the declared reference plan."""

    ordered = sorted(
        scenario_comparisons,
        key=lambda item: (item.scenario_id, item.binding_id, item.comparison_id),
    )
    scenario_ids = [item.scenario_id for item in ordered]
    binding_ids = [item.binding_id for item in ordered]
    if len(ordered) != len(plan.bound_scenario_ids):
        raise ReferenceComparisonError(
            "reference result requires exactly one comparison per bound scenario"
        )
    if set(scenario_ids) != set(plan.bound_scenario_ids):
        raise ReferenceComparisonError(
            "reference result scenario coverage differs from the comparison plan"
        )
    if set(binding_ids) != set(plan.binding_ids):
        raise ReferenceComparisonError(
            "reference result binding coverage differs from the comparison plan"
        )
    if any(
        item.analysis_id != plan.analysis_id
        or item.revision_id != plan.revision_id
        or item.model_sha256 != plan.model_sha256
        or item.engine_manifest_sha256 != plan.engine_manifest_sha256
        for item in ordered
    ):
        raise ReferenceComparisonError(
            "reference scenario comparisons differ from the comparison-plan identity"
        )

    links = sorted(
        evidence_claim_links,
        key=lambda item: (
            item.scenario_id,
            item.pair_id,
            item.comparison_id,
            item.direct_run_id,
            item.claim_id,
        ),
    )
    claim_ids = [item.claim_id for item in links]
    if not links or len(claim_ids) != len(set(claim_ids)):
        raise ReferenceComparisonError(
            "reference results require unique, non-empty evidence claim IDs"
        )
    expected_links = {
        (
            comparison.comparison_id,
            comparison.scenario_id,
            pair.pair_id,
            comparison.direct_run_id,
        )
        for comparison in ordered
        for pair in comparison.pair_comparisons
    }
    actual_links = {
        (
            item.comparison_id,
            item.scenario_id,
            item.pair_id,
            item.direct_run_id,
        )
        for item in links
    }
    if len(actual_links) != len(links):
        raise ReferenceComparisonError(
            "reference results require unique scenario-pair evidence claim links"
        )
    if actual_links != expected_links:
        raise ReferenceComparisonError(
            "reference results require exactly one evidence claim link for every scenario pair"
        )
    result_limitations = _unique([*limitations, CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION])
    return ReferenceComparisonResult(
        result_id=result_id,
        plan_id=plan.plan_id,
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        model_sha256=plan.model_sha256,
        engine_manifest_sha256=plan.engine_manifest_sha256,
        scenario_comparisons=ordered,
        status=("PASSED" if all(item.status == "PASSED" for item in ordered) else "FAILED"),
        exact_direct_evidence_only=True,
        current_raster_numeric_comparison_performed=False,
        evidence_claim_links=links,
        completed_at=completed_at,
        limitations=result_limitations,
    )


def _validate_scenario_identity(
    *,
    plan: ReferenceComparisonPlan,
    binding: ReferenceScenarioBinding,
    scenario: ConnectivityScenario,
    observed_run: DirectConnectivityRun,
) -> None:
    if binding.binding_id not in plan.binding_ids:
        raise ReferenceComparisonError("reference binding is not included in the comparison plan")
    if (
        binding.scenario_id != scenario.scenario_id
        or scenario.scenario_id not in plan.bound_scenario_ids
    ):
        raise ReferenceComparisonError("reference binding, scenario, and plan do not match")
    if (
        observed_run.analysis_id != plan.analysis_id
        or observed_run.revision_id != plan.revision_id
        or observed_run.scenario_id != scenario.scenario_id
        or observed_run.portfolio_id != plan.run_plan.baseline_portfolio_id
        or observed_run.engine_manifest_sha256 != plan.engine_manifest_sha256
    ):
        raise ReferenceComparisonError(
            "observed direct run does not belong to the planned baseline scenario"
        )
    if (
        observed_run.state != DirectRunState.SUCCEEDED
        or not observed_run.exact
        or observed_run.test_only
        or observed_run.engine_name != "Circuitscape"
        or observed_run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}
        or observed_run.matrix_sha256 is None
    ):
        raise ReferenceComparisonError(
            "reference comparison requires an accepted exact Circuitscape direct run"
        )


def _canonical_pairs(
    prepared_pairs: Sequence[PreparedPair],
    *,
    scenario: ConnectivityScenario,
) -> list[PreparedPair]:
    pairs = sorted(prepared_pairs, key=lambda item: item.pair_id)
    pair_ids = [item.pair_id for item in pairs]
    if len(pair_ids) != len(set(pair_ids)) or set(pair_ids) != set(scenario.included_pair_ids):
        raise ReferenceComparisonError(
            "prepared pairs must uniquely and completely cover the scenario included pairs"
        )
    return pairs


def _validate_reference_assets(
    *,
    binding: ReferenceScenarioBinding,
    scenario: ConnectivityScenario,
    expected_scenario_grid: RasterGrid,
    reference_matrix_asset: ConnectivityAsset,
    reference_current_asset: ConnectivityAsset | None,
) -> None:
    if expected_scenario_grid.grid_id != scenario.grid_id:
        raise ReferenceComparisonError(
            "expected scenario grid does not match the bound scenario identity"
        )
    if (
        reference_matrix_asset.asset_id != binding.reference_matrix_asset_id
        or reference_matrix_asset.kind != AssetKind.REFERENCE_MATRIX
    ):
        raise ReferenceComparisonError(
            "reference matrix asset does not match the reviewed scenario binding"
        )
    if binding.reference_current_asset_id is None:
        if reference_current_asset is not None:
            raise ReferenceComparisonError(
                "an unbound reference current asset cannot enter the comparison"
            )
        return
    if (
        reference_current_asset is None
        or reference_current_asset.asset_id != binding.reference_current_asset_id
        or reference_current_asset.kind != AssetKind.REFERENCE_CURRENT
    ):
        raise ReferenceComparisonError(
            "reference current asset does not match the diagnostic-only binding"
        )
    if (
        reference_current_asset.grid is None
        or reference_current_asset.grid.value_kind != RasterValueKind.CONTINUOUS
        or grid_identity(reference_current_asset.grid) != grid_identity(expected_scenario_grid)
    ):
        raise ReferenceComparisonError(
            "reference current asset does not use the exact bound scenario grid alignment"
        )


def _strict_parse(
    path: Path,
    pairs: list[PreparedPair],
    *,
    label: str,
) -> list[ProtocolPairMetric]:
    try:
        return parse_circuitscape_matrix(Path(path), pairs)
    except EngineProtocolError as exc:
        raise ReferenceComparisonError(
            f"{label} matrix failed strict Circuitscape parsing: {exc}"
        ) from exc


def _validate_run_metrics(
    observed_run: DirectConnectivityRun,
    observed_metrics: Sequence[ProtocolPairMetric],
    *,
    scenario: ConnectivityScenario,
) -> None:
    retained = {item.pair_id: item.effective_resistance for item in observed_run.pair_metrics}
    reparsed = {item.pair_id: item.effective_resistance for item in observed_metrics}
    if (
        len(retained) != len(observed_run.pair_metrics)
        or set(retained) != set(scenario.included_pair_ids)
        or retained != reparsed
    ):
        raise ReferenceComparisonError(
            "observed direct-run metrics differ from the retained strict matrix parse"
        )


def _by_pair(
    metrics: Sequence[ProtocolPairMetric],
    *,
    label: str,
) -> dict[str, ProtocolPairMetric]:
    output = {item.pair_id: item for item in metrics}
    if len(output) != len(metrics):
        raise ReferenceComparisonError(f"{label} matrix contains duplicate pair identities")
    return output


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
