"""Evaluate both pinned Evidence Guide candidates through W&B Inference.

Dry-run validates the versioned corpus and writes a credential-free execution
plan without importing a cloud SDK or making a network request.  Execute mode
first verifies the exact W&B project is Restricted, then evaluates both pinned
models.  Console output contains only paths, counts, and the fail-closed model
decision; model text and credentials are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from gi_scenario_core.evidence_candidate_evaluation import (
    CANDIDATE_MODELS,
    evaluate_candidate,
    evaluation_plan,
    load_practitioner_ratings,
    model_slug,
    release_decision,
    write_json_atomic,
)
from gi_scenario_core.evidence_evaluation import load_dataset
from gi_scenario_core.inference import WandbInferenceAdapter
from gi_scenario_core.observability import CredentialSession, validate_binding_from_environment

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "benchmarks" / "evidence_guide" / "v1" / "dataset.json"
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-candidate-evaluation"


def _safe_console(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and write a no-network evaluation plan",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="verify Restricted policy and call both pinned W&B Inference models",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--practitioner-ratings", type=Path)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    dataset_path = arguments.dataset.resolve()
    dataset = load_dataset(dataset_path)
    plan = evaluation_plan(dataset, dataset_path=dataset_path)
    plan_path = arguments.output_dir.resolve() / "evaluation-plan.json"
    write_json_atomic(plan_path, plan)

    if arguments.dry_run:
        _safe_console(
            {
                "mode": "dry_run",
                "network_calls": 0,
                "plan": str(plan_path),
                "candidate_count": len(CANDIDATE_MODELS),
                "case_count": plan["case_count"],
                "planned_provider_calls": plan["planned_provider_calls"],
            }
        )
        return

    if not arguments.entity or not arguments.project:
        parser.error("--execute requires --entity and --project (or their W&B environment values)")
    if not os.environ.get("WANDB_API_KEY"):
        parser.error("--execute requires WANDB_API_KEY in the process environment")

    verified = validate_binding_from_environment(
        entity=arguments.entity,
        project=arguments.project,
        project_id="evidence-guide-candidate-evaluation",
    )
    if not verified.verified or verified.access != "Restricted":
        raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")

    credentials = CredentialSession()
    provider = WandbInferenceAdapter(credentials)
    ratings = load_practitioner_ratings(arguments.practitioner_ratings)
    runs = []
    for model_id in CANDIDATE_MODELS:
        run = evaluate_candidate(
            dataset=dataset,
            dataset_path=dataset_path,
            provider=provider,
            entity=arguments.entity,
            project=arguments.project,
            model_id=model_id,
            practitioner_ratings=ratings,
        )
        destination = arguments.output_dir.resolve() / model_slug(model_id)
        outputs_path = destination / "outputs.json"
        evaluation_path = destination / "evaluation.json"
        receipt_path = destination / "receipt.json"
        write_json_atomic(outputs_path, run.outputs)
        write_json_atomic(evaluation_path, run.evaluation)
        write_json_atomic(receipt_path, run.receipt)
        runs.append(run)

    decision = release_decision(runs, dataset=dataset, dataset_path=dataset_path)
    decision_path = arguments.output_dir.resolve() / "release-gate.json"
    write_json_atomic(decision_path, decision)
    _safe_console(
        {
            "mode": "execute",
            "network_calls": sum(run.receipt["provider_call_count"] for run in runs),
            "models_evaluated": len(runs),
            "release_gate": str(decision_path),
            "decision": decision["decision"],
            "selected_model": decision["selected_model"],
        }
    )


if __name__ == "__main__":
    main()
