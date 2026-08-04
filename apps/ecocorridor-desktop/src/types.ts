import type { ConnectivityCostBasis, ConnectivityDecisionBrief, ConnectivityDecisionRule, ConnectivityExportEligibility, ConnectivityProjectBundle, ConnectivityRunPlan, ConnectivityScenario, EngineManifest, FocalAreaSet, GridAlignmentPlan, InterventionOption, InterventionSite, ModelRevision, PortfolioDecisionSummary, ResistanceHypothesis, ResourcePolicy, RiskReviewMask, SafeguardRecord, SensitiveDataAuthorization } from "./generated/contracts";

export type AppMode = "desktop" | "public-replay";
export type EntryPath = "existing" | "planning" | "guided";

export type WorkflowStepId =
  | "overview"
  | "study-context"
  | "source-assets"
  | "grid-alignment"
  | "hypotheses"
  | "focal-areas"
  | "model-fitness"
  | "scenarios"
  | "interventions"
  | "safeguards"
  | "rules-costs"
  | "run-plan"
  | "direct-runs"
  | "robustness"
  | "evidence-package";

export interface WorkflowStep {
  id: WorkflowStepId;
  label: string;
  shortLabel: string;
  phase: "assemble" | "review" | "compare" | "share";
}

export interface RidgewayScenario {
  id: string;
  guild: string;
  purpose: "target" | "safeguard";
  hypothesis: string;
  futureCondition: string;
  maturity: string;
  includedPairs: number;
}

export interface RidgewayScenarioResult {
  scenarioId: string;
  resistanceChangePct: number;
  ruleStatus: "pass" | "fail" | "review";
  reviewMaskChangePct: number;
  runId: string;
  baselineMapRunId: string;
  portfolioMapRunId: string;
  pairResults: RidgewayPairResult[];
}

export interface RidgewayPairResult {
  pairId: string;
  baselineEffectiveResistance: number;
  portfolioEffectiveResistance: number;
  baselineRelativeChangePct: number;
  passed: boolean;
  claimId: string;
  baselineRunId: string;
  portfolioRunId: string;
  baselineMatrixSha256: string;
  portfolioMatrixSha256: string;
}

export interface RidgewayCurrentRaster {
  runId: string;
  portfolioId: string;
  scenarioId: string;
  sha256: string;
  width: number;
  height: number;
  declaredNodata: number;
  nodataTopologySha256: string;
  minimumCurrent: number;
  maximumCurrent: number;
  cells: Array<number | null>;
}

export interface RidgewayReviewMaskProjection {
  maskId: string;
  sha256: string;
  width: number;
  height: number;
  cells: Array<0 | 1>;
  interpretation: string;
}

export type RidgewayHypothesisId =
  | "h1_low_barrier"
  | "h2_moderate_barrier"
  | "h3_high_barrier"
  | "h4_future_pressure";

export interface RidgewayHypothesisRankObservation {
  rank: number;
  score: number;
}

export type RidgewayHypothesisRanks = Record<
  RidgewayHypothesisId,
  RidgewayHypothesisRankObservation
>;

export interface RidgewayResourceObservability {
  jobsWithObservations: number;
  jobsWithoutObservations: number;
  directSolverWallSeconds: number;
  estimatedEnergyKwh: number | null;
  estimatedEnergyLowerKwh: number | null;
  estimatedEnergyUpperKwh: number | null;
  maximumObservedSingleProcessMemoryBytes: number | null;
  peakMemoryIsSampledLowerBound: boolean;
  peakMemoryStatus: "MEASURED" | "ESTIMATED" | "NOT_MEASURED" | "MIXED" | "NOT_RECORDED";
  energyStatus: "MEASURED" | "ESTIMATED" | "NOT_MEASURED" | "MIXED" | "NOT_RECORDED";
  proposerWallTimeStatus: "NOT_APPLICABLE_FIXED_ENUMERATION";
  limitation: string;
}

export interface RidgewayPortfolio {
  projectionKey: string;
  id: string | null;
  name: string;
  role: "caution" | "carry-forward" | "over-budget" | "brittle";
  costUsd: number | null;
  ruleStatus: "pass" | "fail" | "review" | "unavailable";
  passCount: number | null;
  requiredCount: number;
  worstResistanceChangePct: number | null;
  worstScenarioId: string | null;
  roadMaskFlag: boolean | null;
  nextAction: string;
  optionSummary: string;
  hypothesisRanks: RidgewayHypothesisRanks | null;
  scenarioResults: RidgewayScenarioResult[];
}

export interface RidgewayFixture {
  schemaVersion: string;
  fixtureId: string;
  baselinePortfolioId: string | null;
  title: string;
  cc0: boolean;
  manifestSha256: string | null;
  manifestVersion: string | null;
  evidenceState: "ENGINE_RUN_REQUIRED" | "VERIFIED_DIRECT_EVIDENCE";
  resourceObservability: RidgewayResourceObservability | null;
  releaseReplayEligible: boolean;
  releaseBlocker: string;
  projectionGeneratedAt: string;
  disclaimer: string;
  decisionBrief: {
    question: string;
    budgetUsd: number;
    targetRule: string;
    safeguardRule: string;
    nonGoals: string[];
  };
  study: {
    portfolioCount: number;
    scenarioCount: number;
    matrixJobCount: number;
    mapJobCount: number;
    coreCount: number;
    interventionSiteCount: number;
    hypothesisRankingMetric: string | null;
  };
  currentRasters: RidgewayCurrentRaster[];
  reviewMask: RidgewayReviewMaskProjection | null;
  scenarios: RidgewayScenario[];
  portfolios: RidgewayPortfolio[];
}

export interface EvidenceSelection {
  claimId: string;
  title: string;
  value: string;
  units: string;
  status: "verified" | "projection" | "review";
  portfolioId: string;
  scenarioId: string;
  runId: string;
  baselineRunId?: string;
  reducer: string;
  assumption: string;
  source?: "GUIDED_PROJECTION" | "PROFESSIONAL_CLAIM";
}

export interface ModelFinding {
  code: string;
  severity: "BLOCKING" | "REVIEW_REQUIRED" | "INFORMATION";
  object_reference: string;
  remediation: string;
}

export interface ProfessionalProjectState {
  project: { project_id: string; title: string; mode: string };
  active_revision: ModelRevision;
  effective_bundle: ConnectivityProjectBundle;
  resource_policy: ResourcePolicy;
  fitness: ConnectivityProjectBundle["fitness"];
  saved_records: Record<string, Record<string, unknown>[]>;
  analyses: Array<{ analysis_id: string; state: string; created_at: string; updated_at: string }>;
  pending_recovery: {
    required: boolean;
    analyses: Array<{ analysis_id: string; analysis_state: "RUNNING" | "PAUSING"; executor_active: boolean; recovery_required: boolean; running_job_ids: string[] }>;
    next_action: string;
  };
  sync_status: Record<string, unknown>;
  validation: Record<string, unknown>;
  engine: { available: boolean; manifest_sha256: string | null; engine_name: string | null; circuitscape_version: string | null };
}

export interface ProfessionalAnalysisPreparation {
  project_id: string;
  revision_id: string;
  scenario_ids: string[];
  resource_policy: ResourcePolicy;
  engine_available: boolean;
  engine_manifest: EngineManifest | null;
  finite_universe_size: number;
  portfolio_capacity: number;
  selected_portfolio_count: number;
  direct_job_count: number;
  arithmetic: string;
  blockers: ModelFinding[];
  search: { method: string; qmc_engine: string | null; finite_universe_size: number; portfolio_capacity: number; selected_portfolio_count: number; seed: number } | null;
  proposed_plan: ConnectivityRunPlan | null;
}

export interface ProfessionalAnalysisStatus {
  analysis_id: string;
  state: string;
  plan: ConnectivityRunPlan;
  completed_run_count: number;
  summary_count: number;
  evidence_claim_count: number;
  direct_job_counts: Record<string, number>;
  error: Record<string, unknown> | null;
}

export interface ProfessionalDecisionBoard {
  analysis_id: string;
  summaries: PortfolioDecisionSummary[];
  direct_run_ids: string[];
  evidence_claim_ids: string[];
  claim_language: "Direct Circuitscape result under documented assumptions.";
}

export interface ProfessionalExportRecord {
  exportId: string;
  projectRelativeDirectory: string;
  manifest: Record<string, unknown>;
  copyReceipt: ExportCopyReceipt | null;
}

export interface ProfessionalAnalysisWorkspace {
  projectState: ProfessionalProjectState | null;
  preparation: ProfessionalAnalysisPreparation | null;
  plannedAnalysisId: string | null;
  status: ProfessionalAnalysisStatus | null;
  decisionBoard: ProfessionalDecisionBoard | null;
  selectedPortfolioId: string | null;
  exportEligibility: ConnectivityExportEligibility | null;
  exportRecord: ProfessionalExportRecord | null;
}

export interface PlanningState {
  projectName: string;
  decisionQuestion: string;
  intendedUse: string;
  studyArea: string;
  reviewer: string;
  targetGuild: string;
  assetNames: string[];
  alignmentMethod: "nearest" | "bilinear";
  alignmentApproved: boolean;
  hypothesisRationale: string;
  focalRationale: string;
  interventionBasis: string;
  safeguardNotes: string;
  budgetUsd: string;
}

export interface HostBridge {
  request<T = unknown>(method: string, params: Record<string, unknown>): Promise<T>;
  chooseConnectivityBundle?(): Promise<OpaqueFileHandle | null>;
  chooseConnectivityAsset?(): Promise<OpaqueFileHandle | null>;
  createProjectWorkspace?(): Promise<OpaqueProjectHandle>;
  listProjectWorkspaces?(): Promise<RecentProjectHandle[]>;
  openProjectWorkspace?(projectHandle: string): Promise<OpaqueProjectHandle>;
  saveExportCopy?(projectHandle: string, exportId: string): Promise<ExportCopyReceipt | null>;
}

export interface OpaqueFileHandle {
  handle: string;
  displayName: string;
  byteSize: number;
  sha256: string;
}

export interface OpaqueProjectHandle {
  handle: string;
}

export interface RecentProjectHandle extends OpaqueProjectHandle {
  lastModifiedMs: number;
}

export interface ReopenableProject {
  projectHandle: string;
  lastModifiedMs: number;
  projectState: ProfessionalProjectState;
}

export interface ExportCopyReceipt {
  exportId: string;
  copied: true;
  destinationLabel: string;
  fileCount: number;
  byteSize: number;
}

export interface RpcPayload<T> {
  api_version: "1.0";
  method: string;
  data: T;
  data_sha256: string;
}

export interface ObservabilityPreflight {
  preflightId: string;
  entity: string;
  project: string;
  deployment: "DEDICATED" | "SELF_MANAGED";
}

export type ProjectBindingState =
  | "PREFLIGHT_VERIFIED"
  | "CONNECTING"
  | "CONNECTED"
  | "SYNCING"
  | "SYNC_QUEUED_OFFLINE"
  | "BLOCKED_PROJECT_POLICY"
  | "AUTHENTICATION_REQUIRED"
  | "FAILED_RETRYING"
  | "FAILED";

export interface ProfessionalProjectSession {
  projectHandle: string;
  projectId: string;
  title: string;
  mode: "IMPORTED" | "PLANNING_DRAFT";
  bindingState: ProjectBindingState;
  bindingMessage: string;
}

export type ProfessionalProjectCandidate = Omit<ProfessionalProjectSession, "bindingState" | "bindingMessage">;

export type ConnectivityAssetKind =
  | "RESISTANCE_RASTER"
  | "FOCAL_RASTER"
  | "ACTION_EFFECT"
  | "REVIEW_MASK"
  | "VECTOR_CONTEXT"
  | "DOCUMENT"
  | "REFERENCE_MATRIX"
  | "REFERENCE_CURRENT";

export type AssetSensitivity =
  | "PUBLIC"
  | "INTERNAL"
  | "SENSITIVE_LOCATION"
  | "CRITICAL_INFRASTRUCTURE"
  | "TRIBAL_OR_CULTURAL"
  | "LICENSE_RESTRICTED";

export type AssetCloudDisposition =
  | "RAW_ALLOWED"
  | "GENERALIZED_DERIVATIVE_ONLY"
  | "DEDICATED_OR_SELF_MANAGED_ONLY"
  | "PROHIBITED";

export type AssetDeployment = "WANDB_CLOUD" | "DEDICATED" | "SELF_MANAGED" | "LOCAL_ONLY";
export type RasterValueKind = "CONTINUOUS" | "CATEGORICAL" | "IDENTIFIER" | "MASK";
export type RasterFormat = "GEOTIFF" | "ESRI_ASCII";

export interface RasterGrid {
  grid_id: string;
  crs: string;
  width: number;
  height: number;
  transform: [number, number, number, number, number, number];
  nodata: number | null;
  value_kind: RasterValueKind;
  cell_area_m2: number;
}

export interface RasterInspection {
  format: RasterFormat;
  sha256: string;
  byte_size: number;
  grid: RasterGrid;
  data_cell_count: number;
  nodata_cell_count: number;
  minimum: number;
  maximum: number;
  unique_value_count: number;
}

export interface ProfessionalAssetRecord {
  assetId: string;
  title: string;
  kind: ConnectivityAssetKind;
  displayName: string;
  relativePath: string;
  sha256: string;
  byteSize: number;
  mediaType: string;
  source: string;
  vintage: string;
  license: string;
  sensitivity: AssetSensitivity;
  cloudDisposition: AssetCloudDisposition;
  exportAllowed: boolean;
  approvedDeployments: AssetDeployment[];
  reviewer: string;
  reviewedAt: string;
  intendedUse: string;
  revision: number | string;
  grid: RasterGrid | null;
  authorization: SensitiveDataAuthorization;
}

export interface ProfessionalGridApproval {
  plan: GridAlignmentPlan;
  approvalId: string;
  derivedAssetId: string;
  revisionId: string;
}

export interface ProfessionalModelRecords {
  hypotheses: ResistanceHypothesis[];
  focalAreaSets: FocalAreaSet[];
  scenarios: ConnectivityScenario[];
  interventionSites: InterventionSite[];
  interventionOptions: InterventionOption[];
  safeguards: SafeguardRecord[];
  reviewMasks: RiskReviewMask[];
  decisionRules: ConnectivityDecisionRule[];
  decisionBrief: ConnectivityDecisionBrief | null;
  costBasis: ConnectivityCostBasis | null;
  resourcePolicy: ResourcePolicy | null;
}

export interface GuidedDesktopSession {
  sessionId: string;
  projectHandle: string;
  evidenceManifestSha256: string | null;
  canRerun: boolean;
  initialRerunState: GuidedOpenData["state"];
}

export interface GuidedOpenData {
  session_id: string;
  projection: unknown;
  can_rerun: boolean;
  state: "VERIFIED_FIXTURE_REPLAY" | "ENGINE_REQUIRED" | "RUNNING" | "PAUSING" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  evidence_manifest_sha256: string | null;
}

export type GuidedEventKind =
  | "ENTERED"
  | "COMPLETED"
  | "BACKTRACKED"
  | "EVIDENCE_OPENED"
  | "ALTERNATIVE_COMPARED"
  | "RERUN_REQUESTED"
  | "EXPORT_COMPLETED";

export interface GuidedEventDraft {
  checkpointId: WorkflowStepId;
  eventKind: GuidedEventKind;
  durationMs?: number;
  evidenceIds?: string[];
  portfolioIds?: string[];
}

declare global {
  interface Window {
    __ECOCORRIDOR_HOST__?: HostBridge;
    __TAURI_INTERNALS__?: unknown;
  }
}
