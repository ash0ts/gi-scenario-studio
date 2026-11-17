"""Seal the failed R2 trace-canary preflight without treating it as study evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence_guide_v2_campaign import file_sha256, json_sha256

INCIDENT_KIND = "evidence_guide_v2_trace_canary_incident"
INCIDENT_VERSION = "r2-readback-1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path}")
    return value


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query)]


def build_trace_canary_incident(campaign_root: Path) -> dict[str, Any]:
    root = campaign_root.resolve()
    database = root / "campaign.sqlite"
    preregistration_path = root / "preregistration.json"
    plan_path = root / "campaign-plan.json"
    if any(not path.is_file() for path in (database, preregistration_path, plan_path)):
        raise FileNotFoundError("failed canary checkpoint is incomplete")
    wal_path = database.with_name(database.name + "-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise RuntimeError("failed canary database must be checkpointed before sealing")

    preregistration = _read_json(preregistration_path)
    preregistration_body = {
        key: value
        for key, value in preregistration.items()
        if key != "preregistration_sha256"
    }
    if preregistration.get("preregistration_sha256") != json_sha256(
        preregistration_body
    ):
        raise RuntimeError("failed canary preregistration digest is invalid")

    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        attempts = [
            json.loads(row["record_json"])
            for row in connection.execute(
                "SELECT record_json FROM attempts ORDER BY attempt_id"
            )
        ]
        ledger = _rows(
            connection,
            "SELECT attempt_id, stage, state, logical_provider_calls, "
            "worst_case_transport_attempts, input_token_envelope, "
            "output_token_envelope, worst_cost_usd FROM attempt_execution_ledger "
            "ORDER BY attempt_id",
        )
        sessions = _rows(
            connection,
            "SELECT stage, time_block, completed_at FROM stage_sessions "
            "ORDER BY stage, time_block",
        )
        receipts = {
            str(row["name"]): json.loads(row["receipt_json"])
            for row in connection.execute(
                "SELECT name, receipt_json FROM receipts ORDER BY name"
            )
        }
        meta = {
            str(row["key"]): json.loads(row["value_json"])
            for row in connection.execute(
                "SELECT key, value_json FROM campaign_meta ORDER BY key"
            )
        }
    finally:
        connection.close()

    canary_attempts = [row for row in attempts if row.get("stage") == "trace_canary"]
    scientific_attempts = [row for row in attempts if row.get("stage") != "trace_canary"]
    block_receipts = {
        key: value for key, value in receipts.items() if key.startswith("block:")
    }
    planned = [row for row in ledger if row.get("state") == "planned"]
    uncertain = [row for row in ledger if row.get("state") == "running"]
    provider_usage = {
        "cost_usd": str(
            sum(
                (Decimal(str(row.get("cost_usd", 0))) for row in canary_attempts),
                Decimal(0),
            )
        ),
        "provider_invocations": sum(
            int(row.get("provider_invocations", 0)) for row in canary_attempts
        ),
        "transport_attempts": sum(
            int(row.get("transport_attempts", 0)) for row in canary_attempts
        ),
        "input_tokens": sum(int(row.get("input_tokens", 0)) for row in canary_attempts),
        "output_tokens": sum(int(row.get("output_tokens", 0)) for row in canary_attempts),
    }
    d0_receipt = next(iter(block_receipts.values()), {})
    checks = {
        "exact_three_deterministic_rows": bool(
            len(canary_attempts) == 3
            and sorted(row.get("architecture_id") for row in canary_attempts)
            == ["D0", "D0", "D1"]
        ),
        "no_provider_or_transport_call": bool(
            provider_usage["provider_invocations"] == 0
            and provider_usage["transport_attempts"] == 0
            and provider_usage["input_tokens"] == 0
            and provider_usage["output_tokens"] == 0
            and provider_usage["cost_usd"] in {"0.0", "0"}
        ),
        "a1_remained_planned": bool(
            len(planned) == 1
            and planned[0].get("stage") == "trace_canary"
            and planned[0].get("logical_provider_calls") == 1
        ),
        "no_uncertain_provider_attempt": not uncertain,
        "one_acknowledged_d0_cloud_block": bool(
            len(block_receipts) == 1
            and d0_receipt.get("group", {}).get("architecture_id") == "D0"
            and d0_receipt.get("completed") == 2
            and d0_receipt.get("cloud", {}).get("attempt_count") == 2
        ),
        "no_completed_canary_session": not any(
            row.get("stage") == "trace_canary" and row.get("completed_at")
            for row in sessions
        ),
        "no_final_canary_receipt": "trace_canary" not in receipts,
        "zero_scientific_attempts": not scientific_attempts,
        "no_architecture_selection": "selection:architecture" not in receipts,
    }
    if not all(checks.values()):
        raise RuntimeError("failed trace-canary checkpoint does not match the incident protocol")

    body = {
        "schema_version": "1.0",
        "receipt_kind": INCIDENT_KIND,
        "incident_version": INCIDENT_VERSION,
        "failed_preregistration_sha256": preregistration["preregistration_sha256"],
        "protocol_amendment_sha256": preregistration["protocol_amendment"][
            "receipt_sha256"
        ],
        "source_files": {
            "campaign_sqlite_sha256": file_sha256(database),
            "preregistration_file_sha256": file_sha256(preregistration_path),
            "campaign_plan_sha256": file_sha256(plan_path),
        },
        "failure_code": "WEAVE_EVALUATION_RUN_READBACK_FAILED",
        "failure_stage": "trace_canary",
        "failure_group": "D1",
        "cause": (
            "The SDK reported no outstanding jobs before the evaluation call became "
            "queryable. The single immediate readback check failed closed."
        ),
        "disposition": "EXCLUDED_NON_SCIENTIFIC_PREFLIGHT",
        "scientific_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "canary_attempt_count": len(canary_attempts),
        "canary_attempt_row_sha256": json_sha256(canary_attempts),
        "provider_usage": provider_usage,
        "prior_usage": meta.get("prior_usage", {}),
        "checks": checks,
        "remediation": (
            "Use a bounded eventual-consistency poll followed by exact readback; start a "
            "fresh campaign instance and never resume the partial checkpoint."
        ),
        "claim_boundary": (
            "This incident establishes observability failure handling only. It contains no "
            "model-quality, architecture-efficacy, practitioner-usefulness, hydraulic, or "
            "ecological result."
        ),
    }
    return {**body, "receipt_sha256": json_sha256(body)}


def validate_trace_canary_incident(value: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    usage = value.get("provider_usage", {})
    checks = value.get("checks", {})
    if (
        value.get("schema_version") != "1.0"
        or value.get("receipt_kind") != INCIDENT_KIND
        or value.get("incident_version") != INCIDENT_VERSION
        or value.get("receipt_sha256") != json_sha256(body)
        or value.get("disposition") != "EXCLUDED_NON_SCIENTIFIC_PREFLIGHT"
        or value.get("scientific_denominator_attempts") != 0
        or value.get("architecture_denominator_attempts") != 0
        or not isinstance(checks, Mapping)
        or not checks
        or not all(item is True for item in checks.values())
        or not isinstance(usage, Mapping)
        or any(
            float(usage.get(key, 0)) != 0
            for key in (
                "cost_usd",
                "provider_invocations",
                "transport_attempts",
                "input_tokens",
                "output_tokens",
            )
        )
    ):
        raise ValueError("Evidence Guide v2 trace-canary incident receipt is invalid")
    return json.loads(json.dumps(value, sort_keys=True))
