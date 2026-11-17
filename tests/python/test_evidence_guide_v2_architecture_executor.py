from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from gi_scenario_core.evidence_evaluation_v2 import REQUIRED_LIMITATION_TEXT
from gi_scenario_core.evidence_guide_v2_architecture_dev import ResearchAttemptSpec
from gi_scenario_core.evidence_guide_v2_architecture_executor import (
    ArchitectureDevelopmentExecutor,
    _conditioned_causal_evidence,
    research_remap_index,
)
from gi_scenario_core.evidence_guide_v2_campaign import ModelPrice, load_corpus
from gi_scenario_core.evidence_guide_v2_protocol import EXPLANATION_PLANS
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceRequestError,
    InferenceToolCall,
    InferenceUsage,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = load_corpus(ROOT / "benchmarks/evidence_guide/v2")
MODEL = "ibm-granite/granite-4.1-8b"
OTHER_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
PROJECT = "gi-scenario-studio-agent-evals"
PRICES = {
    model: ModelPrice(Decimal(1), Decimal(2), "test", "2026-08-07")
    for model in (MODEL, OTHER_MODEL)
}


def _case(case_id: str) -> dict[str, Any]:
    return CORPUS.case_map("development")[case_id]


def _spec(
    case: dict[str, Any],
    architecture: str,
    *,
    stage: str = "development",
    condition: str | None = None,
    repeat: int = 0,
    model: str = MODEL,
) -> ResearchAttemptSpec:
    return ResearchAttemptSpec(
        attempt_id=f"research-{stage}-{architecture}-{case['case_id']}-{condition}-{repeat}-{model}",
        stage=stage,
        split="development",
        case_id=case["case_id"],
        project_family=case["project_family"],
        model_id=model,
        architecture_id=architecture,
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=repeat,
        time_block=f"block-{repeat % 3 + 1}",
        presentation_index=repeat,
        ablation_condition=condition,
    )


class Trace:
    def __init__(self) -> None:
        self.llm_count = 0
        self.tool_count = 0
        self.operations: dict[str, Any] | None = None

    @contextmanager
    def llm(self, **_kwargs: Any):
        self.llm_count += 1

        class Handle:
            def record(self, _completion: InferenceCompletion) -> None:
                return None

        yield Handle()

    @contextmanager
    def tool(self, **_kwargs: Any):
        self.tool_count += 1

        class Handle:
            def record(self, _result: dict[str, Any]) -> None:
                return None

        yield Handle()

    def validation(self, **_kwargs: Any) -> None:
        return None

    def assert_operations(self, **value: Any) -> None:
        self.operations = value


class Observer:
    def __init__(self, *, acknowledge: bool = True) -> None:
        self.trace = Trace()
        self.acknowledge = acknowledge

    @contextmanager
    def attempt(self, _spec: ResearchAttemptSpec, _packet: dict[str, Any]):
        yield self.trace
        if self.acknowledge:
            self.trace.receipt = SimpleNamespace(
                conversation_id="conversation-research",
                trace_id="trace-research",
                root_span_id="root-research",
                url="https://wandb.ai/wandb/project/weave/agents/conversation-research",
            )

    def log_outcome(self, _spec: ResearchAttemptSpec, _outcome: Any) -> None:
        return None


class SmartClient:
    def __init__(
        self,
        case: dict[str, Any],
        *,
        wrong_interpretation: bool = False,
        failure: InferenceRequestError | None = None,
        input_tokens: int = 10,
        answer_text_override: str | None = None,
    ) -> None:
        self.case = case
        self.wrong_interpretation = wrong_interpretation
        self.failure = failure
        self.input_tokens = input_tokens
        self.answer_text_override = answer_text_override
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> InferenceCompletion:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        schema = kwargs.get("response_schema") or {}
        properties = schema.get("properties") or {}
        tool_calls: list[InferenceToolCall] = []
        if kwargs.get("tools"):
            tool_calls = [
                InferenceToolCall(f"tool-{index}", name, {})
                for index, name in enumerate(
                    self.case["expectations"]["required_tool_plan"]
                )
            ]
            content = ""
        elif "explanation_plan_id" in properties:
            selected = properties["explanation_plan_id"]["enum"][0]
            content = json.dumps({"explanation_plan_id": selected})
        else:
            packet = json.loads(kwargs["messages"][-1]["content"])
            retrieved = packet.get("retrieved_evidence", [])
            available_evidence = retrieved or packet.get("evidence_catalog", [])
            if self.case["expectations"].get("request_class") == "cost":
                available_evidence = [
                    item
                    for item in available_evidence
                    if item.get("authority") == "sourced_cost"
                ]
            allowed = properties["interpretation_id"]["enum"]
            if allowed == ["unsupported"]:
                available_evidence = []
                interpretation = "unsupported"
                mode = "refuse"
                limitations = ["no_supporting_evidence"]
                actions = ["review_limitations"]
            else:
                if {
                    "recorded_rules_fail_v1",
                    "recorded_rules_pass_v1",
                }.issubset(allowed):
                    value = float(available_evidence[0]["value"])
                    interpretation = (
                        "recorded_rules_fail_v1"
                        if value > 0
                        else "recorded_rules_pass_v1"
                    )
                    if self.wrong_interpretation:
                        interpretation = (
                            "recorded_rules_pass_v1"
                            if interpretation == "recorded_rules_fail_v1"
                            else "recorded_rules_fail_v1"
                        )
                else:
                    expected = self.case["expectations"]["explanation_plan_id"]
                    interpretation = expected if expected in allowed else allowed[0]
                plan = EXPLANATION_PLANS[interpretation]
                mode = plan.evaluation_response_mode
                if packet.get("research_condition"):
                    limitations = list(plan.evaluation_limitation_ids)
                    actions = list(plan.evaluation_action_ids)
                else:
                    limitations = list(
                        self.case["expectations"]["required_limitations"]
                    )
                    actions = list(self.case["expectations"]["required_actions"])
            facts = "; ".join(
                f"{item['label']}: {item['value']} {item['units']}"
                for item in available_evidence
            )
            content = json.dumps(
                {
                    "interpretation_id": interpretation,
                    "response_mode": mode,
                    "answer_text": self.answer_text_override
                    or " ".join(
                        [
                            f"The repository evidence records {facts}." if facts else "",
                            *(REQUIRED_LIMITATION_TEXT[item] for item in limitations),
                        ]
                    ).strip(),
                    "evidence_ids": [
                        item["evidence_id"] for item in available_evidence
                    ]
                    if mode == "answer"
                    else [],
                    "limitation_ids": limitations,
                    "action_ids": actions,
                }
            )
        index = len(self.calls)
        return InferenceCompletion(
            provider_call_id=f"provider-{index}",
            model=kwargs["model"],
            content=content,
            tool_calls=tool_calls,
            usage=InferenceUsage(
                input_tokens=self.input_tokens,
                output_tokens=2,
                total_tokens=self.input_tokens + 2,
            ),
            finish_reason="tool_calls" if tool_calls else "stop",
            transport_attempts=1,
            request_bytes=100,
            latency_ms=float(index),
        )


def _executor(client: Any) -> ArchitectureDevelopmentExecutor:
    return ArchitectureDevelopmentExecutor(
        corpus=CORPUS,
        provider=client,
        entity="wandb",
        project=PROJECT,
        pricing=PRICES,
    )


def test_research_remap_is_shared_across_original_and_causal_siblings() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    siblings = [
        _spec(case, "A1"),
        _spec(case, "A3X"),
        _spec(
            case,
            "A1",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        _spec(
            case,
            "A3P",
            stage="causal_perturbation",
            condition="evidence_withheld",
        ),
        _spec(
            case,
            "A0xF",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
    ]
    assert len({research_remap_index(item) for item in siblings}) == 1
    assert research_remap_index(_spec(case, "A1", repeat=1)) != research_remap_index(
        siblings[0]
    )
    assert research_remap_index(
        _spec(case, "A1", model=OTHER_MODEL)
    ) != research_remap_index(siblings[0])


@pytest.mark.parametrize(
    ("case_id", "expected_plan", "positive_after_crossing"),
    [
        ("egv2-dev-causal-retrieval-001", "recorded_rules_pass_v1", False),
        ("egv2-dev-causal-retrieval-004", "recorded_rules_fail_v1", True),
    ],
)
def test_counterfactual_really_crosses_both_native_rule_sides(
    case_id: str, expected_plan: str, positive_after_crossing: bool
) -> None:
    case = _case(case_id)
    _tool, evidence, plan = _conditioned_causal_evidence(
        case,
        attempt_index=7,
        condition="counterfactual_threshold_crossing",
    )
    assert plan == expected_plan
    assert (float(evidence[0]["value"]) > 0) is positive_after_crossing


def test_a3x_dispatch_is_three_metered_calls_with_no_released_answer() -> None:
    case = _case("egv2-dev-supported-015")
    client = SmartClient(case)
    observer = Observer()
    outcome = _executor(client).execute(
        spec=_spec(case, "A3X"), case=case, observer=observer
    )

    assert outcome.provider_invocations == 3
    assert outcome.transport_attempts == 3
    assert outcome.input_tokens == 30
    assert outcome.output_tokens == 6
    assert outcome.input_token_envelope == 30
    assert outcome.output_token_envelope == 384
    assert outcome.cost_usd == Decimal("0.000042")
    assert len(outcome.details["tool_receipts"]) == 3
    assert outcome.details["answer_released"] is False
    assert outcome.details["candidate_conformant"] is True
    assert outcome.details["hypothetical_release_eligible"] is True
    assert all(
        branch["answer_released"] is False
        for branch in outcome.details["paired_branch_outcomes"].values()
    )
    assert outcome.details["weave_turn_receipt"]["trace_id"] == "trace-research"
    assert outcome.details["request_accounting"] == {
        "request_utf8_bytes_observed": 300,
        "request_utf8_bytes_transport_envelope": 300,
        "input_token_envelope_basis": (
            "reported_input_tokens_times_transport_attempts_on_completion; "
            "2048_tokens_per_transport_attempt_on_provider_error"
        ),
        "maximum_input_tokens_per_transport": 2048,
        "maximum_output_tokens_per_transport": 128,
        "request_bytes_used_as_token_count": False,
    }
    assert observer.trace.operations == {
        "llm_calls": 3,
        "tool_calls": 3,
        "final_answer": False,
    }


@pytest.mark.parametrize(
    ("case_id", "expected_plan"),
    [
        ("egv2-dev-causal-retrieval-001", "recorded_rules_pass_v1"),
        ("egv2-dev-causal-retrieval-004", "recorded_rules_fail_v1"),
    ],
)
def test_causal_finite_arm_uses_computed_crossing_plan(
    case_id: str, expected_plan: str
) -> None:
    case = _case(case_id)
    client = SmartClient(case)
    outcome = _executor(client).execute(
        spec=_spec(
            case,
            "A1",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )
    assert len(client.calls) == 1
    assert outcome.explanation_plan_id == expected_plan
    assert outcome.causal is True
    assert outcome.details["candidate_conformant"] is True
    assert outcome.details["answer_released"] is False


def test_wrong_diagnostic_interpretation_is_raw_invalid_and_contained() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    client = SmartClient(case, wrong_interpretation=True)
    outcome = _executor(client).execute(
        spec=_spec(
            case,
            "A0xF",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )
    schema = client.calls[0]["response_schema"]
    assert schema["properties"]["interpretation_id"]["enum"] == [
        "recorded_rules_fail_v1",
        "recorded_rules_pass_v1",
    ]
    branch = outcome.details["branch_outcome"]
    assert branch["raw_invalid"] is True
    assert branch["candidate_conformant"] is False
    assert branch["hypothetical_release_eligible"] is False
    assert branch["answer_released"] is False
    assert "causal_interpretation_mismatch" in branch["error_codes"]


def test_withheld_diagnostic_refuses_without_releasing_an_answer() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    outcome = _executor(SmartClient(case)).execute(
        spec=_spec(
            case,
            "A0xF",
            stage="causal_perturbation",
            condition="evidence_withheld",
        ),
        case=case,
        observer=Observer(),
    )
    assert outcome.causal is True
    assert outcome.details["branch_outcome"]["interpretation_id"] == "unsupported"
    assert outcome.details["candidate_conformant"] is True
    assert outcome.details["answer_released"] is False


def test_provider_fault_is_metered_and_fails_closed() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    failure = InferenceRequestError(
        "INFERENCE_AUTHORIZATION_REQUIRED",
        transport_attempts=2,
        request_bytes=300,
    )
    outcome = _executor(SmartClient(case, failure=failure)).execute(
        spec=_spec(
            case,
            "A1",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )
    assert outcome.status == "failed_closed"
    assert outcome.provider_invocations == 1
    assert outcome.transport_attempts == 2
    assert outcome.input_token_envelope == 4_096
    assert outcome.output_token_envelope == 256
    assert outcome.details["candidate_conformant"] is False
    assert outcome.details["answer_released"] is False
    assert outcome.error_codes == ("INFERENCE_AUTHORIZATION_REQUIRED",)
    assert outcome.details["request_accounting"][
        "request_utf8_bytes_transport_envelope"
    ] == 600


def test_local_request_byte_failure_records_bytes_without_transport_or_tokens() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    failure = InferenceRequestError(
        "INFERENCE_INPUT_ENVELOPE_EXCEEDED",
        transport_attempts=0,
        request_bytes=65_537,
    )
    outcome = _executor(SmartClient(case, failure=failure)).execute(
        spec=_spec(
            case,
            "A1",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )

    assert outcome.status == "failed_closed"
    assert outcome.provider_invocations == 1
    assert outcome.transport_attempts == 0
    assert outcome.input_token_envelope == 0
    assert outcome.output_token_envelope == 0
    assert outcome.error_codes == ("INFERENCE_INPUT_ENVELOPE_EXCEEDED",)
    accounting = outcome.details["request_accounting"]
    assert accounting["request_utf8_bytes_observed"] == 65_537
    assert accounting["request_utf8_bytes_transport_envelope"] == 0
    assert accounting["request_bytes_used_as_token_count"] is False


def test_reported_input_usage_above_r2_cap_fails_closed() -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    outcome = _executor(SmartClient(case, input_tokens=2_049)).execute(
        spec=_spec(
            case,
            "A1",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )
    assert outcome.status == "failed_closed"
    assert outcome.input_tokens == 2_049
    assert outcome.input_token_envelope == 2_049
    assert outcome.details["answer_released"] is False
    assert outcome.error_codes == ("R2_PROVIDER_INPUT_TOKEN_CAP_EXCEEDED",)


@pytest.mark.parametrize(
    "answer_text",
    [
        "The repository evidence records a 999 cfs result.",
        "The repository evidence records a 0.01 Mgal result.",
        "The repository evidence records one hundred cfs.",
        "The repository evidence records the exact result and an extra 999.",
    ],
    ids=["wrong_digit", "wrong_unit", "wrong_spelled_quantity", "unbound_digit"],
)
def test_causal_free_prose_numeric_mutations_are_contained(
    answer_text: str,
) -> None:
    case = _case("egv2-dev-causal-retrieval-001")
    outcome = _executor(
        SmartClient(case, answer_text_override=answer_text)
    ).execute(
        spec=_spec(
            case,
            "A0xF",
            stage="causal_perturbation",
            condition="counterfactual_threshold_crossing",
        ),
        case=case,
        observer=Observer(),
    )
    branch = outcome.details["branch_outcome"]
    assert branch["raw_invalid"] is True
    assert branch["candidate_conformant"] is False
    assert branch["answer_released"] is False
    assert "prose_quantity_mismatch" in branch["error_codes"]


def test_product_parity_arm_is_normalized_to_zero_actual_release() -> None:
    case = _case("egv2-dev-supported-044")
    outcome = _executor(SmartClient(case)).execute(
        spec=_spec(case, "A1"), case=case, observer=Observer()
    )
    assert outcome.status == "completed"
    assert outcome.details["upstream_answer_release_flag"] is False
    assert outcome.details["candidate_conformant"] is True
    assert outcome.details["hypothetical_release_eligible"] is True
    assert outcome.details["answer_released"] is False
