from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

FILM_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = FILM_ROOT.parents[1]
CLAIMS_PATH = FILM_ROOT / "src/gi_story/claims.py"
SPEC = importlib.util.spec_from_file_location("gi_story_claims_test", CLAIMS_PATH)
assert SPEC is not None and SPEC.loader is not None
CLAIMS_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLAIMS_MODULE
SPEC.loader.exec_module(CLAIMS_MODULE)
load_claims = CLAIMS_MODULE.load_claims


@pytest.fixture(scope="module")
def claims():
    return load_claims(REPOSITORY_ROOT)


def test_job_arithmetic_and_fixture_identity(claims) -> None:
    assert claims.fixture_id == "cedar_trace_exact_v2"
    assert claims.solver_version == "5.2.4"
    assert len(claims.solver_sha256) == 64
    assert claims.storm_count == 4
    assert claims.guided_alternatives == 220
    assert len(claims.guided_points) == 220
    assert len({point.id for point in claims.guided_points}) == 220
    assert claims.guided_jobs == 884
    assert claims.guided_jobs == (claims.guided_alternatives + 1) * claims.storm_count
    assert claims.retained_jobs == 20
    assert claims.largest_baseline_continuity_error == pytest.approx(0.079)
    assert claims.qa_warning_threshold == pytest.approx(2.0)
    assert claims.fitness_disposition == "not_assessed"
    assert [rule.threshold for rule in claims.decision_rules] == [6.0, 200.0, 0.0, "accepted"]


def test_complete_universe_and_exhaustive_benchmark(claims) -> None:
    assert claims.candidate_choice_counts == (5, 4, 5, 7)
    assert claims.universe_vectors == 700
    assert claims.nonbaseline_vectors == 699
    assert claims.exhaustive_jobs == 2800
    assert claims.exhaustive_jobs == claims.universe_vectors * claims.storm_count


def test_four_synthetic_storms_and_baseline(claims) -> None:
    assert claims.baseline_volume == pytest.approx(0.748)
    assert [storm.storm_id for storm in claims.storms] == [
        "synthetic_balanced_1in",
        "synthetic_front_loaded_2in",
        "synthetic_long_2_5in",
        "synthetic_stress_4in",
    ]
    assert [(storm.depth_inches, storm.duration_hours) for storm in claims.storms] == [
        (1.0, 1.0),
        (2.0, 1.0),
        (2.5, 4.0),
        (4.0, 2.0),
    ]
    assert [storm.baseline_peak for storm in claims.storms] == pytest.approx(
        [4.14, 10.11, 3.14, 13.33]
    )
    assert [storm.baseline_volume for storm in claims.storms] == pytest.approx(
        [0.062, 0.157, 0.167, 0.362]
    )
    assert all(storm.authority_status == "user_defined" for storm in claims.storms)


def test_teaching_alternatives_are_exactly_the_approved_four(claims) -> None:
    assert [alternative.id for alternative in claims.alternatives] == [
        "ALT-017",
        "ALT-207",
        "ALT-109",
        "ALT-014",
    ]
    assert set(claims.teaching_by_id) == {"ALT-017", "ALT-207", "ALT-109", "ALT-014"}

    expected = {
        "ALT-017": ((0, 0, 1, 6), 54.99, 0.717, 4.14, False),
        "ALT-207": ((0, 3, 2, 6), 190.25, 0.700, 6.42, True),
        "ALT-109": ((1, 3, 2, 6), 222.01, 0.696, 6.95, False),
        "ALT-014": ((4, 3, 4, 5), 361.86, 0.682, 8.82, False),
    }
    for identifier, (vector, cost, volume, reduction, meets_all) in expected.items():
        alternative = claims.teaching_by_id[identifier]
        assert alternative.vector == vector
        assert alternative.cost == pytest.approx(cost)
        assert alternative.volume == pytest.approx(volume)
        assert alternative.reduction == pytest.approx(reduction)
        assert alternative.meets_all_rules is meets_all
        assert alternative.nondominated
        assert len(alternative.storm_runs) == 4
        assert all(run.qa_status == "accepted" for run in alternative.storm_runs)


def test_one_synthetic_cost_basis(claims) -> None:
    assert claims.cost_currency == "synthetic 2026 USD"
    assert claims.cost_horizon_years == 30
    assert claims.cost_real_discount_rate_percent == pytest.approx(3.0)


def test_alt017_retains_both_peak_increases_with_evidence_hashes(claims) -> None:
    increases = claims.alt017_peak_increases
    assert [run.storm_id for run in increases] == [
        "synthetic_long_2_5in",
        "synthetic_stress_4in",
    ]
    assert [
        (run.baseline_peak, run.alternative_peak, run.delta) for run in increases
    ] == pytest.approx([(3.14, 3.21, 0.07), (13.33, 13.34, 0.01)])
    assert all(run.run_id.startswith("run_") for run in increases)
    for run in increases:
        assert len(run.input_sha256 or "") == 64
        assert len(run.report_sha256 or "") == 64
        assert len(run.output_sha256 or "") == 64


def test_default_loader_discovers_the_repository() -> None:
    discovered = load_claims()
    assert (
        discovered.guided_fixture_path
        == REPOSITORY_ROOT / "apps/desktop/src/data/guidedFixture.json"
    )
    assert (
        discovered.exhaustive_dataset_path
        == REPOSITORY_ROOT / "benchmarks/exhaustive_exact/dataset.json"
    )
