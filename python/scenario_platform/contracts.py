"""Domain-neutral contract primitives used by scenario applications.

The public surface follows the small subset of Pydantic used by the workers.
Pydantic is used in packaged builds; the fallback keeps security and repository
tests runnable in lean/offline environments without importing either domain.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import types
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar, Literal, Union, get_args, get_origin, get_type_hints

_ID_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


try:  # pragma: no cover - exercised by packaged builds
    from pydantic import BaseModel as _PydanticBaseModel
    from pydantic import ConfigDict, Field

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

except ImportError:  # pragma: no cover - default in the lean test environment
    _MISSING = object()

    class _FieldSpec:
        def __init__(
            self,
            default: Any = _MISSING,
            *,
            default_factory: Any = None,
            ge: float | None = None,
            gt: float | None = None,
            min_length: int | None = None,
        ) -> None:
            self.default = default
            self.default_factory = default_factory
            self.ge = ge
            self.gt = gt
            self.min_length = min_length

        def value(self) -> Any:
            if self.default_factory is not None:
                return self.default_factory()
            if self.default is not _MISSING:
                return copy.deepcopy(self.default)
            raise ValueError("field is required")

        def check(self, name: str, value: Any) -> None:
            if value is None:
                return
            if self.ge is not None and value < self.ge:
                raise ValueError(f"{name} must be >= {self.ge}")
            if self.gt is not None and value <= self.gt:
                raise ValueError(f"{name} must be > {self.gt}")
            if self.min_length is not None and len(value) < self.min_length:
                raise ValueError(f"{name} must contain at least {self.min_length} items")

    def Field(  # type: ignore[misc]
        default: Any = _MISSING,
        *,
        default_factory: Any = None,
        ge: float | None = None,
        gt: float | None = None,
        min_length: int | None = None,
    ) -> _FieldSpec:
        return _FieldSpec(
            default,
            default_factory=default_factory,
            ge=ge,
            gt=gt,
            min_length=min_length,
        )

    def _coerce(annotation: Any, value: Any) -> Any:
        if value is None:
            return None
        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin in (Union, types.UnionType):
            for option in arguments:
                if option is type(None):
                    continue
                try:
                    return _coerce(option, value)
                except (TypeError, ValueError):
                    continue
            return value
        if origin is Literal:
            if value not in arguments:
                raise ValueError(f"expected one of {arguments!r}")
            return value
        if origin is list:
            subtype = arguments[0] if arguments else Any
            return [_coerce(subtype, item) for item in value]
        if origin is dict:
            key_type, value_type = arguments if arguments else (Any, Any)
            return {
                _coerce(key_type, key): _coerce(value_type, item) for key, item in value.items()
            }
        if annotation is Any:
            return value
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return value if isinstance(value, annotation) else annotation(value)
        if isinstance(annotation, type) and issubclass(annotation, ContractModel):
            return value if isinstance(value, annotation) else annotation.model_validate(value)
        if annotation in (str, int, float, bool):
            if annotation is bool and isinstance(value, str):
                return value.casefold() in {"true", "yes", "1"}
            return annotation(value)
        return value

    def _dump(value: Any) -> Any:
        if isinstance(value, ContractModel):
            return value.model_dump(mode="json")
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, list):
            return [_dump(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _dump(item) for key, item in value.items()}
        return value

    class ContractModel:
        """Strict, typed compatibility model for offline test environments."""

        def __init__(self, **values: Any) -> None:
            annotations: dict[str, Any] = {}
            for base in reversed(self.__class__.__mro__):
                try:
                    annotations.update(get_type_hints(base))
                except (NameError, TypeError):
                    annotations.update(getattr(base, "__annotations__", {}))
            annotations = {
                key: annotation
                for key, annotation in annotations.items()
                if not key.startswith("_") and get_origin(annotation) is not ClassVar
            }
            unexpected = set(values) - set(annotations)
            if unexpected:
                raise ValueError(f"unexpected fields: {', '.join(sorted(unexpected))}")
            for name, annotation in annotations.items():
                declared = getattr(self.__class__, name, _MISSING)
                if name in values:
                    raw = values[name]
                elif isinstance(declared, _FieldSpec):
                    raw = declared.value()
                elif declared is not _MISSING:
                    raw = copy.deepcopy(declared)
                else:
                    raise ValueError(f"{name} is required")
                value = _coerce(annotation, raw)
                if isinstance(declared, _FieldSpec):
                    declared.check(name, value)
                setattr(self, name, value)
            self._validate_contract()

        @classmethod
        def model_validate(cls, value: Any) -> ContractModel:
            if isinstance(value, cls):
                return value
            if not isinstance(value, dict):
                raise TypeError(f"{cls.__name__} expects an object")
            return cls(**value)

        def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
            del mode
            annotations: dict[str, Any] = {}
            for base in reversed(self.__class__.__mro__):
                annotations.update(getattr(base, "__annotations__", {}))
            return {
                name: _dump(getattr(self, name))
                for name, annotation in annotations.items()
                if not name.startswith("_") and get_origin(annotation) is not ClassVar
            }

        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": cls.__name__,
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: {"title": key}
                    for key in getattr(cls, "__annotations__", {})
                    if not key.startswith("_")
                },
            }


def new_id(prefix: str) -> str:
    """Create a sortable-enough, opaque identifier with a validated namespace."""

    if not _ID_PREFIX.fullmatch(prefix):
        raise ValueError("ID prefix must be lowercase alphanumeric snake case")
    return f"{prefix}_{uuid.uuid4().hex}"


def require_sha256(value: str, *, label: str = "sha256") -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def canonical_json(value: Any) -> str:
    if isinstance(value, ContractModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


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


class DirectJobState(str, Enum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    QA_REJECTED = "QA_REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKING = "BLOCKING"


class ErrorCategory(str, Enum):
    USER_FIXABLE = "USER_FIXABLE"
    MODEL_QUALITY = "MODEL_QUALITY"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    DATA_PROVIDER = "DATA_PROVIDER"
    ENGINE_FAILURE = "ENGINE_FAILURE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    SECURITY = "SECURITY"
    INTERNAL = "INTERNAL"


ANALYSIS_TRANSITIONS: dict[AnalysisState, frozenset[AnalysisState]] = {
    AnalysisState.DRAFT: frozenset({AnalysisState.VALIDATING, AnalysisState.CANCELLED}),
    AnalysisState.VALIDATING: frozenset(
        {AnalysisState.READY, AnalysisState.FAILED, AnalysisState.CANCELLED}
    ),
    AnalysisState.READY: frozenset({AnalysisState.RUNNING, AnalysisState.CANCELLED}),
    AnalysisState.RUNNING: frozenset(
        {
            AnalysisState.PAUSING,
            AnalysisState.COMPLETED,
            AnalysisState.FAILED,
            AnalysisState.CANCELLED,
        }
    ),
    AnalysisState.PAUSING: frozenset(
        {AnalysisState.PAUSED, AnalysisState.FAILED, AnalysisState.CANCELLED}
    ),
    AnalysisState.PAUSED: frozenset({AnalysisState.RUNNING, AnalysisState.CANCELLED}),
    AnalysisState.COMPLETED: frozenset(),
    AnalysisState.FAILED: frozenset(),
    AnalysisState.CANCELLED: frozenset(),
}

DIRECT_JOB_TRANSITIONS: dict[DirectJobState, frozenset[DirectJobState]] = {
    DirectJobState.PLANNED: frozenset({DirectJobState.QUEUED, DirectJobState.CANCELLED}),
    DirectJobState.QUEUED: frozenset({DirectJobState.RUNNING, DirectJobState.CANCELLED}),
    DirectJobState.RUNNING: frozenset(
        {
            DirectJobState.SUCCEEDED,
            DirectJobState.QA_REJECTED,
            DirectJobState.FAILED,
            DirectJobState.CANCELLED,
            DirectJobState.QUEUED,
        }
    ),
    DirectJobState.SUCCEEDED: frozenset(),
    DirectJobState.QA_REJECTED: frozenset(),
    DirectJobState.FAILED: frozenset(),
    DirectJobState.CANCELLED: frozenset(),
}


def assert_transition(
    current: AnalysisState | DirectJobState | str,
    target: AnalysisState | DirectJobState | str,
    *,
    kind: Literal["analysis", "direct_job"],
) -> None:
    graph: Any
    if kind == "analysis":
        enum_type, graph = AnalysisState, ANALYSIS_TRANSITIONS
    else:
        enum_type, graph = DirectJobState, DIRECT_JOB_TRANSITIONS
    source = enum_type(current)
    destination = enum_type(target)
    if destination not in graph[source]:
        raise ValueError(f"invalid {kind} state transition: {source.value} -> {destination.value}")


class TypedError(ContractModel):
    code: str
    category: ErrorCategory
    severity: Severity
    user_message: str
    technical_message: str = ""
    object_reference: str | None = None
    remediation: str | None = None
    retryable: bool = False
    diagnostic_id: str = Field(default_factory=lambda: new_id("diag"))

    def _validate_contract(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", self.code):
            raise ValueError("error code must be stable uppercase snake case")
        if not self.user_message:
            raise ValueError("user_message is required")


class SequencedEvent(ContractModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    stream_id: str
    sequence: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def _validate_contract(self) -> None:
        if not self.stream_id or not self.event_type:
            raise ValueError("stream_id and event_type are required")


class EventSequencer:
    """Thread-safe monotonic sequence allocator for ordered worker events."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def observe(self, event: SequencedEvent | dict[str, Any]) -> SequencedEvent:
        parsed = SequencedEvent.model_validate(event)
        with self._lock:
            expected = self._last.get(parsed.stream_id, 0) + 1
            if parsed.sequence != expected:
                raise ValueError(
                    f"event sequence gap for {parsed.stream_id}: "
                    f"expected {expected}, received {parsed.sequence}"
                )
            self._last[parsed.stream_id] = parsed.sequence
        return parsed

    def next(
        self,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> SequencedEvent:
        with self._lock:
            sequence = self._last.get(stream_id, 0) + 1
            self._last[stream_id] = sequence
        return SequencedEvent(
            stream_id=stream_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload or {},
        )
