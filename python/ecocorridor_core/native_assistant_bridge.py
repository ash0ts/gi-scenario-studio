"""Bridge existing deterministic assistants to native Weave Agents traces.

The evidence and intake implementations still own authorization, evidence
resolution, deterministic rendering, and post-provider validation.  This
bridge replaces their former manual Weave-Op trace sink and HTTP provider with
one short-lived OpenAI Agents SDK invocation.  The familiar trace-sink methods
below are local lifecycle bookkeeping only; they never create cloud Ops.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from scenario_platform.cloud_sync import InMemoryCredentialSession, WandbTarget

from .assistants import (
    APPROVED_TRACE_STEPS,
    AssistantEvidenceAuthorization,
    AssistantProvider,
    AssistantRunAssociation,
    NestedTraceSink,
    ProviderResponse,
    TraceReadback,
    validate_feedback_comment,
)
from .contracts import canonical_sha256
from .native_agents_runtime import (
    NativeAgentAuthorization,
    NativeSingleTurnAgentsExecutor,
    NativeSingleTurnAgentsResult,
)

ExecutorFactory = Callable[
    [AssistantRunAssociation, NativeAgentAuthorization], NativeSingleTurnAgentsExecutor
]
ScopeVerifier = Callable[[], None]


class WeaveFeedbackTarget(Protocol):
    entity: str
    project: str
    api_base_url: str


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def attach_verified_weave_feedback(
    *,
    target: WeaveFeedbackTarget,
    credentials: InMemoryCredentialSession,
    scope_verifier: ScopeVerifier,
    weave_call_id: str,
    agents_trace_id: str,
    wandb_run_id: str,
    rating: str,
    comment: str,
    feedback_type: str,
    client_feedback_id: str,
) -> dict[str, Any]:
    """Attach feedback only after exact server trace and association readback."""

    if rating not in {"HELPFUL", "NOT_HELPFUL"}:
        raise ValueError("assistant feedback rating is invalid")
    validate_feedback_comment(comment)
    if (
        not all((weave_call_id, agents_trace_id, wandb_run_id, feedback_type, client_feedback_id))
        or len(client_feedback_id) > 256
    ):
        raise ValueError("assistant feedback trace identity is incomplete")
    scope_verifier()
    payload = {
        "client_feedback_id": client_feedback_id,
        "rating": rating,
        "comment": comment,
    }
    with credentials.environment():
        previous = {
            key: os.environ.get(key)
            for key in ("WANDB_BASE_URL", "WANDB_CONSOLE", "WANDB_DISABLE_CODE")
        }
        os.environ.update(
            {
                "WANDB_BASE_URL": target.api_base_url,
                "WANDB_CONSOLE": "off",
                "WANDB_DISABLE_CODE": "true",
            }
        )
        try:
            import weave

            client = weave.init(
                f"{target.entity}/{target.project}",
                settings={
                    "print_call_link": False,
                    "capture_code": False,
                    "capture_client_info": False,
                    "capture_system_info": False,
                    "implicitly_patch_integrations": False,
                    "redact_pii": True,
                    "use_server_cache": False,
                    "enable_disk_fallback": False,
                },
            )
            trace_record = client.get_call(weave_call_id)
            attributes = _record_value(trace_record, "attributes", {})
            expected_run = f"{target.entity}/{target.project}/{wandb_run_id}"
            if (
                trace_record is None
                or str(_record_value(trace_record, "id", "")) != weave_call_id
                or _record_value(attributes, "agent_trace_id") != agents_trace_id
                or _record_value(trace_record, "wb_run_id") != expected_run
                or _record_value(_record_value(trace_record, "output", {}), "status") != "completed"
            ):
                raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_TRACE_INVALID")
            prior_feedback = [
                item
                for item in list(trace_record.feedback)
                if _record_value(item, "feedback_type") == feedback_type
                and _record_value(_record_value(item, "payload", {}), "client_feedback_id")
                == client_feedback_id
            ]
            if len(prior_feedback) > 1:
                raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_DUPLICATED")
            if prior_feedback:
                if _record_value(prior_feedback[0], "payload") != payload:
                    raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_ID_REUSED")
                feedback_id = str(_record_value(prior_feedback[0], "id", ""))
            else:
                created = trace_record.feedback.add(feedback_type, payload)
                client.flush()
                feedback_id = str(
                    created
                    if isinstance(created, str)
                    else (
                        created.get("id", "")
                        if isinstance(created, dict)
                        else getattr(created, "id", "")
                    )
                )
            records = list(client.get_feedback(feedback_id)) if feedback_id else []
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    scope_verifier()
    record = records[0] if len(records) == 1 else None
    record_id = _record_value(record, "id", "")
    record_payload = _record_value(record, "payload")
    expected_ref = _record_value(getattr(trace_record, "feedback", None), "weave_ref")
    if (
        not feedback_id
        or str(record_id) != feedback_id
        or record_payload != payload
        or _record_value(record, "feedback_type") != feedback_type
        or (expected_ref and _record_value(record, "weave_ref") != expected_ref)
    ):
        raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_READBACK_INVALID")
    return {
        "weave_feedback_id": feedback_id,
        "trace_id": agents_trace_id,
        "weave_call_id": weave_call_id,
        "wandb_run_id": wandb_run_id,
        "verified": True,
    }


@dataclass(slots=True)
class _NativeCall:
    call_id: str
    operation: str
    association: AssistantRunAssociation
    evidence_authorization: AssistantEvidenceAuthorization | None
    steps: list[str] = field(default_factory=list)
    result: NativeSingleTurnAgentsResult | None = None
    finished_output_sha256: str | None = None
    aborted: bool = False


class NativeAgentsAssistantBridge(AssistantProvider, NestedTraceSink):
    """Expose a native Agents invocation through the legacy assistant seams.

    ``start``/``step``/``finish`` only prove the deterministic local pipeline
    ran in the required order.  The sole externally persisted trace is the one
    returned by :class:`NativeSingleTurnAgentsExecutor` after exact Weave
    readback.
    """

    def __init__(
        self,
        *,
        executor_factory: ExecutorFactory,
        target: WandbTarget,
        credentials: InMemoryCredentialSession,
        scope_verifier: ScopeVerifier,
        feedback_root: Path,
    ) -> None:
        self.executor_factory = executor_factory
        self.target = target
        self.credentials = credentials
        self.scope_verifier = scope_verifier
        self.feedback_root = feedback_root.resolve()
        self.feedback_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.feedback_root.is_symlink():
            raise ValueError("native assistant feedback root must not be a symlink")
        self._lock = threading.RLock()
        self._thread_call: dict[int, str] = {}
        self._calls: dict[str, _NativeCall] = {}
        self._trace_to_call: dict[str, str] = {}

    def _current(self) -> _NativeCall:
        with self._lock:
            call_id = self._thread_call.get(threading.get_ident())
            call = self._calls.get(call_id or "")
        if call is None:
            raise RuntimeError("NATIVE_AGENT_BRIDGE_CALL_NOT_STARTED")
        return call

    def start(self, *, call_id: str, operation: str, inputs: dict[str, Any]) -> None:
        association = AssistantRunAssociation.model_validate(inputs.get("wandb_association"))
        evidence_payload = inputs.get("evidence_audit")
        evidence_authorization = (
            AssistantEvidenceAuthorization.model_validate(evidence_payload)
            if evidence_payload is not None
            else None
        )
        if operation != "assistant.request":
            raise RuntimeError("NATIVE_AGENT_BRIDGE_OPERATION_INVALID")
        with self._lock:
            if call_id in self._calls or threading.get_ident() in self._thread_call:
                raise RuntimeError("NATIVE_AGENT_BRIDGE_CALL_ALREADY_STARTED")
            requested = str(inputs.get("requested_operation") or "")
            if requested not in {"evidence.explain", "evidence_intake.extract"}:
                raise RuntimeError("NATIVE_AGENT_BRIDGE_OPERATION_INVALID")
            self._calls[call_id] = _NativeCall(
                call_id=call_id,
                operation=requested,
                association=association,
                evidence_authorization=evidence_authorization,
            )
            self._thread_call[threading.get_ident()] = call_id

    def step(
        self,
        *,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        del inputs, output
        call = self._current()
        if call.call_id != call_id or name not in APPROVED_TRACE_STEPS:
            raise RuntimeError("NATIVE_AGENT_BRIDGE_STEP_INVALID")
        if name in call.steps:
            raise RuntimeError("NATIVE_AGENT_BRIDGE_STEP_DUPLICATED")
        expected = APPROVED_TRACE_STEPS[len(call.steps)]
        if name != expected:
            raise RuntimeError("NATIVE_AGENT_BRIDGE_STEP_ORDER_INVALID")
        call.steps.append(name)

    def complete(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> ProviderResponse:
        del system_prompt, response_schema
        call = self._current()
        if operation != call.operation or call.result is not None:
            raise RuntimeError("NATIVE_AGENT_BRIDGE_PROVIDER_STATE_INVALID")
        if call.steps != list(APPROVED_TRACE_STEPS[:4]):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_LOCAL_PREFLIGHT_INCOMPLETE")
        # The authorizer checks this at the beginning of the deterministic
        # pipeline. Recheck immediately before the only network/model action so
        # scope drift cannot occur between evidence resolution and invocation.
        self.scope_verifier()
        association_scope = call.association.analysis_id or call.association.run_subject_id
        evidence_authorization = call.evidence_authorization
        if call.operation == "evidence.explain":
            if evidence_authorization is None:
                raise RuntimeError("NATIVE_AGENT_BRIDGE_AUTHORIZATION_REQUIRED")
            authorization_sha256 = canonical_sha256(evidence_authorization.model_dump(mode="json"))
            model_revision_sha256 = evidence_authorization.model_revision_id_sha256
            scope_evidence_sha256 = evidence_authorization.scope_evidence_sha256
            native_operation = "EVIDENCE_EXPLAIN"
            intent = str(context.get("question_intent") or user_prompt)
        else:
            if evidence_authorization is not None:
                raise RuntimeError("NATIVE_AGENT_BRIDGE_INTAKE_AUTHORIZATION_INVALID")
            authorization_sha256 = canonical_sha256(
                {
                    "project_id": call.association.project_id,
                    "operation": call.operation,
                    "association_id": call.association.association_id,
                    "context_sha256": canonical_sha256(context),
                }
            )
            model_revision_sha256 = hashlib.sha256(
                call.association.run_subject_id.encode("utf-8")
            ).hexdigest()
            scope_evidence_sha256 = canonical_sha256(
                self.target.scope_evidence.model_dump(mode="json")
            )
            native_operation = "EVIDENCE_INTAKE"
            intent = user_prompt
        authorization = NativeAgentAuthorization(
            project_id_sha256=hashlib.sha256(
                call.association.project_id.encode("utf-8")
            ).hexdigest(),
            analysis_id_sha256=hashlib.sha256(association_scope.encode("utf-8")).hexdigest(),
            model_revision_id_sha256=model_revision_sha256,
            authorization_sha256=authorization_sha256,
            scope_evidence_sha256=scope_evidence_sha256,
        )
        executor = self.executor_factory(call.association, authorization)
        call.result = executor.execute(
            operation=native_operation,
            intent=intent,
            context=context,
        )
        if (
            call.result.trace.association_id != call.association.association_id
            or call.result.trace.wandb_run_id != call.association.wandb_run_id
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_TRACE_ASSOCIATION_INVALID")
        usage: dict[str, int] = {"elapsed_ms": call.result.elapsed_ms}
        if call.result.input_tokens is not None:
            usage["prompt_tokens"] = call.result.input_tokens
        if call.result.output_tokens is not None:
            usage["completion_tokens"] = call.result.output_tokens
        if call.result.input_tokens is not None and call.result.output_tokens is not None:
            usage["total_tokens"] = call.result.input_tokens + call.result.output_tokens
        return ProviderResponse(
            content=json.dumps(
                call.result.output,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            provider_call_id=f"sha256:{call.result.provider_call_id_sha256}",
            model=call.result.provider_model,
            usage=usage,
        )

    def finish(self, *, call_id: str, output: dict[str, Any]) -> None:
        call = self._current()
        if (
            call.call_id != call_id
            or call.result is None
            or call.steps != list(APPROVED_TRACE_STEPS)
            or call.aborted
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_FINISH_INVALID")
        call.finished_output_sha256 = canonical_sha256(output)

    def flush(self, *, call_id: str) -> str:
        call = self._current()
        if (
            call.call_id != call_id
            or call.result is None
            or not call.finished_output_sha256
            or call.aborted
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_FLUSH_INVALID")
        trace_id = call.result.trace.agents_trace_id
        with self._lock:
            self._trace_to_call[trace_id] = call_id
            self._thread_call.pop(threading.get_ident(), None)
        return trace_id

    def readback(self, *, trace_id: str) -> TraceReadback:
        with self._lock:
            call_id = self._trace_to_call.get(trace_id)
            call = self._calls.get(call_id or "")
        if (
            call is None
            or call.result is None
            or call.result.trace.agents_trace_id != trace_id
            or not call.result.trace.readback_verified
            or not call.finished_output_sha256
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_READBACK_INVALID")
        return TraceReadback(
            trace_id=trace_id,
            trace_url=call.result.trace.trace_url,
            call_id=call.call_id,
            completed=True,
            steps=list(call.steps),
            association=call.association,
        )

    def abort(self, *, call_id: str, error_code: str) -> None:
        del error_code
        with self._lock:
            call = self._calls.get(call_id)
            if call is not None:
                call.aborted = True
            self._thread_call.pop(threading.get_ident(), None)

    def aborted_readback(self, *, call_id: str) -> dict[str, Any]:
        with self._lock:
            call = self._calls.get(call_id)
        if (
            call is None
            or call.result is None
            or not call.aborted
            or not call.result.trace.readback_verified
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_REJECTION_TRACE_UNAVAILABLE")
        return {
            "call_id": call_id,
            "completed": True,
            "persisted": True,
            "trace_readback_verified": True,
            "answer_returned": False,
            "trace_id": call.result.trace.agents_trace_id,
            "weave_call_id": call.result.trace.weave_call_id,
            "trace_url": call.result.trace.trace_url,
            "association": call.association.model_dump(mode="json"),
        }

    def record_feedback(
        self,
        *,
        call_id: str,
        trace_id: str,
        rating: str,
        comment: str,
    ) -> dict[str, Any]:
        with self._lock:
            bound_call_id = self._trace_to_call.get(trace_id)
            call = self._calls.get(bound_call_id or "")
        if (
            bound_call_id != call_id
            or call is None
            or call.result is None
            or not call.result.trace.readback_verified
        ):
            raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_TRACE_INVALID")
        return attach_verified_weave_feedback(
            target=self.target,
            credentials=self.credentials,
            scope_verifier=self.scope_verifier,
            weave_call_id=call.result.trace.weave_call_id,
            agents_trace_id=trace_id,
            wandb_run_id=call.result.trace.wandb_run_id,
            rating=rating,
            comment=comment,
            feedback_type="ecocorridor.practitioner_usefulness",
            client_feedback_id=call_id,
        )


__all__ = ["NativeAgentsAssistantBridge", "attach_verified_weave_feedback"]
