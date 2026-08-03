export type ProjectKind = "existing" | "conceptual" | "demo";
export type Readiness = "DEMO" | "CONCEPTUAL" | "ENGINEER_REVIEWED" | "REFERENCE_MATCHED";
export type StepId = "model" | "storms" | "opportunities" | "goals" | "run" | "compare" | "export";
export type EvidenceKind = "run" | "model" | "storm" | "assumption" | "qa" | "alternative";

export interface EvidenceItem {
  id: string;
  kind: EvidenceKind;
  eyebrow: string;
  title: string;
  subtitle: string;
  status: "exact" | "review" | "warning" | "source";
  fields: Array<{ label: string; value: string; mono?: boolean }>;
  changes?: string[];
  artifactsAvailable?: boolean;
}

export interface ProjectSummary {
  id: string;
  name: string;
  kind: ProjectKind;
  readiness: Readiness;
  location: string;
  modelFile: string;
  updatedAt: string;
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
}

export interface RpcTransport {
  mode: "tauri" | "browser-demo";
  request<T>(method: string, params?: Record<string, unknown>): Promise<T>;
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
