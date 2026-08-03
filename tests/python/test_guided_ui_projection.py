from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from gi_scenario_core.demo import (
    GUIDED_COST_BASIS_ID,
    GUIDED_PROJECT_NAME,
    GUIDED_STORY_ID,
    guided_cost_basis_payload,
)

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
    assert len(projection["retained_shortlist_sequences"]) == 4


def test_projection_exposes_one_versioned_cost_basis_and_visible_rules(manifest: dict) -> None:
    projection = MODULE.project(manifest)
    story = projection["guided_story"]

    assert projection["schema_version"] == "2.0"
    assert projection["project_name"] == GUIDED_PROJECT_NAME
    assert projection["story_id"] == story["story_id"] == GUIDED_STORY_ID
    assert projection["cost_basis"] == guided_cost_basis_payload()
    assert story["schema_version"] == projection["cost_basis"]["schema_version"] == "2.0"
    assert projection["cost_basis"]["basis_id"] == GUIDED_COST_BASIS_ID
    assert [item["rule_id"] for item in story["rules"]] == [
        "aggregate_volume_reduction",
        "present_value_cost_cap",
        "no_outfall_peak_increase",
        "solver_qa",
    ]
    assert [item["threshold"] for item in story["rules"]] == [6.0, 200.0, 0.0, "accepted"]


def test_projection_has_ten_ordered_learning_checkpoints(manifest: dict) -> None:
    checkpoints = MODULE.project(manifest)["guided_story"]["checkpoints"]

    assert len(checkpoints) == 10
    assert [item["sequence"] for item in checkpoints] == list(range(1, 11))
    assert len({item["checkpoint_id"] for item in checkpoints}) == 10
    assert all(item["question"] and item["learning_outcome"] for item in checkpoints)


def test_teaching_alternatives_are_exact_manifest_projections(manifest: dict) -> None:
    projection = MODULE.project(manifest)
    alternatives = {item["id"]: item for item in projection["teaching_alternatives"]}

    assert projection["teaching_alternative_ids"] == [
        "ALT-017",
        "ALT-207",
        "ALT-109",
        "ALT-014",
    ]
    assert alternatives["ALT-017"]["vector"] == [0, 0, 1, 6]
    assert alternatives["ALT-017"]["present_value_cost_thousand_usd"] == 54.99
    assert alternatives["ALT-017"]["outfall_volume_reduction_percent"] == 4.14
    assert alternatives["ALT-017"]["screening"]["worst_peak_change"] == {
        "storm_id": "synthetic_long_2_5in",
        "baseline_outfall_peak_cfs": 3.14,
        "alternative_outfall_peak_cfs": 3.21,
        "delta_cfs": 0.07,
        "delta_percent": 2.23,
    }

    assert alternatives["ALT-207"]["vector"] == [0, 3, 2, 6]
    assert alternatives["ALT-207"]["present_value_cost_thousand_usd"] == 190.25
    assert alternatives["ALT-207"]["outfall_volume_reduction_percent"] == 6.42
    assert alternatives["ALT-207"]["screening"]["status"] == "meets_all"

    assert alternatives["ALT-109"]["vector"] == [1, 3, 2, 6]
    assert alternatives["ALT-109"]["present_value_cost_thousand_usd"] == 222.01
    assert alternatives["ALT-109"]["outfall_volume_reduction_percent"] == 6.95

    assert alternatives["ALT-014"]["vector"] == [4, 3, 4, 5]
    assert alternatives["ALT-014"]["present_value_cost_thousand_usd"] == 361.86
    assert alternatives["ALT-014"]["outfall_volume_reduction_percent"] == 8.82

    for alternative in alternatives.values():
        assert alternative["nondominated"]
        assert len(alternative["storm_runs"]) == 4
        assert all(run["qa_status"] == "accepted" for run in alternative["storm_runs"])


def test_rule_matrix_explains_each_teaching_role(manifest: dict) -> None:
    alternatives = {
        item["teaching_role"]: item for item in MODULE.project(manifest)["teaching_alternatives"]
    }

    caution = {
        item["rule_id"]: item["passes"]
        for item in alternatives["low_cost_caution"]["screening"]["checks"]
    }
    assert caution == {
        "aggregate_volume_reduction": False,
        "present_value_cost_cap": True,
        "no_outfall_peak_increase": False,
        "solver_qa": True,
    }

    carry_forward = alternatives["carry_forward_candidate"]
    assert carry_forward["screening"]["meets_all_rules"]
    assert all(item["passes"] for item in carry_forward["screening"]["checks"])

    for role in ("stronger_over_budget", "observed_performance_extreme"):
        checks = {
            item["rule_id"]: item["passes"] for item in alternatives[role]["screening"]["checks"]
        }
        assert checks["aggregate_volume_reduction"]
        assert not checks["present_value_cost_cap"]
        assert checks["no_outfall_peak_increase"]
        assert checks["solver_qa"]


def test_replay_and_rerun_availability_are_not_conflated(manifest: dict) -> None:
    projection = MODULE.project(manifest)
    modes = projection["guided_story"]["execution_modes"]

    assert modes["default"] == "verified_replay"
    assert modes["verified_replay"]["availability"] == "available_offline"
    assert not modes["verified_replay"]["executes_solver"]
    assert modes["verified_replay"]["exact_run_records"] == 884
    assert modes["verified_replay"]["qa_accepted_records"] == 884
    assert modes["verified_replay"]["retained_full_artifact_run_sets"] == 20

    assert modes["optional_rerun"]["availability"] == "conditional_on_configured_pinned_solver"
    assert modes["optional_rerun"]["executes_solver"]
    assert modes["optional_rerun"]["runtime_status"] == "not_assessed_by_static_fixture"
    assert modes["optional_rerun"]["required_solver"]["sha256"] == manifest["solver"]["sha256"]

    for alternative in projection["teaching_alternatives"]:
        availability = alternative["artifact_availability"]
        assert availability["exact_metrics_and_hashes"] == "available"
        assert availability["full_run_artifacts"] == "retained"
        assert availability["retained_run_sets"] == 4
        assert availability["can_open_local_inp_rpt_out"]

    metrics_only = next(item for item in projection["alternatives"] if item["id"] == "ALT-001")
    assert metrics_only["artifact_availability"]["exact_metrics_and_hashes"] == "available"
    assert metrics_only["artifact_availability"]["full_run_artifacts"] == "not_retained"
    assert not metrics_only["artifact_availability"]["can_open_local_inp_rpt_out"]


def test_benchmark_story_metadata_matches_projection(manifest: dict) -> None:
    metadata = json.loads(
        (ROOT / "benchmarks" / "guided_exact" / "story.json").read_text(encoding="utf-8")
    )
    projection = MODULE.project(manifest)

    assert metadata["hydraulic_values_in_this_file"] is False
    assert metadata["schema_version"] == projection["schema_version"] == "2.0"
    assert metadata["story_id"] == projection["story_id"]
    assert metadata["project_name"] == projection["project_name"]
    assert metadata["exact_fixture_id"] == projection["fixture_id"]
    assert metadata["cost_basis_id"] == projection["cost_basis"]["basis_id"]
    assert [
        (item["alternative_id"], item["design_sequence"], item["role"])
        for item in metadata["teaching_alternatives"]
    ] == [
        (item["id"], item["sequence"], item["teaching_role"])
        for item in projection["teaching_alternatives"]
    ]
    assert all(
        item["artifact_availability"]["full_run_artifacts"] == "retained"
        for item in projection["teaching_alternatives"]
    )


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

    assert len(retained_runs) == manifest["job_accounting"]["retained_jobs"] == 20
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


def test_projection_rejects_teaching_vector_drift(manifest: dict) -> None:
    malformed = copy.deepcopy(manifest)
    for run in malformed["runs"]:
        if run.get("design_sequence") == 16:
            run["design_vector"] = [0, 0, 2, 6]

    with pytest.raises(ValueError, match="changed design vector"):
        MODULE.project(malformed)
