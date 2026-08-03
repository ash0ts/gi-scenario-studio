import { describe, expect, it } from "vitest";
import {
  aggregateValidation,
  candidateSlotPayload,
  designVectorErrors,
  emptyCandidateSlot,
  exactJobArithmetic,
  importedModelSummaries,
  initialStormCases,
  resolvedRunEvidence,
  summarizeExactJobs,
  type CandidateSlotDraft,
  type ModelValidationResult,
} from "./practitioner";

const approvedDraft = (): CandidateSlotDraft => ({
  slotId: "S1-BIO",
  subcatchmentId: "S1",
  lidControl: "BIOCELL",
  lidType: "BC",
  baseUnits: "1",
  minimumAdditionalUnits: "0",
  maximumAdditionalUnits: "3",
  unitArea: "200",
  surfaceWidth: "20",
  initialSaturationPercent: "0",
  fromImperviousPercent: "15",
  drainTo: "J1",
  displacedSurfaceArea: "600",
  fromPervious: false,
  routeToPervious: false,
  feasibilityBasis: "Reviewed GIS footprint and field note GI-17.",
  engineerApproved: true,
});

const acceptedValidation = (): ModelValidationResult => ({
  issues: [],
  checks: [],
  section_names: ["OPTIONS", "SUBCATCHMENTS"],
  object_counts: { SUBCATCHMENTS: 1 },
  blocking_count: 0,
  warning_count: 0,
  structurally_runnable: true,
});

describe("practitioner exact-analysis setup", () => {
  it("leaves unknown engineering quantities blank until the engineer enters them", () => {
    expect(emptyCandidateSlot(0)).toMatchObject({
      lidType: "",
      baseUnits: "",
      minimumAdditionalUnits: "0",
      maximumAdditionalUnits: "",
      unitArea: "",
      surfaceWidth: "",
      initialSaturationPercent: "",
      fromImperviousPercent: "",
      displacedSurfaceArea: "",
      fromPervious: false,
      routeToPervious: false,
    });
  });

  it("maps validated portable dependencies into isolated-run target/source pairs", () => {
    const models = importedModelSummaries([{
      model_path: "model/reference.inp",
      references: ["rain/event.dat"],
      resolved: ["model/rain/event.dat"],
      missing: [],
    }]);

    expect(models[0]).toMatchObject({
      projectPath: "sources/imported/model/reference.inp",
      dependencies: {
        "rain/event.dat": "sources/imported/model/rain/event.dat",
      },
    });
    expect(initialStormCases(models, models[0].projectPath)[0]).toMatchObject({
      stormId: "reference",
      modelPath: "sources/imported/model/reference.inp",
      included: true,
      stormEligible: true,
      modelRole: "authoritative_base",
    });
  });

  it("keeps non-selected INPs classified as companions and out of storm analysis", () => {
    const models = importedModelSummaries([
      { model_path: "base.inp", references: [], resolved: [], missing: [] },
      { model_path: "storm-looking-name.inp", references: [], resolved: [], missing: [] },
    ]);

    expect(initialStormCases(models, models[0].projectPath)).toEqual([
      expect.objectContaining({
        displayPath: "base.inp",
        included: true,
        stormEligible: true,
        modelRole: "authoritative_base",
      }),
      expect.objectContaining({
        displayPath: "storm-looking-name.inp",
        included: false,
        stormEligible: false,
        modelRole: "unclassified_companion",
      }),
    ]);
  });

  it("does not turn blank engineering quantities into silent defaults", () => {
    const draft = approvedDraft();
    draft.unitArea = "";
    expect(() => candidateSlotPayload(draft)).toThrow("unit area is required");
  });

  it("creates a complete typed slot only after explicit engineer approval", () => {
    const slot = candidateSlotPayload(approvedDraft());
    expect(slot).toMatchObject({
      slot_id: "S1-BIO",
      base_units: 1,
      max_additional_units: 3,
      unit_area: 200,
      engineer_approved: true,
    });
  });

  it("keeps the untouched baseline inside every approved slot bound", () => {
    const draft = approvedDraft();
    draft.minimumAdditionalUnits = "1";
    expect(() => candidateSlotPayload(draft)).toThrow("untouched baseline remains in range");
  });

  it("rejects baseline, duplicate, and out-of-bounds alternative vectors", () => {
    const slot = candidateSlotPayload(approvedDraft());
    expect(designVectorErrors([[0], [1], [1], [4]], [slot])).toEqual([
      "Alternative 1 duplicates the automatic baseline.",
      "Alternative 3 duplicates another exact vector.",
      "Alternative 4: S1-BIO must be 0–3.",
    ]);
  });

  it("shows exact baseline-plus-design-by-storm arithmetic", () => {
    expect(exactJobArithmetic(4, [[1], [2], [2], [0]])).toEqual({
      baselineJobs: 4,
      alternativeDesigns: 2,
      alternativeJobs: 8,
      totalJobs: 12,
    });
  });

  it("requires validation evidence for the authoritative analysis case", () => {
    const models = importedModelSummaries([
      { model_path: "a.inp", references: [], resolved: [], missing: [] },
      { model_path: "b.inp", references: [], resolved: [], missing: [] },
    ]);
    const storms = initialStormCases(models, models[0].projectPath);
    expect(aggregateValidation(storms)).toBeNull();
    storms[0].validation = acceptedValidation();
    expect(aggregateValidation(storms)).toMatchObject({ blocking_count: 0, structurally_runnable: true });
    storms[0].validation = { ...acceptedValidation(), blocking_count: 1, structurally_runnable: false };
    expect(aggregateValidation(storms)).toMatchObject({ blocking_count: 1, structurally_runnable: false });
  });

  it("deduplicates metric-expanded result rows for truthful progress", () => {
    const base = { analysis_id: "analysis", design_id: "design", storm_id: "storm", metric_id: null, object_id: null, value: null, units: null };
    expect(summarizeExactJobs([
      { ...base, job_id: "job-1", state: "SUCCEEDED" },
      { ...base, job_id: "job-1", state: "SUCCEEDED", metric_id: "peak" },
      { ...base, job_id: "job-2", state: "RUNNING" },
    ], 2)).toMatchObject({ knownJobs: 2, completedJobs: 1, succeededJobs: 1, totalJobs: 2 });
  });

  it("builds Evidence Rail content only from resolved worker run evidence", () => {
    const item = resolvedRunEvidence({
      run_id: "job-1",
      analysis_id: "analysis",
      model_revision_id: "revision-1",
      design_id: "design",
      storm_id: "storm",
      state: "SUCCEEDED",
      exact: true,
      metrics: { peak: 3.2 },
      metric_records: [{ metric_id: "outfall_peak_cfs", object_id: "OUT1", value: 3.2, units: "cfs" }],
      qa_status: "accepted",
      qa_findings: [],
      qa_records: [],
      artifacts: [{ role: "output", sha256: "a".repeat(64), relative_path: "runs/job-1/output.out", bytes: 42 }],
      input_sha256: "b".repeat(64),
      report_sha256: "e".repeat(64),
      solver_sha256: "c".repeat(64),
      output_sha256: "a".repeat(64),
      extractor_version: "swmm-report-metrics-v2",
      hashes: { input: "b".repeat(64), solver: "c".repeat(64), output: "a".repeat(64) },
      physics_cache_key: "d".repeat(64),
      cache_hit: false,
      completed_at: "2026-08-03T00:00:00Z",
    });

    expect(item).toMatchObject({
      id: "job-1",
      status: "exact",
      claim: "Exact SWMM result under documented assumptions.",
      artifactsAvailable: true,
    });
    expect(item.fields.some((field) => field.label.includes("outfall_peak_cfs") && field.value === "3.2 cfs")).toBe(true);
    expect(item.fields).toEqual(expect.arrayContaining([
      { label: "Model revision", value: "revision-1", mono: true },
      { label: "Metric extractor", value: "swmm-report-metrics-v2", mono: true },
      { label: "Report SHA-256", value: "e".repeat(64), mono: true },
    ]));
  });
});
