from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest
import wandb
from PIL import Image

FILM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FILM_ROOT.parents[1]
BUILDER = FILM_ROOT / "scripts" / "build_evidence_guide_r4c_report.py"
PUBLISHER = FILM_ROOT / "scripts" / "publish_wandb_report.py"
INCIDENT_ROOT = REPO_ROOT / "benchmarks" / "evidence_guide" / "r4"
R4B_AMENDMENT = INCIDENT_ROOT / "protocol-amendment-r4b.json"
R4C_AMENDMENT = INCIDENT_ROOT / "protocol-amendment-r4c.json"
R4B_SNAPSHOT = INCIDENT_ROOT / "r4b-incident-snapshot.json"
STUDY = "gi-evidence-guide-grounding-r4c-final"
MODELS = (
    "ibm-granite/granite-4.1-8b",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
)


def _module() -> dict:
    return runpy.run_path(str(BUILDER))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _self_hashed(module: dict, body: dict, field: str) -> dict:
    return {**body, field: module["_json_sha256"](body)}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mechanisms() -> list[dict]:
    values = {
        (MODELS[0], "development"): (6, 8, 7, 4),
        (MODELS[1], "development"): (9, 10, 8, 6),
        (MODELS[0], "validation"): (1, 6, 10, 0),
        (MODELS[1], "validation"): (5, 7, 10, 3),
    }
    rows = []
    for split in ("development", "validation"):
        for model in MODELS:
            threshold, distractor, withheld, complete = values[(model, split)]
            rows.append(
                {
                    "model_id": model,
                    "split": split,
                    "project_families": [f"{split}_a", f"{split}_b"],
                    "unique_case_count": 12,
                    "threshold_sensitivity_case_success": threshold,
                    "distractor_specificity_case_success": distractor,
                    "missing_evidence_abstention_case_success": withheld,
                    "complete_mechanism_case_success": complete,
                    "complete_mechanism_case_rate": complete / 12,
                }
            )
    return rows


def _prose() -> tuple[list[dict], list[dict]]:
    primary = []
    paired = []
    success_128 = {
        (MODELS[0], "development", "target_withheld"): 0,
        (MODELS[0], "validation", "target_withheld"): 1,
        (MODELS[1], "development", "target_withheld"): 5,
        (MODELS[1], "validation", "target_withheld"): 6,
    }
    for split in ("development", "validation"):
        for condition in ("original_target", "target_threshold_crossing", "target_withheld"):
            for model in MODELS:
                short = success_128.get((model, split, condition), 0)
                for cap, success in ((128, short), (256, 6)):
                    primary.append(
                        {
                            "model_id": model,
                            "split": split,
                            "condition": condition,
                            "max_output_tokens": cap,
                            "project_families": [f"{split}_a", f"{split}_b"],
                            "presentation_count_per_case": 2,
                            "unique_case_count": 6,
                            "protocol_valid_case_success": success,
                            "protocol_valid_case_rate": success / 6,
                        }
                    )
                paired.append(
                    {
                        "model_id": model,
                        "split": split,
                        "condition": condition,
                        "project_families": [f"{split}_a", f"{split}_b"],
                        "paired_unique_case_count": 6,
                        "success_128_only": 0,
                        "success_256_only": 6 - short,
                        "discordant_pairs": 6 - short,
                        "exact_two_sided_mcnemar_p": 0.03125 if short == 0 else 1.0,
                    }
                )
    return primary, paired


def _attempts() -> list[dict]:
    rows = []
    index = 0
    for model in MODELS:
        for stage, count in (("grounding", 192), ("prose_diagnosis", 144)):
            for offset in range(count):
                protocol_valid = stage == "grounding" or offset % 3 != 0
                end_to_end = protocol_valid and offset % 4 != 0
                rows.append(
                    {
                        "attempt_id": f"fixture-{index:04d}",
                        "study_version": STUDY,
                        "model_id": model,
                        "stage": stage,
                        "status": "completed",
                        "error_codes": [] if end_to_end else ["fixture_mismatch"],
                        "latency_ms": 1000.0 + offset,
                        "provider_invocations": 1,
                        "transport_attempts": 1,
                        "input_tokens": 1600,
                        "output_tokens": 40 if stage == "grounding" else 128,
                        "cost_usd": "0.000100",
                        "protocol_valid": protocol_valid,
                        "trace_acknowledged": True,
                        "details": {
                            "provider_response_received": True,
                            "end_to_end_conformance": end_to_end,
                        },
                    }
                )
                index += 1
    qwen_grounding = [
        row
        for row in rows
        if row["model_id"] == MODELS[1] and row["stage"] == "grounding"
    ][:2]
    for row, condition, value, fidelity in zip(
        qwen_grounding,
        ("original_target", "target_threshold_crossing"),
        (0.79, 0.91),
        (True, False),
        strict=True,
    ):
        row.update(
            {
                "ablation_condition": condition,
                "case_id": "fixture-counterfactual",
                "presentation_index": 1,
                "project_family": "fixture_validation",
                "split": "validation",
                "protocol_valid": True,
                "trace_acknowledged": True,
            }
        )
        row["details"].update(
            {
                "answer_released": False,
                "decision_fidelity": fidelity,
                "end_to_end_conformance": fidelity,
                "selected_plan_id": "fixture_same_plan",
                "experimental_receipt": {
                    "decision_target": {
                        "metric": "conduit_capacity_ratio",
                        "object_id": "C-19",
                    },
                    "intervention": {
                        "current_target_record": {"value": value},
                    },
                    "operator": "<=",
                    "rule_units": "ratio",
                    "threshold": 0.85,
                },
                "weave_turn_receipt": {
                    "url": (
                        "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/"
                        f"weave/agents/conversations/{condition}"
                    )
                },
            }
        )
    assert len(rows) == 672
    return rows


def _fixture_source(tmp_path: Path) -> Path:
    module = _module()
    source = tmp_path / "source"
    instance = "a" * 64
    schedule = "b" * 64
    mechanisms = _mechanisms()
    prose_primary, mcnemar = _prose()
    attempts = _attempts()
    grounding_primary: list[dict] = []
    diagnostics = [
        {
            "model_id": model,
            "stage": stage,
            "attempt_count": sum(
                row["model_id"] == model and row["stage"] == stage for row in attempts
            ),
        }
        for model in MODELS
        for stage in ("grounding", "prose_diagnosis")
    ]
    d1 = {
        "all_passed": True,
        "cell_count": 192,
        "passed_count": 192,
        "gate_id": "fixture-d1",
        "gate_sha256": "e" * 64,
        "rows_sha256": "f" * 64,
    }
    prereg_body = {
        "schema_version": "1.0",
        "study_version": STUDY,
        "study_instance_sha256": instance,
        "schedule_sha256": schedule,
        "d1_positive_control": d1,
        "release_policy": {
            "production_state": "DISABLE_LIVE_GUIDE",
            "release_eligible": False,
            "practitioner_usefulness_status": "not_measured",
        },
    }
    prereg = _self_hashed(module, prereg_body, "preregistration_sha256")
    analysis_body = {
        "schema_version": "1.0",
        "study_version": STUDY,
        "study_instance_sha256": instance,
        "schedule_sha256": schedule,
        "attempt_count": 672,
        "stage_attempt_counts": {"grounding": 384, "prose_diagnosis": 288},
        "grounding_mechanism_results": mechanisms,
        "grounding_primary_unique_case_results": grounding_primary,
        "prose_cap_paired_mcnemar": mcnemar,
        "prose_cap_primary_unique_case_results": prose_primary,
        "attempt_level_diagnostics_not_inferential_units": diagnostics,
        "all_experimental_receipts_valid": True,
        "actual_answers_released": 0,
        "production_release_eligible": False,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
        "d1_positive_control_gate_sha256": d1["gate_sha256"],
        "statistical_unit": (
            "Unique scenario. Both alias/order presentations must pass; presentations are "
            "repeated measurements and are not counted as independent trials."
        ),
        "claim_boundary": (
            "Synthetic post-retrieval automated conformance only; practitioner usefulness, "
            "retrieval, safety, hydraulic validity, ecological value, and field performance "
            "were not measured."
        ),
    }
    analysis = _self_hashed(module, analysis_body, "analysis_sha256")
    execution_body = {
        "schema_version": "1.0",
        "study_version": STUDY,
        "study_instance_sha256": instance,
        "preregistration_sha256": prereg["preregistration_sha256"],
        "attempt_count": 448,
        "completed_all": False,
        "execution_mode": "live",
        "time_blocks_complete": {
            "grounding": ["block-1", "block-2"],
            "prose_diagnosis": ["block-1", "block-2"],
        },
        "production_state": "DISABLE_LIVE_GUIDE",
        "production_release_eligible": False,
        "practitioner_usefulness_status": "not_measured",
        "actual_answers_released": 0,
        "usage": {},
        "publication": {},
    }
    execution = _self_hashed(module, execution_body, "receipt_sha256")
    payloads = {
        "publication/analysis.json": analysis,
        "publication/attempt-diagnostics.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": diagnostics,
        },
        "publication/attempts.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": attempts,
        },
        "publication/grounding-mechanisms.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": mechanisms,
        },
        "publication/grounding-primary.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": grounding_primary,
        },
        "publication/prose-cap-mcnemar.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": mcnemar,
        },
        "publication/prose-cap-primary.json": {
            "schema_version": "1.0",
            "study_version": STUDY,
            "rows": prose_primary,
        },
        "execution-receipt.json": execution,
        "preregistration.json": prereg,
    }
    for relative, payload in payloads.items():
        _write(source / relative, payload)
    publication_names = (
        "analysis.json",
        "attempt-diagnostics.json",
        "attempts.json",
        "grounding-mechanisms.json",
        "grounding-primary.json",
        "prose-cap-mcnemar.json",
        "prose-cap-primary.json",
    )
    original_files = {
        name: _sha(source / "publication" / name) for name in publication_names
    }
    original_package = module["_json_sha256"](
        {"kind": "r4c_original_seven_file_publication", "files": original_files}
    )
    publication_amendment = {
        "schema_version": "1.0",
        "amendment_id": "fixture-r4c-publication-recovery",
        "study_version": STUDY,
        "recovery_version": "gi-evidence-guide-r4c-publication-recovery-v1",
        "destination": {
            "entity": "wandb",
            "project": "gi-scenario-studio-agent-evals",
            "required_access": "Restricted",
        },
        "failure": {
            "operation": "wandb.Table.add_data",
            "reason": "fixture nested mapping type drift",
            "failure_boundary": "after all analysis",
        },
        "original_publication": {
            "package_sha256": original_package,
            "analysis_file_sha256": original_files["analysis.json"],
            "analysis_sha256": analysis["analysis_sha256"],
            "files": original_files,
        },
        "pre_recovery_execution_receipt": {
            "file_sha256": _sha(source / "execution-receipt.json"),
            "receipt_sha256": execution["receipt_sha256"],
            "attempt_count": 448,
            "completed_all": False,
        },
        "recovery_method": {
            "table_projection_version": "canonical-json-string-per-cell-v1",
        },
    }
    publication_amendment_path = tmp_path / "publication-amendment-r4c.json"
    _write(publication_amendment_path, publication_amendment)
    amendment_sha = _sha(publication_amendment_path)
    recovery_identity = module["_json_sha256"](
        {
            "recovery_version": publication_amendment["recovery_version"],
            "study_instance_sha256": instance,
            "preregistration_sha256": prereg["preregistration_sha256"],
            "amendment_sha256": amendment_sha,
            "package_sha256": original_package,
            "table_projection_version": "canonical-json-string-per-cell-v1",
            "destination": publication_amendment["destination"],
        }
    )
    cloud = {
        "schema_version": "1.0",
        "campaign_version": "fixture-r4c",
        "kind": "r4c_publication_recovery",
        "entity": "wandb",
        "project": "gi-scenario-studio-agent-evals",
        "run_id": "c" * 24,
        "run_url": "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/" + "c" * 24,
        "artifact_reference": (
            "wandb/gi-scenario-studio-agent-evals/evidence-guide-r4c-fixture:v0"
        ),
        "artifact_id": "fixture-artifact",
        "source_sha256": recovery_identity,
        "readback_sha256": "d" * 64,
        "dataset_readback_sha256": None,
        "weave_uri": None,
    }
    recovery_body = {
        "schema_version": "1.0",
        "recovery_version": publication_amendment["recovery_version"],
        "mode": "wandb_publication_recovery",
        "network_calls": "publication_only",
        "provider_calls": 0,
        "inference_reexecuted": False,
        "attempts_validated": 672,
        "provider_responses_validated": 672,
        "trace_acknowledgements_validated": 672,
        "block_receipts_validated": 3,
        "actual_answers_released": 0,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
        "analysis_file_sha256": original_files["analysis.json"],
        "analysis_sha256": analysis["analysis_sha256"],
        "original_files": original_files,
        "original_package_sha256": original_package,
        "publication_amendment_sha256": amendment_sha,
        "recovery_identity_sha256": recovery_identity,
        "publication_cloud_receipt": cloud,
    }
    recovery = _self_hashed(module, recovery_body, "receipt_sha256")
    _write(
        source
        / "publication-recovery"
        / "publication-recovery-cloud-receipt.json",
        recovery,
    )
    return source


def _build(module: dict, source: Path, output: Path) -> dict:
    return module["build_package"](
        REPO_ROOT,
        source,
        R4B_AMENDMENT,
        R4C_AMENDMENT,
        R4B_SNAPSHOT,
        source.parent / "publication-amendment-r4c.json",
        output,
    )


def _configure_publisher(module: dict, source: Path, package: Path) -> None:
    namespace = module["_load_evidence_guide_r4c_grounding"].__globals__
    namespace["EVIDENCE_GUIDE_R4C_SOURCE_ROOT"] = source
    namespace["EVIDENCE_GUIDE_R4C_PACKAGE_ROOT"] = package
    namespace["EVIDENCE_GUIDE_R4C_SUMMARY"] = package / "article-summary.json"
    namespace["EVIDENCE_GUIDE_R4C_TABLE"] = package / "article-table.json"
    namespace["EVIDENCE_GUIDE_R4C_TABLE_CSV"] = package / "article-table.csv"
    namespace["EVIDENCE_GUIDE_R4C_MEDIA"] = package / "article-media.png"
    namespace["EVIDENCE_GUIDE_R4C_MANIFEST"] = package / "package-manifest.json"
    namespace["EVIDENCE_GUIDE_R4C_R4B_AMENDMENT"] = R4B_AMENDMENT
    namespace["EVIDENCE_GUIDE_R4C_AMENDMENT"] = R4C_AMENDMENT
    namespace["EVIDENCE_GUIDE_R4C_R4B_SNAPSHOT"] = R4B_SNAPSHOT
    namespace["EVIDENCE_GUIDE_R4C_PUBLICATION_AMENDMENT"] = (
        source.parent / "publication-amendment-r4c.json"
    )


def test_builds_hash_bound_accessible_r4c_package(tmp_path: Path) -> None:
    module = _module()
    source = _fixture_source(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest = _build(module, source, first)
    _build(module, source, second)

    for item in manifest["outputs"].values():
        assert _sha(first / item["path"]) == item["sha256"]
        assert _sha(first / item["path"]) == _sha(second / item["path"])
    with Image.open(first / "article-media.png") as image:
        assert image.size == (1920, 1080)
        assert image.mode == "RGB"
    summary = json.loads((first / "article-summary.json").read_text(encoding="utf-8"))
    assert summary["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert summary["attempt_count"] == 672
    assert summary["d1_deterministic_control"]["passed_count"] == 192
    assert summary["representative_counterfactual"] == {
        "answer_released": False,
        "case_id": "fixture-counterfactual",
        "changed_rule_met": False,
        "changed_trace_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/"
            "weave/agents/conversations/target_threshold_crossing"
        ),
        "changed_value": 0.91,
        "metric": "conduit_capacity_ratio",
        "model_id": MODELS[1],
        "object_id": "C-19",
        "operator": "<=",
        "original_rule_met": True,
        "original_trace_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/"
            "weave/agents/conversations/original_target"
        ),
        "original_value": 0.79,
        "presentation_index": 1,
        "project_family": "fixture_validation",
        "same_plan_returned": True,
        "threshold": 0.85,
        "units": "ratio",
    }
    assert summary["failed_r4_protocol_incident"]["failed_closed_count"] == 224
    assert summary["failed_r4b_protocol_incident"]["post_response_input_cap_rejections"] == 34
    assert summary["practitioner_usefulness_status"] == "not_measured"
    table = json.loads((first / "article-table.json").read_text(encoding="utf-8"))
    assert {
        "grounding_mechanism",
        "prose_cap_primary",
        "prose_cap_paired_protocol",
        "model_resource_ledger",
        "deterministic_control",
        "protocol_incident",
    }.issubset({row["section"] for row in table["rows"]})


def test_rejects_drifted_source_hash_and_cloud_readback(tmp_path: Path) -> None:
    module = _module()
    source = _fixture_source(tmp_path)
    analysis_path = source / "publication" / "analysis.json"
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis["attempt_count"] = 99
    _write(analysis_path, analysis)
    with pytest.raises(module["R4CPackageError"], match="self-hash drifted"):
        _build(module, source, tmp_path / "out-a")

    source = _fixture_source(tmp_path / "fresh")
    recovery_path = (
        source
        / "publication-recovery"
        / "publication-recovery-cloud-receipt.json"
    )
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    recovery["publication_cloud_receipt"]["source_sha256"] = "0" * 64
    recovery_body = {key: value for key, value in recovery.items() if key != "receipt_sha256"}
    recovery["receipt_sha256"] = module["_json_sha256"](recovery_body)
    _write(recovery_path, recovery)
    with pytest.raises(module["R4CPackageError"], match="recovery receipt drifted"):
        _build(module, source, tmp_path / "out-b")


def test_rejects_incomplete_final_source_package(tmp_path: Path) -> None:
    module = _module()
    source = _fixture_source(tmp_path)
    (source / "publication" / "prose-cap-mcnemar.json").unlink()
    with pytest.raises(module["R4CPackageError"], match="source package is incomplete"):
        _build(module, source, tmp_path / "out")


def test_article_publisher_consumes_only_complete_r4c_package(tmp_path: Path) -> None:
    builder = _module()
    source = _fixture_source(tmp_path)
    package = tmp_path / "package"
    _build(builder, source, package)
    publisher = runpy.run_path(str(PUBLISHER))
    _configure_publisher(publisher, source, package)

    projection = publisher["_load_evidence_guide_r4c_grounding"]()
    assert projection["available"] is True
    assert projection["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert projection["practitioner_usefulness_status"] == "not_measured"
    assert projection["failed_r4_protocol_incident"]["scientific_denominator"] == 0
    assert projection["failed_r4b_protocol_incident"]["post_response_input_cap_rejections"] == 34
    assert len(projection["artifact_references"]) == 1
    markdown = publisher["_evidence_guide_r4c_markdown"](projection)
    for text in (
        "post-retrieval synthetic",
        "0/12",
        "3/12",
        "1/36",
        "11/36",
        "36/36",
        "DISABLE_LIVE_GUIDE",
        "practitioner usefulness",
        "R4b",
        "Every trace arrived",
        "same plan",
        "0.79",
        "0.91",
        "A trace is not enough",
    ):
        assert text.lower() in markdown.lower()

    class FakeTable:
        def __init__(self, *, columns, data):
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    table = publisher["_evidence_guide_r4c_table"](
        FakeWandb, {"evidence_guide_r4c_grounding": projection}
    )
    assert table.columns == list(builder["TABLE_COLUMNS"])
    assert len(table.data) == len(projection["rows"])
    cap_index = table.columns.index("output_token_cap")
    assert {
        row[cap_index] for row in table.data if row[cap_index] is not None
    } == {"128", "256", "128_vs_256"}

    # Exercise the pinned SDK's real type inference without starting a run or
    # making a network request. This guards the live-report projection itself,
    # not only our fake table adapter.
    sdk_table = publisher["_evidence_guide_r4c_table"](
        wandb, {"evidence_guide_r4c_grounding": projection}
    )
    assert len(sdk_table.data) == len(projection["rows"])


def test_article_publisher_keeps_partial_or_drifted_r4c_results_out(
    tmp_path: Path,
) -> None:
    publisher = runpy.run_path(str(PUBLISHER))
    source = _fixture_source(tmp_path)
    package = tmp_path / "missing-package"
    _configure_publisher(publisher, source, package)
    unavailable = publisher["_load_evidence_guide_r4c_grounding"]()
    assert unavailable["available"] is False
    assert unavailable["rows"] == []
    markdown = publisher["_evidence_guide_r4c_markdown"](unavailable)
    assert "No R4c result or headline number is displayed yet" in markdown
    assert "R4b" in markdown

    builder = _module()
    _build(builder, source, package)
    (package / "article-media.png").write_bytes(b"drift")
    with pytest.raises(RuntimeError, match="article_media hash drifted"):
        publisher["_load_evidence_guide_r4c_grounding"]()
