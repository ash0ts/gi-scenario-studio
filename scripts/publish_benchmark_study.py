#!/usr/bin/env python3
"""Publish the exact Cedar Trace benchmark study to a Restricted W&B project.

The default mode is a local, read-only validation pass. External W&B writes
occur only when ``--publish`` is supplied. The publisher never reads dotenv
files and never accepts an API key on the command line; W&B authentication must
already be present in the process environment or the platform credential store.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "benchmarks" / "exhaustive_exact" / "dataset.json"
DEFAULT_CAMPAIGN = ROOT / "dist" / "search-benchmark" / "campaign-manifest.json"
DEFAULT_PUBLICATION = ROOT / "dist" / "benchmark-publication" / "publication-manifest.json"
DEFAULT_MEDIA_SPEC = ROOT / "media" / "gi-scenario-film" / "story" / "benchmark-media.json"
DEFAULT_PLAN = ROOT / "dist" / "benchmark-publication" / "wandb-publication-plan.json"
DEFAULT_RECEIPT = ROOT / "dist" / "benchmark-publication" / "wandb-publication.json"

PROJECT_POLICY_GQL = """
query GIProjectPolicy($name: String!, $entity: String!) {
  project(name: $name, entityName: $entity) {
    id
    name
    entityName
    access
  }
}
"""

EXPECTED_METHODS = ("random", "sobol", "nsga2", "sobol_extratrees")
EXPECTED_BUDGETS = (64, 128, 256)
EXPECTED_SEEDS = (11, 23, 37, 41, 53, 67, 71, 83, 97, 101)
PROHIBITED_RUN_FILES = frozenset({"output.log", "requirements.txt", "wandb-metadata.json"})

EXACT_TABLE_SCHEMA: dict[str, Callable[[str], Any]] = {
    "design_id": str,
    "is_baseline": lambda value: _parse_bool(value),
    "bio_units": int,
    "permeable_pavement_units": int,
    "rain_garden_units": int,
    "rain_barrel_groups": int,
    "cost_thousand_usd": float,
    "volume_reduction_percent": float,
    "peak_guardrail_pass": lambda value: _parse_bool(value),
    "qa_pass": lambda value: _parse_bool(value),
    "teaching_rule_feasible": lambda value: _parse_bool(value),
    "adverse_any_storm": lambda value: _parse_bool(value),
    "exact_feasible_frontier": lambda value: _parse_bool(value),
    "in_guided_220": lambda value: _parse_bool(value),
}
STORM_TABLE_SCHEMA: dict[str, Callable[[str], Any]] = {
    "design_id": str,
    "storm_id": str,
    "storm_label": str,
    "outfall_peak_cfs": float,
    "baseline_peak_cfs": float,
    "peak_delta_cfs": float,
    "outfall_volume_million_gallons": float,
    "qa_status": str,
    "run_id": str,
}
AGGREGATE_TABLE_SCHEMA: dict[str, Callable[[str], Any]] = {
    "method": str,
    "method_label": str,
    "design_budget": int,
    "exact_job_equivalent": int,
    "seed_count": int,
    "hypervolume_ratio_p10": float,
    "hypervolume_ratio_median": float,
    "hypervolume_ratio_p90": float,
    "epsilon_indicator_p10": float,
    "epsilon_indicator_median": float,
    "epsilon_indicator_p90": float,
    "feasible_recall_p10": float,
    "feasible_recall_median": float,
    "feasible_recall_p90": float,
    "adverse_recall_p10": float,
    "adverse_recall_median": float,
    "adverse_recall_p90": float,
    "frontier_recall_p10": float,
    "frontier_recall_median": float,
    "frontier_recall_p90": float,
}
HISTORY_TABLE_SCHEMA: dict[str, Callable[[str], Any]] = {
    "method": str,
    "method_label": str,
    "seed": int,
    "configured_design_budget": int,
    "adaptive_designs": int,
    "adverse_recall": float,
    "epsilon_indicator": float,
    "exact_designs": int,
    "exact_job_equivalent": int,
    "exploration_designs": int,
    "feasible_recall": float,
    "frontier_recall": float,
    "frontier_size": int,
    "hypervolume_ratio": float,
    "step": int,
}
GATE_TABLE_SCHEMA: dict[str, Callable[[str], Any]] = {
    "criterion": str,
    "observed": str,
    "passed": lambda value: _parse_bool(value),
}

FIGURE_CAPTIONS = {
    "exact_decision_landscape": (
        "All 699 exact alternatives. Cost and volume alone do not encode the peak-flow guardrail."
    ),
    "rule_filter": (
        "Only 12 alternatives satisfy the complete synthetic teaching contract; 34 of the "
        "46 cost-and-volume candidates fail the peak guardrail."
    ),
    "storm_guardrail_atlas": (
        "Exact adverse-result counts by synthetic storm. The long-duration event reveals the "
        "largest number of peak increases."
    ),
    "guided_coverage": (
        "The deterministic guided subset teaches the workflow; it is not an exhaustive or "
        "adaptive-search result."
    ),
    "search_efficiency": (
        "Search quality and recall across exact-job-equivalent budgets, summarized over ten seeds."
    ),
    "release_gate_audit": (
        "The preregistered production gate is not met; adaptive ML remains experimental."
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"required benchmark input is missing: {_display_path(path)}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {_display_path(path)}")
    return value


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


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def _repo_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(
            "benchmark publication inputs must remain inside the repository"
        ) from exc
    return resolved


def _safe_child(root: Path, relative_path: str) -> Path:
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RuntimeError(f"unsafe manifest path: {relative_path!r}")
    resolved = (root / Path(*candidate.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"manifest path escaped its root: {relative_path!r}") from exc
    return resolved


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"expected CSV boolean, received {value!r}")


def _read_typed_csv(
    path: Path, schema: dict[str, Callable[[str], Any]]
) -> tuple[list[str], list[list[Any]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = list(schema)
        if reader.fieldnames != columns:
            raise RuntimeError(
                f"CSV schema drift in {_display_path(path)}: {reader.fieldnames} != {columns}"
            )
        rows = []
        for line_number, raw in enumerate(reader, start=2):
            try:
                rows.append([schema[column](raw[column]) for column in columns])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid value in {_display_path(path)} at line {line_number}: {exc}"
                ) from exc
    return columns, rows


def _validate_dataset(dataset_path: Path) -> dict[str, Any]:
    payload = _load_json(dataset_path)
    if payload.get("schema_version") != "2.0":
        raise RuntimeError("exact benchmark schema_version must be 2.0")
    expected_digest = payload.get("dataset_sha256")
    without_digest = {key: value for key, value in payload.items() if key != "dataset_sha256"}
    if expected_digest != _canonical_sha256(without_digest):
        raise RuntimeError("exact benchmark content digest does not match dataset_sha256")

    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 700 or payload.get("universe_size") != 700:
        raise RuntimeError("publication requires the pinned 700-vector exact benchmark")
    design_ids = [str(row.get("design_id")) for row in rows]
    vectors = [tuple(row.get("vector", [])) for row in rows]
    if len(set(design_ids)) != 700 or len(set(vectors)) != 700:
        raise RuntimeError("exact benchmark contains duplicate design IDs or vectors")
    if any(len(vector) != 4 for vector in vectors):
        raise RuntimeError("every exact benchmark vector must have four integer dimensions")

    storm_ids = {str(storm.get("storm_id")) for storm in payload.get("storms", [])}
    if len(storm_ids) != 4:
        raise RuntimeError("publication requires exactly four pinned synthetic storms")
    run_count = 0
    run_ids: set[str] = set()
    for row in rows:
        runs = row.get("runs")
        if not isinstance(runs, list) or {run.get("storm_id") for run in runs} != storm_ids:
            raise RuntimeError(f"design {row.get('design_id')} lacks complete four-storm evidence")
        run_count += len(runs)
        for run in runs:
            run_id = str(run.get("run_id"))
            if not run_id or run_id in run_ids:
                raise RuntimeError("exact benchmark run IDs must be present and unique")
            run_ids.add(run_id)
    accounting = payload.get("job_accounting") or {}
    if run_count != 2800 or accounting.get("direct_swmm_jobs") != 2800:
        raise RuntimeError("publication requires 2,800 exact design-by-storm records")
    return payload


def _validate_campaign(
    campaign_path: Path, *, dataset_id: str, dataset_sha256: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    campaign = _load_json(campaign_path)
    if campaign.get("schema_version") != "2.0":
        raise RuntimeError("search campaign schema_version must be 2.0")
    expected_manifest_digest = campaign.get("manifest_sha256")
    without_digest = {key: value for key, value in campaign.items() if key != "manifest_sha256"}
    if expected_manifest_digest != _canonical_sha256(without_digest):
        raise RuntimeError("search campaign manifest digest does not match its contents")
    if (campaign.get("dataset_id"), campaign.get("dataset_sha256")) != (
        dataset_id,
        dataset_sha256,
    ):
        raise RuntimeError("search campaign is not pinned to the selected exact dataset")
    if campaign.get("hydraulic_reruns_performed") != 0:
        raise RuntimeError("the replay campaign must not claim new hydraulic executions")
    if tuple(campaign.get("methods", [])) != EXPECTED_METHODS:
        raise RuntimeError("search campaign methods drifted from the preregistered study")
    if tuple(campaign.get("design_budgets", [])) != EXPECTED_BUDGETS:
        raise RuntimeError("search campaign budgets drifted from the preregistered study")
    if tuple(campaign.get("seeds", [])) != EXPECTED_SEEDS:
        raise RuntimeError("search campaign seeds drifted from the preregistered study")

    records = campaign.get("records")
    if not isinstance(records, list) or len(records) != 120 or campaign.get("run_count") != 120:
        raise RuntimeError("publication requires all 120 deterministic search replays")
    expected_grid = set(itertools.product(EXPECTED_METHODS, EXPECTED_BUDGETS, EXPECTED_SEEDS))
    observed_grid: set[tuple[str, int, int]] = set()
    results: list[dict[str, Any]] = []
    result_root = campaign_path.parent
    for record in records:
        key = (
            str(record.get("method")),
            int(record.get("budget", -1)),
            int(record.get("seed", -1)),
        )
        if key in observed_grid:
            raise RuntimeError(f"duplicate search replay in campaign: {key}")
        observed_grid.add(key)
        result_path = _safe_child(result_root, str(record.get("relative_path", "")))
        if not result_path.is_file() or _sha256(result_path) != record.get("file_sha256"):
            raise RuntimeError(f"search result file hash mismatch: {_display_path(result_path)}")
        result = _load_json(result_path)
        result_digest = result.get("campaign_sha256")
        result_without_digest = {
            name: value for name, value in result.items() if name != "campaign_sha256"
        }
        if result_digest != _canonical_sha256(result_without_digest):
            raise RuntimeError(f"search trace digest mismatch: {_display_path(result_path)}")
        if (
            result.get("schema_version") != "2.0"
            or result.get("dataset_id") != dataset_id
            or result.get("dataset_sha256") != dataset_sha256
            or (result.get("method"), result.get("budget"), result.get("seed")) != key
            or result_digest != record.get("campaign_sha256")
            or result.get("summary") != record.get("summary")
            or result.get("selection_counts") != record.get("selection_counts")
        ):
            raise RuntimeError(f"search campaign record drift: {_display_path(result_path)}")
        history = result.get("history")
        if not isinstance(history, list) or not history:
            raise RuntimeError(f"search trace has no history: {_display_path(result_path)}")
        steps = [int(item.get("step", -1)) for item in history]
        if steps != sorted(set(steps)) or steps[-1] != key[1]:
            raise RuntimeError(f"search history steps are incomplete: {_display_path(result_path)}")
        summary = result["summary"]
        if (
            summary.get("exact_designs") != key[1]
            or summary.get("exact_job_equivalent") != key[1] * 4
            or len(result.get("selected_design_ids", [])) != key[1]
            or sum(result["selection_counts"].values()) != key[1]
        ):
            raise RuntimeError(f"search budget accounting drift: {_display_path(result_path)}")
        runtime = result.get("scientific_runtime") or {}
        if key[0] in {"sobol", "nsga2", "sobol_extratrees"} and not runtime.get("scipy"):
            raise RuntimeError(f"SciPy-backed Sobol was not recorded: {_display_path(result_path)}")
        if key[0] == "sobol_extratrees":
            if not runtime.get("scikit_learn"):
                raise RuntimeError(f"ExtraTrees runtime is absent: {_display_path(result_path)}")
            if key[1] > 64 and result["selection_counts"].get("adaptive", 0) <= 0:
                raise RuntimeError(
                    f"ExtraTrees made no adaptive selections: {_display_path(result_path)}"
                )
        results.append(result)
    if observed_grid != expected_grid:
        raise RuntimeError(
            "search campaign does not cover the complete method × budget × seed grid"
        )
    if len({result["campaign_sha256"] for result in results}) != 120:
        raise RuntimeError("search replay identities must be unique")
    return campaign, results


def _validate_publication(
    publication_path: Path,
    *,
    dataset: dict[str, Any],
    campaign: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, tuple[list[str], list[list[Any]]]]]:
    publication = _load_json(publication_path)
    if publication.get("schema_version") != "2.0":
        raise RuntimeError("benchmark publication schema_version must be 2.0")
    if (
        publication.get("dataset_id") != dataset.get("dataset_id")
        or publication.get("dataset_sha256") != dataset.get("dataset_sha256")
        or publication.get("campaign_manifest_sha256") != campaign.get("manifest_sha256")
    ):
        raise RuntimeError("publication is not pinned to the exact dataset and campaign")
    if publication.get("notice") != dataset.get("notice"):
        raise RuntimeError("publication synthetic-model notice drifted from the exact dataset")

    publication_root = publication_path.parent
    files = publication.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("publication manifest contains no output inventory")
    for record in files:
        path = _safe_child(publication_root, str(record.get("relative_path", "")))
        if (
            not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or _sha256(path) != record.get("sha256")
        ):
            raise RuntimeError(f"publication output hash mismatch: {_display_path(path)}")

    figures = publication.get("figures")
    if not isinstance(figures, list) or len(figures) != 6:
        raise RuntimeError("publication requires exactly six reviewed benchmark graphics")
    if {item.get("key") for item in figures} != set(FIGURE_CAPTIONS):
        raise RuntimeError("publication figure keys drifted from the reviewed report layout")
    for figure in figures:
        path = _safe_child(publication_root, str(figure.get("relative_path", "")))
        if not path.is_file() or _sha256(path) != figure.get("sha256"):
            raise RuntimeError(f"publication figure hash mismatch: {_display_path(path)}")

    table_specs = {
        "exact_alternatives": ("tables/exact_alternatives.csv", EXACT_TABLE_SCHEMA, 700),
        "storm_results": ("tables/storm_results.csv", STORM_TABLE_SCHEMA, 2800),
        "campaign_aggregates": ("tables/campaign_aggregates.csv", AGGREGATE_TABLE_SCHEMA, 12),
        "campaign_history": ("tables/campaign_history.csv", HISTORY_TABLE_SCHEMA, 960),
        "release_gate": ("tables/release_gate.csv", GATE_TABLE_SCHEMA, 5),
    }
    tables: dict[str, tuple[list[str], list[list[Any]]]] = {}
    for key, (relative_path, schema, expected_rows) in table_specs.items():
        table = _read_typed_csv(_safe_child(publication_root, relative_path), schema)
        if len(table[1]) != expected_rows:
            raise RuntimeError(f"{key} contains {len(table[1])} rows, expected {expected_rows}")
        tables[key] = table

    dataset_designs = {str(row["design_id"]) for row in dataset["rows"]}
    exact_design_index = EXACT_TABLE_SCHEMA.keys()
    exact_design_column = list(exact_design_index).index("design_id")
    if {
        str(row[exact_design_column]) for row in tables["exact_alternatives"][1]
    } != dataset_designs:
        raise RuntimeError("exact-alternative Table does not match the dataset design IDs")
    storm_design_column = list(STORM_TABLE_SCHEMA).index("design_id")
    if {str(row[storm_design_column]) for row in tables["storm_results"][1]} != dataset_designs:
        raise RuntimeError("storm-results Table does not cover every exact design")

    claims = publication.get("claims") or {}
    required_claims = {
        "alternatives": 699,
        "all_rules": 12,
        "adverse": 421,
        "frontier": 4,
        "search_run_count": 120,
        "seed_count": 10,
        "hydraulic_reruns_performed": 0,
        "adaptive_production_gate_passed": False,
    }
    if any(claims.get(key) != value for key, value in required_claims.items()):
        raise RuntimeError("publication claims drifted from the reviewed synthetic study")
    return publication, tables


def _deterministic_id(kind: str, entity: str, project: str, digest: str) -> str:
    return hashlib.sha256(f"{kind}|{entity}|{project}|{digest}".encode()).hexdigest()[:24]


def _plan_file(path: Path, logical_name: str) -> dict[str, Any]:
    return {
        "source": _display_path(path),
        "logical_name": logical_name,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def build_plan(
    entity: str,
    project: str,
    *,
    dataset_path: Path = DEFAULT_DATASET,
    campaign_path: Path = DEFAULT_CAMPAIGN,
    publication_path: Path = DEFAULT_PUBLICATION,
    media_spec_path: Path = DEFAULT_MEDIA_SPEC,
    plan_path: Path = DEFAULT_PLAN,
) -> dict[str, Any]:
    dataset_path = _repo_path(dataset_path)
    campaign_path = _repo_path(campaign_path)
    publication_path = _repo_path(publication_path)
    media_spec_path = _repo_path(media_spec_path)
    dataset = _validate_dataset(dataset_path)
    campaign, results = _validate_campaign(
        campaign_path,
        dataset_id=str(dataset["dataset_id"]),
        dataset_sha256=str(dataset["dataset_sha256"]),
    )
    publication, tables = _validate_publication(
        publication_path, dataset=dataset, campaign=campaign
    )
    media_spec = _load_json(media_spec_path)
    if media_spec.get("schema_version") != "2.0":
        raise RuntimeError("benchmark media schema_version must be 2.0")
    charts = media_spec.get("charts")
    if not isinstance(charts, list) or len(charts) != 6:
        raise RuntimeError("benchmark media specification must contain exactly six charts")
    chart_by_figure = {str(chart.get("figure_key")): chart for chart in charts}
    if set(chart_by_figure) != set(FIGURE_CAPTIONS):
        raise RuntimeError("benchmark media keys do not match the six reviewed figures")
    if any(chart.get("key") != f"benchmark/{chart.get('figure_key')}" for chart in charts):
        raise RuntimeError("benchmark media keys must use the reviewed benchmark/<figure> names")

    publication_root = publication_path.parent
    exact_table_path = publication_root / "tables" / "exact_alternatives.csv"
    storm_table_path = publication_root / "tables" / "storm_results.csv"
    dataset_files = [
        _plan_file(dataset_path, "exact/dataset.json"),
        _plan_file(exact_table_path, "tables/exact_alternatives.csv"),
        _plan_file(storm_table_path, "tables/storm_results.csv"),
    ]
    readme = dataset_path.parent / "README.md"
    if readme.is_file():
        dataset_files.append(_plan_file(readme, "exact/README.md"))

    search_runs = []
    result_by_digest = {result["campaign_sha256"]: result for result in results}
    for record in campaign["records"]:
        digest = str(record["campaign_sha256"])
        result = result_by_digest[digest]
        search_runs.append(
            {
                "run_id": _deterministic_id("search-benchmark-v2", entity, project, digest),
                "run_name": (
                    f"search-{record['method']}-b{record['budget']}-s{record['seed']}-{digest[:8]}"
                ),
                "method": record["method"],
                "budget": record["budget"],
                "seed": record["seed"],
                "campaign_sha256": digest,
                "result_source": (campaign_path.parent / str(record["relative_path"]))
                .relative_to(ROOT)
                .as_posix(),
                "result_sha256": record["file_sha256"],
                "history_rows": len(result["history"]),
                "trace_artifact_name": f"gi-search-trace-{digest[:20]}",
            }
        )

    campaign_files = [_plan_file(campaign_path, "campaign/campaign-manifest.json")]
    for record in campaign["records"]:
        path = _safe_child(campaign_path.parent, str(record["relative_path"]))
        campaign_files.append(_plan_file(path, f"campaign/results/{path.name}"))
    campaign_files.append(_plan_file(publication_path, "publication/publication-manifest.json"))
    for record in publication["files"]:
        source = _safe_child(publication_root, str(record["relative_path"]))
        campaign_files.append(_plan_file(source, f"publication/{record['relative_path']}"))

    digest_payload = {
        "dataset_sha256": dataset["dataset_sha256"],
        "dataset_file_sha256": _sha256(dataset_path),
        "campaign_manifest_sha256": campaign["manifest_sha256"],
        "campaign_file_sha256": _sha256(campaign_path),
        "publication_manifest_sha256": _sha256(publication_path),
        "publication_files": [
            (record["relative_path"], record["sha256"]) for record in publication["files"]
        ],
    }
    publication_digest = _canonical_sha256(digest_payload)
    plan = {
        "schema_version": "2.0",
        "entity": entity,
        "project": project,
        "project_access_required": "RESTRICTED",
        "publication_digest": publication_digest,
        "dataset": {
            "dataset_id": dataset["dataset_id"],
            "dataset_sha256": dataset["dataset_sha256"],
            "file_sha256": _sha256(dataset_path),
            "notice": dataset["notice"],
            "row_count": 700,
            "storm_row_count": 2800,
            "run_id": _deterministic_id(
                "benchmark-dataset-v2", entity, project, str(dataset["dataset_sha256"])
            ),
            "run_name": f"benchmark-dataset-{str(dataset['dataset_sha256'])[:8]}",
            "artifact_name": "gi-cedar-trace-exhaustive-exact",
            "artifact_aliases": ["latest", f"sha-{str(dataset['dataset_sha256'])[:12]}"],
            "files": dataset_files,
        },
        "search_campaign": {
            "campaign_id": campaign["campaign_id"],
            "manifest_sha256": campaign["manifest_sha256"],
            "run_count": 120,
            "methods": list(EXPECTED_METHODS),
            "budgets": list(EXPECTED_BUDGETS),
            "seeds": list(EXPECTED_SEEDS),
            "runs": search_runs,
        },
        "publication": {
            "publication_id": publication["publication_id"],
            "run_id": _deterministic_id(
                "benchmark-publication-v2", entity, project, publication_digest
            ),
            "run_name": f"benchmark-publication-{publication_digest[:8]}",
            "artifact_name": "gi-cedar-trace-search-campaign",
            "artifact_aliases": ["latest", f"sha-{publication_digest[:12]}"],
            "aggregate_rows": len(tables["campaign_aggregates"][1]),
            "history_rows": len(tables["campaign_history"][1]),
            "gate_rows": len(tables["release_gate"][1]),
            "figure_count": len(publication["figures"]),
            "figures": [
                {
                    **figure,
                    "source": (publication_root / str(figure["relative_path"]))
                    .relative_to(ROOT)
                    .as_posix(),
                    "media_key": chart_by_figure[str(figure["key"])]["key"],
                    "caption": chart_by_figure[str(figure["key"])]["caption"],
                }
                for figure in publication["figures"]
            ],
            "files": campaign_files,
        },
    }
    _write_json_atomic(plan_path, plan)
    return plan


def _defensive_wandb_environment() -> None:
    cloud_root = ROOT / "dist" / "benchmark-publication" / "wandb"
    for name, child in (
        ("WANDB_DIR", "run"),
        ("WANDB_CACHE_DIR", "cache"),
        ("WANDB_ARTIFACT_DIR", "artifacts"),
    ):
        path = cloud_root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    for name, value in {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
    }.items():
        os.environ[name] = value


def _project_policy(api: Any, entity: str, project: str) -> dict[str, str] | None:
    """Read exact cloud access without trusting lazy SDK project attributes."""
    response = api._service_api.execute_graphql(
        PROJECT_POLICY_GQL,
        {"name": project, "entity": entity},
    )
    record = response.get("project")
    if record is None:
        return None
    required = ("id", "name", "entityName", "access")
    if any(not record.get(field) for field in required):
        raise RuntimeError("W&B project policy response was incomplete; nothing was uploaded")
    if record["name"] != project or record["entityName"] != entity:
        raise RuntimeError("W&B project policy response did not match the requested destination")
    return {field: str(record[field]) for field in required}


def _create_restricted_project(api: Any, entity: str, project: str) -> None:
    from wandb.apis._generated import CREATE_PROJECT_GQL

    response = api._service_api.execute_graphql(
        CREATE_PROJECT_GQL,
        {"input": {"name": project, "entityName": entity, "access": "RESTRICTED"}},
    )
    result = response.get("result") or {}
    record = result.get("project") or result.get("model") or {}
    if str(record.get("access") or "").upper() != "RESTRICTED":
        raise RuntimeError("W&B did not confirm Restricted access during project creation")


def _get_project(api: Any, entity: str, project: str, allow_create: bool) -> Any:
    policy = _project_policy(api, entity, project)
    if policy is None:
        if not allow_create:
            raise RuntimeError(
                "W&B project does not exist; rerun with --allow-create-project only after "
                "reviewing the exact entity/project destination"
            )
        _create_restricted_project(api, entity, project)
        for _ in range(15):
            policy = _project_policy(api, entity, project)
            if policy is not None:
                break
            time.sleep(1)
    if policy is None:
        raise RuntimeError("W&B project was not accessible after creation")
    if policy["access"].upper() != "RESTRICTED":
        raise RuntimeError(
            f"W&B project access is {policy['access'].upper()}, not RESTRICTED; nothing was uploaded"
        )
    selected = api.project(project, entity=entity)
    if str(selected.id) != policy["id"]:
        raise RuntimeError("W&B project identity changed during policy verification")
    return selected


def _wandb_settings(wandb: Any) -> Any:
    return wandb.Settings(
        console="off",
        disable_code=True,
        disable_git=True,
        save_code=False,
        show_errors=False,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_disable_stats=True,
        x_save_requirements=False,
    )


def _source_path(record: dict[str, Any]) -> Path:
    return _safe_child(ROOT, str(record["source"]))


def _add_files(artifact: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        source = _source_path(record)
        if _sha256(source) != record["sha256"]:
            raise RuntimeError(f"source changed after validation: {record['source']}")
        artifact.add_file(str(source), name=record["logical_name"], policy="immutable")


def _verify_downloaded_artifact(logged: Any, records: list[dict[str, Any]], *, prefix: str) -> None:
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=dist) as temporary:
        download_root = Path(logged.download(root=temporary, skip_cache=True))
        for record in records:
            downloaded = _safe_child(download_root, record["logical_name"])
            if not downloaded.is_file() or _sha256(downloaded) != record["sha256"]:
                raise RuntimeError(f"W&B artifact roundtrip failed for {record['logical_name']}")


def _existing_complete_run(
    api: Any, wandb: Any, entity: str, project: str, run_id: str, digest: str
) -> Any | None:
    try:
        run = api.run(f"{entity}/{project}/{run_id}")
    except wandb.errors.CommError as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    if run.summary.get("publisher_status") != "complete":
        return None
    if run.summary.get("publication_digest") != digest:
        raise RuntimeError(f"deterministic W&B run ID collision for {run_id}")
    return run


def _assert_run_file_policy(
    api: Any, entity: str, project: str, run_ids: list[str]
) -> dict[str, int]:
    affected: list[tuple[str, str]] = []
    for run_id in run_ids:
        run = api.run(f"{entity}/{project}/{run_id}")
        # W&B 0.28.1's explicit ``names=`` query may return placeholder
        # records for deleted or nonexistent names. Enumerate the authoritative
        # current inventory, then filter locally.
        for file in run.files():
            if file.name in PROHIBITED_RUN_FILES:
                affected.append((run_id, file.name))
    if affected:
        names = sorted({name for _, name in affected})
        raise RuntimeError(
            "W&B run-file privacy audit failed for "
            f"{len({run_id for run_id, _ in affected})} runs: {', '.join(names)}"
        )
    return {"audited_runs": len(run_ids), "prohibited_files": 0}


def _table(wandb: Any, value: tuple[list[str], list[list[Any]]]) -> Any:
    columns, rows = value
    return wandb.Table(columns=columns, data=rows)


def _publish_dataset_run(
    wandb: Any,
    api: Any,
    plan: dict[str, Any],
    tables: dict[str, tuple[list[str], list[list[Any]]]],
) -> dict[str, str]:
    entity, project = plan["entity"], plan["project"]
    spec = plan["dataset"]
    digest = str(spec["dataset_sha256"])
    existing = _existing_complete_run(api, wandb, entity, project, spec["run_id"], digest)
    if existing is not None:
        qualified_name = str(existing.summary.get("artifact_qualified_name") or "")
        if (
            not qualified_name
            or not api.artifact(qualified_name).metadata.get("dataset_sha256") == digest
        ):
            raise RuntimeError("completed dataset run lacks the pinned dataset artifact")
        return {"run_id": spec["run_id"], "run_url": existing.url, "artifact": qualified_name}

    with wandb.init(
        entity=entity,
        project=project,
        id=spec["run_id"],
        name=spec["run_name"],
        job_type="benchmark_dataset",
        resume="allow",
        tags=["synthetic", "exact-swmm", "benchmark-dataset", "gi-scenario-studio"],
        config={
            "dataset_id": spec["dataset_id"],
            "dataset_sha256": digest,
            "exact_alternatives": 700,
            "design_storm_records": 2800,
            "synthetic_teaching_evidence": True,
            "hydraulic_source": "pinned direct SWMM fixture results",
        },
        settings=_wandb_settings(wandb),
    ) as run:
        run.log(
            {
                "benchmark/exact_alternatives": _table(wandb, tables["exact_alternatives"]),
                "benchmark/storm_results": _table(wandb, tables["storm_results"]),
            }
        )
        artifact = wandb.Artifact(
            name=spec["artifact_name"],
            type="benchmark-dataset",
            metadata={
                "dataset_id": spec["dataset_id"],
                "dataset_sha256": digest,
                "exact_alternatives": 700,
                "design_storm_records": 2800,
                "notice": spec["notice"],
            },
        )
        _add_files(artifact, spec["files"])
        logged = run.log_artifact(artifact, aliases=spec["artifact_aliases"])
        logged.wait()
        _verify_downloaded_artifact(logged, spec["files"], prefix="gi-dataset-roundtrip-")
        qualified_name = str(logged.qualified_name)
        run.summary.update(
            {
                "publisher_status": "complete",
                "publication_digest": digest,
                "artifact_qualified_name": qualified_name,
                "exact_alternatives": 700,
                "design_storm_records": 2800,
                "roundtrip_verified": True,
            }
        )
        return {"run_id": spec["run_id"], "run_url": run.url, "artifact": qualified_name}


def _publish_search_runs(
    wandb: Any,
    api: Any,
    plan: dict[str, Any],
    *,
    dataset_artifact: str,
) -> list[dict[str, str]]:
    entity, project = plan["entity"], plan["project"]
    receipts: list[dict[str, str]] = []
    sample_keys = {(method, EXPECTED_BUDGETS[-1], EXPECTED_SEEDS[0]) for method in EXPECTED_METHODS}
    for index, spec in enumerate(plan["search_campaign"]["runs"], start=1):
        digest = str(spec["campaign_sha256"])
        existing = _existing_complete_run(api, wandb, entity, project, spec["run_id"], digest)
        if existing is not None:
            qualified_name = str(existing.summary.get("trace_artifact_qualified_name") or "")
            if (
                not qualified_name
                or api.artifact(qualified_name).metadata.get("campaign_sha256") != digest
            ):
                raise RuntimeError("completed search run lacks its pinned trace artifact")
            receipts.append(
                {
                    "run_id": spec["run_id"],
                    "run_url": existing.url,
                    "trace_artifact": qualified_name,
                }
            )
            continue
        result_path = _source_path(
            {
                "source": spec["result_source"],
            }
        )
        if _sha256(result_path) != spec["result_sha256"]:
            raise RuntimeError(f"search trace changed after validation: {spec['result_source']}")
        result = _load_json(result_path)
        config = {
            key: result[key]
            for key in (
                "dataset_id",
                "dataset_sha256",
                "method",
                "seed",
                "budget",
                "scientific_runtime",
            )
        }
        if "method_configuration" in result:
            config["method_configuration"] = result["method_configuration"]
        config.update(
            {
                "campaign_id": plan["search_campaign"]["campaign_id"],
                "campaign_sha256": digest,
                "oracle_policy": "reveal exact stored outcome only after candidate request",
                "hydraulic_reruns_performed": 0,
            }
        )
        with wandb.init(
            entity=entity,
            project=project,
            id=spec["run_id"],
            name=spec["run_name"],
            job_type="search_benchmark",
            resume="allow",
            tags=["synthetic", "search-replay", str(spec["method"]), "gi-scenario-studio"],
            config=config,
            settings=_wandb_settings(wandb),
        ) as run:
            run.use_artifact(dataset_artifact)
            for row in result["history"]:
                run.log(dict(row), step=int(row["step"]))
            artifact = wandb.Artifact(
                name=spec["trace_artifact_name"],
                type="search-trace",
                metadata={
                    "campaign_sha256": digest,
                    "dataset_sha256": result["dataset_sha256"],
                    "method": result["method"],
                    "budget": result["budget"],
                    "seed": result["seed"],
                    "hydraulic_reruns_performed": 0,
                },
            )
            artifact.add_file(str(result_path), name="search-trace.json", policy="immutable")
            logged = run.log_artifact(artifact, aliases=[f"sha-{digest[:12]}"])
            logged.wait()
            if (spec["method"], spec["budget"], spec["seed"]) in sample_keys:
                _verify_downloaded_artifact(
                    logged,
                    [
                        {
                            "logical_name": "search-trace.json",
                            "sha256": spec["result_sha256"],
                        }
                    ],
                    prefix="gi-trace-roundtrip-",
                )
            qualified_name = str(logged.qualified_name)
            run.summary.update(
                {
                    **result["summary"],
                    "publisher_status": "complete",
                    "publication_digest": digest,
                    "trace_artifact_qualified_name": qualified_name,
                    "selection_exploration": result["selection_counts"]["exploration"],
                    "selection_adaptive": result["selection_counts"]["adaptive"],
                    "hydraulic_reruns_performed": 0,
                }
            )
            receipts.append(
                {"run_id": spec["run_id"], "run_url": run.url, "trace_artifact": qualified_name}
            )
        if index % 20 == 0:
            print(json.dumps({"published_search_runs": index, "total_search_runs": 120}))
    return receipts


def _publish_publication_run(
    wandb: Any,
    api: Any,
    plan: dict[str, Any],
    tables: dict[str, tuple[list[str], list[list[Any]]]],
    publication: dict[str, Any],
    *,
    dataset_artifact: str,
) -> dict[str, str]:
    entity, project = plan["entity"], plan["project"]
    spec = plan["publication"]
    digest = str(plan["publication_digest"])
    existing = _existing_complete_run(api, wandb, entity, project, spec["run_id"], digest)
    if existing is not None:
        qualified_name = str(existing.summary.get("artifact_qualified_name") or "")
        if (
            not qualified_name
            or api.artifact(qualified_name).metadata.get("publication_digest") != digest
        ):
            raise RuntimeError("completed publication run lacks its pinned campaign artifact")
        return {
            "run_id": spec["run_id"],
            "run_url": existing.url,
            "artifact": qualified_name,
        }
    with wandb.init(
        entity=entity,
        project=project,
        id=spec["run_id"],
        name=spec["run_name"],
        job_type="benchmark_publication",
        resume="allow",
        tags=["synthetic", "benchmark-publication", "release-gate", "gi-scenario-studio"],
        config={
            "publication_id": spec["publication_id"],
            "publication_digest": digest,
            "dataset_sha256": plan["dataset"]["dataset_sha256"],
            "campaign_manifest_sha256": plan["search_campaign"]["manifest_sha256"],
            "search_run_count": 120,
            "seed_count": 10,
            "hydraulic_reruns_performed": 0,
            "adaptive_production_gate_passed": False,
            "synthetic_teaching_evidence": True,
        },
        settings=_wandb_settings(wandb),
    ) as run:
        run.use_artifact(dataset_artifact)
        payload: dict[str, Any] = {
            "benchmark/campaign_aggregates": _table(wandb, tables["campaign_aggregates"]),
            "benchmark/campaign_history": _table(wandb, tables["campaign_history"]),
            "benchmark/release_gate": _table(wandb, tables["release_gate"]),
        }
        for figure in spec["figures"]:
            path = _source_path({"source": figure["source"]})
            payload[str(figure["media_key"])] = wandb.Image(
                str(path), caption=str(figure["caption"])
            )
        run.log(payload)
        artifact = wandb.Artifact(
            name=spec["artifact_name"],
            type="benchmark-campaign",
            metadata={
                "publication_id": spec["publication_id"],
                "publication_digest": digest,
                "dataset_sha256": plan["dataset"]["dataset_sha256"],
                "campaign_manifest_sha256": plan["search_campaign"]["manifest_sha256"],
                "search_run_count": 120,
                "adaptive_production_gate_passed": False,
                "notice": plan["dataset"]["notice"],
            },
        )
        _add_files(artifact, spec["files"])
        logged = run.log_artifact(artifact, aliases=spec["artifact_aliases"])
        logged.wait()
        _verify_downloaded_artifact(logged, spec["files"], prefix="gi-campaign-roundtrip-")
        qualified_name = str(logged.qualified_name)
        claims = publication["claims"]
        run.summary.update(
            {
                "publisher_status": "complete",
                "publication_digest": digest,
                "artifact_qualified_name": qualified_name,
                "search_run_count": 120,
                "exact_alternatives": 700,
                "design_storm_records": 2800,
                "feasible_alternatives": claims["all_rules"],
                "adverse_alternatives": claims["adverse"],
                "exact_feasible_frontier": claims["frontier"],
                "adaptive_production_gate_passed": False,
                "roundtrip_verified": True,
            }
        )
        return {"run_id": spec["run_id"], "run_url": run.url, "artifact": qualified_name}


def publish(
    plan: dict[str, Any], *, allow_create_project: bool, receipt_path: Path = DEFAULT_RECEIPT
) -> dict[str, Any]:
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    _defensive_wandb_environment()
    try:
        import wandb
    except ImportError as exc:
        raise RuntimeError("install the observability dependency group before publishing") from exc

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    exact_project = _get_project(
        api, plan["entity"], plan["project"], allow_create=allow_create_project
    )

    dataset_path = _source_path({"source": plan["dataset"]["files"][0]["source"]})
    campaign_manifest_record = next(
        record
        for record in plan["publication"]["files"]
        if record["logical_name"] == "campaign/campaign-manifest.json"
    )
    campaign_path = _source_path({"source": campaign_manifest_record["source"]})
    publication_manifest_record = next(
        record
        for record in plan["publication"]["files"]
        if record["logical_name"] == "publication/publication-manifest.json"
    )
    publication_path = _source_path({"source": publication_manifest_record["source"]})
    dataset = _validate_dataset(dataset_path)
    campaign, _ = _validate_campaign(
        campaign_path,
        dataset_id=str(dataset["dataset_id"]),
        dataset_sha256=str(dataset["dataset_sha256"]),
    )
    publication, tables = _validate_publication(
        publication_path, dataset=dataset, campaign=campaign
    )

    dataset_receipt = _publish_dataset_run(wandb, api, plan, tables)
    search_receipts = _publish_search_runs(
        wandb, api, plan, dataset_artifact=dataset_receipt["artifact"]
    )
    if len(search_receipts) != 120:
        raise RuntimeError("W&B publication did not account for all 120 search runs")
    publication_receipt = _publish_publication_run(
        wandb,
        api,
        plan,
        tables,
        publication,
        dataset_artifact=dataset_receipt["artifact"],
    )
    final_policy = _project_policy(api, plan["entity"], plan["project"])
    if final_policy is None or final_policy["access"].upper() != "RESTRICTED":
        raise RuntimeError("W&B project policy changed during publication")
    if str(exact_project.id) != final_policy["id"]:
        raise RuntimeError("W&B project identity changed during publication")
    run_file_policy = _assert_run_file_policy(
        api,
        plan["entity"],
        plan["project"],
        [
            dataset_receipt["run_id"],
            *(receipt["run_id"] for receipt in search_receipts),
            publication_receipt["run_id"],
        ],
    )

    receipt = {
        "schema_version": "2.0",
        "entity": plan["entity"],
        "project": plan["project"],
        "project_access": "RESTRICTED",
        "publication_digest": plan["publication_digest"],
        "dataset_artifact": dataset_receipt["artifact"],
        "publication_artifact": publication_receipt["artifact"],
        "publication_id": plan["publication"]["publication_id"],
        "publication_run_id": publication_receipt["run_id"],
        "publication_run_url": publication_receipt["run_url"],
        "dataset": dataset_receipt,
        "search_run_count": len(search_receipts),
        "search_runs": search_receipts,
        "publication": publication_receipt,
        "roundtrip_verification": {
            "dataset_artifact": "all files",
            "search_trace_artifacts": "one maximum-budget trace per method",
            "campaign_artifact": "all files",
        },
        "run_file_privacy_audit": run_file_policy,
        "public_share_link_enabled": False,
    }
    _write_json_atomic(receipt_path, receipt)
    return receipt


def audit_published_run_files(
    plan: dict[str, Any], *, receipt_path: Path = DEFAULT_RECEIPT
) -> dict[str, int]:
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    receipt = _load_json(receipt_path)
    if (
        receipt.get("entity") != plan["entity"]
        or receipt.get("project") != plan["project"]
        or receipt.get("publication_digest") != plan["publication_digest"]
        or receipt.get("project_access") != "RESTRICTED"
    ):
        raise RuntimeError("W&B receipt does not match the validated publication plan")
    search_runs = receipt.get("search_runs")
    if not isinstance(search_runs, list) or len(search_runs) != 120:
        raise RuntimeError("W&B receipt does not account for all search runs")

    _defensive_wandb_environment()
    import wandb

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    _get_project(api, plan["entity"], plan["project"], allow_create=False)
    audit = _assert_run_file_policy(
        api,
        plan["entity"],
        plan["project"],
        [
            str(receipt["dataset"]["run_id"]),
            *(str(item["run_id"]) for item in search_runs),
            str(receipt["publication"]["run_id"]),
        ],
    )
    receipt["run_file_privacy_audit"] = audit
    _write_json_atomic(receipt_path, receipt)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--entity", default=os.environ.get("WANDB_REPORT_ENTITY") or os.environ.get("WANDB_ENTITY")
    )
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--publication", type=Path, default=DEFAULT_PUBLICATION)
    parser.add_argument("--media-spec", type=Path, default=DEFAULT_MEDIA_SPEC)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--publish", action="store_true", help="perform the external W&B writes")
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="verify the published run-file privacy policy and update the local receipt",
    )
    parser.add_argument(
        "--allow-create-project",
        action="store_true",
        help="create the exact requested project with Restricted access if it does not exist",
    )
    arguments = parser.parse_args()
    if not arguments.entity or not arguments.project:
        parser.error(
            "--entity and --project are required; the publisher never guesses a destination"
        )
    if arguments.allow_create_project and not arguments.publish:
        parser.error("--allow-create-project has no effect without --publish")
    if arguments.publish and arguments.audit_only:
        parser.error("--publish and --audit-only are mutually exclusive")
    plan = build_plan(
        arguments.entity,
        arguments.project,
        dataset_path=arguments.dataset,
        campaign_path=arguments.campaign,
        publication_path=arguments.publication,
        media_spec_path=arguments.media_spec,
        plan_path=arguments.plan,
    )
    if arguments.audit_only:
        audit = audit_published_run_files(plan, receipt_path=arguments.receipt)
        print(json.dumps({"run_file_privacy_audit": audit}))
    elif arguments.publish:
        receipt = publish(
            plan,
            allow_create_project=arguments.allow_create_project,
            receipt_path=arguments.receipt,
        )
        print(
            json.dumps(
                {
                    "project_access": receipt["project_access"],
                    "dataset_run_url": receipt["dataset"]["run_url"],
                    "search_run_count": receipt["search_run_count"],
                    "publication_run_url": receipt["publication"]["run_url"],
                    "receipt": _display_path(arguments.receipt),
                }
            )
        )
    else:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "plan": _display_path(arguments.plan),
                    "dataset_rows": plan["dataset"]["row_count"],
                    "storm_rows": plan["dataset"]["storm_row_count"],
                    "search_runs": plan["search_campaign"]["run_count"],
                    "figures": plan["publication"]["figure_count"],
                }
            )
        )


if __name__ == "__main__":
    main()
