#!/usr/bin/env python3
"""Build evidence-driven SVG figures and a source manifest for article v2."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path


HERE = Path(__file__).resolve()
ARTICLE_ROOT = HERE.parents[1]
REPO_ROOT = HERE.parents[3]
FIGURES = ARTICLE_ROOT / "figures"
DIST = ARTICLE_ROOT / "dist"

SUMMARY_PATH = REPO_ROOT / "media/gi-scenario-film/dist/evidence-guide-r4c/article-summary.json"
GUIDED_PATH = REPO_ROOT / "apps/desktop/src/data/guidedFixture.json"
ANALYSIS_PATH = REPO_ROOT / "dist/evidence-guide-r4c-grounding-final-r1/live/publication/analysis.json"
ATTEMPTS_PATH = REPO_ROOT / "dist/evidence-guide-r4c-grounding-final-r1/live/publication/attempts.json"

PAPER = "#F5F7F6"
INK = "#17313A"
TEAL = "#08766F"
BLUE = "#2F6F9F"
OCHRE = "#A96315"
RED = "#B43D47"
MUTED = "#48616B"
GRID = "#CBD8D8"
WHITE = "#FFFFFF"
PALE_TEAL = "#DCEFED"
PALE_BLUE = "#E4EFF6"
PALE_OCHRE = "#F6E9D8"
PALE_RED = "#F6E3E5"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def text(x: int, y: int, value: object, cls: str = "body", *, anchor: str = "start", fill: str | None = None) -> str:
    attr = f' style="fill:{fill}"' if fill else ""
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}"{attr}>{esc(value)}</text>'


def multiline(x: int, y: int, lines: list[str], cls: str = "body", *, gap: int = 40, anchor: str = "start", fill: str | None = None) -> str:
    attr = f' style="fill:{fill}"' if fill else ""
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else gap}">{esc(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}"{attr}>{spans}</text>'


def rect(x: int, y: int, width: int, height: int, *, fill: str = WHITE, stroke: str = GRID, radius: int = 24, stroke_width: int = 2, dash: str | None = None) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"{dash_attr}/>'
    )


def line(x1: int, y1: int, x2: int, y2: int, *, stroke: str = INK, width: int = 4, dash: str | None = None, marker: bool = False) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    marker_attr = ' marker-end="url(#arrow)"' if marker else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{width}" stroke-linecap="round"{dash_attr}{marker_attr}/>'


def circle(cx: int, cy: int, r: int, *, fill: str, stroke: str = "none", width: int = 0) -> str:
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'


def svg_document(title_value: str, description: str, content: str, *, width: int = 1200, height: int = 675) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{esc(title_value)}</title>
  <desc id="desc">{esc(description)}</desc>
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker>
    <style>
      @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Regular.ttf'); }}
      @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Bold.ttf'); font-weight: 700; }}
      @font-face {{ font-family: PlexCondensed; src: url('../../gi-scenario-film/assets/fonts/IBMPlexSansCondensed-SemiBold.ttf'); font-weight: 600; }}
      @font-face {{ font-family: PlexMono; src: url('../../gi-scenario-film/assets/fonts/IBMPlexMono-Regular.ttf'); }}
      .eyebrow {{ font: 700 24px Atkinson, sans-serif; letter-spacing: 1.2px; fill: {TEAL}; }}
      .headline {{ font: 600 48px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .subhead {{ font: 400 25px Atkinson, sans-serif; fill: {MUTED}; }}
      .section {{ font: 700 29px Atkinson, sans-serif; fill: {INK}; }}
      .body {{ font: 400 24px Atkinson, sans-serif; fill: {INK}; }}
      .small {{ font: 400 20px Atkinson, sans-serif; fill: {MUTED}; }}
      .label {{ font: 700 21px Atkinson, sans-serif; letter-spacing: .6px; fill: {INK}; }}
      .tinyLabel {{ font: 700 17px Atkinson, sans-serif; letter-spacing: .35px; fill: {INK}; }}
      .tableBody {{ font: 400 21px Atkinson, sans-serif; fill: {INK}; }}
      .mono {{ font: 400 24px PlexMono, monospace; fill: {INK}; }}
      .number {{ font: 600 64px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .numberSmall {{ font: 600 44px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .whiteLabel {{ font: 700 21px Atkinson, sans-serif; letter-spacing: .6px; fill: white; }}
      .whiteBody {{ font: 400 24px Atkinson, sans-serif; fill: white; }}
    </style>
  </defs>
  <rect width="1200" height="{height}" fill="{PAPER}"/>
  {content}
</svg>'''


def svg_mobile_document(title_value: str, description: str, content: str, *, height: int) -> str:
    """A separate narrow artboard; essential labels remain readable at phone width."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="540" height="{height}" viewBox="0 0 540 {height}" role="img" aria-labelledby="title desc">
  <title id="title">{esc(title_value)}</title>
  <desc id="desc">{esc(description)}</desc>
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker>
    <style>
      @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Regular.ttf'); }}
      @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Bold.ttf'); font-weight: 700; }}
      @font-face {{ font-family: PlexCondensed; src: url('../../gi-scenario-film/assets/fonts/IBMPlexSansCondensed-SemiBold.ttf'); font-weight: 600; }}
      @font-face {{ font-family: PlexMono; src: url('../../gi-scenario-film/assets/fonts/IBMPlexMono-Regular.ttf'); }}
      .eyebrow {{ font: 700 21px Atkinson, sans-serif; letter-spacing: 1px; fill: {TEAL}; }}
      .headline {{ font: 600 42px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .subhead {{ font: 400 24px Atkinson, sans-serif; fill: {MUTED}; }}
      .section {{ font: 700 30px Atkinson, sans-serif; fill: {INK}; }}
      .body {{ font: 400 26px Atkinson, sans-serif; fill: {INK}; }}
      .small {{ font: 400 21px Atkinson, sans-serif; fill: {MUTED}; }}
      .label {{ font: 700 24px Atkinson, sans-serif; letter-spacing: .4px; fill: {INK}; }}
      .tinyLabel {{ font: 700 18px Atkinson, sans-serif; letter-spacing: .25px; fill: {INK}; }}
      .mono {{ font: 400 27px PlexMono, monospace; fill: {INK}; }}
      .number {{ font: 600 62px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .numberSmall {{ font: 600 46px PlexCondensed, Atkinson, sans-serif; fill: {INK}; }}
      .whiteLabel {{ font: 700 23px Atkinson, sans-serif; fill: white; }}
      .whiteBody {{ font: 400 24px Atkinson, sans-serif; fill: white; }}
    </style>
  </defs>
  <rect width="540" height="{height}" fill="{PAPER}"/>
  {content}
</svg>'''


def build_counterfactual(summary: dict) -> str:
    case = summary["representative_counterfactual"]
    assert case["same_plan_returned"] is True
    assert case["answer_released"] is False
    assert case["original_value"] == 0.79
    assert case["changed_value"] == 0.91
    assert case["threshold"] == 0.85
    parts = [
        text(68, 58, "ONE SYNTHETIC VALIDATION CASE", "eyebrow"),
        text(68, 118, "The value crossed the limit.", "headline"),
        text(68, 172, "The selected plan stayed the same.", "headline"),
        text(68, 214, "Capacity ratio for C-19 · fixed rule: ratio ≤ 0.85 · same question", "subhead"),
    ]
    cards = [
        (68, "ORIGINAL EVIDENCE", case["original_value"], "RULE MET", TEAL, PALE_TEAL, True),
        (626, "CHANGED EVIDENCE", case["changed_value"], "RULE FAILED", RED, PALE_RED, False),
    ]
    for x, label, value, status, color, pale, rule_met in cards:
        parts += [
            rect(x, 250, 506, 276, fill=WHITE, stroke=color, radius=26, stroke_width=3),
            text(x + 30, 292, label, "label", fill=color),
            text(x + 30, 356, f'C-19 = {value:.2f}', "number"),
            text(x + 30, 402, "declared rule  ≤  0.85", "mono"),
            rect(x + 30, 432, 196, 62, fill=color, stroke=color, radius=14),
            text(x + 128, 471, status, "whiteLabel", anchor="middle"),
            rect(x + 250, 432, 226, 62, fill=PALE_BLUE, stroke=BLUE, radius=14),
            multiline(x + 363, 458, ["Trace", "acknowledged"], "small", gap=24, anchor="middle", fill=BLUE),
            line(x + 253, 530, x + 253, 562, stroke=color, width=4, marker=True),
        ]
    parts += [
        rect(246, 574, 708, 88, fill=PALE_OCHRE, stroke=OCHRE, radius=20, stroke_width=3),
        text(600, 609, "Same predefined answer structure selected", "section", anchor="middle", fill=OCHRE),
        text(600, 643, "The test required a different plan. No answer was released.", "body", anchor="middle"),
        text(68, 710, "Prepared synthetic evidence · C-19 is a test identifier, not a surveyed asset", "small"),
    ]
    return svg_document(
        "The value crossed the limit; the selected plan stayed the same.",
        "One representative synthetic validation case uses the identifier C-19. Its capacity ratio changes from 0.79, which meets the fixed less-than-or-equal-to 0.85 rule, to 0.91, which fails it. The selected predefined answer structure did not change. Both Weave traces were acknowledged. No answer was released. These are prepared test records from a separate project family, not SWMM results or field measurements.",
        "\n".join(parts),
        height=746,
    )


def build_listening_tests() -> str:
    parts = [
        text(68, 58, "CONTROLLED EVIDENCE TEST", "eyebrow"),
        text(68, 118, "Three checks for choosing an explanation", "headline"),
        text(68, 158, "A plan is a predefined answer structure; the model selects which one applies.", "subhead"),
    ]
    rows = [
        (208, "1", "Relevant value crosses the limit", "the rule stays fixed", ["Choose the plan for", "the changed result"], RED, PALE_RED),
        (352, "2", "Only an irrelevant value changes", "relevant evidence stays fixed", ["Keep the same plan"], BLUE, PALE_BLUE),
        (496, "3", "Required evidence is missing", "a result cannot be supported", ["Choose an insufficient-", "evidence plan"], OCHRE, PALE_OCHRE),
    ]
    for y, number, change, evidence, expected, color, pale in rows:
        parts += [
            rect(68, y, 1064, 116, fill=WHITE, stroke=GRID, radius=22),
            circle(118, y + 58, 28, fill=color),
            text(118, y + 67, number, "whiteLabel", anchor="middle"),
            text(164, y + 47, change, "body"),
            text(164, y + 82, evidence, "small"),
            line(588, y + 58, 678, y + 58, stroke=color, width=4, marker=True),
            rect(708, y + 18, 388, 80, fill=pale, stroke=color, radius=14, stroke_width=2),
            multiline(902, y + (50 if len(expected) > 1 else 67), expected, "body", gap=30, anchor="middle", fill=color),
        ]
    parts += [
        text(68, 658, "Each scenario is shown twice, with reordered evidence and renamed identifiers.", "small"),
        text(68, 688, "Both presentations must produce the expected behavior for that scenario to pass.", "small"),
    ]
    return svg_document(
        "Three checks for choosing an explanation",
        "The model selects a predefined answer structure. A relevant value crossing a fixed limit requires a plan for the changed result. Changing only an irrelevant value should keep the selected plan the same. Missing required evidence should select the insufficient-evidence plan. Each scenario is shown twice with reordered evidence and renamed identifiers; both presentations must behave correctly.",
        "\n".join(parts),
        height=724,
    )


def build_two_ledgers(summary: dict) -> str:
    total = summary["total_resource_summary"]
    assert total["attempt_count"] == 672
    assert total["provider_response_count"] == 672
    assert total["trace_acknowledged_count"] == 672
    validation = {row["model_id"]: row for row in summary["mechanism_summaries"] if row["split"] == "validation"}
    granite = validation["ibm-granite/granite-4.1-8b"]
    qwen = validation["Qwen/Qwen3-30B-A3B-Instruct-2507"]
    assert granite["complete_mechanism"]["success"] == 0
    assert qwen["complete_mechanism"]["success"] == 3
    control = summary["d1_deterministic_control"]
    assert control["passed_count"] == control["cell_count"] == 192

    parts = [
        text(68, 58, "TWO LEDGERS", "eyebrow"),
        text(68, 118, "Every call returned. The behavior didn't.", "headline"),
        text(68, 158, "A complete call record and evidence-following are different results.", "subhead"),
        rect(68, 204, 442, 352, fill=PALE_TEAL, stroke=TEAL, radius=26, stroke_width=3),
        text(100, 250, "OPERATIONAL LEDGER", "label", fill=TEAL),
        text(100, 326, "672 / 672", "number", fill=INK),
        text(100, 364, "provider responses", "body"),
        text(100, 438, "672 / 672", "numberSmall", fill=INK),
        text(100, 474, "Weave trace acknowledgements", "body"),
        rect(100, 500, 378, 34, fill=TEAL, stroke=TEAL, radius=10),
        text(289, 525, "NO RUNTIME RETRIES", "whiteLabel", anchor="middle"),
        rect(538, 204, 594, 352, fill=WHITE, stroke=RED, radius=26, stroke_width=3),
        text(570, 250, "HELD-OUT BEHAVIOR LEDGER", "label", fill=RED),
        text(920, 294, "Granite", "label", anchor="middle"),
        text(1060, 294, "Qwen", "label", anchor="middle"),
    ]
    rows = [
        (334, "Target crosses rule", granite["threshold_sensitivity"]["success"], qwen["threshold_sensitivity"]["success"]),
        (384, "Distractor stays irrelevant", granite["distractor_specificity"]["success"], qwen["distractor_specificity"]["success"]),
        (434, "Missing target → abstain", granite["withheld_target_abstention"]["success"], qwen["withheld_target_abstention"]["success"]),
        (500, "All three", granite["complete_mechanism"]["success"], qwen["complete_mechanism"]["success"]),
    ]
    for y, label, g, q in rows:
        cls = "label" if label == "All three" else "tableBody"
        fill = RED if label == "All three" else INK
        parts += [
            text(570, y, label, cls, fill=fill),
            text(920, y, f"{g}/12", "mono", anchor="middle", fill=fill),
            text(1060, y, f"{q}/12", "mono", anchor="middle", fill=fill),
        ]
        if label != "All three":
            parts.append(line(570, y + 16, 1098, y + 16, stroke=GRID, width=2))
    parts += [
        rect(68, 584, 1064, 54, fill=INK, stroke=INK, radius=14),
        text(90, 619, "DETERMINISTIC POSITIVE CONTROL", "whiteLabel"),
        text(1108, 619, f'{control["passed_count"]}/{control["cell_count"]} protocol cells', "whiteLabel", anchor="end"),
        text(68, 674, "Control checks protocol machinery; it does not measure practitioner usefulness.", "small"),
        rect(68, 700, 1064, 70, fill=PALE_RED, stroke=RED, radius=16, stroke_width=3),
        text(96, 744, "DISABLE LIVE GUIDE", "label", fill=RED),
        text(1104, 744, "0 answers released", "label", anchor="end", fill=RED),
        text(68, 806, "Synthetic post-retrieval evidence · 672 is a call count, not a scenario count.", "small"),
    ]
    return svg_document(
        "Every call returned. The behavior did not.",
        "The operational ledger shows 672 of 672 provider responses and 672 of 672 trace acknowledgements. The held-out behavior ledger shows Granite passing all three grounding mechanisms in zero of twelve scenarios and Qwen in three of twelve. A separately labeled deterministic control passes 192 of 192 cells.",
        "\n".join(parts),
        height=830,
    )


def build_authority_map() -> str:
    parts = [
        text(68, 58, "INTENDED PRODUCT ARCHITECTURE", "eyebrow"),
        text(68, 118, "We gave the model a smaller job than it wanted.", "headline"),
        text(68, 158, "The explanation path can be observed. It cannot acquire hydraulic or decision authority.", "subhead"),
    ]
    nodes = [
        (68, 310, 188, 120, PALE_BLUE, BLUE, "SWMM RESULT", ["modeled flow", "and volume"]),
        (286, 310, 188, 120, PALE_TEAL, TEAL, "REPOSITORY", ["facts · units", "rules · limits"]),
        (504, 310, 168, 120, WHITE, INK, "READ-ONLY", ["bounded", "evidence tools"]),
        (702, 310, 188, 120, PALE_OCHRE, OCHRE, "MODEL", ["selects one", "explanation plan"]),
        (920, 310, 212, 120, WHITE, INK, "RENDER + VALIDATE", ["insert exact facts", "check plan/evidence"]),
    ]
    for x, y, w, h, fill, stroke, label, body_lines in nodes:
        label_class = "tinyLabel" if label == "RENDER + VALIDATE" else "label"
        body_class = "small" if label == "RENDER + VALIDATE" else "body"
        parts += [
            rect(x, y, w, h, fill=fill, stroke=stroke, radius=22, stroke_width=3),
            text(x + w // 2, y + 40, label, label_class, anchor="middle", fill=stroke),
            multiline(x + w // 2, y + 76, body_lines, body_class, gap=29, anchor="middle", fill=INK),
        ]
    for x1, x2 in [(256, 286), (474, 504), (672, 702), (890, 920)]:
        parts.append(line(x1, 370, x2 - 8, 370, stroke=INK, width=4, marker=True))
    parts += [
        rect(680, 194, 452, 50, fill=PALE_OCHRE, stroke=OCHRE, radius=12, stroke_width=2),
        text(906, 216, "R4c STARTS WITH A PREPARED PACKET", "tinyLabel", anchor="middle", fill=OCHRE),
        text(906, 238, "RETRIEVAL NOT TESTED", "tinyLabel", anchor="middle", fill=OCHRE),
        rect(476, 258, 656, 38, fill=PAPER, stroke=TEAL, radius=12, stroke_width=2, dash="8 8"),
        text(804, 283, "WEAVE RECORDS EXECUTION · IT DOES NOT CERTIFY THE CHOICE", "tinyLabel", anchor="middle", fill=TEAL),
        line(588, 296, 588, 304, stroke=TEAL, width=2, dash="6 6"),
        line(796, 296, 796, 304, stroke=TEAL, width=2, dash="6 6"),
        line(1026, 296, 1026, 304, stroke=TEAL, width=2, dash="6 6"),
        line(1026, 434, 1026, 458, stroke=INK, width=4),
        '<polygon points="1026,458 1040,472 1026,486 1012,472" fill="' + INK + '"/>',
        line(1012, 472, 803, 472, stroke=TEAL, width=4),
        line(803, 472, 803, 504, stroke=TEAL, width=4, marker=True),
        line(1040, 472, 1052, 472, stroke=RED, width=5),
        line(1052, 472, 1052, 504, stroke=RED, width=5, marker=True),
        rect(678, 520, 250, 82, fill=PALE_TEAL, stroke=TEAL, radius=18, stroke_width=3),
        text(803, 553, "RELEASE", "label", anchor="middle", fill=TEAL),
        text(803, 583, "all checks pass", "small", anchor="middle"),
        rect(956, 520, 176, 82, fill=PALE_RED, stroke=RED, radius=18, stroke_width=4),
        text(1044, 553, "STOP", "label", anchor="middle", fill=RED),
        text(1044, 583, "any check fails", "small", anchor="middle"),
        rect(68, 646, 1064, 56, fill=INK, stroke=INK, radius=14),
        text(100, 682, "HYDRAULICS", "whiteLabel"),
        text(260, 682, "SWMM", "whiteBody"),
        text(440, 682, "VALUES", "whiteLabel"),
        text(556, 682, "repository", "whiteBody"),
        text(760, 682, "DECISION", "whiteLabel"),
        text(898, 682, "engineer", "whiteBody"),
    ]
    return svg_document(
        "Authority map for the Evidence Guide",
        "An intended left-to-right architecture shows SWMM results, repository-owned facts and rules, bounded read-only retrieval, a model selecting an explanation plan, and repository rendering plus validation. A marker shows that R4c began with a prepared packet and did not test retrieval. An exclusive gate releases only when all checks pass and otherwise stops; the study followed the stop path. Weave is a separate observation layer.",
        "\n".join(parts),
        height=730,
    )


def build_counterfactual_mobile(summary: dict) -> str:
    case = summary["representative_counterfactual"]
    parts = [
        text(30, 46, "ONE SYNTHETIC VALIDATION CASE", "eyebrow"),
        text(30, 102, "The value crossed the limit", "headline"),
        text(30, 152, "The plan stayed the same", "headline"),
        text(30, 194, "Capacity ratio for C-19 · same question", "subhead"),
    ]
    cards = [
        (30, 230, "ORIGINAL", case["original_value"], "RULE MET", TEAL, PALE_TEAL),
        (30, 526, "CHANGED", case["changed_value"], "RULE FAILED", RED, PALE_RED),
    ]
    for x, y, label, value, status, color, pale in cards:
        parts += [
            rect(x, y, 480, 266, fill=WHITE, stroke=color, radius=24, stroke_width=3),
            text(x + 26, y + 40, f"{label} EVIDENCE", "label", fill=color),
            text(x + 26, y + 104, f'C-19 = {value:.2f}', "number"),
            text(x + 26, y + 146, "declared rule  ≤  0.85", "mono"),
            rect(x + 26, y + 176, 184, 64, fill=color, stroke=color, radius=12),
            text(x + 118, y + 216, status, "whiteLabel", anchor="middle"),
            rect(x + 228, y + 176, 224, 64, fill=PALE_BLUE, stroke=BLUE, radius=12),
            multiline(x + 340, y + 202, ["Trace", "acknowledged"], "small", gap=25, anchor="middle", fill=BLUE),
        ]
    parts += [
        rect(30, 832, 480, 150, fill=PALE_OCHRE, stroke=OCHRE, radius=20, stroke_width=3),
        multiline(270, 873, ["Same predefined answer", "structure selected"], "body", gap=32, anchor="middle", fill=OCHRE),
        text(270, 944, "The test required a different plan.", "small", anchor="middle"),
        text(270, 1018, "No answer was released.", "body", anchor="middle"),
        multiline(30, 1074, ["Prepared synthetic evidence", "C-19 is a test identifier, not a surveyed asset.", "One representative validation pair."], "small", gap=30),
    ]
    return svg_mobile_document(
        "The value crossed the limit; the selected plan stayed the same.",
        "One representative synthetic validation case changes capacity ratio C-19 from 0.79 within a fixed 0.85 rule to 0.91 outside it, while the same predefined answer structure is selected. Both Weave traces were acknowledged. No answer was released. These are prepared test records from a separate project family, not SWMM results or field measurements.",
        "\n".join(parts),
        height=1170,
    )


def build_listening_tests_mobile() -> str:
    parts = [
        text(30, 46, "CONTROLLED EVIDENCE TEST", "eyebrow"),
        text(30, 102, "Three checks for choosing", "headline"),
        text(30, 152, "an explanation", "headline"),
        multiline(30, 196, ["The model selects a plan:", "a predefined answer structure."], "subhead", gap=31),
    ]
    rows = [
        (264, "1", ["Relevant value crosses", "the fixed limit"], ["Choose the plan for", "the changed result"], RED, PALE_RED),
        (516, "2", ["Only an irrelevant", "value changes"], ["Keep the same plan"], BLUE, PALE_BLUE),
        (768, "3", ["Required evidence", "is missing"], ["Choose an insufficient-", "evidence plan"], OCHRE, PALE_OCHRE),
    ]
    for y, number, label, expected, color, pale in rows:
        parts += [
            rect(30, y, 480, 220, fill=WHITE, stroke=GRID, radius=22),
            circle(76, y + 61, 27, fill=color),
            text(76, y + 70, number, "whiteLabel", anchor="middle"),
            multiline(120, y + 51, label, "body", gap=32),
            line(270, y + 100, 270, y + 124, stroke=color, width=3, marker=True),
            rect(56, y + 138, 428, 66, fill=pale, stroke=color, radius=12, stroke_width=2),
            multiline(270, y + (164 if len(expected) > 1 else 181), expected, "body", gap=29, anchor="middle", fill=color),
        ]
    parts += [
        multiline(30, 1040, ["Each scenario is shown twice, with reordered", "evidence and renamed identifiers.", "Both presentations must behave correctly."], "small", gap=30),
    ]
    return svg_mobile_document(
        "Three checks for choosing an explanation",
        "The model selects a predefined answer structure. A relevant value crossing the fixed limit requires a plan for the changed result. Changing only an irrelevant value should keep the same plan. Missing required evidence should select the insufficient-evidence plan. Both reordered, renamed presentations must behave correctly for a scenario to pass.",
        "\n".join(parts),
        height=1140,
    )


def build_two_ledgers_mobile(summary: dict) -> str:
    total = summary["total_resource_summary"]
    validation = {row["model_id"]: row for row in summary["mechanism_summaries"] if row["split"] == "validation"}
    granite = validation["ibm-granite/granite-4.1-8b"]
    qwen = validation["Qwen/Qwen3-30B-A3B-Instruct-2507"]
    control = summary["d1_deterministic_control"]
    parts = [
        text(30, 46, "TWO LEDGERS", "eyebrow"),
        text(30, 102, "Every call returned.", "headline"),
        text(30, 152, "The behavior didn't.", "headline"),
        rect(30, 204, 480, 260, fill=PALE_TEAL, stroke=TEAL, radius=24, stroke_width=3),
        text(56, 246, "OPERATIONAL LEDGER", "label", fill=TEAL),
        text(56, 316, f'{total["provider_response_count"]}/{total["attempt_count"]}', "number"),
        text(56, 350, "provider responses", "body"),
        text(56, 406, f'{total["trace_acknowledged_count"]}/{total["attempt_count"]} traces acknowledged', "section"),
        rect(30, 494, 480, 478, fill=WHITE, stroke=RED, radius=24, stroke_width=3),
        text(56, 538, "HELD-OUT BEHAVIOR", "label", fill=RED),
        text(360, 584, "Granite", "tinyLabel", anchor="middle"),
        text(458, 584, "Qwen", "tinyLabel", anchor="middle"),
    ]
    rows = [
        (628, "Target crosses rule", granite["threshold_sensitivity"]["success"], qwen["threshold_sensitivity"]["success"]),
        (696, "Distractor only", granite["distractor_specificity"]["success"], qwen["distractor_specificity"]["success"]),
        (764, "Evidence withheld", granite["withheld_target_abstention"]["success"], qwen["withheld_target_abstention"]["success"]),
        (852, "All three", granite["complete_mechanism"]["success"], qwen["complete_mechanism"]["success"]),
    ]
    for y, label, g, q in rows:
        cls = "label" if label == "All three" else "body"
        fill = RED if label == "All three" else INK
        parts += [
            text(56, y, label, cls, fill=fill),
            text(360, y, f"{g}/12", "mono", anchor="middle", fill=fill),
            text(458, y, f"{q}/12", "mono", anchor="middle", fill=fill),
        ]
        if label != "All three":
            parts.append(line(56, y + 20, 484, y + 20, stroke=GRID, width=2))
    parts += [
        rect(56, 898, 428, 50, fill=INK, stroke=INK, radius=12),
        text(270, 931, f'D1 control  {control["passed_count"]}/{control["cell_count"]} cells', "whiteLabel", anchor="middle"),
        rect(30, 1006, 480, 104, fill=PALE_RED, stroke=RED, radius=18, stroke_width=3),
        text(270, 1048, "DISABLE LIVE GUIDE", "label", anchor="middle", fill=RED),
        text(270, 1082, "Practitioner usefulness not measured", "small", anchor="middle"),
        multiline(30, 1162, ["672 is a call count, not a scenario count.", "The control checks machinery — not model usefulness."], "small", gap=30),
    ]
    return svg_mobile_document(
        "Every call returned. The behavior did not.",
        "A mobile two-ledger view shows 672 of 672 responses and trace acknowledgements, held-out mechanism counts for Granite and Qwen, a separate 192 of 192 deterministic control, and the decision to disable the live Guide.",
        "\n".join(parts),
        height=1240,
    )


def build_authority_map_mobile() -> str:
    parts = [
        text(30, 46, "INTENDED PRODUCT ARCHITECTURE", "eyebrow"),
        text(30, 102, "The model gets", "headline"),
        text(30, 152, "one small job.", "headline"),
        rect(30, 198, 480, 82, fill=PAPER, stroke=TEAL, radius=16, stroke_width=2, dash="8 8"),
        text(270, 232, "WEAVE RECORDS THE INVESTIGATION", "tinyLabel", anchor="middle", fill=TEAL),
        text(270, 260, "IT DOES NOT CERTIFY THE CHOICE", "tinyLabel", anchor="middle", fill=TEAL),
    ]
    nodes = [
        (310, PALE_BLUE, BLUE, "1 · SWMM RESULT", "modeled flow and volume"),
        (442, PALE_TEAL, TEAL, "2 · REPOSITORY", "facts · units · rules · limits"),
        (574, WHITE, INK, "3 · READ-ONLY TOOLS", "retrieve bounded evidence"),
        (790, PALE_OCHRE, OCHRE, "4 · MODEL", "select one explanation plan"),
        (922, WHITE, INK, "5 · RENDER + VALIDATE", "insert exact facts · check plan"),
    ]
    for y, fill, stroke, label, body_value in nodes:
        parts += [
            rect(50, y, 440, 102, fill=fill, stroke=stroke, radius=20, stroke_width=3),
            text(78, y + 40, label, "label", fill=stroke),
            text(78, y + 76, body_value, "body"),
        ]
        if y != nodes[-1][0] and y != 574:
            parts.append(line(270, y + 104, 270, y + 124, stroke=INK, width=4, marker=True))
    parts += [
        rect(50, 696, 440, 62, fill=PALE_OCHRE, stroke=OCHRE, radius=14, stroke_width=2),
        text(270, 722, "R4c STARTS WITH A PREPARED PACKET", "tinyLabel", anchor="middle", fill=OCHRE),
        text(270, 748, "RETRIEVAL NOT TESTED", "tinyLabel", anchor="middle", fill=OCHRE),
        line(270, 678, 270, 690, stroke=INK, width=4, marker=True),
        line(270, 758, 270, 778, stroke=INK, width=4, marker=True),
        line(270, 1026, 270, 1042, stroke=INK, width=4),
        '<polygon points="270,1042 284,1056 270,1070 256,1056" fill="' + INK + '"/>',
        line(256, 1056, 142, 1056, stroke=TEAL, width=4),
        line(142, 1056, 142, 1080, stroke=TEAL, width=4, marker=True),
        line(284, 1056, 398, 1056, stroke=RED, width=5),
        line(398, 1056, 398, 1080, stroke=RED, width=5, marker=True),
        rect(30, 1096, 224, 92, fill=PALE_TEAL, stroke=TEAL, radius=16, stroke_width=3),
        text(142, 1132, "RELEASE", "label", anchor="middle", fill=TEAL),
        text(142, 1164, "all checks pass", "small", anchor="middle"),
        rect(286, 1096, 224, 92, fill=PALE_RED, stroke=RED, radius=16, stroke_width=4),
        text(398, 1132, "STOP", "label", anchor="middle", fill=RED),
        text(398, 1164, "any check fails", "small", anchor="middle"),
        rect(30, 1224, 480, 110, fill=INK, stroke=INK, radius=16),
        text(56, 1262, "HYDRAULICS", "whiteLabel"),
        text(252, 1262, "SWMM", "whiteBody"),
        text(56, 1297, "VALUES", "whiteLabel"),
        text(252, 1297, "repository", "whiteBody"),
        text(56, 1328, "DECISION", "whiteLabel"),
        text(252, 1328, "engineer", "whiteBody"),
    ]
    return svg_mobile_document(
        "Authority map for the Evidence Guide",
        "A mobile vertical intended architecture shows SWMM results, repository facts and rules, read-only tools, a study boundary noting retrieval was not tested, one finite model plan, repository rendering and validation, and exclusive release and stop branches. Weave is a separate observation layer.",
        "\n".join(parts),
        height=1370,
    )


def main() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    summary = json.loads(SUMMARY_PATH.read_text())
    guided = json.loads(GUIDED_PATH.read_text())
    analysis = json.loads(ANALYSIS_PATH.read_text())

    assert summary["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert summary["production_release_eligible"] is False
    assert summary["practitioner_usefulness_status"] == "not_measured"
    assert analysis["actual_answers_released"] == 0
    alt017 = next(item for item in guided["teaching_alternatives"] if item["id"] == "ALT-017")
    assert alt017["outfall_volume_reduction_percent"] == 4.14
    assert alt017["present_value_cost_thousand_usd"] == 54.99
    assert [run["outfall_peak_delta_cfs"] for run in alt017["storm_runs"] if run["outfall_peak_delta_cfs"] > 0] == [0.07, 0.01]

    figures = {
        "02-listening-tests.svg": build_listening_tests(),
        "02-listening-tests-mobile.svg": build_listening_tests_mobile(),
        "01-counterfactual.svg": build_counterfactual(summary),
        "01-counterfactual-mobile.svg": build_counterfactual_mobile(summary),
    }
    for name, value in figures.items():
        (FIGURES / name).write_text(value)

    manifest = {
        "schema_version": "1.1",
        "article_id": "gi-evidence-guide-listening-v2",
        "production_decision": summary["production_decision"],
        "active_element_order": ["alt017-rules", "02-listening-tests", "01-counterfactual", "held-out-results"],
        "table_claims": {
            "alt017-rules": {
                "source": str(GUIDED_PATH.relative_to(REPO_ROOT)),
                "source_pointer": "/teaching_alternatives/id=ALT-017",
                "rules": alt017["screening"]["checks"],
                "accepted_qa_runs": sum(run["qa_status"] == "accepted" for run in alt017["storm_runs"]),
                "run_count": len(alt017["storm_runs"]),
                "peak_increases": [
                    {key: run[key] for key in ["storm_id", "baseline_outfall_peak_cfs", "outfall_peak_cfs", "outfall_peak_delta_cfs"]}
                    for run in alt017["storm_runs"] if run["outfall_peak_delta_cfs"] > 0
                ],
            },
            "held-out-results": {
                "source": str(SUMMARY_PATH.relative_to(REPO_ROOT)),
                "source_pointer": "/mechanism_summaries/split=validation",
                "statistical_unit": summary["statistical_unit"],
                "rows": [row for row in summary["mechanism_summaries"] if row["split"] == "validation"],
            },
        },
        "source_files": {
            str(path.relative_to(REPO_ROOT)): sha256(path)
            for path in [SUMMARY_PATH, GUIDED_PATH, ANALYSIS_PATH, ATTEMPTS_PATH]
        },
        "figures": {
            name: {"sha256": sha256(FIGURES / name), "width": 540 if "-mobile" in name else 1200}
            for name in figures
        },
    }
    (DIST / "evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"built {len(figures)} evidence-driven SVG figures")


if __name__ == "__main__":
    main()
