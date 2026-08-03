#!/usr/bin/env python3
"""Create the compact, trust-preserving UI projection of the exact demo manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from gi_scenario_core.demo import (
    GUIDED_PROJECT_NAME,
    GUIDED_STORY_ID,
    guided_cost_basis_payload,
    guided_story_definition,
    present_value_unit_costs_thousand_usd,
)

DEFAULT_MANIFEST = ROOT / "benchmarks" / "guided_exact" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "apps" / "desktop" / "src" / "data" / "guidedFixture.json"

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _label(vector: list[int]) -> str:
    active = sum(value > 0 for value in vector)
    if active == 1:
        return "Focused single-practice option"
    if vector[1] >= 3 and vector[2] >= 3:
        return "Street and rain-garden blend"
    if vector[0] >= 3 and vector[2] >= 3:
        return "Bioretention and rain-garden blend"
    if vector[3] >= 5:
        return "Distributed storage blend"
    return "Distributed mixed GI"


def _round_delta(value: float) -> float:
    """Avoid presenting negative zero while retaining fixture precision."""

    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


def _screening_evaluation(
    *,
    reduction_percent: float,
    cost_thousand_usd: float,
    storm_runs: list[dict[str, Any]],
    story: dict[str, Any],
) -> dict[str, Any]:
    rules = {item["rule_id"]: item for item in story["rules"]}
    worst_peak = max(storm_runs, key=lambda item: item["outfall_peak_delta_cfs"])
    all_qa_accepted = all(item["qa_status"] == "accepted" for item in storm_runs)
    values: dict[str, tuple[Any, bool, str]] = {
        "aggregate_volume_reduction": (
            round(reduction_percent, 2),
            reduction_percent >= float(rules["aggregate_volume_reduction"]["threshold"]),
            "percent",
        ),
        "present_value_cost_cap": (
            round(cost_thousand_usd, 2),
            cost_thousand_usd <= float(rules["present_value_cost_cap"]["threshold"]),
            "thousand USD",
        ),
        "no_outfall_peak_increase": (
            _round_delta(float(worst_peak["outfall_peak_delta_cfs"])),
            float(worst_peak["outfall_peak_delta_cfs"])
            <= float(rules["no_outfall_peak_increase"]["threshold"]),
            "cfs",
        ),
        "solver_qa": (
            "accepted" if all_qa_accepted else "not_accepted",
            all_qa_accepted,
            "status",
        ),
    }
    checks = [
        {
            "rule_id": rule["rule_id"],
            "label": rule["label"],
            "operator": rule["operator"],
            "threshold": rule["threshold"],
            "actual": values[rule["rule_id"]][0],
            "units": values[rule["rule_id"]][2],
            "passes": values[rule["rule_id"]][1],
        }
        for rule in story["rules"]
    ]
    return {
        "status": "meets_all" if all(item["passes"] for item in checks) else "does_not_meet",
        "meets_all_rules": all(item["passes"] for item in checks),
        "checks": checks,
        "worst_peak_change": {
            "storm_id": worst_peak["storm_id"],
            "baseline_outfall_peak_cfs": worst_peak["baseline_outfall_peak_cfs"],
            "alternative_outfall_peak_cfs": worst_peak["outfall_peak_cfs"],
            "delta_cfs": worst_peak["outfall_peak_delta_cfs"],
            "delta_percent": worst_peak["outfall_peak_delta_percent"],
        },
    }


def _artifact_availability(storm_runs: list[dict[str, Any]]) -> dict[str, Any]:
    retained_count = sum(bool(item["artifacts"]) for item in storm_runs)
    all_retained = retained_count == len(storm_runs)
    return {
        "exact_metrics_and_hashes": "available",
        "full_run_artifacts": "retained" if all_retained else "not_retained",
        "retained_run_sets": retained_count,
        "expected_run_sets": len(storm_runs),
        "can_open_local_inp_rpt_out": all_retained,
        "message": (
            "All storm-specific INP, RPT, and OUT files are retained locally."
            if all_retained
            else (
                "Exact manifest metrics and hashes are available, but this alternative's "
                "storm-specific INP, RPT, and OUT files were not retained. Use an optional "
                "solver rerun to create new local artifacts."
            )
        ),
    }


def _teaching_claims(candidate: dict[str, Any], role: str) -> list[str]:
    claims = ["Direct SWMM 5.2.4 result under the documented synthetic assumptions."]
    if candidate["nondominated"]:
        claims.append(
            "Nondominated among 220 exact evaluated alternatives for synthetic present-value "
            "cost and four-storm OUT1 volume."
        )
    if candidate["screening"]["meets_all_rules"]:
        claims.extend(
            [
                "Meets the teaching screening rules across the four selected synthetic storm cases.",
                "Candidate to carry forward for site and feasibility review.",
            ]
        )
    elif role == "low_cost_caution":
        claims.append(
            "Does not meet the complete teaching screen; a modeled OUT1 peak increase occurs "
            "in the identified synthetic storm."
        )
    else:
        claims.append("Does not meet every teaching screening rule.")
    return claims


def project(
    manifest: dict[str, Any],
    *,
    source_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    accounting = manifest["job_accounting"]
    runs = manifest["runs"]
    story = guided_story_definition()
    if story["story_id"] != GUIDED_STORY_ID:
        raise ValueError("guided story identifier is inconsistent")
    if manifest.get("project_kind") != "demo":
        raise ValueError("guided fixture project_kind must remain demo")
    if manifest.get("fitness_disposition") != "not_assessed":
        raise ValueError("guided Model Fitness Passport must remain not_assessed")
    if not SHA256_PATTERN.fullmatch(str(manifest.get("extractor", {}).get("script_sha256", ""))):
        raise ValueError("guided fixture must identify its metric extractor by SHA-256")
    if accounting.get("completed_jobs") != accounting.get("expected_jobs") or len(runs) != 884:
        raise ValueError("guided fixture does not contain all 884 jobs")
    if accounting.get("baseline_jobs") != 4 or accounting.get("alternative_jobs") != 880:
        raise ValueError("guided fixture job accounting is inconsistent")
    if any(not run.get("exact") or run["qa"]["status"] != "accepted" for run in runs):
        raise ValueError("every guided UI result must be an accepted exact run")
    run_ids = [run.get("run_id") for run in runs]
    if len(set(run_ids)) != len(run_ids) or any(not value for value in run_ids):
        raise ValueError("guided fixture run IDs must be nonempty and unique")
    required_hashes = ("input_sha256", "output_sha256", "report_sha256", "solver_sha256")
    if any(
        not SHA256_PATTERN.fullmatch(str(run.get("hashes", {}).get(name, "")))
        for run in runs
        for name in required_hashes
    ):
        raise ValueError("every guided run hash must be a lowercase SHA-256 digest")
    solver_hashes = {run["hashes"]["solver_sha256"] for run in runs}
    if not SHA256_PATTERN.fullmatch(str(manifest["solver"]["sha256"])) or solver_hashes != {
        manifest["solver"]["sha256"]
    }:
        raise ValueError("guided run solver hashes do not match the pinned manifest solver")

    storms = [item["storm_id"] for item in manifest["storm_cases"]]
    if len(storms) != 4 or len(set(storms)) != 4:
        raise ValueError("guided fixture must define four unique storm cases")
    baseline_list = [run for run in runs if run["kind"] == "baseline"]
    baseline_runs = {run["storm_id"]: run for run in baseline_list}
    if len(baseline_list) != 4 or set(baseline_runs) != set(storms):
        raise ValueError("guided fixture is missing a baseline storm")
    baseline_volume = sum(
        baseline_runs[storm]["metrics"]["outfall_volume_million_gallons"] for storm in storms
    )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["kind"] == "alternative":
            grouped[run["design_id"]].append(run)
        elif run["kind"] != "baseline":
            raise ValueError(f"unknown guided run kind: {run['kind']!r}")

    if len(grouped) != 220:
        raise ValueError("guided fixture must contain 220 unique alternative design IDs")

    slot_ids = [slot["slot_id"] for slot in manifest["candidate_slots"]]
    pv_by_slot = present_value_unit_costs_thousand_usd()
    if set(pv_by_slot) != set(slot_ids):
        raise ValueError("the versioned guided CostBasis does not match the candidate slots")
    alternatives: list[dict[str, Any]] = []
    for design_id, design_runs in grouped.items():
        if len(design_runs) != len(storms) or {item["storm_id"] for item in design_runs} != set(
            storms
        ):
            raise ValueError(f"design {design_id!r} does not contain one run for every storm")
        vectors = {tuple(item["design_vector"]) for item in design_runs}
        sequences = {item["design_sequence"] for item in design_runs}
        if len(vectors) != 1 or len(sequences) != 1:
            raise ValueError(f"design {design_id!r} changes vector or sequence across storms")
        design_runs.sort(key=lambda item: storms.index(item["storm_id"]))
        first = design_runs[0]
        vector = first["design_vector"]
        if len(vector) != len(slot_ids):
            raise ValueError(f"design {design_id!r} has the wrong vector length")
        volume = sum(run["metrics"]["outfall_volume_million_gallons"] for run in design_runs)
        peak = max(run["metrics"]["outfall_peak_cfs"] for run in design_runs)
        reduction = 100 * (baseline_volume - volume) / baseline_volume
        cost = sum(pv_by_slot[slot] * count for slot, count in zip(slot_ids, vector, strict=True))
        storm_results: list[dict[str, Any]] = []
        for run in design_runs:
            baseline_run = baseline_runs[run["storm_id"]]
            baseline_peak = float(baseline_run["metrics"]["outfall_peak_cfs"])
            alternative_peak = float(run["metrics"]["outfall_peak_cfs"])
            peak_delta = alternative_peak - baseline_peak
            storm_results.append(
                {
                    "run_id": run["run_id"],
                    "storm_id": run["storm_id"],
                    "outfall_volume_million_gallons": run["metrics"][
                        "outfall_volume_million_gallons"
                    ],
                    "outfall_peak_cfs": alternative_peak,
                    "baseline_outfall_peak_cfs": baseline_peak,
                    "outfall_peak_delta_cfs": _round_delta(peak_delta),
                    "outfall_peak_delta_percent": round(100 * peak_delta / baseline_peak, 2),
                    "runoff_continuity_error_percent": run["metrics"][
                        "runoff_continuity_error_percent"
                    ],
                    "flow_continuity_error_percent": run["metrics"][
                        "flow_continuity_error_percent"
                    ],
                    "input_sha256": run["hashes"]["input_sha256"],
                    "output_sha256": run["hashes"]["output_sha256"],
                    "report_sha256": run["hashes"]["report_sha256"],
                    "qa_status": run["qa"]["status"],
                    "artifacts": run["artifacts"] if run["artifacts_retained"] else {},
                }
            )
        alternatives.append(
            {
                "id": f"ALT-{first['design_sequence'] + 1:03d}",
                "design_id": design_id,
                "sequence": first["design_sequence"],
                "name": _label(vector),
                "vector": vector,
                "additional_units": dict(zip(slot_ids, vector, strict=True)),
                "present_value_cost_thousand_usd": round(cost, 2),
                "four_storm_outfall_volume_million_gallons": round(volume, 3),
                "outfall_volume_reduction_percent": round(reduction, 2),
                "maximum_outfall_peak_cfs": round(peak, 2),
                "_comparison_cost_thousand_usd": cost,
                "_comparison_volume_million_gallons": volume,
                "_comparison_reduction_percent": reduction,
                "retained_artifacts": all(run["artifacts_retained"] for run in design_runs),
                "storm_runs": storm_results,
            }
        )

    for candidate in alternatives:
        candidate["nondominated"] = not any(
            other["_comparison_cost_thousand_usd"] <= candidate["_comparison_cost_thousand_usd"]
            and other["_comparison_volume_million_gallons"]
            <= candidate["_comparison_volume_million_gallons"]
            and (
                other["_comparison_cost_thousand_usd"] < candidate["_comparison_cost_thousand_usd"]
                or other["_comparison_volume_million_gallons"]
                < candidate["_comparison_volume_million_gallons"]
            )
            for other in alternatives
        )
        candidate["screening"] = _screening_evaluation(
            reduction_percent=candidate["_comparison_reduction_percent"],
            cost_thousand_usd=candidate["_comparison_cost_thousand_usd"],
            storm_runs=candidate["storm_runs"],
            story=story,
        )
        candidate["artifact_availability"] = _artifact_availability(candidate["storm_runs"])
    alternatives.sort(key=lambda item: item["sequence"])
    if {item["sequence"] for item in alternatives} != set(range(220)):
        raise ValueError("guided design sequences must be unique and contiguous from 0 through 219")
    if len({tuple(item["vector"]) for item in alternatives}) != 220:
        raise ValueError("guided alternative vectors must be unique")

    retained = [item for item in alternatives if item["retained_artifacts"]]
    if len(retained) != len(story["teaching_alternatives"]):
        raise ValueError("guided UI expects one retained design for each teaching alternative")
    shortlist_sequences = list(manifest["search"]["shortlist_design_sequences"])
    if set(shortlist_sequences) != {item["sequence"] for item in retained}:
        raise ValueError("guided UI shortlist must exactly match the manifest's retained shortlist")

    alternatives_by_id = {item["id"]: item for item in alternatives}
    teaching_alternatives: list[dict[str, Any]] = []
    for teaching_spec in story["teaching_alternatives"]:
        alternative_id = teaching_spec["alternative_id"]
        candidate = alternatives_by_id.get(alternative_id)
        if candidate is None:
            raise ValueError(f"teaching alternative {alternative_id!r} is absent from the manifest")
        if candidate["sequence"] != teaching_spec["design_sequence"]:
            raise ValueError(f"teaching alternative {alternative_id!r} changed sequence")
        if candidate["vector"] != teaching_spec["expected_vector"]:
            raise ValueError(f"teaching alternative {alternative_id!r} changed design vector")
        if not candidate["nondominated"]:
            raise ValueError(f"teaching alternative {alternative_id!r} is no longer nondominated")
        projected = dict(candidate)
        projected.update(
            {
                "teaching_role": teaching_spec["role"],
                "teaching_label": teaching_spec["label"],
                "why_in_story": teaching_spec["why"],
                "claims": _teaching_claims(candidate, teaching_spec["role"]),
            }
        )
        teaching_alternatives.append(projected)

    teaching_by_role = {item["teaching_role"]: item for item in teaching_alternatives}
    if teaching_by_role["carry_forward_candidate"]["screening"]["status"] != "meets_all":
        raise ValueError("the declared carry-forward teaching candidate no longer meets every rule")
    caution_checks = {
        item["rule_id"]: item["passes"]
        for item in teaching_by_role["low_cost_caution"]["screening"]["checks"]
    }
    if caution_checks["aggregate_volume_reduction"] or caution_checks["no_outfall_peak_increase"]:
        raise ValueError(
            "the low-cost teaching caution no longer demonstrates its declared failures"
        )
    for role in ("stronger_over_budget", "observed_performance_extreme"):
        checks = {
            item["rule_id"]: item["passes"]
            for item in teaching_by_role[role]["screening"]["checks"]
        }
        if checks["present_value_cost_cap"]:
            raise ValueError(f"the {role} teaching alternative is no longer over the cost cap")

    best_observed_reduction = max(item["_comparison_reduction_percent"] for item in alternatives)
    if (
        teaching_by_role["observed_performance_extreme"]["_comparison_reduction_percent"]
        != best_observed_reduction
    ):
        raise ValueError(
            "the declared performance extreme is no longer the best observed reduction"
        )

    accepted_count = sum(run["qa"]["status"] == "accepted" for run in runs)
    replay_metadata = {
        "mode_id": "verified_replay",
        "label": "Replay verified exact-result records",
        "availability": "available_offline",
        "executes_solver": False,
        "source": "benchmarks/guided_exact/manifest.json",
        "exact_run_records": len(runs),
        "qa_accepted_records": accepted_count,
        "retained_full_artifact_run_sets": accounting["retained_jobs"],
        "integrity": (
            "Each replayed run records input, report, output, and pinned-solver SHA-256 hashes."
        ),
        "limitations": [
            "Replay reads previously generated exact result records; it does not execute SWMM.",
            (
                "Only baseline runs and the four declared teaching alternatives have local "
                "INP, RPT, and OUT files; the other exact records retain metrics and hashes only."
            ),
        ],
    }
    rerun_metadata = {
        "mode_id": "optional_rerun",
        "label": "Run SWMM again",
        "availability": "conditional_on_configured_pinned_solver",
        "executes_solver": True,
        "runtime_status": "not_assessed_by_static_fixture",
        "required_solver": {
            "version": manifest["solver"]["required_version"],
            "sha256": manifest["solver"]["sha256"],
            "environment": manifest["solver"]["environment"],
        },
        "requirements": [
            "A host-configured SWMM 5.2.4 executable matching the recorded SHA-256.",
            "New isolated run directories and freshly materialized storm/design inputs.",
            "A differential comparison using the fixture's metric-specific QA tolerances.",
        ],
        "result_treatment": (
            "A rerun creates new evidence and must be compared with the replay record; it does "
            "not retroactively make missing retained files available."
        ),
    }

    projection_limitations = list(manifest["limitations"])
    projection_limitations.extend(
        [
            (
                "The teaching rule matrix evaluates aggregate OUT1 event volume, OUT1 peak flow "
                "for each selected storm, synthetic cost, and recorded run QA only."
            ),
            (
                "The fixture does not support claims about node HGL, surcharge duration, link "
                "capacity, a complete hydrograph, or full-network adverse impacts."
            ),
            "Displayed rounded values are explanatory; screening is evaluated from manifest values.",
        ]
    )

    internal_comparison_keys = (
        "_comparison_cost_thousand_usd",
        "_comparison_volume_million_gallons",
        "_comparison_reduction_percent",
    )
    for candidate in [*alternatives, *teaching_alternatives]:
        for key in internal_comparison_keys:
            candidate.pop(key, None)

    projection = {
        "schema_version": "2.0",
        "generated_from": "benchmarks/guided_exact/manifest.json",
        "fixture_id": manifest["fixture_id"],
        "project_name": GUIDED_PROJECT_NAME,
        "story_id": GUIDED_STORY_ID,
        "project_kind": manifest["project_kind"],
        "fitness_disposition": manifest["fitness_disposition"],
        "claim": manifest["claim"],
        "notice": manifest["notice"],
        "limitations": projection_limitations,
        "solver": {
            "version": manifest["solver"]["required_version"],
            "sha256": manifest["solver"]["sha256"],
        },
        "extractor": manifest["extractor"],
        "source_model_sha256": manifest["source_model"]["sha256"],
        "job_accounting": accounting,
        "storms": manifest["storm_cases"],
        "candidate_slots": manifest["candidate_slots"],
        "cost_basis": guided_cost_basis_payload(),
        "present_value_unit_costs_thousand_usd": {
            key: round(value, 4) for key, value in pv_by_slot.items()
        },
        "guided_story": {
            **story,
            "execution_modes": {
                "default": "verified_replay",
                "verified_replay": replay_metadata,
                "optional_rerun": rerun_metadata,
            },
        },
        "baseline": {
            "four_storm_outfall_volume_million_gallons": round(baseline_volume, 3),
            "runs": [baseline_runs[storm] for storm in storms],
            "artifact_availability": _artifact_availability(
                [
                    {
                        "artifacts": baseline_runs[storm]["artifacts"]
                        if baseline_runs[storm]["artifacts_retained"]
                        else {}
                    }
                    for storm in storms
                ]
            ),
        },
        "alternatives": alternatives,
        "teaching_alternatives": teaching_alternatives,
        "teaching_alternative_ids": [item["id"] for item in teaching_alternatives],
        "retained_shortlist_sequences": shortlist_sequences,
    }
    if source_manifest_sha256 is not None:
        if not SHA256_PATTERN.fullmatch(source_manifest_sha256):
            raise ValueError("source manifest digest must be a lowercase SHA-256")
        projection["source_manifest_sha256"] = source_manifest_sha256
    return projection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    manifest_bytes = arguments.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    payload = project(
        manifest,
        source_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    if not all(
        math.isfinite(item["present_value_cost_thousand_usd"]) for item in payload["alternatives"]
    ):
        raise ValueError("non-finite UI cost")
    _atomic_json(arguments.output.resolve(), payload)
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
