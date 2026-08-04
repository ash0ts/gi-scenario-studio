from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def comparator() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "compare_ridgeway_protected_rerun.py"
    spec = importlib.util.spec_from_file_location("ridgeway_protected_rerun", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _job(phase: str, index: int, *, delta: float = 0.0) -> dict[str, object]:
    return {
        "direct_run_id": f"{phase}_portfolio_{index:04d}_scenario_{index % 12:02d}",
        "phase": phase,
        "portfolio_id": f"portfolio_{index:04d}",
        "scenario_id": f"scenario_{index % 12:02d}",
        "revision_id": "revision_ridgeway_v1",
        "state": "SUCCEEDED",
        "exact": True,
        "qa_status": "ACCEPTED",
        "test_only": False,
        "pair_metrics": [
            {
                "pair_id": "pair_1_3",
                "effective_resistance": 2.0 + (delta if index == 0 else 0.0),
                "baseline_effective_resistance": 2.5,
                "baseline_relative_change_percent": -20.0,
            }
        ],
    }


def _package(root: Path, *, delta: float = 0.0) -> Path:
    root.mkdir(parents=True)
    index = {
        "schema_version": "ridgeway-evidence-index/v1.1",
        "model_sha256": "1" * 64,
        "workflow_manifest_sha256": "2" * 64,
        "workflow_components": {
            "project": "3" * 64,
            "manifest": "4" * 64,
            "protocol": "5" * 64,
            "study_orchestrator": "6" * 64,
            "julia_runtime_tree": "7" * 64,
            "python_runtime": "8" * 64,
        },
        "matrix_jobs": [_job("matrix", index, delta=delta) for index in range(1_536)],
        "map_jobs": [_job("map", index, delta=delta) for index in range(60)],
    }
    archive = root / "ridgeway-direct-evidence-test.tar.gz"
    payload = json.dumps(index, sort_keys=True).encode()
    with tarfile.open(archive, mode="w:gz") as output:
        member = tarfile.TarInfo("evidence-index.json")
        member.size = len(payload)
        member.mtime = 0
        output.addfile(member, io.BytesIO(payload))
    teaching = {
        "release_eligible": True,
        "low_cost_caution_portfolio_id": "portfolio_0001",
        "carry_forward_portfolio_id": "portfolio_0002",
        "over_budget_portfolio_id": "portfolio_0003",
        "hypothesis_collapse_portfolio_id": "portfolio_0004",
        "map_rerun_portfolio_ids": [
            "portfolio_0000",
            "portfolio_0001",
            "portfolio_0002",
            "portfolio_0003",
            "portfolio_0004",
        ],
        "observed_map_review_flags": {
            "portfolio_0001": True,
            "portfolio_0002": False,
            "portfolio_0003": False,
            "portfolio_0004": False,
        },
    }
    (root / "direct-evidence-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "ridgeway-direct-evidence/v1.1",
                "release_replay_eligible": True,
                "matrix_job_count": 1_536,
                "map_job_count": 60,
                "model_sha256": "1" * 64,
                "workflow_manifest_sha256": "2" * 64,
                "teaching_selection": teaching,
                "evidence_bundle": {
                    "filename": archive.name,
                    "sha256": _sha(archive),
                    "byte_size": archive.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "ui_projection.json").write_text(
        json.dumps(
            {
                "currentRasters": [
                    {
                        "runId": f"map_portfolio_{index:04d}_scenario_{index % 12:02d}",
                        "portfolioId": f"portfolio_{index:04d}",
                        "scenarioId": f"scenario_{index % 12:02d}",
                        "width": 1,
                        "height": 1,
                        "nodata": -9999,
                        "cells": [1.0 + (delta if index == 0 else 0.0)],
                    }
                    for index in range(60)
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def test_protected_rerun_compares_every_job_pair_and_current_cell(
    comparator: ModuleType, tmp_path: Path
) -> None:
    reference = _package(tmp_path / "reference")
    producer = _package(tmp_path / "producer", delta=1e-9)

    result = comparator.compare(
        source_commit="a" * 40,
        reference_directory=reference,
        producer_directory=producer,
    )

    assert result["status"] == "PASSED"
    assert result["matrix_job_count"] == 1_536
    assert result["map_job_count"] == 60
    assert result["pair_metric_value_comparisons"] == (1_536 + 60) * 3
    assert result["current_raster_cell_comparisons"] == 60
    assert result["maximum_absolute_difference"] == pytest.approx(1e-9)


def test_protected_rerun_rejects_one_out_of_tolerance_direct_value(
    comparator: ModuleType, tmp_path: Path
) -> None:
    reference = _package(tmp_path / "reference")
    producer = _package(tmp_path / "producer", delta=1e-4)

    with pytest.raises(ValueError, match="outside tolerance"):
        comparator.compare(
            source_commit="a" * 40,
            reference_directory=reference,
            producer_directory=producer,
        )
