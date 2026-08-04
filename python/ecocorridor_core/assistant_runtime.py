"""Project-scoped production wiring for traced, read-only assistants.

The native host is the only component that reads keychain secrets.  It sends a
provider key once over the private worker pipe; this module keeps it in a
zeroable process-memory session and never writes it to project state, logs, or
trace inputs.  Each assistant call revalidates the persisted W&B identity and
its documented exact Restricted-scope evidence before resolving local evidence
or constructing a provider request.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scenario_platform.cloud_sync import InMemoryCredentialSession, WandbTarget
from scenario_platform.observability import ProjectScopeResolver

from .assistants import (
    AssistantProvider,
    AssistantProviderConfiguration,
    AssistantRejectionAuditSink,
    EvidenceAssistant,
    EvidenceIntakeAssistant,
    EvidenceResolver,
    NestedTraceSink,
    OpenAICompatibleProvider,
    ProjectAuthorizer,
    ProviderCredentialSession,
)
from .contracts import ConnectivityEvidenceClaim
from .observability import EcoRpcObservabilityAdapter, WeaveNestedTraceSink
from .project_store import EcoProjectStore

ProviderFactory = Callable[
    [AssistantProviderConfiguration, ProviderCredentialSession], AssistantProvider
]
TraceFactory = Callable[[WandbTarget, Path], NestedTraceSink]

_ALLOWED_OPERATIONS = frozenset({"evidence.explain", "evidence_intake.extract"})


def _default_provider_factory(
    configuration: AssistantProviderConfiguration,
    credentials: ProviderCredentialSession,
) -> AssistantProvider:
    return OpenAICompatibleProvider(
        base_url=configuration.base_url,
        model=configuration.model,
        credentials=credentials,
        timeout_seconds=configuration.timeout_seconds,
    )


class StoreEvidenceResolver(EvidenceResolver):
    """Resolve only claims in one authoritative, bound project store."""

    def __init__(self, project_root: Path, project_id: str) -> None:
        self.project_root = project_root
        self.project_id = project_id

    def resolve(
        self, *, project_id: str, evidence_ids: Sequence[str]
    ) -> list[ConnectivityEvidenceClaim]:
        if project_id != self.project_id:
            raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
        store = EcoProjectStore(self.project_root)
        try:
            if store.project()["project_id"] != self.project_id:
                raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
            return [
                ConnectivityEvidenceClaim.model_validate(
                    store.resolve_evidence_claim(evidence_id)["claim"]
                )
                for evidence_id in evidence_ids
            ]
        finally:
            store.close()


class StoreAssistantRejectionAuditSink(AssistantRejectionAuditSink):
    """Persist post-provider safety rejections in the authoritative project store."""

    def __init__(self, project_root: Path, project_id: str) -> None:
        self.project_root = project_root
        self.project_id = project_id

    def record_rejected(
        self,
        *,
        call_id: str,
        project_id: str,
        operation: str,
        error_code: str,
        provider_call_id: str,
        provider_model: str,
        association: Any,
        provider_abort_trace: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if project_id != self.project_id or association.project_id != self.project_id:
            raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
        store = EcoProjectStore(self.project_root)
        try:
            if store.project()["project_id"] != self.project_id:
                raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
            return store.record_assistant_rejection(
                call_id=call_id,
                operation=operation,
                error_code=error_code,
                provider_call_id_sha256=hashlib.sha256(
                    provider_call_id.encode("utf-8")
                ).hexdigest(),
                provider_model=provider_model,
                association=association.model_dump(mode="json"),
                provider_abort_trace=provider_abort_trace,
            )
        finally:
            store.close()


class BoundProjectAuthorizer(ProjectAuthorizer):
    """Revalidate project identity and exact cloud scope for every AI call."""

    def __init__(
        self,
        runtime: ProviderBackedAssistantRuntime,
        project_root: Path,
        project_id: str,
        target: WandbTarget,
    ) -> None:
        self.runtime = runtime
        self.project_root = project_root
        self.project_id = project_id
        self.target = target

    def authorize(self, *, project_id: str, operation: str) -> bool:
        if project_id != self.project_id or operation not in _ALLOWED_OPERATIONS:
            return False
        current_project_id, target = self.runtime.verify_project(self.project_root)
        return current_project_id == self.project_id and target.model_dump(
            mode="json"
        ) == self.target.model_dump(mode="json")


@dataclass(slots=True)
class _ConfiguredSession:
    project_id: str
    target: WandbTarget
    configuration: AssistantProviderConfiguration
    credentials: ProviderCredentialSession
    evidence: EvidenceAssistant
    intake: EvidenceIntakeAssistant
    trace_sink: NestedTraceSink


class ProviderBackedAssistantRuntime:
    """Construct and retain project-bound assistants without persisting secrets."""

    def __init__(
        self,
        *,
        scope_resolver: ProjectScopeResolver,
        observability_credentials: InMemoryCredentialSession,
        provider_factory: ProviderFactory | None = None,
        trace_factory: TraceFactory | None = None,
    ) -> None:
        self.scope_resolver = scope_resolver
        self.observability_credentials = observability_credentials
        self.provider_factory = provider_factory or _default_provider_factory
        self.trace_factory = trace_factory or self._default_trace_factory
        self._sessions: dict[Path, _ConfiguredSession] = {}
        self._lock = threading.RLock()

    def _default_trace_factory(self, target: WandbTarget, project_root: Path) -> NestedTraceSink:
        return WeaveNestedTraceSink(
            target=target,
            scope_resolver=self.scope_resolver,
            project_root=project_root,
            credentials=self.observability_credentials,
        )

    def verify_project(self, project_root: Path) -> tuple[str, WandbTarget]:
        root = project_root.resolve(strict=True)
        store = EcoProjectStore(root)
        try:
            project_id = str(store.project()["project_id"])
            binding = store.observability()
        finally:
            store.close()
        if binding is None:
            raise PermissionError("OBSERVABILITY_BINDING_REQUIRED")
        if (
            binding.get("state") != "CONNECTED"
            or binding.get("verified") is not True
            or binding.get("profile_namespace") != "ecocorridor"
        ):
            raise PermissionError("ASSISTANT_VERIFIED_ONLINE_BINDING_REQUIRED")
        target = EcoRpcObservabilityAdapter.target_from_binding(binding)
        try:
            with self.observability_credentials.environment():
                current = self.scope_resolver.resolve(
                    entity=target.entity,
                    project=target.project,
                    deployment=target.deployment,
                    api_base_url=target.api_base_url,
                )
        except Exception as exc:
            raise PermissionError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE") from exc
        if current.model_dump(mode="json") != target.scope_evidence.model_dump(mode="json"):
            raise PermissionError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")
        return project_id, target

    def configure(
        self,
        *,
        project_root: Path,
        configuration: AssistantProviderConfiguration,
        api_key: str,
    ) -> dict[str, Any]:
        root = project_root.resolve(strict=True)
        project_id, target = self.verify_project(root)
        credentials = ProviderCredentialSession()
        credentials.configure(api_key)
        try:
            provider = self.provider_factory(configuration, credentials)
            trace_sink = self.trace_factory(target, root)
            authorizer = BoundProjectAuthorizer(self, root, project_id, target)
            rejection_audit_sink = StoreAssistantRejectionAuditSink(root, project_id)
            session = _ConfiguredSession(
                project_id=project_id,
                target=target,
                configuration=configuration,
                credentials=credentials,
                evidence=EvidenceAssistant(
                    provider=provider,
                    trace_sink=trace_sink,
                    authorizer=authorizer,
                    resolver=StoreEvidenceResolver(root, project_id),
                    rejection_audit_sink=rejection_audit_sink,
                ),
                intake=EvidenceIntakeAssistant(
                    provider=provider,
                    trace_sink=trace_sink,
                    authorizer=authorizer,
                    rejection_audit_sink=rejection_audit_sink,
                ),
                trace_sink=trace_sink,
            )
        except BaseException:
            credentials.clear()
            raise
        with self._lock:
            prior = self._sessions.get(root)
            self._sessions[root] = session
            if prior is not None:
                prior.credentials.clear()
        return self.status(project_root=root)

    def status(self, *, project_root: Path) -> dict[str, Any]:
        root = project_root.resolve(strict=True)
        with self._lock:
            session = self._sessions.get(root)
            if session is None:
                return {"configured": False, "provider": None}
            return {
                "configured": session.credentials.configured,
                "provider": {
                    "base_url": session.configuration.base_url,
                    "model": session.configuration.model,
                    "timeout_seconds": session.configuration.timeout_seconds,
                },
                "project_id": session.project_id,
                "wandb_external_project_id": session.target.external_project_id,
                "trace_required": True,
            }

    def evidence(self, *, project_root: Path) -> EvidenceAssistant:
        return self._session(project_root).evidence

    def intake(self, *, project_root: Path) -> EvidenceIntakeAssistant:
        return self._session(project_root).intake

    def feedback_sink(self, *, project_root: Path) -> Any | None:
        sink = self._session(project_root).trace_sink
        return sink if callable(getattr(sink, "record_feedback", None)) else None

    def clear(self, *, project_root: Path) -> None:
        root = project_root.resolve(strict=True)
        with self._lock:
            session = self._sessions.pop(root, None)
        if session is not None:
            session.credentials.clear()

    def _session(self, project_root: Path) -> _ConfiguredSession:
        root = project_root.resolve(strict=True)
        with self._lock:
            session = self._sessions.get(root)
        if session is None or not session.credentials.configured:
            raise RuntimeError("TRACED_ASSISTANT_NOT_CONFIGURED")
        return session


__all__ = [
    "BoundProjectAuthorizer",
    "ProviderBackedAssistantRuntime",
    "StoreAssistantRejectionAuditSink",
    "StoreEvidenceResolver",
]
