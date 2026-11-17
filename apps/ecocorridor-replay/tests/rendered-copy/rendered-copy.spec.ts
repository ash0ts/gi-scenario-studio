import { spawnSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  expect,
  test,
  type BrowserContext,
  type Locator,
  type Page,
  type TestInfo,
} from "@playwright/test";

type RenderedSnapshot = {
  label: string;
  bodyText: string;
  attributes: string[];
  pseudoContent: string[];
};

const REPLAY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const REPOSITORY_ROOT = path.resolve(REPLAY_ROOT, "../..");
const VALIDATOR = path.join(REPOSITORY_ROOT, "scripts/validate_ecocorridor_rendered_copy.py");
const REPLAY_ORIGIN = "http://127.0.0.1:4431";

async function collectSnapshot(page: Page, label: string): Promise<RenderedSnapshot> {
  return page.evaluate((snapshotLabel) => {
    const attributes: string[] = [`document-title: ${document.title}`];
    const pseudoContent: string[] = [];
    const renderedText: string[] = [document.body.innerText];
    const attributeNames = ["aria-label", "aria-valuetext", "title", "alt", "placeholder"];
    const roots: Array<Document | ShadowRoot> = [document];
    for (let rootIndex = 0; rootIndex < roots.length; rootIndex += 1) {
      const root = roots[rootIndex];
      for (const element of root.querySelectorAll<HTMLElement>("*")) {
        for (const name of attributeNames) {
          const value = element.getAttribute(name);
          if (value) attributes.push(`${name}: ${value}`);
        }
        if (element.tagName.toLowerCase() === "title" && element.textContent) {
          attributes.push(`title-element: ${element.textContent}`);
        }
        for (const pseudo of ["::before", "::after"] as const) {
          const content = getComputedStyle(element, pseudo).content;
          if (content && content !== "none" && content !== "normal" && content !== '""') {
            pseudoContent.push(`${pseudo}: ${content}`);
          }
        }
        if (element.shadowRoot) {
          roots.push(element.shadowRoot);
          for (const child of element.shadowRoot.children) {
            const text = child instanceof HTMLElement ? child.innerText : child.textContent;
            if (text) renderedText.push(text);
          }
        }
      }
    }
    return {
      label: snapshotLabel,
      bodyText: renderedText.join("\n"),
      attributes,
      pseudoContent,
    };
  }, label);
}

function pythonExecutable(): string {
  const configured = process.env.ECOCORRIDOR_PYTHON;
  if (configured) return configured;
  const worktreePython = path.join(REPOSITORY_ROOT, ".venv/bin/python");
  return existsSync(worktreePython) ? worktreePython : "python3";
}

function validateRenderedCopy(
  payload: Record<string, unknown>,
  testInfo: TestInfo,
  claimsOnly = false,
) {
  const input = testInfo.outputPath(claimsOnly ? "claim-probe.json" : "rendered-copy.json");
  writeFileSync(input, JSON.stringify(payload), "utf8");
  return spawnSync(
    pythonExecutable(),
    [VALIDATOR, "--input", input, ...(claimsOnly ? ["--claims-only"] : [])],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: path.join(REPOSITORY_ROOT, "python") },
    },
  );
}

function isReplayOrigin(value: string): boolean {
  try {
    return new URL(value).origin === REPLAY_ORIGIN;
  } catch {
    return false;
  }
}

async function enforceNetworkBoundary(context: BrowserContext, page: Page, unexpected: Set<string>) {
  context.on("request", (request) => {
    if (!isReplayOrigin(request.url())) {
      unexpected.add(`request:${request.url()}`);
    }
  });
  context.on("page", (opened) => {
    if (opened === page) return;
    unexpected.add(`popup:${opened.url() || "about:blank"}`);
    void opened.close();
  });
  context.on("serviceworker", (worker) => {
    unexpected.add(`service-worker:${worker.url()}`);
  });
  page.on("worker", (worker) => {
    unexpected.add(`worker:${worker.url()}`);
  });
  page.on("framenavigated", (frame) => {
    const url = frame.url();
    if (url && url !== "about:blank" && !isReplayOrigin(url)) {
      unexpected.add(`navigation:${url}`);
    }
  });
  await context.route("**/*", async (route) => {
    if (!isReplayOrigin(route.request().url())) {
      unexpected.add(`blocked-request:${route.request().url()}`);
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  await context.routeWebSocket(/.*/, async (webSocket) => {
    unexpected.add(`websocket:${webSocket.url()}`);
    await webSocket.close({ code: 1008, reason: "Public replay network boundary" });
  });
}

async function captureInteractionState(
  page: Page,
  snapshots: RenderedSnapshot[],
  mode: "focus" | "hover",
  locator: Locator,
  label: string,
) {
  if (mode === "focus") await locator.focus();
  else await locator.hover();
  snapshots.push(await collectSnapshot(page, `${mode}:${label}`));
}

test("network allowlist accepts only the exact replay origin", () => {
  expect(isReplayOrigin("http://127.0.0.1:4431/")).toBe(true);
  expect(isReplayOrigin("http://localhost:4431/")).toBe(false);
  expect(isReplayOrigin("http://127.0.0.1:4432/")).toBe(false);
  expect(isReplayOrigin("https://127.0.0.1:4431/")).toBe(false);
  expect(isReplayOrigin("https://example.invalid/")).toBe(false);
});

test("production replay renders only bounded, policy-safe ecological copy", async ({ context, page }, testInfo) => {
  const unexpectedNetwork = new Set<string>();
  await enforceNetworkBoundary(context, page, unexpectedNetwork);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Which Ridgeway concept deserves another look?" }),
  ).toBeVisible();

  const snapshots: RenderedSnapshot[] = [];
  const navigation = page.getByRole("navigation", { name: "Replay checkpoints" });
  const steps = navigation.getByRole("button");
  await expect(steps).toHaveCount(5);
  for (let index = 0; index < 5; index += 1) {
    await steps.nth(index).click();
    snapshots.push(await collectSnapshot(page, `step:${index + 1}`));
  }

  await navigation.getByRole("button", { name: "Robustness board" }).click();
  const radios = page.getByRole("radio", { name: /^Inspect / });
  await expect(radios).toHaveCount(4);
  const scenarioIds = new Set<string>();
  let portfolioScenarioStates = 0;
  let controllingEvidenceDialogs = 0;
  let pairEvidenceDialogs = 0;

  for (let portfolioIndex = 0; portfolioIndex < 4; portfolioIndex += 1) {
    await radios.nth(portfolioIndex).check();
    const scenarioSelect = page.getByLabel("Scenario shown in matrix and map");
    await expect(scenarioSelect.locator("option")).toHaveCount(12);
    const portfolioScenarioIds = new Set<string>();
    for (let scenarioIndex = 0; scenarioIndex < 12; scenarioIndex += 1) {
      await scenarioSelect.selectOption({ index: scenarioIndex });
      const scenarioId = await scenarioSelect.inputValue();
      scenarioIds.add(scenarioId);
      portfolioScenarioIds.add(scenarioId);
      portfolioScenarioStates += 1;
      await page.locator("details").evaluateAll((elements) => {
        for (const element of elements) (element as HTMLDetailsElement).open = true;
      });
      snapshots.push(
        await collectSnapshot(page, `board:${portfolioIndex + 1}:${scenarioIndex + 1}:${scenarioId}`),
      );

      await page.getByRole("button", { name: "Trace controlling result" }).click();
      await expect(page.getByRole("dialog", { name: /Controlling direct result/ })).toBeVisible();
      snapshots.push(
        await collectSnapshot(page, `evidence:controlling:${portfolioIndex + 1}:${scenarioId}`),
      );
      controllingEvidenceDialogs += 1;
      await page.getByRole("button", { name: "Close evidence rail" }).click();

      const pairButtons = page.getByRole("button", { name: "Trace pair" });
      await expect(pairButtons).toHaveCount(2);
      for (let pairIndex = 0; pairIndex < 2; pairIndex += 1) {
        await pairButtons.nth(pairIndex).click();
        await expect(page.getByRole("dialog", { name: /Focal pair/ })).toBeVisible();
        snapshots.push(
          await collectSnapshot(
            page,
            `evidence:pair:${portfolioIndex + 1}:${scenarioId}:${pairIndex + 1}`,
          ),
        );
        pairEvidenceDialogs += 1;
        await page.getByRole("button", { name: "Close evidence rail" }).click();
      }
    }
    expect(portfolioScenarioIds.size).toBe(12);
  }

  let sliderStates = 0;
  for (let portfolioIndex = 0; portfolioIndex < 4; portfolioIndex += 1) {
    await radios.nth(portfolioIndex).check();
    const scenarioSelect = page.getByLabel("Scenario shown in matrix and map");
    await scenarioSelect.selectOption({ index: 0 });
    const slider = page.getByRole("slider", { name: "Comparison boundary" });
    for (const value of ["0", "100"] as const) {
      await slider.fill(value);
      await expect(slider).toHaveValue(value);
      snapshots.push(await collectSnapshot(page, `slider:${portfolioIndex + 1}:${value}`));
      sliderStates += 1;
    }
  }

  const interactionTargets: Array<[string, Locator]> = [
    ["checkpoint", navigation.getByRole("button", { name: "Robustness board" })],
    ["portfolio", radios.nth(0)],
    ["scenario", page.getByLabel("Scenario shown in matrix and map")],
    ["scrubber", page.getByRole("slider", { name: "Comparison boundary" })],
    ["trace", page.getByRole("button", { name: "Trace controlling result" })],
  ];
  for (const [label, locator] of interactionTargets) {
    await captureInteractionState(page, snapshots, "focus", locator, label);
  }
  for (const [label, locator] of interactionTargets) {
    await captureInteractionState(page, snapshots, "hover", locator, label);
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  let compactDarkSteps = 0;
  for (let index = 0; index < 5; index += 1) {
    await steps.nth(index).click();
    snapshots.push(await collectSnapshot(page, `compact-dark-step:${index + 1}`));
    compactDarkSteps += 1;
  }

  const coverage = {
    steps: 5,
    portfolios: 4,
    scenarios: scenarioIds.size,
    portfolioScenarioStates,
    controllingEvidenceDialogs,
    pairEvidenceDialogs,
    evidenceDialogs: controllingEvidenceDialogs + pairEvidenceDialogs,
    sliderStates,
    focusStates: interactionTargets.length,
    hoverStates: interactionTargets.length,
    compactDarkSteps,
    snapshots: snapshots.length,
  };
  expect(coverage).toEqual({
    steps: 5,
    portfolios: 4,
    scenarios: 12,
    portfolioScenarioStates: 48,
    controllingEvidenceDialogs: 48,
    pairEvidenceDialogs: 96,
    evidenceDialogs: 144,
    sliderStates: 8,
    focusStates: 5,
    hoverStates: 5,
    compactDarkSteps: 5,
    snapshots: 220,
  });
  const validation = validateRenderedCopy({ coverage, snapshots }, testInfo);
  expect(validation.status, validation.stderr || validation.stdout).toBe(0);
  expect([...unexpectedNetwork]).toEqual([]);
});

test("DOM-assembled prohibited copy is rejected by the rendered policy", async ({ context, page }, testInfo) => {
  const unexpectedNetwork = new Set<string>();
  await enforceNetworkBoundary(context, page, unexpectedNetwork);
  await page.goto("/");
  await page.evaluate(() => {
    const injected = document.createElement("p");
    injected.id = "rendered-claim-regression";
    const verb = document.createElement("span");
    verb.textContent = "Improves habitat";
    const object = document.createElement("span");
    object.textContent = "connectivity";
    injected.append(verb, document.createTextNode(" "), object);
    document.body.append(injected);
  });
  const snapshot = await collectSnapshot(page, "injected-dom-claim");
  expect(snapshot.bodyText).toContain("Improves habitat connectivity");
  const validation = validateRenderedCopy({ snapshots: [snapshot] }, testInfo, true);
  expect(validation.status).not.toBe(0);
  expect(`${validation.stderr}\n${validation.stdout}`).toContain("HABITAT_RECONNECTION");

  const invisibleClaimProbes = [
    ["soft-hyphen", "Improves habitat connectiv\u00adity"],
    ["combining-grapheme-joiner", "Improves habitat connectiv\u034fity"],
    ["variation-selector-16", "Improves habitat connectiv\ufe0fity"],
    ["variation-selector-17", "Improves habitat connectiv\u{e0100}ity"],
  ] as const;
  for (const [label, claim] of invisibleClaimProbes) {
    await page.evaluate((renderedClaim) => {
      const injected = document.getElementById("rendered-claim-regression");
      if (!injected) throw new Error("claim regression probe is missing");
      injected.textContent = renderedClaim;
    }, claim);
    const unicodeSnapshot = await collectSnapshot(page, `injected-${label}-claim`);
    expect(unicodeSnapshot.bodyText).toContain(claim);
    const unicodeValidation = validateRenderedCopy(
      { snapshots: [unicodeSnapshot] },
      testInfo,
      true,
    );
    expect(unicodeValidation.status).not.toBe(0);
    expect(`${unicodeValidation.stderr}\n${unicodeValidation.stdout}`).toContain(
      "HABITAT_RECONNECTION",
    );
  }
  expect([...unexpectedNetwork]).toEqual([]);
});
