#!/usr/bin/env python3
"""Create the compact, trust-preserving UI projection of the exact demo manifest."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "guided_exact" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "apps" / "desktop" / "src" / "data" / "guidedFixture.json"

COST_BASIS = {
    "currency": "synthetic 2026 USD",
    "source": "Self-authored CC0 teaching assumptions; not a bid, estimate, or market source",
    "analysis_horizon_years": 30,
    "real_discount_rate_percent": 3.0,
    "unit_costs_thousand_usd": {
        "S1_BIO": {"capex": 20.0, "annual_om": 0.6, "replacement_year": None, "replacement": 0.0},
        "S1_PP": {"capex": 28.0, "annual_om": 0.45, "replacement_year": None, "replacement": 0.0},
        "S2_RG": {"capex": 15.0, "annual_om": 0.5, "replacement_year": None, "replacement": 0.0},
        "S2_RB": {"capex": 2.5, "annual_om": 0.08, "replacement_year": 15, "replacement": 1.5},
    },
}

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _present_value_unit(cost: dict[str, Any]) -> float:
    horizon = int(COST_BASIS["analysis_horizon_years"])
    rate = float(COST_BASIS["real_discount_rate_percent"]) / 100
    annuity = ((1 + rate) ** horizon - 1) / (rate * (1 + rate) ** horizon)
    replacement = 0.0
    if cost["replacement_year"]:
        replacement = float(cost["replacement"]) / (1 + rate) ** int(cost["replacement_year"])
    return float(cost["capex"]) + float(cost["annual_om"]) * annuity + replacement


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


def project(manifest: dict[str, Any]) -> dict[str, Any]:
    accounting = manifest["job_accounting"]
    runs = manifest["runs"]
    if manifest.get("readiness") != "DEMO":
        raise ValueError("guided fixture must remain DEMO")
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
    pv_by_slot = {
        slot_id: _present_value_unit(COST_BASIS["unit_costs_thousand_usd"][slot_id])
        for slot_id in slot_ids
    }
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
                "retained_artifacts": all(run["artifacts_retained"] for run in design_runs),
                "storm_runs": [
                    {
                        "run_id": run["run_id"],
                        "storm_id": run["storm_id"],
                        "outfall_volume_million_gallons": run["metrics"][
                            "outfall_volume_million_gallons"
                        ],
                        "outfall_peak_cfs": run["metrics"]["outfall_peak_cfs"],
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
                    for run in design_runs
                ],
            }
        )

    for candidate in alternatives:
        candidate["nondominated"] = not any(
            other["present_value_cost_thousand_usd"] <= candidate["present_value_cost_thousand_usd"]
            and other["four_storm_outfall_volume_million_gallons"]
            <= candidate["four_storm_outfall_volume_million_gallons"]
            and (
                other["present_value_cost_thousand_usd"]
                < candidate["present_value_cost_thousand_usd"]
                or other["four_storm_outfall_volume_million_gallons"]
                < candidate["four_storm_outfall_volume_million_gallons"]
            )
            for other in alternatives
        )
    alternatives.sort(key=lambda item: item["sequence"])
    if {item["sequence"] for item in alternatives} != set(range(220)):
        raise ValueError("guided design sequences must be unique and contiguous from 0 through 219")
    if len({tuple(item["vector"]) for item in alternatives}) != 220:
        raise ValueError("guided alternative vectors must be unique")

    retained = [item for item in alternatives if item["retained_artifacts"]]
    if len(retained) != 8:
        raise ValueError("guided UI expects eight result-independent retained alternatives")
    shortlist_sequences = list(manifest["search"]["shortlist_design_sequences"])
    if set(shortlist_sequences) != {item["sequence"] for item in retained}:
        raise ValueError("guided UI shortlist must exactly match the manifest's retained shortlist")

    return {
        "schema_version": "1.0",
        "generated_from": "benchmarks/guided_exact/manifest.json",
        "fixture_id": manifest["fixture_id"],
        "readiness": manifest["readiness"],
        "claim": manifest["claim"],
        "notice": manifest["notice"],
        "limitations": manifest["limitations"],
        "solver": {
            "version": manifest["solver"]["required_version"],
            "sha256": manifest["solver"]["sha256"],
        },
        "extractor": manifest["extractor"],
        "source_model_sha256": manifest["source_model"]["sha256"],
        "job_accounting": accounting,
        "storms": manifest["storm_cases"],
        "candidate_slots": manifest["candidate_slots"],
        "cost_basis": COST_BASIS,
        "present_value_unit_costs_thousand_usd": {
            key: round(value, 4) for key, value in pv_by_slot.items()
        },
        "baseline": {
            "four_storm_outfall_volume_million_gallons": round(baseline_volume, 3),
            "runs": [baseline_runs[storm] for storm in storms],
        },
        "alternatives": alternatives,
        "retained_shortlist_sequences": shortlist_sequences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    payload = project(manifest)
    if not all(
        math.isfinite(item["present_value_cost_thousand_usd"]) for item in payload["alternatives"]
    ):
        raise ValueError("non-finite UI cost")
    _atomic_json(arguments.output.resolve(), payload)
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
