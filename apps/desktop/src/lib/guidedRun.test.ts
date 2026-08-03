import { describe, expect, it } from "vitest";
import { initialGuidedRunState, summarizeSolverJobs } from "./guidedRun";

describe("guided local-rerun progress", () => {
  it("counts jobs once when results.query repeats them for each metric", () => {
    const summary = summarizeSolverJobs([
      { job_id: "job-1", state: "SUCCEEDED" },
      { job_id: "job-1", state: "SUCCEEDED" },
      { job_id: "job-2", state: "RUNNING" },
      { job_id: "job-3", state: "QA_REJECTED" },
      { job_id: "job-4", state: "FAILED" },
      { job_id: "job-5", state: "CANCELLED" },
    ], 884);

    expect(summary).toMatchObject({
      knownJobs: 5,
      completedJobs: 4,
      succeededJobs: 1,
      rejectedJobs: 1,
      failedJobs: 1,
      cancelledJobs: 1,
      totalJobs: 884,
    });
  });

  it("distinguishes the pinned replay from a local execution", () => {
    expect(initialGuidedRunState(884)).toMatchObject({
      phase: "replay",
      completedJobs: 0,
      totalJobs: 884,
    });
  });
});
