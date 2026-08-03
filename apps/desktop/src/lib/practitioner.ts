import type { EvidenceItem, ImportedModelSummary } from "../types";

export interface ModelValidationIssue {
  code: string;
  severity: "info" | "warning" | "blocking";
  message: string;
  object_type?: string | null;
  object_id?: string | null;
  section?: string | null;
  remediation?: string | null;
}

export interface ModelValidationCheck {
  key: string;
  label: string;
  status: string;
  detail: string;
}

export interface ModelValidationResult {
  issues: ModelValidationIssue[];
  checks: ModelValidationCheck[];
  section_names: string[];
  object_counts: Record<string, number>;
  blocking_count: number;
  warning_count: number;
  structurally_runnable: boolean;
  setup_options?: {
    source: "parsed_model";
    subcatchment_ids: string[];
    lid_control_ids: string[];
    existing_lid_usage: Array<{
      subcatchment_id: string;
      lid_control: string;
      unit_count: number | null;
      raw_unit_count: string;
    }>;
  };
}

export interface PractitionerStormCase {
  stormId: string;
  name: string;
  modelPath: string;
  displayPath: string;
  dependencies: Record<string, string>;
  included: boolean;
  /** Only the worker-selected authoritative base can enter analysis in the MVP. */
  stormEligible: boolean;
  modelRole: "authoritative_base" | "unclassified_companion";
  authorityStatus: "adopted" | "preliminary" | "sensitivity" | "user_defined";
  source: string;
  validation?: ModelValidationResult;
}

export type LidType = "BC" | "RG" | "GR" | "IT" | "PP" | "RB" | "RD" | "VS";

/** String fields keep unentered engineering quantities distinct from zero. */
export interface CandidateSlotDraft {
  slotId: string;
  subcatchmentId: string;
  lidControl: string;
  lidType: LidType | "";
  baseUnits: string;
  minimumAdditionalUnits: string;
  maximumAdditionalUnits: string;
  unitArea: string;
  surfaceWidth: string;
  initialSaturationPercent: string;
  fromImperviousPercent: string;
  drainTo: string;
  displacedSurfaceArea: string;
  fromPervious: boolean;
  routeToPervious: boolean;
  feasibilityBasis: string;
  engineerApproved: boolean;
}

export interface CandidateSlotPayload {
  slot_id: string;
  subcatchment_id: string;
  lid_control: string;
  lid_type: LidType | null;
  base_units: number;
  min_additional_units: number;
  max_additional_units: number;
  unit_area: number;
  surface_width: number;
  initial_saturation_percent: number;
  from_impervious_percent: number;
  from_pervious: boolean;
  route_to_pervious: boolean;
  report_file: null;
  drain_to: string | null;
  displaced_surface_area: number;
  feasibility_basis: string;
  engineer_approved: true;
}

export interface AnalysisRunPlan {
  plan_id: string;
  project_id: string;
  analysis_id: string;
  model_revision_id: string;
  model_sha256: string;
  baseline_jobs: number;
  candidate_designs: number;
  storm_count: number;
  total_exact_jobs: number;
  cache_hits: number;
  worker_count: number;
  search_method: string;
  search_reason: string;
  seed: number;
  estimated_storage_bytes?: number | null;
  reportable_outputs: string[];
}

export interface AnalysisPlanResponse {
  analysis_id: string;
  state: string;
  exact: true;
  design_count: number;
  alternative_count: number;
  storm_count: number;
  planned_solver_jobs: number;
  job_ids: string[];
  run_plan: AnalysisRunPlan;
}

export interface ExactJobRow {
  job_id: string;
  analysis_id: string;
  design_id: string;
  storm_id: string;
  state: string;
  metric_id: string | null;
  object_id: string | null;
  value: number | null;
  units: string | null;
}

export interface ExactJobSummary {
  knownJobs: number;
  totalJobs: number;
  completedJobs: number;
  succeededJobs: number;
  rejectedJobs: number;
  failedJobs: number;
  cancelledJobs: number;
}

export interface DecisionMetricRecord {
  metric_id: string;
  object_id: string;
  value: number;
  units: string;
}

export interface DecisionMetricSummary {
  metric_id: string;
  object_id: string;
  units: string;
  aggregation: string;
  value: number | null;
  source_storms: string[];
}

export interface AdversePeakChange {
  storm_id: string;
  metric_id: string;
  object_id: string;
  baseline_value: number;
  alternative_value: number;
  delta: number;
  units: string;
  baseline_job_id: string;
  alternative_job_id: string;
}

export interface DecisionDesign {
  design_id: string;
  vector: number[];
  source: string;
  sequence: number;
  job_ids: Record<string, string>;
  storm_metrics: Record<string, DecisionMetricRecord[]>;
  metric_summaries: DecisionMetricSummary[];
  adverse_peak_changes?: AdversePeakChange[];
}

export interface DecisionBoard {
  analysis_id: string;
  project_id: string;
  exact_only: true;
  storm_ids: string[];
  baseline: DecisionDesign | null;
  alternatives: DecisionDesign[];
  excluded_designs: Array<{
    design_id: string;
    reason: string;
    job_states: Record<string, string>;
  }>;
  blockers: string[];
  model_fitness_passport: Record<string, unknown> | null;
  decision_brief: Record<string, unknown> | null;
  claim: string;
}

export interface ResolvedExactRun {
  run_id: string;
  analysis_id: string;
  model_revision_id: string;
  design_id: string;
  storm_id: string;
  state: "SUCCEEDED";
  exact: true;
  metrics: Record<string, number>;
  metric_records: DecisionMetricRecord[];
  qa_status: "accepted" | "warning";
  qa_findings: string[];
  qa_records: Array<Record<string, unknown>>;
  artifacts: Array<{ role: string; sha256: string; relative_path: string; bytes: number }>;
  input_sha256: string;
  report_sha256: string;
  solver_sha256: string;
  output_sha256: string;
  extractor_version: string;
  hashes: { input: string; solver: string; output: string };
  physics_cache_key: string;
  cache_hit: boolean;
  completed_at: string;
}

export interface ExportEligibilityResult {
  eligibility_id: string;
  allowed: boolean;
  blockers: string[];
  warnings: string[];
  requested_package: "learning" | "screening" | "verified_alternatives";
  fitness_disposition: string;
  required_evidence_ids: string[];
}

export interface EvidenceExportResult {
  output_directory: string;
  manifest: { export_id: string; package_type: string; files: Record<string, string> };
  files: Record<string, string>;
  cloud_sync?: { state: string; outbox_id?: string; envelope_id?: string };
}

export const emptyCandidateSlot = (index: number): CandidateSlotDraft => ({
  slotId: `SLOT-${String(index + 1).padStart(2, "0")}`,
  subcatchmentId: "",
  lidControl: "",
  lidType: "",
  baseUnits: "",
  minimumAdditionalUnits: "0",
  maximumAdditionalUnits: "",
  unitArea: "",
  surfaceWidth: "",
  initialSaturationPercent: "",
  fromImperviousPercent: "",
  drainTo: "",
  displacedSurfaceArea: "",
  fromPervious: false,
  routeToPervious: false,
  feasibilityBasis: "",
  engineerApproved: false,
});

const safeInteger = (value: string, label: string, minimum: number, maximum?: number) => {
  if (!/^\d+$/.test(value.trim())) throw new Error(`${label} must be a whole number.`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || (maximum !== undefined && parsed > maximum)) {
    throw new Error(`${label} is outside its permitted range.`);
  }
  return parsed;
};

const safeNumber = (value: string, label: string, minimum: number, maximum?: number) => {
  if (!value.trim()) throw new Error(`${label} is required.`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum || (maximum !== undefined && parsed > maximum)) {
    throw new Error(`${label} is outside its permitted range.`);
  }
  return parsed;
};

export function candidateSlotPayload(draft: CandidateSlotDraft): CandidateSlotPayload {
  if (!draft.slotId.trim()) throw new Error("Candidate slot ID is required.");
  if (!draft.subcatchmentId.trim()) throw new Error(`${draft.slotId}: subcatchment ID is required.`);
  if (!draft.lidControl.trim()) throw new Error(`${draft.slotId}: an existing LID control ID is required.`);
  if (!draft.feasibilityBasis.trim()) throw new Error(`${draft.slotId}: record the feasibility basis.`);
  if (!draft.engineerApproved) throw new Error(`${draft.slotId}: engineer approval has not been confirmed.`);
  const baseUnits = safeInteger(draft.baseUnits, `${draft.slotId} baseline units`, 0);
  const minimum = safeInteger(draft.minimumAdditionalUnits, `${draft.slotId} minimum units`, 0);
  const maximum = safeInteger(draft.maximumAdditionalUnits, `${draft.slotId} maximum units`, 0);
  if (minimum !== 0) throw new Error(`${draft.slotId}: minimum additional units must be 0 so the untouched baseline remains in range.`);
  if (maximum < minimum) throw new Error(`${draft.slotId}: maximum units must be at least the minimum.`);
  return {
    slot_id: draft.slotId.trim(),
    subcatchment_id: draft.subcatchmentId.trim(),
    lid_control: draft.lidControl.trim(),
    lid_type: draft.lidType || null,
    base_units: baseUnits,
    min_additional_units: minimum,
    max_additional_units: maximum,
    unit_area: safeNumber(draft.unitArea, `${draft.slotId} unit area`, Number.MIN_VALUE),
    surface_width: safeNumber(draft.surfaceWidth, `${draft.slotId} surface width`, Number.MIN_VALUE),
    initial_saturation_percent: safeNumber(draft.initialSaturationPercent, `${draft.slotId} initial saturation`, 0, 100),
    from_impervious_percent: safeNumber(draft.fromImperviousPercent, `${draft.slotId} impervious routing`, 0, 100),
    from_pervious: draft.fromPervious,
    route_to_pervious: draft.routeToPervious,
    report_file: null,
    drain_to: draft.drainTo.trim() || null,
    displaced_surface_area: safeNumber(draft.displacedSurfaceArea, `${draft.slotId} displaced surface`, 0),
    feasibility_basis: draft.feasibilityBasis.trim(),
    engineer_approved: true,
  };
}

export function candidateSlotErrors(drafts: CandidateSlotDraft[]): string[] {
  if (!drafts.length) return ["Add at least one engineer-approved candidate slot."];
  const errors: string[] = [];
  const identifiers = new Set<string>();
  for (const draft of drafts) {
    try {
      const slot = candidateSlotPayload(draft);
      const key = slot.slot_id.toLocaleLowerCase();
      if (identifiers.has(key)) errors.push(`Candidate slot ID ${slot.slot_id} is duplicated.`);
      identifiers.add(key);
    } catch (reason) {
      errors.push(reason instanceof Error ? reason.message : "Candidate slot is invalid.");
    }
  }
  return errors;
}

export function designVectorErrors(vectors: number[][], slots: CandidateSlotPayload[]): string[] {
  if (!vectors.length) return ["Add at least one non-baseline exact alternative."];
  const errors: string[] = [];
  const seen = new Set<string>();
  vectors.forEach((vector, vectorIndex) => {
    if (vector.length !== slots.length) {
      errors.push(`Alternative ${vectorIndex + 1} does not match the candidate-slot count.`);
      return;
    }
    vector.forEach((value, slotIndex) => {
      const slot = slots[slotIndex];
      if (!Number.isInteger(value) || value < slot.min_additional_units || value > slot.max_additional_units) {
        errors.push(`Alternative ${vectorIndex + 1}: ${slot.slot_id} must be ${slot.min_additional_units}–${slot.max_additional_units}.`);
      }
    });
    if (vector.every((value) => value === 0)) errors.push(`Alternative ${vectorIndex + 1} duplicates the automatic baseline.`);
    const key = vector.join(",");
    if (seen.has(key)) errors.push(`Alternative ${vectorIndex + 1} duplicates another exact vector.`);
    seen.add(key);
  });
  return errors;
}

export function exactJobArithmetic(stormCount: number, vectors: number[][]) {
  const uniqueAlternatives = new Set(
    vectors.filter((vector) => vector.some((value) => value !== 0)).map((vector) => vector.join(",")),
  ).size;
  return {
    baselineJobs: stormCount,
    alternativeDesigns: uniqueAlternatives,
    alternativeJobs: uniqueAlternatives * stormCount,
    totalJobs: stormCount + uniqueAlternatives * stormCount,
  };
}

const normalizeArchiveReference = (modelPath: string, reference: string) => {
  const normalizedReference = reference.replaceAll("\\", "/");
  if (normalizedReference.startsWith("/") || normalizedReference.split("/").includes("..")) return null;
  const parent = modelPath.split("/").slice(0, -1);
  const parts = [...parent, ...normalizedReference.split("/")].filter((part) => part && part !== ".");
  return { target: normalizedReference.split("/").filter((part) => part && part !== ".").join("/"), archivePath: parts.join("/") };
};

export function importedModelSummaries(
  models: Array<{ model_path: string; references: string[]; resolved: string[]; missing: string[] }>,
): ImportedModelSummary[] {
  return models.map((model) => {
    const resolved = new Map(model.resolved.map((path) => [path.toLocaleLowerCase(), path]));
    const dependencies: Record<string, string> = {};
    for (const reference of model.references) {
      const candidate = normalizeArchiveReference(model.model_path, reference);
      if (!candidate) continue;
      const actual = resolved.get(candidate.archivePath.toLocaleLowerCase());
      if (actual) dependencies[candidate.target] = `sources/imported/${actual}`;
    }
    return {
      modelPath: model.model_path,
      projectPath: `sources/imported/${model.model_path}`,
      references: [...model.references],
      resolved: [...model.resolved],
      dependencies,
    };
  });
}

const safeStormId = (path: string, index: number) => {
  const filename = path.split("/").at(-1)?.replace(/\.inp$/i, "") ?? `storm-${index + 1}`;
  const normalized = filename.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 110);
  return normalized || `storm-${index + 1}`;
};

export function initialStormCases(
  models: ImportedModelSummary[],
  authoritativeModelPath?: string,
): PractitionerStormCase[] {
  const used = new Set<string>();
  return models.map((model, index) => {
    const base = safeStormId(model.modelPath, index);
    let stormId = base;
    let suffix = 2;
    while (used.has(stormId.toLocaleLowerCase())) stormId = `${base}-${suffix++}`;
    used.add(stormId.toLocaleLowerCase());
    const authoritative = model.projectPath === authoritativeModelPath;
    return {
      stormId,
      name: model.modelPath.split("/").at(-1) ?? stormId,
      modelPath: model.projectPath,
      displayPath: model.modelPath,
      dependencies: { ...model.dependencies },
      included: authoritative,
      stormEligible: authoritative,
      modelRole: authoritative ? "authoritative_base" : "unclassified_companion",
      authorityStatus: "user_defined",
      source: authoritative
        ? "Worker-selected authoritative base revision"
        : "Imported companion INP; semantic role not established",
    };
  });
}

export function aggregateValidation(storms: PractitionerStormCase[]): ModelValidationResult | null {
  const selected = storms.filter((storm) => storm.included);
  if (!selected.length || selected.some((storm) => !storm.validation)) return null;
  const reports = selected.map((storm) => storm.validation as ModelValidationResult);
  return {
    issues: reports.flatMap((report) => report.issues),
    checks: reports.flatMap((report) => report.checks),
    section_names: [...new Set(reports.flatMap((report) => report.section_names))],
    object_counts: reports.reduce<Record<string, number>>((counts, report) => {
      for (const [key, value] of Object.entries(report.object_counts)) counts[key] = (counts[key] ?? 0) + value;
      return counts;
    }, {}),
    blocking_count: reports.reduce((total, report) => total + report.blocking_count, 0),
    warning_count: reports.reduce((total, report) => total + report.warning_count, 0),
    structurally_runnable: reports.every((report) => report.structurally_runnable),
  };
}

const terminalJobStates = new Set(["SUCCEEDED", "QA_REJECTED", "FAILED", "CANCELLED"]);

export function summarizeExactJobs(rows: ExactJobRow[], expectedJobs: number): ExactJobSummary {
  const byJob = new Map(rows.map((row) => [row.job_id, row.state]));
  const states = [...byJob.values()];
  const count = (state: string) => states.filter((item) => item === state).length;
  return {
    knownJobs: byJob.size,
    totalJobs: expectedJobs,
    completedJobs: states.filter((state) => terminalJobStates.has(state)).length,
    succeededJobs: count("SUCCEEDED"),
    rejectedJobs: count("QA_REJECTED"),
    failedJobs: count("FAILED"),
    cancelledJobs: count("CANCELLED"),
  };
}

export function resolvedRunEvidence(run: ResolvedExactRun): EvidenceItem {
  const metrics = run.metric_records.map((record) => ({
    label: `${record.metric_id}${record.object_id ? ` · ${record.object_id}` : ""}`,
    value: `${record.value.toLocaleString(undefined, { maximumFractionDigits: 6 })} ${record.units}`,
    mono: true,
  }));
  return {
    id: run.run_id,
    kind: "run",
    eyebrow: "Repository-verified exact run",
    title: `${run.design_id} × ${run.storm_id}`,
    subtitle: "Rehashed against the authoritative local artifact ledger before display",
    status: "exact",
    claim: "Exact SWMM result under documented assumptions.",
    fields: [
      { label: "Run ID", value: run.run_id, mono: true },
      { label: "Model revision", value: run.model_revision_id, mono: true },
      { label: "Metric extractor", value: run.extractor_version, mono: true },
      { label: "QA", value: run.qa_status.toUpperCase(), mono: true },
      { label: "Cache", value: run.cache_hit ? "Reused immutable physics result" : "Direct execution" },
      ...metrics,
      { label: "Input SHA-256", value: run.input_sha256, mono: true },
      { label: "Report SHA-256", value: run.report_sha256, mono: true },
      { label: "Solver SHA-256", value: run.solver_sha256, mono: true },
      { label: "Output SHA-256", value: run.output_sha256, mono: true },
    ],
    artifactsAvailable: run.artifacts.length > 0,
    runIds: [run.run_id],
    artifacts: run.artifacts.map((artifact) => ({
      label: artifact.role.toUpperCase(),
      path: artifact.relative_path,
      available: true,
    })),
  };
}

export function aggregateMetric(design: DecisionDesign, contains: string): DecisionMetricSummary | undefined {
  const normalized = contains.toLocaleLowerCase();
  return design.metric_summaries.find(
    (metric) => !metric.object_id && metric.value !== null && metric.metric_id.toLocaleLowerCase().includes(normalized),
  );
}
