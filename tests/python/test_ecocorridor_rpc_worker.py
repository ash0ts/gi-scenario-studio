from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core import providers as provider_module
from ecocorridor_core import rpc_worker
from ecocorridor_core.analysis_executor import (
    AnalysisOwnershipError,
    DurableAnalysisExecutor,
    OrphanProcessAuditError,
)
from ecocorridor_core.claim_safety import assert_safe_rendered_ecological_copy
from ecocorridor_core.contracts import (
    OVERSIZE_SYNC_ACKNOWLEDGEMENT,
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
from ecocorridor_core.governance import create_oversize_sync_approval
from ecocorridor_core.interventions import canonicalize_portfolio
from ecocorridor_core.metrics import build_pair_metric_claim
from ecocorridor_core.observability import (
    ECOCORRIDOR_APP_PROFILE,
    EcoRpcObservabilityAdapter,
    derive_cloud_sync_state,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.providers import preview_download
from ecocorridor_core.ridgeway import build_public_ui_projection, build_ridgeway_fixture
from ecocorridor_core.rpc_worker import (
    ExportValidateParams,
    RPCDispatcher,
    RPCFault,
    run_jsonl_worker,
)
from ecocorridor_core.telemetry import EcoTelemetryProjector
from scenario_platform import archive as archive_module
from scenario_platform.cloud_sync import InMemoryCredentialSession
from scenario_platform.contracts import canonical_json
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
        payloads = _fixture_asset_payloads()
        for asset in fixture.bundle.assets:
            destination = project / asset.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payloads[asset.asset_id])
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


def _make_oversized_resource_project(project: Path) -> int:
    _, expected_bytes = _create_resource_estimate_project(project)
    store = EcoProjectStore(project)
    try:
        bundle = store.effective_bundle()
        policy = bundle.resource_policy.model_copy(
            update={"max_current_raster_bytes": max(1, expected_bytes - 1)}
        )
        store.publish_semantic_change(
            [("resource_policy", "current", policy.model_dump(mode="json"))],
            created_by="Resource reviewer",
            reason="Exercise exact-plan oversize approval.",
        )
        _set_verified_binding(store)
    finally:
        store.close()
    return expected_bytes


def _test_wandb_credentials() -> InMemoryCredentialSession:
    credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    with credentials.candidate("not-a-real-wandb-key-value"):
        pass
    return credentials


def _create_asset_ingest_planning_project(
    tmp_path: Path, *, project_name: str
) -> tuple[RPCDispatcher, Path, Any]:
    fixture = build_ridgeway_fixture()
    project = tmp_path / project_name
    dispatcher = RPCDispatcher(data_root=tmp_path)
    revision_id = f"{project_name}-revision"
    dispatcher.dispatch(
        "connectivity.planning.create",
        {
            "project_directory": str(project),
            "project_id": project_name,
            "title": "Verified asset ingest test",
            "revision": fixture.bundle.revision.model_copy(
                update={"project_id": project_name, "revision_id": revision_id}
            ).model_dump(mode="json"),
            "decision_brief": fixture.bundle.decision_brief.model_dump(mode="json"),
            "fitness": fixture.bundle.fitness.model_copy(
                update={"revision_id": revision_id}
            ).model_dump(mode="json"),
            "resource_policy": fixture.bundle.resource_policy.model_dump(mode="json"),
        },
    )
    return dispatcher, project, fixture


def _reviewed_document_asset(
    fixture: Any,
    source: Path,
    *,
    asset_id: str,
) -> ConnectivityAsset:
    payload = source.read_bytes()
    return ConnectivityAsset(
        asset_id=asset_id,
        kind=AssetKind.DOCUMENT,
        title="Reviewed evidence document",
        relative_path=f"data_assets/{asset_id}.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        media_type="application/octet-stream",
        source="User-provided test fixture",
        vintage="2026",
        license="CC0-1.0",
        authorization=fixture.bundle.assets[0].authorization.model_copy(
            update={"asset_id": asset_id}
        ),
    )


def _asset_publication_state(project: Path) -> dict[str, Any]:
    store = EcoProjectStore(project)
    try:
        return {
            "assets": store.list_records("asset"),
            "revisions": store.list_records("revision"),
            "semantic_diffs": store.list_records("semantic_diff"),
            "outbox": store.telemetry_rows(),
        }
    finally:
        store.close()


_PROVIDER_DOWNLOAD_URL = "https://www2.census.gov/geo/tiger/TIGER2025/ROADS/tl_2025_01001_roads.zip"
_PROVIDER_DOWNLOAD_BYTES = b"reviewed provider context"


class _ProviderDownloadResponse(io.BytesIO):
    def __init__(self, payload: bytes = _PROVIDER_DOWNLOAD_BYTES) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def geturl(self) -> str:
        return _PROVIDER_DOWNLOAD_URL

    def getcode(self) -> int:
        return 200


class _ProviderDownloadOpener:
    def __init__(self, payload: bytes = _PROVIDER_DOWNLOAD_BYTES) -> None:
        self.payload = payload
        self.open_count = 0

    def open(self, request: Any, timeout: float) -> _ProviderDownloadResponse:
        assert request.full_url == _PROVIDER_DOWNLOAD_URL
        assert timeout == 60.0
        self.open_count += 1
        return _ProviderDownloadResponse(self.payload)


def _provider_download_params(
    project: Path,
    fixture: Any,
    *,
    asset_id: str,
) -> dict[str, Any]:
    preview = preview_download(
        provider_id="tiger_line_2025",
        source_url=_PROVIDER_DOWNLOAD_URL,
        extent_description="Synthetic county planning context",
        expected_bytes=len(_PROVIDER_DOWNLOAD_BYTES),
    )
    authorization = fixture.bundle.assets[0].authorization.model_copy(
        update={
            "asset_id": asset_id,
            "license_spdx_or_name": preview.license,
        }
    )
    return {
        "project_directory": str(project),
        "provider_id": preview.provider_id,
        "source_url": preview.source_url,
        "extent_description": preview.extent_description,
        "expected_bytes": preview.expected_bytes,
        "asset_id": asset_id,
        "title": f"Reviewed provider asset {asset_id}",
        "kind": AssetKind.VECTOR_CONTEXT.value,
        "media_type": "application/zip",
        "authorization": authorization.model_dump(mode="json"),
        "explicit_consent": True,
        "maximum_bytes": 1024,
    }


def _set_provider_input_limit(project: Path, maximum_bytes: int) -> None:
    store = EcoProjectStore(project)
    try:
        policy = store.effective_bundle().resource_policy.model_copy(
            update={"max_input_bytes": maximum_bytes}
        )
        store.publish_semantic_change(
            [("resource_policy", "current", policy.model_dump(mode="json"))],
            created_by="Resource reviewer",
            reason="Set a focused provider-input limit for the concurrency test.",
        )
    finally:
        store.close()


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
        lambda _store: ({}, {"blockers": []}),
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


@pytest.mark.parametrize("replacement_phase", ["after_open", "before_manifest", "before_extract"])
def test_bundle_import_binds_archive_identity_across_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_phase: str,
) -> None:
    fixture = build_ridgeway_fixture()
    manifest_a = fixture.bundle.model_copy(update={"title": "Reviewed archive A"}).model_dump(
        mode="json"
    )
    manifest_b = fixture.bundle.model_copy(update={"title": "Replacement archive B"}).model_dump(
        mode="json"
    )
    selected = tmp_path / "selected.ecocorridor.zip"
    replacement = tmp_path / "replacement.ecocorridor.zip"
    _write_fixture_bundle_with_manifest(selected, manifest_a)
    _write_fixture_bundle_with_manifest(replacement, manifest_b)
    selected_sha256 = hashlib.sha256(selected.read_bytes()).hexdigest()
    with zipfile.ZipFile(selected, "r") as archive:
        selected_manifest_bytes = archive.read("connectivity-project.json")
    replacement_applied = False

    def replace_selected_path() -> None:
        nonlocal replacement_applied
        if replacement_applied:
            return
        os.replace(replacement, selected)
        replacement_applied = True

    if replacement_phase == "after_open":
        original_open = archive_module._open_zip

        @contextmanager
        def replace_after_open(path: str | Path):
            with original_open(path) as stream:
                replace_selected_path()
                yield stream

        monkeypatch.setattr(archive_module, "_open_zip", replace_after_open)
    elif replacement_phase == "before_manifest":
        original_read_member = archive_module.ValidatedZipSnapshot.read_member

        def replace_before_manifest(
            snapshot: archive_module.ValidatedZipSnapshot,
            member_path: str,
            *,
            max_bytes: int = 8 * 1024 * 1024,
        ) -> bytes:
            replace_selected_path()
            return original_read_member(snapshot, member_path, max_bytes=max_bytes)

        monkeypatch.setattr(
            archive_module.ValidatedZipSnapshot, "read_member", replace_before_manifest
        )
    else:
        original_extract = archive_module.ValidatedZipSnapshot.extract

        def replace_before_extract(
            snapshot: archive_module.ValidatedZipSnapshot, destination: str | Path
        ) -> list[Path]:
            replace_selected_path()
            return original_extract(snapshot, destination)

        monkeypatch.setattr(archive_module.ValidatedZipSnapshot, "extract", replace_before_extract)

    project = tmp_path / f"imported-{replacement_phase}"
    result = RPCDispatcher(data_root=tmp_path).dispatch(
        "connectivity.bundle.import",
        {
            "bundle_path": str(selected),
            "project_directory": str(project),
            "expected_sha256": selected_sha256,
        },
    )["data"]

    assert replacement_applied is True
    assert result["archive_sha256"] == selected_sha256
    assert hashlib.sha256(selected.read_bytes()).hexdigest() != selected_sha256
    store = EcoProjectStore(project)
    try:
        assert store.effective_bundle().title == "Reviewed archive A"
        retained_index = store.get_record("import_manifest", selected_sha256)
        retained_manifest_member = next(
            item
            for item in retained_index["members"]
            if item["path"] == "connectivity-project.json"
        )
        assert (
            retained_manifest_member["sha256"]
            == hashlib.sha256(selected_manifest_bytes).hexdigest()
        )
        retained_manifest_path = (
            project / retained_index["materialized_directory"] / "connectivity-project.json"
        )
        assert retained_manifest_path.read_bytes() == selected_manifest_bytes
        assert json.loads(retained_manifest_path.read_bytes())["title"] == "Reviewed archive A"
        events = [item for item in store.pending_outbox() if item["event_type"] == "bundle_import"]
        assert len(events) == 1
        assert events[0]["payload"]["import_manifest"]["record_id"] == selected_sha256
    finally:
        store.close()


@pytest.mark.parametrize(
    ("extra_kind", "object_reference"),
    [
        ("executable", "unexpected.exe"),
        ("directory", "unexpected-directory"),
        ("symlink", "unexpected-link"),
    ],
)
def test_bundle_import_rejects_unindexed_materialized_tree_entries_before_publication(
    tmp_path: Path,
    extra_kind: str,
    object_reference: str,
) -> None:
    archive = tmp_path / "reviewed.ecocorridor.zip"
    _write_fixture_bundle(archive)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    project = tmp_path / f"project-{extra_kind}"
    target = project / "sources" / archive_sha256
    archive_module.extract_zip(archive, target)

    extra = target / object_reference
    if extra_kind == "executable":
        extra.write_bytes(b"unindexed executable")
    elif extra_kind == "directory":
        extra.mkdir()
    else:
        try:
            extra.symlink_to(target / "connectivity-project.json")
        except OSError as exc:  # pragma: no cover - Windows privilege policy
            pytest.skip(f"symbolic links are unavailable on this test host: {exc}")

    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.bundle.import",
            {
                "bundle_path": str(archive),
                "project_directory": str(project),
                "expected_sha256": archive_sha256,
            },
        )

    assert raised.value.error.code == "MATERIALIZED_BUNDLE_INTEGRITY_FAILED"
    assert raised.value.error.severity.value == "BLOCKING"
    assert raised.value.error.object_reference == object_reference
    assert not (project / "project.sqlite").exists()


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


def test_bundle_import_rejects_assets_above_policy_before_project_publication(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    payloads = _fixture_asset_payloads()
    oversized = fixture.bundle.model_copy(
        update={
            "resource_policy": fixture.bundle.resource_policy.model_copy(
                update={"max_input_bytes": 1}
            )
        }
    )
    archive = tmp_path / "oversized-input-policy.ecocorridor"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "connectivity-project.json",
            _json_bytes(oversized.model_dump(mode="json")),
        )
        for asset in oversized.assets:
            output.writestr(asset.relative_path, payloads[asset.asset_id])
    project = tmp_path / "oversized-input-policy-project"
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.bundle.import",
            {
                "bundle_path": str(archive),
                "project_directory": str(project),
            },
        )

    assert blocked.value.error.code == "BUNDLE_INPUT_RESOURCE_POLICY_BLOCKED"
    assert not project.exists()


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


def test_asset_ingest_binds_open_inode_across_equal_length_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="source-swap-project"
    )
    source = tmp_path / "reviewed-source.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="source-swap-asset")
    destination = project / asset.relative_path
    original_stream = rpc_worker._stream_copy_and_digest
    swapped = False

    def replace_path_after_open(
        source_descriptor: int,
        target_descriptor: int,
        *,
        expected_bytes: int,
    ) -> tuple[int, str]:
        nonlocal swapped
        replacement = tmp_path / "equal-length-replacement.bin"
        replacement.write_bytes(b"tampered")
        os.replace(replacement, source)
        swapped = True
        return original_stream(
            source_descriptor,
            target_descriptor,
            expected_bytes=expected_bytes,
        )

    monkeypatch.setattr(rpc_worker, "_stream_copy_and_digest", replace_path_after_open)

    added = dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )["data"]

    assert swapped is True
    assert source.read_bytes() == b"tampered"
    assert destination.read_bytes() == b"reviewed"
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == added["sha256"]
    assert len(_asset_publication_state(project)["assets"]) == 1
    assert list(destination.parent.glob(f".{destination.name}.*")) == []


def test_asset_ingest_rejects_equal_length_in_place_mutation_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="source-mutation-project"
    )
    source = tmp_path / "reviewed-source-mutation.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="source-mutation-asset")
    destination = project / asset.relative_path
    before = _asset_publication_state(project)
    original_stream = rpc_worker._stream_copy_and_digest

    def mutate_open_inode_before_read(
        source_descriptor: int,
        target_descriptor: int,
        *,
        expected_bytes: int,
    ) -> tuple[int, str]:
        source.write_bytes(b"tampered")
        return original_stream(
            source_descriptor,
            target_descriptor,
            expected_bytes=expected_bytes,
        )

    monkeypatch.setattr(rpc_worker, "_stream_copy_and_digest", mutate_open_inode_before_read)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    assert raised.value.error.code == "ASSET_INGEST_INTEGRITY_MISMATCH"
    assert raised.value.error.severity.value == "BLOCKING"
    assert not destination.exists()
    assert list(destination.parent.glob(f".{destination.name}.*")) == []
    assert _asset_publication_state(project) == before


def test_asset_ingest_destination_race_never_clobbers_or_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="destination-race-project"
    )
    source = tmp_path / "reviewed-destination-race.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="destination-race-asset")
    destination = project / asset.relative_path
    before = _asset_publication_state(project)
    original_link = rpc_worker._link_file_noreplace
    racer_inode: int | None = None

    def create_conflict_then_link(staged: Path, target: Path) -> None:
        nonlocal racer_inode
        assert target == destination
        target.write_bytes(b"conflict")
        racer_inode = target.stat().st_ino
        original_link(staged, target)

    monkeypatch.setattr(rpc_worker, "_link_file_noreplace", create_conflict_then_link)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    assert raised.value.error.code == "ASSET_DESTINATION_CONFLICT"
    assert raised.value.error.severity.value == "BLOCKING"
    assert destination.read_bytes() == b"conflict"
    assert destination.stat().st_ino == racer_inode
    assert list(destination.parent.glob(f".{destination.name}.*")) == []
    assert _asset_publication_state(project) == before


def test_asset_ingest_reuses_identical_destination_idempotently(tmp_path: Path) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="identical-destination-project"
    )
    source = tmp_path / "reviewed-identical.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="identical-destination-asset")
    destination = project / asset.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    original_inode = destination.stat().st_ino
    params = {
        "project_directory": str(project),
        "source_path": str(source),
        "asset": asset.model_dump(mode="json"),
    }

    first = dispatcher.dispatch("connectivity.asset.add", params)["data"]
    after_first = _asset_publication_state(project)
    second = dispatcher.dispatch("connectivity.asset.add", params)["data"]

    assert first == second
    assert destination.read_bytes() == b"reviewed"
    assert destination.stat().st_ino == original_inode
    assert len(after_first["assets"]) == 1
    assert _asset_publication_state(project) == after_first


def test_asset_ingest_rejects_changed_bytes_for_an_existing_asset_id(tmp_path: Path) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="immutable-local-asset-project"
    )
    first_source = tmp_path / "immutable-local-first.bin"
    changed_source = tmp_path / "immutable-local-changed.bin"
    first_source.write_bytes(b"first-content")
    changed_source.write_bytes(b"other-content")
    first_asset = _reviewed_document_asset(
        fixture,
        first_source,
        asset_id="immutable-local-asset",
    )
    changed_asset = _reviewed_document_asset(
        fixture,
        changed_source,
        asset_id="immutable-local-asset",
    ).model_copy(update={"relative_path": "data_assets/immutable-local-changed.bin"})
    dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(first_source),
            "asset": first_asset.model_dump(mode="json"),
        },
    )
    before = _asset_publication_state(project)

    with pytest.raises(RPCFault) as changed:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(changed_source),
                "asset": changed_asset.model_dump(mode="json"),
            },
        )

    assert changed.value.error.code == "ASSET_ID_IMMUTABLE_CONFLICT"
    assert not (project / changed_asset.relative_path).exists()
    assert _asset_publication_state(project) == before


@pytest.mark.parametrize(
    "relative_path",
    [
        "outside-managed-input.bin",
        "data_assets/.downloads/.reserved-stage.token.download",
        "data_assets/.provider-download.lock",
        "data_assets/provider/tiger_line_2025/sha256/" + "a" * 64,
    ],
)
def test_local_asset_rejects_provider_operational_paths(tmp_path: Path, relative_path: str) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="reserved-local-asset-path-project"
    )
    source = tmp_path / "reserved-local-asset.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(
        fixture,
        source,
        asset_id="reserved-local-asset",
    ).model_copy(update={"relative_path": relative_path})

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    assert raised.value.error.code == "ASSET_PATH_RESERVED"
    store = EcoProjectStore(project)
    try:
        assert store.list_records("asset") == []
    finally:
        store.close()


def test_raster_contract_inspects_hash_verified_staged_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="staged-raster-project"
    )
    source = tmp_path / "reviewed-staged-raster.asc"
    source.write_text(
        "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 10\n"
        "NODATA_value -9999\n10 20\n30 40\n",
        encoding="utf-8",
    )
    grid = RasterGrid(
        grid_id="staged-raster-grid",
        crs="EPSG:3857",
        width=2,
        height=2,
        transform=(10, 0, 0, 0, -10, 20),
        nodata=-9999,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=100,
    )
    asset = ConnectivityAsset(
        asset_id="staged-raster-asset",
        kind=AssetKind.RESISTANCE_RASTER,
        title="Reviewed staged raster",
        relative_path="data_assets/staged-raster.asc",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        byte_size=source.stat().st_size,
        media_type="application/x-esri-ascii-grid",
        source="User-provided test fixture",
        vintage="2026",
        license="CC0-1.0",
        grid=grid,
        authorization=fixture.bundle.assets[0].authorization.model_copy(
            update={"asset_id": "staged-raster-asset"}
        ),
    )
    destination = project / asset.relative_path
    original_inspect = rpc_worker.inspect_reviewed_raster
    inspected_paths: list[Path] = []

    def inspect_staged(path: Path, reviewed_asset: ConnectivityAsset) -> Any:
        inspected_paths.append(Path(path))
        assert Path(path) != source
        assert Path(path).parent == project / "data_assets" / ".downloads"
        assert Path(path).read_bytes() == source.read_bytes()
        return original_inspect(path, reviewed_asset)

    monkeypatch.setattr(rpc_worker, "inspect_reviewed_raster", inspect_staged)

    added = dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )["data"]

    assert added["inspection"]["minimum"] == 10
    assert len(inspected_paths) == 1
    assert inspected_paths[0].name.startswith(".local_asset.")
    assert inspected_paths[0].name.endswith(".download")
    assert not inspected_paths[0].exists()
    assert destination.read_bytes() == source.read_bytes()


def test_asset_publication_failure_rolls_back_repository_and_reuses_exact_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="publication-failure-project"
    )
    source = tmp_path / "reviewed-publication-failure.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="publication-failure-asset")
    _set_provider_input_limit(project, asset.byte_size)
    destination = project / asset.relative_path
    before = _asset_publication_state(project)
    original_enqueue = EcoProjectStore._enqueue_outbox

    def fail_semantic_outbox(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("event_type") == "semantic_revision":
            raise OSError("injected transactional outbox failure")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        EcoProjectStore,
        "_enqueue_outbox",
        staticmethod(fail_semantic_outbox),
    )

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    assert raised.value.error.code == "ASSET_PUBLICATION_FAILED"
    assert raised.value.error.severity.value == "BLOCKING"
    assert raised.value.error.retryable is True
    assert destination.read_bytes() == b"reviewed"
    assert _asset_publication_state(project) == before
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == asset.byte_size
    finally:
        store.close()

    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(original_enqueue))
    recovered = dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )["data"]
    assert recovered["asset_id"] == asset.asset_id
    assert len(_asset_publication_state(project)["assets"]) == 1


def test_local_publication_orphan_blocks_a_different_asset_at_input_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="local-orphan-quota-project"
    )
    first_source = tmp_path / "local-orphan-first.bin"
    second_source = tmp_path / "local-orphan-second.bin"
    first_source.write_bytes(b"first123")
    second_source.write_bytes(b"second12")
    first = _reviewed_document_asset(fixture, first_source, asset_id="local-orphan-first")
    second = _reviewed_document_asset(fixture, second_source, asset_id="local-orphan-second")
    _set_provider_input_limit(project, first.byte_size)
    original_enqueue = EcoProjectStore._enqueue_outbox

    def fail_semantic_outbox(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("event_type") == "semantic_revision":
            raise OSError("injected local publication failure")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        EcoProjectStore,
        "_enqueue_outbox",
        staticmethod(fail_semantic_outbox),
    )
    with pytest.raises(RPCFault) as failed:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(first_source),
                "asset": first.model_dump(mode="json"),
            },
        )
    assert failed.value.error.code == "ASSET_PUBLICATION_FAILED"
    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(original_enqueue))

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(second_source),
                "asset": second.model_dump(mode="json"),
            },
        )

    assert blocked.value.error.code == "ASSET_RESOURCE_POLICY_BLOCKED"
    assert (project / first.relative_path).read_bytes() == first_source.read_bytes()
    assert not (project / second.relative_path).exists()
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == first.byte_size
        assert store.list_records("asset") == []
    finally:
        store.close()


def test_local_asset_aliases_share_one_physical_quota_charge(tmp_path: Path) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="local-shared-path-project"
    )
    source = tmp_path / "local-shared.bin"
    source.write_bytes(b"shared-local")
    first = _reviewed_document_asset(fixture, source, asset_id="local-shared-first")
    second = _reviewed_document_asset(
        fixture,
        source,
        asset_id="local-shared-second",
    ).model_copy(update={"relative_path": first.relative_path})
    _set_provider_input_limit(project, first.byte_size)

    for asset in (first, second):
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == first.byte_size
        assert {item["asset_id"] for item in store.list_records("asset")} == {
            first.asset_id,
            second.asset_id,
        }
    finally:
        store.close()


def test_provider_download_publication_failure_keeps_orphan_for_idempotent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-publication-failure-project"
    )
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())
    params = _provider_download_params(project, fixture, asset_id="provider-retry-asset")
    before = _asset_publication_state(project)
    digest = hashlib.sha256(_PROVIDER_DOWNLOAD_BYTES).hexdigest()
    retained = project / "data_assets" / "provider" / "tiger_line_2025" / "sha256" / digest
    original_enqueue = EcoProjectStore._enqueue_outbox

    def fail_semantic_outbox(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("event_type") == "semantic_revision":
            raise OSError("injected provider publication failure")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        EcoProjectStore,
        "_enqueue_outbox",
        staticmethod(fail_semantic_outbox),
    )

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch("connectivity.asset.download", params)

    assert raised.value.error.code == "ASSET_PUBLICATION_FAILED"
    assert raised.value.error.severity.value == "BLOCKING"
    assert raised.value.error.retryable is True
    assert retained.read_bytes() == _PROVIDER_DOWNLOAD_BYTES
    assert _asset_publication_state(project) == before

    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(original_enqueue))
    recovered = dispatcher.dispatch("connectivity.asset.download", params)["data"]
    after_recovery = _asset_publication_state(project)
    retained_inode = retained.stat().st_ino
    repeated = dispatcher.dispatch("connectivity.asset.download", params)["data"]

    assert recovered == repeated
    assert retained.read_bytes() == _PROVIDER_DOWNLOAD_BYTES
    assert retained.stat().st_ino == retained_inode
    assert len(after_recovery["assets"]) == 1
    assert _asset_publication_state(project) == after_recovery
    store = EcoProjectStore(project)
    try:
        receipts = store.list_records("provider_receipt")
    finally:
        store.close()
    assert len(receipts) == 1
    assert receipts[0]["created"] is False


def test_provider_cas_directory_fsync_failure_blocks_database_publication_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-fsync-failure-project"
    )
    opener = _ProviderDownloadOpener()
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=opener)
    params = _provider_download_params(project, fixture, asset_id="provider-fsync-retry")
    digest = hashlib.sha256(_PROVIDER_DOWNLOAD_BYTES).hexdigest()
    retained = project / "data_assets" / "provider" / "tiger_line_2025" / "sha256" / digest
    before = _asset_publication_state(project)
    original_fsync = provider_module._fsync_directory

    def fail_directory_fsync(_directory: Path) -> None:
        raise OSError("injected provider CAS directory fsync failure")

    monkeypatch.setattr(provider_module, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch("connectivity.asset.download", params)

    assert raised.value.error.code == "ASSET_PUBLICATION_FAILED"
    assert raised.value.error.retryable is True
    assert retained.read_bytes() == _PROVIDER_DOWNLOAD_BYTES
    assert _asset_publication_state(project) == before

    monkeypatch.setattr(provider_module, "_fsync_directory", original_fsync)
    recovered = dispatcher.dispatch("connectivity.asset.download", params)["data"]
    assert recovered["asset"]["asset_id"] == "provider-fsync-retry"
    assert opener.open_count == 2
    store = EcoProjectStore(project)
    try:
        assert len(store.list_records("asset")) == 1
        receipts = store.list_records("provider_receipt")
    finally:
        store.close()
    assert len(receipts) == 1
    assert receipts[0]["created"] is False


def test_provider_download_failed_creator_never_unlinks_reused_published_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-concurrent-publication-project"
    )
    first_dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())
    second_dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())
    first_params = _provider_download_params(project, fixture, asset_id="provider-first")
    second_params = _provider_download_params(project, fixture, asset_id="provider-second")
    original_publish = EcoProjectStore.publish_semantic_change

    def interleaved_publish(
        store: EcoProjectStore,
        records: Any,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if records and records[0][0:2] == ("asset", "provider-first"):
            raise OSError("injected first-writer publication failure")
        return original_publish(store, records, *args, **kwargs)

    monkeypatch.setattr(EcoProjectStore, "publish_semantic_change", interleaved_publish)
    with pytest.raises(RPCFault) as first_error:
        first_dispatcher.dispatch("connectivity.asset.download", first_params)
    first_fault = first_error.value
    assert first_fault.error.code == "ASSET_PUBLICATION_FAILED"
    assert first_fault.error.retryable is True
    second = second_dispatcher.dispatch("connectivity.asset.download", second_params)["data"]
    retained = project / second["asset"]["relative_path"]
    assert retained.read_bytes() == _PROVIDER_DOWNLOAD_BYTES
    store = EcoProjectStore(project)
    try:
        assets = store.list_records("asset")
        receipts = store.list_records("provider_receipt")
    finally:
        store.close()
    assert [item["asset_id"] for item in assets] == ["provider-second"]
    assert [item["asset_id"] for item in receipts] == ["provider-second"]


def test_provider_orphan_counts_against_input_limit_but_exact_retry_can_reuse_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-orphan-quota-project"
    )
    _set_provider_input_limit(project, len(_PROVIDER_DOWNLOAD_BYTES))
    opener = _ProviderDownloadOpener()
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=opener)
    params = _provider_download_params(project, fixture, asset_id="provider-orphan-retry")
    original_enqueue = EcoProjectStore._enqueue_outbox

    def fail_semantic_outbox(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("event_type") == "semantic_revision":
            raise OSError("injected provider publication failure")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        EcoProjectStore,
        "_enqueue_outbox",
        staticmethod(fail_semantic_outbox),
    )
    with pytest.raises(RPCFault) as failed:
        dispatcher.dispatch("connectivity.asset.download", params)
    assert failed.value.error.code == "ASSET_PUBLICATION_FAILED"

    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(_PROVIDER_DOWNLOAD_BYTES)
        assert store.list_records("asset") == []
        assert store.list_records("provider_receipt") == []
    finally:
        store.close()
    preview = dispatcher.dispatch(
        "connectivity.asset.download.preview",
        {
            key: params[key]
            for key in (
                "project_directory",
                "provider_id",
                "source_url",
                "extent_description",
                "expected_bytes",
                "selected_vintage",
                "selected_resolution",
            )
            if key in params
        },
    )["data"]
    assert preview["remaining_project_input_bytes"] == 0
    assert preview["resource_policy_allowed"] is False

    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(original_enqueue))
    recovered = dispatcher.dispatch("connectivity.asset.download", params)["data"]
    assert recovered["asset"]["asset_id"] == "provider-orphan-retry"
    assert opener.open_count == 2
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(_PROVIDER_DOWNLOAD_BYTES)
        assert len(store.list_records("asset")) == 1
        assert len(store.list_records("provider_receipt")) == 1
    finally:
        store.close()


def test_provider_orphan_at_input_limit_blocks_local_asset_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-orphan-local-quota-project"
    )
    _set_provider_input_limit(project, len(_PROVIDER_DOWNLOAD_BYTES))
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())
    provider_params = _provider_download_params(
        project,
        fixture,
        asset_id="provider-orphan-before-local",
    )
    original_enqueue = EcoProjectStore._enqueue_outbox

    def fail_semantic_outbox(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("event_type") == "semantic_revision":
            raise OSError("injected provider publication failure")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        EcoProjectStore,
        "_enqueue_outbox",
        staticmethod(fail_semantic_outbox),
    )
    with pytest.raises(RPCFault) as provider_failure:
        dispatcher.dispatch("connectivity.asset.download", provider_params)
    assert provider_failure.value.error.code == "ASSET_PUBLICATION_FAILED"
    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(original_enqueue))

    source = tmp_path / "local-after-provider-orphan.bin"
    source.write_bytes(b"local")
    local_asset = _reviewed_document_asset(
        fixture,
        source,
        asset_id="local-after-provider-orphan",
    )
    destination = project / local_asset.relative_path
    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": local_asset.model_dump(mode="json"),
            },
        )

    assert blocked.value.error.code == "ASSET_RESOURCE_POLICY_BLOCKED"
    assert not destination.exists()
    assert list(destination.parent.glob(f".{destination.name}.*")) == []
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(_PROVIDER_DOWNLOAD_BYTES)
        assert store.list_records("asset") == []
        assert store.list_records("provider_receipt") == []
    finally:
        store.close()


def test_imported_bundle_assets_count_against_local_asset_input_limit(tmp_path: Path) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="imported-base-asset-quota-project"
    )
    project_id = "imported-base-asset-quota-project"
    base_bytes = sum(asset.byte_size for asset in fixture.bundle.assets)
    imported = fixture.bundle.model_copy(
        update={
            "project_id": project_id,
            "title": "Imported base asset quota project",
            "revision": fixture.bundle.revision.model_copy(
                update={"project_id": project_id, "revision_id": f"{project_id}-revision"}
            ),
            "fitness": fixture.bundle.fitness.model_copy(
                update={"revision_id": f"{project_id}-revision"}
            ),
            "resource_policy": fixture.bundle.resource_policy.model_copy(
                update={"max_input_bytes": base_bytes}
            ),
        }
    )
    store = EcoProjectStore(project)
    try:
        store.put_record("bundle", project_id, imported.model_dump(mode="json"))
        assert store.retained_input_bytes() == base_bytes
    finally:
        store.close()
    _set_provider_input_limit(project, base_bytes)
    source = tmp_path / "local-after-imported-base.bin"
    source.write_bytes(b"local")
    asset = _reviewed_document_asset(fixture, source, asset_id="local-after-imported-base")

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.asset.add",
            {
                "project_directory": str(project),
                "source_path": str(source),
                "asset": asset.model_dump(mode="json"),
            },
        )

    assert blocked.value.error.code == "ASSET_RESOURCE_POLICY_BLOCKED"
    assert not (project / asset.relative_path).exists()


def test_concurrent_local_assets_cannot_both_publish_above_input_limit(tmp_path: Path) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="concurrent-local-quota-project"
    )
    first_source = tmp_path / "concurrent-local-first.bin"
    second_source = tmp_path / "concurrent-local-second.bin"
    first_source.write_bytes(b"first-content")
    second_source.write_bytes(b"other-content")
    assert first_source.stat().st_size == second_source.stat().st_size
    _set_provider_input_limit(project, first_source.stat().st_size)
    assets = [
        _reviewed_document_asset(fixture, first_source, asset_id="concurrent-local-first"),
        _reviewed_document_asset(fixture, second_source, asset_id="concurrent-local-second"),
    ]
    start_barrier = threading.Barrier(2)
    dispatchers = [RPCDispatcher(data_root=tmp_path), RPCDispatcher(data_root=tmp_path)]
    sources = [first_source, second_source]
    successes: list[dict[str, Any]] = []
    failures: list[RPCFault] = []

    def add_local(index: int) -> None:
        start_barrier.wait(10)
        try:
            successes.append(
                dispatchers[index].dispatch(
                    "connectivity.asset.add",
                    {
                        "project_directory": str(project),
                        "source_path": str(sources[index]),
                        "asset": assets[index].model_dump(mode="json"),
                    },
                )["data"]
            )
        except RPCFault as exc:
            failures.append(exc)

    threads = [threading.Thread(target=add_local, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].error.code == "ASSET_RESOURCE_POLICY_BLOCKED"
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == first_source.stat().st_size
        assert len(store.list_records("asset")) == 1
    finally:
        store.close()
    retained = [project / asset.relative_path for asset in assets]
    assert sum(path.exists() for path in retained) == 1
    assert all(list(path.parent.glob(f".{path.name}.*")) == [] for path in retained)


def test_local_asset_rechecks_concurrently_lowered_input_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="local-policy-race-project"
    )
    source = tmp_path / "local-policy-race.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="local-policy-race")
    staged_event = threading.Event()
    continue_event = threading.Event()
    original_stage = rpc_worker._stage_verified_file

    def pause_after_stage(*args: Any, **kwargs: Any) -> Any:
        staged = original_stage(*args, **kwargs)
        staged_event.set()
        assert continue_event.wait(10)
        return staged

    monkeypatch.setattr(rpc_worker, "_stage_verified_file", pause_after_stage)
    failures: list[RPCFault] = []

    def add_local() -> None:
        try:
            dispatcher.dispatch(
                "connectivity.asset.add",
                {
                    "project_directory": str(project),
                    "source_path": str(source),
                    "asset": asset.model_dump(mode="json"),
                },
            )
        except RPCFault as exc:
            failures.append(exc)

    thread = threading.Thread(target=add_local)
    thread.start()
    assert staged_event.wait(10)
    _set_provider_input_limit(project, asset.byte_size - 1)
    continue_event.set()
    thread.join(15)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert failures[0].error.code == "ASSET_RESOURCE_POLICY_BLOCKED"
    assert not (project / asset.relative_path).exists()
    store = EcoProjectStore(project)
    try:
        assert store.list_records("asset") == []
        assert store.retained_input_bytes() == 0
    finally:
        store.close()


def test_resource_policy_cannot_be_lowered_below_retained_input_bytes(tmp_path: Path) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="resource-policy-retained-input-project"
    )
    source = tmp_path / "resource-policy-retained-input.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="resource-policy-retained-input")
    dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )
    store = EcoProjectStore(project)
    try:
        active_policy = store.effective_bundle().resource_policy
        before = store.latest_record("resource_policy")
    finally:
        store.close()
    proposed = active_policy.model_copy(update={"max_input_bytes": asset.byte_size - 1})

    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "connectivity.resource_policy.save",
            {
                "project_directory": str(project),
                "resource_policy": proposed.model_dump(mode="json"),
            },
        )

    assert blocked.value.error.code == "RESOURCE_POLICY_INPUT_BYTES_BLOCKED"
    store = EcoProjectStore(project)
    try:
        assert store.latest_record("resource_policy") == before
        assert store.retained_input_bytes() == asset.byte_size
    finally:
        store.close()


def test_asset_commit_wins_race_against_lower_resource_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="asset-policy-reverse-race-project"
    )
    source = tmp_path / "asset-policy-reverse-race.bin"
    source.write_bytes(b"reviewed")
    asset = _reviewed_document_asset(fixture, source, asset_id="asset-policy-reverse-race")
    store = EcoProjectStore(project)
    try:
        active_policy = store.effective_bundle().resource_policy
    finally:
        store.close()
    proposed = active_policy.model_copy(update={"max_input_bytes": asset.byte_size - 1})
    policy_ready = threading.Event()
    asset_committed = threading.Event()
    original_publish = EcoProjectStore.publish_semantic_change

    def pause_policy_publication(
        store: EcoProjectStore,
        records: Any,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if records and records[0][0:2] == ("resource_policy", "current"):
            policy_ready.set()
            assert asset_committed.wait(10)
        return original_publish(store, records, *args, **kwargs)

    monkeypatch.setattr(EcoProjectStore, "publish_semantic_change", pause_policy_publication)
    policy_failures: list[RPCFault] = []

    def lower_policy() -> None:
        try:
            dispatcher.dispatch(
                "connectivity.resource_policy.save",
                {
                    "project_directory": str(project),
                    "resource_policy": proposed.model_dump(mode="json"),
                },
            )
        except RPCFault as exc:
            policy_failures.append(exc)

    thread = threading.Thread(target=lower_policy)
    thread.start()
    assert policy_ready.wait(10)
    dispatcher.dispatch(
        "connectivity.asset.add",
        {
            "project_directory": str(project),
            "source_path": str(source),
            "asset": asset.model_dump(mode="json"),
        },
    )
    asset_committed.set()
    thread.join(15)

    assert not thread.is_alive()
    assert len(policy_failures) == 1
    assert policy_failures[0].error.code == "RESOURCE_POLICY_INPUT_BYTES_BLOCKED"
    store = EcoProjectStore(project)
    try:
        assert store.effective_bundle().resource_policy == active_policy
        assert store.retained_input_bytes() == asset.byte_size
        assert len(store.list_records("asset")) == 1
    finally:
        store.close()


def test_two_assets_can_share_one_provider_blob_without_double_charging_quota(
    tmp_path: Path,
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-shared-blob-project"
    )
    _set_provider_input_limit(project, len(_PROVIDER_DOWNLOAD_BYTES))
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())

    first = dispatcher.dispatch(
        "connectivity.asset.download",
        _provider_download_params(project, fixture, asset_id="provider-shared-first"),
    )["data"]
    second = dispatcher.dispatch(
        "connectivity.asset.download",
        _provider_download_params(project, fixture, asset_id="provider-shared-second"),
    )["data"]

    assert first["asset"]["sha256"] == second["asset"]["sha256"]
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(_PROVIDER_DOWNLOAD_BYTES)
        assert len(store.list_records("asset")) == 2
        assert len(store.list_records("provider_receipt")) == 2
    finally:
        store.close()


def test_reused_provider_asset_id_with_changed_request_fails_before_download(
    tmp_path: Path,
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-immutable-id-project"
    )
    first_opener = _ProviderDownloadOpener()
    first_dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=first_opener)
    params = _provider_download_params(project, fixture, asset_id="provider-immutable")
    retained = first_dispatcher.dispatch("connectivity.asset.download", params)["data"]
    changed_bytes = b"changed provider context"
    changed_params = {**params, "expected_bytes": len(changed_bytes)}
    changed_opener = _ProviderDownloadOpener(changed_bytes)
    changed_dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=changed_opener)

    with pytest.raises(RPCFault) as changed:
        changed_dispatcher.dispatch("connectivity.asset.download", changed_params)

    assert changed.value.error.code == "ASSET_ID_IMMUTABLE_CONFLICT"
    assert changed.value.error.retryable is False
    assert changed_opener.open_count == 0
    assert first_dispatcher.dispatch("connectivity.asset.download", params)["data"] == retained
    assert first_opener.open_count == 1


def test_concurrent_different_provider_blobs_cannot_both_publish_above_input_limit(
    tmp_path: Path,
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-concurrent-quota-project"
    )
    first_bytes = b"provider-content-one"
    second_bytes = b"provider-content-two"
    assert len(first_bytes) == len(second_bytes)
    _set_provider_input_limit(project, len(first_bytes))
    start_barrier = threading.Barrier(2)
    dispatchers = [
        RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener(first_bytes)),
        RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener(second_bytes)),
    ]
    params = [
        _provider_download_params(project, fixture, asset_id="provider-quota-first"),
        _provider_download_params(project, fixture, asset_id="provider-quota-second"),
    ]
    for item in params:
        item["expected_bytes"] = len(first_bytes)
    successes: list[dict[str, Any]] = []
    failures: list[RPCFault] = []

    def download(index: int) -> None:
        start_barrier.wait(10)
        try:
            successes.append(
                dispatchers[index].dispatch("connectivity.asset.download", params[index])["data"]
            )
        except RPCFault as exc:
            failures.append(exc)

    threads = [threading.Thread(target=download, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].error.code == "ASSET_DOWNLOAD_RESOURCE_POLICY_BLOCKED"
    assert failures[0].error.retryable is False
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(first_bytes)
        assert len(store.list_records("asset")) == 1
        assert len(store.list_records("provider_receipt")) == 1
        provider_blobs = [
            path for path in (project / "data_assets" / "provider").rglob("*") if path.is_file()
        ]
        assert len(provider_blobs) == 1
    finally:
        store.close()


def test_provider_asset_rechecks_concurrently_lowered_input_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-policy-race-project"
    )
    _set_provider_input_limit(project, len(_PROVIDER_DOWNLOAD_BYTES))
    dispatcher = RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener())
    params = _provider_download_params(project, fixture, asset_id="provider-policy-race")
    staged_event = threading.Event()
    continue_event = threading.Event()
    original_stage = rpc_worker.stage_approved_asset

    def pause_after_stage(*args: Any, **kwargs: Any) -> Any:
        staged = original_stage(*args, **kwargs)
        staged_event.set()
        assert continue_event.wait(10)
        return staged

    monkeypatch.setattr(rpc_worker, "stage_approved_asset", pause_after_stage)
    failures: list[RPCFault] = []

    def download_provider() -> None:
        try:
            dispatcher.dispatch("connectivity.asset.download", params)
        except RPCFault as exc:
            failures.append(exc)

    thread = threading.Thread(target=download_provider)
    thread.start()
    assert staged_event.wait(10)
    _set_provider_input_limit(project, len(_PROVIDER_DOWNLOAD_BYTES) - 1)
    continue_event.set()
    thread.join(15)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert failures[0].error.code == "ASSET_DOWNLOAD_RESOURCE_POLICY_BLOCKED"
    store = EcoProjectStore(project)
    try:
        assert store.list_records("asset") == []
        assert store.list_records("provider_receipt") == []
        assert store.retained_input_bytes() == 0
    finally:
        store.close()


def test_concurrent_changed_bytes_for_one_provider_asset_id_are_rejected_atomically(
    tmp_path: Path,
) -> None:
    _, project, fixture = _create_asset_ingest_planning_project(
        tmp_path, project_name="provider-concurrent-id-project"
    )
    first_bytes = b"provider-content-one"
    second_bytes = b"provider-content-two"
    assert len(first_bytes) == len(second_bytes)
    _set_provider_input_limit(project, len(first_bytes) * 2)
    start_barrier = threading.Barrier(2)
    dispatchers = [
        RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener(first_bytes)),
        RPCDispatcher(data_root=tmp_path, download_opener=_ProviderDownloadOpener(second_bytes)),
    ]
    params = _provider_download_params(project, fixture, asset_id="provider-one-identity")
    params["expected_bytes"] = len(first_bytes)
    successes: list[dict[str, Any]] = []
    failures: list[RPCFault] = []

    def download(dispatcher: RPCDispatcher) -> None:
        start_barrier.wait(10)
        try:
            successes.append(dispatcher.dispatch("connectivity.asset.download", params)["data"])
        except RPCFault as exc:
            failures.append(exc)

    threads = [threading.Thread(target=download, args=(dispatcher,)) for dispatcher in dispatchers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].error.code == "ASSET_ID_IMMUTABLE_CONFLICT"
    assert failures[0].error.retryable is False
    store = EcoProjectStore(project)
    try:
        assert store.retained_input_bytes() == len(first_bytes)
        assert len(store.list_records("asset")) == 1
        assert len(store.list_records("provider_receipt")) == 1
    finally:
        store.close()


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
        return {
            "external_project_id": binding["scope_evidence"]["external_project_id"],
            "profile_namespace": "ecocorridor",
            "verified": True,
        }

    def sync(self, **request: Any) -> dict[str, Any]:
        self.sync_requests.append(request)
        self.sync_plans.append(request["sync_plan"])
        if self.sync_result is not None:
            return dict(self.sync_result)
        store = EcoProjectStore(Path(request["project_directory"]))
        try:
            rows = store.telemetry_rows()
        finally:
            store.close()
        sync_state, state_counts = derive_cloud_sync_state(
            binding_state="CONNECTED",
            telemetry_rows=rows,
        )
        online = bool(request.get("online", True))
        return {
            "online": online,
            "claimed": 0,
            "synced": 0,
            "failed": 0,
            "blocked": 0,
            "queued_offline": not online,
            "remaining": sum(count for state, count in state_counts.items() if state != "SYNCED"),
            "state_counts": state_counts,
            "sync_state": sync_state,
        }

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
    assert retried["data"]["receipt"]["online"] is True
    assert retried["data"]["receipt"]["remaining"] >= 1
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
        "online": True,
        "claimed": 1,
        "synced": 0,
        "failed": 0,
        "sync_state": "BLOCKED",
        "blocked": 1,
        "remaining": 1,
        "queued_offline": False,
        "state_counts": {"BLOCKED": 1},
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
    assert queued["receipt"]["online"] is False
    assert queued["receipt"]["queued_offline"] is True
    assert queued["receipt"]["remaining"] >= 1
    assert adapter.sync_requests[-1]["online"] is False
    assert (
        adapter.sync_requests[-1]["sync_plan"]["estimated_current_raster_bytes"] == expected_bytes
    )


def test_oversize_approval_is_immutable_plan_bound_and_survives_restart(tmp_path: Path) -> None:
    class StableScopeProvider:
        def __init__(self, external_project_id: str = "external-project-1") -> None:
            self.external_project_id = external_project_id
            self.calls = 0

        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            self.calls += 1
            return ProjectScopeEvidence(
                entity=request["entity"],
                project=request["project"],
                external_project_id=self.external_project_id,
                deployment=request["deployment"],
                api_base_url=request["api_base_url"],
                scope="RESTRICTED",
                evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
                provider_field="admin.project.scope",
            )

    project = tmp_path / "oversize-plan-bound"
    expected_bytes = _make_oversized_resource_project(project)
    provider = StableScopeProvider()
    adapter = _ObservabilityAdapter()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=adapter,
        scope_resolver=ProjectScopeResolver(provider),
        observability_credentials=_test_wandb_credentials(),
    )

    preview = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert preview["estimated_current_raster_bytes"] == expected_bytes
    assert preview["approval_status"] == "REQUIRED"
    assert preview["allowed"] is False

    approved = dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": preview["sync_plan_sha256"],
            "reviewed_by": "Dr. Ecologist",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )["data"]
    assert approved["upload_preview"]["allowed"] is True
    assert approved["upload_preview"]["approval_status"] == "APPROVED"
    assert approved["approval"]["sync_plan_sha256"] == preview["sync_plan_sha256"]
    invariant_fields = {
        "sync_plan_sha256",
        "model_revision_id",
        "model_sha256",
        "entity",
        "project",
        "external_project_id",
        "input_bytes",
        "estimated_current_raster_bytes",
        "telemetry_inventory_sha256",
        "telemetry_projected_bytes",
        "telemetry_artifact_count",
        "approval_audit_reservation_bytes",
        "approval_audit_reservation_count",
        "operational_metadata_reservation_bytes",
        "operational_metadata_reservation_count",
        "estimated_total_bytes",
    }
    assert preview["telemetry_projected_bytes"] >= 16 * 1024
    assert {key: preview[key] for key in invariant_fields} == {
        key: approved["upload_preview"][key] for key in invariant_fields
    }

    # A lost response can be retried without changing reviewer/time or adding
    # another audit event. A different payload cannot mutate that evidence.
    retry = dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": preview["sync_plan_sha256"],
            "reviewed_by": "Dr. Ecologist",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )["data"]
    assert retry["approval"] == approved["approval"]
    assert {key: retry["upload_preview"][key] for key in invariant_fields} == {
        key: preview[key] for key in invariant_fields
    }
    with pytest.raises(RPCFault) as conflicting_retry:
        dispatcher.dispatch(
            "observability.approve_oversize",
            {
                "project_directory": str(project),
                "sync_plan_sha256": preview["sync_plan_sha256"],
                "reviewed_by": "Another reviewer",
                "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
            },
        )
    assert conflicting_retry.value.error.code == "OVERSIZE_SYNC_APPROVAL_CONFLICT"

    store = EcoProjectStore(project)
    try:
        retained = store.oversize_sync_approval(preview["sync_plan_sha256"])
        assert retained is not None
        assert retained.reviewed_by == "Dr. Ecologist"
        approval_events = store.telemetry_rows(event_type="oversize_sync_approval")
        assert len(approval_events) == 1
        assert approval_events[0]["subject_id"] == retained.approval_id
        assert approval_events[0]["payload"]["reviewed_by"] == "Dr. Ecologist"
        assert "acknowledgement" not in approval_events[0]["payload"]
        audit_projection = EcoTelemetryProjector(
            store=store,
            project_root=project,
            sync_plan=approved["upload_preview"],
            profile=ECOCORRIDOR_APP_PROFILE,
        )(approval_events[0], approval_events[0]["payload"])
        assert audit_projection.event_type == "oversize_sync_approval"
        assert {table.logical_name for table in audit_projection.tables} >= {
            "oversize_sync_approvals"
        }
        assert (
            len((canonical_json(audit_projection.model_dump(mode="json")) + "\n").encode())
            <= 16 * 1024
        )
        with pytest.raises(ValueError, match="immutable oversize_sync_approval"):
            store.record_oversize_sync_approval(
                retained.model_copy(update={"reviewed_by": "Another reviewer"})
            )
    finally:
        store.close()

    # A new worker reads the immutable approval and stable exact-scope evidence.
    restarted = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=adapter,
        scope_resolver=ProjectScopeResolver(provider),
        observability_credentials=_test_wandb_credentials(),
    )
    restarted.dispatch("observability.validate_binding", {"project_directory": str(project)})
    after_restart = restarted.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert after_restart["sync_plan_sha256"] == preview["sync_plan_sha256"]
    assert after_restart["approval_status"] == "APPROVED"
    synced = restarted.dispatch(
        "observability.sync", {"project_directory": str(project), "online": True}
    )["data"]
    assert synced["receipt"]["online"] is True
    assert synced["receipt"]["remaining"] >= 1
    assert provider.calls >= 1


def test_oversize_approval_record_and_outbox_are_producer_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "oversize-atomic"
    _make_oversized_resource_project(project)
    dispatcher = RPCDispatcher(data_root=tmp_path, observability_adapter=_ObservabilityAdapter())
    store = EcoProjectStore(project)
    try:
        _, plan = dispatcher._cloud_sync_plan(store, use_persisted_approval=False)
        approval = create_oversize_sync_approval(
            plan,
            reviewed_by="Atomic reviewer",
            approved_at=datetime.now(UTC),
        )

        def fail_outbox(*_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("simulated outbox failure")

        monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(fail_outbox))
        with pytest.raises(RuntimeError, match="simulated outbox failure"):
            store.record_oversize_sync_approval(approval)
        assert store.oversize_sync_approval(plan.approval_subject.sha256) is None
        assert store.telemetry_rows(event_type="oversize_sync_approval") == []
    finally:
        store.close()


def test_operational_retry_metadata_stays_inside_approved_reservation(tmp_path: Path) -> None:
    project = tmp_path / "oversize-operational-reserve"
    _make_oversized_resource_project(project)
    dispatcher = RPCDispatcher(data_root=tmp_path, observability_adapter=_ObservabilityAdapter())
    reviewed = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": reviewed["sync_plan_sha256"],
            "reviewed_by": "Retry reviewer",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )

    store = EcoProjectStore(project)
    try:
        bundle_row = store._connection.execute(
            "SELECT outbox_id FROM eco_telemetry_outbox "
            "WHERE event_type='bundle_import' ORDER BY outbox_sequence LIMIT 1"
        ).fetchone()
        assert bundle_row is not None
        store._connection.execute(
            "UPDATE eco_telemetry_outbox SET state='FAILED',attempt_count=1,"
            "last_error_code='PROVIDER_RETRY',updated_at=? WHERE outbox_id=?",
            (datetime.now(UTC).isoformat(), str(bundle_row["outbox_id"])),
        )
    finally:
        store.close()
    failed_preview = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert failed_preview["approval_status"] == "APPROVED"
    assert failed_preview["sync_plan_sha256"] == reviewed["sync_plan_sha256"]
    assert (
        failed_preview["operational_metadata_reservation_bytes"]
        == reviewed["operational_metadata_reservation_bytes"]
    )

    store = EcoProjectStore(project)
    try:
        store._connection.execute(
            "UPDATE eco_telemetry_outbox SET state='SYNCED',last_error_code=NULL,"
            "external_reference_json=?,updated_at=? WHERE outbox_id=?",
            (
                canonical_json(
                    {
                        "run_id": "provider-run",
                        "envelope_id": "provider-envelope",
                        "artifacts": [
                            {"provider_artifact_ref": "entity/project/connectivity-project:v1"}
                        ],
                    }
                ),
                datetime.now(UTC).isoformat(),
                str(bundle_row["outbox_id"]),
            ),
        )
    finally:
        store.close()
    synced_preview = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert synced_preview["approval_status"] == "APPROVED"
    assert synced_preview["sync_plan_sha256"] == reviewed["sync_plan_sha256"]
    assert synced_preview["telemetry_inventory_sha256"] == reviewed["telemetry_inventory_sha256"]


def test_stale_approval_cannot_hide_a_nonapproval_resource_blocker(tmp_path: Path) -> None:
    project = tmp_path / "oversize-stale-resource-blocker"
    _make_oversized_resource_project(project)
    dispatcher = RPCDispatcher(data_root=tmp_path, observability_adapter=_ObservabilityAdapter())
    reviewed = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": reviewed["sync_plan_sha256"],
            "reviewed_by": "Resource reviewer",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )
    store = EcoProjectStore(project)
    try:
        policy = store.effective_bundle().resource_policy.model_copy(update={"max_input_bytes": 1})
        store.publish_semantic_change(
            [("resource_policy", "current", policy.model_dump(mode="json"))],
            created_by="Resource reviewer",
            reason="Exercise a non-approval resource blocker.",
        )
    finally:
        store.close()

    stale = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert stale["approval_status"] == "STALE"
    assert any("policy permits" in blocker for blocker in stale["blockers"])
    with pytest.raises(RPCFault) as blocked:
        dispatcher.dispatch(
            "observability.approve_oversize",
            {
                "project_directory": str(project),
                "sync_plan_sha256": stale["sync_plan_sha256"],
                "reviewed_by": "Resource reviewer",
                "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
            },
        )
    assert blocked.value.error.code == "OVERSIZE_SYNC_PLAN_BLOCKED"


def test_oversize_approval_fails_closed_after_policy_or_scope_identity_change(
    tmp_path: Path,
) -> None:
    class ScopeProvider:
        def __init__(self) -> None:
            self.external_project_id = "external-project-1"

        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            return ProjectScopeEvidence(
                entity=request["entity"],
                project=request["project"],
                external_project_id=self.external_project_id,
                deployment=request["deployment"],
                api_base_url=request["api_base_url"],
                scope="RESTRICTED",
                evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
                provider_field="admin.project.scope",
            )

    project = tmp_path / "oversize-stale"
    expected_bytes = _make_oversized_resource_project(project)
    provider = ScopeProvider()
    adapter = _ObservabilityAdapter()
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=adapter,
        scope_resolver=ProjectScopeResolver(provider),
        observability_credentials=_test_wandb_credentials(),
    )
    preview = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": preview["sync_plan_sha256"],
            "reviewed_by": "Dr. Ecologist",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )

    store = EcoProjectStore(project)
    try:
        bundle = store.effective_bundle()
        changed_policy = bundle.resource_policy.model_copy(
            update={"max_current_raster_bytes": max(1, expected_bytes - 2)}
        )
        store.publish_semantic_change(
            [("resource_policy", "current", changed_policy.model_dump(mode="json"))],
            created_by="Resource reviewer",
            reason="Change the reviewed resource ceiling.",
        )
    finally:
        store.close()

    changed = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert changed["sync_plan_sha256"] != preview["sync_plan_sha256"]
    assert changed["approval_status"] == "STALE"
    assert changed["telemetry_artifact_count"] >= preview["telemetry_artifact_count"] + 2
    assert changed["telemetry_projected_bytes"] > preview["telemetry_projected_bytes"]
    with pytest.raises(RPCFault) as stale_plan:
        dispatcher.dispatch(
            "observability.approve_oversize",
            {
                "project_directory": str(project),
                "sync_plan_sha256": preview["sync_plan_sha256"],
                "reviewed_by": "Dr. Ecologist",
                "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
            },
        )
    assert stale_plan.value.error.code == "OVERSIZE_SYNC_APPROVAL_STALE"
    with pytest.raises(RPCFault) as blocked_sync:
        dispatcher.dispatch(
            "observability.sync", {"project_directory": str(project), "online": False}
        )
    assert blocked_sync.value.error.code == "BLOCKED_PROJECT_POLICY"

    # Reapprove the new policy, then prove an authoritative destination identity
    # change produces a different digest and cannot reuse either approval.
    reapproved = dispatcher.dispatch(
        "observability.approve_oversize",
        {
            "project_directory": str(project),
            "sync_plan_sha256": changed["sync_plan_sha256"],
            "reviewed_by": "Dr. Ecologist",
            "acknowledgement": OVERSIZE_SYNC_ACKNOWLEDGEMENT,
        },
    )["data"]
    assert reapproved["upload_preview"]["sync_plan_sha256"] == changed["sync_plan_sha256"]
    assert (
        reapproved["upload_preview"]["telemetry_inventory_sha256"]
        == changed["telemetry_inventory_sha256"]
    )
    store = EcoProjectStore(project)
    try:
        assert len(store.telemetry_rows(event_type="oversize_sync_approval")) == 2
    finally:
        store.close()
    provider.external_project_id = "external-project-2"
    with pytest.raises(RPCFault) as online_scope_blocked:
        dispatcher.dispatch(
            "observability.sync", {"project_directory": str(project), "online": True}
        )
    assert online_scope_blocked.value.error.code == "BLOCKED_PROJECT_POLICY"
    changed_scope = dispatcher.dispatch(
        "observability.preview_upload", {"project_directory": str(project)}
    )["data"]
    assert changed_scope["sync_plan_sha256"] != changed["sync_plan_sha256"]
    assert changed_scope["approval_status"] == "STALE"
    with pytest.raises(RPCFault) as scope_blocked_sync:
        dispatcher.dispatch(
            "observability.sync", {"project_directory": str(project), "online": False}
        )
    assert scope_blocked_sync.value.error.code == "BLOCKED_PROJECT_POLICY"


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
        payloads = _fixture_asset_payloads()
        for asset in fixture.bundle.assets:
            destination = project / asset.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payloads[asset.asset_id])
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
