import type { Alternative, EvidenceItem } from "../types";
export { demoProject } from "./guidedSummary";

export type GuidedFixture = typeof import("./guidedFixture.json");

export interface GuidedData {
  fixture: GuidedFixture;
  alternatives: Alternative[];
  evaluatedAlternatives: Alternative[];
  evidence: Record<string, EvidenceItem>;
}

const colors = ["#08766f", "#2f6f9f", "#a96315", "#5c6670", "#b43d47", "#71558f"];
const slotLabels: Record<string, string> = {
  S1_BIO: "bioretention",
  S1_PP: "permeable pavement",
  S2_RG: "rain garden",
  S2_RB: "rain barrel",
};

/**
 * Build the UI projection from the fixture returned by the authoritative worker.
 *
 * Keeping this function pure makes the renderer's engineering data dependency
 * explicit: production screens cannot obtain the bundled JSON by importing this
 * module. The browser-only transport is the sole preview adapter that may load it.
 */
export function buildGuidedData(fixture: GuidedFixture): GuidedData {
const asAlternative = (item: (typeof fixture.alternatives)[number], colorIndex = 0): Alternative => {
  const interventions = Object.entries(item.additional_units)
    .filter(([, count]) => Number(count) > 0)
    .map(([slot, count]) => `${count} ${slotLabels[slot] ?? slot} units`)
    .join(" · ");
  const volume = item.four_storm_outfall_volume_million_gallons;
  const screeningCheck = (ruleId: string) => item.screening.checks.find((check) => check.rule_id === ruleId)?.passes === true;
  return {
    id: item.id,
    designId: item.design_id,
    sequence: item.sequence,
    name: item.name,
    cost: item.present_value_cost_thousand_usd,
    volume,
    peak: item.maximum_outfall_peak_cfs,
    reduction: item.outfall_volume_reduction_percent,
    status: item.screening.meets_all_rules ? "meets" : "fails",
    color: colors[colorIndex % colors.length],
    exactRuns: item.storm_runs.length,
    interventions,
    stormVolumes: item.storm_runs.map((run) => run.outfall_volume_million_gallons),
    retainedArtifacts: item.retained_artifacts,
    nondominated: item.nondominated,
    vector: item.vector,
    volumeTargetPass: screeningCheck("aggregate_volume_reduction"),
    costCapPass: screeningCheck("present_value_cost_cap"),
    peakGuardrailPass: screeningCheck("no_outfall_peak_increase"),
    qaPass: screeningCheck("solver_qa"),
    meetsAllRules: item.screening.meets_all_rules,
    worstPeakDelta: item.screening.worst_peak_change.delta_cfs,
    worstPeakStormId: item.screening.worst_peak_change.storm_id,
    worstPeakBaseline: item.screening.worst_peak_change.baseline_outfall_peak_cfs,
    worstPeakAlternative: item.screening.worst_peak_change.alternative_outfall_peak_cfs,
  };
};

const evaluatedAlternatives: Alternative[] = fixture.alternatives.map((item) =>
  asAlternative(item, item.sequence),
);

const alternatives: Alternative[] = fixture.retained_shortlist_sequences.map((sequence, index) => {
  const item = fixture.alternatives.find((candidate) => candidate.sequence === sequence);
  if (!item) throw new Error(`The exact guided shortlist is missing sequence ${sequence}.`);
  return asAlternative(item, index);
});

const shortHash = (value: string) => `${value.slice(0, 8)}…${value.slice(-8)}`;

const alternativeEvidence: Record<string, EvidenceItem> = Object.fromEntries(
  alternatives.map((alternative) => {
    const source = fixture.alternatives.find((item) => item.sequence === alternative.sequence)!;
    const controlling = source.storm_runs.reduce((worst, run) =>
      run.outfall_volume_million_gallons > worst.outfall_volume_million_gallons ? run : worst,
    );
    const stormName = (stormId: string) => fixture.storms.find((storm) => storm.storm_id === stormId)?.name ?? stormId;
    const changes = Object.entries(source.additional_units)
      .filter(([, count]) => Number(count) > 0)
      .map(([slot, count]) => `${slot}: additional units 0 → ${count}`);
    return [
      alternative.id,
      {
        id: alternative.id,
        kind: "alternative" as const,
        eyebrow: "Executed teaching alternative",
        title: `${alternative.id} · ${alternative.name}`,
        subtitle: alternative.id === "ALT-207"
          ? "Candidate to carry forward for site and feasibility review."
          : alternative.id === "ALT-017"
            ? "Low-cost caution — misses the volume target and increases a selected-storm peak."
            : alternative.id === "ALT-109"
              ? "Stronger hydraulic result — exceeds the synthetic screening cost cap."
              : alternative.id === "ALT-014"
                ? "Performance extreme — strongest observed volume result with poor cost fit."
                : "Fixed, result-independent artifact shortlist",
        status: "exact" as const,
        claim: `Exact SWMM result under documented synthetic assumptions; ${source.storm_runs.length} of 4 storm runs accepted.`,
        artifactsAvailable: source.retained_artifacts,
        runIds: source.storm_runs.map((run) => run.run_id),
        artifacts: source.storm_runs.flatMap((run) => Object.entries(run.artifacts).map(([label, path]) => ({
          label: `${stormName(run.storm_id)} · ${label.toUpperCase()}`,
          path,
          available: source.retained_artifacts,
        }))),
        runRecords: source.storm_runs.map((run) => ({
          storm: stormName(run.storm_id),
          runId: run.run_id,
          qa: run.qa_status.toUpperCase(),
          volume: `${run.outfall_volume_million_gallons.toFixed(3)} Mgal`,
          peak: `${run.outfall_peak_cfs.toFixed(2)} cfs`,
          baselinePeak: `${run.baseline_outfall_peak_cfs.toFixed(2)} cfs`,
          peakDelta: `${run.outfall_peak_delta_cfs >= 0 ? "+" : ""}${run.outfall_peak_delta_cfs.toFixed(2)} cfs`,
          inputHash: shortHash(run.input_sha256),
          outputHash: shortHash(run.output_sha256),
          reportHash: shortHash(run.report_sha256),
        })),
        fields: [
          { label: "Hydraulic result", value: "Exact under synthetic assumptions" },
          { label: "Design ID", value: source.design_id, mono: true },
          { label: "Storm coverage", value: `${source.storm_runs.length} of 4 accepted exact runs` },
          { label: "Four-storm outfall volume", value: `${alternative.volume.toFixed(3)} Mgal`, mono: true },
          { label: "Change from baseline", value: `−${alternative.reduction.toFixed(2)}%`, mono: true },
          { label: "Maximum outfall peak", value: `${alternative.peak.toFixed(2)} cfs`, mono: true },
          { label: "Teaching PV cost", value: `$${alternative.cost.toFixed(2)}k synthetic`, mono: true },
          { label: "Decision rules", value: alternative.meetsAllRules ? "PASSES ALL 4" : "DOES NOT PASS ALL 4", mono: true },
          { label: "Controlling run", value: controlling.run_id, mono: true },
          { label: "Input fingerprint", value: shortHash(controlling.input_sha256), mono: true },
          { label: "Output fingerprint", value: shortHash(controlling.output_sha256), mono: true },
          { label: "Solver", value: `EPA SWMM ${fixture.solver.version} · ${shortHash(fixture.solver.sha256)}`, mono: true },
          { label: "QA", value: `${controlling.qa_status.toUpperCase()} · continuity within 2% policy` },
          { label: "Artifact coverage", value: `${source.storm_runs.length} storms × INP/RPT/OUT retained` },
        ],
        changes,
      },
    ];
  }),
);

const baselineRuns = fixture.baseline.runs;
const largestRunoffError = Math.max(
  ...baselineRuns.map((run) => Math.abs(run.metrics.runoff_continuity_error_percent)),
);
const largestFlowError = Math.max(
  ...baselineRuns.map((run) => Math.abs(run.metrics.flow_continuity_error_percent)),
);

const stormEvidence: Record<string, EvidenceItem> = Object.fromEntries(
  fixture.storms.map((storm) => [
    `storm:${storm.storm_id}`,
    {
      id: `storm:${storm.storm_id}`,
      kind: "storm" as const,
      eyebrow: "Synthetic teaching storm",
      title: storm.name,
      subtitle: "User-defined fixture case with no regulatory authority",
      status: "source" as const,
      claim: "Source definition only; no recurrence interval or regulatory authority is inferred.",
      artifactsAvailable: true,
      runIds: baselineRuns.filter((run) => run.storm_id === storm.storm_id).map((run) => run.run_id),
      artifacts: baselineRuns
        .filter((run) => run.storm_id === storm.storm_id)
        .map((run) => ({ label: "Baseline input", path: run.artifacts.input, available: true })),
      fields: [
        { label: "Depth", value: `${storm.depth_inches.toFixed(2)} in`, mono: true },
        { label: "Duration", value: `${storm.duration_hours.toFixed(1)} hr`, mono: true },
        { label: "Interval", value: `${storm.interval_minutes} min`, mono: true },
        { label: "Authority status", value: storm.authority_status },
        { label: "Source", value: storm.source },
        { label: "Description", value: storm.description },
      ],
    },
  ]),
);

const evidence: Record<string, EvidenceItem> = {
  ...alternativeEvidence,
  ...stormEvidence,
  "guided-manifest": {
    id: "guided-manifest",
    kind: "qa",
    eyebrow: "Pinned exact-run manifest",
    title: "Cedar Trace fixture accounting",
    subtitle: `${fixture.job_accounting.completed_jobs} direct SWMM jobs reconciled against one hashed manifest`,
    status: "exact",
    claim: "The guided replay projects only exact results recorded in the pinned fixture manifest.",
    artifactsAvailable: true,
    artifacts: [{ label: "Exact fixture manifest", path: "benchmarks/guided_exact/manifest.json", available: true }],
    fields: [
      { label: "Fixture ID", value: fixture.fixture_id, mono: true },
      { label: "Completed / expected jobs", value: `${fixture.job_accounting.completed_jobs} / ${fixture.job_accounting.expected_jobs}`, mono: true },
      { label: "Untouched baselines", value: String(fixture.job_accounting.baseline_jobs), mono: true },
      { label: "Alternative designs", value: String(fixture.job_accounting.unique_alternative_designs), mono: true },
      { label: "Storm cases", value: String(fixture.job_accounting.storm_cases), mono: true },
      { label: "Manifest SHA-256", value: shortHash(fixture.source_manifest_sha256), mono: true },
      { label: "Solver SHA-256", value: shortHash(fixture.solver.sha256), mono: true },
      { label: "Metric extractor", value: `${fixture.extractor.version} · ${shortHash(fixture.extractor.script_sha256)}`, mono: true },
    ],
  },
  "guided-model": {
    id: "guided-model",
    kind: "model",
    eyebrow: "Self-authored CC0 fixture",
    title: "Cedar Trace teaching network",
    subtitle: "Synthetic, self-contained, and intentionally not reference matched",
    status: "source",
    claim: "Source and Model Fitness Passport record; successful execution is not calibration or reference matching.",
    artifactsAvailable: true,
    artifacts: [{ label: "Source model", path: "benchmarks/guided_exact/reference.inp", available: true }],
    fields: [
      { label: "Project kind", value: "TEACHING FIXTURE", mono: true },
      { label: "Model fitness", value: "NOT ASSESSED", mono: true },
      { label: "Source model SHA-256", value: shortHash(fixture.source_model_sha256), mono: true },
      { label: "Metric extractor", value: fixture.extractor.version, mono: true },
      { label: "Extractor SHA-256", value: shortHash(fixture.extractor.script_sha256), mono: true },
      { label: "Network", value: "2 subcatchments · 2 junctions · 2 conduits · 1 outfall" },
      { label: "Candidate slots", value: "4 integer-count decisions" },
      { label: "External dependencies", value: "None" },
      { label: "Reference comparison", value: "Not supplied" },
    ],
  },
  "concept-model": {
    id: "concept-model",
    kind: "model",
    eyebrow: "Unexecuted setup draft",
    title: "New conceptual study",
    subtitle: "No source data or hydraulic result has been recorded yet",
    status: "warning",
    artifactsAvailable: false,
    fields: [
      { label: "Project kind", value: "CONCEPTUAL", mono: true },
      { label: "Model fitness", value: "NOT ASSESSED", mono: true },
      { label: "Project state", value: "Draft setup preview" },
      { label: "Exact runs", value: "None" },
      { label: "Next action", value: "Load source data through the desktop worker" },
    ],
  },
  "import-model": {
    id: "import-model",
    kind: "model",
    eyebrow: "Unexecuted import draft",
    title: "New SWMM import",
    subtitle: "No project bundle, dependency, or hydraulic result has been recorded yet",
    status: "warning",
    artifactsAvailable: false,
    fields: [
      { label: "Project kind", value: "EXISTING MODEL IMPORT", mono: true },
      { label: "Model fitness", value: "NOT ASSESSED", mono: true },
      { label: "Project state", value: "Awaiting secure bundle inspection" },
      { label: "Exact runs", value: "None" },
      { label: "Next action", value: "Select a complete SWMM project bundle" },
    ],
  },
  "J1": {
    id: "J1",
    kind: "model",
    eyebrow: "Teaching-model junction",
    title: "J1 · upstream junction",
    subtitle: "Source object in reference.inp",
    status: "source",
    artifactsAvailable: true,
    fields: [
      { label: "Invert elevation", value: "100.00 ft", mono: true },
      { label: "Maximum depth", value: "5.00 ft", mono: true },
      { label: "Source", value: "reference.inp · [JUNCTIONS]" },
      { label: "Model fitness", value: "Not assessed · teaching fixture" },
    ],
  },
  "qa-continuity": {
    id: "qa-continuity",
    kind: "qa",
    eyebrow: "Executed baseline QA",
    title: "Continuity across four synthetic storms",
    subtitle: "All four baseline executions accepted under the fixture policy",
    status: "exact",
    claim: "Exact baseline solver QA under the fixture continuity policy; not a reference comparison.",
    artifactsAvailable: true,
    runIds: baselineRuns.map((run) => run.run_id),
    artifacts: baselineRuns.map((run, index) => ({
      label: `${fixture.storms[index].name} report`,
      path: run.artifacts.report,
      available: true,
    })),
    fields: [
      { label: "Largest runoff error", value: `${largestRunoffError.toFixed(3)}%`, mono: true },
      { label: "Largest flow-routing error", value: `${largestFlowError.toFixed(3)}%`, mono: true },
      { label: "Warning threshold", value: "±2.000%", mono: true },
      { label: "QA status", value: `${fixture.job_accounting.completed_jobs} of ${fixture.job_accounting.expected_jobs} accepted` },
      { label: "Solver SHA-256", value: shortHash(fixture.solver.sha256), mono: true },
    ],
  },
  "cost-basis": {
    id: "cost-basis",
    kind: "assumption",
    eyebrow: "Synthetic teaching cost basis",
    title: "Illustrative present-value unit costs",
    subtitle: "Explicitly not a bid, estimate, market source, or professional cost basis",
    status: "review",
    claim: "Synthetic teaching assumption; excluded from hydraulic cache identity.",
    artifactsAvailable: false,
    fields: [
      { label: "Currency / vintage", value: fixture.cost_basis.currency },
      { label: "Analysis horizon", value: `${fixture.cost_basis.analysis_horizon_years} years`, mono: true },
      { label: "Real discount rate", value: `${fixture.cost_basis.real_discount_rate_percent.toFixed(1)}%`, mono: true },
      { label: "Includes", value: "Self-authored CAPEX, annual O&M, and barrel replacement assumptions" },
      { label: "Source", value: fixture.cost_basis.source },
    ],
  },
};

return { fixture, alternatives, evaluatedAlternatives, evidence };
}
