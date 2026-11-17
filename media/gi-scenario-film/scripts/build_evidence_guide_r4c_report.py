#!/usr/bin/env python3
"""Build the receipt-bound R4c article package from exact local evidence.

This adapter never calls a model and never publishes. It accepts the R4c
results only after the completed analysis, the preserved stale execution
receipt, the publication-recovery readback receipt, source tables, and the
R4/R4b/R4c incident records all agree by hash.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
PAPER = "#F5F7F6"
INK = "#17313A"
INK_SOFT = "#425A62"
TEAL = "#08766F"
TEAL_WASH = "#DCEDEA"
BLUE = "#2F6F9F"
BLUE_WASH = "#E2ECF3"
OCHRE = "#A96315"
OCHRE_WASH = "#F4E8D9"
RED = "#B43D47"
RED_WASH = "#F4E0E2"
LINE = "#C8D2D0"
WHITE = "#FFFFFF"

STUDY_VERSION = "gi-evidence-guide-grounding-r4c-final"
EXPECTED_ENTITY = "wandb"
EXPECTED_PROJECT = "gi-scenario-studio-agent-evals"
EXPECTED_MODELS = (
    "ibm-granite/granite-4.1-8b",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
)
SOURCE_FILES = (
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
TABLE_COLUMNS = (
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


class R4CPackageError(ValueError):
    """The source package cannot support the R4c report projection."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise R4CPackageError(f"Could not read {path.name}") from error
    if not isinstance(payload, dict):
        raise R4CPackageError(f"{path.name} must contain a JSON object")
    return payload


def _rows(payload: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise R4CPackageError(f"{label} must contain an object row list")
    return rows


def _number(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise R4CPackageError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise R4CPackageError(f"{label} must be numeric") from error
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise R4CPackageError(f"{label} is outside its valid range")
    return result


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise R4CPackageError(f"{label} must be an integer >= {minimum}")
    return value


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise R4CPackageError(f"{label} must be a decimal") from error
    if not result.is_finite() or result < 0:
        raise R4CPackageError(f"{label} must be finite and nonnegative")
    return result


def _self_hash(payload: dict[str, Any], field: str, *, label: str) -> None:
    claimed = payload.get(field)
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise R4CPackageError(f"{label} lacks {field}")
    body = {key: value for key, value in payload.items() if key != field}
    if _json_sha256(body) != claimed:
        raise R4CPackageError(f"{label} self-hash drifted")


def _cloud_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise R4CPackageError(f"{label} is missing")
    parsed = urlparse(value)
    expected = f"/{EXPECTED_ENTITY}/{EXPECTED_PROJECT}/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "wandb.ai"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(expected)
    ):
        raise R4CPackageError(f"{label} does not match the pinned W&B destination")
    return value


def _validate_incidents(
    r4b_amendment_path: Path,
    r4c_amendment_path: Path,
    r4b_snapshot_path: Path,
    publication_amendment_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    paths = {
        "protocol-amendment-r4b.json": r4b_amendment_path,
        "protocol-amendment-r4c.json": r4c_amendment_path,
        "r4b-incident-snapshot.json": r4b_snapshot_path,
        "publication-amendment-r4c.json": publication_amendment_path,
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise R4CPackageError("Checked-in protocol evidence is incomplete: " + ", ".join(missing))
    loaded = {name: _load_object(path) for name, path in paths.items()}
    hashes = {name: _sha256(path) for name, path in paths.items()}
    r4b = loaded["protocol-amendment-r4b.json"]
    r4c = loaded["protocol-amendment-r4c.json"]
    snapshot = loaded["r4b-incident-snapshot.json"]
    _self_hash(snapshot, "snapshot_sha256", label="R4b incident snapshot")

    r4 = r4b.get("r4_live_incident")
    diagnostic = r4b.get("six_call_schema_diagnostic")
    if (
        r4b.get("superseded_study_version") != "gi-evidence-guide-grounding-r4-final"
        or r4b.get("replacement_study_version") != "gi-evidence-guide-grounding-r4b-final"
        or not isinstance(r4, dict)
        or r4.get("attempt_count") != 224
        or r4.get("provider_response_count") != 0
        or r4.get("failed_closed_count") != 224
        or r4.get("scientific_denominator") != 0
        or r4.get("answers_released") != 0
        or r4.get("production_release_eligible") is not False
        or not isinstance(diagnostic, dict)
        or diagnostic.get("expected_unique_items_failures") != 2
        or diagnostic.get("successful_calls") != 4
    ):
        raise R4CPackageError("The preserved R4 schema incident drifted")

    incident_ref = r4c.get("incident_snapshot")
    summary = snapshot.get("incident_summary")
    if (
        r4c.get("superseded_study_version") != "gi-evidence-guide-grounding-r4b-final"
        or r4c.get("replacement_study_version") != STUDY_VERSION
        or not isinstance(incident_ref, dict)
        or incident_ref.get("file_sha256") != hashes["r4b-incident-snapshot.json"]
        or incident_ref.get("snapshot_sha256") != snapshot.get("snapshot_sha256")
        or not isinstance(summary, dict)
        or summary.get("provider_invocations") != 272
        or summary.get("transport_attempts") != 272
        or summary.get("accepted_traced_responses") != 238
        or summary.get("post_response_input_cap_rejections") != 34
        or summary.get("r4b_cells_reused_by_r4c") != 0
        or (snapshot.get("execution_ledger") or {}).get("ambiguous_ledger_states") != 0
    ):
        raise R4CPackageError("The preserved R4b resource-guard incident drifted")
    return loaded, hashes


def _validate_source(
    source_root: Path,
    r4b_amendment_path: Path,
    r4c_amendment_path: Path,
    r4b_snapshot_path: Path,
    publication_amendment_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    paths = {name: source_root / name for name in SOURCE_FILES}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise R4CPackageError("Final R4c source package is incomplete: " + ", ".join(missing))
    loaded = {name: _load_object(path) for name, path in paths.items()}
    incidents, incident_hashes = _validate_incidents(
        r4b_amendment_path,
        r4c_amendment_path,
        r4b_snapshot_path,
        publication_amendment_path,
    )
    source_hashes = {name: _sha256(path) for name, path in paths.items()} | incident_hashes

    analysis = loaded["publication/analysis.json"]
    attempts_payload = loaded["publication/attempts.json"]
    execution = loaded["execution-receipt.json"]
    preregistration = loaded["preregistration.json"]
    recovery = loaded[
        "publication-recovery/publication-recovery-cloud-receipt.json"
    ]
    _self_hash(analysis, "analysis_sha256", label="analysis")
    _self_hash(execution, "receipt_sha256", label="execution receipt")
    _self_hash(preregistration, "preregistration_sha256", label="preregistration")

    versioned = [analysis, execution, preregistration]
    versioned.extend(
        loaded[name]
        for name in SOURCE_FILES
        if name.startswith("publication/") and name != "publication/analysis.json"
    )
    if any(item.get("study_version") != STUDY_VERSION for item in versioned):
        raise R4CPackageError("R4c study version drifted across source files")
    instance = analysis.get("study_instance_sha256")
    if not isinstance(instance, str) or len(instance) != 64:
        raise R4CPackageError("R4c analysis lacks a study identity")
    if any(item.get("study_instance_sha256") != instance for item in (execution, preregistration)):
        raise R4CPackageError("R4c study identity drifted")
    if analysis.get("schedule_sha256") != preregistration.get("schedule_sha256"):
        raise R4CPackageError("R4c analysis and preregistration schedules differ")
    if execution.get("preregistration_sha256") != preregistration.get("preregistration_sha256"):
        raise R4CPackageError("R4c execution is not bound to its preregistration")

    attempts = _rows(attempts_payload, label="attempts")
    attempt_count = _integer(analysis.get("attempt_count"), label="analysis attempt_count")
    if attempt_count != 672 or len(attempts) != 672:
        raise R4CPackageError("R4c attempt denominator drifted")
    if len({row.get("attempt_id") for row in attempts}) != len(attempts):
        raise R4CPackageError("R4c attempts contain duplicate identities")
    publication_amendment = incidents["publication-amendment-r4c.json"]
    stale = publication_amendment.get("pre_recovery_execution_receipt")
    if (
        execution.get("execution_mode") != "live"
        or execution.get("completed_all") is not False
        or execution.get("attempt_count") != 448
        or not isinstance(stale, dict)
        or stale.get("file_sha256") != source_hashes["execution-receipt.json"]
        or stale.get("receipt_sha256") != execution.get("receipt_sha256")
        or stale.get("attempt_count") != execution.get("attempt_count")
        or stale.get("completed_all") != execution.get("completed_all")
    ):
        raise R4CPackageError("The preserved stale execution receipt drifted")
    if analysis.get("stage_attempt_counts") != {"grounding": 384, "prose_diagnosis": 288}:
        raise R4CPackageError("R4c stage attempt counts drifted")
    stages: dict[str, int] = defaultdict(int)
    for row in attempts:
        stages[str(row.get("stage"))] += 1
    if dict(stages) != analysis.get("stage_attempt_counts"):
        raise R4CPackageError("R4c attempt rows do not match the stage denominators")

    required_disabled = (
        analysis.get("production_state") == "DISABLE_LIVE_GUIDE"
        and execution.get("production_state") == "DISABLE_LIVE_GUIDE"
        and analysis.get("production_release_eligible") is False
        and execution.get("production_release_eligible") is False
        and analysis.get("actual_answers_released") == 0
        and execution.get("actual_answers_released") == 0
        and analysis.get("practitioner_usefulness_status") == "not_measured"
        and execution.get("practitioner_usefulness_status") == "not_measured"
        and analysis.get("all_experimental_receipts_valid") is True
    )
    if not required_disabled:
        raise R4CPackageError("R4c release or authority boundary drifted")

    d1 = preregistration.get("d1_positive_control")
    if (
        not isinstance(d1, dict)
        or d1.get("all_passed") is not True
        or d1.get("cell_count") != 192
        or d1.get("passed_count") != 192
        or analysis.get("d1_positive_control_gate_sha256") != d1.get("gate_sha256")
    ):
        raise R4CPackageError("The deterministic D1 control drifted")

    expected_tables = {
        "grounding_mechanism_results": "publication/grounding-mechanisms.json",
        "grounding_primary_unique_case_results": "publication/grounding-primary.json",
        "prose_cap_paired_mcnemar": "publication/prose-cap-mcnemar.json",
        "prose_cap_primary_unique_case_results": "publication/prose-cap-primary.json",
        "attempt_level_diagnostics_not_inferential_units": "publication/attempt-diagnostics.json",
    }
    for analysis_key, name in expected_tables.items():
        if analysis.get(analysis_key) != _rows(loaded[name], label=name):
            raise R4CPackageError(f"Analysis projection drifted from {name}")

    _self_hash(recovery, "receipt_sha256", label="publication recovery receipt")
    publication_files = {
        Path(name).name: source_hashes[name]
        for name in SOURCE_FILES
        if name.startswith("publication/") and "/publication-recovery/" not in name
    }
    package_identity = _json_sha256(
        {"kind": "r4c_original_seven_file_publication", "files": publication_files}
    )
    original = publication_amendment.get("original_publication")
    recovery_method = publication_amendment.get("recovery_method")
    destination = publication_amendment.get("destination")
    if (
        not isinstance(original, dict)
        or original.get("files") != publication_files
        or original.get("package_sha256") != package_identity
        or original.get("analysis_file_sha256")
        != source_hashes["publication/analysis.json"]
        or original.get("analysis_sha256") != analysis.get("analysis_sha256")
        or not isinstance(recovery_method, dict)
        or recovery_method.get("table_projection_version")
        != "canonical-json-string-per-cell-v1"
        or destination
        != {"entity": EXPECTED_ENTITY, "project": EXPECTED_PROJECT, "required_access": "Restricted"}
    ):
        raise R4CPackageError("The R4c publication-recovery amendment drifted")
    recovery_identity = _json_sha256(
        {
            "recovery_version": publication_amendment["recovery_version"],
            "study_instance_sha256": instance,
            "preregistration_sha256": preregistration["preregistration_sha256"],
            "amendment_sha256": source_hashes["publication-amendment-r4c.json"],
            "package_sha256": package_identity,
            "table_projection_version": recovery_method["table_projection_version"],
            "destination": destination,
        }
    )
    cloud = recovery.get("publication_cloud_receipt")
    if (
        recovery.get("recovery_version") != publication_amendment.get("recovery_version")
        or recovery.get("mode") != "wandb_publication_recovery"
        or recovery.get("network_calls") != "publication_only"
        or recovery.get("provider_calls") != 0
        or recovery.get("inference_reexecuted") is not False
        or recovery.get("attempts_validated") != 672
        or recovery.get("provider_responses_validated") != 672
        or recovery.get("trace_acknowledgements_validated") != 672
        or recovery.get("block_receipts_validated") != 3
        or recovery.get("actual_answers_released") != 0
        or recovery.get("production_state") != "DISABLE_LIVE_GUIDE"
        or recovery.get("practitioner_usefulness_status") != "not_measured"
        or recovery.get("analysis_file_sha256")
        != source_hashes["publication/analysis.json"]
        or recovery.get("analysis_sha256") != analysis.get("analysis_sha256")
        or recovery.get("original_files") != publication_files
        or recovery.get("original_package_sha256") != package_identity
        or recovery.get("publication_amendment_sha256")
        != source_hashes["publication-amendment-r4c.json"]
        or recovery.get("recovery_identity_sha256") != recovery_identity
        or not isinstance(cloud, dict)
        or cloud.get("kind") != "r4c_publication_recovery"
        or cloud.get("entity") != EXPECTED_ENTITY
        or cloud.get("project") != EXPECTED_PROJECT
        or cloud.get("source_sha256") != recovery_identity
    ):
        raise R4CPackageError("R4c publication recovery receipt drifted")
    _cloud_url(cloud.get("run_url"), label="R4c recovery W&B run URL")
    artifact = cloud.get("artifact_reference")
    if not isinstance(artifact, str) or not artifact.startswith(
        f"{EXPECTED_ENTITY}/{EXPECTED_PROJECT}/"
    ):
        raise R4CPackageError("R4c artifact reference drifted")
    return loaded, incidents, source_hashes


def _mechanism_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    observed = set()
    for row in rows:
        model = str(row.get("model_id") or "")
        split = str(row.get("split") or "")
        denominator = _integer(row.get("unique_case_count"), label="mechanism denominator", minimum=1)
        observed.add((model, split))
        item = {
            "model_id": model,
            "split": split,
            "project_families": row.get("project_families"),
            "denominator": denominator,
        }
        for label, field in (
            ("threshold_sensitivity", "threshold_sensitivity_case_success"),
            ("distractor_specificity", "distractor_specificity_case_success"),
            ("withheld_target_abstention", "missing_evidence_abstention_case_success"),
            ("complete_mechanism", "complete_mechanism_case_success"),
        ):
            success = _integer(row.get(field), label=field)
            item[label] = {"success": success, "rate": success / denominator}
        result.append(item)
    expected = {(model, split) for model in EXPECTED_MODELS for split in ("development", "validation")}
    if observed != expected:
        raise R4CPackageError("R4c mechanism projection lacks a model/split cell")
    return sorted(result, key=lambda row: (row["split"], EXPECTED_MODELS.index(row["model_id"])))


def _aggregate_prose(
    primary: list[dict[str, Any]], mcnemar: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"success": 0, "denominator": 0})
    )
    paired: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in primary:
        model = str(row.get("model_id") or "")
        cap = _integer(row.get("max_output_tokens"), label="prose output cap", minimum=1)
        if cap not in {128, 256}:
            raise R4CPackageError("R4c prose report contains an unexpected output cap")
        grouped[model][cap]["success"] += _integer(
            row.get("protocol_valid_case_success"), label="prose success count"
        )
        grouped[model][cap]["denominator"] += _integer(
            row.get("unique_case_count"), label="prose denominator", minimum=1
        )
    for row in mcnemar:
        model = str(row.get("model_id") or "")
        paired[model]["success_128_only"] += _integer(
            row.get("success_128_only"), label="128-only successes"
        )
        paired[model]["success_256_only"] += _integer(
            row.get("success_256_only"), label="256-only successes"
        )
        paired[model]["paired_case_conditions"] += _integer(
            row.get("paired_unique_case_count"), label="paired prose denominator", minimum=1
        )
    result = []
    for model in EXPECTED_MODELS:
        if set(grouped[model]) != {128, 256}:
            raise R4CPackageError("R4c prose report lacks a 128/256 cap pair")
        result.append(
            {
                "model_id": model,
                "caps": {
                    str(cap): {
                        **grouped[model][cap],
                        "rate": grouped[model][cap]["success"]
                        / grouped[model][cap]["denominator"],
                    }
                    for cap in (128, 256)
                },
                "paired": dict(paired[model]),
            }
        )
    return result


def _nearest_rank(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def _aggregate_usage(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        grouped[(str(row.get("model_id") or ""), str(row.get("stage") or ""))].append(row)
    result = []
    for model in EXPECTED_MODELS:
        for stage in ("grounding", "prose_diagnosis"):
            rows = grouped[(model, stage)]
            if not rows:
                raise R4CPackageError("R4c usage ledger lacks a model/stage cell")
            latencies = [
                _number(row.get("latency_ms"), label="attempt latency", minimum=0)
                for row in rows
            ]
            cost = sum(
                (_decimal(row.get("cost_usd", "0"), label="attempt cost") for row in rows),
                Decimal(0),
            )
            provider_responses = sum(
                bool((row.get("details") or {}).get("provider_response_received"))
                for row in rows
            )
            trace_acknowledged = sum(bool(row.get("trace_acknowledged")) for row in rows)
            protocol_valid = sum(bool(row.get("protocol_valid")) for row in rows)
            end_to_end = sum(
                bool((row.get("details") or {}).get("end_to_end_conformance"))
                for row in rows
            )
            result.append(
                {
                    "model_id": model,
                    "stage": stage,
                    "attempt_count": len(rows),
                    "provider_response_count": provider_responses,
                    "trace_acknowledged_count": trace_acknowledged,
                    "protocol_valid_count": protocol_valid,
                    "end_to_end_conformant_count": end_to_end,
                    "failure_count": len(rows) - end_to_end,
                    "median_latency_ms": statistics.median(latencies),
                    "p95_latency_ms": _nearest_rank(latencies, 95),
                    "mean_latency_ms": statistics.fmean(latencies),
                    "max_latency_ms": max(latencies),
                    "provider_invocations": sum(
                        _integer(row.get("provider_invocations"), label="provider invocations")
                        for row in rows
                    ),
                    "transport_attempts": sum(
                        _integer(row.get("transport_attempts"), label="transport attempts")
                        for row in rows
                    ),
                    "input_tokens": sum(
                        _integer(row.get("input_tokens"), label="input tokens") for row in rows
                    ),
                    "output_tokens": sum(
                        _integer(row.get("output_tokens"), label="output tokens") for row in rows
                    ),
                    "cost_usd": str(cost),
                }
            )
    return result


def _representative_counterfactual(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Project one held-out pair that makes the grounding failure concrete.

    The pair is selected by behavior rather than by prose: the original answer
    is correct, one target value crosses the declared rule, the model returns
    the same plan, and the changed answer is therefore wrong. Both calls must
    still have valid response contracts and acknowledged Weave traces.
    """
    grouped: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in attempts:
        if (
            row.get("stage") != "grounding"
            or row.get("split") != "validation"
            or row.get("model_id") != "Qwen/Qwen3-30B-A3B-Instruct-2507"
            or row.get("ablation_condition")
            not in {"original_target", "target_threshold_crossing"}
        ):
            continue
        case_id = row.get("case_id")
        presentation = row.get("presentation_index")
        if not isinstance(case_id, str) or not isinstance(presentation, int):
            continue
        grouped[(str(row["model_id"]), case_id, presentation)][
            str(row["ablation_condition"])
        ] = row

    candidates: list[dict[str, Any]] = []
    for (model_id, case_id, presentation), pair in grouped.items():
        if set(pair) != {"original_target", "target_threshold_crossing"}:
            continue
        original = pair["original_target"]
        changed = pair["target_threshold_crossing"]
        original_details = original.get("details")
        changed_details = changed.get("details")
        if not isinstance(original_details, dict) or not isinstance(changed_details, dict):
            continue
        original_receipt = original_details.get("experimental_receipt")
        changed_receipt = changed_details.get("experimental_receipt")
        if not isinstance(original_receipt, dict) or not isinstance(changed_receipt, dict):
            continue
        original_intervention = original_receipt.get("intervention")
        changed_intervention = changed_receipt.get("intervention")
        if not isinstance(original_intervention, dict) or not isinstance(
            changed_intervention, dict
        ):
            continue
        original_target = original_intervention.get("current_target_record")
        changed_target = changed_intervention.get("current_target_record")
        if not isinstance(original_target, dict) or not isinstance(changed_target, dict):
            continue
        threshold = _number(original_receipt.get("threshold"), label="example threshold")
        changed_threshold = _number(
            changed_receipt.get("threshold"), label="changed example threshold"
        )
        original_value = _number(original_target.get("value"), label="example original value")
        changed_value = _number(changed_target.get("value"), label="example changed value")
        operator = original_receipt.get("operator")
        if operator not in {"<=", ">="} or changed_receipt.get("operator") != operator:
            continue
        original_meets = original_value <= threshold if operator == "<=" else original_value >= threshold
        changed_meets = changed_value <= threshold if operator == "<=" else changed_value >= threshold
        if (
            threshold != changed_threshold
            or original_meets == changed_meets
            or original_details.get("decision_fidelity") is not True
            or changed_details.get("decision_fidelity") is not False
            or original_details.get("selected_plan_id")
            != changed_details.get("selected_plan_id")
            or original.get("protocol_valid") is not True
            or changed.get("protocol_valid") is not True
            or original.get("trace_acknowledged") is not True
            or changed.get("trace_acknowledged") is not True
            or original_details.get("answer_released") is not False
            or changed_details.get("answer_released") is not False
        ):
            continue
        original_trace = (original_details.get("weave_turn_receipt") or {}).get("url")
        changed_trace = (changed_details.get("weave_turn_receipt") or {}).get("url")
        candidates.append(
            {
                "model_id": model_id,
                "case_id": case_id,
                "project_family": original.get("project_family"),
                "presentation_index": presentation,
                "metric": original_receipt.get("decision_target", {}).get("metric"),
                "object_id": original_receipt.get("decision_target", {}).get("object_id"),
                "units": original_receipt.get("rule_units"),
                "operator": operator,
                "threshold": threshold,
                "original_value": original_value,
                "changed_value": changed_value,
                "original_rule_met": original_meets,
                "changed_rule_met": changed_meets,
                "same_plan_returned": True,
                "original_trace_url": _cloud_url(
                    original_trace, label="counterfactual original Weave trace"
                ),
                "changed_trace_url": _cloud_url(
                    changed_trace, label="counterfactual changed Weave trace"
                ),
                "answer_released": False,
            }
        )
    if not candidates:
        raise R4CPackageError("R4c lacks a trace-bound held-out counterfactual failure")
    return min(candidates, key=lambda row: (row["case_id"], row["presentation_index"]))


def _usage_total(usage: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = sum(row["attempt_count"] for row in usage)
    weighted_latency = sum(row["mean_latency_ms"] * row["attempt_count"] for row in usage)
    return {
        "attempt_count": attempts,
        "provider_response_count": sum(row["provider_response_count"] for row in usage),
        "trace_acknowledged_count": sum(row["trace_acknowledged_count"] for row in usage),
        "provider_invocations": sum(row["provider_invocations"] for row in usage),
        "transport_attempts": sum(row["transport_attempts"] for row in usage),
        "input_tokens": sum(row["input_tokens"] for row in usage),
        "output_tokens": sum(row["output_tokens"] for row in usage),
        "cost_usd": str(sum((Decimal(row["cost_usd"]) for row in usage), Decimal(0))),
        "mean_latency_ms": weighted_latency / attempts,
        "max_latency_ms": max(row["max_latency_ms"] for row in usage),
    }


def _table_rows(
    source: dict[str, dict[str, Any]], incidents: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mechanism_source = _rows(
        source["publication/grounding-mechanisms.json"], label="grounding mechanisms"
    )
    prose_source = _rows(source["publication/prose-cap-primary.json"], label="prose primary")
    mcnemar_source = _rows(source["publication/prose-cap-mcnemar.json"], label="prose McNemar")
    attempts = _rows(source["publication/attempts.json"], label="attempts")
    mechanisms = _mechanism_summaries(mechanism_source)
    prose = _aggregate_prose(prose_source, mcnemar_source)
    usage = _aggregate_usage(attempts)
    rows: list[dict[str, Any]] = []

    for row in mechanism_source:
        denominator = int(row["unique_case_count"])
        for metric, field in (
            ("threshold_sensitivity", "threshold_sensitivity_case_success"),
            ("distractor_specificity", "distractor_specificity_case_success"),
            ("withheld_target_abstention", "missing_evidence_abstention_case_success"),
            ("complete_mechanism", "complete_mechanism_case_success"),
        ):
            success = int(row[field])
            rows.append(
                {
                    "section": "grounding_mechanism",
                    "model_id": row["model_id"],
                    "split": row["split"],
                    "metric": metric,
                    "success_count": success,
                    "denominator": denominator,
                    "rate": success / denominator,
                    "note": "A scenario passes only when both opaque-ID/order presentations pass.",
                }
            )
    for row in prose_source:
        rows.append(
            {
                "section": "prose_cap_primary",
                "model_id": row["model_id"],
                "split": row["split"],
                "condition": row["condition"],
                "output_token_cap": row["max_output_tokens"],
                "metric": "strict_full_prose_protocol",
                "success_count": row["protocol_valid_case_success"],
                "denominator": row["unique_case_count"],
                "rate": row["protocol_valid_case_rate"],
                "note": "Each case-condition passes only if both presentations satisfy the strict JSON/prose contract; usefulness is not measured.",
            }
        )
    for row in mcnemar_source:
        rows.append(
            {
                "section": "prose_cap_paired_protocol",
                "model_id": row["model_id"],
                "split": row["split"],
                "condition": row["condition"],
                "output_token_cap": "128_vs_256",
                "metric": "paired_exact_mcnemar",
                "denominator": row["paired_unique_case_count"],
                "paired_128_only": row["success_128_only"],
                "paired_256_only": row["success_256_only"],
                "paired_mcnemar_p": row["exact_two_sided_mcnemar_p"],
                "note": "Paired protocol-conformance comparison; not an explanation-quality or usefulness test.",
            }
        )
    for row in usage:
        rows.append(
            {
                "section": "model_resource_ledger",
                "model_id": row["model_id"],
                "condition": row["stage"],
                "metric": "observed_attempt_ledger",
                **{key: value for key, value in row.items() if key not in {"model_id", "stage"}},
                "note": "Attempt-level diagnostics are repeated measurements, not inferential units. Latency is client-observed; tokens are provider-reported; cost is projected from the pinned price snapshot.",
            }
        )
    d1 = source["preregistration.json"]["d1_positive_control"]
    rows.append(
        {
            "section": "deterministic_control",
            "model_id": "D1",
            "metric": "deterministic_positive_control",
            "success_count": d1["passed_count"],
            "denominator": d1["cell_count"],
            "rate": d1["passed_count"] / d1["cell_count"],
            "scientific_denominator": 0,
            "note": "Deterministic renderer control; not model performance.",
        }
    )
    r4 = incidents["protocol-amendment-r4b.json"]["r4_live_incident"]
    rows.append(
        {
            "section": "protocol_incident",
            "model_id": "R4 · both models",
            "metric": "provider_schema_rejection",
            "attempt_count": r4["attempt_count"],
            "provider_response_count": r4["provider_response_count"],
            "failure_count": r4["failed_closed_count"],
            "scientific_denominator": r4["scientific_denominator"],
            "note": "The unsupported uniqueItems keyword caused pre-response HTTP 400 failures; no answer was released.",
        }
    )
    r4b = incidents["r4b-incident-snapshot.json"]["incident_summary"]
    rows.append(
        {
            "section": "protocol_incident",
            "model_id": "R4b · both models",
            "metric": "post_response_resource_guard",
            "attempt_count": r4b["provider_invocations"],
            "provider_response_count": r4b["provider_invocations"],
            "trace_acknowledged_count": r4b["accepted_traced_responses"],
            "failure_count": r4b["post_response_input_cap_rejections"],
            "scientific_denominator": 0,
            "note": "Thirty-four completed responses crossed the local 2,048-token cap and were persisted without response/trace evidence; the primary study was stopped and no cell was reused by R4c.",
        }
    )
    rows.append(
        {
            "section": "protocol_incident",
            "model_id": "R4c · publication",
            "metric": "wandb_table_projection_failure",
            "attempt_count": 672,
            "provider_response_count": 672,
            "trace_acknowledged_count": 672,
            "failure_count": 1,
            "scientific_denominator": 672,
            "note": (
                "The original W&B Table projection failed after analysis because nested "
                "error-code mappings had different keys. A hash-bound publication-only "
                "recovery uploaded the unchanged seven-file package with zero provider calls."
            ),
        }
    )
    normalized = [{column: row.get(column) for column in TABLE_COLUMNS} for row in rows]
    return normalized, mechanisms, prose, usage


def _font(repo_root: Path, name: str, size: int) -> ImageFont.FreeTypeFont:
    path = repo_root / "media" / "gi-scenario-film" / "assets" / "fonts" / name
    if not path.is_file():
        raise R4CPackageError(f"Required font is missing: {name}")
    return ImageFont.truetype(str(path), size)


def _short_model(model: str) -> str:
    if "granite" in model.lower():
        return "Granite 4.1 8B"
    if "qwen" in model.lower():
        return "Qwen3 30B A3B"
    return model.rsplit("/", 1)[-1]


def _draw_bar(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    rate: float,
    color: str,
) -> None:
    draw.rounded_rectangle((x, y, x + width, y + 16), radius=8, fill=BLUE_WASH)
    if rate > 0:
        draw.rounded_rectangle((x, y, x + round(width * rate), y + 16), radius=8, fill=color)


def _draw_text_within(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
    max_x: int,
    max_y: int,
    spacing: int = 4,
) -> None:
    """Draw report copy only after proving it fits its authored region."""
    bounds = draw.multiline_textbbox(xy, text, font=font, spacing=spacing)
    if bounds[2] > max_x or bounds[3] > max_y:
        raise R4CPackageError(
            f"Report text exceeds its authored region: {text!r} -> {bounds}, "
            f"limit=({max_x}, {max_y})"
        )
    draw.multiline_text(xy, text, font=font, fill=fill, spacing=spacing)


def _render_media(
    repo_root: Path,
    output: Path,
    mechanisms: list[dict[str, Any]],
    prose: list[dict[str, Any]],
    usage: list[dict[str, Any]],
    d1: dict[str, Any],
    incidents: dict[str, dict[str, Any]],
) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    heading = _font(repo_root, "IBMPlexSansCondensed-SemiBold.ttf", 52)
    section = _font(repo_root, "IBMPlexSansCondensed-SemiBold.ttf", 29)
    body = _font(repo_root, "AtkinsonHyperlegible-Regular.ttf", 21)
    bold = _font(repo_root, "AtkinsonHyperlegible-Bold.ttf", 21)
    small = _font(repo_root, "AtkinsonHyperlegible-Regular.ttf", 18)
    mono = _font(repo_root, "IBMPlexMono-Medium.ttf", 19)

    draw.text((96, 45), "SYNTHETIC POST-RETRIEVAL EVALUATION · R4c", font=bold, fill=TEAL)
    draw.text((96, 82), "Every trace arrived. Did the answer follow the evidence?", font=heading, fill=INK)
    draw.text(
        (96, 150),
        "Change the deciding fact. Change only an unrelated fact. Then remove the evidence.",
        font=body,
        fill=INK_SOFT,
    )
    draw.rounded_rectangle((1466, 44, 1824, 171), radius=18, fill=TEAL_WASH, outline=TEAL, width=2)
    draw.text((1490, 66), "D1 deterministic control", font=bold, fill=INK)
    draw.text((1490, 108), f"{d1['passed_count']}/{d1['cell_count']} passed", font=mono, fill=TEAL)
    draw.text((1490, 139), "control · not model score", font=small, fill=INK_SOFT)

    left = (96, 210, 1172, 705)
    right = (1200, 210, 1824, 705)
    draw.rounded_rectangle(left, radius=20, fill=WHITE, outline=LINE, width=2)
    draw.rounded_rectangle(right, radius=20, fill=WHITE, outline=LINE, width=2)
    draw.text((126, 238), "Three grounding checks", font=section, fill=INK)
    draw.text(
        (126, 276),
        "Each case is shown twice with evidence IDs and order shuffled; both must pass",
        font=small,
        fill=INK_SOFT,
    )
    metric_labels = (
        ("Target crosses rule", "threshold_sensitivity"),
        ("Only unrelated fact", "distractor_specificity"),
        ("Evidence withheld", "withheld_target_abstention"),
        ("All three checks", "complete_mechanism"),
    )
    split_x = {"development": 345, "validation": 745}
    for split, x in split_x.items():
        label = "HELD-OUT VALIDATION" if split == "validation" else "DEVELOPMENT"
        draw.text((x, 318), label, font=bold, fill=INK_SOFT)
    draw.rounded_rectangle((1000, 247, 1012, 259), radius=2, fill=TEAL)
    draw.text((1019, 240), "Granite", font=small, fill=INK_SOFT)
    draw.rounded_rectangle((1000, 271, 1012, 283), radius=2, fill=BLUE)
    draw.text((1019, 264), "Qwen", font=small, fill=INK_SOFT)
    for metric_index, (label, key) in enumerate(metric_labels):
        y = 367 + metric_index * 78
        draw.text((126, y - 4), label, font=bold, fill=INK)
        for split, x in split_x.items():
            split_rows = [row for row in mechanisms if row["split"] == split]
            for model_index, row in enumerate(split_rows):
                yy = y + model_index * 29
                value = row[key]
                color = TEAL if model_index == 0 else BLUE
                _draw_bar(draw, x=x, y=yy + 3, width=255, rate=float(value["rate"]), color=color)
                draw.text(
                    (x + 270, yy - 3),
                    f"{value['success']}/{row['denominator']}",
                    font=mono,
                    fill=INK,
                )

    draw.text((1230, 238), "Fixed response form", font=section, fill=INK)
    draw.text((1230, 276), "36 case-condition cells per model", font=small, fill=INK_SOFT)
    for index, row in enumerate(prose):
        y = 326 + index * 142
        draw.text((1230, y), _short_model(row["model_id"]), font=bold, fill=INK)
        for cap_index, cap in enumerate(("128", "256")):
            cap_row = row["caps"][cap]
            yy = y + 37 + cap_index * 38
            draw.text((1230, yy - 3), f"{cap} tokens", font=small, fill=INK_SOFT)
            _draw_bar(
                draw,
                x=1340,
                y=yy,
                width=330,
                rate=float(cap_row["rate"]),
                color=OCHRE if cap == "128" else TEAL,
            )
            draw.text(
                (1685, yy - 6),
                f"{cap_row['success']}/{cap_row['denominator']}",
                font=mono,
                fill=INK,
            )
    draw.text(
        (1230, 626),
        "256 tokens fixed form completion here.\nIt did not prove clearer or more useful prose.",
        font=small,
        fill=INK_SOFT,
        spacing=7,
    )

    total = _usage_total(usage)
    draw.rounded_rectangle((96, 734, 1824, 865), radius=20, fill=TEAL_WASH, outline=TEAL, width=2)
    draw.text((126, 758), "WHAT R4c COMPLETED", font=bold, fill=TEAL)
    draw.text(
        (126, 798),
        f"{total['attempt_count']} calls · {total['provider_response_count']} responses · "
        f"{total['trace_acknowledged_count']} trace acknowledgements · no runtime retries",
        font=section,
        fill=INK,
    )
    draw.text(
        (1115, 758),
        f"{total['input_tokens']:,} input · {total['output_tokens']:,} output tokens\n"
        f"${Decimal(total['cost_usd']):.6f} projected · {total['mean_latency_ms']/1000:.3f}s mean · "
        f"{total['max_latency_ms']/1000:.3f}s max",
        font=small,
        fill=INK,
        spacing=7,
    )

    r4 = incidents["protocol-amendment-r4b.json"]["r4_live_incident"]
    r4b = incidents["r4b-incident-snapshot.json"]["incident_summary"]
    draw.rounded_rectangle((96, 888, 1824, 1006), radius=20, fill=RED_WASH, outline=RED, width=2)
    draw.text((126, 910), "FAILED PROTOCOLS STAY VISIBLE", font=bold, fill=RED)
    _draw_text_within(
        draw,
        (126, 946),
        f"R4 · {r4['failed_closed_count']}/{r4['attempt_count']} pre-response schema failures\n"
        "scientific denominator 0",
        font=small,
        fill=INK,
        max_x=640,
        max_y=996,
        spacing=3,
    )
    _draw_text_within(
        draw,
        (685, 946),
        f"R4b · {r4b['post_response_input_cap_rejections']}/{r4b['provider_invocations']} completed responses mishandled\n"
        "by local cap · no cells reused",
        font=small,
        fill=INK,
        max_x=1210,
        max_y=996,
        spacing=3,
    )
    _draw_text_within(
        draw,
        (1235, 946),
        "R4c publish · W&B Table type error\nrecovery made 0 model calls",
        font=small,
        fill=INK,
        max_x=1794,
        max_y=996,
        spacing=3,
    )
    _draw_text_within(
        draw,
        (96, 1023),
        "Production disabled · synthetic post-retrieval evidence only · retrieval, safety, "
        "hydraulics, ecology, field value, and practitioner usefulness: not measured",
        font=_font(repo_root, "AtkinsonHyperlegible-Regular.ttf", 17),
        fill=INK_SOFT,
        max_x=1824,
        max_y=1065,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".png", delete=False) as handle:
        temporary = Path(handle.name)
    image.save(temporary, format="PNG", compress_level=9, optimize=False)
    temporary.replace(output)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".json", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, suffix=".csv", delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=TABLE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def build_package(
    repo_root: Path,
    source_root: Path,
    r4b_amendment_path: Path,
    r4c_amendment_path: Path,
    r4b_snapshot_path: Path,
    publication_amendment_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    source, incidents, source_hashes = _validate_source(
        source_root,
        r4b_amendment_path,
        r4c_amendment_path,
        r4b_snapshot_path,
        publication_amendment_path,
    )
    table_rows, mechanisms, prose, usage = _table_rows(source, incidents)
    d1 = source["preregistration.json"]["d1_positive_control"]
    analysis = source["publication/analysis.json"]
    recovery = source[
        "publication-recovery/publication-recovery-cloud-receipt.json"
    ]
    representative_counterfactual = _representative_counterfactual(
        _rows(source["publication/attempts.json"], label="attempts")
    )
    cloud = recovery["publication_cloud_receipt"]
    total = _usage_total(usage)
    table_path = output_root / "article-table.json"
    csv_path = output_root / "article-table.csv"
    media_path = output_root / "article-media.png"
    summary_path = output_root / "article-summary.json"
    manifest_path = output_root / "package-manifest.json"

    table_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": analysis["study_instance_sha256"],
        "statistical_unit": analysis["statistical_unit"],
        "columns": list(TABLE_COLUMNS),
        "rows": table_rows,
    }
    table_payload = {**table_body, "table_sha256": _json_sha256(table_body)}
    _write_json(table_path, table_payload)
    _write_csv(csv_path, table_rows)
    _render_media(repo_root, media_path, mechanisms, prose, usage, d1, incidents)

    r4 = incidents["protocol-amendment-r4b.json"]["r4_live_incident"]
    r4b = incidents["r4b-incident-snapshot.json"]["incident_summary"]
    summary_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": analysis["study_instance_sha256"],
        "state": "R4C_SYNTHETIC_POST_RETRIEVAL_EVALUATION_COMPLETE",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "production_release_eligible": False,
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_validity_status": "not_measured",
        "ecological_value_status": "not_measured",
        "retrieval_quality_status": "not_measured",
        "safety_and_injection_status": "not_measured",
        "evaluation_scope": "synthetic_post_retrieval_automated_conformance",
        "research_question": (
            "When relevant synthetic evidence crosses a rule boundary, does the selected "
            "explanation change; when only a distractor changes, does it stay fixed; and when "
            "the target is withheld, does it abstain?"
        ),
        "headline": (
            "Every call completed and every trace was recorded. The explanations still "
            "often ignored the deciding evidence."
        ),
        "analysis_contract": {
            "audience": "Engineers and technical readers evaluating an evidence explanation layer.",
            "fixed": [
                "Repository-rendered facts, limitations, and actions",
                "Temperature 0, seed 17, one transport attempt, and three time blocks",
                "Two shuffled opaque-ID/order presentations per scenario",
            ],
            "varied": [
                "Relevant threshold crossing",
                "Distractor-only perturbation",
                "Target withholding",
                "Model identity",
                "128 versus 256 output-token cap for the strict prose contract",
            ],
            "measured": [
                "Threshold sensitivity",
                "Distractor specificity",
                "Withheld-target abstention",
                "Strict response-contract completion",
                "Trace acknowledgement, latency, provider-reported tokens, and projected cost",
            ],
            "primary_unit": "Unique scenario; both presentations must pass.",
            "interpretation": (
                "The held-out synthetic families exposed weak threshold sensitivity and low "
                "complete-mechanism pass rates. A larger token cap repaired strict contract "
                "completion in this corpus, not explanation usefulness."
            ),
            "limitations": [
                "Post-retrieval synthetic evidence only",
                "No retrieval, safety, prompt-injection, hydraulic, ecological, field, or practitioner-usefulness evaluation",
                "Fixed corpus performance is not generalized model accuracy",
            ],
        },
        "claim_boundary": analysis["claim_boundary"],
        "statistical_unit": analysis["statistical_unit"],
        "d1_deterministic_control": d1,
        "representative_counterfactual": representative_counterfactual,
        "mechanism_summaries": mechanisms,
        "prose_cap_summaries": prose,
        "model_resource_summaries": usage,
        "total_resource_summary": total,
        "failed_r4_protocol_incident": r4,
        "failed_r4b_protocol_incident": r4b,
        "failed_r4c_publication_incident": incidents["publication-amendment-r4c.json"][
            "failure"
        ],
        "publication_recovery": {
            "recovery_version": recovery["recovery_version"],
            "recovery_identity_sha256": recovery["recovery_identity_sha256"],
            "original_package_sha256": recovery["original_package_sha256"],
            "provider_calls": recovery["provider_calls"],
            "attempts_validated": recovery["attempts_validated"],
            "provider_responses_validated": recovery[
                "provider_responses_validated"
            ],
            "trace_acknowledgements_validated": recovery[
                "trace_acknowledgements_validated"
            ],
        },
        "attempt_count": analysis["attempt_count"],
        "stage_attempt_counts": analysis["stage_attempt_counts"],
        "source_publication": {
            "entity": cloud["entity"],
            "project": cloud["project"],
            "run_id": cloud["run_id"],
            "run_url": cloud["run_url"],
            "artifact_reference": cloud["artifact_reference"],
            "weave_uri": cloud.get("weave_uri"),
            "source_sha256": cloud["source_sha256"],
            "readback_sha256": cloud["readback_sha256"],
            "recovery_identity_sha256": recovery["recovery_identity_sha256"],
            "recovery_receipt_sha256": recovery["receipt_sha256"],
        },
        "source_file_sha256": source_hashes,
        "article_table_sha256": _sha256(table_path),
        "article_table_csv_sha256": _sha256(csv_path),
        "article_media_sha256": _sha256(media_path),
        "media": {
            "width": WIDTH,
            "height": HEIGHT,
            "title": "R4c evidence-grounding mechanisms, prose contract, resources, and protocol incidents",
            "alt_text": (
                "Analytical chart of Granite and Qwen on 24 synthetic post-retrieval scenarios. "
                "Validation complete-mechanism passes are zero of twelve for Granite and three "
                "of twelve for Qwen. Validation threshold, distractor, and withheld-target "
                "passes are one, six, and ten of twelve for Granite and five, seven, and ten "
                "of twelve for Qwen. Under the strict full-prose contract, 128 tokens pass one "
                "of 36 Granite and eleven of 36 Qwen case-condition cells; 256 tokens pass 36 "
                "of 36 for both. All 672 calls returned and were trace acknowledged. R4 and "
                "R4b protocol incidents remain visible. Production is disabled."
            ),
        },
    }
    summary = {**summary_body, "summary_sha256": _json_sha256(summary_body)}
    _write_json(summary_path, summary)
    manifest_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": analysis["study_instance_sha256"],
        "source_root_kind": "final_r4c_live_publication",
        "source_file_sha256": source_hashes,
        "outputs": {
            "article_summary": {"path": summary_path.name, "sha256": _sha256(summary_path)},
            "article_table": {"path": table_path.name, "sha256": _sha256(table_path)},
            "article_table_csv": {"path": csv_path.name, "sha256": _sha256(csv_path)},
            "article_media": {
                "path": media_path.name,
                "sha256": _sha256(media_path),
                "width": WIDTH,
                "height": HEIGHT,
            },
        },
        "boundaries": {
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
        },
    }
    manifest = {**manifest_body, "manifest_sha256": _json_sha256(manifest_body)}
    _write_json(manifest_path, manifest)
    return manifest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> None:
    repo_root = _repo_root()
    incident_root = repo_root / "benchmarks" / "evidence_guide" / "r4"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=repo_root / "dist" / "evidence-guide-r4c-grounding-final-r1" / "live",
    )
    parser.add_argument(
        "--r4b-amendment",
        type=Path,
        default=incident_root / "protocol-amendment-r4b.json",
    )
    parser.add_argument(
        "--r4c-amendment",
        type=Path,
        default=incident_root / "protocol-amendment-r4c.json",
    )
    parser.add_argument(
        "--r4b-snapshot",
        type=Path,
        default=incident_root / "r4b-incident-snapshot.json",
    )
    parser.add_argument(
        "--publication-amendment",
        type=Path,
        default=incident_root / "publication-amendment-r4c.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repo_root / "media" / "gi-scenario-film" / "dist" / "evidence-guide-r4c",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build_package(
                repo_root,
                args.source_root,
                args.r4b_amendment,
                args.r4c_amendment,
                args.r4b_snapshot,
                args.publication_amendment,
                args.output_root,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
