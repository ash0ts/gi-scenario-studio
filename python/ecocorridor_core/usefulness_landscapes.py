"""Deterministic CC0 inputs for the EcoCorridor usefulness evidence program.

The five landscapes in this module are authored test constructs, not ecological
models of real places.  The module creates self-authored synthetic assumptions,
finite portfolio universes, strict Circuitscape request documents, and
checksum-bound run plans.  It intentionally contains no solver results.  Any
condition that depends on resistance matrices or current rasters remains
``PENDING_DIRECT_CIRCUITSCAPE`` until the pinned engine produces and the normal
evidence pipeline validates those artifacts.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .claim_safety import assert_strict_ecological_claim
from .contracts import (
    ContractModel,
    NeighborRule,
    RasterGrid,
    RasterValueKind,
    ResistanceAverage,
    canonical_sha256,
)
from .fixture_pipeline import render_esri_ascii
from .grid import InMemoryRaster, assert_same_grid, grid_identity

USEFULNESS_FIXTURE_VERSION = "1.0.0"
USEFULNESS_LICENSE = "CC0-1.0"
USEFULNESS_NOTICE = (
    "These landscapes are self-authored synthetic test constructs. They are not real places, "
    "species assessments, observed animal movement, crossing-use studies, collision analyses, "
    "population studies, regulatory findings, or designs."
)
USEFULNESS_AUTHORSHIP_DATE = "2026-08-07"
NODATA = -9999.0
WIDTH = 24
HEIGHT = 18

LandscapeFamily = Literal[
    "linear_bottleneck",
    "braided_dual_barrier",
    "fragmented_periurban_mosaic",
    "boundary_sensitive",
    "asymmetric_multicore",
]
CaseConditionKind = Literal[
    "CLEAN_PASS_WITHOUT_REVIEW_MASK_FLAG",
    "ROAD_MASK_ADVISORY",
    "OVER_BUDGET",
    "HYPOTHESIS_SENSITIVE_RANKING",
    "INCOMPLETE_SAFEGUARD_ENSEMBLE",
    "QA_REJECTION",
    "STALE_REVISION_OR_HASH",
    "UNAUTHORIZED_ACTION_EFFECT",
    "BENIGN_EVIDENCE_NO_WARNING",
]
TruthBasis = Literal["DIRECT_CIRCUITSCAPE_REQUIRED", "DETERMINISTIC_INPUT_POLICY"]
TruthStatus = Literal[
    "PENDING_DIRECT_CIRCUITSCAPE",
    "READY_FOR_POLICY_ASSERTION",
]
ExpectedGate = Literal[
    "PENDING",
    "BLOCK_BEFORE_COMPARISON",
    "BLOCK_BEFORE_EXECUTION",
    "NO_WARNING_EXPECTED",
    "KNOWN_COST_CONDITION",
]

ALL_FAMILIES: tuple[LandscapeFamily, ...] = (
    "linear_bottleneck",
    "braided_dual_barrier",
    "fragmented_periurban_mosaic",
    "boundary_sensitive",
    "asymmetric_multicore",
)
ALL_CASE_CONDITIONS: frozenset[str] = frozenset(
    {
        "CLEAN_PASS_WITHOUT_REVIEW_MASK_FLAG",
        "ROAD_MASK_ADVISORY",
        "OVER_BUDGET",
        "HYPOTHESIS_SENSITIVE_RANKING",
        "INCOMPLETE_SAFEGUARD_ENSEMBLE",
        "QA_REJECTION",
        "STALE_REVISION_OR_HASH",
        "UNAUTHORIZED_ACTION_EFFECT",
        "BENIGN_EVIDENCE_NO_WARNING",
    }
)
DIRECT_RESULT_CONDITIONS: frozenset[str] = frozenset(
    {
        "CLEAN_PASS_WITHOUT_REVIEW_MASK_FLAG",
        "ROAD_MASK_ADVISORY",
        "HYPOTHESIS_SENSITIVE_RANKING",
    }
)


class SyntheticAssumption(ContractModel):
    assumption_id: str
    category: Literal[
        "LANDSCAPE_STRUCTURE",
        "FOCAL_GEOMETRY",
        "RESISTANCE_HYPOTHESIS",
        "ACTION_EFFECT",
        "REVIEW_MASK",
    ]
    statement: str
    source: Literal["SELF_AUTHORED_CC0_SYNTHETIC"] = "SELF_AUTHORED_CC0_SYNTHETIC"
    authorship: Literal["SELF_AUTHORED_CC0_SYNTHETIC"] = "SELF_AUTHORED_CC0_SYNTHETIC"
    authored_on: Literal[USEFULNESS_AUTHORSHIP_DATE] = USEFULNESS_AUTHORSHIP_DATE
    adjudication_status: Literal["PENDING_INDEPENDENT_ADJUDICATION"] = (
        "PENDING_INDEPENDENT_ADJUDICATION"
    )
    confidence: Literal["ASSUMED"] = "ASSUMED"
    limitation: str = (
        "A synthetic benchmark assumption has no authority outside this test construct."
    )


class PlannedFocalPair(ContractModel):
    pair_id: str
    from_focal_id: int = Field(gt=0)
    to_focal_id: int = Field(gt=0)

    @model_validator(mode="after")
    def canonical_pair(self) -> PlannedFocalPair:
        if self.from_focal_id >= self.to_focal_id:
            raise ValueError("planned focal pairs must use ascending distinct IDs")
        return self


class UsefulnessScenario(ContractModel):
    scenario_id: str
    title: str
    guild_alias: str
    life_stage: str
    resistance_hypothesis: str
    future_condition: str
    maturity_horizon: str
    resistance_asset_id: str
    focal_asset_id: str
    pairs: tuple[PlannedFocalPair, ...]
    safeguard: bool
    neighbor_rule: NeighborRule = NeighborRule.EIGHT
    resistance_average: ResistanceAverage = ResistanceAverage.ARITHMETIC

    @model_validator(mode="after")
    def unique_pairs(self) -> UsefulnessScenario:
        pair_ids = [pair.pair_id for pair in self.pairs]
        endpoints = [(pair.from_focal_id, pair.to_focal_id) for pair in self.pairs]
        if not pair_ids or len(pair_ids) != len(set(pair_ids)):
            raise ValueError("scenario pair IDs must be present and unique")
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("scenario focal endpoints must be unique")
        return self


class UsefulnessActionSite(ContractModel):
    site_id: str
    cells: tuple[tuple[int, int], ...]
    cost_usd: int = Field(gt=0)
    absolute_replacement_by_scenario: dict[str, float]
    evidence_basis: str = (
        "Self-authored absolute replacement values for this synthetic benchmark only; "
        "independent adjudication is pending."
    )

    @model_validator(mode="after")
    def valid_cells_and_values(self) -> UsefulnessActionSite:
        if not self.cells or len(self.cells) != len(set(self.cells)):
            raise ValueError("action cells must be non-empty and unique")
        if any(row < 0 or column < 0 for row, column in self.cells):
            raise ValueError("action cells cannot use negative indices")
        if not self.absolute_replacement_by_scenario or any(
            value <= 0 for value in self.absolute_replacement_by_scenario.values()
        ):
            raise ValueError("every authored action replacement must be strictly positive")
        return self


class UsefulnessPortfolio(ContractModel):
    portfolio_id: str
    selected_site_ids: tuple[str, ...]
    cost_usd: int = Field(ge=0)


class CaseTruthCondition(ContractModel):
    condition_id: str
    kind: CaseConditionKind
    truth_basis: TruthBasis
    truth_status: TruthStatus
    expected_gate: ExpectedGate
    subject_id: str
    assertion: str
    limitation: str = (
        "Only the named deterministic gate is pre-authored; numerical connectivity outcomes "
        "must come from retained direct Circuitscape artifacts."
    )

    @model_validator(mode="after")
    def direct_truth_remains_pending(self) -> CaseTruthCondition:
        if self.kind in DIRECT_RESULT_CONDITIONS:
            if self.truth_basis != "DIRECT_CIRCUITSCAPE_REQUIRED":
                raise ValueError("outcome-dependent case truth must require direct Circuitscape")
            if self.truth_status != "PENDING_DIRECT_CIRCUITSCAPE":
                raise ValueError("outcome-dependent case truth cannot be pre-authored")
            if self.expected_gate != "PENDING":
                raise ValueError("outcome-dependent case truth must remain pending")
        elif self.truth_basis != "DETERMINISTIC_INPUT_POLICY":
            raise ValueError("input-policy case truth must have a deterministic basis")
        return self


class UsefulnessLandscape(ContractModel):
    family: LandscapeFamily
    title: str
    description: str
    notice: Literal[USEFULNESS_NOTICE] = USEFULNESS_NOTICE
    license: Literal["CC0-1.0"] = USEFULNESS_LICENSE
    grid_id: str
    rasters: dict[str, InMemoryRaster]
    scenarios: tuple[UsefulnessScenario, ...]
    action_sites: tuple[UsefulnessActionSite, ...]
    portfolios: tuple[UsefulnessPortfolio, ...]
    assumptions: tuple[SyntheticAssumption, ...]
    case_truths: tuple[CaseTruthCondition, ...]
    budget_usd: int = Field(gt=0)
    execution_state: Literal["DIRECT_EVIDENCE_PENDING"] = "DIRECT_EVIDENCE_PENDING"
    reportable_outcomes: None = None

    @model_validator(mode="after")
    def validate_complete_input_contract(self) -> UsefulnessLandscape:
        if len(self.scenarios) != 4:
            raise ValueError("each usefulness landscape requires exactly four scenarios")
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("landscape scenario IDs must be unique")
        if not any(scenario.safeguard for scenario in self.scenarios):
            raise ValueError("landscape ensemble requires at least one safeguard scenario")
        if not any(not scenario.safeguard for scenario in self.scenarios):
            raise ValueError("landscape ensemble requires at least one target scenario")

        required_assets = {
            asset_id
            for scenario in self.scenarios
            for asset_id in (scenario.resistance_asset_id, scenario.focal_asset_id)
        }
        required_assets.add(f"{self.family}_review_mask")
        if not required_assets.issubset(self.rasters):
            raise ValueError("scenario or review-mask raster is absent")
        rasters = tuple(self.rasters.values())
        if any(raster.grid.grid_id != self.grid_id for raster in rasters):
            raise ValueError("all landscape rasters must bind the same self-authored grid ID")
        assert_same_grid(*rasters)
        topology = _nodata_topology(rasters[0])
        if any(_nodata_topology(raster) != topology for raster in rasters[1:]):
            raise ValueError("all landscape rasters must preserve identical NODATA topology")

        scenario_by_id = {scenario.scenario_id: scenario for scenario in self.scenarios}
        for scenario in self.scenarios:
            resistance = self.rasters[scenario.resistance_asset_id]
            focal = self.rasters[scenario.focal_asset_id]
            if resistance.grid.value_kind != RasterValueKind.CONTINUOUS:
                raise ValueError("scenario resistance input must be continuous")
            if focal.grid.value_kind != RasterValueKind.IDENTIFIER:
                raise ValueError("scenario focal input must use identifiers")
            focal_ids = {
                int(value)
                for row in focal.values
                for value in row
                if value not in {None, focal.grid.nodata, 0}
            }
            for pair in scenario.pairs:
                if pair.from_focal_id not in focal_ids or pair.to_focal_id not in focal_ids:
                    raise ValueError("scenario pair references an absent focal ID")

        all_action_cells: set[tuple[int, int]] = set()
        site_ids: list[str] = []
        for site in self.action_sites:
            site_ids.append(site.site_id)
            if set(site.absolute_replacement_by_scenario) != set(scenario_by_id):
                raise ValueError("each action requires an absolute effect for every scenario")
            if all_action_cells.intersection(site.cells):
                raise ValueError("separate action sites may not overlap")
            all_action_cells.update(site.cells)
            for row, column in site.cells:
                if row >= HEIGHT or column >= WIDTH:
                    raise ValueError("action cell lies outside the self-authored test grid")
                for scenario in self.scenarios:
                    resistance = self.rasters[scenario.resistance_asset_id]
                    if _is_nodata(resistance.values[row][column], resistance.grid.nodata):
                        raise ValueError("action effects cannot alter NODATA topology")

        if len(site_ids) != len(set(site_ids)):
            raise ValueError("action site IDs must be unique")
        expected_portfolios = 2 ** len(site_ids)
        if len(self.portfolios) != expected_portfolios:
            raise ValueError("finite binary portfolio universe must be exhaustive")
        selections = {portfolio.selected_site_ids for portfolio in self.portfolios}
        expected_selections = {
            tuple(site_ids[index] for index, selected in enumerate(bits) if selected)
            for bits in itertools.product((0, 1), repeat=len(site_ids))
        }
        if selections != expected_selections:
            raise ValueError("portfolio selections are not the complete canonical universe")
        costs = {site.site_id: site.cost_usd for site in self.action_sites}
        for portfolio in self.portfolios:
            if portfolio.cost_usd != sum(costs[site_id] for site_id in portfolio.selected_site_ids):
                raise ValueError("portfolio cost differs from its self-authored test site costs")
        return self

    @property
    def direct_job_count(self) -> int:
        return len(self.portfolios) * len(self.scenarios)


class DirectRunPlanRow(ContractModel):
    direct_run_id: str
    family: LandscapeFamily
    portfolio_id: str
    scenario_id: str
    safeguard: bool
    pair_ids: tuple[str, ...]
    request_relative_path: str
    result_relative_path: str
    request_sha256: str
    prepared_resistance_sha256: str
    focal_raster_sha256: str
    review_mask_sha256: str
    execution_state: Literal["PENDING_DIRECT_CIRCUITSCAPE"] = "PENDING_DIRECT_CIRCUITSCAPE"
    reportable_values: None = None


class UsefulnessCatalog(ContractModel):
    schema_version: Literal["ecocorridor-usefulness-inputs/v1.0"] = (
        "ecocorridor-usefulness-inputs/v1.0"
    )
    fixture_version: Literal[USEFULNESS_FIXTURE_VERSION] = USEFULNESS_FIXTURE_VERSION
    license: Literal["CC0-1.0"] = USEFULNESS_LICENSE
    notice: Literal[USEFULNESS_NOTICE] = USEFULNESS_NOTICE
    landscapes: tuple[UsefulnessLandscape, ...]
    evidence_state: Literal["DIRECT_EVIDENCE_PENDING"] = "DIRECT_EVIDENCE_PENDING"
    release_eligible: Literal[False] = False
    release_blocker: str = (
        "No usefulness outcome exists until the pinned Circuitscape engine completes every "
        "planned direct job and the retained artifacts pass QA."
    )

    @model_validator(mode="after")
    def validate_program_coverage(self) -> UsefulnessCatalog:
        families = tuple(landscape.family for landscape in self.landscapes)
        if families != ALL_FAMILIES:
            raise ValueError("usefulness catalog must contain the five canonical families in order")
        kinds = {truth.kind for landscape in self.landscapes for truth in landscape.case_truths}
        if kinds != ALL_CASE_CONDITIONS:
            raise ValueError("usefulness catalog does not cover every required case condition")
        for text in _narrative_strings(self):
            assert_strict_ecological_claim(text)
        return self

    @property
    def direct_job_count(self) -> int:
        return sum(landscape.direct_job_count for landscape in self.landscapes)


class MaterializedUsefulnessFile(ContractModel):
    relative_path: str
    sha256: str
    byte_size: int = Field(ge=0)
    role: str


class UsefulnessMaterializationManifest(ContractModel):
    schema_version: Literal["ecocorridor-usefulness-materialization/v1.0"] = (
        "ecocorridor-usefulness-materialization/v1.0"
    )
    fixture_version: Literal[USEFULNESS_FIXTURE_VERSION] = USEFULNESS_FIXTURE_VERSION
    program_sha256: str
    materializer_sha256: str
    run_plan_sha256: str
    landscape_count: Literal[5] = 5
    scenario_count: Literal[20] = 20
    portfolio_count: Literal[40] = 40
    direct_job_count: Literal[160] = 160
    files: tuple[MaterializedUsefulnessFile, ...]
    files_sha256: str
    execution_state: Literal["INPUTS_MATERIALIZED"] = "INPUTS_MATERIALIZED"
    direct_evidence_state: Literal["PENDING_DIRECT_CIRCUITSCAPE"] = (
        "PENDING_DIRECT_CIRCUITSCAPE"
    )
    result_file_count: Literal[0] = 0
    release_eligible: Literal[False] = False
    release_blocker: str = (
        "Materialized source inputs and requests are not direct Circuitscape evidence."
    )


def _is_nodata(value: float | None, nodata: float | None) -> bool:
    return value is None or value == nodata


def _nodata_topology(raster: InMemoryRaster) -> str:
    return canonical_sha256(
        [[_is_nodata(value, raster.grid.nodata) for value in row] for row in raster.values]
    )


def _grid(family: LandscapeFamily, value_kind: RasterValueKind) -> RasterGrid:
    return RasterGrid(
        grid_id=f"{family}_grid_v1",
        crs='LOCAL_CS["Synthetic metre grid; no geographic location"]',
        width=WIDTH,
        height=HEIGHT,
        transform=(100.0, 0.0, 0.0, 0.0, -100.0, 1800.0),
        nodata=NODATA,
        value_kind=value_kind,
        cell_area_m2=10_000.0,
    )


def _valid_domain(family: LandscapeFamily, row: int, column: int) -> bool:
    if row in {0, HEIGHT - 1} or column in {0, WIDTH - 1}:
        return False
    if family == "boundary_sensitive":
        return not (
            (1 <= row <= 3 and 9 <= column <= 10)
            or (14 <= row <= 16 and 14 <= column <= 15)
        )
    if family == "asymmetric_multicore":
        return not (1 <= row <= 2 and 20 <= column <= 22)
    return True


def _base_resistance(family: LandscapeFamily, row: int, column: int) -> float:
    if family == "linear_bottleneck":
        value = 4.0 + 0.08 * abs(row - 8.5)
        if column in {11, 12}:
            value = 7.0 if row in {8, 9} else 80.0
        return value
    if family == "braided_dual_barrier":
        value = 4.5 + 0.06 * row + 0.03 * column
        if column == 8:
            value = 7.0 if row in {4, 5} else 55.0
        if column == 15:
            value = 7.5 if row in {11, 12} else 62.0
        if abs(row - (column * 0.55 + 1.5)) < 1.2:
            value = min(value, 3.8)
        return value
    if family == "fragmented_periurban_mosaic":
        value = 4.2 + ((row * 7 + column * 11) % 5) * 0.35
        if row in {6, 12} or column in {9, 17}:
            value = 34.0
        if (2 <= row <= 5 and 12 <= column <= 16) or (
            9 <= row <= 11 and 3 <= column <= 7
        ):
            value = 22.0
        return value
    if family == "boundary_sensitive":
        value = 5.0 + 0.05 * abs(row - 8.5)
        if 10 <= column <= 13:
            value = 46.0
        if row in {2, 15}:
            value = min(value, 4.0)
        return value
    value = 3.5 + 0.16 * column + 0.09 * row
    if 8 <= column <= 14 and row <= column - 1:
        value += 24.0
    if 7 <= row <= 10 and 10 <= column <= 12:
        value = 6.0
    return value


def _resistance_values(
    family: LandscapeFamily, variant: Literal["current", "future", "alternate", "safeguard"]
) -> tuple[tuple[float, ...], ...]:
    rows: list[tuple[float, ...]] = []
    for row in range(HEIGHT):
        values: list[float] = []
        for column in range(WIDTH):
            if not _valid_domain(family, row, column):
                values.append(NODATA)
                continue
            value = _base_resistance(family, row, column)
            if variant == "future" and column >= WIDTH // 2:
                value *= 1.18
            elif variant == "alternate":
                value *= 0.82 + 0.32 * row / (HEIGHT - 1)
            elif variant == "safeguard":
                value *= 1.14 - 0.24 * column / (WIDTH - 1)
            values.append(round(max(value, 1.0), 6))
        rows.append(tuple(values))
    return tuple(rows)


def _paint_core(
    values: list[list[float | int]], focal_id: int, top: int, left: int
) -> None:
    for row in range(top, top + 2):
        for column in range(left, left + 2):
            if values[row][column] == NODATA:
                raise ValueError("synthetic focal core intersects NODATA")
            values[row][column] = focal_id


def _focal_values(
    family: LandscapeFamily, focal_kind: Literal["target_a", "target_b", "safeguard"]
) -> tuple[tuple[float | int, ...], ...]:
    values: list[list[float | int]] = [
        [0 if _valid_domain(family, row, column) else NODATA for column in range(WIDTH)]
        for row in range(HEIGHT)
    ]
    if focal_kind == "target_a" and family == "asymmetric_multicore":
        for focal_id, (top, left) in enumerate(((3, 2), (3, 19), (12, 3), (13, 19)), start=1):
            _paint_core(values, focal_id, top, left)
    elif focal_kind == "target_a":
        _paint_core(values, 1, 7, 2)
        _paint_core(values, 2, 7, 20)
    elif focal_kind == "target_b":
        _paint_core(values, 1, 2, 5)
        _paint_core(values, 2, 14, 18)
    else:
        _paint_core(values, 1, 3, 3)
        _paint_core(values, 2, 12, 20)
    return tuple(tuple(row) for row in values)


def _review_mask_values(family: LandscapeFamily) -> tuple[tuple[float | int, ...], ...]:
    values: list[tuple[float | int, ...]] = []
    for row in range(HEIGHT):
        current: list[float | int] = []
        for column in range(WIDTH):
            if not _valid_domain(family, row, column):
                current.append(NODATA)
            else:
                selected = (
                    column in {8, 9, 10, 11, 12, 13, 14, 15}
                    if family in {"braided_dual_barrier", "fragmented_periurban_mosaic"}
                    else column in {10, 11, 12, 13}
                )
                current.append(1 if selected else 0)
        values.append(tuple(current))
    return tuple(values)


def _raster(
    family: LandscapeFamily,
    asset_id: str,
    value_kind: RasterValueKind,
    values: tuple[tuple[float | int | None, ...], ...],
) -> InMemoryRaster:
    source_payload = {
        "fixture_version": USEFULNESS_FIXTURE_VERSION,
        "family": family,
        "asset_id": asset_id,
        "values": values,
        "source": "SELF_AUTHORED_CC0_SYNTHETIC",
    }
    return InMemoryRaster(
        asset_id=asset_id,
        grid=_grid(family, value_kind),
        values=values,
        source_sha256=canonical_sha256(source_payload),
    )


def _scenario_pairs(
    family: LandscapeFamily, focal_kind: str
) -> tuple[PlannedFocalPair, ...]:
    if focal_kind == "target_a" and family == "asymmetric_multicore":
        return (
            PlannedFocalPair(pair_id=f"{family}_pair_1_2", from_focal_id=1, to_focal_id=2),
            PlannedFocalPair(pair_id=f"{family}_pair_1_3", from_focal_id=1, to_focal_id=3),
            PlannedFocalPair(pair_id=f"{family}_pair_3_4", from_focal_id=3, to_focal_id=4),
        )
    suffix = {"target_a": "target_a", "target_b": "target_b", "safeguard": "safeguard"}[
        focal_kind
    ]
    return (
        PlannedFocalPair(
            pair_id=f"{family}_pair_{suffix}", from_focal_id=1, to_focal_id=2
        ),
    )


def _scenarios(family: LandscapeFamily) -> tuple[UsefulnessScenario, ...]:
    definitions = (
        ("target_current", "target_a", "current", "target_guild_a", False, "current", "present"),
        ("target_future", "target_a", "future", "target_guild_a", False, "future", "mature"),
        (
            "target_alternate",
            "target_b",
            "alternate",
            "target_guild_b",
            False,
            "current",
            "present",
        ),
        (
            "safeguard_current",
            "safeguard",
            "safeguard",
            "safeguard_guild",
            True,
            "current",
            "present",
        ),
    )
    return tuple(
        UsefulnessScenario(
            scenario_id=f"{family}_{suffix}",
            title=f"{family.replace('_', ' ').title()} {suffix.replace('_', ' ')} hypothesis",
            guild_alias=guild_alias,
            life_stage="anonymous synthetic stage",
            resistance_hypothesis=f"{family}_{variant}_resistance_assumption",
            future_condition=future_condition,
            maturity_horizon=maturity,
            resistance_asset_id=f"{family}_resistance_{variant}",
            focal_asset_id=f"{family}_focal_{focal_kind}",
            pairs=_scenario_pairs(family, focal_kind),
            safeguard=safeguard,
        )
        for suffix, focal_kind, variant, guild_alias, safeguard, future_condition, maturity in definitions
    )


def _action_cells(family: LandscapeFamily) -> tuple[tuple[tuple[int, int], ...], ...]:
    return {
        "linear_bottleneck": (((7, 11), (8, 11)), ((9, 12), (10, 12)), ((8, 10), (9, 10))),
        "braided_dual_barrier": (((5, 8), (6, 8)), ((11, 15), (12, 15)), ((8, 11), (8, 12))),
        "fragmented_periurban_mosaic": (
            ((6, 5), (6, 6)),
            ((12, 14), (12, 15)),
            ((8, 17), (9, 17)),
        ),
        "boundary_sensitive": (((2, 11), (2, 12)), ((15, 11), (15, 12)), ((8, 11), (8, 12))),
        "asymmetric_multicore": (((5, 9), (5, 10)), ((10, 13), (10, 14)), ((8, 11), (8, 12))),
    }[family]


def _action_sites(
    family: LandscapeFamily,
    scenarios: tuple[UsefulnessScenario, ...],
    rasters: dict[str, InMemoryRaster],
) -> tuple[UsefulnessActionSite, ...]:
    sites: list[UsefulnessActionSite] = []
    for index, cells in enumerate(_action_cells(family), start=1):
        replacements: dict[str, float] = {}
        for scenario in scenarios:
            raster = rasters[scenario.resistance_asset_id]
            values = [float(raster.values[row][column]) for row, column in cells]
            factor = (0.34, 0.48, 0.62)[index - 1]
            if scenario.safeguard and index == 3:
                factor = 1.24
            elif scenario.safeguard:
                factor = 0.74
            replacements[scenario.scenario_id] = round(max(1.0, sum(values) / len(values) * factor), 6)
        sites.append(
            UsefulnessActionSite(
                site_id=f"{family}_site_{index}",
                cells=cells,
                cost_usd=(110_000, 180_000, 260_000)[index - 1],
                absolute_replacement_by_scenario=replacements,
            )
        )
    return tuple(sites)


def _portfolios(
    family: LandscapeFamily, sites: tuple[UsefulnessActionSite, ...]
) -> tuple[UsefulnessPortfolio, ...]:
    result: list[UsefulnessPortfolio] = []
    costs = {site.site_id: site.cost_usd for site in sites}
    for bits in itertools.product((0, 1), repeat=len(sites)):
        selected = tuple(site.site_id for site, enabled in zip(sites, bits, strict=True) if enabled)
        code = "".join(str(bit) for bit in bits)
        result.append(
            UsefulnessPortfolio(
                portfolio_id=f"{family}_portfolio_{code}",
                selected_site_ids=selected,
                cost_usd=sum(costs[site_id] for site_id in selected),
            )
        )
    return tuple(result)


def _assumptions(family: LandscapeFamily) -> tuple[SyntheticAssumption, ...]:
    title = family.replace("_", " ")
    return (
        SyntheticAssumption(
            assumption_id=f"{family}_structure",
            category="LANDSCAPE_STRUCTURE",
            statement=f"The {title} cell pattern is authored solely to exercise a distinct graph structure.",
        ),
        SyntheticAssumption(
            assumption_id=f"{family}_focal",
            category="FOCAL_GEOMETRY",
            statement="Anonymous focal identifiers are fixed test geometry and have no species interpretation.",
        ),
        SyntheticAssumption(
            assumption_id=f"{family}_resistance",
            category="RESISTANCE_HYPOTHESIS",
            statement="Positive resistance values are dimensionless synthetic inputs, not fitted observations.",
        ),
        SyntheticAssumption(
            assumption_id=f"{family}_actions",
            category="ACTION_EFFECT",
            statement="Absolute action replacements are fixed test inputs and are not field estimates.",
        ),
        SyntheticAssumption(
            assumption_id=f"{family}_mask",
            category="REVIEW_MASK",
            statement="The review mask marks an anonymous test zone and contains no real road or location.",
        ),
    )


def _case_truths(family: LandscapeFamily) -> tuple[CaseTruthCondition, ...]:
    shared = {
        "linear_bottleneck": (
            (
                "clean_pass",
                "CLEAN_PASS_WITHOUT_REVIEW_MASK_FLAG",
                "DIRECT_CIRCUITSCAPE_REQUIRED",
                "PENDING_DIRECT_CIRCUITSCAPE",
                "PENDING",
                f"{family}_portfolio_001",
                "A clean modeled disposition may be labelled only after its complete direct ensemble and map checks pass.",
            ),
            (
                "incomplete_safeguard",
                "INCOMPLETE_SAFEGUARD_ENSEMBLE",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "BLOCK_BEFORE_COMPARISON",
                "negative_control:omit_safeguard_current",
                "Removing the required safeguard job must block portfolio comparison.",
            ),
            (
                "benign_metadata",
                "BENIGN_EVIDENCE_NO_WARNING",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "NO_WARNING_EXPECTED",
                "negative_control:unchanged_authorized_metadata",
                "Authorized unchanged synthetic metadata must not create a warning.",
            ),
        ),
        "braided_dual_barrier": (
            (
                "road_mask",
                "ROAD_MASK_ADVISORY",
                "DIRECT_CIRCUITSCAPE_REQUIRED",
                "PENDING_DIRECT_CIRCUITSCAPE",
                "PENDING",
                f"{family}_portfolio_101",
                "A review-mask advisory must be computed from retained baseline and portfolio current rasters.",
            ),
            (
                "hypothesis_order",
                "HYPOTHESIS_SENSITIVE_RANKING",
                "DIRECT_CIRCUITSCAPE_REQUIRED",
                "PENDING_DIRECT_CIRCUITSCAPE",
                "PENDING",
                f"{family}_portfolio_110",
                "Any change in portfolio ordering across hypotheses must be derived from direct matrices.",
            ),
        ),
        "fragmented_periurban_mosaic": (
            (
                "over_budget",
                "OVER_BUDGET",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "KNOWN_COST_CONDITION",
                f"{family}_portfolio_111",
                "The all-sites portfolio costs 550000 USD against the fixed 500000 USD test cap.",
            ),
            (
                "unauthorized_effect",
                "UNAUTHORIZED_ACTION_EFFECT",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "BLOCK_BEFORE_EXECUTION",
                "negative_control:unauthorized_replacement",
                "An action replacement absent from the self-authored test ledger must not execute.",
            ),
        ),
        "boundary_sensitive": (
            (
                "stale_revision",
                "STALE_REVISION_OR_HASH",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "BLOCK_BEFORE_EXECUTION",
                "negative_control:stale_definition_sha256",
                "A request bound to a superseded landscape digest must not execute.",
            ),
            (
                "qa_rejection",
                "QA_REJECTION",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "BLOCK_BEFORE_COMPARISON",
                "negative_control:rejected_direct_job",
                "A direct job with rejected QA must keep the portfolio out of comparison.",
            ),
        ),
        "asymmetric_multicore": (
            (
                "hypothesis_order",
                "HYPOTHESIS_SENSITIVE_RANKING",
                "DIRECT_CIRCUITSCAPE_REQUIRED",
                "PENDING_DIRECT_CIRCUITSCAPE",
                "PENDING",
                f"{family}_portfolio_011",
                (
                    "Any change in portfolio ordering across the asymmetric hypotheses must "
                    "be derived from direct matrices."
                ),
            ),
            (
                "benign_metadata",
                "BENIGN_EVIDENCE_NO_WARNING",
                "DETERMINISTIC_INPUT_POLICY",
                "READY_FOR_POLICY_ASSERTION",
                "NO_WARNING_EXPECTED",
                "negative_control:unchanged_authorized_metadata",
                "Authorized unchanged multi-core metadata must not create a warning.",
            ),
        ),
    }[family]
    return tuple(
        CaseTruthCondition(
            condition_id=f"{family}_{condition_id}",
            kind=kind,
            truth_basis=basis,
            truth_status=status,
            expected_gate=gate,
            subject_id=subject,
            assertion=assertion,
        )
        for condition_id, kind, basis, status, gate, subject, assertion in shared
    )


def _build_landscape(family: LandscapeFamily) -> UsefulnessLandscape:
    scenarios = _scenarios(family)
    rasters: dict[str, InMemoryRaster] = {}
    for variant in ("current", "future", "alternate", "safeguard"):
        asset_id = f"{family}_resistance_{variant}"
        rasters[asset_id] = _raster(
            family,
            asset_id,
            RasterValueKind.CONTINUOUS,
            _resistance_values(family, variant),  # type: ignore[arg-type]
        )
    for focal_kind in ("target_a", "target_b", "safeguard"):
        asset_id = f"{family}_focal_{focal_kind}"
        rasters[asset_id] = _raster(
            family,
            asset_id,
            RasterValueKind.IDENTIFIER,
            _focal_values(family, focal_kind),  # type: ignore[arg-type]
        )
    review_mask_id = f"{family}_review_mask"
    rasters[review_mask_id] = _raster(
        family,
        review_mask_id,
        RasterValueKind.MASK,
        _review_mask_values(family),
    )
    sites = _action_sites(family, scenarios, rasters)
    return UsefulnessLandscape(
        family=family,
        title=family.replace("_", " ").title(),
        description=(
            f"Self-authored {family.replace('_', ' ')} graph structure for auditable workflow tests."
        ),
        grid_id=f"{family}_grid_v1",
        rasters=rasters,
        scenarios=scenarios,
        action_sites=sites,
        portfolios=_portfolios(family, sites),
        assumptions=_assumptions(family),
        case_truths=_case_truths(family),
        budget_usd=500_000,
    )


def _narrative_strings(catalog: UsefulnessCatalog) -> tuple[str, ...]:
    values: list[str] = [catalog.notice, catalog.release_blocker]
    for landscape in catalog.landscapes:
        values.extend((landscape.title, landscape.description, landscape.notice))
        for assumption in landscape.assumptions:
            values.extend((assumption.statement, assumption.limitation))
        for truth in landscape.case_truths:
            values.extend((truth.assertion, truth.limitation))
        for site in landscape.action_sites:
            values.append(site.evidence_basis)
    return tuple(values)


def build_usefulness_catalog() -> UsefulnessCatalog:
    """Return the deterministic five-landscape public input catalog."""

    return UsefulnessCatalog(landscapes=tuple(_build_landscape(family) for family in ALL_FAMILIES))


def landscape_definition_sha256(landscape: UsefulnessLandscape) -> str:
    return canonical_sha256(landscape.model_dump(mode="json"))


def usefulness_program_sha256(catalog: UsefulnessCatalog | None = None) -> str:
    catalog = catalog or build_usefulness_catalog()
    return canonical_sha256(catalog.model_dump(mode="json"))


def _apply_portfolio(
    landscape: UsefulnessLandscape,
    scenario: UsefulnessScenario,
    portfolio: UsefulnessPortfolio,
) -> InMemoryRaster:
    source = landscape.rasters[scenario.resistance_asset_id]
    selected = {site.site_id: site for site in landscape.action_sites if site.site_id in portfolio.selected_site_ids}
    values = [list(row) for row in source.values]
    for site_id in portfolio.selected_site_ids:
        site = selected[site_id]
        replacement = site.absolute_replacement_by_scenario[scenario.scenario_id]
        for row, column in site.cells:
            if _is_nodata(values[row][column], source.grid.nodata):
                raise ValueError("prepared action unexpectedly intersects NODATA")
            values[row][column] = replacement
    return InMemoryRaster(
        asset_id=f"prepared_{portfolio.portfolio_id}_{scenario.scenario_id}",
        grid=source.grid,
        values=tuple(tuple(row) for row in values),
        source_sha256=canonical_sha256(
            {
                "source_raster_sha256": source.sha256,
                "portfolio_id": portfolio.portfolio_id,
                "selected_site_ids": portfolio.selected_site_ids,
                "scenario_id": scenario.scenario_id,
            }
        ),
    )


def _pairs_text(scenario: UsefulnessScenario) -> str:
    return "mode\tinclude\n" + "".join(
        f"{pair.from_focal_id}\t{pair.to_focal_id}\n" for pair in scenario.pairs
    )


def _request_payload(scenario: UsefulnessScenario, run_id: str) -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "prepared_run_id": run_id,
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/included_pairs.tsv",
            "review_mask": "inputs/review-mask.asc",
        },
        "pairs": [pair.model_dump(mode="json") for pair in scenario.pairs],
        "settings": {
            "precision": "double",
            "neighbor_rule": scenario.neighbor_rule.value,
            "resistance_average": scenario.resistance_average.value,
            "julia_threads": 1,
            "blas_threads": 1,
            "omp_threads": 1,
            "emit_current_map": True,
        },
        "output_directory": "outputs",
    }


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def usefulness_run_plan_rows(
    catalog: UsefulnessCatalog | None = None,
) -> tuple[DirectRunPlanRow, ...]:
    """Return one checksum-bound pending row per portfolio × complete scenario."""

    catalog = catalog or build_usefulness_catalog()
    rows: list[DirectRunPlanRow] = []
    for landscape in catalog.landscapes:
        review_mask = landscape.rasters[f"{landscape.family}_review_mask"]
        review_bytes = render_esri_ascii(review_mask).encode("utf-8")
        for portfolio in landscape.portfolios:
            for scenario in landscape.scenarios:
                identity = canonical_sha256(
                    {
                        "fixture_version": USEFULNESS_FIXTURE_VERSION,
                        "landscape_sha256": landscape_definition_sha256(landscape),
                        "portfolio_id": portfolio.portfolio_id,
                        "scenario_id": scenario.scenario_id,
                    }
                )
                run_id = f"usefulness_{identity[:20]}"
                request = _request_payload(scenario, run_id)
                prepared = _apply_portfolio(landscape, scenario, portfolio)
                focal = landscape.rasters[scenario.focal_asset_id]
                relative_root = f"{landscape.family}/runs/{run_id}"
                rows.append(
                    DirectRunPlanRow(
                        direct_run_id=run_id,
                        family=landscape.family,
                        portfolio_id=portfolio.portfolio_id,
                        scenario_id=scenario.scenario_id,
                        safeguard=scenario.safeguard,
                        pair_ids=tuple(pair.pair_id for pair in scenario.pairs),
                        request_relative_path=f"{relative_root}/request.json",
                        result_relative_path=f"{relative_root}/result.json",
                        request_sha256=_sha_bytes(_canonical_json_bytes(request)),
                        prepared_resistance_sha256=_sha_bytes(
                            render_esri_ascii(prepared).encode("utf-8")
                        ),
                        focal_raster_sha256=_sha_bytes(
                            render_esri_ascii(focal).encode("utf-8")
                        ),
                        review_mask_sha256=_sha_bytes(review_bytes),
                    )
                )
    return tuple(rows)


def usefulness_run_plan_sha256(catalog: UsefulnessCatalog | None = None) -> str:
    return canonical_sha256(
        [row.model_dump(mode="json") for row in usefulness_run_plan_rows(catalog)]
    )


def usefulness_public_manifest(catalog: UsefulnessCatalog | None = None) -> dict[str, object]:
    """Return repository metadata without any direct outcome slot."""

    catalog = catalog or build_usefulness_catalog()
    rows = usefulness_run_plan_rows(catalog)
    row_by_family: dict[str, list[DirectRunPlanRow]] = {family: [] for family in ALL_FAMILIES}
    for row in rows:
        row_by_family[row.family].append(row)
    payload: dict[str, object] = {
        "schema_version": "ecocorridor-usefulness-program/v1.0",
        "fixture_version": catalog.fixture_version,
        "license": catalog.license,
        "notice": catalog.notice,
        "source": "SELF_AUTHORED_CC0_SYNTHETIC",
        "authorship": "SELF_AUTHORED_CC0_SYNTHETIC",
        "adjudication_status": "PENDING_INDEPENDENT_ADJUDICATION",
        "evidence_state": catalog.evidence_state,
        "truth_status": "PENDING_DIRECT_CIRCUITSCAPE",
        "release_eligible": False,
        "release_blocker": catalog.release_blocker,
        "landscape_count": len(catalog.landscapes),
        "scenario_count": sum(len(item.scenarios) for item in catalog.landscapes),
        "portfolio_count": sum(len(item.portfolios) for item in catalog.landscapes),
        "direct_job_count": len(rows),
        "required_case_conditions": sorted(ALL_CASE_CONDITIONS),
        "landscapes": [
            {
                "family": landscape.family,
                "title": landscape.title,
                "definition_sha256": landscape_definition_sha256(landscape),
                "grid_identity_sha256": grid_identity(next(iter(landscape.rasters.values())).grid),
                "nodata_topology_sha256": _nodata_topology(
                    next(iter(landscape.rasters.values()))
                ),
                "scenario_ids": [scenario.scenario_id for scenario in landscape.scenarios],
                "safeguard_scenario_ids": [
                    scenario.scenario_id for scenario in landscape.scenarios if scenario.safeguard
                ],
                "portfolio_ids": [portfolio.portfolio_id for portfolio in landscape.portfolios],
                "direct_job_count": len(row_by_family[landscape.family]),
                "run_plan_sha256": canonical_sha256(
                    [row.model_dump(mode="json") for row in row_by_family[landscape.family]]
                ),
                "case_truths": [truth.model_dump(mode="json") for truth in landscape.case_truths],
            }
            for landscape in catalog.landscapes
        ],
        "run_plan_sha256": canonical_sha256(
            [row.model_dump(mode="json") for row in rows]
        ),
        "program_sha256": usefulness_program_sha256(catalog),
        "accepted_result_requirements": [
            "Every portfolio has one successful, QA-accepted direct job per required scenario.",
            "Every matrix is reparsed from retained Circuitscape output.",
            "Every requested current raster is retained and reconciled with its matrix run.",
            "Resistance, focal, review-mask, grid, NODATA, engine, request, and result hashes resolve.",
            "No outcome-dependent case truth is promoted before the complete direct ensemble passes.",
        ],
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    return payload


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_record(
    root: Path, path: Path, payload: bytes, role: str
) -> MaterializedUsefulnessFile:
    _atomic_bytes(path, payload)
    return MaterializedUsefulnessFile(
        relative_path=path.relative_to(root).as_posix(),
        sha256=_sha_bytes(payload),
        byte_size=len(payload),
        role=role,
    )


def materialize_usefulness_landscapes(
    output_directory: Path,
    *,
    catalog: UsefulnessCatalog | None = None,
) -> UsefulnessMaterializationManifest:
    """Materialize executable direct-job inputs without creating result artifacts."""

    catalog = catalog or build_usefulness_catalog()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError("usefulness materialization target must be absent or empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    records: list[MaterializedUsefulnessFile] = []
    rows = usefulness_run_plan_rows(catalog)
    row_lookup = {
        (row.family, row.portfolio_id, row.scenario_id): row for row in rows
    }
    for landscape in catalog.landscapes:
        family_root = output_directory / landscape.family
        review_mask = landscape.rasters[f"{landscape.family}_review_mask"]
        records.append(
            _write_record(
                output_directory,
                family_root / "review-mask.asc",
                render_esri_ascii(review_mask).encode("utf-8"),
                "review_mask_input",
            )
        )
        unique_focal_ids = sorted({scenario.focal_asset_id for scenario in landscape.scenarios})
        for asset_id in unique_focal_ids:
            records.append(
                _write_record(
                    output_directory,
                    family_root / "focal" / f"{asset_id}.asc",
                    render_esri_ascii(landscape.rasters[asset_id]).encode("utf-8"),
                    "self_authored_focal_input",
                )
            )
        for scenario in landscape.scenarios:
            records.append(
                _write_record(
                    output_directory,
                    family_root / "resistance" / f"{scenario.scenario_id}.asc",
                    render_esri_ascii(landscape.rasters[scenario.resistance_asset_id]).encode(
                        "utf-8"
                    ),
                    "self_authored_resistance_input",
                )
            )
            records.append(
                _write_record(
                    output_directory,
                    family_root / "pairs" / f"{scenario.scenario_id}.tsv",
                    _pairs_text(scenario).encode("utf-8"),
                    "included_pairs_input",
                )
            )
        records.append(
            _write_record(
                output_directory,
                family_root / "assumptions.json",
                _canonical_json_bytes(
                    [assumption.model_dump(mode="json") for assumption in landscape.assumptions]
                ),
                "self_authored_unadjudicated_synthetic_assumptions",
            )
        )
        records.append(
            _write_record(
                output_directory,
                family_root / "case-truths.json",
                _canonical_json_bytes(
                    [truth.model_dump(mode="json") for truth in landscape.case_truths]
                ),
                "case_truth_plan",
            )
        )
        for portfolio in landscape.portfolios:
            for scenario in landscape.scenarios:
                row = row_lookup[(landscape.family, portfolio.portfolio_id, scenario.scenario_id)]
                run_root = output_directory / Path(row.request_relative_path).parent
                prepared = _apply_portfolio(landscape, scenario, portfolio)
                resources = (
                    ("resistance.asc", render_esri_ascii(prepared).encode("utf-8"), "prepared_resistance"),
                    (
                        "focal.asc",
                        render_esri_ascii(landscape.rasters[scenario.focal_asset_id]).encode("utf-8"),
                        "materialized_focal",
                    ),
                    ("included_pairs.tsv", _pairs_text(scenario).encode("utf-8"), "materialized_pairs"),
                    ("review-mask.asc", render_esri_ascii(review_mask).encode("utf-8"), "materialized_review_mask"),
                )
                for filename, payload, role in resources:
                    records.append(
                        _write_record(
                            output_directory,
                            run_root / "inputs" / filename,
                            payload,
                            role,
                        )
                    )
                request_bytes = _canonical_json_bytes(
                    _request_payload(scenario, row.direct_run_id)
                )
                if _sha_bytes(request_bytes) != row.request_sha256:
                    raise ValueError("materialized request differs from its planned identity")
                records.append(
                    _write_record(
                        output_directory,
                        run_root / "request.json",
                        request_bytes,
                        "engine_request",
                    )
                )
        family_rows = [row for row in rows if row.family == landscape.family]
        records.append(
            _write_record(
                output_directory,
                family_root / "study-plan.json",
                _canonical_json_bytes(
                    {
                        "schema_version": "ecocorridor-usefulness-study-plan/v1.0",
                        "family": landscape.family,
                        "definition_sha256": landscape_definition_sha256(landscape),
                        "execution_state": "PENDING_DIRECT_CIRCUITSCAPE",
                        "release_eligible": False,
                        "jobs": [row.model_dump(mode="json") for row in family_rows],
                    }
                ),
                "direct_run_plan",
            )
        )

    records.sort(key=lambda item: item.relative_path)
    files_sha256 = canonical_sha256([record.model_dump(mode="json") for record in records])
    manifest = UsefulnessMaterializationManifest(
        program_sha256=usefulness_program_sha256(catalog),
        materializer_sha256=_sha_bytes(Path(__file__).read_bytes()),
        run_plan_sha256=usefulness_run_plan_sha256(catalog),
        files=tuple(records),
        files_sha256=files_sha256,
    )
    _atomic_bytes(
        output_directory / "materialization-manifest.json",
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
    )
    return manifest


__all__ = [
    "ALL_CASE_CONDITIONS",
    "ALL_FAMILIES",
    "DIRECT_RESULT_CONDITIONS",
    "CaseTruthCondition",
    "DirectRunPlanRow",
    "UsefulnessCatalog",
    "UsefulnessLandscape",
    "UsefulnessMaterializationManifest",
    "build_usefulness_catalog",
    "landscape_definition_sha256",
    "materialize_usefulness_landscapes",
    "usefulness_program_sha256",
    "usefulness_public_manifest",
    "usefulness_run_plan_rows",
    "usefulness_run_plan_sha256",
]
