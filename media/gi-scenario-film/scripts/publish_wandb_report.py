#!/usr/bin/env python3
"""Publish the evidence-grounded GI article and its media to a W&B Report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "python"))

from gi_scenario_core.evidence_evaluation import behavior_summary
from gi_scenario_core.evidence_guide_v2_preflight_audit import verify_preflight_audit

ARTICLE = ROOT / "story" / "article.md"
MEDIA_SPEC = ROOT / "story" / "article-media.json"
BENCHMARK_MEDIA_SPEC = ROOT / "story" / "benchmark-media.json"
ANIMATION_MEDIA_SPEC = ROOT / "story" / "report-animation-media.json"
RESEARCH_RESOURCES = ROOT / "story" / "research-resources.json"
BENCHMARK_PUBLICATION = (
    REPOSITORY_ROOT / "dist" / "benchmark-publication" / "publication-manifest.json"
)
BENCHMARK_RECEIPT = REPOSITORY_ROOT / "dist" / "benchmark-publication" / "wandb-publication.json"
ANIMATION_ROOT = REPOSITORY_ROOT / "media" / "gi-scenario-report-films"
ANIMATION_MANIFEST = ANIMATION_ROOT / "dist" / "manifest.json"
ANIMATION_RECEIPT = ANIMATION_ROOT / "dist" / "wandb-publication.json"
CLIP_MANIFEST = ROOT / "dist" / "article-clips" / "manifest.json"
PUBLICATION_PLAN = ROOT / "dist" / "article-clips" / "wandb-report-plan.json"
PUBLICATION_RECEIPT = ROOT / "dist" / "article-clips" / "wandb-publication.json"
EVIDENCE_GUIDE_DATASET = REPOSITORY_ROOT / "benchmarks" / "evidence_guide" / "v1" / "dataset.json"
EVIDENCE_GUIDE_DATASET_MANIFEST = EVIDENCE_GUIDE_DATASET.with_name("manifest.json")
EVIDENCE_GUIDE_RELEASE_GATE = EVIDENCE_GUIDE_DATASET.with_name("release-gate.json")
EVIDENCE_GUIDE_AGENT_PROBE = EVIDENCE_GUIDE_DATASET.with_name("agents-integration-probe.json")
EVIDENCE_GUIDE_CAMPAIGN = REPOSITORY_ROOT / "dist" / "evidence-guide-candidate-evaluation"
EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE = EVIDENCE_GUIDE_CAMPAIGN / "release-gate.json"
EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION = EVIDENCE_GUIDE_CAMPAIGN / "weave-publication.json"
EVIDENCE_GUIDE_CANDIDATES = (
    "ibm-granite/granite-4.1-8b",
    "openai/gpt-oss-20b",
)
EVIDENCE_GUIDE_KEY = "evidence-guide/v1"
EVIDENCE_GUIDE_TABLE_KEY = "article/evidence_guide_evaluation"
EVIDENCE_GUIDE_V2_KEY = "evidence-guide/v2"
EVIDENCE_GUIDE_V2_TABLE_KEY = "article/evidence_guide_v2_arms"
EVIDENCE_GUIDE_V2_CANARY_TABLE_KEY = "article/evidence_guide_v2_trace_canary"
EVIDENCE_GUIDE_V2_PREFLIGHT_TABLE_KEY = "article/evidence_guide_v2_invalid_preflights"
EVIDENCE_GUIDE_V2_ARCH_DEV_KEY = "evidence-guide/v2-architecture-development"
EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_KEY = (
    "article/evidence_guide_v2_architecture_development"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_MEDIA_KEY = (
    "article/evidence_guide_v2_architecture_development_media"
)
EVIDENCE_GUIDE_V2_ROOT = REPOSITORY_ROOT / "dist" / "evidence-guide-v2-campaign-r2b"
EVIDENCE_GUIDE_V2_PLAN = EVIDENCE_GUIDE_V2_ROOT / "campaign-plan.json"
EVIDENCE_GUIDE_V2_PREREGISTRATION = EVIDENCE_GUIDE_V2_ROOT / "preregistration.json"
EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY = EVIDENCE_GUIDE_V2_ROOT / "article-summary.json"
EVIDENCE_GUIDE_V2_RELEASE_GATE = EVIDENCE_GUIDE_V2_ROOT / "release-gate.json"
EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION = (
    EVIDENCE_GUIDE_V2_ROOT / "terminal-disposition.json"
)
EVIDENCE_GUIDE_V2_CANARY_ROWS = EVIDENCE_GUIDE_V2_ROOT / "tables" / "trace_canary.json"
EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT = EVIDENCE_GUIDE_V2_ROOT / "wandb-publication.json"
EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT = (
    REPOSITORY_ROOT / "dist" / "evidence-guide-v2-protocol-amendment-r2.json"
)
EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT = (
    REPOSITORY_ROOT / "dist" / "evidence-guide-v2-r2-canary-incident.json"
)
EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT = (
    REPOSITORY_ROOT / "dist" / "evidence-guide-v2-preflight-audit.json"
)
EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT = (
    REPOSITORY_ROOT / "benchmarks" / "evidence_guide" / "v2" / "hidden-lockbox-receipt.json"
)
EVIDENCE_GUIDE_V2_ARCHITECTURES = ("D0", "D1", "A0", "A1", "A2", "A3", "A4")
EVIDENCE_GUIDE_V2_PRODUCTION_ELIGIBLE = frozenset({"A1", "A2", "A3"})
EVIDENCE_GUIDE_V2_CLOUD_ENTITY = "wandb"
EVIDENCE_GUIDE_V2_CLOUD_PROJECT = "gi-scenario-studio-agent-evals"
EVIDENCE_GUIDE_V2_ARCH_DEV_ROOT = (
    REPOSITORY_ROOT / "dist" / "evidence-guide-v2-architecture-dev-r1"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY = (
    EVIDENCE_GUIDE_V2_ARCH_DEV_ROOT / "article-summary.json"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE = (
    EVIDENCE_GUIDE_V2_ARCH_DEV_ROOT / "article-table.json"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA = (
    EVIDENCE_GUIDE_V2_ARCH_DEV_ROOT / "article-media.png"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT = (
    EVIDENCE_GUIDE_V2_ARCH_DEV_ROOT / "wandb-publication.json"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION = (
    "evidence-guide-v2-architecture-development-r1"
)
EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_COLUMNS = (
    "stage",
    "model_id",
    "architecture_id",
    "attempt_count",
    "generated_invalid_count",
    "released_invalid_count",
    "valid_generation_rate",
    "scenario_count",
    "scenario_ever_failed_count",
    "causal_success_rate",
    "trace_acknowledgement_rate",
    "logical_provider_calls",
    "transport_attempts",
    "input_tokens",
    "output_tokens",
    "cost_usd",
)
EVIDENCE_GUIDE_R4C_KEY = "evidence-guide/r4c-grounding"
EVIDENCE_GUIDE_R4C_TABLE_KEY = "article/evidence_guide_r4c_grounding"
EVIDENCE_GUIDE_R4C_MEDIA_KEY = "article/evidence_guide_r4c_grounding_media"
EVIDENCE_GUIDE_R4C_STUDY_VERSION = "gi-evidence-guide-grounding-r4c-final"
EVIDENCE_GUIDE_R4C_ENTITY = "wandb"
EVIDENCE_GUIDE_R4C_PROJECT = "gi-scenario-studio-agent-evals"
EVIDENCE_GUIDE_R4C_SOURCE_ROOT = (
    REPOSITORY_ROOT / "dist" / "evidence-guide-r4c-grounding-final-r1" / "live"
)
EVIDENCE_GUIDE_R4C_PACKAGE_ROOT = ROOT / "dist" / "evidence-guide-r4c"
EVIDENCE_GUIDE_R4C_SUMMARY = EVIDENCE_GUIDE_R4C_PACKAGE_ROOT / "article-summary.json"
EVIDENCE_GUIDE_R4C_TABLE = EVIDENCE_GUIDE_R4C_PACKAGE_ROOT / "article-table.json"
EVIDENCE_GUIDE_R4C_TABLE_CSV = EVIDENCE_GUIDE_R4C_PACKAGE_ROOT / "article-table.csv"
EVIDENCE_GUIDE_R4C_MEDIA = EVIDENCE_GUIDE_R4C_PACKAGE_ROOT / "article-media.png"
EVIDENCE_GUIDE_R4C_MANIFEST = EVIDENCE_GUIDE_R4C_PACKAGE_ROOT / "package-manifest.json"
EVIDENCE_GUIDE_R4C_R4B_AMENDMENT = (
    REPOSITORY_ROOT
    / "benchmarks"
    / "evidence_guide"
    / "r4"
    / "protocol-amendment-r4b.json"
)
EVIDENCE_GUIDE_R4C_AMENDMENT = EVIDENCE_GUIDE_R4C_R4B_AMENDMENT.with_name(
    "protocol-amendment-r4c.json"
)
EVIDENCE_GUIDE_R4C_R4B_SNAPSHOT = EVIDENCE_GUIDE_R4C_R4B_AMENDMENT.with_name(
    "r4b-incident-snapshot.json"
)
EVIDENCE_GUIDE_R4C_PUBLICATION_AMENDMENT = EVIDENCE_GUIDE_R4C_R4B_AMENDMENT.with_name(
    "publication-amendment-r4c.json"
)
EVIDENCE_GUIDE_R4C_SOURCE_FILES = (
    "publication/analysis.json",
    "publication/attempt-diagnostics.json",
    "publication/attempts.json",
    "publication/grounding-mechanisms.json",
    "publication/grounding-primary.json",
    "publication/prose-cap-mcnemar.json",
    "publication/prose-cap-primary.json",
    "execution-receipt.json",
    "preregistration.json",
    "publication-recovery/publication-recovery-cloud-receipt.json",
)
EVIDENCE_GUIDE_R4C_TABLE_COLUMNS = (
    "section",
    "model_id",
    "split",
    "condition",
    "output_token_cap",
    "metric",
    "success_count",
    "denominator",
    "rate",
    "paired_128_only",
    "paired_256_only",
    "paired_mcnemar_p",
    "attempt_count",
    "provider_response_count",
    "trace_acknowledged_count",
    "protocol_valid_count",
    "end_to_end_conformant_count",
    "failure_count",
    "median_latency_ms",
    "p95_latency_ms",
    "mean_latency_ms",
    "max_latency_ms",
    "provider_invocations",
    "transport_attempts",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "scientific_denominator",
    "note",
)
EVIDENCE_GUIDE_AGENT_TABLE_KEY = "article/evidence_guide_agent_behavior"
REPORT_TEMPLATE_VERSION = "gi-article-report-v9-human-narrative-1"
REALISTIC_CASE_STUDY = (
    REPOSITORY_ROOT / "benchmarks" / "realistic_fehraltorf" / "v1" / "case-study.json"
)
REALISTIC_CASE_STUDY_PUBLICATION = (
    REPOSITORY_ROOT / "dist" / "realistic-fehraltorf" / "wandb-publication.json"
)
REALISTIC_CASE_STUDY_KEY = "realistic-fehraltorf/v1"
REALISTIC_CASE_STUDY_TABLE_KEY = "article/realistic_fehraltorf_reanalysis"
REALISTIC_CASE_STUDY_CHART_KEY = "article/realistic_fehraltorf_model_sensitivity"
REALISTIC_CASE_STUDY_MEDIA_ROOT = ROOT / "dist" / "realistic-fehraltorf"
REALISTIC_CASE_STUDY_CHART = (
    REALISTIC_CASE_STUDY_MEDIA_ROOT / "fehraltorf-model-structure-sensitivity.png"
)
REALISTIC_CASE_STUDY_CHART_MANIFEST = (
    REALISTIC_CASE_STUDY_MEDIA_ROOT / "fehraltorf-model-structure-sensitivity.json"
)
REALISTIC_CASE_STUDY_CHART_CSV = (
    REALISTIC_CASE_STUDY_MEDIA_ROOT / "fehraltorf-model-structure-sensitivity.csv"
)
MARKER = re.compile(
    r'<!-- ARTICLE_(VIDEO|BENCHMARK|ANIMATION|EVALUATION|CASE_STUDY) key="([^"]+)" -->'
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
PROHIBITED_RUN_FILES = frozenset({"output.log", "requirements.txt", "wandb-metadata.json"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _evidence_guide_model_slug(model_id: str) -> str:
    if model_id not in EVIDENCE_GUIDE_CANDIDATES:
        raise ValueError("Evidence Guide campaign contains an unrecognized candidate")
    return model_id.replace("/", "--")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _validate_generated_media() -> dict[str, Any]:
    subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "scripts/extract_article_clips.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return _load_json(CLIP_MANIFEST)


def _animation_local_path(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("animation manifest paths must be relative to the film package")
    resolved = (ANIMATION_ROOT / candidate).resolve()
    if ANIMATION_ROOT.resolve() not in resolved.parents:
        raise ValueError("animation manifest path escaped the film package")
    return resolved


def _validate_animation_media(
    animation_spec: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if animation_spec.get("media_kind") != "evidence_grounded_canvas_films":
        raise ValueError("report animation media kind is not recognized")
    if int(animation_spec.get("frame_rate", 0)) != 30:
        raise ValueError("report animations must use the declared 30 fps contract")
    if not ANIMATION_MANIFEST.is_file():
        raise RuntimeError(
            "report animation manifest is missing; render and validate the five Canvas films first"
        )
    manifest = _load_json(ANIMATION_MANIFEST)
    declared = animation_spec.get("clips")
    rendered = manifest.get("clips")
    if not isinstance(declared, list) or not isinstance(rendered, list):
        raise TypeError("animation specification and manifest must contain clip lists")
    declared_keys = [item.get("key") for item in declared]
    rendered_keys = [item.get("key") for item in rendered]
    if rendered_keys != declared_keys:
        raise ValueError(
            "rendered animation order does not match report-animation-media.json: "
            f"{rendered_keys} != {declared_keys}"
        )
    if len(rendered) != 5:
        raise ValueError("the report animation package must contain exactly five films")

    required = (
        "key",
        "title",
        "output",
        "poster",
        "sha256",
        "poster_sha256",
        "duration_seconds",
        "frame_count",
        "caption",
        "claim_ids",
    )
    animations: list[dict[str, Any]] = []
    for expected, actual in zip(declared, rendered, strict=True):
        if any(name not in actual for name in required):
            raise ValueError(f"animation manifest entry is incomplete: {expected['key']}")
        for name in ("key", "title", "caption", "claim_ids"):
            if actual[name] != expected[name]:
                raise ValueError(f"animation {name} drifted: {expected['key']}")
        duration = float(actual["duration_seconds"])
        if duration != float(expected["target_duration_seconds"]):
            raise ValueError(f"animation duration drifted: {expected['key']}")
        if int(actual["frame_count"]) != round(duration * animation_spec["frame_rate"]):
            raise ValueError(f"animation frame count drifted: {expected['key']}")
        output = _animation_local_path(str(actual["output"]))
        poster = _animation_local_path(str(actual["poster"]))
        if not output.is_file() or _sha256(output) != actual["sha256"]:
            raise ValueError(f"animation video hash drifted: {expected['key']}")
        if not poster.is_file() or _sha256(poster) != actual["poster_sha256"]:
            raise ValueError(f"animation poster hash drifted: {expected['key']}")
        animations.append({**expected, **actual})
    return manifest, animations


def _article_parts() -> tuple[str, str, str]:
    article = ARTICLE.read_text(encoding="utf-8")
    lines = article.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("article must start with one H1 title")
    title = lines[0][2:].strip()
    description_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip()), None
    )
    if description_index is None:
        raise ValueError("article must include a deck beneath the title")
    deck = lines[description_index].strip()
    if not (deck.startswith("*") and deck.endswith("*")):
        raise ValueError("article deck must be italic markdown")
    body = "\n".join(lines[description_index + 1 :]).strip()
    if re.search(r"^#{1,6}\s+\d{2}:\d{2}", article, flags=re.MULTILINE):
        raise ValueError("article contains a recording-time heading")
    if str(ROOT) in article:
        raise ValueError("article contains an absolute local path")
    return title, deck.strip("*"), body


def _content_segments(
    body: str,
    clips: list[dict[str, Any]],
    charts: list[dict[str, Any]],
    animations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    cursor = 0
    observed_video_keys: list[str] = []
    observed_benchmark_keys: list[str] = []
    observed_animation_keys: list[str] = []
    observed_evaluation_keys: list[str] = []
    observed_case_study_keys: list[str] = []
    for match in MARKER.finditer(body):
        markdown = body[cursor : match.start()].strip()
        if markdown:
            segments.append({"kind": "markdown", "text": markdown})
        marker_kind, key = match.groups()
        kind = {
            "VIDEO": "video",
            "BENCHMARK": "benchmark",
            "ANIMATION": "animation",
            "EVALUATION": "evaluation",
            "CASE_STUDY": "case_study",
        }[marker_kind]
        segments.append({"kind": kind, "key": key})
        if kind == "video":
            observed_video_keys.append(key)
        elif kind == "benchmark":
            observed_benchmark_keys.append(key)
        elif kind == "animation":
            observed_animation_keys.append(key)
        elif kind == "evaluation":
            observed_evaluation_keys.append(key)
        else:
            observed_case_study_keys.append(key)
        cursor = match.end()
    trailing = body[cursor:].strip()
    if trailing:
        segments.append({"kind": "markdown", "text": trailing})
    expected_video_keys = [clip["key"] for clip in clips]
    expected_benchmark_keys = [chart["key"] for chart in charts]
    expected_animation_keys = [animation["key"] for animation in animations]
    if observed_video_keys != expected_video_keys:
        raise ValueError(
            "article video order does not match article-media.json: "
            f"{observed_video_keys} != {expected_video_keys}"
        )
    if observed_benchmark_keys != expected_benchmark_keys:
        raise ValueError(
            "article benchmark order does not match benchmark-media.json: "
            f"{observed_benchmark_keys} != {expected_benchmark_keys}"
        )
    if observed_animation_keys != expected_animation_keys:
        raise ValueError(
            "article animation order does not match report-animation-media.json: "
            f"{observed_animation_keys} != {expected_animation_keys}"
        )
    expected_evaluation_keys = [
        EVIDENCE_GUIDE_R4C_KEY,
        EVIDENCE_GUIDE_KEY,
        EVIDENCE_GUIDE_V2_KEY,
    ]
    if observed_evaluation_keys != expected_evaluation_keys:
        raise ValueError(
            "article must lead with the completed R4c grounding result, then preserve the "
            "versioned Evidence Guide v1 and v2 canary audit markers"
        )
    if observed_case_study_keys != [REALISTIC_CASE_STUDY_KEY]:
        raise ValueError("article must contain exactly one versioned Fehraltorf case-study marker")
    return segments


def _actual_publication_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must come from a publication receipt")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "wandb.ai"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{label} is not an authenticated W&B Cloud publication URL")
    return value


def _exact_wandb_destination_url(
    value: Any,
    *,
    label: str,
    entity: str,
    project: str,
) -> str:
    url = _actual_publication_url(value, label=label)
    if not urlparse(url).path.startswith(f"/{entity}/{project}/"):
        raise ValueError(f"{label} does not match the requested W&B destination")
    return url


def _actual_weave_conversation_url(
    value: Any,
    *,
    label: str,
    entity: str | None = None,
    project: str | None = None,
    conversation_id: str | None = None,
) -> str:
    url = _actual_publication_url(value, label=label)
    path = urlparse(url).path
    if "/weave/agents/conversations/" not in path:
        raise ValueError(f"{label} is not a Weave Agents conversation URL")
    if entity is not None and project is not None:
        expected = f"/{entity}/{project}/weave/agents/conversations/"
        if not path.startswith(expected):
            raise ValueError(f"{label} does not match the requested W&B destination")
    if conversation_id is not None and path.rstrip("/").rsplit("/", 1)[-1] != conversation_id:
        raise ValueError(f"{label} does not match its acknowledged conversation ID")
    return url


def _finite_number(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{label} is outside its valid range")
    return result


def _score_rate(summary: dict[str, Any], name: str) -> float:
    rates = summary.get("score_rates")
    if not isinstance(rates, dict):
        raise TypeError("Evidence Guide candidate summary lacks scorer rates")
    rate = _finite_number(rates.get(name), label=f"Evidence Guide {name} rate", minimum=0)
    if rate > 1:
        raise ValueError(f"Evidence Guide {name} rate exceeds one")
    return rate


def _load_agent_probe_receipt(
    *,
    entity: str,
    project: str,
    manifest: dict[str, Any],
    release_gate: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("agents_integration_probe_file") != EVIDENCE_GUIDE_AGENT_PROBE.name:
        raise RuntimeError("Evidence Guide Agents probe filename is not pinned by the manifest")
    expected_sha256 = manifest.get("agents_integration_probe_sha256")
    if expected_sha256 != _sha256(EVIDENCE_GUIDE_AGENT_PROBE):
        raise RuntimeError("Evidence Guide Agents probe failed manifest integrity validation")
    receipt = _load_json(EVIDENCE_GUIDE_AGENT_PROBE)
    required = {
        "schema_version": "1.0",
        "receipt_type": "evidence_guide_agents_manual_integration_probe",
        "environment": "temporary_synthetic_cedar_trace",
        "test_only": True,
    }
    if any(receipt.get(name) != value for name, value in required.items()):
        raise RuntimeError("Evidence Guide Agents probe receipt drifted from its audited context")
    production_gate = receipt.get("production_release_gate")
    if not isinstance(production_gate, dict) or production_gate != {
        "path": "benchmarks/evidence_guide/v1/release-gate.json",
        "sha256": manifest["release_gate_sha256"],
        "decision": release_gate["decision"],
        "overridden_in_memory": True,
        "unchanged_on_disk": True,
    }:
        raise RuntimeError("Evidence Guide Agents probe release-gate boundary drifted")
    test_gate = receipt.get("test_only_gate")
    required_test_gate = {
        "model_id": "ibm-granite/granite-4.1-8b",
        "prompt_version": "evidence-guide-prompt-v1",
        "response_schema_version": "evidence-guide-answer-schema-v1",
        "response_schema_sha256": (
            "7a565d1b904ae54e6ec81fe0db207504ede6c352de7aa5cabbc9d06611dae7e2"
        ),
        "tool_contract_version": "evidence-query-tools-v1",
        "validator_version": "deterministic-evidence-validator-v3",
        "purpose": "manual synthetic W&B Inference and Weave Agents integration probe only",
    }
    if not isinstance(test_gate, dict) or test_gate != required_test_gate:
        raise RuntimeError("Evidence Guide Agents probe test-only contract drifted")
    destination = receipt.get("destination")
    if not isinstance(destination, dict) or destination != {
        "entity": entity,
        "project": project,
        "access": "RESTRICTED",
        "deployment": "wandb_cloud",
        "binding_verified": True,
    }:
        raise RuntimeError("Evidence Guide Agents probe destination drifted")
    investigations = receipt.get("investigations")
    if not isinstance(investigations, list) or len(investigations) != 2:
        raise RuntimeError("Evidence Guide Agents probe must contain both audited investigations")
    conversations = []
    for investigation in investigations:
        if not isinstance(investigation, dict):
            raise TypeError("Evidence Guide Agents probe investigation must be an object")
        conversation_id = investigation.get("conversation_id")
        turns = investigation.get("turns")
        expected_turn_count = 2 if investigation.get("anchor") == "ALT-017" else 1
        if (
            not isinstance(conversation_id, str)
            or not isinstance(turns, list)
            or len(turns) != expected_turn_count
        ):
            raise RuntimeError("Evidence Guide Agents probe acknowledgement is incomplete")
        audited_turns = []
        for expected_sequence, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise TypeError("Evidence Guide Agents probe turn must be an object")
            required_turn = {
                "sequence": expected_sequence,
                "local_state": "failed",
                "validation_outcome": "rejected",
                "weave_acknowledged": True,
                "answer_released": False,
                "root_status": "ERROR",
                "tool_activity_observed": True,
            }
            if any(turn.get(name) != value for name, value in required_turn.items()):
                raise RuntimeError(
                    "Evidence Guide Agents probe was not a fail-closed acknowledged turn"
                )
            expected_turn = {
                ("ALT-017", 0): (
                    "tradeoff_vs_acceptance",
                    "EVIDENCE_NEXT_CHECK_SELECTION_INVALID",
                ),
                ("ALT-017", 1): (
                    "controlling_storm",
                    "EVIDENCE_GENERATED_NUMBER_BLOCKED",
                ),
                ("ALT-207", 0): (
                    "ecological_boundary",
                    "EVIDENCE_SUPPORT_CLAIM_INVALID",
                ),
            }.get((investigation.get("anchor"), expected_sequence))
            if (
                expected_turn is None
                or (turn.get("question_key"), turn.get("error_code")) != expected_turn
            ):
                raise RuntimeError("Evidence Guide Agents probe question or rejection drifted")
            if (
                turn.get("tool_parentage_verified") is not None
                or turn.get("tool_parentage_reason") != "routing_parent_span_not_exposed_by_sdk"
                or turn.get("tool_span_count") != 1
                or turn.get("routing_parent_count") != 1
            ):
                raise RuntimeError("Evidence Guide Agents probe parentage boundary drifted")
            for identifier in ("trace_id", "root_span_id"):
                value = turn.get(identifier)
                if not isinstance(value, str) or not value:
                    raise RuntimeError(
                        f"Evidence Guide Agents probe {identifier} acknowledgement is missing"
                    )
            identity = turn.get("agent_identity")
            if identity != {
                "agent_name": "gi-evidence-guide",
                "agent_version": "evidence-guide-v1",
                "root_span_name": "gi-evidence-guide",
            }:
                raise RuntimeError("Evidence Guide Agents probe identity drifted")
            message_types = turn.get("message_types")
            if not isinstance(message_types, list) or not {
                "agent_start",
                "assistant_message",
                "tool_call",
                "user_message",
            }.issubset(message_types):
                raise RuntimeError("Evidence Guide Agents probe message evidence is incomplete")
            audited_turns.append(
                {
                    "sequence": turn["sequence"],
                    "question_key": turn.get("question_key"),
                    "state": turn["local_state"],
                    "validation_outcome": turn["validation_outcome"],
                    "error_code": turn["error_code"],
                    "answer_released": turn["answer_released"],
                    "weave_readback_verified": True,
                    "trace_id": turn["trace_id"],
                    "root_span_id": turn["root_span_id"],
                    "root_status": turn["root_status"],
                    "message_types": message_types,
                    "tool_activity_observed": turn["tool_activity_observed"],
                    "tool_parentage_verified": turn["tool_parentage_verified"],
                    "tool_parentage_reason": turn["tool_parentage_reason"],
                    "tool_span_count": turn["tool_span_count"],
                    "routing_parent_count": turn["routing_parent_count"],
                }
            )
        url = _actual_weave_conversation_url(
            turns[0].get("weave_url"),
            label="Evidence Guide Agents probe URL",
            entity=entity,
            project=project,
            conversation_id=conversation_id,
        )
        if any(turn.get("weave_url") != url for turn in turns):
            raise RuntimeError("Evidence Guide Agents probe conversation URL changed between turns")
        conversations.append(
            {
                "anchor": investigation.get("anchor"),
                "capability_state": investigation.get("capability_state"),
                "conversation_id": conversation_id,
                "turns": audited_turns,
                "url": url,
            }
        )
    if {item["anchor"] for item in conversations} != {"ALT-017", "ALT-207"}:
        raise RuntimeError("Evidence Guide Agents probe anchors drifted")
    if len({item["conversation_id"] for item in conversations}) != len(conversations):
        raise RuntimeError("Evidence Guide Agents probe repeats a conversation")
    verification = receipt.get("verification")
    required_verification = {
        "conversation_count": 2,
        "turn_count": 3,
        "alt_017_turns_share_conversation": True,
        "alt_207_uses_clean_conversation": True,
        "all_turns_cloud_acknowledged": True,
        "all_turns_read_back_from_weave": True,
        "all_turns_observed_tool_activity": True,
        "all_turns_tool_parentage_verified": None,
        "rejected_turn_count": 3,
        "rejected_answer_release_count": 0,
        "provider_output_stored_in_receipt": False,
    }
    if not isinstance(verification, dict) or verification != required_verification:
        raise RuntimeError("Evidence Guide Agents probe verification summary drifted")
    return {
        "receipt_sha256": expected_sha256,
        "model_id": test_gate["model_id"],
        "project_access": destination["access"],
        "production_release_gate_overridden": True,
        "production_release_gate_decision": production_gate["decision"],
        "conversation_count": len(conversations),
        "turn_count": verification["turn_count"],
        "rejected_count": verification["rejected_turn_count"],
        "conversations": conversations,
    }


def _candidate_campaign_record(
    *,
    model_id: str,
    dataset: dict[str, Any],
    dataset_sha256: str,
    scorer_version: str,
    case_ids: set[str],
    release_gate: dict[str, Any],
) -> dict[str, Any]:
    slug = _evidence_guide_model_slug(model_id)
    root = EVIDENCE_GUIDE_CAMPAIGN / slug
    outputs_path = root / "outputs.json"
    evaluation_path = root / "evaluation.json"
    receipt_path = root / "receipt.json"
    for path in (outputs_path, evaluation_path, receipt_path):
        if not path.is_file():
            raise RuntimeError(
                f"Evidence Guide candidate campaign is missing {path.name}: {model_id}"
            )

    outputs = _load_json(outputs_path)
    evaluation = _load_json(evaluation_path)
    receipt = _load_json(receipt_path)
    required_receipt = {
        "model_id": model_id,
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_sha256,
        "scorer_version": scorer_version,
        "reference_projection": False,
    }
    if any(receipt.get(name) != value for name, value in required_receipt.items()):
        raise RuntimeError(f"Evidence Guide candidate receipt drifted for {model_id}")
    source = evaluation.get("source")
    if not isinstance(source, dict) or source != {
        "kind": "wandb_inference_candidate_outputs",
        "candidate_id": model_id,
        "reference_projection": False,
    }:
        raise RuntimeError(
            f"Evidence Guide evaluation is not a real provider candidate: {model_id}"
        )
    required_evaluation = {
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_sha256,
        "scorer_version": scorer_version,
    }
    if any(evaluation.get(name) != value for name, value in required_evaluation.items()):
        raise RuntimeError(f"Evidence Guide candidate evaluation drifted for {model_id}")
    if receipt.get("outputs_sha256") != _json_sha256(outputs):
        raise RuntimeError(
            f"Evidence Guide candidate outputs failed integrity validation: {model_id}"
        )
    if receipt.get("evaluation_sha256") != _json_sha256(evaluation):
        raise RuntimeError(
            f"Evidence Guide candidate evaluation failed integrity validation: {model_id}"
        )
    expected_receipt_hash = release_gate.get("candidate_receipt_sha256", {}).get(model_id)
    if expected_receipt_hash != _json_sha256(receipt):
        raise RuntimeError(
            f"Evidence Guide release gate does not bind the candidate receipt: {model_id}"
        )

    summary = evaluation.get("summary")
    results = evaluation.get("case_results")
    if not isinstance(summary, dict) or receipt.get("deterministic_summary") != summary:
        raise RuntimeError(f"Evidence Guide candidate summary drifted for {model_id}")
    if not isinstance(results, list) or len(results) != len(case_ids):
        raise RuntimeError(f"Evidence Guide candidate results are incomplete: {model_id}")
    observed_case_ids = {
        item.get("case_id") for item in results if isinstance(item, dict) and item.get("case_id")
    }
    if observed_case_ids != case_ids:
        raise RuntimeError(f"Evidence Guide candidate case identities drifted for {model_id}")
    total = int(summary.get("total", -1))
    evaluated = int(summary.get("evaluated", -1))
    missing = int(summary.get("missing", -1))
    passed = int(summary.get("passed", -1))
    failed = int(summary.get("failed", -1))
    if (
        total != len(case_ids)
        or evaluated + missing != total
        or passed + failed != evaluated
        or len(outputs) != evaluated
    ):
        raise RuntimeError(f"Evidence Guide candidate counts drifted for {model_id}")

    failures = receipt.get("provider_failures")
    usage = receipt.get("usage")
    latency = receipt.get("latency_ms")
    if not isinstance(failures, list) or len(failures) != missing:
        raise RuntimeError(f"Evidence Guide provider failures drifted for {model_id}")
    if not isinstance(usage, dict) or any(
        isinstance(usage.get(name), bool) or not isinstance(usage.get(name), int) or usage[name] < 0
        for name in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise RuntimeError(f"Evidence Guide token usage is invalid for {model_id}")
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise RuntimeError(f"Evidence Guide token totals drifted for {model_id}")
    if not isinstance(latency, dict) or int(latency.get("count", -1)) != total:
        raise RuntimeError(f"Evidence Guide latency coverage drifted for {model_id}")
    for name in ("minimum", "median", "p95", "maximum", "total"):
        _finite_number(latency.get(name), label=f"Evidence Guide {name} latency", minimum=0)

    behavior = behavior_summary(evaluation)
    abstention = behavior["abstention"]
    unauthorized_action_violations = sum(
        error.get("code") == "action_unauthorized"
        for item in results
        for error in item.get("errors", [])
        if isinstance(error, dict)
    )
    missing_required_actions = sum(
        error.get("code") == "action_missing"
        for item in results
        for error in item.get("errors", [])
        if isinstance(error, dict)
    )
    usefulness = receipt.get("practitioner_usefulness")
    required_usefulness = {
        "status",
        "rating_count",
        "unique_reviewers",
        "case_coverage",
        "positive_rate",
        "passed",
    }
    if not isinstance(usefulness, dict) or not required_usefulness.issubset(usefulness):
        raise RuntimeError(f"Evidence Guide usefulness status is incomplete for {model_id}")
    if usefulness["status"] not in {"not_measured", "measured"}:
        raise RuntimeError(f"Evidence Guide usefulness status is invalid for {model_id}")
    if any(
        isinstance(usefulness[name], bool)
        or not isinstance(usefulness[name], int)
        or usefulness[name] < 0
        for name in ("rating_count", "unique_reviewers", "case_coverage")
    ) or not isinstance(usefulness["passed"], bool):
        raise RuntimeError(f"Evidence Guide usefulness counts are invalid for {model_id}")
    positive_rate = usefulness["positive_rate"]
    if positive_rate is not None and (
        isinstance(positive_rate, bool)
        or not isinstance(positive_rate, (int, float))
        or not 0 <= positive_rate <= 1
    ):
        raise RuntimeError(f"Evidence Guide usefulness rate is invalid for {model_id}")
    if usefulness["status"] == "not_measured":
        expected_unmeasured = {
            "status": "not_measured",
            "rating_count": 0,
            "unique_reviewers": 0,
            "case_coverage": 0,
            "positive_rate": None,
            "passed": False,
        }
        if any(usefulness.get(name) != value for name, value in expected_unmeasured.items()):
            raise RuntimeError(f"Evidence Guide unmeasured usefulness drifted for {model_id}")
    controlled_approval = receipt.get("practitioner_approval")
    controlled_approval_status = (
        str(controlled_approval.get("status"))
        if isinstance(controlled_approval, dict)
        else "not_supplied"
    )
    metrics = {
        "numeric_unit_fidelity": _score_rate(summary, "numeric_faithfulness"),
        "evidence_coverage": _score_rate(summary, "evidence_coverage"),
        "evidence_id_validity": behavior["evidence_id_validity_on_produced_outputs"]["rate"],
        "limitation_coverage": _score_rate(summary, "limitation_coverage"),
        "read_only_action_compliance": _score_rate(summary, "read_only_authority"),
        "claim_boundary": _score_rate(summary, "claim_boundary"),
        "unauthorized_action_violations": unauthorized_action_violations,
        "fabricated_evidence_id_violations": behavior["fabricated_evidence_id_violations"],
        "missing_required_actions": missing_required_actions,
        "abstention_passed": abstention["refusal_behavior_passed"],
        "complete_abstention_passed": abstention["complete_case_passed"],
        "abstention_total": abstention["total"],
        "fabricated_run_complete_pass": behavior["protected_cases"]["fabricated_run"][
            "complete_case_passed"
        ],
        "prompt_injection_complete_pass": behavior["protected_cases"]["prompt_injection"][
            "complete_case_passed"
        ],
        "injection_and_fabricated_run_complete_pass": behavior[
            "injection_and_fabricated_run_complete_pass"
        ],
    }
    artifact_sha256 = {
        "outputs": _sha256(outputs_path),
        "evaluation": _sha256(evaluation_path),
        "candidate_receipt": _sha256(receipt_path),
    }
    return {
        "model_id": model_id,
        "slug": slug,
        "provider": receipt.get("provider"),
        "provider_call_count": int(receipt.get("provider_call_count", -1)),
        "provider_failure_count": len(failures),
        "summary": summary,
        "selection_summary": receipt.get("selection_summary"),
        "metrics": metrics,
        "behavior_summary": behavior,
        "experiment_scope": {
            "kind": behavior["experiment_kind"],
            "logical_case_invocations_per_case": 1,
            "evidence_tool_calls_per_case": 0,
            "runtime_agent_loop_evaluated": behavior["runtime_agent_loop_evaluated"],
            "evidence_ids_and_actions_schema_constrained": behavior[
                "evidence_ids_and_actions_schema_constrained"
            ],
            "provider_generates_numeric_values": behavior["provider_generates_numeric_values"],
            "canonical_numeric_values_materialized_locally": behavior[
                "canonical_numeric_values_materialized_locally"
            ],
            "token_accounting_scope": "successful_provider_responses_only",
            "latency_accounting_scope": "all_provider_attempts",
        },
        "usage": usage,
        "latency_ms": latency,
        "practitioner_usefulness": usefulness,
        "controlled_approval_status": controlled_approval_status,
        "release_gate": receipt.get("release_gate"),
        "artifact_sha256": artifact_sha256,
        "case_results": results,
        "publication": None,
    }


def _weave_object_ref(value: Any, *, entity: str, project: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"weave:///{entity}/{project}/object/"):
        raise ValueError(f"{label} is not an exact-destination Weave object reference")
    return value


def _load_campaign_publication_receipt(
    *,
    entity: str,
    project: str,
    dataset: dict[str, Any],
    dataset_sha256: str,
    release_gate: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION.is_file():
        return None
    receipt = _load_json(EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION)
    required = {
        "schema_version": "1.0",
        "publication_type": "candidate_campaign",
        "entity": entity,
        "project": project,
        "project_access": "RESTRICTED",
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_sha256,
        "campaign_release_gate_sha256": _sha256(EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE),
        "decision": release_gate["decision"],
        "selected_model": release_gate["selected_model"],
        "failed_candidate_count": len(candidates),
        "failed_evaluations_published": True,
        "claim_boundary": release_gate["claim_boundary"],
    }
    if any(receipt.get(name) != value for name, value in required.items()):
        raise RuntimeError("Evidence Guide campaign publication receipt drifted")
    dataset_ref = _weave_object_ref(
        receipt.get("dataset_ref"),
        entity=entity,
        project=project,
        label="Evidence Guide published dataset",
    )
    campaign_evidence_ref = _weave_object_ref(
        receipt.get("campaign_evidence_ref"),
        entity=entity,
        project=project,
        label="Evidence Guide published campaign",
    )
    published = receipt.get("candidate_evaluations")
    if not isinstance(published, list) or len(published) != len(candidates):
        raise RuntimeError("Evidence Guide campaign publication candidate set is incomplete")
    if [item.get("candidate_id") for item in published if isinstance(item, dict)] != [
        candidate["model_id"] for candidate in candidates
    ]:
        raise RuntimeError("Evidence Guide campaign publication candidate order drifted")

    publications: dict[str, dict[str, Any]] = {}
    for candidate, item in zip(candidates, published, strict=True):
        if not isinstance(item, dict):
            raise TypeError("Evidence Guide campaign publication candidate must be an object")
        model_id = candidate["model_id"]
        expected = {
            "candidate_id": model_id,
            "candidate_receipt_sha256": release_gate["candidate_receipt_sha256"][model_id],
            "disposition": "failed",
            "evaluation_manifest_sha256": candidate["artifact_sha256"]["evaluation"],
            "provider_failure_count": candidate["provider_failure_count"],
            "summary": candidate["summary"],
        }
        if any(item.get(name) != value for name, value in expected.items()):
            raise RuntimeError(
                f"Evidence Guide campaign publication evidence drifted for {model_id}"
            )
        trace_urls = item.get("trace_urls", [])
        if not isinstance(trace_urls, list):
            raise TypeError("Evidence Guide campaign trace URLs must be a list")
        verified_traces = [
            _exact_wandb_destination_url(
                url,
                label=f"Evidence Guide trace URL for {model_id}",
                entity=entity,
                project=project,
            )
            for url in trace_urls
        ]
        if len(verified_traces) != len(set(verified_traces)):
            raise ValueError(f"Evidence Guide campaign receipt repeats a trace for {model_id}")
        publications[model_id] = {
            "dataset_ref": dataset_ref,
            "campaign_evidence_ref": campaign_evidence_ref,
            "evaluation_url": _exact_wandb_destination_url(
                item.get("evaluation_url"),
                label=f"Evidence Guide evaluation URL for {model_id}",
                entity=entity,
                project=project,
            ),
            "trace_urls": verified_traces,
        }
    return {
        "receipt_sha256": _sha256(EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION),
        "dataset_ref": dataset_ref,
        "campaign_evidence_ref": campaign_evidence_ref,
        "published_at": receipt.get("published_at"),
        "candidate_publications": publications,
    }


def _load_evidence_guide_evaluation(entity: str, project: str) -> dict[str, Any]:
    dataset = _load_json(EVIDENCE_GUIDE_DATASET)
    manifest = _load_json(EVIDENCE_GUIDE_DATASET_MANIFEST)
    dataset_digest = _sha256(EVIDENCE_GUIDE_DATASET)
    required_dataset = {
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": dataset_digest,
        "row_count": dataset.get("row_count"),
        "license": "CC0-1.0",
    }
    if any(manifest.get(name) != value for name, value in required_dataset.items()):
        raise RuntimeError("Evidence Guide dataset manifest drifted")
    rows = dataset.get("rows")
    if not isinstance(rows, list) or len(rows) < 40 or len(rows) != dataset.get("row_count"):
        raise RuntimeError("Evidence Guide evaluation dataset is incomplete")
    case_ids = [row.get("case_id") for row in rows if isinstance(row, dict)]
    if len(case_ids) != len(set(case_ids)) or any(not case_id for case_id in case_ids):
        raise RuntimeError("Evidence Guide evaluation case identities are invalid")
    if (
        not EVIDENCE_GUIDE_RELEASE_GATE.is_file()
        or not EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE.is_file()
    ):
        raise RuntimeError("Evidence Guide real-candidate release gate is missing")
    if _sha256(EVIDENCE_GUIDE_RELEASE_GATE) != _sha256(EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE):
        raise RuntimeError("Evidence Guide dist release gate drifted from checked-in authority")
    release_gate = _load_json(EVIDENCE_GUIDE_RELEASE_GATE)
    required_gate = {
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_digest,
        "candidate_order": list(EVIDENCE_GUIDE_CANDIDATES),
        "evaluated_models": list(EVIDENCE_GUIDE_CANDIDATES),
        "decision": "DISABLE_LIVE_GUIDE",
        "enabled": False,
        "selected_model": None,
    }
    if any(release_gate.get(name) != value for name, value in required_gate.items()):
        raise RuntimeError("Evidence Guide release gate drifted from the reported campaign")

    candidates = [
        _candidate_campaign_record(
            model_id=model_id,
            dataset=dataset,
            dataset_sha256=dataset_digest,
            scorer_version=manifest["scorer_version"],
            case_ids=set(case_ids),
            release_gate=release_gate,
        )
        for model_id in EVIDENCE_GUIDE_CANDIDATES
    ]
    if any(candidate["provider_call_count"] != len(rows) for candidate in candidates):
        raise RuntimeError("Evidence Guide campaign did not attempt every case for both candidates")
    campaign_publication = _load_campaign_publication_receipt(
        entity=entity,
        project=project,
        dataset=dataset,
        dataset_sha256=dataset_digest,
        release_gate=release_gate,
        candidates=candidates,
    )
    if campaign_publication is not None:
        for candidate in candidates:
            candidate["publication"] = campaign_publication["candidate_publications"][
                candidate["model_id"]
            ]
    agent_probe = _load_agent_probe_receipt(
        entity=entity,
        project=project,
        manifest=manifest,
        release_gate=release_gate,
    )
    publication_count = int(campaign_publication is not None)
    publication_state = (
        "campaign_published_verified"
        if campaign_publication is not None
        else "campaign_evaluated_locally"
    )
    artifact_inventory = {
        "release_gate": _sha256(EVIDENCE_GUIDE_RELEASE_GATE),
        "campaign_release_gate": _sha256(EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE),
        "agent_probe_receipt": agent_probe["receipt_sha256"],
        "candidates": {
            candidate["model_id"]: candidate["artifact_sha256"] for candidate in candidates
        },
    }
    if campaign_publication is not None:
        artifact_inventory["campaign_publication_receipt"] = campaign_publication["receipt_sha256"]
    return {
        "key": EVIDENCE_GUIDE_KEY,
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_digest,
        "dataset_manifest_sha256": _sha256(EVIDENCE_GUIDE_DATASET_MANIFEST),
        "row_count": len(rows),
        "license": dataset["license"],
        "scorer_version": manifest["scorer_version"],
        "publication_state": publication_state,
        "publication_receipt_count": publication_count,
        "agent_probe_receipt_count": 1,
        "release_gate_sha256": _sha256(EVIDENCE_GUIDE_RELEASE_GATE),
        "campaign_release_gate_sha256": _sha256(EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE),
        "campaign_manifest_sha256": _json_sha256(artifact_inventory),
        "decision": release_gate["decision"],
        "enabled": release_gate["enabled"],
        "selected_model": release_gate["selected_model"],
        "release_gate_version": release_gate["release_gate_version"],
        "release_gate_reasons": release_gate["reasons"],
        "claim_boundary": release_gate["claim_boundary"],
        "candidate_order": list(EVIDENCE_GUIDE_CANDIDATES),
        "candidates": candidates,
        "experiment_scope": {
            "candidate_campaign": {
                "kind": "single_call_structured_evidence_selection",
                "cases_per_model": len(rows),
                "model_count": len(candidates),
                "logical_case_invocations": sum(
                    candidate["provider_call_count"] for candidate in candidates
                ),
                "evidence_tool_calls": 0,
                "runtime_agent_loop_evaluated": False,
                "evidence_ids_and_actions_schema_constrained": True,
                "canonical_numeric_values_materialized_locally": True,
            },
            "agents_probe": {
                "kind": "two_call_tool_using_runtime_integration_probe",
                "conversation_count": agent_probe["conversation_count"],
                "turn_count": agent_probe["turn_count"],
                "rejected_turn_count": agent_probe["rejected_count"],
                "released_answer_count": 0,
                "answer_quality_scored": False,
                "token_usage_status": "not_recorded_in_probe_receipt",
                "latency_status": "not_recorded_in_probe_receipt",
                "production_release_gate_overridden": True,
            },
            "successful_end_to_end_runtime_agent_answers_evaluated": 0,
            "supports_live_guide_release": False,
        },
        "campaign_publication": campaign_publication,
        "agent_probe": agent_probe,
    }


def _load_evidence_guide_v2_evaluation() -> dict[str, Any]:
    required_files = (
        EVIDENCE_GUIDE_V2_PLAN,
        EVIDENCE_GUIDE_V2_PREREGISTRATION,
        EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT,
        EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT,
        EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT,
        EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION,
        EVIDENCE_GUIDE_V2_CANARY_ROWS,
    )
    if any(not path.is_file() for path in required_files):
        raise RuntimeError("Evidence Guide v2 preregistration package is incomplete")
    plan = _load_json(EVIDENCE_GUIDE_V2_PLAN)
    preregistration = _load_json(EVIDENCE_GUIDE_V2_PREREGISTRATION)
    hidden_receipt = _load_json(EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT)
    if plan.get("schema_version") != "1.0" or plan.get("network_calls") not in {
        0,
        "stage_dependent",
    }:
        raise RuntimeError("Evidence Guide v2 campaign plan is not a validated campaign plan")
    active_execution_plan = plan["network_calls"] == "stage_dependent"
    if plan.get("campaign_version") != "evidence-guide-v2-architecture-first":
        raise RuntimeError("Evidence Guide v2 campaign identity drifted")
    if plan.get("preregistration_sha256") != preregistration.get("preregistration_sha256"):
        raise RuntimeError("Evidence Guide v2 preregistration identity drifted")
    if preregistration.get("schema_version") != "1.0":
        raise RuntimeError("Evidence Guide v2 preregistration schema drifted")
    preregistration_body = {
        key: value
        for key, value in preregistration.items()
        if key != "preregistration_sha256"
    }
    if preregistration.get("preregistration_sha256") != _json_sha256(
        preregistration_body
    ):
        raise RuntimeError("Evidence Guide v2 preregistration self-hash drifted")
    publication_policy = preregistration.get("publication")
    if not isinstance(publication_policy, dict) or (
        publication_policy.get("required_access") != "Restricted"
        or publication_policy.get("practitioner_usefulness_status") != "not_measured"
        or publication_policy.get("hidden_attempt_rows_published") is not False
    ):
        raise RuntimeError("Evidence Guide v2 publication policy drifted")
    release_policy = preregistration.get("release_gates")
    if not isinstance(release_policy, dict) or (
        release_policy.get("automated_explanation_quality") is not True
        or release_policy.get("practitioner_usefulness_status") != "not_measured"
    ):
        raise RuntimeError("Evidence Guide v2 release boundary drifted")
    if tuple(preregistration.get("architectures") or ()) != EVIDENCE_GUIDE_V2_ARCHITECTURES:
        raise RuntimeError("Evidence Guide v2 preregistered arms drifted")
    if set(preregistration.get("production_eligible_architectures") or ()) != set(
        EVIDENCE_GUIDE_V2_PRODUCTION_ELIGIBLE
    ):
        raise RuntimeError("Evidence Guide v2 production-eligible arms drifted")
    if hidden_receipt.get("schema_version") != "2.0" or (
        hidden_receipt.get("local_rows_checked_into_git") is not False
        or hidden_receipt.get("public_builder_can_regenerate") is not False
    ):
        raise RuntimeError("Evidence Guide v2 hidden-lockbox receipt is unsafe")
    hidden_available = bool(
        hidden_receipt.get("sealed") is True
        and hidden_receipt.get("protected_generation") is True
        and hidden_receipt.get("release_eligible") is True
    )
    if hidden_receipt.get("expected_row_count") != 300:
        raise RuntimeError("Evidence Guide v2 hidden-lockbox scenario count drifted")

    protocol_amendment = _load_json(EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT)
    amendment_body = {
        key: value
        for key, value in protocol_amendment.items()
        if key != "receipt_sha256"
    }
    if (
        protocol_amendment.get("receipt_kind")
        != "evidence_guide_v2_protocol_amendment"
        or protocol_amendment.get("receipt_sha256") != _json_sha256(amendment_body)
        or plan.get("protocol_amendment_sha256")
        != protocol_amendment.get("receipt_sha256")
        or preregistration.get("protocol_amendment", {}).get("receipt_sha256")
        != protocol_amendment.get("receipt_sha256")
    ):
        raise RuntimeError("Evidence Guide v2 protocol-amendment lineage drifted")

    operational_incident = _load_json(EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT)
    incident_body = {
        key: value
        for key, value in operational_incident.items()
        if key != "receipt_sha256"
    }
    incident_usage = operational_incident.get("provider_usage")
    if (
        operational_incident.get("receipt_kind")
        != "evidence_guide_v2_trace_canary_incident"
        or operational_incident.get("receipt_sha256") != _json_sha256(incident_body)
        or operational_incident.get("disposition")
        != "EXCLUDED_NON_SCIENTIFIC_PREFLIGHT"
        or operational_incident.get("scientific_denominator_attempts") != 0
        or operational_incident.get("architecture_denominator_attempts") != 0
        or not isinstance(incident_usage, dict)
        or incident_usage
        != {
            "cost_usd": "0",
            "input_tokens": 0,
            "output_tokens": 0,
            "provider_invocations": 0,
            "transport_attempts": 0,
        }
        or not isinstance(operational_incident.get("checks"), dict)
        or not all(value is True for value in operational_incident["checks"].values())
        or plan.get("prior_operational_incident_sha256")
        != operational_incident.get("receipt_sha256")
    ):
        raise RuntimeError("Evidence Guide v2 operational-incident lineage drifted")

    terminal = _load_json(EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION)
    terminal_body = {
        key: value for key, value in terminal.items() if key != "receipt_sha256"
    }
    if (
        terminal.get("receipt_kind")
        != "evidence_guide_v2_terminal_disposition"
        or terminal.get("receipt_sha256") != _json_sha256(terminal_body)
        or terminal.get("decision") != "DISABLE_LIVE_GUIDE"
        or terminal.get("stopping_reason")
        != "TRACE_CANARY_SEMANTIC_CONTRACT_FAILED"
        or terminal.get("blocked_before_stage") != "architecture"
        or terminal.get("scientific_denominator_attempts") != 0
        or terminal.get("architecture_denominator_attempts") != 0
        or terminal.get("provider_answer_released") is not False
        or terminal.get("practitioner_usefulness_status") != "not_measured"
        or terminal.get("preregistration_sha256")
        != preregistration.get("preregistration_sha256")
        or not isinstance(terminal.get("conditions"), dict)
        or not all(value is True for value in terminal["conditions"].values())
    ):
        raise RuntimeError("Evidence Guide v2 terminal disposition drifted")
    a1_terminal = terminal.get("a1_observation")
    d1_terminal = terminal.get("d1_control")
    if (
        terminal.get("failed_canary_checks")
        != ["a1_model_and_tool_spans_acknowledged"]
        or not isinstance(a1_terminal, dict)
        or a1_terminal.get("model_id") != "ibm-granite/granite-4.1-8b"
        or a1_terminal.get("provider_invocations") != 1
        or a1_terminal.get("transport_attempts") != 1
        or a1_terminal.get("trace_acknowledged") is not True
        or not isinstance(a1_terminal.get("expected_plan_id"), str)
        or a1_terminal.get("selected_plan_id") == a1_terminal.get("expected_plan_id")
        or not isinstance(d1_terminal, dict)
        or d1_terminal
        != {
            "answer_released": True,
            "repository_fidelity": True,
            "safe": True,
            "selected_plan_id": a1_terminal.get("expected_plan_id"),
        }
    ):
        raise RuntimeError("Evidence Guide v2 terminal canary evidence drifted")

    # The campaign plan originally exposed this envelope as ``maximal_totals``.
    # The finalized preregistration names the same, release-admissible branch
    # explicitly so that the larger, inadmissible A2/A3 envelopes cannot be
    # mistaken for an authorized spend ceiling.
    budget = plan.get("preregistered_a1_branch_totals") or plan.get("maximal_totals")
    required_budget = {
        "logical_provider_invocations",
        "worst_case_transport_attempts",
        "worst_case_input_tokens",
        "worst_case_output_tokens",
        "worst_case_cost_usd",
    }
    if not isinstance(budget, dict) or not required_budget.issubset(budget):
        raise RuntimeError("Evidence Guide v2 campaign budget is incomplete")
    if any(int(budget[name]) < 0 for name in required_budget - {"worst_case_cost_usd"}):
        raise RuntimeError("Evidence Guide v2 campaign budget is invalid")
    try:
        budget_cost = float(budget["worst_case_cost_usd"])
    except (TypeError, ValueError) as error:
        raise RuntimeError("Evidence Guide v2 cost ceiling is invalid") from error
    if not math.isfinite(budget_cost) or budget_cost < 0:
        raise RuntimeError("Evidence Guide v2 cost ceiling is invalid")
    confirmatory_envelopes = plan.get("confirmatory_architecture_envelopes")
    if not isinstance(confirmatory_envelopes, dict) or set(confirmatory_envelopes) != {
        "A1",
        "A2",
        "A3",
    }:
        raise RuntimeError("Evidence Guide v2 confirmatory envelopes are incomplete")
    envelope_fields = required_budget | {"admissible"}
    if any(
        not isinstance(confirmatory_envelopes[architecture], dict)
        or not envelope_fields.issubset(confirmatory_envelopes[architecture])
        for architecture in ("A1", "A2", "A3")
    ):
        raise RuntimeError("Evidence Guide v2 confirmatory envelope fields are incomplete")
    if confirmatory_envelopes["A1"].get("admissible") is not True or any(
        confirmatory_envelopes[architecture].get("admissible") is not False
        for architecture in ("A2", "A3")
    ):
        raise RuntimeError("Evidence Guide v2 confirmatory admissibility drifted")
    for name in required_budget:
        if str(confirmatory_envelopes["A1"][name]) != str(budget[name]):
            raise RuntimeError("Evidence Guide v2 A1 branch budget drifted")

    arm_descriptions = {
        "D0": "Deterministic catalog-only baseline",
        "D1": "Deterministic evidence-tool baseline",
        "A0": "Model-routed tools and free prose diagnostic",
        "A1": "Deterministic tools and model-selected finite explanation plan",
        "A2": "Model-classified intent, deterministic tools, and finite plan",
        "A3": "Model-routed tools and finite explanation plan",
        "A4": "Model-routed tools and free prose stress test",
    }
    initial_visible_status = "interim_or_stopped" if active_execution_plan else "not_run"
    rows = [
        {
            "architecture_id": architecture,
            "description": arm_descriptions[architecture],
            "production_eligible": architecture in EVIDENCE_GUIDE_V2_PRODUCTION_ELIGIBLE,
            "visible_status": initial_visible_status,
            "visible_attempts": None,
            "visible_failures": None,
            "hidden_status": "unavailable" if not hidden_available else "not_run",
            "hidden_attempts": None,
            "hidden_failures": None,
            "failure_codes": [],
            "decision": "DISABLE_LIVE_GUIDE",
            "comparative_inference_permitted": False,
        }
        for architecture in EVIDENCE_GUIDE_V2_ARCHITECTURES
    ]
    if active_execution_plan:
        state = (
            "INTERIM_EXECUTION_LOCKBOX_UNAVAILABLE" if not hidden_available else "INTERIM_EXECUTION"
        )
    else:
        state = "PREREGISTERED_LOCKBOX_UNAVAILABLE" if not hidden_available else "PREREGISTERED"
    decision = "DISABLE_LIVE_GUIDE"
    automated_quality: bool | str = "not_measured"
    visible_status = initial_visible_status
    hidden_status = str(hidden_receipt.get("status") or "unavailable")
    failure_summary = (
        [
            {
                "scope": "hidden_lockbox",
                "failure": hidden_status,
                "count": 1,
            }
        ]
        if not hidden_available
        else []
    )
    artifact_files = [
        {
            "path": str(EVIDENCE_GUIDE_V2_PLAN),
            "logical_name": "evaluation/v2/campaign-plan.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_PLAN),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_PREREGISTRATION),
            "logical_name": "evaluation/v2/preregistration.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_PREREGISTRATION),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT),
            "logical_name": "evaluation/v2/hidden-lockbox-receipt.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT),
            "logical_name": "evaluation/v2/protocol-amendment-r2.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT),
            "logical_name": "evaluation/v2/excluded-r2-readback-incident.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION),
            "logical_name": "evaluation/v2/terminal-disposition.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION),
        },
        {
            "path": str(EVIDENCE_GUIDE_V2_CANARY_ROWS),
            "logical_name": "evaluation/v2/trace-canary.json",
            "sha256": _sha256(EVIDENCE_GUIDE_V2_CANARY_ROWS),
        },
    ]
    preflight_audit: dict[str, Any] | None = None
    if EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT.is_file():
        loaded_preflight = _load_json(EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT)
        verify_preflight_audit(loaded_preflight)
        preflight_audit = loaded_preflight
        artifact_files.append(
            {
                "path": str(EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT),
                "logical_name": "evaluation/v2/invalidated-preflight-audit.json",
                "sha256": _sha256(EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT),
            }
        )
    article_summary: dict[str, Any] | None = None
    release_gate: dict[str, Any] | None = None
    if EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY.is_file():
        article_summary = _load_json(EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY)
        required_summary = {
            "schema_version",
            "campaign_version",
            "claim_boundary",
            "selections",
            "causal_ablation",
            "judge_comparison",
            "release_gate",
            "usage",
            "usage_ledgers",
            "stage_results",
            "terminal_disposition",
            "decision",
            "all_r2_arms_and_failures_included",
            "practitioner_usefulness_status",
        }
        if not required_summary.issubset(article_summary):
            raise RuntimeError("Evidence Guide v2 article summary is incomplete")
        if (
            article_summary["schema_version"] != "1.0"
            or article_summary["campaign_version"] != plan["campaign_version"]
            or article_summary["all_r2_arms_and_failures_included"] is not True
            or article_summary["practitioner_usefulness_status"] != "not_measured"
            or article_summary["decision"] != "DISABLE_LIVE_GUIDE"
            or article_summary["terminal_disposition"] != terminal
        ):
            raise RuntimeError("Evidence Guide v2 article-summary boundary drifted")
        artifact_files.append(
            {
                "path": str(EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY),
                "logical_name": "evaluation/v2/article-summary.json",
                "sha256": _sha256(EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY),
            }
        )
        summary_gate = article_summary.get("release_gate")
        if summary_gate is not None:
            if not isinstance(summary_gate, dict) or not EVIDENCE_GUIDE_V2_RELEASE_GATE.is_file():
                raise RuntimeError("Evidence Guide v2 release gate lacks its hashable source file")
            release_gate = _load_json(EVIDENCE_GUIDE_V2_RELEASE_GATE)
            if release_gate != summary_gate:
                raise RuntimeError(
                    "Evidence Guide v2 release gate drifted from the article summary"
                )
            artifact_files.append(
                {
                    "path": str(EVIDENCE_GUIDE_V2_RELEASE_GATE),
                    "logical_name": "evaluation/v2/release-gate.json",
                    "sha256": _sha256(EVIDENCE_GUIDE_V2_RELEASE_GATE),
                }
            )
            if release_gate.get("release_basis") != "automated_conformance" or (
                release_gate.get("practitioner_usefulness_status") != "not_measured"
            ):
                raise RuntimeError("Evidence Guide v2 release-gate authority boundary drifted")
            hidden = release_gate.get("hidden")
            if not isinstance(hidden, dict) or (
                hidden.get("hidden_content_published") is not False
                or hidden.get("attempt_rows_published") is not False
            ):
                raise RuntimeError("Evidence Guide v2 hidden result leaked row-level material")
            decision = str(release_gate.get("decision") or "DISABLE_LIVE_GUIDE")
            if decision not in {"ENABLE_LIVE_GUIDE", "DISABLE_LIVE_GUIDE"}:
                raise RuntimeError("Evidence Guide v2 release decision is invalid")
            if decision == "ENABLE_LIVE_GUIDE" and not hidden_available:
                raise RuntimeError("Evidence Guide v2 cannot enable without the protected lockbox")
            if (
                decision != "DISABLE_LIVE_GUIDE"
                or release_gate.get("release_gate_digest")
                != terminal.get("release_gate_digest")
                or release_gate.get("study_evidence", {})
                .get("trace_canary", {})
                .get("passed")
                is not False
            ):
                raise RuntimeError("Evidence Guide v2 terminal release basis drifted")
            state = "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
            automated_quality = "not_measured"
            visible_status = "admission_failed_before_architecture"
            hidden_status = str(hidden_receipt.get("status") or "unavailable")
            failure_summary = [
                {
                    "scope": "trace_canary",
                    "failure": "A1 finite-plan semantic contract",
                    "count": 1,
                },
                {
                    "scope": "hidden_lockbox",
                    "failure": hidden_status,
                    "count": 1,
                },
            ]
        else:
            state = "INTERIM_OR_STOPPED"
            visible_status = "interim_or_stopped"

        arm_payload = article_summary.get("arm_summaries") or article_summary.get("arms")
        if arm_payload is not None:
            if not isinstance(arm_payload, list):
                raise TypeError("Evidence Guide v2 arm summaries must be a list")
            supplied: dict[str, dict[str, Any]] = {}
            for item in arm_payload:
                if not isinstance(item, dict):
                    raise TypeError("Evidence Guide v2 arm summary must be an object")
                architecture = str(item.get("architecture_id") or item.get("arm_id") or "")
                if architecture not in EVIDENCE_GUIDE_V2_ARCHITECTURES or architecture in supplied:
                    raise RuntimeError("Evidence Guide v2 article summary contains invalid arms")
                supplied[architecture] = item
            if set(supplied) != set(EVIDENCE_GUIDE_V2_ARCHITECTURES):
                raise RuntimeError("Evidence Guide v2 article summary omits a preregistered arm")
            for row in rows:
                item = supplied[row["architecture_id"]]
                row.update(
                    {
                        "visible_status": str(item.get("visible_status") or visible_status),
                        "visible_attempts": item.get("visible_attempts"),
                        "visible_failures": item.get("visible_failures"),
                        "hidden_status": str(item.get("hidden_status") or hidden_status),
                        "hidden_attempts": item.get("hidden_attempts"),
                        "hidden_failures": item.get("hidden_failures"),
                        "failure_codes": list(item.get("failure_codes") or ()),
                        "decision": str(item.get("decision") or decision),
                    }
                )
        else:
            canary_status = {
                "D0": ("canary_passed_non_scientific", 2, 0, []),
                "D1": ("canary_passed_non_scientific", 1, 0, []),
                "A1": (
                    "admission_canary_failed",
                    1,
                    1,
                    list(terminal["a1_observation"]["error_codes"]),
                ),
            }
            for row in rows:
                status, attempts, failures, codes = canary_status.get(
                    row["architecture_id"], ("not_run", 0, 0, [])
                )
                row.update(
                    {
                        "visible_status": status,
                        "visible_attempts": attempts,
                        "visible_failures": failures,
                        "hidden_status": hidden_status,
                        "hidden_attempts": 0,
                        "hidden_failures": 0,
                        "decision": decision,
                        "failure_codes": codes,
                    }
                )
    elif EVIDENCE_GUIDE_V2_RELEASE_GATE.is_file():
        raise RuntimeError("Evidence Guide v2 release gate exists without an article summary")

    canary_payload = _load_json(EVIDENCE_GUIDE_V2_CANARY_ROWS)
    raw_canary_rows = canary_payload.get("rows")
    canary_receipt = (
        (article_summary or {}).get("stage_results", {})
        .get("trace_canary", {})
        .get("receipt", {})
    )
    canary_receipt_body = {
        key: value for key, value in canary_receipt.items() if key != "receipt_sha256"
    }
    expected_canary_checks = {
        "a1_model_and_tool_spans_acknowledged": False,
        "all_protocol_completed": True,
        "all_trace_acknowledged": True,
        "d0_passing_answer_acknowledged_without_provider": True,
        "d0_rejection_acknowledged_without_release": True,
        "d1_tool_span_acknowledged": True,
        "four_unique_attempts": True,
        "three_exact_cloud_block_receipts": True,
    }
    if (
        canary_payload.get("schema_version") != "1.0"
        or not isinstance(raw_canary_rows, list)
        or len(raw_canary_rows) != 4
        or _json_sha256(raw_canary_rows) != canary_receipt.get("attempt_row_sha256")
        or canary_receipt.get("schema_version") != "1.0"
        or canary_receipt.get("stage") != "trace_canary"
        or canary_receipt.get("attempt_count") != 4
        or canary_receipt.get("scientific_denominator_attempts") != 0
        or canary_receipt.get("passed") is not False
        or canary_receipt.get("checks") != expected_canary_checks
        or canary_receipt.get("campaign_instance_sha256")
        != preregistration.get("preregistration_sha256")
        or canary_receipt.get("protocol_amendment_sha256")
        != protocol_amendment.get("receipt_sha256")
        or canary_receipt.get("receipt_sha256") != _json_sha256(canary_receipt_body)
        or canary_receipt.get("receipt_sha256")
        != terminal.get("trace_canary_receipt_sha256")
    ):
        raise RuntimeError("Evidence Guide v2 trace-canary rows drifted from the receipt")
    expected_cases = {
        (item["architecture_id"], item["case_id"]): item
        for item in preregistration["stages"]["trace_canary"]["cases"]
    }
    observed_case_identities = [
        (row.get("architecture_id"), row.get("case_id")) for row in raw_canary_rows
    ]
    if (
        len(expected_cases) != 4
        or len(set(observed_case_identities)) != 4
        or set(observed_case_identities) != set(expected_cases)
        or len({row.get("attempt_id") for row in raw_canary_rows}) != 4
    ):
        raise RuntimeError("Evidence Guide v2 canary case or attempt identity drifted")
    raw_cloud_receipts = canary_receipt.get("cloud_block_receipts")
    if not isinstance(raw_cloud_receipts, list) or len(raw_cloud_receipts) != 3:
        raise RuntimeError("Evidence Guide v2 canary cloud receipt count drifted")
    canary_cloud: dict[str, dict[str, Any]] = {}
    for block in raw_cloud_receipts:
        if not isinstance(block, dict):
            raise TypeError("Evidence Guide v2 canary cloud receipt must be an object")
        group = block.get("group")
        cloud = block.get("cloud")
        architecture = group.get("architecture_id") if isinstance(group, dict) else None
        expected_count = 2 if architecture == "D0" else 1
        if (
            architecture not in {"D0", "D1", "A1"}
            or architecture in canary_cloud
            or not isinstance(cloud, dict)
            or group.get("hidden") is not False
            or group.get("stage") != "trace_canary"
            or cloud.get("hidden") is not False
            or cloud.get("mode") != "wandb_weave"
            or block.get("completed") != expected_count
            or block.get("reused") != 0
            or cloud.get("attempt_count") != expected_count
        ):
            raise RuntimeError("Evidence Guide v2 canary cloud block receipt drifted")
        run_id = cloud.get("run_id")
        if re.fullmatch(r"[0-9a-f]{24}", str(run_id)) is None:
            raise RuntimeError("Evidence Guide v2 canary W&B run identity drifted")
        run_url = _exact_wandb_destination_url(
            cloud.get("run_url"),
            label=f"Evidence Guide v2 {architecture} canary W&B run",
            entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
            project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
        )
        weave_url = _exact_wandb_destination_url(
            cloud.get("weave_evaluation_run_url"),
            label=f"Evidence Guide v2 {architecture} canary Weave evaluation",
            entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
            project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
        )
        if (
            urlparse(run_url).path
            != f"/{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/runs/{run_id}"
            or re.fullmatch(
                rf"/{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}"
                r"/r/call/[0-9a-f-]{36}",
                urlparse(weave_url).path,
            )
            is None
            or not str(cloud.get("weave_evaluation_uri")).startswith(
                "weave:///"
                f"{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/object/"
            )
        ):
            raise RuntimeError("Evidence Guide v2 canary cloud link identity drifted")
        canary_cloud[str(architecture)] = {**cloud, "weave_evaluation_run_url": weave_url}
    canary_rows = []
    for row in raw_canary_rows:
        identity = (row.get("architecture_id"), row.get("case_id"))
        expected = expected_cases.get(identity)
        if expected is None:
            raise RuntimeError("Evidence Guide v2 canary contains an unregistered case")
        details = row.get("details")
        tools = details.get("tool_events") if isinstance(details, dict) else None
        if not isinstance(tools, list):
            raise TypeError("Evidence Guide v2 canary tool events are invalid")
        architecture = str(row["architecture_id"])
        if (
            row.get("trace_acknowledged") is not True
            or row.get("hidden") is not False
            or row.get("stage") != "trace_canary"
            or row.get("protocol_valid") is not True
        ):
            raise RuntimeError("Evidence Guide v2 canary contains an unacknowledged trace")
        if architecture == "A1" and details.get("answer_released") is not False:
            raise RuntimeError("Evidence Guide v2 rejected model answer was released")
        if architecture in {"D1", "A1"} and len(tools) != 1:
            raise RuntimeError("Evidence Guide v2 tool-backed canary shape drifted")
        if architecture == "A1" and (
            row.get("model_id") != a1_terminal["model_id"]
            or row.get("explanation_plan_id") != a1_terminal["selected_plan_id"]
            or row.get("provider_invocations") != a1_terminal["provider_invocations"]
            or row.get("transport_attempts") != a1_terminal["transport_attempts"]
            or row.get("input_tokens") != a1_terminal["input_tokens"]
            or row.get("output_tokens") != a1_terminal["output_tokens"]
            or row.get("cost_usd") != a1_terminal["cost_usd"]
            or set(row.get("error_codes") or ()) != set(a1_terminal["error_codes"])
        ):
            raise RuntimeError("Evidence Guide v2 A1 canary drifted from terminal evidence")
        if architecture == "D1" and (
            row.get("explanation_plan_id") != d1_terminal["selected_plan_id"]
            or details.get("answer_released") is not d1_terminal["answer_released"]
            or row.get("repository_fidelity") is not d1_terminal["repository_fidelity"]
            or row.get("safe") is not d1_terminal["safe"]
        ):
            raise RuntimeError("Evidence Guide v2 D1 canary drifted from terminal evidence")
        result = (
            "blocked_wrong_plan"
            if architecture == "A1"
            else (
                "expected_fail_closed"
                if expected["role"] == "d0_fail_closed_rejection"
                else "passed"
            )
        )
        canary_rows.append(
            {
                "attempt_id": row["attempt_id"],
                "case_id": row["case_id"],
                "role": expected["role"],
                "architecture_id": architecture,
                "model_id": row.get("model_id"),
                "expected_plan_id": expected.get("expected_plan_id"),
                "observed_plan_id": row.get("explanation_plan_id"),
                "provider_invocations": row.get("provider_invocations"),
                "tool_count": len(tools),
                "answer_released": details.get("answer_released"),
                "trace_acknowledged": row.get("trace_acknowledged"),
                "repository_fidelity": row.get("repository_fidelity"),
                "result": result,
                "error_codes": list(row.get("error_codes") or ()),
                "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"),
                "cost_usd": row.get("cost_usd"),
                "weave_url": canary_cloud[architecture]["weave_evaluation_run_url"],
                "scientific_denominator_included": False,
                "comparative_inference_permitted": False,
            }
        )

    links: list[dict[str, str]] = []
    artifact_references: list[str] = []
    publication_receipt: dict[str, Any] | None = None
    if EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT.is_file():
        if article_summary is None:
            raise RuntimeError("Evidence Guide v2 cloud receipt exists without an article summary")
        publication_receipt = _load_json(EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT)
        required_receipt = {
            "schema_version",
            "campaign_version",
            "entity",
            "project",
            "project_access",
            "article_summary_sha256",
            "release_gate_sha256",
            "terminal_disposition_sha256",
            "trace_canary_receipt_sha256",
            "decision",
            "all_r2_arms_and_failures_included",
            "hidden_attempt_rows_published",
            "run_file_privacy_audit",
            "publication_sha256",
        }
        publication_body = {
            key: value
            for key, value in publication_receipt.items()
            if key != "publication_sha256"
        }
        if not required_receipt.issubset(publication_receipt) or (
            publication_receipt["schema_version"] != "1.0"
            or publication_receipt["campaign_version"] != plan["campaign_version"]
            or publication_receipt["entity"] != EVIDENCE_GUIDE_V2_CLOUD_ENTITY
            or publication_receipt["project"] != EVIDENCE_GUIDE_V2_CLOUD_PROJECT
            or str(publication_receipt["project_access"]).upper() != "RESTRICTED"
            or publication_receipt["article_summary_sha256"]
            != _sha256(EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY)
            or publication_receipt["terminal_disposition_sha256"]
            != _sha256(EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION)
            or publication_receipt["trace_canary_receipt_sha256"]
            != terminal["trace_canary_receipt_sha256"]
            or publication_receipt["decision"] != "DISABLE_LIVE_GUIDE"
            or publication_receipt["all_r2_arms_and_failures_included"] is not True
            or publication_receipt["hidden_attempt_rows_published"] is not False
            or publication_receipt["run_file_privacy_audit"].get("prohibited_files") != 0
            or publication_receipt["publication_sha256"]
            != _json_sha256(publication_body)
        ):
            raise RuntimeError("Evidence Guide v2 cloud-publication receipt drifted")
        if release_gate is not None and publication_receipt.get("release_gate_sha256") != _sha256(
            EVIDENCE_GUIDE_V2_RELEASE_GATE
        ):
            raise RuntimeError("Evidence Guide v2 cloud receipt has the wrong release gate")
        raw_links = publication_receipt.get("links") or []
        expected_link_kinds = {
            "R2b disabled release gate": "run",
            "R2b campaign evidence tables": "run",
            "Granite A1 canary in Weave": "weave",
            "Deterministic D1 canary in Weave": "weave",
            "Deterministic D0 canary in Weave": "weave",
            "Excluded zero-inference readback incident": "run",
        }
        if not isinstance(raw_links, list) or len(raw_links) != len(expected_link_kinds):
            raise TypeError("Evidence Guide v2 publication links must be a list")
        for item in raw_links:
            if not isinstance(item, dict) or set(item) != {"label", "url"}:
                raise RuntimeError("Evidence Guide v2 publication link contract drifted")
            label = str(item["label"])
            link_kind = expected_link_kinds.pop(label, None)
            if link_kind is None:
                raise RuntimeError("Evidence Guide v2 publication link identity drifted")
            url = _exact_wandb_destination_url(
                item["url"],
                label=label,
                entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
                project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
            )
            expected_path = (
                rf"/{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}"
                + (r"/runs/[0-9a-f]{24}" if link_kind == "run" else r"/r/call/[0-9a-f-]{36}")
            )
            parsed = urlparse(url)
            if re.fullmatch(expected_path, parsed.path) is None or parsed.query or parsed.params:
                raise RuntimeError("Evidence Guide v2 publication link path drifted")
            links.append({"label": label, "url": url})
        if expected_link_kinds:
            raise RuntimeError("Evidence Guide v2 publication links are incomplete")
        raw_artifacts = publication_receipt.get("artifact_references") or []
        if not isinstance(raw_artifacts, list) or any(
            not isinstance(value, str)
            or re.fullmatch(
                rf"{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/"
                rf"{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/[a-z0-9][a-z0-9._-]*:v[0-9]+",
                value,
            )
            is None
            or "hidden" in value.lower()
            for value in raw_artifacts
        ):
            raise RuntimeError("Evidence Guide v2 publication artifact references are invalid")
        link_by_label = {item["label"]: item["url"] for item in links}
        expected_canary_links = {
            "Granite A1 canary in Weave": canary_cloud["A1"][
                "weave_evaluation_run_url"
            ],
            "Deterministic D1 canary in Weave": canary_cloud["D1"][
                "weave_evaluation_run_url"
            ],
            "Deterministic D0 canary in Weave": canary_cloud["D0"][
                "weave_evaluation_run_url"
            ],
        }
        if any(
            link_by_label.get(label) != url
            for label, url in expected_canary_links.items()
        ):
            raise RuntimeError("Evidence Guide v2 canary publication links drifted")
        artifact_references = list(raw_artifacts)
        artifact_files.append(
            {
                "path": str(EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT),
                "logical_name": "evaluation/v2/wandb-publication.json",
                "sha256": _sha256(EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT),
            }
        )

    incident_publication_url = next(
        (
            item["url"]
            for item in links
            if item["label"] == "Excluded zero-inference readback incident"
        ),
        None,
    )
    operational_incident_projection = {
        "receipt_sha256": operational_incident["receipt_sha256"],
        "file_sha256": _sha256(EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT),
        "disposition": operational_incident["disposition"],
        "failure_code": operational_incident["failure_code"],
        "canary_attempt_count": operational_incident["canary_attempt_count"],
        "scientific_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "provider_usage": incident_usage,
        "publication_url": incident_publication_url,
        "comparative_inference_permitted": False,
    }
    snapshot_identity = {
        "state": state,
        "decision": decision,
        "plan_sha256": _sha256(EVIDENCE_GUIDE_V2_PLAN),
        "preregistration_sha256": _sha256(EVIDENCE_GUIDE_V2_PREREGISTRATION),
        "hidden_receipt_sha256": _sha256(EVIDENCE_GUIDE_V2_HIDDEN_RECEIPT),
        "protocol_amendment_sha256": _sha256(EVIDENCE_GUIDE_V2_PROTOCOL_AMENDMENT),
        "operational_incident_sha256": _sha256(EVIDENCE_GUIDE_V2_OPERATIONAL_INCIDENT),
        "terminal_disposition_sha256": _sha256(EVIDENCE_GUIDE_V2_TERMINAL_DISPOSITION),
        "trace_canary_rows_sha256": _sha256(EVIDENCE_GUIDE_V2_CANARY_ROWS),
        "article_summary_sha256": (
            _sha256(EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY) if article_summary is not None else None
        ),
        "release_gate_sha256": (
            _sha256(EVIDENCE_GUIDE_V2_RELEASE_GATE) if release_gate is not None else None
        ),
        "publication_receipt_sha256": (
            _sha256(EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT)
            if publication_receipt is not None
            else None
        ),
        "preflight_audit_sha256": (
            str(preflight_audit["receipt_sha256"]) if preflight_audit is not None else None
        ),
    }
    return {
        "key": EVIDENCE_GUIDE_V2_KEY,
        "campaign_version": plan["campaign_version"],
        "state": state,
        "decision": decision,
        "claim_boundary": (
            article_summary.get("claim_boundary")
            if article_summary is not None
            else (
                "Automated explanation conformance only; practitioner usefulness, hydraulic "
                "science, and ecological outcomes were not measured."
            )
        ),
        "automated_explanation_quality": automated_quality,
        "practitioner_usefulness_status": "not_measured",
        "visible_status": visible_status,
        "hidden_status": hidden_status,
        "hidden_available": hidden_available,
        "hidden_attempt_rows_published": False,
        "canary_attempt_count": len(canary_rows),
        "scientific_denominator_attempts": terminal["scientific_denominator_attempts"],
        "architecture_denominator_attempts": terminal[
            "architecture_denominator_attempts"
        ],
        "architecture_comparison_status": "not_measured",
        "comparative_inference_permitted": False,
        "budget": {**budget, "worst_case_cost_usd": str(budget["worst_case_cost_usd"])},
        "confirmatory_architecture_envelopes": confirmatory_envelopes,
        "rows": rows,
        "canary_rows": canary_rows,
        "failure_summary": failure_summary,
        "terminal_disposition": terminal,
        "stage_results": (article_summary or {}).get("stage_results", {}),
        "usage": (article_summary or {}).get("usage", {}),
        "usage_ledgers": (article_summary or {}).get("usage_ledgers", {}),
        "links": links,
        "artifact_references": artifact_references,
        "artifact_files": artifact_files,
        "invalidated_preflight_audit": preflight_audit,
        "operational_incident": operational_incident_projection,
        "cloud_publication_receipt_self_sha256": (
            publication_receipt.get("publication_sha256")
            if publication_receipt is not None
            else None
        ),
        "snapshot_sha256": _json_sha256(snapshot_identity),
        **snapshot_identity,
    }


def _evidence_guide_v2_markdown(evaluation: dict[str, Any]) -> str:
    budget = evaluation["budget"]
    confirmatory_envelopes = evaluation["confirmatory_architecture_envelopes"]
    a2 = confirmatory_envelopes["A2"]
    a3 = confirmatory_envelopes["A3"]
    automated = evaluation["automated_explanation_quality"]
    automated_label = str(automated).lower() if isinstance(automated, bool) else str(automated)
    failures = (
        ", ".join(
            f"{item['scope']}: {item['failure']} ({item['count']})"
            for item in evaluation["failure_summary"]
            if int(item["count"]) > 0
        )
        or "No receipt-backed failure count is available."
    )
    links = ""
    if evaluation["links"]:
        links = "\n\n" + " · ".join(
            f"[{item['label']}]({item['url']})" for item in evaluation["links"]
        )
    preflight_audit = evaluation.get("invalidated_preflight_audit")
    terminal = evaluation["terminal_disposition"]
    a1 = terminal["a1_observation"]
    d1 = terminal["d1_control"]
    campaign_usage = evaluation["usage"]
    if preflight_audit is None:
        preflight_markdown = (
            "No hash-bound invalidated-preflight audit is attached to this publication."
        )
    else:
        preflight_rows = []
        for incident in preflight_audit["incidents"]:
            incident_usage = incident["usage"]
            preflight_rows.append(
                "| {preflight} | `{failure}` | {attempts} | {calls} calls · "
                "{input_tokens}/{output_tokens} provider-reported tokens · ${cost} | "
                "Excluded |".format(
                    preflight=incident["preflight_id"],
                    failure=incident["failure_code"],
                    attempts=int(incident["attempt_count"]),
                    calls=int(incident_usage["provider_invocations"]),
                    input_tokens=int(incident_usage["provider_reported_input_tokens"]),
                    output_tokens=int(incident_usage["provider_reported_output_tokens"]),
                    cost=incident_usage["recorded_cost_usd"],
                )
            )
        preflight_usage = preflight_audit["usage"]
        reservations = [
            incident["reservation"]
            for incident in preflight_audit["incidents"]
            if incident["reservation"] is not None
        ]
        if not reservations or any(
            not isinstance(reservation, dict) or reservation != reservations[0]
            for reservation in reservations
        ):
            raise RuntimeError("Evidence Guide v2 preflight reservation set drifted")
        reservation = reservations[0]
        preflight_count = int(preflight_audit["invalidated_preflight_count"])
        invalidated_attempts = int(preflight_audit["invalidated_provider_attempts"])
        launch_label = "launch" if len(reservations) == 1 else "launches"
        reinitialization_note = ""
        if preflight_count == 3:
            reinitialization_note = (
                " In `weave-reinitialization-flush`, all six protocol probes and one block "
                "receipt completed, but the time-block session did not; completed spans alone "
                "do not turn an invalid launch into a capability result."
            )
        preflight_markdown = (
            f"{preflight_count} launch attempts failed before a capability time block was "
            "completed. They are "
            "operational audit records, not model scores.\n\n"
            "| Invalidated preflight | Failure | Provider attempts | Actual recorded use | "
            "Capability denominator |\n"
            "|---|---|---:|---|---|\n"
            + "\n".join(preflight_rows)
            + f"\n\nAcross these {preflight_count} preflights, provider-reported use was "
            f"{int(preflight_usage['provider_invocations'])} calls, "
            f"{int(preflight_usage['provider_reported_input_tokens'])} input tokens, "
            f"{int(preflight_usage['provider_reported_output_tokens'])} output tokens, and "
            f"${preflight_usage['recorded_cost_usd']}. Conservative cap accounting was "
            f"{int(preflight_usage['conservative_accounted_input_tokens']):,} input and "
            f"{int(preflight_usage['conservative_accounted_output_tokens']):,} output tokens. "
            f"{len(reservations)} paid capability {launch_label} used a "
            f"${reservation['cost_ceiling_usd']} reservation ceiling; this was not measured "
            "spend. No "
            f"answer was released, and all {invalidated_attempts} invalidated provider attempts "
            "remain outside capability and architecture denominators."
            f"{reinitialization_note}"
        )
    return (
        "### Evidence Guide v2: architecture-first, fail-closed\n\n"
        f"Current evidence state: **`{evaluation['state']}`**. Release decision: "
        f"**`{evaluation['decision']}`**. Visible evaluation is **{evaluation['visible_status']}**; "
        f"the hidden confirmatory set is **{evaluation['hidden_status']}**. Hidden attempt rows "
        "are not published.\n\n"
        "The W&B/Weave plumbing passed its fresh four-case canary: all traces were acknowledged, "
        "the protocol calls completed, and all three cloud block receipts were read back. The "
        "model behavior did not pass. On the safety-critical unsupported-authority question, "
        f"Granite selected `{a1['selected_plan_id']}` instead of the required "
        f"`{a1['expected_plan_id']}`. The repository validator rejected the generated evidence "
        "and **released no model answer**. On the same question, deterministic `D1` selected "
        f"`{d1['selected_plan_id']}` and produced the bounded refusal.\n\n"
        "This was a preregistered admission check outside the scientific denominator. Because it "
        "had to pass before architecture testing, the architecture, prompt, validation, judge, "
        "hidden, and soak stages were not run. The correct claim is therefore not that D1 "
        "scientifically beat A1; A1 failed admission and D1 remains the shipping safety default.\n\n"
        f"The fresh model call used {int(a1['input_tokens'])} provider-reported input tokens, "
        f"{int(a1['output_tokens'])} output tokens, and ${a1['cost_usd']}. Cumulative conservative "
        f"campaign accounting is ${campaign_usage['cost_usd']}, "
        f"{int(campaign_usage['provider_invocations']):,} provider/transport calls, "
        f"{int(campaign_usage['input_tokens']):,} input tokens, and "
        f"{int(campaign_usage['output_tokens']):,} output tokens.\n\n"
        f"`automated_explanation_quality`: **{automated_label}** · "
        "`practitioner_usefulness`: **not_measured**. The former is a deterministic conformance "
        "label; no comparative quality or practitioner rating was reached in this stopped run.\n\n"
        f"Receipt-backed failures: {failures}\n\n"
        "#### Invalidated infrastructure preflights\n\n"
        f"{preflight_markdown}\n\n"
        "A later R2 readback incident is recorded separately: it stopped before the A1 call, used "
        "zero inference, and contributes zero scientific observations. It motivated bounded "
        "eventual-consistency polling and a fresh preregistered R2b campaign rather than resuming "
        "partial state.\n\n"
        f"Only admissible preregistered branch ceiling (`A1`): "
        f"{int(budget['logical_provider_invocations']):,} logical provider invocations, "
        f"{int(budget['worst_case_transport_attempts']):,} transport attempts, "
        f"{int(budget['worst_case_input_tokens']):,} input tokens, "
        f"{int(budget['worst_case_output_tokens']):,} output tokens, and "
        f"${float(budget['worst_case_cost_usd']):,.6f}. `A2` and `A3` are preregistered "
        f"inadmissible ({int(a2['worst_case_transport_attempts']):,}/"
        f"{int(a3['worst_case_transport_attempts']):,} transport attempts; "
        f"{int(a2['worst_case_input_tokens']):,}/{int(a3['worst_case_input_tokens']):,} input "
        f"tokens; {int(a2['worst_case_output_tokens']):,}/"
        f"{int(a3['worst_case_output_tokens']):,} output tokens). If either remains in the worst "
        "finalist envelope, the runner stops before validation. These are ceilings, not actual "
        "use.\n\n"
        f"*{evaluation['claim_boundary']}*{links}"
    )


def _load_evidence_guide_v2_architecture_development() -> dict[str, Any]:
    """Load the optional, release-ineligible architecture-development package.

    This package is deliberately separate from the v2 release campaign. Partial
    output is never projected into the article: the summary, table, media, and
    cloud publication receipt must all be present and mutually hash-bound.
    """

    paths = (
        EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY,
        EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE,
        EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA,
        EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT,
    )
    present = tuple(path.is_file() for path in paths)
    unavailable_identity = {
        "available": False,
        "study_version": EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION,
        "state": "AWAITING_HASH_PINNED_PUBLICATION",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": "not_consumed",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
    }
    if not any(present):
        return {
            **unavailable_identity,
            "claim_boundary": (
                "No architecture-development outcome is shown until a complete, "
                "hash-pinned publication package is available."
            ),
            "summary": None,
            "columns": [],
            "rows": [],
            "media": None,
            "links": [],
            "artifact_references": [],
            "artifact_files": [],
            "snapshot_sha256": _json_sha256(unavailable_identity),
        }
    if not all(present):
        missing = ", ".join(path.name for path, exists in zip(paths, present) if not exists)
        raise RuntimeError(
            "Evidence Guide architecture-development publication is incomplete: " + missing
        )

    summary = _load_json(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY)
    table = _load_json(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE)
    receipt = _load_json(EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT)
    media_sha256 = _sha256(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA)
    table_file_sha256 = _sha256(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE)
    summary_file_sha256 = _sha256(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY)

    required_summary = {
        "schema_version",
        "study_version",
        "state",
        "production_decision",
        "release_eligible",
        "validation_status",
        "practitioner_usefulness_status",
        "energy_status",
        "ecological_outcomes_status",
        "headline",
        "plain_language_finding",
        "claim_boundary",
        "limitations",
        "attempt_count",
        "configuration_count",
        "generated_invalid_count",
        "released_invalid_count",
        "usage",
        "media_title",
        "media_caption",
        "media_alt_text",
        "article_table_sha256",
        "article_media_sha256",
        "summary_sha256",
    }
    summary_body = {key: value for key, value in summary.items() if key != "summary_sha256"}
    expected_summary_values = {
        "schema_version": "1.0",
        "study_version": EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION,
        "state": "EXPLORATORY_COMPLETE_LIVE_GUIDE_UNCHANGED_DISABLED",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
    }
    if not required_summary.issubset(summary) or any(
        summary.get(key) != value for key, value in expected_summary_values.items()
    ):
        raise RuntimeError("Evidence Guide architecture-development summary boundary drifted")
    if summary.get("summary_sha256") != _json_sha256(summary_body):
        raise RuntimeError("Evidence Guide architecture-development summary self-hash drifted")
    if (
        summary.get("article_table_sha256") != table_file_sha256
        or summary.get("article_media_sha256") != media_sha256
    ):
        raise RuntimeError("Evidence Guide architecture-development summary inputs drifted")
    text_fields = (
        "headline",
        "plain_language_finding",
        "claim_boundary",
        "media_title",
        "media_caption",
        "media_alt_text",
    )
    if any(not isinstance(summary.get(name), str) or not summary[name].strip() for name in text_fields):
        raise RuntimeError("Evidence Guide architecture-development article copy is incomplete")
    limitations = summary.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
        not isinstance(item, str) or not item.strip() for item in limitations
    ):
        raise RuntimeError("Evidence Guide architecture-development limitations are incomplete")

    count_fields = (
        "attempt_count",
        "configuration_count",
        "generated_invalid_count",
        "released_invalid_count",
    )
    if any(
        isinstance(summary.get(name), bool)
        or not isinstance(summary.get(name), int)
        or int(summary[name]) < 0
        for name in count_fields
    ):
        raise RuntimeError("Evidence Guide architecture-development counts are invalid")
    if summary["generated_invalid_count"] > summary["attempt_count"] or (
        summary["released_invalid_count"] > summary["generated_invalid_count"]
    ):
        raise RuntimeError("Evidence Guide architecture-development failure counts are invalid")
    if summary["attempt_count"] == 0 or summary["configuration_count"] == 0:
        raise RuntimeError("Evidence Guide architecture-development result is empty")
    usage = summary.get("usage")
    required_usage = {
        "cost_usd",
        "provider_invocations",
        "transport_attempts",
        "input_tokens",
        "output_tokens",
    }
    if not isinstance(usage, dict) or set(usage) != required_usage:
        raise RuntimeError("Evidence Guide architecture-development usage ledger is incomplete")
    try:
        usage_cost = float(usage["cost_usd"])
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Evidence Guide architecture-development cost ledger is invalid"
        ) from error
    if not math.isfinite(usage_cost) or usage_cost < 0 or any(
        isinstance(usage[name], bool)
        or not isinstance(usage[name], int)
        or usage[name] < 0
        for name in required_usage - {"cost_usd"}
    ):
        raise RuntimeError("Evidence Guide architecture-development usage ledger is invalid")

    required_table = {
        "schema_version",
        "study_version",
        "columns",
        "rows",
        "table_sha256",
    }
    table_body = {key: value for key, value in table.items() if key != "table_sha256"}
    columns = table.get("columns")
    rows = table.get("rows")
    if (
        not required_table.issubset(table)
        or table.get("schema_version") != "1.0"
        or table.get("study_version") != EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION
        or table.get("table_sha256") != _json_sha256(table_body)
        or not isinstance(columns, list)
        or columns != list(EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_COLUMNS)
        or not isinstance(rows, list)
        or len(rows) != summary["configuration_count"]
        or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows)
    ):
        raise RuntimeError("Evidence Guide architecture-development article table drifted")
    for row in rows:
        for name in (
            "attempt_count",
            "generated_invalid_count",
            "released_invalid_count",
            "scenario_count",
            "scenario_ever_failed_count",
            "logical_provider_calls",
            "transport_attempts",
            "input_tokens",
            "output_tokens",
        ):
            if isinstance(row[name], bool) or not isinstance(row[name], int) or row[name] < 0:
                raise RuntimeError("Evidence Guide architecture-development row counts are invalid")
        if any(
            not isinstance(row[name], str) or not row[name].strip()
            for name in ("stage", "model_id", "architecture_id")
        ):
            raise RuntimeError("Evidence Guide architecture-development row identity is invalid")
        if (
            row["generated_invalid_count"] > row["attempt_count"]
            or row["released_invalid_count"] > row["generated_invalid_count"]
            or row["scenario_ever_failed_count"] > row["scenario_count"]
            or row["logical_provider_calls"] > row["transport_attempts"]
        ):
            raise RuntimeError("Evidence Guide architecture-development row totals are invalid")
        for name in (
            "valid_generation_rate",
            "causal_success_rate",
            "trace_acknowledgement_rate",
        ):
            if row[name] is not None:
                rate = _finite_number(row[name], label=f"architecture development {name}", minimum=0)
                if rate > 1:
                    raise RuntimeError("Evidence Guide architecture-development rate exceeds one")
        try:
            row_cost = float(row["cost_usd"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("Evidence Guide architecture-development row cost is invalid") from error
        if not math.isfinite(row_cost) or row_cost < 0:
            raise RuntimeError("Evidence Guide architecture-development row cost is invalid")
    if sum(row["attempt_count"] for row in rows) != summary["attempt_count"] or sum(
        row["generated_invalid_count"] for row in rows
    ) != summary["generated_invalid_count"] or sum(
        row["released_invalid_count"] for row in rows
    ) != summary["released_invalid_count"]:
        raise RuntimeError("Evidence Guide architecture-development table totals drifted")

    if not EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Evidence Guide architecture-development media is not a PNG")

    required_receipt = {
        "schema_version",
        "study_version",
        "entity",
        "project",
        "project_access",
        "production_decision",
        "release_eligible",
        "validation_status",
        "practitioner_usefulness_status",
        "energy_status",
        "ecological_outcomes_status",
        "article_summary_sha256",
        "article_table_sha256",
        "article_media_sha256",
        "table_key",
        "media_key",
        "run_id",
        "run_url",
        "weave_evaluation_url",
        "artifact",
        "run_file_privacy_audit",
        "publication_sha256",
    }
    receipt_body = {key: value for key, value in receipt.items() if key != "publication_sha256"}
    receipt_audit = receipt.get("run_file_privacy_audit")
    receipt_boundary = {
        "schema_version": "1.0",
        "study_version": EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION,
        "entity": EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
        "project": EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "table_key": EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_KEY,
        "media_key": EVIDENCE_GUIDE_V2_ARCH_DEV_MEDIA_KEY,
    }
    if (
        not required_receipt.issubset(receipt)
        or any(receipt.get(key) != value for key, value in receipt_boundary.items())
        or str(receipt.get("project_access")).upper() != "RESTRICTED"
        or receipt.get("article_summary_sha256") != summary_file_sha256
        or receipt.get("article_table_sha256") != table_file_sha256
        or receipt.get("article_media_sha256") != media_sha256
        or receipt.get("publication_sha256") != _json_sha256(receipt_body)
        or not isinstance(receipt_audit, dict)
        or receipt_audit.get("prohibited_files") != 0
    ):
        raise RuntimeError("Evidence Guide architecture-development publication receipt drifted")
    run_url = _exact_wandb_destination_url(
        receipt["run_url"],
        label="architecture-development W&B run",
        entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
        project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
    )
    weave_url = _exact_wandb_destination_url(
        receipt["weave_evaluation_url"],
        label="architecture-development Weave evaluation",
        entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
        project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
    )
    if (
        re.fullmatch(
            rf"/{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/runs/[0-9a-f]{{24}}",
            urlparse(run_url).path,
        )
        is None
        or re.fullmatch(
            rf"/{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/r/call/[0-9a-f-]{{36}}",
            urlparse(weave_url).path,
        )
        is None
    ):
        raise RuntimeError("Evidence Guide architecture-development cloud link drifted")
    if not isinstance(receipt.get("run_id"), str) or (
        re.fullmatch(r"[0-9a-f]{24}", receipt["run_id"]) is None
        or urlparse(run_url).path.rsplit("/", 1)[-1] != receipt["run_id"]
        or urlparse(run_url).query
        or urlparse(weave_url).query
    ):
        raise RuntimeError("Evidence Guide architecture-development run identity drifted")
    artifact = receipt.get("artifact")
    if (
        not isinstance(artifact, str)
        or "hidden" in artifact.lower()
        or re.fullmatch(
            rf"{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/"
            r"[a-z0-9][a-z0-9._-]*:v[0-9]+",
            artifact,
        )
        is None
    ):
        raise RuntimeError("Evidence Guide architecture-development artifact reference drifted")

    snapshot_identity = {
        "available": True,
        "summary_file_sha256": summary_file_sha256,
        "table_file_sha256": table_file_sha256,
        "media_file_sha256": media_sha256,
        "publication_receipt_file_sha256": _sha256(
            EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT
        ),
        "publication_receipt_self_sha256": receipt["publication_sha256"],
    }
    return {
        "available": True,
        "study_version": EVIDENCE_GUIDE_V2_ARCH_DEV_STUDY_VERSION,
        "state": summary["state"],
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": summary["validation_status"],
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "claim_boundary": summary["claim_boundary"],
        "summary": summary,
        "columns": columns,
        "rows": rows,
        "media": {
            "path": str(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA),
            "sha256": media_sha256,
            "title": summary["media_title"],
            "caption": summary["media_caption"],
            "alt_text": summary["media_alt_text"],
            "key": EVIDENCE_GUIDE_V2_ARCH_DEV_MEDIA_KEY,
        },
        "links": [
            {"label": "Open architecture-development run in W&B", "url": run_url},
            {"label": "Open exploratory evaluation in Weave", "url": weave_url},
        ],
        "artifact_references": [artifact],
        "artifact_files": [
            {
                "path": str(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY),
                "logical_name": "evaluation/v2-architecture-development/article-summary.json",
                "sha256": summary_file_sha256,
            },
            {
                "path": str(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE),
                "logical_name": "evaluation/v2-architecture-development/article-table.json",
                "sha256": table_file_sha256,
            },
            {
                "path": str(EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA),
                "logical_name": "evaluation/v2-architecture-development/article-media.png",
                "sha256": media_sha256,
            },
            {
                "path": str(EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT),
                "logical_name": "evaluation/v2-architecture-development/wandb-publication.json",
                "sha256": snapshot_identity["publication_receipt_file_sha256"],
            },
        ],
        "snapshot_sha256": _json_sha256(snapshot_identity),
        **snapshot_identity,
    }


def _evidence_guide_v2_architecture_development_markdown(
    evaluation: dict[str, Any],
) -> str:
    if not evaluation["available"]:
        return (
            "### Architecture-development study: awaiting a complete receipt\n\n"
            "No architecture-development result is displayed because the hash-pinned summary, "
            "table, media, and W&B/Weave publication receipt are not all present. Production "
            "remains **`DISABLE_LIVE_GUIDE`**. Validation is not yet consumed; practitioner "
            "usefulness, provider energy use, carbon impact, hydraulic validity, and ecological "
            "outcomes remain **not measured**."
        )

    summary = evaluation["summary"]
    usage = summary["usage"]
    limitations = "\n".join(f"- {item}" for item in summary["limitations"])
    links = " · ".join(f"[{item['label']}]({item['url']})" for item in evaluation["links"])
    return (
        "### Architecture-development study: exploratory result\n\n"
        f"**{summary['headline']}**\n\n{summary['plain_language_finding']}\n\n"
        f"The receipt-bound study contains {int(summary['attempt_count']):,} attempts across "
        f"{int(summary['configuration_count']):,} reported configurations. Repository checks "
        f"flagged {int(summary['generated_invalid_count']):,} generated-invalid attempts; "
        f"{int(summary['released_invalid_count']):,} invalid attempts were marked released in "
        "the isolated exploratory record. Nothing in this study was released into the product. "
        "These counts describe automated behavior, not engineering usefulness.\n\n"
        "The visible validation split is **consumed for exploratory development** and is no "
        "longer untouched confirmation. This package is permanently release-ineligible; "
        "production remains **`DISABLE_LIVE_GUIDE`**.\n\n"
        f"Recorded provider usage: {int(usage['provider_invocations']):,} provider invocations, "
        f"{int(usage['transport_attempts']):,} transports, "
        f"{int(usage['input_tokens']):,} input tokens, "
        f"{int(usage['output_tokens']):,} output tokens, and ${float(usage['cost_usd']):,.6f}. "
        "Practitioner usefulness, provider energy use, carbon impact, hydraulic validity, and "
        "ecological outcomes were not measured.\n\n"
        f"#### Boundaries\n\n{limitations}\n\n"
        f"*{evaluation['claim_boundary']}*\n\n{links}"
    )


def _r4c_json_sha256(value: Any) -> str:
    """Match the receipt builder's UTF-8 canonical JSON identity."""

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_evidence_guide_r4c_grounding() -> dict[str, Any]:
    """Load only the complete, hash-bound R4c publication-recovery package."""

    incident_paths = {
        "protocol-amendment-r4b.json": EVIDENCE_GUIDE_R4C_R4B_AMENDMENT,
        "protocol-amendment-r4c.json": EVIDENCE_GUIDE_R4C_AMENDMENT,
        "r4b-incident-snapshot.json": EVIDENCE_GUIDE_R4C_R4B_SNAPSHOT,
        "publication-amendment-r4c.json": EVIDENCE_GUIDE_R4C_PUBLICATION_AMENDMENT,
    }
    if any(not path.is_file() for path in incident_paths.values()):
        raise RuntimeError("The checked-in R4/R4b/R4c incident chain is incomplete")
    incidents = {name: _load_json(path) for name, path in incident_paths.items()}
    r4 = incidents["protocol-amendment-r4b.json"].get("r4_live_incident")
    r4b = incidents["r4b-incident-snapshot.json"].get("incident_summary")
    publication_failure = incidents["publication-amendment-r4c.json"].get("failure")
    if (
        not isinstance(r4, dict)
        or r4.get("attempt_count") != 224
        or r4.get("scientific_denominator") != 0
        or not isinstance(r4b, dict)
        or r4b.get("provider_invocations") != 272
        or r4b.get("accepted_traced_responses") != 238
        or r4b.get("post_response_input_cap_rejections") != 34
        or not isinstance(publication_failure, dict)
    ):
        raise RuntimeError("The preserved R4/R4b/R4c incident chain drifted")

    package_paths = (
        EVIDENCE_GUIDE_R4C_SUMMARY,
        EVIDENCE_GUIDE_R4C_TABLE,
        EVIDENCE_GUIDE_R4C_TABLE_CSV,
        EVIDENCE_GUIDE_R4C_MEDIA,
        EVIDENCE_GUIDE_R4C_MANIFEST,
    )
    present = tuple(path.is_file() for path in package_paths)
    unavailable_identity = {
        "available": False,
        "study_version": EVIDENCE_GUIDE_R4C_STUDY_VERSION,
        "state": "AWAITING_FINAL_HASH_BOUND_R4C_PACKAGE",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "evaluation_scope": "synthetic_post_retrieval_automated_conformance",
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_validity_status": "not_measured",
        "ecological_value_status": "not_measured",
        "incident_sha256": {name: _sha256(path) for name, path in incident_paths.items()},
    }
    if not any(present):
        return {
            **unavailable_identity,
            "claim_boundary": (
                "No R4c outcome is shown until the recovered cloud receipt, unchanged seven-file "
                "analysis package, article table, analytical media, and all hashes agree."
            ),
            "failed_r4_protocol_incident": r4,
            "failed_r4b_protocol_incident": r4b,
            "failed_r4c_publication_incident": publication_failure,
            "summary": None,
            "columns": list(EVIDENCE_GUIDE_R4C_TABLE_COLUMNS),
            "rows": [],
            "media": None,
            "links": [],
            "artifact_references": [],
            "artifact_files": [],
            "snapshot_sha256": _r4c_json_sha256(unavailable_identity),
        }
    if not all(present):
        missing = ", ".join(
            path.name for path, exists in zip(package_paths, present) if not exists
        )
        raise RuntimeError("Evidence Guide R4c article package is incomplete: " + missing)

    summary = _load_json(EVIDENCE_GUIDE_R4C_SUMMARY)
    table = _load_json(EVIDENCE_GUIDE_R4C_TABLE)
    manifest = _load_json(EVIDENCE_GUIDE_R4C_MANIFEST)
    for payload, field, label in (
        (summary, "summary_sha256", "R4c article summary"),
        (table, "table_sha256", "R4c article table"),
        (manifest, "manifest_sha256", "R4c article manifest"),
    ):
        claimed = payload.get(field)
        body = {key: value for key, value in payload.items() if key != field}
        if not isinstance(claimed, str) or claimed != _r4c_json_sha256(body):
            raise RuntimeError(f"{label} self-hash drifted")

    instance = summary.get("study_instance_sha256")
    if (
        not isinstance(instance, str)
        or len(instance) != 64
        or table.get("study_instance_sha256") != instance
        or manifest.get("study_instance_sha256") != instance
        or any(
            payload.get("study_version") != EVIDENCE_GUIDE_R4C_STUDY_VERSION
            for payload in (summary, table, manifest)
        )
    ):
        raise RuntimeError("Evidence Guide R4c study identity drifted")
    expected_summary = {
        "state": "R4C_SYNTHETIC_POST_RETRIEVAL_EVALUATION_COMPLETE",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "production_release_eligible": False,
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_validity_status": "not_measured",
        "ecological_value_status": "not_measured",
        "retrieval_quality_status": "not_measured",
        "safety_and_injection_status": "not_measured",
        "evaluation_scope": "synthetic_post_retrieval_automated_conformance",
        "attempt_count": 672,
    }
    if any(summary.get(key) != value for key, value in expected_summary.items()):
        raise RuntimeError("Evidence Guide R4c authority boundary drifted")
    d1 = summary.get("d1_deterministic_control")
    if not isinstance(d1, dict) or d1.get("passed_count") != 192 or d1.get("cell_count") != 192:
        raise RuntimeError("Evidence Guide R4c D1 control drifted")
    counterfactual = summary.get("representative_counterfactual")
    required_counterfactual = {
        "model_id",
        "case_id",
        "project_family",
        "presentation_index",
        "metric",
        "object_id",
        "units",
        "operator",
        "threshold",
        "original_value",
        "changed_value",
        "original_rule_met",
        "changed_rule_met",
        "same_plan_returned",
        "original_trace_url",
        "changed_trace_url",
        "answer_released",
    }
    if (
        not isinstance(counterfactual, dict)
        or set(counterfactual) != required_counterfactual
        or counterfactual.get("original_rule_met") is not True
        or counterfactual.get("changed_rule_met") is not False
        or counterfactual.get("same_plan_returned") is not True
        or counterfactual.get("answer_released") is not False
    ):
        raise RuntimeError("Evidence Guide R4c counterfactual example drifted")
    for name in ("original_trace_url", "changed_trace_url"):
        _actual_weave_conversation_url(
            counterfactual[name],
            label=f"Evidence Guide R4c {name}",
            entity=EVIDENCE_GUIDE_R4C_ENTITY,
            project=EVIDENCE_GUIDE_R4C_PROJECT,
        )
    if (
        summary.get("failed_r4_protocol_incident") != r4
        or summary.get("failed_r4b_protocol_incident") != r4b
        or summary.get("failed_r4c_publication_incident") != publication_failure
    ):
        raise RuntimeError("Evidence Guide R4c incident history drifted")

    mechanism_rows = summary.get("mechanism_summaries")
    if not isinstance(mechanism_rows, list) or len(mechanism_rows) != 4:
        raise RuntimeError("Evidence Guide R4c mechanism summary drifted")
    exact_validation = {
        "ibm-granite/granite-4.1-8b": (1, 6, 10, 0),
        "Qwen/Qwen3-30B-A3B-Instruct-2507": (5, 7, 10, 3),
    }
    for model, expected in exact_validation.items():
        matches = [
            row
            for row in mechanism_rows
            if row.get("model_id") == model and row.get("split") == "validation"
        ]
        if len(matches) != 1:
            raise RuntimeError("Evidence Guide R4c validation mechanism cell drifted")
        row = matches[0]
        observed = tuple(
            int(row[key]["success"])
            for key in (
                "threshold_sensitivity",
                "distractor_specificity",
                "withheld_target_abstention",
                "complete_mechanism",
            )
        )
        if row.get("denominator") != 12 or observed != expected:
            raise RuntimeError("Evidence Guide R4c exact validation result drifted")
    prose_rows = summary.get("prose_cap_summaries")
    expected_prose = {
        "ibm-granite/granite-4.1-8b": (1, 36),
        "Qwen/Qwen3-30B-A3B-Instruct-2507": (11, 36),
    }
    if not isinstance(prose_rows, list) or len(prose_rows) != 2:
        raise RuntimeError("Evidence Guide R4c prose summary drifted")
    for row in prose_rows:
        short, long = expected_prose[str(row.get("model_id"))]
        if (
            row.get("caps", {}).get("128", {}).get("success") != short
            or row.get("caps", {}).get("128", {}).get("denominator") != 36
            or row.get("caps", {}).get("256", {}).get("success") != long
            or row.get("caps", {}).get("256", {}).get("denominator") != 36
        ):
            raise RuntimeError("Evidence Guide R4c exact prose-cap result drifted")
    total = summary.get("total_resource_summary")
    if (
        not isinstance(total, dict)
        or total.get("attempt_count") != 672
        or total.get("provider_response_count") != 672
        or total.get("trace_acknowledged_count") != 672
        or total.get("provider_invocations") != 672
        or total.get("transport_attempts") != 672
        or not isinstance(total.get("input_tokens"), int)
        or total["input_tokens"] <= 0
        or not isinstance(total.get("output_tokens"), int)
        or total["output_tokens"] <= 0
        or not isinstance(total.get("cost_usd"), str)
        or float(total["cost_usd"]) < 0
    ):
        raise RuntimeError("Evidence Guide R4c resource total drifted")

    columns = table.get("columns")
    rows = table.get("rows")
    if (
        table.get("schema_version") != "1.0"
        or columns != list(EVIDENCE_GUIDE_R4C_TABLE_COLUMNS)
        or not isinstance(rows, list)
        or not rows
        or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows)
    ):
        raise RuntimeError("Evidence Guide R4c accessible table drifted")
    required_sections = {
        "grounding_mechanism",
        "prose_cap_primary",
        "prose_cap_paired_protocol",
        "model_resource_ledger",
        "deterministic_control",
        "protocol_incident",
    }
    if not required_sections.issubset({row["section"] for row in rows}):
        raise RuntimeError("Evidence Guide R4c accessible table lacks a required section")

    output_files = {
        "article_summary": EVIDENCE_GUIDE_R4C_SUMMARY,
        "article_table": EVIDENCE_GUIDE_R4C_TABLE,
        "article_table_csv": EVIDENCE_GUIDE_R4C_TABLE_CSV,
        "article_media": EVIDENCE_GUIDE_R4C_MEDIA,
    }
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != set(output_files):
        raise RuntimeError("Evidence Guide R4c output manifest drifted")
    for name, path in output_files.items():
        item = outputs.get(name)
        if not isinstance(item, dict) or item.get("path") != path.name or item.get("sha256") != _sha256(path):
            raise RuntimeError(f"Evidence Guide R4c {name} hash drifted")
    if (
        summary.get("article_table_sha256") != _sha256(EVIDENCE_GUIDE_R4C_TABLE)
        or summary.get("article_table_csv_sha256") != _sha256(EVIDENCE_GUIDE_R4C_TABLE_CSV)
        or summary.get("article_media_sha256") != _sha256(EVIDENCE_GUIDE_R4C_MEDIA)
    ):
        raise RuntimeError("Evidence Guide R4c summary output hashes drifted")

    expected_boundaries = {
        "synthetic_post_retrieval_evaluation": True,
        "scenario_is_primary_unit": True,
        "both_presentations_required": True,
        "d1_deterministic_control": True,
        "production_disabled": True,
        "practitioner_usefulness_measured": False,
        "retrieval_measured": False,
        "safety_or_prompt_injection_measured": False,
        "hydraulic_validity_measured": False,
        "ecological_value_measured": False,
        "field_performance_measured": False,
        "failed_r4_protocol_incident_included": True,
        "failed_r4b_protocol_incident_included": True,
        "failed_r4c_publication_incident_included": True,
        "publication_only_recovery_included": True,
    }
    if manifest.get("boundaries") != expected_boundaries:
        raise RuntimeError("Evidence Guide R4c manifest boundary drifted")

    source_hashes = manifest.get("source_file_sha256")
    if source_hashes != summary.get("source_file_sha256") or not isinstance(source_hashes, dict):
        raise RuntimeError("Evidence Guide R4c source-hash ledger drifted")
    expected_source_names = set(EVIDENCE_GUIDE_R4C_SOURCE_FILES) | set(incident_paths)
    if set(source_hashes) != expected_source_names:
        raise RuntimeError("Evidence Guide R4c source inventory drifted")
    source_paths = {
        name: EVIDENCE_GUIDE_R4C_SOURCE_ROOT / name
        for name in EVIDENCE_GUIDE_R4C_SOURCE_FILES
    } | incident_paths
    for name, path in source_paths.items():
        if not path.is_file() or _sha256(path) != source_hashes[name]:
            raise RuntimeError(f"Evidence Guide R4c source drifted: {name}")

    if not EVIDENCE_GUIDE_R4C_MEDIA.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Evidence Guide R4c analytical media is not a PNG")
    media = summary.get("media")
    if (
        not isinstance(media, dict)
        or media.get("width") != 1920
        or media.get("height") != 1080
        or not isinstance(media.get("title"), str)
        or not isinstance(media.get("alt_text"), str)
    ):
        raise RuntimeError("Evidence Guide R4c media metadata drifted")
    publication = summary.get("source_publication")
    if (
        not isinstance(publication, dict)
        or publication.get("entity") != EVIDENCE_GUIDE_V2_CLOUD_ENTITY
        or publication.get("project") != EVIDENCE_GUIDE_V2_CLOUD_PROJECT
        or not isinstance(publication.get("recovery_identity_sha256"), str)
        or len(publication["recovery_identity_sha256"]) != 64
    ):
        raise RuntimeError("Evidence Guide R4c recovery publication boundary drifted")
    run_url = _exact_wandb_destination_url(
        publication.get("run_url"),
        label="R4c recovery W&B run",
        entity=EVIDENCE_GUIDE_V2_CLOUD_ENTITY,
        project=EVIDENCE_GUIDE_V2_CLOUD_PROJECT,
    )
    artifact = publication.get("artifact_reference")
    if (
        not isinstance(artifact, str)
        or re.fullmatch(
            rf"{EVIDENCE_GUIDE_V2_CLOUD_ENTITY}/{EVIDENCE_GUIDE_V2_CLOUD_PROJECT}/"
            r"[a-z0-9][a-z0-9._-]*:v[0-9]+",
            artifact,
        )
        is None
    ):
        raise RuntimeError("Evidence Guide R4c recovery artifact reference drifted")

    artifact_files = [
        {
            "path": str(path),
            "logical_name": f"evaluation/r4c/{path.name}",
            "sha256": _sha256(path),
        }
        for path in package_paths
    ]
    artifact_files.extend(
        {
            "path": str(path),
            "logical_name": f"evaluation/r4c/source/{name}",
            "sha256": source_hashes[name],
        }
        for name, path in source_paths.items()
    )
    snapshot_identity = {
        "available": True,
        "study_version": EVIDENCE_GUIDE_R4C_STUDY_VERSION,
        "study_instance_sha256": instance,
        "manifest_file_sha256": _sha256(EVIDENCE_GUIDE_R4C_MANIFEST),
        "manifest_self_sha256": manifest["manifest_sha256"],
        "summary_file_sha256": _sha256(EVIDENCE_GUIDE_R4C_SUMMARY),
        "table_file_sha256": _sha256(EVIDENCE_GUIDE_R4C_TABLE),
        "media_file_sha256": _sha256(EVIDENCE_GUIDE_R4C_MEDIA),
        "source_file_sha256": source_hashes,
    }
    return {
        "available": True,
        "study_version": EVIDENCE_GUIDE_R4C_STUDY_VERSION,
        "state": summary["state"],
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "evaluation_scope": summary["evaluation_scope"],
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_validity_status": "not_measured",
        "ecological_value_status": "not_measured",
        "claim_boundary": summary["claim_boundary"],
        "failed_r4_protocol_incident": r4,
        "failed_r4b_protocol_incident": r4b,
        "failed_r4c_publication_incident": publication_failure,
        "summary": summary,
        "columns": columns,
        "rows": rows,
        "media": {
            "path": str(EVIDENCE_GUIDE_R4C_MEDIA),
            "sha256": _sha256(EVIDENCE_GUIDE_R4C_MEDIA),
            "title": media["title"],
            "alt_text": media["alt_text"],
            "key": EVIDENCE_GUIDE_R4C_MEDIA_KEY,
        },
        "links": [{"label": "Open the R4c recovery run in W&B", "url": run_url}],
        "artifact_references": [artifact],
        "artifact_files": artifact_files,
        "snapshot_sha256": _r4c_json_sha256(snapshot_identity),
        **snapshot_identity,
    }


def _evidence_guide_r4c_markdown(evaluation: dict[str, Any]) -> str:
    r4 = evaluation["failed_r4_protocol_incident"]
    r4b = evaluation["failed_r4b_protocol_incident"]
    boundary = (
        "This tests post-retrieval synthetic evidence only. It does not test retrieval, broad "
        "safety, prompt injection, hydraulic or ecological validity, field performance, or "
        "practitioner usefulness. Production remains **`DISABLE_LIVE_GUIDE`**."
    )
    incidents = (
        f"R4 failed before inference: {r4['failed_closed_count']}/{r4['attempt_count']} schema "
        f"rejections and scientific denominator {r4['scientific_denominator']}. R4b then "
        f"mishandled {r4b['post_response_input_cap_rejections']}/{r4b['provider_invocations']} "
        "completed responses at a local resource guard. R4c reused none of those cells. Its "
        "first W&B Table projection later failed after analysis; the receipt-bound recovery "
        "made zero new model calls and uploaded the unchanged seven-file package."
    )
    if not evaluation["available"]:
        return (
            "### R4c evidence-following study: awaiting the final evidence package\n\n"
            f"{incidents}\n\nNo R4c result or headline number is displayed yet. The recovery "
            "receipt, source package, accessible table, analytical graphic, and hashes must "
            f"agree first.\n\n{boundary}"
        )

    summary = evaluation["summary"]
    validation = {
        row["model_id"]: row
        for row in summary["mechanism_summaries"]
        if row["split"] == "validation"
    }
    granite = validation["ibm-granite/granite-4.1-8b"]
    qwen = validation["Qwen/Qwen3-30B-A3B-Instruct-2507"]
    prose = {row["model_id"]: row for row in summary["prose_cap_summaries"]}
    total = summary["total_resource_summary"]
    example = summary["representative_counterfactual"]
    rule_symbol = "≤" if example["operator"] == "<=" else "≥"
    metric_label = str(example["metric"]).replace("_", " ")
    links = " · ".join(f"[{item['label']}]({item['url']})" for item in evaluation["links"])
    return (
        "### Every trace arrived. The explanations still often ignored the evidence.\n\n"
        f"**{summary['headline']}**\n\n"
        "We gave each model three controlled questions. If the deciding fact crossed a rule, "
        "the answer should change. If only an unrelated fact changed, the answer should stay "
        "fixed. If the needed evidence disappeared, the model should say it could not decide. "
        "The 24 scenarios were split evenly between development and held-out validation. Each "
        "case appeared twice with evidence IDs and order shuffled; both presentations had to "
        "pass.\n\n"
        "#### One pair shows why a trace is not enough\n\n"
        f"In held-out case `{example['case_id']}`, the {metric_label} at "
        f"`{example['object_id']}` had to be {rule_symbol} {example['threshold']:g} "
        f"{example['units']}. The first record was {example['original_value']:g}; the model "
        f"returned the passing plan. We changed only that target to "
        f"{example['changed_value']:g}, across the rule, and it returned the **same plan**. "
        "Both responses satisfied the response schema and both received Weave traces. The "
        "pipeline worked; the evidence-following behavior did not. No answer was released. "
        f"[Original trace]({example['original_trace_url']}) · "
        f"[Changed-evidence trace]({example['changed_trace_url']})\n\n"
        "#### Held-out validation\n\n"
        "| Required behavior, 12 scenarios per model | Granite 4.1 8B | Qwen3 30B A3B |\n"
        "|---|---:|---:|\n"
        f"| Changed when the deciding fact crossed the rule | "
        f"{granite['threshold_sensitivity']['success']}/12 | "
        f"{qwen['threshold_sensitivity']['success']}/12 |\n"
        f"| Ignored an unrelated change | {granite['distractor_specificity']['success']}/12 | "
        f"{qwen['distractor_specificity']['success']}/12 |\n"
        f"| Abstained when the needed evidence was missing | "
        f"{granite['withheld_target_abstention']['success']}/12 | "
        f"{qwen['withheld_target_abstention']['success']}/12 |\n"
        f"| Passed all three checks under both shuffled presentations | "
        f"{granite['complete_mechanism']['success']}/12 | "
        f"{qwen['complete_mechanism']['success']}/12 |\n\n"
        "The same decision logic without an LLM passed **192/192** positive-control cells. "
        "That does not make the control intelligent; it shows the test harness could express "
        "the expected behavior.\n\n"
        f"> All {total['attempt_count']} model calls returned, and all "
        f"{total['trace_acknowledged_count']} were acknowledged by Weave. Neither model "
        "reliably followed the deciding evidence.\n\n"
        "#### More output space fixed the form, not the explanation\n\n"
        f"At 128 output tokens, Granite completed the fixed response form for "
        f"**{prose['ibm-granite/granite-4.1-8b']['caps']['128']['success']}/36** "
        f"case-condition cells and Qwen for **{prose['Qwen/Qwen3-30B-A3B-Instruct-2507']['caps']['128']['success']}/36**. "
        "At 256 tokens both reached **36/36**. More room repaired response-format completion "
        "in this corpus. It did not establish clearer, more useful, or better-grounded prose.\n\n"
        "#### What this changes\n\n"
        "The generated Guide stays off. The deterministic Evidence Rail remains available. "
        "Weave is valuable because it makes each failure inspectable, but observability is not "
        "a correctness certificate. This diagnostic study could disqualify a configuration; "
        "it could not authorize production by itself.\n\n"
        "#### Reproducibility and audit trail\n\n"
        f"The completed calls used "
        f"{total['input_tokens']:,} provider-reported input tokens and "
        f"{total['output_tokens']:,} output tokens. Their pinned-price projection was "
        f"${float(total['cost_usd']):.6f}; client-observed mean latency was "
        f"{float(total['mean_latency_ms'])/1000:.3f} seconds and the maximum was "
        f"{float(total['max_latency_ms'])/1000:.3f} seconds.\n\n"
        f"{incidents}\n\n{boundary}\n\n"
        "This fixed benchmark is evidence to keep these configurations off—not evidence that "
        "language models are universally incapable of this task.\n\n"
        f"*{summary['claim_boundary']}*\n\n{links}"
    )


def _load_realistic_case_study() -> dict[str, Any]:
    """Load the pinned published-output reanalysis and project its report rows."""
    if not REALISTIC_CASE_STUDY.is_file():
        raise RuntimeError(
            "Fehraltorf case-study record is missing; build and verify the published-output "
            "reanalysis before rebuilding the article"
        )
    case_study = _load_json(REALISTIC_CASE_STUDY)
    required_top_level = {
        "schema_version",
        "case_id",
        "title",
        "study",
        "analysis_boundary",
        "sources",
        "method",
        "model_structures",
        "gi_scenarios",
        "cases",
        "comparison_claims",
        "generation",
    }
    if set(case_study) != required_top_level:
        raise RuntimeError("Fehraltorf case-study top-level contract drifted")
    if case_study["schema_version"] != "1.0":
        raise RuntimeError("Fehraltorf case-study schema version is not supported")
    if not isinstance(case_study["case_id"], str) or not case_study["case_id"]:
        raise RuntimeError("Fehraltorf case-study identity is missing")

    study = case_study["study"]
    required_study = {
        "location",
        "paper_doi",
        "dataset_doi",
        "license",
        "source_model_disclaimer",
    }
    if not isinstance(study, dict) or set(study) != required_study:
        raise RuntimeError("Fehraltorf study provenance contract drifted")
    if study["paper_doi"] != "https://doi.org/10.1016/j.watres.2024.121284":
        raise RuntimeError("Fehraltorf paper DOI drifted")
    if study["dataset_doi"] != "https://doi.org/10.25678/0009PE" or not str(
        study["license"]
    ).startswith("CC0-1.0"):
        raise RuntimeError("Fehraltorf dataset identity or license drifted")
    if not study["source_model_disclaimer"]:
        raise RuntimeError("Fehraltorf source-model disclaimer is missing")

    boundary = case_study["analysis_boundary"]
    required_boundary = {
        "analysis_type",
        "fresh_swmm_rerun",
        "reason_rerun_not_possible",
        "authority",
        "not_established",
    }
    if not isinstance(boundary, dict) or set(boundary) != required_boundary:
        raise RuntimeError("Fehraltorf analysis-boundary contract drifted")
    if boundary["fresh_swmm_rerun"] is not False:
        raise RuntimeError("Fehraltorf case must remain a published-output reanalysis")
    if not all(
        isinstance(boundary[name], str) and boundary[name].strip()
        for name in ("analysis_type", "reason_rerun_not_possible", "authority")
    ):
        raise RuntimeError("Fehraltorf analysis boundary is incomplete")
    if not isinstance(boundary["not_established"], list) or not boundary["not_established"]:
        raise RuntimeError("Fehraltorf case must declare what the reanalysis does not establish")

    sources = case_study["sources"]
    if not isinstance(sources, list) or len(sources) < 2:
        raise RuntimeError("Fehraltorf source inventory is incomplete")
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source_id", "url", "sha256", "role"}:
            raise RuntimeError("Fehraltorf source contract drifted")
        if source["source_id"] in source_ids or not source["source_id"]:
            raise RuntimeError("Fehraltorf source identities are invalid")
        source_ids.add(source["source_id"])
        if not str(source["url"]).startswith("https://"):
            raise RuntimeError("Fehraltorf source URL must be HTTPS")
        if re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"])) is None:
            raise RuntimeError("Fehraltorf source hash is invalid")

    method = case_study["method"]
    required_method = {
        "outlet_metric",
        "threshold_lps",
        "year_range",
        "annual_conversion",
        "summary_statistic",
        "baseline_comparison",
    }
    if not isinstance(method, dict) or set(method) != required_method:
        raise RuntimeError("Fehraltorf analysis-method contract drifted")
    year_range = method["year_range"]
    if year_range != {"start": 1990, "end": 2019, "count": 30}:
        raise RuntimeError("Fehraltorf thirty-year analysis window drifted")
    if not math.isclose(float(method["threshold_lps"]), 0.003, abs_tol=1e-12):
        raise RuntimeError("Fehraltorf overflow threshold drifted")

    structures = case_study["model_structures"]
    scenarios = case_study["gi_scenarios"]
    cases = case_study["cases"]
    if not isinstance(structures, list) or len(structures) != 5:
        raise RuntimeError("Fehraltorf case must contain five model structures")
    if not isinstance(scenarios, list) or len(scenarios) != 4:
        raise RuntimeError("Fehraltorf case must contain four GI scenarios")
    structure_by_id: dict[str, dict[str, Any]] = {}
    for structure in structures:
        if not isinstance(structure, dict) or set(structure) != {
            "id",
            "label",
            "included_processes",
        }:
            raise RuntimeError("Fehraltorf model-structure contract drifted")
        if structure["id"] in structure_by_id or not structure["id"]:
            raise RuntimeError("Fehraltorf model-structure identities are invalid")
        if not isinstance(structure["included_processes"], list):
            raise TypeError("Fehraltorf included-process inventory is invalid")
        structure_by_id[structure["id"]] = structure
    scenario_by_id: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {"id", "label"}:
            raise RuntimeError("Fehraltorf GI-scenario contract drifted")
        if scenario["id"] in scenario_by_id or not scenario["id"]:
            raise RuntimeError("Fehraltorf GI-scenario identities are invalid")
        scenario_by_id[scenario["id"]] = scenario
    bioretention_ids = [
        scenario_id
        for scenario_id, scenario in scenario_by_id.items()
        if "bioretention" in str(scenario["label"]).lower()
    ]
    if len(bioretention_ids) != 1:
        raise RuntimeError("Fehraltorf case must identify exactly one bioretention scenario")
    bioretention_id = bioretention_ids[0]

    if not isinstance(cases, list) or len(cases) != 20:
        raise RuntimeError("Fehraltorf case must contain the complete 5 × 4 output matrix")
    cases_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    required_case = {
        "case_id",
        "model_structure_id",
        "gi_scenario_id",
        "csv_source",
        "report_source",
        "year_count",
        "median_annual_cso_m3",
        "reduction_from_same_structure_baseline_percent",
        "qa",
    }
    required_qa = {
        "swmm_version",
        "runoff_continuity_error_percent",
        "groundwater_continuity_error_percent",
        "routing_continuity_error_percent",
        "warning_count",
        "percent_not_converging",
        "max_abs_node_continuity_error_percent",
        "model_disclaimer_present",
    }
    for case in cases:
        if not isinstance(case, dict) or set(case) != required_case:
            raise RuntimeError("Fehraltorf output-case contract drifted")
        pair = (case["model_structure_id"], case["gi_scenario_id"])
        if pair in cases_by_pair or pair[0] not in structure_by_id or pair[1] not in scenario_by_id:
            raise RuntimeError("Fehraltorf output-case matrix contains invalid identities")
        if int(case["year_count"]) != 30 or float(case["median_annual_cso_m3"]) < 0:
            raise RuntimeError("Fehraltorf annual overflow result is invalid")
        for source_key in ("csv_source", "report_source"):
            source = case[source_key]
            if not isinstance(source, dict) or set(source) != {"member", "sha256"}:
                raise RuntimeError("Fehraltorf retained-file provenance contract drifted")
            member = Path(str(source["member"]))
            if member.is_absolute() or ".." in member.parts:
                raise RuntimeError("Fehraltorf retained-file member is unsafe")
            if re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"])) is None:
                raise RuntimeError("Fehraltorf retained-file hash is invalid")
        qa = case["qa"]
        if not isinstance(qa, dict) or set(qa) != required_qa:
            raise RuntimeError("Fehraltorf SWMM-report QA contract drifted")
        if not qa["model_disclaimer_present"]:
            raise RuntimeError("Fehraltorf source-model disclaimer was not found in every report")
        cases_by_pair[pair] = case
    expected_pairs = {
        (structure_id, scenario_id)
        for structure_id in structure_by_id
        for scenario_id in scenario_by_id
    }
    if set(cases_by_pair) != expected_pairs:
        raise RuntimeError("Fehraltorf output-case matrix is incomplete")

    claims = case_study["comparison_claims"]
    if not isinstance(claims, list) or not claims:
        raise RuntimeError("Fehraltorf comparison claims are missing")
    bioretention_claims = [
        claim for claim in claims if claim.get("gi_scenario_id") == bioretention_id
    ]
    if len(bioretention_claims) != 1:
        raise RuntimeError("Fehraltorf bioretention comparison claim is missing")
    bioretention_claim = bioretention_claims[0]
    required_claim = {
        "claim_id",
        "gi_scenario_id",
        "min_reduction_percent",
        "max_reduction_percent",
        "spread_percentage_points",
        "min_structure_id",
        "max_structure_id",
        "limitation",
    }
    if set(bioretention_claim) != required_claim:
        raise RuntimeError("Fehraltorf comparison-claim contract drifted")
    bioretention_reductions = {
        structure_id: float(
            cases_by_pair[(structure_id, bioretention_id)][
                "reduction_from_same_structure_baseline_percent"
            ]
        )
        for structure_id in structure_by_id
    }
    minimum = min(bioretention_reductions.items(), key=lambda item: item[1])
    maximum = max(bioretention_reductions.items(), key=lambda item: item[1])
    if (
        bioretention_claim["min_structure_id"] != minimum[0]
        or bioretention_claim["max_structure_id"] != maximum[0]
        or not math.isclose(
            float(bioretention_claim["min_reduction_percent"]), minimum[1], abs_tol=1e-9
        )
        or not math.isclose(
            float(bioretention_claim["max_reduction_percent"]), maximum[1], abs_tol=1e-9
        )
        or not math.isclose(
            float(bioretention_claim["spread_percentage_points"]),
            maximum[1] - minimum[1],
            abs_tol=1e-9,
        )
    ):
        raise RuntimeError("Fehraltorf bioretention sensitivity claim is internally inconsistent")
    combined_ids = [
        structure_id
        for structure_id, structure in structure_by_id.items()
        if "combined" in str(structure["label"]).lower()
    ]
    if len(combined_ids) != 1:
        raise RuntimeError("Fehraltorf case must identify exactly one combined model structure")
    combined_case = cases_by_pair[(combined_ids[0], bioretention_id)]
    groundwater_error = combined_case["qa"]["groundwater_continuity_error_percent"]
    if groundwater_error is None or not math.isclose(
        float(groundwater_error), -54.273, abs_tol=1e-6
    ):
        raise RuntimeError("Fehraltorf combined-case groundwater continuity finding drifted")

    table_rows: list[dict[str, Any]] = []
    for structure in structures:
        structure_id = structure["id"]
        structure_cases = [
            case
            for (observed_structure, _), case in cases_by_pair.items()
            if observed_structure == structure_id
        ]
        baseline_cases = [
            case
            for case in structure_cases
            if case["reduction_from_same_structure_baseline_percent"] is None
        ]
        if len(baseline_cases) != 1:
            raise RuntimeError("Fehraltorf structure must contain exactly one no-GI baseline")
        baseline = baseline_cases[0]
        bioretention = cases_by_pair[(structure_id, bioretention_id)]
        table_rows.append(
            {
                "model_structure": structure["label"],
                "included_processes": ", ".join(structure["included_processes"]),
                "baseline_median_annual_cso_m3": baseline["median_annual_cso_m3"],
                "bioretention_median_annual_cso_m3": bioretention["median_annual_cso_m3"],
                "modeled_reduction_percent": bioretention[
                    "reduction_from_same_structure_baseline_percent"
                ],
                "runoff_continuity_error_percent": bioretention["qa"][
                    "runoff_continuity_error_percent"
                ],
                "groundwater_continuity_error_percent": bioretention["qa"][
                    "groundwater_continuity_error_percent"
                ],
                "routing_continuity_error_percent": bioretention["qa"][
                    "routing_continuity_error_percent"
                ],
                "warning_count": bioretention["qa"]["warning_count"],
                "model_disclaimer_present": bioretention["qa"]["model_disclaimer_present"],
            }
        )
    return {
        **case_study,
        "sha256": _sha256(REALISTIC_CASE_STUDY),
        "bioretention_claim": bioretention_claim,
        "combined_bioretention_case": combined_case,
        "table_rows": table_rows,
    }


def _realistic_case_study_markdown(case_study: dict[str, Any], publication: dict[str, Any]) -> str:
    claim = case_study["bioretention_claim"]
    combined = case_study["combined_bioretention_case"]
    return (
        "### Independent reanalysis of published Fehraltorf outputs\n\n"
        f"Across the five published model structures, the recomputed bioretention reduction "
        f"ranges from **{claim['min_reduction_percent']:.1f}% to "
        f"{claim['max_reduction_percent']:.1f}%**—a "
        f"**{claim['spread_percentage_points']:.1f}-percentage-point** span. The "
        f"combined-process report records a **"
        f"{combined['qa']['groundwater_continuity_error_percent']:.3f}% groundwater "
        "continuity error**. The table keeps the modeled benefit and its execution-quality "
        "context together.\n\n"
        f"[Open the source dataset]({case_study['study']['dataset_doi']}) · "
        f"[Read the associated paper]({case_study['study']['paper_doi']}) · "
        f"[Open the scientific W&B run]({publication['run_url']}) · "
        f"[Inspect the computation trace in Weave]({publication['weave_trace_url']})\n\n"
        "The cloud links above come from a hash-bound receipt after the exact Restricted "
        "destination, artifact, run association, trace readback, and run-file privacy audit "
        "were verified.\n\n"
        "*Published-output reanalysis, not a fresh SWMM rerun or field validation.*"
    )


def _load_realistic_case_study_media(case_study: dict[str, Any]) -> dict[str, Any]:
    if not REALISTIC_CASE_STUDY_CHART_MANIFEST.is_file():
        raise RuntimeError(
            "Fehraltorf case-study media manifest is missing; build the deterministic chart "
            "before rebuilding the article"
        )
    manifest = _load_json(REALISTIC_CASE_STUDY_CHART_MANIFEST)
    required = {
        "schema_version",
        "media_key",
        "title",
        "question",
        "takeaway",
        "alt_text",
        "source",
        "claim_boundary",
        "comparison",
        "table",
        "outputs",
    }
    if set(manifest) != required or manifest["schema_version"] != "1.0":
        raise RuntimeError("Fehraltorf case-study media contract drifted")
    if manifest["media_key"] != "fehraltorf_model_structure_sensitivity":
        raise RuntimeError("Fehraltorf case-study media identity drifted")
    if not all(
        isinstance(manifest[name], str) and manifest[name].strip()
        for name in (
            "title",
            "question",
            "takeaway",
            "alt_text",
        )
    ):
        raise RuntimeError("Fehraltorf case-study media copy is incomplete")
    source = manifest["source"]
    if source != {
        "case_study_path": "benchmarks/realistic_fehraltorf/v1/case-study.json",
        "case_study_sha256": case_study["sha256"],
        "dataset_doi": "10.25678/0009PE",
    }:
        raise RuntimeError("Fehraltorf case-study media source identity drifted")
    claim = case_study["bioretention_claim"]
    comparison = manifest["comparison"]
    expected_comparison = {
        "scenario": "bioretention",
        "year_count": 30,
        "min_structure_id": claim["min_structure_id"],
        "max_structure_id": claim["max_structure_id"],
        "spread_percentage_points": claim["spread_percentage_points"],
    }
    if comparison != expected_comparison:
        raise RuntimeError("Fehraltorf chart comparison drifted from the case-study claims")
    boundary = manifest["claim_boundary"]
    if (
        not isinstance(boundary, dict)
        or not boundary.get("establishes")
        or not boundary.get("does_not_establish")
        or boundary.get("limitation_from_source") != claim["limitation"]
    ):
        raise RuntimeError("Fehraltorf chart claim boundary is incomplete")
    outputs = manifest["outputs"]
    if not isinstance(outputs, dict) or set(outputs) != {"png", "csv"}:
        raise RuntimeError("Fehraltorf chart output inventory drifted")
    png = outputs["png"]
    csv = outputs["csv"]
    if png != {
        "path": REALISTIC_CASE_STUDY_CHART.name,
        "sha256": png.get("sha256"),
        "width": 1920,
        "height": 1080,
    }:
        raise RuntimeError("Fehraltorf chart dimensions or path drifted")
    if csv.get("path") != REALISTIC_CASE_STUDY_CHART_CSV.name or int(csv.get("rows", -1)) != 5:
        raise RuntimeError("Fehraltorf chart CSV contract drifted")
    for path, output in ((REALISTIC_CASE_STUDY_CHART, png), (REALISTIC_CASE_STUDY_CHART_CSV, csv)):
        if (
            not path.is_file()
            or re.fullmatch(r"[0-9a-f]{64}", str(output.get("sha256"))) is None
            or _sha256(path) != output["sha256"]
        ):
            raise RuntimeError(f"Fehraltorf generated media hash drifted: {path.name}")
    table = manifest["table"]
    if not isinstance(table, list) or len(table) != 5:
        raise RuntimeError("Fehraltorf chart must retain a five-row accessible data projection")
    chart_rows = {row.get("model_structure_id"): row for row in table if isinstance(row, dict)}
    if len(chart_rows) != 5:
        raise RuntimeError("Fehraltorf chart table identities are invalid")
    case_rows = {
        case["model_structure_id"]: case
        for case in case_study["cases"]
        if case["gi_scenario_id"] == claim["gi_scenario_id"]
    }
    for structure_id, case in case_rows.items():
        row = chart_rows.get(structure_id)
        if row is None or any(
            row.get(name) != value
            for name, value in (
                (
                    "reduction_from_same_structure_baseline_percent",
                    case["reduction_from_same_structure_baseline_percent"],
                ),
                (
                    "groundwater_continuity_error_percent",
                    case["qa"]["groundwater_continuity_error_percent"],
                ),
                (
                    "routing_continuity_error_percent",
                    case["qa"]["routing_continuity_error_percent"],
                ),
                ("runoff_continuity_error_percent", case["qa"]["runoff_continuity_error_percent"]),
            )
        ):
            raise RuntimeError("Fehraltorf chart table drifted from the pinned output cases")
    return {
        **manifest,
        "manifest_sha256": _sha256(REALISTIC_CASE_STUDY_CHART_MANIFEST),
        "png_path": str(REALISTIC_CASE_STUDY_CHART),
        "csv_path": str(REALISTIC_CASE_STUDY_CHART_CSV),
        "report_media_key": REALISTIC_CASE_STUDY_CHART_KEY,
    }


def _load_realistic_case_study_publication(
    entity: str,
    project: str,
    case_study: dict[str, Any],
    media: dict[str, Any],
) -> dict[str, Any]:
    if not REALISTIC_CASE_STUDY_PUBLICATION.is_file():
        raise RuntimeError(
            "Fehraltorf W&B/Weave publication receipt is missing; publish and independently "
            "verify the scientific reanalysis before rebuilding the article"
        )
    receipt = _load_json(REALISTIC_CASE_STUDY_PUBLICATION)
    required = {
        "schema_version",
        "entity",
        "project",
        "project_id",
        "project_access",
        "case_id",
        "case_sha256",
        "publication_digest",
        "run_id",
        "run_url",
        "artifact",
        "weave_root_call_id",
        "weave_trace_id",
        "weave_trace_url",
        "weave_readback_verified",
        "wandb_run_association_verified",
        "wandb_run_association",
        "fresh_swmm_rerun",
        "source_authority",
        "bounded_claim_sha256",
        "chart_sha256",
        "table_sha256",
        "run_file_privacy_audit",
    }
    if set(receipt) != required or receipt["schema_version"] != "1.0":
        raise RuntimeError("Fehraltorf scientific-publication receipt contract drifted")
    if receipt["entity"] != entity or receipt["project"] != project:
        raise RuntimeError("Fehraltorf publication destination does not match the article")
    if receipt["project_access"] != "RESTRICTED" or not receipt["project_id"]:
        raise RuntimeError(
            "Fehraltorf publication was not verified in the exact Restricted project"
        )
    if (
        receipt["case_id"] != case_study["case_id"]
        or receipt["case_sha256"] != case_study["sha256"]
    ):
        raise RuntimeError("Fehraltorf publication case identity drifted")
    if receipt["fresh_swmm_rerun"] is not False:
        raise RuntimeError("Fehraltorf publication must remain a published-output reanalysis")
    if receipt["source_authority"] != case_study["analysis_boundary"]["authority"]:
        raise RuntimeError("Fehraltorf publication source authority drifted")
    if receipt["chart_sha256"] != media["outputs"]["png"]["sha256"]:
        raise RuntimeError("Fehraltorf publication chart hash drifted")
    if receipt["table_sha256"] != media["outputs"]["csv"]["sha256"]:
        raise RuntimeError("Fehraltorf publication table hash drifted")
    bounded_claim_sha256 = receipt["bounded_claim_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", str(bounded_claim_sha256)) is None:
        raise RuntimeError("Fehraltorf bounded-claim hash is invalid")
    expected_digest = _json_sha256(
        {
            "case_sha256": case_study["sha256"],
            "media_receipt_sha256": media["manifest_sha256"],
            "chart_sha256": media["outputs"]["png"]["sha256"],
            "table_sha256": media["outputs"]["csv"]["sha256"],
            "bounded_claim_sha256": bounded_claim_sha256,
        }
    )
    if receipt["publication_digest"] != expected_digest:
        raise RuntimeError("Fehraltorf scientific-publication digest drifted")
    expected_run_id = hashlib.sha256(
        f"fehraltorf-reanalysis-v1|{entity}|{project}|{expected_digest}".encode()
    ).hexdigest()[:24]
    if receipt["run_id"] != expected_run_id:
        raise RuntimeError("Fehraltorf publication run identity drifted")

    def exact_url(value: Any, *, label: str, path: str) -> str:
        url = _actual_publication_url(value, label=label)
        parsed = urlparse(url)
        if parsed.path != path or parsed.query or parsed.params:
            raise RuntimeError(f"{label} does not match its exact receipt-backed path")
        return url

    run_url = exact_url(
        receipt["run_url"],
        label="Fehraltorf W&B run URL",
        path=f"/{entity}/{project}/runs/{expected_run_id}",
    )
    root_call_id = receipt["weave_root_call_id"]
    trace_id = receipt["weave_trace_id"]
    if any(
        re.fullmatch(r"[0-9a-f-]{36}", str(value)) is None for value in (root_call_id, trace_id)
    ):
        raise RuntimeError("Fehraltorf Weave call or trace identity is invalid")
    trace_url = exact_url(
        receipt["weave_trace_url"],
        label="Fehraltorf Weave trace URL",
        path=f"/{entity}/{project}/r/call/{root_call_id}",
    )
    if (
        receipt["weave_readback_verified"] is not True
        or receipt["wandb_run_association_verified"] is not True
    ):
        raise RuntimeError("Fehraltorf W&B/Weave readback was not verified")
    if receipt["wandb_run_association"] != f"{entity}/{project}/{expected_run_id}":
        raise RuntimeError("Fehraltorf Weave trace is not associated with the exact W&B run")
    expected_artifact = re.compile(
        rf"{re.escape(entity)}/{re.escape(project)}/"
        r"fehraltorf-published-output-reanalysis:v[0-9]+"
    )
    if expected_artifact.fullmatch(str(receipt["artifact"])) is None:
        raise RuntimeError("Fehraltorf publication artifact name or destination drifted")
    if receipt["run_file_privacy_audit"] != {"audited_runs": 1, "prohibited_files": 0}:
        raise RuntimeError("Fehraltorf publication did not pass its run-file privacy audit")
    return {
        **receipt,
        "receipt_sha256": _sha256(REALISTIC_CASE_STUDY_PUBLICATION),
        "run_url": run_url,
        "weave_trace_url": trace_url,
    }


def _evaluation_markdown(evaluation: dict[str, Any]) -> str:
    logical_invocations = evaluation["experiment_scope"]["candidate_campaign"][
        "logical_case_invocations"
    ]
    base = (
        f"### Real candidate campaign and release decision\n\n"
        f"Dataset `{evaluation['dataset_id']}:{evaluation['dataset_version']}` contains "
        f"{evaluation['row_count']} synthetic CC0 cases. The deterministic scorer version is "
        f"`{evaluation['scorer_version']}`. The campaign made {logical_invocations} logical "
        "case invocations: one schema-constrained evidence-selection invocation for each "
        "case and candidate. This is not a count of HTTP transport attempts."
    )
    decision = (
        f"\n\nThe hash-validated campaign decision is **`{evaluation['decision']}`**. "
        "Neither candidate passed every deterministic and practitioner gate, so no model is "
        "selected and the live Guide remains disabled."
    )
    header = (
        "\n\n| Candidate | Full pass | Scorable | Numeric/unit | Evidence coverage | "
        "Limitations | Read-only/actions | Refusal mode / complete safe | "
        "Tokens (in/out/total) | Latency median/p95 |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = []
    for candidate in evaluation["candidates"]:
        summary = candidate["summary"]
        metrics = candidate["metrics"]
        usage = candidate["usage"]
        latency = candidate["latency_ms"]
        rows.append(
            "| `{}` | {}/{} | {}/{} | {:.2%} | {:.2%} | {:.2%} | {:.2%} | "
            "{}/{} / {}/{} | {:,}/{:,}/{:,} | {:,.0f}/{:,.0f} ms |".format(
                candidate["model_id"],
                summary["passed"],
                summary["total"],
                summary["evaluated"],
                summary["total"],
                metrics["numeric_unit_fidelity"],
                metrics["evidence_coverage"],
                metrics["limitation_coverage"],
                metrics["read_only_action_compliance"],
                metrics["abstention_passed"],
                metrics["abstention_total"],
                metrics["complete_abstention_passed"],
                metrics["abstention_total"],
                usage["input_tokens"],
                usage["output_tokens"],
                usage["total_tokens"],
                latency["median"],
                latency["p95"],
            )
        )
    comparison = header + "\n" + "\n".join(rows)
    authority = (
        "\n\nBoth candidates had 100% evidence-ID validity on the outputs they produced; "
        "that is not the same as all-case evidence coverage. "
        + " ".join(
            f"`{candidate['model_id']}` omitted "
            f"{candidate['metrics']['missing_required_actions']} required actions and had "
            f"{candidate['metrics']['unauthorized_action_violations']} explicit "
            "unauthorized-action violations."
            for candidate in evaluation["candidates"]
        )
    )
    granite, gpt_oss = evaluation["candidates"]
    protected_cases = (
        " Granite completely handled the prompt-injection case but not the fabricated-run "
        "case. GPT-OSS completely handled neither protected case."
        if granite["metrics"]["prompt_injection_complete_pass"]
        and not granite["metrics"]["fabricated_run_complete_pass"]
        and not gpt_oss["metrics"]["prompt_injection_complete_pass"]
        and not gpt_oss["metrics"]["fabricated_run_complete_pass"]
        else ""
    )
    boundary = (
        " The refusal count checks only whether an unsupported prompt entered refusal mode; it "
        "does not mean the complete refusal passed its evidence, limitation, and action checks. "
        "Zero explicit unauthorized-action errors do not clear the authority gate: missing "
        "outputs, required actions, limitations, and practitioner ratings still fail closed. "
        "Practitioner usefulness was not measured and no controlled practitioner approval was "
        "supplied. Token totals cover successful provider responses; latency covers all case "
        "attempts. Energy use, kWh, and CO2e were not measured. "
        "This one-call synthetic selection campaign does not establish production service "
        "reliability or validate the runtime Guide's two-call tool loop."
    )
    published = [candidate for candidate in evaluation["candidates"] if candidate["publication"]]
    if not published:
        links = (
            "\n\nNo matching Restricted-project W&B/Weave publication receipt is present. The "
            "report therefore shows no live candidate-evaluation link."
        )
    else:
        lines = [
            (
                "\n\nVerified receipt-backed W&B/Weave campaign evidence (both evaluations failed; "
                "publication is not approval):"
            ),
            f"- Campaign object: `{evaluation['campaign_publication']['campaign_evidence_ref']}`",
        ]
        for candidate in published:
            publication = candidate["publication"]
            lines.append(
                f"- `{candidate['model_id']}`: "
                f"[evaluation]({publication['evaluation_url']}) · dataset "
                f"`{publication['dataset_ref']}`"
            )
            lines.extend(
                f"  - [Trace {index}]({url})"
                for index, url in enumerate(publication["trace_urls"], start=1)
            )
        links = "\n".join(lines)
    probe = evaluation["agent_probe"]
    probe_turns = [
        turn for conversation in probe["conversations"] for turn in conversation["turns"]
    ]
    acknowledged = sum(
        1
        for turn in probe_turns
        if turn["root_status"] == "ERROR" and turn["trace_id"] and turn["root_span_id"]
    )
    tool_observed = sum(1 for turn in probe_turns if "tool_call" in turn["message_types"])
    released = sum(1 for turn in probe_turns if turn["answer_released"])
    failure_codes = sorted(
        {
            turn["error_code"]
            for conversation in probe["conversations"]
            for turn in conversation["turns"]
        }
    )
    probe_lines = [
        (
            "\n\nA separate synthetic integration probe explicitly overrode the production gate in "
            f"its injected test harness. All {probe['turn_count']} actual Granite turns across "
            f"{probe['conversation_count']} Weave Agents conversations were independently read "
            "back with root `ERROR` status, tool activity, and no released answer. The local "
            f"rejection codes were `{', '.join(failure_codes)}`. The second ALT-017 question "
            "remained in the first conversation; ALT-207 began a clean conversation. This is "
            "fail-closed integration evidence, not a passing model result, and the product gate "
            "remained `DISABLE_LIVE_GUIDE`."
        ),
        (
            "\n\n| Runtime-agent behavior check | Observed result |\n"
            "|---|---|\n"
            f"| Weave acknowledgement and independent readback | {acknowledged}/{len(probe_turns)} turns |\n"
            f"| Evidence-tool activity visible in the turn | {tool_observed}/{len(probe_turns)} turns |\n"
            "| Multi-turn continuity | Two ALT-017 turns shared one conversation |\n"
            "| Evidence-subject isolation | ALT-207 started a separate conversation |\n"
            "| Exact cloud tool-parent relationship | Not asserted; the SDK omitted the routing parent span |\n"
            f"| Generated answers released after local validation | {released}/{len(probe_turns)} turns |\n"
            f"| Fail-closed rejections | {probe['rejected_count']}/{len(probe_turns)} turns |"
        ),
    ]
    probe_lines.extend(
        f"- [{conversation['anchor']} rejected Agents conversation]({conversation['url']})"
        for conversation in probe["conversations"]
    )
    return (
        base
        + decision
        + comparison
        + authority
        + protected_cases
        + boundary
        + links
        + "\n".join(probe_lines)
    )


def _publication_digest(
    article_sha256: str,
    media_spec_sha256: str,
    benchmark_media_sha256: str,
    animation_media_sha256: str,
    animation_manifest_sha256: str,
    animation_publication_id: str,
    benchmark_publication_sha256: str,
    research_resources_sha256: str,
    evidence_guide_evaluation: dict[str, Any],
    evidence_guide_v2_evaluation: dict[str, Any],
    evidence_guide_v2_architecture_development: dict[str, Any],
    evidence_guide_r4c_grounding: dict[str, Any],
    realistic_case_study: dict[str, Any],
    realistic_case_study_media: dict[str, Any],
    realistic_case_study_publication: dict[str, Any],
    clips: list[dict[str, Any]],
    animations: list[dict[str, Any]],
) -> str:
    payload = {
        "report_template_version": REPORT_TEMPLATE_VERSION,
        "article_sha256": article_sha256,
        "media_spec_sha256": media_spec_sha256,
        "benchmark_media_sha256": benchmark_media_sha256,
        "animation_media_sha256": animation_media_sha256,
        "animation_manifest_sha256": animation_manifest_sha256,
        "animation_publication_id": animation_publication_id,
        "benchmark_publication_sha256": benchmark_publication_sha256,
        "research_resources_sha256": research_resources_sha256,
        "evidence_guide_dataset_sha256": evidence_guide_evaluation["dataset_sha256"],
        "evidence_guide_release_gate_sha256": evidence_guide_evaluation["release_gate_sha256"],
        "evidence_guide_campaign_sha256": evidence_guide_evaluation["campaign_manifest_sha256"],
        "evidence_guide_publication_state": evidence_guide_evaluation["publication_state"],
        "evidence_guide_v2_snapshot_sha256": evidence_guide_v2_evaluation["snapshot_sha256"],
        "evidence_guide_v2_preflight_audit_sha256": evidence_guide_v2_evaluation[
            "preflight_audit_sha256"
        ],
        "evidence_guide_v2_state": evidence_guide_v2_evaluation["state"],
        "evidence_guide_v2_decision": evidence_guide_v2_evaluation["decision"],
        "evidence_guide_v2_architecture_development_snapshot_sha256": (
            evidence_guide_v2_architecture_development["snapshot_sha256"]
        ),
        "evidence_guide_v2_architecture_development_available": (
            evidence_guide_v2_architecture_development["available"]
        ),
        "evidence_guide_r4c_grounding_snapshot_sha256": evidence_guide_r4c_grounding[
            "snapshot_sha256"
        ],
        "evidence_guide_r4c_grounding_available": evidence_guide_r4c_grounding["available"],
        "realistic_case_study_sha256": realistic_case_study["sha256"],
        "realistic_case_study_media_manifest_sha256": realistic_case_study_media["manifest_sha256"],
        "realistic_case_study_chart_sha256": realistic_case_study_media["outputs"]["png"]["sha256"],
        "realistic_case_study_publication_receipt_sha256": realistic_case_study_publication[
            "receipt_sha256"
        ],
        "clips": [{"key": clip["key"], "sha256": clip["sha256"]} for clip in clips],
        "animations": [
            {
                "key": animation["key"],
                "sha256": animation["sha256"],
                "poster_sha256": animation["poster_sha256"],
            }
            for animation in animations
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _load_animation_receipt(
    entity: str, project: str, animation_manifest_sha256: str
) -> dict[str, Any]:
    if not ANIMATION_RECEIPT.is_file():
        raise RuntimeError(
            "report-animation W&B receipt is missing; publish and verify the five-film package "
            "before rebuilding the article report"
        )
    receipt = _load_json(ANIMATION_RECEIPT)
    required = (
        "schema_version",
        "entity",
        "project",
        "project_access",
        "publication_id",
        "run_id",
        "run_url",
        "artifact",
        "animation_manifest_sha256",
        "clip_count",
        "run_file_privacy_audit",
    )
    if any(receipt.get(name) in (None, "") for name in required):
        raise RuntimeError("report-animation W&B receipt is incomplete")
    if receipt["entity"] != entity or receipt["project"] != project:
        raise RuntimeError("report-animation publication destination does not match the article")
    if str(receipt["project_access"]).upper() != "RESTRICTED":
        raise RuntimeError("report-animation publication was not verified in a RESTRICTED project")
    if receipt["animation_manifest_sha256"] != animation_manifest_sha256:
        raise RuntimeError("report-animation manifest identity drifted after W&B publication")
    if int(receipt["clip_count"]) != 5:
        raise RuntimeError("report-animation publication does not contain exactly five films")
    audit = receipt["run_file_privacy_audit"]
    if not isinstance(audit, dict) or int(audit.get("prohibited_files", -1)) != 0:
        raise RuntimeError("report-animation W&B run did not pass its run-file privacy audit")
    return receipt


def build_plan(entity: str, project: str) -> dict[str, Any]:
    media_spec = _load_json(MEDIA_SPEC)
    benchmark_media = _load_json(BENCHMARK_MEDIA_SPEC)
    animation_media = _load_json(ANIMATION_MEDIA_SPEC)
    benchmark_publication = _load_json(BENCHMARK_PUBLICATION)
    clip_manifest = _validate_generated_media()
    _, animations = _validate_animation_media(animation_media)
    animation_manifest_sha256 = _sha256(ANIMATION_MANIFEST)
    animation_receipt = _load_animation_receipt(entity, project, animation_manifest_sha256)
    evidence_guide_evaluation = _load_evidence_guide_evaluation(entity, project)
    evidence_guide_v2_evaluation = _load_evidence_guide_v2_evaluation()
    evidence_guide_v2_architecture_development = (
        _load_evidence_guide_v2_architecture_development()
    )
    evidence_guide_r4c_grounding = _load_evidence_guide_r4c_grounding()
    realistic_case_study = _load_realistic_case_study()
    realistic_case_study_media = _load_realistic_case_study_media(realistic_case_study)
    realistic_case_study_publication = _load_realistic_case_study_publication(
        entity,
        project,
        realistic_case_study,
        realistic_case_study_media,
    )
    title, description, body = _article_parts()
    if title != media_spec["report"]["title"] or description != media_spec["report"]["description"]:
        raise ValueError("article title or deck drifted from the report specification")
    clips = clip_manifest["clips"]
    figures = {item["key"]: item for item in benchmark_publication["figures"]}
    charts = []
    for chart in benchmark_media["charts"]:
        figure = figures.get(chart["figure_key"])
        if figure is None:
            raise ValueError(f"benchmark figure is missing: {chart['figure_key']}")
        local_path = REPOSITORY_ROOT / "dist" / "benchmark-publication" / figure["relative_path"]
        if not local_path.is_file() or _sha256(local_path) != figure["sha256"]:
            raise ValueError(f"benchmark figure hash drifted: {chart['figure_key']}")
        charts.append({**chart, "sha256": figure["sha256"], "local_path": str(local_path)})
    segments = _content_segments(body, clips, charts, animations)
    article_sha256 = _sha256(ARTICLE)
    media_spec_sha256 = _sha256(MEDIA_SPEC)
    benchmark_media_sha256 = _sha256(BENCHMARK_MEDIA_SPEC)
    animation_media_sha256 = _sha256(ANIMATION_MEDIA_SPEC)
    benchmark_publication_sha256 = _sha256(BENCHMARK_PUBLICATION)
    research_resources_sha256 = _sha256(RESEARCH_RESOURCES)
    digest = _publication_digest(
        article_sha256,
        media_spec_sha256,
        benchmark_media_sha256,
        animation_media_sha256,
        animation_manifest_sha256,
        animation_receipt["publication_id"],
        benchmark_publication_sha256,
        research_resources_sha256,
        evidence_guide_evaluation,
        evidence_guide_v2_evaluation,
        evidence_guide_v2_architecture_development,
        evidence_guide_r4c_grounding,
        realistic_case_study,
        realistic_case_study_media,
        realistic_case_study_publication,
        clips,
        animations,
    )
    run_id = hashlib.sha256(f"gi-article-v2|{entity}|{project}|{digest}".encode()).hexdigest()[:24]
    plan = {
        "schema_version": "1.0",
        "report_template_version": REPORT_TEMPLATE_VERSION,
        "entity": entity,
        "project": project,
        "title": title,
        "description": description,
        "report_width": media_spec["report"]["width"],
        "media_kind": media_spec["media_kind"],
        "article_sha256": article_sha256,
        "media_spec_sha256": media_spec_sha256,
        "benchmark_media_sha256": benchmark_media_sha256,
        "animation_media_kind": animation_media["media_kind"],
        "animation_media_sha256": animation_media_sha256,
        "animation_manifest_sha256": animation_manifest_sha256,
        "animation_publication_id": animation_receipt["publication_id"],
        "animation_run_id": animation_receipt["run_id"],
        "animation_run_url": animation_receipt["run_url"],
        "animation_artifact": animation_receipt["artifact"],
        "benchmark_publication_sha256": benchmark_publication_sha256,
        "research_resources_sha256": research_resources_sha256,
        "evidence_guide_evaluation": evidence_guide_evaluation,
        "evidence_guide_v2_evaluation": evidence_guide_v2_evaluation,
        "evidence_guide_v2_architecture_development": (
            evidence_guide_v2_architecture_development
        ),
        "evidence_guide_r4c_grounding": evidence_guide_r4c_grounding,
        "realistic_case_study": realistic_case_study,
        "realistic_case_study_media": realistic_case_study_media,
        "realistic_case_study_publication": realistic_case_study_publication,
        "benchmark_publication_id": benchmark_publication["publication_id"],
        "benchmark_dataset_id": benchmark_publication["dataset_id"],
        "publication_digest": digest,
        "run_id": run_id,
        "run_name": f"gi-article-{digest[:8]}",
        "clip_count": len(clips),
        "animation_clip_count": len(animations),
        "benchmark_chart_count": len(charts),
        "clips": [
            {
                "key": clip["key"],
                "marker": clip["marker"],
                "section": clip["section"],
                "layout": clip["layout"],
                "report_copy": clip["report_copy"],
                "caption": clip["caption"],
                "output": clip["output"],
                "poster": clip["poster"],
                "sha256": clip["sha256"],
                "poster_sha256": clip["poster_sha256"],
                "duration_seconds": clip["duration_seconds"],
                "frame_count": clip["frame_count"],
            }
            for clip in clips
        ],
        "animations": [
            {
                "key": animation["key"],
                "marker": animation["marker"],
                "title": animation["title"],
                "section": animation["section"],
                "placement": animation["placement"],
                "layout": animation["layout"],
                "report_copy": animation["report_copy"],
                "caption": animation["caption"],
                "establishes": animation["establishes"],
                "does_not_establish": animation["does_not_establish"],
                "output": animation["output"],
                "poster": animation["poster"],
                "sha256": animation["sha256"],
                "poster_sha256": animation["poster_sha256"],
                "duration_seconds": animation["duration_seconds"],
                "frame_count": animation["frame_count"],
                "claim_ids": animation["claim_ids"],
            }
            for animation in animations
        ],
        "charts": charts,
        "segments": segments,
        "report_block_count": 1
        + len([s for s in segments if s["kind"] == "markdown"])
        + len(clips)
        + len([clip for clip in clips if clip["layout"] == "full_width"])
        + 2 * len(animations)
        + 2 * len(charts)
        + 2
        * len(
            [
                s
                for s in segments
                if s["kind"] == "evaluation"
                and s["key"]
                not in {EVIDENCE_GUIDE_V2_ARCH_DEV_KEY, EVIDENCE_GUIDE_R4C_KEY}
            ]
        )
        + (
            (3 if evidence_guide_v2_architecture_development["available"] else 1)
            if any(
                s["kind"] == "evaluation"
                and s["key"] == EVIDENCE_GUIDE_V2_ARCH_DEV_KEY
                for s in segments
            )
            else 0
        )
        + (3 if evidence_guide_r4c_grounding["available"] else 1)
        + 3 * len([s for s in segments if s["kind"] == "case_study"])
        + 1,
    }
    _write_json_atomic(PUBLICATION_PLAN, plan)
    return plan


def _defensive_wandb_environment() -> None:
    cloud_root = ROOT / "dist" / "wandb"
    for name, child in (
        ("WANDB_DIR", "run"),
        ("WANDB_CACHE_DIR", "cache"),
        ("WANDB_ARTIFACT_DIR", "artifacts"),
    ):
        path = cloud_root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    defaults = {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
    }
    for name, value in defaults.items():
        os.environ[name] = value


def _project_policy(api: Any, entity: str, project: str) -> dict[str, str] | None:
    """Read the project's exact cloud access policy without relying on lazy SDK attrs."""
    response = api._service_api.execute_graphql(
        PROJECT_POLICY_GQL,
        {"name": project, "entity": entity},
    )
    record = response.get("project")
    if record is None:
        return None
    required = ("id", "name", "entityName", "access")
    if any(not record.get(field) for field in required):
        raise RuntimeError("W&B project policy response was incomplete; nothing was uploaded")
    if record["name"] != project or record["entityName"] != entity:
        raise RuntimeError("W&B project policy response did not match the requested destination")
    return {field: str(record[field]) for field in required}


def _create_restricted_project(api: Any, entity: str, project: str) -> None:
    """Create the exact destination with Restricted visibility in the same mutation."""
    from wandb.apis._generated import CREATE_PROJECT_GQL

    response = api._service_api.execute_graphql(
        CREATE_PROJECT_GQL,
        {"input": {"name": project, "entityName": entity, "access": "RESTRICTED"}},
    )
    result = response.get("result") or {}
    record = result.get("project") or result.get("model") or {}
    if str(record.get("access") or "").upper() != "RESTRICTED":
        raise RuntimeError("W&B did not confirm Restricted access during project creation")


def _get_project(api: Any, entity: str, project: str, allow_create: bool) -> Any:
    policy = _project_policy(api, entity, project)
    if policy is None:
        if not allow_create:
            raise RuntimeError(
                "W&B project does not exist; rerun with --allow-create-project only after reviewing "
                "the exact entity/project destination"
            )
        _create_restricted_project(api, entity, project)
        for _ in range(15):
            policy = _project_policy(api, entity, project)
            if policy is not None:
                break
            time.sleep(1)
    if policy is None:
        raise RuntimeError("W&B project was not accessible after creation")
    access = policy["access"].upper()
    if access != "RESTRICTED":
        raise RuntimeError(f"W&B project access is {access}, not RESTRICTED; nothing was uploaded")
    selected = api.project(project, entity=entity)
    if selected.id != policy["id"]:
        raise RuntimeError("W&B project identity changed during policy verification")
    return selected


def _assert_restricted_project_unchanged(
    api: Any, entity: str, project: str, expected_project_id: str
) -> None:
    """Fail closed if the destination policy or identity changes during publication."""
    policy = _project_policy(api, entity, project)
    if policy is None:
        raise RuntimeError("W&B project disappeared after report publication")
    if policy["access"].upper() != "RESTRICTED":
        raise RuntimeError(
            f"W&B project access changed to {policy['access']} during report publication"
        )
    if policy["id"] != expected_project_id:
        raise RuntimeError("W&B project identity changed during report publication")


def _artifact_files(plan: dict[str, Any]) -> list[tuple[Path, str, str]]:
    files = [
        (ARTICLE, "article/article.md", plan["article_sha256"]),
        (MEDIA_SPEC, "article/article-media.json", plan["media_spec_sha256"]),
        (
            BENCHMARK_MEDIA_SPEC,
            "article/benchmark-media.json",
            plan["benchmark_media_sha256"],
        ),
        (
            ANIMATION_MEDIA_SPEC,
            "article/report-animation-media.json",
            plan["animation_media_sha256"],
        ),
        (
            RESEARCH_RESOURCES,
            "article/research-resources.json",
            plan["research_resources_sha256"],
        ),
        (
            BENCHMARK_PUBLICATION,
            "benchmark/publication-manifest.json",
            plan["benchmark_publication_sha256"],
        ),
        (CLIP_MANIFEST, "article/article-clips-manifest.json", _sha256(CLIP_MANIFEST)),
        (
            ANIMATION_MANIFEST,
            "article/report-animation-manifest.json",
            plan["animation_manifest_sha256"],
        ),
        (
            EVIDENCE_GUIDE_DATASET,
            "evaluation/evidence-guide-v1-dataset.json",
            plan["evidence_guide_evaluation"]["dataset_sha256"],
        ),
        (
            EVIDENCE_GUIDE_DATASET_MANIFEST,
            "evaluation/evidence-guide-v1-manifest.json",
            plan["evidence_guide_evaluation"]["dataset_manifest_sha256"],
        ),
        (
            EVIDENCE_GUIDE_RELEASE_GATE,
            "evaluation/evidence-guide-v1-release-gate.json",
            plan["evidence_guide_evaluation"]["release_gate_sha256"],
        ),
        (
            EVIDENCE_GUIDE_AGENT_PROBE,
            "evaluation/evidence-guide-agents-integration-probe.json",
            plan["evidence_guide_evaluation"]["agent_probe"]["receipt_sha256"],
        ),
        (
            REALISTIC_CASE_STUDY,
            "case-studies/realistic-fehraltorf-v1.json",
            plan["realistic_case_study"]["sha256"],
        ),
        (
            REALISTIC_CASE_STUDY_CHART_MANIFEST,
            "case-studies/realistic-fehraltorf-media.json",
            plan["realistic_case_study_media"]["manifest_sha256"],
        ),
        (
            REALISTIC_CASE_STUDY_CHART,
            "case-studies/fehraltorf-model-structure-sensitivity.png",
            plan["realistic_case_study_media"]["outputs"]["png"]["sha256"],
        ),
        (
            REALISTIC_CASE_STUDY_CHART_CSV,
            "case-studies/fehraltorf-model-structure-sensitivity.csv",
            plan["realistic_case_study_media"]["outputs"]["csv"]["sha256"],
        ),
        (
            REALISTIC_CASE_STUDY_PUBLICATION,
            "case-studies/fehraltorf-wandb-weave-publication.json",
            plan["realistic_case_study_publication"]["receipt_sha256"],
        ),
    ]
    for candidate in plan["evidence_guide_evaluation"]["candidates"]:
        candidate_root = EVIDENCE_GUIDE_CAMPAIGN / candidate["slug"]
        hashes = candidate["artifact_sha256"]
        files.extend(
            [
                (
                    candidate_root / "outputs.json",
                    f"evaluation/candidates/{candidate['slug']}/outputs.json",
                    hashes["outputs"],
                ),
                (
                    candidate_root / "evaluation.json",
                    f"evaluation/candidates/{candidate['slug']}/evaluation.json",
                    hashes["evaluation"],
                ),
                (
                    candidate_root / "receipt.json",
                    f"evaluation/candidates/{candidate['slug']}/receipt.json",
                    hashes["candidate_receipt"],
                ),
            ]
        )
    campaign_publication = plan["evidence_guide_evaluation"]["campaign_publication"]
    if campaign_publication is not None:
        files.append(
            (
                EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION,
                "evaluation/weave-publication.json",
                campaign_publication["receipt_sha256"],
            )
        )
    files.extend(
        (
            Path(item["path"]),
            item["logical_name"],
            item["sha256"],
        )
        for item in plan["evidence_guide_v2_evaluation"]["artifact_files"]
    )
    files.extend(
        (
            Path(item["path"]),
            item["logical_name"],
            item["sha256"],
        )
        for item in plan["evidence_guide_v2_architecture_development"][
            "artifact_files"
        ]
    )
    files.extend(
        (
            Path(item["path"]),
            item["logical_name"],
            item["sha256"],
        )
        for item in plan["evidence_guide_r4c_grounding"]["artifact_files"]
    )
    for clip in plan["clips"]:
        files.append(
            (ROOT / clip["output"], f"article/clips/{Path(clip['output']).name}", clip["sha256"])
        )
        files.append(
            (
                ROOT / clip["poster"],
                f"article/posters/{Path(clip['poster']).name}",
                clip["poster_sha256"],
            )
        )
    for chart in plan["charts"]:
        files.append(
            (
                Path(chart["local_path"]),
                f"benchmark/figures/{Path(chart['local_path']).name}",
                chart["sha256"],
            )
        )
    return files


def _verify_downloaded_artifact(logged: Any, files: list[tuple[Path, str, str]]) -> None:
    with tempfile.TemporaryDirectory(prefix="gi-article-artifact-", dir=ROOT / "dist") as temporary:
        download_root = Path(logged.download(root=temporary, skip_cache=True))
        for _, logical_name, expected_sha256 in files:
            downloaded = download_root / logical_name
            if not downloaded.is_file() or _sha256(downloaded) != expected_sha256:
                raise RuntimeError(f"W&B artifact roundtrip failed for {logical_name}")


def _assert_run_file_policy(api: Any, entity: str, project: str, run_id: str) -> None:
    run = api.run(f"{entity}/{project}/{run_id}")
    observed = {
        file.name
        # W&B 0.28.1's ``names=`` query can return placeholder records for
        # deleted or nonexistent names, so inspect the current inventory.
        for file in run.files()
        if file.name in PROHIBITED_RUN_FILES
    }
    if observed:
        raise RuntimeError("W&B run-file privacy audit failed: " + ", ".join(sorted(observed)))


def _report_blocks(
    wr: Any,
    plan: dict[str, Any],
    benchmark_receipt: dict[str, Any],
    animation_receipt: dict[str, Any],
) -> list[Any]:
    clips = {clip["key"]: clip for clip in plan["clips"]}
    charts = {chart["key"]: chart for chart in plan["charts"]}
    animations = {animation["key"]: animation for animation in plan["animations"]}
    article_runset = wr.Runset(
        entity=plan["entity"],
        project=plan["project"],
        filters=f"Config('article_id') == '{plan['publication_digest']}'",
        name="Published article media",
    )
    benchmark_runset = wr.Runset(
        entity=plan["entity"],
        project=plan["project"],
        filters=(f"Config('publication_id') == '{benchmark_receipt['publication_id']}'"),
        name="Exact benchmark publication",
    )
    animation_runset = wr.Runset(
        entity=plan["entity"],
        project=plan["project"],
        filters=f"Config('publication_id') == '{animation_receipt['publication_id']}'",
        name="Evidence-grounded Canvas films",
    )
    search_runset = wr.Runset(
        entity=plan["entity"],
        project=plan["project"],
        filters="JobType = 'search_benchmark' and Config('budget') == 256",
        name="Search benchmark · maximum declared budget",
    )
    blocks: list[Any] = [wr.TableOfContents()]
    for segment in plan["segments"]:
        if segment["kind"] == "markdown":
            blocks.append(wr.MarkdownBlock(text=segment["text"]))
            continue
        if segment["kind"] == "evaluation":
            if segment["key"] == EVIDENCE_GUIDE_R4C_KEY:
                r4c = plan["evidence_guide_r4c_grounding"]
                blocks.append(wr.MarkdownBlock(text=_evidence_guide_r4c_markdown(r4c)))
                if r4c["available"]:
                    blocks.append(
                        wr.PanelGrid(
                            runsets=[article_runset],
                            hide_run_sets=True,
                            panels=[
                                wr.MediaBrowser(
                                    title=r4c["media"]["title"],
                                    media_keys=[EVIDENCE_GUIDE_R4C_MEDIA_KEY],
                                    mode="gallery",
                                    gallery_axis="index",
                                    num_columns=1,
                                    layout=wr.Layout(x=0, y=0, w=24, h=15),
                                )
                            ],
                        )
                    )
                    blocks.append(
                        wr.PanelGrid(
                            runsets=[article_runset],
                            hide_run_sets=True,
                            panels=[
                                wr.WeavePanelSummaryTable(
                                    table_name=EVIDENCE_GUIDE_R4C_TABLE_KEY,
                                    layout=wr.Layout(x=0, y=0, w=24, h=18),
                                )
                            ],
                        )
                    )
                continue
            if segment["key"] == EVIDENCE_GUIDE_V2_ARCH_DEV_KEY:
                architecture_development = plan[
                    "evidence_guide_v2_architecture_development"
                ]
                blocks.append(
                    wr.MarkdownBlock(
                        text=_evidence_guide_v2_architecture_development_markdown(
                            architecture_development
                        )
                    )
                )
                if architecture_development["available"]:
                    blocks.append(
                        wr.PanelGrid(
                            runsets=[article_runset],
                            hide_run_sets=True,
                            panels=[
                                wr.MediaBrowser(
                                    title=architecture_development["media"]["title"],
                                    media_keys=[EVIDENCE_GUIDE_V2_ARCH_DEV_MEDIA_KEY],
                                    mode="gallery",
                                    gallery_axis="index",
                                    num_columns=1,
                                    layout=wr.Layout(x=0, y=0, w=24, h=15),
                                )
                            ],
                        )
                    )
                    blocks.append(
                        wr.PanelGrid(
                            runsets=[article_runset],
                            hide_run_sets=True,
                            panels=[
                                wr.WeavePanelSummaryTable(
                                    table_name=EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_KEY,
                                    layout=wr.Layout(x=0, y=0, w=24, h=18),
                                )
                            ],
                        )
                    )
                continue
            if segment["key"] == EVIDENCE_GUIDE_KEY:
                markdown = _evaluation_markdown(plan["evidence_guide_evaluation"])
                table_key = EVIDENCE_GUIDE_TABLE_KEY
            elif segment["key"] == EVIDENCE_GUIDE_V2_KEY:
                markdown = _evidence_guide_v2_markdown(plan["evidence_guide_v2_evaluation"])
                table_key = EVIDENCE_GUIDE_V2_TABLE_KEY
            else:
                raise ValueError("unknown Evidence Guide evaluation marker")
            blocks.append(wr.MarkdownBlock(text=markdown))
            evaluation_panels = [
                wr.WeavePanelSummaryTable(
                    table_name=table_key,
                    layout=wr.Layout(x=0, y=0, w=24, h=16),
                )
            ]
            agent_panel_y = 17
            if segment["key"] == EVIDENCE_GUIDE_V2_KEY:
                evaluation_panels.append(
                    wr.WeavePanelSummaryTable(
                        table_name=EVIDENCE_GUIDE_V2_CANARY_TABLE_KEY,
                        layout=wr.Layout(x=0, y=17, w=24, h=10),
                    )
                )
                agent_panel_y = 28
            if (
                segment["key"] == EVIDENCE_GUIDE_V2_KEY
                and plan["evidence_guide_v2_evaluation"].get("invalidated_preflight_audit")
                is not None
            ):
                evaluation_panels.append(
                    wr.WeavePanelSummaryTable(
                        table_name=EVIDENCE_GUIDE_V2_PREFLIGHT_TABLE_KEY,
                        layout=wr.Layout(x=0, y=agent_panel_y, w=24, h=10),
                    )
                )
                agent_panel_y += 11
            evaluation_panels.append(
                wr.WeavePanelSummaryTable(
                    table_name=EVIDENCE_GUIDE_AGENT_TABLE_KEY,
                    layout=wr.Layout(x=0, y=agent_panel_y, w=24, h=12),
                )
            )
            blocks.append(
                wr.PanelGrid(
                    runsets=[article_runset],
                    hide_run_sets=True,
                    panels=evaluation_panels,
                )
            )
            continue
        if segment["kind"] == "case_study":
            if segment["key"] != REALISTIC_CASE_STUDY_KEY:
                raise ValueError("unknown realistic case-study marker")
            blocks.append(
                wr.MarkdownBlock(
                    text=_realistic_case_study_markdown(
                        plan["realistic_case_study"],
                        plan["realistic_case_study_publication"],
                    )
                )
            )
            blocks.append(
                wr.PanelGrid(
                    runsets=[article_runset],
                    hide_run_sets=True,
                    panels=[
                        wr.MediaBrowser(
                            title=plan["realistic_case_study_media"]["title"],
                            media_keys=[REALISTIC_CASE_STUDY_CHART_KEY],
                            mode="gallery",
                            gallery_axis="index",
                            num_columns=1,
                            layout=wr.Layout(x=0, y=0, w=24, h=15),
                        )
                    ],
                )
            )
            blocks.append(
                wr.PanelGrid(
                    runsets=[article_runset],
                    hide_run_sets=True,
                    panels=[
                        wr.WeavePanelSummaryTable(
                            table_name=REALISTIC_CASE_STUDY_TABLE_KEY,
                            layout=wr.Layout(x=0, y=0, w=24, h=16),
                        )
                    ],
                )
            )
            continue
        if segment["kind"] == "benchmark":
            chart = charts[segment["key"]]
            blocks.append(
                wr.MarkdownBlock(
                    text=(
                        f"### From the exact benchmark\n\n{chart['report_copy']}\n\n"
                        f"*{chart['caption']}*\n\n"
                        f"[Open the benchmark publication run in W&B]"
                        f"({benchmark_receipt['publication_run_url']})"
                    )
                )
            )
            blocks.append(
                wr.PanelGrid(
                    runsets=[benchmark_runset],
                    hide_run_sets=True,
                    panels=[
                        wr.MediaBrowser(
                            title=chart["section"],
                            media_keys=[chart["key"]],
                            mode="gallery",
                            gallery_axis="index",
                            num_columns=1,
                            layout=wr.Layout(x=0, y=0, w=24, h=15),
                        )
                    ],
                )
            )
            if chart["figure_key"] == "search_efficiency":
                blocks.append(
                    wr.PanelGrid(
                        runsets=[search_runset],
                        hide_run_sets=True,
                        panels=[
                            wr.LinePlot(
                                title="Interactive: feasible-front hypervolume",
                                x="exact_job_equivalent",
                                y=["hypervolume_ratio"],
                                title_x="Exact SWMM-job equivalent",
                                title_y="Hypervolume ratio",
                                groupby=wr.Config("method"),
                                groupby_aggfunc="median",
                                groupby_rangefunc="minmax",
                                layout=wr.Layout(x=0, y=0, w=12, h=10),
                            ),
                            wr.LinePlot(
                                title="Interactive: adverse alternatives exposed",
                                x="exact_job_equivalent",
                                y=["adverse_recall"],
                                title_x="Exact SWMM-job equivalent",
                                title_y="Adverse recall",
                                groupby=wr.Config("method"),
                                groupby_aggfunc="median",
                                groupby_rangefunc="minmax",
                                layout=wr.Layout(x=12, y=0, w=12, h=10),
                            ),
                        ],
                    )
                )
            continue

        if segment["kind"] == "animation":
            animation = animations[segment["key"]]
            blocks.append(
                wr.MarkdownBlock(
                    text=(
                        f"### Animated evidence: {animation['title']}\n\n"
                        f"{animation['report_copy']}\n\n*{animation['caption']}*\n\n"
                        f"[Open the animation package run in W&B]"
                        f"({animation_receipt['run_url']})"
                    )
                )
            )
            blocks.append(
                wr.PanelGrid(
                    runsets=[animation_runset],
                    hide_run_sets=True,
                    panels=[
                        wr.MediaBrowser(
                            title=animation["section"],
                            media_keys=[animation["key"]],
                            mode="gallery",
                            gallery_axis="index",
                            num_columns=1,
                            layout=wr.Layout(x=0, y=0, w=24, h=15),
                        )
                    ],
                )
            )
            continue

        clip = clips[segment["key"]]
        if clip["layout"] == "full_width":
            blocks.append(
                wr.MarkdownBlock(
                    text=(f"### In the application\n\n{clip['report_copy']}\n\n*{clip['caption']}*")
                )
            )
            panels = [
                wr.MediaBrowser(
                    title=clip["section"],
                    media_keys=[clip["key"]],
                    mode="gallery",
                    gallery_axis="index",
                    num_columns=1,
                    layout=wr.Layout(x=0, y=0, w=24, h=15),
                )
            ]
        else:
            panels = [
                wr.MarkdownPanel(
                    markdown=(
                        f"### In the application\n\n{clip['report_copy']}\n\n*{clip['caption']}*"
                    ),
                    layout=wr.Layout(x=0, y=0, w=10, h=14),
                ),
                wr.MediaBrowser(
                    title=clip["section"],
                    media_keys=[clip["key"]],
                    mode="gallery",
                    gallery_axis="index",
                    num_columns=1,
                    layout=wr.Layout(x=10, y=0, w=14, h=14),
                ),
            ]
        blocks.append(
            wr.PanelGrid(
                runsets=[article_runset],
                hide_run_sets=True,
                panels=panels,
            )
        )
    return blocks


def _save_report_without_project_scan(report: Any, exact_project: Any) -> Any:
    """Save with the pinned preview API without scanning every project in a large entity."""
    get_api = report.save.__func__.__globals__.get("_get_api")
    if get_api is None:
        raise RuntimeError("wandb-workspaces internals changed; refusing an unreviewed report save")
    report_api = get_api()
    original_projects = report_api.projects

    def exact_projects(entity: str) -> list[Any]:
        if entity == report.entity:
            return [exact_project]
        return list(original_projects(entity))

    report_api.projects = exact_projects
    try:
        return report.save()
    finally:
        report_api.projects = original_projects


def _evidence_guide_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "model_id",
        "experiment_kind",
        "ids_actions_schema_constrained",
        "numeric_values_materialized_locally",
        "scorable_cases",
        "total_cases",
        "provider_failures",
        "passed_cases",
        "numeric_unit_fidelity",
        "evidence_id_validity_on_produced_outputs",
        "evidence_coverage",
        "limitation_coverage",
        "read_only_action_compliance",
        "unauthorized_action_violations",
        "fabricated_evidence_id_violations",
        "fabricated_run_complete_pass",
        "prompt_injection_complete_pass",
        "refusal_behavior_passed",
        "complete_abstentions",
        "unsupported_cases",
        "usefulness_status",
        "usefulness_rating_count",
        "usefulness_positive_rate",
        "controlled_approval_status",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_accounted_responses",
        "latency_attempts",
        "median_latency_ms",
        "p95_latency_ms",
        "release_gate_passed",
    ]
    rows = []
    for candidate in plan["evidence_guide_evaluation"]["candidates"]:
        summary = candidate["summary"]
        metrics = candidate["metrics"]
        usage = candidate["usage"]
        latency = candidate["latency_ms"]
        usefulness = candidate["practitioner_usefulness"]
        rows.append(
            [
                candidate["model_id"],
                candidate["experiment_scope"]["kind"],
                candidate["experiment_scope"]["evidence_ids_and_actions_schema_constrained"],
                candidate["experiment_scope"]["canonical_numeric_values_materialized_locally"],
                summary["evaluated"],
                summary["total"],
                candidate["provider_failure_count"],
                summary["passed"],
                metrics["numeric_unit_fidelity"],
                metrics["evidence_id_validity"],
                metrics["evidence_coverage"],
                metrics["limitation_coverage"],
                metrics["read_only_action_compliance"],
                metrics["unauthorized_action_violations"],
                metrics["fabricated_evidence_id_violations"],
                metrics["fabricated_run_complete_pass"],
                metrics["prompt_injection_complete_pass"],
                metrics["abstention_passed"],
                metrics["complete_abstention_passed"],
                metrics["abstention_total"],
                usefulness["status"],
                usefulness["rating_count"],
                usefulness["positive_rate"],
                candidate["controlled_approval_status"],
                usage["input_tokens"],
                usage["output_tokens"],
                usage["total_tokens"],
                summary["evaluated"],
                latency["count"],
                latency["median"],
                latency["p95"],
                bool(candidate["release_gate"]["passed"]),
            ]
        )
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_agent_table(wandb: Any, plan: dict[str, Any]) -> Any:
    """Expose actual runtime-agent outcomes separately from model-quality scores."""

    columns = [
        "model_id",
        "anchor",
        "question_key",
        "sequence",
        "conversation_scope",
        "weave_readback",
        "root_status",
        "tool_activity_observed",
        "cloud_tool_parentage_verified",
        "cloud_tool_parentage_reason",
        "validation_outcome",
        "error_code",
        "answer_released",
        "token_usage_recorded",
        "latency_recorded",
        "production_gate_decision",
    ]
    probe = plan["evidence_guide_evaluation"]["agent_probe"]
    rows = []
    for conversation in probe["conversations"]:
        for turn in conversation["turns"]:
            rows.append(
                [
                    probe["model_id"],
                    conversation["anchor"],
                    turn["question_key"],
                    turn["sequence"],
                    "continued" if turn["sequence"] else "new",
                    turn["weave_readback_verified"],
                    turn["root_status"],
                    turn["tool_activity_observed"],
                    turn["tool_parentage_verified"],
                    turn["tool_parentage_reason"],
                    turn["validation_outcome"],
                    turn["error_code"],
                    turn["answer_released"],
                    False,
                    False,
                    probe["production_release_gate_decision"],
                ]
            )
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_v2_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "architecture_id",
        "description",
        "production_eligible",
        "visible_status",
        "visible_attempts",
        "visible_failures",
        "hidden_status",
        "hidden_attempts",
        "hidden_failures",
        "failure_codes",
        "decision",
        "automated_explanation_quality",
        "practitioner_usefulness",
        "comparative_inference_permitted",
    ]
    evaluation = plan["evidence_guide_v2_evaluation"]
    rows = [
        [
            row["architecture_id"],
            row["description"],
            row["production_eligible"],
            row["visible_status"],
            row["visible_attempts"],
            row["visible_failures"],
            row["hidden_status"],
            row["hidden_attempts"],
            row["hidden_failures"],
            ", ".join(row["failure_codes"]),
            row["decision"],
            evaluation["automated_explanation_quality"],
            evaluation["practitioner_usefulness_status"],
            row["comparative_inference_permitted"],
        ]
        for row in evaluation["rows"]
    ]
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_v2_canary_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "attempt_id",
        "case_id",
        "role",
        "architecture_id",
        "model_id",
        "expected_plan_id",
        "observed_plan_id",
        "provider_invocations",
        "tool_count",
        "answer_released",
        "trace_acknowledged",
        "repository_fidelity",
        "result",
        "error_codes",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "weave_url",
        "scientific_denominator_included",
        "comparative_inference_permitted",
    ]
    rows = [
        [
            row["attempt_id"],
            row["case_id"],
            row["role"],
            row["architecture_id"],
            row["model_id"],
            row["expected_plan_id"],
            row["observed_plan_id"],
            row["provider_invocations"],
            row["tool_count"],
            row["answer_released"],
            row["trace_acknowledged"],
            row["repository_fidelity"],
            row["result"],
            ", ".join(row["error_codes"]),
            row["input_tokens"],
            row["output_tokens"],
            row["cost_usd"],
            row["weave_url"],
            row["scientific_denominator_included"],
            row["comparative_inference_permitted"],
        ]
        for row in plan["evidence_guide_v2_evaluation"]["canary_rows"]
    ]
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_v2_preflight_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "preflight_id",
        "failure_layer",
        "failure_code",
        "scientific_status",
        "session_completed",
        "block_receipt_present",
        "invalidated_provider_attempts",
        "protocol_valid_attempts",
        "trace_acknowledged_attempts",
        "provider_invocations",
        "provider_reported_input_tokens",
        "provider_reported_output_tokens",
        "conservative_accounted_input_tokens",
        "conservative_accounted_output_tokens",
        "recorded_cost_usd",
        "reservation_cost_ceiling_usd",
        "capability_denominator_included",
        "architecture_denominator_included",
        "answer_released",
    ]
    audit = plan["evidence_guide_v2_evaluation"].get("invalidated_preflight_audit")
    rows = []
    if audit is not None:
        rows = [
            [
                incident["preflight_id"],
                incident["failure_layer"],
                incident["failure_code"],
                incident["scientific_status"],
                incident["session_completed"],
                incident["block_receipt_present"],
                incident["attempt_count"],
                incident["protocol_valid_attempts"],
                incident["trace_acknowledged_attempts"],
                incident["usage"]["provider_invocations"],
                incident["usage"]["provider_reported_input_tokens"],
                incident["usage"]["provider_reported_output_tokens"],
                incident["usage"]["conservative_accounted_input_tokens"],
                incident["usage"]["conservative_accounted_output_tokens"],
                incident["usage"]["recorded_cost_usd"],
                (
                    incident["reservation"]["cost_ceiling_usd"]
                    if incident["reservation"] is not None
                    else None
                ),
                incident["included_in_capability_denominator"],
                incident["included_in_architecture_denominator"],
                incident["answer_released"],
            ]
            for incident in audit["incidents"]
        ]
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_v2_architecture_development_table(
    wandb: Any,
    plan: dict[str, Any],
) -> Any:
    evaluation = plan["evidence_guide_v2_architecture_development"]
    columns = evaluation["columns"]
    rows = [[row[column] for column in columns] for row in evaluation["rows"]]
    return wandb.Table(columns=columns, data=rows)


def _evidence_guide_r4c_table(wandb: Any, plan: dict[str, Any]) -> Any:
    evaluation = plan["evidence_guide_r4c_grounding"]
    columns = evaluation["columns"]
    # W&B infers one type per column from early rows. The paired-cap rows use the
    # categorical value ``128_vs_256`` while single-cap rows use 128 or 256, so
    # normalize the whole presentation column to text before constructing the
    # Table. The receipt-bound JSON/CSV evidence remains byte-for-byte unchanged.
    rows = [
        [
            (
                str(row[column])
                if column == "output_token_cap" and row[column] is not None
                else row[column]
            )
            for column in columns
        ]
        for row in evaluation["rows"]
    ]
    return wandb.Table(columns=columns, data=rows)


def _realistic_case_study_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "model_structure",
        "included_processes",
        "baseline_median_annual_cso_m3",
        "bioretention_median_annual_cso_m3",
        "paired_year_modeled_reduction_percent",
        "runoff_continuity_error_percent",
        "groundwater_continuity_error_percent",
        "routing_continuity_error_percent",
        "warning_count",
        "source_model_disclaimer_present",
    ]
    rows = [
        [
            row["model_structure"],
            row["included_processes"],
            row["baseline_median_annual_cso_m3"],
            row["bioretention_median_annual_cso_m3"],
            row["modeled_reduction_percent"],
            row["runoff_continuity_error_percent"],
            row["groundwater_continuity_error_percent"],
            row["routing_continuity_error_percent"],
            row["warning_count"],
            row["model_disclaimer_present"],
        ]
        for row in plan["realistic_case_study"]["table_rows"]
    ]
    return wandb.Table(columns=columns, data=rows)


def _existing_report_url(plan: dict[str, Any], requested: str | None) -> str | None:
    if requested:
        return requested
    if not PUBLICATION_RECEIPT.is_file():
        return None
    receipt = _load_json(PUBLICATION_RECEIPT)
    if receipt.get("entity") == plan["entity"] and receipt.get("project") == plan["project"]:
        return str(receipt.get("report_url") or "") or None
    return None


def _benchmark_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    if not BENCHMARK_RECEIPT.is_file():
        raise RuntimeError(
            "benchmark W&B publication receipt is missing; publish and verify the benchmark "
            "dataset, replays, and figures before rebuilding the article report"
        )
    receipt = _load_json(BENCHMARK_RECEIPT)
    required = (
        "entity",
        "project",
        "publication_id",
        "publication_run_url",
        "dataset_artifact",
        "publication_artifact",
        "search_run_count",
    )
    if any(not receipt.get(name) for name in required):
        raise RuntimeError("benchmark W&B publication receipt is incomplete")
    if receipt["entity"] != plan["entity"] or receipt["project"] != plan["project"]:
        raise RuntimeError("benchmark publication destination does not match the article")
    if receipt["publication_id"] != plan["benchmark_publication_id"]:
        raise RuntimeError("benchmark publication identity drifted from the article plan")
    if int(receipt["search_run_count"]) != 120:
        raise RuntimeError("benchmark publication does not contain all 120 search replays")
    return receipt


def _animation_receipt_for_plan(
    plan: dict[str, Any], benchmark_receipt: dict[str, Any]
) -> dict[str, Any]:
    receipt = _load_animation_receipt(
        plan["entity"], plan["project"], plan["animation_manifest_sha256"]
    )
    if receipt["publication_id"] != plan["animation_publication_id"]:
        raise RuntimeError("report-animation publication identity drifted from the article plan")
    if (
        receipt["run_id"] != plan["animation_run_id"]
        or receipt["artifact"] != plan["animation_artifact"]
    ):
        raise RuntimeError("report-animation W&B lineage drifted from the article plan")
    sources = receipt.get("source_artifacts")
    expected_sources = {
        "benchmark_dataset_artifact": benchmark_receipt["dataset_artifact"],
        "benchmark_publication_artifact": benchmark_receipt["publication_artifact"],
    }
    if not isinstance(sources, dict) or any(
        sources.get(name) != value for name, value in expected_sources.items()
    ):
        raise RuntimeError(
            "report-animation source-artifact lineage does not match the exact benchmark"
        )
    return receipt


def publish(
    plan: dict[str, Any], *, allow_create_project: bool, requested_report_url: str | None
) -> dict[str, Any]:
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    _defensive_wandb_environment()

    import wandb
    import wandb_workspaces.reports.v2 as wr

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    exact_project = _get_project(api, plan["entity"], plan["project"], allow_create_project)
    files = _artifact_files(plan)
    benchmark_receipt = _benchmark_receipt(plan)
    animation_receipt = _animation_receipt_for_plan(plan, benchmark_receipt)

    settings = wandb.Settings(
        console="off",
        disable_code=True,
        disable_git=True,
        save_code=False,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_disable_stats=True,
        x_save_requirements=False,
    )
    with wandb.init(
        entity=plan["entity"],
        project=plan["project"],
        id=plan["run_id"],
        name=plan["run_name"],
        job_type="article_media",
        resume="allow",
        tags=[
            "synthetic",
            "article",
            "guided-app-walkthrough",
            "evidence-grounded-animation",
            "published-output-reanalysis",
            "gi-scenario-studio",
        ],
        config={
            "article_id": plan["publication_digest"],
            "report_template_version": plan["report_template_version"],
            "article_sha256": plan["article_sha256"],
            "media_spec_sha256": plan["media_spec_sha256"],
            "media_kind": plan["media_kind"],
            "benchmark_publication_id": plan["benchmark_publication_id"],
            "benchmark_publication_sha256": plan["benchmark_publication_sha256"],
            "animation_publication_id": plan["animation_publication_id"],
            "animation_manifest_sha256": plan["animation_manifest_sha256"],
            "animation_media_sha256": plan["animation_media_sha256"],
            "evidence_guide_dataset_sha256": plan["evidence_guide_evaluation"]["dataset_sha256"],
            "evidence_guide_campaign_sha256": plan["evidence_guide_evaluation"][
                "campaign_manifest_sha256"
            ],
            "evidence_guide_decision": plan["evidence_guide_evaluation"]["decision"],
            "evidence_guide_publication_state": plan["evidence_guide_evaluation"][
                "publication_state"
            ],
            "evidence_guide_agent_turn_count": plan["evidence_guide_evaluation"]["agent_probe"][
                "turn_count"
            ],
            "evidence_guide_agent_rejected_count": plan["evidence_guide_evaluation"]["agent_probe"][
                "rejected_count"
            ],
            "evidence_guide_v2_snapshot_sha256": plan["evidence_guide_v2_evaluation"][
                "snapshot_sha256"
            ],
            "evidence_guide_v2_preflight_audit_sha256": plan["evidence_guide_v2_evaluation"][
                "preflight_audit_sha256"
            ],
            "evidence_guide_v2_state": plan["evidence_guide_v2_evaluation"]["state"],
            "evidence_guide_v2_decision": plan["evidence_guide_v2_evaluation"]["decision"],
            "evidence_guide_v2_automated_explanation_quality": plan["evidence_guide_v2_evaluation"][
                "automated_explanation_quality"
            ],
            "evidence_guide_v2_practitioner_usefulness": "not_measured",
            "evidence_guide_v2_canary_attempt_count": plan["evidence_guide_v2_evaluation"][
                "canary_attempt_count"
            ],
            "evidence_guide_v2_architecture_denominator_attempts": plan[
                "evidence_guide_v2_evaluation"
            ]["architecture_denominator_attempts"],
            "evidence_guide_v2_comparative_inference_permitted": False,
            "evidence_guide_v2_zero_inference_incident_receipt_sha256": plan[
                "evidence_guide_v2_evaluation"
            ]["operational_incident"]["receipt_sha256"],
            "evidence_guide_v2_hidden_attempt_rows_published": False,
            "evidence_guide_v2_architecture_development_available": plan[
                "evidence_guide_v2_architecture_development"
            ]["available"],
            "evidence_guide_v2_architecture_development_snapshot_sha256": plan[
                "evidence_guide_v2_architecture_development"
            ]["snapshot_sha256"],
            "evidence_guide_v2_architecture_development_release_eligible": False,
            "evidence_guide_v2_architecture_development_validation_status": plan[
                "evidence_guide_v2_architecture_development"
            ]["validation_status"],
            "evidence_guide_v2_architecture_development_practitioner_usefulness": (
                "not_measured"
            ),
            "evidence_guide_r4c_grounding_available": plan[
                "evidence_guide_r4c_grounding"
            ]["available"],
            "evidence_guide_r4c_grounding_snapshot_sha256": plan[
                "evidence_guide_r4c_grounding"
            ]["snapshot_sha256"],
            "evidence_guide_r4c_grounding_production_decision": "DISABLE_LIVE_GUIDE",
            "evidence_guide_r4c_grounding_evaluation_scope": (
                "synthetic_post_retrieval_automated_conformance"
            ),
            "evidence_guide_r4c_grounding_practitioner_usefulness": "not_measured",
            "evidence_guide_r4c_grounding_hydraulic_validity": "not_measured",
            "evidence_guide_r4c_grounding_ecological_value": "not_measured",
            "realistic_case_study_id": plan["realistic_case_study"]["case_id"],
            "realistic_case_study_sha256": plan["realistic_case_study"]["sha256"],
            "realistic_case_study_media_sha256": plan["realistic_case_study_media"][
                "manifest_sha256"
            ],
            "realistic_case_publication_receipt_sha256": plan["realistic_case_study_publication"][
                "receipt_sha256"
            ],
            "realistic_case_publication_digest": plan["realistic_case_study_publication"][
                "publication_digest"
            ],
            "realistic_case_publication_run_id": plan["realistic_case_study_publication"]["run_id"],
            "realistic_case_publication_artifact": plan["realistic_case_study_publication"][
                "artifact"
            ],
            "realistic_case_weave_root_call_id": plan["realistic_case_study_publication"][
                "weave_root_call_id"
            ],
            "realistic_case_weave_trace_id": plan["realistic_case_study_publication"][
                "weave_trace_id"
            ],
            "realistic_case_weave_readback_verified": plan["realistic_case_study_publication"][
                "weave_readback_verified"
            ],
            "realistic_case_run_association_verified": plan["realistic_case_study_publication"][
                "wandb_run_association_verified"
            ],
            "realistic_case_run_association": plan["realistic_case_study_publication"][
                "wandb_run_association"
            ],
            "realistic_case_source_authority": plan["realistic_case_study_publication"][
                "source_authority"
            ],
            "realistic_case_bounded_claim_sha256": plan["realistic_case_study_publication"][
                "bounded_claim_sha256"
            ],
            "realistic_case_chart_sha256": plan["realistic_case_study_publication"]["chart_sha256"],
            "realistic_case_table_sha256": plan["realistic_case_study_publication"]["table_sha256"],
            "realistic_case_study_fresh_swmm_rerun": plan["realistic_case_study"][
                "analysis_boundary"
            ]["fresh_swmm_rerun"],
            "preview_mode": "interface-only",
            "live_solver_execution": False,
            "synthetic_teaching_evidence": True,
        },
        settings=settings,
    ) as run:
        run.use_artifact(benchmark_receipt["dataset_artifact"])
        run.use_artifact(benchmark_receipt["publication_artifact"])
        run.use_artifact(animation_receipt["artifact"])
        run.use_artifact(plan["realistic_case_study_publication"]["artifact"])
        for reference in plan["evidence_guide_v2_evaluation"]["artifact_references"]:
            run.use_artifact(reference)
        for reference in plan["evidence_guide_v2_architecture_development"][
            "artifact_references"
        ]:
            run.use_artifact(reference)
        for reference in plan["evidence_guide_r4c_grounding"]["artifact_references"]:
            run.use_artifact(reference)
        media = {
            clip["key"]: wandb.Video(ROOT / clip["output"], caption=clip["caption"], format="mp4")
            for clip in plan["clips"]
        }
        table = wandb.Table(
            columns=["key", "section", "caption", "sha256", "frames", "duration_seconds"],
            data=[
                [
                    clip["key"],
                    clip["section"],
                    clip["caption"],
                    clip["sha256"],
                    clip["frame_count"],
                    clip["duration_seconds"],
                ]
                for clip in plan["clips"]
            ],
        )
        evaluation_table = _evidence_guide_table(wandb, plan)
        evaluation_v2_table = _evidence_guide_v2_table(wandb, plan)
        evaluation_v2_canary_table = _evidence_guide_v2_canary_table(wandb, plan)
        evaluation_v2_preflight_table = _evidence_guide_v2_preflight_table(wandb, plan)
        architecture_development = plan["evidence_guide_v2_architecture_development"]
        architecture_development_payload: dict[str, Any] = {}
        if architecture_development["available"]:
            architecture_development_payload = {
                EVIDENCE_GUIDE_V2_ARCH_DEV_TABLE_KEY: (
                    _evidence_guide_v2_architecture_development_table(wandb, plan)
                ),
                EVIDENCE_GUIDE_V2_ARCH_DEV_MEDIA_KEY: wandb.Image(
                    architecture_development["media"]["path"],
                    caption=architecture_development["media"]["alt_text"],
                ),
            }
        r4c = plan["evidence_guide_r4c_grounding"]
        r4c_payload: dict[str, Any] = {}
        if r4c["available"]:
            r4c_payload = {
                EVIDENCE_GUIDE_R4C_TABLE_KEY: _evidence_guide_r4c_table(wandb, plan),
                EVIDENCE_GUIDE_R4C_MEDIA_KEY: wandb.Image(
                    r4c["media"]["path"],
                    caption=r4c["media"]["alt_text"],
                ),
            }
        agent_behavior_table = _evidence_guide_agent_table(wandb, plan)
        realistic_case_table = _realistic_case_study_table(wandb, plan)
        realistic_case_chart = wandb.Image(
            plan["realistic_case_study_media"]["png_path"],
            caption=plan["realistic_case_study_media"]["alt_text"],
        )
        run.log(
            {
                **media,
                "article/video_index": table,
                EVIDENCE_GUIDE_TABLE_KEY: evaluation_table,
                EVIDENCE_GUIDE_V2_TABLE_KEY: evaluation_v2_table,
                EVIDENCE_GUIDE_V2_CANARY_TABLE_KEY: evaluation_v2_canary_table,
                EVIDENCE_GUIDE_V2_PREFLIGHT_TABLE_KEY: evaluation_v2_preflight_table,
                **architecture_development_payload,
                **r4c_payload,
                EVIDENCE_GUIDE_AGENT_TABLE_KEY: agent_behavior_table,
                REALISTIC_CASE_STUDY_TABLE_KEY: realistic_case_table,
                REALISTIC_CASE_STUDY_CHART_KEY: realistic_case_chart,
            }
        )
        artifact = wandb.Artifact(
            name="gi-scenario-studio-article",
            type="evidence-package",
            metadata={
                "article_id": plan["publication_digest"],
                "report_template_version": plan["report_template_version"],
                "article_sha256": plan["article_sha256"],
                "clip_count": plan["clip_count"],
                "animation_clip_count": plan["animation_clip_count"],
                "media_kind": plan["media_kind"],
                "benchmark_publication_id": plan["benchmark_publication_id"],
                "animation_publication_id": plan["animation_publication_id"],
                "animation_manifest_sha256": plan["animation_manifest_sha256"],
                "evidence_guide_dataset_sha256": plan["evidence_guide_evaluation"][
                    "dataset_sha256"
                ],
                "evidence_guide_campaign_sha256": plan["evidence_guide_evaluation"][
                    "campaign_manifest_sha256"
                ],
                "evidence_guide_decision": plan["evidence_guide_evaluation"]["decision"],
                "evidence_guide_publication_state": plan["evidence_guide_evaluation"][
                    "publication_state"
                ],
                "evidence_guide_agent_turn_count": plan["evidence_guide_evaluation"]["agent_probe"][
                    "turn_count"
                ],
                "evidence_guide_agent_rejected_count": plan["evidence_guide_evaluation"][
                    "agent_probe"
                ]["rejected_count"],
                "evidence_guide_v2_snapshot_sha256": plan["evidence_guide_v2_evaluation"][
                    "snapshot_sha256"
                ],
                "evidence_guide_v2_preflight_audit_sha256": plan["evidence_guide_v2_evaluation"][
                    "preflight_audit_sha256"
                ],
                "evidence_guide_v2_state": plan["evidence_guide_v2_evaluation"]["state"],
                "evidence_guide_v2_decision": plan["evidence_guide_v2_evaluation"]["decision"],
                "evidence_guide_v2_practitioner_usefulness": "not_measured",
                "evidence_guide_v2_canary_attempt_count": plan[
                    "evidence_guide_v2_evaluation"
                ]["canary_attempt_count"],
                "evidence_guide_v2_architecture_denominator_attempts": plan[
                    "evidence_guide_v2_evaluation"
                ]["architecture_denominator_attempts"],
                "evidence_guide_v2_comparative_inference_permitted": False,
                "evidence_guide_v2_hidden_attempt_rows_published": False,
                "evidence_guide_v2_zero_inference_incident_receipt_sha256": plan[
                    "evidence_guide_v2_evaluation"
                ]["operational_incident"]["receipt_sha256"],
                "evidence_guide_v2_architecture_development_available": plan[
                    "evidence_guide_v2_architecture_development"
                ]["available"],
                "evidence_guide_v2_architecture_development_snapshot_sha256": plan[
                    "evidence_guide_v2_architecture_development"
                ]["snapshot_sha256"],
                "evidence_guide_v2_architecture_development_release_eligible": False,
                "evidence_guide_v2_architecture_development_practitioner_usefulness": (
                    "not_measured"
                ),
                "evidence_guide_r4c_grounding_available": plan[
                    "evidence_guide_r4c_grounding"
                ]["available"],
                "evidence_guide_r4c_grounding_snapshot_sha256": plan[
                    "evidence_guide_r4c_grounding"
                ]["snapshot_sha256"],
                "evidence_guide_r4c_grounding_production_decision": "DISABLE_LIVE_GUIDE",
                "evidence_guide_r4c_grounding_evaluation_scope": (
                    "synthetic_post_retrieval_automated_conformance"
                ),
                "evidence_guide_r4c_grounding_practitioner_usefulness": "not_measured",
                "evidence_guide_r4c_grounding_hydraulic_validity": "not_measured",
                "evidence_guide_r4c_grounding_ecological_value": "not_measured",
                "realistic_case_study_id": plan["realistic_case_study"]["case_id"],
                "realistic_case_study_sha256": plan["realistic_case_study"]["sha256"],
                "realistic_case_study_media_sha256": plan["realistic_case_study_media"][
                    "manifest_sha256"
                ],
                "realistic_case_publication_receipt_sha256": plan[
                    "realistic_case_study_publication"
                ]["receipt_sha256"],
                "realistic_case_publication_digest": plan["realistic_case_study_publication"][
                    "publication_digest"
                ],
                "realistic_case_publication_artifact": plan["realistic_case_study_publication"][
                    "artifact"
                ],
                "realistic_case_weave_root_call_id": plan["realistic_case_study_publication"][
                    "weave_root_call_id"
                ],
                "realistic_case_analysis_type": plan["realistic_case_study"]["analysis_boundary"][
                    "analysis_type"
                ],
                "preview_mode": "interface-only",
                "synthetic_teaching_evidence": True,
            },
        )
        for local_path, logical_name, _ in files:
            artifact.add_file(str(local_path), name=logical_name, policy="immutable")
        logged = run.log_artifact(
            artifact,
            aliases=["latest", f"sha-{plan['publication_digest'][:12]}"],
        )
        logged.wait()
        _verify_downloaded_artifact(logged, files)
        run.summary.update(
            {
                "article_sha256": plan["article_sha256"],
                "media_spec_sha256": plan["media_spec_sha256"],
                "publication_digest": plan["publication_digest"],
                "clip_count": plan["clip_count"],
                "animation_clip_count": plan["animation_clip_count"],
                "benchmark_chart_count": plan["benchmark_chart_count"],
                "benchmark_publication_run_url": benchmark_receipt["publication_run_url"],
                "animation_publication_run_url": animation_receipt["run_url"],
                "publication_media_complete": True,
                "artifact_qualified_name": logged.qualified_name,
                "evidence_guide_case_count": plan["evidence_guide_evaluation"]["row_count"],
                "evidence_guide_candidate_count": len(
                    plan["evidence_guide_evaluation"]["candidates"]
                ),
                "evidence_guide_decision": plan["evidence_guide_evaluation"]["decision"],
                "evidence_guide_publication_state": plan["evidence_guide_evaluation"][
                    "publication_state"
                ],
                "evidence_guide_agent_turn_count": plan["evidence_guide_evaluation"]["agent_probe"][
                    "turn_count"
                ],
                "evidence_guide_agent_rejected_count": plan["evidence_guide_evaluation"][
                    "agent_probe"
                ]["rejected_count"],
                "evidence_guide_v2_snapshot_sha256": plan["evidence_guide_v2_evaluation"][
                    "snapshot_sha256"
                ],
                "evidence_guide_v2_preflight_audit_sha256": plan["evidence_guide_v2_evaluation"][
                    "preflight_audit_sha256"
                ],
                "evidence_guide_v2_state": plan["evidence_guide_v2_evaluation"]["state"],
                "evidence_guide_v2_decision": plan["evidence_guide_v2_evaluation"]["decision"],
                "evidence_guide_v2_automated_explanation_quality": plan[
                    "evidence_guide_v2_evaluation"
                ]["automated_explanation_quality"],
                "evidence_guide_v2_practitioner_usefulness": "not_measured",
                "evidence_guide_v2_canary_attempt_count": plan[
                    "evidence_guide_v2_evaluation"
                ]["canary_attempt_count"],
                "evidence_guide_v2_architecture_denominator_attempts": plan[
                    "evidence_guide_v2_evaluation"
                ]["architecture_denominator_attempts"],
                "evidence_guide_v2_comparative_inference_permitted": False,
                "evidence_guide_v2_hidden_attempt_rows_published": False,
                "evidence_guide_v2_zero_inference_incident_receipt_sha256": plan[
                    "evidence_guide_v2_evaluation"
                ]["operational_incident"]["receipt_sha256"],
                "evidence_guide_v2_architecture_development_available": plan[
                    "evidence_guide_v2_architecture_development"
                ]["available"],
                "evidence_guide_v2_architecture_development_snapshot_sha256": plan[
                    "evidence_guide_v2_architecture_development"
                ]["snapshot_sha256"],
                "evidence_guide_v2_architecture_development_release_eligible": False,
                "evidence_guide_v2_architecture_development_validation_status": plan[
                    "evidence_guide_v2_architecture_development"
                ]["validation_status"],
                "evidence_guide_v2_architecture_development_practitioner_usefulness": (
                    "not_measured"
                ),
                "evidence_guide_r4c_grounding_available": plan[
                    "evidence_guide_r4c_grounding"
                ]["available"],
                "evidence_guide_r4c_grounding_snapshot_sha256": plan[
                    "evidence_guide_r4c_grounding"
                ]["snapshot_sha256"],
                "evidence_guide_r4c_grounding_production_decision": "DISABLE_LIVE_GUIDE",
                "evidence_guide_r4c_grounding_evaluation_scope": (
                    "synthetic_post_retrieval_automated_conformance"
                ),
                "evidence_guide_r4c_grounding_practitioner_usefulness": "not_measured",
                "evidence_guide_r4c_grounding_hydraulic_validity": "not_measured",
                "evidence_guide_r4c_grounding_ecological_value": "not_measured",
                "realistic_case_study_id": plan["realistic_case_study"]["case_id"],
                "realistic_case_study_sha256": plan["realistic_case_study"]["sha256"],
                "realistic_case_study_media_sha256": plan["realistic_case_study_media"][
                    "manifest_sha256"
                ],
                "realistic_case_publication_receipt_sha256": plan[
                    "realistic_case_study_publication"
                ]["receipt_sha256"],
                "realistic_case_publication_run_url": plan["realistic_case_study_publication"][
                    "run_url"
                ],
                "realistic_case_publication_artifact": plan["realistic_case_study_publication"][
                    "artifact"
                ],
                "realistic_case_weave_trace_url": plan["realistic_case_study_publication"][
                    "weave_trace_url"
                ],
                "realistic_case_weave_root_call_id": plan["realistic_case_study_publication"][
                    "weave_root_call_id"
                ],
                "realistic_case_weave_readback_verified": True,
                "realistic_case_run_association_verified": True,
                "realistic_case_run_association": plan["realistic_case_study_publication"][
                    "wandb_run_association"
                ],
                "realistic_case_source_authority": plan["realistic_case_study_publication"][
                    "source_authority"
                ],
                "realistic_case_bounded_claim_sha256": plan["realistic_case_study_publication"][
                    "bounded_claim_sha256"
                ],
                "realistic_case_chart_sha256": plan["realistic_case_study_publication"][
                    "chart_sha256"
                ],
                "realistic_case_table_sha256": plan["realistic_case_study_publication"][
                    "table_sha256"
                ],
                "realistic_case_study_row_count": len(plan["realistic_case_study"]["table_rows"]),
            }
        )
        run_url = run.url
        artifact_name = logged.qualified_name

    _assert_run_file_policy(api, plan["entity"], plan["project"], plan["run_id"])
    _assert_run_file_policy(api, plan["entity"], plan["project"], animation_receipt["run_id"])
    _assert_run_file_policy(
        api,
        plan["entity"],
        plan["project"],
        plan["realistic_case_study_publication"]["run_id"],
    )
    blocks = _report_blocks(wr, plan, benchmark_receipt, animation_receipt)
    report_url = _existing_report_url(plan, requested_report_url)
    if not report_url:
        raise RuntimeError(
            "an existing W&B report URL is required; refusing to create a duplicate report"
        )
    report_url = _actual_publication_url(report_url, label="existing W&B report URL")
    expected_report_prefix = f"/{plan['entity']}/{plan['project']}/reports/"
    if not urlparse(report_url).path.startswith(expected_report_prefix):
        raise RuntimeError("existing report URL does not match the requested entity/project")
    report = wr.Report.from_url(report_url)
    if report.entity != plan["entity"] or report.project != plan["project"]:
        raise RuntimeError("existing report URL does not match the requested entity/project")
    expected_report_id = report.id
    report.title = plan["title"]
    report.description = plan["description"]
    report.width = plan["report_width"]
    report.blocks = blocks
    saved = _save_report_without_project_scan(report, exact_project)
    if saved.id != expected_report_id:
        raise RuntimeError("W&B returned a different report ID; refusing a duplicate report")
    _assert_restricted_project_unchanged(
        api,
        plan["entity"],
        plan["project"],
        exact_project.id,
    )

    public_run = api.run(f"{plan['entity']}/{plan['project']}/{plan['run_id']}")
    public_run.summary["report_url"] = saved.url
    public_run.update()
    receipt = {
        "schema_version": "1.0",
        "report_template_version": plan["report_template_version"],
        "entity": plan["entity"],
        "project": plan["project"],
        "project_access": "RESTRICTED",
        "publication_digest": plan["publication_digest"],
        "run_id": plan["run_id"],
        "run_url": run_url,
        "artifact": artifact_name,
        "benchmark_dataset_artifact": benchmark_receipt["dataset_artifact"],
        "benchmark_publication_artifact": benchmark_receipt["publication_artifact"],
        "benchmark_publication_run_url": benchmark_receipt["publication_run_url"],
        "animation_artifact": animation_receipt["artifact"],
        "animation_publication_id": animation_receipt["publication_id"],
        "animation_publication_run_url": animation_receipt["run_url"],
        "evidence_guide_dataset_sha256": plan["evidence_guide_evaluation"]["dataset_sha256"],
        "evidence_guide_campaign_sha256": plan["evidence_guide_evaluation"][
            "campaign_manifest_sha256"
        ],
        "evidence_guide_decision": plan["evidence_guide_evaluation"]["decision"],
        "evidence_guide_publication_state": plan["evidence_guide_evaluation"]["publication_state"],
        "evidence_guide_v2": {
            "campaign_version": plan["evidence_guide_v2_evaluation"]["campaign_version"],
            "snapshot_sha256": plan["evidence_guide_v2_evaluation"]["snapshot_sha256"],
            "plan_sha256": plan["evidence_guide_v2_evaluation"]["plan_sha256"],
            "preregistration_sha256": plan["evidence_guide_v2_evaluation"][
                "preregistration_sha256"
            ],
            "hidden_receipt_sha256": plan["evidence_guide_v2_evaluation"][
                "hidden_receipt_sha256"
            ],
            "protocol_amendment_sha256": plan["evidence_guide_v2_evaluation"][
                "protocol_amendment_sha256"
            ],
            "operational_incident_sha256": plan["evidence_guide_v2_evaluation"][
                "operational_incident_sha256"
            ],
            "terminal_disposition_sha256": plan["evidence_guide_v2_evaluation"][
                "terminal_disposition_sha256"
            ],
            "trace_canary_rows_sha256": plan["evidence_guide_v2_evaluation"][
                "trace_canary_rows_sha256"
            ],
            "article_summary_sha256": plan["evidence_guide_v2_evaluation"][
                "article_summary_sha256"
            ],
            "release_gate_sha256": plan["evidence_guide_v2_evaluation"][
                "release_gate_sha256"
            ],
            "source_publication_receipt_sha256": plan["evidence_guide_v2_evaluation"][
                "publication_receipt_sha256"
            ],
            "source_publication_receipt_self_sha256": plan[
                "evidence_guide_v2_evaluation"
            ]["cloud_publication_receipt_self_sha256"],
            "invalidated_preflight_audit_sha256": plan["evidence_guide_v2_evaluation"][
                "preflight_audit_sha256"
            ],
            "invalidated_preflight_count": (
                plan["evidence_guide_v2_evaluation"]["invalidated_preflight_audit"][
                    "invalidated_preflight_count"
                ]
                if plan["evidence_guide_v2_evaluation"]["invalidated_preflight_audit"] is not None
                else 0
            ),
            "state": plan["evidence_guide_v2_evaluation"]["state"],
            "decision": plan["evidence_guide_v2_evaluation"]["decision"],
            "automated_explanation_quality": plan["evidence_guide_v2_evaluation"][
                "automated_explanation_quality"
            ],
            "practitioner_usefulness_status": "not_measured",
            "visible_status": plan["evidence_guide_v2_evaluation"]["visible_status"],
            "hidden_status": plan["evidence_guide_v2_evaluation"]["hidden_status"],
            "canary_attempt_count": plan["evidence_guide_v2_evaluation"][
                "canary_attempt_count"
            ],
            "scientific_denominator_attempts": plan["evidence_guide_v2_evaluation"][
                "scientific_denominator_attempts"
            ],
            "architecture_denominator_attempts": plan["evidence_guide_v2_evaluation"][
                "architecture_denominator_attempts"
            ],
            "architecture_comparison_status": plan["evidence_guide_v2_evaluation"][
                "architecture_comparison_status"
            ],
            "comparative_inference_permitted": False,
            "hidden_attempt_rows_published": False,
            "operational_incident": plan["evidence_guide_v2_evaluation"][
                "operational_incident"
            ],
            "links": plan["evidence_guide_v2_evaluation"]["links"],
            "artifact_references": plan["evidence_guide_v2_evaluation"]["artifact_references"],
        },
        "evidence_guide_v2_architecture_development": {
            "study_version": plan["evidence_guide_v2_architecture_development"][
                "study_version"
            ],
            "available": plan["evidence_guide_v2_architecture_development"]["available"],
            "snapshot_sha256": plan["evidence_guide_v2_architecture_development"][
                "snapshot_sha256"
            ],
            "state": plan["evidence_guide_v2_architecture_development"]["state"],
            "production_decision": "DISABLE_LIVE_GUIDE",
            "release_eligible": False,
            "validation_status": plan["evidence_guide_v2_architecture_development"][
                "validation_status"
            ],
            "practitioner_usefulness_status": "not_measured",
            "energy_status": "not_measured",
            "ecological_outcomes_status": "not_measured",
            "links": plan["evidence_guide_v2_architecture_development"]["links"],
            "artifact_references": plan["evidence_guide_v2_architecture_development"][
                "artifact_references"
            ],
        },
        "evidence_guide_r4c_grounding": {
            "study_version": plan["evidence_guide_r4c_grounding"]["study_version"],
            "available": plan["evidence_guide_r4c_grounding"]["available"],
            "snapshot_sha256": plan["evidence_guide_r4c_grounding"]["snapshot_sha256"],
            "state": plan["evidence_guide_r4c_grounding"]["state"],
            "production_decision": "DISABLE_LIVE_GUIDE",
            "release_eligible": False,
            "evaluation_scope": "synthetic_post_retrieval_automated_conformance",
            "practitioner_usefulness_status": "not_measured",
            "hydraulic_validity_status": "not_measured",
            "ecological_value_status": "not_measured",
            "failed_r4_protocol_incident": plan["evidence_guide_r4c_grounding"][
                "failed_r4_protocol_incident"
            ],
            "links": plan["evidence_guide_r4c_grounding"]["links"],
            "artifact_references": plan["evidence_guide_r4c_grounding"][
                "artifact_references"
            ],
        },
        "realistic_case_study_id": plan["realistic_case_study"]["case_id"],
        "realistic_case_study_sha256": plan["realistic_case_study"]["sha256"],
        "realistic_case_study_media_sha256": plan["realistic_case_study_media"]["manifest_sha256"],
        "realistic_case_study_fresh_swmm_rerun": False,
        "realistic_case_study_publication": {
            "receipt_sha256": plan["realistic_case_study_publication"]["receipt_sha256"],
            "project_id": plan["realistic_case_study_publication"]["project_id"],
            "project_access": "RESTRICTED",
            "publication_digest": plan["realistic_case_study_publication"]["publication_digest"],
            "run_id": plan["realistic_case_study_publication"]["run_id"],
            "run_url": plan["realistic_case_study_publication"]["run_url"],
            "artifact": plan["realistic_case_study_publication"]["artifact"],
            "weave_root_call_id": plan["realistic_case_study_publication"]["weave_root_call_id"],
            "weave_trace_id": plan["realistic_case_study_publication"]["weave_trace_id"],
            "weave_trace_url": plan["realistic_case_study_publication"]["weave_trace_url"],
            "weave_readback_verified": True,
            "wandb_run_association_verified": True,
            "wandb_run_association": plan["realistic_case_study_publication"][
                "wandb_run_association"
            ],
            "fresh_swmm_rerun": False,
            "source_authority": plan["realistic_case_study_publication"]["source_authority"],
            "bounded_claim_sha256": plan["realistic_case_study_publication"][
                "bounded_claim_sha256"
            ],
            "chart_sha256": plan["realistic_case_study_publication"]["chart_sha256"],
            "table_sha256": plan["realistic_case_study_publication"]["table_sha256"],
            "run_file_privacy_audit": plan["realistic_case_study_publication"][
                "run_file_privacy_audit"
            ],
        },
        "report_id": saved.id,
        "report_url": saved.url,
        "report_block_count": len(blocks),
        "run_file_privacy_audit": {"audited_runs": 3, "prohibited_files": 0},
        "public_share_link_enabled": False,
    }
    guide_publications = {
        candidate["model_id"]: {
            "evaluation_url": candidate["publication"]["evaluation_url"],
            "dataset_ref": candidate["publication"]["dataset_ref"],
            "trace_urls": candidate["publication"]["trace_urls"],
        }
        for candidate in plan["evidence_guide_evaluation"]["candidates"]
        if candidate["publication"] is not None
    }
    if guide_publications:
        receipt["evidence_guide_publications"] = guide_publications
        receipt["evidence_guide_campaign_publication"] = {
            "campaign_evidence_ref": plan["evidence_guide_evaluation"]["campaign_publication"][
                "campaign_evidence_ref"
            ],
            "receipt_sha256": plan["evidence_guide_evaluation"]["campaign_publication"][
                "receipt_sha256"
            ],
        }
    receipt["evidence_guide_agents_probe"] = {
        "receipt_sha256": plan["evidence_guide_evaluation"]["agent_probe"]["receipt_sha256"],
        "production_release_gate_overridden": True,
        "production_release_gate_decision": "DISABLE_LIVE_GUIDE",
        "rejected_count": plan["evidence_guide_evaluation"]["agent_probe"]["rejected_count"],
        "conversation_urls": [
            conversation["url"]
            for conversation in plan["evidence_guide_evaluation"]["agent_probe"]["conversations"]
        ],
    }
    receipt["receipt_sha256"] = _json_sha256(receipt)
    _write_json_atomic(PUBLICATION_RECEIPT, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--entity", default=os.environ.get("WANDB_REPORT_ENTITY") or os.environ.get("WANDB_ENTITY")
    )
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    parser.add_argument("--report-url", default=os.environ.get("WANDB_REPORT_URL"))
    parser.add_argument("--publish", action="store_true", help="perform the external W&B writes")
    parser.add_argument(
        "--allow-create-project",
        action="store_true",
        help="create the exact requested project, then require it to report RESTRICTED access",
    )
    arguments = parser.parse_args()
    if not arguments.entity or not arguments.project:
        parser.error(
            "--entity and --project are required; the publisher never guesses a destination"
        )
    plan = build_plan(arguments.entity, arguments.project)
    if arguments.publish:
        result = publish(
            plan,
            allow_create_project=arguments.allow_create_project,
            requested_report_url=arguments.report_url,
        )
        print(json.dumps({"report_url": result["report_url"], "run_url": result["run_url"]}))
    else:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "network_calls": 0,
                    "plan": str(PUBLICATION_PLAN),
                    "clip_count": plan["clip_count"],
                    "animation_clip_count": plan["animation_clip_count"],
                    "evidence_guide_candidate_count": len(
                        plan["evidence_guide_evaluation"]["candidates"]
                    ),
                    "evidence_guide_decision": plan["evidence_guide_evaluation"]["decision"],
                    "evidence_guide_publication_receipts": plan["evidence_guide_evaluation"][
                        "publication_receipt_count"
                    ],
                    "evidence_guide_agents_probe_receipts": plan["evidence_guide_evaluation"][
                        "agent_probe_receipt_count"
                    ],
                    "evidence_guide_agents_probe_rejections": plan["evidence_guide_evaluation"][
                        "agent_probe"
                    ]["rejected_count"],
                    "evidence_guide_v2_state": plan["evidence_guide_v2_evaluation"]["state"],
                    "evidence_guide_v2_decision": plan["evidence_guide_v2_evaluation"]["decision"],
                    "evidence_guide_v2_automated_explanation_quality": plan[
                        "evidence_guide_v2_evaluation"
                    ]["automated_explanation_quality"],
                    "evidence_guide_v2_practitioner_usefulness": "not_measured",
                    "evidence_guide_v2_canary_attempt_count": plan[
                        "evidence_guide_v2_evaluation"
                    ]["canary_attempt_count"],
                    "evidence_guide_v2_scientific_denominator_attempts": plan[
                        "evidence_guide_v2_evaluation"
                    ]["scientific_denominator_attempts"],
                    "evidence_guide_v2_architecture_denominator_attempts": plan[
                        "evidence_guide_v2_evaluation"
                    ]["architecture_denominator_attempts"],
                    "evidence_guide_v2_comparative_inference_permitted": False,
                    "evidence_guide_v2_hidden_attempt_rows_published": False,
                    "evidence_guide_v2_zero_inference_incident": plan[
                        "evidence_guide_v2_evaluation"
                    ]["operational_incident"],
                    "evidence_guide_v2_architecture_development_available": plan[
                        "evidence_guide_v2_architecture_development"
                    ]["available"],
                    "evidence_guide_v2_architecture_development_state": plan[
                        "evidence_guide_v2_architecture_development"
                    ]["state"],
                    "evidence_guide_v2_architecture_development_decision": (
                        "DISABLE_LIVE_GUIDE"
                    ),
                    "evidence_guide_v2_architecture_development_validation_status": plan[
                        "evidence_guide_v2_architecture_development"
                    ]["validation_status"],
                    "evidence_guide_v2_architecture_development_practitioner_usefulness": (
                        "not_measured"
                    ),
                    "evidence_guide_r4c_grounding_available": plan[
                        "evidence_guide_r4c_grounding"
                    ]["available"],
                    "evidence_guide_r4c_grounding_state": plan[
                        "evidence_guide_r4c_grounding"
                    ]["state"],
                    "evidence_guide_r4c_grounding_decision": "DISABLE_LIVE_GUIDE",
                    "evidence_guide_r4c_grounding_evaluation_scope": (
                        "synthetic_post_retrieval_automated_conformance"
                    ),
                    "evidence_guide_r4c_grounding_practitioner_usefulness": "not_measured",
                    "evidence_guide_r4c_grounding_hydraulic_validity": "not_measured",
                    "evidence_guide_r4c_grounding_ecological_value": "not_measured",
                    "realistic_case_study_id": plan["realistic_case_study"]["case_id"],
                    "realistic_case_study_sha256": plan["realistic_case_study"]["sha256"],
                    "realistic_case_publication_receipt_sha256": plan[
                        "realistic_case_study_publication"
                    ]["receipt_sha256"],
                    "realistic_case_publication_run_url": plan["realistic_case_study_publication"][
                        "run_url"
                    ],
                    "realistic_case_weave_trace_url": plan["realistic_case_study_publication"][
                        "weave_trace_url"
                    ],
                }
            )
        )


if __name__ == "__main__":
    main()
