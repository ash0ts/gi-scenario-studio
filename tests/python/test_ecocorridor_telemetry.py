from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import (
    OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION,
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
    ConnectivityExportManifest,
    EvidenceArtifactSource,
    EvidenceProjectAssetSource,
    ExcludedArtifactPolicy,
    ReferenceEvidenceEntryManifest,
    ReferenceEvidenceSectionManifest,
    compute_export_eligibility,
    create_evidence_export,
)
from ecocorridor_core.footprints import (
    create_professional_footprint_proof_document,
    footprint_proof_set_sha256,
)
from ecocorridor_core.governance import (
    SyncDestination,
    create_oversize_sync_approval,
    plan_cloud_sync,
)
from ecocorridor_core.interventions import canonicalize_portfolio, portfolio_cost
from ecocorridor_core.metrics import build_pair_metric_claim
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.telemetry import (
    EcoTelemetryProjector,
    build_telemetry_projection_inventory,
    validate_projection_operational_bound,
)
from scenario_platform.cloud_sync import (
    CloudArtifactReference,
    CloudIntegrityError,
    CloudPolicyError,
    CloudTransportError,
    CloudUploadReceipt,
    DurableOutboxConsumer,
    TableProjection,
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


def _bounded_projection(**updates: Any) -> TelemetryProjection:
    values: dict[str, Any] = {
        "profile_namespace": "ecocorridor",
        "project_id": "project-bound-test",
        "run_kind": "release_gate",
        "run_subject_id": "subject-bound-test",
        "event_type": "release_gate",
        "event_sequence": 1,
    }
    values.update(updates)
    return TelemetryProjection.model_validate(values)


def test_operational_metadata_reservation_rejects_unbounded_refs_and_failures() -> None:
    # Provider references and retry diagnostics are uploaded but normalized out
    # of evidence identity. Their actual bytes must remain inside the fixed
    # per-envelope reserve rather than becoming uncounted upload data.
    reference_count = OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION // 80 + 100
    references = [
        f"entity/project/artifact-{index:05d}-{'x' * 60}:v1" for index in range(reference_count)
    ]
    with pytest.raises(
        CloudIntegrityError, match="ECO_TELEMETRY_OPERATIONAL_METADATA_BOUND_EXCEEDED"
    ):
        validate_projection_operational_bound(_bounded_projection(input_artifact_refs=references))

    failure_rows = [
        [f"failure-{index:05d}-{'y' * 100}"]
        for index in range(OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION // 100 + 100)
    ]
    with pytest.raises(
        CloudIntegrityError, match="ECO_TELEMETRY_OPERATIONAL_METADATA_BOUND_EXCEEDED"
    ):
        validate_projection_operational_bound(
            _bounded_projection(
                tables=[
                    TableProjection(
                        logical_name="sync_failures",
                        columns=["diagnostic"],
                        rows=failure_rows,
                    )
                ]
            )
        )


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


def test_cloud_artifact_projection_rejects_arbitrary_absolute_path_text(
    tmp_path: Path,
) -> None:
    class RootOnlyStore:
        root = tmp_path.resolve()

    leaked = tmp_path / "evidence.json"
    leaked.write_text('{"source":"/Volumes/ecology/private/model.tif"}\n', encoding="utf-8")
    digest = hashlib.sha256(leaked.read_bytes()).hexdigest()
    projector = EcoTelemetryProjector(
        store=RootOnlyStore(),  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan={"allowed": True, "blockers": [], "projections": []},
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    with pytest.raises(CloudIntegrityError, match="ECO_ARTIFACT_LOCAL_PATH_DISCLOSURE"):
        projector._verified_artifact(
            local_artifact_id="leaked-artifact",
            artifact_type="decision_evidence",
            logical_id="leaked-evidence",
            logical_name="evidence/leaked.json",
            source_relative_path="evidence.json",
            expected_sha256=digest,
            expected_bytes=leaked.stat().st_size,
            media_type="application/json",
        )


def test_reference_backed_export_waits_for_reference_lineage_and_counts_it(
    tmp_path: Path,
) -> None:
    bundle = build_ridgeway_fixture().bundle
    export_id = "export-reference-lineage"
    package_path = tmp_path / "exports" / export_id / "payload.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text('{"kind":"direct evidence"}\n', encoding="utf-8")
    package_sha256 = hashlib.sha256(package_path.read_bytes()).hexdigest()
    hash_a = "a" * 64
    hash_b = "b" * 64
    reference_entry = ReferenceEvidenceEntryManifest(
        analysis_id="analysis-reference",
        plan_id="reference-plan",
        result_id="reference-result",
        plan_file="reference-evidence/results/reference-result/comparison-plan.json",
        plan_sha256=hash_a,
        plan_file_sha256=hash_a,
        result_file="reference-evidence/results/reference-result/comparison-result.json",
        result_sha256=hash_b,
        result_file_sha256=hash_b,
        claims_file="reference-evidence/results/reference-result/evidence-claims.json",
        claims_sha256=hash_a,
        direct_runs_file="reference-evidence/results/reference-result/direct-runs.json",
        direct_runs_sha256=hash_b,
        binding_ids=["reference-binding"],
        scenario_ids=["reference-scenario-1", "reference-scenario-2"],
        comparison_ids=["comparison-1", "comparison-2"],
        direct_run_ids=["reference-run-1", "reference-run-2"],
        evidence_claim_ids=["reference-claim-1", "reference-claim-2"],
        reference_asset_ids=["reference-matrix"],
        scientific_fingerprints={"reference-binding": hash_a},
        artifact_files={"reference-run-1": {"matrix": "reference/matrix.tsv"}},
        engine_manifest_sha256=hash_b,
        reference_matrix_parser_sha256=hash_a,
    )
    reference_section = ReferenceEvidenceSectionManifest(
        excluded_artifacts=[
            ExcludedArtifactPolicy(
                role="stdout",
                reason="Console output is not scientific evidence.",
            )
        ],
        reference_assets_file="reference-evidence/assets-and-authorizations.json",
        reference_assets_sha256=hash_a,
        reference_asset_files={"reference-matrix": "source-assets/reference-matrix/asset.tsv"},
        authorization_sha256_by_asset={"reference-matrix": hash_b},
        engine_manifest_sha256=hash_b,
        engine_manifest_file_sha256=hash_b,
        reference_matrix_parser_file_sha256=hash_a,
        reference_matrix_parser_sha256=hash_a,
        entries=[reference_entry],
    )
    manifest = ConnectivityExportManifest(
        schema_version="ecocorridor-export/v1.1",
        export_id=export_id,
        project_id=bundle.project_id,
        revision_id=bundle.revision.revision_id,
        baseline_portfolio_id="portfolio-baseline",
        model_snapshot_sha256=hash_a,
        engine_manifest_sha256=hash_b,
        run_plan_sha256=hash_a,
        cost_basis_sha256=hash_b,
        conceptual=True,
        notice="Conceptual connectivity screening.",
        files={"payload.json": package_sha256},
        direct_run_ids=["ordinary-run"],
        evidence_claim_ids=["ordinary-claim"],
        sync_state="CONNECTED",
        reference_evidence=reference_section,
    )
    rows = [
        {
            "outbox_id": "outbox-analysis-complete",
            "event_type": "analysis_completion",
            "analysis_id": "analysis-main",
            "subject_id": "analysis-main",
            "state": "SYNCED",
            "external_reference": {
                "artifacts": [{"provider_artifact_ref": "team/project/analysis:v1"}]
            },
        },
        {
            "outbox_id": "outbox-reference",
            "event_type": "reference_comparison",
            "analysis_id": "analysis-reference",
            "subject_id": "reference-result",
            "state": "PENDING",
            "external_reference": None,
        },
    ]

    class ReferenceExportStore:
        root = tmp_path.resolve()

        @staticmethod
        def analysis_bundle(analysis_id: str):  # type: ignore[no-untyped-def]
            assert analysis_id == "analysis-main"
            return bundle

        @staticmethod
        def effective_bundle():  # type: ignore[no-untyped-def]
            return bundle

        @staticmethod
        def telemetry_rows(
            *,
            event_type: str | None = None,
            analysis_id: str | None = None,
            subject_id: str | None = None,
        ) -> list[dict[str, Any]]:
            return [
                item
                for item in rows
                if (event_type is None or item["event_type"] == event_type)
                and (analysis_id is None or item["analysis_id"] == analysis_id)
                and (subject_id is None or item["subject_id"] == subject_id)
            ]

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
            for asset in bundle.assets
        ],
    }
    projector = EcoTelemetryProjector(
        store=ReferenceExportStore(),  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    row = {
        "outbox_id": "outbox-export",
        "project_id": bundle.project_id,
        "analysis_id": "analysis-main",
        "event_type": "evidence_export",
        "subject_id": export_id,
        "created_at": "2026-08-03T12:00:00+00:00",
    }
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "package_files": [
            {
                "relative_path": package_path.relative_to(tmp_path).as_posix(),
                "sha256": package_sha256,
                "byte_size": package_path.stat().st_size,
                "media_type": "application/json",
            }
        ],
    }
    with pytest.raises(CloudTransportError, match="ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED"):
        projector._evidence_export(row, payload)

    rows[1]["state"] = "SYNCED"
    rows[1]["external_reference"] = {
        "artifacts": [{"provider_artifact_ref": "team/project/reference:v1"}]
    }
    projection = projector._evidence_export(row, payload)
    assert projection.input_artifact_refs == [
        "team/project/analysis:v1",
        "team/project/reference:v1",
    ]
    assert projection.summary["ordinary_direct_run_count"] == 1
    assert projection.summary["reference_direct_run_count"] == 2
    assert projection.summary["direct_run_count"] == 3
    assert projection.summary["ordinary_evidence_claim_count"] == 1
    assert projection.summary["reference_evidence_claim_count"] == 2
    assert projection.summary["evidence_claim_count"] == 3
    dependency_table = next(
        item for item in projection.tables if item.logical_name == "export_reference_dependencies"
    )
    assert dependency_table.rows[0][0:6] == [
        "analysis-reference",
        "reference-plan",
        "reference-result",
        2,
        2,
        2,
    ]


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
    required_target_rule = evidence_rule.model_copy(
        update={
            "rule_id": "rule-telemetry-required-target",
            "title": "Required telemetry target coverage",
            "pair_ids": list(scenario.included_pair_ids),
            "required": True,
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
            # Keep the claim-consumer rule optional for this fixture's
            # cardinality assertions while satisfying the professional export
            # invariant that every target scenario/pair has required coverage.
            "decision_rules": [evidence_rule, required_target_rule],
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
        {
            "node_id": f"run:{run.direct_run_id}",
            "node_type": "DIRECT_RUN",
            "sha256": run.result_sha256,
        },
        {"node_id": f"scenario:{run.scenario_id}", "node_type": "SCENARIO"},
        {
            "node_id": f"diff:{prepared.action_raster_sha256}",
            "node_type": "RASTER_DIFF",
            "sha256": prepared.action_raster_sha256,
            "footprint_proof_sha256": prepared.footprint_proof_sha256,
        },
        {
            "node_id": f"asset:{hypothesis.resistance_asset_id}",
            "node_type": "ASSET",
            "sha256": next(
                item.sha256
                for item in bundle.assets
                if item.asset_id == hypothesis.resistance_asset_id
            ),
        },
        {
            "node_id": f"engine:{run.engine_manifest_sha256}",
            "node_type": "ENGINE_MANIFEST",
            "sha256": run.engine_manifest_sha256,
        },
        {
            "node_id": f"qa:{run.direct_run_id}",
            "node_type": "QA",
            "status": run.qa_status.value,
        },
        {
            "node_id": f"footprint-proof:{by_role['footprint_proof_document']['sha256']}",
            "node_type": "FOOTPRINT_PROOF",
            "sha256": by_role["footprint_proof_document"]["sha256"],
        },
        *[
            {
                "node_id": f"artifact:{item['role']}:{item['sha256']}",
                "node_type": "ARTIFACT",
                "role": item["role"],
                "sha256": item["sha256"],
            }
            for item in artifact_records
        ],
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


def _prepare_minimal_export_intent(
    store: EcoProjectStore,
    *,
    export_id: str,
    analysis_id: str,
) -> tuple[Path, Path]:
    staging = store.root / "exports" / f".{export_id}.staging"
    final = store.root / "exports" / export_id
    staging.mkdir(parents=True)
    evidence = staging / "evidence.json"
    evidence.write_text('{"evidence":"direct"}\n', encoding="utf-8")
    evidence_sha256 = hashlib.sha256(evidence.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "ecocorridor-export/test",
        "export_id": export_id,
        "project_id": store.project()["project_id"],
        "files": {"evidence.json": evidence_sha256},
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    package_files = [
        {
            "relative_path": f"exports/{export_id}/{path.name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size,
            "media_type": "application/json",
        }
        for path in (evidence, manifest_path)
    ]
    store.prepare_export_publication(
        export_id,
        analysis_id,
        manifest,
        package_files=package_files,
        staging_relative_path=staging.relative_to(store.root).as_posix(),
    )
    return staging, final


def test_export_publication_recovers_before_and_after_atomic_rename(tmp_path: Path) -> None:
    store, _ = _seed_authoritative_project(tmp_path / "recoverable-export")
    analysis_id = store.telemetry_rows(event_type="analysis_completion")[0]["analysis_id"]
    try:
        staging_before, final_before = _prepare_minimal_export_intent(
            store,
            export_id="export-before-rename",
            analysis_id=analysis_id,
        )
        assert staging_before.is_dir()
        assert store.recover_export_publications() == ["export-before-rename"]
        assert final_before.is_dir()
        assert not staging_before.exists()

        staging_after, final_after = _prepare_minimal_export_intent(
            store,
            export_id="export-after-rename",
            analysis_id=analysis_id,
        )
        os.rename(staging_after, final_after)
        assert store.recover_export_publications() == ["export-after-rename"]
        export_rows = store.telemetry_rows(event_type="evidence_export")
        assert {item["subject_id"] for item in export_rows}.issuperset(
            {"export-before-rename", "export-after-rename"}
        )
    finally:
        store.close()


def test_export_finalization_failure_retains_recoverable_intent_and_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _seed_authoritative_project(tmp_path / "atomic-export")
    analysis_id = store.telemetry_rows(event_type="analysis_completion")[0]["analysis_id"]
    staging, final = _prepare_minimal_export_intent(
        store,
        export_id="export-atomic-failure",
        analysis_id=analysis_id,
    )
    os.rename(staging, final)
    original_enqueue = store._enqueue_outbox

    def fail_outbox(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("injected outbox failure")

    try:
        monkeypatch.setattr(store, "_enqueue_outbox", fail_outbox)
        with pytest.raises(RuntimeError, match="injected outbox failure"):
            store.finalize_export_publication("export-atomic-failure")
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM eco_exports WHERE export_id='export-atomic-failure'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.get_record("export_publication_intent", "export-atomic-failure")[
                "final_relative_path"
            ]
            == "exports/export-atomic-failure"
        )
        monkeypatch.setattr(store, "_enqueue_outbox", original_enqueue)
        assert store.recover_export_publications() == ["export-atomic-failure"]
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM eco_exports WHERE export_id='export-atomic-failure'"
            ).fetchone()[0]
            == 1
        )
        export_rows = store.telemetry_rows(
            event_type="evidence_export", subject_id="export-atomic-failure"
        )
        assert len(export_rows) == 1
    finally:
        store.close()


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


def test_sync_authority_excludes_post_inventory_producer_race_and_stales_approval(
    tmp_path: Path,
) -> None:
    project = tmp_path / "inventory-race"
    store, sync_plan = _seed_authoritative_project(project)
    before = build_telemetry_projection_inventory(
        store=store,
        project_root=project,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
        reserve_oversize_approval=True,
    )
    authorized_ids = {item.outbox_id for item in before.authorized_envelopes}

    # Simulate a producer commit after the reviewed inventory but before the
    # consumer claims rows. The new envelope is valid evidence, just not part
    # of this exact synchronization authority.
    store.set_guided(
        "guided-post-inventory-race",
        "VERIFIED_REPLAY",
        {"verified_fixture_replay": True, "race_fixture": True},
    )
    late = store.telemetry_rows(
        event_type="guided_session",
        subject_id="guided-post-inventory-race",
    )[0]
    assert late["outbox_id"] not in authorized_ids

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
        authorized_envelopes=[item.as_dict() for item in before.authorized_envelopes],
    )
    report = consumer.drain(online=True, limit=100)

    assert report.synced == len(authorized_ids)
    assert all(
        projection.run_subject_id != "guided-post-inventory-race"
        for projection in uploader.projections
    )
    late_after = store.telemetry_rows(
        event_type="guided_session",
        subject_id="guided-post-inventory-race",
    )[0]
    assert late_after["state"] == "PENDING"
    assert late_after["attempt_count"] == 0

    after = build_telemetry_projection_inventory(
        store=store,
        project_root=project,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
        reserve_oversize_approval=True,
    )
    assert after.sha256 != before.sha256
    bundle = store.effective_bundle()
    oversize_bundle = bundle.model_copy(
        update={
            "resource_policy": bundle.resource_policy.model_copy(
                update={"max_materialized_bytes": 1}
            )
        }
    )
    scope = _target().scope_evidence
    reviewed_plan = plan_cloud_sync(
        oversize_bundle,
        destination=SyncDestination.DEDICATED,
        scope_evidence=scope,
        estimated_current_raster_bytes=0,
        binding_id="binding-inventory-race",
        telemetry_inventory=before,
    )
    approval = create_oversize_sync_approval(
        reviewed_plan,
        reviewed_by="Inventory race reviewer",
        approved_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    next_plan = plan_cloud_sync(
        oversize_bundle,
        destination=SyncDestination.DEDICATED,
        scope_evidence=scope,
        estimated_current_raster_bytes=0,
        binding_id="binding-inventory-race",
        telemetry_inventory=after,
        approval=approval,
    )
    assert next_plan.approval_status == "STALE"
    assert "OVERSIZE_SYNC_APPROVAL_STALE" in " ".join(next_plan.blockers)
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
    with pytest.raises(
        ValueError,
        match="prepared|semantic diff|footprint proof|input artifact|provenance",
    ):
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
