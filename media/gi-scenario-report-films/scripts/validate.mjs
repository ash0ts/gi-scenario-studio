import { chromium } from "@playwright/test";
import { access, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
  CAPTIONS_PATH,
  DIST_DIR,
  FILMS_PATH,
  MEDIA_SPEC_PATH,
  PACKAGE_DIR,
  parseCli,
  readJson,
  resolveProductionBinaries,
  run,
  sha256File,
  validateCaptions,
  validateFilmRecord,
  validateRuntimeSpec,
} from "./toolchain.mjs";

const HELP = `Usage: node scripts/validate.mjs [--stills-only] [--film <slug>]

Validates deterministic runtime behavior, controls, reduced motion, local-only
assets, text measurement, 96 px essential-copy margins, collisions, authored
holds, posters/checkpoints at native and 50% scale, captions, hashes, and (unless
--stills-only) the complete H.264 media contract.`;

const options = parseCli(process.argv.slice(2));
if (options.help) {
  process.stdout.write(`${HELP}\n`);
  process.exit(0);
}

const production = await readJson(FILMS_PATH);
const captionStory = await readJson(CAPTIONS_PATH);
const mediaSpec = await readJson(MEDIA_SPEC_PATH);
const manifestPath = resolve(DIST_DIR, "manifest.json");
const manifest = await readJson(manifestPath);
const selectedFilms = options.film
  ? production.films.filter((film) => film.slug === options.film)
  : production.films;
if (selectedFilms.length === 0) throw new Error(`Unknown film slug: ${options.film}`);
for (const film of selectedFilms) {
  validateFilmRecord(film, production);
  validateCaptions(film, captionStory.films[film.slug]);
}

if (manifest.schema_version !== "1.0" || manifest.production.width !== 1920 || manifest.production.height !== 1080 || manifest.production.fps !== 30) {
  throw new Error("dist/manifest.json does not match the production contract.");
}
if (!options.stillsOnly && !options.film && manifest.complete !== true) throw new Error("Production validation requires a complete render manifest.");
if (manifest.persistent_boundary !== production.persistent_notice) throw new Error("Persistent boundary language drifted.");

const { chrome, ffmpeg, ffprobe } = await resolveProductionBinaries({ includeProbe: true });
const errors = [];
const filmResults = [];

function requireCondition(condition, message) {
  if (!condition) throw new Error(message);
}

async function probe(path, countFrames = false) {
  return JSON.parse(await run(ffprobe, [
    "-v", "error",
    ...(countFrames ? ["-count_frames"] : []),
    "-show_streams", "-show_format", "-of", "json", path,
  ], { capture: true }));
}

async function validateImage(path, expectedWidth, expectedHeight) {
  await access(path);
  const details = await probe(path);
  const image = details.streams.find((stream) => stream.codec_type === "video");
  requireCondition(image, `${path}: image stream is missing.`);
  requireCondition(image.width === expectedWidth && image.height === expectedHeight, `${path}: expected ${expectedWidth}×${expectedHeight}, found ${image.width}×${image.height}.`);
}

function parseRate(value) {
  const [numerator, denominator] = String(value).split("/").map(Number);
  return numerator / denominator;
}

function overlapRatio(first, second) {
  const width = Math.max(0, Math.min(first.right, second.right) - Math.max(first.left, second.left));
  const height = Math.max(0, Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top));
  const overlap = width * height;
  const firstArea = Math.max(1, (first.right - first.left) * (first.bottom - first.top));
  const secondArea = Math.max(1, (second.right - second.left) * (second.bottom - second.top));
  return overlap / Math.min(firstArea, secondArea);
}

function auditText(entries, film, pointName, { transition = false } = {}) {
  const visible = entries.filter((entry) => entry.alpha >= 0.35 && entry.text.trim());
  const essential = visible.filter((entry) => entry.fontSize >= 42);
  const boundary = visible.some((entry) => entry.text === production.persistent_notice);
  requireCondition(boundary, `${film.slug}/${pointName}: persistent evidence boundary is not visible.`);
  if (!transition) {
    requireCondition(essential.length > 0, `${film.slug}/${pointName}: no essential copy at 42 px or larger.`);
  }
  for (const entry of visible) {
    requireCondition(Number.isFinite(entry.width) && entry.width > 0, `${film.slug}/${pointName}: text was not measurable: ${entry.text}`);
    requireCondition(entry.left >= -1 && entry.top >= -1 && entry.right <= production.width + 1 && entry.bottom <= production.height + 1, `${film.slug}/${pointName}: clipped text: ${entry.text}`);
  }
  for (const entry of essential) {
    const withinSafeMargin = entry.left >= 88 && entry.right <= production.width - 88 && entry.top >= 64 && entry.bottom <= 972;
    requireCondition(withinSafeMargin, `${film.slug}/${pointName}: essential copy crosses the 96 px authored safe area: ${entry.text} (${entry.left.toFixed(1)}, ${entry.top.toFixed(1)}, ${entry.right.toFixed(1)}, ${entry.bottom.toFixed(1)}).`);
  }
  if (!transition) {
    for (let firstIndex = 0; firstIndex < essential.length; firstIndex += 1) {
      for (let secondIndex = firstIndex + 1; secondIndex < essential.length; secondIndex += 1) {
        const first = essential[firstIndex];
        const second = essential[secondIndex];
        if (first.text === second.text) continue;
        const ratio = overlapRatio(first, second);
        requireCondition(ratio < 0.12, `${film.slug}/${pointName}: essential text collision (${(ratio * 100).toFixed(1)}%): “${first.text}” / “${second.text}”.`);
      }
    }
  }
  return { visibleText: visible.length, essentialText: essential.length };
}

const instrumentation = String.raw`
(() => {
  const entries = [];
  const original = CanvasRenderingContext2D.prototype.fillText;
  const transformedBounds = (ctx, left, top, right, bottom) => {
    const matrix = ctx.getTransform();
    const points = [
      new DOMPoint(left, top), new DOMPoint(right, top),
      new DOMPoint(left, bottom), new DOMPoint(right, bottom),
    ].map((point) => point.matrixTransform(matrix));
    return {
      left: Math.min(...points.map((point) => point.x)),
      top: Math.min(...points.map((point) => point.y)),
      right: Math.max(...points.map((point) => point.x)),
      bottom: Math.max(...points.map((point) => point.y)),
    };
  };
  CanvasRenderingContext2D.prototype.fillText = function(value, x, y, maxWidth) {
    const text = String(value);
    const metrics = this.measureText(text);
    const match = this.font.match(/([0-9]+(?:\.[0-9]+)?)px/);
    const fontSize = match ? Number(match[1]) : 0;
    let width = metrics.width;
    if (Number.isFinite(maxWidth)) width = Math.min(width, Number(maxWidth));
    let left = Number(x);
    if (this.textAlign === "center") left -= width / 2;
    else if (this.textAlign === "right" || this.textAlign === "end") left -= width;
    const ascent = metrics.actualBoundingBoxAscent || fontSize * 0.8;
    const descent = metrics.actualBoundingBoxDescent || fontSize * 0.2;
    let top = Number(y) - ascent;
    let bottom = Number(y) + descent;
    if (this.textBaseline === "top" || this.textBaseline === "hanging") {
      top = Number(y);
      bottom = Number(y) + ascent + descent;
    } else if (this.textBaseline === "middle") {
      top = Number(y) - (ascent + descent) / 2;
      bottom = Number(y) + (ascent + descent) / 2;
    } else if (this.textBaseline === "bottom" || this.textBaseline === "ideographic") {
      top = Number(y) - ascent - descent;
      bottom = Number(y);
    }
    entries.push({
      text,
      font: this.font,
      fontSize,
      alpha: this.globalAlpha,
      width,
      ...transformedBounds(this, left, top, left + width, bottom),
    });
    return original.call(this, value, x, y, maxWidth);
  };
  window.__canvasTextAudit = {
    clear() { entries.length = 0; },
    read() { return entries.map((entry) => ({ ...entry })); },
  };
})();`;

const browser = await chromium.launch({
  executablePath: chrome,
  headless: true,
  args: [
    "--allow-file-access-from-files",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
  ],
});

async function openInstrumented(film, reducedMotion = "no-preference") {
  const context = await browser.newContext({
    viewport: { width: production.width, height: production.height },
    deviceScaleFactor: 1,
    colorScheme: "light",
    reducedMotion,
  });
  await context.addInitScript({ content: instrumentation });
  const page = await context.newPage();
  const pageErrors = [];
  const networkRequests = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") pageErrors.push(message.text());
  });
  page.on("request", (request) => {
    if (/^https?:/.test(request.url())) networkRequests.push(request.url());
  });
  await page.goto(pathToFileURL(resolve(PACKAGE_DIR, `${film.slug}.html`)).href, { waitUntil: "load" });
  await page.waitForFunction(() => window.__animationReady === true, null, { timeout: 20_000 });
  requireCondition(pageErrors.length === 0, `${film.slug}: runtime errors: ${pageErrors.join(" | ")}`);
  requireCondition(networkRequests.length === 0, `${film.slug}: network requests are forbidden: ${networkRequests.join(", ")}`);
  return { context, page };
}

async function auditAt(page, film, point, transition = false) {
  const entries = await page.evaluate((time) => {
    window.__canvasTextAudit.clear();
    window.setAnimationTime(time);
    return window.__canvasTextAudit.read();
  }, point.time);
  return auditText(entries, film, point.name, { transition });
}

async function semanticHold(page, film, point) {
  const textsAt = async (time) => page.evaluate((target) => {
    window.__canvasTextAudit.clear();
    window.setAnimationTime(target);
    return window.__canvasTextAudit.read()
      .filter((entry) => entry.alpha >= 0.55 && entry.fontSize >= 42)
      .map((entry) => entry.text);
  }, time);
  const before = new Set(await textsAt(Math.max(0, point.time - 0.15)));
  const center = new Set(await textsAt(point.time));
  const after = new Set(await textsAt(Math.min(film.duration - 1 / production.fps, point.time + 0.15)));
  const stable = [...center].filter((text) => before.has(text) && after.has(text));
  requireCondition(stable.length >= Math.min(2, center.size), `${film.slug}/${point.name}: authored checkpoint does not hold essential copy for 0.3 seconds.`);
  return stable;
}

try {
  for (const film of selectedFilms) {
    const media = mediaSpec.clips.find((clip) => clip.key === film.key);
    requireCondition(media?.title === film.title && media?.target_duration_seconds === film.duration, `${film.slug}: report media contract drifted.`);
    const sourcePaths = [
      resolve(PACKAGE_DIR, `${film.slug}.html`),
      resolve(PACKAGE_DIR, "src/runtime.js"),
      resolve(PACKAGE_DIR, "src/styles.css"),
      resolve(PACKAGE_DIR, "data/animation-data.js"),
    ];
    for (const sourcePath of sourcePaths) {
      const source = await readFile(sourcePath, "utf8");
      requireCondition(!/(?:src|href)\s*=\s*["']https?:\/\//i.test(source) && !/url\(\s*["']?https?:\/\//i.test(source), `${film.slug}: network asset found in ${sourcePath}.`);
    }

    const clip = manifest.clips.find((item) => item.slug === film.slug);
    requireCondition(clip, `${film.slug}: missing from dist/manifest.json.`);
    requireCondition(clip.key === film.key && clip.frame_count === film.duration * production.fps, `${film.slug}: manifest identity or frame count drifted.`);
    const outputDir = resolve(DIST_DIR, film.slug);
    await validateImage(resolve(PACKAGE_DIR, clip.poster), 1920, 1080);
    await validateImage(resolve(PACKAGE_DIR, clip.poster_report_scale), 960, 540);
    requireCondition(await sha256File(resolve(PACKAGE_DIR, clip.poster)) === clip.poster_sha256, `${film.slug}: poster hash mismatch.`);
    requireCondition(await sha256File(resolve(PACKAGE_DIR, clip.poster_report_scale)) === clip.poster_report_scale_sha256, `${film.slug}: 50% poster hash mismatch.`);
    for (const point of [...clip.checkpoints, ...clip.transitions]) {
      await validateImage(resolve(PACKAGE_DIR, point.path), 1920, 1080);
      await validateImage(resolve(PACKAGE_DIR, point.report_scale_path), 960, 540);
      requireCondition(await sha256File(resolve(PACKAGE_DIR, point.path)) === point.sha256, `${film.slug}/${point.name}: native checkpoint hash mismatch.`);
      requireCondition(await sha256File(resolve(PACKAGE_DIR, point.report_scale_path)) === point.report_scale_sha256, `${film.slug}/${point.name}: 50% checkpoint hash mismatch.`);
    }
    requireCondition(clip.caption === media.caption, `${film.slug}: canonical report caption drifted.`);
    requireCondition(await sha256File(resolve(PACKAGE_DIR, clip.captions)) === clip.captions_sha256, `${film.slug}: caption-file hash mismatch.`);
    await access(resolve(PACKAGE_DIR, clip.contact_sheet));
    requireCondition(await sha256File(resolve(PACKAGE_DIR, clip.contact_sheet)) === clip.contact_sheet_sha256, `${film.slug}: contact-sheet hash mismatch.`);

    const { context, page } = await openInstrumented(film);
    try {
      const runtimeSpec = await page.evaluate(() => window.__filmSpec);
      validateRuntimeSpec(runtimeSpec, film, production);
      const runtimeState = await page.evaluate(() => ({
        ready: window.__animationReady,
        canvas: [film.width, film.height],
        max: Number(scrub.max),
        fonts: document.fonts.status,
        hooks: [typeof window.setAnimationTime, typeof window.releaseAnimation],
      }));
      requireCondition(runtimeState.ready && runtimeState.canvas[0] === 1920 && runtimeState.canvas[1] === 1080 && runtimeState.max === film.duration && runtimeState.fonts === "loaded" && runtimeState.hooks.every((kind) => kind === "function"), `${film.slug}: incomplete runtime contract: ${JSON.stringify(runtimeState)}`);

      await page.evaluate(() => { document.querySelector(".controls").hidden = true; });
      const posterPixels = async () => page.evaluate((time) => {
        window.setAnimationTime(time);
        const canvas = document.querySelector("#film");
        const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
        let first = 2166136261;
        let second = 5381;
        for (let index = 0; index < pixels.length; index += 1) {
          first = Math.imul(first ^ pixels[index], 16777619);
          second = Math.imul(second, 33) ^ pixels[index];
        }
        return [first >>> 0, second >>> 0, pixels.length];
      }, film.poster_time);
      const firstPoster = await posterPixels();
      const secondPoster = await posterPixels();
      requireCondition(JSON.stringify(firstPoster) === JSON.stringify(secondPoster), `${film.slug}: exact-time poster pixels are nondeterministic.`);

      const checkpointAudits = [];
      for (const point of film.checkpoints) {
        checkpointAudits.push({ name: point.name, ...(await auditAt(page, film, point)), stable: await semanticHold(page, film, point) });
      }
      const transitionAudits = [];
      for (const point of film.transition_checkpoints) {
        transitionAudits.push({ name: point.name, ...(await auditAt(page, film, point, true)) });
      }
      const posterAudit = await auditAt(page, film, { name: "poster", time: film.poster_time });
      requireCondition(posterAudit.visibleText >= 5 && posterAudit.essentialText >= 2, `${film.slug}: poster does not communicate enough without motion.`);

      await page.evaluate(() => { document.querySelector(".controls").hidden = false; });
      await page.evaluate(() => window.releaseAnimation());
      const toggle = page.locator("#toggle");
      await toggle.click();
      requireCondition(await toggle.getAttribute("aria-label") === "Play animation", `${film.slug}: pause control label drifted.`);
      await toggle.focus();
      await page.keyboard.press("Space");
      requireCondition(await toggle.getAttribute("aria-label") === "Pause animation", `${film.slug}: keyboard playback failed.`);
      await toggle.click();
      const scrub = page.locator("#scrub");
      await scrub.evaluate((element, midpoint) => {
        element.value = String(midpoint);
        element.dispatchEvent(new Event("input", { bubbles: true }));
      }, film.duration / 2);
      const beforeArrow = Number(await scrub.inputValue());
      await scrub.focus();
      await page.keyboard.press("ArrowRight");
      requireCondition(Number(await scrub.inputValue()) > beforeArrow, `${film.slug}: keyboard scrubbing did not advance.`);

      await page.setViewportSize({ width: 960, height: 600 });
      const responsive = await page.locator("main").evaluate((element) => {
        const box = element.getBoundingClientRect();
        return [box.width, box.height];
      });
      requireCondition(Math.abs(responsive[0] / responsive[1] - 16 / 9) < 0.01, `${film.slug}: responsive aspect ratio changed.`);

      filmResults.push({
        slug: film.slug,
        deterministic_poster_sha256: await sha256File(resolve(PACKAGE_DIR, clip.poster)),
        checkpoint_audits: checkpointAudits,
        transition_audits: transitionAudits,
        responsive: responsive.map(Math.round),
      });
    } finally {
      await context.close();
    }

    const reduced = await openInstrumented(film, "reduce");
    try {
      const state = await reduced.page.evaluate(() => ({
        time: Number(scrub.value),
        text: toggle.textContent,
        label: toggle.getAttribute("aria-label"),
      }));
      requireCondition(Math.abs(state.time - film.reduced_motion_time) < 0.01 && state.text === "Play" && state.label === "Play animation", `${film.slug}: reduced-motion state drifted: ${JSON.stringify(state)}`);
    } finally {
      await reduced.context.close();
    }

    if (!options.stillsOnly) {
      const videoPath = resolve(PACKAGE_DIR, clip.output);
      requireCondition(await sha256File(videoPath) === clip.sha256, `${film.slug}: video hash mismatch.`);
      const details = await probe(videoPath, true);
      const video = details.streams.find((stream) => stream.codec_type === "video");
      const audio = details.streams.find((stream) => stream.codec_type === "audio");
      const fps = parseRate(video?.r_frame_rate);
      const averageFps = parseRate(video?.avg_frame_rate);
      const frames = Number(video?.nb_read_frames);
      const duration = Number(details.format.duration);
      requireCondition(video && !audio && video.codec_name === "h264" && video.profile === "High" && video.pix_fmt === "yuv420p" && video.color_range === "tv", `${film.slug}: codec/pixel/audio contract failed.`);
      requireCondition(video.width === 1920 && video.height === 1080 && fps === 30 && averageFps === 30 && frames === film.duration * 30, `${film.slug}: dimensions, CFR, or frame count failed.`);
      requireCondition(Math.abs(duration - film.duration) <= 1 / 30, `${film.slug}: duration is ${duration}, expected ${film.duration}.`);
      await run(ffmpeg, ["-v", "error", "-i", videoPath, "-f", "null", "-"]);
    }

    process.stdout.write(`validated ${film.slug}${options.stillsOnly ? " stills" : " production media"}\n`);
  }
} catch (error) {
  errors.push(error instanceof Error ? error.message : String(error));
} finally {
  await browser.close();
}

if (!options.stillsOnly && !options.film) {
  requireCondition(manifest.review_reel, "Production manifest is missing the review reel.");
  const reviewPath = resolve(PACKAGE_DIR, manifest.review_reel.path);
  requireCondition(await sha256File(reviewPath) === manifest.review_reel.sha256, "Review-reel hash mismatch.");
  const review = await probe(reviewPath, true);
  const video = review.streams.find((stream) => stream.codec_type === "video");
  const audio = review.streams.find((stream) => stream.codec_type === "audio");
  const expectedFrames = production.films.reduce((total, film) => total + film.duration * production.fps, 0);
  const expectedDuration = production.films.reduce((total, film) => total + film.duration, 0);
  requireCondition(video && !audio && video.codec_name === "h264" && video.profile === "High" && video.pix_fmt === "yuv420p", "Review-reel media contract failed.");
  requireCondition(video.width === 1920 && video.height === 1080 && parseRate(video.r_frame_rate) === 30 && parseRate(video.avg_frame_rate) === 30 && Number(video.nb_read_frames) === expectedFrames, "Review-reel dimensions, CFR, or frame count failed.");
  requireCondition(Math.abs(Number(review.format.duration) - expectedDuration) <= 1 / 30, "Review-reel duration failed.");
  await run(ffmpeg, ["-v", "error", "-i", reviewPath, "-f", "null", "-"]);
}

for (const source of manifest.source_files) {
  const sourcePath = resolve(PACKAGE_DIR, source.path);
  requireCondition(await sha256File(sourcePath) === source.sha256, `Manifest source hash drifted: ${source.path}`);
}
requireCondition(await sha256File(resolve(PACKAGE_DIR, manifest.contact_sheet.path)) === manifest.contact_sheet.sha256, "Global contact-sheet hash mismatch.");
requireCondition(await sha256File(resolve(PACKAGE_DIR, manifest.combined_caption.path)) === manifest.combined_caption.sha256, "Combined caption hash mismatch.");

const receipt = {
  schema_version: "1.0",
  ok: errors.length === 0,
  mode: options.stillsOnly ? "stills-only" : "production",
  publication_id: manifest.publication_id,
  clip_count: filmResults.length,
  films: filmResults,
  errors,
};
await writeFile(resolve(DIST_DIR, "validation.json"), `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
if (errors.length) throw new Error(errors.join("\n"));
process.stdout.write(`${JSON.stringify({ ok: true, mode: receipt.mode, publication_id: receipt.publication_id, clip_count: receipt.clip_count }, null, 2)}\n`);
