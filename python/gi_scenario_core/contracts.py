"""Versioned public contracts.

Pydantic v2 is used when installed.  A small compatibility implementation keeps
the CLI, fixtures, and tests functional in constrained/offline environments.  It
implements the Pydantic methods used by this project (`model_validate`,
`model_dump`, and `model_json_schema`) but is intentionally not a Pydantic clone.
"""

from __future__ import annotations

import copy
import json
import math
import types
import urllib.parse
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Union, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "2.0"
CONCEPTUAL_NOTICE = (
    "Conceptual planning model — not for design, permitting, flood-risk "
    "determination, or representation of an existing drainage system."
)
FULL_EVIDENCE_SYNC_CATEGORIES = (
    "model",
    "gis",
    "storm",
    "solver_input",
    "solver_report",
    "solver_output",
    "result",
    "qa",
    "search_trace",
    "decision_brief",
    "export",
    "benchmark",
    "assistant_trace",
)


def _require_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


try:  # pragma: no cover - exercised in packaged builds
    from pydantic import BaseModel as _PydanticBaseModel
    from pydantic import ConfigDict, Field

    PYDANTIC_AVAILABLE = True

    class ContractModel(_PydanticBaseModel):
        model_config = ConfigDict(
            extra="forbid",
            validate_assignment=True,
            use_enum_values=False,
            str_strip_whitespace=True,
        )

        def model_post_init(self, __context: Any, /) -> None:
            self._validate_contract()

        def _validate_contract(self) -> None:
            return None

except ImportError:  # pragma: no cover - the default in the lean test image
    PYDANTIC_AVAILABLE = False

    _MISSING = object()

    class _FieldSpec:
        def __init__(
            self,
            default: Any = _MISSING,
            *,
            default_factory: Any = None,
            ge: float | None = None,
            gt: float | None = None,
            le: float | None = None,
            lt: float | None = None,
            min_length: int | None = None,
            max_length: int | None = None,
            description: str | None = None,
        ) -> None:
            self.default = default
            self.default_factory = default_factory
            self.ge = ge
            self.gt = gt
            self.le = le
            self.lt = lt
            self.min_length = min_length
            self.max_length = max_length
            self.description = description

        def value(self) -> Any:
            if self.default_factory is not None:
                return self.default_factory()
            if self.default is not _MISSING:
                return copy.deepcopy(self.default)
            raise TypeError("field is required")

        def check(self, name: str, value: Any) -> None:
            if value is None:
                return
            if self.ge is not None and value < self.ge:
                raise ValueError(f"{name} must be >= {self.ge}")
            if self.gt is not None and value <= self.gt:
                raise ValueError(f"{name} must be > {self.gt}")
            if self.le is not None and value > self.le:
                raise ValueError(f"{name} must be <= {self.le}")
            if self.lt is not None and value >= self.lt:
                raise ValueError(f"{name} must be < {self.lt}")
            if self.min_length is not None and len(value) < self.min_length:
                raise ValueError(f"{name} must contain at least {self.min_length} items")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"{name} must contain at most {self.max_length} items")

    def Field(  # type: ignore[misc]
        default: Any = _MISSING,
        *,
        default_factory: Any = None,
        ge: float | None = None,
        gt: float | None = None,
        le: float | None = None,
        lt: float | None = None,
        min_length: int | None = None,
        max_length: int | None = None,
        description: str | None = None,
    ) -> _FieldSpec:
        return _FieldSpec(
            default,
            default_factory=default_factory,
            ge=ge,
            gt=gt,
            le=le,
            lt=lt,
            min_length=min_length,
            max_length=max_length,
            description=description,
        )

    def _fields(cls: type) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            result.update(getattr(base, "__annotations__", {}))
        return {
            key: value
            for key, value in result.items()
            if get_origin(value) is not ClassVar and not key.startswith("_")
        }

    def _coerce(annotation: Any, value: Any) -> Any:
        if value is None:
            return None
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in (Union, types.UnionType):
            for choice in args:
                if choice is type(None):
                    continue
                try:
                    return _coerce(choice, value)
                except (TypeError, ValueError):
                    pass
            return value
        if origin is Literal:
            if value not in args:
                raise ValueError(f"expected one of {args!r}")
            return value
        if origin is list:
            subtype = args[0] if args else Any
            return [_coerce(subtype, item) for item in value]
        if origin is tuple:
            subtype = args[0] if args else Any
            return tuple(_coerce(subtype, item) for item in value)
        if origin is dict:
            key_type, value_type = args if args else (Any, Any)
            return {
                _coerce(key_type, key): _coerce(value_type, item) for key, item in value.items()
            }
        if annotation is Any:
            return value
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return value if isinstance(value, annotation) else annotation(value)
        if isinstance(annotation, type) and issubclass(annotation, ContractModel):
            return value if isinstance(value, annotation) else annotation.model_validate(value)
        if annotation is Path:
            return value if isinstance(value, Path) else Path(value)
        if annotation in (str, int, float, bool):
            if annotation is bool and isinstance(value, str):
                return value.lower() in {"true", "1", "yes"}
            return annotation(value)
        return value

    def _dump(value: Any, *, mode: str = "python") -> Any:
        if isinstance(value, ContractModel):
            return value.model_dump(mode=mode)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, list):
            return [_dump(item, mode=mode) for item in value]
        if isinstance(value, tuple):
            return [_dump(item, mode=mode) for item in value]
        if isinstance(value, dict):
            return {str(key): _dump(item, mode=mode) for key, item in value.items()}
        return value

    def _schema(annotation: Any) -> dict[str, Any]:
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in (Union, types.UnionType):
            return {"anyOf": [_schema(choice) for choice in args]}
        if origin is Literal:
            return {"enum": list(args)}
        if origin in (list, tuple):
            return {"type": "array", "items": _schema(args[0] if args else Any)}
        if origin is dict:
            return {
                "type": "object",
                "additionalProperties": _schema(args[1] if args else Any),
            }
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return {"type": "string", "enum": [item.value for item in annotation]}
        if isinstance(annotation, type) and issubclass(annotation, ContractModel):
            return annotation.model_json_schema()
        return {
            str: {"type": "string"},
            int: {"type": "integer"},
            float: {"type": "number"},
            bool: {"type": "boolean"},
            Path: {"type": "string", "format": "path"},
        }.get(annotation, {})

    class ContractModel:
        """Minimal offline fallback with a Pydantic-shaped public surface."""

        def __init__(self, **data: Any) -> None:
            try:
                annotations = get_type_hints(self.__class__)
            except (NameError, TypeError):
                annotations = _fields(self.__class__)
            annotations = {
                key: value
                for key, value in annotations.items()
                if get_origin(value) is not ClassVar and not key.startswith("_")
            }
            unexpected = set(data) - set(annotations)
            if unexpected:
                raise ValueError(f"unexpected fields: {', '.join(sorted(unexpected))}")
            for name, annotation in annotations.items():
                field = getattr(self.__class__, name, _MISSING)
                if name in data:
                    raw = data[name]
                elif isinstance(field, _FieldSpec):
                    raw = field.value()
                elif field is not _MISSING:
                    raw = copy.deepcopy(field)
                else:
                    raise ValueError(f"{name} is required")
                value = _coerce(annotation, raw)
                if isinstance(field, _FieldSpec):
                    field.check(name, value)
                setattr(self, name, value)
            self._validate_contract()

        def _validate_contract(self) -> None:
            return None

        @classmethod
        def model_validate(cls, value: Any) -> ContractModel:
            if isinstance(value, cls):
                return value
            if not isinstance(value, dict):
                raise TypeError(f"{cls.__name__} requires an object")
            return cls(**value)

        def model_dump(self, *, mode: str = "python", **_: Any) -> dict[str, Any]:
            return {
                name: _dump(getattr(self, name), mode=mode)
                for name in _fields(self.__class__)
                if hasattr(self, name)
            }

        def model_dump_json(self, **kwargs: Any) -> str:
            return json.dumps(self.model_dump(mode="json"), **kwargs)

        def model_copy(self, *, update: dict[str, Any] | None = None) -> ContractModel:
            values = self.model_dump(mode="python")
            values.update(update or {})
            return self.__class__.model_validate(values)

        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            try:
                annotations = get_type_hints(cls)
            except (NameError, TypeError):
                annotations = _fields(cls)
            properties = {
                name: _schema(annotation)
                for name, annotation in annotations.items()
                if get_origin(annotation) is not ClassVar and not name.startswith("_")
            }
            required = []
            for name in properties:
                default = getattr(cls, name, _MISSING)
                if default is _MISSING or (
                    isinstance(default, _FieldSpec)
                    and default.default is _MISSING
                    and default.default_factory is None
                ):
                    required.append(name)
            return {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": cls.__name__,
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            }


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ParameterOrigin(str, Enum):
    OBSERVED = "observed"
    IMPORTED = "imported"
    DERIVED = "derived"
    DEFAULTED = "defaulted"
    MANUAL = "manually_entered"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    ASSUMED = "assumed"


class ProjectKind(str, Enum):
    DEMO = "demo"
    CONCEPTUAL = "conceptual"
    EXISTING = "existing"


class AuthorityStatus(str, Enum):
    ADOPTED = "adopted"
    PRELIMINARY = "preliminary"
    SENSITIVITY = "sensitivity"
    USER_DEFINED = "user_defined"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class AnalysisState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SolverJobState(str, Enum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    QA_REJECTED = "QA_REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MetricDirection(str, Enum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    AT_MOST = "at_most"
    AT_LEAST = "at_least"


class LIDType(str, Enum):
    BIORETENTION = "BC"
    RAIN_GARDEN = "RG"
    GREEN_ROOF = "GR"
    INFILTRATION_TRENCH = "IT"
    PERMEABLE_PAVEMENT = "PP"
    RAIN_BARREL = "RB"
    ROOFTOP_DISCONNECTION = "RD"
    VEGETATIVE_SWALE = "VS"


class ErrorCategory(str, Enum):
    USER_FIXABLE = "USER_FIXABLE"
    MODEL_QUALITY = "MODEL_QUALITY"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    DATA_PROVIDER = "DATA_PROVIDER"
    SOLVER_FAILURE = "SOLVER_FAILURE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    INTERNAL = "INTERNAL"


class FitnessDisposition(str, Enum):
    NOT_ASSESSED = "not_assessed"
    FIT_FOR_STATED_USE = "fit_for_stated_use"
    FIT_WITH_LIMITATIONS = "fit_with_limitations"
    NOT_FIT = "not_fit"


class ObservabilityProvider(str, Enum):
    WANDB = "wandb"
    WEAVE = "weave"


class ObservabilityMode(str, Enum):
    UNBOUND = "unbound"
    ONLINE = "online"
    OFFLINE_QUEUE = "offline_queue"


class TelemetryState(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    SYNCED = "synced"
    FAILED = "failed"
    BLOCKED = "blocked"


class DataAsset(ContractModel):
    asset_id: str = Field(default_factory=lambda: new_id("asset"))
    path: str
    role: str
    source: str
    license: str
    retrieved_at: datetime | None = None
    vintage: str | None = None
    crs: str | None = None
    resolution: str | None = None
    sha256: str | None = None
    attribution: str | None = None


class ModelObjectProvenance(ContractModel):
    object_type: str
    object_id: str
    parameter: str
    value: Any
    units: str | None = None
    origin: ParameterOrigin
    source_asset_id: str | None = None
    formula: str | None = None
    confidence: Confidence
    reviewed: bool = False
    reviewer: str | None = None
    notes: str | None = None


class StudyArea(ContractModel):
    boundary: list[tuple[float, float]] = Field(min_length=4)
    crs: str
    name: str = "Study area"

    def _validate_contract(self) -> None:
        if self.boundary[0] != self.boundary[-1]:
            raise ValueError("study-area boundary must be a closed ring")


class OutletSpec(ContractModel):
    outlet_id: str
    x: float
    y: float
    invert_elevation: float | None = None
    confirmed: bool = False


class ConceptModelRecipe(ContractModel):
    schema_version: str = SCHEMA_VERSION
    recipe_id: str = Field(default_factory=lambda: new_id("recipe"))
    title: str
    study_area: StudyArea
    outlets: list[OutletSpec] = Field(min_length=1)
    data_assets: list[DataAsset] = Field(default_factory=list)
    synthesis_engine: Literal["local_deterministic", "swmmanywhere"] = "local_deterministic"
    coordinate_units: Literal["feet", "meters"] = "meters"
    flow_units: Literal["CFS", "CMS", "LPS"] = "CMS"
    subcatchment_count: int = Field(default=4, ge=1, le=64)
    assumptions: dict[str, Any] = Field(default_factory=dict)
    seed: int = 42


class StormCase(ContractModel):
    storm_id: str = Field(default_factory=lambda: new_id("storm"))
    name: str
    model_path: str
    role: str
    authority_status: AuthorityStatus
    source: str
    source_version: str | None = None
    retrieved_at: datetime | None = None
    aep: float | None = Field(default=None, gt=0, le=1)
    duration_hours: float | None = Field(default=None, gt=0)
    initial_conditions: str | None = None
    boundary_assumptions: str | None = None


class ProjectBundle(ContractModel):
    schema_version: str = SCHEMA_VERSION
    project_id: str = Field(default_factory=lambda: new_id("project"))
    title: str
    base_model_path: str
    storm_cases: list[StormCase] = Field(default_factory=list)
    dependencies: list[DataAsset] = Field(default_factory=list)
    reference_report_path: str | None = None
    project_kind: ProjectKind
    model_revision_id: str
    model_fitness_passport_id: str | None = None
    limitations: list[str] = Field(default_factory=list)


class CandidateSlot(ContractModel):
    slot_id: str = Field(default_factory=lambda: new_id("slot"))
    subcatchment_id: str
    lid_control: str
    lid_type: LIDType | None = None
    base_units: int = Field(default=0, ge=0)
    min_additional_units: int = Field(default=0, ge=0)
    max_additional_units: int = Field(ge=0)
    unit_area: float = Field(gt=0)
    surface_width: float = Field(gt=0)
    initial_saturation_percent: float = Field(default=0, ge=0, le=100)
    from_impervious_percent: float = Field(default=100, ge=0, le=100)
    from_pervious: bool = False
    route_to_pervious: bool = False
    report_file: str | None = None
    drain_to: str | None = None
    displaced_surface_area: float = Field(default=0, ge=0)
    feasibility_basis: str
    engineer_approved: bool = False

    def _validate_contract(self) -> None:
        if self.max_additional_units < self.min_additional_units:
            raise ValueError("max_additional_units must be >= min_additional_units")


class CostBasis(ContractModel):
    unit_capex: dict[str, float] = Field(default_factory=dict)
    annual_om: dict[str, float] = Field(default_factory=dict)
    replacement_cost: dict[str, float] = Field(default_factory=dict)
    replacement_years: dict[str, list[int]] = Field(default_factory=dict)
    analysis_horizon_years: int = Field(default=30, gt=0)
    discount_rate: float = Field(default=0.03, ge=0, le=1)
    currency: str = "USD"
    price_vintage: str
    source: str
    confirmed: bool = False


class MetricSpec(ContractModel):
    metric_id: str = Field(default_factory=lambda: new_id("metric"))
    object_type: Literal["system", "node", "link", "outfall", "lid"]
    object_id: str | None = None
    metric: str
    units: str
    direction: MetricDirection
    threshold: float | None = None
    storm_aggregation: Literal["each", "maximum", "minimum", "mean", "weighted_mean"] = "each"
    weight: float = Field(default=1, ge=0)


class QAPolicy(ContractModel):
    runoff_continuity_warning_percent: float = Field(default=2.0, ge=0)
    flow_continuity_warning_percent: float = Field(default=2.0, ge=0)
    absolute_tolerance: float = Field(default=1e-6, ge=0)
    relative_tolerance: float = Field(default=1e-4, ge=0)
    accepted_exceptions: dict[str, str] = Field(default_factory=dict)


class SimulationBudget(ContractModel):
    max_solver_jobs: int = Field(default=1000, ge=1, le=5000)
    max_wall_seconds: int | None = Field(default=None, gt=0)
    max_energy_kwh: float | None = Field(default=None, gt=0)
    seed: int = 42
    worker_count: int = Field(default=1, ge=1, le=64)

    def design_capacity(self, storm_count: int, baseline_jobs: int | None = None) -> int:
        if storm_count < 1:
            raise ValueError("at least one storm case is required")
        baseline = storm_count if baseline_jobs is None else baseline_jobs
        return max(0, (self.max_solver_jobs - baseline) // storm_count)


class ExactRunResult(ContractModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    analysis_id: str
    model_revision_id: str
    design_id: str
    storm_id: str
    exact: Literal[True] = True
    metrics: dict[str, float]
    qa_status: Literal["accepted", "warning", "rejected"] = "accepted"
    qa_findings: list[str] = Field(default_factory=list)
    input_sha256: str
    report_sha256: str
    solver_sha256: str
    output_sha256: str
    extractor_version: str
    cache_hit: bool = False
    completed_at: datetime

    def _validate_contract(self) -> None:
        for label, digest in (
            ("input", self.input_sha256),
            ("report", self.report_sha256),
            ("solver", self.solver_sha256),
            ("output", self.output_sha256),
        ):
            _require_sha256(digest, label=f"exact-run {label}")
        if not self.extractor_version.strip():
            raise ValueError("exact-run extractor version cannot be empty")


class SearchEvent(ContractModel):
    sequence: int = Field(ge=0)
    method: str
    design_id: str
    design_vector: list[int]
    batch: int = Field(ge=0)


class SearchTrace(ContractModel):
    analysis_id: str
    seed: int
    method: str
    solver_job_cap: int
    storm_count: int
    events: list[SearchEvent] = Field(default_factory=list)
    model_hash: str | None = None
    stop_reason: str | None = None


class EnergyObservation(ContractModel):
    observation_id: str = Field(default_factory=lambda: new_id("energy"))
    analysis_id: str
    boundary: str
    method: str
    status: Literal["measured", "estimated"]
    energy_kwh: float = Field(ge=0)
    hardware: str
    sampling_interval_seconds: float | None = Field(default=None, gt=0)
    uncertainty: str | None = None
    exclusions: list[str] = Field(default_factory=list)


class ExportManifest(ContractModel):
    schema_version: str = SCHEMA_VERSION
    export_id: str = Field(default_factory=lambda: new_id("export"))
    project_id: str
    analysis_id: str
    model_revision_id: str
    model_fitness_passport_id: str
    export_eligibility_id: str
    package_type: Literal["learning", "screening", "verified_alternatives"]
    created_at: datetime = Field(default_factory=utc_now)
    input_hashes: dict[str, str]
    solver_sha256: str
    extractor_version: str
    contract_version: str = SCHEMA_VERSION
    template_version: str
    files: dict[str, str] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    accepted_exceptions: list[str] = Field(default_factory=list)
    fitness_notice: str | None = None


class EvidenceClaim(ContractModel):
    claim_id: str = Field(default_factory=lambda: new_id("claim"))
    project_id: str
    claim_type: Literal[
        "exact_metric",
        "qa",
        "assumption",
        "cost",
        "provenance",
        "fitness",
        "reference_comparison",
    ]
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    value: float | None = None
    units: str | None = None
    run_id: str | None = None
    metric_id: str | None = None
    object_id: str = ""
    exact: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if (self.value is None) != (self.units is None):
            raise ValueError("numeric evidence claims require both value and units")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("evidence claim values must be finite")
        if self.exact and (self.run_id is None or self.metric_id is None or self.value is None):
            raise ValueError("exact evidence claims require run, metric, value, and units")


class ModelFitnessCriterion(ContractModel):
    criterion_id: str
    label: str
    status: Literal["pass", "review", "block", "not_assessed"]
    finding: str
    evidence_claim_ids: list[str] = Field(default_factory=list)
    remediation: str | None = None


class ModelFitnessPassport(ContractModel):
    """Independent, intended-use decision gate signed by an engineer."""

    passport_id: str = Field(default_factory=lambda: new_id("fitness"))
    project_id: str
    model_revision_id: str
    intended_use: str
    disposition: FitnessDisposition = FitnessDisposition.NOT_ASSESSED
    criteria: list[ModelFitnessCriterion] = Field(default_factory=list)
    evidence_claims: list[EvidenceClaim] = Field(default_factory=list)
    permitted_outputs: list[str] = Field(default_factory=list)
    prohibited_uses: list[str] = Field(default_factory=list)
    unresolved_limitations: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    reviewer_attested: bool = False
    issued_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        statuses = {item.status for item in self.criteria}
        evidence_ids = {claim.claim_id for claim in self.evidence_claims}
        cited_ids = {
            evidence_id
            for criterion in self.criteria
            for evidence_id in criterion.evidence_claim_ids
        }
        if cited_ids.difference(evidence_ids):
            raise ValueError("fitness criteria cite evidence claims outside this passport")
        if self.disposition == FitnessDisposition.FIT_FOR_STATED_USE:
            if not self.criteria or statuses.difference({"pass"}):
                raise ValueError("fit_for_stated_use requires every criterion to pass")
            if not self.reviewer or not self.reviewer_attested or not self.evidence_claims:
                raise ValueError("fit_for_stated_use requires attested reviewer evidence")
        if self.disposition == FitnessDisposition.FIT_WITH_LIMITATIONS:
            if "block" in statuses or not self.unresolved_limitations:
                raise ValueError(
                    "fit_with_limitations requires limitations and no blocking criterion"
                )
            if not self.reviewer or not self.reviewer_attested:
                raise ValueError("fit_with_limitations requires engineer attestation")
        if self.disposition == FitnessDisposition.NOT_FIT and "block" not in statuses:
            raise ValueError("not_fit requires at least one blocking criterion")


class ExportEligibility(ContractModel):
    eligibility_id: str = Field(default_factory=lambda: new_id("eligibility"))
    project_id: str
    analysis_id: str
    passport_id: str
    model_revision_id: str
    fitness_disposition: FitnessDisposition
    requested_package: Literal["learning", "screening", "verified_alternatives"]
    allowed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    required_evidence_ids: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if self.allowed and self.blockers:
            raise ValueError("an allowed export cannot retain blocking findings")


class FeasibilityRecord(ContractModel):
    feasibility_id: str = Field(default_factory=lambda: new_id("feasibility"))
    project_id: str
    slot_id: str
    status: Literal["unreviewed", "feasible", "conditional", "infeasible"] = "unreviewed"
    basis: str
    footprint_available: bool | None = None
    known_conflicts: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    reviewed_at: datetime | None = None


class DecisionCriterion(ContractModel):
    criterion_id: str = Field(default_factory=lambda: new_id("criterion"))
    label: str
    category: Literal["hydraulic", "cost", "adverse_effect", "feasibility", "implementation"]
    units: str | None = None
    direction: MetricDirection | None = None
    threshold: float | None = None
    storm_aggregation: str | None = None
    rationale: str


class AlternativeDecisionSummary(ContractModel):
    design_id: str
    label: str
    exact: Literal[True] = True
    qa_status: Literal["accepted", "warning", "rejected"]
    exact_run_ids: list[str] = Field(min_length=1)
    controlling_storm_ids: list[str] = Field(default_factory=list)
    criterion_values: dict[str, float] = Field(default_factory=dict)
    benefits: list[str] = Field(default_factory=list)
    adverse_effects: list[str] = Field(default_factory=list)
    feasibility_record_ids: list[str] = Field(default_factory=list)
    present_value_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    evidence_claim_ids: list[str] = Field(min_length=1)

    def _validate_contract(self) -> None:
        if (self.present_value_cost is None) != (self.currency is None):
            raise ValueError("present-value cost and currency must be supplied together")


class DecisionBrief(ContractModel):
    brief_id: str = Field(default_factory=lambda: new_id("brief"))
    project_id: str
    analysis_id: str
    model_revision_id: str
    title: str
    status: Literal["draft", "review_ready", "accepted"] = "draft"
    baseline_design_id: str
    intended_decision: str
    model_fitness_passport_id: str
    criteria: list[DecisionCriterion] = Field(min_length=1)
    alternatives: list[AlternativeDecisionSummary] = Field(min_length=1)
    recommended_design_id: str | None = None
    selection_rationale: list[str] = Field(default_factory=list)
    full_network_adverse_effects_reviewed: bool = False
    evidence_claim_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    engineer: str | None = None
    accepted_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        option_ids = {item.design_id for item in self.alternatives}
        if self.recommended_design_id and self.recommended_design_id not in option_ids:
            raise ValueError("recommended_design_id must identify an alternative in the brief")
        if self.status in {"review_ready", "accepted"}:
            if not self.full_network_adverse_effects_reviewed:
                raise ValueError(
                    "review-ready decisions require full-network adverse-effect review"
                )
            if not self.evidence_claim_ids or any(
                item.qa_status == "rejected" for item in self.alternatives
            ):
                raise ValueError(
                    "review-ready decisions require evidence and no QA-rejected option"
                )
        if self.status == "accepted" and (not self.engineer or self.accepted_at is None):
            raise ValueError("accepted decisions require an engineer and acceptance time")


class AnalysisRunPlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: new_id("runplan"))
    project_id: str
    analysis_id: str
    model_revision_id: str
    model_sha256: str
    baseline_jobs: int = Field(ge=0)
    candidate_designs: int = Field(ge=0)
    storm_count: int = Field(ge=1)
    total_exact_jobs: int = Field(ge=0)
    cache_hits: int = Field(default=0, ge=0)
    worker_count: int = Field(default=1, ge=1)
    search_method: str
    search_reason: str
    seed: int
    estimated_storage_bytes: int | None = Field(default=None, ge=0)
    reportable_outputs: list[str] = Field(default_factory=list)

    def _validate_contract(self) -> None:
        _require_sha256(self.model_sha256, label="analysis model revision")
        expected = self.baseline_jobs + self.candidate_designs * self.storm_count
        if self.total_exact_jobs != expected:
            raise ValueError("total_exact_jobs must equal baseline plus design-by-storm jobs")


class ProjectDataPolicy(ContractModel):
    policy_id: str = Field(default_factory=lambda: new_id("policy"))
    project_id: str
    classification: Literal["standard_engineering", "critical_infrastructure"]
    deployment: Literal["wandb_cloud", "dedicated_cloud", "self_managed"]
    full_evidence_sync: Literal[True] = True
    permitted_categories: list[
        Literal[
            "model",
            "gis",
            "storm",
            "solver_input",
            "solver_report",
            "solver_output",
            "result",
            "qa",
            "search_trace",
            "decision_brief",
            "export",
            "benchmark",
            "assistant_trace",
        ]
    ] = Field(default_factory=lambda: list(FULL_EVIDENCE_SYNC_CATEGORIES))
    prohibited_categories: list[str] = Field(
        default_factory=lambda: [
            "credentials",
            "environment_variables",
            "local_absolute_paths",
            "raw_console_logs",
        ]
    )
    local_authority: Literal[True] = True
    acknowledged_by: str
    acknowledged_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if self.classification == "critical_infrastructure" and self.deployment not in {
            "dedicated_cloud",
            "self_managed",
        }:
            raise ValueError(
                "critical-infrastructure projects require Dedicated or Self-Managed W&B"
            )
        required_prohibitions = {
            "credentials",
            "environment_variables",
            "local_absolute_paths",
            "raw_console_logs",
        }
        if not required_prohibitions.issubset(set(self.prohibited_categories)):
            raise ValueError(
                "project data policy must exclude secrets, environment, paths, and console logs"
            )
        if set(self.permitted_categories) != set(FULL_EVIDENCE_SYNC_CATEGORIES):
            raise ValueError("full evidence sync requires every engineering evidence category")


class ObservabilityBinding(ContractModel):
    binding_id: str = Field(default_factory=lambda: new_id("binding"))
    project_id: str
    data_policy_id: str
    entity: str
    external_project: str
    external_project_id: str
    api_base_url: str = "https://api.wandb.ai"
    web_base_url: str = "https://wandb.ai"
    access: Literal["Restricted"] = "Restricted"
    deployment: Literal["wandb_cloud", "dedicated_cloud", "self_managed"]
    mode: ObservabilityMode = ObservabilityMode.ONLINE
    verified: bool = False
    immutable_after_first_upload: Literal[True] = True
    first_upload_at: datetime | None = None
    previous_binding_id: str | None = None
    verified_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if self.verified != (self.verified_at is not None):
            raise ValueError("verified binding state and verification timestamp must agree")
        if self.mode == ObservabilityMode.OFFLINE_QUEUE and not self.verified:
            raise ValueError("offline queue is available only after initial binding verification")
        for label, value in (
            ("api_base_url", self.api_base_url),
            ("web_base_url", self.web_base_url),
        ):
            parsed = urllib.parse.urlparse(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{label} must be an HTTPS origin without credentials")


class CloudArtifactManifest(ContractModel):
    artifact_id: str = Field(default_factory=lambda: new_id("cloudartifact"))
    project_id: str
    category: Literal[
        "model",
        "gis",
        "storm",
        "solver_input",
        "solver_report",
        "solver_output",
        "result",
        "qa",
        "search_trace",
        "decision_brief",
        "export",
        "benchmark",
        "assistant_trace",
    ]
    logical_name: str
    sha256: str
    bytes: int = Field(ge=0)
    media_type: str
    source_object_id: str
    local_blob_sha256: str
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        for label, value in (
            ("sha256", self.sha256),
            ("local_blob_sha256", self.local_blob_sha256),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")


class TelemetryEnvelope(ContractModel):
    envelope_id: str = Field(default_factory=lambda: new_id("envelope"))
    project_id: str
    analysis_id: str | None = None
    binding_id: str
    category: Literal[
        "engineering_evidence", "benchmark_run", "assistant_trace", "assistant_evaluation"
    ]
    event_type: str
    subject_id: str
    idempotency_key: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[CloudArtifactManifest] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("telemetry idempotency key cannot be empty")


class TelemetryOutboxItem(ContractModel):
    outbox_id: str = Field(default_factory=lambda: new_id("outbox"))
    envelope: TelemetryEnvelope
    payload_sha256: str
    state: TelemetryState = TelemetryState.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error_code: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WandbRunReference(ContractModel):
    reference_id: str = Field(default_factory=lambda: new_id("wandbrun"))
    project_id: str
    envelope_id: str
    entity: str
    external_project: str
    run_id: str
    url: str
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class WeaveTraceReference(ContractModel):
    reference_id: str = Field(default_factory=lambda: new_id("weavetrace"))
    project_id: str
    call_id: str
    entity: str
    external_project: str
    trace_id: str
    provider_call_id: str
    url: str
    created_at: datetime = Field(default_factory=utc_now)


class CloudSyncState(ContractModel):
    project_id: str
    state: Literal[
        "LOCAL_ONLY",
        "CONNECTED",
        "SYNCING",
        "SYNC_QUEUED_OFFLINE",
        "FAILED_RETRYING",
        "AUTHENTICATION_REQUIRED",
        "BLOCKED_PROJECT_POLICY",
    ] = "LOCAL_ONLY"
    binding_required: bool = False
    binding: ObservabilityBinding | None = None
    can_enter_home: bool = True
    can_open_project: bool = True
    can_run_local: bool = True
    can_sync: bool = False
    can_use_assistant: bool = False
    online: bool = False
    outbox_counts: dict[str, int] = Field(default_factory=dict)
    pending_artifact_bytes: int = Field(default=0, ge=0)
    last_synced_at: datetime | None = None
    last_error_code: str | None = None
    local_authority: Literal[True] = True

    def _validate_contract(self) -> None:
        if not self.can_enter_home or not self.can_open_project or not self.can_run_local:
            raise ValueError("local engineering work must remain available in every cloud state")
        if self.binding_required:
            raise ValueError("a W&B binding cannot be required for local engineering work")
        if self.state == "LOCAL_ONLY" and self.binding is not None:
            raise ValueError("local-only project state cannot claim a cloud binding")
        if self.can_sync and (not self.online or self.binding is None or not self.binding.verified):
            raise ValueError("cloud sync requires an online verified Restricted binding")
        if self.can_use_assistant and (
            not self.online or self.binding is None or not self.binding.verified
        ):
            raise ValueError("Evidence Assistant requires an online verified binding")


class ObservabilityAccountState(ContractModel):
    """Validated pre-project W&B/Weave onboarding state used by the desktop RPC."""

    schemaVersion: Literal["2.0"] = "2.0"
    scope: Literal["account"] = "account"
    state: Literal["LOCAL_ONLY", "CONNECTED", "AUTHENTICATION_REQUIRED", "BLOCKED_PROJECT_POLICY"]
    mode: Literal["local", "connected", "error"]
    configured: bool
    provider: Literal["none", "wandb-weave"]
    disclosureAccepted: bool
    sessionId: str | None = None
    entity: str | None = None
    project: str | None = None
    weaveProject: str | None = None
    wandbUrl: str | None = None
    weaveUrl: str | None = None
    deployment: Literal["wandb_cloud", "dedicated_cloud", "self_managed"] | None = None
    apiBaseUrl: str | None = None
    webBaseUrl: str | None = None
    externalProjectId: str | None = None
    includedCategories: list[str] = Field(default_factory=list)
    excludedCategories: list[str] = Field(default_factory=list)
    probes: dict[str, Any] = Field(default_factory=dict)
    lastChecked: datetime | None = None
    message: str

    def _validate_contract(self) -> None:
        if self.state == "CONNECTED":
            required = (
                self.sessionId,
                self.entity,
                self.project,
                self.wandbUrl,
                self.weaveUrl,
                self.externalProjectId,
            )
            if not self.configured or self.mode != "connected" or not all(required):
                raise ValueError(
                    "connected account observability requires verified provider fields"
                )
        elif self.state == "LOCAL_ONLY":
            if not self.configured or self.mode != "local" or self.provider != "none":
                raise ValueError("local-only account state must be configured without a provider")
            if self.probes or self.disclosureAccepted:
                raise ValueError("local-only account state cannot claim cloud probes or consent")
        elif self.configured or self.mode == "connected":
            raise ValueError("an unconnected account cannot be configured as connected")


class BenchmarkRunEnvelope(ContractModel):
    benchmark_id: str
    project_id: str
    analysis_id: str
    fixture_id: str
    fixture_license: str
    solver_sha256: str
    search_method: str
    seed: int
    exact_job_count: int = Field(ge=0)
    exact_design_count: int = Field(ge=0)
    nondominated_count: int = Field(ge=0)
    qa_rejected_count: int = Field(ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    artifact_manifest_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AssistantEvidenceReference(ContractModel):
    evidence_id: str
    kind: Literal["project", "exact_run", "exact_metric", "qa", "artifact", "documentation"]
    label: str
    source_id: str


class AssistantNumericClaim(ContractModel):
    label: str
    value: float
    units: str
    evidence_id: str
    run_id: str
    metric_id: str
    object_id: str = ""
    exact: Literal[True] = True

    def _validate_contract(self) -> None:
        if not math.isfinite(self.value):
            raise ValueError("assistant numeric claims must be finite")


class AssistantProposedAction(ContractModel):
    action: Literal["navigate", "open_evidence", "review", "prepare_draft"]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)


class AssistantAnswer(ContractModel):
    answer_id: str = Field(default_factory=lambda: new_id("answer"))
    call_id: str
    project_id: str
    intent: str
    answer: str
    evidence_references: list[AssistantEvidenceReference] = Field(default_factory=list)
    numeric_claims: list[AssistantNumericClaim] = Field(default_factory=list)
    assumptions_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    proposed_actions: list[AssistantProposedAction] = Field(default_factory=list)
    weave_trace: WeaveTraceReference
    requires_engineer_confirmation: Literal[True] = True
    read_only: Literal[True] = True
    created_at: datetime = Field(default_factory=utc_now)


class AssistantEvaluationRecord(ContractModel):
    evaluation_id: str = Field(default_factory=lambda: new_id("assistanteval"))
    project_id: str
    answer_id: str
    trace_id: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_evidence_ids: list[str] = Field(default_factory=list)
    blocked_numeric_claims: list[str] = Field(default_factory=list)
    user_feedback: Literal["helpful", "not_helpful", "incorrect", "unsafe"] | None = None
    feedback_notes: str | None = None
    validator_version: str = "deterministic-evidence-validator-v2"
    created_at: datetime = Field(default_factory=utc_now)


class GuidedCheckpoint(ContractModel):
    checkpoint_id: str
    project_id: str
    step_id: Literal[
        "overview", "model", "storms", "opportunities", "goals", "run", "compare", "export"
    ]
    title: str
    purpose: str
    how_to_use: str
    completion_signal: str
    status: Literal["complete", "current", "pending", "blocked"] = "pending"
    primary_action_label: str
    primary_rpc_method: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    analysis_id: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class SemanticModelDiff(ContractModel):
    diff_id: str = Field(default_factory=lambda: new_id("modeldiff"))
    project_id: str
    from_revision_id: str | None = None
    to_revision_id: str
    commands: list[str] = Field(default_factory=list)
    added_objects: list[str] = Field(default_factory=list)
    removed_objects: list[str] = Field(default_factory=list)
    changed_parameters: list[dict[str, Any]] = Field(default_factory=list)
    affected_artifact_ids: list[str] = Field(default_factory=list)
    sha256: str

    def _validate_contract(self) -> None:
        _require_sha256(self.sha256, label="semantic model diff")


class ModelRevision(ContractModel):
    revision_id: str = Field(default_factory=lambda: new_id("revision"))
    project_id: str
    parent_revision_id: str | None = None
    reason: Literal["import", "generated", "manual_edit", "alternative", "rebind"]
    model_sha256: str
    dependency_hashes: dict[str, str] = Field(default_factory=dict)
    semantic_diff: SemanticModelDiff | None = None
    observability_binding_id: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        _require_sha256(self.model_sha256, label="model revision")
        for name, digest in self.dependency_hashes.items():
            if not name.strip():
                raise ValueError("model revision dependency names cannot be empty")
            _require_sha256(digest, label=f"model revision dependency {name!r}")
        if not self.created_by.strip():
            raise ValueError("model revision creator cannot be empty")
        if self.reason == "rebind" and not self.observability_binding_id:
            raise ValueError("an observability rebind revision must identify its new binding")


# Internal persistence records. They are not public RPC contracts.
class TelemetryEvent(ContractModel):
    event_id: str = Field(default_factory=lambda: new_id("telemetry"))
    project_id: str
    analysis_id: str | None = None
    provider: ObservabilityProvider
    event_type: Literal["search_benchmark", "assistant_evaluation", "release_gate"]
    data_classification: Literal["PUBLIC_BENCHMARK", "SYNTHETIC_EVALUATION"]
    redistributable: Literal[True] = True
    idempotency_key: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)


class ExternalReference(ContractModel):
    reference_id: str = Field(default_factory=lambda: new_id("external"))
    project_id: str
    analysis_id: str | None = None
    event_id: str | None = None
    provider: ObservabilityProvider
    resource_type: str
    external_id: str
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class AssistantExplanationRequest(ContractModel):
    project_id: str
    intent: Literal["model_fitness", "exact_run"]
    job_id: str | None = None


class AssistantExplanation(ContractModel):
    response_id: str = Field(default_factory=lambda: new_id("explanation"))
    project_id: str
    intent: str
    answer: str
    evidence_references: list[AssistantEvidenceReference] = Field(default_factory=list)
    numeric_claims: list[AssistantNumericClaim] = Field(default_factory=list)
    assumptions_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    proposed_actions: list[AssistantProposedAction] = Field(default_factory=list)
    requires_engineer_confirmation: bool = True
    read_only: Literal[True] = True


class AssistantValidation(ContractModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_evidence_ids: list[str] = Field(default_factory=list)
    blocked_numeric_claims: list[str] = Field(default_factory=list)


class GuidedStep(ContractModel):
    step_id: Literal[
        "overview", "model", "storms", "opportunities", "goals", "run", "compare", "export"
    ]
    title: str
    purpose: str
    how_to_use: str
    completion_signal: str
    status: Literal["complete", "current", "pending", "blocked"] = "pending"
    primary_action_label: str
    primary_rpc_method: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class GuidedWorkflow(ContractModel):
    workflow_id: str
    project_id: str
    audience: str
    outcome: str
    project_kind: ProjectKind
    fitness_disposition: FitnessDisposition
    current_step_id: str
    steps: list[GuidedStep] = Field(min_length=1)
    notice: str
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class DomainError(ContractModel):
    code: str
    category: ErrorCategory
    severity: Severity
    user_message: str
    technical_message: str
    object_reference: str | None = None
    remediation: str | None = None
    retryable: bool = False
    diagnostic_id: str = Field(default_factory=lambda: new_id("diag"))


PUBLIC_CONTRACTS: dict[str, type[ContractModel]] = {
    cls.__name__: cls
    for cls in (
        DataAsset,
        ModelObjectProvenance,
        StudyArea,
        OutletSpec,
        ConceptModelRecipe,
        StormCase,
        ProjectBundle,
        CandidateSlot,
        CostBasis,
        MetricSpec,
        QAPolicy,
        SimulationBudget,
        ExactRunResult,
        SearchEvent,
        SearchTrace,
        EnergyObservation,
        ExportManifest,
        EvidenceClaim,
        ModelFitnessPassport,
        ExportEligibility,
        FeasibilityRecord,
        DecisionCriterion,
        AlternativeDecisionSummary,
        DecisionBrief,
        AnalysisRunPlan,
        ProjectDataPolicy,
        ObservabilityBinding,
        ObservabilityAccountState,
        CloudSyncState,
        TelemetryEnvelope,
        TelemetryOutboxItem,
        CloudArtifactManifest,
        WandbRunReference,
        WeaveTraceReference,
        BenchmarkRunEnvelope,
        AssistantAnswer,
        AssistantEvaluationRecord,
        GuidedCheckpoint,
        ModelRevision,
        SemanticModelDiff,
        DomainError,
    )
}


def public_schemas() -> dict[str, dict[str, Any]]:
    """Return JSON Schemas for frontend type generation and RPC validation."""

    return {name: contract.model_json_schema() for name, contract in PUBLIC_CONTRACTS.items()}
