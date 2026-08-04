import { ConnectivityCostBasisSchema, ConnectivityExportEligibilitySchema, ConnectivityModelFitnessSchema, ConnectivityProjectBundleSchema, ConnectivityRunPlanSchema, ConnectivityScenarioSchema, EngineManifestSchema, GridAlignmentApprovalSchema, GridAlignmentPlanSchema, InterventionOptionSchema, InterventionSiteSchema, ModelRevisionSchema, PortfolioDecisionSummarySchema, ResourcePolicySchema, SemanticModelDiffSchema, SensitiveDataAuthorizationSchema } from "../generated/contracts";
import { z } from "zod";
import type { AssetCloudDisposition, AssetDeployment, AssetSensitivity, ConnectivityAssetKind, OpaqueFileHandle, PlanningState, ProfessionalAssetRecord, ProfessionalGridApproval, ProfessionalModelRecords, ProfessionalProjectState, RasterFormat, RasterInspection, RasterValueKind } from "../types";

const ConnectivityAssetSchema = ConnectivityProjectBundleSchema.shape.assets.element;

const FITNESS_KEYS = [
  "intended_use",
  "movement_process",
  "focal_rationale",
  "resistance_derivation",
  "validation_evidence",
  "spatial_scale",
  "boundary_effects",
  "action_effect_evidence",
  "scenario_coverage",
  "sensitivity",
  "numerical_qa",
  "unresolved_assumptions",
] as const;

export const ModelFindingSchema = z.object({
  code: z.string().min(1),
  severity: z.enum(["BLOCKING", "REVIEW_REQUIRED", "INFORMATION"]),
  object_reference: z.string(),
  remediation: z.string().min(1),
}).strict();

export const ModelValidationResponseSchema = z.object({
  status: z.enum(["READY", "BLOCKED"]),
  findings: z.array(ModelFindingSchema),
  project_id: z.string().nullable().optional(),
  revision_id: z.string().nullable().optional(),
  scenario_count: z.number().int().nonnegative().nullable().optional(),
  target_scenario_count: z.number().int().nonnegative().nullable().optional(),
  safeguard_scenario_count: z.number().int().nonnegative().nullable().optional(),
  asset_count: z.number().int().nonnegative().nullable().optional(),
  intervention_site_count: z.number().int().nonnegative().nullable().optional(),
  retained_source_validation: z.boolean().nullable().optional(),
}).strict();

export const ProfessionalProjectStateSchema = z.object({
  project: z.object({ project_id: z.string().min(1), title: z.string().min(1), mode: z.string().min(1) }).strict(),
  active_revision: ModelRevisionSchema,
  effective_bundle: ConnectivityProjectBundleSchema,
  fitness: ConnectivityModelFitnessSchema,
  resource_policy: ResourcePolicySchema,
  saved_records: z.record(z.string(), z.array(z.record(z.string(), z.unknown()))),
  analyses: z.array(z.object({ analysis_id: z.string().min(1), state: z.string().min(1), created_at: z.string().min(1), updated_at: z.string().min(1) }).strict()),
  pending_recovery: z.object({
    required: z.boolean(),
    analyses: z.array(z.object({
      analysis_id: z.string().min(1),
      analysis_state: z.enum(["RUNNING", "PAUSING"]),
      executor_active: z.boolean(),
      recovery_required: z.boolean(),
      running_job_ids: z.array(z.string().min(1)),
    }).strict()),
    next_action: z.string().min(1),
  }).strict(),
  sync_status: z.record(z.string(), z.unknown()),
  validation: z.record(z.string(), z.unknown()),
  engine: z.object({ available: z.boolean(), manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/).nullable(), engine_name: z.string().nullable(), circuitscape_version: z.string().nullable() }).strict(),
}).strict();

export interface HydratedProfessionalProject {
  state: ProfessionalProjectState;
  planning: PlanningState;
  assets: ProfessionalAssetRecord[];
  gridApprovals: ProfessionalGridApproval[];
  records: ProfessionalModelRecords;
  activeAnalysisId: string | null;
  recoveryAnalysisId: string | null;
}

function intendedUseFromAuthorization(notes: string | undefined, kind: ConnectivityAssetKind): string {
  const prefix = "Intended use:";
  if (notes?.startsWith(prefix)) {
    return notes.slice(prefix.length).split("\nAuthorization notes:", 1)[0]?.trim() || `Retained ${kind.toLowerCase().replaceAll("_", " ")} evidence.`;
  }
  return `Retained ${kind.toLowerCase().replaceAll("_", " ")} evidence.`;
}

/**
 * Hydrate every professional editor from one worker-validated project snapshot.
 * The renderer never reconstructs ecological records from local storage and it
 * never treats component state as authority.
 */
export function hydrateProfessionalProject(value: unknown): HydratedProfessionalProject {
  const state = ProfessionalProjectStateSchema.parse(value) as ProfessionalProjectState;
  const bundle = state.effective_bundle;
  const assets: ProfessionalAssetRecord[] = bundle.assets.map((asset) => ({
    assetId: asset.asset_id,
    title: asset.title,
    kind: asset.kind,
    displayName: asset.relative_path.split("/").at(-1) ?? asset.relative_path,
    relativePath: asset.relative_path,
    sha256: asset.sha256,
    byteSize: asset.byte_size,
    mediaType: asset.media_type,
    source: asset.source,
    vintage: asset.vintage,
    license: asset.license,
    sensitivity: asset.authorization.sensitivity,
    cloudDisposition: asset.authorization.cloud_disposition,
    exportAllowed: asset.authorization.export_allowed,
    approvedDeployments: asset.authorization.approved_deployments,
    reviewer: asset.authorization.reviewer,
    reviewedAt: asset.authorization.reviewed_at,
    intendedUse: intendedUseFromAuthorization(asset.authorization.notes, asset.kind),
    revision: state.active_revision.revision_id,
    grid: asset.grid ?? null,
    authorization: asset.authorization,
  }));

  const plans = (state.saved_records.grid_alignment_plan ?? []).map((item) => GridAlignmentPlanSchema.parse(item));
  const planById = new Map(plans.map((plan) => [plan.plan_id, plan]));
  const diffs = (state.saved_records.semantic_diff ?? []).map((item) => SemanticModelDiffSchema.parse(item));
  const gridApprovals = (state.saved_records.grid_alignment_approval ?? []).map((item) => {
    const approval = GridAlignmentApprovalSchema.parse(item);
    const plan = planById.get(approval.plan_id);
    if (!plan) throw new Error(`Alignment approval ${approval.approval_id} has no retained plan.`);
    const derived = bundle.assets.find((asset) => asset.source.endsWith(`:alignment:${plan.plan_id}`));
    if (!derived) throw new Error(`Alignment approval ${approval.approval_id} has no retained derived asset.`);
    const diff = diffs.find((candidate) => candidate.changes.some((change) => change.object_id === derived.asset_id));
    return {
      plan,
      approvalId: approval.approval_id,
      derivedAssetId: derived.asset_id,
      revisionId: diff?.to_revision_id ?? state.active_revision.revision_id,
    };
  });

  const records: ProfessionalModelRecords = {
    hypotheses: bundle.resistance_hypotheses,
    focalAreaSets: bundle.focal_area_sets,
    scenarios: bundle.scenarios,
    interventionSites: bundle.intervention_sites,
    interventionOptions: bundle.intervention_options,
    safeguards: bundle.safeguards ?? [],
    reviewMasks: bundle.review_masks ?? [],
    decisionRules: bundle.decision_rules ?? [],
    decisionBrief: bundle.decision_brief,
    costBasis: bundle.cost_basis ?? null,
    resourcePolicy: bundle.resource_policy,
  };

  let intendedUse = bundle.decision_brief.intended_use;
  let studyArea = "";
  if (state.project.mode === "PLANNING_DRAFT") {
    const marker = " Study context: ";
    const markerIndex = intendedUse.lastIndexOf(marker);
    if (markerIndex >= 0) {
      studyArea = intendedUse.slice(markerIndex + marker.length).replace(/\.$/, "").trim();
      intendedUse = intendedUse.slice(0, markerIndex).trim();
    }
  }
  const firstHypothesis = records.hypotheses[0];
  const firstFocalSet = records.focalAreaSets[0];
  const firstEffect = records.interventionOptions.flatMap((option) => option.effects)[0];
  const planning: PlanningState = {
    projectName: bundle.title,
    decisionQuestion: bundle.decision_brief.decision_question,
    intendedUse,
    studyArea,
    reviewer: bundle.decision_brief.decision_owner,
    targetGuild: bundle.decision_brief.target_guilds[0] ?? "",
    assetNames: bundle.assets.map((asset) => asset.title),
    alignmentMethod: plans.some((plan) => plan.method === "BILINEAR") ? "bilinear" : "nearest",
    alignmentApproved: gridApprovals.length > 0,
    hypothesisRationale: firstHypothesis?.rationale ?? "",
    focalRationale: firstFocalSet?.rationale ?? "",
    interventionBasis: firstEffect?.basis.purpose ?? "",
    safeguardNotes: records.safeguards.flatMap((record) => record.concerns ?? []).join("\n"),
    budgetUsd: bundle.decision_brief.budget_amount == null ? "" : String(bundle.decision_brief.budget_amount),
  };
  const recoveryAnalysisId = state.pending_recovery.analyses.find((analysis) => analysis.recovery_required)?.analysis_id ?? null;
  const latestAnalysis = [...state.analyses]
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.analysis_id.localeCompare(left.analysis_id))[0];
  const activeAnalysisId = recoveryAnalysisId ?? latestAnalysis?.analysis_id ?? null;
  return { state, planning, assets, gridApprovals, records, activeAnalysisId, recoveryAnalysisId };
}

export const AnalysisPrepareResponseSchema = z.object({
  project_id: z.string().min(1),
  revision_id: z.string().min(1),
  scenario_ids: z.array(z.string().min(1)),
  resource_policy: ResourcePolicySchema,
  engine_available: z.boolean(),
  engine_manifest: EngineManifestSchema.nullable(),
  finite_universe_size: z.number().int().nonnegative(),
  portfolio_capacity: z.number().int().nonnegative(),
  selected_portfolio_count: z.number().int().nonnegative(),
  direct_job_count: z.number().int().nonnegative(),
  arithmetic: z.string().min(1),
  blockers: z.array(ModelFindingSchema),
  search: z.object({ method: z.string().min(1), qmc_engine: z.string().nullable(), finite_universe_size: z.number().int().positive(), portfolio_capacity: z.number().int().positive(), selected_portfolio_count: z.number().int().positive(), seed: z.number().int().nonnegative() }).strict().nullable(),
  proposed_plan: ConnectivityRunPlanSchema.nullable(),
}).strict();

export const AnalysisPlanReceiptSchema = z.object({
  analysis_id: z.string().min(1),
  state: z.literal("READY"),
  direct_job_count: z.number().int().positive(),
  review_findings: z.array(ModelFindingSchema),
  arithmetic: z.string().min(1),
}).strict();

export const AnalysisStatusResponseSchema = z.object({
  analysis_id: z.string().min(1),
  state: z.string().min(1),
  plan: ConnectivityRunPlanSchema,
  completed_run_count: z.number().int().nonnegative(),
  summary_count: z.number().int().nonnegative(),
  evidence_claim_count: z.number().int().nonnegative(),
  direct_job_counts: z.record(z.string(), z.number().int().nonnegative()),
  error: z.record(z.string(), z.unknown()).nullable(),
}).strict();

export const AnalysisStateResponseSchema = z.object({ analysis_id: z.string().min(1), state: z.string().min(1) }).strict();

export const GuidedRerunStatusSchema = z.object({
  session_id: z.string().min(1),
  analysis_id: z.string().min(1),
  state: z.enum(["RUNNING", "PAUSING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]),
  phase: z.enum(["MATRIX", "MAP", "DIFFERENTIAL", "COMPLETE"]),
  completed_jobs: z.number().int().min(0).max(1596),
  total_jobs: z.literal(1596),
  matrix_completed: z.number().int().min(0).max(1536),
  map_completed: z.number().int().min(0).max(60),
  cached_jobs: z.number().int().min(0).max(1596),
  differential: z.record(z.string(), z.unknown()).nullable(),
}).strict().superRefine((value, context) => {
  if (value.completed_jobs !== value.matrix_completed + value.map_completed) context.addIssue({ code: "custom", message: "Completed jobs must equal matrix plus map jobs." });
  if (value.cached_jobs > value.completed_jobs) context.addIssue({ code: "custom", message: "Cached jobs cannot exceed completed jobs." });
  if (value.state === "COMPLETED" && (value.phase !== "COMPLETE" || value.completed_jobs !== value.total_jobs)) context.addIssue({ code: "custom", message: "Completed reruns require all 1,596 jobs and the COMPLETE phase." });
});

export const GuidedControlResponseSchema = z.object({ session_id: z.string().min(1), state: z.string().min(1) }).strict();

export const DecisionBoardResponseSchema = z.object({
  analysis_id: z.string().min(1),
  summaries: z.array(PortfolioDecisionSummarySchema).min(1),
  direct_run_ids: z.array(z.string().min(1)),
  evidence_claim_ids: z.array(z.string().min(1)),
  claim_language: z.literal("Direct Circuitscape result under documented assumptions."),
}).strict();

const Sha256Schema = z.string().regex(/^[a-f0-9]{64}$/);

const ScenarioDetailRunSchema = z.object({
  direct_run_id: z.string().min(1),
  portfolio_id: z.string().min(1),
  scenario_id: z.string().min(1),
  exact: z.literal(true),
  engine_name: z.literal("Circuitscape"),
  engine_manifest_sha256: Sha256Schema,
  model_run_cache_key: Sha256Schema,
  result_sha256: Sha256Schema,
  matrix_sha256: Sha256Schema,
  current_map_sha256: Sha256Schema.nullable(),
  qa_status: z.enum(["ACCEPTED", "WARNING_ACCEPTED"]),
  qa_findings: z.array(z.string()),
}).strict();

const ScenarioPairEvidenceSchema = z.object({
  pair_id: z.string().min(1),
  required: z.boolean(),
  baseline_effective_resistance: z.number().positive(),
  portfolio_effective_resistance: z.number().positive(),
  baseline_relative_reduction_percent: z.number().finite(),
  baseline_direct_run_id: z.string().min(1),
  portfolio_direct_run_id: z.string().min(1),
  baseline_matrix_sha256: Sha256Schema,
  portfolio_matrix_sha256: Sha256Schema,
  evidence_claim_ids: z.array(z.string().min(1)).min(1),
  primary_evidence_claim_id: z.string().min(1),
}).strict().superRefine((value, context) => {
  if (!value.evidence_claim_ids.includes(value.primary_evidence_claim_id)) context.addIssue({ code: "custom", message: "Primary pair claim is absent from the retained claim set." });
});

const ScenarioRasterGridSchema = z.object({
  grid_id: z.string().min(1),
  crs: z.string().min(1),
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  transform: z.tuple([z.number(), z.number(), z.number(), z.number(), z.number(), z.number()]),
  nodata: z.number().nullable(),
  value_kind: z.enum(["CONTINUOUS", "CATEGORICAL", "IDENTIFIER", "MASK"]),
  cell_area_m2: z.number().positive(),
}).strict();

const CurrentMapArtifactSchema = z.object({
  direct_run_id: z.string().min(1),
  artifact_id: z.string().min(1),
  sha256: Sha256Schema,
  byte_size: z.number().int().positive(),
  media_type: z.string().min(1),
}).strict();

const CurrentMapPreviewCellSchema = z.object({
  preview_row: z.number().int().nonnegative(),
  preview_column: z.number().int().nonnegative(),
  source_row: z.number().int().nonnegative(),
  source_column: z.number().int().nonnegative(),
  baseline_current: z.number().nonnegative().nullable(),
  portfolio_current: z.number().nonnegative().nullable(),
}).strict();

const CurrentMapPreviewSchema = z.object({
  width: z.number().int().min(1).max(64),
  height: z.number().int().min(1).max(64),
  selection_method: z.literal("SYSTEMATIC_EXACT_CELL_SAMPLE"),
  baseline_minimum: z.number().nonnegative(),
  baseline_maximum: z.number().nonnegative(),
  portfolio_minimum: z.number().nonnegative(),
  portfolio_maximum: z.number().nonnegative(),
  cells: z.array(CurrentMapPreviewCellSchema).min(1).max(4096),
}).strict().superRefine((value, context) => {
  if (value.cells.length !== value.width * value.height) context.addIssue({ code: "custom", message: "Current-map preview is incomplete." });
});

const CurrentMapExactCellPageSchema = z.object({
  offset: z.number().int().nonnegative(),
  limit: z.number().int().min(1).max(500),
  total_cell_count: z.number().int().positive(),
  has_previous: z.boolean(),
  has_next: z.boolean(),
  cells: z.array(z.object({
    linear_index: z.number().int().nonnegative(),
    row: z.number().int().nonnegative(),
    column: z.number().int().nonnegative(),
    x_center: z.number().finite(),
    y_center: z.number().finite(),
    baseline_current: z.number().nonnegative().nullable(),
    portfolio_current: z.number().nonnegative().nullable(),
    portfolio_minus_baseline: z.number().finite().nullable(),
  }).strict()).max(500),
}).strict();

const CurrentMapInterpretation = z.literal("Diagnostic current redistribution; not observed or predicted animal use.");
const ScenarioMapEvidenceSchema = z.discriminatedUnion("status", [
  z.object({
    status: z.literal("AVAILABLE"),
    absence_code: z.null().optional(),
    absence_reason: z.null().optional(),
    grid: ScenarioRasterGridSchema,
    baseline_artifact: CurrentMapArtifactSchema,
    portfolio_artifact: CurrentMapArtifactSchema,
    evidence_claim_ids: z.array(z.string().min(1)),
    preview: CurrentMapPreviewSchema,
    exact_cell_page: CurrentMapExactCellPageSchema,
    interpretation: CurrentMapInterpretation,
  }).strict(),
  z.object({
    status: z.literal("ABSENT"),
    absence_code: z.string().min(1),
    absence_reason: z.string().min(1),
    grid: z.null().optional(),
    baseline_artifact: z.null().optional(),
    portfolio_artifact: z.null().optional(),
    evidence_claim_ids: z.array(z.string()).length(0),
    preview: z.null().optional(),
    exact_cell_page: z.null().optional(),
    interpretation: CurrentMapInterpretation,
  }).strict(),
]);

export const ScenarioDetailResponseSchema = z.object({
  analysis_id: z.string().min(1),
  portfolio_id: z.string().min(1),
  baseline_portfolio_id: z.string().min(1),
  model_revision_id: z.string().min(1),
  scenario: ConnectivityScenarioSchema,
  baseline_run: ScenarioDetailRunSchema,
  portfolio_run: ScenarioDetailRunSchema,
  pair_evidence: z.array(ScenarioPairEvidenceSchema).min(1),
  map_evidence: ScenarioMapEvidenceSchema,
  same_scenario_only: z.literal(true),
  claim_language: z.literal("Direct Circuitscape result under documented assumptions."),
  limitations: z.array(z.string().min(1)).min(1),
}).strict().superRefine((value, context) => {
  if (value.baseline_run.scenario_id !== value.scenario.scenario_id || value.portfolio_run.scenario_id !== value.scenario.scenario_id) context.addIssue({ code: "custom", message: "Scenario detail mixes direct-run scenarios." });
  if (value.baseline_run.portfolio_id !== value.baseline_portfolio_id || value.portfolio_run.portfolio_id !== value.portfolio_id) context.addIssue({ code: "custom", message: "Scenario detail mixes portfolio identities." });
  if (value.baseline_run.engine_manifest_sha256 !== value.portfolio_run.engine_manifest_sha256) context.addIssue({ code: "custom", message: "Scenario detail mixes engine manifests." });
  if (value.pair_evidence.map((item) => item.pair_id).join("\u0000") !== value.scenario.included_pair_ids.join("\u0000")) context.addIssue({ code: "custom", message: "Scenario detail pair scope differs from the scenario contract." });
});

export type ScenarioDetailResponse = z.infer<typeof ScenarioDetailResponseSchema>;

export const ExportEligibilityResponseSchema = z.object({ eligibility: ConnectivityExportEligibilitySchema }).strict();

export const ExportCreateResponseSchema = z.object({
  export_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
  project_relative_directory: z.string().regex(/^exports\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
  manifest: z.record(z.string(), z.unknown()),
}).strict();

function identifier(prefix: string): string {
  const random = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
  return `${prefix}_${random.replaceAll("-", "")}`;
}

export interface AssetDraft {
  assetId: string;
  title: string;
  kind: ConnectivityAssetKind | "";
  mediaType: string;
  source: string;
  vintage: string;
  license: string;
  intendedUse: string;
  ownership: string;
  sensitivity: AssetSensitivity | "";
  cloudDisposition: AssetCloudDisposition | "";
  generalizedAssetId: string;
  exportPermission: "" | "ALLOW" | "BLOCK";
  retentionPolicy: string;
  spatialRedaction: string;
  approvedDeployments: AssetDeployment[];
  reviewer: string;
  reviewedAt: string;
  authorizationNotes: string;
  roleConfirmed: boolean;
  rasterFormat: RasterFormat | "";
  rasterValueKind: RasterValueKind | "";
  esriAsciiCrs: string;
  esriAsciiCellAreaM2: string;
}

export function createAssetDraft(reviewer = ""): AssetDraft {
  return {
    assetId: identifier("asset"),
    title: "",
    kind: "",
    mediaType: "",
    source: "",
    vintage: "",
    license: "",
    intendedUse: "",
    ownership: "",
    sensitivity: "",
    cloudDisposition: "",
    generalizedAssetId: "",
    exportPermission: "",
    retentionPolicy: "",
    spatialRedaction: "",
    approvedDeployments: [],
    reviewer,
    reviewedAt: "",
    authorizationNotes: "",
    roleConfirmed: false,
    rasterFormat: "",
    rasterValueKind: "",
    esriAsciiCrs: "",
    esriAsciiCellAreaM2: "",
  };
}

export function assetUsesRasterGrid(kind: AssetDraft["kind"]): boolean {
  return kind === "RESISTANCE_RASTER" || kind === "FOCAL_RASTER" || kind === "ACTION_EFFECT" || kind === "REVIEW_MASK";
}

export function assetRequirements(draft: AssetDraft, selected: OpaqueFileHandle | null, inspection: RasterInspection | null = null): string[] {
  const missing: string[] = [];
  const required: Array<[string | boolean, string]> = [
    [Boolean(selected), "Selected source bytes"],
    [draft.title.trim(), "Asset title"],
    [draft.kind, "Reviewer-assigned asset role"],
    [draft.mediaType, "Media type"],
    [draft.source.trim(), "Source and attribution"],
    [draft.vintage.trim(), "Data vintage"],
    [draft.license.trim(), "License or use terms"],
    [draft.intendedUse.trim(), "Intended use"],
    [draft.ownership.trim(), "Ownership or custodian"],
    [draft.sensitivity, "Sensitivity classification"],
    [draft.cloudDisposition, "Raw/generalized cloud permission"],
    [draft.exportPermission, "Export permission"],
    [draft.retentionPolicy.trim(), "Retention policy"],
    [draft.spatialRedaction.trim(), "Spatial-redaction decision"],
    [draft.approvedDeployments.length > 0, "At least one approved deployment"],
    [draft.reviewer.trim(), "Authorization reviewer"],
    [draft.reviewedAt, "Authorization review timestamp"],
    [draft.roleConfirmed, "Reviewer role confirmation"],
  ];
  for (const [present, label] of required) if (!present) missing.push(label);
  if (draft.reviewedAt && Number.isNaN(Date.parse(draft.reviewedAt))) {
    missing.push("Valid authorization review timestamp");
  }
  if (draft.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" && !draft.generalizedAssetId.trim()) {
    missing.push("Generalized derivative asset ID");
  }
  if (draft.cloudDisposition === "PROHIBITED") {
    if (draft.approvedDeployments.length !== 1 || draft.approvedDeployments[0] !== "LOCAL_ONLY") {
      missing.push("PROHIBITED data may approve LOCAL_ONLY only");
    }
  }
  if (draft.cloudDisposition === "DEDICATED_OR_SELF_MANAGED_ONLY") {
    if (draft.approvedDeployments.includes("WANDB_CLOUD")) {
      missing.push("Dedicated-only data cannot approve W&B Cloud");
    }
    if (!draft.approvedDeployments.some((item) => item === "DEDICATED" || item === "SELF_MANAGED")) {
      missing.push("Approve Dedicated Cloud or Self-Managed for dedicated-only data");
    }
  }
  if (draft.sensitivity === "CRITICAL_INFRASTRUCTURE" && draft.approvedDeployments.includes("WANDB_CLOUD")) {
    missing.push("Critical-infrastructure data cannot approve multi-tenant W&B Cloud");
  }
  if (assetUsesRasterGrid(draft.kind)) {
    if (!draft.rasterFormat) missing.push("Raster format");
    if (!draft.rasterValueKind) missing.push("Raster value semantics");
    if (draft.rasterFormat === "ESRI_ASCII") {
      if (!draft.esriAsciiCrs.trim()) missing.push("ESRI ASCII CRS");
      const area = Number(draft.esriAsciiCellAreaM2);
      if (!Number.isFinite(area) || area <= 0) missing.push("Positive ESRI ASCII cell area");
    }
    if (!inspection) missing.push("Successful worker raster inspection");
    if (inspection && selected && (inspection.sha256 !== selected.sha256 || inspection.byte_size !== selected.byteSize)) {
      missing.push("Raster inspection matching the staged source bytes");
    }
    if (draft.kind === "FOCAL_RASTER" && draft.rasterValueKind !== "IDENTIFIER") missing.push("Focal rasters must use IDENTIFIER semantics");
    if (draft.kind === "REVIEW_MASK" && draft.rasterValueKind !== "MASK") missing.push("Review-mask rasters must use MASK semantics");
    if ((draft.kind === "RESISTANCE_RASTER" || draft.kind === "ACTION_EFFECT") && draft.rasterValueKind !== "CONTINUOUS") missing.push("Resistance and action-effect rasters must use CONTINUOUS semantics");
  }
  return [...new Set(missing)];
}

function sourceSuffix(displayName: string): string {
  const match = displayName.match(/\.([A-Za-z0-9]{1,10})$/);
  return match ? `.${match[1].toLowerCase()}` : "";
}

export function buildAssetAddParams(
  draft: AssetDraft,
  selected: OpaqueFileHandle,
  projectHandle: string,
  inspection: RasterInspection | null = null,
): Record<string, unknown> {
  const missing = assetRequirements(draft, selected, inspection);
  if (missing.length) throw new Error(`Complete or correct: ${missing.join(", ")}.`);
  const reviewedAt = new Date(draft.reviewedAt).toISOString();
  const authorization = {
    asset_id: draft.assetId,
    ownership: draft.ownership.trim(),
    license_spdx_or_name: draft.license.trim(),
    sensitivity: draft.sensitivity,
    cloud_disposition: draft.cloudDisposition,
    export_allowed: draft.exportPermission === "ALLOW",
    retention_policy: draft.retentionPolicy.trim(),
    spatial_redaction: draft.spatialRedaction.trim(),
    approved_deployments: draft.approvedDeployments,
    reviewer: draft.reviewer.trim(),
    reviewed_at: reviewedAt,
    generalized_asset_id: draft.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY"
      ? draft.generalizedAssetId.trim()
      : null,
    notes: `Intended use: ${draft.intendedUse.trim()}${draft.authorizationNotes.trim() ? `\nAuthorization notes: ${draft.authorizationNotes.trim()}` : ""}`,
  };
  const parsedAuthorization = SensitiveDataAuthorizationSchema.parse(authorization);
  return {
    project_handle: projectHandle,
    source_handle: selected.handle,
    asset: {
      asset_id: draft.assetId,
      kind: draft.kind,
      title: draft.title.trim(),
      relative_path: `assets/${draft.assetId}/source${sourceSuffix(selected.displayName)}`,
      sha256: selected.sha256,
      byte_size: selected.byteSize,
      media_type: draft.mediaType,
      source: draft.source.trim(),
      vintage: draft.vintage.trim(),
      license: draft.license.trim(),
      grid: inspection?.grid ?? null,
      authorization: parsedAuthorization,
    },
  };
}

const rasterGridSchema = z.object({
  grid_id: z.string().min(1),
  crs: z.string().min(1),
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  transform: z.tuple([z.number().finite(), z.number().finite(), z.number().finite(), z.number().finite(), z.number().finite(), z.number().finite()]),
  nodata: z.number().finite().nullable(),
  value_kind: z.enum(["CONTINUOUS", "CATEGORICAL", "IDENTIFIER", "MASK"]),
  cell_area_m2: z.number().finite().positive(),
}).strict();

export const DigitizationDraftSchema = z.object({
  draft_id: z.string().min(1),
  kind: z.enum(["FOCAL_AREAS", "INTERVENTION_FOOTPRINT", "RISK_REVIEW_MASK"]),
  reference_asset_id: z.string().min(1),
  cells: z.array(z.object({ feature_id: z.string().min(1), row: z.number().int().nonnegative(), column: z.number().int().nonnegative(), value: z.number().int() }).strict()),
  polygons: z.array(z.object({ feature_id: z.string().min(1), value: z.number().int(), vertices: z.array(z.object({ x: z.number().finite(), y: z.number().finite() }).strict()).min(3) }).strict()),
}).strict();

export const DigitizationPreviewSchema = z.object({
  draft_id: z.string().min(1),
  kind: z.enum(["FOCAL_AREAS", "INTERVENTION_FOOTPRINT", "RISK_REVIEW_MASK"]),
  reference_asset_id: z.string().min(1),
  reference_asset_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  grid: rasterGridSchema,
  grid_identity_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  value_kind: z.enum(["CONTINUOUS", "CATEGORICAL", "IDENTIFIER", "MASK"]),
  affected_cells: z.array(z.object({ row: z.number().int().nonnegative(), column: z.number().int().nonnegative(), value: z.number().int(), feature_id: z.string().min(1), source: z.enum(["CELL", "POLYGON"]) }).strict()).min(1),
  affected_cell_count: z.number().int().positive(),
  affected_area_m2: z.number().positive(),
  value_counts: z.record(z.string(), z.number().int().nonnegative()),
  explicit_cell_count: z.number().int().nonnegative(),
  polygon_count: z.number().int().nonnegative(),
  background_cell_count: z.number().int().nonnegative(),
  nodata_cell_count: z.number().int().nonnegative(),
  nodata_topology_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  materialized_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  estimated_materialized_bytes: z.number().int().positive(),
  preview_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  notices: z.array(z.string()),
}).strict();

export const DigitizationPreviewResponseSchema = z.object({ preview: DigitizationPreviewSchema }).strict();

export const DigitizationPublicationResponseSchema = z.object({
  preview: DigitizationPreviewSchema,
  asset: ConnectivityAssetSchema,
  provenance: z.object({
    provenance_id: z.string().min(1),
    provenance_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    draft: DigitizationDraftSchema,
    preview_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    reference_asset_id: z.string().min(1),
    reference_asset_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    nodata_topology_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    materialized_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    affected_cell_count: z.number().int().positive(),
    value_counts: z.record(z.string(), z.number().int().nonnegative()),
    ecological_basis: z.string().min(1),
    reviewer: z.string().min(1),
    reviewed_at: z.string().min(1),
    authorization_asset_id: z.string().min(1),
    authorization_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  }).strict(),
  semantic_diff: SemanticModelDiffSchema,
  revision: ModelRevisionSchema,
}).strict();

export const RasterInspectionSchema = z.object({
  format: z.enum(["GEOTIFF", "ESRI_ASCII"]),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  byte_size: z.number().int().nonnegative(),
  grid: rasterGridSchema,
  data_cell_count: z.number().int().nonnegative(),
  nodata_cell_count: z.number().int().nonnegative(),
  minimum: z.number().finite(),
  maximum: z.number().finite(),
  unique_value_count: z.number().int().nonnegative(),
}).strict();

export const GridPreviewResponseSchema = z.object({
  plan: GridAlignmentPlanSchema,
}).strict();

export const RecordSaveResponseSchema = z.object({
  record_id: z.string().min(1),
  revision: z.number().int().positive(),
}).strict();

export const CostBasisGetResponseSchema = z.object({
  cost_basis: ConnectivityCostBasisSchema.nullable(),
  active_revision_id: z.string().min(1),
  confirmed: z.boolean(),
  missing_requirements: z.array(z.string().min(1)),
}).strict();

export const CostBasisSaveResponseSchema = RecordSaveResponseSchema.extend({
  cost_basis: ConnectivityCostBasisSchema,
  active_revision_id: z.string().min(1),
  cost_basis_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  confirmed: z.literal(true),
}).strict();

export const ModelAttestResponseSchema = z.object({
  passport_id: z.string().min(1),
  fitness_revision: z.number().int().positive(),
}).strict();

export const InterventionSaveResponseSchema = z.object({
  site_id: z.string().min(1),
  option_ids: z.array(z.string().min(1)).min(1),
}).strict();

const InterventionEffectHashSchema = z.object({
  option_id: z.string().min(1),
  effect_id: z.string().min(1),
  scenario_id: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();

export const InterventionPreviewResponseSchema = z.object({
  preview_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  revision_id: z.string().min(1),
  site: InterventionSiteSchema,
  options: z.array(InterventionOptionSchema).min(1),
  effect_hashes: z.array(InterventionEffectHashSchema),
  findings: z.array(ModelFindingSchema),
  can_save: z.boolean(),
  persisted: z.literal(false),
  semantic_diff: z.object({
    site_id: z.string().min(1),
    previous_option_ids: z.array(z.string()),
    proposed_option_ids: z.array(z.string()),
    added_option_ids: z.array(z.string()),
    removed_option_ids: z.array(z.string()),
    effect_hashes: z.array(InterventionEffectHashSchema),
  }).strict(),
}).strict();

export const ProviderDownloadResponseSchema = z.object({
  asset: z.object({
    asset_id: z.string().min(1),
    kind: z.literal("VECTOR_CONTEXT"),
    title: z.string().min(1),
    relative_path: z.string().min(1),
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    byte_size: z.number().int().positive(),
    media_type: z.string().min(1),
    source: z.string().url(),
    vintage: z.string().min(1),
    license: z.string().min(1),
    grid: z.null().optional(),
    authorization: SensitiveDataAuthorizationSchema,
  }).strict(),
  context_only: z.literal(true),
}).strict();

export const GridApprovalResponseSchema = z.object({
  approval: GridAlignmentApprovalSchema,
  derived_asset: z.object({
    asset_id: z.string().min(1),
    kind: z.enum(["RESISTANCE_RASTER", "FOCAL_RASTER", "ACTION_EFFECT", "REVIEW_MASK", "VECTOR_CONTEXT", "DOCUMENT", "REFERENCE_MATRIX", "REFERENCE_CURRENT"]),
    title: z.string().min(1),
    relative_path: z.string().min(1),
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    byte_size: z.number().int().nonnegative(),
    media_type: z.string().min(1),
    source: z.string().min(1),
    vintage: z.string().min(1),
    license: z.string().min(1),
    grid: rasterGridSchema.nullable().optional(),
    authorization: SensitiveDataAuthorizationSchema,
  }).strict(),
  semantic_diff: SemanticModelDiffSchema,
  revision: ModelRevisionSchema,
  original_asset_retained: z.string().min(1),
}).strict();

export function buildAssetInspectParams(draft: AssetDraft, selected: OpaqueFileHandle, projectHandle: string): Record<string, unknown> {
  if (!assetUsesRasterGrid(draft.kind) || !draft.rasterFormat || !draft.rasterValueKind) {
    throw new Error("Choose the reviewer-assigned raster role, file format, and value semantics first.");
  }
  if (draft.rasterFormat === "ESRI_ASCII") {
    const area = Number(draft.esriAsciiCellAreaM2);
    if (!draft.esriAsciiCrs.trim() || !Number.isFinite(area) || area <= 0) {
      throw new Error("ESRI ASCII inspection requires an explicit CRS and positive cell area.");
    }
  }
  return {
    project_handle: projectHandle,
    source_handle: selected.handle,
    grid_id: `grid_${draft.assetId}`,
    value_kind: draft.rasterValueKind,
    format: draft.rasterFormat,
    esri_ascii_crs: draft.rasterFormat === "ESRI_ASCII" ? draft.esriAsciiCrs.trim() : null,
    esri_ascii_cell_area_m2: draft.rasterFormat === "ESRI_ASCII" ? Number(draft.esriAsciiCellAreaM2) : null,
  };
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function planningRequirements(state: PlanningState): string[] {
  const requirements: Array<[boolean, string]> = [
    [Boolean(state.projectName.trim()), "Project name"],
    [Boolean(state.studyArea.trim()), "Study area"],
    [Boolean(state.decisionQuestion.trim()), "Decision question"],
    [Boolean(state.intendedUse.trim()), "Intended use and scope boundary"],
    [Boolean(state.targetGuild.trim()), "Target guild or life stage"],
    [Boolean(state.reviewer.trim()), "Responsible ecological reviewer"],
  ];
  return requirements.filter(([present]) => !present).map(([, label]) => label);
}

export async function buildPlanningCreateParams(state: PlanningState, projectHandle: string): Promise<Record<string, unknown>> {
  const missing = planningRequirements(state);
  if (missing.length) throw new Error(`Complete these fields first: ${missing.join(", ")}.`);
  const projectId = identifier("project");
  const revisionId = identifier("revision");
  const now = new Date().toISOString();
  const modelSha256 = await sha256(JSON.stringify({
    projectName: state.projectName.trim(),
    studyArea: state.studyArea.trim(),
    decisionQuestion: state.decisionQuestion.trim(),
    intendedUse: state.intendedUse.trim(),
    targetGuild: state.targetGuild.trim(),
    reviewer: state.reviewer.trim(),
  }));
  return {
    project_handle: projectHandle,
    project_id: projectId,
    title: state.projectName.trim(),
    revision: {
      revision_id: revisionId,
      project_id: projectId,
      created_at: now,
      created_by: state.reviewer.trim(),
      reason: "Create an explicit conceptual connectivity planning record.",
      model_sha256: modelSha256,
    },
    decision_brief: {
      brief_id: identifier("brief"),
      title: state.projectName.trim(),
      decision_question: state.decisionQuestion.trim(),
      decision_owner: state.reviewer.trim(),
      intended_use: `${state.intendedUse.trim()} Study context: ${state.studyArea.trim()}.`,
      target_guilds: [state.targetGuild.trim()],
      safeguard_guilds: [],
      budget_amount: null,
      currency: null,
      cost_vintage: null,
      non_goals: [
        "Observed animal movement or crossing use",
        "Collision reduction or population benefit",
        "Regulatory approval, corridor designation, or crossing design",
      ],
      required_next_action: "Classify and authorize source assets, then obtain ecological review for resistance, focal-area, scenario, and intervention records.",
      conceptual: true,
    },
    fitness: {
      passport_id: identifier("fitness"),
      revision_id: revisionId,
      intended_use: state.intendedUse.trim(),
      dimensions: FITNESS_KEYS.map((key) => ({
        key,
        status: key === "intended_use" ? "REVIEW_REQUIRED" : "BLOCKING",
        evidence_ids: [],
        reviewer: null,
        notes: key === "intended_use"
          ? "The decision scope is recorded but has not been attested."
          : `Complete and review the ${key.replaceAll("_", " ")} evidence before analysis.`,
      })),
      unresolved_assumptions: ["Planning assembly is incomplete."],
      attestations: [],
      reviewed_by: null,
      reviewed_at: null,
    },
    resource_policy: {
      max_input_bytes: 2_147_483_648,
      max_materialized_bytes: 8_589_934_592,
      max_current_raster_bytes: 4_294_967_296,
      max_direct_jobs: 5_000,
      max_wall_seconds_per_job: 3_600,
      require_explicit_oversize_approval: true,
    },
    scenarios: [],
    safeguards: [],
    review_masks: [],
    decision_rules: [],
  };
}
