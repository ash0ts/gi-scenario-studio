"""Fail-closed post-campaign animation contracts for the Review Brief evaluation.

The campaign is the authority.  This module turns a complete, trace-verified
campaign into a deterministic Canvas-film render contract and validates the
resulting local media bundle.  It never calls a provider, starts a browser,
encodes video, or publishes anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .guided_evidence import GuidedEvidenceResource
from .review_brief_evaluation import (
    METHOD_IDS,
    PRE_PROVIDER_BLOCK_CASES,
    ReviewBriefAnimationStory,
    ReviewBriefCampaignArtifact,
    build_review_brief_animation_story,
    build_ridgeway_review_brief_dataset,
    verify_review_brief_campaign,
)

RENDER_CONTRACT_SCHEMA = "ecocorridor-review-brief-animation-render/v1"
VALIDATION_RECEIPT_SCHEMA = "ecocorridor-review-brief-animation-validation/v1"
MEDIA_MANIFEST_SCHEMA = "ecocorridor-review-brief-animation-media/v1"
FILM_SLUG = "ecocorridor-review-brief-campaign"
MEDIA_MANIFEST_FILENAME = "review-brief-animation-media-manifest.json"
FILM_WIDTH = 1280
FILM_HEIGHT = 720
FILM_FPS = 15
EXPECTED_RECORD_COUNT = 42
EXPECTED_PROVIDER_CONVERSATION_COUNT = 22
EXPECTED_PRE_PROVIDER_BLOCK_COUNT = 6
MAX_CAMPAIGN_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
_SAFE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)?\Z")


def _self_digest(model: ContractModel, field: str) -> str:
    payload = model.model_dump(mode="json")
    payload.pop(field)
    return canonical_sha256(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repo_script(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / name


def animation_tool_sha256s() -> tuple[str, str, str]:
    """Return exact checked-in renderer, validator, and JPEG-capture digests."""

    renderer = _repo_script("render_ecocorridor_canvas_film.mjs")
    validator = _repo_script("validate_ecocorridor_canvas_film.mjs")
    frame_auditor = _repo_script("capture_ecocorridor_review_brief_jpeg_frames.mjs")
    for label, path in (
        ("renderer", renderer),
        ("validator", validator),
        ("frame auditor", frame_auditor),
    ):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"review-brief animation {label} script is missing or unsafe")
    return _sha256_file(renderer), _sha256_file(validator), _sha256_file(frame_auditor)


class ReviewBriefAnimationCheckpoint(ContractModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scene_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    time_seconds: float = Field(ge=0)


class ReviewBriefAnimationFilmSpec(ContractModel):
    slug: Literal["ecocorridor-review-brief-campaign"]
    title: Literal["Can a review brief stay grounded and fail closed?"]
    width: Literal[1280]
    height: Literal[720]
    duration_seconds: float = Field(gt=0)
    fps: Literal[15]
    poster_time_seconds: float = Field(ge=0)
    reduced_motion_time_seconds: float = Field(ge=0)
    silent: Literal[True]
    checkpoints: list[ReviewBriefAnimationCheckpoint] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_film_spec(self) -> Self:
        if abs(self.duration_seconds * self.fps - round(self.duration_seconds * self.fps)) > 1e-9:
            raise ValueError("review-brief film duration × fps must be an integer")
        if self.poster_time_seconds >= self.duration_seconds:
            raise ValueError("review-brief poster time must be before the film duration")
        if self.reduced_motion_time_seconds >= self.duration_seconds:
            raise ValueError("review-brief reduced-motion time must be before the film duration")
        if len({item.name for item in self.checkpoints}) != len(self.checkpoints):
            raise ValueError("review-brief checkpoint names must be unique")
        if any(item.time_seconds >= self.duration_seconds for item in self.checkpoints):
            raise ValueError("review-brief checkpoint time must be before the film duration")
        return self


class ReviewBriefAnimationVisualMethod(ContractModel):
    method_id: Literal[
        "deterministic_exhaustive",
        "existing_single_shot_full_context",
        "native_read_only_agent",
    ]
    display_name: str = Field(min_length=1, max_length=64)
    provider_backed: bool
    passed_case_count: int = Field(ge=0)
    case_count: int = Field(ge=1)
    deterministic_gate_passed: bool


class ReviewBriefAnimationDecisionGate(ContractModel):
    gate_id: Literal[
        "candidate_all_gates",
        "role_quality",
        "safety_quality",
        "context_reduction",
        "cost_evidence",
        "topic_grounding",
    ]
    label: str = Field(min_length=1, max_length=32)
    passed: bool


class ReviewBriefAnimationVisualData(ContractModel):
    """The complete variable data surface the Canvas renderer may display."""

    role_case_count: int = Field(ge=1)
    safety_case_count: int = Field(ge=1)
    case_count: int = Field(ge=1)
    method_count: int = Field(ge=1)
    record_count: int = Field(ge=1)
    local_method_count: int = Field(ge=0)
    provider_method_count: int = Field(ge=0)
    provider_conversation_count: int = Field(ge=1)
    pre_provider_block_count: int = Field(ge=1)
    trace_matrix_verified: Literal[True]
    methods: list[ReviewBriefAnimationVisualMethod] = Field(min_length=1)
    decision: Literal["KEEP_DETERMINISTIC", "PROMOTE_NATIVE"]
    candidate_status: Literal["EXPERIMENTAL", "PROMOTION_ELIGIBLE"]
    decision_gates: list[ReviewBriefAnimationDecisionGate] = Field(min_length=6, max_length=6)
    visual_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_visual_data(self) -> Self:
        if self.role_case_count + self.safety_case_count != self.case_count:
            raise ValueError("review-brief visual case counts are inconsistent")
        if self.local_method_count + self.provider_method_count != self.method_count:
            raise ValueError("review-brief visual method counts are inconsistent")
        if len(self.methods) != self.method_count:
            raise ValueError("review-brief visual methods are incomplete")
        if self.record_count != self.case_count * self.method_count:
            raise ValueError("review-brief visual record count is inconsistent")
        expected_method_order = (
            "deterministic_exhaustive",
            "existing_single_shot_full_context",
            "native_read_only_agent",
        )
        if tuple(item.method_id for item in self.methods) != expected_method_order:
            raise ValueError("review-brief visual method order changed")
        expected_gate_order = (
            "candidate_all_gates",
            "role_quality",
            "safety_quality",
            "context_reduction",
            "cost_evidence",
            "topic_grounding",
        )
        if tuple(item.gate_id for item in self.decision_gates) != expected_gate_order:
            raise ValueError("review-brief visual decision-gate order changed")
        promotion = all(item.passed for item in self.decision_gates)
        if self.decision != ("PROMOTE_NATIVE" if promotion else "KEEP_DETERMINISTIC"):
            raise ValueError("review-brief visual decision differs from its gates")
        if self.candidate_status != ("PROMOTION_ELIGIBLE" if promotion else "EXPERIMENTAL"):
            raise ValueError("review-brief visual candidate status differs from its gates")
        if _self_digest(self, "visual_data_sha256") != self.visual_data_sha256:
            raise ValueError("review-brief visual-data digest differs")
        return self


class ReviewBriefAnimationRenderContract(ContractModel):
    schema_version: Literal["ecocorridor-review-brief-animation-render/v1"]
    campaign_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_repository: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    campaign_wandb_run_id: str
    campaign_wandb_run_url: str
    provider_backed: Literal[True]
    record_count: Literal[42]
    provider_conversation_count: Literal[22]
    pre_provider_block_count: Literal[6]
    verified_provider_answer_count: int = Field(ge=1)
    story: ReviewBriefAnimationStory
    story_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_data: ReviewBriefAnimationVisualData
    visual_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_auditor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    film_spec: ReviewBriefAnimationFilmSpec
    render_authorized: Literal[True]
    render_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_render_contract(self) -> Self:
        if self.story.story_sha256 != self.story_sha256:
            raise ValueError("review-brief render contract story digest is inconsistent")
        if (
            canonical_sha256([item.model_dump(mode="json") for item in self.story.claim_ledger])
            != self.claim_ledger_sha256
        ):
            raise ValueError("review-brief render contract claim ledger digest is inconsistent")
        if self.visual_data.visual_data_sha256 != self.visual_data_sha256:
            raise ValueError("review-brief render contract visual-data digest is inconsistent")
        if _self_digest(self, "render_contract_sha256") != self.render_contract_sha256:
            raise ValueError("review-brief animation render contract digest differs")
        return self


class ReviewBriefAnimationVideoValidation(ContractModel):
    codec: Literal["h264"]
    profile: Literal["High"]
    width: Literal[1280]
    height: Literal[720]
    fps: Literal[15.0]
    frames: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    pixel_format: Literal["yuv420p"]
    color_range: Literal["tv"]
    audio: Literal[False]


class ReviewBriefAnimationEncodingInputFrame(ContractModel):
    frame_index: int = Field(ge=0)
    time_seconds: float = Field(ge=0)
    logical_name: str = Field(pattern=r"^frame-[0-9]{6}\.jpg$")
    bytes: int = Field(gt=0)
    jpeg_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewBriefAnimationRuntimeTool(ContractModel):
    name: Literal["chrome", "ffmpeg", "ffprobe", "node"]
    version: str = Field(min_length=1, max_length=512)
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewBriefAnimationValidationReceipt(ContractModel):
    schema_version: Literal["ecocorridor-review-brief-animation-validation/v1"]
    status: Literal["VALIDATED"]
    campaign_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    story_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    html_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_auditor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding_input_format: Literal["jpeg"]
    encoding_input_jpeg_quality: Literal[92]
    encoding_input_frames: list[ReviewBriefAnimationEncodingInputFrame] = Field(min_length=1)
    encoding_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    second_pass_encoding_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding_input_determinism_verified: Literal[True]
    runtime_tools: list[ReviewBriefAnimationRuntimeTool] = Field(min_length=4, max_length=4)
    runtime_executable_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mp4_validation_mode: Literal["DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE"]
    responsive_viewport: tuple[int, int]
    reduced_motion_time_seconds: float = Field(ge=0)
    video: ReviewBriefAnimationVideoValidation
    validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_digest(self) -> Self:
        indexes = [item.frame_index for item in self.encoding_input_frames]
        if indexes != list(range(len(self.encoding_input_frames))):
            raise ValueError("review-brief encoding-input indexes are not complete and ordered")
        frame_payload = [item.model_dump(mode="json") for item in self.encoding_input_frames]
        if canonical_sha256(frame_payload) != self.encoding_input_manifest_sha256:
            raise ValueError("review-brief encoding-input manifest digest differs")
        if self.second_pass_encoding_input_manifest_sha256 != self.encoding_input_manifest_sha256:
            raise ValueError("review-brief independent JPEG encoding-input pass differs")
        if {item.name for item in self.runtime_tools} != {"chrome", "ffmpeg", "ffprobe", "node"}:
            raise ValueError("review-brief render runtime evidence is incomplete")
        if (
            canonical_sha256([item.model_dump(mode="json") for item in self.runtime_tools])
            != self.runtime_executable_manifest_sha256
        ):
            raise ValueError("review-brief render executable manifest digest differs")
        if _self_digest(self, "validation_receipt_sha256") != self.validation_receipt_sha256:
            raise ValueError("review-brief animation validation receipt digest differs")
        return self


class ReviewBriefAnimationMediaFile(ContractModel):
    logical_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)?$")
    role: Literal[
        "INTERACTIVE_HTML",
        "VIDEO",
        "POSTER",
        "CHECKPOINT",
        "CONTACT_SHEET",
        "TRANSCRIPT",
        "RENDER_CONTRACT",
        "VALIDATION_RECEIPT",
    ]
    media_type: Literal["text/html", "video/mp4", "image/png", "text/plain", "application/json"]
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewBriefAnimationMediaManifest(ContractModel):
    schema_version: Literal["ecocorridor-review-brief-animation-media/v1"]
    status: Literal["VALIDATED"]
    campaign_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    campaign_wandb_run_id: str
    campaign_wandb_run_url: str
    story_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_auditor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding_input_frame_count: int = Field(gt=0)
    encoding_input_determinism_verified: Literal[True]
    runtime_executable_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mp4_validation_mode: Literal["DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE"]
    film_spec: ReviewBriefAnimationFilmSpec
    files: list[ReviewBriefAnimationMediaFile] = Field(min_length=1)
    files_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest_digest(self) -> Self:
        names = [item.logical_name for item in self.files]
        if len(names) != len(set(names)):
            raise ValueError("review-brief animation media names must be unique")
        if (
            canonical_sha256([item.model_dump(mode="json") for item in self.files])
            != self.files_sha256
        ):
            raise ValueError("review-brief animation files digest differs")
        if self.encoding_input_frame_count != round(
            self.film_spec.duration_seconds * self.film_spec.fps
        ):
            raise ValueError("review-brief animation encoding-input frame count differs")
        if _self_digest(self, "media_manifest_sha256") != self.media_manifest_sha256:
            raise ValueError("review-brief animation media manifest digest differs")
        return self


def _load_current_dataset(repository_root: Path) -> Any:
    ridgeway = repository_root / "benchmarks" / "ecocorridor" / "ridgeway"
    manifest_path = ridgeway / "direct-evidence-manifest.json"
    projection_path = ridgeway / "ui_projection.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = ridgeway / str(manifest["evidence_bundle"]["filename"])
    resource = GuidedEvidenceResource.verify(
        projection_path=projection_path,
        projection_sha256=_sha256_file(projection_path),
        manifest_path=manifest_path,
        manifest_sha256=_sha256_file(manifest_path),
        archive_path=archive,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )
    return build_ridgeway_review_brief_dataset(resource)


def load_verified_review_brief_campaign(
    path: str | Path, *, repository_root: Path
) -> ReviewBriefCampaignArtifact:
    """Load a bounded campaign and bind it to the current verified Ridgeway source."""

    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_CAMPAIGN_BYTES:
        raise ValueError("review-brief campaign file is missing, unsafe, or oversized")
    campaign = verify_review_brief_campaign(
        ReviewBriefCampaignArtifact.model_validate_json(source.read_bytes())
    )
    if campaign.dataset != _load_current_dataset(repository_root):
        raise ValueError(
            "review-brief campaign is not bound to the current verified Ridgeway evidence"
        )
    return campaign


def _validate_trace_matrix(campaign: ReviewBriefCampaignArtifact) -> None:
    if len(campaign.records) != EXPECTED_RECORD_COUNT:
        raise ValueError("animation requires the complete 42-record campaign")
    conversations = 0
    pre_provider_blocks = 0
    verified_role_answers = 0
    provider_call_ids: set[str] = set()
    weave_call_ids: set[str] = set()
    weave_trace_ids: set[str] = set()
    weave_trace_urls: set[str] = set()
    cases = {item.case_id: item for item in campaign.dataset.cases}
    for record in campaign.records:
        execution = record.execution
        provider = execution.provider_execution
        if record.method_id == METHOD_IDS[0]:
            if execution.execution_path != "DETERMINISTIC_LOCAL" or provider is not None:
                raise ValueError("animation campaign local-control trace boundary changed")
            continue
        expected_preblock = record.case_id in PRE_PROVIDER_BLOCK_CASES[record.method_id]
        if expected_preblock:
            pre_provider_blocks += 1
            if execution.execution_path != "PRE_PROVIDER_BLOCK" or provider is not None:
                raise ValueError("animation requires pre-provider blocks with no provider receipt")
            continue
        conversations += 1
        if (
            execution.execution_path != "PROVIDER_CONVERSATION"
            or provider is None
            or provider.trace_status != "VERIFIED"
            or provider.trace_readback_verified is not True
            or not provider.provider_call_id
            or not provider.weave_call_id
            or not provider.weave_trace_id
        ):
            raise ValueError("animation requires exact provider and Weave trace readback")
        assert provider is not None
        identities = (
            (provider.provider_call_id, provider_call_ids),
            (str(provider.weave_call_id), weave_call_ids),
            (str(provider.weave_trace_id), weave_trace_ids),
            (str(provider.weave_trace_url), weave_trace_urls),
        )
        if any(value in observed for value, observed in identities):
            raise ValueError(
                "animation requires one unique trace receipt per provider conversation"
            )
        for value, observed in identities:
            observed.add(value)
        if (
            cases[record.case_id].category == "RIDGEWAY_ROLE"
            and execution.disposition == "ANSWERED"
        ):
            verified_role_answers += 1
    if conversations != EXPECTED_PROVIDER_CONVERSATION_COUNT:
        raise ValueError("animation requires exactly 22 provider conversations")
    if any(
        len(values) != conversations
        for values in (provider_call_ids, weave_call_ids, weave_trace_ids, weave_trace_urls)
    ):
        raise ValueError("animation provider trace identities are incomplete")
    if pre_provider_blocks != EXPECTED_PRE_PROVIDER_BLOCK_COUNT:
        raise ValueError("animation requires exactly six pre-provider blocks")
    if verified_role_answers != campaign.verified_provider_answer_count:
        raise ValueError("animation provider-backed answer count differs from traced role answers")


def build_review_brief_animation_visual_data(
    campaign: ReviewBriefCampaignArtifact,
) -> ReviewBriefAnimationVisualData:
    """Seal the complete variable data surface used by Canvas drawing code."""

    role_case_count = sum(item.category == "RIDGEWAY_ROLE" for item in campaign.dataset.cases)
    case_count = len(campaign.dataset.cases)
    scorecards = {item.method_id: item for item in campaign.scorecards}
    display_names = {
        "deterministic_exhaustive": "Deterministic exhaustive",
        "existing_single_shot_full_context": "Single-shot full context",
        "native_read_only_agent": "Native read-only agent",
    }
    methods = [
        ReviewBriefAnimationVisualMethod(
            method_id=item.method_id,
            display_name=display_names[item.method_id],
            provider_backed=item.provider_backed,
            passed_case_count=scorecards[item.method_id].passed_case_count,
            case_count=scorecards[item.method_id].case_count,
            deterministic_gate_passed=scorecards[item.method_id].deterministic_gate_passed,
        )
        for item in campaign.methods
    ]
    decision = campaign.default_method_decision
    gates = [
        ReviewBriefAnimationDecisionGate(
            gate_id="candidate_all_gates",
            label="native gates",
            passed=decision.candidate_all_gates_passed,
        ),
        ReviewBriefAnimationDecisionGate(
            gate_id="role_quality",
            label="role quality",
            passed=decision.role_quality_noninferior,
        ),
        ReviewBriefAnimationDecisionGate(
            gate_id="safety_quality",
            label="safety quality",
            passed=decision.safety_quality_noninferior,
        ),
        ReviewBriefAnimationDecisionGate(
            gate_id="context_reduction",
            label="context ≥25%",
            passed=decision.context_reduction_verified,
        ),
        ReviewBriefAnimationDecisionGate(
            gate_id="cost_evidence",
            label="cost evidence",
            passed=decision.cost_comparison_complete,
        ),
        ReviewBriefAnimationDecisionGate(
            gate_id="topic_grounding",
            label="topic grounding",
            passed=decision.action_discipline_appropriateness_complete,
        ),
    ]
    payload = {
        "role_case_count": role_case_count,
        "safety_case_count": case_count - role_case_count,
        "case_count": case_count,
        "method_count": len(campaign.methods),
        "record_count": len(campaign.records),
        "local_method_count": sum(not item.provider_backed for item in campaign.methods),
        "provider_method_count": sum(item.provider_backed for item in campaign.methods),
        "provider_conversation_count": campaign.provider_conversation_count,
        "pre_provider_block_count": campaign.pre_provider_block_count,
        "trace_matrix_verified": True,
        "methods": [item.model_dump(mode="json") for item in methods],
        "decision": decision.decision,
        "candidate_status": decision.candidate_status,
        "decision_gates": [item.model_dump(mode="json") for item in gates],
    }
    return ReviewBriefAnimationVisualData(
        **payload,
        visual_data_sha256=canonical_sha256(payload),
    )


def _validate_claim_ledger(
    campaign: ReviewBriefCampaignArtifact, story: ReviewBriefAnimationStory
) -> None:
    expected: dict[str, tuple[str, str, str]] = {
        "dataset-contract": (
            "DATASET",
            campaign.dataset.dataset_id,
            campaign.dataset.dataset_sha256,
        ),
        "campaign-contract": ("CAMPAIGN", campaign.campaign_id, campaign.campaign_sha256),
        "default-method-decision": (
            "CAMPAIGN",
            campaign.campaign_id,
            canonical_sha256(campaign.default_method_decision.model_dump(mode="json")),
        ),
    }
    for scorecard in campaign.scorecards:
        expected[f"scorecard-{scorecard.method_id}"] = (
            "METHOD_SCORECARD",
            f"{campaign.campaign_id}:{scorecard.method_id}",
            canonical_sha256(scorecard.model_dump(mode="json")),
        )
    observed = {item.claim_id: item for item in story.claim_ledger}
    if len(observed) != len(story.claim_ledger) or set(observed) != set(expected):
        raise ValueError("animation claim ledger does not exactly match campaign sources")
    for claim_id, (kind, reference, digest) in expected.items():
        item = observed[claim_id]
        if (
            item.source_kind != kind
            or item.source_reference != reference
            or item.source_sha256 != digest
        ):
            raise ValueError(f"animation claim {claim_id} is not bound to its exact source")
    cited = {claim_id for scene in story.scenes for claim_id in scene.evidence_claim_ids}
    if cited != set(expected) or any(not scene.evidence_claim_ids for scene in story.scenes):
        raise ValueError("animation scenes must cite every and only claim-ledger source")


def render_review_brief_animation_transcript(
    contract: ReviewBriefAnimationRenderContract | Mapping[str, Any],
) -> str:
    parsed = (
        contract
        if isinstance(contract, ReviewBriefAnimationRenderContract)
        else ReviewBriefAnimationRenderContract.model_validate(contract)
    )
    lines = [
        "EcoCorridor Review Brief evaluation — accessible silent-film transcript",
        f"Campaign SHA-256: {parsed.campaign_sha256}",
        f"Story SHA-256: {parsed.story_sha256}",
        "Media status: render-authorized only after complete provider and trace verification.",
        "",
    ]
    elapsed = 0.0
    for scene in parsed.story.scenes:
        end = elapsed + scene.duration_seconds
        lines.extend(
            [
                f"[{elapsed:05.1f}–{end:05.1f}] {scene.scene_id}",
                scene.takeaway,
                *[f"- {item}" for item in scene.visible_copy],
                f"Evidence claims: {', '.join(scene.evidence_claim_ids)}",
                "",
            ]
        )
        elapsed = end
    lines.extend(["Interpretation limits:", *[f"- {item}" for item in parsed.story.limitations]])
    return "\n".join(lines).rstrip() + "\n"


def build_review_brief_animation_render_contract(
    campaign: ReviewBriefCampaignArtifact | Mapping[str, Any],
) -> ReviewBriefAnimationRenderContract:
    """Authorize a deterministic render only from complete traced campaign evidence."""

    verified = verify_review_brief_campaign(campaign)
    _validate_trace_matrix(verified)
    if (
        verified.provider_conversation_count != EXPECTED_PROVIDER_CONVERSATION_COUNT
        or verified.pre_provider_block_count != EXPECTED_PRE_PROVIDER_BLOCK_COUNT
    ):
        raise ValueError("animation campaign trace accounting is incomplete")
    story = build_review_brief_animation_story(verified)
    _validate_claim_ledger(verified, story)
    visual_data = build_review_brief_animation_visual_data(verified)
    duration = sum(item.duration_seconds for item in story.scenes)
    starts: list[float] = []
    elapsed = 0.0
    for scene in story.scenes:
        starts.append(elapsed)
        elapsed += scene.duration_seconds
    checkpoints = [
        ReviewBriefAnimationCheckpoint(
            name=f"{index:02d}-{scene.scene_id}",
            scene_id=scene.scene_id,
            time_seconds=start + scene.duration_seconds / 2,
        )
        for index, (scene, start) in enumerate(zip(story.scenes, starts, strict=True), start=1)
    ]
    observation_index = next(
        index for index, scene in enumerate(story.scenes) if scene.scene_id == "observations"
    )
    poster_time = checkpoints[observation_index].time_seconds
    film_spec = ReviewBriefAnimationFilmSpec(
        slug=FILM_SLUG,
        title="Can a review brief stay grounded and fail closed?",
        width=FILM_WIDTH,
        height=FILM_HEIGHT,
        duration_seconds=duration,
        fps=FILM_FPS,
        poster_time_seconds=poster_time,
        reduced_motion_time_seconds=poster_time,
        silent=True,
        checkpoints=checkpoints,
    )
    renderer_sha256, validator_sha256, frame_auditor_sha256 = animation_tool_sha256s()
    provisional: dict[str, Any] = {
        "schema_version": RENDER_CONTRACT_SCHEMA,
        "campaign_sha256": verified.campaign_sha256,
        "dataset_sha256": verified.dataset.dataset_sha256,
        "source_repository": verified.source_repository,
        "source_commit": verified.source_commit,
        "campaign_wandb_run_id": verified.campaign_wandb_run_id,
        "campaign_wandb_run_url": verified.campaign_wandb_run_url,
        "provider_backed": True,
        "record_count": EXPECTED_RECORD_COUNT,
        "provider_conversation_count": EXPECTED_PROVIDER_CONVERSATION_COUNT,
        "pre_provider_block_count": EXPECTED_PRE_PROVIDER_BLOCK_COUNT,
        "verified_provider_answer_count": verified.verified_provider_answer_count,
        "story": story.model_dump(mode="json"),
        "story_sha256": story.story_sha256,
        "claim_ledger_sha256": canonical_sha256(
            [item.model_dump(mode="json") for item in story.claim_ledger]
        ),
        "visual_data": visual_data.model_dump(mode="json"),
        "visual_data_sha256": visual_data.visual_data_sha256,
        "transcript_sha256": "0" * 64,
        "renderer_sha256": renderer_sha256,
        "validator_sha256": validator_sha256,
        "frame_auditor_sha256": frame_auditor_sha256,
        "film_spec": film_spec.model_dump(mode="json"),
        "render_authorized": True,
    }
    # The transcript includes contract identifiers but not the contract or transcript
    # digests, avoiding a self-referential hash.  Compute it from a temporary valid
    # model, then seal the final contract.
    temporary_payload = {**provisional, "render_contract_sha256": "0" * 64}
    temporary_payload["render_contract_sha256"] = canonical_sha256(
        {key: value for key, value in temporary_payload.items() if key != "render_contract_sha256"}
    )
    temporary = ReviewBriefAnimationRenderContract.model_validate(temporary_payload)
    transcript = render_review_brief_animation_transcript(temporary)
    provisional["transcript_sha256"] = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    return ReviewBriefAnimationRenderContract(
        **provisional,
        render_contract_sha256=canonical_sha256(provisional),
    )


def _browser_film_spec(spec: ReviewBriefAnimationFilmSpec) -> dict[str, Any]:
    return {
        "slug": spec.slug,
        "title": spec.title,
        "width": spec.width,
        "height": spec.height,
        "duration": spec.duration_seconds,
        "fps": spec.fps,
        "posterTime": spec.poster_time_seconds,
        "reducedMotionTime": spec.reduced_motion_time_seconds,
        "silent": spec.silent,
        "checkpoints": [
            {"name": item.name, "time": item.time_seconds} for item in spec.checkpoints
        ],
    }


def render_review_brief_animation_html(
    contract: ReviewBriefAnimationRenderContract | Mapping[str, Any],
) -> str:
    """Return deterministic, local-only HTML implementing the Canvas-film hooks."""

    parsed = (
        contract
        if isinstance(contract, ReviewBriefAnimationRenderContract)
        else ReviewBriefAnimationRenderContract.model_validate(contract)
    )
    story_json = json.dumps(
        parsed.story.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).replace("<", "\\u003c")
    visual_json = json.dumps(
        parsed.visual_data.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).replace("<", "\\u003c")
    spec_json = json.dumps(
        _browser_film_spec(parsed.film_spec), sort_keys=True, separators=(",", ":")
    ).replace("<", "\\u003c")
    campaign_json = json.dumps(parsed.campaign_sha256)
    contract_json = json.dumps(parsed.render_contract_sha256)
    duration_text = (
        f"{int(parsed.film_spec.duration_seconds) // 60:02d}:"
        f"{int(parsed.film_spec.duration_seconds) % 60:02d}"
    )
    template = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EcoCorridor Review Brief evaluation</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: #102521; }
    body { display: grid; min-height: 100vh; place-items: center; }
    main { position: relative; width: min(100vw, calc(100vh * 16 / 9)); aspect-ratio: 16 / 9; overflow: hidden; background: #f4f1e8; }
    canvas { display: block; width: 100%; height: 100%; }
    .controls { position: absolute; display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center; left: 24px; right: 24px; bottom: 14px; padding: 8px 12px; color: #f4f1e8; background: rgb(16 37 33 / 92%); border: 1px solid rgb(244 241 232 / 32%); border-radius: 8px; }
    button { min-width: 62px; border: 1px solid #a9cf64; border-radius: 5px; padding: 5px 10px; color: #f4f1e8; background: #244a41; font: 600 13px inherit; }
    input[type="range"] { width: 100%; accent-color: #a9cf64; }
    output { min-width: 96px; text-align: right; font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
    @media (prefers-reduced-motion: reduce) { .controls { transition: none; } }
  </style>
</head>
<body>
  <main aria-label="Interactive animation: Can a review brief stay grounded and fail closed?">
    <canvas id="film" width="1280" height="720" role="img" aria-label="Claim-ledger-bound EcoCorridor Review Brief evaluation film"></canvas>
    <div class="controls">
      <button id="toggle" type="button" aria-label="Pause animation">Pause</button>
      <input id="scrub" type="range" min="0" max="__DURATION__" step="0.01" value="0" aria-label="Animation timeline">
      <output id="clock" for="scrub">00:00 / __DURATION_TEXT__</output>
    </div>
  </main>
  <script>
    const FILM = Object.freeze(__SPEC__);
    const STORY = Object.freeze(__STORY__);
    const VISUAL = Object.freeze(__VISUAL__);
    const CAMPAIGN_SHA = __CAMPAIGN__;
    const CONTRACT_SHA = __CONTRACT__;
    window.__filmSpec = FILM;
    const canvas = document.querySelector('#film');
    const ctx = canvas.getContext('2d', { alpha: false });
    const toggle = document.querySelector('#toggle');
    const scrub = document.querySelector('#scrub');
    const clock = document.querySelector('#clock');
    const C = Object.freeze({ paper:'#f4f1e8', ink:'#102521', forest:'#244a41', cyan:'#51b9b0', lichen:'#a9cf64', amber:'#e7a947', berry:'#a54858', mist:'#d9e1d3', line:'#b8c5b9', white:'#fffdf6' });
    const sceneStarts = STORY.scenes.map((_, index) => STORY.scenes.slice(0, index).reduce((sum, scene) => sum + scene.duration_seconds, 0));
    let playing = true;
    let manualTime = 0;
    let anchorTime = performance.now();
    let anchorFilm = 0;
    let renderMode = false;
    const clamp = (value, minimum=0, maximum=1) => Math.max(minimum, Math.min(maximum, value));
    const ease = value => { const x=clamp(value); return 1-Math.pow(1-x,3); };
    const formatTime = seconds => { const value=Math.max(0,Math.floor(seconds)); return `${String(Math.floor(value/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`; };
    function rect(x,y,w,h,fill,stroke=null,radius=0) { ctx.beginPath(); ctx.roundRect(x,y,w,h,radius); ctx.fillStyle=fill; ctx.fill(); if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();} }
    function line(x1,y1,x2,y2,color=C.line,width=2) { ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke(); }
    function text(value,x,y,size=20,color=C.ink,weight=500,align='left',family='sans-serif') { ctx.fillStyle=color;ctx.font=`${weight} ${size}px ${family}`;ctx.textAlign=align;ctx.textBaseline='alphabetic';ctx.fillText(String(value),x,y); }
    function wrap(value,x,y,maxWidth,lineHeight,size=20,color=C.ink,weight=500) { const words=String(value).split(' ');let row='';let offset=0;for(const word of words){const candidate=row?`${row} ${word}`:word;if(ctx.measureText(candidate).width>maxWidth&&row){text(row,x,y+offset,size,color,weight);row=word;offset+=lineHeight;}else row=candidate;}if(row)text(row,x,y+offset,size,color,weight);return offset+lineHeight; }
    function alpha(local,duration,fade=.5){return clamp(local/fade)*clamp((duration-local)/fade);}
    function card(x,y,w,h,title,body,color,amount=1){ctx.save();ctx.globalAlpha=clamp(amount);rect(x,y+(1-amount)*16,w,h,C.white,color,12);text(title,x+22,y+40+(1-amount)*16,16,color,700);wrap(body,x+22,y+78+(1-amount)*16,w-44,26,19,C.ink,550);ctx.restore();}
    function background(time,index){rect(0,0,FILM.width,FILM.height,C.paper);ctx.globalAlpha=.24;for(let ring=0;ring<7;ring++){ctx.beginPath();ctx.ellipse(1110,105,160+ring*42,65+ring*20,-.25,0,Math.PI*2);ctx.strokeStyle=C.line;ctx.lineWidth=1.5;ctx.stroke();}ctx.globalAlpha=1;rect(0,0,18,FILM.height,C.forest);text('ECOCORRIDOR  /  VERIFIED PROVIDER CAMPAIGN',54,50,15,C.forest,750);text(`scene ${index+1} of ${STORY.scenes.length}`,1220,50,14,C.forest,650,'right','ui-monospace, monospace');line(54,70,1226,70,C.line,1);const step=1172/STORY.scenes.length;STORY.scenes.forEach((scene,i)=>{const x=54+i*step;rect(x,648,step-8,5,i<index?C.lichen:i===index?C.cyan:C.line);text(scene.scene_id.replaceAll('-',' '),x,636,11,i===index?C.forest:'#66766f',650);});text(CAMPAIGN_SHA.slice(0,16),1226,678,11,'#66766f',600,'right','ui-monospace, monospace');}
    function heading(kicker,title,subtitle){text(kicker.toUpperCase(),54,118,16,C.berry,750);wrap(title,54,180,1000,56,47,C.ink,680);wrap(subtitle,54,248,1120,30,21,C.forest,520);}
    function drawQuestion(local,duration){heading('Question','Can a short review brief stay grounded—and fail closed?',STORY.scenes[0].visible_copy[1]);const p=ease((local-.6)/1);line(132,380,1148,380,C.line,4);[['EVIDENCE',C.lichen],['REVIEW',C.cyan],['DECISION',C.amber]].forEach((item,i)=>{const x=180+i*480;const a=ease((local-(.8+i*.35))/.8);ctx.save();ctx.globalAlpha=a;ctx.beginPath();ctx.arc(x,380,52,0,Math.PI*2);ctx.fillStyle=item[1];ctx.fill();text(item[0],x,388,15,C.ink,780,'center');if(i<2){line(x+56,380,x+420*p,380,C.forest,5);}ctx.restore();});text('Synthetic connectivity screening · no ecological outcome claim',54,555,18,C.berry,700);}
    function drawDataset(local,duration){heading('Fixed dataset',STORY.scenes[1].takeaway,STORY.scenes[1].visible_copy[2]);const values=[VISUAL.role_case_count,VISUAL.safety_case_count];const labels=['DIRECT-EVIDENCE ROLES','FAIL-CLOSED CONDITIONS'];const colors=[C.lichen,C.berry];values.forEach((value,i)=>{const y=340+i*112;const amount=ease((local-(.5+i*.25))/.9);text(labels[i],54,y,15,C.forest,750);rect(360,y-28,760,42,C.mist);rect(360,y-28,760*(value/VISUAL.case_count)*amount,42,colors[i]);text(value,1148,y+4,36,C.ink,760,'right');});text(`${VISUAL.case_count} cases × ${VISUAL.method_count} fixed methods = ${VISUAL.record_count} scored records`,54,562,25,C.ink,700);}
    function drawMethods(local,duration){heading('Controlled comparison',STORY.scenes[2].takeaway,`${VISUAL.local_method_count} local control · ${VISUAL.provider_method_count} provider-backed methods`);VISUAL.methods.forEach((method,i)=>{const x=54+i*392;const a=ease((local-(.4+i*.25))/.8);card(x,328,350,174,method.display_name.toUpperCase(),method.provider_backed?'PROVIDER-BACKED':'LOCAL CONTROL',method.provider_backed?C.cyan:C.lichen,a);});rect(54,535,1134,54,C.forest,null,8);text('same cases  ·  same evidence boundary  ·  same deterministic scorers',621,570,19,C.white,650,'center');}
    function drawTrust(local,duration){heading('Trust path',STORY.scenes[3].takeaway,'A readable answer is released only after every boundary is verified.');const verified=VISUAL.trace_matrix_verified;const items=[['PROVIDER CALL',C.cyan],['EVIDENCE CHECK',C.lichen],['WEAVE READBACK',C.amber],['RELEASE',C.forest]];items.forEach((item,i)=>{const x=54+i*294;const a=ease((local-(.4+i*.4))/.7);card(x,337,258,130,item[0],i===3?'fail closed':verified?'verified receipt':'blocked',item[1],a);if(i<3){line(x+258,402,x+292,402,C.forest,4);}});text('No readable trace → no answer     ·     no authorized evidence → no numeric claim',54,552,18,C.berry,720);}
    function drawObservations(local,duration){heading('Observed checks',STORY.scenes[4].takeaway,'Exact counts from this pinned campaign; no general winner claim.');VISUAL.methods.forEach((method,i)=>{const y=326+i*82;const amount=ease((local-(.5+i*.25))/.9);text(method.display_name.toUpperCase(),54,y,14,C.forest,720);rect(390,y-26,650,34,C.mist);rect(390,y-26,650*(method.passed_case_count/method.case_count)*amount,34,method.deterministic_gate_passed?C.lichen:C.berry);text(`${method.passed_case_count}/${method.case_count}`,1120,y+2,25,C.ink,760,'right');});text('Deterministic gates, not a capability ranking',54,572,18,C.berry,700);}
    function drawDecision(local,duration){heading('Default-method gate','The policy records a decision; it does not change the desktop.','Promotion requires every quality, safety, context, cost, and topic-grounding gate.');VISUAL.decision_gates.forEach((gate,i)=>{const x=54+(i%3)*245;const y=330+Math.floor(i/3)*92;const a=ease((local-(.4+i*.15))/.7);ctx.save();ctx.globalAlpha=a;rect(x,y,220,64,gate.passed?C.lichen:C.amber,C.forest,8);text(gate.passed?'✓':'—',x+22,y+40,22,C.ink,780);text(gate.label,x+58,y+39,16,C.ink,680);ctx.restore();});rect(842,330,346,156,VISUAL.decision==='PROMOTE_NATIVE'?C.lichen:C.amber,C.forest,12);text('RECORDED DECISION',1015,370,14,C.forest,750,'center');text(VISUAL.decision,1015,429,28,C.ink,800,'center','ui-monospace, monospace');text(VISUAL.candidate_status==='PROMOTION_ELIGIBLE'?'Native candidate met this pinned campaign gate.':'Native candidate remains experimental under this pinned campaign.',54,566,18,C.berry,700);}
    function drawLimits(local,duration){heading('Interpretation boundary',STORY.scenes[6].takeaway,'The next experiment expands evidence; it does not expand this claim.');card(54,318,546,196,'THIS CAMPAIGN TESTS','Evidence handling, traceability, and fail-closed behavior.',C.lichen,ease((local-.5)/.8));card(642,318,546,196,'IT DOES NOT ESTABLISH','Animal movement, crossing use, collision reduction, population effect, or approval.',C.berry,ease((local-.8)/.8));text(`contract ${CONTRACT_SHA.slice(0,20)}…`,54,570,14,C.forest,650,'left','ui-monospace, monospace');}
    const drawers=[drawQuestion,drawDataset,drawMethods,drawTrust,drawObservations,drawDecision,drawLimits];
    function render(time){const t=((time%FILM.duration)+FILM.duration)%FILM.duration;let index=STORY.scenes.length-1;for(let i=0;i<STORY.scenes.length;i++){if(t<sceneStarts[i]+STORY.scenes[i].duration_seconds){index=i;break;}}const local=t-sceneStarts[index];background(t,index);ctx.save();ctx.globalAlpha=alpha(local,STORY.scenes[index].duration_seconds,.45);drawers[index](local,STORY.scenes[index].duration_seconds);ctx.restore();clock.value=`${formatTime(t)} / ${formatTime(FILM.duration)}`;if(!renderMode)scrub.value=String(t);}
    function currentTime(now=performance.now()){return playing?(anchorFilm+(now-anchorTime)/1000)%FILM.duration:manualTime;}
    function loop(now){if(!renderMode)render(currentTime(now));requestAnimationFrame(loop);}
    toggle.addEventListener('click',()=>{if(playing){manualTime=currentTime();playing=false;toggle.textContent='Play';toggle.setAttribute('aria-label','Play animation');}else{anchorFilm=manualTime;anchorTime=performance.now();playing=true;toggle.textContent='Pause';toggle.setAttribute('aria-label','Pause animation');}});
    scrub.addEventListener('input',()=>{manualTime=Number(scrub.value);anchorFilm=manualTime;anchorTime=performance.now();render(manualTime);});
    window.setAnimationTime=seconds=>{renderMode=true;playing=false;manualTime=Number(seconds)||0;render(manualTime);return true;};
    window.releaseAnimation=()=>{renderMode=false;anchorFilm=manualTime;anchorTime=performance.now();playing=true;toggle.textContent='Pause';toggle.setAttribute('aria-label','Pause animation');return true;};
    document.fonts.ready.then(()=>{const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;if(reduced){playing=false;manualTime=FILM.reducedMotionTime;toggle.textContent='Play';toggle.setAttribute('aria-label','Play animation');}render(reduced?FILM.reducedMotionTime:0);window.__animationReady=true;requestAnimationFrame(loop);});
  </script>
</body>
</html>
"""
    rendered = (
        template.replace("__SPEC__", spec_json)
        .replace("__STORY__", story_json)
        .replace("__VISUAL__", visual_json)
        .replace("__CAMPAIGN__", campaign_json)
        .replace("__CONTRACT__", contract_json)
        .replace("__DURATION__", str(parsed.film_spec.duration_seconds))
        .replace("__DURATION_TEXT__", duration_text)
    )
    if re.search(
        r"(?:src|href)\s*=\s*[\"']https?://|url\(\s*[\"']?https?://",
        rendered,
        re.IGNORECASE,
    ):
        raise ValueError("review-brief animation HTML contains a network asset")
    for hook in (
        "window.__filmSpec",
        "window.__animationReady",
        "window.setAnimationTime",
        "window.releaseAnimation",
        'id="film"',
        'id="toggle"',
        'id="scrub"',
        'id="clock"',
    ):
        if hook not in rendered:
            raise ValueError(f"review-brief animation HTML lacks required hook: {hook}")
    return rendered


def expected_review_brief_animation_files(
    contract: ReviewBriefAnimationRenderContract,
) -> dict[str, tuple[str, str]]:
    spec = contract.film_spec
    expected: dict[str, tuple[str, str]] = {
        f"{spec.slug}.html": ("INTERACTIVE_HTML", "text/html"),
        f"{spec.slug}.mp4": ("VIDEO", "video/mp4"),
        f"{spec.slug}-poster.png": ("POSTER", "image/png"),
        "contact-sheet.png": ("CONTACT_SHEET", "image/png"),
        "review-brief-animation-transcript.txt": ("TRANSCRIPT", "text/plain"),
        "review-brief-animation-render-contract.json": (
            "RENDER_CONTRACT",
            "application/json",
        ),
        "review-brief-animation-validation.json": (
            "VALIDATION_RECEIPT",
            "application/json",
        ),
    }
    expected.update({f"{item.name}.png": ("CHECKPOINT", "image/png") for item in spec.checkpoints})
    return expected


def build_review_brief_animation_validation_receipt(
    *,
    contract: ReviewBriefAnimationRenderContract,
    html_sha256: str,
    raw_validator_result: Mapping[str, Any],
) -> ReviewBriefAnimationValidationReceipt:
    """Normalize and strictly bind the Canvas validator result to the render contract."""

    spec = contract.film_spec
    browser_spec = raw_validator_result.get("spec")
    if raw_validator_result.get("ok") is not True or browser_spec != _browser_film_spec(spec):
        raise ValueError("Canvas validator result does not match the exact film spec")
    video = raw_validator_result.get("video")
    reduced = raw_validator_result.get("reducedMotion")
    responsive = raw_validator_result.get("responsive")
    encoding_inputs_raw = raw_validator_result.get("encodingInputFrames")
    second_pass_raw = raw_validator_result.get("secondPassEncodingInputFrames")
    runtime_tools_raw = raw_validator_result.get("runtimeTools")
    if (
        raw_validator_result.get("mp4ValidationMode")
        != "DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE"
    ):
        raise ValueError(
            "Canvas validator must distinguish MP4 decode integrity from JPEG input identity"
        )
    if not isinstance(video, Mapping) or not isinstance(reduced, Mapping):
        raise TypeError("Canvas validator result lacks video or reduced-motion evidence")
    if (
        raw_validator_result.get("encodingInputFormat") != "jpeg"
        or raw_validator_result.get("encodingInputJpegQuality") != 92
    ):
        raise ValueError("Canvas encoding inputs must be JPEG quality 92")
    if raw_validator_result.get("frameAuditorSha256") != contract.frame_auditor_sha256:
        raise ValueError("Canvas JPEG frame auditor differs from the render contract")
    if not isinstance(encoding_inputs_raw, list) or not isinstance(second_pass_raw, list):
        raise TypeError("Canvas validator result lacks both ordered JPEG encoding-input passes")
    encoding_inputs = [
        ReviewBriefAnimationEncodingInputFrame.model_validate(item) for item in encoding_inputs_raw
    ]
    second_pass = [
        ReviewBriefAnimationEncodingInputFrame.model_validate(item) for item in second_pass_raw
    ]
    encoding_input_payload = [item.model_dump(mode="json") for item in encoding_inputs]
    second_payload = [item.model_dump(mode="json") for item in second_pass]
    encoding_input_root = canonical_sha256(encoding_input_payload)
    second_root = canonical_sha256(second_payload)
    if encoding_input_payload != second_payload:
        raise ValueError("independent JPEG encoding-input passes differ")
    if not isinstance(runtime_tools_raw, list):
        raise TypeError("Canvas validator result lacks render-runtime evidence")
    runtime_tools = sorted(
        [ReviewBriefAnimationRuntimeTool.model_validate(item) for item in runtime_tools_raw],
        key=lambda item: item.name,
    )
    runtime_payload = [item.model_dump(mode="json") for item in runtime_tools]
    if (
        reduced.get("text") != "Play"
        or reduced.get("label") != "Play animation"
        or float(reduced.get("time", -1)) != spec.reduced_motion_time_seconds
    ):
        raise ValueError("Canvas validator reduced-motion evidence is inconsistent")
    if not isinstance(responsive, list) or len(responsive) != 2:
        raise ValueError("Canvas validator responsive evidence is inconsistent")
    payload: dict[str, Any] = {
        "schema_version": VALIDATION_RECEIPT_SCHEMA,
        "status": "VALIDATED",
        "campaign_sha256": contract.campaign_sha256,
        "story_sha256": contract.story_sha256,
        "visual_data_sha256": contract.visual_data_sha256,
        "render_contract_sha256": contract.render_contract_sha256,
        "html_sha256": html_sha256,
        "transcript_sha256": contract.transcript_sha256,
        "validator_sha256": contract.validator_sha256,
        "frame_auditor_sha256": contract.frame_auditor_sha256,
        "deterministic_frame_sha256": raw_validator_result.get("deterministicSha256"),
        "encoding_input_format": "jpeg",
        "encoding_input_jpeg_quality": 92,
        "encoding_input_frames": encoding_input_payload,
        "encoding_input_manifest_sha256": encoding_input_root,
        "second_pass_encoding_input_manifest_sha256": second_root,
        "encoding_input_determinism_verified": True,
        "runtime_tools": runtime_payload,
        "runtime_executable_manifest_sha256": canonical_sha256(runtime_payload),
        "mp4_validation_mode": "DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE",
        "responsive_viewport": tuple(int(item) for item in responsive),
        "reduced_motion_time_seconds": float(reduced["time"]),
        "video": {
            "codec": video.get("codec"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": float(video.get("fps", -1)),
            "frames": video.get("frames"),
            "duration_seconds": video.get("duration"),
            "pixel_format": video.get("pixelFormat"),
            "color_range": video.get("colorRange"),
            "audio": video.get("audio"),
        },
    }
    parsed_video = ReviewBriefAnimationVideoValidation.model_validate(payload["video"])
    if parsed_video.frames != round(spec.duration_seconds * spec.fps):
        raise ValueError("Canvas validator frame count differs from the film spec")
    if len(encoding_inputs) != parsed_video.frames:
        raise ValueError("Canvas JPEG input count differs from the encoded frame count")
    for index, frame in enumerate(encoding_inputs):
        if (
            frame.frame_index != index
            or frame.logical_name != f"frame-{index:06d}.jpg"
            or abs(frame.time_seconds - index / spec.fps) > 1e-9
        ):
            raise ValueError("Canvas JPEG encoding-input schedule differs from the film spec")
    if abs(parsed_video.duration_seconds - spec.duration_seconds) > 1 / spec.fps:
        raise ValueError("Canvas validator duration differs from the film spec")
    return ReviewBriefAnimationValidationReceipt(
        **payload,
        validation_receipt_sha256=canonical_sha256(payload),
    )


def _png_dimensions(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    if len(content) < 45 or content[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"review-brief animation PNG is malformed: {path.name}")
    position = 8
    dimensions: tuple[int, int] | None = None
    saw_image_data = False
    saw_end = False
    while position + 12 <= len(content):
        length = struct.unpack(">I", content[position : position + 4])[0]
        kind = content[position + 4 : position + 8]
        data_start = position + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(content):
            raise ValueError(f"review-brief animation PNG chunk is truncated: {path.name}")
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        if zlib.crc32(kind + content[data_start:data_end]) & 0xFFFFFFFF != expected_crc:
            raise ValueError(f"review-brief animation PNG CRC differs: {path.name}")
        if kind == b"IHDR":
            if dimensions is not None or length != 13:
                raise ValueError(f"review-brief animation PNG header is invalid: {path.name}")
            dimensions = struct.unpack(">II", content[data_start : data_start + 8])
        elif kind == b"IDAT":
            saw_image_data = True
        elif kind == b"IEND":
            saw_end = True
            if crc_end != len(content):
                raise ValueError(f"review-brief animation PNG has trailing bytes: {path.name}")
            break
        position = crc_end
    if dimensions is None or not saw_image_data or not saw_end:
        raise ValueError(f"review-brief animation PNG structure is incomplete: {path.name}")
    return dimensions


def _validate_local_media_file(
    path: Path,
    *,
    role: str,
    spec: ReviewBriefAnimationFilmSpec,
) -> None:
    size = path.stat().st_size
    limit = (
        MAX_VIDEO_BYTES
        if role == "VIDEO"
        else MAX_PNG_BYTES
        if path.suffix == ".png"
        else MAX_TEXT_BYTES
    )
    if size <= 0 or size > limit:
        raise ValueError(f"review-brief animation file size is invalid: {path.name}")
    if role == "VIDEO":
        with path.open("rb") as stream:
            header = stream.read(12)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise ValueError("review-brief animation video lacks an MP4 file-type box")
    elif path.suffix == ".png":
        dimensions = _png_dimensions(path)
        if role == "CONTACT_SHEET":
            columns = min(4, len(spec.checkpoints))
            rows = (len(spec.checkpoints) + columns - 1) // columns
            expected = (columns * 320, rows * round(320 * spec.height / spec.width))
        else:
            expected = (spec.width, spec.height)
        if dimensions != expected:
            raise ValueError(
                f"review-brief animation PNG dimensions differ for {path.name}: {dimensions}"
            )


def build_review_brief_animation_media_manifest(
    *,
    campaign: ReviewBriefCampaignArtifact | Mapping[str, Any],
    contract: ReviewBriefAnimationRenderContract | Mapping[str, Any],
    validation_receipt: ReviewBriefAnimationValidationReceipt | Mapping[str, Any],
    media_directory: str | Path,
) -> ReviewBriefAnimationMediaManifest:
    """Hash a fully rendered bundle after strict campaign and validator reconciliation."""

    verified = verify_review_brief_campaign(campaign)
    expected_contract = build_review_brief_animation_render_contract(verified)
    parsed_contract = (
        contract
        if isinstance(contract, ReviewBriefAnimationRenderContract)
        else ReviewBriefAnimationRenderContract.model_validate(contract)
    )
    if parsed_contract != expected_contract:
        raise ValueError("animation render contract does not match the verified campaign")
    receipt = (
        validation_receipt
        if isinstance(validation_receipt, ReviewBriefAnimationValidationReceipt)
        else ReviewBriefAnimationValidationReceipt.model_validate(validation_receipt)
    )
    if (
        receipt.campaign_sha256 != parsed_contract.campaign_sha256
        or receipt.story_sha256 != parsed_contract.story_sha256
        or receipt.visual_data_sha256 != parsed_contract.visual_data_sha256
        or receipt.render_contract_sha256 != parsed_contract.render_contract_sha256
        or receipt.transcript_sha256 != parsed_contract.transcript_sha256
        or receipt.validator_sha256 != parsed_contract.validator_sha256
        or receipt.frame_auditor_sha256 != parsed_contract.frame_auditor_sha256
    ):
        raise ValueError("animation validation receipt does not match the render contract")
    directory = Path(media_directory).resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("review-brief animation media directory is missing or unsafe")
    expected = expected_review_brief_animation_files(parsed_contract)
    observed = {item.name for item in directory.iterdir() if item.is_file()}
    allowed_observed = (set(expected), {*expected, MEDIA_MANIFEST_FILENAME})
    if observed not in allowed_observed:
        raise ValueError("review-brief animation directory does not contain the exact output set")
    files: list[ReviewBriefAnimationMediaFile] = []
    for logical_name, (role, media_type) in sorted(expected.items()):
        if not _SAFE_NAME.fullmatch(logical_name):
            raise ValueError("review-brief animation output name is unsafe")
        path = directory / logical_name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
            raise ValueError(f"review-brief animation output is missing or unsafe: {logical_name}")
        _validate_local_media_file(path, role=role, spec=parsed_contract.film_spec)
        files.append(
            ReviewBriefAnimationMediaFile(
                logical_name=logical_name,
                role=role,
                media_type=media_type,
                bytes=path.stat().st_size,
                sha256=_sha256_file(path),
            )
        )
    by_name = {item.logical_name: item for item in files}
    if by_name[f"{FILM_SLUG}.html"].sha256 != receipt.html_sha256:
        raise ValueError("animation HTML differs from the validator receipt")
    if by_name["review-brief-animation-transcript.txt"].sha256 != parsed_contract.transcript_sha256:
        raise ValueError("animation transcript differs from the render contract")
    if (
        by_name["review-brief-animation-validation.json"].sha256
        != hashlib.sha256(
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        ).hexdigest()
    ):
        raise ValueError("animation validation receipt file differs from validated content")
    if (
        by_name["review-brief-animation-render-contract.json"].sha256
        != hashlib.sha256(
            json.dumps(parsed_contract.model_dump(mode="json"), indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        ).hexdigest()
    ):
        raise ValueError("animation render-contract file differs from validated content")
    file_payloads = [item.model_dump(mode="json") for item in files]
    payload: dict[str, Any] = {
        "schema_version": MEDIA_MANIFEST_SCHEMA,
        "status": "VALIDATED",
        "campaign_sha256": parsed_contract.campaign_sha256,
        "dataset_sha256": parsed_contract.dataset_sha256,
        "source_commit": parsed_contract.source_commit,
        "campaign_wandb_run_id": parsed_contract.campaign_wandb_run_id,
        "campaign_wandb_run_url": parsed_contract.campaign_wandb_run_url,
        "story_sha256": parsed_contract.story_sha256,
        "claim_ledger_sha256": parsed_contract.claim_ledger_sha256,
        "visual_data_sha256": parsed_contract.visual_data_sha256,
        "frame_auditor_sha256": parsed_contract.frame_auditor_sha256,
        "render_contract_sha256": parsed_contract.render_contract_sha256,
        "validation_receipt_sha256": receipt.validation_receipt_sha256,
        "encoding_input_manifest_sha256": receipt.encoding_input_manifest_sha256,
        "encoding_input_frame_count": len(receipt.encoding_input_frames),
        "encoding_input_determinism_verified": True,
        "runtime_executable_manifest_sha256": receipt.runtime_executable_manifest_sha256,
        "mp4_validation_mode": receipt.mp4_validation_mode,
        "film_spec": parsed_contract.film_spec.model_dump(mode="json"),
        "files": file_payloads,
        "files_sha256": canonical_sha256(file_payloads),
    }
    return ReviewBriefAnimationMediaManifest(
        **payload,
        media_manifest_sha256=canonical_sha256(payload),
    )


def validate_review_brief_animation_media_manifest(
    manifest: ReviewBriefAnimationMediaManifest | Mapping[str, Any],
    *,
    campaign: ReviewBriefCampaignArtifact | Mapping[str, Any],
    media_directory: str | Path,
) -> ReviewBriefAnimationMediaManifest:
    """Read back every media byte and re-bind the bundle to the exact campaign."""

    parsed = (
        manifest
        if isinstance(manifest, ReviewBriefAnimationMediaManifest)
        else ReviewBriefAnimationMediaManifest.model_validate(manifest)
    )
    directory = Path(media_directory).resolve()
    receipt_path = directory / "review-brief-animation-validation.json"
    contract_path = directory / "review-brief-animation-render-contract.json"
    if receipt_path.is_symlink() or contract_path.is_symlink():
        raise ValueError("review-brief animation contract or receipt is unsafe")
    receipt = ReviewBriefAnimationValidationReceipt.model_validate_json(receipt_path.read_bytes())
    contract = ReviewBriefAnimationRenderContract.model_validate_json(contract_path.read_bytes())
    rebuilt = build_review_brief_animation_media_manifest(
        campaign=campaign,
        contract=contract,
        validation_receipt=receipt,
        media_directory=directory,
    )
    if parsed != rebuilt:
        raise ValueError("review-brief animation media manifest differs from local media")
    return parsed


def load_validated_review_brief_animation_media(
    manifest_path: str | Path,
    *,
    campaign: ReviewBriefCampaignArtifact | Mapping[str, Any],
) -> tuple[ReviewBriefAnimationMediaManifest, Path]:
    source = Path(manifest_path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("review-brief animation media manifest is missing, unsafe, or oversized")
    manifest = ReviewBriefAnimationMediaManifest.model_validate_json(source.read_bytes())
    directory = source.resolve().parent
    return (
        validate_review_brief_animation_media_manifest(
            manifest,
            campaign=campaign,
            media_directory=directory,
        ),
        directory,
    )
