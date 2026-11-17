(function () {
  "use strict";

  const DATA = window.GI_ANIMATION_DATA;
  if (!DATA || DATA.schema_version !== "1.0") {
    throw new Error("The hash-verified GI animation data projection is missing.");
  }

  const slug = document.body.dataset.film;
  const filmRecord = DATA.films.find((item) => item.slug === slug);
  if (!filmRecord) throw new Error(`Unknown GI report film: ${slug}`);

  const FILM = Object.freeze({
    slug: filmRecord.slug,
    title: filmRecord.title,
    width: DATA.width,
    height: DATA.height,
    duration: filmRecord.duration,
    fps: DATA.fps,
    posterTime: filmRecord.poster_time,
    reducedMotionTime: filmRecord.reduced_motion_time,
    silent: true,
    checkpoints: Object.freeze(filmRecord.checkpoints.map((item) => Object.freeze(item))),
  });
  window.__filmSpec = FILM;

  const canvas = document.querySelector("#film");
  const ctx = canvas.getContext("2d", { alpha: false, willReadFrequently: true });
  const toggle = document.querySelector("#toggle");
  const scrub = document.querySelector("#scrub");
  const clock = document.querySelector("#clock");

  const C = Object.freeze({
    paper: "#F5F7F6",
    paper2: "#EDF2F0",
    ink: "#17313A",
    muted: "#5D7177",
    faint: "#D7E1DE",
    line: "#B8C8C4",
    teal: "#08766F",
    tealSoft: "#D7ECE8",
    blue: "#2F6F9F",
    blueSoft: "#DCE9F2",
    ochre: "#A96315",
    ochreSoft: "#F3E4D1",
    red: "#B43D47",
    redSoft: "#F3DDE0",
    gray: "#839397",
    graySoft: "#E5EBE9",
    white: "#FFFFFF",
  });

  let playing = true;
  let manualTime = 0;
  let anchorTime = performance.now();
  let anchorFilm = 0;
  let renderMode = false;

  const clamp = (value, minimum = 0, maximum = 1) =>
    Math.max(minimum, Math.min(maximum, value));
  const ease = (value) => {
    const t = clamp(value);
    return t * t * (3 - 2 * t);
  };
  const out = (value) => 1 - Math.pow(1 - clamp(value), 3);
  const appear = (time, start, duration = 0.8) => ease((time - start) / duration);
  const phase = (time, start, end, fade = 0.65) =>
    clamp((time - start) / fade) * clamp((end - time) / fade);
  const mix = (a, b, amount) => a + (b - a) * clamp(amount);
  const formatTime = (seconds) => {
    const value = Math.max(0, Math.floor(seconds));
    return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  };

  function withAlpha(alpha, draw) {
    if (alpha <= 0) return;
    ctx.save();
    ctx.globalAlpha *= clamp(alpha);
    draw();
    ctx.restore();
  }

  function roundRect(x, y, width, height, radius, fill, stroke = null, lineWidth = 2) {
    const r = Math.min(radius, width / 2, height / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + width, y, x + width, y + height, r);
    ctx.arcTo(x + width, y + height, x, y + height, r);
    ctx.arcTo(x, y + height, x, y, r);
    ctx.arcTo(x, y, x + width, y, r);
    ctx.closePath();
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fill();
    }
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = lineWidth;
      ctx.stroke();
    }
  }

  function line(x1, y1, x2, y2, color = C.line, width = 2, dash = []) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.stroke();
    ctx.restore();
  }

  function arrow(x1, y1, x2, y2, color = C.ink, width = 4, progress = 1) {
    const p = clamp(progress);
    const ex = mix(x1, x2, p);
    const ey = mix(y1, y2, p);
    line(x1, y1, ex, ey, color, width);
    if (p < 0.92) return;
    const angle = Math.atan2(y2 - y1, x2 - x1);
    ctx.beginPath();
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - 18 * Math.cos(angle - Math.PI / 6), ey - 18 * Math.sin(angle - Math.PI / 6));
    ctx.lineTo(ex - 18 * Math.cos(angle + Math.PI / 6), ey - 18 * Math.sin(angle + Math.PI / 6));
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  function face(kind) {
    if (kind === "heading") return '"IBM Plex Sans Condensed"';
    if (kind === "mono") return '"IBM Plex Mono"';
    return '"Atkinson Hyperlegible"';
  }

  function text(value, x, y, size = 42, color = C.ink, options = {}) {
    const {
      family = "body",
      weight = 400,
      align = "left",
      baseline = "alphabetic",
      maxWidth,
    } = options;
    ctx.fillStyle = color;
    ctx.font = `${weight} ${size}px ${face(family)}`;
    ctx.textAlign = align;
    ctx.textBaseline = baseline;
    if (maxWidth) ctx.fillText(String(value), x, y, maxWidth);
    else ctx.fillText(String(value), x, y);
  }

  function wrap(value, x, y, maxWidth, size = 42, color = C.ink, options = {}) {
    const words = String(value).split(/\s+/);
    const lines = [];
    let current = "";
    ctx.font = `${options.weight ?? 400} ${size}px ${face(options.family ?? "body")}`;
    for (const word of words) {
      const candidate = current ? `${current} ${word}` : word;
      if (current && ctx.measureText(candidate).width > maxWidth) {
        lines.push(current);
        current = word;
      } else current = candidate;
    }
    if (current) lines.push(current);
    const lineHeight = options.lineHeight ?? Math.round(size * 1.18);
    lines.forEach((item, index) =>
      text(item, x, y + index * lineHeight, size, color, options),
    );
    return lines.length * lineHeight;
  }

  function pill(label, x, y, fill, color = C.ink, border = null, width = null) {
    ctx.font = `500 26px ${face("mono")}`;
    const measured = ctx.measureText(label).width;
    const boxWidth = width ?? measured + 34;
    roundRect(x, y, boxWidth, 44, 22, fill, border, 2);
    text(label, x + boxWidth / 2, y + 23, 26, color, {
      family: "mono",
      weight: 500,
      align: "center",
      baseline: "middle",
    });
    return boxWidth;
  }

  function card(x, y, width, height, fill = C.white, stroke = C.line, radius = 22) {
    roundRect(x, y, width, height, radius, fill, stroke, 2);
  }

  function packet(x, y, color = C.blue, scale = 1) {
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(scale, scale);
    ctx.beginPath();
    ctx.moveTo(0, -28);
    ctx.bezierCurveTo(8, -15, 26, 4, 26, 19);
    ctx.bezierCurveTo(26, 35, 14, 45, 0, 45);
    ctx.bezierCurveTo(-14, 45, -26, 35, -26, 19);
    ctx.bezierCurveTo(-26, 4, -8, -15, 0, -28);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = C.paper;
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.restore();
  }

  function checkIcon(x, y, passes, scale = 1) {
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(scale, scale);
    ctx.beginPath();
    ctx.arc(0, 0, 22, 0, Math.PI * 2);
    ctx.fillStyle = passes ? C.teal : C.red;
    ctx.fill();
    ctx.strokeStyle = C.white;
    ctx.lineWidth = 5;
    if (passes) {
      ctx.beginPath();
      ctx.moveTo(-10, 0);
      ctx.lineTo(-2, 9);
      ctx.lineTo(12, -9);
      ctx.stroke();
    } else {
      line(-8, -8, 8, 8, C.white, 5);
      line(8, -8, -8, 8, C.white, 5);
    }
    ctx.restore();
  }

  function background() {
    ctx.fillStyle = C.paper;
    ctx.fillRect(0, 0, FILM.width, FILM.height);
    ctx.strokeStyle = C.faint;
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.34;
    for (let x = 96; x < FILM.width; x += 96) line(x, 0, x, FILM.height, C.faint, 1);
    for (let y = 96; y < FILM.height; y += 96) line(0, y, FILM.width, y, C.faint, 1);
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.ink;
    ctx.fillRect(0, 0, FILM.width, 18);
  }

  function header(eyebrow, titleValue, alpha = 1) {
    withAlpha(alpha, () => {
      text(eyebrow.toUpperCase(), 96, 86, 26, C.teal, { family: "mono", weight: 500 });
      text(titleValue, 96, 164, 68, C.ink, { family: "heading", weight: 600 });
    });
  }

  function footer(time, short = false) {
    const y = 1018;
    line(96, 984, 1824, 984, C.line, 2);
    text(
      short ? "Cedar Trace synthetic evidence · not field evidence" : DATA.persistent_notice,
      96,
      y,
      25,
      C.muted,
      { family: "mono", weight: 400 },
    );
    const amount = clamp(time / FILM.duration);
    ctx.fillStyle = C.faint;
    ctx.fillRect(96, 1044, 1728, 6);
    ctx.fillStyle = C.teal;
    ctx.fillRect(96, 1044, 1728 * amount, 6);
  }

  function evidenceTransition(time, center, label, duration = 1.2) {
    const start = center - duration / 2;
    const progress = clamp((time - start) / duration);
    const alpha = Math.sin(Math.PI * progress);
    if (alpha <= 0) return;
    withAlpha(alpha, () => {
      const x = mix(96, 1824, progress);
      line(96, 540, x, 540, C.teal, 6);
      packet(x, 540, C.blue, 0.78);
      text(label, 960, 650, 34, C.ink, {
        family: "mono",
        weight: 500,
        align: "center",
      });
    });
  }

  function drawIncompletePlot(time) {
    const source = DATA.guided;
    const chartAlpha = phase(time, 0, 12.4, 0.8);
    header("220 direct alternatives · four selected storms each", FILM.title, chartAlpha);
    withAlpha(chartAlpha, () => {
      const left = 150;
      const top = 270;
      const width = 1170;
      const height = 570;
      line(left, top + height, left + width, top + height, C.ink, 3);
      line(left, top, left, top + height, C.ink, 3);
      text("lower synthetic cost →", left + width / 2, top + height + 66, 30, C.muted, {
        family: "mono",
        align: "center",
      });
      ctx.save();
      ctx.translate(88, top + height / 2);
      ctx.rotate(-Math.PI / 2);
      text("more modeled volume reduction →", 0, 0, 30, C.muted, {
        family: "mono",
        align: "center",
      });
      ctx.restore();
      const mapX = (value) => left + (value / source.plot_bounds.max_cost) * width;
      const mapY = (value) => top + height - (value / source.plot_bounds.max_reduction) * height;
      const pointsReveal = appear(time, 1.4, 1.4);
      source.points.forEach((point, index) => {
        const local = clamp(pointsReveal * source.points.length - index);
        if (local <= 0) return;
        ctx.beginPath();
        ctx.arc(mapX(point.cost), mapY(point.reduction), point.id === "ALT-017" ? 0 : 7, 0, Math.PI * 2);
        ctx.fillStyle = point.nondominated ? C.teal : C.gray;
        ctx.globalAlpha = point.nondominated ? 0.8 : 0.34;
        ctx.fill();
        ctx.globalAlpha = 1;
      });
      const target = source.alt017;
      const highlight = appear(time, 4.8, 0.8);
      const tx = mapX(target.cost);
      const ty = mapY(target.reduction);
      withAlpha(highlight, () => {
        ctx.beginPath();
        ctx.arc(tx, ty, 21, 0, Math.PI * 2);
        ctx.fillStyle = C.ochre;
        ctx.fill();
        ctx.strokeStyle = C.ink;
        ctx.lineWidth = 5;
        ctx.stroke();
        packet(tx, ty, C.blue, 0.55);
        card(1390, 310, 420, 250, C.ochreSoft, C.ochre);
        text("ALT-017", 1430, 370, 34, C.ink, { family: "mono", weight: 500 });
        text(`$${target.cost.toFixed(2)}k`, 1430, 446, 58, C.ink, {
          family: "heading",
          weight: 600,
        });
        text(`${target.reduction.toFixed(2)}% less volume`, 1430, 510, 36, C.ink, {
          weight: 700,
        });
      });
      withAlpha(appear(time, 8.5, 0.7), () => {
        text("Attractive on these two axes", 1390, 625, 36, C.ochre, { weight: 700 });
        wrap("But the engineering brief has four required rules.", 1390, 686, 420, 34, C.ink, {
          lineHeight: 42,
        });
      });
    });

    withAlpha(phase(time, 12.8, 23.8, 0.8), () => {
      header("The same alternative · every required rule", "A decision is an AND statement");
      const rules = source.alt017.rules;
      const labels = ["Volume target", "Cost cap", "Peak guardrail", "Direct-run QA"];
      const colors = [C.redSoft, C.tealSoft, C.redSoft, C.tealSoft];
      const border = [C.red, C.teal, C.red, C.teal];
      rules.forEach((rule, index) => {
        const a = appear(time, 13 + index * 0.8, 0.7);
        withAlpha(a, () => {
          const x = 96 + index * 432;
          const y = 280 + (1 - a) * 22;
          card(x, y, 390, 250, colors[index], border[index]);
          checkIcon(x + 52, y + 56, rule.passes, 0.9);
          text(labels[index], x + 92, y + 66, 32, C.ink, { weight: 700 });
          text(rule.display_actual, x + 28, y + 148, 50, border[index], {
            family: "heading",
            weight: 600,
          });
          text(rule.display_threshold, x + 28, y + 204, 27, C.muted, { family: "mono" });
        });
      });
      withAlpha(appear(time, 18.2, 0.8), () => {
        const storms = source.alt017.peak_increases;
        text("Two selected storms get worse", 96, 625, 44, C.red, {
          family: "heading",
          weight: 600,
        });
        storms.forEach((storm, index) => {
          const x = 96 + index * 700;
          card(x, 674, 650, 150, C.white, C.red);
          text(storm.short_label, x + 30, 724, 30, C.ink, { weight: 700 });
          text(`${storm.baseline_peak.toFixed(2)} → ${storm.alternative_peak.toFixed(2)} cfs`, x + 30, 787, 45, C.red, {
            family: "mono",
            weight: 500,
          });
        });
        pill("2 PASS · 2 FAIL", 1510, 690, C.redSoft, C.red, C.red, 300);
      });
    });

    withAlpha(phase(time, 24.2, 34.2, 0.8), () => {
      header("Decision lesson", "The plot was honest. It was incomplete.");
      const a = appear(time, 25.2, 0.9);
      withAlpha(a, () => {
        const y = 390;
        packet(360, y, C.ochre, 1.15);
        text("nondominated", 360, y + 88, 30, C.ochre, { family: "mono", align: "center" });
        arrow(465, y, 760, y, C.ink, 5, appear(time, 26, 1));
        card(820, 285, 820, 230, C.redSoft, C.red);
        text("Fails the declared brief", 1230, 360, 52, C.red, {
          family: "heading",
          weight: 600,
          align: "center",
        });
        text("misses volume target · raises two peaks", 1230, 430, 34, C.ink, {
          family: "mono",
          align: "center",
        });
        wrap(
          "Nondominated on two visible axes does not mean acceptable under every required rule.",
          960,
          670,
          1300,
          48,
          C.ink,
          { weight: 700, align: "center", lineHeight: 58 },
        );
      });
    });
    evidenceTransition(time, 12.6, "RESTORE THE MISSING RULES");
    evidenceTransition(time, 24.0, "KEEP EVERY REQUIRED DIMENSION");
    footer(time);
  }

  function ledgerBar(label, y, baseline, alternative, maximum, note = "") {
    const x = 540;
    const width = 1040;
    text(label, 96, y + 20, 34, C.ink, { weight: 700 });
    if (note) text(note, 96, y + 58, 25, C.ochre, { family: "mono" });
    const baseWidth = (baseline / maximum) * width;
    const altWidth = (alternative / maximum) * width;
    roundRect(x, y - 8, width, 42, 21, C.graySoft);
    roundRect(x, y - 8, baseWidth, 42, 21, C.blueSoft, C.blue, 2);
    text(baseline.toFixed(3), x + baseWidth + 18, y + 14, 29, C.blue, { family: "mono", weight: 500, baseline: "middle" });
    roundRect(x, y + 49, width, 42, 21, C.graySoft);
    roundRect(x, y + 49, altWidth, 42, 21, C.tealSoft, C.teal, 2);
    text(alternative.toFixed(3), x + altWidth + 18, y + 71, 29, C.teal, { family: "mono", weight: 500, baseline: "middle" });
  }

  function drawWaterLedger(time) {
    const water = DATA.water_ledger;
    withAlpha(phase(time, 0, 10.4, 0.8), () => {
      header("One synthetic storm · same modeled rainfall", FILM.title);
      const rain = appear(time, 1.1, 1.2);
      withAlpha(rain, () => {
        pill("2.5 in over 4 hr", 760, 215, C.blueSoft, C.blue, C.blue, 400);
        for (let index = 0; index < 13; index += 1) {
          const x = 440 + index * 82;
          const y = 300 + ((index * 37) % 80);
          packet(x, y, C.blue, 0.55);
        }
        arrow(960, 385, 960, 530, C.blue, 6, appear(time, 2.5, 1.0));
        card(430, 560, 460, 220, C.white, C.blue);
        card(1030, 560, 460, 220, C.white, C.teal);
        text("UNCHANGED MODEL", 660, 622, 30, C.blue, { family: "mono", weight: 500, align: "center" });
        text("ALT-207", 1260, 622, 30, C.teal, { family: "mono", weight: 500, align: "center" });
        text(`${water.baseline.precipitation.toFixed(3)} acre-ft`, 660, 708, 52, C.ink, { family: "heading", weight: 600, align: "center" });
        text(`${water.alternative.precipitation.toFixed(3)} acre-ft`, 1260, 708, 52, C.ink, { family: "heading", weight: 600, align: "center" });
        text("same modeled precipitation", 960, 865, 36, C.ink, { weight: 700, align: "center" });
      });
    });

    withAlpha(phase(time, 10.8, 30.3, 0.8), () => {
      header("SWMM runoff quantity continuity", "The model partitions the same rain differently");
      text("UNCHANGED MODEL", 1160, 226, 26, C.blue, { family: "mono", weight: 500 });
      text("ALT-207", 1440, 226, 26, C.teal, { family: "mono", weight: 500 });
      const reveal = appear(time, 11.6, 1.0);
      withAlpha(reveal, () => {
        ledgerBar("Infiltration loss", 300, water.baseline.infiltration_loss, water.alternative.infiltration_loss, 0.56, "SWMM runoff-accounting term");
        ledgerBar("Surface runoff", 465, water.baseline.surface_runoff, water.alternative.surface_runoff, 0.56);
        ledgerBar("Final storage", 630, water.baseline.final_storage, water.alternative.final_storage, 0.56);
        ledgerBar("LID drainage", 795, water.baseline.lid_drainage, water.alternative.lid_drainage, 0.56);
      });
      text("acre-ft", 1715, 226, 26, C.muted, { family: "mono" });
    });

    withAlpha(phase(time, 30.7, 41, 0.8), () => {
      header("Modeled outlet response", "17,000 fewer modeled gallons reach OUT1");
      const a = appear(time, 31.2, 0.9);
      withAlpha(a, () => {
        card(170, 315, 680, 270, C.blueSoft, C.blue);
        card(1070, 315, 680, 270, C.tealSoft, C.teal);
        text("UNCHANGED MODEL", 510, 380, 28, C.blue, { family: "mono", weight: 500, align: "center" });
        text(`${water.baseline.external_outflow.toFixed(3)} Mgal`, 510, 486, 76, C.ink, { family: "heading", weight: 600, align: "center" });
        text("ALT-207", 1410, 380, 28, C.teal, { family: "mono", weight: 500, align: "center" });
        text(`${water.alternative.external_outflow.toFixed(3)} Mgal`, 1410, 486, 76, C.ink, { family: "heading", weight: 600, align: "center" });
        arrow(860, 450, 1060, 450, C.ink, 6, appear(time, 32.2, 0.7));
        pill("−0.017 Mgal", 770, 620, C.tealSoft, C.teal, C.teal, 380);
        text("Across four separate synthetic tests", 960, 762, 36, C.muted, { align: "center" });
        text(`${water.four_event.baseline.toFixed(3)} → ${water.four_event.alternative.toFixed(3)} Mgal at OUT1`, 960, 832, 52, C.ink, { family: "mono", weight: 500, align: "center" });
      });
    });

    withAlpha(phase(time, 41.4, 50.2, 0.8), () => {
      header("Evidence boundary", "A hydraulic signal can justify the next investigation.");
      card(96, 280, 800, 430, C.tealSoft, C.teal);
      card(1024, 280, 800, 430, C.ochreSoft, C.ochre);
      text("MODELED HERE", 150, 348, 28, C.teal, { family: "mono", weight: 500 });
      text("Outlet volume + peak", 150, 430, 48, C.ink, { family: "heading", weight: 600 });
      text("Runoff accounting", 150, 500, 42, C.ink, { weight: 700 });
      text("Direct-run QA", 150, 568, 42, C.ink, { weight: 700 });
      text("NOT MEASURED HERE", 1080, 348, 28, C.ochre, { family: "mono", weight: 500 });
      text("Pollutants · habitat · heat", 1080, 430, 42, C.ink, { weight: 700 });
      text("Groundwater recharge", 1080, 500, 42, C.ink, { weight: 700 });
      text("Real flood risk · carbon", 1080, 568, 42, C.ink, { weight: 700 });
      text("It cannot substitute for field evidence.", 960, 825, 52, C.red, { family: "heading", weight: 600, align: "center" });
    });
    evidenceTransition(time, 10.6, "FOLLOW THE SAME RAIN");
    evidenceTransition(time, 30.5, "FOLLOW IT TO THE OUTFALL");
    evidenceTransition(time, 41.2, "DRAW THE EVIDENCE BOUNDARY");
    footer(time);
  }

  function hyetograph(x, y, width, height, intensities, color) {
    const max = Math.max(...intensities, 0.001);
    const gap = 3;
    const barWidth = (width - gap * (intensities.length - 1)) / intensities.length;
    intensities.forEach((value, index) => {
      const h = (value / max) * height;
      ctx.fillStyle = color;
      ctx.fillRect(x + index * (barWidth + gap), y + height - h, Math.max(2, barWidth), h);
    });
    line(x, y + height, x + width, y + height, C.ink, 2);
  }

  function drawStormReveals(time) {
    const storms = DATA.storms;
    withAlpha(phase(time, 0, 12.5, 0.8), () => {
      header("699 exact alternatives · model held fixed", FILM.title);
      storms.items.forEach((storm, index) => {
        const a = appear(time, 0.8 + index * 0.45, 0.8);
        withAlpha(a, () => {
          const x = 96 + index * 432;
          card(x, 250, 390, 520, C.white, index === 2 ? C.ochre : C.line);
          text(storm.short_label, x + 30, 310, 31, C.ink, { weight: 700 });
          text(`${storm.depth_inches.toFixed(1)} in · ${storm.duration_hours.toFixed(0)} hr`, x + 30, 358, 27, C.muted, { family: "mono" });
          hyetograph(x + 30, 415, 330, 180, storm.intensities, C.blue);
          text("positive peak deltas", x + 30, 658, 26, C.muted, { family: "mono" });
          text(String(storm.adverse_count), x + 195, 735, 82, storm.adverse_count ? C.red : C.teal, { family: "heading", weight: 600, align: "center" });
        });
      });
      text("Same network and alternatives. Different synthetic rain questions.", 960, 875, 40, C.ink, { weight: 700, align: "center" });
    });

    withAlpha(phase(time, 12.9, 25.5, 0.8), () => {
      header("Storm-specific guardrail", "The counts overlap");
      const counts = storms.items.map((storm) => storm.adverse_count);
      counts.forEach((count, index) => {
        const x = 150 + index * 350;
        pill(String(count), x, 300, count ? C.redSoft : C.tealSoft, count ? C.red : C.teal, count ? C.red : C.teal, 210);
        text(storms.items[index].tiny_label, x + 105, 390, 27, C.ink, { align: "center" });
      });
      text("= 468 storm–alternative flags", 960, 525, 48, C.ink, { family: "mono", weight: 500, align: "center" });
      withAlpha(appear(time, 18.5, 0.8), () => {
        card(520, 615, 880, 150, C.ochreSoft, C.ochre);
        text("47 alternatives fail in two events", 960, 675, 42, C.ochre, { weight: 700, align: "center" });
        text("421 unique adverse alternatives", 960, 738, 48, C.ink, { family: "heading", weight: 600, align: "center" });
      });
    });

    withAlpha(phase(time, 25.9, 36.2, 0.8), () => {
      header("What the storm set revealed", "The deepest storm was not the most revealing guardrail.");
      card(160, 295, 1600, 320, C.blueSoft, C.blue);
      text("2.5 in over 4 hr", 520, 410, 54, C.ink, { family: "heading", weight: 600, align: "center" });
      text("420", 520, 525, 92, C.red, { family: "heading", weight: 600, align: "center" });
      text("adverse alternatives", 520, 580, 31, C.ink, { align: "center" });
      text("4.0 in over 2 hr", 1400, 410, 54, C.ink, { family: "heading", weight: 600, align: "center" });
      text("47", 1400, 525, 92, C.red, { family: "heading", weight: 600, align: "center" });
      text("adverse alternatives", 1400, 580, 31, C.ink, { align: "center" });
      wrap("Depth, duration, and shape changed together. This study does not isolate one cause.", 960, 755, 1500, 42, C.ochre, { weight: 700, align: "center", lineHeight: 52 });
    });
    evidenceTransition(time, 12.7, "COMPARE EACH STORM");
    evidenceTransition(time, 25.7, "RECONCILE THE OVERLAP");
    footer(time);
  }

  function drawTokenField(rows, time, stage) {
    const columns = 37;
    const size = 15;
    const gap = 7;
    const left = 105;
    const top = 250;
    rows.forEach((row, index) => {
      const x = left + (index % columns) * (size + gap);
      const y = top + Math.floor(index / columns) * (size + gap);
      let active = true;
      if (stage >= 1) active = row.volume_pass;
      if (stage >= 2) active = row.volume_cost_pass;
      if (stage >= 3) active = row.feasible;
      const frontier = stage >= 4 && row.frontier;
      ctx.fillStyle = active ? C.teal : C.graySoft;
      ctx.fillRect(x, y, size, size);
      if (frontier) {
        ctx.strokeStyle = C.ink;
        ctx.lineWidth = 4;
        ctx.strokeRect(x - 4, y - 4, size + 8, size + 8);
      }
    });
  }

  function drawDecisionFunnel(time) {
    const funnel = DATA.decision_funnel;
    withAlpha(phase(time, 0, 28.7, 0.8), () => {
      header("Complete finite universe · direct SWMM results", FILM.title);
      let stage = 0;
      if (time >= 8) stage = 1;
      if (time >= 13.5) stage = 2;
      if (time >= 19) stage = 3;
      if (time >= 24.5) stage = 4;
      drawTokenField(funnel.rows, time, stage);
      text("Each square is one nonbaseline alternative", 105, 730, 30, C.muted, { family: "mono" });
      const stages = [
        ["Directly simulated", "699", C.blue],
        ["Meet ≥6% volume", "297", C.teal],
        ["Also ≤$200k", "46", C.ochre],
        ["Also no peak rise", "12", C.red],
        ["Feasible frontier", "4", C.ink],
      ];
      stages.forEach((item, index) => {
        const active = index <= stage;
        const x = 1030;
        const y = 245 + index * 132;
        withAlpha(active ? 1 : 0.28, () => {
          card(x, y, 760, 100, active ? C.white : C.graySoft, active ? item[2] : C.line);
          text(item[0], x + 32, y + 58, 34, C.ink, { weight: 700 });
          text(item[1], x + 690, y + 58, 55, item[2], { family: "heading", weight: 600, align: "right", baseline: "middle" });
        });
      });
      withAlpha(appear(time, 26.5, 0.7), () => {
        pill("QA ACCEPTED: 699 / 699", 1120, 890, C.tealSoft, C.teal, C.teal, 580);
      });
    });

    withAlpha(phase(time, 29.1, 38.2, 0.8), () => {
      header("Screening outcome", "Field effort becomes focused—not finished.");
      const questions = ["SOILS", "UTILITIES", "MAINTENANCE", "CONSTRUCTABILITY"];
      questions.forEach((question, index) => {
        const x = 150 + index * 430;
        card(x, 330, 360, 250, C.ochreSoft, C.ochre);
        packet(x + 180, 405, C.teal, 0.8);
        text(question, x + 180, 505, 28, C.ink, { family: "mono", weight: 500, align: "center" });
      });
      text("12 modeled candidates", 960, 700, 54, C.teal, { family: "heading", weight: 600, align: "center" });
      text("→ a smaller set of real-world questions", 960, 778, 44, C.ink, { weight: 700, align: "center" });
      text("No construction verdict is produced.", 960, 868, 36, C.ochre, { family: "mono", align: "center" });
    });
    evidenceTransition(time, 28.9, "MODELED SHORTLIST → FIELD QUESTIONS");
    footer(time);
  }

  function drawSearchReleaseGate(time) {
    const search = DATA.search;
    withAlpha(phase(time, 0, 11.8, 0.8), () => {
      header("Bounded search", FILM.title);
      const cols = 10;
      for (let index = 0; index < 50; index += 1) {
        const x = 120 + (index % cols) * 70;
        const y = 260 + Math.floor(index / cols) * 70;
        card(x, y, 48, 48, C.white, C.line, 8);
      }
      text("699 face-down alternatives", 455, 680, 36, C.muted, { family: "mono", align: "center" });
      const p = appear(time, 2.2, 1.2);
      const packetX = mix(505, 945, p);
      const packetY = mix(330, 420, p);
      packet(packetX, packetY, C.blue, 0.85);
      text("SEARCH ASKS", 920, 280, 28, C.blue, { family: "mono", weight: 500, align: "center" });
      card(800, 320, 280, 200, C.blueSoft, C.blue);
      text("[0, 3, 2, 6]", 940, 425, 38, C.ink, { family: "mono", weight: 500, align: "center" });
      arrow(1100, 420, 1280, 420, C.ink, 5, appear(time, 4.1, 0.8));
      card(1300, 250, 500, 370, C.tealSoft, C.teal);
      text("4 DIRECT SWMM JOBS", 1550, 305, 28, C.teal, { family: "mono", weight: 500, align: "center" });
      search.storm_labels.forEach((label, index) => {
        const y = 355 + index * 60;
        packet(1360, y, C.blue, 0.35);
        text(label, 1400, y + 2, 28, C.ink, { baseline: "middle" });
        pill("EXACT", 1635, y - 21, C.white, C.teal, C.teal, 120);
      });
      text("Search selects the question.", 960, 780, 46, C.blue, { family: "heading", weight: 600, align: "center" });
      text("SWMM supplies every hydraulic answer.", 960, 848, 46, C.teal, { family: "heading", weight: 600, align: "center" });
    });

    withAlpha(phase(time, 12.2, 22.5, 0.8), () => {
      header("Controlled benchmark", "Every method takes the same exam");
      const methods = search.methods;
      const budgets = search.budgets;
      text("METHOD", 120, 265, 27, C.muted, { family: "mono" });
      budgets.forEach((budget, index) => text(`${budget} designs`, 730 + index * 330, 265, 27, C.muted, { family: "mono", align: "center" }));
      methods.forEach((method, row) => {
        const y = 320 + row * 130;
        text(method, 120, y + 62, 36, C.ink, { weight: 700 });
        budgets.forEach((budget, column) => {
          const x = 600 + column * 330;
          card(x, y, 260, 96, C.white, C.line);
          for (let seed = 0; seed < 10; seed += 1) {
            ctx.beginPath();
            ctx.arc(x + 34 + seed * 21, y + 48, 7, 0, Math.PI * 2);
            ctx.fillStyle = method.includes("ExtraTrees") ? C.teal : C.blue;
            ctx.fill();
          }
        });
      });
      pill("4 METHODS × 3 BUDGETS × 10 SEEDS = 120 REPLAYS", 420, 860, C.blueSoft, C.blue, C.blue, 1080);
    });

    withAlpha(phase(time, 22.9, 35.5, 0.8), () => {
      header("Largest declared budget · Sobol + ExtraTrees", "A promising score faces release gates");
      const metrics = [
        ["Hypervolume", search.extratrees.hypervolume.toFixed(3)],
        ["Feasible recall", `${(search.extratrees.feasible_recall * 100).toFixed(1)}%`],
        ["Adverse recall", `${(search.extratrees.adverse_recall * 100).toFixed(1)}%`],
      ];
      metrics.forEach((metric, index) => {
        const x = 96 + index * 420;
        card(x, 270, 380, 190, C.tealSoft, C.teal);
        text(metric[0], x + 30, 330, 29, C.ink, { family: "mono" });
        text(metric[1], x + 190, 414, 62, C.teal, { family: "heading", weight: 600, align: "center" });
      });
      card(1400, 270, 420, 190, C.redSoft, C.red);
      text("RELEASE GATES", 1610, 330, 29, C.red, { family: "mono", weight: 500, align: "center" });
      text("1 PASS · 4 FAIL", 1610, 414, 52, C.red, { family: "heading", weight: 600, align: "center" });
      withAlpha(appear(time, 27.5, 0.8), () => {
        card(210, 560, 1500, 265, C.ochreSoft, C.ochre);
        text("The 95% adverse-recall gate is impossible at this budget", 960, 625, 45, C.ochre, { family: "heading", weight: 600, align: "center" });
        text("421 adverse × 95% = 400 adverse discoveries needed", 960, 706, 38, C.ink, { family: "mono", weight: 500, align: "center" });
        text("budget = 256 total requests · theoretical ceiling = 60.8%", 960, 768, 34, C.ink, { family: "mono", align: "center" });
      });
    });

    withAlpha(phase(time, 35.9, 46.2, 0.8), () => {
      header("Release decision", "A promising score is not a release decision.");
      card(140, 300, 1640, 290, C.white, C.line);
      text("ADAPTIVE ML", 260, 382, 28, C.red, { family: "mono", weight: 500 });
      text("remains experimental", 260, 468, 58, C.red, { family: "heading", weight: 600 });
      line(960, 330, 960, 555, C.line, 3);
      text("PRODUCTION SEARCH", 1080, 382, 28, C.teal, { family: "mono", weight: 500 });
      text("scrambled Sobol", 1080, 468, 58, C.teal, { family: "heading", weight: 600 });
      pill("kWh: NOT MEASURED", 360, 690, C.ochreSoft, C.ochre, C.ochre, 520);
      pill("CO2e: NOT MEASURED", 1040, 690, C.ochreSoft, C.ochre, C.ochre, 520);
      text("Exact-job equivalents are accounting units—not carbon savings.", 960, 855, 38, C.ink, { weight: 700, align: "center" });
    });
    evidenceTransition(time, 12.0, "SAME CANDIDATES · SAME BUDGET");
    evidenceTransition(time, 22.7, "REVEAL ONLY THE REQUESTED ANSWER");
    evidenceTransition(time, 35.7, "APPLY THE RELEASE GATES");
    footer(time);
  }

  const DRAWERS = Object.freeze({
    "incomplete-plot": drawIncompletePlot,
    "modeled-water-ledger": drawWaterLedger,
    "storm-reveals": drawStormReveals,
    "decision-funnel": drawDecisionFunnel,
    "search-release-gate": drawSearchReleaseGate,
  });

  function render(time) {
    const t = clamp(time, 0, FILM.duration - 1 / FILM.fps);
    if (typeof ctx.reset === "function") ctx.reset();
    else {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      ctx.setLineDash([]);
    }
    ctx.imageSmoothingEnabled = true;
    background();
    DRAWERS[slug](t);
    clock.value = `${formatTime(t)} / ${formatTime(FILM.duration)}`;
    if (!renderMode) scrub.value = String(t);
  }

  function currentTime(now = performance.now()) {
    return playing ? (anchorFilm + (now - anchorTime) / 1000) % FILM.duration : manualTime;
  }

  function loop(now) {
    if (!renderMode) render(currentTime(now));
    requestAnimationFrame(loop);
  }

  toggle.addEventListener("click", () => {
    if (playing) {
      manualTime = currentTime();
      playing = false;
      toggle.textContent = "Play";
      toggle.setAttribute("aria-label", "Play animation");
    } else {
      anchorFilm = manualTime;
      anchorTime = performance.now();
      playing = true;
      toggle.textContent = "Pause";
      toggle.setAttribute("aria-label", "Pause animation");
    }
  });

  scrub.addEventListener("input", () => {
    manualTime = Number(scrub.value);
    anchorFilm = manualTime;
    anchorTime = performance.now();
    render(manualTime);
  });

  window.setAnimationTime = (seconds) => {
    renderMode = true;
    playing = false;
    manualTime = Number(seconds) || 0;
    render(manualTime);
    return true;
  };

  window.releaseAnimation = () => {
    renderMode = false;
    anchorFilm = manualTime;
    anchorTime = performance.now();
    playing = true;
    toggle.textContent = "Pause";
    toggle.setAttribute("aria-label", "Pause animation");
    return true;
  };

  Promise.all([
    document.fonts.load('600 68px "IBM Plex Sans Condensed"'),
    document.fonts.load('400 42px "Atkinson Hyperlegible"'),
    document.fonts.load('700 48px "Atkinson Hyperlegible"'),
    document.fonts.load('400 25px "IBM Plex Mono"'),
    document.fonts.load('500 34px "IBM Plex Mono"'),
  ]).then(() => document.fonts.ready).then(() => {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      playing = false;
      manualTime = FILM.reducedMotionTime;
      toggle.textContent = "Play";
      toggle.setAttribute("aria-label", "Play animation");
    }
    render(reduced ? FILM.reducedMotionTime : 0);
    window.__animationReady = true;
    requestAnimationFrame(loop);
  });
})();
