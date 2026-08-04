from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

RIDGEWAY_HYPOTHESIS_IDS = (
    "h1_low_barrier",
    "h2_moderate_barrier",
    "h3_high_barrier",
    "h4_future_pressure",
)
RIDGEWAY_HYPOTHESIS_RANKING_METRIC = (
    "Minimum required target-pair resistance reduction (%) within each resistance "
    "hypothesis; rank 1 is the highest score among the 128 directly evaluated portfolios."
)


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_ecocorridor_public_replay.py"
    spec = importlib.util.spec_from_file_location("public_replay_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    root = tmp_path / "dist"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'self'; connect-src 'none'; object-src 'none'; "
        "base-uri 'self'; form-action 'none'; frame-ancestors 'none'\">",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("document.body.dataset.replay = 'synthetic';", encoding="utf-8")
    fixture = tmp_path / "fixture.json"
    projection = tmp_path / "projection.json"
    fixture.write_text(
        """{
  "schemaVersion": "ecocorridor-replay/v1.1",
  "fixtureId": "ridgeway_crossing",
  "evidenceState": "ENGINE_RUN_REQUIRED",
  "releaseReplayEligible": false,
  "releaseBlocker": "Current direct evidence is required.",
  "manifestSha256": null,
  "baselinePortfolioId": null,
  "portfolios": [
    {"id": null, "costUsd": null, "hypothesisRanks": null, "scenarioResults": []},
    {"id": null, "costUsd": null, "hypothesisRanks": null, "scenarioResults": []},
    {"id": null, "costUsd": null, "hypothesisRanks": null, "scenarioResults": []},
    {"id": null, "costUsd": null, "hypothesisRanks": null, "scenarioResults": []}
  ]
}\n""",
        encoding="utf-8",
    )
    projection.write_bytes(fixture.read_bytes())
    trust = tmp_path / "release-trust.json"
    trust.write_text(
        '{"schemaVersion":"ecocorridor-release-trust/v1.0",'
        '"fixtureId":"ridgeway_crossing","trusted":false,'
        '"evidenceManifestSha256":null}\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "data-manifest.json"
    manifest.write_text('{"manifest_version":"1.0"}\n', encoding="utf-8")
    return root, fixture, projection, trust, manifest


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


def _verified_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    baseline_id = f"portfolio_{'0' * 20}"
    scenarios = [{"id": f"scenario_{index:02d}", "includedPairs": 2} for index in range(12)]
    portfolio_ids = [f"portfolio_{index:020x}" for index in range(1, 5)]
    current_rasters = []
    for portfolio_id in [baseline_id, *portfolio_ids]:
        for scenario in scenarios:
            current_rasters.append(
                {
                    "runId": f"map_{portfolio_id}_{scenario['id']}",
                    "portfolioId": portfolio_id,
                    "scenarioId": scenario["id"],
                    "sha256": "c" * 64,
                    "width": 2,
                    "height": 1,
                    "cells": [0.0, 1.0],
                }
            )
    portfolios = []
    for portfolio_id in portfolio_ids:
        results = []
        for scenario in scenarios:
            scenario_id = scenario["id"]
            results.append(
                {
                    "scenarioId": scenario_id,
                    "runId": f"matrix_{portfolio_id}_{scenario_id}",
                    "baselineMapRunId": f"map_{baseline_id}_{scenario_id}",
                    "portfolioMapRunId": f"map_{portfolio_id}_{scenario_id}",
                    "pairResults": [
                        {
                            "pairId": f"pair_{pair_index}",
                            "claimId": (f"ECO-PAIR-{portfolio_id}-{scenario_id}-pair_{pair_index}"),
                            "baselineRunId": f"matrix_{baseline_id}_{scenario_id}",
                            "portfolioRunId": f"matrix_{portfolio_id}_{scenario_id}",
                            "baselineMatrixSha256": "a" * 64,
                            "portfolioMatrixSha256": "b" * 64,
                        }
                        for pair_index in range(2)
                    ],
                }
            )
        portfolios.append(
            {
                "id": portfolio_id,
                "hypothesisRanks": {
                    hypothesis_id: {"rank": index + 1, "score": 70.0 - index}
                    for index, hypothesis_id in enumerate(RIDGEWAY_HYPOTHESIS_IDS)
                },
                "scenarioResults": results,
            }
        )
    manifest_sha = "d" * 64
    payload = {
        "schemaVersion": "ecocorridor-replay/v1.2",
        "fixtureId": "ridgeway_crossing",
        "evidenceState": "VERIFIED_DIRECT_EVIDENCE",
        "releaseReplayEligible": True,
        "manifestSha256": manifest_sha,
        "baselinePortfolioId": baseline_id,
        "study": {
            "portfolioCount": 128,
            "scenarioCount": 12,
            "matrixJobCount": 1536,
            "mapJobCount": 60,
            "hypothesisRankingMetric": RIDGEWAY_HYPOTHESIS_RANKING_METRIC,
        },
        "scenarios": scenarios,
        "portfolios": portfolios,
        "currentRasters": current_rasters,
        "reviewMask": {"maskId": "road-review-mask"},
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)
    trust.write_text(
        json.dumps(
            {
                "schemaVersion": "ecocorridor-release-trust/v1.0",
                "fixtureId": "ridgeway_crossing",
                "trusted": True,
                "evidenceManifestSha256": manifest_sha,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source_sha = "a" * 40
    differential_path = root / "ridgeway-protected-rerun-differential.json"
    differential_path.write_text(
        json.dumps(
            {
                "schema_version": "ridgeway-protected-rerun-differential/v1.1",
                "source_commit_sha": source_sha,
                "status": "PASSED",
                "reference_manifest_sha256": manifest_sha,
                "matrix_job_count": 1_536,
                "map_job_count": 60,
                "absolute_tolerance": 1e-8,
                "relative_tolerance": 1e-7,
                "teaching_selection_matched": True,
                "reference_retained_process_interval_overlap": _interval_overlap(4),
                "producer_retained_process_interval_overlap": _interval_overlap(8),
                "retained_process_interval_overlap_comparison_policy": (
                    "AUDIT_ONLY_EXCLUDED_FROM_SCIENTIFIC_EQUIVALENCE"
                ),
            }
        ),
        encoding="utf-8",
    )
    differential_sha = hashlib.sha256(differential_path.read_bytes()).hexdigest()
    (root / "ridgeway-protected-producer-attestation.json").write_text(
        json.dumps(
            {
                "schema_version": "ridgeway-protected-producer-attestation/v1.0",
                "source_commit_sha": source_sha,
                "execution_contract": "FULL_DIRECT_CIRCUITSCAPE_RASTER_PAIRWISE_RERUN",
                "fixture_id": "ridgeway_crossing",
                "evidence_disposition": "VERIFIED_DIRECT_EVIDENCE",
                "direct_evidence_manifest_sha256": manifest_sha,
                "matrix_job_count": 1_536,
                "map_job_count": 60,
                "archive_sha256": "1" * 64,
                "producer_archive_sha256": "2" * 64,
                "producer_direct_evidence_manifest_sha256": "3" * 64,
                "producer_engine_manifest_sha256": "4" * 64,
                "producer_workflow_manifest_sha256": "5" * 64,
                "protected_rerun_differential_sha256": differential_sha,
            }
        ),
        encoding="utf-8",
    )
    identity = {
        "schema_version": "ecocorridor-github-actions-producer-identity/v1.0",
        "repository": "example/ecocorridor",
        "run_id": 123,
        "run_attempt": 1,
        "workflow_path": ".github/workflows/ecocorridor-ridgeway-evidence.yml",
        "head_sha": source_sha,
    }
    (root / "ridgeway-producer-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (root / "ridgeway-producer-provenance.json").write_text(
        json.dumps(
            {
                "schema_version": "ecocorridor-github-actions-provenance/v1.0",
                "repository": identity["repository"],
                "run_id": identity["run_id"],
                "run_attempt": identity["run_attempt"],
                "workflow_path": identity["workflow_path"],
                "head_sha": source_sha,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "conclusion": "success",
                "artifact": {
                    "id": 456,
                    "name": "ecocorridor-ridgeway-protected-evidence",
                    "digest": f"sha256:{'6' * 64}",
                },
            }
        ),
        encoding="utf-8",
    )
    return root, fixture, projection, trust, manifest


def test_public_replay_inventory_is_static_and_source_bound(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    payload = checker.validate_and_inventory(
        root,
        source_sha="a" * 40,
        fixture=fixture,
        projection=projection,
        trust_record=trust,
        data_manifest=manifest,
    )
    assert payload["artifact_kind"] == "CC0_SYNTHETIC_PUBLIC_REPLAY"
    assert payload["file_count"] == 2
    assert {item["path"] for item in payload["files"]} == {"index.html", "assets/app.js"}


def test_public_replay_rejects_prohibited_copy_even_when_evidence_is_withheld(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    payload = json.loads(projection.read_text(encoding="utf-8"))
    payload["portfolios"][0]["name"] = "Robust but over budget"
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)

    with pytest.raises(ValueError, match="violates ecological claim policy"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


def test_public_replay_source_graph_excludes_desktop_application_and_host_bridge() -> None:
    repository = Path(__file__).resolve().parents[2]
    source_root = repository / "apps/ecocorridor-replay/src"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(source_root.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )
    tsconfig = json.loads(
        (repository / "apps/ecocorridor-replay/tsconfig.json").read_text(encoding="utf-8")
    )

    assert "ecocorridor-desktop" not in source
    assert "scenario-platform" not in source
    assert 'from "../../../benchmarks/ecocorridor/ridgeway/ui_projection.json"' in source
    assert tsconfig["include"] == ["src"]
    for runtime_marker in (
        "hostRequest",
        "__ECOCORRIDOR_HOST__",
        "@tauri-apps/",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
    ):
        assert runtime_marker not in source


def test_public_replay_source_copy_passes_the_strict_ecological_claim_policy(
    checker: ModuleType,
) -> None:
    checker.validate_public_replay_source_copy()


def test_redacted_story_source_is_bound_to_the_reviewed_manifest_and_projection(
    checker: ModuleType,
) -> None:
    checker.validate_redacted_story_source_binding()


def test_redacted_story_source_binding_rejects_a_stale_projection_digest(
    checker: ModuleType,
    tmp_path: Path,
) -> None:
    source = tmp_path / "index.ts"
    source.write_text(checker.REDACTED_STORY_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    altered_projection = tmp_path / "ridgewayFixture.json"
    altered_projection.write_text('{"fixtureId":"altered"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="PINNED_PROJECTION_SHA256"):
        checker.validate_redacted_story_source_binding(
            source,
            checker.RIDGEWAY_DIRECT_EVIDENCE_MANIFEST,
            altered_projection,
        )


def test_redacted_story_source_binding_requires_runtime_projection_comparison(
    checker: ModuleType,
    tmp_path: Path,
) -> None:
    source = tmp_path / "index.ts"
    source.write_text(
        checker.REDACTED_STORY_SOURCE.read_text(encoding="utf-8").replace(
            "await sha256(stableJson(fixture)) !== PINNED_PROJECTION_VALUE_SHA256",
            "false",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime does not compare"):
        checker.validate_redacted_story_source_binding(source)


@pytest.mark.parametrize(
    "unsafe_copy",
    [
        "This is a robust candidate.",
        "This portfolio is ecologically robust.",
        "This is the recommended corridor.",
        "This option reconnects habitat.",
    ],
)
def test_public_replay_source_copy_rejects_prohibited_paraphrases(
    checker: ModuleType,
    tmp_path: Path,
    unsafe_copy: str,
) -> None:
    source = tmp_path / "PublicReplayApp.tsx"
    source.write_text(f"export const App = () => <p>{unsafe_copy}</p>;\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source copy violates ecological claim policy"):
        checker.validate_public_replay_source_copy(source)


def test_public_replay_document_declares_no_cloud_csp_and_local_assets() -> None:
    repository = Path(__file__).resolve().parents[2]
    index = (repository / "apps/ecocorridor-replay/index.html").read_text(encoding="utf-8")
    vite_config = (repository / "apps/ecocorridor-replay/vite.config.ts").read_text(
        encoding="utf-8"
    )

    for directive in (
        "default-src 'self'",
        "connect-src 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        "media-src 'none'",
        "worker-src 'none'",
        "manifest-src 'none'",
    ):
        assert directive in index
    assert "http://" not in index
    assert "https://" not in index
    assert "modulePreload: { polyfill: false }" in vite_config


def test_public_replay_accepts_complete_v12_pair_and_current_raster_evidence(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    payload = checker.validate_and_inventory(
        root,
        source_sha="a" * 40,
        fixture=fixture,
        projection=projection,
        trust_record=trust,
        data_manifest=manifest,
        require_verified_evidence=True,
    )
    assert payload["verified_direct_evidence"] is True
    assert checker.PROTECTED_RIDGEWAY_FILES <= {item["path"] for item in payload["files"]}


def test_public_replay_rejects_verified_fixture_without_the_pinned_rank_metric(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    payload = json.loads(projection.read_text(encoding="utf-8"))
    payload["study"]["hypothesisRankingMetric"] = None
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)

    with pytest.raises(ValueError, match="missing trusted direct evidence"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


@pytest.mark.parametrize(("field", "value"), [("rank", 0), ("rank", 129), ("score", "70")])
def test_public_replay_rejects_invalid_hypothesis_rank_observations(
    checker: ModuleType, tmp_path: Path, field: str, value: object
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    payload = json.loads(projection.read_text(encoding="utf-8"))
    payload["portfolios"][0]["hypothesisRanks"]["h1_low_barrier"][field] = value
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)

    with pytest.raises(ValueError, match="invalid hypothesis-rank evidence"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


def test_public_replay_rejects_incomplete_hypothesis_rank_basis(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    payload = json.loads(projection.read_text(encoding="utf-8"))
    del payload["portfolios"][0]["hypothesisRanks"]["h4_future_pressure"]
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)

    with pytest.raises(ValueError, match="incomplete teaching evidence"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


def test_public_replay_rejects_verified_fixture_without_protected_producer_receipt(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    (root / "ridgeway-producer-provenance.json").unlink()

    with pytest.raises(ValueError, match="omits protected Ridgeway producer evidence"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


def test_public_replay_rejects_impossible_retained_interval_overlap(
    checker: ModuleType,
) -> None:
    impossible = _interval_overlap(1_597)

    assert checker._valid_retained_process_interval_overlap(impossible) is False


def test_public_replay_rejects_incomplete_v12_pair_evidence(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _verified_inputs(tmp_path)
    payload = json.loads(projection.read_text(encoding="utf-8"))
    payload["portfolios"][0]["scenarioResults"][0]["pairResults"].pop()
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    fixture.write_bytes(encoded)
    projection.write_bytes(encoded)
    with pytest.raises(ValueError, match="incomplete focal-pair evidence"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


def test_public_replay_inventories_only_the_named_producer_identity_json(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    identity = root / "ecocorridor-pages-producer-identity.json"
    identity.write_text('{"schema_version":"producer-test"}\n', encoding="utf-8")

    payload = checker.validate_and_inventory(
        root,
        source_sha="a" * 40,
        fixture=fixture,
        projection=projection,
        trust_record=trust,
        data_manifest=manifest,
    )

    assert "ecocorridor-pages-producer-identity.json" in {item["path"] for item in payload["files"]}


@pytest.mark.parametrize(
    "name", ["project.sqlite", "resistance.tif", "evidence.tar.gz", "raw.json"]
)
def test_public_replay_rejects_professional_or_uninventoried_files(
    checker: ModuleType, tmp_path: Path, name: str
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    (root / name).write_bytes(b"must not publish")
    with pytest.raises(ValueError, match="unapproved file type"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


@pytest.mark.parametrize("name", ["unclassified-location-layer.png", "unreviewed-icon.ico"])
def test_public_replay_rejects_unclassified_binary_images(
    checker: ModuleType, tmp_path: Path, name: str
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    (root / "assets" / name).write_bytes(b"unclassified binary payload")

    with pytest.raises(ValueError, match="unapproved file type"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


def test_public_replay_rejects_font_bytes_outside_locked_font_packages(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    (root / "assets" / "unreviewed-location-font.woff2").write_bytes(
        b"not a locked font package file"
    )

    with pytest.raises(ValueError, match="exact locked source package"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


def test_public_replay_records_exact_locked_font_source(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    source = (
        checker.PUBLIC_REPLAY_APP
        / "node_modules/@fontsource/atkinson-hyperlegible/files/atkinson-hyperlegible-latin-400-normal.woff2"
    )
    emitted = root / "assets" / "atkinson-hyperlegible-latin-400-normal-buildhash.woff2"
    emitted.write_bytes(source.read_bytes())

    payload = checker.validate_and_inventory(
        root,
        source_sha="a" * 40,
        fixture=fixture,
        projection=projection,
        trust_record=trust,
        data_manifest=manifest,
    )

    record = next(
        item for item in payload["files"] if item["path"] == emitted.relative_to(root).as_posix()
    )
    assert record["source_binding"] == {
        "package": "@fontsource/atkinson-hyperlegible",
        "version": "5.3.0",
        "file": "atkinson-hyperlegible-latin-400-normal.woff2",
    }


def test_public_replay_rejects_fixture_drift(checker: ModuleType, tmp_path: Path) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    projection.write_text('{"synthetic":false}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="byte-identical"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


def test_public_replay_rejects_symlinked_assets(checker: ModuleType, tmp_path: Path) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_text("outside", encoding="utf-8")
    (root / "assets" / "outside.js").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe file"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


def test_public_replay_deployment_requires_verified_direct_evidence(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    with pytest.raises(ValueError, match="requires a trusted v1.2 direct-evidence package"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
            require_verified_evidence=True,
        )


def test_public_replay_rejects_stale_trusted_digest_for_unavailable_evidence(
    checker: ModuleType, tmp_path: Path
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    trust.write_text(
        '{"schemaVersion":"ecocorridor-release-trust/v1.0",'
        '"fixtureId":"ridgeway_crossing","trusted":true,'
        f'"evidenceManifestSha256":"{"a" * 64}"}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not fail closed"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )


@pytest.mark.parametrize(
    "desktop_marker",
    [
        "window.__ECOCORRIDOR_HOST__",
        "connectivity.analysis.start",
        "observability.connect",
        "assistant.explain",
        "createProjectWorkspace",
        "chooseConnectivityBundle",
    ],
)
def test_public_replay_rejects_desktop_or_rpc_runtime_code(
    checker: ModuleType, tmp_path: Path, desktop_marker: str
) -> None:
    root, fixture, projection, trust, manifest = _inputs(tmp_path)
    (root / "assets" / "app.js").write_text(desktop_marker, encoding="utf-8")

    with pytest.raises(ValueError, match="private/runtime material"):
        checker.validate_and_inventory(
            root,
            source_sha="a" * 40,
            fixture=fixture,
            projection=projection,
            trust_record=trust,
            data_manifest=manifest,
        )
