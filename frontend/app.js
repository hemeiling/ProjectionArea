/* Projected Area Analyzer — front end.
 *
 * Every number shown here comes from the backend. This file renders state and
 * collects input; it never computes an area, a scale or a unit conversion. When
 * something needs explaining, it prints the evidence the engine already
 * produced (FootprintInterpretation.evidence, Scale.calibration, warnings,
 * assumptions) rather than re-deriving a second opinion in JavaScript.
 */

const API = location.origin;

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
const STAGES_PDF = [
  ["load", "File loaded"],
  ["geometry", "Geometry extracted"],
  ["regions", "Drawing regions identified"],
  ["scale", "Scale / units evaluated"],
  ["candidates", "Footprint candidates created"],
  ["area", "Area calculated"],
];
const STAGES_CAD = [
  ["load", "DWG validated"],
  ["convert", "CAD conversion completed"],
  ["geometry", "Geometry extracted"],
  ["scale", "CAD units detected"],
  ["regions", "Layers / blocks analysed"],
  ["candidates", "Footprint candidates created"],
  ["area", "Area calculated"],
];
let STAGES = STAGES_PDF;
const MARKS = {
  pending: "·", active: "…", done: "✓", warn: "⚠", input: "⚠",
  unsupported: "—", failed: "✕",
};

function renderTimeline(states) {
  $("timeline").innerHTML = STAGES.map(([key, label]) => {
    const st = states[key] || { state: "pending" };
    return `<div class="stage" data-stage="${key}" data-state="${st.state}">
      <span class="mark">${MARKS[st.state] || "·"}</span>
      <span class="label">${esc(label)}</span>
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

async function handleFile(file) {
  $("uploadNotice").innerHTML = "";
  show("processing");
  $("procFile").textContent = file.name;
  $("procTitle").textContent = "Processing drawing";
  $("procNotice").innerHTML = "";

  /* The extension is only a hint; the backend decides by signature. Picking the
   * timeline up front means a DWG reads "DWG validated" from the first tick. */
  STAGES = /\.(dwg|dxf)$/i.test(file.name) ? STAGES_CAD : STAGES_PDF;
  if (/\.dwg$/i.test(file.name)) $("procTitle").textContent = "Converting and analysing DWG";

  const states = {};
  STAGES.forEach(([k]) => (states[k] = { state: "pending" }));
  states.load = { state: "active" };
  renderTimeline(states);

  let doc;
  try {
    const body = new FormData();
    body.append("file", file);
    doc = await call("/api/documents", { method: "POST", body });

    /* A DWG is converted and parsed on a worker thread — the largest real
     * drawing takes minutes — so the upload hands back a job and the timeline
     * follows it rather than the browser sitting on an open request. */
    if (doc.job_id) doc = await followJob(doc, states);
  } catch (err) {
    const d = err.detail;
    const missing = d?.kind === "dwg_component_missing";
    const conversionFailed = d?.kind === "conversion_failed";
    const emptyDrawing = d?.kind === "empty_drawing";
    /* A DWG that reached conversion was a valid DWG: the file is the problem,
     * or the component is, and those are different things to tell someone. */
    states.load = (missing || conversionFailed || emptyDrawing)
      ? { state: "done", note: `valid DWG ${d.version || ""}`.trim() }
      : { state: "failed", note: "unreadable" };
    for (const [k] of STAGES.slice(1)) states[k] = { state: "unsupported" };
    if (states.convert) {
      if (missing) states.convert = { state: "input", note: "component required" };
      else if (conversionFailed) states.convert = { state: "failed", note: "could not be converted" };
      else if (emptyDrawing) states.convert = { state: "done", note: "converted, but empty" };
    }
    renderTimeline(states);
    $("procTitle").textContent =
      missing ? "DWG support is not installed yet"
      : conversionFailed ? "DWG could not be converted"
      : emptyDrawing ? "Drawing contains no geometry"
      : "Cannot read this file";
    notice("procNotice", {
      headline: d?.headline || err.message,
      body: d?.reason,
      fix: d?.fix,
      kind: missing ? "warn" : "error",
    });
    return;
  }

  S.docId = doc.document_id;
  S.fileName = doc.file_name;
  S.kind = doc.source_kind || "pdf";
  S.cad = doc.cad || null;
  S.conversion = (doc.cad && doc.cad.conversion) || null;
  S.page = doc.suggested_page || 1;
  S.scaleSpec = { mode: "auto" };
  S.fpType = null;
  S.picks = [];

  /* The backend decides the kind by signature, so re-pick the timeline in case
   * the extension lied, and fill in the conversion row for a DWG. */
  const wantCad = S.kind !== "pdf";
  if ((STAGES === STAGES_CAD) !== wantCad) {
    STAGES = wantCad ? STAGES_CAD : STAGES_PDF;
    for (const [k] of STAGES) if (!states[k]) states[k] = { state: "pending" };
  }
  states.load = S.conversion
    ? { state: "done", note: `${S.conversion.dwg_signature} · ${S.conversion.dwg_version}` }
    : { state: "done", note: `${S.kind.toUpperCase()} · ${doc.page_count} page(s)` };
  if (states.convert) {
    states.convert = S.conversion
      ? { state: S.conversion.warnings.length ? "warn" : "done",
          note: `${S.conversion.tool} ${S.conversion.tool_version} · ` +
                `${S.conversion.duration_seconds}s · ` +
                `${(S.conversion.intermediate_dxf_bytes / 1e6).toFixed(0)} MB DXF` }
      : { state: "done", note: "read directly" };
  }
  states.geometry = { state: "active" };
  renderTimeline(states);

  // Render the drawing while the analysis runs. A CAD source has no page image,
  // so its own linework is stroked onto the canvas once the geometry arrives.
  const renderTask = S.kind === "pdf" ? renderPdf(file) : Promise.resolve();

  try {
    const analysis = await call(`/api/documents/${S.docId}/pages/${S.page}/analyze`);
    S.analysis = analysis;
    states.geometry = { state: "done", note: `${analysis.primitive_count.toLocaleString()} primitives` };

    const views = analysis.regions.filter((r) => r.kind === "view");
    states.regions = {
      state: "done",
      note: `${analysis.regions.length} region(s), ${views.length} view(s)`,
    };
    if ((analysis.ambiguities || []).length) {
      states.regions = { state: "warn", note: analysis.ambiguities[0].headline };
    }
    renderTimeline(states);

    const roles = "dimension,annotation,centerline,hidden,sheet,hatch,uncertain";
    S.ignored = (await call(
      `/api/documents/${S.docId}/pages/${S.page}/geometry?roles=${roles}&max_primitives=8000`
    )).primitives || [];

    states.scale = analysis.scale?.verified
      ? { state: "done", note: `${analysis.scale.source.replace(/_/g, " ")}` }
      : { state: "input", note: S.kind === "pdf"
          ? "Scale requires confirmation" : "Units not declared by the drawing" };
    if (S.kind !== "pdf" && S.cad) {
      const layers = (S.cad.layers || []).filter((l) => l.entity_count).length;
      const blocks = (S.cad.blocks || []).filter((b) => b.insert_count).length;
      states.regions = { state: "done", note: `${layers} layer(s), ${blocks} block(s)` };
    }
    states.candidates = { state: "active" };
    renderTimeline(states);

    await computeArea({ silent: true });
    const verified = S.result?.scale?.verified;
    states.candidates = {
      state: "done",
      note: `${S.result.footprint_interpretations.length} reading(s)`,
    };
    states.area = verified
      ? { state: "done", note: areaText(readingUnits(currentReading())).value + " " +
                               areaText(readingUnits(currentReading())).unit }
      : { state: "input", note: "Calibrate to obtain a physical area" };
    renderTimeline(states);

    await renderTask;
    setTimeout(() => {
      show("workspace");
      if (S.kind !== "pdf") { S.cadBox = cadExtent(); renderCad(); }
      paintAll();
      fitToWindow();
    }, 380);
  } catch (err) {
    states.area = { state: "failed", note: err.message };
    renderTimeline(states);
    notice("procNotice", { headline: "Analysis failed", body: err.message, kind: "error" });
  }
}

/* Job stages, mapped onto the timeline rows the user is watching. */
const JOB_STAGE_ROWS = {
  validated: ["load", "done"],
  converting: ["convert", "active"],
  converted: ["convert", "done"],
  reading: ["geometry", "active"],
  read: ["geometry", "done"],
  analysed: ["regions", "done"],
  complete: ["regions", "done"],
};

async function followJob(job, states) {
  const applyStage = (stage, detail) => {
    const row = JOB_STAGE_ROWS[stage];
    if (!row) return;
    const [key, state] = row;
    states[key] = { state, note: detail || "" };
    if (state === "done" || state === "active") {
      // Everything before a reached stage is necessarily finished.
      const order = STAGES.map(([k]) => k);
      for (const earlier of order.slice(0, order.indexOf(key))) {
        if (states[earlier]?.state === "pending") states[earlier] = { state: "done" };
      }
    }
    renderTimeline(states);
  };

  while (true) {
    const status = await call(`/api/jobs/${job.job_id}`);
    for (const done of status.stages_done) applyStage(done.stage, done.detail);
    applyStage(status.stage, status.detail);

    if (status.state === "failed") {
      const err = new Error(status.error?.headline || "Processing failed");
      err.detail = status.error;
      throw err;
    }
    if (status.state === "done") return status.result;
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
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

const FP_STYLE = {
  geometry_union: { colour: "#1F6F63", label: "Geometry Union" },
  enclosing_boundary: { colour: "#A9711B", label: "Enclosing Boundary" },
  internal_union: { colour: "#2F8E7E", label: "Internal Union" },
  convex_envelope: { colour: "#3E6392", label: "Convex Envelope" },
  bounding_rectangle: { colour: "#6C5A93", label: "Bounding Rectangle" },
  conveyor_footprint: { colour: "#55636A", label: "Conveyor Footprint" },
  guarded_area: { colour: "#55636A", label: "Guarded Area" },
  line_footprint: { colour: "#55636A", label: "Production Line Footprint" },
};
const OVERLAY_LAYERS = [
  ["source", "Source geometry", "rgba(85,99,106,.34)"],
  ["counted", "Counted geometry", "rgba(31,111,99,.55)"],
  ["excluded", "Excluded geometry", "rgba(85,99,106,.55)"],
  ["dimensions", "Dimensions / annotation", "rgba(85,99,106,.25)"],
  ["holes", "Holes", "rgba(154,52,18,.55)"],
  ["footprint", "Selected footprint", "rgba(31,111,99,.32)"],
  ["boundary", "Enclosing boundary", "rgba(169,113,27,.45)"],
  ["internal", "Internal geometry", "rgba(47,142,126,.45)"],
  ["envelope", "Convex envelope", "rgba(62,99,146,.45)"],
  ["rect", "Bounding rectangle", "rgba(108,90,147,.45)"],
  ["calibration", "Calibration span", "rgba(154,52,18,.85)"],
  ["warnings", "Ambiguous regions", "rgba(169,113,27,.55)"],
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
        out.push(`<path d="${ring(r)}" fill="none" stroke="${FP_STYLE[type].colour}"
          stroke-width="${hair * 1.3}" stroke-dasharray="5 3.5" stroke-opacity=".95"/>`);
      }
    }

    const sel = currentReading();
    if (S.layers.footprint && sel && (sel.outer || []).length) {
      const tint = FP_STYLE[sel.type]?.colour || "#1F6F63";
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
  if (!item) { $("rdName").textContent = "No geometry reconstructed"; return; }

  $("rdName").textContent = FP_STYLE[item.type]?.label || item.name;
  const a = areaText(item.units);
  $("rdValue").innerHTML = item.units
    ? `${a.value}<small>${a.unit}</small>`
    : `Not available<small>scale unverified</small>`;
  $("rdAlt").textContent = item.units ? altText(item.units) : "";
  $("rdMeans").textContent = item.means;

  const badges = [];
  badges.push(`<span class="badge ${item.semantics}">${item.semantics}</span>`);
  if (scale?.operator_supplied && scale?.verified && scale?.calibration) {
    badges.push(`<span class="badge manual">Operator calibrated</span>`);
  } else if (scale?.operator_supplied && scale?.verified) {
    badges.push(`<span class="badge manual">Operator-stated units</span>`);
  } else if (scale?.source === "cad_declared_units") {
    badges.push(`<span class="badge confirmed">CAD $INSUNITS</span>`);
  } else if (!scale?.verified) {
    badges.push(`<span class="badge warn">Scale requires confirmation</span>`);
  }
  if (S.result?.confidence) {
    badges.push(`<span class="badge">Confidence ${S.result.confidence.percent}%</span>`);
  }
  $("rdBadges").innerHTML = badges.join("");
}

function renderFootprints() {
  const list = S.result?.footprint_interpretations || [];
  const current = currentReading();
  $("fpList").innerHTML = list.map((item) => {
    const style = FP_STYLE[item.type] || {};
    const a = areaText(item.units);
    const on = current && item.type === current.type;
    return `<button type="button" class="fp" data-fp="${esc(item.type)}" aria-pressed="${on}">
      <span class="row1">
        <span class="nm"><i class="sw" style="background:${style.colour || "#1F6F63"}"></i>${esc(style.label || item.name)}</span>
        <span class="val">${item.units ? `${a.value} ${a.unit}` : "—"}</span>
      </span>
      <span class="why">${esc(item.means)}</span>
      ${item.provisional ? `<span class="req">Provisional — meaning not confirmed</span>` : ""}
    </button>`;
  }).join("") || `<p style="color:var(--ink-2)">No readings — nothing was reconstructed.</p>`;

  for (const btn of $("fpList").querySelectorAll("button[data-fp]")) {
    btn.onclick = () => { S.fpType = btn.dataset.fp; renderReading(); renderFootprints();
                          renderExplain(); renderWarnings(); paintOverlay(); };
  }

  const pending = S.result?.pending_interpretations || [];
  $("fpPending").innerHTML = pending.map((item) => `
    <div class="fp unavailable" data-fp-pending="${esc(item.type)}">
      <span class="row1">
        <span class="nm">${esc(FP_STYLE[item.type]?.label || item.name)}</span>
        <span class="val">Not yet available</span>
      </span>
      <span class="why">${esc(item.means)}</span>
      <span class="req">Requires ${esc(item.requires)}</span>
    </div>`).join("");
}

function renderOverlayLayers() {
  $("overlayLayers").innerHTML = OVERLAY_LAYERS.map(([key, label, colour]) =>
    `<label><input type="checkbox" data-layer="${key}" ${S.layers[key] ? "checked" : ""}>
      <i style="background:${colour}"></i>${esc(label)}</label>`).join("");
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
        <div class="badges"><span class="badge manual">Operator-stated units</span></div>
        <div class="result" style="margin-top:8px">1 drawing unit =
          ${num(scale.mm_per_unit, 4)} mm</div>
        <p style="margin:0;font-size:11.5px;color:var(--ink-2)">${esc(scale.detail || "")}</p>
        <button class="ghost" id="restateUnit" style="margin-top:8px">Change unit</button>
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
        <div class="badges"><span class="badge manual">Operator calibrated</span></div>
        <div class="result" style="margin-top:8px">${esc(c.label)} / ${num(c.span_units, 2)} units
          = ${num(scale.mm_per_unit, 4)} mm/unit</div>
        <p style="margin:0;font-size:11.5px;color:var(--ink-2)">The span you measured is drawn on
          the drawing in red. This scale was supplied by you, not verified from the drawing.</p>
        <button class="ghost" id="recalibrate" style="margin-top:8px">Recalibrate</button>
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
    const dims = (cadEvidence.measurements || []).slice(0, 6)
      .map((v) => num(v, 1)).join("   ·   ");
    host.innerHTML = `<div class="calib">
      <div class="badges"><span class="badge warn">Units not declared</span></div>
      <p style="margin:8px 0 0;font-size:11.5px;color:var(--ink-2)">
        This drawing leaves <span class="mono">$INSUNITS</span> at 0, so it does not
        say which unit its coordinates are in. Its own dimensions measure:</p>
      ${dims ? `<div class="result" style="margin-top:7px">${esc(dims)}</div>` : ""}
      <p style="margin:0 0 7px;font-size:11.5px;color:var(--ink-2)">
        Extent ${cadEvidence.extent_units
          ? `${num(cadEvidence.extent_units[0], 0)} × ${num(cadEvidence.extent_units[1], 0)}`
          : "—"} drawing units. One drawing unit is:</p>
      <div class="fields">
        <select id="cadUnit">
          <option value="mm">millimetres (mm)</option>
          <option value="cm">centimetres (cm)</option>
          <option value="m">metres (m)</option>
          <option value="in">inches (in)</option>
          <option value="ft">feet (ft)</option>
        </select>
        <button class="primary" id="applyCadUnit">Apply</button>
      </div>
      <p style="margin:0;font-size:11px;color:var(--ink-2)">
        The geometry is the drawing's own; only the unit is yours, so the result
        is labelled operator-stated.</p>
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
    <div class="badges"><span class="badge warn">Scale requires confirmation</span></div>
    <p style="margin:8px 0 0;font-size:11.5px;color:var(--ink-2)">
      ${esc(scale?.detail || "No scale could be established from the drawing.")}</p>
    ${S.picking ? `
      <ol class="steps">
        <li class="${step === 1 ? "active" : ""}">Click the first point of a known dimension</li>
        <li class="${step === 2 ? "active" : ""}">Click the second point</li>
        <li class="${step === 3 ? "active" : ""}">Enter the real distance and apply</li>
      </ol>
      <p class="picked">${S.picks.map((p, i) =>
        `P${i + 1} ${num(p[0], 2)}, ${num(p[1], 2)}`).join("   ") || "No points picked yet"}</p>
      ${S.picks.length === 2 ? `<div class="result" id="calPreview"></div>` : ""}
      <div class="fields">
        <input type="number" id="knownLength" placeholder="Known distance" step="any">
        <select id="knownUnit">
          <option value="mm">mm</option><option value="cm">cm</option>
          <option value="m">m</option><option value="in">in</option><option value="ft">ft</option>
        </select>
      </div>
      <button class="primary" id="applyCal" ${S.picks.length === 2 ? "" : "disabled"}
        style="width:100%">Apply calibration</button>
      <button class="ghost" id="cancelCal" style="width:100%;margin-top:5px">Cancel</button>
    ` : `<button class="primary" id="startCal" style="width:100%;margin-top:9px">Calibrate Drawing</button>`}
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
  box.innerHTML = `Drawing span: ${num(span, 2)} units` +
    (raw > 0 ? `<br>Calculated scale: ${num((raw * factor) / span, 4)} mm/unit` : "");
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

const WHY = {
  "no embedded": "Without text there are no dimensions to calibrate from, so the scale must be supplied by hand.",
  "operator": "The scale was supplied by a person, not derived from the drawing, so the area inherits that judgement.",
  "annotation": "Annotation counted as geometry inflates envelopes and bounding rectangles.",
  "boundary": "If this boundary is the site rather than a machine, the number answers a different question.",
  "semantic meaning is not confirmed": "This is a geometric construction. What it represents has not been established, so do not quote it as a named physical region.",
  "stroked annotation": "Annotation drawn as lines is counted as geometry, which enlarges envelopes and bounding rectangles.",
};
function whyItMatters(text) {
  const lower = text.toLowerCase();
  for (const key of Object.keys(WHY)) if (lower.includes(key)) return WHY[key];
  return null;
}

function renderWarnings() {
  const result = S.result, item = currentReading();
  if (!result) { $("warningsPane").innerHTML = ""; return; }

  const warnings = [...(result.warnings || [])];
  for (const w of item?.warnings || []) if (!warnings.includes(w)) warnings.push(w);

  /* Facts the engine records in structured fields rather than as warning text.
   * Surfaced here because they change how much the number can be trusted; each
   * is read from backend state, never inferred. */
  if (!result.scale?.verified) {
    warnings.unshift("No verified scale, so no physical area is reported for this drawing.");
  } else if (result.scale.operator_supplied) {
    warnings.unshift("Scale was operator calibrated, not derived from the drawing.");
  }
  if (S.kind === "pdf" && S.analysis && S.analysis.text_span_count === 0) {
    warnings.push("No embedded PDF text was found; dimension annotation is stroked geometry.");
  }
  if (item?.provisional) {
    warnings.push(`${FP_STYLE[item.type]?.label || item.name} semantic meaning is not confirmed.`);
  }
  const excludedShare = result.geometry.raw_primitives
    ? result.geometry.ignored_primitives / result.geometry.raw_primitives : 0;
  if (S.kind === "pdf" && S.analysis?.text_span_count === 0 && excludedShare < 0.2) {
    warnings.push("Stroked annotation geometry may inflate the envelope and bounding rectangle.");
  }
  for (const amb of S.analysis?.ambiguities || []) warnings.push(`${amb.headline}: ${amb.reason}`);

  const count = warnings.length;
  $("warnCount").textContent = count;
  $("warnCount").classList.toggle("hide", count === 0);

  const assumptions = [...(result.assumptions || []), ...(item?.assumptions || [])]
    .filter((v, i, a) => a.indexOf(v) === i);

  $("warningsPane").innerHTML = (count ? `
    <div class="review">
      <h4>Review required</h4>
      <ul>${warnings.map((w) => {
        const why = whyItMatters(w);
        return `<li>${esc(w.split("\n")[0])}${why ? `<span class="why">${esc(why)}</span>` : ""}</li>`;
      }).join("")}</ul>
    </div>` : `<p style="color:var(--ink-2)">No warnings for this reading.</p>`) +
    (assumptions.length ? `<h4>Assumptions</h4>
      <ul class="assumptions">${assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : "");
}

function renderExplain() {
  const r = S.result, item = currentReading();
  if (!r) { $("explainFlow").innerHTML = ""; return; }
  const g = r.geometry, scale = r.scale;
  const a = areaText(item?.units);

  const steps = [
    ["Source", `${esc(S.fileName)}<br><span class="mono">${S.kind.toUpperCase()}${
      S.cad ? ` · ${esc(S.cad.dxf_version)}` : ""}</span>`],
    ["Drawing", S.kind === "pdf" ? `Page ${r.page}` : "Model space"],
    ["Scale", scale?.verified
      ? `${scale.operator_supplied ? "Operator calibrated" :
          scale.source === "cad_declared_units" ? "CAD $INSUNITS" : "Derived from the drawing"}<br>
         ${scale.calibration
           ? `<span class="mono">${esc(scale.calibration.label)} / ${num(scale.calibration.span_units, 2)} units</span><br>`
           : ""}
         <span class="mono">= ${num(scale.mm_per_unit, 4)} mm/unit</span>
         ${(scale.evidence || []).length
           ? `<ul>${scale.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}`
      : "Not verified — no physical area is reported"],
    ["Geometry", `<span class="mono">${g.raw_primitives.toLocaleString()}</span> primitives ·
      <span class="mono">${g.profile_primitives.toLocaleString()}</span> counted ·
      <span class="mono">${g.ignored_primitives.toLocaleString()}</span> excluded`],
    ["Interpretation", item
      ? `${esc(FP_STYLE[item.type]?.label || item.name)}<br>Semantic status:
         <b>${esc(item.semantics)}</b>
         ${(item.evidence || []).length
           ? `<ul>${item.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}`
      : "—"],
    ["Area", `${esc(r.method.replace(/_/g, " "))}<br>${g.outer_contours} outer ring(s),
      ${g.holes} hole(s)${r.subtract_holes ? " subtracted" : " kept"}`],
    ["Result", item?.units
      ? `<span class="mono" style="font-size:14px"><b>${a.value} ${a.unit}</b></span><br>
         <span class="mono">${esc(altText(item.units))}</span>`
      : "Not available without a verified scale"],
  ];

  $("explainFlow").innerHTML = steps.map(([k, v], i) =>
    `${i ? `<div class="arrow">↓</div>` : ""}
     <div class="step"><div class="sk">${esc(k)}</div><div class="sv">${v}</div></div>`).join("");
}

function renderDetail() {
  const r = S.result, a = S.analysis;
  if (!r) { $("detailPane").innerHTML = ""; return; }
  const g = r.geometry;
  const conv = S.conversion;
  const rows = [
    ["Method", r.method.replace(/_/g, " ")],
    ["Source", S.kind.toUpperCase()],
  ];
  if (conv) {
    rows.push(
      ["DWG version", `${conv.dwg_signature} · ${conv.dwg_version}`],
      ["Processing path", conv.path],
      ["Conversion", `successful · ${conv.tool} ${conv.tool_version} · ${conv.duration_seconds}s`],
      ["Converter warnings", String(conv.warnings.length)],
      ["Source SHA-256", conv.source_sha256.slice(0, 16) + "…"],
      ["Intermediate DXF", `${(conv.intermediate_dxf_bytes / 1e6).toFixed(1)} MB · ` +
                           conv.intermediate_dxf_sha256.slice(0, 16) + "…"],
    );
  }
  if (S.cad) {
    rows.push(["CAD units", S.cad.units.declared
      ? `${S.cad.units.name} ($INSUNITS ${S.cad.units.insunits})`
      : `not declared ($INSUNITS ${S.cad.units.insunits})`]);
  }
  rows.push(
    ["Region", `${r.view.label} (${r.view.source.replace(/_/g, " ")})`],
    ["Page rotation", a ? `${a.rotation}°` : "—"],
    ["Extent", a ? `${a.width_pt} × ${a.height_pt}` : "—"],
    ["Primitives", g.raw_primitives.toLocaleString()],
    ["Counted", g.profile_primitives.toLocaleString()],
    ["Excluded", g.ignored_primitives.toLocaleString()],
    ["Segments", g.segments.toLocaleString()],
    ["Faces", g.faces.toLocaleString()],
    ["Components", g.components.toLocaleString()],
    ["Outer rings", g.outer_contours],
    ["Holes", g.holes],
    ["Scale source", r.scale.source.replace(/_/g, " ")],
    ["mm per unit", r.scale.verified ? num(r.scale.mm_per_unit, 6) : "—"],
    ["Implied ratio", r.scale.implied_ratio || "—"],
    ["Engine", `${r.engine_version} · ${r.timestamp}`],
  );
  const roles = Object.entries(g.role_counts || {})
    .sort((x, y) => y[1] - x[1])
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v.toLocaleString()}</td></tr>`).join("");
  const repairs = (g.repairs || []).filter((x) => x.count > 0);

  $("detailPane").innerHTML =
    `<div class="kv">${rows.map(([k, v]) =>
      `<div class="r"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")}</div>
     <h4>Classification</h4>
     <table class="grid"><thead><tr><th>Role</th><th style="text-align:right">Count</th></tr></thead>
       <tbody>${roles}</tbody></table>` +
    (repairs.length ? `<h4>Repairs</h4><table class="grid"><tbody>${repairs.map((x) =>
      `<tr><td>${esc(x.type)}</td><td class="n">${x.count.toLocaleString()}</td></tr>`).join("")}</tbody></table>` : "");
}

const ACI = { 1: "#FF0000", 2: "#FFFF00", 3: "#00FF00", 4: "#00FFFF", 5: "#0000FF",
              6: "#FF00FF", 7: "#333333", 8: "#808080", 9: "#C0C0C0" };

function renderLayersPane() {
  /* Any CAD source has layers — a DXF read directly, or a DWG converted
   * locally. Only a PDF has none. */
  if (!S.cad) {
    $("layersPane").innerHTML = `<p style="color:var(--ink-2)">
      CAD layers are available when a DWG or DXF is loaded. A printed PDF carries no
      layer information — that is the main reason the CAD file is the better source.</p>`;
    return;
  }
  const layers = (S.cad.layers || []).filter((l) => l.entity_count > 0);
  const blocks = (S.cad.blocks || []).filter((b) => b.insert_count > 0);
  $("layersPane").innerHTML = `
    <p style="color:var(--ink-2);font-size:11.5px;margin:0 0 9px">
      Layer names are shown exactly as the file records them. No business meaning is
      assigned to them.</p>
    <table class="grid"><thead><tr>
      <th>Show</th><th>Layer</th><th>Linetype</th><th style="text-align:right">Entities</th>
    </tr></thead><tbody>${layers.map((l) => `
      <tr class="${S.hiddenLayers.has(l.name) ? "off" : ""}" data-layer-row="${esc(l.name)}">
        <td><input type="checkbox" data-cad-layer="${esc(l.name)}"
          ${S.hiddenLayers.has(l.name) ? "" : "checked"}></td>
        <td><span class="sw" style="background:${ACI[l.color] || "#55636A"}"></span>${esc(l.name)}</td>
        <td>${esc(l.linetype || "—")}</td>
        <td class="n">${l.entity_count.toLocaleString()}</td>
      </tr>`).join("")}</tbody></table>
    ${blocks.length ? `<h4>Blocks</h4>
      <table class="grid"><thead><tr><th>Block</th><th>XREF</th>
        <th style="text-align:right">Entities</th><th style="text-align:right">Placed</th></tr></thead>
      <tbody>${blocks.map((b) => `<tr><td>${esc(b.name)}</td><td>${b.is_xref ? "yes" : ""}</td>
        <td class="n">${b.entity_count.toLocaleString()}</td>
        <td class="n">${b.insert_count.toLocaleString()}</td></tr>`).join("")}</tbody></table>` : ""}
    <h4>Units</h4>
    <div class="kv"><div class="r"><span class="k">$INSUNITS</span>
      <span class="v">${S.cad.units.insunits} (${esc(S.cad.units.name)})</span></div>
      <div class="r"><span class="k">mm per unit</span>
      <span class="v">${S.cad.units.mm_per_unit ?? "—"}</span></div></div>`;

  for (const input of $("layersPane").querySelectorAll("input[data-cad-layer]")) {
    input.onchange = () => {
      const name = input.dataset.cadLayer;
      if (input.checked) S.hiddenLayers.delete(name); else S.hiddenLayers.add(name);
      renderLayersPane(); paintOverlay();
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
(async function showCapabilities() {
  try {
    const caps = (await call("/api/capabilities")).formats;
    const line = $("formatLine");
    if (!line) return;
    if (!caps.dwg.supported) {
      line.innerHTML = `PDF · DXF · <span style="color:var(--amber)">DWG (setup required)</span>`;
      line.title = caps.dwg.reason || "";
      const host = $("uploadNotice");
      host.innerHTML = `<div class="notice">
        <h4>DWG support is not installed</h4>
        <p>DWG files are converted locally — nothing is uploaded anywhere — but the
           conversion component is not present yet.</p>
        <p class="fix">Run: <span class="mono">${esc(caps.dwg.setup_command || "")}</span></p>
      </div>`;
    } else {
      line.title = caps.dwg.note || "";
    }
  } catch (e) { /* the landing screen still works without this */ }
})();

/* Reference drawings, generated and measured by the real backend. */
(async function loadDemos() {
  try {
    const data = await call("/api/demo");
    $("demoGrid").innerHTML = (data.drawings || []).slice(0, 4).map((d) =>
      `<button type="button" data-demo="${esc(d.id)}">
        <span class="t">${esc(d.title)}</span>
        <span class="d">${esc(d.exercises)}</span>
      </button>`).join("");
    for (const btn of $("demoGrid").querySelectorAll("button[data-demo]")) {
      btn.onclick = () => openDemo(btn.dataset.demo);
    }
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
