import { useState } from "react";
import { guidedSummary } from "../data/guidedSummary";
import { Icon } from "./Icons";
import { observabilityDetail, observabilityLabel } from "../lib/observability";
import type { ObservabilityState, ProjectKind } from "../types";

interface HomeProps {
  onStart: (kind: ProjectKind) => Promise<void>;
  observability: ObservabilityState;
  onObservability: () => void;
  desktopAvailable: boolean;
}

const paths = [
  {
    kind: "existing" as const,
    number: "01",
    icon: "folder" as const,
    title: "Open existing SWMM project",
    copy: "For practicing engineers with a fit-for-purpose model: reproduce the baseline, compare GI alternatives, and export a defensible evidence trail.",
    action: "Select SWMM bundle",
    detail: "ZIP with INP + referenced rainfall, climate, interface, and hot-start files",
  },
  {
    kind: "conceptual" as const,
    number: "02",
    icon: "map" as const,
    title: "Conceptual model starter · planned",
    copy: "Inspect the intended planning workflow and safety gates. GIS ingestion, generation, repair, and persisted provenance are not operational in this source MVP.",
    action: "Inspect planned workflow",
    detail: "Source-empty preview · no generated hydraulic model",
  },
  {
    kind: "demo" as const,
    number: "03",
    icon: "spark" as const,
    title: "Run guided example",
    copy: `Learn the decision workflow with Cedar Trace, a public teaching catchment backed by ${guidedSummary.completedJobs} accepted EPA SWMM ${guidedSummary.solverVersion} executions.`,
    action: "Open exact fixture",
    detail: `${guidedSummary.alternativeDesigns} alternatives · ${guidedSummary.stormCases} synthetic storms · verified fixture replay`,
  },
];

export function Home({ onStart, observability, onObservability, desktopAvailable }: HomeProps) {
  const [loading, setLoading] = useState<ProjectKind | null>(null);

  const start = async (kind: ProjectKind) => {
    setLoading(kind);
    try { await onStart(kind); } finally { setLoading(null); }
  };

  return (
    <main className="home-shell" id="main-content">
      <header className="home-header">
        <a className="wordmark" href="#top" aria-label="GI Scenario Studio home">
          <span className="wordmark-glyph"><i /><i /><i /></span>
          <span>GI <b>Scenario Studio</b></span>
        </a>
        <div className="home-meta">
          <button className="home-observability" onClick={onObservability}>
            <span className={`observability-dot mode-${observability.mode}`} />
            <span><strong>{observabilityLabel(observability)}</strong><small>{observabilityDetail(observability)}</small></span>
            <Icon name="chevron" />
          </button>
        </div>
      </header>

      <section className="home-intro" id="top">
        <div>
          <div className="home-kicker"><span /> Alternatives workspace for EPA SWMM</div>
          <h1>See which GI alternative earns a closer look.</h1>
          <p>For stormwater and water-resources engineers who need to compare cost and hydraulic performance without losing the evidence behind a number.</p>
        </div>
        <aside className="home-outcomes" aria-label="What the application helps you decide">
          <p className="eyebrow">Decision value</p>
          <ol>
            <li><span>01</span><p><strong>Reproduce the baseline</strong><small>Know whether the starting model and storms are trustworthy enough for screening.</small></p></li>
            <li><span>02</span><p><strong>Test feasible GI placements</strong><small>Search only within engineer-approved controls, slots, and counts.</small></p></li>
            <li><span>03</span><p><strong>Defend the shortlist</strong><small>Trace every displayed hydraulic value to its exact SWMM run and QA record.</small></p></li>
          </ol>
        </aside>
        <div className="promise-line">
          <Icon name="shield" />
          <span><strong>Hydraulics stay authoritative.</strong> ML may choose the next candidate to simulate; it never supplies a reported engineering result.</span>
        </div>
      </section>

      <section className="entry-paths" aria-labelledby="entry-title">
        <div className="path-intro">
          <p className="eyebrow" id="entry-title">Choose where you’re starting</p>
          <p>Professional import, planned model starter, or exact guided replay.</p>
        </div>
        <div className="path-grid">
          {paths.map((path) => (
            <article className={`path-card path-${path.kind}`} key={path.kind}>
              <div className="path-card-top"><span>{path.number}</span><Icon name={path.icon} /></div>
              <h2>{path.title}</h2>
              <p>{path.copy}</p>
              <button onClick={() => start(path.kind)} disabled={loading !== null || (!desktopAvailable && path.kind !== "demo")}>
                <span>{!desktopAvailable && path.kind !== "demo" ? "Desktop app required" : loading === path.kind ? "Preparing workspace…" : path.action}</span>
                <Icon name="arrow" />
              </button>
              <small>{!desktopAvailable && path.kind !== "demo" ? "Browser preview cannot access engineering files or create projects" : path.detail}</small>
            </article>
          ))}
        </div>
      </section>

      <footer className="home-footer">
        <span>GI Scenario Studio <b>v0.2 source MVP</b></span>
        <span>Targets EPA SWMM 5.2.4 · Local evidence authority · Restricted W&amp;B mirror required for production</span>
      </footer>
    </main>
  );
}
