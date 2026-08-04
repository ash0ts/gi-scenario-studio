"""Build the deterministic, locally verifiable Ridgeway evidence release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ecocorridor_core.cache_identity import ridgeway_model_run_cache_payload
from ecocorridor_core.claim_safety import assert_safe_rendered_ecological_copy
from ecocorridor_core.contracts import (
    ComputationalResourceObservation,
    DirectConnectivityRun,
    EngineManifest,
    PairResistanceMetric,
    PortfolioDisposition,
    QAStatus,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    RuleMetric,
)
from ecocorridor_core.engine import (
    EngineProtocolResult,
    PreparedPair,
    parse_circuitscape_matrix,
    validate_effective_circuitscape_configuration,
)
from ecocorridor_core.fixture_pipeline import (
    RidgewayMaterializationManifest,
    materialize_ridgeway_ascii,
    ridgeway_model_snapshot_payload,
)
from ecocorridor_core.footprints import (
    FootprintProofDocument,
    verify_footprint_proof_document,
)
from ecocorridor_core.interventions import (
    apply_absolute_replacements,
    portfolio_cost,
    scenario_effects_for_portfolio,
    unresolved_portfolio_concerns,
)
from ecocorridor_core.metrics import evaluate_portfolio
from ecocorridor_core.raster_io import (
    RasterIOError,
    compare_current_maps_in_mask,
    current_map_values_and_nodata,
    load_esri_ascii,
)
from ecocorridor_core.ridgeway import (
    FIXED_TIME,
    build_ridgeway_fixture,
    ridgeway_assumption_ledger_payload,
    ridgeway_cost_basis_payload,
)
from ecocorridor_core.semantic_diff import verify_semantic_raster_diff

ARCHIVE_LIMIT_BYTES = 100_000_000
BUNDLE_SCHEMA = "ridgeway-evidence-bundle/v1.1"
RAW_UI_SCHEMA = "ecocorridor-replay/v1.1"
UI_SCHEMA = "ecocorridor-replay/v1.2"
MATRIX_ABSOLUTE_TOLERANCE = 1e-8
MATRIX_RELATIVE_TOLERANCE = 1e-7


class PackagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Member:
    path: str
    source: Path
    sha256: str
    byte_size: int
    role: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pin_public_ledger_identities(
    direct_manifest: dict[str, Any],
    *,
    assumption_ledger_sha256: str,
    cost_basis_sha256: str,
) -> None:
    """Bind the reviewed assumption and cost ledgers at the replay boundary."""

    for label, digest in (
        ("assumption_ledger_sha256", assumption_ledger_sha256),
        ("cost_basis_sha256", cost_basis_sha256),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PackagingError(f"{label} must be a lowercase SHA-256 digest")
        direct_manifest[label] = digest


def reviewed_engine_sources() -> dict[str, Path]:
    """Return the repository sources independently trusted by this packager."""

    root = Path(__file__).resolve().parents[1]
    project = root / "julia" / "EcoCorridorEngine"
    return {
        "Project.toml": project / "Project.toml",
        "Manifest.toml": project / "Manifest.toml",
        "src/EcoCorridorEngine.jl": project / "src" / "EcoCorridorEngine.jl",
        "bin/ecocorridor-engine.jl": project / "bin" / "ecocorridor-engine.jl",
    }


def reviewed_workflow_sources() -> dict[str, Path]:
    """Return every reviewed Python source allowed to have produced the study."""

    root = Path(__file__).resolve().parents[1]
    core = root / "python" / "ecocorridor_core"
    return {
        "contracts": core / "contracts.py",
        "fixture_definition": core / "ridgeway.py",
        "grid_model": core / "grid.py",
        "input_materializer": core / "fixture_pipeline.py",
        "model_run_cache_identity": core / "cache_identity.py",
        "study_orchestrator": core / "direct_study.py",
        "python_protocol_parser": core / "engine.py",
        "footprint_authorization": core / "footprints.py",
        "metric_definitions": core / "metrics.py",
        "intervention_mutator": core / "interventions.py",
        "current_raster_parser": core / "raster_io.py",
        "resource_observability": core / "resource_observability.py",
        "semantic_raster_diff": core / "semantic_diff.py",
        "python_project_manifest": root / "pyproject.toml",
        "python_dependency_lock": root / "uv.lock",
    }


def reviewed_report_sources() -> dict[str, tuple[Path, str]]:
    """Return every source that can change the packaged public report copy."""

    root = Path(__file__).resolve().parents[1]
    return {
        "package_ridgeway_evidence.py": (Path(__file__).resolve(), "report_packager_snapshot"),
        "claim_safety.py": (
            root / "python" / "ecocorridor_core" / "claim_safety.py",
            "report_claim_safety_snapshot",
        ),
    }


def ensure_reviewed_report_snapshots(
    evidence_root: Path,
) -> dict[str, tuple[Path, str, str]]:
    """Materialize and verify the exact allowlisted report dependencies."""

    snapshots: dict[str, tuple[Path, str, str]] = {}
    report_root = evidence_root / "report-snapshot"
    for filename, (source, role) in reviewed_report_sources().items():
        if not source.is_file() or source.is_symlink():
            raise PackagingError(f"reviewed report source is absent or unsafe: {filename}")
        digest = sha256_file(source)
        snapshot = report_root / filename
        if snapshot.exists() and (
            snapshot.is_symlink() or not snapshot.is_file() or sha256_file(snapshot) != digest
        ):
            raise PackagingError(
                f"retained report dependency differs from this verifier: {filename}"
            )
        if not snapshot.exists():
            atomic_bytes(snapshot, source.read_bytes())
        snapshots[filename] = (snapshot, digest, role)
    if set(exact_regular_files(report_root)) != set(reviewed_report_sources()):
        raise PackagingError("report snapshot contains an extra or missing dependency")
    return snapshots


def reviewed_dependency_snapshot() -> bytes:
    try:
        packages = {
            package: importlib.metadata.version(package)
            for package in ("pydantic", "pydantic-core")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise PackagingError(f"required scientific dependency is unavailable: {exc}") from exc
    return canonical_json_bytes(
        {
            "schema_version": "ridgeway-python-dependencies/v1.0",
            "packages": packages,
        }
    )


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PackagingError(f"unsafe archive member path: {value!r}")
    return path.as_posix()


def add_member(
    members: dict[str, Member],
    *,
    evidence_root: Path,
    source: Path,
    role: str,
    expected_sha256: str | None = None,
) -> None:
    source = source.resolve()
    root = evidence_root.resolve()
    if root not in source.parents or not source.is_file() or source.is_symlink():
        raise PackagingError(f"evidence member is absent, unsafe, or outside the root: {source}")
    path = safe_archive_path(source.relative_to(root).as_posix())
    if path in members:
        if members[path].role != role:
            raise PackagingError(f"archive member received conflicting roles: {path}")
        return
    digest = sha256_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise PackagingError(f"artifact hash mismatch: {path}")
    members[path] = Member(
        path=path,
        source=source,
        sha256=digest,
        byte_size=source.stat().st_size,
        role=role,
    )


def add_raw_producer_inputs(
    members: dict[str, Member],
    *,
    evidence_root: Path,
    direct_manifest_path: Path,
    raw_projection_path: Path,
    direct_manifest_sha256: str,
    raw_projection_sha256: str,
) -> None:
    """Retain the exact producer inputs needed for semantic recomputation."""

    add_member(
        members,
        evidence_root=evidence_root,
        source=direct_manifest_path,
        role="raw_direct_evidence_manifest",
        expected_sha256=direct_manifest_sha256,
    )
    add_member(
        members,
        evidence_root=evidence_root,
        source=raw_projection_path,
        role="raw_ui_projection",
        expected_sha256=raw_projection_sha256,
    )


def verify_indexed_pair_metrics(
    indexed_metrics: Any,
    reparsed_metrics: list[Any],
    *,
    direct_run_id: str,
) -> None:
    """Require the index to match the complete canonical direct-run metric shape."""

    expected_metrics = [
        PairResistanceMetric(
            pair_id=item.pair_id,
            effective_resistance=item.effective_resistance,
        ).model_dump(mode="json")
        for item in sorted(reparsed_metrics, key=lambda item: item.pair_id)
    ]
    if not isinstance(indexed_metrics, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("pair_id"), str)
        for item in indexed_metrics
    ):
        raise PackagingError(f"indexed metrics are malformed: {direct_run_id}")
    observed_metrics = sorted(indexed_metrics, key=lambda item: item["pair_id"])
    if observed_metrics != expected_metrics:
        raise PackagingError(f"raw matrix and indexed metrics differ: {direct_run_id}")


def role_for_artifact(phase: str, path: str) -> str:
    name = PurePosixPath(path).name
    if name == "footprint-proofs.json":
        return "footprint_proof_records"
    if name == "action-raster-diff.json":
        return "semantic_raster_diff"
    if name == "circuitscape.ini":
        return "circuitscape_effective_configuration"
    if name == "request.json":
        return f"{phase}_request"
    if name == "result.json":
        return f"{phase}_result"
    if name.endswith("_resistances_3columns.out"):
        return f"{phase}_matrix"
    if "cum_curmap" in name:
        return "map_current_raster"
    if name == "stdout.log":
        return f"{phase}_stdout"
    if name == "stderr.log":
        return f"{phase}_stderr"
    if "/inputs/" in path:
        return f"{phase}_materialized_input"
    return f"{phase}_artifact"


def role_for_source(path: str, declared_role: str | None = None) -> str:
    if declared_role in {
        "reviewed_footprint_mask",
        "risk_review_mask",
        "model_decision_snapshot",
        "reviewed_asset_source",
        "cost_basis",
        "assumption_ledger",
        "resistance_raster",
        "focal_raster",
        "included_pairs_source",
        "study_plan",
    }:
        return str(declared_role)
    name = PurePosixPath(path).name
    if name == "materialization-manifest.json":
        return "source_materialization_manifest"
    return "normalized_model_source"


def exact_regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise PackagingError(f"required evidence directory is absent or unsafe: {root}")
    output: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackagingError(f"symlinked evidence is forbidden: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackagingError(f"non-regular evidence is forbidden: {path}")
        output[path.relative_to(root).as_posix()] = path
    return output


def verify_source_evidence(
    evidence_root: Path, fixture: Any, evidence_index: dict[str, Any]
) -> tuple[RidgewayMaterializationManifest, dict[str, Path]]:
    sources = evidence_root / "sources"
    observed = exact_regular_files(sources)
    manifest_path = sources / "materialization-manifest.json"
    try:
        manifest = RidgewayMaterializationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise PackagingError("source materialization manifest is invalid") from exc
    if (
        manifest.fixture_version != fixture.fixture_version
        or manifest.project_id != fixture.bundle.project_id
        or manifest.model_sha256 != fixture.bundle.revision.model_sha256
        or manifest.materializer_sha256
        != evidence_index.get("workflow_components", {}).get("input_materializer")
    ):
        raise PackagingError("source materialization identities differ from direct evidence")
    declared = {item.relative_path: item for item in manifest.files}
    if len(declared) != len(manifest.files) or set(observed) != {
        "materialization-manifest.json",
        *declared,
    }:
        raise PackagingError("source tree does not exactly match its materialization manifest")
    for relative, record in declared.items():
        path = observed[relative]
        if sha256_file(path) != record.sha256 or path.stat().st_size != record.byte_size:
            raise PackagingError(f"source file differs from its materialization record: {relative}")
    role_counts = Counter(item.role for item in manifest.files)
    expected_roles = {
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
    }
    if role_counts != Counter(expected_roles):
        raise PackagingError(f"source semantic coverage is incomplete: {dict(role_counts)}")
    with tempfile.TemporaryDirectory(prefix="ridgeway-packager-", dir="/private/tmp") as directory:
        expected_root = Path(directory)
        expected_manifest = materialize_ridgeway_ascii(expected_root, fixture=fixture)
        if expected_manifest != manifest:
            raise PackagingError("source manifest differs from canonical fixture materialization")
        expected = exact_regular_files(expected_root)
        if set(expected) != set(observed):
            raise PackagingError("source file set differs from canonical fixture materialization")
        for relative in expected:
            if expected[relative].read_bytes() != observed[relative].read_bytes():
                raise PackagingError(f"source bytes differ from canonical fixture: {relative}")
    snapshot_records = [item for item in manifest.files if item.role != "model_decision_snapshot"]
    expected_snapshot = ridgeway_model_snapshot_payload(fixture, snapshot_records)
    if (
        json.loads(observed["model-decision-snapshot.json"].read_text(encoding="utf-8"))
        != expected_snapshot
    ):
        raise PackagingError("retained model/decision snapshot differs from canonical contracts")
    actual_cost = json.loads(observed["ledgers/ridgeway-cost-basis.json"].read_text())
    expected_cost = ridgeway_cost_basis_payload(fixture.bundle.intervention_options)
    if (
        actual_cost != expected_cost
        or actual_cost.get("content_sha256") != fixture.cost_basis_sha256
    ):
        raise PackagingError("retained cost basis differs from its decision identity")
    assumption = json.loads(observed["ledgers/ridgeway-assumption-ledger.json"].read_text())
    expected_assumption = ridgeway_assumption_ledger_payload(
        fixture.bundle.intervention_options, fixture.bundle.fitness
    )
    if (
        assumption != expected_assumption
        or assumption.get("content_sha256") != fixture.assumption_ledger_sha256
    ):
        raise PackagingError("retained assumption ledger differs from its decision identity")
    return manifest, observed


def verify_snapshots_against_reviewed_sources(
    *,
    engine_files: dict[str, Path],
    workflow_files: dict[str, Path],
    workflow_records: dict[str, Any],
    components: dict[str, str],
) -> None:
    """Reject a self-consistent snapshot that differs from reviewed release source."""

    reviewed_engine = reviewed_engine_sources()
    if set(engine_files) != set(reviewed_engine):
        raise PackagingError("engine snapshot file coverage differs from reviewed source")
    engine_component_by_path = {
        "Project.toml": "project",
        "Manifest.toml": "manifest",
        "src/EcoCorridorEngine.jl": "wrapper",
        "bin/ecocorridor-engine.jl": "entrypoint",
    }
    for relative, reviewed in reviewed_engine.items():
        retained = engine_files[relative]
        if (
            not reviewed.is_file()
            or reviewed.is_symlink()
            or retained.read_bytes() != reviewed.read_bytes()
            or components.get(engine_component_by_path[relative]) != sha256_file(reviewed)
        ):
            raise PackagingError(f"engine snapshot differs from reviewed source: {relative}")

    reviewed_workflow = reviewed_workflow_sources()
    expected_labels = {*reviewed_workflow, "python_dependency_versions"}
    if set(workflow_records) != expected_labels:
        raise PackagingError("workflow snapshot allowlist differs from reviewed source")
    if set(workflow_files) != {
        f"{label}{source.suffix or '.bin'}" for label, source in reviewed_workflow.items()
    } | {"python-dependency-versions.json"}:
        raise PackagingError("workflow snapshot file coverage differs from reviewed source")

    for label, reviewed in reviewed_workflow.items():
        relative = f"{label}{reviewed.suffix or '.bin'}"
        record = workflow_records[label]
        if (
            not isinstance(record, dict)
            or record.get("relative_path") != f"workflow-snapshot/{relative}"
            or record.get("sha256") != sha256_file(reviewed)
            or components.get(label) != sha256_file(reviewed)
            or workflow_files[relative].read_bytes() != reviewed.read_bytes()
        ):
            raise PackagingError(f"workflow snapshot differs from reviewed source: {label}")

    dependency_bytes = reviewed_dependency_snapshot()
    dependency_sha = hashlib.sha256(dependency_bytes).hexdigest()
    dependency_record = workflow_records["python_dependency_versions"]
    if (
        not isinstance(dependency_record, dict)
        or dependency_record.get("relative_path")
        != "workflow-snapshot/python-dependency-versions.json"
        or dependency_record.get("sha256") != dependency_sha
        or components.get("python_dependency_versions") != dependency_sha
        or workflow_files["python-dependency-versions.json"].read_bytes() != dependency_bytes
    ):
        raise PackagingError("workflow dependency snapshot differs from reviewed environment")


def verify_engine_and_workflow_snapshots(
    evidence_root: Path, evidence_index: dict[str, Any]
) -> tuple[Path, dict[str, Path]]:
    try:
        manifest = EngineManifest.model_validate(evidence_index["engine_manifest"])
    except (KeyError, ValueError) as exc:
        raise PackagingError("typed EngineManifest is absent or invalid") from exc
    if manifest.sha256 != evidence_index.get("engine_manifest_sha256"):
        raise PackagingError("typed EngineManifest digest differs from evidence index")
    record = evidence_index.get("engine_manifest_record", {})
    manifest_path = evidence_root / safe_archive_path(str(record.get("relative_path", "")))
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or sha256_file(manifest_path) != record.get("sha256")
    ):
        raise PackagingError("retained EngineManifest record is absent or changed")
    retained = json.loads(manifest_path.read_text(encoding="utf-8"))
    engine_environment_components = {
        key: evidence_index.get("workflow_components", {}).get(key)
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
    }
    if (
        retained.get("engine_manifest") != manifest.model_dump(mode="json")
        or retained.get("engine_manifest_sha256") != manifest.sha256
        or retained.get("engine_environment_sha256")
        != evidence_index.get("engine_environment_sha256")
        or retained.get("engine_environment_components") != engine_environment_components
    ):
        raise PackagingError("retained EngineManifest record differs from the index")
    components = evidence_index.get("workflow_components")
    if not isinstance(components, dict):
        raise PackagingError("workflow component manifest is absent")
    engine_files = exact_regular_files(evidence_root / "engine-snapshot")
    expected_engine = {
        "Project.toml": "project",
        "Manifest.toml": "manifest",
        "src/EcoCorridorEngine.jl": "wrapper",
        "bin/ecocorridor-engine.jl": "entrypoint",
    }
    if set(engine_files) != set(expected_engine):
        raise PackagingError("engine snapshot file coverage differs from its allowlist")
    for relative, component in expected_engine.items():
        if sha256_file(engine_files[relative]) != components.get(component):
            raise PackagingError(f"engine snapshot component differs: {relative}")
    workflow_files = exact_regular_files(evidence_root / "workflow-snapshot")
    workflow_records = evidence_index.get("workflow_snapshot")
    if not isinstance(workflow_records, dict):
        raise PackagingError("workflow snapshot manifest is absent")
    expected_workflow_paths: set[str] = set()
    for component, record_value in workflow_records.items():
        if not isinstance(record_value, dict):
            raise PackagingError("workflow snapshot record is invalid")
        relative = safe_archive_path(str(record_value.get("relative_path", "")))
        if not relative.startswith("workflow-snapshot/"):
            raise PackagingError("workflow snapshot path is outside its directory")
        local_relative = relative.removeprefix("workflow-snapshot/")
        expected_workflow_paths.add(local_relative)
        path = workflow_files.get(local_relative)
        digest = record_value.get("sha256")
        if path is None or digest != components.get(component) or sha256_file(path) != digest:
            raise PackagingError(f"workflow snapshot component differs: {component}")
    if set(workflow_files) != expected_workflow_paths:
        raise PackagingError("workflow snapshot contains an extra or missing source")
    verify_snapshots_against_reviewed_sources(
        engine_files=engine_files,
        workflow_files=workflow_files,
        workflow_records=workflow_records,
        components=components,
    )
    expected_component_keys = {
        "julia_executable",
        "project",
        "manifest",
        "wrapper",
        "entrypoint",
        "julia_runtime_tree",
        "native_artifact_tree",
        "protocol",
        "engine_manifest_contract",
        "python_runtime",
        "python_dependency_versions",
        *reviewed_workflow_sources(),
    }
    if set(components) != expected_component_keys:
        raise PackagingError("workflow component manifest differs from its exact allowlist")
    expected_protocol = canonical_sha256(
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
    if components.get("protocol") != expected_protocol:
        raise PackagingError("engine protocol identity differs from the reviewed contract")
    expected_environment_sha = canonical_sha256(engine_environment_components)
    if evidence_index.get("engine_environment_sha256") != expected_environment_sha:
        raise PackagingError("engine-environment digest differs from its component manifest")
    if (
        components.get("engine_manifest_contract") != manifest.sha256
        or manifest.executable_sha256 != components.get("julia_executable")
        or manifest.julia_runtime_sha256 != components.get("julia_runtime_tree")
        or manifest.julia_manifest_sha256 != components.get("manifest")
        or manifest.native_artifacts_sha256 != components.get("native_artifact_tree")
        or manifest.wrapper_sha256 != components.get("wrapper")
        or manifest.parser_sha256 != components.get("python_protocol_parser")
        or manifest.metric_definitions_sha256 != components.get("metric_definitions")
        or manifest.manifest_id != "ridgeway-circuitscape-5.17.1-julia-1.12.6"
    ):
        raise PackagingError("typed EngineManifest differs from reviewed component identities")
    if canonical_sha256(components) != evidence_index.get("workflow_manifest_sha256"):
        raise PackagingError("workflow manifest digest differs from its components")
    return manifest_path, {
        **engine_files,
        **{f"workflow/{k}": v for k, v in workflow_files.items()},
    }


def _artifact_path(
    evidence_root: Path,
    complete: dict[str, Any],
    *,
    basename: str | None = None,
    suffix: str | None = None,
) -> Path:
    matches = []
    for relative, digest in complete.get("artifact_hashes", {}).items():
        name = PurePosixPath(relative).name
        if (basename is not None and name == basename) or (
            suffix is not None and name.endswith(suffix)
        ):
            path = evidence_root / safe_archive_path(relative)
            if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
                raise PackagingError(f"direct-job artifact failed its completion hash: {relative}")
            matches.append(path)
    if len(matches) != 1:
        label = basename or suffix or "artifact"
        raise PackagingError(f"direct job must retain exactly one {label}")
    return matches[0]


def _validate_job_artifact_paths(
    *,
    evidence_root: Path,
    complete: dict[str, Any],
    job: dict[str, Any],
    phase: str,
) -> None:
    prefix = f"jobs/{phase}/{job['portfolio_id']}/{job['scenario_id']}/attempts/"
    artifact_directory = str(job.get("artifact_directory", ""))
    if not re.fullmatch(re.escape(prefix) + r"attempt-[0-9]{4}", artifact_directory):
        raise PackagingError(f"job artifact directory is not canonical: {job['direct_run_id']}")
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
        "outputs/connectivity_resistances_3columns.out",
    }
    if phase == "map":
        relative_names.add("outputs/connectivity_cum_curmap.asc")
    expected = {f"{artifact_directory}/{name}" for name in relative_names}
    artifact_hashes = complete.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or set(artifact_hashes) != expected:
        raise PackagingError(
            f"direct-job artifact paths do not match the exact allowlist: {job['direct_run_id']}"
        )
    for relative, digest in artifact_hashes.items():
        safe = safe_archive_path(relative)
        if not safe.startswith(f"{artifact_directory}/"):
            raise PackagingError(f"job artifact aliases another evidence path: {relative}")
        path = evidence_root / safe
        if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
            raise PackagingError(f"direct-job artifact failed its completion hash: {relative}")


def verify_direct_job_semantics(
    *,
    evidence_root: Path,
    fixture: Any,
    evidence_index: dict[str, Any],
    job: dict[str, Any],
    complete: dict[str, Any],
    phase: str,
) -> None:
    scenario = next(
        item for item in fixture.bundle.scenarios if item.scenario_id == job["scenario_id"]
    )
    portfolio = next(
        item for item in fixture.portfolios if item.portfolio_id == job["portfolio_id"]
    )
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    focal_set = next(
        item
        for item in fixture.bundle.focal_area_sets
        if item.focal_set_id == scenario.focal_set_id
    )
    assets = {item.asset_id: item for item in fixture.bundle.assets}
    request_path = _artifact_path(evidence_root, complete, basename="request.json")
    result_path = _artifact_path(evidence_root, complete, basename="result.json")
    matrix_path = _artifact_path(evidence_root, complete, suffix="_resistances_3columns.out")
    resistance_path = _artifact_path(evidence_root, complete, basename="resistance.asc")
    focal_path = _artifact_path(evidence_root, complete, basename="focal.asc")
    pairs_path = _artifact_path(evidence_root, complete, basename="pairs.tsv")
    diff_path = _artifact_path(evidence_root, complete, basename="action-raster-diff.json")
    proof_path = _artifact_path(evidence_root, complete, basename="footprint-proofs.json")
    config_path = _artifact_path(evidence_root, complete, basename="circuitscape.ini")
    _validate_job_artifact_paths(
        evidence_root=evidence_root,
        complete=complete,
        job=job,
        phase=phase,
    )
    named_hashes = {
        "request_sha256": request_path,
        "input_sha256": request_path,
        "result_sha256": result_path,
        "matrix_sha256": matrix_path,
        "action_raster_sha256": diff_path,
        "prepared_resistance_sha256": resistance_path,
        "footprint_proof_document_sha256": proof_path,
    }
    for field, path in named_hashes.items():
        if sha256_file(path) != job.get(field):
            raise PackagingError(
                f"named {field} differs from retained bytes: {job['direct_run_id']}"
            )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    expected_request = {
        "protocol_version": "1.0",
        "prepared_run_id": job["direct_run_id"],
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/pairs.tsv",
        },
        "pairs": [],
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
    pair_by_id = {item.pair_id: item for item in focal_set.pairs}
    prepared_pairs = [
        PreparedPair(
            pair_id=pair_id,
            from_focal_id=pair_by_id[pair_id].from_focal_id,
            to_focal_id=pair_by_id[pair_id].to_focal_id,
        )
        for pair_id in scenario.included_pair_ids
    ]
    expected_request["pairs"] = [item.model_dump(mode="json") for item in prepared_pairs]
    if request != expected_request:
        raise PackagingError(
            f"direct request differs from its exact contract: {job['direct_run_id']}"
        )
    protocol = EngineProtocolResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    expected_matrix_relative = "outputs/connectivity_resistances_3columns.out"
    expected_current_relative = "outputs/connectivity_cum_curmap.asc" if phase == "map" else None
    if (
        protocol.status != "SUCCEEDED"
        or protocol.engine_name != "Circuitscape"
        or protocol.circuitscape_version != "5.17.1"
        or protocol.matrix_path != expected_matrix_relative
        or protocol.current_map_path != expected_current_relative
        or protocol.review_mask_summaries
        or protocol.failure_message is not None
    ):
        raise PackagingError(
            f"result protocol is not a successful direct run: {job['direct_run_id']}"
        )
    reparsed = parse_circuitscape_matrix(matrix_path, prepared_pairs)
    expected_metrics = [
        item.model_dump(mode="json") for item in sorted(reparsed, key=lambda item: item.pair_id)
    ]
    if expected_metrics != [
        item.model_dump(mode="json")
        for item in sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
    ]:
        raise PackagingError(f"raw matrix and result protocol differ: {job['direct_run_id']}")
    verify_indexed_pair_metrics(
        job.get("pair_metrics", []),
        reparsed,
        direct_run_id=job["direct_run_id"],
    )
    expected_qa = QAStatus.WARNING_ACCEPTED.value if protocol.warnings else QAStatus.ACCEPTED.value
    if job.get("qa_status") != expected_qa or job.get("qa_findings", []) != protocol.warnings:
        raise PackagingError(f"raw warnings and indexed QA differ: {job['direct_run_id']}")
    validate_effective_circuitscape_configuration(
        config_path,
        resource_paths=request["resources"],
        output_directory="outputs",
        neighbor_rule=scenario.neighbor_rule.value,
        resistance_average=scenario.resistance_average.value,
        emit_current_map=phase == "map",
    )
    resistance_asset = assets[hypothesis.resistance_asset_id]
    baseline_path = evidence_root / "sources" / "resistance" / f"{hypothesis.hypothesis_id}.asc"
    normalized_baseline = load_esri_ascii(baseline_path, resistance_asset)
    baseline = fixture.rasters[resistance_asset.asset_id]
    if (
        normalized_baseline.grid != baseline.grid
        or normalized_baseline.values != baseline.values
        or baseline.source_sha256 != resistance_asset.sha256
    ):
        raise PackagingError(
            f"normalized resistance differs from reviewed raw asset: {scenario.scenario_id}"
        )
    prepared_resistance = load_esri_ascii(resistance_path, resistance_asset)
    effects = scenario_effects_for_portfolio(
        portfolio,
        fixture.bundle.intervention_options,
        scenario,
    )
    expected_prepared = apply_absolute_replacements(
        baseline,
        effects,
        derived_asset_id=prepared_resistance.asset_id,
    )
    if expected_prepared.values != prepared_resistance.values:
        raise PackagingError(f"prepared resistance cannot be reconstructed: {job['direct_run_id']}")
    if sha256_file(resistance_path) != job["prepared_resistance_sha256"]:
        raise PackagingError(f"prepared resistance hash differs: {job['direct_run_id']}")
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    verified_diff = verify_semantic_raster_diff(
        diff,
        baseline=baseline,
        effects=effects,
        prepared=prepared_resistance,
        expected_project_id=fixture.bundle.project_id,
        expected_revision_id=fixture.bundle.revision.revision_id,
        expected_model_identity_sha256=fixture.physics_model_sha256,
        expected_portfolio_id=portfolio.portfolio_id,
        expected_scenario_id=scenario.scenario_id,
        prepared_resistance_sha256=job["prepared_resistance_sha256"],
    )
    if verified_diff != job["action_raster_sha256"] or sha256_file(diff_path) != verified_diff:
        raise PackagingError(f"semantic raster diff hash differs: {job['direct_run_id']}")
    proof_document = FootprintProofDocument.model_validate_json(
        proof_path.read_text(encoding="utf-8")
    )
    verified_proof = verify_footprint_proof_document(
        proof_document,
        evidence_root=evidence_root,
        effects=effects,
        assets=fixture.bundle.assets,
    )
    if (
        verified_proof != job["footprint_proof_sha256"]
        or sha256_file(proof_path) != job["footprint_proof_document_sha256"]
    ):
        raise PackagingError(f"footprint proof hash differs: {job['direct_run_id']}")
    expected_cache_key = canonical_sha256(
        ridgeway_model_run_cache_payload(
            mode=request["mode"],
            raster_semantics=request["raster_semantics"],
            resistance_sha256=sha256_file(resistance_path),
            focal_sha256=sha256_file(focal_path),
            included_pairs_sha256=sha256_file(pairs_path),
            pairs=request["pairs"],
            settings=request["settings"],
            engine_manifest_sha256=evidence_index["engine_manifest_sha256"],
            engine_environment_sha256=evidence_index["engine_environment_sha256"],
            workflow_manifest_sha256=evidence_index["workflow_manifest_sha256"],
        )
    )
    if job.get("model_run_cache_key") != expected_cache_key:
        raise PackagingError(f"model-run cache identity differs: {job['direct_run_id']}")
    if (
        job.get("engine_manifest_sha256") != evidence_index.get("engine_manifest_sha256")
        or job.get("engine_environment_sha256") != evidence_index.get("engine_environment_sha256")
        or job.get("workflow_manifest_sha256") != evidence_index.get("workflow_manifest_sha256")
        or job.get("physics_model_sha256") != evidence_index.get("physics_model_sha256")
        or job.get("decision_snapshot_sha256") != evidence_index.get("decision_snapshot_sha256")
    ):
        raise PackagingError(
            f"job provenance identity differs from evidence index: {job['direct_run_id']}"
        )
    if phase == "map":
        current = _artifact_path(evidence_root, complete, suffix="cum_curmap.asc")
        if sha256_file(current) != job.get("current_map_sha256"):
            raise PackagingError(f"current-map hash differs: {job['direct_run_id']}")
    elif job.get("current_map_sha256") is not None:
        raise PackagingError(
            f"matrix-only job unexpectedly carries a current map: {job['direct_run_id']}"
        )


def current_map_path(evidence_root: Path, job: dict[str, Any]) -> Path:
    attempt = evidence_root / job["artifact_directory"]
    candidates = sorted((attempt / "outputs").glob("*cum_curmap*.asc"))
    if len(candidates) != 1:
        raise PackagingError(
            f"map job {job['direct_run_id']} must retain exactly one cumulative current raster"
        )
    return candidates[0]


def project_pair_results(
    *,
    baseline_job: dict[str, Any],
    portfolio_job: dict[str, Any],
    safeguard: bool,
    target_threshold: float,
) -> list[dict[str, Any]]:
    """Project every direct focal-pair value without collapsing the matrix."""

    scenario_id = str(portfolio_job.get("scenario_id", ""))
    portfolio_id = str(portfolio_job.get("portfolio_id", ""))
    if (
        not scenario_id
        or not portfolio_id
        or baseline_job.get("scenario_id") != scenario_id
        or baseline_job.get("portfolio_id") == portfolio_id
    ):
        raise PackagingError("pair projection requires a same-scenario, distinct baseline run")
    baseline_metrics = {
        str(item["pair_id"]): float(item["effective_resistance"])
        for item in baseline_job.get("pair_metrics", [])
    }
    portfolio_metrics = {
        str(item["pair_id"]): float(item["effective_resistance"])
        for item in portfolio_job.get("pair_metrics", [])
    }
    if not baseline_metrics or baseline_metrics.keys() != portfolio_metrics.keys():
        raise PackagingError(f"pair coverage differs for {portfolio_job.get('direct_run_id')}")
    for digest_field in ("matrix_sha256",):
        for job in (baseline_job, portfolio_job):
            if not re.fullmatch(r"[0-9a-f]{64}", str(job.get(digest_field, ""))):
                raise PackagingError(
                    f"pair projection lacks an exact {digest_field}: {job.get('direct_run_id')}"
                )
    limit = 0.0 if safeguard else -float(target_threshold)
    rows: list[dict[str, Any]] = []
    for pair_id in sorted(baseline_metrics):
        baseline = baseline_metrics[pair_id]
        observed = portfolio_metrics[pair_id]
        if (
            not math.isfinite(baseline)
            or baseline <= 0
            or not math.isfinite(observed)
            or observed <= 0
        ):
            raise PackagingError(
                f"pair projection contains an invalid effective resistance: {pair_id}"
            )
        change = (observed - baseline) / baseline * 100
        rows.append(
            {
                "pairId": pair_id,
                "baselineEffectiveResistance": baseline,
                "portfolioEffectiveResistance": observed,
                "baselineRelativeChangePct": change,
                "passed": change <= limit,
                "claimId": f"ECO-PAIR-{portfolio_id}-{scenario_id}-{pair_id}",
                "baselineRunId": str(baseline_job["direct_run_id"]),
                "portfolioRunId": str(portfolio_job["direct_run_id"]),
                "baselineMatrixSha256": str(baseline_job["matrix_sha256"]),
                "portfolioMatrixSha256": str(portfolio_job["matrix_sha256"]),
            }
        )
    return rows


def verify_map_matrix_coherence(
    matrix_jobs: list[dict[str, Any]], map_jobs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Prove each retained map rerun reproduces its study matrix within tolerance."""

    matrix_by_key = {
        (str(item.get("portfolio_id", "")), str(item.get("scenario_id", ""))): item
        for item in matrix_jobs
    }
    maximum_absolute = 0.0
    maximum_relative = 0.0
    values_compared = 0
    for map_job in map_jobs:
        key = (str(map_job.get("portfolio_id", "")), str(map_job.get("scenario_id", "")))
        matrix_job = matrix_by_key.get(key)
        if matrix_job is None:
            raise PackagingError(f"map rerun has no corresponding study matrix: {key}")
        for identity in (
            "physics_model_sha256",
            "decision_snapshot_sha256",
            "engine_manifest_sha256",
            "engine_environment_sha256",
            "workflow_manifest_sha256",
            "prepared_resistance_sha256",
            "action_raster_sha256",
            "footprint_proof_sha256",
            "footprint_proof_document_sha256",
        ):
            if map_job.get(identity) != matrix_job.get(identity):
                raise PackagingError(f"map rerun and study matrix differ in {identity}: {key}")
        matrix_pairs = {
            str(item.get("pair_id", "")): float(item.get("effective_resistance", math.nan))
            for item in matrix_job.get("pair_metrics", [])
        }
        map_pairs = {
            str(item.get("pair_id", "")): float(item.get("effective_resistance", math.nan))
            for item in map_job.get("pair_metrics", [])
        }
        if (
            not matrix_pairs
            or matrix_pairs.keys() != map_pairs.keys()
            or len(matrix_pairs) != len(matrix_job.get("pair_metrics", []))
            or len(map_pairs) != len(map_job.get("pair_metrics", []))
        ):
            raise PackagingError(f"map rerun pair coverage differs from study matrix: {key}")
        for pair_id in sorted(matrix_pairs):
            expected = matrix_pairs[pair_id]
            observed = map_pairs[pair_id]
            if (
                not math.isfinite(expected)
                or expected <= 0
                or not math.isfinite(observed)
                or observed <= 0
            ):
                raise PackagingError(f"map/matrix coherence contains an invalid value: {key}")
            absolute = abs(observed - expected)
            allowed = MATRIX_ABSOLUTE_TOLERANCE + MATRIX_RELATIVE_TOLERANCE * max(
                abs(observed), abs(expected)
            )
            relative = absolute / max(abs(expected), MATRIX_ABSOLUTE_TOLERANCE)
            if absolute > allowed:
                raise PackagingError(
                    "map rerun effective resistance differs from the study matrix outside "
                    f"tolerance: {key}:{pair_id}"
                )
            maximum_absolute = max(maximum_absolute, absolute)
            maximum_relative = max(maximum_relative, relative)
            values_compared += 1
    return {
        "map_jobs_compared": len(map_jobs),
        "pair_values_compared": values_compared,
        "maximum_absolute_difference": maximum_absolute,
        "maximum_relative_difference": maximum_relative,
        "absolute_tolerance": MATRIX_ABSOLUTE_TOLERANCE,
        "relative_tolerance": MATRIX_RELATIVE_TOLERANCE,
        "status": "WITHIN_TOLERANCE",
    }


def compact_current_raster(
    *,
    evidence_root: Path,
    job: dict[str, Any],
    expected_grid: Any,
) -> dict[str, Any]:
    """Retain exact row-major current cells while replacing only declared NODATA with null."""

    path = current_map_path(evidence_root, job)
    digest = sha256_file(path)
    if digest != job.get("current_map_sha256"):
        raise PackagingError(f"current-map hash differs: {job.get('direct_run_id')}")
    try:
        values, nodata = current_map_values_and_nodata(path, expected_grid=expected_grid)
    except RasterIOError as exc:
        raise PackagingError(f"current-map grid or data failed validation: {exc}") from exc
    cells: list[float | None] = []
    observed: list[float] = []
    for row in values:
        for value in row:
            if _is_declared_nodata(value, nodata):
                cells.append(None)
                continue
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise PackagingError(
                    f"current raster contains an invalid data cell: {job.get('direct_run_id')}"
                )
            cells.append(number)
            observed.append(number)
    if len(cells) != expected_grid.width * expected_grid.height or not observed:
        raise PackagingError(f"current raster has incomplete data: {job.get('direct_run_id')}")
    return {
        "runId": str(job["direct_run_id"]),
        "portfolioId": str(job["portfolio_id"]),
        "scenarioId": str(job["scenario_id"]),
        "sha256": digest,
        "width": expected_grid.width,
        "height": expected_grid.height,
        "declaredNodata": float(nodata),
        "nodataTopologySha256": canonical_sha256([value is None for value in cells]),
        "minimumCurrent": min(observed),
        "maximumCurrent": max(observed),
        "cells": cells,
    }


def review_mask_projection(fixture: Any) -> dict[str, Any]:
    mask_contract = fixture.bundle.review_masks[0]
    mask_asset = next(
        item for item in fixture.bundle.assets if item.asset_id == mask_contract.asset_id
    )
    mask = fixture.rasters[mask_asset.asset_id]
    cells: list[int] = []
    for row in mask.values:
        for value in row:
            if value is None or not float(value).is_integer() or int(value) not in {0, 1}:
                raise PackagingError("review-mask projection contains a non-binary cell")
            cells.append(int(value))
    return {
        "maskId": mask_contract.mask_id,
        "sha256": mask_asset.sha256,
        "width": mask.grid.width,
        "height": mask.grid.height,
        "cells": cells,
        "interpretation": "Diagnostic review mask; not observed or predicted animal use.",
    }


def _is_declared_nodata(value: float | None, nodata: float | None) -> bool:
    if value is None or nodata is None:
        return value is None
    if math.isnan(float(nodata)):
        return math.isnan(float(value))
    return math.isclose(float(value), float(nodata), rel_tol=0.0, abs_tol=1e-9)


def _review_mask_current_totals(
    baseline_path: Path,
    portfolio_path: Path,
    mask: Any,
) -> tuple[float, float]:
    try:
        comparison = compare_current_maps_in_mask(baseline_path, portfolio_path, mask)
    except RasterIOError as exc:
        raise PackagingError(f"current-map grid or data failed validation: {exc}") from exc
    return comparison.baseline_total, comparison.portfolio_total


def map_change_percent(
    evidence_root: Path,
    fixture: Any,
    baseline_job: dict[str, Any],
    portfolio_job: dict[str, Any],
) -> float:
    mask = fixture.rasters[fixture.bundle.review_masks[0].asset_id]
    baseline_total, portfolio_total = _review_mask_current_totals(
        current_map_path(evidence_root, baseline_job),
        current_map_path(evidence_root, portfolio_job),
        mask,
    )
    if baseline_total <= 0:
        raise PackagingError("baseline review-mask current is not positive")
    return (portfolio_total - baseline_total) / baseline_total * 100


def _direct_run(job: dict[str, Any]) -> DirectConnectivityRun:
    return DirectConnectivityRun.model_validate(
        {key: value for key, value in job.items() if key in DirectConnectivityRun.model_fields}
    )


def _package_matrix_summaries(fixture: Any, jobs: list[dict[str, Any]]) -> list[Any]:
    direct = [_direct_run(item) for item in jobs]
    baseline = [item for item in direct if item.portfolio_id == fixture.baseline_portfolio_id]
    rules = [
        item
        for item in fixture.bundle.decision_rules
        if item.metric != RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
    ]
    output = []
    for portfolio in fixture.portfolios:
        cost, currency, _ = portfolio_cost(portfolio, fixture.bundle.intervention_options)
        output.append(
            evaluate_portfolio(
                portfolio=portfolio,
                runs=[item for item in direct if item.portfolio_id == portfolio.portfolio_id],
                baseline_runs=baseline,
                scenarios=fixture.bundle.scenarios,
                rules=rules,
                cost=cost,
                currency=currency,
                unresolved_feasibility=unresolved_portfolio_concerns(
                    portfolio, fixture.bundle.intervention_options
                ),
            )
        )
    return output


def _target_reductions(summary: Any) -> list[float]:
    return [
        float(item.observed_value)
        for item in summary.rule_dispositions
        if item.rule_id == "rule_target_improvement"
        and isinstance(item.observed_value, (int, float))
    ]


def _package_hypothesis_ranks(fixture: Any, summaries: list[Any]) -> dict[str, dict[str, Any]]:
    scenario_by_id = {item.scenario_id: item for item in fixture.bundle.scenarios}
    scores: dict[str, dict[str, float]] = {}
    for summary in summaries:
        grouped: dict[str, list[float]] = {}
        for disposition in summary.rule_dispositions:
            if (
                disposition.rule_id != "rule_target_improvement"
                or disposition.scenario_id is None
                or not isinstance(disposition.observed_value, (int, float))
            ):
                continue
            scenario = scenario_by_id[disposition.scenario_id]
            if not scenario.safeguard:
                grouped.setdefault(scenario.resistance_hypothesis_id, []).append(
                    float(disposition.observed_value)
                )
        scores[summary.portfolio_id] = {
            hypothesis_id: min(values) for hypothesis_id, values in grouped.items()
        }
    ranks: dict[str, dict[str, Any]] = {item.portfolio_id: {} for item in summaries}
    for hypothesis in sorted(item.hypothesis_id for item in fixture.bundle.resistance_hypotheses):
        ordered = sorted(
            (
                (portfolio_id, values[hypothesis])
                for portfolio_id, values in scores.items()
                if hypothesis in values
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for rank, (portfolio_id, score) in enumerate(ordered, start=1):
            ranks[portfolio_id][hypothesis] = {"rank": rank, "score": score}
    return ranks


HYPOTHESIS_RANKING_METRIC = (
    "Minimum required target-pair resistance reduction (%) within each resistance hypothesis; "
    "rank 1 is the highest score among the 128 directly evaluated portfolios."
)


def independently_aggregate_resource_observability(
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate every retained observation and aggregate without trusting UI data."""

    observations: list[ComputationalResourceObservation] = []
    observation_ids: set[str] = set()
    for job in jobs:
        raw = job.get("resource_observation")
        if raw is None:
            raise PackagingError(
                f"direct job has no resource observation: {job.get('direct_run_id', 'unknown')}"
            )
        try:
            observation = ComputationalResourceObservation.model_validate(raw)
        except ValueError as exc:
            raise PackagingError(
                f"direct job has invalid resource observation: "
                f"{job.get('direct_run_id', 'unknown')}"
            ) from exc
        if observation.boundary not in {
            ResourceObservationBoundary.DIRECT_SOLVER,
            ResourceObservationBoundary.CACHE_REUSE,
        }:
            raise PackagingError("Ridgeway direct evidence has a non-direct resource boundary")
        if observation.observation_id in observation_ids:
            raise PackagingError("Ridgeway resource observation IDs must be unique")
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


def _aware_process_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise PackagingError(f"process-tree {label} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PackagingError(f"process-tree {label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PackagingError(f"process-tree {label} is not timezone-aware")
    return parsed


def independently_validate_process_tree_exits(
    jobs: list[dict[str, Any]],
) -> dict[str, object]:
    """Validate per-job tree-exit proof without trusting the producer summary."""

    identities: set[str] = set()
    methods: Counter[str] = Counter()
    for job in jobs:
        direct_run_id = str(job.get("direct_run_id", "unknown"))
        evidence = job.get("process_exit_evidence")
        if not isinstance(evidence, dict) or set(evidence) != _PROCESS_TREE_EXIT_KEYS:
            raise PackagingError(
                f"direct job has incomplete process-tree exit evidence: {direct_run_id}"
            )
        process_id = evidence["process_id"]
        process_group_id = evidence["process_group_id"]
        process_identity = evidence["process_identity"]
        if (
            isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
            or isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or process_group_id != process_id
            or not isinstance(process_identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
            or evidence["termination_reason"] != "natural_exit"
            or evidence["tree_exit_confirmed"] is not True
            or evidence["confirmation_method"] != "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT"
        ):
            raise PackagingError(
                f"direct job process-tree exit evidence is invalid: {direct_run_id}"
            )
        launched_at = _aware_process_timestamp(evidence["launched_at"], label="launch time")
        confirmed_at = _aware_process_timestamp(evidence["confirmed_at"], label="confirmation time")
        if confirmed_at < launched_at:
            raise PackagingError(f"direct job process-tree exit predates launch: {direct_run_id}")
        if process_identity in identities:
            raise PackagingError(
                f"direct jobs reuse a process-execution identity: {process_identity}"
            )
        identities.add(process_identity)
        methods[str(evidence["confirmation_method"])] += 1
    return {
        "jobs_with_confirmed_tree_exit": len(jobs),
        "jobs_without_confirmed_tree_exit": 0,
        "unique_process_identities": len(identities),
        "confirmation_methods": dict(sorted(methods.items())),
    }


def validated_hypothesis_rank_observations(
    fixture: Any,
    observations: object,
    *,
    portfolio_id: str,
) -> dict[str, dict[str, float | int]]:
    """Return one complete, finite, directly recomputed rank record."""

    expected_hypotheses = {item.hypothesis_id for item in fixture.bundle.resistance_hypotheses}
    if not isinstance(observations, dict) or set(observations) != expected_hypotheses:
        raise PackagingError(f"hypothesis-rank evidence is incomplete for {portfolio_id}")
    validated: dict[str, dict[str, float | int]] = {}
    for hypothesis_id in sorted(expected_hypotheses):
        observation = observations[hypothesis_id]
        if not isinstance(observation, dict) or set(observation) != {"rank", "score"}:
            raise PackagingError(
                f"hypothesis-rank observation is malformed for {portfolio_id}:{hypothesis_id}"
            )
        rank = observation["rank"]
        score = observation["score"]
        if (
            isinstance(rank, bool)
            or not isinstance(rank, int)
            or not 1 <= rank <= len(fixture.portfolios)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            raise PackagingError(
                f"hypothesis-rank value is invalid for {portfolio_id}:{hypothesis_id}"
            )
        validated[hypothesis_id] = {"rank": rank, "score": float(score)}
    return validated


def independently_select_matrix_roles(
    fixture: Any, matrix_jobs: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    summaries = _package_matrix_summaries(fixture, matrix_jobs)
    budget = float(fixture.bundle.decision_brief.budget_amount or 0)
    baseline = fixture.baseline_portfolio_id
    caution_pool = [
        item
        for item in summaries
        if item.portfolio_id != baseline
        and item.cost <= budget
        and not item.unresolved_feasibility
        and _target_reductions(item)
        and max(_target_reductions(item)) > 0
    ]
    if not caution_pool:
        raise PackagingError("direct matrix evidence has no low-cost positive lesson")
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
        raise PackagingError("direct matrix evidence has no robust under-budget lesson")
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
        raise PackagingError("direct matrix evidence has no cost-only failure lesson")
    over = max(
        over_pool,
        key=lambda item: (item.worst_resistance_reduction_percent or -math.inf, -item.cost),
    )
    ranks = _package_hypothesis_ranks(fixture, summaries)
    minimum_swing = max(5, len(summaries) // 10)
    top_third = max(1, math.ceil(len(summaries) / 3))
    collapse_pool = []
    for item in summaries:
        if (
            item.portfolio_id
            in {
                baseline,
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
        swing = max(rank_values) - min(rank_values)
        if min(rank_values) <= top_third and swing >= minimum_swing:
            collapse_pool.append((swing, max(score_values) - min(score_values), item))
    if not collapse_pool:
        raise PackagingError("direct matrix evidence has no hypothesis-rank collapse lesson")
    under_budget_collapse_pool = [item for item in collapse_pool if item[2].cost <= budget]
    collapse = max(
        under_budget_collapse_pool or collapse_pool,
        key=lambda item: (item[0], item[1], item[2].portfolio_id),
    )[2]
    return {"caution": caution, "carry": carry, "over": over, "collapse": collapse}, ranks


def option_summary(fixture: Any, portfolio_id: str) -> str:
    portfolio = next(item for item in fixture.portfolios if item.portfolio_id == portfolio_id)
    options = {item.option_id: item for item in fixture.bundle.intervention_options}
    selected = [
        options[option_id]
        for option_id in portfolio.canonical_option_ids
        if options[option_id].kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
    ]
    counts = Counter(item.kind.value.replace("_", " ").lower() for item in selected)
    details = ", ".join(f"{count} {title}" for title, count in sorted(counts.items()))
    return f"{len(selected)} reviewed site actions: {details}."


def build_ui_projection(
    *,
    evidence_root: Path,
    evidence_index: dict[str, Any],
    direct_manifest: dict[str, Any],
    manifest_sha256: str,
    raw_projection: dict[str, Any],
) -> dict[str, Any]:
    fixture = build_ridgeway_fixture()
    matrix_jobs = {
        (item["portfolio_id"], item["scenario_id"]): item for item in evidence_index["matrix_jobs"]
    }
    map_jobs = {
        (item["portfolio_id"], item["scenario_id"]): item for item in evidence_index["map_jobs"]
    }
    raw_by_id = {item["id"]: item for item in raw_projection["portfolios"]}
    independently_selected, independently_ranked = independently_select_matrix_roles(
        fixture, list(matrix_jobs.values())
    )
    resource_observability = independently_aggregate_resource_observability(
        [*evidence_index["matrix_jobs"], *evidence_index["map_jobs"]]
    )
    if raw_projection.get("resourceObservability") != resource_observability:
        raise PackagingError(
            "raw resource-observability summary differs from retained direct observations"
        )
    baseline_id = fixture.baseline_portfolio_id
    pair_baselines = {
        scenario.scenario_id: {
            item["pair_id"]: float(item["effective_resistance"])
            for item in matrix_jobs[(baseline_id, scenario.scenario_id)]["pair_metrics"]
        }
        for scenario in fixture.bundle.scenarios
    }
    selection = direct_manifest["teaching_selection"]
    roles = [
        (
            "low-cost-caution",
            selection["low_cost_caution_portfolio_id"],
            "Low-cost caution",
            "caution",
        ),
        (
            "carry-forward",
            selection["carry_forward_portfolio_id"],
            "Carry-forward candidate",
            "carry-forward",
        ),
        (
            "over-budget",
            selection["over_budget_portfolio_id"],
            "Modeled-rule pass, over budget",
            "over-budget",
        ),
        (
            "single-hypothesis-collapse",
            selection["hypothesis_collapse_portfolio_id"],
            "Hypothesis-sensitive option",
            "brittle",
        ),
    ]
    selected_ids = {
        "caution": selection["low_cost_caution_portfolio_id"],
        "carry": selection["carry_forward_portfolio_id"],
        "over": selection["over_budget_portfolio_id"],
        "collapse": selection["hypothesis_collapse_portfolio_id"],
    }
    if {
        role: summary.portfolio_id for role, summary in independently_selected.items()
    } != selected_ids:
        raise PackagingError("teaching selection differs from independently recomputed ranks")
    map_portfolio_order = [baseline_id, *(portfolio_id for _, portfolio_id, _, _ in roles)]
    expected_map_keys = {
        (portfolio_id, scenario.scenario_id)
        for portfolio_id in map_portfolio_order
        for scenario in fixture.bundle.scenarios
    }
    if set(map_jobs) != expected_map_keys or len(map_jobs) != 60:
        raise PackagingError("the UI projection requires exactly the 60 retained teaching maps")
    hypotheses = {item.hypothesis_id: item for item in fixture.bundle.resistance_hypotheses}
    assets = {item.asset_id: item for item in fixture.bundle.assets}
    current_rasters = []
    for portfolio_id in map_portfolio_order:
        for scenario in fixture.bundle.scenarios:
            hypothesis = hypotheses[scenario.resistance_hypothesis_id]
            expected_grid = assets[hypothesis.resistance_asset_id].grid
            if expected_grid is None:
                raise PackagingError(
                    f"scenario has no reviewed raster grid: {scenario.scenario_id}"
                )
            current_rasters.append(
                compact_current_raster(
                    evidence_root=evidence_root,
                    job=map_jobs[(portfolio_id, scenario.scenario_id)],
                    expected_grid=expected_grid,
                )
            )
    if len({item["runId"] for item in current_rasters}) != 60:
        raise PackagingError("retained teaching current-raster run identities are not unique")
    target_threshold = next(
        float(item.threshold)
        for item in fixture.bundle.decision_rules
        if item.rule_id == "rule_target_improvement"
    )
    review_threshold = fixture.bundle.review_masks[0].review_trigger_percent
    budget = float(fixture.bundle.decision_brief.budget_amount or 0)
    portfolio_rows = []
    for projection_key, portfolio_id, name, role in roles:
        rank_observations = validated_hypothesis_rank_observations(
            fixture,
            independently_ranked.get(portfolio_id),
            portfolio_id=portfolio_id,
        )
        raw_rank_observations = validated_hypothesis_rank_observations(
            fixture,
            raw_by_id[portfolio_id].get("hypothesisRanks"),
            portfolio_id=portfolio_id,
        )
        if raw_rank_observations != rank_observations:
            raise PackagingError(
                f"raw hypothesis-rank evidence differs from direct matrices for {portfolio_id}"
            )
        scenario_rows = []
        raw_scenarios = {
            item["scenarioId"]: item for item in raw_by_id[portfolio_id]["scenarioResults"]
        }
        for scenario in fixture.bundle.scenarios:
            job = matrix_jobs[(portfolio_id, scenario.scenario_id)]
            baseline_job = matrix_jobs[(baseline_id, scenario.scenario_id)]
            pair_rows = project_pair_results(
                baseline_job=baseline_job,
                portfolio_job=job,
                safeguard=scenario.safeguard,
                target_threshold=target_threshold,
            )
            baseline = pair_baselines[scenario.scenario_id]
            if {item["pairId"] for item in pair_rows} != set(baseline):
                raise PackagingError(f"pair projection differs for {job['direct_run_id']}")
            resistance_change = max(item["baselineRelativeChangePct"] for item in pair_rows)
            required_pass = all(item["passed"] for item in pair_rows)
            map_change = map_change_percent(
                evidence_root,
                fixture,
                map_jobs[(baseline_id, scenario.scenario_id)],
                map_jobs[(portfolio_id, scenario.scenario_id)],
            )
            raw_change = float(raw_scenarios[scenario.scenario_id]["roadMaskCurrentChangePct"])
            if not math.isclose(map_change, raw_change, rel_tol=1e-12, abs_tol=1e-12):
                raise PackagingError(f"road-mask reducer mismatch for {job['direct_run_id']}")
            review = map_change > review_threshold
            scenario_rows.append(
                {
                    "scenarioId": scenario.scenario_id,
                    "resistanceChangePct": resistance_change,
                    "ruleStatus": "fail" if not required_pass else "review" if review else "pass",
                    "reviewMaskChangePct": map_change,
                    "runId": job["direct_run_id"],
                    "baselineMapRunId": map_jobs[(baseline_id, scenario.scenario_id)][
                        "direct_run_id"
                    ],
                    "portfolioMapRunId": map_jobs[(portfolio_id, scenario.scenario_id)][
                        "direct_run_id"
                    ],
                    "pairResults": pair_rows,
                }
            )
        cost, currency, _ = portfolio_cost(
            next(item for item in fixture.portfolios if item.portfolio_id == portfolio_id),
            fixture.bundle.intervention_options,
        )
        if currency != "USD" or not math.isclose(cost, float(raw_by_id[portfolio_id]["costUsd"])):
            raise PackagingError(f"cost evidence mismatch for {portfolio_id}")
        required_pass_count = sum(item["ruleStatus"] != "fail" for item in scenario_rows)
        road_flag = any(item["ruleStatus"] == "review" for item in scenario_rows)
        required_failure = required_pass_count != len(fixture.bundle.scenarios) or cost > budget
        rule_status = "fail" if required_failure else "review" if road_flag else "pass"
        controlling = max(scenario_rows, key=lambda item: item["resistanceChangePct"])
        next_actions = {
            "caution": "Resolve road-mask redistribution and field assumptions before carry-forward.",
            "carry-forward": (
                "Candidate to carry forward for ecological, field, and engineering feasibility review."
            ),
            "over-budget": "Do not carry forward under the current cost rule; inspect tradeoffs.",
            "brittle": "Resolve hypothesis sensitivity and controlling assumptions before carry-forward.",
        }
        if road_flag and role == "carry-forward":
            next_actions[role] = (
                "Resolve modeled road-mask current redistribution, then consider this "
                "candidate for ecological, field, and engineering feasibility review."
            )
        portfolio_rows.append(
            {
                "projectionKey": projection_key,
                "id": portfolio_id,
                "name": name,
                "role": role,
                "costUsd": cost,
                "ruleStatus": rule_status,
                "passCount": required_pass_count,
                "requiredCount": len(fixture.bundle.scenarios),
                "worstResistanceChangePct": controlling["resistanceChangePct"],
                "worstScenarioId": controlling["scenarioId"],
                "roadMaskFlag": road_flag,
                "nextAction": next_actions[role],
                "optionSummary": option_summary(fixture, portfolio_id),
                "hypothesisRanks": rank_observations,
                "scenarioResults": scenario_rows,
            }
        )
    hypothesis_titles = {
        item.hypothesis_id: item.title for item in fixture.bundle.resistance_hypotheses
    }
    guild_titles = {
        "target_guild_a": "Target guild A",
        "target_guild_b": "Target guild B",
        "safeguard_guild": "Safeguard guild",
    }
    return {
        "schemaVersion": UI_SCHEMA,
        "fixtureId": fixture.bundle.project_id,
        "baselinePortfolioId": fixture.baseline_portfolio_id,
        "title": fixture.bundle.title,
        "cc0": True,
        "manifestSha256": manifest_sha256,
        "manifestVersion": fixture.fixture_version,
        "evidenceState": "VERIFIED_DIRECT_EVIDENCE",
        "resourceObservability": resource_observability,
        "releaseReplayEligible": True,
        "releaseBlocker": "",
        "projectionGeneratedAt": FIXED_TIME.isoformat().replace("+00:00", "Z"),
        "disclaimer": fixture.notice,
        "decisionBrief": {
            "question": fixture.bundle.decision_brief.decision_question,
            "budgetUsd": budget,
            "targetRule": (
                "Reduce effective resistance by at least 3% for every required target pair "
                "within each target scenario."
            ),
            "safeguardRule": (
                "Do not increase effective resistance for any required safeguard pair; "
                "road-mask current redistribution remains a diagnostic review flag."
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
            "hypothesisRankingMetric": HYPOTHESIS_RANKING_METRIC,
        },
        "currentRasters": current_rasters,
        "reviewMask": review_mask_projection(fixture),
        "scenarios": [
            {
                "id": scenario.scenario_id,
                "guild": guild_titles[scenario.guild_id],
                "purpose": "safeguard" if scenario.safeguard else "target",
                "hypothesis": hypothesis_titles[scenario.resistance_hypothesis_id],
                "futureCondition": scenario.future_condition,
                "maturity": scenario.maturity_horizon,
                "includedPairs": len(scenario.included_pair_ids),
            }
            for scenario in fixture.bundle.scenarios
        ],
        "portfolios": portfolio_rows,
    }


def write_archive(
    *,
    output_directory: Path,
    bundle_index: dict[str, Any],
    members: dict[str, Member],
) -> tuple[Path, str, int]:
    index_payload = canonical_json_bytes(bundle_index)
    temporary = output_directory / ".ridgeway-direct-evidence.tar.gz.tmp"
    with (
        temporary.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        entries: list[tuple[str, bytes | None, Path | None]] = [
            ("bundle-index.json", index_payload, None)
        ]
        entries.extend((path, None, members[path].source) for path in sorted(members))
        for path, payload, source in entries:
            if payload is None:
                if source is None:
                    raise PackagingError(f"archive source is absent for {path}")
                data = source.read_bytes()
                member = members[path]
                if (
                    len(data) != member.byte_size
                    or hashlib.sha256(data).hexdigest() != member.sha256
                ):
                    raise PackagingError(f"evidence source changed during archive write: {path}")
            else:
                data = payload
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
        raw.flush()
        os.fsync(raw.fileno())
    byte_size = temporary.stat().st_size
    if byte_size >= ARCHIVE_LIMIT_BYTES:
        raise PackagingError(
            f"evidence archive is {byte_size} bytes; GitHub requires less than {ARCHIVE_LIMIT_BYTES}"
        )
    archive_digest = sha256_file(temporary)
    final = output_directory / f"ridgeway-direct-evidence-{archive_digest[:12]}.tar.gz"
    if final.exists():
        if sha256_file(final) != archive_digest:
            raise PackagingError(f"existing archive hash mismatch: {final}")
        temporary.unlink()
    else:
        temporary.replace(final)
        directory_descriptor = os.open(output_directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    expected = {"bundle-index.json": hashlib.sha256(index_payload).hexdigest()}
    expected.update({path: item.sha256 for path, item in members.items()})
    observed: dict[str, str] = {}
    try:
        with tarfile.open(final, mode="r:gz") as archive:
            for entry in archive:
                name = safe_archive_path(entry.name)
                if not entry.isfile() or name in observed:
                    raise PackagingError("written evidence archive has a nonregular duplicate")
                stream = archive.extractfile(entry)
                if stream is None:
                    raise PackagingError(f"written evidence member cannot be reopened: {name}")
                digest = hashlib.sha256()
                observed_bytes = 0
                for block in iter(lambda source=stream: source.read(1024 * 1024), b""):
                    observed_bytes += len(block)
                    digest.update(block)
                if observed_bytes != entry.size:
                    raise PackagingError(f"written evidence member is truncated: {name}")
                observed[name] = digest.hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise PackagingError("written evidence archive failed reopen verification") from exc
    if observed != expected:
        raise PackagingError("written archive bytes differ from the verified member set")
    return final, archive_digest, byte_size


def package(evidence_root: Path, publish_directory: Path, desktop_fixture: Path) -> dict[str, Any]:
    evidence_root = evidence_root.resolve()
    fixture = build_ridgeway_fixture()
    evidence_path = evidence_root / "evidence-index.json"
    source_manifest_path = evidence_root / "public" / "direct-evidence-manifest.json"
    raw_projection_path = evidence_root / "public" / "ui_projection.json"
    for path in (evidence_path, source_manifest_path, raw_projection_path):
        if not path.is_file() or path.is_symlink():
            raise PackagingError(
                f"required completed evidence artifact is absent or unsafe: {path}"
            )
    evidence_index = json.loads(evidence_path.read_text(encoding="utf-8"))
    direct_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    raw_projection = json.loads(raw_projection_path.read_text(encoding="utf-8"))
    evidence_sha = sha256_file(evidence_path)
    source_manifest_sha = sha256_file(source_manifest_path)
    raw_projection_sha = sha256_file(raw_projection_path)
    if evidence_sha != direct_manifest["evidence_index_sha256"]:
        raise PackagingError("direct manifest does not bind the completed evidence index")
    if raw_projection_sha != direct_manifest.get("ui_projection_sha256"):
        raise PackagingError("raw projection is not bound by the direct-study manifest")
    if (
        direct_manifest.get("schema_version") != "ridgeway-direct-evidence/v1.1"
        or evidence_index.get("schema_version") != "ridgeway-evidence-index/v1.1"
        or raw_projection.get("schemaVersion") != RAW_UI_SCHEMA
        or direct_manifest.get("release_replay_eligible") is not False
        or direct_manifest.get("release_blocker") != "PACKAGE_VERIFICATION_REQUIRED"
        or raw_projection.get("releaseReplayEligible") is not False
        or raw_projection.get("evidenceState") != "PACKAGE_VERIFICATION_REQUIRED"
    ):
        raise PackagingError("direct runner bypassed the independent package-verification gate")
    if direct_manifest["model_sha256"] != fixture.bundle.revision.model_sha256:
        raise PackagingError("direct evidence belongs to another Ridgeway model")
    for field, expected in (
        ("physics_model_sha256", fixture.physics_model_sha256),
        ("decision_snapshot_sha256", fixture.decision_snapshot_sha256),
    ):
        if direct_manifest.get(field) != expected or evidence_index.get(field) != expected:
            raise PackagingError(f"direct evidence has a different {field}")
    source_manifest, source_files = verify_source_evidence(evidence_root, fixture, evidence_index)
    engine_manifest_path, snapshot_files = verify_engine_and_workflow_snapshots(
        evidence_root, evidence_index
    )
    matrix_jobs = evidence_index["matrix_jobs"]
    map_jobs = evidence_index["map_jobs"]
    if len(matrix_jobs) != 1536 or len(map_jobs) != 60:
        raise PackagingError("direct study coverage is incomplete")
    expected_matrix = {
        (portfolio.portfolio_id, scenario.scenario_id)
        for portfolio in fixture.portfolios
        for scenario in fixture.bundle.scenarios
    }
    package_roles, _package_hypothesis_ranks = independently_select_matrix_roles(
        fixture, matrix_jobs
    )
    package_resource_observability = independently_aggregate_resource_observability(
        [*matrix_jobs, *map_jobs]
    )
    package_process_tree_exits = independently_validate_process_tree_exits(
        [*matrix_jobs, *map_jobs]
    )
    if (
        raw_projection.get("resourceObservability") != package_resource_observability
        or direct_manifest.get("resource_observability") != package_resource_observability
    ):
        raise PackagingError(
            "runner resource-observability summaries differ from retained direct observations"
        )
    if (
        evidence_index.get("process_tree_exit_evidence") != package_process_tree_exits
        or direct_manifest.get("process_tree_exit_evidence") != package_process_tree_exits
    ):
        raise PackagingError(
            "runner process-tree exit summary differs from retained per-job evidence"
        )
    expected_selection_ids = {
        "low_cost_caution_portfolio_id": package_roles["caution"].portfolio_id,
        "carry_forward_portfolio_id": package_roles["carry"].portfolio_id,
        "over_budget_portfolio_id": package_roles["over"].portfolio_id,
        "hypothesis_collapse_portfolio_id": package_roles["collapse"].portfolio_id,
    }
    source_selection = direct_manifest.get("teaching_selection", {})
    if any(source_selection.get(key) != value for key, value in expected_selection_ids.items()):
        raise PackagingError("direct-study teaching roles differ from independent matrix evidence")
    expected_map_order = [fixture.baseline_portfolio_id] + [
        package_roles[key].portfolio_id for key in ("caution", "carry", "over", "collapse")
    ]
    if source_selection.get("map_rerun_portfolio_ids") != expected_map_order:
        raise PackagingError(
            "direct-study map rerun order differs from independently selected roles"
        )
    selected_map_ids = set(expected_map_order)
    expected_map = {
        (portfolio_id, scenario.scenario_id)
        for portfolio_id in selected_map_ids
        for scenario in fixture.bundle.scenarios
    }
    observed_matrix = {(item["portfolio_id"], item["scenario_id"]) for item in matrix_jobs}
    observed_map = {(item["portfolio_id"], item["scenario_id"]) for item in map_jobs}
    if observed_matrix != expected_matrix or observed_map != expected_map:
        raise PackagingError("direct job identities do not match required Ridgeway coverage")
    map_matrix_coherence = verify_map_matrix_coherence(matrix_jobs, map_jobs)
    members: dict[str, Member] = {}
    source_records = {item.relative_path: item for item in source_manifest.files}
    for relative, source in sorted(source_files.items()):
        record = source_records.get(relative)
        add_member(
            members,
            evidence_root=evidence_root,
            source=source,
            role=(
                "source_materialization_manifest"
                if relative == "materialization-manifest.json"
                else role_for_source(relative, record.role if record else None)
            ),
        )
    for relative, source in sorted(snapshot_files.items()):
        role = "workflow_snapshot" if relative.startswith("workflow/") else "engine_snapshot"
        add_member(
            members,
            evidence_root=evidence_root,
            source=source,
            role=role,
        )
    add_member(
        members,
        evidence_root=evidence_root,
        source=engine_manifest_path,
        role="typed_engine_manifest",
    )
    add_member(
        members,
        evidence_root=evidence_root,
        source=evidence_path,
        role="evidence_index",
        expected_sha256=evidence_sha,
    )
    add_raw_producer_inputs(
        members,
        evidence_root=evidence_root,
        direct_manifest_path=source_manifest_path,
        raw_projection_path=raw_projection_path,
        direct_manifest_sha256=source_manifest_sha,
        raw_projection_sha256=raw_projection_sha,
    )
    for phase, jobs in (("matrix", matrix_jobs), ("map", map_jobs)):
        for job in jobs:
            if (
                job["phase"] != phase
                or job["state"] != "SUCCEEDED"
                or not job["exact"]
                or job["qa_status"] != "ACCEPTED"
                or job["engine_manifest_sha256"] != evidence_index["engine_manifest_sha256"]
                or job.get("engine_environment_sha256")
                != evidence_index["engine_environment_sha256"]
                or job.get("workflow_manifest_sha256") != evidence_index["workflow_manifest_sha256"]
                or job.get("physics_model_sha256") != fixture.physics_model_sha256
                or job.get("decision_snapshot_sha256") != fixture.decision_snapshot_sha256
            ):
                raise PackagingError(f"job is not accepted direct evidence: {job['direct_run_id']}")
            complete_path = (
                evidence_root
                / "jobs"
                / phase
                / job["portfolio_id"]
                / job["scenario_id"]
                / "complete.json"
            )
            complete = json.loads(complete_path.read_text(encoding="utf-8"))
            completion_job = complete.get("job")
            if isinstance(completion_job, dict):
                completion_job = dict(completion_job)
                completion_job.pop("cached", None)
            if complete_path.is_symlink() or completion_job != job:
                raise PackagingError(
                    f"completion record differs from index: {job['direct_run_id']}"
                )
            cache_lineage = complete.get("cache_lineage")
            if cache_lineage is not None:
                if not isinstance(cache_lineage, dict) or set(cache_lineage) != {
                    "source_completion_sha256",
                    "source_completion_relative_path",
                    "source_artifact_directory",
                    "revalidation",
                }:
                    raise PackagingError(f"cache lineage is invalid: {job['direct_run_id']}")
                source_digest = cache_lineage["source_completion_sha256"]
                source_relative = safe_archive_path(
                    str(cache_lineage["source_completion_relative_path"])
                )
                expected_lineage_prefix = (
                    f"jobs/{phase}/{job['portfolio_id']}/{job['scenario_id']}/cache-lineage/"
                )
                if (
                    not isinstance(source_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
                    or source_relative != f"{expected_lineage_prefix}{source_digest}.json"
                ):
                    raise PackagingError(
                        f"cache lineage identity is invalid: {job['direct_run_id']}"
                    )
                source_completion = evidence_root / source_relative
                if (
                    not source_completion.is_file()
                    or source_completion.is_symlink()
                    or sha256_file(source_completion) != source_digest
                ):
                    raise PackagingError(
                        f"cache-source completion is absent or altered: {job['direct_run_id']}"
                    )
                source_payload = json.loads(source_completion.read_text(encoding="utf-8"))
                source_job = source_payload.get("job")
                if (
                    not isinstance(source_job, dict)
                    or source_job.get("artifact_directory")
                    != cache_lineage["source_artifact_directory"]
                ):
                    raise PackagingError(
                        f"cache-source artifact lineage is inconsistent: {job['direct_run_id']}"
                    )
            verify_direct_job_semantics(
                evidence_root=evidence_root,
                fixture=fixture,
                evidence_index=evidence_index,
                job=job,
                complete=complete,
                phase=phase,
            )
            add_member(
                members,
                evidence_root=evidence_root,
                source=complete_path,
                role=f"{phase}_completion_record",
            )
            for relative, digest in complete["artifact_hashes"].items():
                add_member(
                    members,
                    evidence_root=evidence_root,
                    source=evidence_root / safe_archive_path(relative),
                    role=role_for_artifact(phase, relative),
                    expected_sha256=digest,
                )
            required_roles = {
                f"{phase}_request",
                f"{phase}_result",
                f"{phase}_matrix",
                f"{phase}_completion_record",
                "footprint_proof_records",
                "semantic_raster_diff",
                "circuitscape_effective_configuration",
                f"{phase}_materialized_input",
            }
            if phase == "map":
                required_roles.add("map_current_raster")
            job_prefix = f"jobs/{phase}/{job['portfolio_id']}/{job['scenario_id']}/"
            observed_roles = {
                item.role for path, item in members.items() if path.startswith(job_prefix)
            }
            if not required_roles.issubset(observed_roles):
                raise PackagingError(f"required artifacts are absent for {job['direct_run_id']}")
    map_by_identity = {(item["portfolio_id"], item["scenario_id"]): item for item in map_jobs}
    mask_contract = fixture.bundle.review_masks[0]
    mask = fixture.rasters[mask_contract.asset_id]
    expected_diagnostics: list[dict[str, object]] = []
    role_flags: dict[str, bool] = {}
    for role, summary in package_roles.items():
        diagnostics = []
        triggered = False
        for scenario in fixture.bundle.scenarios:
            baseline_job = map_by_identity[(fixture.baseline_portfolio_id, scenario.scenario_id)]
            portfolio_job = map_by_identity[(summary.portfolio_id, scenario.scenario_id)]
            try:
                comparison = compare_current_maps_in_mask(
                    current_map_path(evidence_root, baseline_job),
                    current_map_path(evidence_root, portfolio_job),
                    mask,
                )
            except RasterIOError as exc:
                raise PackagingError(
                    f"map evidence cannot be compared for {portfolio_job['direct_run_id']}: {exc}"
                ) from exc
            if comparison.baseline_total <= 0:
                raise PackagingError("baseline review-mask current must be positive")
            change = (
                (comparison.portfolio_total - comparison.baseline_total)
                / comparison.baseline_total
                * 100
            )
            review = change > mask_contract.review_trigger_percent
            triggered |= review
            diagnostics.append(
                {
                    "mask_id": mask_contract.mask_id,
                    "baseline_total_current": comparison.baseline_total,
                    "portfolio_total_current": comparison.portfolio_total,
                    "relative_change_percent": change,
                    "review_triggered": review,
                    "reviewed_cell_count": comparison.reviewed_cell_count,
                    "included_cell_count": comparison.included_cell_count,
                    "excluded_nodata_cell_count": comparison.excluded_nodata_cell_count,
                    "raster_nodata_cell_count": comparison.raster_nodata_cell_count,
                    "nodata_topology_sha256": comparison.nodata_topology_sha256,
                    "interpretation": (
                        "Diagnostic current redistribution; not predicted animal use."
                    ),
                }
            )
            expected_diagnostics.append(
                {
                    "direct_run_id": portfolio_job["direct_run_id"],
                    "summaries": [diagnostics[-1]],
                }
            )
        role_flags[role] = triggered
    if not role_flags.get("caution"):
        raise PackagingError("selected low-cost caution has no observed road-mask review flag")
    observed_diagnostics = sorted(
        evidence_index.get("map_diagnostic_records", []),
        key=lambda item: str(item.get("direct_run_id")),
    )
    if observed_diagnostics != sorted(
        expected_diagnostics, key=lambda item: str(item["direct_run_id"])
    ):
        raise PackagingError("retained map diagnostic records differ from raw current rasters")
    report_snapshots = ensure_reviewed_report_snapshots(evidence_root)
    for snapshot, digest, role in report_snapshots.values():
        add_member(
            members,
            evidence_root=evidence_root,
            source=snapshot,
            role=role,
            expected_sha256=digest,
        )
    packager_sha = report_snapshots["package_ridgeway_evidence.py"][1]
    claim_safety_sha = report_snapshots["claim_safety.py"][1]
    portfolio_order = [item.portfolio_id for item in fixture.portfolios]
    scenario_order = [item.scenario_id for item in fixture.bundle.scenarios]
    job_order = [
        f"matrix_{portfolio_id}_{scenario_id}"
        for portfolio_id in portfolio_order
        for scenario_id in scenario_order
    ]
    role_counts = Counter(item.role for item in members.values())
    expected_role_counts = Counter(
        {
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
            "workflow_snapshot": len(evidence_index["workflow_snapshot"]),
            "typed_engine_manifest": 1,
            "report_packager_snapshot": 1,
            "report_claim_safety_snapshot": 1,
            "evidence_index": 1,
            "raw_direct_evidence_manifest": 1,
            "raw_ui_projection": 1,
            "matrix_completion_record": 1536,
            "map_completion_record": 60,
            "matrix_request": 1536,
            "map_request": 60,
            "matrix_result": 1536,
            "map_result": 60,
            "matrix_matrix": 1536,
            "map_matrix": 60,
            "matrix_stdout": 1536,
            "map_stdout": 60,
            "matrix_stderr": 1536,
            "map_stderr": 60,
            "matrix_materialized_input": 1536 * 3,
            "map_materialized_input": 60 * 3,
            "footprint_proof_records": 1596,
            "semantic_raster_diff": 1596,
            "circuitscape_effective_configuration": 1596,
            "map_current_raster": 60,
        }
    )
    if role_counts != expected_role_counts:
        raise PackagingError(
            "archive semantic role coverage differs from the release contract: "
            f"observed={dict(role_counts)} expected={dict(expected_role_counts)}"
        )
    bundle_index = {
        "schema_version": BUNDLE_SCHEMA,
        "fixture_version": fixture.fixture_version,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "physics_model_sha256": fixture.physics_model_sha256,
        "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
        "cost_basis_sha256": fixture.cost_basis_sha256,
        "assumption_ledger_sha256": fixture.assumption_ledger_sha256,
        "engine_manifest_sha256": evidence_index["engine_manifest_sha256"],
        "engine_environment_sha256": evidence_index["engine_environment_sha256"],
        "workflow_manifest_sha256": evidence_index["workflow_manifest_sha256"],
        "evidence_index_sha256": evidence_sha,
        "claim_safety_sha256": claim_safety_sha,
        "matrix_job_count": len(matrix_jobs),
        "map_job_count": len(map_jobs),
        "resource_observability": package_resource_observability,
        "process_tree_exit_evidence": package_process_tree_exits,
        "map_matrix_coherence": map_matrix_coherence,
        "required_coverage": {
            "reviewed_raw_asset_sources": 16,
            "normalized_resistance_rasters": 4,
            "normalized_focal_rasters": 3,
            "normalized_footprint_masks": 8,
            "normalized_review_masks": 1,
            "model_decision_snapshots": 1,
            "cost_basis_ledgers": 1,
            "assumption_ledgers": 1,
            "typed_engine_manifests": 1,
            "workflow_snapshots": len(evidence_index["workflow_snapshot"]),
            "report_packager_snapshots": 1,
            "report_claim_safety_snapshots": 1,
            "raw_direct_evidence_manifests": 1,
            "raw_ui_projections": 1,
            "matrix_completion_records": 1536,
            "map_completion_records": 60,
            "footprint_proof_documents": 1596,
            "semantic_raster_diffs": 1596,
            "circuitscape_effective_configurations": 1596,
            "raw_effective_resistance_matrices": 1596,
            "retained_current_rasters": 60,
            "validated_resource_observations": 1596,
            "validated_process_tree_exits": 1596,
            "all_jobs_exact_and_qa_accepted": True,
        },
        "search_trace": {
            "method": "FIXED_128_PORTFOLIO_DIRECT_STUDY",
            "portfolio_order": portfolio_order,
            "scenario_order": scenario_order,
            "job_order_sha256": canonical_sha256(job_order),
            "job_count": len(job_order),
        },
        "packager_sha256": packager_sha,
        "role_counts": dict(sorted(role_counts.items())),
        "files": [
            {
                "path": member.path,
                "sha256": member.sha256,
                "byte_size": member.byte_size,
                "role": member.role,
            }
            for member in (members[path] for path in sorted(members))
        ],
    }
    publish_directory.mkdir(parents=True, exist_ok=True)
    archive, archive_sha, archive_size = write_archive(
        output_directory=publish_directory,
        bundle_index=bundle_index,
        members=members,
    )
    internal_index_sha = hashlib.sha256(canonical_json_bytes(bundle_index)).hexdigest()
    direct_manifest.pop("ui_projection_sha256", None)
    direct_manifest["release_replay_eligible"] = True
    direct_manifest.pop("release_blocker", None)
    direct_manifest["teaching_selection"]["release_eligible"] = True
    direct_manifest["teaching_selection"]["observed_map_review_flags"] = role_flags
    carry_note = (
        "Passes all configured modeled connectivity rules across the 12 reviewed scenarios "
        "and is within the synthetic screening cost cap; a configured road-mask diagnostic "
        "review flag must be resolved before ecological, field, and engineering feasibility "
        "review."
        if role_flags.get("carry")
        else (
            "Passes all configured modeled connectivity rules across the 12 reviewed scenarios "
            "and is within the synthetic screening cost cap; candidate to carry forward for "
            "ecological, field, and engineering feasibility review."
        )
    )
    # The retained producer snapshot remains immutable. Public release metadata
    # deliberately replaces its narrative notes with bounded, model-specific copy.
    direct_manifest["teaching_selection"]["selection_notes"] = {
        "low_cost_caution": (
            "Direct modeled result with a configured road-mask diagnostic review flag; no "
            "animal-use or road-safety inference."
        ),
        "carry_forward": carry_note,
        "over_budget": (
            "Passes all configured modeled connectivity rules across the 12 reviewed scenarios "
            "but exceeds the synthetic screening cost cap."
        ),
        "hypothesis_collapse": (
            "Has a strong observed target-resistance-reduction rank under one reviewed "
            "resistance hypothesis and a materially lower rank under another; no animal-use "
            "inference."
        ),
    }
    try:
        assert_safe_rendered_ecological_copy(
            direct_manifest["teaching_selection"]["selection_notes"]
        )
    except ValueError as exc:
        raise PackagingError("public teaching-selection copy violates claim policy") from exc
    direct_manifest["projection_contract"] = "RidgewayFixture/ecocorridor-replay/v1.2"
    # The ledgers are already retained in the hash-indexed evidence archive.
    # Pin the same reviewed identities at the public-manifest boundary so a
    # replay can resolve its assumptions and costs before unpacking the bundle.
    pin_public_ledger_identities(
        direct_manifest,
        assumption_ledger_sha256=fixture.assumption_ledger_sha256,
        cost_basis_sha256=fixture.cost_basis_sha256,
    )
    direct_manifest["claim_safety_sha256"] = claim_safety_sha
    direct_manifest["resource_observability"] = package_resource_observability
    direct_manifest["process_tree_exit_evidence"] = package_process_tree_exits
    direct_manifest["map_matrix_coherence"] = map_matrix_coherence
    direct_manifest["evidence_bundle"] = {
        "filename": archive.name,
        "format": "tar+gzip",
        "sha256": archive_sha,
        "byte_size": archive_size,
        "file_count": len(members) + 1,
        "internal_index_path": "bundle-index.json",
        "internal_index_sha256": internal_index_sha,
        "packager_sha256": packager_sha,
        "claim_safety_sha256": claim_safety_sha,
    }
    manifest_payload = canonical_json_bytes(direct_manifest)
    manifest_path = publish_directory / "direct-evidence-manifest.json"
    atomic_bytes(manifest_path, manifest_payload)
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    projection = build_ui_projection(
        evidence_root=evidence_root,
        evidence_index=evidence_index,
        direct_manifest=direct_manifest,
        manifest_sha256=manifest_sha,
        raw_projection=raw_projection,
    )
    try:
        assert_safe_rendered_ecological_copy(projection)
    except ValueError as exc:
        raise PackagingError("public UI projection violates ecological claim policy") from exc
    projection_payload = canonical_json_bytes(projection)
    projection_path = publish_directory / "ui_projection.json"
    atomic_bytes(projection_path, projection_payload)
    atomic_bytes(desktop_fixture, projection_payload)
    trust_path = desktop_fixture.with_name("ridgewayReleaseTrust.json")
    trust_payload = canonical_json_bytes(
        {
            "schemaVersion": "ecocorridor-release-trust/v1.0",
            "fixtureId": fixture.bundle.project_id,
            "trusted": True,
            "evidenceManifestSha256": manifest_sha,
            "releaseBlocker": "",
        }
    )
    atomic_bytes(trust_path, trust_payload)
    if (
        sha256_file(manifest_path) != manifest_sha
        or sha256_file(projection_path) != hashlib.sha256(projection_payload).hexdigest()
        or sha256_file(desktop_fixture) != hashlib.sha256(projection_payload).hexdigest()
        or sha256_file(trust_path) != hashlib.sha256(trust_payload).hexdigest()
    ):
        raise PackagingError("published Ridgeway resources changed during atomic commit")
    if (
        json.loads(projection_path.read_text(encoding="utf-8")).get("manifestSha256")
        != manifest_sha
    ):
        raise PackagingError("published projection does not bind the verified manifest")
    for stale in sorted(publish_directory.glob("ridgeway-direct-evidence-*.tar.gz")):
        if stale != archive:
            stale.unlink()
    directory_descriptor = os.open(publish_directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return {
        "archive": archive.as_posix(),
        "archive_sha256": archive_sha,
        "archive_byte_size": archive_size,
        "archive_file_count": len(members) + 1,
        "direct_evidence_manifest": manifest_path.as_posix(),
        "direct_evidence_manifest_sha256": manifest_sha,
        "ui_projection": projection_path.as_posix(),
        "ui_projection_sha256": hashlib.sha256(projection_payload).hexdigest(),
        "desktop_fixture": desktop_fixture.as_posix(),
        "release_trust": trust_path.as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--publish-directory", required=True, type=Path)
    parser.add_argument("--desktop-fixture", required=True, type=Path)
    arguments = parser.parse_args()
    result = package(
        arguments.evidence_root,
        arguments.publish_directory,
        arguments.desktop_fixture,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
