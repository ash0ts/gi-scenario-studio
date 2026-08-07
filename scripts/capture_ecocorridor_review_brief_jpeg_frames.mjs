#!/usr/bin/env node

// Capture the exact JPEG sequence later supplied to FFmpeg by the Review Brief
// renderer. Each invocation starts a new Chrome process and reports an ordered,
// byte-level manifest for the files it wrote.

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const htmlPath = resolve(process.argv[2] ?? '');
const chromePath = resolve(process.argv[3] ?? '');
const outputDirectory = resolve(process.argv[4] ?? '');
const JPEG_QUALITY = 92;

if ((await readdir(outputDirectory)).length !== 0) {
  throw new Error('Encoding-input directory must be empty.');
}

const profileDirectory = await mkdtemp(join(tmpdir(), 'ecocorridor-jpeg-frame-capture-'));
const chrome = spawn(chromePath, [
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
  `--user-data-dir=${profileDirectory}`,
  '--window-size=1280,720',
  pathToFileURL(htmlPath).href
], { stdio: ['ignore', 'ignore', 'pipe'] });

const sleep = milliseconds => new Promise(resolvePromise => setTimeout(resolvePromise, milliseconds));
let chromeErrors = '';
chrome.stderr.on('data', chunk => { chromeErrors += chunk.toString(); });

async function waitForPort() {
  const marker = join(profileDirectory, 'DevToolsActivePort');
  for (let attempt = 0; attempt < 240; attempt += 1) {
    try {
      return Number((await readFile(marker, 'utf8')).trim().split('\n')[0]);
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
    this.webSocket = new WebSocket(url);
  }

  async open() {
    await new Promise((resolvePromise, reject) => {
      this.webSocket.addEventListener('open', resolvePromise, { once: true });
      this.webSocket.addEventListener('error', reject, { once: true });
    });
    this.webSocket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { resolve: resolvePromise, reject });
      this.webSocket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.webSocket.close();
  }
}

let devtools;
try {
  const port = await waitForPort();
  let pages = [];
  for (let attempt = 0; attempt < 100 && pages.length === 0; attempt += 1) {
    try {
      pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    } catch {
      // Chrome may still be starting.
    }
    if (!pages.length) await sleep(50);
  }
  const page = pages.find(candidate => candidate.type === 'page');
  if (!page) throw new Error('Chrome did not expose a page target.');
  devtools = new DevTools(page.webSocketDebuggerUrl);
  await devtools.open();
  await devtools.send('Page.enable');
  await devtools.send('Runtime.enable');
  const value = async expression => {
    const result = await devtools.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true
    });
    if (result.exceptionDetails) throw new Error('Encoding-input evaluation failed.');
    return result.result?.value;
  };
  for (let attempt = 0; attempt < 200 && !(await value('Boolean(window.__animationReady)')); attempt += 1) {
    await sleep(50);
  }
  if (!(await value('Boolean(window.__animationReady)'))) {
    throw new Error('Animation did not report ready.');
  }
  const spec = await value('window.__filmSpec');
  await devtools.send('Emulation.setDeviceMetricsOverride', {
    width: spec.width,
    height: spec.height,
    deviceScaleFactor: 1,
    mobile: false
  });
  const frameCount = Math.round(spec.duration * spec.fps);
  const frames = [];
  for (let frameIndex = 0; frameIndex < frameCount; frameIndex += 1) {
    const timeSeconds = frameIndex / spec.fps;
    await value(`window.setAnimationTime(${JSON.stringify(timeSeconds)})`);
    const shot = await devtools.send('Page.captureScreenshot', {
      format: 'jpeg',
      quality: JPEG_QUALITY,
      fromSurface: true,
      captureBeyondViewport: false,
      clip: { x: 0, y: 0, width: spec.width, height: spec.height, scale: 1 }
    });
    const bytes = Buffer.from(shot.data, 'base64');
    const logicalName = `frame-${String(frameIndex).padStart(6, '0')}.jpg`;
    const outputPath = join(outputDirectory, logicalName);
    await writeFile(outputPath, bytes, { flag: 'wx' });
    const metadata = await stat(outputPath);
    frames.push({
      frame_index: frameIndex,
      time_seconds: Number(timeSeconds.toFixed(9)),
      logical_name: basename(outputPath),
      bytes: metadata.size,
      jpeg_sha256: createHash('sha256').update(bytes).digest('hex')
    });
  }
  process.stdout.write(JSON.stringify({
    format: 'jpeg',
    jpeg_quality: JPEG_QUALITY,
    frames
  }));
} finally {
  devtools?.close();
  if (chrome.exitCode === null) {
    const closed = new Promise(resolvePromise => chrome.once('close', resolvePromise));
    chrome.kill('SIGTERM');
    await Promise.race([closed, sleep(2000)]);
  }
  await rm(profileDirectory, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
}
