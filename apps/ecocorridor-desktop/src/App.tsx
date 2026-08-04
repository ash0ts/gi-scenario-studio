import { useState } from "react";
import type { AppMode, EntryPath, ObservabilityPreflight, ReopenableProject, RidgewayFixture } from "./types";
import { Home } from "./components/Home";
import { WandbGate } from "./components/WandbGate";
import { Workspace } from "./components/Workspace";

const RESUMABLE_BINDING_STATES = new Set(["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE", "FAILED_RETRYING"]);

function hasPreviouslyVerifiedBinding(project: ReopenableProject): boolean {
  const state = project.projectState.sync_status["state"];
  return typeof state === "string" && RESUMABLE_BINDING_STATES.has(state);
}

export function EcoApp({ mode = "desktop", publicFixture = null }: { mode?: AppMode; publicFixture?: RidgewayFixture | null }) {
  const publicMode = mode === "public-replay";
  const [path, setPath] = useState<EntryPath | null>(publicMode ? "guided" : null);
  const [pendingPath, setPendingPath] = useState<Exclude<EntryPath, "guided"> | null>(null);
  const [preflight, setPreflight] = useState<ObservabilityPreflight | null>(null);
  const [reopenedProject, setReopenedProject] = useState<ReopenableProject | null>(null);
  const [pendingReopenedProject, setPendingReopenedProject] = useState<ReopenableProject | null>(null);

  const choose = (next: EntryPath) => {
    setPendingReopenedProject(null);
    if (next === "guided") {
      setReopenedProject(null);
      setPath(next);
    } else {
      setPendingPath(next);
    }
  };

  const enter = (verified: ObservabilityPreflight) => {
    if (!pendingPath) return;
    setPreflight(verified);
    setReopenedProject(pendingReopenedProject);
    setPath(pendingPath);
    setPendingPath(null);
    setPendingReopenedProject(null);
  };

  const resume = (project: ReopenableProject) => {
    if (project.projectState.project.mode === "GUIDED") {
      setPreflight(null);
      setPendingPath(null);
      setPendingReopenedProject(null);
      setReopenedProject(project);
      setPath("guided");
      return;
    }
    const projectPath = project.projectState.project.mode === "PLANNING_DRAFT" ? "planning" : "existing";
    if (!hasPreviouslyVerifiedBinding(project)) {
      setReopenedProject(null);
      setPreflight(null);
      setPendingReopenedProject(project);
      setPendingPath(projectPath);
      return;
    }
    setPreflight(null);
    setPendingPath(null);
    setPendingReopenedProject(null);
    setReopenedProject(project);
    setPath(projectPath);
  };

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      {path ? <Workspace mode={mode} path={path} localPreview={false} preflight={preflight} reopenedProject={reopenedProject} publicFixture={publicFixture} onHome={publicMode ? undefined : () => { setPath(null); setPreflight(null); setReopenedProject(null); setPendingReopenedProject(null); }} /> : <Home onChoose={choose} onResume={resume} />}
      {!publicMode && <WandbGate path={pendingPath} onClose={() => { setPendingPath(null); setPendingReopenedProject(null); }} onVerified={enter} />}
    </>
  );
}

export function App() {
  return <EcoApp mode="desktop" />;
}
