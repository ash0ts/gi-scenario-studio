#!/usr/bin/env python3
"""Build the receipt-bound Evidence Guide architecture article package.

This renderer is deliberately downstream of the completed exploratory study.
It does not rerun, rescore, or reinterpret provider outputs.  It verifies the
study summary, terminal disposition, exported table manifest, and raw W&B
publication receipt before projecting three article assets:

* ``article-summary.json`` -- bounded prose and headline counts;
* ``article-table.json`` -- an accessible configuration-level fallback; and
* ``article-media.png`` -- a 1920x1080 analytical figure.

The raw W&B receipt is an input lineage record.  The final article publication
receipt is produced separately, after the three article files exist, so no
receipt participates in its own content digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

STUDY_VERSION = "evidence-guide-v2-architecture-development-r1"
TERMINAL_DISPOSITION = "EXPLORATORY_COMPLETE_LIVE_GUIDE_UNCHANGED_DISABLED"
PRODUCTION_DECISION = "DISABLE_LIVE_GUIDE"
TARGET_ENTITY = "wandb"
TARGET_PROJECT = "gi-scenario-studio-agent-evals"
CAMPAIGN_VERSION = "evidence-guide-v2-architecture-first"
TABLE_KEY = "article/evidence_guide_v2_architecture_development"
MEDIA_KEY = "article/evidence_guide_v2_architecture_development_media"
ARTICLE_TABLE_COLUMNS = (
    "stage",
    "model_id",
    "architecture_id",
    "attempt_count",
    "generated_invalid_count",
    "released_invalid_count",
    "valid_generation_rate",
    "scenario_count",
    "scenario_ever_failed_count",
    "causal_success_rate",
    "trace_acknowledgement_rate",
    "logical_provider_calls",
    "transport_attempts",
    "input_tokens",
    "output_tokens",
    "cost_usd",
)
DISPLAY_ARMS = ("D1", "A1", "A3P", "A0xF")
ARM_LABELS = {
    "D0": "D0 · deterministic catalog",
    "D1": "D1 · deterministic tools",
    "A1": "A1 · finite plan",
    "A3P": "A3-P · routed finite plan",
    "A0xF": "A0x-F · diagnostic prose",
}
ARM_COLORS = {"D1": INK_SOFT, "A1": TEAL, "A3P": BLUE, "A0xF": OCHRE}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9a-f]{24}")
_WEAVE_CALL_ID = re.compile(r"[0-9a-f-]{36}")


class ArticlePackageError(ValueError):
    """Raised when source evidence cannot support the authored article package."""


@dataclass(frozen=True, slots=True)
class ArchitectureMetric:
    arm: str
    development_successes: int
    development_total: int
    validation_successes: int
    validation_total: int
    raw_invalid: int
    total: int
    cost_usd: Decimal
    p95_latency_ms: float | None

    @property
    def development_rate(self) -> float | None:
        return _rate(self.development_successes, self.development_total)

    @property
    def validation_rate(self) -> float | None:
        return _rate(self.validation_successes, self.validation_total)

    @property
    def cost_per_100(self) -> Decimal | None:
        if not self.total:
            return None
        return self.cost_usd * Decimal(100) / Decimal(self.total)


@dataclass(frozen=True, slots=True)
class CausalMetric:
    arm: str
    complete: int
    threshold_successes: int
    withheld_successes: int

    @property
    def threshold_rate(self) -> float | None:
        return _rate(self.threshold_successes, self.complete)

    @property
    def withheld_rate(self) -> float | None:
        return _rate(self.withheld_successes, self.complete)


@dataclass(frozen=True, slots=True)
class ArticleEvidence:
    research_summary: dict[str, Any]
    terminal: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    branches: tuple[dict[str, Any], ...]
    causal_triads: tuple[dict[str, Any], ...]
    structural_plan_analysis: dict[str, Any]
    raw_publication_receipt: dict[str, Any]
    source_hashes: dict[str, str]
    configuration_rows: tuple[dict[str, Any], ...]
    architecture_metrics: tuple[ArchitectureMetric, ...]
    causal_metrics: tuple[CausalMetric, ...]
    incremental_usage: dict[str, Any]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _report_json_sha256(value: Any) -> str:
    """Match the report publisher's public-package hash contract."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArticlePackageError(f"Could not read JSON evidence: {path.name}") from error
    if not isinstance(value, dict):
        raise ArticlePackageError(f"Expected a JSON object: {path.name}")
    return value


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArticlePackageError(f"{label} must be an object")
    return value


def _rows(value: Mapping[str, Any], *, label: str) -> tuple[dict[str, Any], ...]:
    raw = value.get("rows")
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ArticlePackageError(f"{label} must contain object rows")
    return tuple(raw)


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ArticlePackageError(f"{label} must be an integer >= {minimum}")
    return value


def _finite(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ArticlePackageError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ArticlePackageError(f"{label} must be numeric") from error
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        raise ArticlePackageError(f"{label} is outside its valid range")
    return parsed


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as error:
        raise ArticlePackageError(f"{label} must be decimal-compatible") from error
    if not parsed.is_finite() or parsed < 0:
        raise ArticlePackageError(f"{label} must be a non-negative finite decimal")
    return parsed


def _rate(successes: int, total: int) -> float | None:
    return successes / total if total else None


def _percentile(values: Sequence[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(proportion * len(ordered)) - 1))
    return ordered[index]


def _verify_self_hash(value: Mapping[str, Any], field: str, *, label: str) -> None:
    digest = value.get(field)
    body = {key: item for key, item in value.items() if key != field}
    if not isinstance(digest, str) or digest != _json_sha256(body):
        raise ArticlePackageError(f"{label} self-hash drifted")


def _verify_report_self_hash(value: Mapping[str, Any], field: str, *, label: str) -> None:
    digest = value.get(field)
    body = {key: item for key, item in value.items() if key != field}
    if not isinstance(digest, str) or digest != _report_json_sha256(body):
        raise ArticlePackageError(f"{label} self-hash drifted")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _verify_raw_publication_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = dict(
        value.get("cloud_receipt")
        or value.get("development_publication")
        or value
    )
    required = {
        "kind",
        "entity",
        "project",
        "run_id",
        "run_url",
        "artifact_reference",
        "source_sha256",
        "readback_sha256",
    }
    if not required.issubset(receipt):
        raise ArticlePackageError("W&B publication receipt is incomplete")
    if receipt["kind"] not in {
        "architecture_development_study",
        "research_architecture_development_study",
    }:
        raise ArticlePackageError("W&B publication receipt has the wrong kind")
    if receipt["entity"] != TARGET_ENTITY or receipt["project"] != TARGET_PROJECT:
        raise ArticlePackageError("W&B publication receipt targets the wrong project")
    if receipt.get("project_access") not in (None, "RESTRICTED", "Restricted"):
        raise ArticlePackageError("W&B project access is not exact Restricted")
    for field in ("source_sha256", "readback_sha256"):
        if not isinstance(receipt[field], str) or not _SHA256.fullmatch(receipt[field]):
            raise ArticlePackageError(f"W&B receipt {field} is invalid")
    if receipt["source_sha256"] != receipt["readback_sha256"]:
        raise ArticlePackageError("W&B publication readback does not match its source")
    run_id = str(receipt["run_id"])
    if not _RUN_ID.fullmatch(run_id):
        raise ArticlePackageError("W&B publication run ID is invalid")
    run_url = urlparse(str(receipt["run_url"]))
    expected_path = f"/{TARGET_ENTITY}/{TARGET_PROJECT}/runs/{run_id}"
    if (
        run_url.scheme != "https"
        or run_url.netloc != "wandb.ai"
        or run_url.path != expected_path
        or run_url.query
    ):
        raise ArticlePackageError("W&B publication run URL drifted")
    artifact = receipt["artifact_reference"]
    if (
        not isinstance(artifact, str)
        or "hidden" in artifact.lower()
        or re.fullmatch(
            rf"{TARGET_ENTITY}/{TARGET_PROJECT}/[a-z0-9][a-z0-9._-]*:v[0-9]+",
            artifact,
        )
        is None
    ):
        raise ArticlePackageError("W&B artifact reference is invalid")
    return receipt


def _verify_publication_package_digest(
    publication_root: Path,
    receipt: Mapping[str, Any],
) -> None:
    paths = sorted(publication_root.glob("*.json"))
    if not paths:
        raise ArticlePackageError("W&B publication package contains no JSON evidence")
    files: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    for path in paths:
        files[path.name] = _file_sha256(path)
        value = _load_object(path)
        rows = value.get("rows")
        if isinstance(rows, list):
            row_counts[path.stem] = len(rows)
    manifest = {
        "schema_version": "1.0",
        "campaign_version": CAMPAIGN_VERSION,
        "kind": "architecture_development_study",
        "files": files,
        "row_counts": row_counts,
        "release_eligible": False,
        "release_gate_consumed": False,
        "assistant_release_basis_emitted": False,
        "hidden_attempt_rows_included": False,
    }
    if receipt.get("campaign_version") not in (None, CAMPAIGN_VERSION):
        raise ArticlePackageError("W&B publication campaign version drifted")
    if receipt.get("source_sha256") != _json_sha256(manifest):
        raise ArticlePackageError("W&B publication receipt is not bound to the source package")


def _load_tables(tables_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    manifest_path = tables_root / "manifest.json"
    manifest = _load_object(manifest_path)
    _verify_self_hash(manifest, "manifest_sha256", label="table manifest")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("study_version") != STUDY_VERSION
        or manifest.get("release_eligible") is not False
    ):
        raise ArticlePackageError("table manifest boundary drifted")
    expected = manifest.get("table_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ArticlePackageError("table manifest does not list source tables")
    required = {
        "attempts",
        "branch-outcomes",
        "causal-triads",
        "metric-breakdowns",
        "configuration-summary",
        "structural-plan-analysis",
        "cost-ledger",
    }
    if not required.issubset(expected):
        raise ArticlePackageError("required architecture-development tables are missing")
    loaded: dict[str, dict[str, Any]] = {}
    hashes = {"tables/manifest.json": _file_sha256(manifest_path)}
    for name, digest in sorted(expected.items()):
        if not isinstance(name, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ArticlePackageError("table manifest contains an invalid identity")
        path = tables_root / f"{name}.json"
        if not path.is_file() or _file_sha256(path) != digest:
            raise ArticlePackageError(f"source table hash drifted: {name}")
        loaded[name] = _load_object(path)
        hashes[f"tables/{name}.json"] = digest
    return loaded, hashes


def _validate_study_sources(
    study_root: Path,
    publication_receipt_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, str],
]:
    summary_path = study_root / "research-summary.json"
    terminal_path = study_root / "terminal-disposition.json"
    summary = _load_object(summary_path)
    terminal = _load_object(terminal_path)
    _verify_self_hash(summary, "summary_sha256", label="research summary")
    _verify_self_hash(terminal, "receipt_sha256", label="terminal disposition")
    if summary.get("study_version") != STUDY_VERSION or terminal.get("study_version") != STUDY_VERSION:
        raise ArticlePackageError("architecture-development study version drifted")
    if (
        terminal.get("disposition") != TERMINAL_DISPOSITION
        or terminal.get("production_decision") != PRODUCTION_DECISION
        or terminal.get("release_eligible") is not False
        or terminal.get("assistant_release_basis_created") is not False
        or terminal.get("practitioner_usefulness_status") != "not_measured"
    ):
        raise ArticlePackageError("terminal non-release boundary drifted")
    if (
        terminal.get("summary_sha256") != summary.get("summary_sha256")
        or terminal.get("attempt_count") != summary.get("attempt_count")
        or terminal.get("usage") != summary.get("usage")
    ):
        raise ArticlePackageError("terminal disposition does not bind the research summary")
    tables_root = study_root / "tables"
    if not tables_root.is_dir():
        tables_root = study_root / "development-publication"
    tables, table_hashes = _load_tables(tables_root)
    raw_receipt = _verify_raw_publication_receipt(_load_object(publication_receipt_path))
    if tables_root.name == "development-publication":
        published_summary = tables_root / "research-summary.json"
        published_terminal = tables_root / "terminal-disposition.json"
        if (
            not published_summary.is_file()
            or not published_terminal.is_file()
            or _file_sha256(published_summary) != _file_sha256(summary_path)
            or _file_sha256(published_terminal) != _file_sha256(terminal_path)
        ):
            raise ArticlePackageError("published summary or terminal duplicate drifted")
    _verify_publication_package_digest(tables_root, raw_receipt)
    source_hashes = {
        "research-summary.json": _file_sha256(summary_path),
        "terminal-disposition.json": _file_sha256(terminal_path),
        **table_hashes,
        "wandb-study-publication.json": _file_sha256(publication_receipt_path),
    }
    return summary, terminal, tables, raw_receipt, source_hashes


def _validate_research_rows(
    summary: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]],
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    dict[str, Any],
]:
    attempts = _rows(tables["attempts"], label="attempts table")
    branches = _rows(tables["branch-outcomes"], label="branch-outcomes table")
    triads = _rows(tables["causal-triads"], label="causal-triads table")
    structural = _mapping(tables["structural-plan-analysis"], label="structural plan analysis")
    if structural.get("analysis_sha256"):
        _verify_self_hash(structural, "analysis_sha256", label="structural plan analysis")
    expected_attempts = _integer(summary.get("attempt_count"), label="summary attempt_count", minimum=1)
    expected_branches = _integer(summary.get("branch_count"), label="summary branch_count", minimum=1)
    if len(attempts) != expected_attempts or len(branches) != expected_branches:
        raise ArticlePackageError("attempt or branch table denominator drifted")
    if (
        structural.get("all_scenarios_have_one_allowed_plan") is not True
        or structural.get("model_reasoning_attribution_permitted") is not False
        or _integer(structural.get("scenario_count"), label="singleton scenario_count", minimum=1)
        <= 0
    ):
        raise ArticlePackageError("finite-plan singleton boundary is incomplete")

    summary_flow = _mapping(summary.get("conformance_flow"), label="summary conformance flow")
    observed = {
        "scheduled_branches": len(branches),
        "raw_invalid": sum(bool(row.get("raw_invalid")) for row in branches),
        "validator_contained": sum(bool(row.get("validator_contained")) for row in branches),
        "candidate_conformant": sum(bool(row.get("candidate_conformant")) for row in branches),
        "actual_responses_released": sum(
            bool(row.get("actual_answer_released")) for row in branches
        ),
        "actual_released_invalid": sum(
            bool(row.get("actual_released_invalid")) for row in branches
        ),
    }
    if any(summary_flow.get(key) != value for key, value in observed.items()):
        raise ArticlePackageError("branch rows drifted from the research conformance flow")
    if (
        observed["actual_responses_released"] != 0
        or observed["actual_released_invalid"] != 0
        or summary.get("production_non_release_enforced") is not True
        or summary.get("release_eligible") is not False
    ):
        raise ArticlePackageError("exploratory study contains an actual released answer")
    return attempts, branches, triads, structural


def _allocated_pair_count(total: int, arm: str) -> int:
    """Allocate one shared router plus two sibling calls without double counting."""

    if total < 0:
        raise ArticlePackageError("provider usage cannot be negative")
    if arm == "A3P":
        return (2 * total + 2) // 3
    if arm == "A0xF":
        return total - ((2 * total + 2) // 3)
    raise ArticlePackageError("paired call allocation requires A3P or A0xF")


def _provider_counts_for_branch(
    branch: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> tuple[int, int]:
    logical = _integer(parent.get("provider_invocations", 0), label="provider_invocations")
    transports = _integer(parent.get("transport_attempts", 0), label="transport_attempts")
    if branch.get("source_architecture_id") == "A3X":
        arm = str(branch.get("arm"))
        return _allocated_pair_count(logical, arm), _allocated_pair_count(transports, arm)
    return logical, transports


def _build_configuration_rows(
    attempts: Sequence[Mapping[str, Any]],
    branches: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    parent_by_id = {str(row.get("attempt_id")): row for row in attempts}
    if len(parent_by_id) != len(attempts) or "None" in parent_by_id:
        raise ArticlePackageError("attempt identities are incomplete or duplicated")
    grouped: dict[tuple[str, str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    for branch in branches:
        parent_id = str(branch.get("parent_attempt_id"))
        parent = parent_by_id.get(parent_id)
        if parent is None:
            raise ArticlePackageError("branch outcome does not resolve to a parent attempt")
        key = (str(branch.get("stage")), str(branch.get("model_id")), str(branch.get("arm")))
        if any(not item or item == "None" for item in key):
            raise ArticlePackageError("branch outcome identity is incomplete")
        grouped[key].append((branch, parent))

    rows: list[dict[str, Any]] = []
    for (stage, model_id, arm), values in sorted(grouped.items()):
        branch_rows = [branch for branch, _ in values]
        logical = 0
        transports = 0
        for branch, parent in values:
            branch_logical, branch_transport = _provider_counts_for_branch(branch, parent)
            logical += branch_logical
            transports += branch_transport
        scenarios: dict[str, bool] = {}
        causal_values: list[bool] = []
        for branch in branch_rows:
            case_id = str(branch.get("case_id"))
            scenarios[case_id] = scenarios.get(case_id, False) or not bool(
                branch.get("candidate_conformant")
            )
            if branch.get("causal") is not None:
                causal_values.append(bool(branch.get("causal")))
        count = len(branch_rows)
        raw_invalid = sum(bool(row.get("raw_invalid")) for row in branch_rows)
        released_invalid = sum(bool(row.get("actual_released_invalid")) for row in branch_rows)
        trace_count = sum(bool(row.get("trace_acknowledged")) for row in branch_rows)
        input_tokens = sum(_integer(row.get("input_tokens", 0), label="branch input_tokens") for row in branch_rows)
        output_tokens = sum(
            _integer(row.get("output_tokens", 0), label="branch output_tokens")
            for row in branch_rows
        )
        cost = sum(
            (_decimal(row.get("cost_usd", "0"), label="branch cost_usd") for row in branch_rows),
            Decimal(0),
        )
        rows.append(
            {
                "stage": stage,
                "model_id": model_id,
                "architecture_id": arm,
                "attempt_count": count,
                "generated_invalid_count": raw_invalid,
                "released_invalid_count": released_invalid,
                "valid_generation_rate": (count - raw_invalid) / count,
                "scenario_count": len(scenarios),
                "scenario_ever_failed_count": sum(scenarios.values()),
                "causal_success_rate": (
                    sum(causal_values) / len(causal_values) if causal_values else None
                ),
                "trace_acknowledgement_rate": trace_count / count,
                "logical_provider_calls": logical,
                "transport_attempts": transports,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": str(cost),
            }
        )
    return tuple(rows)


def _architecture_metrics(
    branches: Sequence[Mapping[str, Any]],
) -> tuple[ArchitectureMetric, ...]:
    output: list[ArchitectureMetric] = []
    for arm in DISPLAY_ARMS:
        rows = [
            row
            for row in branches
            if row.get("arm") == arm and row.get("stage") in {"development", "transfer"}
        ]
        if not rows:
            raise ArticlePackageError(f"primary architecture is absent from source rows: {arm}")
        development = [row for row in rows if row.get("stage") == "development"]
        validation = [row for row in rows if row.get("stage") == "transfer"]
        if not development or not validation:
            raise ArticlePackageError(f"development-to-validation transfer is incomplete: {arm}")
        latencies = [
            _finite(row.get("latency_ms", 0), label="branch latency_ms", minimum=0)
            for row in rows
        ]
        output.append(
            ArchitectureMetric(
                arm=arm,
                development_successes=sum(
                    bool(row.get("candidate_conformant")) for row in development
                ),
                development_total=len(development),
                validation_successes=sum(
                    bool(row.get("candidate_conformant")) for row in validation
                ),
                validation_total=len(validation),
                raw_invalid=sum(bool(row.get("raw_invalid")) for row in rows),
                total=len(rows),
                cost_usd=sum(
                    (_decimal(row.get("cost_usd", "0"), label="branch cost_usd") for row in rows),
                    Decimal(0),
                ),
                p95_latency_ms=_percentile(latencies, 0.95),
            )
        )
    return tuple(output)


def _causal_metrics(triads: Sequence[Mapping[str, Any]]) -> tuple[CausalMetric, ...]:
    output: list[CausalMetric] = []
    for arm in ("A1", "A3P", "A0xF"):
        rows = [row for row in triads if row.get("arm") == arm and row.get("complete") is True]
        if not rows:
            raise ArticlePackageError(f"complete causal triads are absent for {arm}")
        output.append(
            CausalMetric(
                arm=arm,
                complete=len(rows),
                threshold_successes=sum(
                    bool(row.get("counterfactual_causal"))
                    and bool(row.get("counterfactual_plan_changed"))
                    for row in rows
                ),
                withheld_successes=sum(
                    bool(row.get("withheld_safely_contained_or_conformant"))
                    for row in rows
                ),
            )
        )
    return tuple(output)


def _incremental_usage(summary: Mapping[str, Any]) -> dict[str, Any]:
    cumulative = _mapping(summary.get("usage"), label="research usage")
    prior = _mapping(summary.get("prior_r2b_usage"), label="prior R2b usage")
    cost = _decimal(cumulative.get("cost_usd"), label="cumulative cost") - _decimal(
        prior.get("cost_usd"), label="prior cost"
    )
    if cost < 0:
        raise ArticlePackageError("incremental study cost is negative")
    result: dict[str, Any] = {"cost_usd": str(cost)}
    for key in ("provider_invocations", "transport_attempts", "input_tokens", "output_tokens"):
        current = _integer(cumulative.get(key), label=f"cumulative {key}")
        inherited = _integer(prior.get(key), label=f"prior {key}")
        if inherited > current:
            raise ArticlePackageError(f"prior {key} exceeds cumulative usage")
        result[key] = current - inherited
    return result


def load_article_evidence(
    study_root: Path,
    publication_receipt_path: Path,
) -> ArticleEvidence:
    summary, terminal, tables, raw_receipt, source_hashes = _validate_study_sources(
        study_root.resolve(), publication_receipt_path.resolve()
    )
    attempts, branches, triads, structural = _validate_research_rows(summary, tables)
    configurations = _build_configuration_rows(attempts, branches)
    usage = _incremental_usage(summary)
    if usage["provider_invocations"] != sum(
        _integer(row.get("provider_invocations", 0), label="attempt provider_invocations")
        for row in attempts
    ) or usage["transport_attempts"] != sum(
        _integer(row.get("transport_attempts", 0), label="attempt transport_attempts")
        for row in attempts
    ):
        raise ArticlePackageError("incremental provider usage drifted from completed attempts")
    return ArticleEvidence(
        research_summary=summary,
        terminal=terminal,
        attempts=attempts,
        branches=branches,
        causal_triads=triads,
        structural_plan_analysis=structural,
        raw_publication_receipt=raw_receipt,
        source_hashes=source_hashes,
        configuration_rows=configurations,
        architecture_metrics=_architecture_metrics(branches),
        causal_metrics=_causal_metrics(triads),
        incremental_usage=usage,
    )


def _font_path(repo_root: Path, filename: str) -> Path:
    path = repo_root / "media" / "gi-scenario-film" / "assets" / "fonts" / filename
    if not path.is_file():
        raise ArticlePackageError(f"Required local font is missing: {filename}")
    return path


def _fonts(repo_root: Path) -> dict[str, ImageFont.FreeTypeFont]:
    heading = _font_path(repo_root, "IBMPlexSansCondensed-SemiBold.ttf")
    body = _font_path(repo_root, "AtkinsonHyperlegible-Regular.ttf")
    bold = _font_path(repo_root, "AtkinsonHyperlegible-Bold.ttf")
    mono = _font_path(repo_root, "IBMPlexMono-Medium.ttf")
    return {
        "eyebrow": ImageFont.truetype(str(bold), 25),
        "title": ImageFont.truetype(str(heading), 56),
        "subtitle": ImageFont.truetype(str(body), 28),
        "panel": ImageFont.truetype(str(heading), 34),
        "card_label": ImageFont.truetype(str(bold), 27),
        "card_value": ImageFont.truetype(str(mono), 46),
        "card_note": ImageFont.truetype(str(body), 23),
        "row": ImageFont.truetype(str(bold), 24),
        "row_value": ImageFont.truetype(str(mono), 23),
        "small": ImageFont.truetype(str(body), 21),
        "small_bold": ImageFont.truetype(str(bold), 21),
        "footer": ImageFont.truetype(str(body), 19),
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


def _checked_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
    box: tuple[int, int, int, int],
    spacing: int = 5,
) -> None:
    bounds = draw.multiline_textbbox(xy, text, font=font, spacing=spacing)
    if bounds[0] < box[0] or bounds[1] < box[1] or bounds[2] > box[2] or bounds[3] > box[3]:
        raise ArticlePackageError(f"Authored text overflows {box}: {bounds}")
    draw.multiline_text(xy, text, font=font, fill=fill, spacing=spacing)


def _format_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _render_media(repo_root: Path, evidence: ArticleEvidence, output: Path) -> None:
    flow = _mapping(evidence.research_summary.get("conformance_flow"), label="conformance flow")
    total = _integer(flow.get("scheduled_branches"), label="scheduled branches", minimum=1)
    raw_invalid = _integer(flow.get("raw_invalid"), label="raw invalid")
    contained = _integer(flow.get("validator_contained"), label="validator contained")
    conformed = _integer(flow.get("candidate_conformant"), label="candidate conformant")
    releases = _integer(flow.get("actual_responses_released"), label="actual releases")

    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    fonts = _fonts(repo_root)

    _checked_text(
        draw,
        (SAFE_MARGIN, 42),
        "EVIDENCE GUIDE · EXPLORATORY ARCHITECTURE STUDY",
        font=fonts["eyebrow"],
        fill=TEAL,
        box=(SAFE_MARGIN, 36, 1000, 78),
    )
    _checked_text(
        draw,
        (SAFE_MARGIN, 82),
        "Did the model help—or did the boundary do the work?",
        font=fonts["title"],
        fill=INK,
        box=(SAFE_MARGIN, 76, 1824, 158),
    )
    subtitle = (
        f"{total:,} branch outcomes · visible development + consumed validation · "
        "all responses isolated from the product"
    )
    _checked_text(
        draw,
        (SAFE_MARGIN, 157),
        subtitle,
        font=fonts["subtitle"],
        fill=INK_SOFT,
        box=(SAFE_MARGIN, 150, 1824, 198),
    )

    card_top, card_bottom = 222, 434
    gap = 22
    card_width = (WIDTH - 2 * SAFE_MARGIN - 3 * gap) // 4
    card_specs = (
        (
            "RAW GENERATION INVALID",
            f"{raw_invalid:,} / {total:,}",
            "Candidate output failed one or more repository checks.",
            RED_WASH,
            RED,
        ),
        (
            "VALIDATOR CONTAINED",
            f"{contained:,} / {raw_invalid:,}" if raw_invalid else "0 / 0",
            "Invalid output was blocked from hypothetical eligibility.",
            OCHRE_WASH,
            OCHRE,
        ),
        (
            "CANDIDATE CONFORMED",
            f"{conformed:,} / {total:,}",
            "Passed automated checks; this is not an engineering approval.",
            TEAL_WASH,
            TEAL,
        ),
        (
            "ACTUALLY RELEASED",
            f"{releases:,} / {total:,}",
            "Study isolation kept every generated response out of the product.",
            BLUE_WASH,
            BLUE,
        ),
    )
    for index, (label, value, note, wash, color) in enumerate(card_specs):
        left = SAFE_MARGIN + index * (card_width + gap)
        right = left + card_width
        draw.rounded_rectangle(
            (left, card_top, right, card_bottom),
            radius=20,
            fill=wash,
            outline=color,
            width=2,
        )
        _checked_text(
            draw,
            (left + 24, card_top + 22),
            label,
            font=fonts["card_label"],
            fill=color,
            box=(left + 20, card_top + 16, right - 20, card_top + 62),
        )
        _checked_text(
            draw,
            (left + 24, card_top + 72),
            value,
            font=fonts["card_value"],
            fill=INK,
            box=(left + 20, card_top + 65, right - 20, card_top + 129),
        )
        wrapped = _wrap(draw, note, fonts["card_note"], card_width - 48)
        _checked_text(
            draw,
            (left + 24, card_top + 137),
            wrapped,
            font=fonts["card_note"],
            fill=INK_SOFT,
            box=(left + 20, card_top + 132, right - 20, card_bottom - 16),
            spacing=4,
        )

    panel_top, panel_bottom = 466, 942
    left_box = (SAFE_MARGIN, panel_top, 708, panel_bottom)
    middle_box = (732, panel_top, 1274, panel_bottom)
    right_box = (1298, panel_top, 1824, panel_bottom)
    for box in (left_box, middle_box, right_box):
        draw.rounded_rectangle(box, radius=20, fill=WHITE, outline=LINE, width=2)

    # Development-to-validation transfer panel.
    _checked_text(
        draw,
        (left_box[0] + 24, panel_top + 20),
        "Automated conformance transfer",
        font=fonts["panel"],
        fill=INK,
        box=(left_box[0] + 20, panel_top + 14, left_box[2] - 20, panel_top + 62),
    )
    _checked_text(
        draw,
        (left_box[0] + 250, panel_top + 76),
        "development",
        font=fonts["small_bold"],
        fill=INK_SOFT,
        box=(left_box[0] + 240, panel_top + 70, left_box[0] + 382, panel_top + 106),
    )
    _checked_text(
        draw,
        (left_box[0] + 430, panel_top + 76),
        "validation*",
        font=fonts["small_bold"],
        fill=INK_SOFT,
        box=(left_box[0] + 420, panel_top + 70, left_box[2] - 20, panel_top + 106),
    )
    for index, metric in enumerate(evidence.architecture_metrics):
        y = panel_top + 126 + index * 67
        color = ARM_COLORS[metric.arm]
        label = ARM_LABELS[metric.arm].split(" · ")[0]
        _checked_text(
            draw,
            (left_box[0] + 24, y),
            label,
            font=fonts["row"],
            fill=color,
            box=(left_box[0] + 20, y - 4, left_box[0] + 124, y + 35),
        )
        dev_x = left_box[0] + 309
        val_x = left_box[0] + 487
        draw.line((dev_x, y + 14, val_x, y + 14), fill=LINE, width=4)
        draw.ellipse((dev_x - 7, y + 7, dev_x + 7, y + 21), fill=color)
        draw.rectangle((val_x - 7, y + 7, val_x + 7, y + 21), fill=color)
        _checked_text(
            draw,
            (dev_x + 13, y - 1),
            _format_rate(metric.development_rate),
            font=fonts["row_value"],
            fill=INK,
            box=(dev_x + 10, y - 5, dev_x + 112, y + 36),
        )
        _checked_text(
            draw,
            (val_x + 13, y - 1),
            _format_rate(metric.validation_rate),
            font=fonts["row_value"],
            fill=INK,
            box=(val_x + 10, y - 5, left_box[2] - 18, y + 36),
        )
    transfer_note = _wrap(
        draw,
        "*Consumed for exploratory development; it is no longer untouched confirmation.",
        fonts["small"],
        left_box[2] - left_box[0] - 48,
    )
    _checked_text(
        draw,
        (left_box[0] + 24, panel_bottom - 82),
        transfer_note,
        font=fonts["small"],
        fill=INK_SOFT,
        box=(left_box[0] + 20, panel_bottom - 88, left_box[2] - 20, panel_bottom - 20),
    )

    # Causal evidence panel.
    _checked_text(
        draw,
        (middle_box[0] + 24, panel_top + 20),
        "Did evidence change the answer?",
        font=fonts["panel"],
        fill=INK,
        box=(middle_box[0] + 20, panel_top + 14, middle_box[2] - 20, panel_top + 62),
    )
    for index, metric in enumerate(evidence.causal_metrics):
        y = panel_top + 92 + index * 91
        color = ARM_COLORS[metric.arm]
        _checked_text(
            draw,
            (middle_box[0] + 24, y),
            ARM_LABELS[metric.arm].split(" · ")[0],
            font=fonts["row"],
            fill=color,
            box=(middle_box[0] + 20, y - 4, middle_box[0] + 119, y + 34),
        )
        threshold = (
            f"threshold  {metric.threshold_successes}/{metric.complete}  "
            f"({_format_rate(metric.threshold_rate)})"
        )
        withheld = (
            f"withheld   {metric.withheld_successes}/{metric.complete}  "
            f"({_format_rate(metric.withheld_rate)})"
        )
        _checked_text(
            draw,
            (middle_box[0] + 125, y - 2),
            f"{threshold}\n{withheld}",
            font=fonts["row_value"],
            fill=INK,
            box=(middle_box[0] + 120, y - 7, middle_box[2] - 20, y + 62),
            spacing=7,
        )
    causal_note = _wrap(
        draw,
        "For A1 and A3-P, the repository reduced every finite choice to one allowed plan. "
        "Their plan changes are repository behavior—not evidence of model reasoning.",
        fonts["small"],
        middle_box[2] - middle_box[0] - 48,
    )
    _checked_text(
        draw,
        (middle_box[0] + 24, panel_bottom - 118),
        causal_note,
        font=fonts["small"],
        fill=INK_SOFT,
        box=(middle_box[0] + 20, panel_bottom - 124, middle_box[2] - 20, panel_bottom - 18),
        spacing=4,
    )

    # Resource panel. Provider price is kept distinct from local deterministic work.
    _checked_text(
        draw,
        (right_box[0] + 24, panel_top + 20),
        "Recorded provider burden",
        font=fonts["panel"],
        fill=INK,
        box=(right_box[0] + 20, panel_top + 14, right_box[2] - 20, panel_top + 62),
    )
    _checked_text(
        draw,
        (right_box[0] + 151, panel_top + 75),
        "$ / 100",
        font=fonts["small_bold"],
        fill=INK_SOFT,
        box=(right_box[0] + 145, panel_top + 69, right_box[0] + 260, panel_top + 105),
    )
    _checked_text(
        draw,
        (right_box[0] + 307, panel_top + 75),
        "p95 latency",
        font=fonts["small_bold"],
        fill=INK_SOFT,
        box=(right_box[0] + 300, panel_top + 69, right_box[2] - 20, panel_top + 105),
    )
    for index, metric in enumerate(evidence.architecture_metrics):
        y = panel_top + 122 + index * 63
        color = ARM_COLORS[metric.arm]
        _checked_text(
            draw,
            (right_box[0] + 24, y),
            ARM_LABELS[metric.arm].split(" · ")[0],
            font=fonts["row"],
            fill=color,
            box=(right_box[0] + 20, y - 4, right_box[0] + 125, y + 35),
        )
        per_100 = metric.cost_per_100
        cost_text = "local" if metric.arm == "D1" else f"${per_100:.5f}"
        latency = "n/a" if metric.p95_latency_ms is None else f"{metric.p95_latency_ms / 1000:.2f}s"
        _checked_text(
            draw,
            (right_box[0] + 151, y),
            cost_text,
            font=fonts["row_value"],
            fill=INK,
            box=(right_box[0] + 145, y - 4, right_box[0] + 287, y + 35),
        )
        _checked_text(
            draw,
            (right_box[0] + 307, y),
            latency,
            font=fonts["row_value"],
            fill=INK,
            box=(right_box[0] + 300, y - 4, right_box[2] - 20, y + 35),
        )
    burden_note = _wrap(
        draw,
        "Inference dollars and client-observed latency only. Local compute, provider energy, "
        "kWh, and CO2e were not measured.",
        fonts["small"],
        right_box[2] - right_box[0] - 48,
    )
    _checked_text(
        draw,
        (right_box[0] + 24, panel_bottom - 98),
        burden_note,
        font=fonts["small"],
        fill=INK_SOFT,
        box=(right_box[0] + 20, panel_bottom - 104, right_box[2] - 20, panel_bottom - 18),
        spacing=4,
    )

    footer = (
        f"Exploratory automated behavior · {PRODUCTION_DECISION} · practitioner usefulness, "
        "hydraulic validity, ecological outcomes, and energy not measured"
    )
    _checked_text(
        draw,
        (SAFE_MARGIN, 978),
        footer,
        font=fonts["footer"],
        fill=INK_SOFT,
        box=(SAFE_MARGIN, 970, 1824, 1010),
    )
    _checked_text(
        draw,
        (SAFE_MARGIN, 1015),
        "Candidate conformance is an automated test result—not a released answer, practitioner finding, or engineering authority.",
        font=fonts["footer"],
        fill=RED,
        box=(SAFE_MARGIN, 1008, 1824, 1048),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".png", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary, format="PNG", optimize=False, compress_level=9)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _claim_ledger(evidence: ArticleEvidence) -> dict[str, Any]:
    flow = _mapping(evidence.research_summary["conformance_flow"], label="conformance flow")
    claims: list[dict[str, Any]] = [
        {
            "claim_id": "agent-study.branch-outcomes",
            "value": flow["scheduled_branches"],
            "unit": "branch outcomes",
            "denominator": None,
            "source": "research-summary.json#/conformance_flow/scheduled_branches",
            "confidence": "exact_repository_projection",
            "caveat": "Paired A3X attempts contribute two branch outcomes.",
        },
        {
            "claim_id": "agent-study.raw-invalid",
            "value": flow["raw_invalid"],
            "unit": "branch outcomes",
            "denominator": flow["scheduled_branches"],
            "source": "research-summary.json#/conformance_flow/raw_invalid",
            "confidence": "exact_repository_projection",
            "caveat": "Raw invalidity describes generated candidate output before release isolation.",
        },
        {
            "claim_id": "agent-study.validator-contained",
            "value": flow["validator_contained"],
            "unit": "branch outcomes",
            "denominator": flow["raw_invalid"],
            "source": "research-summary.json#/conformance_flow/validator_contained",
            "confidence": "exact_repository_projection",
            "caveat": "Containment is distinct from candidate conformance and actual release.",
        },
        {
            "claim_id": "agent-study.candidate-conformant",
            "value": flow["candidate_conformant"],
            "unit": "branch outcomes",
            "denominator": flow["scheduled_branches"],
            "source": "research-summary.json#/conformance_flow/candidate_conformant",
            "confidence": "exact_repository_projection",
            "caveat": "Automated conformance is not an engineering approval or product release.",
        },
        {
            "claim_id": "agent-study.actual-releases",
            "value": flow["actual_responses_released"],
            "unit": "responses",
            "denominator": flow["scheduled_branches"],
            "source": "research-summary.json#/conformance_flow/actual_responses_released",
            "confidence": "exact_repository_projection",
            "caveat": "Zero is enforced by experimental isolation, not proof that all generations were safe.",
        },
        {
            "claim_id": "agent-study.singleton-finite-plans",
            "value": evidence.structural_plan_analysis["scenario_count"],
            "unit": "visible scenarios with one allowed finite plan",
            "denominator": evidence.structural_plan_analysis["scenario_count"],
            "source": "tables/structural-plan-analysis.json#/scenario_count",
            "confidence": "exact_repository_projection",
            "caveat": "A1 and A3-P finite-plan calls measure protocol/reliability overhead, not semantic plan choice.",
        },
    ]
    for metric in evidence.architecture_metrics:
        claims.append(
            {
                "claim_id": f"agent-study.transfer.{metric.arm}",
                "value": {
                    "development": {
                        "successes": metric.development_successes,
                        "total": metric.development_total,
                    },
                    "validation": {
                        "successes": metric.validation_successes,
                        "total": metric.validation_total,
                    },
                    "raw_invalid": metric.raw_invalid,
                    "provider_cost_usd": str(metric.cost_usd),
                    "p95_latency_ms": metric.p95_latency_ms,
                },
                "unit": "architecture-level automated observations",
                "denominator": metric.total,
                "source": "tables/branch-outcomes.json#/rows",
                "derivation": "group primary development and transfer rows by arm",
                "confidence": "deterministic_derived",
                "caveat": "Validation is consumed exploratory evidence; provider dollars exclude local deterministic work.",
            }
        )
    for metric in evidence.causal_metrics:
        claims.append(
            {
                "claim_id": f"agent-study.causal.{metric.arm}",
                "value": {
                    "complete_triads": metric.complete,
                    "threshold_successes": metric.threshold_successes,
                    "withheld_successes": metric.withheld_successes,
                },
                "unit": "complete causal triads",
                "denominator": metric.complete,
                "source": "tables/causal-triads.json#/rows",
                "derivation": "require original, threshold-crossing, and evidence-withheld observations",
                "confidence": "deterministic_derived",
                "caveat": (
                    "Finite-plan changes for A1/A3-P originate in repository constraints and cannot be attributed to model reasoning."
                ),
            }
        )
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "audience": "environmental engineers, ML evaluators, and technically curious readers",
        "human_question": "Did model assistance improve evidence explanation, or did deterministic constraints provide the safety?",
        "intended_takeaway": (
            "Raw generation quality, validator containment, automated conformance, and actual product release are different outcomes and must be reported separately."
        ),
        "fixed": [
            "repository evidence and allowed actions",
            "temperature zero and bounded retry policy",
            "no actual product release",
        ],
        "varied": [
            "deterministic versus model-assisted architecture",
            "model family",
            "development versus consumed validation project family",
            "original, threshold-crossing, and evidence-withheld state",
        ],
        "measured": [
            "candidate conformance and raw invalidity",
            "validator containment and trace acknowledgement",
            "causal response to retrieved evidence",
            "provider tokens, price, transport attempts, and client-observed latency",
        ],
        "interpretation": (
            "The study can compare automated system behavior under these conditions. It cannot establish practitioner usefulness, production readiness, or engineering benefit."
        ),
        "next_question": (
            "Can a newly sealed confirmatory corpus and practitioner study show benefit over D1 without weakening any deterministic boundary?"
        ),
        "forbidden_overclaims": [
            "production release",
            "practitioner usefulness",
            "hydraulic or ecological benefit",
            "model reasoning for singleton finite plans",
            "energy, kWh, carbon, or CO2e",
        ],
        "source_file_sha256": dict(sorted(evidence.source_hashes.items())),
        "claims": claims,
    }
    return {**body, "ledger_sha256": _json_sha256(body)}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _article_table(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [{name: row[name] for name in ARTICLE_TABLE_COLUMNS} for row in rows]
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "columns": list(ARTICLE_TABLE_COLUMNS),
        "rows": normalized,
    }
    return {**body, "table_sha256": _report_json_sha256(body)}


def _summary(
    evidence: ArticleEvidence,
    *,
    article_table_sha256: str,
    article_media_sha256: str,
    claim_ledger_sha256: str,
) -> dict[str, Any]:
    flow = _mapping(evidence.research_summary["conformance_flow"], label="conformance flow")
    total = int(flow["scheduled_branches"])
    raw = int(flow["raw_invalid"])
    contained = int(flow["validator_contained"])
    conformed = int(flow["candidate_conformant"])
    missed = int(evidence.research_summary.get("validator_missed_raw_invalid_count", 0))
    headline = (
        "The study isolated every answer—and exposed where model output still failed."
        if raw
        else "The study isolated every answer; this sample observed no raw-invalid generation."
    )
    finding = (
        f"Across {total:,} branch-level evaluations, {raw:,} generated candidates were raw-invalid. "
        f"Repository validators contained {contained:,}; {conformed:,} candidates conformed to the "
        f"automated contract; {missed:,} raw-invalid candidates escaped hypothetical containment. "
        "The experiment released zero answers. These are system-behavior measurements, not a "
        "model leaderboard, practitioner finding, or engineering approval."
    )
    alt = (
        f"Analytical figure for {total:,} Evidence Guide branch outcomes. Four separate cards show "
        f"{raw:,} raw-invalid generations, {contained:,} validator-contained generations, "
        f"{conformed:,} automated-conformant candidates, and zero actual releases. Lower panels "
        "compare D1, A1, A3-P, and A0x-F across development and consumed validation, causal "
        "threshold and evidence-withheld triads, and recorded provider cost and latency. The "
        "figure states that finite-plan calls had one repository-allowed plan and therefore do "
        "not demonstrate model reasoning. Practitioner usefulness, production readiness, energy, "
        "hydraulic validity, and ecological outcomes were not measured."
    )
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "state": TERMINAL_DISPOSITION,
        "production_decision": PRODUCTION_DECISION,
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "headline": headline,
        "plain_language_finding": finding,
        "claim_boundary": (
            "Exploratory automated conformance, causal behavior, reliability, and provider-resource evidence only. No product release, practitioner, hydraulic, ecological, or energy conclusion follows."
        ),
        "limitations": [
            "The visible validation split was consumed for exploratory development and is not untouched confirmation.",
            "Every finite-plan case had one repository-allowed plan after deterministic policy; A1 and A3-P therefore do not measure semantic model choice.",
            "Candidate conformance is an automated contract result, not an actual released answer or engineering approval.",
            "No practitioner study or LLM-judge comparison measured explanation usefulness.",
            "Provider cost, tokens, transport attempts, and client-observed latency do not measure energy, kWh, carbon, or CO2e.",
            "The study did not evaluate hydraulic validity, ecological outcomes, field decisions, or regulatory suitability.",
        ],
        "attempt_count": total,
        "configuration_count": len(evidence.configuration_rows),
        "generated_invalid_count": raw,
        "released_invalid_count": int(flow["actual_released_invalid"]),
        "usage": evidence.incremental_usage,
        "media_title": "Four outcomes that should never be collapsed into ‘the agent passed’",
        "media_caption": (
            "Automated architecture-development results, with generation, containment, candidate conformance, and actual release reported separately. Validation is consumed exploratory evidence; finite-plan calls had one repository-allowed choice."
        ),
        "media_alt_text": alt,
        "article_table_sha256": article_table_sha256,
        "article_media_sha256": article_media_sha256,
        "claim_ledger_sha256": claim_ledger_sha256,
        "source_summary_sha256": evidence.research_summary["summary_sha256"],
        "source_terminal_receipt_sha256": evidence.terminal["receipt_sha256"],
        "source_publication_sha256": evidence.raw_publication_receipt["source_sha256"],
    }
    return {**body, "summary_sha256": _report_json_sha256(body)}


def build_article_package(
    repo_root: Path,
    study_root: Path,
    publication_receipt_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    evidence = load_article_evidence(study_root, publication_receipt_path)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "article-claim-ledger.json"
    table_path = output_dir / "article-table.json"
    media_path = output_dir / "article-media.png"
    summary_path = output_dir / "article-summary.json"

    ledger = _claim_ledger(evidence)
    _write_json(ledger_path, ledger)
    table = _article_table(evidence.configuration_rows)
    _write_json(table_path, table)
    _render_media(repo_root.resolve(), evidence, media_path)
    summary = _summary(
        evidence,
        article_table_sha256=_file_sha256(table_path),
        article_media_sha256=_file_sha256(media_path),
        claim_ledger_sha256=_file_sha256(ledger_path),
    )
    _write_json(summary_path, summary)
    return {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "production_decision": PRODUCTION_DECISION,
        "release_eligible": False,
        "outputs": {
            "article-summary.json": _file_sha256(summary_path),
            "article-table.json": _file_sha256(table_path),
            "article-media.png": _file_sha256(media_path),
            "article-claim-ledger.json": _file_sha256(ledger_path),
        },
    }


def finalize_publication_receipt(
    raw_receipt_path: Path,
    article_root: Path,
    output_path: Path,
    *,
    weave_evaluation_url: str | None = None,
    project_access: str | None = None,
    run_file_privacy_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a raw cloud receipt only after article content hashes exist."""

    raw = _verify_raw_publication_receipt(_load_object(raw_receipt_path))
    summary_path = article_root / "article-summary.json"
    table_path = article_root / "article-table.json"
    media_path = article_root / "article-media.png"
    summary = _load_object(summary_path)
    _verify_report_self_hash(summary, "summary_sha256", label="article summary")
    if (
        summary.get("article_table_sha256") != _file_sha256(table_path)
        or summary.get("article_media_sha256") != _file_sha256(media_path)
    ):
        raise ArticlePackageError("article summary does not bind the article table and media")
    access = project_access or raw.get("project_access")
    if str(access).upper() != "RESTRICTED":
        raise ArticlePackageError("final publication requires exact Restricted project access")
    weave_url = weave_evaluation_url or raw.get("weave_evaluation_url")
    parsed_weave = urlparse(str(weave_url or ""))
    if (
        parsed_weave.scheme != "https"
        or parsed_weave.netloc != "wandb.ai"
        or re.fullmatch(
            rf"/{TARGET_ENTITY}/{TARGET_PROJECT}/r/call/[0-9a-f-]{{36}}",
            parsed_weave.path,
        )
        is None
        or not _WEAVE_CALL_ID.fullmatch(parsed_weave.path.rsplit("/", 1)[-1])
        or parsed_weave.query
    ):
        raise ArticlePackageError("final publication requires an exact Weave evaluation URL")
    audit = dict(run_file_privacy_audit or raw.get("run_file_privacy_audit") or {})
    if audit.get("prohibited_files") != 0:
        raise ArticlePackageError("final publication run-file privacy audit did not pass")
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "entity": TARGET_ENTITY,
        "project": TARGET_PROJECT,
        "project_access": "RESTRICTED",
        "production_decision": PRODUCTION_DECISION,
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "article_summary_sha256": _file_sha256(summary_path),
        "article_table_sha256": _file_sha256(table_path),
        "article_media_sha256": _file_sha256(media_path),
        "table_key": TABLE_KEY,
        "media_key": MEDIA_KEY,
        "run_id": raw["run_id"],
        "run_url": raw["run_url"],
        "weave_evaluation_url": str(weave_url),
        "artifact": raw["artifact_reference"],
        "run_file_privacy_audit": audit,
        "research_publication_source_sha256": raw["source_sha256"],
        "research_publication_readback_sha256": raw["readback_sha256"],
        "source_publication_receipt_file_sha256": _file_sha256(raw_receipt_path),
    }
    receipt = {**body, "publication_sha256": _report_json_sha256(body)}
    _write_json(output_path, receipt)
    return receipt


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_default_repo_root())
    parser.add_argument(
        "--study-root",
        type=Path,
        default=_default_repo_root() / "dist" / "evidence-guide-v2-architecture-dev-r1",
    )
    parser.add_argument("--publication-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--finalize-publication", action="store_true")
    parser.add_argument("--weave-evaluation-url")
    parser.add_argument("--project-access")
    parser.add_argument("--privacy-audit", type=Path)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    study_root = arguments.study_root.resolve()
    output = (arguments.output_dir or study_root).resolve()
    receipt = build_article_package(
        arguments.repo_root.resolve(),
        study_root,
        arguments.publication_receipt.resolve(),
        output,
    )
    if arguments.finalize_publication:
        audit = _load_object(arguments.privacy_audit) if arguments.privacy_audit else None
        final = finalize_publication_receipt(
            arguments.publication_receipt.resolve(),
            output,
            output / "wandb-publication.json",
            weave_evaluation_url=arguments.weave_evaluation_url,
            project_access=arguments.project_access,
            run_file_privacy_audit=audit,
        )
        receipt = {**receipt, "publication_sha256": final["publication_sha256"]}
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
