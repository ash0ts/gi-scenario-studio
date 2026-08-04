#!/usr/bin/env python3
"""Require an explicit public-data record for every tracked ecological spatial asset."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "ecocorridor" / "data-manifest.json"
SPATIAL_SUFFIXES = {
    ".asc",
    ".cpg",
    ".dbf",
    ".fgb",
    ".geojson",
    ".gpkg",
    ".grd",
    ".img",
    ".prj",
    ".shp",
    ".shx",
    ".tif",
    ".tiff",
}
TABULAR_FIXTURE_SUFFIXES = {".csv", ".tsv"}
EMBEDDED_FIXTURE_SUFFIXES = {".json", ".ts", ".tsx"}
EMBEDDED_FIXTURE_NAME_MARKERS = ("fixture", "location", "species")
ECOLOGICAL_BENCHMARK_PREFIX = "benchmarks/ecocorridor/"
ECOLOGICAL_DATA_PREFIX = "python/ecocorridor_core/data/"
PUBLIC_REPLAY_SOURCE_PREFIX = "apps/ecocorridor-replay/src/"
GI_LOCKED_PREFIXES = ("apps/desktop/", "benchmarks/cc0_reference/")
RIDGEWAY_PREFIX = "benchmarks/ecocorridor/ridgeway/"
SEARCH_PREFIX = "benchmarks/ecocorridor/search/"
SEARCH_HISTORICAL_PROVENANCE = f"{SEARCH_PREFIX}historical-provenance.json"
DESKTOP_FIXTURE = "apps/ecocorridor-desktop/src/data/ridgewayFixture.json"
DESKTOP_TRUST_RECORD = "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json"
SCREENSHOT_PREFIX = "apps/ecocorridor-desktop/tests/__screenshots__/"
MANIFEST_PATH = "benchmarks/ecocorridor/data-manifest.json"
MANIFEST_VERSION = "1.1"
SIGNED_DESKTOP_BUNDLE_ROLES = {
    "DIRECT_EVIDENCE_ARCHIVE",
    "DIRECT_EVIDENCE_MANIFEST",
    "RELEASE_TRUST_RECORD",
    "UI_PROJECTION",
}
NON_ASSET_CONTROL_FILES = {
    MANIFEST_PATH,
    f"{RIDGEWAY_PREFIX}materialize_inputs.py",
    f"{RIDGEWAY_PREFIX}run_direct_study.py",
}
ALLOWED_ASSET_ROLES = {
    "DIRECT_EVIDENCE_ARCHIVE",
    "DIRECT_EVIDENCE_MANIFEST",
    "DOCUMENTATION",
    "FIXTURE_METADATA",
    "PUBLIC_REPLAY_FIXTURE",
    "ECOLOGICAL_FIXTURE_SOURCE",
    "ASSISTANT_EVALUATION_DATASET",
    "ASSISTANT_EVALUATION_MANIFEST",
    "RELEASE_TRUST_RECORD",
    "SCREENSHOT",
    "SEARCH_DATASET",
    "SEARCH_MANIFEST",
    "SEARCH_PROVENANCE",
    "SEARCH_RELEASE_GATE",
    "SEARCH_RESOURCE_OBSERVATION",
    "SPATIAL_FIXTURE",
    "SUPPORTING_EVIDENCE",
    "UI_PROJECTION",
}
ALLOWED_REPOSITORY_USES = {
    "BENCHMARK_ONLY",
    "DOCUMENTATION",
    "PUBLIC_REPLAY_SOURCE",
    "VERIFICATION_EVIDENCE",
}
EXPECTED_MEDIA_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".ts": "text/typescript",
    ".tsx": "text/typescript-jsx",
    ".md": "text/markdown",
    ".png": "image/png",
    ".geojson": "application/geo+json",
    ".gpkg": "application/geopackage+sqlite3",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
MAX_ARCHIVE_FILES = 50_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
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

RIDGEWAY_FIXED_ROLE_COUNTS = {
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

RIDGEWAY_REQUIRED_COVERAGE = {
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
}

RIDGEWAY_HISTORICAL_ROLE_COUNTS = {
    "source_asset": 21,
    "engine_snapshot": 4,
    "evidence_index": 1,
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
    "map_current_raster": 60,
}


def validate_signed_desktop_bundle_policy(
    path: str,
    record: dict[str, object],
) -> str | None:
    """Require an explicit, role-bounded decision for desktop fixture bytes.

    This permission is deliberately independent of ``public_replay_allowed``:
    the public site remains projection-only, while a signed desktop fixture may
    carry the directly executed synthetic evidence needed by its Evidence Rail.
    """

    role = record.get("asset_role")
    declared = "signed_desktop_bundle_allowed" in record
    if role in SIGNED_DESKTOP_BUNDLE_ROLES:
        if not declared or not isinstance(record.get("signed_desktop_bundle_allowed"), bool):
            return f"Desktop-bundle ecological data must declare an explicit decision: {path}"
    elif declared:
        return f"Desktop-bundle authorization is not valid for this ecological asset role: {path}"
    if (
        record.get("signed_desktop_bundle_allowed") is True
        and path != DESKTOP_TRUST_RECORD
        and not path.startswith(RIDGEWAY_PREFIX)
    ):
        return f"Signed desktop fixture authorization is limited to Ridgeway assets: {path}"
    return None


def validate_signed_desktop_bundle_authorizations(
    records: dict[str, dict[str, object]],
) -> str | None:
    """Authorize only the current, independently trusted Ridgeway evidence set."""

    direct_path = f"{RIDGEWAY_PREFIX}direct-evidence-manifest.json"
    try:
        direct = json.loads((ROOT / direct_path).read_text(encoding="utf-8"))
        archive_name = direct["evidence_bundle"]["filename"]
    except (OSError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return "Signed desktop authorization cannot resolve the active Ridgeway evidence archive."
    if (
        not isinstance(archive_name, str)
        or PurePosixPath(archive_name).name != archive_name
        or "/" in archive_name
        or "\\" in archive_name
    ):
        return "Signed desktop authorization resolved an unsafe Ridgeway evidence archive."

    expected = {
        DESKTOP_TRUST_RECORD,
        direct_path,
        f"{RIDGEWAY_PREFIX}{archive_name}",
        f"{RIDGEWAY_PREFIX}ui_projection.json",
    }
    authorized = {
        path
        for path, record in records.items()
        if record.get("signed_desktop_bundle_allowed") is True
    }
    if authorized != expected:
        missing = ", ".join(sorted(expected - authorized)) or "none"
        unexpected = ", ".join(sorted(authorized - expected)) or "none"
        return (
            "Signed desktop fixture authorization must match the current trusted evidence set "
            f"exactly (missing: {missing}; unexpected: {unexpected})."
        )

    expected_public = {
        DESKTOP_TRUST_RECORD,
        f"{RIDGEWAY_PREFIX}ui_projection.json",
    }
    for path in expected:
        record = records.get(path)
        if not isinstance(record, dict):
            return f"Signed desktop fixture authorization references an unclassified asset: {path}"
        if record.get("public_replay_allowed") is not (path in expected_public):
            return f"Signed desktop fixture has the wrong public-replay disposition: {path}"
    return None


def requires_classification(path: str) -> bool:
    """Return whether a tracked path is ecological/public-replay evidence.

    These rules are deliberately path based. Adding a new file format or a
    second copy of a fixture must expand this allowlisted surface explicitly;
    a generic JSON, archive, or screenshot elsewhere in the repository is not
    silently treated as ecological evidence.
    """

    normalized = PurePosixPath(path).as_posix()
    suffix = PurePosixPath(normalized).suffix.casefold()
    if suffix in SPATIAL_SUFFIXES:
        return True
    if normalized.startswith(ECOLOGICAL_DATA_PREFIX):
        return True
    if suffix in TABULAR_FIXTURE_SUFFIXES and not normalized.startswith(GI_LOCKED_PREFIXES):
        return True
    filename = PurePosixPath(normalized).name.casefold()
    if (
        suffix in EMBEDDED_FIXTURE_SUFFIXES
        and any(marker in filename for marker in EMBEDDED_FIXTURE_NAME_MARKERS)
        and not normalized.startswith(GI_LOCKED_PREFIXES)
    ):
        return True
    if normalized.startswith(PUBLIC_REPLAY_SOURCE_PREFIX) and suffix in {
        ".json",
        ".csv",
        ".tsv",
    }:
        return True
    if normalized.startswith(ECOLOGICAL_BENCHMARK_PREFIX):
        return normalized not in NON_ASSET_CONTROL_FILES
    if normalized in {DESKTOP_FIXTURE, DESKTOP_TRUST_RECORD}:
        return True
    return normalized.startswith(SCREENSHOT_PREFIX)


def expected_asset_role(path: str) -> str | None:
    """Return the specific role required for a known release asset."""

    suffix = PurePosixPath(path).suffix.casefold()
    if path == DESKTOP_FIXTURE:
        return "PUBLIC_REPLAY_FIXTURE"
    if path == DESKTOP_TRUST_RECORD:
        return "RELEASE_TRUST_RECORD"
    if path == f"{ECOLOGICAL_DATA_PREFIX}assistant_evaluation_cases.json":
        return "ASSISTANT_EVALUATION_DATASET"
    if path == f"{ECOLOGICAL_DATA_PREFIX}assistant_evaluation_manifest.json":
        return "ASSISTANT_EVALUATION_MANIFEST"
    if path.startswith(SCREENSHOT_PREFIX):
        return "SCREENSHOT"
    if path == f"{RIDGEWAY_PREFIX}README.md" or path == f"{SEARCH_PREFIX}README.md":
        return "DOCUMENTATION"
    if path == f"{RIDGEWAY_PREFIX}manifest.json":
        return "FIXTURE_METADATA"
    if path == f"{RIDGEWAY_PREFIX}ui_projection.json":
        return "UI_PROJECTION"
    if path == f"{RIDGEWAY_PREFIX}direct-evidence-manifest.json":
        return "DIRECT_EVIDENCE_MANIFEST"
    if path.startswith(RIDGEWAY_PREFIX) and path.endswith(".tar.gz"):
        return "DIRECT_EVIDENCE_ARCHIVE"
    if path == f"{SEARCH_PREFIX}manifest.json":
        return "SEARCH_MANIFEST"
    if path == SEARCH_HISTORICAL_PROVENANCE:
        return "SEARCH_PROVENANCE"
    if path == f"{SEARCH_PREFIX}release-gate.json":
        return "SEARCH_RELEASE_GATE"
    if path == f"{SEARCH_PREFIX}release-gate.resources.json":
        return "SEARCH_RESOURCE_OBSERVATION"
    if path.startswith(SEARCH_PREFIX) and suffix == ".json":
        return "SEARCH_DATASET"
    if (
        suffix in TABULAR_FIXTURE_SUFFIXES
        or path.startswith(PUBLIC_REPLAY_SOURCE_PREFIX)
        or (
            suffix in EMBEDDED_FIXTURE_SUFFIXES
            and any(
                marker in PurePosixPath(path).name.casefold()
                for marker in EMBEDDED_FIXTURE_NAME_MARKERS
            )
        )
    ):
        return "ECOLOGICAL_FIXTURE_SOURCE"
    if suffix in SPATIAL_SUFFIXES:
        return "SPATIAL_FIXTURE"
    if path.startswith(ECOLOGICAL_BENCHMARK_PREFIX):
        return "SUPPORTING_EVIDENCE"
    return None


def expected_media_type(path: str) -> str | None:
    if path.endswith(".tar.gz"):
        return "application/gzip"
    return EXPECTED_MEDIA_TYPES.get(PurePosixPath(path).suffix.casefold())


def classified_asset_candidates() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
    return {path for path in paths if requires_classification(path)}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON release asset must contain an object: {path.relative_to(ROOT)}")
    return value


def _safe_archive_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and "\\" not in name and not path.is_absolute() and ".." not in path.parts


def _valid_retained_process_interval_overlap(value: object) -> bool:
    maximum = (
        value.get("maximum_overlapping_retained_process_intervals")
        if isinstance(value, dict)
        else None
    )
    interval_count = value.get("retained_interval_count") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and set(value) == PROCESS_INTERVAL_OVERLAP_KEYS
        and value.get("schema_version") == "ridgeway-retained-process-interval-overlap/v1.0"
        and value.get("observation_method") == "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS"
        and not isinstance(maximum, bool)
        and isinstance(maximum, int)
        and not isinstance(interval_count, bool)
        and interval_count == 1_596
        and 1 <= maximum <= interval_count
        and value.get("coincident_endpoint_policy")
        == "COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION"
        and value.get("scientific_identity_role") == "AUDIT_ONLY_EXCLUDED"
        and value.get("limitation") == PROCESS_INTERVAL_OVERLAP_LIMITATION
    )


def validate_ridgeway_relationships() -> list[str]:
    """Audit either current release evidence or the explicit non-release fallback.

    A superseded archive remains checksum-verifiable benchmark material, but it
    must not authenticate a replay, populate an outcome, or leave a trusted
    manifest digest behind.  The current v1.2 package takes the stricter branch
    below and must satisfy the complete semantic evidence contract.
    """

    errors: list[str] = []
    direct_path = ROOT / RIDGEWAY_PREFIX / "direct-evidence-manifest.json"
    projection_path = ROOT / RIDGEWAY_PREFIX / "ui_projection.json"
    desktop_path = ROOT / DESKTOP_FIXTURE
    trust_path = ROOT / DESKTOP_TRUST_RECORD
    try:
        direct = _json_object(direct_path)
        projection = _json_object(projection_path)
        desktop = _json_object(desktop_path)
        trust = _json_object(trust_path)
    except (OSError, TypeError, ValueError) as error:
        return [str(error)]

    direct_sha = digest(direct_path)
    historical = (
        direct.get("evidence_contract_status") == "SUPERSEDED_NON_RELEASE"
        and direct.get("historical_evidence_only") is True
        and direct.get("release_replay_eligible") is False
    )
    if projection != desktop:
        errors.append("Ridgeway UI projection and public desktop fixture differ.")
    if historical:
        if (
            direct.get("schema_version") != "ridgeway-direct-evidence/v1.0"
            or direct.get("superseded_by_schema") != "ridgeway-direct-evidence/v1.1"
            or not isinstance(direct.get("release_blocker"), str)
            or not direct.get("release_blocker")
        ):
            errors.append("Ridgeway historical evidence is not explicitly superseded by v1.1.")
        if (
            projection.get("schemaVersion") != "ecocorridor-replay/v1.1"
            or projection.get("evidenceState") != "ENGINE_RUN_REQUIRED"
            or projection.get("releaseReplayEligible") is not False
            or projection.get("manifestSha256") is not None
            or projection.get("baselinePortfolioId") is not None
            or not isinstance(projection.get("releaseBlocker"), str)
            or not projection.get("releaseBlocker")
        ):
            errors.append("Ridgeway non-release projection is not fail closed.")
        portfolios = projection.get("portfolios")
        if not isinstance(portfolios, list) or len(portfolios) != 4:
            errors.append("Ridgeway non-release projection must retain four empty teaching roles.")
        else:
            for portfolio in portfolios:
                if (
                    not isinstance(portfolio, dict)
                    or portfolio.get("id") is not None
                    or portfolio.get("costUsd") is not None
                    or portfolio.get("passCount") is not None
                    or portfolio.get("requiredCount") != 12
                    or portfolio.get("worstReductionPct") is not None
                    or portfolio.get("roadMaskFlag") is not None
                    or portfolio.get("hypothesisRanks") is not None
                    or portfolio.get("scenarioResults") != []
                ):
                    errors.append(
                        "Ridgeway non-release projection contains a reportable portfolio outcome."
                    )
                    break
        if (
            trust.get("schemaVersion") != "ecocorridor-release-trust/v1.0"
            or trust.get("fixtureId") != projection.get("fixtureId")
            or trust.get("trusted") is not False
            or trust.get("evidenceManifestSha256") is not None
        ):
            errors.append("Ridgeway non-release trust record is not fail closed.")
    else:
        if (
            direct.get("schema_version") != "ridgeway-direct-evidence/v1.2"
            or direct.get("release_replay_eligible") is not True
            or projection.get("schemaVersion") != "ecocorridor-replay/v1.2"
            or projection.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
            or projection.get("releaseReplayEligible") is not True
            or projection.get("manifestSha256") != direct_sha
        ):
            errors.append("Ridgeway current replay does not satisfy the v1.2 release contract.")
        if (
            trust.get("schemaVersion") != "ecocorridor-release-trust/v1.0"
            or trust.get("fixtureId") != projection.get("fixtureId")
            or trust.get("trusted") is not True
            or trust.get("evidenceManifestSha256") != direct_sha
        ):
            errors.append(
                "Ridgeway independent release-trust record does not pin the direct-evidence manifest digest."
            )
        resource_observability = direct.get("resource_observability")
        if (
            not isinstance(resource_observability, dict)
            or projection.get("resourceObservability") != resource_observability
            or resource_observability.get("jobsWithObservations") != 1596
            or resource_observability.get("jobsWithoutObservations") != 0
        ):
            errors.append("Ridgeway current evidence lacks complete resource observations.")
        coherence = direct.get("map_matrix_coherence")
        if (
            not isinstance(coherence, dict)
            or set(coherence)
            != {
                "map_jobs_compared",
                "pair_values_compared",
                "maximum_absolute_difference",
                "maximum_relative_difference",
                "absolute_tolerance",
                "relative_tolerance",
                "status",
            }
            or coherence.get("map_jobs_compared") != 60
            or coherence.get("pair_values_compared") != 120
            or coherence.get("absolute_tolerance") != 1e-8
            or coherence.get("relative_tolerance") != 1e-7
            or coherence.get("status") != "WITHIN_TOLERANCE"
            or isinstance(coherence.get("maximum_absolute_difference"), bool)
            or not isinstance(coherence.get("maximum_absolute_difference"), (int, float))
            or coherence["maximum_absolute_difference"] < 0
            or isinstance(coherence.get("maximum_relative_difference"), bool)
            or not isinstance(coherence.get("maximum_relative_difference"), (int, float))
            or coherence["maximum_relative_difference"] < 0
        ):
            errors.append("Ridgeway map reruns are not reconciled to the study matrices.")
    study = projection.get("study")
    if not isinstance(study, dict):
        errors.append("Ridgeway UI projection is missing its study summary.")
    else:
        if study.get("matrixJobCount") != direct.get("matrix_job_count"):
            errors.append("Ridgeway matrix-job counts disagree across release manifests.")
        if study.get("mapJobCount") != direct.get("map_job_count"):
            errors.append("Ridgeway map-job counts disagree across release manifests.")
    if historical:

        def contains_active_release_flag(value: object) -> bool:
            if isinstance(value, dict):
                return any(
                    (
                        key
                        in {
                            "release_eligible",
                            "release_replay_eligible",
                            "releaseReplayEligible",
                            "trusted",
                        }
                        and child is True
                    )
                    or contains_active_release_flag(child)
                    for key, child in value.items()
                )
            if isinstance(value, list):
                return any(contains_active_release_flag(child) for child in value)
            return False

        if any(contains_active_release_flag(value) for value in (direct, projection, trust)):
            errors.append("Ridgeway superseded evidence contains an active release flag.")

    bundle = direct.get("evidence_bundle")
    if not isinstance(bundle, dict):
        return [*errors, "Ridgeway direct-evidence manifest is missing evidence_bundle."]
    filename = bundle.get("filename")
    if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
        return [*errors, "Ridgeway evidence archive filename is unsafe."]
    archive_path = direct_path.parent / filename
    if archive_path.is_symlink() or not archive_path.is_file():
        return [*errors, "Ridgeway evidence archive is missing or is a symlink."]
    if bundle.get("sha256") != digest(archive_path):
        errors.append("Ridgeway evidence archive digest disagrees with its direct manifest.")
    if bundle.get("byte_size") != archive_path.stat().st_size:
        errors.append("Ridgeway evidence archive byte count disagrees with its direct manifest.")

    internal_index = bundle.get("internal_index_path")
    expected_index_sha = bundle.get("internal_index_sha256")
    if not isinstance(internal_index, str) or not _safe_archive_member(internal_index):
        return [*errors, "Ridgeway archive internal-index path is unsafe."]
    seen: set[str] = set()
    index_bytes: bytes | None = None
    member_facts: dict[str, tuple[int, str]] = {}
    total_bytes = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if len(seen) >= MAX_ARCHIVE_FILES:
                    errors.append("Ridgeway evidence archive exceeds the file-count policy.")
                    break
                if not _safe_archive_member(member.name) or member.name in seen:
                    errors.append(
                        f"Ridgeway evidence archive has an unsafe or duplicate member: {member.name}"
                    )
                    break
                if not member.isfile():
                    errors.append(
                        f"Ridgeway evidence archive contains a non-regular member: {member.name}"
                    )
                    break
                if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    errors.append(
                        f"Ridgeway evidence archive member exceeds the size policy: {member.name}"
                    )
                    break
                total_bytes += member.size
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    errors.append("Ridgeway evidence archive exceeds the uncompressed-size policy.")
                    break
                seen.add(member.name)
                extracted = archive.extractfile(member)
                if extracted is None:
                    errors.append(f"Ridgeway archive member could not be read: {member.name}")
                    break
                member_digest = hashlib.sha256()
                member_bytes = 0
                retained = bytearray() if member.name == internal_index else None
                while chunk := extracted.read(1024 * 1024):
                    member_bytes += len(chunk)
                    member_digest.update(chunk)
                    if retained is not None:
                        retained.extend(chunk)
                if member_bytes != member.size:
                    errors.append(f"Ridgeway archive member is truncated: {member.name}")
                    break
                member_facts[member.name] = (member_bytes, member_digest.hexdigest())
                if retained is not None:
                    index_bytes = bytes(retained)
    except (OSError, tarfile.TarError) as error:
        errors.append(f"Ridgeway evidence archive is invalid: {error}")
        return errors
    if bundle.get("file_count") != len(seen):
        errors.append("Ridgeway evidence archive file count disagrees with its direct manifest.")
    if index_bytes is None:
        errors.append("Ridgeway evidence archive does not contain its declared internal index.")
    elif hashlib.sha256(index_bytes).hexdigest() != expected_index_sha:
        errors.append("Ridgeway archive internal-index digest disagrees with its direct manifest.")
    else:
        try:
            bundle_index = json.loads(index_bytes)
        except (UnicodeDecodeError, ValueError) as error:
            errors.append(f"Ridgeway archive internal index is invalid JSON: {error}")
        else:
            if not isinstance(bundle_index, dict) or not isinstance(
                bundle_index.get("files"), list
            ):
                errors.append("Ridgeway archive internal index must contain a files list.")
            else:
                indexed: dict[str, tuple[int, str]] = {}
                indexed_roles: list[str] = []
                for record in bundle_index["files"]:
                    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                        errors.append("Ridgeway archive internal index has an invalid file record.")
                        break
                    path = str(record["path"])
                    size = record.get("byte_size")
                    sha256 = record.get("sha256")
                    role = record.get("role")
                    if (
                        not _safe_archive_member(path)
                        or path in indexed
                        or not isinstance(size, int)
                        or size < 0
                        or not isinstance(sha256, str)
                        or not SHA256.fullmatch(sha256)
                        or not isinstance(role, str)
                        or not role.strip()
                    ):
                        errors.append(
                            f"Ridgeway archive internal index has an invalid entry: {path}"
                        )
                        break
                    indexed[path] = (size, sha256)
                    indexed_roles.append(role)
                actual_indexed = {
                    path: facts for path, facts in member_facts.items() if path != internal_index
                }
                if indexed != actual_indexed:
                    errors.append(
                        "Ridgeway archive members do not exactly match the byte counts and digests in its internal index."
                    )
                if bundle_index.get("evidence_index_sha256") != direct.get("evidence_index_sha256"):
                    errors.append(
                        "Ridgeway archive internal index does not pin the declared evidence index."
                    )
                if bundle_index.get("engine_manifest_sha256") != direct.get(
                    "engine_manifest_sha256"
                ):
                    errors.append(
                        "Ridgeway archive internal index does not pin the declared engine manifest."
                    )
                for identity in (
                    "fixture_version",
                    "model_sha256",
                    "physics_model_sha256",
                    "decision_snapshot_sha256",
                    "cost_basis_sha256",
                    "assumption_ledger_sha256",
                    "engine_environment_sha256",
                    "workflow_manifest_sha256",
                    "claim_safety_sha256",
                ):
                    if bundle_index.get(identity) != direct.get(identity):
                        errors.append(f"Ridgeway archive internal index does not pin {identity}.")
                coverage = bundle_index.get("required_coverage")
                if historical:
                    if (
                        bundle_index.get("schema_version") != "ridgeway-evidence-bundle/v1.0"
                        or bundle_index.get("matrix_job_count") != 1536
                        or bundle_index.get("map_job_count") != 60
                        or coverage
                        != {
                            "all_jobs_exact_and_qa_accepted": True,
                            "map_complete_artifact_sets": 60,
                            "matrix_request_result_matrix_qa_hash_records": 1536,
                        }
                        or Counter(indexed_roles) != Counter(RIDGEWAY_HISTORICAL_ROLE_COUNTS)
                    ):
                        errors.append(
                            "Ridgeway historical archive no longer matches its checksum-pinned v1.0 contract."
                        )
                else:
                    workflow_count = (
                        coverage.get("workflow_snapshots") if isinstance(coverage, dict) else None
                    )
                    expected_coverage = {
                        **RIDGEWAY_REQUIRED_COVERAGE,
                        "workflow_snapshots": workflow_count,
                    }
                    expected_roles = (
                        Counter(
                            {
                                **RIDGEWAY_FIXED_ROLE_COUNTS,
                                "workflow_snapshot": workflow_count,
                            }
                        )
                        if isinstance(workflow_count, int) and workflow_count > 0
                        else None
                    )
                    if coverage != expected_coverage or expected_roles is None:
                        errors.append("Ridgeway archive omits required semantic evidence coverage.")
                    elif Counter(indexed_roles) != expected_roles or bundle_index.get(
                        "role_counts"
                    ) != dict(sorted(expected_roles.items())):
                        errors.append(
                            "Ridgeway archive semantic role cardinalities are incomplete."
                        )
                    if bundle_index.get("resource_observability") != direct.get(
                        "resource_observability"
                    ):
                        errors.append(
                            "Ridgeway archive resource summary differs from its direct manifest."
                        )
                    process_exits = bundle_index.get("process_tree_exit_evidence")
                    if (
                        process_exits != direct.get("process_tree_exit_evidence")
                        or not isinstance(process_exits, dict)
                        or process_exits.get("jobs_with_confirmed_tree_exit") != 1596
                        or process_exits.get("jobs_without_confirmed_tree_exit") != 0
                        or process_exits.get("unique_process_identities") != 1596
                        or process_exits.get("confirmation_methods")
                        != {"POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT": 1596}
                    ):
                        errors.append(
                            "Ridgeway archive lacks complete confirmed process-tree exits."
                        )
                    process_interval_overlap = bundle_index.get("retained_process_interval_overlap")
                    if process_interval_overlap != direct.get(
                        "retained_process_interval_overlap"
                    ) or not _valid_retained_process_interval_overlap(process_interval_overlap):
                        errors.append(
                            "Ridgeway archive lacks valid retained process-interval overlap metadata."
                        )
                    if bundle_index.get("map_matrix_coherence") != direct.get(
                        "map_matrix_coherence"
                    ):
                        errors.append(
                            "Ridgeway archive map/matrix reconciliation differs from its direct manifest."
                        )
    return errors


def validate_search_relationships() -> list[str]:
    """Audit immutable historical search artifacts without rebinding their provenance.

    The committed campaign predates the active Ridgeway evidence contract.  Its
    source identities therefore resolve through a frozen historical provenance
    record and passive source snapshots, never through whichever implementation
    happens to be current in ``python/ecocorridor_core``.
    """

    errors: list[str] = []
    search_dir = ROOT / SEARCH_PREFIX
    try:
        manifest = _json_object(search_dir / "manifest.json")
        gate = _json_object(search_dir / "release-gate.json")
        resources = _json_object(search_dir / "release-gate.resources.json")
        provenance = _json_object(ROOT / SEARCH_HISTORICAL_PROVENANCE)
    except (OSError, TypeError, ValueError) as error:
        return [str(error)]

    expected_provenance_keys = {
        "schema_version",
        "dataset_disposition",
        "production_authorization",
        "claim_boundary",
        "source_evidence",
        "search_manifest",
        "campaign",
        "resource_report",
        "generator_source_snapshots",
        "limitations",
        "provenance_sha256",
    }
    if set(provenance) != expected_provenance_keys:
        errors.append("Historical search provenance has an unexpected contract.")
    provenance_payload = {
        key: value for key, value in provenance.items() if key != "provenance_sha256"
    }
    if provenance.get("provenance_sha256") != canonical_sha256(provenance_payload):
        errors.append("Historical search provenance digest does not match its contents.")
    if (
        provenance.get("schema_version") != "ecocorridor-search-historical-provenance/v1.0"
        or provenance.get("dataset_disposition") != "HISTORICAL_BENCHMARK_ONLY"
        or provenance.get("production_authorization") != "PROHIBITED"
        or not isinstance(provenance.get("claim_boundary"), str)
        or not provenance.get("claim_boundary")
        or not isinstance(provenance.get("limitations"), list)
        or not provenance.get("limitations")
    ):
        errors.append("Historical search artifacts lack an explicit non-production disposition.")

    def validate_pinned_file(
        record: object,
        *,
        expected_path: str,
        label: str,
        semantic_key: str | None = None,
        semantic_value: object = None,
    ) -> None:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "sha256",
            *(set() if semantic_key is None else {semantic_key}),
        }:
            errors.append(f"Historical search {label} record has an unexpected contract.")
            return
        if record.get("path") != expected_path:
            errors.append(f"Historical search {label} resolves to an unexpected path.")
            return
        path = ROOT / expected_path
        if path.is_symlink() or not path.is_file():
            errors.append(f"Historical search {label} is missing or is a symlink.")
            return
        if record.get("sha256") != digest(path):
            errors.append(f"Historical search {label} digest does not match its frozen bytes.")
        if semantic_key is not None and record.get(semantic_key) != semantic_value:
            errors.append(f"Historical search {label} semantic digest is inconsistent.")

    validate_pinned_file(
        provenance.get("search_manifest"),
        expected_path=f"{SEARCH_PREFIX}manifest.json",
        label="manifest",
        semantic_key="semantic_sha256",
        semantic_value=manifest.get("manifest_sha256"),
    )
    validate_pinned_file(
        provenance.get("campaign"),
        expected_path=f"{SEARCH_PREFIX}release-gate.json",
        label="campaign",
        semantic_key="campaign_sha256",
        semantic_value=gate.get("campaign_sha256"),
    )
    validate_pinned_file(
        provenance.get("resource_report"),
        expected_path=f"{SEARCH_PREFIX}release-gate.resources.json",
        label="resource report",
        semantic_key="report_sha256",
        semantic_value=resources.get("report_sha256"),
    )

    manifest_payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != canonical_sha256(manifest_payload):
        errors.append("Search manifest digest does not match its contents.")

    source = provenance.get("source_evidence")
    expected_source_keys = {
        "contract_status",
        "archive_path",
        "archive_sha256",
        "archive_byte_size",
        "archive_file_count",
        "bundle_index_path",
        "bundle_index_sha256",
        "evidence_index_path",
        "evidence_index_sha256",
    }
    if not isinstance(source, dict) or set(source) != expected_source_keys:
        errors.append("Historical search source-evidence record has an unexpected contract.")
        source = {}
    if source.get("contract_status") != "SUPERSEDED_NON_RELEASE":
        errors.append("Historical search source evidence is not explicitly superseded.")
    source_index_sha = source.get("evidence_index_sha256")
    if manifest.get("source_evidence_index_sha256") != source_index_sha:
        errors.append("Search manifest does not pin its historical Ridgeway evidence index.")

    archive_relative = source.get("archive_path")
    archive_path: Path | None = None
    if (
        not isinstance(archive_relative, str)
        or not archive_relative.startswith(RIDGEWAY_PREFIX)
        or "\\" in archive_relative
        or PurePosixPath(archive_relative).is_absolute()
        or ".." in PurePosixPath(archive_relative).parts
    ):
        errors.append("Historical search source archive path is unsafe.")
    else:
        archive_path = ROOT / archive_relative
        if archive_path.is_symlink() or not archive_path.is_file():
            errors.append("Historical search source archive is missing or is a symlink.")
            archive_path = None
        elif (
            source.get("archive_sha256") != digest(archive_path)
            or source.get("archive_byte_size") != archive_path.stat().st_size
        ):
            errors.append("Historical search source archive does not match its frozen identity.")

    bundle_bytes: bytes | None = None
    evidence_bytes: bytes | None = None
    if archive_path is not None:
        bundle_name = source.get("bundle_index_path")
        evidence_name = source.get("evidence_index_path")
        if (
            not isinstance(bundle_name, str)
            or not _safe_archive_member(bundle_name)
            or not isinstance(evidence_name, str)
            or not _safe_archive_member(evidence_name)
        ):
            errors.append("Historical search source archive declares unsafe index paths.")
        else:
            seen: set[str] = set()
            total_bytes = 0
            try:
                with tarfile.open(archive_path, mode="r:gz") as archive:
                    for member in archive:
                        if (
                            not _safe_archive_member(member.name)
                            or member.name in seen
                            or not member.isfile()
                            or member.size < 0
                            or member.size > MAX_ARCHIVE_MEMBER_BYTES
                        ):
                            errors.append(
                                "Historical search source archive contains an unsafe member."
                            )
                            break
                        seen.add(member.name)
                        total_bytes += member.size
                        if len(seen) > MAX_ARCHIVE_FILES or total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                            errors.append("Historical search source archive exceeds safety limits.")
                            break
                        if member.name in {bundle_name, evidence_name}:
                            extracted = archive.extractfile(member)
                            if extracted is None:
                                errors.append("Historical search source index could not be read.")
                                break
                            payload = extracted.read(member.size + 1)
                            if len(payload) != member.size:
                                errors.append("Historical search source index is truncated.")
                                break
                            if member.name == bundle_name:
                                bundle_bytes = payload
                            else:
                                evidence_bytes = payload
                if source.get("archive_file_count") != len(seen):
                    errors.append("Historical search source archive file count changed.")
            except (OSError, tarfile.TarError) as error:
                errors.append(f"Historical search source archive is invalid: {error}")

    if bundle_bytes is None or evidence_bytes is None:
        errors.append("Historical search source archive omits a declared evidence index.")
    else:
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
        if bundle_sha != source.get("bundle_index_sha256"):
            errors.append("Historical search bundle-index digest changed.")
        if evidence_sha != source_index_sha:
            errors.append("Historical search evidence-index digest changed.")
        try:
            bundle_index = json.loads(bundle_bytes)
        except (TypeError, ValueError) as error:
            errors.append(f"Historical search bundle index is invalid JSON: {error}")
        else:
            if (
                not isinstance(bundle_index, dict)
                or bundle_index.get("schema_version") != "ridgeway-evidence-bundle/v1.0"
                or bundle_index.get("evidence_index_sha256") != source_index_sha
            ):
                errors.append("Historical search bundle index does not pin its evidence index.")

    snapshots = provenance.get("generator_source_snapshots")
    expected_snapshot_keys = {"benchmark_sha256", "resource_observability_sha256"}
    declared_snapshot_shas: dict[str, str] = {}
    if not isinstance(snapshots, dict) or set(snapshots) != expected_snapshot_keys:
        errors.append("Historical search generator snapshots have an unexpected contract.")
    else:
        for identity in sorted(expected_snapshot_keys):
            record = snapshots.get(identity)
            if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
                errors.append(f"Historical search generator snapshot is invalid: {identity}")
                continue
            relative = record.get("path")
            expected_sha = record.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative.startswith(f"{SEARCH_PREFIX}provenance/")
                or not relative.endswith(".py.txt")
                or "\\" in relative
                or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts
                or not isinstance(expected_sha, str)
                or not SHA256.fullmatch(expected_sha)
            ):
                errors.append(f"Historical search generator snapshot path is unsafe: {identity}")
                continue
            snapshot_path = ROOT / relative
            if (
                snapshot_path.is_symlink()
                or not snapshot_path.is_file()
                or digest(snapshot_path) != expected_sha
            ):
                errors.append(f"Historical search generator snapshot digest changed: {identity}")
                continue
            declared_snapshot_shas[identity] = expected_sha

    resource_payload = {key: value for key, value in resources.items() if key != "report_sha256"}
    if (
        resources.get("schema_version") != "ecocorridor-search-resource-observation/v1.0"
        or resources.get("campaign_sha256") != gate.get("campaign_sha256")
        or resources.get("report_sha256") != canonical_sha256(resource_payload)
    ):
        errors.append("Search resource observation is not bound to the deterministic campaign.")
    if resources.get("execution_method_sources") != declared_snapshot_shas:
        errors.append("Search resource observation does not bind its frozen generator snapshots.")
    observation = resources.get("resource_observation")
    if (
        not isinstance(observation, dict)
        or observation.get("boundary") != "SEARCH_PROPOSER"
        or observation.get("energy_status") != "ESTIMATED"
        or observation.get("peak_memory_status") not in {"MEASURED", "NOT_MEASURED"}
        or "carbon" not in str(resources.get("claim_boundary", "")).casefold()
    ):
        errors.append("Search resource observation omits its bounded measurement disposition.")

    records = manifest.get("files")
    if not isinstance(records, list):
        return [*errors, "Search manifest files must be a list."]
    dataset_shas: set[str] = set()
    seen_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            errors.append("Search manifest contains an invalid dataset record.")
            continue
        relative = str(record["path"])
        if PurePosixPath(relative).name != relative or relative in seen_paths:
            errors.append(f"Search manifest contains an unsafe or duplicate path: {relative}")
            continue
        seen_paths.add(relative)
        asset = search_dir / relative
        if asset.is_symlink() or not asset.is_file() or digest(asset) != record.get("sha256"):
            errors.append(f"Search dataset digest disagrees with its manifest: {relative}")
        dataset_sha = record.get("dataset_sha256")
        if not isinstance(dataset_sha, str) or not SHA256.fullmatch(dataset_sha):
            errors.append(f"Search dataset has an invalid semantic digest: {relative}")
        else:
            dataset_shas.add(dataset_sha)
    gate_datasets = gate.get("datasets")
    if not isinstance(gate_datasets, list):
        errors.append("Search release gate is missing its dataset records.")
    else:
        gate_shas = {
            item.get("dataset_sha256")
            for item in gate_datasets
            if isinstance(item, dict) and isinstance(item.get("dataset_sha256"), str)
        }
        if gate_shas != dataset_shas:
            errors.append("Search release gate does not reference exactly the manifested datasets.")
    if (
        manifest.get("intended_use") != "ALGORITHM_RELEASE_GATE_ONLY"
        or gate.get("pinned_wandb_artifact") is not None
        or gate.get("production_adaptive_enabled") is not False
    ):
        errors.append("Historical search artifacts must remain prohibited from production use.")
    return errors


def main() -> int:
    if MANIFEST.is_symlink() or not MANIFEST.is_file():
        print("Ecological data manifest is missing.")
        return 1
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if payload.get("manifest_version") != MANIFEST_VERSION:
        print(f"Ecological data manifest must use manifest version {MANIFEST_VERSION}.")
        return 1
    records = payload.get("files")
    if not isinstance(records, list):
        print("Ecological data manifest files must be a list.")
        return 1
    by_path: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            print("Ecological data manifest contains an invalid record.")
            return 1
        path = str(record["path"])
        if (
            path in by_path
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            print(f"Ecological data manifest contains an unsafe or duplicate path: {path}")
            return 1
        by_path[path] = record

    tracked = classified_asset_candidates()
    if missing := sorted(tracked - by_path.keys()):
        print("Unclassified ecological spatial assets:")
        for path in missing:
            print(f"  {path}")
        return 1
    if stale := sorted(by_path.keys() - tracked):
        print("Ecological data manifest references untracked assets:")
        for path in stale:
            print(f"  {path}")
        return 1

    for path, record in sorted(by_path.items()):
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
            print(f"Ecological data record is incomplete: {path}")
            return 1
        if record["synthetic"] is not True or record["license"] != "CC0-1.0":
            print(f"Only checksum-verified CC0 synthetic spatial data may be tracked: {path}")
            return 1
        if record["sensitivity"] != "PUBLIC":
            print(f"Tracked ecological data must be classified PUBLIC: {path}")
            return 1
        if not isinstance(record["public_replay_allowed"], bool):
            print(f"Tracked synthetic data must declare whether public replay is allowed: {path}")
            return 1
        if desktop_policy_error := validate_signed_desktop_bundle_policy(path, record):
            print(desktop_policy_error)
            return 1
        if (
            record["public_replay_allowed"] is False
            and record["repository_use"] != "BENCHMARK_ONLY"
        ):
            print(f"Non-public ecological data must be benchmark-only: {path}")
            return 1
        if record["provenance"] != "SELF_AUTHORED_SYNTHETIC":
            print(f"Tracked ecological data must use self-authored synthetic provenance: {path}")
            return 1
        if record["contains_real_locations"] is not False:
            print(f"Tracked ecological data must explicitly contain no real locations: {path}")
            return 1
        if record["contains_species_occurrences"] is not False:
            print(f"Tracked ecological data must explicitly contain no species occurrences: {path}")
            return 1
        if record["asset_role"] not in ALLOWED_ASSET_ROLES:
            print(f"Ecological data record has an unsupported asset role: {path}")
            return 1
        if expected_asset_role(path) != record["asset_role"]:
            print(f"Ecological data record has the wrong asset role for its path: {path}")
            return 1
        if record["repository_use"] not in ALLOWED_REPOSITORY_USES:
            print(f"Ecological data record has an unsupported repository use: {path}")
            return 1
        if not isinstance(record["byte_size"], int) or record["byte_size"] < 0:
            print(f"Ecological data record has an invalid byte count: {path}")
            return 1
        if (
            not isinstance(record["ownership"], str)
            or not record["ownership"].strip()
            or not isinstance(record["media_type"], str)
            or "/" not in record["media_type"]
            or not isinstance(record["sha256"], str)
            or not SHA256.fullmatch(record["sha256"])
        ):
            print(f"Ecological data record has invalid classification metadata: {path}")
            return 1
        asset = ROOT / path
        if asset.is_symlink() or not asset.is_file():
            print(f"Tracked ecological data must be a regular non-symlink file: {path}")
            return 1
        if asset.stat().st_size != record["byte_size"]:
            print(f"Ecological data byte-count mismatch: {path}")
            return 1
        expected_type = expected_media_type(path)
        if expected_type is not None and record["media_type"] != expected_type:
            print(f"Ecological data media type does not match its path: {path}")
            return 1
        actual = digest(asset)
        if actual != record["sha256"]:
            print(f"Ecological data checksum mismatch: {path}")
            return 1
        if record["media_type"] == "application/json":
            try:
                _json_object(asset)
            except (TypeError, ValueError) as error:
                print(error)
                return 1
        if record["media_type"] == "image/png":
            with asset.open("rb") as source:
                if source.read(8) != b"\x89PNG\r\n\x1a\n":
                    print(f"Ecological screenshot is not a PNG file: {path}")
                    return 1

    if desktop_authorization_error := validate_signed_desktop_bundle_authorizations(by_path):
        print(desktop_authorization_error)
        return 1

    relationship_errors = [
        *validate_ridgeway_relationships(),
        *validate_search_relationships(),
    ]
    if relationship_errors:
        print("Ecological release-asset relationship checks failed:")
        for error in relationship_errors:
            print(f"  {error}")
        return 1
    print(f"Ecological data manifest passed for {len(tracked)} ecological release asset(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
