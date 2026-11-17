from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = ROOT / "scripts" / "publish_fehraltorf_case_study.py"


def _module() -> dict[str, object]:
    return runpy.run_path(str(PUBLISHER))


def test_plan_projects_the_pinned_realistic_case_without_changing_its_boundary() -> None:
    module = _module()
    plan = module["build_plan"](entity="wandb", project="gi-scenario-studio-article")

    assert plan["case_id"] == "eawag-fehraltorf-published-output-reanalysis-v1"
    assert plan["analysis_boundary"]["fresh_swmm_rerun"] is False
    assert len(plan["rows"]) == 20
    assert len(plan["comparisons"]) == 3
    bioretention = plan["bounded_claim"]["bioretention"]
    assert bioretention == {
        "gi_scenario_id": "bb",
        "minimum_percent": 56.8133,
        "maximum_percent": 92.2367,
        "spread_percentage_points": pytest.approx(35.4234),
        "minimum_structure_id": "CB",
        "maximum_structure_id": "GW",
    }
    assert plan["qa"]["groundwater_continuity_error_percent"] == -54.273
    assert plan["qa"]["routing_continuity_error_percent"] == -2.107
    assert plan["qa"]["max_abs_node_continuity_error_percent"] == 253.94
    assert plan["qa"]["disposition"] == "ENGINEER_REVIEW_REQUIRED"
    assert "field performance" in " ".join(plan["bounded_claim"]["not_established"])


def test_comparison_is_recomputed_from_rows_not_trusted_from_prose() -> None:
    module = _module()
    plan = module["build_plan"](entity="wandb", project="gi-scenario-studio-article")
    rows = [dict(row) for row in plan["rows"]]
    changed = next(
        row
        for row in rows
        if row["model_structure_id"] == "CB" and row["gi_scenario_id"] == "bb"
    )
    changed["reduction_percent"] = 50.0

    comparison = module["compare_model_structures"](rows)
    bioretention = next(item for item in comparison if item["gi_scenario_id"] == "bb")

    assert bioretention["minimum_percent"] == 50.0
    assert bioretention["spread_percentage_points"] == pytest.approx(42.2367)


def test_weave_trace_url_must_match_the_exact_destination() -> None:
    module = _module()
    validator = module["_validate_trace_url"]

    expected = "https://wandb.ai/wandb/gi-scenario-studio-article/r/call/call-123"
    assert validator(expected, "wandb", "gi-scenario-studio-article") == expected
    with pytest.raises(module["PublicationError"]):
        validator("https://wandb.ai/wandb/wrong-project/r/call/call-123", "wandb", "gi-scenario-studio-article")
    with pytest.raises(module["PublicationError"]):
        validator(expected + "?redirect=1", "wandb", "gi-scenario-studio-article")
    with pytest.raises(module["PublicationError"]):
        validator("http://wandb.ai/wandb/gi-scenario-studio-article/r/call/call-123", "wandb", "gi-scenario-studio-article")


def test_case_validator_rejects_a_claimed_fresh_rerun() -> None:
    module = _module()
    case = json.loads(module["DEFAULT_CASE_STUDY"].read_text(encoding="utf-8"))
    case["analysis_boundary"]["fresh_swmm_rerun"] = True

    with pytest.raises(module["PublicationError"], match="no fresh SWMM rerun"):
        module["_validate_case_study"](case)


def test_dry_run_needs_no_credentials_and_performs_no_network_calls() -> None:
    environment = dict(os.environ)
    environment.pop("WANDB_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            str(PUBLISHER),
            "--entity",
            "wandb",
            "--project",
            "gi-scenario-studio-article",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(result.stdout)
    assert output["dry_run"] is True
    assert output["network_calls"] == 0
    assert output["case_count"] == 20
    assert output["fresh_swmm_rerun"] is False


def test_publication_is_scientific_observability_not_an_agent_claim() -> None:
    source = PUBLISHER.read_text(encoding="utf-8")

    assert 'name="fehraltorf.published_output_reanalysis"' in source
    assert 'name="fehraltorf.compare_model_structures"' in source
    assert 'name="fehraltorf.inspect_report_qa"' in source
    assert 'name="fehraltorf.formulate_bounded_claim"' in source
    assert "wandb.Table" in source
    assert "wandb.Artifact" in source
    assert "expected_wb_run_id = f\"{plan['entity']}/{plan['project']}/{run.id}\"" in source
    assert "Evidence Guide" not in source
    assert '"fresh_swmm_rerun": False' in source
