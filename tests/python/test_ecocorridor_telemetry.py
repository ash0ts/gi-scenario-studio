from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ComputationalResourceObservation,
    ConnectivityAsset,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    SensitiveDataAuthorization,
    SensitivityClass,
    canonical_sha256,
)
from ecocorridor_core.engine import (
    EngineProtocolResult,
    PreparedConnectivityRun,
    PreparedPair,
    PreparedResource,
    ProtocolPairMetric,
    effective_circuitscape_configuration_text,
    engine_request_payload,
)
from ecocorridor_core.export import (
    EvidenceArtifactSource,
    EvidenceProjectAssetSource,
    compute_export_eligibility,
    create_evidence_export,
)
from ecocorridor_core.footprints import (
    create_professional_footprint_proof_document,
    footprint_proof_set_sha256,
)
from ecocorridor_core.interventions import canonicalize_portfolio, portfolio_cost
from ecocorridor_core.metrics import build_pair_metric_claim
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.telemetry import EcoTelemetryProjector
from scenario_platform.cloud_sync import (
    CloudArtifactReference,
    CloudIntegrityError,
    CloudPolicyError,
    CloudUploadReceipt,
    DurableOutboxConsumer,
    TelemetryProjection,
    WandbTarget,
)
from scenario_platform.observability import (
    ProjectScope,
    ProjectScopeEvidence,
    ScopeEvidenceKind,
)
from scenario_platform.storage import ContentAddressedStore


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _engine_manifest() -> EngineManifest:
    return EngineManifest(
        manifest_id="test-telemetry-engine",
        executable_sha256="0" * 64,
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


def _target() -> WandbTarget:
    evidence = ProjectScopeEvidence(
        entity="wildlife-team",
        project="restricted-connectivity",
        external_project_id="restricted-project-id",
        deployment="DEDICATED",
        api_base_url="https://api.wandb.example",
        scope=ProjectScope.RESTRICTED,
        evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
        provider_field="documented_admin.visibility_scope",
    )
    return WandbTarget(
        entity=evidence.entity,
        project=evidence.project,
        external_project_id=evidence.external_project_id,
        deployment=evidence.deployment,
        api_base_url="https://api.wandb.example",
        web_base_url="https://wandb.example",
        scope_evidence=evidence,
    )


def test_projector_accepts_multiple_authorized_sources_for_one_derivative(tmp_path: Path) -> None:
    class RootOnlyStore:
        root = tmp_path.resolve()

    projector = EcoTelemetryProjector(
        store=RootOnlyStore(),  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan={
            "allowed": True,
            "blockers": [],
            "projections": [
                {
                    "source_asset_id": "sensitive-source",
                    "uploaded_asset_id": "generalized-derivative",
                    "representation": "GENERALIZED_DERIVATIVE",
                    "bytes": 100,
                },
                {
                    "source_asset_id": "generalized-derivative",
                    "uploaded_asset_id": "generalized-derivative",
                    "representation": "RAW",
                    "bytes": 100,
                },
            ],
        },
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    assert len(projector.asset_projections["generalized-derivative"]) == 2


def test_projector_rejects_duplicate_source_projection(tmp_path: Path) -> None:
    class RootOnlyStore:
        root = tmp_path.resolve()

    with pytest.raises(CloudPolicyError, match="SOURCE_DUPLICATED"):
        EcoTelemetryProjector(
            store=RootOnlyStore(),  # type: ignore[arg-type]
            project_root=tmp_path,
            sync_plan={
                "allowed": True,
                "blockers": [],
                "projections": [
                    {
                        "source_asset_id": "source",
                        "uploaded_asset_id": "source",
                        "representation": "RAW",
                        "bytes": 1,
                    },
                    {
                        "source_asset_id": "source",
                        "uploaded_asset_id": "other",
                        "representation": "GENERALIZED_DERIVATIVE",
                        "bytes": 1,
                    },
                ],
            },
            profile=ECOCORRIDOR_APP_PROFILE,
        )


def test_cloud_bundle_projection_withholds_generalized_source_metadata(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    original = ConnectivityAsset(
        asset_id="opaque-sensitive-source",
        kind=AssetKind.DOCUMENT,
        title="Hidden Den North exact survey",
        relative_path="private/hidden-den-north.txt",
        sha256="7" * 64,
        byte_size=19,
        media_type="text/plain",
        source="Confidential field notebook",
        vintage="2026-08-03",
        license="LicenseRef-Confidential",
        authorization=SensitiveDataAuthorization(
            asset_id="opaque-sensitive-source",
            ownership="Project team",
            license_spdx_or_name="LicenseRef-Confidential",
            sensitivity=SensitivityClass.SENSITIVE_LOCATION,
            cloud_disposition=CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
            export_allowed=False,
            retention_policy="Local retention only.",
            spatial_redaction="Generalize named den locations.",
            approved_deployments=["DEDICATED"],
            reviewer="Data steward",
            reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
            generalized_asset_id="approved-generalized-document",
        ),
    )
    derivative = original.model_copy(
        update={
            "asset_id": "approved-generalized-document",
            "title": "Approved generalized field summary",
            "relative_path": "cloud/generalized-summary.txt",
            "sha256": "8" * 64,
            "byte_size": 11,
            "source": "Reviewed generalized derivative",
            "license": "CC0-1.0",
            "authorization": original.authorization.model_copy(
                update={
                    "asset_id": "approved-generalized-document",
                    "license_spdx_or_name": "CC0-1.0",
                    "sensitivity": SensitivityClass.PUBLIC,
                    "cloud_disposition": CloudDisposition.RAW_ALLOWED,
                    "export_allowed": True,
                    "generalized_asset_id": None,
                }
            ),
        }
    )
    bundle = fixture.bundle.model_copy(
        update={"assets": [*fixture.bundle.assets, original, derivative]}
    )

    class RootOnlyStore:
        root = tmp_path.resolve()

    projections = [
        {
            "source_asset_id": asset.asset_id,
            "uploaded_asset_id": asset.asset_id,
            "representation": "RAW",
            "bytes": asset.byte_size,
        }
        for asset in fixture.bundle.assets
    ]
    projections.extend(
        [
            {
                "source_asset_id": original.asset_id,
                "uploaded_asset_id": derivative.asset_id,
                "representation": "GENERALIZED_DERIVATIVE",
                "bytes": derivative.byte_size,
            },
            {
                "source_asset_id": derivative.asset_id,
                "uploaded_asset_id": derivative.asset_id,
                "representation": "RAW",
                "bytes": derivative.byte_size,
            },
        ]
    )
    projector = EcoTelemetryProjector(
        store=RootOnlyStore(),  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan={"allowed": True, "blockers": [], "projections": projections},
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    projected = json.dumps(projector._cloud_bundle_payload(bundle), sort_keys=True)
    assert "Hidden Den North" not in projected
    assert "private/hidden-den-north.txt" not in projected
    assert "Confidential field notebook" not in projected
    assert original.asset_id not in projected
    assert derivative.asset_id in projected


class _HashVerifyingUploader:
    def __init__(self) -> None:
        self.projections: list[TelemetryProjection] = []

    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence:
        assert target.scope_evidence.scope == ProjectScope.RESTRICTED
        return target.scope_evidence

    def upload(self, **values: Any) -> CloudUploadReceipt:
        projection: TelemetryProjection = values["projection"]
        project_root: Path = values["project_root"]
        references = []
        for index, artifact in enumerate(projection.artifacts):
            source = (project_root / artifact.source_relative_path).resolve()
            assert project_root.resolve() in source.parents
            assert source.is_file() and not source.is_symlink()
            assert source.stat().st_size == artifact.bytes
            assert hashlib.sha256(source.read_bytes()).hexdigest() == artifact.sha256
            references.append(
                CloudArtifactReference(
                    local_artifact_id=artifact.local_artifact_id,
                    provider_artifact_id=f"provider-{len(self.projections)}-{index}",
                    provider_artifact_ref=(
                        "wildlife-team/restricted-connectivity/"
                        f"artifact-{len(self.projections)}-{index}:v1"
                    ),
                    sha256=artifact.sha256,
                )
            )
        self.projections.append(projection)
        return CloudUploadReceipt(
            run_id=f"run-{len(self.projections)}",
            run_url=f"https://wandb.example/run/{len(self.projections)}",
            envelope_id=values["envelope_id"],
            artifacts=references,
        )


class _GuidedRerunProjectionStore:
    """Small read-only authoritative surface for a focused projector test."""

    def __init__(
        self,
        root: Path,
        *,
        session: dict[str, Any],
        differential: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        self.root = root.resolve()
        self._session = session
        self._differential = differential
        self._events = events

    def project(self) -> dict[str, Any]:
        return {"project_id": "ridgeway-guided-project"}

    def guided(self, session_id: str) -> dict[str, Any]:
        assert session_id == "guided-ridgeway"
        return self._session

    def get_record(self, record_type: str, record_id: str) -> dict[str, Any]:
        assert (record_type, record_id) == ("guided_differential", "analysis-matrix")
        return self._differential

    def analysis(self, analysis_id: str) -> dict[str, Any]:
        assert analysis_id in {"analysis-matrix", "analysis-maps"}
        return {"state": "COMPLETED"}

    def telemetry_rows(
        self,
        *,
        event_type: str | None = None,
        analysis_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self._events
            if (event_type is None or item["event_type"] == event_type)
            and (analysis_id is None or item.get("analysis_id") == analysis_id)
            and (subject_id is None or item.get("subject_id") == subject_id)
        ]


def _seed_authoritative_project(
    project: Path, *, detach_prepared: str | None = None
) -> tuple[EcoProjectStore, dict[str, Any]]:
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
    assets = [item for item in fixture.bundle.assets if item.asset_id in required_asset_ids]
    evidence_rule = fixture.bundle.decision_rules[0].model_copy(
        update={
            "scenario_ids": [scenario.scenario_id],
            "pair_ids": [scenario.included_pair_ids[0]],
            "required": False,
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
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
                        "CAPEX basis: Synthetic reviewed telemetry-test fixture costs.",
                        "O&M assumptions: Included in the retained option cost for this test.",
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
    for asset in assets:
        payload = _json_bytes(fixture.rasters[asset.asset_id].values)
        assert hashlib.sha256(payload).hexdigest() == asset.sha256
        path = project / asset.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    store = EcoProjectStore(project)
    store.initialize(
        project_id=bundle.project_id,
        title=bundle.title,
        mode="PLANNING",
        skeleton={"test": "telemetry"},
    )
    store.store_bundle(bundle)

    baseline = canonicalize_portfolio(
        bundle.intervention_sites,
        bundle.intervention_options,
        {site.site_id: no_action.option_id},
    )
    plan = ConnectivityRunPlan(
        analysis_id="analysis-telemetry",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=7,
        engine_manifest_sha256=_engine_manifest().sha256,
        search_resource_observation=ComputationalResourceObservation(
            observation_id="resource:analysis-telemetry:search",
            boundary=ResourceObservationBoundary.SEARCH_PROPOSER,
            proposer_wall_seconds=0.125,
            peak_memory_status=ResourceObservationStatus.NOT_MEASURED,
            peak_memory_method="NOT_COLLECTED_NO_PER_PROCESS_SAMPLER",
            energy_status=ResourceObservationStatus.NOT_MEASURED,
            energy_method="NOT_COLLECTED_NO_ENERGY_METER",
            hardware="os=test;arch=test;logical_cpus=2",
            boundary_description="One bounded search proposal.",
        ),
    )
    store.create_analysis(plan, bundle=bundle)
    store.transition_analysis(plan.analysis_id, "RUNNING")
    store.schedule_direct_jobs(
        plan.analysis_id,
        [
            {
                "direct_job_id": "direct-telemetry",
                "portfolio_id": baseline.portfolio_id,
                "scenario_id": scenario.scenario_id,
                "sequence_number": 0,
            }
        ],
    )
    assert store.claim_next_direct_job(plan.analysis_id) is not None
    cas = ContentAddressedStore(project / "blobs")
    pair_by_id = {item.pair_id: item for item in focal_set.pairs}
    prepared_pairs = [
        PreparedPair(
            pair_id=pair_id,
            from_focal_id=pair_by_id[pair_id].from_focal_id,
            to_focal_id=pair_by_id[pair_id].to_focal_id,
        )
        for pair_id in scenario.included_pair_ids
    ]
    input_resistance = (
        b"ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n1\n"
    )
    input_focal = b"ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n1\n"
    input_pairs = b"mode\tinclude\n" + b"".join(
        f"{item.from_focal_id}\t{item.to_focal_id}\n".encode() for item in prepared_pairs
    )
    prepared_resistance_sha256 = hashlib.sha256(input_resistance).hexdigest()
    diff_payload = _json_bytes(
        {
            "schema_version": "ecocorridor-semantic-raster-diff/v1.0",
            "project_id": bundle.project_id,
            "revision_id": bundle.revision.revision_id,
            "model_identity_sha256": "a" * 64,
            "portfolio_id": baseline.portfolio_id,
            "scenario_id": scenario.scenario_id,
            "baseline_asset_id": hypothesis.resistance_asset_id,
            "baseline_reviewed_content_sha256": next(
                item.sha256
                for item in bundle.assets
                if item.asset_id == hypothesis.resistance_asset_id
            ),
            "prepared_resistance_sha256": prepared_resistance_sha256,
            "changes": [],
        }
    )
    proof_document = create_professional_footprint_proof_document(
        project_id=bundle.project_id,
        revision_id=bundle.revision.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario_id=scenario.scenario_id,
        baseline=fixture.rasters[hypothesis.resistance_asset_id],
        proofs=[],
        assets=bundle.assets,
        source_paths={},
    )
    proof_payload = (
        json.dumps(proof_document.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    ).encode()
    prepared = PreparedConnectivityRun(
        prepared_run_id="direct-telemetry",
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario=scenario,
        pairs=prepared_pairs,
        resources=[
            PreparedResource(
                logical_name="resistance",
                source_path=project / "prepared-resistance.asc",
                sha256=prepared_resistance_sha256,
                target_filename="resistance.asc",
            ),
            PreparedResource(
                logical_name="focal",
                source_path=project / "prepared-focal.asc",
                sha256=hashlib.sha256(input_focal).hexdigest(),
                target_filename="focal.asc",
            ),
            PreparedResource(
                logical_name="included_pairs",
                source_path=project / "prepared-pairs.tsv",
                sha256=hashlib.sha256(input_pairs).hexdigest(),
                target_filename="included-pairs.tsv",
            ),
        ],
        action_raster_sha256=hashlib.sha256(diff_payload).hexdigest(),
        action_raster_diff_path=project / "action-raster-diff.json",
        prepared_resistance_sha256=prepared_resistance_sha256,
        footprint_proof_sha256=footprint_proof_set_sha256([]),
        engine_manifest_sha256=plan.engine_manifest_sha256,
        model_run_cache_key="b" * 64,
    )
    if detach_prepared == "diff":
        prepared = prepared.model_copy(update={"action_raster_sha256": "f" * 64})
    elif detach_prepared == "proof":
        prepared = prepared.model_copy(update={"footprint_proof_sha256": "f" * 64})
    elif detach_prepared == "input":
        resources = list(prepared.resources)
        resources[0] = resources[0].model_copy(update={"sha256": "f" * 64})
        prepared = prepared.model_copy(update={"resources": resources})
    elif detach_prepared is not None:
        raise AssertionError(f"unknown prepared detachment: {detach_prepared}")
    request_payload = (
        json.dumps(engine_request_payload(prepared), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    artifact_payloads = {
        "request": request_payload,
        "result_protocol": (
            json.dumps(
                EngineProtocolResult(
                    protocol_version="1.0",
                    status="SUCCEEDED",
                    engine_name="Circuitscape",
                    circuitscape_version="5.17.1",
                    pair_metrics=[
                        ProtocolPairMetric(
                            pair_id=item.pair_id,
                            from_focal_id=item.from_focal_id,
                            to_focal_id=item.to_focal_id,
                            effective_resistance=10.0 + index,
                        )
                        for index, item in enumerate(prepared_pairs)
                    ],
                    matrix_path="outputs/connectivity_resistances_3columns.out",
                    current_map_path=None,
                ).model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(),
        "stdout": b"direct Circuitscape run complete\n",
        "stderr": b"",
        "matrix": b"".join(
            f"{item.from_focal_id} {item.to_focal_id} {10 + index}.0\n".encode()
            for index, item in enumerate(prepared_pairs)
        ),
        "circuitscape_configuration": effective_circuitscape_configuration_text(
            resource_paths={
                "resistance": "inputs/resistance.asc",
                "focal": "inputs/focal.asc",
                "included_pairs": "inputs/included-pairs.tsv",
            },
            output_directory="outputs",
            neighbor_rule=scenario.neighbor_rule.value,
            resistance_average=scenario.resistance_average.value,
            emit_current_map=False,
        ).encode(),
        "action_raster_diff": diff_payload,
        "footprint_proof_document": proof_payload,
        "input_resistance": input_resistance,
        "input_focal": input_focal,
        "input_included_pairs": input_pairs,
    }
    artifact_records = []
    for role, value in artifact_payloads.items():
        digest = cas.put_bytes(value)
        artifact_records.append(
            {
                "role": role,
                "sha256": digest,
                "byte_size": len(value),
                "cas_relative_path": cas.path_for(digest).relative_to(project).as_posix(),
                "media_type": "application/json" if role != "matrix" else "text/plain",
            }
        )
    store.set_direct_job_prepared("direct-telemetry", prepared.model_dump(mode="json"), "b" * 64)
    metrics = [
        PairResistanceMetric(
            pair_id=pair_id,
            effective_resistance=10.0 + index,
            baseline_effective_resistance=10.0 + index,
            baseline_relative_change_percent=0.0,
        )
        for index, pair_id in enumerate(scenario.included_pair_ids)
    ]
    by_role = {item["role"]: item for item in artifact_records}
    run = DirectConnectivityRun(
        direct_run_id="direct-telemetry",
        analysis_id=plan.analysis_id,
        revision_id=plan.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario_id=scenario.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=plan.engine_manifest_sha256,
        model_run_cache_key="b" * 64,
        input_sha256=by_role["request"]["sha256"],
        result_sha256=by_role["result_protocol"]["sha256"],
        matrix_sha256=by_role["matrix"]["sha256"],
        pair_metrics=metrics,
        qa_status=QAStatus.ACCEPTED,
        resource_observation=ComputationalResourceObservation(
            observation_id="resource:direct-telemetry",
            boundary=ResourceObservationBoundary.DIRECT_SOLVER,
            solver_wall_seconds=0.75,
            peak_memory_status=ResourceObservationStatus.NOT_MEASURED,
            peak_memory_method="NOT_COLLECTED_NO_PER_PROCESS_SAMPLER",
            energy_status=ResourceObservationStatus.NOT_MEASURED,
            energy_method="NOT_COLLECTED_NO_ENERGY_METER",
            hardware="os=test;arch=test;logical_cpus=2",
            boundary_description="One isolated direct solver process.",
            exclusions=["No carbon value is derived from this observation."],
        ),
        completed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    evidence_nodes = [
        {"node_id": "node-direct_run", "node_type": "DIRECT_RUN", "label": "DIRECT_RUN"},
        {"node_id": "node-scenario", "node_type": "SCENARIO", "label": "SCENARIO"},
        {
            "node_id": "node-raster_diff",
            "node_type": "RASTER_DIFF",
            "label": "RASTER_DIFF",
            "sha256": by_role["action_raster_diff"]["sha256"],
        },
        {"node_id": "node-asset", "node_type": "ASSET", "label": "ASSET"},
        {
            "node_id": "node-engine_manifest",
            "node_type": "ENGINE_MANIFEST",
            "label": "ENGINE_MANIFEST",
        },
        {"node_id": "node-qa", "node_type": "QA", "label": "QA"},
        {"node_id": "node-artifact", "node_type": "ARTIFACT", "label": "ARTIFACT"},
        {
            "node_id": "node-footprint_proof",
            "node_type": "FOOTPRINT_PROOF",
            "label": "FOOTPRINT_PROOF",
            "sha256": by_role["footprint_proof_document"]["sha256"],
        },
    ]
    store.finalize_direct_job(
        run.direct_run_id,
        "SUCCEEDED",
        run=run,
        artifacts=artifact_records,
        evidence_nodes=evidence_nodes,
    )
    rule = evidence_rule
    claim = build_pair_metric_claim(
        claim_id="claim-telemetry",
        run=run,
        baseline_run=run,
        metric=metrics[0],
        rule=rule,
        raster_diff_sha256=by_role["action_raster_diff"]["sha256"],
        source_asset_ids=sorted(required_asset_ids),
        assumption_ids=["reviewed-assumption"],
        authorization_ids=[
            "reviewed-authorization-1",
            "reviewed-authorization-2",
            "reviewed-authorization-3",
        ],
    )
    cost, currency, _ = portfolio_cost(baseline, fixture.bundle.intervention_options)
    summary = PortfolioDecisionSummary(
        portfolio_id=baseline.portfolio_id,
        disposition=PortfolioDisposition.PASSES,
        cost=cost,
        currency=currency,
        rule_dispositions=[],
        controlling_scenario_id=scenario.scenario_id,
        controlling_pair_id=scenario.included_pair_ids[0],
        worst_resistance_reduction_percent=0,
        next_action=(
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        ),
        direct_run_ids=[run.direct_run_id],
        disposition_evidence_claim_id="claim-disposition-telemetry",
    )
    qa_claim = DurableAnalysisExecutor._qa_claim(
        claim_id="claim-qa-telemetry", run=run, rule_id="rule-exact-qa"
    )
    cost_claim = DurableAnalysisExecutor._cost_claim(
        claim_id="claim-cost-telemetry",
        bundle=bundle,
        portfolio=baseline,
        rule_id="rule-cost-telemetry",
        cost=cost,
        currency=currency,
    )
    disposition_claim = DurableAnalysisExecutor._portfolio_disposition_claim(
        claim_id="claim-disposition-telemetry",
        summary=summary,
        runs=[run],
        supporting_claim_ids=[claim.claim_id, qa_claim.claim_id, cost_claim.claim_id],
    )
    export_claims = [claim, qa_claim, cost_claim, disposition_claim]
    store.complete_analysis(plan.analysis_id, summaries=[summary], claims=export_claims)
    store.set_guided("guided-telemetry", "VERIFIED_REPLAY", {"verified_fixture_replay": True})
    store.record_guided_checkpoint(
        session_id="guided-telemetry",
        event_id="checkpoint-telemetry",
        sequence_number=1,
        checkpoint_id="overview",
        event_kind="COMPLETED",
        payload={"duration_ms": 125, "evidence_ids": [claim.claim_id], "portfolio_ids": []},
    )
    store.set_guided("guided-telemetry", "COMPLETED", {"verified_fixture_replay": True})
    eligibility = compute_export_eligibility(
        bundle=bundle,
        current_authorization_bundle=bundle,
        summaries=[summary],
        runs=[run],
        deployment="LOCAL_ONLY",
        sync_state="CONNECTED",
        professional=True,
    )
    export_path = create_evidence_export(
        output_root=project / "exports",
        export_id="export-telemetry",
        baseline_portfolio_id=baseline.portfolio_id,
        bundle=bundle,
        current_authorization_bundle=bundle,
        deployment="LOCAL_ONLY",
        engine_manifest=_engine_manifest(),
        run_plan=plan,
        semantic_diff=None,
        eligibility=eligibility,
        summaries=[summary],
        runs=[run],
        claims=export_claims,
        artifact_sources=[
            EvidenceArtifactSource(
                direct_run_id=run.direct_run_id,
                role=item["role"],
                source_path=project / item["cas_relative_path"],
                sha256=item["sha256"],
                byte_size=item["byte_size"],
                media_type=item["media_type"],
            )
            for item in artifact_records
            if item["role"] not in {"stdout", "stderr"}
        ],
        project_asset_sources=[
            EvidenceProjectAssetSource(
                asset_id=asset.asset_id,
                source_path=project / asset.relative_path,
                sha256=asset.sha256,
                byte_size=asset.byte_size,
                media_type=asset.media_type,
            )
            for asset in assets
        ],
    )
    manifest = json.loads((export_path / "manifest.json").read_text(encoding="utf-8"))
    package_files = [
        {
            "relative_path": path.relative_to(project).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size,
            "media_type": "text/csv" if path.suffix == ".csv" else "application/json",
        }
        for path in sorted(export_path.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    store.store_export(
        "export-telemetry",
        plan.analysis_id,
        manifest,
        package_files=package_files,
    )
    sync_plan = {
        "allowed": True,
        "blockers": [],
        "projections": [
            {
                "source_asset_id": asset.asset_id,
                "uploaded_asset_id": asset.asset_id,
                "representation": "RAW",
                "bytes": asset.byte_size,
            }
            for asset in assets
        ],
    }
    return store, sync_plan


def test_all_authoritative_producers_project_and_sync_idempotently(tmp_path: Path) -> None:
    project = tmp_path / "project"
    store, sync_plan = _seed_authoritative_project(project)
    uploader = _HashVerifyingUploader()
    consumer = DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=project,
        projector=EcoTelemetryProjector(
            store=store,
            project_root=project,
            sync_plan=sync_plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        ),
    )

    first = consumer.drain(online=True, limit=100)
    second = consumer.drain(online=True, limit=100)

    assert (first.claimed, first.synced, first.failed, first.blocked) == (8, 8, 0, 0)
    assert (second.claimed, second.synced) == (0, 0)
    assert [item.event_type for item in uploader.projections] == [
        "bundle_import",
        "analysis_plan",
        "direct_job_terminal",
        "analysis_completion",
        "guided_session",
        "guided_checkpoint",
        "guided_session",
        "evidence_export",
    ]
    analysis_events = [
        item for item in uploader.projections if item.run_kind == "connectivity_analysis"
    ]
    assert [item.event_sequence for item in analysis_events] == [1, 2, 3]
    analysis_plan = next(
        item for item in uploader.projections if item.event_type == "analysis_plan"
    )
    assert analysis_plan.history["proposer_wall_seconds"] == 0.125
    assert any(table.logical_name == "resource_observations" for table in analysis_plan.tables)
    guided_events = [item for item in uploader.projections if item.run_kind == "guided_session"]
    assert [item.event_sequence for item in guided_events] == [1, 2, 3]
    terminal = next(
        item for item in uploader.projections if item.event_type == "direct_job_terminal"
    )
    resource_table = next(
        table for table in terminal.tables if table.logical_name == "resource_observations"
    )
    resource_row = dict(zip(resource_table.columns, resource_table.rows[0], strict=True))
    assert resource_row == {
        "subject_id": "direct-telemetry",
        "observation_id": "resource:direct-telemetry",
        "boundary": "DIRECT_SOLVER",
        "solver_wall_seconds": 0.75,
        "proposer_wall_seconds": None,
        "cache_lookup_wall_seconds": None,
        "peak_memory_status": "NOT_MEASURED",
        "peak_memory_bytes": None,
        "peak_memory_method": "NOT_COLLECTED_NO_PER_PROCESS_SAMPLER",
        "peak_memory_sample_count": 0,
        "peak_memory_sampling_interval_seconds": None,
        "peak_memory_is_lower_bound": True,
        "energy_status": "NOT_MEASURED",
        "energy_kwh": None,
        "energy_method": "NOT_COLLECTED_NO_ENERGY_METER",
        "energy_estimate_watts": None,
        "energy_estimate_lower_watts": None,
        "energy_estimate_upper_watts": None,
        "energy_estimate_lower_kwh": None,
        "energy_estimate_upper_kwh": None,
        "hardware": "os=test;arch=test;logical_cpus=2",
        "boundary_description": "One isolated direct solver process.",
        "assumptions": [],
        "exclusions": ["No carbon value is derived from this observation."],
    }
    completion = next(
        item for item in uploader.projections if item.event_type == "analysis_completion"
    )
    assert completion.summary["measured_solver_wall_seconds"] == 0.75
    assert completion.summary["energy_not_measured_observations"] == 1
    assert "measured_energy_kwh" not in completion.summary
    assert "estimated_energy_kwh" not in completion.summary
    assert all("/Users/" not in item.model_dump_json() for item in uploader.projections)
    assert all(row["state"] == "SYNCED" for row in store.telemetry_rows())
    store.close()


def test_direct_artifact_sync_rejects_same_id_asset_replacement(tmp_path: Path) -> None:
    project = tmp_path / "replaced-analysis-asset"
    store, _ = _seed_authoritative_project(project)
    snapshot = store.analysis_bundle("analysis-telemetry")
    original = snapshot.assets[0]
    replacement = original.model_copy(update={"sha256": "f" * 64})
    publication = store.publish_semantic_change(
        [("asset", original.asset_id, replacement.model_dump(mode="json"))],
        created_by="data-steward@example.test",
        reason="Replace the reviewed source bytes under a new model revision.",
    )
    assert publication["changed"] is True
    current = store.effective_bundle()
    sync_plan = {
        "allowed": True,
        "blockers": [],
        "projections": [
            {
                "source_asset_id": asset.asset_id,
                "uploaded_asset_id": asset.asset_id,
                "representation": "RAW",
                "bytes": asset.byte_size,
            }
            for asset in current.assets
        ],
    }
    projector = EcoTelemetryProjector(
        store=store,
        project_root=project,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    terminal = store.telemetry_rows(event_type="direct_job_terminal")[0]

    with pytest.raises(CloudPolicyError, match="ECO_ANALYSIS_ASSET_IDENTITY_STALE"):
        projector(terminal, terminal["payload"])
    store.close()


def test_direct_artifact_sync_rechecks_current_deployment_authorization(
    tmp_path: Path,
) -> None:
    project = tmp_path / "revoked-analysis-deployment"
    store, _ = _seed_authoritative_project(project)
    current = store.effective_bundle()
    original = current.assets[0]
    revoked = original.model_copy(
        update={
            "authorization": original.authorization.model_copy(
                update={"approved_deployments": ["WANDB_CLOUD"]}
            )
        }
    )
    store.publish_semantic_change(
        [("asset", original.asset_id, revoked.model_dump(mode="json"))],
        created_by="data-steward@example.test",
        reason="Revoke Dedicated deployment authorization.",
    )
    current = store.effective_bundle()
    sync_plan = {
        "destination": "DEDICATED",
        "allowed": True,
        "blockers": [],
        "projections": [
            {
                "source_asset_id": asset.asset_id,
                "uploaded_asset_id": asset.asset_id,
                "representation": "RAW",
                "bytes": asset.byte_size,
            }
            for asset in current.assets
        ],
    }
    projector = EcoTelemetryProjector(
        store=store,
        project_root=project,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    terminal = store.telemetry_rows(event_type="direct_job_terminal")[0]

    with pytest.raises(
        CloudPolicyError,
        match="ECO_ANALYSIS_ASSET_CURRENT_DEPLOYMENT_UNAUTHORIZED",
    ):
        projector(terminal, terminal["payload"])
    store.close()


@pytest.mark.parametrize("detached", ["diff", "proof", "input"])
def test_direct_job_finalization_rejects_detached_prepared_evidence(
    tmp_path: Path, detached: str
) -> None:
    with pytest.raises(ValueError, match="prepared|semantic diff|footprint proof|input artifact"):
        _seed_authoritative_project(tmp_path / detached, detach_prepared=detached)


def test_export_artifact_tampering_is_blocked_after_upstream_lineage_syncs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "tampered-project"
    store, sync_plan = _seed_authoritative_project(project)
    export_file = project / "exports" / "export-telemetry" / "decision-board.json"
    export_file.write_bytes(export_file.read_bytes() + b"tampered")
    uploader = _HashVerifyingUploader()
    consumer = DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=project,
        projector=EcoTelemetryProjector(
            store=store,
            project_root=project,
            sync_plan=sync_plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        ),
    )

    report = consumer.drain(online=True, limit=100)

    assert (report.synced, report.blocked) == (7, 1)
    export_row = store.telemetry_rows(event_type="evidence_export")[0]
    assert export_row["state"] == "BLOCKED"
    assert export_row["last_error_code"] == "ECO_ARTIFACT_SOURCE_HASH_MISMATCH"
    store.close()


def test_semantic_revision_projects_exact_retained_record_and_predecessor(
    tmp_path: Path,
) -> None:
    project = tmp_path / "semantic-project"
    store, sync_plan = _seed_authoritative_project(project)
    brief = store.effective_bundle().decision_brief
    changed = brief.model_copy(
        update={
            "required_next_action": (
                "Review the updated field-reconnaissance scope before another direct analysis."
            )
        }
    )
    publication = store.publish_semantic_change(
        [("decision_brief", brief.brief_id, changed.model_dump(mode="json"))],
        created_by="reviewer@example.test",
        reason="Clarify the required feasibility action.",
    )
    assert publication["changed"] is True

    uploader = _HashVerifyingUploader()
    consumer = DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=project,
        projector=EcoTelemetryProjector(
            store=store,
            project_root=project,
            sync_plan=sync_plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        ),
    )

    report = consumer.drain(online=True, limit=100)

    assert (report.claimed, report.synced, report.blocked) == (9, 9, 0)
    projection = next(
        item for item in uploader.projections if item.event_type == "semantic_revision"
    )
    assert projection.run_kind == "planning_model"
    assert projection.config["revision_id"] == publication["revision"]["revision_id"]
    assert projection.input_artifact_refs
    assert projection.tables[0].rows[0][:3] == ["decision_brief", brief.brief_id, 1]
    assert projection.artifacts[0].artifact_type == "connectivity_project"
    store.close()


def test_model_attestation_projects_ready_validation_and_exact_passport(
    tmp_path: Path,
) -> None:
    project = tmp_path / "attestation-project"
    store, sync_plan = _seed_authoritative_project(project)
    bundle = store.effective_bundle()
    candidate = bundle.fitness.model_copy(
        update={
            "reviewed_by": "reviewer@example.test",
            "reviewed_at": datetime(2026, 8, 3, tzinfo=UTC),
            "attestations": ["Reviewed for the stated screening purpose."],
        }
    )
    candidate_payload = candidate.model_dump(mode="json")
    candidate_sha256 = canonical_sha256(candidate_payload)
    validation = store.record_validation(
        revision=bundle.revision,
        validation={
            "status": "READY",
            "candidate_fitness_sha256": candidate_sha256,
            "findings": [],
        },
    )
    publication = store.publish_fitness_attestation(
        fitness=candidate_payload,
        reviewer="reviewer@example.test",
        attestation="Reviewed for the stated screening purpose.",
        validation_id=validation["validation_id"],
        candidate_fitness_sha256=candidate_sha256,
    )

    uploader = _HashVerifyingUploader()
    consumer = DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=project,
        projector=EcoTelemetryProjector(
            store=store,
            project_root=project,
            sync_plan=sync_plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        ),
    )
    report = consumer.drain(online=True, limit=100)

    assert (report.claimed, report.synced, report.blocked) == (9, 9, 0)
    projection = next(
        item for item in uploader.projections if item.event_type == "model_attestation"
    )
    assert projection.run_kind == "planning_model"
    assert projection.summary["attestation_id"] == publication["attestation_id"]
    assert projection.summary["validation_status"] == "READY"
    assert projection.input_artifact_refs
    assert projection.artifacts[0].artifact_type == "decision_evidence"
    store.close()


def test_unknown_eco_producer_event_fails_closed(tmp_path: Path) -> None:
    project = tmp_path / "unknown-event-project"
    store, _ = _seed_authoritative_project(project)
    project_id = store.project()["project_id"]
    with (
        pytest.raises(sqlite3.IntegrityError, match="unsupported telemetry event type"),
        store.transaction() as connection,
    ):
        store._enqueue_outbox(
            connection,
            project_id=project_id,
            analysis_id=None,
            event_type="future_unreviewed_event",
            subject_id="future-1",
            stream_id="future_unreviewed:future-1",
            payload={"future": True},
            idempotency_key="future-unreviewed-event:1",
        )
    store.close()


def test_guided_rerun_completion_projects_verified_differential_lineage(
    tmp_path: Path,
) -> None:
    projection = {"verified_fixture_replay": True, "fixture_id": "ridgeway_crossing"}
    report = {
        "schema_version": "ridgeway-differential/v1.0",
        "report_sha256": "a" * 64,
        "accepted": True,
        "matrix": {"jobs_compared": 1_536, "outside_tolerance": 0},
        "map_pairs": {"jobs_compared": 60, "outside_tolerance": 0},
        "current_maps": {"jobs_compared": 60, "outside_tolerance": 0},
    }
    cas = ContentAddressedStore(tmp_path / "blobs")
    differential_sha256 = cas.put_json(report)
    differential_relative = cas.path_for(differential_sha256).relative_to(tmp_path).as_posix()
    completion = {
        "analysis_id": "analysis-matrix",
        "map_analysis_id": "analysis-maps",
        "total_jobs": 1_596,
        "matrix_jobs": 1_536,
        "map_jobs": 60,
        "differential_sha256": differential_sha256,
        "differential_cas_relative_path": differential_relative,
        "differential": report,
    }
    row = {
        "outbox_id": "outbox-guided-complete",
        "project_id": "ridgeway-guided-project",
        "analysis_id": "analysis-matrix",
        "event_type": "guided_session",
        "subject_id": "guided-ridgeway",
        "created_at": "2026-08-03T12:00:03+00:00",
    }
    events = [
        {
            "outbox_id": "outbox-guided-open",
            "project_id": "ridgeway-guided-project",
            "analysis_id": None,
            "event_type": "guided_session",
            "subject_id": "guided-ridgeway",
            "created_at": "2026-08-03T12:00:00+00:00",
            "payload": {"session_id": "guided-ridgeway"},
            "state": "SYNCED",
            "external_reference": {"artifacts": []},
        },
        {
            "outbox_id": "outbox-matrix-complete",
            "project_id": "ridgeway-guided-project",
            "analysis_id": "analysis-matrix",
            "event_type": "analysis_completion",
            "subject_id": "analysis-matrix",
            "created_at": "2026-08-03T12:00:01+00:00",
            "payload": {},
            "state": "SYNCED",
            "external_reference": {
                "artifacts": [{"provider_artifact_ref": "team/project/matrix-evidence:v1"}]
            },
        },
        {
            "outbox_id": "outbox-map-complete",
            "project_id": "ridgeway-guided-project",
            "analysis_id": "analysis-maps",
            "event_type": "analysis_completion",
            "subject_id": "analysis-maps",
            "created_at": "2026-08-03T12:00:02+00:00",
            "payload": {},
            "state": "SYNCED",
            "external_reference": {
                "artifacts": [{"provider_artifact_ref": "team/project/map-evidence:v1"}]
            },
        },
        {
            **row,
            "payload": {"session_id": "guided-ridgeway"},
            "state": "PENDING",
            "external_reference": None,
        },
    ]
    store = _GuidedRerunProjectionStore(
        tmp_path,
        session={"state": "COMPLETED", "revision": 2, "projection": projection},
        differential={**report, "artifact_sha256": differential_sha256},
        events=events,
    )
    projector = EcoTelemetryProjector(
        store=store,  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan={"allowed": True, "blockers": [], "projections": []},
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    payload = {
        "session_id": "guided-ridgeway",
        "state": "COMPLETED",
        "revision": 2,
        "projection_sha256": hashlib.sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "projection": projection,
        "rerun_completion": completion,
    }

    projected = projector(row, payload)

    assert projected.event_sequence == 2
    assert projected.summary["differential_accepted"] is True
    assert projected.history["direct_jobs_completed"] == 1_596
    assert projected.input_artifact_refs == [
        "team/project/map-evidence:v1",
        "team/project/matrix-evidence:v1",
    ]
    differential_artifact = next(
        item for item in projected.artifacts if item.logical_name.endswith("differential.json")
    )
    assert differential_artifact.sha256 == differential_sha256
    assert differential_artifact.source_relative_path == differential_relative

    differential_path = cas.path_for(differential_sha256)
    differential_path.chmod(0o600)
    differential_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CloudIntegrityError, match="ARTIFACT_SOURCE_HASH_MISMATCH"):
        projector(row, payload)
