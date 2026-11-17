#!/usr/bin/env python3
"""Render the Fehraltorf realistic-case figure and accessible data fallbacks.

The figure is intentionally a dot plot rather than a truncated bar chart. It
shows one bounded observation: the modeled median annual CSO reduction for the
same bioretention scenario changes across five published SWMM process
structures. The adjacent QA card keeps the most consequential report finding
visible at the point of interpretation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
SAFE_MARGIN = 96

PAPER = "#F5F7F6"
INK = "#17313A"
INK_SOFT = "#425A62"
TEAL = "#08766F"
TEAL_WASH = "#DCEDEA"
BLUE = "#2F6F9F"
BLUE_WASH = "#E2ECF3"
OCHRE = "#A96315"
OCHRE_WASH = "#F4E8D9"
RED = "#B43D47"
RED_WASH = "#F4E0E2"
LINE = "#C8D2D0"
WHITE = "#FFFFFF"

MODEL_ORDER = ("BC", "BI", "CB", "ET", "GW")
MODEL_LABELS = {
    "BC": "Base case",
    "BI": "Boundary inflow",
    "CB": "Combined processes",
    "ET": "Evapotranspiration",
    "GW": "Groundwater",
}
TARGET_SCENARIO = "bb"
OUTPUT_STEM = "fehraltorf-model-structure-sensitivity"


class CaseStudyValidationError(ValueError):
    """Raised when the pinned case study cannot support the authored chart."""


@dataclass(frozen=True, slots=True)
class ChartRow:
    model_structure_id: str
    model_structure_label: str
    baseline_median_annual_cso_m3: float
    bioretention_median_annual_cso_m3: float
    reduction_percent: float
    year_count: int
    swmm_version: str
    runoff_continuity_error_percent: float
    groundwater_continuity_error_percent: float | None
    routing_continuity_error_percent: float
    warning_count: int
    percent_not_converging: float
    max_abs_node_continuity_error_percent: float
    model_disclaimer_present: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_structure_id": self.model_structure_id,
            "model_structure_label": self.model_structure_label,
            "scenario": "bioretention",
            "year_count": self.year_count,
            "baseline_median_annual_cso_m3": self.baseline_median_annual_cso_m3,
            "bioretention_median_annual_cso_m3": self.bioretention_median_annual_cso_m3,
            "reduction_from_same_structure_baseline_percent": self.reduction_percent,
            "swmm_version": self.swmm_version,
            "runoff_continuity_error_percent": self.runoff_continuity_error_percent,
            "groundwater_continuity_error_percent": self.groundwater_continuity_error_percent,
            "routing_continuity_error_percent": self.routing_continuity_error_percent,
            "warning_count": self.warning_count,
            "percent_not_converging": self.percent_not_converging,
            "max_abs_node_continuity_error_percent": self.max_abs_node_continuity_error_percent,
            "model_disclaimer_present": self.model_disclaimer_present,
        }


@dataclass(frozen=True, slots=True)
class ChartEvidence:
    rows: tuple[ChartRow, ...]
    spread_percentage_points: float
    min_structure_id: str
    max_structure_id: str
    limitation: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise CaseStudyValidationError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise CaseStudyValidationError(f"{field} must be numeric") from error
    if not math.isfinite(parsed):
        raise CaseStudyValidationError(f"{field} must be finite")
    return parsed


def _optional_float(value: Any, *, field: str) -> float | None:
    return None if value is None else _float(value, field=field)


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseStudyValidationError(f"Could not read case study: {path}") from error
    if not isinstance(payload, dict):
        raise CaseStudyValidationError("Case-study payload must be a JSON object")
    return payload


def extract_chart_evidence(payload: dict[str, Any]) -> ChartEvidence:
    """Validate and project the five bioretention comparisons used on screen."""

    raw_cases = payload.get("cases")
    raw_claims = payload.get("comparison_claims")
    if not isinstance(raw_cases, list) or not isinstance(raw_claims, list):
        raise CaseStudyValidationError("case-study.json must contain cases and comparison_claims")

    cases: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise CaseStudyValidationError(f"cases[{index}] must be an object")
        structure = raw.get("model_structure_id")
        scenario = raw.get("gi_scenario_id")
        if structure in MODEL_ORDER and isinstance(scenario, str):
            key = (structure, scenario)
            if key in cases:
                raise CaseStudyValidationError(f"Duplicate case for {structure}/{scenario}")
            cases[key] = raw

    rows: list[ChartRow] = []
    for structure in MODEL_ORDER:
        try:
            baseline = cases[(structure, "sq")]
            intervention = cases[(structure, TARGET_SCENARIO)]
        except KeyError as error:
            raise CaseStudyValidationError(
                f"Missing required status-quo or bioretention case for {structure}"
            ) from error
        reduction = intervention.get("reduction_from_same_structure_baseline_percent")
        if reduction is None:
            raise CaseStudyValidationError(f"Missing bioretention reduction for {structure}")
        qa = intervention.get("qa")
        if not isinstance(qa, dict):
            raise CaseStudyValidationError(f"Missing QA record for {structure}/bb")
        row = ChartRow(
            model_structure_id=structure,
            model_structure_label=MODEL_LABELS[structure],
            baseline_median_annual_cso_m3=_float(
                baseline.get("median_annual_cso_m3"),
                field=f"{structure}/sq median_annual_cso_m3",
            ),
            bioretention_median_annual_cso_m3=_float(
                intervention.get("median_annual_cso_m3"),
                field=f"{structure}/bb median_annual_cso_m3",
            ),
            reduction_percent=_float(reduction, field=f"{structure}/bb reduction"),
            year_count=int(intervention.get("year_count", 0)),
            swmm_version=str(qa.get("swmm_version", "unknown")),
            runoff_continuity_error_percent=_float(
                qa.get("runoff_continuity_error_percent"),
                field=f"{structure}/bb runoff continuity",
            ),
            groundwater_continuity_error_percent=_optional_float(
                qa.get("groundwater_continuity_error_percent"),
                field=f"{structure}/bb groundwater continuity",
            ),
            routing_continuity_error_percent=_float(
                qa.get("routing_continuity_error_percent"),
                field=f"{structure}/bb routing continuity",
            ),
            warning_count=int(qa.get("warning_count", 0)),
            percent_not_converging=_float(
                qa.get("percent_not_converging", 0.0),
                field=f"{structure}/bb percent not converging",
            ),
            max_abs_node_continuity_error_percent=_float(
                qa.get("max_abs_node_continuity_error_percent", 0.0),
                field=f"{structure}/bb max node continuity",
            ),
            model_disclaimer_present=bool(qa.get("model_disclaimer_present", False)),
        )
        if row.year_count <= 0:
            raise CaseStudyValidationError(f"Invalid year_count for {structure}/bb")
        if not 0.0 <= row.reduction_percent <= 100.0:
            raise CaseStudyValidationError(f"Reduction outside 0–100% for {structure}/bb")
        rows.append(row)

    claim = next(
        (
            raw
            for raw in raw_claims
            if isinstance(raw, dict) and raw.get("gi_scenario_id") == TARGET_SCENARIO
        ),
        None,
    )
    if claim is None:
        raise CaseStudyValidationError("Missing bioretention comparison claim")
    calculated_min = min(row.reduction_percent for row in rows)
    calculated_max = max(row.reduction_percent for row in rows)
    calculated_spread = calculated_max - calculated_min
    claimed_min = _float(claim.get("min_reduction_percent"), field="claimed minimum")
    claimed_max = _float(claim.get("max_reduction_percent"), field="claimed maximum")
    claimed_spread = _float(
        claim.get("spread_percentage_points"), field="claimed percentage-point spread"
    )
    if not (
        math.isclose(calculated_min, claimed_min, abs_tol=1e-8)
        and math.isclose(calculated_max, claimed_max, abs_tol=1e-8)
        and math.isclose(calculated_spread, claimed_spread, abs_tol=1e-8)
    ):
        raise CaseStudyValidationError("Bioretention comparison claim does not match case rows")
    min_row = min(rows, key=lambda row: row.reduction_percent)
    max_row = max(rows, key=lambda row: row.reduction_percent)
    if claim.get("min_structure_id") != min_row.model_structure_id:
        raise CaseStudyValidationError("Claimed minimum model structure does not match cases")
    if claim.get("max_structure_id") != max_row.model_structure_id:
        raise CaseStudyValidationError("Claimed maximum model structure does not match cases")

    combined = next(row for row in rows if row.model_structure_id == "CB")
    if combined.groundwater_continuity_error_percent is None:
        raise CaseStudyValidationError(
            "Combined-process bioretention case needs a groundwater continuity result"
        )
    if not combined.model_disclaimer_present:
        raise CaseStudyValidationError("Published model disclaimer was not detected")

    return ChartEvidence(
        rows=tuple(rows),
        spread_percentage_points=claimed_spread,
        min_structure_id=min_row.model_structure_id,
        max_structure_id=max_row.model_structure_id,
        limitation=str(claim.get("limitation", "")),
    )


def _font_path(repo_root: Path, filename: str) -> Path:
    path = repo_root / "media" / "gi-scenario-film" / "assets" / "fonts" / filename
    if not path.is_file():
        raise CaseStudyValidationError(f"Required local font is missing: {path.name}")
    return path


def _fonts(repo_root: Path) -> dict[str, ImageFont.FreeTypeFont]:
    heading = _font_path(repo_root, "IBMPlexSansCondensed-SemiBold.ttf")
    body = _font_path(repo_root, "AtkinsonHyperlegible-Regular.ttf")
    bold = _font_path(repo_root, "AtkinsonHyperlegible-Bold.ttf")
    mono = _font_path(repo_root, "IBMPlexMono-Medium.ttf")
    return {
        "eyebrow": ImageFont.truetype(str(bold), 25),
        "title": ImageFont.truetype(str(heading), 58),
        "subtitle": ImageFont.truetype(str(body), 29),
        "axis": ImageFont.truetype(str(body), 22),
        "label": ImageFont.truetype(str(bold), 25),
        "value": ImageFont.truetype(str(mono), 25),
        "card_heading": ImageFont.truetype(str(heading), 36),
        "card_big": ImageFont.truetype(str(mono), 55),
        "card_body": ImageFont.truetype(str(body), 25),
        "card_bold": ImageFont.truetype(str(bold), 25),
        "footer": ImageFont.truetype(str(body), 20),
        "footer_mono": ImageFont.truetype(str(mono), 19),
    }


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return "\n".join(lines)


def _assert_box(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    box: tuple[int, int, int, int],
    *,
    spacing: int = 6,
) -> None:
    left, top, right, bottom = draw.multiline_textbbox(xy, text, font=font, spacing=spacing)
    if left < box[0] or top < box[1] or right > box[2] or bottom > box[3]:
        raise CaseStudyValidationError(
            f"Text leaves authored box {box}: rendered bounds {(left, top, right, bottom)}"
        )


def _draw_checked_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
    box: tuple[int, int, int, int],
    spacing: int = 6,
) -> None:
    _assert_box(draw, xy, text, font, box, spacing=spacing)
    draw.multiline_text(xy, text, font=font, fill=fill, spacing=spacing)


def _render_png(repo_root: Path, evidence: ChartEvidence, output: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    fonts = _fonts(repo_root)

    _draw_checked_text(
        draw,
        (SAFE_MARGIN, 52),
        "REALISTIC PUBLISHED CASE · FEHRALTORF, SWITZERLAND",
        font=fonts["eyebrow"],
        fill=TEAL,
        box=(SAFE_MARGIN, 45, 1200, 92),
    )
    _draw_checked_text(
        draw,
        (SAFE_MARGIN, 94),
        "One intervention. Five modeled answers.",
        font=fonts["title"],
        fill=INK,
        box=(SAFE_MARGIN, 88, 1824, 164),
    )
    subtitle = (
        "Bioretention’s modeled median annual CSO reduction changes when the SWMM "
        "process structure changes."
    )
    _draw_checked_text(
        draw,
        (SAFE_MARGIN, 176),
        subtitle,
        font=fonts["subtitle"],
        fill=INK_SOFT,
        box=(SAFE_MARGIN, 170, 1824, 222),
    )

    chart_box = (SAFE_MARGIN, 252, 1328, 918)
    card_box = (1372, 252, 1824, 918)
    draw.rounded_rectangle(chart_box, radius=24, fill=WHITE, outline=LINE, width=2)
    draw.rounded_rectangle(card_box, radius=24, fill=TEAL_WASH, outline=TEAL, width=2)

    plot_left, plot_right = 380, 1245
    axis_top, axis_bottom = 342, 825
    for tick in (0, 25, 50, 75, 100):
        x = round(plot_left + (plot_right - plot_left) * tick / 100)
        draw.line((x, axis_top, x, axis_bottom), fill=LINE, width=2)
        label = f"{tick}%"
        bbox = draw.textbbox((0, 0), label, font=fonts["axis"])
        draw.text((x - (bbox[2] - bbox[0]) / 2, 845), label, font=fonts["axis"], fill=INK_SOFT)

    row_y = {structure: 405 + index * 91 for index, structure in enumerate(MODEL_ORDER)}
    for row in evidence.rows:
        y = row_y[row.model_structure_id]
        draw.text((132, y - 16), row.model_structure_label, font=fonts["label"], fill=INK)
        x = round(plot_left + (plot_right - plot_left) * row.reduction_percent / 100)
        color = OCHRE if row.model_structure_id == "CB" else TEAL
        radius = 14 if row.model_structure_id == "CB" else 11
        draw.line((plot_left, y, x, y), fill=BLUE_WASH, width=5)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=INK, width=2)
        value = f"{row.reduction_percent:.1f}%"
        value_bbox = draw.textbbox((0, 0), value, font=fonts["value"])
        value_width = value_bbox[2] - value_bbox[0]
        value_x = x + 22 if x + 22 + value_width <= plot_right else x - value_width - 22
        draw.text((value_x, y - 15), value, font=fonts["value"], fill=INK)
        if row.model_structure_id == "CB":
            draw.rounded_rectangle((132, y + 23, 305, y + 56), radius=10, fill=OCHRE_WASH)
            draw.text((146, y + 27), "QA caution", font=fonts["axis"], fill=OCHRE)

    min_row = next(row for row in evidence.rows if row.model_structure_id == evidence.min_structure_id)
    max_row = next(row for row in evidence.rows if row.model_structure_id == evidence.max_structure_id)
    min_x = round(plot_left + (plot_right - plot_left) * min_row.reduction_percent / 100)
    max_x = round(plot_left + (plot_right - plot_left) * max_row.reduction_percent / 100)
    bracket_y = 310
    draw.line((min_x, bracket_y, max_x, bracket_y), fill=BLUE, width=4)
    draw.line((min_x, bracket_y - 8, min_x, bracket_y + 8), fill=BLUE, width=4)
    draw.line((max_x, bracket_y - 8, max_x, bracket_y + 8), fill=BLUE, width=4)
    spread_label = f"{evidence.spread_percentage_points:.1f} percentage-point range"
    label_bbox = draw.textbbox((0, 0), spread_label, font=fonts["axis"])
    label_width = label_bbox[2] - label_bbox[0]
    draw.rounded_rectangle(
        (
            (min_x + max_x - label_width) / 2 - 12,
            273,
            (min_x + max_x + label_width) / 2 + 12,
            306,
        ),
        radius=10,
        fill=BLUE_WASH,
    )
    draw.text(
        ((min_x + max_x - label_width) / 2, 278),
        spread_label,
        font=fonts["axis"],
        fill=BLUE,
    )
    draw.text(
        (380, 878),
        "Reduction from each structure’s own no-GI baseline · full 0–100% scale",
        font=fonts["axis"],
        fill=INK_SOFT,
    )

    combined = next(row for row in evidence.rows if row.model_structure_id == "CB")
    _draw_checked_text(
        draw,
        (1410, 292),
        "The honest reading",
        font=fonts["card_heading"],
        fill=INK,
        box=(1400, 280, 1792, 348),
    )
    _draw_checked_text(
        draw,
        (1410, 350),
        f"{evidence.spread_percentage_points:.1f} pp",
        font=fonts["card_big"],
        fill=TEAL,
        box=(1400, 342, 1792, 426),
    )
    _draw_checked_text(
        draw,
        (1410, 427),
        "range across five model structures\n—not an uncertainty interval",
        font=fonts["card_body"],
        fill=INK,
        box=(1400, 418, 1792, 492),
        spacing=7,
    )
    draw.line((1410, 516, 1786, 516), fill=TEAL, width=2)
    draw.rounded_rectangle((1410, 547, 1786, 752), radius=18, fill=OCHRE_WASH, outline=OCHRE, width=2)
    _draw_checked_text(
        draw,
        (1434, 568),
        "QA EXAMPLE · COMBINED",
        font=fonts["eyebrow"],
        fill=OCHRE,
        box=(1428, 560, 1770, 606),
    )
    groundwater = combined.groundwater_continuity_error_percent
    qa_copy = (
        f"Groundwater continuity {groundwater:+.1f}%\n"
        f"Routing continuity {combined.routing_continuity_error_percent:+.1f}%"
    )
    _draw_checked_text(
        draw,
        (1434, 621),
        qa_copy,
        font=fonts["footer_mono"],
        fill=INK,
        box=(1428, 612, 1770, 676),
        spacing=8,
    )
    _draw_checked_text(
        draw,
        (1434, 684),
        "Surface the QA before carrying\nthe percentage forward.",
        font=fonts["card_body"],
        fill=INK,
        box=(1428, 677, 1780, 746),
        spacing=6,
    )
    boundary = _wrap(
        draw,
        "Independent reanalysis of published outputs. No fresh SWMM rerun; no field validation.",
        fonts["card_body"],
        345,
    )
    _draw_checked_text(
        draw,
        (1410, 768),
        boundary,
        font=fonts["card_body"],
        fill=INK,
        box=(1400, 755, 1792, 898),
        spacing=7,
    )

    footer = (
        "Published 30-year SWMM outputs · modeled, not measured · not ecological or field evidence"
    )
    _draw_checked_text(
        draw,
        (SAFE_MARGIN, 958),
        footer,
        font=fonts["footer"],
        fill=INK_SOFT,
        box=(SAFE_MARGIN, 948, 1824, 994),
    )
    _draw_checked_text(
        draw,
        (SAFE_MARGIN, 1006),
        "Source: Fehraltorf open-data package · DOI 10.25678/0009PE",
        font=fonts["footer_mono"],
        fill=INK_SOFT,
        box=(SAFE_MARGIN, 998, 1824, 1042),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".png", delete=False) as handle:
        temporary = Path(handle.name)
    image.save(temporary, format="PNG", compress_level=9, optimize=False)
    temporary.replace(output)


def _write_csv(path: Path, rows: tuple[ChartRow, ...]) -> None:
    columns = list(rows[0].as_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, suffix=".csv", delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(row.as_dict() for row in rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".json", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def build_media(repo_root: Path, input_path: Path, output_dir: Path) -> dict[str, Any]:
    """Build deterministic report media and return its publication receipt."""

    payload = _load_payload(input_path)
    evidence = extract_chart_evidence(payload)
    png_path = output_dir / f"{OUTPUT_STEM}.png"
    csv_path = output_dir / f"{OUTPUT_STEM}.csv"
    json_path = output_dir / f"{OUTPUT_STEM}.json"

    _render_png(repo_root, evidence, png_path)
    _write_csv(csv_path, evidence.rows)
    receipt = {
        "schema_version": "1.0",
        "media_key": "fehraltorf_model_structure_sensitivity",
        "title": "One intervention. Five modeled answers.",
        "question": (
            "How much does the modeled bioretention result change when the SWMM process "
            "structure changes?"
        ),
        "takeaway": (
            f"Across five published model structures, the modeled median annual CSO "
            f"reduction spans {evidence.spread_percentage_points:.1f} percentage points; "
            "the combined-process report also carries a material continuity warning."
        ),
        "alt_text": (
            "Dot plot of bioretention's modeled median annual combined-sewer-overflow "
            f"reduction across five Fehraltorf SWMM structures. Values range from "
            f"{min(row.reduction_percent for row in evidence.rows):.1f}% for combined "
            f"processes to {max(row.reduction_percent for row in evidence.rows):.1f}% "
            f"for groundwater, a {evidence.spread_percentage_points:.1f} percentage-point "
            "range. A QA card reports the combined bioretention case's groundwater and "
            "routing continuity errors and states that the outputs were independently "
            "reanalyzed, not rerun or field validated."
        ),
        "source": {
            "case_study_path": "benchmarks/realistic_fehraltorf/v1/case-study.json",
            "case_study_sha256": _sha256(input_path),
            "dataset_doi": "10.25678/0009PE",
        },
        "claim_boundary": {
            "establishes": (
                "Published modeled CSO reductions are sensitive to represented process structure, "
                "and their report QA belongs beside the result."
            ),
            "does_not_establish": [
                "A fresh reproduction of the SWMM simulations",
                "Observed or field-validated GI performance",
                "Avoided flooding, ecological benefit, regulatory suitability, or design readiness",
                "A statistical uncertainty interval or causal attribution to one process",
            ],
            "limitation_from_source": evidence.limitation,
        },
        "comparison": {
            "scenario": "bioretention",
            "year_count": min(row.year_count for row in evidence.rows),
            "min_structure_id": evidence.min_structure_id,
            "max_structure_id": evidence.max_structure_id,
            "spread_percentage_points": evidence.spread_percentage_points,
        },
        "table": [row.as_dict() for row in evidence.rows],
        "outputs": {
            "png": {"path": png_path.name, "sha256": _sha256(png_path), "width": WIDTH, "height": HEIGHT},
            "csv": {"path": csv_path.name, "sha256": _sha256(csv_path), "rows": len(evidence.rows)},
        },
    }
    _write_json(json_path, receipt)
    return {**receipt, "receipt_path": json_path.name, "receipt_sha256": _sha256(json_path)}


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> None:
    repo_root = _default_repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=repo_root / "benchmarks" / "realistic_fehraltorf" / "v1" / "case-study.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root / "media" / "gi-scenario-film" / "dist" / "realistic-fehraltorf",
    )
    args = parser.parse_args()
    receipt = build_media(repo_root, args.input, args.output_dir)
    print(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
