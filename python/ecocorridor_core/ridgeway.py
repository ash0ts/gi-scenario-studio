"""Deterministic, CC0 Ridgeway Crossing teaching fixture.

The fixture builder creates reviewed synthetic inputs and a fixed 128-portfolio
study design.  It does **not** fabricate Circuitscape outcomes.  Teaching roles
are selected only after complete direct evidence is supplied.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (
    ActionCellReplacement,
    ActionEffectBasis,
    ActionEffectRaster,
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityCostBasis,
    ConnectivityDecisionBrief,
    ConnectivityDecisionRule,
    ConnectivityModelFitness,
    ConnectivityProjectBundle,
    ConnectivityScenario,
    ContractModel,
    DirectConnectivityRun,
    DirectRunState,
    EvidenceConfidence,
    FitnessDimension,
    FocalAreaSet,
    FocalPair,
    InterventionKind,
    InterventionOption,
    InterventionSite,
    ModelRevision,
    NeighborRule,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    PortfolioSelection,
    QAStatus,
    RasterGrid,
    RasterValueKind,
    ResistanceAverage,
    ResistanceHypothesis,
    ResourcePolicy,
    RiskReviewMask,
    RuleMetric,
    RuleOperator,
    SafeguardRecord,
    SensitiveDataAuthorization,
    SensitivityClass,
    canonical_sha256,
)
from .grid import InMemoryRaster
from .interventions import action_effect_sha256, canonicalize_portfolio
from .metrics import assert_safe_ecological_claim

RIDGEWAY_FIXTURE_VERSION = "1.2.0"
RIDGEWAY_LICENSE = "CC0-1.0"
RIDGEWAY_NOTICE = (
    "Ridgeway Crossing is synthetic. It is not a real place, species assessment, "
    "wildlife-crossing design, collision analysis, or regulatory recommendation."
)
FIXED_TIME = datetime(2026, 8, 3, tzinfo=UTC)


class RidgewayFixture(ContractModel):
    fixture_version: Literal["1.2.0"] = RIDGEWAY_FIXTURE_VERSION
    license: Literal["CC0-1.0"] = RIDGEWAY_LICENSE
    notice: str = RIDGEWAY_NOTICE
    bundle: ConnectivityProjectBundle
    rasters: dict[str, InMemoryRaster]
    portfolios: list[PortfolioSelection]
    baseline_portfolio_id: str
    physics_model_sha256: str
    decision_snapshot_sha256: str
    cost_basis_sha256: str
    assumption_ledger_sha256: str
    required_scenario_count: Literal[12] = 12
    study_portfolio_count: Literal[128] = 128
    direct_matrix_job_count: Literal[1536] = 1536
    teaching_map_job_count: Literal[60] = 60
    evidence_state: Literal["ENGINE_RUN_REQUIRED"] = "ENGINE_RUN_REQUIRED"

    @model_validator(mode="after")
    def validate_arithmetic(self) -> RidgewayFixture:
        if len(self.bundle.scenarios) != self.required_scenario_count:
            raise ValueError("Ridgeway must contain exactly 12 required scenarios")
        if len(self.portfolios) != self.study_portfolio_count:
            raise ValueError("Ridgeway must contain exactly 128 fixed portfolios")
        if self.direct_matrix_job_count != len(self.portfolios) * len(self.bundle.scenarios):
            raise ValueError("Ridgeway direct-job arithmetic is inconsistent")
        if self.baseline_portfolio_id not in {item.portfolio_id for item in self.portfolios}:
            raise ValueError("Ridgeway baseline is absent from the fixed study")
        combined = canonical_sha256(
            {
                "physics_model_sha256": self.physics_model_sha256,
                "decision_snapshot_sha256": self.decision_snapshot_sha256,
            }
        )
        if combined != self.bundle.revision.model_sha256:
            raise ValueError("Ridgeway revision does not bind physics and decision identities")
        return self

    def public_manifest(self) -> dict:
        return {
            "schema_version": "ecocorridor/v1.0",
            "fixture_version": self.fixture_version,
            "license": self.license,
            "notice": self.notice,
            "project_id": self.bundle.project_id,
            "revision_id": self.bundle.revision.revision_id,
            "model_sha256": self.bundle.revision.model_sha256,
            "physics_model_sha256": self.physics_model_sha256,
            "decision_snapshot_sha256": self.decision_snapshot_sha256,
            "cost_basis_sha256": self.cost_basis_sha256,
            "assumption_ledger_sha256": self.assumption_ledger_sha256,
            "scenario_ids": [item.scenario_id for item in self.bundle.scenarios],
            "portfolio_ids": [item.portfolio_id for item in self.portfolios],
            "baseline_portfolio_id": self.baseline_portfolio_id,
            "direct_matrix_job_count": self.direct_matrix_job_count,
            "teaching_map_job_count": self.teaching_map_job_count,
            "evidence_state": self.evidence_state,
            "release_replay_eligible": False,
            "release_blocker": (
                "Pinned Julia/Circuitscape execution and complete direct artifacts are required "
                "before this input fixture can be labelled a verified replay."
            ),
        }


class TeachingPortfolioSelection(ContractModel):
    low_cost_caution_portfolio_id: str
    carry_forward_portfolio_id: str
    over_budget_portfolio_id: str
    hypothesis_collapse_portfolio_id: str
    map_rerun_portfolio_ids: list[str] = Field(min_length=5, max_length=5)
    evidence_mode: Literal["DIRECT_CIRCUITSCAPE", "TEST_ONLY"]
    release_eligible: bool
    selection_notes: dict[str, str]


def _authorization(asset_id: str) -> SensitiveDataAuthorization:
    return SensitiveDataAuthorization(
        asset_id=asset_id,
        ownership="EcoCorridor Scenario Studio contributors",
        license_spdx_or_name=RIDGEWAY_LICENSE,
        sensitivity=SensitivityClass.PUBLIC,
        cloud_disposition=CloudDisposition.RAW_ALLOWED,
        export_allowed=True,
        retention_policy="Synthetic CC0 fixture; retain with release evidence.",
        spatial_redaction="Not required; all coordinates are synthetic and anonymous.",
        approved_deployments=["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED", "LOCAL_ONLY"],
        reviewer="Synthetic fixture review board",
        reviewed_at=FIXED_TIME,
        notes=RIDGEWAY_NOTICE,
    )


def _asset(
    asset_id: str,
    kind: AssetKind,
    path: str,
    payload: object,
    *,
    grid: RasterGrid | None = None,
    media_type: str = "application/json",
) -> ConnectivityAsset:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ConnectivityAsset(
        asset_id=asset_id,
        kind=kind,
        title=asset_id.replace("_", " ").title(),
        relative_path=path,
        sha256=canonical_sha256(payload),
        byte_size=len(encoded),
        media_type=media_type,
        source="Self-authored synthetic Ridgeway fixture",
        vintage=RIDGEWAY_FIXTURE_VERSION,
        license=RIDGEWAY_LICENSE,
        grid=grid,
        authorization=_authorization(asset_id),
    )


def _resistance_values(highway_resistance: float) -> tuple[tuple[float, ...], ...]:
    rows: list[tuple[float, ...]] = []
    for row in range(12):
        values = []
        for column in range(20):
            if column in {9, 10}:
                value = highway_resistance
            else:
                value = 4.0 + abs(9.5 - column) * 0.15 + abs(5.5 - row) * 0.08
            values.append(round(value, 6))
        rows.append(tuple(values))
    return tuple(rows)


def _focal_values(guild_index: int) -> tuple[tuple[int, ...], ...]:
    values = [[0 for _ in range(20)] for _ in range(12)]
    offsets = ((1, 1), (1, 16), (8, 1), (8, 16))
    for focal_id, (top, left) in enumerate(offsets, start=1):
        height = 2 if guild_index != 2 else 3
        for row in range(top, min(top + height, 12)):
            for column in range(left, min(left + 3, 20)):
                values[row][column] = focal_id
    return tuple(tuple(row) for row in values)


def _review_mask_values() -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(1 if column in {8, 9, 10, 11} else 0 for column in range(20)) for _ in range(12)
    )


def _effect(
    *,
    effect_id: str,
    scenario_id: str,
    grid_id: str,
    footprint_asset_id: str,
    row: int,
    resistance: float,
    option_title: str,
) -> ActionEffectRaster:
    basis = ActionEffectBasis(
        basis_id=f"basis_{effect_id}",
        purpose=f"Synthetic reviewed effect for {option_title}",
        evidence_source="Self-authored CC0 teaching assumption",
        citation="Ridgeway Fixture Assumption Ledger v1.0",
        reviewer="Synthetic fixture review board",
        reviewed_at=FIXED_TIME,
        confidence=EvidenceConfidence.ASSUMED,
        assumptions=[
            "Absolute replacement values are synthetic and scenario-specific.",
            "No crossing use, collision reduction, or population effect is represented.",
        ],
        limitations=[RIDGEWAY_NOTICE],
    )
    cells = [
        ActionCellReplacement(row=row, column=9, resistance=resistance),
        ActionCellReplacement(row=row, column=10, resistance=resistance),
    ]
    provisional = ActionEffectRaster(
        effect_id=effect_id,
        scenario_id=scenario_id,
        grid_id=grid_id,
        footprint_asset_id=footprint_asset_id,
        cells=cells,
        basis=basis,
        sha256="0" * 64,
    )
    return provisional.model_copy(update={"sha256": action_effect_sha256(provisional)})


def ridgeway_physics_identity(
    *,
    assets: list[ConnectivityAsset],
    hypotheses: list[ResistanceHypothesis],
    focal_sets: list[FocalAreaSet],
    scenarios: list[ConnectivityScenario],
    sites: list[InterventionSite],
    options: list[InterventionOption],
    review_masks: list[RiskReviewMask],
) -> str:
    """Hash reviewed model inputs that can affect prepared Circuitscape bytes.

    Review masks and site/cost/governance metadata intentionally live in the
    decision identity.  A changed review mask re-reduces an already retained
    current raster; it does not justify another direct solve.
    """

    solver_assets = [
        {
            "asset_id": item.asset_id,
            "kind": item.kind.value,
            "sha256": item.sha256,
            "grid": item.grid.model_dump(mode="json") if item.grid is not None else None,
        }
        for item in assets
        if item.kind
        in {
            AssetKind.RESISTANCE_RASTER,
            AssetKind.FOCAL_RASTER,
        }
    ]
    physics_options = [
        {
            "option_id": item.option_id,
            "site_id": item.site_id,
            "kind": item.kind.value,
            "effects": [
                {
                    "effect_id": effect.effect_id,
                    "scenario_id": effect.scenario_id,
                    "grid_id": effect.grid_id,
                    "cells": [cell.model_dump(mode="json") for cell in effect.cells],
                }
                for effect in item.effects
            ],
        }
        for item in options
    ]
    return canonical_sha256(
        {
            "identity_schema": "ridgeway-physics-model/v1.1",
            "fixture_version": RIDGEWAY_FIXTURE_VERSION,
            "assets": solver_assets,
            "resistance_hypotheses": [item.model_dump(mode="json") for item in hypotheses],
            "focal_area_sets": [item.model_dump(mode="json") for item in focal_sets],
            "scenarios": [item.model_dump(mode="json") for item in scenarios],
            "intervention_effects": physics_options,
        }
    )


def ridgeway_decision_identity(
    *,
    assets: list[ConnectivityAsset],
    sites: list[InterventionSite],
    decision_brief: ConnectivityDecisionBrief,
    safeguards: list[SafeguardRecord],
    review_masks: list[RiskReviewMask],
    rules: list[ConnectivityDecisionRule],
    fitness: ConnectivityModelFitness,
    resource_policy: ResourcePolicy,
    options: list[InterventionOption],
    portfolios: list[PortfolioSelection],
    baseline_portfolio_id: str,
) -> str:
    """Hash costs, governance, rule, fitness, and fixed-study decision evidence."""

    return canonical_sha256(
        {
            "identity_schema": "ridgeway-decision-model/v1.0",
            "asset_contracts_and_authorizations": [item.model_dump(mode="json") for item in assets],
            "intervention_sites": [item.model_dump(mode="json") for item in sites],
            "decision_brief": decision_brief.model_dump(mode="json"),
            "safeguards": [item.model_dump(mode="json") for item in safeguards],
            "review_masks": [item.model_dump(mode="json") for item in review_masks],
            "decision_rules": [item.model_dump(mode="json") for item in rules],
            "fitness": fitness.model_dump(mode="json"),
            "resource_policy": resource_policy.model_dump(mode="json"),
            "intervention_options": [item.model_dump(mode="json") for item in options],
            "portfolios": [item.model_dump(mode="json") for item in portfolios],
            "baseline_portfolio_id": baseline_portfolio_id,
            "cost_basis": ridgeway_cost_basis_payload(options),
            "assumption_ledger": ridgeway_assumption_ledger_payload(options, fitness),
        }
    )


def _content_bound(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "content_sha256": canonical_sha256(payload)}


def ridgeway_cost_basis_payload(options: list[InterventionOption]) -> dict[str, object]:
    return _content_bound(
        {
            "schema_version": "ridgeway-cost-basis/v1.0",
            "cost_basis_id": "ridgeway_synthetic_cost_basis_v1",
            "license": RIDGEWAY_LICENSE,
            "currency": "USD",
            "vintage": "2026 synthetic screening",
            "source": "Self-authored CC0 Ridgeway synthetic site ledger v1.0",
            "options": [
                {
                    "option_id": item.option_id,
                    "site_id": item.site_id,
                    "cost": item.cost,
                    "currency": item.currency,
                    "cost_vintage": item.cost_vintage,
                    "reviewed_by": item.reviewed_by,
                    "reviewed_at": item.reviewed_at.isoformat(),
                }
                for item in options
            ],
            "limitations": [
                "Synthetic screening values are not bids, estimates, or construction costs."
            ],
        }
    )


def ridgeway_assumption_ledger_payload(
    options: list[InterventionOption],
    fitness: ConnectivityModelFitness,
) -> dict[str, object]:
    return _content_bound(
        {
            "schema_version": "ridgeway-assumption-ledger/v1.0",
            "ledger_id": "ridgeway_assumption_ledger_v1",
            "license": RIDGEWAY_LICENSE,
            "notice": RIDGEWAY_NOTICE,
            "effect_bases": [
                {
                    "effect_id": effect.effect_id,
                    "effect_sha256": effect.sha256,
                    "basis": effect.basis.model_dump(mode="json"),
                }
                for option in options
                for effect in option.effects
            ],
            "fitness": fitness.model_dump(mode="json"),
        }
    )


def build_ridgeway_fixture() -> RidgewayFixture:
    continuous_grid = RasterGrid(
        grid_id="ridgeway_grid_v1",
        crs="LOCAL_SYNTHETIC_METERS",
        width=20,
        height=12,
        transform=(25.0, 0.0, 0.0, 0.0, -25.0, 300.0),
        nodata=-9999.0,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=625.0,
    )
    focal_grid = continuous_grid.model_copy(
        update={"value_kind": RasterValueKind.IDENTIFIER, "nodata": 0}
    )
    mask_grid = continuous_grid.model_copy(update={"value_kind": RasterValueKind.MASK, "nodata": 0})
    hypothesis_specs = (
        ("h1_low_barrier", "Low highway penalty", 80.0),
        ("h2_moderate_barrier", "Moderate highway penalty", 180.0),
        ("h3_high_barrier", "High highway penalty", 420.0),
        ("h4_future_pressure", "Future pressure", 760.0),
    )
    guild_specs = (
        ("target_guild_a", "Adult dispersal", False),
        ("target_guild_b", "Seasonal movement", False),
        ("safeguard_guild", "Safeguard movement", True),
    )
    assets: list[ConnectivityAsset] = []
    rasters: dict[str, InMemoryRaster] = {}
    hypotheses: list[ResistanceHypothesis] = []
    for hypothesis_id, title, penalty in hypothesis_specs:
        asset_id = f"asset_resistance_{hypothesis_id}"
        values = _resistance_values(penalty)
        raster = InMemoryRaster(
            asset_id=asset_id,
            grid=continuous_grid,
            values=values,
            source_sha256=canonical_sha256(values),
        )
        rasters[asset_id] = raster
        assets.append(
            _asset(
                asset_id,
                AssetKind.RESISTANCE_RASTER,
                f"inputs/resistance/{hypothesis_id}.json",
                values,
                grid=continuous_grid,
            )
        )
        hypotheses.append(
            ResistanceHypothesis(
                hypothesis_id=hypothesis_id,
                title=title,
                guild_id="synthetic_shared_hypothesis_axis",
                life_stage="Scenario-specific; see ConnectivityScenario",
                future_condition="Current"
                if hypothesis_id != "h4_future_pressure"
                else "Future pressure",
                maturity_horizon="Mature action effect",
                resistance_asset_id=asset_id,
                grid_id=continuous_grid.grid_id,
                rationale="Synthetic contrast for robustness teaching; not a biological estimate.",
                derivation="Self-authored deterministic values with an explicit highway penalty.",
                evidence_citations=["Ridgeway Fixture Assumption Ledger v1.0"],
                confidence=EvidenceConfidence.ASSUMED,
                reviewed_by="Synthetic fixture review board",
                reviewed_at=FIXED_TIME,
            )
        )
    focal_sets: list[FocalAreaSet] = []
    scenarios: list[ConnectivityScenario] = []
    for guild_index, (guild_id, life_stage, safeguard) in enumerate(guild_specs):
        focal_asset_id = f"asset_focal_{guild_id}"
        values = _focal_values(guild_index)
        raster = InMemoryRaster(
            asset_id=focal_asset_id,
            grid=focal_grid,
            values=values,
            source_sha256=canonical_sha256(values),
        )
        rasters[focal_asset_id] = raster
        assets.append(
            _asset(
                focal_asset_id,
                AssetKind.FOCAL_RASTER,
                f"inputs/focal/{guild_id}.json",
                values,
                grid=focal_grid,
            )
        )
        pair_specs = (
            ((1, 2), (3, 4))
            if guild_id == "target_guild_a"
            else ((1, 4), (2, 3))
            if guild_id == "target_guild_b"
            else ((1, 3), (2, 4))
        )
        pairs = [
            FocalPair(
                pair_id=f"{guild_id}_pair_{left}_{right}",
                from_focal_id=left,
                to_focal_id=right,
            )
            for left, right in pair_specs
        ]
        focal_set_id = f"focal_set_{guild_id}"
        focal_sets.append(
            FocalAreaSet(
                focal_set_id=focal_set_id,
                title=f"Synthetic cores for {guild_id}",
                asset_id=focal_asset_id,
                grid_id=focal_grid.grid_id,
                guild_id=guild_id,
                focal_ids=[1, 2, 3, 4],
                pairs=pairs,
                rationale="Four anonymous synthetic cores selected for a teaching landscape.",
                reviewed_by="Synthetic fixture review board",
                reviewed_at=FIXED_TIME,
            )
        )
        for hypothesis_id, _, _ in hypothesis_specs:
            scenarios.append(
                ConnectivityScenario(
                    scenario_id=f"scenario_{guild_id}_{hypothesis_id}",
                    title=f"{guild_id} under {hypothesis_id}",
                    guild_id=guild_id,
                    life_stage=life_stage,
                    resistance_hypothesis_id=hypothesis_id,
                    focal_set_id=focal_set_id,
                    future_condition="Current"
                    if hypothesis_id != "h4_future_pressure"
                    else "Future pressure",
                    maturity_horizon="Mature action effect",
                    included_pair_ids=[item.pair_id for item in pairs],
                    safeguard=safeguard,
                    grid_id=continuous_grid.grid_id,
                    neighbor_rule=NeighborRule.EIGHT,
                    resistance_average=ResistanceAverage.ARITHMETIC,
                )
            )
    mask_values = _review_mask_values()
    mask_asset_id = "asset_road_review_mask"
    rasters[mask_asset_id] = InMemoryRaster(
        asset_id=mask_asset_id,
        grid=mask_grid,
        values=mask_values,
        source_sha256=canonical_sha256(mask_values),
    )
    assets.append(
        _asset(
            mask_asset_id,
            AssetKind.REVIEW_MASK,
            "inputs/review-masks/highway.json",
            mask_values,
            grid=mask_grid,
        )
    )
    sites: list[InterventionSite] = []
    options: list[InterventionOption] = []
    primary_kinds = (
        InterventionKind.CROSSING_COMPLETE_GUIDANCE,
        InterventionKind.APPROACH_RESTORATION,
        InterventionKind.CROSSING_APPROACH,
        InterventionKind.COMPOSITE,
    )
    site_rows = (1, 2, 4, 5, 7, 8, 9, 10)
    site_costs = (85000.0, 65000.0, 120000.0, 155000.0, 75000.0, 95000.0, 140000.0, 110000.0)
    for index, (row, cost) in enumerate(zip(site_rows, site_costs, strict=True), start=1):
        site_id = f"site_{index:02d}"
        footprint_id = f"asset_footprint_{site_id}"
        footprint_values = tuple(
            tuple(1 if row_index == row and column in {9, 10} else 0 for column in range(20))
            for row_index in range(12)
        )
        footprint_sha256 = canonical_sha256(footprint_values)
        rasters[footprint_id] = InMemoryRaster(
            asset_id=footprint_id,
            grid=mask_grid,
            values=footprint_values,
            source_sha256=footprint_sha256,
        )
        assets.append(
            _asset(
                footprint_id,
                AssetKind.ACTION_EFFECT,
                f"inputs/interventions/{site_id}.json",
                footprint_values,
                grid=mask_grid,
            )
        )
        no_action_id = f"{site_id}_no_action"
        survey_id = f"{site_id}_survey_first"
        action_id = f"{site_id}_reviewed_action"
        options.extend(
            [
                InterventionOption(
                    option_id=no_action_id,
                    site_id=site_id,
                    title="No action",
                    kind=InterventionKind.NO_ACTION,
                    cost=0,
                    currency="USD",
                    cost_vintage="2026 synthetic screening",
                    land_access="No action modeled; access not evaluated.",
                    maintenance="None modeled.",
                    reviewed_by="Synthetic fixture review board",
                    reviewed_at=FIXED_TIME,
                ),
                InterventionOption(
                    option_id=survey_id,
                    site_id=site_id,
                    title="Survey first",
                    kind=InterventionKind.SURVEY_FIRST,
                    cost=12000.0,
                    currency="USD",
                    cost_vintage="2026 synthetic screening",
                    land_access="Unknown pending synthetic field review.",
                    maintenance="Not applicable until an action is reviewed.",
                    unresolved_concerns=["Synthetic field assumption remains unresolved."],
                    reviewed_by="Synthetic fixture review board",
                    reviewed_at=FIXED_TIME,
                ),
            ]
        )
        kind = primary_kinds[(index - 1) % len(primary_kinds)]
        effects = []
        for scenario in scenarios:
            hypothesis_index = next(
                position
                for position, spec in enumerate(hypothesis_specs)
                if spec[0] == scenario.resistance_hypothesis_id
            )
            guild_factor = (
                1.0
                if scenario.guild_id == "target_guild_a"
                else 1.25
                if scenario.guild_id == "target_guild_b"
                else 1.6
            )
            kind_factor = {
                InterventionKind.CROSSING_COMPLETE_GUIDANCE: 1.0,
                InterventionKind.APPROACH_RESTORATION: 2.8,
                InterventionKind.CROSSING_APPROACH: 1.8,
                InterventionKind.COMPOSITE: 0.8,
            }[kind]
            replacement = round((3.0 + hypothesis_index * 0.75) * guild_factor * kind_factor, 6)
            effects.append(
                _effect(
                    effect_id=f"effect_{action_id}_{scenario.scenario_id}",
                    scenario_id=scenario.scenario_id,
                    grid_id=continuous_grid.grid_id,
                    footprint_asset_id=footprint_id,
                    row=row,
                    resistance=replacement,
                    option_title=kind.value,
                )
            )
        options.append(
            InterventionOption(
                option_id=action_id,
                site_id=site_id,
                title=kind.value.replace("_", " ").title(),
                kind=kind,
                effects=effects,
                cost=cost,
                currency="USD",
                cost_vintage="2026 synthetic screening",
                dependencies=[
                    "Field reconnaissance",
                    "Ecological review",
                    "Engineering feasibility",
                ],
                exclusions=["No structural crossing design", "No collision-reduction claim"],
                land_access="Synthetic access assumption; requires field confirmation.",
                maintenance="Synthetic screening allowance; ownership unresolved.",
                external_feasibility_evidence=["Ridgeway synthetic site ledger v1.0"],
                unresolved_concerns=(
                    ["Maintenance ownership is unresolved."] if index == 6 else []
                ),
                reviewed_by="Synthetic fixture review board",
                reviewed_at=FIXED_TIME,
            )
        )
        sites.append(
            InterventionSite(
                site_id=site_id,
                title=f"Ridgeway intervention site {index}",
                footprint_asset_id=footprint_id,
                option_ids=[no_action_id, action_id, survey_id],
                known_constraints=(
                    ["Synthetic land-access status is unresolved."] if index == 6 else []
                ),
                reviewed_by="Synthetic fixture review board",
                reviewed_at=FIXED_TIME,
            )
        )
    baseline = canonicalize_portfolio(
        sites,
        options,
        {site.site_id: f"{site.site_id}_no_action" for site in sites},
    )
    portfolios: list[PortfolioSelection] = []
    for value in range(128):
        bits = [(value >> bit) & 1 for bit in range(7)]
        bits.append(sum(bits) % 2)
        selections = {
            site.site_id: (
                f"{site.site_id}_reviewed_action" if bit else f"{site.site_id}_no_action"
            )
            for site, bit in zip(sites, bits, strict=True)
        }
        portfolios.append(canonicalize_portfolio(sites, options, selections))
    # Parity construction includes the all-zero baseline exactly once.
    portfolios.sort(
        key=lambda item: (item.portfolio_id != baseline.portfolio_id, item.portfolio_id)
    )
    dimensions = [
        FitnessDimension(
            key=key,
            status="REVIEW_REQUIRED" if key == "unresolved_assumptions" else "ACCEPTED",
            evidence_ids=["ridgeway_assumption_ledger_v1"],
            reviewer="Synthetic fixture review board",
            notes=(
                "One synthetic field assumption is intentionally unresolved for teaching."
                if key == "unresolved_assumptions"
                else "Accepted only for the synthetic teaching purpose."
            ),
        )
        for key in (
            "intended_use",
            "movement_process",
            "focal_rationale",
            "resistance_derivation",
            "validation_evidence",
            "spatial_scale",
            "boundary_effects",
            "action_effect_evidence",
            "scenario_coverage",
            "sensitivity",
            "numerical_qa",
            "unresolved_assumptions",
        )
    ]
    target_scenarios = [item.scenario_id for item in scenarios if not item.safeguard]
    safeguard_scenarios = [item.scenario_id for item in scenarios if item.safeguard]
    rules = [
        ConnectivityDecisionRule(
            rule_id="rule_target_improvement",
            title="Target-pair minimum improvement",
            metric=RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
            operator=RuleOperator.AT_LEAST,
            threshold=3.0,
            scenario_ids=target_scenarios,
            rationale="Synthetic teaching threshold; not a biological standard.",
        ),
        ConnectivityDecisionRule(
            rule_id="rule_safeguard",
            title="Safeguard nonregression",
            metric=RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION,
            operator=RuleOperator.EQUALS,
            threshold=True,
            scenario_ids=safeguard_scenarios,
            rationale="Require no modeled safeguard resistance increase beyond its tolerance.",
        ),
        ConnectivityDecisionRule(
            rule_id="rule_review_mask",
            title="Road-mask current redistribution review",
            metric=RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT,
            operator=RuleOperator.AT_MOST,
            threshold=15.0,
            required=False,
            rationale="Diagnostic review trigger only; not an animal-use prediction.",
        ),
        ConnectivityDecisionRule(
            rule_id="rule_cost",
            title="Synthetic screening cost cap",
            metric=RuleMetric.MAX_COST,
            operator=RuleOperator.AT_MOST,
            threshold=500000.0,
            rationale="Synthetic 2026 USD teaching constraint.",
        ),
        ConnectivityDecisionRule(
            rule_id="rule_qa",
            title="Accepted direct-run QA",
            metric=RuleMetric.REQUIRE_ACCEPTED_QA,
            operator=RuleOperator.EQUALS,
            threshold=True,
            rationale="Every portfolio × scenario result must have accepted direct QA.",
        ),
    ]
    decision_brief = ConnectivityDecisionBrief(
        brief_id="brief_ridgeway_v1",
        title="Ridgeway Crossing synthetic connectivity screening",
        decision_question=(
            "Which synthetic intervention portfolio, if any, should advance to ecological, field, "
            "and engineering feasibility review?"
        ),
        decision_owner="Synthetic state DOT environmental team",
        intended_use="Demonstrate robust, auditable connectivity portfolio screening.",
        target_guilds=["target_guild_a", "target_guild_b"],
        safeguard_guilds=["safeguard_guild"],
        budget_amount=500000.0,
        currency="USD",
        cost_vintage="2026 synthetic screening",
        non_goals=[
            "Observed animal movement or crossing use",
            "Collision reduction",
            "Population viability or biodiversity benefit",
            "Crossing, fencing, or construction design",
            "Regulatory or permitting determination",
        ],
        required_next_action="Field, ecological, engineering, ownership, and permitting feasibility review.",
        conceptual=True,
    )
    revision_id = "revision_ridgeway_v1_2"
    safeguards = [
        SafeguardRecord(
            safeguard_id="safeguard_ridgeway",
            guild_id="safeguard_guild",
            scenario_ids=safeguard_scenarios,
            nonregression_threshold_percent=0.0,
            concerns=["Diagnostic model result only; no population inference."],
            reviewed_by="Synthetic fixture review board",
            reviewed_at=FIXED_TIME,
        )
    ]
    review_masks = [
        RiskReviewMask(
            mask_id="road_review_mask",
            title="Synthetic highway review mask",
            asset_id=mask_asset_id,
            grid_id=mask_grid.grid_id,
            review_trigger_percent=15.0,
            interpretation="Highlights modeled current redistribution requiring review.",
        )
    ]
    fitness = ConnectivityModelFitness(
        passport_id="fitness_ridgeway_v1",
        revision_id=revision_id,
        intended_use=decision_brief.intended_use,
        dimensions=dimensions,
        unresolved_assumptions=[
            "Maintenance ownership at site 06 is unresolved for the teaching workflow."
        ],
        attestations=["Synthetic fixture only"],
        reviewed_by="Synthetic fixture review board",
        reviewed_at=FIXED_TIME,
    )
    resource_policy = ResourcePolicy(
        max_input_bytes=100_000_000,
        max_materialized_bytes=500_000_000,
        max_current_raster_bytes=200_000_000,
        max_direct_jobs=2_000,
        max_wall_seconds_per_job=300,
    )
    physics_model_sha256 = ridgeway_physics_identity(
        assets=assets,
        hypotheses=hypotheses,
        focal_sets=focal_sets,
        scenarios=scenarios,
        sites=sites,
        options=options,
        review_masks=review_masks,
    )
    decision_snapshot_sha256 = ridgeway_decision_identity(
        assets=assets,
        sites=sites,
        decision_brief=decision_brief,
        safeguards=safeguards,
        review_masks=review_masks,
        rules=rules,
        fitness=fitness,
        resource_policy=resource_policy,
        options=options,
        portfolios=portfolios,
        baseline_portfolio_id=baseline.portfolio_id,
    )
    cost_basis_sha256 = str(ridgeway_cost_basis_payload(options)["content_sha256"])
    assumption_ledger_sha256 = str(
        ridgeway_assumption_ledger_payload(options, fitness)["content_sha256"]
    )
    model_hash = canonical_sha256(
        {
            "physics_model_sha256": physics_model_sha256,
            "decision_snapshot_sha256": decision_snapshot_sha256,
        }
    )
    revision = ModelRevision(
        revision_id=revision_id,
        project_id="ridgeway_crossing",
        created_at=FIXED_TIME,
        created_by="EcoCorridor Scenario Studio contributors",
        reason="Create deterministic CC0 teaching input fixture.",
        model_sha256=model_hash,
    )
    bundle = ConnectivityProjectBundle(
        project_id="ridgeway_crossing",
        title="Ridgeway Crossing",
        revision=revision,
        decision_brief=decision_brief,
        assets=assets,
        resistance_hypotheses=hypotheses,
        focal_area_sets=focal_sets,
        scenarios=scenarios,
        intervention_sites=sites,
        intervention_options=options,
        cost_basis=ConnectivityCostBasis(
            cost_basis_id="ridgeway_synthetic_cost_basis_v1",
            source="Self-authored CC0 Ridgeway synthetic site ledger v1.0",
            currency="USD",
            vintage="2026 synthetic screening",
            reviewer="Synthetic fixture review board",
            reviewed_at=FIXED_TIME,
            option_costs={item.option_id: item.cost for item in options},
            assumptions=[
                "Synthetic screening values are not bids, estimates, or construction costs."
            ],
        ),
        safeguards=safeguards,
        review_masks=review_masks,
        decision_rules=rules,
        fitness=fitness,
        resource_policy=resource_policy,
    )
    return RidgewayFixture(
        bundle=bundle,
        rasters=rasters,
        portfolios=portfolios,
        baseline_portfolio_id=baseline.portfolio_id,
        physics_model_sha256=physics_model_sha256,
        decision_snapshot_sha256=decision_snapshot_sha256,
        cost_basis_sha256=cost_basis_sha256,
        assumption_ledger_sha256=assumption_ledger_sha256,
    )


def build_ridgeway_bundle() -> ConnectivityProjectBundle:
    """Return the authoritative worker bundle for RPC/governance projections."""

    return build_ridgeway_fixture().bundle


def _rule_failures(summary: PortfolioDecisionSummary) -> set[str]:
    return {item.rule_id for item in summary.rule_dispositions if not item.passed}


def _target_reductions(summary: PortfolioDecisionSummary) -> list[float]:
    return [
        float(item.observed_value)
        for item in summary.rule_dispositions
        if item.rule_id == "rule_target_improvement"
        and isinstance(item.observed_value, (int, float))
    ]


def select_teaching_portfolios(
    fixture: RidgewayFixture,
    summaries: list[PortfolioDecisionSummary],
    *,
    direct_runs: list[DirectConnectivityRun] | None = None,
    test_only: bool = False,
) -> TeachingPortfolioSelection:
    """Select observed teaching roles; never pre-author outcome IDs or values."""

    if len({item.portfolio_id for item in summaries}) != fixture.study_portfolio_count:
        raise ValueError("teaching selection requires one summary for every fixed portfolio")
    direct_evidence_complete = validate_ridgeway_direct_evidence(fixture, direct_runs or [])
    if not test_only and not direct_evidence_complete:
        raise ValueError("release teaching selection requires all 1,536 direct Circuitscape jobs")
    by_id = {item.portfolio_id: item for item in summaries}
    budget = float(fixture.bundle.decision_brief.budget_amount or 0)
    caution_candidates = [
        item
        for item in summaries
        if item.cost <= budget
        and item.review_mask_triggered
        and item.disposition in {PortfolioDisposition.FAILS, PortfolioDisposition.SURVEY_FIRST}
        and _target_reductions(item)
        and max(_target_reductions(item)) > 0
    ]
    if not caution_candidates:
        raise ValueError("direct results contain no low-cost caution lesson with a road-mask flag")
    caution = min(caution_candidates, key=lambda item: (item.cost, item.portfolio_id))
    carry_candidates = [
        item
        for item in summaries
        if item.disposition == PortfolioDisposition.PASSES
        and item.cost <= budget
        and not item.review_mask_triggered
        and not item.unresolved_feasibility
        and item.worst_resistance_reduction_percent is not None
    ]
    if not carry_candidates:
        raise ValueError("direct results contain no robust under-budget carry-forward lesson")
    carry = max(
        carry_candidates,
        key=lambda item: (
            item.worst_resistance_reduction_percent or float("-inf"),
            -item.cost,
            item.portfolio_id,
        ),
    )
    over_budget_candidates = [
        item
        for item in summaries
        if item.cost > budget
        and _rule_failures(item).issubset({"rule_cost"})
        and item.worst_resistance_reduction_percent is not None
    ]
    if not over_budget_candidates:
        raise ValueError("direct results contain no robust over-budget lesson")
    over_budget = max(
        over_budget_candidates,
        key=lambda item: (
            item.worst_resistance_reduction_percent or float("-inf"),
            item.cost,
            item.portfolio_id,
        ),
    )
    collapse_candidates = []
    for item in summaries:
        reductions = _target_reductions(item)
        if reductions and max(reductions) > 0 and min(reductions) < 0:
            collapse_candidates.append((max(reductions) - min(reductions), item))
    if not collapse_candidates:
        raise ValueError("direct results contain no hypothesis-collapse lesson")
    collapse = max(collapse_candidates, key=lambda pair: (pair[0], pair[1].portfolio_id))[1]
    chosen = {
        caution.portfolio_id,
        carry.portfolio_id,
        over_budget.portfolio_id,
        collapse.portfolio_id,
    }
    if len(chosen) != 4:
        raise ValueError("teaching roles did not resolve to four distinct observed portfolios")
    for summary in (caution, carry, over_budget, collapse):
        assert_safe_ecological_claim(summary.next_action)
        if summary.portfolio_id not in by_id:
            raise AssertionError("selected portfolio disappeared from evidence set")
    return TeachingPortfolioSelection(
        low_cost_caution_portfolio_id=caution.portfolio_id,
        carry_forward_portfolio_id=carry.portfolio_id,
        over_budget_portfolio_id=over_budget.portfolio_id,
        hypothesis_collapse_portfolio_id=collapse.portfolio_id,
        map_rerun_portfolio_ids=[
            fixture.baseline_portfolio_id,
            caution.portfolio_id,
            carry.portfolio_id,
            over_budget.portfolio_id,
            collapse.portfolio_id,
        ],
        evidence_mode="TEST_ONLY" if test_only else "DIRECT_CIRCUITSCAPE",
        release_eligible=bool(direct_evidence_complete and not test_only),
        selection_notes={
            "low_cost_caution": "Observed positive headline result plus road-mask review trigger.",
            "carry_forward": (
                "Observed robust, under-budget candidate for ecological, field, and engineering "
                "feasibility review."
            ),
            "over_budget": "Observed robust ecological rule set with cost-rule failure.",
            "hypothesis_collapse": "Observed ranking sensitivity across resistance hypotheses.",
        },
    )


def validate_ridgeway_direct_evidence(
    fixture: RidgewayFixture,
    runs: list[DirectConnectivityRun],
) -> bool:
    expected = {
        (portfolio.portfolio_id, scenario.scenario_id)
        for portfolio in fixture.portfolios
        for scenario in fixture.bundle.scenarios
    }
    observed: set[tuple[str, str]] = set()
    for run in runs:
        identity = (run.portfolio_id, run.scenario_id)
        if identity in observed:
            return False
        observed.add(identity)
        if (
            run.state != DirectRunState.SUCCEEDED
            or not run.exact
            or run.test_only
            or run.engine_name != "Circuitscape"
            or run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}
        ):
            return False
    return observed == expected


def build_public_ui_projection(fixture: RidgewayFixture) -> dict:
    """Build the exact fail-closed UI contract used before current evidence exists.

    The planning story may expose authored rules and scenario definitions.  It
    must not retain a portfolio identity, rank, cost result, model result, map
    diagnostic, or direct-run identity from an earlier evidence contract.
    """

    hypothesis_titles = {
        item.hypothesis_id: item.title for item in fixture.bundle.resistance_hypotheses
    }
    guild_titles = {
        "target_guild_a": "Target guild A",
        "target_guild_b": "Target guild B",
        "safeguard_guild": "Safeguard guild",
    }
    role_placeholders = (
        (
            "low-cost-caution",
            "Low-cost caution",
            "caution",
            "Reserved for a current direct-run lower-cost option with an important review flag.",
        ),
        (
            "carry-forward",
            "Carry-forward candidate",
            "carry-forward",
            "Reserved for a current direct-run robust candidate that satisfies the teaching rules.",
        ),
        (
            "over-budget",
            "Robust but over budget",
            "over-budget",
            "Reserved for a current direct-run result that fails the authored cost rule.",
        ),
        (
            "single-hypothesis-collapse",
            "Hypothesis-sensitive option",
            "brittle",
            "Reserved for a current direct-run option whose rank changes materially across hypotheses.",
        ),
    )
    blocker = (
        "Current direct Circuitscape evidence has not passed the v1.1 evidence contract. "
        "The retained earlier archive is historical benchmark material and is not a verified replay."
    )
    return {
        "schemaVersion": "ecocorridor-replay/v1.1",
        "fixtureId": fixture.bundle.project_id,
        "baselinePortfolioId": None,
        "title": fixture.bundle.title,
        "cc0": True,
        "manifestSha256": None,
        "manifestVersion": fixture.fixture_version,
        "evidenceState": "ENGINE_RUN_REQUIRED",
        "releaseReplayEligible": False,
        "releaseBlocker": blocker,
        "projectionGeneratedAt": FIXED_TIME.isoformat().replace("+00:00", "Z"),
        "disclaimer": fixture.notice,
        "decisionBrief": {
            "question": fixture.bundle.decision_brief.decision_question,
            "budgetUsd": float(fixture.bundle.decision_brief.budget_amount or 0),
            "targetRule": (
                "Reduce effective resistance by at least 3% for every required target pair "
                "within each target scenario."
            ),
            "safeguardRule": (
                "Do not increase effective resistance for any required safeguard pair; "
                "road-mask current redistribution remains a diagnostic review flag."
            ),
            "nonGoals": fixture.bundle.decision_brief.non_goals,
        },
        "study": {
            "portfolioCount": len(fixture.portfolios),
            "scenarioCount": len(fixture.bundle.scenarios),
            "matrixJobCount": fixture.direct_matrix_job_count,
            "mapJobCount": fixture.teaching_map_job_count,
            "coreCount": 4,
            "interventionSiteCount": len(fixture.bundle.intervention_sites),
            "hypothesisRankingMetric": None,
        },
        "scenarios": [
            {
                "id": scenario.scenario_id,
                "guild": guild_titles[scenario.guild_id],
                "purpose": "safeguard" if scenario.safeguard else "target",
                "hypothesis": hypothesis_titles[scenario.resistance_hypothesis_id],
                "futureCondition": scenario.future_condition,
                "maturity": scenario.maturity_horizon,
                "includedPairs": len(scenario.included_pair_ids),
            }
            for scenario in fixture.bundle.scenarios
        ],
        "portfolios": [
            {
                "projectionKey": projection_key,
                "id": None,
                "name": name,
                "role": role,
                "costUsd": None,
                "ruleStatus": "unavailable",
                "passCount": None,
                "requiredCount": len(fixture.bundle.scenarios),
                "worstResistanceChangePct": None,
                "worstScenarioId": None,
                "roadMaskFlag": None,
                "nextAction": (
                    "Run and independently package the complete pinned Circuitscape study before "
                    "interpreting this evidence role."
                ),
                "optionSummary": option_summary,
                "hypothesisRanks": None,
                "scenarioResults": [],
            }
            for projection_key, name, role, option_summary in role_placeholders
        ],
    }
