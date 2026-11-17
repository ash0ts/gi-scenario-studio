#!/usr/bin/env python3
"""Run the frozen packaged-engine conformance suite and write its receipt.

This script is intended for the protected signed-release workflow.  The output
does not become authoritative merely because this script created it: the
workflow must subsequently apply GitHub build provenance to the exact receipt
bytes, and consumers must verify that attestation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ecocorridor_core.contracts import EngineManifest, canonical_sha256
from ecocorridor_core.engine import sha256_file
from ecocorridor_core.engine_provenance import (
    ENGINE_PROVENANCE_SCHEMA,
    EngineBuildProvenanceReceipt,
    EngineConformanceCase,
)


def _write_ascii(path: Path, values: list[list[float]], *, nodata: float) -> None:
    rows = [
        f"ncols {len(values[0])}",
        f"nrows {len(values)}",
        "xllcorner 0",
        "yllcorner 0",
        "cellsize 1",
        f"NODATA_value {nodata:g}",
        *(" ".join(f"{value:g}" for value in row) for row in values),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="ascii")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _artifact_hashes(root: Path, matrix_relative: str) -> dict[str, str]:
    return {
        "resistance": sha256_file(root / "inputs/resistance.asc"),
        "focal": sha256_file(root / "inputs/focal.asc"),
        "included_pairs": sha256_file(root / "inputs/included_pairs.tsv"),
        "request": sha256_file(root / "request.json"),
        "result": sha256_file(root / "result.json"),
        "matrix": sha256_file(root / matrix_relative),
    }


def _run_case(
    *,
    engine: Path,
    root: Path,
    case_id: str,
    resistance_source: Path | None,
    focal_source: Path | None,
    resistance: list[list[float]] | None,
    focal: list[list[float]] | None,
    pairs: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    tolerance: float,
) -> EngineConformanceCase:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    if resistance_source is not None and focal_source is not None:
        (inputs / "resistance.asc").write_bytes(resistance_source.read_bytes())
        (inputs / "focal.asc").write_bytes(focal_source.read_bytes())
    elif resistance is not None and focal is not None:
        _write_ascii(inputs / "resistance.asc", resistance, nodata=-9999)
        _write_ascii(inputs / "focal.asc", focal, nodata=0)
    else:  # pragma: no cover - protected by the fixed callers
        raise RuntimeError("conformance inputs are incomplete")
    (inputs / "included_pairs.tsv").write_text(
        "mode\tinclude\n"
        + "".join(
            f"{pair['from_focal_id']}\t{pair['to_focal_id']}\n" for pair in pairs
        ),
        encoding="ascii",
    )
    request = {
        "protocol_version": "1.0",
        "prepared_run_id": f"engine_conformance_{case_id}",
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/included_pairs.tsv",
        },
        "pairs": pairs,
        "settings": {
            "precision": "double",
            "julia_threads": 1,
            "blas_threads": 1,
            "omp_threads": 1,
            "neighbor_rule": "FOUR",
            "resistance_average": "ARITHMETIC",
            "emit_current_map": False,
        },
        "output_directory": "outputs",
    }
    (root / "request.json").write_bytes(_canonical_bytes(request))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE"}
    }
    environment.update(
        {
            "JULIA_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
    )
    completed = subprocess.run(
        [os.fspath(engine), "--request", "request.json", "--result", "result.json"],
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(f"packaged engine conformance failed for {case_id}")
    try:
        result = json.loads((root / "result.json").read_text(encoding="utf-8"))
        observed = sorted(result["pair_metrics"], key=lambda item: item["pair_id"])
        matrix_relative = str(result["matrix_path"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"packaged engine returned malformed conformance for {case_id}") from exc
    expected = sorted(expected, key=lambda item: item["pair_id"])
    if (
        result.get("status") != "SUCCEEDED"
        or result.get("engine_name") != "Circuitscape"
        or result.get("circuitscape_version") != "5.17.1"
        or [item.get("pair_id") for item in observed]
        != [item.get("pair_id") for item in expected]
    ):
        raise SystemExit(f"packaged engine identity or pairs differ for {case_id}")
    errors = [
        abs(
            float(observed_item["effective_resistance"])
            - float(expected_item["effective_resistance"])
        )
        for observed_item, expected_item in zip(observed, expected, strict=True)
    ]
    if not errors or not all(math.isfinite(value) for value in errors) or max(errors) > tolerance:
        raise SystemExit(f"packaged engine conformance exceeded tolerance for {case_id}")
    authority = (
        "CIRCUITSCAPE_5_17_1_OFFICIAL_FIXTURE"
        if case_id == "official_circuitscape_sgverify4"
        else "HAND_CALCULATED_ANALYTIC"
    )
    expected_scalar = (
        float(expected[0]["effective_resistance"])
        if len(expected) == 1
        else None
    )
    observed_scalar = (
        float(observed[0]["effective_resistance"])
        if len(observed) == 1
        else None
    )
    payload = {
        "case_id": case_id,
        "authority": authority,
        "exact_engine_execution": True,
        "passed": True,
        "pair_count": len(expected),
        "expected_effective_resistance": expected_scalar,
        "observed_effective_resistance": observed_scalar,
        "absolute_tolerance": tolerance,
        "maximum_absolute_error": max(errors),
        "expected_metrics_sha256": canonical_sha256(expected),
        "observed_metrics_sha256": canonical_sha256(observed),
        "artifact_sha256": _artifact_hashes(root, matrix_relative),
    }
    return EngineConformanceCase(
        **payload,
        case_sha256=canonical_sha256(payload),
    )


def _official_expected(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        left_text, right_text, resistance_text = line.split()
        left = int(float(left_text))
        right = int(float(right_text))
        resistance = float(resistance_text)
        if resistance <= 0:
            continue
        pair_id = f"pair_{min(left, right)}_{max(left, right)}"
        pairs.append(
            {
                "pair_id": pair_id,
                "from_focal_id": left,
                "to_focal_id": right,
            }
        )
        expected.append(
            {
                "pair_id": pair_id,
                "from_focal_id": left,
                "to_focal_id": right,
                "effective_resistance": resistance,
            }
        )
    if len(expected) != 15:
        raise SystemExit("official sgVerify4 fixture must contain 15 positive pairs")
    return pairs, expected


def _atomic_json(path: Path, value: object) -> None:
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--engine-manifest", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--release-signature", type=Path, required=True)
    parser.add_argument("--release-public-key", type=Path, required=True)
    parser.add_argument("--official-fixture-root", type=Path, required=True)
    parser.add_argument("--official-expected", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--producer-run-id", type=int, required=True)
    parser.add_argument("--producer-run-attempt", type=int, required=True)
    parser.add_argument("--producer-url", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument(
        "--target",
        choices=("macos-arm64", "macos-x64", "windows-x64"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    engine = arguments.engine.resolve(strict=True)
    engine_manifest_path = arguments.engine_manifest.resolve(strict=True)
    if any(
        path.is_symlink() or not path.is_file()
        for path in (
            engine,
            engine_manifest_path,
            arguments.release_manifest,
            arguments.release_signature,
            arguments.release_public_key,
            arguments.official_expected,
        )
    ):
        raise SystemExit("engine provenance inputs must be regular non-symlink files")
    manifest = EngineManifest.model_validate_json(
        engine_manifest_path.read_text(encoding="utf-8")
    )
    if sha256_file(engine) != manifest.executable_sha256:
        raise SystemExit("engine executable differs from its typed EngineManifest")
    fixture_root = arguments.official_fixture_root.resolve(strict=True)
    official_pairs, official_expected = _official_expected(arguments.official_expected)
    pair = [{"pair_id": "pair_1_2", "from_focal_id": 1, "to_focal_id": 2}]
    with tempfile.TemporaryDirectory(prefix="ecocorridor-engine-conformance-") as temporary:
        root = Path(temporary)
        cases = (
            _run_case(
                engine=engine,
                root=root / "series",
                case_id="hand_calculated_series_four_ohm",
                resistance_source=None,
                focal_source=None,
                resistance=[[-9999, -9999, -9999], [2, 2, 2], [-9999, -9999, -9999]],
                focal=[[0, 0, 0], [1, 0, 2], [0, 0, 0]],
                pairs=pair,
                expected=[{**pair[0], "effective_resistance": 4.0}],
                tolerance=1.0e-8,
            ),
            _run_case(
                engine=engine,
                root=root / "parallel",
                case_id="hand_calculated_parallel_one_ohm",
                resistance_source=None,
                focal_source=None,
                resistance=[[-9999, -9999], [2, 2], [2, 2], [-9999, -9999]],
                focal=[[0, 0], [1, 2], [1, 2], [0, 0]],
                pairs=pair,
                expected=[{**pair[0], "effective_resistance": 1.0}],
                tolerance=1.0e-8,
            ),
            _run_case(
                engine=engine,
                root=root / "official",
                case_id="official_circuitscape_sgverify4",
                resistance_source=fixture_root / "cellmap.asc",
                focal_source=fixture_root / "points.asc",
                resistance=None,
                focal=None,
                pairs=official_pairs,
                expected=official_expected,
                tolerance=5.0e-8,
            ),
        )
    cases = tuple(sorted(cases, key=lambda item: item.case_id))
    payload = {
        "schema_version": ENGINE_PROVENANCE_SCHEMA,
        "trust_profile": "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "test_only": False,
        "publishable": True,
        "source_repository": "https://github.com/ash0ts/gi-scenario-studio",
        "source_commit": arguments.source_commit,
        "source_ref": "refs/heads/main",
        "producer_workflow": ".github/workflows/ecocorridor-release.yml",
        "producer_run_id": arguments.producer_run_id,
        "producer_run_attempt": arguments.producer_run_attempt,
        "producer_url": arguments.producer_url,
        "release_version": arguments.release_version.removeprefix("v"),
        "target": arguments.target,
        "engine_name": "Circuitscape",
        "engine_version": "5.17.1",
        "julia_version": "1.12.6",
        "engine_executable_sha256": manifest.executable_sha256,
        "engine_manifest_sha256": manifest.sha256,
        "engine_manifest_file_sha256": sha256_file(engine_manifest_path),
        "release_manifest_sha256": sha256_file(arguments.release_manifest),
        "release_manifest_signature_sha256": sha256_file(arguments.release_signature),
        "release_resource_public_key_sha256": sha256_file(arguments.release_public_key),
        "conformance_cases": [item.model_dump(mode="json") for item in cases],
        "conformance_suite_sha256": canonical_sha256(
            [item.model_dump(mode="json") for item in cases]
        ),
    }
    receipt = EngineBuildProvenanceReceipt(
        **payload,
        receipt_sha256=canonical_sha256(payload),
    )
    _atomic_json(arguments.output, receipt.model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
