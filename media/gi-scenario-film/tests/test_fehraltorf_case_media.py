from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

FILM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FILM_ROOT.parents[1]
SCRIPT = FILM_ROOT / "scripts" / "build_fehraltorf_case_media.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_fehraltorf_case_media", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(
    structure: str,
    scenario: str,
    volume: float,
    reduction: float | None,
    *,
    groundwater_error: float | None = -1.0,
    routing_error: float = -0.2,
) -> dict:
    return {
        "case_id": f"{structure}-{scenario}",
        "model_structure_id": structure,
        "gi_scenario_id": scenario,
        "median_annual_cso_m3": volume,
        "reduction_from_same_structure_baseline_percent": reduction,
        "year_count": 30,
        "qa": {
            "swmm_version": "EPA SWMM 5.1 Build 5.1.13",
            "runoff_continuity_error_percent": -0.9,
            "groundwater_continuity_error_percent": groundwater_error,
            "routing_continuity_error_percent": routing_error,
            "warning_count": 2,
            "percent_not_converging": 0.01,
            "max_abs_node_continuity_error_percent": 51.2,
            "model_disclaimer_present": True,
        },
    }


def _payload() -> dict:
    structures = {
        "BC": (142_179.557, 24_505.018, 82.7647),
        "BI": (209_184.892, 43_493.907, 79.2079),
        "CB": (183_032.100, 69_956.694, 61.7790),
        "ET": (121_129.998, 21_990.842, 81.8453),
        "GW": (124_567.889, 5_869.214, 95.2883),
    }
    cases = []
    for structure, (baseline, intervention, reduction) in structures.items():
        cases.append(_case(structure, "sq", baseline, None))
        groundwater = -54.273 if structure == "CB" else -1.0
        routing = -2.107 if structure == "CB" else -0.2
        cases.append(
            _case(
                structure,
                "bb",
                intervention,
                reduction,
                groundwater_error=groundwater,
                routing_error=routing,
            )
        )
    return {
        "title": "Fehraltorf realistic case study",
        "study": {
            "location": "Fehraltorf, Switzerland",
            "dataset_doi": "10.25678/0009PE",
            "license": "CC0-1.0",
        },
        "cases": cases,
        "comparison_claims": [
            {
                "claim_id": "fehraltorf.bioretention.model_structure_spread",
                "gi_scenario_id": "bb",
                "min_reduction_percent": 61.7790,
                "max_reduction_percent": 95.2883,
                "spread_percentage_points": 33.5093,
                "min_structure_id": "CB",
                "max_structure_id": "GW",
                "limitation": "Model-structure range, not a confidence interval.",
            }
        ],
    }


def test_builds_deterministic_accessible_report_media(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "case-study.json"
    source.write_text(json.dumps(_payload(), indent=2), encoding="utf-8")

    first = tmp_path / "first"
    second = tmp_path / "second"
    receipt = module.build_media(REPO_ROOT, source, first)
    module.build_media(REPO_ROOT, source, second)

    png = first / "fehraltorf-model-structure-sensitivity.png"
    table_csv = first / "fehraltorf-model-structure-sensitivity.csv"
    table_json = first / "fehraltorf-model-structure-sensitivity.json"
    assert png.is_file() and table_csv.is_file() and table_json.is_file()
    assert _sha256(png) == _sha256(second / png.name)
    assert _sha256(table_csv) == _sha256(second / table_csv.name)
    assert _sha256(table_json) == _sha256(second / table_json.name)

    with Image.open(png) as image:
        assert image.size == (1920, 1080)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (245, 247, 246)

    with table_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["model_structure_id"] for row in rows] == ["BC", "BI", "CB", "ET", "GW"]
    assert rows[2]["reduction_from_same_structure_baseline_percent"] == "61.779"
    assert rows[2]["groundwater_continuity_error_percent"] == "-54.273"

    persisted = json.loads(table_json.read_text(encoding="utf-8"))
    assert persisted["comparison"]["spread_percentage_points"] == pytest.approx(33.5093)
    assert persisted["outputs"]["png"]["sha256"] == _sha256(png)
    assert persisted["outputs"]["csv"]["sha256"] == _sha256(table_csv)
    assert "not rerun or field validated" in persisted["alt_text"]
    assert "statistical uncertainty interval" in " ".join(
        persisted["claim_boundary"]["does_not_establish"]
    )
    assert not Path(persisted["source"]["case_study_path"]).is_absolute()
    assert receipt["receipt_sha256"] == _sha256(table_json)


def test_rejects_comparison_claim_that_does_not_match_cases() -> None:
    module = _load_module()
    payload = _payload()
    payload["comparison_claims"][0]["spread_percentage_points"] = 5.0

    with pytest.raises(module.CaseStudyValidationError, match="does not match case rows"):
        module.extract_chart_evidence(payload)


def test_rejects_missing_combined_case_qa_boundary() -> None:
    module = _load_module()
    payload = deepcopy(_payload())
    combined = next(
        case
        for case in payload["cases"]
        if case["model_structure_id"] == "CB" and case["gi_scenario_id"] == "bb"
    )
    combined["qa"]["groundwater_continuity_error_percent"] = None

    with pytest.raises(module.CaseStudyValidationError, match="groundwater continuity"):
        module.extract_chart_evidence(payload)


def test_real_pinned_case_study_remains_renderable_when_present(tmp_path: Path) -> None:
    module = _load_module()
    source = REPO_ROOT / "benchmarks" / "realistic_fehraltorf" / "v1" / "case-study.json"
    if not source.is_file():
        pytest.skip("Pinned realistic case-study evidence has not been generated yet")

    receipt = module.build_media(REPO_ROOT, source, tmp_path)

    assert receipt["comparison"]["spread_percentage_points"] == pytest.approx(35.4234)
    assert receipt["comparison"]["min_structure_id"] == "CB"
    assert receipt["comparison"]["max_structure_id"] == "GW"
