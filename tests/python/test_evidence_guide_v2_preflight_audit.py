from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_preflight_audit import (
    CAMPAIGN_VERSION,
    build_preflight_audit,
    verify_preflight_audit,
    write_preflight_audit,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _archive(root: Path, *, weave_collision: bool) -> Path:
    root.mkdir()
    preregistration = {
        "schema_version": "1.0",
        "campaign_version": CAMPAIGN_VERSION,
        "preregistration_sha256": "preregistration-identity",
    }
    preregistration_path = root / "preregistration.json"
    preregistration_path.write_text(
        json.dumps(preregistration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "campaign-plan.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "campaign_version": CAMPAIGN_VERSION,
                "preregistration_sha256": "preregistration-identity",
                "network_calls": "stage_dependent",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    database = sqlite3.connect(root / "campaign.sqlite")
    with database:
        database.executescript(
            """
            CREATE TABLE campaign_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE stage_sessions (
                stage TEXT NOT NULL, time_block TEXT NOT NULL, session_id TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT,
                PRIMARY KEY(stage, time_block)
            );
            CREATE TABLE stage_reservations (
                stage TEXT PRIMARY KEY, cost_usd TEXT NOT NULL,
                nominal_invocations INTEGER NOT NULL, worst_case_invocations INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY, stage TEXT NOT NULL, hidden INTEGER NOT NULL,
                record_json TEXT NOT NULL, completed_at TEXT NOT NULL
            );
            CREATE TABLE receipts (
                name TEXT PRIMARY KEY, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        database.execute(
            "INSERT INTO campaign_meta VALUES (?, ?)",
            ("preregistration", json.dumps(preregistration, sort_keys=True, separators=(",", ":"))),
        )
        if weave_collision:
            database.execute(
                "INSERT INTO stage_sessions VALUES (?, ?, ?, ?, NULL)",
                ("capability", "block-1", "session-id", "2026-08-07T03:11:50+00:00"),
            )
            database.execute(
                "INSERT INTO stage_reservations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "capability",
                    "0.264320",
                    320,
                    640,
                    1_310_720,
                    81_920,
                    "2026-08-07T03:11:50+00:00",
                ),
            )
            for index, latency in enumerate((5667.7, 8847.3), start=1):
                record = {
                    "attempt_id": f"attempt-{index}",
                    "stage": "capability",
                    "hidden": False,
                    "model_id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                    "architecture_id": "A3",
                    "status": "failed_closed",
                    "protocol_valid": True,
                    "safe": False,
                    "repository_fidelity": False,
                    "trace_acknowledged": False,
                    "provider_invocations": 2,
                    "transport_attempts": 2,
                    "input_tokens": 178,
                    "output_tokens": 26,
                    "input_token_envelope": 963,
                    "output_token_envelope": 256,
                    "cost_usd": "0.000027",
                    "latency_ms": latency,
                    "error_codes": ["WEAVE_AGENT_TURN_INCOMPLETE"],
                    "details": {"answer_released": False},
                }
                database.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                    (
                        record["attempt_id"],
                        "capability",
                        0,
                        json.dumps(record, sort_keys=True, separators=(",", ":")),
                        f"2026-08-07T03:12:0{index}+00:00",
                    ),
                )
            prereg_receipt = {
                "source_sha256": _sha256(preregistration_path),
                "readback_sha256": _sha256(preregistration_path),
                "run_url": "https://wandb.ai/example/project/runs/preflight",
                "artifact_reference": "example/project/preregistration:v0",
            }
            visible_receipt = {
                "references": {
                    split: {
                        "sha256": f"{split}-sha256",
                        "wandb_artifact": f"example/project/{split}:v0",
                        "weave_uri": f"weave:///example/project/object/{split}:digest",
                    }
                    for split in ("development", "validation", "judge_calibration")
                }
            }
            database.executemany(
                "INSERT INTO receipts VALUES (?, ?, ?)",
                [
                    (
                        "preregistration_cloud",
                        json.dumps(prereg_receipt, sort_keys=True, separators=(",", ":")),
                        "2026-08-07T03:11:32+00:00",
                    ),
                    (
                        "visible_datasets_cloud",
                        json.dumps(visible_receipt, sort_keys=True, separators=(",", ":")),
                        "2026-08-07T03:11:50+00:00",
                    ),
                ],
            )
    database.close()
    return root


def _reinitialization_archive(root: Path) -> Path:
    archive = _archive(root, weave_collision=True)
    database = sqlite3.connect(archive / "campaign.sqlite")
    metrics = (
        (284, 26, 1425, 256, "0.000037"),
        (336, 26, 1637, 256, "0.000043"),
        (381, 26, 1834, 256, "0.000047"),
        (333, 26, 1633, 256, "0.000042"),
        (333, 26, 1633, 256, "0.000042"),
        (284, 26, 1425, 256, "0.000037"),
    )
    with database:
        database.execute("DELETE FROM attempts")
        for index, (
            input_tokens,
            output_tokens,
            input_envelope,
            output_envelope,
            cost,
        ) in enumerate(metrics, start=1):
            record = {
                "attempt_id": f"completed-probe-{index}",
                "stage": "capability",
                "hidden": False,
                "model_id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                "architecture_id": "A3",
                "status": "completed",
                "protocol_valid": True,
                "safe": True,
                "repository_fidelity": True,
                "trace_acknowledged": True,
                "provider_invocations": 2,
                "transport_attempts": 2,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_token_envelope": input_envelope,
                "output_token_envelope": output_envelope,
                "cost_usd": cost,
                "latency_ms": 4000 + index,
                "error_codes": [],
                "details": {
                    "answer_released": False,
                    "schema_variant": "marker",
                    "tool_count": 1,
                    "tool_name": "evidence.resolve",
                },
            }
            database.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                (
                    record["attempt_id"],
                    "capability",
                    0,
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                    f"2026-08-07T03:29:0{index}+00:00",
                ),
            )
        block_receipt = {
            "cloud": {
                "attempt_count": 6,
                "group_sha256": "group-sha256",
                "hidden": False,
                "mode": "wandb_weave",
                "run_id": "block-run",
                "run_url": "https://wandb.ai/example/project/runs/block-run",
                "weave_evaluation_uri": (
                    "weave:///example/project/object/gi-evidence-guide-v2-component:digest"
                ),
            },
            "completed": 6,
            "group": {
                "architecture_id": "A3",
                "hidden": False,
                "max_output_tokens": 128,
                "model_id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                "prompt_id": "protocol",
                "repeat": 0,
                "split": "development",
                "stage": "capability",
                "time_block": "block-1",
            },
            "reused": 0,
        }
        database.execute(
            "INSERT INTO receipts VALUES (?, ?, ?)",
            (
                "block:group-sha256",
                json.dumps(block_receipt, sort_keys=True, separators=(",", ":")),
                "2026-08-07T03:29:30+00:00",
            ),
        )
    database.close()
    return archive


def test_preflight_receipt_separates_actual_accounting_and_denominators(
    tmp_path: Path,
) -> None:
    first = _archive(tmp_path / "first", weave_collision=False)
    second = _archive(tmp_path / "second", weave_collision=True)

    receipt = build_preflight_audit(
        artifact_readback_root=first,
        weave_collision_root=second,
    )

    verify_preflight_audit(receipt)
    assert receipt["scientific_status"] == "NO_SCIENTIFIC_RESULT"
    assert receipt["capability_denominator_attempts"] == 0
    assert receipt["architecture_denominator_attempts"] == 0
    assert receipt["scientifically_usable_provider_results"] == 0
    assert receipt["invalidated_provider_attempts"] == 2
    assert receipt["usage"] == {
        "provider_invocations": 4,
        "transport_attempts": 4,
        "provider_reported_input_tokens": 356,
        "provider_reported_output_tokens": 52,
        "conservative_accounted_input_tokens": 1926,
        "conservative_accounted_output_tokens": 512,
        "recorded_cost_usd": "0.000054",
    }
    first_incident, second_incident = receipt["incidents"]
    assert first_incident["attempt_count"] == 0
    assert first_incident["cloud_evidence"]["orphan_upload_possible"] is True
    assert second_incident["attempt_count"] == 2
    assert second_incident["protocol_valid_attempts"] == 2
    assert second_incident["trace_acknowledged_attempts"] == 0
    assert second_incident["reservation"] == {
        "stage": "capability",
        "cost_ceiling_usd": "0.264320",
        "logical_provider_call_ceiling": 320,
        "transport_attempt_ceiling": 640,
        "input_token_ceiling": 1_310_720,
        "output_token_ceiling": 81_920,
        "measured_use": False,
    }
    assert all(
        incident["included_in_capability_denominator"] is False
        and incident["included_in_architecture_denominator"] is False
        and incident["answer_released"] is False
        for incident in receipt["incidents"]
    )

    output = tmp_path / "receipt.json"
    write_preflight_audit(output, receipt)
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_preflight_receipt_includes_reinitialization_flush_without_scientific_use(
    tmp_path: Path,
) -> None:
    receipt = build_preflight_audit(
        artifact_readback_root=_archive(tmp_path / "first", weave_collision=False),
        weave_collision_root=_archive(tmp_path / "second", weave_collision=True),
        weave_reinitialization_root=_reinitialization_archive(tmp_path / "third"),
    )

    verify_preflight_audit(receipt)
    assert receipt["invalidated_preflight_count"] == 3
    assert receipt["invalidated_provider_attempts"] == 8
    assert receipt["capability_denominator_attempts"] == 0
    assert receipt["architecture_denominator_attempts"] == 0
    assert receipt["scientifically_usable_provider_results"] == 0
    assert receipt["usage"] == {
        "provider_invocations": 16,
        "transport_attempts": 16,
        "provider_reported_input_tokens": 2307,
        "provider_reported_output_tokens": 208,
        "conservative_accounted_input_tokens": 11513,
        "conservative_accounted_output_tokens": 2048,
        "recorded_cost_usd": "0.000302",
    }
    third = receipt["incidents"][2]
    assert third["preflight_id"] == "weave-reinitialization-flush"
    assert third["failure_code"] == "WEAVE_REINITIALIZATION_FLUSH_STALL"
    assert third["attempt_count"] == 6
    assert third["protocol_valid_attempts"] == 6
    assert third["trace_acknowledged_attempts"] == 6
    assert third["block_receipt_present"] is True
    assert third["session_completed"] is False
    assert third["included_in_capability_denominator"] is False
    assert third["included_in_architecture_denominator"] is False
    assert third["scientifically_usable_provider_results"] == 0
    assert third["usage"] == {
        "provider_invocations": 12,
        "transport_attempts": 12,
        "provider_reported_input_tokens": 1951,
        "provider_reported_output_tokens": 156,
        "conservative_accounted_input_tokens": 9587,
        "conservative_accounted_output_tokens": 1536,
        "recorded_cost_usd": "0.000248",
    }


def test_preflight_receipt_rejects_a_recomputed_denominator_claim(tmp_path: Path) -> None:
    receipt = build_preflight_audit(
        artifact_readback_root=_archive(tmp_path / "first", weave_collision=False),
        weave_collision_root=_archive(tmp_path / "second", weave_collision=True),
    )
    receipt["capability_denominator_attempts"] = 2
    body = dict(receipt)
    body.pop("receipt_sha256")
    receipt["receipt_sha256"] = _canonical_sha256(body)

    with pytest.raises(RuntimeError, match="scientific boundary"):
        verify_preflight_audit(receipt)


def test_preflight_builder_rejects_any_released_answer(tmp_path: Path) -> None:
    first = _archive(tmp_path / "first", weave_collision=False)
    second = _archive(tmp_path / "second", weave_collision=True)
    database = sqlite3.connect(second / "campaign.sqlite")
    row = database.execute(
        "SELECT attempt_id, record_json FROM attempts ORDER BY attempt_id LIMIT 1"
    ).fetchone()
    record = json.loads(row[1])
    record["details"]["answer_released"] = True
    with database:
        database.execute(
            "UPDATE attempts SET record_json = ? WHERE attempt_id = ?",
            (json.dumps(record, sort_keys=True, separators=(",", ":")), row[0]),
        )
    database.close()

    with pytest.raises(RuntimeError, match="answer-release ambiguity"):
        build_preflight_audit(
            artifact_readback_root=first,
            weave_collision_root=second,
        )
