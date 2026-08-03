from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_guided_ui_fixture.py"
SPEC = importlib.util.spec_from_file_location("generate_guided_ui_fixture", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(
        (ROOT / "benchmarks" / "guided_exact" / "manifest.json").read_text(encoding="utf-8")
    )


def test_exact_manifest_projects_all_retained_designs(manifest: dict) -> None:
    projection = MODULE.project(manifest)

    assert len(projection["alternatives"]) == 220
    assert (
        projection["retained_shortlist_sequences"]
        == manifest["search"]["shortlist_design_sequences"]
    )
    assert len(projection["retained_shortlist_sequences"]) == 8


def test_exact_manifest_declares_redistributable_fixture_provenance(manifest: dict) -> None:
    assert manifest["license"] == "CC0-1.0"
    assert manifest["redistribution"] == "permitted"
    assert manifest["attribution"]
    assert manifest["modification_notice"]
    assert manifest["source_model"]["origin"] == "self-authored"
    assert manifest["source_model"]["version"]


def test_retained_artifact_bytes_match_manifest(manifest: dict) -> None:
    fixture_root = (ROOT / "benchmarks" / "guided_exact").resolve()
    retained_runs = [run for run in manifest["runs"] if run["artifacts_retained"]]

    assert len(retained_runs) == manifest["job_accounting"]["retained_jobs"] == 36
    for run in retained_runs:
        for artifact_kind in ("input", "report", "output"):
            relative_path = Path(run["artifacts"][artifact_kind])
            assert not relative_path.is_absolute()
            artifact_path = (fixture_root / relative_path).resolve()
            assert artifact_path.is_relative_to(fixture_root)
            assert artifact_path.is_file()
            expected_hash = run["hashes"][f"{artifact_kind}_sha256"]
            assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == expected_hash


def test_projection_rejects_duplicate_storm_coverage(manifest: dict) -> None:
    malformed = copy.deepcopy(manifest)
    design_runs = [run for run in malformed["runs"] if run["kind"] == "alternative"][:4]
    design_runs[-1]["storm_id"] = design_runs[0]["storm_id"]

    with pytest.raises(ValueError, match="one run for every storm"):
        MODULE.project(malformed)


def test_projection_rejects_non_sha256_hash(manifest: dict) -> None:
    malformed = copy.deepcopy(manifest)
    malformed["runs"][0]["hashes"]["output_sha256"] = "not-a-sha256"

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        MODULE.project(malformed)


def test_projection_rejects_cross_storm_vector_drift(manifest: dict) -> None:
    malformed = copy.deepcopy(manifest)
    design_runs = [run for run in malformed["runs"] if run["kind"] == "alternative"][:4]
    design_runs[-1]["design_vector"] = [4, 4, 4, 6]

    with pytest.raises(ValueError, match="changes vector or sequence"):
        MODULE.project(malformed)
