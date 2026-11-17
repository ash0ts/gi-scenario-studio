"""Domain-neutral finite categorical exploration and Pareto helpers."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Hashable, Iterable, Sequence
from enum import Enum
from typing import TypeVar

T = TypeVar("T", bound=Hashable)


class ObjectiveDirection(str, Enum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


def categorical_universe_size(categories: Sequence[Sequence[T]]) -> int:
    if not categories or any(not dimension for dimension in categories):
        raise ValueError("every categorical dimension must contain at least one option")
    if any(len(set(dimension)) != len(dimension) for dimension in categories):
        raise ValueError("categorical dimensions may not contain duplicate options")
    return math.prod(len(dimension) for dimension in categories)


def enumerate_categorical(categories: Sequence[Sequence[T]]) -> Iterable[tuple[T, ...]]:
    categorical_universe_size(categories)
    yield from itertools.product(*categories)


def _first_primes(count: int) -> list[int]:
    primes: list[int] = []
    candidate = 2
    while len(primes) < count:
        if all(candidate % divisor for divisor in primes if divisor * divisor <= candidate):
            primes.append(candidate)
        candidate += 1
    return primes


def _van_der_corput(index: int, base: int) -> float:
    denominator = 1.0
    value = 0.0
    while index:
        index, remainder = divmod(index, base)
        denominator *= base
        value += remainder / denominator
    return value


def qmc_points(count: int, dimensions: int, seed: int) -> tuple[list[list[float]], str]:
    if count < 0 or dimensions < 0:
        raise ValueError("count and dimensions must be non-negative")
    if count == 0:
        return [], "none"
    if dimensions == 0:
        return [[] for _ in range(count)], "degenerate"
    try:  # pragma: no cover - optional scientific dependency
        from scipy.stats import qmc

        exponent = math.ceil(math.log2(count))
        points = qmc.Sobol(d=dimensions, scramble=True, seed=seed).random_base2(exponent)
        return [list(map(float, point)) for point in points[:count]], "scipy_sobol_scrambled"
    except ImportError:
        primes = _first_primes(dimensions)
        generator = random.Random(seed)
        shifts = [generator.random() for _ in range(dimensions)]
        return (
            [
                [
                    (_van_der_corput(index + 1, base) + shifts[dimension]) % 1.0
                    for dimension, base in enumerate(primes)
                ]
                for index in range(count)
            ],
            "halton_cranley_patterson_fallback",
        )


def categorical_qmc(
    categories: Sequence[Sequence[T]],
    count: int,
    seed: int,
    *,
    exclude: set[tuple[T, ...]] | None = None,
    repair: Callable[[tuple[T, ...]], tuple[T, ...] | None] | None = None,
) -> tuple[list[tuple[T, ...]], str]:
    """Sample distinct categorical portfolios, then fill deterministically.

    ``repair`` may enforce cross-dimension constraints. Returning ``None`` marks
    a portfolio invalid. Repaired portfolios must still use declared options.
    """

    universe_size = categorical_universe_size(categories)
    if count < 0:
        raise ValueError("count must be non-negative")
    excluded = set(exclude or set())
    if any(
        len(portfolio) != len(categories)
        or any(value not in categories[index] for index, value in enumerate(portfolio))
        for portfolio in excluded
    ):
        raise ValueError("excluded portfolios must belong to the categorical space")
    target = min(count, max(0, universe_size - len(excluded)))
    selected: list[tuple[T, ...]] = []
    engine = "none"

    def accept(portfolio: tuple[T, ...]) -> None:
        if repair is not None:
            repaired = repair(portfolio)
            if repaired is None:
                return
            portfolio = repaired
        if len(portfolio) != len(categories) or any(
            value not in categories[index] for index, value in enumerate(portfolio)
        ):
            raise ValueError("constraint repair returned an option outside the categorical space")
        if portfolio in excluded:
            return
        excluded.add(portfolio)
        selected.append(portfolio)

    for round_index in range(5):
        points, engine = qmc_points(max(8, target * 4), len(categories), seed + round_index)
        for point in points:
            portfolio = tuple(
                dimension[min(int(value * len(dimension)), len(dimension) - 1)]
                for value, dimension in zip(point, categories, strict=True)
            )
            accept(portfolio)
            if len(selected) >= target:
                return selected, engine
    for portfolio in enumerate_categorical(categories):
        accept(portfolio)
        if len(selected) >= target:
            break
    return selected, engine


def nondominated_indices(
    rows: Sequence[dict[str, float]],
    directions: dict[str, ObjectiveDirection | str],
) -> list[int]:
    """Return stable input indices on the finite evaluated Pareto frontier."""

    if not directions:
        raise ValueError("at least one objective direction is required")
    metrics = tuple(directions)
    parsed = {metric: ObjectiveDirection(direction) for metric, direction in directions.items()}
    for row in rows:
        missing = set(metrics) - set(row)
        if missing:
            raise ValueError(f"objective row is missing metrics: {', '.join(sorted(missing))}")
        if any(not math.isfinite(float(row[metric])) for metric in metrics):
            raise ValueError("objective values must be finite")

    def minimize_value(row: dict[str, float], metric: str) -> float:
        value = float(row[metric])
        return -value if parsed[metric] is ObjectiveDirection.MAXIMIZE else value

    frontier: list[int] = []
    for index, row in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            no_worse = all(
                minimize_value(other, metric) <= minimize_value(row, metric) for metric in metrics
            )
            better = any(
                minimize_value(other, metric) < minimize_value(row, metric) for metric in metrics
            )
            if no_worse and better:
                dominated = True
                break
        if not dominated:
            frontier.append(index)
    return frontier
