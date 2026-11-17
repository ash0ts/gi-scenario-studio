"""Canonical semantic raster diffs for direct connectivity evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import ActionEffectRaster
from .grid import InMemoryRaster
from .interventions import action_effect_sha256, apply_absolute_replacements


class SemanticRasterDiffError(ValueError):
    pass


def semantic_raster_diff_payload(
    *,
    project_id: str,
    revision_id: str,
    model_identity_sha256: str,
    portfolio_id: str,
    scenario_id: str,
    baseline: InMemoryRaster,
    effects: list[ActionEffectRaster],
    prepared_resistance_sha256: str,
) -> dict[str, object]:
    changes: list[dict[str, object]] = []
    occupied: set[tuple[int, int]] = set()
    for effect in sorted(effects, key=lambda item: item.effect_id):
        if action_effect_sha256(effect) != effect.sha256:
            raise SemanticRasterDiffError(f"action effect content hash differs: {effect.effect_id}")
        for cell in sorted(effect.cells, key=lambda item: (item.row, item.column)):
            coordinate = (cell.row, cell.column)
            if coordinate in occupied:
                raise SemanticRasterDiffError("semantic raster diff contains overlapping effects")
            occupied.add(coordinate)
            before = baseline.values[cell.row][cell.column]
            if before is None:
                raise SemanticRasterDiffError(f"action diff references NODATA: {effect.effect_id}")
            changes.append(
                {
                    "effect_id": effect.effect_id,
                    "effect_sha256": effect.sha256,
                    "footprint_asset_id": effect.footprint_asset_id,
                    "row": cell.row,
                    "column": cell.column,
                    "before_resistance": before,
                    "after_resistance": cell.resistance,
                }
            )
    return {
        "schema_version": "ecocorridor-semantic-raster-diff/v1.0",
        "project_id": project_id,
        "revision_id": revision_id,
        "model_identity_sha256": model_identity_sha256,
        "portfolio_id": portfolio_id,
        "scenario_id": scenario_id,
        "baseline_asset_id": baseline.asset_id,
        "baseline_reviewed_content_sha256": baseline.source_sha256,
        "prepared_resistance_sha256": prepared_resistance_sha256,
        "changes": changes,
    }


def canonical_semantic_raster_diff_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def semantic_raster_diff_sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_semantic_raster_diff_bytes(value)).hexdigest()


def verify_semantic_raster_diff(
    payload: dict[str, Any],
    *,
    baseline: InMemoryRaster,
    effects: list[ActionEffectRaster],
    prepared: InMemoryRaster,
    expected_project_id: str,
    expected_revision_id: str,
    expected_model_identity_sha256: str,
    expected_portfolio_id: str,
    expected_scenario_id: str,
    prepared_resistance_sha256: str,
) -> str:
    expected = semantic_raster_diff_payload(
        project_id=expected_project_id,
        revision_id=expected_revision_id,
        model_identity_sha256=expected_model_identity_sha256,
        portfolio_id=expected_portfolio_id,
        scenario_id=expected_scenario_id,
        baseline=baseline,
        effects=effects,
        prepared_resistance_sha256=prepared_resistance_sha256,
    )
    if payload != expected:
        raise SemanticRasterDiffError("semantic raster diff differs from reviewed model effects")
    reconstructed = apply_absolute_replacements(
        baseline,
        effects,
        derived_asset_id=prepared.asset_id,
    )
    if reconstructed.grid != prepared.grid or reconstructed.values != prepared.values:
        raise SemanticRasterDiffError(
            "semantic raster diff does not reconstruct prepared resistance"
        )
    return semantic_raster_diff_sha256(payload)


__all__ = [
    "SemanticRasterDiffError",
    "canonical_semantic_raster_diff_bytes",
    "semantic_raster_diff_payload",
    "semantic_raster_diff_sha256",
    "verify_semantic_raster_diff",
]
