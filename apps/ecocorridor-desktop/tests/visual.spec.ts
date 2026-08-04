import { expect, test } from "@playwright/test";
import axe from "axe-core";

async function installProfessionalVisualHost(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    let credentialStored = false;
    const response = (method: string, data: unknown) => ({ api_version: "1.0", method, data, data_sha256: "a".repeat(64) });
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
      request: async (method: string) => {
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
            reason: "Create authoritative visual-test project.",
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
            project: { project_id: projectId, title: decisionBrief.title, mode: "PLANNING_DRAFT" },
            active_revision: revision,
            effective_bundle: {
              schema_version: "1.0",
              project_id: projectId,
              title: decisionBrief.title,
              revision,
              decision_brief: decisionBrief,
              assets: [], resistance_hypotheses: [], focal_area_sets: [], scenarios: [],
              intervention_sites: [], intervention_options: [], cost_basis: null,
              safeguards: [], review_masks: [], decision_rules: [], fitness,
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
        throw new Error(`Unexpected professional visual-test RPC ${method}`);
      },
    };
  });
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

async function openProfessionalPlanning(page: import("@playwright/test").Page) {
  await installProfessionalVisualHost(page);
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
  await expect(page.getByRole("columnheader", { name: "Safeguard pairs" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "4/4 scenarios pass" })).toHaveCount(4);
  await expect(page.getByRole("heading", { name: "Carry-forward candidate" })).toBeVisible();
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
