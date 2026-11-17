#!/usr/bin/env python3
"""Build the pinned Fehraltorf published-output reanalysis.

This script deliberately does not run SWMM. It verifies the public Eawag
archives, recomputes annual combined-sewer-overflow summaries from the
published CSV output projections, and extracts QA facts from the retained SWMM
reports. The resulting JSON contains no local paths, credentials, or runtime
metadata.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import math
import re
import statistics
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

BUILDER_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
CASE_ID = "eawag-fehraltorf-published-output-reanalysis-v1"

INPUT_ARCHIVE_URL = (
    "https://opendata.eawag.ch/dataset/58c87cc9-00d2-4ad3-87a6-190e177f4900/"
    "resource/851af9e8-c4cb-4d1a-99a7-5c03592bd193/download/swmm_input_files.zip"
)
INPUT_ARCHIVE_SHA256 = "de7fe255ee0f69cc0ea426d82edb633599b620591062c58830525d8d68cd566f"
RESULTS_ARCHIVE_URL = (
    "https://opendata.eawag.ch/dataset/58c87cc9-00d2-4ad3-87a6-190e177f4900/"
    "resource/a4bafc54-2870-4510-843b-9a6f13e5a1b5/download/results.zip"
)
RESULTS_ARCHIVE_SHA256 = "b0cb5d50ade421cc416cfa857f5017827db2b1e4a159345657a025e58f0ca31a"
README_URL = (
    "https://opendata.eawag.ch/dataset/58c87cc9-00d2-4ad3-87a6-190e177f4900/"
    "resource/95073d24-6f11-4d8f-8daa-85c4506b43a7/download/readme.md"
)
README_SHA256 = "36fe3b580c3a3a26728da41b83e0e74828f414b9336332a99b00f3a009954bdb"

DATASET_DOI = "https://doi.org/10.25678/0009PE"
PAPER_DOI = "https://doi.org/10.1016/j.watres.2024.121284"
CSV_ROOT = "results/SWMM_simulation_results"

YEAR_START = 1990
YEAR_END = 2019
EXPECTED_YEAR_COUNT = 30
CSO_THRESHOLD_LPS = 0.003
SECONDS_PER_365_DAY_YEAR = 365 * 24 * 60 * 60

MODEL_STRUCTURES: tuple[dict[str, Any], ...] = (
    {"id": "BC", "label": "Basecase", "included_processes": ["basecase assumptions"]},
    {
        "id": "BI",
        "label": "Boundary inflow",
        "included_processes": ["basecase assumptions", "boundary inflow"],
    },
    {
        "id": "CB",
        "label": "Combined",
        "included_processes": [
            "basecase assumptions",
            "boundary inflow",
            "evapotranspiration",
            "groundwater",
        ],
    },
    {
        "id": "ET",
        "label": "Evapotranspiration",
        "included_processes": ["basecase assumptions", "evapotranspiration"],
    },
    {
        "id": "GW",
        "label": "Groundwater",
        "included_processes": ["basecase assumptions", "groundwater"],
    },
)

GI_SCENARIOS: tuple[dict[str, str], ...] = (
    {"id": "sq", "label": "No green infrastructure"},
    {"id": "bb", "label": "Bioretention basin"},
    {"id": "pp", "label": "Porous pavement"},
    {"id": "gr", "label": "Green roof"},
)

REPORT_PREFIX = {"BC": "1", "ET": "2", "BI": "3", "GW": "4", "CB": "5"}
REPORT_STRUCTURE_NAME = {
    "BC": "basecase",
    "ET": "et",
    "BI": "inflow",
    "GW": "GW",
    "CB": "comb",
}
REPORT_SCENARIO = {
    "sq": ("a", "sq"),
    "bb": ("b", "bc"),
    "pp": ("c", "pp"),
    "gr": ("d", "gr"),
}


class CaseStudyBuildError(RuntimeError):
    """Raised when a source or derived result cannot satisfy the pinned contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str, source_id: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise CaseStudyBuildError(
            f"{source_id} SHA-256 mismatch: expected {expected}, received {actual}"
        )


def _hashed_text_lines(handle: BinaryIO, digest: Any) -> Iterator[str]:
    for line in handle:
        digest.update(line)
        yield line.decode("utf-8-sig")


def _expected_observation_count(year: int) -> int:
    return (366 if calendar.isleap(year) else 365) * 48


def _annual_volume_m3(system_lps_values: Iterable[float]) -> float:
    thresholded = [
        value if value >= CSO_THRESHOLD_LPS else 0.0 for value in system_lps_values
    ]
    if not thresholded:
        raise CaseStudyBuildError("cannot calculate annual CSO volume from an empty year")
    mean_lps = math.fsum(thresholded) / len(thresholded)
    return mean_lps * SECONDS_PER_365_DAY_YEAR / 1000.0


def _paired_annual_reduction_percent(
    baseline_annual_m3: list[float], gi_annual_m3: list[float]
) -> float:
    if len(baseline_annual_m3) != len(gi_annual_m3):
        raise CaseStudyBuildError("baseline and GI annual records have different lengths")
    baseline_median = statistics.median(baseline_annual_m3)
    if baseline_median <= 0:
        raise CaseStudyBuildError("no-GI median annual volume must be positive")
    paired_differences = [
        baseline - gi
        for baseline, gi in zip(baseline_annual_m3, gi_annual_m3, strict=True)
    ]
    return statistics.median(paired_differences) / baseline_median * 100.0


def _annual_cso_volumes_from_rows(rows: Iterable[dict[str, str]]) -> tuple[list[float], int]:
    sums: dict[int, list[float]] = defaultdict(list)
    counts: dict[int, int] = defaultdict(int)

    for row_number, row in enumerate(rows, start=2):
        try:
            year = int(row["Date"][:4])
            system_lps = float(row["System"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaseStudyBuildError(f"invalid CSO CSV row {row_number}") from exc
        if not math.isfinite(system_lps):
            raise CaseStudyBuildError(f"non-finite System value in CSO CSV row {row_number}")
        if year < YEAR_START or year > YEAR_END:
            raise CaseStudyBuildError(f"unexpected year {year} in CSO CSV row {row_number}")
        counts[year] += 1
        sums[year].append(system_lps if system_lps >= CSO_THRESHOLD_LPS else 0.0)

    expected_years = list(range(YEAR_START, YEAR_END + 1))
    if sorted(counts) != expected_years:
        raise CaseStudyBuildError(
            f"expected complete {YEAR_START}-{YEAR_END} record, received years {sorted(counts)}"
        )

    annual_volumes: list[float] = []
    for year in expected_years:
        expected_count = _expected_observation_count(year)
        if counts[year] != expected_count:
            raise CaseStudyBuildError(
                f"year {year} has {counts[year]} half-hour observations; expected {expected_count}"
            )
        annual_volumes.append(_annual_volume_m3(sums[year]))
    return annual_volumes, sum(counts.values())


def _read_csv_case(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    try:
        handle = archive.open(member)
    except KeyError as exc:
        raise CaseStudyBuildError(f"results archive is missing {member}") from exc
    with handle:
        reader = csv.DictReader(_hashed_text_lines(handle, digest))
        if reader.fieldnames is None or not {"Date", "System"}.issubset(reader.fieldnames):
            raise CaseStudyBuildError(f"{member} lacks Date and System columns")
        annual_volumes, observation_count = _annual_cso_volumes_from_rows(reader)
    return {
        "annual_volumes": annual_volumes,
        "median_annual_cso_m3": statistics.median(annual_volumes),
        "observation_count": observation_count,
        "sha256": digest.hexdigest(),
    }


def _section_continuity_error(report: str, heading: str, *, required: bool) -> float | None:
    match = re.search(
        rf"{re.escape(heading)}.*?Continuity Error \(%\)\s+\.{{2,}}\s+([-+]?\d+(?:\.\d+)?)",
        report,
        flags=re.DOTALL,
    )
    if match:
        return float(match.group(1))
    if required:
        raise CaseStudyBuildError(f"SWMM report lacks {heading} continuity error")
    return None


def _parse_report(report: str) -> dict[str, Any]:
    version_match = re.search(
        r"STORM WATER MANAGEMENT MODEL - VERSION\s+([^\r\n]+)", report
    )
    if not version_match:
        raise CaseStudyBuildError("SWMM report lacks an engine version")
    not_converging = re.search(
        r"Percent Not Converging\s*:\s*([-+]?\d+(?:\.\d+)?)", report
    )
    if not not_converging:
        raise CaseStudyBuildError("SWMM report lacks Percent Not Converging")

    highest_block = re.search(
        r"Highest Continuity Errors\s*\*+\s*(.*?)(?:\*+\s*Time-Step Critical Elements)",
        report,
        flags=re.DOTALL,
    )
    if not highest_block:
        raise CaseStudyBuildError("SWMM report lacks Highest Continuity Errors")
    node_errors = [
        abs(float(value))
        for value in re.findall(r"^\s*Node\s+.+?\(([-+]?\d+(?:\.\d+)?)%\)\s*$", highest_block.group(1), re.MULTILINE)
    ]
    if not node_errors:
        raise CaseStudyBuildError("SWMM report lacks reported node continuity errors")

    return {
        "swmm_version": version_match.group(1).strip(),
        "runoff_continuity_error_percent": _section_continuity_error(
            report, "Runoff Quantity Continuity", required=True
        ),
        "groundwater_continuity_error_percent": _section_continuity_error(
            report, "Groundwater Continuity", required=False
        ),
        "routing_continuity_error_percent": _section_continuity_error(
            report, "Flow Routing Continuity", required=True
        ),
        "warning_count": len(re.findall(r"^\s*WARNING\s+\d+:\s", report, re.MULTILINE)),
        "percent_not_converging": float(not_converging.group(1)),
        "max_abs_node_continuity_error_percent": max(node_errors),
        "model_disclaimer_present": (
            "model is not complete and may still contain errors" in report.lower()
        ),
    }


def _read_report_case(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    try:
        report_bytes = archive.read(member)
    except KeyError as exc:
        raise CaseStudyBuildError(f"results archive is missing {member}") from exc
    report = report_bytes.decode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(report_bytes).hexdigest(), "qa": _parse_report(report)}


def _csv_member(model_structure_id: str, gi_scenario_id: str) -> str:
    return f"{CSV_ROOT}/results_{gi_scenario_id}_{model_structure_id}.csv"


def _report_member(model_structure_id: str, gi_scenario_id: str) -> str:
    letter, report_scenario = REPORT_SCENARIO[gi_scenario_id]
    prefix = REPORT_PREFIX[model_structure_id]
    structure = REPORT_STRUCTURE_NAME[model_structure_id]
    return f"{CSV_ROOT}/{prefix}{letter}_cal_{structure}_{report_scenario}.rpt"


def _assert_input_archive_shape(archive: zipfile.ZipFile) -> None:
    input_members = [name for name in archive.namelist() if name.lower().endswith(".inp")]
    if len(input_members) != 20:
        raise CaseStudyBuildError(
            f"source input archive contains {len(input_members)} INP files; expected 20"
        )
    weather_notes = [name for name in archive.namelist() if name.endswith("readme_download_weather_data.md")]
    if len(weather_notes) != 1:
        raise CaseStudyBuildError("source input archive lacks its weather-data acquisition notice")


def _round(value: float, digits: int) -> float:
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def build_case_study(input_archive: Path, results_archive: Path, readme: Path) -> dict[str, Any]:
    _verify_sha256(input_archive, INPUT_ARCHIVE_SHA256, "SWMM input archive")
    _verify_sha256(results_archive, RESULTS_ARCHIVE_SHA256, "results archive")
    _verify_sha256(readme, README_SHA256, "dataset README")

    with zipfile.ZipFile(input_archive) as source_zip:
        _assert_input_archive_shape(source_zip)

    cases: list[dict[str, Any]] = []
    baseline_by_structure: dict[str, list[float]] = {}
    with zipfile.ZipFile(results_archive) as results_zip:
        for structure in MODEL_STRUCTURES:
            structure_id = structure["id"]
            for scenario in GI_SCENARIOS:
                scenario_id = scenario["id"]
                csv_member = _csv_member(structure_id, scenario_id)
                report_member = _report_member(structure_id, scenario_id)
                csv_result = _read_csv_case(results_zip, csv_member)
                report_result = _read_report_case(results_zip, report_member)
                median = csv_result["median_annual_cso_m3"]
                if scenario_id == "sq":
                    baseline_by_structure[structure_id] = csv_result["annual_volumes"]
                    reduction = None
                else:
                    baseline = baseline_by_structure.get(structure_id)
                    if baseline is None or statistics.median(baseline) <= 0:
                        raise CaseStudyBuildError(
                            f"missing positive no-GI baseline for structure {structure_id}"
                        )
                    reduction = _paired_annual_reduction_percent(
                        baseline, csv_result["annual_volumes"]
                    )
                cases.append(
                    {
                        "case_id": f"{structure_id}-{scenario_id}",
                        "model_structure_id": structure_id,
                        "gi_scenario_id": scenario_id,
                        "csv_source": {"member": csv_member, "sha256": csv_result["sha256"]},
                        "report_source": {
                            "member": report_member,
                            "sha256": report_result["sha256"],
                        },
                        "year_count": EXPECTED_YEAR_COUNT,
                        "median_annual_cso_m3": _round(median, 3),
                        "reduction_from_same_structure_baseline_percent": (
                            None if reduction is None else _round(reduction, 4)
                        ),
                        "qa": report_result["qa"],
                    }
                )

    comparison_claims: list[dict[str, Any]] = []
    for scenario in GI_SCENARIOS:
        scenario_id = scenario["id"]
        if scenario_id == "sq":
            continue
        reductions = {
            case["model_structure_id"]: case["reduction_from_same_structure_baseline_percent"]
            for case in cases
            if case["gi_scenario_id"] == scenario_id
        }
        min_structure = min(reductions, key=reductions.get)  # type: ignore[arg-type]
        max_structure = max(reductions, key=reductions.get)  # type: ignore[arg-type]
        minimum = float(reductions[min_structure])
        maximum = float(reductions[max_structure])
        comparison_claims.append(
            {
                "claim_id": f"fehraltorf-{scenario_id}-model-structure-sensitivity",
                "gi_scenario_id": scenario_id,
                "min_reduction_percent": minimum,
                "max_reduction_percent": maximum,
                "spread_percentage_points": _round(maximum - minimum, 4),
                "min_structure_id": min_structure,
                "max_structure_id": max_structure,
                "limitation": (
                    "Published modeled CSO response varies with model structure; this comparison "
                    "does not establish field performance or resolve the reports' QA findings."
                ),
            }
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "case_id": CASE_ID,
        "title": "Fehraltorf model-structure sensitivity and green-infrastructure screening",
        "study": {
            "location": "Fehraltorf, Switzerland",
            "paper_doi": PAPER_DOI,
            "dataset_doi": DATASET_DOI,
            "license": (
                "CC0-1.0 in the dataset metadata; included software files carry an MIT-style "
                "permission notice"
            ),
            "source_model_disclaimer": (
                "The published reports state that the model is incomplete and may contain errors."
            ),
        },
        "analysis_boundary": {
            "analysis_type": "independent reanalysis of published SWMM output summaries",
            "fresh_swmm_rerun": False,
            "reason_rerun_not_possible": (
                "The public package omits complete SWMM OUT files and requires separately "
                "licensed MeteoSwiss rainfall and evapotranspiration series for a fresh rerun."
            ),
            "authority": "published model output, not measured catchment response",
            "not_established": [
                "fresh SWMM reproduction",
                "field performance of any green-infrastructure scenario",
                "regulatory suitability",
                "construction feasibility",
                "water-quality or ecological benefit",
            ],
        },
        "sources": [
            {
                "source_id": "swmm-input-archive",
                "url": INPUT_ARCHIVE_URL,
                "sha256": INPUT_ARCHIVE_SHA256,
                "role": "model-input provenance and rerun-boundary verification",
            },
            {
                "source_id": "published-results-archive",
                "url": RESULTS_ARCHIVE_URL,
                "sha256": RESULTS_ARCHIVE_SHA256,
                "role": "published CSO projections and retained SWMM reports",
            },
            {
                "source_id": "dataset-readme",
                "url": README_URL,
                "sha256": README_SHA256,
                "role": "study scope, file definitions, license notice, and data limitations",
            },
        ],
        "method": {
            "outlet_metric": "published System combined-sewer-overflow flow",
            "threshold_lps": CSO_THRESHOLD_LPS,
            "year_range": {
                "start": YEAR_START,
                "end": YEAR_END,
                "count": EXPECTED_YEAR_COUNT,
            },
            "annual_conversion": (
                "For each calendar year, values below 0.003 L/s are set to zero; the mean L/s "
                "is multiplied by 31,536,000 seconds and divided by 1,000 to obtain m3/year. "
                "This is equivalent to the authors' hourly-then-annual mean for the complete "
                "half-hour records in the verified archive."
            ),
            "summary_statistic": "median of 30 annual CSO volumes",
            "baseline_comparison": (
                "For each structure and GI scenario, reduction is median(baseline annual volume "
                "minus GI annual volume) divided by median(baseline annual volume), multiplied "
                "by 100. Annual values are paired by calendar year; the baseline always comes "
                "from the same model structure."
            ),
        },
        "model_structures": list(MODEL_STRUCTURES),
        "gi_scenarios": list(GI_SCENARIOS),
        "cases": cases,
        "comparison_claims": comparison_claims,
        "generation": {"builder_version": BUILDER_VERSION, "deterministic": True},
    }
    if len(cases) != 20:
        raise CaseStudyBuildError(f"expected 20 model/scenario cases, received {len(cases)}")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-archive", type=Path, required=True)
    parser.add_argument("--results-archive", type=Path, required=True)
    parser.add_argument("--readme", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/realistic_fehraltorf/v1/case-study.json"),
    )
    args = parser.parse_args()

    payload = build_case_study(args.input_archive, args.results_archive, args.readme)
    _write_json_atomic(args.output, payload)
    print(
        json.dumps(
            {
                "case_id": payload["case_id"],
                "case_count": len(payload["cases"]),
                "fresh_swmm_rerun": payload["analysis_boundary"]["fresh_swmm_rerun"],
                "output_sha256": _sha256(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
