"""Method-specific EcoCorridor JSON-RPC response contracts.

The worker validates requests at dispatch and validates these response data
shapes again before crossing the JSONL boundary.  Provider receipts and large
projection documents remain opaque nested JSON where their own authoritative
contracts already apply, but every method has an exact top-level shape.
"""

from __future__ import annotations

import hashlib
from typing import Any, Generic, Literal, TypeVar

from pydantic import AwareDatetime, Field, model_validator
from scenario_platform.contracts import ContractModel, TypedError, canonical_json, require_sha256

from .assistants import AssistantAnswer, AssistantRunAssociation, IntakeDraft
from .contracts import (
    ConnectivityAsset,
    ConnectivityCostBasis,
    ConnectivityEvidenceClaim,
    ConnectivityExportEligibility,
    ConnectivityModelFitness,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ConnectivityScenario,
    EngineManifest,
    GridAlignmentApproval,
    GridAlignmentPlan,
    InterventionOption,
    InterventionSite,
    ModelRevision,
    PortfolioDecisionSummary,
    RasterGrid,
    ResourcePolicy,
    SemanticModelDiff,
    Sha256Digest,
)
from .digitization import DigitizationPreview, DigitizationPublication
from .export import ConnectivityExportManifest

API_VERSION = "1.0"
DataT = TypeVar("DataT", bound=ContractModel)


class RpcPayloadResult(ContractModel, Generic[DataT]):
    api_version: Literal["1.0"] = API_VERSION
    method: str
    data: DataT
    data_sha256: Sha256Digest

    @model_validator(mode="after")
    def _validate_contract(self) -> RpcPayloadResult[DataT]:
        if not self.method:
            raise ValueError("RPC result method is required")
        require_sha256(self.data_sha256, label="RPC result data hash")
        payload = (
            self.data.model_dump(mode="json") if hasattr(self.data, "model_dump") else self.data
        )
        actual = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if actual != self.data_sha256:
            raise ValueError("RPC result data hash does not match the validated payload")
        return self


class SystemPingResult(ContractModel):
    api_version: Literal["1.0"] = API_VERSION
    application: Literal["EcoCorridor Scenario Studio"] = "EcoCorridor Scenario Studio"
    schema_version: Literal["1.0"] = "1.0"
    engine_configured: bool
    methods: list[str]


class JsonRpcSuccessFrame(ContractModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    result: RpcPayloadResult[Any] | SystemPingResult

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_id(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("id"), bool):
            raise ValueError("JSON-RPC response IDs cannot be boolean")  # noqa: TRY004
        return value


class JsonRpcErrorBody(ContractModel):
    code: int
    message: str
    data: TypedError


class JsonRpcErrorFrame(ContractModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None
    error: JsonRpcErrorBody

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_id(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("id"), bool):
            raise ValueError("JSON-RPC response IDs cannot be boolean")  # noqa: TRY004
        return value


class BundleInspectData(ContractModel):
    project_id: str
    title: str
    archive_sha256: Sha256Digest
    file_count: int = Field(ge=1)
    uncompressed_bytes: int = Field(ge=1)
    asset_count: int = Field(ge=0)
    scenario_count: int = Field(ge=0)
    bundle: ConnectivityProjectBundle


class BundleImportData(ContractModel):
    project_id: str
    archive_sha256: Sha256Digest
    state: Literal["IMPORTED"]


class PlanningCreateData(ContractModel):
    project_id: str
    state: Literal["PLANNING_DRAFT"]
    next_action: str


class ProjectIdentityData(ContractModel):
    project_id: str
    title: str
    mode: str


class ProjectAnalysisStatusData(ContractModel):
    analysis_id: str
    state: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PendingRecoveryAnalysisData(ContractModel):
    analysis_id: str
    analysis_state: Literal["RUNNING", "PAUSING"]
    executor_active: bool
    recovery_required: bool
    running_job_ids: list[str]


class PendingRecoveryData(ContractModel):
    required: bool
    analyses: list[PendingRecoveryAnalysisData]
    next_action: str


class EngineAvailabilityData(ContractModel):
    available: bool
    manifest_sha256: Sha256Digest | None
    engine_name: str | None
    circuitscape_version: str | None


class ProjectStateData(ContractModel):
    project: ProjectIdentityData
    active_revision: ModelRevision
    effective_bundle: ConnectivityProjectBundle
    fitness: ConnectivityModelFitness
    resource_policy: ResourcePolicy
    saved_records: dict[str, list[dict[str, Any]]]
    analyses: list[ProjectAnalysisStatusData]
    pending_recovery: PendingRecoveryData
    sync_status: dict[str, Any]
    validation: dict[str, Any]
    engine: EngineAvailabilityData


class AssetInspectData(ContractModel):
    inspection: dict[str, Any]
    transformed: Literal[False]
    next_action: str


class AssetAddData(ContractModel):
    asset_id: str
    revision: int = Field(gt=0)
    sha256: Sha256Digest
    inspection: dict[str, Any] | None


class AssetDownloadPreviewData(ContractModel):
    provider_id: str
    provider: str
    dataset: str
    vintage: str
    resolution: str
    license: str
    attribution: str
    extent_description: str
    expected_bytes: int = Field(gt=0)
    intended_use: str
    source_url: str
    consent_statement: str
    remaining_project_input_bytes: int = Field(ge=0)
    resource_policy_allowed: bool


class AssetDownloadData(ContractModel):
    asset: ConnectivityAsset
    context_only: Literal[True]


class GridPreviewData(ContractModel):
    plan: GridAlignmentPlan


class GridApprovalData(ContractModel):
    approval: GridAlignmentApproval
    derived_asset: ConnectivityAsset
    semantic_diff: SemanticModelDiff
    revision: ModelRevision
    original_asset_retained: str


class DigitizationPreviewData(ContractModel):
    preview: DigitizationPreview


class RecordSaveData(ContractModel):
    record_id: str
    revision: int = Field(gt=0)


class CostBasisGetData(ContractModel):
    cost_basis: ConnectivityCostBasis | None
    active_revision_id: str
    confirmed: bool
    missing_requirements: list[str]


class CostBasisSaveData(RecordSaveData):
    cost_basis: ConnectivityCostBasis
    active_revision_id: str
    cost_basis_sha256: Sha256Digest
    confirmed: Literal[True]


class InterventionSaveData(ContractModel):
    site_id: str
    option_ids: list[str] = Field(min_length=1)


class ModelFindingData(ContractModel):
    code: str
    severity: Literal["BLOCKING", "REVIEW_REQUIRED", "INFORMATION"]
    object_reference: str
    remediation: str


class InterventionEffectHashData(ContractModel):
    option_id: str
    effect_id: str
    scenario_id: str
    sha256: Sha256Digest


class InterventionSemanticDiffData(ContractModel):
    site_id: str
    previous_option_ids: list[str]
    proposed_option_ids: list[str]
    added_option_ids: list[str]
    removed_option_ids: list[str]
    effect_hashes: list[InterventionEffectHashData]


class InterventionPreviewData(ContractModel):
    preview_sha256: Sha256Digest
    revision_id: str
    site: InterventionSite
    options: list[InterventionOption] = Field(min_length=1)
    effect_hashes: list[InterventionEffectHashData]
    findings: list[ModelFindingData]
    can_save: bool
    persisted: Literal[False]
    semantic_diff: InterventionSemanticDiffData


class ModelValidationData(ContractModel):
    status: Literal["READY", "BLOCKED"]
    findings: list[ModelFindingData]
    project_id: str | None = None
    revision_id: str | None = None
    scenario_count: int | None = Field(default=None, ge=0)
    target_scenario_count: int | None = Field(default=None, ge=0)
    safeguard_scenario_count: int | None = Field(default=None, ge=0)
    asset_count: int | None = Field(default=None, ge=0)
    intervention_site_count: int | None = Field(default=None, ge=0)
    retained_source_validation: bool | None = None


class ModelAttestData(ContractModel):
    passport_id: str
    fitness_revision: int = Field(gt=0)


class AnalysisPlanData(ContractModel):
    analysis_id: str
    state: Literal["READY"]
    direct_job_count: int = Field(gt=0)
    review_findings: list[ModelFindingData]
    arithmetic: str


class AnalysisSearchProjectionData(ContractModel):
    method: str
    qmc_engine: str | None
    finite_universe_size: int = Field(gt=0)
    portfolio_capacity: int = Field(gt=0)
    selected_portfolio_count: int = Field(gt=0)
    seed: int = Field(ge=0)


class AnalysisPrepareData(ContractModel):
    project_id: str
    revision_id: str
    scenario_ids: list[str]
    resource_policy: ResourcePolicy
    engine_available: bool
    engine_manifest: EngineManifest | None
    finite_universe_size: int = Field(ge=0)
    portfolio_capacity: int = Field(ge=0)
    selected_portfolio_count: int = Field(ge=0)
    direct_job_count: int = Field(ge=0)
    arithmetic: str
    blockers: list[ModelFindingData]
    search: AnalysisSearchProjectionData | None
    proposed_plan: ConnectivityRunPlan | None


class AnalysisStateData(ContractModel):
    analysis_id: str
    state: str


class AnalysisStatusData(AnalysisStateData):
    plan: ConnectivityRunPlan
    completed_run_count: int = Field(ge=0)
    summary_count: int = Field(ge=0)
    evidence_claim_count: int = Field(ge=0)
    direct_job_counts: dict[str, int]
    error: dict[str, Any] | None


class DecisionBoardData(ContractModel):
    analysis_id: str
    summaries: list[PortfolioDecisionSummary] = Field(min_length=1)
    direct_run_ids: list[str]
    evidence_claim_ids: list[str]
    claim_language: Literal["Direct Circuitscape result under documented assumptions."]


class ScenarioDetailRunIdentity(ContractModel):
    direct_run_id: str
    portfolio_id: str
    scenario_id: str
    exact: Literal[True]
    engine_name: Literal["Circuitscape"]
    engine_manifest_sha256: Sha256Digest
    model_run_cache_key: Sha256Digest
    result_sha256: Sha256Digest
    matrix_sha256: Sha256Digest
    current_map_sha256: Sha256Digest | None = None
    qa_status: Literal["ACCEPTED", "WARNING_ACCEPTED"]
    qa_findings: list[str]


class ScenarioPairEvidence(ContractModel):
    pair_id: str
    required: bool
    baseline_effective_resistance: float = Field(gt=0)
    portfolio_effective_resistance: float = Field(gt=0)
    baseline_relative_reduction_percent: float
    baseline_direct_run_id: str
    portfolio_direct_run_id: str
    baseline_matrix_sha256: Sha256Digest
    portfolio_matrix_sha256: Sha256Digest
    evidence_claim_ids: list[str] = Field(min_length=1)
    primary_evidence_claim_id: str

    @model_validator(mode="after")
    def primary_claim_is_retained(self) -> ScenarioPairEvidence:
        if self.primary_evidence_claim_id not in self.evidence_claim_ids:
            raise ValueError("primary pair claim must be in the retained claim set")
        if len(self.evidence_claim_ids) != len(set(self.evidence_claim_ids)):
            raise ValueError("pair evidence claim IDs must be unique")
        return self


class CurrentMapArtifactIdentity(ContractModel):
    direct_run_id: str
    artifact_id: str
    sha256: Sha256Digest
    byte_size: int = Field(gt=0)
    media_type: str


class CurrentMapPreviewCell(ContractModel):
    preview_row: int = Field(ge=0)
    preview_column: int = Field(ge=0)
    source_row: int = Field(ge=0)
    source_column: int = Field(ge=0)
    baseline_current: float | None
    portfolio_current: float | None


class CurrentMapPreview(ContractModel):
    width: int = Field(gt=0, le=64)
    height: int = Field(gt=0, le=64)
    selection_method: Literal["SYSTEMATIC_EXACT_CELL_SAMPLE"]
    baseline_minimum: float = Field(ge=0)
    baseline_maximum: float = Field(ge=0)
    portfolio_minimum: float = Field(ge=0)
    portfolio_maximum: float = Field(ge=0)
    cells: list[CurrentMapPreviewCell] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def preview_is_complete(self) -> CurrentMapPreview:
        if len(self.cells) != self.width * self.height:
            raise ValueError("current-map preview must contain one exact sample per preview cell")
        return self


class CurrentMapExactCell(ContractModel):
    linear_index: int = Field(ge=0)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    x_center: float
    y_center: float
    baseline_current: float | None
    portfolio_current: float | None
    portfolio_minus_baseline: float | None


class CurrentMapExactCellPage(ContractModel):
    offset: int = Field(ge=0)
    limit: int = Field(gt=0, le=500)
    total_cell_count: int = Field(gt=0)
    has_previous: bool
    has_next: bool
    cells: list[CurrentMapExactCell] = Field(max_length=500)


class ScenarioMapEvidence(ContractModel):
    status: Literal["AVAILABLE", "ABSENT"]
    absence_code: str | None = None
    absence_reason: str | None = None
    grid: RasterGrid | None = None
    baseline_artifact: CurrentMapArtifactIdentity | None = None
    portfolio_artifact: CurrentMapArtifactIdentity | None = None
    evidence_claim_ids: list[str] = Field(default_factory=list)
    preview: CurrentMapPreview | None = None
    exact_cell_page: CurrentMapExactCellPage | None = None
    interpretation: Literal[
        "Diagnostic current redistribution; not observed or predicted animal use."
    ] = "Diagnostic current redistribution; not observed or predicted animal use."

    @model_validator(mode="after")
    def availability_is_explicit(self) -> ScenarioMapEvidence:
        retained = (
            self.grid,
            self.baseline_artifact,
            self.portfolio_artifact,
            self.preview,
            self.exact_cell_page,
        )
        if self.status == "AVAILABLE":
            if any(item is None for item in retained):
                raise ValueError("available map evidence requires both artifacts and exact cells")
            if self.absence_code is not None or self.absence_reason is not None:
                raise ValueError("available map evidence cannot contain an absence reason")
        elif any(item is not None for item in retained):
            raise ValueError("absent map evidence cannot expose partial raster values")
        elif self.evidence_claim_ids:
            raise ValueError("absent map evidence cannot reference map claims")
        elif not self.absence_code or not self.absence_reason:
            raise ValueError("absent map evidence requires an explicit code and remediation")
        return self


class ScenarioDetailData(ContractModel):
    analysis_id: str
    portfolio_id: str
    baseline_portfolio_id: str
    model_revision_id: str
    scenario: ConnectivityScenario
    baseline_run: ScenarioDetailRunIdentity
    portfolio_run: ScenarioDetailRunIdentity
    pair_evidence: list[ScenarioPairEvidence] = Field(min_length=1)
    map_evidence: ScenarioMapEvidence
    same_scenario_only: Literal[True]
    claim_language: Literal["Direct Circuitscape result under documented assumptions."]
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def detail_is_one_scenario(self) -> ScenarioDetailData:
        scenario_id = self.scenario.scenario_id
        if (
            self.baseline_run.scenario_id != scenario_id
            or self.portfolio_run.scenario_id != scenario_id
        ):
            raise ValueError("scenario detail cannot mix direct runs from different scenarios")
        if self.baseline_run.portfolio_id != self.baseline_portfolio_id:
            raise ValueError("scenario detail baseline run has the wrong portfolio")
        if self.portfolio_run.portfolio_id != self.portfolio_id:
            raise ValueError("scenario detail subject run has the wrong portfolio")
        if self.baseline_run.engine_manifest_sha256 != self.portfolio_run.engine_manifest_sha256:
            raise ValueError("scenario detail cannot compare different engine manifests")
        pair_ids = [item.pair_id for item in self.pair_evidence]
        if pair_ids != self.scenario.included_pair_ids or len(pair_ids) != len(set(pair_ids)):
            raise ValueError("scenario detail pair rows must exactly match the scenario contract")
        return self


class GuidedEvidenceClaimProjection(ContractModel):
    claim_id: str
    portfolio_id: str
    scenario_id: str
    pair_id: str | None = None
    value: float
    units: str
    reducer: str
    direct_run_ids: list[str] = Field(min_length=1)


class EvidenceResolveData(ContractModel):
    claim: ConnectivityEvidenceClaim | GuidedEvidenceClaimProjection
    analysis_id: str | None = None
    analysis_plan_sha256: Sha256Digest | None = None
    model_revision: ModelRevision | None = None
    model_bundle_sha256: Sha256Digest | None = None
    direct_runs: list[dict[str, Any]] | None = None
    baseline_runs: list[dict[str, Any]] | None = None
    portfolio_runs: list[dict[str, Any]] | None = None
    prepared_runs: list[dict[str, Any]] | None = None
    artifact_records: list[dict[str, Any]] | None = None
    map_descriptors: list[dict[str, Any]] | None = None
    source_assets: list[dict[str, Any]] | None = None
    scenarios: list[dict[str, Any]] | None = None
    engine_manifest_sha256: Sha256Digest | None = None
    workflow_manifest_sha256: Sha256Digest | None = None
    evidence_index_sha256: Sha256Digest | None = None
    qa: str | None = None
    qa_records: list[dict[str, Any]] | None = None
    assumptions: list[str] | None = None
    cloud_references: dict[str, Any] | None = None
    limitations: list[str] | None = None


class ExportValidateData(ContractModel):
    eligibility: ConnectivityExportEligibility


class ExportCreateData(ContractModel):
    export_id: str
    project_relative_directory: str
    manifest: ConnectivityExportManifest

    @model_validator(mode="after")
    def validate_project_relative_directory(self) -> ExportCreateData:
        normalized = self.project_relative_directory.replace("\\", "/")
        if normalized != f"exports/{self.export_id}":
            raise ValueError("export response must use its authoritative project-relative path")
        if self.manifest.export_id != self.export_id:
            raise ValueError("export response and manifest IDs must match")
        return self


class GuidedOpenData(ContractModel):
    session_id: str
    projection: dict[str, Any]
    can_rerun: bool
    state: str
    evidence_manifest_sha256: Sha256Digest | None


class GuidedRerunData(ContractModel):
    session_id: str
    analysis_id: str
    state: str


class GuidedStatusData(ContractModel):
    session_id: str
    analysis_id: str
    state: Literal["RUNNING", "PAUSING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]
    phase: Literal["MATRIX", "MAP", "DIFFERENTIAL", "COMPLETE"]
    completed_jobs: int = Field(ge=0, le=1596)
    total_jobs: Literal[1596] = 1596
    matrix_completed: int = Field(ge=0, le=1536)
    map_completed: int = Field(ge=0, le=60)
    cached_jobs: int = Field(ge=0, le=1596)
    differential: dict[str, Any] | None

    @model_validator(mode="after")
    def validate_progress_arithmetic(self) -> GuidedStatusData:
        if self.completed_jobs != self.matrix_completed + self.map_completed:
            raise ValueError("guided completed jobs must equal matrix plus map completions")
        if self.cached_jobs > self.completed_jobs:
            raise ValueError("guided cached jobs cannot exceed completed jobs")
        if self.phase == "COMPLETE" and self.state != "COMPLETED":
            raise ValueError("guided COMPLETE phase requires COMPLETED state")
        if self.state == "COMPLETED" and (
            self.phase != "COMPLETE" or self.completed_jobs != self.total_jobs
        ):
            raise ValueError("completed guided rerun requires all direct jobs and COMPLETE phase")
        return self


class GuidedControlData(ContractModel):
    session_id: str
    state: str


class GuidedCheckpointEventData(ContractModel):
    event_id: str
    session_id: str
    sequence_number: int = Field(gt=0)
    checkpoint_id: str
    event_kind: str
    payload: dict[str, Any]
    created_at: AwareDatetime


class ObservabilityPreflightData(ContractModel):
    preflight_id: str
    scope: Literal["RESTRICTED"]
    allowed: Literal[True]
    message: str
    binding_preview: dict[str, Any]


class CloudSyncPlanData(ContractModel):
    destination: str | None = None
    projections: list[dict[str, Any]] | None = None
    input_bytes: int | None = Field(default=None, ge=0)
    estimated_current_raster_bytes: int | None = Field(default=None, ge=0)
    estimated_total_bytes: int | None = Field(default=None, ge=0)
    current_raster_estimate_status: (
        Literal[
            "NOT_REQUIRED",
            "PRE_RUN_ESTIMATE",
            "ACTUAL_RECONCILED",
            "PARTIALLY_RECONCILED",
            "UNAVAILABLE",
        ]
        | None
    ) = None
    actual_retained_current_raster_bytes: int | None = Field(default=None, ge=0)
    pending_estimated_current_raster_bytes: int | None = Field(default=None, ge=0)
    expected_current_raster_count: int | None = Field(default=None, ge=0)
    retained_current_raster_count: int | None = Field(default=None, ge=0)
    current_raster_estimate_basis: str | None = None
    blockers: list[str]
    warnings: list[str]
    allowed: bool
    message: str | None = None
    technical_type: str | None = None


class ObservabilityBindingData(ContractModel):
    state: str
    binding_id: str | None = None
    entity: str | None = None
    project: str | None = None
    deployment: str | None = None
    api_base_url: str | None = None
    web_base_url: str | None = None
    profile_namespace: str | None = None
    profile: str | None = None
    scope_evidence: dict[str, Any] | None = None
    verified: bool | None = None
    external_project_id: str | None = None
    estimated_current_raster_bytes: int | None = Field(default=None, ge=0)
    oversize_approved_by: str | None = None
    last_sync: dict[str, Any] | None = None


class ObservabilityConnectData(ContractModel):
    binding: ObservabilityBindingData
    upload_preview: CloudSyncPlanData


class ObservabilityBindingResultData(ContractModel):
    binding: ObservabilityBindingData


class ObservabilitySyncData(ContractModel):
    binding: ObservabilityBindingData
    receipt: dict[str, Any]


class ObservabilityLinkData(ContractModel):
    url: str


class AssistantProviderStatusData(ContractModel):
    base_url: str
    model: str
    timeout_seconds: float = Field(gt=0)


class AssistantStatusData(ContractModel):
    configured: bool
    provider: AssistantProviderStatusData | None = None
    project_id: str | None = None
    wandb_external_project_id: str | None = None
    trace_required: Literal[True] | None = None
    cleared: Literal[True] | None = None


class AssistantAnswerData(ContractModel):
    answer: AssistantAnswer


class IntakeExtractData(ContractModel):
    draft: IntakeDraft
    ready_for_model: Literal[False]
    human_confirmation_required: Literal[True]


class IntakeGetData(ContractModel):
    draft: IntakeDraft
    ready_for_model: bool


class IntakeConfirmData(ContractModel):
    draft: IntakeDraft
    ready_for_model: bool
    confirmed_fields: list[dict[str, Any]] | None
    applied_to_model: Literal[False]


class AssistantFeedbackData(ContractModel):
    recorded: Literal[True]
    receipt: dict[str, Any]


class AssistantTraceData(ContractModel):
    call_id: str
    disposition: Literal["ANSWERED", "REJECTED"]
    trace_id: str | None = None
    association: AssistantRunAssociation | None = None
    local_rejection_audit: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> AssistantTraceData:
        answered = self.disposition == "ANSWERED"
        if answered != bool(
            self.trace_id and self.association is not None and self.local_rejection_audit is None
        ):
            raise ValueError("assistant trace disposition has inconsistent evidence")
        if not answered:
            audit = self.local_rejection_audit
            if (
                self.trace_id is not None
                or self.association is not None
                or not isinstance(audit, dict)
                or audit.get("local_audit_verified") is not True
                or audit.get("answer_returned") is not False
            ):
                raise ValueError("rejected assistant trace requires a verified local audit")
        return self


RPC_RESPONSE_DATA_CONTRACTS: dict[str, type[ContractModel]] = {
    "connectivity.bundle.inspect": BundleInspectData,
    "connectivity.bundle.import": BundleImportData,
    "connectivity.project.get_state": ProjectStateData,
    "connectivity.planning.create": PlanningCreateData,
    "connectivity.asset.inspect": AssetInspectData,
    "connectivity.asset.add": AssetAddData,
    "connectivity.asset.download.preview": AssetDownloadPreviewData,
    "connectivity.asset.download": AssetDownloadData,
    "connectivity.grid.preview": GridPreviewData,
    "connectivity.grid.approve": GridApprovalData,
    "connectivity.digitization.preview": DigitizationPreviewData,
    "connectivity.digitization.publish": DigitizationPublication,
    "connectivity.hypothesis.save": RecordSaveData,
    "connectivity.focal_areas.save": RecordSaveData,
    "connectivity.intervention.preview": InterventionPreviewData,
    "connectivity.intervention.save": InterventionSaveData,
    "connectivity.scenario.save": RecordSaveData,
    "connectivity.safeguard.save": RecordSaveData,
    "connectivity.review_mask.save": RecordSaveData,
    "connectivity.decision_rule.save": RecordSaveData,
    "connectivity.decision_brief.save": RecordSaveData,
    "connectivity.cost_basis.get": CostBasisGetData,
    "connectivity.cost_basis.save": CostBasisSaveData,
    "connectivity.resource_policy.save": RecordSaveData,
    "connectivity.model.validate": ModelValidationData,
    "connectivity.model.attest": ModelAttestData,
    "connectivity.analysis.prepare": AnalysisPrepareData,
    "connectivity.analysis.plan": AnalysisPlanData,
    "connectivity.analysis.start": AnalysisStateData,
    "connectivity.analysis.status": AnalysisStatusData,
    "connectivity.analysis.pause": AnalysisStateData,
    "connectivity.analysis.resume": AnalysisStateData,
    "connectivity.analysis.cancel": AnalysisStateData,
    "connectivity.results.decision_board": DecisionBoardData,
    "connectivity.results.scenario_detail": ScenarioDetailData,
    "connectivity.evidence.resolve": EvidenceResolveData,
    "connectivity.export.validate": ExportValidateData,
    "connectivity.export.create": ExportCreateData,
    "connectivity.guided.open": GuidedOpenData,
    "connectivity.guided.rerun": GuidedRerunData,
    "connectivity.guided.status": GuidedStatusData,
    "connectivity.guided.pause": GuidedControlData,
    "connectivity.guided.resume": GuidedControlData,
    "connectivity.guided.cancel": GuidedControlData,
    "connectivity.guided.checkpoint": GuidedCheckpointEventData,
    "observability.preflight": ObservabilityPreflightData,
    "observability.connect": ObservabilityConnectData,
    "observability.validate_binding": ObservabilityBindingResultData,
    "observability.get_status": ObservabilityBindingData,
    "observability.preview_upload": CloudSyncPlanData,
    "observability.sync": ObservabilitySyncData,
    "observability.retry": ObservabilitySyncData,
    "observability.open_run": ObservabilityLinkData,
    "observability.open_artifact": ObservabilityLinkData,
    "observability.open_trace": ObservabilityLinkData,
    "assistant.configure": AssistantStatusData,
    "assistant.status": AssistantStatusData,
    "assistant.clear": AssistantStatusData,
    "assistant.explain": AssistantAnswerData,
    "assistant.follow_up": AssistantAnswerData,
    "assistant.intake.extract": IntakeExtractData,
    "assistant.intake.get": IntakeGetData,
    "assistant.intake.confirm": IntakeConfirmData,
    "assistant.feedback": AssistantFeedbackData,
    "assistant.get_trace": AssistantTraceData,
}


def response_contract_for(method: str) -> type[RpcPayloadResult[Any]]:
    try:
        data_contract = RPC_RESPONSE_DATA_CONTRACTS[method]
    except KeyError as exc:
        raise ValueError(f"missing method-specific RPC response contract: {method}") from exc
    return RpcPayloadResult[data_contract]  # type: ignore[return-value,valid-type]


__all__ = [
    "RPC_RESPONSE_DATA_CONTRACTS",
    "JsonRpcErrorBody",
    "JsonRpcErrorFrame",
    "JsonRpcSuccessFrame",
    "RpcPayloadResult",
    "SystemPingResult",
    "response_contract_for",
]
