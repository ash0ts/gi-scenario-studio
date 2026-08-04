from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.assistant_runtime import ProviderBackedAssistantRuntime
from ecocorridor_core.assistants import (
    APPROVED_TRACE_STEPS,
    INTAKE_LIMITATION,
    ProviderResponse,
    TraceReadback,
)
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    EngineManifest,
    SensitiveDataAuthorization,
    SensitivityClass,
)
from ecocorridor_core.interventions import canonicalize_portfolio
from ecocorridor_core.metrics import CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.raster_io import render_esri_ascii
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault
from scenario_platform.cloud_sync import InMemoryCredentialSession
from scenario_platform.contracts import canonical_json
from scenario_platform.observability import (
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
)


class _ScopeProvider:
    def __init__(self) -> None:
        self.external_project_id = "wandb-project-immutable-1"

    def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
        return ProjectScopeEvidence(
            entity=request["entity"],
            project=request["project"],
            external_project_id=self.external_project_id,
            deployment=request["deployment"],
            api_base_url=request["api_base_url"],
            scope="RESTRICTED",
            evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
            provider_field="projects.access_scope",
        )


class _TraceSink:
    def __init__(self, *, failure: str | None = None) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.feedback: list[dict[str, str]] = []
        self.failure = failure

    def start(self, *, call_id: str, operation: str, inputs: dict[str, Any]) -> None:
        assert call_id not in self.sessions
        self.sessions[call_id] = {
            "operation": operation,
            "inputs": inputs,
            "steps": [],
            "step_payloads": [],
        }

    def step(
        self,
        *,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        rendered = json.dumps({"inputs": inputs, "output": output})
        assert "provider-secret" not in rendered
        self.sessions[call_id]["steps"].append(name)
        self.sessions[call_id]["step_payloads"].append(
            {"name": name, "inputs": inputs, "output": output}
        )

    def finish(self, *, call_id: str, output: dict[str, Any]) -> None:
        self.sessions[call_id]["finished"] = output

    def flush(self, *, call_id: str) -> str:
        if self.failure == "FLUSH":
            raise RuntimeError("simulated flush failure")
        return f"trace-{call_id}"

    def readback(self, *, trace_id: str) -> TraceReadback:
        if self.failure == "READBACK":
            raise RuntimeError("simulated readback failure")
        call_id = trace_id.removeprefix("trace-")
        return TraceReadback(
            trace_id=trace_id,
            call_id=call_id,
            completed=True,
            steps=self.sessions[call_id]["steps"],
            association=self.sessions[call_id]["inputs"]["wandb_association"],
        )

    def abort(self, *, call_id: str, error_code: str) -> None:
        if "finished" in self.sessions[call_id]:
            return
        self.sessions[call_id]["aborted"] = error_code

    def aborted_readback(self, *, call_id: str) -> dict[str, Any]:
        raise RuntimeError(f"no verified abort trace for {call_id}")

    def record_feedback(
        self,
        *,
        call_id: str,
        trace_id: str,
        rating: str,
        comment: str,
    ) -> dict[str, Any]:
        assert trace_id == f"trace-{call_id}"
        record = {
            "feedback_id": f"feedback-{len(self.feedback) + 1}",
            "trace_id": trace_id,
            "rating": rating,
            "comment": comment,
        }
        self.feedback.append(record)
        return {**record, "verified": True}


class _Provider:
    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self.calls: list[str] = []
        self.requests: list[dict[str, Any]] = []

    def complete(self, **request: Any) -> ProviderResponse:
        with self.credentials.authorization_header() as authorization:
            assert authorization == "Bearer provider-secret-value-123456"
        operation = str(request["operation"])
        self.calls.append(operation)
        self.requests.append(request)
        if operation == "evidence.explain":
            payload = {
                "answer": (
                    "Direct Circuitscape result under documented assumptions. "
                    "The cited within-scenario change is 0%."
                ),
                "evidence_ids": ["E1"],
                "numeric_claims": [{"evidence_id": "E1", "value": 0.0, "units": "percent"}],
                "limitations": [CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
            }
        else:
            page_text = "Screening cost must remain below 200000 USD."
            payload = {
                "draft_fields": [
                    {
                        "field_name": "cost_limit",
                        "value": "Screening cost must remain below 200000 USD.",
                        "citations": [
                            {
                                "document_id": "document-1",
                                "page_number": 4,
                                "start": 0,
                                "end": len(page_text),
                                "quote": page_text,
                            }
                        ],
                    }
                ],
                "limitations": [INTAKE_LIMITATION, CONNECTIVITY_LIMITATION],
            }
        return ProviderResponse(
            content=json.dumps(payload),
            provider_call_id=f"provider-{len(self.calls)}",
            model="explicit-model-v1",
        )


def _engine_manifest(executable_sha256: str) -> EngineManifest:
    return EngineManifest(
        manifest_id="assistant-runtime-test-engine",
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


def _write_fake_engine(path: Path) -> tuple[Path, str]:
    script = """#!/usr/bin/python3
import argparse
import json
import pathlib

parser = argparse.ArgumentParser()
parser.add_argument('--request', required=True)
parser.add_argument('--result', required=True)
args = parser.parse_args()
request = json.loads(pathlib.Path(args.request).read_text(encoding='utf-8'))
outputs = pathlib.Path(request['output_directory'])
outputs.mkdir(parents=True, exist_ok=True)
matrix = outputs / 'resistance-matrix.txt'
matrix.write_text(''.join(
    f"{pair['from_focal_id']} {pair['to_focal_id']} {10.0 + index}\\n"
    for index, pair in enumerate(request['pairs'])
), encoding='utf-8')
settings = request['settings']
resources = request['resources']
current = 'True' if settings['emit_current_map'] else 'False'
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
    f'write_cur_maps = {current}',
    f'write_cum_cur_map_only = {current}',
    'log_transform_maps = False',
    'write_volt_maps = False',
    f"output_file = {request['output_directory'].rstrip('/')}/connectivity",
    '[Options for pairwise and one-to-all and all-to-one modes]',
    f"included_pairs_file = {resources['included_pairs']}",
    'use_included_pairs = True',
    f"point_file = {resources['focal']}",
    '[Connection scheme for raster habitat data]',
    f'connect_four_neighbors_only = {four}',
    f'connect_using_avg_resistances = {arithmetic}',
    '[Habitat raster or graph]',
    'habitat_map_is_resistances = True',
    f"habitat_file = {resources['resistance']}",
]
pathlib.Path('circuitscape.ini').write_text('\\n'.join(configuration) + '\\n', encoding='utf-8')
pathlib.Path(args.result).write_text(json.dumps({
    'protocol_version': '1.0',
    'status': 'SUCCEEDED',
    'engine_name': 'Circuitscape',
    'circuitscape_version': '5.17.1',
    'pair_metrics': [
        {
            'pair_id': pair['pair_id'],
            'from_focal_id': pair['from_focal_id'],
            'to_focal_id': pair['to_focal_id'],
            'effective_resistance': 10.0 + index,
        }
        for index, pair in enumerate(request['pairs'])
    ],
    'matrix_path': matrix.as_posix(),
    'current_map_path': None,
    'review_mask_summaries': [],
    'warnings': [],
    'failure_message': None,
}, sort_keys=True), encoding='utf-8')
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _alias_claim(source: ConnectivityEvidenceClaim, claim_id: str) -> ConnectivityEvidenceClaim:
    payload = source.model_dump(mode="json")
    old_id = source.claim_id
    payload["claim_id"] = claim_id
    payload["root_node_id"] = claim_id
    for node in payload["nodes"]:
        if node["node_id"] == old_id:
            node["node_id"] = claim_id
    for edge in payload["edges"]:
        if edge["from_node_id"] == old_id:
            edge["from_node_id"] = claim_id
        if edge["to_node_id"] == old_id:
            edge["to_node_id"] = claim_id
    return ConnectivityEvidenceClaim.model_validate(payload)


def _seed_project(root: Path, scope: ProjectScopeEvidence) -> None:
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
    site = fixture.bundle.intervention_sites[0]
    no_action = next(
        item
        for item in fixture.bundle.intervention_options
        if item.site_id == site.site_id and item.kind.value == "NO_ACTION"
    )
    required_asset_ids = {
        hypothesis.resistance_asset_id,
        focal_set.asset_id,
        site.footprint_asset_id,
    }
    assets: list[ConnectivityAsset] = []
    for asset in fixture.bundle.assets:
        if asset.asset_id not in required_asset_ids:
            continue
        payload = render_esri_ascii(fixture.rasters[asset.asset_id])
        relative_path = Path("data_assets") / f"{asset.asset_id}.asc"
        retained = root / relative_path
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(payload)
        assets.append(
            asset.model_copy(
                update={
                    "relative_path": relative_path.as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                    "media_type": "application/x-esri-ascii-grid",
                }
            )
        )
    revision = fixture.bundle.revision.model_copy(update={"project_id": "project-1"})
    evidence_rule = fixture.bundle.decision_rules[0].model_copy(
        update={
            "scenario_ids": [scenario.scenario_id],
            "pair_ids": [scenario.included_pair_ids[0]],
            "required": False,
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
            "project_id": "project-1",
            "revision": revision,
            "title": "Assistant boundary test",
            "decision_brief": fixture.bundle.decision_brief.model_copy(
                update={"target_guilds": [scenario.guild_id], "safeguard_guilds": []}
            ),
            "assets": assets,
            "resistance_hypotheses": [hypothesis],
            "focal_area_sets": [focal_set],
            "scenarios": [scenario],
            "intervention_sites": [site.model_copy(update={"option_ids": [no_action.option_id]})],
            "intervention_options": [no_action],
            "safeguards": [],
            "review_masks": [],
            "decision_rules": [evidence_rule],
            "cost_basis": fixture.bundle.cost_basis.model_copy(
                update={
                    "option_costs": {no_action.option_id: no_action.cost},
                    "assumptions": [
                        "Analysis horizon years: 30",
                        "Real discount rate percent: 3.0",
                        "CAPEX basis: Synthetic assistant-runtime fixture costs.",
                        "O&M assumptions: Included in the retained synthetic option cost.",
                        "Replacement assumptions: No replacement is modeled in this test.",
                        (
                            "Reviewer confirmation: I reviewed this synthetic test cost basis "
                            "and its declared assumptions."
                        ),
                    ],
                }
            ),
        }
    )
    bundle = type(fixture.bundle).model_validate(bundle.model_dump(mode="json"))
    baseline = canonicalize_portfolio(
        bundle.intervention_sites,
        bundle.intervention_options,
        {site.site_id: no_action.option_id},
    )
    engine, engine_sha256 = _write_fake_engine(root / "test-engine")
    manifest = _engine_manifest(engine_sha256)
    plan = ConnectivityRunPlan(
        analysis_id="analysis-1",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[],
        engine_manifest_sha256=manifest.sha256,
    )
    store = EcoProjectStore(root)
    try:
        store.initialize(
            project_id="project-1",
            title="Assistant boundary test",
            mode="IMPORTED",
            skeleton={"test": True},
        )
        store.store_bundle(bundle)
        store.create_analysis(plan, bundle=bundle)
    finally:
        store.close()

    executor = DurableAnalysisExecutor(
        manifest,
        lock_root=root / ".test-analysis-locks",
        engine_executable=engine,
        engine_sha256=engine_sha256,
    )
    executor.start(
        project_directory=root,
        bundle=bundle,
        plan=plan,
        engine_executable=engine,
        engine_sha256=engine_sha256,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        store = EcoProjectStore(root)
        try:
            analysis = store.analysis(plan.analysis_id)
        finally:
            store.close()
        if analysis["state"] == "COMPLETED":
            break
        if analysis["state"] == "FAILED":
            raise AssertionError(f"assistant direct-evidence fixture failed: {analysis['error']}")
        time.sleep(0.02)
    else:
        raise AssertionError("assistant direct-evidence fixture did not complete")
    assert executor.shutdown(timeout_seconds=5)

    store = EcoProjectStore(root)
    try:
        _, _, claims = store.analysis_results(plan.analysis_id)
        source_claim = next(
            item
            for item in claims
            if item.claim_kind == "DIRECT_PAIR_METRIC"
            and item.pair_id == scenario.included_pair_ids[0]
        )
        claim = _alias_claim(source_claim, "claim-1")
        assert claim.value == 0.0
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO eco_evidence_claims(claim_id,analysis_id,payload_json) VALUES(?,?,?)",
                (
                    "claim-1",
                    "analysis-1",
                    canonical_json(claim.model_dump(mode="json")),
                ),
            )
        store.set_observability(
            {
                "binding_id": "binding-1",
                "entity": scope.entity,
                "project": scope.project,
                "deployment": scope.deployment,
                "api_base_url": "https://api.wandb.example",
                "web_base_url": "https://wandb.example",
                "external_project_id": scope.external_project_id,
                "scope_evidence": scope.model_dump(mode="json"),
                "profile_namespace": "ecocorridor",
                "state": "CONNECTED",
                "verified": True,
            }
        )
        retained = root / "data_assets" / "documents" / "document-1.txt"
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text(
            "Cover page\fMethods page\fEvidence page\fScreening cost must remain below 200000 USD.",
            encoding="utf-8",
        )
        asset = ConnectivityAsset(
            asset_id="document-1",
            kind=AssetKind.DOCUMENT,
            title="Reviewed screening brief",
            relative_path="data_assets/documents/document-1.txt",
            sha256=hashlib.sha256(retained.read_bytes()).hexdigest(),
            byte_size=retained.stat().st_size,
            media_type="text/plain",
            source="reviewed project brief",
            vintage="2026-08-03",
            license="LicenseRef-Project-Document",
            authorization=SensitiveDataAuthorization(
                asset_id="document-1",
                ownership="Project team",
                license_spdx_or_name="LicenseRef-Project-Document",
                sensitivity=SensitivityClass.INTERNAL,
                cloud_disposition=CloudDisposition.RAW_ALLOWED,
                export_allowed=True,
                retention_policy="Retain with project evidence.",
                spatial_redaction="Coordinates are minimized unless explicitly authorized.",
                approved_deployments=["SELF_MANAGED"],
                reviewer="Data steward",
                reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
            ),
        )
        store.put_record("asset", asset.asset_id, asset.model_dump(mode="json"))
    finally:
        store.close()


def _dispatcher(
    tmp_path: Path, *, trace_failure: str | None = None
) -> tuple[RPCDispatcher, Path, _TraceSink, _Provider]:
    scope_provider = _ScopeProvider()
    scope = scope_provider.resolve_exact_scope(
        entity="eco-team",
        project="restricted-project",
        deployment="SELF_MANAGED",
        api_base_url="https://api.wandb.example",
    )
    project = tmp_path / "project"
    _seed_project(project, scope)
    resolver = ProjectScopeResolver(scope_provider)
    wandb_credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    with wandb_credentials.candidate("wandb-secret-value-123456789"):
        pass
    trace = _TraceSink(failure=trace_failure)
    providers: list[_Provider] = []

    def provider_factory(_configuration: Any, credentials: Any) -> _Provider:
        provider = _Provider(credentials)
        providers.append(provider)
        return provider

    runtime = ProviderBackedAssistantRuntime(
        scope_resolver=resolver,
        observability_credentials=wandb_credentials,
        provider_factory=provider_factory,
        trace_factory=lambda _target, _root: trace,
    )
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        scope_resolver=resolver,
        observability_credentials=wandb_credentials,
        assistant_runtime=runtime,
    )
    configured = dispatcher.dispatch(
        "assistant.configure",
        {
            "project_directory": str(project),
            "configuration": {
                "base_url": "https://provider.example/v1",
                "model": "explicit-model-v1",
            },
            "api_key": "provider-secret-value-123456",
        },
    )
    assert configured["data"]["configured"] is True
    assert "provider-secret" not in json.dumps(configured)
    return dispatcher, project, trace, providers[0]


def test_provider_runtime_explains_and_persists_only_validated_traced_answer(
    tmp_path: Path,
) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    response = dispatcher.dispatch(
        "assistant.explain",
        {
            "project_directory": str(project),
            "call_id": "call-1",
            "question": "What changed in this direct result?",
            "evidence_ids": ["claim-1"],
        },
    )["data"]
    assert response["answer"]["trace_id"] == "trace-call-1"
    association = response["answer"]["association"]
    assert association["analysis_id"] == "analysis-1"
    assert association["wandb_run_kind"] == "connectivity_analysis"
    assert trace.sessions["call-1"]["inputs"]["wandb_association"] == association
    assert trace.sessions["call-1"]["steps"] == list(APPROVED_TRACE_STEPS)
    assert provider.calls == ["evidence.explain"]
    authorization = response["answer"]["evidence_authorization"]
    assert authorization["decision_id"].startswith("evidence_auth_")
    assert (
        trace.sessions["call-1"]["inputs"]["evidence_authorization"]["decision_id"]
        == authorization["decision_id"]
    )
    assert provider.requests[0]["user_prompt"] == "EXPLAIN_BASELINE_CHANGE"
    assert (
        provider.requests[0]["context"]["authorization_decision_id"] == authorization["decision_id"]
    )
    assert provider.requests[0]["context"]["claims"][0]["evidence_alias"] == "E1"
    replicated = json.dumps(
        {
            "provider": provider.requests,
            "trace": trace.sessions["call-1"],
        }
    )
    assert "claim-1" not in replicated
    assert "CORE-A--CORE-B" not in replicated
    assert "Resistance source" not in replicated
    assert b"provider-secret-value-123456" not in (project / "project.sqlite").read_bytes()
    feedback = dispatcher.dispatch(
        "assistant.feedback",
        {
            "project_directory": str(project),
            "call_id": "call-1",
            "rating": "HELPFUL",
            "comment": "The evidence links were useful.",
        },
    )["data"]
    assert feedback["recorded"] is True
    assert feedback["receipt"]["verified"] is True
    assert trace.feedback[0]["trace_id"] == "trace-call-1"

    readback = dispatcher.dispatch(
        "assistant.get_trace",
        {"project_directory": str(project), "call_id": "call-1"},
    )["data"]
    assert readback["association"] == association


def test_provider_completed_answer_is_locally_audited_when_weave_flush_fails(
    tmp_path: Path,
) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path, trace_failure="FLUSH")
    request = {
        "project_directory": str(project),
        "call_id": "call-flush-rejected",
        "question": "What changed in this direct result?",
        "evidence_ids": ["claim-1"],
    }

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch("assistant.explain", request)
    assert error.value.error.code == "ASSISTANT_TRACE_FLUSH_FAILED"
    assert provider.calls == ["evidence.explain"]

    store = EcoProjectStore(project)
    try:
        record = store.assistant_call_record("call-flush-rejected")
        assert record is not None and record["disposition"] == "REJECTED"
        audit = record["audit"]
        assert audit["local_audit_verified"] is True
        assert audit["provider_completed"] is True
        assert audit["completed_trace_readback_verified"] is False
        assert audit["answer_returned"] is False
        assert audit["error_code"] == "ASSISTANT_TRACE_FLUSH_FAILED"
        assert audit["provider_call_id_sha256"] == hashlib.sha256(b"provider-1").hexdigest()
        assert audit["provider_abort_trace"] is None
        with pytest.raises(KeyError):
            store.assistant_answer("call-flush-rejected")
    finally:
        store.close()

    trace_record = dispatcher.dispatch(
        "assistant.get_trace",
        {"project_directory": str(project), "call_id": "call-flush-rejected"},
    )["data"]
    assert trace_record["disposition"] == "REJECTED"
    assert trace_record["local_rejection_audit"]["local_audit_verified"] is True
    assert trace_record["trace_id"] is None

    with pytest.raises(RPCFault) as repeated:
        dispatcher.dispatch("assistant.explain", request)
    assert repeated.value.error.code == "ASSISTANT_CALL_PREVIOUSLY_REJECTED"
    assert provider.calls == ["evidence.explain"]

    reopened = EcoProjectStore(project)
    try:
        assert (
            reopened.assistant_rejection_audit("call-flush-rejected")["local_audit_verified"]
            is True
        )
    finally:
        reopened.close()


def test_provider_completed_intake_is_locally_audited_when_weave_readback_fails(
    tmp_path: Path,
) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path, trace_failure="READBACK")
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-readback-rejected",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "Screening cost must remain below 200000 USD.",
                    }
                ],
            },
        )
    assert error.value.error.code == "ASSISTANT_TRACE_READBACK_INVALID"
    assert provider.calls == ["evidence_intake.extract"]

    store = EcoProjectStore(project)
    try:
        audit = store.assistant_rejection_audit("intake-readback-rejected")
        assert audit["operation"] == "evidence_intake.extract"
        assert audit["local_audit_verified"] is True
        assert audit["provider_completed"] is True
        assert audit["completed_trace_readback_verified"] is False
        assert audit["answer_returned"] is False
    finally:
        store.close()


def test_feedback_rejects_sensitive_content_before_trace_or_sqlite(tmp_path: Path) -> None:
    dispatcher, project, trace, _ = _dispatcher(tmp_path)
    dispatcher.dispatch(
        "assistant.explain",
        {
            "project_directory": str(project),
            "call_id": "call-sensitive-feedback",
            "question": "What changed in this direct result?",
            "evidence_ids": ["claim-1"],
        },
    )
    secret = "feedback-secret-must-not-persist"
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.feedback",
            {
                "project_directory": str(project),
                "call_id": "call-sensitive-feedback",
                "rating": "HELPFUL",
                "comment": f"api_key={secret}",
            },
        )
    assert error.value.error.code == "ASSISTANT_FEEDBACK_SENSITIVE_CONTENT"
    assert trace.feedback == []
    assert secret.encode() not in (project / "project.sqlite").read_bytes()


def test_evidence_question_and_project_authored_names_never_cross_ai_boundary(
    tmp_path: Path,
) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    response = dispatcher.dispatch(
        "assistant.explain",
        {
            "project_directory": str(project),
            "call_id": "call-minimized-context",
            "question": "Explain the result near Bluebird Mesa and Hidden Den North.",
            "evidence_ids": ["claim-1"],
        },
    )["data"]

    replicated = json.dumps(
        {
            "provider": provider.requests,
            "trace": trace.sessions["call-minimized-context"],
        }
    )
    assert "Bluebird Mesa" not in replicated
    assert "Hidden Den North" not in replicated
    assert "claim-1" not in replicated
    assert "portfolio-1" not in replicated
    assert "scenario-1" not in replicated
    assert provider.requests[0]["user_prompt"] == "EXPLAIN_SELECTED_EVIDENCE"
    assert response["answer"]["evidence_ids"] == ["claim-1"]


def _analysis_source_asset(project: Path) -> ConnectivityAsset:
    store = EcoProjectStore(project)
    try:
        claim = store.evidence_claim("claim-1")
        source_id = next(
            node.node_id.removeprefix("asset:") for node in claim.nodes if node.node_type == "ASSET"
        )
        return next(
            item
            for item in store.analysis_bundle("analysis-1").assets
            if item.asset_id == source_id
        )
    finally:
        store.close()


def test_revoked_evidence_authorization_blocks_before_trace_or_provider(tmp_path: Path) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    asset = _analysis_source_asset(project)
    revoked = SensitiveDataAuthorization.model_validate(
        {
            **asset.authorization.model_dump(mode="json"),
            "cloud_disposition": "PROHIBITED",
            "approved_deployments": ["LOCAL_ONLY"],
        }
    )
    store = EcoProjectStore(project)
    try:
        changed = ConnectivityAsset.model_validate(
            {**asset.model_dump(mode="json"), "authorization": revoked.model_dump(mode="json")}
        )
        store.put_record("asset", asset.asset_id, changed.model_dump(mode="json"))
    finally:
        store.close()

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "call-revoked",
                "question": "Explain the selected evidence.",
                "evidence_ids": ["claim-1"],
            },
        )

    assert error.value.error.code == "ASSISTANT_EVIDENCE_DEPLOYMENT_UNAUTHORIZED"
    assert provider.calls == []
    assert "call-revoked" not in trace.sessions


def test_stale_analysis_asset_blocks_before_trace_or_provider(tmp_path: Path) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    asset = _analysis_source_asset(project)
    store = EcoProjectStore(project)
    try:
        changed = ConnectivityAsset.model_validate(
            {**asset.model_dump(mode="json"), "sha256": "f" * 64}
        )
        store.put_record("asset", asset.asset_id, changed.model_dump(mode="json"))
    finally:
        store.close()

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "call-stale",
                "question": "Explain the selected evidence.",
                "evidence_ids": ["claim-1"],
            },
        )

    assert error.value.error.code == "ASSISTANT_EVIDENCE_ASSET_STALE"
    assert provider.calls == []
    assert "call-stale" not in trace.sessions


def test_generalized_only_scientific_source_requires_semantic_revision(tmp_path: Path) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    asset = _analysis_source_asset(project)
    derivative_id = f"{asset.asset_id}-generalized"
    derivative_authorization = SensitiveDataAuthorization.model_validate(
        {
            **asset.authorization.model_dump(mode="json"),
            "asset_id": derivative_id,
            "cloud_disposition": "RAW_ALLOWED",
            "generalized_asset_id": None,
            "approved_deployments": ["SELF_MANAGED"],
        }
    )
    source_authorization = SensitiveDataAuthorization.model_validate(
        {
            **asset.authorization.model_dump(mode="json"),
            "cloud_disposition": "GENERALIZED_DERIVATIVE_ONLY",
            "generalized_asset_id": derivative_id,
            "approved_deployments": ["SELF_MANAGED"],
        }
    )
    derivative_path = f"inputs/generalized/{derivative_id}.json"
    derivative = ConnectivityAsset.model_validate(
        {
            **asset.model_dump(mode="json"),
            "asset_id": derivative_id,
            "title": "Approved generalized derivative",
            "relative_path": derivative_path,
            "authorization": derivative_authorization.model_dump(mode="json"),
        }
    )
    changed_source = ConnectivityAsset.model_validate(
        {
            **asset.model_dump(mode="json"),
            "authorization": source_authorization.model_dump(mode="json"),
        }
    )
    retained = project / derivative_path
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes((project / asset.relative_path).read_bytes())
    store = EcoProjectStore(project)
    try:
        store.put_record("asset", derivative.asset_id, derivative.model_dump(mode="json"))
        store.put_record("asset", asset.asset_id, changed_source.model_dump(mode="json"))
    finally:
        store.close()

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "call-scientific-generalized",
                "question": "Explain the selected evidence.",
                "evidence_ids": ["claim-1"],
            },
        )

    assert error.value.error.code == "ASSISTANT_EVIDENCE_SCIENTIFIC_DERIVATIVE_REQUIRES_REVISION"
    assert provider.calls == []
    assert "call-scientific-generalized" not in trace.sessions


def test_intake_requires_page_spans_and_human_confirmation_before_model_use(
    tmp_path: Path,
) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    page_text = "Screening cost must remain below 200000 USD."
    extracted = dispatcher.dispatch(
        "assistant.intake.extract",
        {
            "project_directory": str(project),
            "call_id": "intake-call-1",
            "instruction": "Extract the stated screening cost constraint.",
            "pages": [{"document_id": "document-1", "page_number": 4, "text": page_text}],
        },
    )["data"]
    assert extracted["ready_for_model"] is False
    assert extracted["human_confirmation_required"] is True
    draft = extracted["draft"]
    assert draft["fields"][0]["confirmed"] is False
    confirmed = dispatcher.dispatch(
        "assistant.intake.confirm",
        {
            "project_directory": str(project),
            "draft_id": draft["draft_id"],
            "field_ids": [draft["fields"][0]["field_id"]],
            "reviewer": "Qualified reviewer",
        },
    )["data"]
    assert confirmed["ready_for_model"] is True
    assert confirmed["applied_to_model"] is False
    assert confirmed["confirmed_fields"][0]["confirmed_by"] == "Qualified reviewer"
    assert trace.sessions["intake-call-1"]["steps"] == list(APPROVED_TRACE_STEPS)
    assert provider.calls == ["evidence_intake.extract"]


def test_intake_rejects_changed_retained_document_before_provider_call(tmp_path: Path) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)
    retained = project / "data_assets" / "documents" / "document-1.txt"
    retained.write_text("tampered after review", encoding="utf-8")

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-tampered",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 1,
                        "text": "tampered after review",
                    }
                ],
            },
        )

    assert error.value.error.code == "ASSISTANT_DOCUMENT_INTEGRITY_FAILED"
    assert provider.calls == []


def test_intake_rejects_renderer_text_not_on_retained_page(tmp_path: Path) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-fabricated-page",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "A fabricated constraint not retained in the document.",
                    }
                ],
            },
        )

    assert error.value.error.code == "ASSISTANT_DOCUMENT_PAGE_EVIDENCE_MISMATCH"
    assert provider.calls == []


def test_intake_rejects_page_excerpt_with_noncanonical_span_offsets(tmp_path: Path) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-page-excerpt",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "cost must remain below 200000 USD",
                    }
                ],
            },
        )

    assert error.value.error.code == "ASSISTANT_DOCUMENT_PAGE_EVIDENCE_MISMATCH"
    assert provider.calls == []


def test_intake_rejects_raw_text_when_only_generalized_derivative_is_authorized(
    tmp_path: Path,
) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)
    store = EcoProjectStore(project)
    try:
        payload = store.get_record("asset", "document-1")
        payload["authorization"]["cloud_disposition"] = "GENERALIZED_DERIVATIVE_ONLY"
        payload["authorization"]["generalized_asset_id"] = "document-1-generalized"
        store.put_record("asset", "document-1", payload)
    finally:
        store.close()

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-generalized-only",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "Screening cost must remain below 200000 USD.",
                    }
                ],
            },
        )

    assert error.value.error.code == "ASSISTANT_DOCUMENT_RAW_REPRESENTATION_BLOCKED"
    assert provider.calls == []


def test_intake_rejects_renderer_exact_location_self_authorization(tmp_path: Path) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-exact-self-auth",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "Screening cost must remain below 200000 USD.",
                    }
                ],
                "sensitive_policy": {"exact_location_authorized": True},
            },
        )
    assert error.value.error.code == "ASSISTANT_EXACT_LOCATION_AUTHORIZATION_UNAVAILABLE"
    assert provider.calls == []


def test_intake_rejects_raw_sensitive_document_without_separate_call_authorization(
    tmp_path: Path,
) -> None:
    dispatcher, project, _, provider = _dispatcher(tmp_path)
    store = EcoProjectStore(project)
    try:
        payload = store.get_record("asset", "document-1")
        payload["authorization"]["sensitivity"] = "SENSITIVE_LOCATION"
        store.put_record("asset", "document-1", payload)
    finally:
        store.close()

    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.intake.extract",
            {
                "project_directory": str(project),
                "call_id": "intake-raw-sensitive",
                "instruction": "Extract the stated screening cost constraint.",
                "pages": [
                    {
                        "document_id": "document-1",
                        "page_number": 4,
                        "text": "Screening cost must remain below 200000 USD.",
                    }
                ],
            },
        )
    assert error.value.error.code == "ASSISTANT_RAW_SENSITIVE_DOCUMENT_REQUIRES_GENERALIZATION"
    assert provider.calls == []


def test_assistant_rechecks_exact_scope_and_clear_zeroes_runtime_session(tmp_path: Path) -> None:
    dispatcher, project, _, _ = _dispatcher(tmp_path)
    store = EcoProjectStore(project)
    try:
        binding = store.observability()
        assert binding is not None
        binding["state"] = "SYNC_QUEUED_OFFLINE"
        store.set_observability(binding)
    finally:
        store.close()
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "blocked-call",
                "question": "Explain this evidence.",
                "evidence_ids": ["claim-1"],
            },
        )
    assert error.value.error.code == "ASSISTANT_VERIFIED_ONLINE_BINDING_REQUIRED"
    cleared = dispatcher.dispatch("assistant.clear", {"project_directory": str(project)})["data"]
    assert cleared["configured"] is False
    assert cleared["cleared"] is True
    assert cleared["provider"] is None
    status = dispatcher.dispatch("assistant.status", {"project_directory": str(project)})["data"]
    assert status["configured"] is False
    assert status["provider"] is None


def test_assistant_rejects_changed_exact_scope_evidence_before_provider_call(
    tmp_path: Path,
) -> None:
    dispatcher, project, trace, provider = _dispatcher(tmp_path)
    scope_provider = dispatcher.scope_resolver.provider
    assert isinstance(scope_provider, _ScopeProvider)
    scope_provider.external_project_id = "wandb-project-different"
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.explain",
            {
                "project_directory": str(project),
                "call_id": "changed-scope-call",
                "question": "Explain this evidence.",
                "evidence_ids": ["claim-1"],
            },
        )
    assert error.value.error.code == "ASSISTANT_PROJECT_AUTHORIZATION_FAILED"
    assert provider.calls == []
    assert trace.sessions["changed-scope-call"]["aborted"] == (
        "ASSISTANT_PROJECT_AUTHORIZATION_FAILED"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "https://user:secret@provider.example/v1",
        "https://provider.example/v1/chat/completions",
    ],
)
def test_provider_configuration_rejects_unsafe_or_ambiguous_endpoints(
    tmp_path: Path, base_url: str
) -> None:
    dispatcher, project, _, _ = _dispatcher(tmp_path)
    with pytest.raises(RPCFault) as error:
        dispatcher.dispatch(
            "assistant.configure",
            {
                "project_directory": str(project),
                "configuration": {"base_url": base_url, "model": "explicit-model-v1"},
                "api_key": "provider-secret-value-123456",
            },
        )
    assert error.value.error.code in {"RPC_CONTRACT_REJECTED", "REQUEST_VALIDATION_FAILED"}
