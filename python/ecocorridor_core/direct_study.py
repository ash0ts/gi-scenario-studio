"""Resumable direct Circuitscape generation for the synthetic Ridgeway fixture."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import stat
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .cache_identity import ridgeway_model_run_cache_key
from .contracts import (
    ContractModel,
    DiagnosticMapSummary,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    RasterGrid,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    RuleDisposition,
    RuleMetric,
    Sha256Digest,
    canonical_sha256,
)
from .engine import (
    EngineProtocolError,
    EngineProtocolResult,
    PreparedPair,
    contained_regular_file,
    parse_circuitscape_matrix,
    sha256_file,
    sha256_tree,
    validate_effective_circuitscape_configuration,
)
from .fixture_pipeline import (
    MaterializedFixtureFile,
    materialize_ridgeway_ascii,
    render_esri_ascii,
    ridgeway_model_snapshot_payload,
)
from .footprints import (
    canonical_footprint_proof_document_bytes,
    create_footprint_proof_document,
    footprint_proof_set_sha256,
    prove_action_effect_footprints,
    verify_footprint_proof_document,
)
from .grid import InMemoryRaster
from .interventions import (
    apply_absolute_replacements,
    portfolio_cost,
    scenario_effects_for_portfolio,
    unresolved_portfolio_concerns,
)
from .metrics import evaluate_portfolio
from .raster_io import (
    RasterIOError,
    compare_current_maps_in_mask,
    current_map_values_and_nodata,
)
from .resource_observability import run_observed_process
from .ridgeway import (
    FIXED_TIME,
    RidgewayFixture,
    TeachingPortfolioSelection,
    build_ridgeway_fixture,
)
from .semantic_diff import (
    canonical_semantic_raster_diff_bytes,
    semantic_raster_diff_payload,
    verify_semantic_raster_diff,
)


class DirectStudyError(RuntimeError):
    pass


class SuccessfulProcessTreeExit(ContractModel):
    """Persisted proof that a successful direct-study process group is absent."""

    process_id: int = Field(gt=0)
    process_group_id: int = Field(gt=0)
    process_identity: Sha256Digest
    launched_at: AwareDatetime
    termination_reason: Literal["natural_exit"]
    tree_exit_confirmed: Literal[True]
    confirmation_method: Literal["POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT"]
    confirmed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_isolated_group_exit(self) -> SuccessfulProcessTreeExit:
        if self.process_group_id != self.process_id:
            raise ValueError("direct-study process group must use its isolated child PID")
        if self.confirmed_at < self.launched_at:
            raise ValueError("process-tree exit cannot precede process launch")
        return self


class ExactFixtureJob(DirectConnectivityRun):
    phase: Literal["matrix", "map"]
    request_sha256: str
    action_raster_sha256: str
    prepared_resistance_sha256: str
    footprint_proof_sha256: str
    footprint_proof_document_sha256: str
    workflow_manifest_sha256: str
    engine_environment_sha256: str
    physics_model_sha256: str
    decision_snapshot_sha256: str
    artifact_directory: str
    process_exit_evidence: SuccessfulProcessTreeExit
    cached: bool = False


@dataclass(frozen=True)
class ExpectedFixtureJobIdentity:
    direct_run_id: str
    revision_id: str
    portfolio_id: str
    scenario_id: str
    phase: Literal["matrix", "map"]
    engine_manifest_sha256: str
    engine_environment_sha256: str
    workflow_manifest_sha256: str
    physics_model_sha256: str
    decision_snapshot_sha256: str
    model_run_cache_key: str
    request_sha256: str
    action_raster_sha256: str
    prepared_resistance_sha256: str
    footprint_proof_sha256: str
    footprint_proof_document_sha256: str
    pair_ids: frozenset[str]


class RidgewayStudyResult(ContractModel):
    state: Literal["INCOMPLETE", "COMPLETED"]
    matrix_jobs_completed: int = Field(ge=0)
    map_jobs_completed: int = Field(ge=0)
    cached_jobs: int = Field(ge=0)
    engine_manifest_sha256: str
    workflow_manifest_sha256: str | None = None
    evidence_index_sha256: str | None = None
    teaching_selection: TeachingPortfolioSelection | None = None
    public_projection_path: Path | None = None
    public_evidence_manifest_path: Path | None = None


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _json(path: Path, value: object) -> None:
    _atomic_text(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _workflow_python_sources() -> dict[str, Path]:
    """Return every Python source that can change retained Ridgeway evidence."""

    repository_root = Path(__file__).resolve().parents[2]
    return {
        "contracts": Path(__file__).with_name("contracts.py"),
        "fixture_definition": Path(__file__).with_name("ridgeway.py"),
        "grid_model": Path(__file__).with_name("grid.py"),
        "input_materializer": Path(__file__).with_name("fixture_pipeline.py"),
        "model_run_cache_identity": Path(__file__).with_name("cache_identity.py"),
        "study_orchestrator": Path(__file__),
        "python_protocol_parser": Path(__file__).with_name("engine.py"),
        "footprint_authorization": Path(__file__).with_name("footprints.py"),
        "metric_definitions": Path(__file__).with_name("metrics.py"),
        "intervention_mutator": Path(__file__).with_name("interventions.py"),
        "current_raster_parser": Path(__file__).with_name("raster_io.py"),
        "resource_observability": Path(__file__).with_name("resource_observability.py"),
        "semantic_raster_diff": Path(__file__).with_name("semantic_diff.py"),
        "python_project_manifest": repository_root / "pyproject.toml",
        "python_dependency_lock": repository_root / "uv.lock",
    }


def _python_dependency_versions() -> dict[str, str]:
    try:
        return {
            package: importlib.metadata.version(package)
            for package in ("pydantic", "pydantic-core")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise DirectStudyError(f"required scientific dependency is unavailable: {exc}") from exc


def _process_tree_exit_summary(jobs: list[ExactFixtureJob]) -> dict[str, object]:
    methods = Counter(item.process_exit_evidence.confirmation_method for item in jobs)
    identities = {item.process_exit_evidence.process_identity for item in jobs}
    if len(identities) != len(jobs):
        raise DirectStudyError("direct jobs do not have unique process-execution identities")
    return {
        "jobs_with_confirmed_tree_exit": len(jobs),
        "jobs_without_confirmed_tree_exit": 0,
        "unique_process_identities": len(identities),
        "confirmation_methods": dict(sorted(methods.items())),
    }


def _ensure_workflow_snapshot(
    evidence_root: Path,
    workflow_components: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Retain the exact inspectable bytes named by the workflow manifest."""

    snapshot_root = evidence_root / "workflow-snapshot"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, str]] = {}
    for label, source in sorted(_workflow_python_sources().items()):
        expected = workflow_components.get(label)
        if expected is None or sha256_file(source) != expected:
            raise DirectStudyError(f"workflow component identity is inconsistent: {label}")
        suffix = source.suffix or ".bin"
        target = snapshot_root / f"{label}{suffix}"
        if not target.is_file() or sha256_file(target) != expected:
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != expected:
                raise DirectStudyError(f"workflow snapshot copy differs: {label}")
            temporary.replace(target)
        records[label] = {
            "relative_path": target.relative_to(evidence_root).as_posix(),
            "sha256": expected,
        }
    dependency_payload = (
        json.dumps(
            {
                "schema_version": "ridgeway-python-dependencies/v1.0",
                "packages": _python_dependency_versions(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    dependency_digest = _sha_bytes(dependency_payload)
    if workflow_components.get("python_dependency_versions") != dependency_digest:
        raise DirectStudyError("workflow dependency-version identity is inconsistent")
    dependency_target = snapshot_root / "python-dependency-versions.json"
    if not dependency_target.is_file() or sha256_file(dependency_target) != dependency_digest:
        temporary = dependency_target.with_suffix(".json.tmp")
        temporary.write_bytes(dependency_payload)
        temporary.replace(dependency_target)
    records["python_dependency_versions"] = {
        "relative_path": dependency_target.relative_to(evidence_root).as_posix(),
        "sha256": dependency_digest,
    }
    observed = {item.name for item in snapshot_root.iterdir() if item.is_file()}
    expected_names = {Path(item["relative_path"]).name for item in records.values()}
    if observed != expected_names:
        raise DirectStudyError("workflow snapshot contains unexpected or stale files")
    return records


def _validate_engine_environment(
    julia_executable: Path,
    julia_project: Path,
    julia_depot: Path,
) -> tuple[EngineManifest, str, str, dict[str, str]]:
    required = {
        "julia_executable": julia_executable,
        "project": julia_project / "Project.toml",
        "manifest": julia_project / "Manifest.toml",
        "wrapper": julia_project / "src" / "EcoCorridorEngine.jl",
        "entrypoint": julia_project / "bin" / "ecocorridor-engine.jl",
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    if missing or not julia_depot.is_dir():
        raise DirectStudyError(f"pinned engine environment is incomplete: {missing}")
    version = subprocess.run(
        [str(julia_executable), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    if version != "julia version 1.12.6":
        raise DirectStudyError(f"expected Julia 1.12.6, observed {version!r}")
    manifest_text = required["manifest"].read_text(encoding="utf-8")
    if "[[deps.Circuitscape]]" not in manifest_text or 'version = "5.17.1"' not in manifest_text:
        raise DirectStudyError("Manifest.toml does not pin Circuitscape 5.17.1")
    julia_runtime = julia_executable.resolve().parent.parent
    native_artifacts = julia_depot / "artifacts"
    if not native_artifacts.is_dir():
        raise DirectStudyError("the pinned Julia depot contains no native artifact tree")
    python_sources = _workflow_python_sources()
    physics_components = {label: sha256_file(path) for label, path in required.items()}
    physics_components.update(
        {
            "julia_runtime_tree": sha256_tree(julia_runtime),
            "native_artifact_tree": sha256_tree(native_artifacts),
        }
    )
    physics_components["protocol"] = canonical_sha256(
        {
            "protocol_version": "1.0",
            "mode": "raster_pairwise",
            "semantics": "resistance",
            "precision": "double",
            "solver": "cg+amg",
            "neighbor_rule": "EIGHT",
            "resistance_average": "ARITHMETIC",
            "threads": 1,
            "included_pairs_dialect": "mode_include_two_columns",
            "matrix_dialect": "resistances_3columns_float_ids",
        }
    )
    engine_environment_sha256 = canonical_sha256(physics_components)
    public_manifest = EngineManifest(
        manifest_id="ridgeway-circuitscape-5.17.1-julia-1.12.6",
        executable_sha256=physics_components["julia_executable"],
        julia_runtime_sha256=physics_components["julia_runtime_tree"],
        julia_manifest_sha256=physics_components["manifest"],
        native_artifacts_sha256=physics_components["native_artifact_tree"],
        wrapper_sha256=physics_components["wrapper"],
        parser_sha256=sha256_file(Path(__file__).with_name("engine.py")),
        metric_definitions_sha256=sha256_file(Path(__file__).with_name("metrics.py")),
        neighbor_rule="EIGHT",
        resistance_average="ARITHMETIC",
        platform=f"{platform.system()}-{platform.machine()}",
        created_at=FIXED_TIME,
    )
    workflow_components = {
        **physics_components,
        "engine_manifest_contract": public_manifest.sha256,
        **{label: sha256_file(path) for label, path in python_sources.items()},
        "python_runtime": canonical_sha256(
            {
                "implementation": platform.python_implementation(),
                "version": sys.version,
                "platform": platform.platform(),
                "machine": platform.machine(),
            }
        ),
    }
    dependency_payload = (
        json.dumps(
            {
                "schema_version": "ridgeway-python-dependencies/v1.0",
                "packages": _python_dependency_versions(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    workflow_components["python_dependency_versions"] = _sha_bytes(dependency_payload)
    workflow_manifest_sha256 = canonical_sha256(workflow_components)
    return public_manifest, engine_environment_sha256, workflow_manifest_sha256, workflow_components


def _ensure_engine_manifest_record(
    evidence_root: Path,
    manifest: EngineManifest,
    engine_environment_sha256: str,
    components: dict[str, str],
) -> dict[str, str]:
    payload = {
        "schema_version": "ridgeway-engine-environment/v1.0",
        "engine_manifest": manifest.model_dump(mode="json"),
        "engine_manifest_sha256": manifest.sha256,
        "engine_environment_sha256": engine_environment_sha256,
        "engine_environment_components": {
            key: components[key]
            for key in (
                "julia_executable",
                "project",
                "manifest",
                "wrapper",
                "entrypoint",
                "julia_runtime_tree",
                "native_artifact_tree",
                "protocol",
            )
        },
    }
    path = evidence_root / "engine-manifest.json"
    expected = _canonical_json_bytes(payload)
    if path.exists() and (path.is_symlink() or path.read_bytes() != expected):
        raise DirectStudyError("retained typed engine manifest differs from the current engine")
    if not path.exists():
        path.write_bytes(expected)
    return {
        "relative_path": path.relative_to(evidence_root).as_posix(),
        "sha256": sha256_file(path),
    }


def _ensure_engine_snapshot(evidence_root: Path, source_project: Path) -> Path:
    snapshot = evidence_root / "engine-snapshot"
    required = (
        Path("Project.toml"),
        Path("Manifest.toml"),
        Path("src/EcoCorridorEngine.jl"),
        Path("bin/ecocorridor-engine.jl"),
    )
    if not snapshot.exists():
        for relative in required:
            source = source_project / relative
            if not source.is_file():
                raise DirectStudyError(f"engine snapshot source is absent: {source}")
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    expected_paths = {item.as_posix() for item in required}
    observed_paths: set[str] = set()
    for candidate in snapshot.rglob("*"):
        metadata = os.lstat(candidate)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise DirectStudyError(f"engine snapshot contains an unsafe file: {candidate}")
        observed_paths.add(candidate.relative_to(snapshot).as_posix())
    if observed_paths != expected_paths:
        raise DirectStudyError("engine snapshot file coverage differs from the allowlist")
    for relative in required:
        target = snapshot / relative
        source = source_project / relative
        if not target.is_file():
            raise DirectStudyError(f"engine snapshot is incomplete: {relative}")
        if sha256_file(target) != sha256_file(source):
            raise DirectStudyError(
                f"engine snapshot differs from the requested project: {relative}"
            )
    return snapshot


def _ensure_sources(root: Path, fixture: RidgewayFixture) -> Path:
    sources = root / "sources"
    manifest_path = sources / "materialization-manifest.json"
    if not manifest_path.exists():
        materialize_ridgeway_ascii(sources, fixture=fixture)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["model_sha256"] != fixture.bundle.revision.model_sha256:
        raise DirectStudyError("materialized Ridgeway sources belong to another model revision")
    materializer_sha256 = sha256_file(Path(__file__).with_name("fixture_pipeline.py"))
    if manifest.get("materializer_sha256") != materializer_sha256:
        raise DirectStudyError("materialized Ridgeway sources use another materializer")
    for record in manifest["files"]:
        path = sources / record["relative_path"]
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            raise DirectStudyError(f"materialized Ridgeway source failed hash verification: {path}")
    declared_paths = {str(item["relative_path"]) for item in manifest["files"]}
    declared_paths.add("materialization-manifest.json")
    observed_paths: set[str] = set()
    for candidate in sources.rglob("*"):
        metadata = os.lstat(candidate)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise DirectStudyError(
                f"materialized Ridgeway sources contain unsafe data: {candidate}"
            )
        observed_paths.add(candidate.relative_to(sources).as_posix())
    if observed_paths != declared_paths:
        raise DirectStudyError("materialized Ridgeway source coverage differs from its manifest")
    source_records = [MaterializedFixtureFile.model_validate(item) for item in manifest["files"]]
    snapshot_path = sources / "model-decision-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected_snapshot = ridgeway_model_snapshot_payload(
        fixture,
        [item for item in source_records if item.role != "model_decision_snapshot"],
    )
    if snapshot != expected_snapshot:
        raise DirectStudyError("materialized Ridgeway model/decision snapshot is inconsistent")
    return sources


def _scenario_parts(fixture: RidgewayFixture, scenario_id: str):
    scenario = next(item for item in fixture.bundle.scenarios if item.scenario_id == scenario_id)
    focal = next(
        item
        for item in fixture.bundle.focal_area_sets
        if item.focal_set_id == scenario.focal_set_id
    )
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    return scenario, focal, hypothesis


def _load_exact_job(
    job_root: Path,
    evidence_root: Path,
    expected: ExpectedFixtureJobIdentity,
    *,
    request_payload: bytes,
    resistance_payload: bytes,
    semantic_diff_payload: bytes,
    footprint_document_payload: bytes,
    focal_source: Path,
    pair_source: Path,
    prepared_pairs: list[PreparedPair],
    expected_grid: RasterGrid,
    emit_current_map: bool,
    neighbor_rule: str,
    resistance_average: str,
) -> ExactFixtureJob | None:
    """Load exact physics or create an explicit current-evidence envelope.

    A completed solver result is reusable only after its retained request,
    normalized inputs, effective configuration, raw matrix, optional current
    raster, protocol result, and artifact allowlist are revalidated.  Human
    review metadata and cost/decision snapshots are deliberately outside the
    physics cache key.  When only that envelope changes, immutable solver
    artifacts are copied into a new attempt and a local lineage record preserves
    the exact source completion; the old completion is never relabelled in
    place.
    """

    complete_path = job_root / "complete.json"
    if not complete_path.is_file():
        return None
    if complete_path.is_symlink():
        raise DirectStudyError("completed job record cannot be a symlink")
    complete_bytes = complete_path.read_bytes()
    try:
        payload = json.loads(complete_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectStudyError("completed job record is not canonical JSON evidence") from exc
    if not isinstance(payload, dict) or set(payload) - {"job", "artifact_hashes", "cache_lineage"}:
        raise DirectStudyError("completed job record has unsupported fields")
    if not isinstance(payload.get("artifact_hashes"), dict):
        raise DirectStudyError("completed job record has no artifact hash allowlist")
    job = ExactFixtureJob.model_validate(payload.get("job"))

    canonical_prefix = (
        f"jobs/{expected.phase}/{expected.portfolio_id}/{expected.scenario_id}/attempts/"
    )
    artifact_directory = job.artifact_directory
    attempt_name = Path(artifact_directory).name
    if (
        not artifact_directory.startswith(canonical_prefix)
        or Path(artifact_directory).parent.as_posix() != canonical_prefix.rstrip("/")
        or not attempt_name.startswith("attempt-")
        or not attempt_name.removeprefix("attempt-").isdigit()
        or len(attempt_name.removeprefix("attempt-")) != 4
    ):
        raise DirectStudyError("completed job artifact directory is not canonical")

    try:
        request_path = contained_regular_file(evidence_root, f"{artifact_directory}/request.json")
        result_path = contained_regular_file(evidence_root, f"{artifact_directory}/result.json")
        resistance_path = contained_regular_file(
            evidence_root, f"{artifact_directory}/inputs/resistance.asc"
        )
        focal_path = contained_regular_file(evidence_root, f"{artifact_directory}/inputs/focal.asc")
        included_pairs_path = contained_regular_file(
            evidence_root, f"{artifact_directory}/inputs/pairs.tsv"
        )
        diff_path = contained_regular_file(
            evidence_root, f"{artifact_directory}/action-raster-diff.json"
        )
        proof_path = contained_regular_file(
            evidence_root, f"{artifact_directory}/footprint-proofs.json"
        )
        ini_path = contained_regular_file(evidence_root, f"{artifact_directory}/circuitscape.ini")
        contained_regular_file(evidence_root, f"{artifact_directory}/stdout.log")
        contained_regular_file(evidence_root, f"{artifact_directory}/stderr.log")
    except EngineProtocolError as exc:
        raise DirectStudyError("completed job contains an unsafe or absent artifact") from exc

    if (
        request_path.read_bytes() != request_payload
        or resistance_path.read_bytes() != resistance_payload
        or focal_path.read_bytes() != focal_source.read_bytes()
        or included_pairs_path.read_bytes() != pair_source.read_bytes()
    ):
        return None
    if (
        sha256_file(request_path) != job.request_sha256
        or job.input_sha256 != job.request_sha256
        or sha256_file(resistance_path) != job.prepared_resistance_sha256
        or sha256_file(diff_path) != job.action_raster_sha256
        or sha256_file(proof_path) != job.footprint_proof_document_sha256
    ):
        raise DirectStudyError("completed job provenance digests differ from retained artifacts")

    _validate_circuitscape_ini(
        ini_path,
        resistance_path=resistance_path,
        focal_path=focal_path,
        pair_path=included_pairs_path,
        output_directory=result_path.parent / "outputs",
        emit_current_map=emit_current_map,
        neighbor_rule=neighbor_rule,
        resistance_average=resistance_average,
    )
    try:
        protocol = EngineProtocolResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DirectStudyError("cached result protocol is invalid") from exc
    if protocol.status != "SUCCEEDED" or protocol.warnings:
        raise DirectStudyError("cached result is not a warning-free successful direct run")
    try:
        matrix_path = contained_regular_file(
            evidence_root, f"{artifact_directory}/{protocol.matrix_path or ''}"
        )
    except EngineProtocolError as exc:
        raise DirectStudyError("cached raw matrix is absent or unsafe") from exc
    reparsed_metrics = parse_circuitscape_matrix(matrix_path, prepared_pairs)
    protocol_metrics = sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
    if [item.model_dump(mode="json") for item in reparsed_metrics] != [
        item.model_dump(mode="json") for item in protocol_metrics
    ]:
        raise DirectStudyError("cached protocol metrics differ from its retained raw matrix")
    pair_metrics = [
        PairResistanceMetric(
            pair_id=item.pair_id,
            effective_resistance=item.effective_resistance,
        )
        for item in reparsed_metrics
    ]
    if [item.model_dump(mode="json") for item in job.pair_metrics] != [
        item.model_dump(mode="json") for item in pair_metrics
    ]:
        raise DirectStudyError("cached job metrics differ from its retained raw matrix")

    current_path: Path | None = None
    if emit_current_map:
        if not protocol.current_map_path:
            raise DirectStudyError("cached map job has no current-raster artifact")
        try:
            current_path = contained_regular_file(
                evidence_root, f"{artifact_directory}/{protocol.current_map_path}"
            )
        except EngineProtocolError as exc:
            raise DirectStudyError("cached current raster is absent or unsafe") from exc
        try:
            current_values, current_nodata = current_map_values_and_nodata(
                current_path, expected_grid=expected_grid
            )
        except RasterIOError as exc:
            raise DirectStudyError("cached current raster is invalid") from exc

        def is_nodata(value: float) -> bool:
            return (math.isnan(current_nodata) and math.isnan(value)) or math.isclose(
                value, current_nodata, rel_tol=0.0, abs_tol=1e-9
            )

        data_values = [value for row in current_values for value in row if not is_nodata(value)]
        if not data_values or any(not math.isfinite(value) or value < 0 for value in data_values):
            raise DirectStudyError("cached current raster has no valid nonnegative current data")
    elif protocol.current_map_path is not None or job.current_map_sha256 is not None:
        raise DirectStudyError("cached matrix-only job unexpectedly names a current raster")

    relative_names = {
        "request.json",
        "result.json",
        "stdout.log",
        "stderr.log",
        "action-raster-diff.json",
        "footprint-proofs.json",
        "circuitscape.ini",
        "inputs/resistance.asc",
        "inputs/focal.asc",
        "inputs/pairs.tsv",
        str(protocol.matrix_path),
    }
    if current_path is not None:
        relative_names.add(str(protocol.current_map_path))
    expected_artifacts = {f"{artifact_directory}/{relative}" for relative in relative_names}
    artifact_hashes = payload["artifact_hashes"]
    if set(artifact_hashes) != expected_artifacts:
        raise DirectStudyError("completed job artifact coverage differs from the exact allowlist")
    for relative, digest in artifact_hashes.items():
        try:
            retained = contained_regular_file(evidence_root, str(relative))
        except EngineProtocolError as exc:
            raise DirectStudyError(f"completed job artifact is unsafe: {relative}") from exc
        if not isinstance(digest, str) or sha256_file(retained) != digest:
            raise DirectStudyError(f"completed job artifact failed verification: {relative}")
    if (
        sha256_file(result_path) != job.result_sha256
        or sha256_file(matrix_path) != job.matrix_sha256
        or (sha256_file(current_path) if current_path else None) != job.current_map_sha256
        or job.state != DirectRunState.SUCCEEDED
        or not job.exact
        or job.engine_name != "Circuitscape"
        or job.qa_status != QAStatus.ACCEPTED
        or job.qa_findings
    ):
        raise DirectStudyError("completed job disposition or output digests are inconsistent")

    physical_identity = (
        job.direct_run_id,
        job.portfolio_id,
        job.scenario_id,
        job.phase,
        job.engine_environment_sha256,
        job.model_run_cache_key,
        job.request_sha256,
        job.prepared_resistance_sha256,
        frozenset(item.pair_id for item in job.pair_metrics),
    )
    expected_physical_identity = (
        expected.direct_run_id,
        expected.portfolio_id,
        expected.scenario_id,
        expected.phase,
        expected.engine_environment_sha256,
        expected.model_run_cache_key,
        expected.request_sha256,
        expected.prepared_resistance_sha256,
        expected.pair_ids,
    )
    if physical_identity != expected_physical_identity:
        return None

    observed_identity = (
        job.direct_run_id,
        job.revision_id,
        job.portfolio_id,
        job.scenario_id,
        job.phase,
        job.engine_manifest_sha256,
        job.engine_environment_sha256,
        job.workflow_manifest_sha256,
        job.physics_model_sha256,
        job.decision_snapshot_sha256,
        job.model_run_cache_key,
        job.request_sha256,
        job.input_sha256,
        job.action_raster_sha256,
        job.prepared_resistance_sha256,
        job.footprint_proof_sha256,
        job.footprint_proof_document_sha256,
        frozenset(item.pair_id for item in job.pair_metrics),
    )
    expected_identity = (
        expected.direct_run_id,
        expected.revision_id,
        expected.portfolio_id,
        expected.scenario_id,
        expected.phase,
        expected.engine_manifest_sha256,
        expected.engine_environment_sha256,
        expected.workflow_manifest_sha256,
        expected.physics_model_sha256,
        expected.decision_snapshot_sha256,
        expected.model_run_cache_key,
        expected.request_sha256,
        expected.request_sha256,
        expected.action_raster_sha256,
        expected.prepared_resistance_sha256,
        expected.footprint_proof_sha256,
        expected.footprint_proof_document_sha256,
        expected.pair_ids,
    )
    if observed_identity != expected_identity:
        source_completion_sha256 = _sha_bytes(complete_bytes)
        lineage_path = job_root / "cache-lineage" / f"{source_completion_sha256}.json"
        if lineage_path.exists() and (
            lineage_path.is_symlink() or lineage_path.read_bytes() != complete_bytes
        ):
            raise DirectStudyError("immutable cache-source completion record is inconsistent")
        if not lineage_path.exists():
            _atomic_text(lineage_path, complete_bytes.decode("utf-8"))

        attempt = _attempt_directory(job_root)
        source_attempt = evidence_root / artifact_directory
        for relative in sorted(relative_names):
            if relative in {"action-raster-diff.json", "footprint-proofs.json"}:
                continue
            source = contained_regular_file(source_attempt, relative)
            target = attempt / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (attempt / "action-raster-diff.json").write_bytes(semantic_diff_payload)
        (attempt / "footprint-proofs.json").write_bytes(footprint_document_payload)
        alias_artifact_directory = attempt.relative_to(evidence_root).as_posix()
        aliased = job.model_copy(
            update={
                "revision_id": expected.revision_id,
                "engine_manifest_sha256": expected.engine_manifest_sha256,
                "engine_environment_sha256": expected.engine_environment_sha256,
                "workflow_manifest_sha256": expected.workflow_manifest_sha256,
                "physics_model_sha256": expected.physics_model_sha256,
                "decision_snapshot_sha256": expected.decision_snapshot_sha256,
                "model_run_cache_key": expected.model_run_cache_key,
                "action_raster_sha256": expected.action_raster_sha256,
                "prepared_resistance_sha256": expected.prepared_resistance_sha256,
                "footprint_proof_sha256": expected.footprint_proof_sha256,
                "footprint_proof_document_sha256": expected.footprint_proof_document_sha256,
                "pair_metrics": pair_metrics,
                "artifact_directory": alias_artifact_directory,
                "cache_source_direct_run_id": None,
                "cached": True,
            }
        )
        alias_paths = [attempt / relative for relative in sorted(relative_names)]
        alias_complete = {
            "job": aliased.model_dump(mode="json"),
            "artifact_hashes": {
                path.relative_to(evidence_root).as_posix(): sha256_file(path)
                for path in alias_paths
            },
            "cache_lineage": {
                "source_completion_sha256": source_completion_sha256,
                "source_completion_relative_path": lineage_path.relative_to(
                    evidence_root
                ).as_posix(),
                "source_artifact_directory": artifact_directory,
                "revalidation": (
                    "Request, inputs, effective configuration, protocol, raw matrix, optional "
                    "current raster, and complete artifact allowlist were revalidated before "
                    "creating this evidence envelope."
                ),
            },
        }
        _json(job_root / "complete.json", alias_complete)
        return aliased
    if (
        diff_path.read_bytes() != semantic_diff_payload
        or proof_path.read_bytes() != footprint_document_payload
    ):
        raise DirectStudyError("cached provenance artifacts differ from the current envelope")
    return job.model_copy(update={"cached": True})


def _attempt_directory(job_root: Path) -> Path:
    attempts = job_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    while True:
        indexes = [
            int(item.name.split("-")[-1])
            for item in attempts.iterdir()
            if item.is_dir()
            and item.name.startswith("attempt-")
            and item.name.split("-")[-1].isdigit()
        ]
        attempt = attempts / f"attempt-{max(indexes, default=0) + 1:04d}"
        try:
            attempt.mkdir()
        except FileExistsError:
            continue
        break
    (attempt / "inputs").mkdir()
    (attempt / "outputs").mkdir()
    return attempt


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _validate_circuitscape_ini(
    path: Path,
    *,
    resistance_path: Path,
    focal_path: Path,
    pair_path: Path,
    output_directory: Path,
    emit_current_map: bool,
    neighbor_rule: str,
    resistance_average: str,
) -> None:
    try:
        validate_effective_circuitscape_configuration(
            path,
            resource_paths={
                "resistance": "inputs/resistance.asc",
                "focal": "inputs/focal.asc",
                "included_pairs": "inputs/pairs.tsv",
            },
            output_directory="outputs",
            neighbor_rule=neighbor_rule,
            resistance_average=resistance_average,
            emit_current_map=emit_current_map,
        )
    except EngineProtocolError as exc:
        raise DirectStudyError(
            "Circuitscape effective configuration differs from the request"
        ) from exc


def _execute_job(
    *,
    evidence_root: Path,
    sources: Path,
    fixture: RidgewayFixture,
    portfolio_id: str,
    scenario_id: str,
    phase: Literal["matrix", "map"],
    engine_manifest_sha256: str,
    engine_environment_sha256: str,
    workflow_manifest_sha256: str,
    julia_executable: Path,
    julia_project: Path,
    julia_depot: Path,
    timeout_seconds: int,
) -> ExactFixtureJob:
    job_root = evidence_root / "jobs" / phase / portfolio_id / scenario_id
    scenario, focal, hypothesis = _scenario_parts(fixture, scenario_id)
    portfolio = next(item for item in fixture.portfolios if item.portfolio_id == portfolio_id)
    baseline = fixture.rasters[hypothesis.resistance_asset_id]
    effects = scenario_effects_for_portfolio(
        portfolio, fixture.bundle.intervention_options, scenario
    )
    footprint_proofs = prove_action_effect_footprints(
        baseline,
        effects,
        fixture.bundle.assets,
        normalized_rasters=fixture.rasters,
    )
    footprint_proof_sha256 = footprint_proof_set_sha256(footprint_proofs)
    resistance = apply_absolute_replacements(
        baseline,
        effects,
        derived_asset_id=f"resistance_{portfolio_id}_{scenario_id}",
    )
    resistance_payload = render_esri_ascii(resistance)
    focal_source = sources / "focal" / f"{focal.focal_set_id}.asc"
    pair_source = sources / "pairs" / f"{scenario_id}.tsv"
    try:
        focal_source = contained_regular_file(
            evidence_root, focal_source.relative_to(evidence_root).as_posix()
        )
        pair_source = contained_regular_file(
            evidence_root, pair_source.relative_to(evidence_root).as_posix()
        )
    except (EngineProtocolError, ValueError) as exc:
        raise DirectStudyError("current focal or included-pair source is absent or unsafe") from exc
    focal_source_sha256 = sha256_file(focal_source)
    pair_source_sha256 = sha256_file(pair_source)
    pairs = {item.pair_id: item for item in focal.pairs}
    request = {
        "protocol_version": "1.0",
        "prepared_run_id": f"{phase}_{portfolio_id}_{scenario_id}",
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/pairs.tsv",
        },
        "pairs": [
            {
                "pair_id": pair_id,
                "from_focal_id": pairs[pair_id].from_focal_id,
                "to_focal_id": pairs[pair_id].to_focal_id,
            }
            for pair_id in scenario.included_pair_ids
        ],
        "settings": {
            "precision": "double",
            "neighbor_rule": scenario.neighbor_rule.value,
            "resistance_average": scenario.resistance_average.value,
            "julia_threads": 1,
            "blas_threads": 1,
            "omp_threads": 1,
            "emit_current_map": phase == "map",
        },
        "output_directory": "outputs",
    }
    request_payload = json.dumps(request, sort_keys=True, indent=2) + "\n"
    prepared_resistance_sha256 = _sha_bytes(resistance_payload.encode("utf-8"))
    footprint_source_paths = {
        proof.footprint_asset_id: sources / "footprints" / f"{proof.footprint_asset_id}.asc"
        for proof in footprint_proofs
    }
    try:
        footprint_source_paths = {
            asset_id: contained_regular_file(
                evidence_root, path.relative_to(evidence_root).as_posix()
            )
            for asset_id, path in footprint_source_paths.items()
        }
    except (EngineProtocolError, ValueError) as exc:
        raise DirectStudyError("reviewed footprint provenance is absent or unsafe") from exc
    footprint_source_sha256 = {path: sha256_file(path) for path in footprint_source_paths.values()}
    footprint_document = create_footprint_proof_document(
        project_id=fixture.bundle.project_id,
        revision_id=fixture.bundle.revision.revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        baseline=baseline,
        proofs=footprint_proofs,
        assets=fixture.bundle.assets,
        source_paths=footprint_source_paths,
        evidence_root=evidence_root,
    )
    verify_footprint_proof_document(
        footprint_document,
        evidence_root=evidence_root,
        effects=effects,
        assets=fixture.bundle.assets,
    )
    footprint_document_payload = canonical_footprint_proof_document_bytes(footprint_document)
    footprint_proof_document_sha256 = _sha_bytes(footprint_document_payload)
    semantic_diff = semantic_raster_diff_payload(
        project_id=fixture.bundle.project_id,
        revision_id=fixture.bundle.revision.revision_id,
        model_identity_sha256=fixture.physics_model_sha256,
        baseline=baseline,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        effects=effects,
        prepared_resistance_sha256=prepared_resistance_sha256,
    )
    verified_diff_sha256 = verify_semantic_raster_diff(
        semantic_diff,
        baseline=baseline,
        effects=effects,
        prepared=resistance,
        expected_project_id=fixture.bundle.project_id,
        expected_revision_id=fixture.bundle.revision.revision_id,
        expected_model_identity_sha256=fixture.physics_model_sha256,
        expected_portfolio_id=portfolio_id,
        expected_scenario_id=scenario_id,
        prepared_resistance_sha256=prepared_resistance_sha256,
    )
    diff_payload = canonical_semantic_raster_diff_bytes(semantic_diff)
    action_raster_sha256 = _sha_bytes(diff_payload)
    if action_raster_sha256 != verified_diff_sha256:
        raise DirectStudyError("semantic raster-diff hash is inconsistent")
    request_sha256 = _sha_bytes(request_payload.encode("utf-8"))
    model_run_cache_key = ridgeway_model_run_cache_key(
        mode=request["mode"],
        raster_semantics=request["raster_semantics"],
        resistance_sha256=prepared_resistance_sha256,
        focal_sha256=focal_source_sha256,
        included_pairs_sha256=pair_source_sha256,
        pairs=request["pairs"],
        settings=request["settings"],
        engine_manifest_sha256=engine_manifest_sha256,
        engine_environment_sha256=engine_environment_sha256,
        workflow_manifest_sha256=workflow_manifest_sha256,
    )
    expected = ExpectedFixtureJobIdentity(
        direct_run_id=f"{phase}_{portfolio_id}_{scenario_id}",
        revision_id=fixture.bundle.revision.revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        phase=phase,
        engine_manifest_sha256=engine_manifest_sha256,
        engine_environment_sha256=engine_environment_sha256,
        workflow_manifest_sha256=workflow_manifest_sha256,
        physics_model_sha256=fixture.physics_model_sha256,
        decision_snapshot_sha256=fixture.decision_snapshot_sha256,
        model_run_cache_key=model_run_cache_key,
        request_sha256=request_sha256,
        action_raster_sha256=action_raster_sha256,
        prepared_resistance_sha256=prepared_resistance_sha256,
        footprint_proof_sha256=footprint_proof_sha256,
        footprint_proof_document_sha256=footprint_proof_document_sha256,
        pair_ids=frozenset(scenario.included_pair_ids),
    )
    prepared_pairs = [
        PreparedPair(
            pair_id=pair_id,
            from_focal_id=pairs[pair_id].from_focal_id,
            to_focal_id=pairs[pair_id].to_focal_id,
        )
        for pair_id in scenario.included_pair_ids
    ]
    cached = _load_exact_job(
        job_root,
        evidence_root,
        expected,
        request_payload=request_payload.encode("utf-8"),
        resistance_payload=resistance_payload.encode("utf-8"),
        semantic_diff_payload=diff_payload,
        footprint_document_payload=footprint_document_payload,
        focal_source=focal_source,
        pair_source=pair_source,
        prepared_pairs=prepared_pairs,
        expected_grid=baseline.grid,
        emit_current_map=phase == "map",
        neighbor_rule=scenario.neighbor_rule.value,
        resistance_average=scenario.resistance_average.value,
    )
    if cached:
        return cached
    job_root.mkdir(parents=True, exist_ok=True)
    attempt = _attempt_directory(job_root)
    resistance_path = attempt / "inputs" / "resistance.asc"
    _atomic_text(resistance_path, resistance_payload)
    diff_path = attempt / "action-raster-diff.json"
    diff_path.write_bytes(diff_payload)
    footprint_document_path = attempt / "footprint-proofs.json"
    footprint_document_path.write_bytes(footprint_document_payload)
    focal_path = attempt / "inputs" / "focal.asc"
    pair_path = attempt / "inputs" / "pairs.tsv"
    shutil.copyfile(focal_source, focal_path)
    shutil.copyfile(pair_source, pair_path)
    request_path = attempt / "request.json"
    _atomic_text(request_path, request_payload)
    if (
        sha256_file(request_path) != request_sha256
        or sha256_file(resistance_path) != prepared_resistance_sha256
        or sha256_file(diff_path) != action_raster_sha256
        or sha256_file(footprint_document_path) != footprint_proof_document_sha256
    ):
        raise DirectStudyError("materialized direct-job inputs differ from their cache identity")
    result_path = attempt / "result.json"
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        {
            "JULIA_DEPOT_PATH": str(julia_depot),
            "JULIA_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    observed = run_observed_process(
        [
            str(julia_executable),
            f"--project={julia_project}",
            str(julia_project / "bin" / "ecocorridor-engine.jl"),
            "--request",
            request_path.name,
            "--result",
            result_path.name,
        ],
        cwd=attempt,
        env=environment,
        timeout_seconds=timeout_seconds,
        observation_id=f"resource:{phase}_{portfolio_id}_{scenario_id}",
        boundary_description=(
            "One isolated Julia/Circuitscape Ridgeway raster-pairwise job, including runtime "
            "startup, input parsing, solve, and result serialization."
        ),
    )
    try:
        process_exit_evidence = SuccessfulProcessTreeExit.model_validate(
            observed.process_exit.as_dict()
        )
    except ValueError as exc:
        raise DirectStudyError(
            "successful direct job lacks confirmed isolated process-tree exit evidence"
        ) from exc
    completed = observed.completed
    (attempt / "stdout.log").write_bytes(completed.stdout)
    (attempt / "stderr.log").write_bytes(completed.stderr)
    try:
        request_path = contained_regular_file(attempt, "request.json")
        result_path = contained_regular_file(attempt, "result.json")
        resistance_path = contained_regular_file(attempt, "inputs/resistance.asc")
        focal_path = contained_regular_file(attempt, "inputs/focal.asc")
        pair_path = contained_regular_file(attempt, "inputs/pairs.tsv")
        diff_path = contained_regular_file(attempt, "action-raster-diff.json")
        footprint_document_path = contained_regular_file(attempt, "footprint-proofs.json")
        stdout_path = contained_regular_file(attempt, "stdout.log")
        stderr_path = contained_regular_file(attempt, "stderr.log")
    except EngineProtocolError as exc:
        raise DirectStudyError(
            f"job produced an absent or unsafe retained artifact: {portfolio_id}/{scenario_id}"
        ) from exc
    if (
        sha256_file(request_path) != request_sha256
        or sha256_file(resistance_path) != prepared_resistance_sha256
        or sha256_file(focal_path) != focal_source_sha256
        or sha256_file(pair_path) != pair_source_sha256
        or sha256_file(diff_path) != action_raster_sha256
        or sha256_file(footprint_document_path) != footprint_proof_document_sha256
        or sha256_file(
            contained_regular_file(
                evidence_root, focal_source.relative_to(evidence_root).as_posix()
            )
        )
        != focal_source_sha256
        or sha256_file(
            contained_regular_file(evidence_root, pair_source.relative_to(evidence_root).as_posix())
        )
        != pair_source_sha256
        or any(
            sha256_file(
                contained_regular_file(evidence_root, path.relative_to(evidence_root).as_posix())
            )
            != expected_sha256
            for path, expected_sha256 in footprint_source_sha256.items()
        )
    ):
        raise DirectStudyError("direct-job input or provenance bytes changed during execution")
    protocol = EngineProtocolResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    if completed.returncode != 0 or protocol.status != "SUCCEEDED":
        raise DirectStudyError(
            f"direct job failed for {portfolio_id}/{scenario_id}: {protocol.failure_message}"
        )
    if protocol.warnings:
        raise DirectStudyError(
            "Ridgeway direct jobs cannot auto-accept Circuitscape warnings; record a reviewed "
            "exception in a new fixture revision"
        )
    try:
        circuitscape_ini_path = contained_regular_file(attempt, "circuitscape.ini")
    except EngineProtocolError as exc:
        raise DirectStudyError("Circuitscape configuration artifact is absent or unsafe") from exc
    _validate_circuitscape_ini(
        circuitscape_ini_path,
        resistance_path=resistance_path,
        focal_path=focal_path,
        pair_path=pair_path,
        output_directory=attempt / "outputs",
        emit_current_map=phase == "map",
        neighbor_rule=scenario.neighbor_rule.value,
        resistance_average=scenario.resistance_average.value,
    )
    observed_pairs = {item.pair_id for item in protocol.pair_metrics}
    if observed_pairs != set(scenario.included_pair_ids):
        raise DirectStudyError(f"direct job returned the wrong focal pairs: {observed_pairs}")
    try:
        matrix_path = contained_regular_file(attempt, protocol.matrix_path or "")
    except EngineProtocolError as exc:
        raise DirectStudyError("matrix artifact is absent or escaped its job directory") from exc
    reparsed_metrics = parse_circuitscape_matrix(matrix_path, prepared_pairs)
    if [item.model_dump(mode="json") for item in reparsed_metrics] != [
        item.model_dump(mode="json")
        for item in sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
    ]:
        raise DirectStudyError("result protocol pair metrics differ from the retained raw matrix")
    current_path: Path | None = None
    if phase == "map":
        try:
            current_path = contained_regular_file(attempt, protocol.current_map_path or "")
        except EngineProtocolError as exc:
            raise DirectStudyError(
                "current-map artifact is absent or escaped its job directory"
            ) from exc
    pair_metrics = [
        PairResistanceMetric(
            pair_id=item.pair_id,
            effective_resistance=item.effective_resistance,
        )
        for item in sorted(reparsed_metrics, key=lambda item: item.pair_id)
    ]
    artifact_directory = attempt.relative_to(evidence_root).as_posix()
    job = ExactFixtureJob(
        direct_run_id=f"{phase}_{portfolio_id}_{scenario_id}",
        analysis_id="ridgeway_direct_study_v1_2",
        revision_id=fixture.bundle.revision.revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=engine_manifest_sha256,
        engine_environment_sha256=engine_environment_sha256,
        workflow_manifest_sha256=workflow_manifest_sha256,
        physics_model_sha256=fixture.physics_model_sha256,
        decision_snapshot_sha256=fixture.decision_snapshot_sha256,
        model_run_cache_key=model_run_cache_key,
        input_sha256=request_sha256,
        result_sha256=sha256_file(result_path),
        matrix_sha256=sha256_file(matrix_path),
        current_map_sha256=sha256_file(current_path) if current_path else None,
        pair_metrics=pair_metrics,
        qa_status=QAStatus.ACCEPTED,
        qa_findings=[],
        resource_observation=observed.observation,
        completed_at=datetime.now(UTC),
        phase=phase,
        request_sha256=request_sha256,
        action_raster_sha256=action_raster_sha256,
        prepared_resistance_sha256=prepared_resistance_sha256,
        footprint_proof_sha256=footprint_proof_sha256,
        footprint_proof_document_sha256=footprint_proof_document_sha256,
        artifact_directory=artifact_directory,
        process_exit_evidence=process_exit_evidence,
    )
    artifact_paths = [
        request_path,
        result_path,
        matrix_path,
        stdout_path,
        stderr_path,
        resistance_path,
        diff_path,
        footprint_document_path,
        circuitscape_ini_path,
        focal_path,
        pair_path,
    ]
    if current_path:
        artifact_paths.append(current_path)
    complete = {
        "job": job.model_dump(mode="json"),
        "artifact_hashes": {
            path.relative_to(evidence_root).as_posix(): sha256_file(path) for path in artifact_paths
        },
    }
    _json(job_root / "complete.json", complete)
    return job


def _execute_phase_jobs(
    *,
    evidence_root: Path,
    sources: Path,
    fixture: RidgewayFixture,
    specs: list[tuple[str, str]],
    phase: Literal["matrix", "map"],
    engine_manifest_sha256: str,
    engine_environment_sha256: str,
    workflow_manifest_sha256: str,
    julia_executable: Path,
    julia_project: Path,
    julia_depot: Path,
    worker_count: int,
    timeout_seconds: int,
    progress: Callable[[int, int, bool], None] | None,
    progress_offset: int,
    progress_total: int,
) -> tuple[list[ExactFixtureJob], int]:
    """Execute one phase and cancel queued work if any job or caller fails."""

    jobs: list[ExactFixtureJob] = []
    cached_count = 0
    executor = ThreadPoolExecutor(max_workers=max(1, worker_count))
    futures = {}
    try:
        for portfolio_id, scenario_id in specs:
            future = executor.submit(
                _execute_job,
                evidence_root=evidence_root,
                sources=sources,
                fixture=fixture,
                portfolio_id=portfolio_id,
                scenario_id=scenario_id,
                phase=phase,
                engine_manifest_sha256=engine_manifest_sha256,
                engine_environment_sha256=engine_environment_sha256,
                workflow_manifest_sha256=workflow_manifest_sha256,
                julia_executable=julia_executable,
                julia_project=julia_project,
                julia_depot=julia_depot,
                timeout_seconds=timeout_seconds,
            )
            futures[future] = (portfolio_id, scenario_id)
        for future in as_completed(futures):
            job = future.result()
            jobs.append(job)
            cached_count += int(job.cached)
            if progress:
                progress(progress_offset + len(jobs), progress_total, job.cached)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    jobs.sort(key=lambda item: (item.portfolio_id, item.scenario_id))
    return jobs, cached_count


def _direct_view(job: ExactFixtureJob) -> DirectConnectivityRun:
    return DirectConnectivityRun.model_validate(
        {
            key: value
            for key, value in job.model_dump(mode="python").items()
            if key in DirectConnectivityRun.model_fields
        }
    )


def _matrix_summaries(
    fixture: RidgewayFixture,
    jobs: list[ExactFixtureJob],
) -> list[PortfolioDecisionSummary]:
    baseline = [
        _direct_view(item) for item in jobs if item.portfolio_id == fixture.baseline_portfolio_id
    ]
    matrix_rules = [
        item
        for item in fixture.bundle.decision_rules
        if item.metric != RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
    ]
    summaries = []
    for portfolio in fixture.portfolios:
        selected = [
            _direct_view(item) for item in jobs if item.portfolio_id == portfolio.portfolio_id
        ]
        cost, currency, _ = portfolio_cost(portfolio, fixture.bundle.intervention_options)
        summaries.append(
            evaluate_portfolio(
                portfolio=portfolio,
                runs=selected,
                baseline_runs=baseline,
                scenarios=fixture.bundle.scenarios,
                rules=matrix_rules,
                cost=cost,
                currency=currency,
                unresolved_feasibility=unresolved_portfolio_concerns(
                    portfolio, fixture.bundle.intervention_options
                ),
            )
        )
    return summaries


def _reductions(summary: PortfolioDecisionSummary) -> list[float]:
    return [
        float(item.observed_value)
        for item in summary.rule_dispositions
        if item.rule_id == "rule_target_improvement"
        and isinstance(item.observed_value, (int, float))
    ]


def _target_hypothesis_scores(
    fixture: RidgewayFixture,
    summaries: list[PortfolioDecisionSummary],
) -> dict[str, dict[str, float]]:
    """Reduce target pairs within each hypothesis without mixing raw resistance."""

    scenario_by_id = {item.scenario_id: item for item in fixture.bundle.scenarios}
    output: dict[str, dict[str, float]] = {}
    for summary in summaries:
        observed: dict[str, list[float]] = defaultdict(list)
        for disposition in summary.rule_dispositions:
            if (
                disposition.rule_id != "rule_target_improvement"
                or disposition.scenario_id is None
                or not isinstance(disposition.observed_value, (int, float))
            ):
                continue
            scenario = scenario_by_id[disposition.scenario_id]
            if not scenario.safeguard:
                observed[scenario.resistance_hypothesis_id].append(
                    float(disposition.observed_value)
                )
        output[summary.portfolio_id] = {
            hypothesis_id: min(values) for hypothesis_id, values in observed.items() if values
        }
    return output


def _hypothesis_rank_observations(
    fixture: RidgewayFixture,
    summaries: list[PortfolioDecisionSummary],
) -> dict[str, dict[str, dict[str, float | int]]]:
    scores = _target_hypothesis_scores(fixture, summaries)
    hypothesis_ids = sorted(item.hypothesis_id for item in fixture.bundle.resistance_hypotheses)
    output: dict[str, dict[str, dict[str, float | int]]] = {
        item.portfolio_id: {} for item in summaries
    }
    for hypothesis_id in hypothesis_ids:
        ranked = sorted(
            (
                (portfolio_id, values[hypothesis_id])
                for portfolio_id, values in scores.items()
                if hypothesis_id in values
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for rank, (portfolio_id, score) in enumerate(ranked, start=1):
            output[portfolio_id][hypothesis_id] = {"rank": rank, "score": score}
    return output


def _preselect_teaching_roles(
    fixture: RidgewayFixture,
    summaries: list[PortfolioDecisionSummary],
) -> tuple[
    dict[str, PortfolioDecisionSummary],
    dict[str, dict[str, dict[str, float | int]]],
]:
    budget = float(fixture.bundle.decision_brief.budget_amount or 0)
    baseline_id = fixture.baseline_portfolio_id
    caution_pool = [
        item
        for item in summaries
        if item.portfolio_id != baseline_id
        and item.cost <= budget
        and not item.unresolved_feasibility
        and _reductions(item)
        and max(_reductions(item)) > 0
    ]
    if not caution_pool:
        raise DirectStudyError("direct matrix evidence contains no low-cost positive lesson")
    caution = min(caution_pool, key=lambda item: (item.cost, item.portfolio_id))
    carry_pool = [
        item
        for item in summaries
        if item.disposition == PortfolioDisposition.PASSES
        and item.cost <= budget
        and not item.unresolved_feasibility
        and item.worst_resistance_reduction_percent is not None
        and item.portfolio_id != caution.portfolio_id
    ]
    if not carry_pool:
        raise DirectStudyError("direct matrix evidence contains no distinct carry-forward lesson")
    carry = max(
        carry_pool,
        key=lambda item: (item.worst_resistance_reduction_percent or -math.inf, -item.cost),
    )
    over_pool = [
        item
        for item in summaries
        if item.cost > budget
        and not item.unresolved_feasibility
        and item.worst_resistance_reduction_percent is not None
        and all(
            disposition.passed or disposition.rule_id == "rule_cost"
            for disposition in item.rule_dispositions
        )
        and item.portfolio_id not in {caution.portfolio_id, carry.portfolio_id}
    ]
    if not over_pool:
        raise DirectStudyError("direct matrix evidence contains no cost-only failure lesson")
    over = max(
        over_pool,
        key=lambda item: (item.worst_resistance_reduction_percent or -math.inf, -item.cost),
    )
    ranks = _hypothesis_rank_observations(fixture, summaries)
    minimum_rank_swing = max(5, len(summaries) // 10)
    top_third = max(1, math.ceil(len(summaries) / 3))
    collapse_pool: list[tuple[int, float, PortfolioDecisionSummary]] = []
    for item in summaries:
        if (
            item.portfolio_id
            in {
                baseline_id,
                caution.portfolio_id,
                carry.portfolio_id,
                over.portfolio_id,
            }
            or item.unresolved_feasibility
        ):
            continue
        observations = ranks[item.portfolio_id]
        if len(observations) != len(fixture.bundle.resistance_hypotheses):
            continue
        rank_values = [int(value["rank"]) for value in observations.values()]
        score_values = [float(value["score"]) for value in observations.values()]
        rank_swing = max(rank_values) - min(rank_values)
        if min(rank_values) <= top_third and rank_swing >= minimum_rank_swing:
            collapse_pool.append((rank_swing, max(score_values) - min(score_values), item))
    if not collapse_pool:
        raise DirectStudyError(
            "direct evidence contains no strong portfolio with a material hypothesis-rank collapse"
        )
    under_budget_collapse_pool = [item for item in collapse_pool if item[2].cost <= budget]
    collapse = max(
        under_budget_collapse_pool or collapse_pool,
        key=lambda item: (item[0], item[1], item[2].portfolio_id),
    )[2]
    roles = {"caution": caution, "carry": carry, "over": over, "collapse": collapse}
    if len({item.portfolio_id for item in roles.values()}) != 4:
        raise DirectStudyError("teaching-role selection did not produce four distinct portfolios")
    return roles, ranks


def _is_declared_nodata(value: float | None, nodata: float | None) -> bool:
    if value is None or nodata is None:
        return value is None
    if math.isnan(float(nodata)):
        return math.isnan(float(value))
    return math.isclose(float(value), float(nodata), rel_tol=0.0, abs_tol=1e-9)


def _review_mask_current_totals(
    *,
    baseline_path: Path,
    portfolio_path: Path,
    mask: InMemoryRaster,
) -> tuple[float, float]:
    try:
        comparison = compare_current_maps_in_mask(baseline_path, portfolio_path, mask)
    except RasterIOError as exc:
        raise DirectStudyError(f"current-map grid or data failed validation: {exc}") from exc
    return comparison.baseline_total, comparison.portfolio_total


def _attach_map_diagnostics(
    evidence_root: Path,
    fixture: RidgewayFixture,
    summaries: list[PortfolioDecisionSummary],
    map_jobs: list[ExactFixtureJob],
) -> tuple[dict[str, PortfolioDecisionSummary], list[dict[str, object]]]:
    mask = fixture.rasters[fixture.bundle.review_masks[0].asset_id]
    baseline_by_scenario = {
        item.scenario_id: item
        for item in map_jobs
        if item.portfolio_id == fixture.baseline_portfolio_id
    }
    summary_by_id = {item.portfolio_id: item for item in summaries}
    output: dict[str, PortfolioDecisionSummary] = {}
    diagnostics_by_run: dict[str, list[DiagnosticMapSummary]] = defaultdict(list)
    for portfolio_id in {item.portfolio_id for item in map_jobs} - {fixture.baseline_portfolio_id}:
        summary = summary_by_id[portfolio_id]
        dispositions = list(summary.rule_dispositions)
        triggered = False
        for job in sorted(
            (item for item in map_jobs if item.portfolio_id == portfolio_id),
            key=lambda item: item.scenario_id,
        ):
            baseline = baseline_by_scenario[job.scenario_id]
            baseline_path = (
                evidence_root
                / baseline.artifact_directory
                / "outputs"
                / "connectivity_cum_curmap.asc"
            )
            portfolio_path = (
                evidence_root / job.artifact_directory / "outputs" / "connectivity_cum_curmap.asc"
            )
            try:
                comparison = compare_current_maps_in_mask(baseline_path, portfolio_path, mask)
            except RasterIOError as exc:
                raise DirectStudyError(
                    f"current-map grid, NODATA topology, or data failed validation: {exc}"
                ) from exc
            if comparison.baseline_total <= 0:
                raise DirectStudyError("baseline review-mask current must be positive")
            change = (
                (comparison.portfolio_total - comparison.baseline_total)
                / comparison.baseline_total
                * 100
            )
            review = change > fixture.bundle.review_masks[0].review_trigger_percent
            triggered |= review
            diagnostic = DiagnosticMapSummary(
                mask_id=fixture.bundle.review_masks[0].mask_id,
                baseline_total_current=comparison.baseline_total,
                portfolio_total_current=comparison.portfolio_total,
                relative_change_percent=change,
                review_triggered=review,
                reviewed_cell_count=comparison.reviewed_cell_count,
                included_cell_count=comparison.included_cell_count,
                excluded_nodata_cell_count=comparison.excluded_nodata_cell_count,
                raster_nodata_cell_count=comparison.raster_nodata_cell_count,
                nodata_topology_sha256=comparison.nodata_topology_sha256,
            )
            diagnostics_by_run[job.direct_run_id].append(diagnostic)
            dispositions.append(
                RuleDisposition(
                    rule_id="rule_review_mask",
                    scenario_id=job.scenario_id,
                    observed_value=change,
                    passed=not review,
                    explanation=(
                        "Diagnostic current redistribution; not predicted animal use. "
                        f"Included {comparison.included_cell_count} of "
                        f"{comparison.reviewed_cell_count} reviewed cells; identical NODATA "
                        f"topology {comparison.nodata_topology_sha256}."
                    ),
                )
            )
        output[portfolio_id] = summary.model_copy(
            update={"rule_dispositions": dispositions, "review_mask_triggered": triggered}
        )
    diagnostic_records = [
        {
            "direct_run_id": direct_run_id,
            "summaries": [item.model_dump(mode="json") for item in summaries],
        }
        for direct_run_id, summaries in sorted(diagnostics_by_run.items())
    ]
    return output, diagnostic_records


def _public_projection(
    fixture: RidgewayFixture,
    roles: dict[str, PortfolioDecisionSummary],
    selection: TeachingPortfolioSelection,
    hypothesis_ranks: dict[str, dict[str, dict[str, float | int]]],
    direct_jobs: list[ExactFixtureJob],
    engine_manifest_sha256: str,
    workflow_manifest_sha256: str,
    evidence_index_sha256: str,
) -> dict:
    labels = {
        "caution": ("low-cost-caution", "Low-cost caution"),
        "carry": ("carry-forward", "Carry-forward candidate"),
        "over": ("over-budget", "Robust but over budget"),
        "collapse": ("single-hypothesis-collapse", "Hypothesis-sensitive option"),
    }
    portfolio_rows = []
    for role, summary in roles.items():
        reductions_by_scenario: dict[str, list[float]] = {}
        mask_by_scenario: dict[str, float] = {}
        for disposition in summary.rule_dispositions:
            if disposition.scenario_id and disposition.rule_id == "rule_target_improvement":
                reductions_by_scenario.setdefault(disposition.scenario_id, []).append(
                    float(disposition.observed_value)
                )
            if disposition.scenario_id and disposition.rule_id == "rule_review_mask":
                mask_by_scenario[disposition.scenario_id] = float(disposition.observed_value)
        key, name = labels[role]
        portfolio_rows.append(
            {
                "projectionKey": key,
                "id": summary.portfolio_id,
                "name": name,
                "role": role,
                "costUsd": summary.cost,
                "ruleStatus": (
                    "fail"
                    if summary.disposition
                    in {
                        PortfolioDisposition.FAILS,
                        PortfolioDisposition.SURVEY_FIRST,
                        PortfolioDisposition.INCOMPLETE,
                    }
                    else "review"
                    if summary.review_mask_triggered
                    else "pass"
                ),
                "worstResistanceReductionPct": summary.worst_resistance_reduction_percent,
                "worstScenarioId": summary.controlling_scenario_id,
                "roadMaskFlag": summary.review_mask_triggered,
                "nextAction": summary.next_action,
                "hypothesisRanks": hypothesis_ranks[summary.portfolio_id],
                "scenarioResults": [
                    {
                        "scenarioId": scenario.scenario_id,
                        "worstPairResistanceReductionPct": min(
                            reductions_by_scenario.get(scenario.scenario_id, [0.0])
                        ),
                        "roadMaskCurrentChangePct": mask_by_scenario[scenario.scenario_id],
                        "safeguard": scenario.safeguard,
                    }
                    for scenario in fixture.bundle.scenarios
                ],
            }
        )
    observations = [
        item.resource_observation for item in direct_jobs if item.resource_observation is not None
    ]
    if len(observations) != len(direct_jobs):
        raise DirectStudyError("every direct Ridgeway job requires a resource observation")
    if len({item.observation_id for item in observations}) != len(observations):
        raise DirectStudyError("direct Ridgeway resource observation IDs must be unique")
    if any(
        item.boundary
        not in {
            ResourceObservationBoundary.DIRECT_SOLVER,
            ResourceObservationBoundary.CACHE_REUSE,
        }
        for item in observations
    ):
        raise DirectStudyError("direct Ridgeway evidence has a non-direct resource boundary")
    energy_statuses = {item.energy_status.value for item in observations}
    memory_statuses = {item.peak_memory_status.value for item in observations}
    solver_wall_seconds = sum(item.solver_wall_seconds or 0.0 for item in observations)
    estimated_energy = [
        item for item in observations if item.energy_status == ResourceObservationStatus.ESTIMATED
    ]
    observed_peak_memory = [
        item.peak_memory_bytes for item in observations if item.peak_memory_bytes is not None
    ]
    return {
        "schemaVersion": "ecocorridor-replay/v1.1",
        "fixtureId": fixture.bundle.project_id,
        "baselinePortfolioId": fixture.baseline_portfolio_id,
        "title": fixture.bundle.title,
        "cc0": True,
        "manifestVersion": fixture.fixture_version,
        "engineManifestSha256": engine_manifest_sha256,
        "workflowManifestSha256": workflow_manifest_sha256,
        "evidenceIndexSha256": evidence_index_sha256,
        "evidenceState": "PACKAGE_VERIFICATION_REQUIRED",
        "resourceObservability": {
            "jobsWithObservations": len(observations),
            "jobsWithoutObservations": len(direct_jobs) - len(observations),
            "directSolverWallSeconds": solver_wall_seconds if observations else None,
            "estimatedEnergyKwh": (
                sum(item.energy_kwh or 0.0 for item in estimated_energy)
                if estimated_energy
                else None
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
        },
        "releaseReplayEligible": selection.release_eligible,
        "releaseBlocker": "Independent archive and semantic verification has not completed.",
        "disclaimer": fixture.notice,
        "decisionBrief": {
            "question": fixture.bundle.decision_brief.decision_question,
            "budgetUsd": fixture.bundle.decision_brief.budget_amount,
            "targetRule": next(
                item.title
                for item in fixture.bundle.decision_rules
                if item.rule_id == "rule_target_improvement"
            ),
            "safeguardRule": next(
                item.title
                for item in fixture.bundle.decision_rules
                if item.rule_id == "rule_safeguard"
            ),
            "nonGoals": fixture.bundle.decision_brief.non_goals,
        },
        "study": {
            "portfolioCount": len(fixture.portfolios),
            "scenarioCount": len(fixture.bundle.scenarios),
            "matrixJobCount": fixture.direct_matrix_job_count,
            "mapJobCount": fixture.teaching_map_job_count,
            "coreCount": 4,
            "interventionSiteCount": len(fixture.bundle.intervention_sites),
            "hypothesisRankingMetric": (
                "Minimum required target-pair resistance reduction (%) within each resistance "
                "hypothesis; rank 1 is the highest score among the 128 directly evaluated "
                "portfolios."
            ),
        },
        "scenarios": [
            {
                "id": scenario.scenario_id,
                "guild": scenario.guild_id,
                "purpose": "safeguard" if scenario.safeguard else "target",
                "hypothesis": scenario.resistance_hypothesis_id,
                "futureCondition": scenario.future_condition,
                "maturity": scenario.maturity_horizon,
                "includedPairs": len(scenario.included_pair_ids),
            }
            for scenario in fixture.bundle.scenarios
        ],
        "portfolios": portfolio_rows,
    }


def run_direct_study(
    *,
    output_directory: Path,
    julia_executable: Path,
    julia_project: Path,
    julia_depot: Path,
    worker_count: int = 4,
    timeout_seconds: int = 300,
    job_limit: int | None = None,
    publish_directory: Path | None = None,
    progress: Callable[[int, int, bool], None] | None = None,
) -> RidgewayStudyResult:
    if os.name != "posix":
        raise DirectStudyError(
            "Ridgeway release-evidence production requires POSIX process-group exit "
            "confirmation; Windows production remains blocked until the launcher owns and "
            "queries a native Job Object"
        )
    fixture = build_ridgeway_fixture()
    # Direct jobs execute from isolated attempt directories. Resolve every
    # trusted runtime input before changing their cwd so a caller-provided
    # relative depot/project path cannot silently point at an empty directory.
    output_directory = output_directory.expanduser().resolve()
    julia_executable = julia_executable.expanduser().resolve(strict=True)
    julia_project = julia_project.expanduser().resolve(strict=True)
    julia_depot = julia_depot.expanduser().resolve(strict=True)
    output_directory.mkdir(parents=True, exist_ok=True)
    julia_project = _ensure_engine_snapshot(output_directory, julia_project).resolve(strict=True)
    engine_manifest, engine_environment_sha, workflow_sha, component_hashes = (
        _validate_engine_environment(julia_executable, julia_project, julia_depot)
    )
    engine_sha = engine_manifest.sha256
    engine_manifest_record = _ensure_engine_manifest_record(
        output_directory,
        engine_manifest,
        engine_environment_sha,
        component_hashes,
    )
    workflow_snapshot = _ensure_workflow_snapshot(output_directory, component_hashes)
    sources = _ensure_sources(output_directory, fixture)
    matrix_specs = [
        (portfolio.portfolio_id, scenario.scenario_id)
        for portfolio in fixture.portfolios
        for scenario in fixture.bundle.scenarios
    ]
    scheduled = matrix_specs if job_limit is None else matrix_specs[:job_limit]
    matrix_jobs, cached_count = _execute_phase_jobs(
        evidence_root=output_directory,
        sources=sources,
        fixture=fixture,
        specs=scheduled,
        phase="matrix",
        engine_manifest_sha256=engine_sha,
        engine_environment_sha256=engine_environment_sha,
        workflow_manifest_sha256=workflow_sha,
        julia_executable=julia_executable,
        julia_project=julia_project,
        julia_depot=julia_depot,
        worker_count=worker_count,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_offset=0,
        progress_total=len(scheduled),
    )
    if len(matrix_jobs) != fixture.direct_matrix_job_count:
        return RidgewayStudyResult(
            state="INCOMPLETE",
            matrix_jobs_completed=len(matrix_jobs),
            map_jobs_completed=0,
            cached_jobs=cached_count,
            engine_manifest_sha256=engine_sha,
            workflow_manifest_sha256=workflow_sha,
        )
    summaries = _matrix_summaries(fixture, matrix_jobs)
    roles, hypothesis_ranks = _preselect_teaching_roles(fixture, summaries)
    map_portfolios = [fixture.baseline_portfolio_id] + [
        roles[key].portfolio_id for key in ("caution", "carry", "over", "collapse")
    ]
    map_specs = [
        (portfolio_id, scenario.scenario_id)
        for portfolio_id in map_portfolios
        for scenario in fixture.bundle.scenarios
    ]
    map_jobs, map_cached_count = _execute_phase_jobs(
        evidence_root=output_directory,
        sources=sources,
        fixture=fixture,
        specs=map_specs,
        phase="map",
        engine_manifest_sha256=engine_sha,
        engine_environment_sha256=engine_environment_sha,
        workflow_manifest_sha256=workflow_sha,
        julia_executable=julia_executable,
        julia_project=julia_project,
        julia_depot=julia_depot,
        worker_count=worker_count,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_offset=len(matrix_jobs),
        progress_total=len(matrix_jobs) + len(map_specs),
    )
    cached_count += map_cached_count
    updated, map_diagnostic_records = _attach_map_diagnostics(
        output_directory, fixture, summaries, map_jobs
    )
    roles = {key: updated[value.portfolio_id] for key, value in roles.items()}
    if not roles["caution"].review_mask_triggered:
        raise DirectStudyError("selected caution portfolio has no observed road-mask review flag")
    selection = TeachingPortfolioSelection(
        low_cost_caution_portfolio_id=roles["caution"].portfolio_id,
        carry_forward_portfolio_id=roles["carry"].portfolio_id,
        over_budget_portfolio_id=roles["over"].portfolio_id,
        hypothesis_collapse_portfolio_id=roles["collapse"].portfolio_id,
        map_rerun_portfolio_ids=map_portfolios,
        evidence_mode="DIRECT_CIRCUITSCAPE",
        release_eligible=False,
        selection_notes={
            "low_cost_caution": "Observed direct result with a road-mask diagnostic review flag.",
            "carry_forward": (
                "Observed robust under-budget screening candidate; resolve observed road-mask "
                "current redistribution before feasibility carry-forward."
                if roles["carry"].review_mask_triggered
                else "Observed robust under-budget screening candidate."
            ),
            "over_budget": "Observed robust ecological rule set over the canonical cost cap.",
            "hypothesis_collapse": (
                "Observed target-reduction ranking is strong under one reviewed resistance "
                "hypothesis and materially lower under another."
            ),
        },
    )
    process_tree_exit_evidence = _process_tree_exit_summary([*matrix_jobs, *map_jobs])
    index_payload = {
        "schema_version": "ridgeway-evidence-index/v1.1",
        "engine_manifest_sha256": engine_sha,
        "engine_manifest": engine_manifest.model_dump(mode="json"),
        "engine_manifest_record": engine_manifest_record,
        "engine_environment_sha256": engine_environment_sha,
        "workflow_manifest_sha256": workflow_sha,
        "workflow_components": component_hashes,
        "workflow_snapshot": workflow_snapshot,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "physics_model_sha256": fixture.physics_model_sha256,
        "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
        "map_diagnostic_records": map_diagnostic_records,
        "process_tree_exit_evidence": process_tree_exit_evidence,
        "matrix_jobs": [item.model_dump(mode="json", exclude={"cached"}) for item in matrix_jobs],
        "map_jobs": [item.model_dump(mode="json", exclude={"cached"}) for item in map_jobs],
    }
    index_path = output_directory / "evidence-index.json"
    _json(index_path, index_payload)
    index_sha = sha256_file(index_path)
    projection = _public_projection(
        fixture,
        roles,
        selection,
        hypothesis_ranks,
        [*matrix_jobs, *map_jobs],
        engine_sha,
        workflow_sha,
        index_sha,
    )
    public_projection_path = output_directory / "public" / "ui_projection.json"
    _json(public_projection_path, projection)
    public_manifest = {
        "schema_version": "ridgeway-direct-evidence/v1.1",
        "fixture_version": fixture.fixture_version,
        "license": fixture.license,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "physics_model_sha256": fixture.physics_model_sha256,
        "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
        "engine_manifest_sha256": engine_sha,
        "engine_environment_sha256": engine_environment_sha,
        "workflow_manifest_sha256": workflow_sha,
        "evidence_index_sha256": index_sha,
        "matrix_job_count": len(matrix_jobs),
        "map_job_count": len(map_jobs),
        "teaching_selection": selection.model_dump(mode="json"),
        "resource_observability": projection["resourceObservability"],
        "process_tree_exit_evidence": process_tree_exit_evidence,
        "ui_projection_sha256": sha256_file(public_projection_path),
        "release_replay_eligible": False,
        "release_blocker": "PACKAGE_VERIFICATION_REQUIRED",
    }
    public_manifest_path = output_directory / "public" / "direct-evidence-manifest.json"
    _json(public_manifest_path, public_manifest)
    if publish_directory:
        raise DirectStudyError(
            "direct generation cannot publish verified replay resources; run the independent "
            "evidence packager after all jobs complete"
        )
    return RidgewayStudyResult(
        state="COMPLETED",
        matrix_jobs_completed=len(matrix_jobs),
        map_jobs_completed=len(map_jobs),
        cached_jobs=cached_count,
        engine_manifest_sha256=engine_sha,
        workflow_manifest_sha256=workflow_sha,
        evidence_index_sha256=index_sha,
        teaching_selection=selection,
        public_projection_path=public_projection_path,
        public_evidence_manifest_path=public_manifest_path,
    )
