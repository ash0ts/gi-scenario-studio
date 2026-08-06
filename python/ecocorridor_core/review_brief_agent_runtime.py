"""Isolated OpenAI Agents SDK runtime for review-brief selection.

The scientific worker owns authorization and persistence.  This module owns a
much narrower boundary: it launches one short-lived Python process, sends an
alias-only evidence snapshot and ephemeral credentials over framed stdin, and
accepts a structured selection only after the native W&B Weave Agents trace is
flushed and read back.

Nothing in this module can open a project database, inspect a project path,
run a solver, search the web, or mutate engineering/ecological state.  The
child may receive only the read-only functions declared below. A prepare run
uses at most seven tool turns so its eighth and final model turn can emit the
closed structured result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator
from scenario_platform.cloud_sync import InMemoryCredentialSession

from .assistants import (
    AssistantProviderConfiguration,
    AssistantRunAssociation,
    ProviderCredentialSession,
)
from .contracts import ContractModel, canonical_sha256

REVIEW_BRIEF_AGENT_PROTOCOL = "ecocorridor-review-brief-agent-ipc/v1"
REVIEW_BRIEF_MAX_CONTEXT_BYTES = 64 * 1024
REVIEW_BRIEF_MAX_FRAME_BYTES = 512 * 1024
REVIEW_BRIEF_MAX_TURNS = 8
REVIEW_BRIEF_MAX_TOOL_CALLS = 8
REVIEW_BRIEF_DEFAULT_DEADLINE_SECONDS = 90.0
REVIEW_BRIEF_AUTHORIZATION_LEASE_SECONDS = 420
REVIEW_BRIEF_AGENT_TOOLS = (
    "review_brief_get_scope",
    "review_brief_list_findings",
    "review_brief_get_findings",
    "review_brief_list_option_constraints",
    "review_brief_list_model_fitness_gaps",
    "review_brief_resolve_evidence_aliases",
    "review_brief_get_reference_status",
    "review_brief_get_authorization_status",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TRACE_ID = re.compile(r"^trace_[0-9a-f]{32}$")
_WAND_DB_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_WANDB_SERVERLESS_INFERENCE_BASE_URL = "https://api.inference.wandb.ai/v1"


class ReviewBriefAgentRuntimeError(RuntimeError):
    """A fail-closed agent subprocess, provider, or trace boundary failed."""


def _close_subprocess_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _terminate_subprocess_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate the isolated agent and descendants without a shell.

    POSIX children run in their own session, so killing the process group also
    closes SDK descendants that inherited credentials or output pipes. Windows
    uses the operating system's tree-aware task terminator before killing the
    leader as a final fallback.
    """

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif os.name == "nt":  # pragma: no cover - exercised in Windows release CI
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass


def _communicate_with_deadline(
    process: subprocess.Popen[bytes],
    *,
    deadline_seconds: float,
    deadline_error_code: str,
) -> tuple[bytes, bytes]:
    try:
        return process.communicate(timeout=deadline_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_subprocess_tree(process)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _close_subprocess_pipes(process)
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise ReviewBriefAgentRuntimeError(deadline_error_code) from exc


class ReviewBriefProviderMode(str):
    """String constants kept lightweight for JSON schema stability."""

    RESPONSES = "RESPONSES"
    CHAT_COMPLETIONS = "CHAT_COMPLETIONS"


class ReviewBriefAgentProvider(ContractModel):
    """Non-secret, explicit provider capabilities for one invocation."""

    base_url: str
    model: str
    mode: Literal["RESPONSES", "CHAT_COMPLETIONS"]
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=180.0)
    tool_calling_preflight_required: Literal[True] = True
    strict_structured_output_preflight_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_provider(self) -> ReviewBriefAgentProvider:
        validated = AssistantProviderConfiguration(
            base_url=self.base_url,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
        )
        if validated.base_url != self.base_url.rstrip("/"):
            raise ValueError("provider base URL must be canonical")
        return self


class ReviewBriefWeaveTarget(ContractModel):
    """Control-plane W&B/Weave target; never included in model input."""

    entity: str
    project: str
    api_base_url: str
    web_base_url: str
    external_project_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("entity", "project")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not _WAND_DB_SLUG.fullmatch(value):
            raise ValueError("W&B entity/project must be an explicit safe slug")
        return value

    @field_validator("api_base_url", "web_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
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
        ):
            raise ValueError("W&B/Weave URL must be HTTPS or explicit loopback HTTP")
        return normalized


def _wandb_inference_project(
    *, provider_base_url: str, weave_target: ReviewBriefWeaveTarget
) -> str | None:
    """Return W&B's non-secret inference routing value for its canonical origin only."""

    if provider_base_url != _WANDB_SERVERLESS_INFERENCE_BASE_URL:
        return None
    return f"{weave_target.entity}/{weave_target.project}"


def _configure_weave_pii_redaction(runtime: Path) -> None:
    """Install an offline, cached Presidio engine before any Weave serialization."""

    try:
        import dataclasses

        import tldextract
        import weave.utils.pii_redaction as weave_pii
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from weave.utils.sanitize import REDACTED_VALUE, redact_dataclass_fields, should_redact
    except ImportError as exc:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PII_REDACTION_UNAVAILABLE") from exc

    cache_directory = runtime / "tldextract-cache"
    cache_directory.mkdir(mode=0o700, exist_ok=True)
    if cache_directory.is_symlink() or runtime not in cache_directory.resolve().parents:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_RUNTIME_PATH_UNSAFE")
    # Presidio's email recognizer uses the module-level extractor. Force its
    # bundled Public Suffix List so an agent subprocess never performs an
    # undeclared network fetch while redacting trace data.
    tldextract.extract = tldextract.TLDExtract(
        cache_dir=str(cache_directory),
        suffix_list_urls=(),
        fallback_to_snapshot=True,
    )

    @lru_cache(maxsize=1)
    def analyzer_engine() -> Any:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        )
        return AnalyzerEngine(nlp_engine=provider.create_engine())

    def redact_results(value: str, results: Any) -> str:
        spans: list[tuple[int, int]] = []
        for result in sorted(results, key=lambda item: (int(item.start), int(item.end))):
            start, end = int(result.start), int(result.end)
            if start < 0 or end > len(value) or start >= end:
                raise ReviewBriefAgentRuntimeError(
                    "REVIEW_BRIEF_AGENT_PII_REDACTION_UNAVAILABLE"
                )
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        redacted = value
        for start, end in reversed(spans):
            redacted = redacted[:start] + REDACTED_VALUE + redacted[end:]
        return redacted

    class _Anonymized:
        def __init__(self, text: str) -> None:
            self.text = text

    class _LocalAnonymizer:
        def anonymize(self, *, text: str, analyzer_results: Any) -> _Anonymized:
            return _Anonymized(redact_results(text, analyzer_results))

    @lru_cache(maxsize=1)
    def engines() -> tuple[Any, Any]:
        return analyzer_engine(), _LocalAnonymizer()

    def redact_string(value: str) -> str:
        if not value:
            return value
        results = analyzer_engine().analyze(
            text=value,
            language="en",
            entities=weave_pii._get_redaction_entities(),
        )
        return redact_results(value, results)

    def redact_with_digest_fields(value: Any) -> Any:
        if isinstance(value, str):
            return redact_string(value)
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            for key, nested in value.items():
                if isinstance(key, str) and should_redact(key):
                    redacted[key] = REDACTED_VALUE
                elif (
                    isinstance(key, str)
                    and key.endswith("_sha256")
                    and isinstance(nested, str)
                    and re.fullmatch(r"[0-9a-f]{64}", nested)
                ):
                    redacted[key] = nested
                else:
                    redacted[key] = redact_with_digest_fields(nested)
            return redacted
        if isinstance(value, list):
            return [redact_with_digest_fields(item) for item in value]
        if dataclasses.is_dataclass(value):
            return redact_dataclass_fields(value, redact_with_digest_fields)
        return value

    weave_pii._get_engines = engines
    weave_pii.redact_pii_string = redact_string
    weave_pii.redact_pii = redact_with_digest_fields
    digest = "a8142d6da77747b58e57708edfeaa9d83e3d465cf3503f9636affa9ef169a919"
    probe = weave_pii.redact_pii_string(
        f"Contact alice@example.com in Seattle. Evidence digest {digest}."
    )
    structured_probe = weave_pii.redact_pii(
        {
            "snapshot_sha256": digest,
            "contact": "Contact alice@example.com in Seattle.",
            "api_key": "must-not-survive",
        }
    )
    if (
        "alice@example.com" in probe
        or "Seattle" in probe
        or structured_probe.get("snapshot_sha256") != digest
        or "alice@example.com" in str(structured_probe.get("contact"))
        or "Seattle" in str(structured_probe.get("contact"))
        or structured_probe.get("api_key") != REDACTED_VALUE
    ):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PII_REDACTION_UNAVAILABLE")


class ReviewBriefAgentBinding(ContractModel):
    """Opaque, short-lived authorization lease rechecked by every tool call.

    The worker issues this immutable lease only after resolving the exact
    project, analysis, portfolio, revision, alias set, and cloud authorization.
    Revocation takes effect no later than lease expiry and always before a new
    invocation; the child has no database or generic RPC access.
    """

    project_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_revision_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_board_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_authorization_decision_id: str = Field(pattern=r"^evidence_auth_[0-9a-f]{24}$")
    current_authorizations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cloud_authorization_kind: Literal["RESTRICTED_EXACT_SCOPE", "RIDGEWAY_CC0_TRACE_ONLY"]
    cloud_authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permitted_finding_aliases: list[str] = Field(min_length=1, max_length=256)
    permitted_evidence_aliases: list[str] = Field(min_length=1, max_length=256)
    permitted_scenario_aliases: list[str] = Field(default_factory=list, max_length=256)
    authorization_lease_issued_at: AwareDatetime
    authorization_lease_expires_at: AwareDatetime
    authorization_lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_aliases(self) -> ReviewBriefAgentBinding:
        for label, aliases in (
            ("finding", self.permitted_finding_aliases),
            ("evidence", self.permitted_evidence_aliases),
            ("scenario", self.permitted_scenario_aliases),
        ):
            if len(aliases) != len(set(aliases)):
                raise ValueError(f"permitted {label} aliases must be unique")
            if any(not _OPAQUE_ID.fullmatch(alias) for alias in aliases):
                raise ValueError(f"permitted {label} aliases must be opaque")
        duration = (
            self.authorization_lease_expires_at - self.authorization_lease_issued_at
        ).total_seconds()
        if duration <= 0 or duration > REVIEW_BRIEF_AUTHORIZATION_LEASE_SECONDS:
            raise ValueError("review-brief authorization lease duration is invalid")
        if canonical_sha256(self.authorization_lease_payload()) != (
            self.authorization_lease_sha256
        ):
            raise ValueError("review-brief authorization lease digest is inconsistent")
        return self

    def authorization_lease_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"authorization_lease_sha256"})

    def assert_authorization_lease_active(self) -> None:
        if canonical_sha256(self.authorization_lease_payload()) != (
            self.authorization_lease_sha256
        ):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_LEASE_CHANGED")
        now = datetime.now(UTC)
        if now < self.authorization_lease_issued_at or now >= self.authorization_lease_expires_at:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_LEASE_EXPIRED")

    @classmethod
    def from_authorized_snapshot(
        cls,
        *,
        project_id: str,
        analysis_id: str,
        portfolio_id: str,
        model_revision_id: str,
        decision_board_sha256: str,
        cloud_authorization_kind: Literal["RESTRICTED_EXACT_SCOPE", "RIDGEWAY_CC0_TRACE_ONLY"],
        cloud_authorization_sha256: str,
        snapshot: Any,
    ) -> ReviewBriefAgentBinding:
        """Derive the exact opaque child boundary from a worker-validated snapshot."""

        payload = snapshot.model_dump(mode="json")
        findings = payload.get("findings")
        if not isinstance(findings, list) or not findings:
            raise ValueError("authorized review snapshot has no findings")
        issued_at = datetime.now(UTC)
        candidate_payload = {
            "project_id_sha256": _sha256_text(project_id),
            "analysis_id_sha256": _sha256_text(analysis_id),
            "portfolio_id_sha256": _sha256_text(portfolio_id),
            "model_revision_id_sha256": _sha256_text(model_revision_id),
            "decision_board_sha256": decision_board_sha256,
            "evidence_authorization_decision_id": payload.get("evidence_authorization_decision_id"),
            "current_authorizations_sha256": payload.get("current_authorizations_sha256"),
            "authorization_sha256": payload.get("authorization_sha256"),
            "cloud_authorization_kind": cloud_authorization_kind,
            "cloud_authorization_sha256": cloud_authorization_sha256,
            "permitted_finding_aliases": [str(item["finding_alias"]) for item in findings],
            "permitted_evidence_aliases": sorted(
                {str(alias) for item in findings for alias in item.get("evidence_aliases", [])}
            ),
            "permitted_scenario_aliases": sorted(
                {str(alias) for item in findings for alias in item.get("scenario_aliases", [])}
            ),
            "authorization_lease_issued_at": issued_at,
            "authorization_lease_expires_at": issued_at
            + timedelta(seconds=REVIEW_BRIEF_AUTHORIZATION_LEASE_SECONDS),
        }
        provisional = cls.model_construct(
            **candidate_payload,
            authorization_lease_sha256="0" * 64,
        )
        candidate = cls(
            **candidate_payload,
            authorization_lease_sha256=canonical_sha256(provisional.authorization_lease_payload()),
        )
        if candidate.analysis_id_sha256 != payload.get("analysis_id_sha256"):
            raise ValueError("authorized review snapshot belongs to another analysis")
        if candidate.model_revision_id_sha256 != payload.get("model_revision_id_sha256"):
            raise ValueError("authorized review snapshot belongs to another model revision")
        source_mode = str(payload.get("source_mode", ""))
        expected_kind = (
            "RIDGEWAY_CC0_TRACE_ONLY"
            if source_mode == "RIDGEWAY_SYNTHETIC"
            else "RESTRICTED_EXACT_SCOPE"
            if source_mode == "PROFESSIONAL"
            else None
        )
        if candidate.cloud_authorization_kind != expected_kind:
            raise ValueError("cloud authorization kind does not match the review source mode")
        return candidate


class ReviewBriefAgentTraceControl(ContractModel):
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    association_id: str
    association_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wandb_run_id: str
    wandb_run_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wandb_run_lifecycle: Literal["ATTACH_EXISTING", "CREATE_OR_RESUME_AND_FINISH"] = (
        "ATTACH_EXISTING"
    )

    @model_validator(mode="after")
    def validate_run_id(self) -> ReviewBriefAgentTraceControl:
        if not _OPAQUE_ID.fullmatch(self.association_id):
            raise ValueError("W&B association ID must be opaque")
        if _sha256_text(self.association_id) != self.association_id_sha256:
            raise ValueError("W&B association ID digest is inconsistent")
        if not _OPAQUE_ID.fullmatch(self.wandb_run_id):
            raise ValueError("W&B run ID must be opaque")
        if _sha256_text(self.wandb_run_id) != self.wandb_run_id_sha256:
            raise ValueError("W&B run ID digest is inconsistent")
        return self


class ReviewBriefAgentCapabilityReceipt(ContractModel):
    provider_mode: Literal["RESPONSES", "CHAT_COMPLETIONS"]
    tool_calling_verified: Literal[True] = True
    strict_structured_output_verified: Literal[True] = True
    trace_readback_verified: Literal[True] = True
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    trace_url: str


class ReviewBriefAgentChildRequest(ContractModel):
    protocol_version: Literal[REVIEW_BRIEF_AGENT_PROTOCOL] = REVIEW_BRIEF_AGENT_PROTOCOL
    action: Literal["PREFLIGHT", "PREPARE"]
    provider: ReviewBriefAgentProvider
    weave_target: ReviewBriefWeaveTarget
    trace: ReviewBriefAgentTraceControl
    binding: ReviewBriefAgentBinding
    runner_request: dict[str, Any]
    provider_api_key: SecretStr
    wandb_api_key: SecretStr

    def ipc_payload(self) -> dict[str, Any]:
        """Return the one explicit serialization path allowed to reveal secrets."""

        payload = self.model_dump(mode="json", exclude={"provider_api_key", "wandb_api_key"})
        payload["provider_api_key"] = self.provider_api_key.get_secret_value()
        payload["wandb_api_key"] = self.wandb_api_key.get_secret_value()
        return payload


class ReviewBriefAgentChildResponse(ContractModel):
    protocol_version: Literal[REVIEW_BRIEF_AGENT_PROTOCOL] = REVIEW_BRIEF_AGENT_PROTOCOL
    ok: bool
    result: dict[str, Any] | None = None
    capability: ReviewBriefAgentCapabilityReceipt | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,127}$")

    @model_validator(mode="after")
    def validate_disposition(self) -> ReviewBriefAgentChildResponse:
        if self.ok:
            if self.error_code is not None or (self.result is None) == (self.capability is None):
                raise ValueError("successful child response must contain exactly one result")
        elif self.error_code is None or self.result is not None or self.capability is not None:
            raise ValueError("failed child response must contain only a safe error code")
        return self


@dataclass(slots=True, repr=False)
class ReviewBriefAgentCredentialLease:
    """Ephemeral secrets copied only while the child frame is written."""

    provider_api_key: str
    wandb_api_key: str

    def __post_init__(self) -> None:
        for value in (self.provider_api_key, self.wandb_api_key):
            if not 16 <= len(value) <= 512 or any(character.isspace() for character in value):
                raise ValueError("agent credential has an invalid shape")

    def __repr__(self) -> str:  # pragma: no cover - safety invariant
        return (
            "ReviewBriefAgentCredentialLease(provider_api_key=<redacted>, wandb_api_key=<redacted>)"
        )


CredentialLeaseFactory = Callable[[], Any]


class ReviewBriefAgentSubprocess(Protocol):
    def invoke(
        self,
        *,
        request: ReviewBriefAgentChildRequest,
        deadline_seconds: float,
    ) -> ReviewBriefAgentChildResponse: ...


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _trace_url_matches_target(url: str, target: ReviewBriefWeaveTarget) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(target.web_base_url)
    parts = parsed.path.split("/")
    return bool(
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and target.entity in parts
        and target.project in parts
    )


def _write_frame(stream: Any, payload: bytes | bytearray) -> None:
    if len(payload) > REVIEW_BRIEF_MAX_FRAME_BYTES:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FRAME_TOO_LARGE")
    stream.write(f"{len(payload):08x}\n".encode("ascii"))
    stream.write(payload)
    stream.flush()


def _read_frame(stream: Any) -> bytes:
    header = stream.readline(10)
    if len(header) != 9 or header[-1:] != b"\n":
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FRAME_INVALID")
    try:
        length = int(header[:8], 16)
    except ValueError as exc:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FRAME_INVALID") from exc
    if length <= 0 or length > REVIEW_BRIEF_MAX_FRAME_BYTES:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FRAME_INVALID")
    payload = stream.read(length)
    if len(payload) != length:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FRAME_TRUNCATED")
    return payload


class PopenReviewBriefAgentSubprocess:
    """Launch exactly one isolated child for each invocation, without a shell."""

    def __init__(
        self,
        *,
        scratch_root: Path,
        module: str = "ecocorridor_core.review_brief_agent_process",
        python_executable: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.scratch_root = scratch_root.resolve()
        self.module = module
        self.python_executable = python_executable or sys.executable
        self.popen_factory = popen_factory
        if not self.scratch_root.is_dir() or self.scratch_root.is_symlink():
            raise ValueError("agent scratch root must be an existing, non-symlink directory")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{2,255}", module):
            raise ValueError("agent child module is invalid")

    def command(self) -> list[str]:
        """Return the source or frozen-worker child command without secrets."""

        if bool(getattr(sys, "frozen", False)):
            return [self.python_executable, "--review-brief-agent-subprocess"]
        return [self.python_executable, "-I", "-m", self.module]

    @staticmethod
    def _environment(runtime_dir: Path) -> dict[str, str]:
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
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        package_root = Path(__file__).resolve().parents[1]
        environment.update(
            {
                "PYTHONPATH": str(package_root),
                "PYTHONUNBUFFERED": "1",
                "TMPDIR": str(runtime_dir),
                "HOME": str(runtime_dir),
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
        return environment

    def invoke(
        self,
        *,
        request: ReviewBriefAgentChildRequest,
        deadline_seconds: float,
    ) -> ReviewBriefAgentChildResponse:
        if not 5.0 <= deadline_seconds <= 180.0:
            raise ValueError("agent deadline must be between 5 and 180 seconds")
        serialized = bytearray(_json_bytes(request.ipc_payload()))
        with tempfile.TemporaryDirectory(
            prefix="review-brief-agent-", dir=self.scratch_root
        ) as runtime_dir_value:
            runtime_dir = Path(runtime_dir_value)
            process = self.popen_factory(
                self.command(),
                cwd=runtime_dir,
                env=self._environment(runtime_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            try:
                if process.stdin is None:
                    raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_STDIN_UNAVAILABLE")
                _write_frame(process.stdin, serialized)
                process.stdin.close()
                process.stdin = None
                stdout, stderr = _communicate_with_deadline(
                    process,
                    deadline_seconds=deadline_seconds,
                    deadline_error_code="REVIEW_BRIEF_AGENT_DEADLINE_EXCEEDED",
                )
            finally:
                serialized[:] = b"\0" * len(serialized)
            if process.returncode != 0:
                # Never reflect stderr: provider/SDK diagnostics can contain credentials.
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PROCESS_FAILED")
            if len(stderr) > REVIEW_BRIEF_MAX_FRAME_BYTES:
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_STDERR_EXCEEDED")
            response = ReviewBriefAgentChildResponse.model_validate_json(
                _read_frame(_BytesReader(stdout))
            )
            if not response.ok:
                raise ReviewBriefAgentRuntimeError(
                    response.error_code or "REVIEW_BRIEF_AGENT_FAILED"
                )
            return response


class _BytesReader:
    """Minimal buffered-reader facade for an already captured child response."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def readline(self, limit: int = -1) -> bytes:
        end = self.payload.find(b"\n", self.offset)
        if end < 0:
            end = len(self.payload) - 1
        end += 1
        if limit >= 0:
            end = min(end, self.offset + limit)
        value = self.payload[self.offset : end]
        self.offset = end
        return value

    def read(self, size: int = -1) -> bytes:
        end = len(self.payload) if size < 0 else min(len(self.payload), self.offset + size)
        value = self.payload[self.offset : end]
        self.offset = end
        return value


class ReviewBriefSubprocessRunner:
    """Worker-facing ``ReviewBriefRunner`` implemented by one native child per call."""

    def __init__(
        self,
        *,
        provider: ReviewBriefAgentProvider,
        weave_target: ReviewBriefWeaveTarget,
        association: AssistantRunAssociation,
        binding: ReviewBriefAgentBinding,
        credential_lease: CredentialLeaseFactory,
        subprocess_client: ReviewBriefAgentSubprocess,
        deadline_seconds: float = REVIEW_BRIEF_DEFAULT_DEADLINE_SECONDS,
        wandb_run_lifecycle: Literal[
            "ATTACH_EXISTING", "CREATE_OR_RESUME_AND_FINISH"
        ] = "ATTACH_EXISTING",
    ) -> None:
        self.provider = provider
        self.weave_target = weave_target
        self.association = association
        self.binding = binding
        self.credential_lease = credential_lease
        self.subprocess_client = subprocess_client
        self.deadline_seconds = deadline_seconds
        self.wandb_run_lifecycle = wandb_run_lifecycle
        self._verified_preflights: set[str] = set()
        self._preflight_lock = threading.RLock()
        if _sha256_text(association.project_id) != binding.project_id_sha256:
            raise ValueError("agent project association does not match authorization")
        if (
            association.analysis_id is None
            or _sha256_text(association.analysis_id) != binding.analysis_id_sha256
        ):
            raise ValueError("agent analysis association does not match authorization")
        if _sha256_text(association.wandb_external_project_id) != (
            weave_target.external_project_id_sha256
        ):
            raise ValueError("agent W&B target does not match run association")

    @property
    def provider_model(self) -> str:
        return self.provider.model

    def _trace(
        self, *, call_alias: str, snapshot_sha256: str, action: str
    ) -> ReviewBriefAgentTraceControl:
        nonce = secrets.token_hex(32)
        digest = hashlib.sha256(
            (
                f"{REVIEW_BRIEF_AGENT_PROTOCOL}\0{action}\0{call_alias}\0"
                f"{snapshot_sha256}\0{self.association.association_id}\0"
                f"{self.association.wandb_run_id}\0{nonce}"
            ).encode()
        ).hexdigest()
        return ReviewBriefAgentTraceControl(
            trace_id=f"trace_{digest[:32]}",
            association_id=self.association.association_id,
            association_id_sha256=_sha256_text(self.association.association_id),
            wandb_run_id=self.association.wandb_run_id,
            wandb_run_id_sha256=_sha256_text(self.association.wandb_run_id),
            wandb_run_lifecycle=self.wandb_run_lifecycle,
        )

    @contextmanager
    def _leased_credentials(self) -> Iterator[ReviewBriefAgentCredentialLease]:
        candidate = self.credential_lease()
        if hasattr(candidate, "__enter__") and hasattr(candidate, "__exit__"):
            with candidate as value:
                if not isinstance(value, ReviewBriefAgentCredentialLease):
                    raise TypeError("credential lease returned an invalid value")
                yield value
        else:
            if not isinstance(candidate, ReviewBriefAgentCredentialLease):
                raise TypeError("credential lease returned an invalid value")
            try:
                yield candidate
            finally:
                candidate.provider_api_key = ""
                candidate.wandb_api_key = ""

    def _invoke(
        self, *, action: Literal["PREFLIGHT", "PREPARE"], request: Any
    ) -> ReviewBriefAgentChildResponse:
        runner_payload = request.model_dump(mode="json")
        snapshot_payload = runner_payload.get("snapshot")
        if not isinstance(snapshot_payload, dict):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_SNAPSHOT_MISSING")
        context = _json_bytes(snapshot_payload)
        if len(context) > REVIEW_BRIEF_MAX_CONTEXT_BYTES:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_CONTEXT_TOO_LARGE")
        # The provider sees the cumulative fixed instructions plus every
        # deterministic tool exchange, not just the compact input snapshot.
        from .review_brief_agent_process import review_brief_model_visible_context_bytes

        expanded_context = review_brief_model_visible_context_bytes(request.snapshot)
        if len(expanded_context) > REVIEW_BRIEF_MAX_CONTEXT_BYTES:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_CONTEXT_TOO_LARGE")
        self.binding.assert_authorization_lease_active()
        snapshot_sha256 = str(snapshot_payload.get("snapshot_sha256", ""))
        if not _SHA256.fullmatch(snapshot_sha256):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_SNAPSHOT_IDENTITY_INVALID")
        if snapshot_payload.get("authorization_sha256") != self.binding.authorization_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_IDENTITY_MISMATCH")
        if snapshot_payload.get("analysis_id_sha256") != self.binding.analysis_id_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_ANALYSIS_IDENTITY_MISMATCH")
        if (
            snapshot_payload.get("model_revision_id_sha256")
            != self.binding.model_revision_id_sha256
        ):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_REVISION_IDENTITY_MISMATCH")
        if snapshot_payload.get("evidence_authorization_decision_id") != (
            self.binding.evidence_authorization_decision_id
        ):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_IDENTITY_MISMATCH")
        if snapshot_payload.get("current_authorizations_sha256") != (
            self.binding.current_authorizations_sha256
        ):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_STATE_MISMATCH")
        findings = snapshot_payload.get("findings")
        if not isinstance(findings, list) or not findings:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FINDINGS_MISSING")
        finding_aliases = {
            str(item.get("finding_alias", "")) for item in findings if isinstance(item, dict)
        }
        evidence_aliases = {
            str(alias)
            for item in findings
            if isinstance(item, dict)
            for alias in item.get("evidence_aliases", [])
        }
        scenario_aliases = {
            str(alias)
            for item in findings
            if isinstance(item, dict)
            for alias in item.get("scenario_aliases", [])
        }
        if finding_aliases != set(self.binding.permitted_finding_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FINDING_AUTHORIZATION_MISMATCH")
        if evidence_aliases != set(self.binding.permitted_evidence_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_EVIDENCE_AUTHORIZATION_MISMATCH")
        if scenario_aliases != set(self.binding.permitted_scenario_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_SCENARIO_AUTHORIZATION_MISMATCH")
        call_alias = str(runner_payload.get("call_alias", ""))
        if not _OPAQUE_ID.fullmatch(call_alias):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_CALL_ALIAS_INVALID")
        with self._leased_credentials() as credentials:
            child = ReviewBriefAgentChildRequest(
                action=action,
                provider=self.provider,
                weave_target=self.weave_target,
                trace=self._trace(
                    call_alias=call_alias,
                    snapshot_sha256=snapshot_sha256,
                    action=action,
                ),
                binding=self.binding,
                runner_request=runner_payload,
                provider_api_key=credentials.provider_api_key,
                wandb_api_key=credentials.wandb_api_key,
            )
            response = self.subprocess_client.invoke(
                request=child,
                deadline_seconds=self.deadline_seconds,
            )
        if action == "PREFLIGHT":
            capability = response.capability
            if (
                capability is None
                or response.result is not None
                or capability.trace_id != child.trace.trace_id
                or capability.provider_mode != self.provider.mode
                or not _trace_url_matches_target(capability.trace_url, self.weave_target)
            ):
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PREFLIGHT_RECEIPT_INVALID")
        else:
            result = response.result
            trace = result.get("trace") if isinstance(result, dict) else None
            tool_calls = result.get("tool_calls") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or response.capability is not None
                or result.get("snapshot_sha256") != snapshot_sha256
                or result.get("provider_model") != self.provider.model
                or not isinstance(trace, dict)
                or trace.get("trace_id") != child.trace.trace_id
                or trace.get("association_id") != child.trace.association_id
                or trace.get("wandb_run_id") != child.trace.wandb_run_id
                or trace.get("flushed") is not True
                or trace.get("readback_verified") is not True
                or not _trace_url_matches_target(str(trace.get("trace_url", "")), self.weave_target)
                or not isinstance(tool_calls, list)
                or not 1 <= len(tool_calls) <= REVIEW_BRIEF_MAX_TOOL_CALLS
                or any(tool not in REVIEW_BRIEF_AGENT_TOOLS for tool in tool_calls)
            ):
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_RESULT_RECEIPT_INVALID")
        return response

    def preflight(self, *, request: Any) -> ReviewBriefAgentCapabilityReceipt:
        response = self._invoke(action="PREFLIGHT", request=request)
        if response.capability is None:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PREFLIGHT_MISSING")
        snapshot_sha256 = request.snapshot.snapshot_sha256
        with self._preflight_lock:
            self._verified_preflights.add(snapshot_sha256)
        return response.capability

    def prepare(self, *, request: Any) -> Any:
        # Delayed import lets the repository construct the runtime without importing
        # the higher-level service during worker bootstrap.
        from .review_brief import ReviewBriefRunnerResult

        snapshot_sha256 = request.snapshot.snapshot_sha256
        with self._preflight_lock:
            if snapshot_sha256 not in self._verified_preflights:
                self.preflight(request=request)
        response = self._invoke(action="PREPARE", request=request)
        if response.result is None:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_RESULT_MISSING")
        result = ReviewBriefRunnerResult.model_validate(response.result)
        if not bool(getattr(getattr(result, "trace", None), "readback_verified", False)):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_NOT_VERIFIED")
        return result


def review_brief_runner_from_sessions(
    *,
    provider_configuration: AssistantProviderConfiguration,
    provider_mode: Literal["RESPONSES", "CHAT_COMPLETIONS"],
    weave_target: ReviewBriefWeaveTarget,
    wandb_external_project_id: str,
    association: AssistantRunAssociation,
    binding: ReviewBriefAgentBinding,
    provider_credentials: ProviderCredentialSession,
    observability_credentials: InMemoryCredentialSession,
    scratch_root: Path,
    wandb_run_lifecycle: Literal["ATTACH_EXISTING", "CREATE_OR_RESUME_AND_FINISH"],
    deadline_seconds: float = REVIEW_BRIEF_DEFAULT_DEADLINE_SECONDS,
    subprocess_client: ReviewBriefAgentSubprocess | None = None,
) -> ReviewBriefSubprocessRunner:
    """Build a runner from the worker's current process-memory sessions.

    The returned runner does not copy either credential. Each invocation leases
    both keys only while it constructs and writes the child frame. This is the
    intended at-call-time seam for ``ProviderBackedAssistantRuntime``.
    """

    @contextmanager
    def credentials() -> Iterator[ReviewBriefAgentCredentialLease]:
        with provider_credentials.authorization_header() as authorization:
            if not authorization.startswith("Bearer "):
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_PROVIDER_CREDENTIAL_INVALID")
            provider_key = authorization.removeprefix("Bearer ")
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

    if _sha256_text(wandb_external_project_id) != weave_target.external_project_id_sha256:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_WANDB_PROJECT_IDENTITY_MISMATCH")
    return ReviewBriefSubprocessRunner(
        provider=ReviewBriefAgentProvider(
            base_url=provider_configuration.base_url,
            model=provider_configuration.model,
            mode=provider_mode,
            timeout_seconds=provider_configuration.timeout_seconds,
        ),
        weave_target=weave_target,
        association=association,
        binding=binding,
        credential_lease=credentials,
        subprocess_client=(
            subprocess_client
            if subprocess_client is not None
            else PopenReviewBriefAgentSubprocess(scratch_root=scratch_root)
        ),
        deadline_seconds=deadline_seconds,
        wandb_run_lifecycle=wandb_run_lifecycle,
    )


def safe_child_error_code(exc: BaseException) -> str:
    """Map every child exception to a credential-safe, finite code."""

    if isinstance(exc, ReviewBriefAgentRuntimeError) and _SAFE_ERROR.fullmatch(str(exc)):
        return str(exc)
    name = type(exc).__name__.upper().replace("_", "")
    if "TIMEOUT" in name:
        return "REVIEW_BRIEF_AGENT_DEADLINE_EXCEEDED"
    if "GUARDRAIL" in name or "MAXTURNS" in name:
        return "REVIEW_BRIEF_AGENT_BOUNDARY_REJECTED"
    provider_errors = {
        "BADREQUESTERROR": "REVIEW_BRIEF_PROVIDER_REQUEST_REJECTED",
        "AUTHENTICATIONERROR": "REVIEW_BRIEF_PROVIDER_AUTHENTICATION_FAILED",
        "PERMISSIONDENIEDERROR": "REVIEW_BRIEF_PROVIDER_ACCESS_DENIED",
        "NOTFOUNDERROR": "REVIEW_BRIEF_PROVIDER_MODEL_NOT_FOUND",
        "RATELIMITERROR": "REVIEW_BRIEF_PROVIDER_RATE_LIMITED",
        "APICONNECTIONERROR": "REVIEW_BRIEF_PROVIDER_UNAVAILABLE",
        "INTERNALSERVERERROR": "REVIEW_BRIEF_PROVIDER_FAILED",
        "APISTATUSERROR": "REVIEW_BRIEF_PROVIDER_FAILED",
        "MODELBEHAVIORERROR": "REVIEW_BRIEF_PROVIDER_OUTPUT_INVALID",
    }
    if name in provider_errors:
        return provider_errors[name]
    return "REVIEW_BRIEF_AGENT_INVOCATION_FAILED"


__all__ = [
    "REVIEW_BRIEF_AGENT_PROTOCOL",
    "REVIEW_BRIEF_AGENT_TOOLS",
    "REVIEW_BRIEF_MAX_CONTEXT_BYTES",
    "REVIEW_BRIEF_MAX_TOOL_CALLS",
    "REVIEW_BRIEF_MAX_TURNS",
    "PopenReviewBriefAgentSubprocess",
    "ReviewBriefAgentBinding",
    "ReviewBriefAgentCapabilityReceipt",
    "ReviewBriefAgentChildRequest",
    "ReviewBriefAgentChildResponse",
    "ReviewBriefAgentCredentialLease",
    "ReviewBriefAgentProvider",
    "ReviewBriefAgentRuntimeError",
    "ReviewBriefAgentTraceControl",
    "ReviewBriefProviderMode",
    "ReviewBriefSubprocessRunner",
    "ReviewBriefWeaveTarget",
    "review_brief_runner_from_sessions",
    "safe_child_error_code",
]
