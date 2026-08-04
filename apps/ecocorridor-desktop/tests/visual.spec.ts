import { expect, test } from "@playwright/test";
import axe from "axe-core";

interface ProfessionalVisualHostOptions {
  /** Browser-only RPC data for layout, keyboard, and axe checks; never engineering evidence. */
  referenceComparisonLayoutMock?: boolean;
}

async function installProfessionalVisualHost(
  page: import("@playwright/test").Page,
  { referenceComparisonLayoutMock = false }: ProfessionalVisualHostOptions = {},
) {
  await page.addInitScript(({ referenceComparisonLayoutMock }) => {
    let credentialStored = false;
    const response = (method: string, data: unknown) => ({ api_version: "1.0", method, data, data_sha256: "a".repeat(64) });
    const sha = (character: string) => character.repeat(64);
    const referencePlanId = "reference-plan-professional-visual";
    const referenceScenario = {
      scenario_id: "scenario-reviewed-present",
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
      engine_mode: "RASTER_PAIRWISE",
      raster_semantics: "RESISTANCE",
      neighbor_rule: "EIGHT",
      resistance_average: "ARITHMETIC",
      precision: "double",
      julia_threads: 1,
      blas_threads: 1,
    };
    const referenceTolerance = {
      metric: "EFFECTIVE_RESISTANCE",
      units: "effective_resistance",
      absolute_tolerance: 0.1,
      relative_tolerance: 0.01,
      comparison_formula: "ABS_OBSERVED_MINUS_REFERENCE_LE_ABSOLUTE_PLUS_RELATIVE_TIMES_ABS_REFERENCE",
    };
    const referenceBinding = {
      binding_id: "binding-reviewed-present",
      scenario_id: referenceScenario.scenario_id,
      reference_matrix_asset_id: "asset-reference-matrix",
      matrix_format: "CIRCUITSCAPE_THREE_COLUMN_MATRIX",
      tolerance: referenceTolerance,
      reference_current_asset_id: null,
      current_raster_disposition: "NOT_PROVIDED",
      current_raster_numeric_comparison_allowed: false,
      reference_basis: "Agency-approved baseline matrix retained with the project bundle.",
      reviewed_by: "Agency connectivity reviewer",
      reviewed_at: "2026-08-03T12:00:00Z",
      limitations: ["Reference reproduction does not establish ecological validity."],
    };
    let activeReferenceAnalysisId = "";
    const referenceCalls: string[] = [];
    if (referenceComparisonLayoutMock) {
      (window as typeof window & { __REFERENCE_LAYOUT_MOCK_CALLS__?: string[] }).__REFERENCE_LAYOUT_MOCK_CALLS__ = referenceCalls;
    }
    const referencePlan = (analysisId: string) => ({
      plan_id: referencePlanId,
      analysis_id: analysisId,
      revision_id: "revision_professional_visual",
      model_sha256: sha("1"),
      engine_manifest_sha256: sha("e"),
      binding_ids: [referenceBinding.binding_id],
      bound_scenario_ids: [referenceScenario.scenario_id],
      run_plan: {
        analysis_id: analysisId,
        revision_id: "revision_professional_visual",
        scenario_ids: [referenceScenario.scenario_id],
        portfolios: [{
          portfolio_id: "portfolio-no-action",
          selections: { "site-reviewed": "option-no-action" },
          canonical_option_ids: ["option-no-action"],
          contains_survey_first: false,
        }],
        baseline_portfolio_id: "portfolio-no-action",
        direct_job_count: 1,
        solver_job_cap: 1,
        search_method: "ENUMERATION",
        seed: 17,
        current_maps_for_portfolio_ids: [],
        engine_manifest_sha256: sha("e"),
        search_resource_observation: null,
      },
      exact_direct_evidence_only: true,
      created_at: "2026-08-03T12:05:00Z",
    });
    const referenceComparison = (analysisId: string) => ({
      comparison_id: "comparison-reviewed-present",
      analysis_id: analysisId,
      revision_id: "revision_professional_visual",
      model_sha256: sha("1"),
      binding_id: referenceBinding.binding_id,
      scenario_id: referenceScenario.scenario_id,
      direct_run_id: "direct-run-reference-present",
      reference_matrix_asset_id: referenceBinding.reference_matrix_asset_id,
      reference_matrix_sha256: sha("2"),
      observed_matrix_sha256: sha("3"),
      engine_manifest_sha256: sha("e"),
      tolerance: referenceTolerance,
      pair_comparisons: [{
        pair_id: "pair-1-2",
        reference_effective_resistance: 100,
        observed_effective_resistance: 100.5,
        tolerance: referenceTolerance,
        absolute_difference: 0.5,
        relative_difference: 0.005,
        allowed_difference: 1.1,
        within_tolerance: true,
      }],
      qa_status: "ACCEPTED",
      status: "PASSED",
      reference_current_asset_id: null,
      current_raster_disposition: "NOT_PROVIDED",
      current_raster_numeric_comparison_performed: false,
      exact_direct_evidence_only: true,
      completed_at: "2026-08-03T12:06:00Z",
      limitations: ["This comparison evaluates numerical reproduction only."],
    });
    (window as typeof window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {
      invoke: async (command: string) => {
        if (command === "credential_status") return credentialStored;
        if (command === "credential_store") {
          credentialStored = true;
          return null;
        }
        throw new Error(`Unexpected visual-test native command ${command}`);
      },
    };
    (window as typeof window & { __ECOCORRIDOR_HOST__?: unknown }).__ECOCORRIDOR_HOST__ = {
      createProjectWorkspace: async () => ({ handle: "professional_visual_project" }),
      request: async (method: string, params: Record<string, unknown> = {}) => {
        if (method === "observability.preflight") return response(method, {
          preflight_id: "preflight_professional_visual",
          scope: "RESTRICTED",
          allowed: true,
        });
        if (method === "connectivity.planning.create") return response(method, {
          project_id: "planning_professional_visual",
          state: "PLANNING_DRAFT",
          next_action: "Add reviewed source evidence.",
        });
        if (method === "connectivity.project.get_state") {
          const projectId = "planning_professional_visual";
          const revision = {
            revision_id: "revision_professional_visual",
            project_id: projectId,
            created_at: "2026-08-03T12:00:00Z",
            created_by: "Agency ecologist",
            reason: referenceComparisonLayoutMock
              ? "Browser-only reference-comparison layout fixture; not engineering evidence."
              : "Create authoritative visual-test project.",
            model_sha256: "1".repeat(64),
          };
          const decisionBrief = {
            brief_id: "brief_professional_visual",
            title: "Route 14 connectivity screen",
            decision_question: "Which reviewed portfolio should advance to feasibility review?",
            decision_owner: "Agency ecologist",
            intended_use: "Planning comparison only; not animal movement, collision, design, or approval evidence. Study context: County-scale generalized study extent.",
            target_guilds: ["Anonymous forest-associated focal guild"],
            safeguard_guilds: [],
            budget_amount: 250000,
            currency: "USD",
            cost_vintage: "2026",
            non_goals: ["Observed animal movement"],
            required_next_action: "Field and engineering feasibility review.",
            conceptual: true,
          };
          const fitness = {
            passport_id: "fitness_professional_visual",
            revision_id: revision.revision_id,
            intended_use: decisionBrief.intended_use,
            dimensions: [],
            unresolved_assumptions: ["Visual-test field assumption remains unresolved."],
            attestations: [],
            reviewed_by: null,
            reviewed_at: null,
          };
          const resourcePolicy = {
            max_input_bytes: 2147483648,
            max_materialized_bytes: 8589934592,
            max_current_raster_bytes: 4294967296,
            max_direct_jobs: 5000,
            max_wall_seconds_per_job: 3600,
            require_explicit_oversize_approval: true,
          };
          return response(method, {
            project: { project_id: projectId, title: decisionBrief.title, mode: referenceComparisonLayoutMock ? "IMPORTED" : "PLANNING_DRAFT" },
            active_revision: revision,
            effective_bundle: {
              schema_version: "1.0",
              project_id: projectId,
              title: decisionBrief.title,
              revision,
              decision_brief: decisionBrief,
              assets: [], resistance_hypotheses: [], focal_area_sets: [], scenarios: referenceComparisonLayoutMock ? [referenceScenario] : [],
              intervention_sites: [], intervention_options: [], cost_basis: null,
              safeguards: [], review_masks: [], decision_rules: [], fitness,
              reference_bindings: referenceComparisonLayoutMock ? [referenceBinding] : [],
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
          });
        }
        if (method === "observability.connect") return response(method, {
          binding: { state: "CONNECTED" },
          upload_preview: { allowed: true, blockers: [] },
        });
        if (referenceComparisonLayoutMock && method === "connectivity.reference.plan") {
          activeReferenceAnalysisId = String(params.analysis_id ?? "");
          referenceCalls.push(method);
          return response(method, {
            analysis_id: activeReferenceAnalysisId,
            plan_id: referencePlanId,
            state: "READY",
            plan: referencePlan(activeReferenceAnalysisId),
            plan_sha256: sha("4"),
            reference_matrix_parser_sha256: sha("5"),
            replayed: false,
            arithmetic: "1 No Action baseline × 1 bound scenarios = 1 direct Circuitscape matrix jobs",
          });
        }
        if (referenceComparisonLayoutMock && method === "connectivity.reference.start") {
          referenceCalls.push(method);
          return response(method, {
            analysis_id: activeReferenceAnalysisId,
            plan_id: referencePlanId,
            state: "RUNNING",
          });
        }
        if (referenceComparisonLayoutMock && method === "connectivity.reference.status") {
          referenceCalls.push(method);
          return response(method, {
            analysis_id: activeReferenceAnalysisId,
            plan_id: referencePlanId,
            state: "COMPLETED",
            plan: referencePlan(activeReferenceAnalysisId),
            completed_run_count: 1,
            direct_job_counts: { SUCCEEDED: 1 },
            result_id: "reference-result-reviewed",
            result_status: "PASSED",
            active_for_current_model: true,
            gates_model_fitness: true,
            evidence_disposition: "ACTIVE_PASSED",
            stale_reasons: [],
            error: null,
          });
        }
        if (referenceComparisonLayoutMock && method === "connectivity.reference.result") {
          referenceCalls.push(method);
          const comparison = referenceComparison(activeReferenceAnalysisId);
          return response(method, {
            analysis_id: activeReferenceAnalysisId,
            plan_id: referencePlanId,
            plan: referencePlan(activeReferenceAnalysisId),
            result: {
              result_id: "reference-result-reviewed",
              plan_id: referencePlanId,
              analysis_id: activeReferenceAnalysisId,
              revision_id: "revision_professional_visual",
              model_sha256: sha("1"),
              engine_manifest_sha256: sha("e"),
              scenario_comparisons: [comparison],
              status: "PASSED",
              exact_direct_evidence_only: true,
              current_raster_numeric_comparison_performed: false,
              evidence_claim_links: [{
                claim_id: "claim-reference-pair-1-2",
                comparison_id: comparison.comparison_id,
                scenario_id: referenceScenario.scenario_id,
                pair_id: "pair-1-2",
                direct_run_id: comparison.direct_run_id,
              }],
              completed_at: "2026-08-03T12:06:00Z",
              limitations: ["Reference reproduction does not validate ecological assumptions."],
            },
            claim_ids: ["claim-reference-pair-1-2"],
            plan_sha256: sha("4"),
            result_sha256: sha("6"),
            reference_matrix_parser_sha256: sha("5"),
            outbox_id: "outbox-reference-result",
            replayed: false,
            active_for_current_model: true,
            gates_model_fitness: true,
            evidence_disposition: "ACTIVE_PASSED",
            stale_reasons: [],
          });
        }
        throw new Error(`Unexpected professional visual-test RPC ${method}`);
      },
    };
  }, { referenceComparisonLayoutMock });
}

async function assertNoBlockingAxeFindings(page: import("@playwright/test").Page) {
  // Evaluate through Playwright's isolated test channel so the production
  // replay can retain `script-src 'self'` without an accessibility-test waiver.
  await page.evaluate(axe.source);
  const blocking = await page.evaluate(async () => {
    const results = await (window as typeof window & { axe: typeof axe }).axe.run(document);
    return results.violations
      .filter((item) => item.impact === "serious" || item.impact === "critical")
      .map((item) => ({ id: item.id, impact: item.impact, targets: item.nodes.map((node) => node.target) }));
  });
  expect(blocking).toEqual([]);
}

async function openProfessionalPlanning(
  page: import("@playwright/test").Page,
  options: ProfessionalVisualHostOptions = {},
) {
  await installProfessionalVisualHost(page, options);
  await page.goto("/");
  await page.getByRole("button", { name: "Assemble planning model" }).click();
  await page.getByLabel("API base URL").fill("https://api.wandb.example.org");
  await page.getByLabel("Entity or team").fill("wildlife-program");
  await page.getByRole("textbox", { name: "Project", exact: true }).fill("route-14-screening");
  await page.getByRole("button", { name: "Enter API key securely" }).click();
  await page.getByRole("checkbox", { name: /I will classify each asset/i }).check();
  await page.getByRole("button", { name: /Verify Restricted project/i }).click();
  await expect(page.getByRole("heading", { name: "Start with knowns, surface the unknowns." })).toBeVisible();
}

async function createProfessionalPlanningRecord(page: import("@playwright/test").Page) {
  const workflow = page.getByRole("navigation", { name: "Project workflow" });
  await workflow.getByRole("button", { name: "Study context" }).click();
  await page.getByLabel("Project name *").fill("Route 14 connectivity screen");
  await page.getByLabel("Generalized study area *").fill("County-scale generalized study extent");
  await page.getByLabel("Decision question *").fill("Which reviewed portfolio should advance to feasibility review?");
  await page.getByLabel("Intended use and scope boundary *").fill("Planning comparison only; not animal movement, collision, design, or approval evidence.");
  await page.getByLabel("Target guild or life stage *").fill("Anonymous forest-associated focal guild");
  await page.getByLabel("Responsible ecological reviewer *").fill("Agency ecologist");
  await page.getByRole("button", { name: "Create planning record" }).click();
  await expect(page.getByText(/Authoritative local draft created/i)).toBeVisible();
  await expect(page.locator(".cloud-state.connected")).toHaveCount(1);
}

async function openBrowserMockedReferenceComparison(page: import("@playwright/test").Page) {
  // This deliberately exercises only browser rendering, accessibility, and
  // keyboard mechanics. Its reviewed-reference values are not solver output,
  // authoritative engineering evidence, or a substitute for worker tests.
  await openProfessionalPlanning(page, { referenceComparisonLayoutMock: true });
  await createProfessionalPlanningRecord(page);
  await page.getByRole("navigation", { name: "Project workflow" }).getByRole("button", { name: "Model fitness" }).click();
  await expect(page.getByRole("heading", { name: "Model Fitness Passport" })).toBeVisible();
}

test("Home preserves the three distinct entry paths", async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /See what holds/i })).toBeVisible();
  await expect(page).toHaveScreenshot(`home-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
});

test("Ridgeway robustness board presents the complete verified direct ensemble", async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  await page.goto("http://127.0.0.1:1431/");
  await page.getByRole("button", { name: "Robustness board" }).click();
  await expect(page.getByRole("heading", { name: "Robustness Board" })).toBeVisible();
  await expect(page.getByRole("radio")).toHaveCount(4);
  await expect(page.getByRole("radio", { name: "Inspect Carry-forward candidate" })).toBeChecked();
  await expect(page.getByText("12/12 direct", { exact: true })).toHaveCount(4);
  await expect(page.getByRole("columnheader", { name: "Hypothesis rank range" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Safeguard pairs" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "4/4 scenarios pass" })).toHaveCount(4);
  await expect(page.getByRole("heading", { name: "Carry-forward candidate" })).toBeVisible();
  const tableHint = page.getByText(/Scroll horizontally to inspect rank sensitivity/i);
  if (testInfo.project.name !== "desktop-1440") {
    await expect(tableHint).toBeVisible();
  } else {
    await expect(tableHint).toBeHidden();
  }
  await expect(page.locator(".replay-verdict").getByText("4/4 scenarios pass", { exact: true })).toBeVisible();
  await expect(page.locator(".replay-verdict").getByText("Review before carry-forward", { exact: true })).toBeVisible();
  await expect(page.getByText(/consider this candidate for ecological, field, and engineering feasibility review/i)).toBeVisible();
  const map = page.locator(".replay-raster");
  await expect(map.getByRole("img", { name: /diagnostic current comparison/i })).toBeVisible();
  await expect(map.getByRole("slider", { name: "Comparison boundary" })).toHaveAttribute("aria-valuetext", /baseline on the left/i);
  await page.getByRole("navigation", { name: "Replay checkpoints" }).evaluate((nav) => {
    const active = nav.querySelector<HTMLElement>("[aria-current='step']");
    if (active && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = Math.max(0, active.offsetLeft - ((nav.clientWidth - active.offsetWidth) / 2));
    }
  });
  await assertNoBlockingAxeFindings(page);
  await expect(page).toHaveScreenshot(`robustness-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
  if (testInfo.project.name === "desktop-1440" || testInfo.project.name === "desktop-1280") {
    await expect(map).toHaveScreenshot(`current-map-${testInfo.project.name}.png`, { animations: "disabled" });
  }
  await map.getByText("View map evidence as a table", { exact: true }).click();
  await expect(map.getByRole("table")).toBeVisible();
  await page.getByRole("radio", { name: "Inspect Hypothesis-sensitive option" }).check();
  const sensitivityVerdict = page.locator(".replay-verdict");
  await expect(sensitivityVerdict.getByText("#35 → #49", { exact: true })).toBeVisible();
  await expect(sensitivityVerdict.getByText("Future pressure best · Low highway penalty weakest", { exact: true })).toBeVisible();
  await expect(sensitivityVerdict.getByText(/its computed position shifts from #35 under Future pressure to #49 under Low highway penalty/i)).toBeVisible();
  await expect(page.getByText(/rank 1 is the highest score among the 128 directly evaluated portfolios/i)).toBeVisible();
  const verdictMetricsOverlap = await sensitivityVerdict.evaluate((verdict) => {
    const rectangles = [...verdict.querySelectorAll("dl > div")].map((item) => item.getBoundingClientRect());
    return rectangles.some((left, index) => rectangles.slice(index + 1).some((right) => !(
      left.right <= right.left + 0.5
      || right.right <= left.left + 0.5
      || left.bottom <= right.top + 0.5
      || right.bottom <= left.top + 0.5
    )));
  });
  expect(verdictMetricsOverlap).toBe(false);
  await assertNoBlockingAxeFindings(page);
});

test("compact Evidence Rail behaves as a modal dialog", async ({ page }) => {
  test.skip(page.viewportSize()?.width !== 760, "Compact evidence behavior is asserted at 760 px.");
  await page.goto("http://127.0.0.1:1431/");
  await page.getByRole("button", { name: "Robustness board" }).click();
  const trigger = page.getByRole("button", { name: "Trace controlling result" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: /Controlling direct result/i });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/Direct Circuitscape result under documented assumptions/i)).toBeVisible();
  await expect(dialog.getByText(/Accepted in verified fixture manifest/i)).toBeVisible();
  const close = dialog.getByRole("button", { name: "Close evidence rail" });
  await expect(close).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("professional W&B gate explains the fail-closed data boundary", async ({ page }, testInfo) => {
  test.skip(!["desktop-1280", "zoom-200"].includes(testInfo.project.name), "The onboarding boundary is pinned at 1280 px and effective 200% zoom.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "Assemble planning model" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Connect the evidence destination" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("radio", { name: /W&B Cloud/i })).toBeDisabled();
  const close = dialog.getByRole("button", { name: "Close W&B connection" });
  await expect(close).toBeFocused();
  await assertNoBlockingAxeFindings(page);
  await expect(page).toHaveScreenshot(`wandb-gate-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.locator(":focus")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("professional evidence package makes W&B synchronization consequences explicit", async ({ page }, testInfo) => {
  test.skip(!["desktop-1440", "compact-760"].includes(testInfo.project.name), "The professional synchronization panel is pinned at desktop and compact widths.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openProfessionalPlanning(page);
  await createProfessionalPlanningRecord(page);
  const workflow = page.getByRole("navigation", { name: "Project workflow" });
  await workflow.getByRole("button", { name: "Evidence package" }).click();
  const syncPanel = page.locator(".cloud-sync-panel");
  await expect(syncPanel.getByRole("heading", { name: "CONNECTED" })).toBeVisible();
  await expect(syncPanel.getByRole("button", { name: "Synchronize authorized evidence" })).toBeEnabled();
  await expect(syncPanel.getByText(/Cloud failure cannot change direct results/i)).toBeVisible();
  await assertNoBlockingAxeFindings(page);
  await expect(syncPanel).toHaveScreenshot(`professional-sync-${testInfo.project.name}.png`, { animations: "disabled" });
});

test("browser-mocked reference comparison checks layout, axe, and keyboard only — not engineering evidence", async ({ page }, testInfo) => {
  test.skip(!["desktop-1440", "compact-760"].includes(testInfo.project.name), "The browser-only reference layout fixture is pinned at primary desktop and compact widths.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openBrowserMockedReferenceComparison(page);

  const panel = page.locator(".reference-comparison-panel");
  await expect(panel.getByRole("heading", { name: "Compare the active model with reviewed reference matrices" })).toBeVisible();
  await expect(panel.getByText("What this check answers", { exact: true })).toBeVisible();
  await expect(panel.getByText(/reproduce each reviewed focal-pair matrix within its recorded tolerance/i)).toBeVisible();
  await expect(panel.getByText("What it cannot answer", { exact: true })).toBeVisible();
  await expect(panel.getByText(/does not validate resistance choices, focal areas, animal movement/i)).toBeVisible();

  const planButton = panel.getByRole("button", { name: "Plan exact reference check" });
  await expect(planButton).toBeEnabled();
  await planButton.click();
  const runButton = panel.getByRole("button", { name: "Run 1 direct matrix job" });
  await expect(runButton).toBeEnabled();
  await expect(panel.getByText("1 No Action baseline × 1 bound scenarios = 1 direct Circuitscape matrix jobs", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as typeof window & { __REFERENCE_LAYOUT_MOCK_CALLS__?: string[] }).__REFERENCE_LAYOUT_MOCK_CALLS__)).toEqual([
    "connectivity.reference.plan",
  ]);

  await runButton.click();
  await expect(panel.getByText("Active reference match passed", { exact: true }).first()).toBeVisible();
  await expect(panel.getByText(/Ecological assumptions and fitness dimensions still require independent review/i)).toBeVisible();
  await expect(panel.getByText("All reviewed tolerances passed.", { exact: true })).toBeVisible();
  await expect(panel.getByText(/No fitness status, attestation, shortlist, or approval was changed automatically/i)).toBeVisible();
  await expect(panel.getByText("100.000000", { exact: true })).toBeVisible();
  await expect(panel.getByText("100.500000", { exact: true })).toBeVisible();
  await expect(panel.getByText("Within tolerance", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Trace exact claim" })).toBeEnabled();
  await expect(panel.getByRole("button", { name: "Plan exact reference check" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Run 1 direct matrix job" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as typeof window & { __REFERENCE_LAYOUT_MOCK_CALLS__?: string[] }).__REFERENCE_LAYOUT_MOCK_CALLS__)).toEqual([
    "connectivity.reference.plan",
    "connectivity.reference.start",
    "connectivity.reference.status",
    "connectivity.reference.result",
  ]);

  const lineage = panel.locator("details.pair-lineage");
  const lineageSummary = lineage.locator("summary");
  await lineageSummary.focus();
  await expect(lineageSummary).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(lineage).toHaveAttribute("open", "");
  await expect(lineage.getByText("Agency-approved baseline matrix retained with the project bundle.", { exact: true })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(lineage).not.toHaveAttribute("open", "");

  const table = panel.getByRole("region", { name: "Exact baseline versus reviewed reference comparisons" });
  await table.focus();
  await expect(table).toBeFocused();
  const scrollHint = panel.getByText(/Scroll horizontally to inspect every tolerance/i);
  await expect(scrollHint).toBeVisible();
  expect(await table.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => table.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await table.evaluate((element) => { element.scrollLeft = 0; });

  await assertNoBlockingAxeFindings(page);
  await panel.evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page).toHaveScreenshot(`professional-reference-comparison-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
});

test("planning assembly keeps the decision boundary and first real action visible", async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  await openProfessionalPlanning(page);
  const workflow = page.getByRole("navigation", { name: "Project workflow" });
  await workflow.getByRole("button", { name: "Study context" }).click();
  await expect(page.getByRole("heading", { name: "Name the decision before drawing the map." })).toBeVisible();
  await expect(page.getByText(/Complete the six required fields/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Create planning record" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Create planning record" })).toBeVisible();
  await expect(page.getByText(/This model may not predict/i)).toBeVisible();
  await assertNoBlockingAxeFindings(page);
  await expect(page).toHaveScreenshot(`planning-context-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
});

test("every planning stage explains its evidence prerequisite", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1440", "The full prerequisite walk is exercised once at the primary practitioner viewport.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openProfessionalPlanning(page);
  await createProfessionalPlanningRecord(page);
  const workflow = page.getByRole("navigation", { name: "Project workflow" });
  const stages = [
    ["Source assets", "Classify every layer before it moves.", "Ownership and replication ledger"],
    ["Grid alignment", "Make each transformation reviewable.", "Add and inspect a modeled raster first."],
    ["Resistance hypotheses", "Treat resistance as a hypothesis—not a fact.", "This records a reviewed hypothesis"],
    ["Focal areas", "Explain why these places must connect.", "Requires an authorized IDENTIFIER raster"],
    ["Model fitness", "Model Fitness Passport", "There is no score"],
    ["Ecological scenarios", "Define every comparison before running.", "No approved scenarios"],
    ["Reviewed interventions", "Only reviewed options enter the search.", "No reviewed options"],
    ["Safeguards", "A headline modeled resistance reduction cannot erase a safeguard failure.", "Safeguard scenarios required"],
    ["Rules and costs", "Make acceptance testable before running.", "Cost basis required"],
    ["Run plan", "Let the worker derive the complete ensemble.", "No runnable proposal"],
  ] as const;
  for (const [step, heading, prerequisite] of stages) {
    await workflow.getByRole("button", { name: step }).click();
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.getByText(prerequisite, { exact: false }).first()).toBeVisible();
  }
  await assertNoBlockingAxeFindings(page);
});

test("professional Rules and costs keeps the retained decision editor readable", async ({ page }, testInfo) => {
  test.skip(!["desktop-1440", "compact-760"].includes(testInfo.project.name), "The full-width and compact professional editor layouts are exercised at their boundary viewports.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openProfessionalPlanning(page);
  await createProfessionalPlanningRecord(page);
  await page.getByRole("navigation", { name: "Project workflow" }).getByRole("button", { name: "Rules and costs" }).click();

  const layout = page.locator(".professional-rule-layout");
  const decision = layout.getByRole("heading", { name: "Decision boundary" }).locator("..");
  const criterion = layout.getByRole("heading", { name: "Testable criterion" }).locator("..");
  const resources = layout.getByRole("heading", { name: "Hard project policy" }).locator("..");
  await expect(layout.getByText("Authoritative decision brief loaded")).toBeVisible();
  await expect(layout.getByLabel("Decision question *")).toHaveValue("Which reviewed portfolio should advance to feasibility review?");
  await expect(layout.getByLabel("Maximum direct jobs")).toHaveValue("5000");
  await expect(layout.getByRole("button", { name: "Save decision brief" })).toBeDisabled();
  await expect(layout.getByRole("button", { name: "Save resource policy" })).toBeDisabled();

  const [decisionBox, criterionBox, resourcesBox] = await Promise.all([decision.boundingBox(), criterion.boundingBox(), resources.boundingBox()]);
  expect(decisionBox).not.toBeNull();
  expect(criterionBox).not.toBeNull();
  expect(resourcesBox).not.toBeNull();
  if (testInfo.project.name === "desktop-1440") {
    expect(decisionBox!.width).toBeGreaterThan(criterionBox!.width * 1.8);
    expect(Math.abs(criterionBox!.y - resourcesBox!.y)).toBeLessThan(2);
  } else {
    expect(Math.abs(decisionBox!.width - criterionBox!.width)).toBeLessThan(2);
    expect(resourcesBox!.y).toBeGreaterThan(criterionBox!.y + criterionBox!.height);
  }

  await assertNoBlockingAxeFindings(page);
  const screenshot = await layout.screenshot({ animations: "disabled" });
  expect(screenshot.byteLength).toBeGreaterThan(10_000);
  await testInfo.attach(`professional-rules-costs-${testInfo.project.name}`, { body: screenshot, contentType: "image/png" });
});

test("compact intervention planning keeps Survey first a readable choice", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "compact-760", "The compact intervention editor has its own regression baseline.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openProfessionalPlanning(page);
  await createProfessionalPlanningRecord(page);
  await page.getByRole("navigation", { name: "Project workflow" }).getByRole("button", { name: "Reviewed interventions" }).click();
  const surveyFirst = page.getByRole("checkbox", { name: /Include a mutually exclusive Survey-first option/i });
  await expect(surveyFirst).toBeVisible();
  const box = await surveyFirst.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.width).toBeLessThanOrEqual(20);
  expect(box!.height).toBeLessThanOrEqual(20);
  await assertNoBlockingAxeFindings(page);
  await expect(page).toHaveScreenshot("planning-interventions-compact-760.png", { fullPage: false, animations: "disabled" });
});

test("Evidence Rail resolves a direct result without cloud or assistant calls", async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  await page.goto("http://127.0.0.1:1431/");
  await page.getByRole("button", { name: "Robustness board" }).click();
  await page.getByRole("button", { name: "Trace controlling result" }).click();
  const rail = page.getByRole("dialog", { name: /Controlling direct result/i });
  await expect(rail.getByText(/Scenario reducer · portfolio_[a-f0-9]{20}/i)).toBeVisible();
  await expect(rail.getByText(/matrix_portfolio_[a-f0-9]{20}_scenario_/i).first()).toBeVisible();
  await expect(rail.getByText(/Accepted in verified fixture manifest/i)).toBeVisible();
  await expect(rail.getByText(/makes no W&B, Weave, model-provider, or application-host call/i)).toBeVisible();
  await assertNoBlockingAxeFindings(page);
  if (testInfo.project.name === "desktop-1440" || testInfo.project.name === "desktop-1280") {
    await expect(rail).toHaveScreenshot(`evidence-boundary-${testInfo.project.name}.png`, { animations: "disabled" });
  } else {
    await expect(page).toHaveScreenshot(`evidence-boundary-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
  }
});

test("the deployed public replay is no-login, local-only, and decision-first", async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (testInfo.project.name === "zoom-200") await page.setViewportSize({ width: 360, height: 450 });
  const unexpectedRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.hostname !== "127.0.0.1" || url.port !== "1431") unexpectedRequests.push(request.url());
  });
  await page.goto("http://127.0.0.1:1431/");
  await expect(page.locator(".replay-boundary")).toContainText("No login · no cloud");
  await expect(page.locator(".replay-boundary")).toContainText("CC0 synthetic data only");
  await expect(page.getByRole("heading", { name: "Which Ridgeway concept deserves another look?" })).toBeVisible();
  await expect(page.getByText(/A shortlist for feasibility review—not a corridor recommendation/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /Open reviewed model/i })).toHaveCount(0);
  await expect(page.getByRole("main")).toBeVisible();
  await assertNoBlockingAxeFindings(page);
  expect(await page.locator("html").evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(unexpectedRequests).toEqual([]);
  await expect(page).toHaveScreenshot(`public-replay-overview-${testInfo.project.name}.png`, { fullPage: false, animations: "disabled" });
});
