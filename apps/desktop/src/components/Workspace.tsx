import { useState } from "react";
import { evidence } from "../data/demo";
import type { EvidenceItem, ProjectSummary, Readiness, StepId } from "../types";
import { EvidenceRail } from "./EvidenceRail";
import { Icon } from "./Icons";
import { CompareScreen, ExportScreen, GoalsScreen, ModelScreen, OpportunitiesScreen, RunScreen, StormsScreen } from "./Screens";

const steps: Array<{ id: StepId; label: string; short: string }> = [
  { id: "model", label: "Model", short: "Import & check" },
  { id: "storms", label: "Storms", short: "4 cases" },
  { id: "opportunities", label: "Opportunities", short: "4 slots" },
  { id: "goals", label: "Goals", short: "Rules & cost" },
  { id: "run", label: "Run", short: "884 jobs" },
  { id: "compare", label: "Compare", short: "Exact results" },
  { id: "export", label: "Export", short: "Evidence bundle" },
];

const setupStepLabels: Record<StepId, string> = {
  model: "Source required",
  storms: "Not configured",
  opportunities: "Not configured",
  goals: "Not configured",
  run: "Locked",
  compare: "Locked",
  export: "Locked",
};

interface WorkspaceProps {
  project: ProjectSummary;
  onHome: () => void;
}

export function Workspace({ project, onHome }: WorkspaceProps) {
  const isGuided = project.kind === "demo";
  const [step, setStep] = useState<StepId>(project.kind === "demo" ? "compare" : "model");
  const readiness: Readiness = project.readiness;
  const initialEvidence = isGuided
    ? evidence["ALT-220"]
    : project.kind === "existing"
      ? evidence["import-model"]
      : evidence["concept-model"];
  const [evidenceItem, setEvidenceItem] = useState<EvidenceItem>(initialEvidence);
  const [evidenceOpen, setEvidenceOpen] = useState(true);
  const [railCollapsed, setRailCollapsed] = useState(false);

  const currentIndex = steps.findIndex((item) => item.id === step);

  const showEvidence = (id: string) => {
    setEvidenceItem(evidence[id] ?? initialEvidence);
    setEvidenceOpen(true);
  };

  const navigate = (next: StepId) => {
    if (!isGuided && ["run", "compare", "export"].includes(next)) return;
    setStep(next);
    document.querySelector(".workspace-main")?.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <div className={`app-frame ${evidenceOpen ? "evidence-visible" : ""} ${railCollapsed ? "nav-collapsed" : ""}`}>
      <header className="app-header">
        <button className="app-wordmark" onClick={onHome} aria-label="Return to project home">
          <span className="wordmark-glyph small"><i /><i /><i /></span>
          <span>GI <b>Scenario Studio</b></span>
        </button>
        <div className="project-title">
          <span>{project.name}</span>
          <small>{project.location}</small>
        </div>
        <div className="header-actions">
          <span className="saved-state"><Icon name="check" /> {isGuided ? "Bundled offline" : "Local setup draft"}</span>
          <span className="avatar-button" aria-label={isGuided ? "Teaching fixture" : "Reviewer not assigned"}>{isGuided ? "DE" : "—"}</span>
        </div>
      </header>

      <nav className="workflow-nav" aria-label="Project workflow">
        <div className="nav-project-block">
          <div className="project-switcher"><span className="project-symbol"><Icon name="rain" /></span><span><strong>{project.name}</strong><small>{project.modelFile}</small></span></div>
          <div className={`readiness-badge readiness-${readiness.toLowerCase()}`}><span /><div><small>Model readiness</small><strong>{readiness.replace("_", " ")}</strong></div></div>
        </div>

        <ol className="step-list">
          {steps.map((item, index) => (
            <li key={item.id}>
              <button
                className={step === item.id ? "active" : index < currentIndex || isGuided ? "complete" : ""}
                onClick={() => navigate(item.id)}
                aria-current={step === item.id ? "step" : undefined}
                disabled={!isGuided && ["run", "compare", "export"].includes(item.id)}
                title={!isGuided && ["run", "compare", "export"].includes(item.id) ? "Available after authoritative model setup and exact jobs" : undefined}
              >
                <span className="step-marker">{index < currentIndex || (project.kind === "demo" && item.id !== step) ? <Icon name="check" /> : index + 1}</span>
                <span><strong>{item.label}</strong><small>{isGuided ? item.short : setupStepLabels[item.id]}</small></span>
                {step === item.id && <i />}
              </button>
            </li>
          ))}
        </ol>

        <div className="nav-bottom">
          <button onClick={() => setRailCollapsed(!railCollapsed)} className="collapse-nav"><Icon name="chevron" /><span>Collapse steps</span></button>
          <div className="local-mode"><span className="online-dot"/><div><strong>Local mode</strong><small>No telemetry</small></div></div>
        </div>
      </nav>

      <main className="workspace-main" id="main-content">
        {(readiness === "CONCEPTUAL" || readiness === "DEMO") && (
          <div className={`readiness-ribbon ${readiness.toLowerCase()}`}>
            <Icon name={readiness === "DEMO" ? "spark" : "warning"} />
            <strong>{readiness === "DEMO" ? "Teaching model" : "Conceptual planning model"}</strong>
            <span>{readiness === "DEMO" ? "For learning and interface evaluation—not a professional model." : "Not for design, permitting, flood-risk determination, or representation of existing drainage infrastructure."}</span>
          </div>
        )}
        {!isGuided && (
          <div className="setup-preview-notice" role="status">
            <Icon name="info" />
            <strong>Setup preview</strong>
            <span>No project bundle or GIS source has been loaded in this browser session. Exact run, comparison, and export remain locked.</span>
          </div>
        )}
        <div className="workspace-content">
          {step === "model" && <ModelScreen kind={project.kind} readiness={readiness} onEvidence={showEvidence} onStep={navigate} />}
          {step === "storms" && <StormsScreen isGuided={isGuided} onEvidence={showEvidence} onStep={navigate} />}
          {step === "opportunities" && <OpportunitiesScreen isGuided={isGuided} onEvidence={showEvidence} onStep={navigate} />}
          {step === "goals" && <GoalsScreen isGuided={isGuided} onEvidence={showEvidence} onStep={navigate} />}
          {step === "run" && isGuided && <RunScreen onEvidence={showEvidence} onStep={navigate} />}
          {step === "compare" && isGuided && <CompareScreen onEvidence={showEvidence} onStep={navigate} />}
          {step === "export" && isGuided && <ExportScreen readiness={readiness} onEvidence={showEvidence} />}
        </div>
      </main>

      {!evidenceOpen && (
        <button className="evidence-tab" onClick={() => setEvidenceOpen(true)} aria-label="Open evidence rail"><Icon name="trace" /><span>Evidence</span></button>
      )}
      <EvidenceRail item={evidenceItem} readiness={readiness} open={evidenceOpen} onClose={() => setEvidenceOpen(false)} />
    </div>
  );
}
