"""Authoritative claim projection for the GI Scenario Studio film.

The film deliberately reads the same checked-in JSON evidence used by the
guided application and the exhaustive search benchmark.  Nothing in this
module recomputes hydraulics or invents display values; it only validates and
normalizes source records for scene code.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GUIDED_FIXTURE = Path("apps/desktop/src/data/guidedFixture.json")
EXHAUSTIVE_DATASET = Path("benchmarks/exhaustive_exact/dataset.json")
STORY_MANIFEST = Path(__file__).resolve().parents[2] / "story" / "story-manifest.json"
TEACHING_ALTERNATIVE_IDS = ("ALT-017", "ALT-207", "ALT-109", "ALT-014")


class ClaimIntegrityError(ValueError):
    """Raised when checked-in evidence no longer supports the approved film."""


@dataclass(frozen=True, slots=True)
class StormRun:
    """One teaching alternative under one matching baseline storm."""

    storm_id: str
    name: str
    baseline_peak: float
    alternative_peak: float
    delta: float
    volume: float
    qa_status: str
    run_id: str
    input_sha256: str | None
    report_sha256: str | None
    output_sha256: str | None


@dataclass(frozen=True, slots=True)
class StormClaim:
    """Synthetic storm definition paired with its untouched baseline result."""

    storm_id: str
    name: str
    depth_inches: float
    duration_hours: float
    authority_status: str
    baseline_peak: float
    baseline_volume: float


@dataclass(frozen=True, slots=True)
class AlternativePlotPoint:
    """Exact aggregate coordinates for one guided alternative."""

    id: str
    cost: float
    volume: float
    reduction: float
    nondominated: bool
    meets_all_rules: bool


@dataclass(frozen=True, slots=True)
class DecisionRuleClaim:
    """One declared screening rule from the fixture decision brief."""

    rule_id: str
    label: str
    operator: str
    threshold: float | str
    units: str


@dataclass(frozen=True, slots=True)
class AlternativeClaim:
    """One artifact-retained teaching alternative."""

    id: str
    vector: tuple[int, ...]
    cost: float
    volume: float
    reduction: float
    nondominated: bool
    meets_all_rules: bool
    storm_runs: tuple[StormRun, ...]

    @property
    def peak_increases(self) -> tuple[StormRun, ...]:
        """Storm runs whose peak exceeds the matching untouched baseline."""

        return tuple(run for run in self.storm_runs if run.delta > 0.0)


@dataclass(frozen=True, slots=True)
class FilmClaims:
    """Validated values scenes may display as factual claims."""

    fixture_id: str
    solver_version: str
    solver_sha256: str
    alternatives: tuple[AlternativeClaim, ...]
    guided_points: tuple[AlternativePlotPoint, ...]
    teaching_by_id: dict[str, AlternativeClaim]
    storms: tuple[StormClaim, ...]
    decision_rules: tuple[DecisionRuleClaim, ...]
    fitness_disposition: str
    baseline_volume: float
    guided_alternatives: int
    guided_jobs: int
    storm_count: int
    retained_jobs: int
    universe_vectors: int
    nonbaseline_vectors: int
    exhaustive_jobs: int
    candidate_choice_counts: tuple[int, ...]
    largest_baseline_continuity_error: float
    qa_warning_threshold: float
    cost_currency: str
    cost_horizon_years: int
    cost_real_discount_rate_percent: float
    guided_fixture_path: Path
    exhaustive_dataset_path: Path
    allowed_language: tuple[str, ...]
    avoided_language: tuple[str, ...]

    @property
    def alt017_peak_increases(self) -> tuple[StormRun, ...]:
        return self.teaching_by_id["ALT-017"].peak_increases


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ClaimIntegrityError(message)


def _repository_root(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [Path(explicit).expanduser().resolve()]
    else:
        source = Path(__file__).resolve()
        candidates = [source.parent, *source.parents]
    for candidate in candidates:
        if (candidate / GUIDED_FIXTURE).is_file() and (candidate / EXHAUSTIVE_DATASET).is_file():
            return candidate
    raise ClaimIntegrityError(
        "Could not locate the repository containing guidedFixture.json and the exhaustive dataset"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClaimIntegrityError(f"Could not read claim source {path}: {error}") from error
    _require(isinstance(payload, dict), f"Claim source {path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_pinned_sources(guided_path: Path, exhaustive_path: Path) -> None:
    manifest = _read_json(STORY_MANIFEST)
    sources = manifest.get("claim_sources")
    _require(isinstance(sources, dict), "Story manifest must pin the film claim sources")
    expected = {
        "guided_fixture": (GUIDED_FIXTURE, guided_path),
        "exhaustive_benchmark": (EXHAUSTIVE_DATASET, exhaustive_path),
    }
    for source_id, (relative_path, source_path) in expected.items():
        record = sources.get(source_id)
        _require(isinstance(record, dict), f"Story manifest is missing claim source {source_id}")
        _require(
            record.get("path") == relative_path.as_posix(),
            f"Story manifest path changed for {source_id}",
        )
        pinned = _as_sha256(record.get("sha256"), f"{source_id} pinned hash")
        _require(pinned is not None, f"Story manifest must pin {source_id}")
        _require(
            _sha256(source_path) == pinned,
            f"Claim source {relative_path} drifted; review the film before updating its hash",
        )


def _as_float(value: object, label: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} is not numeric"
    )
    return float(value)


def _as_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    _require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{label} is not a lowercase SHA-256 digest",
    )
    return value


def _baseline_runs_by_storm(fixture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    runs = fixture.get("baseline", {}).get("runs", [])
    _require(isinstance(runs, list), "Guided baseline runs must be a list")
    by_storm = {str(run["storm_id"]): run for run in runs}
    _require(len(by_storm) == 4, "The guided fixture must contain four unique baseline storms")
    return by_storm


def _storm_claims(
    fixture: dict[str, Any], baseline_runs: dict[str, dict[str, Any]]
) -> tuple[StormClaim, ...]:
    claims: list[StormClaim] = []
    for storm in fixture.get("storms", []):
        storm_id = str(storm["storm_id"])
        _require(storm_id in baseline_runs, f"Storm {storm_id} has no matching baseline run")
        metrics = baseline_runs[storm_id].get("metrics", {})
        claims.append(
            StormClaim(
                storm_id=storm_id,
                name=str(storm["name"]),
                depth_inches=_as_float(storm["depth_inches"], f"{storm_id} depth"),
                duration_hours=_as_float(storm["duration_hours"], f"{storm_id} duration"),
                authority_status=str(storm["authority_status"]),
                baseline_peak=_as_float(metrics.get("outfall_peak_cfs"), f"{storm_id} peak"),
                baseline_volume=_as_float(
                    metrics.get("outfall_volume_million_gallons"), f"{storm_id} volume"
                ),
            )
        )
    return tuple(claims)


def _alternative_claim(raw: dict[str, Any], storm_names: dict[str, str]) -> AlternativeClaim:
    identifier = str(raw["id"])
    storm_runs: list[StormRun] = []
    for run in raw.get("storm_runs", []):
        storm_id = str(run["storm_id"])
        _require(storm_id in storm_names, f"{identifier} references unknown storm {storm_id}")
        storm_runs.append(
            StormRun(
                storm_id=storm_id,
                name=storm_names[storm_id],
                baseline_peak=_as_float(
                    run["baseline_outfall_peak_cfs"], f"{identifier}/{storm_id} baseline peak"
                ),
                alternative_peak=_as_float(
                    run["outfall_peak_cfs"], f"{identifier}/{storm_id} alternative peak"
                ),
                delta=_as_float(run["outfall_peak_delta_cfs"], f"{identifier}/{storm_id} delta"),
                volume=_as_float(
                    run["outfall_volume_million_gallons"], f"{identifier}/{storm_id} volume"
                ),
                qa_status=str(run["qa_status"]),
                run_id=str(run["run_id"]),
                input_sha256=_as_sha256(
                    run.get("input_sha256"), f"{identifier}/{storm_id} input hash"
                ),
                report_sha256=_as_sha256(
                    run.get("report_sha256"), f"{identifier}/{storm_id} report hash"
                ),
                output_sha256=_as_sha256(
                    run.get("output_sha256"), f"{identifier}/{storm_id} output hash"
                ),
            )
        )
    return AlternativeClaim(
        id=identifier,
        vector=tuple(int(value) for value in raw["vector"]),
        cost=_as_float(raw["present_value_cost_thousand_usd"], f"{identifier} cost"),
        volume=_as_float(raw["four_storm_outfall_volume_million_gallons"], f"{identifier} volume"),
        reduction=_as_float(raw["outfall_volume_reduction_percent"], f"{identifier} reduction"),
        nondominated=bool(raw["nondominated"]),
        meets_all_rules=bool(raw["screening"]["meets_all_rules"]),
        storm_runs=tuple(storm_runs),
    )


def _validate_alt017(claim: AlternativeClaim) -> None:
    increases = claim.peak_increases
    _require(len(increases) == 2, "ALT-017 must retain both recorded peak increases")
    by_storm = {run.storm_id: run for run in increases}
    expected = {
        "synthetic_long_2_5in": (3.14, 3.21, 0.07),
        "synthetic_stress_4in": (13.33, 13.34, 0.01),
    }
    _require(set(by_storm) == set(expected), "ALT-017 peak-increase storm set changed")
    for storm_id, values in expected.items():
        observed = by_storm[storm_id]
        for actual, target, label in zip(
            (observed.baseline_peak, observed.alternative_peak, observed.delta),
            values,
            ("baseline", "alternative", "delta"),
            strict=True,
        ):
            _require(
                math.isclose(actual, target, abs_tol=1e-12),
                f"ALT-017 {storm_id} {label} changed from the approved film value",
            )


def load_claims(repo_root: Path | None = None) -> FilmClaims:
    """Load and validate the exact values approved for the film.

    Args:
        repo_root: Repository root.  When omitted, the function walks upward
            from this module until both authoritative JSON sources are found.
    """

    root = _repository_root(repo_root)
    guided_path = root / GUIDED_FIXTURE
    exhaustive_path = root / EXHAUSTIVE_DATASET
    _verify_pinned_sources(guided_path, exhaustive_path)
    fixture = _read_json(guided_path)
    exhaustive = _read_json(exhaustive_path)

    baseline_runs = _baseline_runs_by_storm(fixture)
    storms = _storm_claims(fixture, baseline_runs)
    storm_count = len(storms)
    _require(storm_count == 4, "The approved film requires exactly four synthetic storms")

    guided_raw = fixture.get("alternatives", [])
    _require(isinstance(guided_raw, list), "Guided alternatives must be a list")
    guided_alternatives = len(guided_raw)
    _require(guided_alternatives == 220, "The guided fixture must contain 220 alternatives")
    guided_points = tuple(
        AlternativePlotPoint(
            id=str(raw["id"]),
            cost=_as_float(raw["present_value_cost_thousand_usd"], f"{raw['id']} cost"),
            volume=_as_float(
                raw["four_storm_outfall_volume_million_gallons"], f"{raw['id']} volume"
            ),
            reduction=_as_float(raw["outfall_volume_reduction_percent"], f"{raw['id']} reduction"),
            nondominated=bool(raw["nondominated"]),
            meets_all_rules=bool(raw["screening"]["meets_all_rules"]),
        )
        for raw in guided_raw
    )
    _require(
        len({point.id for point in guided_points}) == 220,
        "Every guided plot point must have a unique alternative ID",
    )

    raw_rules = fixture.get("guided_story", {}).get("rules", [])
    _require(isinstance(raw_rules, list), "Guided decision rules must be a list")
    decision_rules = tuple(
        DecisionRuleClaim(
            rule_id=str(rule["rule_id"]),
            label=str(rule["label"]),
            operator=str(rule["operator"]),
            threshold=(
                _as_float(rule["threshold"], f"{rule['rule_id']} threshold")
                if isinstance(rule["threshold"], (int, float))
                and not isinstance(rule["threshold"], bool)
                else str(rule["threshold"])
            ),
            units=str(rule["units"]),
        )
        for rule in raw_rules
    )
    _require(
        tuple(rule.rule_id for rule in decision_rules)
        == (
            "aggregate_volume_reduction",
            "present_value_cost_cap",
            "no_outfall_peak_increase",
            "solver_qa",
        ),
        "The four guided decision rules changed",
    )
    _require(
        tuple(rule.threshold for rule in decision_rules) == (6.0, 200.0, 0.0, "accepted"),
        "The guided decision thresholds changed",
    )
    fitness_disposition = str(fixture.get("fitness_disposition"))
    _require(
        fitness_disposition == "not_assessed",
        "The approved teaching fixture must retain a not-assessed fitness disposition",
    )

    accounting = fixture.get("job_accounting", {})
    guided_jobs = int(accounting.get("completed_jobs", -1))
    retained_jobs = int(accounting.get("retained_jobs", -1))
    _require(
        int(accounting.get("unique_alternative_designs", -1)) == 220, "Guided design count drifted"
    )
    _require(int(accounting.get("baseline_jobs", -1)) == 4, "Guided baseline count drifted")
    _require(
        int(accounting.get("alternative_jobs", -1)) == 880, "Guided alternative-job count drifted"
    )
    _require(guided_jobs == 884, "The approved guided film requires 884 completed jobs")
    _require(
        guided_jobs == storm_count * (guided_alternatives + 1),
        "Guided job arithmetic does not reconcile",
    )
    _require(retained_jobs == 20, "The approved guided film requires 20 retained run sets")

    raw_by_id = {str(item["id"]): item for item in guided_raw}
    teaching_ids = tuple(str(value) for value in fixture.get("teaching_alternative_ids", []))
    _require(teaching_ids == TEACHING_ALTERNATIVE_IDS, "The four teaching alternatives changed")
    storm_names = {storm.storm_id: storm.name for storm in storms}
    alternatives = tuple(
        _alternative_claim(raw_by_id[identifier], storm_names) for identifier in teaching_ids
    )
    _require(len(alternatives) == 4, "The film requires exactly four teaching alternatives")
    _require(
        all(len(item.storm_runs) == 4 for item in alternatives),
        "Each teaching alternative needs four runs",
    )
    _require(
        all(item.nondominated for item in alternatives),
        "Every teaching alternative must remain nondominated",
    )
    _require(
        all(all(run.qa_status == "accepted" for run in item.storm_runs) for item in alternatives),
        "Every teaching run must retain accepted fixture QA",
    )
    _require(
        all(bool(raw_by_id[item.id].get("retained_artifacts")) for item in alternatives),
        "Every teaching alternative must retain complete artifacts",
    )
    teaching_by_id = {item.id: item for item in alternatives}
    _validate_alt017(teaching_by_id["ALT-017"])
    _require(teaching_by_id["ALT-207"].meets_all_rules, "ALT-207 must pass all teaching rules")
    _require(
        sum(item.meets_all_rules for item in alternatives) == 1,
        "Exactly one of the four teaching alternatives must pass all rules",
    )

    candidate_slots = exhaustive.get("candidate_slots", [])
    choice_counts = tuple(
        int(slot["max_additional_units"]) - int(slot["min_additional_units"]) + 1
        for slot in candidate_slots
    )
    _require(choice_counts == (5, 4, 5, 7), "Candidate-slot cardinalities changed")
    universe_vectors = int(exhaustive.get("universe_size", -1))
    _require(math.prod(choice_counts) == 700, "Candidate choices must produce 700 vectors")
    _require(universe_vectors == 700, "The exhaustive dataset must contain 700 vectors")
    exhaustive_rows = exhaustive.get("rows", [])
    _require(
        isinstance(exhaustive_rows, list) and len(exhaustive_rows) == 700, "Exhaustive rows drifted"
    )
    nonbaseline_vectors = universe_vectors - 1
    _require(
        nonbaseline_vectors == 699, "The exhaustive dataset must contain 699 nonbaseline vectors"
    )

    exhaustive_accounting = exhaustive.get("job_accounting", {})
    exhaustive_jobs = int(exhaustive_accounting.get("direct_swmm_jobs", -1))
    _require(int(exhaustive_accounting.get("vectors", -1)) == 700, "Exhaustive accounting drifted")
    _require(
        int(exhaustive_accounting.get("baseline_jobs", -1)) == 4,
        "Exhaustive baseline count drifted",
    )
    _require(
        int(exhaustive_accounting.get("alternative_jobs", -1)) == 2796,
        "Exhaustive alternative-job count drifted",
    )
    _require(exhaustive_jobs == 2800, "The exhaustive benchmark must contain 2,800 jobs")
    _require(
        exhaustive_jobs == universe_vectors * storm_count,
        "Exhaustive job arithmetic does not reconcile",
    )

    exhaustive_vectors = {tuple(int(value) for value in row["vector"]) for row in exhaustive_rows}
    _require(len(exhaustive_vectors) == 700, "Exhaustive vectors must be unique")
    guided_vectors = {tuple(int(value) for value in item["vector"]) for item in guided_raw}
    _require(len(guided_vectors) == 220, "Guided vectors must be unique")
    _require(
        guided_vectors <= exhaustive_vectors,
        "Guided vectors must be a subset of the 700-vector universe",
    )

    fixture_solver = fixture.get("solver", {})
    exhaustive_solver = exhaustive.get("solver", {})
    solver_version = str(fixture_solver.get("version"))
    solver_sha256 = str(fixture_solver.get("sha256"))
    _require(solver_version == "5.2.4", "The film is approved only for EPA SWMM 5.2.4")
    _require(exhaustive_solver.get("required_version") == solver_version, "Solver versions differ")
    _require(exhaustive_solver.get("sha256") == solver_sha256, "Solver hashes differ")
    _as_sha256(solver_sha256, "solver hash")

    baseline_volume = _as_float(
        fixture.get("baseline", {}).get("four_storm_outfall_volume_million_gallons"),
        "aggregate baseline volume",
    )
    continuity_values = [
        abs(_as_float(run["metrics"][metric], f"{run['storm_id']} {metric}"))
        for run in baseline_runs.values()
        for metric in ("runoff_continuity_error_percent", "flow_continuity_error_percent")
    ]
    largest_continuity_error = max(continuity_values)
    _require(
        math.isclose(largest_continuity_error, 0.079, abs_tol=1e-12),
        "Largest baseline continuity error changed from 0.079%",
    )
    qa_thresholds = {
        _as_float(value, f"{run['storm_id']} QA threshold")
        for run in baseline_runs.values()
        for value in run["qa"]["policy"].values()
    }
    _require(qa_thresholds == {2.0}, "The fixture continuity-warning policy must remain ±2%")

    cost_basis = fixture.get("cost_basis", {})
    cost_currency = str(cost_basis.get("currency"))
    cost_horizon_years = int(cost_basis.get("analysis_horizon_years", -1))
    cost_discount_rate = _as_float(
        cost_basis.get("real_discount_rate_percent"), "cost-basis real discount rate"
    )
    _require(cost_currency == "synthetic 2026 USD", "Synthetic cost currency/vintage changed")
    _require(cost_horizon_years == 30, "Synthetic cost horizon changed")
    _require(math.isclose(cost_discount_rate, 3.0), "Synthetic real discount rate changed")

    language = fixture.get("guided_story", {}).get("claim_language", {})
    return FilmClaims(
        fixture_id=str(fixture["fixture_id"]),
        solver_version=solver_version,
        solver_sha256=solver_sha256,
        alternatives=alternatives,
        guided_points=guided_points,
        teaching_by_id=teaching_by_id,
        storms=storms,
        decision_rules=decision_rules,
        fitness_disposition=fitness_disposition,
        baseline_volume=baseline_volume,
        guided_alternatives=guided_alternatives,
        guided_jobs=guided_jobs,
        storm_count=storm_count,
        retained_jobs=retained_jobs,
        universe_vectors=universe_vectors,
        nonbaseline_vectors=nonbaseline_vectors,
        exhaustive_jobs=exhaustive_jobs,
        candidate_choice_counts=choice_counts,
        largest_baseline_continuity_error=largest_continuity_error,
        qa_warning_threshold=next(iter(qa_thresholds)),
        cost_currency=cost_currency,
        cost_horizon_years=cost_horizon_years,
        cost_real_discount_rate_percent=cost_discount_rate,
        guided_fixture_path=guided_path,
        exhaustive_dataset_path=exhaustive_path,
        allowed_language=tuple(str(value) for value in language.get("allowed", [])),
        avoided_language=tuple(str(value) for value in language.get("avoid", [])),
    )


__all__ = [
    "AlternativeClaim",
    "AlternativePlotPoint",
    "ClaimIntegrityError",
    "DecisionRuleClaim",
    "FilmClaims",
    "StormClaim",
    "StormRun",
    "load_claims",
]
