"""Create the immutable r2 amendment for the interrupted Evidence Guide study.

The first preregistered campaign completed capability testing before an
outcome-dependent Weave acknowledgement contract invalidated architecture
testing.  This module proves the narrow boundary needed for a clean restart:
capability may be inherited as development evidence, every r1 architecture row
is excluded, no confirmatory stage was opened, and all prior usage still counts
against the original campaign caps.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence_guide_v2_campaign import (
    CAMPAIGN_VERSION,
    CANDIDATE_MODELS,
    canonical_json,
    json_sha256,
    load_corpus,
    select_capability_models,
    stage_attempts,
)
from .evidence_guide_v2_protocol import PLAN_REGISTRY_SHA256, PROTOCOL_SHA256

SCHEMA_VERSION = "1.0"
RECEIPT_KIND = "evidence_guide_v2_protocol_amendment"
PROTOCOL_REVISION = "r2"
R1_PREREGISTRATION_VERSION = "evidence-guide-v2-preregistration-1"
EXPECTED_R1_SOURCE_KEYS = {
    "campaign_orchestrator",
    "cloud_projection",
    "deterministic_scorer",
    "inference_adapter",
    "pricing_snapshot",
    "production_evidence_guide",
    "provider_executor",
    "shared_runtime_protocol",
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(path: Path, logical_name: str) -> dict[str, Any]:
    return {
        "logical_name": logical_name,
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _open_immutable(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    wal = path.with_name(f"{path.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("r1 campaign database is not checkpointed")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "cost_usd": format(
            sum((Decimal(str(row.get("cost_usd") or 0)) for row in rows), Decimal(0)),
            "f",
        ),
        "provider_invocations": sum(int(row.get("provider_invocations") or 0) for row in rows),
        "transport_attempts": sum(int(row.get("transport_attempts") or 0) for row in rows),
        "provider_reported_input_tokens": sum(
            int(row.get("input_tokens") or 0) for row in rows
        ),
        "provider_reported_output_tokens": sum(
            int(row.get("output_tokens") or 0) for row in rows
        ),
        "input_tokens": sum(
            max(
                int(row.get("input_tokens") or 0),
                int(row.get("input_token_envelope") or 0),
            )
            for row in rows
        ),
        "output_tokens": sum(
            max(
                int(row.get("output_tokens") or 0),
                int(row.get("output_token_envelope") or 0),
            )
            for row in rows
        ),
    }


def _sum_usage(*values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cost_usd": format(
            sum((Decimal(str(value["cost_usd"])) for value in values), Decimal(0)), "f"
        ),
        "provider_invocations": sum(int(value["provider_invocations"]) for value in values),
        "transport_attempts": sum(int(value["transport_attempts"]) for value in values),
        "provider_reported_input_tokens": sum(
            int(value["provider_reported_input_tokens"]) for value in values
        ),
        "provider_reported_output_tokens": sum(
            int(value["provider_reported_output_tokens"]) for value in values
        ),
        "input_tokens": sum(int(value["input_tokens"]) for value in values),
        "output_tokens": sum(int(value["output_tokens"]) for value in values),
    }


def _validate_selection(receipt: Mapping[str, Any]) -> None:
    payload = {key: value for key, value in receipt.items() if key != "selection_sha256"}
    if receipt.get("selection_sha256") != json_sha256(payload):
        raise RuntimeError("r1 capability selection digest is invalid")
    selection = receipt.get("selection")
    if not isinstance(selection, Mapping) or selection != {
        "passed": True,
        "passed_models": [
            "Qwen/Qwen3-30B-A3B-Instruct-2507",
            "ibm-granite/granite-4.1-8b",
        ],
        "required_results_per_model": 32,
        "retained_models": ["ibm-granite/granite-4.1-8b"],
        "stage": "capability",
    }:
        raise RuntimeError("r1 capability selection boundary drifted")


def _validate_preflight_receipt(value: Mapping[str, Any]) -> None:
    digest = str(value.get("receipt_sha256") or "")
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if digest != json_sha256(body):
        raise RuntimeError("preflight audit digest is invalid")
    if (
        value.get("scientific_status") != "NO_SCIENTIFIC_RESULT"
        or value.get("capability_denominator_attempts") != 0
        or value.get("architecture_denominator_attempts") != 0
    ):
        raise RuntimeError("preflight audit scientific boundary drifted")


def build_protocol_amendment(
    *,
    r1_root: Path,
    preflight_audit_path: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    root = r1_root.resolve()
    database_path = root / "campaign.sqlite"
    preregistration_path = root / "preregistration.json"
    plan_path = root / "campaign-plan.json"
    for path in (database_path, preregistration_path, plan_path, preflight_audit_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if (
        preregistration.get("campaign_version") != CAMPAIGN_VERSION
        or preregistration.get("preregistration_version") != R1_PREREGISTRATION_VERSION
        or set(preregistration.get("source_hashes", {})) != EXPECTED_R1_SOURCE_KEYS
        or plan.get("preregistration_sha256") != preregistration.get("preregistration_sha256")
    ):
        raise RuntimeError("r1 campaign identity drifted")

    connection = _open_immutable(database_path)
    try:
        metadata = {
            str(row["key"]): json.loads(row["value_json"])
            for row in connection.execute("SELECT key,value_json FROM campaign_meta")
        }
        if metadata != {"preregistration": preregistration}:
            raise RuntimeError("r1 SQLite preregistration binding drifted")
        sessions = [
            dict(row)
            for row in connection.execute(
                "SELECT stage,time_block,session_id,started_at,completed_at "
                "FROM stage_sessions ORDER BY stage,time_block"
            )
        ]
        attempts = [
            json.loads(row["record_json"])
            for row in connection.execute(
                "SELECT record_json FROM attempts ORDER BY attempt_id"
            )
        ]
        receipts = {
            str(row["name"]): json.loads(row["receipt_json"])
            for row in connection.execute("SELECT name,receipt_json FROM receipts")
        }
    finally:
        connection.close()

    capability_sessions = [row for row in sessions if row["stage"] == "capability"]
    architecture_sessions = [row for row in sessions if row["stage"] == "architecture"]
    if (
        {row["time_block"] for row in capability_sessions} != {
            "block-1",
            "block-2",
            "block-3",
        }
        or any(row["completed_at"] is None for row in capability_sessions)
        or len(architecture_sessions) != 1
        or architecture_sessions[0]["time_block"] != "block-1"
        or architecture_sessions[0]["completed_at"] is not None
    ):
        raise RuntimeError("r1 stage boundary drifted")

    stage_counts = Counter(str(row.get("stage")) for row in attempts)
    if stage_counts != {"capability": 160, "architecture": 18}:
        raise RuntimeError("r1 attempt boundary drifted")
    capability_rows = [row for row in attempts if row["stage"] == "capability"]
    architecture_rows = [row for row in attempts if row["stage"] == "architecture"]
    if any(
        row.get("hidden") is not False
        or row.get("details", {}).get("answer_released") is not False
        for row in attempts
    ):
        raise RuntimeError("r1 answer-release or hidden-data boundary drifted")

    corpus = load_corpus(corpus_root.resolve())
    if (
        corpus.manifest_sha256 != preregistration["corpus"]["manifest_sha256"]
        or dict(corpus.split_sha256) != preregistration["corpus"]["split_sha256"]
        or list(CANDIDATE_MODELS) != preregistration["models"]
        or preregistration["shared_runtime_protocol"]
        != {
            "protocol_sha256": PROTOCOL_SHA256,
            "plan_registry_sha256": PLAN_REGISTRY_SHA256,
        }
    ):
        raise RuntimeError("r1 capability corpus or protocol binding drifted")
    scheduled_specs = stage_attempts("capability", corpus)
    scheduled = {spec.attempt_id: spec for spec in scheduled_specs}
    observed = {str(row["attempt_id"]): row for row in capability_rows}
    if len(scheduled) != 160 or set(scheduled) != set(observed):
        raise RuntimeError("r1 capability schedule identity drifted")
    for attempt_id, spec in scheduled.items():
        row = observed[attempt_id]
        expected_fields = {
            "stage": spec.stage,
            "split": spec.split,
            "case_id": spec.case_id,
            "project_family": spec.project_family,
            "model_id": spec.model_id,
            "architecture_id": spec.architecture_id,
            "prompt_id": spec.prompt_id,
            "max_output_tokens": spec.max_output_tokens,
            "repeat": spec.repeat,
            "time_block": spec.time_block,
            "presentation_index": spec.presentation_index,
            "ablation_condition": spec.ablation_condition,
            "hidden": spec.hidden,
        }
        if any(row.get(key) != value for key, value in expected_fields.items()):
            raise RuntimeError("r1 capability schedule field drifted")

    selection = receipts.get("selection:capability")
    if not isinstance(selection, Mapping):
        raise TypeError("r1 capability selection is missing")
    _validate_selection(selection)
    if select_capability_models(capability_rows) != selection["selection"]:
        raise RuntimeError("r1 capability selection is not independently reproducible")
    capability_blocks = [
        value
        for name, value in receipts.items()
        if name.startswith("block:") and value.get("group", {}).get("stage") == "capability"
    ]
    architecture_blocks = [
        value
        for name, value in receipts.items()
        if name.startswith("block:") and value.get("group", {}).get("stage") == "architecture"
    ]
    if len(capability_blocks) != 30 or sum(item["completed"] for item in capability_blocks) != 160:
        raise RuntimeError("r1 capability cloud receipt coverage drifted")
    if len(architecture_blocks) != 2 or sum(item["completed"] for item in architecture_blocks) != 16:
        raise RuntimeError("r1 incomplete architecture receipt boundary drifted")
    if any(int(item.get("reused") or 0) != 0 for item in capability_blocks):
        raise RuntimeError("r1 capability receipt unexpectedly reused an attempt")
    if "selection:architecture" in receipts or any(
        name.startswith("selection:") and name != "selection:capability" for name in receipts
    ):
        raise RuntimeError("r1 advanced beyond the inherited capability boundary")

    biased = [
        row
        for row in architecture_rows
        if row.get("error_codes") == ["WEAVE_AGENT_TURN_INCOMPLETE"]
    ]
    if (
        len(biased) != 1
        or biased[0].get("architecture_id") != "D0"
        or biased[0].get("provider_invocations") != 0
        or biased[0].get("details", {}).get("score", {}).get("passed") is not True
        or biased[0].get("trace_acknowledged") is not False
    ):
        raise RuntimeError("r1 outcome-dependent trace failure boundary drifted")

    preflight = json.loads(preflight_audit_path.read_text(encoding="utf-8"))
    _validate_preflight_receipt(preflight)
    r1_usage = _usage(attempts)
    capability_usage = _usage(capability_rows)
    architecture_usage = _usage(architecture_rows)
    if _sum_usage(capability_usage, architecture_usage) != r1_usage:
        raise RuntimeError("r1 stage usage does not reconcile")
    preflight_usage = {
        "cost_usd": str(preflight["usage"]["recorded_cost_usd"]),
        "provider_invocations": int(preflight["usage"]["provider_invocations"]),
        "transport_attempts": int(preflight["usage"]["transport_attempts"]),
        "provider_reported_input_tokens": int(
            preflight["usage"]["provider_reported_input_tokens"]
        ),
        "provider_reported_output_tokens": int(
            preflight["usage"]["provider_reported_output_tokens"]
        ),
        "input_tokens": int(preflight["usage"]["conservative_accounted_input_tokens"]),
        "output_tokens": int(preflight["usage"]["conservative_accounted_output_tokens"]),
    }
    cumulative = _sum_usage(r1_usage, preflight_usage)

    body = {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": RECEIPT_KIND,
        "campaign_version": CAMPAIGN_VERSION,
        "protocol_revision": PROTOCOL_REVISION,
        "r1_status": "INVALIDATED_BEFORE_CONFIRMATORY_TESTING",
        "failure_code": "OBSERVABILITY_CONTRACT_OUTCOME_DEPENDENT_D0_TOOL_REQUIREMENT",
        "release_eligible": False,
        "source_files": [
            _source(database_path, "r1/campaign.sqlite"),
            _source(preregistration_path, "r1/preregistration.json"),
            _source(plan_path, "r1/campaign-plan.json"),
            _source(preflight_audit_path.resolve(), "prior/preflight-audit.json"),
        ],
        "r1_preregistration_sha256": preregistration["preregistration_sha256"],
        "capability_inheritance": {
            "eligible": True,
            "reason": (
                "Capability completed and was sealed before architecture. Its protocol cases "
                "all require model and tool events, so the D0-only trace-shape defect did not "
                "alter capability advancement."
            ),
            "attempt_count": 160,
            "row_sha256": json_sha256(capability_rows),
            "schedule_sha256": json_sha256(
                [asdict(spec) for spec in sorted(scheduled_specs, key=lambda item: item.attempt_id)]
            ),
            "corpus_manifest_sha256": corpus.manifest_sha256,
            "development_split_sha256": corpus.split_sha256["development"],
            "protocol_sha256": PROTOCOL_SHA256,
            "plan_registry_sha256": PLAN_REGISTRY_SHA256,
            "model_list_sha256": json_sha256(list(CANDIDATE_MODELS)),
            "cloud_block_count": 30,
            "cloud_receipt_sha256": json_sha256(capability_blocks),
            "session_sha256": json_sha256(capability_sessions),
            "usage": capability_usage,
            "outcomes": {
                "completed": sum(row.get("status") == "completed" for row in capability_rows),
                "failed_closed": sum(
                    row.get("status") == "failed_closed" for row in capability_rows
                ),
                "protocol_valid": sum(
                    row.get("protocol_valid") is True for row in capability_rows
                ),
                "trace_acknowledged": sum(
                    row.get("trace_acknowledged") is True for row in capability_rows
                ),
            },
            "selection_receipt": dict(selection),
        },
        "architecture_exclusion": {
            "excluded": True,
            "attempt_count": 18,
            "row_sha256": json_sha256(architecture_rows),
            "completed_cloud_attempt_count": 16,
            "cloud_receipt_sha256": json_sha256(architecture_blocks),
            "biased_attempt_sha256": hashlib.sha256(
                str(biased[0]["attempt_id"]).encode()
            ).hexdigest(),
            "reason": (
                "The trace contract censored a scientifically passing D0 answer; no r1 "
                "architecture row may enter an r2 denominator, selection, or release gate."
            ),
            "usage": architecture_usage,
        },
        "confirmatory_boundary": {
            "validation_started": False,
            "judge_comparison_started": False,
            "hidden_opened": False,
            "soak_started": False,
        },
        "prior_preflight_receipt_sha256": preflight["receipt_sha256"],
        "r1_usage": r1_usage,
        "preflight_usage": preflight_usage,
        "cumulative_prior_usage": cumulative,
        "r2_requirements": {
            "fresh_sqlite_store": True,
            "fresh_preregistration": True,
            "campaign_instance_scoped_cloud_ids": True,
            "trace_contract_source_pinned": True,
            "capability_not_rerun": True,
            "architecture_restarts_from_zero": True,
            "all_prior_usage_counts_toward_original_caps": True,
        },
        "claim_boundary": (
            "This amendment preserves a completed protocol compatibility screen and prior "
            "resource use. It establishes no architecture benefit, practitioner usefulness, "
            "hydraulic accuracy, or ecological outcome."
        ),
    }
    return {**body, "receipt_sha256": json_sha256(body)}


def validate_protocol_amendment(value: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    capability = value.get("capability_inheritance", {})
    architecture = value.get("architecture_exclusion", {})
    try:
        recomputed_r1 = _sum_usage(capability["usage"], architecture["usage"])
        recomputed_cumulative = _sum_usage(recomputed_r1, value["preflight_usage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Evidence Guide v2 protocol amendment is invalid") from exc
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("receipt_kind") != RECEIPT_KIND
        or value.get("protocol_revision") != PROTOCOL_REVISION
        or value.get("receipt_sha256") != json_sha256(body)
        or value.get("capability_inheritance", {}).get("eligible") is not True
        or value.get("architecture_exclusion", {}).get("excluded") is not True
        or value.get("r2_requirements", {}).get("capability_not_rerun") is not True
        or recomputed_r1 != value.get("r1_usage")
        or recomputed_cumulative != value.get("cumulative_prior_usage")
    ):
        raise ValueError("Evidence Guide v2 protocol amendment is invalid")
    return json.loads(canonical_json(value))


def write_protocol_amendment(path: Path, value: Mapping[str, Any]) -> None:
    validated = validate_protocol_amendment(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(validated, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
