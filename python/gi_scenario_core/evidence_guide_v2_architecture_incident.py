"""Seal the aborted Evidence Guide architecture-development R1 checkpoint.

The R1 checkpoint contains two operational observations, but it is not a
scientific result.  One request reached W&B Inference and received a traced
response; the next request raised a local input-envelope error before any
network transport.  The exact trigger is unresolved: R1 conflated serialized
request bytes with an input-token budget and did not retain a complete preflight
audit.  This module preserves the known facts and cumulative debit without
allowing either attempt into an architecture or model-quality denominator.

The builder is deliberately read-only.  A caller that encounters a non-empty
SQLite WAL must checkpoint it before calling :func:`build_architecture_development_incident`.
The companion CLI owns that narrowly scoped operation and then reopens the
database with SQLite's immutable URI flag.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence_guide_v2_campaign import file_sha256, json_sha256

SCHEMA_VERSION = "1.0"
RECEIPT_KIND = "evidence_guide_v2_architecture_development_incident"
INCIDENT_VERSION = "r1-protocol-implementation-1"
STUDY_VERSION = "evidence-guide-v2-architecture-development-r1"
DISPOSITION = "ABORTED_NONSCIENTIFIC_PROTOCOL_IMPLEMENTATION_INCIDENT"
TARGET_ENTITY = "wandb"
TARGET_PROJECT = "gi-scenario-studio-agent-evals"
LOCAL_FAILURE_CODE = "INFERENCE_INPUT_ENVELOPE_EXCEEDED"
SUCCESS_ATTEMPT_ID = "ee59099cbf74388fd7d171de058b0477"
FAILURE_ATTEMPT_ID = "e92a6edd1d1887cd696cc64b35858751"
SUCCESS_WEAVE_URL = (
    "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/agents/"
    "conversations/eval-f246b396277c-ee59099cbf74388fd7d171de058b0477"
)
R2B_RECEIPT_SHA256 = "83b7c19e4416dac9dd9ff2d9858be03e1cb5a46aa3e69927f33b37cfb0cc88a0"
R2B_USAGE = {
    "cost_usd": "0.013704",
    "provider_invocations": 291,
    "transport_attempts": 291,
    "input_tokens": 243928,
    "output_tokens": 37248,
}
CLAIM_BOUNDARY = (
    "This receipt establishes an observability and protocol-implementation failure "
    "only. It contains no model-quality, architecture-efficacy, practitioner-"
    "usefulness, hydraulic, or ecological result."
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path.name}")
    return value


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query)]


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"architecture incident {name} is not an object")
    return value


def _hex_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "cost_usd": format(
            sum((Decimal(str(row.get("cost_usd") or "0")) for row in rows), Decimal(0)),
            "f",
        ),
        "provider_invocations": sum(
            int(row.get("provider_invocations") or 0) for row in rows
        ),
        "transport_attempts": sum(
            int(row.get("transport_attempts") or 0) for row in rows
        ),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
    }


def _add_usage(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "cost_usd": format(
            Decimal(str(first["cost_usd"])) + Decimal(str(second["cost_usd"])),
            "f",
        ),
        "provider_invocations": int(first["provider_invocations"])
        + int(second["provider_invocations"]),
        "transport_attempts": int(first["transport_attempts"])
        + int(second["transport_attempts"]),
        "input_tokens": int(first["input_tokens"]) + int(second["input_tokens"]),
        "output_tokens": int(first["output_tokens"])
        + int(second["output_tokens"]),
    }


def _open_immutable_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    wal = path.with_name(f"{path.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("architecture R1 SQLite WAL must be checkpointed before sealing")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _assert_preregistered_files(
    *,
    preregistration: Mapping[str, Any],
    research_plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    preregistration_path: Path,
    schedule_path: Path,
) -> None:
    preregistration_body = {
        key: value
        for key, value in preregistration.items()
        if key != "preregistration_sha256"
    }
    preregistration_sha256 = preregistration.get("preregistration_sha256")
    schedule_section = _mapping(preregistration.get("schedule"), name="schedule")
    schedule_records = schedule.get("attempts")
    checks = (
        preregistration.get("schema_version") == SCHEMA_VERSION,
        preregistration.get("study_version") == STUDY_VERSION,
        preregistration_sha256 == json_sha256(preregistration_body),
        preregistration.get("release_eligible") is False,
        preregistration.get("assistant_release_basis_may_be_created") is False,
        research_plan.get("schema_version") == SCHEMA_VERSION,
        research_plan.get("study_version") == STUDY_VERSION,
        research_plan.get("preregistration_sha256") == preregistration_sha256,
        research_plan.get("preregistration_file_sha256")
        == file_sha256(preregistration_path),
        research_plan.get("schedule_file_sha256") == file_sha256(schedule_path),
        research_plan.get("schedule_sha256")
        == schedule_section.get("schedule_sha256"),
        schedule.get("schema_version") == SCHEMA_VERSION,
        schedule.get("study_version") == STUDY_VERSION,
        schedule.get("study_instance_sha256")
        == preregistration.get("study_instance_sha256"),
        isinstance(schedule_records, list),
        schedule.get("schedule_sha256") == json_sha256(schedule_records),
        schedule.get("schedule_sha256") == schedule_section.get("schedule_sha256"),
        schedule_section.get("attempt_count") == len(schedule_records or []),
        research_plan.get("release_eligible") is False,
        research_plan.get("assistant_release_basis_may_be_created") is False,
    )
    if not all(checks):
        raise RuntimeError("architecture R1 preregistration or schedule binding drifted")


def _load_checkpoint(root: Path) -> dict[str, Any]:
    database = root / "campaign.sqlite"
    preregistration_path = root / "preregistration.json"
    research_plan_path = root / "research-plan.json"
    schedule_path = root / "attempt-schedule.json"
    for path in (
        database,
        preregistration_path,
        research_plan_path,
        schedule_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    preregistration = _read_object(preregistration_path)
    research_plan = _read_object(research_plan_path)
    schedule = _read_object(schedule_path)
    _assert_preregistered_files(
        preregistration=preregistration,
        research_plan=research_plan,
        schedule=schedule,
        preregistration_path=preregistration_path,
        schedule_path=schedule_path,
    )

    connection = _open_immutable_database(database)
    try:
        metadata = {
            str(row["key"]): json.loads(row["value_json"])
            for row in connection.execute(
                "SELECT key, value_json FROM campaign_meta ORDER BY key"
            )
        }
        attempts = []
        for row in connection.execute(
            "SELECT a.attempt_id, a.stage, a.hidden, a.completed_at, a.record_json, "
            "l.spec_sha256, l.state, l.logical_provider_calls, "
            "l.worst_case_transport_attempts, l.input_token_envelope, "
            "l.output_token_envelope, l.worst_cost_usd, l.started_at, "
            "l.completed_at AS ledger_completed_at, l.outcome_sha256 "
            "FROM attempts a JOIN attempt_execution_ledger l USING(attempt_id) "
            "ORDER BY a.attempt_id"
        ):
            value = dict(row)
            value["record"] = json.loads(value.pop("record_json"))
            attempts.append(value)
        ledger_counts = _rows(
            connection,
            "SELECT state, COUNT(*) AS attempt_count, "
            "SUM(logical_provider_calls) AS logical_provider_calls, "
            "SUM(worst_case_transport_attempts) AS worst_case_transport_attempts, "
            "SUM(input_token_envelope) AS input_token_envelope, "
            "SUM(output_token_envelope) AS output_token_envelope "
            "FROM attempt_execution_ledger GROUP BY state ORDER BY state",
        )
        sessions = _rows(
            connection,
            "SELECT stage, time_block, session_id, started_at, completed_at "
            "FROM stage_sessions ORDER BY stage, time_block",
        )
        reservations = _rows(
            connection,
            "SELECT stage, cost_usd, nominal_invocations, worst_case_invocations, "
            "input_tokens, output_tokens, created_at "
            "FROM stage_reservations ORDER BY stage",
        )
        receipts = {
            str(row["name"]): json.loads(row["receipt_json"])
            for row in connection.execute(
                "SELECT name, receipt_json FROM receipts ORDER BY name"
            )
        }
    finally:
        connection.close()

    if metadata.get("preregistration") != preregistration:
        raise RuntimeError("architecture R1 SQLite preregistration drifted")
    schedule_sha256 = preregistration["schedule"]["schedule_sha256"]
    if metadata.get("attempt_schedule_sha256") != schedule_sha256:
        raise RuntimeError("architecture R1 SQLite schedule binding drifted")
    predecessor = _mapping(preregistration.get("predecessor"), name="predecessor")
    if metadata.get("predecessor_terminal") != predecessor:
        raise RuntimeError("architecture R1 predecessor binding drifted")
    prior_usage = _mapping(metadata.get("prior_usage"), name="prior usage")
    predecessor_usage = _mapping(predecessor.get("usage"), name="predecessor usage")
    if any(prior_usage.get(key) != predecessor_usage.get(key) for key in _USAGE_KEYS):
        raise RuntimeError("architecture R1 prior debit drifted")

    source_files = {
        "campaign_sqlite_sha256": file_sha256(database),
        "preregistration_file_sha256": file_sha256(preregistration_path),
        "research_plan_file_sha256": file_sha256(research_plan_path),
        "attempt_schedule_file_sha256": file_sha256(schedule_path),
    }
    return {
        "database": database,
        "preregistration": preregistration,
        "research_plan": research_plan,
        "schedule": schedule,
        "metadata": metadata,
        "attempts": attempts,
        "ledger_counts": ledger_counts,
        "sessions": sessions,
        "reservations": reservations,
        "receipts": receipts,
        "source_files": source_files,
    }


_USAGE_KEYS = (
    "cost_usd",
    "provider_invocations",
    "transport_attempts",
    "input_tokens",
    "output_tokens",
)


def _attempt_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(row.get("record"), name="attempt record")
    details = _mapping(record.get("details"), name="attempt details")
    trace = details.get("weave_turn_receipt")
    summary = {
        "attempt_id": str(row["attempt_id"]),
        "spec_sha256": str(row["spec_sha256"]),
        "outcome_sha256": str(row["outcome_sha256"]),
        "record_sha256": json_sha256(record),
        "stage": str(record.get("stage")),
        "split": str(record.get("split")),
        "case_id": str(record.get("case_id")),
        "model_id": str(record.get("model_id")),
        "architecture_id": str(record.get("architecture_id")),
        "ablation_condition": record.get("ablation_condition"),
        "status": str(record.get("status")),
        "error_codes": list(record.get("error_codes") or []),
        "provider_invocations": int(record.get("provider_invocations") or 0),
        "transport_attempts": int(record.get("transport_attempts") or 0),
        "input_tokens": int(record.get("input_tokens") or 0),
        "output_tokens": int(record.get("output_tokens") or 0),
        "cost_usd": format(Decimal(str(record.get("cost_usd") or "0")), "f"),
        "trace_acknowledged": record.get("trace_acknowledged") is True,
        "answer_released": details.get("answer_released") is True,
        "weave_turn_receipt": dict(trace) if isinstance(trace, Mapping) else None,
    }
    if summary["outcome_sha256"] != summary["record_sha256"]:
        raise RuntimeError("architecture R1 attempt outcome hash drifted")
    return summary


def _assert_incident_shape(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    attempts = list(checkpoint["attempts"])
    summaries = [_attempt_summary(row) for row in attempts]
    transported = [row for row in summaries if row["transport_attempts"] == 1]
    local = [row for row in summaries if row["transport_attempts"] == 0]
    sessions = list(checkpoint["sessions"])
    receipts = _mapping(checkpoint["receipts"], name="receipts")
    research_plan = _mapping(checkpoint["research_plan"], name="research plan")
    preregistration = _mapping(
        checkpoint["preregistration"], name="preregistration"
    )
    ledger_by_state = {
        str(row["state"]): row for row in checkpoint["ledger_counts"]
    }
    completed_ledger = ledger_by_state.get("completed", {})
    planned_ledger = ledger_by_state.get("planned", {})
    running_ledger = ledger_by_state.get("running")
    prereg_cloud = _mapping(
        receipts.get("preregistration_cloud"), name="W&B preregistration receipt"
    )
    datasets_cloud = _mapping(
        receipts.get("visible_datasets_cloud"), name="W&B dataset receipt"
    )
    references = _mapping(
        datasets_cloud.get("references"), name="W&B dataset references"
    )

    success = transported[0] if len(transported) == 1 else {}
    failure = local[0] if len(local) == 1 else {}
    local_source_rows = [
        _mapping(row.get("record"), name="attempt record")
        for row in attempts
        if int(
            _mapping(row.get("record"), name="attempt record").get(
                "transport_attempts", 0
            )
            or 0
        )
        == 0
    ]
    local_source = local_source_rows[0] if len(local_source_rows) == 1 else {}
    local_details = (
        _mapping(local_source.get("details"), name="attempt details")
        if local_source
        else {}
    )
    trace = success.get("weave_turn_receipt")
    success_trace = trace if isinstance(trace, Mapping) else {}
    checks = {
        "exactly_two_materialized_attempts": len(summaries) == 2,
        "exactly_one_completed_inference_transport": bool(
            len(transported) == 1
            and success.get("attempt_id") == SUCCESS_ATTEMPT_ID
            and success.get("status") == "completed"
            and success.get("provider_invocations") == 1
            and success.get("input_tokens") == 438
            and success.get("output_tokens") == 8
            and success.get("cost_usd") == "0.000047"
            and success.get("error_codes") == []
            and success.get("trace_acknowledged") is True
            and success.get("answer_released") is False
        ),
        "exactly_one_local_zero_transport_envelope_failure": bool(
            len(local) == 1
            and failure.get("attempt_id") == FAILURE_ATTEMPT_ID
            and failure.get("status") == "failed_closed"
            and failure.get("provider_invocations") == 1
            and failure.get("input_tokens") == 0
            and failure.get("output_tokens") == 0
            and failure.get("cost_usd") == "0"
            and failure.get("error_codes") == [LOCAL_FAILURE_CODE]
            and failure.get("trace_acknowledged") is False
            and failure.get("weave_turn_receipt") is None
            and failure.get("answer_released") is False
        ),
        "exact_trigger_unresolved_by_checkpoint": bool(
            len(local_source_rows) == 1
            and not any(
                key in local_source or key in local_details
                for key in (
                    "serialized_request_bytes",
                    "provider_payload_bytes",
                    "input_token_estimate",
                    "preflight_audit",
                )
            )
        ),
        "both_answers_nonreleased": bool(
            len(summaries) == 2
            and all(row.get("answer_released") is False for row in summaries)
        ),
        "ledger_preserves_two_completed_and_766_unspent_plans": bool(
            completed_ledger.get("attempt_count") == 2
            and planned_ledger.get("attempt_count") == 766
            and running_ledger is None
        ),
        "one_incomplete_session": bool(
            len(sessions) == 1 and sessions[0].get("completed_at") is None
        ),
        "no_block_or_terminal_receipt": bool(
            set(receipts) == {"preregistration_cloud", "visible_datasets_cloud"}
            and not any(
                str(name).startswith(("block:", "terminal", "selection:", "release"))
                for name in receipts
            )
        ),
        "no_release_basis": bool(
            preregistration.get("release_eligible") is False
            and preregistration.get("assistant_release_basis_may_be_created") is False
            and research_plan.get("release_eligible") is False
            and research_plan.get("assistant_release_basis_may_be_created") is False
        ),
        "wandb_preregistration_roundtrip_preserved": bool(
            prereg_cloud.get("entity") == TARGET_ENTITY
            and prereg_cloud.get("project") == TARGET_PROJECT
            and prereg_cloud.get("source_sha256")
            == checkpoint["source_files"]["preregistration_file_sha256"]
            and prereg_cloud.get("readback_sha256")
            == checkpoint["source_files"]["preregistration_file_sha256"]
        ),
        "wandb_dataset_receipt_preserved": bool(
            datasets_cloud.get("references_sha256") == json_sha256(references)
            and set(references) == {"development", "validation", "judge_calibration"}
        ),
        "successful_weave_trace_preserved": bool(
            success_trace.get("conversation_id")
            and success_trace.get("trace_id")
            and success_trace.get("root_span_id")
            and isinstance(success_trace.get("url"), str)
            and success_trace["url"].startswith(
                f"https://wandb.ai/{TARGET_ENTITY}/{TARGET_PROJECT}/weave/agents/conversations/"
            )
            and success_trace["url"] == SUCCESS_WEAVE_URL
        ),
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise RuntimeError(f"architecture R1 incident boundary mismatch: {failed}")
    return {"summaries": summaries, "checks": checks}


def build_architecture_development_incident(campaign_root: Path) -> dict[str, Any]:
    """Build a self-hashed incident receipt from the immutable R1 checkpoint."""

    root = campaign_root.resolve()
    checkpoint = _load_checkpoint(root)
    incident = _assert_incident_shape(checkpoint)
    summaries = incident["summaries"]
    preregistration = checkpoint["preregistration"]
    receipts = checkpoint["receipts"]
    prior = checkpoint["metadata"]["prior_usage"]
    prior_r2b_usage = {
        "source": str(prior.get("source")),
        "source_receipt_sha256": str(prior.get("source_receipt_sha256")),
        **{key: prior[key] for key in _USAGE_KEYS},
    }
    incident_usage = _usage(summaries)
    cumulative_usage = _add_usage(prior_r2b_usage, incident_usage)
    source_files = dict(checkpoint["source_files"])
    source_identity = {
        "study_version": STUDY_VERSION,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "schedule_sha256": preregistration["schedule"]["schedule_sha256"],
        "source_files": source_files,
        "preregistered_source_hashes": preregistration["source_hashes"],
        "attempt_hashes": [
            {
                "attempt_id": row["attempt_id"],
                "spec_sha256": row["spec_sha256"],
                "outcome_sha256": row["outcome_sha256"],
                "record_sha256": row["record_sha256"],
            }
            for row in summaries
        ],
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": RECEIPT_KIND,
        "incident_version": INCIDENT_VERSION,
        "study_version": STUDY_VERSION,
        "disposition": DISPOSITION,
        "failure_stage": "causal_perturbation",
        "failure_code": LOCAL_FAILURE_CODE,
        "cause": (
            "After one successful transport, the R1 implementation raised "
            "INFERENCE_INPUT_ENVELOPE_EXCEEDED locally before the next transport. "
            "R1 conflated serialized request bytes with the 2,048 input-token "
            "budget and did not retain a complete preflight audit. A later "
            "reconstruction measured 1,923 serialized bytes, so the exact trigger "
            "is unresolved."
        ),
        "root_cause_status": "UNRESOLVED_INCOMPLETE_PREFLIGHT_AUDIT",
        "confirmed_protocol_defects": [
            "SERIALIZED_BYTES_CONFLATED_WITH_INPUT_TOKEN_BUDGET",
            "COMPLETE_PREFLIGHT_AUDIT_NOT_RETAINED",
        ],
        "scientific_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "scientifically_usable_attempts": 0,
        "materialized_attempt_count": 2,
        "answer_released": False,
        "production_state_before_and_after": "DISABLE_LIVE_GUIDE",
        "release_basis_created": False,
        "terminal_receipt_created": False,
        "completed_block_receipt_count": 0,
        "completed_session_count": 0,
        "source_identity": source_identity,
        "source_identity_sha256": json_sha256(source_identity),
        "attempts": summaries,
        "attempts_sha256": json_sha256(summaries),
        "cloud_receipts": {
            "preregistration_cloud": receipts["preregistration_cloud"],
            "visible_datasets_cloud": receipts["visible_datasets_cloud"],
        },
        "cloud_receipts_sha256": json_sha256(
            {
                "preregistration_cloud": receipts["preregistration_cloud"],
                "visible_datasets_cloud": receipts["visible_datasets_cloud"],
            }
        ),
        "weave_trace_url": next(
            row["weave_turn_receipt"]["url"]
            for row in summaries
            if row["weave_turn_receipt"] is not None
        ),
        "prior_r2b_usage": prior_r2b_usage,
        "incident_usage": incident_usage,
        "cumulative_usage": cumulative_usage,
        "unspent_planned_attempts": 766,
        "checks": incident["checks"],
        "remediation": (
            "Start a new preregistered study instance with byte and token envelopes "
            "modeled separately and a complete pre-transport audit retained before "
            "block execution. Never resume or publish R1 as a study."
        ),
        "claim_boundary": CLAIM_BOUNDARY,
    }
    receipt = {**body, "receipt_sha256": json_sha256(body)}
    return validate_architecture_development_incident(receipt)


def _validate_usage(value: Any, *, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("architecture incident usage is invalid")
    normalized = {
        "cost_usd": format(Decimal(str(value.get("cost_usd"))), "f"),
        "provider_invocations": int(value.get("provider_invocations")),
        "transport_attempts": int(value.get("transport_attempts")),
        "input_tokens": int(value.get("input_tokens")),
        "output_tokens": int(value.get("output_tokens")),
    }
    if Decimal(normalized["cost_usd"]) < 0 or any(
        normalized[key] < 0 for key in _USAGE_KEYS if key != "cost_usd"
    ):
        raise ValueError("architecture incident usage is invalid")
    if expected is not None and any(
        normalized[key] != expected[key] for key in _USAGE_KEYS
    ):
        raise ValueError("architecture incident usage is invalid")
    return normalized


def validate_architecture_development_incident(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the R1 incident before a successor inherits its debit.

    Validation is intentionally strict about the two operational attempts and
    their usage.  A successful return does *not* make those rows scientific
    evidence; it only makes the debit and exclusion boundary consumable by a
    fresh preregistered study instance.
    """

    try:
        body = {key: item for key, item in value.items() if key != "receipt_sha256"}
        source_identity = _mapping(value.get("source_identity"), name="source identity")
        source_files = _mapping(source_identity.get("source_files"), name="source files")
        attempts = value.get("attempts")
        cloud = _mapping(value.get("cloud_receipts"), name="cloud receipts")
        checks = _mapping(value.get("checks"), name="checks")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise ValueError("architecture incident attempt set is invalid")
        if not all(isinstance(row, Mapping) for row in attempts):
            raise ValueError("architecture incident attempt set is invalid")
        attempt_rows = [dict(row) for row in attempts]
        transported = [row for row in attempt_rows if row.get("transport_attempts") == 1]
        local = [row for row in attempt_rows if row.get("transport_attempts") == 0]
        success = transported[0] if len(transported) == 1 else {}
        failure = local[0] if len(local) == 1 else {}
        trace = success.get("weave_turn_receipt")
        prereg_cloud = _mapping(
            cloud.get("preregistration_cloud"), name="W&B preregistration receipt"
        )
        datasets_cloud = _mapping(
            cloud.get("visible_datasets_cloud"), name="W&B dataset receipt"
        )
        references = _mapping(
            datasets_cloud.get("references"), name="W&B dataset references"
        )
        incident_usage = _validate_usage(
            value.get("incident_usage"),
            expected={
                "cost_usd": "0.000047",
                "provider_invocations": 2,
                "transport_attempts": 1,
                "input_tokens": 438,
                "output_tokens": 8,
            },
        )
        if incident_usage != _usage(attempt_rows):
            raise ValueError("architecture incident attempt debit is invalid")
        prior_value = _mapping(value.get("prior_r2b_usage"), name="prior R2b usage")
        prior = _validate_usage(prior_value, expected=R2B_USAGE)
        cumulative = _validate_usage(value.get("cumulative_usage"))
        if cumulative != _add_usage(prior, incident_usage):
            raise ValueError("architecture incident cumulative debit is invalid")
        expected_attempt_hashes = [
            {
                "attempt_id": row.get("attempt_id"),
                "spec_sha256": row.get("spec_sha256"),
                "outcome_sha256": row.get("outcome_sha256"),
                "record_sha256": row.get("record_sha256"),
            }
            for row in attempt_rows
        ]
        conditions = (
            value.get("schema_version") == SCHEMA_VERSION,
            value.get("receipt_kind") == RECEIPT_KIND,
            value.get("incident_version") == INCIDENT_VERSION,
            value.get("study_version") == STUDY_VERSION,
            value.get("disposition") == DISPOSITION,
            value.get("failure_stage") == "causal_perturbation",
            value.get("failure_code") == LOCAL_FAILURE_CODE,
            value.get("root_cause_status") == "UNRESOLVED_INCOMPLETE_PREFLIGHT_AUDIT",
            value.get("confirmed_protocol_defects")
            == [
                "SERIALIZED_BYTES_CONFLATED_WITH_INPUT_TOKEN_BUDGET",
                "COMPLETE_PREFLIGHT_AUDIT_NOT_RETAINED",
            ],
            value.get("receipt_sha256") == json_sha256(body),
            value.get("source_identity_sha256") == json_sha256(source_identity),
            value.get("attempts_sha256") == json_sha256(attempt_rows),
            value.get("cloud_receipts_sha256") == json_sha256(cloud),
            value.get("scientific_denominator_attempts") == 0,
            value.get("architecture_denominator_attempts") == 0,
            value.get("scientifically_usable_attempts") == 0,
            value.get("materialized_attempt_count") == 2,
            value.get("answer_released") is False,
            value.get("production_state_before_and_after") == "DISABLE_LIVE_GUIDE",
            value.get("release_basis_created") is False,
            value.get("terminal_receipt_created") is False,
            value.get("completed_block_receipt_count") == 0,
            value.get("completed_session_count") == 0,
            value.get("unspent_planned_attempts") == 766,
            value.get("claim_boundary") == CLAIM_BOUNDARY,
            isinstance(checks, Mapping) and bool(checks),
            all(item is True for item in checks.values()),
            len(transported) == 1,
            success.get("status") == "completed",
            success.get("attempt_id") == SUCCESS_ATTEMPT_ID,
            success.get("provider_invocations") == 1,
            success.get("input_tokens") == 438,
            success.get("output_tokens") == 8,
            success.get("cost_usd") == "0.000047",
            success.get("error_codes") == [],
            success.get("trace_acknowledged") is True,
            success.get("answer_released") is False,
            isinstance(trace, Mapping),
            value.get("weave_trace_url") == (trace or {}).get("url"),
            value.get("weave_trace_url") == SUCCESS_WEAVE_URL,
            (trace or {}).get("conversation_id")
            == "eval-f246b396277c-ee59099cbf74388fd7d171de058b0477",
            (trace or {}).get("trace_id") == "80a401970f175743c8233ddb77aa8eae",
            (trace or {}).get("root_span_id") == "ac5ff89db9613a10",
            len(local) == 1,
            failure.get("attempt_id") == FAILURE_ATTEMPT_ID,
            failure.get("status") == "failed_closed",
            failure.get("provider_invocations") == 1,
            failure.get("input_tokens") == 0,
            failure.get("output_tokens") == 0,
            failure.get("cost_usd") == "0",
            failure.get("error_codes") == [LOCAL_FAILURE_CODE],
            failure.get("trace_acknowledged") is False,
            failure.get("answer_released") is False,
            failure.get("weave_turn_receipt") is None,
            prior_value.get("source") == "evidence-guide-v2-campaign-r2b-terminal",
            prior_value.get("source_receipt_sha256") == R2B_RECEIPT_SHA256,
            source_identity.get("attempt_hashes") == expected_attempt_hashes,
            all(
                _hex_digest(row.get(key))
                for row in attempt_rows
                for key in ("spec_sha256", "outcome_sha256", "record_sha256")
            ),
            all(row.get("outcome_sha256") == row.get("record_sha256") for row in attempt_rows),
            set(source_files)
            == {
                "campaign_sqlite_sha256",
                "preregistration_file_sha256",
                "research_plan_file_sha256",
                "attempt_schedule_file_sha256",
            },
            all(_hex_digest(item) for item in source_files.values()),
            _hex_digest(source_identity.get("preregistration_sha256")),
            _hex_digest(source_identity.get("schedule_sha256")),
            prereg_cloud.get("entity") == TARGET_ENTITY,
            prereg_cloud.get("project") == TARGET_PROJECT,
            prereg_cloud.get("source_sha256")
            == source_files.get("preregistration_file_sha256"),
            prereg_cloud.get("readback_sha256")
            == source_files.get("preregistration_file_sha256"),
            datasets_cloud.get("references_sha256") == json_sha256(references),
            set(references) == {"development", "validation", "judge_calibration"},
        )
        if not all(conditions):
            raise ValueError("Evidence Guide architecture R1 incident receipt is invalid")
    except (KeyError, TypeError, ValueError, ArithmeticError, RuntimeError) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "Evidence Guide architecture R1 incident receipt is invalid"
        ):
            raise
        raise ValueError(
            "Evidence Guide architecture R1 incident receipt is invalid"
        ) from error
    return json.loads(json.dumps(value, sort_keys=True))
