import fixture from "./guidedFixture.json";
import type { Alternative, EvidenceItem, ProjectSummary } from "../types";

export const guidedFixture = fixture;

export const demoProject: ProjectSummary = {
  id: fixture.fixture_id,
  name: "CC0 exact-run teaching fixture",
  kind: "demo",
  readiness: "DEMO",
  location: "Synthetic study area",
  modelFile: "reference.inp",
  updatedAt: "2026-08-03T00:00:00Z",
};

const colors = ["#08766f", "#2f6f9f", "#a96315", "#5c6670", "#b43d47", "#71558f"];
const slotLabels: Record<string, string> = {
  S1_BIO: "bioretention",
  S1_PP: "permeable pavement",
  S2_RG: "rain garden",
  S2_RB: "rain barrel",
};

const asAlternative = (item: (typeof fixture.alternatives)[number], colorIndex = 0): Alternative => {
  const interventions = Object.entries(item.additional_units)
    .filter(([, count]) => Number(count) > 0)
    .map(([slot, count]) => `${count} ${slotLabels[slot] ?? slot} units`)
    .join(" · ");
  const volume = item.four_storm_outfall_volume_million_gallons;
  return {
    id: item.id,
    designId: item.design_id,
    sequence: item.sequence,
    name: item.name,
    cost: item.present_value_cost_thousand_usd,
    volume,
    peak: item.maximum_outfall_peak_cfs,
    reduction: item.outfall_volume_reduction_percent,
    status: volume <= 0.7 ? "meets" : volume <= 0.72 ? "partial" : "fails",
    color: colors[colorIndex % colors.length],
    exactRuns: item.storm_runs.length,
    interventions,
    stormVolumes: item.storm_runs.map((run) => run.outfall_volume_million_gallons),
    retainedArtifacts: item.retained_artifacts,
    nondominated: item.nondominated,
  };
};

export const evaluatedAlternatives: Alternative[] = fixture.alternatives.map((item) =>
  asAlternative(item, item.sequence),
);

export const alternatives: Alternative[] = fixture.retained_shortlist_sequences.map((sequence, index) => {
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
        subtitle: alternative.nondominated
          ? "Nondominated for teaching PV cost and four-storm volume among 220 exact alternatives"
          : "Fixed, result-independent artifact shortlist",
        status: "exact" as const,
        artifactsAvailable: source.retained_artifacts,
        fields: [
          { label: "Hydraulic result", value: "Exact under synthetic assumptions" },
          { label: "Design ID", value: source.design_id, mono: true },
          { label: "Storm coverage", value: `${source.storm_runs.length} of 4 accepted exact runs` },
          { label: "Four-storm outfall volume", value: `${alternative.volume.toFixed(3)} Mgal`, mono: true },
          { label: "Change from baseline", value: `−${alternative.reduction.toFixed(2)}%`, mono: true },
          { label: "Maximum outfall peak", value: `${alternative.peak.toFixed(2)} cfs`, mono: true },
          { label: "Teaching PV cost", value: `$${alternative.cost.toFixed(2)}k synthetic`, mono: true },
          { label: "Controlling run", value: controlling.run_id, mono: true },
          { label: "Input fingerprint", value: shortHash(controlling.input_sha256), mono: true },
          { label: "Output fingerprint", value: shortHash(controlling.output_sha256), mono: true },
          { label: "Solver", value: `EPA SWMM ${fixture.solver.version} · ${shortHash(fixture.solver.sha256)}`, mono: true },
          { label: "QA", value: `${controlling.qa_status.toUpperCase()} · continuity within 2% policy` },
          ...(Object.keys(controlling.artifacts).length > 0
            ? [{ label: "Retained artifacts", value: Object.values(controlling.artifacts).join(" · "), mono: true }]
            : []),
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
      artifactsAvailable: true,
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

export const evidence: Record<string, EvidenceItem> = {
  ...alternativeEvidence,
  ...stormEvidence,
  "guided-model": {
    id: "guided-model",
    kind: "model",
    eyebrow: "Self-authored CC0 fixture",
    title: "Two-subcatchment teaching network",
    subtitle: "Synthetic, self-contained, and intentionally not reference matched",
    status: "source",
    artifactsAvailable: true,
    fields: [
      { label: "Readiness", value: "DEMO", mono: true },
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
      { label: "Readiness", value: "CONCEPTUAL", mono: true },
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
      { label: "Readiness", value: "CONCEPTUAL", mono: true },
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
      { label: "Readiness", value: "DEMO · not engineer reviewed" },
    ],
  },
  "qa-continuity": {
    id: "qa-continuity",
    kind: "qa",
    eyebrow: "Executed baseline QA",
    title: "Continuity across four synthetic storms",
    subtitle: "All four baseline executions accepted under the fixture policy",
    status: "exact",
    artifactsAvailable: true,
    fields: [
      { label: "Largest runoff error", value: `${largestRunoffError.toFixed(3)}%`, mono: true },
      { label: "Largest flow-routing error", value: `${largestFlowError.toFixed(3)}%`, mono: true },
      { label: "Warning threshold", value: "±2.000%", mono: true },
      { label: "QA status", value: "884 of 884 accepted" },
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
