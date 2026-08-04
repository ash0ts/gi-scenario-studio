from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_script(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


packager = _load_script("package_ridgeway_evidence.py", "package_ridgeway_evidence")
publisher = _load_script("publish_ridgeway_evidence.py", "ridgeway_evidence_publisher")


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def test_packager_preserves_raw_v11_boundary_and_advances_published_contracts() -> None:
    assert packager.RAW_DIRECT_EVIDENCE_SCHEMA == "ridgeway-direct-evidence/v1.1"
    assert packager.PUBLISHED_DIRECT_EVIDENCE_SCHEMA == "ridgeway-direct-evidence/v1.2"
    assert packager.BUNDLE_SCHEMA == "ridgeway-evidence-bundle/v1.2"


def _published_archive(directory: Path, payload: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(payload).hexdigest()
    archive = directory / f"ridgeway-direct-evidence-{digest[:12]}.tar.gz"
    archive.write_bytes(payload)
    return archive, digest


def _write_active_manifest(directory: Path, archive: Path, digest: str) -> None:
    (directory / "direct-evidence-manifest.json").write_bytes(
        _canonical(
            {
                # Exercise replacement of the currently deployed pre-v1.2 package.
                "schema_version": "ridgeway-direct-evidence/v1.1",
                "release_replay_eligible": True,
                "evidence_bundle": {
                    "format": "tar+gzip",
                    "filename": archive.name,
                    "sha256": digest,
                    "byte_size": archive.stat().st_size,
                },
            }
        )
    )


def test_publication_cleanup_removes_the_prior_active_archive(tmp_path: Path) -> None:
    publish = tmp_path / "publish"
    publish.mkdir()
    previous, previous_digest = _published_archive(publish, b"previous active archive")
    current, _current_digest = _published_archive(publish, b"new active archive")
    _write_active_manifest(publish, previous, previous_digest)

    prior = publisher.prior_active_archive(publish)
    removed = publisher.remove_replaced_active_archive(prior, current_archive=current)

    assert removed is True
    assert not previous.exists()
    assert current.is_file()


def test_publication_cleanup_preserves_unrelated_historical_archives(tmp_path: Path) -> None:
    publish = tmp_path / "publish"
    publish.mkdir()
    previous, previous_digest = _published_archive(publish, b"previous active archive")
    historical_one, _ = _published_archive(publish, b"historical archive one")
    historical_two, _ = _published_archive(publish, b"historical archive two")
    current, _ = _published_archive(publish, b"new active archive")
    _write_active_manifest(publish, previous, previous_digest)

    prior = publisher.prior_active_archive(publish)
    publisher.remove_replaced_active_archive(prior, current_archive=current)

    assert not previous.exists()
    assert current.is_file()
    assert historical_one.read_bytes() == b"historical archive one"
    assert historical_two.read_bytes() == b"historical archive two"


def test_safe_publisher_stages_packaging_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    publish = tmp_path / "published"
    publish.mkdir()
    desktop = tmp_path / "desktop" / "ridgewayFixture.json"
    previous, previous_digest = _published_archive(publish, b"previous active archive")
    historical, _ = _published_archive(publish, b"historical archive")
    _write_active_manifest(publish, previous, previous_digest)

    def fake_package(
        _evidence_root: Path,
        staged_publish: Path,
        staged_fixture: Path,
    ) -> dict[str, object]:
        staged_publish.mkdir(parents=True)
        staged_fixture.parent.mkdir(parents=True)
        current, current_digest = _published_archive(staged_publish, b"current archive")
        (staged_publish / "direct-evidence-manifest.json").write_bytes(
            _canonical(
                {
                    "release_replay_eligible": True,
                    "evidence_bundle": {
                        "format": "tar+gzip",
                        "filename": current.name,
                        "sha256": current_digest,
                        "byte_size": current.stat().st_size,
                    },
                }
            )
        )
        (staged_publish / "ui_projection.json").write_bytes(_canonical({"verified": True}))
        staged_fixture.write_bytes(_canonical({"fixture": "verified"}))
        staged_fixture.with_name("ridgewayReleaseTrust.json").write_bytes(
            _canonical({"trusted": True})
        )
        return {"archive": current.name}

    monkeypatch.setattr(publisher.packager, "package", fake_package)
    result = publisher.publish(evidence, publish, desktop)

    archive_name = Path(str(result["archive"])).name
    assert archive_name.startswith("ridgeway-direct-evidence-")
    assert not previous.exists()
    assert historical.read_bytes() == b"historical archive"
    active = publisher.prior_active_archive(publish)
    assert active is not None and active.path.name == archive_name
    assert json.loads(desktop.read_text(encoding="utf-8")) == {"fixture": "verified"}


def _process_exit(
    index: int,
    *,
    launched_at: str = "2026-08-03T12:00:00+00:00",
    confirmed_at: str = "2026-08-03T12:00:01+00:00",
) -> dict[str, object]:
    process_id = 30_000 + index
    return {
        "process_id": process_id,
        "process_group_id": process_id,
        "process_identity": hashlib.sha256(f"packager-process-{index}".encode()).hexdigest(),
        "launched_at": launched_at,
        "termination_reason": "natural_exit",
        "tree_exit_confirmed": True,
        "confirmation_method": "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT",
        "confirmed_at": confirmed_at,
    }


def _retained_snapshots(
    root: Path,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, object], dict[str, str]]:
    engine_root = root / "engine-snapshot"
    workflow_root = root / "workflow-snapshot"
    engine_files: dict[str, Path] = {}
    workflow_files: dict[str, Path] = {}
    records: dict[str, object] = {}
    components: dict[str, str] = {}

    for relative, source in packager.reviewed_engine_sources().items():
        target = engine_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        engine_files[relative] = target
        component = {
            "Project.toml": "project",
            "Manifest.toml": "manifest",
            "src/EcoCorridorEngine.jl": "wrapper",
            "bin/ecocorridor-engine.jl": "entrypoint",
        }[relative]
        components[component] = packager.sha256_file(source)

    for label, source in packager.reviewed_workflow_sources().items():
        relative = f"{label}{source.suffix or '.bin'}"
        target = workflow_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digest = packager.sha256_file(source)
        workflow_files[relative] = target
        records[label] = {
            "relative_path": f"workflow-snapshot/{relative}",
            "sha256": digest,
        }
        components[label] = digest

    dependency = packager.reviewed_dependency_snapshot()
    dependency_target = workflow_root / "python-dependency-versions.json"
    dependency_target.write_bytes(dependency)
    dependency_sha = hashlib.sha256(dependency).hexdigest()
    workflow_files[dependency_target.name] = dependency_target
    records["python_dependency_versions"] = {
        "relative_path": "workflow-snapshot/python-dependency-versions.json",
        "sha256": dependency_sha,
    }
    components["python_dependency_versions"] = dependency_sha
    return engine_files, workflow_files, records, components


def test_packager_binds_retained_snapshots_to_reviewed_sources(tmp_path: Path) -> None:
    engine, workflow, records, components = _retained_snapshots(tmp_path)
    packager.verify_snapshots_against_reviewed_sources(
        engine_files=engine,
        workflow_files=workflow,
        workflow_records=records,
        components=components,
    )

    workflow["study_orchestrator.py"].write_text("# substituted workflow\n", encoding="utf-8")
    substituted_sha = packager.sha256_file(workflow["study_orchestrator.py"])
    records["study_orchestrator"] = {
        "relative_path": "workflow-snapshot/study_orchestrator.py",
        "sha256": substituted_sha,
    }
    components["study_orchestrator"] = substituted_sha
    with pytest.raises(packager.PackagingError, match="workflow snapshot differs"):
        packager.verify_snapshots_against_reviewed_sources(
            engine_files=engine,
            workflow_files=workflow,
            workflow_records=records,
            components=components,
        )


def test_packager_rejects_self_declared_engine_source_substitution(tmp_path: Path) -> None:
    engine, workflow, records, components = _retained_snapshots(tmp_path)
    engine["src/EcoCorridorEngine.jl"].write_text(
        "module EcoCorridorEngine\nend\n", encoding="utf-8"
    )
    components["wrapper"] = packager.sha256_file(engine["src/EcoCorridorEngine.jl"])
    with pytest.raises(packager.PackagingError, match="engine snapshot differs"):
        packager.verify_snapshots_against_reviewed_sources(
            engine_files=engine,
            workflow_files=workflow,
            workflow_records=records,
            components=components,
        )


def test_packager_binds_and_rejects_tampered_claim_safety_dependency(tmp_path: Path) -> None:
    snapshots = packager.ensure_reviewed_report_snapshots(tmp_path)
    claim_snapshot, claim_digest, role = snapshots["claim_safety.py"]
    assert role == "report_claim_safety_snapshot"
    assert packager.sha256_file(claim_snapshot) == claim_digest

    claim_snapshot.write_text("# substituted claim policy\n", encoding="utf-8")
    with pytest.raises(packager.PackagingError, match="claim_safety.py"):
        packager.ensure_reviewed_report_snapshots(tmp_path)


def test_packager_pins_reviewed_ledgers_in_public_manifest() -> None:
    manifest: dict[str, object] = {}
    assumption_sha = "a" * 64
    cost_sha = "b" * 64

    packager.pin_public_ledger_identities(
        manifest,
        assumption_ledger_sha256=assumption_sha,
        cost_basis_sha256=cost_sha,
    )

    assert manifest == {
        "assumption_ledger_sha256": assumption_sha,
        "cost_basis_sha256": cost_sha,
    }
    with pytest.raises(packager.PackagingError, match="lowercase SHA-256"):
        packager.pin_public_ledger_identities(
            {},
            assumption_ledger_sha256="not-a-digest",
            cost_basis_sha256=cost_sha,
        )


def test_packager_requires_full_canonical_indexed_metric_shape() -> None:
    from ecocorridor_core.engine import ProtocolPairMetric

    matrix_metrics = [
        ProtocolPairMetric(
            pair_id="pair_1",
            from_focal_id=1,
            to_focal_id=2,
            effective_resistance=12.5,
        )
    ]
    canonical = [
        {
            "pair_id": "pair_1",
            "effective_resistance": 12.5,
            "baseline_effective_resistance": None,
            "baseline_relative_change_percent": None,
        }
    ]
    packager.verify_indexed_pair_metrics(
        canonical,
        matrix_metrics,
        direct_run_id="matrix_portfolio_scenario",
    )

    for tampered in (
        [{"pair_id": "pair_1", "effective_resistance": 12.5}],
        [dict(canonical[0], effective_resistance=12.6)],
        [dict(canonical[0], baseline_effective_resistance=12.5)],
        [dict(canonical[0], undocumented_field="not allowed")],
    ):
        with pytest.raises(packager.PackagingError, match="indexed metrics differ"):
            packager.verify_indexed_pair_metrics(
                tampered,
                matrix_metrics,
                direct_run_id="matrix_portfolio_scenario",
            )


def test_packager_retains_hash_bound_raw_inputs_for_semantic_recomputation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    public = root / "public"
    public.mkdir(parents=True)
    direct = public / "direct-evidence-manifest.json"
    projection = public / "ui_projection.json"
    direct.write_bytes(_canonical({"schema_version": "ridgeway-direct-evidence/v1.1"}))
    projection.write_bytes(_canonical({"schemaVersion": "ecocorridor-replay/v1.1"}))
    direct_sha = packager.sha256_file(direct)
    projection_sha = packager.sha256_file(projection)

    members: dict[str, object] = {}
    packager.add_raw_producer_inputs(
        members,
        evidence_root=root,
        direct_manifest_path=direct,
        raw_projection_path=projection,
        direct_manifest_sha256=direct_sha,
        raw_projection_sha256=projection_sha,
    )
    assert members["public/direct-evidence-manifest.json"].role == ("raw_direct_evidence_manifest")
    assert members["public/ui_projection.json"].role == "raw_ui_projection"

    projection.write_bytes(_canonical({"schemaVersion": "tampered"}))
    with pytest.raises(packager.PackagingError, match="artifact hash mismatch"):
        packager.add_raw_producer_inputs(
            {},
            evidence_root=root,
            direct_manifest_path=direct,
            raw_projection_path=projection,
            direct_manifest_sha256=direct_sha,
            raw_projection_sha256=projection_sha,
        )


def _job(*, phase: str, value: float) -> dict[str, object]:
    return {
        "phase": phase,
        "portfolio_id": "portfolio_0123456789abcdef0123",
        "scenario_id": "scenario_1",
        "physics_model_sha256": "1" * 64,
        "decision_snapshot_sha256": "2" * 64,
        "engine_manifest_sha256": "3" * 64,
        "engine_environment_sha256": "4" * 64,
        "workflow_manifest_sha256": "5" * 64,
        "prepared_resistance_sha256": "6" * 64,
        "action_raster_sha256": "7" * 64,
        "footprint_proof_sha256": "8" * 64,
        "footprint_proof_document_sha256": "9" * 64,
        "pair_metrics": [{"pair_id": "pair_1", "effective_resistance": value}],
    }


def test_packager_requires_map_matrix_reproduction_within_declared_tolerance() -> None:
    matrix = _job(phase="matrix", value=10.0)
    close_map = _job(
        phase="map",
        value=10.0 + packager.MATRIX_ABSOLUTE_TOLERANCE + packager.MATRIX_RELATIVE_TOLERANCE * 10.0,
    )
    summary = packager.verify_map_matrix_coherence([matrix], [close_map])
    assert summary["status"] == "WITHIN_TOLERANCE"
    assert summary["map_jobs_compared"] == 1
    assert summary["pair_values_compared"] == 1

    changed_map = _job(phase="map", value=10.01)
    with pytest.raises(packager.PackagingError, match="outside tolerance"):
        packager.verify_map_matrix_coherence([matrix], [changed_map])


def test_packager_requires_map_matrix_provenance_identity() -> None:
    matrix = _job(phase="matrix", value=10.0)
    changed_map = _job(phase="map", value=10.0)
    changed_map["engine_environment_sha256"] = "a" * 64
    with pytest.raises(packager.PackagingError, match="engine_environment_sha256"):
        packager.verify_map_matrix_coherence([matrix], [changed_map])


def test_packager_independently_requires_confirmed_unique_process_tree_exits() -> None:
    jobs = [
        {"direct_run_id": "run-1", "process_exit_evidence": _process_exit(1)},
        {"direct_run_id": "run-2", "process_exit_evidence": _process_exit(2)},
    ]
    summary = packager.independently_validate_process_tree_exits(jobs)
    assert summary == {
        "jobs_with_confirmed_tree_exit": 2,
        "jobs_without_confirmed_tree_exit": 0,
        "unique_process_identities": 2,
        "confirmation_methods": {
            "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT": 2,
        },
    }

    unconfirmed = _process_exit(3)
    unconfirmed["tree_exit_confirmed"] = False
    with pytest.raises(packager.PackagingError, match="process-tree exit evidence is invalid"):
        packager.independently_validate_process_tree_exits(
            [{"direct_run_id": "run-3", "process_exit_evidence": unconfirmed}]
        )

    duplicate = _process_exit(4)
    with pytest.raises(packager.PackagingError, match="reuse a process-execution identity"):
        packager.independently_validate_process_tree_exits(
            [
                {"direct_run_id": "run-4", "process_exit_evidence": duplicate},
                {"direct_run_id": "run-5", "process_exit_evidence": duplicate},
            ]
        )


def test_packager_derives_retained_interval_overlap_without_inventing_worker_count() -> None:
    jobs = [
        {
            "direct_run_id": "run-1",
            "process_exit_evidence": _process_exit(
                1,
                launched_at="2026-08-03T12:00:00+00:00",
                confirmed_at="2026-08-03T12:00:04+00:00",
            ),
        },
        {
            "direct_run_id": "run-2",
            "process_exit_evidence": _process_exit(
                2,
                launched_at="2026-08-03T12:00:01+00:00",
                confirmed_at="2026-08-03T12:00:04+00:00",
            ),
        },
        {
            "direct_run_id": "run-3",
            "process_exit_evidence": _process_exit(
                3,
                launched_at="2026-08-03T12:00:04+00:00",
                confirmed_at="2026-08-03T12:00:05+00:00",
            ),
        },
    ]

    forward = packager.independently_measure_retained_process_interval_overlap(jobs)
    completion_order_changed = packager.independently_measure_retained_process_interval_overlap(
        [jobs[2], jobs[0], jobs[1]]
    )

    assert forward == completion_order_changed
    assert forward["maximum_overlapping_retained_process_intervals"] == 3
    assert forward["retained_interval_count"] == 3
    assert forward["scientific_identity_role"] == "AUDIT_ONLY_EXCLUDED"
    assert "configured_worker_count" not in forward
    assert "may understate true process concurrency" in str(forward["limitation"])
    assert "coincident endpoints may overstate" in str(forward["limitation"])
    assert "configured worker count" in str(forward["limitation"])
