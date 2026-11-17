import { useEffect, useRef, useState } from "react";
import type { GuidedData } from "../data/demo";
import { initialGuidedRunState, summarizeSolverJobs, type GuidedRunState, type SolverJobStatusRow } from "../lib/guidedRun";
import type { EvidenceGuideReference } from "../lib/evidenceGuide";
import {
  observabilityDetail,
  observabilityLabel,
  projectCloudActionLabel,
  projectCloudSyncDetail,
  projectCloudSyncLabel,
  projectCloudSyncTone,
  type ProjectCloudSyncState,
} from "../lib/observability";
import {
  aggregateValidation,
  candidateSlotPayload,
  emptyCandidateSlot,
  initialStormCases,
  resolvedRunEvidence,
  summarizeExactJobs,
  type AnalysisPlanResponse,
  type CandidateSlotDraft,
  type DecisionBoard,
  type EvidenceExportResult,
  type ExactJobRow,
  type ExportEligibilityResult,
  type ModelValidationResult,
  type PractitionerStormCase,
  type ResolvedExactRun,
} from "../lib/practitioner";
import { transport } from "../lib/transport";
import type { EvidenceItem, ObservabilityState, ProjectSummary, StepId } from "../types";
import { EvidenceRail } from "./EvidenceRail";
import { Icon } from "./Icons";
import {
  PractitionerCompareScreen,
  PractitionerExportScreen,
  PractitionerGoalsScreen,
  PractitionerModelScreen,
  PractitionerOpportunitiesScreen,
  PractitionerRunScreen,
  PractitionerStormsScreen,
  type FitnessAttestationDraft,
  type PractitionerRunRuntime,
} from "./PractitionerScreens";
import { CompareScreen, ExportScreen, GoalsScreen, ModelScreen, OpportunitiesScreen, OverviewScreen, RunScreen, StormsScreen } from "./Screens";

const workflowSteps = (guidedData?: GuidedData | null): Array<{ id: StepId; label: string; short: string }> => [
  { id: "model", label: "Model", short: "Import & check" },
  { id: "storms", label: "Storms", short: guidedData ? `${guidedData.fixture.job_accounting.storm_cases} cases` : "Configure cases" },
  { id: "opportunities", label: "Opportunities", short: guidedData ? `${guidedData.fixture.candidate_slots.length} slots` : "Configure slots" },
  { id: "goals", label: "Goals", short: "Rules & cost" },
  { id: "run", label: "Run", short: guidedData ? `${guidedData.fixture.job_accounting.completed_jobs} jobs` : "Exact jobs" },
  { id: "compare", label: "Compare", short: "Exact results" },
  { id: "export", label: "Export", short: "Evidence bundle" },
];

const guidedSteps = (guidedData: GuidedData): Array<{ id: StepId; label: string; short: string }> => [
  { id: "overview", label: "Overview", short: "Purpose & checkpoints" },
  ...workflowSteps(guidedData),
];

const setupStepLabels: Record<StepId, string> = {
  overview: "Purpose & checkpoints",
  model: "Source required",
  storms: "Not configured",
  opportunities: "Not configured",
  goals: "Not configured",
  run: "Plan exact jobs",
  compare: "Exact results",
  export: "Evidence gates",
};

interface WorkspaceProps {
  project: ProjectSummary;
  onProjectUpdate: (project: ProjectSummary) => void;
  guidedData: GuidedData | null;
  observability: ObservabilityState;
  projectCloudSync: ProjectCloudSyncState | null;
  projectCloudSyncBusy: boolean;
  projectCloudSyncError: string | null;
  onRefreshProjectCloudSync: () => void;
  onObservability: () => void;
  onHome: () => void;
}

const setupEvidence = (project: ProjectSummary): EvidenceItem => ({
  id: project.kind === "existing" ? "import-model" : "concept-model",
  kind: "model",
  eyebrow: project.bundle ? "Hashed source import" : "Unexecuted setup draft",
  title: project.name,
  subtitle: project.bundle ? "Source bundle recorded; baseline execution and model-fitness review remain" : "No authoritative source or hydraulic result has been recorded yet",
  status: project.bundle ? "source" : "warning",
  artifactsAvailable: Boolean(project.bundle),
  fields: [
    { label: "Project kind", value: project.kind.toUpperCase(), mono: true },
    { label: "Model fitness", value: project.fitnessDisposition.replaceAll("_", " ").toUpperCase(), mono: true },
    ...(project.bundle ? [
      { label: "Bundle SHA-256", value: project.bundle.sha256, mono: true },
      { label: "Dependency closure", value: project.bundle.dependencyComplete ? `${project.bundle.dependencyCount} references resolved` : "Incomplete" },
      { label: "Import preflight", value: `${project.bundle.blockingCount ?? "?"} blocking · ${project.bundle.warningCount ?? "?"} review` },
    ] : []),
    { label: "Exact runs", value: "None" },
    { label: "Next action", value: project.kind === "existing" ? project.bundle ? "Review preflight and define storm cases" : "Select a complete SWMM project bundle" : "Load study-area source data" },
  ],
});

export function Workspace({ project, onProjectUpdate, guidedData, observability, projectCloudSync, projectCloudSyncBusy, projectCloudSyncError, onRefreshProjectCloudSync, onObservability, onHome }: WorkspaceProps) {
  const isGuided = project.kind === "demo";
  if (isGuided && !guidedData) {
    throw new Error("A guided workspace requires evidence returned by guided.open.");
  }
  const data = guidedData as GuidedData | null;
  const steps = isGuided
    ? guidedSteps(data as GuidedData)
    : workflowSteps().map((item) => item.id === "goals" ? { ...item, label: "Alternatives", short: "Exact test set" } : item);
  const [step, setStep] = useState<StepId>(isGuided ? "overview" : "model");
  const [unlockedIndex, setUnlockedIndex] = useState(0);
  const [completedSteps, setCompletedSteps] = useState<Set<StepId>>(new Set());
  const initialEvidence = isGuided
    ? (data as GuidedData).evidence[(data as GuidedData).alternatives.at(-1)?.id ?? "guided-model"]
    : setupEvidence(project);
  const [evidenceItem, setEvidenceItem] = useState<EvidenceItem>(initialEvidence);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [guidedRun, setGuidedRun] = useState<GuidedRunState>(() =>
    initialGuidedRunState(data?.fixture.job_accounting.expected_jobs ?? 0),
  );
  const [runControlBusy, setRunControlBusy] = useState(false);
  const [practitionerValidation, setPractitionerValidation] = useState<ModelValidationResult | null>(null);
  const [practitionerValidationBusy, setPractitionerValidationBusy] = useState(false);
  const [practitionerValidationError, setPractitionerValidationError] = useState<string | null>(null);
  const [practitionerStorms, setPractitionerStorms] = useState<PractitionerStormCase[]>(() => {
    const authoritativeModelPath = project.bundle?.models.find(
      (model) => model.modelPath === project.bundle?.baseModel,
    )?.projectPath;
    return initialStormCases(project.bundle?.models ?? [], authoritativeModelPath);
  });
  const [practitionerCandidates, setPractitionerCandidates] = useState<CandidateSlotDraft[]>([]);
  const [practitionerVectors, setPractitionerVectors] = useState<number[][]>([]);
  const [practitionerBudget, setPractitionerBudget] = useState(1000);
  const [adverseTolerance, setAdverseTolerance] = useState(0);
  const [practitionerRun, setPractitionerRun] = useState<PractitionerRunRuntime>({
    phase: "unplanned",
    completedJobs: 0,
    succeededJobs: 0,
    rejectedJobs: 0,
    failedJobs: 0,
    cancelledJobs: 0,
    knownJobs: 0,
    totalJobs: 0,
    message: "No exact job plan has been written. Review the arithmetic before creating one.",
  });
  const [practitionerRunBusy, setPractitionerRunBusy] = useState(false);
  const [practitionerRows, setPractitionerRows] = useState<ExactJobRow[]>([]);
  const [decisionBoard, setDecisionBoard] = useState<DecisionBoard | null>(null);
  const [decisionBoardBusy, setDecisionBoardBusy] = useState(false);
  const [decisionBoardError, setDecisionBoardError] = useState<string | null>(null);
  const [fitnessPassport, setFitnessPassport] = useState<Record<string, unknown> | null | undefined>(undefined);
  const [fitnessBusy, setFitnessBusy] = useState(false);
  const [fitnessError, setFitnessError] = useState<string | null>(null);
  const [exportMode, setExportMode] = useState<"learning" | "screening" | "verified_alternatives">("learning");
  const [includeExportArtifacts, setIncludeExportArtifacts] = useState(true);
  const [exportEligibility, setExportEligibility] = useState<ExportEligibilityResult | null>(null);
  const [createdExport, setCreatedExport] = useState<EvidenceExportResult | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const currentFitnessDisposition = typeof fitnessPassport?.disposition === "string" ? fitnessPassport.disposition : project.fitnessDisposition;
  const fitnessLabel = currentFitnessDisposition.replaceAll("_", " ");
  const evidenceReturnFocus = useRef<HTMLElement | null>(null);
  const cloudLabel = projectCloudSync ? projectCloudSyncLabel(projectCloudSync) : observabilityLabel(observability);
  const cloudDetail = projectCloudSync ? projectCloudSyncDetail(projectCloudSync) : observabilityDetail(observability);
  const cloudTone = projectCloudSync ? `cloud-${projectCloudSyncTone(projectCloudSync)}` : `mode-${observability.mode}`;
  const cloudActionLabel = projectCloudSync ? projectCloudActionLabel(projectCloudSync) : "Configure W&B";
  const cloudNeedsAccountAction = projectCloudSync?.state === "LOCAL_ONLY" || projectCloudSync?.state === "AUTHENTICATION_REQUIRED" || projectCloudSync?.state === "BLOCKED_PROJECT_POLICY";
  const handleCloudAction = projectCloudSync && !cloudNeedsAccountAction ? onRefreshProjectCloudSync : onObservability;
  const canRunLocally = transport.mode === "tauri" && Boolean(project.projectDirectory) && Boolean(projectCloudSync?.can_run_local);

  const currentIndex = steps.findIndex((item) => item.id === step);
  const practitionerSlots = (() => {
    try {
      return practitionerCandidates.map(candidateSlotPayload);
    } catch {
      return [];
    }
  })();
  const selectedPractitionerStorms = practitionerStorms.filter((storm) => storm.included);
  const succeededPractitionerJobIds = [...new Set(practitionerRows.filter((row) => row.state === "SUCCEEDED").map((row) => row.job_id))];

  const resetPlannedAnalysis = () => {
    setPractitionerRun({
      phase: "unplanned",
      completedJobs: 0,
      succeededJobs: 0,
      rejectedJobs: 0,
      failedJobs: 0,
      cancelledJobs: 0,
      knownJobs: 0,
      totalJobs: 0,
      message: "Setup changed. Review the arithmetic and create a new durable analysis revision.",
    });
    setPractitionerRows([]);
    setDecisionBoard(null);
    setExportEligibility(null);
    setCreatedExport(null);
  };

  const showEvidence = (id: string) => {
    evidenceReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const selected = data?.evidence[id] ?? initialEvidence;
    setEvidenceItem(id === "import-model" && typeof fitnessPassport?.disposition === "string" ? {
      ...selected,
      fields: selected.fields.map((field) => field.label === "Model fitness" ? { ...field, value: fitnessPassport.disposition as string } : field),
    } : selected);
    setEvidenceOpen(true);
  };

  const showWorkerRunEvidence = async (jobId: string) => {
    if (!project.projectDirectory) return;
    evidenceReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setEvidenceItem({
      id: jobId,
      kind: "run",
      eyebrow: "Resolving authoritative evidence",
      title: jobId,
      subtitle: "The worker is rehashing local artifacts before returning this exact run.",
      status: "review",
      fields: [{ label: "State", value: "CHECKING", mono: true }],
    });
    setEvidenceOpen(true);
    try {
      const run = await transport.request<ResolvedExactRun>("evidence.resolve", {
        project_directory: project.projectDirectory,
        job_id: jobId,
      });
      setEvidenceItem(resolvedRunEvidence(run));
    } catch (reason) {
      setEvidenceItem({
        id: jobId,
        kind: "run",
        eyebrow: "Evidence unavailable",
        title: jobId,
        subtitle: reason instanceof Error ? reason.message : "The worker could not resolve this run.",
        status: "warning",
        fields: [{ label: "Corrective action", value: "Confirm that the exact job succeeded and retry." }],
      });
    }
  };

  const navigate = (next: StepId) => {
    const nextIndex = steps.findIndex((item) => item.id === next);
    if (nextIndex < 0) return;
    if (nextIndex > unlockedIndex) return;
    setStep(next);
  };

  const advance = (next: StepId) => {
    const nextIndex = steps.findIndex((item) => item.id === next);
    if (nextIndex < 0) return;
    setCompletedSteps((current) => new Set(current).add(step));
    setUnlockedIndex((current) => Math.max(current, nextIndex));
    setStep(next);
  };

  useEffect(() => {
    const workspaceMain = document.querySelector<HTMLElement>(".workspace-main");
    workspaceMain?.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    window.requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>("[data-screen-title]");
      heading?.focus({ preventScroll: true });
    });
  }, [step]);

  useEffect(() => {
    if (project.kind !== "existing" || !project.projectDirectory) return;
    let active = true;
    transport.request<{ passport: Record<string, unknown> | null }>("model.get_fitness", {
      project_directory: project.projectDirectory,
      project_id: project.id,
    }).then((result) => {
      if (active) setFitnessPassport(result.passport);
    }).catch((reason) => {
      if (!active) return;
      setFitnessPassport(null);
      setFitnessError(reason instanceof Error ? reason.message : "Model fitness evidence could not be read.");
    });
    return () => { active = false; };
  }, [project.kind, project.projectDirectory, project.id, project.bundle?.modelFitnessPassportId]);

  useEffect(() => {
    if (project.kind !== "existing" || !fitnessPassport || typeof fitnessPassport.disposition !== "string") return;
    setEvidenceItem((current) => current.id !== "import-model" ? current : {
      ...current,
      fields: current.fields.map((field) => field.label === "Model fitness"
        ? { ...field, value: fitnessPassport.disposition as string }
        : field),
    });
  }, [fitnessPassport, project.kind]);

  useEffect(() => {
    if (!project.projectDirectory || !guidedRun.analysisId || !["running", "pause_requested", "cancel_requested"].includes(guidedRun.phase)) return;
    const projectDirectory = project.projectDirectory;
    let active = true;
    const refresh = async () => {
      try {
        const result = await transport.request<{ rows: SolverJobStatusRow[] }>("results.query", {
          project_directory: projectDirectory,
          analysis_id: guidedRun.analysisId,
        });
        if (!active) return;
        const summary = summarizeSolverJobs(result.rows, guidedRun.totalJobs);
        setGuidedRun((current) => {
          if (current.analysisId !== guidedRun.analysisId) return current;
          const allFinished = summary.knownJobs === summary.totalJobs && summary.completedJobs === summary.totalJobs;
          const hasRejected = summary.rejectedJobs > 0 || summary.failedJobs > 0;
          const haltedOnFailure = summary.failedJobs > 0;
          const phase = haltedOnFailure
            ? "failed"
            : allFinished
            ? hasRejected
              ? "failed"
              : summary.cancelledJobs > 0
                ? "cancelled"
                : "completed"
            : current.phase;
          const message = haltedOnFailure
            ? "The local worker stopped after a failed exact job; inspect repository diagnostics before retrying."
            : allFinished
            ? hasRejected
              ? "Local rerun finished with failed or QA-rejected jobs; inspect repository evidence before comparison."
              : summary.cancelledJobs > 0
                ? "Local rerun cancelled; completed exact results remain preserved."
                : "Local rerun completed; the worker recorded a tolerance-based differential report."
            : current.phase === "pause_requested"
              ? `Pause requested after ${summary.completedJobs} of ${summary.totalJobs} terminal jobs.`
              : current.phase === "cancel_requested"
                ? `Cancellation requested; ${summary.completedJobs} of ${summary.totalJobs} jobs are terminal.`
                : `${summary.completedJobs} of ${summary.totalJobs} local exact jobs are terminal.`;
          return { ...current, ...summary, phase, message, error: undefined };
        });
      } catch (reason) {
        if (!active) return;
        setGuidedRun((current) => ({
          ...current,
          message: "The rerun continues locally, but its progress refresh failed and will retry.",
          error: reason instanceof Error ? reason.message : "Progress refresh failed.",
        }));
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [guidedRun.analysisId, guidedRun.phase, guidedRun.totalJobs, project.projectDirectory]);

  useEffect(() => {
    if (!project.projectDirectory || !practitionerRun.analysisId || !["starting", "running", "pause_requested", "cancel_requested"].includes(practitionerRun.phase)) return;
    const projectDirectory = project.projectDirectory;
    const analysisId = practitionerRun.analysisId;
    let active = true;
    const refresh = async () => {
      try {
        const result = await transport.request<{ rows: ExactJobRow[] }>("results.query", {
          project_directory: projectDirectory,
          analysis_id: analysisId,
        });
        if (!active) return;
        setPractitionerRows(result.rows);
        const summary = summarizeExactJobs(result.rows, practitionerRun.totalJobs);
        const states = new Map(result.rows.map((row) => [row.job_id, row.state]));
        const hasRunning = [...states.values()].includes("RUNNING");
        const hasQueued = [...states.values()].includes("QUEUED");
        setPractitionerRun((current) => {
          if (current.analysisId !== analysisId) return current;
          const allTerminal = summary.knownJobs === summary.totalJobs && summary.completedJobs === summary.totalJobs;
          let phase = current.phase;
          let message = current.message;
          if (summary.failedJobs > 0) {
            phase = "failed";
            message = "Execution stopped after a failed exact job. Succeeded runs remain available for evidence inspection.";
          } else if (allTerminal) {
            phase = summary.cancelledJobs > 0 ? "cancelled" : "completed";
            message = summary.cancelledJobs > 0
              ? "Execution was cancelled; completed exact evidence remains preserved."
              : summary.rejectedJobs > 0
                ? "Execution finished with QA-rejected jobs. Compare will exclude incomplete accepted ensembles."
                : "Every planned direct SWMM job completed with accepted repository evidence.";
          } else if (current.phase === "pause_requested" && !hasRunning && hasQueued) {
            phase = "paused";
            message = "Execution is paused at a durable job barrier. Completed results are preserved.";
          } else if (current.phase === "cancel_requested") {
            message = `Cancellation is being applied; ${summary.completedJobs} of ${summary.totalJobs} jobs are terminal.`;
          } else if (current.phase === "pause_requested") {
            message = `Pause requested; ${summary.completedJobs} of ${summary.totalJobs} jobs are terminal.`;
          } else {
            phase = "running";
            message = `${summary.completedJobs} of ${summary.totalJobs} direct SWMM jobs are terminal.`;
          }
          return { ...current, ...summary, phase, message, error: undefined };
        });
      } catch (reason) {
        if (!active) return;
        setPractitionerRun((current) => ({ ...current, error: reason instanceof Error ? reason.message : "Progress refresh failed." }));
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [practitionerRun.analysisId, practitionerRun.phase, practitionerRun.totalJobs, project.projectDirectory]);

  const startGuidedRerun = async () => {
    const directory = project.projectDirectory;
    if (!directory || transport.mode !== "tauri") return;
    setRunControlBusy(true);
    setGuidedRun({
      ...initialGuidedRunState(data?.fixture.job_accounting.expected_jobs ?? 884),
      phase: "validating",
      message: "Checking the unchanged Cedar Trace source model before exact execution.",
    });
    try {
      const normalizedDirectory = directory.replace(/[\\/]+$/, "");
      const validation = await transport.request<Record<string, unknown>>("model.validate", {
        path: `${normalizedDirectory}/sources/cedar-trace-reference.inp`,
      });
      setGuidedRun((current) => ({ ...current, phase: "starting", message: "Planning 884 direct SWMM jobs from the pinned teaching design order." }));
      const started = await transport.request<{ analysis_id: string; state: string; replay_jobs: number }>("guided.rerun", {
        project_directory: directory,
        project_id: project.id,
        validation,
        idempotency_key: `guided-rerun-${globalThis.crypto.randomUUID()}`,
      });
      setGuidedRun((current) => ({
        ...current,
        phase: "running",
        analysisId: started.analysis_id,
        totalJobs: started.replay_jobs,
        message: "Local exact rerun started. Progress is read from the authoritative solver-job ledger.",
      }));
    } catch (reason) {
      setGuidedRun((current) => ({
        ...current,
        phase: "failed",
        message: "The local rerun did not start.",
        error: reason instanceof Error ? reason.message : "The local rerun did not start.",
      }));
    } finally {
      setRunControlBusy(false);
    }
  };

  const transitionGuidedRun = async (method: "guided.pause" | "guided.resume" | "guided.cancel") => {
    if (!project.projectDirectory || !guidedRun.analysisId) return;
    setRunControlBusy(true);
    try {
      const result = await transport.request<{ state: string }>(method, {
        project_directory: project.projectDirectory,
        analysis_id: guidedRun.analysisId,
      });
      if (!result.state) throw new Error("The worker returned no analysis state.");
      setGuidedRun((current) => ({
        ...current,
        phase: method === "guided.pause" ? "pause_requested" : method === "guided.cancel" ? "cancel_requested" : "running",
        message: method === "guided.pause"
          ? "Pause requested. The active SWMM subprocess may finish; no new jobs will start."
          : method === "guided.cancel"
            ? "Cancellation requested. Completed exact results will be preserved."
            : "Local rerun resumed from the durable job ledger.",
        error: undefined,
      }));
    } catch (reason) {
      setGuidedRun((current) => ({
        ...current,
        message: "The requested rerun control was not applied.",
        error: reason instanceof Error ? reason.message : "Rerun control failed.",
      }));
    } finally {
      setRunControlBusy(false);
    }
  };

  const projectContainedPath = (relativePath: string) => {
    const directory = project.projectDirectory?.replace(/[\\/]+$/, "");
    if (!directory) throw new Error("The authoritative project directory is unavailable.");
    return `${directory}/${relativePath.replace(/^[\\/]+/, "")}`;
  };

  const selectPractitionerBaseModel = async (modelPath: string) => {
    if (!project.projectDirectory || !project.bundle) {
      setPractitionerValidationError("The authoritative imported project is unavailable.");
      return;
    }
    const selected = project.bundle.models.find((model) => model.projectPath === modelPath);
    if (!selected) {
      setPractitionerValidationError("Choose one of the project-contained SWMM models.");
      return;
    }
    setPractitionerValidationBusy(true);
    setPractitionerValidationError(null);
    try {
      const result = await transport.request<{
        project_state: {
          selected_base_model: string;
          selected_model_dependencies?: Record<string, string>;
          current_model_revision_id: string;
          model_fitness_passport_id: string;
          base_model_selection_required: boolean;
        };
        model_validation: ModelValidationResult;
      }>("bundle.select_base_model", {
        project_directory: project.projectDirectory,
        project_id: project.id,
        model_path: selected.projectPath,
      });
      if (result.project_state.selected_base_model !== selected.projectPath) {
        throw new Error("The worker selected a different base model than requested.");
      }
      const models = project.bundle.models.map((model) => model.projectPath === selected.projectPath
        ? { ...model, dependencies: result.project_state.selected_model_dependencies ?? model.dependencies }
        : model);
      onProjectUpdate({
        ...project,
        location: `Imported · ${project.bundle.fileCount} files · base model selected`,
        modelFile: selected.modelPath,
        bundle: {
          ...project.bundle,
          baseModel: selected.modelPath,
          baseModelSelectionRequired: result.project_state.base_model_selection_required,
          blockingCount: result.model_validation.blocking_count,
          warningCount: result.model_validation.warning_count,
          structurallyRunnable: result.model_validation.structurally_runnable,
          models,
          currentModelRevisionId: result.project_state.current_model_revision_id,
          modelFitnessPassportId: result.project_state.model_fitness_passport_id,
        },
      });
      setPractitionerValidation(result.model_validation);
      setPractitionerStorms(initialStormCases(models, selected.projectPath).map((storm) =>
        storm.modelPath === selected.projectPath
          ? {
              ...storm,
              dependencies: result.project_state.selected_model_dependencies ?? storm.dependencies,
              validation: result.model_validation,
            }
          : storm,
      ));
      setFitnessPassport(undefined);
      setFitnessError(null);
      resetPlannedAnalysis();
    } catch (reason) {
      setPractitionerValidationError(reason instanceof Error ? reason.message : "Base-model selection failed.");
    } finally {
      setPractitionerValidationBusy(false);
    }
  };

  const validatePractitionerModel = async () => {
    if (project.bundle?.baseModelSelectionRequired) {
      setPractitionerValidationError("Select and persist the authoritative base model before running checks.");
      return;
    }
    const selected = project.bundle?.models.find((model) => model.modelPath === project.bundle?.baseModel);
    if (!selected) {
      setPractitionerValidationError("The imported bundle has no selected SWMM model.");
      return;
    }
    setPractitionerValidationBusy(true);
    setPractitionerValidationError(null);
    try {
      const report = await transport.request<ModelValidationResult>("model.validate", {
        path: projectContainedPath(selected.projectPath),
      });
      setPractitionerValidation(report);
      setPractitionerStorms((current) => current.map((storm) => storm.modelPath === selected.projectPath ? { ...storm, validation: report } : storm));
    } catch (reason) {
      setPractitionerValidationError(reason instanceof Error ? reason.message : "Model checks failed.");
    } finally {
      setPractitionerValidationBusy(false);
    }
  };

  const attestPractitionerFitness = async (draft: FitnessAttestationDraft) => {
    if (!project.projectDirectory || !project.bundle?.currentModelRevisionId || !practitionerValidation) return;
    setFitnessBusy(true);
    setFitnessError(null);
    const structuralFinding = `Worker structural validation recorded ${practitionerValidation.blocking_count} blocking and ${practitionerValidation.warning_count} review findings.`;
    try {
      const result = await transport.request<{ passport: Record<string, unknown> }>("model.attest", {
        project_directory: project.projectDirectory,
        passport: {
          project_id: project.id,
          model_revision_id: project.bundle.currentModelRevisionId,
          intended_use: draft.intendedUse,
          disposition: draft.disposition,
          criteria: [
            {
              criterion_id: "structural_validation",
              label: "Structural validation",
              status: practitionerValidation.warning_count > 0 ? "review" : "pass",
              finding: structuralFinding,
            },
            {
              criterion_id: "intended_use_review",
              label: "Fitness for stated use",
              status: draft.disposition === "not_fit" ? "block" : "review",
              finding: draft.disposition === "not_fit"
                ? "The identified engineer determined that this revision is not fit for the stated use."
                : "The identified engineer determined that this revision is fit only with the recorded limitations.",
              remediation: draft.disposition === "not_fit" ? "Resolve the blocking fitness evidence before relying on professional screening outputs." : null,
            },
          ],
          permitted_outputs: draft.disposition === "not_fit" ? ["learning evidence package"] : ["draft alternatives-screening evidence with stated limitations"],
          prohibited_uses: ["design", "permitting", "flood-risk determination", "regulatory approval", "construction recommendation"],
          unresolved_limitations: draft.limitations,
          reviewer: draft.reviewer,
          reviewer_attested: true,
        },
      });
      setFitnessPassport(result.passport);
      setExportEligibility(null);
      setCreatedExport(null);
    } catch (reason) {
      setFitnessError(reason instanceof Error ? reason.message : "The fitness attestation could not be recorded.");
    } finally {
      setFitnessBusy(false);
    }
  };

  const updatePractitionerStorm = (index: number, patch: Partial<PractitionerStormCase>) => {
    setPractitionerStorms((current) => current.map((storm, itemIndex) => {
      if (itemIndex !== index) return storm;
      if (patch.included === true && !storm.stormEligible) return storm;
      return { ...storm, ...patch };
    }));
    resetPlannedAnalysis();
  };

  const validatePractitionerStorms = async () => {
    const selected = practitionerStorms.map((storm, index) => ({ storm, index })).filter(({ storm }) => storm.included);
    if (!selected.length) return;
    setPractitionerValidationBusy(true);
    setPractitionerValidationError(null);
    try {
      const reports = await Promise.all(selected.map(({ storm }) => transport.request<ModelValidationResult>("model.validate", {
        path: projectContainedPath(storm.modelPath),
      })));
      setPractitionerStorms((current) => current.map((storm, index) => {
        const selectedIndex = selected.findIndex((item) => item.index === index);
        return selectedIndex >= 0 ? { ...storm, validation: reports[selectedIndex] } : storm;
      }));
      const primaryIndex = selected.findIndex(({ storm }) => storm.modelPath === (project.bundle?.models.find((model) => model.modelPath === project.bundle?.baseModel)?.projectPath ?? project.bundle?.models[0]?.projectPath));
      setPractitionerValidation(reports[Math.max(0, primaryIndex)]);
    } catch (reason) {
      setPractitionerValidationError(reason instanceof Error ? reason.message : "Storm-model validation failed.");
    } finally {
      setPractitionerValidationBusy(false);
    }
  };

  const planPractitionerAnalysis = async () => {
    if (!project.projectDirectory) return;
    const validation = aggregateValidation(practitionerStorms);
    if (!validation || validation.blocking_count !== 0) {
      setPractitionerRun((current) => ({ ...current, phase: "failed", message: "The durable plan was not created.", error: "Every selected storm model requires zero blocking validation findings." }));
      return;
    }
    let slots;
    try {
      slots = practitionerCandidates.map(candidateSlotPayload);
    } catch (reason) {
      setPractitionerRun((current) => ({ ...current, phase: "failed", message: "The durable plan was not created.", error: reason instanceof Error ? reason.message : "Candidate setup is invalid." }));
      return;
    }
    setPractitionerRunBusy(true);
    setPractitionerRun((current) => ({ ...current, phase: "planning", message: "The worker is validating every exact mutation and writing the complete job ledger.", error: undefined }));
    try {
      const plan = await transport.request<AnalysisPlanResponse>("analysis.plan", {
        project_directory: project.projectDirectory,
        project_id: project.id,
        candidate_slots: slots,
        design_vectors: practitionerVectors,
        storm_cases: selectedPractitionerStorms.map((storm) => ({
          storm_id: storm.stormId,
          model_path: storm.modelPath,
          dependencies: storm.dependencies,
        })),
        budget: { max_solver_jobs: practitionerBudget, seed: 42, worker_count: 1 },
        qa_policy: {
          runoff_continuity_warning_percent: 2,
          flow_continuity_warning_percent: 2,
          absolute_tolerance: 0.000001,
          relative_tolerance: 0.0001,
          accepted_exceptions: {},
        },
        timeout_seconds: 3600,
        validation,
        search_method: "engineer_selected_exact",
        search_reason: "Evaluate the explicit engineer-approved alternatives with direct SWMM runs.",
        idempotency_key: `practitioner-plan-${globalThis.crypto.randomUUID()}`,
      });
      setPractitionerRun({
        phase: "ready",
        analysisId: plan.analysis_id,
        runPlan: plan.run_plan,
        completedJobs: 0,
        succeededJobs: 0,
        rejectedJobs: 0,
        failedJobs: 0,
        cancelledJobs: 0,
        knownJobs: plan.planned_solver_jobs,
        totalJobs: plan.run_plan.total_exact_jobs,
        message: "The complete exact job plan is durable and ready. No solver process has started.",
      });
      setPractitionerRows([]);
      setDecisionBoard(null);
      setExportEligibility(null);
      setCreatedExport(null);
    } catch (reason) {
      setPractitionerRun((current) => ({
        ...current,
        phase: "failed",
        message: "The durable plan was not created.",
        error: reason instanceof Error ? reason.message : "Exact planning failed.",
      }));
    } finally {
      setPractitionerRunBusy(false);
    }
  };

  const startPractitionerAnalysis = async () => {
    if (!project.projectDirectory || !practitionerRun.analysisId) return;
    setPractitionerRunBusy(true);
    setPractitionerRun((current) => ({ ...current, phase: "starting", message: "Starting the pinned SWMM worker from the durable ledger.", error: undefined }));
    try {
      await transport.request("analysis.start", {
        project_directory: project.projectDirectory,
        analysis_id: practitionerRun.analysisId,
      });
      setPractitionerRun((current) => ({ ...current, phase: "running", message: "Direct SWMM jobs are running in persisted design and storm order." }));
    } catch (reason) {
      setPractitionerRun((current) => ({ ...current, phase: "failed", message: "Exact execution did not start.", error: reason instanceof Error ? reason.message : "Analysis start failed." }));
    } finally {
      setPractitionerRunBusy(false);
    }
  };

  const transitionPractitionerAnalysis = async (method: "analysis.pause" | "analysis.resume" | "analysis.cancel") => {
    if (!project.projectDirectory || !practitionerRun.analysisId) return;
    setPractitionerRunBusy(true);
    try {
      await transport.request(method, {
        project_directory: project.projectDirectory,
        analysis_id: practitionerRun.analysisId,
      });
      setPractitionerRun((current) => ({
        ...current,
        phase: method === "analysis.pause" ? "pause_requested" : method === "analysis.cancel" ? "cancel_requested" : "running",
        message: method === "analysis.pause"
          ? "Pause requested. The active subprocess may finish; no new job will start after the worker reaches the barrier."
          : method === "analysis.cancel"
            ? "Cancellation requested. Completed exact evidence will remain in the repository."
            : "Execution resumed from the durable job ledger.",
        error: undefined,
      }));
    } catch (reason) {
      setPractitionerRun((current) => ({ ...current, error: reason instanceof Error ? reason.message : "The run control failed." }));
    } finally {
      setPractitionerRunBusy(false);
    }
  };

  const loadDecisionBoard = async () => {
    if (!project.projectDirectory || !practitionerRun.analysisId) return;
    setDecisionBoardBusy(true);
    setDecisionBoardError(null);
    try {
      const board = await transport.request<DecisionBoard>("results.decision_board", {
        project_directory: project.projectDirectory,
        analysis_id: practitionerRun.analysisId,
        adverse_tolerance: adverseTolerance,
      });
      setDecisionBoard(board);
      if (board.model_fitness_passport) setFitnessPassport(board.model_fitness_passport);
    } catch (reason) {
      setDecisionBoardError(reason instanceof Error ? reason.message : "The exact decision board could not be loaded.");
    } finally {
      setDecisionBoardBusy(false);
    }
  };

  const validatePractitionerExport = async () => {
    if (!project.projectDirectory || !practitionerRun.analysisId || !succeededPractitionerJobIds.length) return;
    setExportBusy(true);
    setExportError(null);
    try {
      const eligibility = await transport.request<ExportEligibilityResult>("export.validate", {
        project_directory: project.projectDirectory,
        analysis_id: practitionerRun.analysisId,
        job_ids: succeededPractitionerJobIds,
        mode: exportMode,
        include_costs: false,
      });
      setExportEligibility(eligibility);
    } catch (reason) {
      setExportError(reason instanceof Error ? reason.message : "Export eligibility could not be checked.");
    } finally {
      setExportBusy(false);
    }
  };

  const createPractitionerExport = async () => {
    if (!project.projectDirectory || !practitionerRun.analysisId || !exportEligibility?.allowed) return;
    setExportBusy(true);
    setExportError(null);
    try {
      const destination = projectContainedPath(`exports/export-${globalThis.crypto.randomUUID()}`);
      const created = await transport.request<EvidenceExportResult>("export.create", {
        project_directory: project.projectDirectory,
        destination,
        analysis_id: practitionerRun.analysisId,
        job_ids: succeededPractitionerJobIds,
        mode: exportMode,
        template_version: "evidence-v2",
        assumptions: [],
        accepted_exceptions: [],
        include_costs: false,
        include_artifacts: includeExportArtifacts,
      });
      setCreatedExport(created);
    } catch (reason) {
      setExportError(reason instanceof Error ? reason.message : "The evidence package could not be created.");
    } finally {
      setExportBusy(false);
    }
  };

  useEffect(() => {
    if (isGuided || step !== "compare" || !practitionerRun.analysisId) return;
    void loadDecisionBoard();
    // The analysis ID and comparison tolerance define this worker projection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, practitionerRun.analysisId, adverseTolerance, isGuided]);

  useEffect(() => {
    if (isGuided || step !== "export" || !project.projectDirectory) return;
    let active = true;
    setFitnessPassport(undefined);
    transport.request<{ passport: Record<string, unknown> | null }>("model.get_fitness", {
      project_directory: project.projectDirectory,
      project_id: project.id,
    }).then((result) => {
      if (active) setFitnessPassport(result.passport);
    }).catch((reason) => {
      if (!active) return;
      setFitnessPassport(null);
      setExportError(reason instanceof Error ? reason.message : "Model fitness evidence could not be read.");
    });
    if (practitionerRun.analysisId) {
      transport.request<{ rows: ExactJobRow[] }>("results.query", {
        project_directory: project.projectDirectory,
        analysis_id: practitionerRun.analysisId,
      }).then((result) => active && setPractitionerRows(result.rows)).catch(() => undefined);
    }
    return () => { active = false; };
  }, [isGuided, step, project.projectDirectory, project.id, practitionerRun.analysisId]);

  const closeEvidence = () => {
    setEvidenceOpen(false);
    window.requestAnimationFrame(() => evidenceReturnFocus.current?.focus());
  };

  const openAssistantEvidenceReference = (reference: EvidenceGuideReference) => {
    const localEvidence = data
      ? Object.values(data.evidence).find((candidate) => (
        candidate.id === reference.evidenceId
        || candidate.id === reference.sourceId
        || candidate.runIds?.includes(reference.sourceId)
        || candidate.runRecords?.some((run) => run.runId === reference.sourceId)
      ))
      : undefined;
    if (localEvidence) {
      setEvidenceItem(localEvidence);
      setEvidenceOpen(true);
      return;
    }
    if (reference.kind === "exact_run" || reference.kind === "exact_metric") {
      void showWorkerRunEvidence(reference.sourceId);
    }
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
          <span className="saved-state"><Icon name="check" /> {isGuided ? "Verified replay loaded" : practitionerRun.phase === "completed" ? "Exact analysis complete" : practitionerRun.analysisId ? `Analysis ${practitionerRun.phase.replaceAll("_", " ")}` : "Local setup draft"}</span>
          <button className={`header-observability ${cloudTone}`} onClick={handleCloudAction} disabled={projectCloudSyncBusy} aria-label={`${cloudLabel}. ${cloudDetail}. ${cloudActionLabel}`}>
            <span className={`observability-dot ${cloudTone}`} />
            <span>{projectCloudSyncBusy ? "Checking sync…" : cloudLabel}</span>
          </button>
          <span className="avatar-button" aria-label={isGuided ? "Teaching fixture" : "Reviewer not assigned"}>{isGuided ? "DE" : "—"}</span>
        </div>
      </header>

      <nav className="workflow-nav" aria-label="Project workflow">
        <div className="nav-project-block">
          <div className="project-switcher"><span className="project-symbol"><Icon name="rain" /></span><span><strong>{project.name}</strong><small>{project.modelFile}</small></span></div>
          <div className={`fitness-badge fitness-${currentFitnessDisposition}`}><span /><div><small>Model fitness</small><strong>{fitnessLabel}</strong></div></div>
        </div>

        <ol className="step-list">
          {steps.map((item, index) => (
            <li key={item.id}>
              <button
                className={step === item.id ? "active" : completedSteps.has(item.id) ? "complete" : ""}
                onClick={() => navigate(item.id)}
                aria-current={step === item.id ? "step" : undefined}
                aria-label={`${index + 1}. ${item.label}: ${isGuided ? item.short : item.id === "run" && practitionerRun.runPlan ? `${practitionerRun.runPlan.total_exact_jobs} exact jobs` : item.id === "compare" && practitionerRun.analysisId ? "Exact evidence" : item.id === "export" ? "Repository gates" : setupStepLabels[item.id]}`}
                disabled={index > unlockedIndex}
                title={index > unlockedIndex ? "Complete the previous checkpoint first" : undefined}
              >
                <span className="step-marker">{completedSteps.has(item.id) ? <Icon name="check" /> : index + 1}</span>
                <span><strong>{item.label}</strong><small>{isGuided ? item.short : item.id === "run" && practitionerRun.runPlan ? `${practitionerRun.runPlan.total_exact_jobs} exact jobs` : item.id === "compare" && practitionerRun.analysisId ? "Exact evidence" : item.id === "export" ? "Repository gates" : setupStepLabels[item.id]}</small></span>
                {step === item.id && <i />}
              </button>
            </li>
          ))}
        </ol>

        <div className="nav-bottom">
          <button onClick={() => setRailCollapsed(!railCollapsed)} className="collapse-nav" aria-expanded={!railCollapsed} aria-label={railCollapsed ? "Expand workflow steps" : "Collapse workflow steps"}><Icon name="chevron" /><span>{railCollapsed ? "Expand steps" : "Collapse steps"}</span></button>
          <div className="cloud-nav-status">
            <button className="local-mode" onClick={handleCloudAction} disabled={projectCloudSyncBusy} aria-label={`${cloudLabel}: ${cloudDetail}. ${cloudActionLabel}`}><span className={`observability-dot ${cloudTone}`}/><div><strong>{projectCloudSyncBusy ? "Checking sync…" : cloudLabel}</strong><small>{cloudDetail}</small><span className="cloud-action-hint">{cloudActionLabel}</span></div></button>
            {projectCloudSyncError && <p className="cloud-sync-error" role="alert">Status refresh failed. Local work remains available.</p>}
          </div>
        </div>
      </nav>

      <main className="workspace-main" id="main-content">
        {(project.kind === "conceptual" || project.kind === "demo") && (
          <div className={`fitness-ribbon ${project.kind}`}>
            <Icon name={project.kind === "demo" ? "spark" : "warning"} />
            <strong>{project.kind === "demo" ? "Teaching model" : "Conceptual planning model"}</strong>
            <span>{project.kind === "demo" ? "For learning and interface evaluation—not a professional model." : "Not for design, permitting, flood-risk determination, or representation of existing drainage infrastructure."}</span>
          </div>
        )}
        {!isGuided && (
          <div className="setup-preview-notice" role="status">
            <Icon name="info" />
            <strong>{project.bundle ? practitionerRun.analysisId ? "Worker-backed exact analysis" : "Imported source · explicit setup required" : "Setup preview"}</strong>
            <span>{project.bundle ? practitionerRun.analysisId ? "Runs, QA, comparisons, and evidence resolve from the authoritative local repository. Cloud synchronization remains a separate mirror." : "Confirm the storm models, approved LID slots, and exact design vectors before the worker can create a durable run plan." : "No project bundle or GIS source has been loaded. Exact analysis requires authoritative source data."}</span>
          </div>
        )}
        <div className="workspace-content">
          {step === "overview" && isGuided && <OverviewScreen data={data as GuidedData} observability={observability} projectCloudSync={projectCloudSync} onRefreshProjectCloudSync={onRefreshProjectCloudSync} onObservability={onObservability} onEvidence={showEvidence} onStep={advance} />}
          {step === "model" && project.kind === "existing" && <PractitionerModelScreen project={project} validation={practitionerValidation} passport={fitnessPassport} busy={practitionerValidationBusy} fitnessBusy={fitnessBusy} error={practitionerValidationError} fitnessError={fitnessError} onSelectBaseModel={selectPractitionerBaseModel} onValidate={validatePractitionerModel} onAttest={attestPractitionerFitness} onEvidence={() => showEvidence("import-model")} onStep={advance} />}
          {step === "model" && project.kind !== "existing" && <ModelScreen data={data ?? undefined} kind={project.kind} project={project} fitnessDisposition={project.fitnessDisposition} onEvidence={showEvidence} onStep={advance} />}
          {step === "storms" && project.kind === "existing" && <PractitionerStormsScreen storms={practitionerStorms} busy={practitionerValidationBusy} error={practitionerValidationError} onChange={updatePractitionerStorm} onValidate={validatePractitionerStorms} onStep={advance} />}
          {step === "storms" && project.kind !== "existing" && <StormsScreen data={data ?? undefined} isGuided={isGuided} onEvidence={showEvidence} onStep={advance} />}
          {step === "opportunities" && project.kind === "existing" && <PractitionerOpportunitiesScreen candidates={practitionerCandidates} validation={practitionerValidation} onAdd={() => { setPractitionerCandidates((current) => [...current, emptyCandidateSlot(current.length)]); resetPlannedAnalysis(); }} onChange={(index, patch) => { setPractitionerCandidates((current) => current.map((candidate, itemIndex) => itemIndex === index ? { ...candidate, ...patch } : candidate)); resetPlannedAnalysis(); }} onRemove={(index) => { setPractitionerCandidates((current) => current.filter((_, itemIndex) => itemIndex !== index)); setPractitionerVectors((current) => current.map((vector) => vector.filter((_, itemIndex) => itemIndex !== index))); resetPlannedAnalysis(); }} onStep={advance} />}
          {step === "opportunities" && project.kind !== "existing" && <OpportunitiesScreen data={data ?? undefined} isGuided={isGuided} onEvidence={showEvidence} onStep={advance} />}
          {step === "goals" && project.kind === "existing" && <PractitionerGoalsScreen slots={practitionerSlots} vectors={practitionerVectors} budget={practitionerBudget} adverseTolerance={adverseTolerance} onVectors={(vectors) => { setPractitionerVectors(vectors); resetPlannedAnalysis(); }} onBudget={(value) => { setPractitionerBudget(value); resetPlannedAnalysis(); }} onAdverseTolerance={(value) => { setAdverseTolerance(value); setDecisionBoard(null); setExportEligibility(null); }} onStep={advance} />}
          {step === "goals" && project.kind !== "existing" && <GoalsScreen isGuided={isGuided} onEvidence={showEvidence} onStep={advance} />}
          {step === "run" && isGuided && <RunScreen data={data as GuidedData} runtime={guidedRun} canRerun={canRunLocally} controlBusy={runControlBusy} onStartRerun={startGuidedRerun} onPauseRerun={() => transitionGuidedRun("guided.pause")} onResumeRerun={() => transitionGuidedRun("guided.resume")} onCancelRerun={() => transitionGuidedRun("guided.cancel")} onEvidence={showEvidence} onStep={advance} />}
          {step === "run" && project.kind === "existing" && <PractitionerRunScreen stormCount={selectedPractitionerStorms.length} vectorCount={practitionerVectors.length} budget={practitionerBudget} runtime={practitionerRun} busy={practitionerRunBusy} onPlan={planPractitionerAnalysis} onStart={startPractitionerAnalysis} onPause={() => transitionPractitionerAnalysis("analysis.pause")} onResume={() => transitionPractitionerAnalysis("analysis.resume")} onCancel={() => transitionPractitionerAnalysis("analysis.cancel")} onStep={advance} />}
          {step === "compare" && isGuided && <CompareScreen data={data as GuidedData} onEvidence={showEvidence} onStep={advance} />}
          {step === "compare" && project.kind === "existing" && <PractitionerCompareScreen board={decisionBoard} loading={decisionBoardBusy} error={decisionBoardError} onRefresh={loadDecisionBoard} onEvidence={showWorkerRunEvidence} onStep={advance} />}
          {step === "export" && isGuided && <ExportScreen data={data as GuidedData} projectKind={project.kind} onEvidence={showEvidence} />}
          {step === "export" && project.kind === "existing" && <PractitionerExportScreen passport={fitnessPassport} eligibility={exportEligibility} created={createdExport} mode={exportMode} includeArtifacts={includeExportArtifacts} busy={exportBusy} error={exportError} exactJobCount={succeededPractitionerJobIds.length} onMode={(mode) => { setExportMode(mode); setExportEligibility(null); setCreatedExport(null); }} onIncludeArtifacts={setIncludeExportArtifacts} onValidate={validatePractitionerExport} onCreate={createPractitionerExport} />}
        </div>
      </main>

      {!evidenceOpen && (
        <button className="evidence-tab" onClick={(event) => { evidenceReturnFocus.current = event.currentTarget; setEvidenceOpen(true); }} aria-label="Open evidence rail"><Icon name="trace" /><span>Evidence</span></button>
      )}
      <EvidenceRail
        item={evidenceItem}
        projectId={project.id}
        projectDirectory={project.projectDirectory}
        analysisId={practitionerRun.analysisId}
        modelRevisionId={project.bundle?.currentModelRevisionId}
        projectKind={project.kind}
        observability={observability}
        projectCloudSync={projectCloudSync}
        open={evidenceOpen}
        onClose={closeEvidence}
        onEvidenceReference={openAssistantEvidenceReference}
        onConfigureObservability={onObservability}
      />
    </div>
  );
}
