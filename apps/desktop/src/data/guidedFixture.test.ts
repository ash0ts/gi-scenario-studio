import { describe, expect, it } from "vitest";
import fixture from "./guidedFixture.json";
import { buildGuidedData } from "./demo";
import { guidedSummary } from "./guidedSummary";

const { alternatives, evaluatedAlternatives, evidence } = buildGuidedData(fixture);

describe("exact guided fixture projection", () => {
  it("keeps the lazy shell summary synchronized", () => {
    expect(guidedSummary.fixtureId).toBe(fixture.fixture_id);
    expect(guidedSummary.schemaVersion).toBe(fixture.schema_version);
    expect(guidedSummary.solverVersion).toBe(fixture.solver.version);
    expect(guidedSummary.completedJobs).toBe(fixture.job_accounting.completed_jobs);
    expect(guidedSummary.retainedRunSets).toBe(fixture.job_accounting.retained_jobs);
  });

  it("contains the complete executed job ledger", () => {
    expect(fixture.fixture_id).toBe("cedar_trace_exact_v2");
    expect(fixture.schema_version).toBe("2.0");
    expect(fixture.project_kind).toBe("demo");
    expect(fixture.fitness_disposition).toBe("not_assessed");
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
    expect(alternatives).toHaveLength(4);
    expect(fixture.retained_shortlist_sequences).toEqual([13, 16, 108, 206]);
    expect(alternatives.every((alternative) => alternative.retainedArtifacts)).toBe(true);
    expect(alternatives.every((alternative) => alternative.exactRuns === 4)).toBe(true);
    const retainedRuns = fixture.alternatives
      .filter((alternative) => alternative.retained_artifacts)
      .flatMap((alternative) => alternative.storm_runs);
    expect(retainedRuns).toHaveLength(16);
    expect(fixture.job_accounting.retained_jobs).toBe(20);
    expect(retainedRuns.every((run) => Object.keys(run.artifacts).length === 3)).toBe(true);
  });

  it("projects the four-rule teaching disposition and adverse-result lesson", () => {
    const byId = Object.fromEntries(alternatives.map((alternative) => [alternative.id, alternative]));
    expect(byId["ALT-207"]).toMatchObject({
      vector: [0, 3, 2, 6],
      volumeTargetPass: true,
      costCapPass: true,
      peakGuardrailPass: true,
      qaPass: true,
      meetsAllRules: true,
    });
    expect(byId["ALT-017"]).toMatchObject({
      vector: [0, 0, 1, 6],
      volumeTargetPass: false,
      costCapPass: true,
      peakGuardrailPass: false,
      qaPass: true,
      worstPeakBaseline: 3.14,
      worstPeakAlternative: 3.21,
      worstPeakDelta: 0.07,
    });
    expect(alternatives.filter((alternative) => alternative.meetsAllRules).map((alternative) => alternative.id)).toEqual(["ALT-207"]);
  });

  it("traces every retained alternative through all four storm-specific records and artifacts", () => {
    for (const alternative of alternatives) {
      expect(evidence[alternative.id].runRecords).toHaveLength(4);
      expect(evidence[alternative.id].artifacts).toHaveLength(12);
    }
    expect(evidence["ALT-207"].subtitle).toBe("Candidate to carry forward for site and feasibility review.");
  });
});
