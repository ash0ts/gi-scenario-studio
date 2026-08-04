from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "validate_ridgeway_producer_attestation.py"
    )
    spec = importlib.util.spec_from_file_location("ridgeway_producer_attestation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _interval_overlap(maximum: int) -> dict[str, object]:
    return {
        "schema_version": "ridgeway-retained-process-interval-overlap/v1.0",
        "observation_method": "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS",
        "maximum_overlapping_retained_process_intervals": maximum,
        "retained_interval_count": 1_596,
        "coincident_endpoint_policy": ("COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION"),
        "scientific_identity_role": "AUDIT_ONLY_EXCLUDED",
        "limitation": (
            "Derived only from retained post-spawn launched_at-to-confirmed_at measurement "
            "intervals. It may understate true process concurrency because launched_at is "
            "recorded after process creation, and coincident endpoints may overstate interval "
            "overlap at retained timestamp resolution. It is not the configured worker count, "
            "CPU utilization, or proof of simultaneous physical execution."
        ),
    }


def _release_package(root: Path, *, concurrency: int = 4) -> tuple[Path, Path]:
    publish = root / "publish"
    desktop = root / "desktop" / "ridgewayFixture.json"
    publish.mkdir(parents=True)
    desktop.parent.mkdir(parents=True)
    archive = publish / "ridgeway-direct-evidence-deadbeef.tar.gz"
    archive.write_bytes(b"authenticated only by an external producer artifact")
    direct = {
        "schema_version": "ridgeway-direct-evidence/v1.2",
        "release_replay_eligible": True,
        "matrix_job_count": 1_536,
        "map_job_count": 60,
        "engine_manifest_sha256": "1" * 64,
        "workflow_manifest_sha256": "2" * 64,
        "evidence_index_sha256": "3" * 64,
        "retained_process_interval_overlap": _interval_overlap(concurrency),
        "evidence_bundle": {
            "format": "tar+gzip",
            "filename": archive.name,
            "sha256": _sha(archive.read_bytes()),
            "byte_size": archive.stat().st_size,
            "file_count": 14_451,
        },
    }
    direct_path = publish / "direct-evidence-manifest.json"
    direct_path.write_text(json.dumps(direct), encoding="utf-8")
    direct_sha = _sha(direct_path.read_bytes())
    projection = {
        "schemaVersion": "ecocorridor-replay/v1.2",
        "fixtureId": "ridgeway_crossing",
        "evidenceState": "VERIFIED_DIRECT_EVIDENCE",
        "releaseReplayEligible": True,
        "manifestSha256": direct_sha,
    }
    payload = json.dumps(projection).encode()
    (publish / "ui_projection.json").write_bytes(payload)
    desktop.write_bytes(payload)
    desktop.with_name("ridgewayReleaseTrust.json").write_text(
        json.dumps(
            {
                "schemaVersion": "ecocorridor-release-trust/v1.0",
                "fixtureId": "ridgeway_crossing",
                "trusted": True,
                "evidenceManifestSha256": direct_sha,
            }
        ),
        encoding="utf-8",
    )
    return publish, desktop


def _differential(root: Path, *, source: str, reference: Path, producer: Path) -> Path:
    reference_manifest = reference / "direct-evidence-manifest.json"
    producer_manifest = producer / "direct-evidence-manifest.json"
    reference_direct = json.loads(reference_manifest.read_text(encoding="utf-8"))
    producer_direct = json.loads(producer_manifest.read_text(encoding="utf-8"))
    path = root / "ridgeway-protected-rerun-differential.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "ridgeway-protected-rerun-differential/v1.1",
                "source_commit_sha": source,
                "status": "PASSED",
                "reference_manifest_sha256": _sha(reference_manifest.read_bytes()),
                "producer_manifest_sha256": _sha(producer_manifest.read_bytes()),
                "reference_archive_sha256": reference_direct["evidence_bundle"]["sha256"],
                "producer_archive_sha256": producer_direct["evidence_bundle"]["sha256"],
                "matrix_job_count": 1_536,
                "map_job_count": 60,
                "teaching_selection_matched": True,
                "reference_retained_process_interval_overlap": reference_direct[
                    "retained_process_interval_overlap"
                ],
                "producer_retained_process_interval_overlap": producer_direct[
                    "retained_process_interval_overlap"
                ],
                "retained_process_interval_overlap_comparison_policy": (
                    "AUDIT_ONLY_EXCLUDED_FROM_SCIENTIFIC_EQUIVALENCE"
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_attestation_binds_every_promoted_evidence_asset(
    validator: ModuleType, tmp_path: Path
) -> None:
    publish, desktop = _release_package(tmp_path)
    producer, _ = _release_package(tmp_path / "producer", concurrency=8)
    source = "a" * 40
    differential = _differential(tmp_path, source=source, reference=publish, producer=producer)
    attestation = validator.derive_attestation(
        source_commit=source,
        publish_directory=publish,
        desktop_fixture=desktop,
        producer_publish_directory=producer,
        differential_report=differential,
    )

    assert set(attestation) == validator.EXPECTED_KEYS
    assert attestation["source_commit_sha"] == source
    assert attestation["execution_contract"] == ("FULL_DIRECT_CIRCUITSCAPE_RASTER_PAIRWISE_RERUN")
    assert attestation["matrix_job_count"] == 1_536
    assert attestation["map_job_count"] == 60
    assert (
        validator.verify_attestation(
            attestation,
            source_commit=source,
            publish_directory=publish,
            desktop_fixture=desktop,
            producer_publish_directory=producer,
            differential_report=differential,
        )
        == attestation
    )


def test_attestation_rejects_source_or_evidence_substitution(
    validator: ModuleType, tmp_path: Path
) -> None:
    publish, desktop = _release_package(tmp_path)
    producer, _ = _release_package(tmp_path / "producer")
    differential = _differential(tmp_path, source="a" * 40, reference=publish, producer=producer)
    attestation = validator.derive_attestation(
        source_commit="a" * 40,
        publish_directory=publish,
        desktop_fixture=desktop,
        producer_publish_directory=producer,
        differential_report=differential,
    )

    with pytest.raises(ValueError, match="not eligible|differs from exact source evidence"):
        validator.verify_attestation(
            attestation,
            source_commit="b" * 40,
            publish_directory=publish,
            desktop_fixture=desktop,
            producer_publish_directory=producer,
            differential_report=differential,
        )

    desktop.write_bytes(desktop.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="projections differ"):
        validator.verify_attestation(
            attestation,
            source_commit="a" * 40,
            publish_directory=publish,
            desktop_fixture=desktop,
            producer_publish_directory=producer,
            differential_report=differential,
        )


def test_historical_or_self_declared_evidence_cannot_receive_attestation(
    validator: ModuleType, tmp_path: Path
) -> None:
    publish, desktop = _release_package(tmp_path)
    producer, _ = _release_package(tmp_path / "producer")
    differential = _differential(tmp_path, source="a" * 40, reference=publish, producer=producer)
    path = publish / "direct-evidence-manifest.json"
    direct = json.loads(path.read_text(encoding="utf-8"))
    direct["historical_evidence_only"] = True
    direct["evidence_contract_status"] = "SUPERSEDED_NON_RELEASE"
    path.write_text(json.dumps(direct), encoding="utf-8")

    with pytest.raises(ValueError, match="not eligible"):
        validator.derive_attestation(
            source_commit="a" * 40,
            publish_directory=publish,
            desktop_fixture=desktop,
            producer_publish_directory=producer,
            differential_report=differential,
        )


def test_attestation_rejects_impossible_retained_interval_overlap(
    validator: ModuleType,
) -> None:
    impossible = _interval_overlap(1_597)

    with pytest.raises(ValueError, match="retained process-interval overlap is invalid"):
        validator._retained_process_interval_overlap(impossible, label="test")
