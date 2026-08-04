import { useEffect, useState, type ReactNode } from "react";
import { serializeRedactedRidgewayDecisionStory } from "@ecocorridor-scenario-studio/evidence";
import { z } from "zod";
import { ConnectivityCostBasisSchema, ConnectivityDecisionBriefSchema, ConnectivityDecisionRuleSchema, ConnectivityModelFitnessSchema, ConnectivityScenarioSchema, FocalAreaSetSchema, InterventionOptionSchema, InterventionSiteSchema, ResistanceHypothesisSchema, ResourcePolicySchema, RiskReviewMaskSchema, SafeguardRecordSchema, SensitiveDataAuthorizationSchema } from "../generated/contracts";
import type { ConnectivityModelFitness, GridAlignmentPlan, PortfolioDecisionSummary } from "../generated/contracts";
import { evidenceFor, formatUsd, hypothesisRankRange, mapEvidenceFor, pairEvidenceFor, scenarioFor } from "../data/ridgeway";
import { chooseConnectivityAsset, chooseConnectivityBundle, createProjectWorkspace, hasNativeHost, hostErrorMessage, hostRequest, rpcData, saveExportCopy } from "../lib/host";
import { guidedDispositionLabel } from "../lib/decisionCopy";
import { AnalysisPlanReceiptSchema, AnalysisPrepareResponseSchema, AnalysisStateResponseSchema, AnalysisStatusResponseSchema, assetRequirements, assetUsesRasterGrid, buildAssetAddParams, buildAssetInspectParams, buildPlanningCreateParams, CostBasisGetResponseSchema, CostBasisSaveResponseSchema, createAssetDraft, DecisionBoardResponseSchema, DigitizationDraftSchema, DigitizationPreviewResponseSchema, DigitizationPublicationResponseSchema, ExportCreateResponseSchema, ExportEligibilityResponseSchema, GridApprovalResponseSchema, GridPreviewResponseSchema, GuidedControlResponseSchema, GuidedRerunStatusSchema, InterventionPreviewResponseSchema, InterventionSaveResponseSchema, ModelAttestResponseSchema, ModelValidationResponseSchema, planningRequirements, ProfessionalProjectStateSchema, ProviderDownloadResponseSchema, RasterInspectionSchema, RecordSaveResponseSchema, type AssetDraft } from "../lib/professional";
import type { AssetDeployment, EntryPath, EvidenceSelection, GuidedDesktopSession, GuidedEventDraft, OpaqueFileHandle, PlanningState, ProfessionalAnalysisWorkspace, ProfessionalAssetRecord, ProfessionalGridApproval, ProfessionalModelRecords, ProfessionalProjectCandidate, ProfessionalProjectSession, RasterGrid, RasterInspection, RidgewayFixture, RidgewayPortfolio, RpcPayload, WorkflowStepId } from "../types";
import { CorridorTransect } from "./CorridorTransect";
import { CurrentRasterScrubber } from "./CurrentRasterScrubber";
import { Icon } from "./Icon";
import { IntakeAssistantPanel } from "./IntakeAssistantPanel";
import { ProfessionalScenarioEvidence } from "./ProfessionalScenarioEvidence";
import { ReferenceComparisonPanel } from "./ReferenceComparisonPanel";
import { useWorkspaceDraft } from "./WorkspaceDrafts";

interface ScreensProps {
  path: EntryPath;
  step: WorkflowStepId;
  publicMode: boolean;
  localPreview: boolean;
  planning: PlanningState;
  onPlanning: (state: PlanningState) => void;
  selectedPortfolio: RidgewayPortfolio | null;
  onPortfolio: (portfolio: RidgewayPortfolio) => void;
  onEvidence: (selection: EvidenceSelection) => void;
  onNavigate: (step: WorkflowStepId) => void;
  projectSession: ProfessionalProjectSession | null;
  onProjectReady: (candidate: ProfessionalProjectCandidate) => void;
  guidedSession: GuidedDesktopSession | null;
  onGuidedEvent: (event: GuidedEventDraft) => void;
  professionalAssets: ProfessionalAssetRecord[];
  onAssetPersisted: (asset: ProfessionalAssetRecord) => void;
  professionalGridApprovals: ProfessionalGridApproval[];
  onGridApproved: (approval: ProfessionalGridApproval, asset: ProfessionalAssetRecord) => void;
  professionalRecords: ProfessionalModelRecords;
  onProfessionalRecords: (records: ProfessionalModelRecords) => void;
  professionalAnalysis: ProfessionalAnalysisWorkspace;
  onProfessionalAnalysis: (analysis: ProfessionalAnalysisWorkspace) => void;
  observabilityDeployment: "DEDICATED" | "SELF_MANAGED" | null;
  fixture: RidgewayFixture | null;
  fixtureVerified: boolean;
  guidedProjectionError: string | null;
}

interface PageIntroProps {
  kicker: string;
  title: string;
  description: string;
  badge?: ReactNode;
}

const ObservabilitySyncResponseSchema = z.object({
  binding: z.object({ state: z.string().min(1) }).passthrough(),
  receipt: z.object({
    online: z.boolean(),
    claimed: z.number().int().nonnegative(),
    synced: z.number().int().nonnegative(),
    failed: z.number().int().nonnegative(),
    blocked: z.number().int().nonnegative(),
    queued_offline: z.boolean(),
  }).strict(),
}).strict();

const CloudUploadPreviewSchema = z.object({
  destination: z.string().min(1).optional(),
  input_bytes: z.number().int().nonnegative().optional(),
  estimated_current_raster_bytes: z.number().int().nonnegative().nullable(),
  estimated_total_bytes: z.number().int().nonnegative().nullable(),
  current_raster_estimate_status: z.enum(["NOT_REQUIRED", "PRE_RUN_ESTIMATE", "ACTUAL_RECONCILED", "PARTIALLY_RECONCILED", "UNAVAILABLE"]),
  actual_retained_current_raster_bytes: z.number().int().nonnegative(),
  pending_estimated_current_raster_bytes: z.number().int().nonnegative().nullable(),
  expected_current_raster_count: z.number().int().nonnegative().nullable(),
  retained_current_raster_count: z.number().int().nonnegative(),
  current_raster_estimate_basis: z.string().min(1),
  blockers: z.array(z.string()),
  warnings: z.array(z.string()),
  allowed: z.boolean(),
}).passthrough();

function PageIntro({ kicker, title, description, badge }: PageIntroProps) {
  return (
    <header className="page-intro">
      <div><p className="section-kicker">{kicker}</p><h1 tabIndex={-1}>{title}</h1><p>{description}</p></div>
      {badge}
    </header>
  );
}

function StatusPill({ state, children }: { state: "pass" | "fail" | "review" | "info"; children: ReactNode }) {
  return <span className={`status-pill status-${state}`}>{state === "pass" && <Icon name="check" />}{state === "fail" && <Icon name="close" />}{state === "review" && <Icon name="warning" />}{state === "info" && <Icon name="info" />}{children}</span>;
}

function ProjectionNotice({ fixture, verified, error }: { fixture: RidgewayFixture | null; verified: boolean; error?: string | null }) {
  return (
    <div className={`projection-notice ${verified ? "verified" : "pending"}`} role={error ? "alert" : "note"}>
      <Icon name={verified ? "shield" : "warning"} />
      <p><strong>{verified ? "Verified synthetic fixture replay" : "Authoritative direct evidence unavailable"}</strong><span>{verified ? `Projection matched ${fixture?.manifestSha256?.slice(0, 12)}…` : error ?? fixture?.releaseBlocker ?? "The desktop is waiting for a digest-matched projection from the scientific worker."}</span></p>
    </div>
  );
}

function GuidedProjectionBoundary({ error }: { error: string | null }) {
  return (
    <section className="paper-panel blocked-workspace" role={error ? "alert" : "status"}>
      <Icon name="lock" />
      <div><span className="section-kicker">Fail-closed guided evidence</span><h2>No Ridgeway outcome is displayed from bundled UI data.</h2><p>{error ?? "Opening the authoritative worker-backed projection and verifying its evidence-manifest digest…"}</p></div>
    </section>
  );
}

function OverviewScreen({ path, publicMode, localPreview, onNavigate, projectSession, onProjectReady, fixture, fixtureVerified, guidedProjectionError }: Pick<ScreensProps, "path" | "publicMode" | "localPreview" | "onNavigate" | "projectSession" | "onProjectReady" | "fixture" | "fixtureVerified" | "guidedProjectionError">) {
  if (path === "planning") {
    return (
      <>
        <PageIntro kicker="Planning assembly" title="Start with knowns, surface the unknowns." description="Assemble a transparent planning model without allowing contextual data to quietly become ecological judgment." badge={<StatusPill state="review">Conceptual screening</StatusPill>} />
        {localPreview && <div className="preview-banner"><Icon name="info" /><p><strong>Local interface preview.</strong> Inputs stay in this browser session. No W&B upload, Circuitscape execution, attestation, or professional export is available.</p></div>}
        <div className="three-card-grid outcome-grid">
          <article className="paper-panel"><span className="card-index">01</span><Icon name="map" /><h2>Build the spatial basis</h2><p>Record the study context, classify each asset, and approve every grid transformation before it occurs.</p><button type="button" className="text-action" onClick={() => onNavigate("study-context")}>Define study context <Icon name="arrow" /></button></article>
          <article className="paper-panel"><span className="card-index">02</span><Icon name="layers" /><h2>Author ecological hypotheses</h2><p>Enter reviewed resistance meanings, focal rationales, future conditions, and action-effect bases.</p><button type="button" className="text-action" onClick={() => onNavigate("hypotheses")}>Review hypotheses <Icon name="arrow" /></button></article>
          <article className="paper-panel"><span className="card-index">03</span><Icon name="shield" /><h2>Earn comparison eligibility</h2><p>Resolve safeguards, missing effects, data authorization, numerical QA, and fitness limitations before comparing portfolios.</p><button type="button" className="text-action" onClick={() => onNavigate("model-fitness")}>Open fitness passport <Icon name="arrow" /></button></article>
        </div>
        <section className="paper-panel permanent-notice"><Icon name="warning" /><div><h2>Permanent planning-model notice</h2><p>Conceptual connectivity screening — not observed animal movement, crossing use, collision reduction, population viability, or an approved corridor.</p></div></section>
      </>
    );
  }
  if (path === "existing") {
    return (
      <>
        <PageIntro kicker="Reviewed-model path" title="Open the model with its evidence, not by itself." description="A portable bundle keeps resistance surfaces, focal areas, pairs, reviewed interventions, costs, authorizations, and references together." badge={<StatusPill state="review">Bundle required</StatusPill>} />
        {localPreview && <div className="preview-banner"><Icon name="info" /><p><strong>Local interface preview.</strong> The browser can stage a filename for layout review, but only the desktop host may inspect or import project bytes.</p></div>}
        <ImportBundlePanel projectSession={projectSession} onProjectReady={onProjectReady} />
        <div className="split-grid">
          <section className="paper-panel checklist-panel"><h2>Portable-bundle preflight</h2><ul className="check-list"><li><Icon name="check" />Circuitscape configuration and engine settings</li><li><Icon name="check" />Resistance, focal, pair, action, and review-mask assets</li><li><Icon name="check" />Ownership, license, sensitivity, retention, and cloud authorization</li><li><Icon name="check" />Optional reference matrices and diagnostic current rasters</li></ul></section>
          <section className="paper-panel caution-panel"><h2>Comparison remains blocked</h2><p>The project cannot compare a portfolio until every target and safeguard scenario has a successful direct run and accepted QA.</p><button className="text-action" type="button" onClick={() => onNavigate("model-fitness")}>Preview the fitness passport <Icon name="arrow" /></button></section>
        </div>
      </>
    );
  }
  if (!fixture) {
    return <><PageIntro kicker="Guided decision story" title="Opening Ridgeway Crossing" description="The desktop accepts teaching outcomes only from the authoritative scientific worker." badge={<StatusPill state="review">Evidence check</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  }
  return (
    <>
      <PageIntro kicker={publicMode ? "Public synthetic replay" : "Guided decision story"} title="Which Ridgeway concept deserves another look?" description={fixture.decisionBrief.question} badge={<StatusPill state="info">CC0 synthetic data</StatusPill>} />
      <ProjectionNotice fixture={fixture} verified={fixtureVerified} error={guidedProjectionError} />
      <section className="decision-brief paper-panel" aria-labelledby="brief-heading">
        <div className="brief-label"><span>Decision brief</span><strong>Ridgeway / screening record 01</strong></div>
        <div><h2 id="brief-heading">A shortlist for feasibility review—not a corridor recommendation.</h2><p>Compare {fixture.study.portfolioCount} fixed portfolios across {fixture.study.scenarioCount} ecological scenarios. Require each target and safeguard rule to hold, keep screening cost at or below {formatUsd(fixture.decisionBrief.budgetUsd)}, and inspect redistribution inside the configured road mask.</p></div>
        <button className="primary-button" type="button" onClick={() => onNavigate("model-fitness")}>Begin with model fitness <Icon name="arrow" /></button>
      </section>
      <CorridorTransect verified={fixtureVerified} />
      <div className="teaching-rules">
        <article><span>Target</span><strong>Resistance decreases within every required scenario</strong></article>
        <article><span>Safeguard</span><strong>No effective-resistance increase; road mask reviewed</strong></article>
        <article><span>Boundary</span><strong>Direct model result, never observed animal behavior</strong></article>
      </div>
    </>
  );
}

interface BundleInspection {
  project_id: string;
  title: string;
  archive_sha256: string;
  file_count: number;
  uncompressed_bytes: number;
  asset_count: number;
  scenario_count: number;
}

function ImportBundlePanel({ projectSession, onProjectReady }: Pick<ScreensProps, "projectSession" | "onProjectReady">) {
  const [selected, setSelected] = useState<OpaqueFileHandle | null>(null);
  const [inspection, setInspection] = useState<BundleInspection | null>(null);
  const [state, setState] = useState<"idle" | "choosing" | "inspecting" | "importing" | "error">("idle");
  const [message, setMessage] = useState("");
  const nativeHost = hasNativeHost();

  const choose = async () => {
    setState("choosing");
    setMessage("");
    try {
      const handle = await chooseConnectivityBundle();
      setSelected(handle);
      setInspection(null);
      setState("idle");
      if (!handle) setMessage("Bundle selection was cancelled. No file was staged.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The desktop host could not stage the selected bundle."));
    }
  };

  const inspect = async () => {
    if (!selected) return;
    setState("inspecting");
    setMessage("");
    try {
      const result = await hostRequest<RpcPayload<BundleInspection>>("connectivity.bundle.inspect", {
        bundle_handle: selected.handle,
        expected_sha256: selected.sha256,
      });
      setInspection(rpcData(result));
      setState("idle");
      setMessage("Secure archive, v1.0 contract, referenced files, byte counts, and asset hashes passed worker inspection.");
    } catch (reason) {
      setState("error");
      setInspection(null);
      setMessage(hostErrorMessage(reason, "The scientific worker rejected this bundle."));
    }
  };

  const importBundle = async () => {
    if (!selected || !inspection) return;
    setState("importing");
    setMessage("");
    try {
      const workspace = await createProjectWorkspace();
      const result = await hostRequest<RpcPayload<{ project_id: string; archive_sha256: string; state: "IMPORTED" }>>("connectivity.bundle.import", {
        bundle_handle: selected.handle,
        project_handle: workspace.handle,
        expected_sha256: inspection.archive_sha256,
      });
      const data = rpcData(result);
      onProjectReady({
        projectHandle: workspace.handle,
        projectId: data.project_id,
        title: inspection.title,
        mode: "IMPORTED",
      });
      setState("idle");
      setMessage("The verified bundle was materialized into isolated application storage. Destination binding is now being checked.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The inspected bundle could not be imported."));
    }
  };

  return (
    <section className="paper-panel import-panel">
      <Icon name="upload" />
      <div>
        <h2>{projectSession?.mode === "IMPORTED" ? "Reviewed bundle imported" : inspection ? "Worker inspection passed" : selected ? "Bundle secured for inspection" : "Choose a portable connectivity bundle"}</h2>
        <p>{projectSession?.mode === "IMPORTED"
          ? `${projectSession.title} is stored in an isolated local project. ${projectSession.bindingMessage}`
          : inspection
            ? `${inspection.title}: ${inspection.asset_count} assets, ${inspection.scenario_count} scenarios, ${inspection.file_count} archive members, ${formatBytes(inspection.uncompressed_bytes)} materialized.`
            : selected
              ? `${selected.displayName} (${formatBytes(selected.byteSize)}) was copied into protected application storage. The renderer received only an opaque handle.`
              : "ZIP the model configuration with every referenced asset, authorization record, and supporting document."}</p>
        {message && <span className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</span>}
      </div>
      <div className="import-actions">
        {!selected && <button className="primary-button" type="button" disabled={!nativeHost || state === "choosing"} onClick={() => void choose()}>{state === "choosing" ? "Opening chooser…" : "Choose bundle"}</button>}
        {selected && !inspection && <><button className="secondary-button" type="button" disabled={state === "inspecting"} onClick={() => void choose()}>Choose another</button><button className="primary-button" type="button" disabled={state === "inspecting"} onClick={() => void inspect()}>{state === "inspecting" ? "Inspecting…" : "Inspect bundle"}</button></>}
        {inspection && !projectSession && <><button className="secondary-button" type="button" disabled={state === "importing"} onClick={() => void choose()}>Choose another</button><button className="primary-button" type="button" disabled={state === "importing"} onClick={() => void importBundle()}>{state === "importing" ? "Importing…" : "Import verified bundle"}</button></>}
      </div>
      {!nativeHost && <small className="control-requirement">Requires the signed desktop host. Browser file inputs are intentionally unavailable.</small>}
    </section>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

function StudyContextScreen({ planning, onPlanning, projectSession, onProjectReady, onNavigate }: Pick<ScreensProps, "planning" | "onPlanning" | "projectSession" | "onProjectReady" | "onNavigate">) {
  const update = (field: keyof PlanningState, value: string | boolean) => onPlanning({ ...planning, [field]: value });
  const [state, setState] = useState<"idle" | "creating" | "error">("idle");
  const [message, setMessage] = useState("");
  const missing = planningRequirements(planning);
  const persisted = projectSession?.mode === "PLANNING_DRAFT";

  const createPlanningProject = async () => {
    setState("creating");
    setMessage("");
    try {
      const workspace = await createProjectWorkspace();
      const params = await buildPlanningCreateParams(planning, workspace.handle);
      const result = await hostRequest<RpcPayload<{ project_id: string; state: "PLANNING_DRAFT"; next_action: string }>>("connectivity.planning.create", params);
      const data = rpcData(result);
      onProjectReady({
        projectHandle: workspace.handle,
        projectId: data.project_id,
        title: planning.projectName.trim(),
        mode: "PLANNING_DRAFT",
      });
      setState("idle");
      setMessage(`Planning record persisted. ${data.next_action}`);
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The planning record could not be created."));
    }
  };
  return (
    <>
      <PageIntro kicker="Assembly · 01" title="Name the decision before drawing the map." description="A precise intended use keeps a planning model from being interpreted as movement, collision, population, or regulatory evidence." badge={<StatusPill state={persisted ? "pass" : "review"}>{persisted ? "Planning record persisted" : `${missing.length} required fields`}</StatusPill>} />
      <section className="paper-panel form-panel">
        <div className="form-section-heading"><span>Study record</span><h2>Purpose and responsible review</h2></div>
        <div className="authoritative-action form-command">
          <div>{persisted ? <><Icon name="check" /><span><strong>Authoritative local draft created.</strong> These creation fields stay locked so changes cannot bypass revision history. Use the reviewed Decision Boundary editor to publish a corrected question, use, owner, guild, or budget as a semantic revision.</span></> : <><Icon name="info" /><span><strong>Complete the six required fields, then create the record.</strong> This commits the decision brief and a deliberately blocking Fitness Passport to project SQLite.</span></>}</div>
          {persisted
            ? <button className="secondary-button" type="button" onClick={() => onNavigate("rules-costs")}>Revise decision boundary</button>
            : <button className="primary-button" type="button" disabled={missing.length > 0 || state === "creating" || !hasNativeHost()} onClick={() => void createPlanningProject()}>{state === "creating" ? "Creating project…" : "Create planning record"}</button>}
        </div>
        <div className="field-grid two-up">
          <label><span>Project name *</span><input disabled={persisted} value={planning.projectName} onChange={(event) => update("projectName", event.target.value)} placeholder="e.g. Route 14 planning screen" /></label>
          <label><span>Generalized study area *</span><input disabled={persisted} value={planning.studyArea} onChange={(event) => update("studyArea", event.target.value)} placeholder="County, route limits, or approved generalized area" /></label>
          <label className="field-span"><span>Decision question *</span><textarea disabled={persisted} rows={3} value={planning.decisionQuestion} onChange={(event) => update("decisionQuestion", event.target.value)} placeholder="Which reviewed intervention portfolios, if any, should advance to field and engineering feasibility review?" /></label>
          <label className="field-span"><span>Intended use and scope boundary *</span><textarea disabled={persisted} rows={4} value={planning.intendedUse} onChange={(event) => update("intendedUse", event.target.value)} placeholder="Describe the screening decision and explicitly state what this model cannot establish." /></label>
          <label><span>Target guild or life stage *</span><input disabled={persisted} value={planning.targetGuild} onChange={(event) => update("targetGuild", event.target.value)} placeholder="Reviewer-authored descriptive guild" /></label>
          <label><span>Responsible ecological reviewer *</span><input disabled={persisted} value={planning.reviewer} onChange={(event) => update("reviewer", event.target.value)} placeholder="Name or accountable role; this is not an attestation" /></label>
        </div>
        {missing.length > 0 && !persisted && <p className="control-requirement">Complete: {missing.join(", ")}.</p>}
        {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
      </section>
      <section className="paper-panel scope-boundary"><h2>Required scope boundary</h2><div className="boundary-grid"><p><strong>This model may compare</strong><span>Baseline-relative effective resistance across explicitly reviewed scenarios and portfolios.</span></p><p><strong>This model may not predict</strong><span>Animal locations, crossing use, collisions, demographic benefit, biodiversity, or project approval.</span></p></div></section>
    </>
  );
}

const ASSET_KIND_OPTIONS = [
  ["RESISTANCE_RASTER", "Resistance raster"],
  ["FOCAL_RASTER", "Focal-area identifier raster"],
  ["ACTION_EFFECT", "Reviewed action-effect asset"],
  ["REVIEW_MASK", "Risk review mask"],
  ["VECTOR_CONTEXT", "Context vector layer"],
  ["DOCUMENT", "Supporting document"],
  ["REFERENCE_MATRIX", "Reference resistance matrix"],
  ["REFERENCE_CURRENT", "Reference current raster"],
] as const;

const DEPLOYMENT_OPTIONS: Array<[AssetDeployment, string]> = [
  ["LOCAL_ONLY", "Local project only"],
  ["DEDICATED", "W&B Dedicated Cloud"],
  ["SELF_MANAGED", "W&B Self-Managed"],
  ["WANDB_CLOUD", "Multi-tenant W&B Cloud"],
];

const PROVIDER_SPECS = {
  usgs_3dep: { provider: "U.S. Geological Survey", dataset: "3D Elevation Program (3DEP)", vintage: "provider item metadata", resolution: "Varies by product and geography; confirm before download", license: "U.S. federal public-domain data; preserve source metadata", attribution: "U.S. Geological Survey 3D Elevation Program", intendedUse: "Terrain and boundary context only", hosts: "prd-tnm S3 · StagedProducts/Elevation only", urlPatterns: [/^https:\/\/prd-tnm\.s3(?:\.us-west-2)?\.amazonaws\.com\/StagedProducts\/Elevation\/(?:1m|13|19|30m|OPR|LPC|Source)\/.+\.(?:tif|tiff|zip|xml|json|txt|jpg|jpeg)$/i] },
  annual_nlcd_1_2: { provider: "U.S. Geological Survey EROS", dataset: "Annual NLCD Collection 1.2", vintage: "selected annual product", resolution: "30 m", license: "U.S. federal public-domain data; preserve source metadata", attribution: "U.S. Geological Survey Annual NLCD", intendedUse: "Land-cover context only", hosts: "usgs-landcover S3 · annual-nlcd/c1/v0 only", urlPatterns: [/^https:\/\/usgs-landcover\.s3(?:\.us-west-2)?\.amazonaws\.com\/annual-nlcd\/c1\/v0\/(?:cu|ak|hi)\/(?:tile\/h\d{2}v\d{2}|mosaic)\/.+\.(?:tif|tiff|zip|xml|json)$/i] },
  pad_us_4_1: { provider: "U.S. Geological Survey Gap Analysis Project", dataset: "Protected Areas Database of the United States 4.1", vintage: "4.1", resolution: "Vector inventory; source precision varies", license: "U.S. federal public-domain data; respect source restrictions", attribution: "U.S. Geological Survey Gap Analysis Project, PAD-US 4.1", intendedUse: "Ownership and protected-area context only", hosts: "ScienceBase file route · four pinned PAD-US 4.1 item IDs", urlPatterns: [/^https:\/\/www\.sciencebase\.gov\/catalog\/file\/get\/(?:652d4fc5d34e44db0e2ee45e|6759abcfd34edfeb8710a004|6759b69fd34edfeb8710a3ea|6759b67ed34edfeb8710a3db)(?:\?name=[^&]+)?$/i] },
  tiger_line_2025: { provider: "U.S. Census Bureau", dataset: "2025 TIGER/Line roads", vintage: "2025", resolution: "Vector cartographic geometry; not survey-grade", license: "U.S. federal data; preserve Census source and vintage", attribution: "U.S. Census Bureau 2025 TIGER/Line", intendedUse: "Road and study-context review only", hosts: "www2.census.gov · TIGER2025/ROADS county file", urlPatterns: [/^https:\/\/www2\.census\.gov\/geo\/tiger\/TIGER2025\/ROADS\/tl_2025_\d{5}_roads\.zip$/i] },
} as const;

function ProviderDownloadPanel({ projectSession, reviewer, onAssetPersisted }: { projectSession: ProfessionalProjectSession | null; reviewer: string; onAssetPersisted: (asset: ProfessionalAssetRecord) => void }) {
  type ProviderId = keyof typeof PROVIDER_SPECS;
  const [open, setOpen] = useState(false);
  const [providerId, setProviderId] = useState<ProviderId>("usgs_3dep");
  const initial = PROVIDER_SPECS.usgs_3dep;
  const [draft, setDraft] = useState<AssetDraft>(() => ({ ...createAssetDraft(reviewer), kind: "VECTOR_CONTEXT", mediaType: "application/octet-stream", source: initial.attribution, vintage: initial.vintage, license: initial.license, intendedUse: initial.intendedUse }));
  const [sourceUrl, setSourceUrl] = useState("");
  const [extent, setExtent] = useState("");
  const [resolution, setResolution] = useState<string>(initial.resolution);
  const [expectedBytes, setExpectedBytes] = useState("");
  const [maximumBytes, setMaximumBytes] = useState("");
  const [consent, setConsent] = useState(false);
  const [state, setState] = useState<"idle" | "downloading" | "complete" | "error">("idle");
  const [message, setMessage] = useState("");
  const spec = PROVIDER_SPECS[providerId];
  const update = <K extends keyof AssetDraft>(field: K, value: AssetDraft[K]) => setDraft((current) => ({ ...current, [field]: value }));
  const toggleDeployment = (deployment: AssetDeployment, checked: boolean) => update("approvedDeployments", checked ? [...new Set([...draft.approvedDeployments, deployment])] : draft.approvedDeployments.filter((item) => item !== deployment));
  const expected = Number(expectedBytes);
  const maximum = Number(maximumBytes);
  const fakeSource: OpaqueFileHandle = { handle: "provider-reviewed", displayName: "provider-context", byteSize: Number.isFinite(expected) ? expected : 0, sha256: "0".repeat(64) };
  const missing = assetRequirements(draft, fakeSource).filter((item) => item !== "Selected source bytes");
  let urlAllowed = false;
  try { const url = new URL(sourceUrl); urlAllowed = url.protocol === "https:" && !url.username && !url.password && !url.hash && (!url.port || url.port === "443") && spec.urlPatterns.some((pattern) => pattern.test(url.href)); } catch { urlAllowed = false; }
  if (!urlAllowed) missing.push("Allowlisted HTTPS provider URL");
  if (!extent.trim()) missing.push("Download extent");
  if (!resolution.trim()) missing.push("Selected resolution");
  if (!Number.isInteger(expected) || expected <= 0) missing.push("Positive expected byte count");
  if (!Number.isInteger(maximum) || maximum < expected) missing.push("Maximum bytes at least expected bytes");
  if (!consent) missing.push("Explicit contextual-download consent");
  const changeProvider = (value: ProviderId) => {
    const next = PROVIDER_SPECS[value];
    setProviderId(value);
    setDraft((current) => ({ ...current, title: "", source: next.attribution, vintage: next.vintage, license: next.license, intendedUse: next.intendedUse }));
    setResolution(next.resolution); setSourceUrl(""); setExtent(""); setExpectedBytes(""); setMaximumBytes(""); setConsent(false); setState("idle"); setMessage("");
  };
  const download = async () => {
    if (!projectSession || missing.length) return;
    setState("downloading"); setMessage("");
    try {
      const built = buildAssetAddParams(draft, fakeSource, projectSession.projectHandle) as { asset: { authorization: Record<string, unknown> } };
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.asset.download", { project_handle: projectSession.projectHandle, provider_id: providerId, source_url: sourceUrl.trim(), extent_description: extent.trim(), expected_bytes: expected, selected_vintage: draft.vintage.trim(), selected_resolution: resolution.trim(), asset_id: draft.assetId, title: draft.title.trim(), kind: "VECTOR_CONTEXT", media_type: draft.mediaType, authorization: built.asset.authorization, explicit_consent: true, maximum_bytes: maximum });
      const parsed = ProviderDownloadResponseSchema.parse(rpcData(response));
      const asset = parsed.asset;
      if (asset.asset_id !== draft.assetId || asset.source !== sourceUrl.trim() || asset.license !== spec.license) throw new Error("The downloaded asset response differs from the reviewed provider request.");
      onAssetPersisted({ assetId: asset.asset_id, title: asset.title, kind: asset.kind, displayName: asset.relative_path.split("/").at(-1) ?? asset.relative_path, relativePath: asset.relative_path, sha256: asset.sha256, byteSize: asset.byte_size, mediaType: asset.media_type, source: asset.source, vintage: asset.vintage, license: asset.license, sensitivity: asset.authorization.sensitivity, cloudDisposition: asset.authorization.cloud_disposition, exportAllowed: asset.authorization.export_allowed, approvedDeployments: asset.authorization.approved_deployments, reviewer: asset.authorization.reviewer, reviewedAt: asset.authorization.reviewed_at, intendedUse: draft.intendedUse.trim(), revision: "provider-download", grid: null, authorization: asset.authorization });
      setState("complete"); setMessage(`Downloaded and checksum-retained ${asset.byte_size.toLocaleString()} bytes as context only.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The approved provider asset could not be downloaded.")); }
  };
  return <section className={`paper-panel provider-panel${open ? " is-open" : ""}`}><div><Icon name="cloud" /><h2>Approved context providers</h2><p>USGS 3DEP, Annual NLCD, PAD-US, and TIGER/Line roads remain contextual inputs. They never become resistance, focal areas, or action effects automatically.</p></div><button type="button" className="secondary-button" disabled={!projectSession} onClick={() => setOpen((current) => !current)}>{open ? "Close provider request" : "Review provider request"}</button><small>{projectSession ? "Exact URL, extent, expected bytes, license, authorization, and consent are required." : "Create the authoritative planning record first."}</small>{open && <div className="provider-editor"><div className="provider-preview"><label><span>Approved provider *</span><select value={providerId} onChange={(event) => changeProvider(event.target.value as ProviderId)}>{Object.entries(PROVIDER_SPECS).map(([id, item]) => <option key={id} value={id}>{item.dataset}</option>)}</select></label><dl><div><dt>Provider</dt><dd>{spec.provider}</dd></div><div><dt>Vintage</dt><dd>{draft.vintage}</dd></div><div><dt>Resolution</dt><dd>{resolution}</dd></div><div><dt>License</dt><dd>{spec.license}</dd></div><div><dt>Attribution</dt><dd>{spec.attribution}</dd></div><div><dt>Permitted role</dt><dd>{spec.intendedUse}</dd></div></dl></div><div className="field-grid two-up"><label className="field-span"><span>Exact HTTPS download URL * · allowed host: {spec.hosts}</span><input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} /></label><label className="field-span"><span>Download extent *</span><textarea rows={2} value={extent} onChange={(event) => setExtent(event.target.value)} placeholder="Generalized bounding area or provider item coverage" /></label><label><span>Selected vintage *</span><input value={draft.vintage} onChange={(event) => update("vintage", event.target.value)} /></label><label><span>Selected resolution *</span><input value={resolution} onChange={(event) => setResolution(event.target.value)} /></label><label><span>Expected bytes *</span><input inputMode="numeric" value={expectedBytes} onChange={(event) => setExpectedBytes(event.target.value.replace(/[^0-9]/g, ""))} /></label><label><span>Hard download maximum bytes *</span><input inputMode="numeric" value={maximumBytes} onChange={(event) => setMaximumBytes(event.target.value.replace(/[^0-9]/g, ""))} /></label><label><span>Asset title *</span><input value={draft.title} onChange={(event) => update("title", event.target.value)} /></label><label><span>Media type *</span><select value={draft.mediaType} onChange={(event) => update("mediaType", event.target.value)}><option value="application/octet-stream">Provider binary/archive</option><option value="application/zip">ZIP archive</option><option value="application/geopackage+sqlite3">GeoPackage</option><option value="application/geo+json">GeoJSON</option><option value="image/tiff">GeoTIFF context</option></select></label><label><span>Owner or custodian *</span><input value={draft.ownership} onChange={(event) => update("ownership", event.target.value)} /></label><label><span>Sensitivity *</span><select value={draft.sensitivity} onChange={(event) => update("sensitivity", event.target.value as AssetDraft["sensitivity"])}><option value="">Classify…</option><option value="PUBLIC">Public</option><option value="INTERNAL">Internal</option><option value="SENSITIVE_LOCATION">Sensitive location</option><option value="CRITICAL_INFRASTRUCTURE">Critical infrastructure</option><option value="TRIBAL_OR_CULTURAL">Tribal or cultural</option><option value="LICENSE_RESTRICTED">License restricted</option></select></label><label><span>Cloud permission *</span><select value={draft.cloudDisposition} onChange={(event) => update("cloudDisposition", event.target.value as AssetDraft["cloudDisposition"])}><option value="">Choose…</option><option value="RAW_ALLOWED">Raw allowed for approved destinations</option><option value="GENERALIZED_DERIVATIVE_ONLY">Generalized derivative only</option><option value="DEDICATED_OR_SELF_MANAGED_ONLY">Dedicated/Self-Managed only</option><option value="PROHIBITED">No cloud replication</option></select></label>{draft.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" && <label><span>Generalized derivative asset ID *</span><input value={draft.generalizedAssetId} onChange={(event) => update("generalizedAssetId", event.target.value)} /></label>}<label><span>Export permission *</span><select value={draft.exportPermission} onChange={(event) => update("exportPermission", event.target.value as AssetDraft["exportPermission"])}><option value="">Choose…</option><option value="ALLOW">Allowed</option><option value="BLOCK">Blocked</option></select></label><label><span>Retention policy *</span><input value={draft.retentionPolicy} onChange={(event) => update("retentionPolicy", event.target.value)} /></label><label className="field-span"><span>Spatial redaction or explicit none *</span><textarea rows={2} value={draft.spatialRedaction} onChange={(event) => update("spatialRedaction", event.target.value)} /></label><label><span>Authorization reviewer *</span><input value={draft.reviewer} onChange={(event) => update("reviewer", event.target.value)} /></label><label><span>Reviewed at *</span><input type="datetime-local" value={draft.reviewedAt} onChange={(event) => update("reviewedAt", event.target.value)} /></label></div><fieldset className="asset-deployments"><legend>Approved destinations *</legend>{DEPLOYMENT_OPTIONS.map(([value, label]) => <label className="checkbox-row" key={value}><input type="checkbox" checked={draft.approvedDeployments.includes(value)} onChange={(event) => toggleDeployment(value, event.target.checked)} />{label}</label>)}</fieldset><label className="confirmation-row"><input type="checkbox" checked={draft.roleConfirmed} onChange={(event) => update("roleConfirmed", event.target.checked)} /><span><strong>I reviewed the source, license, authorization, extent, and contextual-only purpose.</strong> Downloaded bytes will not acquire ecological meaning automatically.</span></label><label className="confirmation-row"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span><strong>Download this contextual source and retain its exact bytes and checksum.</strong> I understand it remains context until separate human-reviewed contracts exist.</span></label><div className="provider-download-action"><div><strong>Storage estimate</strong><span>{Number.isFinite(expected) && expected > 0 ? `${formatBytes(expected)} expected · ${formatBytes(maximum)} hard maximum` : "Enter exact estimates before network access."}</span>{missing.length > 0 && <small>{missing.length} required or inconsistent item{missing.length === 1 ? "" : "s"}: {missing.join(", ")}</small>}</div><button className="primary-button" type="button" disabled={missing.length > 0 || state === "downloading" || state === "complete"} onClick={() => void download()}>{state === "downloading" ? "Downloading and hashing…" : state === "complete" ? "Context asset retained" : "Consent and download"}</button></div>{message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}</div>}</section>;
}

function SourceAssetsScreen({ projectSession, planning, professionalAssets, onAssetPersisted }: Pick<ScreensProps, "projectSession" | "planning" | "professionalAssets" | "onAssetPersisted">) {
  const [selected, setSelected] = useState<OpaqueFileHandle | null>(null);
  const [draft, setDraft] = useState<AssetDraft>(() => createAssetDraft(planning.reviewer));
  const [inspection, setInspection] = useState<RasterInspection | null>(null);
  const [state, setState] = useState<"idle" | "choosing" | "inspecting" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const missing = assetRequirements(draft, selected, inspection);

  const update = <K extends keyof AssetDraft>(field: K, value: AssetDraft[K]) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  const choose = async () => {
    if (!projectSession) return;
    setState("choosing");
    setMessage("");
    try {
      const handle = await chooseConnectivityAsset();
      if (!handle) {
        setState("idle");
        setMessage("Asset selection was cancelled. No project record was changed.");
        return;
      }
      setSelected(handle);
      setDraft(createAssetDraft(planning.reviewer));
      setInspection(null);
      setState("idle");
      setMessage("The source bytes were staged in protected application storage. No content or ecological meaning was inspected in the interface.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The desktop host could not stage the selected asset."));
    }
  };

  const inspectRaster = async () => {
    if (!projectSession || !selected) return;
    setState("inspecting");
    setMessage("");
    try {
      const params = buildAssetInspectParams(draft, selected, projectSession.projectHandle);
      const result = await hostRequest<RpcPayload<{ inspection: unknown; transformed: false; next_action: string }>>("connectivity.asset.inspect", params);
      const data = rpcData(result);
      if (data.transformed !== false) throw new Error("Raster inspection unexpectedly reported a transformation.");
      const parsed = RasterInspectionSchema.parse(data.inspection);
      if (parsed.sha256 !== selected.sha256 || parsed.byte_size !== selected.byteSize) {
        throw new Error("The raster inspection does not match the staged source checksum and byte count.");
      }
      if (parsed.grid.value_kind !== draft.rasterValueKind || parsed.grid.grid_id !== `grid_${draft.assetId}`) {
        throw new Error("The returned grid identity or value semantics differ from the reviewed inspection request.");
      }
      setInspection(parsed);
      setState("idle");
      setMessage("The scientific worker read the retained source bytes without transformation. Review the exact grid before persisting the asset.");
    } catch (reason) {
      setInspection(null);
      setState("error");
      setMessage(hostErrorMessage(reason, "The scientific worker could not inspect this raster without transformation."));
    }
  };

  const toggleDeployment = (deployment: AssetDeployment, checked: boolean) => {
    const next = checked
      ? [...new Set([...draft.approvedDeployments, deployment])]
      : draft.approvedDeployments.filter((item) => item !== deployment);
    update("approvedDeployments", next);
  };

  const persist = async () => {
    if (!projectSession || !selected || missing.length) return;
    setState("saving");
    setMessage("");
    try {
      const params = buildAssetAddParams(draft, selected, projectSession.projectHandle, inspection);
      const result = await hostRequest<RpcPayload<{ asset_id: string; revision: number; sha256: string }>>("connectivity.asset.add", params);
      const persisted = rpcData(result);
      if (persisted.asset_id !== draft.assetId || persisted.sha256 !== selected.sha256) {
        throw new Error("The worker response did not match the reviewed asset identity and checksum.");
      }
      onAssetPersisted({
        assetId: persisted.asset_id,
        title: draft.title.trim(),
        kind: draft.kind as ProfessionalAssetRecord["kind"],
        displayName: selected.displayName,
        relativePath: `assets/${draft.assetId}/source${selected.displayName.match(/\.([A-Za-z0-9]{1,10})$/)?.[0]?.toLowerCase() ?? ""}`,
        sha256: persisted.sha256,
        byteSize: selected.byteSize,
        mediaType: draft.mediaType,
        source: draft.source.trim(),
        vintage: draft.vintage.trim(),
        license: draft.license.trim(),
        sensitivity: draft.sensitivity as ProfessionalAssetRecord["sensitivity"],
        cloudDisposition: draft.cloudDisposition as ProfessionalAssetRecord["cloudDisposition"],
        exportAllowed: draft.exportPermission === "ALLOW",
        approvedDeployments: draft.approvedDeployments,
        reviewer: draft.reviewer.trim(),
        reviewedAt: new Date(draft.reviewedAt).toISOString(),
        intendedUse: draft.intendedUse.trim(),
        revision: persisted.revision,
        grid: inspection?.grid ?? null,
        authorization: {
          asset_id: persisted.asset_id,
          ownership: draft.ownership.trim(),
          license_spdx_or_name: draft.license.trim(),
          sensitivity: draft.sensitivity as ProfessionalAssetRecord["sensitivity"],
          cloud_disposition: draft.cloudDisposition as ProfessionalAssetRecord["cloudDisposition"],
          export_allowed: draft.exportPermission === "ALLOW",
          retention_policy: draft.retentionPolicy.trim(),
          spatial_redaction: draft.spatialRedaction.trim(),
          approved_deployments: draft.approvedDeployments,
          reviewer: draft.reviewer.trim(),
          reviewed_at: new Date(draft.reviewedAt).toISOString(),
          generalized_asset_id: draft.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" ? draft.generalizedAssetId.trim() : null,
          notes: `Intended use: ${draft.intendedUse.trim()}${draft.authorizationNotes.trim() ? `\nAuthorization notes: ${draft.authorizationNotes.trim()}` : ""}`,
        },
      });
      setSelected(null);
      setDraft(createAssetDraft(planning.reviewer));
      setInspection(null);
      setState("idle");
      setMessage(`Asset ${persisted.asset_id} was persisted by the scientific worker at revision ${persisted.revision}.`);
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The reviewed asset could not be persisted."));
    }
  };

  return (
    <>
      <PageIntro kicker="Assembly · 02" title="Classify every layer before it moves." description="Context layers can support ecological review, but they never automatically become resistance, focal areas, or intervention effects." badge={<StatusPill state={professionalAssets.length ? "review" : "fail"}>{professionalAssets.length ? `${professionalAssets.length} persisted asset${professionalAssets.length === 1 ? "" : "s"}` : "No authorized assets"}</StatusPill>} />
      <section className="paper-panel table-panel">
        <div className="panel-heading"><div><span className="section-kicker">Authoritative local assets</span><h2>Ownership and replication ledger</h2></div><button className="secondary-button" type="button" disabled={!projectSession || state === "choosing" || !hasNativeHost()} title={!projectSession ? "Create the authoritative planning project first." : !hasNativeHost() ? "Requires the signed desktop host." : undefined} onClick={() => void choose()}><Icon name="upload" /> {state === "choosing" ? "Opening chooser…" : professionalAssets.length ? "Add another asset" : "Choose reviewed asset"}</button></div>
        <div className="table-scroll"><table><thead><tr><th>Asset</th><th>Reviewer-assigned role</th><th>Sensitivity</th><th>Cloud disposition</th><th>Persisted evidence</th></tr></thead><tbody>{professionalAssets.length ? professionalAssets.map((asset) => <tr key={asset.assetId}><td><strong>{asset.title}</strong><span className="table-subline mono">{asset.assetId}</span></td><td>{ASSET_KIND_OPTIONS.find(([value]) => value === asset.kind)?.[1] ?? asset.kind}{asset.grid && <span className="table-subline mono">{asset.grid.width}×{asset.grid.height} · {asset.grid.value_kind}</span>}</td><td>{asset.sensitivity.replaceAll("_", " ")}</td><td>{asset.cloudDisposition.replaceAll("_", " ")}<span className="table-subline">{asset.approvedDeployments.join(", ")}</span></td><td><StatusPill state="pass">Worker revision {asset.revision}</StatusPill><span className="table-subline mono">sha256:{asset.sha256.slice(0, 12)}…</span></td></tr>) : <tr><td><strong>No source assets added</strong></td><td>—</td><td>—</td><td>—</td><td>Create the planning record, then choose local bytes</td></tr>}</tbody></table></div>
        {!projectSession && <div className="blocked-control-note"><Icon name="lock" /><p><strong>Create the planning record first.</strong><span>Source bytes cannot enter an unnamed project or bypass its fail-closed Model Fitness Passport.</span></p></div>}
        {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
      </section>
      {selected && <section className="paper-panel asset-editor" aria-labelledby="asset-editor-heading">
        <div className="panel-heading"><div><span className="section-kicker">Reviewed asset contract</span><h2 id="asset-editor-heading">Authorize {selected.displayName}</h2></div><span className="mono">{formatBytes(selected.byteSize)} · sha256:{selected.sha256.slice(0, 12)}…</span></div>
        <div className="asset-boundary"><Icon name="shield" /><p><strong>Bytes are staged locally; meaning is not inferred.</strong><span>Selecting a role records the reviewer’s classification. For modeled rasters, the scientific worker reads grid metadata and cell statistics without transforming the file; React never parses raster bytes.</span></p></div>

        <fieldset className="asset-fieldset"><legend>Identity, provenance, and intended use</legend><div className="field-grid two-up">
          <label><span>Stable asset ID</span><input value={draft.assetId} readOnly className="mono" /></label>
          <label><span>Asset title *</span><input value={draft.title} onChange={(event) => update("title", event.target.value)} /></label>
          <label><span>Reviewer-assigned role *</span><select value={draft.kind} onChange={(event) => { const kind = event.target.value as AssetDraft["kind"]; const semantics = kind === "FOCAL_RASTER" ? "IDENTIFIER" : kind === "REVIEW_MASK" ? "MASK" : kind === "RESISTANCE_RASTER" || kind === "ACTION_EFFECT" ? "CONTINUOUS" : ""; setDraft((current) => ({ ...current, kind, rasterValueKind: semantics })); setInspection(null); }}><option value="">Choose a reviewed role…</option>{ASSET_KIND_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label><span>Media type *</span><select value={draft.mediaType} onChange={(event) => update("mediaType", event.target.value)}><option value="">Choose the documented format…</option><option value="image/tiff">GeoTIFF / TIFF</option><option value="text/plain">ESRI ASCII grid</option><option value="application/geopackage+sqlite3">GeoPackage</option><option value="application/geo+json">GeoJSON</option><option value="text/csv">CSV</option><option value="application/json">JSON</option><option value="application/pdf">PDF</option><option value="application/octet-stream">Other documented binary</option></select></label>
          <label><span>Source and attribution *</span><input value={draft.source} onChange={(event) => update("source", event.target.value)} placeholder="Agency, consultant, survey, or provider record" /></label>
          <label><span>Vintage or observation date *</span><input value={draft.vintage} onChange={(event) => update("vintage", event.target.value)} placeholder="e.g. 2025 publication; surveyed 2026-04" /></label>
          <label><span>License or use terms *</span><input value={draft.license} onChange={(event) => update("license", event.target.value)} placeholder="SPDX identifier or exact license name" /></label>
          <label><span>Owner or custodian *</span><input value={draft.ownership} onChange={(event) => update("ownership", event.target.value)} /></label>
          <label className="field-span"><span>Intended use in this project *</span><textarea rows={3} value={draft.intendedUse} onChange={(event) => update("intendedUse", event.target.value)} placeholder="State how this asset may inform review; do not claim it proves movement, collision, population, or regulatory outcomes." /></label>
        </div></fieldset>

        {assetUsesRasterGrid(draft.kind) && <fieldset className="asset-fieldset"><legend>Exact source-grid inspection</legend><div className="field-grid two-up">
          <label><span>Raster format *</span><select value={draft.rasterFormat} onChange={(event) => { const rasterFormat = event.target.value as AssetDraft["rasterFormat"]; setDraft((current) => ({ ...current, rasterFormat, mediaType: rasterFormat === "GEOTIFF" ? "image/tiff" : rasterFormat === "ESRI_ASCII" ? "text/plain" : current.mediaType })); setInspection(null); }}><option value="">Choose exact format…</option><option value="GEOTIFF">GeoTIFF</option><option value="ESRI_ASCII">ESRI ASCII</option></select></label>
          <label><span>Value semantics *</span><select value={draft.rasterValueKind} onChange={(event) => { update("rasterValueKind", event.target.value as AssetDraft["rasterValueKind"]); setInspection(null); }}><option value="">Choose semantics…</option><option value="CONTINUOUS">Continuous resistance / replacement values</option><option value="CATEGORICAL">Categorical context values</option><option value="IDENTIFIER">Positive focal identifiers</option><option value="MASK">Review-mask values</option></select></label>
          {draft.rasterFormat === "ESRI_ASCII" && <><label><span>Declared CRS *</span><input value={draft.esriAsciiCrs} onChange={(event) => { update("esriAsciiCrs", event.target.value); setInspection(null); }} placeholder="e.g. EPSG:5070" /></label><label><span>Declared cell area (m²) *</span><input inputMode="decimal" value={draft.esriAsciiCellAreaM2} onChange={(event) => { update("esriAsciiCellAreaM2", event.target.value); setInspection(null); }} /></label></>}
        </div><div className="asset-inspection-action"><button className="secondary-button" type="button" disabled={!draft.rasterFormat || !draft.rasterValueKind || state === "inspecting"} onClick={() => void inspectRaster()}><Icon name="grid" /> {state === "inspecting" ? "Inspecting retained bytes…" : inspection ? "Inspect again" : "Inspect raster grid"}</button>{inspection ? <dl><div><dt>Grid</dt><dd className="mono">{inspection.grid.width} × {inspection.grid.height} · {inspection.grid.crs}</dd></div><div><dt>Cell area</dt><dd>{inspection.grid.cell_area_m2.toLocaleString()} m²</dd></div><div><dt>Data / NODATA</dt><dd>{inspection.data_cell_count.toLocaleString()} / {inspection.nodata_cell_count.toLocaleString()}</dd></div><div><dt>Range</dt><dd>{inspection.minimum} to {inspection.maximum} · {inspection.unique_value_count.toLocaleString()} unique</dd></div></dl> : <p>No raster was opened or transformed by React. Persistence remains blocked until the worker returns matching grid evidence.</p>}</div></fieldset>}

        <fieldset className="asset-fieldset"><legend>Sensitivity and raw/generalized permission</legend><div className="field-grid two-up">
          <label><span>Sensitivity *</span><select value={draft.sensitivity} onChange={(event) => update("sensitivity", event.target.value as AssetDraft["sensitivity"])}><option value="">Classify sensitivity…</option><option value="PUBLIC">Public</option><option value="INTERNAL">Internal</option><option value="SENSITIVE_LOCATION">Sensitive location</option><option value="CRITICAL_INFRASTRUCTURE">Critical infrastructure</option><option value="TRIBAL_OR_CULTURAL">Tribal or cultural</option><option value="LICENSE_RESTRICTED">License restricted</option></select></label>
          <label><span>Raw/generalized cloud permission *</span><select value={draft.cloudDisposition} onChange={(event) => update("cloudDisposition", event.target.value as AssetDraft["cloudDisposition"])}><option value="">Choose an explicit disposition…</option><option value="RAW_ALLOWED">Raw bytes allowed for approved deployments</option><option value="GENERALIZED_DERIVATIVE_ONLY">Generalized derivative only; raw bytes local</option><option value="DEDICATED_OR_SELF_MANAGED_ONLY">Raw bytes only on approved Dedicated/Self-Managed deployment</option><option value="PROHIBITED">No cloud replication</option></select></label>
          {draft.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" && <label className="field-span"><span>Approved generalized derivative asset ID *</span><input value={draft.generalizedAssetId} onChange={(event) => update("generalizedAssetId", event.target.value)} placeholder="Stable ID of the separately reviewed derivative" /><small>This selected raw asset remains local. The referenced derivative must be added and authorized separately before sync.</small></label>}
          <label><span>Export permission *</span><select value={draft.exportPermission} onChange={(event) => update("exportPermission", event.target.value as AssetDraft["exportPermission"])}><option value="">Choose explicitly…</option><option value="ALLOW">Allowed under stated terms</option><option value="BLOCK">Not allowed</option></select></label>
          <label><span>Retention policy *</span><input value={draft.retentionPolicy} onChange={(event) => update("retentionPolicy", event.target.value)} placeholder="Duration, disposal trigger, and responsible party" /></label>
          <label className="field-span"><span>Spatial redaction or explicit none *</span><textarea rows={2} value={draft.spatialRedaction} onChange={(event) => update("spatialRedaction", event.target.value)} placeholder="Generalization method and resolution, or why none is required" /></label>
        </div>
        <fieldset className="asset-deployments"><legend>Approved deployment destinations *</legend>{DEPLOYMENT_OPTIONS.map(([value, label]) => <label className="checkbox-row" key={value}><input type="checkbox" checked={draft.approvedDeployments.includes(value)} onChange={(event) => toggleDeployment(value, event.target.checked)} />{label}</label>)}</fieldset>
        </fieldset>

        <fieldset className="asset-fieldset"><legend>Authorization review</legend><div className="field-grid two-up">
          <label><span>Reviewer or accountable role *</span><input value={draft.reviewer} onChange={(event) => update("reviewer", event.target.value)} /></label>
          <label><span>Reviewed at *</span><input type="datetime-local" value={draft.reviewedAt} onChange={(event) => update("reviewedAt", event.target.value)} /></label>
          <label className="field-span"><span>Authorization notes</span><textarea rows={2} value={draft.authorizationNotes} onChange={(event) => update("authorizationNotes", event.target.value)} /></label>
        </div><label className="confirmation-row"><input type="checkbox" checked={draft.roleConfirmed} onChange={(event) => update("roleConfirmed", event.target.checked)} /><span><strong>I confirm that a responsible reviewer assigned this asset role and authorization.</strong> EcoCorridor did not infer ecological meaning or cloud permission from the file.</span></label></fieldset>

        <div className="asset-save-row"><div>{missing.length ? <details><summary>{missing.length} required or inconsistent item{missing.length === 1 ? "" : "s"}</summary><ul>{missing.map((item) => <li key={item}>{item}</li>)}</ul></details> : <p><Icon name="check" /><strong>Contract complete.</strong> The worker will verify source bytes against this checksum before persisting.</p>}</div><button className="primary-button" type="button" disabled={missing.length > 0 || state === "saving"} onClick={() => void persist()}>{state === "saving" ? "Verifying and persisting…" : "Persist reviewed asset"}</button></div>
        <p className="control-requirement">Raster grid identity is not inferred here. Raster use remains blocked until the scientific workflow records and validates CRS, alignment, dimensions, transform, NODATA, value kind, and cell area.</p>
      </section>}
      <ProviderDownloadPanel projectSession={projectSession} reviewer={planning.reviewer} onAssetPersisted={onAssetPersisted} />
      <DigitizationPanel projectSession={projectSession} reviewer={planning.reviewer} professionalAssets={professionalAssets} onAssetPersisted={onAssetPersisted} />
    </>
  );
}

function stableUiId(prefix: string): string {
  const value = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${prefix}_${value.replaceAll("-", "")}`;
}

const COST_ASSUMPTION_PREFIXES = {
  analysisHorizonYears: "Analysis horizon years:",
  realDiscountRatePercent: "Real discount rate percent:",
  capexBasis: "CAPEX basis:",
  omAssumptions: "O&M assumptions:",
  replacementAssumptions: "Replacement assumptions:",
} as const;

function costAssumptionValue(assumptions: string[] | undefined, prefix: string): string {
  const line = assumptions?.find((item) => item.startsWith(prefix));
  return line?.slice(prefix.length).trim() ?? "";
}

function dateTimeLocalValue(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Date(date.valueOf() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

type DigitizationPreview = ReturnType<typeof DigitizationPreviewResponseSchema.parse>["preview"];

function parseDigitizedCells(value: string) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean).map((line, index) => {
    const fields = line.split(",").map((item) => item.trim());
    if (fields.length !== 4) throw new Error(`Cell line ${index + 1}: use feature-id,row,column,value.`);
    const row = Number(fields[1]); const column = Number(fields[2]); const cellValue = Number(fields[3]);
    if (!fields[0] || !Number.isInteger(row) || row < 0 || !Number.isInteger(column) || column < 0 || !Number.isInteger(cellValue)) throw new Error(`Cell line ${index + 1} has an invalid feature ID, row, column, or integer value.`);
    return { feature_id: fields[0], row, column, value: cellValue };
  });
}

function parseDigitizedPolygons(value: string) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean).map((line, index) => {
    const [featureId, rawValue, rawVertices, ...extra] = line.split("|").map((item) => item.trim());
    if (!featureId || !rawValue || !rawVertices || extra.length) throw new Error(`Polygon line ${index + 1}: use feature-id|value|x,y;x,y;x,y.`);
    const polygonValue = Number(rawValue);
    if (!Number.isInteger(polygonValue)) throw new Error(`Polygon line ${index + 1} requires an integer value.`);
    const vertices = rawVertices.split(";").map((vertex, vertexIndex) => {
      const fields = vertex.split(",").map((item) => Number(item.trim()));
      if (fields.length !== 2 || fields.some((item) => !Number.isFinite(item))) throw new Error(`Polygon line ${index + 1}, vertex ${vertexIndex + 1}: use finite x,y coordinates.`);
      return { x: fields[0], y: fields[1] };
    });
    return { feature_id: featureId, value: polygonValue, vertices };
  });
}

function DigitizationPanel({ projectSession, reviewer, professionalAssets, onAssetPersisted }: { projectSession: ProfessionalProjectSession | null; reviewer: string; professionalAssets: ProfessionalAssetRecord[]; onAssetPersisted: (asset: ProfessionalAssetRecord) => void }) {
  const referenceAssets = professionalAssets.filter((asset) => asset.grid);
  const [draftId] = useState(() => stableUiId("digitization"));
  const [assetId] = useState(() => stableUiId("asset_digitized"));
  const [revisionId] = useState(() => stableUiId("revision"));
  const [kind, setKind] = useState<"FOCAL_AREAS" | "INTERVENTION_FOOTPRINT" | "RISK_REVIEW_MASK">("FOCAL_AREAS");
  const [referenceAssetId, setReferenceAssetId] = useState(referenceAssets[0]?.assetId ?? "");
  const [cellsText, setCellsText] = useState("");
  const [polygonsText, setPolygonsText] = useState("");
  const [title, setTitle] = useState("");
  const [license, setLicense] = useState("");
  const [ecologicalBasis, setEcologicalBasis] = useState("");
  const [reason, setReason] = useState("");
  const [reviewedAt, setReviewedAt] = useState("");
  const [ownership, setOwnership] = useState("");
  const [sensitivity, setSensitivity] = useState<ProfessionalAssetRecord["sensitivity"]>("INTERNAL");
  const [cloudDisposition, setCloudDisposition] = useState<ProfessionalAssetRecord["cloudDisposition"]>("PROHIBITED");
  const [generalizedAssetId, setGeneralizedAssetId] = useState("");
  const [exportAllowed, setExportAllowed] = useState(false);
  const [retention, setRetention] = useState("");
  const [spatialRedaction, setSpatialRedaction] = useState("");
  const [approvedDeployments, setApprovedDeployments] = useState<AssetDeployment[]>(["LOCAL_ONLY"]);
  const [authorizationConfirmed, setAuthorizationConfirmed] = useState(false);
  const [preview, setPreview] = useState<DigitizationPreview | null>(null);
  const [previewSignature, setPreviewSignature] = useState("");
  const [state, setState] = useState<"idle" | "previewing" | "publishing" | "error">("idle");
  const [message, setMessage] = useState("");
  const reference = referenceAssets.find((asset) => asset.assetId === referenceAssetId) ?? null;
  const signature = JSON.stringify({ kind, referenceAssetId, cellsText, polygonsText });
  const previewCurrent = Boolean(preview && previewSignature === signature);

  const draft = () => DigitizationDraftSchema.parse({ draft_id: draftId, kind, reference_asset_id: referenceAssetId, cells: parseDigitizedCells(cellsText), polygons: parseDigitizedPolygons(polygonsText) });
  const previewDraft = async () => {
    if (!projectSession) return;
    setState("previewing"); setMessage("");
    try {
      const authored = draft();
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.digitization.preview", { project_handle: projectSession.projectHandle, draft: authored });
      const result = DigitizationPreviewResponseSchema.parse(rpcData(response));
      if (result.preview.draft_id !== draftId || result.preview.reference_asset_id !== referenceAssetId) throw new Error("The worker returned a different digitization identity.");
      setPreview(result.preview); setPreviewSignature(signature); setState("idle");
      setMessage(`Exact preview covers ${result.preview.affected_cell_count.toLocaleString()} cells (${result.preview.affected_area_m2.toLocaleString()} m²). No derived asset exists yet.`);
    } catch (error) { setPreview(null); setPreviewSignature(""); setState("error"); setMessage(hostErrorMessage(error, "The worker rejected this digitization draft.")); }
  };
  const toggleDeployment = (deployment: AssetDeployment, checked: boolean) => setApprovedDeployments((current) => checked ? [...new Set([...current, deployment])] : current.filter((item) => item !== deployment));
  const publish = async () => {
    if (!projectSession || !preview || !previewCurrent || !reference?.grid) return;
    setState("publishing"); setMessage("");
    try {
      if ([title, license, ecologicalBasis, reason, reviewer, reviewedAt, ownership, retention, spatialRedaction].some((item) => !item.trim()) || !authorizationConfirmed || !approvedDeployments.length) throw new Error("Complete and confirm the reviewer, ecological basis, semantic revision, ownership, license, retention, redaction, and deployment authorization.");
      if (cloudDisposition === "PROHIBITED" && (approvedDeployments.length !== 1 || approvedDeployments[0] !== "LOCAL_ONLY")) throw new Error("PROHIBITED data may approve LOCAL_ONLY only.");
      if (cloudDisposition === "DEDICATED_OR_SELF_MANAGED_ONLY" && approvedDeployments.includes("WANDB_CLOUD")) throw new Error("Dedicated-only data cannot authorize multi-tenant W&B Cloud.");
      const reviewedIso = new Date(reviewedAt).toISOString();
      if (cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" && !generalizedAssetId.trim()) throw new Error("Generalized-only permission requires the stable ID of a separately retained and authorized derivative.");
      const authorization = SensitiveDataAuthorizationSchema.parse({ asset_id: assetId, ownership: ownership.trim(), license_spdx_or_name: license.trim(), sensitivity, cloud_disposition: cloudDisposition, export_allowed: exportAllowed, retention_policy: retention.trim(), spatial_redaction: spatialRedaction.trim(), approved_deployments: approvedDeployments, reviewer: reviewer.trim(), reviewed_at: reviewedIso, generalized_asset_id: cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" ? generalizedAssetId.trim() : null, notes: `Reviewer-authored ${kind.toLowerCase().replaceAll("_", " ")} on retained reference ${reference.assetId}.` });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.digitization.publish", { project_handle: projectSession.projectHandle, draft: draft(), expected_preview_sha256: preview.preview_sha256, asset_id: assetId, title: title.trim(), relative_path: `data_assets/digitized/${assetId}.asc`, license: license.trim(), authorization, reviewer: reviewer.trim(), reviewed_at: reviewedIso, ecological_basis: ecologicalBasis.trim(), revision_id: revisionId, reason: reason.trim() });
      const publication = DigitizationPublicationResponseSchema.parse(rpcData(response));
      if (publication.preview.preview_sha256 !== preview.preview_sha256 || publication.asset.asset_id !== assetId || publication.revision.revision_id !== revisionId || publication.provenance.reference_asset_sha256 !== reference.sha256) throw new Error("The publication differs from the reviewed preview, reference bytes, or semantic revision identity.");
      const asset = publication.asset;
      if (!asset.grid) throw new Error("The published digitization has no grid identity.");
      onAssetPersisted({ assetId: asset.asset_id, title: asset.title, kind: asset.kind, displayName: asset.relative_path.split("/").at(-1) ?? asset.relative_path, relativePath: asset.relative_path, sha256: asset.sha256, byteSize: asset.byte_size, mediaType: asset.media_type, source: asset.source, vintage: asset.vintage, license: asset.license, sensitivity: asset.authorization.sensitivity, cloudDisposition: asset.authorization.cloud_disposition, exportAllowed: asset.authorization.export_allowed, approvedDeployments: asset.authorization.approved_deployments, reviewer: asset.authorization.reviewer, reviewedAt: asset.authorization.reviewed_at, intendedUse: ecologicalBasis.trim(), revision: publication.revision.revision_id, grid: asset.grid, authorization: asset.authorization });
      setState("idle"); setMessage(`Published ${asset.title} as a new semantic revision. The reference asset and exact authored provenance remain retained.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The worker rejected this digitization publication.")); }
  };
  const visualCells = preview && preview.affected_cells.length <= 2_000 ? preview.affected_cells : [];
  const minRow = visualCells.length ? Math.min(...visualCells.map((cell) => cell.row)) : 0; const maxRow = visualCells.length ? Math.max(...visualCells.map((cell) => cell.row)) : 0;
  const minColumn = visualCells.length ? Math.min(...visualCells.map((cell) => cell.column)) : 0; const maxColumn = visualCells.length ? Math.max(...visualCells.map((cell) => cell.column)) : 0;

  return <section className="paper-panel digitization-panel" aria-labelledby="digitization-heading">
    <div className="panel-heading"><div><span className="section-kicker">Reviewer-authored geometry</span><h2 id="digitization-heading">Digitize on an exact retained grid</h2></div><StatusPill state={previewCurrent ? "pass" : "review"}>{previewCurrent ? "Exact preview" : "No derived asset"}</StatusPill></div>
    <div className="digitization-layout"><div className="digitization-editor"><div className="field-grid two-up"><label><span>Purpose *</span><select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="FOCAL_AREAS">Focal-area identifiers</option><option value="INTERVENTION_FOOTPRINT">Intervention footprint</option><option value="RISK_REVIEW_MASK">Risk review mask</option></select></label><label><span>Exact reference raster *</span><select value={referenceAssetId} onChange={(event) => setReferenceAssetId(event.target.value)}><option value="">Choose retained grid…</option>{referenceAssets.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.grid?.grid_id}</option>)}</select></label></div><label><span>Exact cells · one per line</span><textarea rows={5} className="mono" value={cellsText} onChange={(event) => setCellsText(event.target.value)} placeholder={kind === "FOCAL_AREAS" ? "core-a,12,44,1\ncore-b,24,70,2" : "feature-a,12,44,1"} /><small>Format: feature-id,row,column,value. Focal values are positive integer IDs; footprint and mask values are exactly 1.</small></label><label><span>Map-coordinate polygons · one per line</span><textarea rows={5} className="mono" value={polygonsText} onChange={(event) => setPolygonsText(event.target.value)} placeholder="core-a|1|500000,4200000;500100,4200000;500100,4200100;500000,4200100" /><small>Format: feature-id|value|x,y;x,y;x,y. Coordinates use the reference grid CRS; overlapping features are rejected.</small></label><button className="primary-button" type="button" disabled={!projectSession || !reference || state === "previewing" || state === "publishing" || (!cellsText.trim() && !polygonsText.trim())} onClick={() => void previewDraft()}>{state === "previewing" ? "Rasterizing exact draft…" : "Preview cells and NODATA"}</button></div>
      <div className="digitization-preview">{preview ? <><div className="digitization-summary"><span>{preview.kind.replaceAll("_", " ")}</span><strong>{preview.affected_cell_count.toLocaleString()} cells</strong><small>{preview.affected_area_m2.toLocaleString()} m² · {preview.value_kind}</small></div>{visualCells.length ? <svg viewBox={`0 0 ${Math.max(1, maxColumn - minColumn + 1)} ${Math.max(1, maxRow - minRow + 1)}`} role="img" aria-label={`Digitization orientation preview of ${visualCells.length} affected cells`} preserveAspectRatio="xMidYMid meet">{visualCells.map((cell) => <rect key={`${cell.row}:${cell.column}`} x={cell.column - minColumn} y={cell.row - minRow} width="1" height="1" className={`digitized-value-${Math.abs(cell.value) % 5}`} />)}</svg> : <div className="digitization-large"><Icon name="grid" /><p>Visual rendering is withheld above 2,000 cells to keep the interface responsive. The complete cell table and hashes remain authoritative.</p></div>}<dl><div><dt>Reference hash</dt><dd className="mono">{preview.reference_asset_sha256.slice(0, 16)}…</dd></div><div><dt>NODATA topology</dt><dd className="mono">{preview.nodata_topology_sha256.slice(0, 16)}…</dd></div><div><dt>Materialized hash</dt><dd className="mono">{preview.materialized_sha256.slice(0, 16)}…</dd></div><div><dt>Estimated bytes</dt><dd>{formatBytes(preview.estimated_materialized_bytes)}</dd></div></dl></> : <div className="empty-state"><Icon name="map" /><h3>No grid mutation preview</h3><p>The worker will rasterize exact authored geometry, reject overlap or NODATA changes, and return every affected cell before publishing.</p></div>}</div></div>
    {preview && <details className="digitization-cell-table"><summary>Accessible affected-cell table ({preview.affected_cell_count.toLocaleString()} rows)</summary><div className="table-scroll" tabIndex={0}><table><thead><tr><th>Feature</th><th>Row</th><th>Column</th><th>Value</th><th>Source</th></tr></thead><tbody>{preview.affected_cells.map((cell) => <tr key={`${cell.feature_id}:${cell.row}:${cell.column}`}><td>{cell.feature_id}</td><td>{cell.row}</td><td>{cell.column}</td><td>{cell.value}</td><td>{cell.source}</td></tr>)}</tbody></table></div></details>}
    {preview && <fieldset className="digitization-publication"><legend>Publish exact preview as a semantic revision</legend><div className="field-grid two-up"><label><span>Derived asset title *</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label><span>License or use terms *</span><input value={license} onChange={(event) => setLicense(event.target.value)} /></label><label><span>Owner or custodian *</span><input value={ownership} onChange={(event) => setOwnership(event.target.value)} /></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><label className="field-span"><span>Ecological basis *</span><textarea rows={3} value={ecologicalBasis} onChange={(event) => setEcologicalBasis(event.target.value)} /></label><label className="field-span"><span>Semantic revision reason *</span><textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} /></label><label><span>Sensitivity *</span><select value={sensitivity} onChange={(event) => setSensitivity(event.target.value as typeof sensitivity)}><option value="PUBLIC">Public</option><option value="INTERNAL">Internal</option><option value="SENSITIVE_LOCATION">Sensitive location</option><option value="CRITICAL_INFRASTRUCTURE">Critical infrastructure</option><option value="TRIBAL_OR_CULTURAL">Tribal or cultural</option><option value="LICENSE_RESTRICTED">License restricted</option></select></label><label><span>Cloud disposition *</span><select value={cloudDisposition} onChange={(event) => setCloudDisposition(event.target.value as typeof cloudDisposition)}><option value="RAW_ALLOWED">Raw allowed</option><option value="GENERALIZED_DERIVATIVE_ONLY">Generalized derivative only</option><option value="DEDICATED_OR_SELF_MANAGED_ONLY">Dedicated/Self-Managed only</option><option value="PROHIBITED">No cloud replication</option></select></label>{cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY" && <label className="field-span"><span>Authorized generalized derivative asset ID *</span><input className="mono" value={generalizedAssetId} onChange={(event) => setGeneralizedAssetId(event.target.value)} /></label>}<label><span>Retention policy *</span><input value={retention} onChange={(event) => setRetention(event.target.value)} /></label><label><span>Spatial redaction or explicit none *</span><input value={spatialRedaction} onChange={(event) => setSpatialRedaction(event.target.value)} /></label></div><fieldset className="asset-deployments"><legend>Approved destinations *</legend>{DEPLOYMENT_OPTIONS.map(([value, label]) => <label className="checkbox-row" key={value}><input type="checkbox" checked={approvedDeployments.includes(value)} onChange={(event) => toggleDeployment(value, event.target.checked)} />{label}</label>)}</fieldset><label className="checkbox-row"><input type="checkbox" checked={exportAllowed} onChange={(event) => setExportAllowed(event.target.checked)} />The derived asset may appear in an authorized export.</label><label className="confirmation-row"><input type="checkbox" checked={authorizationConfirmed} onChange={(event) => setAuthorizationConfirmed(event.target.checked)} /><span><strong>I reviewed the authored cells/polygons, ecological basis, license, sensitivity, retention, redaction, and destination permissions.</strong> The worker did not infer these values.</span></label><button className="primary-button" type="button" disabled={!previewCurrent || !authorizationConfirmed || state === "publishing"} onClick={() => void publish()}>{state === "publishing" ? "Publishing exact revision…" : "Publish reviewed digitization"}</button></fieldset>}
    {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
  </section>;
}

interface GridDraft {
  gridId: string;
  crs: string;
  width: string;
  height: string;
  transform: string;
  nodata: string;
  cellAreaM2: string;
}

function gridDraftFrom(grid: RasterGrid | null): GridDraft {
  return grid ? {
    gridId: grid.grid_id,
    crs: grid.crs,
    width: String(grid.width),
    height: String(grid.height),
    transform: grid.transform.join(", "),
    nodata: grid.nodata === null ? "" : String(grid.nodata),
    cellAreaM2: String(grid.cell_area_m2),
  } : { gridId: "", crs: "", width: "", height: "", transform: "", nodata: "", cellAreaM2: "" };
}

function parseTargetGrid(draft: GridDraft, source: RasterGrid): RasterGrid {
  const transform = draft.transform.split(",").map((item) => Number(item.trim()));
  const width = Number(draft.width);
  const height = Number(draft.height);
  const cellArea = Number(draft.cellAreaM2);
  const nodata = draft.nodata.trim() ? Number(draft.nodata) : null;
  if (!draft.gridId.trim() || !draft.crs.trim()) throw new Error("Enter a stable target grid ID and CRS.");
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) throw new Error("Target width and height must be positive integers.");
  if (transform.length !== 6 || transform.some((item) => !Number.isFinite(item))) throw new Error("Affine transform must contain exactly six finite numbers.");
  if (!Number.isFinite(cellArea) || cellArea <= 0) throw new Error("Target cell area must be positive.");
  if (nodata !== null && !Number.isFinite(nodata)) throw new Error("Target NODATA must be blank or a finite number.");
  return {
    grid_id: draft.gridId.trim(),
    crs: draft.crs.trim(),
    width,
    height,
    transform: transform as RasterGrid["transform"],
    nodata,
    value_kind: source.value_kind,
    cell_area_m2: cellArea,
  };
}

function GridAlignmentScreen({ planning, onPlanning, projectSession, professionalAssets, professionalGridApprovals, onGridApproved }: Pick<ScreensProps, "planning" | "onPlanning" | "projectSession" | "professionalAssets" | "professionalGridApprovals" | "onGridApproved">) {
  const rasterAssets = professionalAssets.filter((item) => item.grid);
  const first = rasterAssets[0] ?? null;
  const [sourceAssetId, setSourceAssetId] = useState(first?.assetId ?? "");
  const [draft, setDraft] = useState<GridDraft>(() => gridDraftFrom(first?.grid ?? null));
  const [method, setMethod] = useState<"NONE" | "NEAREST" | "BILINEAR">("NONE");
  const [planId, setPlanId] = useState(() => stableUiId("alignment"));
  const [plan, setPlan] = useState<GridAlignmentPlan | null>(null);
  const [state, setState] = useState<"idle" | "previewing" | "approving" | "error">("idle");
  const [message, setMessage] = useState("");
  const [reviewer, setReviewer] = useState(planning.reviewer);
  const [approvedAt, setApprovedAt] = useState("");
  const [purpose, setPurpose] = useState("");
  const [reason, setReason] = useState("");
  const [warningsAcknowledged, setWarningsAcknowledged] = useState(false);
  const [derivedAssetId, setDerivedAssetId] = useState(() => stableUiId("asset_aligned"));
  const [derivedTitle, setDerivedTitle] = useState("");
  const [approvalId, setApprovalId] = useState(() => stableUiId("alignment_approval"));
  const [revisionId, setRevisionId] = useState(() => stableUiId("revision"));
  const source = rasterAssets.find((item) => item.assetId === sourceAssetId) ?? null;
  const sourceGrid = source?.grid ?? null;
  const approved = professionalGridApprovals.find((item) => item.plan.plan_id === plan?.plan_id) ?? null;
  const methodLockedToNearest = sourceGrid?.value_kind === "IDENTIFIER" || sourceGrid?.value_kind === "MASK" || sourceGrid?.value_kind === "CATEGORICAL";

  const invalidate = (alignmentMethod = planning.alignmentMethod) => {
    setPlan(null);
    setWarningsAcknowledged(false);
    onPlanning({ ...planning, alignmentMethod, alignmentApproved: false });
  };
  const chooseSource = (assetId: string) => {
    const next = rasterAssets.find((item) => item.assetId === assetId) ?? null;
    setSourceAssetId(assetId);
    setDraft(gridDraftFrom(next?.grid ?? null));
    setMethod("NONE");
    setPlanId(stableUiId("alignment"));
    setDerivedAssetId(stableUiId("asset_aligned"));
    setDerivedTitle(next ? `${next.title} · aligned derivative` : "");
    setApprovalId(stableUiId("alignment_approval"));
    setRevisionId(stableUiId("revision"));
    invalidate();
    setMessage("");
  };
  const updateDraft = (field: keyof GridDraft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    invalidate();
  };
  const preview = async () => {
    if (!projectSession || !source || !sourceGrid) return;
    setState("previewing");
    setMessage("");
    try {
      const targetGrid = parseTargetGrid(draft, sourceGrid);
      const selectedMethod = methodLockedToNearest ? "NEAREST" : method;
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.grid.preview", {
        project_handle: projectSession.projectHandle,
        source_asset_id: source.assetId,
        target_grid: targetGrid,
        method: selectedMethod,
        plan_id: planId,
      });
      const parsed = GridPreviewResponseSchema.parse(rpcData(response));
      if (parsed.plan.source_asset_id !== source.assetId || parsed.plan.plan_id !== planId || parsed.plan.source_sha256 !== source.sha256) {
        throw new Error("The returned alignment plan does not match the selected retained source asset.");
      }
      setPlan(parsed.plan);
      setDerivedTitle(`${source.title} · aligned derivative`);
      setState("idle");
      setMessage("Preview persisted. No derived raster exists until a reviewer approves this exact plan and every warning.");
    } catch (error) {
      setPlan(null);
      setState("error");
      setMessage(hostErrorMessage(error, "The worker could not create this alignment preview."));
    }
  };
  const approve = async () => {
    if (!projectSession || !source || !sourceGrid || !plan) return;
    setState("approving");
    setMessage("");
    try {
      if (!reviewer.trim() || !approvedAt || !purpose.trim() || !reason.trim() || !derivedTitle.trim()) throw new Error("Complete the reviewer, review time, purpose, derived title, and revision reason.");
      if ((plan.warnings?.length ?? 0) > 0 && !warningsAcknowledged) throw new Error("Acknowledge every worker warning before approval.");
      const reviewedAtIso = new Date(approvedAt).toISOString();
      const authorization = {
        ...source.authorization,
        asset_id: derivedAssetId,
        reviewer: reviewer.trim(),
        reviewed_at: reviewedAtIso,
        notes: `${source.authorization.notes ?? ""}\nDerived only through approved plan ${plan.plan_id}; permission is no broader than the retained source.`.trim(),
      };
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.grid.approve", {
        project_handle: projectSession.projectHandle,
        plan_id: plan.plan_id,
        approval_id: approvalId,
        reviewer: reviewer.trim(),
        approved_at: reviewedAtIso,
        acknowledged_warnings: warningsAcknowledged ? plan.warnings ?? [] : [],
        purpose: purpose.trim(),
        derived_asset_id: derivedAssetId,
        derived_title: derivedTitle.trim(),
        derived_relative_path: `data_assets/derived/${derivedAssetId}.asc`,
        derived_authorization: authorization,
        revision_id: revisionId,
        reason: reason.trim(),
      });
      const parsed = GridApprovalResponseSchema.parse(rpcData(response));
      const derivedGrid = parsed.derived_asset.grid;
      if (parsed.approval.plan_id !== plan.plan_id || parsed.approval.approval_id !== approvalId || parsed.derived_asset.asset_id !== derivedAssetId || parsed.original_asset_retained !== source.assetId || !derivedGrid) {
        throw new Error("The approval response does not match the reviewed plan, source, and derived identities.");
      }
      const asset = parsed.derived_asset;
      onGridApproved({ plan, approvalId, derivedAssetId, revisionId: parsed.revision.revision_id }, {
        assetId: asset.asset_id,
        title: asset.title,
        kind: asset.kind,
        displayName: asset.relative_path.split("/").at(-1) ?? asset.relative_path,
        relativePath: asset.relative_path,
        sha256: asset.sha256,
        byteSize: asset.byte_size,
        mediaType: asset.media_type,
        source: asset.source,
        vintage: asset.vintage,
        license: asset.license,
        sensitivity: asset.authorization.sensitivity,
        cloudDisposition: asset.authorization.cloud_disposition,
        exportAllowed: asset.authorization.export_allowed,
        approvedDeployments: asset.authorization.approved_deployments,
        reviewer: asset.authorization.reviewer,
        reviewedAt: asset.authorization.reviewed_at,
        intendedUse: purpose.trim(),
        revision: parsed.revision.revision_id,
        grid: derivedGrid,
        authorization: asset.authorization,
      });
      onPlanning({ ...planning, alignmentApproved: true });
      setState("idle");
      setMessage(`Approved derivative ${asset.asset_id}; retained source ${source.assetId} remains immutable.`);
    } catch (error) {
      setState("error");
      setMessage(hostErrorMessage(error, "The worker could not approve and materialize this plan."));
    }
  };

  return (
    <>
      <PageIntro kicker="Assembly · 03" title="Make each transformation reviewable." description="No reprojection, resampling, NODATA change, or focal merge occurs until an explicit Grid Alignment Plan is previewed and approved." badge={<StatusPill state={planning.alignmentApproved ? "pass" : "review"}>{planning.alignmentApproved ? "Plan approved" : "Approval required"}</StatusPill>} />
      {!rasterAssets.length ? <section className="paper-panel blocked-workspace"><Icon name="lock" /><div><h2>Add and inspect a modeled raster first.</h2><p>The worker needs retained source bytes plus exact CRS, dimensions, affine transform, NODATA, value semantics, and cell area before it can preview any change.</p></div></section> : <div className="split-grid grid-review-layout">
        <section className="paper-panel form-panel"><div className="form-section-heading"><span>Target grid</span><h2>Alignment contract</h2></div>
          <label><span>Retained source raster</span><select value={sourceAssetId} onChange={(event) => chooseSource(event.target.value)}>{rasterAssets.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.grid?.value_kind}</option>)}</select></label>
          <label><span>Resampling method</span><select value={methodLockedToNearest ? "NEAREST" : method} disabled={methodLockedToNearest} onChange={(event) => { const next = event.target.value as typeof method; setMethod(next); invalidate(next === "BILINEAR" ? "bilinear" : "nearest"); }}><option value="NONE">No change · exact grid confirmation</option><option value="NEAREST">Nearest neighbor</option>{sourceGrid?.value_kind === "CONTINUOUS" && <option value="BILINEAR">Bilinear</option>}</select><small>{methodLockedToNearest ? `${sourceGrid?.value_kind} grids require nearest-neighbor alignment.` : "Continuous resistance permits explicit nearest or bilinear alignment. NONE requires exact source and target identity."}</small></label>
          <div className="field-grid two-up"><label><span>Target grid ID</span><input className="mono" value={draft.gridId} onChange={(event) => updateDraft("gridId", event.target.value)} /></label><label><span>Target CRS</span><input className="mono" value={draft.crs} onChange={(event) => updateDraft("crs", event.target.value)} /></label><label><span>Width</span><input inputMode="numeric" value={draft.width} onChange={(event) => updateDraft("width", event.target.value)} /></label><label><span>Height</span><input inputMode="numeric" value={draft.height} onChange={(event) => updateDraft("height", event.target.value)} /></label><label className="field-span"><span>Affine transform · six comma-separated numbers</span><input className="mono" value={draft.transform} onChange={(event) => updateDraft("transform", event.target.value)} /></label><label><span>NODATA · blank means none</span><input inputMode="decimal" value={draft.nodata} onChange={(event) => updateDraft("nodata", event.target.value)} /></label><label><span>Cell area (m²)</span><input inputMode="decimal" value={draft.cellAreaM2} onChange={(event) => updateDraft("cellAreaM2", event.target.value)} /></label></div>
          <button className="primary-button" type="button" disabled={!projectSession || state === "previewing" || state === "approving"} onClick={() => void preview()}>{state === "previewing" ? "Computing preview…" : <>Preview alignment plan <Icon name="eye" /></>}</button><p className="control-requirement">Preview is read-only. The worker calculates target cell count, estimated NODATA effects, category counts, warnings, and the resulting content hash before approval.</p></section>
        <section className="paper-panel alignment-preview" aria-live="polite">{plan ? <><div className="form-section-heading"><span>Worker preview · {plan.plan_id}</span><h2>{plan.method === "NONE" ? "Explicit no-change confirmation" : `${plan.method} derived grid`}</h2></div><dl className="alignment-metrics"><div><dt>Cells</dt><dd>{plan.source_cell_count.toLocaleString()} → {plan.target_cell_count.toLocaleString()}</dd></div><div><dt>Estimated NODATA</dt><dd>{plan.source_nodata_count.toLocaleString()} → {plan.estimated_target_nodata_count.toLocaleString()}</dd></div><div><dt>Result hash</dt><dd className="mono">sha256:{plan.resulting_sha256.slice(0, 16)}…</dd></div><div><dt>Original retained</dt><dd>{source?.assetId}</dd></div></dl>{plan.warnings?.length ? <div className="alignment-warnings"><strong>Worker warnings</strong><ul>{plan.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul><label className="checkbox-row"><input type="checkbox" checked={warningsAcknowledged} onChange={(event) => setWarningsAcknowledged(event.target.checked)} />I reviewed and acknowledge every warning above.</label></div> : <p className="alignment-clean"><Icon name="check" /> No worker warning was reported for this plan. Ecological review is still required.</p>}<div className="alignment-approval-fields"><label><span>Reviewer *</span><input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label><span>Approved at *</span><input type="datetime-local" value={approvedAt} onChange={(event) => setApprovedAt(event.target.value)} /></label><label className="field-span"><span>Purpose *</span><textarea rows={2} value={purpose} onChange={(event) => setPurpose(event.target.value)} /></label><label className="field-span"><span>Derived asset title *</span><input value={derivedTitle} onChange={(event) => setDerivedTitle(event.target.value)} /></label><label className="field-span"><span>Semantic revision reason *</span><textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} /></label></div><button className="primary-button" type="button" disabled={Boolean(approved) || state === "approving" || ((plan.warnings?.length ?? 0) > 0 && !warningsAcknowledged)} onClick={() => void approve()}>{state === "approving" ? "Materializing approved derivative…" : approved ? "Plan already approved" : "Approve exact plan and create revision"}</button></> : <div className="empty-state"><Icon name="grid" /><h2>No worker preview exists</h2><p>No raster was changed. Approval remains unavailable until the worker returns cell-count changes, NODATA effects, categorical statistics, warnings, and a result hash.</p></div>}</section>
      </div>}
      {message && <p className={`operation-message alignment-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
    </>
  );
}

function HypothesesScreen({ planning, onPlanning, projectSession, professionalAssets, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "planning" | "onPlanning" | "projectSession" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords">) {
  const assets = professionalAssets.filter((item) => item.kind === "RESISTANCE_RASTER" && item.grid);
  const [draft, setDraft, undoDraft, draftDirty, commitDraft] = useWorkspaceDraft("hypothesis-editor", () => ({
    hypothesisId: stableUiId("hypothesis"), title: "", assetId: assets[0]?.assetId ?? "", guildId: "", lifeStage: "",
    futureCondition: "Current planning condition", maturityHorizon: "Planning horizon", derivation: "", citations: "",
    confidence: "INFERRED" as "CONFIRMED" | "INFERRED" | "ASSUMED", reviewedAt: "",
  }));
  const { hypothesisId, title, assetId, guildId, lifeStage, futureCondition, maturityHorizon, derivation, citations, confidence, reviewedAt } = draft;
  const updateDraft = <K extends keyof typeof draft>(field: K, value: (typeof draft)[K]) => setDraft((current) => ({ ...current, [field]: value }));
  const setTitle = (value: string) => updateDraft("title", value);
  const setAssetId = (value: string) => updateDraft("assetId", value);
  const setGuildId = (value: string) => updateDraft("guildId", value);
  const setLifeStage = (value: string) => updateDraft("lifeStage", value);
  const setFutureCondition = (value: string) => updateDraft("futureCondition", value);
  const setMaturityHorizon = (value: string) => updateDraft("maturityHorizon", value);
  const setDerivation = (value: string) => updateDraft("derivation", value);
  const setCitations = (value: string) => updateDraft("citations", value);
  const setConfidence = (value: typeof confidence) => updateDraft("confidence", value);
  const setReviewedAt = (value: string) => updateDraft("reviewedAt", value);
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const save = async () => {
    if (!projectSession) return;
    setState("saving");
    setMessage("");
    try {
      const asset = assets.find((item) => item.assetId === assetId);
      if (!asset?.grid) throw new Error("Select a persisted resistance raster with worker-inspected grid evidence.");
      if ([title, guildId, lifeStage, futureCondition, maturityHorizon, planning.hypothesisRationale, derivation, citations, planning.reviewer].some((item) => !item.trim()) || !reviewedAt) throw new Error("Complete every hypothesis field, evidence citation, reviewer, and review time.");
      const hypothesis = ResistanceHypothesisSchema.parse({ hypothesis_id: hypothesisId, title: title.trim(), guild_id: guildId.trim(), life_stage: lifeStage.trim(), future_condition: futureCondition.trim(), maturity_horizon: maturityHorizon.trim(), resistance_asset_id: asset.assetId, grid_id: asset.grid.grid_id, rationale: planning.hypothesisRationale.trim(), derivation: derivation.trim(), evidence_citations: citations.split("\n").map((item) => item.trim()).filter(Boolean), confidence, reviewed_by: planning.reviewer.trim(), reviewed_at: new Date(reviewedAt).toISOString() });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.hypothesis.save", { project_handle: projectSession.projectHandle, hypothesis });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      if (saved.record_id !== hypothesis.hypothesis_id) throw new Error("The worker returned a different hypothesis identity.");
      onProfessionalRecords({ ...professionalRecords, hypotheses: [...professionalRecords.hypotheses.filter((item) => item.hypothesis_id !== hypothesis.hypothesis_id), hypothesis] });
      commitDraft();
      setState("idle");
      setMessage(`Reviewed hypothesis persisted at worker revision ${saved.revision}.`);
    } catch (error) {
      setState("error");
      setMessage(hostErrorMessage(error, "Complete every hypothesis field with valid reviewed evidence."));
    }
  };
  return (
    <>
      <PageIntro kicker="Assembly · 04" title="Treat resistance as a hypothesis—not a fact." description="Document what each surface represents, where its values came from, and which uncertainty it is meant to test." badge={<StatusPill state={professionalRecords.hypotheses.length ? "pass" : planning.hypothesisRationale ? "review" : "fail"}>{professionalRecords.hypotheses.length ? `${professionalRecords.hypotheses.length} reviewed` : planning.hypothesisRationale ? "Draft needs reviewer" : "Rationale missing"}</StatusPill>} />
      <section className="paper-panel hypothesis-ledger"><div className="panel-heading"><div><span className="section-kicker">Hypothesis editor</span><h2>Ecologist-authored meaning</h2></div><div className="draft-actions"><span className="mono">{draftDirty ? "Draft retained in workspace" : `${professionalRecords.hypotheses.length} saved`}</span><button className="text-action" type="button" disabled={!draftDirty} onClick={undoDraft}>Undo draft changes</button></div></div><article><span className="hypothesis-code">H</span><div className="reviewed-record-form"><div className="field-grid two-up"><label><span>Title *</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label><span>Reviewed resistance asset *</span><select value={assetId} onChange={(event) => setAssetId(event.target.value)}><option value="">Choose persisted raster…</option>{assets.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.grid?.grid_id}</option>)}</select></label><label><span>Guild ID *</span><input value={guildId} onChange={(event) => setGuildId(event.target.value)} /></label><label><span>Life stage *</span><input value={lifeStage} onChange={(event) => setLifeStage(event.target.value)} /></label><label><span>Future condition *</span><input value={futureCondition} onChange={(event) => setFutureCondition(event.target.value)} /></label><label><span>Maturity horizon *</span><input value={maturityHorizon} onChange={(event) => setMaturityHorizon(event.target.value)} /></label><label className="field-span"><span>Movement-process rationale *</span><textarea rows={4} value={planning.hypothesisRationale} onChange={(event) => onPlanning({ ...planning, hypothesisRationale: event.target.value })} placeholder="Explain intended movement process, spatial scale, boundary effects, and why higher cells mean greater resistance." /></label><label className="field-span"><span>Resistance derivation *</span><textarea rows={3} value={derivation} onChange={(event) => setDerivation(event.target.value)} placeholder="State how reviewed values were authored; do not infer them from context layers here." /></label><label className="field-span"><span>Evidence citations * · one per line</span><textarea rows={3} value={citations} onChange={(event) => setCitations(event.target.value)} /></label><label><span>Evidence confidence *</span><select value={confidence} onChange={(event) => setConfidence(event.target.value as typeof confidence)}><option value="CONFIRMED">Confirmed</option><option value="INFERRED">Inferred</option><option value="ASSUMED">Assumed</option></select></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label></div><div className="hypothesis-meta"><span>Reviewer: {planning.reviewer || "required in Study context"}</span><span>Resistance semantics only</span><span>Grid and source checksum stay linked</span></div><button className="primary-button" type="button" disabled={!projectSession || !assets.length || state === "saving"} onClick={() => void save()}>{state === "saving" ? "Persisting reviewed hypothesis…" : "Save reviewed hypothesis"}</button>{message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}</div></article><p className="control-requirement">Unconfirmed edits are retained only in this open workspace. This records a reviewed hypothesis; it does not validate biological truth or create resistance values.</p></section>
      <div className="assumption-callout"><Icon name="warning" /><p><strong>The application will not infer resistance from land cover.</strong> Open-data layers are context. A qualified reviewer must author or import every resistance value and its evidence basis.</p></div>
    </>
  );
}

function FocalAreasScreen({ planning, onPlanning, projectSession, professionalAssets, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "planning" | "onPlanning" | "projectSession" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords">) {
  const assets = professionalAssets.filter((item) => item.kind === "FOCAL_RASTER" && item.grid?.value_kind === "IDENTIFIER");
  const [draft, setDraft, undoDraft, draftDirty, commitDraft] = useWorkspaceDraft("focal-area-editor", () => ({ focalSetId: stableUiId("focal_set"), title: "", assetId: assets[0]?.assetId ?? "", guildId: "", focalIds: "", pairText: "", reviewedAt: "" }));
  const { focalSetId, title, assetId, guildId, focalIds, pairText, reviewedAt } = draft;
  const updateDraft = <K extends keyof typeof draft>(field: K, value: (typeof draft)[K]) => setDraft((current) => ({ ...current, [field]: value }));
  const setTitle = (value: string) => updateDraft("title", value);
  const setAssetId = (value: string) => updateDraft("assetId", value);
  const setGuildId = (value: string) => updateDraft("guildId", value);
  const setFocalIds = (value: string) => updateDraft("focalIds", value);
  const setPairText = (value: string) => updateDraft("pairText", value);
  const setReviewedAt = (value: string) => updateDraft("reviewedAt", value);
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const save = async () => {
    if (!projectSession) return;
    setState("saving");
    setMessage("");
    try {
      const asset = assets.find((item) => item.assetId === assetId);
      if (!asset?.grid) throw new Error("Select a worker-inspected identifier raster.");
      const ids = focalIds.split(",").map((item) => Number(item.trim())).filter((item) => Number.isInteger(item));
      const pairs = pairText.split(",").map((item, index) => {
        const values = item.trim().split(/[-:]/).map((value) => Number(value.trim()));
        if (values.length !== 2 || values.some((value) => !Number.isInteger(value) || value <= 0)) throw new Error(`Pair ${item || index + 1} must use positive IDs such as 1-2.`);
        const [from, to] = values[0] < values[1] ? values : [values[1], values[0]];
        return { pair_id: `${focalSetId}_pair_${from}_${to}`, from_focal_id: from, to_focal_id: to, required: true };
      });
      if ([title, guildId, planning.focalRationale, planning.reviewer].some((item) => !item.trim()) || !reviewedAt) throw new Error("Complete title, guild, rationale, reviewer, and review time.");
      const focalAreaSet = FocalAreaSetSchema.parse({ focal_set_id: focalSetId, title: title.trim(), asset_id: asset.assetId, grid_id: asset.grid.grid_id, guild_id: guildId.trim(), focal_ids: ids, pairs, rationale: planning.focalRationale.trim(), reviewed_by: planning.reviewer.trim(), reviewed_at: new Date(reviewedAt).toISOString() });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.focal_areas.save", { project_handle: projectSession.projectHandle, focal_area_set: focalAreaSet });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      if (saved.record_id !== focalAreaSet.focal_set_id) throw new Error("The worker returned a different focal-set identity.");
      onProfessionalRecords({ ...professionalRecords, focalAreaSets: [...professionalRecords.focalAreaSets.filter((item) => item.focal_set_id !== focalAreaSet.focal_set_id), focalAreaSet] });
      commitDraft();
      setState("idle");
      setMessage(`Reviewed focal set persisted at worker revision ${saved.revision}.`);
    } catch (error) {
      setState("error");
      setMessage(hostErrorMessage(error, "Complete a valid reviewed focal set and explicit pair list."));
    }
  };
  return (
    <>
      <PageIntro kicker="Assembly · 05" title="Explain why these places must connect." description="Focal areas establish the question Circuitscape solves. Their rationale, identity, scale, and included pairs must be explicit." badge={<StatusPill state={professionalRecords.focalAreaSets.length ? "pass" : planning.focalRationale ? "review" : "fail"}>{professionalRecords.focalAreaSets.length ? `${professionalRecords.focalAreaSets.length} reviewed` : planning.focalRationale ? "Draft focal set" : "Focal set required"}</StatusPill>} />
      <div className="focal-layout">
        <section className="paper-panel focal-sketch" aria-label="Conceptual focal relationship sketch"><div className="focal-placeholder"><span>A</span><span>B</span><i /><span>C</span><span>D</span></div><p>Relationship sketch only. Import a reviewed IDENTIFIER raster, or publish reviewer-authored cells or polygons from Source assets. This screen binds that retained geometry to explicit focal IDs and pairs; it never merges or alters the asset.</p></section>
        <section className="paper-panel form-panel"><div className="form-section-heading"><span>Focal-set editor</span><h2>Rationale and explicit pairs</h2><div className="draft-actions"><small>{draftDirty ? "Draft retained in workspace" : "No unsaved field changes"}</small><button className="text-action" type="button" disabled={!draftDirty} onClick={undoDraft}>Undo draft changes</button></div></div><label><span>Title *</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label><span>Reviewed identifier raster *</span><select value={assetId} onChange={(event) => setAssetId(event.target.value)}><option value="">Choose persisted focal raster…</option>{assets.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.grid?.grid_id}</option>)}</select></label><label><span>Guild ID *</span><input value={guildId} onChange={(event) => setGuildId(event.target.value)} /></label><label><span>Positive focal IDs * · comma separated</span><input className="mono" value={focalIds} onChange={(event) => setFocalIds(event.target.value)} placeholder="1, 2, 3, 4" /></label><label><span>Required pairs * · comma separated</span><input className="mono" value={pairText} onChange={(event) => setPairText(event.target.value)} placeholder="1-2, 1-3, 2-4" /></label><label><span>Ecological rationale *</span><textarea rows={5} value={planning.focalRationale} onChange={(event) => onPlanning({ ...planning, focalRationale: event.target.value })} placeholder="Describe guild, evidence basis, core selection, exclusions, scale, and intended comparisons." /></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><button className="primary-button" type="button" disabled={!projectSession || !assets.length || state === "saving"} onClick={() => void save()}>{state === "saving" ? "Persisting reviewed focal set…" : "Save reviewed focal set"}</button>{message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}<p className="control-requirement">Unconfirmed edits remain local to this open workspace. Saving requires an authorized IDENTIFIER raster, explicit pairs, guild, rationale, reviewer, and review time.</p></section>
      </div>
    </>
  );
}

interface ValidationFinding {
  code: string;
  severity: "BLOCKING" | "REVIEW_REQUIRED" | "INFORMATION";
  object_reference: string;
  remediation: string;
}

function ModelFitnessScreen({ path, projectSession, professionalAnalysis, onEvidence, fixture, fixtureVerified, guidedProjectionError }: Pick<ScreensProps, "path" | "projectSession" | "professionalAnalysis" | "onEvidence" | "fixture" | "fixtureVerified" | "guidedProjectionError">) {
  const guided = path === "guided";
  const retainedValidation = ModelValidationResponseSchema.safeParse(professionalAnalysis.projectState?.validation);
  const [validation, setValidation] = useState<{ status: "READY" | "BLOCKED"; findings: ValidationFinding[] } | null>(() => retainedValidation.success ? retainedValidation.data : null);
  const [fitness, setFitness] = useState<ConnectivityModelFitness | null>(() => professionalAnalysis.projectState?.fitness ?? null);
  const [validationState, setValidationState] = useState<"idle" | "checking" | "attesting" | "error">("idle");
  const [validationMessage, setValidationMessage] = useState("");
  const [reviewer, setReviewer] = useState(() => professionalAnalysis.projectState?.fitness.reviewed_by ?? "");
  const [reviewedAt, setReviewedAt] = useState(() => professionalAnalysis.projectState?.fitness.reviewed_at?.slice(0, 16) ?? "");
  const [unresolvedAssumptions, setUnresolvedAssumptions] = useState(() => (professionalAnalysis.projectState?.fitness.unresolved_assumptions ?? []).join("\n"));
  const [attestation, setAttestation] = useState("");
  const [attestationConfirmed, setAttestationConfirmed] = useState(false);

  useEffect(() => {
    if (guided || !professionalAnalysis.projectState) return;
    const current = ConnectivityModelFitnessSchema.parse(professionalAnalysis.projectState.fitness);
    const currentValidation = ModelValidationResponseSchema.safeParse(professionalAnalysis.projectState.validation);
    setFitness(current);
    setValidation(currentValidation.success ? currentValidation.data : null);
    setReviewer(current.reviewed_by ?? "");
    setReviewedAt(current.reviewed_at?.slice(0, 16) ?? "");
    setUnresolvedAssumptions((current.unresolved_assumptions ?? []).join("\n"));
  }, [guided, professionalAnalysis.projectState?.active_revision.revision_id]);

  const loadAuthoritativeState = async (): Promise<ConnectivityModelFitness> => {
    if (!projectSession) throw new Error("Open a professional project first.");
    const response = await hostRequest<RpcPayload<unknown>>("connectivity.project.get_state", { project_handle: projectSession.projectHandle });
    const projectState = ProfessionalProjectStateSchema.parse(rpcData(response));
    if (projectState.project.project_id !== projectSession.projectId) throw new Error("The worker returned a different project identity.");
    const current = ConnectivityModelFitnessSchema.parse(projectState.fitness);
    setFitness(current);
    setReviewer(current.reviewed_by ?? "");
    setReviewedAt(current.reviewed_at ? current.reviewed_at.slice(0, 16) : "");
    setUnresolvedAssumptions((current.unresolved_assumptions ?? []).join("\n"));
    return current;
  };

  const validate = async () => {
    if (!projectSession) return;
    setValidationState("checking");
    setValidationMessage("");
    try {
      const result = await hostRequest<RpcPayload<unknown>>("connectivity.model.validate", { project_handle: projectSession.projectHandle });
      const data = ModelValidationResponseSchema.parse(rpcData(result));
      await loadAuthoritativeState();
      setValidation(data);
      setValidationState("idle");
      setValidationMessage(data.status === "READY" ? "The retained project passed the worker's blocking model checks. A reviewer may now complete each passport dimension." : `The worker recorded ${data.findings.length} finding${data.findings.length === 1 ? "" : "s"}. Blocking findings cannot be attested away.`);
    } catch (reason) {
      setValidationState("error");
      setValidationMessage(hostErrorMessage(reason, "The worker could not complete model validation."));
    }
  };

  const updateDimension = (index: number, field: "status" | "evidence_ids" | "notes", value: string) => {
    setFitness((current) => {
      if (!current) return current;
      const dimensions = current.dimensions.map((dimension, dimensionIndex) => dimensionIndex === index ? {
        ...dimension,
        [field]: field === "evidence_ids" ? value.split("\n").map((item) => item.trim()).filter(Boolean) : value,
      } : dimension);
      return { ...current, dimensions } as ConnectivityModelFitness;
    });
  };

  const blockingDimensions = fitness?.dimensions.filter((dimension) => dimension.status === "BLOCKING") ?? [];
  const incompleteDimensions = fitness?.dimensions.filter((dimension) => !dimension.notes.trim() || !(dimension.evidence_ids ?? []).length) ?? [];
  const candidateResolvedFindingCodes = new Set(["FITNESS_BLOCKER", "MODEL_FITNESS_ATTESTATION_MISSING"]);
  const nonFitnessBlockers = validation?.findings.filter((finding) => finding.severity === "BLOCKING" && !candidateResolvedFindingCodes.has(finding.code)) ?? [];
  const candidateReviewAllowed = Boolean(validation && !nonFitnessBlockers.length);
  const canAttest = Boolean(candidateReviewAllowed && fitness && !blockingDimensions.length && !incompleteDimensions.length && reviewer.trim() && reviewedAt && attestation.trim() && attestationConfirmed);
  const attest = async () => {
    if (!projectSession || !fitness || !canAttest) return;
    setValidationState("attesting");
    setValidationMessage("");
    try {
      const reviewedFitness = ConnectivityModelFitnessSchema.parse({
        ...fitness,
        revision_id: fitness.revision_id,
        dimensions: fitness.dimensions.map((dimension) => ({ ...dimension, reviewer: reviewer.trim() })),
        unresolved_assumptions: unresolvedAssumptions.split("\n").map((item) => item.trim()).filter(Boolean),
        reviewed_by: reviewer.trim(),
        reviewed_at: new Date(reviewedAt).toISOString(),
      });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.model.attest", {
        project_handle: projectSession.projectHandle,
        fitness: reviewedFitness,
        reviewer: reviewer.trim(),
        attestation: attestation.trim(),
      });
      const receipt = ModelAttestResponseSchema.parse(rpcData(response));
      if (receipt.passport_id !== reviewedFitness.passport_id) throw new Error("The worker returned a different passport identity.");
      setFitness({ ...reviewedFitness, attestations: [...(reviewedFitness.attestations ?? []), attestation.trim()] });
      setAttestationConfirmed(false);
      setValidationState("idle");
      setValidationMessage(`Passport attested in authoritative worker revision ${receipt.fitness_revision}. Analysis still remains subject to every current validation, scenario, authorization, and direct-run gate.`);
    } catch (reason) {
      setValidationState("error");
      setValidationMessage(hostErrorMessage(reason, "The worker rejected this Model Fitness attestation."));
    }
  };

  const rows = guided ? [
    ["Intended use", "pass", "Synthetic teaching comparison only"],
    ["Resistance derivation", "review", "Four synthetic reviewed hypotheses; no field validation"],
    ["Focal-area rationale", "pass", "Four anonymous synthetic habitat cores"],
    ["Action-effect evidence", "review", "Synthetic immutable effects for teaching"],
    ["Scenario coverage", "pass", "12 target and safeguard scenarios"],
    ["Numerical QA", "pass", "Required for every direct run in authoritative manifest"],
    ["Validation evidence", "review", "No telemetry, movement, or reference-fit claim"],
    ["Unresolved assumptions", "review", "Land access at the north approach"],
  ] as const : validation?.findings.length ? validation.findings.map((finding) => [
    finding.object_reference || finding.code,
    finding.severity === "BLOCKING" ? "fail" : "review",
    finding.remediation,
  ] as const) : fitness?.dimensions.length ? fitness.dimensions.map((dimension) => [
    dimension.key.replaceAll("_", " "),
    dimension.status === "ACCEPTED" ? "pass" : dimension.status === "BLOCKING" ? "fail" : "review",
    dimension.notes || ((dimension.evidence_ids ?? []).length ? `${dimension.evidence_ids?.length} retained evidence reference${dimension.evidence_ids?.length === 1 ? "" : "s"}` : "No reviewer rationale is retained for this dimension."),
  ] as const) : [
    ["Authoritative passport", "review", projectSession ? "No fitness dimensions are retained for this active revision. Run the worker check to record its disposition." : "Open a professional project to read its retained Model Fitness Passport."],
  ] as const;
  return (
    <>
      <PageIntro kicker="Independent review" title="Model Fitness Passport" description="Fitness is reported by dimension. There is no score that can hide a weak ecological basis behind strong numerical QA." badge={<StatusPill state="review">Limitations remain visible</StatusPill>} />
      {guided && <ProjectionNotice fixture={fixture} verified={fixtureVerified} error={guidedProjectionError} />}
      {!guided && <section className="paper-panel validation-action"><div><span className="section-kicker">Repository check</span><h2>{validation ? `Worker disposition: ${validation.status}` : "Run the authoritative readiness check"}</h2><p>{projectSession ? "The worker validates retained bytes, the active semantic revision, scenario completeness, authorizations, model objects, and numerical configuration." : "Import a verified bundle or create a planning record before running model validation."}</p>{validationMessage && <span className={`operation-message ${validationState === "error" ? "error" : "success"}`} role={validationState === "error" ? "alert" : "status"}>{validationMessage}</span>}</div><button className="primary-button" type="button" disabled={!projectSession || validationState === "checking" || validationState === "attesting"} onClick={() => void validate()}>{validationState === "checking" ? "Checking retained evidence…" : validation ? "Run check again" : "Run model check"}</button></section>}
      {!guided && projectSession && professionalAnalysis.projectState && (professionalAnalysis.projectState.effective_bundle.reference_bindings?.length ?? 0) > 0 && <ReferenceComparisonPanel projectHandle={projectSession.projectHandle} projectState={professionalAnalysis.projectState} onEvidence={onEvidence} />}
      <section className="paper-panel fitness-passport"><div className="passport-spine"><span>MODEL</span><strong>FITNESS</strong><small>{guided ? "RIDGEWAY · SYNTHETIC" : "PLANNING PROJECT · DRAFT"}</small></div><div className="passport-rows">{rows.map(([label, status, detail]) => <div className="fitness-row" key={label}><StatusPill state={status}>{status === "pass" ? "Supported" : status === "fail" ? "Blocking" : "Review"}</StatusPill><strong>{label}</strong><p>{detail}</p><span className="row-chevron" aria-hidden="true"><Icon name="chevron" /></span></div>)}</div></section>
      {!guided && fitness && <section className="paper-panel fitness-review" aria-labelledby="fitness-review-heading">
        <div className="panel-heading"><div><span className="section-kicker">Human review · active revision</span><h2 id="fitness-review-heading">Review every fitness dimension</h2></div><span className="mono">{fitness.revision_id}</span></div>
        <div className="fitness-review-boundary"><Icon name="shield" /><p><strong>An attestation records judgment; it cannot erase a blocker.</strong><span>Choose each disposition from retained project evidence. Evidence IDs must refer to records a reviewer actually inspected. The worker rejects a passport with a blocking dimension or a stale revision.</span></p></div>
        <div className="fitness-dimension-editors">{fitness.dimensions.map((dimension, index) => <fieldset key={dimension.key}><legend>{dimension.key.replaceAll("_", " ")}</legend><div className="field-grid two-up"><label><span>Reviewer disposition *</span><select value={dimension.status} onChange={(event) => updateDimension(index, "status", event.target.value)}><option value="BLOCKING">Blocking</option><option value="REVIEW_REQUIRED">Review required</option><option value="ACCEPTED">Accepted for stated use</option></select></label><label><span>Retained evidence IDs * · one per line</span><textarea rows={2} className="mono" value={(dimension.evidence_ids ?? []).join("\n")} onChange={(event) => updateDimension(index, "evidence_ids", event.target.value)} /></label><label className="field-span"><span>Dimension-specific rationale and limitation *</span><textarea rows={3} value={dimension.notes} onChange={(event) => updateDimension(index, "notes", event.target.value)} /></label></div></fieldset>)}</div>
        <div className="field-grid two-up"><label><span>Named reviewer *</span><input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><label className="field-span"><span>Unresolved assumptions · one per line</span><textarea rows={3} value={unresolvedAssumptions} onChange={(event) => setUnresolvedAssumptions(event.target.value)} /></label><label className="field-span"><span>Explicit attestation *</span><textarea rows={3} value={attestation} onChange={(event) => setAttestation(event.target.value)} placeholder="I reviewed this passport against the retained evidence for this active semantic revision and accept the listed review-required limitations for the stated screening use." /></label></div>
        <label className="confirmation-row"><input type="checkbox" checked={attestationConfirmed} onChange={(event) => setAttestationConfirmed(event.target.checked)} /><span><strong>I am recording a model-fitness review, not design, approval, movement, collision, population, or regulatory evidence.</strong> Any changed model object creates a new semantic revision that requires review again.</span></label>
        <div className="fitness-attest-action"><div>{!validation ? <p><Icon name="lock" /><strong>Run the authoritative model check first.</strong></p> : nonFitnessBlockers.length ? <p><Icon name="lock" /><strong>{nonFitnessBlockers.length} non-fitness blocker{nonFitnessBlockers.length === 1 ? "" : "s"} must be resolved before review.</strong></p> : blockingDimensions.length ? <p><Icon name="warning" /><strong>{blockingDimensions.length} blocking dimension{blockingDimensions.length === 1 ? "" : "s"} remain in this candidate passport.</strong></p> : incompleteDimensions.length ? <p><Icon name="warning" /><strong>{incompleteDimensions.length} dimension{incompleteDimensions.length === 1 ? "" : "s"} lack retained evidence IDs or rationale.</strong></p> : <p><Icon name="check" /><strong>Candidate passport is review-complete; the worker will validate it against the active revision before publication.</strong></p>}</div><button className="primary-button" type="button" disabled={!canAttest || validationState === "attesting"} onClick={() => void attest()}>{validationState === "attesting" ? "Recording attestation…" : "Attest active revision"}</button></div>
      </section>}
      <div className="fitness-footer"><div><strong>Suitable for this decision?</strong><span>{guided ? "Suitable for a synthetic robustness lesson under stated assumptions." : validation?.status === "READY" && !blockingDimensions.length ? "The retained worker check is READY for the stated screening use; review-required limitations remain visible." : validation?.status === "BLOCKED" ? "No. The retained worker check is BLOCKED; inspect the recorded findings above." : "No current worker disposition is retained. Run the authoritative model check."}</span></div><div><strong>Suitable for design or approval?</strong><span>No. This application does not generate designs or regulatory determinations.</span></div></div>
    </>
  );
}

function ScenariosScreen({ path, onEvidence, fixture, fixtureVerified, guidedProjectionError, projectSession, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "path" | "onEvidence" | "fixture" | "fixtureVerified" | "guidedProjectionError" | "projectSession" | "professionalRecords" | "onProfessionalRecords">) {
  const projection = path === "guided";
  const [scenarioId] = useState(() => stableUiId("scenario"));
  const [title, setTitle] = useState("");
  const [hypothesisId, setHypothesisId] = useState(professionalRecords.hypotheses[0]?.hypothesis_id ?? "");
  const [focalSetId, setFocalSetId] = useState(professionalRecords.focalAreaSets[0]?.focal_set_id ?? "");
  const [safeguard, setSafeguard] = useState(false);
  const [neighborRule, setNeighborRule] = useState<"FOUR" | "EIGHT">("EIGHT");
  const [resistanceAverage, setResistanceAverage] = useState<"ARITHMETIC" | "HARMONIC">("ARITHMETIC");
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const saveScenario = async () => {
    if (!projectSession) return;
    setState("saving");
    setMessage("");
    try {
      const hypothesis = professionalRecords.hypotheses.find((item) => item.hypothesis_id === hypothesisId);
      const focalSet = professionalRecords.focalAreaSets.find((item) => item.focal_set_id === focalSetId);
      if (!hypothesis || !focalSet) throw new Error("Select persisted reviewed hypothesis and focal-set records.");
      if (hypothesis.grid_id !== focalSet.grid_id) throw new Error("Hypothesis and focal-set grids differ. Approve an explicit alignment revision first.");
      if (hypothesis.guild_id !== focalSet.guild_id) throw new Error("Hypothesis and focal-set guild identities differ.");
      if (!title.trim()) throw new Error("Enter a scenario title.");
      const scenario = ConnectivityScenarioSchema.parse({ scenario_id: scenarioId, title: title.trim(), guild_id: hypothesis.guild_id, life_stage: hypothesis.life_stage, resistance_hypothesis_id: hypothesis.hypothesis_id, focal_set_id: focalSet.focal_set_id, future_condition: hypothesis.future_condition, maturity_horizon: hypothesis.maturity_horizon, included_pair_ids: focalSet.pairs.filter((item) => item.required !== false).map((item) => item.pair_id), safeguard, grid_id: hypothesis.grid_id, engine_mode: "RASTER_PAIRWISE", raster_semantics: "RESISTANCE", neighbor_rule: neighborRule, resistance_average: resistanceAverage, precision: "double", julia_threads: 1, blas_threads: 1 });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.scenario.save", { project_handle: projectSession.projectHandle, scenario });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      if (saved.record_id !== scenario.scenario_id) throw new Error("The worker returned a different scenario identity.");
      onProfessionalRecords({ ...professionalRecords, scenarios: [...professionalRecords.scenarios.filter((item) => item.scenario_id !== scenario.scenario_id), scenario] });
      setState("idle");
      setMessage(`Required scenario persisted at worker revision ${saved.revision}.`);
    } catch (error) {
      setState("error");
      setMessage(hostErrorMessage(error, "The scenario contract is incomplete or incompatible."));
    }
  };
  if (!projection) {
    return (
      <>
        <PageIntro kicker="Scenario ensemble" title="Define every comparison before running." description="Each scenario binds one reviewed guild or life stage, focal set, resistance hypothesis, future condition, maturity horizon, pair set, grid, and engine settings." badge={<StatusPill state={professionalRecords.scenarios.length ? "pass" : "fail"}>{professionalRecords.scenarios.length ? `${professionalRecords.scenarios.length} required` : "No approved scenarios"}</StatusPill>} />
        <div className="scenario-rule"><Icon name="info" /><p>Raw effective resistance is never aggregated across species, focal sets, grids, or incompatible hypotheses. Only baseline-relative changes within one complete scenario are comparable.</p></div>
        {professionalRecords.hypotheses.length && professionalRecords.focalAreaSets.length ? <div className="split-grid"><section className="paper-panel form-panel"><div className="form-section-heading"><span>Required scenario editor</span><h2>Bind reviewed model objects</h2></div><label><span>Scenario title *</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label><span>Resistance hypothesis *</span><select value={hypothesisId} onChange={(event) => setHypothesisId(event.target.value)}>{professionalRecords.hypotheses.map((item) => <option key={item.hypothesis_id} value={item.hypothesis_id}>{item.title}</option>)}</select></label><label><span>Focal set *</span><select value={focalSetId} onChange={(event) => setFocalSetId(event.target.value)}>{professionalRecords.focalAreaSets.map((item) => <option key={item.focal_set_id} value={item.focal_set_id}>{item.title}</option>)}</select></label><label className="checkbox-row"><input type="checkbox" checked={safeguard} onChange={(event) => setSafeguard(event.target.checked)} />This is a required safeguard scenario</label><label><span>Neighbor rule</span><select value={neighborRule} onChange={(event) => setNeighborRule(event.target.value as typeof neighborRule)}><option value="EIGHT">Eight-neighbor</option><option value="FOUR">Four-neighbor</option></select></label><label><span>Resistance averaging</span><select value={resistanceAverage} onChange={(event) => setResistanceAverage(event.target.value as typeof resistanceAverage)}><option value="ARITHMETIC">Arithmetic</option><option value="HARMONIC">Harmonic</option></select></label><button className="primary-button" type="button" disabled={!projectSession || state === "saving"} onClick={() => void saveScenario()}>{state === "saving" ? "Persisting scenario…" : "Save required scenario"}</button>{message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}<p className="control-requirement">v0.1 fixes raster pairwise mode, resistance semantics, double precision, one Julia thread, and one BLAS thread.</p></section><section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Authoritative ensemble</span><h2>Persisted required scenarios</h2></div></div><div className="table-scroll"><table><thead><tr><th>Scenario</th><th>Role</th><th>Hypothesis</th><th>Pairs</th></tr></thead><tbody>{professionalRecords.scenarios.length ? professionalRecords.scenarios.map((item) => <tr key={item.scenario_id}><td><strong>{item.title}</strong><span className="table-subline mono">{item.scenario_id}</span></td><td><StatusPill state={item.safeguard ? "review" : "info"}>{item.safeguard ? "Safeguard" : "Target"}</StatusPill></td><td className="mono">{item.resistance_hypothesis_id}</td><td>{item.included_pair_ids.length}</td></tr>) : <tr><td colSpan={4}>No scenario record has been persisted.</td></tr>}</tbody></table></div></section></div> : <section className="paper-panel blocked-workspace"><Icon name="lock" /><div><span className="section-kicker">Scenario authoring unavailable</span><h2>No fixture scenarios have been copied into this project.</h2><p>Persist an approved resistance hypothesis and focal set first. Their grid and guild identities must match before a scenario can be saved.</p><button className="secondary-button" type="button" disabled>Save reviewed scenario</button></div></section>}
      </>
    );
  }
  if (!fixture) return <><PageIntro kicker="Scenario ensemble" title="Ecological scenarios" description="Scenario definitions and results are read from the authoritative guided projection." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  const examplePortfolio = fixture.portfolios.find((item) => item.role === "carry-forward" && item.id);
  return (
    <>
      <PageIntro kicker="Scenario ensemble" title="Compare within a scenario, then test across all of them." description="Each scenario binds one guild or life stage, focal set, resistance hypothesis, future condition, maturity horizon, pair set, grid, and engine settings." badge={<StatusPill state={projection ? "pass" : "fail"}>{projection ? "12 required scenarios" : "Ensemble incomplete"}</StatusPill>} />
      <div className="scenario-rule"><Icon name="info" /><p>Raw effective resistance is never aggregated across species, focal sets, grids, or incompatible hypotheses. The board compares baseline-relative changes within each scenario.</p></div>
      <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Required ensemble</span><h2>Targets and safeguards</h2></div><span className="mono">{fixture.scenarios.length} / {fixture.study.scenarioCount} defined</span></div><div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable guided ecological scenario evidence"><table><thead><tr><th>Scenario</th><th>Purpose</th><th>Resistance hypothesis</th><th>Condition</th><th>Maturity</th><th>Pairs</th><th>Evidence</th></tr></thead><tbody>{fixture.scenarios.map((scenario) => { const result = examplePortfolio?.scenarioResults.find((item) => item.scenarioId === scenario.id); return <tr key={scenario.id}><td className="mono">{scenario.id}</td><td><StatusPill state={scenario.purpose === "safeguard" ? "review" : "info"}>{scenario.guild}</StatusPill></td><td>{scenario.hypothesis}</td><td>{scenario.futureCondition}</td><td>{scenario.maturity}</td><td>{scenario.includedPairs}</td><td>{examplePortfolio && result && fixtureVerified ? <button className="table-button" type="button" onClick={() => onEvidence(evidenceFor(fixture, examplePortfolio, true, result))}>Trace direct result</button> : <span className="mono">Not published</span>}</td></tr>; })}</tbody></table></div></section>
    </>
  );
}

const interventionRows = [
  ["S01", "No action / survey first", "No connectivity mutation", "Ownership unknown"],
  ["S02", "Crossing + complete guidance", "Reviewed composite footprint", "Land access unresolved"],
  ["S03", "Approach restoration", "Scenario-specific replacement cells", "Invasive-species review"],
  ["S04", "No action", "No connectivity mutation", "No open concern"],
  ["S05", "Crossing concept", "Complete footprint required", "Traffic + structure review"],
  ["S06", "Approach restoration", "Maturity-specific effect", "Maintenance owner"],
  ["S07", "Survey first", "Shares no-action physics", "Cultural review"],
  ["S08", "No action", "No connectivity mutation", "Private-land boundary"],
];

function InterventionsScreen({ path, planning, onPlanning, projectSession, professionalAssets, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "path" | "planning" | "onPlanning" | "projectSession" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords">) {
  const guided = path === "guided";
  const [siteId] = useState(() => stableUiId("site"));
  const [baselineOptionId] = useState(() => stableUiId("option_no_action"));
  const [surveyOptionId] = useState(() => stableUiId("option_survey_first"));
  const [siteTitle, setSiteTitle] = useState("");
  const [footprintAssetId, setFootprintAssetId] = useState(professionalAssets.find((item) => item.grid)?.assetId ?? "");
  const [includeSurveyFirst, setIncludeSurveyFirst] = useState(true);
  const [surveyTitle, setSurveyTitle] = useState("Survey first");
  const [surveyCost, setSurveyCost] = useState("0");
  const [currency, setCurrency] = useState("USD");
  const [costVintage, setCostVintage] = useState("");
  const [landAccess, setLandAccess] = useState("");
  const [maintenance, setMaintenance] = useState("");
  const [knownConstraints, setKnownConstraints] = useState("");
  const [unresolvedConcerns, setUnresolvedConcerns] = useState("");
  const [externalEvidence, setExternalEvidence] = useState("");
  const [reviewedAt, setReviewedAt] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const save = async () => {
    if (!projectSession) return;
    setState("saving"); setMessage("");
    try {
      if ([siteTitle, footprintAssetId, landAccess, maintenance, planning.reviewer, costVintage].some((item) => !item.trim()) || !reviewedAt) throw new Error("Complete site, footprint, land access, maintenance, cost vintage, reviewer, and review time.");
      const reviewed = new Date(reviewedAt).toISOString();
      const noAction = InterventionOptionSchema.parse({ option_id: baselineOptionId, site_id: siteId, title: "No action", kind: "NO_ACTION", effects: [], cost: 0, currency, cost_vintage: costVintage.trim(), dependencies: [], exclusions: [], land_access: landAccess.trim(), maintenance: maintenance.trim(), external_feasibility_evidence: externalEvidence.split("\n").map((item) => item.trim()).filter(Boolean), unresolved_concerns: unresolvedConcerns.split("\n").map((item) => item.trim()).filter(Boolean), reviewed_by: planning.reviewer.trim(), reviewed_at: reviewed });
      const options = [noAction];
      if (includeSurveyFirst) options.push(InterventionOptionSchema.parse({ option_id: surveyOptionId, site_id: siteId, title: surveyTitle.trim(), kind: "SURVEY_FIRST", effects: [], cost: Number(surveyCost), currency: currency.trim(), cost_vintage: costVintage.trim(), dependencies: ["Complete the documented field investigation before authoring an action effect."], exclusions: ["No modeled connectivity mutation"], land_access: landAccess.trim(), maintenance: maintenance.trim(), external_feasibility_evidence: externalEvidence.split("\n").map((item) => item.trim()).filter(Boolean), unresolved_concerns: unresolvedConcerns.split("\n").map((item) => item.trim()).filter(Boolean), reviewed_by: planning.reviewer.trim(), reviewed_at: reviewed }));
      const site = InterventionSiteSchema.parse({ site_id: siteId, title: siteTitle.trim(), footprint_asset_id: footprintAssetId, option_ids: options.map((item) => item.option_id), known_constraints: knownConstraints.split("\n").map((item) => item.trim()).filter(Boolean), reviewed_by: planning.reviewer.trim(), reviewed_at: reviewed });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.intervention.save", { project_handle: projectSession.projectHandle, site, options });
      const saved = InterventionSaveResponseSchema.parse(rpcData(response));
      if (saved.site_id !== site.site_id || saved.option_ids.join("|") !== site.option_ids.join("|")) throw new Error("The worker returned different site or option identities.");
      onProfessionalRecords({ ...professionalRecords, interventionSites: [...professionalRecords.interventionSites.filter((item) => item.site_id !== site.site_id), site], interventionOptions: [...professionalRecords.interventionOptions.filter((item) => item.site_id !== site.site_id), ...options] });
      setState("idle"); setMessage("No-action and Survey-first choices were persisted without any connectivity mutation.");
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The reviewed intervention site is incomplete.")); }
  };
  if (!guided) {
    return (
      <>
        <PageIntro kicker="Bounded portfolio space" title="Only reviewed options enter the search." description="No Ridgeway option or action effect is copied into a professional project. Each site option is authored from project evidence." badge={<StatusPill state={professionalRecords.interventionSites.length ? "review" : "fail"}>{professionalRecords.interventionSites.length ? `${professionalRecords.interventionSites.length} reviewed site${professionalRecords.interventionSites.length === 1 ? "" : "s"}` : "No reviewed options"}</StatusPill>} />
        <div className="split-grid"><section className="paper-panel form-panel"><div className="form-section-heading"><span>Safe site editor</span><h2>No action and Survey first</h2></div><p>This editor creates feasibility choices that share unchanged connectivity physics. It cannot disguise an unknown as an action effect.</p><label><span>Site title *</span><input value={siteTitle} onChange={(event) => setSiteTitle(event.target.value)} /></label><label><span>Complete reviewed footprint asset *</span><select value={footprintAssetId} onChange={(event) => setFootprintAssetId(event.target.value)}><option value="">Choose persisted asset…</option>{professionalAssets.filter((item) => item.grid).map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title}</option>)}</select></label><label className="checkbox-row"><input type="checkbox" checked={includeSurveyFirst} onChange={(event) => setIncludeSurveyFirst(event.target.checked)} />Include a mutually exclusive Survey-first option beside No action</label>{includeSurveyFirst && <><label><span>Survey-first title *</span><input value={surveyTitle} onChange={(event) => setSurveyTitle(event.target.value)} /></label><label><span>Survey screening cost</span><input inputMode="decimal" value={surveyCost} onChange={(event) => setSurveyCost(event.target.value)} /></label></>}<div className="field-grid two-up"><label><span>Currency *</span><input value={currency} onChange={(event) => setCurrency(event.target.value)} /></label><label><span>Cost vintage *</span><input value={costVintage} onChange={(event) => setCostVintage(event.target.value)} /></label></div><label><span>Land access status *</span><textarea rows={2} value={landAccess} onChange={(event) => setLandAccess(event.target.value)} /></label><label><span>Maintenance status *</span><textarea rows={2} value={maintenance} onChange={(event) => setMaintenance(event.target.value)} /></label><label><span>Known constraints · one per line</span><textarea rows={3} value={knownConstraints} onChange={(event) => setKnownConstraints(event.target.value)} /></label><label><span>Unresolved concerns · one per line</span><textarea rows={3} value={unresolvedConcerns} onChange={(event) => setUnresolvedConcerns(event.target.value)} /></label><label><span>External feasibility evidence · one citation per line</span><textarea rows={3} value={externalEvidence} onChange={(event) => setExternalEvidence(event.target.value)} /></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><button className="primary-button" type="button" disabled={!projectSession || !professionalAssets.some((item) => item.grid) || state === "saving"} onClick={() => void save()}>{state === "saving" ? "Persisting site catalog…" : "Save reviewed site choices"}</button>{message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}</section><section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Persisted catalog</span><h2>Mutually exclusive choices</h2></div></div><div className="table-scroll"><table><thead><tr><th>Site</th><th>Options</th><th>Physics</th></tr></thead><tbody>{professionalRecords.interventionSites.length ? professionalRecords.interventionSites.map((site) => { const options = professionalRecords.interventionOptions.filter((item) => item.site_id === site.site_id); const modeled = options.some((item) => !["NO_ACTION", "SURVEY_FIRST"].includes(item.kind)); return <tr key={site.site_id}><td><strong>{site.title}</strong><span className="table-subline mono">{site.site_id}</span></td><td>{options.map((item) => item.title).join(" · ")}</td><td>{modeled ? "Worker-canonical scenario effects" : "No action / survey first reuse unchanged direct results"}</td></tr>; }) : <tr><td colSpan={3}>No site catalog has been persisted.</td></tr>}</tbody></table></div></section></div>
        <ModeledInterventionEditor projectSession={projectSession} planning={planning} professionalAssets={professionalAssets} professionalRecords={professionalRecords} onProfessionalRecords={onProfessionalRecords} />
        <div className="assumption-callout"><Icon name="layers" /><p><strong>Mutation rule:</strong> reviewed absolute replacement cells apply only inside the complete authored footprint. Separate options may not overlap, change grid topology, or alter NODATA semantics.</p></div>
      </>
    );
  }
  return (
    <>
      <PageIntro kicker="Bounded portfolio space" title="Only reviewed options enter the search." description="Each stable site option carries its complete footprint, scenario-specific effect, evidence basis, cost, dependencies, and feasibility record." badge={<StatusPill state={guided ? "pass" : "fail"}>{guided ? "8 sites · reviewed fixture" : "Action basis missing"}</StatusPill>} />
      <CorridorTransect compact />
      <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Intervention catalog</span><h2>Mutually exclusive site options</h2></div><span className="mono">Overlap requires one composite</span></div><div className="table-scroll"><table><thead><tr><th>Site</th><th>Reviewed option</th><th>Model effect</th><th>Feasibility gate</th></tr></thead><tbody>{interventionRows.map(([site, option, effect, concern]) => <tr key={site}><td className="mono">{site}</td><td><strong>{option}</strong></td><td>{effect}</td><td><StatusPill state={concern === "No open concern" ? "pass" : "review"}>{concern}</StatusPill></td></tr>)}</tbody></table></div></section>
      <div className="assumption-callout"><Icon name="layers" /><p><strong>Mutation rule:</strong> reviewed absolute replacement cells apply only inside the complete authored footprint. Separate options may not overlap, change grid topology, or alter NODATA semantics.</p></div>
    </>
  );
}

type InterventionPreview = ReturnType<typeof InterventionPreviewResponseSchema.parse>;

function parseActionCells(value: string, scenarioTitle: string) {
  const lines = value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!lines.length) throw new Error(`${scenarioTitle} needs at least one reviewed replacement cell.`);
  return lines.map((line, index) => {
    const fields = line.split(",").map((item) => item.trim());
    if (fields.length !== 3) throw new Error(`${scenarioTitle}, line ${index + 1}: use row,column,resistance.`);
    const row = Number(fields[0]);
    const column = Number(fields[1]);
    const resistance = Number(fields[2]);
    if (!Number.isInteger(row) || row < 0 || !Number.isInteger(column) || column < 0 || !Number.isFinite(resistance) || resistance <= 0) throw new Error(`${scenarioTitle}, line ${index + 1}: row and column must be nonnegative integers and resistance must be positive.`);
    return { row, column, resistance };
  });
}

function ModeledInterventionEditor({ projectSession, planning, professionalAssets, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "projectSession" | "planning" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords">) {
  const [siteId] = useState(() => stableUiId("site_modeled"));
  const [noActionId] = useState(() => stableUiId("option_no_action"));
  const [actionOptionId] = useState(() => stableUiId("option_action"));
  const [siteTitle, setSiteTitle] = useState("");
  const [actionTitle, setActionTitle] = useState("");
  const [kind, setKind] = useState<"CROSSING_APPROACH" | "CROSSING_COMPLETE_GUIDANCE" | "APPROACH_RESTORATION" | "COMPOSITE">("CROSSING_COMPLETE_GUIDANCE");
  const [footprintAssetId, setFootprintAssetId] = useState(professionalAssets.find((item) => item.grid)?.assetId ?? "");
  const [cost, setCost] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [costVintage, setCostVintage] = useState("");
  const [purpose, setPurpose] = useState("");
  const [evidenceSource, setEvidenceSource] = useState("");
  const [citation, setCitation] = useState("");
  const [confidence, setConfidence] = useState<"CONFIRMED" | "INFERRED" | "ASSUMED">("CONFIRMED");
  const [assumptions, setAssumptions] = useState("");
  const [limitations, setLimitations] = useState("");
  const [dependencies, setDependencies] = useState("");
  const [exclusions, setExclusions] = useState("");
  const [knownConstraints, setKnownConstraints] = useState("");
  const [landAccess, setLandAccess] = useState("");
  const [maintenance, setMaintenance] = useState("");
  const [externalEvidence, setExternalEvidence] = useState("");
  const [unresolvedConcerns, setUnresolvedConcerns] = useState("");
  const [reviewedAt, setReviewedAt] = useState("");
  const [effectCells, setEffectCells] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<InterventionPreview | null>(null);
  const [previewSignature, setPreviewSignature] = useState("");
  const [state, setState] = useState<"idle" | "previewing" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");

  const signature = JSON.stringify({ siteTitle, actionTitle, kind, footprintAssetId, cost, currency, costVintage, purpose, evidenceSource, citation, confidence, assumptions, limitations, dependencies, exclusions, knownConstraints, landAccess, maintenance, externalEvidence, unresolvedConcerns, reviewedAt, effectCells, reviewer: planning.reviewer, scenarioIds: professionalRecords.scenarios.map((item) => item.scenario_id) });
  const previewCurrent = preview !== null && previewSignature === signature;

  const createDraft = () => {
    const footprint = professionalAssets.find((item) => item.assetId === footprintAssetId);
    if (!footprint?.grid) throw new Error("Select a retained, grid-identified complete footprint asset.");
    if (!professionalRecords.scenarios.length) throw new Error("Persist the complete target and safeguard scenario ensemble first.");
    if ([siteTitle, actionTitle, costVintage, purpose, evidenceSource, citation, landAccess, maintenance, planning.reviewer].some((item) => !item.trim()) || !reviewedAt) throw new Error("Complete identity, evidence basis, feasibility, reviewer, and review time.");
    const actionCost = Number(cost);
    if (!Number.isFinite(actionCost) || actionCost < 0) throw new Error("Screening cost must be a nonnegative number.");
    const reviewed = new Date(reviewedAt).toISOString();
    const lineList = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);
    const shared = { cost_vintage: costVintage.trim(), currency: currency.trim(), land_access: landAccess.trim(), maintenance: maintenance.trim(), external_feasibility_evidence: lineList(externalEvidence), unresolved_concerns: lineList(unresolvedConcerns), reviewed_by: planning.reviewer.trim(), reviewed_at: reviewed };
    const noAction = { option_id: noActionId, site_id: siteId, title: "No action", kind: "NO_ACTION", effects: [], cost: 0, dependencies: [], exclusions: [], ...shared };
    const effects = professionalRecords.scenarios.map((scenario, index) => ({
      effect_id: `${actionOptionId}_effect_${index + 1}`,
      scenario_id: scenario.scenario_id,
      grid_id: scenario.grid_id,
      footprint_asset_id: footprint.assetId,
      cells: parseActionCells(effectCells[scenario.scenario_id] ?? "", scenario.title),
      basis: { basis_id: `${actionOptionId}_basis_${index + 1}`, purpose: purpose.trim(), evidence_source: evidenceSource.trim(), citation: citation.trim(), reviewer: planning.reviewer.trim(), reviewed_at: reviewed, confidence, assumptions: lineList(assumptions), limitations: lineList(limitations) },
    }));
    const action = { option_id: actionOptionId, site_id: siteId, title: actionTitle.trim(), kind, effects, cost: actionCost, dependencies: lineList(dependencies), exclusions: lineList(exclusions), ...shared };
    const site = InterventionSiteSchema.parse({ site_id: siteId, title: siteTitle.trim(), footprint_asset_id: footprint.assetId, option_ids: [noActionId, actionOptionId], known_constraints: lineList(knownConstraints), reviewed_by: planning.reviewer.trim(), reviewed_at: reviewed });
    return { site, options: [noAction, action] };
  };

  const requestPreview = async () => {
    if (!projectSession) return;
    setState("previewing"); setMessage("");
    try {
      const draft = createDraft();
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.intervention.preview", { project_handle: projectSession.projectHandle, ...draft });
      const workerPreview = InterventionPreviewResponseSchema.parse(rpcData(response));
      if (workerPreview.site.site_id !== siteId || workerPreview.options.map((item) => item.option_id).sort().join("|") !== [noActionId, actionOptionId].sort().join("|")) throw new Error("The worker returned a different intervention catalog identity.");
      setPreview(workerPreview); setPreviewSignature(signature); setState("idle");
      setMessage(workerPreview.can_save ? `Worker canonicalized ${workerPreview.effect_hashes.length} scenario effect${workerPreview.effect_hashes.length === 1 ? "" : "s"} for active revision ${workerPreview.revision_id}.` : `Worker preview is blocked by ${workerPreview.findings.filter((item) => item.severity === "BLOCKING").length} finding(s).`);
    } catch (error) { setPreview(null); setPreviewSignature(""); setState("error"); setMessage(hostErrorMessage(error, "The worker could not canonicalize this modeled intervention.")); }
  };

  const savePreview = async () => {
    if (!projectSession || !preview || !previewCurrent || !preview.can_save) return;
    setState("saving"); setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.intervention.save", { project_handle: projectSession.projectHandle, site: preview.site, options: preview.options, preview_sha256: preview.preview_sha256 });
      const receipt = InterventionSaveResponseSchema.parse(rpcData(response));
      if (receipt.site_id !== preview.site.site_id || receipt.option_ids.slice().sort().join("|") !== preview.site.option_ids.slice().sort().join("|")) throw new Error("The worker returned a different saved catalog identity.");
      onProfessionalRecords({ ...professionalRecords, interventionSites: [...professionalRecords.interventionSites.filter((item) => item.site_id !== preview.site.site_id), preview.site], interventionOptions: [...professionalRecords.interventionOptions.filter((item) => item.site_id !== preview.site.site_id), ...preview.options] });
      setState("idle"); setMessage("The exact worker-canonical preview was persisted. Later edits require a new active-revision preview.");
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The worker rejected this modeled intervention preview.")); }
  };

  return <section className="paper-panel modeled-intervention" aria-labelledby="modeled-intervention-heading">
    <div className="panel-heading"><div><span className="section-kicker">Modeled action · worker-canonical preview</span><h2 id="modeled-intervention-heading">Author reviewed absolute replacements</h2></div><StatusPill state={previewCurrent && preview?.can_save ? "pass" : "review"}>{previewCurrent && preview?.can_save ? "Preview eligible" : "Not persisted"}</StatusPill></div>
    <div className="modeled-intervention-boundary"><Icon name="warning" /><p><strong>The interface does not infer a resistance effect.</strong><span>Enter complete scenario-specific absolute replacement cells from reviewed evidence. The worker checks grids, NODATA, footprint containment, scenario coverage, overlap, active revision, and computes every immutable hash.</span></p></div>
    <div className="modeled-intervention-fields"><fieldset><legend>Site and feasibility</legend><div className="field-grid two-up"><label><span>Site title *</span><input value={siteTitle} onChange={(event) => setSiteTitle(event.target.value)} /></label><label><span>Complete footprint asset *</span><select value={footprintAssetId} onChange={(event) => setFootprintAssetId(event.target.value)}><option value="">Choose retained grid asset…</option>{professionalAssets.filter((item) => item.grid).map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.grid?.grid_id}</option>)}</select></label><label><span>Modeled option title *</span><input value={actionTitle} onChange={(event) => setActionTitle(event.target.value)} /></label><label><span>Reviewed option kind *</span><select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="CROSSING_APPROACH">Crossing and approach concept</option><option value="CROSSING_COMPLETE_GUIDANCE">Crossing with complete guidance and end treatment</option><option value="APPROACH_RESTORATION">Approach restoration</option><option value="COMPOSITE">Explicit reviewed composite</option></select></label><label><span>Screening cost *</span><input inputMode="decimal" value={cost} onChange={(event) => setCost(event.target.value)} /></label><label><span>Currency / vintage *</span><span className="inline-pair"><input aria-label="Modeled action currency" value={currency} onChange={(event) => setCurrency(event.target.value)} /><input aria-label="Modeled action cost vintage" value={costVintage} onChange={(event) => setCostVintage(event.target.value)} /></span></label><label><span>Land access *</span><textarea rows={2} value={landAccess} onChange={(event) => setLandAccess(event.target.value)} /></label><label><span>Maintenance responsibility *</span><textarea rows={2} value={maintenance} onChange={(event) => setMaintenance(event.target.value)} /></label><label><span>Known constraints · one per line</span><textarea rows={3} value={knownConstraints} onChange={(event) => setKnownConstraints(event.target.value)} /></label><label><span>Unresolved concerns · one per line</span><textarea rows={3} value={unresolvedConcerns} onChange={(event) => setUnresolvedConcerns(event.target.value)} /></label><label><span>Dependencies · one per line</span><textarea rows={3} value={dependencies} onChange={(event) => setDependencies(event.target.value)} /></label><label><span>Exclusions · one per line</span><textarea rows={3} value={exclusions} onChange={(event) => setExclusions(event.target.value)} /></label></div></fieldset>
      <fieldset><legend>Reviewed action-effect basis</legend><div className="field-grid two-up"><label className="field-span"><span>Modeled purpose *</span><textarea rows={3} value={purpose} onChange={(event) => setPurpose(event.target.value)} /></label><label><span>Evidence source *</span><input value={evidenceSource} onChange={(event) => setEvidenceSource(event.target.value)} /></label><label><span>Exact citation *</span><input value={citation} onChange={(event) => setCitation(event.target.value)} /></label><label><span>Confidence *</span><select value={confidence} onChange={(event) => setConfidence(event.target.value as typeof confidence)}><option value="CONFIRMED">Confirmed</option><option value="INFERRED">Inferred</option><option value="ASSUMED">Assumed</option></select></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><label><span>Assumptions · one per line</span><textarea rows={3} value={assumptions} onChange={(event) => setAssumptions(event.target.value)} /></label><label><span>Limitations · one per line</span><textarea rows={3} value={limitations} onChange={(event) => setLimitations(event.target.value)} /></label><label><span>External feasibility citations · one per line</span><textarea rows={3} value={externalEvidence} onChange={(event) => setExternalEvidence(event.target.value)} /></label></div></fieldset>
      <fieldset className="scenario-effect-fieldset"><legend>Complete scenario effect ensemble</legend><p>One cell per line: <span className="mono">row,column,absolute resistance</span>. These are replacement values, not deltas or predictions.</p>{professionalRecords.scenarios.length ? professionalRecords.scenarios.map((scenario) => <label key={scenario.scenario_id}><span>{scenario.title} <small className="mono">{scenario.scenario_id} · {scenario.grid_id}</small></span><textarea rows={4} className="mono" value={effectCells[scenario.scenario_id] ?? ""} onChange={(event) => setEffectCells((current) => ({ ...current, [scenario.scenario_id]: event.target.value }))} placeholder="12,44,18.5\n12,45,18.5" /></label>) : <div className="blocked-control-note"><Icon name="lock" /><p><strong>No scenario ensemble exists.</strong><span>Persist every target and safeguard scenario before authoring a modeled effect.</span></p></div>}</fieldset>
    </div>
    {preview && <div className="intervention-preview-ledger"><div><span>Preview digest</span><strong className="mono">{preview.preview_sha256}</strong></div><div><span>Canonical effects</span><strong>{preview.effect_hashes.length}</strong></div><div><span>Semantic additions</span><strong>{preview.semantic_diff.added_option_ids.length}</strong></div><div><span>Disposition</span><strong>{preview.can_save ? "Eligible to persist" : "Blocked"}</strong></div></div>}
    {preview?.findings.length ? <div className="preview-findings"><h3>Worker findings</h3><ul>{preview.findings.map((finding) => <li key={`${finding.code}:${finding.object_reference}`}><StatusPill state={finding.severity === "BLOCKING" ? "fail" : finding.severity === "REVIEW_REQUIRED" ? "review" : "info"}>{finding.severity.replaceAll("_", " ")}</StatusPill><span><strong>{finding.code}</strong>{finding.remediation}</span></li>)}</ul></div> : null}
    {message && <p className={`operation-message ${state === "error" || (preview && !preview.can_save) ? "error" : "success"}`} role={state === "error" || (preview && !preview.can_save) ? "alert" : "status"}>{message}</p>}
    <div className="modeled-intervention-actions"><button className="secondary-button" type="button" disabled={!projectSession || !professionalRecords.scenarios.length || state === "previewing" || state === "saving"} onClick={() => void requestPreview()}>{state === "previewing" ? "Checking every scenario…" : previewCurrent ? "Preview again" : "Preview exact mutation"}</button><button className="primary-button" type="button" disabled={!previewCurrent || !preview?.can_save || state === "saving"} onClick={() => void savePreview()}>{state === "saving" ? "Persisting canonical effects…" : "Persist reviewed preview"}</button></div>
  </section>;
}

function SafeguardsScreen({ path, planning, onPlanning, projectSession, professionalAssets, professionalRecords, onProfessionalRecords }: Pick<ScreensProps, "path" | "planning" | "onPlanning" | "projectSession" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords">) {
  const guided = path === "guided";
  const concerns = ["Target + safeguard ensemble complete", "Invasive and disease pathways reviewed", "Predator and mortality effects reviewed", "Construction-habitat timing reviewed", "Cultural and Tribal information authorization", "Ownership and land access", "Traffic, structural, permitting, and maintenance"];
  const safeguardScenarios = professionalRecords.scenarios.filter((item) => item.safeguard);
  const safeguardGuildIds = [...new Set(safeguardScenarios.map((item) => item.guild_id))].sort();
  const retainedSafeguardGuildIds = new Set(professionalRecords.safeguards.map((item) => item.guild_id));
  const uncoveredSafeguardGuildIds = safeguardGuildIds.filter((item) => !retainedSafeguardGuildIds.has(item));
  const maskAssets = professionalAssets.filter((item) => item.kind === "REVIEW_MASK" && item.grid?.value_kind === "MASK");
  const [safeguardId, setSafeguardId] = useState(() => stableUiId("safeguard"));
  const [guildId, setGuildId] = useState("");
  const [threshold, setThreshold] = useState("0");
  const [reviewedAt, setReviewedAt] = useState("");
  const [maskId] = useState(() => stableUiId("review_mask"));
  const [maskAssetId, setMaskAssetId] = useState(maskAssets[0]?.assetId ?? "");
  const [maskTitle, setMaskTitle] = useState("");
  const [maskThreshold, setMaskThreshold] = useState("");
  const [maskInterpretation, setMaskInterpretation] = useState("");
  const [state, setState] = useState<"idle" | "saving-safeguard" | "saving-mask" | "error">("idle");
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!guildId && safeguardGuildIds.length) chooseSafeguardGuild(safeguardGuildIds[0]);
    if (guildId && !safeguardGuildIds.includes(guildId)) chooseSafeguardGuild(safeguardGuildIds[0] ?? "");
    // Guild changes are also driven by the explicit selector below. This effect
    // only repairs an empty/stale selection after authoritative records reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [guildId, safeguardGuildIds.join("\u0000")]);
  const selectedSafeguardScenarios = safeguardScenarios.filter((item) => item.guild_id === guildId);
  const chooseSafeguardGuild = (nextGuildId: string) => {
    const retained = professionalRecords.safeguards.find((item) => item.guild_id === nextGuildId);
    setGuildId(nextGuildId);
    setSafeguardId(retained?.safeguard_id ?? stableUiId("safeguard"));
    setThreshold(String(retained?.nonregression_threshold_percent ?? 0));
    setReviewedAt(dateTimeLocalValue(retained?.reviewed_at));
    onPlanning({ ...planning, safeguardNotes: (retained?.concerns ?? []).join("\n") });
    setMessage("");
  };
  const saveSafeguard = async () => {
    if (!projectSession) return;
    setState("saving-safeguard"); setMessage("");
    try {
      if (!guildId.trim() || !reviewedAt || !planning.reviewer.trim() || !planning.safeguardNotes.trim()) throw new Error("Complete guild, concerns, reviewer, and review time.");
      if (!selectedSafeguardScenarios.length) throw new Error("Select a guild with at least one persisted safeguard scenario.");
      const record = SafeguardRecordSchema.parse({ safeguard_id: safeguardId, guild_id: guildId.trim(), scenario_ids: selectedSafeguardScenarios.map((item) => item.scenario_id), nonregression_threshold_percent: Number(threshold), concerns: planning.safeguardNotes.split("\n").map((item) => item.trim()).filter(Boolean), reviewed_by: planning.reviewer.trim(), reviewed_at: new Date(reviewedAt).toISOString() });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.safeguard.save", { project_handle: projectSession.projectHandle, safeguard: record });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      if (saved.record_id !== record.safeguard_id) throw new Error("The worker returned a different safeguard identity.");
      onProfessionalRecords({ ...professionalRecords, safeguards: [...professionalRecords.safeguards.filter((item) => item.safeguard_id !== record.safeguard_id && item.guild_id !== record.guild_id), record] });
      setState("idle"); setMessage(`${record.guild_id}: ${record.scenario_ids.length} safeguard scenario${record.scenario_ids.length === 1 ? "" : "s"} persisted at worker revision ${saved.revision}.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The safeguard record is incomplete.")); }
  };
  const saveMask = async () => {
    if (!projectSession) return;
    setState("saving-mask"); setMessage("");
    try {
      const asset = maskAssets.find((item) => item.assetId === maskAssetId);
      if (!asset?.grid) throw new Error("Select a persisted MASK raster.");
      if (!maskTitle.trim() || !maskInterpretation.trim()) throw new Error("Complete the mask title and diagnostic interpretation.");
      const reviewMask = RiskReviewMaskSchema.parse({ mask_id: maskId, title: maskTitle.trim(), asset_id: asset.assetId, grid_id: asset.grid.grid_id, review_trigger_percent: Number(maskThreshold), interpretation: maskInterpretation.trim(), not_an_objective_notice: "Current redistribution is a diagnostic review signal, not predicted animal use." });
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.review_mask.save", { project_handle: projectSession.projectHandle, review_mask: reviewMask });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      if (saved.record_id !== reviewMask.mask_id) throw new Error("The worker returned a different review-mask identity.");
      onProfessionalRecords({ ...professionalRecords, reviewMasks: [...professionalRecords.reviewMasks.filter((item) => item.mask_id !== reviewMask.mask_id), reviewMask] });
      setState("idle"); setMessage(`Diagnostic review mask persisted at worker revision ${saved.revision}.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The review-mask record is incomplete.")); }
  };
  if (!guided) {
    return (
      <>
        <PageIntro kicker="Environmental safety" title="A headline modeled resistance reduction cannot erase a safeguard failure." description="Critical unknowns resolve to Survey first. They do not disappear into a composite score." badge={<StatusPill state={safeguardGuildIds.length > 0 && uncoveredSafeguardGuildIds.length === 0 ? "pass" : "fail"}>{safeguardGuildIds.length ? `${safeguardGuildIds.length - uncoveredSafeguardGuildIds.length} / ${safeguardGuildIds.length} guilds covered` : "Safeguard scenarios required"}</StatusPill>} />
        <div className="safeguard-layout">
          <section className="paper-panel safeguard-matrix"><div className="panel-heading"><div><span className="section-kicker">Required safeguard ensemble</span><h2>{safeguardScenarios.length ? `${safeguardScenarios.length} scenarios across ${safeguardGuildIds.length} guild${safeguardGuildIds.length === 1 ? "" : "s"}` : "No safeguard scenario exists"}</h2></div></div>{safeguardScenarios.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="Required safeguard scenario coverage by guild"><table><thead><tr><th>Guild</th><th>Scenario</th><th>Hypothesis</th><th>Review record</th></tr></thead><tbody>{safeguardScenarios.map((scenario) => <tr key={scenario.scenario_id}><td className="mono">{scenario.guild_id}</td><td><strong>{scenario.title}</strong><span className="table-subline mono">{scenario.scenario_id}</span></td><td className="mono">{scenario.resistance_hypothesis_id}</td><td><StatusPill state={retainedSafeguardGuildIds.has(scenario.guild_id) ? "pass" : "review"}>{retainedSafeguardGuildIds.has(scenario.guild_id) ? "Recorded" : "Survey first"}</StatusPill></td></tr>)}</tbody></table></div> : <div className="blocked-control-note"><Icon name="lock" /><p><strong>No safeguard scenario is retained.</strong><span>Mark the required target-and-safeguard scenarios before authoring a review record.</span></p></div>}{concerns.map((concern, index) => <div className="safeguard-row" key={concern}><span>{String(index + 1).padStart(2, "0")}</span><strong>{concern}</strong><StatusPill state={index === 0 && retainedSafeguardGuildIds.size > 0 ? "pass" : "review"}>{index === 0 && retainedSafeguardGuildIds.size > 0 ? "Recorded by guild" : "Survey first"}</StatusPill></div>)}<div className="safeguard-editor"><label><span>Safeguard guild *</span><select value={guildId} onChange={(event) => chooseSafeguardGuild(event.target.value)}><option value="">Choose persisted safeguard guild…</option>{safeguardGuildIds.map((item) => <option key={item} value={item}>{item} · {safeguardScenarios.filter((scenario) => scenario.guild_id === item).length} scenario{safeguardScenarios.filter((scenario) => scenario.guild_id === item).length === 1 ? "" : "s"}</option>)}</select></label><label><span>Allowed resistance increase (%) *</span><input inputMode="decimal" value={threshold} onChange={(event) => setThreshold(event.target.value)} /></label>{guildId && <div className="field-span scenario-scope-note"><Icon name="shield" /><p><strong>This review applies only to {guildId}.</strong><span>{selectedSafeguardScenarios.map((scenario) => scenario.title).join(" · ") || "No scenarios selected"}</span></p></div>}<label><span>Concerns and unresolved checks * · one per line</span><textarea rows={5} value={planning.safeguardNotes} onChange={(event) => onPlanning({ ...planning, safeguardNotes: event.target.value })} /></label><label><span>Reviewed at *</span><input type="datetime-local" value={reviewedAt} onChange={(event) => setReviewedAt(event.target.value)} /></label><button className="primary-button" type="button" disabled={!projectSession || !selectedSafeguardScenarios.length || state === "saving-safeguard"} onClick={() => void saveSafeguard()}>{state === "saving-safeguard" ? "Persisting safeguard…" : retainedSafeguardGuildIds.has(guildId) ? "Update guild safeguard review" : "Save guild safeguard record"}</button><p className="control-requirement">Each guild is reviewed separately. The authoritative model check rejects cross-guild or overlapping scenario coverage.</p></div></section>
          <section className="paper-panel road-mask-panel"><h2>{professionalRecords.reviewMasks.length ? "Project diagnostic mask recorded" : "No project review mask configured"}</h2><p>Import and authorize a project-specific MASK raster. Current redistribution is a diagnostic review trigger, not collision risk, crossing use, or an objective.</p><label><span>Reviewed MASK asset *</span><select value={maskAssetId} onChange={(event) => setMaskAssetId(event.target.value)}><option value="">Choose persisted mask…</option>{maskAssets.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title}</option>)}</select></label><label><span>Mask title *</span><input value={maskTitle} onChange={(event) => setMaskTitle(event.target.value)} /></label><label><span>Diagnostic review trigger (%) *</span><input inputMode="decimal" value={maskThreshold} onChange={(event) => setMaskThreshold(event.target.value)} /></label><label><span>Interpretation *</span><textarea rows={4} value={maskInterpretation} onChange={(event) => setMaskInterpretation(event.target.value)} placeholder="Explain what a trigger sends to human review and what it cannot mean." /></label><button className="secondary-button" type="button" disabled={!projectSession || !maskAssets.length || state === "saving-mask"} onClick={() => void saveMask()}>{state === "saving-mask" ? "Persisting mask…" : "Save diagnostic mask"}</button><p className="control-requirement">The mask remains diagnostic and cannot become a portfolio objective.</p></section>
        </div>
        {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
      </>
    );
  }
  return (
    <>
      <PageIntro kicker="Environmental safety" title="A headline modeled resistance reduction cannot erase a safeguard failure." description="Critical unknowns resolve to Survey first. They do not disappear into a composite score." badge={<StatusPill state="review">One unresolved assumption</StatusPill>} />
      <div className="safeguard-layout">
        <section className="paper-panel safeguard-matrix"><div className="panel-heading"><div><span className="section-kicker">Review ledger</span><h2>Blocking concerns</h2></div></div>{concerns.map((concern, index) => <div className="safeguard-row" key={concern}><span>{String(index + 1).padStart(2, "0")}</span><strong>{concern}</strong><StatusPill state={guided && index < 5 ? "pass" : "review"}>{guided && index < 5 ? "Reviewed" : index === 5 ? "Survey first" : "Review"}</StatusPill></div>)}</section>
        <section className="paper-panel road-mask-panel"><div className="mask-graphic" aria-hidden="true"><span className="mask-road" /><span className="mask-zone" /><i /><i /><i /></div><h2>Configured road-review mask</h2><p>Modeled current redistribution inside this mask triggers review; it is not a collision-risk or crossing-use estimate.</p><dl><div><dt>Threshold</dt><dd>Fixture rule · review when configured summary exceeds its authored limit</dd></div><div><dt>Unresolved</dt><dd>{guided ? "North-approach land access" : planning.safeguardNotes || "No project-specific safeguard record entered"}</dd></div></dl>{!guided && <label><span>Safeguard notes</span><textarea rows={3} value={planning.safeguardNotes} onChange={(event) => onPlanning({ ...planning, safeguardNotes: event.target.value })} /></label>}</section>
      </div>
    </>
  );
}

function RulesCostsScreen({ path, planning, onPlanning, fixture, guidedProjectionError, projectSession, professionalAssets, professionalRecords, onProfessionalRecords, observabilityDeployment }: Pick<ScreensProps, "path" | "planning" | "onPlanning" | "fixture" | "guidedProjectionError" | "projectSession" | "professionalAssets" | "professionalRecords" | "onProfessionalRecords" | "observabilityDeployment">) {
  const guided = path === "guided";
  type RuleMetric = "MIN_RESISTANCE_REDUCTION_PERCENT" | "MAX_RESISTANCE_INCREASE_PERCENT" | "MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT" | "MAX_COST" | "REQUIRE_ACCEPTED_QA" | "REQUIRE_SAFEGUARD_NONREGRESSION";
  const [briefDraft, setBriefDraft, undoBriefDraft, briefDirty, commitBriefDraft] = useWorkspaceDraft(`decision-brief-editor:${projectSession?.projectId ?? "unbound"}:${professionalRecords.decisionBrief?.brief_id ?? "new"}`, () => {
    const retained = professionalRecords.decisionBrief;
    return {
      briefId: retained?.brief_id ?? stableUiId("decision_brief"),
      briefTitle: retained?.title ?? "",
      decisionQuestion: retained?.decision_question ?? planning.decisionQuestion,
      decisionOwner: retained?.decision_owner ?? planning.reviewer,
      intendedUse: retained?.intended_use ?? planning.intendedUse,
      targetGuilds: (retained?.target_guilds ?? (planning.targetGuild ? [planning.targetGuild] : [])).join("\n"),
      safeguardGuilds: (retained?.safeguard_guilds ?? []).join("\n"),
      budgetAmount: retained?.budget_amount == null ? planning.budgetUsd : String(retained.budget_amount),
      currency: retained?.currency ?? "USD",
      costVintage: retained?.cost_vintage ?? "",
      nonGoals: (retained?.non_goals ?? ["No animal-movement, crossing-use, collision-reduction, population, biodiversity, design, approval, or regulatory claim."]).join("\n"),
      nextAction: retained?.required_next_action ?? "Ecological, field, engineering, ownership, and permitting feasibility review.",
      conceptual: retained?.conceptual ?? path === "planning",
    };
  });
  const [ruleDraft, setRuleDraft, undoRuleDraft, ruleDirty, commitRuleDraft] = useWorkspaceDraft("decision-rule-editor", () => ({ ruleId: stableUiId("rule"), ruleTitle: "", metric: "MIN_RESISTANCE_REDUCTION_PERCENT" as RuleMetric, ruleThreshold: "", ruleRationale: "" }));
  const [policyDraft, setPolicyDraft, undoPolicyDraft, policyDirty, commitPolicyDraft] = useWorkspaceDraft(`resource-policy-editor:${projectSession?.projectId ?? "unbound"}:${professionalRecords.resourcePolicy ? "retained" : "new"}`, () => {
    const retained = professionalRecords.resourcePolicy;
    const mib = 1024 * 1024;
    return {
      maxDirectJobs: String(retained?.max_direct_jobs ?? 1000),
      maxInputMb: String((retained?.max_input_bytes ?? 2048 * mib) / mib),
      maxMaterializedMb: String((retained?.max_materialized_bytes ?? 8192 * mib) / mib),
      maxCurrentMb: String((retained?.max_current_raster_bytes ?? 4096 * mib) / mib),
      maxWallSeconds: String(retained?.max_wall_seconds_per_job ?? 1800),
      requireExplicitOversizeApproval: retained?.require_explicit_oversize_approval ?? true,
    };
  });
  const [costBasisDraft, setCostBasisDraft, undoCostBasisDraft, costBasisDirty, commitCostBasisDraft] = useWorkspaceDraft("cost-basis-editor", () => {
    const basis = professionalRecords.costBasis;
    return {
      costBasisId: basis?.cost_basis_id ?? stableUiId("cost_basis"),
      source: basis?.source ?? "",
      currency: basis?.currency ?? professionalRecords.decisionBrief?.currency ?? professionalRecords.interventionOptions[0]?.currency ?? "USD",
      vintage: basis?.vintage ?? professionalRecords.decisionBrief?.cost_vintage ?? professionalRecords.interventionOptions[0]?.cost_vintage ?? "",
      analysisHorizonYears: costAssumptionValue(basis?.assumptions, COST_ASSUMPTION_PREFIXES.analysisHorizonYears) || "30",
      realDiscountRatePercent: costAssumptionValue(basis?.assumptions, COST_ASSUMPTION_PREFIXES.realDiscountRatePercent) || "3",
      capexBasis: costAssumptionValue(basis?.assumptions, COST_ASSUMPTION_PREFIXES.capexBasis),
      omAssumptions: costAssumptionValue(basis?.assumptions, COST_ASSUMPTION_PREFIXES.omAssumptions),
      replacementAssumptions: costAssumptionValue(basis?.assumptions, COST_ASSUMPTION_PREFIXES.replacementAssumptions),
      reviewer: basis?.reviewer ?? planning.reviewer,
      reviewedAt: dateTimeLocalValue(basis?.reviewed_at),
      optionCosts: Object.fromEntries(professionalRecords.interventionOptions.map((option) => [option.option_id, String(basis?.option_costs[option.option_id] ?? option.cost)])),
      confirmed: false,
    };
  });
  const { briefId, briefTitle, decisionQuestion, decisionOwner, intendedUse, targetGuilds, safeguardGuilds, budgetAmount, currency, costVintage, nonGoals, nextAction, conceptual } = briefDraft;
  const { ruleId, ruleTitle, metric, ruleThreshold, ruleRationale } = ruleDraft;
  const { maxDirectJobs, maxInputMb, maxMaterializedMb, maxCurrentMb, maxWallSeconds, requireExplicitOversizeApproval } = policyDraft;
  const { costBasisId, source: costSource, currency: basisCurrency, vintage: basisVintage, analysisHorizonYears, realDiscountRatePercent, capexBasis, omAssumptions, replacementAssumptions, reviewer: costReviewer, reviewedAt: costReviewedAt, optionCosts, confirmed: costBasisConfirmed } = costBasisDraft;
  const setBriefField = <K extends keyof typeof briefDraft>(field: K, value: (typeof briefDraft)[K]) => setBriefDraft((current) => ({ ...current, [field]: value }));
  const setRuleField = <K extends keyof typeof ruleDraft>(field: K, value: (typeof ruleDraft)[K]) => setRuleDraft((current) => ({ ...current, [field]: value }));
  const setPolicyField = <K extends keyof typeof policyDraft>(field: K, value: (typeof policyDraft)[K]) => setPolicyDraft((current) => ({ ...current, [field]: value }));
  const setCostBasisField = <K extends keyof typeof costBasisDraft>(field: K, value: (typeof costBasisDraft)[K]) => setCostBasisDraft((current) => ({ ...current, [field]: value }));
  const setBriefTitle = (value: string) => setBriefField("briefTitle", value);
  const setDecisionQuestion = (value: string) => setBriefField("decisionQuestion", value);
  const setDecisionOwner = (value: string) => setBriefField("decisionOwner", value);
  const setIntendedUse = (value: string) => setBriefField("intendedUse", value);
  const setTargetGuilds = (value: string) => setBriefField("targetGuilds", value);
  const setSafeguardGuilds = (value: string) => setBriefField("safeguardGuilds", value);
  const setBudgetAmount = (value: string) => setBriefField("budgetAmount", value);
  const setCurrency = (value: string) => setBriefField("currency", value);
  const setCostVintage = (value: string) => setBriefField("costVintage", value);
  const setNonGoals = (value: string) => setBriefField("nonGoals", value);
  const setNextAction = (value: string) => setBriefField("nextAction", value);
  const setRuleTitle = (value: string) => setRuleField("ruleTitle", value);
  const setMetric = (value: RuleMetric) => setRuleField("metric", value);
  const setRuleThreshold = (value: string) => setRuleField("ruleThreshold", value);
  const setRuleRationale = (value: string) => setRuleField("ruleRationale", value);
  const setMaxDirectJobs = (value: string) => setPolicyField("maxDirectJobs", value);
  const setMaxInputMb = (value: string) => setPolicyField("maxInputMb", value);
  const setMaxMaterializedMb = (value: string) => setPolicyField("maxMaterializedMb", value);
  const setMaxCurrentMb = (value: string) => setPolicyField("maxCurrentMb", value);
  const setMaxWallSeconds = (value: string) => setPolicyField("maxWallSeconds", value);
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [message, setMessage] = useState("");
  const [costBasisState, setCostBasisState] = useState<"idle" | "saving" | "error">("idle");
  const [costBasisMessage, setCostBasisMessage] = useState("");
  const [costBasisAuthority, setCostBasisAuthority] = useState<ReturnType<typeof CostBasisGetResponseSchema.parse> | null>(null);
  const [costBasisAuthorityState, setCostBasisAuthorityState] = useState<"idle" | "loading" | "error">("idle");
  useEffect(() => {
    if (guided || !projectSession) return;
    let active = true;
    setCostBasisAuthorityState("loading");
    void hostRequest<RpcPayload<unknown>>("connectivity.cost_basis.get", { project_handle: projectSession.projectHandle })
      .then((response) => {
        if (!active) return;
        setCostBasisAuthority(CostBasisGetResponseSchema.parse(rpcData(response)));
        setCostBasisAuthorityState("idle");
      })
      .catch(() => {
        if (!active) return;
        setCostBasisAuthority(null);
        setCostBasisAuthorityState("error");
      });
    return () => { active = false; };
  }, [guided, projectSession?.projectHandle]);
  const costOptions = [...professionalRecords.interventionOptions].sort((left, right) => left.site_id.localeCompare(right.site_id) || left.option_id.localeCompare(right.option_id));
  const parsedHorizon = Number(analysisHorizonYears);
  const parsedDiscountRate = Number(realDiscountRatePercent);
  const costBasisIssues: string[] = [];
  if (!costOptions.length) costBasisIssues.push("Persist at least one reviewed intervention option before authoring its cost basis.");
  if (!costSource.trim()) costBasisIssues.push("Enter the exact cost source or citation.");
  if (!basisCurrency.trim()) costBasisIssues.push("Enter a currency.");
  if (!basisVintage.trim()) costBasisIssues.push("Enter the cost-data vintage.");
  if (!Number.isInteger(parsedHorizon) || parsedHorizon <= 0 || parsedHorizon > 200) costBasisIssues.push("Analysis horizon must be a whole number from 1 through 200 years.");
  if (!Number.isFinite(parsedDiscountRate) || parsedDiscountRate < 0 || parsedDiscountRate > 100) costBasisIssues.push("Real discount rate must be a finite percentage from 0 through 100%.");
  if (!capexBasis.trim()) costBasisIssues.push("Describe what the CAPEX values include and exclude.");
  if (!omAssumptions.trim()) costBasisIssues.push("Describe recurring operations and maintenance assumptions.");
  if (!replacementAssumptions.trim()) costBasisIssues.push("Describe replacement timing and residual-value assumptions, or state that none apply.");
  if (!costReviewer.trim()) costBasisIssues.push("Name the accountable cost-basis reviewer.");
  if (!costReviewedAt || Number.isNaN(new Date(costReviewedAt).valueOf())) costBasisIssues.push("Enter a valid review date and time.");
  const optionIds = new Set(costOptions.map((option) => option.option_id));
  const extraCostIds = Object.keys(optionCosts).filter((optionId) => !optionIds.has(optionId));
  if (extraCostIds.length) costBasisIssues.push(`Remove ${extraCostIds.length} stale option-cost entr${extraCostIds.length === 1 ? "y" : "ies"}.`);
  for (const option of costOptions) {
    const value = Number(optionCosts[option.option_id]);
    if (!optionCosts[option.option_id]?.trim() || !Number.isFinite(value) || value < 0) {
      costBasisIssues.push(`Enter a finite, nonnegative cost for ${option.title}.`);
    } else if (Math.abs(value - option.cost) > 1e-9) {
      costBasisIssues.push(`${option.title} must match its persisted intervention cost (${option.cost.toLocaleString()} ${option.currency}); revise the intervention record first.`);
    }
    if (option.currency !== basisCurrency.trim()) costBasisIssues.push(`${option.title} uses ${option.currency}; every option must use the cost-basis currency exactly.`);
    if (option.cost_vintage !== basisVintage.trim()) costBasisIssues.push(`${option.title} uses vintage ${option.cost_vintage}; every option must use the cost-basis vintage.`);
  }
  if (!costBasisConfirmed) costBasisIssues.push("Confirm that the source, assumptions, and complete option-cost ledger were reviewed.");
  const coveredCostCount = costOptions.filter((option) => {
    const value = Number(optionCosts[option.option_id]);
    return optionCosts[option.option_id]?.trim() && Number.isFinite(value) && value >= 0;
  }).length;
  const saveRecord = async (method: string, key: string, record: unknown, apply: (revision: number) => void, commitDraft: () => void) => {
    if (!projectSession) return;
    setState("saving"); setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>(method, { project_handle: projectSession.projectHandle, [key]: record });
      const saved = RecordSaveResponseSchema.parse(rpcData(response));
      apply(saved.revision);
      commitDraft();
      setState("idle"); setMessage(`${saved.record_id} persisted at worker revision ${saved.revision}.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The reviewed decision contract could not be persisted.")); }
  };
  const saveBrief = () => {
    try {
      const targetGuildList = [...new Set(targetGuilds.split("\n").map((item) => item.trim()).filter(Boolean))];
      const safeguardGuildList = [...new Set(safeguardGuilds.split("\n").map((item) => item.trim()).filter(Boolean))];
      const nonGoalList = nonGoals.split("\n").map((item) => item.trim()).filter(Boolean);
      if ([briefTitle, decisionQuestion, decisionOwner, intendedUse, nextAction].some((item) => !item.trim()) || !targetGuildList.length || !nonGoalList.length) throw new Error("Complete the title, question, owner, intended use, target guilds, non-goals, and required next action.");
      const amount = budgetAmount.trim() ? Number(budgetAmount) : null;
      if (amount !== null && (!Number.isFinite(amount) || amount < 0 || !currency.trim() || !costVintage.trim())) throw new Error("A budget requires a nonnegative amount, currency, and cost vintage.");
      const brief = ConnectivityDecisionBriefSchema.parse({ brief_id: briefId, title: briefTitle.trim(), decision_question: decisionQuestion.trim(), decision_owner: decisionOwner.trim(), intended_use: intendedUse.trim(), target_guilds: targetGuildList, safeguard_guilds: safeguardGuildList, budget_amount: amount, currency: amount === null ? null : currency.trim(), cost_vintage: amount === null ? null : costVintage.trim(), non_goals: nonGoalList, required_next_action: nextAction.trim(), conceptual });
      void saveRecord("connectivity.decision_brief.save", "decision_brief", brief, () => {
        setBriefDraft({ briefId: brief.brief_id, briefTitle: brief.title, decisionQuestion: brief.decision_question, decisionOwner: brief.decision_owner, intendedUse: brief.intended_use, targetGuilds: brief.target_guilds.join("\n"), safeguardGuilds: (brief.safeguard_guilds ?? []).join("\n"), budgetAmount: brief.budget_amount == null ? "" : String(brief.budget_amount), currency: brief.currency ?? "", costVintage: brief.cost_vintage ?? "", nonGoals: (brief.non_goals ?? []).join("\n"), nextAction: brief.required_next_action, conceptual: brief.conceptual ?? false });
        onProfessionalRecords({ ...professionalRecords, decisionBrief: brief });
        onPlanning({ ...planning, decisionQuestion: brief.decision_question, intendedUse: brief.intended_use, reviewer: brief.decision_owner, targetGuild: brief.target_guilds[0] ?? "", budgetUsd: brief.budget_amount == null ? "" : String(brief.budget_amount) });
      }, commitBriefDraft);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The decision brief is incomplete.")); }
  };
  const saveRule = () => {
    try {
      if (!ruleTitle.trim() || !ruleRationale.trim() || !ruleThreshold.trim()) throw new Error("Complete rule title, threshold, and rationale.");
      const booleanMetric = metric === "REQUIRE_ACCEPTED_QA" || metric === "REQUIRE_SAFEGUARD_NONREGRESSION";
      if (booleanMetric && !["true", "false"].includes(ruleThreshold.trim().toLowerCase())) throw new Error("Boolean release rules require exactly true or false.");
      const operator = metric === "MIN_RESISTANCE_REDUCTION_PERCENT" ? "AT_LEAST" : booleanMetric ? "EQUALS" : "AT_MOST";
      const thresholdValue = booleanMetric ? ruleThreshold.trim().toLowerCase() === "true" : Number(ruleThreshold);
      if (!booleanMetric && !Number.isFinite(thresholdValue)) throw new Error("Numeric rules require a finite threshold.");
      const pairIds = [...new Set(professionalRecords.focalAreaSets.flatMap((item) => item.pairs.map((pair) => pair.pair_id)))];
      const rule = ConnectivityDecisionRuleSchema.parse({ rule_id: ruleId, title: ruleTitle.trim(), metric, operator, threshold: thresholdValue, scenario_ids: professionalRecords.scenarios.map((item) => item.scenario_id), pair_ids: metric === "MIN_RESISTANCE_REDUCTION_PERCENT" || metric === "MAX_RESISTANCE_INCREASE_PERCENT" ? pairIds : [], required: true, rationale: ruleRationale.trim() });
      void saveRecord("connectivity.decision_rule.save", "decision_rule", rule, () => onProfessionalRecords({ ...professionalRecords, decisionRules: [...professionalRecords.decisionRules.filter((item) => item.rule_id !== rule.rule_id), rule] }), commitRuleDraft);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The decision rule is incomplete.")); }
  };
  const savePolicy = () => {
    try {
      if (!requireExplicitOversizeApproval) throw new Error("Explicit oversize approval is a required fail-closed safety control.");
      const mib = 1024 * 1024;
      const policy = ResourcePolicySchema.parse({ max_input_bytes: Number(maxInputMb) * mib, max_materialized_bytes: Number(maxMaterializedMb) * mib, max_current_raster_bytes: Number(maxCurrentMb) * mib, max_direct_jobs: Number(maxDirectJobs), max_wall_seconds_per_job: Number(maxWallSeconds), require_explicit_oversize_approval: requireExplicitOversizeApproval });
      void saveRecord("connectivity.resource_policy.save", "resource_policy", policy, () => {
        setPolicyDraft({ maxDirectJobs: String(policy.max_direct_jobs), maxInputMb: String(policy.max_input_bytes / mib), maxMaterializedMb: String(policy.max_materialized_bytes / mib), maxCurrentMb: String(policy.max_current_raster_bytes / mib), maxWallSeconds: String(policy.max_wall_seconds_per_job), requireExplicitOversizeApproval: policy.require_explicit_oversize_approval ?? true });
        onProfessionalRecords({ ...professionalRecords, resourcePolicy: policy });
      }, commitPolicyDraft);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "Every resource limit must be a positive number.")); }
  };
  const saveCostBasis = async () => {
    if (!projectSession) return;
    setCostBasisMessage("");
    if (costBasisIssues.length) {
      setCostBasisState("error");
      setCostBasisMessage(costBasisIssues[0] ?? "Complete the reviewed cost basis before saving.");
      return;
    }
    setCostBasisState("saving");
    try {
      const assumptions = [
        `${COST_ASSUMPTION_PREFIXES.analysisHorizonYears} ${parsedHorizon}`,
        `${COST_ASSUMPTION_PREFIXES.realDiscountRatePercent} ${parsedDiscountRate}`,
        `${COST_ASSUMPTION_PREFIXES.capexBasis} ${capexBasis.trim()}`,
        `${COST_ASSUMPTION_PREFIXES.omAssumptions} ${omAssumptions.trim()}`,
        `${COST_ASSUMPTION_PREFIXES.replacementAssumptions} ${replacementAssumptions.trim()}`,
      ];
      const costBasis = ConnectivityCostBasisSchema.parse({
        cost_basis_id: costBasisId,
        source: costSource.trim(),
        currency: basisCurrency.trim(),
        vintage: basisVintage.trim(),
        reviewer: costReviewer.trim(),
        reviewed_at: new Date(costReviewedAt).toISOString(),
        option_costs: Object.fromEntries(costOptions.map((option) => [option.option_id, Number(optionCosts[option.option_id])])),
        assumptions,
      });
      const confirmationStatement = "I reviewed the cited source, present-value assumptions, and exact costs for every retained intervention option; no ecological or feasibility benefit is implied.";
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.cost_basis.save", {
        project_handle: projectSession.projectHandle,
        cost_basis: costBasis,
        confirmed: true,
        confirmation_statement: confirmationStatement,
      });
      const saved = CostBasisSaveResponseSchema.parse(rpcData(response));
      onProfessionalRecords({ ...professionalRecords, costBasis: saved.cost_basis });
      setCostBasisAuthority({ cost_basis: saved.cost_basis, active_revision_id: saved.active_revision_id, confirmed: true, missing_requirements: [] });
      setCostBasisAuthorityState("idle");
      setCostBasisDraft((current) => ({ ...current, confirmed: false }));
      commitCostBasisDraft();
      setCostBasisState("idle");
      setCostBasisMessage(`Reviewed cost basis persisted at worker revision ${saved.revision}; all ${costOptions.length} option costs remain traceable. Semantic revision ${saved.active_revision_id} is now active, so revalidate Model Fitness before attesting or running.`);
    } catch (error) {
      setCostBasisState("error");
      setCostBasisMessage(hostErrorMessage(error, "The reviewed cost basis could not be persisted."));
    }
  };
  if (!guided) {
    return (
      <>
        <PageIntro kicker="Decision contract" title="Make acceptance testable before running." description="No Ridgeway threshold is reused. Project rules retain their scenario, pair, direction, reducer, threshold, rationale, and reviewed cost basis." badge={<StatusPill state={professionalRecords.decisionRules.length && professionalRecords.decisionBrief && professionalRecords.costBasis && professionalRecords.resourcePolicy ? "pass" : "fail"}>{professionalRecords.costBasis ? `${professionalRecords.decisionRules.length} rule${professionalRecords.decisionRules.length === 1 ? "" : "s"} · cost basis reviewed` : "Cost basis required"}</StatusPill>} />
        <div className="professional-rule-layout rule-cards">
          <article className="paper-panel"><span className="rule-type">BRIEF</span><h2>Decision boundary</h2><p>Revise the retained professional question without losing its stable record identity or silently replacing fields that are not on screen.</p><div className="draft-actions"><small>{briefDirty ? "Draft retained in workspace" : professionalRecords.decisionBrief ? "Authoritative decision brief loaded" : "No unsaved field changes"}</small><button className="text-action" type="button" disabled={!briefDirty} onClick={undoBriefDraft}>Undo brief draft</button></div><label><span>Brief title *</span><input value={briefTitle} onChange={(event) => setBriefTitle(event.target.value)} /></label><label><span>Decision question *</span><textarea rows={3} value={decisionQuestion} onChange={(event) => setDecisionQuestion(event.target.value)} /></label><label><span>Decision owner *</span><input value={decisionOwner} onChange={(event) => setDecisionOwner(event.target.value)} /></label><label><span>Intended use *</span><textarea rows={3} value={intendedUse} onChange={(event) => setIntendedUse(event.target.value)} /></label><div className="field-grid two-up"><label><span>Target guilds * · one per line</span><textarea rows={3} value={targetGuilds} onChange={(event) => setTargetGuilds(event.target.value)} /></label><label><span>Safeguard guilds · one per line</span><textarea rows={3} value={safeguardGuilds} onChange={(event) => setSafeguardGuilds(event.target.value)} /></label><label><span>Screening budget</span><input inputMode="decimal" value={budgetAmount} onChange={(event) => setBudgetAmount(event.target.value.replace(/[^0-9.]/g, ""))} /></label><label><span>Currency</span><input value={currency} onChange={(event) => setCurrency(event.target.value)} /></label><label><span>Cost vintage</span><input value={costVintage} onChange={(event) => setCostVintage(event.target.value)} /></label></div><label><span>Permanent non-goals * · one per line</span><textarea rows={3} value={nonGoals} onChange={(event) => setNonGoals(event.target.value)} /></label><label><span>Required next action *</span><textarea rows={2} value={nextAction} onChange={(event) => setNextAction(event.target.value)} /></label><p className="control-requirement"><strong>Record class:</strong> {conceptual ? "Conceptual planning screen" : "Reviewed-model screen"}. Saving retains <span className="mono">{briefId}</span>, publishes a semantic revision, and makes the prior fitness review stale.</p><button className="secondary-button" type="button" disabled={!projectSession || !briefDirty || state === "saving"} onClick={saveBrief}>Save decision brief</button></article>
          <article className="paper-panel"><span className="rule-type safeguard">RULE</span><h2>Testable criterion</h2><div className="draft-actions"><small>{ruleDirty ? "Draft retained in workspace" : "No unsaved field changes"}</small><button className="text-action" type="button" disabled={!ruleDirty} onClick={undoRuleDraft}>Undo rule draft</button></div><label><span>Rule title *</span><input value={ruleTitle} onChange={(event) => setRuleTitle(event.target.value)} /></label><label><span>Metric *</span><select value={metric} onChange={(event) => setMetric(event.target.value as RuleMetric)}><option value="MIN_RESISTANCE_REDUCTION_PERCENT">Minimum within-scenario resistance reduction</option><option value="MAX_RESISTANCE_INCREASE_PERCENT">Maximum within-scenario resistance increase</option><option value="MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT">Maximum diagnostic mask redistribution</option><option value="MAX_COST">Maximum screening cost</option><option value="REQUIRE_ACCEPTED_QA">Require accepted QA</option><option value="REQUIRE_SAFEGUARD_NONREGRESSION">Require safeguard nonregression</option></select></label><label><span>Threshold * {metric.startsWith("REQUIRE_") ? "(true or false)" : ""}</span><input value={ruleThreshold} onChange={(event) => setRuleThreshold(event.target.value)} /></label><label><span>Rationale *</span><textarea rows={3} value={ruleRationale} onChange={(event) => setRuleRationale(event.target.value)} /></label><button className="secondary-button" type="button" disabled={!projectSession || !professionalRecords.scenarios.length || state === "saving"} onClick={saveRule}>Save required rule</button><small>Binds all currently persisted required scenarios and applicable focal pairs.</small></article>
          <article className="paper-panel"><span className="rule-type cost">RESOURCES</span><h2>Hard project policy</h2><div className="draft-actions"><small>{policyDirty ? "Draft retained in workspace" : professionalRecords.resourcePolicy ? "Authoritative resource policy loaded" : "No unsaved field changes"}</small><button className="text-action" type="button" disabled={!policyDirty} onClick={undoPolicyDraft}>Undo policy draft</button></div><label><span>Maximum direct jobs</span><input inputMode="numeric" value={maxDirectJobs} onChange={(event) => setMaxDirectJobs(event.target.value)} /></label><label><span>Maximum input (MiB)</span><input inputMode="numeric" value={maxInputMb} onChange={(event) => setMaxInputMb(event.target.value)} /></label><label><span>Maximum materialized (MiB)</span><input inputMode="numeric" value={maxMaterializedMb} onChange={(event) => setMaxMaterializedMb(event.target.value)} /></label><label><span>Maximum current rasters (MiB)</span><input inputMode="numeric" value={maxCurrentMb} onChange={(event) => setMaxCurrentMb(event.target.value)} /></label><label><span>Per-job wall time (seconds)</span><input inputMode="numeric" value={maxWallSeconds} onChange={(event) => setMaxWallSeconds(event.target.value)} /></label><label className="confirmation-row"><input type="checkbox" checked={requireExplicitOversizeApproval} disabled={requireExplicitOversizeApproval} onChange={(event) => { if (event.target.checked) setPolicyField("requireExplicitOversizeApproval", true); }} /><span><strong>Require explicit approval for every oversize run.</strong> This fail-closed control can be enabled here but cannot be weakened in the editor.</span></label><button className="secondary-button" type="button" disabled={!projectSession || !policyDirty || state === "saving"} onClick={savePolicy}>Save resource policy</button><small>No current raster is silently omitted while the application claims complete synchronization.</small></article>
        </div>
        <section className="paper-panel cost-basis-ledger" aria-labelledby="cost-basis-heading">
          <div className="cost-basis-heading">
            <div><span className="section-kicker">Evidence ledger</span><h2 id="cost-basis-heading">Reviewed present-value cost basis</h2><p>Record one sourced economic basis for the full intervention catalog. Costs affect portfolio ranking only; they never enter the Circuitscape model-run cache or imply ecological benefit. Publishing creates a semantic model revision and makes any earlier Model Fitness attestation stale.</p></div>
            <div className="cost-basis-heading-status" aria-live="polite">
              <StatusPill state={costBasisAuthorityState === "error" || (costBasisAuthority && !costBasisAuthority.confirmed) ? "fail" : costBasisAuthority?.confirmed ? "pass" : "review"}>{costBasisAuthorityState === "loading" ? "Checking worker status" : costBasisAuthorityState === "error" ? "Worker status unavailable" : costBasisAuthority?.confirmed ? "Worker-confirmed basis" : costBasisAuthority ? `${costBasisAuthority.missing_requirements.length} requirement${costBasisAuthority.missing_requirements.length === 1 ? "" : "s"} missing` : "No retained basis"}</StatusPill>
              {costBasisAuthority?.missing_requirements.length ? <small>{costBasisAuthority.missing_requirements.slice(0, 2).join(" · ")}</small> : costBasisAuthority?.confirmed ? <small className="mono">Revision {costBasisAuthority.active_revision_id}</small> : null}
              <div className="cost-basis-coverage" aria-label={`${coveredCostCount} of ${costOptions.length} intervention option costs entered`}><strong>{coveredCostCount}/{costOptions.length}</strong><span>option costs covered</span></div>
            </div>
          </div>
          <div className="cost-basis-fields">
            <div className="draft-actions"><small>{costBasisDirty ? "Draft retained in workspace" : professionalRecords.costBasis ? "Authoritative basis loaded" : "No unsaved field changes"}</small><button className="text-action" type="button" disabled={!costBasisDirty} onClick={undoCostBasisDraft}>Undo cost-basis draft</button></div>
            <div className="field-grid two-up">
              <label className="field-span"><span>Cost source or citation *</span><textarea rows={2} value={costSource} onChange={(event) => setCostBasisField("source", event.target.value)} placeholder="Agency estimate, cost database edition, consultant memorandum, or other exact reviewed source" /></label>
              <label><span>Currency *</span><input value={basisCurrency} onChange={(event) => setCostBasisField("currency", event.target.value)} /></label>
              <label><span>Cost vintage *</span><input value={basisVintage} onChange={(event) => setCostBasisField("vintage", event.target.value)} placeholder="2026 USD" /></label>
              <label><span>Analysis horizon (years) *</span><input inputMode="numeric" value={analysisHorizonYears} onChange={(event) => setCostBasisField("analysisHorizonYears", event.target.value.replace(/[^0-9]/g, ""))} /></label>
              <label><span>Real discount rate (%) *</span><input inputMode="decimal" value={realDiscountRatePercent} onChange={(event) => setCostBasisField("realDiscountRatePercent", event.target.value.replace(/[^0-9.-]/g, ""))} /></label>
              <label className="field-span"><span>CAPEX basis *</span><textarea rows={3} value={capexBasis} onChange={(event) => setCostBasisField("capexBasis", event.target.value)} placeholder="Included quantities, unit-cost method, contingencies, escalation treatment, and exclusions" /></label>
              <label className="field-span"><span>O&amp;M assumptions *</span><textarea rows={3} value={omAssumptions} onChange={(event) => setCostBasisField("omAssumptions", event.target.value)} placeholder="Inspection, vegetation, fence, crossing, and access maintenance assumptions" /></label>
              <label className="field-span"><span>Replacement assumptions *</span><textarea rows={3} value={replacementAssumptions} onChange={(event) => setCostBasisField("replacementAssumptions", event.target.value)} placeholder="Replacement years, residual value, or an explicit reviewed statement that none apply" /></label>
              <label><span>Cost-basis reviewer *</span><input value={costReviewer} onChange={(event) => setCostBasisField("reviewer", event.target.value)} /></label>
              <label><span>Reviewed at *</span><input type="datetime-local" value={costReviewedAt} onChange={(event) => setCostBasisField("reviewedAt", event.target.value)} /></label>
            </div>
          </div>
          <div className="cost-option-ledger">
            <div className="panel-heading"><div><span className="section-kicker">Complete catalog coverage</span><h2>Exact screening cost by option</h2></div><StatusPill state={costOptions.length > 0 && coveredCostCount === costOptions.length ? "pass" : "fail"}>{costOptions.length > 0 && coveredCostCount === costOptions.length ? "Every option represented" : "Coverage incomplete"}</StatusPill></div>
            <div className="table-scroll"><table><thead><tr><th>Site / option</th><th>Reviewed kind</th><th>Persisted cost</th><th>Cost-basis value</th></tr></thead><tbody>{costOptions.length ? costOptions.map((option) => { const site = professionalRecords.interventionSites.find((item) => item.site_id === option.site_id); return <tr key={option.option_id}><td><strong>{site?.title ?? option.site_id} · {option.title}</strong><span className="cost-option-meta">{option.option_id}</span></td><td>{option.kind.replaceAll("_", " ").toLowerCase()}</td><td className="mono">{option.cost.toLocaleString()} {option.currency} · {option.cost_vintage}</td><td><input aria-label={`${option.title} cost (${basisCurrency || option.currency})`} inputMode="decimal" value={optionCosts[option.option_id] ?? ""} onChange={(event) => setCostBasisField("optionCosts", { ...optionCosts, [option.option_id]: event.target.value.replace(/[^0-9.]/g, "") })} /></td></tr>; }) : <tr><td colSpan={4}>No persisted intervention options exist. Return to Reviewed interventions and create the complete mutually exclusive catalog first.</td></tr>}</tbody></table></div>
          </div>
          <label className="confirmation-row cost-basis-confirmation"><input type="checkbox" checked={costBasisConfirmed} onChange={(event) => setCostBasisField("confirmed", event.target.checked)} /><span><strong>I reviewed the source, present-value assumptions, and every exact option cost.</strong> This basis supports screening comparisons only; it does not establish ecological benefit, constructability, funding eligibility, or approval.</span></label>
          <div className="cost-basis-action">
            <div>{costBasisIssues.length ? <><strong>{costBasisIssues.length} item{costBasisIssues.length === 1 ? "" : "s"} prevent publication</strong><ul>{costBasisIssues.slice(0, 4).map((issue, index) => <li key={`${index}-${issue}`}>{issue}</li>)}</ul>{costBasisIssues.length > 4 && <span>{costBasisIssues.length - 4} additional catalog issue{costBasisIssues.length - 4 === 1 ? "" : "s"} remain.</span>}</> : <><strong>Complete reviewed ledger</strong><span>The worker will recheck catalog coverage and create a new semantic revision. Model Fitness must then be revalidated and re-attested.</span></>}</div>
            <button className="primary-button" type="button" disabled={!projectSession || costBasisIssues.length > 0 || costBasisState === "saving"} onClick={() => void saveCostBasis()}>{costBasisState === "saving" ? "Publishing reviewed basis…" : "Save reviewed cost basis"}</button>
          </div>
          {costBasisMessage && <p className={`operation-message ${costBasisState === "error" ? "error" : "success"}`} role={costBasisState === "error" ? "alert" : "status"}>{costBasisMessage}</p>}
        </section>
        <IntakeAssistantPanel projectSession={projectSession} deployment={observabilityDeployment} documents={professionalAssets.filter((asset) => asset.kind === "DOCUMENT")} defaultReviewer={planning.reviewer} />
        {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
        {!professionalRecords.scenarios.length && <section className="paper-panel blocked-workspace"><Icon name="lock" /><div><h2>Rule persistence waits for model object identities.</h2><p>Create approved scenarios and focal pairs first. The application will not silently bind a threshold to synthetic or missing objects.</p></div></section>}
      </>
    );
  }
  if (!fixture) return <><PageIntro kicker="Decision contract" title="Rules and costs" description="The desktop reads the synthetic decision contract from the authoritative worker projection." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  return (
    <>
      <PageIntro kicker="Decision contract" title="Make acceptance testable before running." description="Rules retain their pair, scenario, direction, threshold, reducer, and source. Cost assumptions remain outside the physics cache key." badge={<StatusPill state={guided ? "pass" : "review"}>{guided ? "Fixture contract complete" : "Draft contract"}</StatusPill>} />
      <div className="three-card-grid rule-cards">
        <article className="paper-panel"><span className="rule-type">TARGET</span><h2>Required pair resistance</h2><p><strong>Pass when</strong><span>{fixture.decisionBrief.targetRule}</span></p><small>Evaluated within each scenario at internal precision.</small></article>
        <article className="paper-panel"><span className="rule-type safeguard">SAFEGUARD</span><h2>No adverse increase</h2><p><strong>Pass when</strong><span>{fixture.decisionBrief.safeguardRule}</span></p><small>Diagnostic current redistribution is reviewed separately.</small></article>
        <article className="paper-panel"><span className="rule-type cost">COST</span><h2>Screening budget</h2><label><span>Present-value cap (USD)</span><input inputMode="numeric" value={String(fixture.decisionBrief.budgetUsd)} readOnly /></label><small>Currency vintage, horizon, discounting, O&M, replacement, and source are required for professional use.</small></article>
      </div>
      <section className="paper-panel no-aggregation"><Icon name="shield" /><div><h2>No hidden weighting</h2><p>A portfolio passes only if every required target and safeguard rule passes. The application does not average away a weak species, scenario, focal pair, or resistance hypothesis.</p></div></section>
    </>
  );
}

function RunPlanScreen({ path, fixture, guidedProjectionError, projectSession, professionalAnalysis, onProfessionalAnalysis }: Pick<ScreensProps, "path" | "fixture" | "guidedProjectionError" | "projectSession" | "professionalAnalysis" | "onProfessionalAnalysis">) {
  const guided = path === "guided";
  const [seed, setSeed] = useState("42");
  const [reviewed, setReviewed] = useState(false);
  const [state, setState] = useState<"idle" | "preparing" | "planning" | "error">("idle");
  const [message, setMessage] = useState("");
  const prepare = async () => {
    if (!projectSession) return;
    setState("preparing"); setMessage(""); setReviewed(false);
    try {
      const parsedSeed = Number(seed);
      if (!Number.isInteger(parsedSeed) || parsedSeed < 0) throw new Error("Search seed must be a nonnegative integer.");
      const [projectResponse, prepareResponse] = await Promise.all([
        hostRequest<RpcPayload<unknown>>("connectivity.project.get_state", { project_handle: projectSession.projectHandle }),
        hostRequest<RpcPayload<unknown>>("connectivity.analysis.prepare", { project_handle: projectSession.projectHandle, seed: parsedSeed }),
      ]);
      const projectState = ProfessionalProjectStateSchema.parse(rpcData(projectResponse));
      const preparation = AnalysisPrepareResponseSchema.parse(rpcData(prepareResponse));
      if (projectState.project.project_id !== preparation.project_id || projectState.active_revision.revision_id !== preparation.revision_id) throw new Error("The project revision changed while the run proposal was prepared. Prepare it again.");
      onProfessionalAnalysis({ ...professionalAnalysis, projectState, preparation, plannedAnalysisId: null, status: null, decisionBoard: null, selectedPortfolioId: null, exportEligibility: null, exportRecord: null });
      setState("idle");
      setMessage(preparation.blockers.length ? `The worker found ${preparation.blockers.length} blocking condition${preparation.blockers.length === 1 ? "" : "s"}. No plan can be committed.` : "The worker derived a complete immutable portfolio × scenario proposal. Review it before committing.");
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The authoritative run proposal could not be prepared.")); }
  };
  const commitPlan = async () => {
    if (!projectSession || !professionalAnalysis.preparation?.proposed_plan || !reviewed) return;
    setState("planning"); setMessage("");
    try {
      const proposedPlan = professionalAnalysis.preparation.proposed_plan;
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.analysis.plan", { project_handle: projectSession.projectHandle, plan: proposedPlan });
      const receipt = AnalysisPlanReceiptSchema.parse(rpcData(response));
      if (receipt.analysis_id !== proposedPlan.analysis_id || receipt.direct_job_count !== proposedPlan.direct_job_count) throw new Error("The committed run-plan receipt differs from the worker proposal.");
      onProfessionalAnalysis({ ...professionalAnalysis, plannedAnalysisId: receipt.analysis_id, status: null, decisionBoard: null, selectedPortfolioId: null, exportEligibility: null, exportRecord: null });
      setState("idle"); setMessage(`${receipt.arithmetic}. The immutable plan is READY; no direct job has started yet.`);
    } catch (error) { setState("error"); setMessage(hostErrorMessage(error, "The exact worker proposal could not be committed.")); }
  };
  if (!guided) {
    const preparation = professionalAnalysis.preparation;
    const ready = Boolean(preparation?.proposed_plan && !preparation.blockers.length);
    return (
      <>
        <PageIntro kicker="Exact run arithmetic" title="Let the worker derive the complete ensemble." description="The interface never constructs portfolio IDs, scenarios, engine hashes, job counts, or search fields. It passes the worker proposal back unchanged after review." badge={<StatusPill state={professionalAnalysis.plannedAnalysisId ? "pass" : ready ? "review" : "fail"}>{professionalAnalysis.plannedAnalysisId ? "Immutable plan READY" : ready ? "Proposal ready for review" : "No runnable proposal"}</StatusPill>} />
        <section className="paper-panel validation-action"><div><span className="section-kicker">Authoritative preparation</span><h2>{preparation ? preparation.arithmetic : "Rebuild from the current project revision"}</h2><p>{projectSession ? "Reads the effective bundle, Fitness Passport, option catalog, required scenarios, resource policy, and installed signed engine." : "Open or create the authoritative project before preparing a run plan."}</p></div><div className="run-prepare-controls"><label><span>Deterministic seed</span><input className="mono" inputMode="numeric" value={seed} onChange={(event) => { setSeed(event.target.value.replace(/[^0-9]/g, "")); setReviewed(false); }} /></label><button className="primary-button" type="button" disabled={!projectSession || state === "preparing" || state === "planning"} onClick={() => void prepare()}>{state === "preparing" ? "Deriving exact scope…" : preparation ? "Prepare current revision again" : "Prepare exact run plan"}</button></div></section>
        {preparation && <>
          <section className="run-equation paper-panel" aria-label="Professional direct run arithmetic"><div><span>Worker-selected portfolios</span><strong>{preparation.selected_portfolio_count}</strong></div><b>×</b><div><span>Required scenarios</span><strong>{preparation.scenario_ids.length}</strong></div><b>=</b><div className="equation-total"><span>Direct Circuitscape jobs</span><strong>{preparation.direct_job_count.toLocaleString()}</strong></div><b>≤</b><div><span>Hard job cap</span><strong>{preparation.resource_policy.max_direct_jobs.toLocaleString()}</strong></div></section>
          <div className="split-grid"><section className="paper-panel plan-details"><h2>Search boundary</h2><dl><div><dt>Method</dt><dd>{preparation.search?.method ?? "Blocked"}</dd></div><div><dt>Finite universe</dt><dd>{preparation.finite_universe_size.toLocaleString()}</dd></div><div><dt>Portfolio capacity</dt><dd>{preparation.portfolio_capacity.toLocaleString()}</dd></div><div><dt>Seed</dt><dd className="mono">{preparation.search?.seed ?? "—"}</dd></div><div><dt>Physics cache</dt><dd>Cost excluded</dd></div></dl></section><section className="paper-panel plan-details"><h2>Signed engine boundary</h2><dl><div><dt>Available</dt><dd>{preparation.engine_available ? "Yes" : "No"}</dd></div><div><dt>Engine</dt><dd>{preparation.engine_manifest?.engine_name ?? "Unavailable"} {preparation.engine_manifest?.circuitscape_version ?? ""}</dd></div><div><dt>Manifest</dt><dd className="mono">{preparation.proposed_plan ? `${preparation.proposed_plan.engine_manifest_sha256.slice(0, 16)}…` : "—"}</dd></div><div><dt>Revision</dt><dd className="mono">{preparation.revision_id}</dd></div><div><dt>Comparison gate</dt><dd>Complete target + safeguard ensemble</dd></div></dl></section></div>
        </>}
        {preparation?.blockers.length ? <section className="paper-panel table-panel blocker-table"><div className="panel-heading"><div><span className="section-kicker">Must resolve</span><h2>Worker blockers</h2></div><span className="mono">{preparation.blockers.length}</span></div><div className="table-scroll"><table><thead><tr><th>Code</th><th>Object</th><th>Corrective action</th></tr></thead><tbody>{preparation.blockers.map((finding) => <tr key={`${finding.code}-${finding.object_reference}`}><td className="mono">{finding.code}</td><td className="mono">{finding.object_reference}</td><td>{finding.remediation}</td></tr>)}</tbody></table></div></section> : preparation?.proposed_plan && <section className="paper-panel plan-commit"><label className="confirmation-row"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /><span><strong>I reviewed the exact arithmetic, revision, scenarios, engine manifest, search method, seed, and resource cap.</strong> Commit the worker-returned proposal unchanged; no direct execution starts in this step.</span></label><button className="primary-button" type="button" disabled={!reviewed || Boolean(professionalAnalysis.plannedAnalysisId) || state === "planning"} onClick={() => void commitPlan()}>{state === "planning" ? "Committing immutable plan…" : professionalAnalysis.plannedAnalysisId ? "Plan committed" : "Commit exact worker plan"}</button></section>}
        {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
      </>
    );
  }
  if (guided && !fixture) return <><PageIntro kicker="Exact run arithmetic" title="Run plan" description="Run arithmetic is displayed only from the authoritative guided projection." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  const matrixJobs = fixture?.study.matrixJobCount ?? 0;
  const mapJobs = fixture?.study.mapJobCount ?? 0;
  return (
    <>
      <PageIntro kicker="Exact run arithmetic" title="One portfolio means one complete scenario ensemble." description="The run plan exposes model executions, resource assumptions, output retention, and eligibility before any solver starts." badge={<StatusPill state={guided ? "pass" : "fail"}>{guided ? `${matrixJobs + mapJobs} direct jobs` : "Blocked by model setup"}</StatusPill>} />
      <section className="run-equation paper-panel" aria-label="Ridgeway run arithmetic"><div><span>Fixed portfolios</span><strong>{guided ? fixture?.study.portfolioCount : "—"}</strong></div><b>×</b><div><span>Required scenarios</span><strong>{guided ? fixture?.study.scenarioCount : "—"}</strong></div><b>=</b><div className="equation-total"><span>Direct matrix jobs</span><strong>{guided ? matrixJobs.toLocaleString() : "—"}</strong></div><b>+</b><div><span>Teaching map jobs</span><strong>{guided ? mapJobs : "—"}</strong></div></section>
      <div className="split-grid">
        <section className="paper-panel plan-details"><h2>Execution policy</h2><dl><div><dt>Engine mode</dt><dd>Raster pairwise · resistance semantics</dd></div><div><dt>Precision</dt><dd>Double</dd></div><div><dt>Threads</dt><dd>Julia 1 · BLAS/OMP 1</dd></div><div><dt>Comparison gate</dt><dd>Complete target + safeguard ensemble with accepted QA</dd></div><div><dt>Model cache</dt><dd>Excludes cost records</dd></div></dl></section>
        <section className="paper-panel plan-details"><h2>Resource and cloud boundary</h2><dl><div><dt>Local estimate</dt><dd>{guided ? "Pinned synthetic bundle · exact size from manifest pending" : "Requires approved source grids"}</dd></div><div><dt>Cloud estimate</dt><dd>Every authorized artifact; no silent omission</dd></div><div><dt>W&B runs</dt><dd>One analysis Run; direct jobs as typed Table rows</dd></div><div><dt>Offline</dt><dd>Only after verified binding</dd></div></dl></section>
      </div>
    </>
  );
}

function DirectRunsScreen({ path, publicMode, guidedSession, onGuidedEvent, fixture, fixtureVerified, guidedProjectionError, projectSession, professionalAnalysis, onProfessionalAnalysis }: Pick<ScreensProps, "path" | "publicMode" | "guidedSession" | "onGuidedEvent" | "fixture" | "fixtureVerified" | "guidedProjectionError" | "projectSession" | "professionalAnalysis" | "onProfessionalAnalysis">) {
  const guided = path === "guided";
  const hostAvailable = hasNativeHost();
  const [rerunState, setRerunState] = useState<"idle" | "starting" | "started" | "working" | "error">("idle");
  const [rerunMessage, setRerunMessage] = useState("");
  const [guidedStatus, setGuidedStatus] = useState<ReturnType<typeof GuidedRerunStatusSchema.parse> | null>(null);
  const [professionalState, setProfessionalState] = useState<"idle" | "working" | "error">("idle");
  const [professionalMessage, setProfessionalMessage] = useState("");
  const analysisId = professionalAnalysis.plannedAnalysisId;
  const refreshProfessionalStatus = async () => {
    if (!projectSession || !analysisId) return;
    try {
      const [statusResponse, projectResponse] = await Promise.all([
        hostRequest<RpcPayload<unknown>>("connectivity.analysis.status", { project_handle: projectSession.projectHandle, analysis_id: analysisId }),
        hostRequest<RpcPayload<unknown>>("connectivity.project.get_state", { project_handle: projectSession.projectHandle }),
      ]);
      const status = AnalysisStatusResponseSchema.parse(rpcData(statusResponse));
      const projectState = ProfessionalProjectStateSchema.parse(rpcData(projectResponse));
      if (status.analysis_id !== analysisId) throw new Error("The analysis status belongs to a different immutable plan.");
      onProfessionalAnalysis({ ...professionalAnalysis, projectState, status });
      setProfessionalState("idle");
    } catch (error) { setProfessionalState("error"); setProfessionalMessage(hostErrorMessage(error, "The durable analysis status could not be refreshed.")); }
  };
  useEffect(() => {
    if (guided || !analysisId) return;
    void refreshProfessionalStatus();
  }, [analysisId, guided, projectSession?.projectHandle]);
  useEffect(() => {
    if (guided || !analysisId || professionalAnalysis.status?.state !== "RUNNING") return;
    const timer = window.setInterval(() => { void refreshProfessionalStatus(); }, 1500);
    return () => window.clearInterval(timer);
  }, [analysisId, guided, professionalAnalysis.status?.state, projectSession?.projectHandle]);
  const refreshGuidedStatus = async () => {
    if (!guidedSession) return;
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.guided.status", { session_id: guidedSession.sessionId, project_handle: guidedSession.projectHandle });
      const status = GuidedRerunStatusSchema.parse(rpcData(response));
      if (status.session_id !== guidedSession.sessionId) throw new Error("The rerun status belongs to a different guided session.");
      setGuidedStatus(status);
      setRerunState("started");
      setRerunMessage(status.state === "COMPLETED" ? "All 1,596 direct jobs completed. The worker retained the tolerance-based differential report." : `${status.phase} phase: ${status.completed_jobs.toLocaleString()} of ${status.total_jobs.toLocaleString()} direct jobs complete; ${status.cached_jobs.toLocaleString()} were exact cache hits.`);
    } catch (error) { setRerunState("error"); setRerunMessage(hostErrorMessage(error, "The authoritative guided rerun status could not be read.")); }
  };
  useEffect(() => {
    if (!guided || !guidedSession || guidedSession.initialRerunState === "VERIFIED_FIXTURE_REPLAY" || guidedSession.initialRerunState === "ENGINE_REQUIRED") return;
    void refreshGuidedStatus();
  }, [guided, guidedSession?.projectHandle, guidedSession?.sessionId]);
  useEffect(() => {
    if (!guided || !guidedSession || !guidedStatus || !["RUNNING", "PAUSING"].includes(guidedStatus.state)) return;
    const timer = window.setInterval(() => { void refreshGuidedStatus(); }, 1500);
    return () => window.clearInterval(timer);
  }, [guided, guidedSession?.sessionId, guidedStatus?.state]);
  const startProfessional = async () => {
    if (!projectSession || !analysisId) return;
    setProfessionalState("working"); setProfessionalMessage("");
    try {
      const result = await hostRequest<RpcPayload<unknown>>("connectivity.analysis.start", { project_handle: projectSession.projectHandle, analysis_id: analysisId });
      const started = AnalysisStateResponseSchema.parse(rpcData(result));
      if (started.analysis_id !== analysisId) throw new Error("The start receipt belongs to a different immutable plan.");
      setProfessionalMessage(`Analysis entered ${started.state}. Scientific results remain local-authoritative regardless of cloud state.`);
      await refreshProfessionalStatus();
    } catch (error) { setProfessionalState("error"); setProfessionalMessage(hostErrorMessage(error, "The immutable direct analysis could not be started.")); }
  };
  const controlProfessional = async (action: "pause" | "resume" | "cancel") => {
    if (!projectSession || !analysisId) return;
    setProfessionalState("working"); setProfessionalMessage("");
    try {
      const result = await hostRequest<RpcPayload<unknown>>(`connectivity.analysis.${action}`, { project_handle: projectSession.projectHandle, analysis_id: analysisId });
      const receipt = AnalysisStateResponseSchema.parse(rpcData(result));
      if (receipt.analysis_id !== analysisId) throw new Error("The analysis control receipt belongs to a different immutable plan.");
      setProfessionalMessage(`${action[0].toUpperCase()}${action.slice(1)} accepted; completed exact results remain immutable.`);
      await refreshProfessionalStatus();
    } catch (error) { setProfessionalState("error"); setProfessionalMessage(hostErrorMessage(error, `The analysis could not ${action}.`)); }
  };
  const startRerun = async () => {
    setRerunState("starting");
    setRerunMessage("");
    try {
      if (!guidedSession) throw new Error("The durable guided session is not ready yet.");
      onGuidedEvent({ checkpointId: "direct-runs", eventKind: "RERUN_REQUESTED" });
      const rerunResult = await hostRequest<RpcPayload<{ session_id: string; analysis_id: string; state: string }>>("connectivity.guided.rerun", {
        session_id: guidedSession.sessionId,
        project_handle: guidedSession.projectHandle,
      });
      const rerun = rpcData(rerunResult);
      setRerunState("started");
      setRerunMessage(`The worker created ${rerun.analysis_id} in ${rerun.state} state. Reading durable direct-job progress…`);
      await refreshGuidedStatus();
    } catch (reason) {
      setRerunState("error");
      setRerunMessage(hostErrorMessage(reason, "The durable rerun could not be started."));
    }
  };
  const controlGuided = async (action: "pause" | "resume" | "cancel") => {
    if (!guidedSession) return;
    setRerunState("working"); setRerunMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>(`connectivity.guided.${action}`, { session_id: guidedSession.sessionId, project_handle: guidedSession.projectHandle });
      const receipt = GuidedControlResponseSchema.parse(rpcData(response));
      if (receipt.session_id !== guidedSession.sessionId) throw new Error("The control receipt belongs to a different guided session.");
      setRerunMessage(`${action[0].toUpperCase()}${action.slice(1)} accepted in ${receipt.state}. Completed direct evidence remains immutable.`);
      await refreshGuidedStatus();
    } catch (error) { setRerunState("error"); setRerunMessage(hostErrorMessage(error, `The guided rerun could not ${action}.`)); }
  };
  if (!guided) {
    const status = professionalAnalysis.status;
    const plannedJobs = professionalAnalysis.preparation?.direct_job_count ?? status?.plan.direct_job_count ?? 0;
    const completed = status?.completed_run_count ?? 0;
    const percent = plannedJobs > 0 ? Math.min(100, Math.round((completed / plannedJobs) * 100)) : 0;
    const pendingRecovery = professionalAnalysis.projectState?.pending_recovery.analyses.find((analysis) => analysis.analysis_id === analysisId && analysis.recovery_required) ?? null;
    const canStart = Boolean(analysisId && !pendingRecovery && (!status || status.state === "READY"));
    const arithmetic = professionalAnalysis.preparation?.arithmetic
      ?? (status ? `${status.plan.portfolios.length} portfolios × ${status.plan.scenario_ids.length} scenarios = ${status.plan.direct_job_count} direct Circuitscape jobs` : "Authoritative run arithmetic is not yet available");
    return (
      <>
        <PageIntro kicker="Durable execution" title="Run the immutable portfolio × scenario plan." description="A portfolio enters comparison only when every required target and safeguard job succeeds with accepted QA." badge={<StatusPill state={status?.state === "COMPLETED" ? "pass" : analysisId ? "review" : "fail"}>{status?.state ?? (analysisId ? "READY" : "No runnable plan")}</StatusPill>} />
        {!analysisId ? <section className="paper-panel blocked-workspace"><Icon name="lock" /><div><h2>No direct job was queued.</h2><p>Prepare and commit the authoritative run plan first. This screen cannot synthesize missing portfolio, scenario, engine, or revision fields.</p></div></section> : <>
          {pendingRecovery && <section className="paper-panel recovery-banner" role="alert"><Icon name="warning" /><div><span className="section-kicker">Interrupted durable work</span><h2>This analysis has no live executor.</h2><p>{professionalAnalysis.projectState?.pending_recovery.next_action} {pendingRecovery.running_job_ids.length.toLocaleString()} abandoned RUNNING job{pendingRecovery.running_job_ids.length === 1 ? "" : "s"} will be requeued; no result is assumed from an incomplete work directory.</p></div><button className="primary-button" type="button" disabled={professionalState === "working"} onClick={() => void controlProfessional("resume")}><Icon name="play" /> Resume and recover jobs</button></section>}
          <section className="paper-panel run-state-panel"><div className="run-orbit" aria-hidden="true"><span>{completed.toLocaleString()}</span><small>of {plannedJobs.toLocaleString()} jobs</small><i style={{ transform: `rotate(${percent * 3.6}deg)` }} /></div><div className="run-state-copy"><span className="section-kicker">Durable state · <span className="mono">{analysisId}</span></span><h2>{status?.state === "COMPLETED" ? "Every directly evaluated portfolio ensemble is complete." : status?.state === "PAUSED" ? "New jobs are paused; completed exact results are retained." : status?.state === "RUNNING" ? "Direct Circuitscape jobs are executing in isolated work directories." : status?.state === "FAILED" ? "The analysis failed; inspect the exact worker error." : status?.state === "CANCELLED" ? "The analysis was cancelled; completed evidence remains retained." : "The exact plan is READY and has not started."}</h2><p>{arithmetic}. Comparison remains blocked until the entire selected target-and-safeguard ensemble completes.</p><div className="run-stats"><p><span>Complete direct runs</span><strong>{completed} / {plannedJobs}</strong></p><p><span>Portfolio summaries</span><strong>{status?.summary_count ?? 0}</strong></p><p><span>Evidence claims</span><strong>{status?.evidence_claim_count ?? 0}</strong></p></div></div><div className="run-action">{canStart && <button className="primary-button" type="button" disabled={professionalState === "working"} onClick={() => void startProfessional()}><Icon name="play" /> {professionalState === "working" ? "Starting…" : "Start direct runs"}</button>}{status?.state === "RUNNING" && !pendingRecovery && <><button className="secondary-button" type="button" disabled={professionalState === "working"} onClick={() => void controlProfessional("pause")}>Pause after active jobs</button><button className="secondary-button" type="button" disabled={professionalState === "working"} onClick={() => void controlProfessional("cancel")}>Cancel analysis</button></>}{status?.state === "PAUSED" && <><button className="primary-button" type="button" disabled={professionalState === "working"} onClick={() => void controlProfessional("resume")}><Icon name="play" /> Resume direct runs</button><button className="secondary-button" type="button" disabled={professionalState === "working"} onClick={() => void controlProfessional("cancel")}>Cancel analysis</button></>}<button className="text-action" type="button" disabled={professionalState === "working"} onClick={() => void refreshProfessionalStatus()}>Refresh authoritative status</button></div></section>
          {status && <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Direct-job ledger</span><h2>Repository state counts</h2></div><span className="mono">{percent}% complete</span></div><div className="table-scroll"><table><thead><tr><th>Direct-job state</th><th>Count</th><th>Meaning</th></tr></thead><tbody>{Object.entries(status.direct_job_counts).sort(([left], [right]) => left.localeCompare(right)).map(([jobState, count]) => <tr key={jobState}><td className="mono">{jobState}</td><td>{count}</td><td>{jobState === "SUCCEEDED" ? "Exact result retained; QA and ensemble gates still apply." : jobState === "QA_REJECTED" ? "Not eligible for comparison." : jobState === "FAILED" ? "No reportable connectivity value." : "Durable scheduler state."}</td></tr>)}</tbody></table></div>{status.error && <div className="blocking-banner"><Icon name="warning" /><p><strong>Worker error</strong> {JSON.stringify(status.error)}</p></div>}</section>}
        </>}
        {professionalMessage && <p className={`operation-message ${professionalState === "error" ? "error" : "success"}`} role={professionalState === "error" ? "alert" : "status"}>{professionalMessage}</p>}
      </>
    );
  }
  if (!fixture) return <><PageIntro kicker="Durable execution" title="Direct runs" description="Replay state is accepted only from the authoritative scientific worker." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  const replayJobCount = fixture.study.matrixJobCount + fixture.study.mapJobCount;
  const guidedCompleted = guidedStatus?.completed_jobs ?? replayJobCount;
  const guidedTotal = guidedStatus?.total_jobs ?? replayJobCount;
  const guidedPercent = guidedTotal ? Math.min(100, Math.round((guidedCompleted / guidedTotal) * 100)) : 0;
  const guidedActive = Boolean(guidedStatus && ["RUNNING", "PAUSING", "PAUSED"].includes(guidedStatus.state));
  return (
    <>
      <PageIntro kicker="Durable execution" title="Replay the evidence without pretending to rerun it." description="A portfolio enters comparison only when every required target and safeguard job succeeds with accepted QA." badge={<StatusPill state={fixtureVerified ? "pass" : "review"}>{fixtureVerified ? "Pinned replay loaded" : "Projection awaiting manifest"}</StatusPill>} />
      <ProjectionNotice fixture={fixture} verified={fixtureVerified} error={guidedProjectionError} />
      <section className="paper-panel run-state-panel">
        <div className="run-orbit" aria-hidden="true"><span>{fixtureVerified ? guidedCompleted.toLocaleString() : "0"}</span><small>of {fixtureVerified ? guidedTotal.toLocaleString() : "0"} jobs</small><i style={{ transform: `rotate(${guidedPercent * 3.6}deg)` }} /></div>
        <div className="run-state-copy"><span className="section-kicker">Current state {guidedStatus && <>· <span className="mono">{guidedStatus.state}</span></>}</span><h2>{guidedStatus?.state === "COMPLETED" ? "The full local rerun and differential are complete." : guidedStatus ? `${guidedStatus.phase} phase is ${guidedStatus.state.toLowerCase()}.` : fixtureVerified ? "Verified synthetic replay is available instantly." : "Direct outcomes are not published."}</h2><p>{guidedStatus ? "These are real durable worker counts, not animation or estimated progress. Comparison continues to use the pinned fixture until the differential passes declared tolerances." : fixtureVerified ? "The UI is reading a pinned, hash-verified worker projection. It has not pretended to start a rerun." : fixture.releaseBlocker}</p><div className="run-stats"><p><span>Matrix jobs</span><strong>{guidedStatus ? `${guidedStatus.matrix_completed} / 1,536` : fixtureVerified ? "1,536 / 1,536 replay" : "0 / 1,536"}</strong></p><p><span>Teaching map jobs</span><strong>{guidedStatus ? `${guidedStatus.map_completed} / 60` : fixtureVerified ? "60 retained" : "0 / 60"}</strong></p><p><span>Exact cache hits</span><strong>{guidedStatus?.cached_jobs ?? 0}</strong></p></div></div>
        <div className="run-action">{!guidedActive && guidedStatus?.state !== "COMPLETED" && <button className="primary-button" type="button" disabled={publicMode || !hostAvailable || !guidedSession || !guidedSession.canRerun || !fixtureVerified || rerunState === "starting" || rerunState === "working"} title={publicMode ? "The public replay never executes local models." : !hostAvailable ? "The packaged Circuitscape desktop host is unavailable." : !guidedSession ? "The durable guided session is still opening." : !guidedSession.canRerun ? "The packaged direct engine is unavailable." : !fixtureVerified ? "Attach and validate the authoritative fixture manifest first." : undefined} onClick={() => void startRerun()}><Icon name="play" /> {rerunState === "starting" ? "Starting…" : "Rerun all 1,596 jobs locally"}</button>}{guidedStatus?.state === "RUNNING" && <><button className="secondary-button" type="button" disabled={rerunState === "working"} onClick={() => void controlGuided("pause")}>Pause after active jobs</button><button className="secondary-button" type="button" disabled={rerunState === "working"} onClick={() => void controlGuided("cancel")}>Cancel rerun</button></>}{guidedStatus?.state === "PAUSED" && <><button className="primary-button" type="button" disabled={rerunState === "working"} onClick={() => void controlGuided("resume")}><Icon name="play" /> Resume rerun</button><button className="secondary-button" type="button" disabled={rerunState === "working"} onClick={() => void controlGuided("cancel")}>Cancel rerun</button></>} {guidedStatus && <button className="text-action" type="button" disabled={rerunState === "working"} onClick={() => void refreshGuidedStatus()}>Refresh durable status</button>}<small>{publicMode ? "Public replay is read-only." : !hostAvailable ? "Requires packaged Julia/Circuitscape host." : !guidedSession ? "Opening a local guided record…" : !guidedSession.canRerun ? "The signed engine bundle is unavailable." : "Writes real progress, cache, QA, differential report, and W&B outbox records."}</small>{rerunMessage && <span className={`run-message ${rerunState}`} role={rerunState === "error" ? "alert" : "status"}>{rerunMessage}</span>}</div>
      </section>
      {fixture.resourceObservability && <section className="paper-panel plan-details" aria-labelledby="guided-resource-heading"><span className="section-kicker">Environmental compute observability</span><h2 id="guided-resource-heading">Measured runtime and labelled energy proxy</h2><dl><div><dt>Instrumented jobs</dt><dd>{fixture.resourceObservability.jobsWithObservations.toLocaleString()} / {replayJobCount.toLocaleString()}</dd></div><div><dt>Direct solver time</dt><dd>{fixture.resourceObservability.directSolverWallSeconds.toFixed(1)} s</dd></div><div><dt>Energy status</dt><dd>{fixture.resourceObservability.energyStatus}</dd></div><div><dt>Energy estimate</dt><dd>{fixture.resourceObservability.estimatedEnergyKwh === null ? "Not measured" : `${fixture.resourceObservability.estimatedEnergyKwh.toFixed(6)} kWh proxy`}</dd></div><div><dt>Peak process memory</dt><dd>{fixture.resourceObservability.maximumObservedSingleProcessMemoryBytes === null ? "Not measured" : `${(fixture.resourceObservability.maximumObservedSingleProcessMemoryBytes / 1_048_576).toFixed(1)} MiB sampled lower bound`}</dd></div></dl><p className="table-note">{fixture.resourceObservability.limitation}</p></section>}
      {guidedStatus?.differential && <section className="paper-panel differential-report"><div><span className="section-kicker">Tolerance-based differential</span><h2>Local rerun compared with the pinned fixture</h2></div><pre>{JSON.stringify(guidedStatus.differential, null, 2)}</pre></section>}
      <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Retained evidence inventory</span><h2>Only worker-projected direct-job totals</h2></div></div><div className="table-scroll"><table><thead><tr><th>Evidence set</th><th>Scope</th><th>Direct jobs</th><th>Replay disposition</th></tr></thead><tbody><tr><td>Portfolio matrices</td><td>{fixture.study.portfolioCount} portfolios × {fixture.study.scenarioCount} scenarios</td><td>{fixture.study.matrixJobCount.toLocaleString()}</td><td><StatusPill state={fixtureVerified ? "pass" : "info"}>{fixtureVerified ? "Manifest verified" : "Unavailable"}</StatusPill></td></tr><tr><td>Teaching current maps</td><td>Baseline plus four teaching portfolios × {fixture.study.scenarioCount} scenarios</td><td>{fixture.study.mapJobCount.toLocaleString()}</td><td><StatusPill state={fixtureVerified ? "pass" : "info"}>{fixtureVerified ? "Manifest verified" : "Unavailable"}</StatusPill></td></tr></tbody></table></div><p className="table-note">No batch boundary or proposer refit is inferred by this interface. Search-trace claims require a retained worker record.</p></section>
    </>
  );
}

type PublishedPortfolio = RidgewayPortfolio & {
  id: string;
  costUsd: number;
  ruleStatus: "pass" | "fail" | "review";
  passCount: number;
  worstResistanceChangePct: number;
  worstScenarioId: string;
  roadMaskFlag: boolean;
};

function isPublishedPortfolio(portfolio: RidgewayPortfolio, requiredScenarioCount: number): portfolio is PublishedPortfolio {
  return Boolean(
    portfolio.id
    && portfolio.costUsd !== null
    && portfolio.passCount !== null
    && portfolio.worstResistanceChangePct !== null
    && portfolio.worstScenarioId
    && portfolio.roadMaskFlag !== null
    && portfolio.ruleStatus !== "unavailable"
    && portfolio.scenarioResults.length === requiredScenarioCount,
  );
}

function safeguardSummary(fixture: RidgewayFixture, portfolio: PublishedPortfolio): { state: "pass" | "review" | "fail"; label: string; detail: string } {
  const safeguardIds = new Set(fixture.scenarios.filter((item) => item.purpose === "safeguard").map((item) => item.id));
  const results = portfolio.scenarioResults.filter((item) => safeguardIds.has(item.scenarioId));
  const failed = results.filter((item) => item.ruleStatus === "fail").length;
  const review = results.filter((item) => item.ruleStatus === "review").length;
  if (!results.length) return { state: "fail", label: "Missing", detail: "no safeguard run evidence" };
  if (failed) return { state: "fail", label: `${failed} fail`, detail: `${failed} of ${results.length} safeguard scenarios fail` };
  if (review) return { state: "review", label: `${review} review`, detail: `${review} of ${results.length} safeguard scenarios require review` };
  return { state: "pass", label: `${results.length} pass`, detail: `all ${results.length} safeguard scenarios pass` };
}

function professionalEvidenceFor(
  summary: PortfolioDecisionSummary,
  claimId: string,
  title: string,
  value: string,
  units: string,
  reducer: string,
  scenarioId = "",
): EvidenceSelection {
  return {
    claimId,
    title,
    value,
    units,
    status: "verified",
    portfolioId: summary.portfolio_id,
    scenarioId,
    runId: summary.direct_run_ids?.[0] ?? "resolved-from-claim",
    reducer,
    assumption: "Interpret only within the selected reviewed scenario, resistance hypothesis, focal set, and direct engine settings.",
    source: "PROFESSIONAL_CLAIM",
  };
}

function RobustnessScreen({ path, selectedPortfolio, onPortfolio, onEvidence, fixture, fixtureVerified, guidedProjectionError, projectSession, professionalAnalysis, onProfessionalAnalysis }: Pick<ScreensProps, "path" | "selectedPortfolio" | "onPortfolio" | "onEvidence" | "fixture" | "fixtureVerified" | "guidedProjectionError" | "projectSession" | "professionalAnalysis" | "onProfessionalAnalysis">) {
  const [scrub, setScrub] = useState(50);
  const [selectedEvidenceScenarioId, setSelectedEvidenceScenarioId] = useState<string | null>(null);
  const [boardState, setBoardState] = useState<"idle" | "loading" | "error">("idle");
  const [boardMessage, setBoardMessage] = useState("");
  const loadBoard = async () => {
    if (!projectSession || !professionalAnalysis.plannedAnalysisId) return;
    setBoardState("loading"); setBoardMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.results.decision_board", { project_handle: projectSession.projectHandle, analysis_id: professionalAnalysis.plannedAnalysisId, portfolio_ids: [] });
      const board = DecisionBoardResponseSchema.parse(rpcData(response));
      if (board.analysis_id !== professionalAnalysis.plannedAnalysisId) throw new Error("The decision board belongs to a different immutable analysis.");
      const selectedPortfolioId = board.summaries.find((summary) => summary.disposition === "PASSES")?.portfolio_id ?? board.summaries[0]?.portfolio_id ?? null;
      onProfessionalAnalysis({ ...professionalAnalysis, decisionBoard: board, selectedPortfolioId, exportEligibility: null, exportRecord: null });
      setBoardState("idle"); setBoardMessage(`${board.summaries.length} complete portfolio summaries loaded from direct evidence.`);
    } catch (error) { setBoardState("error"); setBoardMessage(hostErrorMessage(error, "The exact decision board could not be loaded.")); }
  };
  if (path !== "guided") {
    const board = professionalAnalysis.decisionBoard;
    const selected = board?.summaries.find((summary) => summary.portfolio_id === professionalAnalysis.selectedPortfolioId) ?? board?.summaries[0] ?? null;
    const controllingRule = selected?.rule_dispositions.find((rule) =>
      Boolean(rule.evidence_claim_id)
      && rule.scenario_id === selected.controlling_scenario_id
      && rule.pair_id === selected.controlling_pair_id
      && typeof rule.observed_value === "number"
      && selected.worst_resistance_reduction_percent !== null
      && selected.worst_resistance_reduction_percent !== undefined
      && Math.abs(rule.observed_value - selected.worst_resistance_reduction_percent) <= 1e-9,
    ) ?? null;
    const statusComplete = professionalAnalysis.status?.state === "COMPLETED";
    const dispositionState = selected?.disposition === "PASSES" ? "pass" : selected?.disposition === "FAILS" || selected?.disposition === "INCOMPLETE" ? "fail" : "review";
    const plannedScenarioIds = professionalAnalysis.status?.plan.scenario_ids ?? [];
    const initialProfessionalScenarioId = selected?.controlling_scenario_id ?? plannedScenarioIds[0] ?? "";
    return (
      <>
        <PageIntro kicker="Robustness before ranking" title={board ? "Direct evidence, weakest case first." : "Load only complete portfolio ensembles."} description="No raw effective resistance is aggregated across species, focal sets, grids, or incompatible hypotheses. Every displayed disposition comes from the worker's direct evidence board." badge={<StatusPill state={board ? dispositionState : statusComplete ? "review" : "fail"}>{board ? `${board.summaries.length} complete portfolios` : statusComplete ? "Decision board available" : "Complete direct evidence required"}</StatusPill>} />
        {!board ? <section className="paper-panel blocked-workspace"><Icon name={statusComplete ? "evidence" : "lock"} /><div><h2>{statusComplete ? "The exact analysis is complete; load its decision board." : "No professional portfolio is comparable yet."}</h2><p>{statusComplete ? "The worker will return only summaries whose complete required target-and-safeguard ensembles and decision claims are retained." : "Ridgeway teaching results are not project results and are never copied into this project. Finish the immutable direct analysis first."}</p><button className="primary-button" type="button" disabled={!statusComplete || boardState === "loading"} onClick={() => void loadBoard()}>{boardState === "loading" ? "Resolving exact evidence…" : "Load exact decision board"}</button></div></section> : <>
          <section className="paper-panel acceptance-matrix"><div className="panel-heading"><div><span className="section-kicker">Acceptance matrix first</span><h2>Every configured rule, safeguard, and feasibility disposition</h2></div><span className="mono">{board.claim_language}</span></div><p className="matrix-interpretation"><strong>How to read reduction:</strong> each value is baseline-relative inside its own scenario. It is not observed animal movement, crossing use, collision reduction, population benefit, or an approved corridor.</p><p className="table-scroll-hint" aria-hidden="true">Scroll horizontally to compare controlling scenario, safeguard, cost, and next action →</p><div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable professional portfolio decision matrix"><table><thead><tr><th>Inspect</th><th>Portfolio</th><th>Rule disposition</th><th>Worst within-scenario reduction</th><th>Controlling scenario</th><th>Safeguard</th><th>Review mask</th><th>Cost</th></tr></thead><tbody>{board.summaries.map((summary) => <tr className={summary.portfolio_id === selected?.portfolio_id ? "is-selected" : ""} key={summary.portfolio_id}><td><input type="radio" name="professional-portfolio" checked={summary.portfolio_id === selected?.portfolio_id} onChange={() => onProfessionalAnalysis({ ...professionalAnalysis, selectedPortfolioId: summary.portfolio_id })} aria-label={`Inspect portfolio ${summary.portfolio_id}`} /></td><td className="mono">{summary.portfolio_id}</td><td><StatusPill state={summary.disposition === "PASSES" ? "pass" : summary.disposition === "SURVEY_FIRST" ? "review" : "fail"}>{summary.disposition.replace("_", " ")}</StatusPill></td><td>{summary.worst_resistance_reduction_percent == null ? "Not reportable" : `${summary.worst_resistance_reduction_percent.toFixed(2)}%`}</td><td className="mono">{summary.controlling_scenario_id ?? "—"}</td><td><StatusPill state={summary.safeguard_passed === true ? "pass" : summary.safeguard_passed === false ? "fail" : "review"}>{summary.safeguard_passed === true ? "Pass" : summary.safeguard_passed === false ? "Fail" : "Review"}</StatusPill></td><td><StatusPill state={summary.review_mask_triggered ? "review" : "pass"}>{summary.review_mask_triggered ? "Review" : "Below review threshold"}</StatusPill></td><td>{new Intl.NumberFormat("en-US", { style: "currency", currency: summary.currency, maximumFractionDigits: 0 }).format(summary.cost)}</td></tr>)}</tbody></table></div></section>
          {selected && <section className={`portfolio-verdict verdict-${dispositionState}`}><div><span className="section-kicker">Selected directly evaluated portfolio</span><h2 className="mono">{selected.portfolio_id}</h2><p>{selected.next_action}</p></div><dl><div><dt>Controlling scenario</dt><dd className="mono">{selected.controlling_scenario_id ?? "Not identified"}<small>within its own reviewed hypothesis</small>{controllingRule?.evidence_claim_id && <button className="table-button" type="button" onClick={() => onEvidence(professionalEvidenceFor(selected, controllingRule.evidence_claim_id!, `Controlling direct result for ${selected.portfolio_id}`, String(controllingRule.observed_value), "percent", controllingRule.explanation, controllingRule.scenario_id ?? ""))}>Trace controlling direct value</button>}</dd></div><div><dt>Controlling pair</dt><dd className="mono">{selected.controlling_pair_id ?? "Not identified"}<small>no cross-guild aggregation</small></dd></div><div><dt>Safeguard outcome</dt><dd>{selected.safeguard_passed === true ? "Pass" : selected.safeguard_passed === false ? "Fail" : "Review"}<small>{selected.review_mask_triggered ? "diagnostic mask review triggered" : "no configured mask trigger"}</small></dd></div><div><dt>Screening cost</dt><dd>{new Intl.NumberFormat("en-US", { style: "currency", currency: selected.currency, maximumFractionDigits: 0 }).format(selected.cost)}<small>cost remains outside the physics cache</small></dd></div></dl><div className="professional-next-action"><strong>{selected.disposition === "PASSES" ? "Meets the configured screening rules—not selected or approved." : selected.disposition === "SURVEY_FIRST" ? "Survey first; no intervention recommendation." : "Does not meet the configured screening rules."}</strong>{selected.unresolved_feasibility?.length ? <ul>{selected.unresolved_feasibility.map((item) => <li key={item}>{item}</li>)}</ul> : <span>No unresolved feasibility text was returned; field and engineering review remain required.</span>}{selected.disposition_evidence_claim_id && <button className="table-button" type="button" onClick={() => onEvidence(professionalEvidenceFor(selected, selected.disposition_evidence_claim_id!, `Screening disposition for ${selected.portfolio_id}`, selected.disposition, "screening disposition", "Complete portfolio disposition over retained rules, safeguards, feasibility records, and direct-run evidence."))}>Trace portfolio disposition</button>}</div></section>}
          {selected && projectSession && initialProfessionalScenarioId && <ProfessionalScenarioEvidence projectHandle={projectSession.projectHandle} analysisId={board.analysis_id} portfolioId={selected.portfolio_id} scenarioIds={plannedScenarioIds} initialScenarioId={initialProfessionalScenarioId} onEvidence={onEvidence} />}
          {selected && <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Rule-by-rule evidence</span><h2>Trace direct-computed values to solver runs</h2></div><span className="mono">{selected.rule_dispositions.length} rules</span></div><div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable rule-by-rule direct evidence"><table><thead><tr><th>Rule</th><th>Scenario</th><th>Pair</th><th>Direct value</th><th>Result</th><th>Explanation</th><th>Evidence</th></tr></thead><tbody>{selected.rule_dispositions.map((rule) => <tr key={`${rule.rule_id}-${rule.scenario_id ?? "all"}-${rule.pair_id ?? "all"}`}><td className="mono">{rule.rule_id}</td><td className="mono">{rule.scenario_id ?? "portfolio"}</td><td className="mono">{rule.pair_id ?? "—"}</td><td>{rule.observed_value == null ? "—" : String(rule.observed_value)}</td><td><StatusPill state={rule.passed ? "pass" : "fail"}>{rule.passed ? "Pass" : "Fail"}</StatusPill></td><td>{rule.explanation}</td><td>{rule.evidence_claim_id ? <button className="table-button" type="button" onClick={() => onEvidence(professionalEvidenceFor(selected, rule.evidence_claim_id!, `Direct evidence for ${rule.rule_id}`, rule.observed_value == null ? "Resolved by worker" : String(rule.observed_value), "Resolved from claim", rule.explanation, rule.scenario_id ?? selected.controlling_scenario_id ?? ""))}>Trace claim</button> : <span>Not numeric evidence</span>}</td></tr>)}</tbody></table></div></section>}
        </>}
        {boardMessage && <p className={`operation-message ${boardState === "error" ? "error" : "success"}`} role={boardState === "error" ? "alert" : "status"}>{boardMessage}</p>}
      </>
    );
  }
  if (!fixture) return <><PageIntro kicker="Decision-first comparison" title="Robustness Board" description="The desktop accepts connectivity outcomes only from the authoritative worker projection." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  const publishedPortfolios = fixture.portfolios.filter((item): item is PublishedPortfolio => isPublishedPortfolio(item, fixture.scenarios.length));
  const portfolio = publishedPortfolios.find((item) => item.projectionKey === selectedPortfolio?.projectionKey);
  if (!fixtureVerified || !portfolio) {
    return (
      <>
        <PageIntro kicker="Decision-first comparison" title="Robustness Board" description="Outcomes remain unpublished until the complete pinned direct Circuitscape evidence package passes QA and manifest verification." badge={<StatusPill state="review">Engine run required</StatusPill>} />
        <ProjectionNotice fixture={fixture} verified={fixtureVerified} error={guidedProjectionError} />
        <section className="paper-panel unpublished-board">
          <div className="unpublished-mark"><Icon name="lock" /></div>
          <div><span className="section-kicker">Fail-closed evidence boundary</span><h2>No portfolio ID, rank, cost outcome, or connectivity value is available yet.</h2><p>{fixture.releaseBlocker}</p></div>
          {selectedPortfolio && <button className="secondary-button" type="button" onClick={() => onEvidence(evidenceFor(fixture, selectedPortfolio, false))}><Icon name="evidence" /> Inspect evidence boundary</button>}
        </section>
        <section className="paper-panel table-panel"><div className="panel-heading"><div><span className="section-kicker">Teaching roles selected after direct runs</span><h2>Required decision-story slots</h2></div></div><div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable unpublished Ridgeway teaching roles"><table><thead><tr><th>Evidence role</th><th>Portfolio ID</th><th>Required outcomes</th><th>State</th></tr></thead><tbody>{fixture.portfolios.map((item) => <tr key={item.projectionKey}><td><strong>{item.name}</strong></td><td>Not selected</td><td>{item.optionSummary}</td><td><StatusPill state="review">Direct evidence required</StatusPill></td></tr>)}</tbody></table></div></section>
        <CorridorTransect verified={false} />
      </>
    );
  }
  const controlling = portfolio.scenarioResults.find((result) => result.scenarioId === portfolio.worstScenarioId) ?? portfolio.scenarioResults[0];
  const scenario = scenarioFor(fixture, controlling);
  const evidenceResult = portfolio.scenarioResults.find((result) => result.scenarioId === selectedEvidenceScenarioId) ?? controlling;
  const evidenceScenario = scenarioFor(fixture, evidenceResult);
  const selectedSafeguard = safeguardSummary(fixture, portfolio);
  const selectedRankRange = hypothesisRankRange(portfolio);
  return (
    <>
      <PageIntro kicker="Decision-first comparison" title="Robustness Board" description="Start with every rule, the worst hypothesis, safeguard outcomes, cost, and feasibility gap. Maps support the decision; they do not lead it." badge={<StatusPill state={portfolio.ruleStatus}>{portfolio.ruleStatus === "pass" ? "All teaching rules pass" : portfolio.ruleStatus === "review" ? "Review required" : "Does not pass"}</StatusPill>} />
      <ProjectionNotice fixture={fixture} verified={fixtureVerified} error={guidedProjectionError} />
      <section className="paper-panel acceptance-matrix" aria-labelledby="acceptance-heading"><div className="panel-heading"><div><span className="section-kicker">Acceptance matrix first</span><h2 id="acceptance-heading">Four direct-run teaching roles</h2></div><span className="mono">Internal precision · rounded display</span></div><p className="matrix-interpretation"><strong>How to read change:</strong> a negative percentage means lower modeled effective resistance than that same scenario’s baseline. Hypothesis rank uses the pinned direct-study metric shown below; it is not an ecological value or an animal-use prediction. Cost is a versioned synthetic screening basis, not a Circuitscape result.</p><p className="table-scroll-hint" aria-hidden="true">Scroll horizontally to compare rank sensitivity, safeguard, cost, and disposition →</p><div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable portfolio acceptance matrix"><table><thead><tr><th scope="col">Inspect</th><th scope="col">Portfolio</th><th scope="col">Direct ensemble</th><th scope="col">Worst resistance change</th><th scope="col">Hypothesis rank range</th><th scope="col">Safeguard</th><th scope="col">Road mask</th><th scope="col">Screening cost basis</th><th scope="col">Disposition</th></tr></thead><tbody>{publishedPortfolios.map((item) => { const safeguard = safeguardSummary(fixture, item); const rankRange = hypothesisRankRange(item); return <tr className={item.id === portfolio.id ? "is-selected" : ""} key={item.id}><td><input type="radio" name="portfolio" checked={item.id === portfolio.id} onChange={() => onPortfolio(item)} aria-label={`Inspect ${item.name}`} /></td><td><button className="table-title-button" type="button" onClick={() => onPortfolio(item)}><strong>{item.name}</strong><span className="mono">{item.id}</span></button></td><td>{item.scenarioResults.length}/{item.requiredCount} direct</td><td className={item.worstResistanceChangePct > 0 ? "value-adverse" : "value-good"}>{item.worstResistanceChangePct > 0 ? "+" : ""}{item.worstResistanceChangePct.toFixed(1)}%</td><td>{rankRange ? <><strong>#{rankRange.best.rank} → #{rankRange.worst.rank}</strong><span className="table-subline">{rankRange.highestScore.toFixed(1)}% → {rankRange.lowestScore.toFixed(1)}% score</span></> : "Direct rank evidence unavailable"}</td><td><StatusPill state={safeguard.state}>{safeguard.label}</StatusPill><span className="sr-only">{safeguard.detail}</span></td><td><StatusPill state={item.roadMaskFlag ? "review" : "pass"}>{item.roadMaskFlag ? "Review" : "Below review threshold"}</StatusPill></td><td>{formatUsd(item.costUsd)}</td><td><StatusPill state={item.ruleStatus}>{guidedDispositionLabel(item.role, item.ruleStatus)}</StatusPill></td></tr>; })}</tbody></table></div><p className="control-requirement"><strong>Computed rank basis:</strong> {fixture.study.hypothesisRankingMetric}</p></section>

      <section className={`portfolio-verdict verdict-${portfolio.ruleStatus}`}>
        <div><span className="section-kicker">Selected evidence role · {portfolio.id}</span><h2>{portfolio.name}</h2><p>{portfolio.nextAction}</p></div>
        <dl><div><dt>Controlling scenario</dt><dd>{scenario.id}<small>{scenario.hypothesis}</small></dd></div><div><dt>Worst within-scenario change</dt><dd>{controlling.resistanceChangePct > 0 ? "+" : ""}{controlling.resistanceChangePct.toFixed(1)}%<small>effective resistance</small></dd></div><div><dt>Computed hypothesis rank</dt><dd>{selectedRankRange ? `#${selectedRankRange.best.rank} → #${selectedRankRange.worst.rank}` : "Unavailable"}<small>{selectedRankRange ? `${selectedRankRange.best.hypothesisId} best · ${selectedRankRange.worst.hypothesisId} worst` : "requires complete direct rank evidence"}</small></dd></div><div><dt>Safeguard outcome</dt><dd>{selectedSafeguard.label}<small>{selectedSafeguard.detail}</small></dd></div><div><dt>Screening cost</dt><dd>{formatUsd(portfolio.costUsd)}<small>{portfolio.costUsd <= fixture.decisionBrief.budgetUsd ? "within fixture cap" : "over fixture cap"} · versioned synthetic cost basis, not a Circuitscape value</small></dd></div></dl>
        <div className="verdict-evidence-actions">
          <button className="secondary-button" type="button" onClick={() => onEvidence(evidenceFor(fixture, portfolio, true, controlling))}><Icon name="evidence" /> Trace controlling resistance</button>
          <button className="secondary-button" type="button" onClick={() => onEvidence(mapEvidenceFor(fixture, portfolio, true, controlling))}><Icon name="map" /> Trace controlling road-mask diagnostic</button>
          <small>Each trace opens only the named evidence chain. Cost is resolved from the fixture cost basis in the evidence package.</small>
        </div>
      </section>

      <CurrentRasterScrubber fixture={fixture} portfolio={portfolio} result={evidenceResult} scrub={scrub} onScrub={setScrub} onTrace={() => onEvidence(mapEvidenceFor(fixture, portfolio, true, evidenceResult))} />

      <div className="comparison-grid">
          <section className="paper-panel table-panel focal-matrix"><div className="panel-heading focal-matrix-heading"><div><span className="section-kicker">Synchronized focal-pair matrix</span><h2>{evidenceScenario.guild} · {evidenceScenario.hypothesis}</h2></div><label><span>Scenario shown in matrix and raster</span><select value={evidenceResult.scenarioId} onChange={(event) => setSelectedEvidenceScenarioId(event.target.value)}>{portfolio.scenarioResults.map((result) => { const item = scenarioFor(fixture, result); return <option key={result.scenarioId} value={result.scenarioId}>{item.guild} · {item.hypothesis} · {result.resistanceChangePct.toFixed(2)}%</option>; })}</select></label></div><p className="matrix-interpretation"><strong>Pair rule:</strong> {evidenceScenario.purpose === "safeguard" ? "No required safeguard pair may increase modeled effective resistance." : "Every required target pair must reduce modeled effective resistance by at least 3%."} Values compare only this scenario with its own direct baseline. Raw effective resistance is shown in the reviewed model's resistance units and is never aggregated across scenarios.</p><div className="table-scroll" tabIndex={0} role="region" aria-label={`Scrollable focal-pair evidence for ${evidenceResult.scenarioId}`}><table><thead><tr><th>Required focal pair</th><th>Baseline effective resistance (model units)</th><th>Selected effective resistance (model units)</th><th>Baseline-relative change</th><th>Rule</th><th>Exact direct identity</th><th>Evidence</th></tr></thead><tbody>{evidenceResult.pairResults.map((pair) => <tr key={pair.pairId}><td className="mono">{pair.pairId}</td><td className="mono">{pair.baselineEffectiveResistance.toPrecision(7)}</td><td className="mono">{pair.portfolioEffectiveResistance.toPrecision(7)}</td><td className={pair.baselineRelativeChangePct > 0 ? "value-adverse" : "value-good"}>{pair.baselineRelativeChangePct > 0 ? "+" : ""}{pair.baselineRelativeChangePct.toFixed(2)}%</td><td><StatusPill state={pair.passed ? "pass" : "fail"}>{pair.passed ? "Pass" : "Fail"}</StatusPill></td><td><details className="pair-lineage"><summary>Runs and matrix hashes</summary><dl><div><dt>Baseline run</dt><dd className="mono">{pair.baselineRunId}</dd></div><div><dt>Baseline matrix</dt><dd className="mono">sha256:{pair.baselineMatrixSha256}</dd></div><div><dt>Selected run</dt><dd className="mono">{pair.portfolioRunId}</dd></div><div><dt>Selected matrix</dt><dd className="mono">sha256:{pair.portfolioMatrixSha256}</dd></div></dl></details></td><td><button className="table-button" type="button" onClick={() => onEvidence(pairEvidenceFor(fixture, portfolio, true, evidenceResult, pair))}>Trace focal pair</button></td></tr>)}</tbody></table></div><div className="focal-matrix-footer"><span><strong>Scenario reducer</strong>{evidenceResult.resistanceChangePct.toFixed(2)}% weakest required pair</span><span><strong>Road-mask diagnostic</strong>{evidenceResult.reviewMaskChangePct.toFixed(2)}% redistribution · not animal use</span><button className="table-button" type="button" onClick={() => onEvidence(evidenceFor(fixture, portfolio, true, evidenceResult))}>Trace scenario reducer</button></div></section>
        <section className="paper-panel next-action-panel"><span className="section-kicker">Professional next action</span><h2>Model evidence narrows the field. Field evidence decides what is feasible.</h2><ol><li><span>01</span><div><strong>Ecological reconnaissance</strong><p>Confirm habitat condition, barriers, seasonal use, and unmodeled mortality pathways.</p></div></li><li><span>02</span><div><strong>Engineering screening</strong><p>Review structure, drainage, utilities, traffic, fencing limits, access, and constructability.</p></div></li><li><span>03</span><div><strong>Governance and ownership</strong><p>Resolve land rights, cultural review, maintenance responsibility, and data permissions.</p></div></li><li><span>04</span><div><strong>Revise the model</strong><p>Record new evidence as a semantic revision; do not overwrite the decision record.</p></div></li></ol></section>
      </div>
    </>
  );
}

async function downloadSyntheticStory(fixture: RidgewayFixture, verified: boolean) {
  if (!verified) throw new Error("Verified direct Ridgeway evidence is required before export.");
  const serialized = await serializeRedactedRidgewayDecisionStory(fixture);
  const url = URL.createObjectURL(new Blob([serialized], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "ridgeway-crossing-static-decision-story.json";
  link.click();
  URL.revokeObjectURL(url);
}

function EvidencePackageScreen({ path, publicMode, onEvidence, selectedPortfolio, fixture, fixtureVerified, guidedProjectionError, projectSession, professionalAnalysis, onProfessionalAnalysis }: Pick<ScreensProps, "path" | "publicMode" | "onEvidence" | "selectedPortfolio" | "fixture" | "fixtureVerified" | "guidedProjectionError" | "projectSession" | "professionalAnalysis" | "onProfessionalAnalysis">) {
  const guided = path === "guided";
  const [exportId] = useState(() => `export_${(crypto.randomUUID ? crypto.randomUUID() : String(Date.now())).replaceAll("-", "")}`);
  const [exportState, setExportState] = useState<"idle" | "validating" | "creating" | "copying" | "error">("idle");
  const [exportMessage, setExportMessage] = useState("");
  const [syncState, setSyncState] = useState<"idle" | "syncing" | "error">("idle");
  const [syncMessage, setSyncMessage] = useState("Synchronization has not been requested in this session.");
  const [syncReport, setSyncReport] = useState<ReturnType<typeof ObservabilitySyncResponseSchema.parse> | null>(null);
  const [uploadPreview, setUploadPreview] = useState<ReturnType<typeof CloudUploadPreviewSchema.parse> | null>(null);
  const selectedProfessionalPortfolio = professionalAnalysis.selectedPortfolioId;
  const canRequestProfessionalExport = Boolean(projectSession && professionalAnalysis.status?.state === "COMPLETED" && professionalAnalysis.decisionBoard && selectedProfessionalPortfolio);
  const exportParams = () => {
    if (!projectSession || !professionalAnalysis.plannedAnalysisId || !selectedProfessionalPortfolio) throw new Error("A complete selected direct portfolio is required.");
    return { project_handle: projectSession.projectHandle, analysis_id: professionalAnalysis.plannedAnalysisId, portfolio_ids: [selectedProfessionalPortfolio] };
  };
  const validateProfessionalExport = async () => {
    setExportState("validating"); setExportMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.export.validate", exportParams());
      const eligibility = ExportEligibilityResponseSchema.parse(rpcData(response)).eligibility;
      onProfessionalAnalysis({ ...professionalAnalysis, exportEligibility: eligibility, exportRecord: null });
      setExportState("idle"); setExportMessage(eligibility.eligible ? "Repository evidence permits a professional package for the selected portfolio." : "Export remains blocked by repository evidence. No package was created.");
    } catch (error) { setExportState("error"); setExportMessage(hostErrorMessage(error, "Export eligibility could not be resolved.")); }
  };
  const createProfessionalExport = async () => {
    if (!professionalAnalysis.exportEligibility?.eligible) return;
    setExportState("creating"); setExportMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.export.create", { ...exportParams(), export_id: exportId });
      const created = ExportCreateResponseSchema.parse(rpcData(response));
      if (created.export_id !== exportId || created.project_relative_directory !== `exports/${exportId}` || created.manifest.export_id !== exportId) throw new Error("The committed export receipt differs from the requested authoritative package.");
      onProfessionalAnalysis({ ...professionalAnalysis, exportRecord: { exportId, projectRelativeDirectory: created.project_relative_directory, manifest: created.manifest, copyReceipt: null } });
      setExportState("idle"); setExportMessage("Authoritative evidence package committed inside the project with its W&B outbox record. Choose Save a copy to select an external destination natively.");
    } catch (error) { setExportState("error"); setExportMessage(hostErrorMessage(error, "The authoritative evidence package could not be created.")); }
  };
  const copyProfessionalExport = async () => {
    if (!projectSession || !professionalAnalysis.exportRecord) return;
    setExportState("copying"); setExportMessage("");
    try {
      const receipt = await saveExportCopy(projectSession.projectHandle, professionalAnalysis.exportRecord.exportId);
      if (!receipt) { setExportState("idle"); setExportMessage("Save a copy was cancelled. The authoritative project package is unchanged."); return; }
      if (receipt.exportId !== professionalAnalysis.exportRecord.exportId || receipt.copied !== true) throw new Error("The native copy receipt differs from the authoritative export identity.");
      onProfessionalAnalysis({ ...professionalAnalysis, exportRecord: { ...professionalAnalysis.exportRecord, copyReceipt: receipt } });
      setExportState("idle"); setExportMessage(`Saved ${receipt.fileCount} hash-verified files (${formatBytes(receipt.byteSize)}) in ${receipt.destinationLabel}. No local path entered React or RPC logs.`);
    } catch (error) { setExportState("error"); setExportMessage(hostErrorMessage(error, "The native host could not save a verified copy.")); }
  };
  const synchronizeEvidence = async () => {
    if (!projectSession) return;
    setSyncState("syncing"); setSyncMessage("");
    try {
      const previewResponse = await hostRequest<RpcPayload<unknown>>("observability.preview_upload", {
        project_handle: projectSession.projectHandle,
      });
      const preview = CloudUploadPreviewSchema.parse(rpcData(previewResponse));
      setUploadPreview(preview);
      if (!preview.allowed) {
        setSyncState("error");
        setSyncMessage(`Upload is blocked before any provider call: ${preview.blockers.join("; ") || "authoritative storage estimate unavailable"}.`);
        return;
      }
      const online = navigator.onLine;
      const response = await hostRequest<RpcPayload<unknown>>("observability.sync", {
        project_handle: projectSession.projectHandle,
        online,
      });
      const report = ObservabilitySyncResponseSchema.parse(rpcData(response));
      setSyncReport(report);
      setSyncState("idle");
      setSyncMessage(report.receipt.queued_offline
        ? "The verified binding remains authoritative and every upload stays queued locally; no provider call was attempted."
        : `W&B accepted ${report.receipt.synced} envelope(s); ${report.receipt.failed} will retry and ${report.receipt.blocked} remain policy-blocked.`);
    } catch (error) {
      setSyncState("error");
      setSyncMessage(hostErrorMessage(error, "W&B synchronization failed; local science and queued evidence are unchanged."));
    }
  };
  const downloadGuidedStory = async () => {
    if (!fixture) return;
    setExportState("creating"); setExportMessage("");
    try {
      await downloadSyntheticStory(fixture, fixtureVerified);
      setExportState("idle");
      setExportMessage("The synthetic story passed its local integrity check and the evidence-linked JSON download was requested.");
    } catch (error) {
      setExportState("error");
      setExportMessage(hostErrorMessage(error, "The synthetic story did not pass its local integrity check, so no file was downloaded."));
    }
  };
  if (!guided) {
    const eligibility = professionalAnalysis.exportEligibility;
    const record = professionalAnalysis.exportRecord;
    const blockers = [...(eligibility?.fitness_blockers ?? []), ...(eligibility?.authorization_blockers ?? []), ...(eligibility?.qa_blockers ?? [])];
    return (
      <>
        <PageIntro kicker="Evidence package" title="Export eligibility comes from repository evidence." description="The interface cannot override missing direct runs, QA, fitness, authorizations, cost basis, or cloud lineage. A copy is saved only by the native host from a committed package." badge={<StatusPill state={record ? "pass" : eligibility?.eligible ? "review" : "fail"}>{record ? "Authoritative package created" : eligibility?.eligible ? "Eligible · not created" : "Export blocked"}</StatusPill>} />
        <section className="paper-panel eligibility-panel"><div className="eligibility-seal"><Icon name={record ? "check" : eligibility?.eligible ? "document" : "lock"} /></div><div><span className="section-kicker">Selected professional portfolio</span><h2>{selectedProfessionalPortfolio ?? "Choose a portfolio on the Robustness Board."}</h2><p>{record ? `Committed at project-relative ${record.projectRelativeDirectory}. External paths remain host-owned.` : eligibility?.eligible ? "Every repository eligibility gate passed. Create the authoritative package before saving any copy." : "Required: a completed exact ensemble, selected decision summary, accepted QA, resolved fitness and authorization, cost basis, and connected lineage."}</p></div>{record ? <button className="primary-button" type="button" disabled={exportState === "copying"} onClick={() => void copyProfessionalExport()}><Icon name="download" /> {exportState === "copying" ? "Verifying and copying…" : record.copyReceipt ? "Save another verified copy…" : "Save a copy…"}</button> : eligibility?.eligible ? <button className="primary-button" type="button" disabled={exportState === "creating"} onClick={() => void createProfessionalExport()}>{exportState === "creating" ? "Committing package…" : "Create evidence package"}</button> : <button className="primary-button" type="button" disabled={!canRequestProfessionalExport || exportState === "validating"} onClick={() => void validateProfessionalExport()}>{exportState === "validating" ? "Checking repository evidence…" : "Check export eligibility"}</button>}</section>
        {eligibility && <div className="split-grid"><section className="paper-panel plan-details"><h2>Eligibility gates</h2><dl><div><dt>Complete ensemble</dt><dd>{eligibility.complete_ensemble ? "Pass" : "Blocked"}</dd></div><div><dt>Direct evidence only</dt><dd>{eligibility.exact_direct_evidence_only ? "Pass" : "Blocked"}</dd></div><div><dt>Professional package</dt><dd>{eligibility.professional ? "Yes" : "No"}</dd></div><div><dt>Cloud lineage</dt><dd>{eligibility.sync_state}</dd></div></dl></section><section className="paper-panel plan-details"><h2>Package boundary</h2><p>{eligibility.required_notice}</p>{blockers.length ? <ul>{blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul> : <p><strong>No repository blocker was returned.</strong> Field, ecological, engineering, cultural, ownership, and permitting feasibility review remain outside this export gate.</p>}</section></div>}
        {record && <section className="paper-panel package-list"><div className="panel-heading"><div><span className="section-kicker">Committed contents</span><h2>Auditable local record</h2></div><span className="mono">{record.exportId}</span></div><ul><li><Icon name="document" /><div><strong>Decision board and redacted static story</strong><span>Shareable story remains explicitly screening-only.</span></div></li><li><Icon name="grid" /><div><strong>Direct-run and evidence claim records</strong><span>Every reportable connectivity value resolves to exact Circuitscape evidence.</span></div></li><li><Icon name="shield" /><div><strong>Manifest, hashes, limitations, and authorization boundary</strong><span>The native copy is rehashed and rejects symlinks or changed files.</span></div></li></ul>{record.copyReceipt && <p className="operation-message success" role="status">Last native copy: {record.copyReceipt.destinationLabel} · {record.copyReceipt.fileCount} files · {formatBytes(record.copyReceipt.byteSize)}</p>}</section>}
        <section className="paper-panel cloud-sync-panel" aria-labelledby="cloud-sync-heading"><div className="panel-heading"><div><span className="section-kicker">W&B lineage mirror</span><h2 id="cloud-sync-heading">{syncReport?.binding.state ?? projectSession?.bindingState ?? "Binding required"}</h2></div><StatusPill state={syncReport?.receipt.blocked || syncState === "error" ? "fail" : syncReport?.receipt.queued_offline || syncReport?.receipt.failed ? "review" : syncReport ? "pass" : "info"}>{syncState === "syncing" ? "Checking and synchronizing" : syncReport?.receipt.queued_offline ? "Queued offline" : syncReport ? `${syncReport.receipt.synced} synchronized` : uploadPreview ? uploadPreview.current_raster_estimate_status.replaceAll("_", " ") : "Not checked"}</StatusPill></div><p>{syncMessage}</p>{uploadPreview && <dl aria-label="Authoritative upload estimate"><div><dt>Reviewed input projection</dt><dd>{uploadPreview.input_bytes === undefined ? "Unavailable" : formatBytes(uploadPreview.input_bytes)}</dd></div><div><dt>Current-raster evidence</dt><dd>{uploadPreview.estimated_current_raster_bytes === null ? "Unknown — synchronization blocked" : formatBytes(uploadPreview.estimated_current_raster_bytes)}</dd></div><div><dt>Retained / expected maps</dt><dd>{uploadPreview.retained_current_raster_count} / {uploadPreview.expected_current_raster_count ?? "unknown"}</dd></div><div><dt>Total authorized evidence</dt><dd>{uploadPreview.estimated_total_bytes === null ? "Unknown — synchronization blocked" : formatBytes(uploadPreview.estimated_total_bytes)}</dd></div></dl>}{uploadPreview && <p className="control-requirement"><strong>{uploadPreview.current_raster_estimate_status.replaceAll("_", " ")}:</strong> {uploadPreview.current_raster_estimate_basis}</p>}{syncReport && <dl><div><dt>Claimed</dt><dd>{syncReport.receipt.claimed}</dd></div><div><dt>Synchronized</dt><dd>{syncReport.receipt.synced}</dd></div><div><dt>Retryable failures</dt><dd>{syncReport.receipt.failed}</dd></div><div><dt>Policy blocked</dt><dd>{syncReport.receipt.blocked}</dd></div></dl>}<button className="secondary-button" type="button" disabled={!projectSession || syncState === "syncing"} onClick={() => void synchronizeEvidence()}><Icon name="cloud" /> {syncState === "syncing" ? "Checking estimate and synchronizing…" : syncState === "error" || syncReport?.receipt.failed ? "Recheck upload policy" : "Preview and synchronize authorized evidence"}</button><small>The worker derives map count from immutable run plans and grids, then replaces estimates with exact retained bytes. Cloud failure cannot change direct results, QA, portfolio order, or local export contents.</small></section>
        {exportMessage && <p className={`operation-message ${exportState === "error" ? "error" : "success"}`} role={exportState === "error" ? "alert" : "status"}>{exportMessage}</p>}
      </>
    );
  }
  if (!fixture || !selectedPortfolio) return <><PageIntro kicker="Decision record" title="Evidence and feasibility package" description="Synthetic exports remain blocked until the worker-backed projection is verified." badge={<StatusPill state="review">Evidence unavailable</StatusPill>} /><GuidedProjectionBoundary error={guidedProjectionError} /></>;
  return (
    <>
      <PageIntro kicker="Decision record" title="Evidence and feasibility package" description="Export eligibility comes from repository evidence. A label, user preference, or assistant cannot override a missing run, QA finding, authorization, or limitation." badge={<StatusPill state={fixtureVerified ? "pass" : "review"}>{fixtureVerified ? "Synthetic export eligible" : "Projection export blocked"}</StatusPill>} />
      <section className="paper-panel eligibility-panel"><div className="eligibility-seal"><Icon name="document" /></div><div><span className="section-kicker">Export eligibility</span><h2>{fixtureVerified ? "A redacted synthetic decision story can be downloaded." : "The evidence manifest has not been verified."}</h2><p>The file contains no professional or sensitive data and carries the permanent ecological limitations. It does not claim approval, design readiness, animal use, or collision reduction.</p></div><button className="primary-button" type="button" disabled={!fixtureVerified || exportState === "creating"} onClick={() => { void downloadGuidedStory(); }}><Icon name="download" /> {exportState === "creating" ? "Checking story…" : "Download static story"}</button></section>
      {exportMessage && <p className={`operation-message ${exportState === "error" ? "error" : "success"}`} role={exportState === "error" ? "alert" : "status"}>{exportMessage}</p>}
      <div className="evidence-package-grid">
        <section className="paper-panel package-list"><div className="panel-heading"><div><span className="section-kicker">Package contents</span><h2>What travels with the decision</h2></div></div><ul><li><Icon name="document" /><div><strong>Decision brief and rule matrix</strong><span>Question, thresholds, reducer definitions, cost basis, and non-goals</span></div></li><li><Icon name="grid" /><div><strong>Direct result projection</strong><span>Portfolio × scenario rows, QA dispositions, map-summary values, and IDs</span></div></li><li><Icon name="layers" /><div><strong>Model revision and semantic diff</strong><span>Source assets, approved transforms, resistance/focal meanings, and action rasters</span></div></li><li><Icon name="shield" /><div><strong>Fitness, safeguards, and authorizations</strong><span>Unresolved assumptions remain visible</span></div></li></ul></section>
        <section className="paper-panel lineage-panel"><span className="section-kicker">Selected claim</span><h2>{selectedPortfolio.name}</h2><p>{selectedPortfolio.nextAction}</p><button className="secondary-button" type="button" onClick={() => onEvidence(evidenceFor(fixture, selectedPortfolio, fixtureVerified))}><Icon name="evidence" /> Inspect local evidence chain</button><button className="secondary-button" type="button" disabled title={publicMode ? "The public replay makes no cloud calls." : "No confirmed synchronized W&B artifact reference has been returned by the worker."}><Icon name="external" /> Open W&B artifact lineage</button><small>{publicMode ? "No login and no W&B connection in public replay." : "Cloud links supplement the local Evidence Rail; they never replace it."}</small></section>
      </div>
      <section className="permanent-export-warning"><Icon name="warning" /><p>{fixture.disclaimer}</p></section>
    </>
  );
}

export function ScreenContent(props: ScreensProps) {
  switch (props.step) {
    case "overview": return <OverviewScreen path={props.path} publicMode={props.publicMode} localPreview={props.localPreview} onNavigate={props.onNavigate} projectSession={props.projectSession} onProjectReady={props.onProjectReady} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} />;
    case "study-context": return <StudyContextScreen planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} onProjectReady={props.onProjectReady} onNavigate={props.onNavigate} />;
    case "source-assets": return <SourceAssetsScreen projectSession={props.projectSession} planning={props.planning} professionalAssets={props.professionalAssets} onAssetPersisted={props.onAssetPersisted} />;
    case "grid-alignment": return <GridAlignmentScreen planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalGridApprovals={props.professionalGridApprovals} onGridApproved={props.onGridApproved} />;
    case "hypotheses": return <HypothesesScreen planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} />;
    case "focal-areas": return <FocalAreasScreen planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} />;
    case "model-fitness": return <ModelFitnessScreen path={props.path} projectSession={props.projectSession} professionalAnalysis={props.professionalAnalysis} onEvidence={props.onEvidence} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} />;
    case "scenarios": return <ScenariosScreen path={props.path} onEvidence={props.onEvidence} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} />;
    case "interventions": return <InterventionsScreen path={props.path} planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} />;
    case "safeguards": return <SafeguardsScreen path={props.path} planning={props.planning} onPlanning={props.onPlanning} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} />;
    case "rules-costs": return <RulesCostsScreen path={props.path} planning={props.planning} onPlanning={props.onPlanning} fixture={props.fixture} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalAssets={props.professionalAssets} professionalRecords={props.professionalRecords} onProfessionalRecords={props.onProfessionalRecords} observabilityDeployment={props.observabilityDeployment} />;
    case "run-plan": return <RunPlanScreen path={props.path} fixture={props.fixture} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalAnalysis={props.professionalAnalysis} onProfessionalAnalysis={props.onProfessionalAnalysis} />;
    case "direct-runs": return <DirectRunsScreen path={props.path} publicMode={props.publicMode} guidedSession={props.guidedSession} onGuidedEvent={props.onGuidedEvent} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalAnalysis={props.professionalAnalysis} onProfessionalAnalysis={props.onProfessionalAnalysis} />;
    case "robustness": return <RobustnessScreen path={props.path} selectedPortfolio={props.selectedPortfolio} onPortfolio={props.onPortfolio} onEvidence={props.onEvidence} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalAnalysis={props.professionalAnalysis} onProfessionalAnalysis={props.onProfessionalAnalysis} />;
    case "evidence-package": return <EvidencePackageScreen path={props.path} publicMode={props.publicMode} selectedPortfolio={props.selectedPortfolio} onEvidence={props.onEvidence} fixture={props.fixture} fixtureVerified={props.fixtureVerified} guidedProjectionError={props.guidedProjectionError} projectSession={props.projectSession} professionalAnalysis={props.professionalAnalysis} onProfessionalAnalysis={props.onProfessionalAnalysis} />;
  }
}
