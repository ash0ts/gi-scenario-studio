#!/usr/bin/env python3
"""Publish the Fehraltorf published-output reanalysis to W&B and Weave.

This is deliberately a scientific-observability workflow, not an AI-agent
demonstration and not a fresh SWMM execution. The local, hash-pinned case-study
record is authoritative. W&B mirrors its table, figure, and artifact lineage;
Weave traces the small sequence of computations used to turn those records
into a bounded engineering statement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_STUDY = (
    REPOSITORY_ROOT / "benchmarks" / "realistic_fehraltorf" / "v1" / "case-study.json"
)
DEFAULT_MEDIA_DIR = (
    REPOSITORY_ROOT / "media" / "gi-scenario-film" / "dist" / "realistic-fehraltorf"
)
DEFAULT_MEDIA_RECEIPT = DEFAULT_MEDIA_DIR / "fehraltorf-model-structure-sensitivity.json"
DEFAULT_CHART = DEFAULT_MEDIA_DIR / "fehraltorf-model-structure-sensitivity.png"
DEFAULT_TABLE = DEFAULT_MEDIA_DIR / "fehraltorf-model-structure-sensitivity.csv"
DEFAULT_RECEIPT = REPOSITORY_ROOT / "dist" / "realistic-fehraltorf" / "wandb-publication.json"

EXPECTED_CASE_ID = "eawag-fehraltorf-published-output-reanalysis-v1"
EXPECTED_SCENARIOS = frozenset({"sq", "bb", "pp", "gr"})
EXPECTED_STRUCTURES = frozenset({"BC", "BI", "CB", "ET", "GW"})
PROHIBITED_RUN_FILES = frozenset({"output.log", "requirements.txt", "wandb-metadata.json"})
TABLE_KEY = "realistic_case/fehraltorf_exact_reanalysis"
COMPARISON_TABLE_KEY = "realistic_case/model_structure_sensitivity"


class PublicationError(RuntimeError):
    """Raised when local evidence or cloud acknowledgement fails closed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"could not read JSON evidence: {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"JSON evidence must be an object: {path.name}")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise PublicationError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PublicationError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise PublicationError(f"{label} must be finite")
    return result


def _validate_case_study(value: dict[str, Any]) -> None:
    if value.get("schema_version") != "1.0" or value.get("case_id") != EXPECTED_CASE_ID:
        raise PublicationError("unexpected Fehraltorf case-study identity")
    boundary = value.get("analysis_boundary")
    if not isinstance(boundary, dict) or boundary.get("fresh_swmm_rerun") is not False:
        raise PublicationError("case study must explicitly state that no fresh SWMM rerun occurred")
    if "published" not in str(boundary.get("analysis_type", "")).casefold():
        raise PublicationError("case study must identify its published-output analysis boundary")
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        raise PublicationError("case study must contain exactly 20 structure/scenario cases")
    identities: set[tuple[str, str]] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise PublicationError(f"cases[{index}] must be an object")
        structure = str(case.get("model_structure_id", ""))
        scenario = str(case.get("gi_scenario_id", ""))
        if structure not in EXPECTED_STRUCTURES or scenario not in EXPECTED_SCENARIOS:
            raise PublicationError(f"unexpected case identity: {structure}/{scenario}")
        if (structure, scenario) in identities:
            raise PublicationError(f"duplicate case identity: {structure}/{scenario}")
        identities.add((structure, scenario))
        if int(case.get("year_count", 0)) != 30:
            raise PublicationError(f"{structure}/{scenario} does not contain 30 annual values")
        _finite(case.get("median_annual_cso_m3"), f"{structure}/{scenario} median CSO")
        qa = case.get("qa")
        if not isinstance(qa, dict) or qa.get("model_disclaimer_present") is not True:
            raise PublicationError(f"{structure}/{scenario} lacks its source-model disclaimer")
        for source_field in ("csv_source", "report_source"):
            source = case.get(source_field)
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("member"), str)
                or len(str(source.get("sha256", ""))) != 64
            ):
                raise PublicationError(f"{structure}/{scenario} has incomplete {source_field}")
    expected = {(structure, scenario) for structure in EXPECTED_STRUCTURES for scenario in EXPECTED_SCENARIOS}
    if identities != expected:
        raise PublicationError("case study does not cover the complete 5 × 4 matrix")
    claims = value.get("comparison_claims")
    if not isinstance(claims, list) or {claim.get("gi_scenario_id") for claim in claims} != {
        "bb",
        "pp",
        "gr",
    }:
        raise PublicationError("case study must contain three structure-sensitivity claims")


def _validate_media_receipt(
    receipt: dict[str, Any], *, case_sha256: str, chart: Path, table: Path
) -> None:
    source = receipt.get("source")
    outputs = receipt.get("outputs")
    if not isinstance(source, dict) or source.get("case_study_sha256") != case_sha256:
        raise PublicationError("Fehraltorf media is not bound to the current case-study record")
    if not isinstance(outputs, dict):
        raise PublicationError("Fehraltorf media receipt lacks outputs")
    png = outputs.get("png")
    csv_record = outputs.get("csv")
    if not isinstance(png, dict) or not isinstance(csv_record, dict):
        raise PublicationError("Fehraltorf media receipt is incomplete")
    if not chart.is_file() or _sha256(chart) != png.get("sha256"):
        raise PublicationError("Fehraltorf chart hash drifted")
    if not table.is_file() or _sha256(table) != csv_record.get("sha256"):
        raise PublicationError("Fehraltorf accessible table hash drifted")
    if int(png.get("width", 0)) != 1920 or int(png.get("height", 0)) != 1080:
        raise PublicationError("Fehraltorf chart must be 1920×1080")


def _project_cases(case_study: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in case_study["cases"]:
        qa = case["qa"]
        rows.append(
            {
                "case_id": case["case_id"],
                "model_structure_id": case["model_structure_id"],
                "gi_scenario_id": case["gi_scenario_id"],
                "year_count": case["year_count"],
                "median_annual_cso_m3": case["median_annual_cso_m3"],
                "reduction_percent": case["reduction_from_same_structure_baseline_percent"],
                "swmm_version": qa["swmm_version"],
                "runoff_continuity_error_percent": qa["runoff_continuity_error_percent"],
                "groundwater_continuity_error_percent": qa[
                    "groundwater_continuity_error_percent"
                ],
                "routing_continuity_error_percent": qa["routing_continuity_error_percent"],
                "percent_not_converging": qa["percent_not_converging"],
                "max_abs_node_continuity_error_percent": qa[
                    "max_abs_node_continuity_error_percent"
                ],
                "warning_count": qa["warning_count"],
                "model_disclaimer_present": qa["model_disclaimer_present"],
                "csv_sha256": case["csv_source"]["sha256"],
                "report_sha256": case["report_source"]["sha256"],
            }
        )
    return sorted(rows, key=lambda row: (row["model_structure_id"], row["gi_scenario_id"]))


def compare_model_structures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recompute scenario ranges from the repository-owned exact projection."""

    comparisons: list[dict[str, Any]] = []
    for scenario in ("bb", "pp", "gr"):
        selected = [row for row in rows if row["gi_scenario_id"] == scenario]
        reductions = [float(row["reduction_percent"]) for row in selected]
        minimum = min(selected, key=lambda row: float(row["reduction_percent"]))
        maximum = max(selected, key=lambda row: float(row["reduction_percent"]))
        comparisons.append(
            {
                "gi_scenario_id": scenario,
                "minimum_percent": min(reductions),
                "maximum_percent": max(reductions),
                "spread_percentage_points": max(reductions) - min(reductions),
                "minimum_structure_id": minimum["model_structure_id"],
                "maximum_structure_id": maximum["model_structure_id"],
            }
        )
    return comparisons


def inspect_report_qa(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Project review-worthy QA facts without declaring a universal pass/fail policy."""

    combined_bioretention = next(
        row
        for row in rows
        if row["model_structure_id"] == "CB" and row["gi_scenario_id"] == "bb"
    )
    return {
        "focus_case_id": combined_bioretention["case_id"],
        "groundwater_continuity_error_percent": combined_bioretention[
            "groundwater_continuity_error_percent"
        ],
        "runoff_continuity_error_percent": combined_bioretention[
            "runoff_continuity_error_percent"
        ],
        "routing_continuity_error_percent": combined_bioretention[
            "routing_continuity_error_percent"
        ],
        "percent_not_converging": combined_bioretention["percent_not_converging"],
        "max_abs_node_continuity_error_percent": combined_bioretention[
            "max_abs_node_continuity_error_percent"
        ],
        "warning_count": combined_bioretention["warning_count"],
        "source_model_disclaimer_present": combined_bioretention[
            "model_disclaimer_present"
        ],
        "disposition": "ENGINEER_REVIEW_REQUIRED",
        "policy_boundary": (
            "These values are surfaced for review; this reanalysis does not impose a universal "
            "continuity acceptance threshold or invalidate the source study."
        ),
    }


def formulate_bounded_claim(
    comparisons: list[dict[str, Any]], qa: dict[str, Any], analysis_boundary: dict[str, Any]
) -> dict[str, Any]:
    """Create the only conclusion the article is permitted to carry forward."""

    bioretention = next(item for item in comparisons if item["gi_scenario_id"] == "bb")
    output = {
        "supported_statement": (
            "Across five published model structures, the modeled bioretention reduction spans "
            f"{bioretention['spread_percentage_points']:.4f} percentage points. The combined-"
            "process report carries review-worthy continuity findings that belong beside that "
            "modeled percentage."
        ),
        "bioretention": bioretention,
        "qa_focus": qa,
        "fresh_swmm_rerun": analysis_boundary["fresh_swmm_rerun"],
        "authority": analysis_boundary["authority"],
        "not_established": analysis_boundary["not_established"],
    }
    output["result_sha256"] = _canonical_sha256(output)
    return output


def build_plan(
    *,
    entity: str,
    project: str,
    case_path: Path = DEFAULT_CASE_STUDY,
    media_receipt_path: Path = DEFAULT_MEDIA_RECEIPT,
    chart_path: Path = DEFAULT_CHART,
    table_path: Path = DEFAULT_TABLE,
) -> dict[str, Any]:
    case_study = _load_json(case_path)
    _validate_case_study(case_study)
    case_sha256 = _sha256(case_path)
    media_receipt = _load_json(media_receipt_path)
    _validate_media_receipt(
        media_receipt, case_sha256=case_sha256, chart=chart_path, table=table_path
    )
    rows = _project_cases(case_study)
    comparisons = compare_model_structures(rows)
    claims = {claim["gi_scenario_id"]: claim for claim in case_study["comparison_claims"]}
    for comparison in comparisons:
        claim = claims[comparison["gi_scenario_id"]]
        if not (
            math.isclose(
                comparison["minimum_percent"], float(claim["min_reduction_percent"]), abs_tol=1e-8
            )
            and math.isclose(
                comparison["maximum_percent"], float(claim["max_reduction_percent"]), abs_tol=1e-8
            )
            and math.isclose(
                comparison["spread_percentage_points"],
                float(claim["spread_percentage_points"]),
                abs_tol=1e-8,
            )
        ):
            raise PublicationError("derived comparison drifted from the pinned claim")
    qa = inspect_report_qa(rows)
    bounded_claim = formulate_bounded_claim(comparisons, qa, case_study["analysis_boundary"])
    publication_digest = _canonical_sha256(
        {
            "case_sha256": case_sha256,
            "media_receipt_sha256": _sha256(media_receipt_path),
            "chart_sha256": _sha256(chart_path),
            "table_sha256": _sha256(table_path),
            "bounded_claim_sha256": bounded_claim["result_sha256"],
        }
    )
    run_id = hashlib.sha256(
        f"fehraltorf-reanalysis-v1|{entity}|{project}|{publication_digest}".encode()
    ).hexdigest()[:24]
    return {
        "schema_version": "1.0",
        "entity": entity,
        "project": project,
        "case_id": case_study["case_id"],
        "case_sha256": case_sha256,
        "media_receipt_sha256": _sha256(media_receipt_path),
        "chart_sha256": _sha256(chart_path),
        "table_sha256": _sha256(table_path),
        "publication_digest": publication_digest,
        "run_id": run_id,
        "run_name": f"fehraltorf-published-output-reanalysis-{publication_digest[:8]}",
        "analysis_boundary": case_study["analysis_boundary"],
        "study": case_study["study"],
        "method": case_study["method"],
        "sources": case_study["sources"],
        "rows": rows,
        "comparisons": comparisons,
        "qa": qa,
        "bounded_claim": bounded_claim,
        "local_files": {
            "case_study": str(case_path),
            "media_receipt": str(media_receipt_path),
            "chart": str(chart_path),
            "table": str(table_path),
        },
    }


def _defensive_wandb_environment() -> None:
    root = REPOSITORY_ROOT / "dist" / "realistic-fehraltorf" / "wandb"
    for name, child in (
        ("WANDB_DIR", "run"),
        ("WANDB_CACHE_DIR", "cache"),
        ("WANDB_ARTIFACT_DIR", "artifacts"),
    ):
        path = root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    defaults = {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
    }
    for name, value in defaults.items():
        os.environ[name] = value


def _validate_trace_url(url: Any, entity: str, project: str) -> str:
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname != "wandb.ai" or parsed.port is not None:
        raise PublicationError("Weave did not return an exact wandb.ai trace URL")
    if not parsed.path.startswith(f"/{entity}/{project}/r/call/"):
        raise PublicationError("Weave trace URL does not match the exact Restricted destination")
    if parsed.query or parsed.fragment:
        raise PublicationError("Weave trace URL contained unexpected query or fragment state")
    return parsed.geturl()


def _wandb_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "case_id",
        "model_structure_id",
        "gi_scenario_id",
        "year_count",
        "median_annual_cso_m3",
        "reduction_percent",
        "swmm_version",
        "runoff_continuity_error_percent",
        "groundwater_continuity_error_percent",
        "routing_continuity_error_percent",
        "percent_not_converging",
        "max_abs_node_continuity_error_percent",
        "warning_count",
        "model_disclaimer_present",
        "csv_sha256",
        "report_sha256",
    ]
    return wandb.Table(columns=columns, data=[[row[column] for column in columns] for row in plan["rows"]])


def _comparison_table(wandb: Any, plan: dict[str, Any]) -> Any:
    columns = [
        "gi_scenario_id",
        "minimum_percent",
        "maximum_percent",
        "spread_percentage_points",
        "minimum_structure_id",
        "maximum_structure_id",
    ]
    return wandb.Table(
        columns=columns,
        data=[[comparison[column] for column in columns] for comparison in plan["comparisons"]],
    )


def _artifact_files(plan: dict[str, Any]) -> list[tuple[Path, str, str]]:
    local = plan["local_files"]
    return [
        (Path(local["case_study"]), "evidence/case-study.json", plan["case_sha256"]),
        (
            Path(local["media_receipt"]),
            "media/fehraltorf-model-structure-sensitivity.json",
            plan["media_receipt_sha256"],
        ),
        (
            Path(local["chart"]),
            "media/fehraltorf-model-structure-sensitivity.png",
            plan["chart_sha256"],
        ),
        (
            Path(local["table"]),
            "media/fehraltorf-model-structure-sensitivity.csv",
            plan["table_sha256"],
        ),
        (
            REPOSITORY_ROOT / "scripts" / "build_fehraltorf_case_study.py",
            "methods/build_fehraltorf_case_study.py",
            _sha256(REPOSITORY_ROOT / "scripts" / "build_fehraltorf_case_study.py"),
        ),
        (
            REPOSITORY_ROOT
            / "media"
            / "gi-scenario-film"
            / "scripts"
            / "build_fehraltorf_case_media.py",
            "methods/build_fehraltorf_case_media.py",
            _sha256(
                REPOSITORY_ROOT
                / "media"
                / "gi-scenario-film"
                / "scripts"
                / "build_fehraltorf_case_media.py"
            ),
        ),
    ]


def _verify_artifact_roundtrip(logged: Any, files: list[tuple[Path, str, str]]) -> None:
    root = REPOSITORY_ROOT / "dist" / "realistic-fehraltorf"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="artifact-roundtrip-", dir=root) as temporary:
        downloaded = Path(logged.download(root=temporary, skip_cache=True))
        for _, logical_name, expected in files:
            candidate = downloaded / logical_name
            if not candidate.is_file() or _sha256(candidate) != expected:
                raise PublicationError(f"artifact roundtrip failed for {logical_name}")


def _assert_run_file_policy(api: Any, entity: str, project: str, run_id: str) -> None:
    run = api.run(f"{entity}/{project}/{run_id}")
    observed = {item.name for item in run.files() if item.name in PROHIBITED_RUN_FILES}
    if observed:
        raise PublicationError("W&B run-file privacy audit failed: " + ", ".join(sorted(observed)))


def _validate_publication_receipt(receipt: dict[str, Any], plan: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "entity",
        "project",
        "project_id",
        "project_access",
        "case_id",
        "case_sha256",
        "publication_digest",
        "run_id",
        "run_url",
        "artifact",
        "weave_root_call_id",
        "weave_trace_id",
        "weave_trace_url",
        "weave_readback_verified",
        "wandb_run_association_verified",
        "wandb_run_association",
        "fresh_swmm_rerun",
        "source_authority",
        "bounded_claim_sha256",
        "chart_sha256",
        "table_sha256",
        "run_file_privacy_audit",
    }
    if set(receipt) != required or receipt.get("schema_version") != "1.0":
        raise PublicationError("Fehraltorf publication receipt contract drifted")
    for name in ("entity", "project", "case_id", "case_sha256", "publication_digest", "run_id"):
        if receipt.get(name) != plan.get(name):
            raise PublicationError(f"Fehraltorf publication receipt {name} drifted")
    if receipt.get("project_access") != "RESTRICTED":
        raise PublicationError("Fehraltorf publication receipt is not Restricted")
    if receipt.get("fresh_swmm_rerun") is not False:
        raise PublicationError("Fehraltorf publication receipt overstates solver execution")
    if receipt.get("source_authority") != plan["analysis_boundary"]["authority"]:
        raise PublicationError("Fehraltorf publication receipt authority drifted")
    if receipt.get("bounded_claim_sha256") != plan["bounded_claim"]["result_sha256"]:
        raise PublicationError("Fehraltorf bounded-claim receipt drifted")
    if receipt.get("chart_sha256") != plan["chart_sha256"] or receipt.get(
        "table_sha256"
    ) != plan["table_sha256"]:
        raise PublicationError("Fehraltorf publication media hashes drifted")
    if (
        receipt.get("weave_readback_verified") is not True
        or receipt.get("wandb_run_association_verified") is not True
    ):
        raise PublicationError("Fehraltorf publication lacks verified Weave/W&B acknowledgement")
    expected_association = f"{plan['entity']}/{plan['project']}/{plan['run_id']}"
    if receipt.get("wandb_run_association") != expected_association:
        raise PublicationError("Fehraltorf Weave-to-run association drifted")
    expected_run_url = (
        f"https://wandb.ai/{plan['entity']}/{plan['project']}/runs/{plan['run_id']}"
    )
    if receipt.get("run_url") != expected_run_url:
        raise PublicationError("Fehraltorf W&B run URL drifted")
    if receipt.get("weave_trace_url") != (
        f"https://wandb.ai/{plan['entity']}/{plan['project']}/r/call/"
        f"{receipt.get('weave_root_call_id')}"
    ):
        raise PublicationError("Fehraltorf Weave call URL drifted")
    artifact_prefix = (
        f"{plan['entity']}/{plan['project']}/fehraltorf-published-output-reanalysis:v"
    )
    if not str(receipt.get("artifact", "")).startswith(artifact_prefix):
        raise PublicationError("Fehraltorf artifact identity drifted")
    audit = receipt.get("run_file_privacy_audit")
    if not isinstance(audit, dict) or audit != {"audited_runs": 1, "prohibited_files": 0}:
        raise PublicationError("Fehraltorf publication privacy audit is incomplete")


def verify_publication(
    plan: dict[str, Any], *, receipt_path: Path = DEFAULT_RECEIPT
) -> dict[str, Any]:
    """Independently read back the W&B run, artifact inventory, and Weave call tree."""

    if not os.environ.get("WANDB_API_KEY"):
        raise PublicationError("WANDB_API_KEY is not configured")
    receipt = _load_json(receipt_path)
    _validate_publication_receipt(receipt, plan)
    _defensive_wandb_environment()

    import wandb
    import weave
    from gi_scenario_core.observability import _resolve_exact_project_policy

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    policy = _resolve_exact_project_policy(api, plan["entity"], plan["project"])
    if (
        policy is None
        or policy["id"] != receipt["project_id"]
        or str(policy["access"]).upper() != "RESTRICTED"
    ):
        raise PublicationError("Fehraltorf publication destination no longer verifies Restricted")
    run = api.run(f"{plan['entity']}/{plan['project']}/{plan['run_id']}")
    if run.url != receipt["run_url"] or str(run.state).casefold() != "finished":
        raise PublicationError("Fehraltorf W&B run readback is incomplete")
    if run.config.get("case_sha256") != plan["case_sha256"] or run.config.get(
        "fresh_swmm_rerun"
    ) is not False:
        raise PublicationError("Fehraltorf W&B run configuration drifted")
    if run.summary.get("publication_digest") != plan["publication_digest"]:
        raise PublicationError("Fehraltorf W&B run summary drifted")
    _assert_run_file_policy(api, plan["entity"], plan["project"], plan["run_id"])

    artifact = api.artifact(receipt["artifact"])
    expected_artifact_entries = {logical_name for _, logical_name, _ in _artifact_files(plan)}
    observed_artifact_entries = set(artifact.manifest.entries)
    if observed_artifact_entries != expected_artifact_entries:
        raise PublicationError("Fehraltorf artifact inventory drifted")

    client = weave.init(
        f"{plan['entity']}/{plan['project']}",
        settings={
            "implicitly_patch_integrations": False,
            "capture_code": False,
            "capture_client_info": False,
            "capture_system_info": False,
            "print_call_link": False,
        },
    )
    root = client.get_call(receipt["weave_root_call_id"])
    if root.output != plan["bounded_claim"]:
        raise PublicationError("Fehraltorf Weave root output drifted")
    if root.wb_run_id != receipt["wandb_run_association"]:
        raise PublicationError("Fehraltorf Weave root lost its W&B run association")
    if _validate_trace_url(root.ui_url, plan["entity"], plan["project"]) != receipt[
        "weave_trace_url"
    ]:
        raise PublicationError("Fehraltorf Weave root URL drifted")
    child_names = sorted(child.func_name for child in root.children())
    expected_children = sorted(
        [
            "fehraltorf.compare_model_structures",
            "fehraltorf.formulate_bounded_claim",
            "fehraltorf.inspect_report_qa",
        ]
    )
    if child_names != expected_children:
        raise PublicationError("Fehraltorf Weave call tree drifted")
    return {
        "verified": True,
        "project_access": "RESTRICTED",
        "run_url": receipt["run_url"],
        "artifact": receipt["artifact"],
        "artifact_file_count": len(observed_artifact_entries),
        "weave_trace_url": receipt["weave_trace_url"],
        "weave_child_ops": child_names,
        "fresh_swmm_rerun": False,
    }


def publish(plan: dict[str, Any], *, receipt_path: Path = DEFAULT_RECEIPT) -> dict[str, Any]:
    if not os.environ.get("WANDB_API_KEY"):
        raise PublicationError("WANDB_API_KEY is not configured")
    _defensive_wandb_environment()

    import wandb
    import weave
    from gi_scenario_core.observability import _resolve_exact_project_policy

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    policy = _resolve_exact_project_policy(api, plan["entity"], plan["project"])
    if policy is None or str(policy["access"]).upper() != "RESTRICTED":
        raise PublicationError("exact Restricted W&B project policy was not verified")
    project_id = policy["id"]

    weave_client = weave.init(
        f"{plan['entity']}/{plan['project']}",
        settings={
            "implicitly_patch_integrations": False,
            "capture_code": False,
            "capture_client_info": False,
            "capture_system_info": False,
            "print_call_link": False,
        },
        attributes={
            "case_id": plan["case_id"],
            "case_sha256": plan["case_sha256"],
            "analysis_type": "published_output_reanalysis",
            "fresh_swmm_rerun": False,
        },
    )
    compare_op = weave.op(name="fehraltorf.compare_model_structures", kind="tool")(
        compare_model_structures
    )
    qa_op = weave.op(name="fehraltorf.inspect_report_qa", kind="tool")(inspect_report_qa)
    claim_op = weave.op(name="fehraltorf.formulate_bounded_claim", kind="tool")(
        formulate_bounded_claim
    )

    def reanalyze(payload: dict[str, Any]) -> dict[str, Any]:
        comparisons = compare_op(payload["rows"])
        qa = qa_op(payload["rows"])
        return claim_op(comparisons, qa, payload["analysis_boundary"])

    reanalysis_op = weave.op(
        name="fehraltorf.published_output_reanalysis",
        call_display_name="Fehraltorf published SWMM-output reanalysis",
        color="green",
    )(reanalyze)

    settings = wandb.Settings(
        console="off",
        disable_code=True,
        disable_git=True,
        save_code=False,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_disable_stats=True,
        x_save_requirements=False,
    )
    files = _artifact_files(plan)
    with wandb.init(
        entity=plan["entity"],
        project=plan["project"],
        id=plan["run_id"],
        name=plan["run_name"],
        job_type="realistic_case_reanalysis",
        resume="allow",
        tags=[
            "real-world-catchment",
            "published-output-reanalysis",
            "swmm",
            "model-structure-sensitivity",
            "not-a-fresh-rerun",
        ],
        config={
            "case_id": plan["case_id"],
            "case_sha256": plan["case_sha256"],
            "publication_digest": plan["publication_digest"],
            "dataset_doi": plan["study"]["dataset_doi"],
            "paper_doi": plan["study"]["paper_doi"],
            "analysis_type": plan["analysis_boundary"]["analysis_type"],
            "fresh_swmm_rerun": False,
            "year_count": 30,
            "case_count": 20,
        },
        settings=settings,
    ) as run:
        traced_result, root_call = reanalysis_op.call(
            {
                "case_id": plan["case_id"],
                "case_sha256": plan["case_sha256"],
                "rows": plan["rows"],
                "analysis_boundary": plan["analysis_boundary"],
            }
        )
        if traced_result != plan["bounded_claim"]:
            raise PublicationError("Weave-traced result drifted from the local authority")
        weave_client.flush()
        readback = weave_client.get_call(root_call.id)
        if readback.output != traced_result:
            raise PublicationError("Weave readback did not match the local traced result")
        expected_wb_run_id = f"{plan['entity']}/{plan['project']}/{run.id}"
        if str(readback.wb_run_id or "") != expected_wb_run_id:
            raise PublicationError("Weave trace was not associated with the W&B computation run")
        trace_url = _validate_trace_url(readback.ui_url, plan["entity"], plan["project"])
        call_record = readback.to_dict()
        trace_id = str(call_record.get("trace_id") or "")
        if not trace_id:
            raise PublicationError("Weave readback did not expose a trace ID")

        run.log(
            {
                TABLE_KEY: _wandb_table(wandb, plan),
                COMPARISON_TABLE_KEY: _comparison_table(wandb, plan),
                "realistic_case/model_structure_chart": wandb.Image(
                    plan["local_files"]["chart"],
                    caption=(
                        "Published Fehraltorf SWMM outputs: bioretention result sensitivity "
                        "across five process structures, with the report QA shown beside it."
                    ),
                ),
            }
        )
        artifact = wandb.Artifact(
            name="fehraltorf-published-output-reanalysis",
            type="case-study-evidence",
            metadata={
                "case_id": plan["case_id"],
                "case_sha256": plan["case_sha256"],
                "publication_digest": plan["publication_digest"],
                "dataset_doi": plan["study"]["dataset_doi"],
                "paper_doi": plan["study"]["paper_doi"],
                "fresh_swmm_rerun": False,
                "authority": plan["analysis_boundary"]["authority"],
                "weave_root_call_id": readback.id,
                "weave_trace_id": trace_id,
            },
        )
        for local_path, logical_name, _ in files:
            artifact.add_file(str(local_path), name=logical_name, policy="immutable")
        logged = run.log_artifact(
            artifact, aliases=["latest", f"sha-{plan['publication_digest'][:12]}"]
        )
        logged.wait()
        _verify_artifact_roundtrip(logged, files)
        run.summary.update(
            {
                "publication_digest": plan["publication_digest"],
                "case_sha256": plan["case_sha256"],
                "artifact_qualified_name": logged.qualified_name,
                "weave_root_call_id": readback.id,
                "weave_trace_id": trace_id,
                "weave_trace_url": trace_url,
                "bioretention_spread_percentage_points": plan["bounded_claim"]["bioretention"][
                    "spread_percentage_points"
                ],
                "combined_bioretention_groundwater_continuity_error_percent": plan["qa"][
                    "groundwater_continuity_error_percent"
                ],
                "fresh_swmm_rerun": False,
                "scientific_claim_bounded": True,
            }
        )
        run_url = run.url
        artifact_name = logged.qualified_name
        root_call_id = readback.id

    _assert_run_file_policy(api, plan["entity"], plan["project"], plan["run_id"])
    final_policy = _resolve_exact_project_policy(api, plan["entity"], plan["project"])
    if (
        final_policy is None
        or str(final_policy["access"]).upper() != "RESTRICTED"
        or final_policy["id"] != project_id
    ):
        raise PublicationError("W&B destination policy or identity changed during publication")

    receipt = {
        "schema_version": "1.0",
        "entity": plan["entity"],
        "project": plan["project"],
        "project_id": project_id,
        "project_access": "RESTRICTED",
        "case_id": plan["case_id"],
        "case_sha256": plan["case_sha256"],
        "publication_digest": plan["publication_digest"],
        "run_id": plan["run_id"],
        "run_url": run_url,
        "artifact": artifact_name,
        "weave_root_call_id": root_call_id,
        "weave_trace_id": trace_id,
        "weave_trace_url": trace_url,
        "weave_readback_verified": True,
        "wandb_run_association_verified": True,
        "wandb_run_association": f"{plan['entity']}/{plan['project']}/{plan['run_id']}",
        "fresh_swmm_rerun": False,
        "source_authority": plan["analysis_boundary"]["authority"],
        "bounded_claim_sha256": plan["bounded_claim"]["result_sha256"],
        "chart_sha256": plan["chart_sha256"],
        "table_sha256": plan["table_sha256"],
        "run_file_privacy_audit": {"audited_runs": 1, "prohibited_files": 0},
    }
    _write_json_atomic(receipt_path, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--entity", default=os.environ.get("WANDB_REPORT_ENTITY") or os.environ.get("WANDB_ENTITY")
    )
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    parser.add_argument("--case-study", type=Path, default=DEFAULT_CASE_STUDY)
    parser.add_argument("--media-receipt", type=Path, default=DEFAULT_MEDIA_RECEIPT)
    parser.add_argument("--chart", type=Path, default=DEFAULT_CHART)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not args.entity or not args.project:
        parser.error("--entity and --project are required; the script never guesses a destination")
    plan = build_plan(
        entity=args.entity,
        project=args.project,
        case_path=args.case_study,
        media_receipt_path=args.media_receipt,
        chart_path=args.chart,
        table_path=args.table,
    )
    if args.publish and args.verify:
        parser.error("choose only one of --publish or --verify")
    if args.publish:
        receipt = publish(plan, receipt_path=args.receipt)
        print(
            json.dumps(
                {
                    "run_url": receipt["run_url"],
                    "weave_trace_url": receipt["weave_trace_url"],
                    "artifact": receipt["artifact"],
                },
                sort_keys=True,
            )
        )
    elif args.verify:
        print(json.dumps(verify_publication(plan, receipt_path=args.receipt), sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "network_calls": 0,
                    "case_id": plan["case_id"],
                    "publication_digest": plan["publication_digest"],
                    "run_id": plan["run_id"],
                    "fresh_swmm_rerun": plan["analysis_boundary"]["fresh_swmm_rerun"],
                    "case_count": len(plan["rows"]),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
