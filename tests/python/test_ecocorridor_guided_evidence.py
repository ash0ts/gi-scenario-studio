from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from ecocorridor_core import guided_evidence
from ecocorridor_core.contracts import EngineManifest, canonical_sha256
from ecocorridor_core.guided_evidence import (
    GuidedEvidenceError,
    GuidedEvidenceResource,
    _aggregate_process_tree_exits,
    _aggregate_resource_observability,
    _measure_retained_process_interval_overlap,
    _safe_member_name,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture

ROOT = Path(__file__).resolve().parents[2]
RIDGEWAY = ROOT / "benchmarks" / "ecocorridor" / "ridgeway"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed_resource() -> GuidedEvidenceResource:
    manifest = RIDGEWAY / "direct-evidence-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    projection = RIDGEWAY / "ui_projection.json"
    archive = RIDGEWAY / payload["evidence_bundle"]["filename"]
    return GuidedEvidenceResource.verify(
        projection_path=projection,
        projection_sha256=_sha256(projection),
        manifest_path=manifest,
        manifest_sha256=_sha256(manifest),
        archive_path=archive,
        archive_sha256=_sha256(archive),
    )


def _resource_observation(observation_id: str) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "boundary": "DIRECT_SOLVER",
        "solver_wall_seconds": 2.5,
        "peak_memory_status": "NOT_MEASURED",
        "peak_memory_bytes": None,
        "peak_memory_method": "NOT_MEASURED",
        "energy_status": "NOT_MEASURED",
        "energy_kwh": None,
        "energy_method": "NOT_MEASURED",
        "hardware": "test-only",
        "boundary_description": "One direct test boundary.",
    }


def _process_exit(index: int) -> dict[str, object]:
    process_id = 20_000 + index
    return {
        "process_id": process_id,
        "process_group_id": process_id,
        "process_identity": hashlib.sha256(f"process-{index}".encode()).hexdigest(),
        "launched_at": "2026-08-03T12:00:00+00:00",
        "termination_reason": "natural_exit",
        "tree_exit_confirmed": True,
        "confirmation_method": "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT",
        "confirmed_at": "2026-08-03T12:00:01+00:00",
    }


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _packaged_v11_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Create the smallest full GuidedEvidenceResource envelope contract."""

    monkeypatch.setattr(
        guided_evidence,
        "_FIXED_ROLE_COUNTS",
        {"typed_engine_manifest": 1, "evidence_index": 1},
    )
    monkeypatch.setattr(
        guided_evidence,
        "_FIXED_REQUIRED_COVERAGE",
        {
            "typed_engine_manifests": 1,
            "validated_resource_observations": 1_596,
            "validated_process_tree_exits": 1_596,
            "all_jobs_exact_and_qa_accepted": True,
        },
    )
    fixture = build_ridgeway_fixture()
    environment_components = {
        "julia_executable": "1" * 64,
        "project": "2" * 64,
        "manifest": "3" * 64,
        "wrapper": "4" * 64,
        "entrypoint": "5" * 64,
        "julia_runtime_tree": "6" * 64,
        "native_artifact_tree": "7" * 64,
        "protocol": "8" * 64,
    }
    workflow_components = {
        **environment_components,
        "python_protocol_parser": "9" * 64,
        "metric_definitions": "a" * 64,
    }
    engine = EngineManifest(
        manifest_id="ridgeway-circuitscape-5.17.1-julia-1.12.6",
        executable_sha256=environment_components["julia_executable"],
        julia_runtime_sha256=environment_components["julia_runtime_tree"],
        julia_manifest_sha256=environment_components["manifest"],
        native_artifacts_sha256=environment_components["native_artifact_tree"],
        wrapper_sha256=environment_components["wrapper"],
        parser_sha256=workflow_components["python_protocol_parser"],
        metric_definitions_sha256=workflow_components["metric_definitions"],
        neighbor_rule="EIGHT",
        resistance_average="ARITHMETIC",
        platform="test-platform",
        created_at="2026-08-03T00:00:00Z",
    )
    environment_sha = canonical_sha256(environment_components)
    engine_record = _canonical(
        {
            "schema_version": "ridgeway-engine-environment/v1.0",
            "engine_manifest": engine.model_dump(mode="json"),
            "engine_manifest_sha256": engine.sha256,
            "engine_environment_sha256": environment_sha,
            "engine_environment_components": environment_components,
        }
    )
    matrix_jobs = [
        {
            "resource_observation": _resource_observation(f"matrix-observation-{index}"),
            "process_exit_evidence": _process_exit(index),
        }
        for index in range(1_536)
    ]
    map_jobs = [
        {
            "resource_observation": _resource_observation(f"map-observation-{index}"),
            "process_exit_evidence": _process_exit(1_536 + index),
        }
        for index in range(60)
    ]
    process_exit_summary = guided_evidence._aggregate_process_tree_exits([*matrix_jobs, *map_jobs])
    process_interval_overlap = guided_evidence._measure_retained_process_interval_overlap(
        [*matrix_jobs, *map_jobs]
    )
    evidence = {
        "engine_manifest": engine.model_dump(mode="json"),
        "engine_manifest_sha256": engine.sha256,
        "engine_environment_sha256": environment_sha,
        "workflow_components": workflow_components,
        "process_tree_exit_evidence": process_exit_summary,
        "matrix_jobs": matrix_jobs,
        "map_jobs": map_jobs,
    }
    evidence_bytes = _canonical(evidence)
    resource_summary = _aggregate_resource_observability([*matrix_jobs, *map_jobs])
    workflow_bytes = b"reviewed workflow snapshot\n"
    retained = {
        "evidence-index.json": (evidence_bytes, "evidence_index"),
        "engine-manifest.json": (engine_record, "typed_engine_manifest"),
        "workflow-snapshot/source.py": (workflow_bytes, "workflow_snapshot"),
    }
    files = [
        {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "role": role,
        }
        for path, (payload, role) in sorted(retained.items())
    ]
    portfolio_order = [item.portfolio_id for item in fixture.portfolios]
    scenario_order = [item.scenario_id for item in fixture.bundle.scenarios]
    job_order = [
        f"matrix_{portfolio_id}_{scenario_id}"
        for portfolio_id in portfolio_order
        for scenario_id in scenario_order
    ]
    bundle_index = {
        "schema_version": "ridgeway-evidence-bundle/v1.2",
        "fixture_version": fixture.fixture_version,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "physics_model_sha256": fixture.physics_model_sha256,
        "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
        "cost_basis_sha256": fixture.cost_basis_sha256,
        "assumption_ledger_sha256": fixture.assumption_ledger_sha256,
        "engine_manifest_sha256": engine.sha256,
        "engine_environment_sha256": environment_sha,
        "workflow_manifest_sha256": "b" * 64,
        "claim_safety_sha256": "1" * 64,
        "evidence_index_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "matrix_job_count": 1_536,
        "map_job_count": 60,
        "resource_observability": resource_summary,
        "process_tree_exit_evidence": process_exit_summary,
        "retained_process_interval_overlap": process_interval_overlap,
        "required_coverage": {
            **guided_evidence._FIXED_REQUIRED_COVERAGE,
            "workflow_snapshots": 1,
        },
        "search_trace": {
            "method": "FIXED_128_PORTFOLIO_DIRECT_STUDY",
            "portfolio_order": portfolio_order,
            "scenario_order": scenario_order,
            "job_order_sha256": canonical_sha256(job_order),
            "job_count": 1_536,
        },
        "role_counts": {
            "evidence_index": 1,
            "typed_engine_manifest": 1,
            "workflow_snapshot": 1,
        },
        "files": files,
    }
    index_bytes = _canonical(bundle_index)
    archive_path = tmp_path / "ridgeway-direct-evidence-test.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for path, payload in [
            ("bundle-index.json", index_bytes),
            *((path, payload) for path, (payload, _role) in sorted(retained.items())),
        ]:
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    archive_sha = _sha256(archive_path)
    manifest = {
        "schema_version": "ridgeway-direct-evidence/v1.2",
        "fixture_version": fixture.fixture_version,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "physics_model_sha256": fixture.physics_model_sha256,
        "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
        "cost_basis_sha256": fixture.cost_basis_sha256,
        "assumption_ledger_sha256": fixture.assumption_ledger_sha256,
        "engine_manifest_sha256": engine.sha256,
        "engine_environment_sha256": environment_sha,
        "workflow_manifest_sha256": "b" * 64,
        "claim_safety_sha256": "1" * 64,
        "evidence_index_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "matrix_job_count": 1_536,
        "map_job_count": 60,
        "resource_observability": resource_summary,
        "process_tree_exit_evidence": process_exit_summary,
        "retained_process_interval_overlap": process_interval_overlap,
        "release_replay_eligible": True,
        "teaching_selection": {},
        "evidence_bundle": {
            "filename": archive_path.name,
            "format": "tar+gzip",
            "sha256": archive_sha,
            "byte_size": archive_path.stat().st_size,
            "file_count": 4,
            "internal_index_path": "bundle-index.json",
            "internal_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        },
    }
    selected = fixture.portfolios[1:5]
    manifest["teaching_selection"] = {
        "low_cost_caution_portfolio_id": selected[0].portfolio_id,
        "carry_forward_portfolio_id": selected[1].portfolio_id,
        "over_budget_portfolio_id": selected[2].portfolio_id,
        "hypothesis_collapse_portfolio_id": selected[3].portfolio_id,
    }
    manifest_path = tmp_path / "direct-evidence-manifest.json"
    manifest_path.write_bytes(_canonical(manifest))
    manifest_sha = _sha256(manifest_path)
    hypothesis_ids = [item.hypothesis_id for item in fixture.bundle.resistance_hypotheses]
    portfolios = []
    for portfolio in selected:
        scenario_results = []
        for scenario in fixture.bundle.scenarios:
            scenario_results.append(
                {
                    "scenarioId": scenario.scenario_id,
                    "runId": f"matrix_{portfolio.portfolio_id}_{scenario.scenario_id}",
                    "baselineMapRunId": (
                        f"map_{fixture.baseline_portfolio_id}_{scenario.scenario_id}"
                    ),
                    "portfolioMapRunId": f"map_{portfolio.portfolio_id}_{scenario.scenario_id}",
                    "pairResults": [
                        {
                            "pairId": pair_id,
                            "claimId": (
                                f"ECO-PAIR-{portfolio.portfolio_id}-{scenario.scenario_id}-{pair_id}"
                            ),
                            "baselineRunId": (
                                f"matrix_{fixture.baseline_portfolio_id}_{scenario.scenario_id}"
                            ),
                            "portfolioRunId": (
                                f"matrix_{portfolio.portfolio_id}_{scenario.scenario_id}"
                            ),
                            "baselineMatrixSha256": "c" * 64,
                            "portfolioMatrixSha256": "d" * 64,
                        }
                        for pair_id in scenario.included_pair_ids
                    ],
                }
            )
        portfolios.append(
            {
                "id": portfolio.portfolio_id,
                "hypothesisRanks": {
                    hypothesis_id: {"rank": 1, "score": 1.0} for hypothesis_id in hypothesis_ids
                },
                "scenarioResults": scenario_results,
            }
        )
    map_ids = [
        f"map_{portfolio_id}_{scenario.scenario_id}"
        for portfolio_id in [
            fixture.baseline_portfolio_id,
            *(item.portfolio_id for item in selected),
        ]
        for scenario in fixture.bundle.scenarios
    ]
    projection = {
        "schemaVersion": "ecocorridor-replay/v1.2",
        "fixtureId": fixture.bundle.project_id,
        "baselinePortfolioId": fixture.baseline_portfolio_id,
        "releaseReplayEligible": True,
        "evidenceState": "VERIFIED_DIRECT_EVIDENCE",
        "manifestSha256": manifest_sha,
        "resourceObservability": resource_summary,
        "study": {
            "portfolioCount": 128,
            "scenarioCount": 12,
            "matrixJobCount": 1_536,
            "mapJobCount": 60,
            "hypothesisRankingMetric": guided_evidence.HYPOTHESIS_RANKING_METRIC,
        },
        "portfolios": portfolios,
        "currentRasters": [
            {
                "runId": run_id,
                "width": 1,
                "height": 1,
                "cells": [0.0],
                "sha256": "e" * 64,
                "nodataTopologySha256": "f" * 64,
            }
            for run_id in map_ids
        ],
        "reviewMask": {},
    }
    projection_path = tmp_path / "ui_projection.json"
    projection_path.write_bytes(_canonical(projection))
    return projection_path, manifest_path, archive_path


def test_committed_verified_archive_opens_as_guided_evidence() -> None:
    resource = _committed_resource()
    assert resource.projection["evidenceState"] == "VERIFIED_DIRECT_EVIDENCE"
    assert resource.manifest["release_replay_eligible"] is True
    assert resource.bundle_index["required_coverage"]["all_jobs_exact_and_qa_accepted"] is True


def test_historical_archive_cannot_replace_current_verified_evidence() -> None:
    historical = RIDGEWAY / "ridgeway-direct-evidence-4858c0a48841.tar.gz"
    manifest = RIDGEWAY / "direct-evidence-manifest.json"
    projection = RIDGEWAY / "ui_projection.json"
    with pytest.raises(GuidedEvidenceError, match="BUNDLE_DECLARATION_INVALID"):
        GuidedEvidenceResource.verify(
            projection_path=projection,
            projection_sha256=_sha256(projection),
            manifest_path=manifest,
            manifest_sha256=_sha256(manifest),
            archive_path=historical,
            archive_sha256=_sha256(historical),
        )


def test_packaged_v11_environment_envelope_opens_as_guided_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection, manifest, archive = _packaged_v11_resource(tmp_path, monkeypatch)
    resource = GuidedEvidenceResource.verify(
        projection_path=projection,
        projection_sha256=_sha256(projection),
        manifest_path=manifest,
        manifest_sha256=_sha256(manifest),
        archive_path=archive,
        archive_sha256=_sha256(archive),
    )
    assert resource.manifest["release_replay_eligible"] is True
    assert resource.bundle_index["schema_version"] == "ridgeway-evidence-bundle/v1.2"


def test_archive_member_validation_rejects_parent_traversal() -> None:
    with pytest.raises(GuidedEvidenceError, match="ARCHIVE_PATH_INVALID"):
        _safe_member_name("../escape")


def test_resource_observations_are_independently_aggregated() -> None:
    summary = _aggregate_resource_observability(
        [
            {"resource_observation": _resource_observation("resource:one")},
            {"resource_observation": _resource_observation("resource:two")},
        ]
    )
    assert summary["jobsWithObservations"] == 2
    assert summary["jobsWithoutObservations"] == 0
    assert summary["directSolverWallSeconds"] == 5.0
    assert summary["estimatedEnergyKwh"] is None
    assert summary["energyStatus"] == "NOT_MEASURED"


def test_resource_observations_reject_missing_duplicate_and_non_direct_boundaries() -> None:
    with pytest.raises(GuidedEvidenceError, match="OBSERVATION_MISSING"):
        _aggregate_resource_observability([{}])
    duplicate = _resource_observation("resource:duplicate")
    with pytest.raises(GuidedEvidenceError, match="OBSERVATION_DUPLICATE"):
        _aggregate_resource_observability(
            [{"resource_observation": duplicate}, {"resource_observation": duplicate}]
        )
    proposer = _resource_observation("resource:proposer")
    proposer["boundary"] = "SEARCH_PROPOSER"
    proposer["solver_wall_seconds"] = None
    proposer["proposer_wall_seconds"] = 2.5
    with pytest.raises(GuidedEvidenceError, match="RESOURCE_BOUNDARY_INVALID"):
        _aggregate_resource_observability([{"resource_observation": proposer}])


def test_guided_evidence_requires_unique_confirmed_process_tree_exits() -> None:
    summary = _aggregate_process_tree_exits(
        [
            {"process_exit_evidence": _process_exit(1)},
            {"process_exit_evidence": _process_exit(2)},
        ]
    )
    assert summary["jobs_with_confirmed_tree_exit"] == 2
    assert summary["unique_process_identities"] == 2

    unconfirmed = _process_exit(3)
    unconfirmed["tree_exit_confirmed"] = False
    with pytest.raises(GuidedEvidenceError, match="PROCESS_TREE_EXIT_INVALID"):
        _aggregate_process_tree_exits([{"process_exit_evidence": unconfirmed}])

    duplicate = _process_exit(4)
    with pytest.raises(GuidedEvidenceError, match="PROCESS_TREE_EXIT_INVALID"):
        _aggregate_process_tree_exits(
            [
                {"process_exit_evidence": duplicate},
                {"process_exit_evidence": duplicate},
            ]
        )


def test_guided_evidence_measures_retained_interval_overlap_independent_of_order() -> None:
    intervals = []
    for index, (launched_at, confirmed_at) in enumerate(
        (
            ("2026-08-03T12:00:00+00:00", "2026-08-03T12:00:04+00:00"),
            ("2026-08-03T12:00:01+00:00", "2026-08-03T12:00:04+00:00"),
            ("2026-08-03T12:00:04+00:00", "2026-08-03T12:00:05+00:00"),
        ),
        start=1,
    ):
        evidence = _process_exit(index)
        evidence["launched_at"] = launched_at
        evidence["confirmed_at"] = confirmed_at
        intervals.append({"process_exit_evidence": evidence})

    forward = _measure_retained_process_interval_overlap(intervals)
    reverse = _measure_retained_process_interval_overlap(list(reversed(intervals)))

    assert forward == reverse
    assert forward["maximum_overlapping_retained_process_intervals"] == 3
    assert forward["scientific_identity_role"] == "AUDIT_ONLY_EXCLUDED"
    assert "may understate true process concurrency" in str(forward["limitation"])
    assert "coincident endpoints may overstate" in str(forward["limitation"])
    assert "configured worker count" in str(forward["limitation"])
