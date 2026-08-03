from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from gi_scenario_core.benchmark import (
    ExactBenchmarkRow,
    HiddenExactOracle,
    _crowding_distances,
    _environmental_survival,
    _integer_mutation,
    _nondominated_fronts,
    _nsga2_candidates,
    _rank_and_crowding,
    _sync_wandb,
    _tournament_winner,
    run_replay,
    score_replay,
)
from gi_scenario_core.contracts import CandidateSlot


def _rows():
    return [
        ExactBenchmarkRow("d00", (0, 0), {"cost": 0, "benefit": 0}, False, True),
        ExactBenchmarkRow("d01", (0, 1), {"cost": 1, "benefit": 3}, True, False),
        ExactBenchmarkRow("d10", (1, 0), {"cost": 2, "benefit": 5}, True, False),
        ExactBenchmarkRow("d11", (1, 1), {"cost": 4, "benefit": 4}, True, True),
    ]


def _payload():
    return {
        "dataset_id": "fixture",
        "dataset_sha256": "a" * 64,
        "objective_directions": {"cost": "minimize", "benefit": "maximize"},
        "candidate_slots": [
            {
                "slot_id": "one",
                "subcatchment_id": "S1",
                "lid_control": "BIO",
                "lid_type": "BC",
                "min_additional_units": 0,
                "max_additional_units": 1,
                "unit_area": 1,
                "surface_width": 1,
                "from_impervious_percent": 0,
                "engineer_approved": True,
                "feasibility_basis": "fixture",
            },
            {
                "slot_id": "two",
                "subcatchment_id": "S2",
                "lid_control": "BIO",
                "lid_type": "BC",
                "min_additional_units": 0,
                "max_additional_units": 1,
                "unit_area": 1,
                "surface_width": 1,
                "from_impervious_percent": 0,
                "engineer_approved": True,
                "feasibility_basis": "fixture",
            },
        ],
    }


def _large_payload():
    payload = _payload()
    payload["candidate_slots"] = [
        {
            **payload["candidate_slots"][0],
            "slot_id": f"slot-{index}",
            "subcatchment_id": f"S{index}",
            "max_additional_units": 4,
        }
        for index in range(3)
    ]
    return payload


def _large_rows():
    rows = []
    for first in range(5):
        for second in range(5):
            for third in range(5):
                vector = (first, second, third)
                rows.append(
                    ExactBenchmarkRow(
                        f"d{first}{second}{third}",
                        vector,
                        {
                            "cost": float(4 * first + 2 * second + third),
                            "benefit": float(first + 3 * second + 2 * third),
                        },
                        first + second + third >= 3,
                        first == 4 and third == 4,
                    )
                )
    return rows


def test_hidden_oracle_rejects_duplicate_and_unknown_queries():
    oracle = HiddenExactOracle(_rows())
    assert oracle.evaluate((0, 1)).design_id == "d01"
    with pytest.raises(ValueError, match="twice"):
        oracle.evaluate((0, 1))
    with pytest.raises(ValueError, match="not in"):
        oracle.evaluate((2, 2))


def test_complete_observation_scores_one_for_all_recalls():
    score = score_replay(_rows(), _rows(), {"cost": "minimize", "benefit": "maximize"})
    assert score["hypervolume_ratio"] == pytest.approx(1.0)
    assert score["frontier_recall"] == pytest.approx(1.0)
    assert score["feasible_recall"] == pytest.approx(1.0)
    assert score["adverse_recall"] == pytest.approx(1.0)


def test_nsga2_sort_uses_feasibility_first_then_pareto_domination():
    directions = {"cost": "minimize", "benefit": "maximize"}
    rows = [
        ExactBenchmarkRow("tradeoff-low", (0,), {"cost": 1, "benefit": 1}, True, False),
        ExactBenchmarkRow("tradeoff-high", (1,), {"cost": 2, "benefit": 2}, True, False),
        ExactBenchmarkRow("dominated-feasible", (2,), {"cost": 3, "benefit": 1}, True, False),
        ExactBenchmarkRow("infeasible", (3,), {"cost": 0, "benefit": 100}, False, False),
    ]

    fronts = _nondominated_fronts(rows, directions)

    assert {row.design_id for row in fronts[0]} == {"tradeoff-low", "tradeoff-high"}
    assert [row.design_id for row in fronts[1]] == ["dominated-feasible"]
    assert [row.design_id for row in fronts[2]] == ["infeasible"]


def test_nsga2_crowding_and_environmental_survival_preserve_front_extremes():
    directions = {"cost": "minimize", "benefit": "maximize"}
    front = [
        ExactBenchmarkRow(f"d{value}", (value,), {"cost": value, "benefit": value}, True, False)
        for value in range(4)
    ]

    crowding = _crowding_distances(front, directions)
    survivors = _environmental_survival(front, directions, population_size=2)

    assert crowding[(0,)] == float("inf")
    assert crowding[(3,)] == float("inf")
    assert crowding[(1,)] == pytest.approx(4 / 3)
    assert crowding[(2,)] == pytest.approx(4 / 3)
    assert {row.vector for row in survivors} == {(0,), (3,)}


def test_nsga2_binary_tournament_prefers_rank_then_crowding():
    directions = {"cost": "minimize", "benefit": "maximize"}
    population = [
        ExactBenchmarkRow("edge", (0,), {"cost": 0, "benefit": 0}, True, False),
        ExactBenchmarkRow("middle", (1,), {"cost": 1, "benefit": 1}, True, False),
        ExactBenchmarkRow("middle-two", (2,), {"cost": 2, "benefit": 2}, True, False),
        ExactBenchmarkRow("far-edge", (3,), {"cost": 3, "benefit": 3}, True, False),
        ExactBenchmarkRow("dominated", (4,), {"cost": 4, "benefit": 0}, True, False),
    ]
    ranked = _rank_and_crowding(population, directions)
    generator = random.Random(7)

    assert _tournament_winner(population[0], population[4], ranked, generator).design_id == "edge"
    assert _tournament_winner(population[0], population[1], ranked, generator).design_id == "edge"


def test_nsga2_integer_mutation_stays_in_bounds_and_changes_mutated_genes():
    slots = [CandidateSlot.model_validate(slot) for slot in _large_payload()["candidate_slots"]]
    original = (0, 2, 4)

    mutated = _integer_mutation(original, slots, random.Random(11), probability=1.0)

    assert mutated != original
    assert all(value != original[index] for index, value in enumerate(mutated))
    assert all(
        slot.min_additional_units <= value <= slot.max_additional_units
        for value, slot in zip(mutated, slots, strict=True)
    )


def test_nsga2_offspring_are_unseen_unique_bounded_vectors():
    payload = _large_payload()
    observed = _large_rows()[:20]
    seen = {row.vector for row in observed}

    candidates, fill_count, fill_engine = _nsga2_candidates(
        payload,
        observed,
        seen,
        seed=19,
        count=24,
    )

    assert len(candidates) == 24
    assert len(set(candidates)) == 24
    assert not (set(candidates) & seen)
    assert 0 <= fill_count <= len(candidates)
    assert fill_engine in {"none", "scipy_sobol_scrambled", "halton_cranley_patterson_fallback"}
    assert all(all(0 <= value <= 4 for value in vector) for vector in candidates)


@pytest.mark.parametrize("method", ["random", "sobol", "nsga2", "sobol_extratrees"])
def test_replay_is_deterministic_and_spends_exactly_the_budget(method):
    first = run_replay(_payload(), _rows(), method=method, seed=9, budget=3)
    second = run_replay(_payload(), _rows(), method=method, seed=9, budget=3)
    assert first == second
    assert first["summary"]["exact_designs"] == 3
    assert len(first["selected_design_ids"]) == 3


def test_nsga2_replay_records_environmental_generations_without_budget_or_duplicate_leaks():
    first = run_replay(_large_payload(), _large_rows(), method="nsga2", seed=23, budget=96)
    second = run_replay(_large_payload(), _large_rows(), method="nsga2", seed=23, budget=96)

    assert first == second
    assert first["summary"]["exact_designs"] == 96
    assert first["summary"]["exact_job_equivalent"] == 384
    assert len(set(first["selected_design_ids"])) == 96
    assert first["method_configuration"] == {
        "variant": "deterministic_batch_barriered_mu_plus_lambda_nsga2",
        "population_size": 64,
        "offspring_batch_size": 32,
        "initialization": first["method_configuration"]["initialization"],
        "selection": "binary_tournament_pareto_rank_then_crowding",
        "constraint_handling": "binary_feasibility_first_then_pareto",
        "crossover": "bounded_integer_uniform",
        "crossover_probability": 0.9,
        "mutation": "bounded_integer_random_reset",
        "mutation_probability": pytest.approx(1 / 3),
        "survival": "mu_plus_lambda_front_fill_then_crowding",
        "deduplication": "canonical_integer_vector_before_oracle_request",
        "operator_exhaustion_fill": "deterministic_qmc_unseen_vectors",
        "generation_count": 1,
    }
    generations = first["nsga2_generations"]
    assert [item["generation"] for item in generations] == [0, 1]
    assert len(generations[0]["population_design_ids"]) == 64
    assert len(generations[1]["offspring_design_ids"]) == 32
    assert len(generations[1]["population_design_ids"]) == 64
    assert set(generations[1]["population_design_ids"]).issubset(set(first["selected_design_ids"]))


def test_wandb_benchmark_sync_requires_an_explicit_restricted_destination(monkeypatch):
    monkeypatch.setitem(sys.modules, "wandb", object())
    monkeypatch.delenv("WANDB_ENTITY", raising=False)
    monkeypatch.delenv("WANDB_PROJECT", raising=False)

    with pytest.raises(RuntimeError, match="WANDB_RESTRICTED_DESTINATION_REQUIRED"):
        _sync_wandb({"campaign_sha256": "a" * 64}, Path("unused.json"))
