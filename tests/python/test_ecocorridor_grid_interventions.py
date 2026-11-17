from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    ActionCellReplacement,
    AssetKind,
    RasterGrid,
    RasterValueKind,
    ResamplingMethod,
)
from ecocorridor_core.footprints import (
    FootprintCoverageError,
    footprint_proof_set_sha256,
    prove_action_effect_footprints,
)
from ecocorridor_core.grid import (
    AlignmentApprovalRequired,
    GridMismatchError,
    InMemoryRaster,
    apply_approved_grid_alignment,
    approve_grid_alignment,
    assert_same_grid,
    preview_grid_alignment,
)
from ecocorridor_core.interventions import (
    InterventionConflictError,
    action_effect_sha256,
    apply_absolute_replacements,
    canonicalize_portfolio,
    scenario_effects_for_portfolio,
    validate_option_scenario_coverage,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _small_continuous_raster() -> InMemoryRaster:
    grid = RasterGrid(
        grid_id="grid",
        crs="EPSG:3857",
        width=2,
        height=2,
        transform=(2.0, 0.0, 0.0, 0.0, -2.0, 4.0),
        nodata=-9999.0,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=4.0,
    )
    return InMemoryRaster(
        asset_id="source",
        grid=grid,
        values=((2.0, 4.0), (6.0, 8.0)),
        source_sha256="a" * 64,
    )


def test_alignment_requires_exact_reviewed_plan() -> None:
    raster = _small_continuous_raster()
    target = raster.grid.model_copy(
        update={
            "grid_id": "target",
            "width": 4,
            "height": 4,
            "transform": (1.0, 0.0, 0.0, 0.0, -1.0, 4.0),
            "cell_area_m2": 1.0,
        }
    )
    plan = preview_grid_alignment(raster, target, ResamplingMethod.NEAREST, plan_id="plan_1")
    with pytest.raises(ValueError, match="acknowledged"):
        approve_grid_alignment(
            plan,
            approval_id="approval_1",
            reviewer="Ecologist",
            approved_at=datetime(2026, 8, 3, tzinfo=UTC),
            acknowledged_warnings=[],
            purpose="Test alignment",
        )
    approval = approve_grid_alignment(
        plan,
        approval_id="approval_1",
        reviewer="Ecologist",
        approved_at=datetime(2026, 8, 3, tzinfo=UTC),
        acknowledged_warnings=plan.warnings,
        purpose="Test alignment",
    )
    aligned = apply_approved_grid_alignment(raster, plan, approval, derived_asset_id="derived")
    assert aligned.grid.width == 4
    assert aligned.values[0][:2] == (2.0, 2.0)
    forged = approval.model_copy(update={"plan_sha256": "f" * 64})
    with pytest.raises(AlignmentApprovalRequired, match="does not match"):
        apply_approved_grid_alignment(raster, plan, forged, derived_asset_id="forged")


def test_identifier_and_mask_alignment_forbid_bilinear() -> None:
    raster = _small_continuous_raster()
    identifier = InMemoryRaster(
        asset_id="focal",
        grid=raster.grid.model_copy(update={"value_kind": RasterValueKind.IDENTIFIER, "nodata": 0}),
        values=((1, 1), (2, 2)),
        source_sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="nearest-neighbor"):
        preview_grid_alignment(
            identifier, identifier.grid, ResamplingMethod.BILINEAR, plan_id="bad"
        )


def test_grid_identity_proves_shared_cells_across_semantic_rasters() -> None:
    fixture = build_ridgeway_fixture()
    resistance = fixture.rasters["asset_resistance_h1_low_barrier"]
    focal = fixture.rasters["asset_focal_target_guild_a"]
    assert assert_same_grid(resistance, focal)
    shifted = focal.model_copy(
        update={
            "grid": focal.grid.model_copy(update={"transform": (25.0, 0.0, 1.0, 0.0, -25.0, 300.0)})
        }
    )
    with pytest.raises(GridMismatchError):
        assert_same_grid(resistance, shifted)


def test_portfolio_identity_and_absolute_replacement_are_auditable() -> None:
    fixture = build_ridgeway_fixture()
    sites = fixture.bundle.intervention_sites
    options = fixture.bundle.intervention_options
    selections = {site.site_id: f"{site.site_id}_no_action" for site in sites}
    selections[sites[0].site_id] = f"{sites[0].site_id}_reviewed_action"
    left = canonicalize_portfolio(sites, reversed(options), selections)
    right = canonicalize_portfolio(
        reversed(sites), options, dict(reversed(list(selections.items())))
    )
    assert left.portfolio_id == right.portfolio_id
    scenario = fixture.bundle.scenarios[0]
    selected_effects = scenario_effects_for_portfolio(left, options, scenario)
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    baseline = fixture.rasters[hypothesis.resistance_asset_id]
    result = apply_absolute_replacements(
        baseline, selected_effects, derived_asset_id="portfolio_resistance"
    )
    first = selected_effects[0].cells[0]
    assert result.values[first.row][first.column] == first.resistance
    assert baseline.values[first.row][first.column] != first.resistance
    assert result.grid == baseline.grid


def test_reviewed_mask_proves_every_replacement_cell_and_has_stable_identity() -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    site = fixture.bundle.intervention_sites[0]
    option = next(
        item
        for item in fixture.bundle.intervention_options
        if item.site_id == site.site_id and item.effects
    )
    effect = next(item for item in option.effects if item.scenario_id == scenario.scenario_id)
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    baseline = fixture.rasters[hypothesis.resistance_asset_id]
    proofs = prove_action_effect_footprints(
        baseline,
        [effect],
        fixture.bundle.assets,
        normalized_rasters=fixture.rasters,
    )
    assert proofs[0].covered_cells == tuple(
        sorted((cell.row, cell.column) for cell in effect.cells)
    )
    assert proofs[0].footprint_asset_sha256 == next(
        item.sha256 for item in fixture.bundle.assets if item.asset_id == site.footprint_asset_id
    )
    assert footprint_proof_set_sha256(proofs) == footprint_proof_set_sha256(reversed(proofs))


def test_reviewed_mask_fails_closed_when_any_replacement_cell_is_outside() -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    site = fixture.bundle.intervention_sites[0]
    option = next(
        item
        for item in fixture.bundle.intervention_options
        if item.site_id == site.site_id and item.effects
    )
    effect = next(item for item in option.effects if item.scenario_id == scenario.scenario_id)
    outside = effect.model_copy(
        update={"cells": [ActionCellReplacement(row=effect.cells[0].row, column=8, resistance=2.0)]}
    )
    outside = outside.model_copy(update={"sha256": action_effect_sha256(outside)})
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    with pytest.raises(FootprintCoverageError, match="outside reviewed footprint"):
        prove_action_effect_footprints(
            fixture.rasters[hypothesis.resistance_asset_id],
            [outside],
            fixture.bundle.assets,
            normalized_rasters=fixture.rasters,
        )


def test_generic_vector_json_cannot_masquerade_as_normalized_footprint(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    site = fixture.bundle.intervention_sites[0]
    option = next(
        item
        for item in fixture.bundle.intervention_options
        if item.site_id == site.site_id and item.effects
    )
    effect = next(item for item in option.effects if item.scenario_id == scenario.scenario_id)
    footprint = next(
        item for item in fixture.bundle.assets if item.asset_id == site.footprint_asset_id
    ).model_copy(
        update={
            "kind": AssetKind.VECTOR_CONTEXT,
            "relative_path": "footprint.json",
            "media_type": "application/json",
            "grid": None,
        }
    )
    path = tmp_path / "footprint.json"
    path.write_text('{"row": 1, "columns": [9, 10]}', encoding="utf-8")
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    with pytest.raises(FootprintCoverageError, match="single-layer GeoPackage"):
        prove_action_effect_footprints(
            fixture.rasters[hypothesis.resistance_asset_id],
            [effect],
            [footprint],
            resolve_path=lambda _asset: path,
        )


def test_single_layer_geopackage_requires_complete_cell_coverage(tmp_path: Path) -> None:
    geopandas = pytest.importorskip("geopandas")
    shapely_geometry = pytest.importorskip("shapely.geometry")
    pytest.importorskip("pyogrio")
    baseline = _small_continuous_raster()
    fixture = build_ridgeway_fixture()
    source_effect = next(
        effect for option in fixture.bundle.intervention_options for effect in option.effects
    )
    effect = source_effect.model_copy(
        update={
            "grid_id": baseline.grid.grid_id,
            "cells": [ActionCellReplacement(row=0, column=0, resistance=1.0)],
        }
    )
    effect = effect.model_copy(update={"sha256": action_effect_sha256(effect)})
    path = tmp_path / "footprint.gpkg"
    frame = geopandas.GeoDataFrame(
        geometry=[shapely_geometry.box(0, 2, 2, 4)],
        crs="EPSG:3857",
    )
    frame.to_file(path, layer="footprint", driver="GPKG", engine="pyogrio")
    source_asset = next(
        item for item in fixture.bundle.assets if item.asset_id == source_effect.footprint_asset_id
    )
    footprint = source_asset.model_copy(
        update={
            "kind": AssetKind.VECTOR_CONTEXT,
            "relative_path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size,
            "media_type": "application/geopackage+sqlite3",
            "grid": None,
        }
    )
    proofs = prove_action_effect_footprints(
        baseline,
        [effect],
        [footprint],
        resolve_path=lambda _asset: path,
    )
    assert proofs[0].semantics == "GEOPACKAGE_POLYGON_COVERS_COMPLETE_GRID_CELL_V1"

    partial_path = tmp_path / "partial.gpkg"
    geopandas.GeoDataFrame(
        geometry=[shapely_geometry.box(0, 2, 1.9, 4)],
        crs="EPSG:3857",
    ).to_file(partial_path, layer="footprint", driver="GPKG", engine="pyogrio")
    partial_asset = footprint.model_copy(
        update={
            "relative_path": partial_path.name,
            "sha256": hashlib.sha256(partial_path.read_bytes()).hexdigest(),
            "byte_size": partial_path.stat().st_size,
        }
    )
    with pytest.raises(FootprintCoverageError, match="not completely covered"):
        prove_action_effect_footprints(
            baseline,
            [effect],
            [partial_asset],
            resolve_path=lambda _asset: partial_path,
        )


def test_separate_overlapping_effects_are_rejected() -> None:
    fixture = build_ridgeway_fixture()
    scenario_id = fixture.bundle.scenarios[0].scenario_id
    modeled = [
        option
        for option in fixture.bundle.intervention_options
        if option.kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
    ]
    first = next(effect for effect in modeled[0].effects if effect.scenario_id == scenario_id)
    second_original = next(
        effect for effect in modeled[1].effects if effect.scenario_id == scenario_id
    )
    second = second_original.model_copy(
        update={
            "cells": [
                ActionCellReplacement(
                    row=first.cells[0].row,
                    column=first.cells[0].column,
                    resistance=2.0,
                )
            ]
        }
    )
    second = second.model_copy(update={"sha256": action_effect_sha256(second)})
    baseline = fixture.rasters["asset_resistance_h1_low_barrier"]
    with pytest.raises(InterventionConflictError, match="overlap"):
        apply_absolute_replacements(baseline, [first, second], derived_asset_id="bad")


def test_modeled_options_require_every_scenario_effect() -> None:
    fixture = build_ridgeway_fixture()
    option = next(
        item
        for item in fixture.bundle.intervention_options
        if item.kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
    )
    validate_option_scenario_coverage(option, fixture.bundle.scenarios)
    incomplete = option.model_copy(update={"effects": option.effects[:-1]})
    with pytest.raises(ValueError, match="cover every required scenario"):
        validate_option_scenario_coverage(incomplete, fixture.bundle.scenarios)
