"""Fail-closed, provider-backed assistants for already-authorized evidence.

Both assistants are deliberately outside the scientific calculation path. They
cannot write model state, approve alternatives, or manufacture ecological
values. A response is returned only after deterministic validation and durable
trace readback succeed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import ssl
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from scenario_platform.observability import redact_sensitive

from .claim_safety import assert_strict_ecological_claim
from .contracts import ConnectivityEvidenceClaim, ContractModel
from .metrics import (
    CONNECTIVITY_LIMITATION,
    CURRENT_MAP_LIMITATION,
    validate_evidence_claim,
)

APPROVED_TRACE_STEPS = (
    "authorize_project",
    "minimize_sensitive_context",
    "resolve_local_evidence",
    "construct_context",
    "call_provider",
    "extract_numeric_claims",
    "validate_evidence_references",
    "validate_units_and_limitations",
    "format_response",
    "record_feedback",
)
INTAKE_LIMITATION = (
    "AI-extracted draft — requires human confirmation against the cited source text."
)
DIRECT_RESULT_QUALIFICATION = "Direct Circuitscape result under documented assumptions."

_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore (?:all |any )?(?:previous|prior|system|developer) instructions?\b",
        r"\b(?:reveal|print|return|expose) (?:the )?(?:system prompt|api key|secret|token)\b",
        r"\b(?:bypass|disable|evade) (?:the )?(?:guardrails?|safety|validation|trace)\b",
        r"\b(?:fabricate|invent|make up) (?:an? )?(?:evidence|citation|result|value)\b",
        r"\bdo not (?:cite|validate|trace)\b",
        r"\bdeveloper message\b",
    )
)
_UNAUTHORIZED_ACTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:i|we) (?:have |will )?(?:approve|attest|modify|edit|change)\b",
        (
            r"\b(?:approve|attest|modify|edit|change)(?:d|s|ing)?\s+(?:the\s+)?"
            r"(?:model|intervention|portfolio|project|assumption|resistance)\b"
        ),
        (
            r"\b(?:select|choose|declare)\s+(?:this|the|a)\s+"
            r"(?:preferred|final|winning|best)\s+(?:portfolio|project|option|corridor)\b"
        ),
        r"\b(?:you should|must) (?:build|approve|select) (?:this|the)\b",
    )
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?![A-Za-z0-9_])")
_COORDINATE_PAIR = re.compile(
    r"(?<![A-Za-z0-9_])-?\d{1,3}\.\d{4,}\s*[, ]\s*-?\d{1,3}\.\d{4,}(?![A-Za-z0-9_])"
)
_PERSONAL_DATA_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
)
_OPAQUE_DOCUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class AssistantSafetyError(RuntimeError):
    """A fail-closed assistant boundary rejected or could not trace a response."""


class ProviderCredentialSession:
    """Process-memory provider credential supplied by the native keychain host."""

    def __init__(self) -> None:
        self._credential: bytearray | None = None
        self._lock = threading.RLock()

    def configure(self, api_key: str) -> None:
        if not 16 <= len(api_key) <= 512 or any(character.isspace() for character in api_key):
            raise ValueError("provider credential has an invalid shape")
        with self._lock:
            self.clear()
            self._credential = bytearray(api_key.encode("utf-8"))

    @property
    def configured(self) -> bool:
        return self._credential is not None

    @contextmanager
    def authorization_header(self) -> Iterator[str]:
        with self._lock:
            if self._credential is None:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_CREDENTIAL_UNAVAILABLE")
            temporary = self._credential.decode("utf-8")
            try:
                yield f"Bearer {temporary}"
            finally:
                temporary = ""

    def appears_in(self, value: bytes) -> bool:
        """Return whether provider bytes contain the configured credential."""

        with self._lock:
            return self._credential is not None and value.find(self._credential) >= 0

    def clear(self) -> None:
        if self._credential is not None:
            self._credential[:] = b"\0" * len(self._credential)
            self._credential = None


class ProviderResponse(ContractModel):
    content: str
    provider_call_id: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_response_metadata(self) -> ProviderResponse:
        if not self.content.strip() or not self.provider_call_id.strip() or not self.model.strip():
            raise ValueError("provider response content, call ID, and model are required")
        if len(self.provider_call_id) > 512 or len(self.model) > 512:
            raise ValueError("provider response identifiers are too long")
        return self


class AssistantProvider(Protocol):
    def complete(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> ProviderResponse: ...


class AssistantProviderConfiguration(ContractModel):
    """Explicit, non-secret configuration for one provider-backed session."""

    base_url: str
    model: str
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validated_provider_base_url(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", normalized)
            or normalized != value
        ):
            raise ValueError("provider model must be an explicit, whitespace-free identifier")
        return normalized


def _validated_provider_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not is_loopback)
        or len(normalized) > 2048
    ):
        raise ValueError("provider base URL must be HTTPS or loopback HTTP without credentials")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("provider base URL must not include the chat-completions resource path")
    return normalized


class OpenAICompatibleProvider:
    """Small OpenAI-compatible JSON provider with no SDK-global credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        credentials: ProviderCredentialSession,
        timeout_seconds: float = 60.0,
        max_request_bytes: int = 2 * 1024 * 1024,
        max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        configuration = AssistantProviderConfiguration(
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        parsed = urlsplit(configuration.base_url)
        self._parsed = parsed
        self.model = configuration.model
        self.credentials = credentials
        self.timeout_seconds = configuration.timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes

    def complete(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> ProviderResponse:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "operation": operation,
                                "question": user_prompt,
                                "authorized_context": context,
                                "required_response_schema": response_schema,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > self.max_request_bytes:
            raise AssistantSafetyError("ASSISTANT_PROVIDER_REQUEST_TOO_LARGE")
        path = self._parsed.path.rstrip("/") + "/chat/completions"
        connection_type = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self._parsed.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        connection = connection_type(self._parsed.hostname, self._parsed.port, **kwargs)
        try:
            with self.credentials.authorization_header() as authorization:
                connection.request(
                    "POST",
                    path,
                    body=body,
                    headers={
                        "Authorization": authorization,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            response = connection.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_RESPONSE_TOO_LARGE")
            if self.credentials.appears_in(raw):
                raise AssistantSafetyError("ASSISTANT_PROVIDER_CREDENTIAL_ECHO_REJECTED")
            if response.status < 200 or response.status >= 300:
                raise AssistantSafetyError(f"ASSISTANT_PROVIDER_HTTP_{response.status}")
            try:
                payload = json.loads(raw.decode("utf-8"))
                content = payload["choices"][0]["message"]["content"]
                provider_call_id = payload["id"]
                returned_model = payload["model"]
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_RESPONSE_INVALID") from exc
            if (
                not isinstance(provider_call_id, str)
                or not provider_call_id.strip()
                or provider_call_id != provider_call_id.strip()
                or not isinstance(returned_model, str)
                or not returned_model.strip()
                or returned_model != returned_model.strip()
            ):
                raise AssistantSafetyError("ASSISTANT_PROVIDER_RESPONSE_METADATA_INVALID")
            if returned_model != self.model:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_MODEL_MISMATCH")
            usage = {
                str(key): int(value)
                for key, value in payload.get("usage", {}).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            return ProviderResponse(
                content=str(content),
                provider_call_id=provider_call_id,
                model=returned_model,
                usage=usage,
            )
        except AssistantSafetyError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise AssistantSafetyError("ASSISTANT_PROVIDER_UNAVAILABLE") from exc
        finally:
            connection.close()


class AssistantRunAssociation(ContractModel):
    """Immutable identity shared by a W&B run and its Weave trace."""

    association_id: str
    project_id: str
    wandb_external_project_id: str
    wandb_run_id: str
    wandb_run_kind: str
    run_subject_id: str
    analysis_id: str | None = None

    @staticmethod
    def _identity_id(identity: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"association_{digest[:24]}"

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        wandb_external_project_id: str,
        wandb_run_id: str,
        wandb_run_kind: str,
        run_subject_id: str,
        analysis_id: str | None = None,
    ) -> AssistantRunAssociation:
        identity = {
            "project_id": project_id,
            "wandb_external_project_id": wandb_external_project_id,
            "wandb_run_id": wandb_run_id,
            "wandb_run_kind": wandb_run_kind,
            "run_subject_id": run_subject_id,
            "analysis_id": analysis_id,
        }
        return cls(association_id=cls._identity_id(identity), **identity)

    @model_validator(mode="after")
    def validate_identity(self) -> AssistantRunAssociation:
        expected = self._identity_id(
            {
                "project_id": self.project_id,
                "wandb_external_project_id": self.wandb_external_project_id,
                "wandb_run_id": self.wandb_run_id,
                "wandb_run_kind": self.wandb_run_kind,
                "run_subject_id": self.run_subject_id,
                "analysis_id": self.analysis_id,
            }
        )
        if (
            not all(
                item.strip()
                for item in (
                    self.project_id,
                    self.wandb_external_project_id,
                    self.wandb_run_id,
                    self.wandb_run_kind,
                    self.run_subject_id,
                )
            )
            or self.association_id != expected
            or (self.wandb_run_kind in {"connectivity_analysis", "assistant_evaluation"})
            != bool(self.analysis_id)
        ):
            raise ValueError("assistant W&B run association is inconsistent")
        return self


class TraceReadback(ContractModel):
    trace_id: str
    trace_url: str | None = None
    call_id: str
    completed: bool
    steps: list[str]
    association: AssistantRunAssociation


class NestedTraceSink(Protocol):
    def start(self, *, call_id: str, operation: str, inputs: dict[str, Any]) -> None: ...

    def step(
        self,
        *,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None: ...

    def finish(self, *, call_id: str, output: dict[str, Any]) -> None: ...

    def flush(self, *, call_id: str) -> str: ...

    def readback(self, *, trace_id: str) -> TraceReadback: ...

    def abort(self, *, call_id: str, error_code: str) -> None: ...

    def aborted_readback(self, *, call_id: str) -> dict[str, Any]: ...


class AssistantRejectionAuditSink(Protocol):
    """Project-local sink for provider-completed calls rejected by trace gates."""

    def record_rejected(
        self,
        *,
        call_id: str,
        project_id: str,
        operation: str,
        error_code: str,
        provider_call_id: str,
        provider_model: str,
        association: AssistantRunAssociation,
        provider_abort_trace: dict[str, Any] | None,
    ) -> dict[str, Any]: ...


class ProjectAuthorizer(Protocol):
    def authorize(self, *, project_id: str, operation: str) -> bool: ...


class EvidenceResolver(Protocol):
    def resolve(
        self, *, project_id: str, evidence_ids: Sequence[str]
    ) -> list[ConnectivityEvidenceClaim]: ...


class SensitiveContextPolicy(ContractModel):
    prohibited_terms: list[str] = Field(default_factory=list)
    exact_location_authorized: bool = False

    @field_validator("prohibited_terms")
    @classmethod
    def validate_terms(cls, value: list[str]) -> list[str]:
        if any(not term.strip() for term in value):
            raise ValueError("sensitive terms may not be empty")
        if len({term.casefold() for term in value}) != len(value):
            raise ValueError("sensitive terms must be unique")
        if len(value) > 256:
            raise ValueError("sensitive terms may contain at most 256 entries")
        if any(len(term) > 256 for term in value):
            raise ValueError("each sensitive term may contain at most 256 characters")
        if sum(len(term.encode("utf-8")) for term in value) > 16_384:
            raise ValueError("sensitive terms exceed the 16 KiB request limit")
        return value


class EvidenceQuestionIntent(str, Enum):
    """Finite provider instruction vocabulary; raw user questions never leave the worker."""

    EXPLAIN_SELECTED_EVIDENCE = "EXPLAIN_SELECTED_EVIDENCE"
    EXPLAIN_DISPOSITION = "EXPLAIN_DISPOSITION"
    EXPLAIN_CONTROLLING_ASSUMPTION = "EXPLAIN_CONTROLLING_ASSUMPTION"
    EXPLAIN_BASELINE_CHANGE = "EXPLAIN_BASELINE_CHANGE"
    TRACE_EVIDENCE = "TRACE_EVIDENCE"
    NEXT_FEASIBILITY_REVIEW = "NEXT_FEASIBILITY_REVIEW"


class AssistantEvidenceAuthorization(ContractModel):
    """Opaque, destination-bound authorization decision for one evidence selection."""

    decision_id: str
    project_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_revision_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: Literal["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]
    external_project_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_authorizations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_asset_count: int = Field(ge=0)
    generalized_derivative_count: int = Field(ge=0)
    context_schema_version: Literal["ecocorridor-assistant-context/v2"] = (
        "ecocorridor-assistant-context/v2"
    )

    @classmethod
    def create(cls, **payload: Any) -> AssistantEvidenceAuthorization:
        identity = {
            **payload,
            "context_schema_version": "ecocorridor-assistant-context/v2",
        }
        decision_id = (
            "evidence_auth_"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
        )
        return cls(decision_id=decision_id, **identity)

    @model_validator(mode="after")
    def validate_decision_id(self) -> AssistantEvidenceAuthorization:
        identity = self.model_dump(mode="json", exclude={"decision_id"})
        expected = (
            "evidence_auth_"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
        )
        if self.decision_id != expected:
            raise ValueError("assistant evidence authorization identity is inconsistent")
        if self.generalized_derivative_count > self.authorized_asset_count:
            raise ValueError("generalized evidence count exceeds authorized asset count")
        return self


_SAFE_CONTEXT_UNITS = re.compile(r"[A-Za-z%][A-Za-z0-9%/_.^-]{0,31}\Z")
_SAFE_CONTEXT_ENUMS = frozenset({"PASSES", "FAILS", "INCOMPLETE", "SURVEY_FIRST"})


class AuthorizedEvidenceClaimContext(ContractModel):
    """Minimal provider-visible evidence record with no project-authored names."""

    evidence_alias: str = Field(pattern=r"^E[1-9][0-9]{0,3}$")
    claim_kind: Literal[
        "DIRECT_PAIR_METRIC",
        "DIRECT_MAP_DIAGNOSTIC",
        "DIRECT_QA",
        "COST",
        "FEASIBILITY",
        "AGGREGATE_RULE",
        "PORTFOLIO_DISPOSITION",
    ]
    value: float | bool | Literal["PASSES", "FAILS", "INCOMPLETE", "SURVEY_FIRST"]
    units: str
    direct_result: bool
    limitations: list[str]

    @model_validator(mode="after")
    def validate_safe_context(self) -> AuthorizedEvidenceClaimContext:
        if isinstance(self.value, str) and self.value not in _SAFE_CONTEXT_ENUMS:
            raise ValueError("provider context string values must use a closed enum")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("provider context numbers must be finite")
        if not _SAFE_CONTEXT_UNITS.fullmatch(self.units):
            raise ValueError("provider context units are not in the safe token grammar")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("provider context limitations must be unique")
        if any(
            item not in {CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION}
            for item in self.limitations
        ):
            raise ValueError("provider context may include only fixed product limitations")
        return self


class AuthorizedEvidenceItem(ContractModel):
    """Local-only binding between an opaque alias and an immutable evidence claim."""

    evidence_id: str
    claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context: AuthorizedEvidenceClaimContext


@dataclass(frozen=True, slots=True)
class AuthorizedEvidenceSelection:
    """Trusted worker output. Only ``provider_context`` crosses the AI boundary."""

    authorization: AssistantEvidenceAuthorization
    items: tuple[AuthorizedEvidenceItem, ...]

    def assert_integrity(self) -> None:
        if not self.items:
            raise AssistantSafetyError("ASSISTANT_AUTHORIZED_EVIDENCE_EMPTY")
        aliases = [item.context.evidence_alias for item in self.items]
        evidence_ids = [item.evidence_id for item in self.items]
        if len(set(aliases)) != len(aliases) or len(set(evidence_ids)) != len(evidence_ids):
            raise AssistantSafetyError("ASSISTANT_AUTHORIZED_EVIDENCE_DUPLICATED")
        digest = hashlib.sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in self.items],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if digest != self.authorization.selected_evidence_sha256:
            raise AssistantSafetyError("ASSISTANT_AUTHORIZED_EVIDENCE_INTEGRITY_FAILED")

    def provider_context(self, intent: EvidenceQuestionIntent) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "context_schema_version": self.authorization.context_schema_version,
            "authorization_decision_id": self.authorization.decision_id,
            "question_intent": intent.value,
            "claims": [item.context.model_dump(mode="json") for item in self.items],
        }


class NumericEvidenceReference(ContractModel):
    evidence_id: str
    value: float
    units: str

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("numeric assistant claims must be finite")
        return value


class _ProviderEvidenceAnswer(ContractModel):
    answer: str
    evidence_ids: list[str] = Field(min_length=1)
    numeric_claims: list[NumericEvidenceReference] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> _ProviderEvidenceAnswer:
        if not self.answer.strip() or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("assistant answer and unique evidence IDs are required")
        return self


class AssistantAnswer(ContractModel):
    answer_id: str
    call_id: str
    project_id: str
    text: str
    evidence_ids: list[str]
    numeric_claims: list[NumericEvidenceReference]
    limitations: list[str]
    provider_call_id: str
    provider_model: str
    trace_id: str
    trace_url: str | None = None
    association: AssistantRunAssociation
    evidence_authorization: AssistantEvidenceAuthorization
    deterministic_validation: Literal["PASSED"] = "PASSED"
    validator_version: Literal["ecocorridor-assistant-validator-v1"] = (
        "ecocorridor-assistant-validator-v1"
    )
    trace_readback_verified: Literal[True] = True
    read_only: Literal[True] = True


class DocumentPage(ContractModel):
    document_id: str
    page_number: int = Field(gt=0)
    text: str

    @model_validator(mode="after")
    def validate_page(self) -> DocumentPage:
        if not _OPAQUE_DOCUMENT_ID.fullmatch(self.document_id) or not self.text:
            raise ValueError("document page identity and text are required")
        return self


class SourceSpan(ContractModel):
    document_id: str
    page_number: int = Field(gt=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str

    @model_validator(mode="after")
    def validate_span(self) -> SourceSpan:
        if (
            not _OPAQUE_DOCUMENT_ID.fullmatch(self.document_id)
            or self.end <= self.start
            or not self.quote
        ):
            raise ValueError("source span must be non-empty and ordered")
        return self


IntakeFieldName = Literal["purpose", "constraint", "safeguard", "cost_limit", "assumption"]


class _ProviderIntakeField(ContractModel):
    field_name: IntakeFieldName
    value: str
    citations: list[SourceSpan] = Field(min_length=1)


class _ProviderIntakeAnswer(ContractModel):
    draft_fields: list[_ProviderIntakeField] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)


class IntakeDraftField(ContractModel):
    field_id: str
    field_name: IntakeFieldName
    value: str
    citations: list[SourceSpan]
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_confirmation(self) -> IntakeDraftField:
        if self.confirmed != bool(self.confirmed_by and self.confirmed_at):
            raise ValueError("confirmed intake fields require reviewer and timestamp")
        return self


class IntakeDraft(ContractModel):
    draft_id: str
    call_id: str
    project_id: str
    fields: list[IntakeDraftField] = Field(min_length=1)
    limitations: list[str]
    provider_call_id: str
    provider_model: str
    trace_id: str
    trace_url: str | None = None
    association: AssistantRunAssociation
    deterministic_validation: Literal["PASSED"] = "PASSED"
    validator_version: Literal["ecocorridor-intake-validator-v1"] = (
        "ecocorridor-intake-validator-v1"
    )
    trace_readback_verified: Literal[True] = True
    requires_human_confirmation: Literal[True] = True
    applied_to_model: Literal[False] = False

    def confirm(self, field_ids: Sequence[str], *, reviewer: str) -> IntakeDraft:
        if not reviewer.strip():
            raise ValueError("human reviewer is required")
        selected = set(field_ids)
        known = {field.field_id for field in self.fields}
        if not selected or not selected.issubset(known):
            raise ValueError("confirmation must reference known draft fields")
        if any(field.confirmed and field.field_id in selected for field in self.fields):
            raise ValueError("confirmed intake fields are immutable")
        confirmed_at = datetime.now(UTC)
        payload = self.model_dump(mode="python")
        payload["fields"] = [
            (
                {
                    **field.model_dump(mode="python"),
                    "confirmed": True,
                    "confirmed_by": reviewer.strip(),
                    "confirmed_at": confirmed_at,
                }
                if field.field_id in selected
                else field.model_dump(mode="python")
            )
            for field in self.fields
        ]
        return IntakeDraft.model_validate(payload)

    def confirmed_model_payload(self) -> list[dict[str, Any]]:
        if not all(field.confirmed for field in self.fields):
            raise AssistantSafetyError("INTAKE_DRAFT_REQUIRES_HUMAN_CONFIRMATION")
        return [
            {
                "field_name": field.field_name,
                "value": field.value,
                "citations": [citation.model_dump(mode="json") for citation in field.citations],
                "confirmed_by": field.confirmed_by,
                "confirmed_at": field.confirmed_at.isoformat() if field.confirmed_at else None,
            }
            for field in self.fields
        ]


def _strict_json_object(value: str) -> dict[str, Any]:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON value: {constant}")

    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=object_hook,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise AssistantSafetyError("ASSISTANT_PROVIDER_JSON_INVALID") from exc
    if not isinstance(parsed, dict):
        raise AssistantSafetyError("ASSISTANT_PROVIDER_JSON_INVALID")
    return parsed


def _assert_no_prompt_injection(value: str) -> None:
    if any(pattern.search(value) for pattern in _INJECTION_PATTERNS):
        raise AssistantSafetyError("ASSISTANT_PROMPT_INJECTION_REJECTED")


def _assert_safe_output(value: str) -> None:
    try:
        assert_strict_ecological_claim(value)
    except ValueError as exc:
        raise AssistantSafetyError("ASSISTANT_PROHIBITED_ECOLOGICAL_CLAIM") from exc
    if any(pattern.search(value) for pattern in _UNAUTHORIZED_ACTION_PATTERNS):
        raise AssistantSafetyError("ASSISTANT_UNAUTHORIZED_ACTION")


def _replace_sensitive_terms(value: str, policy: SensitiveContextPolicy) -> str:
    def generalized(match: re.Match[str]) -> str:
        marker = "[GENERALIZED_LOCATION]"
        length = len(match.group(0))
        if length < len(marker):
            return "X" * length
        return marker + "_" * (length - len(marker))

    result = value
    if not policy.exact_location_authorized:
        for term in sorted(policy.prohibited_terms, key=len, reverse=True):
            result = re.sub(re.escape(term), generalized, result, flags=re.IGNORECASE)
        result = _COORDINATE_PAIR.sub(generalized, result)
    for pattern in _PERSONAL_DATA_PATTERNS:
        result = pattern.sub(generalized, result)
    return result


def _minimize(value: Any, policy: SensitiveContextPolicy) -> Any:
    redacted = redact_sensitive(value)
    if isinstance(redacted, dict):
        return {key: _minimize(item, policy) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [_minimize(item, policy) for item in redacted]
    if isinstance(redacted, str):
        return _replace_sensitive_terms(redacted, policy)
    return redacted


def _assert_no_sensitive_location(value: str, policy: SensitiveContextPolicy) -> None:
    if any(pattern.search(value) for pattern in _PERSONAL_DATA_PATTERNS):
        raise AssistantSafetyError("ASSISTANT_SENSITIVE_CONTEXT_LEAK")
    if policy.exact_location_authorized:
        return
    if _COORDINATE_PAIR.search(value) or any(
        term.casefold() in value.casefold() for term in policy.prohibited_terms
    ):
        raise AssistantSafetyError("ASSISTANT_SENSITIVE_LOCATION_LEAK")


def validate_feedback_comment(
    value: str,
    policy: SensitiveContextPolicy | None = None,
) -> str:
    """Reject feedback that would persist or replicate sensitive context."""

    active_policy = policy or SensitiveContextPolicy()
    if redact_sensitive(value) != value:
        raise AssistantSafetyError("ASSISTANT_FEEDBACK_SENSITIVE_CONTENT")
    _assert_no_prompt_injection(value)
    _assert_no_sensitive_location(value, active_policy)
    return value


def _number_tokens(value: str, *, evidence_ids: Sequence[str] = ()) -> list[Decimal]:
    scrubbed = value
    for evidence_id in evidence_ids:
        scrubbed = scrubbed.replace(evidence_id, "")
    result: list[Decimal] = []
    for match in _NUMBER.finditer(scrubbed):
        try:
            result.append(Decimal(match.group(0)))
        except InvalidOperation as exc:  # pragma: no cover - regex accepts decimal grammar
            raise AssistantSafetyError("ASSISTANT_NUMERIC_CLAIM_INVALID") from exc
    return result


def _answer_mentions_numeric_claim(answer: str, claim: NumericEvidenceReference) -> bool:
    """Require each structured value to appear in prose with its declared units."""

    expected = Decimal(str(claim.value))
    unit = claim.units.strip()
    if not unit:
        return False
    for match in _NUMBER.finditer(answer):
        try:
            observed = Decimal(match.group(0))
        except InvalidOperation:  # pragma: no cover - regex accepts decimal grammar
            continue
        if observed != expected:
            continue
        suffix = answer[match.end() :]
        if unit.casefold() in {"percent", "%"}:
            if re.match(r"\s*(?:%|percent\b)", suffix, re.IGNORECASE):
                return True
        elif re.match(rf"\s*{re.escape(unit)}(?![A-Za-z0-9_])", suffix, re.IGNORECASE):
            return True
    return False


def _classify_evidence_intent(question: str) -> EvidenceQuestionIntent:
    """Classify locally so user-authored text is never sent to Weave or the provider."""

    normalized = question.casefold()
    if any(token in normalized for token in ("trace", "source", "where did", "evidence")):
        return EvidenceQuestionIntent.TRACE_EVIDENCE
    if any(token in normalized for token in ("next", "investigate", "field review", "feasibility")):
        return EvidenceQuestionIntent.NEXT_FEASIBILITY_REVIEW
    if any(token in normalized for token in ("assumption", "controls", "controlling", "worst")):
        return EvidenceQuestionIntent.EXPLAIN_CONTROLLING_ASSUMPTION
    if any(token in normalized for token in ("baseline", "changed", "change", "delta")):
        return EvidenceQuestionIntent.EXPLAIN_BASELINE_CHANGE
    if any(token in normalized for token in ("pass", "fail", "disposition", "shown")):
        return EvidenceQuestionIntent.EXPLAIN_DISPOSITION
    return EvidenceQuestionIntent.EXPLAIN_SELECTED_EVIDENCE


class _TracedAssistant:
    def __init__(
        self,
        *,
        provider: AssistantProvider,
        trace_sink: NestedTraceSink,
        authorizer: ProjectAuthorizer,
        rejection_audit_sink: AssistantRejectionAuditSink | None = None,
    ) -> None:
        self.provider = provider
        self.trace_sink = trace_sink
        self.authorizer = authorizer
        self.rejection_audit_sink = rejection_audit_sink

    def _start(
        self,
        call_id: str,
        operation: str,
        project_id: str,
        association: AssistantRunAssociation,
        *,
        evidence_authorization: AssistantEvidenceAuthorization | None = None,
    ) -> None:
        if association.project_id != project_id:
            raise AssistantSafetyError("ASSISTANT_RUN_ASSOCIATION_PROJECT_MISMATCH")
        try:
            self.trace_sink.start(
                call_id=call_id,
                operation="assistant.request",
                inputs={
                    "project_id_sha256": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
                    "requested_operation": operation,
                    "wandb_association": association.model_dump(mode="json"),
                    "evidence_authorization": (
                        evidence_authorization.model_dump(mode="json")
                        if evidence_authorization is not None
                        else None
                    ),
                },
            )
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_TRACE_UNAVAILABLE") from exc

    def _step(
        self,
        call_id: str,
        name: str,
        *,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        if name not in APPROVED_TRACE_STEPS:
            raise AssistantSafetyError("ASSISTANT_TRACE_STEP_NOT_APPROVED")
        try:
            self.trace_sink.step(
                call_id=call_id,
                name=name,
                inputs=redact_sensitive(inputs),
                output=redact_sensitive(output),
            )
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_TRACE_UNAVAILABLE") from exc

    def _authorize(
        self,
        call_id: str,
        project_id: str,
        operation: str,
        *,
        evidence_authorization: AssistantEvidenceAuthorization | None = None,
    ) -> None:
        try:
            authorized = self.authorizer.authorize(project_id=project_id, operation=operation)
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_PROJECT_AUTHORIZATION_FAILED") from exc
        self._step(
            call_id,
            "authorize_project",
            inputs={
                "project_id_sha256": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
                "operation": operation,
            },
            output={
                "authorized": authorized,
                "evidence_authorization_decision_id": (
                    evidence_authorization.decision_id
                    if evidence_authorization is not None
                    else None
                ),
            },
        )
        if not authorized:
            raise AssistantSafetyError("ASSISTANT_PROJECT_NOT_AUTHORIZED")

    def _finish_trace(
        self,
        call_id: str,
        *,
        output: dict[str, Any],
        association: AssistantRunAssociation,
    ) -> TraceReadback:
        try:
            self.trace_sink.finish(call_id=call_id, output=redact_sensitive(output))
            trace_id = self.trace_sink.flush(call_id=call_id)
            if not trace_id:
                raise AssistantSafetyError("ASSISTANT_TRACE_FLUSH_FAILED")
        except AssistantSafetyError:
            raise
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_TRACE_FLUSH_FAILED") from exc
        try:
            readback = TraceReadback.model_validate(self.trace_sink.readback(trace_id=trace_id))
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_TRACE_READBACK_INVALID") from exc
        if (
            readback.trace_id != trace_id
            or readback.call_id != call_id
            or not readback.completed
            or readback.steps != list(APPROVED_TRACE_STEPS)
            or readback.association != association
        ):
            raise AssistantSafetyError("ASSISTANT_TRACE_READBACK_INVALID")
        return readback

    def _abort(self, call_id: str, error: BaseException) -> dict[str, Any] | None:
        code = (
            str(error) if isinstance(error, AssistantSafetyError) else "ASSISTANT_INTERNAL_FAILURE"
        )
        try:
            self.trace_sink.abort(call_id=call_id, error_code=code)
            return self.trace_sink.aborted_readback(call_id=call_id)
        except Exception:  # noqa: BLE001 - abort must never mask the original safety failure
            return None

    def _record_post_provider_rejection(
        self,
        *,
        call_id: str,
        project_id: str,
        operation: str,
        error: BaseException,
        provider_response: ProviderResponse,
        association: AssistantRunAssociation,
        provider_abort_trace: dict[str, Any] | None,
    ) -> None:
        """Durably record that provider output was withheld by a local safety gate."""

        if self.rejection_audit_sink is None:
            return
        error_code = (
            str(error) if isinstance(error, AssistantSafetyError) else "ASSISTANT_INTERNAL_FAILURE"
        )
        try:
            receipt = self.rejection_audit_sink.record_rejected(
                call_id=call_id,
                project_id=project_id,
                operation=operation,
                error_code=error_code,
                provider_call_id=provider_response.provider_call_id,
                provider_model=provider_response.model,
                association=association,
                provider_abort_trace=provider_abort_trace,
            )
        except Exception as exc:
            raise AssistantSafetyError("ASSISTANT_LOCAL_REJECTION_AUDIT_FAILED") from exc
        if (
            receipt.get("local_audit_verified") is not True
            or receipt.get("disposition") != "REJECTED"
            or receipt.get("answer_returned") is not False
            or receipt.get("completed_trace_readback_verified") is not False
        ):
            raise AssistantSafetyError("ASSISTANT_LOCAL_REJECTION_AUDIT_FAILED")


class EvidenceAssistant(_TracedAssistant):
    """Read-only explanation of selected, direct-run-backed evidence claims."""

    def __init__(
        self,
        *,
        provider: AssistantProvider,
        trace_sink: NestedTraceSink,
        authorizer: ProjectAuthorizer,
        resolver: EvidenceResolver,
        rejection_audit_sink: AssistantRejectionAuditSink | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            trace_sink=trace_sink,
            authorizer=authorizer,
            rejection_audit_sink=rejection_audit_sink,
        )
        self.resolver = resolver

    def explain(
        self,
        *,
        call_id: str,
        project_id: str,
        question: str,
        evidence_ids: Sequence[str],
        association: AssistantRunAssociation,
        authorized_selection: AuthorizedEvidenceSelection,
        sensitive_policy: SensitiveContextPolicy | None = None,
    ) -> AssistantAnswer:
        policy = sensitive_policy or SensitiveContextPolicy()
        authorized_selection.assert_integrity()
        authorization = authorized_selection.authorization
        if (
            authorization.project_id_sha256
            != hashlib.sha256(project_id.encode("utf-8")).hexdigest()
            or association.analysis_id is None
            or authorization.analysis_id_sha256
            != hashlib.sha256(association.analysis_id.encode("utf-8")).hexdigest()
            or authorization.external_project_id_sha256
            != hashlib.sha256(association.wandb_external_project_id.encode("utf-8")).hexdigest()
        ):
            raise AssistantSafetyError("ASSISTANT_EVIDENCE_AUTHORIZATION_ASSOCIATION_MISMATCH")
        self._start(
            call_id,
            "evidence.explain",
            project_id,
            association,
            evidence_authorization=authorization,
        )
        provider_response: ProviderResponse | None = None
        try:
            self._authorize(
                call_id,
                project_id,
                "evidence.explain",
                evidence_authorization=authorization,
            )
            if (
                not question.strip()
                or not evidence_ids
                or len(set(evidence_ids)) != len(evidence_ids)
            ):
                raise AssistantSafetyError("ASSISTANT_REQUEST_INVALID")
            _assert_no_prompt_injection(question)
            selected_by_id = {item.evidence_id: item for item in authorized_selection.items}
            if set(selected_by_id) != set(evidence_ids):
                raise AssistantSafetyError("ASSISTANT_AUTHORIZED_EVIDENCE_SELECTION_MISMATCH")
            intent = _classify_evidence_intent(question)
            self._step(
                call_id,
                "minimize_sensitive_context",
                inputs={"question_bytes": len(question.encode("utf-8"))},
                output={
                    "question_intent": intent.value,
                    "raw_question_replicated": False,
                    "authorization_decision_id": authorization.decision_id,
                },
            )
            claims = self.resolver.resolve(project_id=project_id, evidence_ids=evidence_ids)
            by_id = {claim.claim_id: claim for claim in claims}
            if set(by_id) != set(evidence_ids) or len(by_id) != len(claims):
                raise AssistantSafetyError("ASSISTANT_EVIDENCE_RESOLUTION_INCOMPLETE")
            for claim in claims:
                try:
                    validate_evidence_claim(claim)
                except ValueError as exc:
                    raise AssistantSafetyError("ASSISTANT_LOCAL_EVIDENCE_INVALID") from exc
                expected = selected_by_id[claim.claim_id]
                observed_sha256 = hashlib.sha256(
                    json.dumps(
                        claim.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if observed_sha256 != expected.claim_sha256:
                    raise AssistantSafetyError("ASSISTANT_AUTHORIZED_EVIDENCE_CHANGED")
            self._step(
                call_id,
                "resolve_local_evidence",
                inputs={
                    "authorization_decision_id": authorization.decision_id,
                    "selected_count": len(evidence_ids),
                },
                output={
                    "resolved_aliases": sorted(
                        item.context.evidence_alias for item in authorized_selection.items
                    )
                },
            )
            minimized_context = authorized_selection.provider_context(intent)
            self._step(
                call_id,
                "construct_context",
                inputs={
                    "resolved_count": len(claims),
                    "context_schema_version": authorization.context_schema_version,
                },
                output=minimized_context,
            )
            provider_response = self.provider.complete(
                operation="evidence.explain",
                system_prompt=(
                    "Explain only the authorized direct Circuitscape evidence. Return strict JSON. "
                    "Every structured numeric claim must appear in the answer beside its declared "
                    "units. Do not choose, approve, modify, or invent anything."
                ),
                user_prompt=intent.value,
                context=minimized_context,
                response_schema=_ProviderEvidenceAnswer.model_json_schema(),
            )
            self._step(
                call_id,
                "call_provider",
                inputs={
                    "context_evidence_aliases": sorted(
                        item.context.evidence_alias for item in authorized_selection.items
                    ),
                    "authorization_decision_id": authorization.decision_id,
                },
                output={
                    "provider_call_id": provider_response.provider_call_id,
                    "model": provider_response.model,
                    "usage": provider_response.usage,
                },
            )
            try:
                proposed = _ProviderEvidenceAnswer.model_validate(
                    _strict_json_object(provider_response.content)
                )
            except (ValueError, TypeError) as exc:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_SCHEMA_INVALID") from exc
            self._step(
                call_id,
                "extract_numeric_claims",
                inputs={"answer_bytes": len(proposed.answer.encode("utf-8"))},
                output={
                    "numeric_claims": [
                        item.model_dump(mode="json") for item in proposed.numeric_claims
                    ]
                },
            )
            claims_by_alias = {
                item.context.evidence_alias: by_id[item.evidence_id]
                for item in authorized_selection.items
            }
            self._validate_evidence_answer(proposed, claims_by_alias, policy)
            self._step(
                call_id,
                "validate_evidence_references",
                inputs={"claimed_ids": proposed.evidence_ids},
                output={"valid": True},
            )
            self._step(
                call_id,
                "validate_units_and_limitations",
                inputs={"limitation_count": len(proposed.limitations)},
                output={"valid": True},
            )
            formatted = (
                proposed.answer.strip()
                + "\n\nLimitations:\n"
                + "\n".join(f"- {limitation}" for limitation in proposed.limitations)
            )
            self._step(
                call_id,
                "format_response",
                inputs={"answer_bytes": len(proposed.answer.encode("utf-8"))},
                output={"formatted_bytes": len(formatted.encode("utf-8"))},
            )
            self._step(
                call_id,
                "record_feedback",
                inputs={},
                output={"feedback_state": "PENDING_USER_FEEDBACK"},
            )
            answer_id = (
                "answer_"
                + hashlib.sha256(
                    f"{call_id}:{provider_response.provider_call_id}".encode()
                ).hexdigest()[:24]
            )
            trace = self._finish_trace(
                call_id,
                output={
                    "answer_id": answer_id,
                    "evidence_aliases": proposed.evidence_ids,
                    "association_id": association.association_id,
                    "authorization_decision_id": authorization.decision_id,
                },
                association=association,
            )
            original_id_by_alias = {
                item.context.evidence_alias: item.evidence_id for item in authorized_selection.items
            }
            answer_evidence_ids = [original_id_by_alias[item] for item in proposed.evidence_ids]
            answer_numeric_claims = [
                NumericEvidenceReference(
                    evidence_id=original_id_by_alias[item.evidence_id],
                    value=item.value,
                    units=item.units,
                )
                for item in proposed.numeric_claims
            ]
            return AssistantAnswer(
                answer_id=answer_id,
                call_id=call_id,
                project_id=project_id,
                text=formatted,
                evidence_ids=answer_evidence_ids,
                numeric_claims=answer_numeric_claims,
                limitations=proposed.limitations,
                provider_call_id=provider_response.provider_call_id,
                provider_model=provider_response.model,
                trace_id=trace.trace_id,
                trace_url=trace.trace_url,
                association=trace.association,
                evidence_authorization=authorization,
            )
        except BaseException as exc:
            provider_abort_trace = self._abort(call_id, exc)
            if provider_response is not None:
                self._record_post_provider_rejection(
                    call_id=call_id,
                    project_id=project_id,
                    operation="evidence.explain",
                    error=exc,
                    provider_response=provider_response,
                    association=association,
                    provider_abort_trace=provider_abort_trace,
                )
            if isinstance(exc, AssistantSafetyError):
                raise
            raise AssistantSafetyError("ASSISTANT_INTERNAL_FAILURE") from exc

    @staticmethod
    def _validate_evidence_answer(
        proposed: _ProviderEvidenceAnswer,
        evidence: dict[str, ConnectivityEvidenceClaim],
        policy: SensitiveContextPolicy,
    ) -> None:
        if len(set(proposed.evidence_ids)) != len(proposed.evidence_ids) or not set(
            proposed.evidence_ids
        ).issubset(evidence):
            raise AssistantSafetyError("ASSISTANT_FABRICATED_EVIDENCE")
        if len({item.evidence_id for item in proposed.numeric_claims}) != len(
            proposed.numeric_claims
        ):
            raise AssistantSafetyError("ASSISTANT_NUMERIC_EVIDENCE_DUPLICATED")
        _assert_safe_output(proposed.answer)
        _assert_no_prompt_injection(proposed.answer)
        _assert_no_sensitive_location(proposed.answer, policy)
        for item in proposed.numeric_claims:
            claim = evidence.get(item.evidence_id)
            if claim is None:
                raise AssistantSafetyError("ASSISTANT_FABRICATED_EVIDENCE")
            if isinstance(claim.value, bool) or not isinstance(claim.value, (int, float)):
                raise AssistantSafetyError("ASSISTANT_NUMERIC_EVIDENCE_TYPE_MISMATCH")
            if Decimal(str(item.value)) != Decimal(str(claim.value)):
                raise AssistantSafetyError("ASSISTANT_NUMERIC_VALUE_MISMATCH")
            if item.units != claim.units:
                raise AssistantSafetyError("ASSISTANT_UNIT_MISMATCH")
            if not _answer_mentions_numeric_claim(proposed.answer, item):
                raise AssistantSafetyError("ASSISTANT_NUMERIC_UNIT_RENDERING_MISMATCH")
        declared_values = {Decimal(str(item.value)) for item in proposed.numeric_claims}
        if any(
            token not in declared_values
            for token in _number_tokens(proposed.answer, evidence_ids=proposed.evidence_ids)
        ):
            raise AssistantSafetyError("ASSISTANT_UNDECLARED_NUMERIC_CLAIM")
        required = {CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION}
        if not required.issubset(proposed.limitations):
            raise AssistantSafetyError("ASSISTANT_REQUIRED_LIMITATION_MISSING")
        for limitation in proposed.limitations:
            _assert_safe_output(limitation)
            _assert_no_prompt_injection(limitation)
            _assert_no_sensitive_location(limitation, policy)
        if proposed.numeric_claims and DIRECT_RESULT_QUALIFICATION not in proposed.answer:
            raise AssistantSafetyError("ASSISTANT_DIRECT_RESULT_QUALIFICATION_MISSING")


class EvidenceIntakeAssistant(_TracedAssistant):
    """Extract unconfirmed draft fields with exact document page/span anchors."""

    def extract(
        self,
        *,
        call_id: str,
        project_id: str,
        instruction: str,
        pages: Sequence[DocumentPage],
        association: AssistantRunAssociation,
        sensitive_policy: SensitiveContextPolicy | None = None,
    ) -> IntakeDraft:
        policy = sensitive_policy or SensitiveContextPolicy()
        self._start(call_id, "evidence_intake.extract", project_id, association)
        provider_response: ProviderResponse | None = None
        try:
            self._authorize(call_id, project_id, "evidence_intake.extract")
            if not instruction.strip() or not pages:
                raise AssistantSafetyError("ASSISTANT_REQUEST_INVALID")
            _assert_no_prompt_injection(instruction)
            for page in pages:
                if redact_sensitive(page.document_id) != page.document_id:
                    raise AssistantSafetyError("ASSISTANT_DOCUMENT_ID_NOT_OPAQUE")
                _assert_no_sensitive_location(page.document_id, policy)
                _assert_no_prompt_injection(page.text)
            page_keys = [(page.document_id, page.page_number) for page in pages]
            if len(set(page_keys)) != len(page_keys):
                raise AssistantSafetyError("ASSISTANT_DOCUMENT_PAGE_DUPLICATED")
            minimized_instruction = _minimize(instruction, policy)
            minimized_pages = [
                DocumentPage(
                    document_id=page.document_id,
                    page_number=page.page_number,
                    text=str(_minimize(page.text, policy)),
                )
                for page in pages
            ]
            self._step(
                call_id,
                "minimize_sensitive_context",
                inputs={"page_count": len(pages)},
                output={"page_count": len(minimized_pages)},
            )
            self._step(
                call_id,
                "resolve_local_evidence",
                inputs={"document_ids": sorted({page.document_id for page in pages})},
                output={"resolved_page_count": len(minimized_pages)},
            )
            context = {
                "pages": [page.model_dump(mode="json") for page in minimized_pages],
                "allowed_fields": [
                    "purpose",
                    "constraint",
                    "safeguard",
                    "cost_limit",
                    "assumption",
                ],
            }
            _assert_no_prompt_injection(json.dumps(context, sort_keys=True, separators=(",", ":")))
            self._step(
                call_id,
                "construct_context",
                inputs={"page_count": len(minimized_pages)},
                output=context,
            )
            provider_response = self.provider.complete(
                operation="evidence_intake.extract",
                system_prompt=(
                    "Extract unconfirmed draft fields only. Every field requires exact page, "
                    "character-span, and quote evidence. Return strict JSON; never derive "
                    "resistance values or intervention effects."
                ),
                user_prompt=str(minimized_instruction),
                context=context,
                response_schema=_ProviderIntakeAnswer.model_json_schema(),
            )
            self._step(
                call_id,
                "call_provider",
                inputs={"page_count": len(minimized_pages)},
                output={
                    "provider_call_id": provider_response.provider_call_id,
                    "model": provider_response.model,
                    "usage": provider_response.usage,
                },
            )
            try:
                proposed = _ProviderIntakeAnswer.model_validate(
                    _strict_json_object(provider_response.content)
                )
            except (ValueError, TypeError) as exc:
                raise AssistantSafetyError("ASSISTANT_PROVIDER_SCHEMA_INVALID") from exc
            numeric_values = [
                str(item) for field in proposed.draft_fields for item in _number_tokens(field.value)
            ]
            self._step(
                call_id,
                "extract_numeric_claims",
                inputs={"field_count": len(proposed.draft_fields)},
                output={"numeric_token_count": len(numeric_values)},
            )
            self._validate_intake(proposed, minimized_pages, policy)
            self._step(
                call_id,
                "validate_evidence_references",
                inputs={"field_count": len(proposed.draft_fields)},
                output={"valid": True},
            )
            self._step(
                call_id,
                "validate_units_and_limitations",
                inputs={"limitation_count": len(proposed.limitations)},
                output={"valid": True},
            )
            fields = [
                IntakeDraftField(
                    field_id="draftfield_"
                    + hashlib.sha256(
                        f"{call_id}:{index}:{field.field_name}:{field.value}".encode()
                    ).hexdigest()[:20],
                    field_name=field.field_name,
                    value=field.value,
                    citations=field.citations,
                )
                for index, field in enumerate(proposed.draft_fields)
            ]
            self._step(
                call_id,
                "format_response",
                inputs={"field_count": len(fields)},
                output={"confirmed_field_count": 0},
            )
            self._step(
                call_id,
                "record_feedback",
                inputs={},
                output={"feedback_state": "PENDING_USER_FEEDBACK"},
            )
            draft_id = (
                "intake_"
                + hashlib.sha256(
                    f"{call_id}:{provider_response.provider_call_id}".encode()
                ).hexdigest()[:24]
            )
            trace = self._finish_trace(
                call_id,
                output={
                    "draft_id": draft_id,
                    "confirmed_field_count": 0,
                    "association_id": association.association_id,
                },
                association=association,
            )
            return IntakeDraft(
                draft_id=draft_id,
                call_id=call_id,
                project_id=project_id,
                fields=fields,
                limitations=proposed.limitations,
                provider_call_id=provider_response.provider_call_id,
                provider_model=provider_response.model,
                trace_id=trace.trace_id,
                trace_url=trace.trace_url,
                association=trace.association,
            )
        except BaseException as exc:
            provider_abort_trace = self._abort(call_id, exc)
            if provider_response is not None:
                self._record_post_provider_rejection(
                    call_id=call_id,
                    project_id=project_id,
                    operation="evidence_intake.extract",
                    error=exc,
                    provider_response=provider_response,
                    association=association,
                    provider_abort_trace=provider_abort_trace,
                )
            if isinstance(exc, AssistantSafetyError):
                raise
            raise AssistantSafetyError("ASSISTANT_INTERNAL_FAILURE") from exc

    @staticmethod
    def _validate_intake(
        proposed: _ProviderIntakeAnswer,
        pages: Sequence[DocumentPage],
        policy: SensitiveContextPolicy,
    ) -> None:
        page_index = {(page.document_id, page.page_number): page.text for page in pages}
        seen_fields: set[tuple[str, str]] = set()
        for field in proposed.draft_fields:
            if not field.value.strip() or (field.field_name, field.value) in seen_fields:
                raise AssistantSafetyError("ASSISTANT_INTAKE_FIELD_INVALID")
            seen_fields.add((field.field_name, field.value))
            quotes: list[str] = []
            for citation in field.citations:
                text = page_index.get((citation.document_id, citation.page_number))
                if text is None or citation.end > len(text):
                    raise AssistantSafetyError("ASSISTANT_FABRICATED_CITATION")
                if text[citation.start : citation.end] != citation.quote:
                    raise AssistantSafetyError("ASSISTANT_CITATION_SPAN_MISMATCH")
                quotes.append(citation.quote)
            quoted_numbers = {item for quote in quotes for item in _number_tokens(quote)}
            if any(item not in quoted_numbers for item in _number_tokens(field.value)):
                raise AssistantSafetyError("ASSISTANT_INTAKE_NUMERIC_VALUE_UNCITED")
            _assert_safe_output(field.value)
            _assert_no_prompt_injection(field.value)
            _assert_no_sensitive_location(field.value, policy)
        required = {INTAKE_LIMITATION, CONNECTIVITY_LIMITATION}
        if not required.issubset(proposed.limitations):
            raise AssistantSafetyError("ASSISTANT_REQUIRED_LIMITATION_MISSING")
        for limitation in proposed.limitations:
            _assert_safe_output(limitation)
            _assert_no_prompt_injection(limitation)
            _assert_no_sensitive_location(limitation, policy)
