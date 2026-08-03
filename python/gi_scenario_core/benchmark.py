"""Hidden-oracle search benchmarks for exact SWMM result tables.

The benchmark never asks a search method to predict an engineering result.  It
reveals a row from an immutable, direct-SWMM dataset only after the method has
spent one design evaluation.  This lets W&B compare search efficiency without
re-running the same hydraulic universe for every method and seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .contracts import CandidateSlot
from .search import nondominated_indices, propose_adaptive, qmc_vectors

Method = Literal["random", "sobol", "nsga2", "sobol_extratrees"]

NSGA2_POPULATION_SIZE = 64
NSGA2_OFFSPRING_BATCH_SIZE = 32
NSGA2_CROSSOVER_PROBABILITY = 0.9


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExactBenchmarkRow:
    design_id: str
    vector: tuple[int, ...]
    metrics: dict[str, float]
    feasible: bool
    adverse: bool

    @classmethod
    def parse(cls, raw: dict[str, Any], metric_names: Sequence[str]) -> ExactBenchmarkRow:
        metrics = {name: float(raw["metrics"][name]) for name in metric_names}
        return cls(
            design_id=str(raw["design_id"]),
            vector=tuple(int(value) for value in raw["vector"]),
            metrics=metrics,
            feasible=bool(raw.get("feasible", True)),
            adverse=bool(raw.get("adverse", False)),
        )


class HiddenExactOracle:
    """Reveal exact rows only after an explicit, unique design request."""

    def __init__(self, rows: Iterable[ExactBenchmarkRow]) -> None:
        parsed = list(rows)
        if not parsed:
            raise ValueError("benchmark dataset contains no exact rows")
        self._rows = {item.vector: item for item in parsed}
        if len(self._rows) != len(parsed):
            raise ValueError("benchmark dataset contains duplicate design vectors")
        self._revealed: list[ExactBenchmarkRow] = []
        self._seen: set[tuple[int, ...]] = set()

    @property
    def spent(self) -> int:
        return len(self._revealed)

    @property
    def revealed(self) -> tuple[ExactBenchmarkRow, ...]:
        return tuple(self._revealed)

    def evaluate(self, vector: Sequence[int]) -> ExactBenchmarkRow:
        key = tuple(int(value) for value in vector)
        if key in self._seen:
            raise ValueError(f"design vector was requested twice: {key}")
        try:
            row = self._rows[key]
        except KeyError as exc:
            raise ValueError(
                f"design vector is not in the exact benchmark universe: {key}"
            ) from exc
        self._seen.add(key)
        self._revealed.append(row)
        return row


def load_benchmark(path: str | Path) -> tuple[dict[str, Any], list[ExactBenchmarkRow]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "2.0":
        raise ValueError("benchmark schema_version must be 2.0")
    metric_names = list(payload.get("objective_directions", {}))
    if len(metric_names) != 2:
        raise ValueError("the release benchmark requires exactly two objective metrics")
    rows = [ExactBenchmarkRow.parse(item, metric_names) for item in payload.get("rows", [])]
    expected = int(payload.get("universe_size", 0))
    if len(rows) != expected:
        raise ValueError(f"benchmark expected {expected} rows but contains {len(rows)}")
    if payload.get("dataset_sha256"):
        without_digest = {key: value for key, value in payload.items() if key != "dataset_sha256"}
        if payload["dataset_sha256"] != _canonical_sha256(without_digest):
            raise ValueError("benchmark dataset digest does not match its contents")
    return payload, rows


def _minimized(row: ExactBenchmarkRow, directions: dict[str, str]) -> tuple[float, float]:
    values: list[float] = []
    for name, direction in directions.items():
        value = row.metrics[name]
        values.append(-value if direction in {"maximize", "at_least"} else value)
    return values[0], values[1]


def _frontier(
    rows: Sequence[ExactBenchmarkRow], directions: dict[str, str]
) -> list[ExactBenchmarkRow]:
    feasible = [item for item in rows if item.feasible]
    if not feasible:
        return []
    indices = nondominated_indices([item.metrics for item in feasible], directions)
    return [feasible[index] for index in indices]


def _hypervolume_2d(
    rows: Sequence[ExactBenchmarkRow],
    directions: dict[str, str],
    reference: tuple[float, float],
) -> float:
    points = sorted({_minimized(item, directions) for item in _frontier(rows, directions)})
    if not points:
        return 0.0
    area = 0.0
    best_y = reference[1]
    for x_value, y_value in points:
        if x_value >= reference[0] or y_value >= best_y:
            continue
        area += (reference[0] - x_value) * (best_y - y_value)
        best_y = y_value
    return max(0.0, area)


def score_replay(
    truth: Sequence[ExactBenchmarkRow],
    observed: Sequence[ExactBenchmarkRow],
    directions: dict[str, str],
) -> dict[str, float | int]:
    truth_frontier = _frontier(truth, directions)
    observed_frontier = _frontier(observed, directions)
    truth_points = [_minimized(item, directions) for item in truth if item.feasible]
    if not truth_points:
        reference = (1.0, 1.0)
    else:
        xs, ys = zip(*truth_points, strict=True)
        x_span = max(xs) - min(xs) or 1.0
        y_span = max(ys) - min(ys) or 1.0
        reference = (max(xs) + 0.05 * x_span, max(ys) + 0.05 * y_span)
    truth_hv = _hypervolume_2d(truth_frontier, directions, reference)
    observed_hv = _hypervolume_2d(observed_frontier, directions, reference)
    truth_frontier_ids = {item.design_id for item in truth_frontier}
    observed_ids = {item.design_id for item in observed}
    feasible_truth = {item.design_id for item in truth if item.feasible}
    adverse_truth = {item.design_id for item in truth if item.adverse}
    epsilon = _epsilon_indicator(truth_frontier, observed_frontier, directions)
    return {
        "exact_designs": len(observed),
        "exact_job_equivalent": len(observed) * 4,
        "frontier_size": len(observed_frontier),
        "hypervolume_ratio": 1.0 if truth_hv == 0 else min(1.0, observed_hv / truth_hv),
        "epsilon_indicator": epsilon,
        "frontier_recall": (
            1.0
            if not truth_frontier_ids
            else len(truth_frontier_ids & observed_ids) / len(truth_frontier_ids)
        ),
        "feasible_recall": (
            1.0 if not feasible_truth else len(feasible_truth & observed_ids) / len(feasible_truth)
        ),
        "adverse_recall": (
            1.0 if not adverse_truth else len(adverse_truth & observed_ids) / len(adverse_truth)
        ),
    }


def _epsilon_indicator(
    truth: Sequence[ExactBenchmarkRow],
    observed: Sequence[ExactBenchmarkRow],
    directions: dict[str, str],
) -> float:
    """Normalized additive epsilon; zero means the observed front covers truth."""

    if not truth:
        return 0.0
    if not observed:
        return 1.0
    truth_points = [_minimized(item, directions) for item in truth]
    observed_points = [_minimized(item, directions) for item in observed]
    spans = []
    for dimension in range(2):
        values = [point[dimension] for point in truth_points]
        spans.append(max(values) - min(values) or 1.0)
    return max(
        min(
            max(
                (candidate[dimension] - target[dimension]) / spans[dimension]
                for dimension in range(2)
            )
            for candidate in observed_points
        )
        for target in truth_points
    )


def _candidate_order(
    payload: dict[str, Any],
    rows: Sequence[ExactBenchmarkRow],
    *,
    method: Method,
    seed: int,
) -> list[tuple[int, ...]]:
    if method == "random":
        order = [item.vector for item in rows]
        random.Random(seed).shuffle(order)
        return order
    slots = [CandidateSlot.model_validate(slot) for slot in payload.get("candidate_slots", [])]
    vectors, _engine = qmc_vectors(slots, len(rows), seed)
    order = [tuple(vector) for vector in vectors]
    exact_vectors = {item.vector for item in rows}
    if set(order) != exact_vectors:
        raise ValueError("Sobol discretization did not cover the exact benchmark universe")
    return order


def _row_key(row: ExactBenchmarkRow) -> tuple[tuple[int, ...], str]:
    """Canonical ordering used at every NSGA-II batch barrier."""

    return row.vector, row.design_id


def _constraint_dominates(
    first: ExactBenchmarkRow,
    second: ExactBenchmarkRow,
    directions: dict[str, str],
) -> bool:
    """Return feasibility-first Pareto domination for the binary benchmark constraint.

    The exhaustive benchmark records only a feasible/infeasible disposition, not
    a continuous constraint-violation magnitude.  Deb's feasibility rule is
    therefore applied at the available resolution: any feasible design dominates
    an infeasible design, while designs with the same disposition are compared by
    their declared objectives.
    """

    if first.feasible != second.feasible:
        return first.feasible
    first_values = _minimized(first, directions)
    second_values = _minimized(second, directions)
    return all(
        first_value <= second_value
        for first_value, second_value in zip(first_values, second_values, strict=True)
    ) and any(
        first_value < second_value
        for first_value, second_value in zip(first_values, second_values, strict=True)
    )


def _nondominated_fronts(
    rows: Sequence[ExactBenchmarkRow], directions: dict[str, str]
) -> list[list[ExactBenchmarkRow]]:
    """Fast-enough deterministic nondominated sorting for NSGA-II populations."""

    ordered = sorted(rows, key=_row_key)
    if not ordered:
        return []
    dominates: list[list[int]] = [[] for _ in ordered]
    domination_counts = [0 for _ in ordered]
    for first_index, first in enumerate(ordered):
        for second_index in range(first_index + 1, len(ordered)):
            second = ordered[second_index]
            if _constraint_dominates(first, second, directions):
                dominates[first_index].append(second_index)
                domination_counts[second_index] += 1
            elif _constraint_dominates(second, first, directions):
                dominates[second_index].append(first_index)
                domination_counts[first_index] += 1

    current = [index for index, count in enumerate(domination_counts) if count == 0]
    fronts: list[list[ExactBenchmarkRow]] = []
    while current:
        fronts.append([ordered[index] for index in current])
        next_front: list[int] = []
        for index in current:
            for dominated_index in dominates[index]:
                domination_counts[dominated_index] -= 1
                if domination_counts[dominated_index] == 0:
                    next_front.append(dominated_index)
        current = sorted(next_front, key=lambda index: _row_key(ordered[index]))
    return fronts


def _crowding_distances(
    front: Sequence[ExactBenchmarkRow], directions: dict[str, str]
) -> dict[tuple[int, ...], float]:
    """Compute standard normalized NSGA-II crowding distance for one front."""

    distances = {row.vector: 0.0 for row in front}
    if len(front) <= 2:
        return {row.vector: math.inf for row in front}
    for metric, direction in directions.items():
        sign = -1.0 if direction in {"maximize", "at_least"} else 1.0
        ordered = sorted(
            front,
            key=lambda row: (sign * row.metrics[metric], _row_key(row)),
        )
        minimum = sign * ordered[0].metrics[metric]
        maximum = sign * ordered[-1].metrics[metric]
        if maximum == minimum:
            continue
        distances[ordered[0].vector] = math.inf
        distances[ordered[-1].vector] = math.inf
        span = maximum - minimum
        for index in range(1, len(ordered) - 1):
            if math.isinf(distances[ordered[index].vector]):
                continue
            previous_value = sign * ordered[index - 1].metrics[metric]
            next_value = sign * ordered[index + 1].metrics[metric]
            distances[ordered[index].vector] += (next_value - previous_value) / span
    return distances


def _rank_and_crowding(
    rows: Sequence[ExactBenchmarkRow], directions: dict[str, str]
) -> dict[tuple[int, ...], tuple[int, float]]:
    ranked: dict[tuple[int, ...], tuple[int, float]] = {}
    for rank, front in enumerate(_nondominated_fronts(rows, directions)):
        crowding = _crowding_distances(front, directions)
        for row in front:
            ranked[row.vector] = rank, crowding[row.vector]
    return ranked


def _environmental_survival(
    rows: Sequence[ExactBenchmarkRow],
    directions: dict[str, str],
    population_size: int,
) -> list[ExactBenchmarkRow]:
    """Apply canonical NSGA-II front filling and crowding-distance truncation."""

    if population_size < 1:
        return []
    unique = {row.vector: row for row in rows}
    survivors: list[ExactBenchmarkRow] = []
    for front in _nondominated_fronts(list(unique.values()), directions):
        capacity = population_size - len(survivors)
        if capacity <= 0:
            break
        if len(front) <= capacity:
            survivors.extend(sorted(front, key=_row_key))
            continue
        crowding = _crowding_distances(front, directions)
        survivors.extend(
            sorted(
                front,
                key=lambda row: (-crowding[row.vector], _row_key(row)),
            )[:capacity]
        )
        break
    return survivors


def _tournament_winner(
    first: ExactBenchmarkRow,
    second: ExactBenchmarkRow,
    rank_and_crowding: dict[tuple[int, ...], tuple[int, float]],
    generator: random.Random,
) -> ExactBenchmarkRow:
    first_rank, first_crowding = rank_and_crowding[first.vector]
    second_rank, second_crowding = rank_and_crowding[second.vector]
    if first_rank != second_rank:
        return first if first_rank < second_rank else second
    if first_crowding != second_crowding:
        return first if first_crowding > second_crowding else second
    return first if generator.random() < 0.5 else second


def _binary_tournament(
    population: Sequence[ExactBenchmarkRow],
    rank_and_crowding: dict[tuple[int, ...], tuple[int, float]],
    generator: random.Random,
) -> ExactBenchmarkRow:
    if len(population) == 1:
        return population[0]
    first, second = generator.sample(list(population), 2)
    return _tournament_winner(first, second, rank_and_crowding, generator)


def _integer_crossover(
    first: tuple[int, ...],
    second: tuple[int, ...],
    generator: random.Random,
    *,
    probability: float,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Uniform integer crossover; genes always remain on the canonical lattice."""

    if generator.random() >= probability:
        return first, second
    child_one: list[int] = []
    child_two: list[int] = []
    for first_value, second_value in zip(first, second, strict=True):
        if generator.random() < 0.5:
            child_one.append(first_value)
            child_two.append(second_value)
        else:
            child_one.append(second_value)
            child_two.append(first_value)
    return tuple(child_one), tuple(child_two)


def _integer_mutation(
    vector: tuple[int, ...],
    slots: Sequence[CandidateSlot],
    generator: random.Random,
    *,
    probability: float,
) -> tuple[int, ...]:
    """Bounded random-reset mutation on the finite integer design lattice."""

    mutated = list(vector)
    for index, slot in enumerate(slots):
        if generator.random() >= probability:
            continue
        cardinality = slot.max_additional_units - slot.min_additional_units + 1
        if cardinality <= 1:
            continue
        offset = generator.randrange(cardinality - 1)
        candidate = slot.min_additional_units + offset
        if candidate >= mutated[index]:
            candidate += 1
        mutated[index] = candidate
    return tuple(mutated)


def _nsga2_candidates(
    payload: dict[str, Any],
    population: Sequence[ExactBenchmarkRow],
    seen: set[tuple[int, ...]],
    *,
    seed: int,
    count: int,
) -> tuple[list[tuple[int, ...]], int, str]:
    """Propose one deterministic, deduplicated integer NSGA-II offspring batch.

    Only the survivor population of already revealed exact rows enters selection.
    The returned fill count identifies candidates supplied by deterministic QMC
    after bounded evolutionary attempts cannot produce enough unseen vectors.
    """

    slots = [CandidateSlot.model_validate(slot) for slot in payload["candidate_slots"]]
    directions = {key: str(value) for key, value in payload["objective_directions"].items()}
    generator = random.Random(seed)
    proposed: list[tuple[int, ...]] = []
    proposed_set: set[tuple[int, ...]] = set()

    def accept(vector: Sequence[int]) -> None:
        key = tuple(int(value) for value in vector)
        if key not in seen and key not in proposed_set:
            proposed.append(key)
            proposed_set.add(key)

    if population:
        ordered_population = sorted(population, key=_row_key)
        ranked = _rank_and_crowding(ordered_population, directions)
        mutation_probability = 1.0 / max(1, len(slots))
        attempts = 0
        maximum_attempts = max(256, count * 128)
        while len(proposed) < count and attempts < maximum_attempts:
            attempts += 1
            first = _binary_tournament(ordered_population, ranked, generator)
            second = _binary_tournament(ordered_population, ranked, generator)
            children = _integer_crossover(
                first.vector,
                second.vector,
                generator,
                probability=NSGA2_CROSSOVER_PROBABILITY,
            )
            for child in children:
                accept(
                    _integer_mutation(
                        child,
                        slots,
                        generator,
                        probability=mutation_probability,
                    )
                )
                if len(proposed) >= count:
                    break
    evolutionary_count = len(proposed)
    fill_engine = "none"
    if len(proposed) < count:
        qmc, fill_engine = qmc_vectors(
            slots,
            count - len(proposed),
            seed,
            exclude=seen | proposed_set,
        )
        for vector in qmc:
            accept(vector)
    selected = proposed[:count]
    return selected, len(selected) - evolutionary_count, fill_engine


def _record_history(
    history: list[dict[str, Any]],
    rows: Sequence[ExactBenchmarkRow],
    observed: Sequence[ExactBenchmarkRow],
    directions: dict[str, str],
    *,
    budget: int,
    exploration: int,
    adaptive: int,
) -> None:
    step = len(observed)
    if step == budget or step & (step - 1) == 0:
        history.append(
            {
                "step": step,
                "exploration_designs": exploration,
                "adaptive_designs": adaptive,
                **score_replay(rows, observed, directions),
            }
        )


def run_replay(
    payload: dict[str, Any],
    rows: Sequence[ExactBenchmarkRow],
    *,
    method: Method,
    seed: int,
    budget: int,
) -> dict[str, Any]:
    if budget < 1:
        raise ValueError("benchmark budget must be positive")
    budget = min(budget, len(rows))
    directions = {key: str(value) for key, value in payload["objective_directions"].items()}
    oracle = HiddenExactOracle(rows)
    history: list[dict[str, Any]] = []
    exploration = 0
    adaptive = 0
    nsga2_population: list[ExactBenchmarkRow] = []
    nsga2_generations: list[dict[str, Any]] = []
    if method in {"random", "sobol"}:
        for vector in _candidate_order(payload, rows, method=method, seed=seed)[:budget]:
            oracle.evaluate(vector)
            exploration += 1
            _record_history(
                history,
                rows,
                oracle.revealed,
                directions,
                budget=budget,
                exploration=exploration,
                adaptive=adaptive,
            )
    else:
        slots = [CandidateSlot.model_validate(slot) for slot in payload["candidate_slots"]]
        initial_count = min(NSGA2_POPULATION_SIZE, budget)
        initial, _engine = qmc_vectors(slots, initial_count, seed)
        for vector in initial:
            oracle.evaluate(vector)
            exploration += 1
            _record_history(
                history,
                rows,
                oracle.revealed,
                directions,
                budget=budget,
                exploration=exploration,
                adaptive=adaptive,
            )
        if method == "nsga2":
            nsga2_population = _environmental_survival(
                oracle.revealed,
                directions,
                initial_count,
            )
            nsga2_generations.append(
                {
                    "generation": 0,
                    "seed": seed,
                    "initialization_designs": initial_count,
                    "initialization_engine": _engine,
                    "offspring_design_ids": [],
                    "qmc_fill_designs": 0,
                    "qmc_fill_engine": "none",
                    "population_design_ids": [row.design_id for row in nsga2_population],
                }
            )
        batch_index = 0
        while oracle.spent < budget:
            batch_index += 1
            batch_count = min(NSGA2_OFFSPRING_BATCH_SIZE, budget - oracle.spent)
            if method == "nsga2":
                candidates, qmc_fill_count, qmc_fill_engine = _nsga2_candidates(
                    payload,
                    nsga2_population,
                    {row.vector for row in oracle.revealed},
                    seed=seed + batch_index,
                    count=batch_count,
                )
                sources = ["adaptive"] * (len(candidates) - qmc_fill_count) + [
                    "exploration"
                ] * qmc_fill_count
            else:
                exact_rows = [
                    {
                        "vector": list(item.vector),
                        "metrics": item.metrics,
                        "qa_status": "accepted",
                    }
                    for item in oracle.revealed
                ]
                proposed, diagnostics = propose_adaptive(
                    slots,
                    exact_rows,
                    objective_directions=directions,
                    count=batch_count,
                    seed=seed + batch_index,
                    exploration_fraction=0.25,
                    benchmark_approved=True,
                )
                candidates = [tuple(item.vector) for item in proposed]
                exploration_in_batch = int(diagnostics.get("exploration_count", len(candidates)))
                sources = ["adaptive"] * max(0, len(candidates) - exploration_in_batch) + [
                    "exploration"
                ] * min(exploration_in_batch, len(candidates))
            if not candidates:
                raise RuntimeError("adaptive benchmark exhausted before reaching its budget")
            exact_batch: list[ExactBenchmarkRow] = []
            for vector, source in zip(candidates, sources, strict=True):
                exact_batch.append(oracle.evaluate(vector))
                if source == "exploration":
                    exploration += 1
                else:
                    adaptive += 1
                _record_history(
                    history,
                    rows,
                    oracle.revealed,
                    directions,
                    budget=budget,
                    exploration=exploration,
                    adaptive=adaptive,
                )
            if method == "nsga2":
                nsga2_population = _environmental_survival(
                    [*nsga2_population, *exact_batch],
                    directions,
                    initial_count,
                )
                nsga2_generations.append(
                    {
                        "generation": batch_index,
                        "seed": seed + batch_index,
                        "initialization_designs": 0,
                        "initialization_engine": "none",
                        "offspring_design_ids": [row.design_id for row in exact_batch],
                        "qmc_fill_designs": qmc_fill_count,
                        "qmc_fill_engine": qmc_fill_engine,
                        "population_design_ids": [row.design_id for row in nsga2_population],
                    }
                )
    result = {
        "schema_version": "2.0",
        "dataset_id": payload["dataset_id"],
        "dataset_sha256": payload.get("dataset_sha256"),
        "method": method,
        "seed": seed,
        "budget": budget,
        "history": history,
        "summary": score_replay(rows, oracle.revealed, directions),
        "selection_counts": {"exploration": exploration, "adaptive": adaptive},
        "selected_design_ids": [item.design_id for item in oracle.revealed],
    }
    if method == "nsga2":
        result["method_configuration"] = {
            "variant": "deterministic_batch_barriered_mu_plus_lambda_nsga2",
            "population_size": initial_count,
            "offspring_batch_size": NSGA2_OFFSPRING_BATCH_SIZE,
            "initialization": f"qmc:{_engine}",
            "selection": "binary_tournament_pareto_rank_then_crowding",
            "constraint_handling": "binary_feasibility_first_then_pareto",
            "crossover": "bounded_integer_uniform",
            "crossover_probability": NSGA2_CROSSOVER_PROBABILITY,
            "mutation": "bounded_integer_random_reset",
            "mutation_probability": 1.0 / max(1, len(slots)),
            "survival": "mu_plus_lambda_front_fill_then_crowding",
            "deduplication": "canonical_integer_vector_before_oracle_request",
            "operator_exhaustion_fill": "deterministic_qmc_unseen_vectors",
            "generation_count": len(nsga2_generations) - 1,
        }
        result["nsga2_generations"] = nsga2_generations
    result["campaign_sha256"] = _canonical_sha256(result)
    return result


def _sync_wandb(result: dict[str, Any], output: Path) -> str:
    try:
        import wandb
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError("install the observability extra to sync benchmark results") from exc
    from .observability import validate_binding_from_environment

    entity = os.environ.get("WANDB_ENTITY", "")
    project = os.environ.get("WANDB_PROJECT", "")
    if not entity or not project:
        raise RuntimeError("WANDB_RESTRICTED_DESTINATION_REQUIRED")
    validate_binding_from_environment(
        entity=entity,
        project=project,
        deployment=os.environ.get("WANDB_DEPLOYMENT", "wandb_cloud"),
        project_id=f"benchmark-{result['campaign_sha256'][:16]}",
    )
    config = {key: result[key] for key in ("dataset_id", "method", "seed", "budget")}
    if "method_configuration" in result:
        config["method_configuration"] = result["method_configuration"]
    with wandb.init(
        entity=entity,
        project=project,
        job_type="search_benchmark",
        config=config,
        settings=wandb.Settings(
            console="off",
            disable_code=True,
            save_code=False,
            show_errors=False,
        ),
    ) as run:
        for row in result["history"]:
            run.log(row, step=int(row["step"]))
        run.summary.update(result["summary"])
        artifact = wandb.Artifact(
            name=f"search-trace-{result['campaign_sha256'][:12]}", type="search-trace"
        )
        artifact.add_file(str(output), name="search-trace.json")
        run.log_artifact(artifact)
        return str(run.url)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--method",
        choices=("random", "sobol", "nsga2", "sobol_extratrees"),
        required=True,
    )
    parser.add_argument("--budget", type=int, required=True)
    parser.add_argument("--seed", type=int, default=424242)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wandb", action="store_true", help="sync the committed result to W&B")
    arguments = parser.parse_args(argv)
    payload, rows = load_benchmark(arguments.dataset)
    result = run_replay(
        payload, rows, method=arguments.method, seed=arguments.seed, budget=arguments.budget
    )
    output = arguments.output or Path(
        f"benchmark-{arguments.method}-seed-{arguments.seed}-budget-{arguments.budget}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    if arguments.wandb:
        print(_sync_wandb(result, output))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
