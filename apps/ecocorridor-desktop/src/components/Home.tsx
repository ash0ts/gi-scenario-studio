import { useEffect, useState } from "react";
import type { EntryPath, ReopenableProject, RpcPayload } from "../types";
import { ProfessionalProjectStateSchema } from "../lib/professional";
import { hasNativeHost, hostErrorMessage, hostRequest, listProjectWorkspaces, openProjectWorkspace, rpcData } from "../lib/host";
import { CorridorMark, Icon } from "./Icon";

interface HomeProps {
  onChoose: (path: EntryPath) => void;
  onResume: (project: ReopenableProject) => void;
}

const paths: Array<{
  id: EntryPath;
  index: string;
  title: string;
  copy: string;
  action: string;
  note: string;
  icon: "upload" | "layers" | "play";
  primary?: boolean;
}> = [
  {
    id: "existing",
    index: "01",
    title: "Open reviewed connectivity model",
    copy: "Compare pre-approved intervention packages against a portable Circuitscape model and its ecological evidence.",
    action: "Open reviewed model",
    note: "Professional path · W&B binding required",
    icon: "upload",
    primary: true,
  },
  {
    id: "planning",
    index: "02",
    title: "Assemble planning model",
    copy: "Align source assets, document resistance hypotheses, define focal areas, and review action effects without silently inventing ecological values.",
    action: "Assemble planning model",
    note: "Conceptual screening · W&B binding required",
    icon: "layers",
  },
  {
    id: "guided",
    index: "03",
    title: "Explore Ridgeway Crossing",
    copy: "Inspect a synthetic decision story that teaches robustness, safeguard review, and traceable direct-run evidence.",
    action: "Open synthetic replay",
    note: "No login · no cloud call · CC0 fixture",
    icon: "play",
  },
];

export function Home({ onChoose, onResume }: HomeProps) {
  const [recentProjects, setRecentProjects] = useState<ReopenableProject[]>([]);
  const [recentState, setRecentState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [recentMessage, setRecentMessage] = useState("");

  useEffect(() => {
    if (!hasNativeHost()) return;
    let active = true;
    setRecentState("loading");
    void (async () => {
      try {
        const handles = await listProjectWorkspaces();
        const projects: ReopenableProject[] = [];
        let unreadable = 0;
        for (const candidate of handles) {
          try {
            const opened = await openProjectWorkspace(candidate.handle);
            const response = await hostRequest<RpcPayload<unknown>>("connectivity.project.get_state", {
              project_handle: opened.handle,
            });
            const projectState = ProfessionalProjectStateSchema.parse(rpcData(response));
            projects.push({ projectHandle: opened.handle, lastModifiedMs: candidate.lastModifiedMs, projectState });
          } catch {
            unreadable += 1;
          }
        }
        if (!active) return;
        setRecentProjects(projects);
        setRecentMessage(unreadable ? `${unreadable} saved project${unreadable === 1 ? "" : "s"} could not be verified. Its local files were left unchanged.` : "");
        setRecentState("ready");
      } catch (reason) {
        if (!active) return;
        setRecentProjects([]);
        setRecentMessage(hostErrorMessage(reason, "Saved EcoCorridor projects could not be listed. Start a new path or restart the desktop application."));
        setRecentState("error");
      }
    })();
    return () => { active = false; };
  }, []);

  return (
    <div className="home-shell">
      <header className="home-header">
        <a className="wordmark" href="#main-content" aria-label="EcoCorridor Scenario Studio home">
          <CorridorMark />
          <span><b>EcoCorridor</b> Scenario Studio</span>
        </a>
        <div className="home-security"><Icon name="shield" /><span>Local evidence remains authoritative</span></div>
      </header>
      <main id="main-content">
        <section className="home-hero" aria-labelledby="home-heading">
          <div className="hero-copy">
            <p className="home-kicker"><span />Auditable connectivity portfolios</p>
            <h1 id="home-heading">See what holds<br />across the landscape.</h1>
            <p>Turn an ecologist-reviewed connectivity model into a defensible shortlist, with each reported value tied to a direct Circuitscape run.</p>
          </div>
          <div className="hero-transect" aria-hidden="true">
            <span className="hero-core core-one" /><span className="hero-core core-two" />
            <span className="hero-road"><i /><i /></span>
            <span className="hero-core core-three" /><span className="hero-core core-four" />
            <svg viewBox="0 0 600 210"><path d="M28 99C131 31 217 159 298 108s175-75 275 20"/><path d="M23 128c116 42 203-24 284 5s173 77 273 18"/></svg>
          </div>
          <div className="promise-strip">
            <Icon name="route" />
            <p><strong>Connectivity under assumptions, not animal behavior.</strong> Current maps diagnose modeled pathways; they do not predict movement, crossing use, collisions, or population outcomes.</p>
          </div>
        </section>
        {(recentState === "loading" || recentProjects.length > 0 || recentMessage) && (
          <section className="recent-projects" aria-labelledby="recent-projects-heading" aria-busy={recentState === "loading"}>
            <div className="recent-heading">
              <div><p className="section-kicker">Local continuity</p><h2 id="recent-projects-heading">Continue a saved project.</h2></div>
              <p>Project SQLite is reopened through a contained, opaque desktop handle.</p>
            </div>
            {recentState === "loading" && <p className="recent-status" role="status"><Icon name="route" /> Checking local engineering records…</p>}
            {recentProjects.length > 0 && (
              <ul className="recent-project-list">
                {recentProjects.map((project) => {
                  const recovery = project.projectState.pending_recovery.required;
                  const guided = project.projectState.project.mode === "GUIDED";
                  const syncState = typeof project.projectState.sync_status.state === "string" ? project.projectState.sync_status.state : "LOCAL_ONLY";
                  const modified = project.lastModifiedMs > 0
                    ? new Date(project.lastModifiedMs).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
                    : "Saved locally";
                  return (
                    <li key={project.projectHandle}>
                      <button type="button" onClick={() => onResume(project)} aria-label={`Continue ${project.projectState.project.title}`}>
                        <span className="recent-project-mark"><Icon name={recovery ? "warning" : "layers"} /></span>
                        <span className="recent-project-copy"><strong>{project.projectState.project.title}</strong><small>{guided ? "Synthetic guided replay" : project.projectState.project.mode === "PLANNING_DRAFT" ? "Planning assembly" : "Reviewed model"} · {modified}</small></span>
                        <span className={`recent-project-state${recovery ? " needs-recovery" : ""}`}>{guided ? recovery ? "Resume Ridgeway rerun" : "Open saved replay" : recovery ? "Resume interrupted run" : syncState.replaceAll("_", " ")}</span>
                        <Icon name="arrow" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
            {recentMessage && <p className={`recent-status${recentState === "error" ? " error" : ""}`} role={recentState === "error" ? "alert" : "status"}><Icon name="warning" /> {recentMessage}</p>}
          </section>
        )}
        <section className="entry-section" aria-labelledby="entry-heading">
          <div className="entry-heading">
            <div><p className="section-kicker">Choose an entry path</p><h2 id="entry-heading">Start with the evidence you have.</h2></div>
            <p>Professional data never enters the public replay.</p>
          </div>
          <div className="entry-grid">
            {paths.map((path) => (
              <article className={`entry-card${path.primary ? " is-primary" : ""}`} key={path.id}>
                <div className="entry-card-top"><span className="entry-index">{path.index}</span><Icon name={path.icon} /></div>
                <h3>{path.title}</h3>
                <p>{path.copy}</p>
                <button className={path.primary ? "primary-button" : "text-action"} type="button" onClick={() => onChoose(path.id)}>
                  {path.action}<Icon name="arrow" />
                </button>
                <small>{path.note}</small>
              </article>
            ))}
          </div>
        </section>
      </main>
      <footer className="home-footer">
        <span>Raster pairwise resistance mode · v0.1</span>
        <span>Direct Circuitscape results under documented assumptions</span>
      </footer>
    </div>
  );
}
