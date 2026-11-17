from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from gi_scenario_core.inference import (
    InferenceRequestError,
    WandbInferenceAdapter,
    _NoRedirectHandler,
)


class Credentials:
    @contextmanager
    def reveal(self):
        yield "test-key-that-never-leaves-the-adapter"


def test_inference_uses_explicit_project_and_parses_tool_calls() -> None:
    seen: dict[str, object] = {}

    def transport(**kwargs):
        seen.update(kwargs)
        return 200, json.dumps(
            {
                "id": "provider-1",
                "model": "ibm-granite/granite-4.1-8b",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "tool-1",
                                    "function": {
                                        "name": "evidence.resolve",
                                        "arguments": '{"evidence_id":"claim-1"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 4,
                    "total_tokens": 34,
                },
            }
        ).encode()

    client = WandbInferenceAdapter(Credentials(), transport=transport)
    completion = client.complete(
        entity="stormwater-team",
        project="cedar-trace",
        model="ibm-granite/granite-4.1-8b",
        messages=[{"role": "user", "content": "Why does this fail?"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "evidence.resolve",
                    "description": "Resolve a canonical evidence claim.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert seen["url"] == "https://api.inference.wandb.ai/v1/chat/completions"
    assert seen["headers"]["OpenAI-Project"] == "stormwater-team/cedar-trace"
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    body = json.loads(seen["payload"])
    assert body["temperature"] == 0
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert completion.tool_calls[0].name == "evidence.resolve"
    assert completion.tool_calls[0].arguments == {"evidence_id": "claim-1"}
    assert completion.usage.total_tokens == 34


def test_inference_retries_only_transient_failures_once() -> None:
    statuses = iter([503, 200])
    calls = 0

    def transport(**_kwargs):
        nonlocal calls
        calls += 1
        status = next(statuses)
        if status != 200:
            return status, b"temporarily unavailable"
        return status, json.dumps(
            {
                "id": "provider-2",
                "model": "model",
                "choices": [
                    {"message": {"content": "{}"}, "finish_reason": "stop"}
                ],
                "usage": {},
            }
        ).encode()

    client = WandbInferenceAdapter(Credentials(), transport=transport)
    completion = client.complete(
        entity="team",
        project="project",
        model="model",
        messages=[{"role": "user", "content": "question"}],
    )
    assert completion.content == "{}"
    assert completion.transport_attempts == 2
    assert calls == 2


def test_inference_can_disable_transport_retries_for_paired_experiments() -> None:
    calls = 0

    def transport(**_kwargs):
        nonlocal calls
        calls += 1
        return 503, b"temporarily unavailable"

    client = WandbInferenceAdapter(
        Credentials(),
        transport=transport,
        max_transport_attempts=1,
    )
    with pytest.raises(InferenceRequestError) as raised:
        client.complete(
            entity="team",
            project="project",
            model="model",
            messages=[{"role": "user", "content": "question"}],
        )

    assert raised.value.code == "INFERENCE_UNAVAILABLE"
    assert raised.value.transport_attempts == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (402, "INFERENCE_CREDITS_REQUIRED"),
        (403, "INFERENCE_AUTHORIZATION_REQUIRED"),
        (404, "INFERENCE_MODEL_UNAVAILABLE"),
        (429, "INFERENCE_RATE_LIMITED"),
        (503, "INFERENCE_UNAVAILABLE"),
    ],
)
def test_inference_normalizes_provider_errors(status: int, code: str) -> None:
    client = WandbInferenceAdapter(
        Credentials(),
        transport=lambda **_kwargs: (status, b"provider details are not retained"),
    )
    with pytest.raises(RuntimeError, match=code):
        client.complete(
            entity="team",
            project="project",
            model="model",
            messages=[{"role": "user", "content": "question"}],
        )


def test_inference_rejects_non_wandb_service_origins() -> None:
    with pytest.raises(ValueError, match="approved HTTPS service origin"):
        WandbInferenceAdapter(Credentials(), base_url="https://example.com/v1")


def test_inference_turn_budget_is_forwarded_to_the_transport() -> None:
    seen: list[float] = []

    def transport(**kwargs):
        seen.append(kwargs["timeout_seconds"])
        return 200, json.dumps(
            {
                "id": "provider-budget",
                "model": "model",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {},
            }
        ).encode()

    client = WandbInferenceAdapter(Credentials(), transport=transport, timeout_seconds=30)
    client.complete(
        entity="team",
        project="project",
        model="model",
        messages=[{"role": "user", "content": "question"}],
        timeout_seconds=0.25,
    )
    assert len(seen) == 1
    assert 0 < seen[0] <= 0.25


def test_inference_request_byte_limit_is_a_typed_zero_transport_error() -> None:
    called = False

    def transport(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("an oversized local payload must not reach the transport")

    client = WandbInferenceAdapter(
        Credentials(),
        transport=transport,
        max_request_bytes=1_024,
    )
    with pytest.raises(InferenceRequestError) as raised:
        client.complete(
            entity="team",
            project="project",
            model="model",
            messages=[{"role": "user", "content": "x" * 2_000}],
        )

    assert raised.value.code == "INFERENCE_INPUT_ENVELOPE_EXCEEDED"
    assert raised.value.transport_attempts == 0
    assert raised.value.request_bytes > client.max_request_bytes
    assert called is False


def test_default_transport_refuses_every_redirect() -> None:
    assert _NoRedirectHandler().redirect_request(
        object(), None, 302, "Found", {}, "https://attacker.invalid"
    ) is None
