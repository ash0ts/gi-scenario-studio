from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.assistant_runtime import ProviderBackedAssistantRuntime
from ecocorridor_core.assistants import (
    APPROVED_TRACE_STEPS,
    AssistantEvidenceAuthorization,
    AssistantProviderConfiguration,
    AssistantRunAssociation,
)
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.native_agents_runtime import (
    NativeAgentAuthorization,
    NativeAgentsTraceReceipt,
    NativeSingleTurnAgentsResult,
)
from ecocorridor_core.native_assistant_bridge import (
    NativeAgentsAssistantBridge,
    attach_verified_weave_feedback,
)
from ecocorridor_core.review_brief_agent_runtime import ReviewBriefAgentCapabilityReceipt
from scenario_platform.cloud_sync import InMemoryCredentialSession, WandbTarget
from scenario_platform.observability import (
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _target() -> WandbTarget:
    evidence = ProjectScopeEvidence(
        entity="eco-team",
        project="restricted-project",
        external_project_id="wandb-project-1",
        deployment="SELF_MANAGED",
        api_base_url="https://api.wandb.example",
        scope="RESTRICTED",
        evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
        provider_field="projects.access_scope",
    )
    return WandbTarget(
        entity=evidence.entity,
        project=evidence.project,
        external_project_id=evidence.external_project_id,
        deployment=evidence.deployment,
        api_base_url=evidence.api_base_url,
        web_base_url="https://wandb.example",
        scope_evidence=evidence,
    )


def _association(*, intake: bool = False) -> AssistantRunAssociation:
    return AssistantRunAssociation.create(
        project_id="project-1",
        wandb_external_project_id="wandb-project-1",
        wandb_run_id="bundle-run-1" if intake else "analysis-run-1",
        wandb_run_kind="bundle_import" if intake else "connectivity_analysis",
        run_subject_id="project-1" if intake else "analysis-1",
        analysis_id=None if intake else "analysis-1",
    )


def _evidence_authorization() -> AssistantEvidenceAuthorization:
    return AssistantEvidenceAuthorization.create(
        project_id_sha256=_sha("project-1"),
        analysis_id_sha256=_sha("analysis-1"),
        model_revision_id_sha256=_sha("revision-1"),
        destination="SELF_MANAGED",
        external_project_id_sha256=_sha("wandb-project-1"),
        scope_evidence_sha256=canonical_sha256(_target().scope_evidence.model_dump(mode="json")),
        snapshot_bundle_sha256=_sha("bundle-1"),
        current_authorizations_sha256=_sha("authorizations-1"),
        selected_evidence_sha256=_sha("selected-evidence-1"),
        authorized_asset_count=1,
        generalized_derivative_count=0,
    )


class _FakeExecutor:
    def __init__(
        self,
        *,
        association: AssistantRunAssociation,
        authorization: NativeAgentAuthorization,
    ) -> None:
        self.association = association
        self.authorization = authorization
        self.calls: list[dict[str, Any]] = []

    def execute(self, **request: Any) -> NativeSingleTurnAgentsResult:
        self.calls.append(request)
        if request["operation"] == "EVIDENCE_EXPLAIN":
            output = {
                "intent": request["intent"],
                "items": [{"evidence_id": "E1", "explanation_kind": "DIRECT_PAIR_METRIC"}],
            }
        else:
            output = {
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
                "limitations": [
                    (
                        "AI-extracted draft — requires human confirmation against the "
                        "cited source text."
                    )
                ],
            }
        return NativeSingleTurnAgentsResult(
            operation=request["operation"],
            output=output,
            output_sha256=canonical_sha256(output),
            provider_call_id_sha256=_sha("provider-call-1"),
            provider_model="model-1",
            input_tokens=10,
            output_tokens=4,
            elapsed_ms=25,
            trace=NativeAgentsTraceReceipt(
                agents_trace_id="trace_" + "a" * 32,
                weave_call_id="weave-call-1",
                association_id=self.association.association_id,
                wandb_run_id=self.association.wandb_run_id,
                trace_url=(
                    "https://wandb.example/eco-team/restricted-project/weave/traces/weave-call-1"
                ),
            ),
        )


def _start_and_locally_prepare(
    bridge: NativeAgentsAssistantBridge,
    *,
    association: AssistantRunAssociation,
    operation: str,
    evidence_authorization: AssistantEvidenceAuthorization | None,
) -> None:
    bridge.start(
        call_id="call-1",
        operation="assistant.request",
        inputs={
            "requested_operation": operation,
            "wandb_association": association.model_dump(mode="json"),
            "evidence_audit": (
                evidence_authorization.model_dump(mode="json")
                if evidence_authorization is not None
                else None
            ),
        },
    )
    for step in APPROVED_TRACE_STEPS[:4]:
        bridge.step(call_id="call-1", name=step, inputs={}, output={})


def test_bridge_invokes_native_executor_only_after_local_authorization_and_scope_recheck(
    tmp_path: Path,
) -> None:
    target = _target()
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    executors: list[_FakeExecutor] = []
    scope_checks = 0

    def factory(
        association: AssistantRunAssociation, authorization: NativeAgentAuthorization
    ) -> _FakeExecutor:
        executor = _FakeExecutor(association=association, authorization=authorization)
        executors.append(executor)
        return executor

    def verify_scope() -> None:
        nonlocal scope_checks
        scope_checks += 1

    bridge = NativeAgentsAssistantBridge(
        executor_factory=factory,
        target=target,
        credentials=credentials,
        scope_verifier=verify_scope,
        feedback_root=tmp_path / "feedback",
    )
    association = _association()
    authorization = _evidence_authorization()
    bridge.start(
        call_id="call-1",
        operation="assistant.request",
        inputs={
            "requested_operation": "evidence.explain",
            "wandb_association": association.model_dump(mode="json"),
            "evidence_audit": authorization.model_dump(mode="json"),
        },
    )
    with pytest.raises(RuntimeError, match="LOCAL_PREFLIGHT_INCOMPLETE"):
        bridge.complete(
            operation="evidence.explain",
            system_prompt="ignored",
            user_prompt="TRACE_EVIDENCE",
            context={"question_intent": "TRACE_EVIDENCE"},
            response_schema={"type": "object"},
        )
    assert executors == [] and scope_checks == 0

    for step in APPROVED_TRACE_STEPS[:4]:
        bridge.step(call_id="call-1", name=step, inputs={}, output={})
    response = bridge.complete(
        operation="evidence.explain",
        system_prompt="not forwarded",
        user_prompt="TRACE_EVIDENCE",
        context={
            "question_intent": "TRACE_EVIDENCE",
            "claims": [{"evidence_alias": "E1"}],
        },
        response_schema={"not": "forwarded"},
    )
    assert scope_checks == 1
    assert len(executors) == 1
    assert executors[0].calls == [
        {
            "operation": "EVIDENCE_EXPLAIN",
            "intent": "TRACE_EVIDENCE",
            "context": {
                "question_intent": "TRACE_EVIDENCE",
                "claims": [{"evidence_alias": "E1"}],
            },
        }
    ]
    assert executors[0].authorization.project_id_sha256 == _sha("project-1")
    assert executors[0].authorization.analysis_id_sha256 == _sha("analysis-1")
    assert response.provider_call_id == "sha256:" + _sha("provider-call-1")

    for step in APPROVED_TRACE_STEPS[4:]:
        bridge.step(call_id="call-1", name=step, inputs={}, output={})
    bridge.finish(call_id="call-1", output={"validated": True})
    trace_id = bridge.flush(call_id="call-1")
    readback = bridge.readback(trace_id=trace_id)
    assert readback.trace_id == "trace_" + "a" * 32
    assert readback.steps == list(APPROVED_TRACE_STEPS)
    assert readback.association == association


def test_bridge_supports_intake_run_subject_scope_without_analysis_id(tmp_path: Path) -> None:
    target = _target()
    association = _association(intake=True)
    observed: list[_FakeExecutor] = []

    def factory(
        current: AssistantRunAssociation, authorization: NativeAgentAuthorization
    ) -> _FakeExecutor:
        executor = _FakeExecutor(association=current, authorization=authorization)
        observed.append(executor)
        return executor

    bridge = NativeAgentsAssistantBridge(
        executor_factory=factory,
        target=target,
        credentials=InMemoryCredentialSession(environment_key="WANDB_API_KEY"),
        scope_verifier=lambda: None,
        feedback_root=tmp_path / "feedback",
    )
    _start_and_locally_prepare(
        bridge,
        association=association,
        operation="evidence_intake.extract",
        evidence_authorization=None,
    )
    bridge.complete(
        operation="evidence_intake.extract",
        system_prompt="ignored",
        user_prompt="Extract purpose.",
        context={
            "pages": [{"document_id": "DOC1", "page_number": 1, "text": "Review purpose"}],
            "allowed_fields": ["purpose"],
        },
        response_schema={"type": "object"},
    )
    assert observed[0].authorization.analysis_id_sha256 == _sha("project-1")
    assert observed[0].calls[0]["operation"] == "EVIDENCE_INTAKE"


def test_native_post_provider_rejection_retains_verified_agents_trace_receipt(
    tmp_path: Path,
) -> None:
    target = _target()
    association = _association()

    def factory(
        current: AssistantRunAssociation, authorization: NativeAgentAuthorization
    ) -> _FakeExecutor:
        return _FakeExecutor(association=current, authorization=authorization)

    bridge = NativeAgentsAssistantBridge(
        executor_factory=factory,
        target=target,
        credentials=InMemoryCredentialSession(environment_key="WANDB_API_KEY"),
        scope_verifier=lambda: None,
        feedback_root=tmp_path / "feedback",
    )
    _start_and_locally_prepare(
        bridge,
        association=association,
        operation="evidence.explain",
        evidence_authorization=_evidence_authorization(),
    )
    bridge.complete(
        operation="evidence.explain",
        system_prompt="ignored",
        user_prompt="TRACE_EVIDENCE",
        context={"question_intent": "TRACE_EVIDENCE", "claims": []},
        response_schema={"type": "object"},
    )
    bridge.abort(call_id="call-1", error_code="ASSISTANT_FABRICATED_EVIDENCE")
    receipt = bridge.aborted_readback(call_id="call-1")
    assert receipt["completed"] is True
    assert receipt["trace_readback_verified"] is True
    assert receipt["answer_returned"] is False
    assert receipt["trace_id"] == "trace_" + "a" * 32
    assert receipt["weave_call_id"] == "weave-call-1"
    assert receipt["association"] == association.model_dump(mode="json")
    with pytest.raises(RuntimeError, match="CALL_NOT_STARTED"):
        bridge.flush(call_id="call-1")


def test_packaged_runtime_uses_native_bridges_and_legacy_requires_explicit_test_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _target()
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    runtime = ProviderBackedAssistantRuntime(
        scope_resolver=ProjectScopeResolver(),
        observability_credentials=credentials,
    )
    monkeypatch.setattr(runtime, "verify_project", lambda _root: ("project-1", target))
    monkeypatch.setattr(
        runtime,
        "_native_capability_preflight",
        lambda **_kwargs: ReviewBriefAgentCapabilityReceipt(
            provider_mode="CHAT_COMPLETIONS",
            trace_id="trace_" + "b" * 32,
            trace_url=("https://wandb.example/eco-team/restricted-project/weave/traces/preflight"),
        ),
    )
    runtime.configure(
        project_root=tmp_path,
        configuration=AssistantProviderConfiguration(
            base_url="https://provider.example/v1",
            model="model-1",
        ),
        api_key="provider-test-key-123456",
    )
    session = runtime._session(tmp_path)
    assert isinstance(session.evidence.provider, NativeAgentsAssistantBridge)
    assert session.evidence.trace_sink is session.evidence.provider
    assert isinstance(session.intake.provider, NativeAgentsAssistantBridge)
    assert session.intake.trace_sink is session.intake.provider
    assert session.intake.provider is not session.evidence.provider
    runtime.clear(project_root=tmp_path)

    provider_factory = lambda _configuration, _credentials: object()
    trace_factory = lambda _target, _root: object()
    with pytest.raises(ValueError, match="provided together"):
        ProviderBackedAssistantRuntime(
            scope_resolver=ProjectScopeResolver(),
            observability_credentials=credentials,
            provider_factory=provider_factory,
        )
    with pytest.raises(ValueError, match="test-only seam"):
        ProviderBackedAssistantRuntime(
            scope_resolver=ProjectScopeResolver(),
            observability_credentials=credentials,
            provider_factory=provider_factory,
            trace_factory=trace_factory,
        )
    legacy = ProviderBackedAssistantRuntime(
        scope_resolver=ProjectScopeResolver(),
        observability_credentials=credentials,
        provider_factory=provider_factory,
        trace_factory=trace_factory,
        allow_legacy_test_seam=True,
    )
    assert legacy._use_native_agents is False


def test_feedback_attaches_only_after_exact_native_trace_and_run_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import weave

    target = _target()
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    with credentials.candidate("wandb-test-key-123456"):
        pass
    payload = {
        "client_feedback_id": "feedback-request-1",
        "rating": "HELPFUL",
        "comment": "The evidence links were useful.",
    }

    class Feedback:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.records: list[dict[str, Any]] = []
            self.weave_ref = "weave:///eco-team/restricted-project/call/weave-call-1"

        def __iter__(self):
            return iter(self.records)

        def add(self, feedback_type: str, value: dict[str, str]) -> str:
            self.calls.append((feedback_type, value))
            self.records.append(
                {
                    "id": "feedback-1",
                    "feedback_type": feedback_type,
                    "payload": value,
                    "weave_ref": self.weave_ref,
                }
            )
            return "feedback-1"

    feedback = Feedback()
    record = type(
        "TraceRecord",
        (),
        {
            "id": "weave-call-1",
            "attributes": {"agent_trace_id": "trace_" + "a" * 32},
            "wb_run_id": "eco-team/restricted-project/analysis-run-1",
            "output": {"status": "completed"},
            "feedback": feedback,
        },
    )()

    class Client:
        def __init__(self) -> None:
            self.flushes = 0

        def get_call(self, call_id: str) -> Any:
            assert call_id == "weave-call-1"
            return record

        def flush(self) -> None:
            self.flushes += 1

        def get_feedback(self, feedback_id: str) -> list[dict[str, Any]]:
            assert feedback_id == "feedback-1"
            return list(feedback.records)

    client = Client()
    monkeypatch.setattr(weave, "init", lambda *args, **kwargs: client)
    scope_checks = 0

    def verify_scope() -> None:
        nonlocal scope_checks
        scope_checks += 1

    receipt = attach_verified_weave_feedback(
        target=target,
        credentials=credentials,
        scope_verifier=verify_scope,
        weave_call_id="weave-call-1",
        agents_trace_id="trace_" + "a" * 32,
        wandb_run_id="analysis-run-1",
        rating="HELPFUL",
        comment="The evidence links were useful.",
        feedback_type="ecocorridor.practitioner_usefulness",
        client_feedback_id="feedback-request-1",
    )
    assert receipt["verified"] is True
    assert receipt["weave_feedback_id"] == "feedback-1"
    assert feedback.calls == [("ecocorridor.practitioner_usefulness", payload)]
    assert client.flushes == 1
    assert scope_checks == 2

    repeated = attach_verified_weave_feedback(
        target=target,
        credentials=credentials,
        scope_verifier=verify_scope,
        weave_call_id="weave-call-1",
        agents_trace_id="trace_" + "a" * 32,
        wandb_run_id="analysis-run-1",
        rating="HELPFUL",
        comment="The evidence links were useful.",
        feedback_type="ecocorridor.practitioner_usefulness",
        client_feedback_id="feedback-request-1",
    )
    assert repeated == receipt
    assert len(feedback.calls) == 1
    assert client.flushes == 1

    with pytest.raises(RuntimeError, match="FEEDBACK_ID_REUSED"):
        attach_verified_weave_feedback(
            target=target,
            credentials=credentials,
            scope_verifier=verify_scope,
            weave_call_id="weave-call-1",
            agents_trace_id="trace_" + "a" * 32,
            wandb_run_id="analysis-run-1",
            rating="NOT_HELPFUL",
            comment="The evidence links were useful.",
            feedback_type="ecocorridor.practitioner_usefulness",
            client_feedback_id="feedback-request-1",
        )
    assert len(feedback.calls) == 1

    record.wb_run_id = "eco-team/restricted-project/wrong-run"
    with pytest.raises(RuntimeError, match="FEEDBACK_TRACE_INVALID"):
        attach_verified_weave_feedback(
            target=target,
            credentials=credentials,
            scope_verifier=verify_scope,
            weave_call_id="weave-call-1",
            agents_trace_id="trace_" + "a" * 32,
            wandb_run_id="analysis-run-1",
            rating="HELPFUL",
            comment="The evidence links were useful.",
            feedback_type="ecocorridor.practitioner_usefulness",
            client_feedback_id="feedback-request-1",
        )
