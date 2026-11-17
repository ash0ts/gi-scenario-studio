"""Build a deterministic audit receipt for invalid Evidence Guide v2 preflights.

Preflight incidents are operational evidence, not model-evaluation observations.  This
module reads the immutable local SQLite checkpoints, validates the narrow facts needed
to classify each incident, and emits a hash-bound receipt that keeps every invalidated
attempt outside capability and architecture denominators.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
CAMPAIGN_VERSION = "evidence-guide-v2-architecture-first"
RECEIPT_KIND = "invalidated_preflight_audit"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file(path: Path, logical_name: str) -> dict[str, Any]:
    return {
        "logical_name": logical_name,
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _open_immutable_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    wal = path.with_name(f"{path.name}-wal")
    if wal.exists() and wal.stat().st_size != 0:
        raise RuntimeError(f"preflight SQLite WAL is not checkpointed: {path.name}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _load_archive(root: Path, archive_label: str) -> dict[str, Any]:
    database_path = root / "campaign.sqlite"
    plan_path = root / "campaign-plan.json"
    preregistration_path = root / "preregistration.json"
    for path in (database_path, plan_path, preregistration_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    if plan.get("campaign_version") != CAMPAIGN_VERSION or (
        preregistration.get("campaign_version") != CAMPAIGN_VERSION
    ):
        raise RuntimeError(f"preflight campaign identity drifted: {archive_label}")
    if plan.get("preregistration_sha256") != preregistration.get("preregistration_sha256"):
        raise RuntimeError(f"preflight plan/preregistration binding drifted: {archive_label}")

    connection = _open_immutable_database(database_path)
    try:
        metadata_rows = connection.execute(
            "SELECT key, value_json FROM campaign_meta ORDER BY key"
        ).fetchall()
        if [row["key"] for row in metadata_rows] != ["preregistration"]:
            raise RuntimeError(f"unexpected preflight metadata: {archive_label}")
        if json.loads(metadata_rows[0]["value_json"]) != preregistration:
            raise RuntimeError(f"SQLite preregistration drifted: {archive_label}")

        sessions = [
            dict(row)
            for row in connection.execute(
                "SELECT stage, time_block, session_id, started_at, completed_at "
                "FROM stage_sessions ORDER BY stage, time_block"
            )
        ]
        reservations = [
            dict(row)
            for row in connection.execute(
                "SELECT stage, cost_usd, nominal_invocations, worst_case_invocations, "
                "input_tokens, output_tokens, created_at "
                "FROM stage_reservations ORDER BY stage"
            )
        ]
        attempts = [
            json.loads(row["record_json"])
            for row in connection.execute("SELECT record_json FROM attempts ORDER BY attempt_id")
        ]
        receipts = {
            str(row["name"]): json.loads(row["receipt_json"])
            for row in connection.execute("SELECT name, receipt_json FROM receipts ORDER BY name")
        }
    finally:
        connection.close()

    files = [
        _source_file(database_path, f"{archive_label}/campaign.sqlite"),
        _source_file(plan_path, f"{archive_label}/campaign-plan.json"),
        _source_file(preregistration_path, f"{archive_label}/preregistration.json"),
    ]
    return {
        "archive_label": archive_label,
        "archive_content_sha256": _json_sha256(files),
        "source_files": files,
        "plan": plan,
        "preregistration": preregistration,
        "sessions": sessions,
        "reservations": reservations,
        "attempts": attempts,
        "receipts": receipts,
    }


def _sum_int(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    return sum(int(row.get(key) or 0) for row in rows)


def _actual_cost(rows: Sequence[Mapping[str, Any]]) -> str:
    value = sum((Decimal(str(row.get("cost_usd") or "0")) for row in rows), Decimal(0))
    return format(value, "f")


def _usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "provider_invocations": _sum_int(rows, "provider_invocations"),
        "transport_attempts": _sum_int(rows, "transport_attempts"),
        "provider_reported_input_tokens": _sum_int(rows, "input_tokens"),
        "provider_reported_output_tokens": _sum_int(rows, "output_tokens"),
        "conservative_accounted_input_tokens": sum(
            max(int(row.get("input_tokens") or 0), int(row.get("input_token_envelope") or 0))
            for row in rows
        ),
        "conservative_accounted_output_tokens": sum(
            max(
                int(row.get("output_tokens") or 0),
                int(row.get("output_token_envelope") or 0),
            )
            for row in rows
        ),
        "recorded_cost_usd": _actual_cost(rows),
    }


def _reservation(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    if len(rows) != 1 or rows[0].get("stage") != "capability":
        raise RuntimeError("invalidated preflight has an unexpected stage reservation")
    row = rows[0]
    return {
        "stage": "capability",
        "cost_ceiling_usd": str(row["cost_usd"]),
        "logical_provider_call_ceiling": int(row["nominal_invocations"]),
        "transport_attempt_ceiling": int(row["worst_case_invocations"]),
        "input_token_ceiling": int(row["input_tokens"]),
        "output_token_ceiling": int(row["output_tokens"]),
        "measured_use": False,
    }


def _assert_attempt_boundary(attempts: Sequence[Mapping[str, Any]]) -> None:
    for row in attempts:
        if row.get("stage") != "capability" or row.get("hidden") is not False:
            raise RuntimeError("preflight attempt escaped the visible capability boundary")
        if row.get("status") != "failed_closed":
            raise RuntimeError("invalidated preflight contains a released provider result")
        if row.get("trace_acknowledged") is not False:
            raise RuntimeError("invalidated preflight unexpectedly has trace acknowledgement")
        details = row.get("details")
        if not isinstance(details, dict) or details.get("answer_released") is not False:
            raise RuntimeError("invalidated preflight contains an answer-release ambiguity")


def _assert_completed_probe_boundary(attempts: Sequence[Mapping[str, Any]]) -> None:
    for row in attempts:
        if row.get("stage") != "capability" or row.get("hidden") is not False:
            raise RuntimeError("preflight probe escaped the visible capability boundary")
        if row.get("status") != "completed":
            raise RuntimeError("reinitialization preflight contains an incomplete protocol probe")
        if any(
            row.get(key) is not True
            for key in (
                "protocol_valid",
                "safe",
                "repository_fidelity",
                "trace_acknowledged",
            )
        ):
            raise RuntimeError("reinitialization preflight protocol evidence drifted")
        if row.get("error_codes") != []:
            raise RuntimeError("reinitialization preflight probe contains an unexpected error")
        details = row.get("details")
        if not isinstance(details, dict) or details.get("answer_released") is not False:
            raise RuntimeError("reinitialization preflight contains an answer-release ambiguity")


def _artifact_readback_incident(archive: Mapping[str, Any]) -> dict[str, Any]:
    if archive["sessions"] or archive["reservations"] or archive["attempts"] or archive["receipts"]:
        raise RuntimeError("artifact-readback preflight advanced beyond its recorded boundary")
    usage = _usage(())
    return {
        "preflight_id": "preflight-01-artifact-readback-reference",
        "failure_layer": "wandb_artifact_readback",
        "failure_code": "WANDB_ARTIFACT_READBACK_REFERENCE_MALFORMED",
        "root_cause_authority": "operator_diagnosed_execution_failure",
        "scientific_status": "INVALIDATED_PREFLIGHT",
        "scientifically_usable_provider_results": 0,
        "included_in_capability_denominator": False,
        "included_in_architecture_denominator": False,
        "model_quality_inference_permitted": False,
        "architecture_efficacy_inference_permitted": False,
        "answer_released": False,
        "session_completed": False,
        "block_receipt_present": False,
        "attempt_count": 0,
        "protocol_valid_attempts": 0,
        "trace_acknowledged_attempts": 0,
        "observed_attempt_error_codes": [],
        "usage": usage,
        "reservation": None,
        "cloud_evidence": {
            "verified_local_receipts": [],
            "orphan_upload_possible": True,
            "claim_boundary": (
                "No local cloud receipt survived readback; upload existence and lineage are "
                "therefore not asserted by this audit."
            ),
        },
        "source_archive": {
            "archive_label": archive["archive_label"],
            "archive_content_sha256": archive["archive_content_sha256"],
            "source_files": archive["source_files"],
        },
    }


def _weave_collision_incident(archive: Mapping[str, Any]) -> dict[str, Any]:
    sessions = archive["sessions"]
    attempts = archive["attempts"]
    receipts = archive["receipts"]
    if len(sessions) != 1 or sessions[0].get("stage") != "capability":
        raise RuntimeError("Weave preflight session shape drifted")
    if sessions[0].get("completed_at") is not None:
        raise RuntimeError("Weave preflight was unexpectedly completed")
    if set(receipts) != {"preregistration_cloud", "visible_datasets_cloud"}:
        raise RuntimeError("Weave preflight cloud receipt set drifted")
    if any(name.startswith("block:") for name in receipts):
        raise RuntimeError("Weave preflight unexpectedly has a completed block receipt")
    if len(attempts) != 2:
        raise RuntimeError("Weave preflight attempt count drifted")
    _assert_attempt_boundary(attempts)
    if any(row.get("error_codes") != ["WEAVE_AGENT_TURN_INCOMPLETE"] for row in attempts):
        raise RuntimeError("Weave preflight failure code drifted")
    if {row.get("model_id") for row in attempts} != {"Qwen/Qwen3-30B-A3B-Instruct-2507"} or {
        row.get("architecture_id") for row in attempts
    } != {"A3"}:
        raise RuntimeError("Weave preflight diagnostic identity drifted")

    preregistration_receipt = receipts["preregistration_cloud"]
    preregistration_source = next(
        item
        for item in archive["source_files"]
        if item["logical_name"].endswith("/preregistration.json")
    )
    if preregistration_receipt.get("source_sha256") != preregistration_source["sha256"] or (
        preregistration_receipt.get("readback_sha256") != preregistration_source["sha256"]
    ):
        raise RuntimeError("Weave preflight preregistration readback drifted")
    visible_references = receipts["visible_datasets_cloud"].get("references")
    if not isinstance(visible_references, dict) or set(visible_references) != {
        "development",
        "validation",
        "judge_calibration",
    }:
        raise RuntimeError("Weave preflight visible dataset receipt drifted")

    error_codes = sorted({str(code) for row in attempts for code in (row.get("error_codes") or ())})
    return {
        "preflight_id": "preflight-02-weave-evaluation-name-collision",
        "failure_layer": "weave_evaluation_publication",
        "failure_code": "WEAVE_EVALUATION_DATASET_NAME_COLLISION",
        "root_cause_authority": "operator_diagnosed_execution_failure",
        "scientific_status": "INVALIDATED_PREFLIGHT",
        "scientifically_usable_provider_results": 0,
        "included_in_capability_denominator": False,
        "included_in_architecture_denominator": False,
        "model_quality_inference_permitted": False,
        "architecture_efficacy_inference_permitted": False,
        "answer_released": False,
        "session_completed": False,
        "block_receipt_present": False,
        "attempt_count": len(attempts),
        "protocol_valid_attempts": sum(row.get("protocol_valid") is True for row in attempts),
        "trace_acknowledged_attempts": sum(
            row.get("trace_acknowledged") is True for row in attempts
        ),
        "observed_attempt_error_codes": error_codes,
        "usage": _usage(attempts),
        "reservation": _reservation(archive["reservations"]),
        "cloud_evidence": {
            "verified_local_receipts": sorted(receipts),
            "preregistration_run_url": preregistration_receipt.get("run_url"),
            "preregistration_artifact_reference": preregistration_receipt.get("artifact_reference"),
            "visible_dataset_references": visible_references,
            "orphan_upload_possible": False,
            "claim_boundary": (
                "Cloud preregistration and visible-dataset receipts were read back; no "
                "completed component-block receipt exists."
            ),
        },
        "source_archive": {
            "archive_label": archive["archive_label"],
            "archive_content_sha256": archive["archive_content_sha256"],
            "source_files": archive["source_files"],
        },
    }


def _weave_reinitialization_incident(archive: Mapping[str, Any]) -> dict[str, Any]:
    sessions = archive["sessions"]
    attempts = archive["attempts"]
    receipts = archive["receipts"]
    if len(sessions) != 1 or sessions[0].get("stage") != "capability":
        raise RuntimeError("Weave reinitialization preflight session shape drifted")
    if sessions[0].get("completed_at") is not None:
        raise RuntimeError("Weave reinitialization preflight was unexpectedly completed")
    block_receipt_names = sorted(name for name in receipts if name.startswith("block:"))
    if len(block_receipt_names) != 1 or set(receipts) != {
        "preregistration_cloud",
        "visible_datasets_cloud",
        block_receipt_names[0],
    }:
        raise RuntimeError("Weave reinitialization preflight receipt set drifted")
    if len(attempts) != 6:
        raise RuntimeError("Weave reinitialization preflight attempt count drifted")
    _assert_completed_probe_boundary(attempts)
    if {row.get("model_id") for row in attempts} != {"Qwen/Qwen3-30B-A3B-Instruct-2507"} or {
        row.get("architecture_id") for row in attempts
    } != {"A3"}:
        raise RuntimeError("Weave reinitialization diagnostic identity drifted")

    preregistration_receipt = receipts["preregistration_cloud"]
    preregistration_source = next(
        item
        for item in archive["source_files"]
        if item["logical_name"].endswith("/preregistration.json")
    )
    if preregistration_receipt.get("source_sha256") != preregistration_source["sha256"] or (
        preregistration_receipt.get("readback_sha256") != preregistration_source["sha256"]
    ):
        raise RuntimeError("Weave reinitialization preregistration readback drifted")
    visible_references = receipts["visible_datasets_cloud"].get("references")
    if not isinstance(visible_references, dict) or set(visible_references) != {
        "development",
        "validation",
        "judge_calibration",
    }:
        raise RuntimeError("Weave reinitialization visible dataset receipt drifted")
    block_receipt = receipts[block_receipt_names[0]]
    cloud = block_receipt.get("cloud")
    group = block_receipt.get("group")
    if (
        not isinstance(cloud, dict)
        or not isinstance(group, dict)
        or block_receipt.get("completed") != 6
        or block_receipt.get("reused") != 0
        or cloud.get("attempt_count") != 6
        or cloud.get("hidden") is not False
        or cloud.get("mode") != "wandb_weave"
        or group.get("stage") != "capability"
        or group.get("architecture_id") != "A3"
        or group.get("model_id") != "Qwen/Qwen3-30B-A3B-Instruct-2507"
        or group.get("hidden") is not False
    ):
        raise RuntimeError("Weave reinitialization completed-block receipt drifted")

    return {
        "preflight_id": "weave-reinitialization-flush",
        "failure_layer": "weave_reinitialization_finalizer",
        "failure_code": "WEAVE_REINITIALIZATION_FLUSH_STALL",
        "root_cause_authority": "operator_diagnosed_execution_failure",
        "scientific_status": "INVALIDATED_PREFLIGHT",
        "scientifically_usable_provider_results": 0,
        "included_in_capability_denominator": False,
        "included_in_architecture_denominator": False,
        "model_quality_inference_permitted": False,
        "architecture_efficacy_inference_permitted": False,
        "answer_released": False,
        "session_completed": False,
        "block_receipt_present": True,
        "attempt_count": len(attempts),
        "protocol_valid_attempts": sum(row.get("protocol_valid") is True for row in attempts),
        "trace_acknowledged_attempts": sum(
            row.get("trace_acknowledged") is True for row in attempts
        ),
        "observed_attempt_error_codes": [],
        "usage": _usage(attempts),
        "reservation": _reservation(archive["reservations"]),
        "cloud_evidence": {
            "verified_local_receipts": sorted(receipts),
            "preregistration_run_url": preregistration_receipt.get("run_url"),
            "preregistration_artifact_reference": preregistration_receipt.get("artifact_reference"),
            "visible_dataset_references": visible_references,
            "completed_block_run_url": cloud.get("run_url"),
            "completed_block_weave_evaluation_uri": cloud.get("weave_evaluation_uri"),
            "orphan_upload_possible": False,
            "claim_boundary": (
                "Six protocol probes and one component-block receipt completed, but the "
                "capability time-block session did not. The probes are retained as operational "
                "evidence and excluded from scientific denominators."
            ),
        },
        "source_archive": {
            "archive_label": archive["archive_label"],
            "archive_content_sha256": archive["archive_content_sha256"],
            "source_files": archive["source_files"],
        },
    }


def _total_usage(incidents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usage_rows = [incident["usage"] for incident in incidents]
    return {
        "provider_invocations": _sum_int(usage_rows, "provider_invocations"),
        "transport_attempts": _sum_int(usage_rows, "transport_attempts"),
        "provider_reported_input_tokens": _sum_int(usage_rows, "provider_reported_input_tokens"),
        "provider_reported_output_tokens": _sum_int(usage_rows, "provider_reported_output_tokens"),
        "conservative_accounted_input_tokens": _sum_int(
            usage_rows, "conservative_accounted_input_tokens"
        ),
        "conservative_accounted_output_tokens": _sum_int(
            usage_rows, "conservative_accounted_output_tokens"
        ),
        "recorded_cost_usd": format(
            sum(
                (Decimal(str(row["recorded_cost_usd"])) for row in usage_rows),
                Decimal(0),
            ),
            "f",
        ),
    }


def build_preflight_audit(
    *,
    artifact_readback_root: Path,
    weave_collision_root: Path,
    weave_reinitialization_root: Path | None = None,
) -> dict[str, Any]:
    """Build and validate the known invalidated-preflight records."""

    artifact_readback = _load_archive(artifact_readback_root.resolve(), "preflight-01")
    weave_collision = _load_archive(weave_collision_root.resolve(), "preflight-02")
    incidents = [
        _artifact_readback_incident(artifact_readback),
        _weave_collision_incident(weave_collision),
    ]
    if weave_reinitialization_root is not None:
        incidents.append(
            _weave_reinitialization_incident(
                _load_archive(weave_reinitialization_root.resolve(), "preflight-03")
            )
        )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_version": CAMPAIGN_VERSION,
        "receipt_kind": RECEIPT_KIND,
        "scientific_status": "NO_SCIENTIFIC_RESULT",
        "capability_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "scientifically_usable_provider_results": 0,
        "invalidated_provider_attempts": sum(
            int(incident["attempt_count"]) for incident in incidents
        ),
        "invalidated_preflight_count": len(incidents),
        "usage": _total_usage(incidents),
        "incidents": incidents,
        "claim_boundary": (
            "These records audit launch and observability integration failures. They do not "
            "measure model quality, architecture efficacy, explanation quality, or practitioner "
            "usefulness, and they are excluded from every scientific denominator."
        ),
        "practitioner_usefulness_status": "not_measured",
    }
    return {**receipt, "receipt_sha256": _json_sha256(receipt)}


def verify_preflight_audit(receipt: Mapping[str, Any]) -> None:
    """Validate receipt integrity and the scientific-denominator boundary."""

    body = dict(receipt)
    digest = body.pop("receipt_sha256", None)
    if not isinstance(digest, str) or digest != _json_sha256(body):
        raise RuntimeError("Evidence Guide v2 preflight audit digest drifted")
    if body.get("schema_version") != SCHEMA_VERSION or (
        body.get("campaign_version") != CAMPAIGN_VERSION or body.get("receipt_kind") != RECEIPT_KIND
    ):
        raise RuntimeError("Evidence Guide v2 preflight audit identity drifted")
    if (
        body.get("scientific_status") != "NO_SCIENTIFIC_RESULT"
        or body.get("capability_denominator_attempts") != 0
        or body.get("architecture_denominator_attempts") != 0
        or body.get("scientifically_usable_provider_results") != 0
        or body.get("practitioner_usefulness_status") != "not_measured"
    ):
        raise RuntimeError("Evidence Guide v2 preflight scientific boundary drifted")
    incidents = body.get("incidents")
    if not isinstance(incidents, list) or len(incidents) not in {2, 3}:
        raise RuntimeError("Evidence Guide v2 preflight incident count drifted")
    expected_ids = [
        "preflight-01-artifact-readback-reference",
        "preflight-02-weave-evaluation-name-collision",
    ]
    expected_failures = [
        "WANDB_ARTIFACT_READBACK_REFERENCE_MALFORMED",
        "WEAVE_EVALUATION_DATASET_NAME_COLLISION",
    ]
    expected_attempts = [0, 2]
    expected_usage_contract = {
        "provider_invocations": 4,
        "transport_attempts": 4,
        "provider_reported_input_tokens": 356,
        "provider_reported_output_tokens": 52,
        "conservative_accounted_input_tokens": 1926,
        "conservative_accounted_output_tokens": 512,
        "recorded_cost_usd": "0.000054",
    }
    if len(incidents) == 3:
        expected_ids.append("weave-reinitialization-flush")
        expected_failures.append("WEAVE_REINITIALIZATION_FLUSH_STALL")
        expected_attempts.append(6)
        expected_usage_contract = {
            "provider_invocations": 16,
            "transport_attempts": 16,
            "provider_reported_input_tokens": 2307,
            "provider_reported_output_tokens": 208,
            "conservative_accounted_input_tokens": 11513,
            "conservative_accounted_output_tokens": 2048,
            "recorded_cost_usd": "0.000302",
        }
    if (
        body.get("invalidated_preflight_count") != len(incidents)
        or [incident.get("preflight_id") for incident in incidents] != expected_ids
    ):
        raise RuntimeError("Evidence Guide v2 preflight identity set drifted")
    if [incident.get("failure_code") for incident in incidents] != expected_failures:
        raise RuntimeError("Evidence Guide v2 preflight failure set drifted")
    if any(
        incident.get("scientific_status") != "INVALIDATED_PREFLIGHT"
        or incident.get("included_in_capability_denominator") is not False
        or incident.get("included_in_architecture_denominator") is not False
        or incident.get("scientifically_usable_provider_results") != 0
        or incident.get("model_quality_inference_permitted") is not False
        or incident.get("architecture_efficacy_inference_permitted") is not False
        or incident.get("answer_released") is not False
        for incident in incidents
    ):
        raise RuntimeError("Evidence Guide v2 preflight incident boundary drifted")
    if [int(incident.get("attempt_count") or 0) for incident in incidents] != expected_attempts:
        raise RuntimeError("Evidence Guide v2 preflight attempt distribution drifted")
    if len(incidents) == 3:
        third = incidents[2]
        if (
            third.get("block_receipt_present") is not True
            or third.get("session_completed") is not False
            or third.get("protocol_valid_attempts") != 6
            or third.get("trace_acknowledged_attempts") != 6
            or third.get("observed_attempt_error_codes") != []
        ):
            raise RuntimeError("Evidence Guide v2 reinitialization probe boundary drifted")
    if int(body.get("invalidated_provider_attempts") or 0) != sum(
        int(incident.get("attempt_count") or 0) for incident in incidents
    ):
        raise RuntimeError("Evidence Guide v2 preflight attempt total drifted")
    expected_usage = _total_usage(incidents)
    if body.get("usage") != expected_usage:
        raise RuntimeError("Evidence Guide v2 preflight usage total drifted")
    if expected_usage != expected_usage_contract:
        raise RuntimeError("Evidence Guide v2 preflight observed usage drifted")
    expected_reservation = {
        "stage": "capability",
        "cost_ceiling_usd": "0.264320",
        "logical_provider_call_ceiling": 320,
        "transport_attempt_ceiling": 640,
        "input_token_ceiling": 1_310_720,
        "output_token_ceiling": 81_920,
        "measured_use": False,
    }
    if incidents[0].get("reservation") is not None or any(
        incident.get("reservation") != expected_reservation for incident in incidents[1:]
    ):
        raise RuntimeError("Evidence Guide v2 preflight reservation drifted")
    for incident in incidents:
        source_archive = incident.get("source_archive")
        if not isinstance(source_archive, dict):
            raise TypeError("Evidence Guide v2 preflight source archive is missing")
        source_files = source_archive.get("source_files")
        if not isinstance(source_files, list) or len(source_files) != 3:
            raise RuntimeError("Evidence Guide v2 preflight source file set drifted")
        if source_archive.get("archive_content_sha256") != _json_sha256(source_files):
            raise RuntimeError("Evidence Guide v2 preflight archive digest drifted")
        if any(
            not isinstance(item, dict)
            or set(item) != {"logical_name", "sha256", "size_bytes"}
            or not isinstance(item["logical_name"], str)
            or item["logical_name"].startswith("/")
            or len(str(item["sha256"])) != 64
            or int(item["size_bytes"]) <= 0
            for item in source_files
        ):
            raise RuntimeError("Evidence Guide v2 preflight source manifest drifted")
    encoded = _canonical_json(body).decode("utf-8")
    if "/Users/" in encoded or "file://" in encoded:
        raise RuntimeError("Evidence Guide v2 preflight audit contains a local path")


def write_preflight_audit(path: Path, receipt: Mapping[str, Any]) -> None:
    verify_preflight_audit(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
