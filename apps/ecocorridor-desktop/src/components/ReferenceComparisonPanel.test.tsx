import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { HostBridge, ProfessionalProjectState } from "../types";
import { freshReferenceAnalysisId, ReferenceComparisonPanel, referenceAnalysisId } from "./ReferenceComparisonPanel";

const sha = (character: string) => character.repeat(64);
const projectId = "reviewed-connectivity-project";
const revisionId = "revision-reviewed-4";
const analysisId = referenceAnalysisId(projectId, revisionId);
const planId = "reference-plan-reviewed-4";
const tolerance = {
  metric: "EFFECTIVE_RESISTANCE" as const,
  units: "effective_resistance" as const,
  absolute_tolerance: 0.1,
  relative_tolerance: 0.01,
  comparison_formula: "ABS_OBSERVED_MINUS_REFERENCE_LE_ABSOLUTE_PLUS_RELATIVE_TIMES_ABS_REFERENCE" as const,
};

const binding = {
  binding_id: "binding-reviewed-present",
  scenario_id: "scenario-reviewed-present",
  reference_matrix_asset_id: "asset-reference-matrix",
  matrix_format: "CIRCUITSCAPE_THREE_COLUMN_MATRIX" as const,
  tolerance,
  reference_current_asset_id: null,
  current_raster_disposition: "NOT_PROVIDED" as const,
  current_raster_numeric_comparison_allowed: false as const,
  reference_basis: "Agency-approved baseline matrix retained with the project bundle.",
  reviewed_by: "Agency connectivity reviewer",
  reviewed_at: "2026-08-03T12:00:00Z",
  limitations: ["Reference reproduction does not establish ecological validity."],
};

const scenario = {
  scenario_id: binding.scenario_id,
  title: "Forest guild · present-day reviewed hypothesis",
  guild_id: "guild-forest",
  life_stage: "adult",
  resistance_hypothesis_id: "hypothesis-reviewed",
  focal_set_id: "focal-set-reviewed",
  future_condition: "present",
  maturity_horizon: "current",
  included_pair_ids: ["pair-1-2"],
  safeguard: false,
  grid_id: "grid-reviewed",
  engine_mode: "RASTER_PAIRWISE" as const,
  raster_semantics: "RESISTANCE" as const,
  neighbor_rule: "EIGHT" as const,
  resistance_average: "ARITHMETIC" as const,
  precision: "double" as const,
  julia_threads: 1 as const,
  blas_threads: 1 as const,
};

const runPlan = {
  analysis_id: analysisId,
  revision_id: revisionId,
  scenario_ids: [scenario.scenario_id],
  portfolios: [{
    portfolio_id: "portfolio-no-action",
    selections: { "site-reviewed": "option-no-action" },
    canonical_option_ids: ["option-no-action"],
    contains_survey_first: false,
  }],
  baseline_portfolio_id: "portfolio-no-action",
  direct_job_count: 1,
  solver_job_cap: 1,
  search_method: "ENUMERATION" as const,
  seed: 17,
  current_maps_for_portfolio_ids: [],
  engine_manifest_sha256: sha("e"),
  search_resource_observation: null,
};

const plan = {
  plan_id: planId,
  analysis_id: analysisId,
  revision_id: revisionId,
  model_sha256: sha("b"),
  engine_manifest_sha256: sha("e"),
  binding_ids: [binding.binding_id],
  bound_scenario_ids: [scenario.scenario_id],
  run_plan: runPlan,
  exact_direct_evidence_only: true as const,
  created_at: "2026-08-03T12:05:00Z",
};

const projectState = {
  project: { project_id: projectId, title: "Reviewed connectivity project", mode: "IMPORTED" },
  active_revision: {
    revision_id: revisionId,
    project_id: projectId,
    created_at: "2026-08-03T12:00:00Z",
    created_by: "Agency connectivity reviewer",
    reason: "Reviewed import",
    model_sha256: sha("b"),
  },
  effective_bundle: {
    scenarios: [scenario],
    reference_bindings: [binding],
  },
  analyses: [],
  pending_recovery: { required: false, analyses: [], next_action: "No interrupted analysis requires recovery." },
} as unknown as ProfessionalProjectState;

function planResponse(state: "READY" | "COMPLETED" = "READY") {
  return {
    analysis_id: analysisId,
    plan_id: planId,
    state,
    plan,
    plan_sha256: sha("c"),
    reference_matrix_parser_sha256: sha("d"),
    replayed: state !== "READY",
    arithmetic: "1 No Action baseline × 1 bound scenarios = 1 direct Circuitscape matrix jobs",
  };
}

function planResponseFor(candidateAnalysisId: string, candidatePlanId: string) {
  const candidateRunPlan = { ...runPlan, analysis_id: candidateAnalysisId };
  const candidatePlan = { ...plan, analysis_id: candidateAnalysisId, plan_id: candidatePlanId, run_plan: candidateRunPlan };
  return {
    ...planResponse(),
    analysis_id: candidateAnalysisId,
    plan_id: candidatePlanId,
    plan: candidatePlan,
  };
}

function statusResponseFor(candidateAnalysisId: string, candidatePlanId: string, state: "READY" | "RUNNING" | "PAUSING" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED", disposition: "PENDING" | "HISTORICAL_STALE" = "PENDING") {
  const candidate = statusResponse(state, disposition);
  return {
    ...candidate,
    analysis_id: candidateAnalysisId,
    plan_id: candidatePlanId,
    plan: { ...candidate.plan, analysis_id: candidateAnalysisId, plan_id: candidatePlanId, run_plan: { ...candidate.plan.run_plan, analysis_id: candidateAnalysisId } },
  };
}

function statusResponse(state: "READY" | "RUNNING" | "PAUSING" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED", disposition: "PENDING" | "HISTORICAL_STALE" = "PENDING") {
  const completed = state === "COMPLETED";
  return {
    analysis_id: analysisId,
    plan_id: planId,
    state,
    plan,
    completed_run_count: completed ? 1 : 0,
    direct_job_counts: completed ? { SUCCEEDED: 1 } : state === "READY" ? {} : { [state]: 1 },
    result_id: completed ? "reference-result-reviewed" : null,
    result_status: completed ? "PASSED" as const : null,
    active_for_current_model: disposition === "PENDING",
    gates_model_fitness: false,
    evidence_disposition: disposition,
    stale_reasons: disposition === "HISTORICAL_STALE" ? ["ACTIVE_ENGINE_MISMATCH"] : [],
    error: null,
  };
}

const comparison = {
  comparison_id: "comparison-reviewed-present",
  analysis_id: analysisId,
  revision_id: revisionId,
  model_sha256: sha("b"),
  binding_id: binding.binding_id,
  scenario_id: scenario.scenario_id,
  direct_run_id: "direct-run-reference-present",
  reference_matrix_asset_id: binding.reference_matrix_asset_id,
  reference_matrix_sha256: sha("1"),
  observed_matrix_sha256: sha("2"),
  engine_manifest_sha256: sha("e"),
  tolerance,
  pair_comparisons: [{
    pair_id: "pair-1-2",
    reference_effective_resistance: 100,
    observed_effective_resistance: 100.5,
    tolerance,
    absolute_difference: 0.5,
    relative_difference: 0.005,
    allowed_difference: 1.1,
    within_tolerance: true,
  }],
  qa_status: "ACCEPTED" as const,
  status: "PASSED" as const,
  reference_current_asset_id: null,
  current_raster_disposition: "NOT_PROVIDED" as const,
  current_raster_numeric_comparison_performed: false as const,
  exact_direct_evidence_only: true as const,
  completed_at: "2026-08-03T12:06:00Z",
  limitations: ["This comparison evaluates numerical reproduction only."],
};

function resultResponse(disposition: "ACTIVE_PASSED" | "HISTORICAL_STALE" = "ACTIVE_PASSED") {
  return {
    analysis_id: analysisId,
    plan_id: planId,
    plan,
    result: {
      result_id: "reference-result-reviewed",
      plan_id: planId,
      analysis_id: analysisId,
      revision_id: revisionId,
      model_sha256: sha("b"),
      engine_manifest_sha256: sha("e"),
      scenario_comparisons: [comparison],
      status: "PASSED" as const,
      exact_direct_evidence_only: true as const,
      current_raster_numeric_comparison_performed: false as const,
      evidence_claim_links: [{
        claim_id: "claim-reference-pair-1-2",
        comparison_id: comparison.comparison_id,
        scenario_id: scenario.scenario_id,
        pair_id: "pair-1-2",
        direct_run_id: comparison.direct_run_id,
      }],
      completed_at: "2026-08-03T12:06:00Z",
      limitations: ["Reference reproduction does not validate ecological assumptions."],
    },
    claim_ids: ["claim-reference-pair-1-2"],
    plan_sha256: sha("c"),
    result_sha256: sha("f"),
    reference_matrix_parser_sha256: sha("d"),
    outbox_id: "outbox-reference-result",
    replayed: false,
    active_for_current_model: disposition === "ACTIVE_PASSED",
    gates_model_fitness: disposition === "ACTIVE_PASSED",
    evidence_disposition: disposition,
    stale_reasons: disposition === "HISTORICAL_STALE" ? ["ACTIVE_ENGINE_MISMATCH"] : [],
  };
}

function payload(method: string, data: unknown) {
  return { api_version: "1.0", method, data, data_sha256: sha("a") };
}

function installHost(handler: (method: string, params: Record<string, unknown>) => Promise<unknown>) {
  const request = vi.fn(handler);
  window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };
  return request;
}

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
  vi.useRealTimers();
});

test("plans one immutable baseline ensemble from the project and active revision", async () => {
  const request = installHost(async (method) => {
    if (method === "connectivity.reference.plan") return payload(method, planResponse());
    throw new Error(`Unexpected method ${method}`);
  });
  const user = userEvent.setup();
  render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={projectState} onEvidence={vi.fn()} />);

  expect(screen.getByText("Compare the active model with reviewed reference matrices")).toBeInTheDocument();
  expect(request).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Plan exact reference check" }));

  expect(await screen.findByRole("button", { name: "Run 1 direct matrix job" })).toBeInTheDocument();
  expect(screen.getByText(planResponse().arithmetic)).toBeInTheDocument();
  expect(request).toHaveBeenCalledWith("connectivity.reference.plan", {
    project_handle: "opaque-project-handle",
    analysis_id: analysisId,
  });
  expect(referenceAnalysisId(projectId, revisionId)).toBe(analysisId);
  expect(referenceAnalysisId(projectId, revisionId)).not.toBe(referenceAnalysisId(projectId, `${revisionId}-new`));
});

test("shows durable progress, loads the exact tolerance table, and opens each direct claim", async () => {
  let statusCalls = 0;
  const request = installHost(async (method) => {
    if (method === "connectivity.reference.plan") return payload(method, planResponse());
    if (method === "connectivity.reference.start") return payload(method, { analysis_id: analysisId, plan_id: planId, state: "RUNNING" });
    if (method === "connectivity.reference.status") {
      statusCalls += 1;
      return payload(method, statusCalls === 1 ? statusResponse("RUNNING") : { ...statusResponse("COMPLETED"), active_for_current_model: true, gates_model_fitness: true, evidence_disposition: "ACTIVE_PASSED" });
    }
    if (method === "connectivity.reference.result") return payload(method, resultResponse());
    throw new Error(`Unexpected method ${method}`);
  });
  const onEvidence = vi.fn();
  const user = userEvent.setup();
  const { container } = render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={projectState} onEvidence={onEvidence} pollIntervalMs={15} />);

  await user.click(screen.getByRole("button", { name: "Plan exact reference check" }));
  await user.click(await screen.findByRole("button", { name: "Run 1 direct matrix job" }));
  expect(await screen.findByText("0 of 1 complete")).toBeInTheDocument();
  expect(screen.getByRole("progressbar", { name: "Exact reference jobs completed" })).toHaveAttribute("value", "0");

  expect((await screen.findAllByText("Active reference match passed", {}, { timeout: 2_000 })).length).toBeGreaterThanOrEqual(1);
  expect(screen.getByRole("region", { name: "Exact baseline versus reviewed reference comparisons" })).toBeInTheDocument();
  expect(screen.getByText("100.000000")).toBeInTheDocument();
  expect(screen.getByText("100.500000")).toBeInTheDocument();
  expect(screen.getByText("0.500000000")).toBeInTheDocument();
  expect(screen.getByText("≤ 1.10000000")).toBeInTheDocument();
  expect(screen.getByText("Within tolerance")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Trace exact claim" }));
  expect(onEvidence).toHaveBeenCalledWith(expect.objectContaining({
    claimId: "claim-reference-pair-1-2",
    value: true,
    units: "within_tolerance",
    scenarioId: scenario.scenario_id,
    runId: comparison.direct_run_id,
    source: "PROFESSIONAL_CLAIM",
  }));
  expect(request.mock.calls.filter(([method]) => method === "connectivity.reference.status").length).toBeGreaterThanOrEqual(2);

  const accessibility = await axe.run(container);
  expect(accessibility.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
});

test("keeps a replayed stale result visible without allowing it to gate Model Fitness", async () => {
  const request = installHost(async (method) => {
    if (method === "connectivity.reference.plan") return payload(method, planResponse("COMPLETED"));
    if (method === "connectivity.reference.status") return payload(method, statusResponse("COMPLETED", "HISTORICAL_STALE"));
    if (method === "connectivity.reference.result") return payload(method, resultResponse("HISTORICAL_STALE"));
    throw new Error(`Unexpected method ${method}`);
  });
  const user = userEvent.setup();
  render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={projectState} onEvidence={vi.fn()} />);

  await user.click(screen.getByRole("button", { name: "Plan exact reference check" }));
  expect((await screen.findAllByText("Historical result — not current")).length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("ACTIVE_ENGINE_MISMATCH")).toBeInTheDocument();
  expect(screen.getByText(/cannot gate the current Model Fitness Passport/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Trace exact claim" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: /run .*direct matrix/i })).not.toBeInTheDocument();
  await waitFor(() => expect(request).toHaveBeenCalledWith("connectivity.reference.result", expect.objectContaining({ analysis_id: analysisId, plan_id: planId })));
});

test("plans a fresh immutable attempt from stale evidence without reusing or mutating the old analysis", async () => {
  let freshAnalysisId = "";
  const request = installHost(async (method, params) => {
    if (method === "connectivity.reference.status") return payload(method, statusResponse("COMPLETED", "HISTORICAL_STALE"));
    if (method === "connectivity.reference.result") return payload(method, resultResponse("HISTORICAL_STALE"));
    if (method === "connectivity.reference.plan") {
      freshAnalysisId = String(params.analysis_id);
      return payload(method, planResponseFor(freshAnalysisId, "reference-plan-fresh-attempt"));
    }
    throw new Error(`Unexpected method ${method}`);
  });
  const user = userEvent.setup();
  const retainedState = {
    ...projectState,
    analyses: [{
      analysis_id: analysisId,
      analysis_kind: "REFERENCE" as const,
      plan_id: planId,
      revision_id: revisionId,
      engine_manifest_sha256: sha("e"),
      state: "COMPLETED",
      created_at: "2026-08-03T12:05:00Z",
      updated_at: "2026-08-03T12:06:00Z",
    }],
  };

  render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={retainedState} onEvidence={vi.fn()} />);

  expect(await screen.findByRole("button", { name: "Trace exact claim" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Plan new exact reference attempt" }));

  expect(await screen.findByRole("button", { name: "Run 1 direct matrix job" })).toBeEnabled();
  expect(freshAnalysisId).toMatch(new RegExp(`^${analysisId}_attempt_[A-Za-z0-9]{16,32}$`));
  expect(freshAnalysisId).not.toBe(analysisId);
  expect(request).toHaveBeenCalledWith("connectivity.reference.plan", {
    project_handle: "opaque-project-handle",
    analysis_id: freshAnalysisId,
  });
  expect(request.mock.calls.some(([method]) => ["connectivity.analysis.cancel", "connectivity.analysis.resume", "connectivity.analysis.pause"].includes(method))).toBe(false);
});

test("fresh reference attempt IDs preserve stable scope while changing immutable identity", () => {
  const first = freshReferenceAnalysisId(projectId, revisionId, "123e4567-e89b-12d3-a456-426614174000");
  const second = freshReferenceAnalysisId(projectId, revisionId, "987f6543-e21b-45d3-b654-123456789abc");

  expect(first).toMatch(new RegExp(`^${analysisId}_attempt_`));
  expect(second).toMatch(new RegExp(`^${analysisId}_attempt_`));
  expect(first).not.toBe(second);
});

test("fresh reference attempt IDs stay inside the RPC contract for maximum-length identities", () => {
  const value = freshReferenceAnalysisId("p".repeat(80), "r".repeat(80), "123e4567-e89b-12d3-a456-426614174000");

  expect(value.length).toBeLessThanOrEqual(128);
  expect(value).toMatch(/^[A-Za-z0-9_.:-]+$/);
  expect(value).toContain("_attempt_123e4567e89b12d3a456426");
});

test("restores a retained reference plan by kind and plan ID without recreating it", async () => {
  const newerAnalysisId = freshReferenceAnalysisId(projectId, revisionId, "123e4567-e89b-12d3-a456-426614174000");
  const newerPlanId = "reference-plan-reviewed-newer";
  const retainedState = {
    ...projectState,
    analyses: [
      {
        analysis_id: analysisId,
        analysis_kind: "REFERENCE" as const,
        plan_id: planId,
        revision_id: revisionId,
        engine_manifest_sha256: sha("e"),
        state: "COMPLETED",
        created_at: "2026-08-03T12:05:00Z",
        updated_at: "2026-08-03T12:05:00Z",
      },
      {
        analysis_id: newerAnalysisId,
        analysis_kind: "REFERENCE" as const,
        plan_id: newerPlanId,
        revision_id: revisionId,
        engine_manifest_sha256: sha("e"),
        state: "READY",
        created_at: "2026-08-03T13:05:00Z",
        updated_at: "2026-08-03T13:05:00Z",
      },
    ],
  };
  const request = installHost(async (method) => {
    if (method === "connectivity.reference.status") return payload(method, statusResponseFor(newerAnalysisId, newerPlanId, "READY"));
    throw new Error(`Unexpected method ${method}`);
  });

  render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={retainedState} onEvidence={vi.fn()} />);

  expect(await screen.findByRole("button", { name: "Run 1 direct matrix job" })).toBeEnabled();
  expect(request).toHaveBeenCalledWith("connectivity.reference.status", {
    project_handle: "opaque-project-handle",
    analysis_id: newerAnalysisId,
    plan_id: newerPlanId,
  });
  expect(request.mock.calls.some(([method]) => method === "connectivity.reference.plan")).toBe(false);
});

test("recovers, pauses, and cancels retained direct reference work through durable analysis controls", async () => {
  const retainedState = {
    ...projectState,
    analyses: [{
      analysis_id: analysisId,
      analysis_kind: "REFERENCE" as const,
      plan_id: planId,
      revision_id: revisionId,
      engine_manifest_sha256: sha("e"),
      state: "RUNNING",
      created_at: "2026-08-03T12:05:00Z",
      updated_at: "2026-08-03T12:06:00Z",
    }],
    pending_recovery: {
      required: true,
      analyses: [{ analysis_id: analysisId, analysis_kind: "REFERENCE" as const, plan_id: planId, revision_id: revisionId, analysis_state: "RUNNING" as const, executor_active: false, recovery_required: true, running_job_ids: ["reference-job-1"] }],
      next_action: "Resume or cancel the interrupted reference analysis.",
    },
  };
  let state: "RUNNING" | "PAUSED" | "CANCELLED" = "RUNNING";
  const request = installHost(async (method) => {
    if (method === "connectivity.reference.status") return payload(method, statusResponse(state));
    if (method === "connectivity.analysis.resume") { state = "RUNNING"; return payload(method, { analysis_id: analysisId, state }); }
    if (method === "connectivity.analysis.pause") { state = "PAUSED"; return payload(method, { analysis_id: analysisId, state }); }
    if (method === "connectivity.analysis.cancel") { state = "CANCELLED"; return payload(method, { analysis_id: analysisId, state }); }
    throw new Error(`Unexpected method ${method}`);
  });
  const user = userEvent.setup();

  render(<ReferenceComparisonPanel projectHandle="opaque-project-handle" projectState={retainedState} onEvidence={vi.fn()} pollIntervalMs={60_000} />);

  await user.click(await screen.findByRole("button", { name: "Resume and recover exact jobs" }));
  await user.click(await screen.findByRole("button", { name: "Pause after active jobs" }));
  await user.click(await screen.findByRole("button", { name: "Cancel exact reference analysis" }));

  expect(request.mock.calls.map(([method]) => method).filter((method) => method.startsWith("connectivity.analysis."))).toEqual([
    "connectivity.analysis.resume",
    "connectivity.analysis.pause",
    "connectivity.analysis.cancel",
  ]);
  expect(await screen.findByText("Reference analysis cancelled")).toBeInTheDocument();
});
