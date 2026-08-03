import { describe, expect, it } from "vitest";
import fixture from "./guidedFixture.json";
import { alternatives, evaluatedAlternatives } from "./demo";

describe("exact guided fixture projection", () => {
  it("contains the complete executed job ledger", () => {
    expect(fixture.readiness).toBe("DEMO");
    expect(fixture.job_accounting).toMatchObject({
      expected_jobs: 884,
      completed_jobs: 884,
      unique_alternative_designs: 220,
      storm_cases: 4,
    });
    expect(evaluatedAlternatives).toHaveLength(220);
    expect(fixture.baseline.runs).toHaveLength(4);
    expect(fixture.alternatives.every((design) => design.storm_runs.length === 4)).toBe(true);
  });

  it("binds every projected hydraulic result to accepted runs and one solver hash", () => {
    const runs = fixture.alternatives.flatMap((design) => design.storm_runs);
    expect(runs).toHaveLength(880);
    expect(runs.every((run) => run.qa_status === "accepted")).toBe(true);
    expect(fixture.solver.version).toBe("5.2.4");
    expect(fixture.solver.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(runs.every((run) => run.input_sha256.length === 64 && run.output_sha256.length === 64)).toBe(true);
  });

  it("shows only result-independent alternatives whose artifacts were retained", () => {
    expect(alternatives).toHaveLength(8);
    expect(fixture.retained_shortlist_sequences).toEqual([0, 1, 2, 3, 54, 109, 164, 219]);
    expect(alternatives.every((alternative) => alternative.retainedArtifacts)).toBe(true);
    expect(alternatives.every((alternative) => alternative.exactRuns === 4)).toBe(true);
    const retainedRuns = fixture.alternatives
      .filter((alternative) => alternative.retained_artifacts)
      .flatMap((alternative) => alternative.storm_runs);
    expect(retainedRuns).toHaveLength(32);
    expect(retainedRuns.every((run) => Object.keys(run.artifacts).length === 3)).toBe(true);
  });
});
