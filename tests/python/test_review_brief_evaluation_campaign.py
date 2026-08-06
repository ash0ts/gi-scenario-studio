from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.guided_evidence import GuidedEvidenceResource
from ecocorridor_core.native_agents_runtime import (
    NativeAgentsTraceReceipt,
    NativeSingleTurnAgentsResult,
    NativeSingleTurnChildResponse,
    PopenNativeSingleTurnSubprocess,
)
from ecocorridor_core.review_brief import (
    ReviewBriefFindingKind,
    ReviewBriefFindingSelection,
    ReviewBriefRunnerResult,
    ReviewBriefTopic,
    ReviewBriefTrace,
    build_authorized_snapshot,
    primary_disciplines_for_topic,
    render_review_brief,
    required_review_brief_tools,
    ridgeway_review_source,
    topic_permissions,
)
from ecocorridor_core.review_brief_agent_runtime import (
    PopenReviewBriefAgentSubprocess,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentChildResponse,
    ReviewBriefAgentProvider,
    ReviewBriefWeaveTarget,
)
from ecocorridor_core.review_brief_animation import (
    FILM_SLUG,
    MEDIA_MANIFEST_FILENAME,
    build_review_brief_animation_media_manifest,
    build_review_brief_animation_render_contract,
    build_review_brief_animation_validation_receipt,
    expected_review_brief_animation_files,
    render_review_brief_animation_html,
    render_review_brief_animation_transcript,
    validate_review_brief_animation_media_manifest,
)
from ecocorridor_core.review_brief_evaluation import (
    METHOD_IDS,
    NATIVE_AGENT_TOOL_NAMES,
    PRE_PROVIDER_BLOCK_CASES,
    DeterministicExhaustiveReviewBriefMethod,
    ProviderExecutionEvidence,
    ReviewBriefAnswer,
    ReviewBriefArtifactReceipt,
    ReviewBriefCampaignArtifact,
    ReviewBriefEvaluationDataset,
    ReviewBriefEvaluationDraftProbe,
    ReviewBriefExecutionObservation,
    ReviewBriefFinding,
    ReviewBriefMethodDeclaration,
    ReviewBriefMethodExecution,
    ReviewBriefProviderCostBasis,
    build_review_brief_animation_story,
    build_review_brief_campaign,
    build_review_brief_publication_plan,
    build_ridgeway_review_brief_dataset,
    expected_review_brief_answer,
    fail_closed_review_brief_execution,
    score_review_brief_execution,
    validate_review_brief_publication_plan,
    verify_review_brief_campaign,
)

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publisher = _script("publish_ecocorridor_review_brief_report")
producer = _script("run_ecocorridor_review_brief_evaluation")
animation_renderer = _script("render_ecocorridor_review_brief_animation")


@pytest.fixture(scope="module")
def ridgeway_resource() -> GuidedEvidenceResource:
    ridgeway = ROOT / "benchmarks" / "ecocorridor" / "ridgeway"
    projection = ridgeway / "ui_projection.json"
    manifest_path = ridgeway / "direct-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = ridgeway / str(manifest["evidence_bundle"]["filename"])
    return GuidedEvidenceResource.verify(
        projection_path=projection,
        projection_sha256=hashlib.sha256(projection.read_bytes()).hexdigest(),
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        archive_path=archive,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )


@pytest.fixture(scope="module")
def dataset(ridgeway_resource: GuidedEvidenceResource) -> ReviewBriefEvaluationDataset:
    return build_ridgeway_review_brief_dataset(ridgeway_resource)


def _declarations() -> list[ReviewBriefMethodDeclaration]:
    return [
        ReviewBriefMethodDeclaration(
            method_id="deterministic_exhaustive",
            implementation_sha256="a" * 64,
            provider_backed=False,
            method_description="Deterministic local exhaustive control.",
        ),
        ReviewBriefMethodDeclaration(
            method_id="existing_single_shot_full_context",
            provider="provider",
            model="model-1",
            prompt_ref="weave:///entity/project/object/single-shot:abcdefghijklmnop",
            implementation_sha256="b" * 64,
            provider_backed=True,
            method_description="Provider-backed strict single-shot full-context baseline.",
        ),
        ReviewBriefMethodDeclaration(
            method_id="native_read_only_agent",
            provider="provider",
            model="model-1",
            prompt_ref="weave:///entity/project/object/native-agent:abcdefghijklmnop",
            implementation_sha256="c" * 64,
            provider_backed=True,
            method_description="Native read-only Agents SDK method.",
        ),
    ]


def _provider_runtime() -> tuple[ReviewBriefAgentProvider, ReviewBriefWeaveTarget]:
    external_project_id = "external-project-1"
    return (
        ReviewBriefAgentProvider(
            base_url="https://provider.example/v1",
            model="model-1",
            mode="CHAT_COMPLETIONS",
            timeout_seconds=30,
        ),
        ReviewBriefWeaveTarget(
            entity="entity",
            project="project",
            api_base_url="https://api.wandb.ai",
            web_base_url="https://wandb.ai",
            external_project_id_sha256=hashlib.sha256(external_project_id.encode()).hexdigest(),
        ),
    )


class _CapturingSingleTurnSubprocess:
    def __init__(self, answer: ReviewBriefAnswer) -> None:
        self.answer = answer
        self.requests: list[Any] = []

    def invoke(self, *, request: Any, deadline_seconds: float) -> NativeSingleTurnChildResponse:
        assert deadline_seconds >= 5
        self.requests.append(request)
        output = self.answer.model_dump(mode="json")
        result = NativeSingleTurnAgentsResult(
            operation=request.payload.operation,
            output=output,
            output_sha256=canonical_sha256(output),
            provider_call_id_sha256="a" * 64,
            provider_model=request.provider.model,
            input_tokens=11,
            output_tokens=7,
            elapsed_ms=13,
            trace=NativeAgentsTraceReceipt(
                agents_trace_id=request.trace.trace_id,
                weave_call_id="call-baseline",
                association_id=request.trace.association_id,
                wandb_run_id=request.trace.wandb_run_id,
                trace_url=("https://wandb.ai/entity/project/weave/traces/trace-baseline"),
            ),
        )
        return NativeSingleTurnChildResponse(ok=True, result=result)


class _DatasetSingleTurnSubprocess:
    """Network-free provider double that answers from the scorer-owned case fixture."""

    def __init__(self, dataset: ReviewBriefEvaluationDataset) -> None:
        self.cases = {item.case_id: item for item in dataset.cases}
        self.requests: list[Any] = []

    def invoke(self, *, request: Any, deadline_seconds: float) -> NativeSingleTurnChildResponse:
        assert deadline_seconds >= 5
        self.requests.append(request)
        case = self.cases[str(request.payload.context["case_id"])]
        output = expected_review_brief_answer(case).model_dump(mode="json")
        result = NativeSingleTurnAgentsResult(
            operation=request.payload.operation,
            output=output,
            output_sha256=canonical_sha256(output),
            provider_call_id_sha256=hashlib.sha256(case.case_id.encode()).hexdigest(),
            provider_model=request.provider.model,
            input_tokens=50,
            output_tokens=7,
            elapsed_ms=13,
            trace=NativeAgentsTraceReceipt(
                agents_trace_id=request.trace.trace_id,
                weave_call_id=f"call-baseline-{case.case_id}",
                association_id=request.trace.association_id,
                wandb_run_id=request.trace.wandb_run_id,
                trace_url=(f"https://wandb.ai/entity/project/weave/traces/baseline-{case.case_id}"),
            ),
        )
        return NativeSingleTurnChildResponse(ok=True, result=result)


class _CapturingReviewBriefSubprocess:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.requests: list[Any] = []

    def invoke(self, *, request: Any, deadline_seconds: float) -> ReviewBriefAgentChildResponse:
        assert deadline_seconds >= 5
        self.actions.append(request.action)
        self.requests.append(request)
        trace_url = f"https://wandb.ai/entity/project/weave/traces/{request.trace.trace_id}"
        if request.action == "PREFLIGHT":
            return ReviewBriefAgentChildResponse(
                ok=True,
                capability=ReviewBriefAgentCapabilityReceipt(
                    provider_mode=request.provider.mode,
                    trace_id=request.trace.trace_id,
                    trace_url=trace_url,
                ),
            )
        runner_request = producer.ReviewBriefRunnerRequest.model_validate(request.runner_request)
        selections = [
            ReviewBriefFindingSelection(
                finding_alias=item.finding_alias,
                action_kind=item.permitted_actions[0],
                reviewer_disciplines=[
                    next(
                        value
                        for value in item.permitted_disciplines
                        if value in primary_disciplines_for_topic(item.review_topic)
                    )
                ],
            )
            for item in runner_request.snapshot.findings
        ]
        result = ReviewBriefRunnerResult(
            snapshot_sha256=runner_request.snapshot.snapshot_sha256,
            selections=selections,
            provider_call_id_sha256="b" * 64,
            provider_model=request.provider.model,
            tool_calls=list(required_review_brief_tools(runner_request.snapshot)),
            input_tokens=17,
            output_tokens=9,
            elapsed_ms=19,
            trace=ReviewBriefTrace(
                trace_id=request.trace.trace_id,
                weave_call_id=f"call-{len(self.actions)}",
                association_id=request.trace.association_id,
                wandb_run_id=request.trace.wandb_run_id,
                trace_url=trace_url,
            ),
        )
        return ReviewBriefAgentChildResponse(ok=True, result=result.model_dump(mode="json"))


def _perfect_campaign(
    dataset: ReviewBriefEvaluationDataset,
    *,
    baseline_input_tokens: int = 200,
    native_input_tokens: int = 100,
    provider_costs_available: bool = False,
) -> ReviewBriefCampaignArtifact:
    cost_basis = ReviewBriefProviderCostBasis.create(
        reference="synthetic-test-cost-ledger/v1",
        input_usd_per_million=1.0,
        output_usd_per_million=2.0,
    )
    executions: list[ReviewBriefMethodExecution] = []
    for method_id in METHOD_IDS:
        for case in dataset.cases:
            if case.category != "RIDGEWAY_ROLE":
                provider_execution = None
                observation = None
                if (
                    method_id != "deterministic_exhaustive"
                    and case.case_id not in PRE_PROVIDER_BLOCK_CASES[method_id]
                ):
                    provider_execution = ProviderExecutionEvidence(
                        provider="provider",
                        model="model-1",
                        provider_call_id=f"provider:{method_id}:{case.case_id}",
                        provider_response_sha256=hashlib.sha256(
                            f"response:{method_id}:{case.case_id}".encode()
                        ).hexdigest(),
                        weave_call_id=f"call:{method_id}:{case.case_id}",
                        weave_trace_id=f"trace:{method_id}:{case.case_id}",
                        weave_trace_url=(
                            "https://wandb.ai/entity/project/weave/traces/"
                            f"{method_id}-{case.case_id}"
                        ),
                        trace_status="VERIFIED",
                        trace_readback_verified=True,
                        prompt_tokens=(
                            baseline_input_tokens
                            if method_id == "existing_single_shot_full_context"
                            else native_input_tokens
                        ),
                        completion_tokens=5,
                        latency_ms=12.5,
                    )
                    observation = ReviewBriefExecutionObservation(
                        context_bytes=4096,
                        prompt_tokens=(
                            baseline_input_tokens
                            if method_id == "existing_single_shot_full_context"
                            else native_input_tokens
                        ),
                        completion_tokens=5,
                        latency_ms=12.5,
                        cost_usd=(
                            cost_basis.estimate(
                                input_tokens=(
                                    baseline_input_tokens
                                    if method_id == "existing_single_shot_full_context"
                                    else native_input_tokens
                                ),
                                output_tokens=5,
                            )
                            if provider_costs_available
                            else None
                        ),
                        cost_method="ESTIMATED" if provider_costs_available else "UNAVAILABLE",
                        cost_basis_ref=(cost_basis.reference if provider_costs_available else None),
                        tool_attempts=[],
                        tool_call_count=0,
                    )
                executions.append(
                    fail_closed_review_brief_execution(
                        method_id=method_id,
                        case=case,
                        provider_execution=provider_execution,
                        observation=observation,
                    )
                )
                continue
            provider_execution = None
            tool_attempts: list[str] = []
            if method_id != "deterministic_exhaustive":
                provider_execution = ProviderExecutionEvidence(
                    provider="provider",
                    model="model-1",
                    provider_call_id=f"provider:{method_id}:{case.case_id}",
                    provider_response_sha256=hashlib.sha256(
                        f"response:{method_id}:{case.case_id}".encode()
                    ).hexdigest(),
                    weave_call_id=f"call:{method_id}:{case.case_id}",
                    weave_trace_id=f"trace:{method_id}:{case.case_id}",
                    weave_trace_url=(
                        f"https://wandb.ai/entity/project/weave/traces/{method_id}-{case.case_id}"
                    ),
                    trace_status="VERIFIED",
                    trace_readback_verified=True,
                    prompt_tokens=(
                        baseline_input_tokens
                        if method_id == "existing_single_shot_full_context"
                        else native_input_tokens
                    ),
                    completion_tokens=5,
                    latency_ms=12.5,
                )
                if method_id == "native_read_only_agent":
                    tool_attempts = [
                        f"{name}#{index}"
                        for index, name in enumerate(
                            (
                                "review_brief_get_scope",
                                "review_brief_list_findings",
                                "review_brief_get_findings",
                                "review_brief_resolve_evidence_aliases",
                                "review_brief_get_authorization_status",
                            ),
                            1,
                        )
                    ]
            answer = expected_review_brief_answer(case)
            if method_id == "native_read_only_agent":
                topics = {
                    "REVIEW_MASK": ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
                    "FEASIBILITY_GAP": ReviewBriefTopic.GENERAL_FEASIBILITY,
                    "COST": ReviewBriefTopic.COST_BASIS,
                    "ASSUMPTION": ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
                }
                answer_payload = answer.model_dump(mode="json")
                native_findings = []
                for item in answer.findings:
                    finding_kind = ReviewBriefFindingKind(item.finding_kind)
                    topic = topics[item.finding_kind]
                    actions, disciplines = topic_permissions(finding_kind, topic)
                    primary = set(primary_disciplines_for_topic(topic))
                    discipline = next(value for value in disciplines if value in primary)
                    native_findings.append(
                        {
                            **item.model_dump(mode="json"),
                            "review_topic": topic.value,
                            "selected_action": actions[0].value,
                            "reviewer_disciplines": [discipline.value],
                        }
                    )
                answer_payload["findings"] = native_findings
                answer = type(answer).model_validate(answer_payload)
            executions.append(
                ReviewBriefMethodExecution(
                    method_id=method_id,
                    case_id=case.case_id,
                    disposition="ANSWERED",
                    execution_path=(
                        "DETERMINISTIC_LOCAL"
                        if method_id == "deterministic_exhaustive"
                        else "PROVIDER_CONVERSATION"
                    ),
                    answer=answer,
                    fault_injection_exercised=False,
                    persistence_check_exercised=False,
                    persistence_backend="NOT_APPLICABLE",
                    rejection_audit_verified=False,
                    saved_draft_count=0,
                    provider_execution=provider_execution,
                    observation=ReviewBriefExecutionObservation(
                        context_bytes=4096 if provider_execution else None,
                        prompt_tokens=(
                            baseline_input_tokens
                            if method_id == "existing_single_shot_full_context"
                            else native_input_tokens
                            if provider_execution
                            else None
                        ),
                        completion_tokens=5 if provider_execution else None,
                        latency_ms=12.5 if provider_execution else 0.0,
                        cost_usd=(
                            cost_basis.estimate(
                                input_tokens=(
                                    baseline_input_tokens
                                    if method_id == "existing_single_shot_full_context"
                                    else native_input_tokens
                                ),
                                output_tokens=5,
                            )
                            if provider_costs_available and provider_execution
                            else None
                        ),
                        cost_method=(
                            "ESTIMATED"
                            if provider_costs_available and provider_execution
                            else "UNAVAILABLE"
                        ),
                        cost_basis_ref=(
                            cost_basis.reference
                            if provider_costs_available and provider_execution
                            else None
                        ),
                        tool_attempts=tool_attempts,
                        tool_call_count=len(tool_attempts),
                    ),
                )
            )
    return build_review_brief_campaign(
        dataset=dataset,
        methods=_declarations(),
        executions=executions,
        source_repository="owner/repository",
        source_commit="d" * 40,
        campaign_wandb_run_id="campaign-run-1",
        campaign_wandb_run_url=("https://wandb.ai/entity/project/runs/campaign-run-1"),
        provider_cost_basis=cost_basis,
    )


def _png(width: int, height: int, color: tuple[int, int, int] = (36, 74, 65)) -> bytes:
    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(color) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height, level=9))
        + chunk(b"IEND", b"")
    )


def _write_animation_bundle(
    tmp_path: Path,
    campaign: ReviewBriefCampaignArtifact,
) -> tuple[Any, Any, Path]:
    directory = tmp_path / "media"
    directory.mkdir()
    contract = build_review_brief_animation_render_contract(campaign)
    html = render_review_brief_animation_html(contract).encode("utf-8")
    transcript = render_review_brief_animation_transcript(contract).encode("utf-8")
    (directory / f"{FILM_SLUG}.html").write_bytes(html)
    (directory / "review-brief-animation-transcript.txt").write_bytes(transcript)
    (directory / "review-brief-animation-render-contract.json").write_bytes(
        json.dumps(contract.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    for checkpoint in contract.film_spec.checkpoints:
        (directory / f"{checkpoint.name}.png").write_bytes(_png(1280, 720))
    (directory / f"{FILM_SLUG}-poster.png").write_bytes(_png(1280, 720, (169, 207, 100)))
    (directory / "contact-sheet.png").write_bytes(_png(1280, 360, (81, 185, 176)))
    (directory / f"{FILM_SLUG}.mp4").write_bytes(
        b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    )
    spec = contract.film_spec
    validation = build_review_brief_animation_validation_receipt(
        contract=contract,
        html_sha256=hashlib.sha256(html).hexdigest(),
        raw_validator_result={
            "ok": True,
            "spec": {
                "slug": spec.slug,
                "title": spec.title,
                "width": spec.width,
                "height": spec.height,
                "duration": spec.duration_seconds,
                "fps": spec.fps,
                "posterTime": spec.poster_time_seconds,
                "reducedMotionTime": spec.reduced_motion_time_seconds,
                "silent": True,
                "checkpoints": [
                    {"name": item.name, "time": item.time_seconds} for item in spec.checkpoints
                ],
            },
            "deterministicSha256": "e" * 64,
            "responsive": [640, 360],
            "reducedMotion": {
                "time": spec.reduced_motion_time_seconds,
                "text": "Play",
                "label": "Play animation",
            },
            "video": {
                "codec": "h264",
                "profile": "High",
                "width": 1280,
                "height": 720,
                "fps": 15,
                "frames": round(spec.duration_seconds * spec.fps),
                "duration": spec.duration_seconds,
                "pixelFormat": "yuv420p",
                "colorRange": "tv",
                "audio": False,
            },
            "artifacts": [],
        },
    )
    (directory / "review-brief-animation-validation.json").write_bytes(
        json.dumps(validation.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    assert set(expected_review_brief_animation_files(contract)) == {
        item.name for item in directory.iterdir()
    }
    manifest = build_review_brief_animation_media_manifest(
        campaign=campaign,
        contract=contract,
        validation_receipt=validation,
        media_directory=directory,
    )
    manifest_path = directory / MEDIA_MANIFEST_FILENAME
    manifest_path.write_bytes(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    return contract, manifest, manifest_path


def test_dataset_is_fixed_four_roles_plus_ten_fail_closed_cases(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    assert len(dataset.cases) == 14
    assert [item.case_id for item in dataset.cases[:4]] == [
        "ridgeway-low-cost-caution",
        "ridgeway-carry-forward",
        "ridgeway-over-budget",
        "ridgeway-hypothesis-collapse",
    ]
    assert [item.case_id for item in dataset.cases[4:]] == [
        "safeguard-failure",
        "incomplete-ensemble",
        "qa-rejection",
        "stale-evidence",
        "unknown-alias",
        "prompt-injection",
        "sensitive-location",
        "unsupported-structured-output",
        "trace-readback-failure",
        "unauthorized-action",
    ]
    assert all(item.allowed_tool_names == list(NATIVE_AGENT_TOOL_NAMES) for item in dataset.cases)


def test_independent_role_ledger_matches_actual_production_finding_granularity(
    dataset: ReviewBriefEvaluationDataset,
    ridgeway_resource: GuidedEvidenceResource,
) -> None:
    for case in dataset.cases[:4]:
        assert case.portfolio_id is not None
        source = ridgeway_review_source(
            project_id="ecocorridor-cc0-review-brief-evaluation",
            model_revision_id=f"ridgeway-{ridgeway_resource.manifest_sha256[:24]}",
            projection=ridgeway_resource.projection,
            portfolio_id=case.portfolio_id,
            evidence_manifest_sha256=ridgeway_resource.manifest_sha256,
        )
        snapshot = build_authorized_snapshot(source)
        selections = [
            ReviewBriefFindingSelection(
                finding_alias=item.finding_alias,
                action_kind=item.permitted_actions[0],
                reviewer_disciplines=[
                    next(
                        discipline
                        for discipline in item.permitted_disciplines
                        if discipline in primary_disciplines_for_topic(item.review_topic)
                    )
                ],
            )
            for item in snapshot.findings
        ]
        result = ReviewBriefRunnerResult(
            snapshot_sha256=snapshot.snapshot_sha256,
            selections=selections,
            provider_call_id_sha256="a" * 64,
            provider_model="model-1",
            tool_calls=list(required_review_brief_tools(snapshot)),
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=1,
            trace=ReviewBriefTrace(
                trace_id=f"trace_{'1' * 32}",
                weave_call_id=f"call-{case.case_id}",
                association_id=f"association-{case.case_id}",
                wandb_run_id="campaign-run-1",
                trace_url=(f"https://wandb.ai/entity/project/weave/traces/{case.case_id}"),
            ),
        )
        draft = render_review_brief(
            call_id=f"semantic-ledger-test:{case.case_id}",
            source=source,
            result=result,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            supersedes_draft_id=None,
        )
        expected = expected_review_brief_answer(case)
        answer = expected.model_copy(
            update={
                "findings": [
                    ReviewBriefFinding(
                        finding_id=item.item_id,
                        finding_kind=item.finding_kind.value,
                        review_topic=item.review_topic.value,
                        evidence_ids=list(item.evidence_claim_ids),
                        rendered_text=item.rendered_text,
                        selected_action=item.action_kind.value,
                        reviewer_disciplines=[value.value for value in item.reviewer_disciplines],
                    )
                    for item in draft.items
                ],
                "limitations": list(draft.limitations),
                "next_action": draft.disposition_snapshot.next_action,
            }
        )
        execution = ReviewBriefMethodExecution(
            method_id="native_read_only_agent",
            case_id=case.case_id,
            disposition="ANSWERED",
            execution_path="PROVIDER_CONVERSATION",
            answer=answer,
            fault_injection_exercised=False,
            persistence_check_exercised=False,
            persistence_backend="NOT_APPLICABLE",
            rejection_audit_verified=False,
            saved_draft_count=0,
            provider_execution=ProviderExecutionEvidence(
                provider="provider",
                model="model-1",
                provider_call_id=f"provider:{case.case_id}",
                provider_response_sha256="b" * 64,
                weave_call_id=f"call:{case.case_id}",
                weave_trace_id=f"trace:{case.case_id}",
                weave_trace_url=(f"https://wandb.ai/entity/project/weave/traces/{case.case_id}"),
                trace_status="VERIFIED",
                trace_readback_verified=True,
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=1,
            ),
            observation=ReviewBriefExecutionObservation(
                context_bytes=None,
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=1,
                cost_method="UNAVAILABLE",
                tool_attempts=[
                    f"{name}#{index}"
                    for index, name in enumerate(required_review_brief_tools(snapshot), 1)
                ],
                tool_call_count=len(required_review_brief_tools(snapshot)),
            ),
        )
        _, metrics = score_review_brief_execution(case, execution)
        assert metrics.required_finding_recall == 1.0
        assert metrics.unsupported_finding_count == 0


def test_deterministic_method_passes_every_fixed_gate_without_provider(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    method = DeterministicExhaustiveReviewBriefMethod(implementation_sha256="a" * 64)
    for case in dataset.cases:
        execution = method.evaluate(case=case, dataset_sha256=dataset.dataset_sha256)
        scores, metrics = score_review_brief_execution(case, execution)
        assert all(scores.values()), case.case_id
        assert execution.provider_execution is None
        assert metrics.tool_call_count == 0


def test_semantic_finding_matching_is_one_to_one(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
) -> None:
    case = dataset.cases[0]
    expected = case.required_findings[0]
    duplicate_expected = expected.model_copy(update={"finding_id": f"{expected.finding_id}-second"})
    two_expected_payload = case.model_dump(mode="json")
    two_expected_payload["required_findings"] = [
        expected.model_dump(mode="json"),
        duplicate_expected.model_dump(mode="json"),
    ]
    two_expected_case = type(case).model_validate(two_expected_payload)

    complete_answer = expected_review_brief_answer(two_expected_case)
    single_answer_payload = complete_answer.model_dump(mode="json")
    single_answer_payload["findings"] = [single_answer_payload["findings"][0]]
    single_answer = ReviewBriefAnswer.model_validate(single_answer_payload)

    with ReviewBriefEvaluationDraftProbe(tmp_path / "semantic-matching-store") as probe:
        method = DeterministicExhaustiveReviewBriefMethod(
            implementation_sha256="a" * 64,
            persistence_probe=probe,
        )
        base_execution = method.evaluate(
            case=two_expected_case,
            dataset_sha256=dataset.dataset_sha256,
        )
        single_execution = ReviewBriefMethodExecution.model_validate(
            {
                **base_execution.model_dump(mode="json"),
                "answer": single_answer.model_dump(mode="json"),
            }
        )
        _, single_metrics = score_review_brief_execution(two_expected_case, single_execution)
        assert single_metrics.required_finding_recall == 0.5
        assert single_metrics.unsupported_finding_count == 0

        one_expected_answer = expected_review_brief_answer(case)
        duplicate_observed = one_expected_answer.findings[0].model_copy(
            update={"finding_id": f"{one_expected_answer.findings[0].finding_id}-duplicate"}
        )
        extra_answer_payload = one_expected_answer.model_dump(mode="json")
        extra_answer_payload["findings"].append(duplicate_observed.model_dump(mode="json"))
        extra_answer = ReviewBriefAnswer.model_validate(extra_answer_payload)
        extra_base_execution = method.evaluate(
            case=case,
            dataset_sha256=dataset.dataset_sha256,
        )
        extra_execution = ReviewBriefMethodExecution.model_validate(
            {
                **extra_base_execution.model_dump(mode="json"),
                "answer": extra_answer.model_dump(mode="json"),
            }
        )
        _, extra_metrics = score_review_brief_execution(case, extra_execution)
        assert extra_metrics.required_finding_recall == 1.0
        assert extra_metrics.unsupported_finding_count == 1


def test_fixed_safety_preblocks_have_no_provider_receipt_or_application_draft(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {item.case_id: item for item in dataset.cases}
    for method_id, case_ids in PRE_PROVIDER_BLOCK_CASES.items():
        for case_id in case_ids:
            execution = fail_closed_review_brief_execution(method_id=method_id, case=cases[case_id])
            assert execution.execution_path == "PRE_PROVIDER_BLOCK"
            assert execution.provider_execution is None
            assert execution.error_code == cases[case_id].expected_error_code
            assert execution.disposition == cases[case_id].expected_disposition
            assert execution.answer is not None
            assert execution.answer.disposition == "ABSTAINED"
            assert execution.observation.tool_call_count == 0
            assert execution.persistence_check_exercised is True
            assert execution.persistence_backend == "ECO_PROJECT_STORE"
            assert execution.rejection_audit_verified is True
            assert execution.saved_draft_count == 0
            scores, _ = score_review_brief_execution(cases[case_id], execution)
            assert all(scores.values()), case_id

    probe = ReviewBriefEvaluationDraftProbe(tmp_path / "evaluation-persistence-project")
    case = cases["prompt-injection"]
    monkeypatch.setattr(
        probe,
        "saved_draft_count",
        lambda *, method_id, case_id: 1,
    )
    try:
        with pytest.raises(RuntimeError, match="DRAFT_PERSISTED"):
            fail_closed_review_brief_execution(
                method_id="existing_single_shot_full_context",
                case=case,
                persistence_probe=probe,
            )
    finally:
        probe.close()


def test_campaign_is_complete_and_recomputed_from_all_42_records(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    campaign = _perfect_campaign(dataset)
    assert len(campaign.records) == 42
    assert campaign.verified_provider_answer_count == 8
    assert campaign.provider_conversation_count == 22
    assert campaign.pre_provider_block_count == 6
    assert campaign.default_method_decision.decision == "KEEP_DETERMINISTIC"
    assert campaign.default_method_decision.current_default_method == "deterministic_exhaustive"
    assert campaign.default_method_decision.context_reduction_threshold_fraction == 0.25
    assert campaign.default_method_decision.context_measurement_method == (
        "PROVIDER_REPORTED_INPUT_TOKENS_V1"
    )
    assert campaign.default_method_decision.baseline_role_input_tokens == 800
    assert campaign.default_method_decision.native_role_input_tokens == 400
    assert campaign.default_method_decision.context_reduction_fraction == 0.5
    assert campaign.default_method_decision.context_reduction_verified is True
    assert campaign.default_method_decision.cost_comparison_complete is False
    assert campaign.default_method_decision.action_discipline_appropriateness_complete is True
    assert campaign.default_method_decision.candidate_status == "EXPERIMENTAL"
    assert all(item.passed_case_count == 14 for item in campaign.scorecards)
    assert all(item.deterministic_gate_passed for item in campaign.scorecards)
    assert verify_review_brief_campaign(campaign) == campaign

    tampered = campaign.model_dump(mode="json")
    tampered["records"][0]["scores"]["disposition_match"] = False
    with pytest.raises(ValueError):
        verify_review_brief_campaign(tampered)

    below_materiality = _perfect_campaign(
        dataset,
        baseline_input_tokens=200,
        native_input_tokens=160,
    )
    assert below_materiality.default_method_decision.context_reduction_fraction == 0.2
    assert below_materiality.default_method_decision.decision == "KEEP_DETERMINISTIC"
    assert below_materiality.default_method_decision.promotion_gate_passed is False

    promotion_eligible = _perfect_campaign(dataset, provider_costs_available=True)
    assert promotion_eligible.default_method_decision.cost_comparison_complete is True
    assert promotion_eligible.default_method_decision.promotion_gate_passed is True
    assert promotion_eligible.default_method_decision.decision == "PROMOTE_NATIVE"
    assert promotion_eligible.default_method_decision.candidate_status == "PROMOTION_ELIGIBLE"
    assert promotion_eligible.provider_cost_basis.reference == "synthetic-test-cost-ledger/v1"
    assert promotion_eligible.provider_cost_basis.basis_sha256
    story = build_review_brief_animation_story(promotion_eligible)
    decision_scene = next(item for item in story.scenes if item.scene_id == "default-decision")
    assert "promotion-eligible" in " ".join(decision_scene.visible_copy)
    assert "cost remains incomplete" not in " ".join(decision_scene.visible_copy)
    plan = build_review_brief_publication_plan(promotion_eligible, source_tree_clean=True)
    assert plan["provider_cost_basis"] == promotion_eligible.provider_cost_basis.model_dump(
        mode="json"
    )
    assert "unmeasured" not in plan["report_copy"]["limitations"]


def test_signed_provider_cost_basis_accepts_zero_rate_and_rejects_unversioned_reference() -> None:
    free = ReviewBriefProviderCostBasis.create(
        reference="provider-pricing-v1",
        input_usd_per_million=0,
        output_usd_per_million=0,
    )
    assert free.estimate(input_tokens=100, output_tokens=50) == 0
    with pytest.raises(ValueError, match="versioned"):
        ReviewBriefProviderCostBasis.create(
            reference="mutable-latest",
            input_usd_per_million=1,
            output_usd_per_million=2,
        )


def test_campaign_verifier_rejects_fabricated_provider_receipt_on_safety_case(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    campaign = _perfect_campaign(dataset)
    tampered = campaign.model_dump(mode="json")
    record = next(
        item
        for item in tampered["records"]
        if item["method_id"] == "native_read_only_agent" and item["case_id"] == "prompt-injection"
    )
    record["execution"]["provider_execution"] = {
        "provider": "provider",
        "model": "model-1",
        "provider_call_id": "fabricated",
        "provider_response_sha256": "f" * 64,
        "weave_call_id": None,
        "weave_trace_id": None,
        "weave_trace_url": None,
        "trace_status": "EXPECTED_FAILURE",
        "trace_readback_verified": False,
        "prompt_tokens": None,
        "completion_tokens": None,
        "latency_ms": None,
    }
    with pytest.raises(ValueError):
        ReviewBriefCampaignArtifact.model_validate(tampered)


def test_publication_plan_is_media_free_and_bound_to_real_campaign(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    plan = build_review_brief_publication_plan(_perfect_campaign(dataset), source_tree_clean=True)
    assert plan["method_count"] == 3
    assert plan["case_count"] == 14
    assert plan["record_count"] == 42
    assert plan["campaign_wandb_run_id"] == "campaign-run-1"
    assert plan["wandb_projection"]["run_id"] == "campaign-run-1"
    assert plan["wandb_projection"]["run_url"] == (
        "https://wandb.ai/entity/project/runs/campaign-run-1"
    )
    assert plan["provider_conversation_count"] == 22
    assert plan["pre_provider_block_count"] == 6
    assert plan["default_method_decision"]["decision"] == "KEEP_DETERMINISTIC"
    assert plan["default_method_decision"]["current_default_method"] == ("deterministic_exhaustive")
    assert plan["default_method_decision"]["candidate_status"] == "EXPERIMENTAL"
    assert plan["default_method_decision"]["context_reduction_threshold_fraction"] == 0.25
    assert plan["default_method_decision"]["promotion_gate_passed"] is False
    result_columns = plan["tables"]["review_brief_method_results"]["columns"]
    assert "persistence_backend" in result_columns
    assert "rejection_audit_verified" in result_columns
    assert "saved_draft_count" in result_columns
    trace_table = plan["tables"]["review_brief_trace_tools"]
    assert trace_table["row_count"] >= plan["provider_conversation_count"]
    assert "weave_trace_url" in trace_table["columns"]
    assert "tool_attempt_index" in trace_table["columns"]
    trace_url_index = trace_table["columns"].index("weave_trace_url")
    assert all(
        str(row[trace_url_index]).startswith("https://wandb.ai/") for row in trace_table["rows"]
    )
    assert plan["provider_backed"] is True
    assert plan["video_rendered"] is False
    assert plan["video_logged"] is False
    assert not any(
        str(item["logical_name"]).endswith((".mp4", ".mov", ".gif"))
        for item in plan["artifact_files"]
    )


def test_animation_story_describes_one_local_and_two_provider_methods(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    story = build_review_brief_animation_story(_perfect_campaign(dataset))
    rendered = story.model_dump_json()
    assert "one local control and two provider-backed methods" in rendered
    assert "Three fixed provider-backed methods" not in rendered


def test_animation_render_contract_is_deterministic_local_and_claim_bound(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    campaign = _perfect_campaign(dataset)
    first = build_review_brief_animation_render_contract(campaign)
    second = build_review_brief_animation_render_contract(campaign)
    assert first == second
    assert first.record_count == 42
    assert first.provider_conversation_count == 22
    assert first.pre_provider_block_count == 6
    assert len(first.film_spec.checkpoints) == len(first.story.scenes) == 7
    assert {item.claim_id for item in first.story.claim_ledger} == {
        claim_id for scene in first.story.scenes for claim_id in scene.evidence_claim_ids
    }
    html = render_review_brief_animation_html(first)
    assert "window.__filmSpec" in html
    assert "window.__animationReady" in html
    assert "window.setAnimationTime" in html
    assert "window.releaseAnimation" in html
    assert "https://" not in html
    assert "http://" not in html
    transcript = render_review_brief_animation_transcript(first)
    assert hashlib.sha256(transcript.encode()).hexdigest() == first.transcript_sha256


def test_animation_media_manifest_rehashes_every_matching_output(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
) -> None:
    campaign = _perfect_campaign(dataset)
    _, manifest, manifest_path = _write_animation_bundle(tmp_path, campaign)
    assert (
        validate_review_brief_animation_media_manifest(
            manifest,
            campaign=campaign,
            media_directory=manifest_path.parent,
        )
        == manifest
    )
    poster = manifest_path.parent / f"{FILM_SLUG}-poster.png"
    poster.write_bytes(poster.read_bytes()[:-1] + b"0")
    with pytest.raises(ValueError, match="PNG|differs"):
        validate_review_brief_animation_media_manifest(
            manifest,
            campaign=campaign,
            media_directory=manifest_path.parent,
        )


def test_publisher_attaches_only_validated_matching_media(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
) -> None:
    campaign = _perfect_campaign(dataset)
    _, manifest, manifest_path = _write_animation_bundle(tmp_path, campaign)
    base = build_review_brief_publication_plan(campaign, source_tree_clean=True)
    plan = publisher.attach_validated_media_to_publication_plan(
        base,
        campaign=campaign,
        media_manifest_path=manifest_path,
    )
    assert plan["video_rendered"] is True
    assert plan["video_logged"] is False
    assert plan["validated_media"]["media_manifest_sha256"] == (manifest.media_manifest_sha256)
    assert any(item["logical_name"].endswith(".mp4") for item in plan["artifact_files"])
    validate_review_brief_publication_plan(plan)

    wrong_campaign = _perfect_campaign(dataset, native_input_tokens=120)
    with pytest.raises(ValueError, match="campaign|media"):
        publisher.attach_validated_media_to_publication_plan(
            build_review_brief_publication_plan(wrong_campaign, source_tree_clean=True),
            campaign=wrong_campaign,
            media_manifest_path=manifest_path,
        )


def test_animation_entrypoint_refuses_before_starting_renderer_without_authorization(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _perfect_campaign(dataset)
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(campaign.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(animation_renderer, "_git_source", lambda _root: ("d" * 40, True))
    monkeypatch.setattr(
        animation_renderer,
        "_run_renderer",
        lambda **_kwargs: pytest.fail("renderer started without explicit authorization"),
    )
    with pytest.raises(SystemExit, match="authorize-verified-provider-media-render"):
        animation_renderer.main(
            [
                "--repository-root",
                str(ROOT),
                "--campaign",
                str(campaign_path),
                "--output-directory",
                str(tmp_path / "outputs"),
            ]
        )
    assert not (tmp_path / "outputs").exists()


def test_producer_uses_real_agents_subprocess_paths_without_expected_answer_injection(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
) -> None:
    provider, target = _provider_runtime()
    role_case = dataset.cases[0]
    expected = expected_review_brief_answer(role_case)
    payload = expected.model_dump(mode="json")
    payload["summary"] = "The provider returned this bounded synthetic screening summary."
    observed_answer = ReviewBriefAnswer.model_validate(payload)
    subprocess_client = _CapturingSingleTurnSubprocess(observed_answer)
    method = producer.ExistingSingleShotFullContextMethod(
        provider_name="provider",
        provider=provider,
        prompt_ref="weave:///entity/project/object/single-shot:abcdefghijklmnop",
        implementation_sha256="a" * 64,
        capability_receipt=ReviewBriefAgentCapabilityReceipt(
            provider_mode="CHAT_COMPLETIONS",
            trace_id=f"trace_{'1' * 32}",
            trace_url="https://wandb.ai/entity/project/weave/traces/preflight",
        ),
        weave_target=target,
        wandb_external_project_id="external-project-1",
        wandb_run_id="run-1",
        provider_api_key="provider-test-key-123456",
        wandb_api_key="not-a-secret-value",
        scratch_root=tmp_path,
        cost_basis=producer.ProviderTokenCostBasis(
            reference="test-token-pricing-v1",
            input_usd_per_million=1.0,
            output_usd_per_million=2.0,
        ),
    )
    assert isinstance(method.subprocess_client, PopenNativeSingleTurnSubprocess)
    method.subprocess_client = subprocess_client
    execution = method.evaluate(case=role_case, dataset_sha256=dataset.dataset_sha256)
    assert execution.execution_path == "PROVIDER_CONVERSATION"
    assert execution.answer is not None
    assert execution.answer.summary == observed_answer.summary
    assert execution.observation.context_bytes is not None
    assert execution.observation.context_bytes > 0
    assert execution.observation.prompt_tokens == 11
    assert execution.observation.cost_method == "ESTIMATED"
    assert execution.observation.cost_usd == pytest.approx(0.000025)
    assert len(subprocess_client.requests) == 1
    sent = subprocess_client.requests[0].payload.context
    assert "authorized_renderer_contract" not in sent
    assert "authorized_findings" not in sent
    assert "authorized_numeric_evidence" in sent
    assert "authorized_limitations" in sent


def test_real_producer_methods_build_and_verify_complete_campaign_without_network(
    dataset: ReviewBriefEvaluationDataset,
    ridgeway_resource: GuidedEvidenceResource,
    tmp_path: Path,
) -> None:
    provider, target = _provider_runtime()
    cost_basis = producer.ProviderTokenCostBasis(
        reference="test-token-pricing-v1",
        input_usd_per_million=1.0,
        output_usd_per_million=2.0,
    )
    probe = ReviewBriefEvaluationDraftProbe(tmp_path / "campaign-project")
    try:
        native = producer.NativeReadOnlyAgentMethod(
            provider_name="provider",
            provider=provider,
            prompt_ref="weave:///entity/project/object/native-agent:abcdefghijklmnop",
            implementation_sha256="c" * 64,
            resource=ridgeway_resource,
            weave_target=target,
            wandb_external_project_id="external-project-1",
            wandb_run_id="campaign-run-1",
            provider_api_key="provider-test-key-123456",
            wandb_api_key="not-a-secret-value",
            scratch_root=tmp_path,
            cost_basis=cost_basis,
            persistence_probe=probe,
        )
        native_client = _CapturingReviewBriefSubprocess()
        native.subprocess_client = native_client
        capability = native.preflight(case=dataset.cases[0], dataset_sha256=dataset.dataset_sha256)
        baseline = producer.ExistingSingleShotFullContextMethod(
            provider_name="provider",
            provider=provider,
            prompt_ref="weave:///entity/project/object/single-shot:abcdefghijklmnop",
            implementation_sha256="b" * 64,
            capability_receipt=capability,
            weave_target=target,
            wandb_external_project_id="external-project-1",
            wandb_run_id="campaign-run-1",
            provider_api_key="provider-test-key-123456",
            wandb_api_key="not-a-secret-value",
            scratch_root=tmp_path,
            cost_basis=cost_basis,
            persistence_probe=probe,
        )
        baseline.subprocess_client = _DatasetSingleTurnSubprocess(dataset)
        campaign = producer.run_review_brief_campaign(
            dataset=dataset,
            methods=[
                DeterministicExhaustiveReviewBriefMethod(
                    implementation_sha256="a" * 64,
                    persistence_probe=probe,
                ),
                baseline,
                native,
            ],
            source_repository="owner/repository",
            source_commit="d" * 40,
            campaign_wandb_run_id="campaign-run-1",
            campaign_wandb_run_url="https://wandb.ai/entity/project/runs/campaign-run-1",
            provider_cost_basis=cost_basis.contract,
        )
    finally:
        probe.close()

    assert verify_review_brief_campaign(campaign) == campaign
    provider_rows = [
        item
        for item in campaign.records
        if item.execution.execution_path == "PROVIDER_CONVERSATION"
    ]
    assert provider_rows
    assert all((item.metrics.context_bytes or 0) > 0 for item in provider_rows)
    assert all((item.metrics.prompt_tokens or 0) > 0 for item in provider_rows)
    assert campaign.provider_cost_basis == cost_basis.contract


def test_native_method_scores_validated_agent_selections_and_exercises_fault_seams(
    dataset: ReviewBriefEvaluationDataset,
    ridgeway_resource: GuidedEvidenceResource,
    tmp_path: Path,
) -> None:
    provider, target = _provider_runtime()
    method = producer.NativeReadOnlyAgentMethod(
        provider_name="provider",
        provider=provider,
        prompt_ref="weave:///entity/project/object/native-agent:abcdefghijklmnop",
        implementation_sha256="b" * 64,
        resource=ridgeway_resource,
        weave_target=target,
        wandb_external_project_id="external-project-1",
        wandb_run_id="run-1",
        provider_api_key="provider-test-key-123456",
        wandb_api_key="not-a-secret-value",
        scratch_root=tmp_path,
        cost_basis=producer.ProviderTokenCostBasis(
            reference="test-token-pricing-v1",
            input_usd_per_million=1.0,
            output_usd_per_million=2.0,
        ),
    )
    assert isinstance(method.subprocess_client, PopenReviewBriefAgentSubprocess)
    subprocess_client = _CapturingReviewBriefSubprocess()
    method.subprocess_client = subprocess_client

    role_case = dataset.cases[0]
    role_execution = method.evaluate(case=role_case, dataset_sha256=dataset.dataset_sha256)
    assert subprocess_client.actions == ["PREFLIGHT", "PREPARE"]
    assert role_execution.execution_path == "PROVIDER_CONVERSATION"
    assert role_execution.answer is not None
    assert all(item.selected_action is not None for item in role_execution.answer.findings)
    role_scores, _ = score_review_brief_execution(role_case, role_execution)
    assert role_scores["native_selection_grounded"] is True
    assert role_scores["numeric_faithfulness_complete"] is True
    assert role_execution.answer is not None
    assert {item.finding_id for item in role_execution.answer.findings}.isdisjoint(
        {item.finding_id for item in role_case.required_findings}
    )
    prepare_request = subprocess_client.requests[-1]
    production_source = producer.ridgeway_review_source(
        project_id="ecocorridor-cc0-review-brief-evaluation",
        model_revision_id=f"ridgeway-{ridgeway_resource.manifest_sha256[:24]}",
        projection=ridgeway_resource.projection,
        portfolio_id=role_case.portfolio_id,
        evidence_manifest_sha256=ridgeway_resource.manifest_sha256,
    )
    sent_snapshot = producer.ReviewBriefRunnerRequest.model_validate(
        prepare_request.runner_request
    ).snapshot
    assert len(sent_snapshot.findings) == len(production_source.findings)
    assert all(item.finding_alias.startswith("F") for item in sent_snapshot.findings)

    subprocess_client.actions.clear()
    trace_case = next(item for item in dataset.cases if item.case_id == "trace-readback-failure")
    trace_execution = method.evaluate(case=trace_case, dataset_sha256=dataset.dataset_sha256)
    assert subprocess_client.actions == ["PREFLIGHT", "PREPARE"]
    assert trace_execution.execution_path == "PROVIDER_CONVERSATION"
    assert trace_execution.fault_injection_exercised is True
    assert trace_execution.provider_execution is not None
    assert trace_execution.provider_execution.trace_readback_verified is True
    trace_scores, _ = score_review_brief_execution(trace_case, trace_execution)
    assert all(trace_scores.values())

    for case_id in ("stale-evidence", "unknown-alias", "prompt-injection", "sensitive-location"):
        subprocess_client.actions.clear()
        subprocess_client.requests.clear()
        case = next(item for item in dataset.cases if item.case_id == case_id)
        execution = method.evaluate(case=case, dataset_sha256=dataset.dataset_sha256)
        assert execution.execution_path == "PRE_PROVIDER_BLOCK"
        assert execution.fault_injection_exercised is True
        assert subprocess_client.actions == []


def test_append_only_report_sections_preserve_historical_blocks_and_are_idempotent() -> None:
    historical = [SimpleNamespace(text="historical heading"), SimpleNamespace(text="history")]
    first = [
        SimpleNamespace(
            text=f"{publisher.SECTION_START_PREFIX} campaign={'a' * 64} plan={'b' * 64} -->"
        ),
        SimpleNamespace(text="first campaign"),
        SimpleNamespace(text=publisher.SECTION_END),
    ]
    second = [
        SimpleNamespace(
            text=f"{publisher.SECTION_START_PREFIX} campaign={'c' * 64} plan={'d' * 64} -->"
        ),
        SimpleNamespace(text="second campaign"),
        SimpleNamespace(text=publisher.SECTION_END),
    ]
    once = publisher.append_owned_report_section(historical, first)
    twice = publisher.append_owned_report_section(once, second)
    assert twice[:2] == historical
    assert twice[2:5] == first
    assert twice[5:] == second
    assert publisher.append_owned_report_section(twice, second) == twice
    with pytest.raises(ValueError, match="unterminated"):
        publisher.append_owned_report_section([first[0]], second)


def test_publisher_orders_artifact_readback_before_existing_report_append(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    plan = build_review_brief_publication_plan(_perfect_campaign(dataset), source_tree_clean=True)

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def publish_campaign(self, _plan: Any) -> ReviewBriefArtifactReceipt:
            self.calls.append("publish")
            return ReviewBriefArtifactReceipt(
                campaign_sha256=plan["campaign_sha256"],
                dataset_sha256=plan["dataset_sha256"],
                run_id=plan["campaign_wandb_run_id"],
                run_url=plan["campaign_wandb_run_url"],
                artifact_ref="entity/project/review-brief:v1",
                artifact_url="https://wandb.ai/entity/project/artifacts/review-brief/v1",
                artifact_manifest_sha256=plan["artifact_manifest_sha256"],
                provider_readback_verified=False,
            )

        def verify_campaign_artifact(self, _plan: Any, receipt: Any) -> bool:
            assert receipt.provider_readback_verified is False
            self.calls.append("readback")
            return True

        def upsert_owned_report_section(self, **kwargs: Any) -> str:
            assert kwargs["receipt"].provider_readback_verified is True
            self.calls.append("append")
            return kwargs["report_url"]

    adapter = Adapter()
    receipt = publisher.publish_and_update_review_brief_report(
        plan=plan,
        report_url="https://wandb.ai/entity/project/reports/existing--id",
        adapter=adapter,
    )
    assert adapter.calls == ["publish", "readback", "append"]
    assert receipt["status"] == "PUBLISHED"
    assert receipt["video_rendered"] is False


def test_publisher_pins_sdk_and_report_operations_to_the_declared_wandb_deployment(
    dataset: ReviewBriefEvaluationDataset,
) -> None:
    plan = build_review_brief_publication_plan(_perfect_campaign(dataset), source_tree_clean=True)

    class FakeApi:
        def artifact(self, _artifact_ref: str) -> Any:
            return SimpleNamespace(metadata={})

    class FakeWandb:
        def __init__(self) -> None:
            self.api_overrides: dict[str, str] | None = None

        def Api(self, *, overrides: dict[str, str]) -> FakeApi:
            self.api_overrides = overrides
            return FakeApi()

    fake_wandb = FakeWandb()
    adapter = publisher.WandbSdkReviewBriefAdapter(
        entity="entity",
        project="project",
        api_base_url="https://api.wandb.example/platform/",
        web_base_url="https://wandb.example/app/",
        authorize_synthetic_public_upload=True,
        wandb_module=fake_wandb,
        reports_module=SimpleNamespace(),
    )
    receipt = ReviewBriefArtifactReceipt(
        campaign_sha256=plan["campaign_sha256"],
        dataset_sha256=plan["dataset_sha256"],
        run_id=plan["campaign_wandb_run_id"],
        run_url="https://wandb.example/entity/project/runs/campaign-run-1",
        artifact_ref="entity/project/review-brief:v1",
        artifact_url="https://wandb.example/entity/project/artifacts/review-brief/v1",
        artifact_manifest_sha256=plan["artifact_manifest_sha256"],
        provider_readback_verified=False,
    )
    assert adapter.verify_campaign_artifact(plan, receipt) is False
    assert fake_wandb.api_overrides == {"base_url": "https://api.wandb.example/platform"}
    with pytest.raises(PermissionError, match="another W&B deployment"):
        adapter.upsert_owned_report_section(
            report_url="https://wandb.ai/entity/project/reports/existing--id",
            plan=plan,
            receipt=receipt,
        )


def test_publisher_dry_run_is_network_free(
    dataset: ReviewBriefEvaluationDataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _perfect_campaign(dataset)
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(campaign.model_dump_json(), encoding="utf-8")
    output = tmp_path / "plan.json"
    monkeypatch.setattr(publisher, "_git_source", lambda _root: ("d" * 40, True))
    original_import = builtins.__import__

    def deny_network_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "wandb" or name.startswith("wandb_workspaces"):
            raise AssertionError("dry run imported a network SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_network_sdk)
    assert (
        publisher.main(
            [
                "--repository-root",
                str(ROOT),
                "--campaign",
                str(campaign_path),
                "--dry-run",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["record_count"] == 42
    assert plan["video_rendered"] is False
    assert "_artifact_payloads" not in plan


def test_provider_mode_requires_explicit_official_responses_endpoint() -> None:
    assert producer._provider_mode("https://api.openai.com/v1", "RESPONSES") == "RESPONSES"
    assert (
        producer._provider_mode("https://provider.example/v1", "CHAT_COMPLETIONS")
        == "CHAT_COMPLETIONS"
    )
    with pytest.raises(ValueError, match="official OpenAI"):
        producer._provider_mode("https://provider.example/v1", "RESPONSES")
