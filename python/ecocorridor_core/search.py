"""Bounded categorical portfolio search.

ML is only an ordering aid.  This module never returns predicted ecological
metrics; selected portfolios still require their complete direct scenario
ensemble before they become comparable.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterable

from pydantic import Field, model_validator

from .contracts import (
    ContractModel,
    InterventionOption,
    InterventionSite,
    PortfolioSelection,
)
from .interventions import canonicalize_portfolio


class ScientificSearchDependencyError(RuntimeError):
    pass


class PortfolioCandidate(ContractModel):
    portfolio: PortfolioSelection
    sequence: int = Field(ge=0)
    batch: int = Field(ge=0)
    source: str


class ConnectivitySearchPlan(ContractModel):
    method: str
    qmc_engine: str | None
    scenario_count: int = Field(gt=0)
    max_direct_jobs: int = Field(gt=0)
    finite_universe_size: int = Field(gt=0)
    portfolio_capacity: int = Field(gt=0)
    planned_direct_jobs: int = Field(gt=0)
    seed: int = Field(ge=0)
    candidates: list[PortfolioCandidate]

    @model_validator(mode="after")
    def validate_arithmetic(self) -> ConnectivitySearchPlan:
        if self.planned_direct_jobs != len(self.candidates) * self.scenario_count:
            raise ValueError("planned direct-job arithmetic is inconsistent")
        if self.planned_direct_jobs > self.max_direct_jobs:
            raise ValueError("search plan exceeds its direct-job cap")
        return self


class ObservedPortfolio(ContractModel):
    portfolio: PortfolioSelection
    objective_values: list[float] = Field(min_length=1)
    feasible: bool
    adverse_found: bool
    complete_scenario_ensemble: bool


class BenchmarkGateResult(ContractModel):
    landscape_count: int = Field(ge=0)
    seeds_per_landscape: int = Field(ge=0)
    median_hypervolume_improvement_percent_by_budget: dict[str, float]
    tenth_percentile_regression_percent: float
    feasible_recall_delta_percent: float
    safeguard_adverse_recall_percent: float

    @property
    def production_enabled(self) -> bool:
        qualifying_budgets = sum(
            value >= 5.0 for value in self.median_hypervolume_improvement_percent_by_budget.values()
        )
        return (
            self.landscape_count >= 3
            and self.seeds_per_landscape >= 10
            and qualifying_budgets >= 2
            and self.tenth_percentile_regression_percent <= 2.0
            and self.feasible_recall_delta_percent >= 0.0
            and self.safeguard_adverse_recall_percent >= 95.0
        )


def _catalog(
    sites: Iterable[InterventionSite], options: Iterable[InterventionOption]
) -> tuple[list[InterventionSite], dict[str, InterventionOption], list[list[str]]]:
    ordered_sites = sorted(sites, key=lambda item: item.site_id)
    options_by_id = {item.option_id: item for item in options}
    dimensions: list[list[str]] = []
    for site in ordered_sites:
        choices = sorted(site.option_ids)
        if not choices:
            raise ValueError(f"site {site.site_id} has no choices")
        for choice in choices:
            if choice not in options_by_id or options_by_id[choice].site_id != site.site_id:
                raise ValueError(f"site {site.site_id} references invalid option {choice}")
        dimensions.append(choices)
    if not dimensions:
        raise ValueError("search requires at least one reviewed intervention site")
    return ordered_sites, options_by_id, dimensions


def enumerate_portfolios(
    sites: Iterable[InterventionSite],
    options: Iterable[InterventionOption],
    *,
    is_valid: Callable[[dict[str, str]], bool] | None = None,
) -> list[PortfolioSelection]:
    ordered_sites, options_by_id, dimensions = _catalog(sites, options)
    valid = is_valid or (lambda _: True)
    result: list[PortfolioSelection] = []
    for vector in itertools.product(*dimensions):
        selections = {
            site.site_id: option_id for site, option_id in zip(ordered_sites, vector, strict=True)
        }
        if valid(selections):
            result.append(canonicalize_portfolio(ordered_sites, options_by_id.values(), selections))
    return result


def _sobol_portfolios(
    sites: list[InterventionSite],
    options: dict[str, InterventionOption],
    dimensions: list[list[str]],
    *,
    count: int,
    seed: int,
    exclude: set[str],
    repair: Callable[[dict[str, str]], dict[str, str] | None] | None,
) -> list[PortfolioSelection]:
    try:
        from scipy.stats import qmc
    except ImportError as error:  # pragma: no cover - dependency gate in packaging
        raise ScientificSearchDependencyError(
            "scrambled Sobol search requires the pinned SciPy science dependency"
        ) from error
    target = count
    if target <= 0:
        return []
    exponent = max(6, math.ceil(math.log2(max(1, target * 16))))
    points = qmc.Sobol(d=len(dimensions), scramble=True, seed=seed).random_base2(exponent)
    selected: list[PortfolioSelection] = []
    seen = set(exclude)
    for point in points:
        selections = {
            site.site_id: choices[min(int(value * len(choices)), len(choices) - 1)]
            for site, choices, value in zip(sites, dimensions, point, strict=True)
        }
        if repair:
            selections = repair(dict(selections))
            if selections is None:
                continue
        portfolio = canonicalize_portfolio(sites, options.values(), selections)
        if portfolio.portfolio_id in seen:
            continue
        seen.add(portfolio.portfolio_id)
        selected.append(portfolio)
        if len(selected) == target:
            return selected
    raise RuntimeError(
        "scrambled Sobol projection could not fill the requested unique categorical budget; "
        "reduce the budget or enumerate the constrained universe"
    )


def make_search_plan(
    *,
    sites: list[InterventionSite],
    options: list[InterventionOption],
    baseline: PortfolioSelection,
    scenario_count: int,
    max_direct_jobs: int,
    seed: int,
    is_valid: Callable[[dict[str, str]], bool] | None = None,
    repair: Callable[[dict[str, str]], dict[str, str] | None] | None = None,
) -> ConnectivitySearchPlan:
    if scenario_count <= 0:
        raise ValueError("scenario_count must be positive")
    capacity = max_direct_jobs // scenario_count
    if capacity < 1:
        raise ValueError("direct-job cap cannot run the baseline ensemble")
    ordered_sites, options_by_id, dimensions = _catalog(sites, options)
    universe = enumerate_portfolios(ordered_sites, options_by_id.values(), is_valid=is_valid)
    universe_by_id = {item.portfolio_id: item for item in universe}
    if baseline.portfolio_id not in universe_by_id:
        raise ValueError("baseline is not a valid member of the reviewed portfolio universe")
    selected: list[PortfolioSelection]
    qmc_engine: str | None = None
    if len(universe) <= capacity:
        selected = [baseline] + [
            item for item in universe if item.portfolio_id != baseline.portfolio_id
        ]
        method = "ENUMERATION"
    else:
        qmc_engine = "scipy_sobol_scrambled"
        sampled = _sobol_portfolios(
            ordered_sites,
            options_by_id,
            dimensions,
            count=capacity - 1,
            seed=seed,
            exclude={baseline.portfolio_id},
            repair=repair,
        )
        # Constraint repair may create a technically valid selection outside the
        # enumerated callback universe.  Refuse it rather than changing constraints.
        invalid = [item.portfolio_id for item in sampled if item.portfolio_id not in universe_by_id]
        if invalid:
            raise ValueError(f"constraint repair produced unapproved portfolios: {invalid}")
        selected = [baseline, *sampled]
        method = "SOBOL"
    candidates = [
        PortfolioCandidate(
            portfolio=item,
            sequence=index,
            batch=0,
            source="BASELINE" if index == 0 else method,
        )
        for index, item in enumerate(selected)
    ]
    return ConnectivitySearchPlan(
        method=method,
        qmc_engine=qmc_engine,
        scenario_count=scenario_count,
        max_direct_jobs=max_direct_jobs,
        finite_universe_size=len(universe),
        portfolio_capacity=capacity,
        planned_direct_jobs=len(candidates) * scenario_count,
        seed=seed,
        candidates=candidates,
    )


def nondominated_indices(rows: list[list[float]]) -> list[int]:
    """Return minimization-front indices with stable input ordering."""

    if not rows:
        return []
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ValueError("objective rows must be nonempty and rectangular")
    front: list[int] = []
    for index, row in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            if all(left <= right for left, right in zip(other, row, strict=True)) and any(
                left < right for left, right in zip(other, row, strict=True)
            ):
                dominated = True
                break
        if not dominated:
            front.append(index)
    return front


def propose_extratrees_batch(
    *,
    sites: list[InterventionSite],
    options: list[InterventionOption],
    observed: list[ObservedPortfolio],
    candidates: list[PortfolioSelection],
    batch_size: int,
    seed: int,
    batch: int,
) -> list[PortfolioCandidate]:
    """Choose a batch without returning or persisting surrogate predictions."""

    complete = sorted(
        (item for item in observed if item.complete_scenario_ensemble),
        key=lambda item: item.portfolio.portfolio_id,
    )
    if len({item.portfolio.portfolio_id for item in complete}) < 64:
        raise ValueError("ExtraTrees requires at least 64 unique complete direct portfolios")
    if batch_size <= 0:
        return []
    observed_ids = {item.portfolio.portfolio_id for item in complete}
    unseen = [item for item in candidates if item.portfolio_id not in observed_ids]
    if len(unseen) < batch_size:
        raise ValueError("not enough unseen portfolios to fill the adaptive batch")
    try:
        import numpy as np
        from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
    except ImportError as error:  # pragma: no cover - dependency gate in packaging
        raise ScientificSearchDependencyError(
            "adaptive search requires pinned NumPy/scikit-learn"
        ) from error
    ordered_sites, _, dimensions = _catalog(sites, options)
    offsets: dict[tuple[str, str], int] = {}
    cursor = 0
    for site, choices in zip(ordered_sites, dimensions, strict=True):
        for choice in choices:
            offsets[(site.site_id, choice)] = cursor
            cursor += 1

    def encode(portfolio: PortfolioSelection) -> list[float]:
        row = [0.0] * cursor
        for site_id, option_id in portfolio.selections.items():
            row[offsets[(site_id, option_id)]] = 1.0
        return row

    train_x = np.asarray([encode(item.portfolio) for item in complete], dtype=float)
    objective_width = len(complete[0].objective_values)
    if any(len(item.objective_values) != objective_width for item in complete):
        raise ValueError("observed objective vectors must be rectangular")
    train_y = np.asarray([item.objective_values for item in complete], dtype=float)
    candidate_x = np.asarray([encode(item) for item in unseen], dtype=float)
    regressors = []
    predicted_columns = []
    uncertainty_columns = []
    for column in range(objective_width):
        model = ExtraTreesRegressor(n_estimators=128, random_state=seed + column, n_jobs=1)
        model.fit(train_x, train_y[:, column])
        regressors.append(model)
        tree_predictions = np.asarray([tree.predict(candidate_x) for tree in model.estimators_])
        predicted_columns.append(tree_predictions.mean(axis=0))
        uncertainty_columns.append(tree_predictions.std(axis=0))
    predicted = np.column_stack(predicted_columns)
    uncertainty = np.column_stack(uncertainty_columns).mean(axis=1)

    def positive_probability(values: list[bool], offset: int) -> np.ndarray:
        labels = np.asarray(values, dtype=int)
        if len(set(labels.tolist())) == 1:
            return np.full(len(unseen), float(labels[0]))
        classifier = ExtraTreesClassifier(n_estimators=128, random_state=seed + offset, n_jobs=1)
        classifier.fit(train_x, labels)
        class_index = list(classifier.classes_).index(1)
        return classifier.predict_proba(candidate_x)[:, class_index]

    feasible_probability = positive_probability([item.feasible for item in complete], 1000)
    adverse_probability = positive_probability([item.adverse_found for item in complete], 2000)
    front = set(nondominated_indices(predicted.tolist()))
    adaptive_count = batch_size - math.ceil(batch_size * 0.25)
    acquisition_order = sorted(
        range(len(unseen)),
        key=lambda index: (
            index not in front,
            -float(feasible_probability[index]),
            -float(adverse_probability[index]),
            -float(uncertainty[index]),
            unseen[index].portfolio_id,
        ),
    )
    adaptive_indexes = acquisition_order[:adaptive_count]
    selected_ids = {unseen[index].portfolio_id for index in adaptive_indexes}
    # The reserved exploration fraction is deterministic over unseen evidence
    # identities.  The parent search plan supplies these in scrambled-Sobol order.
    exploration = [item for item in unseen if item.portfolio_id not in selected_ids][
        : batch_size - adaptive_count
    ]
    selected = [unseen[index] for index in adaptive_indexes] + exploration
    return [
        PortfolioCandidate(
            portfolio=item,
            sequence=index,
            batch=batch,
            source="EXTRATREES_ACQUISITION" if index < adaptive_count else "SOBOL_EXPLORATION",
        )
        for index, item in enumerate(selected)
    ]
