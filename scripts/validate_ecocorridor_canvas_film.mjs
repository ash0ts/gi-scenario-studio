import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { access, mkdtemp, readFile, rm } from 'node:fs/promises';
import { constants } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const htmlPath = resolve(process.argv[2] ?? 'film.html');
const outputDir = resolve(process.argv[3] ?? dirname(htmlPath));

function onPath(name) {
  const result = spawnSync('/usr/bin/which', [name], { encoding: 'utf8' });
  return result.status === 0 ? result.stdout.trim() : null;
}
async function executable(path) {
  if (!path) return false;
  try { await access(path, constants.X_OK); return true; } catch { return false; }
}
async function resolveBinary(envName, candidates, commands) {
  for (const candidate of [process.env[envName], ...candidates, ...commands.map(onPath)]) {
    if (await executable(candidate)) return candidate;
  }
  throw new Error(`Unable to find ${envName.replace('_BIN', '')}. Set ${envName} to an executable path.`);
}
const CHROME = await resolveBinary('CHROME_BIN', [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium'
], ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']);
const FFMPEG = await resolveBinary('FFMPEG_BIN', ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'], ['ffmpeg']);
const FFPROBE = await resolveBinary('FFPROBE_BIN', ['/opt/homebrew/bin/ffprobe', '/usr/local/bin/ffprobe'], ['ffprobe']);

function run(command, args, capture = false) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', capture ? 'pipe' : 'ignore', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', chunk => { stdout += chunk.toString(); });
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.on('error', reject);
    child.on('close', code => {
      if (code === 0) resolvePromise(stdout);
      else reject(new Error(`${basename(command)} exited ${code}: ${stderr.slice(-5000)}`));
    });
  });
}

const source = await readFile(htmlPath, 'utf8');
if (/(?:src|href)\s*=\s*["']https?:\/\//i.test(source) || /url\(\s*["']?https?:\/\//i.test(source)) {
  throw new Error('Film contains a network asset. Package all scripts, styles, fonts, and images locally.');
}

const profileDir = await mkdtemp(join(tmpdir(), 'ecocorridor-film-validate-'));
const chrome = spawn(CHROME, [
  '--headless=new', '--hide-scrollbars', '--mute-audio', '--allow-file-access-from-files',
  '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
  '--disable-features=Translate,PaintHolding', '--disable-sync', '--no-first-run',
  '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profileDir}`,
  '--window-size=1280,720', pathToFileURL(htmlPath).href
], { stdio: ['ignore', 'ignore', 'pipe'] });
const sleep = milliseconds => new Promise(resolvePromise => setTimeout(resolvePromise, milliseconds));
let chromeErrors = '';
chrome.stderr.on('data', chunk => { chromeErrors += chunk.toString(); });

async function waitForPort() {
  const marker = join(profileDir, 'DevToolsActivePort');
  for (let attempt = 0; attempt < 240; attempt += 1) {
    try { return Number((await readFile(marker, 'utf8')).trim().split('\n')[0]); }
    catch {
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
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
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
  const value = async expression => {
    const result = await devtools.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(`Film evaluation failed: ${JSON.stringify(result.exceptionDetails)}`);
    return result.result?.value;
  };
  for (let attempt = 0; attempt < 200 && !(await value('Boolean(window.__animationReady)')); attempt += 1) await sleep(50);
  if (!(await value('Boolean(window.__animationReady)'))) throw new Error('Animation did not report ready.');
  const spec = await value('window.__filmSpec');
  if (!spec || spec.silent !== true) throw new Error('Film spec must exist and declare silent: true.');
  if (spec.width !== 1280 || spec.height !== 720 || spec.fps !== 15) throw new Error('Film spec differs from the pinned output profile.');
  await devtools.send('Emulation.setDeviceMetricsOverride', { width: spec.width, height: spec.height, deviceScaleFactor: 1, mobile: false });

  async function screenshotHash(time) {
    await value(`window.setAnimationTime(${JSON.stringify(time)})`);
    const result = await devtools.send('Page.captureScreenshot', {
      format: 'png', fromSurface: true, captureBeyondViewport: false,
      clip: { x: 0, y: 0, width: spec.width, height: spec.height, scale: 1 }
    });
    return createHash('sha256').update(Buffer.from(result.data, 'base64')).digest('hex');
  }
  const firstHash = await screenshotHash(spec.posterTime);
  const secondHash = await screenshotHash(spec.posterTime);
  if (firstHash !== secondHash) throw new Error('Repeated deterministic captures do not match.');
  await value('window.releaseAnimation(); true');

  const initial = await value("({ready:Boolean(window.__animationReady),max:Number(scrub.max),canvas:[film.width,film.height],fonts:document.fonts.status})");
  if (!initial.ready || initial.max !== spec.duration || initial.canvas[0] !== spec.width || initial.canvas[1] !== spec.height || initial.fonts !== 'loaded') {
    throw new Error(`Initial runtime state is inconsistent: ${JSON.stringify(initial)}`);
  }
  await value('toggle.focus(); toggle.click(); true');
  if ((await value("toggle.getAttribute('aria-label')")) !== 'Play animation') throw new Error('Pause control did not expose Play animation.');
  await devtools.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
  await devtools.send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
  await sleep(50);
  if ((await value("toggle.getAttribute('aria-label')")) !== 'Pause animation') throw new Error('Space did not activate the playback button.');

  await value(`toggle.click(); scrub.focus(); scrub.value=${JSON.stringify(String(spec.duration / 2))}; scrub.dispatchEvent(new Event('input',{bubbles:true})); true`);
  const beforeArrow = await value('Number(scrub.value)');
  await devtools.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 });
  await devtools.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 });
  if ((await value('Number(scrub.value)')) <= beforeArrow) throw new Error('Range keyboard scrubbing did not advance.');

  await value(`scrub.value=${JSON.stringify(String(spec.duration - 0.05))}; scrub.dispatchEvent(new Event('input',{bubbles:true})); toggle.click(); true`);
  await sleep(180);
  if ((await value('Number(scrub.value)')) >= 1) throw new Error('Film did not loop near its duration boundary.');

  const responsiveWidth = Math.min(640, spec.width);
  await devtools.send('Emulation.setDeviceMetricsOverride', {
    width: responsiveWidth,
    height: Math.ceil(responsiveWidth * spec.height / spec.width) + 60,
    deviceScaleFactor: 1,
    mobile: false
  });
  await sleep(50);
  const responsive = await value("(() => {const r=document.querySelector('main').getBoundingClientRect();return [r.width,r.height];})()");
  if (Math.abs(responsive[0] / responsive[1] - spec.width / spec.height) > 0.01) throw new Error('Responsive aspect ratio changed.');

  await devtools.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  await devtools.send('Page.reload', { ignoreCache: true });
  for (let attempt = 0; attempt < 200 && !(await value('Boolean(window.__animationReady)')); attempt += 1) await sleep(50);
  if (!(await value('Boolean(window.__animationReady)'))) throw new Error('Reduced-motion reload did not report ready.');
  const reduced = await value("({time:Number(scrub.value),text:toggle.textContent,label:toggle.getAttribute('aria-label')})");
  if (Math.abs(reduced.time - spec.reducedMotionTime) > 0.01 || reduced.text !== 'Play' || reduced.label !== 'Play animation') {
    throw new Error(`Reduced-motion state is inconsistent: ${JSON.stringify(reduced)}`);
  }

  const required = [
    htmlPath,
    join(outputDir, `${spec.slug}.mp4`),
    join(outputDir, `${spec.slug}-poster.png`),
    join(outputDir, 'contact-sheet.png'),
    ...spec.checkpoints.map(checkpoint => join(outputDir, `${checkpoint.name}.png`))
  ];
  for (const path of required) await access(path);

  const videoPath = join(outputDir, `${spec.slug}.mp4`);
  const probe = JSON.parse(await run(FFPROBE, ['-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', videoPath], true));
  const video = probe.streams.find(stream => stream.codec_type === 'video');
  const audio = probe.streams.find(stream => stream.codec_type === 'audio');
  if (!video) throw new Error('Encoded artifact does not contain a video stream.');
  const expectedFrames = Math.round(spec.duration * spec.fps);
  const actualDuration = Number(probe.format.duration);
  const [fpsNumerator, fpsDenominator] = video.r_frame_rate.split('/').map(Number);
  const actualFps = fpsNumerator / fpsDenominator;
  if (audio || video.codec_name !== 'h264' || video.profile !== 'High' || video.pix_fmt !== 'yuv420p' || video.color_range !== 'tv') {
    throw new Error(`Video codec contract failed: ${JSON.stringify({ video, audio })}`);
  }
  if (video.width !== spec.width || video.height !== spec.height || Math.abs(actualFps - spec.fps) > 1e-9 || Number(video.nb_read_frames) !== expectedFrames) {
    throw new Error('Video dimensions, frame rate, or frame count do not match the film spec.');
  }
  if (Math.abs(actualDuration - spec.duration) > 1 / spec.fps) throw new Error('Video duration does not match the film spec.');
  await run(FFMPEG, ['-v', 'error', '-i', videoPath, '-f', 'null', '-']);

  process.stdout.write(`${JSON.stringify({
    ok: true,
    spec,
    deterministicSha256: firstHash,
    responsive: responsive.map(item => Math.round(item)),
    reducedMotion: reduced,
    video: {
      codec: video.codec_name,
      profile: video.profile,
      width: video.width,
      height: video.height,
      fps: actualFps,
      frames: Number(video.nb_read_frames),
      duration: actualDuration,
      pixelFormat: video.pix_fmt,
      colorRange: video.color_range,
      audio: false
    },
    artifacts: required
  }, null, 2)}\n`);
} finally {
  devtools?.close();
  if (chrome.exitCode === null) {
    const closed = new Promise(resolvePromise => chrome.once('close', resolvePromise));
    chrome.kill('SIGTERM');
    await Promise.race([closed, sleep(2000)]);
  }
  await rm(profileDir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
}
