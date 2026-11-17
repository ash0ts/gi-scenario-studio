import { useId, useMemo } from "react";

import type { RidgewayFixture, RidgewayPortfolio, RidgewayScenarioResult } from "../types";
import { Icon } from "./Icon";

interface CurrentRasterScrubberProps {
  fixture: RidgewayFixture;
  portfolio: RidgewayPortfolio;
  result: RidgewayScenarioResult;
  scrub: number;
  onScrub: (value: number) => void;
  onTrace: () => void;
}

function currentFill(value: number | null, maximum: number): string {
  if (value === null) return "url(#nodata-pattern)";
  const intensity = maximum > 0 ? Math.log1p(value) / Math.log1p(maximum) : 0;
  const start = [226, 237, 231];
  const end = [22, 125, 141];
  const color = start.map((channel, index) => Math.round(channel + (end[index] - channel) * intensity));
  return `rgb(${color.join(",")})`;
}

function currentText(value: number | null): string {
  if (value === null) return "NODATA";
  if (value === 0) return "0";
  return Math.abs(value) < 0.001 || Math.abs(value) >= 10_000
    ? value.toExponential(4)
    : value.toPrecision(6);
}

export function CurrentRasterScrubber({ fixture, portfolio, result, scrub, onScrub, onTrace }: CurrentRasterScrubberProps) {
  const titleId = useId();
  const descriptionId = useId();
  const idPrefix = useId().replaceAll(":", "");
  const baseline = fixture.currentRasters.find((item) => item.runId === result.baselineMapRunId);
  const selected = fixture.currentRasters.find((item) => item.runId === result.portfolioMapRunId);
  const mask = fixture.reviewMask;
  const rows = useMemo(() => {
    if (!baseline || !selected) return [];
    return baseline.cells.map((baselineValue, index) => ({
      index,
      row: Math.floor(index / baseline.width) + 1,
      column: index % baseline.width + 1,
      baselineValue,
      selectedValue: selected.cells[index] ?? null,
      inReviewMask: mask?.cells[index] === 1,
    }));
  }, [baseline, mask, selected]);

  if (!baseline || !selected || !mask || baseline.width !== selected.width || baseline.height !== selected.height) {
    return (
      <section className="paper-panel blocked-workspace" role="alert">
        <Icon name="lock" />
        <div><h2>Direct current-raster comparison unavailable</h2><p>The verified projection does not contain the exact baseline, selected-portfolio, and review-mask grids for this scenario.</p></div>
      </section>
    );
  }

  const cellSize = 14;
  const width = baseline.width * cellSize;
  const height = baseline.height * cellSize;
  const divider = width * Math.max(0, Math.min(100, scrub)) / 100;
  const maximum = Math.max(baseline.maximumCurrent, selected.maximumCurrent);
  const baselineClip = `${idPrefix}-baseline-clip`;
  const selectedClip = `${idPrefix}-selected-clip`;
  const nodataPattern = `${idPrefix}-nodata-pattern`;
  const mapLabel = `${scrub}% baseline width · ${100 - scrub}% selected-portfolio width`;

  const cells = (values: Array<number | null>, prefix: string) => values.map((value, index) => {
    const row = Math.floor(index / baseline.width);
    const column = index % baseline.width;
    return <rect key={`${prefix}-${index}`} x={column * cellSize} y={row * cellSize} width={cellSize} height={cellSize} fill={value === null ? `url(#${nodataPattern})` : currentFill(value, maximum)} />;
  });

  return (
    <section className="current-raster-shell" aria-labelledby={titleId}>
      <div className="current-raster-heading">
        <div><span className="section-kicker">Direct current-raster comparison</span><h2 id={titleId}>Baseline ↔ {portfolio.name}</h2><p>{result.scenarioId}</p></div>
        <button className="secondary-button" type="button" onClick={onTrace}><Icon name="evidence" /> Trace retained map evidence</button>
      </div>
      <div className="current-raster-layout">
        <div className="current-raster-figure">
          <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${titleId} ${descriptionId}`} preserveAspectRatio="xMidYMid meet">
            <desc id={descriptionId}>A split comparison of exact retained Circuitscape cumulative-current cells for the within-scenario baseline and selected teaching portfolio. Outlined cells intersect the configured diagnostic road-review mask. This is not observed or predicted animal use.</desc>
            <defs>
              <pattern id={nodataPattern} width="6" height="6" patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#d9dfda" /><path d="M-1 1 1-1M0 6 6 0M5 7 7 5" stroke="#9ba7a0" strokeWidth="1" /></pattern>
              <clipPath id={baselineClip}><rect x="0" y="0" width={divider} height={height} /></clipPath>
              <clipPath id={selectedClip}><rect x={divider} y="0" width={Math.max(0, width - divider)} height={height} /></clipPath>
            </defs>
            <g clipPath={`url(#${baselineClip})`}>{cells(baseline.cells, "baseline")}</g>
            <g clipPath={`url(#${selectedClip})`}>{cells(selected.cells, "selected")}</g>
            <g className="current-raster-mask" aria-hidden="true">{mask.cells.map((value, index) => value === 1 ? <rect key={index} x={(index % mask.width) * cellSize + 1} y={Math.floor(index / mask.width) * cellSize + 1} width={cellSize - 2} height={cellSize - 2} /> : null)}</g>
            <line className="current-raster-divider" x1={divider} x2={divider} y1="0" y2={height} />
          </svg>
          <div className="current-raster-key" aria-hidden="true"><span>Lower modeled current</span><i /><span>Higher modeled current</span><b>Outlined = review mask</b></div>
          <label className="raster-scrub-control">
            <span>Move the baseline / selected divider</span>
            <input type="range" min="0" max="100" value={scrub} aria-label="Move the baseline / selected divider" aria-valuetext={mapLabel} onChange={(event) => onScrub(Number(event.target.value))} />
            <output>{mapLabel}</output>
          </label>
        </div>
        <div className="current-raster-facts">
          <p><strong>What changed in the configured road mask?</strong> {result.reviewMaskChangePct.toFixed(2)}% diagnostic current redistribution. This flags review; it is not a movement, crossing-use, collision, or population claim.</p>
          <dl>
            <div><dt>Grid</dt><dd>{baseline.width} × {baseline.height} exact cells</dd></div>
            <div><dt>Visual scale</dt><dd>Shared log1p scale; retained values remain unchanged</dd></div>
            <div><dt>Baseline range</dt><dd className="mono">{currentText(baseline.minimumCurrent)}–{currentText(baseline.maximumCurrent)} diagnostic current units</dd></div>
            <div><dt>Selected range</dt><dd className="mono">{currentText(selected.minimumCurrent)}–{currentText(selected.maximumCurrent)} diagnostic current units</dd></div>
            <div><dt>Baseline run</dt><dd className="mono">{baseline.runId}</dd></div>
            <div><dt>Baseline raster</dt><dd className="mono">sha256:{baseline.sha256}</dd></div>
            <div><dt>Selected run</dt><dd className="mono">{selected.runId}</dd></div>
            <div><dt>Selected raster</dt><dd className="mono">sha256:{selected.sha256}</dd></div>
            <div><dt>NODATA topology</dt><dd className="mono">sha256:{selected.nodataTopologySha256}</dd></div>
          </dl>
        </div>
      </div>
      <details className="current-raster-table">
        <summary>Inspect all {rows.length} exact raster cells</summary>
        <p>Values are retained Circuitscape cumulative-current diagnostics. “Selected − baseline” is a cell-level arithmetic aid, not an ecological outcome.</p>
        <div className="table-scroll" tabIndex={0} role="region" aria-label={`Scrollable exact current-raster cells for ${result.scenarioId}`}>
          <table><thead><tr><th>Row</th><th>Column</th><th>Baseline current (diagnostic units)</th><th>Selected current (diagnostic units)</th><th>Selected − baseline (diagnostic units)</th><th>Review mask</th></tr></thead><tbody>{rows.map((cell) => <tr key={cell.index}><td>{cell.row}</td><td>{cell.column}</td><td className="mono">{currentText(cell.baselineValue)}</td><td className="mono">{currentText(cell.selectedValue)}</td><td className="mono">{cell.baselineValue === null || cell.selectedValue === null ? "NODATA" : currentText(cell.selectedValue - cell.baselineValue)}</td><td>{cell.inReviewMask ? "Inside" : "Outside"}</td></tr>)}</tbody></table>
        </div>
      </details>
      <p className="map-caption"><strong>Direct diagnostic evidence.</strong> The split view renders the exact compact arrays bound to the two named current-raster SHA-256 hashes. Color uses one shared visual transform and never changes the retained values.</p>
    </section>
  );
}
