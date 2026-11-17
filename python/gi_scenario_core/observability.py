"""Optional verified W&B binding and durable engineering-evidence cloud projection.

SQLite plus the local content-addressed store remain authoritative.  A cloud
failure changes only the durable outbox state; it never changes engineering
state, exact SWMM results, fitness, or export eligibility.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .contracts import (
    CloudArtifactManifest,
    CloudSyncState,
    ModelRevision,
    ObservabilityBinding,
    ObservabilityMode,
    ProjectDataPolicy,
    TelemetryEnvelope,
    TelemetryState,
    WandbRunReference,
    WeaveTraceReference,
)
from .repository import ProjectRepository

MAX_METADATA_BYTES = 256 * 1024
MAX_PROBE_BYTES = 4 * 1024
_DESTINATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WANDB_ARTIFACT_REFERENCE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,2}"
    r":v(?:0|[1-9][0-9]*)"
)
PROJECT_POLICY_GQL = """
query GIProjectPolicy($name: String!, $entity: String!) {
  project(name: $name, entityName: $entity) {
    id
    name
    entityName
    access
  }
}
"""
_ABSOLUTE_PATH = re.compile(
    r"(?:"
    r"/Users/|"
    r"[A-Za-z]:\\|"
    r"\\\\|"
    r"(?:^|[\s\"'=])/(?:[^\s/\"']+/)+[^\s\"']*|"
    r"(?:^|[\s\"'=])/[^\s/\"']+(?=$|[\"'])"
    r")"
)
_PROHIBITED_KEYS = {
    "apikey",
    "authorization",
    "consolelog",
    "cookie",
    "credential",
    "environment",
    "environmentvariable",
    "localabsolutepath",
    "localpath",
    "password",
    "privatekey",
    "rawconsolelog",
    "secret",
    "stderr",
    "stdout",
    "token",
}
_WANDB_ENV_LOCK = threading.RLock()
_SCIENTIFIC_OPERATIONS = {
    "adaptive_search_batch_proposal",
    "exact_result_ingestion",
    "adverse_effect_evaluation",
}
_TRACE_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_TRACE_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_TRACE_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TRACE_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:wandb[_ -]?)?(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token)"
    r"\s*[:=]\s*[^\s,;]+"
)
_TRACE_PHONE = re.compile(r"(?i)\b(?:phone|mobile|cell)\s*[:=]\s*\+?[0-9][0-9 ()-]{6,}[0-9]")
_TRACE_REDACTED = "[redacted:local-sensitive-content]"


class CredentialSession:
    """Process-memory W&B credential initialized only by the native host."""

    def __init__(self) -> None:
        self._api_key: bytearray | None = None
        self._base_url = "https://api.wandb.ai"

    def configure(self, api_key: str, base_url: str) -> None:
        self._validate(api_key)
        if self._api_key is not None:
            self.clear()
        self._api_key = bytearray(api_key.encode("utf-8"))
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def _validate(api_key: str) -> None:
        if not 20 <= len(api_key) <= 512 or any(character.isspace() for character in api_key):
            raise ValueError("W&B credential has an invalid shape")

    @contextmanager
    def candidate(self, api_key: str, base_url: str):
        """Install a candidate credential and roll it back unless validation succeeds.

        A failed account reconfiguration must not destroy a previously verified
        session or leave the rejected credential resident in worker memory.
        """

        self._validate(api_key)
        candidate = bytearray(api_key.encode("utf-8"))
        previous = self._api_key
        previous_base_url = self._base_url
        self._api_key = candidate
        self._base_url = base_url.rstrip("/")
        try:
            yield
        except BaseException:
            candidate[:] = b"\0" * len(candidate)
            self._api_key = previous
            self._base_url = previous_base_url
            raise
        else:
            if previous is not None:
                previous[:] = b"\0" * len(previous)

    @property
    def configured(self) -> bool:
        return self._api_key is not None or bool(os.environ.get("WANDB_API_KEY"))

    @contextmanager
    def environment(self):
        # The manually approved evaluation campaign supplies one immutable
        # process-level credential. In that case there is no secret mutation to
        # serialize, and allowing two readers preserves the preregistered
        # provider concurrency. Native-host credentials remain protected by the
        # process-wide mutation lock below.
        if self._api_key is None:
            value = os.environ.get("WANDB_API_KEY")
            if not value:
                raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
            self._validate(value)
            yield
            return
        with _WANDB_ENV_LOCK:
            previous_key = os.environ.get("WANDB_API_KEY")
            previous_base = os.environ.get("WANDB_BASE_URL")
            temporary_key: str | None = None
            if self._api_key is not None:
                temporary_key = self._api_key.decode("utf-8")
                os.environ["WANDB_API_KEY"] = temporary_key
                os.environ["WANDB_BASE_URL"] = self._base_url
            try:
                yield
            finally:
                if previous_key is None:
                    os.environ.pop("WANDB_API_KEY", None)
                else:
                    os.environ["WANDB_API_KEY"] = previous_key
                if previous_base is None:
                    os.environ.pop("WANDB_BASE_URL", None)
                else:
                    os.environ["WANDB_BASE_URL"] = previous_base
                temporary_key = None

    @contextmanager
    def reveal(self):
        """Yield the credential only inside the worker's serialized secret scope."""

        if not self.configured:
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        with self.environment():
            value = os.environ.get("WANDB_API_KEY")
            if not value:
                raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
            yield value

    def clear(self) -> None:
        if self._api_key is not None:
            self._api_key[:] = b"\0" * len(self._api_key)
            self._api_key = None


@contextmanager
def _wandb_environment(workspace: Path):
    """Temporarily apply quiet, project-scoped W&B SDK directories."""

    root = (workspace / "cloud" / "wandb").resolve()
    values = {
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
        "WANDB_SILENT": "true",
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_DIR": str(root / "run"),
        "WANDB_CACHE_DIR": str(root / "cache"),
        "WANDB_ARTIFACT_DIR": str(root / "artifacts"),
    }
    for key in ("WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_ARTIFACT_DIR"):
        Path(values[key]).mkdir(parents=True, exist_ok=True)
    with _WANDB_ENV_LOCK:
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@dataclass(frozen=True)
class WandbUploadReceipt:
    run_id: str
    url: str
    artifact_ids: list[str]


@dataclass(frozen=True)
class WeaveAgentTurnReceipt:
    conversation_id: str
    trace_id: str
    root_span_id: str
    url: str


class WeaveAgentTurnRecorder:
    """Small façade over Weave's OTel-backed Conversation SDK objects."""

    def __init__(self, *, weave: Any, client: Any, turn: Any, include_content: bool) -> None:
        self.weave = weave
        self.client = client
        self.turn = turn
        self.include_content = include_content
        self.trace_id, self.root_span_id = _active_turn_ids(turn)
        self.receipt: WeaveAgentTurnReceipt | None = None
        self.expected_turn_error = False
        self.validation_recorded = False
        self.final_answer_recorded = False
        # The campaign compares deterministic and model-backed architectures.
        # Their traces do not all contain the same chat events: D0 has no LLM
        # or tool span, while D1 may have a tool span but no LLM span.  Record
        # what this turn actually emitted so cloud readback can prove that
        # exact trace shape instead of assuming every successful turn used a
        # tool-backed agent.
        self.required_message_types: set[str] = {"user_message"}
        self.llm_completion_count = 0
        self.tool_completion_count = 0

    @contextmanager
    def llm(
        self,
        *,
        model: str,
        system_instructions: list[str],
        input_messages: list[dict[str, Any]],
    ):
        from weave.conversation import Message, Usage  # type: ignore[import-not-found]

        recorded_system = (
            _prepare_trace_value(system_instructions, context="assistant system instructions")
            if self.include_content
            else ["[content omitted by project policy]"]
        )
        with self.weave.start_llm(
            model=model,
            provider_name="wandb_inference",
            system_instructions=recorded_system,
        ) as span:

            class LlmHandle:
                def record(self, completion: Any) -> None:
                    output_messages = []
                    output_content = completion.content
                    if not output_content and completion.tool_calls:
                        # W&B Inference tool-routing responses commonly have no
                        # text. Keep the routing chat span visible in the Agents
                        # span graph with deterministic local wording so child
                        # tool parentage can be independently audited. This is
                        # not provider prose and contains no tool arguments.
                        output_content = "[read-only evidence tool request]"
                    if output_content:
                        output_messages.append(
                            Message(
                                role="assistant",
                                content=(
                                    _prepare_trace_value(
                                        output_content,
                                        context="assistant model output",
                                    )
                                    if self_outer.include_content
                                    else "[content omitted by project policy]"
                                ),
                            )
                        )
                    recorded_inputs = []
                    for item in input_messages:
                        content: Any = item.get("content", "")
                        if set(item).difference({"role", "content"}):
                            # Preserve the exact tool-call protocol sent to the
                            # provider. The Conversation SDK's flat Message form
                            # cannot represent OpenAI tool_calls directly, so the
                            # complete non-role payload is recorded canonically.
                            content = _canonical_json(
                                {key: value for key, value in item.items() if key != "role"}
                            ).decode("utf-8")
                        recorded_inputs.append(
                            Message(
                                role=item["role"],
                                content=(
                                    _prepare_trace_value(
                                        content,
                                        context="assistant model input",
                                    )
                                    if self_outer.include_content
                                    else "[content omitted by project policy]"
                                ),
                                tool_call_id=str(item.get("tool_call_id") or ""),
                            )
                        )
                    span.record(
                        input_messages=recorded_inputs,
                        output_messages=output_messages,
                        usage=Usage(
                            input_tokens=completion.usage.input_tokens,
                            output_tokens=completion.usage.output_tokens,
                        ),
                        response_id=completion.provider_call_id or None,
                        response_model=completion.model,
                        finish_reasons=[completion.finish_reason]
                        if completion.finish_reason
                        else None,
                        output_type="tool_calls" if completion.tool_calls else "text",
                    )
                    self_outer.required_message_types.add("assistant_message")
                    self_outer.llm_completion_count += 1

            self_outer = self
            yield LlmHandle()

    @contextmanager
    def tool(self, *, name: str, arguments: dict[str, Any], tool_call_id: str):
        if self.include_content:
            recorded_arguments = _prepare_trace_value(
                arguments,
                context="assistant tool arguments",
            )
        else:
            payload = _canonical_json(arguments)
            recorded_arguments = {
                "content_logged": False,
                "arguments_sha256": hashlib.sha256(payload).hexdigest(),
            }
        with self.weave.start_tool(
            name=name,
            arguments=json.dumps(recorded_arguments, sort_keys=True, separators=(",", ":")),
            tool_call_id=tool_call_id,
        ) as span:

            class ToolHandle:
                def record(self, result: dict[str, Any]) -> None:
                    if self_outer.include_content:
                        span.result = _prepare_trace_value(
                            result,
                            context="assistant tool result",
                        )
                    else:
                        payload = json.dumps(
                            result,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            default=str,
                        ).encode("utf-8")
                        span.result = {
                            "content_logged": False,
                            "result_sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    self_outer.required_message_types.add("tool_call")
                    self_outer.tool_completion_count += 1

            self_outer = self
            yield ToolHandle()

    def validation(self, *, valid: bool, checked_claim_count: int, error_codes: list[str]) -> None:
        safe_error_codes = [
            code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else "EVIDENCE_VALIDATION_FAILED"
            for code in error_codes
        ]
        self.turn.set_attributes(
            {
                "gi.validation.passed": valid,
                "gi.validation.checked_claim_count": checked_claim_count,
                "gi.validation.error_codes": safe_error_codes,
            }
        )
        self.validation_recorded = True
        self.expected_turn_error = not valid
        if not valid:
            mark_error = getattr(self.turn, "_record_otel_error", None)
            if not callable(mark_error):
                raise RuntimeError("WEAVE_AGENT_ERROR_STATUS_UNAVAILABLE")
            detail = ",".join(safe_error_codes) or "EVIDENCE_VALIDATION_FAILED"
            mark_error(RuntimeError(f"EVIDENCE_GUIDE_VALIDATION_REJECTED:{detail}"))

    def record_final_answer(self, answer: str) -> None:
        """Attach the locally materialized, evidence-validated answer to the root turn."""

        if not self.validation_recorded:
            raise RuntimeError("WEAVE_UNVALIDATED_TURN_CANNOT_RECORD_ANSWER")
        if self.expected_turn_error:
            raise RuntimeError("WEAVE_REJECTED_TURN_CANNOT_RECORD_ANSWER")
        if not answer.strip():
            raise ValueError("the final Evidence Guide answer cannot be empty")
        from weave.conversation import Message  # type: ignore[import-not-found]

        content = (
            _prepare_trace_value(answer, context="assistant materialized answer")
            if self.include_content
            else "[content omitted by project policy]"
        )
        record = getattr(self.turn, "record", None)
        if not callable(record):
            raise RuntimeError(  # noqa: TRY004 - required SDK tracing capability
                "WEAVE_AGENT_OUTPUT_RECORDING_UNAVAILABLE"
            )
        record(output_messages=[Message(role="assistant", content=content)])
        self.final_answer_recorded = True
        self.required_message_types.add("assistant_message")

    def assert_operations(
        self,
        *,
        llm_calls: int,
        tool_calls: int,
        final_answer: bool,
    ) -> None:
        """Bind the trace to the executor's independently counted operations."""

        expected_types = {"user_message"}
        if llm_calls or final_answer:
            expected_types.add("assistant_message")
        if tool_calls:
            expected_types.add("tool_call")
        if (
            self.llm_completion_count != llm_calls
            or self.tool_completion_count != tool_calls
            or self.final_answer_recorded is not final_answer
            or self.required_message_types != expected_types
        ):
            raise RuntimeError("WEAVE_TRACE_OPERATION_COUNT_MISMATCH")
        self.turn.set_attributes(
            {
                "gi.trace.expected_llm_calls": llm_calls,
                "gi.trace.expected_tool_calls": tool_calls,
                "gi.trace.final_answer_recorded": final_answer,
            }
        )


class WandbEvidenceAdapter(Protocol):
    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding: ...

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt: ...


class WeaveAssistantAdapter(Protocol):
    def trace_call(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference: ...

    def trace_scientific_operation(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        wandb_run_id: str | None = None,
        wandb_step: int | None = None,
    ) -> WeaveTraceReference: ...


class WandbAdapter:
    """Lazy SDK adapter; credentials are read only from the worker environment."""

    def __init__(self, credentials: CredentialSession | None = None) -> None:
        self.credentials = credentials or CredentialSession()

    def _sdk(self) -> Any:
        if not os.environ.get("WANDB_API_KEY"):
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        try:
            import wandb  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WANDB_SDK_UNAVAILABLE") from exc
        return wandb

    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding:
        if binding.mode != ObservabilityMode.ONLINE:
            raise ValueError("initial W&B binding validation requires online mode")
        _validate_destination(binding)
        if binding.deployment != policy.deployment:
            raise ValueError("binding deployment does not match the project data policy")
        with (
            self.credentials.environment(),
            tempfile.TemporaryDirectory(prefix="gi-wandb-validate-") as temporary,
            _wandb_environment(Path(temporary)),
        ):
            return self._validate_binding_online(binding)

    def _validate_binding_online(self, binding: ObservabilityBinding) -> ObservabilityBinding:
        _validate_destination(binding)
        wandb = self._sdk()
        api = wandb.Api()
        policy = _resolve_exact_project_policy(api, binding.entity, binding.external_project)
        if policy is None:
            raise RuntimeError("WANDB_PROJECT_NOT_ACCESSIBLE")
        if policy["access"].casefold() != "restricted":
            raise ValueError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
        project_id = policy["id"]
        if binding.external_project_id not in {"pending-validation", project_id}:
            raise RuntimeError("WANDB_PROJECT_ID_CHANGED")
        payload = binding.model_dump(mode="json")
        payload.update(
            {
                "external_project_id": project_id,
                "verified": True,
                "verified_at": datetime.now(UTC),
            }
        )
        return ObservabilityBinding.model_validate(payload)

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt:
        with _wandb_environment(workspace), self.credentials.environment():
            self._validate_binding_online(binding)
            return self._upload_online(envelope, binding, artifact_paths)

    def _upload_online(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
    ) -> WandbUploadReceipt:
        wandb = self._sdk()
        input_artifact_refs = _wandb_input_artifact_refs(envelope.metadata)
        table_specs = _wandb_table_specs(envelope.metadata)
        config_metadata = dict(envelope.metadata)
        config_metadata.pop("tables", None)
        validated_sources: dict[str, Path] = {}
        for manifest in envelope.artifacts:
            source = artifact_paths.get(manifest.artifact_id)
            if source is None or not source.is_file():
                raise RuntimeError("WANDB_ARTIFACT_SOURCE_MISSING")
            if source.stat().st_size != manifest.bytes or _sha256_path(source) != manifest.sha256:
                raise RuntimeError("WANDB_ARTIFACT_SOURCE_HASH_MISMATCH")
            validated_sources[manifest.artifact_id] = source
        settings = wandb.Settings(
            console="off",
            disable_code=True,
            disable_git=True,
            silent=True,
            save_code=False,
            x_disable_machine_info=True,
            x_disable_meta=True,
            x_disable_stats=True,
            x_save_requirements=False,
        )
        run_id = _wandb_run_id(envelope.project_id, envelope.envelope_id)
        run = wandb.init(
            entity=binding.entity,
            project=binding.external_project,
            id=run_id,
            resume="allow",
            job_type=envelope.event_type,
            config={
                "schema_version": "2.0",
                "category": envelope.category,
                "subject_id": envelope.subject_id,
                "metadata": config_metadata,
                "local_authority": True,
            },
            settings=settings,
            reinit=True,
        )
        if run is None:  # pragma: no cover - defensive around third-party SDK
            raise RuntimeError("WANDB_RUN_CREATION_FAILED")
        artifact_ids: list[str] = []
        artifact_projection: list[dict[str, Any]] = []
        try:
            numeric = _finite_numeric_values(envelope.metadata)
            if numeric:
                run.log(numeric)
            for table_name, table_spec in table_specs.items():
                run.log(
                    {
                        table_name: wandb.Table(
                            columns=table_spec["columns"],
                            data=table_spec["rows"],
                        )
                    }
                )
            run.summary["table_names"] = sorted(table_specs)
            run.summary["envelope_sha256"] = _envelope_sha256(envelope)
            run.summary["local_authority"] = True
            for reference in input_artifact_refs:
                run.use_artifact(reference)
            run.summary["input_artifact_refs"] = input_artifact_refs
            for manifest in envelope.artifacts:
                source = validated_sources[manifest.artifact_id]
                artifact = wandb.Artifact(
                    name=f"{manifest.category}-{manifest.artifact_id}",
                    type=manifest.category,
                    metadata={
                        "sha256": manifest.sha256,
                        "bytes": manifest.bytes,
                        "media_type": manifest.media_type,
                        "source_object_id": manifest.source_object_id,
                    },
                )
                artifact.add_file(
                    local_path=str(source),
                    name=manifest.logical_name,
                )
                logged = run.log_artifact(artifact)
                wait = getattr(logged, "wait", None)
                if not callable(wait):
                    raise TypeError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
                wait()
                _verify_downloaded_wandb_artifact(logged, manifest)
                artifact_id = str(getattr(logged, "id", "") or "")
                if not artifact_id:
                    raise RuntimeError("WANDB_ARTIFACT_ID_UNAVAILABLE")
                artifact_ids.append(artifact_id)
                artifact_projection.append(
                    {
                        "local_artifact_id": manifest.artifact_id,
                        "provider_artifact_id": artifact_id,
                        "provider_artifact_ref": _logged_wandb_artifact_ref(logged, binding),
                        "sha256": manifest.sha256,
                    }
                )
            run.summary["output_artifact_ids"] = artifact_ids
            run.summary["artifact_projection"] = artifact_projection
            run_id = str(run.id)
            url = str(getattr(run, "url", "") or "")
            if not url:
                url = (
                    f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                    f"{binding.external_project}/runs/{run_id}"
                )
        finally:
            run.finish()
        return WandbUploadReceipt(run_id=run_id, url=url, artifact_ids=artifact_ids)

    def roundtrip_probe(
        self,
        binding: ObservabilityBinding,
        payload: bytes,
        expected_sha256: str,
    ) -> WandbRunReference:
        """Env-gated CI probe using the same safe SDK settings as evidence sync."""

        with (
            self.credentials.environment(),
            tempfile.TemporaryDirectory(prefix="gi-wandb-probe-env-") as temporary,
            _wandb_environment(Path(temporary)),
        ):
            verified = self._validate_binding_online(binding)
            return self._roundtrip_probe_online(verified, payload, expected_sha256)

    def _roundtrip_probe_online(
        self,
        binding: ObservabilityBinding,
        payload: bytes,
        expected_sha256: str,
    ) -> WandbRunReference:

        if len(payload) > MAX_PROBE_BYTES:
            raise ValueError("integration probe payload exceeds the safe limit")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256 or _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("integration probe payload hash mismatch")
        wandb = self._sdk()
        probe_id = f"gi-probe-{expected_sha256[:16]}"
        with tempfile.TemporaryDirectory(prefix="gi-wandb-probe-") as temporary:
            root = Path(temporary)
            source = root / "probe.bin"
            source.write_bytes(payload)
            run = wandb.init(
                entity=binding.entity,
                project=binding.external_project,
                id=hashlib.sha256(
                    f"gi-probe|{binding.entity}|{binding.external_project}|{expected_sha256}".encode()
                ).hexdigest()[:24],
                resume="allow",
                job_type="integration-probe",
                settings=wandb.Settings(
                    console="off",
                    disable_code=True,
                    disable_git=True,
                    silent=True,
                    save_code=False,
                    x_disable_machine_info=True,
                    x_disable_meta=True,
                    x_disable_stats=True,
                    x_save_requirements=False,
                ),
                reinit=True,
            )
            if run is None:  # pragma: no cover
                raise RuntimeError("WANDB_PROBE_RUN_CREATION_FAILED")
            try:
                artifact = wandb.Artifact(
                    name=probe_id,
                    type="integration-probe",
                    metadata={"sha256": expected_sha256, "bytes": len(payload)},
                )
                artifact.add_file(str(source), name="probe.bin")
                logged = run.log_artifact(artifact)
                wait = getattr(logged, "wait", None)
                if not callable(wait):
                    raise TypeError("WANDB_ARTIFACT_WAIT_UNAVAILABLE")
                wait()
                download = getattr(logged, "download", None)
                if not callable(download):
                    raise TypeError("WANDB_ARTIFACT_DOWNLOAD_UNAVAILABLE")
                downloaded = download(root=str(root / "download"), skip_cache=True)
                target = Path(downloaded) / "probe.bin"
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256
                ):
                    raise RuntimeError("WANDB_PROBE_HASH_MISMATCH")
                artifact_id = str(getattr(logged, "id", "") or "")
                if not artifact_id:
                    raise RuntimeError("WANDB_ARTIFACT_ID_UNAVAILABLE")
                run_id = str(run.id)
                run_url = str(getattr(run, "url", "") or "")
            finally:
                run.finish()
        return WandbRunReference(
            project_id=binding.project_id,
            envelope_id=f"probe:{expected_sha256}",
            entity=binding.entity,
            external_project=binding.external_project,
            run_id=run_id,
            url=run_url
            or (
                f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                f"{binding.external_project}/runs/{run_id}"
            ),
            artifact_ids=[artifact_id],
        )


class WeaveAdapter:
    def __init__(
        self,
        credentials: CredentialSession | None = None,
        *,
        preinitialized_client: Any | None = None,
    ) -> None:
        self.credentials = credentials or CredentialSession()
        self.preinitialized_client = preinitialized_client
        self._resolved_client: Any | None = preinitialized_client
        self._client_lock = threading.RLock()

    def _resolve_client(self, weave: Any, binding: ObservabilityBinding) -> Any:
        """Return one project-pinned client without reinitializing Weave.

        ``weave.init`` replaces and finishes the process-global client. That is
        unsafe while an EvaluationLogger or another trace still has pending
        work. Campaign observers therefore inject their process-owned client;
        ordinary worker use lazily initializes once and then reuses it.
        """

        expected = f"{binding.entity}/{binding.external_project}"
        with self._client_lock:
            current = weave.get_client()
            client = self._resolved_client
            if client is None:
                if current is None:
                    client = weave.init(
                        expected,
                        settings={"redact_pii": False},
                    )
                else:
                    client = current
                self._resolved_client = client
            current_changed = current is not None and current is not client
            project_changed = str(getattr(client, "project_id", "")) != expected
            if current_changed or project_changed:
                code = (
                    "WEAVE_PREINITIALIZED_CLIENT_MISMATCH"
                    if self.preinitialized_client is not None
                    else "WEAVE_CLIENT_CHANGED"
                )
                raise RuntimeError(code)
            # ``weave.init`` made this client current during the branch above.
            if weave.get_client() is not client:
                raise RuntimeError("WEAVE_CLIENT_CHANGED")
            return client

    def trace_call(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference:
        if binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_REQUIRES_ONLINE_WEAVE")
        if not self.credentials.configured:
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        safe_inputs = _prepare_trace_value(inputs, context="assistant trace inputs")
        safe_output = _prepare_trace_value(output, context="assistant trace output")
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc
        with self.credentials.environment():
            client = self._resolve_client(weave, binding)
            _configure_weave_sensitive_key_redaction()
            call = client.create_call(
                op=f"gi_scenario_{operation}",
                inputs=safe_inputs,
                attributes={"gi_call_id": call_id, "schema_version": "2.0"},
            )
            client.finish_call(call, output=safe_output)
            trace_id = str(getattr(call, "trace_id", "") or "")
            provider_call_id = str(getattr(call, "id", "") or "")
            if not trace_id or not provider_call_id:
                raise RuntimeError("WEAVE_TRACE_ID_UNAVAILABLE")
            client.flush()
            acknowledged_call = client.get_call(provider_call_id)
            if acknowledged_call is None:
                raise RuntimeError("WEAVE_TRACE_NOT_ACKNOWLEDGED")
            provider_url = str(
                getattr(acknowledged_call, "ui_url", "") or getattr(call, "ui_url", "") or ""
            )
        return WeaveTraceReference(
            project_id=binding.project_id,
            call_id=call_id,
            entity=binding.entity,
            external_project=binding.external_project,
            trace_id=trace_id,
            provider_call_id=provider_call_id,
            url=provider_url
            or (
                f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                f"{binding.external_project}/weave/r/call/{provider_call_id}"
            ),
        )

    def trace_scientific_operation(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        wandb_run_id: str | None = None,
        wandb_step: int | None = None,
    ) -> WeaveTraceReference:
        """Record one bounded scientific operation as a regular Weave call.

        These calls intentionally use ``create_call`` rather than the Agents
        conversation APIs.  A caller can provide a W&B run ID so Weave records
        the native run association without relying on process-global
        ``wandb.run`` state.
        """

        if operation not in _SCIENTIFIC_OPERATIONS:
            raise ValueError(f"unsupported scientific operation: {operation}")
        if binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("SCIENTIFIC_TRACE_REQUIRES_ONLINE_WEAVE")
        if not self.credentials.configured:
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        if wandb_step is not None and (wandb_run_id is None or wandb_step < 0):
            raise ValueError("a W&B step requires a run ID and must be nonnegative")
        safe_inputs = _prepare_trace_value(inputs, context="scientific trace inputs")
        safe_output = _prepare_trace_value(output, context="scientific trace output")
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc

        with self.credentials.environment():
            client = self._resolve_client(weave, binding)
            _configure_weave_sensitive_key_redaction()
            context_set = wandb_run_id is not None
            try:
                if context_set:
                    client.set_wandb_run_context(wandb_run_id, step=wandb_step)
                call = client.create_call(
                    op=f"gi.scientific.{operation}",
                    inputs=safe_inputs,
                    attributes={
                        "gi.call_id": call_id,
                        "gi.trace_kind": "scientific_operation",
                        "schema_version": "2.0",
                        "wandb_run_linked": context_set,
                    },
                )
                client.finish_call(call, output=safe_output)
                trace_id = str(getattr(call, "trace_id", "") or "")
                provider_call_id = str(getattr(call, "id", "") or "")
                if not trace_id or not provider_call_id:
                    raise RuntimeError("WEAVE_TRACE_ID_UNAVAILABLE")
                client.flush()
                acknowledged_call = client.get_call(provider_call_id)
                if acknowledged_call is None:
                    raise RuntimeError("WEAVE_TRACE_NOT_ACKNOWLEDGED")
                provider_url = str(
                    getattr(acknowledged_call, "ui_url", "") or getattr(call, "ui_url", "") or ""
                )
            finally:
                if context_set:
                    client.clear_wandb_run_context()
        return WeaveTraceReference(
            project_id=binding.project_id,
            call_id=call_id,
            entity=binding.entity,
            external_project=binding.external_project,
            trace_id=trace_id,
            provider_call_id=provider_call_id,
            url=provider_url
            or (
                f"{binding.web_base_url.rstrip('/')}/{binding.entity}/"
                f"{binding.external_project}/weave/r/call/{provider_call_id}"
            ),
        )

    @contextmanager
    def agent_turn(
        self,
        binding: ObservabilityBinding,
        *,
        conversation_id: str,
        conversation_name: str,
        question: str,
        model: str,
        include_content: bool,
        attributes: dict[str, Any],
    ):
        """Trace one genuine Evidence Guide turn and require cloud acknowledgement."""

        if binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_REQUIRES_ONLINE_WEAVE")
        if not self.credentials.configured:
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        safe_attributes = _prepare_trace_value(
            attributes,
            context="assistant agent attributes",
        )
        safe_conversation_name = _prepare_trace_value(
            conversation_name,
            context="assistant conversation name",
        )
        safe_question = (
            _prepare_trace_value(question, context="assistant question")
            if include_content
            else "[content omitted by project policy]"
        )
        agent_version = "evidence-guide-v2"
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc

        with self.credentials.environment():
            client = self._resolve_client(weave, binding)
            _configure_weave_sensitive_key_redaction()
            with (
                weave.start_conversation(
                    agent_name="gi-evidence-guide",
                    model=model,
                    conversation_id=conversation_id,
                    conversation_name=safe_conversation_name,
                    include_content=include_content,
                    attributes={"schema_version": "2.0", **safe_attributes},
                ) as conversation,
                conversation.start_turn(
                    user_message=safe_question,
                    model=model,
                    agent_name="gi-evidence-guide",
                    agent_id="gi-evidence-guide",
                    agent_description="Read-only guide to repository-owned engineering evidence",
                    agent_version=agent_version,
                    system_instructions=[
                        "Explain only the supplied engineering evidence.",
                        "Never select, approve, attest, or modify an alternative.",
                    ],
                ) as turn,
            ):
                recorder = WeaveAgentTurnRecorder(
                    weave=weave,
                    client=client,
                    turn=turn,
                    include_content=include_content,
                )
                yield recorder

            _flush_agent_spans()
            acknowledgement_error: RuntimeError | None = None
            for attempt in range(3):
                try:
                    acknowledged = client.get_agent_turn(trace_id=recorder.trace_id)
                    _validate_agent_turn_acknowledgement(
                        acknowledged,
                        trace_id=recorder.trace_id,
                        conversation_id=conversation_id,
                        agent_name="gi-evidence-guide",
                        agent_version=agent_version,
                        expected_error=recorder.expected_turn_error,
                        required_message_types=recorder.required_message_types,
                    )
                except Exception as exc:  # noqa: BLE001 - normalized and never persisted
                    acknowledgement_error = RuntimeError(
                        _safe_error_code(exc, prefix="WEAVE_AGENT_ACKNOWLEDGEMENT")
                    )
                else:
                    acknowledgement_error = None
                    break
                if attempt < 2:
                    time.sleep(0.15)
            if acknowledgement_error is not None:
                raise acknowledgement_error

            recorder.receipt = WeaveAgentTurnReceipt(
                conversation_id=conversation_id,
                trace_id=recorder.trace_id,
                root_span_id=recorder.root_span_id,
                url=_agent_conversation_url(binding, conversation_id),
            )

    def add_agent_feedback(
        self,
        binding: ObservabilityBinding,
        *,
        trace_id: str,
        feedback: str,
        notes: str | None,
    ) -> None:
        if feedback not in {"helpful", "not_helpful", "incorrect", "unsafe"}:
            raise ValueError("unsupported Evidence Guide feedback value")
        if notes is not None and len(notes) > 1_024:
            raise ValueError("assistant feedback notes exceed 1,024 characters")
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc
        with self.credentials.environment():
            client = self._resolve_client(weave, binding)
            _configure_weave_sensitive_key_redaction()
            target = client.get_agent_turn_feedback(trace_id)
            reaction = "👍" if feedback == "helpful" else "👎"
            created_feedback_ids = [
                target.add_reaction(reaction),
                target.add(
                    "gi.evidence_guide.feedback",
                    {"label": feedback},
                ),
            ]
            if notes:
                created_feedback_ids.append(
                    target.add_note(_prepare_trace_value(notes, context="assistant feedback"))
                )
            if any(
                not isinstance(feedback_id, str) or not feedback_id.strip()
                for feedback_id in created_feedback_ids
            ):
                raise RuntimeError("WEAVE_FEEDBACK_ID_UNAVAILABLE")
            feedback_ids = set(created_feedback_ids)
            if len(feedback_ids) != len(created_feedback_ids):
                raise RuntimeError("WEAVE_FEEDBACK_ID_DUPLICATE")
            client.flush()
            refresh = getattr(target, "refresh", None)
            if not callable(refresh):
                raise RuntimeError(  # noqa: TRY004 - normalized SDK capability failure
                    "WEAVE_FEEDBACK_READBACK_UNAVAILABLE"
                )
            acknowledged_ids: set[str] = set()
            for attempt in range(3):
                try:
                    readback = refresh()
                    items = getattr(readback, "items", readback)
                    acknowledged_ids = set()
                    for item in items:
                        _, item_id = _acknowledgement_field(item, "id")
                        trace_present, item_trace_id = _acknowledgement_field(item, "span_trace_id")
                        if trace_present and item_trace_id and str(item_trace_id) != trace_id:
                            continue
                        if item_id:
                            acknowledged_ids.add(str(item_id))
                except Exception:  # noqa: BLE001 - normalized at the service boundary
                    acknowledged_ids = set()
                if feedback_ids.issubset(acknowledged_ids):
                    break
                if attempt < 2:
                    time.sleep(0.15)
            if not feedback_ids.issubset(acknowledged_ids):
                raise RuntimeError("WEAVE_FEEDBACK_NOT_ACKNOWLEDGED")


def _resolve_exact_project_policy(
    api: Any,
    entity: str,
    project_name: str,
) -> dict[str, str] | None:
    """Resolve exact project identity and access without treating PRIVATE as Restricted.

    Current W&B SDK project objects can expose the legacy value ``PRIVATE`` for
    both Team and Restricted projects.  Prefer a supported exact field when it
    becomes available; until then isolate the same narrow GraphQL policy query
    used by the publication workflow.  Any schema or identity drift fails closed.
    """

    try:
        project = api.project(project_name, entity=entity)
    except Exception as exc:
        raise RuntimeError("WANDB_PROJECT_NOT_ACCESSIBLE") from exc
    project_id = str(getattr(project, "id", "") or "")
    supported_access = str(
        getattr(project, "visibility_scope", None)
        or getattr(project, "visibility", None)
        or getattr(project, "access_level", None)
        or getattr(project, "access", "")
    )
    if supported_access.casefold() == "restricted" and project_id:
        return {
            "id": project_id,
            "name": project_name,
            "entityName": entity,
            "access": "RESTRICTED",
        }

    service = getattr(api, "_service_api", None)
    execute = getattr(service, "execute_graphql", None)
    if not callable(execute):
        raise ValueError(  # noqa: TRY004 - normalized policy capability failure
            "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"
        )
    try:
        response = execute(PROJECT_POLICY_GQL, {"name": project_name, "entity": entity})
    except Exception as exc:
        raise ValueError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE") from exc
    record = response.get("project") if isinstance(response, dict) else None
    if record is None:
        return None
    required = ("id", "name", "entityName", "access")
    if not isinstance(record, dict) or any(not record.get(field) for field in required):
        raise ValueError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
    if str(record["name"]) != project_name or str(record["entityName"]) != entity:
        raise RuntimeError("WANDB_PROJECT_ID_CHANGED")
    if project_id and str(record["id"]) != project_id:
        raise RuntimeError("WANDB_PROJECT_ID_CHANGED")
    return {field: str(record[field]) for field in required}


def _active_turn_ids(turn: Any) -> tuple[str, str]:
    span = getattr(turn, "_otel_span", None)
    context = span.get_span_context() if span is not None else None
    trace_id = int(getattr(context, "trace_id", 0) or 0)
    span_id = int(getattr(context, "span_id", 0) or 0)
    if not trace_id or not span_id:
        raise RuntimeError("WEAVE_AGENT_SPAN_ID_UNAVAILABLE")
    return format(trace_id, "032x"), format(span_id, "016x")


def _flush_agent_spans() -> None:
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("WEAVE_AGENT_TRACING_UNAVAILABLE") from exc
    provider = otel_trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if not callable(force_flush) or force_flush(timeout_millis=5_000) is False:
        raise RuntimeError("WEAVE_TRACE_FLUSH_FAILED")


def _acknowledgement_field(value: Any, field: str) -> tuple[bool, Any]:
    """Read an acknowledgement field without depending on one SDK model type."""

    if isinstance(value, dict):
        return field in value, value.get(field)
    if hasattr(value, field):
        return True, getattr(value, field)
    return False, None


def _validate_agent_turn_acknowledgement(
    acknowledgement: Any,
    *,
    trace_id: str,
    conversation_id: str,
    agent_name: str,
    agent_version: str,
    expected_error: bool = False,
    required_message_types: set[str] | frozenset[str] | None = None,
) -> None:
    """Fail closed when the installed Agents API contradicts the emitted turn.

    Weave 0.53 exposes the trace, root agent identity, version, completion
    status, duration, and chat messages.  It does not expose the conversation
    ID on ``AgentTraceChatRes`` today, so that field is checked when a later SDK
    supplies it rather than inferred from the requested URL.
    """

    if acknowledgement is None:
        raise RuntimeError("WEAVE_TRACE_NOT_ACKNOWLEDGED")
    present, acknowledged_trace_id = _acknowledgement_field(acknowledgement, "trace_id")
    if not present or str(acknowledged_trace_id or "") != trace_id:
        raise RuntimeError("WEAVE_AGENT_TRACE_ID_MISMATCH")

    for field, expected, code in (
        ("conversation_id", conversation_id, "WEAVE_AGENT_CONVERSATION_ID_MISMATCH"),
        ("agent_name", agent_name, "WEAVE_AGENT_NAME_MISMATCH"),
        ("agent_version", agent_version, "WEAVE_AGENT_VERSION_MISMATCH"),
        ("root_span_name", agent_name, "WEAVE_AGENT_ROOT_SPAN_MISMATCH"),
    ):
        field_present, actual = _acknowledgement_field(acknowledgement, field)
        if field_present and str(actual or "") != expected:
            raise RuntimeError(code)

    status_present, status = _acknowledgement_field(acknowledgement, "status_code")
    if status_present:
        if expected_error and status != "ERROR":
            raise RuntimeError("WEAVE_AGENT_TURN_STATUS_MISMATCH")
        if not expected_error and status not in {"OK", "UNSET"}:
            raise RuntimeError("WEAVE_AGENT_TURN_FAILED")
    duration_present, duration = _acknowledgement_field(acknowledgement, "total_duration_ms")
    if duration_present and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise RuntimeError("WEAVE_AGENT_TURN_INCOMPLETE")
    messages_present, messages = _acknowledgement_field(acknowledgement, "messages")
    if messages_present and (not isinstance(messages, list) or not messages):
        raise RuntimeError("WEAVE_AGENT_TURN_INCOMPLETE")
    if messages_present:
        message_types = {
            str(_acknowledgement_field(message, "type")[1] or "") for message in messages
        }
        expected_message_types = (
            set(required_message_types)
            if required_message_types is not None
            else (
                {"user_message"}
                if expected_error
                else {"user_message", "tool_call", "assistant_message"}
            )
        )
        if "user_message" not in expected_message_types:
            raise ValueError("agent acknowledgement must require the user message")
        if not expected_message_types.issubset(message_types):
            raise RuntimeError("WEAVE_AGENT_TURN_INCOMPLETE")
        if not expected_error and any(
            _acknowledgement_field(message, "status_code")[1] == "ERROR" for message in messages
        ):
            raise RuntimeError("WEAVE_AGENT_TURN_FAILED")


def _agent_conversation_url(
    binding: ObservabilityBinding,
    conversation_id: str,
) -> str:
    """Build an Agents deep link from the policy-verified W&B web origin."""

    _validate_destination(binding)
    if not conversation_id.strip():
        raise ValueError("Weave conversation identifier cannot be empty")
    parsed = urllib.parse.urlsplit(binding.web_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("WANDB_WEB_ORIGIN_INVALID")
    base_path = parsed.path.rstrip("/")
    path = "/".join(
        (
            base_path,
            urllib.parse.quote(binding.entity, safe=""),
            urllib.parse.quote(binding.external_project, safe=""),
            "weave/agents/conversations",
            urllib.parse.quote(conversation_id, safe=""),
        )
    )
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class ObservabilityService:
    def __init__(
        self,
        *,
        wandb_adapter: WandbEvidenceAdapter | None = None,
        weave_adapter: WeaveAssistantAdapter | None = None,
    ) -> None:
        self.credentials = CredentialSession()
        self.wandb = wandb_adapter or WandbAdapter(self.credentials)
        self.weave = weave_adapter or WeaveAdapter(self.credentials)

    def initialize_credentials(self, api_key: str, base_url: str) -> None:
        self.credentials.configure(api_key, base_url)

    @contextmanager
    def candidate_credentials(self, api_key: str, base_url: str):
        with self.credentials.candidate(api_key, base_url):
            yield

    def clear_credentials(self) -> None:
        self.credentials.clear()

    def validate_binding(
        self,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
    ) -> ObservabilityBinding:
        validated_policy = ProjectDataPolicy.model_validate(policy)
        candidate = ObservabilityBinding.model_validate(binding)
        if candidate.project_id != validated_policy.project_id:
            raise ValueError("W&B binding and data policy must identify the same GI project")
        return self.wandb.validate_binding(candidate, validated_policy)

    def connect(
        self,
        repository: ProjectRepository,
        *,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
    ) -> CloudSyncState:
        repository.get_project(ProjectDataPolicy.model_validate(policy).project_id)
        validated = self.validate_binding(policy, binding)
        repository.upsert_project_data_policy(policy)
        repository.connect_observability_binding(validated)
        return self.status(repository, validated.project_id)

    def status(self, repository: ProjectRepository, project_id: str) -> CloudSyncState:
        repository.get_project(project_id)
        state = repository.observability_status(project_id)
        binding = state["binding"]
        counts = state["outbox_counts"]
        if binding is None:
            connection_state = "LOCAL_ONLY"
        elif not binding.get("verified") or not self.credentials.configured:
            connection_state = "AUTHENTICATION_REQUIRED"
        elif binding.get("mode") == ObservabilityMode.OFFLINE_QUEUE.value:
            connection_state = "SYNC_QUEUED_OFFLINE"
        elif counts.get("uploading", 0) or counts.get("pending", 0):
            connection_state = "SYNCING"
        elif counts.get("failed", 0):
            connection_state = "FAILED_RETRYING"
        else:
            connection_state = "CONNECTED"
        online = bool(
            binding
            and binding.get("mode") == ObservabilityMode.ONLINE.value
            and self.credentials.configured
        )
        verified = bool(binding and binding.get("verified"))
        return CloudSyncState(
            project_id=project_id,
            state=connection_state,
            binding_required=False,
            binding=binding,
            can_enter_home=True,
            can_open_project=True,
            can_run_local=True,
            can_sync=verified and online,
            can_use_assistant=verified and online and self.credentials.configured,
            online=online,
            outbox_counts=counts,
            pending_artifact_bytes=state["pending_artifact_bytes"],
            last_synced_at=state["last_synced_at"],
            last_error_code=state["last_error_code"],
        )

    def preview_upload(
        self,
        repository: ProjectRepository,
        envelope: TelemetryEnvelope | dict[str, Any],
    ) -> dict[str, Any]:
        validated = TelemetryEnvelope.model_validate(envelope)
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(validated.project_id)
        )
        policy = ProjectDataPolicy.model_validate(
            repository.get_project_data_policy(validated.project_id)
        )
        blockers = _validate_envelope(repository, validated, binding, policy)
        return {
            "project_id": validated.project_id,
            "envelope_id": validated.envelope_id,
            "destination": f"{binding.entity}/{binding.external_project}",
            "access": binding.access,
            "deployment": binding.deployment,
            "category": validated.category,
            "artifact_categories": sorted({item.category for item in validated.artifacts}),
            "artifact_count": len(validated.artifacts),
            "artifact_bytes": sum(item.bytes for item in validated.artifacts),
            "metadata_fields": sorted(validated.metadata),
            "excluded_categories": policy.prohibited_categories,
            "payload_sha256": _envelope_sha256(validated),
            "blockers": blockers,
            "local_authority": True,
        }

    def sync(
        self,
        repository: ProjectRepository,
        *,
        envelope: TelemetryEnvelope | dict[str, Any],
        preview_sha256: str,
    ) -> dict[str, Any]:
        validated = TelemetryEnvelope.model_validate(envelope)
        preview = self.preview_upload(repository, validated)
        if preview["blockers"]:
            raise ValueError("cloud evidence upload is blocked: " + "; ".join(preview["blockers"]))
        if preview_sha256 != preview["payload_sha256"]:
            raise ValueError("cloud evidence changed after upload preview")
        queued = repository.enqueue_telemetry_envelope(validated)
        if queued["state"] == TelemetryState.SYNCED.value:
            return _sync_payload(queued, repository.get_wandb_run_reference(validated.envelope_id))
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(validated.project_id)
        )
        if binding.mode == ObservabilityMode.OFFLINE_QUEUE:
            return _sync_payload(queued, None)
        claimed = repository.claim_telemetry_outbox_item(queued["outbox_id"])
        paths = {
            manifest.artifact_id: repository.root / "blobs" / "sha256" / manifest.local_blob_sha256
            for manifest in validated.artifacts
        }
        try:
            receipt = self.wandb.upload(validated, binding, paths, repository.root)
            reference = repository.add_wandb_run_reference(
                WandbRunReference(
                    project_id=validated.project_id,
                    envelope_id=validated.envelope_id,
                    entity=binding.entity,
                    external_project=binding.external_project,
                    run_id=receipt.run_id,
                    url=receipt.url,
                    artifact_ids=receipt.artifact_ids,
                )
            )
            completed = repository.mark_telemetry_outbox_item(
                claimed["outbox_id"], TelemetryState.SYNCED
            )
            repository.record_first_cloud_upload(validated.project_id)
            return _sync_payload(completed, reference)
        except Exception as exc:  # noqa: BLE001 - normalized; no SDK error text persisted
            code = _safe_error_code(exc, prefix="WANDB_UPLOAD")
            failed = repository.mark_telemetry_outbox_item(
                claimed["outbox_id"], TelemetryState.FAILED, error_code=code
            )
            return _sync_payload(failed, None)

    def retry(self, repository: ProjectRepository, outbox_id: str) -> dict[str, Any]:
        current = repository.get_telemetry_outbox_item(outbox_id)
        if current["state"] == TelemetryState.SYNCED.value:
            return _sync_payload(
                current,
                repository.get_wandb_run_reference(current["envelope"]["envelope_id"]),
            )
        repository.retry_telemetry_outbox_item(outbox_id)
        envelope = TelemetryEnvelope.model_validate(current["envelope"])
        return self.sync(
            repository,
            envelope=envelope,
            preview_sha256=_envelope_sha256(envelope),
        )

    def trace_assistant_call(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        call_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> WeaveTraceReference:
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(project_id)
        )
        if not binding.verified or binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_DISABLED_WEAVE_UNAVAILABLE")
        policy = ProjectDataPolicy.model_validate(repository.get_project_data_policy(project_id))
        binding = self.validate_binding(policy, binding)
        trace = self.weave.trace_call(
            binding,
            call_id=call_id,
            operation=operation,
            inputs=inputs,
            output=output,
        )
        repository.add_weave_trace_reference(trace)
        return trace

    def trace_scientific_operation(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        analysis_id: str,
        operation: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        wandb_step: int | None = None,
    ) -> dict[str, Any]:
        """Best-effort trace of a bounded scientific operation.

        This boundary deliberately absorbs every cloud-side error.  Exact
        results, candidate order, adverse-effect findings, and local state are
        already committed before the observer is called and can never be
        changed by Weave availability.
        """

        try:
            if operation not in _SCIENTIFIC_OPERATIONS:
                raise ValueError(f"unsupported scientific operation: {operation}")
            analysis = repository.get_analysis(analysis_id)
            if analysis["project_id"] != project_id:
                raise ValueError("scientific trace analysis belongs to another project")
            binding = ObservabilityBinding.model_validate(
                repository.get_observability_binding(project_id)
            )
            policy = ProjectDataPolicy.model_validate(
                repository.get_project_data_policy(project_id)
            )
            if (
                not binding.verified
                or binding.mode != ObservabilityMode.ONLINE
                or binding.access != "Restricted"
                or binding.data_policy_id != policy.policy_id
                or binding.deployment != policy.deployment
            ):
                return {
                    "status": "skipped",
                    "reason": "verified_online_restricted_binding_unavailable",
                }
            try:
                binding = self.validate_binding(policy, binding)
            except Exception:  # noqa: BLE001 - cloud policy recheck is best effort
                return {
                    "status": "skipped",
                    "reason": "exact_restricted_policy_revalidation_failed",
                }
            if (
                not binding.verified
                or binding.mode != ObservabilityMode.ONLINE
                or binding.access != "Restricted"
                or binding.data_policy_id != policy.policy_id
                or binding.deployment != policy.deployment
            ):
                return {
                    "status": "skipped",
                    "reason": "exact_restricted_policy_revalidation_failed",
                }
            trace_method = getattr(self.weave, "trace_scientific_operation", None)
            if not callable(trace_method):
                return {"status": "skipped", "reason": "adapter_capability_unavailable"}

            safe_inputs = {
                "project_key": _opaque_subject_key("project", project_id),
                "analysis_key": _opaque_subject_key("analysis", analysis_id),
                **inputs,
            }
            safe_output = dict(output)
            _validate_cloud_value(safe_inputs, context="scientific trace inputs")
            _validate_cloud_value(safe_output, context="scientific trace output")
            call_digest = hashlib.sha256(
                _canonical_json(
                    {
                        "schema_version": "2.0",
                        "operation": operation,
                        "inputs": safe_inputs,
                        "output": safe_output,
                    }
                )
            ).hexdigest()
            call_id = f"scientific_{call_digest[:24]}"
            existing = repository.get_weave_trace_reference(project_id, call_id)
            if existing is not None:
                return {
                    "status": "reused",
                    "call_id": call_id,
                    "url": existing["url"],
                    "wandb_run_linked": bool(existing.get("provider_call_id")),
                }

            run_reference = repository.get_wandb_run_reference_for_analysis(analysis_id)
            if run_reference is None:
                wandb_run_id = _planned_analysis_wandb_run_id(project_id, analysis_id)
                run_link_state = "planned"
            else:
                wandb_run_id = str(run_reference["run_id"])
                run_link_state = "confirmed"
            trace = trace_method(
                binding,
                call_id=call_id,
                operation=operation,
                inputs=safe_inputs,
                output=safe_output,
                wandb_run_id=wandb_run_id,
                wandb_step=wandb_step,
            )
            stored = repository.add_weave_trace_reference(trace)
            return {
                "status": "traced",
                "call_id": call_id,
                "url": stored["url"],
                "wandb_run_id": wandb_run_id,
                "wandb_run_link_state": run_link_state,
            }
        except Exception as exc:  # noqa: BLE001 - observability cannot affect engineering
            return {
                "status": "failed",
                "error_code": _safe_error_code(exc, prefix="WEAVE_SCIENTIFIC_TRACE"),
            }

    @contextmanager
    def trace_evidence_guide_turn(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        conversation_id: str,
        conversation_name: str,
        question: str,
        model: str,
        include_content: bool,
        attributes: dict[str, Any],
    ):
        binding = self.validated_assistant_binding(repository, project_id)
        agent_turn = getattr(self.weave, "agent_turn", None)
        if not callable(agent_turn):
            raise RuntimeError(  # noqa: TRY004 - normalized adapter capability failure
                "WEAVE_AGENT_TRACING_UNAVAILABLE"
            )
        with agent_turn(
            binding,
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            question=question,
            model=model,
            include_content=include_content,
            attributes=attributes,
        ) as recorder:
            yield recorder

    def add_evidence_guide_feedback(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        trace_id: str,
        feedback: str,
        notes: str | None,
    ) -> None:
        try:
            binding = self.validated_assistant_binding(repository, project_id)
            add_feedback = getattr(self.weave, "add_agent_feedback", None)
            if not callable(add_feedback):
                raise RuntimeError(  # noqa: TRY004 - normalized adapter capability failure
                    "WEAVE_AGENT_FEEDBACK_UNAVAILABLE"
                )
            add_feedback(
                binding,
                trace_id=trace_id,
                feedback=feedback,
                notes=notes,
            )
        except Exception as exc:
            raise RuntimeError(_safe_error_code(exc, prefix="WEAVE_AGENT_FEEDBACK")) from exc

    def validated_assistant_binding(
        self,
        repository: ProjectRepository,
        project_id: str,
    ) -> ObservabilityBinding:
        binding = ObservabilityBinding.model_validate(
            repository.get_observability_binding(project_id)
        )
        if not binding.verified or binding.mode != ObservabilityMode.ONLINE:
            raise RuntimeError("ASSISTANT_DISABLED_WEAVE_UNAVAILABLE")
        policy = ProjectDataPolicy.model_validate(repository.get_project_data_policy(project_id))
        if policy.classification == "critical_infrastructure":
            raise RuntimeError("ASSISTANT_DISABLED_CRITICAL_INFRASTRUCTURE")
        return self.validate_binding(policy, binding)

    def rebind(
        self,
        repository: ProjectRepository,
        *,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
        revision: ModelRevision | dict[str, Any],
    ) -> CloudSyncState:
        validated = self.validate_binding(policy, binding)
        repository.rebind_observability_binding(validated, revision)
        return self.status(repository, validated.project_id)


def _validate_destination(binding: ObservabilityBinding) -> None:
    for label, value in (("entity", binding.entity), ("project", binding.external_project)):
        if _DESTINATION.fullmatch(value) is None:
            raise ValueError(f"W&B {label} contains unsupported characters")
    if binding.access != "Restricted":
        raise ValueError("W&B project access must be Restricted")


def _validate_envelope(
    repository: ProjectRepository,
    envelope: TelemetryEnvelope,
    binding: ObservabilityBinding,
    policy: ProjectDataPolicy,
) -> list[str]:
    blockers: list[str] = []
    if not binding.verified:
        blockers.append("W&B project binding is not verified")
    if envelope.binding_id != binding.binding_id:
        blockers.append("evidence envelope does not use the active W&B binding")
    try:
        _validate_cloud_value(envelope.metadata, context="cloud evidence metadata")
    except ValueError as exc:
        blockers.append(str(exc))
    metadata_bytes = len(_canonical_json(envelope.metadata))
    if metadata_bytes > MAX_METADATA_BYTES:
        blockers.append("cloud evidence metadata exceeds the size limit")
    for manifest in envelope.artifacts:
        if manifest.project_id != envelope.project_id:
            blockers.append(f"artifact {manifest.artifact_id} belongs to another project")
            continue
        if manifest.category not in policy.permitted_categories:
            blockers.append(f"data policy does not permit {manifest.category} artifacts")
        path = repository.root / "blobs" / "sha256" / manifest.local_blob_sha256
        if not path.is_file():
            blockers.append(f"artifact {manifest.artifact_id} is missing from local storage")
        elif path.stat().st_size != manifest.bytes or _sha256_path(path) != manifest.sha256:
            blockers.append(f"artifact {manifest.artifact_id} does not match its local hash")
        elif manifest.media_type.startswith("text/") or manifest.logical_name.casefold().endswith(
            (".inp", ".rpt", ".json", ".csv", ".md", ".txt")
        ):
            text = path.read_text(encoding="utf-8", errors="replace")
            if _contains_local_absolute_path(text):
                blockers.append(
                    f"artifact {manifest.artifact_id} contains a local absolute path; materialize portable references first"
                )
    return sorted(set(blockers))


def _validate_cloud_value(value: Any, *, context: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if any(fragment in normalized for fragment in _PROHIBITED_KEYS):
                raise ValueError(f"{context} contains prohibited field {path}.{key}")
            _validate_cloud_value(item, context=context, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_cloud_value(item, context=context, path=f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{context} contains a non-finite number at {path}")
    elif isinstance(value, str) and _contains_local_absolute_path(value):
        raise ValueError(f"{context} contains a local absolute path at {path}")
    elif not isinstance(value, (str, int, float, bool, type(None), dict, list)):
        raise ValueError(f"{context} contains unsupported data at {path}")


def _prepare_trace_value(value: Any, *, context: str) -> Any:
    """Deterministically redact trace content without runtime NLP downloads.

    Evidence packets are allowlisted before reaching this layer.  This final
    boundary strips sensitive-key fields and common credential/PII forms,
    removes local paths, then applies the existing finite/portable-value
    validation.  It intentionally does not claim general-purpose PII
    detection.
    """

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            prepared: dict[str, Any] = {}
            for key, nested in item.items():
                key_text = str(key)
                normalized = "".join(
                    character for character in key_text.casefold() if character.isalnum()
                )
                if any(fragment in normalized for fragment in _PROHIBITED_KEYS):
                    safe_key = (
                        "redacted_field_"
                        + hashlib.sha256(key_text.encode("utf-8")).hexdigest()[:12]
                    )
                    prepared[safe_key] = _TRACE_REDACTED
                else:
                    prepared[key_text] = visit(nested)
            return prepared
        if isinstance(item, list):
            return [visit(nested) for nested in item]
        if isinstance(item, tuple):
            return [visit(nested) for nested in item]
        if isinstance(item, str):
            lines = []
            for line in item.splitlines() or [item]:
                if _contains_local_absolute_path(line):
                    lines.append(_TRACE_REDACTED)
                    continue
                redacted = _TRACE_EMAIL.sub("[redacted:email]", line)
                redacted = _TRACE_SSN.sub("[redacted:government-id]", redacted)
                redacted = _TRACE_BEARER.sub("[redacted:authorization]", redacted)
                redacted = _TRACE_SECRET_ASSIGNMENT.sub("[redacted:credential]", redacted)
                redacted = _TRACE_PHONE.sub("[redacted:phone]", redacted)
                lines.append(redacted)
            return "\n".join(lines)
        return item

    prepared = visit(value)
    _validate_cloud_value(prepared, context=context)
    return prepared


def _configure_weave_sensitive_key_redaction() -> None:
    """Keep Weave's key redaction while avoiding its optional Presidio model."""

    from weave.utils import sanitize  # type: ignore[import-not-found]

    for key in sorted(_PROHIBITED_KEYS):
        sanitize.add_redact_key(key)


def _contains_local_absolute_path(value: str) -> bool:
    return any(_ABSOLUTE_PATH.search(line.strip()) for line in value.splitlines())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _opaque_subject_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"gi-v2|{kind}|{value}".encode()).hexdigest()


def _wandb_run_id(project_id: str, envelope_id: str) -> str:
    return hashlib.sha256(f"gi-v2|{project_id}|{envelope_id}".encode()).hexdigest()[:24]


def exact_analysis_batch_envelope_id(analysis_id: str, batch_index: int) -> str:
    if batch_index < 0:
        raise ValueError("exact-analysis batch index must be nonnegative")
    stable = hashlib.sha256(f"{analysis_id}|exact-run-batch|{batch_index}".encode()).hexdigest()[
        :24
    ]
    return f"envelope_{stable}"


def _planned_analysis_wandb_run_id(project_id: str, analysis_id: str) -> str:
    """Return the uploader's deterministic first exact-batch run ID."""

    return _wandb_run_id(project_id, exact_analysis_batch_envelope_id(analysis_id, 0))


def _envelope_sha256(envelope: TelemetryEnvelope) -> str:
    return hashlib.sha256(_canonical_json(envelope.model_dump(mode="json"))).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wandb_input_artifact_refs(metadata: dict[str, Any]) -> list[str]:
    """Return explicit, provider-qualified upstream artifacts for W&B lineage.

    Engineering producers may place immutable W&B references in
    ``metadata.input_artifact_refs`` after a prior upload has been confirmed.
    Local paths and implicit ``latest`` aliases are deliberately not accepted:
    a run must name the exact artifact version it consumed.
    """

    supplied = metadata.get("input_artifact_refs", [])
    if supplied is None:
        return []
    if not isinstance(supplied, list):
        raise TypeError("input_artifact_refs must be an array of W&B artifact references")
    references: list[str] = []
    for item in supplied:
        if isinstance(item, str) and item.casefold().endswith(":latest"):
            raise ValueError("input artifact lineage must pin a W&B artifact version")
        if not isinstance(item, str) or _WANDB_ARTIFACT_REFERENCE.fullmatch(item) is None:
            raise ValueError(
                "input_artifact_refs must use a qualified, versioned W&B artifact reference"
            )
        if item not in references:
            references.append(item)
    return references


def _wandb_table_specs(metadata: dict[str, Any]) -> dict[str, dict[str, list[Any]]]:
    supplied = metadata.get("tables", {})
    if supplied is None:
        return {}
    if not isinstance(supplied, dict):
        raise TypeError("W&B tables metadata must be an object")
    result: dict[str, dict[str, list[Any]]] = {}
    for name, spec in supplied.items():
        if not isinstance(name, str) or _DESTINATION.fullmatch(name) is None:
            raise ValueError("W&B table names contain unsupported characters")
        if not isinstance(spec, dict):
            raise TypeError(f"W&B table {name!r} must be an object")
        columns = spec.get("columns")
        rows = spec.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)
        ):
            raise ValueError(f"W&B table {name!r} requires unique string columns")
        if not isinstance(rows, list) or len(rows) > 100_000:
            raise ValueError(f"W&B table {name!r} has an invalid row collection")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError(f"W&B table {name!r} row width does not match its columns")
        result[name] = {"columns": columns, "rows": rows}
    return result


def _artifact_relative_path(logical_name: str) -> PurePosixPath:
    relative = PurePosixPath(logical_name.replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("WANDB_ARTIFACT_LOGICAL_NAME_UNSAFE")
    return relative


def _logged_wandb_artifact_ref(logged: Any, binding: ObservabilityBinding) -> str:
    try:
        reference = str(getattr(logged, "qualified_name", "") or "")
    except Exception as exc:  # pragma: no cover - normalized provider boundary
        raise RuntimeError("WANDB_ARTIFACT_REFERENCE_UNAVAILABLE") from exc
    expected_prefix = f"{binding.entity}/{binding.external_project}/"
    if _WANDB_ARTIFACT_REFERENCE.fullmatch(reference) is None or not reference.startswith(
        expected_prefix
    ):
        raise RuntimeError("WANDB_ARTIFACT_REFERENCE_UNAVAILABLE")
    return reference


def _verify_downloaded_wandb_artifact(logged: Any, manifest: CloudArtifactManifest) -> None:
    """Download the exact logged version and compare it with the local manifest."""

    download = getattr(logged, "download", None)
    if not callable(download):
        raise TypeError("WANDB_ARTIFACT_DOWNLOAD_UNAVAILABLE")
    relative = _artifact_relative_path(manifest.logical_name)
    with tempfile.TemporaryDirectory(prefix="gi-wandb-verify-") as temporary:
        requested_root = Path(temporary).resolve()
        downloaded_value = download(root=str(requested_root), skip_cache=True)
        if downloaded_value is None:
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        downloaded_root = Path(downloaded_value).resolve()
        if not downloaded_root.is_dir():
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        target = downloaded_root.joinpath(*relative.parts).resolve()
        if (
            target == downloaded_root
            or downloaded_root not in target.parents
            or not target.is_file()
        ):
            raise RuntimeError("WANDB_ARTIFACT_DOWNLOAD_MISSING")
        if target.stat().st_size != manifest.bytes or _sha256_path(target) != manifest.sha256:
            raise RuntimeError("WANDB_ARTIFACT_HASH_MISMATCH")


def _finite_numeric_values(value: dict[str, Any], prefix: str = "") -> dict[str, float]:
    result: dict[str, float] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
        ):
            result[name] = float(item)
        elif isinstance(item, dict):
            result.update(_finite_numeric_values(item, name))
    return result


def _safe_error_code(error: Exception, *, prefix: str) -> str:
    message = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", message):
        return message
    return f"{prefix}_{type(error).__name__.upper()}"[:64]


def _sync_payload(item: dict[str, Any], reference: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "outbox_id": item["outbox_id"],
        "envelope_id": item["envelope"]["envelope_id"],
        "state": item["state"],
        "attempts": item["attempts"],
        "payload_sha256": item["payload_sha256"],
        "last_error_code": item.get("last_error_code"),
        "wandb_run": reference,
        "local_authority": True,
    }


def validate_binding_from_environment(
    *,
    entity: str,
    project: str,
    deployment: str = "wandb_cloud",
    project_id: str = "environment-integration",
) -> ObservabilityBinding:
    """Fail closed unless environment credentials resolve an exact Restricted scope."""

    policy = ProjectDataPolicy(
        project_id=project_id,
        classification="standard_engineering",
        deployment=deployment,
        acknowledged_by="ci",
    )
    candidate = ObservabilityBinding(
        project_id=project_id,
        data_policy_id=policy.policy_id,
        entity=entity,
        external_project=project,
        external_project_id="pending-validation",
        api_base_url=os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai"),
        deployment=deployment,
        mode="online",
    )
    return WandbAdapter().validate_binding(candidate, policy)


def validate_and_probe_from_environment(
    *,
    entity: str,
    project: str,
    deployment: str = "wandb_cloud",
) -> dict[str, Any]:
    """Validate Restricted access, then perform a tiny hash roundtrip for CI."""

    verified = validate_binding_from_environment(
        entity=entity,
        project=project,
        deployment=deployment,
        project_id="ci-integration-probe",
    )
    adapter = WandbAdapter()
    payload = b"GI Scenario Studio W&B integration probe v2\n"
    digest = hashlib.sha256(payload).hexdigest()
    reference = adapter.roundtrip_probe(verified, payload, digest)
    return {
        "verified": verified.verified,
        "access": verified.access,
        "entity": verified.entity,
        "project": verified.external_project,
        "external_project_id": verified.external_project_id,
        "probe_sha256": digest,
        "run_id": reference.run_id,
        "url": reference.url,
        "artifact_ids": reference.artifact_ids,
    }
