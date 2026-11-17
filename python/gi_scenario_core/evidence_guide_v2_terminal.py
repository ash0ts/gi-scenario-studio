"""Validate and seal an early, fail-closed Evidence Guide v2 campaign stop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .evidence_guide_v2_campaign import (
    DEFAULT_BUDGET_CAPS,
    STAGES,
    CampaignStore,
    json_sha256,
)

TERMINAL_RECEIPT_KIND = "evidence_guide_v2_terminal_disposition"
TERMINAL_RECEIPT_VERSION = "trace-canary-stop-1"


def _row(
    rows: Sequence[Mapping[str, Any]], architecture_id: str
) -> Mapping[str, Any]:
    selected = [row for row in rows if row.get("architecture_id") == architecture_id]
    if len(selected) != 1:
        raise RuntimeError(f"trace canary requires one {architecture_id} row")
    return selected[0]


def build_trace_canary_terminal_disposition(
    *,
    store: CampaignStore,
    preregistration: Mapping[str, Any],
    release_gate: Mapping[str, Any],
    finalizer_sha256: str,
) -> dict[str, Any]:
    """Build a terminal negative-result receipt without retrying inference."""

    canary = store.get_receipt("trace_canary") or {}
    canary_body = {key: value for key, value in canary.items() if key != "receipt_sha256"}
    rows = store.attempts("trace_canary")
    a1 = _row(rows, "A1")
    d1 = _row(rows, "D1")
    scientific_rows = [row for row in store.attempts() if row.get("stage") in STAGES]
    cloud_receipts = canary.get("cloud_block_receipts")
    checks = canary.get("checks")
    failed_checks = sorted(
        key for key, passed in checks.items() if passed is not True
    ) if isinstance(checks, Mapping) else []
    usage = store.usage()

    conditions = {
        "canary_receipt_digest_valid": bool(
            canary.get("receipt_sha256") == json_sha256(canary_body)
        ),
        "canary_completed_and_failed": bool(
            canary.get("attempt_count") == 4
            and canary.get("passed") is False
            and store.completed_time_blocks("trace_canary") == {"block-1"}
        ),
        "only_model_semantic_check_failed": failed_checks
        == ["a1_model_and_tool_spans_acknowledged"],
        "observability_acknowledged": bool(
            isinstance(checks, Mapping)
            and checks.get("all_trace_acknowledged") is True
            and checks.get("all_protocol_completed") is True
            and checks.get("three_exact_cloud_block_receipts") is True
            and isinstance(cloud_receipts, list)
            and len(cloud_receipts) == 3
        ),
        "a1_was_rejected_before_release": bool(
            a1.get("provider_invocations") == 1
            and a1.get("trace_acknowledged") is True
            and a1.get("safe") is False
            and a1.get("repository_fidelity") is False
            and a1.get("details", {}).get("answer_released") is False
        ),
        "d1_passed_same_question": bool(
            d1.get("provider_invocations") == 0
            and d1.get("safe") is True
            and d1.get("repository_fidelity") is True
            and d1.get("explanation_plan_id") == "authority_boundary_v1"
            and d1.get("details", {}).get("answer_released") is True
        ),
        "no_scientific_attempts": not scientific_rows,
        "no_uncertain_provider_attempt": not store.uncertain_attempts(),
        "architecture_not_started": not store.attempts("architecture"),
        "release_gate_disables_live_guide": bool(
            release_gate.get("decision") == "DISABLE_LIVE_GUIDE"
            and release_gate.get("selected_model") is None
            and release_gate.get("production_scope") == []
        ),
        "budget_caps_respected": bool(
            float(usage["cost_usd"]) <= float(DEFAULT_BUDGET_CAPS.cost_usd)
            and int(usage["provider_invocations"])
            <= DEFAULT_BUDGET_CAPS.provider_invocations
            and int(usage["input_tokens"]) <= DEFAULT_BUDGET_CAPS.input_tokens
            and int(usage["output_tokens"]) <= DEFAULT_BUDGET_CAPS.output_tokens
        ),
    }
    if not all(conditions.values()):
        raise RuntimeError("campaign does not satisfy the terminal canary-stop protocol")

    body = {
        "schema_version": "1.0",
        "receipt_kind": TERMINAL_RECEIPT_KIND,
        "receipt_version": TERMINAL_RECEIPT_VERSION,
        "decision": "DISABLE_LIVE_GUIDE",
        "active_guide": "D1_DETERMINISTIC_EVIDENCE_RAIL",
        "stopping_reason": "TRACE_CANARY_SEMANTIC_CONTRACT_FAILED",
        "blocked_before_stage": "architecture",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "trace_canary_receipt_sha256": canary["receipt_sha256"],
        "release_gate_digest": release_gate["release_gate_digest"],
        "finalizer_sha256": finalizer_sha256,
        "scientific_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "provider_answer_released": False,
        "failed_canary_checks": failed_checks,
        "a1_observation": {
            "model_id": a1.get("model_id"),
            "selected_plan_id": a1.get("explanation_plan_id"),
            "expected_plan_id": "authority_boundary_v1",
            "error_codes": a1.get("error_codes", []),
            "provider_invocations": a1.get("provider_invocations"),
            "transport_attempts": a1.get("transport_attempts"),
            "input_tokens": a1.get("input_tokens"),
            "output_tokens": a1.get("output_tokens"),
            "cost_usd": a1.get("cost_usd"),
            "trace_acknowledged": a1.get("trace_acknowledged"),
        },
        "d1_control": {
            "selected_plan_id": d1.get("explanation_plan_id"),
            "answer_released": d1.get("details", {}).get("answer_released"),
            "safe": d1.get("safe"),
            "repository_fidelity": d1.get("repository_fidelity"),
        },
        "usage": usage,
        "conditions": conditions,
        "practitioner_usefulness_status": "not_measured",
        "claim_boundary": (
            "The canary establishes fail-closed runtime behavior and one model-selection "
            "failure only. It does not estimate architecture efficacy, practitioner "
            "usefulness, hydraulic performance, or ecological benefit."
        ),
    }
    return {**body, "receipt_sha256": json_sha256(body)}


def validate_terminal_disposition(value: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    conditions = value.get("conditions")
    if (
        value.get("schema_version") != "1.0"
        or value.get("receipt_kind") != TERMINAL_RECEIPT_KIND
        or value.get("receipt_version") != TERMINAL_RECEIPT_VERSION
        or value.get("decision") != "DISABLE_LIVE_GUIDE"
        or value.get("provider_answer_released") is not False
        or value.get("scientific_denominator_attempts") != 0
        or value.get("architecture_denominator_attempts") != 0
        or not isinstance(conditions, Mapping)
        or not conditions
        or not all(item is True for item in conditions.values())
        or value.get("receipt_sha256") != json_sha256(body)
    ):
        raise ValueError("Evidence Guide v2 terminal disposition is invalid")
    return json.loads(json.dumps(value, sort_keys=True))
