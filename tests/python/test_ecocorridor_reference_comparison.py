from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    ConnectivityScenario,
    DirectConnectivityRun,
    DirectRunState,
    EvidenceEdge,
    EvidenceNode,
    PairResistanceMetric,
    PortfolioSelection,
    QAStatus,
    RasterGrid,
    RasterValueKind,
    ReferenceComparisonPlan,
    ReferenceEvidenceClaimLink,
    ReferenceMetricTolerance,
    ReferenceScenarioBinding,
    ReferenceScenarioComparison,
    SensitiveDataAuthorization,
    SensitivityClass,
)
from ecocorridor_core.engine import PreparedPair
from ecocorridor_core.metrics import CONNECTIVITY_LIMITATION, validate_evidence_claim
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.reference_comparison import (
    ReferenceComparisonError,
    assemble_reference_comparison_result,
    compare_reference_matrix,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
MODEL_SHA = "a" * 64
ENGINE_SHA = "b" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_matrix(path: Path, *, pair_a: float, pair_b: float) -> Path:
    path.write_text(
        f"1 2 {pair_b:.17g}\n1 3 -1\n2 3 {pair_a:.17g}\n",
        encoding="utf-8",
    )
    return path


def _authorization(asset_id: str) -> SensitiveDataAuthorization:
    return SensitiveDataAuthorization(
        asset_id=asset_id,
        ownership="Synthetic test owner",
        license_spdx_or_name="CC0-1.0",
        sensitivity=SensitivityClass.PUBLIC,
        cloud_disposition=CloudDisposition.RAW_ALLOWED,
        export_allowed=True,
        retention_policy="Retain with test fixture.",
        spatial_redaction="No locations are present.",
        approved_deployments=["LOCAL_ONLY"],
        reviewer="Test reviewer",
        reviewed_at=NOW,
    )


def _grid(*, x_offset: float = 0.0, nodata: float = -9999.0) -> RasterGrid:
    return RasterGrid(
        grid_id="grid-a",
        crs="EPSG:5070",
        width=4,
        height=3,
        transform=(30.0, 0.0, x_offset, 0.0, -30.0, 90.0),
        nodata=nodata,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=900.0,
    )


def _asset(
    asset_id: str,
    kind: AssetKind,
    path: Path,
    *,
    grid: RasterGrid | None = None,
) -> ConnectivityAsset:
    return ConnectivityAsset(
        asset_id=asset_id,
        kind=kind,
        title=asset_id,
        relative_path=f"reference/{path.name}",
        sha256=_sha256(path),
        byte_size=path.stat().st_size,
        media_type="text/plain",
        source="Synthetic test evidence",
        vintage="2026",
        license="CC0-1.0",
        grid=grid,
        authorization=_authorization(asset_id),
    )


def _scenario(scenario_id: str) -> ConnectivityScenario:
    return ConnectivityScenario(
        scenario_id=scenario_id,
        title=scenario_id,
        guild_id="guild-a",
        life_stage="adult",
        resistance_hypothesis_id="hypothesis-a",
        focal_set_id="focal-a",
        future_condition="present",
        maturity_horizon="year-0",
        included_pair_ids=["pair-b", "pair-a"],
        grid_id="grid-a",
    )


def _pairs() -> list[PreparedPair]:
    return [
        PreparedPair(pair_id="pair-b", from_focal_id=1, to_focal_id=2),
        PreparedPair(pair_id="pair-a", from_focal_id=2, to_focal_id=3),
    ]


def _plan(
    scenario_ids: list[str],
    binding_ids: list[str],
) -> ReferenceComparisonPlan:
    baseline = PortfolioSelection(
        portfolio_id="baseline",
        selections={"site-a": "no-action"},
        canonical_option_ids=("no-action",),
    )
    run_plan = ConnectivityRunPlan(
        analysis_id="reference-analysis",
        revision_id="revision-a",
        scenario_ids=scenario_ids,
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(scenario_ids),
        solver_job_cap=len(scenario_ids),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=ENGINE_SHA,
    )
    return ReferenceComparisonPlan(
        plan_id="reference-plan",
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        model_sha256=MODEL_SHA,
        engine_manifest_sha256=ENGINE_SHA,
        binding_ids=binding_ids,
        bound_scenario_ids=scenario_ids,
        run_plan=run_plan,
        created_at=NOW,
    )


def _binding(
    binding_id: str,
    scenario_id: str,
    matrix_asset_id: str,
    *,
    current_asset_id: str | None = None,
) -> ReferenceScenarioBinding:
    return ReferenceScenarioBinding(
        binding_id=binding_id,
        scenario_id=scenario_id,
        reference_matrix_asset_id=matrix_asset_id,
        tolerance=ReferenceMetricTolerance(
            absolute_tolerance=0.5,
            relative_tolerance=0.01,
        ),
        reference_current_asset_id=current_asset_id,
        current_raster_disposition=(
            "DIAGNOSTIC_ONLY_NOT_NUMERICALLY_COMPARED_V0_1"
            if current_asset_id is not None
            else "NOT_PROVIDED"
        ),
        reference_basis="Approved synthetic reference matrix.",
        reviewed_by="Test reviewer",
        reviewed_at=NOW,
        limitations=["Synthetic reference evidence."],
    )


def _direct_run(
    *,
    scenario_id: str,
    matrix_path: Path,
    pair_a: float,
    pair_b: float,
) -> DirectConnectivityRun:
    return DirectConnectivityRun(
        direct_run_id=f"run-{scenario_id}",
        analysis_id="reference-analysis",
        revision_id="revision-a",
        portfolio_id="baseline",
        scenario_id=scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=ENGINE_SHA,
        model_run_cache_key="c" * 64,
        input_sha256="d" * 64,
        result_sha256="e" * 64,
        matrix_sha256=_sha256(matrix_path),
        pair_metrics=[
            PairResistanceMetric(pair_id="pair-b", effective_resistance=pair_b),
            PairResistanceMetric(pair_id="pair-a", effective_resistance=pair_a),
        ],
        qa_status=QAStatus.ACCEPTED,
        completed_at=NOW,
    )


def _compare(
    tmp_path: Path,
    *,
    plan: ReferenceComparisonPlan,
    scenario_id: str,
    binding_id: str,
    reference_a: float = 50.0,
    reference_b: float = 100.0,
    observed_a: float = 51.0,
    observed_b: float = 101.5,
    include_current: bool = False,
    current_grid: RasterGrid | None = None,
):
    reference_path = _write_matrix(
        tmp_path / f"{scenario_id}-reference.txt",
        pair_a=reference_a,
        pair_b=reference_b,
    )
    observed_path = _write_matrix(
        tmp_path / f"{scenario_id}-observed.txt",
        pair_a=observed_a,
        pair_b=observed_b,
    )
    matrix_asset = _asset(
        f"matrix-{scenario_id}",
        AssetKind.REFERENCE_MATRIX,
        reference_path,
    )
    current_asset = None
    if include_current:
        current_path = tmp_path / f"{scenario_id}-current.bin"
        current_path.write_bytes(b"diagnostic bytes that are not a numeric comparison")
        current_asset = _asset(
            f"current-{scenario_id}",
            AssetKind.REFERENCE_CURRENT,
            current_path,
            grid=current_grid or _grid(),
        )
    binding = _binding(
        binding_id,
        scenario_id,
        matrix_asset.asset_id,
        current_asset_id=current_asset.asset_id if current_asset is not None else None,
    )
    scenario = _scenario(scenario_id)
    direct_run = _direct_run(
        scenario_id=scenario_id,
        matrix_path=observed_path,
        pair_a=observed_a,
        pair_b=observed_b,
    )
    return compare_reference_matrix(
        comparison_id=f"comparison-{scenario_id}",
        plan=plan,
        binding=binding,
        scenario=scenario,
        expected_scenario_grid=_grid(),
        prepared_pairs=_pairs(),
        observed_run=direct_run,
        observed_matrix_path=observed_path,
        reference_matrix_asset=matrix_asset,
        reference_matrix_path=reference_path,
        reference_current_asset=current_asset,
        completed_at=NOW,
    )


def _claim_links(
    *comparisons: ReferenceScenarioComparison,
) -> list[ReferenceEvidenceClaimLink]:
    return [
        ReferenceEvidenceClaimLink(
            claim_id=f"claim-{comparison.scenario_id}-{pair.pair_id}",
            comparison_id=comparison.comparison_id,
            scenario_id=comparison.scenario_id,
            pair_id=pair.pair_id,
            direct_run_id=comparison.direct_run_id,
        )
        for comparison in comparisons
        for pair in comparison.pair_comparisons
    ]


def test_compare_reference_matrix_uses_declared_formula_and_canonical_pair_order(
    tmp_path: Path,
) -> None:
    plan = _plan(["scenario-b"], ["binding-b"])
    comparison = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-b",
        binding_id="binding-b",
        include_current=True,
    )

    assert comparison.status == "PASSED"
    assert [item.pair_id for item in comparison.pair_comparisons] == ["pair-a", "pair-b"]
    pair_a, pair_b = comparison.pair_comparisons
    assert pair_a.absolute_difference == 1.0
    assert pair_a.relative_difference == 0.02
    assert pair_a.allowed_difference == 1.0
    assert pair_a.within_tolerance is True
    assert pair_b.absolute_difference == 1.5
    assert pair_b.allowed_difference == 1.5
    assert pair_b.within_tolerance is True
    assert comparison.current_raster_numeric_comparison_performed is False
    assert comparison.reference_current_sha256 is not None
    assert FITNESS_LIMITATION in comparison.limitations
    assert "fitness" not in comparison.model_dump(mode="json")


def test_reference_current_requires_exact_geometry_but_not_identical_nodata(
    tmp_path: Path,
) -> None:
    plan = _plan(["scenario-a"], ["binding-a"])

    comparison = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-a",
        binding_id="binding-a",
        include_current=True,
        current_grid=_grid(nodata=-32768.0),
    )
    assert comparison.status == "PASSED"

    with pytest.raises(ReferenceComparisonError, match="exact bound scenario grid alignment"):
        _compare(
            tmp_path,
            plan=plan,
            scenario_id="scenario-a",
            binding_id="binding-a",
            include_current=True,
            current_grid=_grid(x_offset=1.0),
        )


def test_compare_reference_matrix_rejects_incomplete_included_pair_coverage(
    tmp_path: Path,
) -> None:
    plan = _plan(["scenario-a"], ["binding-a"])
    reference_path = tmp_path / "incomplete-reference.txt"
    reference_path.write_text("1 2 100\n1 3 -1\n", encoding="utf-8")
    observed_path = _write_matrix(tmp_path / "observed.txt", pair_a=50, pair_b=100)
    matrix_asset = _asset("matrix-a", AssetKind.REFERENCE_MATRIX, reference_path)

    with pytest.raises(ReferenceComparisonError, match="omits requested pair pair-a"):
        compare_reference_matrix(
            comparison_id="comparison-a",
            plan=plan,
            binding=_binding("binding-a", "scenario-a", matrix_asset.asset_id),
            scenario=_scenario("scenario-a"),
            expected_scenario_grid=_grid(),
            prepared_pairs=_pairs(),
            observed_run=_direct_run(
                scenario_id="scenario-a",
                matrix_path=observed_path,
                pair_a=50,
                pair_b=100,
            ),
            observed_matrix_path=observed_path,
            reference_matrix_asset=matrix_asset,
            reference_matrix_path=reference_path,
            reference_current_asset=None,
            completed_at=NOW,
        )


def test_compare_reference_matrix_rejects_metrics_not_reproduced_by_retained_matrix(
    tmp_path: Path,
) -> None:
    plan = _plan(["scenario-a"], ["binding-a"])
    reference_path = _write_matrix(tmp_path / "reference.txt", pair_a=50, pair_b=100)
    observed_path = _write_matrix(tmp_path / "observed.txt", pair_a=50, pair_b=100)
    matrix_asset = _asset("matrix-a", AssetKind.REFERENCE_MATRIX, reference_path)
    direct_run = _direct_run(
        scenario_id="scenario-a",
        matrix_path=observed_path,
        pair_a=50,
        pair_b=100,
    ).model_copy(
        update={
            "pair_metrics": [
                PairResistanceMetric(pair_id="pair-a", effective_resistance=999),
                PairResistanceMetric(pair_id="pair-b", effective_resistance=100),
            ]
        }
    )

    with pytest.raises(ReferenceComparisonError, match="differ from the retained strict"):
        compare_reference_matrix(
            comparison_id="comparison-a",
            plan=plan,
            binding=_binding("binding-a", "scenario-a", matrix_asset.asset_id),
            scenario=_scenario("scenario-a"),
            expected_scenario_grid=_grid(),
            prepared_pairs=_pairs(),
            observed_run=direct_run,
            observed_matrix_path=observed_path,
            reference_matrix_asset=matrix_asset,
            reference_matrix_path=reference_path,
            reference_current_asset=None,
            completed_at=NOW,
        )


def test_aggregate_result_is_scenario_deterministic_and_fails_if_any_pair_fails(
    tmp_path: Path,
) -> None:
    plan = _plan(
        ["scenario-z", "scenario-a"],
        ["binding-z", "binding-a"],
    )
    scenario_z = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-z",
        binding_id="binding-z",
        observed_b=102.0,
    )
    scenario_a = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-a",
        binding_id="binding-a",
    )

    result = assemble_reference_comparison_result(
        result_id="reference-result",
        plan=plan,
        scenario_comparisons=[scenario_z, scenario_a],
        evidence_claim_links=list(reversed(_claim_links(scenario_z, scenario_a))),
        completed_at=NOW,
    )

    assert result.status == "FAILED"
    assert [item.scenario_id for item in result.scenario_comparisons] == [
        "scenario-a",
        "scenario-z",
    ]
    assert [
        (item.scenario_id, item.pair_id, item.comparison_id, item.direct_run_id)
        for item in result.evidence_claim_links
    ] == [
        ("scenario-a", "pair-a", "comparison-scenario-a", "run-scenario-a"),
        ("scenario-a", "pair-b", "comparison-scenario-a", "run-scenario-a"),
        ("scenario-z", "pair-a", "comparison-scenario-z", "run-scenario-z"),
        ("scenario-z", "pair-b", "comparison-scenario-z", "run-scenario-z"),
    ]
    assert result.current_raster_numeric_comparison_performed is False
    assert FITNESS_LIMITATION in result.limitations

    with pytest.raises(ReferenceComparisonError, match="exactly one comparison"):
        assemble_reference_comparison_result(
            result_id="incomplete-result",
            plan=plan,
            scenario_comparisons=[scenario_a],
            evidence_claim_links=_claim_links(scenario_a),
            completed_at=NOW,
        )

    with pytest.raises(
        ReferenceComparisonError,
        match="one evidence claim link for every scenario pair",
    ):
        assemble_reference_comparison_result(
            result_id="missing-pair-claims",
            plan=plan,
            scenario_comparisons=[scenario_z, scenario_a],
            evidence_claim_links=_claim_links(scenario_z, scenario_a)[:-1],
            completed_at=NOW,
        )


def test_aggregate_result_rejects_swapped_unrelated_and_duplicate_claim_links(
    tmp_path: Path,
) -> None:
    plan = _plan(["scenario-z", "scenario-a"], ["binding-z", "binding-a"])
    scenario_z = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-z",
        binding_id="binding-z",
    )
    scenario_a = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-a",
        binding_id="binding-a",
    )
    links = _claim_links(scenario_z, scenario_a)

    swapped = [
        item.model_copy(
            update={
                "direct_run_id": (
                    scenario_a.direct_run_id
                    if item.scenario_id == scenario_z.scenario_id
                    else scenario_z.direct_run_id
                )
            }
        )
        for item in links
    ]
    unrelated = [
        links[0].model_copy(update={"direct_run_id": "unrelated-direct-run"}),
        *links[1:],
    ]
    duplicated_identity = [
        links[0],
        links[0].model_copy(update={"claim_id": "claim-duplicate-identity"}),
        *links[2:],
    ]

    for result_id, invalid_links, message in (
        (
            "swapped-claim-links",
            swapped,
            "one evidence claim link for every scenario pair",
        ),
        (
            "unrelated-claim-link",
            unrelated,
            "one evidence claim link for every scenario pair",
        ),
        (
            "duplicate-claim-link",
            duplicated_identity,
            "unique scenario-pair evidence claim links",
        ),
    ):
        with pytest.raises(ReferenceComparisonError, match=message):
            assemble_reference_comparison_result(
                result_id=result_id,
                plan=plan,
                scenario_comparisons=[scenario_z, scenario_a],
                evidence_claim_links=invalid_links,
                completed_at=NOW,
            )

    duplicate_claim_id = links[1].model_copy(update={"claim_id": links[0].claim_id})
    with pytest.raises(ReferenceComparisonError, match="unique, non-empty evidence claim IDs"):
        assemble_reference_comparison_result(
            result_id="duplicate-claim-id",
            plan=plan,
            scenario_comparisons=[scenario_z, scenario_a],
            evidence_claim_links=[links[0], duplicate_claim_id, *links[2:]],
            completed_at=NOW,
        )


def test_deserialized_reference_results_require_fixed_limitations(tmp_path: Path) -> None:
    plan = _plan(["scenario-a"], ["binding-a"])
    comparison = _compare(
        tmp_path,
        plan=plan,
        scenario_id="scenario-a",
        binding_id="binding-a",
    )
    result = assemble_reference_comparison_result(
        result_id="reference-result",
        plan=plan,
        scenario_comparisons=[comparison],
        evidence_claim_links=_claim_links(comparison),
        completed_at=NOW,
    )

    for required in (CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION):
        with pytest.raises(ValueError, match="current-raster and model-fitness limitations"):
            type(comparison).model_validate(
                {
                    **comparison.model_dump(mode="json"),
                    "limitations": [item for item in comparison.limitations if item != required],
                }
            )
        with pytest.raises(ValueError, match="current-raster and model-fitness limitations"):
            type(result).model_validate(
                {
                    **result.model_dump(mode="json"),
                    "limitations": [item for item in result.limitations if item != required],
                }
            )


def test_reference_claim_graph_is_bound_to_its_comparison_pair_and_direct_run(
    tmp_path: Path,
) -> None:
    claim_id = "claim-reference-scenario-a-pair-a"
    direct_run_id = "run-scenario-a"
    comparison_id = "comparison-scenario-a"
    comparison_node = EvidenceNode(
        node_id=f"reference-comparison:{comparison_id}",
        node_type="REFERENCE_COMPARISON",
        label="Reviewed matrix comparison",
        attributes={
            "comparison_id": comparison_id,
            "scenario_id": "scenario-a",
            "pair_id": "pair-a",
            "direct_run_id": direct_run_id,
        },
    )
    supporting_nodes = [
        comparison_node,
        EvidenceNode(node_id="metric:pair-a", node_type="METRIC", label="Pair comparison"),
        EvidenceNode(
            node_id=f"run:{direct_run_id}",
            node_type="DIRECT_RUN",
            label="Direct Circuitscape run",
        ),
        EvidenceNode(node_id="scenario:scenario-a", node_type="SCENARIO", label="Scenario"),
        EvidenceNode(node_id="asset:reference-a", node_type="ASSET", label="Reference matrix"),
        EvidenceNode(
            node_id="engine:manifest-a",
            node_type="ENGINE_MANIFEST",
            label="Pinned engine",
        ),
        EvidenceNode(node_id="qa:run-a", node_type="QA", label="Accepted numerical QA"),
        EvidenceNode(
            node_id="assumption:reference-a",
            node_type="ASSUMPTION",
            label="Reference limitation",
        ),
        EvidenceNode(
            node_id="authorization:reference-a",
            node_type="AUTHORIZATION",
            label="Reference authorization",
        ),
        EvidenceNode(
            node_id="artifact:matrix-a",
            node_type="ARTIFACT",
            label="Retained matrix bytes",
        ),
    ]
    claim = ConnectivityEvidenceClaim(
        claim_id=claim_id,
        claim_kind="DIRECT_REFERENCE_COMPARISON",
        text="Direct Circuitscape result under documented assumptions.",
        value=True,
        units="within_tolerance",
        portfolio_id="baseline",
        scenario_id="scenario-a",
        pair_id="pair-a",
        direct_run_ids=[direct_run_id],
        root_node_id=claim_id,
        nodes=[
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label="Reference result"),
            *supporting_nodes,
        ],
        edges=[
            EvidenceEdge(from_node_id=claim_id, to_node_id=node.node_id, relation="traces_to")
            for node in supporting_nodes
        ],
        limitations=[CONNECTIVITY_LIMITATION, CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION],
    )

    validate_evidence_claim(claim)

    mismatched_node = comparison_node.model_copy(
        update={"attributes": {**comparison_node.attributes, "pair_id": "pair-b"}}
    )
    mismatched = claim.model_copy(
        update={
            "nodes": [
                mismatched_node if node.node_id == comparison_node.node_id else node
                for node in claim.nodes
            ]
        }
    )
    with pytest.raises(ValueError, match="does not match the claim subject"):
        validate_evidence_claim(mismatched)

    missing_run = claim.model_copy(update={"direct_run_ids": []})
    with pytest.raises(ValueError, match="one scenario, pair, direct run"):
        validate_evidence_claim(missing_run)

    matrix_path = _write_matrix(tmp_path / "claim-observed.txt", pair_a=50.0, pair_b=100.0)
    plan = _plan(["scenario-a"], ["binding-a"])
    run = _direct_run(
        scenario_id="scenario-a",
        matrix_path=matrix_path,
        pair_a=50.0,
        pair_b=100.0,
    )
    with pytest.raises(ValueError, match="atomically stored ReferenceComparisonResult"):
        EcoProjectStore._validate_claim_run_references(
            plan=plan.run_plan,
            runs=[run],
            claims=[claim],
        )
