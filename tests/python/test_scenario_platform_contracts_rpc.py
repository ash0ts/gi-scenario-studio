from __future__ import annotations

import pytest
from scenario_platform.contracts import (
    AnalysisState,
    ContractModel,
    ErrorCategory,
    EventSequencer,
    Severity,
    TypedError,
    assert_transition,
)
from scenario_platform.rpc import (
    RpcProtocolError,
    RuntimeContractRegistry,
    decode_jsonl,
    encode_jsonl,
)


class EchoRequest(ContractModel):
    message: str


class EchoResponse(ContractModel):
    echoed: str


def test_typed_errors_and_state_transitions_are_strict() -> None:
    error = TypedError(
        code="MODEL_GRID_MISMATCH",
        category=ErrorCategory.MODEL_QUALITY,
        severity=Severity.BLOCKING,
        user_message="The grids do not align.",
        remediation="Review the alignment plan.",
    )
    assert error.code == "MODEL_GRID_MISMATCH"
    assert_transition(AnalysisState.DRAFT, AnalysisState.VALIDATING, kind="analysis")
    with pytest.raises(ValueError, match="invalid analysis state transition"):
        assert_transition(AnalysisState.DRAFT, AnalysisState.COMPLETED, kind="analysis")
    with pytest.raises(ValueError, match="uppercase snake case"):
        TypedError(
            code="bad-code",
            category=ErrorCategory.INTERNAL,
            severity=Severity.ERROR,
            user_message="Invalid.",
        )


def test_event_sequences_are_monotonic_per_stream() -> None:
    allocator = EventSequencer()
    first = allocator.next("analysis-1", "analysis.started")
    second = allocator.next("analysis-1", "analysis.progress")
    other = allocator.next("analysis-2", "analysis.started")
    assert (first.sequence, second.sequence, other.sequence) == (1, 2, 1)

    observer = EventSequencer()
    observer.observe(first)
    with pytest.raises(ValueError, match="sequence gap"):
        observer.observe(
            {
                "stream_id": "analysis-1",
                "sequence": 3,
                "event_type": "analysis.completed",
                "payload": {},
            }
        )


def test_jsonl_is_strict_and_contract_registry_fails_closed() -> None:
    registry = RuntimeContractRegistry(allowed_prefixes=("connectivity.",))
    registry.register_method("connectivity.echo", EchoRequest, EchoResponse)
    request, params = registry.validate_request(
        b'{"jsonrpc":"2.0","id":"one","method":"connectivity.echo","params":{"message":"hello"}}\n'
    )
    assert request.method == "connectivity.echo"
    assert params.message == "hello"
    response = registry.validate_response("connectivity.echo", {"echoed": "hello"})
    assert decode_jsonl(encode_jsonl(response)) == {"echoed": "hello"}

    with pytest.raises(RpcProtocolError) as unknown:
        registry.validate_request(
            b'{"jsonrpc":"2.0","id":1,"method":"connectivity.noop","params":{}}\n'
        )
    assert unknown.value.code == -32601
    with pytest.raises(RpcProtocolError, match="invalid JSONL"):
        decode_jsonl(b'{"value":NaN}\n')
    with pytest.raises(RpcProtocolError, match="exactly one"):
        decode_jsonl(b"{}\n{}\n")
    with pytest.raises(RpcProtocolError, match="invalid JSONL"):
        decode_jsonl(b'{"id":1,"id":2}\n')
    with pytest.raises(RpcProtocolError, match="invalid JSON-RPC"):
        registry.validate_request(
            {"jsonrpc": "2.0", "id": True, "method": "connectivity.echo", "params": {}}
        )
    with pytest.raises(ValueError, match="outside the configured allowlist"):
        registry.register_method("gi.echo", EchoRequest, EchoResponse)
