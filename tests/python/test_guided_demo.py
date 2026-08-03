from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gi_scenario_core.demo import (
    GUIDED_COST_BASIS_ID,
    GUIDED_PROJECT_NAME,
    GUIDED_SOURCE_MODEL_SHA256,
    GUIDED_STORY_ID,
    create_demo_project,
    guided_candidate_slots,
    guided_cost_basis_payload,
)
from gi_scenario_core.validation import validate_model

ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_demo_project_materializes_the_cedar_trace_evidence_inputs(tmp_path: Path) -> None:
    destination = tmp_path / "cedar-trace"
    result = create_demo_project(destination)
    manifest = json.loads(
        (ROOT / "benchmarks" / "guided_exact" / "manifest.json").read_text(encoding="utf-8")
    )
    bundle = json.loads((destination / "project.json").read_text(encoding="utf-8"))

    assert result["project"]["title"] == bundle["title"] == GUIDED_PROJECT_NAME
    assert result["project"]["project_kind"] == bundle["project_kind"] == "demo"
    assert bundle["model_fitness_passport_id"] is None
    assert result["story_id"] == GUIDED_STORY_ID
    assert result["cost_basis_id"] == GUIDED_COST_BASIS_ID
    assert result["storm_count"] == len(bundle["storm_cases"]) == 4
    assert result["model_sha256"] == manifest["source_model"]["sha256"]
    assert result["model_sha256"] == GUIDED_SOURCE_MODEL_SHA256

    source_model = destination / bundle["base_model_path"]
    assert _sha256(source_model) == GUIDED_SOURCE_MODEL_SHA256
    assert validate_model(source_model.read_text(encoding="utf-8")).blocking_count == 0
    assert (
        _sha256(destination / "sources" / "cedar-trace-reference.inp") == GUIDED_SOURCE_MODEL_SHA256
    )


def test_demo_storm_models_match_retained_exact_baseline_inputs(tmp_path: Path) -> None:
    destination = tmp_path / "cedar-trace"
    create_demo_project(destination)
    bundle = json.loads((destination / "project.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "benchmarks" / "guided_exact" / "manifest.json").read_text(encoding="utf-8")
    )
    baseline_hashes = {
        run["storm_id"]: run["hashes"]["input_sha256"]
        for run in manifest["runs"]
        if run["kind"] == "baseline"
    }

    assert {case["storm_id"] for case in bundle["storm_cases"]} == set(baseline_hashes)
    for storm_case in bundle["storm_cases"]:
        model = destination / storm_case["model_path"]
        assert _sha256(model) == baseline_hashes[storm_case["storm_id"]]
        assert validate_model(model.read_text(encoding="utf-8")).blocking_count == 0


def test_demo_project_uses_shared_story_costs_and_opportunities(tmp_path: Path) -> None:
    destination = tmp_path / "cedar-trace"
    create_demo_project(destination)
    cost_basis = json.loads((destination / "cost-basis.json").read_text(encoding="utf-8"))
    story = json.loads((destination / "guided-story.json").read_text(encoding="utf-8"))
    slots = json.loads((destination / "candidate-slots.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "benchmarks" / "guided_exact" / "manifest.json").read_text(encoding="utf-8")
    )

    assert cost_basis == guided_cost_basis_payload()
    assert story["story_id"] == GUIDED_STORY_ID
    assert len(story["checkpoints"]) == 10
    assert story["execution_modes"]["default"] == "verified_replay"
    assert not story["execution_modes"]["verified_replay"]["executes_solver"]
    assert story["execution_modes"]["optional_rerun"]["executes_solver"]
    assert slots == [item.model_dump(mode="json") for item in guided_candidate_slots()]
    assert slots == manifest["candidate_slots"]
    assert not {"results", "run_metrics", "hydraulic_outputs"}.intersection(story)
