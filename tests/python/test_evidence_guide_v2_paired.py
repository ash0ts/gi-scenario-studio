from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from gi_scenario_core.evidence_evaluation_v2 import (
    REQUIRED_LIMITATION_TEXT,
    load_split,
)
from gi_scenario_core.evidence_guide_v2_campaign import AttemptSpec
from gi_scenario_core.evidence_guide_v2_paired import (
    FINITE_BRANCH_ID,
    PAIRED_ARCHITECTURE_ID,
    PROSE_BRANCH_ID,
    PairedArchitectureExecutor,
    branch_order,
)
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceToolCall,
    InferenceUsage,
)

ROOT = Path(__file__).resolve().parents[2]


def _case() -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = load_split(ROOT / "benchmarks/evidence_guide/v2/development.json")
    case = next(
        row
        for row in dataset["rows"]
        if row["case_id"] == "egv2-dev-causal-retrieval-001"
    )
    return dataset, case


def _spec(case: dict[str, Any], *, repeat: int = 0, presentation: int = 0) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=f"paired-{case['case_id']}-{repeat}-{presentation}",
        stage="architecture_development",
        split="development",
        case_id=case["case_id"],
        project_family=case["project_family"],
        model_id="ibm-granite/granite-4.1-8b",
        architecture_id=PAIRED_ARCHITECTURE_ID,
        prompt_id="paired-checklist",
        max_output_tokens=128,
        repeat=repeat,
        time_block=f"block-{repeat % 3 + 1}",
        presentation_index=presentation,
    )


def _format_value(value: Any) -> str:
    return f"{float(value):.12g}" if isinstance(value, float) else str(value)


class FakeClient:
    def __init__(self, case: dict[str, Any], *, invalid_prose_action: bool = False) -> None:
        self.case = case
        self.invalid_prose_action = invalid_prose_action
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> InferenceCompletion:
        self.calls.append(kwargs)
        call_index = len(self.calls)
        if kwargs.get("tools"):
            content = ""
            tool_calls = [
                InferenceToolCall(
                    call_id="route-tool-1",
                    name=self.case["expectations"]["required_tool_plan"][0],
                    arguments={},
                )
            ]
        elif "explanation_plan_id" in kwargs["response_schema"]["properties"]:
            allowed = kwargs["response_schema"]["properties"]["explanation_plan_id"][
                "enum"
            ]
            content = json.dumps({"explanation_plan_id": allowed[0]})
            tool_calls = []
        else:
            evidence_packet = json.loads(kwargs["messages"][-1]["content"])
            retrieved = evidence_packet.get("retrieved_evidence", [])
            response_mode = self.case["expectations"]["response_mode"]
            selected = retrieved if response_mode == "answer" else []
            if selected:
                facts = "; ".join(
                    f"{item['label']}: {_format_value(item['value'])} {item['units']}"
                    for item in selected
                )
                explanation = f"The repository evidence records {facts}."
            else:
                explanation = (
                    "I cannot establish that claim from the available repository evidence."
                )
            limitation_ids = list(self.case["expectations"]["required_limitations"])
            answer = " ".join(
                [
                    explanation,
                    *(REQUIRED_LIMITATION_TEXT[item] for item in limitation_ids),
                ]
            )
            actions = list(self.case["expectations"]["required_actions"])
            if self.invalid_prose_action:
                actions.append("approve_design")
            content = json.dumps(
                {
                    "interpretation_id": self.case["expectations"][
                        "explanation_plan_id"
                    ],
                    "response_mode": response_mode,
                    "answer_text": answer,
                    "evidence_ids": [item["evidence_id"] for item in selected],
                    "limitation_ids": limitation_ids,
                    "action_ids": actions,
                }
            )
            tool_calls = []
        return InferenceCompletion(
            provider_call_id=f"provider-{call_index}",
            model=kwargs["model"],
            content=content,
            tool_calls=tool_calls,
            usage=InferenceUsage(input_tokens=100 + call_index, output_tokens=10 + call_index),
            finish_reason="tool_calls" if tool_calls else "stop",
            transport_attempts=1,
            latency_ms=float(call_index),
        )


class FakeTrace:
    def __init__(self) -> None:
        self.llm_records: list[InferenceCompletion] = []
        self.tool_records: list[dict[str, Any]] = []
        self.validation_record: dict[str, Any] | None = None
        self.operation_record: dict[str, Any] | None = None

    @contextmanager
    def llm(self, **_: Any):
        outer = self

        class Handle:
            def record(self, completion: InferenceCompletion) -> None:
                outer.llm_records.append(completion)

        yield Handle()

    @contextmanager
    def tool(self, **_: Any):
        outer = self

        class Handle:
            def record(self, result: dict[str, Any]) -> None:
                outer.tool_records.append(result)

        yield Handle()

    def validation(self, **value: Any) -> None:
        self.validation_record = value

    def assert_operations(self, **value: Any) -> None:
        self.operation_record = value


class FakeObserver:
    def __init__(self) -> None:
        self.trace = FakeTrace()
        self.public_input: dict[str, Any] | None = None

    @contextmanager
    def attempt(self, _spec: AttemptSpec, public_input: dict[str, Any]):
        self.public_input = public_input
        yield self.trace
        self.trace.receipt = SimpleNamespace(
            conversation_id="conversation-safe",
            trace_id="trace-safe",
            root_span_id="root-safe",
            url="https://wandb.ai/wandb/project/weave/agents/conversation-safe",
        )

    def log_outcome(self, _spec: AttemptSpec, _outcome: Any) -> None:
        raise AssertionError("the separate paired outcome must not be projected as a v2 outcome")


def _execute(*, invalid_prose_action: bool = False, repeat: int = 0):
    dataset, case = _case()
    spec = _spec(case, repeat=repeat)
    client = FakeClient(case, invalid_prose_action=invalid_prose_action)
    observer = FakeObserver()
    outcome = PairedArchitectureExecutor(
        client,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    ).execute(spec=spec, dataset=dataset, case=case, observer=observer)
    return dataset, case, spec, client, observer, outcome


def test_paired_executor_routes_and_executes_tool_once_then_scores_two_siblings() -> None:
    _, case, _, client, observer, outcome = _execute()

    assert len(client.calls) == 3
    assert sum(bool(call.get("tools")) for call in client.calls) == 1
    assert len(observer.trace.llm_records) == 3
    assert len(observer.trace.tool_records) == 1
    assert observer.trace.operation_record == {
        "llm_calls": 3,
        "tool_calls": 1,
        "final_answer": False,
    }
    assert outcome.route.tool_name == case["expectations"]["required_tool_plan"][0]
    assert outcome.finite.passed is True
    assert outcome.prose.passed is True
    assert outcome.finite.evidence_payload_sha256 == outcome.prose.evidence_payload_sha256
    assert outcome.evidence_payload_sha256 == outcome.finite.evidence_payload_sha256
    assert outcome.provider_invocations == 3
    assert outcome.transport_attempts == 3
    assert outcome.production_eligible is False
    assert outcome.answer_released is False
    assert outcome.weave_turn_receipt == {
        "conversation_id": "conversation-safe",
        "trace_id": "trace-safe",
        "root_span_id": "root-safe",
        "url": "https://wandb.ai/wandb/project/weave/agents/conversation-safe",
    }
    assert observer.trace.validation_record == {
        "valid": True,
        "checked_claim_count": 2,
        "error_codes": [],
    }

    router_packet = json.loads(client.calls[0]["messages"][-1]["content"])
    assert set(router_packet) == {
        "anchor_kind",
        "available_tool_aliases",
        "intent",
        "question",
        "rules",
        "task",
    }
    target = case["claim_graph"]["tool_claims"][0]
    assert target["semantic_id"] not in client.calls[0]["messages"][-1]["content"]
    assert "evidence_catalog" not in client.calls[0]["messages"][-1]["content"]

    synthesis_calls = [call for call in client.calls if not call.get("tools")]
    assert synthesis_calls[0]["messages"][-1]["content"] == synthesis_calls[1][
        "messages"
    ][-1]["content"]
    prose_call = next(
        call
        for call in synthesis_calls
        if "interpretation_id" in call["response_schema"]["properties"]
    )
    assert prose_call["response_schema"]["properties"]["interpretation_id"][
        "enum"
    ] == ["recorded_rules_fail_v1", "recorded_rules_pass_v1"]


def test_receipts_are_frozen_and_return_defensive_copies() -> None:
    *_, outcome = _execute()

    with pytest.raises(FrozenInstanceError):
        outcome.route.tool_name = "evidence.resolve"  # type: ignore[misc]
    first_result = outcome.tool.result()
    first_result[0]["value"] = 999
    assert outcome.tool.result()[0]["value"] != 999
    first_arguments = outcome.route.arguments()
    first_arguments["unsafe"] = True
    assert outcome.route.arguments() == {}
    assert len(outcome.route.receipt_sha256) == 64
    assert len(outcome.tool.receipt_sha256) == 64


def test_branch_order_is_deterministic_and_reverses_next_presentation() -> None:
    _, case = _case()
    first = _spec(case, repeat=0)
    second = _spec(case, repeat=1)

    assert set(branch_order(first)) == {FINITE_BRANCH_ID, PROSE_BRANCH_ID}
    assert branch_order(second) == tuple(reversed(branch_order(first)))
    _, _, _, _, _, first_outcome = _execute(repeat=0)
    _, _, _, _, _, second_outcome = _execute(repeat=1)
    assert second_outcome.branch_order == tuple(reversed(first_outcome.branch_order))


def test_prose_validation_failure_does_not_contaminate_finite_branch_metrics() -> None:
    *_, observer, outcome = _execute(invalid_prose_action=True)

    assert outcome.finite.passed is True
    assert outcome.finite.error_codes == ()
    assert outcome.prose.passed is False
    assert "action_unauthorized" in outcome.prose.error_codes
    assert outcome.passed is False
    assert observer.trace.validation_record is not None
    assert observer.trace.validation_record["valid"] is False
    assert any(
        "A0X_F_ACTION_UNAUTHORIZED" in code
        for code in observer.trace.validation_record["error_codes"]
    )
    assert outcome.to_record()["production_eligible"] is False
    assert outcome.to_record()["answer_released"] is False


def test_router_rejects_duplicate_tool_calls_without_running_a_tool() -> None:
    dataset, case = _case()
    client = FakeClient(case)
    original_complete = client.complete

    def duplicate_router(**kwargs: Any) -> InferenceCompletion:
        completion = original_complete(**kwargs)
        if kwargs.get("tools"):
            return InferenceCompletion(
                **{
                    **completion.__dict__,
                    "tool_calls": [*completion.tool_calls, *completion.tool_calls],
                }
            )
        return completion

    client.complete = duplicate_router  # type: ignore[method-assign]
    observer = FakeObserver()
    with pytest.raises(RuntimeError, match="PAIRED_ROUTER_TOOL_INVALID"):
        PairedArchitectureExecutor(
            client,
            entity="wandb",
            project="gi-scenario-studio-agent-evals",
        ).execute(
            spec=_spec(case),
            dataset=dataset,
            case=case,
            observer=observer,
        )
    assert observer.trace.tool_records == []


def test_router_executes_complete_unique_required_plan_and_shares_all_receipts() -> None:
    dataset = load_split(ROOT / "benchmarks/evidence_guide/v2/development.json")
    case = next(
        row for row in dataset["rows"] if row["case_id"] == "egv2-dev-supported-044"
    )
    client = FakeClient(case)
    original_complete = client.complete

    def full_router(**kwargs: Any) -> InferenceCompletion:
        completion = original_complete(**kwargs)
        if kwargs.get("tools"):
            calls = [
                InferenceToolCall(f"route-{index}", name, {})
                for index, name in enumerate(case["expectations"]["required_tool_plan"])
            ]
            return InferenceCompletion(**{**completion.__dict__, "tool_calls": calls})
        return completion

    client.complete = full_router  # type: ignore[method-assign]
    observer = FakeObserver()
    outcome = PairedArchitectureExecutor(
        client,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    ).execute(
        spec=_spec(case),
        dataset=dataset,
        case=case,
        observer=observer,
    )

    assert outcome.router_plan_valid is True
    assert [item.tool_name for item in outcome.tools] == case["expectations"][
        "required_tool_plan"
    ]
    assert len(observer.trace.tool_records) == 3
    assert observer.trace.operation_record == {
        "llm_calls": 3,
        "tool_calls": 3,
        "final_answer": False,
    }
    assert outcome.finite.evidence_payload_sha256 == outcome.prose.evidence_payload_sha256
    assert len(outcome.shared_router_tool_receipt_sha256) == 64


@pytest.mark.parametrize("repeat", [0, 1])
def test_schema_invalid_first_branch_is_contained_and_sibling_still_runs(
    repeat: int,
) -> None:
    dataset, case = _case()
    client = FakeClient(case)
    original_complete = client.complete
    synthesis_calls = 0

    def invalidate_first_synthesis(**kwargs: Any) -> InferenceCompletion:
        nonlocal synthesis_calls
        completion = original_complete(**kwargs)
        if not kwargs.get("tools"):
            synthesis_calls += 1
            if synthesis_calls == 1:
                return InferenceCompletion(
                    **{**completion.__dict__, "content": "{}"}
                )
        return completion

    client.complete = invalidate_first_synthesis  # type: ignore[method-assign]
    observer = FakeObserver()
    outcome = PairedArchitectureExecutor(
        client,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    ).execute(
        spec=_spec(case, repeat=repeat),
        dataset=dataset,
        case=case,
        observer=observer,
    )

    first, second = outcome.branch_order
    branches = {FINITE_BRANCH_ID: outcome.finite, PROSE_BRANCH_ID: outcome.prose}
    assert len(client.calls) == 3
    assert synthesis_calls == 2
    assert branches[first].passed is False
    assert branches[second].passed is True
    assert any("SCHEMA_INVALID" in code for code in branches[first].error_codes)
    assert observer.trace.operation_record == {
        "llm_calls": 3,
        "tool_calls": 1,
        "final_answer": False,
    }
