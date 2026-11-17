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
from datetime import UTC, datetime, timedelta
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


class EvidenceAuthority(str, Enum):
    """Authority carried by a claim without implying that every value is hydraulic truth."""

    EXACT_SWMM = "exact_swmm"
    DERIVED_EXACT = "derived_exact"
    SOURCED_INPUT = "sourced_input"
    DECISION_RULE = "decision_rule"
    QA_RECORD = "qa_record"
    MODEL_FITNESS = "model_fitness"
    PROJECT_RECORD = "project_record"
    DOCUMENTATION = "documentation"


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
        "derived_metric",
        "decision_rule",
        "limitation",
    ]
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    authority: EvidenceAuthority = EvidenceAuthority.PROJECT_RECORD
    source_ids: list[str] = Field(default_factory=list)
    value: float | None = None
    units: str | None = None
    run_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    metric_id: str | None = None
    object_id: str = ""
    derivation: str | None = None
    exact: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if not self.statement.strip():
            raise ValueError("evidence claim statement cannot be empty")
        if any(not item.strip() for item in (*self.evidence_ids, *self.source_ids, *self.run_ids)):
            raise ValueError("evidence claim identifiers cannot be empty")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence claim evidence IDs must be unique")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("evidence claim source IDs must be unique")
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("evidence claim run IDs must be unique")
        if (self.value is None) != (self.units is None):
            raise ValueError("numeric evidence claims require both value and units")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("evidence claim values must be finite")
        if (
            self.exact
            and (self.run_id is None or self.metric_id is None or self.value is None)
            and self.authority != EvidenceAuthority.DERIVED_EXACT
        ):
            raise ValueError("exact evidence claims require run, metric, value, and units")
        if self.authority == EvidenceAuthority.EXACT_SWMM and (
            not self.exact or self.run_id is None or self.metric_id is None
        ):
            raise ValueError("exact SWMM authority requires one exact run and metric")
        if self.authority == EvidenceAuthority.DERIVED_EXACT and (
            not self.exact or not self.run_ids or not self.derivation or self.value is None
        ):
            raise ValueError(
                "derived exact authority requires exact source runs, a derivation, and a value"
            )
        if (
            self.authority
            in {
                EvidenceAuthority.SOURCED_INPUT,
                EvidenceAuthority.DECISION_RULE,
                EvidenceAuthority.QA_RECORD,
                EvidenceAuthority.MODEL_FITNESS,
                EvidenceAuthority.PROJECT_RECORD,
                EvidenceAuthority.DOCUMENTATION,
            }
            and self.exact
        ):
            raise ValueError("non-SWMM evidence authority cannot claim exact hydraulic status")


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


class WeaveConversationReference(ContractModel):
    """Acknowledged Weave Agents turn associated with one local assistant turn."""

    reference_id: str = Field(default_factory=lambda: new_id("weaveconversation"))
    project_id: str
    conversation_id: str
    turn_id: str
    entity: str
    external_project: str
    trace_id: str
    root_span_id: str
    url: str
    agent_id: str
    agent_version: str
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        for label, value in (
            ("project", self.project_id),
            ("conversation", self.conversation_id),
            ("turn", self.turn_id),
            ("entity", self.entity),
            ("external project", self.external_project),
            ("trace", self.trace_id),
            ("root span", self.root_span_id),
            ("agent", self.agent_id),
            ("agent version", self.agent_version),
        ):
            if not value.strip():
                raise ValueError(f"Weave {label} identifier cannot be empty")
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Weave conversation URL must be a credential-free HTTPS URL")


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
    kind: Literal[
        "project",
        "model_fitness",
        "alternative",
        "exact_run",
        "exact_metric",
        "derived_metric",
        "decision_rule",
        "cost_basis",
        "qa",
        "limitation",
        "artifact",
        "documentation",
    ]
    label: str
    source_id: str


class AssistantNumericClaim(ContractModel):
    label: str
    value: float
    units: str
    evidence_id: str
    authority: EvidenceAuthority = EvidenceAuthority.EXACT_SWMM
    source_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    metric_id: str | None = None
    object_id: str = ""
    derivation: str | None = None
    exact: bool = True

    def _validate_contract(self) -> None:
        if not math.isfinite(self.value):
            raise ValueError("assistant numeric claims must be finite")
        if not self.evidence_id.strip():
            raise ValueError("assistant numeric claims require an evidence ID")
        if len(set(self.source_ids)) != len(self.source_ids) or len(set(self.run_ids)) != len(
            self.run_ids
        ):
            raise ValueError("assistant numeric claim source IDs must be unique")
        if self.authority == EvidenceAuthority.EXACT_SWMM:
            if not self.exact or self.run_id is None or self.metric_id is None:
                raise ValueError("exact SWMM numeric claims require one exact run and metric")
        elif self.authority == EvidenceAuthority.DERIVED_EXACT:
            if not self.exact or not self.run_ids or not self.derivation:
                raise ValueError(
                    "derived exact numeric claims require source runs and a derivation"
                )
        elif self.exact:
            raise ValueError("sourced inputs and decision rules cannot claim exact SWMM status")


class AssistantProposedAction(ContractModel):
    action: Literal["navigate", "open_evidence", "review", "prepare_draft"]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)


class AssistantEvidenceAnchor(ContractModel):
    kind: Literal["project", "model_fitness", "alternative", "exact_run", "export"]
    identifier: str

    def _validate_contract(self) -> None:
        if not self.identifier.strip():
            raise ValueError("assistant evidence anchor identifier cannot be empty")


class AssistantEvidencePacket(ContractModel):
    packet_id: str
    project_id: str
    analysis_id: str | None = None
    model_revision_id: str | None = None
    anchor: AssistantEvidenceAnchor
    project_kind: Literal["demo", "conceptual", "existing"]
    evidence_references: list[AssistantEvidenceReference] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    capability_names: list[str] = Field(default_factory=list)
    packet_sha256: str
    local_authority: Literal[True] = True

    def _validate_contract(self) -> None:
        _require_sha256(self.packet_sha256, label="assistant evidence packet")
        for label, digest in self.source_hashes.items():
            if not label.strip():
                raise ValueError("assistant evidence source-hash labels cannot be empty")
            _require_sha256(digest, label=f"assistant evidence source {label!r}")
        evidence_ids = [item.evidence_id for item in self.evidence_references]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("assistant evidence packet references must be unique")
        claim_ids = [item.claim_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("assistant evidence packet claim IDs must be unique")
        if len(self.capability_names) != len(set(self.capability_names)):
            raise ValueError("assistant evidence packet capabilities must be unique")


class AssistantProviderPolicy(ContractModel):
    """Secret-free, per-project policy for W&B Inference and Weave Agents."""

    policy_id: str = Field(default_factory=lambda: new_id("providerpolicy"))
    project_id: str
    provider: Literal["wandb_inference"] = "wandb_inference"
    enabled: bool = False
    observability_binding_id: str | None = None
    external_project_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    response_schema_version: str | None = None
    response_schema_sha256: str | None = None
    tool_contract_version: str | None = None
    validator_version: str | None = None
    agent_variant_id: Literal["A0", "A1", "A2", "A3", "A4"] = "A0"
    # A policy is created before any turn, so this field pins the finite plan
    # registry identifier. AssistantAnswer uses the same public field for the
    # specific plan selected for that turn.
    explanation_plan_id: str | None = None
    provider_call_count: int | None = Field(default=None, ge=1, le=2)
    release_gate_digest: str | None = None
    release_basis: Literal["automated_conformance"] = "automated_conformance"
    practitioner_usefulness_status: Literal["not_measured"] = "not_measured"
    inference_consent: bool = False
    content_logging_consent: bool = False
    consent_actor: str | None = None
    consented_at: datetime | None = None
    critical_infrastructure_prohibited: Literal[True] = True
    allowed_data_categories: list[str] = Field(default_factory=lambda: ["structured_evidence"])
    max_tool_calls: int = Field(default=6, ge=1, le=12)
    max_turn_seconds: int = Field(default=60, ge=5, le=300)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if len(set(self.allowed_data_categories)) != len(self.allowed_data_categories):
            raise ValueError("assistant provider data categories must be unique")
        if any(not item.strip() for item in self.allowed_data_categories):
            raise ValueError("assistant provider data categories cannot be empty")
        if self.content_logging_consent and not self.inference_consent:
            raise ValueError("content logging consent requires inference consent")
        if not self.enabled:
            if self.inference_consent or self.content_logging_consent:
                raise ValueError("disabled assistant policy cannot retain active consent")
            if self.consent_actor is not None or self.consented_at is not None:
                raise ValueError("disabled assistant policy cannot claim a consent record")
            return
        if not self.inference_consent or not all(
            (
                self.model_id,
                self.observability_binding_id,
                self.external_project_id,
                self.prompt_version,
                self.response_schema_version,
                self.response_schema_sha256,
                self.tool_contract_version,
                self.validator_version,
                self.consent_actor,
                self.consented_at,
            )
        ):
            raise ValueError(
                "enabled W&B Inference policy requires a pinned model, prompt/tool/validator "
                "schema/tool/validator versions, and an acknowledged inference-consent record"
            )
        if "structured_evidence" not in self.allowed_data_categories:
            raise ValueError("assistant provider policy must allow structured evidence")
        for label, value in (
            ("observability binding", self.observability_binding_id),
            ("external project", self.external_project_id),
            ("model", self.model_id),
            ("prompt", self.prompt_version),
            ("response schema", self.response_schema_version),
            ("tool contract", self.tool_contract_version),
            ("validator", self.validator_version),
            ("consent actor", self.consent_actor),
        ):
            if value is None or not value.strip():
                raise ValueError(f"assistant provider {label} cannot be empty")
        if self.response_schema_sha256 is None:
            raise ValueError("assistant provider response schema hash cannot be empty")
        _require_sha256(
            self.response_schema_sha256,
            label="assistant provider response schema",
        )
        if self.agent_variant_id in {"A1", "A2", "A3"}:
            expected_calls = {"A1": 1, "A2": 2, "A3": 2}[self.agent_variant_id]
            if self.provider_call_count != expected_calls:
                raise ValueError(
                    f"assistant provider variant {self.agent_variant_id} requires "
                    f"{expected_calls} logical provider call(s)"
                )
            if self.explanation_plan_id is None or not self.explanation_plan_id.strip():
                raise ValueError(
                    "production Evidence Guide policies require a finite explanation-plan registry"
                )
            if self.release_gate_digest is None:
                raise ValueError("production Evidence Guide policies require a release-gate digest")
            _require_sha256(
                self.release_gate_digest,
                label="assistant provider release gate",
            )


class AssistantReleaseBasis(ContractModel):
    """Immutable automated-conformance basis for one production assistant release.

    This record deliberately does not claim practitioner usefulness.  It binds the
    finite explanation architecture and every repository-controlled component used
    to make an automated release decision.
    """

    basis_id: str = Field(default_factory=lambda: new_id("assistantreleasebasis"))
    release_gate_digest: str
    decision: Literal["ENABLE_LIVE_GUIDE", "DISABLE_LIVE_GUIDE"]
    agent_variant_id: Literal["D1", "A1", "A2", "A3"]
    model_id: str | None = None
    provider_call_count: int = Field(ge=0, le=2)
    prompt_sha256: str
    response_schema_sha256: str
    tool_contract_sha256: str
    plan_registry_sha256: str
    renderer_sha256: str
    validator_sha256: str
    dataset_sha256: str
    judge_config_sha256: str
    receipt_sha256: str
    protocol_amendment_sha256: str
    automated_gate_results: dict[str, bool]
    production_scope: list[Literal["demo", "standard_engineering"]]
    release_basis: Literal["automated_conformance"] = "automated_conformance"
    practitioner_usefulness_status: Literal["not_measured"] = "not_measured"
    critical_infrastructure_prohibited: Literal[True] = True
    issued_at: datetime
    expires_at: datetime

    def _validate_contract(self) -> None:
        for label, digest in (
            ("release gate", self.release_gate_digest),
            ("prompt", self.prompt_sha256),
            ("response schema", self.response_schema_sha256),
            ("tool contract", self.tool_contract_sha256),
            ("plan registry", self.plan_registry_sha256),
            ("renderer", self.renderer_sha256),
            ("validator", self.validator_sha256),
            ("dataset", self.dataset_sha256),
            ("judge config", self.judge_config_sha256),
            ("receipt", self.receipt_sha256),
            ("protocol amendment", self.protocol_amendment_sha256),
        ):
            _require_sha256(digest, label=f"assistant release {label}")
        expected_calls = {"D1": 0, "A1": 1, "A2": 2, "A3": 2}[self.agent_variant_id]
        if self.provider_call_count != expected_calls:
            raise ValueError(
                f"assistant release variant {self.agent_variant_id} requires "
                f"{expected_calls} logical provider call(s)"
            )
        if self.agent_variant_id == "D1":
            if self.model_id is not None:
                raise ValueError("deterministic D1 release basis cannot pin a provider model")
        elif self.model_id is None or not self.model_id.strip():
            raise ValueError("LLM Evidence Guide release basis requires a pinned model")
        if not self.automated_gate_results or any(
            not key.strip() for key in self.automated_gate_results
        ):
            raise ValueError("assistant release basis requires named automated gates")
        if self.decision == "ENABLE_LIVE_GUIDE" and not all(self.automated_gate_results.values()):
            raise ValueError("an enabled assistant release requires every automated gate")
        if len(self.production_scope) != len(set(self.production_scope)):
            raise ValueError("assistant release production scope must be unique")
        if self.decision == "ENABLE_LIVE_GUIDE":
            if self.agent_variant_id == "D1":
                raise ValueError("deterministic D1 cannot enable the live LLM guide")
            if set(self.production_scope) != {"demo", "standard_engineering"}:
                raise ValueError(
                    "enabled assistant release scope must be demo and standard_engineering"
                )
        elif self.production_scope:
            raise ValueError("disabled assistant release basis cannot claim production scope")
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("assistant release timestamps must include a UTC offset")
        if self.expires_at <= self.issued_at:
            raise ValueError("assistant release expiry must follow issuance")
        if self.expires_at - self.issued_at > timedelta(days=90):
            raise ValueError("assistant release basis cannot remain valid for more than 90 days")


# Internal compatibility alias while the unreleased public schema moves to the
# more explicit contract name requested for schema 2.0.
ProviderPolicy = AssistantProviderPolicy


class AssistantConversation(ContractModel):
    conversation_id: str = Field(default_factory=lambda: new_id("assistantconversation"))
    project_id: str
    analysis_id: str | None = None
    model_revision_id: str | None = None
    provider_policy_id: str
    packet_id: str
    packet_sha256: str
    anchor: AssistantEvidenceAnchor
    title: str
    status: Literal["active", "closed"] = "active"
    weave_reference_id: str | None = None
    weave_url: str | None = None
    next_turn_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if not self.title.strip():
            raise ValueError("assistant conversation title cannot be empty")
        if not self.packet_id.strip():
            raise ValueError("assistant conversation packet ID cannot be empty")
        _require_sha256(self.packet_sha256, label="assistant conversation evidence packet")
        if (self.weave_reference_id is None) != (self.weave_url is None):
            raise ValueError(
                "assistant conversation Weave reference and URL must be stored together"
            )
        if self.weave_url is not None:
            parsed = urllib.parse.urlparse(self.weave_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("assistant conversation Weave URL must be credential-free HTTPS")


class AssistantTurn(ContractModel):
    turn_id: str = Field(default_factory=lambda: new_id("assistantturn"))
    conversation_id: str
    project_id: str
    sequence: int = Field(ge=0)
    user_message: str = Field(min_length=1, max_length=2_000)
    anchor: AssistantEvidenceAnchor
    evidence_packet_id: str
    evidence_packet_sha256: str
    state: Literal["pending", "completed", "blocked", "failed"] = "pending"
    answer_id: str | None = None
    error_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def _validate_contract(self) -> None:
        if not self.evidence_packet_id.strip():
            raise ValueError("assistant turn evidence packet ID cannot be empty")
        _require_sha256(self.evidence_packet_sha256, label="assistant turn evidence packet")
        if self.state == "completed" and (self.answer_id is None or self.completed_at is None):
            raise ValueError("completed assistant turns require an answer and completion time")
        if self.state in {"blocked", "failed"} and (
            self.error_code is None or self.completed_at is None
        ):
            raise ValueError(
                "blocked or failed assistant turns require an error and completion time"
            )
        if self.state == "pending" and any((self.answer_id, self.error_code, self.completed_at)):
            raise ValueError("pending assistant turns cannot contain a terminal result")


class AssistantAnswer(ContractModel):
    answer_id: str = Field(default_factory=lambda: new_id("answer"))
    call_id: str
    project_id: str
    conversation_id: str | None = None
    turn_id: str | None = None
    evidence_packet_id: str | None = None
    evidence_packet_sha256: str | None = None
    intent: str
    answer: str
    evidence_references: list[AssistantEvidenceReference] = Field(default_factory=list)
    numeric_claims: list[AssistantNumericClaim] = Field(default_factory=list)
    assumptions_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    proposed_actions: list[AssistantProposedAction] = Field(default_factory=list)
    weave_trace: WeaveTraceReference | None = None
    weave_conversation: WeaveConversationReference | None = None
    provider_model_id: str | None = None
    provider_call_ids: list[str] = Field(default_factory=list)
    provider_input_tokens: int = Field(default=0, ge=0)
    provider_output_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int = Field(default=0, ge=0)
    provider_transport_attempts: int = Field(default=0, ge=0)
    provider_latency_ms: float = Field(default=0.0, ge=0)
    evidence_tool_calls: list[str] = Field(default_factory=list)
    agent_variant_id: Literal["A0", "A1", "A2", "A3", "A4"] = "A0"
    explanation_plan_id: str | None = None
    provider_call_count: int | None = Field(default=None, ge=1, le=2)
    release_gate_digest: str | None = None
    release_basis: Literal["automated_conformance"] = "automated_conformance"
    practitioner_usefulness_status: Literal["not_measured"] = "not_measured"
    prompt_version: str | None = None
    response_schema_version: str | None = None
    response_schema_sha256: str | None = None
    tool_contract_version: str | None = None
    validator_version: str | None = None
    requires_engineer_confirmation: Literal[True] = True
    read_only: Literal[True] = True
    created_at: datetime = Field(default_factory=utc_now)

    def _validate_contract(self) -> None:
        if (self.weave_trace is None) == (self.weave_conversation is None):
            raise ValueError("assistant answers require exactly one acknowledged Weave reference")
        if any(
            (
                self.conversation_id,
                self.turn_id,
                self.evidence_packet_id,
                self.evidence_packet_sha256,
            )
        ) and not all(
            (
                self.conversation_id,
                self.turn_id,
                self.evidence_packet_id,
                self.evidence_packet_sha256,
            )
        ):
            raise ValueError(
                "conversation-backed assistant answers require turn, packet ID, and packet hash"
            )
        if self.evidence_packet_id is not None and not self.evidence_packet_id.strip():
            raise ValueError("assistant answer evidence packet ID cannot be empty")
        if self.evidence_packet_sha256 is not None:
            _require_sha256(self.evidence_packet_sha256, label="assistant answer evidence packet")
        if self.weave_conversation is not None and (
            self.conversation_id != self.weave_conversation.conversation_id
            or self.turn_id != self.weave_conversation.turn_id
        ):
            raise ValueError("assistant answer and Weave conversation identifiers must match")
        if self.provider_total_tokens != (self.provider_input_tokens + self.provider_output_tokens):
            raise ValueError("assistant provider total tokens must equal input plus output tokens")
        if len(set(self.provider_call_ids)) != len(self.provider_call_ids):
            raise ValueError("assistant provider call IDs must be unique")
        if self.weave_conversation is not None:
            actual_call_count = len(self.provider_call_ids)
            expected_call_count = {
                "A0": 2,
                "A1": 1,
                "A2": 2,
                "A3": 2,
                "A4": 2,
            }[self.agent_variant_id]
            if actual_call_count != expected_call_count:
                raise ValueError(
                    f"conversation-backed {self.agent_variant_id} answers require "
                    f"{expected_call_count} logical provider call(s)"
                )
            if (
                self.provider_call_count is not None
                and self.provider_call_count != actual_call_count
            ):
                raise ValueError("assistant provider call count must match the recorded call IDs")
            if not actual_call_count <= self.provider_transport_attempts <= (actual_call_count * 2):
                raise ValueError(
                    "conversation-backed answers allow at most one transient retry per call"
                )
            if not 1 <= len(self.evidence_tool_calls) <= 4:
                raise ValueError(
                    "conversation-backed answers require one to four evidence tool calls"
                )
            for label, value in (
                ("provider model", self.provider_model_id),
                ("prompt version", self.prompt_version),
                ("response schema version", self.response_schema_version),
                ("tool contract version", self.tool_contract_version),
                ("validator version", self.validator_version),
            ):
                if value is None or not value.strip():
                    raise ValueError(f"conversation-backed answer {label} cannot be empty")
            if self.response_schema_sha256 is None:
                raise ValueError("conversation-backed answer response schema hash cannot be empty")
            _require_sha256(
                self.response_schema_sha256,
                label="assistant answer response schema",
            )
            if self.agent_variant_id in {"A1", "A2", "A3"}:
                if self.explanation_plan_id is None or not self.explanation_plan_id.strip():
                    raise ValueError(
                        "production Evidence Guide answers require a finite explanation plan"
                    )
                if self.provider_call_count is None:
                    raise ValueError(
                        "production Evidence Guide answers require a provider call count"
                    )
                if self.release_gate_digest is None:
                    raise ValueError(
                        "production Evidence Guide answers require a release-gate digest"
                    )
                _require_sha256(
                    self.release_gate_digest,
                    label="assistant answer release gate",
                )


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
        WeaveConversationReference,
        BenchmarkRunEnvelope,
        AssistantEvidenceAnchor,
        AssistantEvidencePacket,
        AssistantReleaseBasis,
        ProviderPolicy,
        AssistantConversation,
        AssistantTurn,
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
