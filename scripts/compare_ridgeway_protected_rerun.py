#!/usr/bin/env python3
"""Compare a protected Ridgeway rerun with committed direct evidence.

Both packages must first pass ``verify_ridgeway_release_evidence.py``.  This
comparison then checks every direct job and pair metric, plus every retained
current-raster cell, using the declared cross-platform tolerances.  Runtime and
energy observations are intentionally not asserted equal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "ridgeway-protected-rerun-differential/v1.0"
ABSOLUTE_TOLERANCE = 1e-8
RELATIVE_TOLERANCE = 1e-7
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
PLATFORM_SPECIFIC_WORKFLOW_COMPONENTS = frozenset(
    {
        "julia_executable",
        "julia_runtime_tree",
        "native_artifact_tree",
        "engine_manifest_contract",
        "python_runtime",
    }
)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _canonical_sha(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink")
    value = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object")
    return value


def _archive_path(directory: Path, manifest: dict[str, Any]) -> Path:
    bundle = manifest.get("evidence_bundle")
    if not isinstance(bundle, dict):
        raise TypeError("Ridgeway direct manifest omits the evidence bundle")
    name = bundle.get("filename")
    if not isinstance(name, str) or PurePosixPath(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Ridgeway archive filename is unsafe")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ValueError("Ridgeway archive is absent or unsafe")
    if bundle.get("sha256") != _digest(path) or bundle.get("byte_size") != path.stat().st_size:
        raise ValueError("Ridgeway archive differs from its manifest")
    return path


def _evidence_index(archive_path: Path) -> dict[str, Any]:
    matches: list[tarfile.TarInfo] = []
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if not member.name or "\\" in member.name or path.is_absolute() or ".." in path.parts:
                raise ValueError("Ridgeway archive contains an unsafe path")
            if path.as_posix() == "evidence-index.json":
                matches.append(member)
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 64 * 1024 * 1024:
            raise ValueError("Ridgeway archive has no unique bounded evidence index")
        stream = archive.extractfile(matches[0])
        if stream is None:
            raise ValueError("Ridgeway evidence index cannot be read")
        payload = json.loads(stream.read())
    if not isinstance(payload, dict):
        raise TypeError("Ridgeway evidence index must contain an object")
    return payload


def _jobs(index: dict[str, Any], key: str, *, expected_count: int) -> dict[str, dict[str, Any]]:
    values = index.get(key)
    if not isinstance(values, list) or len(values) != expected_count:
        raise ValueError(f"Ridgeway {key} coverage is incomplete")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("direct_run_id"), str):
            raise TypeError(f"Ridgeway {key} contains an invalid direct job")
        run_id = value["direct_run_id"]
        if run_id in result:
            raise ValueError(f"Ridgeway {key} repeats direct job {run_id}")
        if (
            value.get("state") != "SUCCEEDED"
            or value.get("exact") is not True
            or value.get("qa_status") != "ACCEPTED"
            or value.get("test_only") is not False
        ):
            raise ValueError(f"Ridgeway direct job is not accepted exact evidence: {run_id}")
        result[run_id] = value
    return result


def _close(reference: Any, candidate: Any, *, label: str) -> tuple[float, float]:
    if reference is None or candidate is None:
        if reference is not candidate:
            raise ValueError(f"Ridgeway rerun changed null semantics for {label}")
        return 0.0, 0.0
    if isinstance(reference, bool) or isinstance(candidate, bool):
        raise TypeError(f"Ridgeway numeric evidence is boolean for {label}")
    try:
        left = float(reference)
        right = float(candidate)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Ridgeway numeric evidence is invalid for {label}") from exc
    if not math.isfinite(left) or not math.isfinite(right):
        raise ValueError(f"Ridgeway numeric evidence is non-finite for {label}")
    absolute = abs(left - right)
    relative = absolute / max(abs(left), abs(right), 1e-30)
    if not math.isclose(
        left,
        right,
        rel_tol=RELATIVE_TOLERANCE,
        abs_tol=ABSOLUTE_TOLERANCE,
    ):
        raise ValueError(
            f"Ridgeway protected rerun differs outside tolerance for {label}: "
            f"reference={left!r}, candidate={right!r}"
        )
    return absolute, relative


def _pair_metrics(job: dict[str, Any], *, run_id: str) -> dict[str, dict[str, Any]]:
    values = job.get("pair_metrics")
    if not isinstance(values, list) or not values:
        raise ValueError(f"Ridgeway direct job has no pair metrics: {run_id}")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("pair_id"), str):
            raise TypeError(f"Ridgeway pair metric is invalid for {run_id}")
        pair_id = value["pair_id"]
        if pair_id in result:
            raise ValueError(f"Ridgeway pair metric repeats {run_id}/{pair_id}")
        result[pair_id] = value
    return result


def _compare_job_sets(
    reference: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> tuple[int, float, float]:
    if set(reference) != set(candidate):
        raise ValueError("Ridgeway protected rerun direct-job identities differ")
    comparisons = 0
    max_absolute = 0.0
    max_relative = 0.0
    stable_fields = ("phase", "portfolio_id", "scenario_id", "revision_id")
    numeric_fields = (
        "effective_resistance",
        "baseline_effective_resistance",
        "baseline_relative_change_percent",
    )
    for run_id in sorted(reference):
        left = reference[run_id]
        right = candidate[run_id]
        if any(left.get(field) != right.get(field) for field in stable_fields):
            raise ValueError(f"Ridgeway protected rerun changed direct-job identity: {run_id}")
        left_pairs = _pair_metrics(left, run_id=run_id)
        right_pairs = _pair_metrics(right, run_id=run_id)
        if set(left_pairs) != set(right_pairs):
            raise ValueError(f"Ridgeway protected rerun changed focal-pair coverage: {run_id}")
        for pair_id in sorted(left_pairs):
            for field in numeric_fields:
                absolute, relative = _close(
                    left_pairs[pair_id].get(field),
                    right_pairs[pair_id].get(field),
                    label=f"{run_id}/{pair_id}/{field}",
                )
                comparisons += 1
                max_absolute = max(max_absolute, absolute)
                max_relative = max(max_relative, relative)
    return comparisons, max_absolute, max_relative


def _current_rasters(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = projection.get("currentRasters")
    if not isinstance(values, list) or len(values) != 60:
        raise ValueError("Ridgeway replay does not retain all 60 current rasters")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("runId"), str):
            raise TypeError("Ridgeway replay contains an invalid current raster")
        run_id = value["runId"]
        if run_id in result:
            raise ValueError(f"Ridgeway replay repeats current raster {run_id}")
        result[run_id] = value
    return result


def _compare_current_rasters(
    reference: dict[str, Any], candidate: dict[str, Any]
) -> tuple[int, float, float]:
    left = _current_rasters(reference)
    right = _current_rasters(candidate)
    if set(left) != set(right):
        raise ValueError("Ridgeway protected rerun current-raster identities differ")
    comparisons = 0
    max_absolute = 0.0
    max_relative = 0.0
    for run_id in sorted(left):
        left_item = left[run_id]
        right_item = right[run_id]
        for field in ("portfolioId", "scenarioId", "width", "height", "nodata"):
            if left_item.get(field) != right_item.get(field):
                raise ValueError(
                    f"Ridgeway protected rerun changed current-raster {field}: {run_id}"
                )
        left_cells = left_item.get("cells")
        right_cells = right_item.get("cells")
        if not isinstance(left_cells, list) or not isinstance(right_cells, list):
            raise TypeError(f"Ridgeway current-raster cells are invalid: {run_id}")
        if len(left_cells) != len(right_cells) or len(left_cells) != (
            int(left_item["width"]) * int(left_item["height"])
        ):
            raise ValueError(f"Ridgeway current-raster cell coverage differs: {run_id}")
        for index, (left_value, right_value) in enumerate(
            zip(left_cells, right_cells, strict=True)
        ):
            absolute, relative = _close(
                left_value,
                right_value,
                label=f"{run_id}/cell-{index}",
            )
            comparisons += 1
            max_absolute = max(max_absolute, absolute)
            max_relative = max(max_relative, relative)
    return comparisons, max_absolute, max_relative


def _teaching_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    selection = manifest.get("teaching_selection")
    if not isinstance(selection, dict) or selection.get("release_eligible") is not True:
        raise ValueError("Ridgeway teaching selection is not release eligible")
    keys = (
        "low_cost_caution_portfolio_id",
        "carry_forward_portfolio_id",
        "over_budget_portfolio_id",
        "hypothesis_collapse_portfolio_id",
        "map_rerun_portfolio_ids",
        "observed_map_review_flags",
    )
    result = {key: selection.get(key) for key in keys}
    if any(value is None for value in result.values()):
        raise ValueError("Ridgeway teaching selection omits a required observed role")
    return result


def compare(
    *,
    source_commit: str,
    reference_directory: Path,
    producer_directory: Path,
) -> dict[str, object]:
    if not SOURCE_SHA.fullmatch(source_commit):
        raise ValueError("source commit must be an exact lowercase Git commit SHA")
    reference_manifest_path = reference_directory / "direct-evidence-manifest.json"
    producer_manifest_path = producer_directory / "direct-evidence-manifest.json"
    reference_manifest = _object(reference_manifest_path, label="reference direct manifest")
    producer_manifest = _object(producer_manifest_path, label="producer direct manifest")
    for label, manifest in (("reference", reference_manifest), ("producer", producer_manifest)):
        if (
            manifest.get("schema_version") != "ridgeway-direct-evidence/v1.1"
            or manifest.get("release_replay_eligible") is not True
            or manifest.get("matrix_job_count") != 1_536
            or manifest.get("map_job_count") != 60
        ):
            raise ValueError(f"{label} Ridgeway evidence is not release eligible")
    if reference_manifest.get("model_sha256") != producer_manifest.get("model_sha256"):
        raise ValueError("Ridgeway protected rerun model identity differs")
    if _teaching_identity(reference_manifest) != _teaching_identity(producer_manifest):
        raise ValueError("Ridgeway protected rerun teaching selection differs")

    reference_index = _evidence_index(_archive_path(reference_directory, reference_manifest))
    producer_index = _evidence_index(_archive_path(producer_directory, producer_manifest))
    if reference_index.get("model_sha256") != producer_index.get("model_sha256"):
        raise ValueError("Ridgeway protected rerun evidence-index model differs")
    reference_components = reference_index.get("workflow_components")
    producer_components = producer_index.get("workflow_components")
    if not isinstance(reference_components, dict) or not isinstance(producer_components, dict):
        raise TypeError("Ridgeway evidence indexes omit workflow components")
    portable_reference = {
        key: value
        for key, value in reference_components.items()
        if key not in PLATFORM_SPECIFIC_WORKFLOW_COMPONENTS
    }
    portable_producer = {
        key: value
        for key, value in producer_components.items()
        if key not in PLATFORM_SPECIFIC_WORKFLOW_COMPONENTS
    }
    if portable_reference != portable_producer:
        raise ValueError("Ridgeway protected rerun scientific source components differ")

    matrix = _compare_job_sets(
        _jobs(reference_index, "matrix_jobs", expected_count=1_536),
        _jobs(producer_index, "matrix_jobs", expected_count=1_536),
    )
    maps = _compare_job_sets(
        _jobs(reference_index, "map_jobs", expected_count=60),
        _jobs(producer_index, "map_jobs", expected_count=60),
    )
    reference_projection = _object(
        reference_directory / "ui_projection.json", label="reference UI projection"
    )
    producer_projection = _object(
        producer_directory / "ui_projection.json", label="producer UI projection"
    )
    rasters = _compare_current_rasters(reference_projection, producer_projection)
    maxima = (matrix[1], matrix[2], maps[1], maps[2], rasters[1], rasters[2])
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit_sha": source_commit,
        "status": "PASSED",
        "reference_manifest_sha256": _digest(reference_manifest_path),
        "producer_manifest_sha256": _digest(producer_manifest_path),
        "reference_archive_sha256": _digest(_archive_path(reference_directory, reference_manifest)),
        "producer_archive_sha256": _digest(_archive_path(producer_directory, producer_manifest)),
        "model_sha256": reference_manifest["model_sha256"],
        "reference_workflow_manifest_sha256": reference_manifest["workflow_manifest_sha256"],
        "producer_workflow_manifest_sha256": producer_manifest["workflow_manifest_sha256"],
        "portable_workflow_components_sha256": _canonical_sha(portable_reference),
        "matrix_job_count": 1_536,
        "map_job_count": 60,
        "pair_metric_value_comparisons": matrix[0] + maps[0],
        "current_raster_cell_comparisons": rasters[0],
        "absolute_tolerance": ABSOLUTE_TOLERANCE,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "maximum_absolute_difference": max(maxima[0], maxima[2], maxima[4]),
        "maximum_relative_difference": max(maxima[1], maxima[3], maxima[5]),
        "teaching_selection_matched": True,
    }


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--reference-directory", required=True, type=Path)
    parser.add_argument("--producer-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare", type=Path)
    arguments = parser.parse_args()
    try:
        payload = compare(
            source_commit=arguments.source_commit,
            reference_directory=arguments.reference_directory.resolve(strict=True),
            producer_directory=arguments.producer_directory.resolve(strict=True),
        )
        if arguments.compare is not None:
            expected = _object(arguments.compare, label="protected rerun differential")
            if payload != expected:
                raise ValueError("Ridgeway protected rerun differential receipt differs")
        _atomic_json(arguments.output, payload)
    except (
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        tarfile.TarError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        "Ridgeway protected rerun matched "
        f"{payload['matrix_job_count'] + payload['map_job_count']} direct jobs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
