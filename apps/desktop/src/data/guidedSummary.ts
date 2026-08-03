import type { ProjectSummary } from "../types";

/** Small shell metadata; the 220-design projection is lazy-loaded with Workspace. */
export const guidedSummary = {
  fixtureId: "cedar_trace_exact_v2",
  schemaVersion: "2.0",
  solverVersion: "5.2.4",
  completedJobs: 884,
  alternativeDesigns: 220,
  stormCases: 4,
  retainedRunSets: 20,
} as const;

export const demoProject: ProjectSummary = {
  id: guidedSummary.fixtureId,
  name: "Cedar Trace teaching fixture",
  kind: "demo",
  fitnessDisposition: "not_assessed",
  location: "Cedar Trace · synthetic study area",
  modelFile: "reference.inp",
  updatedAt: "2026-08-03T00:00:00Z",
};
