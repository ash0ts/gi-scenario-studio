import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { access, readFile } from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const REPO_DIR = resolve(PACKAGE_DIR, "../..");
export const DIST_DIR = resolve(PACKAGE_DIR, "dist");
export const FILMS_PATH = resolve(PACKAGE_DIR, "story/films.json");
export const CAPTIONS_PATH = resolve(PACKAGE_DIR, "story/captions.json");
export const MEDIA_SPEC_PATH = resolve(REPO_DIR, "media/gi-scenario-film/story/report-animation-media.json");

export function parseCli(argv) {
  const options = { stillsOnly: false, film: null };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--stills-only") options.stillsOnly = true;
    else if (argument === "--film") {
      options.film = argv[index + 1];
      index += 1;
      if (!options.film) throw new Error("--film requires a film slug.");
    } else if (argument === "--help") options.help = true;
    else throw new Error(`Unknown argument: ${argument}`);
  }
  return options;
}

export async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

export async function sha256File(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

export function sha256Text(value) {
  return createHash("sha256").update(value).digest("hex");
}

function onPath(name) {
  const result = spawnSync("/usr/bin/which", [name], { encoding: "utf8" });
  return result.status === 0 ? result.stdout.trim() : null;
}

async function executable(path) {
  if (!path) return false;
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export async function resolveBinary(envName, candidates, commands) {
  for (const candidate of [process.env[envName], ...candidates, ...commands.map(onPath)]) {
    if (await executable(candidate)) return candidate;
  }
  throw new Error(`Unable to find ${envName.replace("_BIN", "")}. Set ${envName} to an executable path.`);
}

export async function resolveProductionBinaries({ includeProbe = false } = {}) {
  const chrome = await resolveBinary(
    "CHROME_BIN",
    [
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
  );
  const ffmpeg = await resolveBinary(
    "FFMPEG_BIN",
    ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
    ["ffmpeg"],
  );
  const binaries = { chrome, ffmpeg };
  if (includeProbe) {
    binaries.ffprobe = await resolveBinary(
      "FFPROBE_BIN",
      ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"],
      ["ffprobe"],
    );
  }
  return binaries;
}

export function run(command, args, { capture = false, cwd = PACKAGE_DIR } = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      cwd,
      stdio: ["ignore", capture ? "pipe" : "ignore", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolvePromise(stdout);
      else reject(new Error(`${command} exited ${code}: ${stderr.slice(-6000)}`));
    });
  });
}

export function validateFilmRecord(film, production) {
  if (!film || typeof film !== "object") throw new Error("Film record is missing.");
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(film.slug)) throw new Error(`Unsafe film slug: ${film.slug}`);
  if (!film.title || !Number.isFinite(film.duration) || film.duration <= 0) {
    throw new Error(`Invalid title or duration for ${film.slug}.`);
  }
  if (!Number.isInteger(film.duration * production.fps)) {
    throw new Error(`${film.slug}: duration × fps must be an integer.`);
  }
  for (const key of ["poster_time", "reduced_motion_time"]) {
    if (!Number.isFinite(film[key]) || film[key] < 0 || film[key] >= film.duration) {
      throw new Error(`${film.slug}: invalid ${key}.`);
    }
  }
  const names = new Set([film.slug, `${film.slug}-poster`, "contact-sheet"]);
  const points = [...(film.checkpoints ?? []), ...(film.transition_checkpoints ?? [])];
  if (!(film.checkpoints?.length > 0)) throw new Error(`${film.slug}: no conclusion checkpoints.`);
  if (!(film.transition_checkpoints?.length > 0)) throw new Error(`${film.slug}: no transition checkpoints.`);
  for (const point of points) {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(point.name) || names.has(point.name)) {
      throw new Error(`${film.slug}: invalid or duplicate checkpoint ${point.name}.`);
    }
    if (!Number.isFinite(point.time) || point.time < 0 || point.time >= film.duration) {
      throw new Error(`${film.slug}: checkpoint ${point.name} is outside the film.`);
    }
    names.add(point.name);
  }
  return true;
}

export function validateCaptions(film, captions) {
  if (!Array.isArray(captions) || captions.length === 0) throw new Error(`${film.slug}: captions are missing.`);
  let previousEnd = 0;
  for (const cue of captions) {
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.start < previousEnd || cue.end <= cue.start || cue.end > film.duration) {
      throw new Error(`${film.slug}: invalid or overlapping caption cue.`);
    }
    if (!cue.text?.trim()) throw new Error(`${film.slug}: empty caption cue.`);
    previousEnd = cue.end;
  }
  if (captions[0].start !== 0 || Math.abs(previousEnd - film.duration) > 1e-9) {
    throw new Error(`${film.slug}: captions must cover the complete film.`);
  }
  return true;
}

function srtTime(seconds) {
  const milliseconds = Math.round(seconds * 1000);
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
  const secs = Math.floor((milliseconds % 60_000) / 1000);
  const millis = milliseconds % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}

export function captionsToSrt(captions, offset = 0, startIndex = 1) {
  return captions.map((cue, index) => [
    String(startIndex + index),
    `${srtTime(cue.start + offset)} --> ${srtTime(cue.end + offset)}`,
    cue.text,
    "",
  ].join("\n")).join("\n");
}

export function relativeToPackage(path) {
  return relative(PACKAGE_DIR, path).split("\\").join("/");
}

export function validateRuntimeSpec(spec, film, production) {
  if (!spec || spec.silent !== true) throw new Error(`${film.slug}: runtime must declare a silent film.`);
  const expected = {
    slug: film.slug,
    title: film.title,
    width: production.width,
    height: production.height,
    duration: film.duration,
    fps: production.fps,
    posterTime: film.poster_time,
    reducedMotionTime: film.reduced_motion_time,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (spec[key] !== value) throw new Error(`${film.slug}: runtime ${key} drifted. Expected ${value}, found ${spec[key]}.`);
  }
  if (JSON.stringify(spec.checkpoints) !== JSON.stringify(film.checkpoints)) {
    throw new Error(`${film.slug}: runtime checkpoints drifted from story/films.json.`);
  }
  return true;
}

export function manifestDigestInput(manifest) {
  const projection = {
    schema_version: manifest.schema_version,
    production: manifest.production,
    source_files: manifest.source_files,
    clips: manifest.clips.map((clip) => ({
      key: clip.key,
      slug: clip.slug,
      duration_seconds: clip.duration_seconds,
      frame_count: clip.frame_count,
      video_sha256: clip.sha256,
      poster_sha256: clip.poster_sha256,
      caption: clip.caption,
      captions_sha256: clip.captions_sha256,
      checkpoints: clip.checkpoints,
      transitions: clip.transitions,
    })),
  };
  return JSON.stringify(projection);
}
