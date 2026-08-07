from __future__ import annotations

import hashlib
import inspect
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.review_brief_animation import (
    FILM_FPS,
    FILM_HEIGHT,
    FILM_SLUG,
    FILM_WIDTH,
    ReviewBriefAnimationCheckpoint,
    ReviewBriefAnimationFilmSpec,
    _validate_trace_matrix,
    animation_tool_sha256s,
    build_review_brief_animation_validation_receipt,
    build_review_brief_animation_visual_data,
    render_review_brief_animation_html,
)
from ecocorridor_core.review_brief_evaluation import METHOD_IDS, PRE_PROVIDER_BLOCK_CASES

ROOT = Path(__file__).resolve().parents[2]


def _visual_campaign(*, promotion_eligible: bool) -> SimpleNamespace:
    methods = [
        SimpleNamespace(method_id=method_id, provider_backed=index > 0)
        for index, method_id in enumerate(METHOD_IDS)
    ]
    return SimpleNamespace(
        dataset=SimpleNamespace(
            cases=[
                *[SimpleNamespace(category="RIDGEWAY_ROLE") for _ in range(4)],
                *[SimpleNamespace(category="PROMPT_INJECTION") for _ in range(10)],
            ]
        ),
        methods=methods,
        scorecards=[
            SimpleNamespace(
                method_id=method_id,
                passed_case_count=14 - index,
                case_count=14,
                deterministic_gate_passed=index == 0,
            )
            for index, method_id in enumerate(METHOD_IDS)
        ],
        records=[object() for _ in range(42)],
        provider_conversation_count=22,
        pre_provider_block_count=6,
        default_method_decision=SimpleNamespace(
            candidate_all_gates_passed=True,
            role_quality_noninferior=True,
            safety_quality_noninferior=True,
            context_reduction_verified=True,
            cost_comparison_complete=promotion_eligible,
            action_discipline_appropriateness_complete=True,
            decision="PROMOTE_NATIVE" if promotion_eligible else "KEEP_DETERMINISTIC",
            candidate_status=("PROMOTION_ELIGIBLE" if promotion_eligible else "EXPERIMENTAL"),
        ),
    )


def _film_spec() -> ReviewBriefAnimationFilmSpec:
    return ReviewBriefAnimationFilmSpec(
        slug=FILM_SLUG,
        title="Can a review brief stay grounded and fail closed?",
        width=FILM_WIDTH,
        height=FILM_HEIGHT,
        duration_seconds=1,
        fps=FILM_FPS,
        poster_time_seconds=0.4,
        reduced_motion_time_seconds=0.4,
        silent=True,
        checkpoints=[
            ReviewBriefAnimationCheckpoint(name="01-proof", scene_id="proof", time_seconds=0.4)
        ],
    )


def _raw_validator_result(spec: ReviewBriefAnimationFilmSpec) -> dict[str, Any]:
    frames = [
        {
            "frame_index": index,
            "time_seconds": round(index / spec.fps, 9),
            "logical_name": f"frame-{index:06d}.jpg",
            "bytes": 1000 + index,
            "jpeg_sha256": canonical_sha256({"frame_index": index, "format": "jpeg"}),
        }
        for index in range(round(spec.duration_seconds * spec.fps))
    ]
    return {
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
        "deterministicSha256": "a" * 64,
        "encodingInputFormat": "jpeg",
        "encodingInputJpegQuality": 92,
        "encodingInputFrames": frames,
        "secondPassEncodingInputFrames": deepcopy(frames),
        "frameAuditorSha256": "8" * 64,
        "runtimeTools": [
            {
                "name": name,
                "version": f"{name} test-runtime-v1",
                "executable_sha256": canonical_sha256({"runtime": name}),
            }
            for name in ("chrome", "ffmpeg", "ffprobe", "node")
        ],
        "mp4ValidationMode": "DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE",
        "responsive": [640, 360],
        "reducedMotion": {
            "time": spec.reduced_motion_time_seconds,
            "text": "Play",
            "label": "Play animation",
        },
        "video": {
            "codec": "h264",
            "profile": "High",
            "width": spec.width,
            "height": spec.height,
            "fps": float(spec.fps),
            "frames": len(frames),
            "duration": spec.duration_seconds,
            "pixelFormat": "yuv420p",
            "colorRange": "tv",
            "audio": False,
        },
    }


def _validation_contract(spec: ReviewBriefAnimationFilmSpec) -> SimpleNamespace:
    return SimpleNamespace(
        film_spec=spec,
        campaign_sha256="1" * 64,
        story_sha256="2" * 64,
        visual_data_sha256="3" * 64,
        render_contract_sha256="4" * 64,
        transcript_sha256="5" * 64,
        validator_sha256="6" * 64,
        frame_auditor_sha256="8" * 64,
    )


def _trace_campaign() -> SimpleNamespace:
    case_ids = (
        "ridgeway-low-cost-caution",
        "ridgeway-carry-forward",
        "ridgeway-over-budget",
        "ridgeway-hypothesis-collapse",
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
    )
    records: list[SimpleNamespace] = []
    conversation_index = 0
    for method_id in METHOD_IDS:
        for case_id in case_ids:
            if method_id == METHOD_IDS[0]:
                execution = SimpleNamespace(
                    execution_path="DETERMINISTIC_LOCAL",
                    disposition="ANSWERED",
                    provider_execution=None,
                )
            elif case_id in PRE_PROVIDER_BLOCK_CASES[method_id]:
                execution = SimpleNamespace(
                    execution_path="PRE_PROVIDER_BLOCK",
                    disposition="BLOCKED",
                    provider_execution=None,
                )
            else:
                conversation_index += 1
                provider = SimpleNamespace(
                    provider_call_id=f"provider-call-{conversation_index}",
                    weave_call_id=f"weave-call-{conversation_index}",
                    weave_trace_id=f"weave-trace-{conversation_index}",
                    weave_trace_url=f"https://wandb.example/traces/{conversation_index}",
                    trace_status="VERIFIED",
                    trace_readback_verified=True,
                )
                execution = SimpleNamespace(
                    execution_path="PROVIDER_CONVERSATION",
                    disposition="ANSWERED",
                    provider_execution=provider,
                )
            records.append(
                SimpleNamespace(method_id=method_id, case_id=case_id, execution=execution)
            )
    return SimpleNamespace(
        records=records,
        dataset=SimpleNamespace(
            cases=[SimpleNamespace(case_id=item, category="PROMPT_INJECTION") for item in case_ids]
        ),
        verified_provider_answer_count=0,
    )


def test_visual_data_seals_every_variable_number_and_decision_gate() -> None:
    experimental = build_review_brief_animation_visual_data(
        _visual_campaign(promotion_eligible=False)
    )
    assert (
        experimental.role_case_count,
        experimental.safety_case_count,
        experimental.case_count,
        experimental.method_count,
        experimental.record_count,
    ) == (4, 10, 14, 3, 42)
    assert [item.passed_case_count for item in experimental.methods] == [14, 13, 12]
    assert experimental.decision == "KEEP_DETERMINISTIC"
    assert experimental.candidate_status == "EXPERIMENTAL"
    assert (
        next(item for item in experimental.decision_gates if item.gate_id == "cost_evidence").passed
        is False
    )
    payload = experimental.model_dump(mode="json")
    observed_digest = payload.pop("visual_data_sha256")
    assert canonical_sha256(payload) == observed_digest

    eligible = build_review_brief_animation_visual_data(_visual_campaign(promotion_eligible=True))
    assert eligible.decision == "PROMOTE_NATIVE"
    assert eligible.candidate_status == "PROMOTION_ELIGIBLE"
    assert all(item.passed for item in eligible.decision_gates)
    assert eligible.visual_data_sha256 != experimental.visual_data_sha256


def test_canvas_renderer_has_no_campaign_number_or_gate_fallbacks() -> None:
    source = inspect.getsource(render_review_brief_animation_html)
    assert "const VISUAL = Object.freeze(__VISUAL__)" in source
    assert "const values=[4,10]" not in source
    assert "value/14" not in source
    assert "14 cases × 3 fixed methods = 42" not in source
    assert "complete=gate!=='cost'" not in source
    assert "VISUAL.decision_gates" in source
    assert "VISUAL.methods" in source


def test_checked_in_auditor_digest_covers_exact_jpeg_capture_settings() -> None:
    auditor = ROOT / "scripts" / "capture_ecocorridor_review_brief_jpeg_frames.mjs"
    _, _, auditor_sha256 = animation_tool_sha256s()
    content = auditor.read_bytes()
    source = content.decode("utf-8")
    assert hashlib.sha256(content).hexdigest() == auditor_sha256
    assert "const JPEG_QUALITY = 92" in source
    assert "format: 'jpeg'" in source
    assert "quality: JPEG_QUALITY" in source
    assert "jpeg_sha256" in source
    assert "format: 'png'" not in source


def test_review_brief_wrapper_avoids_a_redundant_third_full_frame_capture() -> None:
    shared_renderer = (ROOT / "scripts" / "render_ecocorridor_canvas_film.mjs").read_text(
        encoding="utf-8"
    )
    review_wrapper = (ROOT / "scripts" / "render_ecocorridor_review_brief_animation.py").read_text(
        encoding="utf-8"
    )
    assert "ECOCORRIDOR_STILLS_ONLY" in shared_renderer
    assert '"ECOCORRIDOR_STILLS_ONLY": "1"' in review_wrapper
    assert "video frames delegated to the contracted auditor" in shared_renderer


def test_validation_receipt_binds_exact_jpeg_inputs_and_runtime_executables() -> None:
    spec = _film_spec()
    raw = _raw_validator_result(spec)
    receipt = build_review_brief_animation_validation_receipt(
        contract=_validation_contract(spec),
        html_sha256="7" * 64,
        raw_validator_result=raw,
    )
    expected_root = canonical_sha256(raw["encodingInputFrames"])
    assert len(receipt.encoding_input_frames) == 15
    assert receipt.encoding_input_format == "jpeg"
    assert receipt.encoding_input_jpeg_quality == 92
    assert receipt.encoding_input_manifest_sha256 == expected_root
    assert receipt.second_pass_encoding_input_manifest_sha256 == expected_root
    assert receipt.encoding_input_determinism_verified is True
    assert receipt.frame_auditor_sha256 == "8" * 64
    assert receipt.mp4_validation_mode == "DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE"
    assert [item.name for item in receipt.runtime_tools] == [
        "chrome",
        "ffmpeg",
        "ffprobe",
        "node",
    ]
    assert receipt.runtime_executable_manifest_sha256 == canonical_sha256(
        [item.model_dump(mode="json") for item in receipt.runtime_tools]
    )


def test_validation_rejects_jpeg_input_drift_and_mp4_pixel_identity_claim() -> None:
    spec = _film_spec()
    raw = _raw_validator_result(spec)
    raw["secondPassEncodingInputFrames"][8]["jpeg_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="JPEG encoding-input passes differ"):
        build_review_brief_animation_validation_receipt(
            contract=_validation_contract(spec),
            html_sha256="7" * 64,
            raw_validator_result=raw,
        )

    raw = _raw_validator_result(spec)
    raw["mp4ValidationMode"] = "PIXEL_IDENTICAL_TO_SOURCE"
    with pytest.raises(ValueError, match="decode integrity"):
        build_review_brief_animation_validation_receipt(
            contract=_validation_contract(spec),
            html_sha256="7" * 64,
            raw_validator_result=raw,
        )


def test_trace_matrix_rejects_reused_provider_or_weave_identity() -> None:
    campaign = _trace_campaign()
    _validate_trace_matrix(campaign)
    conversations = [
        record.execution.provider_execution
        for record in campaign.records
        if record.execution.provider_execution is not None
    ]
    conversations[1].weave_call_id = conversations[0].weave_call_id
    with pytest.raises(ValueError, match="unique trace receipt"):
        _validate_trace_matrix(campaign)
