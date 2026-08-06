from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents.models.interface import Model
from ecocorridor_core.assistants import AssistantRunAssociation
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.review_brief import (
    ReviewBriefAuthorizedSnapshot,
    ReviewBriefFindingContext,
    ReviewBriefFindingSelection,
    ReviewBriefRunnerRequest,
    ReviewBriefRunnerResult,
    ReviewBriefSafetyError,
    ReviewBriefSourceMode,
    ReviewBriefTopic,
    ReviewBriefTrace,
    _feasibility_topics,
    required_review_brief_tools,
    validate_selection_appropriateness,
)
from ecocorridor_core.review_brief_agent_process import (
    _AgentSelectionOutput,
    _associated_wandb_run,
    _build_tools,
    _evidence_payload,
    _execute,
    _finding_payload,
    _get_trace_call_with_retry,
    _InvocationContext,
    _model,
    _raise_stage_failure,
    _readback_trace,
    _ReceiptWeaveTracingProcessor,
    _scope_payload,
    _usage,
    _validate_trace_gate,
    _validate_trace_url,
    review_brief_model_visible_context,
    review_brief_model_visible_context_bytes,
)
from ecocorridor_core.review_brief_agent_runtime import (
    REVIEW_BRIEF_AGENT_TOOLS,
    REVIEW_BRIEF_MAX_CONTEXT_BYTES,
    REVIEW_BRIEF_MAX_TOOL_CALLS,
    REVIEW_BRIEF_MAX_TURNS,
    PopenReviewBriefAgentSubprocess,
    ReviewBriefAgentBinding,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentChildRequest,
    ReviewBriefAgentChildResponse,
    ReviewBriefAgentCredentialLease,
    ReviewBriefAgentProvider,
    ReviewBriefAgentRuntimeError,
    ReviewBriefAgentTraceControl,
    ReviewBriefSubprocessRunner,
    ReviewBriefWeaveTarget,
    _communicate_with_deadline,
    _configure_weave_pii_redaction,
    _json_bytes,
    _read_frame,
    _trace_url_matches_target,
    _write_frame,
    safe_child_error_code,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [
        ("BadRequestError", "REVIEW_BRIEF_PROVIDER_REQUEST_REJECTED"),
        ("AuthenticationError", "REVIEW_BRIEF_PROVIDER_AUTHENTICATION_FAILED"),
        ("PermissionDeniedError", "REVIEW_BRIEF_PROVIDER_ACCESS_DENIED"),
        ("NotFoundError", "REVIEW_BRIEF_PROVIDER_MODEL_NOT_FOUND"),
        ("RateLimitError", "REVIEW_BRIEF_PROVIDER_RATE_LIMITED"),
        ("APIConnectionError", "REVIEW_BRIEF_PROVIDER_UNAVAILABLE"),
        ("InternalServerError", "REVIEW_BRIEF_PROVIDER_FAILED"),
        ("ModelBehaviorError", "REVIEW_BRIEF_PROVIDER_OUTPUT_INVALID"),
    ],
)
def test_safe_child_error_code_classifies_provider_failures_without_message(
    exception_name: str, expected: str
) -> None:
    error_type = type(exception_name, (Exception,), {})
    error = error_type("credential-and-provider-payload-must-not-escape")

    observed = safe_child_error_code(error)

    assert observed == expected
    assert "credential" not in observed.casefold()


def test_stage_failure_preserves_provider_classification_and_bounds_generic_error() -> None:
    bad_request = type("BadRequestError", (Exception,), {})
    with pytest.raises(
        ReviewBriefAgentRuntimeError,
        match="^REVIEW_BRIEF_PROVIDER_REQUEST_REJECTED$",
    ):
        _raise_stage_failure(
            bad_request("provider payload must not escape"),
            fallback_code="REVIEW_BRIEF_AGENT_EXECUTION_FAILED",
        )

    with pytest.raises(
        ReviewBriefAgentRuntimeError,
        match="^REVIEW_BRIEF_AGENT_CONFIGURATION_FAILED$",
    ):
        _raise_stage_failure(
            TypeError("internal payload must not escape"),
            fallback_code="REVIEW_BRIEF_AGENT_CONFIGURATION_FAILED",
        )


def test_weave_pii_redaction_uses_offline_cached_presidio_engine(tmp_path: Path) -> None:
    import tldextract
    import weave.utils.pii_redaction as weave_pii

    _configure_weave_pii_redaction(tmp_path)
    first = weave_pii._get_engines()
    second = weave_pii._get_engines()
    digest = "0" * 64
    redacted = weave_pii.redact_pii_string(
        f"Contact alice@example.com in Seattle. Evidence digest {digest}."
    )

    assert first is second
    assert "alice@example.com" not in redacted
    assert "Seattle" not in redacted
    assert digest in redacted
    assert os.environ["WEAVE_REDACT_PII_EXCLUDE_FIELDS"] == "CRYPTO"
    assert tuple(tldextract.extract.suffix_list_urls) == ()
    assert (tmp_path / "tldextract-cache").is_dir()


def _snapshot(*, evidence_aliases: list[str] | None = None) -> ReviewBriefAuthorizedSnapshot:
    evidence = evidence_aliases or ["E1"]
    finding = ReviewBriefFindingContext(
        finding_alias="F1",
        finding_kind="MODEL_FITNESS",
        review_topic="MODEL_FITNESS_BASIS",
        priority="BLOCKING",
        evidence_aliases=evidence,
        scenario_aliases=["S1"],
        permitted_actions=["INSPECT_EVIDENCE", "REVISE_MODEL_AND_RERUN"],
        permitted_disciplines=["ECOLOGY"],
    )
    return ReviewBriefAuthorizedSnapshot.create(
        source_mode="RIDGEWAY_SYNTHETIC",
        disposition="SURVEY_FIRST",
        analysis_id_sha256=_sha("analysis-1"),
        model_revision_id_sha256=_sha("revision-1"),
        evidence_authorization_decision_id="evidence_auth_" + "a" * 24,
        current_authorizations_sha256=_sha("authorizations-1"),
        authorization_sha256=_sha("authorization-1"),
        findings=[finding.model_dump(mode="json")],
    )


def _binding(snapshot: ReviewBriefAuthorizedSnapshot) -> ReviewBriefAgentBinding:
    return ReviewBriefAgentBinding.from_authorized_snapshot(
        project_id="project-1",
        analysis_id="analysis-1",
        portfolio_id="portfolio-1",
        model_revision_id="revision-1",
        decision_board_sha256=_sha("board-1"),
        cloud_authorization_kind="RIDGEWAY_CC0_TRACE_ONLY",
        cloud_authorization_sha256=_sha("ridgeway-trace-consent-1"),
        snapshot=snapshot,
    )


def _provider() -> ReviewBriefAgentProvider:
    return ReviewBriefAgentProvider(
        base_url="https://api.openai.example/v1",
        model="review-model-1",
        mode="CHAT_COMPLETIONS",
    )


def _target() -> ReviewBriefWeaveTarget:
    return ReviewBriefWeaveTarget(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        external_project_id_sha256=_sha("wandb-project-1"),
    )


def _association() -> AssistantRunAssociation:
    return AssistantRunAssociation.create(
        project_id="project-1",
        wandb_external_project_id="wandb-project-1",
        wandb_run_id="review-run-1",
        wandb_run_kind="review_brief_evaluation",
        run_subject_id="portfolio-1",
        analysis_id="analysis-1",
    )


def _trace() -> ReviewBriefAgentTraceControl:
    association = _association()
    return ReviewBriefAgentTraceControl(
        trace_id="trace_" + "c" * 32,
        association_id=association.association_id,
        association_id_sha256=_sha(association.association_id),
        wandb_run_id=association.wandb_run_id,
        wandb_run_id_sha256=_sha(association.wandb_run_id),
    )


def _child(*, action: str = "PREPARE") -> ReviewBriefAgentChildRequest:
    snapshot = _snapshot()
    return ReviewBriefAgentChildRequest(
        action=action,
        provider=_provider(),
        weave_target=_target(),
        trace=_trace(),
        binding=_binding(snapshot),
        runner_request=ReviewBriefRunnerRequest(snapshot=snapshot).model_dump(mode="json"),
        provider_api_key="provider-test-key-123456",
        wandb_api_key="not-a-secret-value",
    )


def test_provider_and_weave_targets_require_https_except_loopback() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ReviewBriefAgentProvider(
            base_url="http://provider.example/v1",
            model="model-1",
            mode="RESPONSES",
        )
    loopback = ReviewBriefAgentProvider(
        base_url="http://127.0.0.1:11434/v1",
        model="model-1",
        mode="CHAT_COMPLETIONS",
    )
    assert loopback.base_url == "http://127.0.0.1:11434/v1"
    with pytest.raises(ValueError, match="HTTPS"):
        ReviewBriefWeaveTarget(
            entity="wandb",
            project="eco",
            api_base_url="http://wandb.example",
            web_base_url="https://wandb.example",
            external_project_id_sha256=_sha("project"),
        )


def test_cloud_authorization_kind_is_discriminated_by_source_mode() -> None:
    synthetic = _snapshot()
    with pytest.raises(ValueError, match="cloud authorization kind"):
        ReviewBriefAgentBinding.from_authorized_snapshot(
            project_id="project-1",
            analysis_id="analysis-1",
            portfolio_id="portfolio-1",
            model_revision_id="revision-1",
            decision_board_sha256=_sha("board-1"),
            cloud_authorization_kind="RESTRICTED_EXACT_SCOPE",
            cloud_authorization_sha256=_sha("wrong-professional-scope"),
            snapshot=synthetic,
        )

    professional = synthetic.model_copy(update={"source_mode": ReviewBriefSourceMode.PROFESSIONAL})
    with pytest.raises(ValueError, match="cloud authorization kind"):
        ReviewBriefAgentBinding.from_authorized_snapshot(
            project_id="project-1",
            analysis_id="analysis-1",
            portfolio_id="portfolio-1",
            model_revision_id="revision-1",
            decision_board_sha256=_sha("board-1"),
            cloud_authorization_kind="RIDGEWAY_CC0_TRACE_ONLY",
            cloud_authorization_sha256=_sha("wrong-synthetic-consent"),
            snapshot=professional,
        )


def test_ipc_serialization_reveals_secrets_only_in_explicit_frame() -> None:
    child = _child()
    provider_key = child.provider_api_key.get_secret_value()
    wandb_key = child.wandb_api_key.get_secret_value()
    assert provider_key not in repr(child)
    assert wandb_key not in repr(child)
    assert provider_key not in child.model_dump_json()
    assert wandb_key not in child.model_dump_json()

    payload = bytearray(_json_bytes(child.ipc_payload()))
    try:
        assert provider_key.encode() in payload
        assert wandb_key.encode() in payload
        decoded = ReviewBriefAgentChildRequest.model_validate_json(payload)
        assert decoded.provider_api_key.get_secret_value() == provider_key
        assert decoded.wandb_api_key.get_secret_value() == wandb_key
    finally:
        payload[:] = b"\0" * len(payload)
    assert set(payload) == {0}


def test_model_routes_only_wandb_inference_to_the_weave_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            requests.append(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    wandb_child = _child().model_copy(
        update={
            "provider": _provider().model_copy(
                update={"base_url": "https://api.inference.wandb.ai/v1"}
            )
        }
    )
    _model(wandb_child)
    _model(_child())

    assert requests[0]["project"] == "wandb/ecocorridor-scenario-studio"
    assert "project" not in requests[1]
    routing_metadata = json.dumps(
        {key: value for key, value in requests[0].items() if key != "api_key"},
        sort_keys=True,
    )
    assert wandb_child.provider_api_key.get_secret_value() not in routing_metadata
    assert wandb_child.wandb_api_key.get_secret_value() not in routing_metadata


def test_owned_child_run_uses_hash_only_config_and_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    child.trace.wandb_run_lifecycle = "CREATE_OR_RESUME_AND_FINISH"
    calls: dict[str, Any] = {}

    class Run:
        id = child.trace.wandb_run_id

        def finish(self, *, exit_code: int) -> None:
            calls.setdefault("finish", []).append(exit_code)

    def init(**kwargs: Any) -> Run:
        calls["init"] = kwargs
        return Run()

    fake_wandb = SimpleNamespace(Settings=lambda **kwargs: kwargs, init=init)
    monkeypatch.setitem(sys.modules, "wandb", fake_wandb)
    with _associated_wandb_run(child) as run:
        assert isinstance(run, Run)

    init_args = calls["init"]
    assert init_args["id"] == child.trace.wandb_run_id
    assert init_args["resume"] == "allow"
    assert init_args["save_code"] is False
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in init_args["config"].values())
    serialized_config = json.dumps(init_args["config"], sort_keys=True)
    assert "project-1" not in serialized_config
    assert "portfolio-1" not in serialized_config
    assert calls["finish"] == [0]


def test_frame_protocol_is_strict_and_bounded() -> None:
    stream = io.BytesIO()
    _write_frame(stream, b'{"ok":true}')
    stream.seek(0)
    assert _read_frame(stream) == b'{"ok":true}'
    with pytest.raises(ReviewBriefAgentRuntimeError, match="FRAME_INVALID"):
        _read_frame(io.BytesIO(b"not-frame\n"))
    with pytest.raises(ReviewBriefAgentRuntimeError, match="FRAME_TRUNCATED"):
        _read_frame(io.BytesIO(b"00000008\n{}"))


def test_source_and_frozen_commands_never_contain_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = PopenReviewBriefAgentSubprocess(scratch_root=tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert launcher.command() == [
        sys.executable,
        "-I",
        "-m",
        "ecocorridor_core.review_brief_agent_process",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launcher.command() == [sys.executable, "--review-brief-agent-subprocess"]
    assert "provider-test-key" not in json.dumps(launcher.command())
    environment = launcher._environment(tmp_path)
    assert "WANDB_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_deadline_kills_the_isolated_process_group_and_reaps_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Process:
        pid = 9142
        stdin = None
        stdout = None
        stderr = None

        def poll(self) -> None:
            return None

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            calls.append(("communicate", timeout))
            if timeout != 5:
                raise subprocess.TimeoutExpired("agent", timeout)
            return b"", b""

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> int:
            calls.append(("wait", timeout))
            return -signal.SIGKILL

    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    with pytest.raises(ReviewBriefAgentRuntimeError, match="DEADLINE_EXCEEDED"):
        _communicate_with_deadline(
            Process(),  # type: ignore[arg-type]
            deadline_seconds=12,
            deadline_error_code="REVIEW_BRIEF_AGENT_DEADLINE_EXCEEDED",
        )
    assert ("killpg", 9143, signal.SIGKILL) in calls
    assert ("communicate", 5) in calls


class _FakeSubprocess:
    def __init__(self) -> None:
        self.requests: list[ReviewBriefAgentChildRequest] = []

    def invoke(
        self, *, request: ReviewBriefAgentChildRequest, deadline_seconds: float
    ) -> ReviewBriefAgentChildResponse:
        assert deadline_seconds == 45
        self.requests.append(request)
        if request.action == "PREFLIGHT":
            return ReviewBriefAgentChildResponse(
                ok=True,
                capability=ReviewBriefAgentCapabilityReceipt(
                    provider_mode=request.provider.mode,
                    trace_id=request.trace.trace_id,
                    trace_url=(
                        "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/preflight"
                    ),
                ),
            )
        result = ReviewBriefRunnerResult(
            snapshot_sha256=request.runner_request["snapshot"]["snapshot_sha256"],
            selections=[
                ReviewBriefFindingSelection(
                    finding_alias="F1",
                    action_kind="INSPECT_EVIDENCE",
                    reviewer_disciplines=["ECOLOGY"],
                )
            ],
            provider_call_id_sha256=_sha("provider-call-1"),
            provider_model=request.provider.model,
            tool_calls=[
                "review_brief_get_scope",
                "review_brief_list_findings",
                "review_brief_get_findings",
                "review_brief_list_model_fitness_gaps",
                "review_brief_resolve_evidence_aliases",
            ],
            input_tokens=10,
            output_tokens=4,
            elapsed_ms=100,
            trace=ReviewBriefTrace(
                trace_id=request.trace.trace_id,
                weave_call_id="weave-call-prepare",
                association_id=request.trace.association_id,
                wandb_run_id=request.trace.wandb_run_id,
                trace_url=(
                    "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/prepare"
                ),
            ),
        )
        return ReviewBriefAgentChildResponse(ok=True, result=result.model_dump(mode="json"))


def _runner(
    tmp_path: Path,
    fake: _FakeSubprocess,
    snapshot: ReviewBriefAuthorizedSnapshot,
) -> ReviewBriefSubprocessRunner:
    @contextmanager
    def credentials() -> Iterator[ReviewBriefAgentCredentialLease]:
        lease = ReviewBriefAgentCredentialLease(
            provider_api_key="provider-test-key-123456",
            wandb_api_key="not-a-secret-value",
        )
        yield lease
        lease.provider_api_key = ""
        lease.wandb_api_key = ""

    return ReviewBriefSubprocessRunner(
        provider=_provider(),
        weave_target=_target(),
        association=_association(),
        binding=_binding(snapshot),
        credential_lease=credentials,
        subprocess_client=fake,
        deadline_seconds=45,
    )


def test_runner_preflight_and_prepare_use_predetermined_distinct_traces(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    request = ReviewBriefRunnerRequest(snapshot=snapshot)
    subprocess_client = _FakeSubprocess()
    runner = _runner(tmp_path, subprocess_client, snapshot)

    capability = runner.preflight(request=request)
    result = runner.prepare(request=request)

    assert capability.trace_readback_verified is True
    assert result.trace.readback_verified is True
    assert len(subprocess_client.requests) == 2
    assert subprocess_client.requests[0].trace.trace_id != (
        subprocess_client.requests[1].trace.trace_id
    )
    assert all(
        value.startswith("trace_")
        for value in (
            subprocess_client.requests[0].trace.trace_id,
            subprocess_client.requests[1].trace.trace_id,
        )
    )
    assert (
        subprocess_client.requests[1]
        .provider_api_key.get_secret_value()
        .startswith("provider-test")
    )


def test_prepare_automatically_blocks_behind_real_capability_preflight(tmp_path: Path) -> None:
    snapshot = _snapshot()
    subprocess_client = _FakeSubprocess()
    runner = _runner(tmp_path, subprocess_client, snapshot)
    runner.prepare(request=ReviewBriefRunnerRequest(snapshot=snapshot))
    assert [request.action for request in subprocess_client.requests] == [
        "PREFLIGHT",
        "PREPARE",
    ]


def test_runner_blocks_snapshot_authorization_drift_before_subprocess(tmp_path: Path) -> None:
    snapshot = _snapshot()
    subprocess_client = _FakeSubprocess()
    runner = _runner(tmp_path, subprocess_client, snapshot)
    object.__setattr__(runner.binding, "authorization_sha256", _sha("changed"))
    request = ReviewBriefRunnerRequest(snapshot=snapshot)
    with pytest.raises(ReviewBriefAgentRuntimeError, match="AUTHORIZATION_LEASE_CHANGED"):
        runner.prepare(request=request)
    assert subprocess_client.requests == []


def test_runner_enforces_64_kib_authorized_context_before_credentials(tmp_path: Path) -> None:
    aliases = [f"E{index}" for index in range(1, 65)]
    findings = [
        ReviewBriefFindingContext(
            finding_alias=f"F{index}",
            finding_kind="MODEL_FITNESS",
            review_topic="MODEL_FITNESS_BASIS",
            priority="BLOCKING",
            evidence_aliases=aliases,
            scenario_aliases=["S1"],
            permitted_actions=["INSPECT_EVIDENCE", "REVISE_MODEL_AND_RERUN"],
            permitted_disciplines=["ECOLOGY"],
        )
        for index in range(1, 65)
    ]
    snapshot = ReviewBriefAuthorizedSnapshot.create(
        source_mode="RIDGEWAY_SYNTHETIC",
        disposition="SURVEY_FIRST",
        analysis_id_sha256=_sha("analysis-1"),
        model_revision_id_sha256=_sha("revision-1"),
        evidence_authorization_decision_id="evidence_auth_" + "a" * 24,
        current_authorizations_sha256=_sha("authorizations-1"),
        authorization_sha256=_sha("authorization-1"),
        findings=[item.model_dump(mode="json") for item in findings],
    )
    binding = _binding(snapshot)
    accessed = False

    def credentials() -> ReviewBriefAgentCredentialLease:
        nonlocal accessed
        accessed = True
        return ReviewBriefAgentCredentialLease("p" * 20, "w" * 20)

    runner = ReviewBriefSubprocessRunner(
        provider=_provider(),
        weave_target=_target(),
        association=_association(),
        binding=binding,
        credential_lease=credentials,
        subprocess_client=_FakeSubprocess(),
    )
    assert len(_json_bytes(snapshot.model_dump(mode="json"))) > REVIEW_BRIEF_MAX_CONTEXT_BYTES
    with pytest.raises(ReviewBriefAgentRuntimeError, match="CONTEXT_TOO_LARGE"):
        runner.prepare(request=ReviewBriefRunnerRequest(snapshot=snapshot))
    assert accessed is False


def test_runner_enforces_expanded_model_visible_context_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ecocorridor_core.review_brief_agent_process as process_module

    snapshot = _snapshot()
    subprocess_client = _FakeSubprocess()
    runner = _runner(tmp_path, subprocess_client, snapshot)
    monkeypatch.setattr(
        process_module,
        "review_brief_model_visible_context_bytes",
        lambda _snapshot: b"x" * (REVIEW_BRIEF_MAX_CONTEXT_BYTES + 1),
    )

    with pytest.raises(ReviewBriefAgentRuntimeError, match="CONTEXT_TOO_LARGE"):
        runner.prepare(request=ReviewBriefRunnerRequest(snapshot=snapshot))
    assert subprocess_client.requests == []


def test_all_and_only_eight_read_only_tools_are_registered() -> None:
    child = _child()
    request = ReviewBriefRunnerRequest.model_validate(child.runner_request)
    context = _InvocationContext(request=request, child=child)
    tools = _build_tools(context)
    assert tuple(tool.name for tool in tools) == REVIEW_BRIEF_AGENT_TOOLS
    assert len(tools) == 8
    assert all(tool.strict_json_schema for tool in tools)
    assert all(tool.timeout_seconds == 5.0 for tool in tools)
    assert all(tool.timeout_behavior == "raise_exception" for tool in tools)


def test_per_tool_revalidation_rejects_identity_alias_and_generic_arguments() -> None:
    child = _child()
    context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    context.revalidate(tool_name="review_brief_get_scope", raw_arguments="{}")
    context.revalidate(tool_name="review_brief_list_findings", raw_arguments="{}")
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TOOL_ALIAS_NOT_AUTHORIZED"):
        context.revalidate(
            tool_name="review_brief_get_findings",
            raw_arguments=json.dumps({"query": {"finding_aliases": ["F9"]}}),
        )
    assert context.inspected_finding_aliases == set()
    assert context.tool_calls == ["review_brief_get_scope", "review_brief_list_findings"]

    generic_context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TOOL_ARGUMENTS_INVALID"):
        generic_context.revalidate(
            tool_name="review_brief_get_scope",
            raw_arguments=json.dumps({"project_id": "project-1"}),
        )
    assert generic_context.tool_calls == []

    changed_context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    object.__setattr__(child.binding, "authorization_sha256", _sha("changed"))
    with pytest.raises(ReviewBriefAgentRuntimeError, match="AUTHORIZATION_LEASE_CHANGED"):
        changed_context.revalidate(tool_name="review_brief_get_scope", raw_arguments="{}")


def test_every_tool_call_rejects_an_expired_authorization_lease() -> None:
    child = _child()
    object.__setattr__(
        child.binding,
        "authorization_lease_expires_at",
        datetime.now(UTC) - timedelta(seconds=1),
    )
    object.__setattr__(
        child.binding,
        "authorization_lease_issued_at",
        datetime.now(UTC) - timedelta(seconds=2),
    )
    object.__setattr__(
        child.binding,
        "authorization_lease_sha256",
        canonical_sha256(child.binding.authorization_lease_payload()),
    )
    context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="AUTHORIZATION_LEASE_EXPIRED"):
        context.revalidate(tool_name="review_brief_get_scope", raw_arguments="{}")


def test_tool_order_and_model_turn_limits_leave_the_final_turn_for_output() -> None:
    assert REVIEW_BRIEF_MAX_TOOL_CALLS == 8
    assert REVIEW_BRIEF_MAX_TURNS == 8
    child = _child()
    context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TOOL_ORDER_INVALID"):
        context.revalidate(tool_name="review_brief_get_findings", raw_arguments="{}")
    assert context.tool_calls == []

    responses = [
        SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            response_id=f"response-{index}",
            request_id=None,
        )
        for index in range(REVIEW_BRIEF_MAX_TURNS + 1)
    ]
    with pytest.raises(ReviewBriefAgentRuntimeError, match="MODEL_TURN_LIMIT_EXCEEDED"):
        _usage(SimpleNamespace(raw_responses=responses))


def test_selection_requires_full_recall_authorized_actions_and_required_traversal() -> None:
    child = _child()
    context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request), child=child
    )
    selection = _AgentSelectionOutput(
        snapshot_sha256=context.snapshot.snapshot_sha256,
        selections=[
            ReviewBriefFindingSelection(
                finding_alias="F1",
                action_kind="INSPECT_EVIDENCE",
                reviewer_disciplines=["ECOLOGY"],
            )
        ],
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="REQUIRED_TRAVERSAL_MISSING"):
        context.validate_selection(selection)
    context.tool_calls = [
        "review_brief_get_scope",
        "review_brief_list_findings",
        "review_brief_get_findings",
        "review_brief_list_model_fitness_gaps",
        "review_brief_resolve_evidence_aliases",
    ]
    with pytest.raises(ReviewBriefAgentRuntimeError, match="FINDING_TRAVERSAL_INCOMPLETE"):
        context.validate_selection(selection)
    context.inspected_finding_aliases = {"F1"}
    with pytest.raises(ReviewBriefAgentRuntimeError, match="EVIDENCE_TRAVERSAL_INCOMPLETE"):
        context.validate_selection(selection)
    context.resolved_evidence_aliases = {"E1"}
    context.validate_selection(selection)
    assert context.output_validated is True
    bad = selection.model_copy(
        update={
            "selections": [
                ReviewBriefFindingSelection(
                    finding_alias="F1",
                    action_kind="FIELD_VERIFY",
                    reviewer_disciplines=["ECOLOGY"],
                )
            ]
        }
    )
    with pytest.raises(ReviewBriefAgentRuntimeError, match="ACTION_INAPPROPRIATE"):
        context.validate_selection(bad)


def test_closed_topics_drive_exact_conditional_traversal_within_turn_budget() -> None:
    findings = [
        ReviewBriefFindingContext(
            finding_alias="F1",
            finding_kind="FEASIBILITY_GAP",
            review_topic="LAND_ACCESS_OR_OWNERSHIP",
            priority="BEFORE_FEASIBILITY",
            evidence_aliases=["E1"],
            scenario_aliases=["S1"],
            permitted_actions=["FIELD_VERIFY", "RESOLVE_LAND_OR_OWNERSHIP"],
            permitted_disciplines=["FIELD", "LAND"],
        ),
        ReviewBriefFindingContext(
            finding_alias="F2",
            finding_kind="QA",
            review_topic="DIRECT_RUN_QA",
            priority="BEFORE_FEASIBILITY",
            evidence_aliases=["E2"],
            scenario_aliases=["S2"],
            permitted_actions=["INSPECT_EVIDENCE", "REVISE_MODEL_AND_RERUN"],
            permitted_disciplines=["ECOLOGY", "ENGINEERING"],
        ),
        ReviewBriefFindingContext(
            finding_alias="F3",
            finding_kind="REFERENCE",
            review_topic="REFERENCE_COMPARISON",
            priority="BEFORE_FEASIBILITY",
            evidence_aliases=["E3"],
            scenario_aliases=["S3"],
            permitted_actions=["INSPECT_EVIDENCE"],
            permitted_disciplines=["ECOLOGY"],
        ),
        ReviewBriefFindingContext(
            finding_alias="F4",
            finding_kind="AUTHORIZATION",
            review_topic="DATA_AUTHORIZATION",
            priority="BLOCKING",
            evidence_aliases=["E4"],
            scenario_aliases=["S4"],
            permitted_actions=["INSPECT_EVIDENCE", "RESOLVE_AUTHORIZATION"],
            permitted_disciplines=["LAND", "CULTURAL", "PERMITTING"],
        ),
    ]
    snapshot = ReviewBriefAuthorizedSnapshot.create(
        source_mode="RIDGEWAY_SYNTHETIC",
        disposition="SURVEY_FIRST",
        analysis_id_sha256=_sha("analysis-1"),
        model_revision_id_sha256=_sha("revision-1"),
        evidence_authorization_decision_id="evidence_auth_" + "a" * 24,
        current_authorizations_sha256=_sha("authorizations-1"),
        authorization_sha256=_sha("authorization-1"),
        findings=[item.model_dump(mode="json") for item in findings],
    )

    assert required_review_brief_tools(snapshot) == (
        "review_brief_get_scope",
        "review_brief_list_findings",
        "review_brief_get_findings",
        "review_brief_list_option_constraints",
        "review_brief_list_model_fitness_gaps",
        "review_brief_get_reference_status",
        "review_brief_get_authorization_status",
        "review_brief_resolve_evidence_aliases",
    )
    assert len(required_review_brief_tools(snapshot)) == 8
    assert required_review_brief_tools(snapshot)[0] == "review_brief_get_scope"
    assert _finding_payload(snapshot.findings[0])["review_topic"] == (
        ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP.value
    )


def test_model_visible_context_is_incremental_closed_and_canonically_accounted() -> None:
    snapshot = _snapshot()
    scope = _scope_payload(snapshot)
    serialized_scope = json.dumps(scope, sort_keys=True)
    assert "F1" not in serialized_scope
    assert "MODEL_FITNESS" not in serialized_scope
    assert "MODEL_FITNESS_BASIS" not in serialized_scope
    assert "MODEL_FITNESS_BASIS_INCOMPLETE" not in serialized_scope

    detail = _finding_payload(snapshot.findings[0])
    assert detail["reason_facets"] == ["MODEL_FITNESS_BASIS_INCOMPLETE"]
    assert detail["scenario_breadth"] == "SINGLE_SCENARIO"
    assert detail["evidence_count"] == 1
    assert detail["scenario_count"] == 1
    assert detail["direct_run_count"] == 0
    resolved = _evidence_payload(snapshot, ["E1"])
    assert resolved == {
        "evidence": [
            {
                "evidence_alias": "E1",
                "evidence_classes": ["RETAINED_MODEL_FITNESS"],
                "finding_aliases": ["F1"],
            }
        ]
    }

    manifest = review_brief_model_visible_context(snapshot)
    assert manifest["schema_version"] == "review-brief-model-visible-context/v1"
    assert [item["tool_name"] for item in manifest["ordered_tool_exchanges"]] == list(
        required_review_brief_tools(snapshot)
    )
    assert manifest == review_brief_model_visible_context(snapshot)
    unsigned = {key: value for key, value in manifest.items() if key != "context_sha256"}
    assert manifest["context_sha256"] == canonical_sha256(unsigned)
    assert len(review_brief_model_visible_context_bytes(snapshot)) > 0
    raw = json.dumps(manifest, sort_keys=True)
    for forbidden in ("project-1", "analysis-1", "portfolio-1", "revision-1"):
        assert forbidden not in raw


def test_snapshot_fails_closed_when_one_call_alias_coverage_is_impossible() -> None:
    with pytest.raises(ValueError, match="at most 64"):
        _snapshot(evidence_aliases=[f"E{index}" for index in range(1, 66)])


def test_topic_validation_rejects_superficially_allowed_but_wrong_discipline() -> None:
    context = ReviewBriefFindingContext(
        finding_alias="F1",
        finding_kind="QA",
        review_topic="DIRECT_RUN_QA",
        priority="BEFORE_FEASIBILITY",
        evidence_aliases=["E1"],
        permitted_actions=["INSPECT_EVIDENCE", "REVISE_MODEL_AND_RERUN"],
        permitted_disciplines=["ECOLOGY", "ENGINEERING"],
    )
    selection = ReviewBriefFindingSelection(
        finding_alias="F1",
        action_kind="INSPECT_EVIDENCE",
        reviewer_disciplines=["ECOLOGY"],
    )
    with pytest.raises(ReviewBriefSafetyError, match="DISCIPLINE_INAPPROPRIATE"):
        validate_selection_appropriateness(context=context, selection=selection)


def test_feasibility_topic_matching_uses_word_and_phrase_boundaries() -> None:
    island_topics = _feasibility_topics("Island habitat uncertainty remains.")
    assert ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP not in island_topics
    assert ReviewBriefTopic.ECOLOGICAL_FIELD_RISK in island_topics
    assert ReviewBriefTopic.SURVEY_OR_FIELD_DATA in island_topics

    bounded = _feasibility_topics(
        "Land-access ownership and permitting require traffic engineering review."
    )
    assert ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP in bounded
    assert ReviewBriefTopic.PERMITTING_REVIEW in bounded
    assert ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY in bounded


class _FakeDelegate:
    def __init__(self) -> None:
        self._trace_data: dict[str, dict[str, Any]] = {}
        self._trace_calls: dict[str, Any] = {}
        self.finished_gate: dict[str, Any] | None = None

    def on_trace_start(self, trace: Any) -> None:
        self._trace_data[trace.trace_id] = {"metadata": {}, "metrics": {}}
        self._trace_calls[trace.trace_id] = SimpleNamespace(id="weave-call-1")

    def on_trace_end(self, trace: Any) -> None:
        self.finished_gate = dict(self._trace_data[trace.trace_id]["metadata"])

    def on_span_start(self, _span: Any) -> None:  # pragma: no cover - delegate contract
        return

    def on_span_end(self, _span: Any) -> None:  # pragma: no cover - delegate contract
        return

    def force_flush(self) -> None:
        return

    def shutdown(self) -> None:  # pragma: no cover - delegate contract
        return


def test_weave_processor_replaces_root_with_hash_only_gate_and_exact_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import weave.integrations.openai_agents.openai_agents as integration

    monkeypatch.setattr(integration, "WeaveTracingProcessor", _FakeDelegate)
    child = _child()
    context = _InvocationContext(
        request=ReviewBriefRunnerRequest.model_validate(child.runner_request),
        child=child,
        tool_calls=["review_brief_get_scope"],
        output_validated=True,
        output_sha256=_sha("validated-output"),
    )
    processor = _ReceiptWeaveTracingProcessor(context=context)
    trace = SimpleNamespace(trace_id=child.trace.trace_id)
    processor.on_trace_start(trace)
    processor.on_trace_end(trace)
    gate = processor._delegate.finished_gate["ecocorridor_trace_gate"]
    serialized = json.dumps(gate)
    assert "project-1" not in serialized
    assert "portfolio-1" not in serialized
    assert gate["project_id_sha256"] == _sha("project-1")
    assert gate["output_sha256"] == _sha("validated-output")
    assert gate["cloud_authorization_kind_sha256"] == _sha("RIDGEWAY_CC0_TRACE_ONLY")
    assert gate["cloud_authorization_sha256"] == _sha("ridgeway-trace-consent-1")
    assert all(key.endswith("_sha256") for key in gate)
    assert all(re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in gate.values())

    record = SimpleNamespace(
        attributes={"agent_trace_id": child.trace.trace_id},
        wb_run_id=("wandb/ecocorridor-scenario-studio/" + child.trace.wandb_run_id),
        output={"status": "completed", "metadata": {"ecocorridor_trace_gate": gate}},
        ui_url=("https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/weave-call-1"),
    )

    class Client:
        def __init__(self) -> None:
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1

        def get_call(self, call_id: str) -> Any:
            assert call_id == "weave-call-1"
            return record

    client = Client()
    trace_id, weave_call_id, trace_url = _readback_trace(
        client=client, processor=processor, context=context
    )
    assert trace_id == child.trace.trace_id
    assert weave_call_id == "weave-call-1"
    assert trace_url == record.ui_url
    assert client.flush_count == 1


def test_trace_gate_failures_identify_only_the_bounded_field() -> None:
    expected = {"snapshot_sha256": _sha("snapshot")}
    with pytest.raises(
        ReviewBriefAgentRuntimeError,
        match="^REVIEW_BRIEF_AGENT_TRACE_GATE_SNAPSHOT_SHA256_MISMATCH$",
    ):
        _validate_trace_gate({"snapshot_sha256": _sha("changed")}, expected)
    with pytest.raises(
        ReviewBriefAgentRuntimeError,
        match="^REVIEW_BRIEF_AGENT_TRACE_GATE_SCHEMA_MISMATCH$",
    ):
        _validate_trace_gate({"other_sha256": _sha("snapshot")}, expected)


def test_trace_readback_retries_bounded_visibility_lag() -> None:
    expected = object()

    class Client:
        def __init__(self) -> None:
            self.attempts = 0

        def get_call(self, call_id: str) -> object:
            assert call_id == "call-1"
            self.attempts += 1
            if self.attempts < 3:
                raise LookupError("not visible yet")
            return expected

    client = Client()
    assert (
        _get_trace_call_with_retry(
            client,
            "call-1",
            timeout_seconds=0.1,
            interval_seconds=0,
        )
        is expected
    )
    assert client.attempts == 3


def test_trace_readback_visibility_timeout_is_content_free() -> None:
    class Client:
        def get_call(self, _call_id: str) -> object:
            raise LookupError("sensitive trace response")

    with pytest.raises(
        ReviewBriefAgentRuntimeError,
        match="^REVIEW_BRIEF_AGENT_TRACE_FETCH_FAILED$",
    ):
        _get_trace_call_with_retry(
            Client(),
            "call-1",
            timeout_seconds=0,
            interval_seconds=0,
        )


@pytest.mark.parametrize("suffix", ["?download=1", "#other-trace"])
def test_review_brief_trace_receipt_rejects_query_and_fragment(suffix: str) -> None:
    child = _child()
    decorated = "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/call-1" + suffix
    assert _trace_url_matches_target(decorated, child.weave_target) is False
    with pytest.raises(ReviewBriefAgentRuntimeError, match="TRACE_URL_INVALID"):
        _validate_trace_url(decorated, child)


def test_child_process_malformed_frame_returns_safe_framed_rejection(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "ecocorridor_core.review_brief_agent_process"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(b"00000002\n{}", timeout=20)
    assert process.returncode == 0
    response = ReviewBriefAgentChildResponse.model_validate_json(_read_frame(io.BytesIO(stdout)))
    assert response.ok is False
    assert response.error_code == "REVIEW_BRIEF_AGENT_INVOCATION_FAILED"
    assert b"test-key" not in stderr


class _ScriptedAgentsModel(Model):
    """Pinned Agents SDK model seam that exercises real tool/structured-output turns."""

    def __init__(self, snapshot_sha256: str) -> None:
        self.snapshot_sha256 = snapshot_sha256
        self.turn = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import (
            ResponseFunctionToolCall,
            ResponseOutputMessage,
            ResponseOutputText,
        )

        calls = [
            ("review_brief_get_scope", {}),
            ("review_brief_list_findings", {}),
            (
                "review_brief_get_findings",
                {"query": {"finding_aliases": ["F1"]}},
            ),
            ("review_brief_list_model_fitness_gaps", {}),
            (
                "review_brief_resolve_evidence_aliases",
                {"query": {"evidence_aliases": ["E1"]}},
            ),
        ]
        if self.turn < len(calls):
            name, arguments = calls[self.turn]
            output = [
                ResponseFunctionToolCall(
                    arguments=json.dumps(arguments),
                    call_id=f"tool-call-{self.turn + 1}",
                    name=name,
                    type="function_call",
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id="message-1",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            annotations=[],
                            type="output_text",
                            text=json.dumps(
                                {
                                    "snapshot_sha256": self.snapshot_sha256,
                                    "selections": [
                                        {
                                            "finding_alias": "F1",
                                            "action_kind": "INSPECT_EVIDENCE",
                                            "reviewer_disciplines": ["ECOLOGY"],
                                        }
                                    ],
                                }
                            ),
                        )
                    ],
                )
            ]
        self.turn += 1
        return ModelResponse(
            output=output,
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5, requests=1),
            response_id=f"response-{self.turn}",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        async def never() -> Any:
            if False:  # pragma: no cover - makes this an async generator
                yield None

        return never()


class _FakeWeaveCall:
    def __init__(self, *, call_id: str, attributes: dict[str, Any], wb_run_id: str | None) -> None:
        self.id = call_id
        self.trace_id = "weave-" + call_id
        self.attributes = attributes
        self.wb_run_id = wb_run_id
        self.output: dict[str, Any] | None = None
        self.ui_url = "https://wandb.ai/wandb/ecocorridor-scenario-studio/weave/traces/" + call_id
        self.ended_at: str | None = None


class _FakeNativeWeaveClient:
    def __init__(self) -> None:
        self.calls: dict[str, _FakeWeaveCall] = {}
        self.counter = 0
        self.run_id: str | None = None

    def set_wandb_run_context(self, *, run_id: str, step: int | None = None) -> None:
        self.run_id = run_id

    def create_call(self, **kwargs: Any) -> _FakeWeaveCall:
        self.counter += 1
        call = _FakeWeaveCall(
            call_id=f"call-{self.counter}",
            attributes=dict(kwargs.get("attributes", {})),
            wb_run_id=(
                "wandb/ecocorridor-scenario-studio/" + self.run_id
                if self.run_id is not None
                else None
            ),
        )
        self.calls[call.id] = call
        return call

    def finish_call(
        self,
        call: _FakeWeaveCall,
        *,
        output: dict[str, Any] | None = None,
        exception: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        call.output = output or {"error": type(exception).__name__ if exception else None}
        call.ended_at = "done"

    def flush(self) -> None:
        return

    def get_call(self, call_id: str) -> _FakeWeaveCall:
        return self.calls[call_id]


def test_pinned_agents_sdk_executes_real_bounded_tools_and_installs_only_weave_processor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agents
    import ecocorridor_core.review_brief_agent_process as process_module
    import weave
    import weave.integrations.openai_agents.openai_agents as integration

    child = _child()
    client = _FakeNativeWeaveClient()
    installed: list[list[Any]] = []
    real_set = agents.set_trace_processors

    def set_processors(processors: list[Any]) -> None:
        installed.append(processors)
        real_set(processors)

    monkeypatch.setattr(agents, "set_trace_processors", set_processors)
    monkeypatch.setattr(weave, "init", lambda *args, **kwargs: client)
    monkeypatch.setattr(integration, "get_weave_client", lambda: client)
    monkeypatch.setattr(
        process_module,
        "_model",
        lambda _child: _ScriptedAgentsModel(_child.runner_request["snapshot"]["snapshot_sha256"]),
    )
    monkeypatch.setattr(process_module, "_configure_environment", lambda _child: None)
    try:
        response = asyncio.run(_execute(child))
    finally:
        real_set([])

    assert response.ok is True
    assert response.result is not None
    result = ReviewBriefRunnerResult.model_validate(response.result)
    assert result.trace.readback_verified is True
    assert result.trace.flushed is True
    assert result.selections[0].finding_alias == "F1"
    assert client.run_id == child.trace.wandb_run_id
    assert len(installed) == 1
    assert len(installed[0]) == 1
    assert isinstance(installed[0][0], _ReceiptWeaveTracingProcessor)
    assert installed[0][0].context.tool_calls == [
        "review_brief_get_scope",
        "review_brief_list_findings",
        "review_brief_get_findings",
        "review_brief_list_model_fitness_gaps",
        "review_brief_resolve_evidence_aliases",
    ]
