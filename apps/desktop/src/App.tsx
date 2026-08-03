import { useState } from "react";
import { Home } from "./components/Home";
import { Workspace } from "./components/Workspace";
import { demoProject } from "./data/demo";
import { transport } from "./lib/transport";
import type { ProjectKind, ProjectSummary } from "./types";

export function App() {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const start = async (kind: ProjectKind) => {
    setError(null);
    try {
      if (kind === "demo") {
        setProject(demoProject);
        return;
      }
      const created = await transport.request<ProjectSummary>("project.create", { kind });
      setProject(created);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The project could not be opened.");
    }
  };

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      {error && <div className="global-error" role="alert">{error}</div>}
      {project ? <Workspace project={project} onHome={() => setProject(null)} /> : <Home onStart={start} />}
    </>
  );
}
