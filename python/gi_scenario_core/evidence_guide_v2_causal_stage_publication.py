"""Fail-closed publication audit for the completed R3 causal stage.

The live R3 database contains every preregistered causal-stage attempt, but a
completed execution is not automatically a publishable causal result.  This
module reopens the database read-only, proves its exact preregistration and
schedule bindings, calls the repository's canonical
``analyze_research_rows`` gate, and emits either a bounded causal-stage
publication or an explicit publication-blocking integrity diagnostic.

The current R3 checkpoint is expected to take the latter path.  A0xF provider
failures did not retain the operation-sequence and protocol-shape fields needed
to establish orchestration equivalence across the three intervention
conditions.  Those records remain useful operational evidence, but must not be
silently promoted into causal evidence.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .evidence_guide_v2_architecture_dev import (
    CAUSAL_ABLATIONS,
    analyze_research_rows,
    extract_research_branches,
)
from .evidence_guide_v2_campaign import file_sha256, json_sha256, write_json_atomic

SCHEMA_VERSION = "1.0"
STUDY_VERSION = "evidence-guide-v2-architecture-development-r3"
PREREGISTRATION_VERSION = (
    "evidence-guide-v2-architecture-development-preregistration-r3"
)
EXPECTED_PREREGISTRATION_SHA256 = (
    "a636b6e7258ae96bec2b539346dc62582302a24a33af79d583fb06ac3ac71a9b"
)
EXPECTED_STUDY_INSTANCE_SHA256 = (
    "5c03eba98d5c6072df6b18ce6c3fe4d3e8e7fbe753f9601c5e5b3192e94d5fe6"
)
EXPECTED_SCHEDULE_SHA256 = (
    "9cc220c73652779963e2837fef92292afea2fe695d63c9b35ac78854aa6b147d"
)
EXPECTED_CAUSAL_ATTEMPTS = 1_344
EXPECTED_CAUSAL_LEDGER_ATTEMPTS = 1_152
EXPECTED_TIME_BLOCKS = ("block-1", "block-2", "block-3")
EXPECTED_EXECUTION_MODE = "live"
EXPECTED_TARGET = {
    "entity": "wandb",
    "kind": "wandb_weave",
    "project": "gi-scenario-studio-agent-evals",
}
REQUESTED_SUCCESS_DISPOSITION = (
    "PREREGISTERED_CAUSAL_STAGE_COMPLETE_BROADER_STUDY_INCOMPLETE"
)
BLOCKED_DISPOSITION = (
    "CAUSAL_STAGE_PUBLICATION_BLOCKED_SEMANTIC_INTERVENTION_TRIAD_DIRTY"
)
ANALYZER_ERROR = "RESEARCH_SEMANTIC_INTERVENTION_TRIAD_DIRTY"
PRODUCTION_DECISION = "DISABLE_LIVE_GUIDE"
_SHA256 = re.compile(r"[0-9a-f]{64}")

CLAIM_BOUNDARY = (
    "This package establishes execution completeness and a publication-blocking "
    "metadata-integrity finding only. It makes no model-ranking, practitioner-"
    "usefulness, hydraulic, ecological, or production-release claim."
)


class CausalStagePublicationError(RuntimeError):
    """Raised when the exact live checkpoint cannot be safely interpreted."""


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CausalStagePublicationError(f"expected JSON object: {path.name}")
    return value


def _canonical_body(value: Mapping[str, Any], digest_key: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != digest_key}


def _source_snapshot(root: Path) -> dict[str, Any]:
    files = {
        "campaign_sqlite": root / "campaign.sqlite",
        "campaign_sqlite_wal": root / "campaign.sqlite-wal",
        "preregistration": root / "preregistration.json",
        "research_plan": root / "research-plan.json",
        "attempt_schedule": root / "attempt-schedule.json",
        "execution_receipt": root / "execution-receipt.json",
    }
    snapshot: dict[str, Any] = {}
    for label, path in files.items():
        if not path.is_file():
            if label == "campaign_sqlite_wal":
                # SQLite may create or remove a zero-byte WAL while negotiating
                # a read-only connection.  Treat absent and empty as the same
                # evidence state; any material WAL remains hash-bound.
                snapshot[label] = {
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "size_bytes": 0,
                }
                continue
            raise FileNotFoundError(path)
        snapshot[label] = {
            "sha256": file_sha256(path),
            "size_bytes": path.stat().st_size,
        }
    return snapshot


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise CausalStagePublicationError("SQLite query-only mode was not established")
    return connection


def _load_exact_checkpoint(root: Path) -> dict[str, Any]:
    preregistration_path = root / "preregistration.json"
    research_plan_path = root / "research-plan.json"
    schedule_path = root / "attempt-schedule.json"
    execution_receipt_path = root / "execution-receipt.json"
    preregistration = _read_object(preregistration_path)
    research_plan = _read_object(research_plan_path)
    schedule = _read_object(schedule_path)
    execution_receipt = _read_object(execution_receipt_path)

    preregistration_body = _canonical_body(
        preregistration, "preregistration_sha256"
    )
    schedule_records = schedule.get("attempts")
    if not isinstance(schedule_records, list):
        raise CausalStagePublicationError("R3 attempt schedule is not a list")
    expected_causal = {
        str(record.get("attempt_id"))
        for record in schedule_records
        if record.get("stage") == "causal_perturbation"
    }
    checks = {
        "preregistration_schema": preregistration.get("schema_version")
        == SCHEMA_VERSION,
        "preregistration_study": preregistration.get("study_version")
        == STUDY_VERSION,
        "preregistration_version": preregistration.get("preregistration_version")
        == PREREGISTRATION_VERSION,
        "preregistration_digest": preregistration.get("preregistration_sha256")
        == EXPECTED_PREREGISTRATION_SHA256
        == json_sha256(preregistration_body),
        "preregistration_mode": preregistration.get("execution_mode")
        == EXPECTED_EXECUTION_MODE,
        "study_instance": preregistration.get("study_instance_sha256")
        == EXPECTED_STUDY_INSTANCE_SHA256,
        "release_ineligible": preregistration.get("release_eligible") is False
        and preregistration.get("assistant_release_basis_may_be_created") is False,
        "schedule_schema": schedule.get("schema_version") == SCHEMA_VERSION,
        "schedule_study": schedule.get("study_version") == STUDY_VERSION,
        "schedule_instance": schedule.get("study_instance_sha256")
        == EXPECTED_STUDY_INSTANCE_SHA256,
        "schedule_digest": schedule.get("schedule_sha256")
        == EXPECTED_SCHEDULE_SHA256
        == json_sha256(schedule_records),
        "causal_schedule_count": len(expected_causal) == EXPECTED_CAUSAL_ATTEMPTS,
        "research_plan_binding": research_plan.get("preregistration_sha256")
        == EXPECTED_PREREGISTRATION_SHA256
        and research_plan.get("schedule_sha256") == EXPECTED_SCHEDULE_SHA256
        and research_plan.get("preregistration_file_sha256")
        == file_sha256(preregistration_path)
        and research_plan.get("schedule_file_sha256") == file_sha256(schedule_path),
        "research_plan_mode": research_plan.get("execution_mode")
        == EXPECTED_EXECUTION_MODE,
        "execution_receipt_mode": execution_receipt.get("execution_mode")
        == EXPECTED_EXECUTION_MODE,
        "execution_receipt_scope": execution_receipt.get("attempt_count")
        == EXPECTED_CAUSAL_ATTEMPTS
        and execution_receipt.get("completed_all") is False
        and execution_receipt.get("release_eligible") is False
        and execution_receipt.get("assistant_release_basis_created") is False,
        "execution_receipt_context": execution_receipt.get("execution_context")
        == {
            "schema_version": "1.0",
            "mode": EXPECTED_EXECUTION_MODE,
            "study_instance_sha256": EXPECTED_STUDY_INSTANCE_SHA256,
            "destination": EXPECTED_TARGET,
        },
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise CausalStagePublicationError(
            "R3 checkpoint binding failed: " + ", ".join(failed)
        )

    database = root / "campaign.sqlite"
    connection = _open_read_only(database)
    try:
        metadata = {
            str(row["key"]): json.loads(row["value_json"])
            for row in connection.execute(
                "SELECT key, value_json FROM campaign_meta ORDER BY key"
            )
        }
        sessions = [
            dict(row)
            for row in connection.execute(
                "SELECT stage, time_block, session_id, started_at, completed_at "
                "FROM stage_sessions ORDER BY stage, time_block"
            )
        ]
        ledger = [
            dict(row)
            for row in connection.execute(
                "SELECT stage, state, COUNT(*) AS attempt_count, "
                "SUM(logical_provider_calls) AS logical_provider_calls, "
                "SUM(worst_case_transport_attempts) AS worst_case_transport_attempts "
                "FROM attempt_execution_ledger GROUP BY stage, state "
                "ORDER BY stage, state"
            )
        ]
        attempt_rows = []
        for row in connection.execute(
            "SELECT attempt_id, stage, hidden, record_json, completed_at "
            "FROM attempts ORDER BY attempt_id"
        ):
            record = json.loads(row["record_json"])
            if not isinstance(record, dict):
                raise CausalStagePublicationError("attempt record is not an object")
            attempt_rows.append({**dict(row), "record": record})
    finally:
        connection.close()

    context = metadata.get("architecture_execution_context")
    if metadata.get("preregistration") != preregistration:
        raise CausalStagePublicationError("SQLite preregistration binding drifted")
    if metadata.get("attempt_schedule_sha256") != EXPECTED_SCHEDULE_SHA256:
        raise CausalStagePublicationError("SQLite schedule binding drifted")
    if context != {
        "schema_version": "1.0",
        "mode": EXPECTED_EXECUTION_MODE,
        "study_instance_sha256": EXPECTED_STUDY_INSTANCE_SHA256,
        "destination": EXPECTED_TARGET,
    }:
        raise CausalStagePublicationError("SQLite execution context drifted")

    observed_ids = {str(row["attempt_id"]) for row in attempt_rows}
    if len(attempt_rows) != EXPECTED_CAUSAL_ATTEMPTS or observed_ids != expected_causal:
        raise CausalStagePublicationError("R3 causal attempt set is incomplete or unexpected")
    noncausal = [
        row
        for row in attempt_rows
        if row["stage"] != "causal_perturbation"
        or row["record"].get("stage") != "causal_perturbation"
    ]
    if noncausal:
        raise CausalStagePublicationError(
            "noncausal rows entered the causal-stage observed set"
        )
    if any(bool(row["hidden"]) or row["record"].get("hidden") is not False for row in attempt_rows):
        raise CausalStagePublicationError("hidden rows entered the causal-stage observed set")

    expected_sessions = {
        ("causal_perturbation", block) for block in EXPECTED_TIME_BLOCKS
    }
    observed_sessions = {(row["stage"], row["time_block"]) for row in sessions}
    if (
        observed_sessions != expected_sessions
        or any(not row["completed_at"] for row in sessions)
        or len({row["session_id"] for row in sessions}) != len(EXPECTED_TIME_BLOCKS)
    ):
        raise CausalStagePublicationError("R3 causal time-block sessions are incomplete")
    if ledger != [
        {
            "stage": "causal_perturbation",
            "state": "completed",
            "attempt_count": EXPECTED_CAUSAL_LEDGER_ATTEMPTS,
            "logical_provider_calls": EXPECTED_CAUSAL_LEDGER_ATTEMPTS,
            "worst_case_transport_attempts": EXPECTED_CAUSAL_LEDGER_ATTEMPTS * 2,
        }
    ]:
        raise CausalStagePublicationError("R3 causal execution ledger drifted")

    return {
        "preregistration": preregistration,
        "research_plan": research_plan,
        "schedule": schedule,
        "execution_receipt": execution_receipt,
        "sessions": sessions,
        "ledger": ledger,
        "records": [row["record"] for row in attempt_rows],
        "broader_schedule_attempt_count": len(schedule_records),
        "broader_noncausal_schedule_attempt_count": len(schedule_records)
        - EXPECTED_CAUSAL_ATTEMPTS,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _diagnose_semantic_triads(
    branches: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    cells: dict[tuple[str, str, str, int, str, str], Mapping[str, Any]] = {}
    for row in branches:
        if (
            row.get("arm") not in {"D1", "A1", "A3P", "A0xF"}
            or row.get("scenario_type") != "causal_retrieval"
            or row.get("stage") != "causal_perturbation"
        ):
            continue
        condition = str(row.get("ablation_condition") or "")
        if condition not in CAUSAL_ABLATIONS:
            continue
        key = (
            str(row.get("model_id")),
            str(row.get("split")),
            str(row.get("case_id")),
            int(row.get("presentation") or 0),
            str(row.get("arm")),
            condition,
        )
        if key in cells:
            raise CausalStagePublicationError("duplicate causal intervention cell")
        cells[key] = row

    triad_keys = sorted({key[:-1] for key in cells})
    receipt_fields = (
        "anchor_sha256",
        "tool_name",
        "opaque_evidence_id",
        "units",
        "evidence_binding",
        "orchestration_contract_sha256",
    )
    triads = []
    for key in triad_keys:
        rows = [cells.get((*key, condition)) for condition in CAUSAL_ABLATIONS]
        complete = all(row is not None for row in rows)
        present = [row for row in rows if row is not None]
        sequences = [tuple(row.get("operation_sequence") or ()) for row in present]
        shapes = [str(row.get("protocol_shape_id") or "") for row in present]
        orchestration_equivalent = bool(
            complete
            and len(set(sequences)) == 1
            and len(set(shapes)) == 1
            and all(sequences)
            and all(shapes)
        )
        receipts = [
            _mapping(row.get("semantic_intervention_receipt")) for row in present
        ]
        receipt_valid = bool(
            complete
            and len(receipts) == 3
            and all(row.get("semantic_intervention_receipt_valid") is True for row in present)
        )
        invariants_equal = bool(
            receipt_valid
            and all(
                len({str(receipt.get(field)) for receipt in receipts}) == 1
                for field in receipt_fields
            )
            and len(
                {
                    json_sha256(dict(_mapping(receipt.get("triad_identity"))))
                    for receipt in receipts
                }
            )
            == 1
        )
        semantic_delta_exact = bool(
            invariants_equal
            and receipts[0].get("condition") == "evidence_original"
            and receipts[1].get("condition")
            == "counterfactual_threshold_crossing"
            and receipts[2].get("condition") == "evidence_withheld"
            and receipts[0].get("intervention_value")
            == receipts[0].get("original_value")
            and receipts[1].get("intervention_value")
            != receipts[1].get("original_value")
            and receipts[2].get("intervention_value") is None
            and receipts[0].get("evidence_available") is True
            and receipts[1].get("evidence_available") is True
            and receipts[2].get("evidence_available") is False
        )
        reasons = []
        if not complete:
            reasons.append("incomplete_triad")
        if not orchestration_equivalent:
            reasons.append("orchestration_equivalence_unproven")
        if not receipt_valid:
            reasons.append("semantic_receipt_invalid")
        if not invariants_equal:
            reasons.append("semantic_invariants_drifted")
        if not semantic_delta_exact:
            reasons.append("semantic_delta_not_exact")
        clean = not reasons
        triads.append(
            {
                "model_id": key[0],
                "split": key[1],
                "case_id": key[2],
                "presentation": key[3],
                "arm": key[4],
                "complete": complete,
                "orchestration_equivalent": orchestration_equivalent,
                "semantic_receipts_valid": receipt_valid,
                "semantic_invariants_equal": invariants_equal,
                "semantic_delta_exact": semantic_delta_exact,
                "semantic_intervention_clean": clean,
                "reason_codes": reasons,
                "operation_sequences": [list(sequence) for sequence in sequences],
                "protocol_shape_ids": [shape or None for shape in shapes],
                "trace_urls": [
                    str(row.get("trace_url"))
                    for row in present
                    if row.get("trace_url")
                ],
            }
        )

    dirty = [row for row in triads if not row["semantic_intervention_clean"]]
    breakdown = Counter(
        (row["model_id"], row["split"], row["arm"]) for row in dirty
    )
    return {
        "triad_count": len(triads),
        "clean_triad_count": len(triads) - len(dirty),
        "dirty_triad_count": len(dirty),
        "all_triads_complete": bool(triads) and all(row["complete"] for row in triads),
        "all_semantic_interventions_clean": bool(triads) and not dirty,
        "reason_counts": dict(
            sorted(
                Counter(reason for row in dirty for reason in row["reason_codes"]).items()
            )
        ),
        "dirty_breakdown": [
            {
                "model_id": model,
                "split": split,
                "arm": arm,
                "dirty_triads": count,
            }
            for (model, split, arm), count in sorted(breakdown.items())
        ],
        "dirty_triads": dirty,
    }


def _percentile(values: Sequence[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(proportion * len(ordered)) - 1))
    return ordered[index]


def _administrative_facts(
    records: Sequence[Mapping[str, Any]], checkpoint: Mapping[str, Any]
) -> dict[str, Any]:
    verified = Decimal(0)
    at_risk = Decimal(0)
    traces = []
    errors: Counter[str] = Counter()
    latencies = []
    for row in records:
        details = _mapping(row.get("details"))
        accounting = _mapping(details.get("cost_accounting"))
        verified += Decimal(
            str(accounting.get("verified_provider_usage_cost_usd") or "0")
        )
        at_risk += Decimal(
            str(accounting.get("conservative_at_risk_cost_usd") or "0")
        )
        trace = _mapping(details.get("weave_turn_receipt"))
        if trace.get("url"):
            traces.append(
                {
                    "attempt_id": str(row.get("attempt_id")),
                    "model_id": str(row.get("model_id")),
                    "arm": str(row.get("architecture_id")),
                    "condition": str(row.get("ablation_condition")),
                    "trace_url": str(trace.get("url")),
                }
            )
        errors.update(str(code) for code in row.get("error_codes") or ())
        latencies.append(float(row.get("latency_ms") or 0.0))
    return {
        "observed_attempt_count": len(records),
        "observed_stage": "causal_perturbation",
        "completed_time_blocks": len(checkpoint["sessions"]),
        "scheduled_time_blocks": len(EXPECTED_TIME_BLOCKS),
        "broader_schedule_attempt_count": checkpoint[
            "broader_schedule_attempt_count"
        ],
        "broader_noncausal_schedule_attempt_count": checkpoint[
            "broader_noncausal_schedule_attempt_count"
        ],
        "observed_noncausal_attempt_count": 0,
        "status_counts": dict(sorted(Counter(str(row.get("status")) for row in records).items())),
        "model_counts": dict(sorted(Counter(str(row.get("model_id")) for row in records).items())),
        "arm_counts": dict(
            sorted(Counter(str(row.get("architecture_id")) for row in records).items())
        ),
        "condition_counts": dict(
            sorted(Counter(str(row.get("ablation_condition")) for row in records).items())
        ),
        "repeat_counts": dict(
            sorted(Counter(str(row.get("repeat")) for row in records).items())
        ),
        "failure_code_counts": dict(sorted(errors.items())),
        "verified_provider_usage_cost_usd": str(verified),
        "conservative_at_risk_cost_usd": str(at_risk),
        "verified_cost_basis": (
            "pricing snapshot times provider-reported tokens from completed responses"
        ),
        "at_risk_cost_basis": (
            "pricing snapshot times retry and provider-error token envelopes; not billed cost"
        ),
        "latency_ms": {
            "median": median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "trace_reference_count": len(traces),
        "trace_references": sorted(traces, key=lambda row: row["attempt_id"]),
        "interpretation": (
            "Administrative execution and resource facts only; no conformance or causal "
            "effect estimate is published because the semantic-intervention gate failed."
        ),
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    temporary.replace(path)


def _font(root: Path, name: str) -> Path:
    path = root / "media" / "gi-scenario-film" / "assets" / "fonts" / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _draw_blocked_panel(
    output: Path,
    *,
    repository_root: Path,
    diagnostic: Mapping[str, Any],
    administrative: Mapping[str, Any],
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1920, 1080
    paper = "#F5F7F6"
    ink = "#17313A"
    teal = "#08766F"
    rain = "#2F6F9F"
    ochre = "#A96315"
    red = "#B43D47"
    muted = "#61747B"
    white = "#FFFFFF"
    image = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(image)
    regular = _font(repository_root, "AtkinsonHyperlegible-Regular.ttf")
    bold = _font(repository_root, "AtkinsonHyperlegible-Bold.ttf")
    condensed = _font(repository_root, "IBMPlexSansCondensed-SemiBold.ttf")
    mono = _font(repository_root, "IBMPlexMono-Medium.ttf")
    fonts = {
        "eyebrow": ImageFont.truetype(str(mono), 26),
        "headline": ImageFont.truetype(str(condensed), 76),
        "subtitle": ImageFont.truetype(str(regular), 31),
        "metric": ImageFont.truetype(str(condensed), 60),
        "label": ImageFont.truetype(str(bold), 28),
        "body": ImageFont.truetype(str(regular), 27),
        "small": ImageFont.truetype(str(regular), 22),
    }

    draw.rounded_rectangle((82, 62, 646, 112), radius=25, fill="#E3ECEB")
    draw.text((108, 72), "EVIDENCE GUIDE R3 · INTEGRITY AUDIT", font=fonts["eyebrow"], fill=teal)
    draw.text((82, 142), "The run finished.", font=fonts["headline"], fill=ink)
    draw.text((82, 225), "The causal publication did not.", font=fonts["headline"], fill=red)
    draw.text(
        (86, 326),
        "Completeness is necessary. Reconstructable experimental identity is necessary too.",
        font=fonts["subtitle"],
        fill=muted,
    )

    cards = (
        (82, 405, 548, 638, "EXECUTION RECORD", "1,344", "live causal-stage records", teal),
        (578, 405, 1044, 638, "TIME BLOCKS", "3 / 3", "sessions marked complete", rain),
        (1074, 405, 1838, 638, "INTEGRITY GATE", "121 / 448", "matched triads cannot prove equivalent orchestration", red),
    )
    for x1, y1, x2, y2, label, metric, body, color in cards:
        draw.rounded_rectangle((x1, y1, x2, y2), radius=22, fill=white, outline="#D4DEDF", width=2)
        draw.text((x1 + 28, y1 + 26), label, font=fonts["eyebrow"], fill=color)
        draw.text((x1 + 28, y1 + 73), metric, font=fonts["metric"], fill=ink)
        draw.text((x1 + 28, y1 + 160), body, font=fonts["small"], fill=muted)

    draw.rounded_rectangle((82, 680, 1838, 884), radius=22, fill="#FFF5EA", outline="#E4B989", width=2)
    draw.text((112, 710), "WHY THE GATE STOPPED", font=fonts["label"], fill=ochre)
    draw.text(
        (112, 758),
        "121 free-prose triads do not retain one consistent operation sequence",
        font=fonts["body"],
        fill=ink,
    )
    draw.text(
        (112, 800),
        "and protocol shape across all three evidence conditions:",
        font=fonts["body"],
        fill=ink,
    )
    draw.text(
        (112, 842),
        "original, threshold-crossing, and withheld.",
        font=fonts["body"],
        fill=ink,
    )
    breakdown = {
        (row["model_id"], row["split"]): row["dirty_triads"]
        for row in diagnostic["dirty_breakdown"]
    }
    labels = (
        ("Granite · development", breakdown.get(("ibm-granite/granite-4.1-8b", "development"), 0)),
        ("Granite · validation", breakdown.get(("ibm-granite/granite-4.1-8b", "validation"), 0)),
        ("Qwen · development", breakdown.get(("Qwen/Qwen3-30B-A3B-Instruct-2507", "development"), 0)),
        ("Qwen · validation", breakdown.get(("Qwen/Qwen3-30B-A3B-Instruct-2507", "validation"), 0)),
    )
    x = 1110
    draw.text((x, 708), "DIRTY TRIADS", font=fonts["eyebrow"], fill=red)
    for index, (label, value) in enumerate(labels):
        y = 746 + index * 34
        draw.text((x, y), label, font=fonts["small"], fill=muted)
        draw.rounded_rectangle((1410, y + 3, 1720, y + 27), radius=12, fill="#E9D6C5")
        draw.rounded_rectangle(
            (1410, y + 3, 1410 + int(310 * value / 32), y + 27), radius=12, fill=red
        )
        draw.text((1740, y - 1), f"{value}/32", font=fonts["small"], fill=ink)

    draw.line((82, 934, 1838, 934), fill="#CBD7D9", width=2)
    draw.text(
        (82, 956),
        "NO CAUSAL EFFECT TABLE · NO MODEL RANKING · LIVE GUIDE REMAINS DISABLED",
        font=fonts["eyebrow"],
        fill=red,
    )
    draw.text(
        (82, 1001),
        "Finite-plan A1/A3P outputs would reflect repository constraint propagation—not model reasoning. "
        "No practitioner, hydraulic, or ecological claim is made.",
        font=fonts["small"],
        fill=muted,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=False)


def _emit_blocked_package(
    *,
    campaign_root: Path,
    output_dir: Path,
    repository_root: Path,
    checkpoint: Mapping[str, Any],
    source_before: Mapping[str, Any],
    analyzer_error: str,
) -> dict[str, Any]:
    records = checkpoint["records"]
    branches = extract_research_branches(records)
    diagnostic = _diagnose_semantic_triads(branches)
    if diagnostic["dirty_triad_count"] == 0:
        raise CausalStagePublicationError(
            "analyzer rejected the study but the diagnostic found no dirty triad"
        )
    administrative = _administrative_facts(records, checkpoint)
    output_dir.mkdir(parents=True, exist_ok=True)

    diagnostic_summary = {
        key: value for key, value in diagnostic.items() if key != "dirty_triads"
    }
    write_json_atomic(
        output_dir / "semantic-integrity-summary.json",
        {
            "schema_version": SCHEMA_VERSION,
            "analyzer": "analyze_research_rows",
            "analyzer_outcome": "blocked",
            "analyzer_error": analyzer_error,
            "diagnostic": diagnostic_summary,
            "claim_boundary": CLAIM_BOUNDARY,
        },
    )
    write_json_atomic(output_dir / "administrative-run-facts.json", administrative)
    _write_csv(
        output_dir / "dirty-triads.csv",
        [
            {
                **{key: row[key] for key in ("model_id", "split", "case_id", "presentation", "arm")},
                "reason_codes": "|".join(row["reason_codes"]),
                "operation_sequences": json.dumps(row["operation_sequences"], separators=(",", ":")),
                "protocol_shape_ids": json.dumps(row["protocol_shape_ids"], separators=(",", ":")),
            }
            for row in diagnostic["dirty_triads"]
        ],
        (
            "model_id",
            "split",
            "case_id",
            "presentation",
            "arm",
            "reason_codes",
            "operation_sequences",
            "protocol_shape_ids",
        ),
    )
    _write_csv(
        output_dir / "trace-references.csv",
        administrative["trace_references"],
        ("attempt_id", "model_id", "arm", "condition", "trace_url"),
    )
    panel_rows = [
        {
            "item": "observed causal-stage records",
            "value": EXPECTED_CAUSAL_ATTEMPTS,
            "interpretation": "execution completeness only",
            "boundary": "not broader-study completion",
        },
        {
            "item": "completed time-block sessions",
            "value": "3/3",
            "interpretation": "all preregistered causal sessions marked complete",
            "boundary": "not causal-result validity",
        },
        {
            "item": "matched triads",
            "value": diagnostic["triad_count"],
            "interpretation": "identity keys have all three conditions",
            "boundary": "orchestration equivalence still required",
        },
        {
            "item": "publication-blocking triads",
            "value": diagnostic["dirty_triad_count"],
            "interpretation": "equivalent orchestration unproven",
            "boundary": "no causal effect table emitted",
        },
    ]
    _write_csv(
        output_dir / "article-panel.csv",
        panel_rows,
        ("item", "value", "interpretation", "boundary"),
    )
    _draw_blocked_panel(
        output_dir / "article-panel.png",
        repository_root=repository_root,
        diagnostic=diagnostic,
        administrative=administrative,
    )

    source_after = _source_snapshot(campaign_root)
    if source_after != source_before:
        raise CausalStagePublicationError("live R3 source files changed during read-only audit")
    publication_sources = {
        "publication_module": file_sha256(Path(__file__).resolve()),
        "canonical_analyzer": file_sha256(
            repository_root
            / "python"
            / "gi_scenario_core"
            / "evidence_guide_v2_architecture_dev.py"
        ),
    }
    file_names = (
        "semantic-integrity-summary.json",
        "administrative-run-facts.json",
        "dirty-triads.csv",
        "trace-references.csv",
        "article-panel.csv",
        "article-panel.png",
    )
    output_hashes = {
        name: file_sha256(output_dir / name) for name in file_names
    }
    receipt_body = {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": "evidence_guide_r3_causal_stage_publication_gate",
        "disposition": BLOCKED_DISPOSITION,
        "requested_success_disposition": REQUESTED_SUCCESS_DISPOSITION,
        "publication_created": False,
        "article_publication_permitted": False,
        "network_calls": 0,
        "source_database_open_mode": "sqlite_uri_mode_ro_query_only",
        "source_database_modified": False,
        "study_version": STUDY_VERSION,
        "execution_mode": EXPECTED_EXECUTION_MODE,
        "study_instance_sha256": EXPECTED_STUDY_INSTANCE_SHA256,
        "preregistration_sha256": EXPECTED_PREREGISTRATION_SHA256,
        "schedule_sha256": EXPECTED_SCHEDULE_SHA256,
        "observed_causal_attempt_count": EXPECTED_CAUSAL_ATTEMPTS,
        "completed_causal_time_blocks": list(EXPECTED_TIME_BLOCKS),
        "broader_schedule_attempt_count": checkpoint[
            "broader_schedule_attempt_count"
        ],
        "broader_noncausal_schedule_attempt_count": checkpoint[
            "broader_noncausal_schedule_attempt_count"
        ],
        "observed_noncausal_attempt_count": 0,
        "broader_study_complete": False,
        "analyzer_gate": {
            "function": "analyze_research_rows",
            "outcome": "raised",
            "error_code": analyzer_error,
            "dirty_triad_count": diagnostic["dirty_triad_count"],
            "matched_triad_count": diagnostic["triad_count"],
            "causal_conformance_tables_emitted": False,
            "wilson_intervals_emitted": False,
            "ever_failed_across_repeats_emitted": False,
        },
        "suppressed_scientific_outputs": [
            "model_arm_condition_repeat_conformance",
            "causal_effect_or_plan_change_counts",
            "wilson_intervals",
            "ever_failed_across_repeats",
            "model_or_architecture_ranking",
        ],
        "administrative_resource_facts": {
            key: administrative[key]
            for key in (
                "verified_provider_usage_cost_usd",
                "conservative_at_risk_cost_usd",
                "verified_cost_basis",
                "at_risk_cost_basis",
                "latency_ms",
                "trace_reference_count",
            )
        },
        "release_eligible": False,
        "assistant_release_basis_created": False,
        "production_decision": PRODUCTION_DECISION,
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_validity_status": "not_measured",
        "ecological_outcome_status": "not_measured",
        "finite_arm_interpretation": (
            "A1/A3P finite-plan outputs encode repository constraint propagation and "
            "must not be attributed to model reasoning. No finite-arm result is "
            "published from this blocked checkpoint."
        ),
        "preregistered_source_hashes": checkpoint["preregistration"]["source_hashes"],
        "source_snapshot": source_before,
        "publication_source_hashes": publication_sources,
        "output_file_sha256": dict(sorted(output_hashes.items())),
        "claim_boundary": CLAIM_BOUNDARY,
    }
    receipt = {**receipt_body, "receipt_sha256": json_sha256(receipt_body)}
    write_json_atomic(output_dir / "receipt.json", receipt)
    return receipt


def build_causal_stage_publication(
    campaign_root: Path,
    *,
    output_dir: Path | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Audit R3 and emit a fail-closed diagnostic when analysis integrity fails."""

    campaign_root = campaign_root.resolve()
    repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    output_dir = (
        output_dir or campaign_root / "causal-stage-publication"
    ).resolve()
    source_before = _source_snapshot(campaign_root)
    checkpoint = _load_exact_checkpoint(campaign_root)
    try:
        # This canonical gate must not be bypassed or weakened.  The diagnostic
        # below is emitted only after the gate refuses to produce an analysis.
        analyze_research_rows(checkpoint["records"])
    except RuntimeError as error:
        if str(error) != ANALYZER_ERROR:
            raise
        return _emit_blocked_package(
            campaign_root=campaign_root,
            output_dir=output_dir,
            repository_root=repository_root,
            checkpoint=checkpoint,
            source_before=source_before,
            analyzer_error=str(error),
        )
    raise CausalStagePublicationError(
        "canonical analysis unexpectedly passed; the bounded success publisher "
        "must be reviewed before emitting the requested success disposition"
    )


def validate_blocked_receipt(receipt: Mapping[str, Any]) -> None:
    body = _canonical_body(receipt, "receipt_sha256")
    checks = (
        receipt.get("disposition") == BLOCKED_DISPOSITION,
        receipt.get("publication_created") is False,
        receipt.get("article_publication_permitted") is False,
        receipt.get("release_eligible") is False,
        receipt.get("assistant_release_basis_created") is False,
        receipt.get("production_decision") == PRODUCTION_DECISION,
        receipt.get("observed_causal_attempt_count") == EXPECTED_CAUSAL_ATTEMPTS,
        _mapping(receipt.get("analyzer_gate")).get("error_code") == ANALYZER_ERROR,
        int(_mapping(receipt.get("analyzer_gate")).get("dirty_triad_count") or 0)
        > 0,
        isinstance(receipt.get("receipt_sha256"), str),
        _SHA256.fullmatch(str(receipt.get("receipt_sha256"))) is not None,
        receipt.get("receipt_sha256") == json_sha256(body),
    )
    if not all(checks):
        raise CausalStagePublicationError("blocked causal-stage receipt drifted")


__all__ = [
    "ANALYZER_ERROR",
    "BLOCKED_DISPOSITION",
    "EXPECTED_CAUSAL_ATTEMPTS",
    "REQUESTED_SUCCESS_DISPOSITION",
    "CausalStagePublicationError",
    "build_causal_stage_publication",
    "validate_blocked_receipt",
]
