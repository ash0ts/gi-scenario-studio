#!/usr/bin/env python3
"""Generate the hidden 700-vector Cedar Trace exact benchmark.

Every hydraulic value is extracted from a direct EPA SWMM 5.2.4 execution. The
published dataset is intentionally compact: it retains per-run hashes and
metrics, but not 2,800 duplicate binary artifacts. Search replays may reveal a
row only after requesting its design vector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "python"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import generate_guided_fixture as guided
from gi_scenario_core.demo import (
    GUIDED_COST_BASIS_ID,
    guided_cost_basis_payload,
    present_value_unit_costs_thousand_usd,
)
from gi_scenario_core.search import design_id, enumerate_vectors
from gi_scenario_core.solver import SolverRunner

SCHEMA_VERSION = "2.0"
DATASET_ID = "cedar-trace-exhaustive-exact-v2"
NONBASELINE_DESIGNS = 699
EXPECTED_JOBS = 4 + NONBASELINE_DESIGNS * 4


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run_projection(run: dict[str, Any]) -> dict[str, Any]:
    metrics = run["metrics"]
    return {
        "run_id": run["run_id"],
        "storm_id": run["storm_id"],
        "input_sha256": run["hashes"]["input_sha256"],
        "report_sha256": run["hashes"]["report_sha256"],
        "output_sha256": run["hashes"]["output_sha256"],
        "qa_status": run["qa"]["status"],
        "outfall_volume_million_gallons": metrics["outfall_volume_million_gallons"],
        "outfall_peak_cfs": metrics["outfall_peak_cfs"],
        "system_flooding_loss_million_gallons": metrics["system_flooding_loss_million_gallons"],
        "runoff_continuity_error_percent": metrics["runoff_continuity_error_percent"],
        "flow_continuity_error_percent": metrics["flow_continuity_error_percent"],
    }


def generate(*, solver: str | Path, model: str | Path, output: str | Path) -> Path:
    executable, version_output, solver_hash, version_return_code = guided.verify_solver_version(
        solver
    )
    base_document, source_bytes = guided._read_base_model(model)
    slots = guided.candidate_slots()
    guided.validate_candidate_slots(base_document, slots)
    vectors = [list(vector) for vector in enumerate_vectors(slots)]
    if len(vectors) != 700 or vectors[0] != [0, 0, 0, 0]:
        raise guided.FixtureError(
            "candidate bounds no longer produce the declared 700-vector universe"
        )

    output_path = Path(output).expanduser().resolve()
    if output_path.exists() and output_path.is_dir() and any(output_path.iterdir()):
        raise guided.FixtureError("output directory must be empty")
    output_path.mkdir(parents=True, exist_ok=True)
    scratch = output_path / ".scratch"
    scratch.mkdir()
    runner = SolverRunner(executable)
    outfalls = base_document.object_ids("OUTFALLS")
    storm_documents = {
        storm.storm_id: guided.materialize_storm(base_document, storm) for storm in guided.STORMS
    }
    slot_ids = [slot.slot_id for slot in slots]
    runs_by_vector: dict[tuple[int, ...], list[dict[str, Any]]] = defaultdict(list)
    sequence = 0

    try:
        for vector in vectors:
            kind = "baseline" if vector == [0, 0, 0, 0] else "alternative"
            current_design_id = "baseline" if kind == "baseline" else design_id(vector)
            for storm in guided.STORMS:
                model_bytes = guided.materialize_design(
                    storm_documents[storm.storm_id], slots, vector
                )
                run = guided._execute_one(
                    runner=runner,
                    output=output_path,
                    scratch=scratch,
                    sequence=sequence,
                    kind=kind,
                    design=current_design_id,
                    design_sequence=None,
                    vector=vector,
                    slot_ids=slot_ids,
                    storm=storm,
                    model_bytes=model_bytes,
                    outfall_ids=outfalls,
                    retain=False,
                )
                runs_by_vector[tuple(vector)].append(run)
                sequence += 1
                if sequence % 100 == 0 or sequence == EXPECTED_JOBS:
                    print(
                        f"completed {sequence}/{EXPECTED_JOBS} direct SWMM jobs",
                        file=sys.stderr,
                        flush=True,
                    )
    finally:
        if scratch.exists() and not any(scratch.iterdir()):
            scratch.rmdir()

    if sequence != EXPECTED_JOBS:
        raise guided.FixtureError(f"expected {EXPECTED_JOBS} jobs, completed {sequence}")

    baseline_runs = runs_by_vector[(0, 0, 0, 0)]
    baseline_by_storm = {run["storm_id"]: run for run in baseline_runs}
    baseline_volume = sum(run["metrics"]["outfall_volume_million_gallons"] for run in baseline_runs)
    unit_costs = present_value_unit_costs_thousand_usd()
    rows: list[dict[str, Any]] = []
    for vector in vectors:
        exact_runs = runs_by_vector[tuple(vector)]
        event_volume = sum(run["metrics"]["outfall_volume_million_gallons"] for run in exact_runs)
        reduction = (
            0.0
            if vector == [0, 0, 0, 0]
            else (baseline_volume - event_volume) / baseline_volume * 100
        )
        peak_increases = [
            {
                "storm_id": run["storm_id"],
                "baseline_peak_cfs": baseline_by_storm[run["storm_id"]]["metrics"][
                    "outfall_peak_cfs"
                ],
                "alternative_peak_cfs": run["metrics"]["outfall_peak_cfs"],
            }
            for run in exact_runs
            if run["metrics"]["outfall_peak_cfs"]
            > baseline_by_storm[run["storm_id"]]["metrics"]["outfall_peak_cfs"]
        ]
        cost = sum(
            unit_costs[slot_id] * count for slot_id, count in zip(slot_ids, vector, strict=True)
        )
        qa_pass = all(run["qa"]["status"] == "accepted" for run in exact_runs)
        rows.append(
            {
                "design_id": "baseline" if vector == [0, 0, 0, 0] else design_id(vector),
                "vector": vector,
                "present_value_cost_thousand_usd": round(cost, 6),
                "outfall_volume_reduction_percent": round(reduction, 8),
                "metrics": {
                    "present_value_cost_thousand_usd": round(cost, 6),
                    "outfall_volume_reduction_percent": round(reduction, 8),
                },
                "aggregate_outfall_volume_million_gallons": round(event_volume, 8),
                "peak_guardrail_pass": not peak_increases,
                "qa_pass": qa_pass,
                "feasible": bool(
                    vector != [0, 0, 0, 0]
                    and reduction >= 6.0
                    and cost <= 200.0
                    and not peak_increases
                    and qa_pass
                ),
                "adverse": bool(peak_increases),
                "adverse_peak_changes": peak_increases,
                "runs": [_run_projection(run) for run in exact_runs],
            }
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "title": "Cedar Trace hidden exhaustive direct-SWMM search benchmark",
        "notice": guided.GUIDED_NOTICE
        if hasattr(guided, "GUIDED_NOTICE")
        else (
            "Synthetic teaching model and storms — not for design, permitting, "
            "flood-risk determination, or representation of an existing drainage system."
        ),
        "source_model_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "solver": {
            "required_version": "5.2.4",
            "sha256": solver_hash,
            "version_output": version_output,
            "version_command_return_code": version_return_code,
        },
        "cost_basis_id": GUIDED_COST_BASIS_ID,
        "cost_basis": guided_cost_basis_payload(),
        "candidate_slots": [slot.model_dump(mode="json") for slot in slots],
        "universe_size": len(vectors),
        "objective_directions": {
            "present_value_cost_thousand_usd": "minimize",
            "outfall_volume_reduction_percent": "maximize",
        },
        "storms": [storm.to_manifest() for storm in guided.STORMS],
        "job_accounting": {
            "vectors": len(vectors),
            "baseline_jobs": 4,
            "alternative_jobs": NONBASELINE_DESIGNS * 4,
            "direct_swmm_jobs": sequence,
        },
        "oracle_policy": (
            "Search methods may inspect a row only after requesting its exact design vector; "
            "each request counts as four direct-SWMM-job equivalents."
        ),
        "rows": rows,
    }
    payload["dataset_sha256"] = _canonical_hash(payload)
    destination = output_path / "dataset.json"
    _atomic_json(destination, payload)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", required=True)
    parser.add_argument("--model", default=str(guided.DEFAULT_MODEL))
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(generate(solver=args.solver, model=args.model, output=args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
