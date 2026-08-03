export type GuidedRunPhase =
  | "replay"
  | "validating"
  | "starting"
  | "running"
  | "pause_requested"
  | "cancel_requested"
  | "cancelled"
  | "completed"
  | "failed";

export interface GuidedRunState {
  phase: GuidedRunPhase;
  analysisId?: string;
  completedJobs: number;
  succeededJobs: number;
  rejectedJobs: number;
  failedJobs: number;
  cancelledJobs: number;
  totalJobs: number;
  message: string;
  error?: string;
}

export interface SolverJobStatusRow {
  job_id: string;
  state: string;
}

const terminalStates = new Set(["SUCCEEDED", "QA_REJECTED", "FAILED", "CANCELLED"]);

/** Collapse metric-expanded results.query rows to truthful per-job progress. */
export function summarizeSolverJobs(rows: SolverJobStatusRow[], expectedJobs: number) {
  const byJob = new Map<string, string>();
  for (const row of rows) byJob.set(row.job_id, row.state);
  const states = [...byJob.values()];
  const count = (state: string) => states.filter((item) => item === state).length;
  return {
    knownJobs: byJob.size,
    totalJobs: expectedJobs,
    completedJobs: states.filter((state) => terminalStates.has(state)).length,
    succeededJobs: count("SUCCEEDED"),
    rejectedJobs: count("QA_REJECTED"),
    failedJobs: count("FAILED"),
    cancelledJobs: count("CANCELLED"),
  };
}

export function initialGuidedRunState(totalJobs: number): GuidedRunState {
  return {
    phase: "replay",
    completedJobs: 0,
    succeededJobs: 0,
    rejectedJobs: 0,
    failedJobs: 0,
    cancelledJobs: 0,
    totalJobs,
    message: "Verified fixture replay loaded; no local solver jobs are running.",
  };
}
