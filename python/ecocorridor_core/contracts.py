"""Versioned public contracts for EcoCorridor Scenario Studio v0.1.

These models are deliberately explicit about grid identity, review state, data
authorization, and the boundary between a direct Circuitscape result and a
diagnostic or test-only calculation.  Unknown fields are rejected so that an
older worker cannot silently ignore a newer ecological assumption.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ECOCORRIDOR_SCHEMA_VERSION = "1.0"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=SHA256_PATTERN),
]
CONCEPTUAL_CONNECTIVITY_NOTICE = (
    "Conceptual connectivity screening — not observed animal movement, crossing use, "
    "collision reduction, population viability, or an approved corridor."
)
REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION = "DIAGNOSTIC_ONLY_NOT_NUMERICALLY_COMPARED_V0_1"
REFERENCE_COMPARISON_FORMULA = (
    "ABS_OBSERVED_MINUS_REFERENCE_LE_ABSOLUTE_PLUS_RELATIVE_TIMES_ABS_REFERENCE"
)
CURRENT_RASTER_LIMITATION = (
    "Reference current rasters are retained for diagnostic inspection only and are not "
    "numerically compared in v0.1."
)
FITNESS_LIMITATION = (
    "A passing reference-matrix comparison does not automatically attest model fitness."
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _check_sha256(value: str, label: str = "digest") -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        allow_inf_nan=False,
    )


class EvidenceConfidence(str, Enum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    ASSUMED = "ASSUMED"


class AssetKind(str, Enum):
    RESISTANCE_RASTER = "RESISTANCE_RASTER"
    FOCAL_RASTER = "FOCAL_RASTER"
    ACTION_EFFECT = "ACTION_EFFECT"
    REVIEW_MASK = "REVIEW_MASK"
    VECTOR_CONTEXT = "VECTOR_CONTEXT"
    DOCUMENT = "DOCUMENT"
    REFERENCE_MATRIX = "REFERENCE_MATRIX"
    REFERENCE_CURRENT = "REFERENCE_CURRENT"


class SensitivityClass(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE_LOCATION = "SENSITIVE_LOCATION"
    CRITICAL_INFRASTRUCTURE = "CRITICAL_INFRASTRUCTURE"
    TRIBAL_OR_CULTURAL = "TRIBAL_OR_CULTURAL"
    LICENSE_RESTRICTED = "LICENSE_RESTRICTED"


class CloudDisposition(str, Enum):
    RAW_ALLOWED = "RAW_ALLOWED"
    GENERALIZED_DERIVATIVE_ONLY = "GENERALIZED_DERIVATIVE_ONLY"
    DEDICATED_OR_SELF_MANAGED_ONLY = "DEDICATED_OR_SELF_MANAGED_ONLY"
    PROHIBITED = "PROHIBITED"


class RasterValueKind(str, Enum):
    CONTINUOUS = "CONTINUOUS"
    CATEGORICAL = "CATEGORICAL"
    IDENTIFIER = "IDENTIFIER"
    MASK = "MASK"


class ResamplingMethod(str, Enum):
    NONE = "NONE"
    NEAREST = "NEAREST"
    BILINEAR = "BILINEAR"


class EngineMode(str, Enum):
    RASTER_PAIRWISE = "RASTER_PAIRWISE"
    RASTER_ADVANCED = "RASTER_ADVANCED"
    NETWORK_PAIRWISE = "NETWORK_PAIRWISE"
    ONE_TO_ALL = "ONE_TO_ALL"
    ALL_TO_ONE = "ALL_TO_ONE"


class RasterSemantics(str, Enum):
    RESISTANCE = "RESISTANCE"
    CONDUCTANCE = "CONDUCTANCE"


class NeighborRule(str, Enum):
    FOUR = "FOUR"
    EIGHT = "EIGHT"


class ResistanceAverage(str, Enum):
    ARITHMETIC = "ARITHMETIC"
    HARMONIC = "HARMONIC"


class InterventionKind(str, Enum):
    NO_ACTION = "NO_ACTION"
    CROSSING_APPROACH = "CROSSING_APPROACH"
    CROSSING_COMPLETE_GUIDANCE = "CROSSING_COMPLETE_GUIDANCE"
    APPROACH_RESTORATION = "APPROACH_RESTORATION"
    COMPOSITE = "COMPOSITE"
    SURVEY_FIRST = "SURVEY_FIRST"


class QAStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    WARNING_ACCEPTED = "WARNING_ACCEPTED"
    REJECTED = "REJECTED"


class DirectRunState(str, Enum):
    PLANNED = "PLANNED"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    QA_REJECTED = "QA_REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ResourceObservationStatus(str, Enum):
    """How a computational resource value was obtained.

    ``NOT_MEASURED`` is a first-class state.  It prevents the application from
    turning a missing energy meter or process-memory sampler into a zero.
    """

    MEASURED = "MEASURED"
    ESTIMATED = "ESTIMATED"
    NOT_MEASURED = "NOT_MEASURED"


class ResourceObservationBoundary(str, Enum):
    DIRECT_SOLVER = "DIRECT_SOLVER"
    CACHE_REUSE = "CACHE_REUSE"
    SEARCH_PROPOSER = "SEARCH_PROPOSER"


class RuleMetric(str, Enum):
    MIN_RESISTANCE_REDUCTION_PERCENT = "MIN_RESISTANCE_REDUCTION_PERCENT"
    MAX_RESISTANCE_INCREASE_PERCENT = "MAX_RESISTANCE_INCREASE_PERCENT"
    MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT = "MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT"
    MAX_COST = "MAX_COST"
    REQUIRE_ACCEPTED_QA = "REQUIRE_ACCEPTED_QA"
    REQUIRE_SAFEGUARD_NONREGRESSION = "REQUIRE_SAFEGUARD_NONREGRESSION"


class RuleOperator(str, Enum):
    AT_LEAST = "AT_LEAST"
    AT_MOST = "AT_MOST"
    EQUALS = "EQUALS"


class PortfolioDisposition(str, Enum):
    PASSES = "PASSES"
    FAILS = "FAILS"
    INCOMPLETE = "INCOMPLETE"
    SURVEY_FIRST = "SURVEY_FIRST"


class RasterGrid(ContractModel):
    grid_id: str
    crs: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    transform: tuple[float, float, float, float, float, float]
    nodata: float | int | None
    value_kind: RasterValueKind
    cell_area_m2: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_transform(self) -> RasterGrid:
        if not self.crs:
            raise ValueError("grid CRS is required")
        if not all(math.isfinite(value) for value in self.transform):
            raise ValueError("grid transform must contain finite values")
        if self.transform[0] == 0 or self.transform[4] == 0:
            raise ValueError("grid pixel size cannot be zero")
        return self


def grid_alignment_identity(grid: RasterGrid) -> str:
    """Return the exact cell-geometry identity used by ``grid.grid_identity``.

    Value kind and NODATA remain raster semantics; they are intentionally not
    part of alignment identity. Keeping this primitive here avoids an import
    cycle while allowing cross-record contract validation to reject a shifted
    raster that merely reuses the same user-authored ``grid_id``.
    """

    return canonical_sha256(
        {
            "grid_id": grid.grid_id,
            "crs": grid.crs,
            "width": grid.width,
            "height": grid.height,
            "transform": grid.transform,
            "cell_area_m2": grid.cell_area_m2,
        }
    )


class SensitiveDataAuthorization(ContractModel):
    asset_id: str
    ownership: str
    license_spdx_or_name: str
    sensitivity: SensitivityClass
    cloud_disposition: CloudDisposition
    export_allowed: bool
    retention_policy: str
    spatial_redaction: str
    approved_deployments: list[Literal["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED", "LOCAL_ONLY"]]
    reviewer: str
    reviewed_at: AwareDatetime
    generalized_asset_id: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_cloud_policy(self) -> SensitiveDataAuthorization:
        if not self.approved_deployments:
            raise ValueError("at least one explicit deployment authorization is required")
        if len(self.approved_deployments) != len(set(self.approved_deployments)):
            raise ValueError("approved deployments must be unique")
        if (
            self.cloud_disposition == CloudDisposition.GENERALIZED_DERIVATIVE_ONLY
            and not self.generalized_asset_id
        ):
            raise ValueError("generalized-only assets require generalized_asset_id")
        if self.generalized_asset_id == self.asset_id:
            raise ValueError("a generalized derivative must be a different retained asset")
        if (
            self.cloud_disposition == CloudDisposition.DEDICATED_OR_SELF_MANAGED_ONLY
            and "WANDB_CLOUD" in self.approved_deployments
        ):
            raise ValueError("Dedicated/Self-Managed-only assets cannot authorize W&B Cloud")
        if self.cloud_disposition == CloudDisposition.PROHIBITED and any(
            item != "LOCAL_ONLY" for item in self.approved_deployments
        ):
            raise ValueError("prohibited assets may only authorize LOCAL_ONLY")
        return self


class ConnectivityAsset(ContractModel):
    asset_id: str
    kind: AssetKind
    title: str
    relative_path: str
    sha256: Sha256Digest
    byte_size: int = Field(ge=0)
    media_type: str
    source: str
    vintage: str
    license: str
    grid: RasterGrid | None = None
    authorization: SensitiveDataAuthorization

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value, "asset sha256")

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith(("/", "//"))
            or (len(normalized) >= 2 and normalized[1] == ":")
            or ".." in normalized.split("/")
            or "." in normalized.split("/")
        ):
            raise ValueError("asset paths must be contained relative paths")
        return normalized

    @model_validator(mode="after")
    def validate_authorization_identity(self) -> ConnectivityAsset:
        if self.authorization.asset_id != self.asset_id:
            raise ValueError("asset authorization must reference the same asset ID")
        if self.authorization.license_spdx_or_name != self.license:
            raise ValueError("asset license must match its reviewed authorization")
        return self


class ConnectivityDecisionBrief(ContractModel):
    brief_id: str
    title: str
    decision_question: str
    decision_owner: str
    intended_use: str
    target_guilds: list[str] = Field(min_length=1)
    safeguard_guilds: list[str] = Field(default_factory=list)
    budget_amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    cost_vintage: str | None = None
    non_goals: list[str] = Field(default_factory=list)
    required_next_action: str
    conceptual: bool = False


class FitnessDimension(ContractModel):
    key: Literal[
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
    ]
    status: Literal["ACCEPTED", "REVIEW_REQUIRED", "BLOCKING"]
    evidence_ids: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    notes: str


class ConnectivityModelFitness(ContractModel):
    passport_id: str
    revision_id: str
    intended_use: str
    dimensions: list[FitnessDimension]
    unresolved_assumptions: list[str] = Field(default_factory=list)
    attestations: list[str] = Field(default_factory=list)
    reviewed_by: str | None = None
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_each_dimension_once(self) -> ConnectivityModelFitness:
        keys = [item.key for item in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValueError("fitness dimensions must be unique")
        expected = {
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
        }
        if set(keys) != expected:
            missing = sorted(expected - set(keys))
            raise ValueError(f"fitness passport requires every dimension; missing={missing}")
        return self

    @property
    def has_blockers(self) -> bool:
        return any(item.status == "BLOCKING" for item in self.dimensions)


class ResistanceHypothesis(ContractModel):
    hypothesis_id: str
    title: str
    guild_id: str
    life_stage: str
    future_condition: str
    maturity_horizon: str
    resistance_asset_id: str
    grid_id: str
    rationale: str
    derivation: str
    evidence_citations: list[str] = Field(min_length=1)
    confidence: EvidenceConfidence
    reviewed_by: str
    reviewed_at: AwareDatetime


class FocalPair(ContractModel):
    pair_id: str
    from_focal_id: int = Field(gt=0)
    to_focal_id: int = Field(gt=0)
    required: bool = True

    @model_validator(mode="after")
    def distinct_and_canonical(self) -> FocalPair:
        if self.from_focal_id == self.to_focal_id:
            raise ValueError("a focal pair must contain two different focal IDs")
        if self.from_focal_id > self.to_focal_id:
            raise ValueError("focal pairs must use ascending focal IDs")
        return self


class FocalAreaSet(ContractModel):
    focal_set_id: str
    title: str
    asset_id: str
    grid_id: str
    guild_id: str
    focal_ids: list[int] = Field(min_length=2)
    pairs: list[FocalPair] = Field(min_length=1)
    rationale: str
    reviewed_by: str
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_pairs(self) -> FocalAreaSet:
        ids = set(self.focal_ids)
        if len(ids) != len(self.focal_ids) or any(item <= 0 for item in ids):
            raise ValueError("focal IDs must be unique positive integers")
        pair_ids = [item.pair_id for item in self.pairs]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("focal pair IDs must be unique")
        for pair in self.pairs:
            if pair.from_focal_id not in ids or pair.to_focal_id not in ids:
                raise ValueError("every pair must reference focal IDs in the set")
        return self


class ConnectivityScenario(ContractModel):
    scenario_id: str
    title: str
    guild_id: str
    life_stage: str
    resistance_hypothesis_id: str
    focal_set_id: str
    future_condition: str
    maturity_horizon: str
    included_pair_ids: list[str] = Field(min_length=1)
    safeguard: bool = False
    grid_id: str
    engine_mode: Literal[EngineMode.RASTER_PAIRWISE] = EngineMode.RASTER_PAIRWISE
    raster_semantics: Literal[RasterSemantics.RESISTANCE] = RasterSemantics.RESISTANCE
    neighbor_rule: NeighborRule = NeighborRule.EIGHT
    resistance_average: ResistanceAverage = ResistanceAverage.ARITHMETIC
    precision: Literal["double"] = "double"
    julia_threads: Literal[1] = 1
    blas_threads: Literal[1] = 1

    @model_validator(mode="after")
    def validate_supported_mode(self) -> ConnectivityScenario:
        if self.engine_mode != EngineMode.RASTER_PAIRWISE:
            raise ValueError("v0.1 supports only raster pairwise mode")
        if self.raster_semantics != RasterSemantics.RESISTANCE:
            raise ValueError("v0.1 supports resistance semantics only")
        if len(self.included_pair_ids) != len(set(self.included_pair_ids)):
            raise ValueError("included focal pair IDs must be unique")
        return self


class ReferenceMetricTolerance(ContractModel):
    """Declared comparison policy for one supported reference metric.

    Relative tolerance is a dimensionless fraction, not a percentage.  The
    approved reference value is deliberately the scale term so comparison
    behavior cannot vary with argument order or an implementation default.
    """

    metric: Literal["EFFECTIVE_RESISTANCE"] = "EFFECTIVE_RESISTANCE"
    units: Literal["effective_resistance"] = "effective_resistance"
    absolute_tolerance: float = Field(ge=0)
    relative_tolerance: float = Field(ge=0)
    comparison_formula: Literal[REFERENCE_COMPARISON_FORMULA] = REFERENCE_COMPARISON_FORMULA


class ReferenceScenarioBinding(ContractModel):
    """Bind approved reference bytes to one exact ecological scenario.

    A current raster may be retained for inspection, but v0.1 has no contract
    for proving equivalence between independently produced current rasters.
    The fixed disposition therefore prevents it from becoming validation
    evidence or influencing a matrix-comparison result.
    """

    binding_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    reference_matrix_asset_id: str = Field(min_length=1)
    matrix_format: Literal["CIRCUITSCAPE_THREE_COLUMN_MATRIX"] = "CIRCUITSCAPE_THREE_COLUMN_MATRIX"
    tolerance: ReferenceMetricTolerance
    reference_current_asset_id: str | None = None
    current_raster_disposition: Literal[
        "NOT_PROVIDED",
        REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION,
    ] = "NOT_PROVIDED"
    current_raster_numeric_comparison_allowed: Literal[False] = False
    reference_basis: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def current_attachment_is_diagnostic_only(self) -> ReferenceScenarioBinding:
        if self.reference_current_asset_id is None:
            if self.current_raster_disposition != "NOT_PROVIDED":
                raise ValueError(
                    "a missing reference current raster must use the NOT_PROVIDED disposition"
                )
        elif self.current_raster_disposition != REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION:
            raise ValueError(
                "a reference current raster is diagnostic-only and is not numerically compared"
            )
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("reference-binding limitations must be unique")
        return self


class ActionCellReplacement(ContractModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    resistance: float = Field(gt=0)


class ActionEffectBasis(ContractModel):
    basis_id: str
    purpose: str
    evidence_source: str
    citation: str
    reviewer: str
    reviewed_at: AwareDatetime
    confidence: EvidenceConfidence
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ActionEffectRaster(ContractModel):
    effect_id: str
    scenario_id: str
    grid_id: str
    footprint_asset_id: str
    cells: list[ActionCellReplacement] = Field(min_length=1)
    basis: ActionEffectBasis
    sha256: Sha256Digest

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value, "action effect sha256")

    @model_validator(mode="after")
    def cells_are_unique(self) -> ActionEffectRaster:
        coordinates = [(item.row, item.column) for item in self.cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("an action effect cannot replace a cell more than once")
        return self


class ActionEffectDraft(ContractModel):
    """Unhashed reviewed action effect accepted only by the preview RPC.

    The scientific worker canonicalizes cell order and computes the immutable
    :class:`ActionEffectRaster` digest.  A renderer-provided hash is therefore
    neither required nor accepted.
    """

    effect_id: str
    scenario_id: str
    grid_id: str
    footprint_asset_id: str
    cells: list[ActionCellReplacement] = Field(min_length=1)
    basis: ActionEffectBasis

    @model_validator(mode="after")
    def cells_are_unique(self) -> ActionEffectDraft:
        coordinates = [(item.row, item.column) for item in self.cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("an action effect cannot replace a cell more than once")
        return self


class InterventionOption(ContractModel):
    option_id: str
    site_id: str
    title: str
    kind: InterventionKind
    effects: list[ActionEffectRaster] = Field(default_factory=list)
    cost: float = Field(ge=0)
    currency: str
    cost_vintage: str
    dependencies: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    land_access: str
    maintenance: str
    external_feasibility_evidence: list[str] = Field(default_factory=list)
    unresolved_concerns: list[str] = Field(default_factory=list)
    reviewed_by: str
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def action_kind_matches_effects(self) -> InterventionOption:
        if self.kind in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}:
            if self.effects:
                raise ValueError(f"{self.kind.value} cannot contain modeled action effects")
        elif not self.effects:
            raise ValueError("a modeled intervention option requires scenario-specific effects")
        scenario_ids = [item.scenario_id for item in self.effects]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("an option may have at most one effect per scenario")
        return self


class ConnectivityCostBasis(ContractModel):
    cost_basis_id: str
    source: str
    currency: str
    vintage: str
    reviewer: str
    reviewed_at: AwareDatetime
    option_costs: dict[str, float] = Field(min_length=1)
    assumptions: list[str] = Field(min_length=1)

    @field_validator("option_costs")
    @classmethod
    def valid_option_costs(cls, value: dict[str, float]) -> dict[str, float]:
        if any(
            not option_id or not math.isfinite(cost) or cost < 0
            for option_id, cost in value.items()
        ):
            raise ValueError("cost-basis option costs must be finite and nonnegative")
        return value

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class InterventionOptionDraft(ContractModel):
    """Typed preview form of an intervention option with unhashed effects."""

    option_id: str
    site_id: str
    title: str
    kind: InterventionKind
    effects: list[ActionEffectDraft] = Field(default_factory=list)
    cost: float = Field(ge=0)
    currency: str
    cost_vintage: str
    dependencies: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    land_access: str
    maintenance: str
    external_feasibility_evidence: list[str] = Field(default_factory=list)
    unresolved_concerns: list[str] = Field(default_factory=list)
    reviewed_by: str
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def action_kind_matches_effects(self) -> InterventionOptionDraft:
        if self.kind in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}:
            if self.effects:
                raise ValueError(f"{self.kind.value} cannot contain modeled action effects")
        elif not self.effects:
            raise ValueError("a modeled intervention option requires scenario-specific effects")
        scenario_ids = [item.scenario_id for item in self.effects]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("an option may have at most one effect per scenario")
        return self


class InterventionSite(ContractModel):
    site_id: str
    title: str
    footprint_asset_id: str
    option_ids: list[str] = Field(min_length=1)
    known_constraints: list[str] = Field(default_factory=list)
    reviewed_by: str
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def unique_options(self) -> InterventionSite:
        if len(self.option_ids) != len(set(self.option_ids)):
            raise ValueError("site option IDs must be unique")
        return self


class SafeguardRecord(ContractModel):
    safeguard_id: str
    guild_id: str
    scenario_ids: list[str] = Field(min_length=1)
    nonregression_threshold_percent: float = Field(ge=0)
    concerns: list[str] = Field(default_factory=list)
    reviewed_by: str
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_scenarios(self) -> SafeguardRecord:
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("safeguard scenario IDs must be unique")
        return self


class RiskReviewMask(ContractModel):
    mask_id: str
    title: str
    asset_id: str
    grid_id: str
    review_trigger_percent: float = Field(ge=0)
    interpretation: str
    not_an_objective_notice: Literal[
        "Current redistribution is a diagnostic review signal, not predicted animal use."
    ] = "Current redistribution is a diagnostic review signal, not predicted animal use."


class PortfolioSelection(ContractModel):
    portfolio_id: str
    selections: dict[str, str]
    canonical_option_ids: tuple[str, ...]
    contains_survey_first: bool = False

    @model_validator(mode="after")
    def validate_canonical_shape(self) -> PortfolioSelection:
        if not self.selections:
            raise ValueError("a portfolio must select at least one site option")
        if len(self.canonical_option_ids) != len(self.selections):
            raise ValueError("canonical option IDs must contain one entry per selected site")
        if len(set(self.canonical_option_ids)) != len(self.canonical_option_ids):
            raise ValueError("canonical option IDs must be unique")
        return self


class ConnectivityDecisionRule(ContractModel):
    rule_id: str
    title: str
    metric: RuleMetric
    operator: RuleOperator
    threshold: float | bool
    scenario_ids: list[str] = Field(default_factory=list)
    pair_ids: list[str] = Field(default_factory=list)
    required: bool = True
    rationale: str

    @model_validator(mode="after")
    def validate_scope_ids(self) -> ConnectivityDecisionRule:
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("decision-rule scenario IDs must be unique")
        if len(self.pair_ids) != len(set(self.pair_ids)):
            raise ValueError("decision-rule pair IDs must be unique")
        return self


class ComputationalResourceObservation(ContractModel):
    """Auditable compute observation without implied carbon accounting.

    Wall-clock durations are measured with a monotonic process clock.  Memory
    and energy values are present only when an identified sampler or meter
    actually measured them, or when an explicit estimation method was used.
    A missing measurement is never represented as zero.
    """

    observation_id: str
    boundary: ResourceObservationBoundary
    solver_wall_seconds: float | None = Field(default=None, ge=0)
    proposer_wall_seconds: float | None = Field(default=None, ge=0)
    cache_lookup_wall_seconds: float | None = Field(default=None, ge=0)
    peak_memory_status: ResourceObservationStatus
    peak_memory_bytes: int | None = Field(default=None, ge=0)
    peak_memory_method: str
    energy_status: ResourceObservationStatus
    energy_kwh: float | None = Field(default=None, ge=0)
    energy_method: str
    peak_memory_sample_count: int = Field(default=0, ge=0)
    peak_memory_sampling_interval_seconds: float | None = Field(default=None, gt=0)
    peak_memory_is_lower_bound: bool = True
    energy_estimate_watts: float | None = Field(default=None, gt=0)
    energy_estimate_lower_watts: float | None = Field(default=None, gt=0)
    energy_estimate_upper_watts: float | None = Field(default=None, gt=0)
    energy_estimate_lower_kwh: float | None = Field(default=None, ge=0)
    energy_estimate_upper_kwh: float | None = Field(default=None, ge=0)
    hardware: str
    boundary_description: str
    assumptions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_measurement_semantics(self) -> ComputationalResourceObservation:
        timings = {
            ResourceObservationBoundary.DIRECT_SOLVER: self.solver_wall_seconds,
            ResourceObservationBoundary.SEARCH_PROPOSER: self.proposer_wall_seconds,
            ResourceObservationBoundary.CACHE_REUSE: self.cache_lookup_wall_seconds,
        }
        if timings[self.boundary] is None:
            raise ValueError("resource observation requires its boundary-specific wall time")
        if sum(value is not None for value in timings.values()) != 1:
            raise ValueError("resource observation may contain only one boundary wall time")
        if self.peak_memory_status == ResourceObservationStatus.NOT_MEASURED:
            if self.peak_memory_bytes is not None:
                raise ValueError("unmeasured peak memory cannot carry a byte value")
            if self.peak_memory_sample_count != 0:
                raise ValueError("unmeasured peak memory cannot carry sample observations")
        else:
            if self.peak_memory_bytes is None:
                raise ValueError("measured or estimated peak memory requires a byte value")
            if self.peak_memory_sample_count < 1:
                raise ValueError("observed peak memory requires at least one sample")
            if self.peak_memory_sampling_interval_seconds is None:
                raise ValueError("observed peak memory requires its sampling interval")
        estimate_fields = (
            self.energy_estimate_watts,
            self.energy_estimate_lower_watts,
            self.energy_estimate_upper_watts,
            self.energy_estimate_lower_kwh,
            self.energy_estimate_upper_kwh,
        )
        if self.energy_status == ResourceObservationStatus.NOT_MEASURED:
            if self.energy_kwh is not None:
                raise ValueError("unmeasured energy cannot carry a kWh value")
            if any(value is not None for value in estimate_fields):
                raise ValueError("unmeasured energy cannot carry estimate coefficients or bounds")
        elif self.energy_kwh is None:
            raise ValueError("measured or estimated energy requires a kWh value")
        if self.energy_status == ResourceObservationStatus.ESTIMATED:
            if any(value is None for value in estimate_fields):
                raise ValueError("estimated energy requires power coefficients and kWh bounds")
            assert self.energy_estimate_lower_watts is not None
            assert self.energy_estimate_watts is not None
            assert self.energy_estimate_upper_watts is not None
            assert self.energy_estimate_lower_kwh is not None
            assert self.energy_kwh is not None
            assert self.energy_estimate_upper_kwh is not None
            if not (
                self.energy_estimate_lower_watts
                <= self.energy_estimate_watts
                <= self.energy_estimate_upper_watts
                and self.energy_estimate_lower_kwh
                <= self.energy_kwh
                <= self.energy_estimate_upper_kwh
            ):
                raise ValueError("estimated energy coefficient and kWh bounds must be ordered")
            wall_seconds = timings[self.boundary]
            assert wall_seconds is not None
            divisor = 3_600_000.0
            expected_values = (
                wall_seconds * self.energy_estimate_lower_watts / divisor,
                wall_seconds * self.energy_estimate_watts / divisor,
                wall_seconds * self.energy_estimate_upper_watts / divisor,
            )
            observed_values = (
                self.energy_estimate_lower_kwh,
                self.energy_kwh,
                self.energy_estimate_upper_kwh,
            )
            if any(
                not math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-15)
                for observed, expected in zip(observed_values, expected_values, strict=True)
            ):
                raise ValueError("estimated energy must match wall time and power coefficients")
        elif self.energy_status == ResourceObservationStatus.MEASURED and any(
            value is not None for value in estimate_fields
        ):
            raise ValueError("measured energy cannot carry proxy estimate coefficients")
        if (
            not self.peak_memory_method
            or not self.energy_method
            or not self.hardware
            or not self.boundary_description
        ):
            raise ValueError(
                "resource observations require methods, boundary, and generalized hardware"
            )
        return self


class ConnectivityRunPlan(ContractModel):
    analysis_id: str
    revision_id: str
    scenario_ids: list[str] = Field(min_length=1)
    portfolios: list[PortfolioSelection] = Field(min_length=1)
    baseline_portfolio_id: str
    direct_job_count: int = Field(gt=0)
    solver_job_cap: int = Field(gt=0)
    search_method: Literal["ENUMERATION", "SOBOL", "SOBOL_EXTRATREES"]
    seed: int = Field(ge=0)
    current_maps_for_portfolio_ids: list[str] = Field(default_factory=list)
    engine_manifest_sha256: Sha256Digest
    search_resource_observation: ComputationalResourceObservation | None = None

    @field_validator("engine_manifest_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value, "engine manifest sha256")

    @model_validator(mode="after")
    def validate_job_arithmetic(self) -> ConnectivityRunPlan:
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("run-plan scenario IDs must be unique")
        portfolio_ids = [item.portfolio_id for item in self.portfolios]
        if len(portfolio_ids) != len(set(portfolio_ids)):
            raise ValueError("run-plan portfolio IDs must be unique")
        expected = len(self.scenario_ids) * len(self.portfolios)
        if self.direct_job_count != expected:
            raise ValueError(f"direct_job_count must equal scenarios × portfolios ({expected})")
        if self.solver_job_cap < self.direct_job_count:
            raise ValueError("solver job cap is smaller than the planned direct-job count")
        if self.baseline_portfolio_id not in {item.portfolio_id for item in self.portfolios}:
            raise ValueError("baseline portfolio must appear in the plan")
        unknown_map_portfolios = set(self.current_maps_for_portfolio_ids) - set(portfolio_ids)
        if unknown_map_portfolios:
            raise ValueError("current-map requests must reference planned portfolios")
        if len(self.current_maps_for_portfolio_ids) != len(
            set(self.current_maps_for_portfolio_ids)
        ):
            raise ValueError("current-map portfolio IDs must be unique")
        if (
            self.search_resource_observation is not None
            and self.search_resource_observation.boundary
            != ResourceObservationBoundary.SEARCH_PROPOSER
        ):
            raise ValueError("run-plan resource observation must describe search proposal")
        return self


class ReferencePairComparison(ContractModel):
    """One auditable effective-resistance comparison against approved bytes."""

    pair_id: str = Field(min_length=1)
    reference_effective_resistance: float = Field(gt=0)
    observed_effective_resistance: float = Field(gt=0)
    tolerance: ReferenceMetricTolerance
    absolute_difference: float = Field(ge=0)
    relative_difference: float = Field(ge=0)
    allowed_difference: float = Field(ge=0)
    within_tolerance: bool

    @model_validator(mode="after")
    def differences_are_derived_from_values(self) -> ReferencePairComparison:
        absolute = abs(self.observed_effective_resistance - self.reference_effective_resistance)
        relative = absolute / abs(self.reference_effective_resistance)
        allowed = self.tolerance.absolute_tolerance + (
            self.tolerance.relative_tolerance * abs(self.reference_effective_resistance)
        )
        for label, supplied, expected in (
            ("absolute difference", self.absolute_difference, absolute),
            ("relative difference", self.relative_difference, relative),
            ("allowed difference", self.allowed_difference, allowed),
        ):
            if not math.isclose(supplied, expected, rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError(f"reference {label} is not derived from the compared values")
        if self.within_tolerance != (absolute <= allowed):
            raise ValueError("within_tolerance must follow the declared comparison formula")
        return self


class ReferenceScenarioComparison(ContractModel):
    """Complete direct comparison for one scenario and every bound focal pair."""

    comparison_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    model_sha256: Sha256Digest
    binding_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    direct_run_id: str = Field(min_length=1)
    reference_matrix_asset_id: str = Field(min_length=1)
    reference_matrix_sha256: Sha256Digest
    observed_matrix_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    tolerance: ReferenceMetricTolerance
    pair_comparisons: list[ReferencePairComparison] = Field(min_length=1)
    qa_status: Literal[QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED]
    status: Literal["PASSED", "FAILED"]
    reference_current_asset_id: str | None = None
    reference_current_sha256: Sha256Digest | None = None
    current_raster_disposition: Literal[
        "NOT_PROVIDED",
        REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION,
    ] = "NOT_PROVIDED"
    current_raster_numeric_comparison_performed: Literal[False] = False
    exact_direct_evidence_only: Literal[True] = True
    completed_at: AwareDatetime
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def comparison_is_complete_and_derived(self) -> ReferenceScenarioComparison:
        pair_ids = [item.pair_id for item in self.pair_comparisons]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("reference comparison focal-pair IDs must be unique")
        if any(item.tolerance != self.tolerance for item in self.pair_comparisons):
            raise ValueError("every pair comparison must use the scenario tolerance")
        expected_status = (
            "PASSED" if all(item.within_tolerance for item in self.pair_comparisons) else "FAILED"
        )
        if self.status != expected_status:
            raise ValueError("reference scenario status must derive from every pair comparison")
        if self.reference_current_asset_id is None:
            if self.reference_current_sha256 is not None:
                raise ValueError("an absent reference current raster cannot carry a digest")
            if self.current_raster_disposition != "NOT_PROVIDED":
                raise ValueError(
                    "a missing reference current raster must use the NOT_PROVIDED disposition"
                )
        elif (
            self.reference_current_sha256 is None
            or self.current_raster_disposition != REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION
        ):
            raise ValueError(
                "a retained reference current raster requires a digest and diagnostic-only disposition"
            )
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("reference-comparison limitations must be unique")
        required_limitations = {CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION}
        if not required_limitations.issubset(self.limitations):
            raise ValueError(
                "reference comparisons require the current-raster and model-fitness limitations"
            )
        return self


class ReferenceComparisonPlan(ContractModel):
    """Immutable baseline-only direct-run plan for supplied reference matrices."""

    plan_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    model_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    binding_ids: list[str] = Field(min_length=1)
    bound_scenario_ids: list[str] = Field(min_length=1)
    run_plan: ConnectivityRunPlan
    exact_direct_evidence_only: Literal[True] = True
    created_at: AwareDatetime

    @model_validator(mode="after")
    def plan_is_one_complete_baseline_ensemble(self) -> ReferenceComparisonPlan:
        if len(self.binding_ids) != len(set(self.binding_ids)):
            raise ValueError("reference-comparison binding IDs must be unique")
        if len(self.bound_scenario_ids) != len(set(self.bound_scenario_ids)):
            raise ValueError("reference-comparison scenario IDs must be unique")
        if len(self.binding_ids) != len(self.bound_scenario_ids):
            raise ValueError("reference comparison requires one binding per bound scenario")
        if not set(self.bound_scenario_ids).issubset(self.run_plan.scenario_ids):
            raise ValueError("bound reference scenarios must be included in the direct-run plan")
        if (
            self.analysis_id != self.run_plan.analysis_id
            or self.revision_id != self.run_plan.revision_id
            or self.engine_manifest_sha256 != self.run_plan.engine_manifest_sha256
        ):
            raise ValueError("reference plan identity must match its direct-run plan")
        if (
            len(self.run_plan.portfolios) != 1
            or self.run_plan.portfolios[0].portfolio_id != self.run_plan.baseline_portfolio_id
        ):
            raise ValueError("reference comparison requires exactly one baseline portfolio")
        if self.run_plan.current_maps_for_portfolio_ids:
            raise ValueError("reference comparison v0.1 does not request current-map equivalence")
        if self.run_plan.search_method != "ENUMERATION":
            raise ValueError("reference comparison is a direct baseline run, not a search")
        return self


class ReferenceEvidenceClaimLink(ContractModel):
    """Bind one reference claim to its exact scenario, pair, comparison, and run."""

    claim_id: str = Field(min_length=1)
    comparison_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    direct_run_id: str = Field(min_length=1)


class ReferenceComparisonResult(ContractModel):
    """Revision-bound outcome derived only from direct matrix comparisons."""

    result_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    model_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    scenario_comparisons: list[ReferenceScenarioComparison] = Field(min_length=1)
    status: Literal["PASSED", "FAILED"]
    exact_direct_evidence_only: Literal[True] = True
    current_raster_numeric_comparison_performed: Literal[False] = False
    evidence_claim_links: list[ReferenceEvidenceClaimLink] = Field(min_length=1)
    completed_at: AwareDatetime
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def result_is_complete_and_derived(self) -> ReferenceComparisonResult:
        for label, values in (
            ("scenario", [item.scenario_id for item in self.scenario_comparisons]),
            ("binding", [item.binding_id for item in self.scenario_comparisons]),
            ("comparison", [item.comparison_id for item in self.scenario_comparisons]),
            ("direct run", [item.direct_run_id for item in self.scenario_comparisons]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"reference-result {label} identities must be unique")
        claim_ids = [item.claim_id for item in self.evidence_claim_links]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("reference-result evidence claim IDs must be unique")
        expected_links = {
            (
                comparison.comparison_id,
                comparison.scenario_id,
                pair.pair_id,
                comparison.direct_run_id,
            )
            for comparison in self.scenario_comparisons
            for pair in comparison.pair_comparisons
        }
        actual_links = {
            (
                item.comparison_id,
                item.scenario_id,
                item.pair_id,
                item.direct_run_id,
            )
            for item in self.evidence_claim_links
        }
        if len(actual_links) != len(self.evidence_claim_links):
            raise ValueError("reference-result scenario-pair claim links must be unique")
        if actual_links != expected_links:
            raise ValueError(
                "reference results require exactly one evidence claim link for every scenario pair"
            )
        if any(
            item.analysis_id != self.analysis_id
            or item.revision_id != self.revision_id
            or item.model_sha256 != self.model_sha256
            or item.engine_manifest_sha256 != self.engine_manifest_sha256
            for item in self.scenario_comparisons
        ):
            raise ValueError("reference scenario results must share the result identity")
        expected_status = (
            "PASSED"
            if all(item.status == "PASSED" for item in self.scenario_comparisons)
            else "FAILED"
        )
        if self.status != expected_status:
            raise ValueError("reference result status must derive from every scenario result")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("reference-result limitations must be unique")
        required_limitations = {CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION}
        if not required_limitations.issubset(self.limitations):
            raise ValueError(
                "reference results require the current-raster and model-fitness limitations"
            )
        return self


class PairResistanceMetric(ContractModel):
    pair_id: str
    effective_resistance: float = Field(gt=0)
    baseline_effective_resistance: float | None = Field(default=None, gt=0)
    baseline_relative_change_percent: float | None = None


class DiagnosticMapSummary(ContractModel):
    mask_id: str
    baseline_total_current: float = Field(ge=0)
    portfolio_total_current: float = Field(ge=0)
    relative_change_percent: float | None
    review_triggered: bool
    reviewed_cell_count: int | None = Field(default=None, ge=1)
    included_cell_count: int | None = Field(default=None, ge=1)
    excluded_nodata_cell_count: int | None = Field(default=None, ge=0)
    raster_nodata_cell_count: int | None = Field(default=None, ge=0)
    nodata_topology_sha256: Sha256Digest | None = None
    interpretation: str = "Diagnostic current redistribution; not predicted animal use."


class DirectConnectivityRun(ContractModel):
    direct_run_id: str
    analysis_id: str
    revision_id: str
    portfolio_id: str
    scenario_id: str
    state: DirectRunState
    exact: bool
    engine_name: str
    engine_manifest_sha256: Sha256Digest
    model_run_cache_key: Sha256Digest
    input_sha256: Sha256Digest
    result_sha256: Sha256Digest | None = None
    matrix_sha256: Sha256Digest | None = None
    current_map_sha256: Sha256Digest | None = None
    cache_source_direct_run_id: str | None = None
    pair_metrics: list[PairResistanceMetric] = Field(default_factory=list)
    diagnostic_maps: list[DiagnosticMapSummary] = Field(default_factory=list)
    qa_status: QAStatus | None = None
    qa_findings: list[str] = Field(default_factory=list)
    resource_observation: ComputationalResourceObservation | None = None
    completed_at: AwareDatetime | None = None
    test_only: bool = False

    @field_validator(
        "engine_manifest_sha256",
        "model_run_cache_key",
        "input_sha256",
        "result_sha256",
        "matrix_sha256",
        "current_map_sha256",
    )
    @classmethod
    def validate_digests(cls, value: str | None) -> str | None:
        return None if value is None else _check_sha256(value)

    @model_validator(mode="after")
    def successful_runs_are_complete(self) -> DirectConnectivityRun:
        if self.state == DirectRunState.SUCCEEDED:
            if not self.exact or self.test_only:
                raise ValueError("SUCCEEDED release runs must be exact, non-test engine results")
            if self.engine_name != "Circuitscape":
                raise ValueError("release connectivity results must come from Circuitscape")
            if not self.result_sha256 or not self.matrix_sha256 or not self.completed_at:
                raise ValueError(
                    "a successful run requires immutable result artifacts and completion time"
                )
            if not self.pair_metrics:
                raise ValueError("a successful run requires focal-pair metrics")
            if self.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}:
                raise ValueError("a successful run requires accepted QA")
            pair_ids = [item.pair_id for item in self.pair_metrics]
            if len(pair_ids) != len(set(pair_ids)):
                raise ValueError("a successful run cannot contain duplicate focal-pair metrics")
        if self.cache_source_direct_run_id == self.direct_run_id:
            raise ValueError("a cached run must name a distinct source direct execution")
        return self


class RuleDisposition(ContractModel):
    rule_id: str
    scenario_id: str | None = None
    pair_id: str | None = None
    observed_value: float | bool | None = None
    passed: bool
    evidence_claim_id: str | None = None
    explanation: str


class PortfolioDecisionSummary(ContractModel):
    portfolio_id: str
    disposition: PortfolioDisposition
    cost: float = Field(ge=0)
    currency: str
    rule_dispositions: list[RuleDisposition]
    controlling_scenario_id: str | None = None
    controlling_pair_id: str | None = None
    worst_resistance_reduction_percent: float | None = None
    safeguard_passed: bool | None = None
    review_mask_triggered: bool = False
    unresolved_feasibility: list[str] = Field(default_factory=list)
    next_action: str
    direct_run_ids: list[str] = Field(default_factory=list)
    disposition_evidence_claim_id: str | None = None

    @model_validator(mode="after")
    def validate_direct_run_ids(self) -> PortfolioDecisionSummary:
        if len(self.direct_run_ids) != len(set(self.direct_run_ids)):
            raise ValueError("portfolio direct-run IDs must be unique")
        return self


class EvidenceNode(ContractModel):
    node_id: str
    node_type: Literal[
        "CLAIM",
        "REFERENCE_COMPARISON",
        "RULE",
        "METRIC",
        "DIRECT_RUN",
        "SCENARIO",
        "RASTER_DIFF",
        "ASSET",
        "ENGINE_MANIFEST",
        "QA",
        "ASSUMPTION",
        "AUTHORIZATION",
        "ARTIFACT",
        "CACHE_LINEAGE",
        "COST_BASIS",
        "PORTFOLIO",
        "OPTION",
        "REDUCER",
        "FEASIBILITY",
        "DISPOSITION",
        "CURRENT_MAP",
        "FOOTPRINT_PROOF",
        "MODEL_REVISION",
        "RUN_PLAN",
        "SUPPORTING_CLAIM",
    ]
    label: str
    sha256: Sha256Digest | None = None
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        return None if value is None else _check_sha256(value)


class EvidenceEdge(ContractModel):
    from_node_id: str
    to_node_id: str
    relation: str


class ConnectivityEvidenceClaim(ContractModel):
    claim_id: str
    claim_kind: Literal[
        "DIRECT_PAIR_METRIC",
        "DIRECT_REFERENCE_COMPARISON",
        "DIRECT_MAP_DIAGNOSTIC",
        "DIRECT_QA",
        "COST",
        "FEASIBILITY",
        "AGGREGATE_RULE",
        "PORTFOLIO_DISPOSITION",
    ] = "DIRECT_PAIR_METRIC"
    text: str
    value: float | bool | str
    units: str
    portfolio_id: str
    scenario_id: str | None = None
    pair_id: str | None = None
    rule_id: str | None = None
    direct_run_ids: list[str] = Field(default_factory=list)
    root_node_id: str
    nodes: list[EvidenceNode] = Field(min_length=1)
    edges: list[EvidenceEdge] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_graph_shape(self) -> ConnectivityEvidenceClaim:
        if len(self.direct_run_ids) != len(set(self.direct_run_ids)):
            raise ValueError("evidence direct-run IDs must be unique")
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("evidence node IDs must be unique")
        known = set(node_ids)
        if self.root_node_id not in known:
            raise ValueError("evidence root node must exist in the graph")
        if any(
            edge.from_node_id not in known or edge.to_node_id not in known for edge in self.edges
        ):
            raise ValueError("every evidence edge must reference existing nodes")
        if self.claim_kind.startswith("DIRECT_") and not self.direct_run_ids:
            raise ValueError("direct evidence claims require at least one direct-run ID")
        if self.claim_kind == "DIRECT_REFERENCE_COMPARISON" and (
            self.scenario_id is None or self.pair_id is None or len(self.direct_run_ids) != 1
        ):
            raise ValueError(
                "direct reference-comparison claims require one scenario, pair, and direct run"
            )
        if self.claim_kind == "DIRECT_REFERENCE_COMPARISON" and (
            type(self.value) is not bool or self.units != "within_tolerance"
        ):
            raise ValueError(
                "direct reference-comparison claims require a Boolean within_tolerance value"
            )
        if self.claim_kind == "COST" and self.direct_run_ids:
            raise ValueError("cost evidence is decision evidence, not direct solver evidence")
        if self.claim_kind == "FEASIBILITY" and self.direct_run_ids:
            raise ValueError("feasibility evidence is not direct solver evidence")
        return self


class GridAlignmentPlan(ContractModel):
    plan_id: str
    source_asset_id: str
    source_sha256: Sha256Digest
    source_grid: RasterGrid
    target_grid: RasterGrid
    method: ResamplingMethod
    source_cell_count: int = Field(gt=0)
    target_cell_count: int = Field(gt=0)
    source_nodata_count: int = Field(ge=0)
    estimated_target_nodata_count: int = Field(ge=0)
    categorical_counts_before: dict[str, int] = Field(default_factory=dict)
    categorical_counts_after: dict[str, int] = Field(default_factory=dict)
    resulting_sha256: Sha256Digest
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_sha256", "resulting_sha256")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _check_sha256(value)

    @model_validator(mode="after")
    def validate_method(self) -> GridAlignmentPlan:
        if self.source_cell_count != self.source_grid.width * self.source_grid.height:
            raise ValueError("source cell count does not match source grid")
        if self.target_cell_count != self.target_grid.width * self.target_grid.height:
            raise ValueError("target cell count does not match target grid")
        if (
            self.source_grid.value_kind in {RasterValueKind.IDENTIFIER, RasterValueKind.MASK}
            and self.method != ResamplingMethod.NEAREST
        ):
            raise ValueError("identifier and mask grids require nearest-neighbor alignment")
        if (
            self.source_grid.value_kind == RasterValueKind.CATEGORICAL
            and self.method != ResamplingMethod.NEAREST
        ):
            raise ValueError("categorical grids require nearest-neighbor alignment")
        if self.method == ResamplingMethod.NONE and self.source_grid != self.target_grid:
            raise ValueError("NONE is valid only when source and target grids are identical")
        return self


class GridAlignmentApproval(ContractModel):
    approval_id: str
    plan_id: str
    plan_sha256: Sha256Digest
    reviewer: str
    approved_at: AwareDatetime
    acknowledged_warnings: list[str]
    purpose: str

    @field_validator("plan_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value)


class EngineManifest(ContractModel):
    manifest_id: str
    engine_name: Literal["Circuitscape"] = "Circuitscape"
    julia_version: Literal["1.12.6"] = "1.12.6"
    circuitscape_version: Literal["5.17.1"] = "5.17.1"
    executable_sha256: Sha256Digest
    julia_runtime_sha256: Sha256Digest
    julia_manifest_sha256: Sha256Digest
    native_artifacts_sha256: Sha256Digest
    wrapper_sha256: Sha256Digest
    parser_sha256: Sha256Digest
    metric_definitions_sha256: Sha256Digest
    precision: Literal["double"] = "double"
    neighbor_rule: NeighborRule
    resistance_average: ResistanceAverage
    julia_threads: Literal[1] = 1
    blas_threads: Literal[1] = 1
    omp_threads: Literal[1] = 1
    platform: str
    created_at: AwareDatetime

    @field_validator(
        "julia_runtime_sha256",
        "executable_sha256",
        "julia_manifest_sha256",
        "native_artifacts_sha256",
        "wrapper_sha256",
        "parser_sha256",
        "metric_definitions_sha256",
    )
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _check_sha256(value)

    @property
    def sha256(self) -> str:
        # ``created_at`` is audit metadata, not part of the executable engine
        # identity. Excluding it keeps identical verified bundles cache-stable
        # across packaging invocations while retaining the timestamp in the
        # public manifest.
        return canonical_sha256(self.model_dump(mode="json", exclude={"created_at"}))


class ResourcePolicy(ContractModel):
    max_input_bytes: int = Field(gt=0)
    max_materialized_bytes: int = Field(gt=0)
    max_current_raster_bytes: int = Field(gt=0)
    max_direct_jobs: int = Field(gt=0)
    max_wall_seconds_per_job: int = Field(gt=0)
    # Professional cloud synchronization is deliberately fail-closed.  This is
    # a contract invariant, not a user-configurable preference: callers cannot
    # disable exact-plan consent by sending ``false`` at an RPC boundary.
    require_explicit_oversize_approval: Literal[True] = True


OVERSIZE_SYNC_ACKNOWLEDGEMENT = (
    "I approve synchronization of the exact oversized evidence plan identified by this digest."
)
APPROVAL_AUDIT_RESERVATION_BYTES_PER_PROJECTION = 16 * 1024
OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION = 64 * 1024


class OversizeSyncApproval(ContractModel):
    """Immutable, exact-plan consent for one oversized cloud projection.

    The approval intentionally repeats the important identities behind the
    plan digest.  This makes an audit understandable without trusting a free-
    form W&B binding field, while the digest prevents reuse after any model,
    policy, asset, estimate, or destination change.
    """

    approval_id: str
    project_id: str
    binding_id: str
    destination: Literal["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]
    entity: str
    project: str
    external_project_id: str
    model_revision_id: str
    model_sha256: Sha256Digest
    bundle_sha256: Sha256Digest
    resource_policy_sha256: Sha256Digest
    projection_sha256: Sha256Digest
    current_raster_estimate_sha256: Sha256Digest
    scope_evidence_sha256: Sha256Digest
    telemetry_inventory_sha256: Sha256Digest
    sync_plan_sha256: Sha256Digest
    approved_input_bytes: int = Field(ge=0)
    approved_current_raster_bytes: int = Field(ge=0)
    approved_pending_current_raster_bytes: int = Field(ge=0)
    approved_telemetry_projected_bytes: int = Field(ge=0)
    approved_telemetry_artifact_count: int = Field(ge=0)
    approved_approval_audit_reservation_bytes: int = Field(ge=0)
    approved_approval_audit_reservation_count: int = Field(ge=0)
    approved_operational_metadata_reservation_bytes: int = Field(ge=0)
    approved_operational_metadata_reservation_count: int = Field(ge=0)
    approved_total_bytes: int = Field(gt=0)
    reviewed_by: str = Field(min_length=1, max_length=256)
    approved_at: AwareDatetime
    acknowledgement: Literal[OVERSIZE_SYNC_ACKNOWLEDGEMENT] = OVERSIZE_SYNC_ACKNOWLEDGEMENT

    @model_validator(mode="after")
    def validate_exact_plan_identity(self) -> OversizeSyncApproval:
        expected_id = f"oversize_approval_{self.sync_plan_sha256}"
        if self.approval_id != expected_id:
            raise ValueError("oversize approval ID must derive from the exact sync-plan digest")
        if any(
            not value.strip()
            for value in (
                self.project_id,
                self.binding_id,
                self.entity,
                self.project,
                self.external_project_id,
                self.model_revision_id,
                self.reviewed_by,
            )
        ):
            raise ValueError("oversize approval identities and reviewer are required")
        # Retained current rasters are exact telemetry artifacts.  Only the
        # still-pending estimate is added separately, avoiding double counting.
        if self.approved_total_bytes != (
            self.approved_input_bytes
            + self.approved_telemetry_projected_bytes
            + self.approved_pending_current_raster_bytes
        ):
            raise ValueError("oversize approval byte totals must match the approved plan")
        for label, byte_size, count in (
            (
                "approval audit",
                self.approved_approval_audit_reservation_bytes,
                self.approved_approval_audit_reservation_count,
            ),
            (
                "operational metadata",
                self.approved_operational_metadata_reservation_bytes,
                self.approved_operational_metadata_reservation_count,
            ),
        ):
            if (byte_size == 0) != (count == 0):
                raise ValueError(f"{label} reservation bytes and count must agree")
        if self.approved_approval_audit_reservation_bytes != (
            self.approved_approval_audit_reservation_count
            * APPROVAL_AUDIT_RESERVATION_BYTES_PER_PROJECTION
        ):
            raise ValueError("approval audit reservation does not match its versioned bound")
        if self.approved_operational_metadata_reservation_bytes != (
            self.approved_operational_metadata_reservation_count
            * OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION
        ):
            raise ValueError("operational metadata reservation does not match its versioned bound")
        if (
            self.approved_approval_audit_reservation_bytes
            + self.approved_operational_metadata_reservation_bytes
            > self.approved_telemetry_projected_bytes
        ):
            raise ValueError("telemetry reservations exceed approved telemetry bytes")
        return self


class ConnectivityExportEligibility(ContractModel):
    eligible: bool
    professional: bool
    complete_ensemble: bool
    exact_direct_evidence_only: bool
    fitness_blockers: list[str] = Field(default_factory=list)
    authorization_blockers: list[str] = Field(default_factory=list)
    qa_blockers: list[str] = Field(default_factory=list)
    sync_state: Literal["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE", "FAILED_RETRYING", "BLOCKED"]
    required_notice: Literal[CONCEPTUAL_CONNECTIVITY_NOTICE] = CONCEPTUAL_CONNECTIVITY_NOTICE

    @model_validator(mode="after")
    def eligibility_is_evidence_derived(self) -> ConnectivityExportEligibility:
        should_be_eligible = (
            self.complete_ensemble
            and self.exact_direct_evidence_only
            and not self.fitness_blockers
            and not self.authorization_blockers
            and not self.qa_blockers
        )
        if self.eligible != should_be_eligible:
            raise ValueError("eligible must be derived from evidence blockers")
        return self


class SemanticChange(ContractModel):
    object_type: str
    object_id: str
    field: str
    before: str | int | float | bool | None
    after: str | int | float | bool | None
    rationale: str


class SemanticModelDiff(ContractModel):
    diff_id: str
    from_revision_id: str
    to_revision_id: str
    changes: list[SemanticChange] = Field(min_length=1)
    sha256: Sha256Digest

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value)


class ModelRevision(ContractModel):
    revision_id: str
    parent_revision_id: str | None = None
    project_id: str
    created_at: AwareDatetime
    created_by: str
    reason: str
    model_sha256: Sha256Digest
    diff_id: str | None = None

    @field_validator("model_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _check_sha256(value)


class ConnectivityProjectBundle(ContractModel):
    schema_version: Literal["1.0"] = ECOCORRIDOR_SCHEMA_VERSION
    project_id: str
    title: str
    revision: ModelRevision
    decision_brief: ConnectivityDecisionBrief
    assets: list[ConnectivityAsset]
    resistance_hypotheses: list[ResistanceHypothesis]
    focal_area_sets: list[FocalAreaSet]
    scenarios: list[ConnectivityScenario]
    # Empty optional bindings serialize as absent so adding this v1.0 extension
    # does not invalidate previously pinned synthetic evidence. Any real
    # binding remains explicit in the portable bundle and its hashes.
    reference_bindings: list[ReferenceScenarioBinding] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    intervention_sites: list[InterventionSite]
    intervention_options: list[InterventionOption]
    cost_basis: ConnectivityCostBasis | None = None
    safeguards: list[SafeguardRecord] = Field(default_factory=list)
    review_masks: list[RiskReviewMask] = Field(default_factory=list)
    decision_rules: list[ConnectivityDecisionRule] = Field(default_factory=list)
    fitness: ConnectivityModelFitness
    resource_policy: ResourcePolicy

    @model_validator(mode="after")
    def validate_references(self) -> ConnectivityProjectBundle:
        if self.revision.project_id != self.project_id:
            raise ValueError("revision must belong to the project")
        assets_by_id = {item.asset_id: item for item in self.assets}
        asset_ids = set(assets_by_id)
        if len(assets_by_id) != len(self.assets):
            raise ValueError("asset IDs must be unique")
        scenarios_by_id = {item.scenario_id: item for item in self.scenarios}
        scenario_ids = set(scenarios_by_id)
        if len(scenarios_by_id) != len(self.scenarios):
            raise ValueError("scenario IDs must be unique")
        hypotheses_by_id = {item.hypothesis_id: item for item in self.resistance_hypotheses}
        hypothesis_ids = set(hypotheses_by_id)
        focal_ids = {item.focal_set_id for item in self.focal_area_sets}
        option_by_id = {item.option_id: item for item in self.intervention_options}
        if len(option_by_id) != len(self.intervention_options):
            raise ValueError("intervention option IDs must be unique")
        for item in self.resistance_hypotheses:
            if item.resistance_asset_id not in asset_ids:
                raise ValueError(f"hypothesis {item.hypothesis_id} references a missing asset")
        for item in self.focal_area_sets:
            if item.asset_id not in asset_ids:
                raise ValueError(f"focal set {item.focal_set_id} references a missing asset")
        for scenario in self.scenarios:
            if scenario.resistance_hypothesis_id not in hypothesis_ids:
                raise ValueError(f"scenario {scenario.scenario_id} references a missing hypothesis")
            if scenario.focal_set_id not in focal_ids:
                raise ValueError(f"scenario {scenario.scenario_id} references a missing focal set")
        binding_ids = [item.binding_id for item in self.reference_bindings]
        bound_scenario_ids = [item.scenario_id for item in self.reference_bindings]
        matrix_asset_ids = [item.reference_matrix_asset_id for item in self.reference_bindings]
        current_asset_ids = [
            item.reference_current_asset_id
            for item in self.reference_bindings
            if item.reference_current_asset_id is not None
        ]
        for label, values in (
            ("reference binding", binding_ids),
            ("reference-bound scenario", bound_scenario_ids),
            ("reference matrix asset", matrix_asset_ids),
            ("reference current asset", current_asset_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} identities must be unique")
        for binding in self.reference_bindings:
            scenario = scenarios_by_id.get(binding.scenario_id)
            if scenario is None:
                raise ValueError(
                    f"reference binding {binding.binding_id} references a missing scenario"
                )
            matrix_asset = assets_by_id.get(binding.reference_matrix_asset_id)
            if matrix_asset is None or matrix_asset.kind != AssetKind.REFERENCE_MATRIX:
                raise ValueError(
                    f"reference binding {binding.binding_id} requires a reference matrix asset"
                )
            if binding.reference_current_asset_id is None:
                continue
            current_asset = assets_by_id.get(binding.reference_current_asset_id)
            if current_asset is None or current_asset.kind != AssetKind.REFERENCE_CURRENT:
                raise ValueError(
                    f"reference binding {binding.binding_id} requires a reference current asset"
                )
            hypothesis = hypotheses_by_id[scenario.resistance_hypothesis_id]
            scenario_resistance_asset = assets_by_id[hypothesis.resistance_asset_id]
            if (
                current_asset.grid is None
                or current_asset.grid.value_kind != RasterValueKind.CONTINUOUS
                or scenario_resistance_asset.grid is None
                or grid_alignment_identity(current_asset.grid)
                != grid_alignment_identity(scenario_resistance_asset.grid)
            ):
                raise ValueError(
                    f"reference current asset {current_asset.asset_id} must use the scenario's "
                    "exact reviewed continuous grid alignment"
                )
        for site in self.intervention_sites:
            for option_id in site.option_ids:
                option = option_by_id.get(option_id)
                if option is None or option.site_id != site.site_id:
                    raise ValueError(f"site {site.site_id} has an invalid option reference")
        for option in self.intervention_options:
            effect_scenarios = {item.scenario_id for item in option.effects}
            if (
                option.kind not in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}
                and effect_scenarios != scenario_ids
            ):
                raise ValueError(
                    f"modeled option {option.option_id} must include one reviewed effect "
                    "for every required scenario"
                )
            for effect in option.effects:
                if effect.scenario_id not in scenario_ids:
                    raise ValueError(f"effect {effect.effect_id} references a missing scenario")
                if effect.footprint_asset_id not in asset_ids:
                    raise ValueError(f"effect {effect.effect_id} references a missing footprint")
        if self.cost_basis is not None:
            expected_costs = {item.option_id: item.cost for item in self.intervention_options}
            if self.cost_basis.option_costs != expected_costs:
                raise ValueError("cost basis must exactly cover reviewed intervention options")
            if any(item.currency != self.cost_basis.currency for item in self.intervention_options):
                raise ValueError("option currencies differ from the reviewed cost basis")
        return self


PUBLIC_CONTRACTS: tuple[type[ContractModel], ...] = (
    ConnectivityProjectBundle,
    ConnectivityDecisionBrief,
    ConnectivityModelFitness,
    ResistanceHypothesis,
    FocalAreaSet,
    ConnectivityScenario,
    ReferenceMetricTolerance,
    ReferenceScenarioBinding,
    InterventionSite,
    InterventionOption,
    ConnectivityCostBasis,
    ActionEffectRaster,
    ActionEffectBasis,
    SafeguardRecord,
    RiskReviewMask,
    ConnectivityDecisionRule,
    ConnectivityRunPlan,
    ReferencePairComparison,
    ReferenceScenarioComparison,
    ReferenceComparisonPlan,
    ReferenceEvidenceClaimLink,
    ReferenceComparisonResult,
    DirectConnectivityRun,
    PortfolioDecisionSummary,
    ConnectivityEvidenceClaim,
    GridAlignmentPlan,
    GridAlignmentApproval,
    SensitiveDataAuthorization,
    EngineManifest,
    ResourcePolicy,
    OversizeSyncApproval,
    ConnectivityExportEligibility,
    ModelRevision,
    SemanticModelDiff,
)


def public_schema_index() -> dict[str, Any]:
    return {
        "schema_version": ECOCORRIDOR_SCHEMA_VERSION,
        "contracts": [
            {"name": contract.__name__, "file": f"{contract.__name__}.schema.json"}
            for contract in PUBLIC_CONTRACTS
        ],
    }
