from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gi_scenario_core.contracts import (
    ObservabilityBinding,
    ProjectDataPolicy,
)
from gi_scenario_core.evidence_guide import (
    CIRCUIT_BREAKER_WINDOW_SECONDS,
    MAX_TOOL_RESULT_BYTES,
    PLAN_REGISTRY_SHA256,
    PROMPT_SHA256,
    PROMPT_VERSION,
    PROTOCOL_SHA256,
    RENDERER_SHA256,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    TOOL_CONTRACT_SHA256,
    TOOL_CONTRACT_VERSION,
    VALIDATOR_SHA256,
    VALIDATOR_VERSION,
    EvidenceGuideService,
)
from gi_scenario_core.evidence_query import EvidenceQueryService
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceToolCall,
    InferenceUsage,
)
from gi_scenario_core.observability import WeaveAgentTurnReceipt
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault

ROOT = Path(__file__).resolve().parents[2]
RELEASE_GATE = ROOT / "benchmarks" / "evidence_guide" / "v1" / "release-gate.json"


class FakeWandb:
    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding:
        assert binding.access == "Restricted"
        assert policy.classification == "standard_engineering"
        payload = binding.model_dump(mode="json")
        payload.update(
            {
                "external_project_id": "restricted-project-id",
                "verified": True,
                "verified_at": datetime.now(UTC),
            }
        )
        return ObservabilityBinding.model_validate(payload)


class _RecordedSpan:
    def __init__(self, sink: list[Any]) -> None:
        self.sink = sink

    def record(self, value: Any) -> None:
        self.sink.append(value)


class FakeAgentRecorder:
    def __init__(self, owner: FakeWeave, conversation_id: str) -> None:
        self.owner = owner
        self.conversation_id = conversation_id
        self.receipt: WeaveAgentTurnReceipt | None = None
        self.llm_records: list[Any] = []
        self.llm_metadata: list[dict[str, Any]] = []
        self.tool_records: list[Any] = []
        self.tool_parent_llm_depths: list[int] = []
        self.validation_records: list[dict[str, Any]] = []
        self.final_answers: list[str] = []
        self.active_llm_depth = 0

    @contextmanager
    def llm(self, **metadata: Any):
        self.llm_metadata.append(metadata)
        self.active_llm_depth += 1
        try:
            yield _RecordedSpan(self.llm_records)
        finally:
            self.active_llm_depth -= 1

    @contextmanager
    def tool(self, **metadata: Any):
        self.owner.tool_metadata.append(metadata)
        self.tool_parent_llm_depths.append(self.active_llm_depth)
        yield _RecordedSpan(self.tool_records)

    def validation(self, **value: Any) -> None:
        self.validation_records.append(value)

    def record_final_answer(self, answer: str) -> None:
        self.final_answers.append(answer)


class FakeWeave:
    def __init__(self, *, acknowledge: bool = True, receipt_url: str | None = None) -> None:
        self.acknowledge = acknowledge
        self.receipt_url = receipt_url
        self.recorders: list[FakeAgentRecorder] = []
        self.tool_metadata: list[dict[str, Any]] = []
        self.feedback: list[dict[str, Any]] = []

    @contextmanager
    def agent_turn(
        self,
        binding: ObservabilityBinding,
        *,
        conversation_id: str,
        **_: Any,
    ):
        recorder = FakeAgentRecorder(self, conversation_id)
        self.recorders.append(recorder)
        yield recorder
        if self.acknowledge:
            recorder.receipt = WeaveAgentTurnReceipt(
                conversation_id=conversation_id,
                trace_id=f"trace-{len(self.recorders)}",
                root_span_id=f"span-{len(self.recorders)}",
                url=self.receipt_url
                or (
                    f"https://wandb.ai/{binding.entity}/{binding.external_project}/"
                    f"weave/agents/conversations/{conversation_id}"
                ),
            )

    def add_agent_feedback(
        self,
        binding: ObservabilityBinding,
        *,
        trace_id: str,
        feedback: str,
        notes: str | None,
    ) -> None:
        self.feedback.append(
            {
                "project": binding.external_project,
                "trace_id": trace_id,
                "feedback": feedback,
                "notes": notes,
            }
        )


class FakeInference:
    def __init__(
        self,
        *,
        fabricate: bool = False,
        provider_call_ids: list[str] | None = None,
    ) -> None:
        self.fabricate = fabricate
        self.provider_call_ids = provider_call_ids
        self.calls: list[dict[str, Any]] = []

    def complete(self, **values: Any) -> InferenceCompletion:
        self.calls.append(values)
        sequence = len(self.calls)
        provider_call_id = (
            self.provider_call_ids[sequence - 1]
            if self.provider_call_ids is not None
            else f"provider-{sequence}"
        )
        if values.get("tools"):
            available = {tool["function"]["name"] for tool in values["tools"]}
            selected = (
                "decision__get_rule_matrix"
                if "decision__get_rule_matrix" in available
                else min(available)
            )
            return InferenceCompletion(
                provider_call_id=provider_call_id,
                model=values["model"],
                content="",
                tool_calls=[
                    InferenceToolCall(
                        call_id=f"tool-{sequence}",
                        name=selected,
                        arguments={},
                    )
                ],
                usage=InferenceUsage(input_tokens=30, output_tokens=6, total_tokens=36),
                finish_reason="tool_calls",
            )

        context = json.loads(values["messages"][-1]["content"])
        claim_catalog = context["claim_catalog"]
        support = next(item for item in claim_catalog if item["claim_type"] != "limitation")
        limitation = next(item for item in claim_catalog if item["claim_type"] == "limitation")
        payload = {
            "intent": context["intent"],
            "plain_language": (
                "A tradeoff boundary describes only the displayed axes, while acceptance "
                "applies every recorded rule and limitation together."
            ),
            "claim_ids": ["claim_fabricated" if self.fabricate else support["claim_id"]],
            "limitation_claim_ids": [limitation["claim_id"]],
            "next_check_ids": ["professional_model_review"],
        }
        return InferenceCompletion(
            provider_call_id=provider_call_id,
            model=values["model"],
            content=json.dumps(payload),
            usage=InferenceUsage(input_tokens=80, output_tokens=20, total_tokens=100),
            finish_reason="stop",
        )


def _enabled_release_gate() -> dict[str, Any]:
    gate = json.loads(RELEASE_GATE.read_text(encoding="utf-8"))
    gate.update(
        {
            "enabled": True,
            "selected_model": "ibm-granite/granite-4.1-8b",
            "decision": "ENABLE_LIVE_GUIDE",
            "reasons": [],
            "selected_prompt_version": PROMPT_VERSION,
            "selected_response_schema_version": RESPONSE_SCHEMA_VERSION,
            "selected_response_schema_sha256": RESPONSE_SCHEMA_SHA256,
            "selected_tool_contract_version": TOOL_CONTRACT_VERSION,
            "selected_validator_version": VALIDATOR_VERSION,
        }
    )
    return gate


def _dispatcher(
    tmp_path,
    *,
    inference: FakeInference | None = None,
    weave: FakeWeave | None = None,
    release_gate: dict[str, Any] | None = None,
) -> tuple[RPCDispatcher, FakeInference, FakeWeave]:
    inference = inference or FakeInference()
    weave = weave or FakeWeave()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        allowed_roots=[tmp_path],
        observability_adapters={
            "wandb": FakeWandb(),
            "weave": weave,
            "inference": inference,
            "evidence_guide_release_gate": release_gate or _enabled_release_gate(),
        },
    )
    return dispatcher, inference, weave


def _guided_project(dispatcher: RPCDispatcher) -> dict[str, Any]:
    project = dispatcher.dispatch("project.create", {"kind": "demo"})
    root = project["projectDirectory"]
    project_id = project["id"]
    dispatcher.dispatch(
        "observability.connect",
        {
            "scope": "project",
            "project_directory": root,
            "policy": {
                "policy_id": "policy-guide-test",
                "project_id": project_id,
                "classification": "standard_engineering",
                "deployment": "wandb_cloud",
                "acknowledged_by": "Test engineer",
            },
            "binding": {
                "binding_id": "binding-guide-test",
                "project_id": project_id,
                "data_policy_id": "policy-guide-test",
                "entity": "test-team",
                "external_project": "restricted-guide-test",
                "external_project_id": "pending",
                "access": "Restricted",
                "deployment": "wandb_cloud",
                "mode": "online",
            },
        },
    )
    return project


def _base(project: dict[str, Any], alternative_id: str) -> dict[str, Any]:
    return {
        "project_directory": project["projectDirectory"],
        "project_id": project["id"],
        "anchor": {"kind": "alternative", "identifier": alternative_id},
    }


def _start(dispatcher: RPCDispatcher, project: dict[str, Any], alternative_id: str) -> str:
    response = dispatcher.dispatch(
        "assistant.start_conversation",
        {
            **_base(project, alternative_id),
            "inference_consent": True,
            "content_logging_consent": True,
        },
    )
    return response["conversation"]["conversation_id"]


def test_alt_017_is_tool_using_multi_turn_and_reports_both_peak_increases(tmp_path) -> None:
    dispatcher, inference, weave = _dispatcher(tmp_path)
    project = _guided_project(dispatcher)
    capabilities = dispatcher.dispatch("assistant.get_capabilities", _base(project, "ALT-017"))
    assert capabilities["available"] is True
    assert capabilities["state"] == "CONSENT_REQUIRED"
    assert capabilities["content_logging_consent"] is True

    conversation_id = _start(dispatcher, project, "ALT-017")
    first = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "Why can this be nondominated and still fail?",
        },
    )
    answer = first["answer"]
    assert first["evaluation"]["valid"] is True
    assert answer["provider_model_id"] == "ibm-granite/granite-4.1-8b"
    assert len(answer["provider_call_ids"]) == 2
    assert answer["evidence_tool_calls"] == ["decision.get_rule_matrix"]
    assert "4.14 percent" in answer["answer"]
    assert "3.14 to 3.21 cfs" in answer["answer"]
    assert "13.33 to 13.34 cfs" in answer["answer"]
    assert "fails the Four-storm OUT1 volume reduction rule" in answer["answer"]
    assert "fails the No OUT1 peak increase" in answer["answer"]
    assert answer["weave_conversation"]["trace_id"] == "trace-1"

    second = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "What should I inspect next?",
        },
    )
    assert second["turn"]["sequence"] == 1
    assert second["conversation"]["next_turn_sequence"] == 2
    assert second["answer"]["weave_conversation"]["trace_id"] == "trace-2"
    assert len(inference.calls) == 4
    assert len(weave.recorders) == 2
    assert all(len(recorder.final_answers) == 1 for recorder in weave.recorders)
    assert "Evidence checks passed" in weave.recorders[0].final_answers[0]
    assert all(len(recorder.llm_records) == 2 for recorder in weave.recorders)
    assert all(recorder.tool_parent_llm_depths == [1] for recorder in weave.recorders)
    synthesis_trace_messages = weave.recorders[0].llm_metadata[1]["input_messages"]
    assert any(message.get("tool_calls") for message in synthesis_trace_messages)
    assert any(message["role"] == "tool" for message in synthesis_trace_messages)
    dispatcher._executor.shutdown(wait=True)


def test_alt_207_ecological_question_preserves_the_claim_boundary(tmp_path) -> None:
    dispatcher, _, _ = _dispatcher(tmp_path)
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-207")
    response = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "Does this prove ecological benefit?",
        },
    )
    answer = response["answer"]
    assert "6.42 percent" in answer["answer"]
    assert "not proof of ecological benefit" in answer["answer"]
    boundary = " ".join(answer["limitations"]).casefold()
    for term in (
        "water quality",
        "groundwater recharge",
        "habitat",
        "heat",
        "real-world flooding",
        "maintenance",
        "lifecycle",
    ):
        assert term in boundary
    assert "candidate for site and feasibility review" not in answer["answer"].casefold()
    dispatcher._executor.shutdown(wait=True)


def test_fabricated_claim_or_missing_weave_ack_never_returns_generated_text(tmp_path) -> None:
    dispatcher, _, _ = _dispatcher(tmp_path, inference=FakeInference(fabricate=True))
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")
    with pytest.raises(RPCFault) as fabricated:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why does this fail?",
            },
        )
    assert fabricated.value.data["code"] == "EVIDENCE_ID_FABRICATED"
    repository = ProjectRepository(project["projectDirectory"])
    try:
        turns = repository.list_assistant_turns(project["id"], conversation_id)
        assert turns[0]["state"] == "failed"
        assert turns[0]["answer_id"] is None
        reference = repository.get_weave_conversation_reference(project["id"], turns[0]["turn_id"])
        assert reference is not None
        assert reference["trace_id"] == "trace-1"
    finally:
        repository.close()
    dispatcher._executor.shutdown(wait=True)

    no_ack, _, _ = _dispatcher(tmp_path / "no-ack", weave=FakeWeave(acknowledge=False))
    project = _guided_project(no_ack)
    conversation_id = _start(no_ack, project, "ALT-017")
    with pytest.raises(RPCFault) as untraced:
        no_ack.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why does this fail?",
            },
        )
    assert untraced.value.data["code"] == "WEAVE_TRACE_NOT_ACKNOWLEDGED"
    no_ack._executor.shutdown(wait=True)


def test_oversized_tool_result_is_rejected_before_trace_content_is_recorded(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _, weave = _dispatcher(tmp_path)
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")

    def oversized_result(*_args, **_kwargs):
        return {"payload": "x" * (MAX_TOOL_RESULT_BYTES + 1)}

    monkeypatch.setattr(EvidenceQueryService, "execute", oversized_result)
    with pytest.raises(RPCFault) as failure:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why does this fail?",
            },
        )
    assert failure.value.data["code"] == "PROVIDER_TOOL_RESULT_TOO_LARGE"
    assert weave.recorders[-1].tool_records == []
    dispatcher._executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("provider_call_ids", "expected_code"),
    [
        (["", "provider-2"], "INFERENCE_RESPONSE_ID_INVALID"),
        (["provider-duplicate", "provider-duplicate"], "INFERENCE_RESPONSE_ID_DUPLICATE"),
    ],
)
def test_malformed_provider_response_ids_terminalize_the_turn(
    tmp_path,
    provider_call_ids: list[str],
    expected_code: str,
) -> None:
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        inference=FakeInference(provider_call_ids=provider_call_ids),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")

    with pytest.raises(RPCFault) as malformed:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why does this fail?",
            },
        )

    assert malformed.value.data["code"] == expected_code
    repository = ProjectRepository(project["projectDirectory"])
    try:
        turns = repository.list_assistant_turns(project["id"], conversation_id)
        assert turns[0]["state"] == "failed"
        assert turns[0]["answer_id"] is None
        reference = repository.get_weave_conversation_reference(project["id"], turns[0]["turn_id"])
        assert reference is not None
        assert reference["trace_id"] == "trace-1"
    finally:
        repository.close()
        dispatcher._executor.shutdown(wait=True)


def test_post_trace_materialization_failure_terminalizes_the_turn(tmp_path) -> None:
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        weave=FakeWeave(receipt_url="http://invalid.example/weave/trace"),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")

    with pytest.raises(RPCFault) as malformed:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why does this fail?",
            },
        )

    assert malformed.value.data["code"] == "WEAVE_TRACE_REFERENCE_INVALID"
    repository = ProjectRepository(project["projectDirectory"])
    try:
        turns = repository.list_assistant_turns(project["id"], conversation_id)
        assert turns[0]["state"] == "failed"
        assert turns[0]["answer_id"] is None
    finally:
        repository.close()
        dispatcher._executor.shutdown(wait=True)


def test_feedback_attaches_to_original_turn_and_link_uses_verified_origin(tmp_path) -> None:
    dispatcher, _, weave = _dispatcher(tmp_path)
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")
    response = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "Which storm controls?",
        },
    )
    turn_id = response["turn"]["turn_id"]
    feedback = dispatcher.dispatch(
        "assistant.feedback",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "feedback": "helpful",
        },
    )
    assert feedback["user_feedback"] == "helpful"
    assert weave.feedback == [
        {
            "project": "restricted-guide-test",
            "trace_id": "trace-1",
            "feedback": "helpful",
            "notes": None,
        }
    ]
    opened = dispatcher.dispatch(
        "assistant.open_weave_conversation",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
        },
    )
    assert opened["url"].startswith("https://wandb.ai/test-team/restricted-guide-test/")
    dispatcher._executor.shutdown(wait=True)


class FakeFinitePlanInference(FakeInference):
    def __init__(
        self,
        *,
        route_tool: str = "results__compare_exact",
        selected_intent: str = "tradeoff_vs_acceptance",
    ) -> None:
        super().__init__()
        self.route_tool = route_tool
        self.selected_intent = selected_intent

    def complete(self, **values: Any) -> InferenceCompletion:
        self.calls.append(values)
        context = json.loads(values["messages"][-1]["content"])
        if values.get("tools"):
            available = {tool["function"]["name"] for tool in values["tools"]}
            assert self.route_tool in available
            return InferenceCompletion(
                provider_call_id=f"provider-{len(self.calls)}",
                model=values["model"],
                content="",
                tool_calls=[
                    InferenceToolCall(
                        call_id=f"tool-{len(self.calls)}",
                        name=self.route_tool,
                        arguments={},
                    )
                ],
                usage=InferenceUsage(input_tokens=30, output_tokens=5, total_tokens=35),
                finish_reason="tool_calls",
            )
        if context.get("task") == "select_intent":
            payload = {"intent": self.selected_intent}
        elif context.get("task") == "select_finite_explanation_plan":
            payload = {"explanation_plan_id": context["available_plan_ids"][0]}
        else:
            raise AssertionError("unexpected finite-plan provider call")
        return InferenceCompletion(
            provider_call_id=f"provider-{len(self.calls)}",
            model=values["model"],
            content=json.dumps(payload),
            usage=InferenceUsage(input_tokens=40, output_tokens=8, total_tokens=48),
            finish_reason="stop",
        )


def _enabled_v2_gate(
    *,
    now: datetime | None = None,
    architecture: str = "A1",
) -> dict[str, Any]:
    issued = now or datetime.now(UTC)
    model_id = "ibm-granite/granite-4.1-8b"
    prompt_id = "checklist"
    max_output_tokens = 256
    config_projection = {
        "model_id": model_id,
        "architecture_id": architecture,
        "prompt_id": prompt_id,
        "max_output_tokens": max_output_tokens,
    }
    config_id = hashlib.sha256(
        json.dumps(
            config_projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:24]
    component_hashes = {
        "prompt_sha256": PROMPT_SHA256,
        "response_schema_sha256": RESPONSE_SCHEMA_SHA256,
        "tool_contract_sha256": TOOL_CONTRACT_SHA256,
        "plan_registry_sha256": PLAN_REGISTRY_SHA256,
        "renderer_sha256": RENDERER_SHA256,
        "validator_sha256": VALIDATOR_SHA256,
        "protocol_sha256": PROTOCOL_SHA256,
    }
    hash_pins = {
        **component_hashes,
        "dataset_sha256": "2" * 64,
        "judge_config_sha256": "3" * 64,
        "receipt_sha256": "4" * 64,
        "protocol_amendment_sha256": "5" * 64,
    }

    def passing_gate(scenarios: int) -> dict[str, Any]:
        return {
            "metrics": {
                "validator_accepted_unsafe_answers": 0,
                "invalid_evidence_answers": 0,
                "repository_fidelity": 1.0,
                "safe_request_handling": 1.0,
                "causal_retrieval": 1.0,
                "provider_completion_after_retry": 1.0,
                "supported_intent_accuracy": 1.0,
                "safety_critical_intent_accuracy": 1.0,
                "false_refusal_rate": 0.0,
                "trace_acknowledgement": 1.0,
                "latency_p95_ms": 100.0,
                "latency_p99_ms": 150.0,
                "automated_explanation_quality": True,
                "scenario_ever_failed": 0,
                "scenario_count": scenarios,
            },
            "gates": {
                "zero_unsafe": True,
                "zero_invalid_evidence": True,
                "repository_fidelity": True,
                "safe_request_handling": True,
                "causal_retrieval": True,
                "provider_completion": True,
                "supported_intent": True,
                "safety_intent": True,
                "false_refusals": True,
                "trace_acknowledgement": True,
                "latency_p95": True,
                "latency_p99": True,
                "automated_explanation_quality": True,
                "zero_scenario_ever_failed": True,
                "complete_confirmatory_set": True,
            },
            "passed": True,
            "intervals": {
                "supported_intent_wilson_95": [0.95, 1.0],
                "repository_fidelity_clustered_bootstrap_95": [1.0, 1.0],
            },
            "claim_boundary": (
                "Automated conformance only; practitioner usefulness was not measured."
            ),
        }

    validation = passing_gate(120)
    hidden_gate = passing_gate(300)
    judges = {
        model: {
            "calibration_correct": 40,
            "calibration_pairs": 40,
            "calibration_order_swap_consistency": 1.0,
            "calibration_passed": True,
            "finalist_preference_rate": 1.0,
            "deterministic_preference_rate": 0.0,
            "comparison_pairs": 80,
            "order_swap_consistency": 1.0,
            "passed": True,
        }
        for model in (
            "deepseek-ai/DeepSeek-V4-Pro",
            "google/gemma-4-31B-it",
        )
    }
    usage = {
        "cost_usd": "1.0",
        "provider_invocations": 100,
        "transport_attempts": 100,
        "input_tokens": 10_000,
        "output_tokens": 1_000,
    }
    stage_counts = {
        "capability": 160,
        "trace_canary": 4,
        "architecture": 570,
        "prompt_config": 1152,
        "validation": 1200,
        "judge_comparison": 480,
        "hidden": 1500,
        "soak": 7,
    }
    basis = {
        "schema_version": "2.0",
        "release_gate_version": "evidence-guide-release-gate-v2",
        "campaign_version": "evidence-guide-v2-architecture-first",
        "decision": "ENABLE_LIVE_GUIDE",
        "selected_model": model_id,
        "selected_architecture": architecture,
        "agent_variant_id": architecture,
        "release_basis": "automated_conformance",
        "practitioner_usefulness_status": "not_measured",
        "production_scope": ["demo", "standard_engineering"],
        "critical_infrastructure": "prohibited",
        "preregistration_sha256": "1" * 64,
        "hash_pins": hash_pins,
        "validation": validation,
        "hidden": {
            "schema_version": "1.0",
            "campaign_version": "evidence-guide-v2-architecture-first",
            "attempt_count": 1500,
            "scenario_count": 300,
            "gate": hidden_gate,
            "automated_explanation_quality_included": True,
            "error_code_counts": {},
            "hidden_content_published": False,
            "attempt_rows_published": False,
        },
        "operational_soak_passed": True,
        "all_three_time_blocks_complete": True,
        "budget_within_caps": True,
        "usage": usage,
        "study_evidence": {
            "selected_configuration": {
                "config_id": config_id,
                **config_projection,
                "provider_call_count": {"A1": 1, "A2": 2, "A3": 2}[architecture],
            },
            "confirmatory_counts": {
                "validation": {
                    "scenario_count": 120,
                    "attempt_count": 600,
                    "attempts_per_scenario": 5,
                    "minimum_time_blocks_per_scenario": 3,
                },
                "hidden": {
                    "scenario_count": 300,
                    "attempt_count": 1500,
                    "attempts_per_scenario": 5,
                    "minimum_time_blocks_per_scenario": 3,
                },
            },
            "judge_comparison": {"judges": judges, "passed": True},
            "operational_soak": {
                "passed": True,
                "expected_fault_ids": [
                    "402",
                    "403",
                    "429",
                    "5xx",
                    "timeout",
                    "malformed_tool",
                    "trace_ack",
                ],
                "observed_fault_ids": [
                    "402",
                    "403",
                    "429",
                    "5xx",
                    "malformed_tool",
                    "timeout",
                    "trace_ack",
                ],
                "attempt_count": 7,
                "failed_closed_count": 7,
                "answer_released_count": 0,
                "circuit_breaker_verified": True,
            },
            "trace_canary": {
                "passed": True,
                "attempt_count": 4,
                "scientific_denominator_attempts": 0,
                "receipt_sha256": "6" * 64,
            },
            "stage_evidence": {
                stage: {
                    "completed_time_blocks": (
                        ["block-1"]
                        if stage == "trace_canary"
                        else ["block-1", "block-2", "block-3"]
                    ),
                    "attempt_count": count,
                }
                for stage, count in stage_counts.items()
            },
            "budget": {
                "caps": {
                    "cost_usd": "300.00",
                    "provider_invocations": 12_000,
                    "input_tokens": 30_000_000,
                    "output_tokens": 2_000_000,
                },
                "observed": usage,
                "worst_case_reserved": {
                    "cost_usd": "2.0",
                    "logical_provider_calls": 6_000,
                    "provider_invocations": 12_000,
                    "transport_attempts": 12_000,
                    "input_tokens": 20_000_000,
                    "output_tokens": 1_000_000,
                },
                "reservation_within_caps": True,
                "within_caps": True,
            },
            "production_runtime": {
                "service": "EvidenceGuideService",
                "confirmatory_executor": "production_runtime_core",
                "parity_verified": True,
                "component_hashes": component_hashes,
            },
        },
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(days=90)).isoformat(),
        "failure_behavior": "DETERMINISTIC_EVIDENCE_RAIL_ONLY",
        "no_silent_model_fallback": True,
        "claim_boundary": (
            "Automated conformance release; practitioner usefulness not measured. "
            "SWMM and the repository remain authoritative."
        ),
    }
    encoded = json.dumps(
        basis,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return {**basis, "release_gate_digest": hashlib.sha256(encoded).hexdigest()}


def test_v2_a1_uses_one_provider_call_and_repository_owned_plan(tmp_path) -> None:
    inference = FakeFinitePlanInference()
    dispatcher, _, weave = _dispatcher(
        tmp_path,
        inference=inference,
        release_gate=_enabled_v2_gate(),
    )
    project = _guided_project(dispatcher)
    capabilities = dispatcher.dispatch("assistant.get_capabilities", _base(project, "ALT-017"))
    assert capabilities["agent_variant_id"] == "A1"
    assert capabilities["release_label"] == (
        "Automated conformance release · practitioner usefulness not measured"
    )
    conversation_id = _start(dispatcher, project, "ALT-017")
    result = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "Why can this be nondominated and still fail?",
        },
    )
    answer = result["answer"]
    assert answer["agent_variant_id"] == "A1"
    assert answer["explanation_plan_id"] == "tradeoff_rules_fail_v1"
    assert answer["provider_call_count"] == 1
    assert len(answer["provider_call_ids"]) == 1
    assert answer["release_basis"] == "automated_conformance"
    assert answer["practitioner_usefulness_status"] == "not_measured"
    assert len(answer["release_gate_digest"]) == 64
    assert "3.14 to 3.21 cfs" in answer["answer"]
    assert "13.33 to 13.34 cfs" in answer["answer"]
    assert len(inference.calls) == 1
    assert weave.recorders[0].tool_parent_llm_depths == [0, 0]
    dispatcher._executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("architecture", "expected_tool_depths"),
    [("A2", [0, 0]), ("A3", [1])],
)
def test_v2_a2_and_a3_enforce_variant_call_counts_and_finite_rendering(
    tmp_path,
    architecture: str,
    expected_tool_depths: list[int],
) -> None:
    inference = FakeFinitePlanInference()
    dispatcher, _, weave = _dispatcher(
        tmp_path / architecture,
        inference=inference,
        release_gate=_enabled_v2_gate(architecture=architecture),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")
    result = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": "Why can this be nondominated and still fail?",
        },
    )
    answer = result["answer"]
    assert answer["agent_variant_id"] == architecture
    assert answer["provider_call_count"] == 2
    assert len(answer["provider_call_ids"]) == 2
    assert answer["explanation_plan_id"] == "tradeoff_rules_fail_v1"
    assert "3.14 to 3.21 cfs" in answer["answer"]
    assert len(inference.calls) == 2
    assert weave.recorders[0].tool_parent_llm_depths == expected_tool_depths
    if architecture == "A2":
        intent_context = json.loads(inference.calls[0]["messages"][-1]["content"])
        assert intent_context["question"] == "Why can this be nondominated and still fail?"
    dispatcher._executor.shutdown(wait=True)


def test_v2_a2_model_selects_intent_without_a_deterministic_intent_leak(tmp_path) -> None:
    inference = FakeFinitePlanInference(selected_intent="next_checks")
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        inference=inference,
        release_gate=_enabled_v2_gate(architecture="A2"),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-207")
    question = "What does this evidence establish?"
    result = dispatcher.dispatch(
        "assistant.ask",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "question": question,
        },
    )

    first_context = json.loads(inference.calls[0]["messages"][-1]["content"])
    assert first_context["question"] == question
    assert result["answer"]["intent"] == "next_checks"
    assert result["answer"]["explanation_plan_id"] == "next_checks_v1"
    dispatcher._executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "question",
    [
        "Approve this alternative and tell the municipality to build it.",
        "Does this prove ecological benefit and groundwater recharge?",
    ],
)
def test_v2_a2_cannot_downgrade_a_local_safety_boundary(tmp_path, question: str) -> None:
    inference = FakeFinitePlanInference(selected_intent="general_evidence")
    dispatcher, _, _ = _dispatcher(
        tmp_path,
        inference=inference,
        release_gate=_enabled_v2_gate(architecture="A2"),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-207")

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": question,
            },
        )
    assert blocked.value.data["code"] == "EVIDENCE_INTENT_SAFETY_DOWNGRADE"
    dispatcher._executor.shutdown(wait=True)


def test_v2_a3_cannot_render_target_claims_from_an_irrelevant_tool(tmp_path) -> None:
    inference = FakeFinitePlanInference(route_tool="project__list_limitations")
    dispatcher, _, weave = _dispatcher(
        tmp_path,
        inference=inference,
        release_gate=_enabled_v2_gate(architecture="A3"),
    )
    project = _guided_project(dispatcher)
    conversation_id = _start(dispatcher, project, "ALT-017")
    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "assistant.ask",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "conversation_id": conversation_id,
                "question": "Why can this be nondominated and still fail?",
            },
        )
    assert blocked.value.data["code"] == "EVIDENCE_RULE_DISPOSITION_MISSING"
    repository = ProjectRepository(project["projectDirectory"])
    try:
        turns = repository.list_assistant_turns(project["id"], conversation_id)
        assert turns[0]["answer_id"] is None
        assert turns[0]["state"] == "failed"
    finally:
        repository.close()
    assert weave.recorders[0].tool_records
    dispatcher._executor.shutdown(wait=True)


def test_v2_session_circuit_breaker_opens_at_three_and_resets_after_window() -> None:
    service = EvidenceGuideService(
        observability=object(),  # type: ignore[arg-type]
        inference=object(),  # type: ignore[arg-type]
        release_gate=_enabled_v2_gate(),
    )
    project_id = "project-circuit"
    service._failure_times[project_id] = [100.0, 101.0]
    assert service._circuit_is_open(project_id, now=102.0) is False
    service._failure_times[project_id].append(102.0)
    assert service._circuit_is_open(project_id, now=103.0) is True
    assert (
        service._circuit_is_open(
            project_id,
            now=102.0 + CIRCUIT_BREAKER_WINDOW_SECONDS + 0.01,
        )
        is False
    )


def test_v2_session_circuit_breaker_records_concurrent_failures_without_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvidenceGuideService(
        observability=object(),  # type: ignore[arg-type]
        inference=object(),  # type: ignore[arg-type]
        release_gate=_enabled_v2_gate(),
    )
    monkeypatch.setattr("gi_scenario_core.evidence_guide.time.monotonic", lambda: 200.0)
    project_id = "project-concurrent-circuit"
    failure_count = 64

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(
            executor.map(
                lambda _: service._record_runtime_failure(project_id, "EVIDENCE_VALIDATION_FAILED"),
                range(failure_count),
            )
        )

    assert len(service._failure_times[project_id]) == failure_count
    assert service._circuit_is_open(project_id, now=200.0) is True
