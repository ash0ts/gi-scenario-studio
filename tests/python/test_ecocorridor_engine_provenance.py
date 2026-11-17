from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.engine_provenance import (
    EngineBuildProvenanceReceipt,
    EngineConformanceCase,
    EngineProvenanceError,
    GithubAttestedEngineProvenanceVerifier,
)


def _case(case_id: str) -> EngineConformanceCase:
    official = case_id == "official_circuitscape_sgverify4"
    expected = None if official else (4.0 if "series" in case_id else 1.0)
    payload = {
        "case_id": case_id,
        "authority": (
            "CIRCUITSCAPE_5_17_1_OFFICIAL_FIXTURE"
            if official
            else "HAND_CALCULATED_ANALYTIC"
        ),
        "exact_engine_execution": True,
        "passed": True,
        "pair_count": 15 if official else 1,
        "expected_effective_resistance": expected,
        "observed_effective_resistance": expected,
        "absolute_tolerance": 5.0e-8 if official else 1.0e-8,
        "maximum_absolute_error": 0.0,
        "expected_metrics_sha256": "1" * 64,
        "observed_metrics_sha256": "2" * 64,
        "artifact_sha256": {
            role: f"{index:x}" * 64
            for index, role in enumerate(
                ("resistance", "focal", "included_pairs", "request", "result", "matrix"),
                start=3,
            )
        },
    }
    return EngineConformanceCase(**payload, case_sha256=canonical_sha256(payload))


def _receipt(path: Path) -> EngineBuildProvenanceReceipt:
    cases = tuple(
        sorted(
            (
                _case("hand_calculated_series_four_ohm"),
                _case("hand_calculated_parallel_one_ohm"),
                _case("official_circuitscape_sgverify4"),
            ),
            key=lambda item: item.case_id,
        )
    )
    payload = {
        "schema_version": "ecocorridor-engine-provenance/v1.0",
        "trust_profile": "PROTECTED_GITHUB_ATTESTED_SIGNED_RELEASE",
        "test_only": False,
        "publishable": True,
        "source_repository": "https://github.com/ash0ts/gi-scenario-studio",
        "source_commit": "a" * 40,
        "source_ref": "refs/heads/main",
        "producer_workflow": ".github/workflows/ecocorridor-release.yml",
        "producer_run_id": 42,
        "producer_run_attempt": 1,
        "producer_url": "https://github.com/ash0ts/gi-scenario-studio/actions/runs/42",
        "release_version": "0.1.0",
        "target": "macos-arm64",
        "engine_name": "Circuitscape",
        "engine_version": "5.17.1",
        "julia_version": "1.12.6",
        "engine_executable_sha256": "e" * 64,
        "engine_manifest_sha256": "b" * 64,
        "engine_manifest_file_sha256": "c" * 64,
        "release_manifest_sha256": "d" * 64,
        "release_manifest_signature_sha256": "f" * 64,
        "release_resource_public_key_sha256": "9" * 64,
        "conformance_cases": [item.model_dump(mode="json") for item in cases],
        "conformance_suite_sha256": canonical_sha256(
            [item.model_dump(mode="json") for item in cases]
        ),
    }
    receipt = EngineBuildProvenanceReceipt(
        **payload, receipt_sha256=canonical_sha256(payload)
    )
    path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return receipt


def test_exact_receipt_requires_independent_github_attestation(tmp_path: Path) -> None:
    path = tmp_path / "engine-provenance.json"
    receipt = _receipt(path)
    verifier = GithubAttestedEngineProvenanceVerifier(
        runner=lambda _arguments: (_ for _ in ()).throw(RuntimeError("no attestation"))
    )
    with pytest.raises(RuntimeError, match="no attestation"):
        verifier.verify(
            receipt_path=path,
            attestation_bundle_path=None,
            expected_engine_sha256=receipt.engine_executable_sha256,
            expected_engine_manifest_sha256=receipt.engine_manifest_sha256,
            expected_engine_manifest_file_sha256=receipt.engine_manifest_file_sha256,
        )


def test_verified_attestation_must_name_exact_receipt_subject(tmp_path: Path) -> None:
    path = tmp_path / "engine-provenance.json"
    receipt = _receipt(path)
    wrong = json.dumps([{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": "0" * 64}}]}}}])
    with pytest.raises(EngineProvenanceError, match="does not bind"):
        GithubAttestedEngineProvenanceVerifier(runner=lambda _arguments: wrong).verify(
            receipt_path=path,
            attestation_bundle_path=None,
            expected_engine_sha256=receipt.engine_executable_sha256,
            expected_engine_manifest_sha256=receipt.engine_manifest_sha256,
            expected_engine_manifest_file_sha256=receipt.engine_manifest_file_sha256,
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output = json.dumps(
        [{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": digest}}]}}}]
    )
    verified = GithubAttestedEngineProvenanceVerifier(
        runner=lambda arguments: output
    ).verify(
        receipt_path=path,
        attestation_bundle_path=None,
        expected_engine_sha256=receipt.engine_executable_sha256,
        expected_engine_manifest_sha256=receipt.engine_manifest_sha256,
        expected_engine_manifest_file_sha256=receipt.engine_manifest_file_sha256,
    )
    assert verified.receipt_file_sha256 == digest
    assert verified.attestation.statement_subject_sha256 == digest
