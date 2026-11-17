"""Plan or run the preregistered Evidence Guide v2 campaign.

No paid call occurs in ``--dry-run`` or ``--fake-provider`` mode. Real mode is
stage-at-a-time, requires an exact Restricted W&B project, a sealed cloud
preregistration, visible-dataset readback, explicit cost acknowledgement, and
one of three time blocks. Hidden execution additionally requires a separately
verified service-account lockbox project and checksum-pinned mounted corpus.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from gi_scenario_core.evidence_evaluation_v2 import (
    load_split,
)
from gi_scenario_core.evidence_guide import (
    PROMPT_SHA256,
    RENDERER_SHA256,
    RESPONSE_SCHEMA_SHA256,
    TOOL_CONTRACT_SHA256,
    VALIDATOR_SHA256,
)
from gi_scenario_core.evidence_guide_v2_amendment import validate_protocol_amendment
from gi_scenario_core.evidence_guide_v2_campaign import (
    CAMPAIGN_VERSION,
    CANDIDATE_MODELS,
    DEFAULT_BUDGET_CAPS,
    MAX_INPUT_TOKENS_PER_CALL,
    PROVIDER_STEPS,
    TARGET_PROJECT,
    TIME_BLOCKS,
    TRACE_CANARY_CASES,
    CampaignStore,
    Corpus,
    FinalistConfig,
    PerfectFakeExecutor,
    assert_confirmatory_budget_from_receipt,
    build_preregistration,
    build_release_gate,
    causal_ablation_gate,
    estimate_hidden_receipt_budget,
    estimate_provider_budget,
    export_stage_tables,
    file_sha256,
    hidden_aggregate,
    json_sha256,
    judge_gate,
    load_corpus,
    load_pricing_snapshot,
    reserve_stage,
    run_attempt_batch,
    seal_selection,
    select_architecture_finalists,
    select_capability_models,
    select_prompt_finalists,
    select_validation_winner,
    stage_attempts,
    write_json_atomic,
)
from gi_scenario_core.evidence_guide_v2_cloud import (
    LocalCampaignPublisher,
    WandbCampaignPublisher,
    group_specs,
)
from gi_scenario_core.evidence_guide_v2_incident import validate_trace_canary_incident
from gi_scenario_core.evidence_guide_v2_protocol import (
    PLAN_REGISTRY_SHA256,
    PROTOCOL_SHA256,
)
from gi_scenario_core.evidence_guide_v2_provider import (
    EvidenceGuideV2ProviderExecutor,
    comparison_answers_from_store,
)
from gi_scenario_core.inference import WandbInferenceAdapter
from gi_scenario_core.observability import CredentialSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "evidence_guide" / "v2"
DEFAULT_PRICING = (
    ROOT / "benchmarks" / "evidence_guide" / "v2" / "inference-pricing-2026-08-06.json"
)
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-v2-campaign-r2b"
DEFAULT_PROTOCOL_AMENDMENT = (
    ROOT / "dist" / "evidence-guide-v2-protocol-amendment-r2.json"
)
DEFAULT_OPERATIONAL_INCIDENT = (
    ROOT / "dist" / "evidence-guide-v2-r2-canary-incident.json"
)
CONFIRM_COST = "300.00"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--fake-provider", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--stage", choices=["all", "trace_canary", *stage_names()], default="all"
    )
    parser.add_argument("--time-block", choices=TIME_BLOCKS)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--protocol-amendment",
        type=Path,
        default=DEFAULT_PROTOCOL_AMENDMENT,
    )
    parser.add_argument(
        "--prior-operational-incident",
        type=Path,
        default=DEFAULT_OPERATIONAL_INCIDENT,
    )
    parser.add_argument("--hidden-lockbox", type=Path)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=TARGET_PROJECT)
    parser.add_argument("--lockbox-project")
    parser.add_argument(
        "--lockbox-service-account",
        default=os.environ.get("EVIDENCE_GUIDE_LOCKBOX_USERNAME"),
    )
    parser.add_argument("--confirm-cost-cap-usd")
    return parser


def stage_names() -> tuple[str, ...]:
    return (
        "capability",
        "architecture",
        "prompt_config",
        "validation",
        "judge_comparison",
        "hidden",
        "soak",
    )


def _mount_hidden(corpus: Corpus, path: Path | None) -> Corpus:
    if path is None:
        return corpus
    receipt = corpus.hidden_receipt
    if not all(
        (
            receipt.get("release_eligible") is True,
            receipt.get("protected_generation") is True,
            receipt.get("sealed") is True,
        )
    ):
        raise RuntimeError("HIDDEN_LOCKBOX_RECEIPT_NOT_RELEASE_ELIGIBLE")
    resolved = path.resolve()
    expected = str(receipt.get("dataset_sha256") or receipt.get("content_sha256") or "")
    if not expected or file_sha256(resolved) != expected:
        raise RuntimeError("HIDDEN_LOCKBOX_HASH_MISMATCH")
    dataset = load_split(resolved)
    if dataset.get("split") != "hidden" or len(dataset["rows"]) != int(
        receipt["expected_row_count"]
    ):
        raise RuntimeError("HIDDEN_LOCKBOX_SHAPE_MISMATCH")
    return replace(
        corpus,
        splits={**corpus.splits, "hidden": tuple(dataset["rows"])},
        source_paths={**corpus.source_paths, "hidden": str(resolved)},
        datasets={**corpus.datasets, "hidden": dataset},
    )


def _source_hashes(pricing_path: Path) -> dict[str, str]:
    files = {
        "campaign_entrypoint": ROOT / "scripts" / "run_evidence_guide_v2_campaign.py",
        "protocol_amendment_builder": ROOT
        / "scripts"
        / "build_evidence_guide_v2_protocol_amendment.py",
        "protocol_amendment_validator": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_amendment.py",
        "operational_incident_builder": ROOT
        / "scripts"
        / "build_evidence_guide_v2_canary_incident.py",
        "operational_incident_validator": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_incident.py",
        "campaign_orchestrator": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_campaign.py",
        "cloud_projection": ROOT / "python" / "gi_scenario_core" / "evidence_guide_v2_cloud.py",
        "provider_executor": ROOT / "python" / "gi_scenario_core" / "evidence_guide_v2_provider.py",
        "deterministic_scorer": ROOT / "python" / "gi_scenario_core" / "evidence_evaluation_v2.py",
        "inference_adapter": ROOT / "python" / "gi_scenario_core" / "inference.py",
        "shared_runtime_protocol": ROOT
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_protocol.py",
        "production_evidence_guide": ROOT / "python" / "gi_scenario_core" / "evidence_guide.py",
        "weave_trace_contract": ROOT / "python" / "gi_scenario_core" / "observability.py",
        "pricing_snapshot": pricing_path,
    }
    return {name: file_sha256(path.resolve()) for name, path in files.items()}


def _hash_pins(
    corpus: Corpus, preregistration: dict[str, Any], store: CampaignStore
) -> dict[str, str]:
    del store
    return {
        "prompt_sha256": PROMPT_SHA256,
        "response_schema_sha256": RESPONSE_SCHEMA_SHA256,
        "tool_contract_sha256": TOOL_CONTRACT_SHA256,
        "plan_registry_sha256": PLAN_REGISTRY_SHA256,
        "protocol_sha256": PROTOCOL_SHA256,
        "renderer_sha256": RENDERER_SHA256,
        "validator_sha256": VALIDATOR_SHA256,
        "dataset_sha256": json_sha256(
            {
                "manifest": corpus.manifest_sha256,
                "splits": dict(corpus.split_sha256),
            }
        ),
        "judge_config_sha256": json_sha256(preregistration["judges"]),
        "receipt_sha256": str(corpus.hidden_receipt["_receipt_sha256"]),
        "protocol_amendment_sha256": str(
            preregistration["protocol_amendment"]["receipt_sha256"]
        ),
    }


def _publish_checked_in_release_gate(gate: dict[str, Any]) -> tuple[Path, Path]:
    """Replace the gate and its independent index in a fail-closed order.

    The corpus manifest is immutable after preregistration, so release metadata
    is not appended to it. The gate is replaced first and the index last. An
    interrupted publication can therefore create only a temporary hash
    mismatch, never a partially published enabled gate.
    """

    destination = ROOT / "benchmarks" / "evidence_guide" / "v2"
    gate_path = destination / "release-gate.json"
    index_path = destination / "release-gate-index.json"
    corpus_manifest_path = destination / "manifest.json"
    gate_next = destination / "release-gate.next.json"
    index_next = destination / "release-gate-index.next.json"
    write_json_atomic(gate_next, gate)
    write_json_atomic(
        index_next,
        {
            "schema_version": "1.0",
            "release_gate_file": "release-gate.json",
            "release_gate_sha256": file_sha256(gate_next),
            "corpus_manifest_sha256": file_sha256(corpus_manifest_path),
        },
    )
    gate_next.replace(gate_path)
    index_next.replace(index_path)
    return gate_path, index_path


def _read_selection(store: CampaignStore, name: str) -> dict[str, Any] | None:
    receipt = store.get_receipt(f"selection:{name}")
    return dict(receipt["selection"]) if receipt else None


def _retained_models(store: CampaignStore) -> list[str]:
    selection = _read_selection(store, "capability")
    if not selection:
        raise RuntimeError("capability selection must be sealed before architecture")
    return list(selection["retained_models"])


def _finalists(store: CampaignStore, name: str) -> list[FinalistConfig]:
    selection = _read_selection(store, name)
    if not selection or selection.get("decision") != "CONTINUE":
        raise RuntimeError(f"{name} did not authorize an LLM confirmatory stage")
    return [FinalistConfig.from_mapping(value) for value in selection["finalists"]]


def _winner(store: CampaignStore) -> FinalistConfig:
    selection = _read_selection(store, "validation_winner")
    if not selection or selection.get("decision") != "CONTINUE" or not selection.get("winner"):
        raise RuntimeError("validation did not seal a production-eligible winner")
    return FinalistConfig.from_mapping(selection["winner"])


def _specs(stage: str, corpus: Corpus, store: CampaignStore) -> list[Any]:
    if stage == "capability":
        return stage_attempts(stage, corpus)
    if stage == "architecture":
        return stage_attempts(stage, corpus, retained_models=_retained_models(store))
    if stage == "prompt_config":
        return stage_attempts(
            stage,
            corpus,
            finalist_configs=_finalists(store, "architecture"),
        )
    if stage == "validation":
        return stage_attempts(
            stage,
            corpus,
            finalist_configs=_finalists(store, "prompt_config"),
        )
    if stage == "judge_comparison":
        return stage_attempts(stage, corpus)
    if stage == "hidden":
        judge = store.get_receipt("judge_comparison")
        if not judge or judge.get("passed") is not True:
            raise RuntimeError("automated explanation-quality gate must pass before hidden")
    if stage == "soak":
        hidden = store.get_receipt("hidden_aggregate")
        if not hidden or hidden.get("gate", {}).get("passed") is not True:
            raise RuntimeError("sealed hidden gate must pass before operational soak")
    return stage_attempts(stage, corpus, winner=_winner(store))


def _trace_canary_specs(corpus: Corpus, campaign_instance_sha256: str) -> list[Any]:
    """Build four non-scientific observability probes outside all study denominators."""

    architecture = stage_attempts(
        "architecture",
        corpus,
        retained_models=["ibm-granite/granite-4.1-8b"],
    )
    selected = []
    for presentation_index, target in enumerate(TRACE_CANARY_CASES):
        architecture_id = str(target["architecture_id"])
        case_id = str(target["case_id"])
        role = str(target["role"])
        base = next(
            item
            for item in architecture
            if item.architecture_id == architecture_id
            and item.case_id == case_id
            and item.ablation_condition is None
        )
        identity = json_sha256(
            {
                "campaign_instance_sha256": campaign_instance_sha256,
                "trace_canary_version": "r2-1",
                "role": role,
                "source_attempt_id": base.attempt_id,
            }
        )[:32]
        selected.append(
            replace(
                base,
                attempt_id=identity,
                stage="trace_canary",
                repeat=0,
                time_block="block-1",
                presentation_index=presentation_index,
            )
        )
    return selected


def _validate_trace_canary(
    rows: Sequence[Mapping[str, Any]],
    cloud_block_receipts: Sequence[Mapping[str, Any] | None],
) -> dict[str, Any]:
    by_role = {
        (str(row.get("architecture_id")), str(row.get("case_id"))): row for row in rows
    }
    d0_pass = by_role.get(("D0", "egv2-dev-multi-turn-fault-003"))
    d0_reject = by_role.get(("D0", "egv2-dev-causal-retrieval-001"))
    d1 = by_role.get(("D1", "egv2-dev-unsupported-002"))
    a1 = by_role.get(("A1", "egv2-dev-unsupported-002"))

    def released_repository_answer(
        row: Mapping[str, Any] | None,
        *,
        provider_calls: int,
        tool_count: int,
        plan_id: str,
    ) -> bool:
        if row is None:
            return False
        tool_events = row.get("details", {}).get("tool_events", [])
        return bool(
            row.get("status") == "completed"
            and row.get("protocol_valid") is True
            and row.get("safe") is True
            and row.get("evidence_valid") is True
            and row.get("repository_fidelity") is True
            and row.get("injection_safe") is True
            and row.get("trace_acknowledged") is True
            and row.get("provider_invocations") == provider_calls
            and row.get("explanation_plan_id") == plan_id
            and row.get("details", {}).get("answer_released") is True
            and len(tool_events) == tool_count
            and (
                tool_count == 0
                or all(
                    event.get("tool_name") == "project.list_limitations"
                    for event in tool_events
                )
            )
        )

    expected_cloud_counts = {"D0": 2, "D1": 1, "A1": 1}
    observed_cloud: dict[str, Mapping[str, Any]] = {}
    for receipt in cloud_block_receipts:
        if not isinstance(receipt, Mapping) or not isinstance(receipt.get("group"), Mapping):
            continue
        architecture_id = str(receipt["group"].get("architecture_id") or "")
        if architecture_id in observed_cloud:
            architecture_id = "duplicate"
        observed_cloud[architecture_id] = receipt

    exact_cloud_receipts = len(cloud_block_receipts) == 3 and set(observed_cloud) == set(
        expected_cloud_counts
    )
    if exact_cloud_receipts:
        for architecture_id, expected_count in expected_cloud_counts.items():
            receipt = observed_cloud[architecture_id]
            group = receipt["group"]
            cloud = receipt.get("cloud")
            exact_cloud_receipts = bool(
                exact_cloud_receipts
                and isinstance(cloud, Mapping)
                and group.get("stage") == "trace_canary"
                and group.get("time_block") == "block-1"
                and group.get("split") == "development"
                and group.get("repeat") == 0
                and receipt.get("completed") == expected_count
                and receipt.get("reused") == 0
                and cloud.get("attempt_count") == expected_count
                and cloud.get("group_sha256") == json_sha256(group)
            )

    checks = {
        "four_unique_attempts": len(rows) == 4 and len(by_role) == 4,
        "all_trace_acknowledged": all(row.get("trace_acknowledged") is True for row in rows),
        "all_protocol_completed": all(
            row.get("status") == "completed" and row.get("protocol_valid") is True
            for row in rows
        ),
        "d0_passing_answer_acknowledged_without_provider": bool(
            released_repository_answer(
                d0_pass,
                provider_calls=0,
                tool_count=0,
                plan_id="evidence_scope_v1",
            )
        ),
        "d0_rejection_acknowledged_without_release": bool(
            d0_reject is not None
            and d0_reject.get("status") == "completed"
            and d0_reject.get("protocol_valid") is True
            and d0_reject.get("trace_acknowledged") is True
            and d0_reject.get("safe") is False
            and d0_reject.get("repository_fidelity") is False
            and d0_reject.get("provider_invocations") == 0
            and not d0_reject.get("details", {}).get("tool_events")
            and d0_reject.get("details", {}).get("answer_released") is False
        ),
        "d1_tool_span_acknowledged": bool(
            released_repository_answer(
                d1,
                provider_calls=0,
                tool_count=1,
                plan_id="authority_boundary_v1",
            )
        ),
        "a1_model_and_tool_spans_acknowledged": bool(
            released_repository_answer(
                a1,
                provider_calls=1,
                tool_count=1,
                plan_id="authority_boundary_v1",
            )
        ),
        "three_exact_cloud_block_receipts": exact_cloud_receipts,
    }
    return {
        "schema_version": "1.0",
        "stage": "trace_canary",
        "scientific_denominator_attempts": 0,
        "attempt_count": len(rows),
        "checks": checks,
        "passed": all(checks.values()),
        "attempt_row_sha256": json_sha256(rows),
        "claim_boundary": (
            "This canary tests observability shape and fail-closed behavior only. It is not "
            "model-quality or architecture-efficacy evidence."
        ),
    }


def _block_receipt_is_complete(
    receipt: Mapping[str, Any] | None,
    *,
    group: Mapping[str, Any],
    expected_attempt_count: int,
) -> bool:
    if not isinstance(receipt, Mapping) or receipt.get("group") != group:
        return False
    cloud = receipt.get("cloud")
    return bool(
        isinstance(cloud, Mapping)
        and receipt.get("completed") == expected_attempt_count
        and receipt.get("reused") == 0
        and cloud.get("attempt_count") == expected_attempt_count
        and cloud.get("group_sha256") == json_sha256(group)
    )


def _require_passed_trace_canary(store: CampaignStore) -> Mapping[str, Any]:
    receipt = store.get_receipt("trace_canary")
    if not receipt or receipt.get("passed") is not True:
        raise RuntimeError("TRACE_CANARY_MUST_PASS_BEFORE_SCIENTIFIC_EXECUTION")
    return receipt


def _post_stage(stage: str, store: CampaignStore) -> dict[str, Any]:
    if store.completed_time_blocks(stage) != set(TIME_BLOCKS):
        return {"stage": stage, "selection": "pending_other_time_blocks"}
    if stage == "capability":
        selection = select_capability_models(store.attempts(stage))
        seal_selection(store, "capability", selection)
    elif stage == "architecture":
        selection = select_architecture_finalists(store.attempts(stage))
        seal_selection(store, "architecture", selection)
    elif stage == "prompt_config":
        deterministic = [
            row
            for row in store.attempts("architecture")
            if row["architecture_id"] == "D1" and not row.get("ablation_condition")
        ]
        selection = select_prompt_finalists(store.attempts(stage), deterministic_rows=deterministic)
        seal_selection(store, "prompt_config", selection)
    elif stage == "validation":
        selection = select_validation_winner(store.attempts(stage))
        seal_selection(store, "validation_winner", selection)
    elif stage == "judge_comparison":
        selection = judge_gate(store.attempts(stage))
        store.put_receipt("judge_comparison", selection)
    elif stage == "hidden":
        selection = hidden_aggregate(
            store.attempts(stage),
            automated_explanation_quality=bool(
                (store.get_receipt("judge_comparison") or {}).get("passed")
            ),
        )
        store.put_receipt("hidden_aggregate", selection)
    else:
        selection = {"stage": "soak", "completed": True}
    return {"stage": stage, "selection": selection}


def _stage_disables_live_guide(result: Mapping[str, Any]) -> bool:
    """Recognize only a completed, structured negative selection.

    Interim blocks intentionally return the string ``pending_other_time_blocks``.
    Treating that status like a selection mapping caused an otherwise completed
    paid block to fail during summary composition.
    """

    selection = result.get("selection")
    return isinstance(selection, Mapping) and selection.get("decision") == ("DISABLE_LIVE_GUIDE")


def _plan(
    corpus: Corpus,
    pricing: dict[str, Any],
    *,
    prior_usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Maximal development/confirmatory plan; selections may stop earlier.
    prior = dict(
        prior_usage
        or {
            "cost_usd": "0",
            "provider_invocations": 0,
            "transport_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    )
    with tempfile.TemporaryDirectory(prefix="gi-evidence-guide-v2-plan-") as temporary:
        placeholder = CampaignStore(Path(temporary) / "campaign.sqlite")
        placeholder.put_receipt(
            "selection:capability",
            {
                "selection": {"retained_models": [CANDIDATE_MODELS[0]]},
                "selection_sha256": "0" * 64,
            },
        )
        placeholder.put_receipt(
            "selection:architecture",
            {
                "selection": {
                    "decision": "CONTINUE",
                    "finalists": [
                        FinalistConfig(CANDIDATE_MODELS[0], "A1", "checklist", 128).as_dict(),
                        FinalistConfig(CANDIDATE_MODELS[0], "A2", "checklist", 128).as_dict(),
                    ],
                },
                "selection_sha256": "1" * 64,
            },
        )
        placeholder.put_receipt(
            "selection:prompt_config",
            {
                "selection": {
                    "decision": "CONTINUE",
                    "finalists": [
                        FinalistConfig(CANDIDATE_MODELS[0], "A1", "checklist", 128).as_dict(),
                        FinalistConfig(CANDIDATE_MODELS[0], "A1", "counterexample", 128).as_dict(),
                    ],
                },
                "selection_sha256": "2" * 64,
            },
        )
        placeholder.put_receipt(
            "selection:validation_winner",
            {
                "selection": {
                    "decision": "CONTINUE",
                    "winner": FinalistConfig(CANDIDATE_MODELS[0], "A1", "checklist", 128).as_dict(),
                },
                "selection_sha256": "3" * 64,
            },
        )
        placeholder.put_receipt(
            "judge_comparison",
            {"passed": True, "planning_placeholder": True},
        )
        placeholder.put_receipt(
            "hidden_aggregate",
            {"gate": {"passed": True}, "planning_placeholder": True},
        )
        estimates = {}
        total_logical = total_transport = total_input = total_output = 0
        total_cost = Decimal(0)
        canary_specs = _trace_canary_specs(corpus, "0" * 64)
        canary_estimate = estimate_provider_budget(canary_specs, pricing=pricing)
        estimates["trace_canary"] = {
            "attempts": len(canary_specs),
            "scientific_denominator_attempts": 0,
            **canary_estimate,
        }
        total_logical += int(canary_estimate["logical_provider_invocations"])
        total_transport += int(canary_estimate["worst_case_transport_attempts"])
        total_input += int(canary_estimate["worst_case_input_tokens"])
        total_output += int(canary_estimate["worst_case_output_tokens"])
        total_cost += Decimal(canary_estimate["worst_case_cost_usd"])
        for stage in stage_names():
            if stage == "capability":
                estimate = {
                    "logical_provider_invocations": 0,
                    "worst_case_transport_attempts": 0,
                    "worst_case_input_tokens": 0,
                    "worst_case_output_tokens": 0,
                    "worst_case_cost_usd": "0",
                }
                specs = [None] * 160
            elif stage == "hidden" and "hidden" not in corpus.splits:
                estimate = estimate_hidden_receipt_budget(
                    corpus,
                    _winner(placeholder),
                    pricing=pricing,
                )
                specs = [None] * int(estimate["attempts"])
            else:
                specs = _specs(stage, corpus, placeholder)
                estimate = estimate_provider_budget(specs, pricing=pricing)
            estimates[stage] = {"attempts": len(specs), **estimate}
            total_logical += int(estimate["logical_provider_invocations"])
            total_transport += int(estimate["worst_case_transport_attempts"])
            total_input += int(estimate["worst_case_input_tokens"])
            total_output += int(estimate["worst_case_output_tokens"])
            total_cost += Decimal(estimate["worst_case_cost_usd"])
        remaining = {
            "logical_provider_invocations": total_logical,
            "worst_case_transport_attempts": total_transport,
            "worst_case_input_tokens": total_input,
            "worst_case_output_tokens": total_output,
            "worst_case_cost_usd": str(total_cost),
        }
        cumulative = {
            "logical_provider_invocations": int(prior["provider_invocations"])
            + total_logical,
            "worst_case_transport_attempts": int(prior["transport_attempts"])
            + total_transport,
            "worst_case_input_tokens": int(prior["input_tokens"]) + total_input,
            "worst_case_output_tokens": int(prior["output_tokens"]) + total_output,
            "worst_case_cost_usd": str(Decimal(str(prior["cost_usd"])) + total_cost),
        }
        result = {
            "stages": estimates,
            "remaining_preregistered_a1_branch_totals": remaining,
            "preregistered_a1_branch_totals": cumulative,
            "prior_campaign_usage": prior,
            "logical_call_definition": "one SDK model request before retry",
            "provider_invocation_hard_cap_definition": (
                "each HTTP transport attempt, including the one permitted retry"
            ),
            "transport_attempt_definition": "individual HTTP attempt including retry",
            "live_llm_can_stop_early_when_d1_wins": True,
        }
        branch_envelopes: dict[str, Any] = {}
        for architecture in ("A1", "A2", "A3"):
            finalists = (
                FinalistConfig(CANDIDATE_MODELS[0], architecture, "checklist", 128),
                FinalistConfig(CANDIDATE_MODELS[0], architecture, "counterexample", 128),
            )
            winner = finalists[0]
            branch_specs = [
                *_trace_canary_specs(corpus, "0" * 64),
                *stage_attempts("architecture", corpus, retained_models=[CANDIDATE_MODELS[0]]),
                *stage_attempts(
                    "prompt_config",
                    corpus,
                    finalist_configs=(
                        FinalistConfig(CANDIDATE_MODELS[0], "A1", "checklist", 128),
                        FinalistConfig(CANDIDATE_MODELS[0], "A2", "checklist", 128),
                    ),
                ),
                *stage_attempts("validation", corpus, finalist_configs=finalists),
                *stage_attempts("judge_comparison", corpus),
                *stage_attempts("soak", corpus, winner=winner),
            ]
            visible = estimate_provider_budget(branch_specs, pricing=pricing)
            hidden = estimate_hidden_receipt_budget(corpus, winner, pricing=pricing)
            envelope = {
                "logical_provider_invocations": int(prior["provider_invocations"])
                + int(visible["logical_provider_invocations"])
                + int(hidden["logical_provider_invocations"]),
                "worst_case_transport_attempts": int(prior["transport_attempts"])
                + int(visible["worst_case_transport_attempts"])
                + int(hidden["worst_case_transport_attempts"]),
                "worst_case_input_tokens": int(prior["input_tokens"])
                + int(visible["worst_case_input_tokens"])
                + int(hidden["worst_case_input_tokens"]),
                "worst_case_output_tokens": int(prior["output_tokens"])
                + int(visible["worst_case_output_tokens"])
                + int(hidden["worst_case_output_tokens"]),
                "worst_case_cost_usd": str(
                    Decimal(str(prior["cost_usd"]))
                    + Decimal(str(visible["worst_case_cost_usd"]))
                    + Decimal(str(hidden["worst_case_cost_usd"]))
                ),
            }
            envelope["admissible"] = bool(
                envelope["worst_case_transport_attempts"]
                <= DEFAULT_BUDGET_CAPS.provider_invocations
                and envelope["worst_case_input_tokens"] <= DEFAULT_BUDGET_CAPS.input_tokens
                and envelope["worst_case_output_tokens"] <= DEFAULT_BUDGET_CAPS.output_tokens
                and Decimal(envelope["worst_case_cost_usd"]) <= DEFAULT_BUDGET_CAPS.cost_usd
            )
            branch_envelopes[architecture] = envelope
        result["confirmatory_architecture_envelopes"] = branch_envelopes
        result["advancement_rule"] = (
            "Continue only when the sealed finalists' unchanged validation, hidden, judge, "
            "and soak envelope is admissible; otherwise DISABLE_LIVE_GUIDE before validation."
        )
        placeholder.close()
        return result


def _article_summary(store: CampaignStore, gate: dict[str, Any] | None) -> dict[str, Any]:
    stage_results = {}
    for stage in stage_names():
        rows = store.attempts(stage)
        stage_results[stage] = {
            "attempt_count": len(rows),
            "completed_count": sum(row.get("status") == "completed" for row in rows),
            "failed_closed_count": sum(row.get("status") == "failed_closed" for row in rows),
            "unsafe_count": sum(row.get("safe") is False for row in rows),
            "invalid_evidence_count": sum(row.get("evidence_valid") is False for row in rows),
            "trace_unacknowledged_count": sum(
                row.get("trace_acknowledged") is False for row in rows
            ),
            "completed_time_blocks": sorted(store.completed_time_blocks(stage)),
        }
    amendment = store.get_meta("protocol_amendment") or {}
    prior_incident = store.get_meta("prior_operational_incident") or {}
    inherited = amendment.get("capability_inheritance", {})
    inherited_outcomes = inherited.get("outcomes", {})
    stage_results["capability"] = {
        "mode": "inherited_protocol_amendment",
        "attempt_count": int(inherited.get("attempt_count") or 0),
        "completed_count": int(inherited_outcomes.get("completed") or 0),
        "failed_closed_count": int(inherited_outcomes.get("failed_closed") or 0),
        "unsafe_count": None,
        "invalid_evidence_count": None,
        "trace_unacknowledged_count": int(inherited.get("attempt_count") or 0)
        - int(inherited_outcomes.get("trace_acknowledged") or 0),
        "completed_time_blocks": list(TIME_BLOCKS),
        "row_sha256": inherited.get("row_sha256"),
    }
    trace_canary_rows = store.attempts("trace_canary")
    stage_results["trace_canary"] = {
        "mode": "non_scientific_observability_probe",
        "attempt_count": len(trace_canary_rows),
        "scientific_denominator_attempts": 0,
        "completed_count": sum(
            row.get("status") == "completed" for row in trace_canary_rows
        ),
        "failed_closed_count": sum(
            row.get("status") == "failed_closed" for row in trace_canary_rows
        ),
        "trace_unacknowledged_count": sum(
            row.get("trace_acknowledged") is False for row in trace_canary_rows
        ),
        "receipt": store.get_receipt("trace_canary"),
    }
    combined_usage = store.usage()
    prior_usage = store.prior_usage()
    r2_usage = {
        "cost_usd": str(
            Decimal(str(combined_usage["cost_usd"]))
            - Decimal(str(prior_usage["cost_usd"]))
        ),
        **{
            key: int(combined_usage[key]) - int(prior_usage[key])
            for key in (
                "provider_invocations",
                "transport_attempts",
                "input_tokens",
                "output_tokens",
            )
        },
    }
    return {
        "schema_version": "1.0",
        "campaign_version": CAMPAIGN_VERSION,
        "claim_boundary": (
            "Automated explanation conformance only; practitioner usefulness, hydraulic "
            "science, and ecological outcomes were not measured."
        ),
        "selections": {
            name: store.get_receipt(f"selection:{name}")
            for name in ("capability", "architecture", "prompt_config", "validation_winner")
        },
        "causal_ablation": causal_ablation_gate(store.attempts("architecture")),
        "judge_comparison": judge_gate(store.attempts("judge_comparison")),
        "release_gate": gate,
        "usage": combined_usage,
        "usage_ledgers": {
            "inherited_capability": inherited.get("usage", {}),
            "excluded_r1_architecture": amendment.get("architecture_exclusion", {}).get(
                "usage", {}
            ),
            "excluded_preflights": amendment.get("preflight_usage", {}),
            "excluded_r2_trace_canary_incident": prior_incident.get(
                "provider_usage", {}
            ),
            "r2_current": r2_usage,
            "combined_cap_debit": combined_usage,
        },
        "worst_case_reserved": store.reserved_budget(),
        "stage_results": stage_results,
        "all_r2_arms_and_failures_included": True,
        "predecessor_evidence_linked_by_protocol_amendment": True,
        "prior_operational_incident": prior_incident,
        "r1_architecture_denominator": 0,
        "practitioner_usefulness_status": "not_measured",
    }


def main() -> None:
    arguments = _parser().parse_args()
    output = arguments.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    amendment_path = arguments.protocol_amendment.resolve()
    if not amendment_path.is_file():
        raise SystemExit(
            "the r2 campaign requires the immutable protocol-amendment receipt"
        )
    amendment = validate_protocol_amendment(
        json.loads(amendment_path.read_text(encoding="utf-8"))
    )
    incident_path = arguments.prior_operational_incident.resolve()
    if not incident_path.is_file():
        raise SystemExit("the restarted campaign requires the sealed canary incident receipt")
    incident = validate_trace_canary_incident(
        json.loads(incident_path.read_text(encoding="utf-8"))
    )
    corpus = _mount_hidden(load_corpus(arguments.corpus_root), arguments.hidden_lockbox)
    pricing_path = arguments.pricing.resolve()
    pricing = load_pricing_snapshot(pricing_path)
    preregistration = build_preregistration(
        corpus,
        pricing=pricing,
        source_hashes=_source_hashes(pricing_path),
        protocol_amendment=amendment,
        prior_operational_incident=incident,
    )
    prereg_path = output / "preregistration.json"
    write_json_atomic(prereg_path, preregistration)

    plan = {
        "schema_version": "1.0",
        "campaign_version": CAMPAIGN_VERSION,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "corpus_manifest_sha256": corpus.manifest_sha256,
        "pricing_sha256": file_sha256(pricing_path),
        "protocol_amendment_sha256": amendment["receipt_sha256"],
        "prior_operational_incident_sha256": incident["receipt_sha256"],
        "network_calls": 0 if not arguments.execute else "stage_dependent",
        **_plan(corpus, pricing, prior_usage=amendment["cumulative_prior_usage"]),
    }
    plan_path = output / "campaign-plan.json"
    write_json_atomic(plan_path, plan)
    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "network_calls": 0,
                    "plan": str(plan_path),
                    "preregistration": str(prereg_path),
                    "a1_branch_logical_provider_invocations": plan[
                        "preregistered_a1_branch_totals"
                    ]["logical_provider_invocations"],
                    "a1_branch_worst_case_cost_usd": plan["preregistered_a1_branch_totals"][
                        "worst_case_cost_usd"
                    ],
                },
                sort_keys=True,
            )
        )
        return

    if arguments.execute:
        if arguments.stage == "all" or arguments.time_block is None:
            raise SystemExit("--execute requires one --stage and one --time-block")
        if arguments.project != TARGET_PROJECT:
            raise SystemExit(f"--execute requires exact project {TARGET_PROJECT}")
        if not arguments.entity or not os.environ.get("WANDB_API_KEY"):
            raise SystemExit("--execute requires WANDB_ENTITY and WANDB_API_KEY")
        if arguments.confirm_cost_cap_usd != CONFIRM_COST:
            raise SystemExit("--execute requires --confirm-cost-cap-usd 300.00")
        if arguments.stage == "hidden" and arguments.hidden_lockbox is None:
            raise SystemExit("hidden execution requires the mounted hidden lockbox")
        if arguments.stage == "trace_canary" and arguments.time_block != "block-1":
            raise SystemExit("trace canary execution is pinned to --time-block block-1")

    store = CampaignStore(output / "campaign.sqlite")
    store.put_meta("preregistration", preregistration, immutable=True)
    store.put_meta("protocol_amendment", amendment, immutable=True)
    store.put_meta("prior_operational_incident", incident, immutable=True)
    store.set_prior_usage(
        protocol_amendment=amendment,
    )
    inherited_selection = amendment["capability_inheritance"]["selection_receipt"]
    store.put_receipt("selection:capability", inherited_selection)
    store.put_receipt(
        "inherited_capability",
        {
            "schema_version": "1.0",
            "eligible": True,
            "attempt_count": amendment["capability_inheritance"]["attempt_count"],
            "row_sha256": amendment["capability_inheritance"]["row_sha256"],
            "selection_sha256": inherited_selection["selection_sha256"],
            "protocol_amendment_sha256": amendment["receipt_sha256"],
        },
    )
    if arguments.fake_provider:
        publisher: Any = LocalCampaignPublisher(output / "local-publication")
        executor: Any = PerfectFakeExecutor()
    else:
        main_credentials = CredentialSession()
        lockbox_credentials = None
        if arguments.lockbox_project:
            lockbox_key = os.environ.get("WANDB_LOCKBOX_API_KEY")
            main_key = os.environ.get("WANDB_API_KEY")
            if not lockbox_key or not arguments.lockbox_service_account or lockbox_key == main_key:
                raise RuntimeError("HIDDEN_LOCKBOX_DISTINCT_SERVICE_ACCOUNT_REQUIRED")
            lockbox_credentials = CredentialSession()
            lockbox_credentials.configure(
                lockbox_key,
                os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai"),
            )
        publisher = WandbCampaignPublisher(
            entity=arguments.entity,
            project=arguments.project,
            workspace=output / "cloud",
            lockbox_project=arguments.lockbox_project,
            credentials=main_credentials,
            lockbox_credentials=lockbox_credentials,
            lockbox_expected_username=arguments.lockbox_service_account,
            campaign_instance_sha256=preregistration["preregistration_sha256"],
        )
        executor = EvidenceGuideV2ProviderExecutor(
            corpus=corpus,
            provider=WandbInferenceAdapter(
                main_credentials,
                max_request_bytes=MAX_INPUT_TOKENS_PER_CALL,
            ),
            entity=arguments.entity,
            project=arguments.project,
            pricing=pricing,
        )

    amendment_cloud_receipt = store.get_receipt("protocol_amendment_cloud")
    if amendment_cloud_receipt:
        amendment_reference = publisher.reuse_protocol_amendment(
            amendment_cloud_receipt,
            path=amendment_path,
        )
    else:
        amendment_cloud_receipt = publisher.publish_protocol_amendment(
            amendment_path
        ).as_dict()
        store.put_receipt("protocol_amendment_cloud", amendment_cloud_receipt)
        amendment_reference = amendment_cloud_receipt.get("artifact_reference")

    incident_cloud_receipt = store.get_receipt("operational_incident_cloud")
    if incident_cloud_receipt:
        incident_reference = publisher.reuse_operational_incident(
            incident_cloud_receipt,
            path=incident_path,
        )
    else:
        incident_cloud_receipt = publisher.publish_operational_incident(
            incident_path
        ).as_dict()
        store.put_receipt("operational_incident_cloud", incident_cloud_receipt)
        incident_reference = incident_cloud_receipt.get("artifact_reference")

    prereg_receipt = store.get_receipt("preregistration_cloud")
    if prereg_receipt and arguments.execute:
        publisher.reuse_preregistration(prereg_receipt, path=prereg_path)
    else:
        receipt = publisher.seal_preregistration(
            prereg_path,
            amendment_reference=(str(amendment_reference) if amendment_reference else None),
            incident_reference=(str(incident_reference) if incident_reference else None),
        ).as_dict()
        store.put_receipt("preregistration_cloud", receipt)

    if arguments.execute:
        visible_receipt = store.get_receipt("visible_datasets_cloud")
        if visible_receipt:
            publisher.reuse_visible_datasets(visible_receipt, corpus=corpus)
        else:
            references = publisher.publish_visible_datasets(corpus)
            store.put_receipt(
                "visible_datasets_cloud",
                {
                    "schema_version": "1.0",
                    "references": references,
                    "references_sha256": json_sha256(references),
                },
            )
        if arguments.stage == "hidden":
            if arguments.hidden_lockbox is None or not arguments.lockbox_project:
                raise RuntimeError(
                    "hidden execution requires a mounted corpus and service-account lockbox project"
                )
            hidden_receipt = store.get_receipt("hidden_lockbox_cloud")
            if hidden_receipt:
                publisher.reuse_hidden_lockbox(
                    hidden_receipt,
                    path=arguments.hidden_lockbox.resolve(),
                    expected_sha256=corpus.split_sha256["hidden"],
                )
            else:
                receipt = publisher.publish_hidden_lockbox(
                    arguments.hidden_lockbox.resolve(),
                    expected_sha256=corpus.split_sha256["hidden"],
                )
                store.put_receipt("hidden_lockbox_cloud", receipt.as_dict())

    stages = (
        ("capability", "trace_canary", *stage_names()[1:])
        if arguments.stage == "all"
        else (arguments.stage,)
    )
    executed: list[dict[str, Any]] = []
    for stage in stages:
        if stage == "capability":
            executed.append(
                {
                    "stage": "capability",
                    "status": "inherited_from_protocol_amendment",
                    "attempt_count": amendment["capability_inheritance"]["attempt_count"],
                    "selection_sha256": inherited_selection["selection_sha256"],
                }
            )
            continue
        if stage != "trace_canary":
            _require_passed_trace_canary(store)
        # A deterministic win is a valid negative result and stops all paid LLM work.
        if stage not in {"capability", "architecture"}:
            architecture = _read_selection(store, "architecture")
            if architecture and architecture.get("decision") == "DISABLE_LIVE_GUIDE":
                executed.append({"stage": stage, "status": "skipped_deterministic_guide_won"})
                break
        specs = (
            _trace_canary_specs(corpus, preregistration["preregistration_sha256"])
            if stage == "trace_canary"
            else _specs(stage, corpus, store)
        )
        stage_executor = executor
        if arguments.execute and stage == "hidden":
            if lockbox_credentials is None or not arguments.lockbox_project:
                raise RuntimeError("HIDDEN_LOCKBOX_DISTINCT_SERVICE_ACCOUNT_REQUIRED")
            stage_executor = EvidenceGuideV2ProviderExecutor(
                corpus=corpus,
                provider=WandbInferenceAdapter(
                    lockbox_credentials,
                    max_request_bytes=MAX_INPUT_TOKENS_PER_CALL,
                ),
                entity=arguments.entity,
                project=arguments.lockbox_project,
                pricing=pricing,
            )
        if (
            arguments.execute
            and stage in {"validation", "hidden", "soak"}
            and getattr(stage_executor, "production_runtime_parity", False) is not True
        ):
            raise RuntimeError("PRODUCTION_RUNTIME_PARITY_EXECUTOR_REQUIRED")
        reserve_stage(stage, specs, pricing=pricing, store=store)
        if stage == "validation":
            validation_finalists = _finalists(store, "prompt_config")
            winner_placeholder = max(
                validation_finalists,
                key=lambda item: (
                    PROVIDER_STEPS[item.architecture_id],
                    item.max_output_tokens,
                    item.config_id,
                ),
            )
            visible_confirmatory = [
                *specs,
                *stage_attempts("judge_comparison", corpus),
                *stage_attempts("soak", corpus, winner=winner_placeholder),
            ]
            assert_confirmatory_budget_from_receipt(
                store,
                visible_confirmatory,
                corpus=corpus,
                winner=winner_placeholder,
                pricing=pricing,
            )
        blocks = (
            ("block-1",)
            if stage == "trace_canary"
            else (
                TIME_BLOCKS
                if arguments.fake_provider and arguments.time_block is None
                else (arguments.time_block,)
            )
        )
        for block in blocks:
            if block is None:
                raise RuntimeError("time block is unavailable")
            store.begin_session(stage, block)
            for group, selected in group_specs(specs, time_block=block):
                pending = [item for item in selected if not store.has_attempt(item.attempt_id)]
                block_receipt_name = f"block:{json_sha256(group)}"
                block_receipt = store.get_receipt(block_receipt_name)
                completed_locally = len(selected) - len(pending)
                if pending and (completed_locally or block_receipt is not None):
                    raise RuntimeError("CAMPAIGN_PARTIAL_BLOCK_REQUIRES_RECONCILIATION")
                if not pending:
                    if not _block_receipt_is_complete(
                        block_receipt,
                        group=group,
                        expected_attempt_count=len(selected),
                    ):
                        raise RuntimeError("CAMPAIGN_BLOCK_CLOUD_RECEIPT_MISSING")
                    continue
                observer = (
                    publisher.start_block(group=group, hidden=bool(group["hidden"]))
                    if arguments.execute
                    else publisher.start_block(group=group)
                )
                if stage == "judge_comparison" and arguments.execute:
                    stage_executor.comparison_answers = comparison_answers_from_store(
                        store, corpus, _winner(store)
                    )
                    if len(stage_executor.comparison_answers) != 80:
                        raise RuntimeError("JUDGE_REQUIRES_80_SEALED_PRESENTATION_PAIRS")
                completed, reused = run_attempt_batch(
                    store=store,
                    specs=pending,
                    corpus=corpus,
                    executor=stage_executor,
                    observer=observer,
                    abort_on_error_prefixes=("WEAVE_",) if arguments.execute else (),
                )
                cloud = observer.finish()
                block_receipt = {
                    "group": group,
                    "cloud": cloud,
                    "completed": completed,
                    "reused": reused,
                }
                if not _block_receipt_is_complete(
                    block_receipt,
                    group=group,
                    expected_attempt_count=len(selected),
                ):
                    raise RuntimeError("CAMPAIGN_BLOCK_CLOUD_RECEIPT_INCOMPLETE")
                store.put_receipt(
                    block_receipt_name,
                    block_receipt,
                )
            store.finish_session(stage, block)
            executed.append(
                {
                    "stage": stage,
                    "time_block": block,
                    "completed_time_blocks": sorted(store.completed_time_blocks(stage)),
                }
            )
        if stage == "trace_canary":
            cloud_block_receipts = [
                store.get_receipt(f"block:{json_sha256(group)}")
                for group, _selected in group_specs(specs, time_block="block-1")
            ]
            result = _validate_trace_canary(
                store.attempts("trace_canary"), cloud_block_receipts
            )
            result = {
                **result,
                "campaign_instance_sha256": preregistration["preregistration_sha256"],
                "protocol_amendment_sha256": amendment["receipt_sha256"],
                "cloud_block_receipts": cloud_block_receipts,
            }
            if result["checks"]["three_exact_cloud_block_receipts"] is not True:
                raise RuntimeError("TRACE_CANARY_CLOUD_RECEIPT_INCOMPLETE")
            result = {**result, "receipt_sha256": json_sha256(result)}
            store.put_receipt("trace_canary", result)
            executed.append({"stage": stage, "selection": result})
            if result["passed"] is not True:
                raise RuntimeError("TRACE_CANARY_FAILED")
            continue
        result = _post_stage(stage, store)
        executed.append(result)
        if _stage_disables_live_guide(result):
            break

    gate = None
    if store.completed_time_blocks("soak") == set(TIME_BLOCKS):
        winner = _winner(store)
        gate = build_release_gate(
            store=store,
            preregistration=preregistration,
            winner=winner,
            hash_pins=_hash_pins(corpus, preregistration, store),
        )
        gate_path = output / "release-gate.json"
        write_json_atomic(gate_path, gate)
        # Runtime consumes the checked-in fail-closed/public release basis.
        if arguments.execute:
            checked_in, checked_in_index = _publish_checked_in_release_gate(gate)
            gate_receipt = publisher.publish_release_gate(gate_path)
            store.put_receipt(
                "release_gate_cloud",
                {
                    **gate_receipt.as_dict(),
                    "checked_in_gate": str(checked_in.relative_to(ROOT)),
                    "checked_in_index": str(checked_in_index.relative_to(ROOT)),
                    "checked_in_index_sha256": file_sha256(checked_in_index),
                },
            )
            tables_dir = output / "tables"
            export_stage_tables(store, tables_dir)
            tables_receipt = publisher.publish_campaign_tables(
                tables_dir,
                release_gate_reference=str(gate_receipt.artifact_reference),
            )
            store.put_receipt("campaign_tables_cloud", tables_receipt.as_dict())

    summary = _article_summary(store, gate)
    write_json_atomic(output / "article-summary.json", summary)
    print(
        json.dumps(
            {
                "mode": "fake_provider" if arguments.fake_provider else "execute",
                "executed": executed,
                "usage": store.usage(),
                "decision": gate["decision"] if gate else "INTERIM_OR_STOPPED",
                "article_summary": str(output / "article-summary.json"),
            },
            sort_keys=True,
        )
    )
    store.close()


if __name__ == "__main__":
    main()
