from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.analysis_executor import (
    AnalysisOwnershipError,
    DurableAnalysisError,
    DurableAnalysisExecutor,
    OrphanProcessAuditError,
)
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    EngineManifest,
    RasterGrid,
    RasterValueKind,
    SensitiveDataAuthorization,
    SensitivityClass,
)
from ecocorridor_core.engine import CircuitscapeAdapter
from ecocorridor_core.interventions import canonicalize_portfolio
from ecocorridor_core.metrics import (
    CONNECTIVITY_LIMITATION,
    CURRENT_MAP_LIMITATION,
    validate_aggregate_claim_support,
    verify_direct_claim_against_retained_evidence,
)
from ecocorridor_core.project_store import EcoProjectStore, SolverShutdownReceiptError
from ecocorridor_core.raster_io import load_raster, render_esri_ascii
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from scenario_platform.contracts import AnalysisState


def _engine_manifest(executable_sha256: str = "0" * 64) -> EngineManifest:
    return EngineManifest(
        manifest_id="test-circuitscape-engine",
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
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


def test_direct_execution_has_one_cross_process_project_owner(tmp_path: Path) -> None:
    lock_root = tmp_path / "analysis-locks"
    first = DurableAnalysisExecutor(_engine_manifest(), lock_root=lock_root)
    second = DurableAnalysisExecutor(_engine_manifest(), lock_root=lock_root)

    first_control, acquired = first._acquire_project_control(tmp_path, "analysis-a")
    assert acquired is True
    with pytest.raises(AnalysisOwnershipError, match="another application process owns"):
        second._acquire_project_control(tmp_path, "analysis-a")
    with pytest.raises(AnalysisOwnershipError, match="another direct analysis"):
        first._acquire_project_control(tmp_path, "analysis-b")

    first._release_project_control(first_control)
    second_control, acquired = second._acquire_project_control(tmp_path, "analysis-a")
    assert acquired is True
    second._release_project_control(second_control)


def test_project_content_cannot_choose_or_alias_the_trusted_lock_file(tmp_path: Path) -> None:
    project = tmp_path / "untrusted-project"
    project.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("must remain intact", encoding="utf-8")
    planted = project / ".ecocorridor-analysis.lock"
    os.link(victim, planted)
    lock_root = tmp_path / "private-app-data" / "analysis-locks"
    executor = DurableAnalysisExecutor(_engine_manifest(), lock_root=lock_root)

    control, acquired = executor._acquire_project_control(project, "analysis-a")
    assert acquired is True
    executor._release_project_control(control)

    assert victim.read_text(encoding="utf-8") == "must remain intact"
    assert planted.read_text(encoding="utf-8") == "must remain intact"
    lock_names = [item.name for item in lock_root.iterdir()]
    assert len(lock_names) == 1
    assert lock_names[0].endswith(".lock")
    assert len(lock_names[0]) == 69
    assert "project" not in lock_names[0]


def test_launch_cannot_publish_an_unjoined_thread_after_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    executor = DurableAnalysisExecutor(_engine_manifest(), lock_root=tmp_path / "analysis-locks")
    control, acquired = executor._acquire_project_control(project, "analysis-race")
    assert acquired is True
    acquired_event = threading.Event()
    continue_event = threading.Event()
    failures: list[BaseException] = []

    def delayed_acquire(_project: Path, _analysis_id: str):
        acquired_event.set()
        assert continue_event.wait(timeout=2)
        return control, True

    monkeypatch.setattr(executor, "_acquire_project_control", delayed_acquire)

    def launch() -> None:
        try:
            executor._launch(project, "analysis-race", tmp_path / "unused-engine")
        except BaseException as exc:  # noqa: BLE001 - capture thread boundary for assertion
            failures.append(exc)

    launcher = threading.Thread(target=launch)
    launcher.start()
    assert acquired_event.wait(timeout=2)
    assert executor.shutdown(timeout_seconds=0) is True
    continue_event.set()
    launcher.join(timeout=2)

    assert not launcher.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], DurableAnalysisError)
    assert "shutting down" in str(failures[0])
    assert control.thread is None
    assert control.project_lock is None


def _fake_engine(path: Path, counter: Path, *, delay: float = 0.0) -> tuple[Path, str]:
    script = f"""#!/usr/bin/python3
import argparse
import json
import pathlib
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--request', required=True)
parser.add_argument('--result', required=True)
args = parser.parse_args()
request = json.loads(pathlib.Path(args.request).read_text())
print('engine-started', flush=True)
print('engine-started-stderr', file=sys.stderr, flush=True)
pathlib.Path({str(counter.with_name(counter.name + ".started"))!r}).write_text(
    'started', encoding='utf-8'
)
time.sleep({delay!r})
counter = pathlib.Path({str(counter)!r})
counter.parent.mkdir(parents=True, exist_ok=True)
with counter.open('a', encoding='utf-8') as stream:
    stream.write(request['prepared_run_id'] + '\\n')
outputs = pathlib.Path(request['output_directory'])
outputs.mkdir(parents=True, exist_ok=True)
matrix = outputs / 'resistance-matrix.txt'
matrix.write_text(
    ''.join(
        f"{{pair['from_focal_id']}} {{pair['to_focal_id']}} {{10.0 + index}}\\n"
        for index, pair in enumerate(request['pairs'])
    ),
    encoding='utf-8',
)
current = None
if request['settings']['emit_current_map']:
    current = outputs / 'current.asc'
    current.write_text(
        'ncols 20\\nnrows 12\\nxllcorner 0\\nyllcorner 0\\ncellsize 25\\n'
        'NODATA_value -9999\\n' + (' '.join(['1'] * 20) + '\\n') * 12,
        encoding='utf-8',
    )
settings = request['settings']
resources = request['resources']
current_flag = 'True' if settings['emit_current_map'] else 'False'
four = 'True' if settings['neighbor_rule'] == 'FOUR' else 'False'
arithmetic = 'True' if settings['resistance_average'] == 'ARITHMETIC' else 'False'
configuration = [
    '[Options for advanced mode]',
    'ground_file_is_resistances = True',
    'source_file = (Browse for a valid file)',
    'ground_file = (Browse for a valid file)',
    'use_unit_currents = False',
    'use_direct_grounds = False',
    'remove_src_or_gnd = keepall',
    '[Mask file]',
    'use_mask = False',
    'mask_file = (Browse for a valid file)',
    '[Calculation options]',
    'low_memory_mode = False',
    'parallelize = False',
    'max_parallel = 1',
    'solver = cg+amg',
    'print_timings = False',
    'preemptive_memory_release = True',
    'print_rusages = False',
    'scenario = pairwise',
    '[Short circuit regions (aka polygons)]',
    'use_polygons = False',
    'polygon_file = (Browse for a valid file)',
    '[Options for one-to-all and all-to-one modes]',
    'use_variable_source_strengths = False',
    'variable_source_file = (Browse for a valid file)',
    '[Output options]',
    'set_null_currents_to_nodata = False',
    'set_null_voltages_to_nodata = False',
    'compress_grids = True',
    f'write_cur_maps = {{current_flag}}',
    f'write_cum_cur_map_only = {{current_flag}}',
    'log_transform_maps = False',
    'write_volt_maps = False',
    f"output_file = {{request['output_directory'].rstrip('/')}}/connectivity",
    '[Options for pairwise and one-to-all and all-to-one modes]',
    f"included_pairs_file = {{resources['included_pairs']}}",
    'use_included_pairs = True',
    f"point_file = {{resources['focal']}}",
    '[Connection scheme for raster habitat data]',
    f'connect_four_neighbors_only = {{four}}',
    f'connect_using_avg_resistances = {{arithmetic}}',
    '[Habitat raster or graph]',
    'habitat_map_is_resistances = True',
    f"habitat_file = {{resources['resistance']}}",
]
pathlib.Path('circuitscape.ini').write_text('\\n'.join(configuration) + '\\n', encoding='utf-8')
result = {{
    'protocol_version': '1.0',
    'status': 'SUCCEEDED',
    'engine_name': 'Circuitscape',
    'circuitscape_version': '5.17.1',
    'pair_metrics': [
        {{
            'pair_id': pair['pair_id'],
            'from_focal_id': pair['from_focal_id'],
            'to_focal_id': pair['to_focal_id'],
            'effective_resistance': 10.0 + index,
        }}
        for index, pair in enumerate(request['pairs'])
    ],
    'matrix_path': matrix.as_posix(),
    'current_map_path': current.as_posix() if current else None,
    'review_mask_summaries': [],
    'warnings': [],
    'failure_message': None,
}}
pathlib.Path(args.result).write_text(json.dumps(result, sort_keys=True), encoding='utf-8')
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _failing_engine(path: Path) -> tuple[Path, str]:
    path.write_text(
        """#!/usr/bin/python3
import argparse
import json
import pathlib

parser = argparse.ArgumentParser()
parser.add_argument('--request', required=True)
parser.add_argument('--result', required=True)
args = parser.parse_args()
pathlib.Path(args.result).write_text(json.dumps({
    'protocol_version': '1.0',
    'status': 'FAILED',
    'engine_name': 'Circuitscape',
    'circuitscape_version': '5.17.1',
    'pair_metrics': [],
    'matrix_path': None,
    'current_map_path': None,
    'review_mask_summaries': [],
    'warnings': [],
    'failure_message': 'synthetic engine failure',
}), encoding='utf-8')
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _materialized_project(root: Path) -> tuple[ConnectivityProjectBundle, Path]:
    fixture = build_ridgeway_fixture()
    assets = []
    for asset in fixture.bundle.assets:
        raster = fixture.rasters.get(asset.asset_id)
        if raster is None:
            assets.append(asset)
            continue
        payload = render_esri_ascii(raster)
        relative = Path("data_assets") / f"{asset.asset_id}.asc"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        assets.append(
            asset.model_copy(
                update={
                    "relative_path": relative.as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                    "media_type": "application/x-esri-ascii-grid",
                }
            )
        )
    bundle = fixture.bundle.model_copy(update={"assets": assets})
    store = EcoProjectStore(root)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="IMPORTED",
            skeleton={"test": True},
        )
        store.store_bundle(bundle)
    finally:
        store.close()
    return bundle, root


def _plan(bundle: ConnectivityProjectBundle, manifest: EngineManifest, analysis_id: str):
    fixture = build_ridgeway_fixture()
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    return ConnectivityRunPlan(
        analysis_id=analysis_id,
        revision_id=bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(bundle.scenarios),
        solver_job_cap=len(bundle.scenarios),
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[baseline.portfolio_id],
        engine_manifest_sha256=manifest.sha256,
    )


def _create_ready_analysis(project: Path, plan: ConnectivityRunPlan) -> None:
    store = EcoProjectStore(project)
    try:
        store.create_analysis(plan, bundle=store.effective_bundle())
    finally:
        store.close()


def _wait_for_state(project: Path, analysis_id: str, states: set[str], timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        store = EcoProjectStore(project)
        try:
            analysis = store.analysis(analysis_id)
            state = str(analysis["state"])
        finally:
            store.close()
        if state in states:
            return state
        if state == "FAILED":
            raise AssertionError(
                f"analysis failed while waiting for {states}: {analysis.get('error')}"
            )
        time.sleep(0.02)
    raise AssertionError(f"analysis did not reach {states}")


def _record_confirmed_shutdown_receipt(
    store: EcoProjectStore,
    executor: DurableAnalysisExecutor,
    plan: ConnectivityRunPlan,
) -> tuple[dict[str, object], dict[str, object]]:
    """Claim one job and persist synthetic, attempt-bound tree-exit evidence."""

    pre_jobs = store.direct_job_state_snapshot(plan.analysis_id)
    claimed = store.claim_next_direct_job(plan.analysis_id)
    assert claimed is not None
    post_jobs = store.direct_job_state_snapshot(plan.analysis_id)
    evidence = {
        "direct_job_id": claimed["direct_job_id"],
        "attempt_count": claimed["attempt_count"],
        "process_id": 41001,
        "process_group_id": 41001,
        "process_identity": "a" * 64,
        "launched_at": "2026-08-03T12:00:00+00:00",
        "termination_reason": "cancelled",
        "tree_exit_confirmed": True,
        "confirmation_method": "TEST_CONFIRMED_PROCESS_TREE_ABSENT",
        "confirmed_at": "2026-08-03T12:00:01+00:00",
    }
    receipt = store.record_solver_shutdown_receipt(
        plan.analysis_id,
        project_path_sha256=executor._project_path_identity(store.root),
        engine_manifest_sha256=executor.engine_manifest.sha256,
        workflow_identity_sha256=executor._workflow_identity(store, plan.analysis_id),
        pre_jobs=pre_jobs,
        post_jobs=post_jobs,
        process_exits={str(claimed["direct_job_id"]): evidence},
    )
    assert receipt is not None
    return claimed, receipt


def test_direct_executor_is_background_exact_and_reuses_physics_cache(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(tmp_path / "fake-engine", tmp_path / "calls.txt")
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )

    first = _plan(bundle, manifest, "analysis-first")
    _create_ready_analysis(project, first)
    result = executor.start(
        project_directory=project,
        bundle=bundle,
        plan=first,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert result == {"state": "RUNNING"}
    assert _wait_for_state(project, first.analysis_id, {"COMPLETED"}) == "COMPLETED"
    call_count = len((tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines())
    assert call_count == 12

    store = EcoProjectStore(project)
    try:
        runs, summaries, claims = store.analysis_results(first.analysis_id)
        assert len(runs) == 12
        assert len(summaries) == 1
        assert claims
        assert all(run.current_map_sha256 for run in runs)
        assert all(run.diagnostic_maps[0].relative_change_percent == 0 for run in runs)
        assert all(
            run.resource_observation is not None
            and run.resource_observation.boundary.value == "DIRECT_SOLVER"
            and run.resource_observation.solver_wall_seconds is not None
            and run.resource_observation.solver_wall_seconds > 0
            and run.resource_observation.peak_memory_status.value == "MEASURED"
            and (run.resource_observation.peak_memory_bytes or 0) > 0
            and run.resource_observation.peak_memory_is_lower_bound is True
            and run.resource_observation.energy_status.value == "ESTIMATED"
            and (run.resource_observation.energy_kwh or 0) > 0
            and (run.resource_observation.energy_estimate_lower_kwh or 0)
            <= (run.resource_observation.energy_kwh or 0)
            <= (run.resource_observation.energy_estimate_upper_kwh or 0)
            for run in runs
        )
        assert store.direct_job_counts(first.analysis_id) == {"SUCCEEDED": 12}
        direct_jobs = store.direct_jobs(first.analysis_id)
        assert all(not item["cached"] for item in direct_jobs)
        prepared_by_run = {item["direct_job_id"]: item["prepared"] for item in direct_jobs}
        direct_metric_claims = [
            item
            for item in claims
            if item.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}
        ]
        assert direct_metric_claims
        assert {"COST", "DIRECT_QA", "PORTFOLIO_DISPOSITION"}.issubset(
            {item.claim_kind for item in claims}
        )
        cost_claim = next(item for item in claims if item.claim_kind == "COST")
        resolved_cost = store.resolve_evidence_claim(cost_claim.claim_id)
        assert resolved_cost["claim"]["value"] == cost_claim.value
        assert any(
            node["node_type"] == "COST_BASIS" and node["sha256"] == bundle.cost_basis.sha256
            for node in resolved_cost["claim"]["nodes"]
        )
        aggregate_claims = [item for item in claims if item.claim_kind == "AGGREGATE_RULE"]
        assert {item.rule_id for item in aggregate_claims} == {
            "aggregate:worst_controlling_resistance_change",
            "aggregate:safeguard_nonregression",
            "aggregate:review_mask_trigger",
        }
        claims_by_id = {item.claim_id: item for item in claims}
        runs_by_id = {item.direct_run_id: item for item in runs}
        expected_reducers = {
            "aggregate:worst_controlling_resistance_change": (
                "MIN_BY_NUMERIC_VALUE_OVER_ALL_COMPLETE_SCENARIO_PAIR_PRIMITIVES"
            ),
            "aggregate:safeguard_nonregression": ("ALL_REQUIRED_SAFEGUARD_RULE_OBSERVATIONS_PASS"),
            "aggregate:review_mask_trigger": (
                "ANY_REVIEW_MASK_PRIMITIVE_HAS_REVIEW_TRIGGERED_TRUE"
            ),
        }
        for aggregate in aggregate_claims:
            reducer = next(node for node in aggregate.nodes if node.node_type == "REDUCER")
            assert reducer.attributes["semantics"] == expected_reducers[aggregate.rule_id]
            support_nodes = [
                node for node in aggregate.nodes if node.node_type == "SUPPORTING_CLAIM"
            ]
            support_ids = {str(node.attributes["claim_id"]) for node in support_nodes}
            assert support_ids
            assert support_ids.issubset(claims_by_id)
            assert all(
                claims_by_id[item].claim_kind
                in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC", "DIRECT_QA"}
                for item in support_ids
            )
            expected_run_ids = sorted(
                {
                    direct_run_id
                    for item in support_ids
                    for direct_run_id in claims_by_id[item].direct_run_ids
                }
            )
            assert aggregate.direct_run_ids == expected_run_ids
            assert set(expected_run_ids).issubset(runs_by_id)
            assert {
                (edge.from_node_id, edge.to_node_id, edge.relation)
                for edge in aggregate.edges
                if edge.relation == "reduces_claim"
            } == {
                (
                    f"reducer:{aggregate.claim_id}",
                    f"supporting-claim:{item}",
                    "reduces_claim",
                )
                for item in support_ids
            }
        worst_aggregate = next(
            item
            for item in aggregate_claims
            if item.rule_id == "aggregate:worst_controlling_resistance_change"
        )
        worst_support = [
            claims_by_id[str(node.attributes["claim_id"])]
            for node in worst_aggregate.nodes
            if node.node_type == "SUPPORTING_CLAIM"
        ]
        summary = summaries[0]
        subject_runs = [runs_by_id[item] for item in summary.direct_run_ids]
        expected_worst_scope = {
            (run.scenario_id, metric.pair_id) for run in subject_runs for metric in run.pair_metrics
        }
        assert {
            (str(item.scenario_id), str(item.pair_id)) for item in worst_support
        } == expected_worst_scope
        assert all(
            item.rule_id == "evidence:within-scenario-resistance-change" for item in worst_support
        )
        validate_aggregate_claim_support(
            summaries=summaries,
            runs=runs,
            claims=claims,
        )
        omitted_support = worst_support[0].claim_id
        tampered_nodes = [
            (
                node.model_copy(
                    update={
                        "attributes": {
                            **node.attributes,
                            "supporting_claim_count": len(worst_support) - 1,
                        }
                    }
                )
                if node.node_type == "REDUCER"
                else node
            )
            for node in worst_aggregate.nodes
            if node.node_id != f"supporting-claim:{omitted_support}"
        ]
        tampered_aggregate = worst_aggregate.model_copy(
            update={
                "nodes": tampered_nodes,
                "edges": [
                    edge
                    for edge in worst_aggregate.edges
                    if edge.from_node_id != f"supporting-claim:{omitted_support}"
                    and edge.to_node_id != f"supporting-claim:{omitted_support}"
                ],
            }
        )
        with pytest.raises(
            ValueError,
            match="does not cover every complete scenario/pair primitive",
        ):
            validate_aggregate_claim_support(
                summaries=summaries,
                runs=runs,
                claims=[
                    tampered_aggregate if item.claim_id == worst_aggregate.claim_id else item
                    for item in claims
                ],
            )
        worst_runs = [runs_by_id[item] for item in worst_aggregate.direct_run_ids]
        review_aggregate = next(
            item for item in aggregate_claims if item.rule_id == "aggregate:review_mask_trigger"
        )
        review_support = [
            claims_by_id[str(node.attributes["claim_id"])]
            for node in review_aggregate.nodes
            if node.node_type == "SUPPORTING_CLAIM"
        ]
        rebuilt_review = executor._aggregate_rule_claim(
            claim_id=review_aggregate.claim_id,
            portfolio_id=review_aggregate.portfolio_id,
            aggregate_key="review_mask_trigger",
            text=review_aggregate.text,
            value=bool(review_aggregate.value),
            units=review_aggregate.units,
            reducer_semantics="ANY_REVIEW_MASK_PRIMITIVE_HAS_REVIEW_TRIGGERED_TRUE",
            supporting_claims=list(reversed(review_support)),
            runs=list(reversed([runs_by_id[item] for item in review_aggregate.direct_run_ids])),
            source_rule_ids=["rule_review_mask"],
            limitations=[CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
        )
        assert rebuilt_review.model_dump(mode="json") == review_aggregate.model_dump(mode="json")
        with pytest.raises(
            DurableAnalysisError,
            match="unique supporting primitive claims",
        ):
            executor._aggregate_rule_claim(
                claim_id="claim-duplicate-support",
                portfolio_id=worst_aggregate.portfolio_id,
                aggregate_key="negative_duplicate",
                text="Direct Circuitscape result under documented assumptions. Duplicate test.",
                value=worst_aggregate.value,
                units=worst_aggregate.units,
                reducer_semantics="NEGATIVE_TEST",
                supporting_claims=[*worst_support, *worst_support],
                runs=worst_runs,
            )
        with pytest.raises(
            DurableAnalysisError,
            match="exactly match primitive-claim support",
        ):
            executor._aggregate_rule_claim(
                claim_id="claim-missing-run",
                portfolio_id=worst_aggregate.portfolio_id,
                aggregate_key="negative_missing_run",
                text="Direct Circuitscape result under documented assumptions. Missing run test.",
                value=worst_aggregate.value,
                units=worst_aggregate.units,
                reducer_semantics="NEGATIVE_TEST",
                supporting_claims=worst_support,
                runs=worst_runs[:-1],
            )
        for claim in direct_metric_claims:
            subject = next(
                run
                for run in runs
                if run.direct_run_id in claim.direct_run_ids
                and run.portfolio_id == claim.portfolio_id
                and run.scenario_id == claim.scenario_id
            )
            diff = next(node for node in claim.nodes if node.node_type == "RASTER_DIFF")
            expected = prepared_by_run[subject.direct_run_id]["action_raster_sha256"]
            assert diff.node_id == f"diff:{expected}"
            assert diff.sha256 == expected
            assert prepared_by_run[subject.direct_run_id]["footprint_proof_sha256"]
        first_claim = direct_metric_claims[0]
        resolved = store.resolve_evidence_claim(first_claim.claim_id)
        resolved_diff = next(
            node for node in resolved["claim"]["nodes"] if node["node_type"] == "RASTER_DIFF"
        )
        resolved_subject = next(
            item
            for item in resolved["prepared_runs"]
            if item["direct_run_id"]
            == next(
                run.direct_run_id
                for run in runs
                if run.portfolio_id == first_claim.portfolio_id
                and run.scenario_id == first_claim.scenario_id
            )
        )
        assert resolved_diff["sha256"] == resolved_subject["action_raster_sha256"]
        assert resolved_subject["footprint_proof_sha256"]
        map_claim = next(
            item for item in direct_metric_claims if item.claim_kind == "DIRECT_MAP_DIAGNOSTIC"
        )
        assert store.resolve_evidence_claim(map_claim.claim_id)["claim"]["claim_id"] == (
            map_claim.claim_id
        )
        tampered_map_claim = map_claim.model_copy(
            update={
                "value": float(map_claim.value) + 1.0,
                "nodes": [
                    node.model_copy(
                        update={
                            "attributes": {
                                **node.attributes,
                                "value": float(map_claim.value) + 1.0,
                            }
                        }
                    )
                    if node.node_type == "METRIC"
                    else node
                    for node in map_claim.nodes
                ],
            }
        )
        artifact_paths = {
            (run_id, str(artifact["role"])): project / str(artifact["cas_relative_path"])
            for run_id in tampered_map_claim.direct_run_ids
            for artifact in store.run_artifacts(run_id)
        }
        asset_paths = {
            asset.asset_id: store.resolve_asset_path(asset.relative_path) for asset in bundle.assets
        }
        with pytest.raises(ValueError, match="retained current rasters"):
            verify_direct_claim_against_retained_evidence(
                claim=tampered_map_claim,
                bundle=bundle,
                baseline_portfolio_id=first.baseline_portfolio_id,
                runs_by_id=runs_by_id,
                artifact_paths=artifact_paths,
                asset_paths=asset_paths,
            )
    finally:
        store.close()
    second = _plan(bundle, manifest, "analysis-second")
    _create_ready_analysis(project, second)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=second,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert _wait_for_state(project, second.analysis_id, {"COMPLETED"}) == "COMPLETED"
    assert len((tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()) == call_count
    store = EcoProjectStore(project)
    try:
        assert all(item["cached"] for item in store.direct_jobs(second.analysis_id))
        cached_runs, _, cached_claims = store.analysis_results(second.analysis_id)
        assert all(item.cache_source_direct_run_id for item in cached_runs)
        assert all(
            item.resource_observation is not None
            and item.resource_observation.boundary.value == "CACHE_REUSE"
            and item.resource_observation.cache_lookup_wall_seconds is not None
            and item.resource_observation.solver_wall_seconds is None
            and item.resource_observation.energy_status.value == "NOT_MEASURED"
            and item.resource_observation.energy_kwh is None
            for item in cached_runs
        )
        first_run_ids = {
            item.direct_run_id for item in store.analysis_results(first.analysis_id)[0]
        }
        assert {item.cache_source_direct_run_id for item in cached_runs} == first_run_ids
        cached_direct_claims = [
            item
            for item in cached_claims
            if item.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}
        ]
        assert cached_direct_claims
        assert all(
            any(node.node_type == "CACHE_LINEAGE" for node in claim.nodes)
            and any(edge.relation == "reuses_direct_execution" for edge in claim.edges)
            for claim in cached_direct_claims
        )
    finally:
        store.close()


def test_cost_and_qa_primitives_do_not_depend_on_decision_rule_count(
    tmp_path: Path,
) -> None:
    def run_variant(name: str, consumer_count: int):
        project = tmp_path / name / "project"
        bundle, _ = _materialized_project(project)
        cost_template = next(
            item for item in bundle.decision_rules if item.metric.value == "MAX_COST"
        )
        qa_template = next(
            item for item in bundle.decision_rules if item.metric.value == "REQUIRE_ACCEPTED_QA"
        )
        independent_rules = [
            item
            for item in bundle.decision_rules
            if item.metric.value not in {"MAX_COST", "REQUIRE_ACCEPTED_QA"}
        ]
        consuming_rules = [
            *[
                cost_template.model_copy(
                    update={
                        "rule_id": f"rule_cost_{index}",
                        "title": f"Cost consumer {index}",
                    }
                )
                for index in range(consumer_count)
            ],
            *[
                qa_template.model_copy(
                    update={
                        "rule_id": f"rule_qa_{index}",
                        "title": f"QA consumer {index}",
                    }
                )
                for index in range(consumer_count)
            ],
        ]
        bundle = bundle.model_copy(
            update={"decision_rules": [*independent_rules, *consuming_rules]}
        )
        store = EcoProjectStore(project)
        try:
            store.store_bundle(bundle)
        finally:
            store.close()
        engine, digest = _fake_engine(
            tmp_path / name / "fake-engine",
            tmp_path / name / "calls.txt",
        )
        manifest = _engine_manifest(digest)
        plan = _plan(bundle, manifest, "analysis-rule-cardinality")
        _create_ready_analysis(project, plan)
        executor = DurableAnalysisExecutor(
            manifest,
            lock_root=tmp_path / "analysis-locks",
            engine_executable=engine,
            engine_sha256=digest,
        )
        executor.start(
            project_directory=project,
            bundle=bundle,
            plan=plan,
            engine_executable=engine,
            engine_sha256=digest,
        )
        assert _wait_for_state(project, plan.analysis_id, {"COMPLETED"}) == "COMPLETED"
        store = EcoProjectStore(project)
        try:
            _, summaries, claims = store.analysis_results(plan.analysis_id)
        finally:
            store.close()
        return bundle, summaries[0], claims

    zero_bundle, zero_summary, zero_claims = run_variant("zero-consumers", 0)
    many_bundle, many_summary, many_claims = run_variant("many-consumers", 2)

    def primitive_projection(bundle, summary, claims):
        cost_claims = [item for item in claims if item.claim_kind == "COST"]
        qa_claims = [item for item in claims if item.claim_kind == "DIRECT_QA"]
        assert len(cost_claims) == 1
        assert len(qa_claims) == len(bundle.scenarios)
        assert len({item.scenario_id for item in qa_claims}) == len(bundle.scenarios)
        cost = cost_claims[0]
        qa_by_scenario = {str(item.scenario_id): item for item in qa_claims}
        return cost, qa_by_scenario, summary

    zero_cost, zero_qa, _ = primitive_projection(zero_bundle, zero_summary, zero_claims)
    many_cost, many_qa, many_summary = primitive_projection(many_bundle, many_summary, many_claims)
    assert zero_cost.claim_id == many_cost.claim_id
    assert {item: claim.claim_id for item, claim in zero_qa.items()} == {
        item: claim.claim_id for item, claim in many_qa.items()
    }
    assert zero_cost.rule_id is None
    assert many_cost.rule_id is None
    assert {node.node_id for node in zero_cost.nodes if node.node_type == "RULE"} == {
        "rule:evidence:screening-cost"
    }
    assert {node.node_id for node in many_cost.nodes if node.node_type == "RULE"} == {
        "rule:rule_cost_0",
        "rule:rule_cost_1",
    }
    assert all(
        {node.node_id for node in claim.nodes if node.node_type == "RULE"}
        == {"rule:evidence:direct-qa"}
        for claim in zero_qa.values()
    )
    assert all(
        {node.node_id for node in claim.nodes if node.node_type == "RULE"}
        == {"rule:rule_qa_0", "rule:rule_qa_1"}
        for claim in many_qa.values()
    )

    cost_dispositions = [
        item for item in many_summary.rule_dispositions if item.rule_id.startswith("rule_cost_")
    ]
    assert len(cost_dispositions) == 2
    assert {item.evidence_claim_id for item in cost_dispositions} == {many_cost.claim_id}
    qa_dispositions = [
        item for item in many_summary.rule_dispositions if item.rule_id.startswith("rule_qa_")
    ]
    assert len(qa_dispositions) == 2 * len(many_bundle.scenarios)
    assert all(
        item.evidence_claim_id == many_qa[str(item.scenario_id)].claim_id
        for item in qa_dispositions
    )


def test_direct_executor_binds_reviewed_footprint_proof_to_action_run(
    tmp_path: Path,
) -> None:
    project = tmp_path / "action-project"
    bundle, _ = _materialized_project(project)
    fixture = build_ridgeway_fixture()
    selections = {site.site_id: f"{site.site_id}_no_action" for site in bundle.intervention_sites}
    first_site = bundle.intervention_sites[0]
    selections[first_site.site_id] = f"{first_site.site_id}_reviewed_action"
    action = canonicalize_portfolio(
        bundle.intervention_sites,
        bundle.intervention_options,
        selections,
    )
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    engine, digest = _fake_engine(tmp_path / "action-engine", tmp_path / "action-calls.txt")
    manifest = _engine_manifest(digest)
    plan = ConnectivityRunPlan(
        analysis_id="analysis-action-proof",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id for scenario in bundle.scenarios],
        portfolios=[baseline, action],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=24,
        solver_job_cap=24,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[],
        engine_manifest_sha256=manifest.sha256,
    )
    _create_ready_analysis(project, plan)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert _wait_for_state(project, plan.analysis_id, {"COMPLETED"}) == "COMPLETED"
    store = EcoProjectStore(project)
    try:
        jobs = store.direct_jobs(plan.analysis_id)
        action_jobs = {
            item["scenario_id"]: item
            for item in jobs
            if item["portfolio_id"] == action.portfolio_id
        }
        assert all(item["prepared"]["footprint_proof_sha256"] for item in action_jobs.values())
        runs, _, claims = store.analysis_results(plan.analysis_id)
        action_runs = {
            item.scenario_id: item for item in runs if item.portfolio_id == action.portfolio_id
        }
        action_claims = [
            item
            for item in claims
            if item.portfolio_id == action.portfolio_id
            and item.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}
        ]
        assert action_claims
        for claim in action_claims:
            diff = next(node for node in claim.nodes if node.node_type == "RASTER_DIFF")
            assert diff.sha256 == action_jobs[claim.scenario_id]["prepared"]["action_raster_sha256"]
            assert action_runs[claim.scenario_id].direct_run_id in claim.direct_run_ids
    finally:
        store.close()


def test_background_executor_uses_immutable_analysis_model_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(tmp_path / "snapshot-engine", tmp_path / "snapshot-calls.txt")
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-snapshot")
    _create_ready_analysis(project, plan)

    # This post-plan project edit is intentionally unusable. The already
    # planned analysis must continue from its checksum-bound model snapshot.
    source_asset = next(item for item in bundle.assets if item.kind == AssetKind.RESISTANCE_RASTER)
    store = EcoProjectStore(project)
    try:
        store.put_record(
            "asset",
            source_asset.asset_id,
            source_asset.model_copy(update={"relative_path": "data_assets/missing.asc"}).model_dump(
                mode="json"
            ),
        )
    finally:
        store.close()

    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert _wait_for_state(project, plan.analysis_id, {"COMPLETED"}) == "COMPLETED"
    assert len((tmp_path / "snapshot-calls.txt").read_text(encoding="utf-8").splitlines()) == 12


def test_pause_waits_for_active_job_and_resume_finishes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(tmp_path / "slow-engine", tmp_path / "slow-calls.txt", delay=0.08)
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-pause")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        store = EcoProjectStore(project)
        try:
            running = store.direct_job_counts(plan.analysis_id).get("RUNNING", 0)
        finally:
            store.close()
        if running:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("executor did not start a direct job")
    executor.pause(project_directory=project, analysis_id=plan.analysis_id)
    assert _wait_for_state(project, plan.analysis_id, {"PAUSED"}) == "PAUSED"
    store = EcoProjectStore(project)
    try:
        counts = store.direct_job_counts(plan.analysis_id)
        assert counts.get("SUCCEEDED", 0) >= 1
        assert counts.get("RUNNING", 0) == 0
        assert counts.get("QUEUED", 0) >= 1
    finally:
        store.close()
    executor.resume(project_directory=project, analysis_id=plan.analysis_id)
    assert _wait_for_state(project, plan.analysis_id, {"COMPLETED"}) == "COMPLETED"


def test_abandoned_running_job_is_requeued_and_cancel_is_durable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    manifest = _engine_manifest()
    plan = _plan(bundle, manifest, "analysis-recovery")
    _create_ready_analysis(project, plan)
    executor = DurableAnalysisExecutor(manifest, lock_root=tmp_path / "analysis-locks")
    specs = executor._job_specs(plan)
    store = EcoProjectStore(project)
    try:
        store.schedule_direct_jobs(plan.analysis_id, specs)
        store.transition_analysis(plan.analysis_id, AnalysisState.RUNNING)
        claimed = store.claim_next_direct_job(plan.analysis_id)
        assert claimed is not None and claimed["state"] == "RUNNING"
    finally:
        store.close()
    reopened = EcoProjectStore(project)
    try:
        before = reopened.direct_job_state_snapshot(plan.analysis_id)
        with pytest.raises(
            SolverShutdownReceiptError, match="confirmed process-tree exit"
        ) as captured:
            reopened.recover_abandoned_direct_jobs(plan.analysis_id)
        assert captured.value.code == "ORPHAN_PROCESS_AUDIT_REQUIRED"
        assert reopened.direct_job_state_snapshot(plan.analysis_id) == before
        assert (
            reopened.recover_abandoned_direct_jobs(
                plan.analysis_id, process_tree_exit_confirmed=True
            )
            == 1
        )
        assert reopened.direct_job_counts(plan.analysis_id)["QUEUED"] == 12
        assert reopened.cancel_queued_direct_jobs(plan.analysis_id) == 12
        reopened.transition_analysis(plan.analysis_id, AnalysisState.CANCELLED)
        assert reopened.direct_job_counts(plan.analysis_id) == {"CANCELLED": 12}
        pending = reopened.pending_outbox()
        assert sum(item["event_type"] == "direct_job_terminal" for item in pending) == 12
        assert sum(item["event_type"] == "bundle_import" for item in pending) == 1
    finally:
        reopened.close()


def test_restart_without_shutdown_receipt_blocks_without_mutating_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(tmp_path / "unused-engine", tmp_path / "unused-calls.txt")
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-no-receipt")
    _create_ready_analysis(project, plan)
    store = EcoProjectStore(project)
    try:
        store.start_analysis_with_direct_jobs(plan.analysis_id, executor._job_specs(plan))
        claimed = store.claim_next_direct_job(plan.analysis_id)
        assert claimed is not None
        before_analysis = store.analysis(plan.analysis_id)
        before_jobs = store.direct_job_state_snapshot(plan.analysis_id)
    finally:
        store.close()

    with pytest.raises(OrphanProcessAuditError) as captured:
        executor.recover(
            project_directory=project,
            analysis_id=plan.analysis_id,
            engine_executable=engine,
            engine_sha256=digest,
        )
    assert captured.value.reason_code == "ORPHAN_RECEIPT_MISSING"
    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id) == before_analysis
        assert store.direct_job_state_snapshot(plan.analysis_id) == before_jobs
        assert store.solver_shutdown_receipts(plan.analysis_id) == []
    finally:
        store.close()


def test_unconfirmed_success_exit_is_nonreportable_and_stops_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    calls = tmp_path / "unconfirmed-calls.txt"
    engine, digest = _fake_engine(tmp_path / "unconfirmed-engine", calls)
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-unconfirmed-tree")
    _create_ready_analysis(project, plan)
    original_execute = CircuitscapeAdapter.execute

    def execute_with_unconfirmed_tree(self, *args, **kwargs):
        execution = original_execute(self, *args, **kwargs)
        return execution.model_copy(
            update={
                "process_exit_evidence": {
                    **execution.process_exit_evidence,
                    "tree_exit_confirmed": False,
                    "confirmation_method": "WINDOWS_NATURAL_DESCENDANT_EXIT_UNCONFIRMED",
                }
            }
        )

    monkeypatch.setattr(CircuitscapeAdapter, "execute", execute_with_unconfirmed_tree)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and executor.is_active(
        project_directory=project, analysis_id=plan.analysis_id
    ):
        time.sleep(0.01)
    assert not executor.is_active(project_directory=project, analysis_id=plan.analysis_id)

    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id)["state"] == "RUNNING"
        assert store.analysis(plan.analysis_id)["error"] is None
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 11, "RUNNING": 1}
        runs, summaries, claims = store.analysis_results(plan.analysis_id)
        assert runs == []
        assert summaries == []
        assert claims == []
        assert store.solver_shutdown_receipts(plan.analysis_id) == []
    finally:
        store.close()
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1

    with pytest.raises(OrphanProcessAuditError) as captured:
        executor.recover(
            project_directory=project,
            analysis_id=plan.analysis_id,
            engine_executable=engine,
            engine_sha256=digest,
        )
    assert captured.value.reason_code == "ORPHAN_RECEIPT_MISSING"


def test_shutdown_receipt_identity_mismatch_blocks_without_mutating_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(tmp_path / "unused-engine", tmp_path / "unused-calls.txt")
    manifest = _engine_manifest(digest)
    producer = DurableAnalysisExecutor(manifest, lock_root=tmp_path / "analysis-locks")
    plan = _plan(bundle, manifest, "analysis-mismatched-receipt")
    _create_ready_analysis(project, plan)
    store = EcoProjectStore(project)
    try:
        store.start_analysis_with_direct_jobs(plan.analysis_id, producer._job_specs(plan))
        _record_confirmed_shutdown_receipt(store, producer, plan)
        before_analysis = store.analysis(plan.analysis_id)
        before_jobs = store.direct_job_state_snapshot(plan.analysis_id)
    finally:
        store.close()

    mismatched_manifest = manifest.model_copy(update={"parser_sha256": "f" * 64})
    consumer = DurableAnalysisExecutor(
        mismatched_manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    with pytest.raises(OrphanProcessAuditError) as captured:
        consumer.recover(
            project_directory=project,
            analysis_id=plan.analysis_id,
            engine_executable=engine,
            engine_sha256=digest,
        )
    assert captured.value.reason_code == "ORPHAN_RECEIPT_IDENTITY_MISMATCH"
    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id) == before_analysis
        assert store.direct_job_state_snapshot(plan.analysis_id) == before_jobs
        assert store.solver_shutdown_receipts(plan.analysis_id)[0]["state"] == "UNUSED"
    finally:
        store.close()


def test_shutdown_receipt_is_consumed_exactly_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    manifest = _engine_manifest()
    executor = DurableAnalysisExecutor(manifest, lock_root=tmp_path / "analysis-locks")
    plan = _plan(bundle, manifest, "analysis-consume-once")
    _create_ready_analysis(project, plan)
    store = EcoProjectStore(project)
    try:
        store.start_analysis_with_direct_jobs(plan.analysis_id, executor._job_specs(plan))
        claimed, receipt = _record_confirmed_shutdown_receipt(store, executor, plan)
        identities = {
            "project_path_sha256": executor._project_path_identity(store.root),
            "engine_manifest_sha256": executor.engine_manifest.sha256,
            "workflow_identity_sha256": executor._workflow_identity(store, plan.analysis_id),
        }
        assert store.consume_solver_shutdown_receipt(plan.analysis_id, **identities) == 1
        receipts = store.solver_shutdown_receipts(plan.analysis_id)
        assert len(receipts) == 1
        assert receipts[0]["generation"] == 1
        assert receipts[0]["receipt_sha256"] == receipt["receipt_sha256"]
        assert receipts[0]["state"] == "CONSUMED"
        assert receipts[0]["consumed_at"] is not None
        reclaimed = store.claim_next_direct_job(plan.analysis_id)
        assert reclaimed is not None
        assert reclaimed["direct_job_id"] == claimed["direct_job_id"]
        before = store.direct_job_state_snapshot(plan.analysis_id)
        with pytest.raises(SolverShutdownReceiptError) as captured:
            store.consume_solver_shutdown_receipt(plan.analysis_id, **identities)
        assert captured.value.code == "ORPHAN_RECEIPT_ALREADY_CONSUMED"
        assert store.direct_job_state_snapshot(plan.analysis_id) == before
    finally:
        store.close()


def test_cancel_stops_new_jobs_and_records_active_job_terminal_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(
        tmp_path / "cancel-engine", tmp_path / "cancel-calls.txt", delay=0.75
    )
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-cancel")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        store = EcoProjectStore(project)
        try:
            active = store.direct_job_counts(plan.analysis_id).get("RUNNING", 0)
        finally:
            store.close()
        if active:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("executor did not start a direct job")
    engine_started = tmp_path / "cancel-calls.txt.started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not engine_started.exists():
        time.sleep(0.01)
    assert engine_started.is_file(), "solver subprocess did not reach its start marker"
    cancel_started = time.monotonic()
    executor.cancel(project_directory=project, analysis_id=plan.analysis_id)
    assert _wait_for_state(project, plan.analysis_id, {"CANCELLED"}) == "CANCELLED"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        store = EcoProjectStore(project)
        try:
            counts = store.direct_job_counts(plan.analysis_id)
        finally:
            store.close()
        if counts == {"CANCELLED": 12}:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"active direct job did not settle after cancellation: {counts}")
    assert time.monotonic() - cancel_started < 1.5
    time.sleep(0.85)
    assert not (tmp_path / "cancel-calls.txt").exists()
    store = EcoProjectStore(project)
    try:
        jobs = store.direct_jobs(plan.analysis_id)
        actively_cancelled = [
            item
            for item in jobs
            if item["error"].get("code") == "ANALYSIS_CANCELLED_DURING_ACTIVE_JOB"
        ]
        assert len(actively_cancelled) == 1
        active_job = actively_cancelled[0]
        assert active_job["state"] == "CANCELLED"
        assert active_job["resource_observation"]["boundary"] == "DIRECT_SOLVER"
        assert active_job["resource_observation"]["energy_status"] == "ESTIMATED"
        assert {item["role"] for item in active_job["terminal_artifacts"]}.issuperset(
            {"request", "stdout", "stderr"}
        )
        artifacts = {item["role"]: item for item in active_job["terminal_artifacts"]}
        assert (
            b"engine-started" in (project / artifacts["stdout"]["cas_relative_path"]).read_bytes()
        )
        assert (
            b"engine-started-stderr"
            in (project / artifacts["stderr"]["cas_relative_path"]).read_bytes()
        )
    finally:
        store.close()


def test_worker_shutdown_leaves_active_job_recoverable_instead_of_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(
        tmp_path / "shutdown-engine", tmp_path / "shutdown-calls.txt", delay=3.0
    )
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-worker-shutdown")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    engine_started = tmp_path / "shutdown-calls.txt.started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not engine_started.exists():
        time.sleep(0.01)
    assert engine_started.is_file(), "solver subprocess did not reach its start marker"

    assert executor.shutdown(timeout_seconds=3.0) is True
    assert executor.is_active(project_directory=project, analysis_id=plan.analysis_id) is False
    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id)["state"] == "RUNNING"
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 11, "RUNNING": 1}
        active = next(
            item for item in store.direct_jobs(plan.analysis_id) if item["state"] == "RUNNING"
        )
        assert active["error"] is None
        assert active["terminal_artifacts"] == []
        assert not any(
            item["event_type"] == "direct_job_terminal"
            and item["subject_id"] == active["direct_job_id"]
            for item in store.pending_outbox()
        )
        receipts = store.solver_shutdown_receipts(plan.analysis_id)
        assert len(receipts) == 1
        assert receipts[0]["state"] == "UNUSED"
        covered = receipts[0]["payload"]["jobs"]
        assert len(covered) == 1
        assert covered[0]["direct_job_id"] == active["direct_job_id"]
        assert covered[0]["post_attempt_count"] == active["attempt_count"]
        assert covered[0]["tree_exit_confirmed"] is True
        assert covered[0]["process_id"] > 0
        assert covered[0]["process_group_id"] > 0
        assert len(covered[0]["process_identity"]) == 64
    finally:
        store.close()

    recovered = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )

    def release_without_launch(
        project_directory: Path, analysis_id: str, _executable: Path
    ) -> None:
        control = recovered._controls[recovered._key(project_directory, analysis_id)]
        recovered._release_project_control(control)

    monkeypatch.setattr(recovered, "_launch", release_without_launch)
    recovered.recover(
        project_directory=project,
        analysis_id=plan.analysis_id,
        engine_executable=engine,
        engine_sha256=digest,
    )
    store = EcoProjectStore(project)
    try:
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 12}
        receipts = store.solver_shutdown_receipts(plan.analysis_id)
        assert len(receipts) == 1
        assert receipts[0]["state"] == "CONSUMED"
        assert receipts[0]["consumed_at"] is not None
    finally:
        store.close()
    time.sleep(0.1)
    assert not (tmp_path / "shutdown-calls.txt").exists()


def test_second_executor_ownership_rejection_preserves_running_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(
        tmp_path / "owner-engine", tmp_path / "owner-calls.txt", delay=3.0
    )
    manifest = _engine_manifest(digest)
    lock_root = tmp_path / "analysis-locks"
    owner = DurableAnalysisExecutor(
        manifest,
        lock_root=lock_root,
        engine_executable=engine,
        engine_sha256=digest,
    )
    contender = DurableAnalysisExecutor(
        manifest,
        lock_root=lock_root,
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-owned")
    _create_ready_analysis(project, plan)
    owner.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    marker = tmp_path / "owner-calls.txt.started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)
    assert marker.is_file()

    with pytest.raises(AnalysisOwnershipError):
        contender.start(
            project_directory=project,
            bundle=bundle,
            plan=plan,
            engine_executable=engine,
            engine_sha256=digest,
        )

    store = EcoProjectStore(project)
    try:
        authoritative = store.analysis(plan.analysis_id)
        assert authoritative["state"] == "RUNNING"
        assert authoritative["error"] is None
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 11, "RUNNING": 1}
    finally:
        store.close()
    assert owner.shutdown(timeout_seconds=3.0) is True


def test_pause_intent_settles_when_worker_shutdown_interrupts_active_job(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(
        tmp_path / "pause-shutdown-engine",
        tmp_path / "pause-shutdown-calls.txt",
        delay=3.0,
    )
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-pause-shutdown")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    marker = tmp_path / "pause-shutdown-calls.txt.started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)
    assert marker.is_file()

    executor.pause(project_directory=project, analysis_id=plan.analysis_id)
    assert executor.shutdown(timeout_seconds=3.0) is True

    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id)["state"] == "PAUSED"
        assert store.analysis(plan.analysis_id)["error"] is None
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 12}
    finally:
        store.close()


def test_cancel_intent_wins_when_worker_shutdown_interrupts_active_job(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _fake_engine(
        tmp_path / "cancel-shutdown-engine",
        tmp_path / "cancel-shutdown-calls.txt",
        delay=3.0,
    )
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-cancel-shutdown")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    marker = tmp_path / "cancel-shutdown-calls.txt.started"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)
    assert marker.is_file()

    executor.cancel(project_directory=project, analysis_id=plan.analysis_id)
    assert executor.shutdown(timeout_seconds=3.0) is True

    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id)["state"] == "CANCELLED"
        assert store.direct_job_counts(plan.analysis_id) == {"CANCELLED": 12}
    finally:
        store.close()


def test_failed_job_retains_passive_diagnostics_without_metrics_or_claims(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    bundle, _ = _materialized_project(project)
    engine, digest = _failing_engine(tmp_path / "failing-engine")
    manifest = _engine_manifest(digest)
    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=tmp_path / "analysis-locks",
        engine_executable=engine,
        engine_sha256=digest,
    )
    plan = _plan(bundle, manifest, "analysis-failure-artifacts")
    _create_ready_analysis(project, plan)
    executor.start(
        project_directory=project,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=digest,
    )
    assert _wait_for_state(project, plan.analysis_id, {"FAILED"}) == "FAILED"
    store = EcoProjectStore(project)
    try:
        failed = next(
            item for item in store.direct_jobs(plan.analysis_id) if item["state"] == "FAILED"
        )
        assert failed["error"] == {
            "code": "DIRECT_EXECUTION_FAILED",
            "category": "ENGINE_FAILURE",
            "retryable": False,
            "technical_type": "EngineProtocolError",
        }
        assert failed["resource_observation"]["boundary"] == "DIRECT_SOLVER"
        assert failed["resource_observation"]["energy_status"] == "ESTIMATED"
        assert failed["resource_observation"]["energy_kwh"] > 0
        assert {item["role"] for item in failed["terminal_artifacts"]}.issuperset(
            {"request", "result_protocol", "stdout", "stderr"}
        )
        assert store.analysis_results(plan.analysis_id) == ([], [], [])
        assert store.run_artifacts(failed["direct_job_id"]) == []
    finally:
        store.close()


def test_pinned_geotiff_adapter_requires_exact_reviewed_grid(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    numpy = pytest.importorskip("numpy")
    path = tmp_path / "reviewed-resistance.tif"
    transform = rasterio.transform.from_origin(100, 200, 10, 10)
    values = numpy.array([[2.0, 3.0], [4.0, 5.0]], dtype="float64")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float64",
        crs="EPSG:3857",
        transform=transform,
        nodata=-9999.0,
    ) as target:
        target.write(values, 1)
    authorization = SensitiveDataAuthorization(
        asset_id="asset-geotiff",
        ownership="Test fixture",
        license_spdx_or_name="CC0-1.0",
        sensitivity=SensitivityClass.PUBLIC,
        cloud_disposition=CloudDisposition.RAW_ALLOWED,
        export_allowed=True,
        retention_policy="Test only",
        spatial_redaction="None",
        approved_deployments=["LOCAL_ONLY"],
        reviewer="Test reviewer",
        reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    asset = ConnectivityAsset(
        asset_id="asset-geotiff",
        kind=AssetKind.RESISTANCE_RASTER,
        title="Reviewed resistance",
        relative_path=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
        media_type="image/tiff",
        source="Synthetic test",
        vintage="2026",
        license="CC0-1.0",
        grid=RasterGrid(
            grid_id="test-grid",
            crs="EPSG:3857",
            width=2,
            height=2,
            transform=(10.0, 0.0, 100.0, 0.0, -10.0, 200.0),
            nodata=-9999.0,
            value_kind=RasterValueKind.CONTINUOUS,
            cell_area_m2=100,
        ),
        authorization=authorization,
    )
    loaded = load_raster(path, asset)
    assert loaded.values == ((2.0, 3.0), (4.0, 5.0))
    with pytest.raises(ValueError, match="transform differs"):
        load_raster(
            path,
            asset.model_copy(
                update={
                    "grid": asset.grid.model_copy(update={"transform": (20, 0, 100, 0, -20, 200)})
                }
            ),
        )
