import axe from "axe-core";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { EcoApp } from "./App";
import { Workspace } from "./components/Workspace";
import { publicRidgewayFixture } from "./data/publicRidgeway";
import { RIDGEWAY_HYPOTHESIS_RANKING_METRIC } from "./data/ridgeway";
import { assetRequirements, buildAssetAddParams, buildAssetInspectParams, createAssetDraft, hydrateProfessionalProject } from "./lib/professional";
import type { HostBridge, OpaqueFileHandle, ProfessionalProjectState, RasterInspection, RidgewayFixture } from "./types";

function payload<T>(method: string, data: T) {
  return { api_version: "1.0", method, data, data_sha256: "a".repeat(64) };
}

function projectState(
  projectId: string,
  title: string,
  mode: "IMPORTED" | "PLANNING_DRAFT" = "PLANNING_DRAFT",
  overrides: Partial<ProfessionalProjectState> = {},
): ProfessionalProjectState {
  const revision = {
    revision_id: `revision_${projectId}`,
    project_id: projectId,
    created_at: "2026-08-03T12:00:00Z",
    created_by: "Agency ecologist",
    reason: "Create authoritative test project.",
    model_sha256: "1".repeat(64),
  };
  const decisionBrief = {
    brief_id: `brief_${projectId}`,
    title,
    decision_question: "Which reviewed concept should advance to feasibility review?",
    decision_owner: "Agency ecologist",
    intended_use: "Planning comparison only; not animal movement, collision, design, or approval evidence. Study context: County-scale generalized extent.",
    target_guilds: ["Anonymous forest-associated focal guild"],
    safeguard_guilds: [],
    budget_amount: 250000,
    currency: "USD",
    cost_vintage: "2026",
    non_goals: ["Observed animal movement"],
    required_next_action: "Field and engineering feasibility review.",
    conceptual: mode === "PLANNING_DRAFT",
  };
  const fitness = {
    passport_id: `fitness_${projectId}`,
    revision_id: revision.revision_id,
    intended_use: decisionBrief.intended_use,
    dimensions: [],
    unresolved_assumptions: ["Test fixture assumption remains unresolved."],
    attestations: [],
    reviewed_by: null,
    reviewed_at: null,
  };
  const resourcePolicy = {
    max_input_bytes: 2_147_483_648,
    max_materialized_bytes: 8_589_934_592,
    max_current_raster_bytes: 4_294_967_296,
    max_direct_jobs: 5_000,
    max_wall_seconds_per_job: 3_600,
    require_explicit_oversize_approval: true,
  };
  const base: ProfessionalProjectState = {
    project: { project_id: projectId, title, mode },
    active_revision: revision,
    effective_bundle: {
      schema_version: "1.0",
      project_id: projectId,
      title,
      revision,
      decision_brief: decisionBrief,
      assets: [],
      resistance_hypotheses: [],
      focal_area_sets: [],
      scenarios: [],
      intervention_sites: [],
      intervention_options: [],
      cost_basis: null,
      safeguards: [],
      review_masks: [],
      decision_rules: [],
      fitness,
      resource_policy: resourcePolicy,
    },
    fitness,
    resource_policy: resourcePolicy,
    saved_records: {
      asset: [], hypothesis: [], focal_area_set: [], scenario: [], intervention_site: [],
      intervention_option: [], safeguard: [], review_mask: [], decision_rule: [],
      grid_alignment_plan: [], grid_alignment_approval: [], semantic_diff: [], revision: [],
    },
    analyses: [],
    pending_recovery: { required: false, analyses: [], next_action: "No interrupted analysis requires recovery." },
    sync_status: { state: "CONNECTED" },
    validation: { status: "BLOCKED" },
    engine: { available: false, manifest_sha256: null, engine_name: null, circuitscape_version: null },
  };
  return { ...base, ...overrides };
}

function directAnalysisPlan(analysisId: string) {
  const scenarioIds = ["scenario_target_reviewed", "scenario_safeguard_reviewed"];
  const portfolios = ["portfolio_baseline", "portfolio_reviewed"].map((portfolioId) => ({
    portfolio_id: portfolioId,
    selections: { site_one: portfolioId === "portfolio_baseline" ? "no_action" : "reviewed_action" },
    canonical_option_ids: [portfolioId === "portfolio_baseline" ? "no_action" : "reviewed_action"],
    contains_survey_first: false,
  }));
  return {
    analysis_id: analysisId,
    revision_id: "revision_saved_analysis",
    scenario_ids: scenarioIds,
    portfolios,
    baseline_portfolio_id: "portfolio_baseline",
    direct_job_count: portfolios.length * scenarioIds.length,
    solver_job_cap: 100,
    search_method: "ENUMERATION" as const,
    seed: 17,
    current_maps_for_portfolio_ids: [],
    engine_manifest_sha256: "e".repeat(64),
  };
}

function verifiedGuidedProjection(): RidgewayFixture {
  const testSha = (value: number) => value.toString(16).padStart(64, "0").slice(-64);
  const manifestSha256 = "d".repeat(64);
  const fixture = structuredClone(publicRidgewayFixture);
  fixture.schemaVersion = "ecocorridor-replay/v1.2";
  fixture.baselinePortfolioId = `portfolio_${"0".repeat(20)}`;
  fixture.manifestSha256 = manifestSha256;
  fixture.manifestVersion = "reopen-test-v1";
  fixture.evidenceState = "VERIFIED_DIRECT_EVIDENCE";
  fixture.releaseReplayEligible = true;
  fixture.releaseBlocker = "";
  fixture.study.hypothesisRankingMetric = RIDGEWAY_HYPOTHESIS_RANKING_METRIC;
  fixture.resourceObservability = {
    jobsWithObservations: 1596,
    jobsWithoutObservations: 0,
    directSolverWallSeconds: 159.6,
    estimatedEnergyKwh: 0.012,
    estimatedEnergyLowerKwh: 0.008,
    estimatedEnergyUpperKwh: 0.016,
    maximumObservedSingleProcessMemoryBytes: 134_217_728,
    peakMemoryIsSampledLowerBound: true,
    peakMemoryStatus: "MEASURED",
    energyStatus: "ESTIMATED",
    proposerWallTimeStatus: "NOT_APPLICABLE_FIXED_ENUMERATION",
    limitation: "Synthetic test instrumentation; energy is a labelled estimate, not a carbon claim.",
  };
  fixture.reviewMask = {
    maskId: "ridgeway-road-review-mask",
    sha256: testSha(10),
    width: 4,
    height: 3,
    cells: [0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0],
    interpretation: "Diagnostic review mask; not observed or predicted animal use.",
  };
  fixture.currentRasters = fixture.scenarios.map((scenario, scenarioIndex) => ({
    runId: `map_${fixture.baselinePortfolioId}_${scenario.id}`,
    portfolioId: fixture.baselinePortfolioId!,
    scenarioId: scenario.id,
    sha256: testSha(100 + scenarioIndex),
    width: 4,
    height: 3,
    declaredNodata: -9999,
    nodataTopologySha256: testSha(200 + scenarioIndex),
    minimumCurrent: 0,
    maximumCurrent: 12 + scenarioIndex,
    cells: [0, 1, 2, 3, 4, null, 6, 7, 8, 9, 10, 12 + scenarioIndex],
  }));
  fixture.portfolios = fixture.portfolios.map((portfolio, index) => {
    const id = `portfolio_${String(index + 1).padStart(20, "0")}`;
    const scenarioResults = fixture.scenarios.map((scenario, scenarioIndex) => {
      const resistanceChangePct = -(index + 4) - scenarioIndex / 100;
      const baselineRunId = `matrix_${fixture.baselinePortfolioId}_${scenario.id}`;
      const portfolioRunId = `matrix_${id}_${scenario.id}`;
      const baselineMapRunId = `map_${fixture.baselinePortfolioId}_${scenario.id}`;
      const portfolioMapRunId = `map_${id}_${scenario.id}`;
      const pairResults = Array.from({ length: scenario.includedPairs }, (_, pairIndex) => {
        const baselineEffectiveResistance = 20 + scenarioIndex + pairIndex;
        const baselineRelativeChangePct = resistanceChangePct - pairIndex / 10;
        return {
          pairId: `${scenario.id}_pair_${pairIndex + 1}`,
          baselineEffectiveResistance,
          portfolioEffectiveResistance: baselineEffectiveResistance * (1 + baselineRelativeChangePct / 100),
          baselineRelativeChangePct,
          passed: true,
          claimId: `ECO-PAIR-${id}-${scenario.id}-${scenario.id}_pair_${pairIndex + 1}`,
          baselineRunId,
          portfolioRunId,
          baselineMatrixSha256: testSha(1_000 + scenarioIndex),
          portfolioMatrixSha256: testSha(2_000 + index * 100 + scenarioIndex),
        };
      });
      fixture.currentRasters.push({
        runId: portfolioMapRunId,
        portfolioId: id,
        scenarioId: scenario.id,
        sha256: testSha(3_000 + index * 100 + scenarioIndex),
        width: 4,
        height: 3,
        declaredNodata: -9999,
        nodataTopologySha256: testSha(200 + scenarioIndex),
        minimumCurrent: 0,
        maximumCurrent: 13 + index + scenarioIndex,
        cells: [0, 1, 2, 3, 4, null, 6 + index, 7, 8, 9 + index, 10, 13 + index + scenarioIndex],
      });
      return {
        scenarioId: scenario.id,
        resistanceChangePct,
        ruleStatus: "pass" as const,
        reviewMaskChangePct: index + scenarioIndex / 10,
        runId: portfolioRunId,
        baselineMapRunId,
        portfolioMapRunId,
        pairResults,
      };
    });
    return {
      ...portfolio,
      id,
      costUsd: 75_000 + index * 50_000,
      ruleStatus: "pass" as const,
      passCount: fixture.scenarios.length,
      worstResistanceChangePct: scenarioResults[0].resistanceChangePct,
      worstScenarioId: scenarioResults[0].scenarioId,
      roadMaskFlag: index === 0,
      nextAction: "Candidate to carry forward for ecological, field, and engineering feasibility review.",
      optionSummary: "Direct-run synthetic teaching result under documented assumptions.",
      hypothesisRanks: {
        h1_low_barrier: { rank: index + 1, score: 4 - index },
        h2_moderate_barrier: { rank: index + 2, score: 3.5 - index },
        h3_high_barrier: { rank: index + 3, score: 3 - index },
        h4_future_pressure: { rank: index + 4, score: 2.5 - index },
      },
      scenarioResults,
    };
  });
  return fixture;
}

async function createPlanningRecord(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole("button", { name: "Study context" })[0]);
  await user.type(screen.getByLabelText("Project name *"), "Route 14 screen");
  await user.type(screen.getByLabelText("Generalized study area *"), "County-scale generalized extent");
  await user.type(screen.getByLabelText("Decision question *"), "Which concept should advance to feasibility review?");
  await user.type(screen.getByLabelText("Intended use and scope boundary *"), "Planning comparison only; not animal movement, collision, design, or approval evidence.");
  await user.type(screen.getByLabelText("Target guild or life stage *"), "Anonymous forest-associated focal guild");
  await user.type(screen.getByLabelText("Responsible ecological reviewer *"), "Agency ecologist");
  await user.click(screen.getByRole("button", { name: "Create planning record" }));
  await screen.findByText(/Authoritative local draft created/i);
}

async function completeAssetAuthorization(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.type(screen.getByLabelText("Asset title *"), title);
  await user.selectOptions(screen.getByLabelText("Reviewer-assigned role *"), "DOCUMENT");
  await user.selectOptions(screen.getByLabelText("Media type *"), "application/pdf");
  await user.type(screen.getByLabelText("Source and attribution *"), "Agency reviewed technical memorandum");
  await user.type(screen.getByLabelText("Vintage or observation date *"), "2026-06");
  await user.type(screen.getByLabelText("License or use terms *"), "Agency internal use terms");
  await user.type(screen.getByLabelText("Owner or custodian *"), "State wildlife agency");
  await user.type(screen.getByLabelText("Intended use in this project *"), "Support reviewer interpretation of the planning context only.");
  await user.selectOptions(screen.getByLabelText("Sensitivity *"), "INTERNAL");
  await user.selectOptions(screen.getByLabelText("Raw/generalized cloud permission *"), "PROHIBITED");
  await user.selectOptions(screen.getByLabelText("Export permission *"), "BLOCK");
  await user.type(screen.getByLabelText("Retention policy *"), "Retain through project close; custodian approves disposal.");
  await user.type(screen.getByLabelText("Spatial redaction or explicit none *"), "No coordinates in this document; no spatial derivative required.");
  await user.click(screen.getByLabelText("Local project only"));
  fireEvent.change(screen.getByLabelText("Reviewed at *"), { target: { value: "2026-08-03T09:30" } });
  await user.click(screen.getByLabelText(/I confirm that a responsible reviewer assigned this asset role/i));
}

async function completeRasterAuthorization(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.type(screen.getByLabelText("Asset title *"), title);
  await user.selectOptions(screen.getByLabelText("Reviewer-assigned role *"), "RESISTANCE_RASTER");
  await user.selectOptions(screen.getByLabelText("Raster format *"), "ESRI_ASCII");
  await user.type(screen.getByLabelText("Declared CRS *"), "EPSG:5070");
  await user.type(screen.getByLabelText("Declared cell area (m²) *"), "900");
  await user.type(screen.getByLabelText("Source and attribution *"), "Reviewed agency resistance workshop output");
  await user.type(screen.getByLabelText("Vintage or observation date *"), "2026-06");
  await user.type(screen.getByLabelText("License or use terms *"), "Agency internal use terms");
  await user.type(screen.getByLabelText("Owner or custodian *"), "State wildlife agency");
  await user.type(screen.getByLabelText("Intended use in this project *"), "Reviewed resistance hypothesis input for planning comparison only.");
  await user.selectOptions(screen.getByLabelText("Sensitivity *"), "INTERNAL");
  await user.selectOptions(screen.getByLabelText("Raw/generalized cloud permission *"), "PROHIBITED");
  await user.selectOptions(screen.getByLabelText("Export permission *"), "BLOCK");
  await user.type(screen.getByLabelText("Retention policy *"), "Retain through project close; custodian approves disposal.");
  await user.type(screen.getByLabelText("Spatial redaction or explicit none *"), "Local-only reviewed raster; no cloud derivative authorized.");
  await user.click(screen.getByLabelText("Local project only"));
  fireEvent.change(screen.getByLabelText("Reviewed at *"), { target: { value: "2026-08-03T09:30" } });
  await user.click(screen.getByLabelText(/I confirm that a responsible reviewer assigned this asset role/i));
}

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
});

describe("EcoCorridor desktop entry paths", () => {
  it("opens Ridgeway without a W&B gate and withholds outcomes when the worker is absent", async () => {
    const user = userEvent.setup();
    render(<EcoApp />);

    await user.click(screen.getByRole("button", { name: "Open synthetic replay" }));

    expect(screen.getByRole("heading", { name: "Opening Ridgeway Crossing" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /connect/i })).not.toBeInTheDocument();
    expect(screen.getByText("Synthetic local replay")).toBeInTheDocument();
    expect(screen.getByText(/No Ridgeway outcome is displayed from bundled UI data/i)).toBeInTheDocument();
  });

  it("places professional routes behind fail-closed project verification", async () => {
    const user = userEvent.setup();
    render(<EcoApp />);

    await user.click(screen.getByRole("button", { name: "Assemble planning model" }));
    const dialog = screen.getByRole("dialog", { name: /connect the evidence destination/i });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /W&B Cloud/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Verify Restricted project/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /local workflow preview/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Professional workflows remain unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/does not assume that Dedicated or Self-Managed is sufficient by itself/i)).toBeInTheDocument();
  });

  it("keeps the public replay local and no-login", () => {
    const request = vi.fn();
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: vi.fn(async () => ({ handle: "must_not_be_created" })),
    };
    render(<EcoApp mode="public-replay" publicFixture={publicRidgewayFixture} />);
    expect(screen.getByText(/No login · no cloud/i)).toBeInTheDocument();
    expect(screen.getByText(/Public synthetic decision story/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open reviewed model/i })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it("shows public baseline-relative claims only from the independently pinned direct package", async () => {
    const user = userEvent.setup();
    render(<EcoApp mode="public-replay" publicFixture={publicRidgewayFixture} />);
    await user.click(screen.getByRole("button", { name: "Robustness board" }));

    expect(screen.getByRole("heading", { name: "Four direct-run teaching roles" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Trace controlling resistance" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Scrollable portfolio acceptance matrix" })).toHaveAttribute("tabindex", "0");
    expect(screen.getAllByText(/^portfolio_[a-f0-9]{20}$/).length).toBeGreaterThanOrEqual(4);
    expect(publicRidgewayFixture.baselinePortfolioId).toMatch(/^portfolio_[a-f0-9]{20}$/);
    expect(publicRidgewayFixture.evidenceState).toBe("VERIFIED_DIRECT_EVIDENCE");
    expect(publicRidgewayFixture.releaseReplayEligible).toBe(true);
  });

  it("shows retained public map diagnostics with the claim boundary attached", async () => {
    const user = userEvent.setup();
    render(<EcoApp mode="public-replay" publicFixture={publicRidgewayFixture} />);
    await user.click(screen.getByRole("button", { name: "Robustness board" }));
    await user.click(screen.getByRole("button", { name: "Trace controlling road-mask diagnostic" }));
    const evidence = screen.getByRole("complementary", { name: "Evidence details" });
    expect(within(evidence).getByText("Pinned public manifest", { exact: true })).toBeInTheDocument();
    expect(within(evidence).getByRole("heading", { name: "Resolved source chain" })).toBeInTheDocument();
    expect(within(evidence).getByText(/not observed animal movement/i)).toBeInTheDocument();
    expect(JSON.stringify(publicRidgewayFixture)).toContain("map_portfolio_");
  });

  it("records ordered, opaque-handle guided checkpoints only in the desktop", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.guided.open") {
        return payload(method, {
          session_id: "guided_ridgeway_v1",
          projection: publicRidgewayFixture,
          can_rerun: false,
          state: "VERIFIED_FIXTURE_REPLAY",
          evidence_manifest_sha256: publicRidgewayFixture.manifestSha256,
        });
      }
      if (method === "connectivity.guided.checkpoint") {
        return payload(method, { recorded: true });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: vi.fn(async () => ({ handle: "project_guided_opaque" })),
    };

    render(<Workspace mode="desktop" path="guided" localPreview={false} preflight={null} />);
    expect(await screen.findByText("Guided record active")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Model fitness" })[0]);

    await waitFor(() => {
      expect(request.mock.calls.filter(([method]) => method === "connectivity.guided.checkpoint")).toHaveLength(3);
    });
    const opened = request.mock.calls.find(([method]) => method === "connectivity.guided.open")?.[1] as Record<string, unknown>;
    const checkpoints = request.mock.calls
      .filter(([method]) => method === "connectivity.guided.checkpoint")
      .map(([, params]) => params as Record<string, unknown>);
    expect(opened).toEqual({ project_handle: "project_guided_opaque" });
    expect(checkpoints.map((item) => item.sequence_number)).toEqual([1, 2, 3]);
    expect(checkpoints.map((item) => item.event_kind)).toEqual(["ENTERED", "COMPLETED", "ENTERED"]);
    expect(checkpoints.map((item) => item.event_id)).toEqual([
      "guided_ridgeway_v1-0001-entered-overview",
      "guided_ridgeway_v1-0002-completed-overview",
      "guided_ridgeway_v1-0003-entered-model-fitness",
    ]);
    expect(JSON.stringify(request.mock.calls)).not.toContain("project_directory");
  });

  it("uses exact worker projection identities and resolves desktop evidence through the worker", async () => {
    const user = userEvent.setup();
    const workerProjection = verifiedGuidedProjection();
    const portfolio = workerProjection.portfolios.find((item) => item.role === "carry-forward")!;
    const result = portfolio.scenarioResults.find((item) => item.scenarioId === portfolio.worstScenarioId)!;
    const claimId = `ECO-CLM-${portfolio.id}-${result.scenarioId}`;
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.guided.open") {
        return payload(method, {
          session_id: "guided_ridgeway_v1",
          projection: workerProjection,
          can_rerun: true,
          state: "VERIFIED_FIXTURE_REPLAY",
          evidence_manifest_sha256: workerProjection.manifestSha256,
        });
      }
      if (method === "connectivity.guided.checkpoint") return payload(method, { recorded: true });
      if (method === "connectivity.evidence.resolve") {
        const requestedClaimId = (_params as { claim_id: string }).claim_id;
        const pairMatch = portfolio.scenarioResults
          .flatMap((scenarioResult) => scenarioResult.pairResults.map((pair) => ({ pair, scenarioResult })))
          .find(({ pair }) => pair.claimId === requestedClaimId);
        const requestedResult = pairMatch?.scenarioResult ?? result;
        const requestedBaselineRunId = `matrix_${workerProjection.baselinePortfolioId}_${requestedResult.scenarioId}`;
        const requestedValue = pairMatch?.pair.baselineRelativeChangePct ?? requestedResult.resistanceChangePct;
        return payload(method, {
          claim: {
            claim_id: requestedClaimId,
            portfolio_id: portfolio.id,
            scenario_id: requestedResult.scenarioId,
            pair_id: pairMatch?.pair.pairId ?? "pair_core_a_core_c",
            value: requestedValue,
            units: "% baseline-relative effective resistance",
            reducer: "(portfolio effective resistance − baseline effective resistance) ÷ baseline effective resistance × 100",
            direct_run_ids: [requestedResult.runId, requestedBaselineRunId],
          },
          direct_runs: [
            { direct_run_id: requestedResult.runId },
            { direct_run_id: requestedBaselineRunId },
          ],
          artifact_records: [],
          engine_manifest_sha256: "1".repeat(64),
          workflow_manifest_sha256: "2".repeat(64),
          evidence_index_sha256: "3".repeat(64),
          qa: "ACCEPTED_DIRECT_CIRCUITSCAPE",
          limitations: [workerProjection.disclaimer],
        });
      }
      if (method === "assistant.status") {
        return payload(method, {
          configured: true,
          provider: { base_url: "https://provider.example/v1", model: "eco-evidence-model", timeout_seconds: 60 },
          project_id: "ridgeway-guided",
          wandb_external_project_id: "wandb-project-1",
          trace_required: true,
        });
      }
      if (method === "assistant.explain") {
        const params = _params as { call_id: string; evidence_ids: string[] };
        return payload(method, {
          answer: {
            answer_id: "answer-verified-1",
            call_id: params.call_id,
            project_id: "ridgeway-guided",
            text: "This claim is shown because it is the directly evaluated controlling scenario for the selected screening portfolio.",
            evidence_ids: params.evidence_ids,
            numeric_claims: [],
            limitations: ["Interpret only within the documented resistance hypothesis and scenario."],
            provider_call_id: "provider-call-1",
            provider_model: "eco-evidence-model",
            trace_id: "weave-trace-1",
            association: {
              association_id: "association-guided-1",
              project_id: "ridgeway-guided",
              wandb_external_project_id: "wandb-project-1",
              wandb_run_id: "ecocorridor-connectivity-analysis-ridgeway",
              wandb_run_kind: "connectivity_analysis",
              run_subject_id: "ridgeway-analysis",
              analysis_id: "ridgeway-analysis",
            },
            evidence_authorization: {
              decision_id: `evidence_auth_${"a".repeat(24)}`,
              project_id_sha256: "1".repeat(64),
              analysis_id_sha256: "2".repeat(64),
              model_revision_id_sha256: "3".repeat(64),
              destination: "SELF_MANAGED",
              external_project_id_sha256: "4".repeat(64),
              scope_evidence_sha256: "5".repeat(64),
              snapshot_bundle_sha256: "6".repeat(64),
              current_authorizations_sha256: "7".repeat(64),
              selected_evidence_sha256: "8".repeat(64),
              authorized_asset_count: 1,
              generalized_derivative_count: 0,
              context_schema_version: "ecocorridor-assistant-context/v2",
            },
            deterministic_validation: "PASSED",
            validator_version: "ecocorridor-assistant-validator-v1",
            trace_readback_verified: true,
            read_only: true,
          },
        });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: async () => ({ handle: "project_guided_exact" }),
    };

    render(<Workspace mode="desktop" path="guided" localPreview={false} preflight={null} />);
    expect(await screen.findByText("Verified synthetic fixture replay")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Robustness board" }));
    expect((await screen.findAllByText(portfolio.id!)).length).toBeGreaterThan(0);
    expect(screen.getByRole("region", { name: "Scrollable portfolio acceptance matrix" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("region", { name: `Scrollable focal-pair evidence for ${result.scenarioId}` })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("slider", { name: "Move the baseline / selected divider" })).toHaveAttribute("aria-valuetext", "50% baseline width · 50% selected-portfolio width");
    expect(screen.getByText("Direct current-raster comparison")).toBeInTheDocument();
    expect(screen.getAllByText(result.portfolioMapRunId, { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getAllByText(result.baselineMapRunId, { exact: false }).length).toBeGreaterThan(0);
    await user.click(screen.getByText("Inspect all 12 exact raster cells"));
    expect(screen.getByRole("region", { name: `Scrollable exact current-raster cells for ${result.scenarioId}` })).toHaveAttribute("tabindex", "0");
    const synchronizedScenario = portfolio.scenarioResults.find((item) => item.scenarioId !== result.scenarioId)!;
    await user.selectOptions(screen.getByLabelText("Scenario shown in matrix and raster"), synchronizedScenario.scenarioId);
    const synchronizedRegion = screen.getByRole("region", { name: `Scrollable focal-pair evidence for ${synchronizedScenario.scenarioId}` });
    expect(synchronizedRegion).toHaveAttribute("tabindex", "0");
    expect(screen.getAllByText(synchronizedScenario.portfolioMapRunId, { exact: false }).length).toBeGreaterThan(0);
    const synchronizedPair = synchronizedScenario.pairResults[0];
    await user.click(within(synchronizedRegion).getAllByRole("button", { name: "Trace focal pair" })[0]);
    expect(await screen.findByText("Worker evidence resolved")).toBeInTheDocument();
    const pairResolveCall = request.mock.calls.find(([method, params]) => method === "connectivity.evidence.resolve" && (params as Record<string, unknown>).claim_id === synchronizedPair.claimId);
    expect(pairResolveCall?.[1]).toEqual({ project_handle: "project_guided_exact", claim_id: synchronizedPair.claimId });
    expect(screen.getAllByText(synchronizedPair.portfolioRunId, { exact: false }).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Close evidence" }));
    expect(screen.getByText(/a negative percentage means lower modeled effective resistance/i)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Safeguard" })).toBeInTheDocument();
    expect(screen.getByText("Safeguard outcome")).toBeInTheDocument();
    expect(screen.getByText("4 pass", { selector: "dd" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Trace controlling resistance" }));
    expect(await screen.findByText("Worker evidence resolved")).toBeInTheDocument();
    expect(screen.getAllByText(result.runId, { exact: false }).length).toBeGreaterThan(0);
    const resolveCall = request.mock.calls.find(([method, params]) => method === "connectivity.evidence.resolve" && (params as Record<string, unknown>).claim_id === claimId);
    expect(resolveCall?.[1]).toEqual({ project_handle: "project_guided_exact", claim_id: claimId });
    expect(await screen.findByText("Traced provider ready")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ask traced assistant" }));
    expect(await screen.findByText(/directly evaluated controlling scenario/i)).toBeInTheDocument();
    expect(screen.getByText("ecocorridor-connectivity-analysis-ridgeway")).toBeInTheDocument();
    expect(screen.getAllByText(/A direct Circuitscape result is not observed animal movement/i).length).toBeGreaterThan(0);
    const assistantCall = request.mock.calls.find(([method]) => method === "assistant.explain");
    expect(assistantCall?.[1]).toMatchObject({ project_handle: "project_guided_exact", evidence_ids: [claimId], sensitive_policy: { prohibited_terms: [], exact_location_authorized: false } });
    expect(JSON.stringify(assistantCall)).not.toContain("api_key");
    expect(JSON.stringify(resolveCall)).not.toContain("project_directory");
  });

  it("blocks a desktop projection when the separately returned manifest digest does not match", async () => {
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.guided.open") {
        return payload(method, {
          session_id: "guided_ridgeway_v1",
          projection: publicRidgewayFixture,
          can_rerun: true,
          state: "VERIFIED_FIXTURE_REPLAY",
          evidence_manifest_sha256: "f".repeat(64),
        });
      }
      if (method === "connectivity.guided.checkpoint") return payload(method, { recorded: true });
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: async () => ({ handle: "project_guided_mismatch" }),
    };

    render(<Workspace mode="desktop" path="guided" localPreview={false} preflight={null} />);
    expect(await screen.findByText(/did not return a digest-matched direct-evidence replay/i)).toBeInTheDocument();
    expect(screen.queryByText("Carry-forward candidate")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open evidence details" })).toBeDisabled();
  });

  it("opens a public evidence dialog for the selected direct-run claim", async () => {
    const user = userEvent.setup();
    render(<EcoApp mode="public-replay" publicFixture={publicRidgewayFixture} />);
    await user.click(screen.getByRole("button", { name: "Robustness board" }));
    await user.click(screen.getByRole("button", { name: "Trace controlling resistance" }));
    const evidence = screen.getByRole("complementary", { name: "Evidence details" });
    expect(evidence).toBeInTheDocument();
    expect(within(evidence).getByRole("heading", { name: "Resolved source chain" })).toBeInTheDocument();
    expect(within(evidence).getByText("Pinned public manifest", { exact: true })).toBeInTheDocument();
    expect(within(evidence).queryByText("Withheld", { exact: true })).not.toBeInTheDocument();
    expect(within(evidence).getByText(/not observed animal movement/i)).toBeInTheDocument();
  });
});

describe("authoritative professional workspace", () => {
  it("reopens a contained saved project from Home without repeating W&B onboarding", async () => {
    const user = userEvent.setup();
    const savedState = projectState("saved-project", "North Fork connectivity review", "IMPORTED", {
      analyses: [{ analysis_id: "analysis_interrupted", state: "RUNNING", created_at: "2026-08-03T12:00:00Z", updated_at: "2026-08-03T12:05:00Z" }],
      pending_recovery: {
        required: true,
        analyses: [{ analysis_id: "analysis_interrupted", analysis_state: "RUNNING", executor_active: false, recovery_required: true, running_job_ids: ["job_17"] }],
        next_action: "Resume or cancel the interrupted analysis explicitly.",
      },
      sync_status: { state: "SYNC_QUEUED_OFFLINE", message: "Exact-scope binding retained; synchronization is queued offline." },
    });
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "connectivity.analysis.status") throw new Error("No live executor after application restart.");
      throw new Error(`Unexpected method ${method}`);
    });
    const openProjectWorkspace = vi.fn(async (handle: string) => ({ handle }));
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      listProjectWorkspaces: async () => [{ handle: `project_${"a".repeat(32)}`, lastModifiedMs: Date.parse("2026-08-03T12:05:00Z") }],
      openProjectWorkspace,
    };

    render(<EcoApp />);
    const continueButton = await screen.findByRole("button", { name: "Continue North Fork connectivity review" });
    expect(within(continueButton).getByText("Resume interrupted run")).toBeInTheDocument();
    await user.click(continueButton);

    expect(screen.queryByRole("dialog", { name: /connect the evidence destination/i })).not.toBeInTheDocument();
    expect(await screen.findByText("Local project restored")).toBeInTheDocument();
    expect(screen.getByText(/interrupted analysis is ready for explicit resume or cancellation/i)).toBeInTheDocument();
    expect(screen.getByText("Sync queued offline")).toBeInTheDocument();
    expect(openProjectWorkspace).toHaveBeenCalledWith(`project_${"a".repeat(32)}`);
    expect(request.mock.calls.filter(([method]) => method === "connectivity.project.get_state").length).toBeGreaterThanOrEqual(2);
    expect(JSON.stringify(request.mock.calls)).not.toContain("project_directory");
  });

  it("rehydrates a paused analysis with its immutable direct-job arithmetic", async () => {
    const analysisId = "analysis_paused_review";
    const plan = directAnalysisPlan(analysisId);
    const savedState = projectState("paused-project", "Paused mountain-pass review", "IMPORTED", {
      analyses: [{ analysis_id: analysisId, state: "PAUSED", created_at: "2026-08-03T12:00:00Z", updated_at: "2026-08-03T12:20:00Z" }],
      sync_status: { state: "CONNECTED", deployment: "DEDICATED", message: "Exact-scope Restricted binding retained." },
    });
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "connectivity.analysis.status") return payload(method, {
        analysis_id: analysisId,
        state: "PAUSED",
        plan,
        completed_run_count: 1,
        summary_count: 0,
        evidence_claim_count: 0,
        direct_job_counts: { SUCCEEDED: 1, QUEUED: 3 },
        error: null,
      });
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<Workspace mode="desktop" path="existing" localPreview={false} preflight={null} reopenedProject={{ projectHandle: "project_paused_opaque", lastModifiedMs: Date.now(), projectState: savedState }} />);

    expect(await screen.findByRole("heading", { name: "New jobs are paused; completed exact results are retained." })).toBeInTheDocument();
    expect(screen.getByText("2 portfolios × 2 scenarios = 4 direct Circuitscape jobs.", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("1 / 4")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume direct runs" })).toBeEnabled();
    expect(request.mock.calls.some(([method, params]) => method === "connectivity.analysis.status" && (params as Record<string, unknown>).analysis_id === analysisId)).toBe(true);
    expect(request.mock.calls.some(([method]) => method === "connectivity.analysis.start")).toBe(false);
  });

  it("rehydrates a completed analysis and its worker-returned decision board", async () => {
    const user = userEvent.setup();
    const analysisId = "analysis_completed_review";
    const plan = directAnalysisPlan(analysisId);
    const savedState = projectState("completed-project", "Completed mountain-pass review", "IMPORTED", {
      analyses: [{ analysis_id: analysisId, state: "COMPLETED", created_at: "2026-08-03T12:00:00Z", updated_at: "2026-08-03T12:30:00Z" }],
      sync_status: { state: "CONNECTED", deployment: "SELF_MANAGED", message: "Exact-scope Restricted binding retained." },
    });
    const decisionBoard = {
      analysis_id: analysisId,
      summaries: [{
        portfolio_id: "portfolio_reviewed",
        disposition: "PASSES" as const,
        cost: 185_000,
        currency: "USD",
        rule_dispositions: [{
          rule_id: "rule_target_reduction",
          scenario_id: "scenario_target_reviewed",
          pair_id: "pair_a_b",
          observed_value: 4.2,
          passed: true,
          evidence_claim_id: "claim_reviewed_target",
          explanation: "Direct within-scenario baseline-relative resistance reduction.",
        }],
        controlling_scenario_id: "scenario_target_reviewed",
        controlling_pair_id: "pair_a_b",
        worst_resistance_reduction_percent: 4.2,
        safeguard_passed: true,
        review_mask_triggered: false,
        unresolved_feasibility: ["Confirm land access before any intervention recommendation."],
        next_action: "Candidate to carry forward for ecological, field, and engineering feasibility review.",
        direct_run_ids: ["run_reviewed_target", "run_reviewed_safeguard"],
        disposition_evidence_claim_id: "claim_reviewed_disposition",
      }],
      direct_run_ids: ["run_reviewed_target", "run_reviewed_safeguard"],
      evidence_claim_ids: ["claim_reviewed_target", "claim_reviewed_disposition"],
      claim_language: "Direct Circuitscape result under documented assumptions." as const,
    };
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "connectivity.analysis.status") return payload(method, {
        analysis_id: analysisId,
        state: "COMPLETED",
        plan,
        completed_run_count: 4,
        summary_count: 1,
        evidence_claim_count: 2,
        direct_job_counts: { SUCCEEDED: 4 },
        error: null,
      });
      if (method === "connectivity.results.decision_board") return payload(method, decisionBoard);
      if (method === "connectivity.evidence.resolve") {
        const claimId = String(params.claim_id);
        const controlling = claimId === "claim_reviewed_target";
        const runId = controlling ? "run_reviewed_target" : "run_reviewed_safeguard";
        return payload(method, {
          claim: {
            claim_id: claimId,
            portfolio_id: "portfolio_reviewed",
            scenario_id: controlling ? "scenario_target_reviewed" : null,
            pair_id: controlling ? "pair_a_b" : null,
            value: controlling ? 4.2 : "PASSES",
            units: controlling ? "percent" : "screening disposition",
            reducer: controlling ? "Direct within-scenario baseline-relative resistance reduction." : "Complete retained portfolio disposition.",
            direct_run_ids: decisionBoard.direct_run_ids,
            nodes: [
              { node_id: claimId, node_type: "CLAIM", label: "Direct reviewed claim" },
              { node_id: `run:${runId}`, node_type: "DIRECT_RUN", label: runId },
            ],
            edges: [{ from_node_id: claimId, to_node_id: `run:${runId}`, relation: "supported_by" }],
            limitations: ["Interpret only within the reviewed scenario and documented assumptions."],
          },
        });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<Workspace mode="desktop" path="existing" localPreview={false} preflight={null} reopenedProject={{ projectHandle: "project_completed_opaque", lastModifiedMs: Date.now(), projectState: savedState }} />);

    expect(await screen.findByRole("heading", { name: "Direct evidence, weakest case first." })).toBeInTheDocument();
    expect(screen.getAllByText("portfolio_reviewed").length).toBeGreaterThan(0);
    expect(screen.getByText("4.20%")).toBeInTheDocument();
    expect(screen.getByText(/Candidate to carry forward for ecological, field, and engineering feasibility review/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Scrollable professional portfolio decision matrix" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("region", { name: "Scrollable rule-by-rule direct evidence" })).toHaveAttribute("tabindex", "0");
    await user.click(screen.getByRole("button", { name: "Trace controlling direct value" }));
    expect(await screen.findByText("Worker evidence resolved")).toBeInTheDocument();
    expect(request.mock.calls.some(([method, params]) => method === "connectivity.evidence.resolve" && (params as Record<string, unknown>).claim_id === "claim_reviewed_target")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Close evidence" }));
    await user.click(screen.getByRole("button", { name: "Trace portfolio disposition" }));
    expect(await screen.findByText("Worker evidence resolved")).toBeInTheDocument();
    expect(request.mock.calls.some(([method, params]) => method === "connectivity.evidence.resolve" && (params as Record<string, unknown>).claim_id === "claim_reviewed_disposition")).toBe(true);
    expect(request.mock.calls.some(([method, params]) => method === "connectivity.results.decision_board" && (params as Record<string, unknown>).analysis_id === analysisId)).toBe(true);
  });

  it("reopens a saved guided rerun through the existing opaque workspace and restores real progress", async () => {
    const user = userEvent.setup();
    const projection = verifiedGuidedProjection();
    const guidedState = projectState("ridgeway-guided-saved", "Ridgeway Crossing rerun", "IMPORTED", {
      project: { project_id: "ridgeway-guided-saved", title: "Ridgeway Crossing rerun", mode: "GUIDED" },
      sync_status: { state: "SYNC_QUEUED_OFFLINE", message: "Guided telemetry is queued offline." },
    });
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, guidedState);
      if (method === "connectivity.guided.open") return payload(method, {
        session_id: "guided_ridgeway_reopened",
        projection,
        can_rerun: true,
        state: "PAUSED",
        evidence_manifest_sha256: projection.manifestSha256,
      });
      if (method === "connectivity.guided.status") return payload(method, {
        session_id: "guided_ridgeway_reopened",
        analysis_id: "guided_analysis_reopened",
        state: "PAUSED",
        phase: "MAP",
        completed_jobs: 300,
        total_jobs: 1596,
        matrix_completed: 290,
        map_completed: 10,
        cached_jobs: 25,
        differential: null,
      });
      if (method === "connectivity.guided.checkpoint") return payload(method, { recorded: true });
      throw new Error(`Unexpected method ${method}`);
    });
    const createProjectWorkspace = vi.fn(async () => ({ handle: "must_not_create_guided_workspace" }));
    const openProjectWorkspace = vi.fn(async (handle: string) => ({ handle }));
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      listProjectWorkspaces: async () => [{ handle: "project_guided_saved_opaque", lastModifiedMs: Date.now() }],
      openProjectWorkspace,
      createProjectWorkspace,
    };

    render(<EcoApp />);
    await user.click(await screen.findByRole("button", { name: "Continue Ridgeway Crossing rerun" }));

    expect(screen.queryByRole("dialog", { name: /connect the evidence destination/i })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "MAP phase is paused." })).toBeInTheDocument();
    expect(screen.getByText("300", { selector: ".run-orbit span" })).toBeInTheDocument();
    expect(screen.getByText("290 / 1,536")).toBeInTheDocument();
    expect(screen.getByText("10 / 60")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume rerun" })).toBeEnabled();
    expect(createProjectWorkspace).not.toHaveBeenCalled();
    expect(openProjectWorkspace).toHaveBeenCalledWith("project_guided_saved_opaque");
    expect(request.mock.calls.find(([method]) => method === "connectivity.guided.open")?.[1]).toEqual({ project_handle: "project_guided_saved_opaque" });
  });

  it("restores the deployment authorization, Model Fitness evidence, and unsaved editor drafts", async () => {
    const user = userEvent.setup();
    const base = projectState("retained-editors", "Retained professional editors", "PLANNING_DRAFT");
    const documentAsset = {
      asset_id: "asset_reviewed_memo",
      kind: "DOCUMENT" as const,
      title: "Reviewed ecological memorandum",
      relative_path: "assets/asset_reviewed_memo/source.pdf",
      sha256: "6".repeat(64),
      byte_size: 4096,
      media_type: "application/pdf",
      source: "State wildlife agency memorandum",
      vintage: "2026-06",
      license: "Agency internal use terms",
      grid: null,
      authorization: {
        asset_id: "asset_reviewed_memo",
        ownership: "State wildlife agency",
        license_spdx_or_name: "Agency internal use terms",
        sensitivity: "INTERNAL" as const,
        cloud_disposition: "DEDICATED_OR_SELF_MANAGED_ONLY" as const,
        export_allowed: false,
        retention_policy: "Retain through project close.",
        spatial_redaction: "Generalized extent; no exact locations in the document.",
        approved_deployments: ["DEDICATED" as const],
        reviewer: "Agency ecologist",
        reviewed_at: "2026-08-03T12:00:00Z",
        generalized_asset_id: null,
        notes: "Intended use: Support reviewer interpretation of the planning context only.",
      },
    };
    const fitness = {
      ...base.fitness,
      dimensions: [{
        key: "resistance_derivation" as const,
        status: "REVIEW_REQUIRED" as const,
        evidence_ids: ["asset_reviewed_memo"],
        reviewer: "Agency ecologist",
        notes: "Resistance derivation is retained as a reviewed hypothesis with no animal-movement validation claim.",
      }],
      reviewed_by: "Agency ecologist",
      reviewed_at: "2026-08-03T12:00:00Z",
    };
    const savedState: ProfessionalProjectState = {
      ...base,
      effective_bundle: { ...base.effective_bundle, assets: [documentAsset], fitness },
      fitness,
      validation: { status: "BLOCKED", findings: [] },
      sync_status: { state: "CONNECTED", deployment: "DEDICATED", message: "Exact-scope Restricted binding retained." },
    };
    const request = vi.fn(async (method: string) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "assistant.status") return payload(method, { configured: false, provider: null, project_id: "retained-editors", wandb_external_project_id: "wandb-retained-editors", trace_required: true });
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={null} reopenedProject={{ projectHandle: "project_retained_editors", lastModifiedMs: Date.now(), projectState: savedState }} />);
    expect(await screen.findByText(/Authoritative records restored/i)).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Model fitness" })[0]);
    expect(screen.getAllByText(/Resistance derivation is retained as a reviewed hypothesis/i).length).toBeGreaterThan(0);
    expect(screen.getByDisplayValue("Agency ecologist")).toBeInTheDocument();
    expect(screen.getByText(/retained worker check is BLOCKED/i)).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Rules and costs" })[0]);
    await user.selectOptions(screen.getByLabelText("Retained supporting document *"), "asset_reviewed_memo");
    expect(screen.getByText("Authorized for DEDICATED")).toBeInTheDocument();
    const briefTitle = screen.getByLabelText("Brief title *");
    await user.type(briefTitle, "North approach screening brief");
    expect(screen.getByText("Draft retained in workspace")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Model fitness" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Rules and costs" })[0]);
    expect(screen.getByLabelText("Brief title *")).toHaveValue("North approach screening brief");
    await user.click(screen.getByRole("button", { name: "Undo brief draft" }));
    expect(screen.getByLabelText("Brief title *")).toHaveValue("");
    expect(screen.getAllByText("No unsaved field changes").length).toBeGreaterThan(0);
  });

  it("publishes one reviewed cost basis with exact coverage for every persisted option", async () => {
    const user = userEvent.setup();
    const base = projectState("cost-ledger", "Reviewed cost ledger", "PLANNING_DRAFT");
    const interventionSite = {
      site_id: "site_north_approach",
      title: "North approach",
      footprint_asset_id: "asset_reviewed_footprint",
      option_ids: ["option_no_action", "option_survey_first"],
      known_constraints: ["Field access remains subject to owner coordination."],
      reviewed_by: "Agency ecologist",
      reviewed_at: "2026-08-03T12:00:00Z",
    };
    const interventionOptions = [
      {
        option_id: "option_no_action",
        site_id: interventionSite.site_id,
        title: "No action",
        kind: "NO_ACTION" as const,
        effects: [],
        cost: 0,
        currency: "USD",
        cost_vintage: "2026",
        dependencies: [],
        exclusions: [],
        land_access: "No new access represented.",
        maintenance: "Existing obligations only.",
        external_feasibility_evidence: [],
        unresolved_concerns: [],
        reviewed_by: "Agency ecologist",
        reviewed_at: "2026-08-03T12:00:00Z",
      },
      {
        option_id: "option_survey_first",
        site_id: interventionSite.site_id,
        title: "Survey first",
        kind: "SURVEY_FIRST" as const,
        effects: [],
        cost: 12_500,
        currency: "USD",
        cost_vintage: "2026",
        dependencies: ["Landowner coordination"],
        exclusions: ["Construction"],
        land_access: "Access permission remains unresolved.",
        maintenance: "No modeled asset maintenance.",
        external_feasibility_evidence: ["Agency field-work estimate, 2026"],
        unresolved_concerns: ["Confirm seasonal survey window."],
        reviewed_by: "Agency ecologist",
        reviewed_at: "2026-08-03T12:00:00Z",
      },
    ];
    const savedState: ProfessionalProjectState = {
      ...base,
      effective_bundle: {
        ...base.effective_bundle,
        intervention_sites: [interventionSite],
        intervention_options: interventionOptions,
      },
    };
    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "connectivity.cost_basis.get") return payload(method, { cost_basis: null, active_revision_id: base.active_revision.revision_id, confirmed: false, missing_requirements: ["A reviewed CostBasis has not been persisted for the active intervention catalog."] });
      if (method === "assistant.status") return payload(method, { configured: false, provider: null, project_id: "cost-ledger", wandb_external_project_id: "wandb-cost-ledger", trace_required: true });
      if (method === "connectivity.cost_basis.save") {
        const costBasis = (params?.cost_basis ?? {}) as Record<string, unknown>;
        return payload(method, {
          record_id: costBasis.cost_basis_id,
          revision: 4,
          cost_basis: { ...costBasis, assumptions: [...(costBasis.assumptions as string[]), "Reviewer confirmation: Explicit confirmation retained by the worker."] },
          active_revision_id: "revision_cost_basis_4",
          cost_basis_sha256: "7".repeat(64),
          confirmed: true,
        });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={null} reopenedProject={{ projectHandle: "project_cost_ledger", lastModifiedMs: Date.now(), projectState: savedState }} />);
    expect(await screen.findByText(/Authoritative records restored/i)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Rules and costs" })[0]);

    expect(screen.getByRole("heading", { name: "Reviewed present-value cost basis" })).toBeInTheDocument();
    expect(await screen.findByText("1 requirement missing")).toBeInTheDocument();
    expect(screen.getByLabelText("No action cost (USD)")).toHaveValue("0");
    expect(screen.getByLabelText("Survey first cost (USD)")).toHaveValue("12500");
    expect(screen.getByText("2/2")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Cost source or citation *"), "State DOT planning estimate, revision 3, table 7.");
    await user.type(screen.getByLabelText("CAPEX basis *"), "2026 planning quantities, mobilization, and 30% contingency; land purchase excluded.");
    await user.type(screen.getByLabelText("O&M assumptions *"), "Annual inspection and routine approach-maintenance allowance included.");
    await user.type(screen.getByLabelText("Replacement assumptions *"), "No replacement inside the 30-year screening horizon; zero residual value.");
    fireEvent.change(screen.getByLabelText("Reviewed at *"), { target: { value: "2026-08-03T13:45" } });
    await user.click(screen.getByRole("checkbox", { name: /I reviewed the source, present-value assumptions, and every exact option cost/i }));
    await user.click(screen.getByRole("button", { name: "Save reviewed cost basis" }));

    await waitFor(() => expect(request.mock.calls.some(([method]) => method === "connectivity.cost_basis.save")).toBe(true));
    const saveCall = request.mock.calls.find(([method]) => method === "connectivity.cost_basis.save");
    const saveParams = saveCall?.[1] as Record<string, unknown>;
    expect(saveParams.confirmed).toBe(true);
    expect(saveParams.confirmation_statement).toMatch(/reviewed the cited source/i);
    expect(saveParams.cost_basis).toMatchObject({
      source: "State DOT planning estimate, revision 3, table 7.",
      currency: "USD",
      vintage: "2026",
      option_costs: { option_no_action: 0, option_survey_first: 12_500 },
      assumptions: expect.arrayContaining([
        "Analysis horizon years: 30",
        "Real discount rate percent: 3",
        "CAPEX basis: 2026 planning quantities, mobilization, and 30% contingency; land purchase excluded.",
      ]),
    });
    expect(await screen.findByText(/all 2 option costs remain traceable/i)).toBeInTheDocument();
    expect(screen.getByText("Worker-confirmed basis")).toBeInTheDocument();
    expect(screen.getByText(/revalidate Model Fitness before attesting or running/i)).toBeInTheDocument();
  });

  it("keeps an unbound or policy-blocked recent project behind exact-scope W&B verification", async () => {
    const user = userEvent.setup();
    const blockedState = projectState("blocked-project", "Blocked destination review", "IMPORTED", {
      sync_status: { state: "BLOCKED_PROJECT_POLICY", message: "No authoritative Restricted binding is retained." },
    });
    const request = vi.fn(async (method: string) => {
      if (method === "connectivity.project.get_state") return payload(method, blockedState);
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      listProjectWorkspaces: async () => [{ handle: `project_${"b".repeat(32)}`, lastModifiedMs: Date.parse("2026-08-03T12:05:00Z") }],
      openProjectWorkspace: async (handle) => ({ handle }),
    };

    render(<EcoApp />);
    await user.click(await screen.findByRole("button", { name: "Continue Blocked destination review" }));

    expect(screen.getByRole("dialog", { name: /connect the evidence destination/i })).toBeInTheDocument();
    expect(screen.getByText(/Professional workflows remain unavailable until exact Restricted scope/i)).toBeInTheDocument();
    expect(screen.queryByText("Local project restored")).not.toBeInTheDocument();
    expect(request.mock.calls.filter(([method]) => method === "connectivity.project.get_state")).toHaveLength(1);
  });

  it("hydrates every imported professional editor from the worker-authoritative snapshot", () => {
    const base = projectState("hydrated-project", "Reviewed mountain-pass model", "IMPORTED");
    const sourceGrid = { grid_id: "grid_source", crs: "EPSG:5070", width: 4, height: 3, transform: [30, 0, 500000, 0, -30, 4200000] as [number, number, number, number, number, number], nodata: -9999, value_kind: "CONTINUOUS" as const, cell_area_m2: 900 };
    const targetGrid = { ...sourceGrid, grid_id: "grid_reviewed" };
    const authorization = (assetId: string, notes: string) => ({
      asset_id: assetId,
      ownership: "State wildlife agency",
      license_spdx_or_name: "Agency internal use terms",
      sensitivity: "INTERNAL" as const,
      cloud_disposition: "DEDICATED_OR_SELF_MANAGED_ONLY" as const,
      export_allowed: false,
      retention_policy: "Retain through project close.",
      spatial_redaction: "Generalized review extent only.",
      approved_deployments: ["DEDICATED" as const],
      reviewer: "Agency ecologist",
      reviewed_at: "2026-08-03T12:00:00Z",
      generalized_asset_id: null,
      notes,
    });
    const sourceAsset = {
      asset_id: "asset_resistance_source", kind: "RESISTANCE_RASTER" as const, title: "Original reviewed resistance",
      relative_path: "data_assets/original-resistance.tif", sha256: "2".repeat(64), byte_size: 4096, media_type: "image/tiff",
      source: "Agency resistance workshop", vintage: "2026", license: "Agency internal use terms", grid: sourceGrid,
      authorization: authorization("asset_resistance_source", "Intended use: Preserve the exact reviewer-supplied raster."),
    };
    const derivedAsset = {
      ...sourceAsset,
      asset_id: "asset_resistance_reviewed",
      title: "Explicitly aligned resistance",
      relative_path: "data_assets/derived/reviewed-resistance.tif",
      sha256: "3".repeat(64),
      source: "Agency resistance workshop:alignment:alignment_plan_1",
      grid: targetGrid,
      authorization: authorization("asset_resistance_reviewed", "Intended use: Authoritative reviewed resistance for direct Circuitscape runs.\nAuthorization notes: Dedicated deployment only."),
    };
    const hypothesis = {
      hypothesis_id: "hypothesis_1", title: "Forest guild current-condition resistance", guild_id: "guild_target", life_stage: "adult",
      future_condition: "current", maturity_horizon: "present", resistance_asset_id: derivedAsset.asset_id, grid_id: targetGrid.grid_id,
      rationale: "Represents an ecologist-reviewed dispersal hypothesis.", derivation: "Values assigned through a reviewed workshop.",
      evidence_citations: ["Workshop memorandum, p. 14"], confidence: "INFERRED" as const, reviewed_by: "Agency ecologist", reviewed_at: "2026-08-03T12:00:00Z",
    };
    const focalSet = {
      focal_set_id: "focal_set_1", title: "North and south habitat cores", asset_id: derivedAsset.asset_id, grid_id: targetGrid.grid_id,
      guild_id: "guild_target", focal_ids: [1, 2], pairs: [{ pair_id: "pair_1_2", from_focal_id: 1, to_focal_id: 2, required: true }],
      rationale: "Agency-reviewed habitat-core question.", reviewed_by: "Agency ecologist", reviewed_at: "2026-08-03T12:00:00Z",
    };
    const scenario = {
      scenario_id: "scenario_1", title: "Target guild · current condition", guild_id: "guild_target", life_stage: "adult",
      resistance_hypothesis_id: hypothesis.hypothesis_id, focal_set_id: focalSet.focal_set_id, future_condition: "current",
      maturity_horizon: "present", included_pair_ids: ["pair_1_2"], safeguard: true, grid_id: targetGrid.grid_id,
      engine_mode: "RASTER_PAIRWISE" as const, raster_semantics: "RESISTANCE" as const, neighbor_rule: "EIGHT" as const,
      resistance_average: "ARITHMETIC" as const, precision: "double" as const, julia_threads: 1 as const, blas_threads: 1 as const,
    };
    const site = { site_id: "site_1", title: "Highway approach review", footprint_asset_id: derivedAsset.asset_id, option_ids: ["option_no_action"], known_constraints: ["Land access unresolved"], reviewed_by: "Agency ecologist", reviewed_at: "2026-08-03T12:00:00Z" };
    const option = { option_id: "option_no_action", site_id: site.site_id, title: "No action", kind: "NO_ACTION" as const, effects: [], cost: 0, currency: "USD", cost_vintage: "2026", dependencies: [], exclusions: [], land_access: "Unresolved", maintenance: "Not applicable", external_feasibility_evidence: [], unresolved_concerns: ["Land access"], reviewed_by: "Agency ecologist", reviewed_at: "2026-08-03T12:00:00Z" };
    const safeguard = { safeguard_id: "safeguard_1", guild_id: "guild_target", scenario_ids: [scenario.scenario_id], nonregression_threshold_percent: 0, concerns: ["Construction disturbance requires field review"], reviewed_by: "Agency ecologist", reviewed_at: "2026-08-03T12:00:00Z" };
    const reviewMask = { mask_id: "mask_road", title: "Road diagnostic review mask", asset_id: derivedAsset.asset_id, grid_id: targetGrid.grid_id, review_trigger_percent: 5, interpretation: "Send modeled current redistribution to human review; not animal use.", not_an_objective_notice: "Current redistribution is a diagnostic review signal, not predicted animal use." as const };
    const decisionRule = { rule_id: "rule_1", title: "Require target nonregression", metric: "MAX_RESISTANCE_INCREASE_PERCENT" as const, operator: "AT_MOST" as const, threshold: 0, scenario_ids: [scenario.scenario_id], pair_ids: ["pair_1_2"], required: true, rationale: "Avoid worsening the required target pair under this hypothesis." };
    const plan = { plan_id: "alignment_plan_1", source_asset_id: sourceAsset.asset_id, source_sha256: sourceAsset.sha256, source_grid: sourceGrid, target_grid: targetGrid, method: "NEAREST" as const, source_cell_count: 12, target_cell_count: 12, source_nodata_count: 1, estimated_target_nodata_count: 1, categorical_counts_before: {}, categorical_counts_after: {}, resulting_sha256: derivedAsset.sha256, warnings: ["Explicit reviewer confirmation required."] };
    const approval = { approval_id: "alignment_approval_1", plan_id: plan.plan_id, plan_sha256: "4".repeat(64), reviewer: "Agency ecologist", approved_at: "2026-08-03T12:10:00Z", acknowledged_warnings: plan.warnings, purpose: "Create the documented target-grid derivative." };
    const diff = { diff_id: "diff_alignment_1", from_revision_id: "revision_before_alignment", to_revision_id: base.active_revision.revision_id, changes: [{ object_type: "asset", object_id: derivedAsset.asset_id, field: "grid", before: sourceGrid.grid_id, after: targetGrid.grid_id, rationale: "Approved grid alignment." }], sha256: "5".repeat(64) };
    const state: ProfessionalProjectState = {
      ...base,
      effective_bundle: {
        ...base.effective_bundle,
        assets: [sourceAsset, derivedAsset],
        resistance_hypotheses: [hypothesis], focal_area_sets: [focalSet], scenarios: [scenario],
        intervention_sites: [site], intervention_options: [option], safeguards: [safeguard], review_masks: [reviewMask], decision_rules: [decisionRule],
      },
      saved_records: { ...base.saved_records, grid_alignment_plan: [plan], grid_alignment_approval: [approval], semantic_diff: [diff] },
    };

    const hydrated = hydrateProfessionalProject(state);

    expect(hydrated.assets.map((asset) => asset.assetId)).toEqual([sourceAsset.asset_id, derivedAsset.asset_id]);
    expect(hydrated.assets[1]).toMatchObject({ intendedUse: "Authoritative reviewed resistance for direct Circuitscape runs.", grid: { grid_id: "grid_reviewed" } });
    expect(hydrated.gridApprovals).toEqual([{ plan, approvalId: approval.approval_id, derivedAssetId: derivedAsset.asset_id, revisionId: base.active_revision.revision_id }]);
    expect(hydrated.records).toMatchObject({
      hypotheses: [hypothesis], focalAreaSets: [focalSet], scenarios: [scenario], interventionSites: [site], interventionOptions: [option],
      safeguards: [safeguard], reviewMasks: [reviewMask], decisionRules: [decisionRule], decisionBrief: base.effective_bundle.decision_brief,
      resourcePolicy: base.resource_policy,
    });
    expect(hydrated.planning).toMatchObject({ projectName: "Reviewed mountain-pass model", reviewer: "Agency ecologist", targetGuild: "Anonymous forest-associated focal guild", alignmentMethod: "nearest", alignmentApproved: true, hypothesisRationale: hypothesis.rationale, focalRationale: focalSet.rationale, safeguardNotes: safeguard.concerns[0], budgetUsd: "250000" });
    expect(hydrated.state.fitness).toEqual(base.fitness);
  });

  it("keeps each professional safeguard review scoped to its persisted guild scenarios", async () => {
    const user = userEvent.setup();
    const base = projectState("safeguard-scope-project", "Two-guild safeguard review", "IMPORTED");
    const scenario = (scenarioId: string, title: string, guildId: string, suffix: string) => ({
      scenario_id: scenarioId,
      title,
      guild_id: guildId,
      life_stage: "adult",
      resistance_hypothesis_id: `hypothesis_${guildId}_${suffix}`,
      focal_set_id: `focal_${guildId}`,
      future_condition: suffix,
      maturity_horizon: "present",
      included_pair_ids: [`pair_${guildId}`],
      safeguard: true,
      grid_id: "grid_reviewed",
      engine_mode: "RASTER_PAIRWISE" as const,
      raster_semantics: "RESISTANCE" as const,
      neighbor_rule: "EIGHT" as const,
      resistance_average: "ARITHMETIC" as const,
      precision: "double" as const,
      julia_threads: 1 as const,
      blas_threads: 1 as const,
    });
    const scenarios = [
      scenario("scenario_guild_a_current", "Guild A current", "guild_a", "current"),
      scenario("scenario_guild_a_future", "Guild A future", "guild_a", "future"),
      scenario("scenario_guild_b_current", "Guild B current", "guild_b", "current"),
      scenario("scenario_guild_b_future", "Guild B future", "guild_b", "future"),
    ];
    const safeguards = [
      {
        safeguard_id: "safeguard_guild_a",
        guild_id: "guild_a",
        scenario_ids: ["scenario_guild_a_current", "scenario_guild_a_future"],
        nonregression_threshold_percent: 0.25,
        concerns: ["Guild A retained concern"],
        reviewed_by: "Agency ecologist",
        reviewed_at: "2026-08-03T14:00:00Z",
      },
      {
        safeguard_id: "safeguard_guild_b",
        guild_id: "guild_b",
        scenario_ids: ["scenario_guild_b_current", "scenario_guild_b_future"],
        nonregression_threshold_percent: 1.5,
        concerns: ["Guild B retained concern"],
        reviewed_by: "Agency ecologist",
        reviewed_at: "2026-08-03T15:00:00Z",
      },
    ];
    const savedState: ProfessionalProjectState = {
      ...base,
      effective_bundle: {
        ...base.effective_bundle,
        scenarios,
        safeguards,
        decision_brief: {
          ...base.effective_bundle.decision_brief,
          safeguard_guilds: ["guild_a", "guild_b"],
        },
      },
      sync_status: { state: "CONNECTED", deployment: "SELF_MANAGED", message: "Exact-scope Restricted binding retained." },
    };
    let revision = 10;
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.project.get_state") return payload(method, savedState);
      if (method === "connectivity.safeguard.save") {
        const record = params.safeguard as { safeguard_id: string };
        revision += 1;
        return payload(method, { record_id: record.safeguard_id, revision });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<Workspace mode="desktop" path="existing" localPreview={false} preflight={null} reopenedProject={{ projectHandle: "project_safeguard_scope", lastModifiedMs: Date.now(), projectState: savedState }} />);

    expect(await screen.findByText("Local project restored")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Safeguards" }));
    const guildSelector = screen.getByLabelText("Safeguard guild *") as HTMLSelectElement;
    await waitFor(() => expect(guildSelector).toHaveValue("guild_a"));
    expect(screen.getByLabelText("Allowed resistance increase (%) *")).toHaveValue("0.25");
    expect(screen.getByLabelText("Concerns and unresolved checks * · one per line")).toHaveValue("Guild A retained concern");
    const guildAScope = screen.getByText("This review applies only to guild_a.").closest(".scenario-scope-note");
    expect(guildAScope).not.toBeNull();
    expect(within(guildAScope as HTMLElement).getByText("Guild A current · Guild A future")).toBeInTheDocument();

    await user.selectOptions(guildSelector, "guild_b");
    expect(screen.getByLabelText("Allowed resistance increase (%) *")).toHaveValue("1.5");
    expect(screen.getByLabelText("Concerns and unresolved checks * · one per line")).toHaveValue("Guild B retained concern");
    const guildBScope = screen.getByText("This review applies only to guild_b.").closest(".scenario-scope-note");
    expect(guildBScope).not.toBeNull();
    expect(within(guildBScope as HTMLElement).getByText("Guild B current · Guild B future")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Allowed resistance increase (%) *"));
    await user.type(screen.getByLabelText("Allowed resistance increase (%) *"), "2.25");
    await user.clear(screen.getByLabelText("Concerns and unresolved checks * · one per line"));
    await user.type(screen.getByLabelText("Concerns and unresolved checks * · one per line"), "Guild B updated concern");
    await user.click(screen.getByRole("button", { name: "Update guild safeguard review" }));
    expect(await screen.findByText(/guild_b: 2 safeguard scenarios persisted/i)).toBeInTheDocument();

    await user.selectOptions(guildSelector, "guild_a");
    expect(screen.getByLabelText("Concerns and unresolved checks * · one per line")).toHaveValue("Guild A retained concern");
    await user.selectOptions(guildSelector, "guild_b");
    expect(screen.getByLabelText("Allowed resistance increase (%) *")).toHaveValue("2.25");
    expect(screen.getByLabelText("Concerns and unresolved checks * · one per line")).toHaveValue("Guild B updated concern");

    const saveCall = request.mock.calls.find(([method]) => method === "connectivity.safeguard.save");
    expect(saveCall?.[1]).toMatchObject({
      project_handle: "project_safeguard_scope",
      safeguard: {
        safeguard_id: "safeguard_guild_b",
        guild_id: "guild_b",
        scenario_ids: ["scenario_guild_b_current", "scenario_guild_b_future"],
        nonregression_threshold_percent: 2.25,
        concerns: ["Guild B updated concern"],
      },
    });
    expect(JSON.stringify(saveCall?.[1])).not.toContain("scenario_guild_a_");
  });

  it("inspects and imports a native-staged bundle without sending a renderer path", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.bundle.inspect") {
        return payload(method, {
          project_id: "reviewed-project",
          title: "Reviewed corridor model",
          archive_sha256: "b".repeat(64),
          file_count: 9,
          uncompressed_bytes: 2048,
          asset_count: 4,
          scenario_count: 3,
        });
      }
      if (method === "connectivity.bundle.import") {
        return payload(method, { project_id: "reviewed-project", archive_sha256: "b".repeat(64), state: "IMPORTED" });
      }
      if (method === "connectivity.project.get_state") {
        return payload(method, projectState("reviewed-project", "Reviewed corridor model", "IMPORTED"));
      }
      if (method === "observability.connect") {
        return payload(method, { binding: { state: "CONNECTED" }, upload_preview: { allowed: true } });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      chooseConnectivityBundle: async () => ({ handle: "bundle_opaque", displayName: "reviewed.ecocorridor", byteSize: 1024, sha256: "b".repeat(64) }),
      createProjectWorkspace: async () => ({ handle: "project_opaque" }),
    };

    render(<Workspace mode="desktop" path="existing" localPreview={false} preflight={{ preflightId: "preflight_1", entity: "team", project: "eco", deployment: "DEDICATED" }} />);
    await user.click(screen.getByRole("button", { name: "Choose bundle" }));
    await user.click(await screen.findByRole("button", { name: "Inspect bundle" }));
    expect(await screen.findByText(/Worker inspection passed/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import verified bundle" }));
    expect(await screen.findByText("Restricted project connected")).toBeInTheDocument();

    const inspectParams = request.mock.calls.find(([method]) => method === "connectivity.bundle.inspect")?.[1];
    const importParams = request.mock.calls.find(([method]) => method === "connectivity.bundle.import")?.[1];
    expect(inspectParams).toMatchObject({ bundle_handle: "bundle_opaque" });
    expect(importParams).toMatchObject({ bundle_handle: "bundle_opaque", project_handle: "project_opaque" });
    expect(JSON.stringify(request.mock.calls)).not.toContain("bundle_path");
    expect(JSON.stringify(request.mock.calls)).not.toContain("project_directory");
  });

  it("persists an intentionally blocking planning skeleton before claiming it is saved", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string, _params: Record<string, unknown>) => {
      if (method === "connectivity.planning.create") {
        return payload(method, { project_id: "planning-project", state: "PLANNING_DRAFT", next_action: "Add reviewed evidence." });
      }
      if (method === "connectivity.project.get_state") {
        return payload(method, projectState("planning-project", "Route 14 screen"));
      }
      if (method === "observability.connect") {
        return payload(method, { binding: { state: "BLOCKED_PROJECT_POLICY" }, upload_preview: { allowed: false, blockers: ["PLANNING_MODEL_INCOMPLETE"] } });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: async () => ({ handle: "project_planning" }),
    };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={{ preflightId: "preflight_2", entity: "team", project: "eco", deployment: "SELF_MANAGED" }} />);
    await user.click(screen.getAllByRole("button", { name: "Study context" })[0]);
    expect(screen.getByText(/Complete the six required fields/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Project name *"), "Route 14 screen");
    await user.type(screen.getByLabelText("Generalized study area *"), "County-scale generalized extent");
    await user.type(screen.getByLabelText("Decision question *"), "Which concept should advance to feasibility review?");
    await user.type(screen.getByLabelText("Intended use and scope boundary *"), "Planning comparison only; not animal movement, collision, design, or approval evidence.");
    await user.type(screen.getByLabelText("Target guild or life stage *"), "Anonymous forest-associated focal guild");
    await user.type(screen.getByLabelText("Responsible ecological reviewer *"), "Agency ecologist");
    await user.click(screen.getByRole("button", { name: "Create planning record" }));
    expect(await screen.findByText(/Authoritative local draft created/i)).toBeInTheDocument();
    expect(await screen.findByText("Sync blocked by asset policy")).toBeInTheDocument();

    const createCall = request.mock.calls.find(([method]) => method === "connectivity.planning.create");
    expect(createCall?.[1]).toMatchObject({ project_handle: "project_planning", scenarios: [], safeguards: [], review_masks: [], decision_rules: [] });
    const fitness = (createCall?.[1] as unknown as { fitness: { dimensions: Array<{ status: string }> } }).fitness;
    expect(fitness.dimensions).toHaveLength(12);
    expect(fitness.dimensions.filter((item) => item.status === "BLOCKING")).toHaveLength(11);
  });

  it("keeps incomplete and inconsistent asset authorizations fail-closed", () => {
    const selected: OpaqueFileHandle = {
      handle: "asset_opaque",
      displayName: "reviewed.pdf",
      byteSize: 512,
      sha256: "c".repeat(64),
    };
    const draft = createAssetDraft("Agency ecologist");
    expect(assetRequirements(draft, selected)).toEqual(expect.arrayContaining([
      "Asset title",
      "Reviewer-assigned asset role",
      "Raw/generalized cloud permission",
      "Authorization review timestamp",
      "Reviewer role confirmation",
    ]));
    expect(() => buildAssetAddParams(draft, selected, "project_opaque")).toThrow(/Complete or correct/i);

    const prohibitedOnCloud = {
      ...draft,
      cloudDisposition: "PROHIBITED" as const,
      approvedDeployments: ["WANDB_CLOUD"] as const,
    };
    expect(assetRequirements({ ...prohibitedOnCloud, approvedDeployments: [...prohibitedOnCloud.approvedDeployments] }, selected)).toContain("PROHIBITED data may approve LOCAL_ONLY only");
  });

  it("requires matching worker grid evidence for modeled rasters and sends only opaque handles", () => {
    const selected: OpaqueFileHandle = {
      handle: "raster_opaque",
      displayName: "resistance.asc",
      byteSize: 824,
      sha256: "e".repeat(64),
    };
    const draft = {
      ...createAssetDraft("Agency ecologist"),
      kind: "RESISTANCE_RASTER" as const,
      rasterFormat: "ESRI_ASCII" as const,
      rasterValueKind: "CONTINUOUS" as const,
      esriAsciiCrs: "EPSG:5070",
      esriAsciiCellAreaM2: "900",
    };

    expect(assetRequirements(draft, selected)).toContain("Successful worker raster inspection");
    expect(buildAssetInspectParams(draft, selected, "project_opaque")).toEqual({
      project_handle: "project_opaque",
      source_handle: "raster_opaque",
      grid_id: `grid_${draft.assetId}`,
      value_kind: "CONTINUOUS",
      format: "ESRI_ASCII",
      esri_ascii_crs: "EPSG:5070",
      esri_ascii_cell_area_m2: 900,
    });
    expect(JSON.stringify(buildAssetInspectParams(draft, selected, "project_opaque"))).not.toContain("source_path");
  });

  it("persists multiple explicitly reviewed assets through opaque native handles", async () => {
    const user = userEvent.setup();
    const staged = [
      { handle: "asset_first_opaque", displayName: "context-one.pdf", byteSize: 512, sha256: "c".repeat(64) },
      { handle: "asset_second_opaque", displayName: "context-two.pdf", byteSize: 768, sha256: "d".repeat(64) },
    ];
    let revision = 0;
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.planning.create") {
        return payload(method, { project_id: "planning-assets", state: "PLANNING_DRAFT", next_action: "Add reviewed evidence." });
      }
      if (method === "connectivity.project.get_state") {
        return payload(method, projectState("planning-assets", "Route 14 screen"));
      }
      if (method === "observability.connect") {
        return payload(method, { binding: { state: "BLOCKED_PROJECT_POLICY" }, upload_preview: { allowed: false, blockers: ["PLANNING_MODEL_INCOMPLETE"] } });
      }
      if (method === "connectivity.asset.add") {
        revision += 1;
        const asset = params.asset as { asset_id: string; sha256: string };
        return payload(method, { asset_id: asset.asset_id, revision, sha256: asset.sha256 });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    const chooseConnectivityAsset = vi.fn(async () => staged.shift() ?? null);
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: async () => ({ handle: "project_assets_opaque" }),
      chooseConnectivityAsset,
    };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={{ preflightId: "preflight_assets", entity: "team", project: "eco", deployment: "SELF_MANAGED" }} />);
    await createPlanningRecord(user);
    await user.click(screen.getAllByRole("button", { name: "Source assets" })[0]);
    await user.click(screen.getByRole("button", { name: "Choose reviewed asset" }));
    expect(screen.getByRole("button", { name: "Persist reviewed asset" })).toBeDisabled();
    await completeAssetAuthorization(user, "Reviewed context memorandum one");
    await user.click(screen.getByRole("button", { name: "Persist reviewed asset" }));
    expect(await screen.findByText("Reviewed context memorandum one")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add another asset" }));
    await completeAssetAuthorization(user, "Reviewed context memorandum two");
    await user.click(screen.getByRole("button", { name: "Persist reviewed asset" }));
    expect(await screen.findByText("Reviewed context memorandum two")).toBeInTheDocument();
    expect(chooseConnectivityAsset).toHaveBeenCalledTimes(2);

    const assetCalls = request.mock.calls.filter(([method]) => method === "connectivity.asset.add");
    expect(assetCalls).toHaveLength(2);
    expect(assetCalls[0][1]).toMatchObject({
      project_handle: "project_assets_opaque",
      source_handle: "asset_first_opaque",
      asset: {
        kind: "DOCUMENT",
        media_type: "application/pdf",
        grid: null,
        authorization: {
          cloud_disposition: "PROHIBITED",
          export_allowed: false,
          approved_deployments: ["LOCAL_ONLY"],
          reviewer: "Agency ecologist",
        },
      },
    });
    expect(JSON.stringify(assetCalls)).not.toContain("source_path");
    expect(JSON.stringify(assetCalls)).not.toContain("project_directory");
  });

  it("previews, authorizes, and downloads an allowlisted provider asset as context only", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.planning.create") return payload(method, { project_id: "planning-provider", state: "PLANNING_DRAFT", next_action: "Add reviewed evidence." });
      if (method === "connectivity.project.get_state") return payload(method, projectState("planning-provider", "Route 14 screen"));
      if (method === "observability.connect") return payload(method, { binding: { state: "BLOCKED_PROJECT_POLICY" }, upload_preview: { allowed: false, blockers: ["PLANNING_MODEL_INCOMPLETE"] } });
      if (method === "connectivity.asset.download") {
        return payload(method, {
          asset: {
            asset_id: params.asset_id,
            kind: "VECTOR_CONTEXT",
            title: params.title,
            relative_path: `data_assets/provider/usgs_3dep/${params.asset_id}`,
            sha256: "9".repeat(64),
            byte_size: 1012,
            media_type: params.media_type,
            source: params.source_url,
            vintage: params.selected_vintage,
            license: "U.S. federal public-domain data; preserve source metadata",
            grid: null,
            authorization: params.authorization,
          },
          context_only: true,
        });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"], createProjectWorkspace: async () => ({ handle: "project_provider_opaque" }) };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={{ preflightId: "preflight_provider", entity: "team", project: "eco", deployment: "SELF_MANAGED" }} />);
    await createPlanningRecord(user);
    await user.click(screen.getAllByRole("button", { name: "Source assets" })[0]);
    await user.click(screen.getByRole("button", { name: "Review provider request" }));
    expect(screen.getByText("3D Elevation Program (3DEP)")).toBeInTheDocument();
    expect(screen.getByText("Terrain and boundary context only")).toBeInTheDocument();
    const sourceUrl = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n39w078/USGS_13_n39w078.tif";
    await user.type(screen.getByLabelText(/Exact HTTPS download URL/i), sourceUrl);
    await user.type(screen.getByLabelText("Download extent *"), "Generalized county study extent");
    await user.type(screen.getByLabelText("Expected bytes *"), "1024");
    await user.type(screen.getByLabelText("Hard download maximum bytes *"), "2048");
    await user.type(screen.getByLabelText("Asset title *"), "Reviewed terrain context");
    await user.type(screen.getByLabelText("Owner or custodian *"), "U.S. Geological Survey");
    await user.selectOptions(screen.getByLabelText("Sensitivity *"), "PUBLIC");
    await user.selectOptions(screen.getByLabelText("Cloud permission *"), "PROHIBITED");
    await user.selectOptions(screen.getByLabelText("Export permission *"), "BLOCK");
    await user.type(screen.getByLabelText("Retention policy *"), "Retain exact source through project close.");
    await user.type(screen.getByLabelText("Spatial redaction or explicit none *"), "Public generalized context; no additional redaction.");
    fireEvent.change(screen.getByLabelText("Reviewed at *"), { target: { value: "2026-08-03T11:00" } });
    await user.click(screen.getByLabelText("Local project only"));
    await user.click(screen.getByLabelText(/I reviewed the source, license, authorization/i));
    await user.click(screen.getByLabelText(/Download this contextual source and retain its exact bytes/i));
    await user.click(screen.getByRole("button", { name: "Consent and download" }));
    expect(await screen.findByText(/Downloaded and checksum-retained 1,012 bytes as context only/i)).toBeInTheDocument();

    const call = request.mock.calls.find(([method]) => method === "connectivity.asset.download");
    expect(call?.[1]).toMatchObject({
      project_handle: "project_provider_opaque",
      provider_id: "usgs_3dep",
      source_url: sourceUrl,
      extent_description: "Generalized county study extent",
      expected_bytes: 1024,
      maximum_bytes: 2048,
      kind: "VECTOR_CONTEXT",
      explicit_consent: true,
      authorization: { cloud_disposition: "PROHIBITED", approved_deployments: ["LOCAL_ONLY"], license_spdx_or_name: "U.S. federal public-domain data; preserve source metadata" },
    });
    expect(JSON.stringify(call)).not.toContain("project_directory");
  });

  it("inspects exact raster bytes, previews alignment, and materializes only an approved semantic revision", async () => {
    const user = userEvent.setup();
    const staged = { handle: "resistance_opaque", displayName: "reviewed-resistance.asc", byteSize: 824, sha256: "e".repeat(64) };
    let persistedAsset: Record<string, unknown> | null = null;
    let previewPlan: Record<string, unknown> | null = null;
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "connectivity.planning.create") {
        return payload(method, { project_id: "planning-raster", state: "PLANNING_DRAFT", next_action: "Add reviewed evidence." });
      }
      if (method === "connectivity.project.get_state") {
        return payload(method, projectState("planning-raster", "Route 14 screen"));
      }
      if (method === "observability.connect") {
        return payload(method, { binding: { state: "BLOCKED_PROJECT_POLICY" }, upload_preview: { allowed: false, blockers: ["PLANNING_MODEL_INCOMPLETE"] } });
      }
      if (method === "connectivity.asset.inspect") {
        const inspection: RasterInspection = {
          format: "ESRI_ASCII",
          sha256: staged.sha256,
          byte_size: staged.byteSize,
          grid: {
            grid_id: String(params.grid_id),
            crs: "EPSG:5070",
            width: 8,
            height: 6,
            transform: [30, 0, 500000, 0, -30, 4200000],
            nodata: -9999,
            value_kind: "CONTINUOUS",
            cell_area_m2: 900,
          },
          data_cell_count: 46,
          nodata_cell_count: 2,
          minimum: 1,
          maximum: 100,
          unique_value_count: 12,
        };
        return payload(method, { inspection, transformed: false, next_action: "Persist the reviewed asset." });
      }
      if (method === "connectivity.asset.add") {
        const asset = params.asset as { asset_id: string; sha256: string };
        persistedAsset = params.asset as Record<string, unknown>;
        return payload(method, { asset_id: asset.asset_id, revision: 2, sha256: asset.sha256 });
      }
      if (method === "connectivity.grid.preview") {
        const target = params.target_grid as RasterInspection["grid"];
        previewPlan = {
          plan_id: params.plan_id,
          source_asset_id: params.source_asset_id,
          source_sha256: staged.sha256,
          source_grid: target,
          target_grid: target,
          method: params.method,
          source_cell_count: 48,
          target_cell_count: 48,
          source_nodata_count: 2,
          estimated_target_nodata_count: 2,
          categorical_counts_before: {},
          categorical_counts_after: {},
          resulting_sha256: "f".repeat(64),
          warnings: [],
        };
        return payload(method, { plan: previewPlan });
      }
      if (method === "connectivity.grid.approve") {
        const source = persistedAsset!;
        const authorization = params.derived_authorization as Record<string, unknown>;
        const targetGrid = previewPlan!.target_grid;
        return payload(method, {
          approval: {
            approval_id: params.approval_id,
            plan_id: params.plan_id,
            plan_sha256: "1".repeat(64),
            reviewer: params.reviewer,
            approved_at: params.approved_at,
            acknowledged_warnings: [],
            purpose: params.purpose,
          },
          derived_asset: {
            ...source,
            asset_id: params.derived_asset_id,
            title: params.derived_title,
            relative_path: params.derived_relative_path,
            sha256: "f".repeat(64),
            byte_size: 826,
            grid: targetGrid,
            authorization,
          },
          semantic_diff: {
            diff_id: "diff_alignment_1",
            from_revision_id: "planning-revision",
            to_revision_id: params.revision_id,
            changes: [{ object_type: "asset", object_id: params.derived_asset_id, field: "grid", before: null, after: String((targetGrid as RasterInspection["grid"]).grid_id), rationale: params.reason }],
            sha256: "2".repeat(64),
          },
          revision: {
            revision_id: params.revision_id,
            parent_revision_id: "planning-revision",
            project_id: "planning-raster",
            created_at: "2026-08-03T14:00:00Z",
            created_by: params.reviewer,
            reason: params.reason,
            model_sha256: "3".repeat(64),
            diff_id: "diff_alignment_1",
          },
          original_asset_retained: source.asset_id,
        });
      }
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = {
      request: request as unknown as HostBridge["request"],
      createProjectWorkspace: async () => ({ handle: "project_raster_opaque" }),
      chooseConnectivityAsset: async () => staged,
    };

    render(<Workspace mode="desktop" path="planning" localPreview={false} preflight={{ preflightId: "preflight_raster", entity: "team", project: "eco", deployment: "SELF_MANAGED" }} />);
    await createPlanningRecord(user);
    await user.click(screen.getAllByRole("button", { name: "Source assets" })[0]);
    await user.click(screen.getByRole("button", { name: "Choose reviewed asset" }));
    await completeRasterAuthorization(user, "Reviewed resistance hypothesis");
    expect(screen.getByRole("button", { name: "Persist reviewed asset" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Inspect raster grid" }));
    expect(await screen.findByText(/scientific worker read the retained source bytes without transformation/i)).toBeInTheDocument();
    expect(screen.getByText("8 × 6 · EPSG:5070")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Persist reviewed asset" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Persist reviewed asset" }));
    expect(await screen.findByText("Reviewed resistance hypothesis")).toBeInTheDocument();

    const inspectCall = request.mock.calls.find(([method]) => method === "connectivity.asset.inspect");
    expect(inspectCall?.[1]).toMatchObject({
      project_handle: "project_raster_opaque",
      source_handle: "resistance_opaque",
      value_kind: "CONTINUOUS",
      format: "ESRI_ASCII",
      esri_ascii_crs: "EPSG:5070",
      esri_ascii_cell_area_m2: 900,
    });
    const addCall = request.mock.calls.find(([method]) => method === "connectivity.asset.add");
    expect(addCall?.[1]).toMatchObject({
      project_handle: "project_raster_opaque",
      source_handle: "resistance_opaque",
      asset: {
        kind: "RESISTANCE_RASTER",
        grid: { width: 8, height: 6, crs: "EPSG:5070", value_kind: "CONTINUOUS", cell_area_m2: 900 },
      },
    });
    await user.click(screen.getAllByRole("button", { name: "Grid alignment" })[0]);
    await user.click(screen.getByRole("button", { name: /Preview alignment plan/i }));
    expect(await screen.findByText("Explicit no-change confirmation")).toBeInTheDocument();
    expect(screen.getByText(/No derived raster exists until a reviewer approves/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Approved at *"), { target: { value: "2026-08-03T10:00" } });
    await user.type(screen.getByLabelText("Purpose *"), "Explicitly confirm the reviewed source grid without resampling.");
    await user.type(screen.getByLabelText("Semantic revision reason *"), "Retain original and create an auditable no-change derivative.");
    await user.click(screen.getByRole("button", { name: "Approve exact plan and create revision" }));
    expect(await screen.findByText(/retained source .* remains immutable/i)).toBeInTheDocument();
    expect(screen.getByText("Plan approved")).toBeInTheDocument();

    const previewCall = request.mock.calls.find(([method]) => method === "connectivity.grid.preview");
    expect(previewCall?.[1]).toMatchObject({
      project_handle: "project_raster_opaque",
      source_asset_id: (addCall?.[1] as { asset: { asset_id: string } }).asset.asset_id,
      method: "NONE",
      target_grid: { width: 8, height: 6, crs: "EPSG:5070", value_kind: "CONTINUOUS", cell_area_m2: 900 },
    });
    const approveCall = request.mock.calls.find(([method]) => method === "connectivity.grid.approve");
    expect(approveCall?.[1]).toMatchObject({
      project_handle: "project_raster_opaque",
      plan_id: (previewCall?.[1] as { plan_id: string }).plan_id,
      derived_relative_path: expect.stringMatching(/^data_assets\/derived\/.+\.asc$/),
      derived_authorization: { cloud_disposition: "PROHIBITED", approved_deployments: ["LOCAL_ONLY"] },
      purpose: "Explicitly confirm the reviewed source grid without resampling.",
    });
    expect(JSON.stringify([inspectCall, addCall, previewCall, approveCall])).not.toContain("source_path");
    expect(JSON.stringify([inspectCall, addCall, previewCall, approveCall])).not.toContain("project_directory");
  });

  it("never presents Ridgeway fixture scenarios as a professional project's evidence", async () => {
    const user = userEvent.setup();
    render(<Workspace mode="desktop" path="existing" localPreview={false} preflight={{ preflightId: "preflight_3", entity: "team", project: "eco", deployment: "DEDICATED" }} />);
    await user.click(screen.getByRole("button", { name: "Ecological scenarios" }));
    expect(screen.getByText(/No fixture scenarios have been copied/i)).toBeInTheDocument();
    expect(screen.queryByText("RW-TG-A-H1-NOW")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Robustness board" }));
    expect(screen.getByText(/Ridgeway teaching results are not project results/i)).toBeInTheDocument();
  });
});

describe("accessibility floor", () => {
  it("has no automatic serious or critical axe findings on Home", async () => {
    const { container } = render(<EcoApp />);
    const results = await axe.run(container, { resultTypes: ["violations"] });
    const blocking = results.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
    expect(blocking).toEqual([]);
  });

  it("has no automatic serious or critical axe findings on the robustness board", async () => {
    const user = userEvent.setup();
    const { container } = render(<EcoApp mode="public-replay" publicFixture={publicRidgewayFixture} />);
    await user.click(screen.getByRole("button", { name: "Robustness board" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Robustness Board" })).toBeInTheDocument());
    const results = await axe.run(container, { resultTypes: ["violations"] });
    const blocking = results.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
    expect(blocking).toEqual([]);
  });
});
