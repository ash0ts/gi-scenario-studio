#!/usr/bin/env python3
"""Check article evidence, accessible assets, and freshness of inspected previews.

This intentionally has no prose-length, exact-closing, or fixed-figure-count gate.
Editorial clarity and visual composition still require human review.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path


ARTICLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ARTICLE_ROOT.parents[1]
SUMMARY = REPO_ROOT / "media/gi-scenario-film/dist/evidence-guide-r4c/article-summary.json"
GUIDED = REPO_ROOT / "apps/desktop/src/data/guidedFixture.json"
MANIFEST = ARTICLE_ROOT / "dist/evidence-manifest.json"
MODEL_IDS = ("ibm-granite/granite-4.1-8b", "Qwen/Qwen3-30B-A3B-Instruct-2507")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numbers(value: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"\d[\d,]*(?:\.\d+)?", value)]


class ArticleMarkup(HTMLParser):
    """Read real table semantics and picture/caption references from Markdown HTML."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.tables: dict = {}
        self.figures: dict = {}
        self.element_order: list[str] = []
        self.table = self.row = self.cell = self.figure = None
        self.in_caption = False
        self.in_figcaption = False
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        if tag == "table":
            self.table = {"caption": "", "rows": []}
            self.tables[attr["id"]] = self.table
            self.element_order.append(attr["id"])
        elif tag == "caption" and self.table is not None:
            self.in_caption = True
        elif tag == "tr" and self.table is not None:
            self.row = {"attrs": attr, "cells": []}
            self.table["rows"].append(self.row)
        elif tag in ("th", "td") and self.row is not None:
            self.cell = {"tag": tag, "scope": attr.get("scope"), "text": ""}
            self.row["cells"].append(self.cell)
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += " "
        elif tag == "figure":
            self.figure = {"caption": "", "sources": [], "images": []}
            self.figures[attr["id"]] = self.figure
            self.element_order.append(attr["id"])
        elif tag == "source" and self.figure is not None:
            self.figure["sources"].append(attr["srcset"])
        elif tag == "img" and self.figure is not None:
            self.figure["images"].append(attr)
        elif tag == "figcaption" and self.figure is not None:
            self.in_figcaption = True

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell["text"] += data
        if self.in_caption:
            self.table["caption"] += data
        if self.in_figcaption:
            self.figure["caption"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.cell = None
        elif tag == "tr":
            self.row = None
        elif tag == "table":
            self.table = None
        elif tag == "caption":
            self.in_caption = False
        elif tag == "figure":
            self.figure = None
        elif tag == "figcaption":
            self.in_figcaption = False


def keyed_rows(table: dict, attribute: str) -> dict:
    return {
        row["attrs"][attribute]: [cell["text"].strip() for cell in row["cells"]]
        for row in table["rows"] if attribute in row["attrs"]
    }


def check_tables(article: ArticleMarkup, methods: ArticleMarkup, summary: dict, guided: dict, manifest: dict) -> None:
    for document in (article, methods):
        for name, table in document.tables.items():
            require(bool(table["caption"].strip()), f"{name}: missing accessible caption")
            require(all(cell["tag"] == "th" and cell["scope"] == "col" for cell in table["rows"][0]["cells"]), f"{name}: missing column headers")
            require(all(row["cells"][0]["scope"] == "row" for row in table["rows"][1:]), f"{name}: missing row headers")

    alt = next(item for item in guided["teaching_alternatives"] if item["id"] == "ALT-017")
    rules = keyed_rows(article.tables["alt017-rules"], "data-rule")
    projected = manifest["table_claims"]["alt017-rules"]
    require(projected["rules"] == alt["screening"]["checks"], "Stale ALT-017 table projection")
    for rule in projected["rules"]:
        cells = rules[rule["rule_id"]]
        require(cells[-1] == ("Passes" if rule["passes"] else "Fails"), f"{rule['rule_id']}: wrong disposition")
    require(numbers(rules["aggregate_volume_reduction"][0]) == [6], "Volume rule threshold drift")
    require(numbers(rules["aggregate_volume_reduction"][1]) == [alt["outfall_volume_reduction_percent"]], "Volume result drift")
    require(numbers(rules["present_value_cost_cap"][0]) == [200000], "Cost cap drift")
    require(numbers(rules["present_value_cost_cap"][1]) == [alt["present_value_cost_thousand_usd"] * 1000], "Cost conversion drift")
    peak_pairs = [(run["baseline_outfall_peak_cfs"], run["outfall_peak_cfs"]) for run in alt["storm_runs"] if run["outfall_peak_delta_cfs"] > 0]
    require(numbers(rules["no_outfall_peak_increase"][1]) == [value for pair in peak_pairs for value in pair], "Missing or incorrect peak increase")
    require("cfs" in rules["no_outfall_peak_increase"][1], "Peak units missing")
    require(numbers(rules["solver_qa"][1]) == [projected["accepted_qa_runs"]] == [4], "QA count drift")

    result_rows = keyed_rows(article.tables["held-out-results"], "data-check")
    validation = {row["model_id"]: row for row in summary["mechanism_summaries"] if row["split"] == "validation"}
    require(projected["run_count"] == len(alt["storm_runs"]), "Fixture run count drift")
    for check, cells in result_rows.items():
        expected = [f"{validation[model][check]['success']}/{validation[model]['denominator']}" for model in MODEL_IDS]
        require(cells[1:] == expected, f"{check}: result or denominator drift")
    required_checks = {"threshold_sensitivity", "distractor_specificity", "withheld_target_abstention", "complete_mechanism"}
    require(set(result_rows) == required_checks, "Missing behavioral check")
    require(numbers(article.tables["held-out-results"]["caption"]) == [12], "Scenario denominator missing from caption")
    require(manifest["table_claims"]["held-out-results"]["rows"] == list(validation.values()), "Stale validation table projection")

    calls = keyed_rows(methods.tables["call-accounting"], "data-stage")
    stages = summary["stage_attempt_counts"]
    require(numbers(calls["grounding"][1]) == [24, 4, 2, 2], "Grounding factorial drift")
    require(numbers(calls["prose_diagnosis"][1]) == [12, 3, 2, 2, 2], "Diagnostic factorial drift")
    require(stages["grounding"] == 24 * 4 * 2 * 2 == 384, "Grounding source count drift")
    require(stages["prose_diagnosis"] == 12 * 3 * 2 * 2 * 2 == 288, "Diagnostic source count drift")
    for stage, expected in {**stages, "total": summary["attempt_count"]}.items():
        require(numbers(calls[stage][-1]) == [expected], f"{stage}: call table drift")
    require(sum(stages.values()) == summary["attempt_count"] == 672, "Total call arithmetic drift")

    diagnostic = keyed_rows(methods.tables["token-diagnostic"], "data-model")
    for row in summary["prose_cap_summaries"]:
        require(diagnostic[row["model_id"]][1:] == [f"{row['caps'][cap]['success']}/{row['caps'][cap]['denominator']}" for cap in ("128", "256")], "Token diagnostic result drift")


def check_assets(article: ArticleMarkup, manifest: dict, source: str) -> None:
    referenced_svgs: set[str] = set()
    order_aliases = {"three-evidence-checks": "02-listening-tests", "recorded-counterfactual": "01-counterfactual"}
    require([order_aliases.get(item, item) for item in article.element_order] == manifest["active_element_order"], "Article explanatory elements are out of order")
    for name, figure in article.figures.items():
        require(bool(figure["caption"].strip()), f"{name}: missing caption")
        require(len(figure["images"]) == 1 and bool(figure["images"][0].get("alt")), f"{name}: missing image alternative")
        for rel in figure["sources"] + [item["src"] for item in figure["images"]]:
            path = ARTICLE_ROOT / rel
            require(path.is_file() and path.stat().st_size > 1000, f"Missing asset {rel}")
            if path.suffix in (".svg", ".png"):
                svg_path = path.with_suffix(".svg")
                require(svg_path.is_file(), f"Editable source missing for {rel}")
                referenced_svgs.add(svg_path.name)
                svg = svg_path.read_text()
                require("<script" not in svg.lower(), f"Active script in {rel}")
                require('href="http' not in svg.lower() and "url(http" not in svg.lower(), f"Remote SVG dependency in {rel}")
    require(referenced_svgs == set(manifest["figures"]), "Active manifest and article SVGs disagree")
    for filename, metadata in manifest["figures"].items():
        require(digest(ARTICLE_ROOT / "figures" / filename) == metadata["sha256"], f"Figure hash mismatch: {filename}")
    for rel, expected in manifest["source_files"].items():
        require(digest(REPO_ROOT / rel) == expected, f"Evidence source drift: {rel}")
    require("03-two-ledgers" not in source and "04-authority-map" not in source, "Inactive visual remains in article")
    for stem in ("03-two-ledgers", "04-authority-map"):
        require((ARTICLE_ROOT / "figures" / f"{stem}.svg").exists(), "Historical source was removed")
    require((ARTICLE_ROOT / "revisions/article-before-engineering-rewrite.md").is_file(), "Previous draft missing")


def check_prose(article: str, methods: str, summary: dict) -> None:
    require("(methods.md)" in article and "(article.md)" in methods, "Companion documents are not linked")
    require("672 = 384 + 288" in methods, "Full accounting missing from methods")
    control = summary["d1_deterministic_control"]
    require(f"{control['passed_count']}/{control['cell_count']}" in methods, "Deterministic control denominator missing")
    resources = summary["total_resource_summary"]
    for key in ("input_tokens", "output_tokens"):
        require(f"{resources[key]:,}" in methods, f"Missing {key} accounting")
    require(resources["provider_invocations"] == resources["provider_response_count"] == resources["trace_acknowledged_count"] == summary["attempt_count"], "Operational source counts changed")
    require(summary["publication_recovery"]["provider_calls"] == 0 and "0 new provider calls" in methods, "Recovery scope drift")
    require("TypeError" in methods and "error_codes" in methods, "Publication failure omitted")
    require(summary["production_decision"] in methods and summary["production_release_eligible"] is False, "Recorded disposition missing")
    require("practitioner_usefulness_status: not_measured" in methods, "Usefulness boundary missing")
    cf = summary["representative_counterfactual"]
    require(cf["same_plan_returned"] and not cf["answer_released"], "Counterfactual conclusion drift")
    for key in ("original_value", "changed_value", "threshold"):
        require(str(cf[key]) in article, f"Counterfactual {key} missing")
    for key in ("original_trace_url", "changed_trace_url"):
        if key in cf:
            require(cf[key] in article, "Counterfactual evidence link missing")
    # Narrow claim regression checks, not a semantic safety certification.
    for phrase in ("complete observability", "perfectly traced", "Weave validated the answer", "Qwen was 25% accurate", "the deterministic guide beat", "the next version has to earn speech"):
        require(phrase.lower() not in article.lower(), f"Unsupported/stale conclusion: {phrase}")


def check_previews() -> None:
    audit = json.loads((ARTICLE_ROOT / "dist/render-audit.json").read_text())
    require(audit["issues"] == [], "Render audit contains unresolved issues")
    for name, page in audit["documents"].items():
        width = 390 if name.endswith("-mobile") else 1440
        require(digest(ARTICLE_ROOT / page["sourceDocument"]) == page["sourceSha256"], f"{name}: stale source capture")
        document_name = page["sourceDocument"].removesuffix(".md")
        require(digest(ARTICLE_ROOT / "dist" / f"{document_name}.html") == page["htmlSha256"], f"{name}: stale HTML capture")
        require(page["viewportWidth"] == page["documentWidth"] == width, f"{name}: viewport overflow")
        for key in ("overflowing", "textOverflow", "failedRequests", "brokenLinks"):
            require(page[key] == [], f"{name}: failed {key}")
        require(all(font["loaded"] for font in page["fonts"]), f"{name}: missing fonts")
        require(all(image["loaded"] and image["alt"] for image in page["images"]), f"{name}: image/alt failure")
        for suffix in (".png", "-top.png"):
            require((ARTICLE_ROOT / "dist" / f"{name}{suffix}").is_file(), f"{name}: missing capture")
    for name in ("article", "methods"):
        html = (ARTICLE_ROOT / "dist" / f"{name}.html").read_text()
        require(digest(ARTICLE_ROOT / f"{name}.md") in html, f"{name}: stale HTML source hash")
    for name, figure in audit["figures"].items():
        require(figure["offCanvasText"] == [], f"{name}: figure text outside canvas")
        path = ARTICLE_ROOT / "figures" / name
        require(digest(path) == figure["svgSha256"], f"{name}: stale SVG capture")
        require(digest(path.with_suffix(".png")) == figure["rasterSha256"], f"{name}: stale PNG capture")


def main() -> None:
    article_source = (ARTICLE_ROOT / "article.md").read_text()
    methods_source = (ARTICLE_ROOT / "methods.md").read_text()
    summary = json.loads(SUMMARY.read_text())
    guided = json.loads(GUIDED.read_text())
    manifest = json.loads(MANIFEST.read_text())
    article, methods = ArticleMarkup(article_source), ArticleMarkup(methods_source)
    check_tables(article, methods, summary, guided, manifest)
    check_assets(article, manifest, article_source)
    check_prose(article_source, methods_source, summary)
    check_previews()
    print("Verified source-bound tables and figures, experiment accounting, retained draft, links, and desktop/mobile render audits.")


if __name__ == "__main__":
    main()
