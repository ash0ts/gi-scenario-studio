#!/usr/bin/env python3
"""Build publication tables, claim evidence, and figures from the exact benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from gi_scenario_core.benchmark import load_benchmark
from gi_scenario_core.search import nondominated_indices
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D

INK = "#17313A"
PAPER = "#F5F7F6"
TEAL = "#08766F"
BLUE = "#2F6F9F"
OCHRE = "#A96315"
RED = "#B43D47"
GRAY = "#91A0A5"
LIGHT = "#DCE5E4"

METHOD_LABELS = {
    "random": "Random",
    "sobol": "Scrambled Sobol",
    "nsga2": "Integer NSGA-II",
    "sobol_extratrees": "Sobol + ExtraTrees",
}
METHOD_STYLES = {
    "random": (GRAY, "o", ":"),
    "sobol": (BLUE, "s", "--"),
    "nsga2": (OCHRE, "D", "-."),
    "sobol_extratrees": (TEAL, "^", "-"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _write_csv(path: Path, columns: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.csv")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _font(root: Path, *, heading: bool = False, mono: bool = False) -> FontProperties:
    fonts = root / "media" / "gi-scenario-film" / "assets" / "fonts"
    if mono:
        name = "IBMPlexMono-Medium.ttf"
    elif heading:
        name = "IBMPlexSansCondensed-SemiBold.ttf"
    else:
        name = "AtkinsonHyperlegible-Regular.ttf"
    return FontProperties(fname=fonts / name)


def _base_figure(root: Path, title: str, subtitle: str):
    figure = plt.figure(figsize=(16, 9), dpi=120, facecolor=PAPER)
    figure.text(
        0.055, 0.935, title, color=INK, fontsize=25, fontproperties=_font(root, heading=True)
    )
    figure.text(0.055, 0.895, subtitle, color=INK, fontsize=13, fontproperties=_font(root))
    figure.text(
        0.055,
        0.025,
        "Cedar Trace synthetic benchmark · exact SWMM 5.2.4 fixture results · not field evidence",
        color=INK,
        alpha=0.72,
        fontsize=10,
        fontproperties=_font(root),
    )
    return figure


def _save(figure, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, facecolor=PAPER, bbox_inches=None)
    plt.close(figure)


def _exact_frontier(rows: list[dict[str, Any]]) -> set[str]:
    feasible = [row for row in rows if row["design_id"] != "baseline" and row["feasible"]]
    directions = {
        "present_value_cost_thousand_usd": "minimize",
        "outfall_volume_reduction_percent": "maximize",
    }
    points = [
        {
            "present_value_cost_thousand_usd": row["present_value_cost_thousand_usd"],
            "outfall_volume_reduction_percent": row["outfall_volume_reduction_percent"],
        }
        for row in feasible
    ]
    return {feasible[index]["design_id"] for index in nondominated_indices(points, directions)}


def _plot_decision_landscape(
    root: Path,
    rows: list[dict[str, Any]],
    frontier: set[str],
    guided_ids: set[str],
    output: Path,
) -> None:
    figure = _base_figure(
        root,
        "The complete modeled decision landscape",
        "699 alternatives; the two visible axes are useful, but the peak guardrail changes the shortlist.",
    )
    axis = figure.add_axes((0.08, 0.13, 0.66, 0.70), facecolor=PAPER)
    alternatives = [row for row in rows if row["design_id"] != "baseline"]
    ordinary = [row for row in alternatives if not row["adverse"] and not row["feasible"]]
    adverse = [row for row in alternatives if row["adverse"]]
    feasible = [row for row in alternatives if row["feasible"]]

    axis.scatter(
        [row["present_value_cost_thousand_usd"] for row in ordinary],
        [row["outfall_volume_reduction_percent"] for row in ordinary],
        s=24,
        c=GRAY,
        alpha=0.48,
        marker="o",
        linewidths=0,
        label=f"Other exact ({len(ordinary)})",
    )
    axis.scatter(
        [row["present_value_cost_thousand_usd"] for row in adverse],
        [row["outfall_volume_reduction_percent"] for row in adverse],
        s=30,
        facecolors="none",
        edgecolors=RED,
        alpha=0.52,
        marker="^",
        linewidths=0.8,
        label=f"Any peak increase ({len(adverse)})",
    )
    axis.scatter(
        [row["present_value_cost_thousand_usd"] for row in feasible],
        [row["outfall_volume_reduction_percent"] for row in feasible],
        s=80,
        c=TEAL,
        marker="o",
        edgecolors=PAPER,
        linewidths=1.2,
        label=f"Pass all rules ({len(feasible)})",
        zorder=4,
    )
    frontier_rows = [row for row in feasible if row["design_id"] in frontier]
    axis.scatter(
        [row["present_value_cost_thousand_usd"] for row in frontier_rows],
        [row["outfall_volume_reduction_percent"] for row in frontier_rows],
        s=175,
        facecolors="none",
        edgecolors=INK,
        marker="s",
        linewidths=2.0,
        label=f"Feasible frontier ({len(frontier_rows)})",
        zorder=5,
    )
    guided = [row for row in alternatives if row["design_id"] in guided_ids]
    axis.scatter(
        [row["present_value_cost_thousand_usd"] for row in guided],
        [row["outfall_volume_reduction_percent"] for row in guided],
        s=54,
        facecolors="none",
        edgecolors=BLUE,
        marker="o",
        linewidths=0.65,
        alpha=0.35,
        label="Inside guided replay (220)",
        zorder=2,
    )
    axis.axhline(6.0, color=OCHRE, linewidth=1.6, linestyle="--")
    axis.axvline(200.0, color=OCHRE, linewidth=1.6, linestyle="--")
    axis.text(
        5, 6.15, "6% modeled volume target", color=OCHRE, fontsize=11, fontproperties=_font(root)
    )
    axis.text(
        203,
        0.2,
        "$200k screening cap",
        color=OCHRE,
        fontsize=11,
        fontproperties=_font(root),
        rotation=90,
    )
    axis.set_xlabel(
        "Synthetic 30-year present-value cost (thousand USD)",
        color=INK,
        fontsize=13,
        fontproperties=_font(root),
    )
    axis.set_ylabel(
        "Aggregate OUT1 event-volume reduction (%)",
        color=INK,
        fontsize=13,
        fontproperties=_font(root),
    )
    axis.set_xlim(-5, 380)
    axis.set_ylim(-0.25, 9.35)
    axis.grid(color=LIGHT, linewidth=0.8, zorder=0)
    axis.tick_params(colors=INK, labelsize=11)
    for spine in axis.spines.values():
        spine.set_color(INK)
        spine.set_alpha(0.35)
    legend = axis.legend(
        loc="upper left",
        bbox_to_anchor=(1.03, 1.0),
        frameon=False,
        fontsize=10,
        labelspacing=1.2,
    )
    for text in legend.get_texts():
        text.set_fontproperties(_font(root))
        text.set_color(INK)
    figure.text(
        0.77,
        0.29,
        "A point can be\nnondominated on cost\nand volume—and still fail\na required storm check.",
        color=INK,
        fontsize=13,
        linespacing=1.35,
        fontproperties=_font(root, heading=True),
    )
    _save(figure, output)


def _plot_rule_filter(root: Path, counts: dict[str, int], output: Path) -> None:
    figure = _base_figure(
        root,
        "A two-axis shortlist is nearly four times too large",
        "Apply the declared rules in sequence to the 699 nonbaseline alternatives.",
    )
    axis = figure.add_axes((0.10, 0.18, 0.82, 0.62), facecolor=PAPER)
    stages = [
        ("Directly simulated", counts["alternatives"], GRAY),
        ("Meet ≥6% volume target", counts["volume"], BLUE),
        ("Also meet ≤$200k cap", counts["volume_cost"], OCHRE),
        ("Also avoid every peak increase", counts["all_rules"], TEAL),
    ]
    y_values = list(reversed(range(len(stages))))
    for y, (label, value, color) in zip(y_values, stages, strict=True):
        axis.barh(y, value, height=0.54, color=color, alpha=0.9)
        axis.text(
            -10,
            y,
            label,
            ha="right",
            va="center",
            color=INK,
            fontsize=14,
            fontproperties=_font(root),
        )
        axis.text(
            value + 8,
            y,
            f"{value}",
            ha="left",
            va="center",
            color=INK,
            fontsize=18,
            fontproperties=_font(root, heading=True),
        )
    axis.set_xlim(-145, 735)
    axis.set_ylim(-0.7, 3.7)
    axis.axis("off")
    figure.text(
        0.55,
        0.49,
        "46 pass cost + volume",
        color=INK,
        fontsize=18,
        fontproperties=_font(root, heading=True),
    )
    figure.text(
        0.55,
        0.435,
        "34 of those still increase a selected-storm peak",
        color=RED,
        fontsize=16,
        fontproperties=_font(root),
    )
    figure.text(
        0.55,
        0.34,
        "12 survive the complete teaching contract",
        color=TEAL,
        fontsize=19,
        fontproperties=_font(root, heading=True),
    )
    figure.text(
        0.10,
        0.09,
        "Execution QA did not remove a design: all 699 alternatives passed the fixture's numerical checks.",
        color=INK,
        fontsize=12,
        fontproperties=_font(root),
    )
    _save(figure, output)


def _plot_storm_atlas(root: Path, storm_counts: list[tuple[str, int]], output: Path) -> None:
    figure = _base_figure(
        root,
        "The deepest storm was not the most revealing guardrail",
        "Count of alternatives whose exact OUT1 peak exceeded the matching baseline in each synthetic event.",
    )
    axis = figure.add_axes((0.10, 0.18, 0.70, 0.62), facecolor=PAPER)
    labels = [label for label, _ in storm_counts]
    values = [value for _, value in storm_counts]
    colors = [BLUE, GRAY, RED, OCHRE]
    bars = axis.bar(range(len(values)), values, color=colors, width=0.62)
    for bar, value in zip(bars, values, strict=True):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value + 10,
            str(value),
            ha="center",
            color=INK,
            fontsize=19,
            fontproperties=_font(root, heading=True),
        )
    axis.set_xticks(range(len(labels)), labels)
    axis.set_ylabel(
        "Alternatives with a peak increase", color=INK, fontsize=13, fontproperties=_font(root)
    )
    axis.set_ylim(0, 480)
    axis.grid(axis="y", color=LIGHT, linewidth=0.8)
    axis.tick_params(colors=INK, labelsize=11)
    for label in axis.get_xticklabels():
        label.set_fontproperties(_font(root))
    for spine in axis.spines.values():
        spine.set_color(INK)
        spine.set_alpha(0.35)
    figure.text(0.84, 0.63, "421", color=RED, fontsize=35, fontproperties=_font(root, heading=True))
    figure.text(
        0.84,
        0.565,
        "unique alternatives\nviolate at least one storm",
        color=INK,
        fontsize=14,
        fontproperties=_font(root),
    )
    figure.text(
        0.84, 0.42, "468", color=OCHRE, fontsize=35, fontproperties=_font(root, heading=True)
    )
    figure.text(
        0.84,
        0.355,
        "storm–design violations\n47 alternatives fail twice",
        color=INK,
        fontsize=14,
        fontproperties=_font(root),
    )
    figure.text(
        0.10,
        0.09,
        "Strict teaching rule: any positive reported peak delta is adverse; a real project must declare precision and tolerance policy.",
        color=INK,
        fontsize=11,
        fontproperties=_font(root),
    )
    _save(figure, output)


def _plot_guided_coverage(root: Path, coverage: list[tuple[str, float, str]], output: Path) -> None:
    figure = _base_figure(
        root,
        "The guided replay teaches the workflow; it does not exhaust the answer space",
        "Coverage of the complete nonbaseline universe by the deterministic 220-alternative teaching subset.",
    )
    axis = figure.add_axes((0.11, 0.19, 0.78, 0.62), facecolor=PAPER)
    labels = [item[0] for item in coverage]
    values = [item[1] for item in coverage]
    notes = [item[2] for item in coverage]
    colors = [BLUE, TEAL, RED, INK]
    y = list(reversed(range(len(values))))
    for position, label, value, note, color in zip(y, labels, values, notes, colors, strict=True):
        axis.barh(position, 100, height=0.52, color=LIGHT)
        axis.barh(position, value, height=0.52, color=color)
        axis.text(
            -2,
            position,
            label,
            ha="right",
            va="center",
            color=INK,
            fontsize=14,
            fontproperties=_font(root),
        )
        axis.text(
            value + 1.5,
            position,
            f"{value:.1f}%  {note}",
            ha="left",
            va="center",
            color=INK,
            fontsize=14,
            fontproperties=_font(root),
        )
    axis.set_xlim(-24, 112)
    axis.set_ylim(-0.7, 3.7)
    axis.axis("off")
    figure.text(
        0.11,
        0.09,
        "None of the four exact feasible-frontier alternatives appears in the guided 220. ALT-207 can clear the teaching rules without becoming a global or exhaustive winner.",
        color=INK,
        fontsize=12,
        fontproperties=_font(root),
    )
    _save(figure, output)


def _aggregate_campaign(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["method"], int(record["budget"]))].append(record)
    aggregated: list[dict[str, Any]] = []
    metrics = (
        "hypervolume_ratio",
        "epsilon_indicator",
        "feasible_recall",
        "adverse_recall",
        "frontier_recall",
    )
    for (method, budget), group in grouped.items():
        row: dict[str, Any] = {
            "method": method,
            "method_label": METHOD_LABELS[method],
            "design_budget": budget,
            "exact_job_equivalent": budget * 4,
            "seed_count": len(group),
        }
        for metric in metrics:
            values = [record["summary"][metric] for record in group]
            row[f"{metric}_p10"] = _quantile(values, 0.10)
            row[f"{metric}_median"] = _quantile(values, 0.50)
            row[f"{metric}_p90"] = _quantile(values, 0.90)
        aggregated.append(row)
    return sorted(aggregated, key=lambda row: (row["design_budget"], row["method"]))


def _plot_search_efficiency(root: Path, aggregated: list[dict[str, Any]], output: Path) -> None:
    figure = _base_figure(
        root,
        "Search quality improves—but not smoothly, and not equally",
        "Median across ten seeds; shaded bands show the 10th–90th percentile. Every requested design costs four exact-job equivalents.",
    )
    axes = [
        figure.add_axes((0.065, 0.18, 0.28, 0.62), facecolor=PAPER),
        figure.add_axes((0.385, 0.18, 0.28, 0.62), facecolor=PAPER),
        figure.add_axes((0.705, 0.18, 0.28, 0.62), facecolor=PAPER),
    ]
    panels = [
        ("hypervolume_ratio", "Exact-frontier hypervolume", 1.0),
        ("feasible_recall", "Feasible alternatives found", 1.0),
        ("adverse_recall", "Adverse alternatives found", 1.0),
    ]
    for axis, (metric, title, maximum) in zip(axes, panels, strict=True):
        for method in METHOD_LABELS:
            rows = sorted(
                [row for row in aggregated if row["method"] == method],
                key=lambda row: row["exact_job_equivalent"],
            )
            x = [row["exact_job_equivalent"] for row in rows]
            medians = [row[f"{metric}_median"] for row in rows]
            lower = [row[f"{metric}_p10"] for row in rows]
            upper = [row[f"{metric}_p90"] for row in rows]
            color, marker, linestyle = METHOD_STYLES[method]
            axis.plot(
                x,
                medians,
                color=color,
                marker=marker,
                linestyle=linestyle,
                linewidth=2.3,
                markersize=7,
            )
            axis.fill_between(x, lower, upper, color=color, alpha=0.12)
        axis.set_title(
            title, color=INK, fontsize=15, fontproperties=_font(root, heading=True), pad=12
        )
        axis.set_xticks([256, 512, 1024])
        axis.set_ylim(-0.04, maximum + 0.04)
        axis.grid(color=LIGHT, linewidth=0.8)
        axis.tick_params(colors=INK, labelsize=10)
        axis.set_xlabel(
            "Exact SWMM-job equivalent", color=INK, fontsize=11, fontproperties=_font(root)
        )
        for spine in axis.spines.values():
            spine.set_color(INK)
            spine.set_alpha(0.35)
    axes[0].set_ylabel(
        "Fraction of exhaustive truth", color=INK, fontsize=11, fontproperties=_font(root)
    )
    handles = [
        Line2D(
            [0],
            [0],
            color=METHOD_STYLES[method][0],
            marker=METHOD_STYLES[method][1],
            linestyle=METHOD_STYLES[method][2],
            linewidth=2,
            label=label,
        )
        for method, label in METHOD_LABELS.items()
    ]
    legend = figure.legend(
        handles=handles,
        loc="lower center",
        ncol=4,
        bbox_to_anchor=(0.5, 0.07),
        frameon=False,
        fontsize=11,
    )
    for text in legend.get_texts():
        text.set_fontproperties(_font(root))
        text.set_color(INK)
    _save(figure, output)


def _release_gate(
    aggregated: list[dict[str, Any]], adverse_count: int
) -> tuple[list[dict[str, Any]], bool]:
    by_key = {(row["method"], row["design_budget"]): row for row in aggregated}
    improvements: dict[int, float] = {}
    lower_tail_gaps: dict[int, float] = {}
    feasible_gaps: dict[int, float] = {}
    for budget in (64, 128, 256):
        sobol = by_key[("sobol", budget)]
        extra = by_key[("sobol_extratrees", budget)]
        improvements[budget] = (
            extra["hypervolume_ratio_median"] / sobol["hypervolume_ratio_median"] - 1
            if sobol["hypervolume_ratio_median"]
            else 0.0
        )
        lower_tail_gaps[budget] = extra["hypervolume_ratio_p10"] - sobol["hypervolume_ratio_p10"]
        feasible_gaps[budget] = extra["feasible_recall_median"] - sobol["feasible_recall_median"]

    improved_budgets = [budget for budget, value in improvements.items() if value >= 0.05]
    tail_pass = all(value >= -0.02 for value in lower_tail_gaps.values())
    feasible_pass = all(value >= 0 for value in feasible_gaps.values())
    maximum = by_key[("sobol_extratrees", 256)]
    adverse_pass = maximum["adverse_recall_median"] >= 0.95
    theoretical_maximum = min(1.0, 256 / adverse_count)
    gates = [
        {
            "criterion": "Median hypervolume ≥5% above Sobol at two budgets",
            "observed": f"Passed at {len(improved_budgets)} of 3 budgets ({', '.join(map(str, improved_budgets)) or 'none'})",
            "passed": len(improved_budgets) >= 2,
        },
        {
            "criterion": "10th-percentile hypervolume no more than 0.02 below Sobol",
            "observed": f"Worst gap {min(lower_tail_gaps.values()):+.3f}",
            "passed": tail_pass,
        },
        {
            "criterion": "Median feasible recall is not worse than Sobol",
            "observed": f"Smallest gap {min(feasible_gaps.values()):+.3f}",
            "passed": feasible_pass,
        },
        {
            "criterion": "Adverse recall ≥95% at maximum budget",
            "observed": (
                f"Median {maximum['adverse_recall_median']:.1%}; mathematical ceiling "
                f"{theoretical_maximum:.1%} with 256 requests"
            ),
            "passed": adverse_pass,
        },
        {
            "criterion": "Evidence spans ≥3 public networks and ten seeds",
            "observed": "One synthetic network; ten seeds",
            "passed": False,
        },
    ]
    return gates, all(item["passed"] for item in gates)


def _plot_release_gate(root: Path, gates: list[dict[str, Any]], output: Path) -> None:
    figure = _base_figure(
        root,
        "The adaptive proposer has not earned production use",
        "Audit against the preregistered release conditions; failures remain visible instead of being averaged away.",
    )
    axis = figure.add_axes((0.055, 0.13, 0.89, 0.70), facecolor=PAPER)
    axis.axis("off")
    y = 0.92
    for gate in gates:
        color = TEAL if gate["passed"] else RED
        label = "PASS" if gate["passed"] else "NOT MET"
        axis.add_patch(
            plt.Rectangle(
                (0.0, y - 0.075), 0.12, 0.09, color=color, alpha=0.95, transform=axis.transAxes
            )
        )
        axis.text(
            0.06,
            y - 0.03,
            label,
            ha="center",
            va="center",
            color=PAPER,
            fontsize=12,
            fontproperties=_font(root, heading=True),
            transform=axis.transAxes,
        )
        axis.text(
            0.15,
            y,
            gate["criterion"],
            ha="left",
            va="center",
            color=INK,
            fontsize=15,
            fontproperties=_font(root, heading=True),
            transform=axis.transAxes,
        )
        axis.text(
            0.15,
            y - 0.052,
            gate["observed"],
            ha="left",
            va="center",
            color=INK,
            alpha=0.82,
            fontsize=12,
            fontproperties=_font(root),
            transform=axis.transAxes,
        )
        axis.plot(
            [0, 1], [y - 0.105, y - 0.105], color=LIGHT, linewidth=1.0, transform=axis.transAxes
        )
        y -= 0.17
    figure.text(
        0.055,
        0.07,
        "Decision: keep adaptive ML experimental. Sobol remains the production search until a corrected, multi-network gate is passed.",
        color=INK,
        fontsize=13,
        fontproperties=_font(root, heading=True),
    )
    _save(figure, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("benchmarks/exhaustive_exact/dataset.json")
    )
    parser.add_argument(
        "--guided", type=Path, default=Path("benchmarks/guided_exact/manifest.json")
    )
    parser.add_argument(
        "--campaign", type=Path, default=Path("dist/search-benchmark/campaign-manifest.json")
    )
    parser.add_argument("--output-root", type=Path, default=Path("dist/benchmark-publication"))
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    payload, _ = load_benchmark(arguments.dataset)
    raw_rows = json.loads(arguments.dataset.read_text(encoding="utf-8"))["rows"]
    campaign = json.loads(arguments.campaign.read_text(encoding="utf-8"))
    guided = json.loads(arguments.guided.read_text(encoding="utf-8"))
    if campaign["dataset_sha256"] != payload["dataset_sha256"]:
        raise RuntimeError("campaign and exact dataset hashes do not match")
    if campaign["run_count"] != 120:
        raise RuntimeError("publication requires the complete 120-run campaign")

    guided_ids = {item["design_id"] for item in guided["search"]["design_order"]}
    frontier = _exact_frontier(raw_rows)
    alternatives = [row for row in raw_rows if row["design_id"] != "baseline"]
    volume = [row for row in alternatives if row["outfall_volume_reduction_percent"] >= 6]
    volume_cost = [row for row in volume if row["present_value_cost_thousand_usd"] <= 200]
    feasible = [row for row in alternatives if row["feasible"]]
    adverse = [row for row in alternatives if row["adverse"]]
    counts = {
        "alternatives": len(alternatives),
        "volume": len(volume),
        "volume_cost": len(volume_cost),
        "all_rules": len(feasible),
        "adverse": len(adverse),
        "frontier": len(frontier),
    }
    if counts != {
        "alternatives": 699,
        "volume": 297,
        "volume_cost": 46,
        "all_rules": 12,
        "adverse": 421,
        "frontier": 4,
    }:
        raise RuntimeError(f"authoritative exact counts drifted: {counts}")

    baseline = next(row for row in raw_rows if row["design_id"] == "baseline")
    baseline_peaks = {run["storm_id"]: run["outfall_peak_cfs"] for run in baseline["runs"]}
    storm_labels = {
        "synthetic_balanced_1in": "Balanced\n1 inch",
        "synthetic_front_loaded_2in": "Front-loaded\n2 inch",
        "synthetic_long_2_5in": "Long-duration\n2.5 inch",
        "synthetic_stress_4in": "Symmetric stress\n4 inch",
    }
    storm_violation_counts = {storm_id: 0 for storm_id in storm_labels}
    storm_rows: list[dict[str, Any]] = []
    exact_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        exact_rows.append(
            {
                "design_id": row["design_id"],
                "is_baseline": row["design_id"] == "baseline",
                "bio_units": row["vector"][0],
                "permeable_pavement_units": row["vector"][1],
                "rain_garden_units": row["vector"][2],
                "rain_barrel_groups": row["vector"][3],
                "cost_thousand_usd": row["present_value_cost_thousand_usd"],
                "volume_reduction_percent": row["outfall_volume_reduction_percent"],
                "peak_guardrail_pass": row["peak_guardrail_pass"],
                "qa_pass": row["qa_pass"],
                "teaching_rule_feasible": row["feasible"],
                "adverse_any_storm": row["adverse"],
                "exact_feasible_frontier": row["design_id"] in frontier,
                "in_guided_220": row["design_id"] in guided_ids,
            }
        )
        for run in row["runs"]:
            delta = run["outfall_peak_cfs"] - baseline_peaks[run["storm_id"]]
            if row["design_id"] != "baseline" and delta > 0:
                storm_violation_counts[run["storm_id"]] += 1
            storm_rows.append(
                {
                    "design_id": row["design_id"],
                    "storm_id": run["storm_id"],
                    "storm_label": storm_labels[run["storm_id"]].replace("\n", " "),
                    "outfall_peak_cfs": run["outfall_peak_cfs"],
                    "baseline_peak_cfs": baseline_peaks[run["storm_id"]],
                    "peak_delta_cfs": round(delta, 9),
                    "outfall_volume_million_gallons": run["outfall_volume_million_gallons"],
                    "qa_status": run["qa_status"],
                    "run_id": run["run_id"],
                }
            )
    expected_storm_counts = {
        "synthetic_balanced_1in": 1,
        "synthetic_front_loaded_2in": 0,
        "synthetic_long_2_5in": 420,
        "synthetic_stress_4in": 47,
    }
    if storm_violation_counts != expected_storm_counts:
        raise RuntimeError(f"storm guardrail counts drifted: {storm_violation_counts}")

    aggregated = _aggregate_campaign(campaign["records"])
    gates, production_ready = _release_gate(aggregated, len(adverse))
    if production_ready:
        raise RuntimeError("unexpected release-gate pass requires explicit practitioner review")

    guided_feasible = len({row["design_id"] for row in feasible} & guided_ids)
    guided_adverse = len({row["design_id"] for row in adverse} & guided_ids)
    guided_frontier = len(frontier & guided_ids)
    coverage = [
        ("All alternatives", 100 * len(guided_ids) / len(alternatives), "220 / 699"),
        (
            "Rule-feasible",
            100 * guided_feasible / len(feasible),
            f"{guided_feasible} / {len(feasible)}",
        ),
        ("Adverse", 100 * guided_adverse / len(adverse), f"{guided_adverse} / {len(adverse)}"),
        (
            "Exact feasible frontier",
            100 * guided_frontier / len(frontier),
            f"{guided_frontier} / {len(frontier)}",
        ),
    ]

    figures = arguments.output_root / "figures"
    figure_specs = [
        ("exact_decision_landscape", "01_exact_decision_landscape.png"),
        ("rule_filter", "02_rule_filter.png"),
        ("storm_guardrail_atlas", "03_storm_guardrail_atlas.png"),
        ("guided_coverage", "04_guided_coverage.png"),
        ("search_efficiency", "05_search_efficiency.png"),
        ("release_gate_audit", "06_release_gate_audit.png"),
    ]
    _plot_decision_landscape(root, raw_rows, frontier, guided_ids, figures / figure_specs[0][1])
    _plot_rule_filter(root, counts, figures / figure_specs[1][1])
    _plot_storm_atlas(
        root,
        [(storm_labels[key], storm_violation_counts[key]) for key in storm_labels],
        figures / figure_specs[2][1],
    )
    _plot_guided_coverage(root, coverage, figures / figure_specs[3][1])
    _plot_search_efficiency(root, aggregated, figures / figure_specs[4][1])
    _plot_release_gate(root, gates, figures / figure_specs[5][1])

    campaign_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    for record in campaign["records"]:
        summary = record["summary"]
        campaign_rows.append(
            {
                "method": record["method"],
                "method_label": METHOD_LABELS[record["method"]],
                "seed": record["seed"],
                "design_budget": record["budget"],
                "exact_job_equivalent": record["exact_job_equivalent"],
                "hypervolume_ratio": summary["hypervolume_ratio"],
                "epsilon_indicator": summary["epsilon_indicator"],
                "frontier_recall": summary["frontier_recall"],
                "feasible_recall": summary["feasible_recall"],
                "adverse_recall": summary["adverse_recall"],
                "exploration_designs": record["selection_counts"]["exploration"],
                "adaptive_designs": record["selection_counts"]["adaptive"],
                "campaign_sha256": record["campaign_sha256"],
            }
        )
        result = json.loads(
            (arguments.campaign.parent / record["relative_path"]).read_text(encoding="utf-8")
        )
        for item in result["history"]:
            history_rows.append(
                {
                    "method": record["method"],
                    "method_label": METHOD_LABELS[record["method"]],
                    "seed": record["seed"],
                    "configured_design_budget": record["budget"],
                    **item,
                }
            )

    tables = arguments.output_root / "tables"
    _write_csv(tables / "exact_alternatives.csv", list(exact_rows[0]), exact_rows)
    _write_csv(tables / "storm_results.csv", list(storm_rows[0]), storm_rows)
    _write_csv(tables / "campaign_runs.csv", list(campaign_rows[0]), campaign_rows)
    _write_csv(tables / "campaign_history.csv", list(history_rows[0]), history_rows)
    _write_csv(tables / "campaign_aggregates.csv", list(aggregated[0]), aggregated)
    _write_csv(tables / "release_gate.csv", ["criterion", "observed", "passed"], gates)

    files = []
    for path in sorted(arguments.output_root.rglob("*")):
        if path.is_file() and path.name != "publication-manifest.json":
            files.append(
                {
                    "relative_path": path.relative_to(arguments.output_root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    publication = {
        "schema_version": "2.0",
        "publication_id": "cedar-trace-benchmark-publication-v2",
        "dataset_id": payload["dataset_id"],
        "dataset_sha256": payload["dataset_sha256"],
        "campaign_manifest_sha256": campaign["manifest_sha256"],
        "notice": payload["notice"],
        "claims": {
            **counts,
            "storm_violation_counts": storm_violation_counts,
            "guided_feasible": guided_feasible,
            "guided_adverse": guided_adverse,
            "guided_frontier": guided_frontier,
            "search_run_count": campaign["run_count"],
            "seed_count": len(campaign["seeds"]),
            "design_budgets": campaign["design_budgets"],
            "exact_job_equivalent_budgets": campaign["exact_job_equivalent_budgets"],
            "hydraulic_reruns_performed": campaign["hydraulic_reruns_performed"],
            "adaptive_production_gate_passed": production_ready,
        },
        "aggregates": aggregated,
        "release_gate": gates,
        "figures": [
            {
                "key": key,
                "relative_path": f"figures/{filename}",
                "sha256": _sha256(figures / filename),
            }
            for key, filename in figure_specs
        ],
        "files": files,
    }
    _write_json_atomic(arguments.output_root / "publication-manifest.json", publication)
    print(arguments.output_root / "publication-manifest.json")


if __name__ == "__main__":
    main()
