"""Shared, domain-neutral runtime for audited scenario applications."""

from .contracts import (
    ANALYSIS_TRANSITIONS,
    DIRECT_JOB_TRANSITIONS,
    AnalysisState,
    ContractModel,
    DirectJobState,
    ErrorCategory,
    EventSequencer,
    Field,
    SequencedEvent,
    Severity,
    TypedError,
    assert_transition,
    canonical_json,
    new_id,
    require_sha256,
)

__all__ = [
    "ANALYSIS_TRANSITIONS",
    "DIRECT_JOB_TRANSITIONS",
    "AnalysisState",
    "ContractModel",
    "DirectJobState",
    "ErrorCategory",
    "EventSequencer",
    "Field",
    "SequencedEvent",
    "Severity",
    "TypedError",
    "assert_transition",
    "canonical_json",
    "new_id",
    "require_sha256",
]
