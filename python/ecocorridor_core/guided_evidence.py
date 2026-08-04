"""Fail-closed verification and materialization for the Ridgeway evidence bundle."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    ComputationalResourceObservation,
    EngineManifest,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    canonical_sha256,
)
from .engine import sha256_file
from .raster_io import RasterIOError, compare_current_maps_in_mask, load_esri_ascii
from .ridgeway import build_ridgeway_fixture

MAX_ARCHIVE_BYTES = 100_000_000
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
_MARKER = ".verified-guided-evidence.json"
DIRECT_EVIDENCE_SCHEMA = "ridgeway-direct-evidence/v1.2"
BUNDLE_EVIDENCE_SCHEMA = "ridgeway-evidence-bundle/v1.2"
UI_PROJECTION_SCHEMA = "ecocorridor-replay/v1.2"
HYPOTHESIS_RANKING_METRIC = (
    "Minimum required target-pair resistance reduction (%) within each resistance hypothesis; "
    "rank 1 is the highest score among the 128 directly evaluated portfolios."
)

_FIXED_ROLE_COUNTS = {
    "reviewed_asset_source": 16,
    "resistance_raster": 4,
    "focal_raster": 3,
    "reviewed_footprint_mask": 8,
    "risk_review_mask": 1,
    "cost_basis": 1,
    "assumption_ledger": 1,
    "included_pairs_source": 12,
    "study_plan": 1,
    "model_decision_snapshot": 1,
    "source_materialization_manifest": 1,
    "engine_snapshot": 4,
    "typed_engine_manifest": 1,
    "report_packager_snapshot": 1,
    "report_claim_safety_snapshot": 1,
    "evidence_index": 1,
    "raw_direct_evidence_manifest": 1,
    "raw_ui_projection": 1,
    "matrix_completion_record": 1_536,
    "map_completion_record": 60,
    "matrix_request": 1_536,
    "map_request": 60,
    "matrix_result": 1_536,
    "map_result": 60,
    "matrix_matrix": 1_536,
    "map_matrix": 60,
    "matrix_stdout": 1_536,
    "map_stdout": 60,
    "matrix_stderr": 1_536,
    "map_stderr": 60,
    "matrix_materialized_input": 1_536 * 3,
    "map_materialized_input": 60 * 3,
    "footprint_proof_records": 1_596,
    "semantic_raster_diff": 1_596,
    "circuitscape_effective_configuration": 1_596,
    "map_current_raster": 60,
}

_FIXED_REQUIRED_COVERAGE = {
    "reviewed_raw_asset_sources": 16,
    "normalized_resistance_rasters": 4,
    "normalized_focal_rasters": 3,
    "normalized_footprint_masks": 8,
    "normalized_review_masks": 1,
    "model_decision_snapshots": 1,
    "cost_basis_ledgers": 1,
    "assumption_ledgers": 1,
    "typed_engine_manifests": 1,
    "report_packager_snapshots": 1,
    "report_claim_safety_snapshots": 1,
    "raw_direct_evidence_manifests": 1,
    "raw_ui_projections": 1,
    "matrix_completion_records": 1_536,
    "map_completion_records": 60,
    "footprint_proof_documents": 1_596,
    "semantic_raster_diffs": 1_596,
    "circuitscape_effective_configurations": 1_596,
    "raw_effective_resistance_matrices": 1_596,
    "retained_current_rasters": 60,
    "validated_resource_observations": 1_596,
    "validated_process_tree_exits": 1_596,
    "all_jobs_exact_and_qa_accepted": True,
}


class GuidedEvidenceError(RuntimeError):
    """A signed Ridgeway resource failed its scientific evidence boundary."""


def _aggregate_resource_observability(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Independently validate and aggregate every retained direct-job observation."""

    observations: list[ComputationalResourceObservation] = []
    observation_ids: set[str] = set()
    for job in jobs:
        raw = job.get("resource_observation")
        if raw is None:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_OBSERVATION_MISSING")
        try:
            observation = ComputationalResourceObservation.model_validate(raw)
        except ValueError as exc:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_OBSERVATION_INVALID") from exc
        if observation.boundary not in {
            ResourceObservationBoundary.DIRECT_SOLVER,
            ResourceObservationBoundary.CACHE_REUSE,
        }:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_BOUNDARY_INVALID")
        if observation.observation_id in observation_ids:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_OBSERVATION_DUPLICATE")
        observation_ids.add(observation.observation_id)
        observations.append(observation)

    energy_statuses = {item.energy_status.value for item in observations}
    memory_statuses = {item.peak_memory_status.value for item in observations}
    estimated_energy = [
        item for item in observations if item.energy_status == ResourceObservationStatus.ESTIMATED
    ]
    observed_peak_memory = [
        item.peak_memory_bytes for item in observations if item.peak_memory_bytes is not None
    ]
    return {
        "jobsWithObservations": len(observations),
        "jobsWithoutObservations": len(jobs) - len(observations),
        "directSolverWallSeconds": sum(item.solver_wall_seconds or 0.0 for item in observations),
        "estimatedEnergyKwh": (
            sum(item.energy_kwh or 0.0 for item in estimated_energy) if estimated_energy else None
        ),
        "estimatedEnergyLowerKwh": (
            sum(item.energy_estimate_lower_kwh or 0.0 for item in estimated_energy)
            if estimated_energy
            else None
        ),
        "estimatedEnergyUpperKwh": (
            sum(item.energy_estimate_upper_kwh or 0.0 for item in estimated_energy)
            if estimated_energy
            else None
        ),
        "maximumObservedSingleProcessMemoryBytes": (
            max(observed_peak_memory) if observed_peak_memory else None
        ),
        "peakMemoryIsSampledLowerBound": bool(observed_peak_memory),
        "peakMemoryStatus": (
            "NOT_RECORDED"
            if not memory_statuses
            else next(iter(memory_statuses))
            if len(memory_statuses) == 1
            else "MIXED"
        ),
        "energyStatus": (
            "NOT_RECORDED"
            if not energy_statuses
            else next(iter(energy_statuses))
            if len(energy_statuses) == 1
            else "MIXED"
        ),
        "proposerWallTimeStatus": "NOT_APPLICABLE_FIXED_ENUMERATION",
        "limitation": (
            "Energy values are active-duration proxy estimates with recorded coefficient "
            "bounds, not meter readings or carbon values. Memory is the maximum sampled "
            "single-process footprint and may miss between-sample peaks."
        ),
    }


_PROCESS_TREE_EXIT_KEYS = {
    "process_id",
    "process_group_id",
    "process_identity",
    "launched_at",
    "termination_reason",
    "tree_exit_confirmed",
    "confirmation_method",
    "confirmed_at",
}

_PROCESS_INTERVAL_OVERLAP_SCHEMA = "ridgeway-retained-process-interval-overlap/v1.0"


def _aggregate_process_tree_exits(jobs: list[dict[str, Any]]) -> dict[str, object]:
    """Independently reject missing, copied, or unconfirmed process-tree evidence."""

    identities: set[str] = set()
    methods: Counter[str] = Counter()
    for job in jobs:
        evidence = job.get("process_exit_evidence")
        try:
            if not isinstance(evidence, dict) or set(evidence) != _PROCESS_TREE_EXIT_KEYS:
                raise ValueError("field coverage")
            process_id = evidence["process_id"]
            process_group_id = evidence["process_group_id"]
            process_identity = evidence["process_identity"]
            launched_at = datetime.fromisoformat(str(evidence["launched_at"]))
            confirmed_at = datetime.fromisoformat(str(evidence["confirmed_at"]))
            if (
                isinstance(process_id, bool)
                or not isinstance(process_id, int)
                or process_id <= 0
                or isinstance(process_group_id, bool)
                or not isinstance(process_group_id, int)
                or process_group_id != process_id
                or not isinstance(process_identity, str)
                or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
                or process_identity in identities
                or launched_at.tzinfo is None
                or launched_at.utcoffset() is None
                or confirmed_at.tzinfo is None
                or confirmed_at.utcoffset() is None
                or confirmed_at < launched_at
                or evidence["termination_reason"] != "natural_exit"
                or evidence["tree_exit_confirmed"] is not True
                or evidence["confirmation_method"]
                != "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT"
            ):
                raise ValueError("invalid process exit")
        except (KeyError, TypeError, ValueError) as exc:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_TREE_EXIT_INVALID") from exc
        identities.add(process_identity)
        methods[str(evidence["confirmation_method"])] += 1
    return {
        "jobs_with_confirmed_tree_exit": len(jobs),
        "jobs_without_confirmed_tree_exit": 0,
        "unique_process_identities": len(identities),
        "confirmation_methods": dict(sorted(methods.items())),
    }


def _measure_retained_process_interval_overlap(
    jobs: list[dict[str, Any]],
) -> dict[str, object]:
    """Recompute overlap among retained intervals without inferring concurrency."""

    events: list[tuple[datetime, int, int]] = []
    try:
        for job in jobs:
            evidence = job.get("process_exit_evidence")
            if not isinstance(evidence, dict) or set(evidence) != _PROCESS_TREE_EXIT_KEYS:
                raise ValueError("field coverage")
            launched_at = datetime.fromisoformat(str(evidence["launched_at"]))
            confirmed_at = datetime.fromisoformat(str(evidence["confirmed_at"]))
            if (
                launched_at.tzinfo is None
                or launched_at.utcoffset() is None
                or confirmed_at.tzinfo is None
                or confirmed_at.utcoffset() is None
                or confirmed_at < launched_at
            ):
                raise ValueError("invalid process interval")
            # Conservatively count coincident endpoints as overlapping because
            # the retained timestamp cannot establish their event order.
            events.append((launched_at, 0, 1))
            events.append((confirmed_at, 1, -1))
    except (KeyError, TypeError, ValueError) as exc:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_INTERVAL_OVERLAP_INVALID") from exc

    active = 0
    observed_maximum = 0
    for _timestamp, _event_order, delta in sorted(events):
        active += delta
        if active < 0:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_INTERVAL_OVERLAP_INVALID")
        observed_maximum = max(observed_maximum, active)
    if active != 0 or observed_maximum < (1 if jobs else 0):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_INTERVAL_OVERLAP_INVALID")
    return {
        "schema_version": _PROCESS_INTERVAL_OVERLAP_SCHEMA,
        "observation_method": "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS",
        "maximum_overlapping_retained_process_intervals": observed_maximum,
        "retained_interval_count": len(jobs),
        "coincident_endpoint_policy": "COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION",
        "scientific_identity_role": "AUDIT_ONLY_EXCLUDED",
        "limitation": (
            "Derived only from retained post-spawn launched_at-to-confirmed_at measurement "
            "intervals. It may understate true process concurrency because launched_at is "
            "recorded after process creation, and coincident endpoints may overstate interval "
            "overlap at retained timestamp resolution. It is not the configured worker count, "
            "CPU utilization, or proof of simultaneous physical execution."
        ),
    }


def _validate_verified_projection(
    projection: dict[str, Any], manifest: dict[str, Any], fixture: Any, manifest_digest: str
) -> None:
    """Require the complete current replay contract before exposing any outcome."""

    scenario_ids = {item.scenario_id for item in fixture.bundle.scenarios}
    hypothesis_ids = {item.hypothesis_id for item in fixture.bundle.resistance_hypotheses}
    study = projection.get("study")
    portfolios = projection.get("portfolios")
    current_rasters = projection.get("currentRasters")
    review_mask = projection.get("reviewMask")
    selection = manifest.get("teaching_selection")
    if (
        projection.get("schemaVersion") != UI_PROJECTION_SCHEMA
        or projection.get("fixtureId") != fixture.bundle.project_id
        or projection.get("baselinePortfolioId") != fixture.baseline_portfolio_id
        or projection.get("releaseReplayEligible") is not True
        or projection.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
        or projection.get("manifestSha256") != manifest_digest
        or not isinstance(study, dict)
        or study.get("portfolioCount") != 128
        or study.get("scenarioCount") != 12
        or study.get("matrixJobCount") != 1_536
        or study.get("mapJobCount") != 60
        or study.get("hypothesisRankingMetric") != HYPOTHESIS_RANKING_METRIC
        or not isinstance(projection.get("resourceObservability"), dict)
        or not isinstance(portfolios, list)
        or len(portfolios) != 4
        or not isinstance(current_rasters, list)
        or len(current_rasters) != 60
        or not isinstance(review_mask, dict)
        or not isinstance(selection, dict)
    ):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
    expected_ids = {
        selection.get("low_cost_caution_portfolio_id"),
        selection.get("carry_forward_portfolio_id"),
        selection.get("over_budget_portfolio_id"),
        selection.get("hypothesis_collapse_portfolio_id"),
    }
    observed_ids: set[str] = set()
    for portfolio in portfolios:
        if not isinstance(portfolio, dict) or not isinstance(portfolio.get("id"), str):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        portfolio_id = str(portfolio["id"])
        observed_ids.add(portfolio_id)
        ranks = portfolio.get("hypothesisRanks")
        results = portfolio.get("scenarioResults")
        if (
            not isinstance(ranks, dict)
            or set(ranks) != hypothesis_ids
            or not isinstance(results, list)
            or len(results) != len(scenario_ids)
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        for observation in ranks.values():
            if (
                not isinstance(observation, dict)
                or set(observation) != {"rank", "score"}
                or isinstance(observation["rank"], bool)
                or not isinstance(observation["rank"], int)
                or not 1 <= observation["rank"] <= 128
                or isinstance(observation["score"], bool)
                or not isinstance(observation["score"], (int, float))
                or not math.isfinite(float(observation["score"]))
            ):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        result_ids = {
            str(result.get("scenarioId", "")) for result in results if isinstance(result, dict)
        }
        if result_ids != scenario_ids or any(
            not isinstance(result, dict)
            or result.get("runId") != f"matrix_{portfolio_id}_{result.get('scenarioId', '')}"
            for result in results
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        scenario_by_id = {item.scenario_id: item for item in fixture.bundle.scenarios}
        for result in results:
            scenario_id = str(result["scenarioId"])
            scenario = scenario_by_id[scenario_id]
            pair_results = result.get("pairResults")
            if (
                result.get("baselineMapRunId")
                != f"map_{fixture.baseline_portfolio_id}_{scenario_id}"
                or result.get("portfolioMapRunId") != f"map_{portfolio_id}_{scenario_id}"
                or not isinstance(pair_results, list)
                or len(pair_results) != len(scenario.included_pair_ids)
            ):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
            pair_ids: set[str] = set()
            for pair in pair_results:
                if not isinstance(pair, dict):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
                pair_id = str(pair.get("pairId", ""))
                if (
                    pair_id in pair_ids
                    or pair_id not in scenario.included_pair_ids
                    or pair.get("claimId") != f"ECO-PAIR-{portfolio_id}-{scenario_id}-{pair_id}"
                    or pair.get("baselineRunId")
                    != f"matrix_{fixture.baseline_portfolio_id}_{scenario_id}"
                    or pair.get("portfolioRunId") != f"matrix_{portfolio_id}_{scenario_id}"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(pair.get("baselineMatrixSha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(pair.get("portfolioMatrixSha256", "")))
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
                pair_ids.add(pair_id)
    if (
        observed_ids != expected_ids
        or fixture.baseline_portfolio_id in observed_ids
        or None in expected_ids
    ):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
    expected_map_ids = {
        f"map_{portfolio_id}_{scenario_id}"
        for portfolio_id in {fixture.baseline_portfolio_id, *observed_ids}
        for scenario_id in scenario_ids
    }
    observed_map_ids: set[str] = set()
    for raster in current_rasters:
        if not isinstance(raster, dict):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        run_id = str(raster.get("runId", ""))
        width = raster.get("width")
        height = raster.get("height")
        cells = raster.get("cells")
        if (
            run_id in observed_map_ids
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
            or not isinstance(cells, list)
            or len(cells) != width * height
            or not re.fullmatch(r"[0-9a-f]{64}", str(raster.get("sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(raster.get("nodataTopologySha256", "")))
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")
        observed_map_ids.add(run_id)
    if observed_map_ids != expected_map_ids:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_UNVERIFIED")


def _is_declared_nodata(value: float | None, nodata: float | None) -> bool:
    if value is None or nodata is None:
        return value is None
    if math.isnan(float(nodata)):
        return math.isnan(float(value))
    return math.isclose(float(value), float(nodata), rel_tol=0.0, abs_tol=1e-9)


def _regular_file(path: str | Path, *, maximum_bytes: int) -> Path:
    candidate = Path(path).expanduser()
    metadata = os.lstat(candidate)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_NOT_REGULAR")
    if metadata.st_size < 1 or metadata.st_size > maximum_bytes:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_SIZE_INVALID")
    return candidate.resolve(strict=True)


def _required_sha(value: object, *, label: str) -> str:
    rendered = str(value)
    if len(rendered) != 64 or any(character not in "0123456789abcdef" for character in rendered):
        raise GuidedEvidenceError(f"GUIDED_EVIDENCE_{label}_SHA256_INVALID")
    return rendered


def _read_json(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > maximum_bytes:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_JSON_TOO_LARGE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_JSON_INVALID")
    return value


def _validated_engine_environment_record(
    payload: bytes,
    *,
    evidence_index: dict[str, Any],
    bundle_index: dict[str, Any],
) -> EngineManifest:
    """Validate the retained environment envelope and its nested typed manifest."""

    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_MANIFEST_INVALID") from exc
    required = {
        "schema_version",
        "engine_manifest",
        "engine_manifest_sha256",
        "engine_environment_sha256",
        "engine_environment_components",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_MANIFEST_INVALID")
    try:
        typed = EngineManifest.model_validate(record["engine_manifest"])
    except ValueError as exc:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_MANIFEST_INVALID") from exc
    environment_components = record.get("engine_environment_components")
    workflow_components = evidence_index.get("workflow_components")
    environment_keys = {
        "julia_executable",
        "project",
        "manifest",
        "wrapper",
        "entrypoint",
        "julia_runtime_tree",
        "native_artifact_tree",
        "protocol",
    }
    if (
        record.get("schema_version") != "ridgeway-engine-environment/v1.0"
        or not isinstance(environment_components, dict)
        or set(environment_components) != environment_keys
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in environment_components.values()
        )
        or not isinstance(workflow_components, dict)
        or environment_components
        != {key: workflow_components.get(key) for key in sorted(environment_keys)}
        or canonical_sha256(environment_components) != record.get("engine_environment_sha256")
        or record.get("engine_environment_sha256")
        != evidence_index.get("engine_environment_sha256")
        or record.get("engine_environment_sha256") != bundle_index.get("engine_environment_sha256")
        or record.get("engine_manifest_sha256") != typed.sha256
        or typed.sha256 != evidence_index.get("engine_manifest_sha256")
        or typed.sha256 != bundle_index.get("engine_manifest_sha256")
        or evidence_index.get("engine_manifest") != typed.model_dump(mode="json")
        or typed.executable_sha256 != environment_components.get("julia_executable")
        or typed.julia_runtime_sha256 != environment_components.get("julia_runtime_tree")
        or typed.julia_manifest_sha256 != environment_components.get("manifest")
        or typed.native_artifacts_sha256 != environment_components.get("native_artifact_tree")
        or typed.wrapper_sha256 != environment_components.get("wrapper")
        or typed.parser_sha256 != workflow_components.get("python_protocol_parser")
        or typed.metric_definitions_sha256 != workflow_components.get("metric_definitions")
    ):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_IDENTITY_MISMATCH")
    return typed


def _safe_member_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value or ":" in value:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_PATH_INVALID")
    return path.as_posix()


def _stream_sha256(source: Any, *, expected_bytes: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed = 0
    for block in iter(lambda: source.read(1024 * 1024), b""):
        observed += len(block)
        if observed > MAX_EXTRACTED_BYTES:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_TOO_LARGE")
        digest.update(block)
    if expected_bytes is not None and observed != expected_bytes:
        raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_SIZE_MISMATCH")
    return digest.hexdigest(), observed


@dataclass(frozen=True, slots=True)
class GuidedEvidenceResource:
    projection_path: Path
    projection_sha256: str
    manifest_path: Path
    manifest_sha256: str
    archive_path: Path
    archive_sha256: str
    projection: dict[str, Any]
    manifest: dict[str, Any]
    bundle_index: dict[str, Any]

    @classmethod
    def verify(
        cls,
        *,
        projection_path: str | Path,
        projection_sha256: str,
        manifest_path: str | Path,
        manifest_sha256: str,
        archive_path: str | Path,
        archive_sha256: str,
    ) -> GuidedEvidenceResource:
        projection_file = _regular_file(projection_path, maximum_bytes=MAX_INDEX_BYTES)
        manifest_file = _regular_file(manifest_path, maximum_bytes=MAX_INDEX_BYTES)
        archive_file = _regular_file(archive_path, maximum_bytes=MAX_ARCHIVE_BYTES)
        projection_digest = _required_sha(projection_sha256, label="PROJECTION")
        manifest_digest = _required_sha(manifest_sha256, label="MANIFEST")
        archive_digest = _required_sha(archive_sha256, label="ARCHIVE")
        for path, expected in (
            (projection_file, projection_digest),
            (manifest_file, manifest_digest),
            (archive_file, archive_digest),
        ):
            if sha256_file(path) != expected:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_HASH_MISMATCH")

        projection = _read_json(projection_file, maximum_bytes=MAX_INDEX_BYTES)
        manifest = _read_json(manifest_file, maximum_bytes=MAX_INDEX_BYTES)
        current_fixture = build_ridgeway_fixture()
        if manifest.get("model_sha256") != current_fixture.bundle.revision.model_sha256:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MODEL_IDENTITY_MISMATCH")
        if (
            manifest.get("schema_version") != DIRECT_EVIDENCE_SCHEMA
            or manifest.get("release_replay_eligible") is not True
            or int(manifest.get("matrix_job_count", -1)) != 1_536
            or int(manifest.get("map_job_count", -1)) != 60
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MANIFEST_INCOMPLETE")
        for identity in (
            "physics_model_sha256",
            "decision_snapshot_sha256",
            "cost_basis_sha256",
            "assumption_ledger_sha256",
            "engine_manifest_sha256",
            "engine_environment_sha256",
            "workflow_manifest_sha256",
            "evidence_index_sha256",
            "claim_safety_sha256",
        ):
            _required_sha(manifest.get(identity), label=identity.upper())
        if manifest.get("resource_observability") != projection.get("resourceObservability"):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_SUMMARY_MISMATCH")
        _validate_verified_projection(projection, manifest, current_fixture, manifest_digest)

        bundle = manifest.get("evidence_bundle")
        if not isinstance(bundle, dict):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_BUNDLE_DECLARATION_MISSING")
        filename = str(bundle.get("filename", ""))
        if (
            filename != archive_file.name
            or PurePosixPath(filename).name != filename
            or bundle.get("format") != "tar+gzip"
            or int(bundle.get("byte_size", -1)) != archive_file.stat().st_size
            or _required_sha(bundle.get("sha256"), label="ARCHIVE_DECLARATION") != archive_digest
            or bundle.get("internal_index_path") != "bundle-index.json"
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_BUNDLE_DECLARATION_INVALID")

        bundle_index = cls._verify_archive(archive_file, manifest)
        for key in (
            "fixture_version",
            "model_sha256",
            "physics_model_sha256",
            "decision_snapshot_sha256",
            "cost_basis_sha256",
            "assumption_ledger_sha256",
            "engine_manifest_sha256",
            "engine_environment_sha256",
            "workflow_manifest_sha256",
            "evidence_index_sha256",
            "claim_safety_sha256",
        ):
            if bundle_index.get(key) != manifest.get(key):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_IDENTITY_MISMATCH")
        return cls(
            projection_path=projection_file,
            projection_sha256=projection_digest,
            manifest_path=manifest_file,
            manifest_sha256=manifest_digest,
            archive_path=archive_file,
            archive_sha256=archive_digest,
            projection=projection,
            manifest=manifest,
            bundle_index=bundle_index,
        )

    @staticmethod
    def _verify_archive(archive_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        bundle = manifest["evidence_bundle"]
        regular: dict[str, tarfile.TarInfo] = {}
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = archive.getmembers()
                if not members or len(members) > MAX_MEMBERS:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_MEMBER_COUNT_INVALID")
                total_bytes = 0
                for member in members:
                    name = _safe_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_SPECIAL_FILE")
                    if member.isdir():
                        continue
                    if not member.isfile() or name in regular:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_MEMBER_INVALID")
                    total_bytes += member.size
                    if member.size < 0 or total_bytes > MAX_EXTRACTED_BYTES:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_EXPANSION_LIMIT")
                    regular[name] = member
                if int(bundle.get("file_count", -1)) != len(regular):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_FILE_COUNT_MISMATCH")
                index_member = regular.get("bundle-index.json")
                if index_member is None or index_member.size > MAX_INDEX_BYTES:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_MISSING")
                source = archive.extractfile(index_member)
                if source is None:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_MISSING")
                index_bytes = source.read(MAX_INDEX_BYTES + 1)
                if len(index_bytes) > MAX_INDEX_BYTES:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_TOO_LARGE")
                index_sha = hashlib.sha256(index_bytes).hexdigest()
                if index_sha != _required_sha(
                    bundle.get("internal_index_sha256"), label="INTERNAL_INDEX"
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_HASH_MISMATCH")
                try:
                    index = json.loads(index_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_INVALID") from exc
                if not isinstance(index, dict):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_INVALID")
                files = index.get("files")
                if (
                    index.get("schema_version") != BUNDLE_EVIDENCE_SCHEMA
                    or int(index.get("matrix_job_count", -1)) != 1_536
                    or int(index.get("map_job_count", -1)) != 60
                    or not isinstance(files, list)
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INTERNAL_INDEX_INVALID")
                indexed: dict[str, dict[str, Any]] = {}
                for record in files:
                    if not isinstance(record, dict):
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_FILE_INDEX_INVALID")
                    name = _safe_member_name(str(record.get("path", "")))
                    if name == "bundle-index.json" or name in indexed:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_FILE_INDEX_INVALID")
                    size = int(record.get("byte_size", -1))
                    digest = _required_sha(record.get("sha256"), label="MEMBER")
                    role = str(record.get("role", "")).strip()
                    if size < 0 or not role:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_FILE_INDEX_INVALID")
                    indexed[name] = {
                        **record,
                        "byte_size": size,
                        "sha256": digest,
                        "role": role,
                    }
                fixture = build_ridgeway_fixture()
                for key, expected in (
                    ("fixture_version", fixture.fixture_version),
                    ("model_sha256", fixture.bundle.revision.model_sha256),
                    ("physics_model_sha256", fixture.physics_model_sha256),
                    ("decision_snapshot_sha256", fixture.decision_snapshot_sha256),
                    ("cost_basis_sha256", fixture.cost_basis_sha256),
                    ("assumption_ledger_sha256", fixture.assumption_ledger_sha256),
                ):
                    if index.get(key) != expected:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_IDENTITY_MISMATCH")
                coverage = index.get("required_coverage")
                if not isinstance(coverage, dict):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_SEMANTIC_COVERAGE_INVALID")
                workflow_count = coverage.get("workflow_snapshots")
                if not isinstance(workflow_count, int) or workflow_count < 1:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_SEMANTIC_COVERAGE_INVALID")
                expected_required = {
                    **_FIXED_REQUIRED_COVERAGE,
                    "workflow_snapshots": workflow_count,
                }
                if index.get("required_coverage") != expected_required:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_SEMANTIC_COVERAGE_INVALID")
                expected_roles = Counter(
                    {**_FIXED_ROLE_COUNTS, "workflow_snapshot": workflow_count}
                )
                observed_roles = Counter(item["role"] for item in indexed.values())
                if observed_roles != expected_roles or index.get("role_counts") != dict(
                    sorted(expected_roles.items())
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_SEMANTIC_ROLE_COVERAGE_INVALID")
                expected_job_order = [
                    f"matrix_{portfolio.portfolio_id}_{scenario.scenario_id}"
                    for portfolio in fixture.portfolios
                    for scenario in fixture.bundle.scenarios
                ]
                search_trace = index.get("search_trace")
                if (
                    not isinstance(search_trace, dict)
                    or search_trace.get("method") != "FIXED_128_PORTFOLIO_DIRECT_STUDY"
                    or search_trace.get("portfolio_order")
                    != [item.portfolio_id for item in fixture.portfolios]
                    or search_trace.get("scenario_order")
                    != [item.scenario_id for item in fixture.bundle.scenarios]
                    or search_trace.get("job_count") != 1_536
                    or search_trace.get("job_order_sha256") != canonical_sha256(expected_job_order)
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_SEARCH_TRACE_INVALID")
                evidence_records = [
                    (name, record)
                    for name, record in indexed.items()
                    if record["role"] == "evidence_index"
                ]
                if len(evidence_records) != 1:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_MISSING")
                evidence_member = regular[evidence_records[0][0]]
                evidence_stream = archive.extractfile(evidence_member)
                if evidence_stream is None:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_MISSING")
                if evidence_member.size > MAX_INDEX_BYTES:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_TOO_LARGE")
                try:
                    evidence_index = json.loads(evidence_stream.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_INVALID") from exc
                matrix_jobs = evidence_index.get("matrix_jobs")
                map_jobs = evidence_index.get("map_jobs")
                if (
                    not isinstance(matrix_jobs, list)
                    or len(matrix_jobs) != 1_536
                    or not all(isinstance(item, dict) for item in matrix_jobs)
                    or not isinstance(map_jobs, list)
                    or len(map_jobs) != 60
                    or not all(isinstance(item, dict) for item in map_jobs)
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_JOB_COVERAGE_INCOMPLETE")
                resource_observability = _aggregate_resource_observability(
                    [*matrix_jobs, *map_jobs]
                )
                if resource_observability != manifest.get(
                    "resource_observability"
                ) or resource_observability != index.get("resource_observability"):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_RESOURCE_SUMMARY_MISMATCH")
                process_tree_exit_evidence = _aggregate_process_tree_exits(
                    [*matrix_jobs, *map_jobs]
                )
                if (
                    process_tree_exit_evidence != manifest.get("process_tree_exit_evidence")
                    or process_tree_exit_evidence != index.get("process_tree_exit_evidence")
                    or process_tree_exit_evidence
                    != evidence_index.get("process_tree_exit_evidence")
                ):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_TREE_EXIT_SUMMARY_MISMATCH")
                process_interval_overlap = _measure_retained_process_interval_overlap(
                    [*matrix_jobs, *map_jobs]
                )
                if process_interval_overlap != manifest.get(
                    "retained_process_interval_overlap"
                ) or process_interval_overlap != index.get("retained_process_interval_overlap"):
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_PROCESS_INTERVAL_OVERLAP_MISMATCH")
                engine_records = [
                    (name, record)
                    for name, record in indexed.items()
                    if record["role"] == "typed_engine_manifest"
                ]
                if len(engine_records) != 1:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_MANIFEST_MISSING")
                engine_member = regular[engine_records[0][0]]
                engine_stream = archive.extractfile(engine_member)
                if engine_stream is None:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_ENGINE_MANIFEST_MISSING")
                _validated_engine_environment_record(
                    engine_stream.read(),
                    evidence_index=evidence_index,
                    bundle_index=index,
                )
                if set(regular) != {"bundle-index.json", *indexed}:
                    raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_COVERAGE_MISMATCH")
                for name, record in indexed.items():
                    member = regular[name]
                    if member.size != record["byte_size"]:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_SIZE_MISMATCH")
                    source = archive.extractfile(member)
                    if source is None:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_MISSING")
                    digest, _ = _stream_sha256(source, expected_bytes=member.size)
                    if digest != record["sha256"]:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_HASH_MISMATCH")
        except (tarfile.TarError, OSError) as exc:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_ARCHIVE_INVALID") from exc
        return index

    def materialize(self, destination_parent: str | Path) -> Path:
        parent = Path(destination_parent).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / self.archive_sha256
        if destination.exists():
            self._verify_materialized(destination)
            return destination
        temporary = Path(tempfile.mkdtemp(prefix=".ridgeway-evidence-", dir=parent))
        try:
            with tarfile.open(self.archive_path, mode="r:gz") as archive:
                for member in archive.getmembers():
                    name = _safe_member_name(member.name)
                    target = temporary.joinpath(*PurePosixPath(name).parts)
                    if temporary not in target.resolve().parents:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_EXTRACTION_ESCAPE")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_MEMBER_MISSING")
                    descriptor = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
            marker = {
                "schema_version": "ridgeway-materialized-evidence/v1.0",
                "archive_sha256": self.archive_sha256,
                "manifest_sha256": self.manifest_sha256,
                "internal_index_sha256": self.manifest["evidence_bundle"]["internal_index_sha256"],
            }
            marker_path = temporary / _MARKER
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with marker_path.open("rb") as source:
                os.fsync(source.fileno())
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                self._verify_materialized(destination)
            self._verify_materialized(destination)
            return destination
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _verify_materialized(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MATERIALIZATION_INVALID")
        marker = _read_json(root / _MARKER, maximum_bytes=64 * 1024)
        if (
            marker.get("archive_sha256") != self.archive_sha256
            or marker.get("manifest_sha256") != self.manifest_sha256
            or marker.get("internal_index_sha256")
            != self.manifest["evidence_bundle"]["internal_index_sha256"]
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MATERIALIZATION_IDENTITY_MISMATCH")
        expected = {str(record["path"]): record for record in self.bundle_index["files"]}
        expected["bundle-index.json"] = {
            "sha256": self.manifest["evidence_bundle"]["internal_index_sha256"],
            "byte_size": (root / "bundle-index.json").stat().st_size,
        }
        observed: set[str] = set()
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == _MARKER:
                continue
            if path.is_symlink() or not path.is_file() or relative not in expected:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MATERIALIZATION_EXTRA_FILE")
            record = expected[relative]
            if path.stat().st_size != int(record["byte_size"]) or sha256_file(path) != str(
                record["sha256"]
            ):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MATERIALIZATION_HASH_MISMATCH")
            observed.add(relative)
        if observed != set(expected):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MATERIALIZATION_INCOMPLETE")

    def resolve_claim(self, claim_id: str, materialized_root: Path) -> dict[str, Any]:
        evidence_index_path = materialized_root / "evidence-index.json"
        if sha256_file(evidence_index_path) != self.manifest["evidence_index_sha256"]:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_INDEX_HASH_MISMATCH")
        evidence = _read_json(evidence_index_path, maximum_bytes=MAX_INDEX_BYTES)
        jobs = evidence.get("matrix_jobs")
        if not isinstance(jobs, list) or len(jobs) != 1_536:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MATRIX_COVERAGE_INCOMPLETE")
        map_jobs = evidence.get("map_jobs")
        if not isinstance(map_jobs, list) or len(map_jobs) != 60:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_COVERAGE_INCOMPLETE")
        selected: (
            tuple[
                dict[str, Any],
                dict[str, Any],
                dict[str, Any],
                str,
                dict[str, Any] | None,
            ]
            | None
        ) = None
        fixture = build_ridgeway_fixture()
        baseline_id = fixture.baseline_portfolio_id
        jobs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for job in jobs:
            if not isinstance(job, dict):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_JOB_INVALID")
            key = (str(job.get("portfolio_id", "")), str(job.get("scenario_id", "")))
            if key in jobs_by_key:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_JOB_DUPLICATE")
            jobs_by_key[key] = job
        map_jobs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for job in map_jobs:
            if not isinstance(job, dict):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_JOB_INVALID")
            key = (str(job.get("portfolio_id", "")), str(job.get("scenario_id", "")))
            if key in map_jobs_by_key:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_JOB_DUPLICATE")
            map_jobs_by_key[key] = job
        for portfolio in self.projection.get("portfolios", []):
            if not isinstance(portfolio, dict) or not portfolio.get("id"):
                continue
            for scenario_result in portfolio.get("scenarioResults", []):
                if not isinstance(scenario_result, dict):
                    continue
                expected_claim = (
                    f"ECO-CLM-{portfolio['id']}-{scenario_result.get('scenarioId', '')}"
                )
                if expected_claim == claim_id:
                    selected = (
                        portfolio,
                        scenario_result,
                        jobs_by_key[(str(portfolio["id"]), str(scenario_result["scenarioId"]))],
                        "resistance",
                        None,
                    )
                    break
                expected_map_claim = (
                    f"ECO-MAP-{portfolio['id']}-{scenario_result.get('scenarioId', '')}"
                )
                if expected_map_claim == claim_id:
                    selected = (
                        portfolio,
                        scenario_result,
                        map_jobs_by_key[(str(portfolio["id"]), str(scenario_result["scenarioId"]))],
                        "diagnostic_map",
                        None,
                    )
                    break
                for pair_result in scenario_result.get("pairResults", []):
                    if isinstance(pair_result, dict) and pair_result.get("claimId") == claim_id:
                        selected = (
                            portfolio,
                            scenario_result,
                            jobs_by_key[(str(portfolio["id"]), str(scenario_result["scenarioId"]))],
                            "pair_resistance",
                            pair_result,
                        )
                        break
                if selected is not None:
                    break
            if selected is not None:
                break
        if selected is None:
            raise KeyError(claim_id)
        portfolio, scenario_result, portfolio_job, claim_kind, selected_pair = selected
        scenario_id = str(scenario_result["scenarioId"])
        if claim_kind == "diagnostic_map":
            diagnostic_records = [
                item
                for item in evidence.get("map_diagnostic_records", [])
                if isinstance(item, dict)
                and item.get("direct_run_id") == portfolio_job.get("direct_run_id")
            ]
            if len(diagnostic_records) != 1:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_DIAGNOSTIC_MISSING")
            return self._resolve_map_claim(
                claim_id=claim_id,
                materialized_root=materialized_root,
                fixture=fixture,
                portfolio=portfolio,
                scenario_result=scenario_result,
                portfolio_job=portfolio_job,
                baseline_job=map_jobs_by_key[(baseline_id, scenario_id)],
                diagnostic_record=diagnostic_records[0],
            )
        baseline_job = jobs_by_key[(baseline_id, scenario_id)]
        if scenario_result.get("runId") != portfolio_job.get("direct_run_id"):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_RUN_MISMATCH")
        action_raster_sha256 = _required_sha(
            portfolio_job.get("action_raster_sha256"), label="ACTION_RASTER"
        )
        footprint_proof_sha256 = _required_sha(
            portfolio_job.get("footprint_proof_sha256"), label="FOOTPRINT_PROOF"
        )
        baseline_metrics = {
            str(item["pair_id"]): float(item["effective_resistance"])
            for item in baseline_job.get("pair_metrics", [])
        }
        portfolio_metrics = {
            str(item["pair_id"]): float(item["effective_resistance"])
            for item in portfolio_job.get("pair_metrics", [])
        }
        if not baseline_metrics or baseline_metrics.keys() != portfolio_metrics.keys():
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PAIR_COVERAGE_MISMATCH")
        reductions = {
            pair_id: (baseline - portfolio_metrics[pair_id]) / baseline * 100
            for pair_id, baseline in baseline_metrics.items()
            if baseline > 0
            and math.isfinite(baseline)
            and math.isfinite(portfolio_metrics[pair_id])
        }
        if reductions.keys() != baseline_metrics.keys():
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PAIR_METRIC_INVALID")
        if claim_kind == "pair_resistance":
            if selected_pair is None:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_PAIR_CLAIM_MISSING")
            pair_id = str(selected_pair.get("pairId", ""))
            if pair_id not in reductions:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_PAIR_CLAIM_MISSING")
            change = -reductions[pair_id]
            expected_claim_id = f"ECO-PAIR-{portfolio['id']}-{scenario_id}-{pair_id}"
            if (
                claim_id != expected_claim_id
                or selected_pair.get("baselineRunId") != baseline_job.get("direct_run_id")
                or selected_pair.get("portfolioRunId") != portfolio_job.get("direct_run_id")
                or selected_pair.get("baselineMatrixSha256") != baseline_job.get("matrix_sha256")
                or selected_pair.get("portfolioMatrixSha256") != portfolio_job.get("matrix_sha256")
                or not math.isclose(
                    float(selected_pair.get("baselineEffectiveResistance")),
                    baseline_metrics[pair_id],
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(selected_pair.get("portfolioEffectiveResistance")),
                    portfolio_metrics[pair_id],
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(selected_pair.get("baselineRelativeChangePct")),
                    change,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            ):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_PAIR_PROJECTION_MISMATCH")
            artifacts = [
                record
                for record in self.bundle_index["files"]
                if str(record["path"]).startswith(
                    f"{portfolio_job['artifact_directory'].rstrip('/')}/"
                )
                or str(record["path"]).startswith(
                    f"{baseline_job['artifact_directory'].rstrip('/')}/"
                )
            ]
            return {
                "claim": {
                    "claim_id": claim_id,
                    "portfolio_id": portfolio["id"],
                    "scenario_id": scenario_id,
                    "pair_id": pair_id,
                    "value": change,
                    "units": "% baseline-relative effective resistance",
                    "reducer": (
                        "(portfolio effective resistance − baseline effective resistance) "
                        "÷ baseline effective resistance × 100"
                    ),
                    "direct_run_ids": [
                        str(portfolio_job["direct_run_id"]),
                        str(baseline_job["direct_run_id"]),
                    ],
                    "raster_diff": {
                        "node_id": f"diff:{action_raster_sha256}",
                        "sha256": action_raster_sha256,
                        "footprint_proof_sha256": footprint_proof_sha256,
                    },
                },
                "direct_runs": [portfolio_job, baseline_job],
                "artifact_records": artifacts,
                "engine_manifest_sha256": self.manifest["engine_manifest_sha256"],
                "workflow_manifest_sha256": self.manifest["workflow_manifest_sha256"],
                "evidence_index_sha256": self.manifest["evidence_index_sha256"],
                "qa": "ACCEPTED_DIRECT_CIRCUITSCAPE",
                "limitations": [self.projection["disclaimer"]],
            }
        controlling_pair, worst_reduction = min(
            reductions.items(), key=lambda item: (item[1], item[0])
        )
        displayed_change = float(scenario_result["resistanceChangePct"])
        if not math.isclose(displayed_change, -worst_reduction, rel_tol=1e-9, abs_tol=1e-9):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_PROJECTION_VALUE_MISMATCH")
        artifacts = [
            record
            for record in self.bundle_index["files"]
            if str(record["path"]).startswith(f"{portfolio_job['artifact_directory'].rstrip('/')}/")
            or str(record["path"]).startswith(f"{baseline_job['artifact_directory'].rstrip('/')}/")
        ]
        return {
            "claim": {
                "claim_id": claim_id,
                "portfolio_id": portfolio["id"],
                "scenario_id": scenario_id,
                "pair_id": controlling_pair,
                "value": displayed_change,
                "units": "% baseline-relative effective resistance",
                "reducer": (
                    "(portfolio effective resistance − baseline effective resistance) "
                    "÷ baseline effective resistance × 100"
                ),
                "direct_run_ids": [
                    str(portfolio_job["direct_run_id"]),
                    str(baseline_job["direct_run_id"]),
                ],
                "raster_diff": {
                    "node_id": f"diff:{action_raster_sha256}",
                    "sha256": action_raster_sha256,
                    "footprint_proof_sha256": footprint_proof_sha256,
                },
            },
            "direct_runs": [portfolio_job, baseline_job],
            "artifact_records": artifacts,
            "engine_manifest_sha256": self.manifest["engine_manifest_sha256"],
            "workflow_manifest_sha256": self.manifest["workflow_manifest_sha256"],
            "evidence_index_sha256": self.manifest["evidence_index_sha256"],
            "qa": "ACCEPTED_DIRECT_CIRCUITSCAPE",
            "limitations": [self.projection["disclaimer"]],
        }

    def _resolve_map_claim(
        self,
        *,
        claim_id: str,
        materialized_root: Path,
        fixture: Any,
        portfolio: dict[str, Any],
        scenario_result: dict[str, Any],
        portfolio_job: dict[str, Any],
        baseline_job: dict[str, Any],
        diagnostic_record: dict[str, Any],
    ) -> dict[str, Any]:
        scenario_id = str(scenario_result["scenarioId"])
        expected_run_ids = (
            f"map_{portfolio['id']}_{scenario_id}",
            f"map_{fixture.baseline_portfolio_id}_{scenario_id}",
        )
        if (
            portfolio_job.get("direct_run_id") != expected_run_ids[0]
            or baseline_job.get("direct_run_id") != expected_run_ids[1]
        ):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_RUN_MISMATCH")
        for job in (portfolio_job, baseline_job):
            if (
                job.get("state") != "SUCCEEDED"
                or job.get("exact") is not True
                or job.get("test_only") is True
                or job.get("engine_name") != "Circuitscape"
                or job.get("qa_status") != "ACCEPTED"
            ):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_RUN_NOT_ACCEPTED")

        root = materialized_root.resolve()

        def current_path(job: dict[str, Any]) -> Path:
            relative = _safe_member_name(
                f"{str(job['artifact_directory']).rstrip('/')}/outputs/connectivity_cum_curmap.asc"
            )
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file() or path.is_symlink():
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_PATH_INVALID")
            if sha256_file(path) != job.get("current_map_sha256"):
                raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_HASH_MISMATCH")
            return path

        mask_contract = fixture.bundle.review_masks[0]
        mask_asset = next(
            item for item in fixture.bundle.assets if item.asset_id == mask_contract.asset_id
        )
        mask_records = [
            record
            for record in self.bundle_index["files"]
            if record.get("role") == "risk_review_mask"
        ]
        if len(mask_records) != 1:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_REVIEW_MASK_MISSING")
        mask_path = (root / _safe_member_name(str(mask_records[0]["path"]))).resolve()
        if root not in mask_path.parents or mask_path.is_symlink() or not mask_path.is_file():
            raise GuidedEvidenceError("GUIDED_EVIDENCE_REVIEW_MASK_PATH_INVALID")
        try:
            mask = load_esri_ascii(mask_path, mask_asset)
            normalized_mask: list[list[int]] = []
            for row in mask.values:
                normalized_row: list[int] = []
                for value in row:
                    if value is None or not float(value).is_integer() or int(value) not in {0, 1}:
                        raise GuidedEvidenceError("GUIDED_EVIDENCE_REVIEW_MASK_INVALID")
                    normalized_row.append(int(value))
                normalized_mask.append(normalized_row)
            if canonical_sha256(normalized_mask) != mask_asset.sha256:
                raise GuidedEvidenceError("GUIDED_EVIDENCE_REVIEW_MASK_IDENTITY_MISMATCH")
            comparison = compare_current_maps_in_mask(
                current_path(baseline_job), current_path(portfolio_job), mask
            )
        except RasterIOError as exc:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_GRID_INVALID") from exc
        if comparison.baseline_total <= 0:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_BASELINE_EMPTY")
        change = (
            (comparison.portfolio_total - comparison.baseline_total)
            / comparison.baseline_total
            * 100
        )
        expected_diagnostic = {
            "mask_id": mask_contract.mask_id,
            "baseline_total_current": comparison.baseline_total,
            "portfolio_total_current": comparison.portfolio_total,
            "relative_change_percent": change,
            "review_triggered": change > mask_contract.review_trigger_percent,
            "reviewed_cell_count": comparison.reviewed_cell_count,
            "included_cell_count": comparison.included_cell_count,
            "excluded_nodata_cell_count": comparison.excluded_nodata_cell_count,
            "raster_nodata_cell_count": comparison.raster_nodata_cell_count,
            "nodata_topology_sha256": comparison.nodata_topology_sha256,
            "interpretation": "Diagnostic current redistribution; not predicted animal use.",
        }
        if diagnostic_record.get("summaries") != [expected_diagnostic]:
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_DIAGNOSTIC_MISMATCH")
        displayed_change = float(scenario_result["reviewMaskChangePct"])
        if not math.isclose(change, displayed_change, rel_tol=1e-9, abs_tol=1e-9):
            raise GuidedEvidenceError("GUIDED_EVIDENCE_MAP_VALUE_MISMATCH")
        artifacts = [
            record
            for record in self.bundle_index["files"]
            if str(record["path"]).startswith(
                f"{str(portfolio_job['artifact_directory']).rstrip('/')}/"
            )
            or str(record["path"]).startswith(
                f"{str(baseline_job['artifact_directory']).rstrip('/')}/"
            )
        ]
        return {
            "claim": {
                "claim_id": claim_id,
                "portfolio_id": portfolio["id"],
                "scenario_id": scenario_id,
                "pair_id": None,
                "value": displayed_change,
                "units": "% diagnostic review-mask current redistribution",
                "reducer": (
                    "(portfolio mask-summed current − baseline mask-summed current) "
                    "÷ baseline mask-summed current × 100"
                ),
                "direct_run_ids": list(expected_run_ids),
            },
            "direct_runs": [portfolio_job, baseline_job],
            "artifact_records": artifacts,
            "map_descriptors": [
                {
                    "descriptor_id": f"map:{job['direct_run_id']}",
                    "label": "Portfolio current raster"
                    if job is portfolio_job
                    else "Within-scenario baseline current raster",
                    "sha256": job["current_map_sha256"],
                }
                for job in (portfolio_job, baseline_job)
            ],
            "engine_manifest_sha256": self.manifest["engine_manifest_sha256"],
            "workflow_manifest_sha256": self.manifest["workflow_manifest_sha256"],
            "evidence_index_sha256": self.manifest["evidence_index_sha256"],
            "qa": "ACCEPTED_DIRECT_CIRCUITSCAPE_DIAGNOSTIC",
            "limitations": [
                self.projection["disclaimer"],
                "Current-density maps are diagnostic visualizations, not objectives or animal-use predictions.",
            ],
        }


__all__ = ["GuidedEvidenceError", "GuidedEvidenceResource"]
