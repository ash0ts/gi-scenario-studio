import type { EntryPath, WorkflowStep } from "./types";

const shared: WorkflowStep[] = [
  { id: "overview", label: "Overview", shortLabel: "Overview", phase: "review" },
  { id: "model-fitness", label: "Model fitness", shortLabel: "Fitness", phase: "review" },
  { id: "scenarios", label: "Ecological scenarios", shortLabel: "Scenarios", phase: "review" },
  { id: "interventions", label: "Reviewed interventions", shortLabel: "Options", phase: "review" },
  { id: "safeguards", label: "Safeguards", shortLabel: "Safeguards", phase: "review" },
  { id: "rules-costs", label: "Rules and costs", shortLabel: "Rules", phase: "compare" },
  { id: "run-plan", label: "Run plan", shortLabel: "Plan", phase: "compare" },
  { id: "direct-runs", label: "Direct runs", shortLabel: "Runs", phase: "compare" },
  { id: "robustness", label: "Robustness board", shortLabel: "Compare", phase: "compare" },
  { id: "evidence-package", label: "Evidence package", shortLabel: "Evidence", phase: "share" },
];

const planningAssembly: WorkflowStep[] = [
  { id: "overview", label: "Planning overview", shortLabel: "Overview", phase: "assemble" },
  { id: "study-context", label: "Study context", shortLabel: "Context", phase: "assemble" },
  { id: "source-assets", label: "Source assets", shortLabel: "Sources", phase: "assemble" },
  { id: "grid-alignment", label: "Grid alignment", shortLabel: "Grid", phase: "assemble" },
  { id: "hypotheses", label: "Resistance hypotheses", shortLabel: "Hypotheses", phase: "assemble" },
  { id: "focal-areas", label: "Focal areas", shortLabel: "Focals", phase: "assemble" },
  ...shared.slice(1),
];

export function stepsFor(path: EntryPath): WorkflowStep[] {
  return path === "planning" ? planningAssembly : shared;
}
