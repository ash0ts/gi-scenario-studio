import { useState } from "react";
import { alternatives, evaluatedAlternatives, guidedFixture } from "../data/demo";
import { attestationsComplete, calculateDesignCapacity, nondominatedIds } from "../lib/analysis";
import type { ProjectKind, Readiness, StepId } from "../types";
import { Icon } from "./Icons";

interface ScreenProps {
  kind: ProjectKind;
  readiness: Readiness;
  onEvidence: (id: string) => void;
  onStep: (step: StepId) => void;
}

export function ModelScreen({ kind, readiness, onEvidence, onStep }: ScreenProps) {
  return kind === "conceptual" ? (
    <ConceptModelSetupPreview readiness={readiness} />
  ) : (
    <ExistingModelCheck kind={kind} onEvidence={onEvidence} onStep={onStep} />
  );
}

function PageHeading({ eyebrow, title, copy, action }: { eyebrow: string; title: string; copy: string; action?: React.ReactNode }) {
  return (
    <header className="page-heading">
      <div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{copy}</p></div>
      {action && <div className="heading-action">{action}</div>}
    </header>
  );
}

function ExistingModelCheck({ kind, onEvidence, onStep }: Pick<ScreenProps, "kind" | "onEvidence" | "onStep">) {
  const [checked, setChecked] = useState(kind === "demo");
  const [showDetails, setShowDetails] = useState(false);

  const isGuided = kind === "demo";
  const findings = isGuided ? [
    { label: "Model syntax", detail: "Self-contained fixture · parser accepted", state: "Pass", tone: "pass" },
    { label: "External files", detail: "No dependencies", state: "Pass", tone: "pass" },
    { label: "Baseline execution", detail: "4 synthetic storms · accepted QA", state: "Pass", tone: "pass" },
    { label: "LID controls", detail: "8 native types represented · 4 bounded slots used", state: "Demo", tone: "review" },
    { label: "Reference comparison", detail: "Not supplied · readiness remains DEMO", state: "Notice", tone: "notice" },
  ] : [
    { label: "Project bundle", detail: "Choose a bundle to inspect", state: "Pending", tone: "review" },
    { label: "Model syntax", detail: "No model parsed", state: "Pending", tone: "review" },
    { label: "External files", detail: "Dependency closure not inspected", state: "Pending", tone: "review" },
    { label: "Baseline execution", detail: "No exact job exists", state: "Pending", tone: "review" },
    { label: "Reference comparison", detail: "No approved reference attached", state: "Notice", tone: "notice" },
  ];

  return (
    <div className="screen-stack">
      <PageHeading
        eyebrow="Model / preflight"
        title={isGuided ? "A small teaching watershed, fully traced" : "Check the model before changing it"}
        copy={isGuided ? "This self-authored CC0 network and its four synthetic storms back the exact guided result set." : "Choose a project bundle before any compatibility, readiness, or hydraulic claim is shown."}
        action={isGuided
          ? <button className="secondary-button" onClick={() => onEvidence("guided-model")}><Icon name="trace" /> Inspect source record</button>
          : <button className="secondary-button" disabled title="Project selection is wired through the packaged desktop worker"><Icon name="folder" /> Select bundle in desktop build</button>}
      />

      <section className="model-file-strip">
        <div className="file-glyph"><Icon name="file" /></div>
        <div><strong>{isGuided ? "reference.inp" : "No bundle selected"}</strong><span>{isGuided ? "Executed with EPA SWMM 5.2.4 · CFS · self-contained" : "ZIP bundle or INP with dependencies required"}</span></div>
        <div className="file-hash"><span>Model fingerprint</span><code>{isGuided ? `${guidedFixture.source_model_sha256 ?? guidedFixture.baseline.runs[0].hashes.input_sha256}`.slice(0, 12) + "…" : "pending"}</code></div>
        <button className="text-button" onClick={() => setShowDetails(!showDetails)}>{showDetails ? "Hide" : "Inspect"} bundle <Icon name="chevron" /></button>
      </section>

      {showDetails && (
        <section className="inline-detail" aria-live="polite">
          <div><span>Sources</span><strong>1 base model</strong></div>
          <div><span>Storm variants</span><strong>{isGuided ? "4 synthetic cases" : "Not inspected"}</strong></div>
          <div><span>Dependencies</span><strong>{isGuided ? "None" : "Not inspected"}</strong></div>
          <div><span>Unsafe paths</span><strong>{isGuided ? "None" : "Not inspected"}</strong></div>
        </section>
      )}

      <div className="split-grid model-check-grid">
        <section className="paper-panel">
          <div className="panel-heading"><div><p className="eyebrow">Readiness checklist</p><h2>{isGuided ? "5 checks recorded" : "Waiting for a bundle"}</h2></div><span className="score-ring">{isGuided ? "5/5" : "0/5"}</span></div>
          <div className="finding-list">
            {findings.map((finding) => (
              <button key={finding.label} disabled={!isGuided} onClick={() => onEvidence(finding.label === "Baseline execution" ? "qa-continuity" : "guided-model")} className="finding-row">
                <span className={`finding-icon ${finding.tone}`}><Icon name={finding.tone === "pass" ? "check" : "info"} /></span>
                <span><strong>{finding.label}</strong><small>{finding.detail}</small></span>
                <span className={`mini-state ${finding.tone}`}>{finding.state}</span>
                <Icon name="chevron" />
              </button>
            ))}
          </div>
        </section>

        {isGuided ? <section className="paper-panel qa-panel">
          <div className="panel-heading"><div><p className="eyebrow">Baseline evidence</p><h2>Execution QA</h2></div><span className="exact-stamp"><Icon name="shield" /> Exact</span></div>
          <div className="qa-figure">
            <div className="tolerance-bar"><span style={{ left: "46%" }} /><i /><i /></div>
            <div className="tolerance-labels"><span>−2.0%</span><strong>0.079% max</strong><span>+2.0%</span></div>
          </div>
          <button className="metric-callout" onClick={() => onEvidence("qa-continuity")}>
            <span><small>Largest baseline continuity error</small><strong>0.079%</strong></span>
            <span className="pass-copy"><Icon name="check" /> Within policy</span>
          </button>
          <p className="fine-print">This is solver QA, not a reference match. All four baseline runs were accepted; the project remains DEMO.</p>
        </section> : <section className="paper-panel qa-panel empty-panel"><Icon name="folder"/><h2>No baseline evidence yet</h2><p>Select a complete bundle, inspect dependencies, and run the untouched storms before continuing.</p></section>}
      </div>

      <div className="screen-footer-action">
        <label className="review-check"><input type="checkbox" checked={checked} disabled={!isGuided} onChange={(event) => setChecked(event.target.checked)} /><span><Icon name="check" /></span>I reviewed the preflight findings</label>
        <button className="primary-button" disabled={!checked || !isGuided} onClick={() => onStep("storms")}>{isGuided ? "Review synthetic storm cases" : "Select a project bundle first"} <Icon name="arrow" /></button>
      </div>
    </div>
  );
}

function ConceptModelSetupPreview({ readiness }: Pick<ScreenProps, "readiness">) {
  const stages = [
    ["Study area", "Boundary, outlets, CRS, and source ownership"],
    ["Source data", "Provider, license, vintage, resolution, extent, and checksum"],
    ["Drainage concept", "Generated topology followed by focused manual repair"],
    ["Parameters", "Value, units, origin, formula, confidence, notes, and review state"],
    ["Readiness", "Topology, QA, sensitivity, assumptions, and explicit attestation"],
  ];
  return (
    <div className="screen-stack">
      <PageHeading eyebrow="Model starter / source-empty" title="Begin with evidence, not a guessed network" copy="No study area, GIS layer, provider download, or hydraulic object has been loaded. The packaged desktop worker must record each source before model generation can begin." />
      <div className="conceptual-notice"><Icon name="warning" /><div><strong>Conceptual planning model · {readiness}</strong><span>Not for design, permitting, flood-risk determination, or representation of existing drainage infrastructure.</span></div></div>
      <div className="builder-grid">
        <section className="paper-panel empty-panel">
          <Icon name="map" />
          <h2>No study boundary yet</h2>
          <p>Use a GeoJSON, GeoPackage, shapefile bundle, drawn boundary, or confirmed coordinate search in the packaged desktop application.</p>
          <div className="rail-notice"><Icon name="info" /><p>Open-data access remains off until a user approves a named provider, dataset, extent, license, and download.</p></div>
        </section>
        <section className="paper-panel source-receipt">
          <div className="panel-heading"><div><p className="eyebrow">Required source receipt</p><h2>Nothing is silently inferred</h2></div><Icon name="trace" /></div>
          <dl><div><dt>Coordinate system</dt><dd>Required before distances, areas, slopes, or elevations are derived.</dd></div><div><dt>Candidate outlets</dt><dd>Must be supplied or confirmed by the user before topology review.</dd></div><div><dt>Unknown engineering values</dt><dd>Remain assumed and visible; they never become observed through generation.</dd></div></dl>
        </section>
      </div>
      <section className="paper-panel ledger-panel">
        <div className="panel-heading"><div><p className="eyebrow">Authoritative desktop workflow</p><h2>Five review gates before alternatives</h2></div><span className="mini-state review">No records yet</span></div>
        <div className="finding-list">{stages.map(([title, detail], index) => <div className="finding-row" key={title}><span className="finding-icon review">{index + 1}</span><span><strong>{title}</strong><small>{detail}</small></span><span className="mini-state review">Pending</span></div>)}</div>
      </section>
      <div className="screen-footer-action"><span className="footer-progress"><Icon name="info" /> Source selection and model generation require the packaged worker</span><button className="primary-button" disabled><Icon name="folder" /> Choose source data in desktop build</button></div>
    </div>
  );
}

const conceptTabs = ["Study area", "Source data", "Drainage concept", "Parameters", "Readiness"] as const;

function ConceptModelBuilder({ readiness, onEvidence, onStep }: Omit<ScreenProps, "kind">) {
  const [tab, setTab] = useState<(typeof conceptTabs)[number]>("Study area");
  const [boundaryDrawn, setBoundaryDrawn] = useState(true);
  const [attestations, setAttestations] = useState([false, false, false, false, false]);
  const currentIndex = conceptTabs.indexOf(tab);

  const next = () => {
    if (currentIndex < conceptTabs.length - 1) setTab(conceptTabs[currentIndex + 1]);
    else onStep("storms");
  };

  return (
    <div className="screen-stack">
      <PageHeading eyebrow="Model starter / conceptual" title="Build the drainage concept in the open" copy="Review the study boundary, source data, generated drainage network, and every planning-level assumption before running scenarios." />

      <div className="conceptual-notice"><Icon name="warning" /><div><strong>Conceptual planning model</strong><span>Not for design, permitting, flood-risk determination, or representation of existing drainage infrastructure.</span></div></div>

      <div className="substep-tabs" role="tablist" aria-label="Concept model steps">
        {conceptTabs.map((item, index) => (
          <button key={item} role="tab" aria-selected={tab === item} className={tab === item ? "active" : index < currentIndex ? "complete" : ""} onClick={() => setTab(item)}>
            <span>{index < currentIndex ? <Icon name="check" /> : index + 1}</span>{item}
          </button>
        ))}
      </div>

      {tab === "Study area" && (
        <div className="builder-grid">
          <section className="paper-panel builder-form">
            <p className="eyebrow">Study area</p><h2>Define the place and outlet</h2>
            <label>Project name<input defaultValue="Mill Creek planning study" /></label>
            <div className="form-row"><label>Coordinate system<input defaultValue="EPSG:26917 · NAD83 / UTM 17N" /></label><label>Area<input defaultValue="1.84 km²" readOnly /></label></div>
            <label>Primary outlet<select defaultValue="draw"><option value="draw">Outlet O-01 · user confirmed</option></select></label>
            <div className="button-row"><button className="secondary-button"><Icon name="upload" /> Upload boundary</button><button className={`secondary-button ${boundaryDrawn ? "selected" : ""}`} onClick={() => setBoundaryDrawn(!boundaryDrawn)}><Icon name="map" /> {boundaryDrawn ? "Boundary drawn" : "Draw boundary"}</button></div>
          </section>
          <StudyMap editing={boundaryDrawn} onEvidence={() => onEvidence("J-104")} />
        </div>
      )}

      {tab === "Source data" && (
        <div className="builder-grid">
          <section className="paper-panel builder-form">
            <p className="eyebrow">Input route</p><h2>Use your files or inspect open data</h2>
            <div className="segmented"><button className="active">Open data</button><button>My files</button></div>
            <div className="provider-card"><div className="provider-icon">3D</div><div><strong>USGS 3DEP elevation</strong><span>1/3 arc-second · 2023 mosaic</span></div><span className="mini-state pass">Cached</span></div>
            <div className="provider-card"><div className="provider-icon">OS</div><div><strong>OpenStreetMap streets</strong><span>ODbL · snapshot 2026-07-29</span></div><span className="mini-state pass">Cached</span></div>
            <div className="provider-card"><div className="provider-icon">NL</div><div><strong>NLCD land cover</strong><span>30 m · 2021 release</span></div><span className="mini-state review">Review</span></div>
            <button className="text-button"><Icon name="plus" /> Add soils or local asset data</button>
          </section>
          <section className="paper-panel source-receipt">
            <div className="panel-heading"><div><p className="eyebrow">Download receipt</p><h2>What these data influence</h2></div><Icon name="trace" /></div>
            <dl><div><dt>Elevation</dt><dd>Catchment slope, surface elevations, initial network grade</dd></div><div><dt>Street centerlines</dt><dd>Candidate drainage network alignment only</dd></div><div><dt>Land cover</dt><dd>Planning-level imperviousness estimate</dd></div></dl>
            <div className="rail-notice"><Icon name="info" /><p>Downloaded assets stay local and are added to the project manifest with source, license, extent, and checksum.</p></div>
          </section>
        </div>
      )}

      {tab === "Drainage concept" && (
        <div className="builder-grid wide-map-grid">
          <StudyMap network editing onEvidence={() => onEvidence("J-104")} />
          <section className="paper-panel object-inspector">
            <p className="eyebrow">Selected conduit</p><h2>C-014</h2><span className="assumption-chip"><Icon name="warning" /> 2 assumed values</span>
            <label>Diameter <span>in</span><input type="number" defaultValue="24" /></label>
            <label>Upstream invert <span>ft</span><input type="number" defaultValue="99.6" /></label>
            <label>Downstream invert <span>ft</span><input type="number" defaultValue="98.4" /></label>
            <label>Manning roughness<input type="number" step="0.001" defaultValue="0.013" /></label>
            <div className="object-actions"><button className="secondary-button">Flag uncertain</button><button className="secondary-button">Remove link</button></div>
            <p className="fine-print">Synthetic alignment from street centerline. Confirm against utility records before engineer review.</p>
          </section>
        </div>
      )}

      {tab === "Parameters" && (
        <section className="paper-panel ledger-panel">
          <div className="panel-heading"><div><p className="eyebrow">Assumption ledger</p><h2>42 values traced · 7 need review</h2></div><button className="secondary-button"><Icon name="download" /> Export ledger</button></div>
          <div className="table-wrap"><table><thead><tr><th>Parameter</th><th>Value</th><th>Origin</th><th>Confidence</th><th>Review</th></tr></thead><tbody>
            <tr><td>Subcatchment imperviousness</td><td className="mono">31–72%</td><td>Derived · NLCD</td><td><span className="mini-state review">Inferred</span></td><td><button className="table-action">Review 12</button></td></tr>
            <tr><td>Conduit diameter</td><td className="mono">18–36 in</td><td>Planning default</td><td><span className="mini-state notice">Assumed</span></td><td><button className="table-action">Review 5</button></td></tr>
            <tr><td>Surface elevation</td><td className="mono">96.2–128.4 ft</td><td>Derived · 3DEP</td><td><span className="mini-state pass">Confirmed</span></td><td><Icon name="check" className="table-check" /></td></tr>
            <tr><td>Conduit roughness</td><td className="mono">0.013</td><td>Manual default</td><td><span className="mini-state notice">Assumed</span></td><td><button className="table-action">Review 2</button></td></tr>
          </tbody></table></div>
        </section>
      )}

      {tab === "Readiness" && (
        <div className="split-grid readiness-grid">
          <section className="paper-panel">
            <div className="panel-heading"><div><p className="eyebrow">Model readiness checklist</p><h2>Ready for conceptual screening</h2></div><span className="readiness-seal"><Icon name="check" /></span></div>
            {[
              ["Connected drainage network", "32 nodes · 31 links · 1 confirmed outlet", true],
              ["Subcatchment routing", "16 of 16 routed to a network object", true],
              ["Hydraulic feasibility", "No negative slopes or missing cross-sections", true],
              ["Assumption review", "7 assumptions require explicit acceptance", false],
              ["Reference comparison", "No approved reference model attached", false],
            ].map(([title, detail, pass]) => <div className="readiness-row" key={String(title)}><span className={pass ? "pass" : "review"}><Icon name={pass ? "check" : "info"} /></span><div><strong>{title}</strong><small>{detail}</small></div></div>)}
          </section>
          <section className="paper-panel attestation-panel">
            <p className="eyebrow">Readiness status</p><h2>{readiness.replace("_", " ")}</h2>
            <p>This model can support exploratory alternatives comparison when its assumptions and limitations remain visible.</p>
            <div className="attestation-list">
              {["I reviewed the generated topology and outlet.", "I reviewed parameter sources and derivations.", "I reviewed the model’s hydraulic feasibility checks.", "I explicitly accept the 7 unresolved assumed values for screening.", "I understand this remains an uncalibrated planning model."].map((label, index) => (
                <label className="review-check" key={label}><input type="checkbox" checked={attestations[index]} onChange={() => setAttestations(current => current.map((value, itemIndex) => itemIndex === index ? !value : value))} /><span><Icon name="check" /></span>{label}</label>
              ))}
            </div>
            <button className="secondary-button" disabled title="The desktop worker must persist readiness attestations"><Icon name="shield" /> {attestationsComplete(attestations) ? "Worker attestation required" : "Complete every attestation"}</button>
            <p className="fine-print">Engineer review requires confirmation of topology, parameters, storm cases, and every unresolved assumption. It does not make the model calibrated or reference-matched.</p>
          </section>
        </div>
      )}

      <div className="screen-footer-action">
        <span className="footer-progress">{currentIndex + 1} of {conceptTabs.length} · {tab}</span>
        <button className="primary-button" onClick={next}>{currentIndex === conceptTabs.length - 1 ? "Review storm cases" : `Continue to ${conceptTabs[currentIndex + 1]}`} <Icon name="arrow" /></button>
      </div>
    </div>
  );
}

function StudyMap({ editing = false, network = false, onEvidence }: { editing?: boolean; network?: boolean; onEvidence: () => void }) {
  return (
    <section className="study-map paper-panel" aria-label="Schematic study area">
      <div className="map-toolbar"><span><Icon name="map" /> {network ? "Drainage concept" : "Study area"}</span><div><button aria-label="Zoom in">+</button><button aria-label="Zoom out">−</button></div></div>
      <svg viewBox="0 0 680 420" role="img" aria-labelledby="study-map-title">
        <title id="study-map-title">Schematic Mill Creek study area and conceptual drainage network</title>
        <defs><pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#d8dfdc" strokeWidth=".7" /></pattern></defs>
        <rect width="680" height="420" fill="url(#grid)" />
        <path d="M58 62 180 35 285 78 408 55 602 108 632 232 548 363 330 387 165 346 72 251Z" fill="#e3efea" stroke="#08766f" strokeWidth="3" strokeDasharray={editing ? "8 6" : undefined} />
        <path d="M40 176c110-45 165 52 278 3s177-23 330 18M115 30c40 100 6 160 80 250s128 80 176 140M470 38c-13 65 21 94 88 142s22 133 77 176" fill="none" stroke="#fff" strokeWidth="17" opacity=".8" />
        <path d="M175 91 236 149 310 168 381 221 470 242 552 318" fill="none" stroke="#2f6f9f" strokeWidth="4" />
        <path d="M116 179 236 149M222 291l159-70M446 124l24 118" fill="none" stroke="#2f6f9f" strokeWidth="3" />
        {[[175,91],[236,149],[310,168],[381,221],[470,242],[552,318],[116,179],[222,291],[446,124]].map(([x,y], index) => <circle key={index} cx={x} cy={y} r={index === 3 ? 9 : 6} fill={index === 3 ? "#a96315" : "#f5f7f6"} stroke="#17313a" strokeWidth="3" />)}
        <g role="button" tabIndex={0} aria-label="Inspect junction J-104" onClick={onEvidence} onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && onEvidence()} className="svg-focusable">
          <circle cx="381" cy="221" r="18" fill="transparent" /><text x="394" y="214">J-104</text>
        </g>
        <circle cx="552" cy="318" r="10" fill="#17313a" /><text x="568" y="324">O-01</text>
      </svg>
      <div className="map-status"><span><i className="legend-boundary" /> Study boundary</span><span><i className="legend-network" /> Conceptual drainage</span><span><i className="legend-review" /> Needs review</span><b>Schematic · EPSG:26917</b></div>
    </section>
  );
}

export function StormsScreen({ isGuided, onEvidence, onStep }: Pick<ScreenProps, "onEvidence" | "onStep"> & { isGuided: boolean }) {
  const storms = isGuided ? guidedFixture.storms : [];
  const plottedStorm = guidedFixture.storms[3];
  const maximumIntensity = Math.max(...plottedStorm.intensities_inches_per_hour);
  return <div className="screen-stack">
    <PageHeading eyebrow={`Storms / ${storms.length} cases`} title="Test the same decision under every storm" copy={isGuided ? "All four cases are explicitly synthetic teaching hyetographs. They carry no recurrence interval or regulatory authority." : "Add a storm case before configuring an analysis. Source, role, and authority status will travel with every exact result."} action={<button className="secondary-button" disabled title="Storm editing requires an authoritative desktop project"><Icon name="plus" /> Add storm case in desktop build</button>} />
    <section className="paper-panel storm-panel">
      <div className="table-wrap"><table><thead><tr><th>Storm case</th><th>Role</th><th>Depth</th><th>Status</th><th>Source</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
        {storms.map((storm, index) => <tr key={storm.storm_id}><td><button className="cell-link" onClick={() => onEvidence(`storm:${storm.storm_id}`)}><span className="storm-swatch" style={{ opacity: .45 + index * .16 }} /><strong>{storm.name}</strong></button></td><td>{index === 3 ? "Stress" : "Teaching"}</td><td className="mono">{storm.depth_inches.toFixed(2)} in</td><td><span className="mini-state review">User-defined</span></td><td>Self-authored CC0</td><td><button className="icon-button" aria-label={`Inspect ${storm.name}`} onClick={() => onEvidence(`storm:${storm.storm_id}`)}><Icon name="chevron" /></button></td></tr>)}
        {!isGuided && <tr><td colSpan={6}><div className="table-empty"><strong>No storm cases</strong><span>Add a model-linked storm variant to continue.</span></div></td></tr>}
      </tbody></table></div>
    </section>
    <div className="split-grid">
      <section className="paper-panel storm-hyetograph"><div className="panel-heading"><div><p className="eyebrow">Intensity profile</p><h2>{isGuided ? plottedStorm.name : "No storm selected"}</h2></div><span className="mini-state review">Synthetic</span></div>{isGuided && <><svg viewBox="0 0 600 180" role="img" aria-label="Synthetic stress storm intensity hyetograph">{plottedStorm.intensities_inches_per_hour.map((intensity,index)=>{const height=150*intensity/maximumIntensity;return <rect key={index} x={20+index*23} y={168-height} width="15" height={height} fill={intensity===maximumIntensity?"#08766f":"#8db8c7"}/>})}</svg><div className="axis-label"><span>0 hr</span><span>1 hr</span><span>2 hr</span></div></>}</section>
      <section className="paper-panel"><p className="eyebrow">Cross-model check</p><h2>{isGuided ? "Storm-only differences confirmed" : "Waiting for storm variants"}</h2><div className="readiness-row"><span className={isGuided ? "pass" : "review"}><Icon name={isGuided ? "check" : "info"} /></span><div><strong>Topology unchanged</strong><small>{isGuided ? "Nodes, links, subcatchments, and LIDs match" : "Not evaluated"}</small></div></div><div className="readiness-row"><span className={isGuided ? "pass" : "review"}><Icon name={isGuided ? "check" : "info"} /></span><div><strong>Simulation settings aligned</strong><small>{isGuided ? "Routing, time step, and report objects match" : "Not evaluated"}</small></div></div><div className="readiness-row"><span className="review"><Icon name="info" /></span><div><strong>Authority status visible</strong><small>{isGuided ? "Every case is synthetic and user-defined" : "Required before exact analysis"}</small></div></div></section>
    </div>
    <div className="screen-footer-action"><span className="footer-progress">{isGuided ? "4 synthetic storms × every evaluated design" : "No storm cases configured"}</span><button className="primary-button" disabled={!isGuided} onClick={() => onStep("opportunities")}>{isGuided ? "Review GI opportunities" : "Add a storm case first"} <Icon name="arrow" /></button></div>
  </div>;
}

export function OpportunitiesScreen({ isGuided, onEvidence, onStep }: Pick<ScreenProps, "onEvidence" | "onStep"> & { isGuided: boolean }) {
  const [selected, setSelected] = useState(isGuided ? "S1_BIO" : "");
  const rows = isGuided ? guidedFixture.candidate_slots : [];
  const selectedSlot = rows.find((slot) => slot.slot_id === selected);
  const typeNames: Record<string, string> = { BC: "Bioretention cell", PP: "Permeable pavement", RG: "Rain garden", RB: "Rain barrel" };
  return <div className="screen-stack">
    <PageHeading eyebrow={`Opportunities / ${rows.length} candidate slots`} title="Search only where an engineer says GI can fit" copy={isGuided ? "The self-authored fixture holds every LID control parameter fixed. Only integer counts of additional identical units vary." : "Import or draw engineer-reviewed candidate slots before any search can run."} action={<button className="secondary-button" disabled title="Candidate import requires an authoritative desktop project"><Icon name="upload" /> Import candidate CSV in desktop build</button>} />
    <div className="opportunity-layout">
      <section className="paper-panel opportunity-map"><div className="map-toolbar"><span><Icon name="layers" /> Candidate deployment map</span><span className="mini-state review">Schematic</span></div><svg viewBox="0 0 600 410" role="img" aria-label="Schematic two-subcatchment teaching network and four GI slots"><rect width="600" height="410" fill="#edf1ef"/><path d="M70 140 275 205 510 285" stroke="#5d7680" strokeWidth="5" fill="none"/><path d="M70 140 275 205 510 285" stroke="#a9bcc2" strokeWidth="2" fill="none" strokeDasharray="7 7"/>{(isGuided ? [[145,125,"S1_BIO"],[205,225,"S1_PP"],[350,190,"S2_RG"],[430,300,"S2_RB"]] : []).map(([x,y,id])=><g key={String(id)} className="svg-focusable" role="button" tabIndex={0} aria-label={`Select ${id}`} onClick={()=>setSelected(String(id))} onKeyDown={(event)=>(event.key==="Enter"||event.key===" ")&&setSelected(String(id))}><circle cx={Number(x)} cy={Number(y)} r={selected===id?24:18} fill={selected===id?"#08766f":"#d8e9e3"} stroke="#08766f" strokeWidth="3"/><text x={Number(x)} y={Number(y)+4} textAnchor="middle" fill={selected===id?"white":"#17313a"}>{String(id).split("_")[1]}</text></g>)}</svg><div className="map-status"><span><i className="legend-candidate" /> Teaching candidate</span><span><i className="legend-network" /> Drainage link</span><b>Synthetic model coordinates</b></div></section>
      <section className="paper-panel candidate-inspector"><p className="eyebrow">Selected candidate</p><h2>{selectedSlot ? `${selectedSlot.slot_id} · ${selectedSlot.subcatchment_id}` : "No candidate selected"}</h2>{selectedSlot && <><span className="mini-state review">Demo approval only</span><dl><div><dt>LID control</dt><dd>{selectedSlot.lid_control} · {typeNames[selectedSlot.lid_type]}</dd></div><div><dt>Unit footprint</dt><dd className="mono">{selectedSlot.unit_area.toFixed(0)} ft²</dd></div><div><dt>Additional-unit range</dt><dd className="mono">{selectedSlot.min_additional_units}–{selectedSlot.max_additional_units}</dd></div><div><dt>From impervious</dt><dd className="mono">{selectedSlot.from_impervious_percent.toFixed(0)}%</dd></div><div><dt>Routes to</dt><dd>{selectedSlot.drain_to}</dd></div></dl></>}<button className="text-button" disabled={!isGuided} onClick={() => onEvidence("cost-basis")}>Inspect synthetic cost assumptions <Icon name="arrow" /></button></section>
    </div>
    <section className="paper-panel"><div className="panel-heading"><div><p className="eyebrow">Candidate ledger</p><h2>{isGuided ? "Four bounded integer decisions" : "No candidate decisions yet"}</h2></div><span className="mini-state review">Read only</span></div><div className="table-wrap"><table><thead><tr><th>Slot</th><th>Subcatchment</th><th>Control</th><th>Type</th><th>Additional units</th><th>Unit area</th></tr></thead><tbody>{rows.map(row=><tr key={row.slot_id} className={selected===row.slot_id?"selected-row":""} onClick={()=>setSelected(row.slot_id)}><td><button className="cell-link">{row.slot_id}</button></td><td className="mono">{row.subcatchment_id}</td><td>{row.lid_control}</td><td>{typeNames[row.lid_type]}</td><td className="mono">{row.min_additional_units}–{row.max_additional_units}</td><td className="mono">{row.unit_area.toFixed(0)} ft²</td></tr>)}{!isGuided&&<tr><td colSpan={6}><div className="table-empty"><strong>No candidate slots</strong><span>Import a reviewed candidate CSV or add a slot on the map.</span></div></td></tr>}</tbody></table></div></section>
    <div className="screen-footer-action"><span className="footer-progress"><Icon name={isGuided ? "check" : "info"} /> {isGuided ? "Every fixture slot has fixed LID parameters and a bounded count" : "Candidate review required"}</span><button className="primary-button" disabled={!isGuided} onClick={() => onStep("goals")}>{isGuided ? "Review teaching objectives" : "Add a candidate first"} <Icon name="arrow" /></button></div>
  </div>;
}

export function GoalsScreen({ isGuided, onEvidence, onStep }: Pick<ScreenProps, "onEvidence" | "onStep"> & { isGuided: boolean }) {
  const [preset, setPreset] = useState("Outfall volume");
  return <div className="screen-stack">
    <PageHeading eyebrow="Goals / decision rules" title="State what a useful alternative must do" copy={isGuided ? "The teaching analysis compares exact four-storm outfall volume with a disclosed synthetic present-value cost." : "Add model-linked metrics, thresholds, and a sourced cost basis before starting exact jobs."} />
    <div className="preset-row">{["Outfall volume","Outfall peak","Flood screening"].map(item=><button key={item} className={preset===item?"active":""} onClick={()=>setPreset(item)}>{item}<small>{item===preset?"Selected":"Use preset"}</small></button>)}</div>
    <div className="split-grid goals-grid">
      <section className="paper-panel rules-panel"><div className="panel-heading"><div><p className="eyebrow">Hydraulic review</p><h2>{isGuided ? "Three teaching checks" : "No checks configured"}</h2></div><button className="secondary-button" disabled><Icon name="plus" /> Add rule in desktop build</button></div>
        <button className="rule-card" disabled={!isGuided} onClick={()=>onEvidence("qa-continuity")}><span className="rule-number">01</span><p>Inspect aggregate system flooding loss and continuity for every exact run.</p><Icon name="chevron" /></button>
        <button className="rule-card" disabled={!isGuided}><span className="rule-number">02</span><p>Compare total outfall event volume over the same four synthetic cases.</p><Icon name="chevron" /></button>
        <button className="rule-card" disabled={!isGuided}><span className="rule-number">03</span><p>Report maximum peak flow at <strong>OUT1</strong> without implying a return period.</p><Icon name="chevron" /></button>
      </section>
      <section className="paper-panel cost-panel"><div className="panel-heading"><div><p className="eyebrow">Decision objectives</p><h2>Minimize two measures</h2></div></div><div className="objective-card"><span>Primary</span><Icon name="rain" /><div><strong>Four-storm outfall volume</strong><small>Sum of exact event volumes · Mgal</small></div></div><button className="objective-card" disabled={!isGuided} onClick={()=>onEvidence("cost-basis")}><span>Secondary</span><b>$</b><div><strong>Illustrative present-value cost</strong><small>30 years · 3.0% real discount rate</small></div><Icon name="chevron" /></button><div className="cost-confirmed caution"><Icon name="warning"/><span><strong>Synthetic cost basis</strong><small>Teaching assumptions only · not an estimate</small></span></div></section>
    </div>
    <section className="paper-panel adverse-strip"><Icon name="trace"/><div><strong>{isGuided ? "Fixture-wide aggregate QA is reported" : "Systemwide scan not configured"}</strong><span>{isGuided ? "All 884 jobs retain system continuity, aggregate flooding loss, and OUT1 loading; per-object adverse-change metrics are not claimed by this fixture." : "Load a model before selecting adverse-change tolerances."}</span></div></section>
    <div className="screen-footer-action"><span className="footer-progress">{isGuided ? "2 objectives · synthetic cost disclosed · exact fixture complete" : "Authoritative setup required"}</span><button className="primary-button" disabled={!isGuided} onClick={() => onStep("run")}>{isGuided ? "Review executed run ledger" : "Complete model setup first"} <Icon name="arrow" /></button></div>
  </div>;
}

type RunScreenProps = Pick<ScreenProps, "onEvidence" | "onStep">;

export function RunScreen({ onEvidence, onStep }: RunScreenProps) {
  const completed = guidedFixture.job_accounting.completed_jobs;
  const designCapacity = calculateDesignCapacity(guidedFixture.job_accounting.expected_jobs, guidedFixture.job_accounting.storm_cases);
  const maximumCost = Math.max(...evaluatedAlternatives.map((item) => item.cost));
  const volumes = evaluatedAlternatives.map((item) => item.volume);
  const minimumVolume = Math.min(...volumes);
  const maximumVolume = Math.max(...volumes);
  return <div className="screen-stack">
    <PageHeading eyebrow="Run / executed teaching ledger" title="The exact fixture is complete" copy="All 884 jobs were executed with the recorded EPA SWMM 5.2.4 binary. This deterministic teaching subset is not an adaptive-search or optimality claim." action={<button className="primary-button" onClick={()=>onStep("compare")}>Compare alternatives <Icon name="arrow"/></button>} />
    <section className="run-hero paper-panel">
      <div className="run-dial" style={{ "--progress": "360deg" } as React.CSSProperties}><div><strong>✓</strong><span>Complete</span></div></div>
      <div className="run-copy"><p className="eyebrow">Budget arithmetic</p><h2><span>4 storms</span> × <span>{designCapacity} designs</span> + <span>4 baselines</span> = <strong>884 jobs</strong></h2><p>All designs were evaluated against the same synthetic storm ensemble. The 220 nonbaseline vectors are a seeded hash-ranked subset of the finite integer universe.</p></div>
      <div className="run-controls"><span className="exact-stamp"><Icon name="shield"/> Exact fixture</span><small>Search stopped: deterministic fixture budget completed</small></div>
    </section>
    <div className="run-stats"><div><span>Completed</span><strong className="mono">{completed}</strong><small>exact solver jobs</small></div><div><span>Solver</span><strong className="mono">5.2.4</strong><small>{guidedFixture.solver.sha256.slice(0,8)}… hash</small></div><div><span>Fully evaluated</span><strong className="mono">220</strong><small>designs · 4 storms each</small></div><div><span>QA accepted</span><strong className="mono">884</strong><small>0 blocking findings</small></div></div>
    <div className="split-grid run-lower">
      <section className="paper-panel frontier-preview"><div className="panel-heading"><div><p className="eyebrow">Executed set</p><h2>Teaching PV cost × outfall volume</h2></div><span className="exact-stamp"><Icon name="shield"/> Hydraulics exact</span></div><svg viewBox="0 0 540 220" role="img" aria-label="Preview scatterplot of 220 exact evaluated alternatives">{evaluatedAlternatives.map((item)=>{const x=30+(item.cost/maximumCost)*480;const y=190-((maximumVolume-item.volume)/(maximumVolume-minimumVolume))*155;return <circle key={item.id} cx={x} cy={y} r={item.nondominated?4:2.5} fill={item.nondominated?"#08766f":"#9bb0b5"} opacity={.82}/>})}</svg><div className="axis-label"><span>Lower synthetic PV cost</span><span>Higher synthetic PV cost</span></div></section>
      <section className="paper-panel event-log"><div className="panel-heading"><div><p className="eyebrow">Execution record</p><h2>Reproducible fixture trace</h2></div><span className="live-indicator">Complete</span></div>{[
        ["Exact execution", "884 of 884 direct subprocess jobs completed", "Complete"],
        ["Candidate order", "Seeded SHA-256 rank over finite integer vectors", "Seed 424242"],
        ["Quality review", "All runs accepted under 2% continuity policy", "Open evidence"],
      ].map((row,index)=><button className="event-row" key={row[0]} onClick={()=>onEvidence(index===2?"qa-continuity":index===1?"guided-model":"ALT-001")}><span className="event-dot"/><div><strong>{row[0]}</strong><small>{row[1]}</small></div><span>{row[2]}</span></button>)}</section>
    </div>
    <div className="screen-footer-action"><span className="footer-progress"><Icon name="check"/> Every visible guided hydraulic value is projected from this exact manifest</span><button className="primary-button" onClick={()=>onStep("compare")}>Compare alternatives <Icon name="arrow"/></button></div>
  </div>;
}

export function CompareScreen({ onEvidence, onStep }: Pick<ScreenProps, "onEvidence" | "onStep">) {
  const [selectedIds, setSelectedIds] = useState([alternatives[0].id, alternatives[3].id]);
  const [metric, setMetric] = useState("Four-storm outfall volume");
  const [activePanel, setActivePanel] = useState("Tradeoff");
  const jumpTo = (label: string, id: string) => {
    setActivePanel(label);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const selected = alternatives.filter(item=>selectedIds.includes(item.id));
  const toggle = (id:string) => setSelectedIds(current=>current.includes(id)?current.filter(item=>item!==id):current.length<4?[...current,id]:[...current.slice(1),id]);
  const metricValue = (item: (typeof evaluatedAlternatives)[number]) => metric === "Maximum outfall peak" ? item.peak : item.volume;
  const values = evaluatedAlternatives.map(metricValue);
  const minimumValue = Math.min(...values);
  const maximumValue = Math.max(...values);
  const maximumCost = Math.max(...evaluatedAlternatives.map((item) => item.cost));
  const xFor = (cost: number) => 75 + (cost / maximumCost) * 815;
  const yFor = (value: number) => 45 + ((maximumValue - value) / (maximumValue - minimumValue)) * 280;
  const frontierIds = nondominatedIds(evaluatedAlternatives, (item) => [item.cost, metricValue(item)]);
  const frontier = evaluatedAlternatives.filter((item) => frontierIds.has(item.id)).sort((left, right) => left.cost - right.cost);
  const frontierPath = frontier.map((item, index) => `${index ? "L" : "M"}${xFor(item.cost).toFixed(1)} ${yFor(metricValue(item)).toFixed(1)}`).join(" ");
  return <div className="screen-stack compare-screen">
    <PageHeading eyebrow="Compare / 220 exact designs" title="See the executed tradeoff, then inspect the evidence" copy="The teal line is nondominated among these 220 exact, hash-ranked teaching alternatives. It is not a global-optimum or real-world claim." action={<div className="comparison-tray">{selected.map(item=><button key={item.id} onClick={()=>onEvidence(item.id)}><i style={{background:item.color}}/>{item.id}</button>)}<span>{selected.length}/4</span></div>} />
    <div className="compare-toolbar"><div className="segmented"><button className={activePanel==="Tradeoff"?"active":""} onClick={()=>jumpTo("Tradeoff","tradeoff-panel")}>Tradeoff</button><button className={activePanel==="Storm matrix"?"active":""} onClick={()=>jumpTo("Storm matrix","storm-matrix")}>Storm matrix</button><button className={activePanel==="QA evidence"?"active":""} onClick={()=>jumpTo("QA evidence","qa-evidence")}>QA evidence</button></div><label>Performance measure<select value={metric} onChange={event=>setMetric(event.target.value)}><option>Four-storm outfall volume</option><option>Maximum outfall peak</option></select></label><span className="exact-stamp"><Icon name="shield"/> Exact hydraulics</span></div>
    <section className="paper-panel tradeoff-panel" id="tradeoff-panel">
      <div className="chart-heading"><div><span>Lower is better on both axes</span><h2>Illustrative present-value cost <small>synthetic 2026 USD, thousands</small></h2></div><div className="chart-legend"><span><i className="meet-dot"/> Nondominated for selected metric</span><span><i className="partial-dot"/> Retained shortlist</span><span><i className="fail-dot"/> Other exact design</span></div></div>
      <div className="tradeoff-chart"><div className="y-label">{metric}<small>{metric === "Maximum outfall peak" ? "cfs" : "million gallons"}</small></div><svg viewBox="0 0 920 390" role="img" aria-label={`Scatterplot of present-value cost and ${metric}`}>
        {[0,1,2,3,4].map(i=><line key={i} x1="75" x2="890" y1={45+i*70} y2={45+i*70} stroke="#dce3e0"/>) }
        {[0,1,2,3,4,5].map(i=><g key={i}><line x1={75+i*163} x2={75+i*163} y1="45" y2="325" stroke="#edf0ef"/><text x={75+i*163} y="350" textAnchor="middle">{Math.round(i*maximumCost/5)}</text></g>)}
        <path d={frontierPath} fill="none" stroke="#08766f" strokeWidth="2.5" strokeDasharray="7 7" />
        {evaluatedAlternatives.map((item)=>{const nondominated=frontierIds.has(item.id);return <circle key={item.id} cx={xFor(item.cost)} cy={yFor(metricValue(item))} r={nondominated ? 4.2 : 2.6} fill={nondominated ? "#08766f" : "#9bb0b5"} opacity={nondominated ? .9 : .48}/>})}
        {alternatives.map((item)=>{const x=xFor(item.cost);const y=yFor(metricValue(item));const active=selectedIds.includes(item.id);return <g key={item.id} role="button" tabIndex={0} aria-label={`${item.name}: ${item.cost.toFixed(2)} thousand synthetic dollars, ${metricValue(item).toFixed(3)} ${metric}`} onClick={()=>{toggle(item.id);onEvidence(item.id)}} onKeyDown={event=>(event.key==="Enter"||event.key===" ")&&toggle(item.id)} className="svg-focusable"><circle cx={x} cy={y} r={active?11:6.5} fill="#a96315" stroke={active?"#17313a":"#fff"} strokeWidth={active?4:2}/>{active&&<><line x1={x} y1={y-14} x2={x} y2={y-34} stroke="#17313a"/><rect x={x-45} y={y-58} width="90" height="24" rx="2" fill="#17313a"/><text x={x} y={y-42} textAnchor="middle" fill="white">{item.id}</text></>}</g>})}
        {[0,1,2,3,4].map(i=><text key={i} x="58" y={50+i*70} textAnchor="end">{(maximumValue-(maximumValue-minimumValue)*i/4).toFixed(metric === "Maximum outfall peak" ? 2 : 3)}</text>)}
      </svg></div>
      <p className="chart-caption"><Icon name="info"/> Gray and teal points are all 220 exact designs. Ochre points are the fixed artifact-retained shortlist; select up to four.</p>
    </section>
    <div className="comparison-detail-grid">
      <section className="paper-panel matrix-panel" id="storm-matrix"><div className="panel-heading"><div><p className="eyebrow">Storm-by-alternative</p><h2>Exact OUT1 event volume</h2></div></div><div className="table-wrap"><table><thead><tr><th>Alternative</th>{guidedFixture.storms.map(storm=><th key={storm.storm_id}>{storm.depth_inches.toFixed(1)} in</th>)}</tr></thead><tbody>{selected.map((item)=><tr key={item.id}><td><button className="cell-link" onClick={()=>onEvidence(item.id)}><i style={{background:item.color}}/>{item.id}</button></td>{item.stormVolumes.map((volume,index)=><td key={guidedFixture.storms[index].storm_id}><span className="matrix-cell" style={{"--intensity": `${Math.min(.92,.16+volume/.4)}`} as React.CSSProperties}>{volume.toFixed(3)}</span></td>)}</tr>)}</tbody></table></div><small className="table-note">OUT1 event volume · million gallons · exact results</small></section>
      <section className="paper-panel impact-panel" id="qa-evidence"><div className="panel-heading"><div><p className="eyebrow">Fixture-wide QA</p><h2>No reported flooding loss</h2></div><span className="mini-state pass">Observed in fixture</span></div><div className="network-mini"><svg viewBox="0 0 420 180"><path d="M45 70 190 100 360 135" fill="none" stroke="#9ab0b6" strokeWidth="5"/>{[[45,70],[190,100],[360,135]].map(([x,y],i)=><circle key={i} cx={x} cy={y} r="8" fill={i===2?"#08766f":"#f5f7f6"} stroke="#17313a" strokeWidth="3"/>)}</svg></div><div className="impact-summary"><div><strong className="mono">2</strong><span>junctions</span></div><div><strong className="mono">2</strong><span>conduits</span></div><div><strong className="mono">0.000</strong><span>Mgal flooding loss</span></div></div><p className="fine-print">This is an exact fixture observation—not a general flood-risk determination.</p></section>
    </div>
    <section className="paper-panel alternative-table"><div className="panel-heading"><div><p className="eyebrow">Accessible result table</p><h2>All eight fixed artifact-retained designs</h2></div><span className="exact-stamp"><Icon name="shield"/> 32 accepted runs</span></div><div className="table-wrap"><table><thead><tr><th>Compare</th><th>Alternative</th><th>Synthetic PV cost</th><th>4-storm volume</th><th>Reduction</th><th>Peak</th><th>Run coverage</th></tr></thead><tbody>{alternatives.map(item=><tr key={item.id} className={selectedIds.includes(item.id)?"selected-row":""}><td><input type="checkbox" checked={selectedIds.includes(item.id)} onChange={()=>toggle(item.id)} aria-label={`Compare ${item.id}`}/></td><td><button className="cell-link" onClick={()=>onEvidence(item.id)}><i style={{background:item.color}}/><strong>{item.id}</strong> · {item.name}</button></td><td className="mono">${item.cost.toFixed(2)}k</td><td className="mono">{item.volume.toFixed(3)} Mgal</td><td className="mono">−{item.reduction.toFixed(2)}%</td><td className="mono">{item.peak.toFixed(2)} cfs</td><td><span className="exact-stamp small">4/4 exact</span></td></tr>)}</tbody></table></div></section>
    <div className="screen-footer-action"><span className="footer-progress">{selected.length} retained alternatives selected for the learning summary</span><button className="primary-button" onClick={()=>onStep("export")}>Prepare learning evidence <Icon name="arrow"/></button></div>
  </div>;
}

export function ExportScreen({ readiness, onEvidence }: Pick<ScreenProps, "readiness" | "onEvidence">) {
  const [checks, setChecks] = useState([true,true,true,true,true,true]);
  const [downloaded, setDownloaded] = useState(false);
  const labels = ["Every displayed hydraulic value resolves to the exact fixture manifest","All 884 solver jobs have accepted QA","The cost basis is visibly synthetic and nonprofessional","The fixture-wide flooding-loss observation is disclosed narrowly","All four storms are labelled synthetic and user-defined","DEMO limitations and non-optimality language remain attached"];
  const ready = checks.every(Boolean);
  const files = [
    ["Exact fixture manifest", "JSON", "884 jobs · SHA-256 traced"], ["Learning UI projection", "JSON", "220 designs · 4 storms"], ["Retained model inputs", "INP", "4 baselines + 32 fixed-shortlist runs"], ["Retained solver reports", "RPT", "Baseline + fixed shortlist"], ["Retained binary outputs", "OUT", "Reopenable exact evidence"], ["Public contracts", "SCHEMA", "Version 1.0 JSON Schemas"],
  ];
  const downloadLearningSummary = () => {
    const blob = new Blob([JSON.stringify(guidedFixture, null, 2) + "\n"], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "gi-scenario-studio-guided-exact-summary.json";
    anchor.click();
    URL.revokeObjectURL(url);
    setDownloaded(true);
  };
  return <div className="screen-stack">
    <PageHeading eyebrow="Export / teaching evidence" title="Package the evidence, not just the chart" copy="The repository fixture retains a complete manifest plus full INP, RPT, and OUT artifacts for every baseline and a fixed, result-independent shortlist." />
    {(readiness==="CONCEPTUAL"||readiness==="DEMO")&&<div className="conceptual-notice"><Icon name="warning"/><div><strong>{readiness==="DEMO"?"Teaching model export":"Conceptual planning model export"}</strong><span>{readiness==="DEMO"?"All pages and tables will remain visibly marked for learning use.":"Not for design, permitting, flood-risk determination, or representation of existing drainage infrastructure."}</span></div></div>}
    <div className="export-grid">
      <section className="paper-panel export-checklist"><div className="panel-heading"><div><p className="eyebrow">Review gate</p><h2>{checks.filter(Boolean).length} of {checks.length} complete</h2></div><span className={`score-ring ${ready?"ready":""}`}>{checks.filter(Boolean).length}/{checks.length}</span></div>{labels.map((label,index)=><label className="export-check" key={label}><input type="checkbox" checked={checks[index]} onChange={()=>setChecks(current=>current.map((value,i)=>i===index?!value:value))}/><span><Icon name="check"/></span><div><strong>{label}</strong><small>{index===0?"220 designs · 884 exact solver jobs":index===2?"Synthetic 2026 USD · 30-year horizon":"No blocking QA findings"}</small></div><button type="button" className="icon-button" onClick={(event)=>{event.preventDefault();onEvidence(index===2?"cost-basis":index===0?"ALT-001":"qa-continuity")}} aria-label={`Inspect evidence for ${label}`}><Icon name="chevron"/></button></label>)}</section>
      <section className="paper-panel bundle-preview"><div className="panel-heading"><div><p className="eyebrow">Repository evidence set</p><h2>guided_cc0_exact_v1</h2></div><Icon name="folder"/></div><ul>{files.map(file=><li key={file[0]}><span className="file-type">{file[1]}</span><div><strong>{file[0]}</strong><small>{file[2]}</small></div><Icon name="check"/></li>)}</ul><div className="bundle-foot"><span>Retained fixture</span><strong className="mono">36 run sets</strong></div></section>
    </div>
    <section className="paper-panel export-summary"><div><span className="export-seal"><Icon name="trace"/></span><div><p className="eyebrow">Evidence chain</p><h2>220 alternatives · 884 accepted exact jobs · one pinned solver hash</h2><p>The browser download is a compact learning summary. The desktop worker creates repository-verified JSON, CSV, Markdown, and HTML bundles with selected retained artifacts.</p></div></div><button className="primary-button" disabled={!ready} onClick={downloadLearningSummary}><Icon name="download"/> {downloaded ? "Download summary again" : "Download learning summary"}</button></section>
    {downloaded&&<div className="success-banner" role="status"><Icon name="check"/><div><strong>Exact learning summary downloaded</strong><span>gi-scenario-studio-guided-exact-summary.json</span></div></div>}
  </div>;
}
