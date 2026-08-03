"""Versioned public contracts.

Pydantic v2 is used when installed.  A small compatibility implementation keeps
the CLI, fixtures, and tests functional in constrained/offline environments.  It
implements the Pydantic methods used by this project (`model_validate`,
`model_dump`, and `model_json_schema`) but is intentionally not a Pydantic clone.
"""

from __future__ import annotations

import copy
import json
import types
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Union, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "1.0"
CONCEPTUAL_NOTICE = (
    "Conceptual planning model — not for design, permitting, flood-risk "
    "determination, or representation of an existing drainage system."
)

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


class ReadinessLevel(str, Enum):
    DEMO = "DEMO"
    CONCEPTUAL = "CONCEPTUAL"
    ENGINEER_REVIEWED = "ENGINEER_REVIEWED"
    REFERENCE_MATCHED = "REFERENCE_MATCHED"


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


class ReadinessAttestation(ContractModel):
    key: str
    label: str
    accepted: bool = False
    reviewer: str | None = None
    accepted_at: datetime | None = None
    notes: str | None = None


class ModelReadiness(ContractModel):
    level: ReadinessLevel
    attestations: list[ReadinessAttestation] = Field(default_factory=list)
    unresolved_limitations: list[str] = Field(default_factory=list)
    reference_comparison_id: str | None = None
    reviewer: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def notice(self) -> str | None:
        return CONCEPTUAL_NOTICE if self.level == ReadinessLevel.CONCEPTUAL else None

    def _validate_contract(self) -> None:
        if self.level == ReadinessLevel.REFERENCE_MATCHED and not self.reference_comparison_id:
            raise ValueError("REFERENCE_MATCHED requires reference_comparison_id")
        if self.level == ReadinessLevel.ENGINEER_REVIEWED:
            if not self.attestations or not all(item.accepted for item in self.attestations):
                raise ValueError("ENGINEER_REVIEWED requires all attestations to be accepted")
            if not self.reviewer:
                raise ValueError("ENGINEER_REVIEWED requires a reviewer")


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
    readiness: ModelReadiness


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
    design_id: str
    storm_id: str
    exact: Literal[True] = True
    metrics: dict[str, float]
    qa_status: Literal["accepted", "warning", "rejected"] = "accepted"
    qa_findings: list[str] = Field(default_factory=list)
    input_sha256: str
    solver_sha256: str
    output_sha256: str
    cache_hit: bool = False
    completed_at: datetime


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
    readiness: ReadinessLevel
    created_at: datetime = Field(default_factory=utc_now)
    input_hashes: dict[str, str]
    solver_sha256: str
    extractor_version: str
    contract_version: str = SCHEMA_VERSION
    template_version: str
    files: dict[str, str] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    accepted_exceptions: list[str] = Field(default_factory=list)
    conceptual_notice: str | None = None

    def _validate_contract(self) -> None:
        if self.readiness == ReadinessLevel.CONCEPTUAL:
            self.conceptual_notice = CONCEPTUAL_NOTICE


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
        ReadinessAttestation,
        ModelReadiness,
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
        DomainError,
    )
}


def public_schemas() -> dict[str, dict[str, Any]]:
    """Return JSON Schemas for frontend type generation and RPC validation."""

    return {name: contract.model_json_schema() for name, contract in PUBLIC_CONTRACTS.items()}
