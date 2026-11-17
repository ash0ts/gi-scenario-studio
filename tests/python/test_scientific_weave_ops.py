from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import gi_scenario_core.observability as observability_module
import pytest
from gi_scenario_core.contracts import (
    ObservabilityBinding,
    ProjectDataPolicy,
    WeaveTraceReference,
)
from gi_scenario_core.observability import (
    CredentialSession,
    ObservabilityService,
    WeaveAdapter,
    WeaveAgentTurnRecorder,
    _agent_conversation_url,
    _prepare_trace_value,
    _validate_agent_turn_acknowledgement,
)
from gi_scenario_core.repository import ProjectRepository


def _documents() -> tuple[ProjectDataPolicy, ObservabilityBinding]:
    policy = ProjectDataPolicy(
        policy_id="policy_1",
        project_id="project_customer_name",
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="engineer",
    )
    binding = ObservabilityBinding(
        binding_id="binding_1",
        project_id=policy.project_id,
        data_policy_id=policy.policy_id,
        entity="example-team",
        external_project="restricted-evidence",
        external_project_id="external-project-1",
        deployment="wandb_cloud",
        mode="online",
        verified=True,
        verified_at=datetime.now(UTC),
    )
    return policy, binding


def _bound_repository(tmp_path) -> ProjectRepository:
    repository = ProjectRepository(tmp_path / "project")
    policy, binding = _documents()
    repository.create_project_with_observability(
        title="Customer drainage study",
        project_kind="existing",
        project_id=policy.project_id,
        policy=policy,
        binding=binding,
    )
    repository.create_analysis(
        policy.project_id,
        analysis_id="analysis_customer_name",
        payload={"metric_extractor_version": "extractor-v2"},
    )
    return repository


class _FakeScientificWeave:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def trace_scientific_operation(self, binding, **kwargs):
        if self.fail:
            raise RuntimeError("WEAVE_UNAVAILABLE")
        self.calls.append(kwargs)
        return WeaveTraceReference(
            project_id=binding.project_id,
            call_id=kwargs["call_id"],
            entity=binding.entity,
            external_project=binding.external_project,
            trace_id=f"trace-{kwargs['call_id']}",
            provider_call_id=f"provider-{kwargs['call_id']}",
            url=f"https://wandb.ai/weave/{kwargs['call_id']}",
        )


class _FakePolicyWandb:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def validate_binding(self, binding, policy):
        self.calls += 1
        if self.reject:
            raise RuntimeError("WANDB_PROJECT_ACCESS_NOT_RESTRICTED")
        assert binding.external_project_id == "external-project-1"
        assert binding.access == "Restricted"
        assert policy.deployment == binding.deployment
        return binding


def test_service_links_deduplicates_and_hashes_local_subjects(tmp_path) -> None:
    repository = _bound_repository(tmp_path)
    adapter = _FakeScientificWeave()
    wandb = _FakePolicyWandb()
    service = ObservabilityService(wandb_adapter=wandb, weave_adapter=adapter)
    try:
        first = service.trace_scientific_operation(
            repository,
            project_id="project_customer_name",
            analysis_id="analysis_customer_name",
            operation="exact_result_ingestion",
            inputs={"planned_job_count": 8, "storm_count": 4},
            output={"ingested_exact_job_count": 8, "metric_count": 40},
        )
        second = service.trace_scientific_operation(
            repository,
            project_id="project_customer_name",
            analysis_id="analysis_customer_name",
            operation="exact_result_ingestion",
            inputs={"planned_job_count": 8, "storm_count": 4},
            output={"ingested_exact_job_count": 8, "metric_count": 40},
        )

        assert first["status"] == "traced"
        assert first["wandb_run_link_state"] == "planned"
        assert len(first["wandb_run_id"]) == 24
        assert second["status"] == "reused"
        assert len(adapter.calls) == 1
        cloud_payload = json.dumps(adapter.calls[0], sort_keys=True)
        assert "project_customer_name" not in cloud_payload
        assert "analysis_customer_name" not in cloud_payload
        assert len(adapter.calls[0]["inputs"]["project_key"]) == 64
        assert adapter.calls[0]["wandb_run_id"] == first["wandb_run_id"]
        assert wandb.calls == 2
    finally:
        repository.close()


def test_scientific_trace_failure_cannot_change_engineering_state(tmp_path) -> None:
    repository = _bound_repository(tmp_path)
    service = ObservabilityService(
        wandb_adapter=_FakePolicyWandb(),
        weave_adapter=_FakeScientificWeave(fail=True),
    )
    try:
        before = repository.get_analysis("analysis_customer_name")
        result = service.trace_scientific_operation(
            repository,
            project_id="project_customer_name",
            analysis_id="analysis_customer_name",
            operation="adverse_effect_evaluation",
            inputs={"storm_count": 4},
            output={"adverse_alternative_count": 2},
        )

        assert result == {"status": "failed", "error_code": "WEAVE_UNAVAILABLE"}
        assert repository.get_analysis("analysis_customer_name") == before
        assert repository.table_counts()["weave_trace_references"] == 0
    finally:
        repository.close()


def test_scientific_trace_skips_when_live_restricted_policy_revalidation_fails(
    tmp_path,
) -> None:
    repository = _bound_repository(tmp_path)
    adapter = _FakeScientificWeave()
    wandb = _FakePolicyWandb(reject=True)
    service = ObservabilityService(wandb_adapter=wandb, weave_adapter=adapter)
    try:
        before = repository.get_analysis("analysis_customer_name")
        result = service.trace_scientific_operation(
            repository,
            project_id="project_customer_name",
            analysis_id="analysis_customer_name",
            operation="exact_result_ingestion",
            inputs={"planned_job_count": 8},
            output={"ingested_exact_job_count": 8},
        )

        assert result == {
            "status": "skipped",
            "reason": "exact_restricted_policy_revalidation_failed",
        }
        assert wandb.calls == 1
        assert adapter.calls == []
        assert repository.get_analysis("analysis_customer_name") == before
        assert repository.table_counts()["weave_trace_references"] == 0
    finally:
        repository.close()


class _FakeSanitize:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def add_redact_key(self, key: str) -> None:
        self.keys.append(key)


class _FakeWeaveClient:
    def __init__(self) -> None:
        self.project_id = "example-team/restricted-evidence"
        self.run_context: tuple[str, int | None] | None = None
        self.created: dict[str, Any] | None = None
        self.output: Any = None
        self.context_clear_count = 0

    def set_wandb_run_context(self, run_id: str, step: int | None = None) -> None:
        self.run_context = (run_id, step)

    def clear_wandb_run_context(self) -> None:
        self.run_context = None
        self.context_clear_count += 1

    def create_call(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(trace_id="trace-1", id="call-1", ui_url="https://trace/1")

    def finish_call(self, call, output=None) -> None:
        self.output = output

    def flush(self) -> None:
        return None

    def get_call(self, call_id: str):
        assert call_id == "call-1"
        return SimpleNamespace(ui_url="https://trace/1")


def test_regular_weave_op_disables_presidio_and_redacts_before_send(monkeypatch) -> None:
    client = _FakeWeaveClient()
    init_calls: list[dict[str, Any]] = []
    current: dict[str, Any] = {"client": None}
    sanitize = _FakeSanitize()
    weave_module = types.ModuleType("weave")

    def init(project: str, *, settings: dict[str, Any]):
        init_calls.append({"project": project, "settings": settings})
        current["client"] = client
        return client

    weave_module.init = init  # type: ignore[attr-defined]
    weave_module.get_client = lambda: current["client"]  # type: ignore[attr-defined]
    utils_module = types.ModuleType("weave.utils")
    utils_module.sanitize = sanitize  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave", weave_module)
    monkeypatch.setitem(sys.modules, "weave.utils", utils_module)

    credentials = CredentialSession()
    credentials.configure("a" * 32, "https://api.wandb.ai")
    adapter = WeaveAdapter(credentials)
    _policy, binding = _documents()
    trace = adapter.trace_scientific_operation(
        binding,
        call_id="scientific-safe",
        operation="adaptive_search_batch_proposal",
        inputs={
            "email": "engineer@example.com",
            "notes": "API key=definitely-not-for-a-trace",
            "local_context": "/Users/private/person/model.inp",
        },
        output={"authorization": "Bearer secret-value-12345", "selected": 8},
        wandb_run_id="wandb-run-1",
    )
    assert trace.provider_call_id == "call-1"
    assert init_calls[0]["settings"] == {"redact_pii": False}
    assert client.run_context is None
    assert client.context_clear_count == 1
    assert client.created is not None
    serialized = json.dumps({"inputs": client.created["inputs"], "output": client.output})
    assert "engineer@example.com" not in serialized
    assert "definitely-not-for-a-trace" not in serialized
    assert "/Users/private" not in serialized
    assert "secret-value" not in serialized
    assert "[redacted:email]" in serialized
    assert any(key == "authorization" for key in sanitize.keys)

    adapter.trace_scientific_operation(
        binding,
        call_id="scientific-safe-second",
        operation="exact_result_ingestion",
        inputs={"planned": 8},
        output={"ingested": 8},
        wandb_run_id="wandb-run-2",
    )
    assert len(init_calls) == 1
    assert client.context_clear_count == 2


def test_regular_weave_op_clears_explicit_run_context_after_failure(monkeypatch) -> None:
    client = _FakeWeaveClient()
    sanitize = _FakeSanitize()
    weave_module = types.ModuleType("weave")

    def init(_project: str, *, settings: dict[str, Any]):
        assert settings == {"redact_pii": False}
        return client

    def fail_create_call(**_kwargs):
        raise RuntimeError("synthetic create failure")

    client.create_call = fail_create_call  # type: ignore[method-assign]
    weave_module.init = init  # type: ignore[attr-defined]
    weave_module.get_client = lambda: client  # type: ignore[attr-defined]
    utils_module = types.ModuleType("weave.utils")
    utils_module.sanitize = sanitize  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave", weave_module)
    monkeypatch.setitem(sys.modules, "weave.utils", utils_module)

    credentials = CredentialSession()
    credentials.configure("a" * 32, "https://api.wandb.ai")
    adapter = WeaveAdapter(credentials)
    _policy, binding = _documents()

    with pytest.raises(RuntimeError, match="synthetic create failure"):
        adapter.trace_scientific_operation(
            binding,
            call_id="scientific-failure",
            operation="adaptive_search_batch_proposal",
            inputs={"selected": 8},
            output={"accepted": 4},
            wandb_run_id="wandb-run-1",
        )

    assert client.run_context is None
    assert client.context_clear_count == 1


def test_agent_turn_reuses_preinitialized_client_without_reinitializing_weave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AgentClient:
        project_id = "example-team/restricted-evidence"

        def get_agent_turn(self, *, trace_id: str) -> Any:
            return SimpleNamespace(
                trace_id=trace_id,
                conversation_id="conversation-1",
                root_span_name="gi-evidence-guide",
                agent_name="gi-evidence-guide",
                agent_version="evidence-guide-v2",
                status_code="OK",
                total_duration_ms=5,
                messages=[
                    SimpleNamespace(type="user_message", status_code="OK"),
                    SimpleNamespace(type="tool_call", status_code="OK"),
                    SimpleNamespace(type="assistant_message", status_code="OK"),
                ],
            )

    class Turn:
        _otel_span = SimpleNamespace(
            get_span_context=lambda: SimpleNamespace(trace_id=1, span_id=2)
        )

    class Conversation:
        def start_turn(self, **_kwargs: Any):
            return nullcontext(Turn())

    client = AgentClient()
    init_calls = 0
    weave_module = types.ModuleType("weave")

    def init(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal init_calls
        init_calls += 1
        return client

    @contextmanager
    def start_conversation(**_kwargs: Any):
        yield Conversation()

    weave_module.init = init  # type: ignore[attr-defined]
    weave_module.get_client = lambda: client  # type: ignore[attr-defined]
    weave_module.start_conversation = start_conversation  # type: ignore[attr-defined]
    utils_module = types.ModuleType("weave.utils")
    utils_module.sanitize = _FakeSanitize()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave", weave_module)
    monkeypatch.setitem(sys.modules, "weave.utils", utils_module)
    monkeypatch.setattr(observability_module, "_flush_agent_spans", lambda: None)

    credentials = CredentialSession()
    credentials.configure("a" * 32, "https://api.wandb.ai")
    adapter = WeaveAdapter(credentials, preinitialized_client=client)
    _policy, binding = _documents()
    with adapter.agent_turn(
        binding,
        conversation_id="conversation-1",
        conversation_name="Evidence investigation",
        question="Why did this alternative fail?",
        model="ibm-granite/granite-4.1-8b",
        include_content=True,
        attributes={"wandb_run_id": "run-1"},
    ) as recorder:
        pass

    assert init_calls == 0
    assert recorder.receipt is not None
    assert recorder.receipt.trace_id == f"{1:032x}"


def test_agent_turn_acknowledgement_checks_exposed_identity_and_completion() -> None:
    acknowledgement = SimpleNamespace(
        trace_id="trace-1",
        root_span_name="gi-evidence-guide",
        agent_name="gi-evidence-guide",
        agent_version="evidence-guide-v2",
        status_code="UNSET",
        total_duration_ms=12,
        messages=[
            SimpleNamespace(type="user_message", status_code="OK"),
            SimpleNamespace(type="tool_call", status_code="OK"),
            SimpleNamespace(type="assistant_message", status_code="OK"),
        ],
    )

    _validate_agent_turn_acknowledgement(
        acknowledgement,
        trace_id="trace-1",
        conversation_id="conversation-1",
        agent_name="gi-evidence-guide",
        agent_version="evidence-guide-v2",
    )

    acknowledgement.agent_name = "another-agent"
    with pytest.raises(RuntimeError, match="WEAVE_AGENT_NAME_MISMATCH"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v1",
        )


def test_agent_turn_acknowledgement_rejects_error_and_incomplete_turns() -> None:
    acknowledgement = {
        "trace_id": "trace-1",
        "conversation_id": "conversation-1",
        "root_span_name": "gi-evidence-guide",
        "agent_name": "gi-evidence-guide",
        "agent_version": "evidence-guide-v2",
        "status_code": "ERROR",
        "total_duration_ms": 12,
        "messages": [
            {"type": "user_message", "status_code": "OK"},
            {"type": "tool_call", "status_code": "OK"},
            {"type": "assistant_message", "status_code": "OK"},
        ],
    }
    with pytest.raises(RuntimeError, match="WEAVE_AGENT_TURN_FAILED"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v2",
        )

    acknowledgement["status_code"] = "OK"
    acknowledgement["messages"] = []
    with pytest.raises(RuntimeError, match="WEAVE_AGENT_TURN_INCOMPLETE"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v2",
        )


def test_expected_local_rejection_requires_and_accepts_error_turn_status() -> None:
    acknowledgement = {
        "trace_id": "trace-1",
        "conversation_id": "conversation-1",
        "root_span_name": "gi-evidence-guide",
        "agent_name": "gi-evidence-guide",
        "agent_version": "evidence-guide-v2",
        "status_code": "ERROR",
        "total_duration_ms": 12,
        "messages": [{"type": "user_message", "status_code": "OK"}],
    }

    _validate_agent_turn_acknowledgement(
        acknowledgement,
        trace_id="trace-1",
        conversation_id="conversation-1",
        agent_name="gi-evidence-guide",
        agent_version="evidence-guide-v2",
        expected_error=True,
    )

    with pytest.raises(RuntimeError, match="WEAVE_AGENT_TURN_FAILED"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v2",
        )

    acknowledgement["status_code"] = "UNSET"
    with pytest.raises(RuntimeError, match="WEAVE_AGENT_TURN_STATUS_MISMATCH"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v2",
            expected_error=True,
        )


class _FakeRootTurn:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.errors: list[Exception] = []
        self.output_messages: list[Any] = []

    def set_attributes(self, value: dict[str, Any]) -> None:
        self.attributes.update(value)

    def _record_otel_error(self, error: Exception) -> None:
        self.errors.append(error)

    def record(self, *, output_messages: list[Any]) -> None:
        self.output_messages = output_messages


def _recorder_for_turn(turn: _FakeRootTurn, *, include_content: bool = True):
    recorder = object.__new__(WeaveAgentTurnRecorder)
    recorder.weave = object()
    recorder.client = object()
    recorder.turn = turn
    recorder.include_content = include_content
    recorder.trace_id = "trace"
    recorder.root_span_id = "span"
    recorder.receipt = None
    recorder.expected_turn_error = False
    recorder.validation_recorded = False
    recorder.final_answer_recorded = False
    recorder.required_message_types = {"user_message"}
    recorder.llm_completion_count = 0
    recorder.tool_completion_count = 0
    return recorder


def test_deterministic_turn_acknowledgement_does_not_require_a_tool_call() -> None:
    acknowledgement = {
        "trace_id": "trace-1",
        "conversation_id": "conversation-1",
        "root_span_name": "gi-evidence-guide",
        "agent_name": "gi-evidence-guide",
        "agent_version": "evidence-guide-v2",
        "status_code": "OK",
        "total_duration_ms": 12,
        "messages": [
            {"type": "user_message", "status_code": "OK"},
            {"type": "assistant_message", "status_code": "OK"},
        ],
    }

    _validate_agent_turn_acknowledgement(
        acknowledgement,
        trace_id="trace-1",
        conversation_id="conversation-1",
        agent_name="gi-evidence-guide",
        agent_version="evidence-guide-v2",
        required_message_types={"user_message", "assistant_message"},
    )

    with pytest.raises(RuntimeError, match="WEAVE_AGENT_TURN_INCOMPLETE"):
        _validate_agent_turn_acknowledgement(
            acknowledgement,
            trace_id="trace-1",
            conversation_id="conversation-1",
            agent_name="gi-evidence-guide",
            agent_version="evidence-guide-v2",
            required_message_types={
                "user_message",
                "tool_call",
                "assistant_message",
            },
        )


def test_trace_operation_contract_is_independent_of_acknowledgement_shape() -> None:
    turn = _FakeRootTurn()
    recorder = _recorder_for_turn(turn)
    recorder.validation(valid=True, checked_claim_count=0, error_codes=[])

    with pytest.raises(RuntimeError, match="WEAVE_TRACE_OPERATION_COUNT_MISMATCH"):
        recorder.assert_operations(llm_calls=0, tool_calls=1, final_answer=False)

    recorder.assert_operations(llm_calls=0, tool_calls=0, final_answer=False)
    assert turn.attributes["gi.trace.expected_tool_calls"] == 0


def test_invalid_validation_marks_root_turn_error_without_losing_audit_state() -> None:
    turn = _FakeRootTurn()
    recorder = _recorder_for_turn(turn)

    recorder.validation(
        valid=False,
        checked_claim_count=0,
        error_codes=["EVIDENCE_ID_FABRICATED"],
    )

    assert recorder.expected_turn_error is True
    assert turn.attributes["gi.validation.passed"] is False
    assert turn.attributes["gi.validation.error_codes"] == ["EVIDENCE_ID_FABRICATED"]
    assert [str(error) for error in turn.errors] == [
        "EVIDENCE_GUIDE_VALIDATION_REJECTED:EVIDENCE_ID_FABRICATED"
    ]
    with pytest.raises(RuntimeError, match="WEAVE_REJECTED_TURN_CANNOT_RECORD_ANSWER"):
        recorder.record_final_answer("This must not escape.")


def test_materialized_answer_is_recorded_on_root_turn_with_policy_redaction(
    monkeypatch,
) -> None:
    class Message:
        def __init__(self, *, role: str, content: str) -> None:
            self.role = role
            self.content = content

    conversation_module = types.ModuleType("weave.conversation")
    conversation_module.Message = Message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave.conversation", conversation_module)
    turn = _FakeRootTurn()
    recorder = _recorder_for_turn(turn, include_content=False)
    recorder.validation(valid=True, checked_claim_count=2, error_codes=[])

    recorder.record_final_answer("Canonical answer for a private project")

    assert recorder.final_answer_recorded is True
    assert len(turn.output_messages) == 1
    assert turn.output_messages[0].role == "assistant"
    assert turn.output_messages[0].content == "[content omitted by project policy]"


def test_llm_trace_canonicalizes_openai_tool_messages_as_text(monkeypatch) -> None:
    class Message:
        def __init__(self, *, role: str, content: str, tool_call_id: str = "") -> None:
            if not isinstance(content, str):
                raise TypeError("message content must be text")
            self.role = role
            self.content = content
            self.tool_call_id = tool_call_id

    class Usage:
        def __init__(self, *, input_tokens: int, output_tokens: int) -> None:
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens

    class Span:
        def __init__(self) -> None:
            self.recorded: dict[str, Any] | None = None

        def record(self, **values: Any) -> None:
            self.recorded = values

    class FakeLlmWeave:
        def __init__(self) -> None:
            self.span = Span()

        @contextmanager
        def start_llm(self, **_metadata: Any):
            yield self.span

    conversation_module = types.ModuleType("weave.conversation")
    conversation_module.Message = Message  # type: ignore[attr-defined]
    conversation_module.Usage = Usage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave.conversation", conversation_module)
    turn = _FakeRootTurn()
    recorder = _recorder_for_turn(turn)
    recorder.weave = FakeLlmWeave()

    tool_protocol = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "tool-1",
                "type": "function",
                "function": {"name": "evidence.resolve", "arguments": "{}"},
            }
        ],
    }
    with recorder.llm(
        model="ibm-granite/granite-4.1-8b",
        system_instructions=["Use repository evidence only."],
        input_messages=[tool_protocol],
    ) as handle:
        handle.record(
            SimpleNamespace(
                content="",
                usage=SimpleNamespace(input_tokens=7, output_tokens=3),
                provider_call_id="provider-1",
                model="ibm-granite/granite-4.1-8b",
                finish_reason="stop",
                tool_calls=[SimpleNamespace(call_id="tool-1")],
            )
        )

    recorded = recorder.weave.span.recorded
    assert recorded is not None
    message = recorded["input_messages"][0]
    assert isinstance(message.content, str)
    assert json.loads(message.content)["tool_calls"][0]["id"] == "tool-1"
    assert recorded["output_messages"][0].content == "[read-only evidence tool request]"
    assert recorded["output_type"] == "tool_calls"


def test_agent_conversation_link_uses_verified_custom_web_origin() -> None:
    _policy, original = _documents()
    binding = ObservabilityBinding.model_validate(
        {
            **original.model_dump(mode="json"),
            "deployment": "self_managed",
            "web_base_url": "https://wandb.internal.example/platform",
        }
    )

    assert _agent_conversation_url(binding, "conversation:1") == (
        "https://wandb.internal.example/platform/example-team/restricted-evidence/"
        "weave/agents/conversations/conversation%3A1"
    )


class _FakeFeedbackTarget:
    def __init__(self, *, acknowledge: bool) -> None:
        self.acknowledge = acknowledge
        self.created: list[SimpleNamespace] = []
        self.refresh_count = 0

    def _create(self, feedback_id: str) -> str:
        self.created.append(SimpleNamespace(id=feedback_id, span_trace_id="trace-1"))
        return feedback_id

    def add_reaction(self, emoji: str) -> str:
        assert emoji == "👍"
        return self._create("feedback-reaction")

    def add(self, feedback_type: str, payload: dict[str, Any]) -> str:
        assert feedback_type == "gi.evidence_guide.feedback"
        assert payload == {"label": "helpful"}
        return self._create("feedback-label")

    def add_note(self, note: str) -> str:
        assert note == "Useful explanation"
        return self._create("feedback-note")

    def refresh(self):
        self.refresh_count += 1
        return SimpleNamespace(items=self.created if self.acknowledge else [])


class _FakeFeedbackClient:
    def __init__(self, target: _FakeFeedbackTarget) -> None:
        self.project_id = "example-team/restricted-evidence"
        self.target = target
        self.flush_count = 0

    def get_agent_turn_feedback(self, trace_id: str) -> _FakeFeedbackTarget:
        assert trace_id == "trace-1"
        return self.target

    def flush(self) -> None:
        self.flush_count += 1


def _install_fake_weave(monkeypatch, client: Any) -> None:
    weave_module = types.ModuleType("weave")
    weave_module.init = lambda *_args, **_kwargs: client  # type: ignore[attr-defined]
    weave_module.get_client = lambda: client  # type: ignore[attr-defined]
    utils_module = types.ModuleType("weave.utils")
    utils_module.sanitize = _FakeSanitize()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weave", weave_module)
    monkeypatch.setitem(sys.modules, "weave.utils", utils_module)


def test_feedback_requires_flush_and_exact_server_readback(monkeypatch) -> None:
    target = _FakeFeedbackTarget(acknowledge=True)
    client = _FakeFeedbackClient(target)
    _install_fake_weave(monkeypatch, client)
    credentials = CredentialSession()
    credentials.configure("a" * 32, "https://api.wandb.ai")
    adapter = WeaveAdapter(credentials)
    _policy, binding = _documents()

    adapter.add_agent_feedback(
        binding,
        trace_id="trace-1",
        feedback="helpful",
        notes="Useful explanation",
    )

    assert client.flush_count == 1
    assert target.refresh_count == 1


def test_feedback_missing_server_readback_fails_closed(monkeypatch) -> None:
    target = _FakeFeedbackTarget(acknowledge=False)
    client = _FakeFeedbackClient(target)
    _install_fake_weave(monkeypatch, client)
    monkeypatch.setattr(observability_module.time, "sleep", lambda _seconds: None)
    credentials = CredentialSession()
    credentials.configure("a" * 32, "https://api.wandb.ai")
    adapter = WeaveAdapter(credentials)
    _policy, binding = _documents()

    with pytest.raises(RuntimeError, match="WEAVE_FEEDBACK_NOT_ACKNOWLEDGED"):
        adapter.add_agent_feedback(
            binding,
            trace_id="trace-1",
            feedback="helpful",
            notes=None,
        )

    assert client.flush_count == 1
    assert target.refresh_count == 3


def test_feedback_service_normalizes_adapter_errors(tmp_path) -> None:
    class FailingFeedbackWeave:
        def add_agent_feedback(self, *_args, **_kwargs) -> None:
            raise RuntimeError("provider rejected /Users/private/model.inp")

    repository = _bound_repository(tmp_path)
    service = ObservabilityService(
        wandb_adapter=_FakePolicyWandb(),
        weave_adapter=FailingFeedbackWeave(),
    )
    try:
        with pytest.raises(RuntimeError, match="WEAVE_AGENT_FEEDBACK_RUNTIMEERROR"):
            service.add_evidence_guide_feedback(
                repository,
                project_id="project_customer_name",
                trace_id="trace-1",
                feedback="helpful",
                notes=None,
            )
    finally:
        repository.close()


class _FakeToolWeave:
    def __init__(self) -> None:
        self.arguments: str | None = None
        self.span = SimpleNamespace(result=None)

    @contextmanager
    def start_tool(self, *, name: str, arguments: str, tool_call_id: str):
        self.arguments = arguments
        yield self.span


def test_standard_project_tool_trace_is_hash_only() -> None:
    fake = _FakeToolWeave()
    recorder = object.__new__(WeaveAgentTurnRecorder)
    recorder.weave = fake
    recorder.client = object()
    recorder.turn = object()
    recorder.include_content = False
    recorder.trace_id = "trace"
    recorder.root_span_id = "span"
    recorder.receipt = None
    recorder.required_message_types = {"user_message"}
    recorder.llm_completion_count = 0
    recorder.tool_completion_count = 0

    with recorder.tool(
        name="evidence.resolve",
        arguments={"evidence_id": "claim-private", "email": "person@example.com"},
        tool_call_id="tool-1",
    ) as handle:
        handle.record({"project_name": "Private project", "value": 12.5})

    assert fake.arguments is not None
    recorded_arguments = json.loads(fake.arguments)
    assert recorded_arguments == {
        "arguments_sha256": recorded_arguments["arguments_sha256"],
        "content_logged": False,
    }
    assert len(recorded_arguments["arguments_sha256"]) == 64
    assert "claim-private" not in fake.arguments
    assert fake.span.result == {
        "content_logged": False,
        "result_sha256": fake.span.result["result_sha256"],
    }


def test_repository_redactor_is_deterministic_for_full_synthetic_content() -> None:
    value = {
        "contact": "engineer@example.com",
        "phone_note": "Phone: +1 (555) 234-5678",
        "secret": "not-for-cloud",
        "portable": "Synthetic hydraulic value 3.21 cfs",
    }
    first = _prepare_trace_value(value, context="test trace")
    second = _prepare_trace_value(value, context="test trace")

    assert first == second
    serialized = json.dumps(first)
    assert "engineer@example.com" not in serialized
    assert "555" not in serialized
    assert "not-for-cloud" not in serialized
    assert "3.21 cfs" in serialized
