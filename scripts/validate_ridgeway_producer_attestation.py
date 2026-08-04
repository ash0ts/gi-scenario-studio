#!/usr/bin/env python3
"""Create or verify the protected producer receipt for Ridgeway direct evidence.

The receipt is intentionally small.  It is useful only when it is carried in a
GitHub Actions artifact whose producer identity is independently resolved from
the Actions API.  Local semantic consistency is necessary, but is not treated
as proof that Circuitscape executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "ridgeway-protected-producer-attestation/v1.0"
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PROCESS_INTERVAL_OVERLAP_KEYS = frozenset(
    {
        "schema_version",
        "observation_method",
        "maximum_overlapping_retained_process_intervals",
        "retained_interval_count",
        "coincident_endpoint_policy",
        "scientific_identity_role",
        "limitation",
    }
)
PROCESS_INTERVAL_OVERLAP_LIMITATION = (
    "Derived only from retained post-spawn launched_at-to-confirmed_at measurement "
    "intervals. It may understate true process concurrency because launched_at is "
    "recorded after process creation, and coincident endpoints may overstate interval "
    "overlap at retained timestamp resolution. It is not the configured worker count, "
    "CPU utilization, or proof of simultaneous physical execution."
)
EXPECTED_KEYS = frozenset(
    {
        "schema_version",
        "source_commit_sha",
        "execution_contract",
        "fixture_id",
        "evidence_disposition",
        "direct_evidence_manifest_sha256",
        "ui_projection_sha256",
        "desktop_fixture_sha256",
        "release_trust_sha256",
        "archive_filename",
        "archive_sha256",
        "archive_byte_size",
        "archive_file_count",
        "producer_direct_evidence_manifest_sha256",
        "producer_archive_sha256",
        "producer_engine_manifest_sha256",
        "producer_workflow_manifest_sha256",
        "protected_rerun_differential_sha256",
        "engine_manifest_sha256",
        "workflow_manifest_sha256",
        "evidence_index_sha256",
        "matrix_job_count",
        "map_job_count",
    }
)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _regular(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _object(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(_regular(path, label=label).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object")
    return value


def _strict_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _retained_process_interval_overlap(value: Any, *, label: str) -> dict[str, Any]:
    maximum = (
        value.get("maximum_overlapping_retained_process_intervals")
        if isinstance(value, dict)
        else None
    )
    interval_count = value.get("retained_interval_count") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != PROCESS_INTERVAL_OVERLAP_KEYS
        or value.get("schema_version") != "ridgeway-retained-process-interval-overlap/v1.0"
        or value.get("observation_method") != "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS"
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or isinstance(interval_count, bool)
        or interval_count != 1_596
        or not 1 <= maximum <= interval_count
        or value.get("coincident_endpoint_policy")
        != "COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION"
        or value.get("scientific_identity_role") != "AUDIT_ONLY_EXCLUDED"
        or value.get("limitation") != PROCESS_INTERVAL_OVERLAP_LIMITATION
    ):
        raise ValueError(f"{label} retained process-interval overlap is invalid")
    return value


def derive_attestation(
    *,
    source_commit: str,
    publish_directory: Path,
    desktop_fixture: Path,
    producer_publish_directory: Path,
    differential_report: Path,
) -> dict[str, object]:
    """Derive the exact receipt from a release-eligible Ridgeway package."""

    if not SOURCE_SHA.fullmatch(source_commit):
        raise ValueError("source commit must be an exact lowercase Git commit SHA")
    if publish_directory.is_symlink():
        raise ValueError("Ridgeway publish directory may not be a symlink")
    publish_directory = publish_directory.resolve(strict=True)
    if not publish_directory.is_dir():
        raise ValueError("Ridgeway publish directory must be a regular directory")

    direct_path = _regular(
        publish_directory / "direct-evidence-manifest.json",
        label="Ridgeway direct-evidence manifest",
    )
    projection_path = _regular(
        publish_directory / "ui_projection.json",
        label="Ridgeway UI projection",
    )
    desktop_fixture = _regular(desktop_fixture, label="Ridgeway desktop fixture")
    trust_path = _regular(
        desktop_fixture.with_name("ridgewayReleaseTrust.json"),
        label="Ridgeway release-trust record",
    )
    direct = _object(direct_path, label="Ridgeway direct-evidence manifest")
    projection = _object(projection_path, label="Ridgeway UI projection")
    trust = _object(trust_path, label="Ridgeway release-trust record")
    if projection_path.read_bytes() != desktop_fixture.read_bytes():
        raise ValueError("Ridgeway benchmark and desktop projections differ")

    direct_sha = _digest(direct_path)
    bundle = direct.get("evidence_bundle")
    if not isinstance(bundle, dict):
        raise TypeError("Ridgeway direct-evidence manifest omits its evidence bundle")
    archive_name = bundle.get("filename")
    if (
        not isinstance(archive_name, str)
        or PurePosixPath(archive_name).name != archive_name
        or "/" in archive_name
        or "\\" in archive_name
    ):
        raise ValueError("Ridgeway evidence archive filename is unsafe")
    archive = _regular(
        publish_directory / archive_name,
        label="Ridgeway direct-evidence archive",
    )
    archive_sha = _digest(archive)

    if producer_publish_directory.is_symlink():
        raise ValueError("producer Ridgeway publish directory may not be a symlink")
    producer_publish_directory = producer_publish_directory.resolve(strict=True)
    producer_direct_path = _regular(
        producer_publish_directory / "direct-evidence-manifest.json",
        label="producer Ridgeway direct-evidence manifest",
    )
    producer_direct = _object(
        producer_direct_path, label="producer Ridgeway direct-evidence manifest"
    )
    producer_bundle = producer_direct.get("evidence_bundle")
    if not isinstance(producer_bundle, dict):
        raise TypeError("producer Ridgeway direct manifest omits its evidence bundle")
    producer_archive_name = producer_bundle.get("filename")
    if (
        not isinstance(producer_archive_name, str)
        or PurePosixPath(producer_archive_name).name != producer_archive_name
        or "/" in producer_archive_name
        or "\\" in producer_archive_name
    ):
        raise ValueError("producer Ridgeway evidence archive filename is unsafe")
    producer_archive = _regular(
        producer_publish_directory / producer_archive_name,
        label="producer Ridgeway direct-evidence archive",
    )
    producer_archive_sha = _digest(producer_archive)
    differential_report = _regular(
        differential_report, label="protected Ridgeway rerun differential"
    )
    differential = _object(differential_report, label="protected Ridgeway rerun differential")
    reference_interval_overlap = _retained_process_interval_overlap(
        direct.get("retained_process_interval_overlap"), label="reference"
    )
    producer_interval_overlap = _retained_process_interval_overlap(
        producer_direct.get("retained_process_interval_overlap"), label="producer"
    )

    if (
        direct.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or direct.get("evidence_contract_status") == "SUPERSEDED_NON_RELEASE"
        or direct.get("historical_evidence_only") is True
        or direct.get("release_replay_eligible") is not True
        or direct.get("matrix_job_count") != 1_536
        or direct.get("map_job_count") != 60
        or bundle.get("format") != "tar+gzip"
        or bundle.get("sha256") != archive_sha
        or bundle.get("byte_size") != archive.stat().st_size
        or type(bundle.get("file_count")) is not int
        or bundle["file_count"] <= 0
        or projection.get("schemaVersion") != "ecocorridor-replay/v1.2"
        or projection.get("fixtureId") != "ridgeway_crossing"
        or projection.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
        or projection.get("releaseReplayEligible") is not True
        or projection.get("manifestSha256") != direct_sha
        or trust.get("schemaVersion") != "ecocorridor-release-trust/v1.0"
        or trust.get("fixtureId") != "ridgeway_crossing"
        or trust.get("trusted") is not True
        or trust.get("evidenceManifestSha256") != direct_sha
        or producer_direct.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or producer_direct.get("release_replay_eligible") is not True
        or producer_direct.get("matrix_job_count") != 1_536
        or producer_direct.get("map_job_count") != 60
        or producer_bundle.get("sha256") != producer_archive_sha
        or producer_bundle.get("byte_size") != producer_archive.stat().st_size
        or differential.get("schema_version") != "ridgeway-protected-rerun-differential/v1.1"
        or differential.get("source_commit_sha") != source_commit
        or differential.get("status") != "PASSED"
        or differential.get("reference_manifest_sha256") != direct_sha
        or differential.get("producer_manifest_sha256") != _digest(producer_direct_path)
        or differential.get("reference_archive_sha256") != archive_sha
        or differential.get("producer_archive_sha256") != producer_archive_sha
        or differential.get("matrix_job_count") != 1_536
        or differential.get("map_job_count") != 60
        or differential.get("teaching_selection_matched") is not True
        or differential.get("reference_retained_process_interval_overlap")
        != reference_interval_overlap
        or differential.get("producer_retained_process_interval_overlap")
        != producer_interval_overlap
        or differential.get("retained_process_interval_overlap_comparison_policy")
        != "AUDIT_ONLY_EXCLUDED_FROM_SCIENTIFIC_EQUIVALENCE"
    ):
        raise ValueError("Ridgeway package is not eligible for protected producer attestation")

    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit_sha": source_commit,
        "execution_contract": "FULL_DIRECT_CIRCUITSCAPE_RASTER_PAIRWISE_RERUN",
        "fixture_id": "ridgeway_crossing",
        "evidence_disposition": "VERIFIED_DIRECT_EVIDENCE",
        "direct_evidence_manifest_sha256": direct_sha,
        "ui_projection_sha256": _digest(projection_path),
        "desktop_fixture_sha256": _digest(desktop_fixture),
        "release_trust_sha256": _digest(trust_path),
        "archive_filename": archive_name,
        "archive_sha256": archive_sha,
        "archive_byte_size": archive.stat().st_size,
        "archive_file_count": bundle["file_count"],
        "producer_direct_evidence_manifest_sha256": _digest(producer_direct_path),
        "producer_archive_sha256": producer_archive_sha,
        "producer_engine_manifest_sha256": _strict_sha(
            producer_direct.get("engine_manifest_sha256"),
            label="producer engine manifest identity",
        ),
        "producer_workflow_manifest_sha256": _strict_sha(
            producer_direct.get("workflow_manifest_sha256"),
            label="producer workflow manifest identity",
        ),
        "protected_rerun_differential_sha256": _digest(differential_report),
        "engine_manifest_sha256": _strict_sha(
            direct.get("engine_manifest_sha256"), label="engine manifest identity"
        ),
        "workflow_manifest_sha256": _strict_sha(
            direct.get("workflow_manifest_sha256"), label="workflow manifest identity"
        ),
        "evidence_index_sha256": _strict_sha(
            direct.get("evidence_index_sha256"), label="evidence index identity"
        ),
        "matrix_job_count": 1_536,
        "map_job_count": 60,
    }


def verify_attestation(
    payload: object,
    *,
    source_commit: str,
    publish_directory: Path,
    desktop_fixture: Path,
    producer_publish_directory: Path,
    differential_report: Path,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != EXPECTED_KEYS:
        raise ValueError("Ridgeway producer attestation has an unexpected contract")
    expected = derive_attestation(
        source_commit=source_commit,
        publish_directory=publish_directory,
        desktop_fixture=desktop_fixture,
        producer_publish_directory=producer_publish_directory,
        differential_report=differential_report,
    )
    if payload != expected:
        mismatches = sorted(key for key in EXPECTED_KEYS if payload.get(key) != expected[key])
        raise ValueError(
            "Ridgeway producer attestation differs from exact source evidence: "
            + ", ".join(mismatches)
        )
    return expected


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--publish-directory", required=True, type=Path)
    parser.add_argument("--desktop-fixture", required=True, type=Path)
    parser.add_argument("--producer-publish-directory", required=True, type=Path)
    parser.add_argument("--differential-report", required=True, type=Path)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            if arguments.output is None or arguments.attestation is not None:
                parser.error("create requires --output and forbids --attestation")
            payload = derive_attestation(
                source_commit=arguments.source_commit,
                publish_directory=arguments.publish_directory,
                desktop_fixture=arguments.desktop_fixture,
                producer_publish_directory=arguments.producer_publish_directory,
                differential_report=arguments.differential_report,
            )
            _atomic_json(arguments.output, payload)
        else:
            if arguments.attestation is None or arguments.output is not None:
                parser.error("verify requires --attestation and forbids --output")
            supplied = json.loads(
                _regular(arguments.attestation, label="Ridgeway producer attestation").read_text(
                    encoding="utf-8"
                )
            )
            payload = verify_attestation(
                supplied,
                source_commit=arguments.source_commit,
                publish_directory=arguments.publish_directory,
                desktop_fixture=arguments.desktop_fixture,
                producer_publish_directory=arguments.producer_publish_directory,
                differential_report=arguments.differential_report,
            )
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        "Ridgeway protected producer attestation verified for "
        f"{payload['matrix_job_count'] + payload['map_job_count']} direct jobs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
