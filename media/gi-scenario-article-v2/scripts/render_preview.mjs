#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const articleRoot = path.resolve(here, "..");
const figuresDir = path.join(articleRoot, "figures");
const distDir = path.join(articleRoot, "dist");
const documents = ["article", "methods"];
const fontSpecs = ["20px Atkinson", "700 20px Atkinson", "600 30px PlexCondensed", "20px PlexMono"];
const issues = [];
const audits = { documents: {}, figures: {} };

function fileHash(filename) {
  return createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

function writeJson(filename, value) {
  fs.writeFileSync(path.join(distDir, filename), JSON.stringify(value, null, 2) + "\n");
}

function activeFigureFiles() {
  const referenced = new Set();
  for (const document of documents) {
    const source = fs.readFileSync(path.join(articleRoot, `${document}.md`), "utf8");
    for (const match of source.matchAll(/(?:\b(?:src|srcset)=["']|\]\()(?:\.\/)?figures\/([a-z0-9-]+\.(?:svg|png))/g)) {
      referenced.add(match[1].replace(/\.png$/, ".svg"));
    }
  }
  const manifestPath = path.join(distDir, "evidence-manifest.json");
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const active = manifest.active_figures;
  if (Array.isArray(active)) {
    const stems = new Set(active.map((item) => (typeof item === "string" ? item : item.id).replace(/\.svg$/, "").replace(/-mobile$/, "")));
    for (const filename of referenced) {
      if (!stems.has(filename.replace(/\.svg$/, "").replace(/-mobile$/, ""))) {
        throw new Error(`Referenced figure is not active in the evidence manifest: ${filename}`);
      }
    }
  }
  return [...referenced].sort();
}

async function loadFonts(page, label) {
  const fonts = await page.evaluate(async (specs) => {
    await document.fonts.ready;
    return Promise.all(specs.map(async (spec) => {
      try {
        const faces = await document.fonts.load(spec);
        return { spec, loaded: faces.length > 0 && document.fonts.check(spec) };
      } catch (error) {
        return { spec, loaded: false, error: String(error) };
      }
    }));
  }, fontSpecs);
  if (fonts.some((font) => !font.loaded)) issues.push(`${label}: local font did not load`);
  return fonts;
}

function checkLocalLinks(links, label) {
  return links.flatMap((link) => {
    const url = new URL(link.href);
    if (url.protocol !== "file:") return [];
    const filename = fileURLToPath(url);
    if (!fs.existsSync(filename)) return [{ ...link, error: "local target is missing" }];
    if (url.hash && /\.html$/.test(filename)) {
      const target = fs.readFileSync(filename, "utf8");
      const id = decodeURIComponent(url.hash.slice(1));
      if (!target.includes(`id="${id}"`) && !target.includes(`id='${id}'`)) {
        return [{ ...link, error: "target heading or anchor is missing" }];
      }
    }
    return [];
  }).map((finding) => {
    issues.push(`${label}: ${finding.error} (${finding.text})`);
    return finding;
  });
}

async function screenshotPage(browser, viewport, documentName, size) {
  const label = `${documentName}-${size}`;
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, reducedMotion: "reduce" });
  const failedRequests = [];
  page.on("requestfailed", (request) => failedRequests.push({ type: request.resourceType(), url: request.url(), error: request.failure()?.errorText }));
  await page.goto(pathToFileURL(path.join(distDir, `${documentName}.html`)).href, { waitUntil: "load" });
  const fonts = await loadFonts(page, label);
  const images = await page.evaluate(async () => Promise.all([...document.images].map(async (img) => {
    try { await img.decode(); } catch { /* The image state below records the failure. */ }
    return { src: img.currentSrc, alt: img.alt, loaded: img.complete && img.naturalWidth > 0 };
  })));
  if (images.some((img) => !img.loaded)) issues.push(`${label}: an image did not load`);
  if (failedRequests.length) issues.push(`${label}: ${failedRequests.length} failed asset requests`);

  await page.screenshot({ path: path.join(distDir, `${label}.png`), fullPage: true });
  await page.screenshot({ path: path.join(distDir, `${label}-top.png`), fullPage: false });
  for (const kind of ["figure", "table"]) {
    const elements = page.locator(`article ${kind}`);
    for (let index = 0; index < await elements.count(); index += 1) {
      const element = elements.nth(index);
      const id = await element.getAttribute("id") || `${index + 1}`;
      await element.screenshot({ path: path.join(distDir, `${label}-${kind}-${id}.png`) });
    }
  }

  const layout = await page.evaluate(() => {
    const describe = (element) => ({ tag: element.tagName, id: element.id, text: (element.textContent || "").trim().slice(0, 100), left: element.getBoundingClientRect().left, right: element.getBoundingClientRect().right });
    const contentElements = [...document.querySelectorAll("p,h1,h2,h3,li,th,td,figcaption")];
    return {
      title: document.title,
      heading: document.querySelector("h1")?.textContent,
      sourceDocument: document.querySelector('meta[name="source-document"]')?.content,
      sourceSha256: document.querySelector('meta[name="source-sha256"]')?.content,
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      overflowing: [...document.querySelectorAll("body *")].filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && (rect.right > window.innerWidth + 0.5 || rect.left < -0.5);
      }).map(describe),
      textOverflow: contentElements.filter((element) => element.scrollWidth > element.clientWidth + 1).map(describe),
      tables: [...document.querySelectorAll("article table")].map((table) => ({ id: table.id, caption: table.caption?.textContent?.trim() || null, rows: table.rows.length, headers: [...table.querySelectorAll("th")].map((th) => ({ text: th.textContent.trim(), scope: th.scope })) })),
      links: [...document.querySelectorAll("a[href]")].map((a) => ({ href: a.href, text: a.textContent.trim() })),
    };
  });
  if (layout.title !== layout.heading) issues.push(`${label}: document title differs from H1`);
  if (layout.sourceSha256 !== fileHash(path.join(articleRoot, `${documentName}.md`))) issues.push(`${label}: rendered HTML is stale against its Markdown source`);
  if (layout.documentWidth > viewport.width || layout.overflowing.length || layout.textOverflow.length) issues.push(`${label}: content overflows its layout`);
  const brokenLinks = checkLocalLinks(layout.links, label);
  const result = { ...layout, htmlSha256: fileHash(path.join(distDir, `${documentName}.html`)), fonts, images, failedRequests, brokenLinks };
  audits.documents[label] = result;
  writeJson(`${label}-layout-audit.json`, result);
  // Keep the established short audit path for the article verifier.
  if (label === "article-mobile") writeJson("mobile-layout-audit.json", result);
  await page.close();
}

async function rasterizeSvg(browser, filename) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1600 }, deviceScaleFactor: 1 });
  await page.goto(pathToFileURL(path.join(figuresDir, filename)).href, { waitUntil: "load" });
  const fonts = await loadFonts(page, filename);
  const geometry = await page.evaluate(() => {
    const svg = document.querySelector("svg");
    const frame = svg.viewBox.baseVal;
    const texts = [...svg.querySelectorAll("text")].map((text) => {
      const box = text.getBBox();
      return { text: text.textContent, x: box.x, y: box.y, width: box.width, height: box.height, fontSize: getComputedStyle(text).fontSize };
    });
    return {
      width: frame.width,
      height: frame.height,
      textCount: texts.length,
      offCanvasText: texts.filter((box) => box.x < frame.x || box.y < frame.y || box.x + box.width > frame.x + frame.width || box.y + box.height > frame.y + frame.height),
    };
  });
  if (geometry.offCanvasText.length) issues.push(`${filename}: text extends outside its SVG viewBox`);
  await page.locator("svg").screenshot({ path: path.join(figuresDir, filename.replace(/\.svg$/, ".png")) });
  audits.figures[filename] = {
    ...geometry,
    fonts,
    svgSha256: fileHash(path.join(figuresDir, filename)),
    rasterSha256: fileHash(path.join(figuresDir, filename.replace(/\.svg$/, ".png"))),
  };
  await page.close();
}

fs.mkdirSync(distDir, { recursive: true });
// Use an installed Chrome when the optional Playwright browser download is absent.
const browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromium.executablePath()) ? {} : { channel: "chrome" }) });
try {
  for (const filename of activeFigureFiles()) await rasterizeSvg(browser, filename);
  for (const documentName of documents) {
    await screenshotPage(browser, { width: 1440, height: 1000 }, documentName, "desktop");
    await screenshotPage(browser, { width: 390, height: 844 }, documentName, "mobile");
  }
} finally {
  await browser.close();
}
writeJson("render-audit.json", { ...audits, issues });
if (issues.length) throw new Error(`Preview inspection failed:\n${issues.join("\n")}`);
console.log(`Rendered ${Object.keys(audits.figures).length} active figure assets and four article/methods previews; all layout and asset checks passed.`);
