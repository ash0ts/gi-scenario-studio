"""Fail-closed W&B publication for verified usefulness direct evidence.

This module publishes computational evidence completeness only. It never
projects pair metrics, current-density values, portfolio rankings, ecological
outcomes, or usefulness conclusions. The local direct-evidence verifier remains
authoritative and is rerun before any cloud operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, model_validator

from .contracts import ContractModel, Sha256Digest, canonical_sha256
from .engine import sha256_file
from .engine_provenance import EngineProvenanceVerifier
from .usefulness_direct_study import (
    UsefulnessEvidencePackageReceipt,
    verify_usefulness_evidence_package,
)

DIRECT_PUBLICATION_SCHEMA = "ecocorridor-usefulness-direct-publication/v1.0"
DIRECT_RUN_KIND = "usefulness_fixture_generation"
DIRECT_ARTIFACT_TYPE = "usefulness-direct-evidence"
DIRECT_SECTION_ID = "usefulness-direct-evidence/v1"
DIRECT_SECTION_START = f"<!-- ecocorridor-owned-section:{DIRECT_SECTION_ID}:start"
DIRECT_SECTION_END = f"<!-- ecocorridor-owned-section:{DIRECT_SECTION_ID}:end -->"
DIRECT_UPLOAD_AUTHORIZATION = (
    "PUBLIC_CC0_SELF_AUTHORED_USEFULNESS_DIRECT_EVIDENCE_ONLY_"
    "NO_REAL_LOCATIONS_OR_SPECIES"
)
OUTCOME_BLOCKER = "BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"
MAX_INDEX_BYTES = 32 * 1024 * 1024
READBACK_TIMEOUT_SECONDS = 12.0
READBACK_INTERVAL_SECONDS = 0.4

DIRECT_JOB_COLUMNS = (
    "direct_run_id",
    "landscape_family",
    "portfolio_id",
    "scenario_id",
    "safeguard",
    "state",
    "exact",
    "qa_status",
    "qa_finding_count",
    "engine_name",
    "engine_version",
    "engine_executable_sha256",
    "engine_manifest_sha256",
    "model_run_cache_key",
    "request_sha256",
    "result_sha256",
    "matrix_sha256",
    "current_map_sha256",
    "effective_configuration_sha256",
    "artifact_count",
    "matrix_current_qa_sha256",
    "process_tree_exit_confirmed",
    "producer_validator_sha256",
)
RESOURCE_QA_COLUMNS = (
    "direct_run_id",
    "observation_id",
    "boundary",
    "solver_wall_seconds",
    "peak_memory_status",
    "peak_memory_bytes",
    "peak_memory_method",
    "peak_memory_sample_count",
    "peak_memory_is_lower_bound",
    "energy_status",
    "energy_kwh",
    "energy_method",
    "energy_estimate_lower_kwh",
    "energy_estimate_upper_kwh",
    "assumption_count",
    "exclusion_count",
    "resource_observation_sha256",
)


class DirectEvidencePublicationError(RuntimeError):
    """A publication precondition or exact readback failed."""


def _safe_https_url(value: object, *, label: str) -> str:
    rendered = str(value or "")
    parsed = urlsplit(rendered)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    return rendered


def _https_origin(value: object, *, label: str) -> tuple[str, str, int]:
    rendered = _safe_https_url(value, label=label)
    parsed = urlsplit(rendered)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    return parsed.scheme, str(parsed.hostname).casefold(), port


def _require_web_origin(value: object, *, web_base_url: str, label: str) -> str:
    rendered = _safe_https_url(value, label=label)
    if _https_origin(rendered, label=label) != _https_origin(
        web_base_url, label="W&B web base URL"
    ):
        raise PermissionError(f"{label} belongs to another W&B deployment")
    return rendered


def _expected_run_url(
    *, web_base_url: str, entity: str, project: str, run_id: str
) -> str:
    for value, label in ((entity, "entity"), (project, "project"), (run_id, "run ID")):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError(f"W&B {label} contains unsupported characters")
    return f"{web_base_url.rstrip('/')}/{entity}/{project}/runs/{run_id}"


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    return (
        getattr(error, "status_code", None) == 404
        or getattr(response, "status_code", None) == 404
    )


def _normalize_repository_url(value: object) -> str:
    rendered = str(value or "").strip()
    if rendered.startswith("git@") and ":" in rendered:
        authority, path = rendered[4:].split(":", 1)
        if not authority or not path:
            raise ValueError("source repository SSH URL is invalid")
        rendered = f"https://{authority}/{path}"
    elif rendered.startswith("ssh://"):
        parsed = urlsplit(rendered)
        if parsed.username != "git" or not parsed.hostname or parsed.password:
            raise ValueError("source repository SSH URL is not a supported Git identity")
        if parsed.port not in (None, 22):
            raise ValueError("source repository SSH URL uses an unsupported port")
        rendered = urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    rendered = _safe_https_url(rendered, label="source repository").rstrip("/")
    return rendered.removesuffix(".git")


class PushedSourceReceipt(ContractModel):
    schema_version: Literal["ecocorridor-pushed-source/v1"] = (
        "ecocorridor-pushed-source/v1"
    )
    source_repository: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree_clean: Literal[True] = True
    pushed_to_upstream: Literal[True] = True
    upstream_ref: str = Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]+$")
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    verifier: Literal["GIT_LS_REMOTE_EXACT_HEAD"] = "GIT_LS_REMOTE_EXACT_HEAD"
    receipt_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if _normalize_repository_url(self.source_repository) != self.source_repository:
            raise ValueError("source repository must use canonical credential-free HTTPS")
        if self.upstream_commit != self.source_commit:
            raise ValueError("pushed upstream commit differs from the package source commit")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("pushed-source receipt digest differs from its contents")
        return self

    @classmethod
    def create(
        cls,
        *,
        source_repository: str,
        source_commit: str,
        upstream_ref: str,
        upstream_commit: str,
    ) -> PushedSourceReceipt:
        payload = {
            "schema_version": "ecocorridor-pushed-source/v1",
            "source_repository": _normalize_repository_url(source_repository),
            "source_commit": source_commit,
            "source_tree_clean": True,
            "pushed_to_upstream": True,
            "upstream_ref": upstream_ref,
            "upstream_commit": upstream_commit,
            "verifier": "GIT_LS_REMOTE_EXACT_HEAD",
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


GitRunner = Callable[[Sequence[str], Path], str]


def _default_git_runner(arguments: Sequence[str], root: Path) -> str:
    try:
        return subprocess.run(
            list(arguments),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DirectEvidencePublicationError("pushed-source verification failed") from exc


class GitPushedSourceVerifier:
    """Verify an exact clean HEAD against the authoritative remote branch."""

    def __init__(self, *, runner: GitRunner = _default_git_runner) -> None:
        self._runner = runner

    def verify(
        self,
        *,
        repository_root: Path,
        source_repository: str,
        source_commit: str,
    ) -> PushedSourceReceipt:
        root = repository_root.resolve()
        head = self._runner(("git", "rev-parse", "HEAD"), root)
        status = self._runner(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"), root
        )
        branch = self._runner(("git", "symbolic-ref", "--short", "HEAD"), root)
        if head != source_commit:
            raise PermissionError("package source commit differs from checked-out HEAD")
        if status:
            raise PermissionError("direct-evidence publication requires a clean source tree")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise PermissionError("publication requires an attached branch with a safe name")
        remote = self._runner(("git", "config", "--get", f"branch.{branch}.remote"), root)
        upstream_ref = self._runner(
            ("git", "config", "--get", f"branch.{branch}.merge"), root
        )
        if not remote or remote == "." or not re.fullmatch(r"[A-Za-z0-9_.-]+", remote):
            raise PermissionError("publication branch has no supported remote upstream")
        if not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", upstream_ref):
            raise PermissionError("publication branch has no supported upstream branch")
        remote_url = self._runner(("git", "remote", "get-url", remote), root)
        if _normalize_repository_url(remote_url) != _normalize_repository_url(
            source_repository
        ):
            raise PermissionError("package source repository differs from branch upstream")
        remote_line = self._runner(
            ("git", "ls-remote", "--exit-code", source_repository, upstream_ref), root
        )
        matches = [
            line.split()
            for line in remote_line.splitlines()
            if line.strip() and len(line.split()) == 2
        ]
        if len(matches) != 1 or matches[0][1] != upstream_ref:
            raise PermissionError("authoritative upstream branch identity is ambiguous")
        upstream_commit = matches[0][0]
        if upstream_commit != source_commit:
            raise PermissionError("package source commit is not the current pushed upstream HEAD")
        return PushedSourceReceipt.create(
            source_repository=_normalize_repository_url(source_repository),
            source_commit=source_commit,
            upstream_ref=upstream_ref,
            upstream_commit=upstream_commit,
        )


class PushedSourceVerifier(Protocol):
    def verify(
        self,
        *,
        repository_root: Path,
        source_repository: str,
        source_commit: str,
    ) -> PushedSourceReceipt: ...


class DirectEvidenceTable(ContractModel):
    name: Literal["usefulness_direct_jobs", "usefulness_direct_resource_qa"]
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...] = Field(min_length=160, max_length=160)

    @model_validator(mode="after")
    def validate_table(self) -> Self:
        expected = (
            DIRECT_JOB_COLUMNS
            if self.name == "usefulness_direct_jobs"
            else RESOURCE_QA_COLUMNS
        )
        if self.columns != expected:
            raise ValueError("direct-evidence Table columns differ from the approved projection")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("direct-evidence Table row width is inconsistent")
        direct_ids = [str(row[0]) for row in self.rows]
        if len(set(direct_ids)) != 160 or direct_ids != sorted(direct_ids):
            raise ValueError("direct-evidence Table requires 160 sorted unique direct jobs")
        return self


class DirectEvidenceArtifactFile(ContractModel):
    role: Literal[
        "PACKAGE_MANIFEST",
        "PACKAGE_ARCHIVE",
        "ENGINE_MANIFEST",
        "ENGINE_PROVENANCE",
        "ENGINE_ATTESTATION_BUNDLE",
    ]
    logical_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    sha256: Sha256Digest
    byte_size: int = Field(gt=0)


class DirectEvidencePublicationPlan(ContractModel):
    schema_version: Literal["ecocorridor-usefulness-direct-publication/v1.0"] = (
        DIRECT_PUBLICATION_SCHEMA
    )
    upload_authorization: Literal[
        "PUBLIC_CC0_SELF_AUTHORED_USEFULNESS_DIRECT_EVIDENCE_ONLY_NO_REAL_LOCATIONS_OR_SPECIES"
    ]
    run_id: str = Field(pattern=r"^usefulness-direct-[0-9a-f]{24}$")
    run_name: str = Field(min_length=1, max_length=128)
    run_kind: Literal["usefulness_fixture_generation"] = DIRECT_RUN_KIND
    artifact_name: str = Field(pattern=r"^ecocorridor-usefulness-direct-[0-9a-f]{16}$")
    artifact_type: Literal["usefulness-direct-evidence"] = DIRECT_ARTIFACT_TYPE
    license: Literal["CC0-1.0"] = "CC0-1.0"
    source_repository: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_upstream_ref: str = Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]+$")
    source_verifier: Literal["GIT_LS_REMOTE_EXACT_HEAD"] = "GIT_LS_REMOTE_EXACT_HEAD"
    pushed_source_receipt_sha256: Sha256Digest
    package_manifest_sha256: Sha256Digest
    package_archive_sha256: Sha256Digest
    evidence_index_sha256: Sha256Digest
    engine_executable_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    engine_manifest_file_sha256: Sha256Digest
    engine_provenance_receipt_sha256: Sha256Digest
    engine_attestation_verification_sha256: Sha256Digest
    artifact_files: tuple[DirectEvidenceArtifactFile, ...] = Field(
        min_length=4, max_length=5
    )
    artifact_manifest_sha256: Sha256Digest
    direct_evidence_complete: Literal[True] = True
    job_count: Literal[160] = 160
    qa_accepted_jobs: Literal[160] = 160
    outcome_analysis: Literal["BLOCKED"] = "BLOCKED"
    adjudication: Literal["BLOCKED"] = "BLOCKED"
    usefulness_claims: Literal["BLOCKED"] = "BLOCKED"
    outcome_promotion: Literal["BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION"] = (
        OUTCOME_BLOCKER
    )
    tables: tuple[DirectEvidenceTable, ...] = Field(min_length=2, max_length=2)
    manifest_path: Path = Field(exclude=True, repr=False)
    archive_path: Path = Field(exclude=True, repr=False)
    engine_manifest_path: Path = Field(exclude=True, repr=False)
    engine_provenance_path: Path = Field(exclude=True, repr=False)
    engine_attestation_bundle_path: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    plan_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if _normalize_repository_url(self.source_repository) != self.source_repository:
            raise ValueError("publication source repository is not canonical HTTPS")
        if tuple(item.name for item in self.tables) != (
            "usefulness_direct_jobs",
            "usefulness_direct_resource_qa",
        ):
            raise ValueError("publication requires the two typed direct-evidence Tables")
        roles = {item.role for item in self.artifact_files}
        names = {item.logical_name for item in self.artifact_files}
        required_roles = {
            "PACKAGE_MANIFEST",
            "PACKAGE_ARCHIVE",
            "ENGINE_MANIFEST",
            "ENGINE_PROVENANCE",
        }
        allowed_roles = required_roles | {"ENGINE_ATTESTATION_BUNDLE"}
        if len(names) != len(self.artifact_files) or not required_roles <= roles <= allowed_roles:
            raise ValueError("publication artifact omits required immutable provenance inputs")
        expected_artifact_digest = canonical_sha256(
            [item.model_dump(mode="json") for item in self.artifact_files]
        )
        if self.artifact_manifest_sha256 != expected_artifact_digest:
            raise ValueError("publication artifact manifest digest differs")
        if self.plan_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"plan_sha256"})
        ):
            raise ValueError("direct-evidence publication plan digest differs")
        return self

    @property
    def run_config(self) -> dict[str, Any]:
        return {
            "run_kind": self.run_kind,
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "upload_authorization": self.upload_authorization,
            "source_repository": self.source_repository,
            "source_commit": self.source_commit,
            "source_tree_clean": True,
            "pushed_to_upstream": True,
            "source_upstream_ref": self.source_upstream_ref,
            "source_verifier": self.source_verifier,
            "pushed_source_receipt_sha256": self.pushed_source_receipt_sha256,
            "license": self.license,
            "data_classification": "CC0_SYNTHETIC",
            "package_manifest_sha256": self.package_manifest_sha256,
            "package_archive_sha256": self.package_archive_sha256,
            "evidence_index_sha256": self.evidence_index_sha256,
            "engine_executable_sha256": self.engine_executable_sha256,
            "engine_manifest_sha256": self.engine_manifest_sha256,
            "engine_manifest_file_sha256": self.engine_manifest_file_sha256,
            "engine_trust_profile": "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
            "engine_provenance_receipt_sha256": self.engine_provenance_receipt_sha256,
            "engine_attestation_verification_sha256": (
                self.engine_attestation_verification_sha256
            ),
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "job_count": 160,
            "qa_accepted_jobs": 160,
            "direct_evidence_complete": True,
            "outcome_analysis": "BLOCKED",
            "adjudication": "BLOCKED",
            "usefulness_claims": "BLOCKED",
            "outcome_promotion": OUTCOME_BLOCKER,
            "ecological_outcome_value_count": 0,
            "new_result_media_logged": False,
        }

    def expected_summary(self, *, artifact_ref: str) -> dict[str, Any]:
        return {
            "direct_evidence/status": "COMPLETE",
            "direct_evidence/plan_sha256": self.plan_sha256,
            "direct_evidence/job_count": 160,
            "direct_evidence/qa_accepted_jobs": 160,
            "direct_evidence/artifact_ref": artifact_ref,
            "direct_evidence/artifact_manifest_sha256": self.artifact_manifest_sha256,
            "direct_evidence/outcome_analysis": "BLOCKED",
            "direct_evidence/adjudication": "BLOCKED",
            "direct_evidence/usefulness_claims": "BLOCKED",
            "direct_evidence/outcome_promotion": OUTCOME_BLOCKER,
            "direct_evidence/ecological_outcome_value_count": 0,
            "direct_evidence/new_result_media_logged": 0,
        }

    @property
    def artifact_paths(self) -> dict[str, Path]:
        archive_name = next(
            item.logical_name
            for item in self.artifact_files
            if item.role == "PACKAGE_ARCHIVE"
        )
        paths = {
            "direct-evidence-manifest.json": self.manifest_path,
            archive_name: self.archive_path,
            "engine-manifest.json": self.engine_manifest_path,
            "engine-provenance.json": self.engine_provenance_path,
        }
        if self.engine_attestation_bundle_path is not None:
            paths["engine-attestation-bundle.jsonl"] = self.engine_attestation_bundle_path
        return paths


def _file_record(role: str, logical_name: str, path: Path) -> DirectEvidenceArtifactFile:
    resolved = path.resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise DirectEvidencePublicationError(f"{role} input is absent or unsafe")
    return DirectEvidenceArtifactFile(
        role=role,
        logical_name=logical_name,
        sha256=sha256_file(resolved),
        byte_size=resolved.stat().st_size,
    )


def _read_verified_index(archive_path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            member = archive.getmember("evidence-index.json")
            if (
                not member.isfile()
                or member.issym()
                or member.islnk()
                or member.size <= 0
                or member.size > MAX_INDEX_BYTES
            ):
                raise DirectEvidencePublicationError("evidence index member is unsafe")
            stream = archive.extractfile(member)
            if stream is None:
                raise DirectEvidencePublicationError("evidence index cannot be read")
            payload = stream.read(MAX_INDEX_BYTES + 1)
    except (OSError, KeyError, tarfile.TarError) as exc:
        raise DirectEvidencePublicationError("verified evidence archive is unreadable") from exc
    if len(payload) > MAX_INDEX_BYTES:
        raise DirectEvidencePublicationError("evidence index exceeds publication limit")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DirectEvidencePublicationError("evidence index is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DirectEvidencePublicationError("evidence index root must be an object")
    return value


def _required_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectEvidencePublicationError(f"{label} must be an object")
    return value


def _project_tables(
    index: Mapping[str, Any], *, engine_manifest_sha256: str
) -> tuple[DirectEvidenceTable, DirectEvidenceTable]:
    jobs_value = index.get("jobs")
    if not isinstance(jobs_value, list) or len(jobs_value) != 160:
        raise DirectEvidencePublicationError("verified evidence index must contain 160 jobs")
    direct_rows: list[tuple[Any, ...]] = []
    resource_rows: list[tuple[Any, ...]] = []
    for value in jobs_value:
        job = _required_mapping(value, label="direct job")
        resource = _required_mapping(
            job.get("resource_observation"), label="resource observation"
        )
        coherence = _required_mapping(
            job.get("matrix_current_coherence"), label="matrix/current QA"
        )
        process_exit = _required_mapping(
            job.get("process_exit_evidence"), label="process exit evidence"
        )
        artifacts = job.get("artifacts")
        qa_findings = job.get("qa_findings")
        if (
            job.get("state") != "SUCCEEDED"
            or job.get("exact") is not True
            or job.get("qa_status") != "ACCEPTED"
            or qa_findings not in ([], ())
            or job.get("engine_manifest_sha256") != engine_manifest_sha256
            or process_exit.get("tree_exit_confirmed") is not True
            or resource.get("boundary") != "DIRECT_SOLVER"
            or not isinstance(artifacts, list)
            or not artifacts
        ):
            raise DirectEvidencePublicationError(
                "direct-job projection encountered nonaccepted or mismatched evidence"
            )
        direct_rows.append(
            (
                job.get("direct_run_id"),
                job.get("family"),
                job.get("portfolio_id"),
                job.get("scenario_id"),
                job.get("safeguard"),
                job.get("state"),
                job.get("exact"),
                job.get("qa_status"),
                len(qa_findings),
                job.get("engine_name"),
                job.get("engine_version"),
                job.get("engine_executable_sha256"),
                job.get("engine_manifest_sha256"),
                job.get("model_run_cache_key"),
                job.get("request_sha256"),
                job.get("result_sha256"),
                job.get("matrix_sha256"),
                job.get("current_map_sha256"),
                job.get("effective_configuration_sha256"),
                len(artifacts),
                coherence.get("qa_sha256"),
                True,
                job.get("producer_validator_sha256"),
            )
        )
        resource_rows.append(
            (
                job.get("direct_run_id"),
                resource.get("observation_id"),
                resource.get("boundary"),
                resource.get("solver_wall_seconds"),
                resource.get("peak_memory_status"),
                resource.get("peak_memory_bytes"),
                resource.get("peak_memory_method"),
                resource.get("peak_memory_sample_count"),
                resource.get("peak_memory_is_lower_bound"),
                resource.get("energy_status"),
                resource.get("energy_kwh"),
                resource.get("energy_method"),
                resource.get("energy_estimate_lower_kwh"),
                resource.get("energy_estimate_upper_kwh"),
                len(resource.get("assumptions") or []),
                len(resource.get("exclusions") or []),
                canonical_sha256(resource),
            )
        )
    direct_rows.sort(key=lambda row: str(row[0]))
    resource_rows.sort(key=lambda row: str(row[0]))
    return (
        DirectEvidenceTable(
            name="usefulness_direct_jobs",
            columns=DIRECT_JOB_COLUMNS,
            rows=tuple(direct_rows),
        ),
        DirectEvidenceTable(
            name="usefulness_direct_resource_qa",
            columns=RESOURCE_QA_COLUMNS,
            rows=tuple(resource_rows),
        ),
    )


def prepare_direct_evidence_publication(
    *,
    manifest_path: Path,
    archive_path: Path,
    engine_manifest_path: Path,
    engine_provenance_path: Path | None = None,
    engine_attestation_bundle_path: Path | None = None,
    engine_provenance_verifier: EngineProvenanceVerifier | None = None,
    expected_engine_sha256: str,
    repository_root: Path,
    upload_authorization: str,
    source_verifier: PushedSourceVerifier | None = None,
) -> DirectEvidencePublicationPlan:
    """Reverify exact local bytes and prepare a non-outcome W&B projection."""

    if upload_authorization != DIRECT_UPLOAD_AUTHORIZATION:
        raise PermissionError("exact public CC0 direct-evidence authorization is required")
    receipt: UsefulnessEvidencePackageReceipt = verify_usefulness_evidence_package(
        manifest_path=manifest_path,
        archive_path=archive_path,
        expected_engine_sha256=expected_engine_sha256,
        engine_manifest_path=engine_manifest_path,
        engine_provenance_path=engine_provenance_path,
        engine_attestation_bundle_path=engine_attestation_bundle_path,
        engine_provenance_verifier=engine_provenance_verifier,
    )
    if (
        not receipt.direct_evidence_complete
        or receipt.job_count != 160
        or receipt.qa_accepted_jobs != 160
        or not receipt.complete_target_and_safeguard_ensembles
        or receipt.outcome_promotion != OUTCOME_BLOCKER
        or not receipt.source_tree_clean
    ):
        raise DirectEvidencePublicationError(
            "verified package does not preserve the blocked direct-evidence boundary"
        )
    verifier = source_verifier or GitPushedSourceVerifier()
    pushed = verifier.verify(
        repository_root=repository_root,
        source_repository=receipt.source_repository,
        source_commit=receipt.source_commit,
    )
    if (
        pushed.source_repository != receipt.source_repository
        or pushed.source_commit != receipt.source_commit
    ):
        raise PermissionError(
            "pushed-source repository or commit differs from the verified package"
        )
    resolved_manifest = receipt.manifest_path.resolve()
    resolved_archive = receipt.archive_path.resolve()
    resolved_engine_manifest = engine_manifest_path.resolve()
    resolved_engine_provenance = receipt.engine_provenance_path.resolve()
    resolved_attestation_bundle = (
        receipt.engine_attestation_bundle_path.resolve()
        if receipt.engine_attestation_bundle_path is not None
        else None
    )
    package_manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    if (
        package_manifest.get("license") != "CC0-1.0"
        or package_manifest.get("usefulness_claims_promoted") is not False
        or package_manifest.get("release_eligible") is not False
        or package_manifest.get("outcome_promotion") != OUTCOME_BLOCKER
    ):
        raise PermissionError("only blocked, self-authored CC0 direct evidence may be uploaded")
    index = _read_verified_index(resolved_archive)
    if (
        index.get("evidence_index_content_sha256") is None
        or receipt.evidence_index_sha256 != sha256_file_from_archive_index(resolved_archive)
    ):
        raise DirectEvidencePublicationError("archive index changed after package verification")
    tables = _project_tables(index, engine_manifest_sha256=receipt.engine_manifest_sha256)
    archive_logical_name = resolved_archive.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", archive_logical_name):
        raise DirectEvidencePublicationError("direct-evidence archive filename is unsafe")
    file_list = [
        _file_record("PACKAGE_MANIFEST", "direct-evidence-manifest.json", resolved_manifest),
        _file_record("PACKAGE_ARCHIVE", archive_logical_name, resolved_archive),
        _file_record("ENGINE_MANIFEST", "engine-manifest.json", resolved_engine_manifest),
        _file_record("ENGINE_PROVENANCE", "engine-provenance.json", resolved_engine_provenance),
    ]
    if resolved_attestation_bundle is not None:
        file_list.append(
            _file_record(
                "ENGINE_ATTESTATION_BUNDLE",
                "engine-attestation-bundle.jsonl",
                resolved_attestation_bundle,
            )
        )
    files = tuple(file_list)
    by_role = {item.role: item for item in files}
    if (
        by_role["PACKAGE_MANIFEST"].sha256 != receipt.manifest_sha256
        or by_role["PACKAGE_ARCHIVE"].sha256 != receipt.archive_sha256
        or by_role["ENGINE_MANIFEST"].sha256
        != package_manifest.get("engine_manifest_file_sha256")
        or receipt.engine_manifest_sha256 != package_manifest.get("engine_manifest_sha256")
        or expected_engine_sha256 != package_manifest.get("engine_executable_sha256")
        or by_role["ENGINE_PROVENANCE"].sha256
        != receipt.engine_provenance_receipt_sha256
        or package_manifest.get("engine_trust_profile")
        != "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
        or package_manifest.get("engine_provenance_receipt_sha256")
        != receipt.engine_provenance_receipt_sha256
        or package_manifest.get("engine_attestation_verification_sha256")
        != receipt.engine_attestation_verification_sha256
    ):
        raise DirectEvidencePublicationError(
            "publication inputs differ from the exact verified engine/package identity"
        )
    artifact_manifest_sha256 = canonical_sha256(
        [item.model_dump(mode="json") for item in files]
    )
    run_id = (
        "usefulness-direct-"
        f"{receipt.manifest_sha256[:8]}{receipt.archive_sha256[:8]}"
        f"{receipt.engine_manifest_sha256[:8]}"
    )
    payload: dict[str, Any] = {
        "schema_version": DIRECT_PUBLICATION_SCHEMA,
        "upload_authorization": DIRECT_UPLOAD_AUTHORIZATION,
        "run_id": run_id,
        "run_name": "EcoCorridor five-landscape direct evidence",
        "run_kind": DIRECT_RUN_KIND,
        "artifact_name": f"ecocorridor-usefulness-direct-{artifact_manifest_sha256[:16]}",
        "artifact_type": DIRECT_ARTIFACT_TYPE,
        "license": "CC0-1.0",
        "source_repository": receipt.source_repository,
        "source_commit": receipt.source_commit,
        "source_upstream_ref": pushed.upstream_ref,
        "source_verifier": pushed.verifier,
        "pushed_source_receipt_sha256": pushed.receipt_sha256,
        "package_manifest_sha256": receipt.manifest_sha256,
        "package_archive_sha256": receipt.archive_sha256,
        "evidence_index_sha256": receipt.evidence_index_sha256,
        "engine_executable_sha256": expected_engine_sha256,
        "engine_manifest_sha256": receipt.engine_manifest_sha256,
        "engine_manifest_file_sha256": by_role["ENGINE_MANIFEST"].sha256,
        "engine_provenance_receipt_sha256": receipt.engine_provenance_receipt_sha256,
        "engine_attestation_verification_sha256": (
            receipt.engine_attestation_verification_sha256
        ),
        "artifact_files": files,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "direct_evidence_complete": True,
        "job_count": 160,
        "qa_accepted_jobs": 160,
        "outcome_analysis": "BLOCKED",
        "adjudication": "BLOCKED",
        "usefulness_claims": "BLOCKED",
        "outcome_promotion": OUTCOME_BLOCKER,
        "tables": tables,
        "manifest_path": resolved_manifest,
        "archive_path": resolved_archive,
        "engine_manifest_path": resolved_engine_manifest,
        "engine_provenance_path": resolved_engine_provenance,
        "engine_attestation_bundle_path": resolved_attestation_bundle,
    }
    draft = DirectEvidencePublicationPlan.model_construct(
        **payload, plan_sha256="0" * 64
    )
    payload["plan_sha256"] = canonical_sha256(
        draft.model_dump(mode="json", exclude={"plan_sha256"})
    )
    plan = DirectEvidencePublicationPlan.model_validate(payload)
    _verify_plan_files(plan)
    return plan


def sha256_file_from_archive_index(archive_path: Path) -> str:
    """Hash the exact retained index bytes without extracting the archive."""

    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            member = archive.getmember("evidence-index.json")
            if not member.isfile() or member.issym() or member.islnk():
                raise DirectEvidencePublicationError("evidence index member is unsafe")
            stream = archive.extractfile(member)
            if stream is None:
                raise DirectEvidencePublicationError("evidence index cannot be read")
            digest = hashlib.sha256()
            remaining = MAX_INDEX_BYTES + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining == 0 and stream.read(1):
                raise DirectEvidencePublicationError("evidence index exceeds publication limit")
            return digest.hexdigest()
    except (OSError, KeyError, tarfile.TarError) as exc:
        raise DirectEvidencePublicationError("verified evidence archive is unreadable") from exc


def _verify_plan_files(plan: DirectEvidencePublicationPlan) -> None:
    records = {item.logical_name: item for item in plan.artifact_files}
    paths = plan.artifact_paths
    if set(paths) != set(records):
        raise DirectEvidencePublicationError("publication paths differ from artifact manifest")
    for logical_name, path in paths.items():
        record = records[logical_name]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != record.byte_size
            or sha256_file(path) != record.sha256
        ):
            raise DirectEvidencePublicationError(
                "direct-evidence publication input changed after verification"
            )


class DirectEvidenceCloudReceipt(ContractModel):
    plan_sha256: Sha256Digest
    run_id: str = Field(min_length=1, max_length=128)
    run_url: str
    artifact_ref: str = Field(min_length=1, max_length=512)
    artifact_url: str
    artifact_manifest_sha256: Sha256Digest
    run_readback_verified: Literal[True] = True
    artifact_readback_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_urls(self) -> Self:
        _safe_https_url(self.run_url, label="W&B run URL")
        _safe_https_url(self.artifact_url, label="W&B artifact URL")
        return self


class DirectEvidencePublicationReceipt(ContractModel):
    schema_version: Literal[
        "ecocorridor-usefulness-direct-publication-receipt/v1"
    ] = "ecocorridor-usefulness-direct-publication-receipt/v1"
    status: Literal["COMPLETE_DIRECT_EVIDENCE_ONLY"] = "COMPLETE_DIRECT_EVIDENCE_ONLY"
    plan_sha256: Sha256Digest
    run_id: str
    run_url: str
    artifact_ref: str
    artifact_url: str
    artifact_manifest_sha256: Sha256Digest
    report_url: str
    run_readback_verified: Literal[True] = True
    artifact_readback_verified: Literal[True] = True
    report_readback_verified: Literal[True] = True
    direct_evidence_complete: Literal[True] = True
    direct_job_count: Literal[160] = 160
    ecological_outcome_value_count: Literal[0] = 0
    new_result_media_logged: Literal[False] = False
    outcome_analysis: Literal["BLOCKED"] = "BLOCKED"
    adjudication: Literal["BLOCKED"] = "BLOCKED"
    usefulness_claims: Literal["BLOCKED"] = "BLOCKED"
    receipt_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        for value, label in (
            (self.run_url, "W&B run URL"),
            (self.artifact_url, "W&B artifact URL"),
            (self.report_url, "W&B Report URL"),
        ):
            _safe_https_url(value, label=label)
        if self.receipt_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        ):
            raise ValueError("direct-evidence publication receipt digest differs")
        return self

    @classmethod
    def create(
        cls, cloud: DirectEvidenceCloudReceipt, *, report_url: str
    ) -> DirectEvidencePublicationReceipt:
        payload = {
            "schema_version": "ecocorridor-usefulness-direct-publication-receipt/v1",
            "status": "COMPLETE_DIRECT_EVIDENCE_ONLY",
            **cloud.model_dump(mode="json"),
            "report_url": report_url,
            "report_readback_verified": True,
            "direct_evidence_complete": True,
            "direct_job_count": 160,
            "ecological_outcome_value_count": 0,
            "new_result_media_logged": False,
            "outcome_analysis": "BLOCKED",
            "adjudication": "BLOCKED",
            "usefulness_claims": "BLOCKED",
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


class DirectEvidencePublisher(Protocol):
    def publish_plan(
        self, plan: DirectEvidencePublicationPlan
    ) -> DirectEvidenceCloudReceipt: ...

    def append_report_section(
        self,
        *,
        report_url: str,
        plan: DirectEvidencePublicationPlan,
        cloud: DirectEvidenceCloudReceipt,
    ) -> str: ...


def publish_verified_direct_evidence(
    *,
    plan: DirectEvidencePublicationPlan,
    report_url: str,
    publisher: DirectEvidencePublisher,
) -> DirectEvidencePublicationReceipt:
    """Publish and return only after artifact, run, and Report readback succeed."""

    _verify_plan_files(plan)
    cloud = publisher.publish_plan(plan)
    if (
        cloud.plan_sha256 != plan.plan_sha256
        or cloud.run_id != plan.run_id
        or cloud.artifact_manifest_sha256 != plan.artifact_manifest_sha256
        or not cloud.run_readback_verified
        or not cloud.artifact_readback_verified
    ):
        raise DirectEvidencePublicationError(
            "cloud receipt differs from the verified publication plan"
        )
    updated_report = publisher.append_report_section(
        report_url=report_url,
        plan=plan,
        cloud=cloud,
    )
    return DirectEvidencePublicationReceipt.create(cloud, report_url=updated_report)


def _report_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _report_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_report_value(nested) for nested in value]
    spec = getattr(value, "_spec", None)
    if isinstance(spec, Mapping):
        return _report_value(spec)
    public = getattr(value, "__dict__", None)
    if isinstance(public, Mapping):
        return {
            str(key): _report_value(nested)
            for key, nested in public.items()
            if not str(key).startswith("_")
        }
    raise TypeError("W&B Report block contains unsupported state")


def _block_representation(block: Any) -> dict[str, Any]:
    spec = getattr(block, "_spec", None)
    value = _report_value(spec) if isinstance(spec, Mapping) else _report_value(block)
    return {"block_type": type(block).__name__, "value": value}


def _block_text(block: Any) -> str | None:
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _section_marker(plan: DirectEvidencePublicationPlan) -> str:
    return (
        f"{DIRECT_SECTION_START} plan={plan.plan_sha256} "
        f"artifact={plan.artifact_manifest_sha256} -->"
    )


def build_direct_evidence_report_section(
    reports: Any,
    plan: DirectEvidencePublicationPlan,
    cloud: DirectEvidenceCloudReceipt,
) -> list[Any]:
    """Build a non-outcome, no-media section from verified package identities."""

    marker = _section_marker(plan)
    return [
        reports.MarkdownBlock(text=marker),
        reports.H1("EcoCorridor direct evidence — current verified package"),
        reports.CalloutBlock(
            text=(
                "DIRECT EVIDENCE COMPLETE. Outcome analysis, independent adjudication, "
                "and usefulness claims remain BLOCKED."
            )
        ),
        reports.MarkdownBlock(
            text=(
                "The five self-authored CC0 landscapes contain **160/160 direct "
                "Circuitscape jobs with accepted computational QA and complete target-and-"
                "safeguard ensembles**. This establishes execution and evidence-package "
                "completeness only. It does not report ecological outcomes, compare "
                "portfolios, adjudicate task truth, or demonstrate practitioner or agent "
                "usefulness.\n\n"
                f"Source commit `{plan.source_commit}` · engine manifest "
                f"`{plan.engine_manifest_sha256}` · package archive "
                f"`{plan.package_archive_sha256}`."
            )
        ),
        reports.H2("Audit trail and blocked claim boundary"),
        reports.MarkdownBlock(
            text=(
                f"[Open the deterministic W&B run]({cloud.run_url}) · immutable artifact "
                f"`{cloud.artifact_ref}` · artifact manifest "
                f"`{plan.artifact_manifest_sha256}`. The run contains two typed 160-row "
                "Tables for direct-job identity/QA and resource QA. No media and no "
                "ecological outcome values were logged. **Outcome analysis: BLOCKED · "
                "independent adjudication: BLOCKED · usefulness claims: BLOCKED.**"
            )
        ),
        reports.MarkdownBlock(text=DIRECT_SECTION_END),
    ]


def _direct_sections(blocks: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    sections: list[tuple[str, list[Any]]] = []
    marker: str | None = None
    body: list[Any] = []
    for block in blocks:
        text = _block_text(block)
        if text is not None and text.startswith(DIRECT_SECTION_START):
            if marker is not None:
                raise ValueError("W&B Report contains nested direct-evidence sections")
            marker = text
            body = []
            continue
        if text == DIRECT_SECTION_END:
            if marker is None:
                raise ValueError("W&B Report contains an unmatched direct-evidence marker")
            sections.append((marker, body))
            marker = None
            body = []
            continue
        if marker is not None:
            body.append(block)
    if marker is not None:
        raise ValueError("W&B Report contains an unterminated direct-evidence section")
    return sections


def append_direct_evidence_section(
    existing: Sequence[Any], owned: Sequence[Any]
) -> list[Any]:
    sections = _direct_sections(owned)
    if len(sections) != 1:
        raise ValueError("publisher must supply exactly one direct-evidence section")
    expected_marker = sections[0][0]
    current = _direct_sections(existing)
    if any(marker == expected_marker for marker, _ in current):
        return list(existing)
    if current:
        raise DirectEvidencePublicationError(
            "Report already contains a different current direct-evidence section"
        )
    return [*existing, *owned]


@contextmanager
def _wandb_base_environment(api_base_url: str):
    previous = os.environ.get("WANDB_BASE_URL")
    os.environ["WANDB_BASE_URL"] = api_base_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WANDB_BASE_URL", None)
        else:
            os.environ["WANDB_BASE_URL"] = previous


class WandbDirectEvidencePublisher:
    """Exact W&B run, Table, artifact, and append-only Report adapter."""

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        api_base_url: str,
        web_base_url: str,
        upload_authorization: str,
        wandb_module: Any | None = None,
        reports_module: Any | None = None,
    ) -> None:
        if upload_authorization != DIRECT_UPLOAD_AUTHORIZATION:
            raise PermissionError("exact public CC0 direct-evidence authorization is required")
        if wandb_module is None:
            import wandb as wandb_module  # type: ignore[import-not-found,no-redef]
        if reports_module is None:
            import wandb_workspaces.reports.v2 as reports_module  # type: ignore[import-not-found,no-redef]
        self.entity = entity
        self.project = project
        self.api_base_url = _safe_https_url(
            api_base_url, label="W&B API base URL"
        ).rstrip("/")
        self.web_base_url = _safe_https_url(
            web_base_url, label="W&B web base URL"
        ).rstrip("/")
        _expected_run_url(
            web_base_url=self.web_base_url,
            entity=entity,
            project=project,
            run_id="validation",
        )
        self.run_path = f"{entity}/{project}"
        self.wandb = wandb_module
        self.reports = reports_module

    def _api(self) -> Any:
        return self.wandb.Api(overrides={"base_url": self.api_base_url})

    @staticmethod
    def _artifact_metadata(plan: DirectEvidencePublicationPlan) -> dict[str, Any]:
        return {
            "schema_version": plan.schema_version,
            "plan_sha256": plan.plan_sha256,
            "upload_authorization": plan.upload_authorization,
            "artifact_manifest_sha256": plan.artifact_manifest_sha256,
            "package_manifest_sha256": plan.package_manifest_sha256,
            "package_archive_sha256": plan.package_archive_sha256,
            "evidence_index_sha256": plan.evidence_index_sha256,
            "engine_manifest_sha256": plan.engine_manifest_sha256,
            "engine_manifest_file_sha256": plan.engine_manifest_file_sha256,
            "engine_trust_profile": "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
            "engine_provenance_receipt_sha256": plan.engine_provenance_receipt_sha256,
            "engine_attestation_verification_sha256": (
                plan.engine_attestation_verification_sha256
            ),
            "source_commit": plan.source_commit,
            "pushed_source_receipt_sha256": plan.pushed_source_receipt_sha256,
            "license": "CC0-1.0",
            "data_classification": "CC0_SYNTHETIC",
            "direct_job_count": 160,
            "ecological_outcome_value_count": 0,
            "new_result_media_logged": False,
            "outcome_analysis": "BLOCKED",
            "adjudication": "BLOCKED",
            "usefulness_claims": "BLOCKED",
        }

    def _verify_artifact(
        self, plan: DirectEvidencePublicationPlan, *, artifact_ref: str
    ) -> str:
        artifact = self._api().artifact(artifact_ref)
        metadata = dict(getattr(artifact, "metadata", {}) or {})
        expected_metadata = self._artifact_metadata(plan)
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise DirectEvidencePublicationError(
                "W&B artifact metadata differs from the verified package"
            )
        artifact_url = _require_web_origin(
            getattr(artifact, "url", ""),
            web_base_url=self.web_base_url,
            label="W&B artifact URL",
        )
        records = {item.logical_name: item for item in plan.artifact_files}
        with tempfile.TemporaryDirectory(
            prefix="ecocorridor-direct-evidence-readback-"
        ) as temporary:
            downloaded = Path(artifact.download(root=temporary)).resolve()
            observed: set[str] = set()
            for path in downloaded.rglob("*"):
                if path.is_dir():
                    continue
                relative = path.relative_to(downloaded).as_posix()
                record = records.get(relative)
                if (
                    path.is_symlink()
                    or record is None
                    or path.stat().st_size != record.byte_size
                    or sha256_file(path) != record.sha256
                ):
                    raise DirectEvidencePublicationError(
                        "downloaded W&B artifact differs from exact local bytes"
                    )
                observed.add(relative)
            if observed != set(records):
                raise DirectEvidencePublicationError(
                    "downloaded W&B artifact file set is incomplete"
                )
        return artifact_url

    def _readback_run(
        self, plan: DirectEvidencePublicationPlan, *, artifact_ref: str
    ) -> tuple[str, Any]:
        expected_url = _expected_run_url(
            web_base_url=self.web_base_url,
            entity=self.entity,
            project=self.project,
            run_id=plan.run_id,
        )
        deadline = time.monotonic() + READBACK_TIMEOUT_SECONDS
        while True:
            try:
                run = self._api().run(f"{self.run_path}/{plan.run_id}")
                config = dict(getattr(run, "config", {}) or {})
                summary = dict(getattr(run, "summary", {}) or {})
                valid = (
                    str(getattr(run, "id", "")) == plan.run_id
                    and str(getattr(run, "state", "")).casefold() == "finished"
                    and _require_web_origin(
                        getattr(run, "url", ""),
                        web_base_url=self.web_base_url,
                        label="W&B run URL",
                    )
                    == expected_url
                    and all(config.get(key) == value for key, value in plan.run_config.items())
                    and all(
                        summary.get(key) == value
                        for key, value in plan.expected_summary(
                            artifact_ref=artifact_ref
                        ).items()
                    )
                )
                if valid:
                    return expected_url, run
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry hides remote content
                run = None
            if time.monotonic() >= deadline:
                raise DirectEvidencePublicationError(
                    "finished W&B direct-evidence run failed exact readback"
                )
            time.sleep(READBACK_INTERVAL_SECONDS)

    def _reuse_completed(
        self, plan: DirectEvidencePublicationPlan
    ) -> DirectEvidenceCloudReceipt | None:
        try:
            run = self._api().run(f"{self.run_path}/{plan.run_id}")
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise DirectEvidencePublicationError(
                "unable to resolve deterministic W&B run identity"
            ) from exc
        config = dict(getattr(run, "config", {}) or {})
        summary = dict(getattr(run, "summary", {}) or {})
        if (
            str(getattr(run, "state", "")).casefold() != "finished"
            or any(config.get(key) != value for key, value in plan.run_config.items())
        ):
            raise DirectEvidencePublicationError(
                "deterministic W&B run ID is occupied by nonmatching state"
            )
        artifact_ref = str(summary.get("direct_evidence/artifact_ref") or "")
        if not re.fullmatch(
            rf"{re.escape(self.entity)}/{re.escape(self.project)}/[^:\s]+:v[0-9]+",
            artifact_ref,
        ):
            raise DirectEvidencePublicationError(
                "completed W&B run lacks an immutable direct-evidence artifact"
            )
        artifact_url = self._verify_artifact(plan, artifact_ref=artifact_ref)
        run_url, _ = self._readback_run(plan, artifact_ref=artifact_ref)
        return DirectEvidenceCloudReceipt(
            plan_sha256=plan.plan_sha256,
            run_id=plan.run_id,
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=plan.artifact_manifest_sha256,
        )

    def publish_plan(
        self, plan: DirectEvidencePublicationPlan
    ) -> DirectEvidenceCloudReceipt:
        _verify_plan_files(plan)
        reused = self._reuse_completed(plan)
        if reused is not None:
            return reused
        expected_url = _expected_run_url(
            web_base_url=self.web_base_url,
            entity=self.entity,
            project=self.project,
            run_id=plan.run_id,
        )
        with tempfile.TemporaryDirectory(
            prefix="ecocorridor-direct-evidence-wandb-"
        ) as temporary:
            settings = self.wandb.Settings(
                base_url=self.api_base_url,
                console="off",
                disable_code=True,
                disable_git=True,
                x_disable_machine_info=True,
                x_disable_meta=True,
                x_save_requirements=False,
                silent=True,
            )
            run = self.wandb.init(
                entity=self.entity,
                project=self.project,
                id=plan.run_id,
                name=plan.run_name,
                job_type=plan.run_kind,
                tags=[
                    "usefulness_direct_evidence",
                    "cc0_synthetic",
                    "outcomes_blocked",
                    "no_media",
                ],
                config=plan.run_config,
                resume="never",
                save_code=False,
                dir=temporary,
                settings=settings,
            )
            artifact_ref = ""
            try:
                if (
                    _require_web_origin(
                        getattr(run, "url", ""),
                        web_base_url=self.web_base_url,
                        label="W&B run URL",
                    )
                    != expected_url
                ):
                    raise DirectEvidencePublicationError(
                        "W&B created a different deterministic run identity"
                    )
                run.log(
                    {
                        table.name: self.wandb.Table(
                            columns=list(table.columns),
                            data=[list(row) for row in table.rows],
                        )
                        for table in plan.tables
                    }
                )
                artifact = self.wandb.Artifact(
                    plan.artifact_name,
                    type=plan.artifact_type,
                    metadata=self._artifact_metadata(plan),
                )
                for logical_name, path in sorted(plan.artifact_paths.items()):
                    artifact.add_file(str(path), name=logical_name, policy="immutable")
                _verify_plan_files(plan)
                logged = run.log_artifact(artifact).wait()
                artifact_ref = str(getattr(logged, "qualified_name", "") or "")
                if not re.fullmatch(
                    rf"{re.escape(self.entity)}/{re.escape(self.project)}/[^:\s]+:v[0-9]+",
                    artifact_ref,
                ):
                    raise DirectEvidencePublicationError(
                        "W&B did not return an immutable artifact version"
                    )
                for key, value in plan.expected_summary(
                    artifact_ref=artifact_ref
                ).items():
                    run.summary[key] = value
                run.finish(exit_code=0)
            except Exception:
                run.finish(exit_code=1)
                raise
        artifact_url = self._verify_artifact(plan, artifact_ref=artifact_ref)
        run_url, _ = self._readback_run(plan, artifact_ref=artifact_ref)
        return DirectEvidenceCloudReceipt(
            plan_sha256=plan.plan_sha256,
            run_id=plan.run_id,
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=plan.artifact_manifest_sha256,
        )

    def append_report_section(
        self,
        *,
        report_url: str,
        plan: DirectEvidencePublicationPlan,
        cloud: DirectEvidenceCloudReceipt,
    ) -> str:
        target = _require_web_origin(
            report_url,
            web_base_url=self.web_base_url,
            label="existing W&B Report URL",
        )
        with _wandb_base_environment(self.api_base_url):
            report = self.reports.Report.from_url(target)
            if (
                str(getattr(report, "entity", "")) != self.entity
                or str(getattr(report, "project", "")) != self.project
            ):
                raise PermissionError("existing W&B Report belongs to another destination")
            existing = list(report.blocks)
            existing_digest = canonical_sha256(
                [_block_representation(block) for block in existing]
            )
            owned = build_direct_evidence_report_section(self.reports, plan, cloud)
            expected_marker, expected_body = _direct_sections(owned)[0]
            expected_body_sha256 = canonical_sha256(
                [_block_representation(block) for block in expected_body]
            )
            appended = append_direct_evidence_section(existing, owned)
            if len(appended) != len(existing):
                report.blocks = appended
                report.save()
            updated_url = _require_web_origin(
                getattr(report, "url", ""),
                web_base_url=self.web_base_url,
                label="updated W&B Report URL",
            )
            deadline = time.monotonic() + READBACK_TIMEOUT_SECONDS
            while True:
                try:
                    readback = self.reports.Report.from_url(updated_url)
                    if (
                        str(getattr(readback, "entity", "")) != self.entity
                        or str(getattr(readback, "project", "")) != self.project
                    ):
                        raise PermissionError(
                            "read-back W&B Report belongs to another destination"
                        )
                    blocks = list(readback.blocks)
                    sections = _direct_sections(blocks)
                    matches = [body for marker, body in sections if marker == expected_marker]
                    preserved = (
                        canonical_sha256(
                            [
                                _block_representation(block)
                                for block in blocks[: len(existing)]
                            ]
                        )
                        == existing_digest
                    )
                    verified = bool(
                        preserved
                        and len(matches) == 1
                        and canonical_sha256(
                            [_block_representation(block) for block in matches[0]]
                        )
                        == expected_body_sha256
                    )
                    if verified:
                        return updated_url
                except PermissionError:
                    raise
                except Exception:  # noqa: BLE001 - bounded retry hides remote content
                    readback = None
                if time.monotonic() >= deadline:
                    raise DirectEvidencePublicationError(
                        "W&B Report failed exact append-only section readback"
                    )
                time.sleep(READBACK_INTERVAL_SECONDS)


__all__ = [
    "DIRECT_ARTIFACT_TYPE",
    "DIRECT_PUBLICATION_SCHEMA",
    "DIRECT_RUN_KIND",
    "DIRECT_SECTION_ID",
    "DIRECT_UPLOAD_AUTHORIZATION",
    "DirectEvidenceArtifactFile",
    "DirectEvidenceCloudReceipt",
    "DirectEvidencePublicationError",
    "DirectEvidencePublicationPlan",
    "DirectEvidencePublicationReceipt",
    "DirectEvidencePublisher",
    "DirectEvidenceTable",
    "GitPushedSourceVerifier",
    "PushedSourceReceipt",
    "PushedSourceVerifier",
    "WandbDirectEvidencePublisher",
    "append_direct_evidence_section",
    "build_direct_evidence_report_section",
    "prepare_direct_evidence_publication",
    "publish_verified_direct_evidence",
]
