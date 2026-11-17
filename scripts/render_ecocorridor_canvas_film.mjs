import { spawn, spawnSync } from 'node:child_process';
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const htmlPath = resolve(process.argv[2] ?? 'film.html');
const outputDir = resolve(process.argv[3] ?? dirname(htmlPath));
const stillsOnly = process.env.ECOCORRIDOR_STILLS_ONLY === '1';

function onPath(name) {
  const result = spawnSync('/usr/bin/which', [name], { encoding: 'utf8' });
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

async function resolveBinary(envName, candidates, commands) {
  const choices = [process.env[envName], ...candidates, ...commands.map(onPath)];
  for (const candidate of choices) if (await executable(candidate)) return candidate;
  throw new Error(`Unable to find ${envName.replace('_BIN', '')}. Set ${envName} to an executable path.`);
}

const CHROME = await resolveBinary('CHROME_BIN', [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium'
], ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']);
const FFMPEG = await resolveBinary('FFMPEG_BIN', [
  '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'
], ['ffmpeg']);

await mkdir(outputDir, { recursive: true });
const profileDir = await mkdtemp(join(tmpdir(), 'ecocorridor-film-chrome-'));
const frameDir = await mkdtemp(join(tmpdir(), 'ecocorridor-film-frames-'));
const chrome = spawn(CHROME, [
  '--headless=new',
  '--hide-scrollbars',
  '--mute-audio',
  '--allow-file-access-from-files',
  '--disable-background-networking',
  '--disable-component-update',
  '--disable-default-apps',
  '--disable-features=Translate,PaintHolding',
  '--disable-sync',
  '--no-first-run',
  '--no-default-browser-check',
  '--remote-debugging-port=0',
  `--user-data-dir=${profileDir}`,
  '--window-size=1280,720',
  pathToFileURL(htmlPath).href
], { stdio: ['ignore', 'ignore', 'pipe'] });

let chromeErrors = '';
chrome.stderr.on('data', chunk => { chromeErrors += chunk.toString(); });
const sleep = milliseconds => new Promise(resolvePromise => setTimeout(resolvePromise, milliseconds));

async function waitForPort() {
  const marker = join(profileDir, 'DevToolsActivePort');
  for (let attempt = 0; attempt < 240; attempt += 1) {
    try {
      const [port] = (await readFile(marker, 'utf8')).trim().split('\n');
      return Number(port);
    } catch {
      if (chrome.exitCode !== null) throw new Error(`Chrome exited early. ${chromeErrors}`);
      await sleep(50);
    }
  }
  throw new Error(`Timed out waiting for Chrome. ${chromeErrors}`);
}

class DevTools {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.ws = new WebSocket(url);
  }
  async open() {
    await new Promise((resolvePromise, reject) => {
      this.ws.addEventListener('open', resolvePromise, { once: true });
      this.ws.addEventListener('error', reject, { once: true });
    });
    this.ws.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
  }
  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { resolve: resolvePromise, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { this.ws.close(); }
}

function run(command, args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'ignore', 'pipe'] });
    let errors = '';
    child.stderr.on('data', chunk => { errors += chunk.toString(); });
    child.on('error', reject);
    child.on('close', code => {
      if (code === 0) resolvePromise();
      else reject(new Error(`${basename(command)} exited ${code}: ${errors.slice(-5000)}`));
    });
  });
}

function validateSpec(spec) {
  if (!spec || typeof spec !== 'object') throw new Error('window.__filmSpec is missing.');
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(spec.slug)) throw new Error('Film slug is not filesystem-safe.');
  for (const key of ['width', 'height', 'duration', 'fps']) {
    if (!Number.isFinite(spec[key]) || spec[key] <= 0) throw new Error(`Invalid film spec value: ${key}`);
  }
  for (const key of ['posterTime', 'reducedMotionTime']) {
    if (!Number.isFinite(spec[key]) || spec[key] < 0) throw new Error(`Invalid film spec value: ${key}`);
  }
  if (Math.abs(spec.duration * spec.fps - Math.round(spec.duration * spec.fps)) > 1e-9) {
    throw new Error('duration × fps must be an integer frame count.');
  }
  if (spec.posterTime >= spec.duration || spec.reducedMotionTime >= spec.duration) {
    throw new Error('Poster and reduced-motion times must be earlier than duration.');
  }
  if (spec.silent !== true) throw new Error('Film must declare silent: true.');
  if (!Array.isArray(spec.checkpoints) || spec.checkpoints.length === 0) throw new Error('At least one checkpoint is required.');
  const names = new Set([spec.slug, `${spec.slug}-poster`, 'contact-sheet']);
  for (const checkpoint of spec.checkpoints) {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(checkpoint.name)) throw new Error(`Invalid checkpoint name: ${checkpoint.name}`);
    if (names.has(checkpoint.name)) throw new Error(`Duplicate or reserved checkpoint name: ${checkpoint.name}`);
    if (!Number.isFinite(checkpoint.time) || checkpoint.time < 0 || checkpoint.time >= spec.duration) {
      throw new Error(`Invalid checkpoint time: ${checkpoint.name}`);
    }
    names.add(checkpoint.name);
  }
}

async function capture(devtools, spec, time, path, format = 'png') {
  await devtools.send('Runtime.evaluate', {
    expression: `window.setAnimationTime(${JSON.stringify(time)})`,
    awaitPromise: true,
    returnByValue: true
  });
  const result = await devtools.send('Page.captureScreenshot', {
    format,
    quality: format === 'jpeg' ? 92 : undefined,
    fromSurface: true,
    captureBeyondViewport: false,
    clip: { x: 0, y: 0, width: spec.width, height: spec.height, scale: 1 }
  });
  await writeFile(path, Buffer.from(result.data, 'base64'));
}

async function makeContactSheet(spec) {
  const count = spec.checkpoints.length;
  const columns = Math.min(4, count);
  const thumbWidth = 320;
  const thumbHeight = Math.round(thumbWidth * spec.height / spec.width);
  const inputs = spec.checkpoints.flatMap(checkpoint => ['-i', join(outputDir, `${checkpoint.name}.png`)]);
  if (count === 1) {
    await run(FFMPEG, ['-y', ...inputs, '-vf', `scale=${thumbWidth}:${thumbHeight}:flags=lanczos`, '-frames:v', '1', join(outputDir, 'contact-sheet.png')]);
    return;
  }
  const scales = spec.checkpoints.map((_, index) => `[${index}:v]scale=${thumbWidth}:${thumbHeight}:flags=lanczos[v${index}]`).join(';');
  const streams = spec.checkpoints.map((_, index) => `[v${index}]`).join('');
  const layout = spec.checkpoints.map((_, index) => `${(index % columns) * thumbWidth}_${Math.floor(index / columns) * thumbHeight}`).join('|');
  const filter = `${scales};${streams}xstack=inputs=${count}:layout=${layout}:fill=#F4F1E8[out]`;
  await run(FFMPEG, ['-y', ...inputs, '-filter_complex', filter, '-map', '[out]', '-frames:v', '1', join(outputDir, 'contact-sheet.png')]);
}

let devtools;
try {
  const port = await waitForPort();
  let pages = [];
  for (let attempt = 0; attempt < 100 && pages.length === 0; attempt += 1) {
    try { pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); } catch { /* Chrome may still be starting. */ }
    if (!pages.length) await sleep(50);
  }
  const page = pages.find(candidate => candidate.type === 'page');
  if (!page) throw new Error('Chrome did not expose a page target.');
  devtools = new DevTools(page.webSocketDebuggerUrl);
  await devtools.open();
  await devtools.send('Page.enable');
  await devtools.send('Runtime.enable');
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const ready = await devtools.send('Runtime.evaluate', { expression: 'Boolean(window.__animationReady)', returnByValue: true });
    if (ready.result?.value) break;
    if (attempt === 199) throw new Error('Animation did not report ready.');
    await sleep(50);
  }
  const specResult = await devtools.send('Runtime.evaluate', { expression: 'window.__filmSpec', returnByValue: true });
  const spec = specResult.result?.value;
  validateSpec(spec);
  await devtools.send('Emulation.setDeviceMetricsOverride', {
    width: spec.width, height: spec.height, deviceScaleFactor: 1, mobile: false
  });

  // Checkpoints are rendered first so a failed still/contact-sheet phase never
  // spends time creating a video that cannot satisfy the complete contract.
  for (const checkpoint of spec.checkpoints) {
    await capture(devtools, spec, checkpoint.time, join(outputDir, `${checkpoint.name}.png`));
    process.stdout.write(`captured ${checkpoint.name} @ ${checkpoint.time.toFixed(2)}s\n`);
  }
  await capture(devtools, spec, spec.posterTime, join(outputDir, `${spec.slug}-poster.png`));
  await makeContactSheet(spec);
  process.stdout.write('created contact-sheet.png\n');

  if (stillsOnly) {
    process.stdout.write('stills-only mode: video frames delegated to the contracted auditor\n');
  } else {
    const totalFrames = Math.round(spec.duration * spec.fps);
    for (let frame = 0; frame < totalFrames; frame += 1) {
      const target = join(frameDir, `frame-${String(frame).padStart(6, '0')}.jpg`);
      await capture(devtools, spec, frame / spec.fps, target, 'jpeg');
      if ((frame + 1) % Math.max(spec.fps * 5, 1) === 0 || frame + 1 === totalFrames) {
        process.stdout.write(`rendered ${frame + 1}/${totalFrames} frames\n`);
      }
    }
    await run(FFMPEG, [
      '-y', '-framerate', String(spec.fps), '-i', join(frameDir, 'frame-%06d.jpg'),
      '-vf', 'scale=in_range=pc:out_range=tv,format=yuv420p',
      '-c:v', 'libx264', '-profile:v', 'high', '-preset', 'medium', '-crf', '18',
      '-pix_fmt', 'yuv420p', '-color_range', 'tv', '-an', '-movflags', '+faststart',
      join(outputDir, `${spec.slug}.mp4`)
    ]);
    process.stdout.write(`encoded ${spec.slug}.mp4\n`);
  }
} finally {
  devtools?.close();
  if (chrome.exitCode === null) {
    const closed = new Promise(resolvePromise => chrome.once('close', resolvePromise));
    chrome.kill('SIGTERM');
    await Promise.race([closed, sleep(2000)]);
  }
  await rm(profileDir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
  await rm(frameDir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
}
