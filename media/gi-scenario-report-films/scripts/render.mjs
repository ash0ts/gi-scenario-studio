import { chromium } from "@playwright/test";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
  CAPTIONS_PATH,
  DIST_DIR,
  FILMS_PATH,
  MEDIA_SPEC_PATH,
  PACKAGE_DIR,
  captionsToSrt,
  manifestDigestInput,
  parseCli,
  readJson,
  relativeToPackage,
  resolveProductionBinaries,
  run,
  sha256File,
  sha256Text,
  validateCaptions,
  validateFilmRecord,
  validateRuntimeSpec,
} from "./toolchain.mjs";

const HELP = `Usage: node scripts/render.mjs [--stills-only] [--film <slug>]

Renders all five deterministic report films. --stills-only creates native and
50% posters, conclusion checkpoints, transition checkpoints, captions, contact
sheets, and a partial manifest without encoding MP4 video.`;

const options = parseCli(process.argv.slice(2));
if (options.help) {
  process.stdout.write(`${HELP}\n`);
  process.exit(0);
}

const production = await readJson(FILMS_PATH);
const captionStory = await readJson(CAPTIONS_PATH);
const mediaSpec = await readJson(MEDIA_SPEC_PATH);
if (production.schema_version !== "1.0" || captionStory.schema_version !== "1.0") {
  throw new Error("Unsupported film or caption story schema.");
}
if (production.width !== 1920 || production.height !== 1080 || production.fps !== 30) {
  throw new Error("The W&B report film contract requires 1920×1080 at 30 fps.");
}

const allFilms = production.films;
const selectedFilms = options.film
  ? allFilms.filter((film) => film.slug === options.film)
  : allFilms;
if (selectedFilms.length === 0) throw new Error(`Unknown film slug: ${options.film}`);
for (const film of selectedFilms) {
  validateFilmRecord(film, production);
  validateCaptions(film, captionStory.films[film.slug]);
}

const mediaByKey = new Map(mediaSpec.clips.map((clip) => [clip.key, clip]));
for (const film of selectedFilms) {
  const media = mediaByKey.get(film.key);
  if (!media || media.title !== film.title || media.target_duration_seconds !== film.duration) {
    throw new Error(`${film.slug}: report-animation-media.json drifted from the film story.`);
  }
}

const { chrome, ffmpeg } = await resolveProductionBinaries();
await mkdir(DIST_DIR, { recursive: true });

function log(message) {
  process.stdout.write(`${message}\n`);
}

async function capture(page, time, path, type = "png") {
  const dataUrl = await page.evaluate(({ target, format }) => {
    window.setAnimationTime(target);
    return document.querySelector("#film").toDataURL(
      format === "jpeg" ? "image/jpeg" : "image/png",
      format === "jpeg" ? 0.92 : undefined,
    );
  }, { target: time, format: type });
  await writeFile(path, Buffer.from(dataUrl.slice(dataUrl.indexOf(",") + 1), "base64"));
}

async function makeHalfScale(source, target) {
  await run(ffmpeg, [
    "-v", "error", "-y", "-i", source,
    "-vf", "scale=960:540:flags=lanczos",
    "-frames:v", "1", target,
  ]);
}

async function makeContactSheet(inputs, output) {
  const thumbWidth = 480;
  const thumbHeight = 270;
  const columns = Math.min(4, inputs.length);
  const ffmpegInputs = inputs.flatMap((input) => ["-i", input]);
  const scales = inputs
    .map((_, index) => `[${index}:v]scale=${thumbWidth}:${thumbHeight}:flags=lanczos[v${index}]`)
    .join(";");
  const streams = inputs.map((_, index) => `[v${index}]`).join("");
  const layout = inputs
    .map((_, index) => `${(index % columns) * thumbWidth}_${Math.floor(index / columns) * thumbHeight}`)
    .join("|");
  const filter = `${scales};${streams}xstack=inputs=${inputs.length}:layout=${layout}:fill=#F5F7F6[out]`;
  await run(ffmpeg, [
    "-v", "error", "-y", ...ffmpegInputs,
    "-filter_complex", filter,
    "-map", "[out]", "-frames:v", "1", output,
  ]);
}

async function renderFilm(browser, film) {
  const outputDir = resolve(DIST_DIR, film.slug);
  await mkdir(outputDir, { recursive: true });
  const frameDir = await mkdtemp(join(tmpdir(), `${film.slug}-frames-`));
  const errors = [];
  const networkRequests = [];
  const context = await browser.newContext({
    viewport: { width: production.width, height: production.height },
    deviceScaleFactor: 1,
    colorScheme: "light",
    reducedMotion: "no-preference",
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("request", (request) => {
    if (/^https?:/.test(request.url())) networkRequests.push(request.url());
  });

  try {
    const htmlPath = resolve(PACKAGE_DIR, `${film.slug}.html`);
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
    await page.waitForFunction(() => window.__animationReady === true, null, { timeout: 20_000 });
    await page.evaluate(() => { document.querySelector(".controls").hidden = true; });
    const spec = await page.evaluate(() => window.__filmSpec);
    validateRuntimeSpec(spec, film, production);
    if (errors.length) throw new Error(`${film.slug}: page errors: ${errors.join(" | ")}`);
    if (networkRequests.length) throw new Error(`${film.slug}: network assets are forbidden: ${networkRequests.join(", ")}`);

    const posterPath = resolve(outputDir, `${film.slug}-poster.png`);
    const posterHalfPath = resolve(outputDir, `${film.slug}-poster-50.png`);
    await capture(page, film.poster_time, posterPath);
    await makeHalfScale(posterPath, posterHalfPath);
    log(`${film.slug}: poster @ ${film.poster_time.toFixed(2)}s`);

    const pointArtifacts = [];
    for (const [kind, points] of [
      ["conclusion", film.checkpoints],
      ["transition", film.transition_checkpoints],
    ]) {
      for (const point of points) {
        const nativePath = resolve(outputDir, `${point.name}.png`);
        const halfPath = resolve(outputDir, `${point.name}-50.png`);
        await capture(page, point.time, nativePath);
        await makeHalfScale(nativePath, halfPath);
        pointArtifacts.push({ kind, ...point, nativePath, halfPath });
        log(`${film.slug}: ${kind} ${point.name} @ ${point.time.toFixed(2)}s`);
      }
    }

    const captionPath = resolve(outputDir, `${film.slug}.srt`);
    await writeFile(captionPath, `${captionsToSrt(captionStory.films[film.slug])}\n`, "utf8");

    const contactSheetPath = resolve(outputDir, "contact-sheet.png");
    await makeContactSheet([posterPath, ...pointArtifacts.map((point) => point.nativePath)], contactSheetPath);

    const frameCount = Math.round(film.duration * production.fps);
    const videoPath = resolve(outputDir, `${film.slug}.mp4`);
    if (!options.stillsOnly) {
      for (let frame = 0; frame < frameCount; frame += 1) {
        const target = resolve(frameDir, `frame-${String(frame).padStart(6, "0")}.jpg`);
        await capture(page, frame / production.fps, target, "jpeg");
        if ((frame + 1) % (production.fps * 5) === 0 || frame + 1 === frameCount) {
          log(`${film.slug}: rendered ${frame + 1}/${frameCount} frames`);
        }
      }
      await run(ffmpeg, [
        "-v", "error", "-y",
        "-framerate", String(production.fps),
        "-start_number", "0",
        "-i", resolve(frameDir, "frame-%06d.jpg"),
        "-frames:v", String(frameCount),
        "-vf", `scale=${production.width}:${production.height}:in_range=pc:out_range=tv:flags=lanczos,format=yuv420p`,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-an",
        "-movflags", "+faststart",
        videoPath,
      ]);
      log(`${film.slug}: encoded ${basename(videoPath)}`);
    }

    const checkpointManifest = async (point) => ({
      name: point.name,
      time_seconds: point.time,
      path: relativeToPackage(point.nativePath),
      sha256: await sha256File(point.nativePath),
      report_scale_path: relativeToPackage(point.halfPath),
      report_scale_sha256: await sha256File(point.halfPath),
    });
    const conclusions = [];
    const transitions = [];
    for (const point of pointArtifacts) {
      const record = await checkpointManifest(point);
      if (point.kind === "conclusion") conclusions.push(record);
      else transitions.push(record);
    }

    return {
      key: film.key,
      slug: film.slug,
      title: film.title,
      claim_ids: film.claim_ids,
      duration_seconds: film.duration,
      width: production.width,
      height: production.height,
      fps: production.fps,
      frame_count: frameCount,
      rendered_frame_count: options.stillsOnly ? 0 : frameCount,
      output: options.stillsOnly ? null : relativeToPackage(videoPath),
      sha256: options.stillsOnly ? null : await sha256File(videoPath),
      poster: relativeToPackage(posterPath),
      poster_sha256: await sha256File(posterPath),
      poster_report_scale: relativeToPackage(posterHalfPath),
      poster_report_scale_sha256: await sha256File(posterHalfPath),
      caption: mediaByKey.get(film.key).caption,
      captions: relativeToPackage(captionPath),
      captions_sha256: await sha256File(captionPath),
      contact_sheet: relativeToPackage(contactSheetPath),
      contact_sheet_sha256: await sha256File(contactSheetPath),
      checkpoints: conclusions,
      transitions,
    };
  } finally {
    await context.close();
    await rm(frameDir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
  }
}

const browser = await chromium.launch({
  executablePath: chrome,
  headless: true,
  args: [
    "--allow-file-access-from-files",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-features=Translate,PaintHolding",
    "--disable-sync",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
  ],
});

let clips;
try {
  clips = [];
  for (const film of selectedFilms) clips.push(await renderFilm(browser, film));
} finally {
  await browser.close();
}

const globalContactSheet = resolve(DIST_DIR, "contact-sheet.png");
await makeContactSheet(clips.map((clip) => resolve(PACKAGE_DIR, clip.poster)), globalContactSheet);

let combinedOffset = 0;
let combinedCueIndex = 1;
const combinedCaptionBlocks = [];
for (const film of selectedFilms) {
  const captions = captionStory.films[film.slug];
  combinedCaptionBlocks.push(captionsToSrt(captions, combinedOffset, combinedCueIndex));
  combinedOffset += film.duration;
  combinedCueIndex += captions.length;
}
const combinedCaptionPath = resolve(DIST_DIR, "review-reel.srt");
await writeFile(combinedCaptionPath, `${combinedCaptionBlocks.join("\n")}\n`, "utf8");

let reviewReel = null;
if (!options.stillsOnly && selectedFilms.length === allFilms.length) {
  const concatPath = resolve(DIST_DIR, ".review-reel-concat.txt");
  const concatBody = clips
    .map((clip) => `file '${resolve(PACKAGE_DIR, clip.output).replaceAll("'", "'\\''")}'`)
    .join("\n");
  await writeFile(concatPath, `${concatBody}\n`, "utf8");
  const reviewPath = resolve(DIST_DIR, "review-reel.mp4");
  try {
    await run(ffmpeg, [
      "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", concatPath,
      "-c:v", "copy", "-an", "-movflags", "+faststart", reviewPath,
    ]);
  } finally {
    await rm(concatPath, { force: true });
  }
  reviewReel = {
    path: relativeToPackage(reviewPath),
    sha256: await sha256File(reviewPath),
    duration_seconds: selectedFilms.reduce((total, film) => total + film.duration, 0),
    frame_count: selectedFilms.reduce((total, film) => total + film.duration * production.fps, 0),
  };
}

const sourcePaths = [
  FILMS_PATH,
  CAPTIONS_PATH,
  MEDIA_SPEC_PATH,
  resolve(PACKAGE_DIR, "data/animation-data.json"),
  resolve(PACKAGE_DIR, "data/animation-data.js"),
  resolve(PACKAGE_DIR, "src/runtime.js"),
  resolve(PACKAGE_DIR, "src/styles.css"),
  resolve(PACKAGE_DIR, "scripts/build_animation_data.py"),
  resolve(PACKAGE_DIR, "scripts/render.mjs"),
  resolve(PACKAGE_DIR, "scripts/validate.mjs"),
  resolve(PACKAGE_DIR, "scripts/toolchain.mjs"),
  resolve(PACKAGE_DIR, "story/analysis-contract.md"),
  resolve(PACKAGE_DIR, "story/claim-ledger.md"),
  ...selectedFilms.map((film) => resolve(PACKAGE_DIR, `${film.slug}.html`)),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Regular.ttf"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Bold.ttf"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/IBMPlexSansCondensed-SemiBold.ttf"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/IBMPlexMono-Regular.ttf"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/IBMPlexMono-Medium.ttf"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/ATKINSON-OFL.txt"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/IBM-PLEX-OFL.txt"),
  resolve(PACKAGE_DIR, "../gi-scenario-film/assets/fonts/SHA256SUMS"),
];
const sourceFiles = [];
for (const path of sourcePaths) {
  sourceFiles.push({ path: relativeToPackage(path), sha256: await sha256File(path) });
}

const manifest = {
  schema_version: "1.0",
  publication_kind: "evidence_grounded_canvas_films",
  publication_id: null,
  width: production.width,
  height: production.height,
  fps: production.fps,
  complete: !options.stillsOnly && selectedFilms.length === allFilms.length,
  render_mode: options.stillsOnly ? "stills-only" : "production",
  production: {
    width: production.width,
    height: production.height,
    fps: production.fps,
    codec: "h264",
    profile: "High",
    pixel_format: "yuv420p",
    color_range: "tv",
    audio: false,
    safe_margin_px: 96,
    report_scale: 0.5,
  },
  persistent_boundary: production.persistent_notice,
  clip_count: clips.length,
  source_files: sourceFiles,
  clips,
  contact_sheet: {
    path: relativeToPackage(globalContactSheet),
    sha256: await sha256File(globalContactSheet),
  },
  review_reel: reviewReel,
  combined_caption: {
    path: relativeToPackage(combinedCaptionPath),
    sha256: await sha256File(combinedCaptionPath),
  },
};
const digest = sha256Text(manifestDigestInput(manifest));
manifest.source_digest_sha256 = digest;
manifest.publication_id = `gi-report-films-${digest.slice(0, 16)}`;

const manifestPath = resolve(DIST_DIR, "manifest.json");
await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
log(`wrote ${relativeToPackage(manifestPath)} (${manifest.publication_id})`);
