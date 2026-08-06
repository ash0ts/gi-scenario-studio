"""Generic isolated single-turn Agents SDK executor for bounded assistants.

This adapter is intentionally smaller than the Review Brief agent: it exposes
no tools and permits exactly one model turn.  It exists so the Evidence and
Evidence Intake assistants can appear natively in Weave's Agents view without
using manual nested Ops as their primary trace.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator

from .assistants import (
    AssistantProviderConfiguration,
    AssistantRunAssociation,
    ProviderCredentialSession,
)
from .contracts import ContractModel, canonical_sha256
from .observability import InMemoryCredentialSession, WandbTarget
from .review_brief_agent_runtime import (
    REVIEW_BRIEF_MAX_CONTEXT_BYTES,
    REVIEW_BRIEF_MAX_FRAME_BYTES,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentCredentialLease,
    ReviewBriefAgentProvider,
    ReviewBriefAgentRuntimeError,
    ReviewBriefAgentTraceControl,
    ReviewBriefWeaveTarget,
    _communicate_with_deadline,
    _json_bytes,
    _read_frame,
    _write_frame,
)

NATIVE_AGENTS_PROTOCOL = "ecocorridor-native-single-turn-agents/v1"
NATIVE_AGENTS_MAX_TURNS = 1
NATIVE_AGENTS_DEADLINE_SECONDS = 90.0
NATIVE_EVIDENCE_EXPLAIN_PROMPT = (
    "Return only the supplied closed evidence intent and an ordered list of authorized "
    "evidence aliases with their exact closed explanation kinds. Return no prose, values, "
    "units, limitations, recommendations, locations, or additional fields. The local "
    "deterministic renderer creates all visible language and numbers."
)
NATIVE_EVIDENCE_INTAKE_PROMPT = (
    "Extract unconfirmed draft fields only from the authorized document pages. Every field "
    "must have exact document, page, character-span, and quote evidence. Return only the "
    "strict schema. Never derive resistance values, intervention effects, ecological "
    "outcomes, approvals, or recommendations."
)
NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT = (
    "Produce the strict evaluation ReviewBriefAnswer using only the supplied synthetic "
    "authorized case context. Do not approve, attest, rank, design, or modify a portfolio; "
    "do not infer ecological values or locations. Cite only evidence IDs present in that "
    "context. The authorized_answer_contract is the complete closed output ledger produced "
    "by deterministic local evidence resolution: reproduce it exactly, without adding, "
    "removing, paraphrasing, rounding, or reordering any item. Preserve every required "
    "limitation and abstain when its fail-closed disposition requires it. This provider "
    "output is evaluation evidence and is not rendered as a professional recommendation."
)
NativeAssistantOperation = Literal[
    "EVIDENCE_EXPLAIN",
    "EVIDENCE_INTAKE",
    "REVIEW_BRIEF_EVALUATION",
]

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class NativeAgentAuthorization(ContractModel):
    project_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_revision_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NativeSingleTurnPayload(ContractModel):
    operation: NativeAssistantOperation
    intent: str = Field(min_length=1, max_length=512)
    context: dict[str, Any]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_context(self) -> NativeSingleTurnPayload:
        if canonical_sha256(self.context) != self.context_sha256:
            raise ValueError("native agent context digest is inconsistent")
        if self.operation == "EVIDENCE_EXPLAIN" and self.intent not in {
            "EXPLAIN_SELECTED_EVIDENCE",
            "EXPLAIN_DISPOSITION",
            "EXPLAIN_CONTROLLING_ASSUMPTION",
            "EXPLAIN_BASELINE_CHANGE",
            "TRACE_EVIDENCE",
            "NEXT_FEASIBILITY_REVIEW",
        }:
            raise ValueError("native evidence intent must use the closed vocabulary")
        if self.operation == "REVIEW_BRIEF_EVALUATION" and self.intent != "PREPARE_REVIEW_BRIEF":
            raise ValueError("review-brief evaluation intent must use the fixed task")
        return self

    @classmethod
    def create(
        cls,
        *,
        operation: NativeAssistantOperation,
        intent: str,
        context: dict[str, Any],
    ) -> NativeSingleTurnPayload:
        return cls(
            operation=operation,
            intent=intent,
            context=context,
            context_sha256=canonical_sha256(context),
        )


class NativeAgentsTraceReceipt(ContractModel):
    agents_trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    weave_call_id: str
    association_id: str
    wandb_run_id: str
    trace_url: str
    flushed: Literal[True] = True
    readback_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_identifiers(self) -> NativeAgentsTraceReceipt:
        if any(
            not _OPAQUE.fullmatch(value)
            for value in (self.weave_call_id, self.association_id, self.wandb_run_id)
        ):
            raise ValueError("native agent trace receipt contains a non-opaque identifier")
        parsed = urlsplit(self.trace_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("native agent trace URL must be undecorated HTTP(S)")
        return self


class NativeSingleTurnAgentsResult(ContractModel):
    operation: NativeAssistantOperation
    output: dict[str, Any]
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_call_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_call_identity_kind: Literal[
        "PROVIDER_RESPONSE_ID", "PROVIDER_REQUEST_ID", "WEAVE_GENERATION_CALL_ID"
    ]
    provider_model: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: int = Field(ge=0)
    trace: NativeAgentsTraceReceipt

    @model_validator(mode="after")
    def validate_output(self) -> NativeSingleTurnAgentsResult:
        if canonical_sha256(self.output) != self.output_sha256:
            raise ValueError("native agent output digest is inconsistent")
        return self


class NativeSingleTurnChildRequest(ContractModel):
    protocol_version: Literal[NATIVE_AGENTS_PROTOCOL] = NATIVE_AGENTS_PROTOCOL
    payload: NativeSingleTurnPayload
    provider: ReviewBriefAgentProvider
    weave_target: ReviewBriefWeaveTarget
    trace: ReviewBriefAgentTraceControl
    authorization: NativeAgentAuthorization
    provider_api_key: SecretStr
    wandb_api_key: SecretStr

    def ipc_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"provider_api_key", "wandb_api_key"})
        payload["provider_api_key"] = self.provider_api_key.get_secret_value()
        payload["wandb_api_key"] = self.wandb_api_key.get_secret_value()
        return payload


class NativeSingleTurnChildResponse(ContractModel):
    protocol_version: Literal[NATIVE_AGENTS_PROTOCOL] = NATIVE_AGENTS_PROTOCOL
    ok: bool
    result: NativeSingleTurnAgentsResult | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,127}$")

    @model_validator(mode="after")
    def validate_disposition(self) -> NativeSingleTurnChildResponse:
        if self.ok != (self.result is not None and self.error_code is None):
            raise ValueError("native agent child response disposition is inconsistent")
        return self


class NativeSingleTurnSubprocess(Protocol):
    def invoke(
        self,
        *,
        request: NativeSingleTurnChildRequest,
        deadline_seconds: float,
    ) -> NativeSingleTurnChildResponse: ...


class PopenNativeSingleTurnSubprocess:
    def __init__(
        self,
        *,
        scratch_root: Path,
        python_executable: str | None = None,
        module: str = "ecocorridor_core.native_agents_process",
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.scratch_root = scratch_root.resolve()
        self.python_executable = python_executable or sys.executable
        self.module = module
        self.popen_factory = popen_factory
        if not self.scratch_root.is_dir() or self.scratch_root.is_symlink():
            raise ValueError("native agent scratch root must be an existing non-symlink directory")

    def command(self) -> list[str]:
        if bool(getattr(sys, "frozen", False)):
            return [self.python_executable, "--native-assistant-agent-subprocess"]
        return [self.python_executable, "-I", "-m", self.module]

    @staticmethod
    def _environment(runtime: Path) -> dict[str, str]:
        allowed = {
            "PATH",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "LANG",
            "LC_ALL",
        }
        result = {key: value for key, value in os.environ.items() if key in allowed}
        result.update(
            {
                "HOME": str(runtime),
                "TMPDIR": str(runtime),
                "PYTHONUNBUFFERED": "1",
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
            }
        )
        return result

    def invoke(
        self,
        *,
        request: NativeSingleTurnChildRequest,
        deadline_seconds: float,
    ) -> NativeSingleTurnChildResponse:
        if not 5 <= deadline_seconds <= 180:
            raise ValueError("native agent deadline is invalid")
        frame = bytearray(_json_bytes(request.ipc_payload()))
        if len(frame) > REVIEW_BRIEF_MAX_FRAME_BYTES:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_FRAME_TOO_LARGE")
        with tempfile.TemporaryDirectory(
            prefix="native-single-turn-agent-", dir=self.scratch_root
        ) as runtime_value:
            runtime = Path(runtime_value)
            process = self.popen_factory(
                self.command(),
                cwd=runtime,
                env=self._environment(runtime),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            try:
                if process.stdin is None:
                    raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_STDIN_UNAVAILABLE")
                _write_frame(process.stdin, frame)
                process.stdin.close()
                process.stdin = None
                stdout, stderr = _communicate_with_deadline(
                    process,
                    deadline_seconds=deadline_seconds,
                    deadline_error_code="NATIVE_AGENT_DEADLINE_EXCEEDED",
                )
            finally:
                frame[:] = b"\0" * len(frame)
        if process.returncode != 0:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_PROCESS_FAILED")
        if len(stderr) > REVIEW_BRIEF_MAX_FRAME_BYTES:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_STDERR_EXCEEDED")
        response = NativeSingleTurnChildResponse.model_validate_json(
            _read_frame(_BytesReader(stdout))
        )
        if not response.ok:
            raise ReviewBriefAgentRuntimeError(response.error_code or "NATIVE_AGENT_FAILED")
        return response


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0

    def readline(self, limit: int = -1) -> bytes:
        end = self.value.find(b"\n", self.offset)
        end = len(self.value) if end < 0 else end + 1
        if limit >= 0:
            end = min(end, self.offset + limit)
        result = self.value[self.offset : end]
        self.offset = end
        return result

    def read(self, size: int = -1) -> bytes:
        end = len(self.value) if size < 0 else min(len(self.value), self.offset + size)
        result = self.value[self.offset : end]
        self.offset = end
        return result


CredentialLeaseFactory = Callable[[], Any]


class NativeSingleTurnAgentsExecutor:
    """Testable worker seam for one no-tool, one-turn native Agents invocation."""

    def __init__(
        self,
        *,
        provider: ReviewBriefAgentProvider,
        weave_target: ReviewBriefWeaveTarget,
        association: AssistantRunAssociation,
        authorization: NativeAgentAuthorization,
        capability_receipt: ReviewBriefAgentCapabilityReceipt,
        credential_lease: CredentialLeaseFactory,
        subprocess_client: NativeSingleTurnSubprocess,
        deadline_seconds: float = NATIVE_AGENTS_DEADLINE_SECONDS,
    ) -> None:
        self.provider = provider
        self.weave_target = weave_target
        self.association = association
        self.authorization = authorization
        self.capability_receipt = capability_receipt
        self.credential_lease = credential_lease
        self.subprocess_client = subprocess_client
        self.deadline_seconds = deadline_seconds
        if _sha(association.project_id) != authorization.project_id_sha256:
            raise ValueError("native agent project association mismatch")
        association_scope_id = association.analysis_id or association.run_subject_id
        if _sha(association_scope_id) != authorization.analysis_id_sha256:
            raise ValueError("native agent analysis/subject association mismatch")
        if _sha(association.wandb_external_project_id) != (weave_target.external_project_id_sha256):
            raise ValueError("native agent W&B association mismatch")
        if capability_receipt.provider_mode != provider.mode:
            raise ValueError("native agent provider preflight mode mismatch")
        if not _trace_url_matches(capability_receipt.trace_url, weave_target):
            raise ValueError("native agent provider preflight target mismatch")
        if not (
            capability_receipt.tool_calling_verified
            and capability_receipt.strict_structured_output_verified
            and capability_receipt.trace_readback_verified
        ):
            raise ValueError("native agent provider preflight is incomplete")

    @contextmanager
    def _credentials(self) -> Iterator[ReviewBriefAgentCredentialLease]:
        candidate = self.credential_lease()
        if hasattr(candidate, "__enter__") and hasattr(candidate, "__exit__"):
            with candidate as value:
                if not isinstance(value, ReviewBriefAgentCredentialLease):
                    raise TypeError("native agent credential lease is invalid")
                yield value
        else:
            if not isinstance(candidate, ReviewBriefAgentCredentialLease):
                raise TypeError("native agent credential lease is invalid")
            try:
                yield candidate
            finally:
                candidate.provider_api_key = ""
                candidate.wandb_api_key = ""

    def execute(
        self,
        *,
        operation: NativeAssistantOperation,
        intent: str,
        context: dict[str, Any],
    ) -> NativeSingleTurnAgentsResult:
        payload = NativeSingleTurnPayload.create(
            operation=operation,
            intent=intent,
            context=context,
        )
        if len(_json_bytes(payload.model_dump(mode="json"))) > REVIEW_BRIEF_MAX_CONTEXT_BYTES:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_CONTEXT_TOO_LARGE")
        trace = _trace_control(
            association=self.association,
            payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
        )
        with self._credentials() as credentials:
            child = NativeSingleTurnChildRequest(
                payload=payload,
                provider=self.provider,
                weave_target=self.weave_target,
                trace=trace,
                authorization=self.authorization,
                provider_api_key=credentials.provider_api_key,
                wandb_api_key=credentials.wandb_api_key,
            )
            response = self.subprocess_client.invoke(
                request=child,
                deadline_seconds=self.deadline_seconds,
            )
        result = response.result
        if result is None:
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_RESULT_MISSING")
        if (
            result.operation != operation
            or result.provider_model != self.provider.model
            or result.trace.agents_trace_id != trace.trace_id
            or result.trace.association_id != trace.association_id
            or result.trace.wandb_run_id != trace.wandb_run_id
            or not _trace_url_matches(result.trace.trace_url, self.weave_target)
        ):
            raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_RESULT_RECEIPT_INVALID")
        return result


def native_single_turn_executor_from_sessions(
    *,
    provider_configuration: AssistantProviderConfiguration,
    provider_mode: Literal["RESPONSES", "CHAT_COMPLETIONS"],
    target: WandbTarget,
    association: AssistantRunAssociation,
    authorization: NativeAgentAuthorization,
    capability_receipt: ReviewBriefAgentCapabilityReceipt,
    provider_credentials: ProviderCredentialSession,
    observability_credentials: InMemoryCredentialSession,
    scratch_root: Path,
    deadline_seconds: float = NATIVE_AGENTS_DEADLINE_SECONDS,
    subprocess_client: NativeSingleTurnSubprocess | None = None,
) -> NativeSingleTurnAgentsExecutor:
    """Build the no-tool executor without persisting or preloading either credential."""

    @contextmanager
    def credentials() -> Iterator[ReviewBriefAgentCredentialLease]:
        with provider_credentials.authorization_header() as authorization_header:
            if not authorization_header.startswith("Bearer "):
                raise ReviewBriefAgentRuntimeError("NATIVE_AGENT_PROVIDER_CREDENTIAL_INVALID")
            provider_key = authorization_header.removeprefix("Bearer ")
            with observability_credentials.environment():
                wandb_key = os.environ.get("WANDB_API_KEY", "")
                lease = ReviewBriefAgentCredentialLease(
                    provider_api_key=provider_key,
                    wandb_api_key=wandb_key,
                )
                try:
                    yield lease
                finally:
                    lease.provider_api_key = ""
                    lease.wandb_api_key = ""
                    provider_key = ""
                    wandb_key = ""

    return NativeSingleTurnAgentsExecutor(
        provider=ReviewBriefAgentProvider(
            base_url=provider_configuration.base_url,
            model=provider_configuration.model,
            mode=provider_mode,
            timeout_seconds=provider_configuration.timeout_seconds,
        ),
        weave_target=ReviewBriefWeaveTarget(
            entity=target.entity,
            project=target.project,
            api_base_url=target.api_base_url,
            web_base_url=target.web_base_url,
            external_project_id_sha256=_sha(target.external_project_id),
        ),
        association=association,
        authorization=authorization,
        capability_receipt=capability_receipt,
        credential_lease=credentials,
        subprocess_client=(
            subprocess_client
            if subprocess_client is not None
            else PopenNativeSingleTurnSubprocess(scratch_root=scratch_root)
        ),
        deadline_seconds=deadline_seconds,
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trace_control(
    *, association: AssistantRunAssociation, payload_sha256: str
) -> ReviewBriefAgentTraceControl:
    nonce = secrets.token_hex(32)
    digest = _sha(
        f"{NATIVE_AGENTS_PROTOCOL}\0{association.association_id}\0"
        f"{association.wandb_run_id}\0{payload_sha256}\0{nonce}"
    )
    return ReviewBriefAgentTraceControl(
        trace_id="trace_" + digest[:32],
        association_id=association.association_id,
        association_id_sha256=_sha(association.association_id),
        wandb_run_id=association.wandb_run_id,
        wandb_run_id_sha256=_sha(association.wandb_run_id),
    )


def _trace_url_matches(url: str, target: ReviewBriefWeaveTarget) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(target.web_base_url)
    parts = parsed.path.split("/")
    return (
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and target.entity in parts
        and target.project in parts
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def safe_native_child_error_code(exc: BaseException) -> str:
    """Map child exceptions to a finite code without leaking input or credentials."""

    message = str(exc)
    if isinstance(exc, ReviewBriefAgentRuntimeError) and re.fullmatch(
        r"NATIVE_AGENT_[A-Z0-9_]{2,112}", message
    ):
        return message
    name = type(exc).__name__.upper().replace("_", "")
    if "TIMEOUT" in name:
        return "NATIVE_AGENT_DEADLINE_EXCEEDED"
    if "GUARDRAIL" in name or "MAXTURNS" in name:
        return "NATIVE_AGENT_BOUNDARY_REJECTED"
    provider_errors = {
        "BADREQUESTERROR": "NATIVE_AGENT_PROVIDER_REQUEST_REJECTED",
        "AUTHENTICATIONERROR": "NATIVE_AGENT_PROVIDER_AUTHENTICATION_FAILED",
        "PERMISSIONDENIEDERROR": "NATIVE_AGENT_PROVIDER_ACCESS_DENIED",
        "NOTFOUNDERROR": "NATIVE_AGENT_PROVIDER_MODEL_NOT_FOUND",
        "RATELIMITERROR": "NATIVE_AGENT_PROVIDER_RATE_LIMITED",
        "APICONNECTIONERROR": "NATIVE_AGENT_PROVIDER_UNAVAILABLE",
        "INTERNALSERVERERROR": "NATIVE_AGENT_PROVIDER_FAILED",
        "APISTATUSERROR": "NATIVE_AGENT_PROVIDER_FAILED",
        "MODELBEHAVIORERROR": "NATIVE_AGENT_PROVIDER_OUTPUT_INVALID",
    }
    if name in provider_errors:
        return provider_errors[name]
    return "NATIVE_AGENT_INVOCATION_FAILED"


__all__ = [
    "NATIVE_AGENTS_DEADLINE_SECONDS",
    "NATIVE_AGENTS_MAX_TURNS",
    "NATIVE_AGENTS_PROTOCOL",
    "NATIVE_EVIDENCE_EXPLAIN_PROMPT",
    "NATIVE_EVIDENCE_INTAKE_PROMPT",
    "NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT",
    "NativeAgentAuthorization",
    "NativeAgentsTraceReceipt",
    "NativeAssistantOperation",
    "NativeSingleTurnAgentsExecutor",
    "NativeSingleTurnAgentsResult",
    "NativeSingleTurnChildRequest",
    "NativeSingleTurnChildResponse",
    "NativeSingleTurnPayload",
    "PopenNativeSingleTurnSubprocess",
    "native_single_turn_executor_from_sessions",
    "safe_native_child_error_code",
]
