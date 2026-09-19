# Projected Area Analyzer

Engineering drawing PDF → verified geometry → verified scale → **projected area
you can see, check and reproduce.**

The tool never reports a physical area it cannot justify. If the scale cannot be
established, it says so and reports nothing in millimetres.

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

Open <http://localhost:8000/> — the viewer is served from the same origin, so
there is no CORS hop and no `file://` restrictions.

Then: **打开文件** → **分析图纸** → pick a view → read the result card.

Headless, no browser:

```bash
.venv/bin/python -m tools.audit_overlay samples/YOUR-DRAWING.pdf --page 3 --out audit.png
```

Tests:

```bash
.venv/bin/python -m pytest tests -q      # 74 tests, ~11 s
```

The two browser tests drive the real page in Chromium and are skipped
automatically if Playwright or a Chromium build is missing; the other 72 run in
about a second.

---

## What was already here, and what changed

| Before | After |
|---|---|
| `cad-area-meter.html` — a working client-side planimeter: PDF.js viewer, magic-wand flood fill, polygon tool, two-point calibration, CSV export | **Kept in full.** Its layout, palette and tools are untouched; a server-analysis panel, a result card and an SVG audit overlay were added alongside them |
| `backend/app.py` — a 238-line prototype in one file | Replaced by a domain-structured engine (below). The prototype mis-read PyMuPDF's item format (`("l", x0, y0, x1, y1)` — the real shape is `("l", Point, Point)`), flattened Béziers from the origin instead of from the curve start, unioned faces without hole handling so every hole was filled, and derived scale from a bare `1:N` regex |
| `tests/test_geometry.py` — 3 tests, one of which could not run (`Point` was never imported) | 74 tests across geometry, calibration, end-to-end pipeline, HTTP API and the viewer itself |

The existing UI represents product intent (CONSTITUTION §21) and was preserved
deliberately. The manual wand and polygon tools remain the fallback for drawings
the automatic path cannot handle, and they now share the backend's scale.

---

## The sample PDF

`104-NSY1682-PDM06-CTP-32PPM-Simplified-V5.1.pdf` exists on this machine only
inside WeWork's cache, where it is **stored encrypted** — its first bytes are
`7b ff 75 95`, not `%PDF`, and PyMuPDF rejects it with "no objects found".
It could not be read, so it has not been validated against.

**To validate it:** save the file from WeWork into `samples/` and run

```bash
.venv/bin/python -m tools.audit_overlay samples/104-NSY1682-*.pdf --page 3 --out audit.png
```

In its place the engine is validated against five synthetic CAD-style drawings
with analytically known areas — each with a sheet frame, title block, dimension
lines with arrowheads and extension lines, dashed centrelines, hatching and
annotation text, because the pipeline's real job is to *ignore* all of that.

| Drawing | Truth | Measured | Error | Method |
|---|---|---|---|---|
| Plate, 3 × Ø20 holes, 1:2 | 23 057.52 mm² | 23 057.63 mm² | **+0.0005 %** | vector exact |
| Two views — top | 14 982.12 mm² | 14 982.25 mm² | **+0.0009 %** | vector exact |
| Two views — hatched front | 6 400.00 mm² | 6 400.00 mm² | **0.0000 %** | vector exact |
| L-shaped floor plan, 1:100 | 54.4606 m² | 54.4608 m² | **+0.0004 %** | vector exact |
| Obround, Bézier arcs, dimensions drawn *through* the face | 17 354.87 mm² | 17 363.0 mm² | **+0.047 %** | vector exact |
| Broken outline (0.9 mm gap) | 12 793.14 mm² | 12 806.88 mm² | +0.107 % | gap-closed, **warned** |
| Scanned page, no scale text | — | *refuses to report mm²* | — | raster trace |
| Scanned page, manual calibration | 23 057.52 mm² | 22 888.63 mm² | −0.73 % | raster trace, 77 % confidence |

Scale was recovered automatically from the dimension annotations in every vector
case, to within 0.02 % of truth. With the scale pinned exactly, the obround's
residual error is 0.008 % — that is pure Bézier flattening error, two orders of
magnitude below any drawing tolerance.

The obround fixture is the deliberately nasty one: a curved profile whose
dimensions are drawn *across the part* rather than outside it, as they are on any
crowded sheet. Dimension linework that is not demoted slices the face into
fragments; arcs that are flattened carelessly lose area on every cap.

---

## How the calculation works

### 1. Classify the page

`page.get_drawings()` path count and total ink versus raster image coverage give
`VECTOR` / `RASTER` / `MIXED` / `UNKNOWN`. Everything downstream branches here.

### 2. Extract native vector primitives

PyMuPDF returns one dict per path, with items already in page space. Two things
must be got right:

- **Subpath recovery.** A single path may hold several disconnected subpaths and
  PyMuPDF does not mark the boundaries. They are recovered geometrically: an
  item whose start point does not coincide with the previous item's end begins a
  new subpath. Getting this wrong welds unrelated linework into one polygon.
- **Adaptive Bézier flattening.** Recursive de Casteljau subdivision on the
  control-polygon flatness test, with a floor on sample count so a small hole
  never collapses into a triangle. A Ø100 circle flattens to within 0.007 % of
  its true circumference.

A degenerate "closed" path — `A → B → A`, three points, zero area, which many
producers emit for every plain line — is reopened, or hatch detection and
dimension matching both go blind.

Raw points are never overwritten; roles and cleaned copies are layered on top
(§15).

### 3. Classify each primitive by role

| Role | Signal | Counted? |
|---|---|---|
| `profile` | everything not matched below | **yes** |
| `centerline` / `hidden` | dashed pattern | no |
| `hatch` | ≥6 parallel, evenly spaced, thin open segments in one direction family | no |
| `dimension` | small solid marker (arrowhead); or a thin segment whose midpoint sits on a numeric annotation | no |
| `annotation` | ≥72 % of its bounding box inside a padded text box | no |
| `sheet` | spans ≥86 % of the page in both directions | no |
| `uncertain` | ambiguous | **yes**, but flagged orange and it costs confidence |

### 4. Choose a region

Profile linework is stamped onto a coarse occupancy grid — the paths themselves,
not their bounding boxes, so one long diagonal cannot bridge two views — dilated
by the whitespace gutter between views, then connected-component labelled. Each
cluster is tightened back onto its ink and labelled a view, title block, table or
note. A `TOP VIEW` caption raises the view guess; **the geometry never depends on
the caption.** Scanned pages get the same treatment from thresholded ink.

The user can always override by dragging a region (**框选视图**).

### 5. Normalise the linework

Snap endpoints (CAD exports emit `100.0` and `99.9999` for the same corner) →
drop zero-length segments → remove duplicate and reversed-duplicate segments →
bridge sub-tolerance gaps between *degree-one* vertices, shortest first, each end
used once. Every step is counted and logged as a repair.

### 6. Reconstruct the silhouette

**Strategy V1 — exact polygonization** (preferred, no rasterization anywhere):

1. `unary_union` over the segments nodes every crossing into shared vertices.
2. `polygonize` recovers the atomic faces of that planar arrangement. Dangling
   linework bounds no face and simply disappears — which is exactly right for
   extension lines and leaders.
3. Each face is called solid or void by **containment depth**:

   ```
   depth(f) = #{ g ≠ f : outer_ring(g) strictly encloses a point of f }
   f is solid  ⟺  depth(f) is even
   ```

4. Union the solid faces.

This is deliberately **not** the even-odd rule applied to raw edge crossings.
Edge parity would XOR two partially overlapping outlines and punch a false hole
through their intersection. Counting enclosing *rings* instead makes nested loops
alternate solid/void — outline, hole, island — while merely overlapping loops
both stay solid, so overlap is counted once.

> **Worked example.** A 100 × 50 plate with a Ø20 hole polygonizes into two
> faces: the annulus and the disc. The annulus is enclosed by nothing (depth 0,
> solid); the disc sits inside the annulus's outer ring (depth 1, void). The
> union is the annulus — one exterior ring, one interior ring, 4 685.84 units².
> A plain `unary_union(polygonize(...))` would have reported 5 000.

**Strategy V2 — gap-closed silhouette** (fallback, reported as a distinct method
with lower confidence): the *already extracted* segments are scan-converted one
pixel wide, a morphological closing bridges the break, and the contour tree is
read with the same nesting-parity rule — odd depth bounds solid, even depth ≥2 is
a hole. Working from the inner boundary of a one-pixel stroke biases every edge
inward by half a pixel, so the result is grown by exactly that much.

This is not "rasterize the PDF and rediscover the lines", which §5 forbids. The
input is known vector geometry; the bitmap is a scratch pad for topology.

**Escalation.** A profile must span at least 30 % of its own linework's bounding
box. (Area coverage is the wrong test — a thin annulus legitimately fills very
little of its box — but *extent* is reliable: a profile that failed to close
collapses onto whatever small loop did close.) If it does not, the closure
tolerance is widened ×2.5 then ×6, retrying V1 each time, then V2. Every step is
recorded and warned about.

**Strategy V3 — raster trace** (Path B, scanned pages): render at 300 dpi,
adaptive threshold, then the *same* nesting-parity contour reader, so a plate with
a hole is interpreted identically whichever path produced the pixels.

### 7. Establish the scale

Three sources, strongest first (§4):

1. **User two-point calibration.** Always wins when supplied. Confidence is
   penalised for short picks, where a one-unit misclick costs real accuracy.
2. **Dimension consensus** — the automatic method. Every dimension annotation is
   matched to nearby linework, giving one candidate mm-per-unit each. Candidates
   are then **voted in log space** (so a 6 000 mm dimension is judged as tightly
   as a 12 mm one). The true scale is shared by *all* dimensions, so correct
   matches pile into one bin while mismatches scatter. A bin is scored by the
   number of **distinct annotations** supporting it, never by raw segment
   matches, and the winner is a span-weighted mean of its members.

   Broken dimension lines — split either side of their text, as most CAD
   exporters emit them — are handled by also offering the total extent of each
   collinear family as a candidate span.

3. **Printed drawing ratio** (`SCALE 1:2`). Recorded, never trusted alone: a
   "fit to page" print silently invalidates it. When a consensus exists the two
   are cross-checked, and a mismatch becomes a warning:

   > Measured scale disagrees with the printed scale 'SCALE 1:2' by 25.6 %. The
   > page was most likely rescaled on export or printed to fit. The measured
   > dimensions were used.

Region-restricted calibration is tried first — a sheet may legitimately mix a 2:1
detail with a 1:10 general view — and falls back to page-wide when the region
lacks enough agreeing dimensions.

If none of the three yields a scale, `projected_area.verified` is `false`, every
millimetre field is `null`, and the message is:

> Scale not verified. Physical projected area cannot yet be calculated.

### 8. Let the engineer overrule any of it

Automation that cannot be corrected is worse than no automation (§9). Three
correction paths, all measured by the same backend and all recorded in the
result:

- **审阅线条 / review linework** — the overlay becomes clickable and every
  primitive is drawn in its effective role: solid where counted, dashed where
  set aside, with its classification reason on hover. Clicking toggles it.
  Overrides are applied through a lookup, never by mutating the cached page, so
  any override can be taken back and a stale override can never leak into the
  next calculation.
- **补画边界 / draw boundary** — a hand-drawn ring is unioned into the profile,
  so a mostly-correct automatic outline can be patched at one corner instead of
  redrawn whole.
- **擦除区域 / erase region** — a hand-drawn ring is subtracted. It removes only
  material that was actually counted: erasing across a hole does not double-
  subtract it.

Every correction appears in `assumptions`, and §10 counts human review as
evidence, so a corrected result scores slightly *higher* than an unreviewed one.

### 9. Score the confidence

A **weighted geometric mean** — not an average, so a weak link drags the result
down instead of being masked:

| Component | Weight | Driven by |
|---|---|---|
| scale | 0.35 | source, number of agreeing dimensions, spread, cross-check |
| geometry | 0.28 | reconstruction method, share of ambiguous primitives, component count |
| repair | 0.12 | gaps bridged, morphological closing, self-intersections rebuilt |
| source | 0.15 | vector 0.97 / mixed 0.86 / raster 0.62 / unknown 0.40 |
| view | 0.10 | user-selected 1.00 / auto 0.85 / whole page 0.55 |

Human review lifts the geometry component by 6 % — deliberately small, because a
hand-drawn boundary is *checked*, not necessarily precise.

Cosmetic repairs (duplicate removal, zero-length drops) cost nothing. Bridging a
gap does. **An unverified scale forces the overall figure to zero** — there is no
physical number to be confident about.

Every component and every note is returned and shown in the UI.

---

## Architecture

```
Frontend  cad-area-meter.html — PDF.js viewer, SVG audit overlay,
                                local wand/polygon tools (unchanged)
              │  HTTP, same origin
Backend   FastAPI
          backend/
            config.py        every tolerance, page-scaled, documented
            units.py         mm / mm² canonical, explicit conversions
            models.py        domain model + result serialisation
            pipeline.py      stage coordinator, per-page cache
            store.py         temp-file store, TTL sweep, delete on exit
            pdf/             PyMuPDF confined here (source-adapter seam)
            geometry/        classify · regions · segments · contours · silhouette · polygons
            calibration/     scale recovery and voting
            area/            projected-area orchestration
            confidence/      interpretable scoring
            raster/          Path B
            api/             routes + request schemas
tools/      audit_overlay.py headless overlay renderer
tests/      fixtures.py + 62 tests
```

### Why this stack

| Need | Chosen | Rejected, and why |
|---|---|---|
| PDF vector paths | **PyMuPDF** | `pdfplumber` re-derives paths through pdfminer, slower and lossier on curves; `pikepdf` is object-level, no path resolution; PDFium/Poppler need a C++ layer for the same data |
| Planar geometry | **Shapely 2** | `pyclipper` clips but does not polygonize an arrangement, which is the whole problem; OCC/CadQuery is a 3-D kernel and a very heavy dependency for 2-D work |
| Raster fallback | **OpenCV** (headless) | only for scan conversion and Path B tracing |
| Viewer | **PDF.js** | already in the UI and correct |

### PDF → DXF was evaluated and rejected

Converting to DXF first adds a lossy hop (arcs re-fitted, layers invented,
colours mapped to ACI) to reach the same primitives PyMuPDF already hands over in
page coordinates, and `ezdxf` would then need the identical noding, polygonize
and parity work. It buys nothing for area. It stays worth having as an *export*,
so a verified profile can go back to CAD — that is a different feature.

### API

```
POST   /api/documents                                  upload → page inventory + classification
DELETE /api/documents/{id}                             delete now
GET    /api/documents/{id}/pages/{n}/analyze           regions, roles, auto-scale + evidence
GET    /api/documents/{id}/pages/{n}/geometry?roles=…  classified primitives for the overlay
POST   /api/documents/{id}/pages/{n}/area              the calculation, plus role_overrides,
                                                       manual_add and manual_subtract
POST   /api/measure/polygon                            authoritative area for hand-drawn rings
GET    /api/health
```

Interactive docs at `/docs`. The backend owns every engineering number, including
the ones drawn by hand (§22).

### Auditability

The overlay paints **green** included profile, **red** holes, **grey** ignored
annotation/dimension/sheet linework, **orange** uncertain geometry, and an
**amber dashed** box around the measured region. Every result answers, on its own:

what geometry was used · what was excluded and under which role · what scale, from
where, with what evidence · what was repaired and by how much · how confident, and
which component drove that · what was assumed.

---

## Known limitations

1. **The real sample PDF has not been tested** — it is encrypted inside WeWork's
   cache. See *The sample PDF* above.
2. **Raster tracing cannot separate annotation from profile.** It has no role
   information, so dimension bands enclose regions indistinguishable from the
   part. Path B therefore includes only the dominant silhouette by default and
   says so; switch the others back on if the view really holds several bodies.
3. **Scale on a scanned drawing needs OCR or a manual pick.** No OCR engine is
   wired in yet, so a page with no text layer has no automatic calibration. This
   is correct behaviour, not a crash — it refuses to guess.
4. **View classification is caption-driven.** Without a `TOP VIEW`-style label
   the view guess stays `unknown`. Projection-convention inference (first vs
   third angle from view layout) is not implemented.
5. **Multi-body views need manual component review.** Several disconnected
   silhouettes may be one part or several; the tool lists them with per-component
   areas and lets the user decide, rather than guessing.
6. **Rotated pages are handled via `page.rect`** and agree with PDF.js in
   testing, but no fixture yet covers a `/Rotate 90` sheet.
7. **Curve flattening is a controlled approximation.** ~0.007 % on a circle's
   perimeter, well under drawing tolerance, but it is an approximation.
8. **No 3-D CAD yet** — see below.
9. **Storage is in-process.** Uploads live in a temp directory with a 4-hour TTL
   and vanish on restart. Right for a single-user tool; not a multi-user service.
10. **Warnings and assumptions are in English inside a Chinese UI.** They are the
    engineering record and are shared verbatim by the JSON API, the CLI and the
    viewer, so they are not translated at the edge. Localising them means a
    message catalogue in `backend/`, which is worth doing but has not been done.

---

## Next steps, in the order they pay off

1. **Run the real drawing** and tune tolerances against it. Everything else is
   speculation until that happens.
2. **OCR for scanned sheets** (PaddleOCR or Tesseract, local only) — purely to
   feed dimension text into the existing consensus voter. Geometry stays
   deterministic. This is the single biggest unlock for Path B.
3. **DXF export** of the verified profile, so a checked outline returns to CAD.
4. **Multi-page batch** — measure the same view across a drawing set and diff.
5. **Localise the warning and assumption strings** — a message catalogue in
   `backend/`, keyed so the JSON API keeps stable identifiers.
6. **Optional vision-model assist** for view classification and title-block
   reading, behind an explicit opt-in flag, never in the numeric path (§7, §35).
7. **3-D CAD source adapter** (STEP/IGES/STL via CadQuery/OCP): project the solid
   along a chosen direction, take the silhouette, union, and hand the resulting
   2-D polygons to the *same* area and confidence code that serves PDFs. The
   `pdf/` package is already isolated behind that seam (§37).

---

## Engineering rules this implementation is bound by

`CONSTITUTION.md`, in particular: §2 what projected area means · §3 never
fabricate a measurement · §4 scale must be explicit · §5 vector first · §8 every
result auditable · §10 confidence must be explainable · §16 every repair recorded
· §30 never hide a fallback · §31 errors must be actionable.

The rule that shaped the most code is §3. Refusing to answer is a feature.
