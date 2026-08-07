"""Resumable direct execution for the CC0 usefulness-landscape program.

This module turns the input-only run plan into retained direct Circuitscape
evidence.  It does not select teaching cases or promote a usefulness claim.
Only jobs whose request, inputs, effective configuration, protocol result, raw
matrix, current raster, process exit, and resource observation all validate are
written to immutable completion records.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, Field, model_validator
from scenario_platform.process_lock import FileLock, FileLockTimeout

from .contracts import (
    ComputationalResourceObservation,
    ContractModel,
    EngineManifest,
    ResourceObservationBoundary,
    Sha256Digest,
    canonical_sha256,
)
from .engine import (
    EngineManifestGateError,
    EngineProtocolError,
    EngineProtocolResult,
    PreparedPair,
    ProtocolPairMetric,
    contained_regular_file,
    parse_circuitscape_matrix,
    sha256_file,
    validate_effective_circuitscape_configuration,
)
from .engine_manifest_gate import verify_circuitscape_manifest
from .engine_provenance import (
    EngineProvenanceError,
    EngineProvenanceVerifier,
    GithubAttestedEngineProvenanceVerifier,
    VerifiedEngineProvenance,
)
from .raster_io import RasterIOError, current_map_values_and_nodata
from .resource_observability import (
    ObservedProcessTimeout,
    run_observed_process,
    unmeasured_cache_observation,
)
from .usefulness_landscapes import (
    DirectRunPlanRow,
    UsefulnessCatalog,
    UsefulnessLandscape,
    UsefulnessMaterializationManifest,
    build_usefulness_catalog,
    materialize_usefulness_landscapes,
    usefulness_program_sha256,
    usefulness_public_manifest,
    usefulness_run_plan_rows,
    usefulness_run_plan_sha256,
)

DIRECT_EVIDENCE_SCHEMA = "ecocorridor-usefulness-direct-evidence/v1.1"
EVIDENCE_INDEX_SCHEMA = "ecocorridor-usefulness-evidence-index/v1.1"
COMPLETION_SCHEMA = "ecocorridor-usefulness-job-completion/v1.1"
MAX_WORKERS = 8
MAX_ENGINE_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
REQUIRED_ARTIFACT_ROLES = frozenset(
    {
        "request",
        "result",
        "matrix",
        "current_raster",
        "effective_configuration",
        "stdout",
        "stderr",
        "input_resistance",
        "input_focal",
        "input_pairs",
        "input_review_mask",
    }
)


class UsefulnessDirectStudyError(RuntimeError):
    """Fail-closed direct-study error."""


class RetainedDirectArtifact(ContractModel):
    role: str
    relative_path: str
    sha256: Sha256Digest
    byte_size: int = Field(ge=0)

    @model_validator(mode="after")
    def safe_relative_path(self) -> RetainedDirectArtifact:
        _require_safe_relative(self.relative_path)
        return self


class MatrixCurrentCoherenceQA(ContractModel):
    matrix_pair_count: int = Field(gt=0)
    matrix_min_effective_resistance: float = Field(gt=0)
    matrix_max_effective_resistance: float = Field(gt=0)
    current_valid_cell_count: int = Field(gt=0)
    current_positive_cell_count: int = Field(gt=0)
    source_nodata_cell_count: int = Field(ge=0)
    current_nodata_cell_count: int = Field(ge=0)
    source_nodata_zero_cell_count: int = Field(ge=0)
    nodata_encoding: Literal[
        "MATCHED_NODATA",
        "ZERO_OUTSIDE_RETAINED_SOURCE_MASK",
    ]
    current_sum: float = Field(gt=0)
    current_max: float = Field(gt=0)
    checks: tuple[
        Literal[
            "POSITIVE_FINITE_MATRIX",
            "NONZERO_FINITE_CURRENT_SUPPORT",
            "SOURCE_MASK_SEMANTICS_PRESERVED",
            "SHARED_DIRECT_RUN_ARTIFACT_IDENTITY",
        ],
        ...,
    ]
    qa_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_quantitative_qa(self) -> MatrixCurrentCoherenceQA:
        if self.matrix_min_effective_resistance > self.matrix_max_effective_resistance:
            raise ValueError("matrix resistance range is inverted")
        if self.current_positive_cell_count > self.current_valid_cell_count:
            raise ValueError("positive current support exceeds valid raster cells")
        if self.nodata_encoding == "MATCHED_NODATA":
            if self.current_nodata_cell_count != self.source_nodata_cell_count:
                raise ValueError("matched NODATA encoding differs from the source mask")
            if self.source_nodata_zero_cell_count:
                raise ValueError("matched NODATA encoding cannot contain zero-mask cells")
        elif (
            self.source_nodata_zero_cell_count != self.source_nodata_cell_count
            or self.current_nodata_cell_count
        ):
            raise ValueError("zero-outside-mask encoding differs from the source mask")
        required = {
            "POSITIVE_FINITE_MATRIX",
            "NONZERO_FINITE_CURRENT_SUPPORT",
            "SOURCE_MASK_SEMANTICS_PRESERVED",
            "SHARED_DIRECT_RUN_ARTIFACT_IDENTITY",
        }
        if set(self.checks) != required or len(self.checks) != len(required):
            raise ValueError("matrix/current coherence checks are incomplete")
        payload = self.model_dump(mode="json", exclude={"qa_sha256"})
        if self.qa_sha256 != canonical_sha256(payload):
            raise ValueError("matrix/current coherence QA digest differs")
        return self


class UsefulnessDirectJobEvidence(ContractModel):
    direct_run_id: str
    family: str
    portfolio_id: str
    scenario_id: str
    safeguard: bool
    state: Literal["SUCCEEDED"] = "SUCCEEDED"
    exact: Literal[True] = True
    qa_status: Literal["ACCEPTED"] = "ACCEPTED"
    qa_findings: tuple[()] = ()
    engine_name: Literal["Circuitscape"] = "Circuitscape"
    engine_version: Literal["5.17.1"] = "5.17.1"
    engine_executable_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    engine_trust_profile: Literal[
        "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "TEST_ONLY_UNPUBLISHABLE",
    ]
    engine_provenance_receipt_sha256: Sha256Digest | None = None
    program_sha256: Sha256Digest
    run_plan_sha256: Sha256Digest
    model_run_cache_key: Sha256Digest
    request_sha256: Sha256Digest
    result_sha256: Sha256Digest
    matrix_sha256: Sha256Digest
    current_map_sha256: Sha256Digest
    effective_configuration_sha256: Sha256Digest
    input_sha256: dict[str, Sha256Digest]
    pair_metrics: tuple[ProtocolPairMetric, ...]
    matrix_current_coherence: MatrixCurrentCoherenceQA
    artifacts: tuple[RetainedDirectArtifact, ...]
    artifact_directory: str
    resource_observation: ComputationalResourceObservation
    process_exit_evidence: dict[str, Any]
    producer_validator_sha256: Sha256Digest
    completed_at: AwareDatetime
    cached: bool = False
    cache_lookup_observation: ComputationalResourceObservation | None = None

    @model_validator(mode="after")
    def complete_accepted_job(self) -> UsefulnessDirectJobEvidence:
        _require_safe_relative(self.artifact_directory)
        roles = [artifact.role for artifact in self.artifacts]
        if not REQUIRED_ARTIFACT_ROLES.issubset(roles):
            raise ValueError("accepted direct job omits a required retained artifact")
        if len(roles) != len(set(roles)):
            raise ValueError("accepted direct job has duplicate artifact roles")
        if not self.pair_metrics:
            raise ValueError("accepted direct job has no pair metrics")
        if self.resource_observation.boundary != ResourceObservationBoundary.DIRECT_SOLVER:
            raise ValueError("accepted direct job requires a direct-solver resource observation")
        if self.process_exit_evidence.get("tree_exit_confirmed") is not True:
            raise ValueError("accepted direct job requires confirmed isolated process-tree exit")
        if self.cached and self.cache_lookup_observation is None:
            raise ValueError("cache reuse requires a separate cache lookup observation")
        if not self.cached and self.cache_lookup_observation is not None:
            raise ValueError("fresh direct execution cannot claim a cache lookup observation")
        if (
            self.engine_trust_profile == "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        ) != (self.engine_provenance_receipt_sha256 is not None):
            raise ValueError("direct job engine trust and provenance receipt disagree")
        return self


class UsefulnessDirectStudyResult(ContractModel):
    state: Literal[
        "INCOMPLETE",
        "QA_ACCEPTED_PACKAGE_BLOCKED_DIRTY_SOURCE",
        "COMPLETED_QA_ACCEPTED",
        "COMPLETED_TEST_ONLY_UNPUBLISHABLE",
    ]
    planned_jobs: Literal[160] = 160
    completed_jobs: int = Field(ge=0, le=160)
    cached_jobs: int = Field(ge=0, le=160)
    engine_executable_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    engine_trust_profile: Literal[
        "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "TEST_ONLY_UNPUBLISHABLE",
    ]
    engine_provenance_receipt_sha256: Sha256Digest | None = None
    program_sha256: Sha256Digest
    run_plan_sha256: Sha256Digest
    evidence_index_sha256: Sha256Digest | None = None
    evidence_manifest_path: Path | None = None
    evidence_archive_path: Path | None = None
    outcome_promotion: Literal["BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"] = (
        "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"
    )

    @model_validator(mode="after")
    def validate_engine_trust(self) -> UsefulnessDirectStudyResult:
        production = self.engine_trust_profile == (
            "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        )
        if production != (self.engine_provenance_receipt_sha256 is not None):
            raise ValueError("direct-study result engine trust is inconsistent")
        if self.state == "COMPLETED_TEST_ONLY_UNPUBLISHABLE" and production:
            raise ValueError("test-only completion cannot claim production provenance")
        if self.state == "COMPLETED_QA_ACCEPTED" and not production:
            raise ValueError("publishable completion requires production provenance")
        return self


class UsefulnessEvidencePackageReceipt(ContractModel):
    manifest_path: Path
    archive_path: Path
    manifest_sha256: Sha256Digest
    archive_sha256: Sha256Digest
    evidence_index_sha256: Sha256Digest
    job_count: Literal[160] = 160
    complete_target_and_safeguard_ensembles: Literal[True] = True
    qa_accepted_jobs: Literal[160] = 160
    direct_evidence_complete: Literal[True] = True
    engine_manifest_sha256: Sha256Digest
    engine_trust_profile: Literal["PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"] = (
        "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
    )
    engine_provenance_receipt_sha256: Sha256Digest
    engine_attestation_verification_sha256: Sha256Digest
    engine_provenance_path: Path = Field(exclude=True, repr=False)
    engine_attestation_bundle_path: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    source_repository: str
    source_commit: str
    source_tree_clean: Literal[True] = True
    outcome_promotion: Literal["BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"] = (
        "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"
    )


class SourceRepositoryIdentity(ContractModel):
    """Credential-free producer identity for a release evidence package."""

    source_repository: str
    source_commit: str
    source_tree_clean: bool

    @model_validator(mode="after")
    def valid_git_identity(self) -> SourceRepositoryIdentity:
        if (
            len(self.source_commit) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in self.source_commit)
        ):
            raise ValueError("source commit must be a lowercase Git object ID")
        try:
            normalized_repository = _safe_repository_url(self.source_repository)
        except UsefulnessDirectStudyError as exc:
            raise ValueError("source repository must be a credential-free HTTPS URL") from exc
        if normalized_repository != self.source_repository:
            raise ValueError("source repository must use its canonical credential-free URL")
        return self


def _require_digest(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise UsefulnessDirectStudyError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe relative evidence path: {value}")
    return path


def _canonical_json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise UsefulnessDirectStudyError(f"immutable evidence record differs: {path}")
        return
    _atomic_bytes(path, payload)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_repository_url(value: str) -> str:
    value = value.strip()
    if value.startswith("git@") and ":" in value:
        authority, path = value[4:].split(":", 1)
        value = f"https://{authority}/{path}"
    elif value.startswith("ssh://git@"):
        parsed_ssh = urlsplit(value)
        value = urlunsplit(("https", parsed_ssh.hostname or "", parsed_ssh.path, "", ""))
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise UsefulnessDirectStudyError(
            "source repository remote must be a credential-free HTTPS or Git SSH URL"
        )
    path = parsed.path.removesuffix(".git")
    if not path or path == "/" or any(part in {"", ".", ".."} for part in path.split("/")[1:]):
        raise UsefulnessDirectStudyError("source repository remote path is invalid")
    return urlunsplit(("https", parsed.hostname.lower(), path, "", ""))


def _git_output(root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise UsefulnessDirectStudyError("Git is required to bind release evidence to source")
    try:
        completed = subprocess.run(
            [executable, "-C", os.fspath(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsefulnessDirectStudyError("unable to resolve release source identity") from exc
    return completed.stdout.strip()


def _source_repository_identity() -> SourceRepositoryIdentity:
    root = _repository_root()
    top_level = Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != root.resolve(strict=True):
        raise UsefulnessDirectStudyError("source files are not rooted in the expected repository")
    commit = _git_output(root, "rev-parse", "HEAD")
    repository = _safe_repository_url(_git_output(root, "remote", "get-url", "origin"))
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    return SourceRepositoryIdentity(
        source_repository=repository,
        source_commit=commit,
        source_tree_clean=not bool(status),
    )


def _validator_sha256() -> str:
    return sha256_file(Path(__file__))


def _current_engine_source_paths() -> dict[str, Path]:
    root = _repository_root()
    return {
        "julia_manifest_sha256": root / "julia/EcoCorridorEngine/Manifest.toml",
        "wrapper_sha256": root / "julia/EcoCorridorEngine/src/EcoCorridorEngine.jl",
        "parser_sha256": root / "python/ecocorridor_core/engine.py",
        "metric_definitions_sha256": root / "python/ecocorridor_core/metrics.py",
    }


def validate_usefulness_engine_manifest(
    *,
    manifest_path: Path,
    expected_engine_sha256: str,
    executable_path: Path | None = None,
) -> tuple[Path, EngineManifest, str]:
    """Validate a typed engine manifest against the executable and current sources."""

    expected_engine_sha256 = _require_digest(
        expected_engine_sha256, "engine executable identity"
    )
    supplied = manifest_path.expanduser()
    try:
        metadata = os.lstat(supplied)
        resolved = supplied.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UsefulnessDirectStudyError("packaged engine manifest is absent") from exc
    if supplied.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise UsefulnessDirectStudyError(
            "packaged engine manifest must be a regular non-symlink file"
        )
    if metadata.st_size > MAX_ENGINE_MANIFEST_BYTES:
        raise UsefulnessDirectStudyError("packaged engine manifest exceeds one MiB")
    try:
        manifest = EngineManifest.model_validate_json(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsefulnessDirectStudyError("packaged engine manifest is invalid") from exc
    if manifest.executable_sha256 != expected_engine_sha256:
        raise UsefulnessDirectStudyError(
            "packaged engine manifest does not bind the selected executable"
        )
    if executable_path is not None and sha256_file(executable_path) != manifest.executable_sha256:
        raise UsefulnessDirectStudyError(
            "selected executable differs from the typed engine manifest"
        )
    if manifest.neighbor_rule.value != "EIGHT" or manifest.resistance_average.value != "ARITHMETIC":
        raise UsefulnessDirectStudyError(
            "usefulness study requires an EIGHT/ARITHMETIC engine manifest"
        )
    source_paths = _current_engine_source_paths()
    try:
        verify_circuitscape_manifest(source_paths["julia_manifest_sha256"])
    except EngineManifestGateError as exc:
        raise UsefulnessDirectStudyError(
            "current Julia manifest fails the Circuitscape provenance gate"
        ) from exc
    stale = [
        field
        for field, source in source_paths.items()
        if not source.is_file() or source.is_symlink() or sha256_file(source) != getattr(manifest, field)
    ]
    if stale:
        raise UsefulnessDirectStudyError(
            "packaged engine manifest is stale relative to current reviewed sources: "
            + ", ".join(sorted(stale))
        )
    return resolved, manifest, sha256_file(resolved)


def validate_production_engine_provenance(
    *,
    provenance_path: Path | None,
    attestation_bundle_path: Path | None,
    expected_engine_sha256: str,
    engine_manifest: EngineManifest,
    engine_manifest_file_sha256: str,
    source_identity: SourceRepositoryIdentity | None = None,
    verifier: EngineProvenanceVerifier | None = None,
) -> VerifiedEngineProvenance:
    """Require independently attested protected-build provenance.

    A typed ``EngineManifest`` alone is deliberately insufficient: a fake
    executable can truthfully hash itself into a self-consistent manifest.
    """

    if provenance_path is None:
        raise UsefulnessDirectStudyError(
            "production direct evidence requires a protected-build engine provenance receipt; "
            "use test_only_engine=True only for categorically non-publishable tests"
        )
    selected = verifier or GithubAttestedEngineProvenanceVerifier()
    try:
        verified = selected.verify(
            receipt_path=provenance_path,
            attestation_bundle_path=attestation_bundle_path,
            expected_engine_sha256=expected_engine_sha256,
            expected_engine_manifest_sha256=engine_manifest.sha256,
            expected_engine_manifest_file_sha256=engine_manifest_file_sha256,
            expected_source_repository=(
                source_identity.source_repository if source_identity else None
            ),
            expected_source_commit=source_identity.source_commit if source_identity else None,
        )
    except EngineProvenanceError as exc:
        raise UsefulnessDirectStudyError(
            "production engine provenance failed independent verification"
        ) from exc
    if (
        verified.receipt.test_only
        or not verified.receipt.publishable
        or verified.receipt.trust_profile
        != "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        or not verified.attestation.verified
    ):
        raise UsefulnessDirectStudyError(
            "engine provenance is not authorized for production evidence"
        )
    return verified


def _regular_executable(path: Path, expected_sha256: str) -> Path:
    expected_sha256 = _require_digest(expected_sha256, "engine executable identity")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise UsefulnessDirectStudyError("packaged engine executable is absent") from exc
    if path.is_symlink() or not resolved.is_file() or resolved.is_symlink():
        raise UsefulnessDirectStudyError("packaged engine must be a regular non-symlink file")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise UsefulnessDirectStudyError("packaged engine is not executable")
    if sha256_file(resolved) != expected_sha256:
        raise UsefulnessDirectStudyError("packaged engine hash differs from the approved identity")
    return resolved


def _assert_separate_roots(
    materialized: Path,
    output: Path,
    engine: Path,
    engine_manifest: Path,
) -> None:
    materialized = materialized.resolve(strict=True)
    output = output.resolve()
    if output == materialized or materialized in output.parents or output in materialized.parents:
        raise UsefulnessDirectStudyError(
            "direct evidence output must be separate from immutable materialized inputs"
        )
    if output == engine or output in engine.parents:
        raise UsefulnessDirectStudyError("direct evidence output cannot contain the engine")
    if output == engine_manifest or output in engine_manifest.parents:
        raise UsefulnessDirectStudyError(
            "direct evidence output cannot contain the engine manifest"
        )


def verify_materialized_usefulness_inputs(
    materialized_root: Path,
    *,
    catalog: UsefulnessCatalog | None = None,
) -> tuple[UsefulnessMaterializationManifest, tuple[DirectRunPlanRow, ...], str]:
    """Verify the complete input materialization before any subprocess starts."""

    catalog = catalog or build_usefulness_catalog()
    root = materialized_root.expanduser().resolve(strict=True)
    manifest_path = contained_regular_file(root, "materialization-manifest.json")
    try:
        manifest = UsefulnessMaterializationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsefulnessDirectStudyError("materialization manifest is invalid") from exc
    if manifest.program_sha256 != usefulness_program_sha256(catalog):
        raise UsefulnessDirectStudyError("materialized inputs bind a different program")
    if manifest.run_plan_sha256 != usefulness_run_plan_sha256(catalog):
        raise UsefulnessDirectStudyError("materialized inputs bind a different run plan")
    if manifest.materializer_sha256 != sha256_file(
        Path(materialize_usefulness_landscapes.__code__.co_filename)
    ):
        raise UsefulnessDirectStudyError("materialized inputs bind a different generator source")
    if manifest.direct_evidence_state != "PENDING_DIRECT_CIRCUITSCAPE":
        raise UsefulnessDirectStudyError("input materialization already claims direct evidence")
    if manifest.result_file_count != 0 or manifest.release_eligible:
        raise UsefulnessDirectStudyError("input materialization contains a result claim")
    declared: set[str] = set()
    for record in manifest.files:
        if record.relative_path in declared:
            raise UsefulnessDirectStudyError("materialization manifest has duplicate paths")
        declared.add(record.relative_path)
        try:
            path = contained_regular_file(root, record.relative_path)
        except EngineProtocolError as exc:
            raise UsefulnessDirectStudyError("materialized input is absent or unsafe") from exc
        if path.stat().st_size != record.byte_size or sha256_file(path) != record.sha256:
            raise UsefulnessDirectStudyError("materialized input differs from its checksum")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected = {"materialization-manifest.json", *declared}
    if actual != expected:
        raise UsefulnessDirectStudyError("materialized input directory has undeclared files")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise UsefulnessDirectStudyError("materialized input directory contains a symlink")
    rows = usefulness_run_plan_rows(catalog)
    if len(rows) != manifest.direct_job_count or len(rows) != 160:
        raise UsefulnessDirectStudyError("materialized direct-job count is incomplete")
    for row in rows:
        request = contained_regular_file(root, row.request_relative_path)
        if sha256_file(request) != row.request_sha256:
            raise UsefulnessDirectStudyError("planned request differs from materialized bytes")
    return manifest, rows, sha256_file(manifest_path)


def _landscape_maps(
    catalog: UsefulnessCatalog,
) -> tuple[dict[str, UsefulnessLandscape], dict[tuple[str, str], Any]]:
    landscapes = {landscape.family: landscape for landscape in catalog.landscapes}
    scenarios = {
        (landscape.family, scenario.scenario_id): scenario
        for landscape in catalog.landscapes
        for scenario in landscape.scenarios
    }
    return landscapes, scenarios


def _model_run_cache_key(
    row: DirectRunPlanRow,
    *,
    program_sha256: str,
    engine_sha256: str,
    engine_manifest_sha256: str,
    engine_trust_profile: str,
    engine_provenance_receipt_sha256: str | None,
    pair_input_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "program_sha256": program_sha256,
            "engine_executable_sha256": engine_sha256,
            "engine_manifest_sha256": engine_manifest_sha256,
            "engine_trust_profile": engine_trust_profile,
            "engine_provenance_receipt_sha256": engine_provenance_receipt_sha256,
            "request_sha256": row.request_sha256,
            "prepared_resistance_sha256": row.prepared_resistance_sha256,
            "focal_raster_sha256": row.focal_raster_sha256,
            "review_mask_sha256": row.review_mask_sha256,
            "included_pairs_sha256": pair_input_sha256,
        }
    )


def _attempt_directory(job_root: Path) -> Path:
    attempts = job_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    indexes = [
        int(path.name.rsplit("-", 1)[-1])
        for path in attempts.iterdir()
        if path.is_dir()
        and path.name.startswith("attempt-")
        and path.name.rsplit("-", 1)[-1].isdigit()
    ]
    attempt = attempts / f"attempt-{max(indexes, default=0) + 1:04d}"
    attempt.mkdir()
    (attempt / "inputs").mkdir()
    return attempt


def _copy_verified(source: Path, target: Path, expected_sha256: str) -> None:
    if source.is_symlink() or not source.is_file() or sha256_file(source) != expected_sha256:
        raise UsefulnessDirectStudyError("source input changed before direct execution")
    shutil.copyfile(source, target)
    if sha256_file(target) != expected_sha256:
        raise UsefulnessDirectStudyError("copied direct input differs from its source")


def _role_for_path(
    relative_to_attempt: str,
    *,
    matrix_relative: str,
    current_relative: str,
) -> str:
    fixed = {
        "request.json": "request",
        "result.json": "result",
        "circuitscape.ini": "effective_configuration",
        "stdout.log": "stdout",
        "stderr.log": "stderr",
        "inputs/resistance.asc": "input_resistance",
        "inputs/focal.asc": "input_focal",
        "inputs/included_pairs.tsv": "input_pairs",
        "inputs/review-mask.asc": "input_review_mask",
    }
    if relative_to_attempt == matrix_relative:
        return "matrix"
    if relative_to_attempt == current_relative:
        return "current_raster"
    return fixed.get(relative_to_attempt, f"engine_auxiliary:{relative_to_attempt}")


def _current_raster_is_valid(
    current_path: Path,
    landscape: UsefulnessLandscape,
    scenario: Any,
) -> tuple[int, int, float, float, int, int, int, str]:
    expected = landscape.rasters[scenario.resistance_asset_id]
    try:
        values, nodata = current_map_values_and_nodata(current_path, expected_grid=expected.grid)
    except RasterIOError as exc:
        raise UsefulnessDirectStudyError("current raster is invalid") from exc
    valid_count = 0
    positive_count = 0
    source_nodata_count = 0
    current_nodata_count = 0
    source_nodata_zero_count = 0
    current_values: list[float] = []
    for row in range(expected.grid.height):
        for column in range(expected.grid.width):
            source_value = expected.values[row][column]
            source_missing = source_value is None or source_value == expected.grid.nodata
            current_value = values[row][column]
            current_missing = (
                math.isnan(nodata) and math.isnan(current_value)
            ) or math.isclose(current_value, nodata, rel_tol=0.0, abs_tol=1e-9)
            if source_missing:
                source_nodata_count += 1
                if current_missing:
                    current_nodata_count += 1
                elif math.isfinite(current_value) and math.isclose(
                    current_value, 0.0, rel_tol=0.0, abs_tol=1e-12
                ):
                    source_nodata_zero_count += 1
                else:
                    raise UsefulnessDirectStudyError(
                        "current raster carries nonzero or nonfinite current outside the retained source mask"
                    )
                continue
            if current_missing:
                raise UsefulnessDirectStudyError(
                    "current raster removed a valid cell from the reviewed source mask"
                )
            if not current_missing:
                if not math.isfinite(current_value) or current_value < 0:
                    raise UsefulnessDirectStudyError(
                        "current raster contains negative or nonfinite data"
                    )
                valid_count += 1
                current_values.append(current_value)
                if current_value > 0:
                    positive_count += 1
    if valid_count == 0:
        raise UsefulnessDirectStudyError("current raster contains no valid diagnostic cells")
    current_sum = math.fsum(current_values)
    current_max = max(current_values)
    if positive_count == 0 or current_sum <= 0 or current_max <= 0:
        raise UsefulnessDirectStudyError(
            "positive matrix results require nonzero diagnostic current support"
        )
    if source_nodata_count == current_nodata_count:
        nodata_encoding = "MATCHED_NODATA"
    elif source_nodata_count == source_nodata_zero_count:
        nodata_encoding = "ZERO_OUTSIDE_RETAINED_SOURCE_MASK"
    else:
        raise UsefulnessDirectStudyError(
            "current raster mixes incompatible source-mask encodings"
        )
    return (
        valid_count,
        positive_count,
        current_sum,
        current_max,
        source_nodata_count,
        current_nodata_count,
        source_nodata_zero_count,
        nodata_encoding,
    )


def _matrix_current_coherence(
    pair_metrics: tuple[ProtocolPairMetric, ...],
    current_statistics: tuple[int, int, float, float, int, int, int, str],
) -> MatrixCurrentCoherenceQA:
    resistances = [item.effective_resistance for item in pair_metrics]
    if not resistances or any(not math.isfinite(value) or value <= 0 for value in resistances):
        raise UsefulnessDirectStudyError("matrix contains no positive finite direct result")
    (
        valid_count,
        positive_count,
        current_sum,
        current_max,
        source_nodata_count,
        current_nodata_count,
        source_nodata_zero_count,
        nodata_encoding,
    ) = current_statistics
    provisional = {
        "matrix_pair_count": len(resistances),
        "matrix_min_effective_resistance": min(resistances),
        "matrix_max_effective_resistance": max(resistances),
        "current_valid_cell_count": valid_count,
        "current_positive_cell_count": positive_count,
        "source_nodata_cell_count": source_nodata_count,
        "current_nodata_cell_count": current_nodata_count,
        "source_nodata_zero_cell_count": source_nodata_zero_count,
        "nodata_encoding": nodata_encoding,
        "current_sum": current_sum,
        "current_max": current_max,
        "checks": (
            "POSITIVE_FINITE_MATRIX",
            "NONZERO_FINITE_CURRENT_SUPPORT",
            "SOURCE_MASK_SEMANTICS_PRESERVED",
            "SHARED_DIRECT_RUN_ARTIFACT_IDENTITY",
        ),
    }
    return MatrixCurrentCoherenceQA(
        **provisional,
        qa_sha256=canonical_sha256(provisional),
    )


def _validate_attempt(
    *,
    evidence_root: Path,
    attempt: Path,
    row: DirectRunPlanRow,
    landscape: UsefulnessLandscape,
    scenario: Any,
    expected_engine_sha256: str,
    completed_returncode: int | None,
) -> tuple[
    EngineProtocolResult,
    tuple[ProtocolPairMetric, ...],
    tuple[RetainedDirectArtifact, ...],
    MatrixCurrentCoherenceQA,
]:
    if completed_returncode not in {None, 0}:
        raise UsefulnessDirectStudyError("packaged engine process returned a failure code")
    request_path = contained_regular_file(attempt, "request.json")
    result_path = contained_regular_file(attempt, "result.json")
    resistance_path = contained_regular_file(attempt, "inputs/resistance.asc")
    focal_path = contained_regular_file(attempt, "inputs/focal.asc")
    contained_regular_file(attempt, "inputs/included_pairs.tsv")
    review_mask_path = contained_regular_file(attempt, "inputs/review-mask.asc")
    configuration_path = contained_regular_file(attempt, "circuitscape.ini")
    contained_regular_file(attempt, "stdout.log")
    contained_regular_file(attempt, "stderr.log")
    if sha256_file(request_path) != row.request_sha256:
        raise UsefulnessDirectStudyError("retained request differs from its planned identity")
    if sha256_file(resistance_path) != row.prepared_resistance_sha256:
        raise UsefulnessDirectStudyError("retained prepared resistance input changed")
    if sha256_file(focal_path) != row.focal_raster_sha256:
        raise UsefulnessDirectStudyError("retained focal input changed")
    if sha256_file(review_mask_path) != row.review_mask_sha256:
        raise UsefulnessDirectStudyError("retained review-mask input changed")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("prepared_run_id") != row.direct_run_id:
        raise UsefulnessDirectStudyError("request run identity differs from the plan")
    if request.get("mode") != "raster_pairwise" or request.get("raster_semantics") != "resistance":
        raise UsefulnessDirectStudyError("request leaves the supported resistance pairwise mode")
    if request.get("output_directory") != "outputs":
        raise UsefulnessDirectStudyError("request output directory differs from the reviewed plan")
    settings = request.get("settings")
    if not isinstance(settings, dict) or settings != {
        "precision": "double",
        "neighbor_rule": scenario.neighbor_rule.value,
        "resistance_average": scenario.resistance_average.value,
        "julia_threads": 1,
        "blas_threads": 1,
        "omp_threads": 1,
        "emit_current_map": True,
    }:
        raise UsefulnessDirectStudyError("request engine settings differ from the reviewed plan")
    resources = request.get("resources")
    expected_resources = {
        "resistance": "inputs/resistance.asc",
        "focal": "inputs/focal.asc",
        "included_pairs": "inputs/included_pairs.tsv",
        "review_mask": "inputs/review-mask.asc",
    }
    if resources != expected_resources:
        raise UsefulnessDirectStudyError("request resources differ from the isolated inputs")
    validate_effective_circuitscape_configuration(
        configuration_path,
        resource_paths={key: value for key, value in expected_resources.items() if key != "review_mask"},
        output_directory="outputs",
        neighbor_rule=scenario.neighbor_rule.value,
        resistance_average=scenario.resistance_average.value,
        emit_current_map=True,
    )
    try:
        protocol = EngineProtocolResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsefulnessDirectStudyError("engine result protocol is invalid") from exc
    if protocol.status != "SUCCEEDED" or protocol.warnings or protocol.failure_message:
        raise UsefulnessDirectStudyError("direct job is not a warning-free successful result")
    requested_pairs = tuple(
        PreparedPair.model_validate(pair) for pair in request.get("pairs", [])
    )
    if tuple(pair.pair_id for pair in requested_pairs) != row.pair_ids:
        raise UsefulnessDirectStudyError("request focal-pair set differs from the run plan")
    try:
        matrix_path = contained_regular_file(attempt, protocol.matrix_path or "")
        current_path = contained_regular_file(attempt, protocol.current_map_path or "")
    except EngineProtocolError as exc:
        raise UsefulnessDirectStudyError("engine result artifact is absent or unsafe") from exc
    parsed = tuple(parse_circuitscape_matrix(matrix_path, list(requested_pairs)))
    if [item.model_dump(mode="json") for item in parsed] != [
        item.model_dump(mode="json")
        for item in sorted(protocol.pair_metrics, key=lambda metric: metric.pair_id)
    ]:
        raise UsefulnessDirectStudyError("protocol metrics differ from the raw matrix")
    current_statistics = _current_raster_is_valid(current_path, landscape, scenario)
    coherence = _matrix_current_coherence(parsed, current_statistics)
    matrix_relative = matrix_path.relative_to(attempt).as_posix()
    current_relative = current_path.relative_to(attempt).as_posix()
    artifacts: list[RetainedDirectArtifact] = []
    roles: set[str] = set()
    for path in sorted(item for item in attempt.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise UsefulnessDirectStudyError("direct job produced a symlinked artifact")
        relative_to_attempt = path.relative_to(attempt).as_posix()
        role = _role_for_path(
            relative_to_attempt,
            matrix_relative=matrix_relative,
            current_relative=current_relative,
        )
        if role in roles:
            raise UsefulnessDirectStudyError("direct job produced duplicate semantic artifacts")
        roles.add(role)
        artifacts.append(
            RetainedDirectArtifact(
                role=role,
                relative_path=path.relative_to(evidence_root).as_posix(),
                sha256=sha256_file(path),
                byte_size=path.stat().st_size,
            )
        )
    if not REQUIRED_ARTIFACT_ROLES.issubset(roles):
        raise UsefulnessDirectStudyError("direct job did not retain all required artifacts")
    if expected_engine_sha256 != _require_digest(expected_engine_sha256, "engine identity"):
        raise AssertionError("unreachable engine identity mismatch")
    return protocol, parsed, tuple(artifacts), coherence


def _job_completion_payload(job: UsefulnessDirectJobEvidence) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": COMPLETION_SCHEMA,
        "job": job.model_dump(
            mode="json", exclude={"cached", "cache_lookup_observation"}
        ),
    }
    payload["completion_sha256"] = canonical_sha256(payload)
    return payload


def _validate_completion_digest(payload: dict[str, object]) -> None:
    asserted = payload.get("completion_sha256")
    body = {key: value for key, value in payload.items() if key != "completion_sha256"}
    if asserted != canonical_sha256(body):
        raise UsefulnessDirectStudyError("direct-job completion digest is invalid")


def _artifact_by_role(job: UsefulnessDirectJobEvidence) -> dict[str, RetainedDirectArtifact]:
    return {artifact.role: artifact for artifact in job.artifacts}


def _load_cached_job(
    *,
    evidence_root: Path,
    job_root: Path,
    row: DirectRunPlanRow,
    landscape: UsefulnessLandscape,
    scenario: Any,
    engine_sha256: str,
    engine_manifest_sha256: str,
    engine_trust_profile: str,
    engine_provenance_receipt_sha256: str | None,
    program_sha256: str,
    run_plan_sha256: str,
    model_run_cache_key: str,
) -> UsefulnessDirectJobEvidence | None:
    complete_path = job_root / "complete.json"
    if not complete_path.exists():
        return None
    if complete_path.is_symlink() or not complete_path.is_file():
        raise UsefulnessDirectStudyError("cached completion record is unsafe")
    started = datetime.now(UTC)
    try:
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != COMPLETION_SCHEMA:
            raise ValueError("unexpected completion contract")
        _validate_completion_digest(payload)
        job = UsefulnessDirectJobEvidence.model_validate(payload["job"])
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise UsefulnessDirectStudyError("cached completion record is invalid") from exc
    expected_identity = (
        row.direct_run_id,
        row.family,
        row.portfolio_id,
        row.scenario_id,
        engine_sha256,
        engine_manifest_sha256,
        engine_trust_profile,
        engine_provenance_receipt_sha256,
        program_sha256,
        run_plan_sha256,
        model_run_cache_key,
        row.request_sha256,
        row.prepared_resistance_sha256,
        row.focal_raster_sha256,
        row.review_mask_sha256,
    )
    observed_identity = (
        job.direct_run_id,
        job.family,
        job.portfolio_id,
        job.scenario_id,
        job.engine_executable_sha256,
        job.engine_manifest_sha256,
        job.engine_trust_profile,
        job.engine_provenance_receipt_sha256,
        job.program_sha256,
        job.run_plan_sha256,
        job.model_run_cache_key,
        job.request_sha256,
        job.input_sha256.get("resistance"),
        job.input_sha256.get("focal"),
        job.input_sha256.get("review_mask"),
    )
    if observed_identity != expected_identity:
        raise UsefulnessDirectStudyError("cached completion belongs to a different run identity")
    attempt = evidence_root / job.artifact_directory
    if not attempt.is_dir() or attempt.is_symlink():
        raise UsefulnessDirectStudyError("cached artifact directory is absent or unsafe")
    declared_paths = {artifact.relative_path for artifact in job.artifacts}
    actual_paths = {
        path.relative_to(evidence_root).as_posix()
        for path in attempt.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if declared_paths != actual_paths or any(path.is_symlink() for path in attempt.rglob("*")):
        raise UsefulnessDirectStudyError("cached artifact set changed after completion")
    for artifact in job.artifacts:
        path = contained_regular_file(evidence_root, artifact.relative_path)
        if path.stat().st_size != artifact.byte_size or sha256_file(path) != artifact.sha256:
            raise UsefulnessDirectStudyError("cached artifact differs from its immutable hash")
    _, parsed, artifacts, coherence = _validate_attempt(
        evidence_root=evidence_root,
        attempt=attempt,
        row=row,
        landscape=landscape,
        scenario=scenario,
        expected_engine_sha256=engine_sha256,
        completed_returncode=None,
    )
    if (
        artifacts != job.artifacts
        or parsed != job.pair_metrics
        or coherence != job.matrix_current_coherence
    ):
        raise UsefulnessDirectStudyError("cached direct evidence failed semantic revalidation")
    elapsed = max(0.0, (datetime.now(UTC) - started).total_seconds())
    return job.model_copy(
        update={
            "cached": True,
            "cache_lookup_observation": unmeasured_cache_observation(
                observation_id=f"cache:{row.direct_run_id}", wall_seconds=elapsed
            ),
        }
    )


def _record_failure(
    attempt: Path,
    *,
    kind: str,
    message: str,
    resource_observation: ComputationalResourceObservation | None = None,
    process_exit: dict[str, object] | None = None,
) -> None:
    payload = {
        "schema_version": "ecocorridor-usefulness-job-failure/v1.0",
        "kind": kind,
        "message": message,
        "resource_observation": (
            resource_observation.model_dump(mode="json") if resource_observation else None
        ),
        "process_exit": process_exit,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _atomic_bytes(attempt / "failure.json", _canonical_json_bytes(payload, pretty=True))


def _execute_one_job(
    *,
    materialized_root: Path,
    evidence_root: Path,
    engine_executable: Path,
    engine_sha256: str,
    engine_manifest_path: Path,
    engine_manifest_file_sha256: str,
    engine_manifest_sha256: str,
    engine_trust_profile: Literal[
        "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "TEST_ONLY_UNPUBLISHABLE",
    ],
    engine_provenance_receipt_sha256: str | None,
    catalog: UsefulnessCatalog,
    row: DirectRunPlanRow,
    timeout_seconds: float,
) -> UsefulnessDirectJobEvidence:
    landscapes, scenarios = _landscape_maps(catalog)
    landscape = landscapes[row.family]
    scenario = scenarios[(row.family, row.scenario_id)]
    source_run = materialized_root / Path(row.request_relative_path).parent
    pair_source = contained_regular_file(source_run, "inputs/included_pairs.tsv")
    pair_sha = sha256_file(pair_source)
    program_sha = usefulness_program_sha256(catalog)
    run_plan_sha = usefulness_run_plan_sha256(catalog)
    cache_key = _model_run_cache_key(
        row,
        program_sha256=program_sha,
        engine_sha256=engine_sha256,
        engine_manifest_sha256=engine_manifest_sha256,
        engine_trust_profile=engine_trust_profile,
        engine_provenance_receipt_sha256=engine_provenance_receipt_sha256,
        pair_input_sha256=pair_sha,
    )
    if sha256_file(engine_executable) != engine_sha256:
        raise UsefulnessDirectStudyError("packaged engine changed before job launch")
    if sha256_file(engine_manifest_path) != engine_manifest_file_sha256:
        raise UsefulnessDirectStudyError("packaged engine manifest changed before job launch")
    job_root = evidence_root / "jobs" / row.direct_run_id
    cached = _load_cached_job(
        evidence_root=evidence_root,
        job_root=job_root,
        row=row,
        landscape=landscape,
        scenario=scenario,
        engine_sha256=engine_sha256,
        engine_manifest_sha256=engine_manifest_sha256,
        engine_trust_profile=engine_trust_profile,
        engine_provenance_receipt_sha256=engine_provenance_receipt_sha256,
        program_sha256=program_sha,
        run_plan_sha256=run_plan_sha,
        model_run_cache_key=cache_key,
    )
    if cached is not None:
        return cached
    job_root.mkdir(parents=True, exist_ok=True)
    attempt = _attempt_directory(job_root)
    sources = {
        "request.json": (contained_regular_file(source_run, "request.json"), row.request_sha256),
        "inputs/resistance.asc": (
            contained_regular_file(source_run, "inputs/resistance.asc"),
            row.prepared_resistance_sha256,
        ),
        "inputs/focal.asc": (
            contained_regular_file(source_run, "inputs/focal.asc"),
            row.focal_raster_sha256,
        ),
        "inputs/included_pairs.tsv": (pair_source, pair_sha),
        "inputs/review-mask.asc": (
            contained_regular_file(source_run, "inputs/review-mask.asc"),
            row.review_mask_sha256,
        ),
    }
    for relative, (source, expected) in sources.items():
        target = attempt / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(source, target, expected)
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        {
            "JULIA_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    try:
        observed = run_observed_process(
            [
                str(engine_executable),
                "--request",
                "request.json",
                "--result",
                "result.json",
            ],
            cwd=attempt,
            env=environment,
            timeout_seconds=timeout_seconds,
            observation_id=f"resource:{row.direct_run_id}",
            boundary_description=(
                "One isolated packaged Circuitscape raster-pairwise process, including input "
                "parsing, solve, matrix/current serialization, and protocol result writing."
            ),
        )
    except ObservedProcessTimeout as exc:
        (attempt / "stdout.log").write_bytes(exc.output or b"")
        (attempt / "stderr.log").write_bytes(exc.stderr or b"")
        _record_failure(
            attempt,
            kind="TIMEOUT",
            message="Packaged engine exceeded the direct-job deadline.",
            resource_observation=exc.resource_observation,
            process_exit=exc.process_exit.as_dict(),
        )
        raise UsefulnessDirectStudyError("direct job timed out") from exc
    completed = observed.completed
    (attempt / "stdout.log").write_bytes(completed.stdout)
    (attempt / "stderr.log").write_bytes(completed.stderr)
    if not observed.process_exit.tree_exit_confirmed:
        _record_failure(
            attempt,
            kind="PROCESS_TREE_EXIT_UNCONFIRMED",
            message="Successful direct evidence requires confirmed process-tree exit.",
            resource_observation=observed.observation,
            process_exit=observed.process_exit.as_dict(),
        )
        raise UsefulnessDirectStudyError("direct job process-tree exit is unconfirmed")
    if sha256_file(engine_executable) != engine_sha256:
        raise UsefulnessDirectStudyError("packaged engine changed during job execution")
    if sha256_file(engine_manifest_path) != engine_manifest_file_sha256:
        raise UsefulnessDirectStudyError("packaged engine manifest changed during job execution")
    try:
        protocol, parsed, artifacts, coherence = _validate_attempt(
            evidence_root=evidence_root,
            attempt=attempt,
            row=row,
            landscape=landscape,
            scenario=scenario,
            expected_engine_sha256=engine_sha256,
            completed_returncode=completed.returncode,
        )
    except (EngineProtocolError, OSError, ValueError, UsefulnessDirectStudyError) as exc:
        _record_failure(
            attempt,
            kind="QA_REJECTED",
            message=str(exc),
            resource_observation=observed.observation,
            process_exit=observed.process_exit.as_dict(),
        )
        raise UsefulnessDirectStudyError(f"direct job QA rejected: {row.direct_run_id}") from exc
    by_role = {artifact.role: artifact for artifact in artifacts}
    job = UsefulnessDirectJobEvidence(
        direct_run_id=row.direct_run_id,
        family=row.family,
        portfolio_id=row.portfolio_id,
        scenario_id=row.scenario_id,
        safeguard=row.safeguard,
        engine_executable_sha256=engine_sha256,
        engine_manifest_sha256=engine_manifest_sha256,
        engine_trust_profile=engine_trust_profile,
        engine_provenance_receipt_sha256=engine_provenance_receipt_sha256,
        program_sha256=program_sha,
        run_plan_sha256=run_plan_sha,
        model_run_cache_key=cache_key,
        request_sha256=row.request_sha256,
        result_sha256=by_role["result"].sha256,
        matrix_sha256=by_role["matrix"].sha256,
        current_map_sha256=by_role["current_raster"].sha256,
        effective_configuration_sha256=by_role["effective_configuration"].sha256,
        input_sha256={
            "resistance": row.prepared_resistance_sha256,
            "focal": row.focal_raster_sha256,
            "included_pairs": pair_sha,
            "review_mask": row.review_mask_sha256,
        },
        pair_metrics=parsed,
        matrix_current_coherence=coherence,
        artifacts=artifacts,
        artifact_directory=attempt.relative_to(evidence_root).as_posix(),
        resource_observation=observed.observation,
        process_exit_evidence=observed.process_exit.as_dict(),
        producer_validator_sha256=_validator_sha256(),
        completed_at=datetime.now(UTC),
    )
    complete_path = job_root / "complete.json"
    _write_once(
        complete_path,
        _canonical_json_bytes(_job_completion_payload(job), pretty=True),
    )
    # The validated protocol is deliberately used only to bind the result
    # artifacts above. Outcome analysis is a separate, later gate.
    del protocol
    return job


def verify_complete_usefulness_ensemble(
    jobs: Iterable[UsefulnessDirectJobEvidence],
    *,
    catalog: UsefulnessCatalog | None = None,
) -> tuple[UsefulnessDirectJobEvidence, ...]:
    catalog = catalog or build_usefulness_catalog()
    expected = {row.direct_run_id: row for row in usefulness_run_plan_rows(catalog)}
    ordered = tuple(sorted(jobs, key=lambda job: job.direct_run_id))
    if len(ordered) != len(expected) or {job.direct_run_id for job in ordered} != set(expected):
        raise UsefulnessDirectStudyError("direct evidence does not cover all 160 planned jobs")
    if len({job.direct_run_id for job in ordered}) != len(ordered):
        raise UsefulnessDirectStudyError("direct evidence contains duplicate jobs")
    if len({job.engine_manifest_sha256 for job in ordered}) != 1:
        raise UsefulnessDirectStudyError("direct evidence spans multiple engine manifests")
    if len(
        {
            (job.engine_trust_profile, job.engine_provenance_receipt_sha256)
            for job in ordered
        }
    ) != 1:
        raise UsefulnessDirectStudyError("direct evidence spans multiple engine trust identities")
    by_portfolio: dict[tuple[str, str], set[str]] = {}
    exact_output_by_unchanged_input: dict[tuple[str, ...], str] = {}
    scenario_map = {
        landscape.family: {scenario.scenario_id: scenario for scenario in landscape.scenarios}
        for landscape in catalog.landscapes
    }
    for job in ordered:
        row = expected[job.direct_run_id]
        if (
            job.family,
            job.portfolio_id,
            job.scenario_id,
            job.safeguard,
            job.state,
            job.qa_status,
            job.exact,
        ) != (
            row.family,
            row.portfolio_id,
            row.scenario_id,
            row.safeguard,
            "SUCCEEDED",
            "ACCEPTED",
            True,
        ):
            raise UsefulnessDirectStudyError("direct evidence job identity or QA is inconsistent")
        by_portfolio.setdefault((job.family, job.portfolio_id), set()).add(job.scenario_id)
        unchanged_input = (
            job.engine_manifest_sha256,
            job.scenario_id,
            job.input_sha256["resistance"],
            job.input_sha256["focal"],
            job.input_sha256["included_pairs"],
        )
        exact_output = canonical_sha256(
            {
                "pair_metrics": [item.model_dump(mode="json") for item in job.pair_metrics],
                "matrix_sha256": job.matrix_sha256,
                "current_map_sha256": job.current_map_sha256,
                "quantitative_qa": job.matrix_current_coherence.model_dump(mode="json"),
            }
        )
        previous_output = exact_output_by_unchanged_input.setdefault(
            unchanged_input, exact_output
        )
        if previous_output != exact_output:
            raise UsefulnessDirectStudyError(
                "unchanged direct inputs produced inconsistent matrix/current evidence"
            )
    for landscape in catalog.landscapes:
        required = set(scenario_map[landscape.family])
        safeguards = {
            scenario.scenario_id for scenario in landscape.scenarios if scenario.safeguard
        }
        targets = required - safeguards
        if not safeguards or not targets:
            raise UsefulnessDirectStudyError("reviewed scenario ensemble lacks target or safeguard")
        for portfolio in landscape.portfolios:
            observed = by_portfolio.get((landscape.family, portfolio.portfolio_id), set())
            if observed != required:
                raise UsefulnessDirectStudyError(
                    "portfolio lacks its complete target-and-safeguard direct ensemble"
                )
    return ordered


def _workflow_sources() -> dict[str, Path]:
    root = _repository_root()
    return {
        "usefulness_input_generator": Path(materialize_usefulness_landscapes.__code__.co_filename),
        "usefulness_direct_executor": Path(__file__),
        "engine_protocol_parser": Path(__file__).with_name("engine.py"),
        "engine_provenance_validator": Path(__file__).with_name("engine_provenance.py"),
        "engine_provenance_generator": (
            root / "scripts/create_ecocorridor_engine_provenance.py"
        ),
        "current_raster_parser": Path(__file__).with_name("raster_io.py"),
        "resource_observability": Path(__file__).with_name("resource_observability.py"),
        "python_project_manifest": root / "pyproject.toml",
        "python_dependency_lock": root / "uv.lock",
    }


def _ensure_package_sources(
    evidence_root: Path,
    materialized_root: Path,
    engine_manifest_path: Path,
    engine_manifest_file_sha256: str,
    engine_provenance: VerifiedEngineProvenance,
) -> tuple[list[RetainedDirectArtifact], dict[str, str]]:
    records: list[RetainedDirectArtifact] = []
    workflow: dict[str, str] = {}
    program_path = _repository_root() / "benchmarks/ecocorridor/usefulness/manifest.json"
    expected_program = _canonical_json_bytes(usefulness_public_manifest())
    if program_path.read_bytes() != expected_program:
        raise UsefulnessDirectStudyError("repository usefulness manifest differs from the catalog")
    sources = {
        "program-manifest.json": (program_path, "program_manifest"),
        "engine-manifest.json": (engine_manifest_path, "typed_engine_manifest"),
        "engine-provenance.json": (
            engine_provenance.receipt_path,
            "attested_engine_provenance",
        ),
        "materialization-manifest.json": (
            materialized_root / "materialization-manifest.json",
            "materialization_manifest",
        ),
    }
    verification_path = evidence_root / "engine-attestation-verification.json"
    verification_payload = engine_provenance.verification_output
    if _sha_bytes(verification_payload) != (
        engine_provenance.attestation.verification_output_sha256
    ):
        raise UsefulnessDirectStudyError("engine attestation verification output changed")
    _write_once(verification_path, verification_payload)
    records.append(
        RetainedDirectArtifact(
            role="engine_attestation_verification",
            relative_path="engine-attestation-verification.json",
            sha256=_sha_bytes(verification_payload),
            byte_size=len(verification_payload),
        )
    )
    if engine_provenance.attestation_bundle_path is not None:
        sources["engine-attestation-bundle.jsonl"] = (
            engine_provenance.attestation_bundle_path,
            "engine_attestation_bundle",
        )
    for relative, (source, role) in sources.items():
        payload = source.read_bytes()
        if role == "typed_engine_manifest" and _sha_bytes(payload) != engine_manifest_file_sha256:
            raise UsefulnessDirectStudyError("engine manifest changed before evidence packaging")
        if (
            role == "attested_engine_provenance"
            and _sha_bytes(payload) != engine_provenance.receipt_file_sha256
        ):
            raise UsefulnessDirectStudyError("engine provenance changed before packaging")
        if (
            role == "engine_attestation_bundle"
            and _sha_bytes(payload)
            != engine_provenance.attestation.attestation_bundle_sha256
        ):
            raise UsefulnessDirectStudyError(
                "engine provenance attestation bundle changed before packaging"
            )
        target = evidence_root / relative
        _write_once(target, payload)
        records.append(
            RetainedDirectArtifact(
                role=role,
                relative_path=relative,
                sha256=_sha_bytes(payload),
                byte_size=len(payload),
            )
        )
    for label, source in sorted(_workflow_sources().items()):
        payload = source.read_bytes()
        digest = _sha_bytes(payload)
        suffix = source.suffix or ".bin"
        relative = f"workflow/{label}{suffix}"
        _write_once(evidence_root / relative, payload)
        workflow[label] = digest
        records.append(
            RetainedDirectArtifact(
                role=f"workflow:{label}",
                relative_path=relative,
                sha256=digest,
                byte_size=len(payload),
            )
        )
    return records, workflow


def _collect_archive_files(
    evidence_root: Path,
    jobs: tuple[UsefulnessDirectJobEvidence, ...],
    source_records: list[RetainedDirectArtifact],
) -> list[RetainedDirectArtifact]:
    records = list(source_records)
    seen = {record.relative_path for record in records}
    for job in jobs:
        complete_relative = f"jobs/{job.direct_run_id}/complete.json"
        complete = contained_regular_file(evidence_root, complete_relative)
        complete_record = RetainedDirectArtifact(
            role="job_completion",
            relative_path=complete_relative,
            sha256=sha256_file(complete),
            byte_size=complete.stat().st_size,
        )
        if complete_relative in seen:
            raise UsefulnessDirectStudyError("duplicate completion artifact")
        records.append(complete_record)
        seen.add(complete_relative)
        for artifact in job.artifacts:
            if artifact.relative_path in seen:
                raise UsefulnessDirectStudyError("duplicate direct artifact path")
            path = contained_regular_file(evidence_root, artifact.relative_path)
            if path.stat().st_size != artifact.byte_size or sha256_file(path) != artifact.sha256:
                raise UsefulnessDirectStudyError("direct artifact changed before packaging")
            records.append(artifact)
            seen.add(artifact.relative_path)
    return sorted(records, key=lambda record: record.relative_path)


def _deterministic_archive(
    archive_temporary: Path,
    *,
    evidence_root: Path,
    members: list[Path],
) -> None:
    archive_temporary.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive_temporary.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        ordered = sorted(
            members,
            key=lambda path: path.relative_to(evidence_root).as_posix(),
        )
        for source in ordered:
            if source.is_symlink() or not source.is_file():
                raise UsefulnessDirectStudyError("archive member is absent or unsafe")
            relative = source.relative_to(evidence_root).as_posix()
            info = tarfile.TarInfo(relative)
            info.size = source.stat().st_size
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            with source.open("rb") as stream:
                archive.addfile(info, stream)


def _build_evidence_package(
    *,
    evidence_root: Path,
    materialized_root: Path,
    materialization_manifest_sha256: str,
    jobs: tuple[UsefulnessDirectJobEvidence, ...],
    engine_sha256: str,
    engine_manifest_path: Path,
    engine_manifest_file_sha256: str,
    engine_manifest_sha256: str,
    engine_provenance: VerifiedEngineProvenance,
    engine_provenance_verifier: EngineProvenanceVerifier | None,
    catalog: UsefulnessCatalog,
    source_identity: SourceRepositoryIdentity,
) -> UsefulnessEvidencePackageReceipt:
    if not source_identity.source_tree_clean:
        raise UsefulnessDirectStudyError(
            "release evidence packaging requires a clean committed source tree"
        )
    public_root = evidence_root / "public"
    manifest_path = public_root / "direct-evidence-manifest.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive_name = payload.get("evidence_bundle", {}).get("filename")
        if not isinstance(archive_name, str):
            raise UsefulnessDirectStudyError("existing direct-evidence manifest is invalid")
        if (
            payload.get("source_repository") != source_identity.source_repository
            or payload.get("source_commit") != source_identity.source_commit
            or payload.get("source_tree_clean") is not True
        ):
            raise UsefulnessDirectStudyError(
                "existing evidence package belongs to a different producer source identity"
            )
        return verify_usefulness_evidence_package(
            manifest_path=manifest_path,
            archive_path=public_root / archive_name,
            expected_engine_sha256=engine_sha256,
            engine_manifest_path=engine_manifest_path,
            engine_provenance_path=engine_provenance.receipt_path,
            engine_attestation_bundle_path=engine_provenance.attestation_bundle_path,
            engine_provenance_verifier=engine_provenance_verifier,
            catalog=catalog,
        )
    source_records, workflow = _ensure_package_sources(
        evidence_root,
        materialized_root,
        engine_manifest_path,
        engine_manifest_file_sha256,
        engine_provenance,
    )
    file_records = _collect_archive_files(evidence_root, jobs, source_records)
    index_payload: dict[str, object] = {
        "schema_version": EVIDENCE_INDEX_SCHEMA,
        "program_sha256": usefulness_program_sha256(catalog),
        "run_plan_sha256": usefulness_run_plan_sha256(catalog),
        "materialization_manifest_sha256": materialization_manifest_sha256,
        "engine_executable_sha256": engine_sha256,
        "engine_manifest_sha256": engine_manifest_sha256,
        "engine_manifest_file_sha256": engine_manifest_file_sha256,
        "engine_trust_profile": engine_provenance.receipt.trust_profile,
        "engine_provenance_receipt_sha256": engine_provenance.receipt_file_sha256,
        "engine_attestation_verification_sha256": (
            engine_provenance.attestation.verification_sha256
        ),
        "source_repository": source_identity.source_repository,
        "source_commit": source_identity.source_commit,
        "source_tree_clean": source_identity.source_tree_clean,
        "package_validator_sha256": _validator_sha256(),
        "workflow_sources": workflow,
        "job_count": len(jobs),
        "complete_target_and_safeguard_ensembles": True,
        "all_jobs_exact_and_qa_accepted": True,
        "outcome_promotion": "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION",
        "jobs": [
            job.model_dump(mode="json", exclude={"cached", "cache_lookup_observation"})
            for job in jobs
        ],
        "files": [record.model_dump(mode="json") for record in file_records],
        "files_sha256": canonical_sha256(
            [record.model_dump(mode="json") for record in file_records]
        ),
    }
    index_payload["evidence_index_content_sha256"] = canonical_sha256(index_payload)
    index_path = evidence_root / "evidence-index.json"
    _write_once(index_path, _canonical_json_bytes(index_payload, pretty=True))
    index_sha = sha256_file(index_path)
    members = [
        contained_regular_file(evidence_root, record.relative_path) for record in file_records
    ]
    members.append(index_path)
    public_root.mkdir(parents=True, exist_ok=True)
    archive_temporary = public_root / ".usefulness-direct-evidence.tar.gz.tmp"
    _deterministic_archive(
        archive_temporary,
        evidence_root=evidence_root,
        members=members,
    )
    archive_sha = sha256_file(archive_temporary)
    archive_name = f"usefulness-direct-evidence-{archive_sha[:12]}.tar.gz"
    archive_path = public_root / archive_name
    if archive_path.exists():
        if archive_path.is_symlink() or sha256_file(archive_path) != archive_sha:
            raise UsefulnessDirectStudyError("immutable evidence archive differs")
        archive_temporary.unlink()
    else:
        archive_temporary.replace(archive_path)
    manifest: dict[str, object] = {
        "schema_version": DIRECT_EVIDENCE_SCHEMA,
        "fixture_version": catalog.fixture_version,
        "license": catalog.license,
        "program_sha256": usefulness_program_sha256(catalog),
        "run_plan_sha256": usefulness_run_plan_sha256(catalog),
        "materialization_manifest_sha256": materialization_manifest_sha256,
        "engine_executable_sha256": engine_sha256,
        "engine_manifest_sha256": engine_manifest_sha256,
        "engine_manifest_file_sha256": engine_manifest_file_sha256,
        "engine_trust_profile": engine_provenance.receipt.trust_profile,
        "engine_provenance_receipt_sha256": engine_provenance.receipt_file_sha256,
        "engine_attestation_verification_sha256": (
            engine_provenance.attestation.verification_sha256
        ),
        "source_repository": source_identity.source_repository,
        "source_commit": source_identity.source_commit,
        "source_tree_clean": source_identity.source_tree_clean,
        "evidence_index_sha256": index_sha,
        "job_count": len(jobs),
        "qa_accepted_jobs": len(jobs),
        "complete_target_and_safeguard_ensembles": True,
        "direct_evidence_complete": True,
        "usefulness_claims_promoted": False,
        "outcome_promotion": "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION",
        "release_eligible": False,
        "release_blocker": (
            "Direct evidence exists, but outcome analysis, independent adjudication, and the "
            "pre-registered usefulness gates have not completed."
        ),
        "evidence_bundle": {
            "filename": archive_name,
            "sha256": archive_sha,
            "byte_size": archive_path.stat().st_size,
            "file_count": len(members),
            "internal_index_path": "evidence-index.json",
            "internal_index_sha256": index_sha,
        },
    }
    _write_once(manifest_path, _canonical_json_bytes(manifest, pretty=True))
    return verify_usefulness_evidence_package(
        manifest_path=manifest_path,
        archive_path=archive_path,
        expected_engine_sha256=engine_sha256,
        engine_manifest_path=engine_manifest_path,
        engine_provenance_path=engine_provenance.receipt_path,
        engine_attestation_bundle_path=engine_provenance.attestation_bundle_path,
        engine_provenance_verifier=engine_provenance_verifier,
        catalog=catalog,
    )


def _read_safe_archive(archive_path: Path) -> dict[str, bytes]:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise UsefulnessDirectStudyError("direct-evidence archive is absent or unsafe")
    members: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise UsefulnessDirectStudyError("direct-evidence archive has too many files")
                _require_safe_relative(member.name)
                if member.name in members or not member.isfile() or member.issym() or member.islnk():
                    raise UsefulnessDirectStudyError("direct-evidence archive member is unsafe")
                if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise UsefulnessDirectStudyError("direct-evidence archive member is oversized")
                total += member.size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise UsefulnessDirectStudyError("direct-evidence archive is oversized")
                stream = archive.extractfile(member)
                if stream is None:
                    raise UsefulnessDirectStudyError("direct-evidence archive member is unreadable")
                payload = stream.read(member.size + 1)
                if len(payload) != member.size:
                    raise UsefulnessDirectStudyError("direct-evidence archive member is truncated")
                members[member.name] = payload
    except (OSError, tarfile.TarError) as exc:
        raise UsefulnessDirectStudyError("direct-evidence archive is invalid") from exc
    return members


def _verify_archived_job(
    *,
    job: UsefulnessDirectJobEvidence,
    row: DirectRunPlanRow,
    landscape: UsefulnessLandscape,
    scenario: Any,
    members: dict[str, bytes],
    temporary_root: Path,
) -> None:
    by_role = _artifact_by_role(job)
    if not REQUIRED_ARTIFACT_ROLES.issubset(by_role):
        raise UsefulnessDirectStudyError("archived job omits a required artifact")
    for artifact in job.artifacts:
        payload = members.get(artifact.relative_path)
        if (
            payload is None
            or len(payload) != artifact.byte_size
            or _sha_bytes(payload) != artifact.sha256
        ):
            raise UsefulnessDirectStudyError("archived job artifact differs from its hash")
    request_bytes = members[by_role["request"].relative_path]
    result_bytes = members[by_role["result"].relative_path]
    if _sha_bytes(request_bytes) != row.request_sha256:
        raise UsefulnessDirectStudyError("archived request differs from the run plan")
    request = json.loads(request_bytes)
    try:
        protocol = EngineProtocolResult.model_validate_json(result_bytes)
    except ValueError as exc:
        raise UsefulnessDirectStudyError("archived result protocol is invalid") from exc
    pairs = [PreparedPair.model_validate(pair) for pair in request.get("pairs", [])]
    matrix_path = temporary_root / f"{job.direct_run_id}-matrix.out"
    matrix_path.write_bytes(members[by_role["matrix"].relative_path])
    parsed = tuple(parse_circuitscape_matrix(matrix_path, pairs))
    if parsed != job.pair_metrics or [item.model_dump(mode="json") for item in parsed] != [
        item.model_dump(mode="json")
        for item in sorted(protocol.pair_metrics, key=lambda metric: metric.pair_id)
    ]:
        raise UsefulnessDirectStudyError("archived matrix differs from stored direct metrics")
    current_path = temporary_root / f"{job.direct_run_id}-current.asc"
    current_path.write_bytes(members[by_role["current_raster"].relative_path])
    current_statistics = _current_raster_is_valid(current_path, landscape, scenario)
    if _matrix_current_coherence(parsed, current_statistics) != job.matrix_current_coherence:
        raise UsefulnessDirectStudyError(
            "archived matrix/current quantitative QA differs from the retained job"
        )
    config_path = temporary_root / f"{job.direct_run_id}-circuitscape.ini"
    config_path.write_bytes(members[by_role["effective_configuration"].relative_path])
    validate_effective_circuitscape_configuration(
        config_path,
        resource_paths={
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/included_pairs.tsv",
        },
        output_directory="outputs",
        neighbor_rule=scenario.neighbor_rule.value,
        resistance_average=scenario.resistance_average.value,
        emit_current_map=True,
    )


def verify_usefulness_evidence_package(
    *,
    manifest_path: Path,
    archive_path: Path,
    expected_engine_sha256: str,
    engine_manifest_path: Path,
    engine_provenance_path: Path | None,
    engine_attestation_bundle_path: Path | None = None,
    engine_provenance_verifier: EngineProvenanceVerifier | None = None,
    catalog: UsefulnessCatalog | None = None,
) -> UsefulnessEvidencePackageReceipt:
    """Independently verify the immutable archive and all 160 direct jobs."""

    catalog = catalog or build_usefulness_catalog()
    expected_engine_sha256 = _require_digest(expected_engine_sha256, "engine identity")
    (
        resolved_engine_manifest,
        engine_manifest,
        engine_manifest_file_sha256,
    ) = validate_usefulness_engine_manifest(
        manifest_path=engine_manifest_path,
        expected_engine_sha256=expected_engine_sha256,
    )
    engine_manifest_sha256 = engine_manifest.sha256
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise UsefulnessDirectStudyError("direct-evidence manifest is absent or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsefulnessDirectStudyError("direct-evidence manifest is invalid") from exc
    try:
        source_identity = SourceRepositoryIdentity(
            source_repository=manifest.get("source_repository"),
            source_commit=manifest.get("source_commit"),
            source_tree_clean=manifest.get("source_tree_clean"),
        )
    except ValueError as exc:
        raise UsefulnessDirectStudyError("direct-evidence source identity is invalid") from exc
    verified_provenance = validate_production_engine_provenance(
        provenance_path=engine_provenance_path,
        attestation_bundle_path=engine_attestation_bundle_path,
        expected_engine_sha256=expected_engine_sha256,
        engine_manifest=engine_manifest,
        engine_manifest_file_sha256=engine_manifest_file_sha256,
        source_identity=source_identity,
        verifier=engine_provenance_verifier,
    )
    if (
        manifest.get("schema_version") != DIRECT_EVIDENCE_SCHEMA
        or manifest.get("program_sha256") != usefulness_program_sha256(catalog)
        or manifest.get("run_plan_sha256") != usefulness_run_plan_sha256(catalog)
        or manifest.get("engine_executable_sha256") != expected_engine_sha256
        or manifest.get("engine_manifest_sha256") != engine_manifest_sha256
        or manifest.get("engine_manifest_file_sha256") != engine_manifest_file_sha256
        or manifest.get("engine_trust_profile")
        != "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        or manifest.get("engine_provenance_receipt_sha256")
        != verified_provenance.receipt_file_sha256
        or manifest.get("engine_attestation_verification_sha256")
        != verified_provenance.attestation.verification_sha256
        or manifest.get("job_count") != 160
        or manifest.get("qa_accepted_jobs") != 160
        or manifest.get("complete_target_and_safeguard_ensembles") is not True
        or manifest.get("direct_evidence_complete") is not True
        or manifest.get("usefulness_claims_promoted") is not False
        or manifest.get("release_eligible") is not False
        or source_identity.source_tree_clean is not True
        or manifest.get("outcome_promotion")
        != "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"
    ):
        raise UsefulnessDirectStudyError("direct-evidence manifest fails its claim boundary")
    bundle = manifest.get("evidence_bundle")
    if not isinstance(bundle, dict) or bundle.get("filename") != archive_path.name:
        raise UsefulnessDirectStudyError("direct-evidence archive identity is inconsistent")
    if (
        sha256_file(archive_path) != bundle.get("sha256")
        or archive_path.stat().st_size != bundle.get("byte_size")
    ):
        raise UsefulnessDirectStudyError("direct-evidence archive differs from its manifest")
    members = _read_safe_archive(archive_path)
    if len(members) != bundle.get("file_count"):
        raise UsefulnessDirectStudyError("direct-evidence archive file count is inconsistent")
    index_name = bundle.get("internal_index_path")
    if index_name != "evidence-index.json" or index_name not in members:
        raise UsefulnessDirectStudyError("direct-evidence archive omits its index")
    index_bytes = members[index_name]
    if (
        _sha_bytes(index_bytes) != bundle.get("internal_index_sha256")
        or _sha_bytes(index_bytes) != manifest.get("evidence_index_sha256")
    ):
        raise UsefulnessDirectStudyError("direct-evidence index hash is inconsistent")
    index = json.loads(index_bytes)
    asserted_content = index.get("evidence_index_content_sha256")
    index_body = {
        key: value for key, value in index.items() if key != "evidence_index_content_sha256"
    }
    if asserted_content != canonical_sha256(index_body):
        raise UsefulnessDirectStudyError("direct-evidence index content digest is invalid")
    if (
        index.get("schema_version") != EVIDENCE_INDEX_SCHEMA
        or index.get("program_sha256") != usefulness_program_sha256(catalog)
        or index.get("run_plan_sha256") != usefulness_run_plan_sha256(catalog)
        or index.get("engine_executable_sha256") != expected_engine_sha256
        or index.get("engine_manifest_sha256") != engine_manifest_sha256
        or index.get("engine_manifest_file_sha256") != engine_manifest_file_sha256
        or index.get("engine_trust_profile")
        != "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        or index.get("engine_provenance_receipt_sha256")
        != verified_provenance.receipt_file_sha256
        or index.get("engine_attestation_verification_sha256")
        != verified_provenance.attestation.verification_sha256
        or index.get("job_count") != 160
        or index.get("complete_target_and_safeguard_ensembles") is not True
        or index.get("all_jobs_exact_and_qa_accepted") is not True
        or index.get("source_repository") != source_identity.source_repository
        or index.get("source_commit") != source_identity.source_commit
        or index.get("source_tree_clean") is not True
    ):
        raise UsefulnessDirectStudyError("direct-evidence index identity is inconsistent")
    file_records = tuple(
        RetainedDirectArtifact.model_validate(record) for record in index.get("files", [])
    )
    if index.get("files_sha256") != canonical_sha256(
        [record.model_dump(mode="json") for record in file_records]
    ):
        raise UsefulnessDirectStudyError("direct-evidence file index digest is invalid")
    indexed = {record.relative_path: record for record in file_records}
    if len(indexed) != len(file_records) or set(members) != {index_name, *indexed}:
        raise UsefulnessDirectStudyError("archive members differ from the evidence index")
    for relative, record in indexed.items():
        payload = members[relative]
        if len(payload) != record.byte_size or _sha_bytes(payload) != record.sha256:
            raise UsefulnessDirectStudyError("archive member differs from its indexed hash")
    archived_engine_manifest = members.get("engine-manifest.json")
    if (
        archived_engine_manifest is None
        or _sha_bytes(archived_engine_manifest) != engine_manifest_file_sha256
        or archived_engine_manifest != resolved_engine_manifest.read_bytes()
    ):
        raise UsefulnessDirectStudyError(
            "archived typed engine manifest differs from the supplied bundle identity"
        )
    if members.get("engine-provenance.json") != verified_provenance.receipt_path.read_bytes():
        raise UsefulnessDirectStudyError(
            "archived engine provenance differs from the independently attested receipt"
        )
    archived_verification = members.get("engine-attestation-verification.json")
    if (
        archived_verification != verified_provenance.verification_output
        or archived_verification is None
        or _sha_bytes(archived_verification)
        != verified_provenance.attestation.verification_output_sha256
    ):
        raise UsefulnessDirectStudyError(
            "archived engine attestation verification differs from independent readback"
        )
    if verified_provenance.attestation_bundle_path is not None and members.get(
        "engine-attestation-bundle.jsonl"
    ) != verified_provenance.attestation_bundle_path.read_bytes():
        raise UsefulnessDirectStudyError(
            "archived engine attestation bundle differs from the verified bundle"
        )
    jobs = tuple(
        UsefulnessDirectJobEvidence.model_validate(job) for job in index.get("jobs", [])
    )
    verify_complete_usefulness_ensemble(jobs, catalog=catalog)
    if any(job.engine_manifest_sha256 != engine_manifest_sha256 for job in jobs):
        raise UsefulnessDirectStudyError("archived direct jobs bind another engine manifest")
    if any(
        job.engine_trust_profile != "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        or job.engine_provenance_receipt_sha256
        != verified_provenance.receipt_file_sha256
        for job in jobs
    ):
        raise UsefulnessDirectStudyError(
            "archived direct jobs are test-only or bind another engine provenance"
        )
    rows = {row.direct_run_id: row for row in usefulness_run_plan_rows(catalog)}
    landscapes, scenarios = _landscape_maps(catalog)
    with tempfile.TemporaryDirectory(prefix="ecocorridor-usefulness-verify-") as temporary:
        temporary_root = Path(temporary)
        for job in jobs:
            _verify_archived_job(
                job=job,
                row=rows[job.direct_run_id],
                landscape=landscapes[job.family],
                scenario=scenarios[(job.family, job.scenario_id)],
                members=members,
                temporary_root=temporary_root,
            )
    return UsefulnessEvidencePackageReceipt(
        manifest_path=manifest_path.resolve(),
        archive_path=archive_path.resolve(),
        manifest_sha256=sha256_file(manifest_path),
        archive_sha256=sha256_file(archive_path),
        evidence_index_sha256=_sha_bytes(index_bytes),
        engine_manifest_sha256=engine_manifest_sha256,
        engine_provenance_receipt_sha256=verified_provenance.receipt_file_sha256,
        engine_attestation_verification_sha256=(
            verified_provenance.attestation.verification_sha256
        ),
        engine_provenance_path=verified_provenance.receipt_path,
        engine_attestation_bundle_path=verified_provenance.attestation_bundle_path,
        source_repository=source_identity.source_repository,
        source_commit=source_identity.source_commit,
    )


def run_usefulness_direct_study(
    *,
    materialized_root: Path,
    output_directory: Path,
    engine_executable: Path,
    engine_manifest_path: Path,
    expected_engine_sha256: str,
    engine_provenance_path: Path | None = None,
    engine_attestation_bundle_path: Path | None = None,
    engine_provenance_verifier: EngineProvenanceVerifier | None = None,
    test_only_engine: bool = False,
    worker_count: int = 4,
    timeout_seconds: float = 300.0,
    job_limit: int | None = None,
    progress: Callable[[int, int, bool], None] | None = None,
) -> UsefulnessDirectStudyResult:
    """Run, resume, QA, and package the complete 160-job direct study."""

    if worker_count < 1 or worker_count > MAX_WORKERS:
        raise UsefulnessDirectStudyError(f"worker count must be between 1 and {MAX_WORKERS}")
    if timeout_seconds <= 0:
        raise UsefulnessDirectStudyError("direct-job timeout must be positive")
    engine_sha = _require_digest(expected_engine_sha256, "engine executable identity")
    engine = _regular_executable(engine_executable, engine_sha)
    (
        engine_manifest,
        typed_engine_manifest,
        engine_manifest_file_sha256,
    ) = validate_usefulness_engine_manifest(
        manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        executable_path=engine,
    )
    engine_manifest_sha256 = typed_engine_manifest.sha256
    catalog = build_usefulness_catalog()
    materialized = materialized_root.expanduser().resolve(strict=True)
    _, rows, materialization_sha = verify_materialized_usefulness_inputs(
        materialized, catalog=catalog
    )
    output = output_directory.expanduser().resolve()
    _assert_separate_roots(materialized, output, engine, engine_manifest)
    output.mkdir(parents=True, exist_ok=True)
    if job_limit is not None and (job_limit < 1 or job_limit > len(rows)):
        raise UsefulnessDirectStudyError("job limit must be between 1 and 160")
    scheduled = rows if job_limit is None else rows[:job_limit]
    source_at_start = _source_repository_identity()
    verified_provenance: VerifiedEngineProvenance | None = None
    if test_only_engine:
        if engine_provenance_path is not None or engine_attestation_bundle_path is not None:
            raise UsefulnessDirectStudyError(
                "test-only execution cannot accept production provenance inputs"
            )
        engine_trust_profile = "TEST_ONLY_UNPUBLISHABLE"
        provenance_receipt_sha256 = None
    else:
        verified_provenance = validate_production_engine_provenance(
            provenance_path=engine_provenance_path,
            attestation_bundle_path=engine_attestation_bundle_path,
            expected_engine_sha256=engine_sha,
            engine_manifest=typed_engine_manifest,
            engine_manifest_file_sha256=engine_manifest_file_sha256,
            source_identity=source_at_start,
            verifier=engine_provenance_verifier,
        )
        engine_trust_profile = verified_provenance.receipt.trust_profile
        provenance_receipt_sha256 = verified_provenance.receipt_file_sha256
    if len(scheduled) == len(rows) and not source_at_start.source_tree_clean:
        raise UsefulnessDirectStudyError(
            "the complete 160-job release run requires a clean committed source tree; "
            "use a smaller --job-limit only for non-release smoke verification"
        )
    lock = FileLock(str(output / ".usefulness-direct-study.lock"), thread_local=False)
    try:
        lock.acquire(timeout=0)
    except FileLockTimeout as exc:
        raise UsefulnessDirectStudyError("another usefulness direct study owns this output") from exc
    jobs: list[UsefulnessDirectJobEvidence] = []
    try:
        futures: dict[Future[UsefulnessDirectJobEvidence], DirectRunPlanRow] = {}
        executor = ThreadPoolExecutor(max_workers=worker_count)
        try:
            for row in scheduled:
                futures[
                    executor.submit(
                        _execute_one_job,
                        materialized_root=materialized,
                        evidence_root=output,
                        engine_executable=engine,
                        engine_sha256=engine_sha,
                        engine_manifest_path=engine_manifest,
                        engine_manifest_file_sha256=engine_manifest_file_sha256,
                        engine_manifest_sha256=engine_manifest_sha256,
                        engine_trust_profile=engine_trust_profile,
                        engine_provenance_receipt_sha256=provenance_receipt_sha256,
                        catalog=catalog,
                        row=row,
                        timeout_seconds=timeout_seconds,
                    )
                ] = row
            for future in as_completed(futures):
                job = future.result()
                jobs.append(job)
                if progress:
                    progress(len(jobs), len(scheduled), job.cached)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        jobs.sort(key=lambda job: job.direct_run_id)
        cached_count = sum(job.cached for job in jobs)
        if len(jobs) != 160:
            return UsefulnessDirectStudyResult(
                state="INCOMPLETE",
                completed_jobs=len(jobs),
                cached_jobs=cached_count,
                engine_executable_sha256=engine_sha,
                engine_manifest_sha256=engine_manifest_sha256,
                engine_trust_profile=engine_trust_profile,
                engine_provenance_receipt_sha256=provenance_receipt_sha256,
                program_sha256=usefulness_program_sha256(catalog),
                run_plan_sha256=usefulness_run_plan_sha256(catalog),
            )
        complete = verify_complete_usefulness_ensemble(jobs, catalog=catalog)
        if test_only_engine:
            return UsefulnessDirectStudyResult(
                state="COMPLETED_TEST_ONLY_UNPUBLISHABLE",
                completed_jobs=160,
                cached_jobs=cached_count,
                engine_executable_sha256=engine_sha,
                engine_manifest_sha256=engine_manifest_sha256,
                engine_trust_profile="TEST_ONLY_UNPUBLISHABLE",
                program_sha256=usefulness_program_sha256(catalog),
                run_plan_sha256=usefulness_run_plan_sha256(catalog),
            )
        (
            completed_engine_manifest_path,
            completed_engine_manifest,
            completed_engine_manifest_file_sha256,
        ) = validate_usefulness_engine_manifest(
            manifest_path=engine_manifest,
            expected_engine_sha256=engine_sha,
            executable_path=engine,
        )
        if (
            completed_engine_manifest_path != engine_manifest
            or completed_engine_manifest.sha256 != engine_manifest_sha256
            or completed_engine_manifest_file_sha256 != engine_manifest_file_sha256
        ):
            raise UsefulnessDirectStudyError("engine manifest changed during the direct study")
        source_at_completion = _source_repository_identity()
        if source_at_completion != source_at_start or not source_at_completion.source_tree_clean:
            return UsefulnessDirectStudyResult(
                state="QA_ACCEPTED_PACKAGE_BLOCKED_DIRTY_SOURCE",
                completed_jobs=160,
                cached_jobs=cached_count,
                engine_executable_sha256=engine_sha,
                engine_manifest_sha256=engine_manifest_sha256,
                engine_trust_profile=engine_trust_profile,
                engine_provenance_receipt_sha256=provenance_receipt_sha256,
                program_sha256=usefulness_program_sha256(catalog),
                run_plan_sha256=usefulness_run_plan_sha256(catalog),
            )
        if verified_provenance is None:  # pragma: no cover - guarded above
            raise AssertionError("production package lacks verified provenance")
        receipt = _build_evidence_package(
            evidence_root=output,
            materialized_root=materialized,
            materialization_manifest_sha256=materialization_sha,
            jobs=complete,
            engine_sha256=engine_sha,
            engine_manifest_path=engine_manifest,
            engine_manifest_file_sha256=engine_manifest_file_sha256,
            engine_manifest_sha256=engine_manifest_sha256,
            engine_provenance=verified_provenance,
            engine_provenance_verifier=engine_provenance_verifier,
            catalog=catalog,
            source_identity=source_at_completion,
        )
        return UsefulnessDirectStudyResult(
            state="COMPLETED_QA_ACCEPTED",
            completed_jobs=160,
            cached_jobs=cached_count,
            engine_executable_sha256=engine_sha,
            engine_manifest_sha256=engine_manifest_sha256,
            engine_trust_profile=engine_trust_profile,
            engine_provenance_receipt_sha256=provenance_receipt_sha256,
            program_sha256=usefulness_program_sha256(catalog),
            run_plan_sha256=usefulness_run_plan_sha256(catalog),
            evidence_index_sha256=receipt.evidence_index_sha256,
            evidence_manifest_path=receipt.manifest_path,
            evidence_archive_path=receipt.archive_path,
        )
    finally:
        lock.release()


__all__ = [
    "DIRECT_EVIDENCE_SCHEMA",
    "MAX_WORKERS",
    "RetainedDirectArtifact",
    "UsefulnessDirectJobEvidence",
    "UsefulnessDirectStudyError",
    "UsefulnessDirectStudyResult",
    "UsefulnessEvidencePackageReceipt",
    "run_usefulness_direct_study",
    "validate_usefulness_engine_manifest",
    "verify_complete_usefulness_ensemble",
    "verify_materialized_usefulness_inputs",
    "verify_usefulness_evidence_package",
]
