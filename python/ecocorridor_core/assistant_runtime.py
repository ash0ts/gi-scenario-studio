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
from typing import Any, Literal

from scenario_platform.cloud_sync import InMemoryCredentialSession, WandbTarget
from scenario_platform.observability import ProjectScopeResolver

from .assistants import (
    AssistantEvidenceAuthorization,
    AssistantProvider,
    AssistantProviderConfiguration,
    AssistantRejectionAuditSink,
    AssistantRunAssociation,
    EvidenceAssistant,
    EvidenceIntakeAssistant,
    EvidenceResolver,
    NestedTraceSink,
    OpenAICompatibleProvider,
    ProjectAuthorizer,
    ProviderCredentialSession,
)
from .contracts import ConnectivityEvidenceClaim, canonical_sha256
from .native_agents_runtime import (
    NativeAgentAuthorization,
    native_single_turn_executor_from_sessions,
)
from .native_assistant_bridge import (
    NativeAgentsAssistantBridge,
    attach_verified_weave_feedback,
)
from .observability import (
    ECOCORRIDOR_APP_PROFILE,
    EcoRpcObservabilityAdapter,
    SyntheticTraceConsentBinding,
    WeaveNestedTraceSink,
)
from .project_store import EcoProjectStore
from .review_brief import (
    ReviewBriefActionKind,
    ReviewBriefDraft,
    ReviewBriefFeedbackTraceReceipt,
    ReviewBriefFindingKind,
    ReviewBriefLocalFinding,
    ReviewBriefPriority,
    ReviewBriefSource,
    ReviewBriefSourceMode,
    ReviewerDiscipline,
    build_authorized_snapshot,
    runner_request,
)
from .review_brief_agent_runtime import (
    ReviewBriefAgentBinding,
    ReviewBriefSubprocessRunner,
    ReviewBriefWeaveTarget,
    review_brief_runner_from_sessions,
)

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


@dataclass(slots=True)
class _SyntheticReviewBriefSession:
    project_id: str
    target: SyntheticTraceConsentBinding
    configuration: AssistantProviderConfiguration
    credentials: ProviderCredentialSession
    capability_receipt: Any


def _capability_preflight_subject_id(
    *,
    project_id: str,
    cloud_authorization_sha256: str,
    configuration: AssistantProviderConfiguration,
) -> str:
    """Return an opaque, non-colliding owner identity for one capability run."""

    digest = canonical_sha256(
        {
            "project_id_sha256": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
            "cloud_authorization_sha256": cloud_authorization_sha256,
            "provider_configuration_sha256": canonical_sha256(
                configuration.model_dump(mode="json")
            ),
        }
    )
    return f"native-assistant-provider-preflight-v1-{digest[:24]}"


class ProviderBackedAssistantRuntime:
    """Construct and retain project-bound assistants without persisting secrets."""

    def __init__(
        self,
        *,
        scope_resolver: ProjectScopeResolver,
        observability_credentials: InMemoryCredentialSession,
        provider_factory: ProviderFactory | None = None,
        trace_factory: TraceFactory | None = None,
        synthetic_trace_identity_verifier: (
            Callable[[SyntheticTraceConsentBinding], None] | None
        ) = None,
        allow_legacy_test_seam: bool = False,
    ) -> None:
        self.scope_resolver = scope_resolver
        self.observability_credentials = observability_credentials
        injected_legacy = provider_factory is not None or trace_factory is not None
        if (provider_factory is None) != (trace_factory is None):
            raise ValueError("legacy assistant test factories must be provided together")
        if injected_legacy and not allow_legacy_test_seam:
            raise ValueError("legacy assistant adapters require the explicit test-only seam")
        # The packaged runtime has no injected factories and therefore always
        # uses native OpenAI Agents SDK conversations. The legacy pair exists
        # only for deterministic unit tests that opt in above.
        self._use_native_agents = not injected_legacy
        self.provider_factory = provider_factory or _default_provider_factory
        self.trace_factory = trace_factory or self._default_trace_factory
        self.synthetic_trace_identity_verifier = synthetic_trace_identity_verifier
        self._sessions: dict[Path, _ConfiguredSession] = {}
        self._synthetic_review_brief_sessions: dict[Path, _SyntheticReviewBriefSession] = {}
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
            project = store.project()
            if project["mode"] == "GUIDED":
                raise PermissionError("PROFESSIONAL_CAPABILITY_GUIDED_PROJECT_BLOCKED")
            if project["mode"] not in {"IMPORTED", "PLANNING"}:
                raise PermissionError("PROFESSIONAL_PROJECT_MODE_UNSUPPORTED")
            project_id = str(project["project_id"])
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

    def _native_capability_preflight(
        self,
        *,
        root: Path,
        project_id: str,
        weave_target: ReviewBriefWeaveTarget,
        wandb_external_project_id: str,
        cloud_authorization_kind: Literal["RESTRICTED_EXACT_SCOPE", "RIDGEWAY_CC0_TRACE_ONLY"],
        cloud_authorization_sha256: str,
        configuration: AssistantProviderConfiguration,
        provider_credentials: ProviderCredentialSession,
    ) -> Any:
        """Prove tool calling, strict output, and Weave readback at configure time."""

        analysis_id = "native-assistant-provider-preflight-v1"
        model_revision_id = "native-assistant-provider-preflight-revision-v1"
        authorization_sha256 = canonical_sha256(
            {
                "purpose": "native-assistant-provider-capability-preflight",
                "project_id_sha256": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
                "cloud_authorization_kind": cloud_authorization_kind,
                "cloud_authorization_sha256": cloud_authorization_sha256,
                "provider": configuration.model,
            }
        )
        evidence_decision_id = (
            "evidence_auth_"
            + canonical_sha256(
                {
                    "authorization_sha256": authorization_sha256,
                    "analysis_id": analysis_id,
                }
            )[:24]
        )
        source = ReviewBriefSource(
            source_mode=(
                ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC
                if cloud_authorization_kind == "RIDGEWAY_CC0_TRACE_ONLY"
                else ReviewBriefSourceMode.PROFESSIONAL
            ),
            project_id=project_id,
            project_title="Synthetic provider capability preflight",
            analysis_id=analysis_id,
            portfolio_id="provider-capability-preflight",
            model_revision_id=model_revision_id,
            decision_board_sha256=canonical_sha256(
                [project_id, analysis_id, model_revision_id, configuration.model]
            ),
            scenario_ids=["capability-scenario"],
            disposition="INCOMPLETE",
            next_action="Complete provider capability verification.",
            controlling_scenario_id=None,
            safeguard_status="REVIEW",
            review_mask_triggered=False,
            authorization_sha256=authorization_sha256,
            evidence_authorization_decision_id=evidence_decision_id,
            current_authorizations_sha256=authorization_sha256,
            findings=[
                ReviewBriefLocalFinding(
                    finding_id="provider-capability-finding",
                    finding_kind=ReviewBriefFindingKind.ASSUMPTION,
                    priority=ReviewBriefPriority.FOLLOW_UP,
                    evidence_claim_ids=["provider-capability-evidence"],
                    scenario_ids=["capability-scenario"],
                    permitted_actions=[ReviewBriefActionKind.INSPECT_EVIDENCE],
                    permitted_disciplines=[ReviewerDiscipline.ECOLOGY],
                )
            ],
        )
        preflight_subject_id = _capability_preflight_subject_id(
            project_id=project_id,
            cloud_authorization_sha256=cloud_authorization_sha256,
            configuration=configuration,
        )
        association = AssistantRunAssociation.create(
            project_id=project_id,
            wandb_external_project_id=wandb_external_project_id,
            wandb_run_id=ECOCORRIDOR_APP_PROFILE.run_id(
                "assistant_evaluation", preflight_subject_id
            ),
            wandb_run_kind="assistant_evaluation",
            run_subject_id=preflight_subject_id,
            analysis_id=analysis_id,
        )
        snapshot = build_authorized_snapshot(source)
        binding = ReviewBriefAgentBinding.from_authorized_snapshot(
            project_id=project_id,
            analysis_id=analysis_id,
            portfolio_id=source.portfolio_id,
            model_revision_id=model_revision_id,
            decision_board_sha256=source.decision_board_sha256,
            cloud_authorization_kind=cloud_authorization_kind,
            cloud_authorization_sha256=cloud_authorization_sha256,
            snapshot=snapshot,
        )
        scratch_root = root / ".ecocorridor-runtime" / "provider-capability-preflight"
        scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if scratch_root.is_symlink():
            raise PermissionError("NATIVE_AGENT_RUNTIME_ROOT_UNSAFE")
        provider_mode = (
            "RESPONSES"
            if configuration.base_url == "https://api.openai.com/v1"
            else "CHAT_COMPLETIONS"
        )
        runner = review_brief_runner_from_sessions(
            provider_configuration=configuration,
            provider_mode=provider_mode,
            weave_target=weave_target,
            wandb_external_project_id=wandb_external_project_id,
            association=association,
            binding=binding,
            provider_credentials=provider_credentials,
            observability_credentials=self.observability_credentials,
            scratch_root=scratch_root,
            wandb_run_lifecycle="CREATE_OR_RESUME_AND_FINISH",
            deadline_seconds=min(configuration.timeout_seconds + 30.0, 180.0),
        )
        capability = runner.preflight(request=runner_request(source))
        if not (
            capability.tool_calling_verified
            and capability.strict_structured_output_verified
            and capability.trace_readback_verified
        ):
            raise PermissionError("NATIVE_AGENT_PROVIDER_CAPABILITY_PREFLIGHT_FAILED")
        return capability

    def configure(
        self,
        *,
        project_root: Path,
        configuration: AssistantProviderConfiguration,
        api_key: str,
    ) -> dict[str, Any]:
        root = project_root.resolve(strict=True)
        with self._lock:
            if root in self._synthetic_review_brief_sessions:
                raise PermissionError("ASSISTANT_CAPABILITY_SESSION_CONFLICT")
        project_id, target = self.verify_project(root)
        credentials = ProviderCredentialSession()
        credentials.configure(api_key)
        try:
            authorizer = BoundProjectAuthorizer(self, root, project_id, target)
            rejection_audit_sink = StoreAssistantRejectionAuditSink(root, project_id)
            if self._use_native_agents:
                scope_sha256 = canonical_sha256(target.scope_evidence.model_dump(mode="json"))
                weave_target = ReviewBriefWeaveTarget(
                    entity=target.entity,
                    project=target.project,
                    api_base_url=target.api_base_url,
                    web_base_url=target.web_base_url,
                    external_project_id_sha256=hashlib.sha256(
                        target.external_project_id.encode("utf-8")
                    ).hexdigest(),
                )
                capability = self._native_capability_preflight(
                    root=root,
                    project_id=project_id,
                    weave_target=weave_target,
                    wandb_external_project_id=target.external_project_id,
                    cloud_authorization_kind="RESTRICTED_EXACT_SCOPE",
                    cloud_authorization_sha256=scope_sha256,
                    configuration=configuration,
                    provider_credentials=credentials,
                )
                provider_mode = (
                    "RESPONSES"
                    if configuration.base_url == "https://api.openai.com/v1"
                    else "CHAT_COMPLETIONS"
                )
                scratch_root = root / ".ecocorridor-runtime" / "native-assistants"
                scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if scratch_root.is_symlink():
                    raise PermissionError("NATIVE_AGENT_RUNTIME_ROOT_UNSAFE")

                def executor_factory(
                    association: AssistantRunAssociation,
                    authorization: NativeAgentAuthorization,
                ) -> Any:
                    return native_single_turn_executor_from_sessions(
                        provider_configuration=configuration,
                        provider_mode=provider_mode,
                        target=target,
                        association=association,
                        authorization=authorization,
                        capability_receipt=capability,
                        provider_credentials=credentials,
                        observability_credentials=self.observability_credentials,
                        scratch_root=scratch_root,
                        deadline_seconds=min(configuration.timeout_seconds + 30.0, 180.0),
                    )

                def verify_scope() -> None:
                    current_project_id, current_target = self.verify_project(root)
                    if current_project_id != project_id or current_target.model_dump(
                        mode="json"
                    ) != target.model_dump(mode="json"):
                        raise PermissionError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")

                evidence_bridge = NativeAgentsAssistantBridge(
                    executor_factory=executor_factory,
                    target=target,
                    credentials=self.observability_credentials,
                    scope_verifier=verify_scope,
                    feedback_root=root / ".ecocorridor-runtime" / "native-assistant-feedback",
                )
                intake_bridge = NativeAgentsAssistantBridge(
                    executor_factory=executor_factory,
                    target=target,
                    credentials=self.observability_credentials,
                    scope_verifier=verify_scope,
                    feedback_root=root / ".ecocorridor-runtime" / "native-intake-feedback",
                )
                provider = evidence_bridge
                trace_sink = evidence_bridge
                intake_provider = intake_bridge
                intake_trace_sink = intake_bridge
            else:
                provider = self.provider_factory(configuration, credentials)
                trace_sink = self.trace_factory(target, root)
                intake_provider = provider
                intake_trace_sink = trace_sink
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
                    provider=intake_provider,
                    trace_sink=intake_trace_sink,
                    authorizer=authorizer,
                    rejection_audit_sink=rejection_audit_sink,
                ),
                trace_sink=trace_sink,
            )
        except BaseException:
            credentials.clear()
            raise
        with self._lock:
            if root in self._synthetic_review_brief_sessions:
                credentials.clear()
                raise PermissionError("ASSISTANT_CAPABILITY_SESSION_CONFLICT")
            prior = self._sessions.get(root)
            self._sessions[root] = session
            if prior is not None:
                prior.credentials.clear()
        return self.status(project_root=root)

    def _validate_synthetic_trace_binding(
        self,
        root: Path,
        expected: SyntheticTraceConsentBinding | None = None,
    ) -> tuple[str, SyntheticTraceConsentBinding]:
        store = EcoProjectStore(root)
        try:
            project = store.project()
            if project["mode"] != "GUIDED":
                raise PermissionError("RIDGEWAY_SYNTHETIC_TRACE_GUIDED_ONLY")
            envelope = store.synthetic_trace_binding()
        finally:
            store.close()
        if envelope is None:
            raise PermissionError("RIDGEWAY_SYNTHETIC_TRACE_BINDING_REQUIRED")
        payload = {
            key: value
            for key, value in envelope.items()
            if key not in {"binding_sha256", "connected_at"}
        }
        target = SyntheticTraceConsentBinding.model_validate(payload)
        if expected is not None and target.model_dump(mode="json") != expected.model_dump(
            mode="json"
        ):
            raise PermissionError("RIDGEWAY_SYNTHETIC_TRACE_BINDING_CHANGED")
        return str(project["project_id"]), target

    def _revalidate_synthetic_trace_identity(self, target: SyntheticTraceConsentBinding) -> None:
        if self.synthetic_trace_identity_verifier is None:
            raise PermissionError("RIDGEWAY_SYNTHETIC_TRACE_IDENTITY_VERIFIER_UNAVAILABLE")
        self.synthetic_trace_identity_verifier(target)

    def configure_synthetic_review_brief(
        self,
        *,
        project_root: Path,
        target: SyntheticTraceConsentBinding,
        configuration: AssistantProviderConfiguration,
        api_key: str,
    ) -> dict[str, Any]:
        """Configure only the CC0 Ridgeway Review Brief Agent capability.

        This session deliberately contains no Evidence Assistant or Intake
        Assistant and can never satisfy the professional exact-scope session
        lookup.
        """

        root = project_root.resolve(strict=True)
        with self._lock:
            if root in self._sessions:
                raise PermissionError("ASSISTANT_CAPABILITY_SESSION_CONFLICT")
        project_id, current = self._validate_synthetic_trace_binding(root, target)
        self._revalidate_synthetic_trace_identity(current)
        credentials = ProviderCredentialSession()
        credentials.configure(api_key)
        try:
            weave_target = ReviewBriefWeaveTarget(
                entity=current.entity,
                project=current.project,
                api_base_url=current.api_base_url,
                web_base_url=current.web_base_url,
                external_project_id_sha256=hashlib.sha256(
                    current.external_project_id.encode("utf-8")
                ).hexdigest(),
            )
            capability = self._native_capability_preflight(
                root=root,
                project_id=project_id,
                weave_target=weave_target,
                wandb_external_project_id=current.external_project_id,
                cloud_authorization_kind="RIDGEWAY_CC0_TRACE_ONLY",
                cloud_authorization_sha256=current.identity_sha256,
                configuration=configuration,
                provider_credentials=credentials,
            )
            session = _SyntheticReviewBriefSession(
                project_id=project_id,
                target=current,
                configuration=configuration,
                credentials=credentials,
                capability_receipt=capability,
            )
        except BaseException:
            credentials.clear()
            raise
        with self._lock:
            if root in self._sessions:
                credentials.clear()
                raise PermissionError("ASSISTANT_CAPABILITY_SESSION_CONFLICT")
            prior = self._synthetic_review_brief_sessions.get(root)
            self._synthetic_review_brief_sessions[root] = session
            if prior is not None:
                prior.credentials.clear()
        return self.synthetic_review_brief_status(project_root=root)

    def synthetic_review_brief_status(self, *, project_root: Path) -> dict[str, Any]:
        root = project_root.resolve(strict=True)
        with self._lock:
            session = self._synthetic_review_brief_sessions.get(root)
        if session is None:
            return {"configured": False, "provider": None}
        try:
            project_id, target = self._validate_synthetic_trace_binding(root, session.target)
        except (PermissionError, RuntimeError, ValueError):
            return {"configured": False, "provider": None}
        return {
            "configured": session.credentials.configured,
            "provider": {
                "base_url": session.configuration.base_url,
                "model": session.configuration.model,
                "timeout_seconds": session.configuration.timeout_seconds,
            },
            "project_id": project_id,
            "wandb_external_project_id": target.external_project_id,
            "trace_required": True,
        }

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

    def record_review_brief_feedback(
        self,
        *,
        project_root: Path,
        draft: ReviewBriefDraft,
        feedback_id: str,
        rating: str,
        comment: str,
    ) -> ReviewBriefFeedbackTraceReceipt:
        """Attach usefulness feedback to the exact retained Agents trace.

        The local feedback row is written only after this server-side attachment
        is flushed and read back. Scope is verified both before and after the
        Weave operation by the shared helper.
        """

        root = project_root.resolve(strict=True)
        if draft.source_mode == ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC:
            with self._lock:
                synthetic = self._synthetic_review_brief_sessions.get(root)
            if synthetic is None or not synthetic.credentials.configured:
                raise RuntimeError("RIDGEWAY_REVIEW_BRIEF_AGENT_NOT_CONFIGURED")
            project_id, current = self._validate_synthetic_trace_binding(root, synthetic.target)
            self._revalidate_synthetic_trace_identity(current)
            if project_id != synthetic.project_id:
                raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
            target: Any = ReviewBriefWeaveTarget(
                entity=current.entity,
                project=current.project,
                api_base_url=current.api_base_url,
                web_base_url=current.web_base_url,
                external_project_id_sha256=hashlib.sha256(
                    current.external_project_id.encode("utf-8")
                ).hexdigest(),
            )

            def verify_scope() -> None:
                self._validate_synthetic_trace_binding(root, current)
                self._revalidate_synthetic_trace_identity(current)

        else:
            session = self._session(root)
            project_id, target = self.verify_project(root)
            if project_id != session.project_id or target.model_dump(
                mode="json"
            ) != session.target.model_dump(mode="json"):
                raise PermissionError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")

            def verify_scope() -> None:
                current_project_id, current_target = self.verify_project(root)
                if current_project_id != project_id or current_target.model_dump(
                    mode="json"
                ) != target.model_dump(mode="json"):
                    raise PermissionError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")

        receipt = attach_verified_weave_feedback(
            target=target,
            credentials=self.observability_credentials,
            scope_verifier=verify_scope,
            weave_call_id=draft.trace.weave_call_id,
            agents_trace_id=draft.trace.trace_id,
            wandb_run_id=draft.trace.wandb_run_id,
            rating=rating,
            comment=comment,
            feedback_type="ecocorridor.review_brief_usefulness",
            client_feedback_id=feedback_id,
        )
        return ReviewBriefFeedbackTraceReceipt.model_validate(receipt)

    def review_brief_runner(
        self,
        *,
        project_root: Path,
        source: ReviewBriefSource,
        association: AssistantRunAssociation | None = None,
        authorization: AssistantEvidenceAuthorization | None = None,
    ) -> ReviewBriefSubprocessRunner:
        """Build one source-bound native Agents runner without exposing either key.

        The authoritative dispatcher constructs ``source`` from immutable direct
        evidence. This method revalidates the bound W&B target, derives an opaque
        alias-only binding, and lends both credentials only while the framed child
        request is written. The returned runner has no repository or generic RPC
        capability.
        """

        root = project_root.resolve(strict=True)
        if source.source_mode == ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC:
            if authorization is not None:
                raise PermissionError("REVIEW_BRIEF_SYNTHETIC_AUTHORIZATION_INVALID")
            with self._lock:
                synthetic_session = self._synthetic_review_brief_sessions.get(root)
            if synthetic_session is None or not synthetic_session.credentials.configured:
                raise RuntimeError("RIDGEWAY_REVIEW_BRIEF_AGENT_NOT_CONFIGURED")
            project_id, synthetic_target = self._validate_synthetic_trace_binding(
                root, synthetic_session.target
            )
            self._revalidate_synthetic_trace_identity(synthetic_target)
            if project_id != synthetic_session.project_id or source.project_id != project_id:
                raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
            external_project_id = synthetic_target.external_project_id
            cloud_authorization_kind: Literal[
                "RESTRICTED_EXACT_SCOPE", "RIDGEWAY_CC0_TRACE_ONLY"
            ] = "RIDGEWAY_CC0_TRACE_ONLY"
            cloud_authorization_sha256 = synthetic_target.identity_sha256
            weave_target = ReviewBriefWeaveTarget(
                entity=synthetic_target.entity,
                project=synthetic_target.project,
                api_base_url=synthetic_target.api_base_url,
                web_base_url=synthetic_target.web_base_url,
                external_project_id_sha256=hashlib.sha256(
                    external_project_id.encode("utf-8")
                ).hexdigest(),
            )
            configuration = synthetic_session.configuration
            provider_credentials = synthetic_session.credentials
        elif source.source_mode == ReviewBriefSourceMode.PROFESSIONAL:
            session = self._session(root)
            project_id, target = self.verify_project(root)
            if project_id != session.project_id or source.project_id != project_id:
                raise PermissionError("ASSISTANT_PROJECT_IDENTITY_MISMATCH")
            if target.model_dump(mode="json") != session.target.model_dump(mode="json"):
                raise PermissionError("WANDB_PROJECT_SCOPE_EVIDENCE_CHANGED")
            scope_evidence_sha256 = canonical_sha256(target.scope_evidence.model_dump(mode="json"))
            if authorization is None:
                raise PermissionError("REVIEW_BRIEF_AUTHORIZATION_REQUIRED")
            expected_authorization_sha256 = canonical_sha256(authorization.model_dump(mode="json"))
            if (
                authorization.decision_id != source.evidence_authorization_decision_id
                or authorization.current_authorizations_sha256
                != source.current_authorizations_sha256
                or expected_authorization_sha256 != source.authorization_sha256
                or authorization.project_id_sha256
                != hashlib.sha256(project_id.encode("utf-8")).hexdigest()
                or authorization.analysis_id_sha256
                != hashlib.sha256(source.analysis_id.encode("utf-8")).hexdigest()
                or authorization.model_revision_id_sha256
                != hashlib.sha256(source.model_revision_id.encode("utf-8")).hexdigest()
                or authorization.external_project_id_sha256
                != hashlib.sha256(target.external_project_id.encode("utf-8")).hexdigest()
                or authorization.scope_evidence_sha256 != scope_evidence_sha256
            ):
                raise PermissionError("REVIEW_BRIEF_AUTHORIZATION_CHANGED")
            external_project_id = target.external_project_id
            cloud_authorization_kind = "RESTRICTED_EXACT_SCOPE"
            cloud_authorization_sha256 = scope_evidence_sha256
            weave_target = ReviewBriefWeaveTarget(
                entity=target.entity,
                project=target.project,
                api_base_url=target.api_base_url,
                web_base_url=target.web_base_url,
                external_project_id_sha256=hashlib.sha256(
                    external_project_id.encode("utf-8")
                ).hexdigest(),
            )
            configuration = session.configuration
            provider_credentials = session.credentials
        else:  # pragma: no cover - enum contract guards this branch
            raise PermissionError("REVIEW_BRIEF_SOURCE_MODE_UNSUPPORTED")

        if association is None:
            if source.source_mode != ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC:
                raise PermissionError("REVIEW_BRIEF_RUN_ASSOCIATION_REQUIRED")
            guided_session_id = synthetic_target.guided_session_id
            association = AssistantRunAssociation.create(
                project_id=project_id,
                wandb_external_project_id=external_project_id,
                wandb_run_id=synthetic_target.wandb_run_id,
                wandb_run_kind="guided_session",
                run_subject_id=(
                    f"{guided_session_id}-"
                    f"{hashlib.sha256(project_id.encode('utf-8')).hexdigest()[:12]}"
                ),
                analysis_id=source.analysis_id,
            )
        if (
            association.project_id != project_id
            or association.wandb_external_project_id != external_project_id
            or association.analysis_id != source.analysis_id
        ):
            raise PermissionError("REVIEW_BRIEF_RUN_ASSOCIATION_MISMATCH")
        if source.source_mode == ReviewBriefSourceMode.PROFESSIONAL:
            store = EcoProjectStore(root)
            try:
                retained_run = any(
                    row.get("state") == "SYNCED"
                    and isinstance(row.get("external_reference"), dict)
                    and row["external_reference"].get("run_id") == association.wandb_run_id
                    for row in store.telemetry_rows()
                )
            finally:
                store.close()
            if not retained_run:
                raise PermissionError("REVIEW_BRIEF_WANDB_ANALYSIS_RUN_NOT_CONFIRMED")

        snapshot = build_authorized_snapshot(source)
        binding = ReviewBriefAgentBinding.from_authorized_snapshot(
            project_id=project_id,
            analysis_id=source.analysis_id,
            portfolio_id=source.portfolio_id,
            model_revision_id=source.model_revision_id,
            decision_board_sha256=source.decision_board_sha256,
            cloud_authorization_kind=cloud_authorization_kind,
            cloud_authorization_sha256=cloud_authorization_sha256,
            snapshot=snapshot,
        )

        scratch_root = root / ".ecocorridor-runtime" / "review-brief-agent"
        scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if scratch_root.is_symlink():
            raise PermissionError("REVIEW_BRIEF_AGENT_RUNTIME_ROOT_UNSAFE")
        provider_mode = (
            "RESPONSES"
            if configuration.base_url == "https://api.openai.com/v1"
            else "CHAT_COMPLETIONS"
        )
        return review_brief_runner_from_sessions(
            provider_configuration=configuration,
            provider_mode=provider_mode,
            weave_target=weave_target,
            wandb_external_project_id=external_project_id,
            association=association,
            binding=binding,
            provider_credentials=provider_credentials,
            observability_credentials=self.observability_credentials,
            scratch_root=scratch_root,
            wandb_run_lifecycle=(
                "CREATE_OR_RESUME_AND_FINISH"
                if source.source_mode == ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC
                else "ATTACH_EXISTING"
            ),
            deadline_seconds=min(configuration.timeout_seconds + 30.0, 180.0),
        )

    def clear(self, *, project_root: Path) -> None:
        root = project_root.resolve(strict=True)
        with self._lock:
            session = self._sessions.pop(root, None)
            synthetic = self._synthetic_review_brief_sessions.pop(root, None)
        if session is not None:
            session.credentials.clear()
        if synthetic is not None:
            synthetic.credentials.clear()

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
