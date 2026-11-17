"""Publish the verified historical EcoCorridor search gate as a W&B Report.

The publisher is intentionally read-only with respect to benchmark evidence.  A
dry run performs the complete local verification and writes a deterministic
publication plan without importing W&B or making a network request.  Online
publication requires an explicit destination and an explicit acknowledgement
that the allowlisted inputs are synthetic public benchmark material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ecocorridor_core.benchmark import (
    verify_campaign_report,
    verify_campaign_resource_report,
    verify_dataset_manifest,
    verify_historical_search_provenance,
)
from scenario_platform.storage import atomic_write_bytes

PUBLISHER_SCHEMA_VERSION = "ecocorridor-wandb-search-report/v1.1"
RECEIPT_SCHEMA_VERSION = "ecocorridor-wandb-search-report-receipt/v1.1"
EXPECTED_DISPOSITION = "HISTORICAL_BENCHMARK_ONLY"
EXPECTED_AUTHORIZATION = "PROHIBITED"
EXPECTED_METHODS = ("random", "sobol", "sobol_extratrees")
EXPECTED_RECORD_COUNT = 240
SEARCH_ROOT = Path("benchmarks/ecocorridor/search")
DATA_MANIFEST_PATH = Path("benchmarks/ecocorridor/data-manifest.json")
BINARY_UPLOAD_AUTHORIZATION_FIELD = "wandb_public_binary_upload_allowed"
REPORT_MARKER_PREFIX = "EcoCorridor publisher marker: "
RUN_COMPLETION_STATUS = "COMPLETE"
DATASET_NAMES = (
    "h1_low_barrier.json",
    "h2_moderate_barrier.json",
    "h3_high_barrier.json",
    "h4_future_pressure.json",
)
PRIMARY_INPUT_NAMES = (
    "manifest.json",
    "release-gate.json",
    "release-gate.resources.json",
    "historical-provenance.json",
)

RUN_COLUMNS = (
    "landscape_id",
    "seed",
    "budget",
    "method",
    "exact_job_equivalent",
    "hypervolume_ratio",
    "feasible_recall",
    "safeguard_adverse_recall",
    "observed_frontier_size",
    "dataset_sha256",
    "trace_sha256",
)
METHOD_AGGREGATE_COLUMNS = (
    "budget",
    "method",
    "run_count",
    "median_hypervolume_ratio",
    "median_feasible_recall",
    "median_safeguard_adverse_recall",
)
GATE_COLUMNS = (
    "scope",
    "metric",
    "value",
    "unit",
    "comparison",
    "threshold",
    "passed",
)
RESOURCE_COLUMNS = (
    "boundary",
    "proposer_wall_seconds",
    "peak_memory_status",
    "peak_memory_bytes",
    "peak_memory_is_lower_bound",
    "energy_status",
    "energy_kwh",
    "energy_estimate_lower_kwh",
    "energy_estimate_upper_kwh",
    "energy_method",
    "hardware",
)
INPUT_COLUMNS = (
    "logical_name",
    "sha256",
    "bytes",
    "media_type",
    "asset_role",
    "repository_use",
    "sensitivity",
    "license",
    "upload_authorized",
)

_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TARGET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bWANDB_API_KEY\s*(?:=|:)\s*['\"]?[0-9a-f]{40}(?![0-9a-f])"),
    re.compile(r"(?i)(?:OPENAI_API_KEY\s*(?:=|:)\s*['\"]?)?sk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"(?i)(?:ANTHROPIC_API_KEY\s*(?:=|:)\s*['\"]?)?sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|"
        r"private[_-]?key)\b\s*(?:=|:)\s*['\"]?(?!REDACTED\b|PROHIBITED\b|"
        r"YOUR[_-]|<)[A-Za-z0-9_./+=:@-]{12,}"
    ),
    re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@[^\s/]+"),
)
_SENSITIVE_MAPPING_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"bearer[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|"
    r"private[_-]?key)(?:$|[^A-Za-z0-9])"
)
_LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)file:(?://)?"),
    re.compile(r"(?m)(?:^|[\s'\"=(])~[/\\][^\s'\")]+"),
    re.compile(
        r"(?m)(?:^|[\s'\"=(])/(?:Users|home|private|tmp|root|mnt|Volumes|"
        r"var/(?:folders|tmp))(?=/|\b)"
    ),
    re.compile(r"(?i)(?:^|[\s'\"=(])[A-Za-z]:\\(?:Users|Documents|Temp|Windows)\\"),
    re.compile(r"(?:^|[\s'\"=(])\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9$_.-]+"),
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_relative_path(value: object, *, label: str) -> Path:
    candidate = Path(str(value))
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != str(value):
        raise ValueError(f"{label} must be a normalized repository-relative path")
    return candidate


def _require_sha(value: object, *, label: str) -> str:
    parsed = str(value)
    if not re.fullmatch(r"[0-9a-f]{64}", parsed):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return parsed


def _unsafe_text_kind(value: str) -> str | None:
    if any(pattern.search(value) for pattern in _LOCAL_PATH_PATTERNS):
        return "local filesystem path"
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        return "credential-shaped material"
    return None


def _require_cloud_safe(value: object, *, label: str = "cloud payload") -> None:
    """Reject local paths and credential-shaped values before network use."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            parsed_key = str(key)
            if _SENSITIVE_MAPPING_KEY_PATTERN.search(parsed_key):
                raise ValueError(f"{label} contains a credential-shaped mapping key")
            key_reason = _unsafe_text_kind(parsed_key)
            if key_reason is not None:
                raise ValueError(f"{label} contains {key_reason} in a mapping key")
            _require_cloud_safe(item, label=f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_cloud_safe(item, label=f"{label}[{index}]")
        return
    if isinstance(value, str):
        reason = _unsafe_text_kind(value)
        if reason is not None:
            raise ValueError(f"{label} contains {reason}")


def _safe_https_url(value: object, *, label: str) -> str:
    parsed_value = str(value or "")
    parsed = urlsplit(parsed_value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    return parsed_value


def _validate_artifact_url_target(
    value: object,
    *,
    entity: str,
    project: str,
    artifact_type: str,
    artifact_name: str,
    version: str,
) -> str:
    parsed_value = _safe_https_url(value, label="artifact URL")
    parts = [part for part in urlsplit(parsed_value).path.split("/") if part]
    expected = [entity, project, "artifacts", artifact_type, artifact_name, version]
    if parts != expected:
        raise PermissionError(
            "artifact URL path belongs to a different destination or artifact version"
        )
    return parsed_value


def _validate_report_url_target(value: object, *, entity: str, project: str) -> str:
    parsed_value = _safe_https_url(value, label="report URL")
    parts = [part for part in urlsplit(parsed_value).path.split("/") if part]
    if len(parts) < 4 or parts[:3] != [entity, project, "reports"]:
        raise PermissionError("report URL path belongs to a different entity or project")
    return parsed_value


@dataclass(frozen=True, slots=True)
class VerifiedSearchInputs:
    repository_root: Path
    files: tuple[tuple[str, Path], ...]
    upload_authorizations: dict[str, dict[str, Any]]
    data_manifest_sha256: str
    campaign: dict[str, Any]
    resource_report: dict[str, Any]
    manifest: dict[str, Any]
    provenance: dict[str, Any]


def _verified_upload_authorizations(
    root: Path, files: Sequence[tuple[str, Path]]
) -> tuple[dict[str, dict[str, Any]], str]:
    """Bind every upload byte to the repository's explicit public-data policy."""

    manifest_path = root / DATA_MANIFEST_PATH
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError("ecological data-manifest upload policy is missing or unsafe")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("ecological data-manifest upload policy is unreadable") from error
    if not isinstance(payload, dict) or payload.get("manifest_version") != "1.1":
        raise ValueError("unexpected ecological data-manifest upload policy")
    records = payload.get("files")
    if not isinstance(records, list):
        raise TypeError("ecological data-manifest files must be a list")

    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise TypeError("ecological data-manifest contains an invalid file record")
        logical_name = str(record["path"])
        relative = _strict_relative_path(logical_name, label="ecological data-manifest path")
        if relative.as_posix() != logical_name or logical_name in by_path:
            raise ValueError("ecological data-manifest contains an unsafe or duplicate path")
        by_path[logical_name] = dict(record)

    authorizations: dict[str, dict[str, Any]] = {}
    for logical_name, path in files:
        record = by_path.get(logical_name)
        if record is None:
            raise PermissionError(
                f"W&B artifact input lacks ecological data-manifest authorization: {logical_name}"
            )
        required = {
            "asset_role",
            "byte_size",
            "contains_real_locations",
            "contains_species_occurrences",
            "sha256",
            "license",
            "provenance",
            "repository_use",
            "synthetic",
            "sensitivity",
            "public_replay_allowed",
            "ownership",
            "media_type",
        }
        if not required.issubset(record):
            raise PermissionError(f"W&B artifact input authorization is incomplete: {logical_name}")
        if record["sha256"] != _file_sha256(path) or record["byte_size"] != path.stat().st_size:
            raise PermissionError(
                f"W&B artifact input differs from its authorized bytes: {logical_name}"
            )
        if (
            record["repository_use"] != "BENCHMARK_ONLY"
            or record["sensitivity"] != "PUBLIC"
            or record["synthetic"] is not True
            or record["license"] != "CC0-1.0"
            or record["provenance"] != "SELF_AUTHORED_SYNTHETIC"
            or record["contains_real_locations"] is not False
            or record["contains_species_occurrences"] is not False
            or not isinstance(record["public_replay_allowed"], bool)
            or not isinstance(record["ownership"], str)
            or not record["ownership"].strip()
            or not isinstance(record["media_type"], str)
            or "/" not in record["media_type"]
        ):
            raise PermissionError(
                f"W&B artifact input is not authorized public CC0 synthetic material: {logical_name}"
            )
        expected_media_type = {
            ".json": "application/json",
            ".txt": "text/plain",
        }.get(path.suffix.lower())
        if expected_media_type is None or record["media_type"] != expected_media_type:
            raise PermissionError(
                f"W&B artifact input has an unexpected media type: {logical_name}"
            )
        binary_authorized = record.get(BINARY_UPLOAD_AUTHORIZATION_FIELD, False)
        if not isinstance(binary_authorized, bool):
            raise PermissionError(
                f"W&B artifact input has an invalid binary-upload policy: {logical_name}"
            )
        authorizations[logical_name] = record
    return authorizations, _file_sha256(manifest_path)


def _authorized_input_bytes(inputs: VerifiedSearchInputs) -> dict[str, bytes]:
    """Read each exact authorized input once and reject unsafe raw cloud payloads."""

    payloads: dict[str, bytes] = {}
    for logical_name, path in inputs.files:
        authorization = inputs.upload_authorizations[logical_name]
        media_type = str(authorization["media_type"]).lower().split(";", 1)[0].strip()
        is_json = (
            path.suffix.lower() == ".json"
            or media_type == "application/json"
            or media_type.endswith("+json")
        )
        payload = path.read_bytes()
        if (
            len(payload) != authorization["byte_size"]
            or hashlib.sha256(payload).hexdigest() != authorization["sha256"]
        ):
            raise ValueError(f"authorized W&B artifact input changed: {logical_name}")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            if is_json:
                raise ValueError(
                    f"JSON W&B artifact input is not valid UTF-8: {logical_name}"
                ) from error
            if authorization.get(BINARY_UPLOAD_AUTHORIZATION_FIELD) is not True:
                raise PermissionError(
                    f"non-UTF-8 W&B artifact input lacks explicit binary authorization: {logical_name}"
                ) from error
        else:
            reason = _unsafe_text_kind(text)
            if reason is not None:
                raise PermissionError(f"W&B artifact input contains {reason}: {logical_name}")
            if is_json:

                def reject_duplicate_keys(
                    pairs: list[tuple[str, Any]], *, input_name: str = logical_name
                ) -> dict[str, Any]:
                    parsed: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in parsed:
                            raise ValueError(
                                f"JSON W&B artifact input contains a duplicate object key: "
                                f"{input_name}"
                            )
                        parsed[key] = value
                    return parsed

                def reject_nonfinite_number(_value: str, *, input_name: str = logical_name) -> Any:
                    raise ValueError(
                        f"JSON W&B artifact input contains a non-finite number: {input_name}"
                    )

                try:
                    parsed_json = json.loads(
                        text,
                        object_pairs_hook=reject_duplicate_keys,
                        parse_constant=reject_nonfinite_number,
                    )
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"JSON W&B artifact input is malformed: {logical_name}"
                    ) from error
                try:
                    _require_cloud_safe(
                        parsed_json, label=f"JSON W&B artifact input {logical_name}"
                    )
                except ValueError as error:
                    raise PermissionError(str(error)) from error
        payloads[logical_name] = payload
    return payloads


def verify_search_inputs(repository_root: str | Path) -> VerifiedSearchInputs:
    """Verify every input and the historical, non-production disposition."""

    root = Path(repository_root).resolve()
    search_root = root / SEARCH_ROOT
    dataset_paths = [search_root / name for name in DATASET_NAMES]
    for path in [*dataset_paths, *(search_root / name for name in PRIMARY_INPUT_NAMES)]:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"required search-report input is missing: {path.name}")

    campaign_path = search_root / "release-gate.json"
    resource_path = search_root / "release-gate.resources.json"
    manifest_path = search_root / "manifest.json"
    provenance_path = search_root / "historical-provenance.json"

    campaign = verify_campaign_report(campaign_path, dataset_paths)
    manifest = verify_dataset_manifest(manifest_path, dataset_paths)
    provenance = verify_historical_search_provenance(
        provenance_path,
        expected_campaign_sha256=str(campaign["campaign_sha256"]),
    )
    frozen_sources = {
        identity: str(record["sha256"])
        for identity, record in provenance["generator_source_snapshots"].items()
    }
    resource = verify_campaign_resource_report(
        resource_path,
        expected_campaign_sha256=str(campaign["campaign_sha256"]),
        expected_execution_method_sources=frozen_sources,
    )

    if provenance["dataset_disposition"] != EXPECTED_DISPOSITION:
        raise PermissionError("only HISTORICAL_BENCHMARK_ONLY inputs may use this publisher")
    if provenance["production_authorization"] != EXPECTED_AUTHORIZATION:
        raise PermissionError("historical search publication must remain PROHIBITED in production")
    if campaign.get("pinned_wandb_artifact") is not None:
        raise PermissionError("historical campaign must remain unpinned and non-promotable")
    if campaign.get("scientific_gate_passed") is not False:
        raise PermissionError("this historical publisher expects the documented failed search gate")
    if campaign.get("production_adaptive_enabled") is not False:
        raise PermissionError("adaptive ML must remain disabled for the historical campaign")
    if tuple(campaign.get("methods", ())) != EXPECTED_METHODS:
        raise ValueError("historical search methods differ from the reviewed campaign")
    if manifest.get("intended_use") != "ALGORITHM_RELEASE_GATE_ONLY":
        raise PermissionError("search assets are not authorized as ecological decision evidence")

    provenance_bindings = {
        "search_manifest": manifest_path,
        "campaign": campaign_path,
        "resource_report": resource_path,
    }
    for key, path in provenance_bindings.items():
        record = provenance[key]
        expected_relative = path.relative_to(root).as_posix()
        if record.get("path") != expected_relative or record.get("sha256") != _file_sha256(path):
            raise ValueError(f"historical provenance no longer binds the exact {key} file")

    files: list[tuple[str, Path]] = []
    for path in [manifest_path, campaign_path, resource_path, provenance_path, *dataset_paths]:
        files.append((path.relative_to(root).as_posix(), path))
    for identity, record in sorted(provenance["generator_source_snapshots"].items()):
        relative = _strict_relative_path(record["path"], label=f"{identity} snapshot path")
        unresolved_path = root / relative
        path = unresolved_path.resolve()
        if unresolved_path.is_symlink() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"historical generator snapshot is outside the repository: {identity}")
        if _file_sha256(path) != _require_sha(record["sha256"], label=f"{identity} digest"):
            raise ValueError(f"historical generator snapshot digest changed: {identity}")
        files.append((relative.as_posix(), path))

    files = sorted(files)
    upload_authorizations, data_manifest_sha256 = _verified_upload_authorizations(root, files)
    _validate_run_records(campaign)
    verified = VerifiedSearchInputs(
        repository_root=root,
        files=tuple(files),
        upload_authorizations=upload_authorizations,
        data_manifest_sha256=data_manifest_sha256,
        campaign=campaign,
        resource_report=resource,
        manifest=manifest,
        provenance=provenance,
    )
    _authorized_input_bytes(verified)
    return verified


def _validate_run_records(campaign: Mapping[str, Any]) -> None:
    records = campaign.get("run_records")
    if not isinstance(records, list) or len(records) != EXPECTED_RECORD_COUNT:
        raise ValueError(f"historical campaign must contain {EXPECTED_RECORD_COUNT} run records")
    datasets = {str(item["landscape_id"]): item for item in campaign["datasets"]}
    budgets = tuple(int(value) for value in campaign["budgets"])
    seeds = tuple(int(value) for value in campaign["seeds"])
    expected = {
        (landscape_id, seed, budget, method)
        for landscape_id in datasets
        for seed in seeds
        for budget in budgets
        for method in EXPECTED_METHODS
    }
    observed: set[tuple[str, int, int, str]] = set()
    trace_digests = {str(item["trace_sha256"]) for item in campaign.get("traces", [])}
    for record in records:
        identity = (
            str(record["landscape_id"]),
            int(record["seed"]),
            int(record["budget"]),
            str(record["method"]),
        )
        if identity in observed:
            raise ValueError(f"duplicate search benchmark run record: {identity}")
        observed.add(identity)
        landscape = datasets.get(identity[0])
        if landscape is None or record["dataset_sha256"] != landscape["dataset_sha256"]:
            raise ValueError("search run record is not bound to its declared dataset")
        if record["trace_sha256"] not in trace_digests:
            raise ValueError("search run record references an unknown immutable trace")
        score = record["score"]
        for metric in ("hypervolume_ratio", "feasible_recall", "safeguard_adverse_recall"):
            value = float(score[metric])
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"search metric is outside [0, 1]: {metric}")
        if int(score["observed_frontier_size"]) < 0:
            raise ValueError("observed frontier size cannot be negative")
        expected_jobs = identity[2] * int(landscape["scenario_count"])
        if int(score["exact_job_equivalent"]) != expected_jobs:
            raise ValueError("exact-job-equivalent arithmetic differs from scenario scope")
    if observed != expected:
        raise ValueError("search run records do not cover the exact campaign matrix")


def _table(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> dict[str, Any]:
    parsed_rows = [list(row) for row in rows]
    payload = {
        "columns": list(columns),
        "rows": parsed_rows,
        "row_count": len(parsed_rows),
    }
    payload["sha256"] = _canonical_sha256(payload)
    return payload


def _run_table(campaign: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for record in sorted(
        campaign["run_records"],
        key=lambda item: (
            str(item["landscape_id"]),
            int(item["seed"]),
            int(item["budget"]),
            str(item["method"]),
        ),
    ):
        score = record["score"]
        rows.append(
            [
                record["landscape_id"],
                int(record["seed"]),
                int(record["budget"]),
                record["method"],
                int(score["exact_job_equivalent"]),
                float(score["hypervolume_ratio"]),
                float(score["feasible_recall"]),
                float(score["safeguard_adverse_recall"]),
                int(score["observed_frontier_size"]),
                record["dataset_sha256"],
                record["trace_sha256"],
            ]
        )
    return _table(RUN_COLUMNS, rows)


def _method_aggregate_table(campaign: Mapping[str, Any]) -> dict[str, Any]:
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in campaign["run_records"]:
        grouped[(int(record["budget"]), str(record["method"]))].append(record["score"])
    rows = []
    for (budget, method), scores in sorted(grouped.items()):
        rows.append(
            [
                budget,
                method,
                len(scores),
                statistics.median(float(item["hypervolume_ratio"]) for item in scores),
                statistics.median(float(item["feasible_recall"]) for item in scores),
                statistics.median(float(item["safeguard_adverse_recall"]) for item in scores),
            ]
        )
    return _table(METHOD_AGGREGATE_COLUMNS, rows)


def _gate_table(campaign: Mapping[str, Any]) -> dict[str, Any]:
    gate = campaign["gate_result"]
    rows: list[list[object]] = [
        [
            "campaign",
            "landscape_count",
            float(gate["landscape_count"]),
            "count",
            ">=",
            3.0,
            int(gate["landscape_count"]) >= 3,
        ],
        [
            "campaign",
            "seeds_per_landscape",
            float(gate["seeds_per_landscape"]),
            "count",
            ">=",
            10.0,
            int(gate["seeds_per_landscape"]) >= 10,
        ],
    ]
    for budget, value in sorted(
        gate["median_hypervolume_improvement_percent_by_budget"].items(),
        key=lambda item: int(item[0]),
    ):
        parsed = float(value)
        rows.append(
            [
                f"budget_{budget}",
                "median_hypervolume_improvement",
                parsed,
                "percent",
                ">=",
                5.0,
                parsed >= 5.0,
            ]
        )
    rows.extend(
        [
            [
                "campaign",
                "tenth_percentile_regression",
                float(gate["tenth_percentile_regression_percent"]),
                "percent",
                "<=",
                2.0,
                float(gate["tenth_percentile_regression_percent"]) <= 2.0,
            ],
            [
                "campaign",
                "feasible_recall_delta",
                float(gate["feasible_recall_delta_percent"]),
                "percentage_points",
                ">=",
                0.0,
                float(gate["feasible_recall_delta_percent"]) >= 0.0,
            ],
            [
                "campaign",
                "safeguard_adverse_recall",
                float(gate["safeguard_adverse_recall_percent"]),
                "percent",
                ">=",
                95.0,
                float(gate["safeguard_adverse_recall_percent"]) >= 95.0,
            ],
            [
                "release",
                "scientific_gate_passed",
                float(bool(campaign["scientific_gate_passed"])),
                "boolean",
                "==",
                1.0,
                bool(campaign["scientific_gate_passed"]),
            ],
            [
                "release",
                "production_adaptive_enabled",
                float(bool(campaign["production_adaptive_enabled"])),
                "boolean",
                "==",
                1.0,
                bool(campaign["production_adaptive_enabled"]),
            ],
            [
                "release",
                "immutable_wandb_input_pin_present",
                float(campaign["pinned_wandb_artifact"] is not None),
                "boolean",
                "==",
                1.0,
                campaign["pinned_wandb_artifact"] is not None,
            ],
        ]
    )
    return _table(GATE_COLUMNS, rows)


def _resource_table(resource_report: Mapping[str, Any]) -> dict[str, Any]:
    observation = resource_report["resource_observation"]
    return _table(
        RESOURCE_COLUMNS,
        [
            [
                observation["boundary"],
                float(observation["proposer_wall_seconds"]),
                observation["peak_memory_status"],
                int(observation["peak_memory_bytes"]),
                bool(observation["peak_memory_is_lower_bound"]),
                observation["energy_status"],
                float(observation["energy_kwh"]),
                float(observation["energy_estimate_lower_kwh"]),
                float(observation["energy_estimate_upper_kwh"]),
                observation["energy_method"],
                observation["hardware"],
            ]
        ],
    )


def _input_table(inputs: VerifiedSearchInputs) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = []
    for relative, path in inputs.files:
        authorization = inputs.upload_authorizations[relative]
        records.append(
            {
                "logical_name": relative,
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
                "media_type": authorization["media_type"],
                "asset_role": authorization["asset_role"],
                "repository_use": authorization["repository_use"],
                "sensitivity": authorization["sensitivity"],
                "license": authorization["license"],
                "upload_authorized": True,
            }
        )
    table = _table(
        INPUT_COLUMNS,
        [[record[column] for column in INPUT_COLUMNS] for record in records],
    )
    return table, records


def build_publication_plan(
    inputs: VerifiedSearchInputs,
    *,
    source_commit: str,
    source_repository: str = "ash0ts/gi-scenario-studio",
    source_tree_clean: bool,
) -> dict[str, Any]:
    """Create a deterministic, cloud-safe projection of the verified evidence."""

    if not _SHA_PATTERN.fullmatch(source_commit):
        raise ValueError("source commit must be a 40- or 64-character lowercase Git digest")
    if not _REPOSITORY_PATTERN.fullmatch(source_repository):
        raise ValueError("source repository must use the owner/repository form")
    campaign = inputs.campaign
    run_table = _run_table(campaign)
    aggregate_table = _method_aggregate_table(campaign)
    gate_table = _gate_table(campaign)
    resource_table = _resource_table(inputs.resource_report)
    input_table, input_records = _input_table(inputs)
    campaign_sha = str(campaign["campaign_sha256"])
    gate = campaign["gate_result"]

    plan: dict[str, Any] = {
        "schema_version": PUBLISHER_SCHEMA_VERSION,
        "disposition": EXPECTED_DISPOSITION,
        "production_authorization": EXPECTED_AUTHORIZATION,
        "production_adaptive_enabled": False,
        "production_search_method": "scrambled_sobol",
        "publication_boundary": (
            "Verified publication of committed historical synthetic benchmark outputs; no "
            "Circuitscape job or search campaign is rerun by this publisher."
        ),
        "source": {
            "repository": source_repository,
            "commit": source_commit,
            "tree_clean": source_tree_clean,
            "product_context_claim": (
                "UNVERIFIED_PRODUCT_CONTEXT tied to the identified source commit; this "
                "publication does not build or test the application and is not signed-installer, "
                "cloud-policy, or practitioner-acceptance evidence."
            ),
            "publisher_schema_version": PUBLISHER_SCHEMA_VERSION,
        },
        "product": {
            "name": "EcoCorridor Scenario Studio",
            "product_context": (
                "An evidence-first wildlife-connectivity portfolio screening application that "
                "keeps reportable connectivity values tied to direct Circuitscape results, "
                "bounds candidate search to reviewed interventions, and exposes hashes, QA, "
                "assumptions, and cloud lineage for audit."
            ),
            "why_useful": [
                (
                    "Helps ecological and transportation teams compare reviewed intervention "
                    "portfolios across multiple resistance hypotheses without treating an ML "
                    "prediction as ecological evidence."
                ),
                (
                    "Makes an adverse or weak-assumption result inspectable before a concept "
                    "advances to field, ecological, and engineering feasibility review."
                ),
                (
                    "Uses an explicit benchmark gate to prevent adaptive ML from shipping when "
                    "it does not materially outperform deterministic Sobol exploration."
                ),
            ],
            "claim_boundary": (
                "Connectivity screening only; not animal movement, crossing use, collision "
                "reduction, population benefit, an approved corridor, or a design-ready crossing."
            ),
        },
        "experiment": {
            "campaign_sha256": campaign_sha,
            "algorithm_version": campaign["algorithm_version"],
            "execution_mode": "VERIFIED_HISTORICAL_OFFLINE_REPLAY",
            "landscape_count": int(gate["landscape_count"]),
            "seeds_per_landscape": int(gate["seeds_per_landscape"]),
            "budgets": [int(value) for value in campaign["budgets"]],
            "methods": list(EXPECTED_METHODS),
            "run_record_count": len(campaign["run_records"]),
            "resource_claim_boundary": inputs.resource_report["claim_boundary"],
        },
        "result": {
            "scientific_gate_passed": False,
            "median_hypervolume_improvement_percent_by_budget": gate[
                "median_hypervolume_improvement_percent_by_budget"
            ],
            "feasible_recall_delta_percent": gate["feasible_recall_delta_percent"],
            "safeguard_adverse_recall_percent": gate["safeguard_adverse_recall_percent"],
            "tenth_percentile_regression_percent": gate["tenth_percentile_regression_percent"],
            "conclusion": (
                "The historical adaptive method failed the declared release thresholds. "
                "ExtraTrees remains disabled and scrambled Sobol remains the production search."
            ),
        },
        "wandb_projection": {
            "run_id": f"ecocorridor-search-gate-{campaign_sha[:12]}-{source_commit[:12]}",
            "run_name": f"EcoCorridor historical search gate {campaign_sha[:8]}",
            "job_type": "search_benchmark",
            "run_kind": "release_gate",
            "artifact_name": f"ecocorridor-search-benchmark-{campaign_sha[:12]}",
            "artifact_type": "benchmark-dataset",
            "report_title": (
                f"EcoCorridor historical search benchmark · {campaign_sha[:8]} · "
                f"{source_commit[:8]}"
            ),
        },
        "upload_authorization": {
            "manifest_path": DATA_MANIFEST_PATH.as_posix(),
            "manifest_sha256": inputs.data_manifest_sha256,
            "required_disposition": (
                "PUBLIC_CC0_SELF_AUTHORED_SYNTHETIC_BENCHMARK_ONLY_NO_REAL_LOCATIONS_OR_SPECIES"
            ),
            "artifact_input_count": len(input_records),
        },
        "tables": {
            "search_benchmark_runs": run_table,
            "search_method_aggregates": aggregate_table,
            "release_gate_metrics": gate_table,
            "resource_observation": resource_table,
            "validated_inputs": input_table,
        },
        "artifact_inputs": input_records,
    }
    plan["artifact_manifest_sha256"] = _canonical_sha256(input_records)
    _require_cloud_safe(plan)
    plan["plan_sha256"] = _canonical_sha256(plan)
    return plan


def _validate_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != PUBLISHER_SCHEMA_VERSION:
        raise ValueError("unexpected W&B search-report plan schema")
    expected = _canonical_sha256(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    if plan.get("plan_sha256") != expected:
        raise ValueError("W&B search-report plan digest does not match its contents")
    if (
        plan.get("disposition") != EXPECTED_DISPOSITION
        or plan.get("production_authorization") != EXPECTED_AUTHORIZATION
        or plan.get("production_adaptive_enabled") is not False
        or plan.get("production_search_method") != "scrambled_sobol"
    ):
        raise PermissionError("W&B search-report plan weakens the historical production boundary")
    if plan["tables"]["search_benchmark_runs"]["row_count"] != EXPECTED_RECORD_COUNT:
        raise ValueError("W&B search-report plan lost benchmark run records")
    for table_name, table in plan["tables"].items():
        expected_table_sha = _canonical_sha256(
            {key: value for key, value in table.items() if key != "sha256"}
        )
        if table.get("sha256") != expected_table_sha:
            raise ValueError(f"W&B table projection digest changed: {table_name}")
        if table.get("row_count") != len(table.get("rows", [])):
            raise ValueError(f"W&B table projection row count changed: {table_name}")
    if plan.get("artifact_manifest_sha256") != _canonical_sha256(plan["artifact_inputs"]):
        raise ValueError("W&B artifact input manifest digest changed")
    upload_authorization = plan.get("upload_authorization")
    if not isinstance(upload_authorization, Mapping) or (
        upload_authorization.get("manifest_path") != DATA_MANIFEST_PATH.as_posix()
        or not re.fullmatch(r"[0-9a-f]{64}", str(upload_authorization.get("manifest_sha256", "")))
        or upload_authorization.get("artifact_input_count") != len(plan["artifact_inputs"])
        or upload_authorization.get("required_disposition")
        != "PUBLIC_CC0_SELF_AUTHORED_SYNTHETIC_BENCHMARK_ONLY_NO_REAL_LOCATIONS_OR_SPECIES"
    ):
        raise PermissionError("W&B plan lacks the exact ecological upload authorization")
    _require_cloud_safe(plan)


def _report_markdown(plan: Mapping[str, Any], *, run_url: str, artifact_ref: str) -> dict[str, str]:
    experiment = plan["experiment"]
    result = plan["result"]
    product = plan["product"]
    budgets = ", ".join(str(value) for value in experiment["budgets"])
    improvements = result["median_hypervolume_improvement_percent_by_budget"]
    result_lines = "\n".join(
        f"- Budget {budget}: {float(value):.3f}% median hypervolume improvement versus Sobol "
        f"(required: at least 5%)."
        for budget, value in sorted(improvements.items(), key=lambda item: int(item[0]))
    )
    why = "\n".join(f"- {item}" for item in product["why_useful"])
    return {
        "boundary": (
            f"**{EXPECTED_DISPOSITION} · production authorization {EXPECTED_AUTHORIZATION}.** "
            "Adaptive ML is disabled; scrambled Sobol remains the production search. This "
            "report republishes verified committed outputs and does not rerun the benchmark."
        ),
        "built": (f"{product['product_context']}\n\n**{plan['source']['product_context_claim']}**"),
        "experiment": (
            f"The offline algorithm-selection replay contains **{experiment['run_record_count']}** "
            f"records: {experiment['landscape_count']} synthetic landscapes × "
            f"{experiment['seeds_per_landscape']} seeds × 3 methods × 2 budgets ({budgets}). "
            "A method sees an outcome only after requesting that portfolio. The tables are "
            "historical synthetic algorithm-test data, not current ecological decision evidence."
        ),
        "results": (
            f"{result_lines}\n"
            f"- Feasible-recall delta: {float(result['feasible_recall_delta_percent']):.3f} "
            "percentage points (must not regress).\n"
            f"- Safeguard/adverse recall: {float(result['safeguard_adverse_recall_percent']):.3f}% "
            "(required: at least 95%).\n"
            f"- Tenth-percentile hypervolume regression: "
            f"{float(result['tenth_percentile_regression_percent']):.3f}% (allowed: at most 2%).\n\n"
            f"**Conclusion:** {result['conclusion']}"
        ),
        "useful": why,
        "lineage": (
            f"[Open the exact W&B run]({run_url}) · immutable artifact version: "
            f"`{artifact_ref}`\n\nEvery input file is checksum-pinned in the `validated_inputs` "
            "Table and in the artifact manifest. Upload authorization is independently bound to "
            f"`{plan['upload_authorization']['manifest_path']}` at SHA-256 "
            f"`{plan['upload_authorization']['manifest_sha256']}`. The publication does not write "
            "an artifact reference back into the historical release-gate JSON."
        ),
        "limits": (
            f"{product['claim_boundary']}\n\n"
            f"Resource limitation: {experiment['resource_claim_boundary']}"
        ),
    }


def _build_report_blocks(
    reports: Any,
    plan: Mapping[str, Any],
    *,
    entity: str,
    project: str,
    run_url: str,
    artifact_ref: str,
) -> list[Any]:
    copy = _report_markdown(plan, run_url=run_url, artifact_ref=artifact_ref)
    projection = plan["wandb_projection"]
    campaign_sha = plan["experiment"]["campaign_sha256"]
    source_commit = plan["source"]["commit"]
    source_repository = plan["source"]["repository"]
    plan_sha = plan["plan_sha256"]
    runset = reports.Runset(
        entity=entity,
        project=project,
        name="Verified historical release-gate publication",
        filters=(
            f"ID = '{projection['run_id']}' and "
            f"Config('campaign_sha256') = '{campaign_sha}' and "
            f"Config('plan_sha256') = '{plan_sha}' and "
            f"Config('source_repository') = '{source_repository}' and "
            f"Config('source_commit') = '{source_commit}' and "
            f"Config('run_kind') = '{projection['run_kind']}'"
        ),
    )
    return [
        reports.TableOfContents(),
        reports.H1("EcoCorridor search benchmark: what we tested and what it means"),
        reports.CalloutBlock(text=copy["boundary"]),
        reports.H2("Product context (not build verification)"),
        reports.MarkdownBlock(text=copy["built"]),
        reports.H2("Experiment contract"),
        reports.MarkdownBlock(text=copy["experiment"]),
        reports.H2("Release-gate result"),
        reports.MarkdownBlock(text=copy["results"]),
        reports.PanelGrid(
            runsets=[runset],
            panels=[
                reports.ScalarChart(
                    title="Scientific gate (1 = passed)",
                    metric="release_gate/scientific_gate_passed",
                ),
                reports.ScalarChart(
                    title="Adaptive production enabled (1 = yes)",
                    metric="release_gate/production_adaptive_enabled",
                ),
                reports.LinePlot(
                    title="Median hypervolume ratio by evaluation budget",
                    x="benchmark/budget",
                    y=[
                        "benchmark/hypervolume/random",
                        "benchmark/hypervolume/sobol",
                        "benchmark/hypervolume/sobol_extratrees",
                    ],
                    title_x="Portfolio evaluation budget",
                    title_y="Median hypervolume ratio",
                ),
            ],
        ),
        reports.H2("Why this is useful"),
        reports.MarkdownBlock(text=copy["useful"]),
        reports.H2("Complete auditable tables"),
        reports.PanelGrid(
            runsets=[runset],
            panels=[
                reports.WeavePanelSummaryTable(
                    table_name="search_benchmark_runs", layout=reports.Layout(w=24, h=16)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="search_method_aggregates", layout=reports.Layout(w=24, h=10)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="release_gate_metrics", layout=reports.Layout(w=24, h=12)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="resource_observation", layout=reports.Layout(w=24, h=8)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="validated_inputs", layout=reports.Layout(w=24, h=10)
                ),
            ],
        ),
        reports.H2("Run and artifact lineage"),
        reports.MarkdownBlock(text=copy["lineage"]),
        reports.H2("Environmental and evidence limitations"),
        reports.MarkdownBlock(text=copy["limits"]),
    ]


def _validate_target(entity: str, project: str) -> None:
    if not _TARGET_PATTERN.fullmatch(entity):
        raise ValueError("W&B entity must be an explicit credential-free slug")
    if not _TARGET_PATTERN.fullmatch(project):
        raise ValueError("W&B project must be an explicit credential-free slug")


def _normalize_github_repository(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        repository = value.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "https" and parsed.username is not None)
            or (parsed.scheme == "ssh" and parsed.username != "git")
        ):
            raise ValueError("origin must be a credential-free GitHub HTTPS or SSH remote")
        repository = parsed.path.lstrip("/")
    repository = repository.rstrip("/").removesuffix(".git")
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("origin must identify exactly one GitHub owner/repository")
    return repository


def _git_remote_repository(root: Path) -> str:
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError("publishing requires an identifiable Git origin remote") from error
    return _normalize_github_repository(remote)


def _expected_run_config(plan: Mapping[str, Any]) -> dict[str, Any]:
    projection = plan["wandb_projection"]
    config = {
        "app_namespace": "ecocorridor",
        "run_kind": projection["run_kind"],
        "campaign_sha256": plan["experiment"]["campaign_sha256"],
        "source_repository": plan["source"]["repository"],
        "source_commit": plan["source"]["commit"],
        "dataset_disposition": EXPECTED_DISPOSITION,
        "production_authorization": EXPECTED_AUTHORIZATION,
        "production_adaptive_enabled": False,
        "production_search_method": "scrambled_sobol",
        "execution_mode": plan["experiment"]["execution_mode"],
        "publisher_schema_version": PUBLISHER_SCHEMA_VERSION,
        "plan_sha256": plan["plan_sha256"],
    }
    _require_cloud_safe(config)
    return config


def _expected_artifact_metadata(plan: Mapping[str, Any]) -> dict[str, Any]:
    metadata = {
        "campaign_sha256": plan["experiment"]["campaign_sha256"],
        "artifact_manifest_sha256": plan["artifact_manifest_sha256"],
        "data_manifest_sha256": plan["upload_authorization"]["manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "publisher_schema_version": PUBLISHER_SCHEMA_VERSION,
        "dataset_disposition": EXPECTED_DISPOSITION,
        "production_authorization": EXPECTED_AUTHORIZATION,
        "production_adaptive_enabled": False,
        "source_repository": plan["source"]["repository"],
        "source_commit": plan["source"]["commit"],
    }
    _require_cloud_safe(metadata)
    return metadata


def _completion_summary(
    plan: Mapping[str, Any], *, artifact_ref: str, artifact_url: str
) -> dict[str, Any]:
    summary = {
        "release_gate/scientific_gate_passed": 0,
        "release_gate/production_adaptive_enabled": 0,
        "release_gate/production_authorization": EXPECTED_AUTHORIZATION,
        "release_gate/production_search_method": "scrambled_sobol",
        "release_gate/run_record_count": EXPECTED_RECORD_COUNT,
        "release_gate/landscape_count": plan["experiment"]["landscape_count"],
        "release_gate/seeds_per_landscape": plan["experiment"]["seeds_per_landscape"],
        "release_gate/feasible_recall_delta_percent": plan["result"][
            "feasible_recall_delta_percent"
        ],
        "release_gate/safeguard_adverse_recall_percent": plan["result"][
            "safeguard_adverse_recall_percent"
        ],
        "release_gate/tenth_percentile_regression_percent": plan["result"][
            "tenth_percentile_regression_percent"
        ],
        "lineage/artifact_ref": artifact_ref,
        "lineage/artifact_url": artifact_url,
        "publisher/status": RUN_COMPLETION_STATUS,
        "publisher/schema_version": PUBLISHER_SCHEMA_VERSION,
        "publisher/plan_sha256": plan["plan_sha256"],
        "publisher/campaign_sha256": plan["experiment"]["campaign_sha256"],
        "publisher/data_manifest_sha256": plan["upload_authorization"]["manifest_sha256"],
    }
    _require_cloud_safe(summary)
    return summary


def _missing_project_error(error: ValueError, *, entity: str, project: str) -> bool:
    return str(error) in {
        f"Could not find project {project}",
        f"Project {project} does not exist under entity {entity}",
    }


def _discover_deterministic_run(api: Any, *, entity: str, project: str, run_id: str) -> Any | None:
    try:
        candidates = list(
            api.runs(
                path=f"{entity}/{project}",
                filters={"name": run_id},
                per_page=50,
            )
        )
    except ValueError as error:
        if _missing_project_error(error, entity=entity, project=project):
            return None
        raise
    exact = [candidate for candidate in candidates if str(getattr(candidate, "id", "")) == run_id]
    if len(exact) > 1:
        raise RuntimeError("multiple W&B runs occupy the deterministic publisher run ID")
    if candidates and len(exact) != len(candidates):
        raise RuntimeError("W&B run discovery returned an inexact deterministic-ID match")
    return exact[0] if exact else None


def _completed_run_lineage(
    run: Any,
    plan: Mapping[str, Any],
    *,
    entity: str,
    project: str,
) -> tuple[str, str, str]:
    if str(getattr(run, "state", "")) != "finished":
        raise RuntimeError(
            "deterministic W&B run exists without the exact completed publisher marker"
        )
    try:
        observed_config = dict(run.config)
        observed_summary = dict(run.summary)
    except (TypeError, ValueError) as error:
        raise RuntimeError("deterministic W&B run metadata is unreadable") from error
    if observed_config != _expected_run_config(plan):
        raise RuntimeError("deterministic W&B run config does not match the publication plan")

    projection = plan["wandb_projection"]
    artifact_name = str(projection["artifact_name"])
    artifact_type = str(projection["artifact_type"])
    artifact_ref = str(observed_summary.get("lineage/artifact_ref", ""))
    artifact_match = re.fullmatch(
        rf"{re.escape(entity)}/{re.escape(project)}/{re.escape(artifact_name)}:(v[0-9]+)",
        artifact_ref,
    )
    if artifact_match is None:
        raise RuntimeError("completed W&B run lacks an immutable artifact version")
    try:
        artifact_url = _validate_artifact_url_target(
            observed_summary.get("lineage/artifact_url", ""),
            entity=entity,
            project=project,
            artifact_type=artifact_type,
            artifact_name=artifact_name,
            version=artifact_match.group(1),
        )
    except (PermissionError, ValueError) as error:
        raise RuntimeError(
            "completed W&B run artifact URL does not match the immutable artifact version"
        ) from error
    expected_summary = _completion_summary(
        plan, artifact_ref=artifact_ref, artifact_url=artifact_url
    )
    mismatches = [
        key for key, expected in expected_summary.items() if observed_summary.get(key) != expected
    ]
    if mismatches:
        raise RuntimeError(
            "deterministic W&B run lacks the exact completed publisher summary: "
            + ", ".join(sorted(mismatches))
        )

    try:
        logged_artifacts = list(run.logged_artifacts())
    except AttributeError as error:
        raise RuntimeError(
            "deterministic W&B run cannot prove its logged artifact lineage"
        ) from error
    exact_artifacts = [
        artifact
        for artifact in logged_artifacts
        if str(getattr(artifact, "qualified_name", "") or "") == artifact_ref
    ]
    if len(exact_artifacts) != 1:
        raise RuntimeError(
            "deterministic W&B run does not log exactly the immutable artifact version"
        )
    logged_artifact = exact_artifacts[0]
    if str(getattr(logged_artifact, "type", "") or "") != artifact_type:
        raise RuntimeError("deterministic W&B run artifact type does not match the plan")
    try:
        observed_metadata = dict(logged_artifact.metadata)
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("deterministic W&B run artifact metadata is unreadable") from error
    if observed_metadata != _expected_artifact_metadata(plan):
        raise RuntimeError("deterministic W&B run artifact metadata does not match the plan")
    try:
        logged_artifact_url = _validate_artifact_url_target(
            getattr(logged_artifact, "url", ""),
            entity=entity,
            project=project,
            artifact_type=artifact_type,
            artifact_name=artifact_name,
            version=artifact_match.group(1),
        )
    except (PermissionError, ValueError) as error:
        raise RuntimeError(
            "deterministic W&B run logged artifact URL belongs to another destination"
        ) from error
    if logged_artifact_url.rstrip("/") != artifact_url.rstrip("/"):
        raise RuntimeError(
            "deterministic W&B run artifact URL does not match the completed summary"
        )
    run_url = _safe_https_url(getattr(run, "url", ""), label="run URL")
    return run_url, artifact_ref, artifact_url


def _report_marker(plan: Mapping[str, Any]) -> dict[str, str]:
    return {
        "publisher_schema_version": PUBLISHER_SCHEMA_VERSION,
        "plan_sha256": str(plan["plan_sha256"]),
        "campaign_sha256": str(plan["experiment"]["campaign_sha256"]),
    }


def _parse_report_marker(description: object) -> dict[str, str] | None:
    value = str(description or "")
    if REPORT_MARKER_PREFIX not in value:
        return None
    encoded = value.rsplit(REPORT_MARKER_PREFIX, 1)[1].splitlines()[0].strip()
    try:
        marker = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(marker, dict) or set(marker) != {
        "publisher_schema_version",
        "plan_sha256",
        "campaign_sha256",
    }:
        return None
    if not all(isinstance(value, str) for value in marker.values()):
        return None
    return marker


def _report_description(plan: Mapping[str, Any]) -> str:
    return (
        "Verified historical synthetic search benchmark. Product narrative is unverified "
        "context, not build evidence. Production authorization is PROHIBITED; adaptive ML is "
        "disabled and scrambled Sobol remains in production.\n\n"
        f"{REPORT_MARKER_PREFIX}{_canonical_json(_report_marker(plan))}"
    )


def _report_identity(report: Any) -> tuple[str, str]:
    report_id = str(getattr(report, "id", "") or "")
    if report_id:
        return ("id", report_id)
    report_url = _safe_https_url(getattr(report, "url", ""), label="W&B Report URL")
    return ("url", report_url.rstrip("/"))


def _discover_report(
    api: Any,
    reports_module: Any,
    plan: Mapping[str, Any],
    *,
    entity: str,
    project: str,
    report_url: str | None,
) -> Any | None:
    title = str(plan["wandb_projection"]["report_title"])
    expected_marker = _report_marker(plan)
    try:
        candidates = list(api.reports(path=f"{entity}/{project}", per_page=50))
    except ValueError as error:
        if _missing_project_error(error, entity=entity, project=project):
            candidates = []
        else:
            raise
    exact: list[Any] = []
    for candidate in candidates:
        display_name = str(getattr(candidate, "display_name", getattr(candidate, "title", "")))
        marker = _parse_report_marker(getattr(candidate, "description", ""))
        if marker == expected_marker:
            exact.append(candidate)
        elif display_name == title:
            raise PermissionError(
                "deterministic W&B Report title is occupied by an unrelated or mismatched Report"
            )
    if len(exact) > 1:
        raise RuntimeError("multiple W&B Reports carry the deterministic publisher marker")

    explicit: Any | None = None
    if report_url is not None:
        validated_url = _validate_report_url_target(report_url, entity=entity, project=project)
        explicit = reports_module.Report.from_url(validated_url)
        if explicit.entity != entity or explicit.project != project:
            raise PermissionError("existing W&B Report belongs to a different destination")
        if _parse_report_marker(getattr(explicit, "description", "")) != expected_marker:
            raise PermissionError("explicit W&B Report is not owned by this exact publisher plan")
        if exact and _report_identity(exact[0]) != _report_identity(explicit):
            raise RuntimeError("explicit W&B Report conflicts with another marked Report")
        return explicit

    if not exact:
        return None
    candidate_url = _safe_https_url(getattr(exact[0], "url", ""), label="W&B Report URL")
    discovered = reports_module.Report.from_url(candidate_url)
    if discovered.entity != entity or discovered.project != project:
        raise PermissionError("discovered W&B Report belongs to a different destination")
    if _parse_report_marker(getattr(discovered, "description", "")) != expected_marker:
        raise RuntimeError("discovered W&B Report marker changed while loading")
    return discovered


def _assert_input_snapshot(inputs: VerifiedSearchInputs, payloads: Mapping[str, bytes]) -> None:
    for logical_name, path in inputs.files:
        if path.read_bytes() != payloads[logical_name]:
            raise RuntimeError(f"publisher input changed during publication: {logical_name}")
    manifest_path = inputs.repository_root / DATA_MANIFEST_PATH
    if _file_sha256(manifest_path) != inputs.data_manifest_sha256:
        raise RuntimeError("ecological upload authorization changed during publication")


def _publish_new_run(
    wandb_module: Any,
    plan: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    *,
    entity: str,
    project: str,
) -> tuple[str, str, str]:
    projection = plan["wandb_projection"]
    config = _expected_run_config(plan)
    run: Any | None = None
    with tempfile.TemporaryDirectory(prefix="ecocorridor-wandb-report-") as temporary:
        settings = wandb_module.Settings(
            console="off",
            disable_code=True,
            disable_git=True,
            x_disable_machine_info=True,
            x_disable_meta=True,
            x_save_requirements=False,
            silent=True,
        )
        run = wandb_module.init(
            entity=entity,
            project=project,
            id=projection["run_id"],
            name=projection["run_name"],
            job_type=projection["job_type"],
            tags=["release_gate", "historical_benchmark_only", "adaptive_ml_disabled"],
            config=config,
            resume="never",
            save_code=False,
            dir=temporary,
            settings=settings,
        )
        try:
            for table_name, table in plan["tables"].items():
                run.log(
                    {
                        table_name: wandb_module.Table(
                            columns=table["columns"],
                            data=table["rows"],
                        )
                    }
                )

            aggregate = plan["tables"]["search_method_aggregates"]
            indexes = {name: index for index, name in enumerate(aggregate["columns"])}
            by_budget: dict[int, dict[str, float]] = defaultdict(dict)
            for row in aggregate["rows"]:
                budget = int(row[indexes["budget"]])
                method = str(row[indexes["method"]])
                by_budget[budget][method] = float(row[indexes["median_hypervolume_ratio"]])
            for budget, values in sorted(by_budget.items()):
                run.log(
                    {
                        "benchmark/budget": budget,
                        **{
                            f"benchmark/hypervolume/{method}": values[method]
                            for method in EXPECTED_METHODS
                        },
                    }
                )

            artifact = wandb_module.Artifact(
                projection["artifact_name"],
                type=projection["artifact_type"],
                metadata=_expected_artifact_metadata(plan),
            )
            expected_inputs = {item["logical_name"] for item in plan["artifact_inputs"]}
            if expected_inputs != set(payloads):
                raise ValueError("artifact input set differs from the verified publication plan")
            for logical_name, payload in sorted(payloads.items()):
                with artifact.new_file(f"inputs/{logical_name}", mode="wb") as stream:
                    stream.write(payload)
            with artifact.new_file("report-plan.json", mode="w", encoding="utf-8") as stream:
                stream.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
            logged_artifact = run.log_artifact(artifact).wait()
            artifact_ref = str(getattr(logged_artifact, "qualified_name", "") or "")
            artifact_match = re.fullmatch(
                rf"{re.escape(entity)}/{re.escape(project)}/"
                rf"{re.escape(str(projection['artifact_name']))}:(v[0-9]+)",
                artifact_ref,
            )
            if artifact_match is None:
                raise RuntimeError("W&B did not return an immutable qualified artifact version")
            artifact_url = _validate_artifact_url_target(
                getattr(logged_artifact, "url", ""),
                entity=entity,
                project=project,
                artifact_type=str(projection["artifact_type"]),
                artifact_name=str(projection["artifact_name"]),
                version=artifact_match.group(1),
            )
            run_url = _safe_https_url(getattr(run, "url", ""), label="run URL")

            # This exact marker is the only state a retry is allowed to reuse.  It must
            # exist before the run is finalized and before any Report is saved.
            run.summary.update(
                _completion_summary(plan, artifact_ref=artifact_ref, artifact_url=artifact_url)
            )
            run.finish(exit_code=0)
            run = None
            return run_url, artifact_ref, artifact_url
        except Exception:
            if run is not None:
                run.finish(exit_code=1)
            raise


def publish_plan(
    plan: Mapping[str, Any],
    inputs: VerifiedSearchInputs,
    *,
    entity: str,
    project: str,
    authorize_synthetic_public_upload: bool,
    report_url: str | None = None,
    wandb_module: Any | None = None,
    reports_module: Any | None = None,
) -> dict[str, Any]:
    """Publish or exactly reuse one completed run, artifact version, and owned Report."""

    _validate_plan(plan)
    _validate_target(entity, project)
    if not authorize_synthetic_public_upload:
        raise PermissionError("publishing requires --authorize-synthetic-public-upload")
    if plan["source"]["tree_clean"] is not True:
        raise PermissionError(
            "publishing requires a clean source tree bound to the reported commit"
        )
    if report_url is not None:
        _validate_report_url_target(report_url, entity=entity, project=project)
    checked_out_commit, tree_clean = _git_source(inputs.repository_root)
    if checked_out_commit != plan["source"]["commit"] or not tree_clean:
        raise PermissionError(
            "checked-out Git commit and clean-tree state do not match the publication plan"
        )
    remote_repository = _git_remote_repository(inputs.repository_root)
    if remote_repository != plan["source"]["repository"]:
        raise PermissionError(
            "Git origin does not match the source repository named in the publication plan"
        )

    # Re-verify from disk immediately before any SDK import or API request so stale
    # caller objects cannot bypass the central manifest or raw-byte scanner.
    current_inputs = verify_search_inputs(inputs.repository_root)
    expected_plan = build_publication_plan(
        current_inputs,
        source_commit=str(plan["source"]["commit"]),
        source_repository=str(plan["source"]["repository"]),
        source_tree_clean=bool(plan["source"]["tree_clean"]),
    )
    if dict(plan) != expected_plan:
        raise ValueError("W&B publication plan differs from the verified benchmark projection")
    payloads = _authorized_input_bytes(current_inputs)
    _assert_input_snapshot(current_inputs, payloads)

    # Lazy imports are a deliberate dry-run safety boundary.
    if wandb_module is None:
        import wandb as wandb_module  # type: ignore[import-not-found,no-redef]
    if reports_module is None:
        import wandb_workspaces.reports.v2 as reports_module  # type: ignore[import-not-found,no-redef]

    projection = plan["wandb_projection"]
    api = wandb_module.Api()
    public_run = _discover_deterministic_run(
        api,
        entity=entity,
        project=project,
        run_id=str(projection["run_id"]),
    )
    existing_report = _discover_report(
        api,
        reports_module,
        plan,
        entity=entity,
        project=project,
        report_url=report_url,
    )
    if public_run is None and existing_report is not None:
        raise RuntimeError("publisher-owned W&B Report exists without its deterministic run")

    run_reused = public_run is not None
    report_reused = existing_report is not None
    if public_run is None:
        run_url, artifact_ref, artifact_url = _publish_new_run(
            wandb_module,
            plan,
            payloads,
            entity=entity,
            project=project,
        )
    else:
        run_url, artifact_ref, artifact_url = _completed_run_lineage(
            public_run,
            plan,
            entity=entity,
            project=project,
        )
    _assert_input_snapshot(current_inputs, payloads)

    blocks = _build_report_blocks(
        reports_module,
        plan,
        entity=entity,
        project=project,
        run_url=run_url,
        artifact_ref=artifact_ref,
    )
    description = _report_description(plan)
    if existing_report is None:
        report = reports_module.Report(
            entity=entity,
            project=project,
            title=projection["report_title"],
            description=description,
            blocks=blocks,
            width="fluid",
        )
    else:
        report = existing_report
        report.title = projection["report_title"]
        report.description = description
        report.blocks = blocks
        report.width = "fluid"
    report.save()
    published_report_url = _safe_https_url(report.url, label="published report URL")
    _assert_input_snapshot(current_inputs, payloads)

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "PUBLISHED",
        "entity": entity,
        "project": project,
        "campaign_sha256": plan["experiment"]["campaign_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "run_id": projection["run_id"],
        "run_url": run_url,
        "artifact_ref": artifact_ref,
        "artifact_url": artifact_url,
        "report_url": published_report_url,
        "run_reused": run_reused,
        "report_reused": report_reused,
        "dataset_disposition": EXPECTED_DISPOSITION,
        "production_authorization": EXPECTED_AUTHORIZATION,
        "production_adaptive_enabled": False,
        "production_search_method": "scrambled_sobol",
    }
    _require_cloud_safe(receipt)
    return receipt


def _git_source(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return commit, not bool(status.strip())


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="local repository root; never included in the cloud projection",
    )
    parser.add_argument("--source-commit", help="reported Git commit (defaults to local HEAD)")
    parser.add_argument("--source-repository", default="ash0ts/gi-scenario-studio")
    parser.add_argument("--entity", help="explicit W&B entity for online publication")
    parser.add_argument("--project", help="explicit W&B project for online publication")
    parser.add_argument(
        "--authorize-synthetic-public-upload",
        action="store_true",
        help="explicitly authorize the verified allowlisted public synthetic inputs",
    )
    parser.add_argument("--report-url", help="credential-free HTTPS URL of a Report to update")
    parser.add_argument("--dry-run", action="store_true", help="verify and write a local plan only")
    parser.add_argument("--output", required=True, type=Path, help="plan or publication receipt")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    git_commit, tree_clean = _git_source(root)
    source_commit = arguments.source_commit or git_commit
    if arguments.source_commit is not None and arguments.source_commit != git_commit:
        raise SystemExit("--source-commit must equal the checked-out repository HEAD")
    inputs = verify_search_inputs(root)
    plan = build_publication_plan(
        inputs,
        source_commit=source_commit,
        source_repository=arguments.source_repository,
        source_tree_clean=tree_clean,
    )
    if arguments.dry_run:
        _write_json(arguments.output, plan)
        print(json.dumps({"status": "DRY_RUN", "plan_sha256": plan["plan_sha256"]}, sort_keys=True))
        return 0
    if not arguments.entity or not arguments.project:
        raise SystemExit("online publication requires explicit --entity and --project")
    receipt = publish_plan(
        plan,
        inputs,
        entity=arguments.entity,
        project=arguments.project,
        authorize_synthetic_public_upload=arguments.authorize_synthetic_public_upload,
        report_url=arguments.report_url,
    )
    _write_json(arguments.output, receipt)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "run_url": receipt["run_url"],
                "report_url": receipt["report_url"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
