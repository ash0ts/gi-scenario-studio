#!/usr/bin/env python3
"""Render the evidence-grounded figures for the EcoCorridor long-form article.

The figures are derived from the committed Ridgeway projection and the retained
Review Brief campaign constants.  They are article illustrations, not new
experiment results.  Every displayed number is also written to a canonical
manifest beside the image assets.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(tempfile.gettempdir()) / "ecocorridor-article-matplotlib"),
)
os.environ.setdefault(
    "XDG_CACHE_HOME",
    str(Path(tempfile.gettempdir()) / "ecocorridor-article-cache"),
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.usefulness_narrative import (
    HISTORICAL_BASELINE_INPUT_TOKENS,
    HISTORICAL_CONTEXT_REDUCTION_PERCENT,
    HISTORICAL_NATIVE_INPUT_TOKENS,
    HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
    HISTORICAL_PROVIDER_CONVERSATION_COUNT,
    HISTORICAL_RECORD_COUNT,
)
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

SCHEMA_VERSION = "ecocorridor-visual-story-figures/v1"
CANDIDATE_ID = "portfolio_d3aa8b1e170ffd76b322"
FIGURE_DPI = 160

INK = "#172B2B"
MIST = "#EDF2EE"
PAPER = "#FAFCFA"
CYAN = "#167D8D"
LICHEN = "#B7CA9D"
LICHEN_DARK = "#537144"
GRAPHITE = "#4B5357"
BERRY = "#A33F5B"
AMBER = "#B56B24"
AMBER_PALE = "#F6E4CC"
CYAN_PALE = "#DCEEF0"
LINE = "#B8C7C2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_projection(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    source = root / "benchmarks/ecocorridor/ridgeway/ui_projection.json"
    projection = json.loads(source.read_text(encoding="utf-8"))
    candidates = [item for item in projection["portfolios"] if item["id"] == CANDIDATE_ID]
    if len(candidates) != 1:
        raise ValueError("Ridgeway carry-forward candidate identity differs")
    candidate = candidates[0]
    if candidate["requiredCount"] != 12 or candidate["passCount"] != 12:
        raise ValueError("Ridgeway candidate scenario completeness differs")
    if candidate["ruleStatus"] != "review" or not candidate["roadMaskFlag"]:
        raise ValueError("Ridgeway candidate review disposition differs")
    return projection, candidate, source


def _setup() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "text.color": INK,
            "axes.labelcolor": GRAPHITE,
            "axes.edgecolor": LINE,
            "xtick.color": GRAPHITE,
            "ytick.color": GRAPHITE,
            "svg.fonttype": "none",
            "svg.hashsalt": "ecocorridor-visual-story-v1",
        }
    )


def _save(fig: Any, output: Path, figure_id: str) -> tuple[Path, Path]:
    png = output / f"{figure_id}.png"
    svg = output / f"{figure_id}.svg"
    fig.savefig(
        png,
        dpi=FIGURE_DPI,
        facecolor=PAPER,
        metadata={"Software": "EcoCorridor Scenario Studio"},
    )
    fig.savefig(
        svg,
        facecolor=PAPER,
        metadata={"Date": None, "Creator": "EcoCorridor Scenario Studio"},
    )
    # Matplotlib emits multiline SVG path data with trailing spaces. Normalize
    # generated text so repository whitespace checks remain meaningful and the
    # same renderer produces stable reviewable diffs across platforms.
    svg_text = svg.read_text(encoding="utf-8")
    svg.write_text(
        "\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
        encoding="utf-8",
    )
    plt.close(fig)
    return png, svg


def _wrap(value: str, width: int) -> str:
    words = value.split()
    lines: list[str] = []
    line: list[str] = []
    for word in words:
        candidate = " ".join([*line, word])
        if line and len(candidate) > width:
            lines.append(" ".join(line))
            line = [word]
        else:
            line.append(word)
    if line:
        lines.append(" ".join(line))
    return "\n".join(lines)


def _card(
    ax: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    face: str,
    edge: str,
    linewidth: float = 1.5,
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=1.6",
            facecolor=face,
            edgecolor=edge,
            linewidth=linewidth,
        )
    )


def _decision_figure(
    projection: dict[str, Any], candidate: dict[str, Any], output: Path
) -> dict[str, Any]:
    safeguard = [
        item for item in candidate["scenarioResults"] if "safeguard_guild" in item["scenarioId"]
    ]
    worst_resistance = max(item["resistanceChangePct"] for item in safeguard)
    mask_values = [item["reviewMaskChangePct"] for item in safeguard]
    budget = projection["decisionBrief"]["budgetUsd"]

    fig, ax = plt.subplots(figsize=(12, 9))
    fig.patch.set_facecolor(PAPER)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    ax.text(
        3,
        96,
        "The model passed. The decision did not turn green.",
        fontsize=25,
        fontweight="bold",
        va="top",
    )
    ax.text(
        3,
        88.5,
        "One synthetic Ridgeway candidate, four kinds of evidence, one bounded next step",
        fontsize=13,
        color=GRAPHITE,
        va="top",
    )

    cards = [
        (
            3,
            64,
            "COMPLETE ENSEMBLE",
            "12 / 12",
            "No required modeled connectivity check failed",
            CYAN_PALE,
            CYAN,
        ),
        (
            52,
            64,
            "SCREENING COST",
            rf"\${candidate['costUsd'] / 1000:.0f}k / \${budget / 1000:.0f}k",
            "Inside the synthetic screening cost cap",
            CYAN_PALE,
            CYAN,
        ),
        (
            3,
            42,
            "LEAST FAVORABLE SAFEGUARD",
            f"{abs(worst_resistance):.3f}% better",
            "Modeled effective resistance still decreased",
            CYAN_PALE,
            CYAN,
        ),
        (
            52,
            42,
            "ROAD-AREA DIAGNOSTIC",
            f"{min(mask_values):.0f}–{max(mask_values):.0f}% change",
            "Above the authored 15% review trigger",
            AMBER_PALE,
            AMBER,
        ),
    ]
    for x, y, label, value, detail, face, edge in cards:
        _card(ax, x, y, 45, 17, face=face, edge=edge, linewidth=2)
        ax.text(x + 2.2, y + 13.2, label, fontsize=9.5, fontweight="bold", color=edge)
        ax.text(x + 2.2, y + 8.1, value, fontsize=19, fontweight="bold", color=INK)
        ax.text(x + 2.2, y + 2.5, _wrap(detail, 42), fontsize=9.7, color=GRAPHITE)

    ax.add_patch(
        FancyArrowPatch(
            (50, 39.5),
            (50, 33.5),
            arrowstyle="-|>",
            mutation_scale=18,
            linewidth=1.8,
            color=GRAPHITE,
        )
    )
    _card(ax, 8, 9, 84, 21, face=MIST, edge=INK, linewidth=2)
    ax.text(12, 26, "DECISION", fontsize=10.5, fontweight="bold", color=AMBER, va="top")
    ax.text(
        12,
        21,
        "Carry forward for ecological, field, and engineering\nfeasibility review",
        fontsize=16.5,
        fontweight="bold",
        va="top",
    )
    ax.text(
        12,
        12.5,
        "The road-area result changes the next action. It does not predict animal movement.",
        fontsize=10.5,
        color=GRAPHITE,
        va="top",
    )

    png, svg = _save(fig, output, "ridgeway-decision")
    return {
        "figure_id": "ridgeway-decision",
        "title": "Why the Ridgeway candidate remained a review",
        "question": "How can every required connectivity check pass while the overall disposition remains review?",
        "takeaway": "A separate road-area diagnostic changed the next action from modeled pass to field review.",
        "alt_text": (
            "Four evidence cards show 12 of 12 scenarios complete, a 400,000 dollar cost "
            "inside a 500,000 dollar cap, 1.603 percent lower modeled effective resistance "
            "in the least-favorable safeguard case, "
            "and a 47 to 66 percent road-area current change above a 15 percent trigger. "
            "An arrow leads to carry forward for feasibility review."
        ),
        "caption": (
            "Three favorable checks did not erase the separate road-area review flag. The "
            "supported action was further ecological, field, and engineering review—not approval."
        ),
        "forbidden_implication": "Observed animal movement, crossing use, ecological benefit, or approval.",
        "values": {
            "scenario_pass_count": candidate["passCount"],
            "scenario_required_count": candidate["requiredCount"],
            "cost_usd": candidate["costUsd"],
            "budget_usd": budget,
            "least_favorable_safeguard_resistance_change_percent": worst_resistance,
            "review_mask_min_change_percent": min(mask_values),
            "review_mask_max_change_percent": max(mask_values),
            "review_trigger_percent": 15.0,
        },
        "source_locations": [
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:portfolios[carry-forward]",
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:decisionBrief",
        ],
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def _road_mask_figure(candidate: dict[str, Any], output: Path) -> dict[str, Any]:
    safeguard = [
        item for item in candidate["scenarioResults"] if "safeguard_guild" in item["scenarioId"]
    ]
    names = ["Low barrier", "Moderate", "High barrier", "Future pressure"]
    values = [item["reviewMaskChangePct"] for item in safeguard]
    if len(values) != 4:
        raise ValueError("Ridgeway safeguard scenario count differs")

    fig, ax = plt.subplots(figsize=(12, 8))
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    fig.subplots_adjust(left=0.18, right=0.95, top=0.76, bottom=0.22)
    bars = ax.barh(names, values, color=AMBER, height=0.55)
    ax.axvline(15, color=BERRY, linewidth=2.5, linestyle=(0, (5, 4)))
    ax.text(15.8, -0.48, "15% authored review trigger", color=BERRY, fontsize=11.5, va="center")
    for bar, value in zip(bars, values, strict=True):
        ax.text(
            value + 1.2,
            bar.get_y() + bar.get_height() / 2,
            f"{value:.2f}%",
            va="center",
            fontsize=14,
            fontweight="bold",
            color=INK,
        )
    ax.set_xlim(0, 74)
    ax.invert_yaxis()
    ax.set_xlabel(
        "Change in total modeled current inside the fixed road-review mask (%)",
        fontsize=13,
        labelpad=14,
    )
    fig.text(
        0.18,
        0.91,
        "The road-area flag appeared in all four safeguard cases.",
        fontsize=22,
        fontweight="bold",
        ha="left",
        va="top",
    )
    fig.text(
        0.18,
        0.845,
        "Each bar compares the candidate with the same scenario's no-action baseline.",
        fontsize=12.5,
        color=GRAPHITE,
        ha="left",
        va="top",
    )
    fig.text(
        0.18,
        0.075,
        "Circuit diagnostic only. This is not observed or predicted animal movement.",
        fontsize=11.5,
        color=GRAPHITE,
        ha="left",
        va="bottom",
    )
    ax.grid(axis="x", color=LINE, linewidth=0.8, alpha=0.65)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=13)
    ax.tick_params(axis="x", labelsize=11)

    png, svg = _save(fig, output, "road-mask-hypotheses")
    return {
        "figure_id": "road-mask-hypotheses",
        "title": "Road-area diagnostic across safeguard hypotheses",
        "question": "Was the road-area flag caused by only one safeguard resistance hypothesis?",
        "takeaway": "No. The authored 15 percent review trigger was exceeded in all four safeguard scenarios.",
        "alt_text": (
            "Horizontal bars show road-area modeled-current changes of 47.35, 59.59, 65.65, "
            "and 64.77 percent for low-barrier, moderate, high-barrier, and future-pressure "
            "safeguard scenarios. Every bar lies beyond a dashed 15 percent review line."
        ),
        "caption": (
            "The warning did not depend on only one landscape assumption. It appeared in all four "
            "safeguard scenarios. The 15% line is an authored synthetic investigation trigger, "
            "not a biological threshold."
        ),
        "forbidden_implication": "Collision risk, road crossing probability, or animal-use concentration.",
        "values": {
            "scenario_ids": [item["scenarioId"] for item in safeguard],
            "review_mask_change_percent": values,
            "review_trigger_percent": 15.0,
        },
        "source_locations": [
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:portfolios[carry-forward].scenarioResults[safeguard]"
        ],
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def _current_map_figure(
    projection: dict[str, Any], candidate: dict[str, Any], output: Path
) -> dict[str, Any]:
    scenario_id = "scenario_safeguard_guild_h1_low_barrier"
    baseline_id = projection["baselinePortfolioId"]
    selected = [
        item
        for item in projection["currentRasters"]
        if item["scenarioId"] == scenario_id
        and item["portfolioId"] in {baseline_id, candidate["id"]}
    ]
    by_portfolio = {item["portfolioId"]: item for item in selected}
    if set(by_portfolio) != {baseline_id, candidate["id"]}:
        raise ValueError("Ridgeway low-barrier current-map pair differs")

    baseline_record = by_portfolio[baseline_id]
    candidate_record = by_portfolio[candidate["id"]]
    width = baseline_record["width"]
    height = baseline_record["height"]
    if (candidate_record["width"], candidate_record["height"]) != (width, height):
        raise ValueError("Ridgeway current-map dimensions differ")

    def _array(record: dict[str, Any]) -> np.ndarray:
        values = np.asarray(
            [np.nan if value is None else float(value) for value in record["cells"]],
            dtype=float,
        )
        return values.reshape((height, width))

    baseline = _array(baseline_record)
    portfolio = _array(candidate_record)
    mask_record = projection["reviewMask"]
    if (mask_record["width"], mask_record["height"]) != (width, height):
        raise ValueError("Ridgeway review-mask dimensions differ")
    mask = np.asarray(mask_record["cells"], dtype=int).reshape((height, width))
    valid = (mask == 1) & np.isfinite(baseline) & np.isfinite(portfolio)
    if int(valid.sum()) != 48:
        raise ValueError("Ridgeway review-mask valid-cell count differs")
    baseline_sum = float(baseline[valid].sum())
    portfolio_sum = float(portfolio[valid].sum())
    change_percent = (portfolio_sum - baseline_sum) / baseline_sum * 100.0
    expected = next(
        item["reviewMaskChangePct"]
        for item in candidate["scenarioResults"]
        if item["scenarioId"] == scenario_id
    )
    if not np.isclose(change_percent, expected, rtol=0, atol=1e-9):
        raise ValueError("Ridgeway review-mask current reducer differs")

    cmap = LinearSegmentedColormap.from_list(
        "ecocorridor_current", [PAPER, "#B9D9D9", CYAN, "#0B4F59"]
    )
    finite_values = np.concatenate(
        [baseline[np.isfinite(baseline)], portfolio[np.isfinite(portfolio)]]
    )
    vmin = float(finite_values.min())
    vmax = float(finite_values.max())
    fig, axes = plt.subplots(1, 2, figsize=(12, 7))
    fig.patch.set_facecolor(PAPER)
    fig.subplots_adjust(left=0.08, right=0.90, top=0.74, bottom=0.22, wspace=0.16)
    labels = [
        (axes[0], baseline, "No-action baseline", baseline_sum),
        (axes[1], portfolio, "Carry-forward candidate", portfolio_sum),
    ]
    image = None
    for axis, raster, label, mask_sum in labels:
        axis.set_facecolor(PAPER)
        image = axis.imshow(raster, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
        axis.contour(mask, levels=[0.5], colors=[AMBER], linewidths=2.5)
        axis.set_xticks([])
        axis.set_yticks([])
        axis.set_title(
            f"{label} · fixed-mask sum {mask_sum:.4f}",
            loc="left",
            fontsize=11.5,
            fontweight="bold",
            pad=8,
        )
        for spine in axis.spines.values():
            spine.set_edgecolor(LINE)
            spine.set_linewidth(1.0)
    assert image is not None
    colorbar = fig.colorbar(image, ax=axes, fraction=0.025, pad=0.025)
    colorbar.set_label("Modeled Circuitscape current (shared scale)", color=GRAPHITE, fontsize=10)
    colorbar.ax.tick_params(labelsize=8)
    fig.text(
        0.08,
        0.94,
        "The electrical map is not a deer.",
        fontsize=24,
        fontweight="bold",
        va="top",
    )
    fig.text(
        0.08,
        0.885,
        "Same synthetic grid, same scale, same fixed road-review area outlined in amber",
        fontsize=12.5,
        color=GRAPHITE,
        va="top",
    )
    fig.text(
        0.08,
        0.08,
        (
            f"The sum inside 48 fixed cells changed {change_percent:.2f}% relative to baseline.\n"
            "This is a circuit diagnostic—not observed or predicted animal movement."
        ),
        fontsize=11,
        color=GRAPHITE,
        va="bottom",
    )

    png, svg = _save(fig, output, "road-mask-current-maps")
    return {
        "figure_id": "road-mask-current-maps",
        "title": "Exact baseline and candidate current maps",
        "question": "What spatial evidence produced the low-barrier road-area warning?",
        "takeaway": (
            "The 47.35 percent value is the baseline-relative change in a sum over the same "
            "48 fixed cells, not an animal-use prediction."
        ),
        "alt_text": (
            "Two 20 by 12 synthetic Circuitscape current grids use the same scale. The no-action "
            "baseline appears beside the carry-forward candidate. The same road-review cells are "
            "outlined in amber. Their summed modeled current changes by 47.35 percent."
        ),
        "caption": (
            "Same synthetic landscape, numerical scale, and fixed review area. We summed modeled "
            "electrical current inside the outlined cells. We did not observe an animal moving "
            "through them."
        ),
        "forbidden_implication": "Animal routes, animal-use probability, collision locations, or harm.",
        "values": {
            "scenario_id": scenario_id,
            "baseline_portfolio_id": baseline_id,
            "candidate_portfolio_id": candidate["id"],
            "width": width,
            "height": height,
            "valid_mask_cell_count": int(valid.sum()),
            "baseline_mask_current_sum": baseline_sum,
            "candidate_mask_current_sum": portfolio_sum,
            "change_percent": change_percent,
            "shared_scale_minimum": vmin,
            "shared_scale_maximum": vmax,
            "baseline_raster_sha256": baseline_record["sha256"],
            "candidate_raster_sha256": candidate_record["sha256"],
            "review_mask_sha256": mask_record["sha256"],
        },
        "source_locations": [
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:currentRasters",
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:reviewMask",
        ],
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def _evidence_chain_figure(output: Path) -> dict[str, Any]:
    fig, ax = plt.subplots(figsize=(12, 8), layout="constrained")
    fig.patch.set_facecolor(PAPER)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 82)
    ax.axis("off")

    ax.text(
        3,
        77,
        "The agent can organize evidence. It cannot approve anything.",
        fontsize=22,
        fontweight="bold",
        va="top",
    )
    ax.text(
        3,
        70.5,
        "The actual handoff has two fail-closed exits and ends with a practitioner.",
        fontsize=13,
        color=GRAPHITE,
        va="top",
    )

    nodes = [
        (4, 49, "1", "Reviewed inputs", "Resistance, focal areas,\nactions, rules", CYAN_PALE, CYAN),
        (36, 49, "2", "Direct runs", "One Circuitscape job per\nportfolio × scenario", CYAN_PALE, CYAN),
        (68, 49, "3", "Local evidence", "QA, claims, hashes,\ncomplete ensemble", CYAN_PALE, CYAN),
        (4, 15, "4", "Optional agent", "Reads authorized aliases;\nselects known findings", MIST, GRAPHITE),
        (36, 15, "5", "Unconfirmed draft", "Local renderer supplies\nwords, numbers, links", MIST, GRAPHITE),
        (68, 15, "6", "Practitioner", "Chooses the next field\nor feasibility check", AMBER_PALE, AMBER),
    ]
    for x, y, number, title, detail, face, edge in nodes:
        _card(ax, x, y, 25, 15, face=face, edge=edge, linewidth=2)
        ax.text(x + 2, y + 11.8, number, fontsize=10, fontweight="bold", color=edge)
        ax.text(x + 5.5, y + 11.6, title, fontsize=15, fontweight="bold", color=INK)
        ax.text(x + 2, y + 7.2, detail, fontsize=10.5, color=GRAPHITE, va="top")

    arrows = [
        ((29, 56.5), (35.5, 56.5)),
        ((61, 56.5), (67.5, 56.5)),
        ((80.5, 49), (16.5, 30)),
        ((29, 22.5), (35.5, 22.5)),
        ((61, 22.5), (67.5, 22.5)),
    ]
    for start, end in arrows:
        ax.add_patch(
            FancyArrowPatch(
                start,
                end,
                arrowstyle="-|>",
                mutation_scale=16,
                linewidth=1.7,
                color=GRAPHITE,
                connectionstyle="arc3,rad=0" if start[1] == end[1] else "arc3,rad=0.08",
            )
        )

    _card(ax, 69, 37, 24, 8, face="#F7E4E8", edge=BERRY, linewidth=1.5)
    ax.text(
        81,
        41,
        "Incomplete / QA rejected\n→ no comparison",
        fontsize=9.2,
        fontweight="bold",
        color=BERRY,
        ha="center",
        va="center",
    )
    ax.add_patch(
        FancyArrowPatch(
            (81, 49),
            (81, 45.5),
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=1.5,
            color=BERRY,
        )
    )

    _card(ax, 4, 2, 30, 8, face="#F7E4E8", edge=BERRY, linewidth=1.5)
    ax.text(
        19,
        6,
        "Trace or validation fails → no draft",
        fontsize=9.7,
        fontweight="bold",
        color=BERRY,
        ha="center",
        va="center",
    )
    ax.add_patch(
        FancyArrowPatch(
            (16.5, 15),
            (18, 10),
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=1.5,
            color=BERRY,
        )
    )

    ax.text(
        92,
        6,
        "No automatic\napproval path",
        fontsize=10.5,
        color=AMBER,
        ha="right",
        fontweight="bold",
    )

    png, svg = _save(fig, output, "evidence-handoff")
    return {
        "figure_id": "evidence-handoff",
        "title": "The evidence and authority handoff",
        "question": "Where may the agent help, and who still owns the decision?",
        "takeaway": "Direct evidence and local validation precede the optional agent; a practitioner owns the next action.",
        "alt_text": (
            "A six-step flow runs from reviewed inputs to direct Circuitscape runs, local "
            "evidence, an optional read-only agent, an unconfirmed draft, and a practitioner. "
            "Side branches stop incomplete or QA-rejected evidence before comparison and stop "
            "trace or validation failures before a draft. There is no approval arrow."
        ),
        "caption": (
            "The agent sits after direct evidence and fixed local checks. It can organize only the "
            "approved evidence it receives. Failed evidence creates no comparison or draft, and only a "
            "practitioner chooses what to investigate next."
        ),
        "forbidden_implication": "Autonomous model changes, ecological judgment, or automatic approval.",
        "values": {"agent_tool_access": "READ_ONLY_ALIAS_SCOPED", "draft_state": "UNCONFIRMED"},
        "source_locations": [
            "python/ecocorridor_core/review_brief_agent.py",
            "python/ecocorridor_core/review_brief_repository.py",
            "docs/ecocorridor/review-brief-agent-evaluation.md",
        ],
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def _evidence_rail_asset(root: Path, output: Path) -> dict[str, Any]:
    source = (
        root
        / "apps/ecocorridor-desktop/tests/__screenshots__/evidence-boundary-desktop-1440.png"
    )
    if not source.is_file():
        raise FileNotFoundError("Evidence Rail screenshot is missing")
    target = output / "evidence-rail-receipt.png"
    shutil.copyfile(source, target)
    return {
        "figure_id": "evidence-rail-receipt",
        "title": "The controlling value's Evidence Rail",
        "question": "Can a reader reconstruct the controlling 1.6031 percent result?",
        "takeaway": (
            "The result resolves to a reducer, candidate and baseline direct jobs, scenario, "
            "matrix hashes, and accepted QA."
        ),
        "alt_text": (
            "EcoCorridor Evidence Rail for the controlling safeguard result. It shows the "
            "negative 1.60 percent baseline-relative change, exact candidate and baseline "
            "matrix jobs, scenario identifier, matrix SHA-256 hash, and accepted numerical QA."
        ),
        "caption": (
            "The 1.6031% value has a direct run, baseline, artifact hash, and QA "
            "disposition. That makes the calculation auditable. It does not make the ecological "
            "assumptions true."
        ),
        "forbidden_implication": "Artifact identity as proof of ecological validity.",
        "values": {
            "controlling_resistance_change_percent": -1.6031418683724497,
            "candidate_matrix_sha256_prefix": "ca751a4e",
            "qa_status": "ACCEPTED_FIXTURE",
        },
        "source_locations": [
            "apps/ecocorridor-desktop/tests/__screenshots__/evidence-boundary-desktop-1440.png",
            "benchmarks/ecocorridor/ridgeway/ui_projection.json:portfolios[carry-forward]",
        ],
        "source_image_sha256": _sha256(source),
        "png_path": str(target.relative_to(output.parents[3])),
    }


def _agent_gate_figure(output: Path) -> dict[str, Any]:
    saved = HISTORICAL_BASELINE_INPUT_TOKENS - HISTORICAL_NATIVE_INPUT_TOKENS
    fig = plt.figure(figsize=(12, 8), facecolor=PAPER)
    grid = fig.add_gridspec(
        1,
        2,
        width_ratios=[1.1, 0.9],
        left=0.12,
        right=0.94,
        top=0.72,
        bottom=0.20,
        wspace=0.34,
    )
    fig.text(
        0.12,
        0.93,
        "The agent earned a trace. It did not earn a promotion.",
        fontsize=23,
        fontweight="bold",
        va="top",
    )
    fig.text(
        0.12,
        0.85,
        "Same synthetic Ridgeway evidence, one historical provider-backed campaign",
        fontsize=12.5,
        color=GRAPHITE,
        va="top",
    )

    tokens = fig.add_subplot(grid[0, 0])
    methods = ["Full context", "Read-only agent"]
    values = [HISTORICAL_BASELINE_INPUT_TOKENS, HISTORICAL_NATIVE_INPUT_TOKENS]
    bars = tokens.barh(methods, values, color=[GRAPHITE, CYAN], height=0.52)
    tokens.invert_yaxis()
    tokens.set_xlim(0, 11800)
    tokens.set_title("Provider input tokens", fontsize=16, fontweight="bold", loc="left", pad=15)
    for bar, value in zip(bars, values, strict=True):
        tokens.text(
            value - 180,
            bar.get_y() + bar.get_height() / 2,
            f"{value:,}",
            color="white",
            ha="right",
            va="center",
            fontsize=14,
            fontweight="bold",
        )
    tokens.text(
        0,
        -0.23,
        f"Difference: {saved:,} tokens",
        transform=tokens.transAxes,
        fontsize=12,
        color=GRAPHITE,
    )
    tokens.spines[["top", "right", "left", "bottom"]].set_visible(False)
    tokens.tick_params(axis="x", bottom=False, labelbottom=False)
    tokens.tick_params(axis="y", length=0, labelsize=12)

    gate = fig.add_subplot(grid[0, 1])
    gate.barh(["Observed reduction"], [HISTORICAL_CONTEXT_REDUCTION_PERCENT], color=CYAN, height=0.38)
    gate.axvline(25, color=BERRY, linewidth=2.5, linestyle=(0, (5, 4)))
    gate.text(25, 0.34, "25% promotion gate", color=BERRY, fontsize=11.5, ha="right")
    gate.text(
        HISTORICAL_CONTEXT_REDUCTION_PERCENT + 0.6,
        0,
        f"{HISTORICAL_CONTEXT_REDUCTION_PERCENT:.2f}%",
        va="center",
        fontsize=16,
        fontweight="bold",
    )
    gate.set_xlim(0, 28)
    gate.set_title("Context reduction", fontsize=16, fontweight="bold", loc="left", pad=15)
    gate.spines[["top", "right", "left"]].set_visible(False)
    gate.tick_params(axis="y", length=0, labelsize=11)
    gate.tick_params(axis="x", labelsize=10)
    gate.grid(axis="x", color=LINE, linewidth=0.8, alpha=0.6)
    gate.set_axisbelow(True)
    fig.text(
        0.61,
        0.11,
        "Decision: keep the deterministic checklist\nas the default.",
        fontsize=11.5,
        fontweight="bold",
        color=AMBER,
        ha="left",
    )
    fig.text(
        0.61,
        0.065,
        (
            f"{HISTORICAL_RECORD_COUNT} records · "
            f"{HISTORICAL_PROVIDER_CONVERSATION_COUNT} provider conversations · "
            f"{HISTORICAL_PRE_PROVIDER_BLOCK_COUNT} pre-provider blocks"
        ),
        fontsize=9,
        color=GRAPHITE,
        ha="left",
    )

    png, svg = _save(fig, output, "agent-promotion-gate")
    return {
        "figure_id": "agent-promotion-gate",
        "title": "Historical Review Brief agent promotion gate",
        "question": "Did the historical agent reduce provider context enough to replace the deterministic default?",
        "takeaway": "No. It reduced input tokens by 2.37 percent against a predeclared 25 percent gate.",
        "alt_text": (
            "Two nearly equal bars show 10,798 full-context input tokens and 10,542 agent "
            "input tokens. A second chart shows a 2.37 percent observed reduction far short of "
            "a dashed 25 percent promotion gate."
        ),
        "caption": (
            "The agent saved 256 provider-input tokens—2.37%, not the required 25%. The "
            "deterministic checklist stayed default."
        ),
        "forbidden_implication": "General agent ranking, practitioner usefulness, or adaptive retrieval benefit.",
        "values": {
            "full_context_input_tokens": HISTORICAL_BASELINE_INPUT_TOKENS,
            "agent_input_tokens": HISTORICAL_NATIVE_INPUT_TOKENS,
            "saved_input_tokens": saved,
            "context_reduction_percent": HISTORICAL_CONTEXT_REDUCTION_PERCENT,
            "promotion_gate_percent": 25.0,
            "record_count": HISTORICAL_RECORD_COUNT,
            "provider_conversation_count": HISTORICAL_PROVIDER_CONVERSATION_COUNT,
            "pre_provider_block_count": HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
        },
        "source_locations": [
            "python/ecocorridor_core/usefulness_narrative.py:HISTORICAL_* constants",
            "docs/ecocorridor/review-brief-agent-evaluation.md",
        ],
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def render(root: Path) -> dict[str, Any]:
    root = root.resolve()
    output = root / "docs/ecocorridor/article-assets/the-model-passed"
    output.mkdir(parents=True, exist_ok=True)
    _setup()
    projection, candidate, source = _load_projection(root)
    figures = [
        _decision_figure(projection, candidate, output),
        _road_mask_figure(candidate, output),
        _current_map_figure(projection, candidate, output),
        _evidence_rail_asset(root, output),
        _evidence_chain_figure(output),
        _agent_gate_figure(output),
    ]
    for figure in figures:
        figure["png_sha256"] = _sha256(root / figure["png_path"])
        if "svg_path" in figure:
            figure["svg_sha256"] = _sha256(root / figure["svg_path"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "article_title": "The Model Passed. We Sent a Human to the Road.",
        "source_projection_path": str(source.relative_to(root)),
        "source_projection_sha256": _sha256(source),
        "candidate_id": candidate["id"],
        "license": "CC0-1.0",
        "boundary": (
            "Synthetic evidence only. Figures do not show observed animal movement, crossing "
            "use, collision reduction, population response, biodiversity benefit, approval, "
            "or a design-ready project."
        ),
        "figures": figures,
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = render(root)
    print(
        json.dumps(
            {
                "status": "RENDERED",
                "figure_count": len(result["figures"]),
                "manifest_sha256": result["manifest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
