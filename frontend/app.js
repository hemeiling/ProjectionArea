/* Projected Area Analyzer — front end.
 *
 * Every number shown here comes from the backend. This file renders state and
 * collects input; it never computes an area, a scale or a unit conversion. When
 * something needs explaining, it prints the evidence the engine already
 * produced (FootprintInterpretation.evidence, Scale.calibration, warnings,
 * assumptions) rather than re-deriving a second opinion in JavaScript.
 */

const API = location.origin;

/* ── i18n ────────────────────────────────────────────────────────────────────
 *
 * One catalogue per language, keyed by stable identifiers. Nothing user-facing
 * is written literally in this file or in the HTML: `t(key, params)` is the only
 * way a string reaches the screen.
 *
 * Engineering identifiers are deliberately *not* translated — file names, layer
 * and block names, entity types, handles, units and every number stay exactly as
 * the drawing and the engine produced them. Translating a layer called
 * `中心线层` into English, or a number into a localised format, would corrupt the
 * record this tool exists to keep.
 */
const LANGS = { en: "en", "zh-CN": "zh-CN" };
const LANG_KEY = "projected-area.lang";
let LANG = "en";
const CATALOGUE = { en: {}, "zh-CN": {} };

function t(key, params = {}, fallback = null) {
  const table = CATALOGUE[LANG] || {};
  let text = table[key];
  if (text === undefined) text = CATALOGUE.en[key];
  if (text === undefined) return fallback !== null ? fallback : key;
  return String(text).replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);
}

async function loadCatalogue(lang) {
  if (Object.keys(CATALOGUE[lang] || {}).length) return;
  try {
    CATALOGUE[lang] = await (await fetch(`/static/i18n/${lang}.json`)).json();
  } catch (e) {
    CATALOGUE[lang] = CATALOGUE[lang] || {};
  }
}

/* Static markup carries keys, so switching language re-renders it in place —
 * no duplicated pages, and the loaded drawing is never disturbed. */
function applyStaticText() {
  document.documentElement.lang = LANG === "zh-CN" ? "zh-CN" : "en";
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    el.title = t(el.dataset.i18nTitle);
  }
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  }
}

async function setLanguage(lang, { rerender = true } = {}) {
  if (!LANGS[lang]) return;
  await loadCatalogue(lang);
  LANG = lang;
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* private window */ }
  for (const btn of document.querySelectorAll("[data-lang]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.lang === lang));
  }
  applyStaticText();
  if (rerender) {
    // Re-render whatever is on screen. The analysis is untouched: no request is
    // made and no state is cleared, so the drawing stays exactly as it was.
    if (!$("workspace").classList.contains("hide")) paintAll();
    // A running job keeps running. Only its labels change, and they change now
    // rather than at the next poll, which on a slow DWG is 700 ms of half-
    // translated screen.
    if (!$("processing").classList.contains("hide") && LAST_SNAP) {
      renderProgress(LAST_SNAP);
      renderTimeline(timelineFromJob(LAST_SNAP));
    }
    renderDemoList();
  }
}

const S = {
  docId: null, fileName: "", kind: "pdf", page: 1, conversion: null,
  pdf: null, pdfPage: null, cad: null,
  analysis: null, result: null, ignored: [],
  scaleSpec: { mode: "auto" },
  fpType: null,
  zoom: 1, fitZoom: 1,
  picking: false, picks: [],
  layers: {
    source: true, counted: true, excluded: true, dimensions: true, holes: true,
    footprint: true, boundary: false, internal: false, envelope: false,
    rect: false, calibration: true, warnings: true,
  },
  hiddenLayers: new Set(),
  capabilities: null, demos: null,
};

const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 2) => Number(v).toLocaleString(undefined,
  { minimumFractionDigits: d, maximumFractionDigits: d });

/* Areas span eleven orders of magnitude between a bracket and a production
 * line, so the unit follows the magnitude rather than being fixed. */
function areaText(units) {
  if (!units) return { value: "—", unit: "" };
  const m2 = units.m2;
  if (m2 >= 1) return { value: num(m2, 2), unit: "m²" };
  if (units.cm2 >= 100) return { value: num(units.cm2, 1), unit: "cm²" };
  return { value: num(units.mm2, 1), unit: "mm²" };
}
function altText(units) {
  if (!units) return "";
  return `${num(units.ft2, 2)} ft²   ·   ${num(units.mm2, 0)} mm²`;
}

async function call(path, options = {}) {
  const res = await fetch(API + path, options);
  if (!res.ok) {
    let detail;
    try { detail = (await res.json()).detail; } catch (e) { detail = await res.text(); }
    const err = new Error(typeof detail === "string" ? detail : (detail?.headline || "Request failed"));
    err.detail = detail; err.status = res.status;
    throw err;
  }
  return res.json();
}

/* ── screens ─────────────────────────────────────────────────────────────── */

function show(screen) {
  for (const id of ["landing", "processing", "workspace"]) {
    $(id).classList.toggle("hide", id !== screen);
  }
}

/* A DWG is converted before it can be read, and its scale comes from CAD units
 * rather than from measuring the drawing, so it gets its own honest sequence
 * instead of the PDF's labels reused. */
/* Stage keys mirror backend/progress.py exactly, so the checklist and the bar
 * are describing the same run rather than two parallel guesses. */
const STAGES_PDF = [
  ["loaded", "stage.load"],
  ["geometry", "stage.geometry"],
  ["regions", "stage.regions"],
  ["scale", "stage.scale"],
  ["candidates", "stage.candidates"],
  ["area", "stage.area"],
];
const STAGES_DXF = [
  ["validated", "stage.load"],
  ["geometry", "stage.geometry"],
  ["units", "stage.scaleCad"],
  ["layers", "stage.regionsCad"],
  ["candidates", "stage.candidates"],
  ["area", "stage.area"],
];
/* A DWG has a conversion step a DXF does not. It is absent from the DXF plan
 * rather than shown as skipped: claiming a step that never ran would be a
 * small lie in a tool whose whole point is not telling them. */
const STAGES_DWG = [
  ["validated", "stage.loadDwg"],
  ["converted", "stage.convert"],
  ["geometry", "stage.geometry"],
  ["units", "stage.scaleCad"],
  ["layers", "stage.regionsCad"],
  ["candidates", "stage.candidates"],
  ["area", "stage.area"],
];
const STAGE_PLANS = { pdf: STAGES_PDF, dxf: STAGES_DXF, dwg: STAGES_DWG };
let STAGES = STAGES_PDF;

/* Which count a stage is reporting, for the "281,400 / 421,517 primitives" line. */
const STAGE_UNITS = {
  geometry: "unit.primitives",
  candidates: "unit.components",
};
const MARKS = {
  pending: "·", active: "…", done: "✓", warn: "⚠", input: "⚠",
  unsupported: "—", failed: "✕",
};

function renderTimeline(states) {
  $("timeline").innerHTML = STAGES.map(([key, labelKey]) => {
    const st = states[key] || { state: "pending" };
    return `<div class="stage" data-stage="${key}" data-state="${st.state}">
      <span class="mark">${MARKS[st.state] || "·"}</span>
      <span class="label">${esc(t(labelKey))}</span>
      <span class="note">${esc(st.note || "")}</span>
    </div>`;
  }).join("");
}

function notice(target, { headline, body, fix, kind }) {
  $(target).innerHTML = `<div class="notice ${kind === "error" ? "error" : ""}">
    <h4>${esc(headline)}</h4>
    ${body ? `<p>${esc(body)}</p>` : ""}
    ${fix ? `<p class="fix">${esc(fix)}</p>` : ""}
  </div>`;
}

/* ── upload and processing ───────────────────────────────────────────────── */

/* How long a stage may run before the UI says "still working" rather than
 * leaving the reader to wonder whether it has hung. */
const SLOW_STAGE_SECONDS = 20;

let PROGRESS = { fraction: 0, state: "idle" };

/* The most recent job snapshot. Kept so a language switch can repaint the bar
 * and the timeline immediately, instead of leaving half the screen in the old
 * language until the next poll arrives. Never used to advance anything. */
let LAST_SNAP = null;

function resetProgress() {
  PROGRESS = { fraction: 0, state: "running", stageSince: Date.now(), stage: "" };
  LAST_SNAP = null;
  const block = $("progressBlock");
  block.classList.remove("done", "failed", "indeterminate");
  $("progressFill").style.width = "0%";
  $("progressGhost").hidden = true;
  $("progressPct").textContent = "0%";
  $("progressWhat").textContent = "";
  $("progressElapsed").textContent = "";
  $("progressCounts").textContent = "";
  $("progressSlow").hidden = true;
}

/* The heading says what the run is and, if it failed, how. Written through the
 * i18n key rather than the text, so a language switch re-translates whichever
 * heading is showing instead of reverting it to the generic one. */
function setProcTitle(key) {
  const el = $("procTitle");
  el.dataset.i18n = key;
  el.textContent = t(key);
}

/* Renders a backend progress snapshot. Nothing here invents a number: the
 * fraction, the counts and the elapsed time all come from the job. */
function renderProgress(snap) {
  LAST_SNAP = snap;
  const block = $("progressBlock");
  // Monotonic on the client too, so a late-arriving poll cannot rewind the bar.
  const fraction = Math.max(PROGRESS.fraction, snap.progress ?? 0);
  PROGRESS.fraction = fraction;

  if (snap.stage && snap.stage !== PROGRESS.stage) {
    PROGRESS.stage = snap.stage;
    PROGRESS.stageSince = Date.now();
  }

  $("progressFill").style.width = `${Math.round(fraction * 1000) / 10}%`;
  $("progressPct").textContent = `${Math.round(fraction * 100)}%`;
  $("progressElapsed").textContent = snap.elapsed_seconds != null
    ? t("progress.elapsed", { seconds: snap.elapsed_seconds.toFixed(1) }) : "";

  const failed = snap.state === "failed";
  const done = snap.state === "done";
  block.classList.toggle("failed", failed);
  block.classList.toggle("done", done);

  /* An unquantifiable stage gets an animated band over its own slice of the bar,
   * between where progress has actually reached and where that stage ends. The
   * span comes from the backend's measured weights, so the band is the real
   * region of uncertainty rather than a guess at one. */
  const indeterminate = Boolean(snap.indeterminate) && !done && !failed;
  const ghost = $("progressGhost");
  block.classList.toggle("indeterminate", indeterminate);
  if (indeterminate && snap.stage_end > fraction) {
    const from = Math.max(fraction, snap.stage_start ?? fraction);
    ghost.style.left = `${from * 100}%`;
    ghost.style.width = `${Math.max(0, snap.stage_end - from) * 100}%`;
    ghost.hidden = false;
  } else {
    ghost.hidden = true;
  }

  if (done) {
    $("progressWhat").textContent = t("progress.complete");
    $("progressCounts").textContent = snap.elapsed_seconds != null
      ? t("progress.total", { seconds: snap.elapsed_seconds.toFixed(1) }) : "";
    $("progressSlow").hidden = true;
    return;
  }
  if (failed) {
    const stage = snap.failed_stage || snap.stage;
    const label = STAGES.find(([key]) => key === stage);
    $("progressWhat").innerHTML = `<span class="failnote">${
      esc(t("progress.failedDuring", { stage: label ? t(label[1]) : stage }))}</span>`;
    $("progressCounts").textContent = "";
    $("progressSlow").hidden = true;
    return;
  }

  $("progressWhat").textContent = t(`stageActive.${snap.stage}`, {}, snap.stage_detail || "");

  /* Real counts where the backend has them; nothing where it does not. */
  const unitKey = STAGE_UNITS[snap.stage];
  if (snap.current && unitKey) {
    const unit = t(unitKey);
    $("progressCounts").textContent = snap.total
      ? t("progress.counts", {
          current: snap.current.toLocaleString(), total: snap.total.toLocaleString(), unit })
      : t("progress.countsUnknown", { current: snap.current.toLocaleString(), unit });
  } else {
    $("progressCounts").textContent = "";
  }

  const stalled = (Date.now() - PROGRESS.stageSince) / 1000 > SLOW_STAGE_SECONDS;
  $("progressSlow").hidden = !stalled;
  if (stalled) $("progressSlow").textContent = t("progress.slow");
}

/* The job's stage records drive the checklist below the bar. */
function timelineFromJob(snap) {
  const states = {};
  for (const [key] of STAGES) states[key] = { state: "pending" };
  for (const record of snap.stages || []) {
    if (!(record.stage in states)) continue;
    states[record.stage] = {
      state: record.done ? "done" : "active",
      note: record.detail || "",
    };
  }
  if (snap.state === "failed") {
    const stage = snap.failed_stage || snap.stage;
    if (stage in states) states[stage] = { state: "failed", note: t("stage.note.unreadable") };
  }
  if (snap.state === "done") for (const key of Object.keys(states)) states[key].state = "done";
  return states;
}

async function handleFile(file) {
  $("uploadNotice").innerHTML = "";
  show("processing");
  $("procFile").textContent = file.name;
  $("procNotice").innerHTML = "";

  /* The extension is only a hint; the backend decides by signature and the plan
   * is re-picked from its reply. Choosing now means a DWG reads "DWG validated"
   * from the first tick rather than starting wrong. */
  const guessed = /\.dwg$/i.test(file.name) ? "dwg"
    : /\.dxf$/i.test(file.name) ? "dxf" : "pdf";
  STAGES = STAGE_PLANS[guessed];
  setProcTitle(guessed === "dwg" ? "proc.titleDwg" : "proc.title");

  resetProgress();
  renderTimeline(timelineFromJob({ stages: [] }));

  let job;
  try {
    const body = new FormData();
    body.append("file", file);
    job = await call("/api/analyse", { method: "POST", body });
  } catch (err) {
    const d = err.detail;
    const missing = d?.kind === "dwg_component_missing";
    setProcTitle(missing ? "proc.titleDwgMissing" : "proc.titleUnreadable");
    renderProgress({ state: "failed", progress: 0, failed_stage: STAGES[0][0] });
    notice("procNotice", {
      headline: d?.headline || err.message, body: d?.reason, fix: d?.fix,
      kind: missing ? "warn" : "error",
    });
    return;
  }

  let snap = job;
  try {
    while (true) {
      snap = await call(`/api/jobs/${job.job_id}`);
      if (snap.plan) {
        const plan = snap.plan.join(",");
        for (const [kind, stages] of Object.entries(STAGE_PLANS)) {
          if (stages.map(([k]) => k).join(",") === plan) STAGES = stages;
        }
      }
      renderProgress(snap);
      renderTimeline(timelineFromJob(snap));
      if (snap.state === "done" || snap.state === "failed") break;
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  } catch (err) {
    /* The bar keeps the progress it earned; only the reason changes. A job that
     * has vanished is the visible symptom of the server process restarting — on
     * a small instance, usually because a large drawing exhausted its memory —
     * so that case is named rather than reported as a generic failure. */
    const lost = err.status === 404 || err.detail?.kind === "job_lost";
    renderProgress({ state: "failed", progress: PROGRESS.fraction, failed_stage: snap.stage });
    if (lost) setProcTitle("proc.titleLost");
    notice("procNotice", {
      headline: lost ? t("proc.lostHeadline") : t("proc.failed"),
      body: lost ? t("proc.lostBody") : err.message,
      fix: lost ? t("proc.lostFix") : (err.detail && err.detail.fix),
      kind: "error",
    });
    return;
  }

  if (snap.state === "failed") {
    const d = snap.error || {};
    const missing = d.kind === "dwg_component_missing";
    const conversionFailed = d.kind === "conversion_failed";
    const emptyDrawing = d.kind === "empty_drawing";
    setProcTitle(
      missing ? "proc.titleDwgMissing"
      : conversionFailed ? "proc.titleDwgFailed"
      : emptyDrawing ? "proc.titleEmpty"
      : "proc.titleUnreadable");
    notice("procNotice", {
      headline: d.headline || t("proc.failed"), body: d.reason, fix: d.fix,
      kind: missing || emptyDrawing ? "warn" : "error",
    });
    return;
  }

  // ── adopt the finished result ──
  const payload = snap.result;
  const doc = payload.document;
  S.docId = doc.document_id;
  S.fileName = doc.file_name;
  S.kind = doc.source_kind || "pdf";
  S.cad = doc.cad || null;
  S.conversion = (doc.cad && doc.cad.conversion) || null;
  S.page = doc.suggested_page || 1;
  S.scaleSpec = { mode: "auto" };
  S.fpType = null;
  S.picks = [];
  S.analysis = payload.analysis;
  S.result = payload.area;
  S.ignored = (payload.overlay && payload.overlay.primitives) || [];

  if (S.kind === "pdf") await renderPdf(file);

  // Let 100 % be visible before moving on, so completion is seen not inferred.
  setTimeout(() => {
    show("workspace");
    if (S.kind !== "pdf") { S.cadBox = cadExtent(); renderCad(); }
    paintAll();
    fitToWindow();
  }, 500);
}

/* ── drawing render ──────────────────────────────────────────────────────── */

async function renderPdf(file) {
  const buffer = await file.arrayBuffer();
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  S.pdf = await pdfjsLib.getDocument({ data: buffer }).promise;
  S.pdfPage = await S.pdf.getPage(S.page);
  await paintPage();
}

async function paintPage() {
  if (S.kind !== "pdf" || !S.pdfPage) return;
  const viewport = S.pdfPage.getViewport({ scale: S.zoom });
  const canvas = $("pageCanvas");
  canvas.width = Math.round(viewport.width);
  canvas.height = Math.round(viewport.height);
  $("sheet").style.width = canvas.width + "px";
  await S.pdfPage.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
}

/* The extent of everything the backend sent us, in drawing units. */
function cadExtent() {
  const pts = [];
  for (const prim of S.ignored) for (const p of prim.points) pts.push(p);
  for (const item of S.result?.footprint_interpretations || []) {
    for (const ringPts of item.outer || []) for (const p of ringPts) pts.push(p);
  }
  if (!pts.length) return null;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

/* A CAD source has no page to rasterise. The geometry itself is the drawing, so
 * the counted linework is stroked onto the canvas — §5: the vectors are drawn,
 * never re-traced from an image. */
function renderCad() {
  const canvas = $("pageCanvas");
  const w = 1000, h = 700;
  canvas.width = Math.round(w * S.zoom);
  canvas.height = Math.round(h * S.zoom);
  $("sheet").style.width = canvas.width + "px";
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!S.cadBox) return;

  ctx.lineWidth = 0.6;
  ctx.strokeStyle = "rgba(18,24,27,.55)";
  ctx.beginPath();
  let drawn = 0;
  for (const prim of S.ignored) {
    if (prim.layer && S.hiddenLayers.has(prim.layer)) continue;
    const pts = prim.points;
    if (pts.length < 2 || ++drawn > 60000) continue;
    const a = px(pts[0]);
    ctx.moveTo(a[0], a[1]);
    for (let i = 1; i < pts.length; i++) { const q = px(pts[i]); ctx.lineTo(q[0], q[1]); }
  }
  ctx.stroke();
}

/* Page-space -> canvas-space. For a PDF this is PDF units at the current zoom;
 * for a DXF the whole model-space extent is fitted into the canvas box. */
function viewTransform() {
  if (S.kind === "pdf") return { k: S.zoom, dx: 0, dy: 0, flip: false };
  const b = S.cadBox;
  if (!b) return { k: S.zoom, dx: 0, dy: 0, flip: false };
  const pad = 0.04 * Math.max(b.x1 - b.x0, b.y1 - b.y0);
  const w = (b.x1 - b.x0) + 2 * pad, h = (b.y1 - b.y0) + 2 * pad;
  const k = Math.min(1000 / w, 700 / h) * S.zoom;
  return { k, dx: (-b.x0 + pad) * k, dy: (b.y1 + pad) * k, flip: true };
}
function px(pt) {
  const t = viewTransform();
  return t.flip
    ? [pt[0] * t.k + t.dx, t.dy - pt[1] * t.k]
    : [pt[0] * t.k, pt[1] * t.k];
}
function toPage(clientX, clientY) {
  const rect = $("pageCanvas").getBoundingClientRect();
  const cx = (clientX - rect.left) * ($("pageCanvas").width / rect.width);
  const cy = (clientY - rect.top) * ($("pageCanvas").height / rect.height);
  const t = viewTransform();
  return t.flip ? [(cx - t.dx) / t.k, (t.dy - cy) / t.k] : [cx / t.k, cy / t.k];
}

/* ── overlay ─────────────────────────────────────────────────────────────── */

const FP_COLOURS = {
  geometry_union: "#1F6F63",
  enclosing_boundary: "#A9711B",
  internal_union: "#2F8E7E",
  convex_envelope: "#3E6392",
  bounding_rectangle: "#6C5A93",
  conveyor_footprint: "#55636A",
  guarded_area: "#55636A",
  line_footprint: "#55636A",
};
/* The reading's display name is a translated label; its colour is not. */
const fpLabel = (type, fallback) => t(`fpName.${type}`, {}, fallback || type);
const fpColour = (type) => FP_COLOURS[type] || "#1F6F63";
const OVERLAY_LAYERS = [
  ["source", "rgba(85,99,106,.34)"],
  ["counted", "rgba(31,111,99,.55)"],
  ["excluded", "rgba(85,99,106,.55)"],
  ["dimensions", "rgba(85,99,106,.25)"],
  ["holes", "rgba(154,52,18,.55)"],
  ["footprint", "rgba(31,111,99,.32)"],
  ["boundary", "rgba(169,113,27,.45)"],
  ["internal", "rgba(47,142,126,.45)"],
  ["envelope", "rgba(62,99,146,.45)"],
  ["rect", "rgba(108,90,147,.45)"],
  ["calibration", "rgba(154,52,18,.85)"],
  ["warnings", "rgba(169,113,27,.55)"],
];

const ring = (pts) => "M" + pts.map((p) => { const q = px(p); return `${q[0].toFixed(1)},${q[1].toFixed(1)}`; }).join("L") + "Z";
const path = (pts, closed) => "M" + pts.map((p) => { const q = px(p); return `${q[0].toFixed(1)},${q[1].toFixed(1)}`; }).join("L") + (closed ? "Z" : "");

function currentReading() {
  const list = S.result?.footprint_interpretations || [];
  if (!list.length) return null;
  return list.find((i) => i.type === S.fpType) || list[0];
}
const readingUnits = (r) => (r ? r.units : null);

function paintOverlay() {
  const svg = $("overlay"), canvas = $("pageCanvas");
  if (!canvas.width) { svg.innerHTML = ""; return; }
  svg.setAttribute("width", canvas.width);
  svg.setAttribute("height", canvas.height);
  svg.setAttribute("viewBox", `0 0 ${canvas.width} ${canvas.height}`);
  const hair = 1.0;
  const out = [];

  const DIM = { dimension: 1, annotation: 1, sheet: 1, hatch: 1 };
  if (S.layers.source) {
    for (const prim of S.ignored) {
      const role = prim.role;
      if (role === "uncertain" ? !S.layers.warnings
        : DIM[role] ? !S.layers.dimensions : !S.layers.excluded) continue;
      if (S.kind === "dxf" && prim.layer && S.hiddenLayers.has(prim.layer)) continue;
      const colour = role === "uncertain" ? "#A9711B" : "#55636A";
      out.push(`<path d="${path(prim.points, prim.closed)}" fill="none" stroke="${colour}"
        stroke-opacity=".38" stroke-width="${hair}"/>`);
    }
  }

  const result = S.result;
  if (result) {
    const byType = {};
    for (const item of result.footprint_interpretations) byType[item.type] = item;
    const refs = [["boundary", "enclosing_boundary"], ["internal", "internal_union"],
                  ["envelope", "convex_envelope"], ["rect", "bounding_rectangle"]];
    for (const [layer, type] of refs) {
      if (!S.layers[layer]) continue;
      const item = byType[type];
      if (!item) continue;
      for (const r of item.outer || []) {
        out.push(`<path d="${ring(r)}" fill="none" stroke="${fpColour(type)}"
          stroke-width="${hair * 1.3}" stroke-dasharray="5 3.5" stroke-opacity=".95"/>`);
      }
    }

    const sel = currentReading();
    if (S.layers.footprint && sel && (sel.outer || []).length) {
      const tint = fpColour(sel.type);
      const d = (sel.outer || []).map(ring)
        .concat(S.layers.holes ? (sel.holes || []).map(ring) : []).join(" ");
      out.push(`<path d="${d}" fill-rule="evenodd" fill="${tint}" fill-opacity=".26"
        stroke="${tint}" stroke-width="${hair * 1.9}" data-fp-shape="${sel.type}"/>`);
      if (S.layers.holes) {
        for (const h of sel.holes || []) {
          out.push(`<path d="${ring(h)}" fill="rgba(154,52,18,.30)" stroke="#9A3412"
            stroke-width="${hair * 1.2}"/>`);
        }
      }
    }

    const cal = result.scale?.calibration;
    if (S.layers.calibration && cal) {
      const a = px(cal.a), b = px(cal.b);
      const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1;
      const nx = (-dy / len) * 8, ny = (dx / len) * 8;
      out.push(`<path d="M${a[0]},${a[1]}L${b[0]},${b[1]}" stroke="#9A3412" fill="none"
        stroke-width="2" data-calibration-span="1"/>`);
      out.push(`<path d="M${a[0]+nx},${a[1]+ny}L${a[0]-nx},${a[1]-ny}
        M${b[0]+nx},${b[1]+ny}L${b[0]-nx},${b[1]-ny}" stroke="#9A3412" stroke-width="2"/>`);
      out.push(`<text x="${(a[0]+b[0])/2 + nx*2}" y="${(a[1]+b[1])/2 + ny*2}" fill="#9A3412"
        font-size="12" font-weight="600" text-anchor="middle" dominant-baseline="middle"
        style="paint-order:stroke" stroke="#fff" stroke-width="3"
        data-calibration-label="1">${esc(cal.label)}</text>`);
    }

    if (S.layers.warnings) {
      for (const amb of S.analysis?.ambiguities || []) {
        for (const region of amb.regions || []) {
          const b = region.bbox; if (!b) continue;
          const p0 = px([b.x0, b.y0]), p1 = px([b.x1, b.y1]);
          out.push(`<rect x="${Math.min(p0[0],p1[0])}" y="${Math.min(p0[1],p1[1])}"
            width="${Math.abs(p1[0]-p0[0])}" height="${Math.abs(p1[1]-p0[1])}"
            fill="rgba(169,113,27,.10)" stroke="#A9711B" stroke-width="1.6"
            stroke-dasharray="4 3"/>`);
        }
      }
    }
  }

  for (const [i, p] of S.picks.entries()) {
    const q = px(p);
    out.push(`<circle cx="${q[0]}" cy="${q[1]}" r="4.5" fill="#9A3412" stroke="#fff" stroke-width="1.5"/>`);
    out.push(`<text x="${q[0] + 8}" y="${q[1] - 6}" font-size="11" fill="#9A3412" font-weight="600">${i + 1}</text>`);
  }
  svg.innerHTML = out.join("");
}

/* ── rail ────────────────────────────────────────────────────────────────── */

function paintAll() {
  $("wsFile").textContent = S.fileName;
  $("wsKind").textContent = S.kind.toUpperCase();
  renderReading();
  renderFootprints();
  renderOverlayLayers();
  renderCalibration();
  renderWarnings();
  renderExplain();
  renderDetail();
  renderLayersPane();
  paintOverlay();
}

function renderReading() {
  const item = currentReading();
  const scale = S.result?.scale;
  if (!item) { $("rdName").textContent = t("ws.noGeometry"); return; }

  $("rdName").textContent = fpLabel(item.type, item.name);
  const a = areaText(item.units);
  $("rdValue").innerHTML = item.units
    ? `${a.value}<small>${a.unit}</small>`
    : `${esc(t("ws.notAvailable"))}<small>${esc(t("ws.scaleUnverified"))}</small>`;
  $("rdAlt").textContent = item.units ? altText(item.units) : "";
  $("rdMeans").textContent = item.means;

  const badges = [`<span class="badge ${item.semantics}">${t("sem." + item.semantics)}</span>`];
  if (scale?.operator_supplied && scale?.verified && scale?.calibration) {
    badges.push(`<span class="badge manual">${t("badge.operatorCalibrated")}</span>`);
  } else if (scale?.operator_supplied && scale?.verified) {
    badges.push(`<span class="badge manual">${t("badge.operatorUnits")}</span>`);
  } else if (scale?.source === "cad_declared_units") {
    badges.push(`<span class="badge confirmed">${t("badge.cadUnits")}</span>`);
  } else if (!scale?.verified) {
    badges.push(`<span class="badge warn">${t("scale.requiresConfirmation")}</span>`);
  }
  $("rdBadges").innerHTML = badges.join("");
  renderStatusGrid(item, scale);
  renderPrimaryAction(scale);
}

/* Four separate facts, shown as four.
 *
 * A single "Confidence 0 %" reads as "the analysis is worthless". On a drawing
 * whose scale is unknown that is wrong twice over: the geometry was extracted
 * perfectly, and the zero is the deliberate refusal to claim a physical area
 * without a scale (§3). Splitting them says what actually happened. */
function renderStatusGrid(item, scale) {
  const r = S.result, g = r?.geometry;
  if (!g) { $("rdStatus").innerHTML = ""; return; }

  const rows = [];
  /* A raster trace legitimately has no vector primitives at all, so success is
   * judged on what came out — reconstructed components — not on what went in. */
  const extracted = g.components > 0 && g.outer_contours > 0;
  rows.push([t("status.geometry"), extracted ? "ok" : "warn",
    extracted ? t("status.geometryOk") : t("status.geometryNone"),
    extracted ? t("status.geometrySub", {
      components: g.components.toLocaleString(), holes: g.holes.toLocaleString() }) : ""]);

  const scaleState = !scale?.verified ? "warn" : "ok";
  const scaleText = !scale?.verified
    ? t("scale.requiresConfirmation")
    : scale.operator_supplied
      ? (scale.calibration ? t("badge.operatorCalibrated") : t("badge.operatorUnits"))
      : t("scale.derived", { source: t("scaleSource." + scale.source, {}, scale.source) });
  rows.push([t("status.scale"), scaleState, scaleText,
    scale?.verified ? `${num(scale.mm_per_unit, 4)} mm/unit` : ""]);

  rows.push([t("status.meaning"), item?.provisional ? "warn" : "ok",
    t("sem." + (item?.semantics || "geometric")),
    item?.provisional ? t("status.meaningProvisionalSub") : t("status.meaningGeometricSub")]);

  const haveArea = Boolean(item?.units);
  rows.push([t("status.physicalArea"), haveArea ? "ok" : "none",
    haveArea ? `${areaText(item.units).value} ${areaText(item.units).unit}`
             : t("status.areaUnavailable"),
    haveArea && r.confidence
      ? t("status.confidenceIs", { pct: r.confidence.percent })
      : t("status.confidenceAfterCalibration")]);

  $("rdStatus").innerHTML = rows.map(([k, cls, v, sub]) =>
    `<div class="srow"><span class="sk">${esc(k)}</span>
      <span class="sv ${cls}">${esc(v)}${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</span>
    </div>`).join("");
}

/* Calibration is the one thing standing between this drawing and a number, so
 * it is offered right beside the result rather than inside a tab. */
function renderPrimaryAction(scale) {
  const host = $("rdPrimaryAction");
  if (!host) return;
  if (scale?.verified || S.picking) { host.innerHTML = ""; return; }

  const cadNeedsUnit = S.kind !== "pdf" && S.analysis?.cad_dimension_evidence
    && !S.analysis.cad_dimension_evidence.units_declared;
  host.innerHTML = `<div class="cta">
    <button class="primary" id="ctaCalibrate">${
      cadNeedsUnit ? esc(t("cta.stateUnits")) : esc(t("cta.calibrate"))}</button>
    <p class="hint">${esc(cadNeedsUnit ? t("cta.stateUnitsHint") : t("cta.calibrateHint"))}</p>
  </div>`;
  $("ctaCalibrate").onclick = () => {
    for (const tab of $("tabs").querySelectorAll("button")) {
      tab.setAttribute("aria-selected", tab.dataset.tab === "footprints");
    }
    for (const pane of document.querySelectorAll(".pane")) {
      pane.classList.toggle("on", pane.dataset.pane === "footprints");
    }
    if (cadNeedsUnit) {
      $("calibBlock").scrollIntoView({ block: "nearest" });
      $("cadUnit")?.focus();
    } else {
      startCalibration();
    }
  };
}

function renderFootprints() {
  const list = S.result?.footprint_interpretations || [];
  const current = currentReading();
  $("fpList").innerHTML = list.map((item) => {
    const a = areaText(item.units);
    const on = current && item.type === current.type;
    return `<button type="button" class="fp" data-fp="${esc(item.type)}" aria-pressed="${on}">
      <span class="row1">
        <span class="nm"><i class="sw" style="background:${fpColour(item.type)}"></i>${
          esc(fpLabel(item.type, item.name))}</span>
        <span class="val">${item.units ? `${a.value} ${a.unit}` : "—"}</span>
      </span>
      <span class="why">${esc(item.means)}</span>
      ${item.provisional ? `<span class="req">${esc(t("fp.provisionalTag"))}</span>` : ""}
    </button>`;
  }).join("") || `<p style="color:var(--ink-2)">${esc(t("fp.none"))}</p>`;

  for (const btn of $("fpList").querySelectorAll("button[data-fp]")) {
    btn.onclick = () => { S.fpType = btn.dataset.fp; renderReading(); renderFootprints();
                          renderExplain(); renderWarnings(); paintOverlay(); };
  }

  const pending = S.result?.pending_interpretations || [];
  $("fpPending").innerHTML = pending.map((item) => `
    <div class="fp unavailable" data-fp-pending="${esc(item.type)}">
      <span class="row1">
        <span class="nm">${esc(fpLabel(item.type, item.name))}</span>
        <span class="val">${esc(t("fp.notYetAvailable"))}</span>
      </span>
      <span class="why">${esc(item.means)}</span>
      <span class="req">${esc(t("fp.requires", { what: item.requires }))}</span>
    </div>`).join("");
}

function renderOverlayLayers() {
  $("overlayLayers").innerHTML = OVERLAY_LAYERS.map(([key, colour]) =>
    `<label><input type="checkbox" data-layer="${key}" ${S.layers[key] ? "checked" : ""}>
      <i style="background:${colour}"></i>${esc(t("overlay." + key))}</label>`).join("");
  for (const input of $("overlayLayers").querySelectorAll("input[data-layer]")) {
    input.onchange = () => { S.layers[input.dataset.layer] = input.checked; paintOverlay(); };
  }
}

/* ── calibration ─────────────────────────────────────────────────────────── */

function renderCalibration() {
  const scale = S.result?.scale;
  const host = $("calibBlock");
  if (scale?.verified && !S.picking) {
    if (scale.operator_supplied && !scale.calibration && S.kind !== "pdf") {
      host.innerHTML = `<div class="calib">
        <div class="badges"><span class="badge manual">${esc(t("badge.operatorUnits"))}</span></div>
        <div class="result" style="margin-top:8px">${
          esc(t("cadUnit.stated", { mm: num(scale.mm_per_unit, 4) }))}</div>
        <p style="margin:0;font-size:11.5px;color:var(--ink-2)">${esc(scale.detail || "")}</p>
        <button class="ghost" id="restateUnit" style="margin-top:8px">${
          esc(t("cadUnit.change"))}</button>
      </div>`;
      $("restateUnit").onclick = () => {
        S.scaleSpec = { mode: "auto" };
        computeArea().then(paintAll);
      };
      return;
    }
    if (scale.operator_supplied && scale.calibration) {
      const c = scale.calibration;
      host.innerHTML = `<div class="calib">
        <div class="badges"><span class="badge manual">${esc(t("badge.operatorCalibrated"))}</span></div>
        <div class="kv" style="margin-top:8px">
          <div class="r"><span class="k">${esc(t("cal.declaredDistance"))}</span>
            <span class="v">${esc(c.label)}</span></div>
          <div class="r"><span class="k">${esc(t("cal.measuredSpan"))}</span>
            <span class="v">${num(c.span_units, 2)}</span></div>
          <div class="r"><span class="k">${esc(t("cal.resultingScale"))}</span>
            <span class="v">${num(scale.mm_per_unit, 4)} mm/unit</span></div>
        </div>
        <p style="margin:7px 0 0;font-size:11.5px;color:var(--ink-2)">${
          esc(t("cal.drawnOnDrawing"))}<br>${esc(t("cal.notVerified"))}</p>
        <button class="ghost" id="recalibrate" style="margin-top:8px">${
          esc(t("cal.recalibrate"))}</button>
      </div>`;
      $("recalibrate").onclick = startCalibration;
    } else {
      host.innerHTML = "";
    }
    return;
  }

  /* A CAD drawing has exact coordinates and states its own dimensions; what it
   * may not say is which physical unit they are in. That is one question with a
   * short answer, so it is asked directly instead of sending the engineer off to
   * pick two points on a picture. */
  const cadEvidence = S.analysis?.cad_dimension_evidence;
  if (S.kind !== "pdf" && cadEvidence && !cadEvidence.units_declared) {
    const dims = (cadEvidence.measurements || []).slice(0, 6).map((v) => num(v, 1)).join("   ·   ");
    const units = ["mm", "cm", "m", "in", "ft"];
    host.innerHTML = `<div class="calib">
      <div class="badges"><span class="badge warn">${esc(t("cadUnit.title"))}</span></div>
      <p style="margin:8px 0 0;font-size:11.5px;color:var(--ink-2)">${esc(t("cadUnit.explain"))}</p>
      ${dims ? `<div class="result" style="margin-top:7px">${esc(dims)}</div>` : ""}
      <p style="margin:0 0 7px;font-size:11.5px;color:var(--ink-2)">${
        esc(t("cadUnit.extent", {
          w: cadEvidence.extent_units ? num(cadEvidence.extent_units[0], 0) : "—",
          h: cadEvidence.extent_units ? num(cadEvidence.extent_units[1], 0) : "—" }))}</p>
      <div class="fields">
        <select id="cadUnit">${units.map((u) =>
          `<option value="${u}">${esc(t("cadUnit." + u))}</option>`).join("")}</select>
        <button class="primary" id="applyCadUnit">${esc(t("cadUnit.apply"))}</button>
      </div>
      <p style="margin:0;font-size:11px;color:var(--ink-2)">${esc(t("cadUnit.note"))}</p>
    </div>`;
    $("applyCadUnit").onclick = async () => {
      S.scaleSpec = { mode: "cad_unit", known_unit: $("cadUnit").value };
      await computeArea();
      paintAll();
    };
    return;
  }

  const step = S.picking ? (S.picks.length < 2 ? S.picks.length + 1 : 3) : 0;
  host.innerHTML = `<div class="calib">
    <div class="badges"><span class="badge warn">${esc(t("scale.requiresConfirmation"))}</span></div>
    <p style="margin:8px 0 0;font-size:11.5px;color:var(--ink-2)">
      ${esc(scale?.detail || t("cal.noScaleDetail"))}</p>
    ${S.picking ? `
      <ol class="steps">
        <li class="${step === 1 ? "active" : ""}">${esc(t("cal.step1"))}</li>
        <li class="${step === 2 ? "active" : ""}">${esc(t("cal.step2"))}</li>
        <li class="${step === 3 ? "active" : ""}">${esc(t("cal.step3"))}</li>
      </ol>
      <p class="picked">${S.picks.map((p, i) =>
        `P${i + 1} ${num(p[0], 2)}, ${num(p[1], 2)}`).join("   ") || esc(t("cal.noPicks"))}</p>
      ${S.picks.length === 2 ? `<div class="result" id="calPreview"></div>` : ""}
      <div class="fields">
        <input type="number" id="knownLength" placeholder="${esc(t("cal.knownDistance"))}" step="any">
        <select id="knownUnit">
          <option value="mm">mm</option><option value="cm">cm</option>
          <option value="m">m</option><option value="in">in</option><option value="ft">ft</option>
        </select>
      </div>
      <button class="primary" id="applyCal" ${S.picks.length === 2 ? "" : "disabled"}
        style="width:100%">${esc(t("cal.apply"))}</button>
      <button class="ghost" id="cancelCal" style="width:100%;margin-top:5px">${
        esc(t("cal.cancel"))}</button>
    ` : `<button class="primary" id="startCal" style="width:100%;margin-top:9px">${
        esc(t("cta.calibrate"))}</button>`}
  </div>`;

  if (S.picking) {
    $("applyCal").onclick = applyCalibration;
    $("cancelCal").onclick = () => { S.picking = false; S.picks = [];
      $("canvasWrap").classList.remove("picking"); $("pickHint").classList.add("hide");
      renderCalibration(); paintOverlay(); };
    $("knownLength").oninput = updateCalPreview;
    $("knownUnit").onchange = updateCalPreview;
    updateCalPreview();
  } else {
    $("startCal").onclick = startCalibration;
  }
}

function startCalibration() {
  S.picking = true; S.picks = [];
  $("canvasWrap").classList.add("picking");
  $("pickHint").classList.remove("hide");
  renderCalibration(); paintOverlay();
}

/* Shown before applying so the operator can sanity-check the implied scale.
 * The authoritative value still comes back from the backend. */
function updateCalPreview() {
  const box = $("calPreview"); if (!box || S.picks.length !== 2) return;
  const [a, b] = S.picks;
  const span = Math.hypot(b[0] - a[0], b[1] - a[1]);
  const raw = parseFloat($("knownLength").value);
  const factor = { mm: 1, cm: 10, m: 1000, in: 25.4, ft: 304.8 }[$("knownUnit").value] || 1;
  box.innerHTML = esc(t("cal.span", { span: num(span, 2) })) +
    (raw > 0 ? `<br>${esc(t("cal.computed", { mm: num((raw * factor) / span, 4) }))}` : "");
}

async function applyCalibration() {
  const raw = parseFloat($("knownLength").value);
  if (!(raw > 0)) { $("knownLength").focus(); return; }
  S.scaleSpec = {
    mode: "two_point",
    points: [S.picks[0], S.picks[1]],
    known_length: raw,
    known_unit: $("knownUnit").value,
  };
  S.picking = false;
  $("canvasWrap").classList.remove("picking");
  $("pickHint").classList.add("hide");
  await computeArea();
  paintAll();
}

/* ── area ────────────────────────────────────────────────────────────────── */

async function computeArea(opts = {}) {
  const region = S.analysis?.default_region_id;
  const body = { scale: S.scaleSpec };
  if (region) body.region_id = region;
  S.result = await call(`/api/documents/${S.docId}/pages/${S.page}/area`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (S.kind === "dxf" && !S.cadBox) {
    const b = S.result.region?.bbox || null;
    S.cadBox = b || null;
  }
  if (!opts.silent) { renderReading(); renderFootprints(); }
}

/* ── warnings, explain, detail, layers ───────────────────────────────────── */

/* Warnings the UI composes from structured backend state. Each is a catalogue
 * key with a "why it matters" line, so both languages read naturally instead of
 * being a translated sentence fragment.
 *
 * Warnings that the *engine* produces as free text stay in the language the
 * engine wrote them: they are the engineering record, shared verbatim by the
 * JSON API, the CLI and this viewer, and rewording them here would mean the
 * report and the screen no longer say the same thing. */
const UI_WARNING_WHY = {
  "warn.noScale": "warn.noScaleWhy",
  "warn.operatorScale": "warn.operatorScaleWhy",
  "warn.noPdfText": "warn.noPdfTextWhy",
  "warn.strokedAnnotation": "warn.strokedAnnotationWhy",
  "warn.provisionalMeaning": "warn.provisionalMeaningWhy",
};

function renderWarnings() {
  const result = S.result, item = currentReading();
  if (!result) { $("warningsPane").innerHTML = ""; return; }

  /* {key, params} for warnings this UI raises; plain strings for the engine's
   * own, which are shown exactly as it wrote them. */
  const entries = [];
  if (!result.scale?.verified) {
    entries.push({ key: "warn.noScale" });
  } else if (result.scale.operator_supplied) {
    entries.push({ key: "warn.operatorScale" });
  }
  /* The engine pairs its common warnings with a stable code; those are shown in
   * the reader's language. Anything uncoded is a finding written by the engine
   * and is shown in its own words, so the screen and the JSON report agree. */
  const coded = result.warnings_coded || (result.warnings || []).map((w) => ({ text: w }));
  for (const w of coded) {
    entries.push(w.code ? { key: "msg." + w.code, text: w.text } : { text: w.text });
  }
  const seen = new Set(coded.map((w) => w.text));
  for (const w of item?.warnings || []) if (!seen.has(w)) entries.push({ text: w });
  if (S.kind === "pdf" && S.analysis && S.analysis.text_span_count === 0) {
    entries.push({ key: "warn.noPdfText" });
    const excluded = result.geometry.raw_primitives
      ? result.geometry.ignored_primitives / result.geometry.raw_primitives : 0;
    if (excluded < 0.2) entries.push({ key: "warn.strokedAnnotation" });
  }
  if (item?.provisional) {
    entries.push({ key: "warn.provisionalMeaning",
                   params: { name: fpLabel(item.type, item.name) } });
  }
  for (const amb of S.analysis?.ambiguities || []) {
    entries.push({ text: `${amb.headline}: ${amb.reason}` });
  }

  $("warnCount").textContent = entries.length;
  $("warnCount").classList.toggle("hide", entries.length === 0);

  const assumptions = [...(result.assumptions || []), ...(item?.assumptions || [])]
    .filter((v, i, a) => a.indexOf(v) === i);

  const items = entries.map((entry) => {
    /* A coded entry falls back to the engine's own wording if the catalogue
     * has no translation for it yet. */
    const text = entry.key
      ? t(entry.key, entry.params || {}, entry.text ? entry.text.split("\n")[0] : null)
      : entry.text.split("\n")[0];
    const whyKey = entry.key ? UI_WARNING_WHY[entry.key] : null;
    const why = whyKey ? t(whyKey) : null;
    return `<li>${esc(text)}${why ? `<span class="why">${esc(why)}</span>` : ""}</li>`;
  }).join("");

  $("warningsPane").innerHTML = (entries.length ? `
    <div class="review">
      <h4>${esc(t("warn.reviewRequired"))}</h4>
      <ul>${items}</ul>
    </div>` : `<p style="color:var(--ink-2)">${esc(t("warn.none"))}</p>`) +
    (assumptions.length ? `<h4>${esc(t("warn.assumptions"))}</h4>
      <ul class="assumptions">${assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : "");
}

function renderExplain() {
  const r = S.result, item = currentReading();
  if (!r) { $("explainFlow").innerHTML = ""; return; }
  const g = r.geometry, scale = r.scale;
  const a = areaText(item?.units);

  const scaleLine = scale?.verified
    ? `${scale.operator_supplied
          ? (scale.calibration ? t("explain.operatorCalibrated") : t("badge.operatorUnits"))
          : scale.source === "cad_declared_units" ? t("explain.cadUnits") : t("explain.derived")}<br>
       ${scale.calibration
         ? `<span class="mono">${esc(scale.calibration.label)} / ${
              num(scale.calibration.span_units, 2)}</span><br>` : ""}
       <span class="mono">= ${num(scale.mm_per_unit, 4)} mm/unit</span>
       ${(scale.evidence || []).length
         ? `<ul>${scale.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}`
    : t("explain.scaleNotVerified");

  const steps = [
    [t("explain.source"), `${esc(S.fileName)}<br><span class="mono">${S.kind.toUpperCase()}${
      S.cad ? ` · ${esc(S.cad.dxf_version)}` : ""}</span>`],
    [t("explain.drawing"), S.kind === "pdf" ? t("explain.page", { n: r.page }) : t("explain.modelSpace")],
    [t("explain.scale"), scaleLine],
    [t("explain.geometry"), `<span class="mono">${t("explain.geometryLine", {
        total: g.raw_primitives.toLocaleString(),
        counted: g.profile_primitives.toLocaleString(),
        excluded: g.ignored_primitives.toLocaleString() })}</span>`],
    [t("explain.interpretation"), item
      ? `${esc(fpLabel(item.type, item.name))}<br>${esc(t("explain.semanticStatus"))}:
         <b>${esc(t("sem." + item.semantics))}</b>
         ${(item.evidence || []).length
           ? `<ul>${item.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}`
      : "—"],
    [t("explain.area"), `${esc(r.method.replace(/_/g, " "))}<br>${t("explain.areaLine", {
        outer: g.outer_contours, holes: g.holes,
        holeAction: r.subtract_holes ? t("explain.holesSubtracted") : t("explain.holesKept") })}`],
    [t("explain.result"), item?.units
      ? `<span class="mono" style="font-size:14px"><b>${a.value} ${a.unit}</b></span><br>
         <span class="mono">${esc(altText(item.units))}</span>`
      : t("explain.resultUnavailable")],
  ];

  $("explainFlow").innerHTML = steps.map(([k, v], i) =>
    `${i ? `<div class="arrow">↓</div>` : ""}
     <div class="step"><div class="sk">${esc(k)}</div><div class="sv">${v}</div></div>`).join("");
}

function renderDetail() {
  const r = S.result, a = S.analysis;
  if (!r) { $("detailPane").innerHTML = ""; return; }
  const g = r.geometry, conv = S.conversion;

  const rows = [
    [t("detail.method"), r.method.replace(/_/g, " ")],
    [t("detail.source"), S.kind.toUpperCase()],
  ];
  if (conv) {
    rows.push(
      [t("detail.dwgVersion"), `${conv.dwg_signature} · ${conv.dwg_version}`],
      [t("detail.processingPath"), conv.path],
      [t("detail.conversion"), t("detail.conversionOk", {
        tool: conv.tool, version: conv.tool_version, seconds: conv.duration_seconds })],
      [t("detail.converterWarnings"), String(conv.warnings.length)],
      [t("detail.sourceHash"), conv.source_sha256.slice(0, 16) + "…"],
      [t("detail.intermediateDxf"), `${(conv.intermediate_dxf_bytes / 1e6).toFixed(1)} MB · ` +
        conv.intermediate_dxf_sha256.slice(0, 16) + "…"],
    );
  }
  if (S.cad) {
    rows.push([t("detail.cadUnits"), S.cad.units.declared
      ? t("detail.cadUnitsDeclared", { name: S.cad.units.name, n: S.cad.units.insunits })
      : t("detail.cadUnitsUndeclared", { n: S.cad.units.insunits })]);
  }
  rows.push(
    [t("detail.region"), `${r.view.label} (${r.view.source.replace(/_/g, " ")})`],
    [t("detail.rotation"), a ? `${a.rotation}°` : "—"],
    [t("detail.extent"), a ? `${a.width_pt} × ${a.height_pt}` : "—"],
    [t("detail.primitives"), g.raw_primitives.toLocaleString()],
    [t("detail.counted"), g.profile_primitives.toLocaleString()],
    [t("detail.excluded"), g.ignored_primitives.toLocaleString()],
    [t("detail.segments"), g.segments.toLocaleString()],
    [t("detail.faces"), g.faces.toLocaleString()],
    [t("detail.components"), g.components.toLocaleString()],
    [t("detail.outerRings"), String(g.outer_contours)],
    [t("detail.holes"), String(g.holes)],
    [t("detail.scaleSource"), t("scaleSource." + r.scale.source, {}, r.scale.source)],
    [t("detail.mmPerUnit"), r.scale.verified ? num(r.scale.mm_per_unit, 6) : "—"],
    [t("detail.impliedRatio"), r.scale.implied_ratio || "—"],
    [t("detail.engine"), `${r.engine_version} · ${r.timestamp}`],
  );

  const roles = Object.entries(g.role_counts || {})
    .sort((x, y) => y[1] - x[1])
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v.toLocaleString()}</td></tr>`).join("");
  const repairs = (g.repairs || []).filter((x) => x.count > 0);

  $("detailPane").innerHTML =
    `<div class="kv">${rows.map(([k, v]) =>
      `<div class="r"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")}</div>
     <h4>${esc(t("detail.classification"))}</h4>
     <table class="grid"><thead><tr><th>${esc(t("detail.role"))}</th>
       <th style="text-align:right">${esc(t("detail.count"))}</th></tr></thead>
       <tbody>${roles}</tbody></table>` +
    (repairs.length ? `<h4>${esc(t("detail.repairs"))}</h4><table class="grid"><tbody>${
      repairs.map((x) => `<tr><td>${esc(x.type)}</td>
        <td class="n">${x.count.toLocaleString()}</td></tr>`).join("")}</tbody></table>` : "");
}

const ACI = { 1: "#FF0000", 2: "#FFFF00", 3: "#00FF00", 4: "#00FFFF", 5: "#0000FF",
              6: "#FF00FF", 7: "#333333", 8: "#808080", 9: "#C0C0C0" };

function renderLayersPane() {
  /* Any CAD source has layers — a DXF read directly, or a DWG converted
   * locally. Only a PDF has none. */
  if (!S.cad) {
    $("layersPane").innerHTML =
      `<p style="color:var(--ink-2)">${esc(t("layers.noneForPdf"))}</p>`;
    return;
  }
  const layers = (S.cad.layers || []).filter((l) => l.entity_count > 0);
  const blocks = (S.cad.blocks || []).filter((b) => b.insert_count > 0);

  /* Layer and block names are engineering identifiers and are never translated
   * — they are how a drafter finds them in AutoCAD. */
  $("layersPane").innerHTML = `
    <p style="color:var(--ink-2);font-size:11.5px;margin:0 0 9px">${esc(t("layers.verbatim"))}</p>
    <table class="grid"><thead><tr>
      <th>${esc(t("layers.show"))}</th><th>${esc(t("layers.layer"))}</th>
      <th>${esc(t("layers.linetype"))}</th>
      <th style="text-align:right">${esc(t("layers.entities"))}</th>
    </tr></thead><tbody>${layers.map((l) => `
      <tr class="${S.hiddenLayers.has(l.name) ? "off" : ""}" data-layer-row="${esc(l.name)}">
        <td><input type="checkbox" data-cad-layer="${esc(l.name)}"
          ${S.hiddenLayers.has(l.name) ? "" : "checked"}></td>
        <td><span class="sw" style="background:${ACI[l.color] || "#55636A"}"></span>${esc(l.name)}</td>
        <td>${esc(l.linetype || "—")}</td>
        <td class="n">${l.entity_count.toLocaleString()}</td>
      </tr>`).join("")}</tbody></table>
    ${blocks.length ? `<h4>${esc(t("layers.blocks"))}</h4>
      <table class="grid"><thead><tr><th>${esc(t("layers.block"))}</th>
        <th>${esc(t("layers.xref"))}</th>
        <th style="text-align:right">${esc(t("layers.entities"))}</th>
        <th style="text-align:right">${esc(t("layers.placed"))}</th></tr></thead>
      <tbody>${blocks.map((b) => `<tr><td>${esc(b.name)}</td><td>${b.is_xref ? "✓" : ""}</td>
        <td class="n">${b.entity_count.toLocaleString()}</td>
        <td class="n">${b.insert_count.toLocaleString()}</td></tr>`).join("")}</tbody></table>` : ""}
    <h4>${esc(t("layers.units"))}</h4>
    <div class="kv"><div class="r"><span class="k">${esc(t("layers.insunits"))}</span>
      <span class="v">${S.cad.units.insunits} (${esc(S.cad.units.name)})</span></div>
      <div class="r"><span class="k">${esc(t("layers.mmPerUnit"))}</span>
      <span class="v">${S.cad.units.mm_per_unit ?? "—"}</span></div></div>`;

  for (const input of $("layersPane").querySelectorAll("input[data-cad-layer]")) {
    input.onchange = () => {
      const name = input.dataset.cadLayer;
      if (input.checked) S.hiddenLayers.delete(name); else S.hiddenLayers.add(name);
      renderLayersPane();
      if (S.kind !== "pdf") renderCad();
      paintOverlay();
    };
  }
}

/* ── export ──────────────────────────────────────────────────────────────── */

function download(name, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click();
  a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportJson() {
  download(`${S.fileName.replace(/\.[^.]+$/, "")}-projected-area.json`,
    JSON.stringify({ file: S.fileName, source: S.kind, analysis: S.analysis, result: S.result }, null, 2),
    "application/json");
}

function exportCsv() {
  const r = S.result; if (!r) return;
  const rows = [["reading", "semantics", "area_m2", "area_ft2", "area_mm2", "scale_mm_per_unit",
                 "scale_source", "operator_supplied", "confidence_pct", "source", "file"]];
  for (const item of r.footprint_interpretations) {
    rows.push([item.type, item.semantics,
      item.units ? item.units.m2 : "", item.units ? item.units.ft2 : "",
      item.units ? item.units.mm2 : "",
      r.scale.verified ? r.scale.mm_per_unit : "", r.scale.source,
      r.scale.operator_supplied, r.confidence.percent, S.kind, S.fileName]);
  }
  download(`${S.fileName.replace(/\.[^.]+$/, "")}-projected-area.csv`,
    rows.map((row) => row.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n"),
    "text/csv");
}

/* ── viewer controls ─────────────────────────────────────────────────────── */

async function setZoom(z) {
  S.zoom = Math.min(8, Math.max(0.05, z));
  $("zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
  if (S.kind === "pdf") await paintPage(); else renderCad();
  paintOverlay();
}
function fitToWindow() {
  const wrap = $("canvasWrap");
  if (S.kind === "pdf" && S.pdfPage) {
    const v = S.pdfPage.getViewport({ scale: 1 });
    S.fitZoom = Math.min((wrap.clientWidth - 48) / v.width, (wrap.clientHeight - 48) / v.height);
  } else {
    S.fitZoom = Math.min((wrap.clientWidth - 48) / 1000, (wrap.clientHeight - 48) / 700);
  }
  setZoom(S.fitZoom || 1);
}

/* ── wiring ──────────────────────────────────────────────────────────────── */

$("browseBtn").onclick = () => $("fileInput").click();
$("fileInput").onchange = (e) => { if (e.target.files[0]) handleFile(e.target.files[0]); };

const dz = $("dropzone");
for (const type of ["dragenter", "dragover"]) {
  dz.addEventListener(type, (e) => { e.preventDefault(); dz.classList.add("over"); });
}
for (const type of ["dragleave", "drop"]) {
  dz.addEventListener(type, (e) => { e.preventDefault(); dz.classList.remove("over"); });
}
dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });

$("procBack").onclick = () => show("landing");
$("newFileBtn").onclick = () => {
  if (S.docId) fetch(`${API}/api/documents/${S.docId}`, { method: "DELETE" }).catch(() => {});
  S.docId = null; S.result = null; S.analysis = null; S.cad = null; S.cadBox = null;
  S.picks = []; S.picking = false; show("landing");
};
$("exportJsonBtn").onclick = exportJson;
$("exportCsvBtn").onclick = exportCsv;

$("zoomIn").onclick = () => setZoom(S.zoom * 1.25);
$("zoomOut").onclick = () => setZoom(S.zoom / 1.25);
$("oneToOne").onclick = () => setZoom(1);
$("fitBtn").onclick = fitToWindow;
$("resetView").onclick = () => { fitToWindow(); $("canvasWrap").scrollTo(0, 0); };
$("fullscreenBtn").onclick = () => {
  const el = document.querySelector(".viewer");
  if (document.fullscreenElement) document.exitFullscreen();
  else el.requestFullscreen?.();
};

$("pageCanvas").addEventListener("click", (e) => {
  if (!S.picking) return;
  const pt = toPage(e.clientX, e.clientY);
  if (S.picks.length >= 2) S.picks = [];
  S.picks.push(pt);
  renderCalibration();
  paintOverlay();
});

for (const tab of $("tabs").querySelectorAll("button[data-tab]")) {
  tab.onclick = () => {
    for (const t of $("tabs").querySelectorAll("button")) t.setAttribute("aria-selected", t === tab);
    for (const p of document.querySelectorAll(".pane")) {
      p.classList.toggle("on", p.dataset.pane === tab.dataset.tab);
    }
  };
}

/* Say honestly which formats this installation can read. DWG needs a local
 * converter, so the landing screen reflects whether it is actually present. */
/* The landing screen states which formats this installation can read. DWG needs
 * a local converter, so it says so rather than assuming. */
function renderCapabilities() {
  const caps = S.capabilities, line = $("formatLine");
  if (!caps || !line) return;
  if (!caps.dwg.supported) {
    line.textContent = t("drop.formatsDwgSetup");
    line.title = caps.dwg.reason || "";
    $("uploadNotice").innerHTML = `<div class="notice">
      <h4>${esc(t("dwg.notInstalledTitle"))}</h4>
      <p>${esc(t("dwg.notInstalledBody"))}</p>
      <p class="fix">${esc(caps.dwg.advice === "deploy_image"
        ? t("dwg.deployImage")
        : t("dwg.runCommand", { command: caps.dwg.setup_command || "" }))}</p>
    </div>`;
  } else {
    line.textContent = t("drop.formats");
    line.title = caps.dwg.note || "";
    $("uploadNotice").innerHTML = "";
  }
}

/* Reference drawings, generated and measured by the real backend. Their titles
 * come from the backend catalogue and stay in its language — they name the
 * fixtures, not the interface. */
function renderDemoList() {
  if (!S.demos) return;
  $("demoGrid").innerHTML = S.demos.slice(0, 4).map((d) =>
    `<button type="button" data-demo="${esc(d.id)}">
      <span class="t">${esc(d.title)}</span>
      <span class="d">${esc(d.exercises)}</span>
    </button>`).join("");
  for (const btn of $("demoGrid").querySelectorAll("button[data-demo]")) {
    btn.onclick = () => openDemo(btn.dataset.demo);
  }
}

(async function bootstrap() {
  let stored = null;
  try { stored = localStorage.getItem(LANG_KEY); } catch (e) { /* private window */ }
  const preferred = stored || (navigator.language || "").toLowerCase().startsWith("zh")
    ? (stored || "zh-CN") : "en";
  await loadCatalogue("en");
  await setLanguage(LANGS[preferred] ? preferred : "en", { rerender: false });

  for (const btn of document.querySelectorAll("[data-lang]")) {
    btn.onclick = () => setLanguage(btn.dataset.lang);
  }

  try {
    S.capabilities = (await call("/api/capabilities")).formats;
    renderCapabilities();
  } catch (e) { /* the landing screen still works without this */ }

  try {
    S.demos = (await call("/api/demo")).drawings || [];
    renderDemoList();
  } catch (e) {
    $("demoRow").classList.add("hide");
  }
})();

async function openDemo(id) {
  const doc = await call(`/api/demo/${id}`, { method: "POST" });
  const bytes = await fetch(`${API}/api/documents/${doc.document_id}/file`);
  const blob = await bytes.blob();
  await fetch(`${API}/api/documents/${doc.document_id}`, { method: "DELETE" }).catch(() => {});
  await handleFile(new File([blob], doc.demo.file_name, { type: "application/pdf" }));
}

window.addEventListener("resize", () => { if (!$("workspace").classList.contains("hide")) paintOverlay(); });
window.__state = S;   // for browser tests
