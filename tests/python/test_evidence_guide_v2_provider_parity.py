from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gi_scenario_core.evidence_evaluation_v2 import provider_packet
from gi_scenario_core.evidence_guide_v2_campaign import AttemptSpec, load_corpus
from gi_scenario_core.evidence_guide_v2_protocol import (
    allowed_explanation_plan_ids,
    bounded_deterministic_tool_plan,
)
from gi_scenario_core.evidence_guide_v2_provider import EvidenceGuideV2ProviderExecutor
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceToolCall,
    InferenceUsage,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = ROOT / "benchmarks" / "evidence_guide" / "v2"
MODEL_ID = "ibm-granite/granite-4.1-8b"


def _spec(architecture_id: str, case_id: str) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=f"parity-{architecture_id}-{case_id}",
        stage="architecture",
        split="development",
        case_id=case_id,
        project_family="cedar_trace_cc0",
        model_id=MODEL_ID,
        architecture_id=architecture_id,
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
    )


def _completion(
    content: str,
    *,
    tool_calls: list[InferenceToolCall] | None = None,
) -> InferenceCompletion:
    return InferenceCompletion(
        provider_call_id="parity-call",
        model=MODEL_ID,
        content=content,
        tool_calls=tool_calls or [],
        usage=InferenceUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        finish_reason="tool_calls" if tool_calls else "stop",
        request_bytes=100,
    )


def _executor() -> EvidenceGuideV2ProviderExecutor:
    corpus = load_corpus(CORPUS_ROOT)
    return EvidenceGuideV2ProviderExecutor(
        corpus=corpus,
        provider=object(),  # type: ignore[arg-type]
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        pricing={},
    )


def _case(case_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    corpus = load_corpus(CORPUS_ROOT)
    dataset = corpus.datasets["development"]
    row = corpus.case_map("development")[case_id]
    return dataset, row, provider_packet(dataset, row, 0)


def test_shared_protocol_returns_full_bounded_plan_and_dynamic_plan_ids() -> None:
    available = {
        "results.compare_exact",
        "model.get_fitness",
        "project.list_limitations",
        "evidence.resolve",
    }
    assert bounded_deterministic_tool_plan("evidence_scope", available) == [
        "results.compare_exact",
        "model.get_fitness",
        "project.list_limitations",
    ]
    assert allowed_explanation_plan_ids("rule_failure", rule_disposition="fail") == [
        "recorded_rules_fail_v1"
    ]
    assert allowed_explanation_plan_ids("rule_failure", rule_disposition="pass") == [
        "recorded_rules_pass_v1"
    ]
    assert allowed_explanation_plan_ids(
        "authority_boundary", rule_disposition=None
    ) == ["authority_boundary_v1"]


def test_a1_runs_complete_tool_plan_and_exposes_only_intent_eligible_plan() -> None:
    dataset, row, packet = _case("egv2-dev-supported-044")
    schemas: list[dict[str, Any]] = []
    messages_seen: list[list[dict[str, Any]]] = []

    def call(**kwargs: Any) -> InferenceCompletion:
        schemas.append(kwargs["response_schema"])
        messages_seen.append(kwargs["messages"])
        return _completion('{"explanation_plan_id":"evidence_scope_v1"}')

    output, plan_id, tool_events, selected_intent = _executor()._run_agent_arm(
        spec=_spec("A1", row["case_id"]),
        case=row,
        dataset=dataset,
        packet=packet,
        attempt_index=0,
        call=call,
        trace=None,
        ablation_condition=None,
    )

    assert [event["tool_name"] for event in tool_events] == [
        "results.compare_exact",
        "model.get_fitness",
        "project.list_limitations",
    ]
    assert schemas[0]["properties"]["explanation_plan_id"]["enum"] == [
        "evidence_scope_v1"
    ]
    sent_packet = json.loads(messages_seen[0][-1]["content"])
    assert sent_packet["selected_intent"] == "evidence_scope"
    assert sent_packet["available_plan_ids"] == ["evidence_scope_v1"]
    assert selected_intent == "evidence_scope"
    assert plan_id == output["explanation_plan_id"] == "evidence_scope_v1"


def test_a2_preserves_selected_intent_and_routes_its_complete_tool_plan() -> None:
    # The target in this case is hidden behind ``results.compare_exact``.
    # Selecting ``evidence_scope`` forces a real packet rebuild after lookup.
    dataset, row, packet = _case("egv2-dev-causal-retrieval-001")
    schemas: list[dict[str, Any]] = []
    messages_seen: list[list[dict[str, Any]]] = []
    calls = 0

    def call(**kwargs: Any) -> InferenceCompletion:
        nonlocal calls
        calls += 1
        schemas.append(kwargs["response_schema"])
        messages_seen.append(kwargs["messages"])
        if calls == 1:
            return _completion('{"intent_id":"evidence_scope"}')
        return _completion('{"explanation_plan_id":"evidence_scope_v1"}')

    output, plan_id, tool_events, selected_intent = _executor()._run_agent_arm(
        spec=_spec("A2", row["case_id"]),
        case=row,
        dataset=dataset,
        packet=packet,
        attempt_index=0,
        call=call,
        trace=None,
        ablation_condition=None,
    )

    assert [event["tool_name"] for event in tool_events] == [
        "results.compare_exact",
        "project.list_limitations",
    ]
    assert any(event["result"] for event in tool_events)
    assert schemas[1]["properties"]["explanation_plan_id"]["enum"] == [
        "evidence_scope_v1"
    ]
    sent_packet = json.loads(messages_seen[1][-1]["content"])
    assert sent_packet["selected_intent"] == "evidence_scope"
    assert "retrieved_evidence" in sent_packet
    assert selected_intent == "evidence_scope"
    assert plan_id == output["explanation_plan_id"] == "evidence_scope_v1"


def test_a2_cannot_downgrade_deterministic_safety_intent() -> None:
    dataset, row, packet = _case("egv2-dev-unsupported-020")
    calls = 0

    def call(**_kwargs: Any) -> InferenceCompletion:
        nonlocal calls
        calls += 1
        return _completion('{"intent_id":"evidence_scope"}')

    with pytest.raises(RuntimeError, match="EVIDENCE_INTENT_SAFETY_DOWNGRADE"):
        _executor()._run_agent_arm(
            spec=_spec("A2", row["case_id"]),
            case=row,
            dataset=dataset,
            packet=packet,
            attempt_index=0,
            call=call,
            trace=None,
            ablation_condition=None,
        )
    assert calls == 1


def test_a3_model_routing_still_receives_dynamic_intent_plan_boundary() -> None:
    dataset, row, packet = _case("egv2-dev-unsupported-020")
    calls = 0
    route_message: dict[str, Any] = {}
    plan_schema: dict[str, Any] = {}

    def call(**kwargs: Any) -> InferenceCompletion:
        nonlocal calls, route_message, plan_schema
        calls += 1
        if kwargs.get("tools") is not None:
            route_message = json.loads(kwargs["messages"][-1]["content"])
            completion = _completion(
                "",
                tool_calls=[
                    InferenceToolCall(
                        "tool-1",
                        "project.list_limitations",
                        {},
                    )
                ],
            )
            kwargs["on_completion"](completion)
            return completion
        plan_schema = kwargs["response_schema"]
        return _completion('{"explanation_plan_id":"authority_boundary_v1"}')

    output, plan_id, tool_events, selected_intent = _executor()._run_agent_arm(
        spec=_spec("A3", row["case_id"]),
        case=row,
        dataset=dataset,
        packet=packet,
        attempt_index=0,
        call=call,
        trace=None,
        ablation_condition=None,
    )

    assert route_message["intent"] == "authority_boundary"
    assert [event["tool_name"] for event in tool_events] == [
        "project.list_limitations"
    ]
    assert plan_schema["properties"]["explanation_plan_id"]["enum"] == [
        "authority_boundary_v1"
    ]
    assert selected_intent == "authority_boundary"
    assert plan_id == output["explanation_plan_id"] == "authority_boundary_v1"


def test_agent_rejects_plan_outside_dynamic_schema_even_from_noncompliant_provider() -> None:
    dataset, row, packet = _case("egv2-dev-unsupported-020")

    def call(**_kwargs: Any) -> InferenceCompletion:
        return _completion('{"explanation_plan_id":"recorded_rules_fail_v1"}')

    with pytest.raises(RuntimeError, match="EVIDENCE_EXPLANATION_PLAN_INVALID"):
        _executor()._run_agent_arm(
            spec=_spec("A1", row["case_id"]),
            case=row,
            dataset=dataset,
            packet=packet,
            attempt_index=0,
            call=call,
            trace=None,
            ablation_condition=None,
        )
