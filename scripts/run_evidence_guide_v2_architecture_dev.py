#!/usr/bin/env python3
"""Plan or run the release-ineligible Evidence Guide architecture study.

``--dry-run`` seals the exact 5,712-call plan locally; ``--fake-provider``
exercises all durable state with zero network calls. Paid ``--execute`` is
deliberately narrow: one preregistered stage and time block, an exact Restricted
W&B destination, and an explicit cost-cap acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gi_scenario_core.evidence_guide_v2_architecture_dev import (
    OUTPUT_DIRECTORY_NAME,
    RESEARCH_EXECUTION_MODES,
    RESEARCH_STAGES,
    ResearchFakeExecutor,
    apply_predecessor_debits,
    build_research_preregistration,
    export_research_tables,
    finalize_exploratory_study,
    schedule_counts,
    schedule_records,
    source_hashes_for_study,
)
from gi_scenario_core.evidence_guide_v2_architecture_executor import (
    RESEARCH_REQUEST_BYTE_CAP,
    ArchitectureDevelopmentExecutor,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    DEFAULT_BUDGET_CAPS,
    TARGET_PROJECT,
    TIME_BLOCKS,
    CampaignStore,
    file_sha256,
    json_sha256,
    load_corpus,
    load_pricing_snapshot,
    reserve_stage,
    run_attempt_batch,
    write_json_atomic,
)
from gi_scenario_core.evidence_guide_v2_cloud import (
    LocalCampaignPublisher,
    WandbCampaignPublisher,
    group_specs,
)
from gi_scenario_core.inference import WandbInferenceAdapter
from gi_scenario_core.observability import CredentialSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "evidence_guide" / "v2"
DEFAULT_PRICING = DEFAULT_CORPUS / "inference-pricing-2026-08-06.json"
DEFAULT_R2B_TERMINAL = (
    ROOT / "dist" / "evidence-guide-v2-campaign-r2b" / "terminal-disposition.json"
)
DEFAULT_R1_INCIDENT = ROOT / "dist" / "evidence-guide-v2-architecture-dev-r1-incident.json"
DEFAULT_OUTPUT = ROOT / "dist" / OUTPUT_DIRECTORY_NAME
CONFIRM_COST = "300.00"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--fake-provider", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--stage", choices=("all", *RESEARCH_STAGES), default="all")
    parser.add_argument("--time-block", choices=TIME_BLOCKS)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    parser.add_argument("--r2b-terminal", type=Path, default=DEFAULT_R2B_TERMINAL)
    parser.add_argument("--r1-incident", type=Path, default=DEFAULT_R1_INCIDENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=TARGET_PROJECT)
    parser.add_argument("--confirm-cost-cap-usd")
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path.name}")
    return value


def _mode_output_root(base: Path, execution_mode: str) -> Path:
    if execution_mode not in RESEARCH_EXECUTION_MODES:
        raise ValueError("unknown architecture-development execution mode")
    return base.resolve() / execution_mode


def _complete_block_receipt(
    value: dict[str, Any] | None,
    *,
    group: dict[str, Any],
    count: int,
    execution_context: dict[str, Any],
) -> bool:
    if not value:
        return False
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if (
        value.get("receipt_sha256") != json_sha256(body)
        or value.get("group") != group
        or value.get("execution_context") != execution_context
    ):
        return False
    cloud = value.get("cloud")
    common = bool(
        isinstance(cloud, dict)
        and value.get("completed") == count
        and value.get("reused") == 0
        and cloud.get("attempt_count") == count
        and cloud.get("group_sha256") == json_sha256(group)
    )
    if not common:
        return False
    if execution_context["mode"] == "fake":
        return bool(
            cloud.get("mode") == "local"
            and execution_context["destination"]
            == {"kind": "local_only", "entity": "local", "project": TARGET_PROJECT}
        )
    return _confirmed_live_cloud_block(
        cloud,
        entity=str(execution_context["destination"]["entity"]),
        project=str(execution_context["destination"]["project"]),
    )


def _destination_fragment(entity: str, project: str) -> str:
    return f"/{entity}/{project}/"


def _wandb_url_targets(value: Any, *, entity: str, project: str) -> bool:
    parsed = urlparse(str(value or ""))
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "wandb.ai"
        and parsed.path.startswith(_destination_fragment(entity, project))
    )


def _weave_uri_targets(value: Any, *, entity: str, project: str) -> bool:
    parsed = urlparse(str(value or ""))
    return bool(
        parsed.scheme == "weave" and parsed.path.startswith(_destination_fragment(entity, project))
    )


def _confirmed_live_cloud_block(
    cloud: dict[str, Any],
    *,
    entity: str,
    project: str,
) -> bool:
    """Require independent W&B and Weave acknowledgements for live completion."""

    return bool(
        cloud.get("mode") == "wandb_weave"
        and isinstance(cloud.get("run_id"), str)
        and cloud.get("run_id")
        and _wandb_url_targets(cloud.get("run_url"), entity=entity, project=project)
        and _weave_uri_targets(cloud.get("weave_evaluation_uri"), entity=entity, project=project)
        and _wandb_url_targets(
            cloud.get("weave_evaluation_run_url"), entity=entity, project=project
        )
        and cloud.get("hidden") is False
    )


def _execution_context(
    *,
    mode: str,
    study_instance_sha256: str,
    entity: str | None,
    project: str,
) -> dict[str, Any]:
    if mode not in RESEARCH_EXECUTION_MODES:
        raise ValueError("unknown architecture-development execution mode")
    destination = (
        {"kind": "local_only", "entity": "local", "project": TARGET_PROJECT}
        if mode == "fake"
        else {
            "kind": "wandb_weave",
            "entity": str(entity or ""),
            "project": project,
        }
    )
    return {
        "schema_version": "1.0",
        "mode": mode,
        "study_instance_sha256": study_instance_sha256,
        "destination": destination,
    }


def _receipt_has_forbidden_mode(receipt: Any, *, expected_mode: str) -> bool:
    if isinstance(receipt, dict):
        mode = receipt.get("mode")
        kind = str(receipt.get("kind") or "")
        entity = receipt.get("entity")
        if expected_mode == "live" and (
            mode == "local" or kind.startswith("local_") or entity == "local"
        ):
            return True
        if expected_mode == "fake" and (
            mode == "wandb_weave" or (kind == "preregistration" and entity not in {None, "local"})
        ):
            return True
        return any(
            _receipt_has_forbidden_mode(item, expected_mode=expected_mode)
            for item in receipt.values()
        )
    if isinstance(receipt, list):
        return any(
            _receipt_has_forbidden_mode(item, expected_mode=expected_mode) for item in receipt
        )
    return False


def _bind_store_execution(
    store: CampaignStore,
    *,
    execution_context: dict[str, Any],
    scheduled_attempt_ids: set[str],
) -> None:
    """Bind a database once and reject copied rehearsal/cloud state.

    Output-root isolation is the first boundary.  This immutable database
    binding and full checkpoint scan are the second boundary, so copying a
    fake database into the live directory fails closed rather than resuming it.
    """

    existing = store.get_meta("architecture_execution_context")
    state_tables = (
        "stage_sessions",
        "stage_reservations",
        "attempts",
        "attempt_execution_ledger",
        "receipts",
    )
    if existing is None:
        populated = any(
            int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in state_tables
        )
        if populated:
            raise RuntimeError("R3_UNBOUND_EXECUTION_STATE_REJECTED")
        store.put_meta(
            "architecture_execution_context",
            execution_context,
            immutable=True,
        )
    elif existing != execution_context:
        raise RuntimeError("R3_EXECUTION_MODE_OR_DESTINATION_MISMATCH")

    preregistration = store.get_meta("preregistration")
    if preregistration is not None and (
        preregistration.get("execution_mode") != execution_context["mode"]
        or preregistration.get("study_instance_sha256")
        != execution_context["study_instance_sha256"]
    ):
        raise RuntimeError("R3_PREREGISTRATION_EXECUTION_MODE_MISMATCH")

    observed_attempt_ids = {
        str(row["attempt_id"])
        for row in store.connection.execute("SELECT attempt_id FROM attempts")
    }
    if not observed_attempt_ids.issubset(scheduled_attempt_ids):
        raise RuntimeError("R3_FOREIGN_EXECUTION_ATTEMPT_STATE_REJECTED")

    for row in store.connection.execute("SELECT name, receipt_json FROM receipts"):
        receipt = json.loads(str(row["receipt_json"]))
        if _receipt_has_forbidden_mode(
            receipt,
            expected_mode=str(execution_context["mode"]),
        ):
            raise RuntimeError("R3_FOREIGN_EXECUTION_RECEIPT_REJECTED")


def _validate_live_preregistration_receipt(
    receipt: dict[str, Any],
    *,
    entity: str,
    project: str,
    source_sha256: str,
) -> None:
    reference = str(receipt.get("artifact_reference") or "")
    if not (
        receipt.get("kind") == "preregistration"
        and receipt.get("entity") == entity
        and receipt.get("project") == project
        and receipt.get("source_sha256") == source_sha256
        and receipt.get("readback_sha256") == source_sha256
        and reference.startswith(f"{entity}/{project}/")
        and _wandb_url_targets(receipt.get("run_url"), entity=entity, project=project)
    ):
        raise RuntimeError("R3_LIVE_PREREGISTRATION_RECEIPT_INVALID")


def _validate_live_visible_dataset_receipt(
    receipt: dict[str, Any],
    *,
    entity: str,
    project: str,
) -> None:
    references = receipt.get("references")
    if not isinstance(references, dict) or set(references) != {
        "development",
        "validation",
        "judge_calibration",
    }:
        raise RuntimeError("R3_LIVE_DATASET_RECEIPT_INVALID")
    for reference in references.values():
        if not isinstance(reference, dict) or not (
            str(reference.get("wandb_artifact") or "").startswith(f"{entity}/{project}/")
            and _weave_uri_targets(reference.get("weave_uri"), entity=entity, project=project)
        ):
            raise RuntimeError("R3_LIVE_DATASET_DESTINATION_MISMATCH")


def _validate_execute_arguments(arguments: argparse.Namespace) -> None:
    if not arguments.execute:
        return
    if arguments.stage == "all" or arguments.time_block is None:
        raise SystemExit("--execute requires one --stage and one --time-block")
    if arguments.project != TARGET_PROJECT:
        raise SystemExit(f"--execute requires exact project {TARGET_PROJECT}")
    if not arguments.entity or not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("--execute requires WANDB_ENTITY and WANDB_API_KEY")
    if arguments.confirm_cost_cap_usd != CONFIRM_COST:
        raise SystemExit("--execute requires --confirm-cost-cap-usd 300.00")


def main() -> None:
    arguments = _parser().parse_args()
    _validate_execute_arguments(arguments)
    execution_mode = "fake" if arguments.fake_provider else "live"
    # Never place fake and live databases, receipts, or output packages in the
    # same directory, including when a caller supplies a custom base path.
    output = _mode_output_root(arguments.output_dir, execution_mode)
    output.mkdir(parents=True, exist_ok=True)
    corpus = load_corpus(arguments.corpus_root.resolve())
    pricing_path = arguments.pricing.resolve()
    pricing = load_pricing_snapshot(pricing_path)
    terminal_path = arguments.r2b_terminal.resolve()
    terminal_receipt = _load_object(terminal_path)
    incident_path = arguments.r1_incident.resolve()
    incident_receipt = _load_object(incident_path)
    preregistration, schedules = build_research_preregistration(
        corpus,
        pricing=pricing,
        source_hashes=source_hashes_for_study(ROOT, pricing_path),
        terminal_receipt=terminal_receipt,
        incident_receipt=incident_receipt,
        execution_mode=execution_mode,
    )
    prereg_path = output / "preregistration.json"
    schedule_path = output / "attempt-schedule.json"
    write_json_atomic(prereg_path, preregistration)
    write_json_atomic(
        schedule_path,
        {
            "schema_version": "1.0",
            "study_version": preregistration["study_version"],
            "study_instance_sha256": preregistration["study_instance_sha256"],
            "schedule_sha256": preregistration["schedule"]["schedule_sha256"],
            "attempts": schedule_records(schedules),
        },
    )
    plan = {
        "schema_version": "1.0",
        "study_version": preregistration["study_version"],
        "execution_mode": execution_mode,
        "mode_specific_output_root": str(output),
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "preregistration_file_sha256": file_sha256(prereg_path),
        "schedule_sha256": preregistration["schedule"]["schedule_sha256"],
        "schedule_file_sha256": file_sha256(schedule_path),
        "counts": schedule_counts(schedules),
        "budget": preregistration["budget"],
        "request_envelope_audit": preregistration["request_envelope_audit"],
        "network_calls": 0,
        "release_eligible": False,
        "assistant_release_basis_may_be_created": False,
    }
    plan_path = output / "research-plan.json"
    write_json_atomic(plan_path, plan)
    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "network_calls": 0,
                    "plan": str(plan_path),
                    "preregistration": str(prereg_path),
                    "attempts": plan["counts"]["attempts"],
                    "logical_provider_calls": plan["counts"]["logical_provider_calls"],
                    "worst_case_transport_attempts": plan["counts"][
                        "worst_case_transport_attempts"
                    ],
                    "combined_transport_attempts_with_predecessor_debits": preregistration[
                        "budget"
                    ]["combined_with_predecessor_debits"]["transport_attempts"],
                    "audited_request_count": preregistration["request_envelope_audit"][
                        "request_count"
                    ],
                    "maximum_request_utf8_bytes": preregistration["request_envelope_audit"][
                        "maximum_request_utf8_bytes"
                    ],
                },
                sort_keys=True,
            )
        )
        return

    store = CampaignStore(output / "campaign.sqlite")
    try:
        context = _execution_context(
            mode=execution_mode,
            study_instance_sha256=preregistration["study_instance_sha256"],
            entity=arguments.entity,
            project=arguments.project,
        )
        scheduled_attempt_ids = {spec.attempt_id for specs in schedules.values() for spec in specs}
        _bind_store_execution(
            store,
            execution_context=context,
            scheduled_attempt_ids=scheduled_attempt_ids,
        )
        store.put_meta("preregistration", preregistration, immutable=True)
        store.put_meta(
            "attempt_schedule_sha256",
            preregistration["schedule"]["schedule_sha256"],
            immutable=True,
        )
        apply_predecessor_debits(store, terminal_receipt, incident_receipt)
        if arguments.fake_provider:
            publisher: Any = LocalCampaignPublisher(output / "local-publication")
            executor: Any = ResearchFakeExecutor()
            prereg_receipt_name = (
                f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                "preregistration_local_seal"
            )
        else:
            credentials = CredentialSession()
            publisher = WandbCampaignPublisher(
                entity=arguments.entity,
                project=arguments.project,
                workspace=output / "cloud",
                credentials=credentials,
                campaign_instance_sha256=preregistration["preregistration_sha256"],
            )
            executor = ArchitectureDevelopmentExecutor(
                corpus=corpus,
                provider=WandbInferenceAdapter(
                    credentials,
                    max_request_bytes=RESEARCH_REQUEST_BYTE_CAP,
                ),
                entity=arguments.entity,
                project=arguments.project,
                pricing=pricing,
            )
            prereg_receipt_name = (
                f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                "preregistration_cloud"
            )

        prereg_cloud = store.get_receipt(prereg_receipt_name)
        if prereg_cloud is None:
            prereg_cloud = publisher.seal_preregistration(prereg_path).as_dict()
            if arguments.execute:
                _validate_live_preregistration_receipt(
                    prereg_cloud,
                    entity=str(arguments.entity),
                    project=arguments.project,
                    source_sha256=file_sha256(prereg_path),
                )
            store.put_receipt(prereg_receipt_name, prereg_cloud)
        elif arguments.execute:
            _validate_live_preregistration_receipt(
                prereg_cloud,
                entity=str(arguments.entity),
                project=arguments.project,
                source_sha256=file_sha256(prereg_path),
            )
            publisher.reuse_preregistration(prereg_cloud, path=prereg_path)

        if arguments.execute:
            visible_receipt_name = (
                f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                "visible_datasets_cloud"
            )
            visible_receipt = store.get_receipt(visible_receipt_name)
            if visible_receipt:
                _validate_live_visible_dataset_receipt(
                    visible_receipt,
                    entity=str(arguments.entity),
                    project=arguments.project,
                )
                publisher.reuse_visible_datasets(visible_receipt, corpus=corpus)
            else:
                references = publisher.publish_visible_datasets(corpus)
                dataset_receipt = {
                    "schema_version": "1.0",
                    "execution_context": context,
                    "references": references,
                    "references_sha256": json_sha256(references),
                }
                _validate_live_visible_dataset_receipt(
                    dataset_receipt,
                    entity=str(arguments.entity),
                    project=arguments.project,
                )
                store.put_receipt(visible_receipt_name, dataset_receipt)
        stages = RESEARCH_STAGES if arguments.stage == "all" else (arguments.stage,)
        blocks = TIME_BLOCKS if arguments.time_block is None else (arguments.time_block,)
        for stage in stages:
            specs = schedules[stage]
            reserve_stage(stage, specs, pricing=pricing, store=store)
            for block in blocks:
                store.begin_session(stage, block)
                for group, selected in group_specs(specs, time_block=block):
                    pending = [spec for spec in selected if not store.has_attempt(spec.attempt_id)]
                    name = (
                        f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                        f"block:{json_sha256(group)}"
                    )
                    receipt = store.get_receipt(name)
                    if not pending:
                        if not _complete_block_receipt(
                            receipt,
                            group=group,
                            count=len(selected),
                            execution_context=context,
                        ):
                            raise RuntimeError("EXPLORATORY_BLOCK_RECEIPT_MISSING")
                        continue
                    if len(pending) != len(selected) or receipt is not None:
                        raise RuntimeError("EXPLORATORY_PARTIAL_BLOCK_REQUIRES_RECONCILIATION")
                    observer = publisher.start_block(
                        group=group,
                        **({"hidden": False} if arguments.execute else {}),
                    )
                    completed, reused = run_attempt_batch(
                        store=store,
                        specs=pending,
                        corpus=corpus,
                        executor=executor,
                        observer=observer,
                        caps=DEFAULT_BUDGET_CAPS,
                        abort_on_error_prefixes=(
                            (
                                "WEAVE_",
                                "WANDB_",
                                "INFERENCE_AUTHORIZATION_",
                                "INFERENCE_CREDITS_",
                                "INFERENCE_MODEL_UNAVAILABLE",
                                "INFERENCE_INPUT_ENVELOPE_",
                                "R2_PROVIDER_INPUT_TOKEN_CAP_",
                            )
                            if arguments.execute
                            else ()
                        ),
                    )
                    cloud = observer.finish()
                    block_receipt_body = {
                        "schema_version": "1.0",
                        "study_version": preregistration["study_version"],
                        "execution_context": context,
                        "group": group,
                        "cloud": cloud,
                        "completed": completed,
                        "reused": reused,
                    }
                    block_receipt = {
                        **block_receipt_body,
                        "receipt_sha256": json_sha256(block_receipt_body),
                    }
                    if not _complete_block_receipt(
                        block_receipt,
                        group=group,
                        count=len(selected),
                        execution_context=context,
                    ):
                        raise RuntimeError("R3_BLOCK_COMPLETION_RECEIPT_INVALID")
                    store.put_receipt(name, block_receipt)
                # A session may become durable only after every scheduled group
                # has the exact mode-bound acknowledgement required above.
                for group, selected in group_specs(specs, time_block=block):
                    name = (
                        f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                        f"block:{json_sha256(group)}"
                    )
                    if not _complete_block_receipt(
                        store.get_receipt(name),
                        group=group,
                        count=len(selected),
                        execution_context=context,
                    ):
                        raise RuntimeError("R3_SESSION_CLOUD_RECEIPT_INCOMPLETE")
                store.finish_session(stage, block)

        completed_all = all(
            store.completed_time_blocks(stage) == set(TIME_BLOCKS) for stage in RESEARCH_STAGES
        )
        terminal = None
        publication_receipt = None
        if completed_all:
            summary, terminal = finalize_exploratory_study(
                store,
                preregistration=preregistration,
                schedules=schedules,
            )
            write_json_atomic(output / "research-summary.json", summary)
            write_json_atomic(output / "terminal-disposition.json", terminal)
            publication_directory = output / "development-publication"
            export_research_tables(store, publication_directory)
            write_json_atomic(publication_directory / "research-summary.json", summary)
            write_json_atomic(publication_directory / "terminal-disposition.json", terminal)
            publication_receipt_name = (
                f"r3:{execution_mode}:{preregistration['study_instance_sha256']}:"
                "architecture_development_publication"
            )
            publication_receipt = (
                store.get_receipt(publication_receipt_name) if arguments.execute else None
            )
            if arguments.execute and publication_receipt is None:
                publication_receipt = publisher.publish_architecture_development_study(
                    publication_directory
                ).as_dict()
                store.put_receipt(
                    publication_receipt_name,
                    publication_receipt,
                )
        result = {
            "mode": "execute" if arguments.execute else "fake_provider",
            "execution_mode": execution_mode,
            "execution_context": context,
            "network_calls": "wandb_weave_and_inference" if arguments.execute else 0,
            "completed_all": completed_all,
            "attempt_count": len(store.attempts()),
            "usage": store.usage(),
            "terminal_disposition": terminal.get("disposition") if terminal else None,
            "release_eligible": False,
            "assistant_release_basis_created": False,
            "cloud_publication_permitted": bool(arguments.execute),
            "development_publication": publication_receipt,
        }
        write_json_atomic(
            output / ("execution-receipt.json" if arguments.execute else "fake-run-receipt.json"),
            result,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    main()
