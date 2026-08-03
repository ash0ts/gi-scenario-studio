export type ProjectKind = "existing" | "conceptual" | "demo";
export type FitnessDisposition = "not_assessed" | "fit_for_stated_use" | "fit_with_limitations" | "not_fit";
export type StepId = "overview" | "model" | "storms" | "opportunities" | "goals" | "run" | "compare" | "export";
export type EvidenceKind = "run" | "model" | "storm" | "assumption" | "qa" | "alternative";

export type ObservabilityMode = "local" | "demo" | "connected" | "error";
export type CloudConnectionState = "CONNECTED" | "SYNCING" | "SYNC_QUEUED_OFFLINE" | "FAILED_RETRYING" | "AUTHENTICATION_REQUIRED" | "BLOCKED_PROJECT_POLICY";

export interface ObservabilityState {
  schemaVersion?: "2.0";
  scope?: "account" | "project";
  state?: CloudConnectionState;
  mode: ObservabilityMode;
  configured: boolean;
  provider: "none" | "wandb-weave";
  entity?: string;
  project?: string;
  weaveProject?: string;
  wandbUrl?: string;
  weaveUrl?: string;
  lastChecked?: string;
  disclosureAccepted: boolean;
  message?: string;
  deployment?: "wandb_cloud" | "dedicated_cloud" | "self_managed";
  apiBaseUrl?: string;
  webBaseUrl?: string;
  sessionId?: string;
  externalProjectId?: string;
  includedCategories?: string[];
  excludedCategories?: string[];
}

export interface EvidenceArtifact {
  label: string;
  path: string;
  available: boolean;
}

export interface EvidenceRunRecord {
  storm: string;
  runId: string;
  qa: string;
  volume: string;
  peak: string;
  baselinePeak: string;
  peakDelta: string;
  inputHash: string;
  outputHash: string;
  reportHash: string;
}

export interface EvidenceItem {
  id: string;
  kind: EvidenceKind;
  eyebrow: string;
  title: string;
  subtitle: string;
  status: "exact" | "review" | "warning" | "source";
  claim?: string;
  fields: Array<{ label: string; value: string; mono?: boolean }>;
  changes?: string[];
  artifactsAvailable?: boolean;
  runIds?: string[];
  runRecords?: EvidenceRunRecord[];
  artifacts?: EvidenceArtifact[];
}

export interface ProjectSummary {
  id: string;
  name: string;
  kind: ProjectKind;
  fitnessDisposition: FitnessDisposition;
  location: string;
  /** Worker-owned repository path for RPC routing; never shown or uploaded as evidence. */
  projectDirectory?: string;
  modelFile: string;
  updatedAt: string;
  bundle?: {
    displayName: string;
    sha256: string;
    fileCount: number;
    modelCount: number;
    dependencyCount: number;
    dependencyComplete: boolean;
    baseModel?: string;
    blockingCount?: number;
    warningCount?: number;
    structurallyRunnable?: boolean;
    /** Project-contained models discovered by the secure bundle importer. */
    models: ImportedModelSummary[];
    currentModelRevisionId?: string;
    modelFitnessPassportId?: string;
  };
}

export interface ImportedModelSummary {
  /** Archive-relative display path. */
  modelPath: string;
  /** Worker repository-relative path used by exact-analysis RPCs. */
  projectPath: string;
  references: string[];
  resolved: string[];
  /** Dependency target expected by the INP -> project-contained source path. */
  dependencies: Record<string, string>;
}

export interface StagedSwmmBundle {
  stagedPath: string;
  displayName: string;
  sha256: string;
  bytes: number;
}

export interface BundleInspection {
  valid: boolean;
  archive_path: string;
  archive_sha256: string;
  archive_bytes: number;
  files: Array<{ path: string; size: number; compressed_size: number; sha256: string; role: "swmm_model" | "dependency" }>;
  models: Array<{ model_path: string; references: string[]; resolved: string[]; missing: string[] }>;
  total_uncompressed_bytes: number;
  warnings: string[];
  model_validation?: {
    blocking_count: number;
    warning_count: number;
    structurally_runnable: boolean;
    issues?: Array<{
      code: string;
      severity: "info" | "warning" | "blocking";
      message: string;
      object_type?: string | null;
      object_id?: string | null;
      section?: string | null;
      remediation?: string | null;
    }>;
  };
  project_state?: {
    selected_base_model?: string;
    selected_model_dependencies?: Record<string, string>;
    current_model_revision_id?: string;
    model_fitness_passport_id?: string;
    base_models?: string[];
    base_model_selection_required?: boolean;
  };
  cloud_sync?: { outbox_id?: string; envelope_id?: string; state: string };
}

export interface Alternative {
  id: string;
  designId: string;
  sequence: number;
  name: string;
  cost: number;
  volume: number;
  peak: number;
  reduction: number;
  status: "meets" | "partial" | "fails";
  color: string;
  exactRuns: number;
  interventions: string;
  stormVolumes: number[];
  retainedArtifacts: boolean;
  nondominated: boolean;
  vector: number[];
  volumeTargetPass: boolean;
  costCapPass: boolean;
  peakGuardrailPass: boolean;
  qaPass: boolean;
  meetsAllRules: boolean;
  worstPeakDelta: number;
  worstPeakStormId: string;
  worstPeakBaseline: number;
  worstPeakAlternative: number;
}

export interface RpcTransport {
  mode: "tauri" | "browser-demo";
  request<T>(method: string, params?: Record<string, unknown>, options?: { timeoutMs?: number }): Promise<T>;
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
    __GI_SCENARIO_RPC__?: <T>(request: {
      jsonrpc: "2.0";
      id: string;
      method: string;
      params?: Record<string, unknown>;
    }) => Promise<{ result?: T; error?: { code: number; message: string } }>;
  }
}
