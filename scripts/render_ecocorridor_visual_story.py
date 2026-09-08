#!/usr/bin/env python3
"""Render four article figures from retained evidence, not new experiments.

The current-raster values come from the retained Ridgeway projection. Historical
agent measurements come from the verified campaign constants. The screenshot is
copied byte-for-byte. Old decision/hypothesis files are excluded from the current
manifest but retained as historical assets.
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
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "ecocorridor-article-matplotlib")
)
os.environ.setdefault(
    "XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "ecocorridor-article-cache")
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
    png, svg = output / f"{figure_id}.png", output / f"{figure_id}.svg"
    fig.savefig(
        png, dpi=FIGURE_DPI, facecolor=PAPER, metadata={"Software": "EcoCorridor Scenario Studio"}
    )
    fig.savefig(
        svg, facecolor=PAPER, metadata={"Date": None, "Creator": "EcoCorridor Scenario Studio"}
    )
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    plt.close(fig)
    return png, svg


def _asset_paths(png: Path, svg: Path, output: Path) -> dict[str, str]:
    return {
        "png_path": str(png.relative_to(output.parents[3])),
        "svg_path": str(svg.relative_to(output.parents[3])),
    }


def _current_map_figure(
    projection: dict[str, Any], candidate: dict[str, Any], output: Path
) -> dict[str, Any]:
    scenario_id = "scenario_safeguard_guild_h1_low_barrier"
    baseline_id = projection["baselinePortfolioId"]
    by_portfolio = {
        item["portfolioId"]: item
        for item in projection["currentRasters"]
        if item["scenarioId"] == scenario_id
        and item["portfolioId"] in {baseline_id, candidate["id"]}
    }
    if set(by_portfolio) != {baseline_id, candidate["id"]}:
        raise ValueError("Ridgeway low-barrier current-map pair differs")
    baseline_record, candidate_record = by_portfolio[baseline_id], by_portfolio[candidate["id"]]
    width, height = baseline_record["width"], baseline_record["height"]
    if (candidate_record["width"], candidate_record["height"]) != (width, height):
        raise ValueError("Ridgeway current-map dimensions differ")

    def array(record: dict[str, Any]) -> np.ndarray:
        return np.asarray(
            [np.nan if value is None else float(value) for value in record["cells"]]
        ).reshape((height, width))

    baseline, portfolio = array(baseline_record), array(candidate_record)
    mask_record = projection["reviewMask"]
    if (mask_record["width"], mask_record["height"]) != (width, height):
        raise ValueError("Ridgeway review-mask dimensions differ")
    mask = np.asarray(mask_record["cells"], dtype=int).reshape((height, width))
    valid = (mask == 1) & np.isfinite(baseline) & np.isfinite(portfolio)
    if int(valid.sum()) != 48:
        raise ValueError("Ridgeway review-mask valid-cell count differs")
    baseline_sum, portfolio_sum = float(baseline[valid].sum()), float(portfolio[valid].sum())
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
    vmin, vmax = float(finite_values.min()), float(finite_values.max())
    fig = plt.figure(figsize=(6, 10), facecolor=PAPER)
    fig.text(
        0.08, 0.965, "Baseline and candidate current", fontsize=18, fontweight="bold", va="top"
    )
    fig.text(
        0.08,
        0.917,
        "Safeguard guild · low-barrier hypothesis",
        fontsize=13.5,
        va="top",
        color=GRAPHITE,
    )
    fig.text(
        0.08, 0.88, "Amber outline: the same 48 review cells", fontsize=13.5, va="top", color=AMBER
    )
    image = None
    for bottom, raster, label, total in [
        (0.505, baseline, "No-action baseline", baseline_sum),
        (0.16, portfolio, "Selected candidate", portfolio_sum),
    ]:
        ax = fig.add_axes((0.08, bottom, 0.84, 0.285))
        image = ax.imshow(raster, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
        ax.contour(mask, levels=[0.5], colors=[AMBER], linewidths=2.0)
        ax.set_title(f"{label}\nReview-mask sum: {total:.4f}", loc="left", fontsize=14, pad=10)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_edgecolor(LINE)
    assert image is not None
    colorbar = fig.colorbar(
        image, cax=fig.add_axes((0.08, 0.09, 0.84, 0.022)), orientation="horizontal"
    )
    colorbar.set_label("Modeled current · one shared scale", fontsize=13.5, labelpad=6)
    colorbar.ax.tick_params(labelsize=12)
    png, svg = _save(fig, output, "road-mask-current-maps")
    return {
        "figure_id": "road-mask-current-maps",
        "title": "Baseline and candidate current",
        "question": "What direct raster calculation produced the road-area review flag?",
        "takeaway": "The same 48-cell current sum rises 47.35 percent; this is a circuit diagnostic.",
        "alt_text": (
            "Two stacked 20 by 12 synthetic Circuitscape current grids show the no-action baseline "
            "above the selected candidate on one color scale. Amber outlines the same 48 road-review "
            "cells. Their summed current is 1.0638 at baseline and 1.5675 for the candidate, "
            "a 47.35 percent increase."
        ),
        "caption": (
            "The same 48-cell road-review mask is used in both direct current rasters, on a shared "
            "scale. Its summed current rises from 1.0638 to 1.5675 in this safeguard scenario, "
            "a 47.35% change. This is a circuit diagnostic, not animal-use evidence."
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
        **_asset_paths(png, svg, output),
    }


def _evidence_rail_asset(root: Path, output: Path) -> dict[str, Any]:
    source = (
        root / "apps/ecocorridor-desktop/tests/__screenshots__/evidence-boundary-desktop-1440.png"
    )
    if not source.is_file():
        raise FileNotFoundError("Evidence Rail screenshot is missing")
    target = output / "evidence-rail-receipt.png"
    shutil.copyfile(source, target)
    return {
        "figure_id": "evidence-rail-receipt",
        "title": "Checking the controlling result",
        "question": "Can a reader reconstruct the controlling 1.6031 percent result?",
        "takeaway": "The result resolves to its calculation, direct jobs, matrix hash, and numerical QA.",
        "alt_text": (
            "The actual EcoCorridor Evidence Rail displays a negative 1.60 percent baseline-relative "
            "resistance change and its candidate and baseline jobs, safeguard scenario, matrix "
            "SHA-256, and accepted fixture QA."
        ),
        "caption": (
            "Opening the controlling result shows the calculation, baseline and candidate jobs, "
            "a saved matrix hash, and numerical QA. These records let a reviewer check the "
            "calculation; the ecological assumptions still require separate review."
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


def _evidence_chain_figure(output: Path) -> dict[str, Any]:
    fig, ax = plt.subplots(figsize=(6, 10), facecolor=PAPER)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set(xlim=(0, 100), ylim=(0, 100))
    ax.axis("off")
    ax.text(7, 97, "Preparing a review brief", fontsize=18, fontweight="bold", va="top")
    ax.text(7, 92.5, "Historical agent workflow", fontsize=14, color=GRAPHITE, va="top")

    def card(y: float, h: float, title: str, detail: str, *, face: str = MIST) -> None:
        ax.add_patch(
            FancyBboxPatch(
                (7, y),
                86,
                h,
                boxstyle="round,pad=0.02,rounding_size=1",
                facecolor=face,
                edgecolor=LINE,
                linewidth=1.5,
            )
        )
        ax.text(10, y + h - 2, title, fontsize=15, fontweight="bold", va="top")
        ax.text(10, y + h - 5.7, detail, fontsize=13.5, va="top", linespacing=1.35, color=GRAPHITE)

    def arrow(start: float, end: float) -> None:
        ax.add_patch(
            FancyArrowPatch(
                (50, start),
                (50, end),
                arrowstyle="-|>",
                mutation_scale=15,
                linewidth=1.5,
                color=GRAPHITE,
            )
        )

    def rejection(y: float, text: str) -> None:
        ax.text(10, y, text, fontsize=12.5, color=BERRY, va="top", fontweight="bold")

    card(
        70,
        17,
        "1. Authorize direct evidence",
        "Check scope, revision, and complete\nscenario evidence.",
        face=CYAN_PALE,
    )
    rejection(73.2, "Missing or unauthorized → no draft")
    arrow(69.5, 66.4)
    card(
        47,
        19,
        "2. Provider + read-only tools",
        "The tested agent followed a fixed\ntool sequence. It returned finding\nselections, not report prose.",
    )
    rejection(50.1, "Provider or tool failure → no draft")
    arrow(46.5, 43.4)
    card(
        17,
        26,
        "3. Required release checks",
        "Read back the exact Weave trace.\nRecheck the source and permissions.\nValidate selections; render words and\nnumbers from local evidence.",
        face=CYAN_PALE,
    )
    rejection(20.2, "Any check fails → no saved draft")
    arrow(16.5, 13.4)
    card(
        2, 11, "4. Save UNCONFIRMED draft", "No portfolio approval or model edits.", face=AMBER_PALE
    )
    png, svg = _save(fig, output, "evidence-handoff")
    return {
        "figure_id": "evidence-handoff",
        "title": "Preparing a review brief",
        "question": "What must succeed before the tested agent output can become a saved draft?",
        "takeaway": "Authorized direct evidence, local validation, and exact Weave readback are all required.",
        "alt_text": (
            "A downward flow follows authorized evidence into bounded provider and read-only tool "
            "work. Required release checks combine exact Weave trace readback, current authorization, "
            "local validation, and deterministic rendering before an unconfirmed draft is saved. "
            "Missing evidence, provider or tool failure, and failed release checks each exit to no draft."
        ),
        "caption": (
            "Only a validated output with an exact Weave trace readback can become an unconfirmed "
            "draft. The historical agent used a fixed tool sequence; this diagram does not claim "
            "selective retrieval or autonomous review."
        ),
        "forbidden_implication": "Autonomous model changes, selective traversal, ecological judgment, or approval.",
        "values": {
            "agent_tool_access": "READ_ONLY_ALIAS_SCOPED",
            "draft_state": "UNCONFIRMED",
            "historical_traversal": "FIXED_TOOL_SEQUENCE",
        },
        "source_locations": [
            "python/ecocorridor_core/review_brief_agent_runtime.py:ReviewBriefSubprocessRunner.prepare",
            "python/ecocorridor_core/review_brief.py:render_review_brief",
            "python/ecocorridor_core/rpc_worker.py:_review_brief_prepare",
            "docs/ecocorridor/review-brief-agent-evaluation.md:Historical campaign interpretation",
        ],
        **_asset_paths(png, svg, output),
    }


def _agent_gate_figure(output: Path) -> dict[str, Any]:
    saved = HISTORICAL_BASELINE_INPUT_TOKENS - HISTORICAL_NATIVE_INPUT_TOKENS
    fig = plt.figure(figsize=(6, 9), facecolor=PAPER)
    fig.text(
        0.09, 0.965, "Historical input-token comparison", fontsize=17, fontweight="bold", va="top"
    )
    fig.text(
        0.09,
        0.91,
        "Totals across four teaching-portfolio cases",
        fontsize=13.5,
        color=GRAPHITE,
        va="top",
    )
    ax = fig.add_axes((0.09, 0.57, 0.82, 0.23))
    ax.barh(
        [1, 0],
        [HISTORICAL_BASELINE_INPUT_TOKENS, HISTORICAL_NATIVE_INPUT_TOKENS],
        height=0.3,
        color=[GRAPHITE, CYAN],
    )
    for y, label, value in [
        (1, "Full context", HISTORICAL_BASELINE_INPUT_TOKENS),
        (0, "Read-only agent", HISTORICAL_NATIVE_INPUT_TOKENS),
    ]:
        ax.text(0, y + 0.25, label, fontsize=14, va="bottom")
        ax.text(
            value - 250,
            y,
            f"{value:,}",
            fontsize=14,
            fontweight="bold",
            color="white",
            ha="right",
            va="center",
        )
    ax.set(xlim=(0, 12000), ylim=(-0.35, 1.75), xticks=[0, 6000, 12000], yticks=[])
    ax.set_xlabel("Provider input tokens", fontsize=13.5)
    ax.tick_params(axis="x", labelsize=12)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.set_facecolor(PAPER)
    fig.text(
        0.09,
        0.49,
        f"Difference: {saved:,} tokens ({HISTORICAL_CONTEXT_REDUCTION_PERCENT:.2f}%)",
        fontsize=14,
        fontweight="bold",
    )
    gate = fig.add_axes((0.09, 0.36, 0.82, 0.065))
    gate.barh([0], [HISTORICAL_CONTEXT_REDUCTION_PERCENT], height=0.45, color=CYAN)
    gate.axvline(25, color=BERRY, linewidth=2, linestyle=(0, (4, 3)))
    gate.text(25, 0.8, "25% gate", color=BERRY, fontsize=13.5, ha="right")
    gate.text(HISTORICAL_CONTEXT_REDUCTION_PERCENT + 0.7, 0, "2.37%", fontsize=14, va="center")
    gate.set(xlim=(0, 28), ylim=(-0.65, 0.65), yticks=[], xticks=[0, 10, 20])
    gate.tick_params(axis="x", labelsize=12)
    gate.spines[["top", "right", "left"]].set_visible(False)
    gate.set_xlabel("Input-token reduction (%)", fontsize=13.5)
    gate.set_facecolor(PAPER)
    fig.text(0.09, 0.245, "Comparison limitations", fontsize=14, fontweight="bold", color=AMBER)
    fig.text(
        0.09,
        0.205,
        "Fixed agent traversal; the full-context\ncomparator received expected-answer hints.",
        fontsize=13.5,
        color=GRAPHITE,
        va="top",
        linespacing=1.4,
    )
    fig.text(
        0.09, 0.075, "Deterministic checklist remains default.", fontsize=14, fontweight="bold"
    )
    png, svg = _save(fig, output, "agent-promotion-gate")
    return {
        "figure_id": "agent-promotion-gate",
        "title": "Historical input-token comparison",
        "question": "Did this historical agent clear the input-token reduction gate?",
        "takeaway": "Its 2.37 percent reduction missed the 25 percent gate; the comparator was also confounded.",
        "alt_text": (
            "Across four teaching-portfolio cases, horizontal bars show 10,798 full-context input "
            "tokens and 10,542 agent input tokens. "
            "A reduction chart places the observed 2.37 percent beside the 25 percent promotion "
            "gate. Notes identify fixed agent traversal and comparator answer hints; the "
            "deterministic checklist remains default."
        ),
        "caption": (
            "Across the four teaching-portfolio cases, the historical agent used 256 fewer input "
            "tokens, a 2.37% reduction against a 25% "
            "gate. Fixed traversal and answer-contract hints in the full-context comparator limit "
            "the comparison. The deterministic checklist stayed default."
        ),
        "forbidden_implication": "Fair agent ranking, practitioner usefulness, or adaptive retrieval benefit.",
        "values": {
            "full_context_input_tokens": HISTORICAL_BASELINE_INPUT_TOKENS,
            "agent_input_tokens": HISTORICAL_NATIVE_INPUT_TOKENS,
            "saved_input_tokens": saved,
            "context_reduction_percent": HISTORICAL_CONTEXT_REDUCTION_PERCENT,
            "promotion_gate_percent": 25.0,
            "record_count": HISTORICAL_RECORD_COUNT,
            "token_comparison_teaching_case_count": 4,
            "provider_conversation_count": HISTORICAL_PROVIDER_CONVERSATION_COUNT,
            "pre_provider_block_count": HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
        },
        "source_locations": [
            "python/ecocorridor_core/usefulness_narrative.py:HISTORICAL_* constants",
            "docs/ecocorridor/review-brief-agent-evaluation.md:Historical campaign interpretation",
        ],
        **_asset_paths(png, svg, output),
    }


def render(root: Path) -> dict[str, Any]:
    root = root.resolve()
    output = root / "docs/ecocorridor/article-assets/the-model-passed"
    output.mkdir(parents=True, exist_ok=True)
    _setup()
    projection, candidate, source = _load_projection(root)
    figures = [
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
        "article_title": "Why I Built EcoCorridor",
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
    (output / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main() -> int:
    result = render(Path(__file__).resolve().parents[1])
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
