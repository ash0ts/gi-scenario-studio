from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_ecological_data_manifest.py"
    spec = importlib.util.spec_from_file_location("ecological_data_manifest_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signed_desktop_bundle_authorization_is_explicit_and_exact(
    checker: ModuleType,
) -> None:
    root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (root / "benchmarks/ecocorridor/data-manifest.json").read_text(encoding="utf-8")
    )
    assert payload["manifest_version"] == checker.MANIFEST_VERSION == "1.1"
    records = {record["path"]: record for record in payload["files"]}
    direct = json.loads(
        (root / "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    selected_archive = "benchmarks/ecocorridor/ridgeway/" + direct["evidence_bundle"]["filename"]
    expected = {
        "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json",
        "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
        selected_archive,
        "benchmarks/ecocorridor/ridgeway/ui_projection.json",
    }

    assert {
        path
        for path, record in records.items()
        if record.get("signed_desktop_bundle_allowed") is True
    } == expected
    assert checker.validate_signed_desktop_bundle_authorizations(records) is None
    assert (
        records["benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json"][
            "public_replay_allowed"
        ]
        is False
    )
    assert records[selected_archive]["public_replay_allowed"] is False

    superseded = "benchmarks/ecocorridor/ridgeway/ridgeway-direct-evidence-4858c0a48841.tar.gz"
    records[superseded] = {**records[superseded], "signed_desktop_bundle_allowed": True}
    assert "exactly" in str(checker.validate_signed_desktop_bundle_authorizations(records))


def test_signed_desktop_bundle_roles_require_a_boolean_decision(checker: ModuleType) -> None:
    direct = {"asset_role": "DIRECT_EVIDENCE_MANIFEST"}
    assert "explicit decision" in str(
        checker.validate_signed_desktop_bundle_policy("benchmarks/ecocorridor/ridgeway/x", direct)
    )
    direct["signed_desktop_bundle_allowed"] = "yes"
    assert "explicit decision" in str(
        checker.validate_signed_desktop_bundle_policy("benchmarks/ecocorridor/ridgeway/x", direct)
    )
    unrelated = {
        "asset_role": "DOCUMENTATION",
        "signed_desktop_bundle_allowed": False,
    }
    assert "not valid" in str(
        checker.validate_signed_desktop_bundle_policy(
            "benchmarks/ecocorridor/ridgeway/README.md", unrelated
        )
    )


@pytest.mark.parametrize(
    "path",
    [
        "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
        "benchmarks/ecocorridor/ridgeway/ridgeway-direct-evidence-deadbeef.tar.gz",
        "benchmarks/ecocorridor/ridgeway/README.md",
        "benchmarks/ecocorridor/search/README.md",
        "benchmarks/ecocorridor/search/manifest.json",
        "benchmarks/ecocorridor/search/nested/landscape.json",
        "benchmarks/ecocorridor/search/unreviewed-output.bin",
        "benchmarks/ecocorridor/ridgeway/unreviewed-map.jp2",
        "apps/ecocorridor-desktop/src/data/ridgewayFixture.json",
        "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json",
        "apps/ecocorridor-desktop/tests/__screenshots__/home-desktop-1440.png",
        "apps/ecocorridor-desktop/tests/__screenshots__/unexpected.txt",
        "apps/ecocorridor-replay/src/alternateFixture.json",
        "apps/ecocorridor-replay/src/real_species.ts",
        "python/ecocorridor_core/data/assistant_evaluation_cases.json",
        "outside/real_locations.csv",
        "assets/ecology/resistance.tif",
        "assets/ecology/focal-areas.gpkg",
    ],
)
def test_public_ecological_evidence_cannot_bypass_classification(
    checker: ModuleType, path: str
) -> None:
    assert checker.requires_classification(path)


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "apps/ecocorridor-desktop/package.json",
        "apps/ecocorridor-desktop/src-tauri/icons/icon.png",
        "benchmarks/ecocorridor/ridgeway/materialize_inputs.py",
        "benchmarks/ecocorridor/ridgeway/run_direct_study.py",
        "benchmarks/ecocorridor/data-manifest.json",
        "docs/ecocorridor/README.md",
    ],
)
def test_non_evidence_files_do_not_expand_the_public_data_surface(
    checker: ModuleType, path: str
) -> None:
    assert not checker.requires_classification(path)


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    [
        (
            "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
            "DIRECT_EVIDENCE_MANIFEST",
            "application/json",
        ),
        (
            "benchmarks/ecocorridor/ridgeway/ridgeway-direct-evidence-deadbeef.tar.gz",
            "DIRECT_EVIDENCE_ARCHIVE",
            "application/gzip",
        ),
        (
            "benchmarks/ecocorridor/search/h1_low_barrier.json",
            "SEARCH_DATASET",
            "application/json",
        ),
        (
            "apps/ecocorridor-desktop/src/data/ridgewayFixture.json",
            "PUBLIC_REPLAY_FIXTURE",
            "application/json",
        ),
        (
            "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json",
            "RELEASE_TRUST_RECORD",
            "application/json",
        ),
        (
            "apps/ecocorridor-desktop/tests/__screenshots__/home-desktop-1440.png",
            "SCREENSHOT",
            "image/png",
        ),
        (
            "apps/ecocorridor-replay/src/alternateFixture.json",
            "ECOLOGICAL_FIXTURE_SOURCE",
            "application/json",
        ),
        (
            "apps/ecocorridor-replay/src/real_species.ts",
            "ECOLOGICAL_FIXTURE_SOURCE",
            "text/typescript",
        ),
        (
            "python/ecocorridor_core/data/assistant_evaluation_cases.json",
            "ASSISTANT_EVALUATION_DATASET",
            "application/json",
        ),
        ("outside/real_locations.csv", "ECOLOGICAL_FIXTURE_SOURCE", "text/csv"),
        ("assets/ecology/resistance.tif", "SPATIAL_FIXTURE", "image/tiff"),
    ],
)
def test_known_assets_have_path_bound_roles_and_media_types(
    checker: ModuleType, path: str, role: str, media_type: str
) -> None:
    assert checker.expected_asset_role(path) == role
    assert checker.expected_media_type(path) == media_type


def test_ridgeway_assets_form_one_fail_closed_historical_chain(checker: ModuleType) -> None:
    assert checker.validate_ridgeway_relationships() == []


def test_manifest_gate_rejects_impossible_retained_interval_overlap(
    checker: ModuleType,
) -> None:
    impossible = {
        "schema_version": "ridgeway-retained-process-interval-overlap/v1.0",
        "observation_method": "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS",
        "maximum_overlapping_retained_process_intervals": 1_597,
        "retained_interval_count": 1_596,
        "coincident_endpoint_policy": ("COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION"),
        "scientific_identity_role": "AUDIT_ONLY_EXCLUDED",
        "limitation": checker.PROCESS_INTERVAL_OVERLAP_LIMITATION,
    }

    assert checker._valid_retained_process_interval_overlap(impossible) is False


def test_search_assets_pin_direct_evidence_and_fail_closed_ml_gate(
    checker: ModuleType,
) -> None:
    assert checker.validate_search_relationships() == []


def test_historical_search_validation_never_rebinds_to_current_sources(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_digest = checker.digest

    def reject_current_source_lookup(path: Path) -> str:
        normalized = path.as_posix()
        if normalized.endswith(
            (
                "python/ecocorridor_core/benchmark.py",
                "python/ecocorridor_core/resource_observability.py",
            )
        ):
            raise AssertionError("historical validation consulted mutable current source")
        return original_digest(path)

    monkeypatch.setattr(checker, "digest", reject_current_source_lookup)
    assert checker.validate_search_relationships() == []


def test_historical_search_snapshot_tampering_is_rejected(
    checker: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = Path(__file__).resolve().parents[2]
    isolated_root = tmp_path / "repository"
    search_target = isolated_root / "benchmarks/ecocorridor/search"
    ridgeway_target = isolated_root / "benchmarks/ecocorridor/ridgeway"
    search_target.parent.mkdir(parents=True)
    ridgeway_target.mkdir(parents=True)
    shutil.copytree(source_root / "benchmarks/ecocorridor/search", search_target)
    shutil.copy2(
        source_root
        / "benchmarks/ecocorridor/ridgeway/ridgeway-direct-evidence-4858c0a48841.tar.gz",
        ridgeway_target / "ridgeway-direct-evidence-4858c0a48841.tar.gz",
    )
    snapshot = search_target / "provenance/resource-observability-dfa5f7187975.py.txt"
    snapshot.write_bytes(snapshot.read_bytes() + b"\n# tampered\n")
    monkeypatch.setattr(checker, "ROOT", isolated_root)

    findings = checker.validate_search_relationships()
    assert any("generator snapshot digest changed" in finding for finding in findings)


def test_historical_search_archive_linkage_and_production_disposition_are_enforced(
    checker: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = Path(__file__).resolve().parents[2]
    isolated_root = tmp_path / "repository"
    search_target = isolated_root / "benchmarks/ecocorridor/search"
    ridgeway_target = isolated_root / "benchmarks/ecocorridor/ridgeway"
    search_target.parent.mkdir(parents=True)
    ridgeway_target.mkdir(parents=True)
    shutil.copytree(source_root / "benchmarks/ecocorridor/search", search_target)
    shutil.copy2(
        source_root
        / "benchmarks/ecocorridor/ridgeway/ridgeway-direct-evidence-4858c0a48841.tar.gz",
        ridgeway_target / "ridgeway-direct-evidence-4858c0a48841.tar.gz",
    )
    provenance_path = search_target / "historical-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["production_authorization"] = "ALLOWED"
    provenance["source_evidence"]["bundle_index_sha256"] = "0" * 64
    unsigned = {key: value for key, value in provenance.items() if key != "provenance_sha256"}
    provenance["provenance_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", isolated_root)

    findings = checker.validate_search_relationships()
    assert any("non-production disposition" in finding for finding in findings)
    assert any("bundle-index digest changed" in finding for finding in findings)


def test_repository_ecological_manifest_is_exhaustive_and_valid(checker: ModuleType) -> None:
    assert checker.main() == 0


def test_current_ridgeway_recomputes_to_verified_direct_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(root / "scripts/verify_ridgeway_release_evidence.py"),
        "--publish-directory",
        str(root / "benchmarks/ecocorridor/ridgeway"),
        "--desktop-fixture",
        str(root / "apps/ecocorridor-desktop/src/data/ridgewayFixture.json"),
    ]
    verified = subprocess.run(command, check=False, capture_output=True, text=True)
    assert verified.returncode == 0, verified.stderr
    assert "VERIFIED_DIRECT_EVIDENCE" in verified.stdout

    explicit_historical_permission_does_not_change_current_evidence = subprocess.run(
        [*command, "--allow-historical-blocked"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert explicit_historical_permission_does_not_change_current_evidence.returncode == 0
    assert (
        "VERIFIED_DIRECT_EVIDENCE"
        in explicit_historical_permission_does_not_change_current_evidence.stdout
    )
