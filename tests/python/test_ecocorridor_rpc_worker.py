from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core import rpc_worker
from ecocorridor_core.analysis_executor import (
    AnalysisOwnershipError,
    DurableAnalysisExecutor,
    OrphanProcessAuditError,
)
from ecocorridor_core.claim_safety import assert_safe_rendered_ecological_copy
from ecocorridor_core.contracts import (
    AssetKind,
    ConnectivityAsset,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    RasterGrid,
    RasterValueKind,
)
from ecocorridor_core.interventions import canonicalize_portfolio
from ecocorridor_core.metrics import build_pair_metric_claim
from ecocorridor_core.observability import EcoRpcObservabilityAdapter
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_public_ui_projection, build_ridgeway_fixture
from ecocorridor_core.rpc_worker import (
    ExportValidateParams,
    RPCDispatcher,
    RPCFault,
    run_jsonl_worker,
)
from scenario_platform.cloud_sync import InMemoryCredentialSession
from scenario_platform.observability import (
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_asset_payloads() -> dict[str, bytes]:
    fixture = build_ridgeway_fixture()
    return {asset_id: _json_bytes(raster.values) for asset_id, raster in fixture.rasters.items()}


def _write_fixture_bundle(path: Path) -> None:
    fixture = build_ridgeway_fixture()
    payloads = _fixture_asset_payloads()
    for asset in fixture.bundle.assets:
        payload = payloads[asset.asset_id]
        assert hashlib.sha256(payload).hexdigest() == asset.sha256
        assert len(payload) == asset.byte_size
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "connectivity-project.json",
            _json_bytes(fixture.bundle.model_dump(mode="json")),
        )
        for asset in fixture.bundle.assets:
            archive.writestr(asset.relative_path, payloads[asset.asset_id])


def _write_fixture_bundle_with_manifest(path: Path, manifest: dict[str, Any]) -> None:
    fixture = build_ridgeway_fixture()
    payloads = _fixture_asset_payloads()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("connectivity-project.json", _json_bytes(manifest))
        for asset in fixture.bundle.assets:
            archive.writestr(asset.relative_path, payloads[asset.asset_id])


def _frame(method: str, params: dict[str, Any], request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _set_verified_binding(
    store: EcoProjectStore,
    *,
    state: str = "CONNECTED",
    deployment: str = "SELF_MANAGED",
) -> None:
    scope = ProjectScopeEvidence(
        entity="test-team",
        project="test-restricted-project",
        external_project_id="external-project-1",
        deployment=deployment,
        api_base_url="https://api.wandb.example",
        scope="RESTRICTED",
        evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
        provider_field="admin.project.scope",
    )
    store.set_observability(
        {
            "binding_id": "binding-test-exact-scope",
            "entity": scope.entity,
            "project": scope.project,
            "external_project_id": scope.external_project_id,
            "deployment": deployment,
            "api_base_url": scope.api_base_url,
            "web_base_url": "https://wandb.example",
            "profile_namespace": rpc_worker.APP_PROFILE.namespace,
            "scope_evidence": scope.model_dump(mode="json"),
            "state": state,
        }
    )


def _create_resource_estimate_project(project: Path) -> tuple[ConnectivityRunPlan, int]:
    fixture = build_ridgeway_fixture()
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="analysis-resource-estimate",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[baseline.portfolio_id],
        engine_manifest_sha256="a" * 64,
    )
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="IMPORTED",
            skeleton={"test": "authoritative resource estimate"},
        )
        store.store_bundle(fixture.bundle)
        store.create_analysis(plan, bundle=fixture.bundle)
    finally:
        store.close()
    asset_by_id = {item.asset_id: item for item in fixture.bundle.assets}
    hypothesis_by_id = {item.hypothesis_id: item for item in fixture.bundle.resistance_hypotheses}
    expected = 0
    for scenario in fixture.bundle.scenarios:
        grid = asset_by_id[
            hypothesis_by_id[scenario.resistance_hypothesis_id].resistance_asset_id
        ].grid
        assert grid is not None
        expected += grid.width * grid.height * 32 + 4_096
    return plan, expected


def _response(dispatcher: RPCDispatcher, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return json.loads(dispatcher.handle_frame(_frame(method, params)))


def _configured_engine_resources(tmp_path: Path) -> tuple[Path, str, EngineManifest, Path, str]:
    engine = tmp_path / "signed-engine"
    engine.write_bytes(b"test-only signed engine")
    engine_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    scenario = build_ridgeway_fixture().bundle.scenarios[0]
    manifest = EngineManifest(
        manifest_id="ownership-test-engine",
        executable_sha256=engine_digest,
        julia_runtime_sha256="1" * 64,
        julia_manifest_sha256="2" * 64,
        native_artifacts_sha256="3" * 64,
        wrapper_sha256="4" * 64,
        parser_sha256="5" * 64,
        metric_definitions_sha256="6" * 64,
        neighbor_rule=scenario.neighbor_rule,
        resistance_average=scenario.resistance_average,
        platform="test",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    manifest_path = tmp_path / "signed-engine-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return engine, engine_digest, manifest, manifest_path, manifest_digest


def test_candidate_only_export_selection_retains_baseline_qa_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    candidate = next(
        item for item in fixture.portfolios if item.portfolio_id != fixture.baseline_portfolio_id
    )
    bundle = fixture.bundle.model_copy(update={"scenarios": [scenario]})
    plan = ConnectivityRunPlan(
        analysis_id="analysis-candidate-only-export",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline, candidate],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=2,
        solver_job_cap=2,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="a" * 64,
    )

    def direct_run(portfolio_id: str) -> DirectConnectivityRun:
        identity = hashlib.sha256(f"{portfolio_id}:{scenario.scenario_id}".encode()).hexdigest()
        return DirectConnectivityRun(
            direct_run_id=f"run-{identity[:24]}",
            analysis_id=plan.analysis_id,
            revision_id=plan.revision_id,
            portfolio_id=portfolio_id,
            scenario_id=scenario.scenario_id,
            state=DirectRunState.SUCCEEDED,
            exact=True,
            engine_name="Circuitscape",
            engine_manifest_sha256=plan.engine_manifest_sha256,
            model_run_cache_key=identity,
            input_sha256="b" * 64,
            result_sha256="c" * 64,
            matrix_sha256="d" * 64,
            pair_metrics=[
                PairResistanceMetric(
                    pair_id=scenario.included_pair_ids[0],
                    effective_resistance=10,
                )
            ],
            qa_status=QAStatus.ACCEPTED,
            completed_at=datetime(2026, 8, 3, tzinfo=UTC),
        )

    baseline_run = direct_run(baseline.portfolio_id)
    candidate_run = direct_run(candidate.portfolio_id)
    baseline_qa = DurableAnalysisExecutor._qa_claim(claim_id="claim-baseline-qa", run=baseline_run)
    candidate_qa = DurableAnalysisExecutor._qa_claim(
        claim_id="claim-candidate-qa", run=candidate_run
    )
    summary = PortfolioDecisionSummary(
        portfolio_id=candidate.portfolio_id,
        disposition=PortfolioDisposition.PASSES,
        cost=0,
        currency="USD",
        rule_dispositions=[],
        next_action=(
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        ),
        direct_run_ids=[candidate_run.direct_run_id],
    )

    class AuthoritativeStore:
        def analysis_bundle(self, analysis_id: str):
            assert analysis_id == plan.analysis_id
            return bundle

        def analysis_results(self, analysis_id: str):
            assert analysis_id == plan.analysis_id
            return [baseline_run, candidate_run], [summary], [baseline_qa, candidate_qa]

        def analysis(self, analysis_id: str):
            assert analysis_id == plan.analysis_id
            return {"plan": plan.model_dump(mode="json")}

        def observability(self):
            return {}

    dispatcher = RPCDispatcher(data_root=tmp_path)
    monkeypatch.setattr(
        dispatcher,
        "_authoritative_project_context",
        lambda _store: ("LOCAL_ONLY", "CONNECTED", False),
    )
    monkeypatch.setattr(
        dispatcher,
        "_sync_plan",
        lambda _store, *, oversize_approved_by: ({}, {"blockers": []}),
    )
    _, _, selected_summaries, selected_runs, selected_claims, _ = dispatcher._eligibility(
        AuthoritativeStore(),  # type: ignore[arg-type]
        ExportValidateParams(
            project_directory=str(tmp_path),
            analysis_id=plan.analysis_id,
            portfolio_ids=[candidate.portfolio_id],
        ),
    )

    assert [item.portfolio_id for item in selected_summaries] == [candidate.portfolio_id]
    assert {item.direct_run_id for item in selected_runs} == {
        baseline_run.direct_run_id,
        candidate_run.direct_run_id,
    }
    assert {item.claim_id for item in selected_claims} == {
        baseline_qa.claim_id,
        candidate_qa.claim_id,
    }


def test_json_rpc_ping_is_versioned_and_unknown_methods_fail(tmp_path: Path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    ping = _response(dispatcher, "system.ping", {})
    assert ping["jsonrpc"] == "2.0"
    assert ping["result"]["api_version"] == "1.0"
    assert ping["result"]["engine_configured"] is False
    assert "connectivity.analysis.plan" in ping["result"]["methods"]

    missing = _response(dispatcher, "connectivity.browser.preview", {})
    assert missing["error"]["code"] == -32601
    assert missing["error"]["data"]["code"] == "RPC_PROTOCOL_ERROR"

    invalid = _response(dispatcher, "system.ping", {"ignored": True})
    assert invalid["error"]["code"] == -32602
    assert invalid["error"]["data"]["code"] == "RPC_PROTOCOL_ERROR"


def test_signed_engine_resource_may_live_outside_user_project_roots(tmp_path: Path) -> None:
    engine = tmp_path / "signed-resources" / "ecocorridor-engine"
    engine.parent.mkdir()
    engine.write_bytes(b"signed packaged engine placeholder")
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        engine_executable=engine,
        engine_sha256=digest,
        analysis_executor=object(),
    )
    assert dispatcher.dispatch("system.ping", {})["engine_configured"] is True


def test_packaged_engine_manifest_is_hash_pinned_and_binds_run_plans(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    engine = tmp_path / "signed-resources" / "ecocorridor-engine"
    engine.parent.mkdir()
    engine.write_bytes(b"signed packaged engine placeholder")
    engine_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    scenario = fixture.bundle.scenarios[0]
    manifest = EngineManifest(
        manifest_id="engine-release-1",
        executable_sha256=engine_digest,
        julia_runtime_sha256="1" * 64,
        julia_manifest_sha256="2" * 64,
        native_artifacts_sha256="3" * 64,
        wrapper_sha256="4" * 64,
        parser_sha256="5" * 64,
        metric_definitions_sha256="6" * 64,
        neighbor_rule=scenario.neighbor_rule,
        resistance_average=scenario.resistance_average,
        platform="test-platform",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    manifest_path = engine.parent / "engine-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_file_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=manifest_path,
        engine_manifest_file_sha256=manifest_file_digest,
    )
    dispatcher.dispatch("connectivity.guided.open", {})
    baseline = fixture.portfolios[0]
    mismatched = ConnectivityRunPlan(
        analysis_id="manifest-mismatch",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=12,
        solver_job_cap=12,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="f" * 64,
    )
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "connectivity.analysis.plan",
            {
                "project_directory": str(tmp_path / "app-data" / "guided" / "ridgeway"),
                "plan": mismatched.model_dump(mode="json"),
            },
        )
    assert error.value.error.code == "ANALYSIS_ENGINE_MANIFEST_MISMATCH"


def test_jsonl_worker_preserves_one_response_per_frame(tmp_path: Path) -> None:
    source = io.StringIO(_frame("system.ping", {}, 1) + "\n" + _frame("system.ping", {}, 2) + "\n")
    output = io.StringIO()
    run_jsonl_worker(RPCDispatcher(data_root=tmp_path), source, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["id"] for item in responses] == [1, 2]
    assert all(item["result"]["api_version"] == "1.0" for item in responses)


def test_main_wires_one_shared_fail_closed_observability_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def capture_worker(dispatcher: RPCDispatcher, stdin: Any, stdout: Any) -> None:
        captured["dispatcher"] = dispatcher
        captured["stdin"] = stdin
        captured["stdout"] = stdout

    monkeypatch.setattr(rpc_worker, "run_jsonl_worker", capture_worker)

    assert (
        rpc_worker.main(
            [
                "--transport",
                "jsonl-stdio",
                "--api-version",
                "1.0",
                "--app-data-root",
                str(tmp_path),
            ]
        )
        == 0
    )

    dispatcher = captured["dispatcher"]
    assert isinstance(dispatcher.observability_adapter, EcoRpcObservabilityAdapter)
    assert dispatcher.scope_resolver is dispatcher.observability_adapter.scope_resolver
    assert dispatcher.assistant_runtime is not None
    assert dispatcher.assistant_runtime.scope_resolver is dispatcher.scope_resolver
    assert (
        dispatcher.assistant_runtime.observability_credentials
        is dispatcher.observability_adapter.credentials
        is dispatcher.observability_credentials
    )

    with pytest.raises(RPCFault) as scope_error:
        dispatcher.dispatch(
            "observability.preflight",
            {
                "entity": "eco-team",
                "project": "restricted-project",
                "deployment": "WANDB_CLOUD",
                "api_base_url": "https://api.wandb.ai",
                "api_key": "not-a-real-wandb-key-value",
            },
        )
    assert scope_error.value.error.code == "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"


def test_main_shuts_down_process_owned_work_after_stdin_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_calls: list[float] = []
    original_shutdown = RPCDispatcher.shutdown

    def capture_shutdown(self: RPCDispatcher, *, timeout_seconds: float = 5.0) -> bool:
        shutdown_calls.append(timeout_seconds)
        return original_shutdown(self, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(rpc_worker, "run_jsonl_worker", lambda *_args: None)
    monkeypatch.setattr(RPCDispatcher, "shutdown", capture_shutdown)

    assert (
        rpc_worker.main(
            [
                "--transport",
                "jsonl-stdio",
                "--api-version",
                "1.0",
                "--app-data-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert shutdown_calls == [5.0]


def test_bundle_inspect_and_import_verify_every_asset_hash(tmp_path: Path) -> None:
    archive = tmp_path / "ridgeway.ecocorridor.zip"
    project = tmp_path / "imported-project"
    _write_fixture_bundle(archive)
    dispatcher = RPCDispatcher(data_root=tmp_path)

    inspected = dispatcher.dispatch("connectivity.bundle.inspect", {"bundle_path": str(archive)})[
        "data"
    ]
    assert inspected["project_id"] == build_ridgeway_fixture().bundle.project_id
    assert inspected["asset_count"] == 16
    assert inspected["scenario_count"] == 12

    imported = dispatcher.dispatch(
        "connectivity.bundle.import",
        {"bundle_path": str(archive), "project_directory": str(project)},
    )["data"]
    assert imported["state"] == "IMPORTED"
    store = EcoProjectStore(project)
    try:
        assert store.effective_bundle().project_id == build_ridgeway_fixture().bundle.project_id
        manifest = store.get_record("import_manifest", imported["archive_sha256"])
        assert len(manifest["members"]) == 17
        assert all(len(item["sha256"]) == 64 for item in manifest["members"])
        events = [item for item in store.pending_outbox() if item["event_type"] == "bundle_import"]
        assert len(events) == 1
        reference = events[0]["payload"]["import_manifest"]
        assert reference["record_id"] == imported["archive_sha256"]
        assert reference["record_revision"] == 1
        retained = store.get_record_revision(
            "import_manifest", reference["record_id"], reference["record_revision"]
        )
        assert retained == manifest
    finally:
        store.close()


def test_import_manifest_conflict_rolls_back_bundle_and_outbox(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "atomic-import"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="IMPORTED",
            skeleton={"source_bundle_sha256": "a" * 64},
        )
        store.put_record(
            "import_manifest",
            "a" * 64,
            {"archive_sha256": "a" * 64, "materialized_directory": "sources/conflict"},
            allow_update=False,
        )
        with pytest.raises(ValueError, match="immutable import_manifest"):
            store.store_bundle(
                fixture.bundle,
                import_manifest_id="a" * 64,
                import_manifest={
                    "archive_sha256": "a" * 64,
                    "materialized_directory": "sources/expected",
                    "members": [],
                },
            )
        with pytest.raises(KeyError):
            store.get_record("bundle", fixture.bundle.project_id)
        assert not [
            item for item in store.pending_outbox() if item["event_type"] == "bundle_import"
        ]
    finally:
        store.close()


def test_bundle_import_rejects_a_tampered_asset(tmp_path: Path) -> None:
    archive = tmp_path / "tampered.ecocorridor.zip"
    fixture = build_ridgeway_fixture()
    payloads = _fixture_asset_payloads()
    first_asset = fixture.bundle.assets[0]
    payloads[first_asset.asset_id] += b"tampered"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "connectivity-project.json",
            _json_bytes(fixture.bundle.model_dump(mode="json")),
        )
        for asset in fixture.bundle.assets:
            bundle.writestr(asset.relative_path, payloads[asset.asset_id])

    dispatcher = RPCDispatcher(data_root=tmp_path)
    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch("connectivity.bundle.inspect", {"bundle_path": str(archive)})
    assert raised.value.error.code == "BUNDLE_ASSET_INTEGRITY_FAILED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine_mode", "RASTER_ADVANCED"),
        ("engine_mode", "NETWORK_PAIRWISE"),
        ("engine_mode", "ONE_TO_ALL"),
        ("engine_mode", "ALL_TO_ONE"),
        ("raster_semantics", "CONDUCTANCE"),
    ],
)
def test_bundle_transport_classifies_known_unsupported_circuitscape_modes(
    tmp_path: Path, field: str, value: str
) -> None:
    payload = build_ridgeway_fixture().bundle.model_dump(mode="json")
    payload["scenarios"][0][field] = value
    archive = tmp_path / f"unsupported-{field}-{value}.zip"
    _write_fixture_bundle_with_manifest(archive, payload)
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch("connectivity.bundle.inspect", {"bundle_path": str(archive)})

    assert raised.value.error.code == "UNSUPPORTED_FEATURE"
    assert raised.value.error.category.value == "UNSUPPORTED_FEATURE"
    assert raised.value.error.object_reference == f"scenarios[0].{field}"


def test_bundle_transport_keeps_unknown_malformed_modes_manifest_invalid(tmp_path: Path) -> None:
    payload = build_ridgeway_fixture().bundle.model_dump(mode="json")
    payload["scenarios"][0]["engine_mode"] = "QUANTUM_CORRIDOR"
    archive = tmp_path / "malformed-unknown-mode.zip"
    _write_fixture_bundle_with_manifest(archive, payload)
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.bundle.import",
            {
                "bundle_path": str(archive),
                "project_directory": str(tmp_path / "malformed-import"),
            },
        )

    assert raised.value.error.code == "BUNDLE_MANIFEST_INVALID"


def test_bundle_import_rejects_duplicate_scientific_identity(tmp_path: Path) -> None:
    payload = build_ridgeway_fixture().bundle.model_dump(mode="json")
    duplicate = dict(payload["resistance_hypotheses"][0])
    duplicate["title"] = "Conflicting duplicate resistance hypothesis"
    payload["resistance_hypotheses"].append(duplicate)
    archive = tmp_path / "duplicate-hypothesis.zip"
    project = tmp_path / "duplicate-hypothesis-project"
    _write_fixture_bundle_with_manifest(archive, payload)
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.bundle.import",
            {
                "bundle_path": str(archive),
                "project_directory": str(project),
            },
        )

    assert raised.value.error.code == "BUNDLE_MANIFEST_INVALID"
    assert raised.value.error.category.value == "MODEL_QUALITY"
    assert "resistance hypothesis IDs must be unique" in raised.value.error.technical_message
    assert not project.exists()


def test_planning_grid_and_validation_state_persist_in_project_sqlite(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "planning-project"
    dispatcher = RPCDispatcher(data_root=tmp_path)
    created = dispatcher.dispatch(
        "connectivity.planning.create",
        {
            "project_directory": str(project),
            "project_id": "planning-project",
            "title": "Planning project",
            "revision": fixture.bundle.revision.model_copy(
                update={"project_id": "planning-project", "revision_id": "planning-revision"}
            ).model_dump(mode="json"),
            "decision_brief": fixture.bundle.decision_brief.model_dump(mode="json"),
            "fitness": fixture.bundle.fitness.model_copy(
                update={"revision_id": "planning-revision"}
            ).model_dump(mode="json"),
            "resource_policy": fixture.bundle.resource_policy.model_dump(mode="json"),
            "scenarios": [item.model_dump(mode="json") for item in fixture.bundle.scenarios],
            "safeguards": [item.model_dump(mode="json") for item in fixture.bundle.safeguards],
            "review_masks": [item.model_dump(mode="json") for item in fixture.bundle.review_masks],
            "decision_rules": [
                item.model_dump(mode="json") for item in fixture.bundle.decision_rules
            ],
        },
    )["data"]
    assert created["state"] == "PLANNING_DRAFT"

    saved = dispatcher.dispatch(
        "connectivity.hypothesis.save",
        {
            "project_directory": str(project),
            "hypothesis": fixture.bundle.resistance_hypotheses[0].model_dump(mode="json"),
        },
    )["data"]
    assert saved["revision"] == 1

    source = tmp_path / "reviewed-resistance.asc"
    source.write_text(
        "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 10\n"
        "NODATA_value -9999\n10 20\n30 40\n",
        encoding="utf-8",
    )
    grid = RasterGrid(
        grid_id="planning-grid",
        crs="EPSG:3857",
        width=2,
        height=2,
        transform=(10, 0, 0, 0, -10, 20),
        nodata=-9999,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=100,
    )
    inspected = dispatcher.dispatch(
        "connectivity.asset.inspect",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "grid_id": grid.grid_id,
            "value_kind": "CONTINUOUS",
            "format": "ESRI_ASCII",
            "esri_ascii_crs": grid.crs,
            "esri_ascii_cell_area_m2": grid.cell_area_m2,
        },
    )["data"]["inspection"]
    assert inspected["grid"] == grid.model_dump(mode="json")
    source_asset = fixture.bundle.assets[0]
    asset = ConnectivityAsset(
        asset_id="planning-resistance",
        kind=AssetKind.RESISTANCE_RASTER,
        title="Reviewed planning resistance",
        relative_path="data_assets/planning-resistance.asc",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        byte_size=source.stat().st_size,
        media_type="application/x-esri-ascii-grid",
        source="User-provided test fixture",
        vintage="2026",
        license="CC0-1.0",
        grid=grid,
        authorization=source_asset.authorization.model_copy(
            update={"asset_id": "planning-resistance"}
        ),
    )
    added = dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )["data"]
    assert added["inspection"]["minimum"] == 10
    preview = dispatcher.dispatch(
        "connectivity.grid.preview",
        {
            "project_directory": str(project),
            "source_asset_id": asset.asset_id,
            "target_grid": grid.model_dump(mode="json"),
            "method": "NONE",
            "plan_id": "alignment-1",
        },
    )["data"]["plan"]
    assert preview["source_asset_id"] == asset.asset_id
    with pytest.raises(RPCFault) as reused_grid_id:
        dispatcher.dispatch(
            "connectivity.grid.preview",
            {
                "project_directory": str(project),
                "source_asset_id": asset.asset_id,
                "target_grid": grid.model_copy(
                    update={"transform": (10, 0, 1, 0, -10, 20)}
                ).model_dump(mode="json"),
                "method": "NEAREST",
                "plan_id": "alignment-reused-grid-id",
            },
        )
    assert reused_grid_id.value.error.code == "GRID_ID_GEOMETRY_CONFLICT"
    derived_authorization = source_asset.authorization.model_copy(
        update={"asset_id": "planning-resistance-aligned"}
    )
    approval_params = {
        "project_directory": str(project),
        "plan_id": "alignment-1",
        "approval_id": "alignment-approval-1",
        "reviewer": "Ecologist",
        "approved_at": datetime(2026, 8, 3, tzinfo=UTC).isoformat(),
        "acknowledged_warnings": preview["warnings"],
        "purpose": "Explicit no-change grid confirmation",
        "derived_asset_id": "planning-resistance-aligned",
        "derived_title": "Reviewed aligned planning resistance",
        "derived_relative_path": "data_assets/derived/planning-resistance-aligned.asc",
        "derived_authorization": derived_authorization.model_dump(mode="json"),
        "revision_id": "planning-revision-aligned",
        "reason": "Approved explicit no-change alignment for test",
    }
    approved = dispatcher.dispatch("connectivity.grid.approve", approval_params)["data"]
    assert approved["approval"]["plan_id"] == "alignment-1"
    assert approved["original_asset_retained"] == asset.asset_id
    assert Path(project, approved["derived_asset"]["relative_path"]).is_file()
    assert dispatcher.dispatch("connectivity.grid.approve", approval_params)["data"] == approved

    validation = dispatcher.dispatch(
        "connectivity.model.validate", {"project_directory": str(project)}
    )["data"]
    assert validation["status"] == "BLOCKED"
    assert validation["findings"][0]["code"] == "PLANNING_MODEL_INCOMPLETE"

    store = EcoProjectStore(project)
    try:
        assert store.project()["mode"] == "PLANNING"
        assert store.get_record("grid_alignment_plan", "alignment-1")["plan_id"] == "alignment-1"
        assert (
            store.get_record("revision", "planning-revision-aligned")["parent_revision_id"]
            == "planning-revision"
        )
        assert store.get_record("validation", "latest")["status"] == "BLOCKED"
    finally:
        store.close()


def test_guided_projection_replays_verified_evidence_without_a_packaged_rerun_engine(
    tmp_path: Path,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    assert opened["state"] == "VERIFIED_FIXTURE_REPLAY"
    assert opened["projection"]["evidenceState"] == "VERIFIED_DIRECT_EVIDENCE"
    assert opened["projection"]["releaseReplayEligible"] is True
    assert opened["projection"]["study"]["matrixJobCount"] == 1536
    assert opened["projection"]["study"]["mapJobCount"] == 60
    assert opened["evidence_manifest_sha256"] == opened["projection"]["manifestSha256"]
    assert all(item["id"] is not None for item in opened["projection"]["portfolios"])
    assert all(len(item["scenarioResults"]) == 12 for item in opened["projection"]["portfolios"])
    assert opened["can_rerun"] is False
    reopened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    assert reopened["session_id"] == opened["session_id"]
    assert reopened["state"] == opened["state"]

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.guided.rerun",
            {
                "session_id": opened["session_id"],
                "project_directory": str(tmp_path / "guided" / "ridgeway"),
            },
        )
    assert raised.value.error.code == "PACKAGED_CIRCUITSCAPE_ENGINE_UNAVAILABLE"


def test_guided_blocked_projection_replaces_legacy_labels_at_presentation_boundary(
    tmp_path: Path,
) -> None:
    projection_path = tmp_path / "blocked-ridgeway.json"
    projection_path.write_text(
        json.dumps(build_public_ui_projection(build_ridgeway_fixture()), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        guided_projection_path=projection_path,
    )

    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    projection = opened["projection"]

    assert opened["state"] == "ENGINE_REQUIRED"
    assert projection["releaseReplayEligible"] is False
    assert {item["projectionKey"]: item["name"] for item in projection["portfolios"]}[
        "over-budget"
    ] == "Modeled-rule pass, over budget"
    rendered = json.dumps(projection, sort_keys=True).casefold()
    assert "robust candidate" not in rendered
    assert "robust but" not in rendered
    assert_safe_rendered_ecological_copy(projection)


def test_guided_open_verifies_current_evidence_without_materializing_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_materialization(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("opening the replay must not materialize retained run artifacts")

    monkeypatch.setattr(
        rpc_worker.GuidedEvidenceResource,
        "materialize",
        unexpected_materialization,
    )
    dispatcher = RPCDispatcher(data_root=tmp_path)
    assert dispatcher.guided_evidence is not None
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    assert opened["state"] == "VERIFIED_FIXTURE_REPLAY"
    assert opened["projection"]["releaseReplayEligible"] is True
    assert opened["projection"]["evidenceState"] == "VERIFIED_DIRECT_EVIDENCE"


def test_guided_checkpoints_and_cloud_outbox_are_one_idempotent_record(
    tmp_path: Path,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    project = tmp_path / "guided" / "ridgeway"
    params = {
        "session_id": opened["session_id"],
        "project_directory": str(project),
        "event_id": "guided-event-1",
        "sequence_number": 1,
        "checkpoint_id": "decision-brief",
        "event_kind": "COMPLETED",
        "duration_ms": 4_200,
        "evidence_ids": ["fixture-notice"],
        "portfolio_ids": [],
    }
    first = dispatcher.dispatch("connectivity.guided.checkpoint", params)["data"]
    repeated = dispatcher.dispatch("connectivity.guided.checkpoint", params)["data"]
    assert repeated == first

    store = EcoProjectStore(project)
    try:
        checkpoints = store.guided_checkpoints(opened["session_id"])
        assert len(checkpoints) == 1
        assert checkpoints[0]["checkpoint_id"] == "decision-brief"
        pending = store.pending_outbox()
        assert {item["event_type"] for item in pending} >= {
            "bundle_import",
            "guided_session",
            "guided_checkpoint",
        }
        assert sum(item["event_type"] == "guided_checkpoint" for item in pending) == 1
    finally:
        store.close()

    with pytest.raises(RPCFault) as conflict:
        dispatcher.dispatch(
            "connectivity.guided.checkpoint",
            {**params, "checkpoint_id": "model-fitness"},
        )
    assert conflict.value.error.code == "REQUEST_VALIDATION_FAILED"


def test_analysis_gates_results_and_engine_start(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    dispatcher = RPCDispatcher(data_root=tmp_path)
    dispatcher.dispatch("connectivity.guided.open", {})
    project = tmp_path / "guided" / "ridgeway"
    baseline = fixture.portfolios[0]
    plan = ConnectivityRunPlan(
        analysis_id="analysis-1",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=12,
        solver_job_cap=12,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="a" * 64,
    )
    planned = dispatcher.dispatch(
        "connectivity.analysis.plan",
        {"project_directory": str(project), "plan": plan.model_dump(mode="json")},
    )["data"]
    assert planned["arithmetic"] == "1 portfolios × 12 scenarios = 12 direct Circuitscape jobs"

    forged = baseline.model_copy(update={"portfolio_id": "portfolio_forged_identity"})
    forged_plan = plan.model_copy(
        update={
            "analysis_id": "analysis-forged",
            "portfolios": [forged],
            "baseline_portfolio_id": forged.portfolio_id,
        }
    )
    with pytest.raises(RPCFault) as forged_error:
        dispatcher.dispatch(
            "connectivity.analysis.plan",
            {
                "project_directory": str(project),
                "plan": forged_plan.model_dump(mode="json"),
            },
        )
    assert forged_error.value.error.code == "ANALYSIS_PLAN_INVALID"

    status = dispatcher.dispatch(
        "connectivity.analysis.status",
        {"project_directory": str(project), "analysis_id": "analysis-1"},
    )["data"]
    assert status["state"] == "READY"
    assert status["completed_run_count"] == 0

    with pytest.raises(RPCFault) as start_error:
        dispatcher.dispatch(
            "connectivity.analysis.start",
            {"project_directory": str(project), "analysis_id": "analysis-1"},
        )
    assert start_error.value.error.code == "PACKAGED_CIRCUITSCAPE_ENGINE_UNAVAILABLE"

    incomplete = _response(
        dispatcher,
        "connectivity.results.decision_board",
        {"project_directory": str(project), "analysis_id": "analysis-1"},
    )
    assert incomplete["error"]["data"]["code"] == "DECISION_BOARD_ANALYSIS_INCOMPLETE"

    cancelled = dispatcher.dispatch(
        "connectivity.analysis.cancel",
        {"project_directory": str(project), "analysis_id": "analysis-1"},
    )["data"]
    assert cancelled["state"] == "CANCELLED"


def test_analysis_start_reports_busy_without_failing_authoritative_running_state(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    engine, engine_digest, manifest, manifest_path, manifest_digest = _configured_engine_resources(
        tmp_path
    )

    class BusyExecutor:
        def start(self, **_: Any) -> dict[str, Any]:
            raise AnalysisOwnershipError("owned by another worker")

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        allowed_roots=[tmp_path],
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=manifest_path,
        engine_manifest_file_sha256=manifest_digest,
        analysis_executor=BusyExecutor(),
    )
    project = tmp_path / "live-project"
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="analysis-live-owner",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=manifest.sha256,
    )
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="GUIDED",
            skeleton={"test": "live owner"},
        )
        store.store_bundle(fixture.bundle)
        store.create_analysis(plan, bundle=fixture.bundle)
        store.start_analysis_with_direct_jobs(
            plan.analysis_id,
            [
                {
                    "direct_job_id": f"owned-{index:02d}",
                    "portfolio_id": baseline.portfolio_id,
                    "scenario_id": scenario_id,
                    "phase": "MATRIX",
                    "sequence_number": index,
                    "emit_current_map": False,
                }
                for index, scenario_id in enumerate(plan.scenario_ids)
            ],
        )
    finally:
        store.close()

    with pytest.raises(RPCFault) as busy:
        dispatcher.dispatch(
            "connectivity.analysis.start",
            {"project_directory": str(project), "analysis_id": plan.analysis_id},
        )
    assert busy.value.error.code == "ANALYSIS_EXECUTION_BUSY"
    assert busy.value.error.retryable is True

    store = EcoProjectStore(project)
    try:
        authoritative = store.analysis(plan.analysis_id)
        assert authoritative["state"] == "RUNNING"
        assert authoritative["error"] is None
        assert store.direct_job_counts(plan.analysis_id) == {"QUEUED": 12}
    finally:
        store.close()


def test_analysis_start_blocks_orphan_recovery_without_mutating_authoritative_state(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    engine, engine_digest, manifest, manifest_path, manifest_digest = _configured_engine_resources(
        tmp_path
    )

    class OrphanAuditExecutor:
        def start(self, **_: Any) -> dict[str, Any]:
            raise OrphanProcessAuditError(
                "ORPHAN_RECEIPT_MISSING",
                "no one-use shutdown receipt covers the running direct jobs",
            )

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        allowed_roots=[tmp_path],
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=manifest_path,
        engine_manifest_file_sha256=manifest_digest,
        analysis_executor=OrphanAuditExecutor(),
    )
    project = tmp_path / "orphan-project"
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="analysis-orphan-audit",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=manifest.sha256,
    )
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="GUIDED",
            skeleton={"test": "orphan audit"},
        )
        store.store_bundle(fixture.bundle)
        store.create_analysis(plan, bundle=fixture.bundle)
        store.start_analysis_with_direct_jobs(
            plan.analysis_id,
            [
                {
                    "direct_job_id": f"orphan-{index:02d}",
                    "portfolio_id": baseline.portfolio_id,
                    "scenario_id": scenario_id,
                    "phase": "MATRIX",
                    "sequence_number": index,
                    "emit_current_map": False,
                }
                for index, scenario_id in enumerate(plan.scenario_ids)
            ],
        )
        before_analysis = store.analysis(plan.analysis_id)
        before_counts = store.direct_job_counts(plan.analysis_id)
    finally:
        store.close()

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.analysis.start",
            {"project_directory": str(project), "analysis_id": plan.analysis_id},
        )
    assert blocked.value.error.code == "ANALYSIS_ORPHAN_PROCESS_AUDIT_REQUIRED"
    assert blocked.value.error.severity.value == "BLOCKING"
    assert blocked.value.error.technical_message == "ORPHAN_RECEIPT_MISSING"
    assert blocked.value.error.retryable is False

    store = EcoProjectStore(project)
    try:
        assert store.analysis(plan.analysis_id) == before_analysis
        assert store.direct_job_counts(plan.analysis_id) == before_counts
    finally:
        store.close()


def test_guided_recovery_reports_busy_without_rewriting_session_state(tmp_path: Path) -> None:
    engine, engine_digest, _, manifest_path, manifest_digest = _configured_engine_resources(
        tmp_path
    )

    class BusyRecoveryExecutor:
        def recover(self, **_: Any) -> None:
            raise AnalysisOwnershipError("owned by another worker")

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=manifest_path,
        engine_manifest_file_sha256=manifest_digest,
        analysis_executor=BusyRecoveryExecutor(),
    )
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    project = tmp_path / "app-data" / "guided" / "ridgeway"
    store = EcoProjectStore(project)
    try:
        store.set_guided(opened["session_id"], "RUNNING", opened["projection"])
        store.put_record(
            "guided_analysis",
            opened["session_id"],
            {"analysis_id": "analysis-owned-ridgeway"},
        )
    finally:
        store.close()

    with pytest.raises(RPCFault) as busy:
        dispatcher.dispatch("connectivity.guided.open", {"project_directory": str(project)})
    assert busy.value.error.code == "ANALYSIS_EXECUTION_BUSY"

    store = EcoProjectStore(project)
    try:
        assert store.guided(opened["session_id"])["state"] == "RUNNING"
    finally:
        store.close()


def test_guided_recovery_blocks_orphan_audit_without_rewriting_session_state(
    tmp_path: Path,
) -> None:
    engine, engine_digest, _, manifest_path, manifest_digest = _configured_engine_resources(
        tmp_path
    )

    class OrphanAuditRecoveryExecutor:
        def recover(self, **_: Any) -> None:
            raise OrphanProcessAuditError(
                "ORPHAN_RECEIPT_IDENTITY_MISMATCH",
                "the shutdown receipt does not match this engine identity",
            )

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app-data",
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=manifest_path,
        engine_manifest_file_sha256=manifest_digest,
        analysis_executor=OrphanAuditRecoveryExecutor(),
    )
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    project = tmp_path / "app-data" / "guided" / "ridgeway"
    store = EcoProjectStore(project)
    try:
        store.set_guided(opened["session_id"], "RUNNING", opened["projection"])
        store.put_record(
            "guided_analysis",
            opened["session_id"],
            {"analysis_id": "analysis-orphan-ridgeway"},
        )
        before = store.guided(opened["session_id"])
    finally:
        store.close()

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch("connectivity.guided.open", {"project_directory": str(project)})
    assert blocked.value.error.code == "ANALYSIS_ORPHAN_PROCESS_AUDIT_REQUIRED"
    assert blocked.value.error.severity.value == "BLOCKING"
    assert blocked.value.error.technical_message == "ORPHAN_RECEIPT_IDENTITY_MISMATCH"
    assert blocked.value.error.retryable is False

    store = EcoProjectStore(project)
    try:
        assert store.guided(opened["session_id"]) == before
    finally:
        store.close()


def test_professional_analysis_and_export_policy_cannot_be_supplied_by_renderer(
    tmp_path: Path,
) -> None:
    project = tmp_path / "professional-no-binding"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id="professional-no-binding",
            title="Professional project without cloud authority",
            mode="IMPORTED",
            skeleton={"test": "binding authority"},
        )
    finally:
        store.close()
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(RPCFault) as prepare_error:
        dispatcher.dispatch(
            "connectivity.analysis.prepare",
            {"project_directory": str(project), "seed": 0},
        )
    assert prepare_error.value.error.code == "OBSERVABILITY_BINDING_REQUIRED"

    fixture = build_ridgeway_fixture()
    baseline = fixture.portfolios[0]
    plan = ConnectivityRunPlan(
        analysis_id="analysis-unbound",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="a" * 64,
    )
    with pytest.raises(RPCFault) as plan_error:
        dispatcher.dispatch(
            "connectivity.analysis.plan",
            {"project_directory": str(project), "plan": plan.model_dump(mode="json")},
        )
    assert plan_error.value.error.code == "OBSERVABILITY_BINDING_REQUIRED"
    with pytest.raises(RPCFault) as start_error:
        dispatcher.dispatch(
            "connectivity.analysis.start",
            {"project_directory": str(project), "analysis_id": plan.analysis_id},
        )
    assert start_error.value.error.code == "OBSERVABILITY_BINDING_REQUIRED"

    for method, params in (
        (
            "connectivity.export.validate",
            {
                "project_directory": str(project),
                "analysis_id": "analysis-forged",
                "portfolio_ids": ["portfolio-forged"],
                "deployment": "LOCAL_ONLY",
            },
        ),
        (
            "connectivity.export.validate",
            {
                "project_directory": str(project),
                "analysis_id": "analysis-forged",
                "portfolio_ids": ["portfolio-forged"],
                "sync_state": "CONNECTED",
            },
        ),
        (
            "connectivity.export.create",
            {
                "project_directory": str(project),
                "analysis_id": "analysis-forged",
                "portfolio_ids": ["portfolio-forged"],
                "professional": False,
                "export_id": "forged-learning-export",
            },
        ),
    ):
        with pytest.raises(RPCFault) as bypass_error:
            dispatcher.dispatch(method, params)
        assert bypass_error.value.error.code == "RPC_CONTRACT_REJECTED"

    store = EcoProjectStore(project)
    try:
        store.set_observability(
            {
                "entity": "test-team",
                "project": "test-project",
                "external_project_id": "external-project-1",
                "deployment": "SELF_MANAGED",
                "profile_namespace": rpc_worker.APP_PROFILE.namespace,
                "scope_evidence": {"scope": "RESTRICTED"},
                "state": "CONNECTED",
            }
        )
    finally:
        store.close()
    with pytest.raises(RPCFault) as malformed_error:
        dispatcher.dispatch(
            "connectivity.analysis.prepare",
            {"project_directory": str(project), "seed": 0},
        )
    assert malformed_error.value.error.code == "OBSERVABILITY_BINDING_NOT_VERIFIED"


def test_synchronous_completed_executor_payload_is_rejected(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    focal_set = next(
        item
        for item in fixture.bundle.focal_area_sets
        if item.focal_set_id == scenario.focal_set_id
    )
    required_assets = {
        hypothesis.resistance_asset_id,
        focal_set.asset_id,
    }
    site = fixture.bundle.intervention_sites[0]
    required_assets.add(site.footprint_asset_id)
    site_options = [
        item
        for item in fixture.bundle.intervention_options
        if item.site_id == site.site_id and item.kind.value in {"NO_ACTION", "SURVEY_FIRST"}
    ]
    site = site.model_copy(update={"option_ids": [item.option_id for item in site_options]})
    decision_rule = fixture.bundle.decision_rules[0].model_copy(
        update={
            "scenario_ids": [scenario.scenario_id],
            "pair_ids": [scenario.included_pair_ids[0]],
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
            "decision_brief": fixture.bundle.decision_brief.model_copy(
                update={"target_guilds": [scenario.guild_id], "safeguard_guilds": []}
            ),
            "assets": [item for item in fixture.bundle.assets if item.asset_id in required_assets],
            "resistance_hypotheses": [hypothesis],
            "focal_area_sets": [focal_set],
            "scenarios": [scenario],
            "intervention_sites": [site],
            "intervention_options": site_options,
            "cost_basis": fixture.bundle.cost_basis.model_copy(
                update={
                    "option_costs": {
                        item.option_id: fixture.bundle.cost_basis.option_costs[item.option_id]
                        for item in site_options
                    }
                }
            ),
            "safeguards": [],
            "review_masks": [],
            "decision_rules": [decision_rule],
        }
    )
    project = tmp_path / "direct-project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="IMPORTED",
            skeleton={"test": "direct executor boundary"},
        )
        store.store_bundle(bundle)
        _set_verified_binding(store)
        source_payloads = _fixture_asset_payloads()
        for asset in bundle.assets:
            path = project / asset.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source_payloads[asset.asset_id])
    finally:
        store.close()

    engine = tmp_path / "ecocorridor-engine"
    engine.write_bytes(b"test-only executable boundary")
    engine_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    engine_manifest = EngineManifest(
        manifest_id="test-direct-export-engine",
        executable_sha256=engine_digest,
        julia_runtime_sha256="1" * 64,
        julia_manifest_sha256="2" * 64,
        native_artifacts_sha256="3" * 64,
        wrapper_sha256="4" * 64,
        parser_sha256="5" * 64,
        metric_definitions_sha256="6" * 64,
        neighbor_rule=scenario.neighbor_rule,
        resistance_average=scenario.resistance_average,
        platform="test",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    engine_manifest_path = tmp_path / "engine-manifest.json"
    engine_manifest_path.write_text(
        json.dumps(engine_manifest.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    engine_manifest_file_digest = hashlib.sha256(engine_manifest_path.read_bytes()).hexdigest()
    no_action = next(item for item in site_options if item.kind.value == "NO_ACTION")
    portfolio = canonicalize_portfolio(
        bundle.intervention_sites,
        bundle.intervention_options,
        {site.site_id: no_action.option_id},
    )
    plan = ConnectivityRunPlan(
        analysis_id="analysis-direct",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[portfolio],
        baseline_portfolio_id=portfolio.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=engine_manifest.sha256,
    )
    focal_pairs = {item.pair_id: item for item in focal_set.pairs}
    retained_matrix = "".join(
        (
            f"{focal_pairs[pair_id].from_focal_id}\t"
            f"{focal_pairs[pair_id].to_focal_id}\t{10.0 + index:.17g}\n"
        )
        for index, pair_id in enumerate(scenario.included_pair_ids)
    ).encode()
    retained_payloads = {
        "request": b'{"request":"direct-test"}\n',
        "result_protocol": b'{"status":"ok"}\n',
        "matrix": retained_matrix,
        "input_resistance": b"ncols 1\nnrows 1\n1\n",
        "input_focal": b"ncols 1\nnrows 1\n1\n",
        "input_included_pairs": b"1\t2\n",
        "circuitscape_configuration": b"[Options]\nscenario = pairwise\n",
        "action_raster_diff": b'{"changed_cells":[]}\n',
        "footprint_proof_document": b'{"proofs":[]}\n',
    }
    retained_digests = {
        role: hashlib.sha256(payload).hexdigest() for role, payload in retained_payloads.items()
    }

    class Executor:
        def start(self, **request: Any) -> dict[str, Any]:
            received_plan = request["plan"]
            metrics = [
                PairResistanceMetric(
                    pair_id=pair_id,
                    effective_resistance=10 + index,
                    baseline_effective_resistance=10 + index,
                    baseline_relative_change_percent=0,
                )
                for index, pair_id in enumerate(scenario.included_pair_ids)
            ]
            run = DirectConnectivityRun(
                direct_run_id="run-direct",
                analysis_id=received_plan.analysis_id,
                revision_id=received_plan.revision_id,
                portfolio_id=portfolio.portfolio_id,
                scenario_id=scenario.scenario_id,
                state=DirectRunState.SUCCEEDED,
                exact=True,
                engine_name="Circuitscape",
                engine_manifest_sha256=received_plan.engine_manifest_sha256,
                model_run_cache_key="b" * 64,
                input_sha256=retained_digests["request"],
                result_sha256=retained_digests["result_protocol"],
                matrix_sha256=retained_digests["matrix"],
                pair_metrics=metrics,
                qa_status=QAStatus.ACCEPTED,
                completed_at=datetime(2026, 8, 3, tzinfo=UTC),
            )
            rule = fixture.bundle.decision_rules[0].model_copy(
                update={
                    "scenario_ids": [scenario.scenario_id],
                    "pair_ids": [scenario.included_pair_ids[0]],
                }
            )
            claim = build_pair_metric_claim(
                claim_id="claim-direct",
                run=run,
                baseline_run=run,
                metric=metrics[0],
                rule=rule,
                raster_diff_sha256=retained_digests["action_raster_diff"],
                source_asset_ids=sorted(required_assets),
                assumption_ids=["reviewed-test-assumption"],
                authorization_ids=["auth-1", "auth-2", "auth-3"],
            )
            summary = PortfolioDecisionSummary(
                portfolio_id=portfolio.portfolio_id,
                disposition=PortfolioDisposition.PASSES,
                cost=0,
                currency="USD",
                rule_dispositions=[],
                controlling_scenario_id=scenario.scenario_id,
                controlling_pair_id=scenario.included_pair_ids[0],
                worst_resistance_reduction_percent=0,
                safeguard_passed=None,
                next_action=(
                    "Candidate to carry forward for ecological, field, and engineering "
                    "feasibility review."
                ),
                direct_run_ids=[run.direct_run_id],
                disposition_evidence_claim_id="claim-direct-disposition",
            )
            cost_claim = DurableAnalysisExecutor._cost_claim(
                claim_id="claim-direct-cost",
                bundle=bundle,
                portfolio=portfolio,
                cost=summary.cost,
                currency=summary.currency,
            )
            qa_claim = DurableAnalysisExecutor._qa_claim(
                claim_id="claim-direct-qa",
                run=run,
            )
            disposition_claim = DurableAnalysisExecutor._portfolio_disposition_claim(
                claim_id="claim-direct-disposition",
                summary=summary,
                runs=[run],
                supporting_claim_ids=[
                    claim.claim_id,
                    cost_claim.claim_id,
                    qa_claim.claim_id,
                ],
            )
            executor_store = EcoProjectStore(Path(request["project_directory"]))
            try:
                executor_store.start_analysis_with_direct_jobs(
                    received_plan.analysis_id,
                    [
                        {
                            "direct_job_id": run.direct_run_id,
                            "portfolio_id": run.portfolio_id,
                            "scenario_id": run.scenario_id,
                            "phase": "MATRIX",
                            "sequence_number": 0,
                            "emit_current_map": False,
                        }
                    ],
                )
                assert executor_store.claim_next_direct_job(received_plan.analysis_id) is not None
                executor_store.set_direct_job_prepared(
                    run.direct_run_id,
                    {
                        "prepared_run_id": "prepared-direct",
                        "analysis_id": run.analysis_id,
                        "revision_id": run.revision_id,
                        "portfolio_id": run.portfolio_id,
                        "scenario": scenario.model_dump(mode="json"),
                        "pairs": [
                            {
                                "pair_id": pair_id,
                                "from_focal_id": 1,
                                "to_focal_id": 2,
                            }
                            for pair_id in scenario.included_pair_ids
                        ],
                        "resources": [
                            {
                                "logical_name": logical_name,
                                "source_path": str(project / f"{logical_name}.dat"),
                                "sha256": retained_digests[role],
                                "target_filename": f"{logical_name}.dat",
                            }
                            for logical_name, role in (
                                ("resistance", "input_resistance"),
                                ("focal", "input_focal"),
                                ("included_pairs", "input_included_pairs"),
                            )
                        ],
                        "action_raster_sha256": retained_digests["action_raster_diff"],
                        "action_raster_diff_path": str(project / "action-raster-diff.json"),
                        "prepared_resistance_sha256": retained_digests["input_resistance"],
                        "footprint_proof_sha256": retained_digests["footprint_proof_document"],
                        "engine_manifest_sha256": received_plan.engine_manifest_sha256,
                        "model_run_cache_key": run.model_run_cache_key,
                        "emit_current_map": False,
                    },
                    run.model_run_cache_key,
                )
            finally:
                executor_store.close()
            return {
                "state": "COMPLETED",
                "runs": [run.model_dump(mode="json")],
                "summaries": [summary.model_dump(mode="json")],
                "claims": [
                    claim.model_dump(mode="json"),
                    cost_claim.model_dump(mode="json"),
                    qa_claim.model_dump(mode="json"),
                    disposition_claim.model_dump(mode="json"),
                ],
            }

        def pause(self, **_: Any) -> None:
            return None

        def resume(self, **_: Any) -> None:
            return None

        def cancel(self, **_: Any) -> None:
            return None

    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        engine_executable=engine,
        engine_sha256=engine_digest,
        engine_manifest=engine_manifest_path,
        engine_manifest_file_sha256=engine_manifest_file_digest,
        analysis_executor=Executor(),
    )
    # This boundary test uses compact JSON fixture placeholders rather than
    # raster files; bypass storage validation only to exercise the real search
    # planning/timing hook. Raster validation is covered by dedicated suites.
    dispatcher._validation_data = lambda _bundle, **_kwargs: {  # type: ignore[method-assign]
        "findings": []
    }
    prepared_response = dispatcher.dispatch(
        "connectivity.analysis.prepare",
        {"project_directory": str(project), "seed": 23},
    )["data"]
    prepared_plan = prepared_response["proposed_plan"]
    assert prepared_plan is not None, prepared_response["blockers"]
    search_resource = prepared_plan["search_resource_observation"]
    assert search_resource["boundary"] == "SEARCH_PROPOSER"
    assert search_resource["proposer_wall_seconds"] >= 0
    assert search_resource["peak_memory_status"] == "MEASURED"
    assert search_resource["peak_memory_bytes"] > 0
    assert search_resource["peak_memory_is_lower_bound"] is True
    assert search_resource["energy_status"] == "ESTIMATED"
    assert search_resource["energy_kwh"] > 0
    assert (
        search_resource["energy_estimate_lower_kwh"]
        <= search_resource["energy_kwh"]
        <= search_resource["energy_estimate_upper_kwh"]
    )
    assert "carbon" in " ".join(search_resource["exclusions"]).casefold()
    store = EcoProjectStore(project)
    try:
        # This executor-boundary test starts from an already reviewed plan;
        # professional plan gating is covered independently.
        store.create_analysis(plan, bundle=bundle)
    finally:
        store.close()
    with pytest.raises(RPCFault) as protocol_error:
        dispatcher.dispatch(
            "connectivity.analysis.start",
            {"project_directory": str(project), "analysis_id": plan.analysis_id},
        )
    assert protocol_error.value.error.code == "ANALYSIS_EXECUTOR_PROTOCOL_VIOLATION"

    store = EcoProjectStore(project)
    try:
        analysis = store.analysis(plan.analysis_id)
        runs, summaries, claims = store.analysis_results(plan.analysis_id)
        assert analysis["state"] == "FAILED"
        assert runs == []
        assert summaries == []
        assert claims == []
        assert store.run_artifacts("run-direct") == []
        assert "analysis_completion" not in {item["event_type"] for item in store.pending_outbox()}
    finally:
        store.close()


class _ObservabilityAdapter:
    def __init__(self) -> None:
        self.sync_plans: list[dict[str, Any]] = []
        self.sync_requests: list[dict[str, Any]] = []
        self.sync_result: dict[str, Any] | None = None

    def validate(self, *, binding: dict[str, Any]) -> dict[str, Any]:
        return {"external_project_id": binding["scope_evidence"]["external_project_id"]}

    def sync(self, **request: Any) -> dict[str, Any]:
        self.sync_requests.append(request)
        self.sync_plans.append(request["sync_plan"])
        return (
            dict(self.sync_result)
            if self.sync_result is not None
            else {"synced": request.get("online", True)}
        )

    def open_link(self, **_: Any) -> dict[str, Any]:
        return {"url": "https://example.invalid"}


def test_binding_revalidation_resolves_scope_inside_credential_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WANDB_API_KEY", raising=False)

    class CredentialCheckingScopeProvider:
        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            assert os.environ["WANDB_API_KEY"] == "not-a-real-wandb-key-value"
            return ProjectScopeEvidence(
                entity=request["entity"],
                project=request["project"],
                external_project_id="external-project-1",
                deployment=request["deployment"],
                api_base_url=request["api_base_url"],
                scope="RESTRICTED",
                evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
                provider_field="documented_admin.project_scope",
            )

    project = tmp_path / "credential-context-project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id="credential-context-project",
            title="Credential context",
            mode="PLANNING",
            skeleton={"test": True},
        )
        _set_verified_binding(store)
    finally:
        store.close()
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    with credentials.candidate("not-a-real-wandb-key-value"):
        pass
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        scope_resolver=ProjectScopeResolver(CredentialCheckingScopeProvider()),
        observability_adapter=_ObservabilityAdapter(),
        observability_credentials=credentials,
    )

    validated = dispatcher.dispatch(
        "observability.validate_binding", {"project_directory": str(project)}
    )["data"]
    assert validated["binding"]["state"] == "CONNECTED"
    assert "WANDB_API_KEY" not in os.environ


def test_observability_and_assistant_boundaries_fail_closed(tmp_path: Path) -> None:
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=_ObservabilityAdapter(),
    )
    with pytest.raises(RPCFault) as scope_error:
        dispatcher.dispatch(
            "observability.preflight",
            {
                "entity": "team",
                "project": "restricted-project",
                "deployment": "WANDB_CLOUD",
                "api_base_url": "https://api.wandb.ai",
                "api_key": "not-a-real-wandb-key-value",
            },
        )
    assert scope_error.value.error.code == "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"

    dispatcher.dispatch("connectivity.guided.open", {})
    project = tmp_path / "guided" / "ridgeway"
    with pytest.raises(RPCFault) as assistant_error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "call-1",
                "question": "Which evidence is available?",
                "evidence_ids": ["missing-claim"],
            },
        )
    assert assistant_error.value.error.code == "ASSISTANT_EVIDENCE_RESOLUTION_INCOMPLETE"


def test_observability_retry_keeps_verified_scope_and_resource_estimate(tmp_path: Path) -> None:
    class ScopeProvider:
        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            return ProjectScopeEvidence(
                entity=request["entity"],
                project=request["project"],
                external_project_id="external-restricted-project",
                deployment=request["deployment"],
                api_base_url=request["api_base_url"],
                scope="RESTRICTED",
                evidence_kind=ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
                provider_field="documented_exact_scope",
            )

    adapter = _ObservabilityAdapter()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=adapter,
        scope_resolver=ProjectScopeResolver(ScopeProvider()),
    )
    project = tmp_path / "resource-project"
    _plan, expected_bytes = _create_resource_estimate_project(project)
    preflight = dispatcher.dispatch(
        "observability.preflight",
        {
            "entity": "eco-team",
            "project": "ridgeway-restricted",
            "deployment": "WANDB_CLOUD",
            "api_base_url": "https://api.wandb.ai",
            "web_base_url": "https://wandb.ai",
            "api_key": "not-a-real-wandb-key-value",
        },
    )["data"]
    assert preflight["scope"] == "RESTRICTED"
    assert "api_key" not in json.dumps(preflight)
    connected = dispatcher.dispatch(
        "observability.connect",
        {
            "project_directory": str(project),
            "preflight_id": preflight["preflight_id"],
        },
    )["data"]
    assert connected["binding"]["state"] == "SYNC_QUEUED_OFFLINE"
    assert connected["upload_preview"]["estimated_current_raster_bytes"] == expected_bytes
    assert connected["upload_preview"]["current_raster_estimate_status"] == "PRE_RUN_ESTIMATE"
    assert connected["upload_preview"]["expected_current_raster_count"] == 12
    assert connected["upload_preview"]["retained_current_raster_count"] == 0
    status = dispatcher.dispatch("observability.get_status", {"project_directory": str(project)})[
        "data"
    ]
    assert status["state"] == "SYNC_QUEUED_OFFLINE"
    blocked_store = EcoProjectStore(project)
    try:
        claimed = blocked_store.claim_outbox(limit=1)
        assert len(claimed) == 1
        blocked_id = str(claimed[0]["outbox_id"])
        blocked_store.mark_outbox_failed(
            blocked_id,
            "CLOUD_POLICY_REVIEW_REQUIRED",
            blocked=True,
            lease_token=str(claimed[0]["lease_token"]),
        )
    finally:
        blocked_store.close()
    retried = dispatcher.dispatch("observability.retry", {"project_directory": str(project)})
    assert retried["method"] == "observability.retry"
    assert retried["data"]["receipt"] == {"synced": True}
    assert adapter.sync_plans[-1]["estimated_current_raster_bytes"] == expected_bytes
    assert adapter.sync_requests[-1]["online"] is True
    authoritative = EcoProjectStore(project)
    try:
        requeued_row = next(
            item for item in authoritative.telemetry_rows() if item["outbox_id"] == blocked_id
        )
        assert requeued_row["state"] == "PENDING"
        audits = authoritative.outbox_requeue_audits(outbox_id=blocked_id)
        assert len(audits) == 1
        assert audits[0]["blocked_error_code"] == "CLOUD_POLICY_REVIEW_REQUIRED"
        _deployment, export_sync_state, _professional = dispatcher._authoritative_project_context(
            authoritative
        )
    finally:
        authoritative.close()
    assert export_sync_state == "SYNC_QUEUED_OFFLINE"

    adapter.sync_result = {
        "sync_state": "BLOCKED",
        "blocked": 1,
        "remaining": 1,
    }
    blocked = dispatcher.dispatch("observability.retry", {"project_directory": str(project)})[
        "data"
    ]
    assert blocked["binding"]["state"] == "BLOCKED"
    assert blocked["receipt"]["remaining"] == 1


def test_observability_sync_queues_offline_without_online_adapter_path(tmp_path: Path) -> None:
    class ScopeProvider:
        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            return ProjectScopeEvidence(
                entity=request["entity"],
                project=request["project"],
                external_project_id="external-restricted-project",
                deployment=request["deployment"],
                api_base_url=request["api_base_url"],
                scope="RESTRICTED",
                evidence_kind=ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
                provider_field="documented_exact_scope",
            )

    adapter = _ObservabilityAdapter()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=adapter,
        scope_resolver=ProjectScopeResolver(ScopeProvider()),
    )
    project = tmp_path / "resource-project-offline"
    _plan, expected_bytes = _create_resource_estimate_project(project)
    preflight = dispatcher.dispatch(
        "observability.preflight",
        {
            "entity": "eco-team",
            "project": "ridgeway-restricted",
            "deployment": "WANDB_CLOUD",
            "api_base_url": "https://api.wandb.ai",
            "web_base_url": "https://wandb.ai",
            "api_key": "not-a-real-wandb-key-value",
        },
    )["data"]
    dispatcher.dispatch(
        "observability.connect",
        {
            "project_directory": str(project),
            "preflight_id": preflight["preflight_id"],
        },
    )

    queued = dispatcher.dispatch(
        "observability.sync",
        {"project_directory": str(project), "online": False},
    )["data"]

    assert queued["binding"]["state"] == "SYNC_QUEUED_OFFLINE"
    assert queued["receipt"] == {"synced": False}
    assert adapter.sync_requests[-1]["online"] is False
    assert (
        adapter.sync_requests[-1]["sync_plan"]["estimated_current_raster_bytes"] == expected_bytes
    )


def test_upload_preview_never_turns_an_unknown_plan_into_renderer_zero(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "no-resource-plan"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="IMPORTED",
            skeleton={"test": "unknown current-raster plan"},
        )
        store.store_bundle(fixture.bundle)
        _set_verified_binding(store)
    finally:
        store.close()
    dispatcher = RPCDispatcher(data_root=tmp_path)

    preview = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert preview["allowed"] is False
    assert preview["current_raster_estimate_status"] == "UNAVAILABLE"
    assert preview["estimated_current_raster_bytes"] is None
    assert preview["estimated_total_bytes"] is None
    assert any("CURRENT_RASTER_ESTIMATE_UNAVAILABLE" in item for item in preview["blockers"])

    with pytest.raises(RPCFault) as supplied_zero:
        dispatcher.dispatch(
            "observability.preview_upload",
            {
                "project_directory": str(project),
                "estimated_current_raster_bytes": 0,
            },
        )
    assert supplied_zero.value.error.code == "RPC_CONTRACT_REJECTED"


def test_archive_member_records_are_slots_dataclasses() -> None:
    """Guard the RPC serializer against relying on a slots object's __dict__."""

    from scenario_platform.archive import ArchiveMember

    member = ArchiveMember("asset.json", 10, 8, "a" * 64)
    assert asdict(member)["sha256"] == "a" * 64
    assert not hasattr(member, "__dict__")
