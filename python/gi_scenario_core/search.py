"""Budget-aware exact design generation and optional task-local ML proposal."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from collections.abc import Iterable
from typing import Any

from .contracts import (
    CandidateSlot,
    ContractModel,
    CostBasis,
    MetricDirection,
    SearchEvent,
    SearchTrace,
    SimulationBudget,
    new_id,
)


class DesignCandidate(ContractModel):
    design_id: str
    vector: list[int]
    source: str
    sequence: int
    batch: int = 0


class SearchPlan(ContractModel):
    analysis_id: str
    method: str
    engine: str
    storm_count: int
    baseline_jobs: int
    max_solver_jobs: int
    design_capacity: int
    planned_solver_jobs: int
    finite_universe_size: int
    candidates: list[DesignCandidate]
    trace: SearchTrace
    warnings: list[str]


def design_id(vector: Iterable[int]) -> str:
    encoded = json.dumps(list(vector), separators=(",", ":")).encode("ascii")
    return "design_" + hashlib.sha256(encoded).hexdigest()[:16]


def finite_universe_size(slots: list[CandidateSlot]) -> int:
    size = 1
    for slot in slots:
        size *= slot.max_additional_units - slot.min_additional_units + 1
    return size


def enumerate_vectors(slots: list[CandidateSlot]) -> Iterable[list[int]]:
    ranges = [range(slot.min_additional_units, slot.max_additional_units + 1) for slot in slots]
    for vector in itertools.product(*ranges):
        yield list(vector)


def _first_primes(count: int) -> list[int]:
    primes: list[int] = []
    candidate = 2
    while len(primes) < count:
        if all(candidate % value for value in primes if value * value <= candidate):
            primes.append(candidate)
        candidate += 1
    return primes


def _van_der_corput(index: int, base: int) -> float:
    denominator = 1.0
    result = 0.0
    while index:
        index, remainder = divmod(index, base)
        denominator *= base
        result += remainder / denominator
    return result


def qmc_points(count: int, dimensions: int, seed: int) -> tuple[list[list[float]], str]:
    """Return deterministic low-discrepancy points.

    SciPy builds use scrambled Sobol generated in a power-of-two batch.  Lean
    builds use a deterministically shifted Halton sequence and identify it as
    such in the audit trace.
    """

    if count <= 0:
        return [], "none"
    if dimensions <= 0:
        return [[] for _ in range(count)], "degenerate"
    try:  # pragma: no cover - optional packaged dependency
        from scipy.stats import qmc

        exponent = max(0, math.ceil(math.log2(count)))
        points = qmc.Sobol(d=dimensions, scramble=True, seed=seed).random_base2(exponent)
        return [list(map(float, row)) for row in points[:count]], "scipy_sobol_scrambled"
    except ImportError:
        primes = _first_primes(dimensions)
        generator = random.Random(seed)
        shifts = [generator.random() for _ in range(dimensions)]
        points = [
            [(_van_der_corput(index + 1, base) + shifts[d]) % 1.0 for d, base in enumerate(primes)]
            for index in range(count)
        ]
        return points, "halton_cranley_patterson_fallback"


def _point_to_vector(point: list[float], slots: list[CandidateSlot]) -> list[int]:
    vector = []
    for value, slot in zip(point, slots, strict=True):
        cardinality = slot.max_additional_units - slot.min_additional_units + 1
        offset = min(int(value * cardinality), cardinality - 1)
        vector.append(slot.min_additional_units + offset)
    return vector


def qmc_vectors(
    slots: list[CandidateSlot],
    count: int,
    seed: int,
    *,
    exclude: set[tuple[int, ...]] | None = None,
) -> tuple[list[list[int]], str]:
    exclude = set(exclude or set())
    universe = finite_universe_size(slots)
    target = min(count, max(0, universe - len(exclude)))
    vectors: list[list[int]] = []
    engine = "none"
    # Discretization can collide. Generate bounded oversampling rounds, then fill
    # lexicographically to guarantee requested capacity for small finite spaces.
    offset_seed = seed
    for round_index in range(5):
        points, engine = qmc_points(max(target * 4, 8), len(slots), offset_seed + round_index)
        for point in points:
            vector = _point_to_vector(point, slots)
            key = tuple(vector)
            if key in exclude:
                continue
            exclude.add(key)
            vectors.append(vector)
            if len(vectors) >= target:
                return vectors, engine
    for vector in enumerate_vectors(slots):
        key = tuple(vector)
        if key not in exclude:
            vectors.append(vector)
            exclude.add(key)
            if len(vectors) >= target:
                break
    return vectors, engine


def make_search_plan(
    slots: list[CandidateSlot | dict[str, Any]],
    budget: SimulationBudget | dict[str, Any],
    *,
    storm_count: int,
    analysis_id: str | None = None,
    baseline_jobs: int | None = None,
) -> SearchPlan:
    parsed_slots = [CandidateSlot.model_validate(slot) for slot in slots]
    if not parsed_slots:
        raise ValueError("at least one candidate slot is required")
    if not all(slot.engineer_approved for slot in parsed_slots):
        raise ValueError("every candidate slot must be engineer-approved before search")
    budget = SimulationBudget.model_validate(budget)
    baseline = storm_count if baseline_jobs is None else baseline_jobs
    capacity = budget.design_capacity(storm_count, baseline)
    if capacity < 1:
        raise ValueError("solver-job budget cannot evaluate one full-ensemble design")
    universe = finite_universe_size(parsed_slots)
    count = min(capacity, universe)
    if universe <= capacity:
        vectors = list(enumerate_vectors(parsed_slots))
        method = "enumeration"
        engine = "exact_cartesian"
    else:
        vectors, engine = qmc_vectors(parsed_slots, count, budget.seed)
        method = "qmc"
    analysis_id = analysis_id or new_id("analysis")
    candidates = [
        DesignCandidate(
            design_id=design_id(vector),
            vector=vector,
            source=method,
            sequence=index,
        )
        for index, vector in enumerate(vectors)
    ]
    events = [
        SearchEvent(
            sequence=item.sequence,
            method=item.source,
            design_id=item.design_id,
            design_vector=item.vector,
            batch=item.batch,
        )
        for item in candidates
    ]
    warnings = []
    if universe > capacity:
        warnings.append(
            f"The finite universe has {universe:,} designs; the budget can evaluate "
            f"{capacity:,} designs across all {storm_count} storms."
        )
    return SearchPlan(
        analysis_id=analysis_id,
        method=method,
        engine=engine,
        storm_count=storm_count,
        baseline_jobs=baseline,
        max_solver_jobs=budget.max_solver_jobs,
        design_capacity=capacity,
        planned_solver_jobs=baseline + len(candidates) * storm_count,
        finite_universe_size=universe,
        candidates=candidates,
        trace=SearchTrace(
            analysis_id=analysis_id,
            seed=budget.seed,
            method=f"{method}:{engine}",
            solver_job_cap=budget.max_solver_jobs,
            storm_count=storm_count,
            events=events,
            stop_reason="finite_universe_exhausted" if universe <= capacity else "budget_planned",
        ),
        warnings=warnings,
    )


def nondominated_indices(
    rows: list[dict[str, float]],
    directions: dict[str, MetricDirection | str],
) -> list[int]:
    """Find nondominated rows; threshold directions are treated by their monotone sense."""

    if not rows:
        return []
    metrics = list(directions)

    def transformed(row: dict[str, float], metric: str) -> float:
        direction = MetricDirection(directions[metric])
        value = row[metric]
        if direction in {MetricDirection.MAXIMIZE, MetricDirection.AT_LEAST}:
            return -value
        return value

    result = []
    for index, row in enumerate(rows):
        dominated = False
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            no_worse = all(
                transformed(other, metric) <= transformed(row, metric) for metric in metrics
            )
            strictly_better = any(
                transformed(other, metric) < transformed(row, metric) for metric in metrics
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            result.append(index)
    return result


def propose_adaptive(
    slots: list[CandidateSlot | dict[str, Any]],
    exact_rows: list[dict[str, Any]],
    *,
    objective_directions: dict[str, MetricDirection | str],
    count: int,
    seed: int,
    exploration_fraction: float = 0.25,
    benchmark_approved: bool = False,
) -> tuple[list[DesignCandidate], dict[str, Any]]:
    """Propose candidates; use ExtraTrees only when eligible and installed.

    Expected exact rows contain `vector`, `metrics`, and optional `qa_status`.
    Predictions are returned only as internal diagnostics, never as results.
    """

    parsed_slots = [CandidateSlot.model_validate(slot) for slot in slots]
    seen = {tuple(int(value) for value in row["vector"]) for row in exact_rows}
    available = finite_universe_size(parsed_slots) - len(seen)
    count = min(count, max(0, available))
    if count == 0:
        return [], {"method": "exhausted", "ml_eligible": False}
    exploration_count = max(1, math.ceil(count * exploration_fraction))
    qmc, engine = qmc_vectors(parsed_slots, max(count * 32, 256), seed, exclude=seen)
    if not benchmark_approved:
        selected = qmc[:count]
        return _adaptive_candidates(selected, "qmc_production_gate"), {
            "method": "qmc_production_gate",
            "engine": engine,
            "ml_eligible": False,
            "reason": (
                "ExtraTrees remains experimental until its version passes the declared "
                "multi-network benchmark release gates"
            ),
        }
    if len(seen) < 64:
        selected = qmc[:count]
        return _adaptive_candidates(selected, "qmc_exploration"), {
            "method": "qmc_exploration",
            "engine": engine,
            "ml_eligible": False,
            "reason": "at least 64 unique exact designs are required",
        }
    try:  # pragma: no cover - optional packaged dependency
        from sklearn.ensemble import ExtraTreesRegressor
    except ImportError:
        selected = qmc[:count]
        return _adaptive_candidates(selected, "qmc_fallback"), {
            "method": "qmc_fallback",
            "engine": engine,
            "ml_eligible": True,
            "reason": "scikit-learn is unavailable",
        }
    metrics = list(objective_directions)
    valid_by_vector: dict[tuple[int, ...], dict[str, Any]] = {}
    for row in exact_rows:
        key = tuple(int(value) for value in row["vector"])
        if row.get("qa_status", "accepted") != "rejected" and all(
            metric in row.get("metrics", {}) for metric in metrics
        ):
            valid_by_vector[key] = row
    valid_rows = list(valid_by_vector.values())
    if len(valid_rows) < 64:
        selected = qmc[:count]
        return _adaptive_candidates(selected, "qmc_fallback"), {
            "method": "qmc_fallback",
            "engine": engine,
            "ml_eligible": False,
            "reason": "fewer than 64 QA-usable exact designs",
        }
    x_train = [row["vector"] for row in valid_rows]
    pool = qmc
    predictions: dict[str, list[float]] = {}
    uncertainties: dict[str, list[float]] = {}
    model_fingerprints = []
    for offset, metric in enumerate(metrics):
        y_train = [float(row["metrics"][metric]) for row in valid_rows]
        model = ExtraTreesRegressor(
            n_estimators=128,
            min_samples_leaf=2,
            max_features=1.0,
            random_state=seed + offset,
            n_jobs=1,
        )
        model.fit(x_train, y_train)
        predictions[metric] = [float(value) for value in model.predict(pool)]
        tree_predictions = [
            [float(value) for value in tree.predict(pool)] for tree in model.estimators_
        ]
        uncertainties[metric] = [
            _sample_std([tree_values[index] for tree_values in tree_predictions])
            for index in range(len(pool))
        ]
        model_fingerprints.append(
            hashlib.sha256(
                json.dumps(
                    {
                        "metric": metric,
                        "seed": seed + offset,
                        "x": x_train,
                        "y": y_train,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
    scores = [0.0] * len(pool)
    for metric in metrics:
        values = predictions[metric]
        low, high = min(values), max(values)
        scale = max(high - low, 1e-12)
        direction = MetricDirection(objective_directions[metric])
        for index, value in enumerate(values):
            normalized = (value - low) / scale
            if direction in {MetricDirection.MAXIMIZE, MetricDirection.AT_LEAST}:
                normalized = 1 - normalized
            uncertainty = uncertainties[metric][index] / scale
            scores[index] += normalized - 0.25 * uncertainty
    acquisition_count = max(0, count - exploration_count)
    acquisition_order = sorted(range(len(pool)), key=lambda index: (scores[index], pool[index]))
    selected: list[list[int]] = []
    used: set[tuple[int, ...]] = set()
    if acquisition_count > 0:
        for index in acquisition_order:
            vector = pool[index]
            if tuple(vector) in used:
                continue
            selected.append(vector)
            used.add(tuple(vector))
            if len(selected) == acquisition_count:
                break
    # Exploration comes from the reverse interleaving of the QMC pool to avoid
    # simply selecting the same early candidates used by acquisition.
    for vector in reversed(pool):
        if tuple(vector) in used:
            continue
        selected.append(vector)
        used.add(tuple(vector))
        if len(selected) >= count:
            break
    return _adaptive_candidates(selected, "adaptive_extratrees"), {
        "method": "adaptive_extratrees",
        "engine": engine,
        "ml_eligible": True,
        "training_rows": len(valid_rows),
        "candidate_pool": len(pool),
        "exploration_count": min(exploration_count, len(selected)),
        "model_hash": hashlib.sha256("".join(model_fingerprints).encode()).hexdigest(),
        "notice": "Model spread is an acquisition heuristic, not engineering confidence.",
    }


def _adaptive_candidates(vectors: list[list[int]], source: str) -> list[DesignCandidate]:
    return [
        DesignCandidate(
            design_id=design_id(vector),
            vector=vector,
            source=source,
            sequence=index,
            batch=1,
        )
        for index, vector in enumerate(vectors)
    ]


def _sample_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def present_value_cost(
    vector: list[int],
    slots: list[CandidateSlot | dict[str, Any]],
    cost_basis: CostBasis | dict[str, Any],
) -> float:
    parsed_slots = [CandidateSlot.model_validate(slot) for slot in slots]
    cost_basis = CostBasis.model_validate(cost_basis)
    if not cost_basis.confirmed:
        raise ValueError("present-value cost requires a confirmed CostBasis")
    if len(vector) != len(parsed_slots):
        raise ValueError("design vector length does not match candidate slots")
    total = 0.0
    rate = cost_basis.discount_rate
    for units, slot in zip(vector, parsed_slots, strict=True):
        if not slot.min_additional_units <= units <= slot.max_additional_units:
            raise ValueError(f"unit count for {slot.slot_id} is outside its feasible range")
        key = slot.slot_id
        if key not in cost_basis.unit_capex or key not in cost_basis.annual_om:
            raise ValueError(f"CostBasis is missing explicit CAPEX or O&M for {key}")
        total += units * cost_basis.unit_capex[key]
        annual = units * cost_basis.annual_om[key]
        for year in range(1, cost_basis.analysis_horizon_years + 1):
            total += annual / ((1 + rate) ** year)
        replacement = units * cost_basis.replacement_cost.get(key, 0.0)
        for year in cost_basis.replacement_years.get(key, []):
            if 0 < year <= cost_basis.analysis_horizon_years:
                total += replacement / ((1 + rate) ** year)
    return total
