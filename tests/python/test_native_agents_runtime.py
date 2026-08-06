from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents.models.interface import Model
from ecocorridor_core.assistants import (
    INTAKE_LIMITATION,
    AssistantRunAssociation,
    _ProviderEvidenceAnswer,
    _ProviderIntakeAnswer,
)
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.native_agents_process import (
    _execute,
    _InvocationContext,
    _readback_trace,
    _ReceiptWeaveTracingProcessor,
    _validate_trace_url,
)
from ecocorridor_core.native_agents_runtime import (
    NATIVE_AGENTS_MAX_TURNS,
    NATIVE_EVIDENCE_EXPLAIN_PROMPT,
    NATIVE_EVIDENCE_INTAKE_PROMPT,
    NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
    NativeAgentAuthorization,
    NativeAgentsTraceReceipt,
    NativeSingleTurnAgentsExecutor,
    NativeSingleTurnAgentsResult,
    NativeSingleTurnChildRequest,
    NativeSingleTurnChildResponse,
    NativeSingleTurnPayload,
    PopenNativeSingleTurnSubprocess,
    _trace_url_matches,
)
from ecocorridor_core.review_brief_agent_runtime import (
    REVIEW_BRIEF_MAX_CONTEXT_BYTES,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentCredentialLease,
    ReviewBriefAgentProvider,
    ReviewBriefAgentRuntimeError,
    ReviewBriefAgentTraceControl,
    ReviewBriefWeaveTarget,
    _json_bytes,
    _read_frame,
)
from ecocorridor_core.review_brief_evaluation import ReviewBriefAnswer


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provider() -> ReviewBriefAgentProvider:
    return ReviewBriefAgentProvider(
        base_url="https://api.openai.example/v1",
        model="review-model-1",
        mode="CHAT_COMPLETIONS",
    )


def _target() -> ReviewBriefWeaveTarget:
    return ReviewBriefWeaveTarget(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        external_project_id_sha256=_sha("wandb-project-1"),
    )


def _association() -> AssistantRunAssociation:
    return AssistantRunAssociation.create(
        project_id="project-1",
        wandb_external_project_id="wandb-project-1",
        wandb_run_id="review-run-1",
        wandb_run_kind="review_brief_evaluation",
        run_subject_id="portfolio-1",
        analysis_id="analysis-1",
    )


def _authorization() -> NativeAgentAuthorization:
    return NativeAgentAuthorization(
        project_id_sha256=_sha("project-1"),
        analysis_id_sha256=_sha("analysis-1"),
        model_revision_id_sha256=_sha("revision-1"),
        authorization_sha256=_sha("authorization-1"),
        scope_evidence_sha256=_sha("scope-1"),
    )


def _capability() -> ReviewBriefAgentCapabilityReceipt:
    return ReviewBriefAgentCapabilityReceipt(
        provider_mode="CHAT_COMPLETIONS",
        trace_id="trace_" + "a" * 32,
        trace_url=("https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/preflight"),
    )


def _trace() -> ReviewBriefAgentTraceControl:
    association = _association()
    return ReviewBriefAgentTraceControl(
        trace_id="trace_" + "b" * 32,
        association_id=association.association_id,
        association_id_sha256=_sha(association.association_id),
        wandb_run_id=association.wandb_run_id,
        wandb_run_id_sha256=_sha(association.wandb_run_id),
    )


def _payload(
    operation: str = "EVIDENCE_EXPLAIN",
    *,
    context: dict[str, Any] | None = None,
) -> NativeSingleTurnPayload:
    if operation == "EVIDENCE_EXPLAIN":
        intent = "TRACE_EVIDENCE"
        body = context or {"claims": [{"evidence_alias": "E1"}]}
    elif operation == "EVIDENCE_INTAKE":
        intent = "Extract the review purpose."
        body = context or {
            "pages": [{"document_id": "DOC1", "page_number": 1, "text": "Review purpose"}]
        }
    else:
        intent = "PREPARE_REVIEW_BRIEF"
        body = context or {"required_evidence_ids": ["E-RIDGEWAY-1"]}
    return NativeSingleTurnPayload.create(operation=operation, intent=intent, context=body)


def _child(
    operation: str = "EVIDENCE_EXPLAIN",
    *,
    context: dict[str, Any] | None = None,
) -> NativeSingleTurnChildRequest:
    return NativeSingleTurnChildRequest(
        payload=_payload(operation, context=context),
        provider=_provider(),
        weave_target=_target(),
        trace=_trace(),
        authorization=_authorization(),
        provider_api_key="provider-test-key-123456",
        wandb_api_key="not-a-secret-value",
    )


def test_fixed_operations_have_exact_exported_prompts_and_no_schema_escape_hatch() -> None:
    assert NATIVE_AGENTS_MAX_TURNS == 1
    assert "local deterministic renderer" in NATIVE_EVIDENCE_EXPLAIN_PROMPT
    assert "exact document" in NATIVE_EVIDENCE_INTAKE_PROMPT
    assert "ReviewBriefAnswer" in NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT
    assert "schema" not in NativeSingleTurnPayload.model_fields
    with pytest.raises(ValueError, match="fixed task"):
        NativeSingleTurnPayload.create(
            operation="REVIEW_BRIEF_EVALUATION",
            intent="FREE_FORM",
            context={},
        )


def test_child_secrets_are_redacted_except_explicit_ipc_payload() -> None:
    child = _child()
    provider_key = child.provider_api_key.get_secret_value()
    wandb_key = child.wandb_api_key.get_secret_value()
    assert provider_key not in repr(child)
    assert wandb_key not in child.model_dump_json()
    frame = bytearray(_json_bytes(child.ipc_payload()))
    try:
        assert provider_key.encode() in frame
        assert wandb_key.encode() in frame
    finally:
        frame[:] = b"\0" * len(frame)
    assert set(frame) == {0}


def test_source_and_frozen_commands_have_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = PopenNativeSingleTurnSubprocess(scratch_root=tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert launcher.command() == [
        sys.executable,
        "-I",
        "-m",
        "ecocorridor_core.native_agents_process",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launcher.command() == [sys.executable, "--native-assistant-agent-subprocess"]
    assert "WANDB_API_KEY" not in launcher._environment(tmp_path)
    assert "OPENAI_API_KEY" not in launcher._environment(tmp_path)


class _FakeSubprocess:
    def __init__(self) -> None:
        self.requests: list[NativeSingleTurnChildRequest] = []

    def invoke(
        self, *, request: NativeSingleTurnChildRequest, deadline_seconds: float
    ) -> NativeSingleTurnChildResponse:
        assert deadline_seconds == 45
        self.requests.append(request)
        output = {
            "intent": "TRACE_EVIDENCE",
            "items": [{"evidence_id": "E1", "explanation_kind": "DIRECT_PAIR_METRIC"}],
        }
        result = NativeSingleTurnAgentsResult(
            operation=request.payload.operation,
            output=output,
            output_sha256=canonical_sha256(output),
            provider_call_id_sha256=_sha("provider-call-1"),
            provider_model=request.provider.model,
            input_tokens=9,
            output_tokens=3,
            elapsed_ms=25,
            trace=NativeAgentsTraceReceipt(
                agents_trace_id=request.trace.trace_id,
                weave_call_id="weave-call-1",
                association_id=request.trace.association_id,
                wandb_run_id=request.trace.wandb_run_id,
                trace_url=(
                    "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/weave-call-1"
                ),
            ),
        )
        return NativeSingleTurnChildResponse(ok=True, result=result)


def _executor(
    subprocess_client: _FakeSubprocess,
    *,
    credential_factory: Any | None = None,
) -> NativeSingleTurnAgentsExecutor:
    @contextmanager
    def credentials() -> Iterator[ReviewBriefAgentCredentialLease]:
        lease = ReviewBriefAgentCredentialLease("p" * 20, "w" * 20)
        yield lease
        lease.provider_api_key = ""
        lease.wandb_api_key = ""

    return NativeSingleTurnAgentsExecutor(
        provider=_provider(),
        weave_target=_target(),
        association=_association(),
        authorization=_authorization(),
        capability_receipt=_capability(),
        credential_lease=credential_factory or credentials,
        subprocess_client=subprocess_client,
        deadline_seconds=45,
    )


@pytest.mark.parametrize("suffix", ["?download=1", "#other-trace"])
def test_native_agent_trace_receipts_reject_query_and_fragment(suffix: str) -> None:
    child = _child()
    trace_url = "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/call-1" + suffix
    with pytest.raises(ValueError, match="undecorated HTTP"):
        NativeAgentsTraceReceipt(
            agents_trace_id=child.trace.trace_id,
            weave_call_id="weave-call-1",
            association_id=child.trace.association_id,
            wandb_run_id=child.trace.wandb_run_id,
            trace_url=trace_url,
        )
    assert _trace_url_matches(trace_url, child.weave_target) is False
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TRACE_URL_INVALID"):
        _validate_trace_url(trace_url, child)


def test_executor_requires_verified_provider_preflight_and_validates_receipt() -> None:
    with pytest.raises(ValueError, match="preflight mode"):
        NativeSingleTurnAgentsExecutor(
            provider=_provider(),
            weave_target=_target(),
            association=_association(),
            authorization=_authorization(),
            capability_receipt=_capability().model_copy(update={"provider_mode": "RESPONSES"}),
            credential_lease=lambda: ReviewBriefAgentCredentialLease("p" * 20, "w" * 20),
            subprocess_client=_FakeSubprocess(),
        )
    fake = _FakeSubprocess()
    result = _executor(fake).execute(
        operation="EVIDENCE_EXPLAIN",
        intent="TRACE_EVIDENCE",
        context={"claims": [{"evidence_alias": "E1"}]},
    )
    assert result.output["items"][0]["evidence_id"] == "E1"
    assert len(fake.requests) == 1
    assert fake.requests[0].trace.trace_id.startswith("trace_")


def test_executor_binds_non_analysis_intake_to_immutable_run_subject() -> None:
    association = AssistantRunAssociation.create(
        project_id="project-1",
        wandb_external_project_id="wandb-project-1",
        wandb_run_id="bundle-run-1",
        wandb_run_kind="bundle_import",
        run_subject_id="project-1",
    )
    authorization = _authorization().model_copy(
        update={"analysis_id_sha256": _sha(association.run_subject_id)}
    )
    executor = NativeSingleTurnAgentsExecutor(
        provider=_provider(),
        weave_target=_target(),
        association=association,
        authorization=authorization,
        capability_receipt=_capability(),
        credential_lease=lambda: ReviewBriefAgentCredentialLease("p" * 20, "w" * 20),
        subprocess_client=_FakeSubprocess(),
    )
    assert executor.association.analysis_id is None
    assert executor.authorization.analysis_id_sha256 == _sha("project-1")


def test_executor_checks_context_limit_before_credential_lease() -> None:
    accessed = False

    def credentials() -> ReviewBriefAgentCredentialLease:
        nonlocal accessed
        accessed = True
        return ReviewBriefAgentCredentialLease("p" * 20, "w" * 20)

    fake = _FakeSubprocess()
    executor = _executor(fake, credential_factory=credentials)
    context = {"claims": [{"evidence_alias": "E1", "padding": "x" * 70_000}]}
    assert len(_json_bytes(context)) > REVIEW_BRIEF_MAX_CONTEXT_BYTES
    with pytest.raises(ReviewBriefAgentRuntimeError, match="CONTEXT_TOO_LARGE"):
        executor.execute(
            operation="EVIDENCE_EXPLAIN",
            intent="TRACE_EVIDENCE",
            context=context,
        )
    assert accessed is False
    assert fake.requests == []


def test_operation_specific_output_validation_is_closed_and_authorized() -> None:
    evidence_context = _InvocationContext(child=_child())
    evidence_context.validate_output(
        _ProviderEvidenceAnswer.model_validate(
            {
                "intent": "TRACE_EVIDENCE",
                "items": [{"evidence_id": "E1", "explanation_kind": "DIRECT_PAIR_METRIC"}],
            }
        )
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="EVIDENCE_UNAUTHORIZED"):
        _InvocationContext(child=_child()).validate_output(
            _ProviderEvidenceAnswer.model_validate(
                {
                    "intent": "TRACE_EVIDENCE",
                    "items": [{"evidence_id": "E9", "explanation_kind": "DIRECT_PAIR_METRIC"}],
                }
            )
        )

    intake_context = _InvocationContext(child=_child("EVIDENCE_INTAKE"))
    intake_context.validate_output(
        _ProviderIntakeAnswer.model_validate(
            {
                "draft_fields": [
                    {
                        "field_name": "purpose",
                        "value": "Review",
                        "citations": [
                            {
                                "document_id": "DOC1",
                                "page_number": 1,
                                "start": 0,
                                "end": 6,
                                "quote": "Review",
                            }
                        ],
                    }
                ],
                "limitations": [INTAKE_LIMITATION],
            }
        )
    )
    evaluation_context = _InvocationContext(child=_child("REVIEW_BRIEF_EVALUATION"))
    evaluation_context.validate_output(
        ReviewBriefAnswer(
            disposition="ABSTAINED",
            summary="The fixed evidence boundary prevents this brief from being released.",
            limitations=["Qualified ecological and field review remains required."],
            next_action="Resolve the recorded evidence boundary before another evaluation.",
        )
    )


class _ScriptedOneTurnModel(Model):
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.calls = 0
        self.kwargs: dict[str, Any] | None = None

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        self.calls += 1
        self.kwargs = kwargs
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="message-1",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            annotations=[], type="output_text", text=json.dumps(self.output)
                        )
                    ],
                )
            ],
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5, requests=1),
            response_id="response-1",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        async def never() -> Any:
            if False:  # pragma: no cover - make this an async generator
                yield None

        return never()


class _FakeWeaveCall:
    def __init__(self, *, call_id: str, attributes: dict[str, Any], wb_run_id: str | None) -> None:
        self.id = call_id
        self.trace_id = "weave-" + call_id
        self.attributes = attributes
        self.wb_run_id = wb_run_id
        self.output: dict[str, Any] | None = None
        self.ui_url = "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/" + call_id
        self.ended_at: str | None = None


class _FakeWeaveClient:
    def __init__(self) -> None:
        self.calls: dict[str, _FakeWeaveCall] = {}
        self.counter = 0
        self.run_id: str | None = None

    def set_wandb_run_context(self, *, run_id: str, step: int | None = None) -> None:
        self.run_id = run_id

    def create_call(self, **kwargs: Any) -> _FakeWeaveCall:
        self.counter += 1
        call = _FakeWeaveCall(
            call_id=f"call-{self.counter}",
            attributes=dict(kwargs.get("attributes", {})),
            wb_run_id=(
                "wandb/ecocorridor-scenario-studio/" + self.run_id
                if self.run_id is not None
                else None
            ),
        )
        self.calls[call.id] = call
        return call

    def finish_call(
        self,
        call: _FakeWeaveCall,
        *,
        output: dict[str, Any] | None = None,
        exception: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        call.output = output or {"error": type(exception).__name__ if exception else None}
        call.ended_at = "done"

    def flush(self) -> None:
        return

    def get_call(self, call_id: str) -> _FakeWeaveCall:
        return self.calls[call_id]


@pytest.mark.parametrize(
    ("operation", "output"),
    [
        (
            "EVIDENCE_EXPLAIN",
            {
                "intent": "TRACE_EVIDENCE",
                "items": [{"evidence_id": "E1", "explanation_kind": "DIRECT_PAIR_METRIC"}],
            },
        ),
        (
            "EVIDENCE_INTAKE",
            {
                "draft_fields": [
                    {
                        "field_name": "purpose",
                        "value": "Review",
                        "citations": [
                            {
                                "document_id": "DOC1",
                                "page_number": 1,
                                "start": 0,
                                "end": 6,
                                "quote": "Review",
                            }
                        ],
                    }
                ],
                "limitations": [INTAKE_LIMITATION],
            },
        ),
        (
            "REVIEW_BRIEF_EVALUATION",
            {
                "disposition": "ABSTAINED",
                "role": None,
                "summary": "The fixed evidence boundary prevents this brief from release.",
                "findings": [],
                "evidence_ids": [],
                "numeric_claims": [],
                "limitations": ["Qualified ecological and field review remains required."],
                "next_action": "Resolve the evidence boundary before another evaluation.",
            },
        ),
    ],
)
def test_pinned_agents_sdk_runs_one_no_tool_turn_with_only_weave_processor(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    output: dict[str, Any],
) -> None:
    import agents
    import ecocorridor_core.native_agents_process as process_module
    import weave
    import weave.integrations.openai_agents.openai_agents as integration

    child = _child(operation)
    model = _ScriptedOneTurnModel(output)
    client = _FakeWeaveClient()
    installed: list[list[Any]] = []
    real_set = agents.set_trace_processors

    def set_processors(processors: list[Any]) -> None:
        installed.append(processors)
        real_set(processors)

    monkeypatch.setattr(agents, "set_trace_processors", set_processors)
    monkeypatch.setattr(weave, "init", lambda *args, **kwargs: client)
    monkeypatch.setattr(integration, "get_weave_client", lambda: client)
    monkeypatch.setattr(process_module, "_model", lambda _child: model)
    monkeypatch.setattr(process_module, "_configure_environment", lambda _child: None)
    try:
        response = asyncio.run(_execute(child))
    finally:
        real_set([])

    assert response.ok is True
    assert response.result is not None
    assert model.calls == 1
    assert response.result.input_tokens == 3
    assert response.result.trace.readback_verified is True
    assert client.run_id == child.trace.wandb_run_id
    assert len(installed) == 1 and len(installed[0]) == 1
    processor = installed[0][0]
    assert isinstance(processor, _ReceiptWeaveTracingProcessor)
    gate = processor._delegate._trace_data
    assert all("provider-test-key" not in json.dumps(value) for value in gate.values())


def test_exact_weave_readback_rejects_wrong_predetermined_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import weave.integrations.openai_agents.openai_agents as integration

    class Delegate:
        def __init__(self) -> None:
            self._trace_data: dict[str, Any] = {}
            self._trace_calls: dict[str, Any] = {}

        def on_trace_start(self, trace: Any) -> None:
            self._trace_data[trace.trace_id] = {"metadata": {}}
            self._trace_calls[trace.trace_id] = SimpleNamespace(id="call-1")

        def on_trace_end(self, trace: Any) -> None:
            return

        def on_span_start(self, span: Any) -> None:
            return

        def on_span_end(self, span: Any) -> None:
            return

        def force_flush(self) -> None:
            return

        def shutdown(self) -> None:
            return

    monkeypatch.setattr(integration, "WeaveTracingProcessor", Delegate)
    context = _InvocationContext(child=_child())
    context.validate_output(
        _ProviderEvidenceAnswer.model_validate(
            {
                "intent": "TRACE_EVIDENCE",
                "items": [{"evidence_id": "E1", "explanation_kind": "DIRECT_PAIR_METRIC"}],
            }
        )
    )
    processor = _ReceiptWeaveTracingProcessor(context=context)
    trace = SimpleNamespace(trace_id=context.child.trace.trace_id)
    processor.on_trace_start(trace)
    processor.on_trace_end(trace)
    gate = processor._delegate._trace_data[trace.trace_id]["metadata"]
    record = SimpleNamespace(
        attributes={"agent_trace_id": "trace_" + "f" * 32},
        output={"status": "completed", "metadata": gate},
        ui_url="https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/call-1",
    )
    client = SimpleNamespace(flush=lambda: None, get_call=lambda _call_id: record)
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TRACE_ID_MISMATCH"):
        _readback_trace(client=client, processor=processor, context=context)
    record.attributes["agent_trace_id"] = context.child.trace.trace_id
    record.wb_run_id = "wandb/ecocorridor-scenario-studio/wrong-run"
    with pytest.raises(ReviewBriefAgentRuntimeError, match="WANDB_ASSOCIATION_MISMATCH"):
        _readback_trace(client=client, processor=processor, context=context)


def test_malformed_child_frame_returns_safe_rejection(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "ecocorridor_core.native_agents_process"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(b"00000002\n{}", timeout=20)
    assert process.returncode == 0
    response = NativeSingleTurnChildResponse.model_validate_json(_read_frame(io.BytesIO(stdout)))
    assert response.ok is False
    assert response.error_code == "NATIVE_AGENT_INVOCATION_FAILED"
    assert b"test-key" not in stderr
