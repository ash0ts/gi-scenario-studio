from __future__ import annotations

import hashlib

import pytest
from ecocorridor_core.review_brief_evaluation import ProviderExecutionEvidence


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provider(*, call_id: str, trace_id: str, trace_url: str) -> ProviderExecutionEvidence:
    return ProviderExecutionEvidence(
        provider="provider",
        model="model-1",
        provider_call_id=f"sha256:{_sha(call_id)}",
        provider_call_identity_kind="WEAVE_GENERATION_CALL_ID",
        provider_response_sha256=_sha(f"response:{call_id}"),
        weave_call_id=call_id,
        weave_trace_id=trace_id,
        weave_trace_url=trace_url,
        trace_status="VERIFIED",
        trace_readback_verified=True,
        prompt_tokens=11,
        completion_tokens=7,
        latency_ms=13,
    )


def test_explicit_weave_call_permalink_requires_exact_root_call_id() -> None:
    call_id = "019fd920-0c1a-7fb7-a3f8-21ecd827c2a5"
    trace_id = "019fd920-0c1a-7bc5-8bb6-711d42989212"
    assert _provider(
        call_id=call_id,
        trace_id=trace_id,
        trace_url=f"https://wandb.ai/entity/project/weave/r/call/{call_id}",
    )

    with pytest.raises(ValueError, match="exact Weave root call"):
        _provider(
            call_id=call_id,
            trace_id=trace_id,
            trace_url="https://wandb.ai/entity/project/weave/r/call/another-call",
        )


@pytest.mark.parametrize("terminal_kind", ["call", "trace"])
def test_weave_trace_view_accepts_exact_call_or_trace_identity(terminal_kind: str) -> None:
    call_id = "019fd920-0c1a-7fb7-a3f8-21ecd827c2a5"
    trace_id = "019fd920-0c1a-7bc5-8bb6-711d42989212"
    terminal = call_id if terminal_kind == "call" else trace_id

    assert _provider(
        call_id=call_id,
        trace_id=trace_id,
        trace_url=f"https://wandb.ai/entity/project/weave/traces/{terminal}",
    )


def test_weave_trace_view_rejects_an_unrelated_terminal() -> None:
    with pytest.raises(ValueError, match="exact Weave root call or trace ID"):
        _provider(
            call_id="call-1",
            trace_id="trace-1",
            trace_url="https://wandb.ai/entity/project/weave/traces/unrelated",
        )
