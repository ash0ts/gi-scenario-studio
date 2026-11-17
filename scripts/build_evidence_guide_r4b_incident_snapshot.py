"""Project the interrupted R4b execution into a deterministic incident receipt.

This utility is intentionally read-only.  It turns the preserved local SQLite
ledger and its on-disk receipts into a compact, content-addressed record that a
fresh R4c preregistration can consume without inheriting any R4b attempt cells.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXECUTION_ROOT = (
    ROOT / "dist" / "evidence-guide-r4b-grounding-final-r1" / "live"
)
DEFAULT_OUTPUT = (
    ROOT
    / "benchmarks"
    / "evidence_guide"
    / "r4"
    / "r4b-incident-snapshot.json"
)
EXPECTED_STUDY = "gi-evidence-guide-grounding-r4b-final"
INCIDENT_ID = "gi-evidence-guide-r4b-input-cap-incident-2026-08-07"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_receipt(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "filename": path.name,
        "utf8_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path.name}")
    return value


def _stable_rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query)]


def _sum_decimal(rows: Sequence[Mapping[str, Any]], path: Sequence[str]) -> Decimal:
    total = Decimal(0)
    for row in rows:
        value: Any = row
        for key in path:
            value = value[key]
        total += Decimal(str(value))
    return total


def build_snapshot(execution_root: Path = DEFAULT_EXECUTION_ROOT) -> dict[str, Any]:
    database = execution_root / "campaign.sqlite"
    preregistration_path = execution_root / "preregistration.json"
    execution_receipt_path = execution_root / "execution-receipt.json"
    block_receipt_path = execution_root / "block-block-1-receipt.json"
    required = (database, preregistration_path, execution_receipt_path, block_receipt_path)
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing R4b incident sources: " + ", ".join(missing))

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        attempt_rows = _stable_rows(
            connection,
            "SELECT attempt_id, stage, hidden, record_json, completed_at "
            "FROM attempts ORDER BY attempt_id",
        )
        records = [json.loads(str(row["record_json"])) for row in attempt_rows]
        ledgers = _stable_rows(
            connection,
            "SELECT attempt_id, stage, hidden, spec_sha256, state, "
            "logical_provider_calls, worst_case_transport_attempts, "
            "input_token_envelope, output_token_envelope, worst_cost_usd, "
            "planned_at, started_at, completed_at, outcome_sha256 "
            "FROM attempt_execution_ledger ORDER BY attempt_id",
        )
        sessions = _stable_rows(
            connection,
            "SELECT stage, time_block, session_id, started_at, completed_at "
            "FROM stage_sessions ORDER BY time_block, stage",
        )
        receipt_rows = _stable_rows(
            connection,
            "SELECT name, receipt_json, created_at FROM receipts ORDER BY name",
        )
        meta_rows = _stable_rows(
            connection,
            "SELECT key, value_json FROM campaign_meta ORDER BY key",
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()

    if integrity != "ok" or len(records) != 272 or len(ledgers) != 672:
        raise RuntimeError("R4B_INCIDENT_SQLITE_INTEGRITY_OR_COUNT_DRIFT")
    if any(record.get("details", {}).get("study_version") != EXPECTED_STUDY for record in records):
        raise RuntimeError("R4B_INCIDENT_STUDY_IDENTITY_DRIFT")

    status_counts = Counter(str(record["status"]) for record in records)
    cap_rows = [
        record
        for record in records
        if "R2_PROVIDER_INPUT_TOKEN_CAP_EXCEEDED" in record.get("error_codes", [])
    ]
    accepted_rows = [record for record in records if record["status"] == "completed"]
    if (
        status_counts != {"completed": 238, "failed_closed": 34}
        or len(cap_rows) != 34
        or sum(bool(row["trace_acknowledged"]) for row in accepted_rows) != 238
        or any(row["trace_acknowledged"] for row in cap_rows)
    ):
        raise RuntimeError("R4B_INCIDENT_OUTCOME_COUNTS_DRIFT")

    by_stage_block: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["time_block"]), str(record["stage"]))].append(record)
    for (block, stage), rows in sorted(grouped.items()):
        by_stage_block[f"{block}/{stage}"] = {
            "attempts": len(rows),
            "accepted_traced": sum(row["status"] == "completed" for row in rows),
            "post_response_cap_rejections": sum(row in cap_rows for row in rows),
            "provider_invocations": sum(int(row["provider_invocations"]) for row in rows),
            "transport_attempts": sum(int(row["transport_attempts"]) for row in rows),
        }

    by_model: dict[str, dict[str, int]] = {}
    for model in sorted({str(row["model_id"]) for row in records}):
        rows = [row for row in records if row["model_id"] == model]
        by_model[model] = {
            "attempts": len(rows),
            "accepted_traced": sum(row["status"] == "completed" for row in rows),
            "post_response_cap_rejections": sum(row in cap_rows for row in rows),
        }

    ledger_counts = Counter(str(row["state"]) for row in ledgers)
    ledger_by_id = {str(row["attempt_id"]): row for row in ledgers}
    attempt_ids = {str(row["attempt_id"]) for row in records}
    completed_ledger_ids = {
        str(row["attempt_id"]) for row in ledgers if row["state"] == "completed"
    }
    outcome_mismatches = 0
    for row, record in zip(attempt_rows, records, strict=True):
        ledger = ledger_by_id[str(row["attempt_id"])]
        if ledger["outcome_sha256"] != json_sha256(record):
            outcome_mismatches += 1
    ambiguous_count = (
        ledger_counts.get("running", 0)
        + len(attempt_ids ^ completed_ledger_ids)
        + outcome_mismatches
    )
    if ledger_counts != {"completed": 272, "planned": 400} or ambiguous_count != 0:
        raise RuntimeError("R4B_INCIDENT_LEDGER_AMBIGUITY")

    meta = {str(row["key"]): json.loads(str(row["value_json"])) for row in meta_rows}
    prior = meta.get("prior_usage")
    context = meta.get("r4_execution_context")
    if not isinstance(prior, Mapping) or not isinstance(context, Mapping):
        raise TypeError("R4B_INCIDENT_CAMPAIGN_META_INVALID")
    if context.get("study_version") != EXPECTED_STUDY:
        raise RuntimeError("R4B_INCIDENT_CONTEXT_IDENTITY_DRIFT")

    observed = {
        "provider_invocations": sum(int(row["provider_invocations"]) for row in records),
        "transport_attempts": sum(int(row["transport_attempts"]) for row in records),
        "provider_reported_input_tokens": sum(int(row["input_tokens"]) for row in records),
        "provider_reported_output_tokens": sum(int(row["output_tokens"]) for row in records),
        "input_token_envelope": sum(int(row["input_token_envelope"]) for row in records),
        "output_token_envelope": sum(int(row["output_token_envelope"]) for row in records),
        "verified_observed_cost_usd": str(_sum_decimal(records, ("cost_usd",))),
        "conservative_at_risk_cost_usd": str(
            _sum_decimal(
                records,
                ("details", "cost_accounting", "conservative_at_risk_cost_usd"),
            )
        ),
    }
    expected_observed = {
        "provider_invocations": 272,
        "transport_attempts": 272,
        "provider_reported_input_tokens": 461_644,
        "provider_reported_output_tokens": 23_016,
        "input_token_envelope": 461_644,
        "output_token_envelope": 43_008,
        "verified_observed_cost_usd": "0.039650",
        "conservative_at_risk_cost_usd": "0.043629",
    }
    if observed != expected_observed:
        raise RuntimeError("R4B_INCIDENT_RESOURCE_LEDGER_DRIFT")

    r4c_prior = {
        "provider_invocations": int(prior["provider_invocations"])
        + observed["provider_invocations"],
        "transport_attempts": int(prior["transport_attempts"])
        + observed["transport_attempts"],
        "input_token_envelope": int(prior["input_tokens"])
        + observed["input_token_envelope"],
        "output_token_envelope": int(prior["output_tokens"])
        + observed["output_token_envelope"],
        "conservative_cumulative_cost_usd": str(
            Decimal(str(prior["cost_usd"]))
            + Decimal(observed["conservative_at_risk_cost_usd"])
        ),
        "verified_observed_cost_usd": str(
            Decimal(str(prior["verified_observed_cost_usd"]))
            + Decimal(observed["verified_observed_cost_usd"])
        ),
    }

    preregistration = _json_file(preregistration_path)
    stale_execution = _json_file(execution_receipt_path)
    block_receipt = _json_file(block_receipt_path)
    embedded_receipts = [json.loads(str(row["receipt_json"])) for row in receipt_rows]
    if (
        preregistration.get("study_version") != EXPECTED_STUDY
        or stale_execution.get("study_version") != EXPECTED_STUDY
        or block_receipt.get("receipt_sha256")
        != embedded_receipts[0].get("receipt_sha256")
        or int(stale_execution.get("attempt_count") or 0) != 224
        or len(embedded_receipts) != 1
    ):
        raise RuntimeError("R4B_INCIDENT_RECEIPT_IDENTITY_DRIFT")

    source_files = {
        "campaign_sqlite": file_receipt(database),
        "preregistration": file_receipt(preregistration_path),
        "stale_execution_receipt": file_receipt(execution_receipt_path),
        "completed_block_1_receipt": file_receipt(block_receipt_path),
    }
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        source_files["campaign_sqlite_wal"] = file_receipt(wal)

    session_projection = [
        {
            "stage": row["stage"],
            "time_block": row["time_block"],
            "complete": row["completed_at"] is not None,
        }
        for row in sessions
    ]
    body = {
        "schema_version": "1.0",
        "incident_id": INCIDENT_ID,
        "source_study_version": EXPECTED_STUDY,
        "source_study_instance_sha256": context["study_instance_sha256"],
        "source_preregistration_sha256": context["preregistration_sha256"],
        "incident_summary": {
            "block_1_attempts": 224,
            "interrupted_block_2_attempts": 48,
            "provider_invocations": 272,
            "transport_attempts": 272,
            "accepted_traced_responses": 238,
            "post_response_input_cap_rejections": 34,
            "configured_input_token_cap": 2048,
            "r4b_cells_reused_by_r4c": 0,
        },
        "outcomes_by_stage_and_block": by_stage_block,
        "outcomes_by_model": by_model,
        "post_response_classification": {
            "error_code_persisted_by_r4b": "R2_PROVIDER_INPUT_TOKEN_CAP_EXCEEDED",
            "classification": "provider response received, then rejected by local resource guard",
            "evidence": (
                "All 34 rows retain nonzero provider-reported input/output usage, one logical "
                "call, and one transport. The R4b meter appended the completion before raising; "
                "the outer executor then persisted these rows as failed_closed without response "
                "content or trace acknowledgement."
            ),
            "r4b_persisted_provider_response_received": False,
            "r4c_required_semantics": (
                "retain and trace the completion; report a distinct post-receipt resource-policy "
                "violation; keep end-to-end conformance false"
            ),
        },
        "execution_ledger": {
            "state_counts": dict(sorted(ledger_counts.items())),
            "attempt_rows": len(records),
            "outcome_hash_mismatches": outcome_mismatches,
            "running_or_uncertain_attempts": 0,
            "ambiguous_ledger_states": ambiguous_count,
            "attempt_records_sha256": json_sha256(records),
            "ledger_rows_sha256": json_sha256(ledgers),
            "campaign_meta_sha256": json_sha256(meta),
            "stage_sessions": session_projection,
            "stage_sessions_sha256": json_sha256(sessions),
            "sqlite_integrity_check": integrity,
            "sqlite_user_version": user_version,
        },
        "receipt_state": {
            "completed_receipts": [row["name"] for row in receipt_rows],
            "completed_block_1_receipt_sha256": block_receipt["receipt_sha256"],
            "block_2_receipt_exists": False,
            "stale_execution_receipt": {
                "receipt_sha256": stale_execution["receipt_sha256"],
                "attempt_count": stale_execution["attempt_count"],
                "database_attempt_count": len(records),
                "unrepresented_saved_attempts": len(records)
                - int(stale_execution["attempt_count"]),
                "completed_all": stale_execution["completed_all"],
                "caveat": (
                    "The runner writes execution-receipt.json only after a requested block exits. "
                    "It therefore describes completed block 1 and omits the 48 locally committed "
                    "block-2 attempts saved before interruption."
                ),
            },
        },
        "termination_record": {
            "event": "operator Ctrl-C during cloud scoring after local attempt save",
            "event_source": "operator observation; not inferred from SQLite alone",
            "local_state_evidence": (
                "48 block-2 attempt outcomes and matching completed ledger rows exist; both "
                "block-2 stage sessions remain incomplete and no block-2 receipt exists."
            ),
        },
        "resource_ledger": {
            "conservative_prior_entering_r4b": dict(prior),
            "r4b_observed_attempts": observed,
            "r4c_prior_for_cap_enforcement": r4c_prior,
            "cost_interpretation": (
                "Verified observed cost uses provider-reported completion tokens. Conservative "
                "cost uses each completed call's pinned output envelope and is the cap basis."
            ),
        },
        "reuse_policy": {
            "r4b_attempt_cells_reused": 0,
            "r4b_responses_reused": 0,
            "fresh_r4c_root_required": True,
            "r4c_output_directory_name": "evidence-guide-r4c-grounding-final-r1",
        },
        "source_files": source_files,
        "volatile_file_exclusions": {
            "campaign_sqlite_shm": (
                "SQLite shared-memory coordination state is volatile and is excluded from "
                "scientific identity; the database, WAL, canonical query projections, and "
                "integrity check are retained."
            )
        },
    }
    return {**body, "snapshot_sha256": json_sha256(body)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-root", type=Path, default=DEFAULT_EXECUTION_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    value = build_snapshot(arguments.execution_root)
    if arguments.check:
        existing = _json_file(arguments.output)
        if existing != value:
            raise RuntimeError("R4B_INCIDENT_SNAPSHOT_DRIFT")
        print(json.dumps({"snapshot_sha256": value["snapshot_sha256"], "valid": True}))
        return 0
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(arguments.output), "snapshot_sha256": value["snapshot_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
