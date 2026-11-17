"""Project exact GI evidence into deterministic Canvas-film data."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from gi_scenario_core.orchestrator import (
    RunoffQuantityLedger,
    extract_runoff_quantity_ledger,
)

GUIDED = ROOT / "apps/desktop/src/data/guidedFixture.json"
EXACT = ROOT / "benchmarks/exhaustive_exact/dataset.json"
BASELINE_RPT = ROOT / "benchmarks/guided_exact/runs/run_ec6a03d02287f847394c/report.rpt"
ALT207_RPT = ROOT / "benchmarks/guided_exact/runs/run_c8220fe9c2697d04c9e4/report.rpt"
TABLES = ROOT / "dist/benchmark-publication/tables"
AGGREGATES = TABLES / "campaign_aggregates.csv"
RELEASE_GATES = TABLES / "release_gate.csv"
EXACT_ALTERNATIVES = TABLES / "exact_alternatives.csv"
STORM_RESULTS = TABLES / "storm_results.csv"
FILMS = PACKAGE / "story/films.json"
OUTPUT_JSON = PACKAGE / "data/animation-data.json"
OUTPUT_JS = PACKAGE / "data/animation-data.js"


class AnimationDataError(ValueError):
    """Raised when checked-in evidence no longer supports the approved films."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AnimationDataError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"{_relative(path)} must contain an object")
    return payload


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _bool(value: str) -> bool:
    _require(value in {"True", "False"}, f"invalid boolean cell: {value!r}")
    return value == "True"


def _ledger_payload(ledger: RunoffQuantityLedger) -> dict[str, float]:
    lid = ledger.lid_drainage.primary.value if ledger.lid_drainage else 0.0
    return {
        "initial_lid_storage": ledger.initial_lid_storage.primary.value,
        "precipitation": ledger.total_precipitation.primary.value,
        "evaporation_loss": ledger.evaporation_loss.primary.value,
        "infiltration_loss": ledger.infiltration_loss.primary.value,
        "surface_runoff": ledger.surface_runoff.primary.value,
        "lid_drainage": lid,
        "final_storage": ledger.final_storage.primary.value,
        "runoff_continuity_error_percent": ledger.runoff_continuity_error.value,
        "rounded_balance_residual": ledger.rounded_runoff_balance_residual().value,
        "wet_weather_inflow": ledger.wet_weather_inflow.secondary.value,
        "external_outflow": ledger.external_outflow.secondary.value,
        "routing_continuity_error_percent": ledger.routing_continuity_error.value,
    }


def _alternative(fixture: dict[str, Any], alternative_id: str) -> dict[str, Any]:
    matches = [item for item in fixture["alternatives"] if item["id"] == alternative_id]
    _require(len(matches) == 1, f"expected one {alternative_id} record")
    return matches[0]


def _alt017_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    record = _alternative(fixture, "ALT-017")
    checks = record["screening"]["checks"]
    _require(
        [item["passes"] for item in checks] == [False, True, False, True],
        "ALT-017 rule results drifted",
    )
    display = [
        (f"{float(checks[0]['actual']):.2f}%", "required ≥ 6.00%"),
        (f"${float(checks[1]['actual']):.2f}k", "required ≤ $200k"),
        (f"+{float(checks[2]['actual']):.2f} cfs", "required ≤ 0.00 cfs"),
        ("4 / 4 accepted", "all direct runs"),
    ]
    rules = [
        {
            "id": check["rule_id"],
            "passes": check["passes"],
            "display_actual": display[index][0],
            "display_threshold": display[index][1],
        }
        for index, check in enumerate(checks)
    ]
    labels = {
        "synthetic_long_2_5in": "Long event · 2.5 in",
        "synthetic_stress_4in": "Stress event · 4.0 in",
    }
    peak_increases = [
        {
            "storm_id": run["storm_id"],
            "short_label": labels[run["storm_id"]],
            "baseline_peak": float(run["baseline_outfall_peak_cfs"]),
            "alternative_peak": float(run["outfall_peak_cfs"]),
            "delta": float(run["outfall_peak_delta_cfs"]),
        }
        for run in record["storm_runs"]
        if float(run["outfall_peak_delta_cfs"]) > 0
    ]
    _require(
        [(item["baseline_peak"], item["alternative_peak"]) for item in peak_increases]
        == [(3.14, 3.21), (13.33, 13.34)],
        "ALT-017 peak increases drifted",
    )
    return {
        "id": record["id"],
        "cost": float(record["present_value_cost_thousand_usd"]),
        "reduction": float(record["outfall_volume_reduction_percent"]),
        "nondominated": bool(record["nondominated"]),
        "rules": rules,
        "peak_increases": peak_increases,
    }


def _guided_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    points = [
        {
            "id": item["id"],
            "cost": float(item["present_value_cost_thousand_usd"]),
            "reduction": float(item["outfall_volume_reduction_percent"]),
            "nondominated": bool(item["nondominated"]),
        }
        for item in fixture["alternatives"]
    ]
    _require(len(points) == 220, "guided fixture must contain exactly 220 alternatives")
    return {
        "points": points,
        "plot_bounds": {
            "max_cost": math.ceil(max(item["cost"] for item in points) / 25) * 25,
            "max_reduction": math.ceil(max(item["reduction"] for item in points)),
        },
        "alt017": _alt017_payload(fixture),
    }


def _storm_payload(exact: dict[str, Any]) -> dict[str, Any]:
    rows = _csv(STORM_RESULTS)
    adverse_by_storm: Counter[str] = Counter()
    adverse_storms_by_design: defaultdict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["design_id"] == "baseline" or float(row["peak_delta_cfs"]) <= 0:
            continue
        adverse_by_storm[row["storm_id"]] += 1
        adverse_storms_by_design[row["design_id"]].add(row["storm_id"])
    order = [item["storm_id"] for item in exact["storms"]]
    expected = [1, 0, 420, 47]
    observed = [adverse_by_storm[item] for item in order]
    _require(observed == expected, f"storm adverse counts drifted: {observed}")
    _require(len(adverse_storms_by_design) == 421, "unique adverse count drifted")
    dual = sum(1 for values in adverse_storms_by_design.values() if len(values) == 2)
    _require(dual == 47, "dual-storm adverse count drifted")
    labels = [
        ("Balanced 1 in", "BALANCED"),
        ("Front-loaded 2 in", "FRONT"),
        ("Long 2.5 in", "LONG"),
        ("Stress 4 in", "STRESS"),
    ]
    items = []
    for index, storm in enumerate(exact["storms"]):
        items.append(
            {
                "storm_id": storm["storm_id"],
                "short_label": labels[index][0],
                "tiny_label": labels[index][1],
                "depth_inches": float(storm["depth_inches"]),
                "duration_hours": float(storm["duration_hours"]),
                "intensities": [float(item) for item in storm["intensities_inches_per_hour"]],
                "adverse_count": observed[index],
            }
        )
    return {
        "items": items,
        "unique_adverse": 421,
        "dual_storm_adverse": 47,
        "storm_design_flags": sum(observed),
    }


def _decision_payload() -> dict[str, Any]:
    rows = []
    for row in _csv(EXACT_ALTERNATIVES):
        if _bool(row["is_baseline"]):
            continue
        reduction = float(row["volume_reduction_percent"])
        cost = float(row["cost_thousand_usd"])
        rows.append(
            {
                "id": row["design_id"],
                "volume_pass": reduction >= 6.0,
                "volume_cost_pass": reduction >= 6.0 and cost <= 200.0,
                "feasible": _bool(row["teaching_rule_feasible"]),
                "frontier": _bool(row["exact_feasible_frontier"]),
            }
        )
    counts = {
        "alternatives": len(rows),
        "volume_pass": sum(item["volume_pass"] for item in rows),
        "volume_cost_pass": sum(item["volume_cost_pass"] for item in rows),
        "feasible": sum(item["feasible"] for item in rows),
        "frontier": sum(item["frontier"] for item in rows),
    }
    _require(
        counts
        == {
            "alternatives": 699,
            "volume_pass": 297,
            "volume_cost_pass": 46,
            "feasible": 12,
            "frontier": 4,
        },
        f"decision funnel drifted: {counts}",
    )
    return {"rows": rows, "counts": counts, "qa_accepted": 699}


def _search_payload() -> dict[str, Any]:
    aggregates = _csv(AGGREGATES)
    matches = [
        row
        for row in aggregates
        if row["method"] == "sobol_extratrees" and int(row["design_budget"]) == 256
    ]
    _require(len(matches) == 1, "expected one maximum-budget ExtraTrees aggregate")
    record = matches[0]
    gates = _csv(RELEASE_GATES)
    passed = sum(_bool(row["passed"]) for row in gates)
    _require(len(gates) == 5 and passed == 1, "release gate tally drifted")
    required = math.ceil(421 * 0.95)
    _require(required == 400, "95% adverse request arithmetic drifted")
    return {
        "methods": ["Random", "Scrambled Sobol", "Integer NSGA-II", "Sobol + ExtraTrees"],
        "budgets": [64, 128, 256],
        "storm_labels": ["Balanced", "Front-loaded", "Long", "Stress"],
        "campaign_replays": 120,
        "extratrees": {
            "hypervolume": float(record["hypervolume_ratio_median"]),
            "feasible_recall": float(record["feasible_recall_median"]),
            "adverse_recall": float(record["adverse_recall_median"]),
        },
        "gates": [
            {
                "criterion": row["criterion"],
                "observed": row["observed"],
                "passed": _bool(row["passed"]),
            }
            for row in gates
        ],
        "gate_pass_count": passed,
        "gate_fail_count": len(gates) - passed,
        "adverse_count": 421,
        "adverse_recall_required_requests": required,
        "maximum_request_budget": 256,
        "theoretical_recall_ceiling": 256 / 421,
        "energy_kwh": None,
        "carbon_co2e": None,
    }


def build_payload() -> dict[str, Any]:
    sources = [
        GUIDED,
        EXACT,
        BASELINE_RPT,
        ALT207_RPT,
        AGGREGATES,
        RELEASE_GATES,
        EXACT_ALTERNATIVES,
        STORM_RESULTS,
        FILMS,
    ]
    for path in sources:
        _require(path.is_file(), f"required animation source is missing: {_relative(path)}")
    fixture = _json(GUIDED)
    exact = _json(EXACT)
    films = _json(FILMS)
    baseline_ledger = extract_runoff_quantity_ledger(BASELINE_RPT.read_text(encoding="utf-8"))
    alternative_ledger = extract_runoff_quantity_ledger(ALT207_RPT.read_text(encoding="utf-8"))
    baseline = _ledger_payload(baseline_ledger)
    alternative = _ledger_payload(alternative_ledger)
    _require(
        baseline["precipitation"] == alternative["precipitation"] == 0.937,
        "long-event precipitation drifted",
    )
    _require(baseline["external_outflow"] == 0.167, "baseline long-event outflow drifted")
    _require(alternative["external_outflow"] == 0.150, "ALT-207 long-event outflow drifted")
    alt207 = _alternative(fixture, "ALT-207")
    four_event = {
        "baseline": float(fixture["baseline"]["four_storm_outfall_volume_million_gallons"]),
        "alternative": float(alt207["four_storm_outfall_volume_million_gallons"]),
    }
    _require(four_event == {"baseline": 0.748, "alternative": 0.7}, "four-event volumes drifted")
    payload = {
        "schema_version": "1.0",
        "width": films["width"],
        "height": films["height"],
        "fps": films["fps"],
        "persistent_notice": films["persistent_notice"],
        "films": films["films"],
        "source_hashes": {_relative(path): _sha256(path) for path in sources},
        "guided": _guided_payload(fixture),
        "water_ledger": {
            "units": {"runoff_primary": "acre-ft", "routing_secondary": "Mgal"},
            "baseline": baseline,
            "alternative": alternative,
            "four_event": four_event,
            "modeled_gallon_difference": round(
                (four_event["baseline"] - four_event["alternative"]) * 1_000_000
            ),
            "infiltration_language": "SWMM runoff-accounting term; not measured groundwater recharge",
        },
        "storms": _storm_payload(exact),
        "decision_funnel": _decision_payload(),
        "search": _search_payload(),
        "forbidden_claims": [
            "annual runoff reduction",
            "avoided flooding or damages",
            "pollutant removal or cleaner water",
            "groundwater recharge",
            "habitat, biodiversity, or heat benefit",
            "real return on investment",
            "energy or carbon savings",
            "calibration or design readiness",
        ],
    }
    return payload


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def write_payload(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _write_atomic(OUTPUT_JSON, serialized)
    compact = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    _write_atomic(
        OUTPUT_JS,
        "// Generated by scripts/build_animation_data.py; do not edit.\n"
        f"window.GI_ANIMATION_DATA = Object.freeze({compact});\n",
    )


def main() -> None:
    payload = build_payload()
    write_payload(payload)
    print(
        json.dumps(
            {
                "animation_data": _relative(OUTPUT_JSON),
                "sha256": _sha256(OUTPUT_JSON),
                "films": len(payload["films"]),
            }
        )
    )


if __name__ == "__main__":
    main()
