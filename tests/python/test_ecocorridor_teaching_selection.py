from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from ecocorridor_core import direct_study
from ecocorridor_core.contracts import (
    ComputationalResourceObservation,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    RasterGrid,
    RasterValueKind,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    RuleDisposition,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _load_packager() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "package_ridgeway_evidence.py"
    spec = importlib.util.spec_from_file_location("test_ridgeway_packager", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _summary(
    portfolio_id: str,
    *,
    cost: float,
    disposition: PortfolioDisposition,
    target_value: float | None,
    cost_passed: bool = True,
    worst: float | None = None,
) -> PortfolioDecisionSummary:
    dispositions = []
    if target_value is not None:
        dispositions.append(
            RuleDisposition(
                rule_id="rule_target_improvement",
                scenario_id="scenario_target_guild_a_h1_low_barrier",
                pair_id="target_guild_a_pair_1_2",
                observed_value=target_value,
                passed=target_value >= 3,
                explanation="Synthetic teaching-selection boundary test.",
            )
        )
    dispositions.append(
        RuleDisposition(
            rule_id="rule_cost",
            observed_value=cost,
            passed=cost_passed,
            explanation="Synthetic teaching-selection boundary test.",
        )
    )
    return PortfolioDecisionSummary(
        portfolio_id=portfolio_id,
        disposition=disposition,
        cost=cost,
        currency="USD",
        rule_dispositions=dispositions,
        worst_resistance_reduction_percent=worst,
        next_action="Inspect direct evidence.",
    )


def _selection_inputs(*, collapse_cost: float):
    fixture = build_ridgeway_fixture()
    ids = [item.portfolio_id for item in fixture.portfolios[:5]]
    baseline_id, caution_id, carry_id, over_id, collapse_id = ids
    assert baseline_id == fixture.baseline_portfolio_id
    summaries = [
        _summary(
            baseline_id,
            cost=0,
            disposition=PortfolioDisposition.INCOMPLETE,
            target_value=None,
        ),
        _summary(
            caution_id,
            cost=50_000,
            disposition=PortfolioDisposition.FAILS,
            target_value=1,
            worst=1,
        ),
        _summary(
            carry_id,
            cost=300_000,
            disposition=PortfolioDisposition.PASSES,
            target_value=5,
            worst=5,
        ),
        _summary(
            over_id,
            cost=600_000,
            disposition=PortfolioDisposition.FAILS,
            target_value=10,
            cost_passed=False,
            worst=10,
        ),
        _summary(
            collapse_id,
            cost=collapse_cost,
            disposition=PortfolioDisposition.FAILS,
            target_value=-4,
            worst=-4,
        ),
    ]
    hypothesis_ids = [item.hypothesis_id for item in fixture.bundle.resistance_hypotheses]
    ranks = {
        item.portfolio_id: {
            hypothesis_id: {
                "rank": (1 if index % 2 == 0 else 70) if item.portfolio_id == collapse_id else 50,
                "score": float(80 - index),
            }
            for index, hypothesis_id in enumerate(hypothesis_ids)
        }
        for item in summaries
    }
    return fixture, summaries, ranks, collapse_id


@pytest.mark.parametrize("selector", ["runner", "packager"])
def test_hypothesis_collapse_role_preserves_honest_over_budget_fallback(
    monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    fixture, summaries, ranks, collapse_id = _selection_inputs(collapse_cost=525_000)
    if selector == "runner":
        monkeypatch.setattr(direct_study, "_hypothesis_rank_observations", lambda *_: ranks)
        roles, _ = direct_study._preselect_teaching_roles(fixture, summaries)
    else:
        packager = _load_packager()
        monkeypatch.setattr(packager, "_package_matrix_summaries", lambda *_: summaries)
        monkeypatch.setattr(packager, "_package_hypothesis_ranks", lambda *_: ranks)
        roles, _ = packager.independently_select_matrix_roles(fixture, [])
    assert roles["collapse"].portfolio_id == collapse_id
    assert roles["collapse"].portfolio_id != roles["over"].portfolio_id
    assert roles["collapse"].cost > fixture.bundle.decision_brief.budget_amount


@pytest.mark.parametrize("selector", ["runner", "packager"])
def test_hypothesis_collapse_role_accepts_distinct_under_budget_evidence(
    monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    fixture, summaries, ranks, collapse_id = _selection_inputs(collapse_cost=450_000)
    stronger_over_budget_id = fixture.portfolios[5].portfolio_id
    summaries.append(
        _summary(
            stronger_over_budget_id,
            cost=550_000,
            disposition=PortfolioDisposition.FAILS,
            target_value=-2,
            worst=-2,
        )
    )
    ranks[stronger_over_budget_id] = {
        hypothesis_id: {
            "rank": 1 if index % 2 == 0 else 100,
            "score": float(100 - index),
        }
        for index, hypothesis_id in enumerate(
            item.hypothesis_id for item in fixture.bundle.resistance_hypotheses
        )
    }
    if selector == "runner":
        monkeypatch.setattr(direct_study, "_hypothesis_rank_observations", lambda *_: ranks)
        roles, _ = direct_study._preselect_teaching_roles(fixture, summaries)
    else:
        packager = _load_packager()
        monkeypatch.setattr(packager, "_package_matrix_summaries", lambda *_: summaries)
        monkeypatch.setattr(packager, "_package_hypothesis_ranks", lambda *_: ranks)
        roles, _ = packager.independently_select_matrix_roles(fixture, [])
    assert roles["collapse"].portfolio_id == collapse_id
    assert roles["collapse"].portfolio_id != stronger_over_budget_id
    assert roles["collapse"].cost <= fixture.bundle.decision_brief.budget_amount


def test_packager_rejects_missing_or_nonfinite_hypothesis_rank_evidence() -> None:
    packager = _load_packager()
    fixture = build_ridgeway_fixture()
    hypotheses = [item.hypothesis_id for item in fixture.bundle.resistance_hypotheses]
    complete = {
        hypothesis_id: {"rank": index + 1, "score": float(10 - index)}
        for index, hypothesis_id in enumerate(hypotheses)
    }
    portfolio_id = fixture.portfolios[1].portfolio_id
    assert (
        packager.validated_hypothesis_rank_observations(
            fixture, complete, portfolio_id=portfolio_id
        )
        == complete
    )

    incomplete = dict(complete)
    incomplete.pop(hypotheses[-1])
    with pytest.raises(packager.PackagingError, match="incomplete"):
        packager.validated_hypothesis_rank_observations(
            fixture, incomplete, portfolio_id=portfolio_id
        )

    nonfinite = {key: dict(value) for key, value in complete.items()}
    nonfinite[hypotheses[0]]["score"] = float("nan")
    with pytest.raises(packager.PackagingError, match="invalid"):
        packager.validated_hypothesis_rank_observations(
            fixture, nonfinite, portfolio_id=portfolio_id
        )


def _resource_observation(observation_id: str) -> dict[str, object]:
    return ComputationalResourceObservation(
        observation_id=observation_id,
        boundary=ResourceObservationBoundary.DIRECT_SOLVER,
        solver_wall_seconds=1.25,
        peak_memory_status=ResourceObservationStatus.NOT_MEASURED,
        peak_memory_method="UNAVAILABLE_IN_TEST",
        energy_status=ResourceObservationStatus.NOT_MEASURED,
        energy_method="UNAVAILABLE_IN_TEST",
        hardware="generalized test hardware",
        boundary_description="One direct synthetic test job.",
    ).model_dump(mode="json")


def test_packager_independently_validates_every_resource_observation() -> None:
    packager = _load_packager()
    jobs = [
        {"direct_run_id": "run-1", "resource_observation": _resource_observation("obs-1")},
        {"direct_run_id": "run-2", "resource_observation": _resource_observation("obs-2")},
    ]
    summary = packager.independently_aggregate_resource_observability(jobs)
    assert summary["jobsWithObservations"] == 2
    assert summary["jobsWithoutObservations"] == 0
    assert summary["directSolverWallSeconds"] == 2.5
    assert summary["energyStatus"] == "NOT_MEASURED"
    assert summary["estimatedEnergyKwh"] is None
    assert "carbon" in summary["limitation"]

    missing = [jobs[0], {"direct_run_id": "run-2"}]
    with pytest.raises(packager.PackagingError, match="no resource observation"):
        packager.independently_aggregate_resource_observability(missing)

    duplicated = [jobs[0], {**jobs[1], "resource_observation": jobs[0]["resource_observation"]}]
    with pytest.raises(packager.PackagingError, match="must be unique"):
        packager.independently_aggregate_resource_observability(duplicated)

    tampered_observation = dict(_resource_observation("obs-3"))
    tampered_observation["solver_wall_seconds"] = -1
    with pytest.raises(packager.PackagingError, match="invalid resource observation"):
        packager.independently_aggregate_resource_observability(
            [{"direct_run_id": "run-3", "resource_observation": tampered_observation}]
        )


def test_packager_projects_every_pair_with_exact_run_and_matrix_identity() -> None:
    packager = _load_packager()
    baseline = {
        "direct_run_id": "matrix_portfolio_baseline_scenario_a",
        "portfolio_id": "portfolio_baseline",
        "scenario_id": "scenario_a",
        "matrix_sha256": "1" * 64,
        "pair_metrics": [
            {"pair_id": "pair_1_2", "effective_resistance": 20.0},
            {"pair_id": "pair_1_3", "effective_resistance": 10.0},
        ],
    }
    portfolio = {
        "direct_run_id": "matrix_portfolio_reviewed_scenario_a",
        "portfolio_id": "portfolio_reviewed",
        "scenario_id": "scenario_a",
        "matrix_sha256": "2" * 64,
        "pair_metrics": [
            {"pair_id": "pair_1_2", "effective_resistance": 18.0},
            {"pair_id": "pair_1_3", "effective_resistance": 9.8},
        ],
    }

    rows = packager.project_pair_results(
        baseline_job=baseline,
        portfolio_job=portfolio,
        safeguard=False,
        target_threshold=3.0,
    )

    assert [item["pairId"] for item in rows] == ["pair_1_2", "pair_1_3"]
    assert rows[0] == {
        "pairId": "pair_1_2",
        "baselineEffectiveResistance": 20.0,
        "portfolioEffectiveResistance": 18.0,
        "baselineRelativeChangePct": -10.0,
        "passed": True,
        "claimId": "ECO-PAIR-portfolio_reviewed-scenario_a-pair_1_2",
        "baselineRunId": baseline["direct_run_id"],
        "portfolioRunId": portfolio["direct_run_id"],
        "baselineMatrixSha256": "1" * 64,
        "portfolioMatrixSha256": "2" * 64,
    }
    assert rows[1]["baselineRelativeChangePct"] == pytest.approx(-2.0)
    assert rows[1]["passed"] is False

    incomplete = {**portfolio, "pair_metrics": portfolio["pair_metrics"][:1]}
    with pytest.raises(packager.PackagingError, match="pair coverage differs"):
        packager.project_pair_results(
            baseline_job=baseline,
            portfolio_job=incomplete,
            safeguard=False,
            target_threshold=3.0,
        )


def test_packager_compacts_exact_current_raster_cells_and_nodata(tmp_path: Path) -> None:
    packager = _load_packager()
    artifact_directory = "jobs/map/portfolio_reviewed/scenario_a/attempt-1"
    current = tmp_path / artifact_directory / "outputs" / "connectivity_cum_curmap.asc"
    current.parent.mkdir(parents=True)
    current.write_text(
        "ncols 3\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n"
        "NODATA_value -9999\n0 1 -9999\n4 9 16\n",
        encoding="utf-8",
    )
    job = {
        "direct_run_id": "map_portfolio_reviewed_scenario_a",
        "portfolio_id": "portfolio_reviewed",
        "scenario_id": "scenario_a",
        "artifact_directory": artifact_directory,
        "current_map_sha256": packager.sha256_file(current),
    }
    grid = RasterGrid(
        grid_id="ridgeway-test-grid",
        crs="EPSG:5070",
        width=3,
        height=2,
        transform=(1.0, 0.0, 0.0, 0.0, -1.0, 2.0),
        nodata=-9999.0,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=1.0,
    )

    projected = packager.compact_current_raster(
        evidence_root=tmp_path,
        job=job,
        expected_grid=grid,
    )

    assert projected["runId"] == job["direct_run_id"]
    assert projected["sha256"] == job["current_map_sha256"]
    assert projected["cells"] == [0.0, 1.0, None, 4.0, 9.0, 16.0]
    assert projected["minimumCurrent"] == 0.0
    assert projected["maximumCurrent"] == 16.0
    assert len(projected["nodataTopologySha256"]) == 64
