import { useEffect, useId, useMemo, useRef, useState } from "react";

import { ScenarioDetailResponseSchema, type ScenarioDetailResponse } from "../lib/professional";
import { hostErrorMessage, hostRequest, rpcData } from "../lib/host";
import type { EvidenceSelection, RpcPayload } from "../types";
import { Icon } from "./Icon";

interface ProfessionalScenarioEvidenceProps {
  projectHandle: string;
  analysisId: string;
  portfolioId: string;
  scenarioIds: string[];
  initialScenarioId: string;
  onEvidence: (selection: EvidenceSelection) => void;
}

const CELL_PAGE_SIZE = 100;

function currentText(value: number | null): string {
  if (value === null) return "NODATA";
  if (value === 0) return "0";
  return Math.abs(value) < 0.001 || Math.abs(value) >= 10_000
    ? value.toExponential(4)
    : value.toPrecision(7);
}

function currentFill(value: number | null, maximum: number): string {
  if (value === null) return "transparent";
  const intensity = maximum > 0 ? Math.log1p(value) / Math.log1p(maximum) : 0;
  const start = [226, 237, 231];
  const end = [22, 125, 141];
  const color = start.map((channel, index) => Math.round(channel + (end[index] - channel) * intensity));
  return `rgb(${color.join(",")})`;
}

function pairEvidenceSelection(
  detail: ScenarioDetailResponse,
  pair: ScenarioDetailResponse["pair_evidence"][number],
): EvidenceSelection {
  return {
    claimId: pair.primary_evidence_claim_id,
    title: `Direct focal-pair evidence · ${pair.pair_id}`,
    value: pair.baseline_relative_reduction_percent.toPrecision(8),
    units: "percent reduction relative to this scenario baseline",
    status: "verified",
    portfolioId: detail.portfolio_id,
    scenarioId: detail.scenario.scenario_id,
    runId: pair.portfolio_direct_run_id,
    baselineRunId: pair.baseline_direct_run_id,
    reducer: "(baseline effective resistance − portfolio effective resistance) ÷ baseline × 100; positive means lower modeled resistance",
    assumption: "This value applies only to the named guild, focal set, resistance hypothesis, future condition, maturity horizon, grid, and engine settings.",
    source: "PROFESSIONAL_CLAIM",
  };
}

function mapEvidenceSelection(detail: ScenarioDetailResponse, claimId: string): EvidenceSelection {
  return {
    claimId,
    title: `Direct current-map diagnostic · ${detail.scenario.scenario_id}`,
    value: "Resolved from retained map claim",
    units: "diagnostic current redistribution",
    status: "review",
    portfolioId: detail.portfolio_id,
    scenarioId: detail.scenario.scenario_id,
    runId: detail.portfolio_run.direct_run_id,
    baselineRunId: detail.baseline_run.direct_run_id,
    reducer: "Configured review-mask reducer over same-scenario retained current rasters",
    assumption: "Current density is a model diagnostic, not observed or predicted animal use.",
    source: "PROFESSIONAL_CLAIM",
  };
}

function ProfessionalCurrentMap({ detail, scrub, onScrub, onPage, onEvidence }: {
  detail: ScenarioDetailResponse;
  scrub: number;
  onScrub: (value: number) => void;
  onPage: (offset: number) => void;
  onEvidence: (selection: EvidenceSelection) => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const prefix = useId().replaceAll(":", "");
  const map = detail.map_evidence;
  if (map.status === "ABSENT") {
    return (
      <section className="paper-panel professional-map-absence" role="status" aria-labelledby={titleId}>
        <Icon name="map" />
        <div><span className="section-kicker">Current-map evidence</span><h2 id={titleId}>Map comparison was not retained</h2><p><strong>{map.absence_code}</strong> · {map.absence_reason}</p><p>{map.interpretation}</p></div>
      </section>
    );
  }

  const cellSize = 10;
  const width = map.preview.width * cellSize;
  const height = map.preview.height * cellSize;
  const divider = width * Math.max(0, Math.min(100, scrub)) / 100;
  const maximum = Math.max(map.preview.baseline_maximum, map.preview.portfolio_maximum);
  const baselineClip = `${prefix}-baseline`;
  const portfolioClip = `${prefix}-portfolio`;
  const nodataPattern = `${prefix}-nodata`;
  const mapLabel = `${scrub}% baseline width · ${100 - scrub}% selected-portfolio width`;
  const page = map.exact_cell_page;

  const rects = (side: "baseline" | "portfolio") => map.preview.cells.map((cell) => {
    const value = side === "baseline" ? cell.baseline_current : cell.portfolio_current;
    return <rect key={`${side}-${cell.preview_row}-${cell.preview_column}`} x={cell.preview_column * cellSize} y={cell.preview_row * cellSize} width={cellSize} height={cellSize} fill={value === null ? `url(#${nodataPattern})` : currentFill(value, maximum)} />;
  });

  return (
    <section className="current-raster-shell professional-current-raster" aria-labelledby={titleId}>
      <div className="current-raster-heading">
        <div><span className="section-kicker">Same-scenario current-map evidence</span><h2 id={titleId}>Baseline ↔ selected portfolio</h2><p>{detail.scenario.scenario_id} · {detail.scenario.guild_id} · {detail.scenario.resistance_hypothesis_id}</p></div>
        {map.evidence_claim_ids[0] && <button className="secondary-button" type="button" onClick={() => onEvidence(mapEvidenceSelection(detail, map.evidence_claim_ids[0]))}><Icon name="evidence" /> Trace map diagnostic</button>}
      </div>
      <div className="current-raster-layout">
        <div className="current-raster-figure">
          <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${titleId} ${descriptionId}`} preserveAspectRatio="xMidYMid meet">
            <desc id={descriptionId}>A split diagnostic preview made from a systematic sample of exact retained current-raster cells for one ecological scenario. The exact-cell table remains authoritative. This is not observed or predicted animal use.</desc>
            <defs>
              <pattern id={nodataPattern} width="6" height="6" patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#d9dfda" /><path d="M-1 1 1-1M0 6 6 0M5 7 7 5" stroke="#9ba7a0" strokeWidth="1" /></pattern>
              <clipPath id={baselineClip}><rect x="0" y="0" width={divider} height={height} /></clipPath>
              <clipPath id={portfolioClip}><rect x={divider} y="0" width={Math.max(0, width - divider)} height={height} /></clipPath>
            </defs>
            <g clipPath={`url(#${baselineClip})`}>{rects("baseline")}</g>
            <g clipPath={`url(#${portfolioClip})`}>{rects("portfolio")}</g>
            <line className="current-raster-divider" x1={divider} x2={divider} y1="0" y2={height} />
          </svg>
          <div className="current-raster-key" aria-hidden="true"><span>Lower modeled current</span><i /><span>Higher modeled current</span><b>Exact systematic sample</b></div>
          <label className="raster-scrub-control"><span>Move the baseline / selected divider</span><input type="range" min="0" max="100" value={scrub} aria-label="Move the professional baseline / selected divider" aria-valuetext={mapLabel} onChange={(event) => onScrub(Number(event.target.value))} /><output>{mapLabel}</output></label>
        </div>
        <div className="current-raster-facts">
          <p><strong>Diagnostic view only.</strong> The preview selects exact source cells systematically across the full grid and applies one shared log1p color scale. It is not a model objective or animal-use prediction.</p>
          <dl><div><dt>Reviewed grid</dt><dd>{map.grid.width.toLocaleString()} × {map.grid.height.toLocaleString()} cells · {map.grid.crs}</dd></div><div><dt>Preview basis</dt><dd>{map.preview.width} × {map.preview.height} exact sampled cells</dd></div><div><dt>Baseline run</dt><dd className="mono">{map.baseline_artifact.direct_run_id}</dd></div><div><dt>Baseline raster</dt><dd className="mono">sha256:{map.baseline_artifact.sha256}</dd></div><div><dt>Selected run</dt><dd className="mono">{map.portfolio_artifact.direct_run_id}</dd></div><div><dt>Selected raster</dt><dd className="mono">sha256:{map.portfolio_artifact.sha256}</dd></div></dl>
        </div>
      </div>
      <details className="current-raster-table" open>
        <summary>Inspect exact retained cells {page.offset + 1}–{page.offset + page.cells.length} of {page.total_cell_count.toLocaleString()}</summary>
        <p>Coordinates are cell centers in the reviewed grid CRS. Selected − baseline is an arithmetic aid in diagnostic current units, not an ecological outcome.</p>
        <div className="table-scroll" tabIndex={0} role="region" aria-label={`Exact current-raster cells for ${detail.scenario.scenario_id}`}><table><thead><tr><th>Cell</th><th>Row</th><th>Column</th><th>X center</th><th>Y center</th><th>Baseline current</th><th>Selected current</th><th>Selected − baseline</th></tr></thead><tbody>{page.cells.map((cell) => <tr key={cell.linear_index}><td>{cell.linear_index + 1}</td><td>{cell.row + 1}</td><td>{cell.column + 1}</td><td className="mono">{cell.x_center.toPrecision(9)}</td><td className="mono">{cell.y_center.toPrecision(9)}</td><td className="mono">{currentText(cell.baseline_current)}</td><td className="mono">{currentText(cell.portfolio_current)}</td><td className="mono">{currentText(cell.portfolio_minus_baseline)}</td></tr>)}</tbody></table></div>
        <div className="professional-cell-pagination" aria-label="Exact current-raster table pages"><button className="table-button" type="button" disabled={!page.has_previous} onClick={() => onPage(Math.max(0, page.offset - page.limit))}>Previous {page.limit}</button><span>Offset {page.offset.toLocaleString()}</span><button className="table-button" type="button" disabled={!page.has_next} onClick={() => onPage(page.offset + page.limit)}>Next {page.limit}</button></div>
      </details>
      <p className="map-caption"><strong>Direct artifact lineage.</strong> Both rasters were rehashed before this view was returned. The compact visual samples exact cells; the paged table exposes exact retained values without sending local paths to the renderer.</p>
    </section>
  );
}

export function ProfessionalScenarioEvidence({ projectHandle, analysisId, portfolioId, scenarioIds, initialScenarioId, onEvidence }: ProfessionalScenarioEvidenceProps) {
  const [scenarioId, setScenarioId] = useState(initialScenarioId);
  const [cellOffset, setCellOffset] = useState(0);
  const [scrub, setScrub] = useState(50);
  const [detail, setDetail] = useState<ScenarioDetailResponse | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const requestSequence = useRef(0);
  const availableScenarioIds = useMemo(() => [...new Set(scenarioIds)], [scenarioIds]);

  useEffect(() => {
    const next = availableScenarioIds.includes(initialScenarioId) ? initialScenarioId : availableScenarioIds[0];
    if (next) setScenarioId(next);
    setCellOffset(0);
    setDetail(null);
  }, [availableScenarioIds, initialScenarioId, portfolioId]);

  useEffect(() => {
    if (!scenarioId) return;
    const sequence = ++requestSequence.current;
    setState("loading");
    setMessage("");
    void hostRequest<RpcPayload<unknown>>("connectivity.results.scenario_detail", {
      project_handle: projectHandle,
      analysis_id: analysisId,
      portfolio_id: portfolioId,
      scenario_id: scenarioId,
      cell_offset: cellOffset,
      cell_limit: CELL_PAGE_SIZE,
    }).then((response) => {
      if (sequence !== requestSequence.current) return;
      const parsed = ScenarioDetailResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== analysisId || parsed.portfolio_id !== portfolioId || parsed.scenario.scenario_id !== scenarioId) throw new Error("Scenario evidence belongs to a different immutable analysis scope.");
      setDetail(parsed);
      setState("ready");
    }).catch((error: unknown) => {
      if (sequence !== requestSequence.current) return;
      setDetail(null);
      setState("error");
      setMessage(hostErrorMessage(error, "The selected same-scenario evidence could not be resolved."));
    });
    return () => {
      if (requestSequence.current === sequence) requestSequence.current += 1;
    };
  }, [analysisId, cellOffset, portfolioId, projectHandle, scenarioId]);

  const changeScenario = (next: string) => {
    setScenarioId(next);
    setCellOffset(0);
  };

  return (
    <section className="professional-scenario-evidence" aria-labelledby="professional-scenario-evidence-heading">
      <div className="paper-panel professional-scenario-toolbar"><div><span className="section-kicker">Synchronized direct evidence</span><h2 id="professional-scenario-evidence-heading">One guild, focal set, hypothesis, and scenario at a time</h2><p>Changing the scenario updates the focal-pair matrix and current-map comparison together. Values are never pooled across ecological meanings.</p></div><label><span>Scenario shown in matrix and map</span><select value={scenarioId} onChange={(event) => changeScenario(event.target.value)}>{availableScenarioIds.map((item) => <option key={item} value={item}>{item}</option>)}</select></label></div>
      {state === "loading" && <div className="paper-panel scenario-detail-loading" role="status"><Icon name="evidence" /><p><strong>Resolving exact same-scenario evidence…</strong><span>Rehashing retained matrices and current maps before display.</span></p></div>}
      {state === "error" && <div className="paper-panel scenario-detail-error" role="alert"><Icon name="warning" /><p><strong>Scenario evidence unavailable</strong><span>{message}</span></p></div>}
      {detail && <>
        <section className="paper-panel table-panel focal-matrix professional-focal-matrix"><div className="panel-heading focal-matrix-heading"><div><span className="section-kicker">Exact focal-pair matrix</span><h2>{detail.scenario.guild_id} · {detail.scenario.resistance_hypothesis_id}</h2></div><span className="mono">{detail.scenario.focal_set_id} · {detail.scenario.future_condition} · {detail.scenario.maturity_horizon}</span></div><p className="matrix-interpretation"><strong>Reduction convention:</strong> positive means lower modeled effective resistance than this scenario’s own baseline. Raw resistance and reductions are never aggregated with another guild, focal set, grid, or hypothesis.</p><div className="table-scroll" tabIndex={0} role="region" aria-label={`Focal-pair direct evidence for ${detail.scenario.scenario_id}`}><table><thead><tr><th>Required pair</th><th>Baseline effective resistance (model units)</th><th>Selected effective resistance (model units)</th><th>Baseline-relative reduction</th><th>QA</th><th>Direct run and matrix lineage</th><th>Evidence</th></tr></thead><tbody>{detail.pair_evidence.map((pair) => <tr key={pair.pair_id}><td className="mono">{pair.pair_id}{!pair.required && <small> optional</small>}</td><td className="mono">{pair.baseline_effective_resistance.toPrecision(9)}</td><td className="mono">{pair.portfolio_effective_resistance.toPrecision(9)}</td><td className={pair.baseline_relative_reduction_percent < 0 ? "value-adverse" : "value-good"}>{pair.baseline_relative_reduction_percent > 0 ? "+" : ""}{pair.baseline_relative_reduction_percent.toFixed(3)}%</td><td>{detail.baseline_run.qa_status} / {detail.portfolio_run.qa_status}</td><td><details className="pair-lineage"><summary>Runs and hashes</summary><dl><div><dt>Baseline run</dt><dd className="mono">{pair.baseline_direct_run_id}</dd></div><div><dt>Baseline matrix</dt><dd className="mono">sha256:{pair.baseline_matrix_sha256}</dd></div><div><dt>Selected run</dt><dd className="mono">{pair.portfolio_direct_run_id}</dd></div><div><dt>Selected matrix</dt><dd className="mono">sha256:{pair.portfolio_matrix_sha256}</dd></div></dl></details></td><td><button className="table-button" type="button" onClick={() => onEvidence(pairEvidenceSelection(detail, pair))}>Trace focal pair</button></td></tr>)}</tbody></table></div><p className="table-note">{detail.claim_language} Model revision <span className="mono">{detail.model_revision_id}</span>.</p></section>
        <ProfessionalCurrentMap detail={detail} scrub={scrub} onScrub={setScrub} onPage={setCellOffset} onEvidence={onEvidence} />
        <p className="professional-scenario-limitations"><strong>Interpretation boundary:</strong> {detail.limitations.join(" ")}</p>
      </>}
    </section>
  );
}
