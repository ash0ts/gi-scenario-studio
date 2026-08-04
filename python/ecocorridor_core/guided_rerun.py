"""Pinned-reference support for the real Ridgeway guided rerun.

The public replay is never used as a solver.  This module extracts only direct
Circuitscape evidence from the integrity-checked Ridgeway archive and compares a
new local execution to those values with declared metric-specific tolerances.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from scenario_platform.storage import PathSecurityError, contained_path

from .contracts import (
    DirectConnectivityRun,
    PortfolioDecisionSummary,
    RuleMetric,
    canonical_sha256,
)
from .engine import sha256_file
from .guided_evidence import GuidedEvidenceResource
from .interventions import portfolio_cost, unresolved_portfolio_concerns
from .metrics import evaluate_portfolio
from .project_store import EcoProjectStore
from .raster_io import current_map_values_and_nodata
from .ridgeway import RidgewayFixture

MATRIX_ABSOLUTE_TOLERANCE = 1e-8
MATRIX_RELATIVE_TOLERANCE = 1e-7
CURRENT_ABSOLUTE_TOLERANCE = 1e-8
CURRENT_RELATIVE_TOLERANCE = 1e-6


class GuidedRerunEvidenceError(RuntimeError):
    """Pinned evidence or a local differential is structurally incomplete."""


def _job_key(portfolio_id: str, scenario_id: str) -> str:
    return f"{portfolio_id}\x1f{scenario_id}"


def _validated_pair_metrics(
    raw: dict[str, Any], *, expected_pair_ids: set[str]
) -> dict[str, float]:
    if (
        raw.get("state") != "SUCCEEDED"
        or raw.get("exact") is not True
        or raw.get("test_only", False) is True
        or raw.get("engine_name") != "Circuitscape"
        or raw.get("qa_status") not in {"ACCEPTED", "WARNING_ACCEPTED"}
    ):
        raise GuidedRerunEvidenceError("pinned Ridgeway job is not accepted direct evidence")
    rows = raw.get("pair_metrics")
    if not isinstance(rows, list):
        raise GuidedRerunEvidenceError("pinned Ridgeway job omits focal-pair metrics")
    observed: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise GuidedRerunEvidenceError("pinned Ridgeway pair metric is malformed")
        pair_id = str(row.get("pair_id", ""))
        value = float(row.get("effective_resistance", math.nan))
        if pair_id in observed or not math.isfinite(value) or value <= 0:
            raise GuidedRerunEvidenceError("pinned Ridgeway pair metric is invalid")
        observed[pair_id] = value
    if set(observed) != expected_pair_ids:
        raise GuidedRerunEvidenceError("pinned Ridgeway job has the wrong focal-pair coverage")
    return observed


def build_pinned_reference(
    resource: GuidedEvidenceResource,
    fixture: RidgewayFixture,
    *,
    materialize_parent: Path,
) -> dict[str, Any]:
    """Build a path-free restart-safe numeric reference from verified artifacts."""

    root = resource.materialize(materialize_parent)
    index_path = contained_path(root, "evidence-index.json")
    if sha256_file(index_path) != str(resource.manifest["evidence_index_sha256"]):
        raise GuidedRerunEvidenceError("pinned Ridgeway evidence index hash changed")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuidedRerunEvidenceError("pinned Ridgeway evidence index is unreadable") from exc
    if not isinstance(index, dict):
        raise GuidedRerunEvidenceError("pinned Ridgeway evidence index is malformed")
    if (
        index.get("model_sha256") != fixture.bundle.revision.model_sha256
        or index.get("engine_manifest_sha256") != resource.manifest["engine_manifest_sha256"]
    ):
        raise GuidedRerunEvidenceError("pinned Ridgeway evidence identifies another model")

    scenario_by_id = {item.scenario_id: item for item in fixture.bundle.scenarios}
    expected_matrix = {
        _job_key(portfolio.portfolio_id, scenario.scenario_id)
        for portfolio in fixture.portfolios
        for scenario in fixture.bundle.scenarios
    }
    teaching = resource.manifest.get("teaching_selection")
    if not isinstance(teaching, dict):
        raise GuidedRerunEvidenceError("pinned Ridgeway teaching selection is absent")
    map_portfolio_ids = [str(item) for item in teaching.get("map_rerun_portfolio_ids", [])]
    expected_map_order = [
        fixture.baseline_portfolio_id,
        str(teaching.get("low_cost_caution_portfolio_id", "")),
        str(teaching.get("carry_forward_portfolio_id", "")),
        str(teaching.get("over_budget_portfolio_id", "")),
        str(teaching.get("hypothesis_collapse_portfolio_id", "")),
    ]
    if map_portfolio_ids != expected_map_order or len(set(map_portfolio_ids)) != 5:
        raise GuidedRerunEvidenceError("pinned Ridgeway teaching portfolio identities are invalid")
    fixture_portfolio_ids = {item.portfolio_id for item in fixture.portfolios}
    if not set(map_portfolio_ids).issubset(fixture_portfolio_ids):
        raise GuidedRerunEvidenceError("pinned teaching selection references another portfolio")
    expected_map = {
        _job_key(portfolio_id, scenario.scenario_id)
        for portfolio_id in map_portfolio_ids
        for scenario in fixture.bundle.scenarios
    }

    def parse_jobs(label: str, expected: set[str], *, include_current: bool) -> dict[str, Any]:
        rows = index.get(label)
        if not isinstance(rows, list):
            raise GuidedRerunEvidenceError(f"pinned Ridgeway index omits {label}")
        parsed: dict[str, Any] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                raise GuidedRerunEvidenceError("pinned Ridgeway direct job is malformed")
            portfolio_id = str(raw.get("portfolio_id", ""))
            scenario_id = str(raw.get("scenario_id", ""))
            key = _job_key(portfolio_id, scenario_id)
            if key in parsed or scenario_id not in scenario_by_id:
                raise GuidedRerunEvidenceError("pinned Ridgeway direct-job identity is invalid")
            scenario = scenario_by_id[scenario_id]
            item: dict[str, Any] = {
                "portfolio_id": portfolio_id,
                "scenario_id": scenario_id,
                "pair_metrics": _validated_pair_metrics(
                    raw, expected_pair_ids=set(scenario.included_pair_ids)
                ),
            }
            if include_current:
                relative = str(raw.get("artifact_directory", ""))
                try:
                    current_path = contained_path(
                        root, Path(relative) / "outputs" / "connectivity_cum_curmap.asc"
                    )
                except PathSecurityError as exc:
                    raise GuidedRerunEvidenceError(
                        "pinned Ridgeway current raster escaped its evidence root"
                    ) from exc
                if (
                    not current_path.is_file()
                    or current_path.is_symlink()
                    or sha256_file(current_path) != raw.get("current_map_sha256")
                ):
                    raise GuidedRerunEvidenceError(
                        "pinned Ridgeway current raster failed integrity verification"
                    )
                grid = fixture.rasters[
                    next(
                        hypothesis.resistance_asset_id
                        for hypothesis in fixture.bundle.resistance_hypotheses
                        if hypothesis.hypothesis_id == scenario.resistance_hypothesis_id
                    )
                ].grid
                values, nodata = current_map_values_and_nodata(current_path, expected_grid=grid)
                item["current_values"] = [list(row) for row in values]
                item["current_nodata"] = nodata
            parsed[key] = item
        if set(parsed) != expected:
            raise GuidedRerunEvidenceError(f"pinned Ridgeway {label} coverage is incomplete")
        return parsed

    reference = {
        "schema_version": "ridgeway-rerun-reference/v1.0",
        "fixture_version": fixture.fixture_version,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "pinned_manifest_sha256": resource.manifest_sha256,
        "pinned_engine_manifest_sha256": resource.manifest["engine_manifest_sha256"],
        "teaching_selection": teaching,
        "matrix": parse_jobs("matrix_jobs", expected_matrix, include_current=False),
        "map": parse_jobs("map_jobs", expected_map, include_current=True),
    }
    reference["reference_sha256"] = canonical_sha256(reference)
    return reference


def matrix_summaries(
    fixture: RidgewayFixture, runs: list[DirectConnectivityRun]
) -> list[PortfolioDecisionSummary]:
    """Evaluate the complete matrix barrier without pretending maps were run."""

    baseline = [item for item in runs if item.portfolio_id == fixture.baseline_portfolio_id]
    rules = [
        item
        for item in fixture.bundle.decision_rules
        if item.metric != RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
    ]
    safeguard_tolerances = {
        scenario_id: safeguard.nonregression_threshold_percent
        for safeguard in fixture.bundle.safeguards
        for scenario_id in safeguard.scenario_ids
    }
    output: list[PortfolioDecisionSummary] = []
    for portfolio in fixture.portfolios:
        selected = [item for item in runs if item.portfolio_id == portfolio.portfolio_id]
        cost, currency, _ = portfolio_cost(portfolio, fixture.bundle.intervention_options)
        output.append(
            evaluate_portfolio(
                portfolio=portfolio,
                runs=selected,
                baseline_runs=baseline,
                scenarios=fixture.bundle.scenarios,
                rules=rules,
                cost=cost,
                currency=currency,
                unresolved_feasibility=unresolved_portfolio_concerns(
                    portfolio, fixture.bundle.intervention_options
                ),
                safeguard_tolerances=safeguard_tolerances,
            )
        )
    return output


def _within(
    observed: float, expected: float, absolute: float, relative: float
) -> tuple[bool, float, float]:
    difference = abs(observed - expected)
    allowed = absolute + relative * max(abs(observed), abs(expected))
    relative_difference = difference / max(abs(expected), absolute)
    return difference <= allowed, difference, relative_difference


def _accumulator() -> dict[str, Any]:
    return {
        "values_compared": 0,
        "outside_tolerance": 0,
        "maximum_absolute_difference": 0.0,
        "maximum_relative_difference": 0.0,
        "examples": [],
    }


def _observe(
    accumulator: dict[str, Any],
    *,
    observed: float,
    expected: float,
    absolute: float,
    relative: float,
    identity: str,
) -> None:
    accepted, difference, relative_difference = _within(observed, expected, absolute, relative)
    accumulator["values_compared"] += 1
    accumulator["maximum_absolute_difference"] = max(
        accumulator["maximum_absolute_difference"], difference
    )
    accumulator["maximum_relative_difference"] = max(
        accumulator["maximum_relative_difference"], relative_difference
    )
    if not accepted:
        accumulator["outside_tolerance"] += 1
        if len(accumulator["examples"]) < 20:
            accumulator["examples"].append(
                {
                    "identity": identity,
                    "expected": expected,
                    "observed": observed,
                    "absolute_difference": difference,
                }
            )


def build_differential_report(
    *,
    store: EcoProjectStore,
    fixture: RidgewayFixture,
    matrix_analysis_id: str,
    map_analysis_id: str,
    local_engine_manifest_sha256: str,
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Compare every reportable pair value and every retained current-map cell."""

    matrix_runs, _, _ = store.analysis_results(matrix_analysis_id)
    map_runs, _, _ = store.analysis_results(map_analysis_id)
    matrix_by_key = {_job_key(item.portfolio_id, item.scenario_id): item for item in matrix_runs}
    map_by_key = {_job_key(item.portfolio_id, item.scenario_id): item for item in map_runs}
    if set(matrix_by_key) != set(reference["matrix"]) or set(map_by_key) != set(reference["map"]):
        raise GuidedRerunEvidenceError("local Ridgeway rerun coverage is incomplete")

    matrix_delta = _accumulator()
    map_pair_delta = _accumulator()
    current_delta = _accumulator()
    for label, local, pinned, accumulator in (
        ("matrix", matrix_by_key, reference["matrix"], matrix_delta),
        ("map", map_by_key, reference["map"], map_pair_delta),
    ):
        for key in sorted(pinned):
            observed = {item.pair_id: item.effective_resistance for item in local[key].pair_metrics}
            expected = pinned[key]["pair_metrics"]
            if set(observed) != set(expected):
                raise GuidedRerunEvidenceError(f"local Ridgeway {label} pair coverage changed")
            for pair_id in sorted(expected):
                _observe(
                    accumulator,
                    observed=observed[pair_id],
                    expected=expected[pair_id],
                    absolute=MATRIX_ABSOLUTE_TOLERANCE,
                    relative=MATRIX_RELATIVE_TOLERANCE,
                    identity=f"{key}:{pair_id}",
                )

    scenario_by_id = {item.scenario_id: item for item in fixture.bundle.scenarios}
    for key in sorted(reference["map"]):
        run = map_by_key[key]
        artifacts = store.run_artifacts(run.direct_run_id)
        current = next((item for item in artifacts if item["role"] == "current_map"), None)
        if current is None or current["sha256"] != run.current_map_sha256:
            raise GuidedRerunEvidenceError("local Ridgeway current-map evidence is incomplete")
        try:
            path = contained_path(store.root, str(current["cas_relative_path"]))
        except PathSecurityError as exc:
            raise GuidedRerunEvidenceError(
                "local current-map artifact escaped the project"
            ) from exc
        if sha256_file(path) != str(current["sha256"]):
            raise GuidedRerunEvidenceError("local current-map artifact hash changed")
        scenario = scenario_by_id[run.scenario_id]
        grid = fixture.rasters[
            next(
                hypothesis.resistance_asset_id
                for hypothesis in fixture.bundle.resistance_hypotheses
                if hypothesis.hypothesis_id == scenario.resistance_hypothesis_id
            )
        ].grid
        observed_values, observed_nodata = current_map_values_and_nodata(path, expected_grid=grid)
        pinned = reference["map"][key]
        expected_values = pinned["current_values"]
        expected_nodata = float(pinned["current_nodata"])
        if len(observed_values) != len(expected_values) or any(
            len(observed_row) != len(expected_row)
            for observed_row, expected_row in zip(observed_values, expected_values, strict=True)
        ):
            raise GuidedRerunEvidenceError("local Ridgeway current-map dimensions changed")
        for row_index, (observed_row, expected_row) in enumerate(
            zip(observed_values, expected_values, strict=True)
        ):
            for column_index, (observed, expected) in enumerate(
                zip(observed_row, expected_row, strict=True)
            ):
                observed_missing = observed == observed_nodata
                expected_missing = expected == expected_nodata
                if observed_missing and expected_missing:
                    continue
                if observed_missing != expected_missing:
                    current_delta["values_compared"] += 1
                    current_delta["outside_tolerance"] += 1
                    if len(current_delta["examples"]) < 20:
                        current_delta["examples"].append(
                            {
                                "identity": f"{key}:cell:{row_index}:{column_index}",
                                "expected": expected,
                                "observed": observed,
                                "reason": "NODATA coverage changed",
                            }
                        )
                    continue
                if (
                    not math.isfinite(observed)
                    or not math.isfinite(expected)
                    or observed < 0
                    or expected < 0
                ):
                    raise GuidedRerunEvidenceError(
                        "Ridgeway current-map differential contains invalid current values"
                    )
                _observe(
                    current_delta,
                    observed=observed,
                    expected=expected,
                    absolute=CURRENT_ABSOLUTE_TOLERANCE,
                    relative=CURRENT_RELATIVE_TOLERANCE,
                    identity=f"{key}:cell:{row_index}:{column_index}",
                )

    for accumulator in (matrix_delta, map_pair_delta, current_delta):
        accumulator["status"] = (
            "WITHIN_TOLERANCE" if accumulator["outside_tolerance"] == 0 else "DIFFERS"
        )
    report: dict[str, Any] = {
        "schema_version": "ridgeway-differential/v1.0",
        "fixture_version": fixture.fixture_version,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "pinned_manifest_sha256": reference["pinned_manifest_sha256"],
        "pinned_engine_manifest_sha256": reference["pinned_engine_manifest_sha256"],
        "local_engine_manifest_sha256": local_engine_manifest_sha256,
        "teaching_selection": reference["teaching_selection"],
        "tolerances": {
            "effective_resistance": {
                "absolute": MATRIX_ABSOLUTE_TOLERANCE,
                "relative": MATRIX_RELATIVE_TOLERANCE,
            },
            "current_map_cell": {
                "absolute": CURRENT_ABSOLUTE_TOLERANCE,
                "relative": CURRENT_RELATIVE_TOLERANCE,
            },
        },
        "matrix": {"jobs_compared": len(matrix_by_key), **matrix_delta},
        "map_pairs": {"jobs_compared": len(map_by_key), **map_pair_delta},
        "current_maps": {"jobs_compared": len(map_by_key), **current_delta},
        "accepted": all(
            item["outside_tolerance"] == 0 for item in (matrix_delta, map_pair_delta, current_delta)
        ),
        "limitations": [
            "This compares direct Circuitscape calculations under the documented fixture assumptions.",
            "It is not evidence of animal movement, crossing use, collision reduction, or population benefit.",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report
