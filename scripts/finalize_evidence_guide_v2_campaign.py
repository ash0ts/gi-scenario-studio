#!/usr/bin/env python3
"""Seal and optionally publish an early fail-closed Evidence Guide v2 result."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

from gi_scenario_core.evidence_guide_v2_campaign import (
    CampaignStore,
    build_release_gate,
    export_stage_tables,
    file_sha256,
    json_sha256,
    load_corpus,
    write_json_atomic,
)
from gi_scenario_core.evidence_guide_v2_cloud import (
    TARGET_PROJECT,
    WandbCampaignPublisher,
    _campaign_run_id,
)
from gi_scenario_core.evidence_guide_v2_terminal import (
    build_trace_canary_terminal_disposition,
    validate_terminal_disposition,
)
from gi_scenario_core.observability import CredentialSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-v2-campaign-r2b"
CORPUS_ROOT = ROOT / "benchmarks" / "evidence_guide" / "v2"
RUNNER_PATH = ROOT / "scripts" / "run_evidence_guide_v2_campaign.py"
PROHIBITED_RUN_FILES = frozenset(
    {"output.log", "requirements.txt", "wandb-metadata.json"}
)


def _runner() -> Any:
    spec = importlib.util.spec_from_file_location("evidence_guide_v2_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("campaign runner could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=TARGET_PROJECT)
    parser.add_argument("--confirm-disabled-release")
    parser.add_argument("--remove-prohibited-run-files", action="store_true")
    return parser


def _valid_preregistration(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    body = {key: item for key, item in value.items() if key != "preregistration_sha256"}
    if value.get("preregistration_sha256") != json_sha256(body):
        raise RuntimeError("campaign preregistration digest is invalid")
    expected_runner = value.get("source_hashes", {}).get("campaign_entrypoint")
    if expected_runner != file_sha256(RUNNER_PATH):
        raise RuntimeError("pinned campaign runner drifted before finalization")
    return value


def _cloud_receipt(store: CampaignStore, name: str) -> dict[str, Any]:
    receipt = store.get_receipt(name)
    if not isinstance(receipt, dict):
        raise TypeError(f"missing cloud receipt: {name}")
    return receipt


def _publication_receipt(
    *,
    publisher: WandbCampaignPublisher,
    store: CampaignStore,
    corpus: Any,
    preregistration: dict[str, Any],
    summary_path: Path,
    gate_path: Path,
    terminal_path: Path,
    remove_prohibited_run_files: bool,
) -> dict[str, Any]:
    import wandb  # type: ignore[import-not-found]

    named_receipts = {
        name: _cloud_receipt(store, name)
        for name in (
            "protocol_amendment_cloud",
            "operational_incident_cloud",
            "preregistration_cloud",
            "release_gate_cloud",
            "campaign_tables_cloud",
        )
    }
    canary = _cloud_receipt(store, "trace_canary")
    block_cloud = [
        item.get("cloud")
        for item in canary.get("cloud_block_receipts", [])
        if isinstance(item, dict)
    ]
    if len(block_cloud) != 3 or any(not isinstance(item, dict) for item in block_cloud):
        raise RuntimeError("trace-canary cloud receipt set is incomplete")
    visible = _cloud_receipt(store, "visible_datasets_cloud")
    visible_refs = visible.get("references")
    if not isinstance(visible_refs, dict):
        raise TypeError("visible dataset references are incomplete")

    dataset_identity = json_sha256(
        {
            split: corpus.split_sha256[split]
            for split in ("development", "validation", "judge_calibration")
        }
    )
    dataset_run_id = _campaign_run_id(
        campaign_instance_sha256=preregistration["preregistration_sha256"],
        kind="visible_datasets",
        entity=publisher.entity,
        project=publisher.project,
        identity=dataset_identity,
    )
    run_ids = {
        dataset_run_id,
        *(str(receipt["run_id"]) for receipt in named_receipts.values()),
        *(str(item["run_id"]) for item in block_cloud),
    }
    affected: list[tuple[str, str]] = []
    removed_prohibited_files = 0
    with publisher.cloud_environment():
        api = wandb.Api()
        for run_id in sorted(run_ids):
            run = api.run(f"{publisher.entity}/{publisher.project}/{run_id}")
            affected.extend(
                (run_id, item.name)
                for item in run.files()
                if item.name in PROHIBITED_RUN_FILES
            )
        if affected and remove_prohibited_run_files:
            removed_prohibited_files = len(affected)
            for run_id, name in affected:
                run = api.run(f"{publisher.entity}/{publisher.project}/{run_id}")
                run.file(name).delete()
            for _attempt in range(10):
                remaining: list[tuple[str, str]] = []
                refreshed = wandb.Api()
                for run_id in sorted(run_ids):
                    run = refreshed.run(
                        f"{publisher.entity}/{publisher.project}/{run_id}"
                    )
                    remaining.extend(
                        (run_id, item.name)
                        for item in run.files()
                        if item.name in PROHIBITED_RUN_FILES
                    )
                affected = remaining
                if not affected:
                    break
                time.sleep(0.5)
    if affected:
        raise RuntimeError(
            "W&B run-file privacy audit failed: "
            + ", ".join(
                f"{run_id}/{name}" for run_id, name in sorted(affected)
            )
        )

    by_architecture = {
        str(item["group"]["architecture_id"]): item["cloud"]
        for item in canary["cloud_block_receipts"]
    }
    links = [
        {
            "label": "R2b disabled release gate",
            "url": named_receipts["release_gate_cloud"]["run_url"],
        },
        {
            "label": "R2b campaign evidence tables",
            "url": named_receipts["campaign_tables_cloud"]["run_url"],
        },
        {
            "label": "Granite A1 canary in Weave",
            "url": by_architecture["A1"]["weave_evaluation_run_url"],
        },
        {
            "label": "Deterministic D1 canary in Weave",
            "url": by_architecture["D1"]["weave_evaluation_run_url"],
        },
        {
            "label": "Deterministic D0 canary in Weave",
            "url": by_architecture["D0"]["weave_evaluation_run_url"],
        },
        {
            "label": "Excluded zero-inference readback incident",
            "url": named_receipts["operational_incident_cloud"]["run_url"],
        },
    ]
    artifact_references = sorted(
        {
            str(receipt["artifact_reference"])
            for receipt in named_receipts.values()
            if receipt.get("artifact_reference")
        }
        | {
            str(value["wandb_artifact"])
            for value in visible_refs.values()
            if isinstance(value, dict) and value.get("wandb_artifact")
        }
    )
    body = {
        "schema_version": "1.0",
        "campaign_version": preregistration["campaign_version"],
        "entity": publisher.entity,
        "project": publisher.project,
        "project_access": publisher.binding.access,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "article_summary_sha256": file_sha256(summary_path),
        "release_gate_sha256": file_sha256(gate_path),
        "terminal_disposition_sha256": file_sha256(terminal_path),
        "trace_canary_receipt_sha256": canary["receipt_sha256"],
        "decision": "DISABLE_LIVE_GUIDE",
        "all_r2_arms_and_failures_included": True,
        "hidden_attempt_rows_published": False,
        "links": links,
        "artifact_references": artifact_references,
        "run_file_privacy_audit": {
            "audited_runs": len(run_ids),
            "prohibited_files": 0,
            "removed_prohibited_files": removed_prohibited_files,
        },
    }
    return {**body, "publication_sha256": json_sha256(body)}


def main() -> None:
    arguments = _parser().parse_args()
    output = arguments.output_dir.resolve()
    preregistration = _valid_preregistration(output / "preregistration.json")
    store = CampaignStore(output / "campaign.sqlite")
    runner = _runner()
    corpus = load_corpus(CORPUS_ROOT)

    gate = store.get_receipt("terminal_release_gate")
    if gate is None:
        gate = build_release_gate(
            store=store,
            preregistration=preregistration,
            winner=None,
            hash_pins=runner._hash_pins(corpus, preregistration, store),
        )
        store.put_receipt("terminal_release_gate", gate)
    if gate.get("decision") != "DISABLE_LIVE_GUIDE":
        raise RuntimeError("terminal finalization may only publish a disabled release")

    terminal = store.get_receipt("terminal_disposition")
    if terminal is None:
        terminal = build_trace_canary_terminal_disposition(
            store=store,
            preregistration=preregistration,
            release_gate=gate,
            finalizer_sha256=file_sha256(Path(__file__)),
        )
        store.put_receipt("terminal_disposition", terminal)
    terminal = validate_terminal_disposition(terminal)

    gate_path = output / "release-gate.json"
    terminal_path = output / "terminal-disposition.json"
    write_json_atomic(gate_path, gate)
    write_json_atomic(terminal_path, terminal)
    summary = runner._article_summary(store, gate)
    summary = {
        **summary,
        "terminal_disposition": terminal,
        "decision": "DISABLE_LIVE_GUIDE",
    }
    summary_path = output / "article-summary.json"
    write_json_atomic(summary_path, summary)

    publication: dict[str, Any] = {"mode": "local_finalization"}
    if arguments.publish:
        if arguments.project != TARGET_PROJECT:
            raise SystemExit(f"--publish requires exact project {TARGET_PROJECT}")
        if arguments.confirm_disabled_release != "DISABLE_LIVE_GUIDE":
            raise SystemExit(
                "--publish requires --confirm-disabled-release DISABLE_LIVE_GUIDE"
            )
        if not arguments.entity or not os.environ.get("WANDB_API_KEY"):
            raise SystemExit("--publish requires WANDB_ENTITY and WANDB_API_KEY")
        publisher = WandbCampaignPublisher(
            entity=arguments.entity,
            project=arguments.project,
            workspace=output / "cloud",
            credentials=CredentialSession(),
            campaign_instance_sha256=preregistration["preregistration_sha256"],
        )
        prereg_cloud = store.get_receipt("preregistration_cloud")
        if prereg_cloud is None:
            raise RuntimeError("preregistration cloud receipt is missing")
        publisher.reuse_preregistration(
            prereg_cloud, path=output / "preregistration.json"
        )

        gate_cloud = store.get_receipt("release_gate_cloud")
        if gate_cloud is None:
            checked_in, checked_in_index = runner._publish_checked_in_release_gate(gate)
            receipt = publisher.publish_release_gate(gate_path)
            gate_cloud = {
                **receipt.as_dict(),
                "checked_in_gate": str(checked_in.relative_to(ROOT)),
                "checked_in_index": str(checked_in_index.relative_to(ROOT)),
                "checked_in_index_sha256": file_sha256(checked_in_index),
            }
            store.put_receipt("release_gate_cloud", gate_cloud)
        gate_reference = gate_cloud.get("artifact_reference")
        if (
            gate_cloud.get("source_sha256") != file_sha256(gate_path)
            or gate_cloud.get("readback_sha256") != file_sha256(gate_path)
            or not gate_reference
        ):
            raise RuntimeError("disabled release-gate cloud receipt is invalid")

        tables_dir = output / "tables"
        export_stage_tables(store, tables_dir)
        write_json_atomic(tables_dir / "terminal-disposition.json", terminal)
        write_json_atomic(tables_dir / "release-gate.json", gate)
        write_json_atomic(tables_dir / "article-summary.json", summary)
        tables_cloud = store.get_receipt("campaign_tables_cloud")
        if tables_cloud is None:
            tables_receipt = publisher.publish_campaign_tables(
                tables_dir,
                release_gate_reference=str(gate_reference),
            )
            tables_cloud = tables_receipt.as_dict()
            store.put_receipt("campaign_tables_cloud", tables_cloud)
        publication = {
            "mode": "published_disabled_release",
            "release_gate": gate_cloud,
            "campaign_tables": tables_cloud,
        }
        publication_receipt = _publication_receipt(
            publisher=publisher,
            store=store,
            corpus=corpus,
            preregistration=preregistration,
            summary_path=summary_path,
            gate_path=gate_path,
            terminal_path=terminal_path,
            remove_prohibited_run_files=arguments.remove_prohibited_run_files,
        )
        publication_path = output / "wandb-publication.json"
        write_json_atomic(publication_path, publication_receipt)
        publication["publication_receipt"] = publication_receipt

    receipt = {
        "schema_version": "1.0",
        "terminal_disposition_sha256": terminal["receipt_sha256"],
        "release_gate_digest": gate["release_gate_digest"],
        "article_summary_sha256": file_sha256(summary_path),
        "publication": publication,
    }
    print(json.dumps(receipt, sort_keys=True))
    store.close()


if __name__ == "__main__":
    main()
