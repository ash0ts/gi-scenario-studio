"""Short-lived no-tool Agents SDK child for bounded single-turn assistants.

The process receives one minimized authorized snapshot over framed stdin.  It
has no project-store, solver, filesystem-tool, shell, web, MCP, or generic RPC
access.  Its only external clients are the explicitly configured model endpoint
and W&B/Weave trace destination.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .assistants import (
    INTAKE_LIMITATION,
    _ProviderEvidenceAnswer,
    _ProviderIntakeAnswer,
)
from .contracts import ContractModel, canonical_sha256
from .native_agents_runtime import (
    NATIVE_AGENTS_MAX_TURNS,
    NATIVE_AGENTS_PROTOCOL,
    NATIVE_EVIDENCE_EXPLAIN_PROMPT,
    NATIVE_EVIDENCE_INTAKE_PROMPT,
    NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
    NativeAgentsTraceReceipt,
    NativeSingleTurnAgentsResult,
    NativeSingleTurnChildRequest,
    NativeSingleTurnChildResponse,
    safe_native_child_error_code,
)
from .review_brief_agent_runtime import (
    ReviewBriefAgentRuntimeError,
    _configure_weave_pii_redaction,
    _read_frame,
    _wandb_inference_project,
    _write_frame,
)
from .review_brief_evaluation import ReviewBriefAnswer

_PROMPTS = {
    "EVIDENCE_EXPLAIN": NATIVE_EVIDENCE_EXPLAIN_PROMPT,
    "EVIDENCE_INTAKE": NATIVE_EVIDENCE_INTAKE_PROMPT,
    "REVIEW_BRIEF_EVALUATION": NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
}
_OUTPUT_TYPES: dict[str, type[ContractModel]] = {
    "EVIDENCE_EXPLAIN": _ProviderEvidenceAnswer,
    "EVIDENCE_INTAKE": _ProviderIntakeAnswer,
    "REVIEW_BRIEF_EVALUATION": ReviewBriefAnswer,
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _model_input(child: NativeSingleTurnChildRequest) -> str:
    return _canonical(
        {
            "task": child.payload.intent,
            "authorized_context": child.payload.context,
        }
    )


def _collect_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"evidence_id", "evidence_alias"} and isinstance(nested, str):
                found.add(nested)
            elif key in {"evidence_ids", "evidence_aliases", "required_evidence_ids"} and (
                isinstance(nested, list)
            ):
                found.update(item for item in nested if isinstance(item, str))
            found.update(_collect_evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_collect_evidence_ids(nested))
    return found


@dataclass(slots=True)
class _InvocationContext:
    child: NativeSingleTurnChildRequest
    output_validated: bool = False
    output_sha256: str | None = None
    child_control_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.child_control_sha256 = canonical_sha256(
            self.child.model_dump(mode="json", exclude={"provider_api_key", "wandb_api_key"})
        )

    def revalidate(self) -> None:
        observed = canonical_sha256(
            self.child.model_dump(mode="json", exclude={"provider_api_key", "wandb_api_key"})
        )
        if observed != self.child_control_sha256:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_CONTROL_CHANGED")
        if canonical_sha256(self.child.payload.context) != self.child.payload.context_sha256:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_CONTEXT_CHANGED")
        if _sha(self.child.trace.association_id) != self.child.trace.association_id_sha256:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_ASSOCIATION_CHANGED")
        if _sha(self.child.trace.wandb_run_id) != self.child.trace.wandb_run_id_sha256:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_RUN_CHANGED")

    def validate_output(self, value: Any) -> None:
        self.revalidate()
        expected_type = _OUTPUT_TYPES[self.child.payload.operation]
        if not isinstance(value, expected_type):
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_SCHEMA_INVALID")
        authorized_evidence = _collect_evidence_ids(self.child.payload.context)
        if self.child.payload.operation == "EVIDENCE_EXPLAIN":
            assert isinstance(value, _ProviderEvidenceAnswer)
            if value.intent.value != self.child.payload.intent:
                raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_INTENT_MISMATCH")
            selected = {item.evidence_id for item in value.items}
            if not selected.issubset(authorized_evidence):
                raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_EVIDENCE_UNAUTHORIZED")
        elif self.child.payload.operation == "EVIDENCE_INTAKE":
            assert isinstance(value, _ProviderIntakeAnswer)
            pages = {
                (str(item.get("document_id")), int(item.get("page_number"))): str(
                    item.get("text", "")
                )
                for item in self.child.payload.context.get("pages", [])
                if isinstance(item, dict)
                and isinstance(item.get("document_id"), str)
                and isinstance(item.get("page_number"), int)
            }
            for item in value.draft_fields:
                for citation in item.citations:
                    text = pages.get((citation.document_id, citation.page_number))
                    if (
                        text is None
                        or citation.end > len(text)
                        or text[citation.start : citation.end] != citation.quote
                    ):
                        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_CITATION_INVALID")
            if INTAKE_LIMITATION not in value.limitations:
                raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_LIMITATION_MISSING")
        else:
            assert isinstance(value, ReviewBriefAnswer)
            cited = set(value.evidence_ids)
            cited.update(item.evidence_id for item in value.numeric_claims)
            cited.update(
                evidence_id for finding in value.findings for evidence_id in finding.evidence_ids
            )
            if not cited.issubset(authorized_evidence):
                raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_EVIDENCE_UNAUTHORIZED")
        output = value.model_dump(mode="json")
        self.output_sha256 = canonical_sha256(output)
        self.output_validated = True


class _ReceiptWeaveTracingProcessor:
    """Delegate native Agents events to Weave and add a hash-only receipt gate."""

    def __init__(self, *, context: _InvocationContext) -> None:
        from weave.integrations.openai_agents.openai_agents import WeaveTracingProcessor

        self._delegate = WeaveTracingProcessor()
        self.context = context
        self.root_call_id: str | None = None

    def on_trace_start(self, trace: Any) -> None:
        self._delegate.on_trace_start(trace)
        call = self._delegate._trace_calls.get(trace.trace_id)
        call_id = str(getattr(call, "id", "") or "") if call is not None else ""
        if not call_id:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_WEAVE_ROOT_MISSING")
        self.root_call_id = call_id

    def on_trace_end(self, trace: Any) -> None:
        trace_data = self._delegate._trace_data.get(trace.trace_id)
        if trace_data is None:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_WEAVE_TRACE_MISSING")
        child = self.context.child
        trace_data.setdefault("metadata", {})["ecocorridor_native_agent_gate"] = {
            "protocol_sha256": _sha(NATIVE_AGENTS_PROTOCOL),
            "operation_sha256": _sha(child.payload.operation),
            "payload_sha256": canonical_sha256(child.payload.model_dump(mode="json")),
            "context_sha256": child.payload.context_sha256,
            "project_id_sha256": child.authorization.project_id_sha256,
            "analysis_id_sha256": child.authorization.analysis_id_sha256,
            "model_revision_id_sha256": child.authorization.model_revision_id_sha256,
            "authorization_sha256": child.authorization.authorization_sha256,
            "scope_evidence_sha256": child.authorization.scope_evidence_sha256,
            "association_id_sha256": child.trace.association_id_sha256,
            "wandb_run_id_sha256": child.trace.wandb_run_id_sha256,
            "output_sha256": self.context.output_sha256,
            "output_validated": self.context.output_validated,
            "read_only": True,
            "tool_call_count": 0,
            "max_turns": NATIVE_AGENTS_MAX_TURNS,
            "session_enabled": False,
        }
        self._delegate.on_trace_end(trace)

    def on_span_start(self, span: Any) -> None:
        self._delegate.on_span_start(span)

    def on_span_end(self, span: Any) -> None:
        self._delegate.on_span_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self) -> None:
        self._delegate.force_flush()


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _trace_gate(record: Any) -> dict[str, Any]:
    output = _value(record, "output", {})
    metadata = _value(output, "metadata", {})
    gate = _value(metadata, "ecocorridor_native_agent_gate", {})
    if not isinstance(gate, dict):
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_GATE_MISSING")
    return gate


def _validate_trace_url(url: str, child: NativeSingleTurnChildRequest) -> str:
    parsed = urlsplit(url)
    expected = urlsplit(child.weave_target.web_base_url)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or child.weave_target.entity not in parsed.path.split("/")
        or child.weave_target.project not in parsed.path.split("/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_URL_INVALID")
    return url


def _expected_trace_gate(context: _InvocationContext) -> dict[str, Any]:
    child = context.child
    return {
        "protocol_sha256": _sha(NATIVE_AGENTS_PROTOCOL),
        "operation_sha256": _sha(child.payload.operation),
        "payload_sha256": canonical_sha256(child.payload.model_dump(mode="json")),
        "context_sha256": child.payload.context_sha256,
        "project_id_sha256": child.authorization.project_id_sha256,
        "analysis_id_sha256": child.authorization.analysis_id_sha256,
        "model_revision_id_sha256": child.authorization.model_revision_id_sha256,
        "authorization_sha256": child.authorization.authorization_sha256,
        "scope_evidence_sha256": child.authorization.scope_evidence_sha256,
        "association_id_sha256": child.trace.association_id_sha256,
        "wandb_run_id_sha256": child.trace.wandb_run_id_sha256,
        "output_sha256": context.output_sha256,
        "output_validated": True,
        "read_only": True,
        "tool_call_count": 0,
        "max_turns": NATIVE_AGENTS_MAX_TURNS,
        "session_enabled": False,
    }


def _readback_trace(
    *,
    client: Any,
    processor: _ReceiptWeaveTracingProcessor,
    context: _InvocationContext,
) -> NativeAgentsTraceReceipt:
    processor.force_flush()
    client.flush()
    if not processor.root_call_id:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_WEAVE_ROOT_MISSING")
    record = client.get_call(processor.root_call_id)
    attributes = _value(record, "attributes", {})
    if _value(attributes, "agent_trace_id") != context.child.trace.trace_id:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_ID_MISMATCH")
    expected_wandb_run_id = (
        f"{context.child.weave_target.entity}/"
        f"{context.child.weave_target.project}/"
        f"{context.child.trace.wandb_run_id}"
    )
    if _value(record, "wb_run_id") != expected_wandb_run_id:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_WANDB_ASSOCIATION_MISMATCH")
    output = _value(record, "output", {})
    if _value(output, "status") != "completed":
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_INCOMPLETE")
    gate = _trace_gate(record)
    if gate != _expected_trace_gate(context):
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_GATE_MISMATCH")
    url = str(_value(record, "ui_url", "") or "")
    if not url:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_TRACE_URL_MISSING")
    return NativeAgentsTraceReceipt(
        agents_trace_id=context.child.trace.trace_id,
        weave_call_id=processor.root_call_id,
        association_id=context.child.trace.association_id,
        wandb_run_id=context.child.trace.wandb_run_id,
        trace_url=_validate_trace_url(url, context.child),
    )


def _configure_environment(child: NativeSingleTurnChildRequest) -> None:
    runtime = Path.cwd().resolve()
    if runtime.is_symlink():
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_RUNTIME_ROOT_UNSAFE")
    for name in ("wandb", "cache", "artifacts", "data", "weave-cache"):
        directory = runtime / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or runtime not in directory.resolve().parents:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_RUNTIME_PATH_UNSAFE")
    os.environ.update(
        {
            "WANDB_API_KEY": child.wandb_api_key.get_secret_value(),
            "WANDB_BASE_URL": child.weave_target.api_base_url,
            "WANDB_MODE": "online",
            "WANDB_DIR": str(runtime / "wandb"),
            "WANDB_CACHE_DIR": str(runtime / "cache"),
            "WANDB_ARTIFACT_DIR": str(runtime / "artifacts"),
            "WANDB_DATA_DIR": str(runtime / "data"),
            "WANDB_CONSOLE": "off",
            "WANDB_DISABLE_CODE": "true",
            "WANDB_DISABLE_GIT": "true",
            "WANDB_ERROR_REPORTING": "false",
            "WANDB_SHOW_RUN": "false",
            "WANDB_SILENT": "true",
            "WEAVE_PRINT_CALL_LINK": "false",
            "WEAVE_CAPTURE_CODE": "false",
            "WEAVE_CAPTURE_CLIENT_INFO": "false",
            "WEAVE_CAPTURE_SYSTEM_INFO": "false",
            "WEAVE_REDACT_PII": "true",
            "WEAVE_SERVER_CACHE_DIR": str(runtime / "weave-cache"),
            "WEAVE_ENABLE_DISK_FALLBACK": "false",
        }
    )
    _configure_weave_pii_redaction(runtime)


def _model(child: NativeSingleTurnChildRequest) -> Any:
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI

    routing_project = _wandb_inference_project(
        provider_base_url=child.provider.base_url,
        weave_target=child.weave_target,
    )
    client_kwargs: dict[str, Any] = {
        "api_key": child.provider_api_key.get_secret_value(),
        "base_url": child.provider.base_url,
        "timeout": child.provider.timeout_seconds,
        "max_retries": 0,
    }
    if routing_project is not None:
        client_kwargs["project"] = routing_project
    client = AsyncOpenAI(
        **client_kwargs,
    )
    if child.provider.mode == "RESPONSES":
        return OpenAIResponsesModel(
            model=child.provider.model,
            openai_client=client,
            model_is_explicit=True,
        )
    return OpenAIChatCompletionsModel(
        model=child.provider.model,
        openai_client=client,
        strict_feature_validation=True,
        buffer_streamed_tool_calls=False,
    )


def _usage(result: Any) -> tuple[int, int, str]:
    input_tokens = sum(int(response.usage.input_tokens) for response in result.raw_responses)
    output_tokens = sum(int(response.usage.output_tokens) for response in result.raw_responses)
    response_ids = [
        str(response.response_id or response.request_id or "") for response in result.raw_responses
    ]
    provider_call_id = next((value for value in reversed(response_ids) if value), "")
    if not provider_call_id:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_PROVIDER_CALL_ID_MISSING")
    return input_tokens, output_tokens, provider_call_id


async def _execute(child: NativeSingleTurnChildRequest) -> NativeSingleTurnChildResponse:
    import weave
    from agents import (
        Agent,
        GuardrailFunctionOutput,
        ModelSettings,
        RunConfig,
        Runner,
        input_guardrail,
        output_guardrail,
        set_trace_processors,
    )

    context = _InvocationContext(child=child)
    context.revalidate()
    _configure_environment(child)
    client = weave.init(
        f"{child.weave_target.entity}/{child.weave_target.project}",
        settings={
            "print_call_link": False,
            "capture_code": False,
            "capture_client_info": False,
            "capture_system_info": False,
            # Native Agents tracing is owned by the sole processor installed
            # below; automatic provider patching would create a second trace
            # path outside the bounded Agents conversation.
            "implicitly_patch_integrations": False,
            "redact_pii": True,
            "use_server_cache": False,
            "enable_disk_fallback": False,
            "http_timeout": min(child.provider.timeout_seconds, 60.0),
        },
        global_attributes={
            "app_namespace": "ecocorridor",
            "native_agent_operation_sha256": _sha(child.payload.operation),
            "project_id_sha256": child.authorization.project_id_sha256,
            "analysis_id_sha256": child.authorization.analysis_id_sha256,
            "model_revision_id_sha256": child.authorization.model_revision_id_sha256,
        },
    )
    set_run_context = getattr(client, "set_wandb_run_context", None)
    if not callable(set_run_context):
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_WANDB_ASSOCIATION_UNAVAILABLE")
    set_run_context(run_id=child.trace.wandb_run_id)
    processor = _ReceiptWeaveTracingProcessor(context=context)
    set_trace_processors([processor])

    model_input = _model_input(child)

    @input_guardrail(name="native_single_turn_fixed_input", run_in_parallel=False)
    async def fixed_input_guard(_wrapper: Any, _agent: Any, value: Any) -> GuardrailFunctionOutput:
        valid = value == model_input
        return GuardrailFunctionOutput(
            output_info={"input_sha256": _sha(model_input), "valid": valid},
            tripwire_triggered=not valid,
        )

    @output_guardrail(name="native_single_turn_closed_output")
    async def closed_output_guard(
        _wrapper: Any, _agent: Any, value: Any
    ) -> GuardrailFunctionOutput:
        try:
            context.validate_output(value)
        except (ValueError, ReviewBriefAgentRuntimeError):
            return GuardrailFunctionOutput(
                output_info={"validated": False}, tripwire_triggered=True
            )
        return GuardrailFunctionOutput(output_info={"validated": True}, tripwire_triggered=False)

    output_type = _OUTPUT_TYPES[child.payload.operation]
    agent = Agent[_InvocationContext](
        name="EcoCorridor Bounded Evidence Agent",
        instructions=_PROMPTS[child.payload.operation],
        model=_model(child),
        model_settings=ModelSettings(
            temperature=0,
            parallel_tool_calls=False,
            include_usage=True,
            store=False,
        ),
        tools=[],
        output_type=output_type,
        input_guardrails=[fixed_input_guard],
        output_guardrails=[closed_output_guard],
        reset_tool_choice=True,
    )
    started = time.monotonic()
    result = await asyncio.wait_for(
        Runner.run(
            agent,
            model_input,
            context=context,
            max_turns=NATIVE_AGENTS_MAX_TURNS,
            session=None,
            previous_response_id=None,
            conversation_id=None,
            run_config=RunConfig(
                trace_include_sensitive_data=False,
                workflow_name="EcoCorridor Bounded Evidence Agent",
                trace_id=child.trace.trace_id,
                group_id=child.trace.wandb_run_id_sha256,
                trace_metadata={
                    "operation_sha256": _sha(child.payload.operation),
                    "payload_sha256": canonical_sha256(child.payload.model_dump(mode="json")),
                    "project_id_sha256": child.authorization.project_id_sha256,
                    "analysis_id_sha256": child.authorization.analysis_id_sha256,
                    "association_id_sha256": child.trace.association_id_sha256,
                },
            ),
        ),
        timeout=child.provider.timeout_seconds,
    )
    if not context.output_validated or context.output_sha256 is None:
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_NOT_VALIDATED")
    receipt = _readback_trace(client=client, processor=processor, context=context)
    input_tokens, output_tokens, provider_call_id = _usage(result)
    final_output = result.final_output
    if not isinstance(final_output, _OUTPUT_TYPES[child.payload.operation]):
        raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_OUTPUT_SCHEMA_INVALID")
    output = final_output.model_dump(mode="json")
    native_result = NativeSingleTurnAgentsResult(
        operation=child.payload.operation,
        output=output,
        output_sha256=canonical_sha256(output),
        provider_call_id_sha256=_sha(provider_call_id),
        provider_model=child.provider.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        trace=receipt,
    )
    return NativeSingleTurnChildResponse(ok=True, result=native_result)


def main() -> int:
    try:
        payload = _read_frame(sys.stdin.buffer)
        child = NativeSingleTurnChildRequest.model_validate_json(payload)
        response = asyncio.run(_execute(child))
    except Exception as exc:  # noqa: BLE001 - boundary emits only a finite safe code
        response = NativeSingleTurnChildResponse(
            protocol_version=NATIVE_AGENTS_PROTOCOL,
            ok=False,
            error_code=safe_native_child_error_code(exc),
        )
    _write_frame(sys.stdout.buffer, response.model_dump_json().encode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess smoke test exercises this
    raise SystemExit(main())


__all__ = ["main"]
