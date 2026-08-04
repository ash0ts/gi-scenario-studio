import { useId } from "react";
import type { RidgewayPortfolio } from "../types";

interface CorridorTransectProps {
  portfolio?: RidgewayPortfolio;
  scrub?: number;
  onScrub?: (value: number) => void;
  compact?: boolean;
  verified?: boolean;
}

export function CorridorTransect({ portfolio, scrub = 100, onScrub, compact = false, verified = false }: CorridorTransectProps) {
  const titleId = useId();
  const descriptionId = useId();
  const strength = Math.max(0.15, Math.min(1, scrub / 100));
  const isFlagged = portfolio?.roadMaskFlag ?? false;
  return (
    <section className={`transect-shell${compact ? " compact" : ""}`} aria-labelledby={titleId}>
      <div className="transect-heading">
        <div>
          <span className="section-kicker">Schematic study transect</span>
          <h2 id={titleId}>{portfolio ? portfolio.name : "Four cores across the Ridgeway highway"}</h2>
        </div>
        {portfolio && <span className={`review-flag ${isFlagged ? "is-flagged" : "is-clear"}`}>{isFlagged ? "Road-mask review" : "No configured mask flag"}</span>}
      </div>
      <svg className="transect-map" viewBox="0 0 1000 270" role="img" aria-labelledby={`${titleId} ${descriptionId}`}>
        <desc id={descriptionId}>A schematic, not-to-scale orientation drawing with four synthetic habitat cores, a central highway, candidate-site markers, and a road review mask. Its connecting lines are not Circuitscape current rasters or modeled animal movement.</desc>
        <defs>
          <pattern id="topo-lines" width="100" height="45" patternUnits="userSpaceOnUse">
            <path d="M-10 28C18 8 54 49 110 18" fill="none" stroke="#879a8f" strokeWidth="1" opacity=".32" />
            <path d="M-12 41C28 17 57 57 112 31" fill="none" stroke="#879a8f" strokeWidth="1" opacity=".22" />
          </pattern>
        </defs>
        <rect width="1000" height="270" fill="#edf2ee" />
        <rect width="1000" height="270" fill="url(#topo-lines)" />
        <path className="core core-a" d="M38 98C58 40 168 20 238 65c45 30 34 103-20 128-64 29-165 4-180-45-5-16-5-33 0-50Z" />
        <path className="core core-b" d="M242 186c14-54 83-88 144-64 51 20 49 83 9 112-52 37-170 17-153-48Z" />
        <path className="core core-c" d="M630 64c35-47 131-42 167 10 31 46-15 96-70 105-57 9-133-15-131-64 1-18 13-35 34-51Z" />
        <path className="core core-d" d="M777 182c27-49 117-75 164-34 46 39 7 98-46 108-63 13-144-18-118-74Z" />
        <g className="highway">
          <path d="M482-10 458 280" />
          <path d="M538-10 514 280" />
          <path className="centerline" d="M510-10 486 280" />
        </g>
        <rect className={`risk-mask${isFlagged ? " flagged" : ""}`} x="433" y="64" width="134" height="143" rx="3" />
        <text x="443" y="57" className="map-label">ROAD REVIEW MASK</text>
        <g className="candidate-sites">
          {[82, 148, 230, 345, 606, 710, 844, 917].map((x, index) => <circle key={x} cx={x} cy={index % 2 ? 153 : 104} r="8" />)}
        </g>
        <g className="schematic-links" style={{ opacity: strength }}>
          <path d="M154 100C301 68 349 102 485 110s276-49 400 28" />
          <path d="M139 128c150 64 250 16 356 28s212 86 347 55" />
          <path d="M312 190c81-37 142-55 185-24 60 42 109 22 176-54" />
        </g>
        {portfolio?.role === "carry-forward" || portfolio?.role === "over-budget" ? (
          <g className="crossing-concept" aria-hidden="true">
            <rect x="472" y="101" width="57" height="27" rx="2" />
            <path d="M466 95v39M535 95v39" />
          </g>
        ) : null}
        <g className="core-labels">
          <text x="105" y="89">CORE A</text><text x="299" y="183">CORE B</text>
          <text x="675" y="88">CORE C</text><text x="846" y="200">CORE D</text>
        </g>
      </svg>
      {onScrub && (
        <div className="scrub-control">
          <span>Context</span>
          <label>
            <span className="sr-only">Adjust selected concept emphasis in the schematic orientation drawing</span>
            <input type="range" min="0" max="100" value={scrub} onChange={(event) => onScrub(Number(event.target.value))} />
          </label>
          <span>{portfolio?.id ?? "Selected concept"}</span>
          <output>{scrub}% concept emphasis</output>
        </div>
      )}
      <p className="map-caption"><strong>Schematic only.</strong> Shapes and connecting lines orient the decision story; they are not current rasters, direct-run values, or animal-use predictions. {verified ? "Verified metrics and retained map artifacts remain available through the Evidence Rail." : "No solver evidence is represented by this drawing."}</p>
    </section>
  );
}
