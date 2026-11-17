"""Cryptographic provenance gate for reportable Circuitscape evidence.

An ``EngineManifest`` proves only that a set of hashes is internally
consistent.  It does not prove who built the executable or that the executable
passed the project's conformance suite.  This module adds that missing trust
boundary.  Production evidence requires a receipt emitted by the protected
signed-release workflow and a GitHub/Sigstore attestation over the exact receipt
bytes.  Local and fake engines remain useful for tests, but are deliberately
non-publishable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from .contracts import ContractModel, Sha256Digest, canonical_sha256
from .engine import sha256_file

ENGINE_PROVENANCE_SCHEMA = "ecocorridor-engine-provenance/v1.0"
ENGINE_ATTESTATION_SCHEMA = "ecocorridor-engine-attestation-verification/v1.0"
ENGINE_PROVENANCE_REPOSITORY = "https://github.com/ash0ts/gi-scenario-studio"
ENGINE_PROVENANCE_REPOSITORY_SLUG = "ash0ts/gi-scenario-studio"
ENGINE_PROVENANCE_WORKFLOW = ".github/workflows/ecocorridor-release.yml"
ENGINE_PROVENANCE_SIGNER = (
    "github.com/ash0ts/gi-scenario-studio/.github/workflows/ecocorridor-release.yml"
)
MAX_PROVENANCE_BYTES = 2 * 1024 * 1024
MAX_ATTESTATION_BYTES = 16 * 1024 * 1024

_REQUIRED_CASES = {
    "hand_calculated_series_four_ohm": (
        "HAND_CALCULATED_ANALYTIC",
        1,
        4.0,
        1.0e-8,
    ),
    "hand_calculated_parallel_one_ohm": (
        "HAND_CALCULATED_ANALYTIC",
        1,
        1.0,
        1.0e-8,
    ),
    "official_circuitscape_sgverify4": (
        "CIRCUITSCAPE_5_17_1_OFFICIAL_FIXTURE",
        15,
        None,
        5.0e-8,
    ),
}


class EngineProvenanceError(RuntimeError):
    """The engine provenance or its independent attestation failed closed."""


class EngineConformanceCase(ContractModel):
    case_id: Literal[
        "hand_calculated_series_four_ohm",
        "hand_calculated_parallel_one_ohm",
        "official_circuitscape_sgverify4",
    ]
    authority: Literal[
        "HAND_CALCULATED_ANALYTIC",
        "CIRCUITSCAPE_5_17_1_OFFICIAL_FIXTURE",
    ]
    exact_engine_execution: Literal[True] = True
    passed: Literal[True] = True
    pair_count: int = Field(gt=0)
    expected_effective_resistance: float | None = Field(default=None, gt=0)
    observed_effective_resistance: float | None = Field(default=None, gt=0)
    absolute_tolerance: float = Field(gt=0)
    maximum_absolute_error: float = Field(ge=0)
    expected_metrics_sha256: Sha256Digest
    observed_metrics_sha256: Sha256Digest
    artifact_sha256: dict[str, Sha256Digest]
    case_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_conformance_case(self) -> Self:
        authority, pair_count, expected_scalar, tolerance = _REQUIRED_CASES[
            self.case_id
        ]
        required_artifacts = {
            "resistance",
            "focal",
            "included_pairs",
            "request",
            "result",
            "matrix",
        }
        if (
            self.authority != authority
            or self.pair_count != pair_count
            or not math.isclose(self.absolute_tolerance, tolerance, rel_tol=0, abs_tol=0)
            or self.maximum_absolute_error > self.absolute_tolerance
            or set(self.artifact_sha256) != required_artifacts
        ):
            raise ValueError("engine conformance case differs from the frozen suite")
        if expected_scalar is None:
            if (
                self.expected_effective_resistance is not None
                or self.observed_effective_resistance is not None
            ):
                raise ValueError("official fixture conformance is a multi-pair result")
        elif (
            self.expected_effective_resistance != expected_scalar
            or self.observed_effective_resistance is None
            or not math.isclose(
                self.observed_effective_resistance,
                expected_scalar,
                rel_tol=0,
                abs_tol=self.absolute_tolerance,
            )
        ):
            raise ValueError("hand-calculated conformance value differs")
        expected_digest = canonical_sha256(
            self.model_dump(mode="json", exclude={"case_sha256"})
        )
        if self.case_sha256 != expected_digest:
            raise ValueError("engine conformance case digest differs")
        return self


class EngineBuildProvenanceReceipt(ContractModel):
    schema_version: Literal["ecocorridor-engine-provenance/v1.0"] = (
        ENGINE_PROVENANCE_SCHEMA
    )
    trust_profile: Literal["PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"] = (
        "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE"
    )
    test_only: Literal[False] = False
    publishable: Literal[True] = True
    source_repository: Literal[
        "https://github.com/ash0ts/gi-scenario-studio"
    ] = ENGINE_PROVENANCE_REPOSITORY
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_ref: Literal["refs/heads/main"] = "refs/heads/main"
    producer_workflow: Literal[".github/workflows/ecocorridor-release.yml"] = (
        ENGINE_PROVENANCE_WORKFLOW
    )
    producer_run_id: int = Field(gt=0)
    producer_run_attempt: int = Field(gt=0)
    producer_url: str
    release_version: str = Field(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
    target: Literal["macos-arm64", "macos-x64", "windows-x64"]
    engine_name: Literal["Circuitscape"] = "Circuitscape"
    engine_version: Literal["5.17.1"] = "5.17.1"
    julia_version: Literal["1.12.6"] = "1.12.6"
    engine_executable_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    engine_manifest_file_sha256: Sha256Digest
    release_manifest_sha256: Sha256Digest
    release_manifest_signature_sha256: Sha256Digest
    release_resource_public_key_sha256: Sha256Digest
    conformance_cases: tuple[EngineConformanceCase, ...] = Field(
        min_length=3, max_length=3
    )
    conformance_suite_sha256: Sha256Digest
    receipt_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_production_receipt(self) -> Self:
        expected_url = (
            "https://github.com/ash0ts/gi-scenario-studio/actions/runs/"
            f"{self.producer_run_id}"
        )
        if self.producer_run_attempt > 1:
            accepted_urls = {
                expected_url,
                f"{expected_url}/attempts/{self.producer_run_attempt}",
            }
        else:
            accepted_urls = {expected_url}
        if self.producer_url not in accepted_urls:
            raise ValueError("engine provenance producer URL is not canonical")
        ordered = tuple(sorted(self.conformance_cases, key=lambda item: item.case_id))
        if self.conformance_cases != ordered or {
            item.case_id for item in ordered
        } != set(_REQUIRED_CASES):
            raise ValueError("engine provenance conformance suite is incomplete")
        cases_payload = [item.model_dump(mode="json") for item in ordered]
        if self.conformance_suite_sha256 != canonical_sha256(cases_payload):
            raise ValueError("engine provenance conformance suite digest differs")
        if self.receipt_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        ):
            raise ValueError("engine provenance receipt digest differs")
        return self


class EngineAttestationVerification(ContractModel):
    schema_version: Literal[
        "ecocorridor-engine-attestation-verification/v1.0"
    ] = ENGINE_ATTESTATION_SCHEMA
    verifier: Literal["GH_SIGSTORE_ATTESTATION_VERIFY"] = (
        "GH_SIGSTORE_ATTESTATION_VERIFY"
    )
    repository: Literal["ash0ts/gi-scenario-studio"] = (
        ENGINE_PROVENANCE_REPOSITORY_SLUG
    )
    signer_workflow: Literal[
        "github.com/ash0ts/gi-scenario-studio/.github/workflows/ecocorridor-release.yml"
    ] = ENGINE_PROVENANCE_SIGNER
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_ref: Literal["refs/heads/main"] = "refs/heads/main"
    receipt_file_sha256: Sha256Digest
    attestation_bundle_sha256: Sha256Digest | None = None
    verification_output_sha256: Sha256Digest
    statement_subject_sha256: Sha256Digest
    verified: Literal[True] = True
    verification_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_verification(self) -> Self:
        if self.receipt_file_sha256 != self.statement_subject_sha256:
            raise ValueError("attested subject differs from the provenance receipt")
        if self.verification_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"verification_sha256"})
        ):
            raise ValueError("engine attestation verification digest differs")
        return self


class VerifiedEngineProvenance(ContractModel):
    receipt: EngineBuildProvenanceReceipt
    receipt_path: Path = Field(exclude=True, repr=False)
    receipt_file_sha256: Sha256Digest
    attestation_bundle_path: Path | None = Field(default=None, exclude=True, repr=False)
    attestation: EngineAttestationVerification
    verification_output: bytes = Field(exclude=True, repr=False)


AttestationRunner = Callable[[Sequence[str]], str]


def _default_attestation_runner(arguments: Sequence[str]) -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise EngineProvenanceError(
            "GitHub CLI is required to verify production engine provenance"
        )
    command = [executable, *arguments]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
            env=environment,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineProvenanceError(
            "GitHub/Sigstore verification rejected the engine provenance receipt"
        ) from exc


def _regular_bounded(path: Path, *, label: str, maximum: int) -> Path:
    supplied = path.expanduser()
    try:
        metadata = os.lstat(supplied)
        resolved = supplied.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EngineProvenanceError(f"{label} is absent") from exc
    if supplied.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise EngineProvenanceError(f"{label} must be a regular non-symlink file")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise EngineProvenanceError(f"{label} violates its byte limit")
    return resolved


def _subject_digests(value: object) -> set[str]:
    observed: set[str] = set()
    if isinstance(value, Mapping):
        digest = value.get("digest")
        if isinstance(digest, Mapping):
            candidate = digest.get("sha256")
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
                observed.add(candidate)
        for nested in value.values():
            observed.update(_subject_digests(nested))
    elif isinstance(value, list):
        for nested in value:
            observed.update(_subject_digests(nested))
    return observed


class GithubAttestedEngineProvenanceVerifier:
    """Verify the exact receipt with GitHub's Sigstore trust root and identity."""

    def __init__(self, *, runner: AttestationRunner = _default_attestation_runner) -> None:
        self._runner = runner

    def verify(
        self,
        *,
        receipt_path: Path,
        attestation_bundle_path: Path | None,
        expected_engine_sha256: str,
        expected_engine_manifest_sha256: str,
        expected_engine_manifest_file_sha256: str,
        expected_source_repository: str | None = None,
        expected_source_commit: str | None = None,
    ) -> VerifiedEngineProvenance:
        receipt_file = _regular_bounded(
            receipt_path,
            label="engine provenance receipt",
            maximum=MAX_PROVENANCE_BYTES,
        )
        try:
            receipt = EngineBuildProvenanceReceipt.model_validate_json(
                receipt_file.read_bytes()
            )
        except ValueError as exc:
            raise EngineProvenanceError("engine provenance receipt is invalid") from exc
        receipt_file_sha = sha256_file(receipt_file)
        if (
            receipt.engine_executable_sha256 != expected_engine_sha256
            or receipt.engine_manifest_sha256 != expected_engine_manifest_sha256
            or receipt.engine_manifest_file_sha256
            != expected_engine_manifest_file_sha256
            or (
                expected_source_repository is not None
                and receipt.source_repository != expected_source_repository
            )
            or (
                expected_source_commit is not None
                and receipt.source_commit != expected_source_commit
            )
        ):
            raise EngineProvenanceError(
                "engine provenance receipt belongs to another engine or source"
            )
        bundle: Path | None = None
        arguments = [
            "attestation",
            "verify",
            os.fspath(receipt_file),
            "--repo",
            ENGINE_PROVENANCE_REPOSITORY_SLUG,
            "--signer-workflow",
            ENGINE_PROVENANCE_SIGNER,
            "--source-digest",
            receipt.source_commit,
            "--source-ref",
            receipt.source_ref,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ]
        if attestation_bundle_path is not None:
            bundle = _regular_bounded(
                attestation_bundle_path,
                label="engine provenance attestation bundle",
                maximum=MAX_ATTESTATION_BYTES,
            )
            arguments.extend(("--bundle", os.fspath(bundle)))
        output = self._runner(tuple(arguments))
        try:
            verified_output = json.loads(output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise EngineProvenanceError(
                "GitHub attestation verifier returned invalid JSON"
            ) from exc
        if not isinstance(verified_output, list) or not verified_output:
            raise EngineProvenanceError("engine provenance has no verified attestation")
        subjects = _subject_digests(verified_output)
        if receipt_file_sha not in subjects:
            raise EngineProvenanceError(
                "verified attestation does not bind the exact provenance receipt"
            )
        payload = {
            "schema_version": ENGINE_ATTESTATION_SCHEMA,
            "verifier": "GH_SIGSTORE_ATTESTATION_VERIFY",
            "repository": ENGINE_PROVENANCE_REPOSITORY_SLUG,
            "signer_workflow": ENGINE_PROVENANCE_SIGNER,
            "source_commit": receipt.source_commit,
            "source_ref": receipt.source_ref,
            "receipt_file_sha256": receipt_file_sha,
            "attestation_bundle_sha256": sha256_file(bundle) if bundle else None,
            "verification_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "statement_subject_sha256": receipt_file_sha,
            "verified": True,
        }
        attestation = EngineAttestationVerification(
            **payload,
            verification_sha256=canonical_sha256(payload),
        )
        return VerifiedEngineProvenance(
            receipt=receipt,
            receipt_path=receipt_file,
            receipt_file_sha256=receipt_file_sha,
            attestation_bundle_path=bundle,
            attestation=attestation,
            verification_output=output.encode("utf-8"),
        )


class EngineProvenanceVerifier(Protocol):
    def verify(
        self,
        *,
        receipt_path: Path,
        attestation_bundle_path: Path | None,
        expected_engine_sha256: str,
        expected_engine_manifest_sha256: str,
        expected_engine_manifest_file_sha256: str,
        expected_source_repository: str | None = None,
        expected_source_commit: str | None = None,
    ) -> VerifiedEngineProvenance: ...


__all__ = [
    "ENGINE_ATTESTATION_SCHEMA",
    "ENGINE_PROVENANCE_SCHEMA",
    "EngineAttestationVerification",
    "EngineBuildProvenanceReceipt",
    "EngineConformanceCase",
    "EngineProvenanceError",
    "EngineProvenanceVerifier",
    "GithubAttestedEngineProvenanceVerifier",
    "VerifiedEngineProvenance",
]
