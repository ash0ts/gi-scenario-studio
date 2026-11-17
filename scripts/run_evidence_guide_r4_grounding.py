#!/usr/bin/env python3
"""Preregister, rehearse, and execute the clean Evidence Guide R4c study."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from gi_scenario_core.evidence_guide_r4_grounding import (
    MODELS,
    OUTPUT_DIRECTORY_NAME,
    R4C_MAX_INPUT_TOKENS,
    STAGES,
    STUDY_VERSION,
    GroundingExecutor,
    PerfectGroundingFakeProvider,
    analyze,
    build_preregistration,
    load_r4_corpus,
    schedule_records,
    source_hashes,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    DEFAULT_BUDGET_CAPS,
    TARGET_PROJECT,
    TIME_BLOCKS,
    CampaignStore,
    Corpus,
    ModelPrice,
    file_sha256,
    json_sha256,
    load_pricing_snapshot,
    reserve_stage,
    run_attempt_batch,
    write_json_atomic,
)
from gi_scenario_core.evidence_guide_v2_cloud import (
    LocalCampaignPublisher,
    WandbCampaignPublisher,
)
from gi_scenario_core.inference import WandbInferenceAdapter
from gi_scenario_core.observability import CredentialSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "evidence_guide" / "r4" / "corpus.json"
DEFAULT_R4B_PROTOCOL_AMENDMENT = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "protocol-amendment-r4b.json"
)
DEFAULT_R4C_PROTOCOL_AMENDMENT = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "protocol-amendment-r4c.json"
)
DEFAULT_R4B_INCIDENT_SNAPSHOT = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "r4b-incident-snapshot.json"
)
DEFAULT_CORPUS_README = ROOT / "benchmarks" / "evidence_guide" / "r4" / "README.md"
DEFAULT_PRICING = (
    ROOT / "benchmarks" / "evidence_guide" / "v2" / "inference-pricing-2026-08-06.json"
)
DEFAULT_R3_DIAGNOSTIC = (
    ROOT
    / "dist"
    / "evidence-guide-v2-architecture-dev-r3"
    / "live"
    / "causal-stage-publication"
    / "receipt.json"
)
# Every protocol revision receives a new root through OUTPUT_DIRECTORY_NAME so
# R4c cannot inherit R4b preregistration, SQLite, or receipt state.
DEFAULT_OUTPUT = ROOT / "dist" / f"{OUTPUT_DIRECTORY_NAME}-final-r1"
CONFIRM_COST = "300.00"
EXPECTED_ATTEMPTS = 672


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--fake-provider", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--time-block", choices=TIME_BLOCKS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    parser.add_argument(
        "--r4b-incident-snapshot",
        type=Path,
        default=DEFAULT_R4B_INCIDENT_SNAPSHOT,
    )
    parser.add_argument("--r3-diagnostic", type=Path, default=DEFAULT_R3_DIAGNOSTIC)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=TARGET_PROJECT)
    parser.add_argument("--confirm-cost-cap-usd")
    return parser


def _validate_arguments(arguments: argparse.Namespace) -> tuple[str, tuple[str, ...]]:
    """Return the execution mode and exact blocks without touching cloud state."""

    if arguments.execute:
        if (
            arguments.confirm_cost_cap_usd != CONFIRM_COST
            or not arguments.time_block
            or not arguments.entity
            or arguments.project != TARGET_PROJECT
        ):
            raise RuntimeError(
                "live R4c execution requires an explicit time block, exact project, entity, "
                "and --confirm-cost-cap-usd 300.00"
            )
        return "live", (str(arguments.time_block),)
    if arguments.confirm_cost_cap_usd is not None:
        raise RuntimeError("the R4c cost confirmation is accepted only for live execution")
    if arguments.fake_provider:
        if arguments.time_block is not None:
            raise RuntimeError("the zero-network fake rehearsal always runs all three blocks")
        return "fake", tuple(TIME_BLOCKS)
    if arguments.time_block is not None:
        raise RuntimeError("dry-run does not accept a time block")
    return "fake", ()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path.name}")
    return value


def _prior_usage(
    path: Path,
    *,
    amendment_path: Path = DEFAULT_R4C_PROTOCOL_AMENDMENT,
    predecessor_amendment_path: Path = DEFAULT_R4B_PROTOCOL_AMENDMENT,
    pricing: Mapping[str, ModelPrice] | None = None,
) -> dict[str, Any]:
    """Load the sealed, conservative prior for a clean R4c execution.

    ``pricing`` remains accepted for backwards-compatible callers, but no cost
    is recomputed here: the incident builder already projected and sealed both
    provider-reported and conservative-at-risk cost under the pinned snapshot.
    """

    del pricing
    snapshot = _load_object(path)
    amendment = _load_object(amendment_path)
    predecessor = _load_object(predecessor_amendment_path)
    snapshot_body = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    incident_reference = amendment.get("incident_snapshot")
    snapshot_resource = snapshot.get("resource_ledger")
    amendment_resource = amendment.get("resource_ledger")
    if (
        snapshot.get("source_study_version")
        != "gi-evidence-guide-grounding-r4b-final"
        or snapshot.get("snapshot_sha256") != json_sha256(snapshot_body)
        or not isinstance(incident_reference, Mapping)
        or not isinstance(snapshot_resource, Mapping)
        or not isinstance(amendment_resource, Mapping)
        or amendment.get("replacement_study_version") != STUDY_VERSION
        or amendment.get("r4c_protocol_change", {}).get("state_reuse_permitted") is not False
    ):
        raise RuntimeError("R4C_INCIDENT_OR_AMENDMENT_INVALID")
    if (
        incident_reference.get("file_sha256") != file_sha256(path)
        or incident_reference.get("snapshot_sha256") != snapshot["snapshot_sha256"]
        or file_sha256(predecessor_amendment_path)
        != snapshot_resource["conservative_prior_entering_r4b"].get(
            "amendment_source_sha256"
        )
        or predecessor.get("replacement_study_version")
        != "gi-evidence-guide-grounding-r4b-final"
    ):
        raise RuntimeError("R4C_INCIDENT_SOURCE_IDENTITY_MISMATCH")
    prior = snapshot_resource.get("r4c_prior_for_cap_enforcement")
    amendment_prior = amendment_resource.get("r4b_prior_for_cap_enforcement")
    if not isinstance(prior, Mapping) or not isinstance(amendment_prior, Mapping):
        raise TypeError("R4C_PRIOR_RESOURCE_LEDGER_MISSING")
    expected = {
        "provider_invocations": 1951,
        "transport_attempts": 1950,
        "input_token_envelope": 1_715_625,
        "output_token_envelope": 262_815,
        "conservative_cumulative_cost_usd": "0.1524103",
        "verified_observed_cost_usd": "0.1065785",
    }
    if any(prior.get(key) != value for key, value in expected.items()) or any(
        amendment_prior.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("R4C_PRIOR_RESOURCE_LEDGER_DRIFT")
    return {
        "source": "r4c-sealed-r4b-incident-carry-forward",
        "source_sha256": file_sha256(path),
        "amendment_source_sha256": file_sha256(amendment_path),
        "predecessor_amendment_source_sha256": file_sha256(
            predecessor_amendment_path
        ),
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "cost_usd": expected["conservative_cumulative_cost_usd"],
        "cost_basis": (
            "complete conservative prior entering R4b + all 272 R4b completion envelopes"
        ),
        "verified_observed_cost_usd": expected["verified_observed_cost_usd"],
        "verified_observed_cost_basis": (
            "verified prior entering R4b + provider-reported usage for all 272 R4b completions"
        ),
        "provider_invocations": expected["provider_invocations"],
        "transport_attempts": expected["transport_attempts"],
        "input_tokens": expected["input_token_envelope"],
        "output_tokens": expected["output_token_envelope"],
        "token_count_basis": "conservative token envelopes for cap enforcement",
        "components": {
            "conservative_prior_entering_r4b": dict(
                snapshot_resource["conservative_prior_entering_r4b"]
            ),
            "r4b_observed_attempts": dict(snapshot_resource["r4b_observed_attempts"]),
        },
    }


def _diagnostic_reference(path: Path) -> dict[str, str]:
    value = _load_object(path)
    if (
        value.get("release_eligible") is not False
        or value.get("publication_created") is not False
        or value.get("article_publication_permitted") is not False
        or value.get("production_decision") != "DISABLE_LIVE_GUIDE"
        or value.get("analyzer_gate", {}).get("outcome") != "raised"
    ):
        raise RuntimeError("R4_R3_DIAGNOSTIC_RECEIPT_INVALID")
    return {
        "path_role": "local_predecessor_diagnostic",
        "source_sha256": file_sha256(path),
        "receipt_sha256": str(value.get("receipt_sha256") or ""),
    }


def _local_runtime_dependency_closure(seed_paths: Sequence[Path]) -> set[Path]:
    """Resolve the local ``gi_scenario_core`` Python import closure."""

    package_root = ROOT / "python" / "gi_scenario_core"
    pending = [path.resolve() for path in seed_paths if path.suffix == ".py"]
    discovered: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in discovered:
            continue
        discovered.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        candidates: set[Path] = set()
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    if node.module:
                        module = node.module
                    else:
                        for alias in node.names:
                            candidate = package_root / f"{alias.name}.py"
                            if candidate.is_file():
                                candidates.add(candidate.resolve())
                elif node.level == 0 and (node.module or "").startswith("gi_scenario_core."):
                    module = str(node.module).removeprefix("gi_scenario_core.")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("gi_scenario_core."):
                        relative = alias.name.removeprefix("gi_scenario_core.")
                        candidate = package_root.joinpath(*relative.split(".")).with_suffix(".py")
                        if candidate.is_file():
                            candidates.add(candidate.resolve())
            if module:
                candidate = package_root.joinpath(*module.split(".")).with_suffix(".py")
                if candidate.is_file():
                    candidates.add(candidate.resolve())
        pending.extend(sorted(candidates - discovered))
    return discovered


def _source_paths(*, pricing_snapshot: Path = DEFAULT_PRICING) -> dict[str, Path]:
    paths = {
        "experiment_module": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_r4_grounding.py",
        "runner": Path(__file__).resolve(),
        "inference_adapter": ROOT / "python" / "gi_scenario_core" / "inference.py",
        "campaign_store": ROOT / "python" / "gi_scenario_core" / "evidence_guide_v2_campaign.py",
        "cloud_observability": ROOT / "python" / "gi_scenario_core" / "evidence_guide_v2_cloud.py",
        "local_validator": ROOT / "python" / "gi_scenario_core" / "evidence_evaluation_v2.py",
        "architecture_executor": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_architecture_executor.py",
        "paired_trace_runtime": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_paired.py",
        "contracts": ROOT / "python" / "gi_scenario_core" / "contracts.py",
        "r4_corpus_builder": ROOT / "scripts" / "build_evidence_guide_r4_corpus.py",
        "r4_corpus_tests": ROOT / "tests" / "python" / "test_evidence_guide_r4_corpus.py",
        "r4_experiment_tests": ROOT / "tests" / "python" / "test_evidence_guide_r4_grounding.py",
        "inference_adapter_tests": ROOT / "tests" / "python" / "test_wandb_inference.py",
        "python_project": ROOT / "pyproject.toml",
        "dependency_lock": ROOT / "uv.lock",
        # Pricing changes both the preregistered budget envelope and every
        # observed per-attempt cost.  It is therefore part of the scientific
        # runtime, not merely publication metadata.
        "pricing_snapshot": pricing_snapshot.resolve(),
        "r4_corpus": DEFAULT_CORPUS,
        "r4_corpus_readme": DEFAULT_CORPUS_README,
        "r4b_protocol_amendment": DEFAULT_R4B_PROTOCOL_AMENDMENT,
        "r4c_protocol_amendment": DEFAULT_R4C_PROTOCOL_AMENDMENT,
        "r4b_incident_snapshot": DEFAULT_R4B_INCIDENT_SNAPSHOT,
        "r4b_incident_snapshot_builder": ROOT
        / "scripts"
        / "build_evidence_guide_r4b_incident_snapshot.py",
    }
    runtime_closure = _local_runtime_dependency_closure(
        path for path in paths.values() if path.suffix == ".py"
    )
    for path in sorted(runtime_closure):
        if path in {value.resolve() for value in paths.values()}:
            continue
        relative = path.relative_to(ROOT).as_posix().replace("/", "__")
        paths[f"runtime__{relative}"] = path
    required_runtime_files = {
        "evidence_guide_v2_protocol.py",
        "evidence_guide_v2_provider.py",
        "observability.py",
        "repository.py",
    }
    if not required_runtime_files.issubset({path.name for path in paths.values()}):
        raise RuntimeError("R4_LOCAL_RUNTIME_SOURCE_CLOSURE_INCOMPLETE")
    return paths


def _embedded_source_capsule(paths: Mapping[str, Path]) -> dict[str, Any]:
    """Freeze exact runnable sources inside the preregistration artifact.

    The surrounding workspace can contain unrelated uncommitted work.  Git is
    therefore not used as the scientific identity for this run.  Each required
    source is embedded without an absolute path, content-addressed, and later
    uploaded as part of the preregistration before inference starts.
    """

    files: dict[str, dict[str, Any]] = {}
    for logical_name, path in sorted(paths.items()):
        payload = path.read_bytes()
        files[logical_name] = {
            "filename": path.name,
            "sha256": file_sha256(path),
            "utf8_bytes": len(payload),
            "encoding": "base64",
            "content": base64.b64encode(payload).decode("ascii"),
        }
    body = {
        "schema_version": "1.0",
        "identity_basis": "embedded_content_addressed_source_capsule",
        "git_commit_is_scientific_identity": False,
        "absolute_paths_included": False,
        "file_count": len(files),
        "files": files,
    }
    return {**body, "capsule_sha256": json_sha256(body)}


def _write_or_verify(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        existing = _load_object(path)
        if existing != value:
            raise RuntimeError(f"R4_IMMUTABLE_FILE_DRIFT:{path.name}")
        return
    write_json_atomic(path, value)


def _mode_root(base: Path, mode: str) -> Path:
    if mode not in {"fake", "live"}:
        raise ValueError("R4 execution mode is invalid")
    return base.resolve() / mode


def _safe_dataset_row(row: Mapping[str, Any]) -> dict[str, str]:
    """Project case metadata for W&B/Weave without evaluator or condition content."""

    value = {
        "case_id": str(row.get("case_id") or row.get("scenario_id") or ""),
        "project_family": str(row.get("project_family") or ""),
        "metric_category": str(row.get("metric_category") or ""),
        "synthetic_notice": str(row.get("synthetic_notice") or ""),
    }
    if not value["case_id"] or not value["project_family"]:
        raise RuntimeError("R4_CLOUD_DATASET_ROW_INVALID")
    serialized = json.dumps(value, sort_keys=True)
    if any(
        prohibited in serialized
        for prohibited in (
            "evaluator_only",
            "expected_plan",
            "condition_id",
            "original_target",
            "target_threshold_crossing",
            "target_withheld",
            "distractor_only_perturbation",
        )
    ):
        raise RuntimeError("R4_CLOUD_DATASET_GOLD_LEAKAGE")
    return value


def _publisher_corpus(corpus: Corpus, output: Path) -> tuple[Corpus, dict[str, Any]]:
    """Materialize upload-safe projections for the v2 cloud publisher interface.

    The checked-in R4 file contains evaluator-only labels needed by the local
    scorer. It is frozen inside the preregistration source capsule, but is not
    published as an evaluation Dataset and is never sent to inference. The
    Weave datasets contain only non-sensitive case metadata; actual provider
    inputs are logged separately by the attempt observer.
    """

    projection_root = output / "cloud-dataset-projections"
    projections: dict[str, tuple[Mapping[str, Any], ...]] = {}
    source_paths: dict[str, str] = {}
    split_hashes: dict[str, str] = {}
    datasets: dict[str, Mapping[str, Any]] = {}
    split_counts: dict[str, int] = {}
    for split in ("development", "validation"):
        if split not in corpus.splits:
            raise RuntimeError(f"R4_CORPUS_SPLIT_UNAVAILABLE:{split}")
        rows = tuple(_safe_dataset_row(row) for row in corpus.splits[split])
        payload = {
            "schema_version": "1.0",
            "study_version": STUDY_VERSION,
            "split": split,
            "provider_input_source": False,
            "evaluator_content_included": False,
            "rows": list(rows),
        }
        path = projection_root / f"{split}.json"
        _write_or_verify(path, payload)
        projections[split] = rows
        datasets[split] = payload
        source_paths[split] = str(path.resolve())
        split_hashes[split] = file_sha256(path)
        split_counts[split] = len(rows)

    # The generic publisher has a calibration slot. R4 has no judge stage, so
    # publish an explicit metadata-only sentinel instead of duplicating cases.
    calibration_rows: tuple[Mapping[str, Any], ...] = (
        {
            "case_id": "r4-no-judge-calibration",
            "project_family": "not_applicable",
            "metric_category": "metadata_only",
            "synthetic_notice": (
                "R4 performs no LLM-judge calibration; this row exists only to make that "
                "absence explicit in the shared dataset registry."
            ),
        },
    )
    calibration_payload = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "split": "judge_calibration",
        "provider_input_source": False,
        "evaluator_content_included": False,
        "rows": list(calibration_rows),
    }
    calibration_path = projection_root / "judge_calibration.json"
    _write_or_verify(calibration_path, calibration_payload)
    projections["judge_calibration"] = calibration_rows
    datasets["judge_calibration"] = calibration_payload
    source_paths["judge_calibration"] = str(calibration_path.resolve())
    split_hashes["judge_calibration"] = file_sha256(calibration_path)
    split_counts["judge_calibration"] = 1

    manifest_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "source_corpus_manifest_sha256": corpus.manifest_sha256,
        "provider_input_source": False,
        "evaluator_content_included": False,
        "split_sha256": dict(sorted(split_hashes.items())),
        "split_counts": dict(sorted(split_counts.items())),
    }
    manifest = {**manifest_body, "manifest_sha256": json_sha256(manifest_body)}
    _write_or_verify(projection_root / "manifest.json", manifest)
    publisher_view = replace(
        corpus,
        split_sha256=split_hashes,
        splits=projections,
        source_paths=source_paths,
        datasets=datasets,
        split_counts=split_counts,
    )
    return publisher_view, manifest


def _bind_store(
    store: CampaignStore,
    *,
    preregistration: Mapping[str, Any],
    schedule: Sequence[Mapping[str, Any]],
    corpus: Corpus,
    mode: str,
    destination: Mapping[str, str],
    prior_usage: Mapping[str, Any],
) -> None:
    schedule_sha256 = json_sha256(list(schedule))
    context = {
        "study_version": STUDY_VERSION,
        "study_instance_sha256": preregistration["study_instance_sha256"],
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "schedule_sha256": schedule_sha256,
        "attempt_count": len(schedule),
        "corpus_manifest_sha256": corpus.manifest_sha256,
        "execution_mode": mode,
        "destination": dict(destination),
    }
    store.put_meta("r4_execution_context", context, immutable=True)
    store.put_meta("prior_usage", dict(prior_usage), immutable=True)
    existing_attempts = store.attempts()
    allowed = {str(row["attempt_id"]) for row in schedule}
    if len(allowed) != len(schedule):
        raise RuntimeError("R4_SCHEDULE_ATTEMPT_IDS_INVALID")
    if existing_attempts and any(
        str(row["attempt_id"]) not in allowed for row in existing_attempts
    ):
        raise RuntimeError("R4_FOREIGN_ATTEMPT_STATE_REJECTED")


def _dataset_receipt_path(output: Path) -> Path:
    return output / "visible-datasets-receipt.json"


def _prepare_publisher(
    *,
    output: Path,
    preregistration_path: Path,
    preregistration: Mapping[str, Any],
    corpus: Corpus,
    entity: str,
    project: str,
) -> WandbCampaignPublisher:
    credentials = CredentialSession()
    publisher = WandbCampaignPublisher(
        entity=entity,
        project=project,
        workspace=output / "cloud",
        credentials=credentials,
        campaign_instance_sha256=str(preregistration["study_instance_sha256"]),
    )
    prereg_receipt_path = output / "preregistration-cloud-receipt.json"
    if prereg_receipt_path.exists():
        receipt = _load_object(prereg_receipt_path)
        publisher.reuse_preregistration(receipt, path=preregistration_path)
    else:
        receipt = publisher.seal_preregistration(preregistration_path).as_dict()
        _write_or_verify(prereg_receipt_path, receipt)
    dataset_path = _dataset_receipt_path(output)
    if dataset_path.exists():
        dataset_receipt = _load_object(dataset_path)
        references = dataset_receipt.get("references")
        if (
            not isinstance(references, Mapping)
            or dataset_receipt.get("references_sha256") != json_sha256(references)
            or dataset_receipt.get("raw_corpus_in_visible_dataset_artifact") is not False
            or dataset_receipt.get("raw_corpus_in_preregistration_source_capsule") is not True
            or dataset_receipt.get("evaluator_content_in_visible_dataset") is not False
        ):
            raise RuntimeError("R4_VISIBLE_DATASET_RECEIPT_INVALID")
        publisher.reuse_visible_datasets(dataset_receipt, corpus=corpus)
    else:
        references = publisher.publish_visible_datasets(corpus)
        receipt = {
            "schema_version": "1.0",
            "study_version": STUDY_VERSION,
            "references": references,
            "references_sha256": json_sha256(references),
            "raw_corpus_in_visible_dataset_artifact": False,
            "raw_corpus_in_preregistration_source_capsule": True,
            "evaluator_content_in_visible_dataset": False,
            "provider_receives_source_capsule": False,
        }
        _write_or_verify(dataset_path, receipt)
    return publisher


def _execution_receipt(
    *,
    store: CampaignStore,
    preregistration: Mapping[str, Any],
    mode: str,
    completed: bool,
    publication: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": preregistration["study_instance_sha256"],
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "execution_mode": mode,
        "attempt_count": len(store.attempts()),
        "time_blocks_complete": {
            stage: sorted(store.completed_time_blocks(stage)) for stage in STAGES
        },
        "completed_all": completed,
        "usage": store.usage(),
        "actual_answers_released": 0,
        "production_release_eligible": False,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
        "publication": dict(publication or {}),
    }
    return {**body, "receipt_sha256": json_sha256(body)}


def _completed_exactly(
    store: CampaignStore,
    *,
    specs: Sequence[Any],
) -> bool:
    expected = {str(spec.attempt_id) for spec in specs}
    observed = {str(row["attempt_id"]) for row in store.attempts()}
    if observed - expected:
        raise RuntimeError("R4_FOREIGN_ATTEMPT_STATE_REJECTED")
    sessions_complete = all(
        set(store.completed_time_blocks(stage)) == set(TIME_BLOCKS) for stage in STAGES
    )
    receipts_complete = all(store.get_receipt(f"block-{block}") for block in TIME_BLOCKS)
    return bool(observed == expected and sessions_complete and receipts_complete)


def _validate_existing_block(
    store: CampaignStore,
    *,
    block: str,
    selected: Sequence[Any],
    receipt: Mapping[str, Any],
) -> None:
    attempt_ids = [str(spec.attempt_id) for spec in selected]
    if (
        receipt.get("attempt_ids_sha256") != json_sha256(attempt_ids)
        or int(receipt.get("attempt_count") or 0) != len(selected)
        or any(block not in store.completed_time_blocks(stage) for stage in STAGES)
    ):
        raise RuntimeError("R4_BLOCK_RECEIPT_DRIFT")
    observed = {str(row["attempt_id"]) for row in store.attempts()}
    if any(attempt_id not in observed for attempt_id in attempt_ids):
        raise RuntimeError("R4_BLOCK_RECEIPT_WITHOUT_ATTEMPTS")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    mode, requested_blocks = _validate_arguments(arguments)
    output = _mode_root(arguments.output_dir, mode)
    output.mkdir(parents=True, exist_ok=True)
    corpus = load_r4_corpus(arguments.corpus)
    publisher_corpus, dataset_projection = _publisher_corpus(corpus, output)
    pricing = load_pricing_snapshot(arguments.pricing)
    prior = _prior_usage(
        arguments.r4b_incident_snapshot,
        amendment_path=DEFAULT_R4C_PROTOCOL_AMENDMENT,
        predecessor_amendment_path=DEFAULT_R4B_PROTOCOL_AMENDMENT,
        pricing=pricing,
    )
    diagnostic = _diagnostic_reference(arguments.r3_diagnostic)
    paths = _source_paths(pricing_snapshot=arguments.pricing)
    paths["r4_corpus"] = arguments.corpus.resolve()
    paths["r4b_incident_snapshot"] = arguments.r4b_incident_snapshot.resolve()
    paths["cloud_dataset_projection"] = output / "cloud-dataset-projections" / "manifest.json"
    hashes = source_hashes(paths)
    source_capsule = _embedded_source_capsule(paths)
    if {name: str(record["sha256"]) for name, record in source_capsule["files"].items()} != hashes:
        raise RuntimeError("R4_EMBEDDED_SOURCE_CAPSULE_HASH_DRIFT")
    preregistration, specs = build_preregistration(
        corpus,
        pricing=pricing,
        source_hashes=hashes,
        prior_usage=prior,
        execution_mode=mode,
        caps=DEFAULT_BUDGET_CAPS,
    )
    if len(specs) != EXPECTED_ATTEMPTS:
        raise RuntimeError("R4_ATTEMPT_COUNT_DRIFT")
    by_stage = {stage: sum(spec.stage == stage for spec in specs) for stage in STAGES}
    if sorted(by_stage.values()) != [288, 384]:
        raise RuntimeError("R4_STAGE_FACTORIZATION_DRIFT")
    # The local predecessor receipt is pinned but never exposed to the model.
    preregistration = {
        **preregistration,
        "predecessor_diagnostic": diagnostic,
        "cloud_dataset_projection": dataset_projection,
        "embedded_source_capsule": source_capsule,
    }
    schedule = schedule_records(specs)
    if len(schedule) != EXPECTED_ATTEMPTS:
        raise RuntimeError("R4_SCHEDULE_ATTEMPT_COUNT_DRIFT")
    if (
        preregistration.get("schedule_sha256") != json_sha256(schedule)
        or preregistration.get("schedule_records") != schedule
    ):
        raise RuntimeError("R4_MODULE_PREREGISTRATION_SCHEDULE_DRIFT")
    # The schedule is a separate immutable artifact; its digest is sealed into
    # the preregistration and the SQLite execution context.
    preregistration["runner_schedule_sha256"] = json_sha256(schedule)
    preregistration_body = {
        key: value for key, value in preregistration.items() if key != "preregistration_sha256"
    }
    preregistration["preregistration_sha256"] = json_sha256(preregistration_body)
    prereg_path = output / "preregistration.json"
    schedule_path = output / "attempt-schedule.json"
    _write_or_verify(prereg_path, preregistration)
    _write_or_verify(
        schedule_path,
        {
            "schema_version": "1.0",
            "study_version": STUDY_VERSION,
            "study_instance_sha256": preregistration["study_instance_sha256"],
            "schedule_sha256": json_sha256(schedule),
            "rows": schedule,
        },
    )
    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "network_calls": 0,
                    "study_instance_sha256": preregistration["study_instance_sha256"],
                    "preregistration_sha256": preregistration["preregistration_sha256"],
                    "attempts": len(specs),
                    "provider_calls": len(specs),
                    "stage_attempts": by_stage,
                    "combined_worst_case": preregistration["budget"]["combined_worst_case"],
                    "production_release_eligible": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    store = CampaignStore(output / "campaign.sqlite")
    try:
        destination = (
            {"kind": "wandb_weave", "entity": str(arguments.entity), "project": arguments.project}
            if mode == "live"
            else {"kind": "local_only", "entity": "local", "project": TARGET_PROJECT}
        )
        _bind_store(
            store,
            preregistration=preregistration,
            schedule=schedule,
            corpus=corpus,
            mode=mode,
            destination=destination,
            prior_usage=prior,
        )
        run_pricing = pricing
        if mode == "fake":
            run_pricing = {
                model: ModelPrice(
                    input_per_million=Decimal(0),
                    output_per_million=Decimal(0),
                    source="fake-provider-zero-network",
                    observed_at="2026-08-07",
                )
                for model in MODELS
            }
            publisher: LocalCampaignPublisher | WandbCampaignPublisher = LocalCampaignPublisher(
                output / "local-publication"
            )
            provider = PerfectGroundingFakeProvider()
        else:
            publisher = _prepare_publisher(
                output=output,
                preregistration_path=prereg_path,
                preregistration=preregistration,
                corpus=publisher_corpus,
                entity=str(arguments.entity),
                project=arguments.project,
            )
            provider = WandbInferenceAdapter(CredentialSession(), max_transport_attempts=1)
        for stage in STAGES:
            reserve_stage(
                stage,
                [spec for spec in specs if spec.stage == stage],
                pricing=run_pricing,
                store=store,
                caps=DEFAULT_BUDGET_CAPS,
                maximum_input_tokens_per_call=R4C_MAX_INPUT_TOKENS,
            )
        executor = GroundingExecutor(
            corpus=corpus,
            provider=provider,
            entity=str(arguments.entity or "local"),
            project=arguments.project,
            pricing=run_pricing,
            execution_mode=mode,
            study_instance_sha256=str(preregistration["study_instance_sha256"]),
        )
        for block in requested_blocks:
            selected = [spec for spec in specs if spec.time_block == block]
            block_name = f"block-{block}"
            existing = store.get_receipt(block_name)
            if existing is not None:
                _validate_existing_block(
                    store,
                    block=block,
                    selected=selected,
                    receipt=existing,
                )
                continue
            for stage in STAGES:
                store.begin_session(stage, block)
            observer = publisher.start_time_block(specs, time_block=block)
            completed, reused = run_attempt_batch(
                store=store,
                specs=selected,
                corpus=corpus,
                executor=executor,
                observer=observer,
                caps=DEFAULT_BUDGET_CAPS,
            )
            cloud = observer.finish()
            for stage in STAGES:
                store.finish_session(stage, block)
            body = {
                "schema_version": "1.0",
                "study_version": STUDY_VERSION,
                "time_block": block,
                "attempt_count": len(selected),
                "attempts_completed": completed,
                "attempts_reused": reused,
                "attempt_ids_sha256": json_sha256([spec.attempt_id for spec in selected]),
                "cloud": cloud,
            }
            receipt = {**body, "receipt_sha256": json_sha256(body)}
            store.put_receipt(block_name, receipt)
            _write_or_verify(output / f"{block_name}-receipt.json", receipt)

        complete = _completed_exactly(store, specs=specs)
        publication: dict[str, Any] | None = None
        if complete:
            analysis = analyze(store.attempts(), expected_schedule=specs)
            package = output / "publication"
            package.mkdir(parents=True, exist_ok=True)
            _write_or_verify(package / "analysis.json", analysis)
            _write_or_verify(
                package / "attempts.json",
                {
                    "schema_version": "1.0",
                    "study_version": STUDY_VERSION,
                    "rows": store.attempts(),
                },
            )
            publication_tables = {
                "grounding-primary.json": analysis["grounding_primary_unique_case_results"],
                "grounding-mechanisms.json": analysis["grounding_mechanism_results"],
                "prose-cap-primary.json": analysis["prose_cap_primary_unique_case_results"],
                "prose-cap-mcnemar.json": analysis["prose_cap_paired_mcnemar"],
                "attempt-diagnostics.json": analysis[
                    "attempt_level_diagnostics_not_inferential_units"
                ],
            }
            for filename, rows in publication_tables.items():
                _write_or_verify(
                    package / filename,
                    {
                        "schema_version": "1.0",
                        "study_version": STUDY_VERSION,
                        "rows": rows,
                    },
                )
            cloud_receipt = publisher.publish_architecture_development_study(package)
            publication = cloud_receipt.as_dict()
            _write_or_verify(output / "publication-cloud-receipt.json", publication)
        receipt = _execution_receipt(
            store=store,
            preregistration=preregistration,
            mode=mode,
            completed=complete,
            publication=publication,
        )
        write_json_atomic(output / "execution-receipt.json", receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
