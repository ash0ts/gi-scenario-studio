#!/usr/bin/env python3
"""Run the complete, deterministic Cedar Trace search-replay campaign.

The campaign reads the immutable exact-SWMM oracle, but each method can reveal
an outcome only after selecting the corresponding design.  Replays therefore
avoid wasteful hydraulic reruns while preserving exact-job-equivalent budgets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from gi_scenario_core.benchmark import load_benchmark, run_replay

METHODS = ("random", "sobol", "nsga2", "sobol_extratrees")
BUDGETS = (64, 128, 256)
SEEDS = (11, 23, 37, 41, 53, 67, 71, 83, 97, 101)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _run_one(dataset: str, output: str, method: str, budget: int, seed: int) -> dict[str, Any]:
    payload, rows = load_benchmark(dataset)
    started = time.perf_counter()
    result = run_replay(payload, rows, method=method, budget=budget, seed=seed)
    elapsed_seconds = time.perf_counter() - started
    output_path = Path(output)
    _write_json_atomic(output_path, result)
    return {
        "method": method,
        "budget": budget,
        "seed": seed,
        "exact_job_equivalent": result["summary"]["exact_job_equivalent"],
        "campaign_sha256": result["campaign_sha256"],
        "file_sha256": _sha256(output_path),
        "relative_path": output_path.name,
        "elapsed_seconds": elapsed_seconds,
        "selection_counts": result["selection_counts"],
        "scientific_runtime": result["scientific_runtime"],
        "summary": result["summary"],
    }


def _result_path(output_root: Path, method: str, budget: int, seed: int) -> Path:
    return output_root / f"{method}__budget-{budget}__seed-{seed}.json"


def _load_completed(path: Path, method: str, budget: int, seed: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (result.get("method"), result.get("budget"), result.get("seed")) != (
        method,
        budget,
        seed,
    ):
        return None
    if result.get("campaign_sha256") is None:
        return None
    return {
        "method": method,
        "budget": budget,
        "seed": seed,
        "exact_job_equivalent": result["summary"]["exact_job_equivalent"],
        "campaign_sha256": result["campaign_sha256"],
        "file_sha256": _sha256(path),
        "relative_path": path.name,
        "elapsed_seconds": None,
        "selection_counts": result["selection_counts"],
        "scientific_runtime": result["scientific_runtime"],
        "summary": result["summary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/exhaustive_exact/dataset.json"),
    )
    parser.add_argument("--output-root", type=Path, default=Path("dist/search-benchmark"))
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, (os.cpu_count() or 2) // 2)),
        help="Independent replay processes. Each scientific model is single-threaded.",
    )
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()

    payload, rows = load_benchmark(arguments.dataset)
    if len(rows) != 700 or payload["job_accounting"]["direct_swmm_jobs"] != 2800:
        raise RuntimeError("the release campaign requires the pinned 700-vector oracle")
    arguments.output_root.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, int, int, Path]] = []
    records: list[dict[str, Any]] = []
    for method in METHODS:
        for budget in BUDGETS:
            for seed in SEEDS:
                path = _result_path(arguments.output_root, method, budget, seed)
                completed = None if arguments.force else _load_completed(path, method, budget, seed)
                if completed is None:
                    jobs.append((method, budget, seed, path))
                else:
                    records.append(completed)

    if jobs and arguments.workers == 1:
        for method, budget, seed, path in jobs:
            record = _run_one(
                str(arguments.dataset),
                str(path),
                method,
                budget,
                seed,
            )
            records.append(record)
            print(f"completed {method} budget={budget} seed={seed}")
    elif jobs:
        with ProcessPoolExecutor(max_workers=max(1, arguments.workers)) as executor:
            futures = {
                executor.submit(
                    _run_one,
                    str(arguments.dataset),
                    str(path),
                    method,
                    budget,
                    seed,
                ): (method, budget, seed)
                for method, budget, seed, path in jobs
            }
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                print(
                    f"completed {record['method']} budget={record['budget']} seed={record['seed']}"
                )

    records.sort(key=lambda item: (METHODS.index(item["method"]), item["budget"], item["seed"]))
    if len(records) != len(METHODS) * len(BUDGETS) * len(SEEDS):
        raise RuntimeError("campaign did not produce all 120 configured replays")
    if len({item["campaign_sha256"] for item in records}) != len(records):
        raise RuntimeError("campaign contains duplicate deterministic run identities")
    for record in records:
        if (
            record["method"] == "sobol_extratrees"
            and record["budget"] > 64
            and record["selection_counts"]["adaptive"] <= 0
        ):
            raise RuntimeError("ExtraTrees replay contains no adaptive selections")

    manifest = {
        "schema_version": "2.0",
        "campaign_id": "cedar-trace-search-release-gate-v2",
        "dataset_id": payload["dataset_id"],
        "dataset_sha256": payload["dataset_sha256"],
        "notice": payload["notice"],
        "oracle_policy": payload["oracle_policy"],
        "methods": list(METHODS),
        "design_budgets": list(BUDGETS),
        "exact_job_equivalent_budgets": [value * 4 for value in BUDGETS],
        "seeds": list(SEEDS),
        "run_count": len(records),
        "hydraulic_reruns_performed": 0,
        "records": records,
    }
    manifest_without_digest = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["manifest_sha256"] = hashlib.sha256(manifest_without_digest.encode()).hexdigest()
    _write_json_atomic(arguments.output_root / "campaign-manifest.json", manifest)
    print(arguments.output_root / "campaign-manifest.json")


if __name__ == "__main__":
    main()
