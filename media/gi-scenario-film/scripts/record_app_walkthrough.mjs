#!/usr/bin/env node

import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const filmRoot = path.resolve(scriptDirectory, "..");
const repositoryRoot = path.resolve(filmRoot, "../..");
const outputRoot = path.join(filmRoot, "dist", "article-recording");
const rawRoot = path.join(outputRoot, "raw");
const masterPath = path.join(outputRoot, "guided-app-master.webm");
const markerPath = path.join(outputRoot, "session-markers.json");
const fixturePath = path.join(repositoryRoot, "apps", "desktop", "src", "data", "guidedFixture.json");
const mediaSpecPath = path.join(filmRoot, "story", "article-media.json");

const argumentsList = process.argv.slice(2);
const argument = (name, fallback) => {
  const index = argumentsList.indexOf(name);
  return index >= 0 ? argumentsList[index + 1] : fallback;
};
const baseUrl = argument("--base-url", "http://127.0.0.1:1420/");
const headed = argumentsList.includes("--headed");
const viewport = { width: 1440, height: 900 };

const sha256 = async (target) => createHash("sha256").update(await readFile(target)).digest("hex");
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function requirePreview() {
  let response;
  try {
    response = await fetch(baseUrl);
  } catch (error) {
    throw new Error(`GI Scenario Studio is not reachable at ${baseUrl}. Start the Vite preview first.`, {
      cause: error,
    });
  }
  if (!response.ok) throw new Error(`GI Scenario Studio returned HTTP ${response.status} at ${baseUrl}`);
}

async function installRecordingCursor(page) {
  await page.addInitScript(() => {
    const install = () => {
      const cursor = document.createElement("div");
      cursor.id = "gi-recording-cursor";
      cursor.setAttribute("aria-hidden", "true");
      cursor.innerHTML = `<svg viewBox="0 0 24 30" aria-hidden="true"><path d="M2 2v21l6-6 5 11 5-2-5-11h9z" /></svg>`;
      const style = document.createElement("style");
      style.textContent = `
        #gi-recording-cursor {
          position: fixed;
          left: 0;
          top: 0;
          width: 24px;
          height: 30px;
          filter: drop-shadow(0 2px 2px rgba(23,49,58,.28));
          transform: translate(-2px, -2px);
          pointer-events: none;
          z-index: 2147483647;
        }
        #gi-recording-cursor svg { display: block; width: 100%; height: 100%; overflow: visible; }
        #gi-recording-cursor path { fill: #f5f7f6; stroke: #17313a; stroke-width: 2.2; stroke-linejoin: round; }
        #gi-recording-cursor::after {
          content: "";
          position: absolute;
          width: 30px;
          height: 30px;
          left: -10px;
          top: -10px;
          border: 3px solid transparent;
          border-radius: 50%;
          transform: scale(.45);
          opacity: 0;
        }
        #gi-recording-cursor.is-clicking::after {
          border-color: #08766f;
          transform: scale(1);
          opacity: .8;
        }
      `;
      document.head.append(style);
      document.body.append(cursor);
      document.addEventListener("mousemove", (event) => {
        cursor.style.left = `${event.clientX}px`;
        cursor.style.top = `${event.clientY}px`;
      });
      document.addEventListener("mousedown", () => cursor.classList.add("is-clicking"));
      document.addEventListener("mouseup", () => cursor.classList.remove("is-clicking"));
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
    else install();
  });
}

async function parkCursor(page) {
  await page.mouse.move(viewport.width - 46, 72, { steps: 16 });
  await wait(260);
}

async function pointerTo(page, locator, steps = 20) {
  await locator.waitFor({ state: "visible" });
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("A visible recording target did not have a bounding box");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps });
  await wait(260);
}

async function pointerClick(page, locator, settle = 700) {
  await pointerTo(page, locator);
  await page.mouse.down();
  await wait(120);
  await page.mouse.up();
  await wait(settle);
}

async function smoothIntoView(locator, block = "center") {
  await locator.evaluate((element, targetBlock) => {
    element.scrollIntoView({ behavior: "smooth", block: targetBlock, inline: "nearest" });
  }, block);
  await wait(1100);
}

async function closeRail(page) {
  const close = page.getByRole("button", { name: "Close evidence rail", exact: true });
  if (await close.isVisible()) await pointerClick(page, close, 650);
}

async function goToStep(page, accessibleName, heading) {
  await closeRail(page);
  await pointerClick(page, page.getByRole("button", { name: accessibleName, exact: true }), 650);
  await page.getByRole("heading", { name: heading, exact: true }).waitFor({ state: "visible" });
  await wait(600);
}

async function unlockGuidedWorkflow(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "See which GI alternative earns a closer look.", exact: true }).waitFor();
  await page.getByRole("button", { name: "Open exact fixture", exact: true }).click();
  await page.getByRole("heading", { name: "Which GI concept, if any, deserves a closer look?", exact: true }).waitFor();
  await page.getByRole("button", { name: "Start the evidence review", exact: true }).click();
  await page.getByRole("heading", { name: "A small teaching watershed, fully traced", exact: true }).waitFor();
  await page.locator("label.review-check").click();
  await page.getByRole("button", { name: "Review synthetic storm cases", exact: true }).click();
  await page.getByRole("heading", { name: "Test the same decision under every storm", exact: true }).waitFor();
  await page.getByRole("button", { name: "Review GI opportunities", exact: true }).click();
  await page.getByRole("heading", { name: "Search only within engineer-declared GI slots", exact: true }).waitFor();
  await page.getByRole("button", { name: "Review teaching objectives", exact: true }).click();
  await page.getByRole("heading", { name: "State what a useful alternative must do", exact: true }).waitFor();
  await page.getByRole("button", { name: "Review verified replay", exact: true }).click();
  await page.getByRole("heading", { name: "Review the pinned exact-run record", exact: true }).waitFor();
  await page.locator(".page-heading").getByRole("button", { name: "Compare pinned alternatives", exact: true }).click();
  await page.getByRole("heading", { name: "Apply the rules before reading the tradeoff plot", exact: true }).waitFor();
  await page.getByRole("button", { name: "Prepare learning evidence", exact: true }).click();
  await page.getByRole("heading", { name: "Package the evidence, not just the chart", exact: true }).waitFor();
}

async function main() {
  await requirePreview();
  const mediaSpec = JSON.parse(await readFile(mediaSpecPath, "utf8"));
  const expectedMarkers = mediaSpec.clips.map((clip) => clip.marker);
  await mkdir(outputRoot, { recursive: true });
  await rm(rawRoot, { recursive: true, force: true });
  await mkdir(rawRoot, { recursive: true });

  const browser = await chromium.launch({ headless: !headed });
  const context = await browser.newContext({
    viewport,
    colorScheme: "light",
    reducedMotion: "reduce",
    recordVideo: { dir: rawRoot, size: viewport },
  });
  const page = await context.newPage();
  await installRecordingCursor(page);
  const video = page.video();
  if (!video) throw new Error("Playwright did not create a recording stream");
  const recordingStartedAt = performance.now();
  const markers = {};
  const elapsed = () => (performance.now() - recordingStartedAt) / 1000;
  const clip = async (key, action) => {
    if (!expectedMarkers.includes(key)) throw new Error(`Unknown article clip marker: ${key}`);
    await wait(1000);
    const startSeconds = elapsed();
    await action();
    await wait(1700);
    markers[key] = {
      start_seconds: Number(startSeconds.toFixed(3)),
      end_seconds: Number(elapsed().toFixed(3)),
    };
  };

  try {
    await unlockGuidedWorkflow(page);

    await goToStep(page, "3. Storms: 4 cases", "Test the same decision under every storm");
    await clip("storm_scope", async () => {
      const longStorm = page.getByRole("button", {
        name: "Inspect Synthetic long-duration 2.5 in event",
        exact: true,
      });
      await pointerClick(page, longStorm, 1000);
      await wait(2400);
      await closeRail(page);
      const hyetograph = page.locator(".storm-hyetograph");
      await smoothIntoView(hyetograph);
      await parkCursor(page);
      await wait(1200);
    });

    await goToStep(page, "2. Model: Import & check", "A small teaching watershed, fully traced");
    await clip("model_fitness", async () => {
      await pointerClick(page, page.locator(".metric-callout"), 900);
      await pointerClick(page, page.getByRole("button", { name: "Explain how to use this", exact: true }), 900);
      await wait(1700);
      await closeRail(page);
      const referenceFinding = page.locator(".finding-row").filter({ hasText: "Reference comparison" });
      await pointerClick(page, referenceFinding, 900);
      await wait(1700);
    });

    await goToStep(page, "4. Opportunities: 4 slots", "Search only within engineer-declared GI slots");
    await clip("bounded_opportunities", async () => {
      for (const [slot, hold] of [["S1_PP", 1100], ["S2_RG", 2100], ["S2_RB", 1100], ["S1_BIO", 1600]]) {
        await pointerClick(page, page.getByRole("button", { name: `Select ${slot}`, exact: true }), hold);
      }
      const ledger = page.getByRole("heading", { name: "4 bounded integer decisions", exact: true });
      await smoothIntoView(ledger);
      await parkCursor(page);
      await wait(1200);
    });

    await goToStep(page, "6. Run: 884 jobs", "Review the pinned exact-run record");
    await clip("verified_replay", async () => {
      await pointerTo(page, page.getByRole("heading", { name: "4 storms × 220 designs + 4 baselines = 884 jobs", exact: true }));
      await parkCursor(page);
      await wait(2600);
      await smoothIntoView(page.locator(".run-stats"));
      await wait(1600);
      await smoothIntoView(page.locator(".run-lower"));
      await wait(1400);
      await smoothIntoView(page.locator(".run-hero"), "start");
      await parkCursor(page);
      await wait(2200);
    });

    await goToStep(page, "7. Compare: Exact results", "Apply the rules before reading the tradeoff plot");
    await clip("frontier_guardrail", async () => {
      await smoothIntoView(page.locator("#acceptance-matrix"), "start");
      await wait(1300);
      await smoothIntoView(page.locator(".adverse-lesson"));
      await wait(1200);
      await smoothIntoView(page.locator("#tradeoff-panel"), "start");
      const measure = page.locator(".compare-toolbar select");
      await pointerTo(page, measure);
      await measure.selectOption({ label: "Maximum outfall peak" });
      await wait(1800);
      await smoothIntoView(page.locator(".adverse-lesson"));
      await parkCursor(page);
      await wait(2200);
    });

    await clip("evidence_rail", async () => {
      await smoothIntoView(page.locator(".adverse-lesson"));
      await pointerClick(page, page.getByRole("button", { name: "Trace the adverse peak", exact: true }), 1000);
      const rail = page.locator('aside[aria-label="Evidence details"]');
      await pointerClick(page, rail.getByRole("button", { name: "Explain how to use this", exact: true }), 900);
      await rail.getByText("3.14 to 3.21 cfs", { exact: false }).waitFor({ state: "visible" });
      await rail.getByText("13.33 to 13.34 cfs", { exact: false }).waitFor({ state: "visible" });
      await smoothIntoView(rail.locator(".rail-explanation"));
      await parkCursor(page);
      await wait(2800);

      await rail.getByText("Evidence Guide held back by release checks", { exact: true }).waitFor({ state: "visible" });
      await rail.getByText("No model call will be attempted", { exact: false }).waitFor({ state: "visible" });
      await smoothIntoView(rail.locator(".evidence-guide"), "start");
      await parkCursor(page);
      await wait(2800);

      await closeRail(page);
      await smoothIntoView(page.locator(".decision-summary"), "start");
      await pointerClick(page, page.getByRole("button", { name: "Trace ALT-207", exact: true }), 900);
      await pointerClick(page, rail.getByRole("button", { name: "Explain how to use this", exact: true }), 900);
      await rail.getByText("does not establish water-quality improvement", { exact: false }).waitFor({ state: "visible" });
      await rail.getByText("groundwater recharge", { exact: false }).waitFor({ state: "visible" });
      await rail.getByText("lifecycle impact", { exact: false }).waitFor({ state: "visible" });
      await smoothIntoView(rail.locator(".rail-explanation"));
      await parkCursor(page);
      await wait(3600);
    });

    await goToStep(page, "8. Export: Evidence bundle", "Package the evidence, not just the chart");
    await clip("engineer_handoff", async () => {
      await smoothIntoView(page.locator(".export-grid"), "start");
      await wait(1700);
      await smoothIntoView(page.locator(".next-action-panel"), "start");
      await wait(2300);
      await smoothIntoView(page.locator(".export-summary"));
      await wait(1200);
    });
  } finally {
    await context.close();
    await browser.close();
  }

  if (JSON.stringify(Object.keys(markers)) !== JSON.stringify(expectedMarkers)) {
    throw new Error(`Recorded marker order drifted: ${Object.keys(markers)} != ${expectedMarkers}`);
  }
  const recordedPath = await video.path();
  await copyFile(recordedPath, masterPath);
  const payload = {
    schema_version: "1.0",
    preview_mode: "interface-only",
    live_solver_execution: false,
    base_url_origin: new URL(baseUrl).origin,
    viewport,
    source_fixture_sha256: await sha256(fixturePath),
    media_spec_sha256: await sha256(mediaSpecPath),
    master: path.relative(filmRoot, masterPath),
    master_sha256: await sha256(masterPath),
    markers,
  };
  await writeFile(markerPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ master: masterPath, markerPath, clipCount: expectedMarkers.length })}\n`);
}

await main();
