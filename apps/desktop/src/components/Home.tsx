import { useState } from "react";
import { Icon } from "./Icons";
import type { ProjectKind } from "../types";

interface HomeProps {
  onStart: (kind: ProjectKind) => Promise<void>;
}

const paths = [
  {
    kind: "existing" as const,
    number: "01",
    icon: "folder" as const,
    title: "Open existing SWMM project",
    copy: "Check a model you trust, compare green-infrastructure alternatives, and create a traceable screening package.",
    action: "Preview import workflow",
    detail: "Desktop worker wiring required for files",
  },
  {
    kind: "conceptual" as const,
    number: "02",
    icon: "map" as const,
    title: "Build conceptual model",
    copy: "Start with your GIS files or approved open data, then review every generated object and assumption.",
    action: "Preview model starter",
    detail: "Source-empty planning workflow",
  },
  {
    kind: "demo" as const,
    number: "03",
    icon: "spark" as const,
    title: "Run guided example",
    copy: "Explore a self-authored teaching catchment backed by 884 accepted EPA SWMM 5.2.4 executions.",
    action: "Open exact fixture",
    detail: "220 alternatives · 4 synthetic storms · works offline",
  },
];

export function Home({ onStart }: HomeProps) {
  const [loading, setLoading] = useState<ProjectKind | null>(null);

  const start = async (kind: ProjectKind) => {
    setLoading(kind);
    try { await onStart(kind); } finally { setLoading(null); }
  };

  return (
    <main className="home-shell">
      <header className="home-header">
        <a className="wordmark" href="#top" aria-label="GI Scenario Studio home">
          <span className="wordmark-glyph"><i /><i /><i /></span>
          <span>GI <b>Scenario Studio</b></span>
        </a>
        <div className="home-meta">
          <span><span className="online-dot" /> Local workspace</span>
          <span>Evidence-first · Offline-ready</span>
        </div>
      </header>

      <section className="home-intro" id="top">
        <div className="home-kicker"><span /> Open alternatives workspace for EPA SWMM</div>
        <h1>Follow every decision<br />back to the water.</h1>
        <p>Build or open a drainage model, test green-infrastructure alternatives, and trace every reported value to an exact simulation.</p>
        <div className="promise-line">
          <Icon name="shield" />
          <span><strong>Exact means exact.</strong> ML can suggest what to simulate next; it never supplies the engineering result.</span>
        </div>
      </section>

      <section className="entry-paths" aria-labelledby="entry-title">
        <div className="path-intro">
          <p className="eyebrow" id="entry-title">Choose where you’re starting</p>
          <p>You can change methods later. Your model and evidence stay local.</p>
        </div>
        <div className="path-grid">
          {paths.map((path) => (
            <article className={`path-card path-${path.kind}`} key={path.kind}>
              <div className="path-card-top"><span>{path.number}</span><Icon name={path.icon} /></div>
              <h2>{path.title}</h2>
              <p>{path.copy}</p>
              <button onClick={() => start(path.kind)} disabled={loading !== null}>
                <span>{loading === path.kind ? "Preparing workspace…" : path.action}</span>
                <Icon name="arrow" />
              </button>
              <small>{path.detail}</small>
            </article>
          ))}
        </div>
      </section>

      <footer className="home-footer">
        <span>GI Scenario Studio <b>v0.1 preview</b></span>
        <span>Targets EPA SWMM 5.2.4 · Open source · Offline-first</span>
      </footer>
    </main>
  );
}
