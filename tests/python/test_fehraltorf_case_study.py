from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_fehraltorf_case_study.py"
SPEC = importlib.util.spec_from_file_location("build_fehraltorf_case_study", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

PINNED_CASE = ROOT / "benchmarks" / "realistic_fehraltorf" / "v1" / "case-study.json"


def test_threshold_and_365_day_conversion_match_published_method() -> None:
    result = builder._annual_volume_m3([0.0029, 0.003, 0.006])

    assert result == pytest.approx(((0.0 + 0.003 + 0.006) / 3) * 31_536_000 / 1000)


def test_reduction_uses_paired_annual_differences_not_ratio_of_medians() -> None:
    baseline = [1.0, 10.0, 100.0]
    gi = [0.0, 9.0, 0.0]

    result = builder._paired_annual_reduction_percent(baseline, gi)

    assert result == pytest.approx(10.0)
    ratio_of_medians = (pytest.approx((10.0 - 0.0) / 10.0 * 100.0))
    assert result != ratio_of_medians


def test_report_parser_extracts_independent_qa_dimensions() -> None:
    report = """
EPA STORM WATER MANAGEMENT MODEL - VERSION 5.1 (Build 5.1.13)
The model is not complete and may still contain errors!
WARNING 04: first warning
WARNING 10: second warning
Runoff Quantity Continuity
Continuity Error (%) ..... -0.913
Groundwater Continuity
Continuity Error (%) ..... -54.273
Flow Routing Continuity
Continuity Error (%) ..... -2.107
Highest Continuity Errors
*************************
Node A (-253.94%)
Node B (12.50%)
***************************
Time-Step Critical Elements
Percent Not Converging : 0.51
"""

    qa = builder._parse_report(report)

    assert qa == {
        "swmm_version": "5.1 (Build 5.1.13)",
        "runoff_continuity_error_percent": -0.913,
        "groundwater_continuity_error_percent": -54.273,
        "routing_continuity_error_percent": -2.107,
        "warning_count": 2,
        "percent_not_converging": 0.51,
        "max_abs_node_continuity_error_percent": 253.94,
        "model_disclaimer_present": True,
    }


def test_member_mapping_covers_the_public_naming_difference() -> None:
    assert builder._csv_member("CB", "bb").endswith("results_bb_CB.csv")
    assert builder._report_member("CB", "bb").endswith("5b_cal_comb_bc.rpt")
    assert builder._report_member("BI", "gr").endswith("3d_cal_inflow_gr.rpt")


def test_source_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(b"not the pinned archive")

    with pytest.raises(builder.CaseStudyBuildError, match="SHA-256 mismatch"):
        builder._verify_sha256(source, "0" * 64, "test source")


def test_pinned_case_reconciles_author_method_qa_and_provenance() -> None:
    payload = json.loads(PINNED_CASE.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in payload["cases"]}
    claims = {claim["gi_scenario_id"]: claim for claim in payload["comparison_claims"]}
    sources = {source["source_id"]: source for source in payload["sources"]}

    assert payload["schema_version"] == "1.0"
    assert payload["case_id"] == "eawag-fehraltorf-published-output-reanalysis-v1"
    assert payload["analysis_boundary"]["fresh_swmm_rerun"] is False
    assert len(cases) == 20
    assert {case["year_count"] for case in cases.values()} == {30}

    assert claims["bb"] == {
        "claim_id": "fehraltorf-bb-model-structure-sensitivity",
        "gi_scenario_id": "bb",
        "min_reduction_percent": 56.8133,
        "max_reduction_percent": 92.2367,
        "spread_percentage_points": 35.4234,
        "min_structure_id": "CB",
        "max_structure_id": "GW",
        "limitation": (
            "Published modeled CSO response varies with model structure; this comparison does "
            "not establish field performance or resolve the reports' QA findings."
        ),
    }
    assert claims["pp"]["spread_percentage_points"] == pytest.approx(29.9552)
    assert claims["gr"]["spread_percentage_points"] == pytest.approx(7.4974)

    combined_bioretention = cases["CB-bb"]
    assert combined_bioretention["median_annual_cso_m3"] == pytest.approx(69_956.694)
    assert combined_bioretention["reduction_from_same_structure_baseline_percent"] == pytest.approx(
        56.8133
    )
    assert combined_bioretention["qa"]["groundwater_continuity_error_percent"] == pytest.approx(
        -54.273
    )
    assert combined_bioretention["qa"]["routing_continuity_error_percent"] == pytest.approx(
        -2.107
    )
    assert combined_bioretention["qa"]["model_disclaimer_present"] is True

    assert sources["swmm-input-archive"]["sha256"] == builder.INPUT_ARCHIVE_SHA256
    assert sources["published-results-archive"]["sha256"] == builder.RESULTS_ARCHIVE_SHA256
    assert sources["dataset-readme"]["sha256"] == builder.README_SHA256

    serialized = PINNED_CASE.read_text(encoding="utf-8")
    assert "/Users/" not in serialized
    assert "/private/" not in serialized
    assert "WANDB_API_KEY" not in serialized
    assert hashlib.sha256(serialized.encode()).hexdigest() == hashlib.sha256(
        PINNED_CASE.read_bytes()
    ).hexdigest()
