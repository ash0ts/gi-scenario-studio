from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ecocorridor_core.analysis_executor as analysis_executor_module
import pytest
from ecocorridor_core.analysis_executor import (
    CurrentMapQAError,
    DurableAnalysisExecutor,
)
from ecocorridor_core.contracts import (
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    QAStatus,
)
from ecocorridor_core.guided_evidence import GuidedEvidenceResource
from ecocorridor_core.guided_rerun import (
    build_differential_report,
    build_pinned_reference,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from scenario_platform.contracts import AnalysisState

ROOT = Path(__file__).resolve().parents[2]
RIDGEWAY = ROOT / "benchmarks" / "ecocorridor" / "ridgeway"
FIXED_TIME = datetime(2026, 8, 3, tzinfo=UTC)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def pinned_reference(tmp_path_factory: pytest.TempPathFactory):
    manifest_path = RIDGEWAY / "direct-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or manifest.get("release_replay_eligible") is not True
    ):
        pytest.skip(
            "guided rerun differential integration requires the regenerated v1.2 direct-evidence package"
        )
    archive_path = RIDGEWAY / manifest["evidence_bundle"]["filename"]
    projection_path = RIDGEWAY / "ui_projection.json"
    resource = GuidedEvidenceResource.verify(
        projection_path=projection_path,
        projection_sha256=_sha(projection_path),
        manifest_path=manifest_path,
        manifest_sha256=_sha(manifest_path),
        archive_path=archive_path,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )
    reference = build_pinned_reference(
        resource,
        build_ridgeway_fixture(),
        materialize_parent=tmp_path_factory.mktemp("ridgeway-reference"),
    )
    return resource, reference


def _engine_manifest(executable_sha256: str = "0" * 64) -> EngineManifest:
    return EngineManifest(
        manifest_id="guided-test-engine",
        executable_sha256=executable_sha256,
        julia_runtime_sha256="1" * 64,
        julia_manifest_sha256="2" * 64,
        native_artifacts_sha256="3" * 64,
        wrapper_sha256="4" * 64,
        parser_sha256="5" * 64,
        metric_definitions_sha256="6" * 64,
        neighbor_rule="EIGHT",
        resistance_average="ARITHMETIC",
        platform="test",
        created_at=FIXED_TIME,
    )


def _plan(analysis_id: str, manifest: EngineManifest) -> ConnectivityRunPlan:
    fixture = build_ridgeway_fixture()
    return ConnectivityRunPlan(
        analysis_id=analysis_id,
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=fixture.portfolios,
        baseline_portfolio_id=fixture.baseline_portfolio_id,
        direct_job_count=1_536,
        solver_job_cap=1_536,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[],
        engine_manifest_sha256=manifest.sha256,
    )


def test_pinned_reference_contains_only_complete_direct_evidence(
    pinned_reference,
) -> None:
    _, reference = pinned_reference
    fixture = build_ridgeway_fixture()
    assert len(reference["matrix"]) == 1_536
    assert len(reference["map"]) == 60
    assert reference["model_sha256"] == fixture.bundle.revision.model_sha256
    assert reference["teaching_selection"]["map_rerun_portfolio_ids"][0] == (
        fixture.baseline_portfolio_id
    )
    assert len(set(reference["teaching_selection"]["map_rerun_portfolio_ids"])) == 5
    assert len(reference["reference_sha256"]) == 64


def test_guided_start_persists_baseline_first_1536_job_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_reference
) -> None:
    resource, reference = pinned_reference
    fixture = build_ridgeway_fixture()
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    engine.chmod(0o700)
    digest = _sha(engine)
    manifest = _engine_manifest(digest)
    project = tmp_path / "guided"
    store = EcoProjectStore(project)
    plan = _plan("analysis-guided", manifest)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="GUIDED",
            skeleton={"fixture_version": fixture.fixture_version},
        )
        store.store_bundle(fixture.bundle)
        store.set_guided("guided_ridgeway_v1", "VERIFIED_FIXTURE_REPLAY", {"fixture": True})
        store.create_analysis(plan, bundle=fixture.bundle)
    finally:
        store.close()

    launched: list[tuple[Path, str, Path]] = []
    monkeypatch.setattr(
        analysis_executor_module,
        "build_pinned_reference",
        lambda *_args, **_kwargs: reference,
    )
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    monkeypatch.setattr(
        executor,
        "_launch_guided",
        lambda directory, analysis_id, executable: launched.append(
            (directory, analysis_id, executable)
        ),
    )
    assert executor.start_guided(
        project_directory=project,
        bundle=fixture.bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
        session_id="guided_ridgeway_v1",
        guided_evidence=resource,
    ) == {"state": "RUNNING"}
    store = EcoProjectStore(project)
    try:
        jobs = store.direct_jobs(plan.analysis_id)
        assert len(jobs) == 1_536
        assert [item["sequence_number"] for item in jobs] == list(range(1_536))
        assert {item["phase"] for item in jobs} == {"MATRIX"}
        assert not any(item["emit_current_map"] for item in jobs)
        first = jobs[:12]
        assert {item["portfolio_id"] for item in first} == {fixture.baseline_portfolio_id}
        assert [item["scenario_id"] for item in first] == sorted(
            item.scenario_id for item in fixture.bundle.scenarios
        )
        metadata = store.get_record("guided_rerun", plan.analysis_id)
        assert metadata["phase"] == "MATRIX"
        assert metadata["map_jobs"] == 60
        assert store.guided("guided_ridgeway_v1")["state"] == "RUNNING"
        map_plan = executor._create_guided_map_analysis(
            store=store,
            fixture=fixture,
            metadata=metadata,
        )
        map_jobs = store.direct_jobs(map_plan.analysis_id)
        assert len(map_jobs) == 60
        assert all(item["emit_current_map"] for item in map_jobs)
        assert [item["portfolio_id"] for item in map_jobs[:12]] == [
            fixture.baseline_portfolio_id
        ] * 12
        assert [item["scenario_id"] for item in map_jobs[:12]] == sorted(
            item.scenario_id for item in fixture.bundle.scenarios
        )
        assert [item.portfolio_id for item in map_plan.portfolios] == reference[
            "teaching_selection"
        ]["map_rerun_portfolio_ids"]
    finally:
        store.close()
    assert launched == [(project.resolve(), plan.analysis_id, engine.resolve())]
    assert executor.guided_status(project_directory=project, analysis_id=plan.analysis_id) == {
        "session_id": "guided_ridgeway_v1",
        "analysis_id": plan.analysis_id,
        "state": "RUNNING",
        "phase": "MATRIX",
        "completed_jobs": 0,
        "total_jobs": 1_596,
        "matrix_completed": 0,
        "map_completed": 0,
        "cached_jobs": 0,
        "differential": None,
    }
    # ``_launch_guided`` is replaced above, so no background thread exists to
    # release the process lock. Model the original worker process exiting
    # before it claims a solver job, then construct the recovery worker. A
    # RUNNING solver attempt would correctly require a confirmed shutdown
    # receipt and must not be silently requeued by this path.
    original_control = executor._controls[executor._key(project, plan.analysis_id)]
    executor._release_project_control(original_control)
    recovered_launches: list[tuple[Path, str, Path]] = []
    recovered = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    monkeypatch.setattr(
        recovered,
        "_launch_guided",
        lambda directory, analysis_id, executable: recovered_launches.append(
            (directory, analysis_id, executable)
        ),
    )
    recovered.recover(
        project_directory=project,
        analysis_id=plan.analysis_id,
        engine_executable=engine,
        engine_sha256=digest,
    )
    store = EcoProjectStore(project)
    try:
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 1_536}
    finally:
        store.close()
    assert recovered_launches == [(project.resolve(), plan.analysis_id, engine.resolve())]
    recovered_control = recovered._controls[recovered._key(project, plan.analysis_id)]
    recovered._release_project_control(recovered_control)

    # Simulate the narrow crash window after the matrix completion transaction
    # committed but before the guided phase advanced to MAP. Recovery must
    # relaunch the guided coordinator without trying to requeue completed jobs.
    store = EcoProjectStore(project)
    try:
        store.transition_analysis(plan.analysis_id, AnalysisState.COMPLETED)
    finally:
        store.close()
    barrier_launches: list[tuple[Path, str, Path]] = []
    barrier_recovery = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    monkeypatch.setattr(
        barrier_recovery,
        "_launch_guided",
        lambda directory, analysis_id, executable: barrier_launches.append(
            (directory, analysis_id, executable)
        ),
    )
    barrier_recovery.recover(
        project_directory=project,
        analysis_id=plan.analysis_id,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert barrier_launches == [(project.resolve(), plan.analysis_id, engine.resolve())]


def _run(
    *,
    analysis_id: str,
    portfolio_id: str,
    scenario_id: str,
    pairs: dict[str, float],
    current_sha256: str | None = None,
) -> DirectConnectivityRun:
    identity = hashlib.sha256(f"{analysis_id}:{portfolio_id}:{scenario_id}".encode()).hexdigest()
    return DirectConnectivityRun(
        direct_run_id=f"direct_{identity[:24]}",
        analysis_id=analysis_id,
        revision_id=build_ridgeway_fixture().bundle.revision.revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256="a" * 64,
        model_run_cache_key=identity,
        input_sha256="b" * 64,
        result_sha256="c" * 64,
        matrix_sha256="d" * 64,
        current_map_sha256=current_sha256,
        pair_metrics=[
            PairResistanceMetric(pair_id=pair_id, effective_resistance=value)
            for pair_id, value in sorted(pairs.items())
        ],
        qa_status=QAStatus.ACCEPTED,
        completed_at=FIXED_TIME,
    )


def _current_ascii(values: list[list[float]], nodata: float) -> str:
    return "\n".join(
        [
            "ncols 20",
            "nrows 12",
            "xllcorner 0",
            "yllcorner 0",
            "cellsize 25",
            f"NODATA_value {nodata:.12g}",
            *(" ".join(f"{float(value):.17g}" for value in row) for row in values),
            "",
        ]
    )


class _DifferentialStore:
    def __init__(
        self,
        root: Path,
        matrix_runs: list[DirectConnectivityRun],
        map_runs: list[DirectConnectivityRun],
        artifacts: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.root = root
        self._runs = {"matrix-local": matrix_runs, "map-local": map_runs}
        self._artifacts = artifacts

    def analysis_results(self, analysis_id: str):
        return self._runs[analysis_id], [], []

    def run_artifacts(self, direct_run_id: str):
        return self._artifacts[direct_run_id]


def test_differential_compares_all_direct_values_and_current_cells(
    tmp_path: Path, pinned_reference
) -> None:
    _, reference = pinned_reference
    matrix_runs = [
        _run(
            analysis_id="matrix-local",
            portfolio_id=item["portfolio_id"],
            scenario_id=item["scenario_id"],
            pairs=item["pair_metrics"],
        )
        for _, item in sorted(reference["matrix"].items())
    ]
    map_runs: list[DirectConnectivityRun] = []
    artifacts: dict[str, list[dict[str, Any]]] = {}
    blob_root = tmp_path / "blobs"
    blob_root.mkdir()
    for index, (_, item) in enumerate(sorted(reference["map"].items())):
        path = blob_root / f"current-{index:03d}.asc"
        path.write_text(
            _current_ascii(item["current_values"], float(item["current_nodata"])),
            encoding="utf-8",
        )
        digest = _sha(path)
        run = _run(
            analysis_id="map-local",
            portfolio_id=item["portfolio_id"],
            scenario_id=item["scenario_id"],
            pairs=item["pair_metrics"],
            current_sha256=digest,
        )
        map_runs.append(run)
        artifacts[run.direct_run_id] = [
            {
                "role": "current_map",
                "sha256": digest,
                "byte_size": path.stat().st_size,
                "cas_relative_path": path.relative_to(tmp_path).as_posix(),
                "media_type": "text/plain",
            }
        ]
    store = _DifferentialStore(tmp_path, matrix_runs, map_runs, artifacts)
    report = build_differential_report(
        store=store,  # type: ignore[arg-type]
        fixture=build_ridgeway_fixture(),
        matrix_analysis_id="matrix-local",
        map_analysis_id="map-local",
        local_engine_manifest_sha256="a" * 64,
        reference=reference,
    )
    assert report["accepted"] is True
    assert report["matrix"]["jobs_compared"] == 1_536
    assert report["map_pairs"]["jobs_compared"] == 60
    assert report["current_maps"]["values_compared"] == 60 * 20 * 12

    first = matrix_runs[0]
    changed = list(first.pair_metrics)
    changed[0] = changed[0].model_copy(
        update={"effective_resistance": changed[0].effective_resistance + 0.01}
    )
    matrix_runs[0] = first.model_copy(update={"pair_metrics": changed})
    report = build_differential_report(
        store=store,  # type: ignore[arg-type]
        fixture=build_ridgeway_fixture(),
        matrix_analysis_id="matrix-local",
        map_analysis_id="map-local",
        local_engine_manifest_sha256="a" * 64,
        reference=reference,
    )
    assert report["accepted"] is False
    assert report["matrix"]["outside_tolerance"] == 1


def _current_map(path: Path, *, special: str = "-9999") -> None:
    rows = [["1" for _ in range(20)] for _ in range(12)]
    rows[0][9] = special
    path.write_text(
        "\n".join(
            [
                "ncols 20",
                "nrows 12",
                "xllcorner 0",
                "yllcorner 0",
                "cellsize 25",
                "NODATA_value -9999",
                *(" ".join(row) for row in rows),
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_current_diagnostics_ignore_other_grids_and_exclude_nodata(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    plan = ConnectivityRunPlan(
        analysis_id="map-diagnostics",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[
            next(
                item
                for item in fixture.portfolios
                if item.portfolio_id == fixture.baseline_portfolio_id
            )
        ],
        baseline_portfolio_id=fixture.baseline_portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[fixture.baseline_portfolio_id],
        engine_manifest_sha256=_engine_manifest().sha256,
    )
    foreign = fixture.bundle.review_masks[0].model_copy(
        update={"mask_id": "foreign-mask", "grid_id": "another-grid", "asset_id": "missing"}
    )
    bundle = fixture.bundle.model_copy(
        update={"review_masks": [foreign, *fixture.bundle.review_masks]}
    )
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=fixture.bundle.project_id,
        title=fixture.bundle.title,
        mode="GUIDED",
        skeleton={"test": True},
    )
    path = tmp_path / "current.asc"
    _current_map(path)
    executor = DurableAnalysisExecutor(_engine_manifest(), lock_root=tmp_path / "analysis-locks")
    try:
        diagnostics = executor._map_diagnostics(
            store=store,
            bundle=bundle,
            plan=plan,
            scenario=scenario,
            portfolio_id=fixture.baseline_portfolio_id,
            current_path=path,
            fixture_rasters=fixture.rasters,
        )
        assert len(diagnostics) == 1
        assert diagnostics[0].baseline_total_current == 47
        assert diagnostics[0].portfolio_total_current == 47
    finally:
        store.close()


def test_current_diagnostics_fail_closed_for_missing_mask_and_invalid_current(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="map-qa",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[baseline.portfolio_id],
        engine_manifest_sha256=_engine_manifest().sha256,
    )
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=fixture.bundle.project_id,
        title=fixture.bundle.title,
        mode="GUIDED",
        skeleton={"test": True},
    )
    executor = DurableAnalysisExecutor(_engine_manifest(), lock_root=tmp_path / "analysis-locks")
    path = tmp_path / "invalid-current.asc"
    _current_map(path, special="-1")
    try:
        with pytest.raises(CurrentMapQAError, match="nonfinite or negative"):
            executor._map_diagnostics(
                store=store,
                bundle=fixture.bundle,
                plan=plan,
                scenario=scenario,
                portfolio_id=baseline.portfolio_id,
                current_path=path,
                fixture_rasters=fixture.rasters,
            )
        foreign_only = fixture.bundle.model_copy(
            update={
                "review_masks": [
                    fixture.bundle.review_masks[0].model_copy(update={"grid_id": "another-grid"})
                ],
                "decision_rules": [
                    rule.model_copy(update={"required": True})
                    if rule.rule_id == "rule_review_mask"
                    else rule
                    for rule in fixture.bundle.decision_rules
                ],
            }
        )
        with pytest.raises(CurrentMapQAError, match="no review mask"):
            executor._map_diagnostics(
                store=store,
                bundle=foreign_only,
                plan=plan,
                scenario=scenario,
                portfolio_id=baseline.portfolio_id,
                current_path=path,
                fixture_rasters=fixture.rasters,
            )
    finally:
        store.close()
