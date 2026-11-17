import { useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";
import { evidenceFor, fixtureIsManifestVerified, parseRidgewayFixture } from "../data/ridgeway";
import { PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256 } from "../data/trustedManifests";
import type { AppMode, EntryPath, EvidenceSelection, GuidedDesktopSession, GuidedEventDraft, GuidedOpenData, ObservabilityPreflight, PlanningState, ProfessionalAnalysisWorkspace, ProfessionalAssetRecord, ProfessionalGridApproval, ProfessionalModelRecords, ProfessionalProjectCandidate, ProfessionalProjectSession, ReopenableProject, RidgewayFixture, RidgewayPortfolio, RpcPayload, WorkflowStepId } from "../types";
import { createProjectWorkspace, hasNativeHost, hostErrorMessage, hostRequest, rpcData } from "../lib/host";
import { ObservabilityConnectResponseSchema } from "../lib/observabilityContracts";
import { AnalysisStatusResponseSchema, DecisionBoardResponseSchema, hydrateProfessionalProject } from "../lib/professional";
import { stepsFor } from "../workflow";
import { EvidencePanel } from "./EvidencePanel";
import { CorridorMark, Icon } from "./Icon";
import { ScreenContent } from "./Screens";
import { WorkspaceDraftProvider } from "./WorkspaceDrafts";

interface WorkspaceProps {
  mode: AppMode;
  path: EntryPath;
  localPreview: boolean;
  preflight: ObservabilityPreflight | null;
  reopenedProject?: ReopenableProject | null;
  publicFixture?: RidgewayFixture | null;
  onConnectObservability?: (projectHandle: string | null) => void;
  onHome?: () => void;
}

const initialPlanning: PlanningState = {
  projectName: "",
  decisionQuestion: "",
  intendedUse: "",
  studyArea: "",
  reviewer: "",
  targetGuild: "",
  assetNames: [],
  alignmentMethod: "nearest",
  alignmentApproved: false,
  hypothesisRationale: "",
  focalRationale: "",
  interventionBasis: "",
  safeguardNotes: "",
  budgetUsd: "",
};

const initialProfessionalAnalysis: ProfessionalAnalysisWorkspace = {
  projectState: null,
  preparation: null,
  plannedAnalysisId: null,
  status: null,
  decisionBoard: null,
  selectedPortfolioId: null,
  exportEligibility: null,
  exportRecord: null,
};

const SyntheticTraceConnectResponseSchema = z.object({
  connected: z.literal(true),
  state: z.literal("SYNTHETIC_TRACE_TARGET_VERIFIED"),
  binding_sha256: z.string().regex(/^[0-9a-f]{64}$/),
  entity: z.string().min(1),
  project: z.string().min(1),
  deployment: z.enum(["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]),
  visibility_claim: z.literal("NONE"),
  restricted_verified: z.literal(false),
  full_sync_allowed: z.literal(false),
  artifact_upload_allowed: z.literal(false),
  weave_capability: z.literal("PENDING_NATIVE_AGENT_PREFLIGHT"),
  message: z.string().min(1),
}).strict();

function reopenedSession(project: ReopenableProject): ProfessionalProjectSession {
  const state = project.projectState.sync_status["state"];
  const bindingState: ProfessionalProjectSession["bindingState"] = state === "CONNECTED"
    ? "CONNECTED"
    : state === "SYNCING"
      ? "SYNCING"
      : state === "SYNC_QUEUED_OFFLINE"
        ? "SYNC_QUEUED_OFFLINE"
        : state === "BLOCKED_PROJECT_POLICY" || state === "BLOCKED"
          ? "BLOCKED_PROJECT_POLICY"
          : state === "FAILED_RETRYING"
            ? "FAILED_RETRYING"
            : "AUTHENTICATION_REQUIRED";
  const mode = project.projectState.project.mode === "PLANNING_DRAFT" ? "PLANNING_DRAFT" : "IMPORTED";
  return {
    projectHandle: project.projectHandle,
    projectId: project.projectState.project.project_id,
    title: project.projectState.project.title,
    mode,
    bindingState,
    bindingMessage: typeof project.projectState.sync_status["message"] === "string"
      ? project.projectState.sync_status["message"]
      : bindingState === "CONNECTED"
        ? "Previously verified Restricted destination restored. Local evidence remains authoritative."
        : "The local project is open. Cloud synchronization remains fail-closed until its saved binding is usable.",
  };
}

function retainedObservabilityDeployment(projectState: ProfessionalAnalysisWorkspace["projectState"]): "DEDICATED" | "SELF_MANAGED" | null {
  const deployment = projectState?.sync_status["deployment"];
  return deployment === "DEDICATED" || deployment === "SELF_MANAGED" ? deployment : null;
}

export function Workspace({ mode, path, localPreview, preflight, reopenedProject = null, publicFixture = null, onConnectObservability, onHome }: WorkspaceProps) {
  const publicMode = mode === "public-replay";
  const steps = useMemo(() => stepsFor(path), [path]);
  const [step, setStep] = useState<WorkflowStepId>("overview");
  const [visitedSteps, setVisitedSteps] = useState<Set<WorkflowStepId>>(() => new Set(["overview"]));
  const [planning, setPlanning] = useState<PlanningState>(initialPlanning);
  const [projectSession, setProjectSession] = useState<ProfessionalProjectSession | null>(() => reopenedProject ? reopenedSession(reopenedProject) : null);
  const [professionalAssets, setProfessionalAssets] = useState<ProfessionalAssetRecord[]>([]);
  const [professionalGridApprovals, setProfessionalGridApprovals] = useState<ProfessionalGridApproval[]>([]);
  const [professionalRecords, setProfessionalRecords] = useState<ProfessionalModelRecords>({ hypotheses: [], focalAreaSets: [], scenarios: [], interventionSites: [], interventionOptions: [], safeguards: [], reviewMasks: [], decisionRules: [], decisionBrief: null, costBasis: null, resourcePolicy: null });
  const [professionalAnalysis, setProfessionalAnalysis] = useState<ProfessionalAnalysisWorkspace>(initialProfessionalAnalysis);
  const [hydrationState, setHydrationState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [hydrationMessage, setHydrationMessage] = useState("");
  const [guidedSession, setGuidedSession] = useState<GuidedDesktopSession | null>(null);
  const [guidedRecordState, setGuidedRecordState] = useState<"UNAVAILABLE" | "OPENING" | "ACTIVE" | "FAILED">("UNAVAILABLE");
  const [guidedFixture, setGuidedFixture] = useState<RidgewayFixture | null>(null);
  const [guidedProjectionError, setGuidedProjectionError] = useState<string | null>(null);
  // A professional/full-sync status can never prove the separate Ridgeway
  // trace-only consent. Reopened guided projects therefore start fail-closed
  // until the dedicated synthetic target is verified in this process.
  const [guidedBindingState, setGuidedBindingState] = useState<"UNBOUND" | "CONNECTING" | "CONNECTED" | "BLOCKED">("UNBOUND");
  const [guidedBindingMessage, setGuidedBindingMessage] = useState("");
  const activeFixture = publicMode ? publicFixture : guidedFixture;
  const fixtureVerified = activeFixture
    ? fixtureIsManifestVerified(activeFixture, publicMode ? PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256 : guidedSession?.evidenceManifestSha256 ?? null)
    : false;
  const [selectedPortfolio, setSelectedPortfolio] = useState<RidgewayPortfolio | null>(null);
  const [evidence, setEvidence] = useState<EvidenceSelection | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const guidedSessionRef = useRef<GuidedDesktopSession | null>(null);
  const guidedSequenceRef = useRef(0);
  const guidedQueueRef = useRef<Promise<void>>(Promise.resolve());
  const pendingGuidedEventsRef = useRef<GuidedEventDraft[]>([]);
  const guidedOpeningRef = useRef(false);
  const reopenedBindingRef = useRef<string | null>(null);
  const guidedBindingRef = useRef<string | null>(null);
  const stepStartedAtRef = useRef(performance.now());
  const currentIndex = steps.findIndex((item) => item.id === step);
  const previous = currentIndex > 0 ? steps[currentIndex - 1] : null;
  const next = currentIndex < steps.length - 1 ? steps[currentIndex + 1] : null;
  const evidenceAvailable = (path === "guided" && Boolean(activeFixture && selectedPortfolio))
    || (path !== "guided" && Boolean(evidence));
  const observabilityDeployment = path === "guided"
    ? null
    : preflight?.deployment === "DEDICATED" || preflight?.deployment === "SELF_MANAGED"
      ? preflight.deployment
      : retainedObservabilityDeployment(professionalAnalysis.projectState);

  useEffect(() => {
    setProfessionalAnalysis(initialProfessionalAnalysis);
    if (path !== "guided") {
      setEvidence(null);
      setEvidenceOpen(false);
    }
  }, [path, projectSession?.projectHandle]);

  useEffect(() => {
    if (path === "guided" || !projectSession) return;
    let active = true;
    setHydrationState("loading");
    setHydrationMessage("Reading the authoritative project snapshot…");
    void (async () => {
      try {
        const response = await hostRequest<RpcPayload<unknown>>("connectivity.project.get_state", {
          project_handle: projectSession.projectHandle,
        });
        const hydrated = hydrateProfessionalProject(rpcData(response));
        if (hydrated.state.project.project_id !== projectSession.projectId) {
          throw new Error("The reopened project identity differs from its authoritative local record.");
        }
        if (!active) return;
        setPlanning(hydrated.planning);
        setProfessionalAssets(hydrated.assets);
        setProfessionalGridApprovals(hydrated.gridApprovals);
        setProfessionalRecords(hydrated.records);
        let analysisStatus: ProfessionalAnalysisWorkspace["status"] = null;
        let decisionBoard: ProfessionalAnalysisWorkspace["decisionBoard"] = null;
        let analysisRestoreMessage = "";
        if (hydrated.activeAnalysisId) {
          try {
            const statusResponse = await hostRequest<RpcPayload<unknown>>("connectivity.analysis.status", {
              project_handle: projectSession.projectHandle,
              analysis_id: hydrated.activeAnalysisId,
            });
            analysisStatus = AnalysisStatusResponseSchema.parse(rpcData(statusResponse));
            if (analysisStatus.analysis_id !== hydrated.activeAnalysisId) {
              throw new Error("The restored analysis status belongs to a different immutable plan.");
            }
            if (analysisStatus.state === "COMPLETED") {
              const boardResponse = await hostRequest<RpcPayload<unknown>>("connectivity.results.decision_board", {
                project_handle: projectSession.projectHandle,
                analysis_id: hydrated.activeAnalysisId,
                portfolio_ids: [],
              });
              decisionBoard = DecisionBoardResponseSchema.parse(rpcData(boardResponse));
              if (decisionBoard.analysis_id !== hydrated.activeAnalysisId) {
                throw new Error("The restored decision board belongs to a different immutable plan.");
              }
            }
          } catch (reason) {
            analysisRestoreMessage = hostErrorMessage(reason, "The saved analysis detail could not be restored; its immutable identity remains available for explicit recovery.");
          }
        }
        if (!active) return;
        setProfessionalAnalysis({
          ...initialProfessionalAnalysis,
          projectState: hydrated.state,
          plannedAnalysisId: hydrated.activeAnalysisId,
          status: analysisStatus,
          decisionBoard,
          selectedPortfolioId: decisionBoard?.summaries[0]?.portfolio_id ?? null,
        });
        if (reopenedProject && (hydrated.activeAnalysisId || hydrated.referenceRecoveryAnalysisId)) {
          const restoredStep = hydrated.referenceRecoveryAnalysisId
            ? "model-fitness"
            : analysisStatus?.state === "COMPLETED" && decisionBoard ? "robustness" : "direct-runs";
          setVisitedSteps((current) => new Set(current).add(restoredStep));
          setStep(restoredStep);
        }
        setHydrationState("ready");
        setHydrationMessage(hydrated.referenceRecoveryAnalysisId
          ? "Authoritative records restored. An interrupted exact reference comparison is ready for explicit recovery in Model Fitness."
          : hydrated.recoveryAnalysisId
          ? `Authoritative records restored. An interrupted analysis is ready for explicit resume or cancellation.${analysisRestoreMessage ? ` ${analysisRestoreMessage}` : ""}`
          : analysisStatus?.state === "COMPLETED" && decisionBoard
            ? `Authoritative records and completed decision board restored for ${analysisStatus.analysis_id}.`
            : `Authoritative records restored: ${hydrated.assets.length} assets, ${hydrated.records.scenarios.length} scenarios, and ${hydrated.records.interventionOptions.length} intervention options.${analysisRestoreMessage ? ` ${analysisRestoreMessage}` : ""}`);
      } catch (reason) {
        if (!active) return;
        setHydrationState("error");
        setHydrationMessage(hostErrorMessage(reason, "The authoritative project records could not be restored. Professional actions remain unavailable."));
      }
    })();
    return () => { active = false; };
  }, [path, projectSession?.projectHandle]);

  useEffect(() => {
    if (!activeFixture) {
      setSelectedPortfolio(null);
      setEvidence(null);
      setEvidenceOpen(false);
      return;
    }
    const initial = activeFixture.portfolios.find((item) => item.role === "carry-forward") ?? activeFixture.portfolios[0] ?? null;
    setSelectedPortfolio(initial);
    setEvidence(initial ? evidenceFor(activeFixture, initial, fixtureVerified) : null);
  }, [activeFixture, fixtureVerified]);

  const queueGuidedEvent = (session: GuidedDesktopSession, event: GuidedEventDraft) => {
    const sequenceNumber = ++guidedSequenceRef.current;
    const eventId = `${session.sessionId}-${String(sequenceNumber).padStart(4, "0")}-${event.eventKind.toLowerCase()}-${event.checkpointId}`;
    const work = guidedQueueRef.current.then(async () => {
      await hostRequest("connectivity.guided.checkpoint", {
        project_handle: session.projectHandle,
        session_id: session.sessionId,
        event_id: eventId,
        sequence_number: sequenceNumber,
        checkpoint_id: event.checkpointId,
        event_kind: event.eventKind,
        duration_ms: event.durationMs,
        evidence_ids: event.evidenceIds ?? [],
        portfolio_ids: event.portfolioIds ?? [],
      });
      setGuidedRecordState("ACTIVE");
    });
    guidedQueueRef.current = work.catch(() => {
      setGuidedRecordState("FAILED");
    });
  };

  const emitGuidedEvent = (event: GuidedEventDraft) => {
    if (publicMode || path !== "guided") return;
    const session = guidedSessionRef.current;
    if (session) queueGuidedEvent(session, event);
    else pendingGuidedEventsRef.current.push(event);
  };

  useEffect(() => {
    if (publicMode || path !== "guided" || guidedOpeningRef.current) return;
    if (localPreview || !hasNativeHost()) {
      setGuidedProjectionError("The signed desktop host and scientific worker are unavailable. No bundled UI outcome is substituted.");
      return;
    }
    guidedOpeningRef.current = true;
    setGuidedRecordState("OPENING");
    void (async () => {
      try {
        const workspace = reopenedProject?.projectState.project.mode === "GUIDED"
          ? { handle: reopenedProject.projectHandle }
          : await createProjectWorkspace();
        const openedResult = await hostRequest<RpcPayload<GuidedOpenData>>("connectivity.guided.open", {
          project_handle: workspace.handle,
        });
        const opened = rpcData(openedResult);
        const session: GuidedDesktopSession = {
          sessionId: opened.session_id,
          projectHandle: workspace.handle,
          evidenceManifestSha256: opened.evidence_manifest_sha256,
          canRerun: opened.can_rerun,
          initialRerunState: opened.state,
        };
        guidedSessionRef.current = session;
        setGuidedSession(session);
        setGuidedRecordState("ACTIVE");
        try {
          const parsed = parseRidgewayFixture(opened.projection);
          if (opened.state === "ENGINE_REQUIRED" || !fixtureIsManifestVerified(parsed, opened.evidence_manifest_sha256)) {
            throw new Error("The worker did not return a digest-matched direct-evidence replay.");
          }
          setGuidedFixture(parsed);
          setGuidedProjectionError(null);
          if (reopenedProject?.projectState.project.mode === "GUIDED" && opened.state !== "VERIFIED_FIXTURE_REPLAY") {
            setVisitedSteps((current) => new Set(current).add("direct-runs"));
            setStep("direct-runs");
          }
        } catch (reason) {
          setGuidedFixture(null);
          setGuidedProjectionError(hostErrorMessage(reason, "The authoritative Ridgeway projection is unavailable."));
        }
        queueGuidedEvent(session, {
          checkpointId: reopenedProject?.projectState.project.mode === "GUIDED" && opened.state !== "VERIFIED_FIXTURE_REPLAY" ? "direct-runs" : "overview",
          eventKind: "ENTERED",
        });
        const pending = pendingGuidedEventsRef.current.splice(0);
        for (const event of pending) queueGuidedEvent(session, event);
      } catch (reason) {
        setGuidedRecordState("FAILED");
        setGuidedFixture(null);
        setGuidedProjectionError(hostErrorMessage(reason, "The authoritative Ridgeway projection could not be opened."));
      }
    })();
  }, [localPreview, path, publicMode, reopenedProject?.projectHandle]);

  useEffect(() => {
    if (publicMode || path !== "guided" || !guidedSession || !preflight) return;
    if (
      preflight.purpose !== "RIDGEWAY_SYNTHETIC_TRACE_ONLY"
      || preflight.projectHandle !== guidedSession.projectHandle
    ) {
      setGuidedBindingState("BLOCKED");
      setGuidedBindingMessage("The trace-only preflight does not belong to this exact opened Ridgeway project. No cloud capability was enabled.");
      return;
    }
    const attemptKey = `${guidedSession.projectHandle}:${preflight.preflightId}`;
    if (guidedBindingRef.current === attemptKey) return;
    guidedBindingRef.current = attemptKey;
    setGuidedBindingState("CONNECTING");
    setGuidedBindingMessage("Binding the optional synthetic trace destination…");
    void hostRequest<RpcPayload<unknown>>("observability.synthetic_trace.connect", {
      project_handle: guidedSession.projectHandle,
      preflight_id: preflight.preflightId,
    }).then((response) => {
      const data = SyntheticTraceConnectResponseSchema.parse(rpcData(response));
      if (data.entity !== preflight.entity || data.project !== preflight.project || data.deployment !== preflight.deployment) {
        throw new Error("The trace-only W&B destination differs from the exact project preflight.");
      }
      setGuidedBindingState("CONNECTED");
      setGuidedBindingMessage("Trace-only connected. Project visibility was not asserted, full synchronization and artifact upload remain disabled, and the local Ridgeway evidence stays authoritative.");
    }).catch((reason: unknown) => {
      setGuidedBindingState("BLOCKED");
      setGuidedBindingMessage(hostErrorMessage(reason, "The optional synthetic trace destination could not be bound."));
    });
  }, [guidedSession?.projectHandle, path, preflight?.preflightId, preflight?.projectHandle, preflight?.purpose, publicMode]);

  useEffect(() => {
    const heading = mainRef.current?.querySelector<HTMLElement>("h1");
    heading?.focus();
    mainRef.current?.scrollTo({ top: 0, behavior: "auto" });
    setNavOpen(false);
  }, [step]);

  const navigate = (target: WorkflowStepId) => {
    if (!steps.some((item) => item.id === target)) return;
    if (target !== step) {
      const targetIndex = steps.findIndex((item) => item.id === target);
      const elapsed = Math.min(86_400_000, Math.max(0, Math.round(performance.now() - stepStartedAtRef.current)));
      if (targetIndex < currentIndex) {
        emitGuidedEvent({ checkpointId: target, eventKind: "BACKTRACKED", durationMs: elapsed });
      } else {
        emitGuidedEvent({ checkpointId: step, eventKind: "COMPLETED", durationMs: elapsed });
      }
      emitGuidedEvent({ checkpointId: target, eventKind: "ENTERED" });
      stepStartedAtRef.current = performance.now();
    }
    setVisitedSteps((visited) => new Set(visited).add(target));
    setStep(target);
  };

  const showEvidence = (selection: EvidenceSelection) => {
    setEvidence(selection);
    setEvidenceOpen(true);
    emitGuidedEvent({
      checkpointId: step,
      eventKind: "EVIDENCE_OPENED",
      evidenceIds: selection.status === "verified" ? [selection.claimId] : [],
      portfolioIds: selection.portfolioId.startsWith("unassigned:") ? [] : [selection.portfolioId],
    });
  };

  const comparePortfolio = (portfolio: RidgewayPortfolio) => {
    if (portfolio.projectionKey !== selectedPortfolio?.projectionKey) {
      emitGuidedEvent({
        checkpointId: "robustness",
        eventKind: "ALTERNATIVE_COMPARED",
        portfolioIds: portfolio.id ? [portfolio.id] : [],
      });
    }
    setSelectedPortfolio(portfolio);
  };

  const bindProject = async (candidate: ProfessionalProjectCandidate) => {
    if (!preflight || preflight.purpose !== "PROFESSIONAL_FULL_SYNC" || preflight.projectHandle !== null) {
      setProjectSession({ ...candidate, bindingState: "FAILED", bindingMessage: "The exact professional Restricted-destination preflight is missing or belongs to another capability. Return home and verify it again." });
      return;
    }
    setProjectSession({ ...candidate, bindingState: "CONNECTING", bindingMessage: "Binding the verified destination to this local project…" });
    try {
      const result = await hostRequest<RpcPayload<unknown>>("observability.connect", {
        project_handle: candidate.projectHandle,
        preflight_id: preflight.preflightId,
      });
      const data = ObservabilityConnectResponseSchema.parse(rpcData(result));
      if (data.binding.entity !== preflight.entity || data.binding.project !== preflight.project || data.binding.deployment !== preflight.deployment) {
        throw new Error("The bound W&B destination differs from the exact destination preflight.");
      }
      const bindingState: ProfessionalProjectSession["bindingState"] = data.binding.state === "BLOCKED" || data.binding.state === "BLOCKED_PROJECT_POLICY"
        ? "BLOCKED_PROJECT_POLICY"
        : data.binding.state;
      const blocked = bindingState === "BLOCKED_PROJECT_POLICY" || !data.upload_preview.allowed;
      setProjectSession({
        ...candidate,
        bindingState: blocked ? "BLOCKED_PROJECT_POLICY" : bindingState,
        bindingMessage: blocked
          ? data.upload_preview.message ?? `Destination bound; synchronization is blocked by: ${(data.upload_preview.blockers ?? ["incomplete asset policy"]).join(", ")}.`
          : bindingState === "SYNC_QUEUED_OFFLINE"
            ? "Restricted destination bound. Committed evidence remains queued locally until synchronization resumes."
            : bindingState === "SYNCING"
              ? "Restricted destination bound. Committed evidence synchronization is active."
              : bindingState === "FAILED_RETRYING"
                ? "Restricted destination bound. A committed upload failed and remains queued for retry."
                : bindingState === "AUTHENTICATION_REQUIRED"
                  ? "Restricted destination identity is retained, but synchronization requires authentication."
                  : "Restricted destination bound. Local evidence remains authoritative.",
      });
    } catch (reason) {
      setProjectSession({ ...candidate, bindingState: "FAILED", bindingMessage: hostErrorMessage(reason, "The local project was created, but its W&B destination could not be bound.") });
    }
  };

  useEffect(() => {
    if (!reopenedProject || !preflight) return;
    const attemptKey = `${reopenedProject.projectHandle}:${preflight.preflightId}`;
    if (reopenedBindingRef.current === attemptKey) return;
    reopenedBindingRef.current = attemptKey;
    void bindProject({
      projectHandle: reopenedProject.projectHandle,
      projectId: reopenedProject.projectState.project.project_id,
      title: reopenedProject.projectState.project.title,
      mode: reopenedProject.projectState.project.mode === "PLANNING_DRAFT" ? "PLANNING_DRAFT" : "IMPORTED",
    });
  }, [preflight?.preflightId, reopenedProject?.projectHandle]);

  const projectName = path === "guided" ? "Ridgeway Crossing" : projectSession?.title ?? (path === "planning" ? planning.projectName || "Untitled planning model" : "Reviewed model workspace");
  const flowLabel = path === "guided" ? (publicMode ? "Public synthetic replay" : "Synthetic guided replay") : path === "planning" ? "Conceptual planning assembly" : "Reviewed-model analysis";

  return (
    <div className={`workspace-shell${evidenceOpen ? " evidence-visible" : ""}`}>
      <header className="workspace-header">
        <button className="brand-button" type="button" onClick={onHome} disabled={!onHome} aria-label={onHome ? "Return to EcoCorridor home" : "EcoCorridor public replay"}>
          <CorridorMark compact /><span><b>EcoCorridor</b><small>Scenario Studio</small></span>
        </button>
        <div className="project-heading"><strong>{projectName}</strong><span>{flowLabel}</span></div>
        <div className="header-actions">
          {publicMode ? <span className="cloud-state public"><Icon name="eye" /> No login · no cloud</span> : localPreview ? <span className="cloud-state preview"><Icon name="info" /> Local UI preview</span> : path === "guided" ? guidedBindingState === "CONNECTED" ? <span className="cloud-state connected" title={guidedBindingMessage}><Icon name="cloud" /> Trace-only connected · no full sync</span> : guidedBindingState === "CONNECTING" ? <span className="cloud-state preview"><Icon name="route" /> Connecting trace-only target</span> : <span className={`cloud-state ${guidedRecordState === "FAILED" ? "preview" : "local"}`} title={guidedBindingMessage || "Ridgeway remains an offline-capable local replay."}><Icon name={guidedRecordState === "FAILED" ? "warning" : "shield"} /> {guidedRecordState === "ACTIVE" ? "Guided record active" : guidedRecordState === "OPENING" ? "Opening guided record" : guidedRecordState === "FAILED" ? "Guided record unavailable" : "Synthetic local replay"}</span> : projectSession?.bindingState === "CONNECTED" ? <span className="cloud-state connected"><Icon name="cloud" /> Restricted project connected</span> : projectSession?.bindingState === "SYNCING" ? <span className="cloud-state connected"><Icon name="cloud" /> Synchronizing evidence</span> : projectSession?.bindingState === "SYNC_QUEUED_OFFLINE" ? <span className="cloud-state preview"><Icon name="shield" /> Sync queued offline</span> : projectSession?.bindingState === "BLOCKED_PROJECT_POLICY" ? <span className="cloud-state preview"><Icon name="warning" /> Sync blocked by asset policy</span> : projectSession?.bindingState === "AUTHENTICATION_REQUIRED" ? <span className="cloud-state preview" title={projectSession.bindingMessage}><Icon name="warning" /> Authentication required</span> : projectSession?.bindingState === "FAILED_RETRYING" ? <span className="cloud-state preview" title={projectSession.bindingMessage}><Icon name="warning" /> Sync failed · retry queued</span> : projectSession?.bindingState === "FAILED" ? <button className="cloud-state preview cloud-retry" type="button" onClick={() => void bindProject(projectSession)} title={projectSession.bindingMessage}><Icon name="warning" /> Retry destination binding</button> : <span className="cloud-state preview"><Icon name="shield" /> Restricted destination preflight</span>}
          <button className="evidence-button" type="button" disabled={!evidenceAvailable} title={evidenceAvailable ? undefined : "Evidence opens after a direct project claim exists."} onClick={() => setEvidenceOpen(true)} aria-expanded={evidenceOpen} aria-label="Open evidence details"><Icon name="evidence" /><span>Evidence</span></button>
          <button className="nav-toggle icon-button" type="button" onClick={() => setNavOpen((open) => !open)} aria-expanded={navOpen} aria-controls="workflow-navigation" aria-label="Toggle workflow navigation"><Icon name="route" /></button>
        </div>
      </header>

      <nav id="workflow-navigation" className={`workflow-navigation${navOpen ? " is-open" : ""}`} aria-label="Project workflow">
        <div className="nav-project">
          <span className="section-kicker">Project path</span>
          <strong>{path === "guided" ? "Ridgeway lesson" : path === "planning" ? "Planning assembly" : "Reviewed model"}</strong>
          <small>{path === "guided" ? "Synthetic · CC0" : localPreview ? "Preview only" : "Professional workspace"}</small>
        </div>
        <ol className="workflow-steps">
          {steps.map((item, index) => {
            const active = item.id === step;
            const complete = visitedSteps.has(item.id) && !active;
            const phaseBreak = index === 0 || steps[index - 1]?.phase !== item.phase;
            return (
              <li key={item.id} className={phaseBreak ? "phase-break" : ""}>
                {phaseBreak && <span className="phase-label">{item.phase}</span>}
                <button type="button" className={active ? "is-active" : complete ? "is-complete" : ""} onClick={() => navigate(item.id)} aria-current={active ? "step" : undefined} aria-label={item.label} title={item.label}>
                  <span className="step-marker">{complete ? <Icon name="check" /> : String(index + 1).padStart(2, "0")}</span>
                  <span>{item.label}</span>
                </button>
              </li>
            );
          })}
        </ol>
        <div className="nav-boundary"><Icon name="shield" /><p>Direct model evidence<br /><span>Ecological judgment stays human.</span></p></div>
      </nav>

      <main ref={mainRef} className="workspace-main" id="main-content">
        {publicMode && <div className="public-replay-bar"><Icon name="eye" /><span><strong>Public synthetic decision story.</strong> Static CC0 projection only; no professional data, model upload, solver execution, account, or cloud request.</span></div>}
        <div className="workspace-content">
          {path !== "guided" && projectSession && hydrationState !== "idle" && (hydrationState !== "ready" || reopenedProject) && (
            <div className={`project-hydration ${hydrationState}`} role={hydrationState === "error" ? "alert" : "status"}>
              <Icon name={hydrationState === "error" ? "warning" : hydrationState === "ready" ? "check" : "route"} />
              <p><strong>{hydrationState === "loading" ? "Restoring local project" : hydrationState === "ready" ? "Local project restored" : "Local project could not be restored"}</strong><span>{hydrationMessage}</span></p>
            </div>
          )}
          {path !== "guided" && projectSession && hydrationState !== "ready" ? (
            <section className="paper-panel blocked-workspace project-restore-boundary">
              <Icon name={hydrationState === "error" ? "warning" : "lock"} />
              <div><h2>{hydrationState === "error" ? "Professional controls are locked." : "Rebuilding the professional workspace from SQLite."}</h2><p>{hydrationMessage} No empty editor state is substituted for retained project evidence.</p>{hydrationState === "error" && onHome && <button className="secondary-button" type="button" onClick={onHome}>Return to project home</button>}</div>
            </section>
          ) : <WorkspaceDraftProvider key={`${path}:${projectSession?.projectHandle ?? "unbound"}`}><ScreenContent
            path={path}
            step={step}
            publicMode={publicMode}
            localPreview={localPreview}
            projectSession={projectSession}
            onProjectReady={(candidate) => void bindProject(candidate)}
            planning={planning}
            onPlanning={setPlanning}
            selectedPortfolio={selectedPortfolio}
            onPortfolio={comparePortfolio}
            onEvidence={showEvidence}
            onNavigate={navigate}
            guidedSession={guidedSession}
            onGuidedEvent={emitGuidedEvent}
            professionalAssets={professionalAssets}
            professionalGridApprovals={professionalGridApprovals}
            professionalRecords={professionalRecords}
            onProfessionalRecords={setProfessionalRecords}
            professionalAnalysis={professionalAnalysis}
            onProfessionalAnalysis={setProfessionalAnalysis}
            observabilityDeployment={observabilityDeployment}
            observabilityConnected={path === "guided" ? guidedBindingState === "CONNECTED" : projectSession?.bindingState === "CONNECTED"}
            observabilityMessage={path === "guided" ? guidedBindingMessage : projectSession?.bindingMessage ?? ""}
            onConnectObservability={onConnectObservability ? () => onConnectObservability(path === "guided" ? guidedSession?.projectHandle ?? null : projectSession?.projectHandle ?? null) : undefined}
            fixture={activeFixture}
            fixtureVerified={fixtureVerified}
            guidedProjectionError={guidedProjectionError}
            onAssetPersisted={(asset) => setProfessionalAssets((current) => [...current.filter((item) => item.assetId !== asset.assetId), asset])}
            onGridApproved={(approval, asset) => {
              setProfessionalGridApprovals((current) => [...current.filter((item) => item.approvalId !== approval.approvalId), approval]);
              setProfessionalAssets((current) => [...current.filter((item) => item.assetId !== asset.assetId), asset]);
            }}
          /></WorkspaceDraftProvider>}
          {!(path !== "guided" && projectSession && hydrationState !== "ready") && <footer className="screen-navigation" aria-label="Workflow step navigation">
            <div>{previous && <button className="secondary-button" type="button" onClick={() => navigate(previous.id)}><span aria-hidden="true">←</span> {previous.label}</button>}</div>
            <p><span>{currentIndex + 1} / {steps.length}</span><strong>{steps[currentIndex]?.label}</strong></p>
            <div>{next ? <button className={path === "planning" ? "secondary-button" : "primary-button"} type="button" onClick={() => navigate(next.id)}>{next.label} <Icon name="arrow" /></button> : <button className="secondary-button" type="button" onClick={() => navigate("overview")}><Icon name="home" /> Return to overview</button>}</div>
          </footer>}
        </div>
      </main>

      {evidence && (activeFixture || evidence.source === "PROFESSIONAL_CLAIM") && <EvidencePanel open={evidenceOpen} selection={evidence} fixture={activeFixture} verified={evidence.source === "PROFESSIONAL_CLAIM" ? true : fixtureVerified} publicMode={publicMode} projectHandle={evidence.source === "PROFESSIONAL_CLAIM" ? projectSession?.projectHandle ?? null : guidedSession?.projectHandle ?? null} showAssistant={evidence.source === "PROFESSIONAL_CLAIM"} allowAssistantConfiguration={evidence.source === "PROFESSIONAL_CLAIM" && projectSession?.bindingState === "CONNECTED"} assistantUnavailableReason="Connect and verify this project's exact-scope Restricted W&B destination before configuring a traced provider." onClose={() => setEvidenceOpen(false)} />}
      {!evidenceOpen && evidenceAvailable && <button className="evidence-tab" type="button" onClick={() => setEvidenceOpen(true)}><Icon name="evidence" /><span>Trace a claim</span></button>}
      <div className="sr-only" aria-live="polite">Current workflow step: {steps[currentIndex]?.label}</div>
    </div>
  );
}
