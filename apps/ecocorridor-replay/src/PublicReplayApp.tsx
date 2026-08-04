import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import {
  RIDGEWAY_HYPOTHESIS_IDS,
  type RidgewayCurrentRaster,
  type RidgewayFixture,
  type RidgewayPairResult,
  type RidgewayPortfolio,
  type RidgewayScenarioResult,
} from "./replayData";

/*
 * These labels describe the four pinned synthetic resistance hypotheses. They
 * do not name species, places, or ecological outcomes.
 */
const HYPOTHESIS_LABELS: Record<(typeof RIDGEWAY_HYPOTHESIS_IDS)[number], string> = {
  h1_low_barrier: "Low highway penalty",
  h2_moderate_barrier: "Moderate highway penalty",
  h3_high_barrier: "High highway penalty",
  h4_future_pressure: "Future pressure",
};

interface HypothesisRankObservation {
  hypothesisId: (typeof RIDGEWAY_HYPOTHESIS_IDS)[number];
  label: string;
  rank: number;
  score: number;
}

function hypothesisRankRange(portfolio: RidgewayPortfolio): { best: HypothesisRankObservation; worst: HypothesisRankObservation } | null {
  const ranks = portfolio.hypothesisRanks;
  if (!ranks) return null;
  const observations = RIDGEWAY_HYPOTHESIS_IDS.map((hypothesisId) => ({
    hypothesisId,
    label: HYPOTHESIS_LABELS[hypothesisId],
    ...ranks[hypothesisId],
  }));
  const ordered = [...observations].sort((left, right) => left.rank - right.rank || left.hypothesisId.localeCompare(right.hypothesisId));
  return { best: ordered[0], worst: ordered[ordered.length - 1] };
}

type ReplayStep = "overview" | "fitness" | "scenarios" | "board" | "evidence";

interface EvidenceSelection {
  title: string;
  portfolio: RidgewayPortfolio;
  result: RidgewayScenarioResult;
  pair?: RidgewayPairResult;
}

const STEPS: Array<{ id: ReplayStep; label: string; short: string }> = [
  { id: "overview", label: "Decision brief", short: "01" },
  { id: "fitness", label: "Model fitness", short: "02" },
  { id: "scenarios", label: "Scenario ensemble", short: "03" },
  { id: "board", label: "Robustness board", short: "04" },
  { id: "evidence", label: "Evidence package", short: "05" },
];

function money(value: number | null): string {
  if (value == null) return "Withheld";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function signed(value: number | null, digits = 2): string {
  if (value == null) return "Withheld";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function isPublished(fixture: RidgewayFixture, portfolio: RidgewayPortfolio): boolean {
  return Boolean(
    fixture.releaseReplayEligible
    && portfolio.id
    && portfolio.costUsd != null
    && portfolio.worstResistanceChangePct != null
    && portfolio.worstScenarioId
    && portfolio.hypothesisRanks
    && fixture.study.hypothesisRankingMetric
    && portfolio.scenarioResults.length === fixture.scenarios.length,
  );
}

function statusLabel(portfolio: RidgewayPortfolio): string {
  if (portfolio.role === "carry-forward") {
    if (portfolio.ruleStatus === "pass") return "Carry-forward candidate";
    if (portfolio.ruleStatus === "review") return "Review before carry-forward";
    return "Does not meet rules";
  }
  if (portfolio.role === "over-budget") return "Over budget";
  if (portfolio.role === "brittle") return "Hypothesis-sensitive";
  return "Caution";
}

function safeguardDisposition(fixture: RidgewayFixture, portfolio: RidgewayPortfolio): { label: string; status: "pass" | "fail" | "review" } {
  const safeguardIds = new Set(fixture.scenarios.filter((scenario) => scenario.purpose === "safeguard").map((scenario) => scenario.id));
  const results = portfolio.scenarioResults.filter((result) => safeguardIds.has(result.scenarioId));
  if (results.length !== safeguardIds.size) return { label: "Incomplete", status: "review" };
  const pairResults = results.flatMap((result) => result.pairResults);
  const failedPairs = pairResults.filter((pair) => !pair.passed).length;
  if (failedPairs > 0) return { label: `${failedPairs} required pair${failedPairs === 1 ? "" : "s"} fail`, status: "fail" };
  return { label: `${results.length}/${safeguardIds.size} scenarios pass`, status: "pass" };
}

function PanelIntro({ eyebrow, title, children }: { eyebrow: string; title: string; children: React.ReactNode }) {
  return <header className="replay-intro"><p>{eyebrow}</p><h1 tabIndex={-1}>{title}</h1><div>{children}</div></header>;
}

function Overview({ fixture, onAdvance }: { fixture: RidgewayFixture; onAdvance: () => void }) {
  return <>
    <PanelIntro eyebrow="Public synthetic decision story" title="Which Ridgeway concept deserves another look?">
      <p>{fixture.decisionBrief.question}</p>
    </PanelIntro>
    <section className="replay-callout replay-verified" aria-label="Replay boundary">
      <span aria-hidden="true">◎</span>
      <div><strong>{fixture.releaseReplayEligible ? "Verified fixture replay" : "Direct evidence is not published"}</strong><p>{fixture.releaseReplayEligible ? `The static projection is bound to manifest ${fixture.manifestSha256?.slice(0, 16)}….` : fixture.releaseBlocker}</p></div>
    </section>
    <section className="replay-decision-card">
      <div className="replay-record"><span>RIDGEWAY</span><strong>SCREENING RECORD 01</strong></div>
      <div><h2>A shortlist for feasibility review—not a corridor recommendation.</h2><p>Compare {fixture.study.portfolioCount} fixed portfolios across {fixture.study.scenarioCount} synthetic ecological scenarios. Require every target and safeguard rule to hold, keep screening cost at or below {money(fixture.decisionBrief.budgetUsd)}, and inspect modeled current redistribution in the road-review mask.</p></div>
      <button type="button" className="replay-primary" onClick={onAdvance}>Begin with model fitness <span aria-hidden="true">→</span></button>
    </section>
    <section className="replay-transect" aria-label="Conceptual Ridgeway transect">
      <div className="replay-core"><span>A</span><small>Focal core</small></div><div className="replay-line" /><div className="replay-road"><span>HIGHWAY</span></div><div className="replay-line" /><div className="replay-core"><span>D</span><small>Focal core</small></div>
    </section>
    <div className="replay-rule-grid">
      <article><span>Target</span><strong>{fixture.decisionBrief.targetRule}</strong></article>
      <article><span>Safeguard</span><strong>{fixture.decisionBrief.safeguardRule}</strong></article>
      <article><span>Boundary</span><strong>Direct model result, never observed animal behavior</strong></article>
    </div>
  </>;
}

const FITNESS_ROWS = [
  ["Intended use", "Supported", "Synthetic teaching comparison only"],
  ["Resistance derivation", "Review", "Four synthetic reviewer-authored hypotheses; no field validation"],
  ["Focal-area rationale", "Supported", "Four anonymous synthetic habitat cores"],
  ["Action-effect evidence", "Review", "Immutable synthetic effects used only for teaching"],
  ["Scenario coverage", "Supported", "Twelve target and safeguard scenarios"],
  ["Numerical QA", "Supported", "Accepted QA required for every retained direct run"],
  ["Validation evidence", "Review", "No telemetry, movement, collision, or population-fit claim"],
  ["Unresolved assumptions", "Review", "Land access at the north approach remains unresolved"],
] as const;

function Fitness({ fixture }: { fixture: RidgewayFixture }) {
  return <>
    <PanelIntro eyebrow="Independent review" title="Model Fitness Passport"><p>Fitness is reported by dimension. No score can hide a weak ecological basis behind strong numerical QA.</p></PanelIntro>
    <section className="replay-passport">
      <div className="replay-passport-spine"><span>MODEL</span><strong>FITNESS</strong><small>RIDGEWAY · SYNTHETIC</small></div>
      <div>{FITNESS_ROWS.map(([label, state, detail]) => <article key={label}><span className={`replay-pill ${state === "Supported" ? "pass" : "review"}`}>{state}</span><strong>{label}</strong><p>{detail}</p></article>)}</div>
    </section>
    <section className="replay-two-up"><article><strong>Suitable for this lesson?</strong><p>Yes—for a synthetic robustness lesson under the stated assumptions and direct-run evidence boundary.</p></article><article><strong>Suitable for design or approval?</strong><p>No. This is not observed animal movement. This is not crossing use evidence. This is not collision reduction evidence. This is not population benefit evidence. This is not regulatory or design evidence.</p></article></section>
    <p className="replay-caption">Evidence state: <span className="mono">{fixture.evidenceState}</span></p>
  </>;
}

function Scenarios({ fixture }: { fixture: RidgewayFixture }) {
  return <>
    <PanelIntro eyebrow="Required ensemble" title="Compare within a scenario, then test across all of them."><p>Each row binds one anonymous guild, resistance hypothesis, future condition, maturity horizon, focal-pair set, and engine configuration.</p></PanelIntro>
    <section className="replay-info"><strong>Comparison rule</strong><p>Raw effective resistance is never aggregated across guilds, focal sets, grids, or incompatible hypotheses. Only baseline-relative changes inside each scenario are compared.</p></section>
    <section className="replay-table-panel"><div className="replay-panel-heading"><div><span>SCENARIO SCOPE</span><h2>Targets and safeguards</h2></div><code>{fixture.scenarios.length} / {fixture.study.scenarioCount}</code></div>
      <div className="replay-table-scroll" tabIndex={0} role="region" aria-label="Scrollable synthetic ecological scenario table"><table><thead><tr><th>Scenario</th><th>Purpose</th><th>Hypothesis</th><th>Condition</th><th>Maturity</th><th>Pairs</th></tr></thead><tbody>{fixture.scenarios.map((scenario) => <tr key={scenario.id}><td><code>{scenario.id}</code></td><td><span className={`replay-pill ${scenario.purpose === "safeguard" ? "review" : "info"}`}>{scenario.guild}</span></td><td>{scenario.hypothesis}</td><td>{scenario.futureCondition}</td><td>{scenario.maturity}</td><td>{scenario.includedPairs}</td></tr>)}</tbody></table></div>
    </section>
  </>;
}

function rasterFor(fixture: RidgewayFixture, runId: string): RidgewayCurrentRaster | null {
  return fixture.currentRasters.find((item) => item.runId === runId) ?? null;
}

function RasterView({ fixture, portfolio, result }: { fixture: RidgewayFixture; portfolio: RidgewayPortfolio; result: RidgewayScenarioResult }) {
  const [split, setSplit] = useState(50);
  const identity = useId().replaceAll(":", "");
  const baseline = rasterFor(fixture, result.baselineMapRunId);
  const selected = rasterFor(fixture, result.portfolioMapRunId);
  if (!baseline || !selected || !fixture.reviewMask) return <section className="replay-map-empty"><strong>Current-map evidence unavailable</strong><p>The public replay will not invent or substitute a diagnostic map.</p></section>;
  const max = Math.max(baseline.maximumCurrent, selected.maximumCurrent, Number.EPSILON);
  const rasterCells = (raster: RidgewayCurrentRaster) => raster.cells.map((value, index) => {
    const x = index % raster.width;
    const y = Math.floor(index / raster.width);
    const opacity = value == null ? 0 : Math.min(1, Math.max(0.06, Math.sqrt(value / max)));
    return <rect key={index} x={x} y={y} width="1.03" height="1.03" fill={value == null ? "#dfe7e1" : `rgba(22,125,141,${opacity})`} />;
  });
  const maskCells = fixture.reviewMask.cells.map((value, index) => {
    if (value !== 1) return null;
    const x = index % fixture.reviewMask!.width;
    const y = Math.floor(index / fixture.reviewMask!.width);
    return <rect key={index} x={x + 0.12} y={y + 0.12} width="0.76" height="0.76" fill="none" stroke="#b56b24" strokeWidth="0.12" />;
  });
  const splitX = baseline.width * split / 100;
  const descriptionId = `raster-description-${identity}`;
  const clipId = `baseline-clip-${identity}`;
  return <section className="replay-raster" aria-labelledby="raster-title">
    <div className="replay-panel-heading"><div><span>DIAGNOSTIC CURRENT</span><h2 id="raster-title">Baseline / portfolio map scrubber</h2></div><code>{result.scenarioId}</code></div>
    <p id={descriptionId}>Modeled current density is a circuit diagnostic—not an objective, animal-use surface, or movement prediction. Amber cells mark the configured road-review mask.</p>
    <div className="replay-raster-layout">
      <svg viewBox={`0 0 ${baseline.width} ${baseline.height}`} role="img" aria-describedby={descriptionId} aria-label={`${portfolio.name} and baseline diagnostic current comparison for ${result.scenarioId}; boundary at ${split}% baseline width`}>
        <defs><clipPath id={clipId}><rect x="0" y="0" width={splitX} height={baseline.height} /></clipPath></defs>
        <g>{rasterCells(selected)}</g>
        <g clipPath={`url(#${clipId})`}>{rasterCells(baseline)}</g>
        <g>{maskCells}</g>
        <line x1={splitX} x2={splitX} y1="0" y2={baseline.height} className="replay-split-line" vectorEffect="non-scaling-stroke" />
      </svg>
      <div>
        <label htmlFor={`replay-scrub-${identity}`}><span>Comparison boundary</span><strong>{split}% baseline width</strong></label>
        <input id={`replay-scrub-${identity}`} type="range" min="0" max="100" step="5" value={split} aria-valuetext={`${split}% baseline on the left and ${100 - split}% ${portfolio.name} on the right`} onChange={(event) => setSplit(Number(event.target.value))} />
        <div className="replay-scrub-labels"><span>Baseline · left</span><span>{portfolio.name} · right</span></div>
        <dl><div><dt>Scenario</dt><dd><code>{result.scenarioId}</code></dd></div><div><dt>Road-mask redistribution</dt><dd>{signed(result.reviewMaskChangePct)}</dd></div></dl>
      </div>
    </div>
    <details className="replay-map-table"><summary>View map evidence as a table</summary><div className="replay-table-scroll" tabIndex={0} role="region" aria-label={`Scrollable map evidence table for ${result.scenarioId}`}><table><thead><tr><th>Layer</th><th>Direct run</th><th>Maximum current</th><th>Valid cells</th><th>Raster SHA-256</th></tr></thead><tbody><tr><td>Baseline</td><td><code>{baseline.runId}</code></td><td><code>{baseline.maximumCurrent.toPrecision(7)}</code></td><td>{baseline.cells.filter((value) => value != null).length}</td><td><code>{baseline.sha256}</code></td></tr><tr><td>{portfolio.name}</td><td><code>{selected.runId}</code></td><td><code>{selected.maximumCurrent.toPrecision(7)}</code></td><td>{selected.cells.filter((value) => value != null).length}</td><td><code>{selected.sha256}</code></td></tr></tbody></table></div></details>
  </section>;
}

function EvidenceDialog({ selection, onClose }: { selection: EvidenceSelection; onClose: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prior = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    const background = [...document.querySelectorAll<HTMLElement>(
      ".replay-skip, .replay-topbar, .replay-sidebar, .replay-footer, #public-replay-main > :not(.replay-dialog-backdrop)",
    )];
    const backgroundState = background.map((node) => ({
      node,
      inert: node.inert,
      ariaHidden: node.getAttribute("aria-hidden"),
    }));
    for (const node of background) {
      node.inert = true;
      node.setAttribute("aria-hidden", "true");
    }
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !dialog.current) return;
      const nodes = [...dialog.current.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter((node) => !node.hasAttribute("disabled"));
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      document.body.style.overflow = priorOverflow;
      for (const state of backgroundState) {
        state.node.inert = state.inert;
        if (state.ariaHidden === null) state.node.removeAttribute("aria-hidden");
        else state.node.setAttribute("aria-hidden", state.ariaHidden);
      }
      prior?.focus();
    };
  }, [onClose]);
  const { portfolio, result, pair } = selection;
  return <div className="replay-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div ref={dialog} className="replay-dialog" role="dialog" aria-modal="true" aria-labelledby="evidence-title" aria-describedby="evidence-limitation"><header><div><span>LOCAL EVIDENCE RAIL</span><h2 id="evidence-title">{selection.title}</h2></div><button ref={close} type="button" onClick={onClose} aria-label="Close evidence rail">×</button></header><p id="evidence-limitation" className="replay-limitation">Direct Circuitscape result under documented assumptions. This is not observed animal movement. This is not crossing use evidence. This is not collision reduction evidence. This is not population benefit evidence. This is not biodiversity benefit evidence. This is not an optimal corridor. This is not approval or design evidence.</p><ol className="replay-chain"><li><span>Claim</span><strong>{pair?.claimId ?? `Scenario reducer · ${portfolio.id}`}</strong></li><li><span>Reducer</span><strong>{pair ? signed(pair.baselineRelativeChangePct) : signed(result.resistanceChangePct)} baseline-relative effective-resistance change</strong></li><li><span>Direct run</span><code>{pair?.portfolioRunId ?? result.runId}</code></li><li><span>Scenario</span><code>{result.scenarioId}</code></li><li><span>Matrix artifact</span><code>sha256:{pair?.portfolioMatrixSha256 ?? result.pairResults[0]?.portfolioMatrixSha256}</code></li><li><span>Baseline run</span><code>{pair?.baselineRunId ?? result.pairResults[0]?.baselineRunId}</code></li><li><span>QA</span><strong>Accepted in verified fixture manifest</strong></li></ol><p className="replay-caption">The no-login replay is read-only and makes no W&B, Weave, model-provider, or application-host call.</p></div></div>;
}

function Board({ fixture }: { fixture: RidgewayFixture }) {
  const published = fixture.portfolios.filter((portfolio) => isPublished(fixture, portfolio));
  const [portfolioId, setPortfolioId] = useState<string | null>(() => published.find((item) => item.role === "carry-forward")?.id ?? published[0]?.id ?? null);
  const portfolio = published.find((item) => item.id === portfolioId) ?? published[0] ?? null;
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceSelection | null>(null);
  const result = portfolio?.scenarioResults.find((item) => item.scenarioId === scenarioId) ?? portfolio?.scenarioResults.find((item) => item.scenarioId === portfolio.worstScenarioId) ?? portfolio?.scenarioResults[0] ?? null;
  const selectedSafeguard = portfolio ? safeguardDisposition(fixture, portfolio) : null;
  const selectedRankRange = portfolio ? hypothesisRankRange(portfolio) : null;
  useEffect(() => { setScenarioId(null); }, [portfolioId]);
  if (!fixture.releaseReplayEligible || !portfolio || !result) return <>
    <PanelIntro eyebrow="Decision-first comparison" title="Robustness Board"><p>Outcomes remain unpublished until the complete pinned direct Circuitscape package passes QA and manifest verification.</p></PanelIntro>
    <section className="replay-blocked"><span aria-hidden="true">⊘</span><div><strong>No portfolio ID, rank, cost outcome, or connectivity value is shown.</strong><p>{fixture.releaseBlocker}</p></div></section>
    <section className="replay-table-panel"><div className="replay-panel-heading"><div><span>TEACHING ROLES</span><h2>Selected only after direct runs</h2></div></div><div className="replay-table-scroll"><table><thead><tr><th>Role</th><th>Portfolio</th><th>Required lesson</th><th>State</th></tr></thead><tbody>{fixture.portfolios.map((item) => <tr key={item.projectionKey}><td>{item.name}</td><td>Not selected</td><td>{item.optionSummary}</td><td><span className="replay-pill review">Direct evidence required</span></td></tr>)}</tbody></table></div></section>
  </>;
  const scenario = fixture.scenarios.find((item) => item.id === result.scenarioId)!;
  return <>
    <PanelIntro eyebrow="Decision-first comparison" title="Robustness Board"><p>Start with every rule, the weakest scenario, safeguards, cost, and feasibility. Maps support the decision; they do not lead it.</p></PanelIntro>
    <section className="replay-table-panel"><div className="replay-panel-heading"><div><span>ACCEPTANCE MATRIX FIRST</span><h2>Four observed teaching roles</h2></div><code>internal precision · rounded display</code></div><p className="replay-caption">A negative percentage means lower modeled effective resistance than that same scenario's direct baseline. Computed rank compares the complete pinned 128-portfolio direct study; it is a study diagnostic, not an ecological value or animal-use prediction. Cost comes from the versioned synthetic screening basis, not Circuitscape.</p><p className="replay-scroll-hint" aria-hidden="true">Scroll horizontally to inspect rank sensitivity, safeguard, road-mask, cost, and disposition evidence →</p><div className="replay-table-scroll" tabIndex={0} role="region" aria-label="Scrollable portfolio acceptance matrix"><table><thead><tr><th>Inspect</th><th>Portfolio</th><th>Direct ensemble</th><th>Weakest change</th><th>Hypothesis rank range</th><th>Safeguard pairs</th><th>Road mask</th><th>Cost</th><th>Disposition</th></tr></thead><tbody>{published.map((item) => { const safeguard = safeguardDisposition(fixture, item); const rankRange = hypothesisRankRange(item); return <tr className={item.id === portfolio.id ? "selected" : ""} key={item.id!}><td><input type="radio" name="public-portfolio" checked={item.id === portfolio.id} onChange={() => setPortfolioId(item.id)} aria-label={`Inspect ${item.name}`} /></td><td><strong>{item.name}</strong><code>{item.id}</code></td><td>{item.scenarioResults.length}/{item.requiredCount} direct</td><td className={(item.worstResistanceChangePct ?? 0) > 0 ? "adverse" : "good"}>{signed(item.worstResistanceChangePct)}</td><td>{rankRange ? <><strong>#{rankRange.best.rank} → #{rankRange.worst.rank}</strong><small className="replay-table-subline">{rankRange.best.score.toFixed(1)}% → {rankRange.worst.score.toFixed(1)}% study score</small></> : "Withheld"}</td><td><span className={`replay-pill ${safeguard.status}`}>{safeguard.label}</span></td><td><span className={`replay-pill ${item.roadMaskFlag ? "review" : "pass"}`}>{item.roadMaskFlag ? "Review" : "Below threshold"}</span></td><td>{money(item.costUsd)}</td><td><span className={`replay-pill ${item.ruleStatus}`}>{statusLabel(item)}</span></td></tr>; })}</tbody></table></div><p className="replay-rank-basis"><strong>Computed rank basis:</strong> {fixture.study.hypothesisRankingMetric}</p></section>
    <section className={`replay-verdict ${portfolio.ruleStatus}`}><div className="replay-verdict-summary"><span>SELECTED EVIDENCE ROLE · {portfolio.id}</span><h2>{portfolio.name}</h2><p>{portfolio.nextAction}</p>{portfolio.role === "brittle" && selectedRankRange && <p className="replay-sensitivity-note"><strong>Why this option is shown:</strong> its computed position shifts from #{selectedRankRange.best.rank} under {selectedRankRange.best.label} to #{selectedRankRange.worst.rank} under {selectedRankRange.worst.label}. This direct-study sensitivity is not an animal-behavior prediction.</p>}</div><dl><div><dt>Weakest scenario</dt><dd><code>{portfolio.worstScenarioId}</code></dd></div><div><dt>Weakest change</dt><dd>{signed(portfolio.worstResistanceChangePct)}</dd></div><div><dt>Computed hypothesis rank</dt><dd><strong>{selectedRankRange ? `#${selectedRankRange.best.rank} → #${selectedRankRange.worst.rank}` : "Withheld"}</strong>{selectedRankRange && <small>{selectedRankRange.best.label} best · {selectedRankRange.worst.label} weakest</small>}</dd></div><div><dt>Safeguard rule</dt><dd><span className={`replay-pill ${selectedSafeguard!.status}`}>{selectedSafeguard!.label}</span></dd></div><div><dt>Screening cost</dt><dd>{money(portfolio.costUsd)}</dd></div><div><dt>Disposition</dt><dd>{statusLabel(portfolio)}</dd></div></dl><button type="button" className="replay-secondary" onClick={() => setEvidence({ title: `Controlling direct result · ${portfolio.name}`, portfolio, result })}>Trace controlling result</button></section>
    <RasterView fixture={fixture} portfolio={portfolio} result={result} />
    <section className="replay-table-panel"><div className="replay-panel-heading replay-pair-heading"><div><span>FOCAL-PAIR MATRIX</span><h2>{scenario.guild} · {scenario.hypothesis}</h2></div><label><span>Scenario shown in matrix and map</span><select value={result.scenarioId} onChange={(event) => setScenarioId(event.target.value)}>{portfolio.scenarioResults.map((item) => { const scope = fixture.scenarios.find((candidate) => candidate.id === item.scenarioId)!; return <option key={item.scenarioId} value={item.scenarioId}>{scope.guild} · {scope.hypothesis} · {signed(item.resistanceChangePct)}</option>; })}</select></label></div><p className="replay-caption">Pair values compare only this scenario with its own baseline. Raw effective resistance remains in model units and is never aggregated across scenarios.</p><div className="replay-table-scroll" tabIndex={0} role="region" aria-label={`Scrollable focal-pair evidence for ${result.scenarioId}`}><table><thead><tr><th>Required pair</th><th>Baseline resistance</th><th>Portfolio resistance</th><th>Change</th><th>Rule</th><th>Direct identity</th><th>Evidence</th></tr></thead><tbody>{result.pairResults.map((pair) => <tr key={pair.pairId}><td><code>{pair.pairId}</code></td><td><code>{pair.baselineEffectiveResistance.toPrecision(7)}</code></td><td><code>{pair.portfolioEffectiveResistance.toPrecision(7)}</code></td><td className={pair.baselineRelativeChangePct > 0 ? "adverse" : "good"}>{signed(pair.baselineRelativeChangePct)}</td><td><span className={`replay-pill ${pair.passed ? "pass" : "fail"}`}>{pair.passed ? "Pass" : "Fail"}</span></td><td><details><summary>Runs and hashes</summary><code>{pair.baselineRunId}</code><code>{pair.portfolioRunId}</code></details></td><td><button type="button" className="replay-text-button" onClick={() => setEvidence({ title: `Focal pair ${pair.pairId}`, portfolio, result, pair })}>Trace pair</button></td></tr>)}</tbody></table></div></section>
    {evidence && <EvidenceDialog selection={evidence} onClose={() => setEvidence(null)} />}
  </>;
}

function EvidencePackage({ fixture }: { fixture: RidgewayFixture }) {
  const download = () => {
    const payload = {
      schema_version: "ecocorridor-public-decision-story/v1.0",
      fixture_id: fixture.fixtureId,
      evidence_manifest_sha256: fixture.manifestSha256,
      evidence_state: fixture.evidenceState,
      limitation: fixture.disclaimer,
      portfolios: fixture.portfolios.map(({ id, name, role, costUsd, ruleStatus, worstResistanceChangePct, worstScenarioId, roadMaskFlag, nextAction }) => ({ id, name, role, costUsd, ruleStatus, worstResistanceChangePct, worstScenarioId, roadMaskFlag, nextAction })),
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "ridgeway-crossing-static-decision-story.json";
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  return <>
    <PanelIntro eyebrow="Shareable synthetic record" title="Evidence package"><p>The public replay can export only its redacted static teaching story. It cannot open professional projects or contact cloud services.</p></PanelIntro>
    <section className="replay-package"><div><span>LOCAL STATIC EXPORT</span><h2>{fixture.releaseReplayEligible ? "Verified synthetic decision story" : "Projection is not release-eligible"}</h2><p>{fixture.disclaimer}</p></div><button type="button" className="replay-primary" disabled={!fixture.releaseReplayEligible} onClick={download}>Download redacted JSON</button></section>
    <section className="replay-lineage"><h2>What this package can establish</h2><ol><li><span>01</span><p>Which fixed portfolios were evaluated under the documented synthetic scenario ensemble.</p></li><li><span>02</span><p>Which baseline-relative effective-resistance and road-mask diagnostics came from retained direct Circuitscape runs.</p></li><li><span>03</span><p>Why a portfolio may be carried forward for ecological, field, and engineering feasibility review.</p></li></ol><h2>What it cannot establish</h2><p>This is not observed animal movement. This is not crossing use evidence. This is not collision reduction evidence. This is not population benefit evidence. This is not biodiversity benefit evidence. This is not an optimal or approved corridor. This is not regulatory evidence. This is not a design-ready crossing.</p></section>
  </>;
}

export function PublicReplayApp({ fixture }: { fixture: RidgewayFixture }) {
  const [step, setStep] = useState<ReplayStep>("overview");
  const main = useRef<HTMLElement>(null);
  const navigation = useRef<HTMLElement>(null);
  const selectedIndex = STEPS.findIndex((item) => item.id === step);
  const verified = fixture.releaseReplayEligible && fixture.evidenceState === "VERIFIED_DIRECT_EVIDENCE";
  const jump = (next: ReplayStep) => { setStep(next); requestAnimationFrame(() => main.current?.querySelector<HTMLElement>("h1")?.focus()); };
  const onNavKeys = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? 0 : event.key === "End" ? STEPS.length - 1 : Math.min(STEPS.length - 1, Math.max(0, index + (event.key === "ArrowRight" ? 1 : -1)));
    setStep(STEPS[target].id);
    requestAnimationFrame(() => navigation.current?.querySelectorAll<HTMLButtonElement>("button")[target]?.focus());
  };
  useEffect(() => { document.title = `${STEPS[selectedIndex].label} · Ridgeway Crossing public replay`; }, [selectedIndex]);
  let content: React.ReactNode;
  if (step === "overview") content = <Overview fixture={fixture} onAdvance={() => jump("fitness")} />;
  else if (step === "fitness") content = <Fitness fixture={fixture} />;
  else if (step === "scenarios") content = <Scenarios fixture={fixture} />;
  else if (step === "board") content = <Board fixture={fixture} />;
  else content = <EvidencePackage fixture={fixture} />;
  return <div className="public-replay-shell">
    <a className="replay-skip" href="#public-replay-main">Skip to decision story</a>
    <header className="replay-topbar"><div className="replay-brand"><span aria-hidden="true">EC</span><div><strong>EcoCorridor</strong><small>SCENARIO STUDIO · PUBLIC REPLAY</small></div></div><div className="replay-boundary"><strong>No login · no cloud</strong><span>CC0 synthetic data only</span></div></header>
    <aside className="replay-sidebar" aria-label="Decision story navigation"><div><p>RIDGEWAY CROSSING</p><strong>Verified replay</strong><span className={`replay-pill ${verified ? "pass" : "review"}`}>{verified ? "Direct evidence" : "Evidence withheld"}</span></div><nav ref={navigation} aria-label="Replay checkpoints">{STEPS.map((item, index) => <button type="button" key={item.id} className={item.id === step ? "active" : ""} aria-current={item.id === step ? "step" : undefined} onClick={() => jump(item.id)} onKeyDown={(event) => onNavKeys(event, index)}><span>{item.short}</span>{item.label}</button>)}</nav><p className="replay-sidebar-note">Synthetic screening. This is not observed movement. This is not collision reduction evidence. This is not population viability evidence. This is not approval or design evidence.</p></aside>
    <main ref={main} id="public-replay-main" className="replay-main" tabIndex={-1}>{content}</main>
    <footer className="replay-footer"><span>CC0 Ridgeway Crossing · static, read-only projection</span><code>{fixture.manifestSha256 ? `sha256:${fixture.manifestSha256.slice(0, 16)}…` : "manifest withheld"}</code></footer>
  </div>;
}
