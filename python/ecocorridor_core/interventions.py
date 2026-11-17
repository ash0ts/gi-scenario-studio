"""Reviewed intervention portfolios and immutable resistance replacements."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .contracts import (
    ActionEffectDraft,
    ActionEffectRaster,
    ConnectivityScenario,
    InterventionKind,
    InterventionOption,
    InterventionOptionDraft,
    InterventionSite,
    PortfolioSelection,
    canonical_sha256,
)
from .grid import InMemoryRaster, _is_nodata, grid_identity


class InterventionConflictError(ValueError):
    """Raised when separately reviewed actions overlap or references are incomplete."""


def canonicalize_action_effect(draft: ActionEffectDraft) -> ActionEffectRaster:
    """Return the worker-authored immutable action effect for a reviewed draft.

    Replacement-cell order carries no ecological meaning, so canonicalization
    sorts it before hashing.  This keeps evidence identities stable across UI
    ordering while retaining every reviewed value and citation verbatim.
    """

    payload = draft.model_dump(mode="python")
    payload["cells"] = sorted(
        payload["cells"], key=lambda item: (int(item["row"]), int(item["column"]))
    )
    digest_payload = {
        "effect_id": payload["effect_id"],
        "scenario_id": payload["scenario_id"],
        "grid_id": payload["grid_id"],
        "footprint_asset_id": payload["footprint_asset_id"],
        "cells": payload["cells"],
        "basis": draft.basis.model_dump(mode="json"),
    }
    return ActionEffectRaster.model_validate(
        {**digest_payload, "sha256": canonical_sha256(digest_payload)}
    )


def canonicalize_intervention_option(draft: InterventionOptionDraft) -> InterventionOption:
    """Canonicalize all action effects without accepting a renderer hash."""

    payload = draft.model_dump(mode="python", exclude={"effects"})
    return InterventionOption.model_validate(
        {
            **payload,
            "effects": [canonicalize_action_effect(effect) for effect in draft.effects],
        }
    )


def intervention_preview_sha256(
    site: InterventionSite,
    options: Iterable[InterventionOption],
    *,
    revision_id: str,
) -> str:
    """Bind a preview token to the exact revision and canonical catalog."""

    canonical_options = sorted(options, key=lambda item: item.option_id)
    return canonical_sha256(
        {
            "revision_id": revision_id,
            "site": site.model_dump(mode="json"),
            "options": [item.model_dump(mode="json") for item in canonical_options],
            "semantics": "reviewed_absolute_resistance_replacements_v1",
        }
    )


def action_effect_content(effect: ActionEffectRaster) -> dict[str, Any]:
    return {
        "effect_id": effect.effect_id,
        "scenario_id": effect.scenario_id,
        "grid_id": effect.grid_id,
        "footprint_asset_id": effect.footprint_asset_id,
        "cells": [item.model_dump(mode="json") for item in effect.cells],
        "basis": effect.basis.model_dump(mode="json"),
    }


def action_effect_sha256(effect: ActionEffectRaster) -> str:
    return canonical_sha256(action_effect_content(effect))


def canonicalize_portfolio(
    sites: Iterable[InterventionSite],
    options: Iterable[InterventionOption],
    selections: dict[str, str],
) -> PortfolioSelection:
    """Create an evidence identity independent from search feature encoding."""

    sites_by_id = {item.site_id: item for item in sites}
    options_by_id = {item.option_id: item for item in options}
    if not sites_by_id:
        raise ValueError("a portfolio requires at least one intervention site")
    if set(selections) != set(sites_by_id):
        missing = sorted(set(sites_by_id) - set(selections))
        unexpected = sorted(set(selections) - set(sites_by_id))
        raise ValueError(
            f"portfolio must select exactly one option per site; missing={missing}, extra={unexpected}"
        )
    ordered_options: list[str] = []
    contains_survey_first = False
    normalized: dict[str, str] = {}
    for site_id in sorted(sites_by_id):
        option_id = selections[site_id]
        site = sites_by_id[site_id]
        option = options_by_id.get(option_id)
        if option is None or option_id not in site.option_ids or option.site_id != site_id:
            raise ValueError(f"{option_id!r} is not a valid option for {site_id!r}")
        ordered_options.append(option_id)
        normalized[site_id] = option_id
        contains_survey_first |= option.kind == InterventionKind.SURVEY_FIRST
    portfolio_id = "portfolio_" + canonical_sha256(ordered_options)[:20]
    return PortfolioSelection(
        portfolio_id=portfolio_id,
        selections=normalized,
        canonical_option_ids=tuple(ordered_options),
        contains_survey_first=contains_survey_first,
    )


def validate_option_scenario_coverage(
    option: InterventionOption,
    required_scenarios: Iterable[ConnectivityScenario],
) -> None:
    required_ids = {item.scenario_id for item in required_scenarios}
    effect_ids = {item.scenario_id for item in option.effects}
    if option.kind in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}:
        if effect_ids:
            raise ValueError(f"{option.kind.value} cannot include modeled effects")
        return
    if effect_ids != required_ids:
        missing = sorted(required_ids - effect_ids)
        extra = sorted(effect_ids - required_ids)
        raise ValueError(
            f"modeled option {option.option_id} must cover every required scenario; "
            f"missing={missing}, extra={extra}"
        )


def scenario_effects_for_portfolio(
    portfolio: PortfolioSelection,
    options: Iterable[InterventionOption],
    scenario: ConnectivityScenario,
) -> list[ActionEffectRaster]:
    options_by_id = {item.option_id: item for item in options}
    effects: list[ActionEffectRaster] = []
    occupied: dict[tuple[int, int], str] = {}
    for option_id in portfolio.canonical_option_ids:
        option = options_by_id.get(option_id)
        if option is None:
            raise ValueError(f"portfolio references missing option {option_id}")
        matches = [item for item in option.effects if item.scenario_id == scenario.scenario_id]
        if (
            option.kind not in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}
            and len(matches) != 1
        ):
            raise ValueError(
                f"option {option.option_id} lacks exactly one reviewed effect for "
                f"scenario {scenario.scenario_id}"
            )
        for effect in matches:
            for cell in effect.cells:
                coordinate = (cell.row, cell.column)
                if coordinate in occupied:
                    raise InterventionConflictError(
                        f"separate options {occupied[coordinate]} and {option.option_id} overlap "
                        f"at cell {coordinate}; author a single reviewed composite option"
                    )
                occupied[coordinate] = option.option_id
            effects.append(effect)
    return effects


def apply_absolute_replacements(
    baseline: InMemoryRaster,
    effects: Iterable[ActionEffectRaster],
    *,
    derived_asset_id: str,
) -> InMemoryRaster:
    """Apply reviewed absolute resistance values without interpolation or inference."""

    effects = list(effects)
    values = [list(row) for row in baseline.values]
    occupied: dict[tuple[int, int], str] = {}
    for effect in effects:
        if effect.grid_id != baseline.grid.grid_id:
            raise InterventionConflictError(
                f"effect {effect.effect_id} targets grid {effect.grid_id}, not {baseline.grid.grid_id}"
            )
        if action_effect_sha256(effect) != effect.sha256:
            raise InterventionConflictError(
                f"effect {effect.effect_id} content hash does not match"
            )
        for replacement in effect.cells:
            coordinate = (replacement.row, replacement.column)
            if coordinate in occupied:
                raise InterventionConflictError(
                    f"effects {occupied[coordinate]} and {effect.effect_id} overlap at {coordinate}"
                )
            if replacement.row >= baseline.grid.height or replacement.column >= baseline.grid.width:
                raise InterventionConflictError(
                    f"effect {effect.effect_id} contains out-of-grid cell {coordinate}"
                )
            existing = values[replacement.row][replacement.column]
            if _is_nodata(existing, baseline.grid.nodata):
                raise InterventionConflictError(
                    f"effect {effect.effect_id} cannot change NODATA at cell {coordinate}"
                )
            values[replacement.row][replacement.column] = replacement.resistance
            occupied[coordinate] = effect.effect_id
    derived_values = tuple(tuple(row) for row in values)
    # Defensive proof that an action did not alter topology or NODATA semantics.
    for row_index, row in enumerate(derived_values):
        for column_index, value in enumerate(row):
            was_nodata = _is_nodata(baseline.values[row_index][column_index], baseline.grid.nodata)
            if was_nodata != _is_nodata(value, baseline.grid.nodata):
                raise InterventionConflictError("action changed NODATA topology")
    result = InMemoryRaster(
        asset_id=derived_asset_id,
        grid=baseline.grid,
        values=derived_values,
        source_sha256=canonical_sha256(
            {
                "baseline": baseline.sha256,
                "effects": sorted(item.sha256 for item in effects),
                "semantics": "absolute_resistance_replacement",
            }
        ),
    )
    if grid_identity(result.grid) != grid_identity(baseline.grid):
        raise InterventionConflictError("action changed grid topology")
    return result


def portfolio_cost(
    portfolio: PortfolioSelection,
    options: Iterable[InterventionOption],
) -> tuple[float, str, str]:
    options_by_id = {item.option_id: item for item in options}
    selected = [options_by_id[item] for item in portfolio.canonical_option_ids]
    currencies = {item.currency for item in selected}
    vintages = {item.cost_vintage for item in selected}
    if len(currencies) != 1 or len(vintages) != 1:
        raise ValueError("portfolio costs require one currency and one cost vintage")
    return sum(item.cost for item in selected), currencies.pop(), vintages.pop()


def unresolved_portfolio_concerns(
    portfolio: PortfolioSelection,
    options: Iterable[InterventionOption],
) -> list[str]:
    options_by_id = {item.option_id: item for item in options}
    concerns = {
        concern
        for option_id in portfolio.canonical_option_ids
        for concern in options_by_id[option_id].unresolved_concerns
    }
    if portfolio.contains_survey_first:
        concerns.add("One or more sites require survey before a modeled action can be compared.")
    return sorted(concerns)
