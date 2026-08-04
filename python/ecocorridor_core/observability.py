"""EcoCorridor-specific W&B and Weave observability wiring.

This module supplies names and trace behavior only.  It does not infer W&B
project visibility and it cannot turn upload permission into ecological data
authorization.  Callers must first produce exact :class:`ProjectScopeEvidence`
through a documented provider and an allowed cloud-sync plan.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from scenario_platform.cloud_sync import (
    DurableOutboxConsumer,
    InMemoryCredentialSession,
    LineageUploader,
    OutboxRepository,
    ProjectCloudDirectories,
    WandbSdkLineageUploader,
    WandbTarget,
)
from scenario_platform.observability import (
    AppProfile,
    ProjectScopeEvidence,
    ProjectScopeResolver,
    UnsupportedWandbCloudScopeProvider,
    redact_sensitive,
)

from .assistants import (
    AssistantRunAssociation,
    NestedTraceSink,
    TraceReadback,
    validate_feedback_comment,
)
from .telemetry import EcoTelemetryProjector

ECOCORRIDOR_APP_PROFILE = AppProfile(
    namespace="ecocorridor",
    api_version="1.0",
    schema_version="1.0",
    run_kinds=[
        "bundle_import",
        "planning_model",
        "connectivity_analysis",
        "guided_session",
        "search_benchmark",
        "assistant_evaluation",
        "evidence_export",
        "release_gate",
    ],
    artifact_types=[
        "connectivity_project",
        "data_asset",
        "scenario_set",
        "intervention_catalog",
        "baseline_runs",
        "direct_run_batch",
        "search_model",
        "search_trace",
        "decision_evidence",
        "evidence_package",
        "guided_fixture",
        "benchmark_dataset",
    ],
    registry_names={
        "guided": "wandb-registry-Datasets/ecocorridor-guided-fixture",
        "benchmarks": "wandb-registry-Datasets/ecocorridor-search-benchmarks",
        "proposer": "wandb-registry-Models/ecocorridor-search-proposer",
        "release": "wandb-registry-Release Evidence/ecocorridor-scenario-studio",
        "prompts": "wandb-registry-Prompts/ecocorridor-evidence-assistant",
    },
    keychain_service="org.ecocorridor.scenariostudio.credentials",
    app_data_root="EcoCorridorScenarioStudio",
)

_REFERENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
_SENSITIVE_WEAVE_KEYS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "environment",
    "local_path",
    "password",
    "private_key",
    "secret",
    "token",
)


def _allowed_target_url(url: str, target: WandbTarget) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(target.web_base_url)
    try:
        parsed_port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        expected_port = expected.port or (443 if expected.scheme.casefold() == "https" else 80)
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and expected.hostname
        and parsed.hostname.casefold() == expected.hostname.casefold()
        and parsed_port == expected_port
        and parsed.scheme.casefold() == expected.scheme.casefold()
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def protected_integration_scope_resolver(deployment: str) -> ProjectScopeResolver:
    """Return the reviewed exact-scope provider available in this release.

    W&B Multi-tenant Cloud currently has no supported exact visibility field,
    and this source tree does not yet contain a documented Dedicated or
    Self-Managed admin-endpoint adapter.  Both paths therefore fail closed.
    Adding a real provider requires reviewed source and tests; deployment
    variables cannot substitute for it.
    """

    normalized = deployment.casefold().replace("-", "_")
    if normalized in {"wandb_cloud", "cloud"}:
        return ProjectScopeResolver(UnsupportedWandbCloudScopeProvider())
    return ProjectScopeResolver()


class WeaveClientFactory(Protocol):
    def create(self, target: WandbTarget) -> Any: ...


class SdkWeaveClientFactory:
    """Load Weave only in a configured worker process."""

    def create(self, target: WandbTarget) -> Any:
        if not os.environ.get("WANDB_API_KEY"):
            raise RuntimeError("WANDB_CREDENTIAL_UNAVAILABLE")
        try:
            import weave  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional integration environment
            raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc
        client = weave.init(
            f"{target.entity}/{target.project}",
            settings={"redact_pii": True},
        )
        try:
            from weave.utils import sanitize  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - pinned SDK compatibility gate
            raise RuntimeError("WEAVE_REDACTION_API_UNAVAILABLE") from exc
        for key in _SENSITIVE_WEAVE_KEYS:
            sanitize.add_redact_key(key)
        return client


@dataclass(slots=True)
class _TraceSession:
    call_id: str
    client: Any
    root_call: Any
    association: AssistantRunAssociation
    steps: list[str] = field(default_factory=list)
    root_disposition: Literal["OPEN", "OUTCOME_UNKNOWN", "COMPLETED", "ABORTED"] = "OPEN"
    verification: Literal["NONE", "ACKNOWLEDGED", "VERIFIED", "FAILED"] = "NONE"
    trace_id: str | None = None
    completed_readback: TraceReadback | None = None
    aborted_trace: dict[str, Any] | None = None


class WeaveNestedTraceSink(NestedTraceSink):
    """Nested Weave calls with mandatory provider flush and readback.

    No assistant answer is released by :mod:`ecocorridor_core.assistants`
    until ``flush`` and ``readback`` return the completed root call and exact
    ordered child-step list.
    """

    def __init__(
        self,
        *,
        target: WandbTarget,
        scope_resolver: ProjectScopeResolver,
        project_root: str | Path,
        client_factory: WeaveClientFactory | None = None,
        credentials: InMemoryCredentialSession | None = None,
        profile: AppProfile = ECOCORRIDOR_APP_PROFILE,
    ) -> None:
        self.target = target
        self.scope_resolver = scope_resolver
        self.profile = profile
        self.directories = ProjectCloudDirectories.create(project_root, profile)
        self.client_factory = client_factory or SdkWeaveClientFactory()
        self.credentials = credentials
        self._sessions: dict[str, _TraceSession] = {}
        self._trace_to_call: dict[str, str] = {}

    def _verify_scope(self) -> ProjectScopeEvidence:
        try:
            with self.credentials.environment() if self.credentials is not None else nullcontext():
                evidence = self.scope_resolver.resolve(
                    entity=self.target.entity,
                    project=self.target.project,
                    deployment=self.target.deployment,
                    api_base_url=self.target.api_base_url,
                )
        except Exception as exc:
            raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE") from exc
        if evidence.external_project_id != self.target.external_project_id or evidence.model_dump(
            mode="json"
        ) != self.target.scope_evidence.model_dump(mode="json"):
            raise RuntimeError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")
        return evidence

    @staticmethod
    def _safe(value: dict[str, Any]) -> dict[str, Any]:
        minimized = redact_sensitive(value)
        if minimized != value:
            raise RuntimeError("WEAVE_CONTEXT_REDACTION_REQUIRED")
        return value

    @staticmethod
    def _set_run_context(client: Any, association: AssistantRunAssociation) -> None:
        set_run_context = getattr(client, "set_wandb_run_context", None)
        if not callable(set_run_context):
            raise TypeError("WEAVE_WANDB_RUN_CONTEXT_UNAVAILABLE")
        set_run_context(run_id=association.wandb_run_id)

    def start(self, *, call_id: str, operation: str, inputs: dict[str, Any]) -> None:
        if not call_id or call_id in self._sessions:
            raise ValueError("assistant trace call ID must be unique and non-empty")
        self._verify_scope()
        try:
            association = AssistantRunAssociation.model_validate(inputs.get("wandb_association"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("WEAVE_WANDB_RUN_ASSOCIATION_REQUIRED") from exc
        if association.wandb_external_project_id != self.target.external_project_id:
            raise RuntimeError("WEAVE_WANDB_PROJECT_ASSOCIATION_MISMATCH")
        with (
            self.credentials.environment() if self.credentials is not None else nullcontext(),
            self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
        ):
            client = self.client_factory.create(self.target)
            self._set_run_context(client, association)
            root = client.create_call(
                op=f"{self.profile.namespace}.{operation}",
                inputs=self._safe(inputs),
                attributes={
                    "app_namespace": self.profile.namespace,
                    "api_version": self.profile.api_version,
                    "schema_version": self.profile.schema_version,
                    "assistant_call_id": call_id,
                    "wandb_association_id": association.association_id,
                    "wandb_analysis_id": association.analysis_id,
                    "wandb_run_id": association.wandb_run_id,
                    "read_only": True,
                },
                use_stack=False,
            )
        root_id = str(getattr(root, "id", "") or "")
        root_trace_id = str(getattr(root, "trace_id", "") or "")
        if not root_id or not root_trace_id:
            raise RuntimeError("WEAVE_TRACE_ID_UNAVAILABLE")
        self._sessions[call_id] = _TraceSession(
            call_id=call_id,
            client=client,
            root_call=root,
            association=association,
        )

    def step(
        self,
        *,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        session = self._active(call_id)
        if not name or name in session.steps:
            raise ValueError("assistant trace steps must be non-empty and unique")
        with (
            self.credentials.environment() if self.credentials is not None else nullcontext(),
            self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
        ):
            self._set_run_context(session.client, session.association)
            child = session.client.create_call(
                op=f"{self.profile.namespace}.assistant.{name}",
                inputs=self._safe(inputs),
                parent=session.root_call,
                attributes={"assistant_call_id": call_id, "step_index": len(session.steps)},
                use_stack=False,
            )
            session.client.finish_call(child, output=self._safe(output))
        session.steps.append(name)

    def finish(self, *, call_id: str, output: dict[str, Any]) -> None:
        session = self._active(call_id)
        trace_output = {
            "assistant_output": self._safe(output),
            "ecocorridor_trace_gate": {
                "call_id": call_id,
                "completed": True,
                "steps": list(session.steps),
                "wandb_association": session.association.model_dump(mode="json"),
            },
        }
        session.root_disposition = "OUTCOME_UNKNOWN"
        try:
            with (
                self.credentials.environment() if self.credentials is not None else nullcontext(),
                self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
            ):
                session.client.finish_call(session.root_call, output=trace_output)
        except Exception:
            session.verification = "FAILED"
            raise
        session.root_disposition = "COMPLETED"

    def flush(self, *, call_id: str) -> str:
        session = self._session(call_id)
        if session.root_disposition != "COMPLETED":
            raise RuntimeError("WEAVE_TRACE_NOT_FINISHED")
        if session.verification == "FAILED":
            raise RuntimeError("WEAVE_TRACE_VERIFICATION_FAILED")
        if session.trace_id is not None and session.verification in {"ACKNOWLEDGED", "VERIFIED"}:
            return session.trace_id
        try:
            with (
                self.credentials.environment() if self.credentials is not None else nullcontext(),
                self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
            ):
                session.client.flush()
                acknowledged = session.client.get_call(str(session.root_call.id))
            trace_id = self._validate_acknowledgement(session, acknowledged)
        except Exception:
            session.verification = "FAILED"
            raise
        session.trace_id = trace_id
        session.verification = "ACKNOWLEDGED"
        self._trace_to_call[trace_id] = call_id
        return trace_id

    def readback(self, *, trace_id: str) -> TraceReadback:
        try:
            call_id = self._trace_to_call[trace_id]
        except KeyError as exc:
            raise RuntimeError("WEAVE_TRACE_NOT_FLUSHED") from exc
        session = self._session(call_id)
        if session.root_disposition != "COMPLETED" or session.verification == "FAILED":
            raise RuntimeError("WEAVE_TRACE_VERIFICATION_FAILED")
        if session.completed_readback is not None and session.verification == "VERIFIED":
            return session.completed_readback
        if session.verification != "ACKNOWLEDGED" or session.trace_id != trace_id:
            raise RuntimeError("WEAVE_TRACE_NOT_FLUSHED")
        try:
            with (
                self.credentials.environment() if self.credentials is not None else nullcontext(),
                self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
            ):
                acknowledged = session.client.get_call(str(session.root_call.id))
            acknowledged_trace_id = self._validate_acknowledgement(session, acknowledged)
            if acknowledged_trace_id != trace_id:
                raise RuntimeError("WEAVE_TRACE_READBACK_ID_MISMATCH")
            gate = _trace_gate(acknowledged)
            trace_url = str(getattr(acknowledged, "ui_url", "") or "") or None
            if trace_url is not None and not _allowed_target_url(trace_url, self.target):
                raise RuntimeError("WEAVE_TRACE_URL_OUTSIDE_VERIFIED_TARGET")
            readback = TraceReadback(
                trace_id=trace_id,
                trace_url=trace_url,
                call_id=call_id,
                completed=gate["completed"] is True,
                steps=list(gate["steps"]),
                association=AssistantRunAssociation.model_validate(gate["wandb_association"]),
            )
        except Exception:
            session.verification = "FAILED"
            raise
        session.completed_readback = readback
        session.verification = "VERIFIED"
        return readback

    def abort(self, *, call_id: str, error_code: str) -> None:
        session = self._sessions.get(call_id)
        if session is None or session.root_disposition != "OPEN":
            return
        safe_code = error_code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", error_code) else "ERROR"
        session.root_disposition = "OUTCOME_UNKNOWN"
        try:
            with (
                self.credentials.environment() if self.credentials is not None else nullcontext(),
                self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
            ):
                session.client.finish_call(
                    session.root_call,
                    output={
                        "ecocorridor_trace_gate": {
                            "call_id": call_id,
                            "completed": False,
                            "steps": list(session.steps),
                            "wandb_association": session.association.model_dump(mode="json"),
                            "error_code": safe_code,
                        }
                    },
                    exception=RuntimeError(safe_code),
                )
                session.root_disposition = "ABORTED"
                session.client.flush()
                acknowledged = session.client.get_call(str(session.root_call.id))
            trace_id = self._validate_aborted_acknowledgement(
                session,
                acknowledged,
                error_code=safe_code,
            )
            trace_url = str(getattr(acknowledged, "ui_url", "") or "")
            if not trace_url or not _allowed_target_url(trace_url, self.target):
                raise RuntimeError("WEAVE_ABORT_TRACE_URL_UNAVAILABLE")
        except Exception:
            session.verification = "FAILED"
            raise
        session.aborted_trace = {
            "trace_id": trace_id,
            "trace_url": trace_url,
            "call_id": call_id,
            "error_code": safe_code,
            "steps": list(session.steps),
            "association": session.association.model_dump(mode="json"),
            "completed": False,
            "persisted": True,
            "trace_readback_verified": True,
        }
        session.trace_id = trace_id
        session.verification = "VERIFIED"

    def aborted_readback(self, *, call_id: str) -> dict[str, Any]:
        """Return only provider-persisted, exact readback evidence for an aborted call."""

        session = self._session(call_id)
        if (
            session.root_disposition != "ABORTED"
            or session.verification != "VERIFIED"
            or session.aborted_trace is None
        ):
            raise RuntimeError("WEAVE_ABORT_TRACE_READBACK_UNAVAILABLE")
        return dict(session.aborted_trace)

    def record_feedback(
        self,
        *,
        call_id: str,
        trace_id: str,
        rating: str,
        comment: str,
    ) -> dict[str, Any]:
        """Attach bounded practitioner feedback and verify provider readback."""

        if rating not in {"HELPFUL", "NOT_HELPFUL"} or len(comment) > 1024:
            raise ValueError("assistant feedback is invalid")
        validate_feedback_comment(comment)
        if self._trace_to_call.get(trace_id) != call_id:
            raise RuntimeError("WEAVE_TRACE_FEEDBACK_IDENTITY_MISMATCH")
        session = self._session(call_id)
        if (
            session.root_disposition != "COMPLETED"
            or session.verification != "VERIFIED"
            or session.trace_id != trace_id
        ):
            raise RuntimeError("WEAVE_TRACE_FEEDBACK_NOT_READY")
        payload = {"rating": rating, "comment": comment}
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) >= 1024:
            raise ValueError("assistant feedback exceeds the Weave custom-feedback limit")
        self._verify_scope()
        with (
            self.credentials.environment() if self.credentials is not None else nullcontext(),
            self.directories.wandb_environment(base_url=self.target.api_base_url, online=True),
        ):
            acknowledged = session.client.get_call(str(session.root_call.id))
            self._validate_acknowledgement(session, acknowledged)
            created = acknowledged.feedback.add("ecocorridor.practitioner_usefulness", payload)
            session.client.flush()
            feedback_id = str(
                (
                    created
                    if isinstance(created, str)
                    else (
                        created.get("id", "")
                        if isinstance(created, dict)
                        else getattr(created, "id", "")
                    )
                )
                or ""
            )
            if not feedback_id:
                raise RuntimeError("WEAVE_FEEDBACK_ID_UNAVAILABLE")
            readback = list(session.client.get_feedback(feedback_id))
        record = readback[0] if len(readback) == 1 else None
        record_id = record.get("id", "") if isinstance(record, dict) else getattr(record, "id", "")
        record_payload = (
            record.get("payload") if isinstance(record, dict) else getattr(record, "payload", None)
        )
        if record is None or str(record_id) != feedback_id:
            raise RuntimeError("WEAVE_FEEDBACK_READBACK_INVALID")
        if record_payload != payload:
            raise RuntimeError("WEAVE_FEEDBACK_PAYLOAD_MISMATCH")
        return {
            "feedback_id": feedback_id,
            "trace_id": trace_id,
            "rating": rating,
            "verified": True,
        }

    def _active(self, call_id: str) -> _TraceSession:
        session = self._session(call_id)
        if session.root_disposition != "OPEN":
            raise RuntimeError("WEAVE_TRACE_ALREADY_FINISHED")
        return session

    def _session(self, call_id: str) -> _TraceSession:
        try:
            return self._sessions[call_id]
        except KeyError as exc:
            raise RuntimeError("WEAVE_TRACE_NOT_STARTED") from exc

    def _validate_acknowledgement(self, session: _TraceSession, acknowledged: Any) -> str:
        if acknowledged is None or str(getattr(acknowledged, "id", "")) != str(
            session.root_call.id
        ):
            raise RuntimeError("WEAVE_TRACE_NOT_ACKNOWLEDGED")
        trace_id = str(getattr(acknowledged, "trace_id", "") or "")
        expected_wandb_run_id = (
            f"{self.target.entity}/{self.target.project}/{session.association.wandb_run_id}"
        )
        gate = _trace_gate(acknowledged)
        if (
            not trace_id
            or str(getattr(acknowledged, "wb_run_id", "") or "") != expected_wandb_run_id
            or gate.get("call_id") != session.call_id
            or gate.get("completed") is not True
            or gate.get("steps") != session.steps
            or gate.get("wandb_association") != session.association.model_dump(mode="json")
        ):
            raise RuntimeError("WEAVE_TRACE_READBACK_INVALID")
        return trace_id

    def _validate_aborted_acknowledgement(
        self,
        session: _TraceSession,
        acknowledged: Any,
        *,
        error_code: str,
    ) -> str:
        if acknowledged is None or str(getattr(acknowledged, "id", "")) != str(
            session.root_call.id
        ):
            raise RuntimeError("WEAVE_ABORT_TRACE_NOT_ACKNOWLEDGED")
        trace_id = str(getattr(acknowledged, "trace_id", "") or "")
        expected_wandb_run_id = (
            f"{self.target.entity}/{self.target.project}/{session.association.wandb_run_id}"
        )
        gate = _trace_gate(acknowledged)
        if (
            not trace_id
            or str(getattr(acknowledged, "wb_run_id", "") or "") != expected_wandb_run_id
            or gate.get("call_id") != session.call_id
            or gate.get("completed") is not False
            or gate.get("steps") != session.steps
            or gate.get("error_code") != error_code
            or gate.get("wandb_association") != session.association.model_dump(mode="json")
        ):
            raise RuntimeError("WEAVE_ABORT_TRACE_READBACK_INVALID")
        return trace_id


def _trace_gate(acknowledged: Any) -> dict[str, Any]:
    output = getattr(acknowledged, "output", None)
    if not isinstance(output, dict):
        raise TypeError("WEAVE_TRACE_OUTPUT_UNAVAILABLE")
    gate = output.get("ecocorridor_trace_gate")
    if not isinstance(gate, dict):
        raise TypeError("WEAVE_TRACE_GATE_UNAVAILABLE")
    return gate


class EcoOutboxStoreFactory(Protocol):
    def __call__(self, project_directory: Path) -> OutboxRepository: ...


class EcoRpcObservabilityAdapter:
    """Adapter for the worker's parameterized ``observability.*`` boundary.

    A store factory is mandatory so this class cannot create a second,
    non-atomic telemetry database beside the authoritative Eco project store.
    """

    def __init__(
        self,
        *,
        scope_resolver: ProjectScopeResolver,
        store_factory: EcoOutboxStoreFactory,
        uploader: LineageUploader | None = None,
        credentials: InMemoryCredentialSession | None = None,
        profile: AppProfile = ECOCORRIDOR_APP_PROFILE,
    ) -> None:
        self.profile = profile
        self.scope_resolver = scope_resolver
        self.store_factory = store_factory
        self.credentials = credentials or InMemoryCredentialSession(environment_key="WANDB_API_KEY")
        self.uploader = uploader or WandbSdkLineageUploader(
            profile=profile, scope_resolver=scope_resolver
        )

    def validate(self, *, binding: dict[str, Any]) -> dict[str, Any]:
        target = self.target_from_binding(binding)
        with self.credentials.environment():
            exact = self.scope_resolver.resolve(
                entity=target.entity,
                project=target.project,
                deployment=target.deployment,
                api_base_url=target.api_base_url,
            )
            if exact.model_dump(mode="json") != target.scope_evidence.model_dump(mode="json"):
                raise RuntimeError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")
            evidence = self.uploader.verify_target(target)
        return {
            "verified": True,
            "external_project_id": evidence.external_project_id,
            "scope_evidence": evidence.model_dump(mode="json"),
            "profile_namespace": self.profile.namespace,
        }

    def sync(
        self,
        *,
        project_directory: Path,
        binding: dict[str, Any],
        sync_plan: dict[str, Any],
        online: bool = True,
    ) -> dict[str, Any]:
        if sync_plan.get("allowed") is not True or sync_plan.get("blockers"):
            raise RuntimeError("BLOCKED_PROJECT_POLICY")
        store = self.store_factory(project_directory)
        try:
            consumer = DurableOutboxConsumer(
                repository=store,
                uploader=self.uploader,
                profile=self.profile,
                target=self.target_from_binding(binding),
                project_root=project_directory,
                projector=EcoTelemetryProjector(
                    store=store,
                    project_root=project_directory,
                    sync_plan=sync_plan,
                    profile=self.profile,
                ),
            )
            if not online:
                # A previously exact-scope-verified binding may continue
                # locally.  No provider credential, target lookup, outbox
                # claim, or fake offline W&B run is attempted here.
                return consumer.drain(online=False).model_dump(mode="json")
            with self.credentials.environment():
                self.validate(binding=binding)
                report = consumer.drain(online=True)
                return report.model_dump(mode="json")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

    def open_link(self, *, project_directory: Path, kind: str, reference_id: str) -> dict[str, Any]:
        if kind not in {"run", "artifact", "trace"} or not _REFERENCE_ID.fullmatch(reference_id):
            raise ValueError("observability reference is invalid")
        store = self.store_factory(project_directory)
        try:
            binding = store.observability()
            if binding is None:
                raise RuntimeError("OBSERVABILITY_BINDING_REQUIRED")
            target = self.target_from_binding(binding)
            url: str | None = None
            if kind in {"run", "artifact"}:
                for row in store.telemetry_rows():
                    receipt = row.get("external_reference")
                    if row.get("state") != "SYNCED" or not isinstance(receipt, dict):
                        continue
                    if kind == "run" and receipt.get("run_id") == reference_id:
                        url = str(receipt.get("run_url", "")) or None
                        break
                    if kind == "artifact":
                        for artifact in receipt.get("artifacts", []):
                            if not isinstance(artifact, dict):
                                continue
                            if reference_id in {
                                artifact.get("provider_artifact_id"),
                                artifact.get("provider_artifact_ref"),
                            }:
                                url = str(artifact.get("provider_artifact_url", "")) or None
                                break
                        if url is not None:
                            break
            else:
                lookup = getattr(store, "assistant_answer_by_trace", None)
                record = lookup(reference_id) if callable(lookup) else None
                if isinstance(record, dict):
                    answer = record.get("answer")
                    if isinstance(answer, dict):
                        url = str(answer.get("trace_url", "")) or None
            if url is None or not _allowed_target_url(url, target):
                raise RuntimeError("OBSERVABILITY_REFERENCE_NOT_RESOLVED")
            return {"url": url}
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

    @staticmethod
    def target_from_binding(binding: dict[str, Any]) -> WandbTarget:
        """Validate a persisted binding and return its exact immutable target."""

        evidence = ProjectScopeEvidence.model_validate(binding.get("scope_evidence"))
        deployment = str(binding["deployment"])
        api_base_url = str(binding.get("api_base_url") or "")
        if not api_base_url:
            raise ValueError("W&B API base URL is required")
        web_base_url = str(binding.get("web_base_url") or api_base_url)
        return WandbTarget(
            entity=str(binding["entity"]),
            project=str(binding["project"]),
            external_project_id=str(
                binding.get("external_project_id") or evidence.external_project_id
            ),
            deployment=deployment,
            api_base_url=api_base_url,
            web_base_url=web_base_url,
            scope_evidence=evidence,
        )


__all__ = [
    "ECOCORRIDOR_APP_PROFILE",
    "EcoRpcObservabilityAdapter",
    "SdkWeaveClientFactory",
    "WeaveClientFactory",
    "WeaveNestedTraceSink",
    "protected_integration_scope_resolver",
]
