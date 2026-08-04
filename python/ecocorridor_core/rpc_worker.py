"""EcoCorridor JSONL/JSON-RPC worker with authoritative, fail-closed services."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from scenario_platform.archive import (
    ArchiveMember,
    ArchiveSecurityError,
    extract_zip,
    inspect_zip,
    read_zip_member,
)
from scenario_platform.cloud_sync import InMemoryCredentialSession, WandbTarget
from scenario_platform.contracts import (
    AnalysisState,
    ErrorCategory,
    Severity,
    TypedError,
    canonical_json,
    new_id,
    require_sha256,
)
from scenario_platform.contracts import (
    ContractModel as PlatformContractModel,
)
from scenario_platform.observability import ProjectScopeEvidence, ProjectScopeResolver
from scenario_platform.rpc import RpcProtocolError, RuntimeContractRegistry, encode_jsonl
from scenario_platform.storage import PathSecurityError, contained_path

from .analysis_executor import (
    AnalysisOwnershipError,
    DurableAnalysisError,
    DurableAnalysisExecutor,
    OrphanProcessAuditError,
)
from .assistant_runtime import ProviderBackedAssistantRuntime
from .assistants import (
    AssistantAnswer,
    AssistantEvidenceAuthorization,
    AssistantProviderConfiguration,
    AssistantRunAssociation,
    AssistantSafetyError,
    AuthorizedEvidenceClaimContext,
    AuthorizedEvidenceItem,
    AuthorizedEvidenceSelection,
    DocumentPage,
    EvidenceAssistant,
    IntakeDraft,
    SensitiveContextPolicy,
    validate_assistant_limitation,
    validate_feedback_comment,
)
from .claim_safety import (
    assert_safe_rendered_ecological_copy,
    assert_strict_ecological_claim,
)
from .contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityCostBasis,
    ConnectivityDecisionBrief,
    ConnectivityDecisionRule,
    ConnectivityEvidenceClaim,
    ConnectivityExportEligibility,
    ConnectivityModelFitness,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ConnectivityScenario,
    DirectConnectivityRun,
    EngineManifest,
    FocalAreaSet,
    GridAlignmentApproval,
    GridAlignmentPlan,
    InterventionOption,
    InterventionOptionDraft,
    InterventionSite,
    ModelRevision,
    PortfolioDecisionSummary,
    RasterGrid,
    RasterValueKind,
    ReferenceComparisonPlan,
    ReferenceComparisonResult,
    ResamplingMethod,
    ResistanceHypothesis,
    ResourceObservationBoundary,
    ResourcePolicy,
    RiskReviewMask,
    SafeguardRecord,
    SemanticChange,
    SemanticModelDiff,
    SensitiveDataAuthorization,
    SensitivityClass,
    canonical_sha256,
)
from .cost_basis import (
    cost_basis_requirements,
    parse_reviewed_cost_basis,
    with_reviewer_confirmation,
)
from .digitization import (
    DigitizationError,
    DigitizationPreviewParams,
    DigitizationPublishParams,
    preview_digitization,
    publish_digitization,
)
from .export import (
    ConnectivityExportManifest,
    EvidenceArtifactSource,
    EvidenceProjectAssetSource,
    ReferenceEvidenceExportSource,
    compute_export_eligibility,
    create_evidence_export,
)
from .governance import (
    CurrentRasterEstimate,
    CurrentRasterEstimateStatus,
    SnapshotAssetAuthorizationError,
    SyncDestination,
    plan_cloud_sync,
    reconcile_snapshot_assets,
    scientific_asset_ids,
)
from .grid import (
    InMemoryRaster,
    _is_nodata,
    apply_approved_grid_alignment,
    approve_grid_alignment,
    grid_alignment_plan_sha256,
    preview_grid_alignment,
)
from .guided_evidence import GuidedEvidenceError, GuidedEvidenceResource
from .interventions import (
    canonicalize_intervention_option,
    canonicalize_portfolio,
    intervention_preview_sha256,
    scenario_effects_for_portfolio,
)
from .metrics import (
    CONNECTIVITY_LIMITATION,
    CURRENT_MAP_LIMITATION,
    validate_complete_ensemble,
    validate_evidence_claim,
)
from .observability import ECOCORRIDOR_APP_PROFILE as APP_PROFILE
from .observability import EcoRpcObservabilityAdapter
from .professional_validation import (
    ModelFinding,
    validate_professional_bundle,
    validate_run_plan,
    validation_projection,
)
from .project_store import EcoProjectStore
from .providers import download_approved_asset, preview_download
from .raster_io import (
    RasterIOError,
    current_map_values_and_nodata,
    inspect_raster,
    inspect_reviewed_raster,
    load_raster,
    write_esri_ascii,
)
from .reference_workflow import (
    ReferenceWorkflowError,
    assemble_and_store_reference_result,
    build_reference_comparison_plan,
    recover_pending_reference_results,
)
from .resource_observability import ProcessResourceRecorder
from .ridgeway import (
    build_public_ui_projection,
    build_ridgeway_fixture,
    ridgeway_assumption_ledger_payload,
)
from .rpc_response_contracts import (
    RPC_RESPONSE_DATA_CONTRACTS,
    JsonRpcErrorBody,
    JsonRpcErrorFrame,
    JsonRpcSuccessFrame,
    SystemPingResult,
    response_contract_for,
)
from .rpc_response_contracts import (
    RpcPayloadResult as PayloadResult,
)
from .search import make_search_plan

API_VERSION = "1.0"
if APP_PROFILE.api_version != API_VERSION:  # pragma: no cover - packaging invariant
    raise RuntimeError("EcoCorridor RPC and observability API versions differ")


class RPCFault(Exception):
    def __init__(self, code: int, error: TypedError) -> None:
        self.code = code
        self.error = error
        super().__init__(error.user_message)


def _fault(
    code: str,
    user_message: str,
    *,
    category: ErrorCategory = ErrorCategory.USER_FIXABLE,
    severity: Severity = Severity.ERROR,
    remediation: str | None = None,
    object_reference: str | None = None,
    retryable: bool = False,
    technical_message: str = "",
    rpc_code: int = -32000,
) -> RPCFault:
    return RPCFault(
        rpc_code,
        TypedError(
            code=code,
            category=category,
            severity=severity,
            user_message=user_message,
            technical_message=technical_message,
            object_reference=object_reference,
            remediation=remediation,
            retryable=retryable,
        ),
    )


class EmptyParams(PlatformContractModel):
    pass


class ProjectParams(PlatformContractModel):
    project_directory: str


class ProjectAnalysisParams(ProjectParams):
    analysis_id: str


class AnalysisPrepareParams(ProjectParams):
    seed: int = Field(default=0, ge=0)


PingResult = SystemPingResult


class BundleInspectParams(PlatformContractModel):
    bundle_path: str
    expected_sha256: str | None = None


class BundleImportParams(BundleInspectParams):
    project_directory: str


class PlanningCreateParams(ProjectParams):
    project_id: str
    title: str
    revision: ModelRevision
    decision_brief: ConnectivityDecisionBrief
    fitness: ConnectivityModelFitness
    resource_policy: ResourcePolicy
    scenarios: list[ConnectivityScenario] = Field(default_factory=list)
    safeguards: list[SafeguardRecord] = Field(default_factory=list)
    review_masks: list[RiskReviewMask] = Field(default_factory=list)
    decision_rules: list[ConnectivityDecisionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity(self) -> PlanningCreateParams:
        if self.revision.project_id != self.project_id:
            raise ValueError("planning revision must belong to the new project")
        return self


class AssetAddParams(ProjectParams):
    source_path: str
    asset: ConnectivityAsset


class AssetInspectParams(ProjectParams):
    source_path: str
    grid_id: str
    value_kind: RasterValueKind
    format: Literal["GEOTIFF", "ESRI_ASCII"]
    esri_ascii_crs: str | None = None
    esri_ascii_cell_area_m2: float | None = Field(default=None, gt=0)


class AssetDownloadParams(ProjectParams):
    provider_id: str
    source_url: str
    extent_description: str
    expected_bytes: int = Field(gt=0)
    selected_vintage: str | None = None
    selected_resolution: str | None = None
    asset_id: str
    title: str
    kind: AssetKind
    media_type: str
    authorization: SensitiveDataAuthorization
    explicit_consent: bool
    maximum_bytes: int = Field(gt=0)


class AssetDownloadPreviewParams(ProjectParams):
    provider_id: str
    source_url: str
    extent_description: str
    expected_bytes: int = Field(gt=0)
    selected_vintage: str | None = None
    selected_resolution: str | None = None


class GridPreviewParams(ProjectParams):
    source_asset_id: str
    target_grid: RasterGrid
    method: ResamplingMethod
    plan_id: str


class GridApproveParams(ProjectParams):
    plan_id: str
    approval_id: str
    reviewer: str
    approved_at: datetime
    acknowledged_warnings: list[str] = Field(default_factory=list)
    purpose: str
    derived_asset_id: str
    derived_title: str
    derived_relative_path: str
    derived_authorization: SensitiveDataAuthorization
    revision_id: str
    reason: str

    @model_validator(mode="after")
    def validate_derived_identity(self) -> GridApproveParams:
        if self.derived_authorization.asset_id != self.derived_asset_id:
            raise ValueError("derived authorization must belong to the derived asset")
        normalized = self.derived_relative_path.replace("\\", "/")
        if (
            not normalized.startswith("data_assets/derived/")
            or not normalized.casefold().endswith(".asc")
            or ".." in normalized.split("/")
        ):
            raise ValueError(
                "derived alignment paths must be contained ESRI ASCII files under data_assets/derived"
            )
        if not self.revision_id.strip() or not self.reason.strip():
            raise ValueError("alignment materialization requires a revision ID and reason")
        return self


class HypothesisSaveParams(ProjectParams):
    hypothesis: ResistanceHypothesis


class FocalAreasSaveParams(ProjectParams):
    focal_area_set: FocalAreaSet


class InterventionSaveParams(ProjectParams):
    site: InterventionSite
    options: list[InterventionOption] = Field(min_length=1)
    preview_sha256: str | None = None


class InterventionPreviewParams(ProjectParams):
    site: InterventionSite
    options: list[InterventionOptionDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog_identity(self) -> InterventionPreviewParams:
        option_ids = [item.option_id for item in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("intervention preview option IDs must be unique")
        if set(option_ids) != set(self.site.option_ids):
            raise ValueError("intervention preview site and option IDs must match")
        if any(item.site_id != self.site.site_id for item in self.options):
            raise ValueError("every preview option must belong to the proposed site")
        return self


class ScenarioSaveParams(ProjectParams):
    scenario: ConnectivityScenario


class SafeguardSaveParams(ProjectParams):
    safeguard: SafeguardRecord


class ReviewMaskSaveParams(ProjectParams):
    review_mask: RiskReviewMask


class DecisionRuleSaveParams(ProjectParams):
    decision_rule: ConnectivityDecisionRule


class DecisionBriefSaveParams(ProjectParams):
    decision_brief: ConnectivityDecisionBrief


class CostBasisSaveParams(ProjectParams):
    cost_basis: ConnectivityCostBasis
    confirmed: Literal[True]
    confirmation_statement: str = Field(min_length=20, max_length=2_000)


class ResourcePolicySaveParams(ProjectParams):
    resource_policy: ResourcePolicy


class ModelAttestParams(ProjectParams):
    fitness: ConnectivityModelFitness
    reviewer: str
    attestation: str


class AnalysisPlanParams(ProjectParams):
    plan: ConnectivityRunPlan


class ReferencePlanParams(ProjectParams):
    analysis_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class ReferenceAnalysisParams(ReferencePlanParams):
    plan_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class AnalysisExecutor(Protocol):
    def is_active(self, *, project_directory: Path, analysis_id: str) -> bool: ...

    def start(
        self,
        *,
        project_directory: Path,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        engine_executable: Path,
        engine_sha256: str,
    ) -> dict[str, Any]: ...

    def pause(self, *, project_directory: Path, analysis_id: str) -> None: ...

    def resume(self, *, project_directory: Path, analysis_id: str) -> None: ...

    def cancel(self, *, project_directory: Path, analysis_id: str) -> None: ...

    def shutdown(self, *, timeout_seconds: float = 5.0) -> bool: ...


class DecisionBoardParams(ProjectAnalysisParams):
    portfolio_ids: list[str] = Field(default_factory=list)


class ScenarioDetailParams(ProjectAnalysisParams):
    portfolio_id: str
    scenario_id: str
    cell_offset: int = Field(default=0, ge=0)
    cell_limit: int = Field(default=100, gt=0, le=500)


class EvidenceResolveParams(ProjectParams):
    claim_id: str


class ExportValidateParams(ProjectAnalysisParams):
    portfolio_ids: list[str] = Field(min_length=1)


class ExportCreateParams(ExportValidateParams):
    export_id: str
    output_directory: str | None = None


class GuidedOpenParams(PlatformContractModel):
    project_directory: str | None = None


class GuidedSessionParams(PlatformContractModel):
    session_id: str
    project_directory: str | None = None


class GuidedCheckpointParams(GuidedSessionParams):
    event_id: str
    sequence_number: int = Field(gt=0)
    checkpoint_id: str
    event_kind: Literal[
        "ENTERED",
        "COMPLETED",
        "BACKTRACKED",
        "EVIDENCE_OPENED",
        "ALTERNATIVE_COMPARED",
        "RERUN_REQUESTED",
        "EXPORT_COMPLETED",
    ]
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    portfolio_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> GuidedCheckpointParams:
        for label, value in (
            ("event ID", self.event_id),
            ("checkpoint ID", self.checkpoint_id),
        ):
            if not value.strip() or len(value) > 128:
                raise ValueError(f"guided {label} must contain 1 to 128 characters")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("guided evidence IDs must be unique")
        if len(set(self.portfolio_ids)) != len(self.portfolio_ids):
            raise ValueError("guided portfolio IDs must be unique")
        return self


class ObservabilityPreflightParams(PlatformContractModel):
    entity: str
    project: str
    deployment: Literal["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]
    api_base_url: str
    web_base_url: str | None = None
    api_key: SecretStr

    @field_validator("api_base_url", "web_base_url")
    @classmethod
    def validate_provider_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Reuse the cloud contract's strict HTTPS/loopback validation rather
        # than accepting credentials, queries, or fragments in a URL.
        placeholder_scope = ProjectScopeEvidence(
            entity="validation",
            project="validation",
            external_project_id="validation",
            deployment="SELF_MANAGED",
            api_base_url=value.rstrip("/"),
            scope="RESTRICTED",
            evidence_kind="ADMIN_API_DOCUMENTED_EXACT",
            provider_field="validation.scope",
        )
        WandbTarget(
            entity="validation",
            project="validation",
            external_project_id="validation",
            deployment="SELF_MANAGED",
            api_base_url=value,
            web_base_url=value,
            scope_evidence=placeholder_scope,
        )
        return value.rstrip("/")


class ObservabilityConnectParams(ProjectParams):
    preflight_id: str
    oversize_approved_by: str | None = None


class ObservabilityPreviewParams(ProjectParams):
    oversize_approved_by: str | None = None


class ObservabilitySyncParams(ProjectParams):
    """Operational connectivity state; never a project-policy override."""

    online: bool = True


class ObservabilityLinkParams(ProjectParams):
    reference_id: str


class ObservabilityAdapter(Protocol):
    def validate(self, *, binding: dict[str, Any]) -> dict[str, Any]: ...

    def sync(
        self,
        *,
        project_directory: Path,
        binding: dict[str, Any],
        sync_plan: dict[str, Any],
        online: bool = True,
    ) -> dict[str, Any]: ...

    def open_link(
        self, *, project_directory: Path, kind: str, reference_id: str
    ) -> dict[str, Any]: ...


class AssistantExplainParams(ProjectParams):
    call_id: str
    question: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)
    sensitive_policy: SensitiveContextPolicy = Field(default_factory=SensitiveContextPolicy)


class AssistantConfigureParams(ProjectParams):
    configuration: AssistantProviderConfiguration
    api_key: SecretStr


class AssistantIntakeExtractParams(ProjectParams):
    call_id: str
    instruction: str
    pages: list[DocumentPage] = Field(min_length=1, max_length=2_000)
    sensitive_policy: SensitiveContextPolicy = Field(default_factory=SensitiveContextPolicy)

    @model_validator(mode="after")
    def validate_request_size(self) -> AssistantIntakeExtractParams:
        if not self.call_id.strip() or not self.instruction.strip():
            raise ValueError("intake call ID and instruction are required")
        total = sum(len(page.text.encode("utf-8")) for page in self.pages)
        if total > 6 * 1024 * 1024:
            raise ValueError("intake document pages exceed the six MiB request limit")
        return self


class AssistantIntakeDraftParams(ProjectParams):
    draft_id: str


class AssistantIntakeConfirmParams(AssistantIntakeDraftParams):
    field_ids: list[str] = Field(min_length=1)
    reviewer: str

    @model_validator(mode="after")
    def validate_confirmation(self) -> AssistantIntakeConfirmParams:
        if not self.draft_id.strip() or not self.reviewer.strip():
            raise ValueError("intake draft ID and human reviewer are required")
        if len(set(self.field_ids)) != len(self.field_ids):
            raise ValueError("intake confirmation field IDs must be unique")
        return self


class AssistantFeedbackParams(ProjectParams):
    call_id: str
    rating: Literal["HELPFUL", "NOT_HELPFUL"]
    comment: str = Field(default="", max_length=1_024)


class AssistantTraceParams(ProjectParams):
    call_id: str


class AssistantFeedbackSink(Protocol):
    def record_feedback(
        self,
        *,
        call_id: str,
        trace_id: str,
        rating: str,
        comment: str,
    ) -> dict[str, Any]: ...


class _Method:
    def __init__(
        self,
        request: type[PlatformContractModel],
        response: type[PlatformContractModel],
        handler: Callable[[Any], PlatformContractModel],
    ) -> None:
        self.request = request
        self.response = response
        self.handler = handler


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _atomic_copy_file(source: Path, destination: Path) -> None:
    """Stream a verified regular file into place without buffering it in memory."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    source_descriptor = -1
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as source_stream,
            os.fdopen(descriptor, "wb", closefd=True) as target_stream,
        ):
            source_descriptor = -1
            descriptor = -1
            shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        if destination.exists():
            raise FileExistsError(destination)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


_SAFE_BLOCKED_RIDGEWAY_ROLE_COPY = {
    "low-cost-caution": (
        "Low-cost caution",
        "Reserved for a current direct-run lower-cost option with an important review flag.",
    ),
    "carry-forward": (
        "Carry-forward candidate",
        "Reserved for a current direct-run candidate that satisfies every teaching rule.",
    ),
    "over-budget": (
        "Modeled-rule pass, over budget",
        "Reserved for a current direct-run result that fails the authored cost rule.",
    ),
    "single-hypothesis-collapse": (
        "Hypothesis-sensitive option",
        "Reserved for a current direct-run option whose rank changes materially across hypotheses.",
    ),
}


def _presentation_safe_blocked_ridgeway_projection(
    projection: dict[str, Any],
) -> dict[str, Any]:
    """Replace legacy blocked-state labels only after exact fixture integrity verification."""

    rendered = json.loads(json.dumps(projection))
    portfolios = rendered.get("portfolios")
    if (
        rendered.get("releaseReplayEligible") is not False
        or rendered.get("evidenceState") != "ENGINE_RUN_REQUIRED"
        or not isinstance(portfolios, list)
        or len(portfolios) != len(_SAFE_BLOCKED_RIDGEWAY_ROLE_COPY)
    ):
        raise ValueError("blocked Ridgeway projection has an invalid presentation boundary")
    seen: set[str] = set()
    for portfolio in portfolios:
        key = portfolio.get("projectionKey") if isinstance(portfolio, dict) else None
        if not isinstance(key, str) or key in seen or key not in _SAFE_BLOCKED_RIDGEWAY_ROLE_COPY:
            raise ValueError("blocked Ridgeway projection has an unknown teaching role")
        seen.add(key)
        name, option_summary = _SAFE_BLOCKED_RIDGEWAY_ROLE_COPY[key]
        portfolio["name"] = name
        portfolio["optionSummary"] = option_summary
    if seen != set(_SAFE_BLOCKED_RIDGEWAY_ROLE_COPY):
        raise ValueError("blocked Ridgeway projection omits a teaching role")
    assert_safe_rendered_ecological_copy(rendered)
    return rendered


class RPCDispatcher:
    def __init__(
        self,
        *,
        data_root: str | Path,
        allowed_roots: Sequence[str | Path] | None = None,
        engine_executable: str | Path | None = None,
        engine_sha256: str | None = None,
        engine_manifest: str | Path | None = None,
        engine_manifest_file_sha256: str | None = None,
        analysis_executor: AnalysisExecutor | None = None,
        scope_resolver: ProjectScopeResolver | None = None,
        observability_adapter: ObservabilityAdapter | None = None,
        evidence_assistant: EvidenceAssistant | None = None,
        assistant_runtime: ProviderBackedAssistantRuntime | None = None,
        assistant_feedback_sink: AssistantFeedbackSink | None = None,
        observability_credentials: InMemoryCredentialSession | None = None,
        download_opener: Any = None,
        guided_projection_path: str | Path | None = None,
        guided_projection_sha256: str | None = None,
        guided_manifest_path: str | Path | None = None,
        guided_manifest_sha256: str | None = None,
        guided_evidence_archive: str | Path | None = None,
        guided_evidence_archive_sha256: str | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = {
            self.data_root,
            *(Path(root).resolve() for root in (allowed_roots or [])),
        }
        self.analysis_executor = analysis_executor
        self.scope_resolver = scope_resolver or ProjectScopeResolver()
        self.observability_adapter = observability_adapter
        self.evidence_assistant = evidence_assistant
        self.assistant_runtime = assistant_runtime
        self.assistant_feedback_sink = assistant_feedback_sink
        adapter_credentials = getattr(observability_adapter, "credentials", None)
        self.observability_credentials = (
            observability_credentials
            or adapter_credentials
            or InMemoryCredentialSession(environment_key="WANDB_API_KEY")
        )
        if (
            adapter_credentials is not None
            and adapter_credentials is not self.observability_credentials
        ):
            raise ValueError(
                "dispatcher and observability adapter must share one credential session"
            )
        self._observability_preflights: dict[str, dict[str, Any]] = {}
        self.download_opener = download_opener
        self.engine_executable: Path | None = None
        self.engine_sha256: str | None = None
        self.engine_manifest_path: Path | None = None
        self.engine_manifest_file_sha256: str | None = None
        self.engine_manifest: EngineManifest | None = None
        if (engine_executable is None) != (engine_sha256 is None):
            raise ValueError("engine executable and pinned SHA-256 must be configured together")
        if (engine_manifest is None) != (engine_manifest_file_sha256 is None):
            raise ValueError(
                "engine manifest and pinned manifest-file SHA-256 must be configured together"
            )
        if engine_executable is None and engine_manifest is not None:
            raise ValueError("an engine manifest cannot be configured without its executable")
        if engine_executable is not None and engine_sha256 is not None:
            supplied_executable = Path(engine_executable).expanduser()
            metadata = os.lstat(supplied_executable)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("configured engine must be a regular non-symlink file")
            executable = supplied_executable.resolve(strict=True)
            digest = require_sha256(engine_sha256.casefold(), label="engine sha256")
            if _sha256_file(executable) != digest:
                raise ValueError("configured engine does not match its pinned SHA-256")
            self.engine_executable = executable
            self.engine_sha256 = digest
        if engine_manifest is not None and engine_manifest_file_sha256 is not None:
            supplied_manifest = Path(engine_manifest).expanduser()
            metadata = os.lstat(supplied_manifest)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("configured engine manifest must be a regular non-symlink file")
            manifest_path = supplied_manifest.resolve(strict=True)
            manifest_file_digest = require_sha256(
                engine_manifest_file_sha256.casefold(),
                label="engine manifest file sha256",
            )
            if _sha256_file(manifest_path) != manifest_file_digest:
                raise ValueError(
                    "configured engine manifest does not match its pinned file SHA-256"
                )
            if metadata.st_size > 1024 * 1024:
                raise ValueError("configured engine manifest exceeds one MiB")
            try:
                parsed_manifest = EngineManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (UnicodeDecodeError, ValidationError, ValueError) as exc:
                raise ValueError("configured engine manifest is invalid") from exc
            self.engine_manifest_path = manifest_path
            self.engine_manifest_file_sha256 = manifest_file_digest
            self.engine_manifest = parsed_manifest
        if (
            self.engine_executable is not None
            and self.engine_manifest is None
            and analysis_executor is None
        ):
            raise ValueError("a packaged engine requires its verified EngineManifest contract")
        if (
            self.engine_executable is not None
            and self.engine_manifest is not None
            and analysis_executor is None
        ):
            self.analysis_executor = DurableAnalysisExecutor(
                self.engine_manifest,
                lock_root=self.data_root / "analysis-locks",
                engine_executable=self.engine_executable,
                engine_sha256=self.engine_sha256,
            )
        repository_root = Path(__file__).resolve().parents[2]
        default_guided_root = repository_root / "benchmarks" / "ecocorridor" / "ridgeway"
        self.guided_projection_path = Path(
            guided_projection_path or default_guided_root / "ui_projection.json"
        ).resolve()
        self.guided_evidence: GuidedEvidenceResource | None = None
        packaged_values = (
            guided_projection_sha256,
            guided_manifest_path,
            guided_manifest_sha256,
            guided_evidence_archive,
            guided_evidence_archive_sha256,
        )
        if any(value is not None for value in packaged_values):
            if not all(value is not None for value in packaged_values):
                raise ValueError("guided evidence resources and hashes must be configured together")
            self.guided_evidence = GuidedEvidenceResource.verify(
                projection_path=self.guided_projection_path,
                projection_sha256=str(guided_projection_sha256),
                manifest_path=str(guided_manifest_path),
                manifest_sha256=str(guided_manifest_sha256),
                archive_path=str(guided_evidence_archive),
                archive_sha256=str(guided_evidence_archive_sha256),
            )

        elif guided_projection_path is None:
            source_manifest = default_guided_root / "direct-evidence-manifest.json"
            if source_manifest.is_file():
                try:
                    source_manifest_payload = json.loads(
                        source_manifest.read_text(encoding="utf-8")
                    )
                    superseded_nonrelease = (
                        source_manifest_payload.get("release_replay_eligible") is False
                        and source_manifest_payload.get("evidence_contract_status")
                        == "SUPERSEDED_NON_RELEASE"
                        and source_manifest_payload.get("historical_evidence_only") is True
                    )
                    if superseded_nonrelease:
                        # Historical bytes remain inspectable in source control but are not
                        # candidate release evidence. `_guided_open` independently matches the
                        # deterministic ENGINE_REQUIRED projection before exposing the lesson.
                        self.guided_evidence = None
                    else:
                        archive_name = source_manifest_payload["evidence_bundle"]["filename"]
                        source_archive = default_guided_root / str(archive_name)
                        self.guided_evidence = GuidedEvidenceResource.verify(
                            projection_path=self.guided_projection_path,
                            projection_sha256=_sha256_file(self.guided_projection_path),
                            manifest_path=source_manifest,
                            manifest_sha256=_sha256_file(source_manifest),
                            archive_path=source_archive,
                            archive_sha256=_sha256_file(source_archive),
                        )
                except GuidedEvidenceError as exc:
                    if str(exc) != "GUIDED_EVIDENCE_MODEL_IDENTITY_MISMATCH":
                        raise
                    # A source checkout may temporarily contain a prior signed
                    # replay while a new semantic fixture revision awaits its
                    # full direct rerun. Keep unrelated professional workflows
                    # available, but never load or present the stale replay.
                    self.guided_evidence = None
                except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
                    raise ValueError(
                        "repository Ridgeway evidence resources are incomplete"
                    ) from exc
        self.registry = RuntimeContractRegistry(
            allowed_prefixes=("system.", "connectivity.", "observability.", "assistant.")
        )
        self._methods: dict[str, _Method] = {}
        self._register_methods()

    def shutdown(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop process-owned work while leaving durable analyses recoverable."""

        executor = self.analysis_executor
        if executor is None:
            return True
        shutdown = getattr(executor, "shutdown", None)
        if not callable(shutdown):
            return True
        return bool(shutdown(timeout_seconds=timeout_seconds))

    @property
    def methods(self) -> list[str]:
        return sorted(self._methods)

    def _register(
        self,
        name: str,
        request: type[PlatformContractModel],
        handler: Callable[[Any], PlatformContractModel],
        response: type[PlatformContractModel] | None = None,
    ) -> None:
        response = response or response_contract_for(name)
        self.registry.register_method(name, request, response)
        self._methods[name] = _Method(request, response, handler)

    def _register_methods(self) -> None:
        self._register("system.ping", EmptyParams, self._system_ping, PingResult)
        self._register("connectivity.bundle.inspect", BundleInspectParams, self._bundle_inspect)
        self._register("connectivity.bundle.import", BundleImportParams, self._bundle_import)
        self._register("connectivity.project.get_state", ProjectParams, self._project_get_state)
        self._register("connectivity.planning.create", PlanningCreateParams, self._planning_create)
        self._register("connectivity.asset.inspect", AssetInspectParams, self._asset_inspect)
        self._register("connectivity.asset.add", AssetAddParams, self._asset_add)
        self._register(
            "connectivity.asset.download.preview",
            AssetDownloadPreviewParams,
            self._asset_download_preview,
        )
        self._register("connectivity.asset.download", AssetDownloadParams, self._asset_download)
        self._register("connectivity.grid.preview", GridPreviewParams, self._grid_preview)
        self._register("connectivity.grid.approve", GridApproveParams, self._grid_approve)
        self._register(
            "connectivity.digitization.preview",
            DigitizationPreviewParams,
            self._digitization_preview,
        )
        self._register(
            "connectivity.digitization.publish",
            DigitizationPublishParams,
            self._digitization_publish,
        )
        self._register("connectivity.hypothesis.save", HypothesisSaveParams, self._hypothesis_save)
        self._register("connectivity.focal_areas.save", FocalAreasSaveParams, self._focal_save)
        self._register(
            "connectivity.intervention.preview",
            InterventionPreviewParams,
            self._intervention_preview,
        )
        self._register(
            "connectivity.intervention.save", InterventionSaveParams, self._intervention_save
        )
        self._register("connectivity.scenario.save", ScenarioSaveParams, self._scenario_save)
        self._register("connectivity.safeguard.save", SafeguardSaveParams, self._safeguard_save)
        self._register(
            "connectivity.review_mask.save", ReviewMaskSaveParams, self._review_mask_save
        )
        self._register(
            "connectivity.decision_rule.save", DecisionRuleSaveParams, self._decision_rule_save
        )
        self._register(
            "connectivity.decision_brief.save",
            DecisionBriefSaveParams,
            self._decision_brief_save,
        )
        self._register("connectivity.cost_basis.get", ProjectParams, self._cost_basis_get)
        self._register("connectivity.cost_basis.save", CostBasisSaveParams, self._cost_basis_save)
        self._register(
            "connectivity.resource_policy.save",
            ResourcePolicySaveParams,
            self._resource_policy_save,
        )
        self._register("connectivity.model.validate", ProjectParams, self._model_validate)
        self._register("connectivity.model.attest", ModelAttestParams, self._model_attest)
        self._register(
            "connectivity.analysis.prepare", AnalysisPrepareParams, self._analysis_prepare
        )
        self._register("connectivity.analysis.plan", AnalysisPlanParams, self._analysis_plan)
        self._register("connectivity.analysis.start", ProjectAnalysisParams, self._analysis_start)
        self._register("connectivity.analysis.status", ProjectAnalysisParams, self._analysis_status)
        self._register("connectivity.analysis.pause", ProjectAnalysisParams, self._analysis_pause)
        self._register("connectivity.analysis.resume", ProjectAnalysisParams, self._analysis_resume)
        self._register("connectivity.analysis.cancel", ProjectAnalysisParams, self._analysis_cancel)
        self._register("connectivity.reference.plan", ReferencePlanParams, self._reference_plan)
        self._register(
            "connectivity.reference.start", ReferenceAnalysisParams, self._reference_start
        )
        self._register(
            "connectivity.reference.status", ReferenceAnalysisParams, self._reference_status
        )
        self._register(
            "connectivity.reference.result", ReferenceAnalysisParams, self._reference_result
        )
        self._register(
            "connectivity.results.decision_board", DecisionBoardParams, self._decision_board
        )
        self._register(
            "connectivity.results.scenario_detail",
            ScenarioDetailParams,
            self._scenario_detail,
        )
        self._register(
            "connectivity.evidence.resolve", EvidenceResolveParams, self._evidence_resolve
        )
        self._register("connectivity.export.validate", ExportValidateParams, self._export_validate)
        self._register("connectivity.export.create", ExportCreateParams, self._export_create)
        self._register("connectivity.guided.open", GuidedOpenParams, self._guided_open)
        self._register("connectivity.guided.rerun", GuidedSessionParams, self._guided_rerun)
        self._register("connectivity.guided.status", GuidedSessionParams, self._guided_status)
        self._register("connectivity.guided.pause", GuidedSessionParams, self._guided_pause)
        self._register("connectivity.guided.resume", GuidedSessionParams, self._guided_resume)
        self._register("connectivity.guided.cancel", GuidedSessionParams, self._guided_cancel)
        self._register(
            "connectivity.guided.checkpoint",
            GuidedCheckpointParams,
            self._guided_checkpoint,
        )
        self._register(
            "observability.preflight", ObservabilityPreflightParams, self._observability_preflight
        )
        self._register(
            "observability.connect", ObservabilityConnectParams, self._observability_connect
        )
        self._register(
            "observability.validate_binding", ProjectParams, self._observability_validate
        )
        self._register("observability.get_status", ProjectParams, self._observability_status)
        self._register(
            "observability.preview_upload", ObservabilityPreviewParams, self._observability_preview
        )
        self._register("observability.sync", ObservabilitySyncParams, self._observability_sync)
        self._register("observability.retry", ProjectParams, self._observability_retry)
        self._register("observability.open_run", ObservabilityLinkParams, self._open_run)
        self._register("observability.open_artifact", ObservabilityLinkParams, self._open_artifact)
        self._register("observability.open_trace", ObservabilityLinkParams, self._open_trace)
        self._register("assistant.explain", AssistantExplainParams, self._assistant_explain)
        self._register("assistant.follow_up", AssistantExplainParams, self._assistant_follow_up)
        self._register("assistant.configure", AssistantConfigureParams, self._assistant_configure)
        self._register("assistant.status", ProjectParams, self._assistant_status)
        self._register("assistant.clear", ProjectParams, self._assistant_clear)
        self._register(
            "assistant.intake.extract",
            AssistantIntakeExtractParams,
            self._assistant_intake_extract,
        )
        self._register(
            "assistant.intake.get", AssistantIntakeDraftParams, self._assistant_intake_get
        )
        self._register(
            "assistant.intake.confirm",
            AssistantIntakeConfirmParams,
            self._assistant_intake_confirm,
        )
        self._register("assistant.feedback", AssistantFeedbackParams, self._assistant_feedback)
        self._register("assistant.get_trace", AssistantTraceParams, self._assistant_trace)

    def _result(self, method: str, data: dict[str, Any]) -> PayloadResult:
        try:
            data_contract = RPC_RESPONSE_DATA_CONTRACTS[method]
            response_contract = self._methods[method].response
        except KeyError as exc:
            raise ValueError(f"unregistered RPC result method: {method}") from exc
        validated_data = data_contract.model_validate(data)
        hash_payload = (
            validated_data.model_dump(mode="json")
            if hasattr(validated_data, "model_dump")
            else validated_data
        )
        return response_contract(
            method=method,
            data=validated_data,
            data_sha256=hashlib.sha256(canonical_json(hash_payload).encode("utf-8")).hexdigest(),
        )

    def _allowed_path(self, value: str | Path, *, must_exist: bool, purpose: str) -> Path:
        path = Path(value).expanduser()
        candidate = path.resolve(strict=must_exist)
        if not any(candidate == root or root in candidate.parents for root in self.allowed_roots):
            raise _fault(
                "PATH_OUTSIDE_ALLOWED_ROOT",
                f"The {purpose} path is outside the authorized application roots.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        current = candidate
        while current != current.parent:
            if current.exists() and current.is_symlink():
                raise _fault(
                    "SYMLINK_PATH_REJECTED",
                    f"The {purpose} path traverses a symbolic link.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            if current in self.allowed_roots:
                break
            current = current.parent
        return candidate

    def _store(self, project_directory: str) -> EcoProjectStore:
        path = self._allowed_path(project_directory, must_exist=False, purpose="project")
        store = EcoProjectStore(path)
        try:
            store.recover_export_publications()
        except KeyError:
            # A newly created database has no project identity or publication
            # intents yet.  Initialization remains the owning RPC's concern.
            pass
        except BaseException:
            store.close()
            raise
        return store

    @staticmethod
    def _project_resource_policy(store: EcoProjectStore) -> ResourcePolicy:
        payload = store.latest_record("resource_policy")
        if payload is None:
            payload = store.project()["skeleton"].get("resource_policy")
        if payload is None:
            try:
                payload = store.effective_bundle().resource_policy.model_dump(mode="json")
            except (KeyError, ValidationError, ValueError):
                payload = None
        if payload is None:
            raise _fault(
                "RESOURCE_POLICY_REQUIRED",
                "Set an explicit project resource policy before adding or transforming rasters.",
                category=ErrorCategory.RESOURCE_LIMIT,
                severity=Severity.BLOCKING,
            )
        return ResourcePolicy.model_validate(payload)

    def _require_engine(self) -> tuple[Path, str, AnalysisExecutor]:
        if self.engine_executable is None or self.engine_sha256 is None:
            raise _fault(
                "PACKAGED_CIRCUITSCAPE_ENGINE_UNAVAILABLE",
                "The verified packaged Circuitscape engine is not installed.",
                category=ErrorCategory.ENGINE_FAILURE,
                severity=Severity.BLOCKING,
                remediation="Install a signed EcoCorridor release containing the pinned engine.",
            )
        if _sha256_file(self.engine_executable) != self.engine_sha256:
            raise _fault(
                "PACKAGED_CIRCUITSCAPE_ENGINE_HASH_CHANGED",
                "The packaged Circuitscape engine failed its integrity check.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        if self.engine_manifest_path is not None and (
            self.engine_manifest_file_sha256 is None
            or _sha256_file(self.engine_manifest_path) != self.engine_manifest_file_sha256
        ):
            raise _fault(
                "PACKAGED_ENGINE_MANIFEST_HASH_CHANGED",
                "The packaged EngineManifest failed its integrity check.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        if self.analysis_executor is None:
            raise _fault(
                "DIRECT_ANALYSIS_EXECUTOR_UNAVAILABLE",
                "The durable direct-analysis executor is unavailable.",
                category=ErrorCategory.ENGINE_FAILURE,
                severity=Severity.BLOCKING,
            )
        return self.engine_executable, self.engine_sha256, self.analysis_executor

    def _require_plan_engine_manifest(self, plan: ConnectivityRunPlan) -> None:
        if (
            self.engine_manifest is not None
            and plan.engine_manifest_sha256 != self.engine_manifest.sha256
        ):
            raise _fault(
                "ANALYSIS_ENGINE_MANIFEST_MISMATCH",
                "The run plan references a different pinned Circuitscape engine manifest.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Regenerate the run plan against the installed signed release.",
            )

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            _, parsed = self.registry.validate_request(
                {"jsonrpc": "2.0", "id": "direct", "method": method, "params": params}
            )
            validated = self._invoke(method, parsed)
            return validated.model_dump(mode="json")
        except RPCFault:
            raise
        except RpcProtocolError as exc:
            raise _fault(
                "RPC_CONTRACT_REJECTED",
                "The worker rejected an unsupported method or invalid request contract.",
                category=ErrorCategory.USER_FIXABLE,
                technical_message=exc.message,
                rpc_code=exc.code,
            ) from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise _fault(
                "REQUEST_VALIDATION_FAILED",
                "The request did not satisfy the EcoCorridor contract.",
                category=ErrorCategory.USER_FIXABLE,
                technical_message=str(exc),
            ) from exc
        except KeyError as exc:
            raise _fault(
                "AUTHORITATIVE_RECORD_NOT_FOUND",
                "The requested project evidence was not found.",
                remediation="Return to the preceding workflow step and create the missing record.",
                object_reference=str(exc),
            ) from exc
        except PermissionError as exc:
            raise _fault(
                "PROJECT_POLICY_BLOCKED",
                "Project evidence or policy blocks this operation.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=str(exc),
            ) from exc
        except OSError as exc:
            raise _fault(
                "PROJECT_IO_FAILURE",
                "The worker could not read or write an authorized project file.",
                category=ErrorCategory.INTERNAL,
                technical_message=type(exc).__name__,
                retryable=True,
            ) from exc

    def _invoke(self, method: str, params: PlatformContractModel) -> PlatformContractModel:
        """Invoke one registered handler and convert domain failures to typed faults."""

        try:
            result = self._methods[method].handler(params)
            if isinstance(result, PayloadResult) and result.method != method:
                raise ValueError("RPC handler returned a result for a different method")
            return self.registry.validate_response(method, result)
        except RPCFault:
            raise
        except RpcProtocolError:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            raise _fault(
                "REQUEST_VALIDATION_FAILED",
                "The request did not satisfy the EcoCorridor contract.",
                category=ErrorCategory.USER_FIXABLE,
                technical_message=str(exc),
            ) from exc
        except KeyError as exc:
            raise _fault(
                "AUTHORITATIVE_RECORD_NOT_FOUND",
                "The requested project evidence was not found.",
                remediation="Return to the preceding workflow step and create the missing record.",
                object_reference=str(exc),
            ) from exc
        except PermissionError as exc:
            raise _fault(
                "PROJECT_POLICY_BLOCKED",
                "Project evidence or policy blocks this operation.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=str(exc),
            ) from exc
        except OSError as exc:
            raise _fault(
                "PROJECT_IO_FAILURE",
                "The worker could not read or write an authorized project file.",
                category=ErrorCategory.INTERNAL,
                technical_message=type(exc).__name__,
                retryable=True,
            ) from exc

    def handle_frame(self, frame: bytes | str) -> bytes:
        request_id: str | int | None = None
        try:
            request, params = self.registry.validate_request(frame)
            request_id = request.id
            validated = self._invoke(request.method, params)
            return encode_jsonl(
                JsonRpcSuccessFrame(
                    id=request.id,
                    result=validated.model_dump(mode="json"),
                )
            )
        except RPCFault as exc:
            return encode_jsonl(
                JsonRpcErrorFrame(
                    id=request_id,
                    error=JsonRpcErrorBody(
                        code=exc.code,
                        message=exc.error.user_message,
                        data=exc.error,
                    ),
                )
            )
        except RpcProtocolError as exc:
            error = _fault(
                "RPC_PROTOCOL_ERROR",
                "The worker rejected the JSON-RPC frame.",
                technical_message=exc.message,
                rpc_code=exc.code,
            )
            return encode_jsonl(
                JsonRpcErrorFrame(
                    id=exc.request_id,
                    error=JsonRpcErrorBody(
                        code=error.code,
                        message=error.error.user_message,
                        data=error.error,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - process boundary must return typed JSON-RPC
            error = _fault(
                "INTERNAL_WORKER_ERROR",
                "The scientific worker encountered an internal error.",
                category=ErrorCategory.INTERNAL,
                technical_message=type(exc).__name__,
            )
            return encode_jsonl(
                JsonRpcErrorFrame(
                    id=request_id,
                    error=JsonRpcErrorBody(
                        code=error.code,
                        message=error.error.user_message,
                        data=error.error,
                    ),
                )
            )

    def _system_ping(self, _: EmptyParams) -> PingResult:
        return PingResult(
            engine_configured=self.engine_executable is not None,
            methods=self.methods,
        )

    def _inspect_bundle(
        self, path: Path, expected_sha256: str | None
    ) -> tuple[ConnectivityProjectBundle, list[ArchiveMember], str]:
        digest = _sha256_file(path)
        if expected_sha256 is not None and digest != require_sha256(expected_sha256.casefold()):
            raise _fault(
                "BUNDLE_HASH_MISMATCH",
                "The selected bundle does not match its expected checksum.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        members = inspect_zip(path)
        names = {member.path: member for member in members}
        manifest_name = "connectivity-project.json"
        if manifest_name not in names:
            raise _fault(
                "BUNDLE_MANIFEST_MISSING",
                "The bundle does not contain connectivity-project.json.",
                category=ErrorCategory.MODEL_QUALITY,
                remediation="Export a complete portable EcoCorridor project bundle.",
            )
        if names[manifest_name].uncompressed_bytes > 8 * 1024 * 1024:
            raise _fault(
                "BUNDLE_MANIFEST_TOO_LARGE",
                "The EcoCorridor project manifest exceeds the supported contract size.",
                category=ErrorCategory.RESOURCE_LIMIT,
                severity=Severity.BLOCKING,
            )
        try:
            raw = json.loads(read_zip_member(path, manifest_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fault(
                "BUNDLE_MANIFEST_INVALID",
                "The project manifest is not a valid EcoCorridor v1.0 contract.",
                category=ErrorCategory.MODEL_QUALITY,
                technical_message=str(exc),
            ) from exc
        unsupported = self._known_unsupported_circuitscape_feature(raw)
        if unsupported is not None:
            location, feature = unsupported
            raise _fault(
                "UNSUPPORTED_FEATURE",
                "This project requests a Circuitscape mode that EcoCorridor v0.1 does not run.",
                category=ErrorCategory.UNSUPPORTED_FEATURE,
                severity=Severity.BLOCKING,
                object_reference=location,
                remediation=(
                    "Provide a raster pairwise model using resistance semantics. Network, "
                    "advanced, one-to-all, all-to-one, and conductance projects remain outside "
                    "v0.1."
                ),
                technical_message=feature,
            )
        try:
            bundle = ConnectivityProjectBundle.model_validate(raw)
        except ValidationError as exc:
            raise _fault(
                "BUNDLE_MANIFEST_INVALID",
                "The project manifest is not a valid EcoCorridor v1.0 contract.",
                category=ErrorCategory.MODEL_QUALITY,
                technical_message=str(exc),
            ) from exc
        for asset in bundle.assets:
            member = names.get(asset.relative_path)
            if member is None:
                raise _fault(
                    "BUNDLE_ASSET_MISSING",
                    f"Bundle asset {asset.asset_id} is missing.",
                    category=ErrorCategory.MODEL_QUALITY,
                    object_reference=asset.asset_id,
                )
            if member.sha256 != asset.sha256 or member.uncompressed_bytes != asset.byte_size:
                raise _fault(
                    "BUNDLE_ASSET_INTEGRITY_FAILED",
                    f"Bundle asset {asset.asset_id} failed checksum or byte-count validation.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=asset.asset_id,
                )
        return bundle, members, digest

    @staticmethod
    def _known_unsupported_circuitscape_feature(
        raw: object,
    ) -> tuple[str, str] | None:
        """Classify only known unsupported settings; malformed unknowns stay invalid.

        The public Eco contract uses ``engine_mode`` and ``raster_semantics``.
        The additional names below are the corresponding Circuitscape settings
        accepted only for the purpose of returning an actionable transport
        error when a portable manifest includes an unconverted engine section.
        """

        if not isinstance(raw, dict):
            return None
        containers: list[tuple[str, dict[str, Any]]] = [("manifest", raw)]
        scenarios = raw.get("scenarios")
        if isinstance(scenarios, list):
            containers.extend(
                (f"scenarios[{index}]", item)
                for index, item in enumerate(scenarios)
                if isinstance(item, dict)
            )
        for key in ("circuitscape", "circuitscape_configuration", "engine_settings"):
            item = raw.get(key)
            if isinstance(item, dict):
                containers.append((key, item))

        unsupported_modes = {
            "RASTER_ADVANCED": "raster advanced mode",
            "NETWORK_PAIRWISE": "network pairwise mode",
            "ONE_TO_ALL": "one-to-all mode",
            "ALL_TO_ONE": "all-to-one mode",
        }
        unsupported_scenarios = {
            "ADVANCED": "raster advanced mode",
            "ONE_TO_ALL": "one-to-all mode",
            "ALL_TO_ONE": "all-to-one mode",
        }
        for location, item in containers:
            mode = item.get("engine_mode")
            if isinstance(mode, str):
                normalized = mode.strip().upper().replace("-", "_").replace(" ", "_")
                if normalized in unsupported_modes:
                    return f"{location}.engine_mode", unsupported_modes[normalized]
            semantics = item.get("raster_semantics")
            if isinstance(semantics, str) and semantics.strip().upper() == "CONDUCTANCE":
                return f"{location}.raster_semantics", "conductance semantics"
            data_type = item.get("data_type")
            if isinstance(data_type, str) and data_type.strip().casefold() == "network":
                return f"{location}.data_type", "network data type"
            scenario = item.get("scenario")
            if isinstance(scenario, str):
                normalized = scenario.strip().upper().replace("-", "_").replace(" ", "_")
                if normalized in unsupported_scenarios:
                    return f"{location}.scenario", unsupported_scenarios[normalized]
            resistance_flag = item.get("habitat_map_is_resistances")
            if resistance_flag is False or (
                isinstance(resistance_flag, str) and resistance_flag.strip().casefold() == "false"
            ):
                return (
                    f"{location}.habitat_map_is_resistances",
                    "conductance semantics",
                )
        return None

    def _bundle_inspect(self, params: BundleInspectParams) -> PayloadResult:
        path = self._allowed_path(params.bundle_path, must_exist=True, purpose="bundle")
        try:
            bundle, members, digest = self._inspect_bundle(path, params.expected_sha256)
        except ArchiveSecurityError as exc:
            raise _fault(
                "BUNDLE_SECURITY_REJECTED",
                "The bundle failed secure archive inspection.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=str(exc),
            ) from exc
        return self._result(
            "connectivity.bundle.inspect",
            {
                "project_id": bundle.project_id,
                "title": bundle.title,
                "archive_sha256": digest,
                "file_count": len(members),
                "uncompressed_bytes": sum(item.uncompressed_bytes for item in members),
                "asset_count": len(bundle.assets),
                "scenario_count": len(bundle.scenarios),
                "bundle": bundle.model_dump(mode="json"),
            },
        )

    def _bundle_import(self, params: BundleImportParams) -> PayloadResult:
        path = self._allowed_path(params.bundle_path, must_exist=True, purpose="bundle")
        project_directory = self._allowed_path(
            params.project_directory, must_exist=False, purpose="project"
        )
        try:
            bundle, members, digest = self._inspect_bundle(path, params.expected_sha256)
        except ArchiveSecurityError as exc:
            raise _fault(
                "BUNDLE_SECURITY_REJECTED",
                "The bundle failed secure archive inspection.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=str(exc),
            ) from exc
        project_directory.mkdir(parents=True, exist_ok=True)
        try:
            target = contained_path(project_directory, Path("sources") / digest)
        except PathSecurityError as exc:
            raise _fault(
                "PROJECT_SOURCE_PATH_UNSAFE",
                "The project source directory contains an unsafe path or symbolic link.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            ) from exc
        source_root = target.parent
        if not target.exists():
            source_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".import.", dir=source_root))
            try:
                extract_zip(path, temporary)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        for member in members:
            try:
                materialized = (target / member.path).resolve(strict=True)
            except OSError as exc:
                raise _fault(
                    "MATERIALIZED_BUNDLE_INTEGRITY_FAILED",
                    "A materialized bundle file is missing after import.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=member.path,
                ) from exc
            if (
                target.resolve() not in materialized.parents
                or not materialized.is_file()
                or materialized.is_symlink()
                or materialized.stat().st_size != member.uncompressed_bytes
                or _sha256_file(materialized) != member.sha256
            ):
                raise _fault(
                    "MATERIALIZED_BUNDLE_INTEGRITY_FAILED",
                    "A materialized bundle file differs from the inspected archive.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=member.path,
                )
        store = EcoProjectStore(project_directory)
        try:
            store.initialize(
                project_id=bundle.project_id,
                title=bundle.title,
                mode="IMPORTED",
                skeleton={"source_bundle_sha256": digest},
            )
            store.store_bundle(
                bundle,
                import_manifest_id=digest,
                import_manifest={
                    "archive_sha256": digest,
                    "members": [asdict(item) for item in members],
                    "materialized_directory": f"sources/{digest}",
                },
            )
        finally:
            store.close()
        return self._result(
            "connectivity.bundle.import",
            {
                "project_id": bundle.project_id,
                "archive_sha256": digest,
                "state": "IMPORTED",
            },
        )

    def _planning_create(self, params: PlanningCreateParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            skeleton = {
                "revision": params.revision.model_dump(mode="json"),
                "decision_brief": params.decision_brief.model_dump(mode="json"),
                "resource_policy": params.resource_policy.model_dump(mode="json"),
                "scenarios": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in params.scenarios
                ],
                "safeguards": [item.model_dump(mode="json") for item in params.safeguards],
                "review_masks": [item.model_dump(mode="json") for item in params.review_masks],
                "decision_rules": [item.model_dump(mode="json") for item in params.decision_rules],
            }
            store.initialize(
                project_id=params.project_id,
                title=params.title,
                mode="PLANNING",
                skeleton=skeleton,
            )
            store.put_record(
                "revision",
                params.revision.revision_id,
                params.revision.model_dump(mode="json"),
                allow_update=False,
            )
            store.put_record(
                "decision_brief",
                params.decision_brief.brief_id,
                params.decision_brief.model_dump(mode="json"),
            )
            store.put_record(
                "fitness",
                params.fitness.passport_id,
                params.fitness.model_dump(mode="json"),
            )
            store.put_record(
                "resource_policy",
                "current",
                params.resource_policy.model_dump(mode="json"),
            )
        finally:
            store.close()
        return self._result(
            "connectivity.planning.create",
            {
                "project_id": params.project_id,
                "state": "PLANNING_DRAFT",
                "next_action": "Add source assets and reviewed ecological model records.",
            },
        )

    def _project_get_state(self, params: ProjectParams) -> PayloadResult:
        """Return one authoritative, read-only snapshot for the professional UI."""

        store = self._store(params.project_directory)
        try:
            try:
                recover_pending_reference_results(store=store)
            except (OSError, ReferenceWorkflowError, ValueError) as exc:
                raise _fault(
                    "REFERENCE_RECOVERY_FAILED",
                    "A completed reference analysis could not recover its canonical result.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    remediation="Restore the retained direct evidence and reopen the project.",
                    technical_message=type(exc).__name__,
                ) from exc
            project = store.project()
            bundle = store.effective_bundle()
            record_types = (
                "asset",
                "hypothesis",
                "focal_area_set",
                "scenario",
                "intervention_site",
                "intervention_option",
                "safeguard",
                "review_mask",
                "decision_rule",
                "cost_basis",
                "grid_alignment_plan",
                "grid_alignment_approval",
                "semantic_diff",
                "revision",
            )
            saved_records = {
                record_type: store.list_records(record_type) for record_type in record_types
            }
            analyses = []
            for row in store._connection.execute(
                "SELECT analysis.analysis_id,analysis.analysis_kind,analysis.plan_json,"
                "analysis.state,analysis.created_at,analysis.updated_at,"
                "reference.plan_id AS reference_plan_id "
                "FROM eco_analyses AS analysis "
                "LEFT JOIN eco_reference_comparison_plans AS reference "
                "ON reference.analysis_id=analysis.analysis_id "
                "ORDER BY analysis.created_at,analysis.analysis_id"
            ):
                run_plan = ConnectivityRunPlan.model_validate_json(str(row["plan_json"]))
                analyses.append(
                    {
                        "analysis_id": str(row["analysis_id"]),
                        "analysis_kind": str(row["analysis_kind"]),
                        "plan_id": (
                            str(row["reference_plan_id"])
                            if row["reference_plan_id"] is not None
                            else None
                        ),
                        "revision_id": run_plan.revision_id,
                        "engine_manifest_sha256": run_plan.engine_manifest_sha256,
                        "state": str(row["state"]),
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                    }
                )
            binding = store.observability()
            latest_validation = store.latest_record("validation")
            validation = latest_validation or validation_projection(bundle)
            process_owned_ids = {
                str(item["analysis_id"])
                for item in analyses
                if item["state"] in {AnalysisState.RUNNING.value, AnalysisState.PAUSING.value}
            }
            live_analysis_ids: set[str] = set()
            active_probe = getattr(self.analysis_executor, "is_active", None)
            if callable(active_probe):
                live_analysis_ids = {
                    analysis_id
                    for analysis_id in process_owned_ids
                    if active_probe(project_directory=store.root, analysis_id=analysis_id)
                }
            pending_recovery = store.pending_recovery_state(live_analysis_ids=live_analysis_ids)
        finally:
            store.close()
        return self._result(
            "connectivity.project.get_state",
            {
                "project": {
                    "project_id": project["project_id"],
                    "title": project["title"],
                    "mode": project["mode"],
                },
                "active_revision": bundle.revision.model_dump(mode="json"),
                "effective_bundle": bundle.model_dump(mode="json"),
                "fitness": bundle.fitness.model_dump(mode="json"),
                "resource_policy": bundle.resource_policy.model_dump(mode="json"),
                "saved_records": saved_records,
                "analyses": analyses,
                "pending_recovery": pending_recovery,
                "sync_status": binding
                or {
                    "state": "AUTHENTICATION_REQUIRED",
                    "profile": APP_PROFILE.namespace,
                },
                "validation": validation,
                "engine": {
                    "available": self.engine_executable is not None
                    and self.engine_manifest is not None
                    and self.analysis_executor is not None,
                    "manifest_sha256": (
                        self.engine_manifest.sha256 if self.engine_manifest is not None else None
                    ),
                    "engine_name": (
                        self.engine_manifest.engine_name
                        if self.engine_manifest is not None
                        else None
                    ),
                    "circuitscape_version": (
                        self.engine_manifest.circuitscape_version
                        if self.engine_manifest is not None
                        else None
                    ),
                },
            },
        )

    def _asset_inspect(self, params: AssetInspectParams) -> PayloadResult:
        source = self._allowed_path(params.source_path, must_exist=True, purpose="source asset")
        store = self._store(params.project_directory)
        try:
            policy = self._project_resource_policy(store)
            if source.stat().st_size > policy.max_input_bytes:
                raise _fault(
                    "ASSET_INSPECTION_RESOURCE_POLICY_BLOCKED",
                    "The staged raster exceeds the reviewed project input policy.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                )
            try:
                inspection = inspect_raster(
                    source,
                    grid_id=params.grid_id,
                    value_kind=params.value_kind,
                    format_hint=params.format,
                    esri_ascii_crs=params.esri_ascii_crs,
                    esri_ascii_cell_area_m2=params.esri_ascii_cell_area_m2,
                    maximum_bytes=policy.max_input_bytes,
                    maximum_cells=max(1, policy.max_materialized_bytes // 8),
                )
            except RasterIOError as exc:
                raise _fault(
                    "RASTER_INSPECTION_FAILED",
                    "The staged raster could not be accepted without transformation.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation=str(exc),
                ) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.asset.inspect",
            {
                "inspection": inspection.model_dump(),
                "transformed": False,
                "next_action": "Review the exact grid and create the authorized asset record.",
            },
        )

    def _asset_add(self, params: AssetAddParams) -> PayloadResult:
        if params.asset.authorization.asset_id != params.asset.asset_id:
            raise _fault(
                "ASSET_AUTHORIZATION_ID_MISMATCH",
                "The asset authorization belongs to different evidence bytes.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        if params.asset.authorization.license_spdx_or_name != params.asset.license:
            raise _fault(
                "ASSET_LICENSE_AUTHORIZATION_MISMATCH",
                "The source license and reviewed authorization do not match.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        source = self._allowed_path(params.source_path, must_exist=True, purpose="source asset")
        if not source.is_file() or source.is_symlink():
            raise _fault(
                "ASSET_SOURCE_INVALID",
                "The selected asset source is not a regular file.",
                category=ErrorCategory.SECURITY,
            )
        if (
            _sha256_file(source) != params.asset.sha256
            or source.stat().st_size != params.asset.byte_size
        ):
            raise _fault(
                "ASSET_INTEGRITY_MISMATCH",
                "The selected asset bytes differ from the reviewed asset contract.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        store = self._store(params.project_directory)
        created_destination: Path | None = None
        try:
            policy = self._project_resource_policy(store)
            existing_assets = {item["asset_id"]: item for item in store.list_records("asset")}
            retained_size = sum(
                int(item["byte_size"])
                for asset_id, item in existing_assets.items()
                if asset_id != params.asset.asset_id
            )
            if retained_size + params.asset.byte_size > policy.max_input_bytes:
                raise _fault(
                    "ASSET_RESOURCE_POLICY_BLOCKED",
                    "The retained project inputs would exceed the reviewed resource policy.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                )
            destination = store.root / params.asset.relative_path
            if store.root not in destination.resolve(strict=False).parents:
                raise _fault(
                    "ASSET_PATH_OUTSIDE_PROJECT",
                    "The reviewed asset path escapes the project.",
                    category=ErrorCategory.SECURITY,
                )
            if destination.exists() and _sha256_file(destination) != params.asset.sha256:
                raise _fault(
                    "ASSET_DESTINATION_CONFLICT",
                    "A different asset already occupies the reviewed project path.",
                    category=ErrorCategory.SECURITY,
                )
            inspection: dict[str, Any] | None = None
            if params.asset.kind in {
                AssetKind.RESISTANCE_RASTER,
                AssetKind.FOCAL_RASTER,
                AssetKind.REVIEW_MASK,
            }:
                if params.asset.grid is None:
                    raise _fault(
                        "RASTER_GRID_CONTRACT_MISSING",
                        "A modeled raster requires the reviewed grid returned by asset inspection.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                    )
                try:
                    inspection = inspect_reviewed_raster(source, params.asset).model_dump()
                except RasterIOError as exc:
                    raise _fault(
                        "RASTER_CONTRACT_MISMATCH",
                        "The staged raster differs from its reviewed grid or value semantics.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        remediation=str(exc),
                    ) from exc
            if not destination.exists():
                _atomic_copy_file(source, destination)
                created_destination = destination
            publication = store.publish_semantic_change(
                [("asset", params.asset.asset_id, params.asset.model_dump(mode="json"))],
                created_by=params.asset.authorization.reviewer,
                created_at=params.asset.authorization.reviewed_at,
                reason="Retain a reviewed project asset and its cloud/export authorization.",
            )
            revision = int(publication["record_revisions"][f"asset:{params.asset.asset_id}"])
            created_destination = None
        except BaseException:
            if created_destination is not None:
                created_destination.unlink(missing_ok=True)
            raise
        finally:
            store.close()
        return self._result(
            "connectivity.asset.add",
            {
                "asset_id": params.asset.asset_id,
                "revision": revision,
                "sha256": params.asset.sha256,
                "inspection": inspection,
            },
        )

    def _asset_download_preview(self, params: AssetDownloadPreviewParams) -> PayloadResult:
        """Inspect an allowlisted provider request without downloading or persisting bytes."""

        try:
            preview = preview_download(
                provider_id=params.provider_id,
                source_url=params.source_url,
                extent_description=params.extent_description,
                expected_bytes=params.expected_bytes,
                selected_vintage=params.selected_vintage,
                selected_resolution=params.selected_resolution,
            )
        except ValueError as exc:
            raise _fault(
                "DATA_PROVIDER_PREVIEW_REJECTED",
                "The proposed contextual-data request is not on the approved provider allowlist.",
                category=ErrorCategory.DATA_PROVIDER,
                severity=Severity.BLOCKING,
                remediation=str(exc),
            ) from exc
        store = self._store(params.project_directory)
        try:
            policy = self._project_resource_policy(store)
            retained_bytes = sum(int(item["byte_size"]) for item in store.list_records("asset"))
            remaining_bytes = max(0, policy.max_input_bytes - retained_bytes)
        finally:
            store.close()
        return self._result(
            "connectivity.asset.download.preview",
            {
                **asdict(preview),
                "remaining_project_input_bytes": remaining_bytes,
                "resource_policy_allowed": preview.expected_bytes <= remaining_bytes,
            },
        )

    def _asset_download(self, params: AssetDownloadParams) -> PayloadResult:
        if params.authorization.asset_id != params.asset_id:
            raise _fault(
                "ASSET_AUTHORIZATION_ID_MISMATCH",
                "The asset authorization belongs to a different asset.",
                category=ErrorCategory.SECURITY,
            )
        preview = preview_download(
            provider_id=params.provider_id,
            source_url=params.source_url,
            extent_description=params.extent_description,
            expected_bytes=params.expected_bytes,
            selected_vintage=params.selected_vintage,
            selected_resolution=params.selected_resolution,
        )
        if params.authorization.license_spdx_or_name != preview.license:
            raise _fault(
                "ASSET_LICENSE_AUTHORIZATION_MISMATCH",
                "The provider license differs from the reviewed asset authorization.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        store = self._store(params.project_directory)
        receipt = None
        try:
            policy = self._project_resource_policy(store)
            retained_size = sum(int(item["byte_size"]) for item in store.list_records("asset"))
            remaining_bytes = policy.max_input_bytes - retained_size
            if params.expected_bytes > remaining_bytes:
                raise _fault(
                    "ASSET_DOWNLOAD_RESOURCE_POLICY_BLOCKED",
                    "The proposed download would exceed the reviewed project input policy.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                )
            receipt = download_approved_asset(
                preview=preview,
                asset_id=params.asset_id,
                cache_root=store.root / "data_assets",
                explicit_consent=params.explicit_consent,
                maximum_bytes=min(params.maximum_bytes, remaining_bytes),
                opener=self.download_opener,
            )
            asset = ConnectivityAsset(
                asset_id=params.asset_id,
                kind=params.kind,
                title=params.title,
                relative_path=f"data_assets/{receipt.relative_path}",
                sha256=receipt.sha256,
                byte_size=receipt.bytes,
                media_type=params.media_type,
                source=receipt.source_url,
                vintage=preview.vintage,
                license=preview.license,
                authorization=params.authorization,
            )
            store.publish_semantic_change(
                [("asset", asset.asset_id, asset.model_dump(mode="json"))],
                created_by=params.authorization.reviewer,
                created_at=params.authorization.reviewed_at,
                reason="Retain an explicitly authorized contextual provider asset.",
                extra_records=[
                    (
                        "provider_receipt",
                        asset.asset_id,
                        {
                            **asdict(receipt),
                            "preview": asdict(preview),
                            "explicit_consent": True,
                        },
                        False,
                    )
                ],
            )
        except BaseException:
            if receipt is not None and receipt.created:
                retained = contained_path(store.root / "data_assets", receipt.relative_path)
                retained.unlink(missing_ok=True)
            raise
        finally:
            store.close()
        return self._result(
            "connectivity.asset.download",
            {"asset": asset.model_dump(mode="json"), "context_only": True},
        )

    def _grid_preview(self, params: GridPreviewParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            policy = self._project_resource_policy(store)
            if params.target_grid.width * params.target_grid.height * 8 > (
                policy.max_materialized_bytes
            ):
                raise _fault(
                    "GRID_ALIGNMENT_RESOURCE_POLICY_BLOCKED",
                    "The target grid exceeds the reviewed materialization policy.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                )
            try:
                asset = ConnectivityAsset.model_validate(
                    store.get_record("asset", params.source_asset_id)
                )
            except KeyError:
                asset = next(
                    (
                        item
                        for item in store.effective_bundle().assets
                        if item.asset_id == params.source_asset_id
                    ),
                    None,
                )
                if asset is None:
                    raise _fault(
                        "ALIGNMENT_SOURCE_ASSET_MISSING",
                        "The alignment source is not a retained project asset.",
                        category=ErrorCategory.MODEL_QUALITY,
                        object_reference=params.source_asset_id,
                    )
            if asset.grid is None:
                raise _fault(
                    "ALIGNMENT_SOURCE_NOT_RASTER",
                    "Only a reviewed raster asset can be aligned.",
                    category=ErrorCategory.MODEL_QUALITY,
                    object_reference=params.source_asset_id,
                )
            try:
                raster = load_raster(store.resolve_asset_path(asset.relative_path), asset)
                plan = preview_grid_alignment(
                    raster, params.target_grid, params.method, plan_id=params.plan_id
                )
            except (RasterIOError, ValueError) as exc:
                raise _fault(
                    "GRID_ALIGNMENT_PREVIEW_FAILED",
                    "The alignment preview could not be produced without changing an unapproved assumption.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation=str(exc),
                ) from exc
            store.put_record(
                "grid_alignment_plan",
                plan.plan_id,
                plan.model_dump(mode="json"),
                allow_update=False,
            )
        finally:
            store.close()
        return self._result("connectivity.grid.preview", {"plan": plan.model_dump(mode="json")})

    def _grid_approve(self, params: GridApproveParams) -> PayloadResult:
        store = self._store(params.project_directory)
        created_path: Path | None = None
        try:
            plan = GridAlignmentPlan.model_validate(
                store.get_record("grid_alignment_plan", params.plan_id)
            )
            approval = approve_grid_alignment(
                plan,
                approval_id=params.approval_id,
                reviewer=params.reviewer,
                approved_at=params.approved_at,
                acknowledged_warnings=params.acknowledged_warnings,
                purpose=params.purpose,
            )
            try:
                existing_approval = store.get_record("grid_alignment_approval", params.approval_id)
            except KeyError:
                existing_approval = None
            if existing_approval is not None:
                try:
                    derived_asset = ConnectivityAsset.model_validate(
                        store.get_record("asset", params.derived_asset_id)
                    )
                    revision = ModelRevision.model_validate(
                        store.get_record("revision", params.revision_id)
                    )
                    if revision.diff_id is None:
                        raise KeyError("semantic diff")
                    semantic_diff = SemanticModelDiff.model_validate(
                        store.get_record("semantic_diff", revision.diff_id)
                    )
                    retained = store.resolve_asset_path(derived_asset.relative_path)
                except (KeyError, FileNotFoundError, ValueError) as exc:
                    raise _fault(
                        "ALIGNMENT_IDEMPOTENCY_CONFLICT",
                        "The prior alignment approval is incomplete or differs from this retry.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                    ) from exc
                if (
                    existing_approval != approval.model_dump(mode="json")
                    or derived_asset.title != params.derived_title
                    or derived_asset.relative_path != params.derived_relative_path
                    or derived_asset.authorization != params.derived_authorization
                    or revision.revision_id != params.revision_id
                    or semantic_diff.to_revision_id != params.revision_id
                    or _sha256_file(retained) != derived_asset.sha256
                    or retained.stat().st_size != derived_asset.byte_size
                ):
                    raise _fault(
                        "ALIGNMENT_IDEMPOTENCY_CONFLICT",
                        "The prior alignment approval differs from this retry.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                    )
                original_asset_id = derived_asset.source.split(":alignment:", 1)[0].removeprefix(
                    "derived:"
                )
                return self._result(
                    "connectivity.grid.approve",
                    {
                        "approval": existing_approval,
                        "derived_asset": derived_asset.model_dump(mode="json"),
                        "semantic_diff": semantic_diff.model_dump(mode="json"),
                        "revision": revision.model_dump(mode="json"),
                        "original_asset_retained": original_asset_id,
                    },
                )
            try:
                source_asset = ConnectivityAsset.model_validate(
                    store.get_record("asset", plan.source_asset_id)
                )
            except KeyError:
                source_asset = next(
                    (
                        item
                        for item in store.effective_bundle().assets
                        if item.asset_id == plan.source_asset_id
                    ),
                    None,
                )
            if source_asset is None or source_asset.grid is None:
                raise _fault(
                    "ALIGNMENT_SOURCE_ASSET_MISSING",
                    "The approved plan no longer resolves to its exact source raster.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                )
            try:
                source_raster = load_raster(
                    store.resolve_asset_path(source_asset.relative_path), source_asset
                )
                derived_raster = apply_approved_grid_alignment(
                    source_raster,
                    plan,
                    approval,
                    derived_asset_id=params.derived_asset_id,
                )
            except (RasterIOError, ValueError, PermissionError) as exc:
                raise _fault(
                    "GRID_ALIGNMENT_MATERIALIZATION_FAILED",
                    "The approved alignment no longer reproduces from the retained source bytes.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation=str(exc),
                ) from exc

            try:
                destination = contained_path(store.root, params.derived_relative_path)
            except PathSecurityError as exc:
                raise _fault(
                    "DERIVED_ASSET_PATH_OUTSIDE_PROJECT",
                    "The derived raster path escapes the project.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                ) from exc
            if destination.exists():
                raise _fault(
                    "DERIVED_ASSET_PATH_CONFLICT",
                    "A retained asset already occupies the requested derived path.",
                    category=ErrorCategory.SECURITY,
                )
            write_esri_ascii(destination, derived_raster)
            created_path = destination
            derived_asset = ConnectivityAsset(
                asset_id=params.derived_asset_id,
                kind=source_asset.kind,
                title=params.derived_title,
                relative_path=params.derived_relative_path,
                sha256=_sha256_file(destination),
                byte_size=destination.stat().st_size,
                media_type="application/x-esri-ascii-grid",
                source=f"derived:{source_asset.asset_id}:alignment:{plan.plan_id}",
                vintage=params.approved_at.date().isoformat(),
                license=source_asset.license,
                grid=plan.target_grid,
                authorization=params.derived_authorization,
            )
            current_revision_payload = store.latest_record("revision")
            current_revision = (
                ModelRevision.model_validate(current_revision_payload)
                if current_revision_payload is not None
                else store.effective_bundle().revision
            )
            if params.revision_id == current_revision.revision_id:
                raise _fault(
                    "MODEL_REVISION_ID_REUSED",
                    "The aligned model must receive a new semantic revision ID.",
                    category=ErrorCategory.MODEL_QUALITY,
                )
            changes = [
                SemanticChange(
                    object_type="ConnectivityAsset",
                    object_id=params.derived_asset_id,
                    field="derived_from_asset_id",
                    before=source_asset.asset_id,
                    after=params.derived_asset_id,
                    rationale=params.reason,
                ),
                SemanticChange(
                    object_type="RasterGrid",
                    object_id=plan.target_grid.grid_id,
                    field="alignment_method",
                    before=source_asset.grid.grid_id,
                    after=plan.method.value,
                    rationale=(
                        "Explicitly previewed and approved grid transformation; original bytes retained."
                    ),
                ),
            ]
            diff_identity = canonical_sha256(
                {
                    "from_revision_id": current_revision.revision_id,
                    "to_revision_id": params.revision_id,
                    "changes": [item.model_dump(mode="json") for item in changes],
                    "plan_sha256": approval.plan_sha256,
                    "derived_asset_sha256": derived_asset.sha256,
                }
            )
            diff_id = f"diff_{diff_identity[:20]}"
            semantic_diff = SemanticModelDiff(
                diff_id=diff_id,
                from_revision_id=current_revision.revision_id,
                to_revision_id=params.revision_id,
                changes=changes,
                sha256=canonical_sha256(
                    {
                        "diff_id": diff_id,
                        "from_revision_id": current_revision.revision_id,
                        "to_revision_id": params.revision_id,
                        "changes": [item.model_dump(mode="json") for item in changes],
                    }
                ),
            )
            revision = ModelRevision(
                revision_id=params.revision_id,
                parent_revision_id=current_revision.revision_id,
                project_id=current_revision.project_id,
                created_at=params.approved_at,
                created_by=params.reviewer,
                reason=params.reason,
                model_sha256=canonical_sha256(
                    {
                        "parent_model_sha256": current_revision.model_sha256,
                        "semantic_diff_sha256": semantic_diff.sha256,
                        "derived_asset_sha256": derived_asset.sha256,
                        "alignment_plan_sha256": approval.plan_sha256,
                    }
                ),
                diff_id=semantic_diff.diff_id,
            )
            store.store_alignment_revision(
                approval=approval.model_dump(mode="json"),
                derived_asset=derived_asset.model_dump(mode="json"),
                semantic_diff=semantic_diff.model_dump(mode="json"),
                revision=revision.model_dump(mode="json"),
            )
            created_path = None
        except BaseException:
            if created_path is not None:
                created_path.unlink(missing_ok=True)
            raise
        finally:
            store.close()
        return self._result(
            "connectivity.grid.approve",
            {
                "approval": approval.model_dump(mode="json"),
                "derived_asset": derived_asset.model_dump(mode="json"),
                "semantic_diff": semantic_diff.model_dump(mode="json"),
                "revision": revision.model_dump(mode="json"),
                "original_asset_retained": source_asset.asset_id,
            },
        )

    @staticmethod
    def _digitization_fault(error: DigitizationError) -> RPCFault:
        return _fault(
            error.code,
            str(error),
            category=ErrorCategory.MODEL_QUALITY,
            severity=Severity.BLOCKING,
            object_reference=error.object_reference,
        )

    def _digitization_preview(self, params: DigitizationPreviewParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            try:
                preview = preview_digitization(store, params.draft)
            except DigitizationError as exc:
                raise self._digitization_fault(exc) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.digitization.preview",
            {"preview": preview.model_dump(mode="json")},
        )

    def _digitization_publish(self, params: DigitizationPublishParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            try:
                publication = publish_digitization(store, params)
            except DigitizationError as exc:
                raise self._digitization_fault(exc) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.digitization.publish",
            publication.model_dump(mode="json"),
        )

    def _hypothesis_save(self, params: HypothesisSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "hypothesis",
            params.hypothesis.hypothesis_id,
            params.hypothesis,
            "connectivity.hypothesis.save",
        )

    def _focal_save(self, params: FocalAreasSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "focal_area_set",
            params.focal_area_set.focal_set_id,
            params.focal_area_set,
            "connectivity.focal_areas.save",
        )

    @staticmethod
    def _candidate_intervention_bundle(
        bundle: ConnectivityProjectBundle,
        site: InterventionSite,
        options: list[InterventionOption],
    ) -> ConnectivityProjectBundle:
        """Overlay one proposed site catalog without mutating repository state."""

        prior_site = next(
            (item for item in bundle.intervention_sites if item.site_id == site.site_id), None
        )
        prior_option_ids = set(prior_site.option_ids if prior_site is not None else ())
        proposed_option_ids = {item.option_id for item in options}
        collisions = sorted(
            item.option_id
            for item in bundle.intervention_options
            if item.option_id in proposed_option_ids and item.site_id != site.site_id
        )
        if collisions:
            raise ValueError(
                "intervention option IDs already belong to another site: " + ", ".join(collisions)
            )
        payload = bundle.model_dump(mode="python")
        payload["intervention_sites"] = [
            item for item in bundle.intervention_sites if item.site_id != site.site_id
        ] + [site]
        payload["intervention_options"] = [
            item
            for item in bundle.intervention_options
            if item.option_id not in prior_option_ids and item.option_id not in proposed_option_ids
        ] + options
        if bundle.cost_basis is not None:
            payload["cost_basis"] = bundle.cost_basis.model_copy(
                update={
                    "option_costs": {
                        item.option_id: item.cost for item in payload["intervention_options"]
                    }
                }
            )
        return ConnectivityProjectBundle.model_validate(payload)

    @staticmethod
    def _intervention_findings(
        findings: Sequence[ModelFinding],
        *,
        site: InterventionSite,
        options: Sequence[InterventionOption],
    ) -> list[ModelFinding]:
        relevant_ids = {
            site.site_id,
            site.footprint_asset_id,
            *(item.option_id for item in options),
            *(effect.effect_id for item in options for effect in item.effects),
        }
        prefixes = (
            "ACTION_EFFECT_",
            "INTERVENTION_",
            "SEPARATE_INTERVENTION_",
            "SURVEY_FIRST_",
            "ORPHAN_INTERVENTION_",
        )
        selected = [
            finding
            for finding in findings
            if finding.object_reference in relevant_ids or finding.code.startswith(prefixes)
        ]
        return sorted(
            selected,
            key=lambda item: (
                {"BLOCKING": 0, "REVIEW_REQUIRED": 1, "INFORMATION": 2}[item.severity],
                item.code,
                item.object_reference,
            ),
        )

    def _intervention_preview(self, params: InterventionPreviewParams) -> PayloadResult:
        canonical_options = [canonicalize_intervention_option(option) for option in params.options]
        store = self._store(params.project_directory)
        try:
            bundle = store.effective_bundle()
            prior_site = next(
                (item for item in bundle.intervention_sites if item.site_id == params.site.site_id),
                None,
            )
            prior_option_ids = sorted(prior_site.option_ids) if prior_site is not None else []
            try:
                candidate = self._candidate_intervention_bundle(
                    bundle, params.site, canonical_options
                )
            except (ValidationError, ValueError) as exc:
                findings = [
                    ModelFinding(
                        "INTERVENTION_PREVIEW_CONTRACT_INVALID",
                        "BLOCKING",
                        params.site.site_id,
                        str(exc),
                    )
                ]
            else:
                findings = self._intervention_findings(
                    [
                        *validate_professional_bundle(candidate),
                        *self._storage_findings(store, candidate),
                    ],
                    site=params.site,
                    options=canonical_options,
                )
            preview_digest = intervention_preview_sha256(
                params.site,
                canonical_options,
                revision_id=bundle.revision.revision_id,
            )
        finally:
            store.close()
        can_save = not any(item.severity == "BLOCKING" for item in findings)
        effect_hashes = [
            {
                "option_id": option.option_id,
                "effect_id": effect.effect_id,
                "scenario_id": effect.scenario_id,
                "sha256": effect.sha256,
            }
            for option in canonical_options
            for effect in option.effects
        ]
        return self._result(
            "connectivity.intervention.preview",
            {
                "preview_sha256": preview_digest,
                "revision_id": bundle.revision.revision_id,
                "site": params.site.model_dump(mode="json"),
                "options": [item.model_dump(mode="json") for item in canonical_options],
                "effect_hashes": effect_hashes,
                "findings": [item.model_dump() for item in findings],
                "can_save": can_save,
                "persisted": False,
                "semantic_diff": {
                    "site_id": params.site.site_id,
                    "previous_option_ids": prior_option_ids,
                    "proposed_option_ids": sorted(params.site.option_ids),
                    "added_option_ids": sorted(set(params.site.option_ids) - set(prior_option_ids)),
                    "removed_option_ids": sorted(
                        set(prior_option_ids) - set(params.site.option_ids)
                    ),
                    "effect_hashes": effect_hashes,
                },
            },
        )

    def _intervention_save(self, params: InterventionSaveParams) -> PayloadResult:
        if {item.option_id for item in params.options} != set(params.site.option_ids) or any(
            item.site_id != params.site.site_id for item in params.options
        ):
            raise _fault(
                "INTERVENTION_CATALOG_MISMATCH",
                "The site and reviewed option catalog do not match.",
                category=ErrorCategory.MODEL_QUALITY,
            )
        store = self._store(params.project_directory)
        try:
            bundle = store.effective_bundle()
            prior_site = next(
                (item for item in bundle.intervention_sites if item.site_id == params.site.site_id),
                None,
            )
            prior_option_ids = set(prior_site.option_ids if prior_site is not None else ())
            proposed_option_ids = {item.option_id for item in params.options}
            removed_option_ids = sorted(prior_option_ids - proposed_option_ids)
            active_options = {item.option_id: item for item in bundle.intervention_options}
            tombstones = {
                str(item["option_id"]): item
                for item in store.list_records("intervention_option_tombstone")
            }
            reassigned = sorted(
                option.option_id
                for option in params.options
                if (tombstone := tombstones.get(option.option_id)) is not None
                and str(tombstone.get("site_id")) != params.site.site_id
            )
            if reassigned:
                raise _fault(
                    "INTERVENTION_OPTION_HISTORICAL_SITE_MISMATCH",
                    "A stable intervention option ID cannot be reassigned to another site.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=reassigned[0],
                    remediation=(
                        "Restore the option to its original site or create a new reviewed option "
                        "ID for this site."
                    ),
                )
            catalog_candidate = self._candidate_intervention_bundle(
                bundle, params.site, params.options
            )
            cost_basis_update: ConnectivityCostBasis | None = None
            if bundle.cost_basis is not None:
                for option in catalog_candidate.intervention_options:
                    retained_cost = bundle.cost_basis.option_costs.get(option.option_id)
                    if retained_cost is not None:
                        if retained_cost != option.cost:
                            raise _fault(
                                "INTERVENTION_COST_BASIS_REVIEW_REQUIRED",
                                "An intervention cost changed outside the reviewed CostBasis.",
                                category=ErrorCategory.MODEL_QUALITY,
                                severity=Severity.BLOCKING,
                                object_reference=option.option_id,
                                remediation=(
                                    "Review and explicitly confirm a CostBasis that contains the "
                                    "changed option cost in the same semantic publication."
                                ),
                            )
                        continue
                    tombstone = tombstones.get(option.option_id)
                    removed_cost = (
                        tombstone.get("removed_cost_basis_amount")
                        if tombstone is not None and tombstone.get("state") == "REMOVED"
                        else None
                    )
                    if removed_cost != option.cost:
                        raise _fault(
                            "INTERVENTION_COST_BASIS_REVIEW_REQUIRED",
                            "A newly added option is not covered by the retained CostBasis.",
                            category=ErrorCategory.MODEL_QUALITY,
                            severity=Severity.BLOCKING,
                            object_reference=option.option_id,
                            remediation=(
                                "Review and explicitly confirm a CostBasis that covers the new "
                                "option before publishing this catalog."
                            ),
                        )
                candidate_cost_basis = catalog_candidate.cost_basis
                if candidate_cost_basis != bundle.cost_basis:
                    cost_basis_update = candidate_cost_basis
            modeled = [
                option
                for option in params.options
                if option.kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
            ]
            if modeled:
                if params.preview_sha256 is None:
                    raise _fault(
                        "INTERVENTION_PREVIEW_REQUIRED",
                        "Modeled action effects require a worker preview for the active revision.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                    )
                canonical_options = [
                    canonicalize_intervention_option(
                        InterventionOptionDraft.model_validate(
                            option.model_dump(mode="python", exclude={"effects"})
                            | {
                                "effects": [
                                    effect.model_dump(mode="python", exclude={"sha256"})
                                    for effect in option.effects
                                ]
                            }
                        )
                    )
                    for option in params.options
                ]
                if canonical_options != params.options:
                    raise _fault(
                        "ACTION_EFFECT_NOT_WORKER_CANONICAL",
                        "Modeled action effects differ from the worker-canonical preview.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                    )
                expected_preview = intervention_preview_sha256(
                    params.site,
                    canonical_options,
                    revision_id=bundle.revision.revision_id,
                )
                if params.preview_sha256 != expected_preview:
                    raise _fault(
                        "INTERVENTION_PREVIEW_STALE",
                        "The intervention preview does not match the active model revision.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                    )
                candidate = self._candidate_intervention_bundle(
                    bundle, params.site, canonical_options
                )
                blockers = [
                    item
                    for item in self._intervention_findings(
                        [
                            *validate_professional_bundle(candidate),
                            *self._storage_findings(store, candidate),
                        ],
                        site=params.site,
                        options=canonical_options,
                    )
                    if item.severity == "BLOCKING"
                ]
                if blockers:
                    raise _fault(
                        "INTERVENTION_PREVIEW_BLOCKED",
                        "The modeled intervention catalog has unresolved blocking findings.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        technical_message=canonical_json([item.model_dump() for item in blockers]),
                    )
            parent_revision_id = bundle.revision.revision_id
            changed_at = params.site.reviewed_at.isoformat()
            lifecycle_records: list[tuple[str, str, dict[str, Any]]] = []
            for option_id in removed_option_ids:
                removed_option = active_options.get(option_id)
                if removed_option is None:
                    raise _fault(
                        "INTERVENTION_OPTION_REMOVAL_SOURCE_MISSING",
                        "The option selected for removal is not present in the active catalog.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=option_id,
                        remediation=(
                            "Reload the active project revision and preview the site catalog again."
                        ),
                    )
                lifecycle_records.append(
                    (
                        "intervention_option_tombstone",
                        option_id,
                        {
                            "schema_version": ("ecocorridor-intervention-option-tombstone/v1.0"),
                            "option_id": option_id,
                            "site_id": params.site.site_id,
                            "state": "REMOVED",
                            "parent_revision_id": parent_revision_id,
                            "option_sha256": canonical_sha256(
                                removed_option.model_dump(mode="json")
                            ),
                            "removed_cost_basis_amount": (
                                bundle.cost_basis.option_costs.get(option_id)
                                if bundle.cost_basis is not None
                                else None
                            ),
                            "cost_basis_sha256": (
                                bundle.cost_basis.sha256 if bundle.cost_basis is not None else None
                            ),
                            "changed_by": params.site.reviewed_by,
                            "changed_at": changed_at,
                            "reason": (
                                "Reviewer removed the option from the mutually exclusive site "
                                "catalog."
                            ),
                        },
                    )
                )
            for option in params.options:
                tombstone = tombstones.get(option.option_id)
                if tombstone is None or tombstone.get("state") != "REMOVED":
                    continue
                lifecycle_records.append(
                    (
                        "intervention_option_tombstone",
                        option.option_id,
                        {
                            "schema_version": ("ecocorridor-intervention-option-tombstone/v1.0"),
                            "option_id": option.option_id,
                            "site_id": params.site.site_id,
                            "state": "RESTORED",
                            "parent_revision_id": parent_revision_id,
                            "option_sha256": canonical_sha256(option.model_dump(mode="json")),
                            "removed_cost_basis_amount": tombstone.get("removed_cost_basis_amount"),
                            "cost_basis_sha256": tombstone.get("cost_basis_sha256"),
                            "changed_by": params.site.reviewed_by,
                            "changed_at": changed_at,
                            "reason": (
                                "Reviewer restored the option to the mutually exclusive site "
                                "catalog."
                            ),
                        },
                    )
                )
            store.publish_semantic_change(
                [
                    (
                        "intervention_site",
                        params.site.site_id,
                        params.site.model_dump(mode="json"),
                    ),
                    *[
                        (
                            "intervention_option",
                            option.option_id,
                            option.model_dump(mode="json"),
                        )
                        for option in params.options
                    ],
                    *lifecycle_records,
                    *(
                        [
                            (
                                "cost_basis",
                                "current",
                                cost_basis_update.model_dump(mode="json"),
                            )
                        ]
                        if cost_basis_update is not None
                        else []
                    ),
                ],
                created_by=params.site.reviewed_by,
                created_at=params.site.reviewed_at,
                reason="Publish a reviewer-approved mutually exclusive intervention catalog.",
            )
        finally:
            store.close()
        return self._result(
            "connectivity.intervention.save",
            {"site_id": params.site.site_id, "option_ids": sorted(params.site.option_ids)},
        )

    def _scenario_save(self, params: ScenarioSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "scenario",
            params.scenario.scenario_id,
            params.scenario,
            "connectivity.scenario.save",
        )

    def _safeguard_save(self, params: SafeguardSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "safeguard",
            params.safeguard.safeguard_id,
            params.safeguard,
            "connectivity.safeguard.save",
        )

    def _review_mask_save(self, params: ReviewMaskSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "review_mask",
            params.review_mask.mask_id,
            params.review_mask,
            "connectivity.review_mask.save",
        )

    def _decision_rule_save(self, params: DecisionRuleSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "decision_rule",
            params.decision_rule.rule_id,
            params.decision_rule,
            "connectivity.decision_rule.save",
        )

    def _decision_brief_save(self, params: DecisionBriefSaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "decision_brief",
            params.decision_brief.brief_id,
            params.decision_brief,
            "connectivity.decision_brief.save",
        )

    def _cost_basis_get(self, params: ProjectParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            bundle = store.effective_bundle()
            requirements = cost_basis_requirements(bundle.cost_basis, bundle.intervention_options)
        finally:
            store.close()
        return self._result(
            "connectivity.cost_basis.get",
            {
                "cost_basis": (
                    bundle.cost_basis.model_dump(mode="json")
                    if bundle.cost_basis is not None
                    else None
                ),
                "active_revision_id": bundle.revision.revision_id,
                "confirmed": not requirements,
                "missing_requirements": [item.message for item in requirements],
            },
        )

    def _cost_basis_save(self, params: CostBasisSaveParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            active_bundle = store.effective_bundle()
            normalized = params.cost_basis.model_copy(
                update={
                    "source": params.cost_basis.source.strip(),
                    "currency": params.cost_basis.currency.strip(),
                    "vintage": params.cost_basis.vintage.strip(),
                    "reviewer": params.cost_basis.reviewer.strip(),
                }
            )
            try:
                confirmed_basis = with_reviewer_confirmation(
                    normalized, params.confirmation_statement
                )
                candidate_payload = active_bundle.model_dump(mode="python")
                candidate_payload["cost_basis"] = confirmed_basis
                candidate = ConnectivityProjectBundle.model_validate(candidate_payload)
                for assumption in confirmed_basis.assumptions:
                    assert_strict_ecological_claim(assumption)
                parse_reviewed_cost_basis(confirmed_basis, candidate.intervention_options)
            except (KeyError, ValidationError, ValueError) as exc:
                raise _fault(
                    "COST_BASIS_REVIEW_INCOMPLETE",
                    "The reviewed CostBasis is incomplete or differs from the retained intervention catalog.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation=(
                        "Complete source, currency, vintage, horizon, discount rate, CAPEX, "
                        "O&M, replacement, reviewer, option coverage, and explicit confirmation."
                    ),
                    technical_message=str(exc),
                ) from exc
            publication = store.publish_semantic_change(
                [
                    (
                        "cost_basis",
                        "current",
                        confirmed_basis.model_dump(mode="json"),
                    )
                ],
                created_by=confirmed_basis.reviewer,
                created_at=confirmed_basis.reviewed_at,
                reason="Publish explicitly confirmed professional screening CostBasis.",
            )
            revision = int(publication["record_revisions"]["cost_basis:current"])
            active_revision_id = str(publication["revision"]["revision_id"])
        finally:
            store.close()
        return self._result(
            "connectivity.cost_basis.save",
            {
                "record_id": confirmed_basis.cost_basis_id,
                "revision": revision,
                "cost_basis": confirmed_basis.model_dump(mode="json"),
                "active_revision_id": active_revision_id,
                "cost_basis_sha256": confirmed_basis.sha256,
                "confirmed": True,
            },
        )

    def _resource_policy_save(self, params: ResourcePolicySaveParams) -> PayloadResult:
        return self._save_contract(
            params.project_directory,
            "resource_policy",
            "current",
            params.resource_policy,
            "connectivity.resource_policy.save",
        )

    def _save_contract(
        self,
        project_directory: str,
        record_type: str,
        record_id: str,
        contract: Any,
        method: str,
    ) -> PayloadResult:
        store = self._store(project_directory)
        try:
            created_by = str(
                getattr(contract, "reviewed_by", None)
                or getattr(contract, "decision_owner", None)
                or "EcoCorridor project editor"
            )
            created_at = getattr(contract, "reviewed_at", None)
            publication = store.publish_semantic_change(
                [(record_type, record_id, contract.model_dump(mode="json"))],
                created_by=created_by,
                created_at=created_at,
                reason=f"Publish reviewed {record_type.replace('_', ' ')} configuration.",
            )
            revision = int(publication["record_revisions"][f"{record_type}:{record_id}"])
        finally:
            store.close()
        return self._result(method, {"record_id": record_id, "revision": revision})

    @staticmethod
    def _model_fitness_evidence_findings(
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        *,
        authoritative_reference_results: Sequence[
            tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim]]
        ] = (),
    ) -> list[ModelFinding]:
        """Resolve every fitness evidence ID against active, retained evidence.

        An ID is valid only when it names either an asset in the effective
        bundle whose exact bytes remain in project storage, or a worker-retained
        evidence record bound to the active revision and model hash.  Merely
        repeating an ID from a prior revision cannot satisfy this gate.
        """

        findings: list[ModelFinding] = []
        active_assets = {item.asset_id: item for item in bundle.assets}
        passed_reference_evidence_ids: set[str] = set()
        reference_result_findings = validate_professional_bundle(
            bundle,
            authoritative_reference_results=[item[0] for item in authoritative_reference_results],
            expected_engine_manifest_sha256=(
                authoritative_reference_results[0][0].engine_manifest_sha256
                if authoritative_reference_results
                else None
            ),
        )
        reference_evidence_blocked = any(
            item.code
            in {
                "REFERENCE_COMPARISON_REQUIRED",
                "REFERENCE_COMPARISON_FAILED",
                "REFERENCE_COMPARISON_CONFLICT",
            }
            for item in reference_result_findings
        )
        for result, claims in authoritative_reference_results:
            if reference_evidence_blocked or result.status != "PASSED":
                continue
            individual_findings = validate_professional_bundle(
                bundle,
                authoritative_reference_results=[result],
                expected_engine_manifest_sha256=result.engine_manifest_sha256,
            )
            if any(
                item.code
                in {
                    "REFERENCE_COMPARISON_REQUIRED",
                    "REFERENCE_COMPARISON_FAILED",
                    "REFERENCE_COMPARISON_CONFLICT",
                }
                for item in individual_findings
            ):
                continue
            passed_reference_evidence_ids.add(result.result_id)
            passed_reference_evidence_ids.update(item.claim_id for item in claims)

        def asset_bytes_status(asset_id: str) -> str:
            cached = asset_statuses.get(asset_id)
            if cached is not None:
                return cached
            asset = active_assets[asset_id]
            try:
                source = store.resolve_asset_path(asset.relative_path)
                changed = (
                    source.stat().st_size != asset.byte_size or _sha256_file(source) != asset.sha256
                )
            except (FileNotFoundError, OSError, ValueError):
                status = "MISSING"
            else:
                status = "CHANGED" if changed else "VERIFIED"
            asset_statuses[asset_id] = status
            return status

        asset_statuses: dict[str, str] = {}
        for dimension in bundle.fitness.dimensions:
            if not dimension.evidence_ids:
                findings.append(
                    ModelFinding(
                        "MODEL_FITNESS_EVIDENCE_REQUIRED",
                        "BLOCKING",
                        dimension.key,
                        (
                            "Attach at least one active, checksum-verified project asset or "
                            "worker-retained evidence record reviewed for this model revision."
                        ),
                    )
                )
                continue
            for evidence_id in sorted(set(dimension.evidence_ids)):
                if evidence_id in active_assets:
                    status = asset_bytes_status(evidence_id)
                    if status != "VERIFIED":
                        findings.append(
                            ModelFinding(
                                (
                                    "MODEL_FITNESS_EVIDENCE_BYTES_MISSING"
                                    if status == "MISSING"
                                    else "MODEL_FITNESS_EVIDENCE_BYTES_CHANGED"
                                ),
                                "BLOCKING",
                                f"{dimension.key}:{evidence_id}",
                                (
                                    "Restore the exact active asset bytes and checksum before "
                                    "using this evidence ID in the Model Fitness Passport."
                                ),
                            )
                        )
                    continue
                if evidence_id in passed_reference_evidence_ids:
                    continue
                try:
                    retained = store.get_record("model_fitness_evidence", evidence_id)
                except KeyError:
                    try:
                        store.get_record("asset", evidence_id)
                    except KeyError:
                        code = "MODEL_FITNESS_EVIDENCE_ID_UNRESOLVED"
                        remediation = (
                            "Choose an asset ID from the active project revision or retain the "
                            "reviewed evidence through the worker before attesting."
                        )
                    else:
                        code = "MODEL_FITNESS_EVIDENCE_ID_STALE"
                        remediation = (
                            "This asset record is not part of the active model revision. Select "
                            "active evidence and review the fitness dimension again."
                        )
                    findings.append(
                        ModelFinding(
                            code,
                            "BLOCKING",
                            f"{dimension.key}:{evidence_id}",
                            remediation,
                        )
                    )
                    continue
                content = retained.get("content")
                source_asset_ids = retained.get("source_asset_ids")
                malformed = (
                    retained.get("schema_version") != "ecocorridor-model-fitness-evidence/v1.0"
                    or retained.get("evidence_id") != evidence_id
                    or not isinstance(content, dict)
                    or retained.get("content_sha256") != canonical_sha256(content)
                    or not isinstance(source_asset_ids, list)
                    or any(not isinstance(item, str) or not item for item in source_asset_ids)
                )
                if malformed:
                    findings.append(
                        ModelFinding(
                            "MODEL_FITNESS_EVIDENCE_RECORD_MISMATCH",
                            "BLOCKING",
                            f"{dimension.key}:{evidence_id}",
                            (
                                "Restore the worker-retained evidence record whose content hash "
                                "and identity match this evidence ID."
                            ),
                        )
                    )
                    continue
                if (
                    retained.get("revision_id") != bundle.revision.revision_id
                    or retained.get("model_sha256") != bundle.revision.model_sha256
                ):
                    findings.append(
                        ModelFinding(
                            "MODEL_FITNESS_EVIDENCE_ID_STALE",
                            "BLOCKING",
                            f"{dimension.key}:{evidence_id}",
                            (
                                "Re-retain or replace this evidence for the active semantic "
                                "revision, then review the fitness dimension again."
                            ),
                        )
                    )
                    continue
                source_mismatches = []
                for source_asset_id in sorted(set(source_asset_ids)):
                    if source_asset_id not in active_assets:
                        source_mismatches.append(source_asset_id)
                        continue
                    status = asset_bytes_status(source_asset_id)
                    if status != "VERIFIED":
                        source_mismatches.append(source_asset_id)
                if source_mismatches:
                    findings.append(
                        ModelFinding(
                            "MODEL_FITNESS_EVIDENCE_SOURCE_MISMATCH",
                            "BLOCKING",
                            f"{dimension.key}:{evidence_id}",
                            (
                                "Restore every active checksum-verified source asset bound to "
                                "this retained evidence record."
                            ),
                        )
                    )
        return findings

    @staticmethod
    def _storage_findings(
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        *,
        authoritative_reference_results: Sequence[
            tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim]]
        ] = (),
    ) -> list[ModelFinding]:
        fitness_findings = RPCDispatcher._model_fitness_evidence_findings(
            store,
            bundle,
            authoritative_reference_results=authoritative_reference_results,
        )
        if store.project()["mode"] == "GUIDED":
            return fitness_findings
        findings: list[ModelFinding] = list(fitness_findings)
        for index, requirement in enumerate(
            cost_basis_requirements(bundle.cost_basis, bundle.intervention_options), start=1
        ):
            findings.append(
                ModelFinding(
                    requirement.code,
                    "BLOCKING",
                    f"cost_basis:{index}",
                    requirement.message,
                )
            )
        assets = {item.asset_id: item for item in bundle.assets}
        retained_rasters: dict[str, InMemoryRaster] = {}
        retained_bytes = 0
        for asset in bundle.assets:
            try:
                source = store.resolve_asset_path(asset.relative_path)
            except (FileNotFoundError, ValueError):
                findings.append(
                    ModelFinding(
                        "ASSET_BYTES_MISSING",
                        "BLOCKING",
                        asset.asset_id,
                        "Restore the exact checksum-verified source asset inside the project.",
                    )
                )
                continue
            retained_bytes += source.stat().st_size
            if _sha256_file(source) != asset.sha256 or source.stat().st_size != asset.byte_size:
                findings.append(
                    ModelFinding(
                        "ASSET_BYTES_CHANGED",
                        "BLOCKING",
                        asset.asset_id,
                        "Re-import or restore the reviewed bytes; do not continue with changed data.",
                    )
                )
                continue
            if asset.kind in {
                AssetKind.RESISTANCE_RASTER,
                AssetKind.FOCAL_RASTER,
                AssetKind.REVIEW_MASK,
            }:
                try:
                    inspect_reviewed_raster(source, asset)
                    retained_rasters[asset.asset_id] = load_raster(source, asset)
                except RasterIOError as exc:
                    findings.append(
                        ModelFinding(
                            "RASTER_STORAGE_VALIDATION_FAILED",
                            "BLOCKING",
                            asset.asset_id,
                            str(exc),
                        )
                    )
            generalized_id = asset.authorization.generalized_asset_id
            if generalized_id is not None and generalized_id not in assets:
                findings.append(
                    ModelFinding(
                        "GENERALIZED_ASSET_REFERENCE_MISSING",
                        "BLOCKING",
                        asset.asset_id,
                        "Add the exact reviewed generalized derivative before cloud synchronization.",
                    )
                )

        for focal_set in bundle.focal_area_sets:
            focal_raster = retained_rasters.get(focal_set.asset_id)
            if focal_raster is None:
                continue
            observed = {
                int(value)
                for row in focal_raster.values
                for value in row
                if value is not None
                and not _is_nodata(value, focal_raster.grid.nodata)
                and int(value) > 0
            }
            if observed != set(focal_set.focal_ids):
                findings.append(
                    ModelFinding(
                        "FOCAL_RASTER_IDENTITY_MISMATCH",
                        "BLOCKING",
                        focal_set.focal_set_id,
                        "Make the reviewed focal IDs exactly match the retained identifier raster.",
                    )
                )

        hypotheses = {item.hypothesis_id: item for item in bundle.resistance_hypotheses}
        scenarios = {item.scenario_id: item for item in bundle.scenarios}
        for option in bundle.intervention_options:
            for effect in option.effects:
                scenario = scenarios.get(effect.scenario_id)
                hypothesis = (
                    hypotheses.get(scenario.resistance_hypothesis_id)
                    if scenario is not None
                    else None
                )
                resistance = (
                    retained_rasters.get(hypothesis.resistance_asset_id)
                    if hypothesis is not None
                    else None
                )
                if resistance is None:
                    continue
                if any(
                    _is_nodata(resistance.values[cell.row][cell.column], resistance.grid.nodata)
                    for cell in effect.cells
                    if cell.row < resistance.grid.height and cell.column < resistance.grid.width
                ):
                    findings.append(
                        ModelFinding(
                            "ACTION_EFFECT_CHANGES_NODATA",
                            "BLOCKING",
                            effect.effect_id,
                            "Remove replacement cells from NODATA; v0.1 cannot change raster topology.",
                        )
                    )
        if retained_bytes > bundle.resource_policy.max_input_bytes:
            findings.append(
                ModelFinding(
                    "PROJECT_INPUT_RESOURCE_POLICY_BLOCKED",
                    "BLOCKING",
                    bundle.project_id,
                    "Revise the explicit resource policy or reduce retained project inputs.",
                )
            )

        plans = {
            item["plan_id"]: GridAlignmentPlan.model_validate(item)
            for item in store.list_records("grid_alignment_plan")
        }
        approvals = {
            item.plan_id: item
            for item in (
                GridAlignmentApproval.model_validate(payload)
                for payload in store.list_records("grid_alignment_approval")
            )
        }
        for asset in bundle.assets:
            marker = ":alignment:"
            if not asset.source.startswith("derived:") or marker not in asset.source:
                continue
            source_reference, plan_id = asset.source[len("derived:") :].split(marker, 1)
            plan = plans.get(plan_id)
            approval = approvals.get(plan_id)
            if (
                plan is None
                or approval is None
                or approval.plan_sha256 != grid_alignment_plan_sha256(plan)
                or plan.source_asset_id != source_reference
                or plan.target_grid != asset.grid
            ):
                findings.append(
                    ModelFinding(
                        "DERIVED_ALIGNMENT_LINEAGE_INCOMPLETE",
                        "BLOCKING",
                        asset.asset_id,
                        "Restore the exact source, immutable alignment plan, approval, and semantic revision.",
                    )
                )
        if bundle.revision.diff_id is not None:
            try:
                semantic_diff = store.get_record("semantic_diff", bundle.revision.diff_id)
            except KeyError:
                semantic_diff = None
            if (
                semantic_diff is None
                or semantic_diff.get("to_revision_id") != bundle.revision.revision_id
            ):
                findings.append(
                    ModelFinding(
                        "SEMANTIC_REVISION_LINEAGE_INCOMPLETE",
                        "BLOCKING",
                        bundle.revision.revision_id,
                        "Restore the semantic diff that created this model revision.",
                    )
                )
        return findings

    @classmethod
    def _validation_data(
        cls,
        bundle: ConnectivityProjectBundle,
        *,
        store: EcoProjectStore | None = None,
        expected_engine_manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        authoritative_results: list[
            tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim]]
        ] = []
        reference_integrity_error: str | None = None
        if store is not None and expected_engine_manifest_sha256 is not None:
            try:
                authoritative_results = store.reference_comparison_results(
                    revision_id=bundle.revision.revision_id,
                    model_sha256=bundle.revision.model_sha256,
                    engine_manifest_sha256=expected_engine_manifest_sha256,
                )
            except (KeyError, OSError, ValueError) as exc:
                reference_integrity_error = type(exc).__name__
        data = validation_projection(
            bundle,
            authoritative_reference_results=[item[0] for item in authoritative_results],
            expected_engine_manifest_sha256=expected_engine_manifest_sha256,
        )
        findings = [dict(item) for item in data["findings"]]
        if store is not None:
            findings.extend(
                item.model_dump()
                for item in cls._storage_findings(
                    store,
                    bundle,
                    authoritative_reference_results=authoritative_results,
                )
            )
        if reference_integrity_error is not None:
            findings.append(
                ModelFinding(
                    "REFERENCE_COMPARISON_EVIDENCE_INTEGRITY_FAILED",
                    "BLOCKING",
                    bundle.revision.revision_id,
                    (
                        "Restore or rerun the immutable reference comparison evidence. "
                        "A checksum, direct-run, plan, model, engine, binding, or claim "
                        "mismatch prevents this evidence from satisfying Model Fitness."
                    ),
                ).model_dump()
            )
        findings.sort(
            key=lambda item: (
                {"BLOCKING": 0, "REVIEW_REQUIRED": 1, "INFORMATION": 2}[str(item["severity"])],
                str(item["code"]),
                str(item["object_reference"]),
            )
        )
        data["findings"] = findings
        data["status"] = (
            "BLOCKED" if any(item["severity"] == "BLOCKING" for item in findings) else "READY"
        )
        data["retained_source_validation"] = store is not None
        return data

    def _model_validate(self, params: ProjectParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            bundle: ConnectivityProjectBundle | None = None
            try:
                bundle = store.effective_bundle()
                data = self._validation_data(
                    bundle,
                    store=store,
                    expected_engine_manifest_sha256=(
                        self.engine_manifest.sha256 if self.engine_manifest is not None else None
                    ),
                )
            except (ValidationError, ValueError) as exc:
                data = {
                    "status": "BLOCKED",
                    "findings": [
                        {
                            "code": "PLANNING_MODEL_INCOMPLETE",
                            "severity": "BLOCKING",
                            "object_reference": "project",
                            "remediation": str(exc),
                        }
                    ],
                }
            if bundle is not None:
                store.record_validation(revision=bundle.revision, validation=data)
            else:
                validation_id = "validation_" + canonical_sha256(data)[:24]
                validation_payload = {
                    **data,
                    "validation_id": validation_id,
                    "revision_id": None,
                    "model_sha256": None,
                    "validated_at": datetime.now(UTC).isoformat(),
                }
                store.put_records_atomic(
                    [
                        ("validation", validation_id, validation_payload, False),
                        ("validation", "latest", validation_payload, True),
                    ]
                )
        finally:
            store.close()
        return self._result("connectivity.model.validate", data)

    def _model_attest(self, params: ModelAttestParams) -> PayloadResult:
        if not params.reviewer.strip() or not params.attestation.strip():
            raise _fault(
                "MODEL_ATTESTATION_INCOMPLETE",
                "A named reviewer and explicit attestation are required.",
                category=ErrorCategory.MODEL_QUALITY,
            )
        if params.fitness.has_blockers:
            raise _fault(
                "MODEL_FITNESS_BLOCKED",
                "Blocking Model Fitness findings cannot be attested away.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
            )
        if params.fitness.reviewed_by != params.reviewer or params.fitness.reviewed_at is None:
            raise _fault(
                "MODEL_ATTESTATION_REVIEWER_MISMATCH",
                "The attestation reviewer must match the reviewed passport.",
                category=ErrorCategory.MODEL_QUALITY,
            )
        store = self._store(params.project_directory)
        try:
            active_bundle = store.effective_bundle()
            active_revision_id = active_bundle.revision.revision_id
            if params.fitness.revision_id != active_revision_id:
                raise _fault(
                    "MODEL_ATTESTATION_REVISION_MISMATCH",
                    "The Model Fitness Passport must be reviewed for the active semantic revision.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                )
            attested_fitness = params.fitness.model_copy(
                update={
                    "attestations": sorted(
                        {*params.fitness.attestations, params.attestation.strip()}
                    )
                }
            )
            candidate_payload = active_bundle.model_dump(mode="python")
            candidate_payload["fitness"] = attested_fitness
            candidate_bundle = ConnectivityProjectBundle.model_validate(candidate_payload)
            candidate_validation = self._validation_data(
                candidate_bundle,
                store=store,
                expected_engine_manifest_sha256=(
                    self.engine_manifest.sha256 if self.engine_manifest is not None else None
                ),
            )
            fitness_evidence_blockers = [
                item
                for item in candidate_validation["findings"]
                if item["severity"] == "BLOCKING"
                and str(item["code"]).startswith("MODEL_FITNESS_EVIDENCE_")
            ]
            if fitness_evidence_blockers:
                first = fitness_evidence_blockers[0]
                raise _fault(
                    str(first["code"]),
                    "The Model Fitness Passport cites evidence that is not retained for the active revision.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=str(first["object_reference"]),
                    remediation=str(first["remediation"]),
                    technical_message=canonical_json(fitness_evidence_blockers),
                )
            candidate_blockers = [
                item
                for item in candidate_validation["findings"]
                if item["severity"] == "BLOCKING"
                and item["code"] != "MODEL_FITNESS_ATTESTATION_MISSING"
            ]
            if candidate_blockers:
                raise _fault(
                    "MODEL_ATTESTATION_CANDIDATE_VALIDATION_BLOCKED",
                    "The submitted Model Fitness Passport cannot be attested until its model and evidence blockers are resolved.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    technical_message=canonical_json(candidate_blockers),
                    remediation="Review the listed model, source-evidence, and fitness findings, then validate again.",
                )
            candidate_fitness_sha256 = canonical_sha256(attested_fitness.model_dump(mode="json"))
            ready_payload = {
                **candidate_validation,
                "status": "READY",
                "findings": [
                    {
                        **item,
                        "severity": (
                            "REVIEW_REQUIRED"
                            if item["code"] == "MODEL_FITNESS_ATTESTATION_MISSING"
                            else item["severity"]
                        ),
                    }
                    for item in candidate_validation["findings"]
                ],
                "attestation_pending": True,
                "candidate_fitness_sha256": candidate_fitness_sha256,
            }
            store.record_validation(
                revision=active_bundle.revision,
                validation=ready_payload,
            )
            ready_validation = store.latest_ready_validation()
            if (
                ready_validation is None
                or ready_validation.get("candidate_fitness_sha256") != candidate_fitness_sha256
            ):
                raise _fault(
                    "MODEL_ATTESTATION_READY_VALIDATION_REQUIRED",
                    "A READY validation for the submitted passport could not be retained.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                )
            attestation_recorded_at = datetime.now(UTC)
            publication = store.publish_fitness_attestation(
                fitness=attested_fitness.model_dump(mode="json"),
                reviewer=params.reviewer,
                attestation=params.attestation,
                validation_id=str(ready_validation["validation_id"]),
                candidate_fitness_sha256=candidate_fitness_sha256,
                recorded_at=attestation_recorded_at,
            )
            revision = int(publication["fitness_revision"])
        finally:
            store.close()
        return self._result(
            "connectivity.model.attest",
            {"passport_id": params.fitness.passport_id, "fitness_revision": revision},
        )

    def _analysis_prepare(self, params: AnalysisPrepareParams) -> PayloadResult:
        """Derive a complete direct-run proposal from authoritative project evidence."""

        store = self._store(params.project_directory)
        try:
            self._authoritative_project_context(store)
            bundle = store.effective_bundle()
            validation = self._validation_data(
                bundle,
                store=store,
                expected_engine_manifest_sha256=(
                    self.engine_manifest.sha256 if self.engine_manifest is not None else None
                ),
            )
        finally:
            store.close()
        blockers = [
            ModelFinding(
                str(item["code"]),
                "BLOCKING",
                str(item["object_reference"]),
                str(item["remediation"]),
            )
            for item in validation["findings"]
            if item["severity"] == "BLOCKING"
        ]
        engine_available = (
            self.engine_executable is not None
            and self.engine_manifest is not None
            and self.analysis_executor is not None
        )
        if not engine_available:
            blockers.append(
                ModelFinding(
                    "PINNED_CIRCUITSCAPE_ENGINE_UNAVAILABLE",
                    "BLOCKING",
                    bundle.project_id,
                    "Install a signed release containing the verified Circuitscape engine bundle.",
                )
            )

        options_by_id = {item.option_id: item for item in bundle.intervention_options}
        baseline_selections: dict[str, str] = {}
        for site in sorted(bundle.intervention_sites, key=lambda item: item.site_id):
            no_action = [
                option_id
                for option_id in site.option_ids
                if option_id in options_by_id and options_by_id[option_id].kind.value == "NO_ACTION"
            ]
            if len(no_action) != 1:
                blockers.append(
                    ModelFinding(
                        "BASELINE_NO_ACTION_NOT_UNIQUE",
                        "BLOCKING",
                        site.site_id,
                        "Provide exactly one reviewed No Action option before preparing analysis.",
                    )
                )
            else:
                baseline_selections[site.site_id] = no_action[0]

        finite_universe_size = math.prod(len(site.option_ids) for site in bundle.intervention_sites)
        if finite_universe_size > 1_000_000:
            blockers.append(
                ModelFinding(
                    "PORTFOLIO_UNIVERSE_TOO_LARGE_FOR_SAFE_PREPARATION",
                    "BLOCKING",
                    bundle.project_id,
                    "Reduce or partition the reviewed site-option catalog before direct search.",
                )
            )

        proposed_plan: ConnectivityRunPlan | None = None
        search_projection: dict[str, Any] | None = None
        if not blockers and self.engine_manifest is not None:
            baseline = canonicalize_portfolio(
                bundle.intervention_sites,
                bundle.intervention_options,
                baseline_selections,
            )

            def valid_portfolio(selections: dict[str, str]) -> bool:
                try:
                    portfolio = canonicalize_portfolio(
                        bundle.intervention_sites,
                        bundle.intervention_options,
                        selections,
                    )
                    for scenario in bundle.scenarios:
                        scenario_effects_for_portfolio(
                            portfolio,
                            bundle.intervention_options,
                            scenario,
                        )
                except (KeyError, ValueError):
                    return False
                return True

            analysis_id = new_id("analysis")
            search_recorder = ProcessResourceRecorder(
                pid=os.getpid(),
                observation_id=f"resource:{analysis_id}:search",
                boundary=ResourceObservationBoundary.SEARCH_PROPOSER,
                boundary_description=(
                    "One in-process bounded portfolio-plan proposal, including categorical "
                    "Sobol generation, constraint repair, and deduplication."
                ),
            )
            try:
                search = make_search_plan(
                    sites=bundle.intervention_sites,
                    options=bundle.intervention_options,
                    baseline=baseline,
                    scenario_count=len(bundle.scenarios),
                    max_direct_jobs=bundle.resource_policy.max_direct_jobs,
                    seed=params.seed,
                    is_valid=valid_portfolio,
                )
            finally:
                search_resource_observation = search_recorder.finish()
            portfolios = [item.portfolio for item in search.candidates]
            current_map_portfolios = (
                [item.portfolio_id for item in portfolios] if bundle.review_masks else []
            )
            proposed_plan = ConnectivityRunPlan(
                analysis_id=analysis_id,
                revision_id=bundle.revision.revision_id,
                scenario_ids=[item.scenario_id for item in bundle.scenarios],
                portfolios=portfolios,
                baseline_portfolio_id=baseline.portfolio_id,
                direct_job_count=search.planned_direct_jobs,
                solver_job_cap=bundle.resource_policy.max_direct_jobs,
                search_method=search.method,
                seed=params.seed,
                current_maps_for_portfolio_ids=current_map_portfolios,
                engine_manifest_sha256=self.engine_manifest.sha256,
                search_resource_observation=search_resource_observation,
            )
            plan_findings = validate_run_plan(bundle, proposed_plan)
            plan_blockers = [item for item in plan_findings if item.severity == "BLOCKING"]
            if plan_blockers:
                blockers.extend(plan_blockers)
                proposed_plan = None
            search_projection = {
                "method": search.method,
                "qmc_engine": search.qmc_engine,
                "finite_universe_size": search.finite_universe_size,
                "portfolio_capacity": search.portfolio_capacity,
                "selected_portfolio_count": len(search.candidates),
                "seed": search.seed,
            }

        scenario_count = len(bundle.scenarios)
        selected_count = len(proposed_plan.portfolios) if proposed_plan is not None else 0
        return self._result(
            "connectivity.analysis.prepare",
            {
                "project_id": bundle.project_id,
                "revision_id": bundle.revision.revision_id,
                "scenario_ids": [item.scenario_id for item in bundle.scenarios],
                "resource_policy": bundle.resource_policy.model_dump(mode="json"),
                "engine_available": engine_available,
                "engine_manifest": (
                    self.engine_manifest.model_dump(mode="json")
                    if self.engine_manifest is not None
                    else None
                ),
                "finite_universe_size": finite_universe_size,
                "portfolio_capacity": (
                    bundle.resource_policy.max_direct_jobs // max(1, scenario_count)
                ),
                "selected_portfolio_count": selected_count,
                "direct_job_count": (
                    proposed_plan.direct_job_count if proposed_plan is not None else 0
                ),
                "arithmetic": (
                    f"{selected_count} portfolios × {scenario_count} scenarios = "
                    f"{selected_count * scenario_count} direct Circuitscape jobs"
                ),
                "blockers": [item.model_dump() for item in blockers],
                "search": search_projection,
                "proposed_plan": (
                    proposed_plan.model_dump(mode="json") if proposed_plan is not None else None
                ),
            },
        )

    def _analysis_plan(self, params: AnalysisPlanParams) -> PayloadResult:
        self._require_plan_engine_manifest(params.plan)
        store = self._store(params.project_directory)
        try:
            self._authoritative_project_context(store)
            bundle = store.effective_bundle()
            validation = self._validation_data(
                bundle,
                store=store,
                expected_engine_manifest_sha256=(
                    self.engine_manifest.sha256 if self.engine_manifest is not None else None
                ),
            )
            if validation["status"] != "READY":
                raise _fault(
                    "ANALYSIS_MODEL_NOT_READY",
                    "Blocking ecological, evidence, raster, or model-fitness findings must be resolved before analysis.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    technical_message=canonical_json(validation["findings"]),
                )
            if params.plan.revision_id != bundle.revision.revision_id:
                raise _fault(
                    "ANALYSIS_REVISION_MISMATCH",
                    "The run plan belongs to another model revision.",
                    category=ErrorCategory.MODEL_QUALITY,
                )
            if self.engine_manifest is not None:
                incompatible_settings = [
                    scenario.scenario_id
                    for scenario in bundle.scenarios
                    if scenario.neighbor_rule != self.engine_manifest.neighbor_rule
                    or scenario.resistance_average != self.engine_manifest.resistance_average
                ]
                if incompatible_settings:
                    raise _fault(
                        "ANALYSIS_ENGINE_SETTINGS_MISMATCH",
                        "One or more scenarios use settings outside the installed signed engine manifest.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        technical_message=", ".join(sorted(incompatible_settings)),
                    )
            plan_findings = validate_run_plan(bundle, params.plan)
            blockers = [item for item in plan_findings if item.severity == "BLOCKING"]
            if blockers:
                raise _fault(
                    "ANALYSIS_PLAN_INVALID",
                    "The run plan does not preserve the complete reviewed ecological ensemble.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    technical_message=canonical_json([item.model_dump() for item in plan_findings]),
                )
            store.create_analysis(params.plan, bundle=bundle)
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.plan",
            {
                "analysis_id": params.plan.analysis_id,
                "state": "READY",
                "direct_job_count": params.plan.direct_job_count,
                "review_findings": [
                    item.model_dump()
                    for item in plan_findings
                    if item.severity == "REVIEW_REQUIRED"
                ],
                "arithmetic": (
                    f"{len(params.plan.portfolios)} portfolios × "
                    f"{len(params.plan.scenario_ids)} scenarios = "
                    f"{params.plan.direct_job_count} direct Circuitscape jobs"
                ),
            },
        )

    def _analysis_start(self, params: ProjectAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            self._authoritative_project_context(store)
            analysis = store.analysis(params.analysis_id)
            if analysis.get("analysis_kind") == "REFERENCE":
                raise _fault(
                    "REFERENCE_ANALYSIS_REQUIRES_REFERENCE_START",
                    "Start this immutable reference analysis through the reference workflow.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=params.analysis_id,
                )
            engine, digest, executor = self._require_engine()
            plan = ConnectivityRunPlan.model_validate(analysis["plan"])
            self._require_plan_engine_manifest(plan)
            try:
                bundle = store.analysis_bundle(params.analysis_id)
            except KeyError as exc:
                raise _fault(
                    "ANALYSIS_MODEL_SNAPSHOT_MISSING",
                    "The immutable model snapshot for this analysis is missing.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                ) from exc
            try:
                execution = executor.start(
                    project_directory=store.root,
                    bundle=bundle,
                    plan=plan,
                    engine_executable=engine,
                    engine_sha256=digest,
                )
                state = str(execution.get("state", "RUNNING"))
                if state != "RUNNING":
                    raise _fault(
                        "ANALYSIS_EXECUTOR_PROTOCOL_VIOLATION",
                        "The direct-analysis executor returned an unsupported synchronous state.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        remediation=(
                            "Use the durable executor queue; completion must be committed from "
                            "successful direct jobs and retained artifacts."
                        ),
                    )
                authoritative = store.analysis(params.analysis_id)
                job_counts = store.direct_job_counts(params.analysis_id)
                if (
                    authoritative["state"]
                    not in {AnalysisState.RUNNING.value, AnalysisState.COMPLETED.value}
                    or sum(job_counts.values()) != plan.direct_job_count
                ):
                    raise _fault(
                        "ANALYSIS_DURABLE_QUEUE_MISSING",
                        "The executor did not create the complete authoritative direct-job queue.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        remediation="Restart with the installed durable direct-analysis executor.",
                    )
            except AnalysisOwnershipError as exc:
                raise _fault(
                    "ANALYSIS_EXECUTION_BUSY",
                    "Another application worker currently owns this project's direct analysis.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                    remediation="Wait for the owning worker to stop or finish, then retry.",
                    retryable=True,
                    technical_message=type(exc).__name__,
                ) from exc
            except OrphanProcessAuditError as exc:
                raise _fault(
                    "ANALYSIS_ORPHAN_PROCESS_AUDIT_REQUIRED",
                    "A prior solver process tree could not be proven stopped, so this analysis "
                    "was not recovered or changed.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    remediation=(
                        "Confirm the prior application and every Circuitscape child process are "
                        "stopped, then complete the local orphan-process audit before retrying."
                    ),
                    technical_message=exc.reason_code,
                ) from exc
            except Exception as exc:
                if store.analysis(params.analysis_id)["state"] == AnalysisState.RUNNING.value:
                    store.fail_analysis(
                        params.analysis_id,
                        {
                            "code": "DIRECT_ANALYSIS_FAILED",
                            "technical_type": type(exc).__name__,
                        },
                    )
                raise
            status = store.analysis(params.analysis_id)
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.start",
            {"analysis_id": params.analysis_id, "state": status["state"]},
        )

    def _analysis_status(self, params: ProjectAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            status = store.analysis(params.analysis_id)
            runs, summaries, claims = store.analysis_results(params.analysis_id)
            direct_job_counts = store.direct_job_counts(params.analysis_id)
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.status",
            {
                "analysis_id": params.analysis_id,
                "state": status["state"],
                "plan": status["plan"],
                "completed_run_count": len(runs),
                "summary_count": len(summaries),
                "evidence_claim_count": len(claims),
                "direct_job_counts": direct_job_counts,
                "error": status["error"],
            },
        )

    def _verify_reference_control_integrity(
        self, store: EcoProjectStore, analysis_id: str
    ) -> ReferenceComparisonPlan | None:
        """Fail closed before a generic lifecycle command touches a reference run."""

        analysis = store.analysis(analysis_id)
        if analysis.get("analysis_kind") != "REFERENCE":
            return None
        try:
            entry = store.reference_comparison_plan_for_analysis(analysis_id)
        except ValueError as exc:
            raise _fault(
                "REFERENCE_CONTROL_INTEGRITY_FAILED",
                "The immutable reference plan failed verification before the lifecycle command.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=analysis_id,
                technical_message=type(exc).__name__,
            ) from exc
        if entry is None:
            raise _fault(
                "REFERENCE_CONTROL_PLAN_MISSING",
                "The reference analysis cannot be controlled without its immutable reference plan.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=analysis_id,
            )
        reference_plan, _ = entry
        try:
            store.store_reference_comparison_plan(reference_plan)
        except ValueError as exc:
            raise _fault(
                "REFERENCE_CONTROL_INTEGRITY_FAILED",
                "The reference analysis no longer matches its immutable plan and model snapshot.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=analysis_id,
                technical_message=type(exc).__name__,
            ) from exc
        return reference_plan

    def _analysis_pause(self, params: ProjectAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            self._verify_reference_control_integrity(store, params.analysis_id)
            _, _, executor = self._require_engine()
            executor.pause(project_directory=store.root, analysis_id=params.analysis_id)
            if store.analysis(params.analysis_id)["state"] == AnalysisState.RUNNING.value:
                # Compatibility for an explicitly injected synchronous executor.
                store.pause_analysis(params.analysis_id)
            state = store.analysis(params.analysis_id)["state"]
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.pause", {"analysis_id": params.analysis_id, "state": state}
        )

    def _analysis_resume(self, params: ProjectAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            reference_plan = self._verify_reference_control_integrity(store, params.analysis_id)
            if reference_plan is not None:
                self._require_plan_engine_manifest(reference_plan.run_plan)
            _, _, executor = self._require_engine()
            executor.resume(project_directory=store.root, analysis_id=params.analysis_id)
            if store.analysis(params.analysis_id)["state"] == AnalysisState.PAUSED.value:
                # Compatibility for an explicitly injected synchronous executor.
                store.transition_analysis(params.analysis_id, AnalysisState.RUNNING)
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.resume",
            {"analysis_id": params.analysis_id, "state": "RUNNING"},
        )

    def _analysis_cancel(self, params: ProjectAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            self._verify_reference_control_integrity(store, params.analysis_id)
            state = AnalysisState(store.analysis(params.analysis_id)["state"])
            if state in {AnalysisState.RUNNING, AnalysisState.PAUSING, AnalysisState.PAUSED}:
                _, _, executor = self._require_engine()
                executor.cancel(project_directory=store.root, analysis_id=params.analysis_id)
            if store.analysis(params.analysis_id)["state"] != AnalysisState.CANCELLED.value:
                store.transition_analysis(params.analysis_id, AnalysisState.CANCELLED)
        finally:
            store.close()
        return self._result(
            "connectivity.analysis.cancel",
            {"analysis_id": params.analysis_id, "state": "CANCELLED"},
        )

    @staticmethod
    def _stored_reference_plan(
        store: EcoProjectStore,
        params: ReferenceAnalysisParams,
    ) -> tuple[ReferenceComparisonPlan, str]:
        analysis = store.analysis(params.analysis_id)
        if analysis.get("analysis_kind") != "REFERENCE":
            raise _fault(
                "REFERENCE_START_REQUIRES_REFERENCE_ANALYSIS",
                "This analysis was not atomically created as a reference comparison.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=params.analysis_id,
            )
        try:
            entry = store.reference_comparison_plan_for_analysis(params.analysis_id)
        except ValueError as exc:
            raise _fault(
                "REFERENCE_PLAN_INTEGRITY_FAILED",
                "The immutable reference-comparison plan could not be verified.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Restore the project evidence or create a new reference analysis.",
                technical_message=type(exc).__name__,
            ) from exc
        if entry is None:
            raise _fault(
                "REFERENCE_PLAN_NOT_FOUND",
                "Create and retain a baseline reference-comparison plan before continuing.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
                object_reference=params.analysis_id,
            )
        plan, plan_sha256 = entry
        if plan.plan_id != params.plan_id:
            raise _fault(
                "REFERENCE_PLAN_ID_MISMATCH",
                "The requested reference plan does not belong to this analysis.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=params.plan_id,
            )
        return plan, plan_sha256

    def _reference_plan(self, params: ReferencePlanParams) -> PayloadResult:
        """Persist a baseline-only comparison plan before any direct work."""

        _, _, _ = self._require_engine()
        assert self.engine_manifest is not None
        store = self._store(params.project_directory)
        try:
            self._authoritative_project_context(store)
            bundle = store.effective_bundle()
            validation = self._validation_data(
                bundle,
                store=store,
                expected_engine_manifest_sha256=self.engine_manifest.sha256,
            )
            allowed_blockers = {
                "REFERENCE_COMPARISON_REQUIRED",
                "REFERENCE_COMPARISON_FAILED",
                "MODEL_FITNESS_ATTESTATION_MISSING",
                "FITNESS_BLOCKER",
            }
            blocking = [
                item
                for item in validation["findings"]
                if item["severity"] == "BLOCKING"
                and str(item["code"]) not in allowed_blockers
                and not str(item["code"]).startswith("MODEL_FITNESS_EVIDENCE_")
            ]
            if blocking:
                raise _fault(
                    "REFERENCE_MODEL_NOT_READY",
                    "Resolve the model, raster, scenario, binding, and source-evidence blockers before running a reference comparison.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation=(
                        "Use Model validation to open each blocking object. Model Fitness and "
                        "reference-comparison findings may remain pending because this direct "
                        "run supplies evidence; no fitness status is changed automatically."
                    ),
                    technical_message=canonical_json(blocking),
                )
            try:
                candidate = build_reference_comparison_plan(
                    bundle=bundle,
                    analysis_id=params.analysis_id,
                    engine_manifest_sha256=self.engine_manifest.sha256,
                    created_at=datetime.now(UTC),
                )
            except ReferenceWorkflowError as exc:
                raise _fault(
                    "REFERENCE_PLAN_INVALID",
                    str(exc),
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                ) from exc
            self._require_plan_engine_manifest(candidate.run_plan)
            try:
                plan, receipt = store.create_reference_analysis(candidate, bundle=bundle)
            except ValueError as exc:
                raise _fault(
                    "REFERENCE_ANALYSIS_ID_REUSED",
                    "The reference analysis ID belongs to different or incomplete immutable evidence.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    remediation="Create a new analysis ID for the active model revision.",
                    object_reference=params.analysis_id,
                    technical_message=type(exc).__name__,
                ) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.reference.plan",
            {
                "analysis_id": plan.analysis_id,
                "plan_id": plan.plan_id,
                "state": receipt["state"],
                "plan": plan.model_dump(mode="json"),
                "plan_sha256": receipt["plan_sha256"],
                "reference_matrix_parser_sha256": receipt["reference_matrix_parser_sha256"],
                "replayed": bool(receipt["replayed"]),
                "arithmetic": (
                    "1 No Action baseline × "
                    f"{len(plan.bound_scenario_ids)} bound scenarios = "
                    f"{plan.run_plan.direct_job_count} direct Circuitscape matrix jobs"
                ),
            },
        )

    def _reference_start(self, params: ReferenceAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            self._authoritative_project_context(store)
            plan, _ = self._stored_reference_plan(store, params)
            engine, digest, executor = self._require_engine()
            self._require_plan_engine_manifest(plan.run_plan)
            active_bundle = store.effective_bundle()
            snapshot = store.analysis_bundle(params.analysis_id)
            if (
                active_bundle.revision.revision_id != plan.revision_id
                or active_bundle.revision.model_sha256 != plan.model_sha256
                or snapshot.revision.revision_id != plan.revision_id
                or snapshot.revision.model_sha256 != plan.model_sha256
            ):
                raise _fault(
                    "REFERENCE_ANALYSIS_STALE",
                    "The planned reference comparison is not for the active model revision.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation="Create a new reference plan for the active semantic revision.",
                )
            state = str(store.analysis(params.analysis_id)["state"])
            if state == AnalysisState.COMPLETED.value:
                recover_pending_reference_results(
                    store=store,
                    analysis_id=params.analysis_id,
                )
                return self._result(
                    "connectivity.reference.start",
                    {
                        "analysis_id": params.analysis_id,
                        "plan_id": params.plan_id,
                        "state": state,
                    },
                )
            try:
                execution = executor.start(
                    project_directory=store.root,
                    bundle=snapshot,
                    plan=plan.run_plan,
                    engine_executable=engine,
                    engine_sha256=digest,
                )
            except (AnalysisOwnershipError, OrphanProcessAuditError) as exc:
                raise _fault(
                    "REFERENCE_EXECUTION_BUSY",
                    "The reference comparison cannot start until the current direct-work owner releases this project.",
                    category=ErrorCategory.RESOURCE_LIMIT,
                    severity=Severity.BLOCKING,
                    retryable=True,
                    technical_message=type(exc).__name__,
                ) from exc
            if execution.get("state") != "RUNNING":
                raise _fault(
                    "REFERENCE_EXECUTOR_PROTOCOL_VIOLATION",
                    "The durable executor returned an unsupported reference-analysis state.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            state = str(store.analysis(params.analysis_id)["state"])
        finally:
            store.close()
        return self._result(
            "connectivity.reference.start",
            {
                "analysis_id": params.analysis_id,
                "plan_id": params.plan_id,
                "state": state,
            },
        )

    def _reference_evidence_disposition(
        self,
        *,
        store: EcoProjectStore,
        plan: ReferenceComparisonPlan,
        result: ReferenceComparisonResult | None,
    ) -> dict[str, Any]:
        """Classify retained reference evidence against the active project state.

        Historical evidence remains inspectable, but only an exact match for the
        active revision, model, installed engine, and complete reviewed binding
        set can participate in the Model Fitness gate. Conflicting exact results
        fail closed instead of selecting the favorable result.
        """

        bundle = store.effective_bundle()
        stale_reasons: list[str] = []
        if (
            plan.revision_id != bundle.revision.revision_id
            or plan.model_sha256 != bundle.revision.model_sha256
        ):
            stale_reasons.append("ACTIVE_MODEL_REVISION_MISMATCH")
        if self.engine_manifest is None:
            stale_reasons.append("PINNED_ENGINE_UNAVAILABLE")
            expected_engine_sha256 = None
        else:
            expected_engine_sha256 = self.engine_manifest.sha256
            if plan.engine_manifest_sha256 != expected_engine_sha256:
                stale_reasons.append("ACTIVE_ENGINE_MISMATCH")
        try:
            expected_plan = build_reference_comparison_plan(
                bundle=bundle,
                analysis_id=plan.analysis_id,
                engine_manifest_sha256=plan.engine_manifest_sha256,
                created_at=plan.created_at,
            )
        except (ReferenceWorkflowError, ValueError):
            stale_reasons.append("ACTIVE_BINDING_SET_UNAVAILABLE")
        else:
            if expected_plan != plan:
                stale_reasons.append("ACTIVE_BINDING_OR_PLAN_MISMATCH")

        if result is None:
            if stale_reasons:
                return {
                    "active_for_current_model": False,
                    "gates_model_fitness": False,
                    "evidence_disposition": "HISTORICAL_STALE",
                    "stale_reasons": sorted(set(stale_reasons)),
                }
            return {
                "active_for_current_model": True,
                "gates_model_fitness": False,
                "evidence_disposition": "PENDING",
                "stale_reasons": [],
            }

        if (
            result.plan_id != plan.plan_id
            or result.revision_id != bundle.revision.revision_id
            or result.model_sha256 != bundle.revision.model_sha256
            or expected_engine_sha256 is None
            or result.engine_manifest_sha256 != expected_engine_sha256
        ):
            stale_reasons.append("RESULT_IDENTITY_MISMATCH")
        if stale_reasons:
            return {
                "active_for_current_model": False,
                "gates_model_fitness": False,
                "evidence_disposition": "HISTORICAL_STALE",
                "stale_reasons": sorted(set(stale_reasons)),
            }

        assert expected_engine_sha256 is not None
        individual_codes = {
            item.code
            for item in validate_professional_bundle(
                bundle,
                authoritative_reference_results=[result],
                expected_engine_manifest_sha256=expected_engine_sha256,
            )
            if item.code.startswith("REFERENCE_COMPARISON_")
        }
        expected_individual_code = (
            "REFERENCE_COMPARISON_PASSED"
            if result.status == "PASSED"
            else "REFERENCE_COMPARISON_FAILED"
        )
        if individual_codes != {expected_individual_code}:
            return {
                "active_for_current_model": False,
                "gates_model_fitness": False,
                "evidence_disposition": "HISTORICAL_STALE",
                "stale_reasons": ["ACTIVE_BINDING_EVIDENCE_MISMATCH"],
            }

        other_current_results = store.reference_comparison_results(
            revision_id=bundle.revision.revision_id,
            model_sha256=bundle.revision.model_sha256,
            engine_manifest_sha256=expected_engine_sha256,
            exclude_result_ids=(result.result_id,),
        )
        current_result_values = [result, *(item[0] for item in other_current_results)]
        current_codes = {
            item.code
            for item in validate_professional_bundle(
                bundle,
                authoritative_reference_results=current_result_values,
                expected_engine_manifest_sha256=expected_engine_sha256,
            )
            if item.code.startswith("REFERENCE_COMPARISON_")
        }
        if "REFERENCE_COMPARISON_CONFLICT" in current_codes:
            return {
                "active_for_current_model": False,
                "gates_model_fitness": False,
                "evidence_disposition": "CONFLICT_NON_GATING",
                "stale_reasons": ["CONFLICTING_CURRENT_RESULTS"],
            }
        return {
            "active_for_current_model": True,
            "gates_model_fitness": True,
            "evidence_disposition": f"ACTIVE_{result.status}",
            "stale_reasons": [],
        }

    def _reference_status(self, params: ReferenceAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            plan, _ = self._stored_reference_plan(store, params)
            recover_pending_reference_results(
                store=store,
                analysis_id=params.analysis_id,
            )
            status = store.analysis(params.analysis_id)
            runs, _, _ = store.analysis_results(params.analysis_id)
            results = store.reference_comparison_results(analysis_id=params.analysis_id)
            if len(results) > 1:
                raise _fault(
                    "REFERENCE_RESULT_AMBIGUOUS",
                    "The reference analysis has more than one immutable result.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            result = results[0][0] if results else None
            disposition = self._reference_evidence_disposition(
                store=store,
                plan=plan,
                result=result,
            )
            counts = store.direct_job_counts(params.analysis_id)
        except (OSError, ValueError) as exc:
            if isinstance(exc, RPCFault):
                raise
            raise _fault(
                "REFERENCE_STATUS_INTEGRITY_FAILED",
                "Reference-analysis status could not be verified from local evidence.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=type(exc).__name__,
            ) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.reference.status",
            {
                "analysis_id": params.analysis_id,
                "plan_id": params.plan_id,
                "state": status["state"],
                "plan": plan.model_dump(mode="json"),
                "completed_run_count": len(runs),
                "direct_job_counts": counts,
                "result_id": result.result_id if result is not None else None,
                "result_status": result.status if result is not None else None,
                **disposition,
                "error": status["error"],
            },
        )

    def _reference_result(self, params: ReferenceAnalysisParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            plan, _ = self._stored_reference_plan(store, params)
            recover_pending_reference_results(
                store=store,
                analysis_id=params.analysis_id,
            )
            existing = store.reference_comparison_results(analysis_id=params.analysis_id)
            if len(existing) > 1:
                raise _fault(
                    "REFERENCE_RESULT_AMBIGUOUS",
                    "The reference analysis has more than one immutable result.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            if existing:
                result, claims = existing[0]
                _, plan_sha256 = store.reference_comparison_plan(
                    plan.plan_id,
                    analysis_id=plan.analysis_id,
                )
                receipt = store.store_reference_comparison_result(result, claims=claims)
            else:
                self._require_engine()
                self._require_plan_engine_manifest(plan.run_plan)
                try:
                    result, claims, receipt = assemble_and_store_reference_result(
                        store=store,
                        plan=plan,
                    )
                except ReferenceWorkflowError as exc:
                    raise _fault(
                        "REFERENCE_RESULT_NOT_READY",
                        str(exc),
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        remediation=(
                            "Wait for every bound baseline scenario to finish with accepted QA, "
                            "then retry result creation."
                        ),
                    ) from exc
                plan_sha256 = str(receipt["plan_sha256"])
            disposition = self._reference_evidence_disposition(
                store=store,
                plan=plan,
                result=result,
            )
        except (KeyError, OSError, ValueError) as exc:
            if isinstance(exc, RPCFault):
                raise
            raise _fault(
                "REFERENCE_RESULT_INTEGRITY_FAILED",
                "The reference result could not be assembled from exact retained evidence.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                technical_message=type(exc).__name__,
            ) from exc
        finally:
            store.close()
        return self._result(
            "connectivity.reference.result",
            {
                "analysis_id": result.analysis_id,
                "plan_id": result.plan_id,
                "plan": plan.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "claim_ids": sorted(item.claim_id for item in claims),
                "plan_sha256": plan_sha256,
                "result_sha256": receipt["result_sha256"],
                "reference_matrix_parser_sha256": receipt["reference_matrix_parser_sha256"],
                "outbox_id": receipt["outbox_id"],
                "replayed": bool(receipt["replayed"]),
                **disposition,
            },
        )

    def _decision_board(self, params: DecisionBoardParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            status = store.analysis(params.analysis_id)
            if status["state"] != "COMPLETED":
                raise _fault(
                    "DECISION_BOARD_ANALYSIS_INCOMPLETE",
                    "The decision board requires a completed direct scenario ensemble.",
                    category=ErrorCategory.MODEL_QUALITY,
                )
            runs, summaries, claims = store.analysis_results(params.analysis_id)
            if params.portfolio_ids:
                selected = set(params.portfolio_ids)
                summaries = [item for item in summaries if item.portfolio_id in selected]
                runs = [item for item in runs if item.portfolio_id in selected]
                claims = [item for item in claims if item.portfolio_id in selected]
            if not summaries:
                raise _fault(
                    "DECISION_BOARD_NO_DIRECT_RESULTS",
                    "No directly evaluated portfolios match this decision-board request.",
                    category=ErrorCategory.MODEL_QUALITY,
                )
            bundle = store.analysis_bundle(params.analysis_id)
            for summary in summaries:
                portfolio_runs = [
                    item for item in runs if item.portfolio_id == summary.portfolio_id
                ]
                complete, errors = validate_complete_ensemble(
                    portfolio_runs,
                    bundle.scenarios,
                    portfolio_id=summary.portfolio_id,
                )
                if not complete:
                    raise _fault(
                        "DECISION_BOARD_ENSEMBLE_INCOMPLETE",
                        "The portfolio does not have a complete accepted direct scenario ensemble.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=summary.portfolio_id,
                        technical_message="; ".join(errors),
                    )
        finally:
            store.close()
        return self._result(
            "connectivity.results.decision_board",
            {
                "analysis_id": params.analysis_id,
                "summaries": [item.model_dump(mode="json") for item in summaries],
                "direct_run_ids": sorted(item.direct_run_id for item in runs),
                "evidence_claim_ids": sorted(item.claim_id for item in claims),
                "claim_language": "Direct Circuitscape result under documented assumptions.",
            },
        )

    @staticmethod
    def _scenario_detail_run_identity(run: DirectConnectivityRun) -> dict[str, Any]:
        if (
            run.state.value != "SUCCEEDED"
            or not run.exact
            or run.test_only
            or run.engine_name != "Circuitscape"
            or run.result_sha256 is None
            or run.matrix_sha256 is None
            or run.qa_status is None
        ):
            raise _fault(
                "SCENARIO_DETAIL_DIRECT_RUN_INVALID",
                "The selected scenario does not have a reportable direct Circuitscape run.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            )
        return {
            "direct_run_id": run.direct_run_id,
            "portfolio_id": run.portfolio_id,
            "scenario_id": run.scenario_id,
            "exact": True,
            "engine_name": "Circuitscape",
            "engine_manifest_sha256": run.engine_manifest_sha256,
            "model_run_cache_key": run.model_run_cache_key,
            "result_sha256": run.result_sha256,
            "matrix_sha256": run.matrix_sha256,
            "current_map_sha256": run.current_map_sha256,
            "qa_status": run.qa_status.value,
            "qa_findings": run.qa_findings,
        }

    @staticmethod
    def _verified_scenario_artifact(
        store: EcoProjectStore,
        run: DirectConnectivityRun,
        role: str,
        *,
        required: bool,
    ) -> tuple[dict[str, Any], Path] | None:
        records = [item for item in store.run_artifacts(run.direct_run_id) if item["role"] == role]
        if not records:
            if required:
                raise _fault(
                    "SCENARIO_DETAIL_ARTIFACT_MISSING",
                    f"The retained {role.replace('_', ' ')} artifact is missing.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=run.direct_run_id,
                )
            return None
        if len(records) != 1:
            raise _fault(
                "SCENARIO_DETAIL_ARTIFACT_AMBIGUOUS",
                "The direct run has more than one artifact for the requested evidence role.",
                category=ErrorCategory.INTERNAL,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            )
        record = records[0]
        try:
            path = contained_path(store.root, str(record["cas_relative_path"]))
        except PathSecurityError as exc:
            raise _fault(
                "SCENARIO_DETAIL_ARTIFACT_PATH_INVALID",
                "A retained direct-run artifact is outside authoritative project storage.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            ) from exc
        try:
            valid = (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == int(record["byte_size"])
                and _sha256_file(path) == str(record["sha256"])
            )
        except OSError as exc:
            raise _fault(
                "SCENARIO_DETAIL_ARTIFACT_UNREADABLE",
                "A retained direct-run artifact could not be read safely.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            ) from exc
        if not valid:
            raise _fault(
                "SCENARIO_DETAIL_ARTIFACT_INTEGRITY_FAILED",
                "A retained direct-run artifact failed hash and size verification.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            )
        expected_sha256 = {
            "matrix": run.matrix_sha256,
            "current_map": run.current_map_sha256,
        }.get(role)
        if expected_sha256 is not None and str(record["sha256"]) != expected_sha256:
            raise _fault(
                "SCENARIO_DETAIL_ARTIFACT_IDENTITY_MISMATCH",
                "A retained artifact differs from the immutable direct-run identity.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=run.direct_run_id,
            )
        return record, path

    @staticmethod
    def _current_map_absence(*, baseline_missing: bool, portfolio_missing: bool) -> dict[str, Any]:
        if baseline_missing and portfolio_missing:
            code = "CURRENT_MAPS_NOT_RETAINED"
            reason = (
                "Neither within-scenario run retained a current raster. Pair evidence remains "
                "available; rerun this portfolio and its baseline with current-map output enabled."
            )
        elif baseline_missing:
            code = "BASELINE_CURRENT_MAP_NOT_RETAINED"
            reason = (
                "The within-scenario baseline did not retain a current raster. Pair evidence "
                "remains available; rerun the baseline with current-map output enabled."
            )
        else:
            code = "PORTFOLIO_CURRENT_MAP_NOT_RETAINED"
            reason = (
                "The selected portfolio did not retain a current raster. Pair evidence remains "
                "available; rerun this portfolio with current-map output enabled."
            )
        return {
            "status": "ABSENT",
            "absence_code": code,
            "absence_reason": reason,
            "evidence_claim_ids": [],
            "interpretation": (
                "Diagnostic current redistribution; not observed or predicted animal use."
            ),
        }

    def _scenario_map_evidence(
        self,
        *,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        scenario: ConnectivityScenario,
        baseline_run: DirectConnectivityRun,
        portfolio_run: DirectConnectivityRun,
        claims: list[ConnectivityEvidenceClaim],
        cell_offset: int,
        cell_limit: int,
    ) -> dict[str, Any]:
        baseline_artifact = self._verified_scenario_artifact(
            store, baseline_run, "current_map", required=False
        )
        portfolio_artifact = (
            baseline_artifact
            if portfolio_run.direct_run_id == baseline_run.direct_run_id
            else self._verified_scenario_artifact(
                store, portfolio_run, "current_map", required=False
            )
        )
        baseline_missing = baseline_artifact is None
        portfolio_missing = portfolio_artifact is None
        if baseline_missing or portfolio_missing:
            if (baseline_run.current_map_sha256 is None) != baseline_missing or (
                portfolio_run.current_map_sha256 is None
            ) != portfolio_missing:
                raise _fault(
                    "SCENARIO_DETAIL_CURRENT_MAP_RECORD_INCONSISTENT",
                    "Current-map metadata differs from the retained direct-run artifacts.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=scenario.scenario_id,
                )
            return self._current_map_absence(
                baseline_missing=baseline_missing,
                portfolio_missing=portfolio_missing,
            )

        assert baseline_artifact is not None and portfolio_artifact is not None
        if baseline_run.current_map_sha256 is None or portfolio_run.current_map_sha256 is None:
            raise _fault(
                "SCENARIO_DETAIL_CURRENT_MAP_RECORD_INCONSISTENT",
                "A retained current-map artifact is absent from the immutable run identity.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=scenario.scenario_id,
            )
        hypothesis = next(
            (
                item
                for item in bundle.resistance_hypotheses
                if item.hypothesis_id == scenario.resistance_hypothesis_id
            ),
            None,
        )
        asset_by_id = {item.asset_id: item for item in bundle.assets}
        resistance_asset = (
            asset_by_id.get(hypothesis.resistance_asset_id) if hypothesis is not None else None
        )
        grid = resistance_asset.grid if resistance_asset is not None else None
        if grid is None or grid.grid_id != scenario.grid_id:
            raise _fault(
                "SCENARIO_DETAIL_GRID_UNAVAILABLE",
                "The immutable scenario does not resolve to one reviewed raster grid.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
                object_reference=scenario.scenario_id,
            )
        baseline_record, baseline_path = baseline_artifact
        portfolio_record, portfolio_path = portfolio_artifact
        try:
            baseline_values, baseline_nodata = current_map_values_and_nodata(
                baseline_path, expected_grid=grid
            )
            portfolio_values, portfolio_nodata = current_map_values_and_nodata(
                portfolio_path, expected_grid=grid
            )
        except RasterIOError as exc:
            raise _fault(
                "SCENARIO_DETAIL_CURRENT_MAP_INVALID",
                "A retained current raster no longer matches the reviewed scenario grid.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
                object_reference=scenario.scenario_id,
                technical_message=str(exc),
            ) from exc

        baseline_flat: list[float | None] = []
        portfolio_flat: list[float | None] = []
        for baseline_row, portfolio_row in zip(baseline_values, portfolio_values, strict=True):
            for baseline_value, portfolio_value in zip(baseline_row, portfolio_row, strict=True):
                baseline_is_nodata = _is_nodata(baseline_value, baseline_nodata)
                portfolio_is_nodata = _is_nodata(portfolio_value, portfolio_nodata)
                if baseline_is_nodata != portfolio_is_nodata:
                    raise _fault(
                        "SCENARIO_DETAIL_CURRENT_MAP_NODATA_MISMATCH",
                        "Baseline and portfolio current rasters have different NODATA topology.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=scenario.scenario_id,
                    )
                if baseline_is_nodata:
                    baseline_flat.append(None)
                    portfolio_flat.append(None)
                    continue
                baseline_number = float(baseline_value)
                portfolio_number = float(portfolio_value)
                if (
                    not math.isfinite(baseline_number)
                    or not math.isfinite(portfolio_number)
                    or baseline_number < 0
                    or portfolio_number < 0
                ):
                    raise _fault(
                        "SCENARIO_DETAIL_CURRENT_MAP_VALUE_INVALID",
                        "A retained current raster contains a nonfinite or negative value.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=scenario.scenario_id,
                    )
                baseline_flat.append(baseline_number)
                portfolio_flat.append(portfolio_number)
        valid_baseline = [item for item in baseline_flat if item is not None]
        valid_portfolio = [item for item in portfolio_flat if item is not None]
        if not valid_baseline or not valid_portfolio:
            raise _fault(
                "SCENARIO_DETAIL_CURRENT_MAP_EMPTY",
                "The retained current rasters contain no comparable data cells.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
                object_reference=scenario.scenario_id,
            )

        total_cells = grid.width * grid.height
        if len(baseline_flat) != total_cells or cell_offset >= total_cells:
            raise _fault(
                "SCENARIO_DETAIL_CELL_PAGE_INVALID",
                "The requested exact-cell page is outside the retained raster grid.",
                category=ErrorCategory.USER_FIXABLE,
                object_reference=scenario.scenario_id,
                remediation="Choose a cell offset smaller than the reported total cell count.",
            )
        page_end = min(total_cells, cell_offset + cell_limit)
        transform = grid.transform
        exact_cells: list[dict[str, Any]] = []
        for index in range(cell_offset, page_end):
            row, column = divmod(index, grid.width)
            baseline_value = baseline_flat[index]
            portfolio_value = portfolio_flat[index]
            exact_cells.append(
                {
                    "linear_index": index,
                    "row": row,
                    "column": column,
                    "x_center": (
                        transform[0] * (column + 0.5) + transform[1] * (row + 0.5) + transform[2]
                    ),
                    "y_center": (
                        transform[3] * (column + 0.5) + transform[4] * (row + 0.5) + transform[5]
                    ),
                    "baseline_current": baseline_value,
                    "portfolio_current": portfolio_value,
                    "portfolio_minus_baseline": (
                        None
                        if baseline_value is None or portfolio_value is None
                        else portfolio_value - baseline_value
                    ),
                }
            )

        preview_width = min(grid.width, 64)
        preview_height = min(grid.height, 64)
        preview_cells: list[dict[str, Any]] = []
        for preview_row in range(preview_height):
            source_row = min(
                grid.height - 1,
                int((preview_row + 0.5) * grid.height / preview_height),
            )
            for preview_column in range(preview_width):
                source_column = min(
                    grid.width - 1,
                    int((preview_column + 0.5) * grid.width / preview_width),
                )
                source_index = source_row * grid.width + source_column
                preview_cells.append(
                    {
                        "preview_row": preview_row,
                        "preview_column": preview_column,
                        "source_row": source_row,
                        "source_column": source_column,
                        "baseline_current": baseline_flat[source_index],
                        "portfolio_current": portfolio_flat[source_index],
                    }
                )

        expected_claim_runs = {baseline_run.direct_run_id, portfolio_run.direct_run_id}
        map_claim_ids = sorted(
            item.claim_id
            for item in claims
            if item.claim_kind == "DIRECT_MAP_DIAGNOSTIC"
            and item.portfolio_id == portfolio_run.portfolio_id
            and item.scenario_id == scenario.scenario_id
            and set(item.direct_run_ids) == expected_claim_runs
        )

        def artifact_identity(run: DirectConnectivityRun, record: dict[str, Any]) -> dict[str, Any]:
            return {
                "direct_run_id": run.direct_run_id,
                "artifact_id": f"artifact:{run.direct_run_id}:current_map",
                "sha256": str(record["sha256"]),
                "byte_size": int(record["byte_size"]),
                "media_type": str(record["media_type"]),
            }

        return {
            "status": "AVAILABLE",
            "grid": grid.model_dump(mode="json"),
            "baseline_artifact": artifact_identity(baseline_run, baseline_record),
            "portfolio_artifact": artifact_identity(portfolio_run, portfolio_record),
            "evidence_claim_ids": map_claim_ids,
            "preview": {
                "width": preview_width,
                "height": preview_height,
                "selection_method": "SYSTEMATIC_EXACT_CELL_SAMPLE",
                "baseline_minimum": min(valid_baseline),
                "baseline_maximum": max(valid_baseline),
                "portfolio_minimum": min(valid_portfolio),
                "portfolio_maximum": max(valid_portfolio),
                "cells": preview_cells,
            },
            "exact_cell_page": {
                "offset": cell_offset,
                "limit": cell_limit,
                "total_cell_count": total_cells,
                "has_previous": cell_offset > 0,
                "has_next": page_end < total_cells,
                "cells": exact_cells,
            },
            "interpretation": (
                "Diagnostic current redistribution; not observed or predicted animal use."
            ),
        }

    def _scenario_detail(self, params: ScenarioDetailParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            analysis = store.analysis(params.analysis_id)
            if analysis["state"] != "COMPLETED":
                raise _fault(
                    "SCENARIO_DETAIL_ANALYSIS_INCOMPLETE",
                    "Scenario evidence requires a completed direct scenario ensemble.",
                    category=ErrorCategory.MODEL_QUALITY,
                    object_reference=params.analysis_id,
                )
            plan = ConnectivityRunPlan.model_validate(analysis["plan"])
            if params.portfolio_id not in {item.portfolio_id for item in plan.portfolios}:
                raise _fault(
                    "SCENARIO_DETAIL_PORTFOLIO_NOT_PLANNED",
                    "The selected portfolio is absent from the immutable analysis plan.",
                    category=ErrorCategory.USER_FIXABLE,
                    object_reference=params.portfolio_id,
                )
            if params.scenario_id not in set(plan.scenario_ids):
                raise _fault(
                    "SCENARIO_DETAIL_SCENARIO_NOT_PLANNED",
                    "The selected scenario is absent from the immutable analysis plan.",
                    category=ErrorCategory.USER_FIXABLE,
                    object_reference=params.scenario_id,
                )
            bundle = store.analysis_bundle(params.analysis_id)
            scenario_by_id = {
                item.scenario_id: item
                for item in bundle.scenarios
                if item.scenario_id in set(plan.scenario_ids)
            }
            if set(scenario_by_id) != set(plan.scenario_ids):
                raise _fault(
                    "SCENARIO_DETAIL_MODEL_SNAPSHOT_INVALID",
                    "The immutable analysis model omits a planned ecological scenario.",
                    category=ErrorCategory.INTERNAL,
                    severity=Severity.BLOCKING,
                    object_reference=params.analysis_id,
                )
            scenario = scenario_by_id[params.scenario_id]
            runs, summaries, claims = store.analysis_results(params.analysis_id)
            summary = next(
                (item for item in summaries if item.portfolio_id == params.portfolio_id), None
            )
            if summary is None:
                raise _fault(
                    "SCENARIO_DETAIL_PORTFOLIO_INCOMPLETE",
                    "The selected portfolio has no completed decision summary.",
                    category=ErrorCategory.MODEL_QUALITY,
                    object_reference=params.portfolio_id,
                )
            planned_scenarios = [scenario_by_id[item] for item in plan.scenario_ids]
            for portfolio_id in {params.portfolio_id, plan.baseline_portfolio_id}:
                complete, errors = validate_complete_ensemble(
                    [item for item in runs if item.portfolio_id == portfolio_id],
                    planned_scenarios,
                    portfolio_id=portfolio_id,
                )
                if not complete:
                    raise _fault(
                        "SCENARIO_DETAIL_ENSEMBLE_INCOMPLETE",
                        "The requested comparison lacks a complete accepted scenario ensemble.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=portfolio_id,
                        technical_message="; ".join(errors),
                    )
            baseline_matches = [
                item
                for item in runs
                if item.portfolio_id == plan.baseline_portfolio_id
                and item.scenario_id == scenario.scenario_id
            ]
            portfolio_matches = [
                item
                for item in runs
                if item.portfolio_id == params.portfolio_id
                and item.scenario_id == scenario.scenario_id
            ]
            if len(baseline_matches) != 1 or len(portfolio_matches) != 1:
                raise _fault(
                    "SCENARIO_DETAIL_DIRECT_RUN_AMBIGUOUS",
                    "The selected scenario does not resolve to one baseline and one portfolio run.",
                    category=ErrorCategory.INTERNAL,
                    severity=Severity.BLOCKING,
                    object_reference=scenario.scenario_id,
                )
            baseline_run = baseline_matches[0]
            portfolio_run = portfolio_matches[0]
            if baseline_run.engine_manifest_sha256 != portfolio_run.engine_manifest_sha256:
                raise _fault(
                    "SCENARIO_DETAIL_ENGINE_MISMATCH",
                    "The baseline and selected portfolio used different engine manifests.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=scenario.scenario_id,
                )
            self._verified_scenario_artifact(store, baseline_run, "matrix", required=True)
            if portfolio_run.direct_run_id != baseline_run.direct_run_id:
                self._verified_scenario_artifact(store, portfolio_run, "matrix", required=True)

            focal_set = next(
                (
                    item
                    for item in bundle.focal_area_sets
                    if item.focal_set_id == scenario.focal_set_id
                ),
                None,
            )
            if focal_set is None or focal_set.guild_id != scenario.guild_id:
                raise _fault(
                    "SCENARIO_DETAIL_FOCAL_SCOPE_INVALID",
                    "The scenario focal set does not match its reviewed guild scope.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=scenario.scenario_id,
                )
            focal_pairs = {item.pair_id: item for item in focal_set.pairs}
            baseline_metrics = {item.pair_id: item for item in baseline_run.pair_metrics}
            portfolio_metrics = {item.pair_id: item for item in portfolio_run.pair_metrics}
            required_pair_ids = scenario.included_pair_ids
            if (
                set(baseline_metrics) != set(required_pair_ids)
                or set(portfolio_metrics) != set(required_pair_ids)
                or any(item not in focal_pairs for item in required_pair_ids)
            ):
                raise _fault(
                    "SCENARIO_DETAIL_PAIR_SCOPE_INVALID",
                    "Direct pair metrics differ from the selected scenario's focal-pair contract.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    object_reference=scenario.scenario_id,
                )
            expected_claim_runs = {baseline_run.direct_run_id, portfolio_run.direct_run_id}
            pair_rows: list[dict[str, Any]] = []
            for pair_id in required_pair_ids:
                baseline_metric = baseline_metrics[pair_id]
                portfolio_metric = portfolio_metrics[pair_id]
                baseline_value = baseline_metric.effective_resistance
                portfolio_value = portfolio_metric.effective_resistance
                reduction = (baseline_value - portfolio_value) / baseline_value * 100.0
                if (
                    portfolio_metric.baseline_effective_resistance is None
                    or portfolio_metric.baseline_relative_change_percent is None
                    or not math.isclose(
                        portfolio_metric.baseline_effective_resistance,
                        baseline_value,
                        rel_tol=0.0,
                        abs_tol=1e-10,
                    )
                    or not math.isclose(
                        portfolio_metric.baseline_relative_change_percent,
                        reduction,
                        rel_tol=0.0,
                        abs_tol=1e-10,
                    )
                ):
                    raise _fault(
                        "SCENARIO_DETAIL_BASELINE_LINK_INVALID",
                        "A portfolio pair metric differs from its within-scenario baseline run.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=pair_id,
                    )
                pair_claims = sorted(
                    (
                        item
                        for item in claims
                        if item.claim_kind == "DIRECT_PAIR_METRIC"
                        and item.portfolio_id == params.portfolio_id
                        and item.scenario_id == scenario.scenario_id
                        and item.pair_id == pair_id
                        and set(item.direct_run_ids) == expected_claim_runs
                    ),
                    key=lambda item: item.claim_id,
                )
                primary_claim = next(
                    (
                        item
                        for item in pair_claims
                        if item.rule_id == "evidence:within-scenario-resistance-change"
                    ),
                    None,
                )
                if (
                    primary_claim is None
                    or primary_claim.units != "percent"
                    or not isinstance(primary_claim.value, (int, float))
                    or not math.isclose(
                        float(primary_claim.value), reduction, rel_tol=0.0, abs_tol=1e-10
                    )
                ):
                    raise _fault(
                        "SCENARIO_DETAIL_PAIR_CLAIM_MISSING",
                        "A displayed pair value has no matching retained direct evidence claim.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=pair_id,
                    )
                pair_rows.append(
                    {
                        "pair_id": pair_id,
                        "required": focal_pairs[pair_id].required,
                        "baseline_effective_resistance": baseline_value,
                        "portfolio_effective_resistance": portfolio_value,
                        "baseline_relative_reduction_percent": reduction,
                        "baseline_direct_run_id": baseline_run.direct_run_id,
                        "portfolio_direct_run_id": portfolio_run.direct_run_id,
                        "baseline_matrix_sha256": baseline_run.matrix_sha256,
                        "portfolio_matrix_sha256": portfolio_run.matrix_sha256,
                        "evidence_claim_ids": [item.claim_id for item in pair_claims],
                        "primary_evidence_claim_id": primary_claim.claim_id,
                    }
                )

            map_evidence = self._scenario_map_evidence(
                store=store,
                bundle=bundle,
                scenario=scenario,
                baseline_run=baseline_run,
                portfolio_run=portfolio_run,
                claims=claims,
                cell_offset=params.cell_offset,
                cell_limit=params.cell_limit,
            )
            response = {
                "analysis_id": params.analysis_id,
                "portfolio_id": params.portfolio_id,
                "baseline_portfolio_id": plan.baseline_portfolio_id,
                "model_revision_id": plan.revision_id,
                "scenario": scenario.model_dump(mode="json"),
                "baseline_run": self._scenario_detail_run_identity(baseline_run),
                "portfolio_run": self._scenario_detail_run_identity(portfolio_run),
                "pair_evidence": pair_rows,
                "map_evidence": map_evidence,
                "same_scenario_only": True,
                "claim_language": "Direct Circuitscape result under documented assumptions.",
                "limitations": [CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
            }
        finally:
            store.close()
        return self._result("connectivity.results.scenario_detail", response)

    def _evidence_resolve(self, params: EvidenceResolveParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            if store.project()["mode"] == "GUIDED" and self.guided_evidence is not None:
                materialized = self.guided_evidence.materialize(self.data_root / "guided-evidence")
                resolved = self.guided_evidence.resolve_claim(params.claim_id, materialized)
            else:
                resolved = store.resolve_evidence_claim(params.claim_id)
                validate_evidence_claim(ConnectivityEvidenceClaim.model_validate(resolved["claim"]))
        except GuidedEvidenceError as exc:
            raise _fault(
                str(exc),
                "The installed Ridgeway evidence package failed integrity verification.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            ) from exc
        except KeyError as exc:
            raise _fault(
                "EVIDENCE_CLAIM_NOT_FOUND",
                "The requested evidence claim does not exist in this project.",
                category=ErrorCategory.USER_FIXABLE,
                object_reference=params.claim_id,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise _fault(
                "EVIDENCE_CHAIN_INTEGRITY_FAILED",
                "The requested evidence chain failed authoritative integrity verification.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=params.claim_id,
            ) from exc
        finally:
            store.close()
        return self._result("connectivity.evidence.resolve", resolved)

    @staticmethod
    def _authoritative_project_context(
        store: EcoProjectStore,
    ) -> tuple[str, Literal["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE"], bool]:
        """Derive export/cloud policy from authoritative project state only.

        The renderer cannot downgrade a professional project to a learning export,
        claim a different deployment, or fabricate synchronization state.  A
        professional project may continue offline only after a complete exact-scope
        binding was persisted by the worker.
        """

        project = store.project()
        mode = str(project["mode"])
        if mode == "GUIDED":
            return "LOCAL_ONLY", "CONNECTED", False
        if mode not in {"PLANNING", "IMPORTED"}:
            raise _fault(
                "PROJECT_MODE_UNSUPPORTED",
                "This project mode cannot create a connectivity evidence package.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        binding = store.observability()
        if binding is None:
            raise _fault(
                "OBSERVABILITY_BINDING_REQUIRED",
                "Professional analysis and export require a previously verified Restricted W&B binding.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Verify the exact project scope through the supported W&B onboarding flow.",
            )
        try:
            scope = ProjectScopeEvidence.model_validate(binding["scope_evidence"])
            deployment = str(binding["deployment"])
            if deployment not in {"WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"}:
                raise ValueError("unsupported professional deployment")
            if (
                scope.scope.value != "RESTRICTED"
                or scope.entity != str(binding["entity"])
                or scope.project != str(binding["project"])
                or scope.deployment != deployment
                or scope.external_project_id != str(binding["external_project_id"])
                or str(binding.get("profile_namespace")) != APP_PROFILE.namespace
            ):
                raise ValueError("binding identity differs from exact scope evidence")
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise _fault(
                "OBSERVABILITY_BINDING_NOT_VERIFIED",
                "The persisted W&B binding is not complete exact Restricted-scope evidence.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Revalidate the destination through a documented exact-scope resolver.",
            ) from exc
        state = str(binding.get("state", ""))
        if state == "BLOCKED_PROJECT_POLICY":
            raise _fault(
                "BLOCKED_PROJECT_POLICY",
                "Project data authorization blocks the professional W&B workflow.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Resolve every asset representation and destination authorization.",
            )
        if state == "CONNECTED":
            sync_state: Literal["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE"] = "CONNECTED"
        elif state == "SYNCING":
            sync_state = "SYNCING"
        elif state in {"SYNC_QUEUED_OFFLINE", "FAILED_RETRYING"}:
            # Exact scope was previously verified; retain local authority and
            # represent pending lineage truthfully without trusting renderer input.
            sync_state = "SYNC_QUEUED_OFFLINE"
        else:
            raise _fault(
                "OBSERVABILITY_BINDING_NOT_ACTIVE",
                "The professional W&B binding has not completed verified onboarding.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Complete or revalidate W&B Models and Weave access.",
            )
        return deployment, sync_state, True

    @staticmethod
    def _export_artifact_sources_from_verified(verified: Any) -> list[EvidenceArtifactSource]:
        """Project verified artifacts into the path-private export boundary."""

        evidence_roles = {
            "request",
            "result_protocol",
            "matrix",
            "current_map",
            "input_resistance",
            "input_focal",
            "input_included_pairs",
            "circuitscape_configuration",
            "action_raster_diff",
            "footprint_proof_document",
        }
        observed_roles: set[str] = set()
        sources: list[EvidenceArtifactSource] = []
        for record in verified.artifacts:
            role = str(record.get("role", ""))
            if role in {"stdout", "stderr"}:
                # The authoritative verifier hashes these retained logs, but they are never
                # exported because console output may disclose local filesystem paths.
                continue
            if (
                role not in evidence_roles and not role.startswith("reviewed_footprint_source:")
            ) or role in observed_roles:
                raise _fault(
                    "EXPORT_DIRECT_ARTIFACTS_INVALID",
                    "Retained direct-run evidence contains an unsupported or duplicate role.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=f"{verified.run.direct_run_id}:{role or 'missing-role'}",
                )
            source_path = verified.artifact_paths_by_role.get(role)
            if source_path is None:
                raise _fault(
                    "EXPORT_DIRECT_ARTIFACTS_UNSAFE",
                    "A retained direct-run artifact has no verified local descriptor path.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=f"{verified.run.direct_run_id}:{role}",
                )
            digest = str(record.get("sha256", ""))
            media_type = str(record.get("media_type", ""))
            try:
                byte_size = int(record.get("byte_size", -1))
            except (TypeError, ValueError) as exc:
                raise _fault(
                    "EXPORT_DIRECT_ARTIFACTS_INTEGRITY_FAILED",
                    "A retained direct-run artifact has invalid size metadata.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=f"{verified.run.direct_run_id}:{role}",
                ) from exc
            if (
                not source_path.is_file()
                or source_path.is_symlink()
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not media_type
                or source_path.stat().st_size != byte_size
                or _sha256_file(source_path) != digest
            ):
                raise _fault(
                    "EXPORT_DIRECT_ARTIFACTS_INTEGRITY_FAILED",
                    "A retained direct-run artifact failed hash and size verification.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=f"{verified.run.direct_run_id}:{role}",
                )
            observed_roles.add(role)
            sources.append(
                EvidenceArtifactSource(
                    direct_run_id=verified.run.direct_run_id,
                    role=role,
                    source_path=source_path,
                    sha256=digest,
                    byte_size=byte_size,
                    media_type=media_type,
                )
            )
        return sources

    def _reference_export_evidence_sources(
        self,
        *,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
    ) -> list[ReferenceEvidenceExportSource]:
        """Collect every active result without choosing a favorable repeat."""

        if not bundle.reference_bindings or self.engine_manifest is None:
            return []
        try:
            recover_pending_reference_results(store=store)
            authoritative = store.reference_comparison_results(
                revision_id=bundle.revision.revision_id,
                model_sha256=bundle.revision.model_sha256,
                engine_manifest_sha256=self.engine_manifest.sha256,
            )
            sources: list[ReferenceEvidenceExportSource] = []
            for result, claims in authoritative:
                plan, plan_sha256 = store.reference_comparison_plan(
                    result.plan_id,
                    analysis_id=result.analysis_id,
                )
                disposition = self._reference_evidence_disposition(
                    store=store,
                    plan=plan,
                    result=result,
                )
                if disposition["evidence_disposition"] == "HISTORICAL_STALE":
                    raise ValueError("active result query returned stale reference evidence")
                for claim in claims:
                    resolved = store.resolve_evidence_claim(claim.claim_id)
                    if resolved.get("claim") != claim.model_dump(mode="json") or resolved.get(
                        "reference_result"
                    ) != result.model_dump(mode="json"):
                        raise ValueError("reference claim resolution differs from its result")
                comparisons_by_run = {
                    item.direct_run_id: item for item in result.scenario_comparisons
                }
                if len(comparisons_by_run) != len(result.scenario_comparisons):
                    raise ValueError("reference result has duplicate direct-run identities")
                verified_runs = []
                artifacts: list[EvidenceArtifactSource] = []
                for direct_run_id, comparison in sorted(comparisons_by_run.items()):
                    verified = store.verify_authoritative_direct_run(
                        direct_run_id,
                        expected_analysis_id=result.analysis_id,
                    )
                    if (
                        verified.run.scenario_id != comparison.scenario_id
                        or verified.run.matrix_sha256 != comparison.observed_matrix_sha256
                        or verified.run.engine_manifest_sha256 != result.engine_manifest_sha256
                    ):
                        raise ValueError(
                            "reference direct run differs from its scenario comparison"
                        )
                    verified_runs.append(verified.run)
                    artifacts.extend(self._export_artifact_sources_from_verified(verified))
                sources.append(
                    ReferenceEvidenceExportSource(
                        plan=plan,
                        plan_sha256=plan_sha256,
                        result=result,
                        result_sha256=canonical_sha256(result.model_dump(mode="json")),
                        claims=tuple(sorted(claims, key=lambda item: item.claim_id)),
                        runs=tuple(sorted(verified_runs, key=lambda item: item.direct_run_id)),
                        artifact_sources=tuple(
                            sorted(
                                artifacts,
                                key=lambda item: (item.direct_run_id, item.role),
                            )
                        ),
                    )
                )
            return sorted(sources, key=lambda item: item.result.result_id)
        except (KeyError, OSError, ReferenceWorkflowError, TypeError, ValueError) as exc:
            if isinstance(exc, RPCFault):
                raise
            raise _fault(
                "EXPORT_REFERENCE_EVIDENCE_INTEGRITY_FAILED",
                "Active reference evidence could not be verified for export.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Repair or rerun the complete reference comparison before exporting.",
                technical_message=type(exc).__name__,
            ) from exc

    def _eligibility(
        self,
        store: EcoProjectStore,
        params: ExportValidateParams,
        *,
        reference_evidence_out: list[ReferenceEvidenceExportSource] | None = None,
    ) -> tuple[
        ConnectivityExportEligibility,
        ConnectivityProjectBundle,
        list[PortfolioDecisionSummary],
        list[DirectConnectivityRun],
        list[ConnectivityEvidenceClaim],
        ConnectivityRunPlan,
    ]:
        bundle = store.analysis_bundle(params.analysis_id)
        runs, summaries, claims = store.analysis_results(params.analysis_id)
        plan = ConnectivityRunPlan.model_validate(store.analysis(params.analysis_id)["plan"])
        selected = set(params.portfolio_ids)
        summaries = [item for item in summaries if item.portfolio_id in selected]
        runs = [
            item
            for item in runs
            if item.portfolio_id in selected or item.portfolio_id == plan.baseline_portfolio_id
        ]
        retained_run_ids = {item.direct_run_id for item in runs}
        claims = [
            item
            for item in claims
            if item.portfolio_id in selected
            or (
                item.claim_kind == "DIRECT_QA"
                and item.portfolio_id == plan.baseline_portfolio_id
                and len(item.direct_run_ids) == 1
                and item.direct_run_ids[0] in retained_run_ids
            )
        ]
        if {item.portfolio_id for item in summaries} != selected:
            raise _fault(
                "EXPORT_PORTFOLIO_EVIDENCE_MISSING",
                "Every selected export portfolio requires a decision summary.",
                category=ErrorCategory.MODEL_QUALITY,
            )
        deployment, sync_state, professional = self._authoritative_project_context(store)
        current_authorization_bundle = store.effective_bundle() if professional else bundle
        reference_evidence: list[ReferenceEvidenceExportSource] = []
        if professional and (
            bundle.reference_bindings or current_authorization_bundle.reference_bindings
        ):
            if (
                current_authorization_bundle.revision.revision_id != bundle.revision.revision_id
                or current_authorization_bundle.revision.model_sha256
                != bundle.revision.model_sha256
                or current_authorization_bundle.reference_bindings != bundle.reference_bindings
            ):
                raise _fault(
                    "EXPORT_REFERENCE_MODEL_STALE",
                    "The analysis reference evidence is not for the active model and binding set.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                    remediation="Rerun the analysis and reference comparison for the active revision.",
                )
            if bundle.reference_bindings:
                reference_evidence = self._reference_export_evidence_sources(
                    store=store,
                    bundle=bundle,
                )
        if reference_evidence_out is not None:
            reference_evidence_out.extend(reference_evidence)
        eligibility = compute_export_eligibility(
            bundle=bundle,
            current_authorization_bundle=current_authorization_bundle,
            summaries=summaries,
            runs=runs,
            deployment=deployment,
            sync_state=sync_state,
            professional=professional,
            reference_evidence=reference_evidence,
            expected_engine_manifest_sha256=(
                self.engine_manifest.sha256 if self.engine_manifest is not None else None
            ),
        )
        if self.engine_manifest is None:
            eligibility = ConnectivityExportEligibility.model_validate(
                {
                    **eligibility.model_dump(mode="json"),
                    "eligible": False,
                    "qa_blockers": sorted(
                        set(
                            eligibility.qa_blockers
                            + ["Pinned EngineManifest is unavailable for export."]
                        )
                    ),
                }
            )
        binding = store.observability() or {}
        _, upload_plan = self._sync_plan(
            store,
            oversize_approved_by=binding.get("oversize_approved_by"),
        )
        resource_blockers = [
            f"Cloud evidence resource policy: {item}" for item in upload_plan["blockers"]
        ]
        if resource_blockers:
            eligibility = ConnectivityExportEligibility.model_validate(
                {
                    **eligibility.model_dump(mode="json"),
                    "eligible": False,
                    "authorization_blockers": sorted(
                        set(eligibility.authorization_blockers + resource_blockers)
                    ),
                }
            )
        return eligibility, bundle, summaries, runs, claims, plan

    def _export_validate(self, params: ExportValidateParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            eligibility, _, _, _, _, _ = self._eligibility(store, params)
        finally:
            store.close()
        return self._result(
            "connectivity.export.validate",
            {"eligibility": eligibility.model_dump(mode="json")},
        )

    def _export_create(self, params: ExportCreateParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            output = contained_path(store.root, "exports")
            if params.output_directory is not None:
                requested = self._allowed_path(
                    params.output_directory, must_exist=False, purpose="export directory"
                )
                if requested.resolve() != output.resolve():
                    raise _fault(
                        "EXPORT_DESTINATION_NOT_AUTHORITATIVE",
                        "Create the evidence package in the project before saving a separate copy.",
                        category=ErrorCategory.SECURITY,
                        remediation=(
                            "Omit output_directory or select the project's exports directory."
                        ),
                    )
            reference_evidence: list[ReferenceEvidenceExportSource] = []
            eligibility, bundle, summaries, runs, claims, run_plan = self._eligibility(
                store,
                params,
                reference_evidence_out=reference_evidence,
            )
            if self.engine_manifest is None:
                raise _fault(
                    "EXPORT_ENGINE_MANIFEST_UNAVAILABLE",
                    "The exact typed EngineManifest is required in the evidence package.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            semantic_diff = (
                SemanticModelDiff.model_validate(
                    store.get_record("semantic_diff", bundle.revision.diff_id)
                )
                if bundle.revision.diff_id is not None
                else None
            )
            project_asset_sources: list[EvidenceProjectAssetSource] = []
            for asset in sorted(bundle.assets, key=lambda item: item.asset_id):
                try:
                    asset_path = store.resolve_asset_path(asset.relative_path)
                except (FileNotFoundError, ValueError) as exc:
                    raise _fault(
                        "EXPORT_SOURCE_ASSET_MISSING",
                        "A reviewed project source asset is unavailable for the evidence package.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=asset.asset_id,
                    ) from exc
                if (
                    asset_path.is_symlink()
                    or asset_path.stat().st_size != asset.byte_size
                    or _sha256_file(asset_path) != asset.sha256
                ):
                    raise _fault(
                        "EXPORT_SOURCE_ASSET_INTEGRITY_FAILED",
                        "A reviewed project source asset differs from its retained checksum.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        object_reference=asset.asset_id,
                    )
                project_asset_sources.append(
                    EvidenceProjectAssetSource(
                        asset_id=asset.asset_id,
                        source_path=asset_path,
                        sha256=asset.sha256,
                        byte_size=asset.byte_size,
                        media_type=asset.media_type,
                    )
                )
            artifact_sources: list[EvidenceArtifactSource] = []
            for run in sorted(runs, key=lambda item: item.direct_run_id):
                try:
                    verified = store.verify_authoritative_direct_run(
                        run.direct_run_id,
                        expected_analysis_id=params.analysis_id,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise _fault(
                        "EXPORT_DIRECT_EVIDENCE_INVALID",
                        "A selected direct run failed full authoritative evidence verification.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        object_reference=run.direct_run_id,
                    ) from exc
                if verified.run != run:
                    raise _fault(
                        "EXPORT_DIRECT_EVIDENCE_MISMATCH",
                        "A selected direct run differs from its authoritative evidence chain.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        object_reference=run.direct_run_id,
                    )
                artifact_sources.extend(self._export_artifact_sources_from_verified(verified))

            current_authorization_bundle = store.effective_bundle()
            if eligibility.professional and (
                bundle.reference_bindings or current_authorization_bundle.reference_bindings
            ):
                if (
                    current_authorization_bundle.revision.revision_id != bundle.revision.revision_id
                    or current_authorization_bundle.revision.model_sha256
                    != bundle.revision.model_sha256
                    or current_authorization_bundle.reference_bindings != bundle.reference_bindings
                ):
                    raise _fault(
                        "EXPORT_REFERENCE_MODEL_CHANGED",
                        "The active reference model changed during export preparation.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        remediation="Validate and export the active revision again.",
                    )
                latest_reference_evidence = (
                    self._reference_export_evidence_sources(
                        store=store,
                        bundle=bundle,
                    )
                    if bundle.reference_bindings
                    else []
                )
                if latest_reference_evidence != reference_evidence:
                    raise _fault(
                        "EXPORT_REFERENCE_EVIDENCE_CHANGED",
                        "Reference evidence changed between validation and package creation.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        remediation="Validate the complete reference evidence set again.",
                    )
            media_types = {
                ".asc": "text/plain",
                ".cfg": "text/plain",
                ".csv": "text/csv",
                ".html": "text/html",
                ".ini": "text/plain",
                ".json": "application/json",
                ".md": "text/markdown",
                ".rpt": "text/plain",
                ".tsv": "text/tab-separated-values",
                ".txt": "text/plain",
                ".xml": "application/xml",
                ".yaml": "application/x-yaml",
                ".yml": "application/x-yaml",
            }

            def prepare_publication(
                staging_directory: Path,
                export_directory: Path,
                export_manifest: ConnectivityExportManifest,
            ) -> None:
                package_files = [
                    {
                        "relative_path": (
                            export_directory.relative_to(store.root)
                            / path.relative_to(staging_directory)
                        ).as_posix(),
                        "sha256": _sha256_file(path),
                        "byte_size": path.stat().st_size,
                        "media_type": media_types.get(
                            path.suffix.casefold(), "application/octet-stream"
                        ),
                    }
                    for path in sorted(
                        staging_directory.rglob("*"), key=lambda item: item.as_posix()
                    )
                    if path.is_file() and not path.is_symlink()
                ]
                store.prepare_export_publication(
                    params.export_id,
                    params.analysis_id,
                    export_manifest.model_dump(mode="json"),
                    package_files=package_files,
                    staging_relative_path=staging_directory.relative_to(store.root).as_posix(),
                )

            export_path = create_evidence_export(
                output_root=output,
                export_id=params.export_id,
                baseline_portfolio_id=run_plan.baseline_portfolio_id,
                bundle=bundle,
                current_authorization_bundle=current_authorization_bundle,
                deployment=self._authoritative_project_context(store)[0],
                engine_manifest=self.engine_manifest,
                run_plan=run_plan,
                semantic_diff=semantic_diff,
                eligibility=eligibility,
                summaries=summaries,
                runs=runs,
                claims=claims,
                artifact_sources=artifact_sources,
                project_asset_sources=project_asset_sources,
                reference_evidence=reference_evidence,
                prepare_publication=prepare_publication,
            )
            manifest_path = export_path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            store.finalize_export_publication(params.export_id)
        finally:
            store.close()
        return self._result(
            "connectivity.export.create",
            {
                "export_id": params.export_id,
                "project_relative_directory": export_path.relative_to(store.root).as_posix(),
                "manifest": manifest,
            },
        )

    def _guided_directory(self, supplied: str | None) -> Path:
        return self._allowed_path(
            supplied or self.data_root / "guided" / "ridgeway",
            must_exist=False,
            purpose="guided project",
        )

    def _guided_open(self, params: GuidedOpenParams) -> PayloadResult:
        if not self.guided_projection_path.is_file():
            raise _fault(
                "RIDGEWAY_PROJECTION_NOT_INSTALLED",
                "The synthetic Ridgeway projection is not installed.",
                category=ErrorCategory.INTERNAL,
            )
        fixture = build_ridgeway_fixture()
        if self.guided_evidence is not None:
            projection = self.guided_evidence.projection
            # Worker initialization already verifies the signed projection,
            # manifest, archive digest, archive index, and every retained member.
            # Extracting and rehashing 14,000+ tiny files here made an otherwise
            # immediate replay wait on evidence that the user had not opened.
            # Materialization stays fail-closed and happens lazily in
            # connectivity.evidence.resolve, where the retained files are needed.
            initial_state = "VERIFIED_FIXTURE_REPLAY"
        else:
            projection = json.loads(self.guided_projection_path.read_text(encoding="utf-8"))
            expected = build_public_ui_projection(fixture)
            if (
                projection != expected
                or projection.get("releaseReplayEligible") is not False
                or projection.get("evidenceState") != "ENGINE_RUN_REQUIRED"
            ):
                raise _fault(
                    "RIDGEWAY_PROJECTION_INTEGRITY_FAILED",
                    "The Ridgeway projection differs from its deterministic gated fixture.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            try:
                projection = _presentation_safe_blocked_ridgeway_projection(projection)
            except ValueError as exc:
                raise _fault(
                    "RIDGEWAY_PROJECTION_CLAIM_POLICY_FAILED",
                    "The blocked Ridgeway teaching labels violate ecological claim policy.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    technical_message=str(exc),
                ) from exc
            initial_state = "ENGINE_REQUIRED"
        directory = self._guided_directory(params.project_directory)
        store = EcoProjectStore(directory)
        session_id = "guided_ridgeway_v1"
        recover_analysis_id: str | None = None
        try:
            store.initialize(
                project_id=fixture.bundle.project_id,
                title=fixture.bundle.title,
                mode="GUIDED",
                skeleton={
                    "fixture_version": fixture.fixture_version,
                    "evidence_manifest_sha256": (
                        self.guided_evidence.manifest_sha256
                        if self.guided_evidence is not None
                        else None
                    ),
                },
            )
            store.store_bundle(fixture.bundle)
            assumption_ledger = ridgeway_assumption_ledger_payload(
                fixture.bundle.intervention_options,
                fixture.bundle.fitness,
            )
            store.retain_model_fitness_evidence(
                evidence_id=str(assumption_ledger["ledger_id"]),
                revision=fixture.bundle.revision,
                content=assumption_ledger,
            )
            try:
                existing = store.guided(session_id)
            except KeyError:
                state = initial_state
                store.set_guided(session_id, state, projection)
            else:
                if existing["projection"] != projection:
                    raise _fault(
                        "RIDGEWAY_SESSION_PROJECTION_CHANGED",
                        "The saved guided session belongs to a different fixture projection.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                    )
                state = str(existing["state"])
                if state == "RUNNING":
                    try:
                        recover_analysis_id = str(
                            store.get_record("guided_analysis", session_id)["analysis_id"]
                        )
                    except KeyError:
                        recover_analysis_id = None
        finally:
            store.close()
        if (
            recover_analysis_id is not None
            and self.engine_executable is not None
            and self.engine_sha256 is not None
            and self.analysis_executor is not None
        ):
            recover = getattr(self.analysis_executor, "recover", None)
            if recover is not None:
                try:
                    recover(
                        project_directory=directory,
                        analysis_id=recover_analysis_id,
                        engine_executable=self.engine_executable,
                        engine_sha256=self.engine_sha256,
                    )
                except AnalysisOwnershipError as exc:
                    raise _fault(
                        "ANALYSIS_EXECUTION_BUSY",
                        "Another application worker currently owns this Ridgeway rerun.",
                        category=ErrorCategory.RESOURCE_LIMIT,
                        severity=Severity.BLOCKING,
                        remediation="Return to the owning application or retry after it stops.",
                        retryable=True,
                        technical_message=type(exc).__name__,
                    ) from exc
                except OrphanProcessAuditError as exc:
                    raise _fault(
                        "ANALYSIS_ORPHAN_PROCESS_AUDIT_REQUIRED",
                        "A prior Ridgeway solver process tree could not be proven stopped, so "
                        "the rerun was not recovered or changed.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        remediation=(
                            "Confirm the prior application and every Circuitscape child process "
                            "are stopped, then complete the local orphan-process audit before "
                            "retrying."
                        ),
                        technical_message=exc.reason_code,
                    ) from exc
                except Exception as exc:
                    failed_store = EcoProjectStore(directory)
                    try:
                        failed_store.set_guided(session_id, "FAILED", projection)
                    finally:
                        failed_store.close()
                    raise _fault(
                        "RIDGEWAY_RERUN_RECOVERY_FAILED",
                        "The abandoned Ridgeway rerun could not be recovered safely.",
                        category=ErrorCategory.ENGINE_FAILURE,
                        severity=Severity.BLOCKING,
                        remediation="Restart the rerun from the verified fixture replay.",
                        technical_message=type(exc).__name__,
                    ) from exc
        return self._result(
            "connectivity.guided.open",
            {
                "session_id": session_id,
                "projection": projection,
                "can_rerun": self.engine_executable is not None
                and self.analysis_executor is not None
                and self.guided_evidence is not None,
                "state": state,
                "evidence_manifest_sha256": (
                    self.guided_evidence.manifest_sha256
                    if self.guided_evidence is not None
                    else None
                ),
            },
        )

    def _guided_rerun(self, params: GuidedSessionParams) -> PayloadResult:
        engine, digest, executor = self._require_engine()
        if self.guided_evidence is None:
            raise _fault(
                "RIDGEWAY_DIRECT_EVIDENCE_REQUIRED",
                "The pinned Ridgeway direct-evidence package is required before a rerun.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        directory = self._guided_directory(params.project_directory)
        store = EcoProjectStore(directory)
        try:
            session = store.guided(params.session_id)
            if session["state"] in {"RUNNING", "PAUSED"}:
                raise _fault(
                    "RIDGEWAY_RERUN_ALREADY_ACTIVE",
                    "The Ridgeway direct rerun is already active.",
                )
            fixture = build_ridgeway_fixture()
            engine_manifest_sha256 = (
                self.engine_manifest.sha256 if self.engine_manifest is not None else digest
            )
            plan = ConnectivityRunPlan(
                analysis_id=new_id("analysis"),
                revision_id=fixture.bundle.revision.revision_id,
                scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
                portfolios=fixture.portfolios,
                baseline_portfolio_id=fixture.baseline_portfolio_id,
                direct_job_count=fixture.direct_matrix_job_count,
                solver_job_cap=fixture.direct_matrix_job_count,
                search_method="ENUMERATION",
                seed=0,
                current_maps_for_portfolio_ids=[],
                engine_manifest_sha256=engine_manifest_sha256,
            )
            store.create_analysis(plan, bundle=fixture.bundle)
            store.put_record(
                "guided_analysis",
                params.session_id,
                {"analysis_id": plan.analysis_id},
            )
            try:
                guided_start = getattr(executor, "start_guided", None)
                if guided_start is None:
                    raise _fault(
                        "RIDGEWAY_DIRECT_STUDY_EXECUTOR_REQUIRED",
                        "The installed executor cannot run the complete Ridgeway direct study.",
                        category=ErrorCategory.ENGINE_FAILURE,
                        severity=Severity.BLOCKING,
                        remediation=(
                            "Install a signed release with the dedicated 1,536 matrix plus 60 "
                            "current-map Ridgeway orchestrator."
                        ),
                    )
                execution = guided_start(
                    project_directory=directory,
                    bundle=fixture.bundle,
                    plan=plan,
                    engine_executable=engine,
                    engine_sha256=digest,
                    session_id=params.session_id,
                    guided_evidence=self.guided_evidence,
                )
                if store.analysis(plan.analysis_id)["state"] == AnalysisState.READY.value:
                    store.transition_analysis(plan.analysis_id, AnalysisState.RUNNING)
                state = str(execution.get("state", "RUNNING"))
                if state == "COMPLETED":
                    self._store_completed_execution(
                        store=store,
                        bundle=fixture.bundle,
                        plan=plan,
                        execution=execution,
                    )
                elif state != "RUNNING":
                    raise ValueError("guided executor returned an unsupported state")
            except Exception as exc:
                if store.analysis(plan.analysis_id)["state"] == AnalysisState.RUNNING.value:
                    store.fail_analysis(
                        plan.analysis_id,
                        {
                            "code": "RIDGEWAY_RERUN_FAILED",
                            "technical_type": type(exc).__name__,
                        },
                    )
                store.set_guided(params.session_id, "FAILED", session["projection"])
                if isinstance(exc, DurableAnalysisError):
                    raise _fault(
                        "RIDGEWAY_DIRECT_STUDY_EXECUTOR_REQUIRED",
                        "The installed executor cannot run the complete Ridgeway direct study.",
                        category=ErrorCategory.ENGINE_FAILURE,
                        severity=Severity.BLOCKING,
                        remediation=(
                            "Install a signed release with the dedicated 1,536 matrix plus 60 "
                            "current-map Ridgeway orchestrator."
                        ),
                    ) from exc
                raise
        finally:
            store.close()
        return self._result(
            "connectivity.guided.rerun",
            {"session_id": params.session_id, "analysis_id": plan.analysis_id, "state": state},
        )

    def _guided_control(self, params: GuidedSessionParams, action: str) -> PayloadResult:
        _, _, executor = self._require_engine()
        directory = self._guided_directory(params.project_directory)
        store = EcoProjectStore(directory)
        try:
            session = store.guided(params.session_id)
            if session["state"] not in {"RUNNING", "PAUSED"}:
                raise _fault(
                    "GUIDED_SESSION_NOT_ACTIVE",
                    "The guided direct rerun is not active.",
                )
            analysis_id = str(store.get_record("guided_analysis", params.session_id)["analysis_id"])
            if action == "pause":
                executor.pause(project_directory=directory, analysis_id=analysis_id)
            elif action == "resume":
                executor.resume(project_directory=directory, analysis_id=analysis_id)
            else:
                executor.cancel(project_directory=directory, analysis_id=analysis_id)
            status = executor.guided_status(project_directory=directory, analysis_id=analysis_id)
            state = str(status["state"])
        finally:
            store.close()
        return self._result(
            f"connectivity.guided.{action}",
            {"session_id": params.session_id, "state": state},
        )

    def _guided_status(self, params: GuidedSessionParams) -> PayloadResult:
        _, _, executor = self._require_engine()
        directory = self._guided_directory(params.project_directory)
        store = EcoProjectStore(directory)
        try:
            session = store.guided(params.session_id)
            if session["state"] == "VERIFIED_FIXTURE_REPLAY":
                raise _fault(
                    "GUIDED_RERUN_NOT_STARTED",
                    "Start the local Ridgeway direct rerun before requesting its status.",
                )
            analysis_id = str(store.get_record("guided_analysis", params.session_id)["analysis_id"])
        finally:
            store.close()
        return self._result(
            "connectivity.guided.status",
            executor.guided_status(
                project_directory=directory,
                analysis_id=analysis_id,
            ),
        )

    def _guided_pause(self, params: GuidedSessionParams) -> PayloadResult:
        return self._guided_control(params, "pause")

    def _guided_resume(self, params: GuidedSessionParams) -> PayloadResult:
        return self._guided_control(params, "resume")

    def _guided_cancel(self, params: GuidedSessionParams) -> PayloadResult:
        return self._guided_control(params, "cancel")

    def _guided_checkpoint(self, params: GuidedCheckpointParams) -> PayloadResult:
        directory = self._guided_directory(params.project_directory)
        store = EcoProjectStore(directory)
        try:
            event = store.record_guided_checkpoint(
                session_id=params.session_id,
                event_id=params.event_id,
                sequence_number=params.sequence_number,
                checkpoint_id=params.checkpoint_id,
                event_kind=params.event_kind,
                payload={
                    "duration_ms": params.duration_ms,
                    "evidence_ids": params.evidence_ids,
                    "portfolio_ids": params.portfolio_ids,
                },
            )
        finally:
            store.close()
        return self._result("connectivity.guided.checkpoint", event)

    @staticmethod
    def _scope_from_binding(binding: dict[str, Any]) -> ProjectScopeEvidence:
        return ProjectScopeEvidence.model_validate(binding["scope_evidence"])

    def _sync_plan(
        self,
        store: EcoProjectStore,
        *,
        oversize_approved_by: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        binding = store.observability()
        if binding is None:
            raise _fault(
                "OBSERVABILITY_BINDING_REQUIRED",
                "Connect this project to a verified Restricted W&B project first.",
                category=ErrorCategory.SECURITY,
            )
        current_raster_estimate = self._authoritative_current_raster_estimate(store)
        plan = plan_cloud_sync(
            store.effective_bundle(),
            destination=SyncDestination(binding["deployment"]),
            scope_evidence=self._scope_from_binding(binding),
            current_raster_estimate=current_raster_estimate,
            oversize_approved_by=oversize_approved_by,
        )
        data = {
            "destination": plan.destination.value,
            "projections": [asdict(item) for item in plan.projections],
            "input_bytes": plan.input_bytes,
            "estimated_current_raster_bytes": plan.estimated_current_raster_bytes,
            "estimated_total_bytes": plan.estimated_total_bytes,
            "current_raster_estimate_status": plan.current_raster_estimate_status.value,
            "actual_retained_current_raster_bytes": (plan.actual_retained_current_raster_bytes),
            "pending_estimated_current_raster_bytes": (plan.pending_estimated_current_raster_bytes),
            "expected_current_raster_count": plan.expected_current_raster_count,
            "retained_current_raster_count": plan.retained_current_raster_count,
            "current_raster_estimate_basis": plan.current_raster_estimate_basis,
            "blockers": list(plan.blockers),
            "warnings": list(plan.warnings),
            "allowed": plan.allowed,
        }
        return binding, data

    def _authoritative_current_raster_estimate(
        self, store: EcoProjectStore
    ) -> CurrentRasterEstimate:
        """Reconcile retained map bytes and estimate only planned, missing maps.

        Renderer values never enter this calculation. Planned output counts come
        from immutable ``ConnectivityRunPlan`` snapshots and cell counts come
        from those analyses' reviewed raster grids. Existing artifacts replace
        estimates with their exact retained byte sizes after path, size, and
        digest verification.
        """

        actual_by_job: dict[tuple[str, str, str], int] = {}
        try:
            artifact_rows = store._connection.execute(
                "SELECT r.analysis_id,r.portfolio_id,r.scenario_id,a.byte_size,"
                "a.cas_relative_path,a.sha256 FROM eco_analysis_runs r "
                "JOIN eco_run_artifacts a ON a.direct_run_id=r.direct_run_id "
                "WHERE a.role='current_map' ORDER BY r.analysis_id,r.portfolio_id,r.scenario_id"
            ).fetchall()
            for row in artifact_rows:
                retained = contained_path(store.root, str(row["cas_relative_path"]))
                expected_size = int(row["byte_size"])
                if (
                    not retained.is_file()
                    or retained.is_symlink()
                    or retained.stat().st_size != expected_size
                    or _sha256_file(retained) != str(row["sha256"])
                ):
                    raise ValueError("retained current-raster artifact failed integrity checks")
                actual_by_job[
                    (
                        str(row["analysis_id"]),
                        str(row["portfolio_id"]),
                        str(row["scenario_id"]),
                    )
                ] = expected_size
        except (OSError, PathSecurityError, TypeError, ValueError):
            return CurrentRasterEstimate(
                status=CurrentRasterEstimateStatus.UNAVAILABLE,
                estimated_bytes=None,
                actual_retained_bytes=0,
                pending_estimated_bytes=None,
                expected_raster_count=None,
                retained_raster_count=0,
                basis=(
                    "A retained current-raster artifact failed authoritative path, size, or "
                    "checksum reconciliation."
                ),
            )

        expected_keys: set[tuple[str, str, str]] = set()
        pending_bytes = 0
        unknown_reasons: list[str] = []
        analyses = store._connection.execute(
            "SELECT analysis_id,state,plan_json FROM eco_analyses ORDER BY created_at,analysis_id"
        ).fetchall()
        for analysis_row in analyses:
            analysis_id = str(analysis_row["analysis_id"])
            try:
                plan = ConnectivityRunPlan.model_validate_json(str(analysis_row["plan_json"]))
                bundle = store.analysis_bundle(analysis_id)
            except (KeyError, ValidationError, ValueError):
                unknown_reasons.append(f"analysis {analysis_id} has no valid immutable run plan")
                continue
            emit_portfolio_ids = set(plan.current_maps_for_portfolio_ids)
            if emit_portfolio_ids:
                emit_portfolio_ids.add(plan.baseline_portfolio_id)
            if not emit_portfolio_ids:
                continue
            scenario_by_id = {item.scenario_id: item for item in bundle.scenarios}
            hypothesis_by_id = {item.hypothesis_id: item for item in bundle.resistance_hypotheses}
            focal_by_id = {item.focal_set_id: item for item in bundle.focal_area_sets}
            asset_by_id = {item.asset_id: item for item in bundle.assets}
            for scenario_id in plan.scenario_ids:
                scenario = scenario_by_id.get(scenario_id)
                if scenario is None:
                    unknown_reasons.append(
                        f"analysis {analysis_id} references an unavailable scenario grid"
                    )
                    continue
                candidate_asset_ids: list[str] = []
                hypothesis = hypothesis_by_id.get(scenario.resistance_hypothesis_id)
                focal_set = focal_by_id.get(scenario.focal_set_id)
                if hypothesis is not None:
                    candidate_asset_ids.append(hypothesis.resistance_asset_id)
                if focal_set is not None:
                    candidate_asset_ids.append(focal_set.asset_id)
                grids = [
                    asset_by_id[asset_id].grid
                    for asset_id in candidate_asset_ids
                    if asset_id in asset_by_id
                    and asset_by_id[asset_id].grid is not None
                    and asset_by_id[asset_id].grid.grid_id == scenario.grid_id
                ]
                dimensions = {(grid.width, grid.height) for grid in grids if grid is not None}
                if len(dimensions) != 1:
                    unknown_reasons.append(
                        f"scenario {scenario_id} has no single authoritative current-raster grid"
                    )
                    continue
                width, height = next(iter(dimensions))
                # Circuitscape's retained raster is textual in the v0.1
                # protocol. Thirty-two bytes per cell plus a bounded header is
                # deliberately conservative for double-precision values and
                # separators; actual retained bytes replace this estimate.
                per_raster_estimate = width * height * 32 + 4_096
                for portfolio_id in emit_portfolio_ids:
                    key = (analysis_id, portfolio_id, scenario_id)
                    expected_keys.add(key)
                    if key not in actual_by_job:
                        pending_bytes += per_raster_estimate
                        if str(analysis_row["state"]) == AnalysisState.COMPLETED.value:
                            unknown_reasons.append(
                                f"completed analysis {analysis_id} is missing a planned current raster"
                            )

        guided_actual_bytes = 0
        guided_actual_count = 0
        if store.project()["mode"] == "GUIDED" and self.guided_evidence is not None:
            guided_files = [
                item
                for item in self.guided_evidence.bundle_index.get("files", [])
                if isinstance(item, dict) and item.get("role") == "map_current_raster"
            ]
            try:
                guided_actual_bytes = sum(int(item["byte_size"]) for item in guided_files)
            except (KeyError, TypeError, ValueError):
                unknown_reasons.append("the verified Ridgeway map manifest has invalid byte counts")
            guided_actual_count = len(guided_files)
            if guided_actual_count != 60 or guided_actual_bytes <= 0:
                unknown_reasons.append(
                    "the verified Ridgeway evidence does not enumerate all 60 current rasters"
                )

        orphan_actual_keys = set(actual_by_job) - expected_keys
        actual_retained_bytes = sum(actual_by_job.values()) + guided_actual_bytes
        retained_count = len(actual_by_job) + guided_actual_count
        expected_count = len(expected_keys | orphan_actual_keys) + guided_actual_count
        if unknown_reasons:
            return CurrentRasterEstimate(
                status=CurrentRasterEstimateStatus.UNAVAILABLE,
                estimated_bytes=None,
                actual_retained_bytes=actual_retained_bytes,
                pending_estimated_bytes=None,
                expected_raster_count=expected_count or None,
                retained_raster_count=retained_count,
                basis="; ".join(sorted(set(unknown_reasons))),
            )
        if not analyses and not actual_by_job and guided_actual_count == 0:
            bundle = store.effective_bundle()
            if bundle.review_masks:
                return CurrentRasterEstimate(
                    status=CurrentRasterEstimateStatus.UNAVAILABLE,
                    estimated_bytes=None,
                    actual_retained_bytes=0,
                    pending_estimated_bytes=None,
                    expected_raster_count=None,
                    retained_raster_count=0,
                    basis=(
                        "The project has review masks but no immutable run plan from which to "
                        "derive current-raster count and grids."
                    ),
                )
            return CurrentRasterEstimate(
                status=CurrentRasterEstimateStatus.NOT_REQUIRED,
                estimated_bytes=0,
                actual_retained_bytes=0,
                pending_estimated_bytes=0,
                expected_raster_count=0,
                retained_raster_count=0,
                basis="No run plan or review mask requests a diagnostic current raster.",
            )
        estimated_bytes = actual_retained_bytes + pending_bytes
        if pending_bytes and retained_count:
            status = CurrentRasterEstimateStatus.PARTIALLY_RECONCILED
        elif pending_bytes:
            status = CurrentRasterEstimateStatus.PRE_RUN_ESTIMATE
        else:
            status = CurrentRasterEstimateStatus.ACTUAL_RECONCILED
        return CurrentRasterEstimate(
            status=status,
            estimated_bytes=estimated_bytes,
            actual_retained_bytes=actual_retained_bytes,
            pending_estimated_bytes=pending_bytes,
            expected_raster_count=expected_count,
            retained_raster_count=retained_count,
            basis=(
                "Exact retained current-map bytes plus a conservative 32-byte-per-cell and "
                "4,096-byte-header estimate for each planned map not yet materialized."
            ),
        )

    def _observability_preflight(self, params: ObservabilityPreflightParams) -> PayloadResult:
        if self.observability_adapter is None:
            raise _fault(
                "OBSERVABILITY_ADAPTER_UNAVAILABLE",
                "The W&B and Weave observability adapter is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        key = params.api_key.get_secret_value()
        try:
            with (
                self.observability_credentials.candidate(key),
                self.observability_credentials.environment(),
            ):
                scope = self.scope_resolver.resolve(
                    entity=params.entity,
                    project=params.project,
                    deployment=params.deployment,
                    api_base_url=params.api_base_url,
                )
                binding = {
                    "binding_id": new_id("binding"),
                    "entity": params.entity,
                    "project": params.project,
                    "deployment": params.deployment,
                    "api_base_url": params.api_base_url,
                    "web_base_url": params.web_base_url or params.api_base_url,
                    "profile_namespace": APP_PROFILE.namespace,
                    "scope_evidence": scope.model_dump(mode="json"),
                    "state": "PREFLIGHT_VERIFIED",
                }
                validation = self.observability_adapter.validate(binding=binding)
                external_id = validation.get("external_project_id")
                if external_id is not None and external_id != scope.external_project_id:
                    raise _fault(
                        "WANDB_PROJECT_IDENTITY_MISMATCH",
                        "The W&B adapter returned a different immutable project identity.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                    )
                binding.update(validation)
        except RPCFault:
            raise
        except RuntimeError as exc:
            code = str(exc)
            if "SCOPE" in code or "RESTRICTED" in code:
                raise _fault(
                    "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE",
                    "The destination cannot be authoritatively verified as Restricted.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    remediation=(
                        "Use a documented exact-scope resolver or approved "
                        "Dedicated/Self-Managed endpoint."
                    ),
                ) from exc
            raise _fault(
                "OBSERVABILITY_VALIDATION_FAILED",
                "W&B Models and Weave access could not be verified.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
                technical_message=type(exc).__name__,
            ) from exc
        finally:
            key = ""

        preflight_id = new_id("wandb_preflight")
        self._observability_preflights[preflight_id] = {
            **binding,
            "preflight_id": preflight_id,
            "verified_at": datetime.now(UTC).isoformat(),
        }
        return self._result(
            "observability.preflight",
            {
                "preflight_id": preflight_id,
                "scope": "RESTRICTED",
                "allowed": True,
                "message": "Restricted scope and W&B Models/Weave access were verified.",
                "binding_preview": {
                    key: value for key, value in binding.items() if key not in {"scope_evidence"}
                },
            },
        )

    def _observability_connect(self, params: ObservabilityConnectParams) -> PayloadResult:
        if self.observability_adapter is None:
            raise _fault(
                "OBSERVABILITY_ADAPTER_UNAVAILABLE",
                "The W&B and Weave observability adapter is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        preflight = self._observability_preflights.get(params.preflight_id)
        if preflight is None:
            raise _fault(
                "OBSERVABILITY_PREFLIGHT_REQUIRED",
                "The verified W&B destination preflight is missing or expired.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Verify the Restricted destination again.",
            )
        try:
            with self.observability_credentials.environment():
                scope = self.scope_resolver.resolve(
                    entity=preflight["entity"],
                    project=preflight["project"],
                    deployment=preflight["deployment"],
                    api_base_url=preflight["api_base_url"],
                )
        except RuntimeError as exc:
            raise _fault(
                "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE",
                "The destination can no longer be authoritatively verified as Restricted.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            ) from exc
        store = self._store(params.project_directory)
        try:
            existing = store.observability()
            identity = (
                preflight["entity"],
                preflight["project"],
                preflight["deployment"],
            )
            if (
                existing is not None
                and (
                    existing.get("entity"),
                    existing.get("project"),
                    existing.get("deployment"),
                )
                != identity
            ):
                raise _fault(
                    "OBSERVABILITY_REBIND_REQUIRES_PROJECT_REVISION",
                    "A project cannot be silently rebound to a different W&B destination.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    remediation="Create an explicit EcoCorridor project revision and lineage link.",
                )
            binding = existing or {
                key: value
                for key, value in preflight.items()
                if key not in {"preflight_id", "verified_at"}
            }
            binding.update(
                {
                    "scope_evidence": scope.model_dump(mode="json"),
                    "state": "CONNECTING",
                    "oversize_approved_by": params.oversize_approved_by,
                }
            )
            try:
                with self.observability_credentials.environment():
                    validation = self.observability_adapter.validate(binding=binding)
            except Exception as exc:
                raise _fault(
                    "OBSERVABILITY_VALIDATION_FAILED",
                    "W&B Models and Weave access could not be verified.",
                    category=ErrorCategory.DATA_PROVIDER,
                    retryable=True,
                    technical_message=type(exc).__name__,
                ) from exc
            external_id = validation.get("external_project_id")
            if external_id is not None and external_id != scope.external_project_id:
                raise _fault(
                    "WANDB_PROJECT_IDENTITY_MISMATCH",
                    "The W&B adapter returned a different immutable project identity.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            binding.update(validation)
            binding["state"] = "CONNECTED"
            store.set_observability(binding)
            try:
                _, sync_plan = self._sync_plan(
                    store,
                    oversize_approved_by=params.oversize_approved_by,
                )
            except (KeyError, ValidationError, ValueError) as exc:
                sync_plan = {
                    "allowed": False,
                    "blockers": ["PLANNING_MODEL_INCOMPLETE"],
                    "warnings": [],
                    "estimated_current_raster_bytes": None,
                    "estimated_total_bytes": None,
                    "current_raster_estimate_status": "UNAVAILABLE",
                    "actual_retained_current_raster_bytes": 0,
                    "pending_estimated_current_raster_bytes": None,
                    "expected_current_raster_count": None,
                    "retained_current_raster_count": 0,
                    "current_raster_estimate_basis": (
                        "The planning model is incomplete, so no authoritative run plan and "
                        "grid estimate exists."
                    ),
                    "message": (
                        "The destination is bound, but synchronization remains blocked "
                        "until the planning model and asset authorizations are complete."
                    ),
                    "technical_type": type(exc).__name__,
                }
            if not sync_plan["allowed"]:
                binding["state"] = "BLOCKED_PROJECT_POLICY"
                store.set_observability(binding)
        finally:
            store.close()
        self._observability_preflights.pop(params.preflight_id, None)
        return self._result(
            "observability.connect", {"binding": binding, "upload_preview": sync_plan}
        )

    def _observability_validate(self, params: ProjectParams) -> PayloadResult:
        if self.observability_adapter is None:
            raise _fault(
                "OBSERVABILITY_ADAPTER_UNAVAILABLE",
                "The W&B and Weave observability adapter is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        store = self._store(params.project_directory)
        try:
            binding = store.observability()
            if binding is None:
                raise _fault(
                    "OBSERVABILITY_BINDING_REQUIRED",
                    "No W&B project binding exists.",
                    category=ErrorCategory.SECURITY,
                )
            try:
                with self.observability_credentials.environment():
                    scope = self.scope_resolver.resolve(
                        entity=binding["entity"],
                        project=binding["project"],
                        deployment=binding["deployment"],
                        api_base_url=binding["api_base_url"],
                    )
            except RuntimeError as exc:
                raise _fault(
                    "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE",
                    "The destination can no longer be authoritatively verified as Restricted.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                ) from exc
            binding["scope_evidence"] = scope.model_dump(mode="json")
            try:
                validation = self.observability_adapter.validate(binding=binding)
            except Exception as exc:
                raise _fault(
                    "OBSERVABILITY_VALIDATION_FAILED",
                    "W&B Models and Weave access could not be revalidated.",
                    category=ErrorCategory.DATA_PROVIDER,
                    retryable=True,
                    technical_message=type(exc).__name__,
                ) from exc
            external_id = validation.get("external_project_id")
            if external_id is not None and external_id != scope.external_project_id:
                raise _fault(
                    "WANDB_PROJECT_IDENTITY_MISMATCH",
                    "The W&B adapter returned a different immutable project identity.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            binding.update(validation)
            binding["state"] = "CONNECTED"
            store.set_observability(binding)
        finally:
            store.close()
        return self._result("observability.validate_binding", {"binding": binding})

    def _observability_status(self, params: ProjectParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            binding = store.observability()
        finally:
            store.close()
        return self._result(
            "observability.get_status",
            binding or {"state": "AUTHENTICATION_REQUIRED", "profile": APP_PROFILE.namespace},
        )

    def _observability_preview(self, params: ObservabilityPreviewParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            _, plan = self._sync_plan(
                store,
                oversize_approved_by=params.oversize_approved_by,
            )
        finally:
            store.close()
        return self._result("observability.preview_upload", plan)

    def _observability_sync(self, params: ObservabilitySyncParams) -> PayloadResult:
        if self.observability_adapter is None:
            raise _fault(
                "OBSERVABILITY_ADAPTER_UNAVAILABLE",
                "The W&B synchronization adapter is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        store = self._store(params.project_directory)
        try:
            binding = store.observability()
            if binding is None:
                raise _fault(
                    "OBSERVABILITY_BINDING_REQUIRED",
                    "No verified W&B project binding exists.",
                    category=ErrorCategory.SECURITY,
                )
            binding, plan = self._sync_plan(
                store,
                oversize_approved_by=binding.get("oversize_approved_by"),
            )
            if not plan["allowed"]:
                raise _fault(
                    "BLOCKED_PROJECT_POLICY",
                    "Asset policy blocks synchronization.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            if not params.online:
                # Offline is an operational condition, not a policy bypass.  The
                # binding and every asset authorization were verified earlier;
                # leave the transactional outbox pending and make no provider or
                # credential call.
                receipt = self.observability_adapter.sync(
                    project_directory=store.root,
                    binding=binding,
                    sync_plan=plan,
                    online=False,
                )
                binding["state"] = "SYNC_QUEUED_OFFLINE"
                store.set_observability(binding)
                return self._result("observability.sync", {"binding": binding, "receipt": receipt})

            binding["state"] = "SYNCING"
            store.set_observability(binding)
            try:
                receipt = self.observability_adapter.sync(
                    project_directory=store.root,
                    binding=binding,
                    sync_plan=plan,
                    online=True,
                )
            except Exception as exc:
                binding["state"] = "FAILED_RETRYING"
                store.set_observability(binding)
                raise _fault(
                    "OBSERVABILITY_SYNC_FAILED",
                    "Project evidence synchronization failed and is queued for retry.",
                    category=ErrorCategory.DATA_PROVIDER,
                    retryable=True,
                    technical_message=type(exc).__name__,
                ) from exc
            binding["state"] = "CONNECTED"
            binding["last_sync"] = receipt
            store.set_observability(binding)
        finally:
            store.close()
        return self._result("observability.sync", {"binding": binding, "receipt": receipt})

    def _observability_retry(self, params: ProjectParams) -> PayloadResult:
        result = self._observability_sync(
            ObservabilitySyncParams(project_directory=params.project_directory, online=True)
        )
        return self._result("observability.retry", result.data)

    def _open_observability(self, params: ObservabilityLinkParams, kind: str) -> PayloadResult:
        if self.observability_adapter is None:
            raise _fault(
                "OBSERVABILITY_ADAPTER_UNAVAILABLE",
                "The observability deep-link adapter is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
            )
        store = self._store(params.project_directory)
        try:
            if store.observability() is None:
                raise _fault(
                    "OBSERVABILITY_BINDING_REQUIRED",
                    "No verified project binding exists.",
                    category=ErrorCategory.SECURITY,
                )
            link = self.observability_adapter.open_link(
                project_directory=store.root,
                kind=kind,
                reference_id=params.reference_id,
            )
        finally:
            store.close()
        return self._result(f"observability.open_{kind}", link)

    def _open_run(self, params: ObservabilityLinkParams) -> PayloadResult:
        return self._open_observability(params, "run")

    def _open_artifact(self, params: ObservabilityLinkParams) -> PayloadResult:
        return self._open_observability(params, "artifact")

    def _open_trace(self, params: ObservabilityLinkParams) -> PayloadResult:
        return self._open_observability(params, "trace")

    @staticmethod
    def _assistant_failure(exc: BaseException) -> RPCFault:
        code = str(exc)
        if not code.startswith(("ASSISTANT_", "TRACED_ASSISTANT_", "WANDB_", "OBSERVABILITY_")):
            code = "ASSISTANT_PROVIDER_OR_TRACE_FAILURE"
        security = any(
            marker in code
            for marker in (
                "AUTHORIZ",
                "CREDENTIAL",
                "SCOPE",
                "IDENTITY",
                "INJECTION",
                "SENSITIVE",
                "PROHIBITED",
                "UNAUTHORIZED",
                "FABRICATED",
                "UNSAFE",
            )
        )
        retryable = any(
            marker in code for marker in ("UNAVAILABLE", "HTTP_", "TRACE_FLUSH", "TRACE_READBACK")
        )
        return _fault(
            code,
            (
                "The assistant request was blocked by project authorization or evidence safety."
                if security
                else "The traced assistant could not return a validated response."
            ),
            category=ErrorCategory.SECURITY if security else ErrorCategory.DATA_PROVIDER,
            severity=Severity.BLOCKING if security else Severity.ERROR,
            remediation=(
                "Revalidate the exact Restricted W&B binding and inspect the selected evidence."
                if security
                else "Check the model-provider credential, provider endpoint, and Weave connection."
            ),
            retryable=retryable,
        )

    def _assistant_configure(self, params: AssistantConfigureParams) -> PayloadResult:
        if self.assistant_runtime is None:
            raise _fault(
                "TRACED_ASSISTANT_RUNTIME_UNAVAILABLE",
                "The provider-backed assistant runtime is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
            )
        project_root = self._allowed_path(
            params.project_directory, must_exist=True, purpose="assistant project"
        )
        key = params.api_key.get_secret_value()
        try:
            status = self.assistant_runtime.configure(
                project_root=project_root,
                configuration=params.configuration,
                api_key=key,
            )
        except (AssistantSafetyError, PermissionError, RuntimeError) as exc:
            raise self._assistant_failure(exc) from exc
        finally:
            key = ""
        return self._result("assistant.configure", status)

    def _assistant_status(self, params: ProjectParams) -> PayloadResult:
        if self.assistant_runtime is None:
            status: dict[str, Any] = {"configured": self.evidence_assistant is not None}
        else:
            project_root = self._allowed_path(
                params.project_directory, must_exist=True, purpose="assistant project"
            )
            status = self.assistant_runtime.status(project_root=project_root)
        return self._result("assistant.status", status)

    def _assistant_clear(self, params: ProjectParams) -> PayloadResult:
        if self.assistant_runtime is not None:
            project_root = self._allowed_path(
                params.project_directory, must_exist=True, purpose="assistant project"
            )
            self.assistant_runtime.clear(project_root=project_root)
        return self._result("assistant.clear", {"configured": False, "cleared": True})

    def _project_evidence_assistant(self, project_root: Path) -> EvidenceAssistant:
        if self.evidence_assistant is not None:
            return self.evidence_assistant
        if self.assistant_runtime is None:
            raise _fault(
                "TRACED_ASSISTANT_UNAVAILABLE",
                "The provider-backed, traced Evidence Assistant is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        try:
            return self.assistant_runtime.evidence(project_root=project_root)
        except RuntimeError as exc:
            raise self._assistant_failure(exc) from exc

    @staticmethod
    def _assistant_run_association(
        store: EcoProjectStore,
        *,
        run_kind: str,
        run_subject_id: str,
        analysis_id: str | None = None,
    ) -> AssistantRunAssociation:
        binding = store.observability()
        if (
            binding is None
            or binding.get("state") != "CONNECTED"
            or binding.get("verified") is not True
            or binding.get("profile_namespace") != APP_PROFILE.namespace
        ):
            raise _fault(
                "ASSISTANT_VERIFIED_ONLINE_BINDING_REQUIRED",
                "The assistant requires the project's verified online W&B binding.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        project_id = str(store.project()["project_id"])
        return AssistantRunAssociation.create(
            project_id=project_id,
            wandb_external_project_id=str(binding.get("external_project_id") or ""),
            wandb_run_id=APP_PROFILE.run_id(run_kind, run_subject_id),
            wandb_run_kind=run_kind,
            run_subject_id=run_subject_id,
            analysis_id=analysis_id,
        )

    def _validated_evidence_sensitive_policy(
        self,
        store: EcoProjectStore,
        evidence_ids: Sequence[str],
        requested_policy: SensitiveContextPolicy,
    ) -> SensitiveContextPolicy:
        """Derive evidence redaction from authoritative project state.

        The renderer may add conservative redaction terms, but it cannot grant
        itself permission to send exact locations to a model provider or Weave.
        Eco v0.1 has no reviewer-bound, per-assistant-call authorization
        contract, so exact-location context remains fail-closed here.
        """

        if requested_policy.exact_location_authorized:
            raise _fault(
                "ASSISTANT_EXACT_LOCATION_AUTHORIZATION_UNAVAILABLE",
                "Exact-location assistant context is not available in this release.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation=(
                    "Use generalized evidence context. A future release must add a "
                    "reviewer-bound, destination-specific call authorization before raw "
                    "location context can be enabled."
                ),
            )

        assets: dict[str, ConnectivityAsset] = {}
        try:
            assets.update({item.asset_id: item for item in store.effective_bundle().assets})
        except (KeyError, ValidationError, ValueError):
            pass
        for payload in store.list_records("asset"):
            asset = ConnectivityAsset.model_validate(payload)
            assets[asset.asset_id] = asset

        sensitive_classes = {
            SensitivityClass.SENSITIVE_LOCATION,
            SensitivityClass.CRITICAL_INFRASTRUCTURE,
            SensitivityClass.TRIBAL_OR_CULTURAL,
        }
        sensitive_assets = {
            asset_id: asset
            for asset_id, asset in assets.items()
            if asset.authorization.sensitivity in sensitive_classes
        }
        terms = {item.strip() for item in requested_policy.prohibited_terms if item.strip()}
        for asset in sensitive_assets.values():
            terms.update({asset.asset_id.strip(), asset.title.strip()})

        for evidence_id in evidence_ids:
            try:
                resolved = store.resolve_evidence_claim(evidence_id)
                claim = ConnectivityEvidenceClaim.model_validate(resolved["claim"])
            except (KeyError, TypeError, ValueError):
                # The authoritative resolver will report the missing claim. Do
                # not weaken redaction merely because resolution will fail.
                continue
            source_asset_ids = {
                str(node.label)
                for node in claim.nodes
                if node.node_type == "ASSET" and str(node.label) in sensitive_assets
            }
            source_asset_ids.update(
                node.node_id.removeprefix("asset:")
                for node in claim.nodes
                if node.node_type == "ASSET"
                and node.node_id.startswith("asset:")
                and node.node_id.removeprefix("asset:") in sensitive_assets
            )
            source_assets = resolved.get("source_assets")
            if isinstance(source_assets, list):
                source_asset_ids.update(
                    str(item["asset_id"])
                    for item in source_assets
                    if isinstance(item, dict) and str(item.get("asset_id", "")) in sensitive_assets
                )
            if source_asset_ids:
                terms.update(source_asset_ids)
                terms.update(
                    item for item in (claim.portfolio_id, claim.scenario_id, claim.pair_id) if item
                )

        return SensitiveContextPolicy(
            prohibited_terms=sorted(terms, key=lambda item: (item.casefold(), item)),
            exact_location_authorized=False,
        )

    @staticmethod
    def _assistant_asset_bytes_are_current(
        store: EcoProjectStore, asset: ConnectivityAsset
    ) -> bool:
        try:
            retained = store.resolve_asset_path(asset.relative_path)
            metadata = os.lstat(retained)
        except (FileNotFoundError, OSError, PermissionError):
            return False
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_size == asset.byte_size
            and _sha256_file(retained) == asset.sha256
        )

    def _authorize_evidence_selection(
        self,
        store: EcoProjectStore,
        *,
        project_id: str,
        analysis_id: str,
        evidence_ids: Sequence[str],
        association: AssistantRunAssociation,
        sensitive_policy: SensitiveContextPolicy,
    ) -> AuthorizedEvidenceSelection:
        """Authorize the immutable analysis snapshot before any Weave/provider operation."""

        binding = store.observability()
        if (
            binding is None
            or binding.get("state") != "CONNECTED"
            or binding.get("verified") is not True
            or binding.get("profile_namespace") != APP_PROFILE.namespace
        ):
            raise _fault(
                "ASSISTANT_EVIDENCE_DESTINATION_UNVERIFIED",
                "Evidence explanation requires the project's current exactly verified W&B binding.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        deployment = str(binding.get("deployment") or "")
        if deployment not in {"WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"}:
            raise _fault(
                "ASSISTANT_EVIDENCE_DESTINATION_UNSUPPORTED",
                "The current W&B deployment is not supported for evidence explanation.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        external_project_id = str(binding.get("external_project_id") or "")
        if not external_project_id or external_project_id != association.wandb_external_project_id:
            raise _fault(
                "ASSISTANT_EVIDENCE_DESTINATION_CHANGED",
                "The evidence destination differs from the W&B run association.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        scope_payload = binding.get("scope_evidence")
        if not isinstance(scope_payload, dict):
            raise _fault(
                "ASSISTANT_EVIDENCE_SCOPE_MISSING",
                "Documented exact Restricted-scope evidence is missing.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        try:
            snapshot = store.analysis_bundle(analysis_id)
            current = store.effective_bundle()
        except (KeyError, ValidationError, ValueError) as exc:
            raise _fault(
                "ASSISTANT_EVIDENCE_SNAPSHOT_UNAVAILABLE",
                "The exact analysis model snapshot or current authorization state is unavailable.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
            ) from exc
        if snapshot.project_id != project_id or current.project_id != project_id:
            raise _fault(
                "ASSISTANT_EVIDENCE_PROJECT_MISMATCH",
                "The evidence snapshot does not belong to this project.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )

        try:
            resolved_evidence = [store.resolve_evidence_claim(item) for item in evidence_ids]
            claims = [
                ConnectivityEvidenceClaim.model_validate(item["claim"])
                for item in resolved_evidence
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise _fault(
                "ASSISTANT_EVIDENCE_RESOLUTION_INCOMPLETE",
                "One or more selected evidence chains are unavailable or failed integrity checks.",
                category=ErrorCategory.MODEL_QUALITY,
                severity=Severity.BLOCKING,
            ) from exc
        snapshot_assets = {item.asset_id: item for item in snapshot.assets}
        current_assets = {item.asset_id: item for item in current.assets}
        referenced_asset_ids: set[str] = set()
        for claim in claims:
            for node in claim.nodes:
                if node.node_type != "ASSET":
                    continue
                node_asset_id = node.node_id.removeprefix("asset:")
                candidates = {
                    item for item in (node_asset_id, str(node.label)) if item in snapshot_assets
                }
                if len(candidates) != 1:
                    raise _fault(
                        "ASSISTANT_EVIDENCE_ASSET_UNRESOLVED",
                        "A selected evidence source does not resolve uniquely in its analysis snapshot.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                        object_reference=claim.claim_id,
                    )
                referenced_asset_ids.update(candidates)
        for resolved in resolved_evidence:
            source_assets = resolved.get("source_assets")
            if not isinstance(source_assets, list):
                raise _fault(
                    "ASSISTANT_EVIDENCE_SOURCE_CHAIN_MISSING",
                    "Selected evidence has no authoritative source-asset chain.",
                    category=ErrorCategory.MODEL_QUALITY,
                    severity=Severity.BLOCKING,
                )
            for asset in source_assets:
                asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
                if not isinstance(asset_id, str) or asset_id not in snapshot_assets:
                    raise _fault(
                        "ASSISTANT_EVIDENCE_ASSET_UNRESOLVED",
                        "A selected evidence source is absent from its analysis snapshot.",
                        category=ErrorCategory.MODEL_QUALITY,
                        severity=Severity.BLOCKING,
                    )
                referenced_asset_ids.add(asset_id)

        scientific_ids = scientific_asset_ids(snapshot)
        try:
            reconciled_assets = reconcile_snapshot_assets(
                snapshot,
                current,
                asset_ids=referenced_asset_ids,
            )
        except SnapshotAssetAuthorizationError as exc:
            code = (
                "ASSISTANT_EVIDENCE_AUTHORIZATION_REVOKED"
                if exc.code == "ECO_ANALYSIS_ASSET_AUTHORIZATION_REVOKED"
                else "ASSISTANT_EVIDENCE_ASSET_STALE"
            )
            message = (
                "A source asset from the analysis snapshot is no longer authorized in the project."
                if code == "ASSISTANT_EVIDENCE_AUTHORIZATION_REVOKED"
                else "A source asset differs from the immutable bytes used by the selected analysis."
            )
            raise _fault(
                code,
                message,
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                object_reference=exc.asset_id,
            ) from exc
        authorization_projection: list[dict[str, Any]] = []
        generalized_count = 0
        for source_asset_id in sorted(referenced_asset_ids):
            snapshot_asset = snapshot_assets[source_asset_id]
            current_asset = reconciled_assets[source_asset_id]
            authorization = current_asset.authorization
            if deployment not in authorization.approved_deployments:
                raise _fault(
                    "ASSISTANT_EVIDENCE_DEPLOYMENT_UNAUTHORIZED",
                    "A selected evidence asset is not authorized for the current destination.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=source_asset_id,
                )
            disposition = authorization.cloud_disposition
            if disposition == CloudDisposition.PROHIBITED:
                raise _fault(
                    "ASSISTANT_EVIDENCE_CLOUD_PROHIBITED",
                    "A selected evidence asset is prohibited from provider or Weave processing.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=source_asset_id,
                )
            if (
                disposition == CloudDisposition.DEDICATED_OR_SELF_MANAGED_ONLY
                and deployment not in {"DEDICATED", "SELF_MANAGED"}
            ):
                raise _fault(
                    "ASSISTANT_EVIDENCE_REQUIRES_DEDICATED_DEPLOYMENT",
                    "A selected evidence asset requires Dedicated or Self-Managed W&B.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=source_asset_id,
                )
            uploaded_asset = current_asset
            representation = "RAW"
            if disposition == CloudDisposition.GENERALIZED_DERIVATIVE_ONLY:
                if source_asset_id in scientific_ids:
                    raise _fault(
                        "ASSISTANT_EVIDENCE_SCIENTIFIC_DERIVATIVE_REQUIRES_REVISION",
                        (
                            "The analysis still references a generalized-only scientific asset; "
                            "create and approve a semantic revision that uses the derivative."
                        ),
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        object_reference=source_asset_id,
                    )
                derivative_id = authorization.generalized_asset_id or ""
                derivative = current_assets.get(derivative_id)
                if (
                    derivative is None
                    or derivative.authorization.cloud_disposition != CloudDisposition.RAW_ALLOWED
                    or deployment not in derivative.authorization.approved_deployments
                ):
                    raise _fault(
                        "ASSISTANT_EVIDENCE_GENERALIZED_DERIVATIVE_UNAVAILABLE",
                        "The approved generalized derivative is missing or unauthorized.",
                        category=ErrorCategory.SECURITY,
                        severity=Severity.BLOCKING,
                        object_reference=source_asset_id,
                    )
                uploaded_asset = derivative
                representation = "GENERALIZED_DERIVATIVE"
                generalized_count += 1
            if not self._assistant_asset_bytes_are_current(store, uploaded_asset):
                raise _fault(
                    "ASSISTANT_EVIDENCE_BYTES_INTEGRITY_FAILED",
                    "Authorized evidence bytes are missing or differ from their reviewed checksum.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=uploaded_asset.asset_id,
                )
            authorization_projection.append(
                {
                    "source_asset_id_sha256": hashlib.sha256(
                        source_asset_id.encode("utf-8")
                    ).hexdigest(),
                    "source_sha256": snapshot_asset.sha256,
                    "uploaded_asset_id_sha256": hashlib.sha256(
                        uploaded_asset.asset_id.encode("utf-8")
                    ).hexdigest(),
                    "uploaded_sha256": uploaded_asset.sha256,
                    "representation": representation,
                    "authorization": authorization.model_dump(mode="json"),
                    "uploaded_authorization": uploaded_asset.authorization.model_dump(mode="json"),
                }
            )

        try:
            items = tuple(
                AuthorizedEvidenceItem(
                    evidence_id=claim.claim_id,
                    claim_sha256=hashlib.sha256(
                        json.dumps(
                            claim.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    context=AuthorizedEvidenceClaimContext(
                        evidence_alias=f"E{index}",
                        claim_kind=claim.claim_kind,
                        value=claim.value,
                        units=claim.units,
                        direct_result=claim.claim_kind.startswith("DIRECT_"),
                        limitations=(
                            [
                                validate_assistant_limitation(item, sensitive_policy)
                                for item in claim.limitations
                            ]
                            if claim.claim_kind == "DIRECT_REFERENCE_COMPARISON"
                            else [
                                item
                                for item in claim.limitations
                                if item in {CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION}
                            ]
                        ),
                    ),
                )
                for index, claim in enumerate(claims, start=1)
            )
        except AssistantSafetyError as exc:
            raise self._assistant_failure(exc) from exc
        except ValidationError as exc:
            raise _fault(
                "ASSISTANT_EVIDENCE_CONTEXT_UNSAFE",
                "Selected evidence cannot be represented in the strict assistant context.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            ) from exc
        selected_evidence_sha256 = hashlib.sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in items],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        authorization = AssistantEvidenceAuthorization.create(
            project_id_sha256=hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
            analysis_id_sha256=hashlib.sha256(analysis_id.encode("utf-8")).hexdigest(),
            model_revision_id_sha256=hashlib.sha256(
                snapshot.revision.revision_id.encode("utf-8")
            ).hexdigest(),
            destination=deployment,
            external_project_id_sha256=hashlib.sha256(
                external_project_id.encode("utf-8")
            ).hexdigest(),
            scope_evidence_sha256=canonical_sha256(scope_payload),
            snapshot_bundle_sha256=canonical_sha256(snapshot.model_dump(mode="json")),
            current_authorizations_sha256=canonical_sha256(authorization_projection),
            selected_evidence_sha256=selected_evidence_sha256,
            authorized_asset_count=len(referenced_asset_ids),
            generalized_derivative_count=generalized_count,
        )
        selection = AuthorizedEvidenceSelection(authorization=authorization, items=items)
        selection.assert_integrity()
        return selection

    def _assistant_explain(self, params: AssistantExplainParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            project_id = store.project()["project_id"]
            prior = store.assistant_call_record(params.call_id)
            if prior is not None:
                if prior["disposition"] == "REJECTED":
                    raise self._assistant_failure(
                        AssistantSafetyError("ASSISTANT_CALL_PREVIOUSLY_REJECTED")
                    )
                answer = AssistantAnswer.model_validate(prior["answer"])
                return self._result("assistant.explain", {"answer": answer.model_dump(mode="json")})
            try:
                analysis_ids = {
                    store.evidence_claim_analysis_id(evidence_id)
                    for evidence_id in params.evidence_ids
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise self._assistant_failure(
                    AssistantSafetyError("ASSISTANT_EVIDENCE_RESOLUTION_INCOMPLETE")
                ) from exc
            if len(analysis_ids) != 1:
                raise self._assistant_failure(
                    AssistantSafetyError("ASSISTANT_EVIDENCE_ANALYSIS_MISMATCH")
                )
            analysis_id = next(iter(analysis_ids))
            association = self._assistant_run_association(
                store,
                run_kind="connectivity_analysis",
                run_subject_id=analysis_id,
                analysis_id=analysis_id,
            )
            sensitive_policy = self._validated_evidence_sensitive_policy(
                store,
                params.evidence_ids,
                params.sensitive_policy,
            )
            authorized_selection = self._authorize_evidence_selection(
                store,
                project_id=project_id,
                analysis_id=analysis_id,
                evidence_ids=params.evidence_ids,
                association=association,
                sensitive_policy=sensitive_policy,
            )
            assistant = self._project_evidence_assistant(store.root)
            try:
                answer = assistant.explain(
                    call_id=params.call_id,
                    project_id=project_id,
                    question=params.question,
                    evidence_ids=params.evidence_ids,
                    association=association,
                    authorized_selection=authorized_selection,
                    sensitive_policy=sensitive_policy,
                )
            except (AssistantSafetyError, PermissionError, RuntimeError) as exc:
                raise self._assistant_failure(exc) from exc
            store.store_assistant_answer(
                params.call_id,
                answer.model_dump(mode="json"),
                answer.trace_id,
            )
        finally:
            store.close()
        return self._result("assistant.explain", {"answer": answer.model_dump(mode="json")})

    def _assistant_follow_up(self, params: AssistantExplainParams) -> PayloadResult:
        result = self._assistant_explain(params)
        return self._result("assistant.follow_up", result.data)

    def _validated_intake_pages(
        self,
        store: EcoProjectStore,
        pages: Sequence[DocumentPage],
        requested_policy: SensitiveContextPolicy,
    ) -> tuple[list[DocumentPage], SensitiveContextPolicy]:
        """Bind renderer page text to exact retained, cloud-authorized document bytes."""

        if requested_policy.exact_location_authorized:
            raise _fault(
                "ASSISTANT_EXACT_LOCATION_AUTHORIZATION_UNAVAILABLE",
                "Exact-location assistant context is not available in this release.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
                remediation="Select an approved generalized derivative for assistant intake.",
            )

        binding = store.observability()
        if binding is None or binding.get("state") != "CONNECTED":
            raise _fault(
                "ASSISTANT_DOCUMENT_DESTINATION_UNVERIFIED",
                "Document intake requires an active, exactly verified W&B destination.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        deployment = str(binding.get("deployment", ""))
        if deployment not in {"WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"}:
            raise _fault(
                "ASSISTANT_DOCUMENT_DESTINATION_UNSUPPORTED",
                "The assistant destination is not an approved W&B deployment.",
                category=ErrorCategory.SECURITY,
                severity=Severity.BLOCKING,
            )
        try:
            bundle_assets = {item.asset_id: item for item in store.effective_bundle().assets}
        except (KeyError, ValidationError, ValueError):
            bundle_assets = {}
        for payload in store.list_records("asset"):
            asset = ConnectivityAsset.model_validate(payload)
            bundle_assets[asset.asset_id] = asset

        documents: dict[str, tuple[ConnectivityAsset, list[str]]] = {}
        for document_id in sorted({page.document_id for page in pages}):
            asset = bundle_assets.get(document_id)
            if asset is None or asset.kind != AssetKind.DOCUMENT:
                raise _fault(
                    "ASSISTANT_DOCUMENT_ASSET_NOT_RETAINED",
                    "Every intake page must reference a retained DOCUMENT asset.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            authorization = asset.authorization
            if deployment not in authorization.approved_deployments:
                raise _fault(
                    "ASSISTANT_DOCUMENT_DEPLOYMENT_UNAUTHORIZED",
                    "The retained document is not authorized for this provider deployment.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            disposition = authorization.cloud_disposition
            if disposition == CloudDisposition.PROHIBITED:
                raise _fault(
                    "ASSISTANT_DOCUMENT_CLOUD_PROHIBITED",
                    "The retained document is prohibited from provider or Weave replication.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            if disposition == CloudDisposition.GENERALIZED_DERIVATIVE_ONLY:
                raise _fault(
                    "ASSISTANT_DOCUMENT_RAW_REPRESENTATION_BLOCKED",
                    "Select the approved retained generalized derivative instead of raw document text.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            if (
                disposition == CloudDisposition.DEDICATED_OR_SELF_MANAGED_ONLY
                and deployment not in {"DEDICATED", "SELF_MANAGED"}
            ):
                raise _fault(
                    "ASSISTANT_DOCUMENT_REQUIRES_DEDICATED_DEPLOYMENT",
                    "The retained document may be used only with Dedicated or Self-Managed W&B.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            if authorization.sensitivity in {
                SensitivityClass.SENSITIVE_LOCATION,
                SensitivityClass.CRITICAL_INFRASTRUCTURE,
                SensitivityClass.TRIBAL_OR_CULTURAL,
            }:
                raise _fault(
                    "ASSISTANT_RAW_SENSITIVE_DOCUMENT_REQUIRES_GENERALIZATION",
                    (
                        "Raw sensitive document context is not authorized for provider or Weave "
                        "processing in this release."
                    ),
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                    remediation="Create, review, and select an approved generalized derivative.",
                )
            try:
                retained = store.resolve_asset_path(asset.relative_path)
                metadata = os.lstat(retained)
            except (FileNotFoundError, OSError, PermissionError) as exc:
                raise _fault(
                    "ASSISTANT_DOCUMENT_BYTES_MISSING",
                    "The retained document bytes are missing or inaccessible.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_size != asset.byte_size
                or _sha256_file(retained) != asset.sha256
            ):
                raise _fault(
                    "ASSISTANT_DOCUMENT_INTEGRITY_FAILED",
                    "The retained document differs from its reviewed checksum and size.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            if asset.media_type.casefold() not in {"text/plain", "text/markdown"}:
                raise _fault(
                    "ASSISTANT_DOCUMENT_VERIFIED_EXTRACTION_UNAVAILABLE",
                    "This release accepts only retained text with verifiable page boundaries.",
                    category=ErrorCategory.UNSUPPORTED_FEATURE,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                )
            try:
                text = retained.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise _fault(
                    "ASSISTANT_DOCUMENT_VERIFIED_EXTRACTION_UNAVAILABLE",
                    "This release accepts only retained UTF-8 page text with verifiable page boundaries.",
                    category=ErrorCategory.UNSUPPORTED_FEATURE,
                    severity=Severity.BLOCKING,
                    object_reference=document_id,
                ) from exc
            documents[document_id] = (asset, text.split("\f"))

        validated_pages: list[DocumentPage] = []
        for page in pages:
            _, retained_pages = documents[page.document_id]
            if (
                page.page_number > len(retained_pages)
                or page.text != retained_pages[page.page_number - 1]
            ):
                raise _fault(
                    "ASSISTANT_DOCUMENT_PAGE_EVIDENCE_MISMATCH",
                    "Submitted page text is not the exact retained page text.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                    object_reference=f"{page.document_id}:page:{page.page_number}",
                )
            validated_pages.append(page)
        return validated_pages, requested_policy.model_copy(
            update={"exact_location_authorized": False}
        )

    def _assistant_intake_extract(self, params: AssistantIntakeExtractParams) -> PayloadResult:
        if self.assistant_runtime is None:
            raise _fault(
                "TRACED_ASSISTANT_UNAVAILABLE",
                "The provider-backed, traced Evidence Intake Assistant is unavailable.",
                category=ErrorCategory.DATA_PROVIDER,
                retryable=True,
            )
        store = self._store(params.project_directory)
        try:
            project_id = store.project()["project_id"]
            prior = store.assistant_call_record(params.call_id)
            if prior is not None:
                if prior["disposition"] == "REJECTED":
                    raise self._assistant_failure(
                        AssistantSafetyError("ASSISTANT_CALL_PREVIOUSLY_REJECTED")
                    )
                draft = IntakeDraft.model_validate(prior["answer"])
                payload = draft.model_dump(mode="json")
                return self._result(
                    "assistant.intake.extract",
                    {
                        "draft": payload,
                        "ready_for_model": False,
                        "human_confirmation_required": True,
                    },
                )
            association = self._assistant_run_association(
                store,
                run_kind="bundle_import",
                run_subject_id=project_id,
            )
            pages, sensitive_policy = self._validated_intake_pages(
                store,
                params.pages,
                params.sensitive_policy,
            )
            try:
                assistant = self.assistant_runtime.intake(project_root=store.root)
                draft = assistant.extract(
                    call_id=params.call_id,
                    project_id=project_id,
                    instruction=params.instruction,
                    pages=pages,
                    association=association,
                    sensitive_policy=sensitive_policy,
                )
            except (AssistantSafetyError, PermissionError, RuntimeError) as exc:
                raise self._assistant_failure(exc) from exc
            payload = draft.model_dump(mode="json")
            store.put_record("assistant_intake_draft", draft.draft_id, payload, allow_update=False)
            store.store_assistant_answer(params.call_id, payload, draft.trace_id)
        finally:
            store.close()
        return self._result(
            "assistant.intake.extract",
            {"draft": payload, "ready_for_model": False, "human_confirmation_required": True},
        )

    def _assistant_intake_get(self, params: AssistantIntakeDraftParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            draft = IntakeDraft.model_validate(
                store.get_record("assistant_intake_draft", params.draft_id)
            )
        finally:
            store.close()
        return self._result(
            "assistant.intake.get",
            {
                "draft": draft.model_dump(mode="json"),
                "ready_for_model": all(field.confirmed for field in draft.fields),
            },
        )

    def _assistant_intake_confirm(self, params: AssistantIntakeConfirmParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            draft = IntakeDraft.model_validate(
                store.get_record("assistant_intake_draft", params.draft_id)
            )
            if draft.project_id != store.project()["project_id"]:
                raise _fault(
                    "ASSISTANT_PROJECT_IDENTITY_MISMATCH",
                    "The intake draft does not belong to this project.",
                    category=ErrorCategory.SECURITY,
                    severity=Severity.BLOCKING,
                )
            confirmed = draft.confirm(params.field_ids, reviewer=params.reviewer)
            payload = confirmed.model_dump(mode="json")
            store.put_record("assistant_intake_draft", confirmed.draft_id, payload)
            ready = all(field.confirmed for field in confirmed.fields)
            confirmed_payload = confirmed.confirmed_model_payload() if ready else None
            if confirmed_payload is not None:
                store.put_record(
                    "confirmed_intake_evidence",
                    confirmed.draft_id,
                    {
                        "draft_id": confirmed.draft_id,
                        "trace_id": confirmed.trace_id,
                        "fields": confirmed_payload,
                        "status": "HUMAN_CONFIRMED_NOT_APPLIED",
                    },
                    allow_update=False,
                )
        finally:
            store.close()
        return self._result(
            "assistant.intake.confirm",
            {
                "draft": payload,
                "ready_for_model": ready,
                "confirmed_fields": confirmed_payload,
                "applied_to_model": False,
            },
        )

    def _assistant_feedback(self, params: AssistantFeedbackParams) -> PayloadResult:
        project_root = self._allowed_path(
            params.project_directory, must_exist=True, purpose="assistant project"
        )
        store = self._store(params.project_directory)
        try:
            policy = self._validated_evidence_sensitive_policy(
                store,
                [],
                SensitiveContextPolicy(),
            )
            try:
                comment = validate_feedback_comment(params.comment, policy)
            except AssistantSafetyError as exc:
                raise self._assistant_failure(exc) from exc
            sink = self.assistant_feedback_sink
            if sink is None and self.assistant_runtime is not None:
                try:
                    sink = self.assistant_runtime.feedback_sink(project_root=project_root)
                except RuntimeError:
                    sink = None
            if sink is None:
                raise _fault(
                    "ASSISTANT_FEEDBACK_TRACE_UNAVAILABLE",
                    "Feedback cannot be recorded without the configured trace sink.",
                    category=ErrorCategory.DATA_PROVIDER,
                )
            record = store.assistant_answer(params.call_id)
            try:
                receipt = sink.record_feedback(
                    call_id=params.call_id,
                    trace_id=record["trace_id"],
                    rating=params.rating,
                    comment=comment,
                )
            except (AssistantSafetyError, PermissionError, RuntimeError, ValueError) as exc:
                raise self._assistant_failure(exc) from exc
            store.put_record(
                "assistant_feedback",
                params.call_id,
                {
                    "rating": params.rating,
                    "comment": comment,
                    "trace_id": record["trace_id"],
                    "receipt": receipt,
                },
                allow_update=False,
            )
        finally:
            store.close()
        return self._result("assistant.feedback", {"recorded": True, "receipt": receipt})

    def _assistant_trace(self, params: AssistantTraceParams) -> PayloadResult:
        store = self._store(params.project_directory)
        try:
            record = store.assistant_call_record(params.call_id)
            if record is None:
                raise KeyError(params.call_id)
        finally:
            store.close()
        if record["disposition"] == "REJECTED":
            return self._result(
                "assistant.get_trace",
                {
                    "call_id": params.call_id,
                    "disposition": "REJECTED",
                    "local_rejection_audit": record["audit"],
                },
            )
        return self._result(
            "assistant.get_trace",
            {
                "call_id": params.call_id,
                "disposition": "ANSWERED",
                "trace_id": record["trace_id"],
                "association": record["answer"]["association"],
            },
        )


def run_jsonl_worker(dispatcher: RPCDispatcher, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        response = dispatcher.handle_frame(line)
        stdout.buffer.write(response) if hasattr(stdout, "buffer") else stdout.write(
            response.decode("utf-8")
        )
        stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EcoCorridor scientific worker")
    parser.add_argument("--transport", choices=["jsonl-stdio"], required=True)
    parser.add_argument("--api-version", required=True)
    parser.add_argument("--app-data-root", required=True)
    parser.add_argument("--engine-executable")
    parser.add_argument("--engine-sha256")
    parser.add_argument("--engine-manifest")
    parser.add_argument("--engine-manifest-file-sha256")
    parser.add_argument("--guided-projection")
    parser.add_argument("--guided-projection-sha256")
    parser.add_argument("--guided-manifest")
    parser.add_argument("--guided-manifest-sha256")
    parser.add_argument("--guided-evidence-archive")
    parser.add_argument("--guided-evidence-archive-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.api_version != API_VERSION:
        print("EcoCorridor worker API version mismatch", file=sys.stderr)
        return 2
    scope_resolver = ProjectScopeResolver()
    observability_credentials = InMemoryCredentialSession(environment_key="WANDB_API_KEY")
    observability_adapter = EcoRpcObservabilityAdapter(
        scope_resolver=scope_resolver,
        store_factory=EcoProjectStore,
        credentials=observability_credentials,
    )
    assistant_runtime = ProviderBackedAssistantRuntime(
        scope_resolver=scope_resolver,
        observability_credentials=observability_credentials,
    )
    dispatcher = RPCDispatcher(
        data_root=arguments.app_data_root,
        engine_executable=arguments.engine_executable,
        engine_sha256=arguments.engine_sha256,
        engine_manifest=arguments.engine_manifest,
        engine_manifest_file_sha256=arguments.engine_manifest_file_sha256,
        guided_projection_path=arguments.guided_projection,
        guided_projection_sha256=arguments.guided_projection_sha256,
        guided_manifest_path=arguments.guided_manifest,
        guided_manifest_sha256=arguments.guided_manifest_sha256,
        guided_evidence_archive=arguments.guided_evidence_archive,
        guided_evidence_archive_sha256=arguments.guided_evidence_archive_sha256,
        scope_resolver=scope_resolver,
        observability_adapter=observability_adapter,
        observability_credentials=observability_credentials,
        assistant_runtime=assistant_runtime,
    )
    try:
        run_jsonl_worker(dispatcher, sys.stdin, sys.stdout)
    finally:
        dispatcher.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
