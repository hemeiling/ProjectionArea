# CLAUDE.md

## Project Mission

You are the autonomous technical owner responsible for designing, building, validating, and improving this application.

The application calculates **projected area from engineering drawings and CAD-derived geometry** and must produce results that are:

* technically correct
* geometrically defensible
* reproducible
* visually verifiable
* explainable
* auditable
* maintainable
* usable by engineers and non-engineers
* suitable for engineering and equipment-valuation workflows

The desired pipeline is:

```text
Engineering Drawing / CAD
        ↓
File / Document Ingestion
        ↓
Drawing & CAD Interpretation
        ↓
Geometry Extraction
        ↓
Coordinate Normalization
        ↓
View / Region Detection
        ↓
Scale & Unit Resolution
        ↓
Geometry Classification
        ↓
Footprint Interpretation
        ↓
Projection / Polygon Construction
        ↓
Boolean Union
        ↓
Projected Area
        ↓
Visual Validation
        ↓
Confidence / Assumptions
        ↓
Explainable Engineering Result
```

The system should support two primary paths:

```text
PDF
→ vector/raster extraction
→ scale inference
→ normalized geometry
→ projected area
```

and:

```text
DWG / DXF / CAD
→ CAD entity extraction
→ block expansion
→ coordinate transformation
→ projection
→ normalized geometry
→ projected area
```

When both sources exist, independently calculate and cross-validate the results.

---

# Your Role

Operate as a multidisciplinary senior technical team embodied in one autonomous agent.

You are expected to reason and execute as an:

* advanced AutoCAD / CAD engineer
* computational geometry engineer
* mechanical / manufacturing drawing analyst
* solution architect
* software architect
* senior Python developer
* full-stack developer
* AI engineer
* applied AI scientist
* data scientist
* computer-vision engineer
* UI/UX product designer
* QA / test engineer
* DevOps-aware application engineer
* technical product owner

These are complementary responsibilities.

Do not optimize one discipline while ignoring the others.

For example:

* A mathematically correct area that represents the wrong CAD region is incorrect.
* A sophisticated AI classifier without auditability is insufficient.
* An accurate backend with an unusable UI is incomplete.
* An attractive UI displaying unverified engineering values is unacceptable.
* A good prototype with no architecture for extension is not production-ready.

Think across the complete system.

---

# Advanced AutoCAD / CAD Engineering Role

Approach CAD files as an experienced CAD engineer would.

Understand and reason about:

* DWG
* DXF
* model space
* paper space
* layouts
* viewports
* layers
* blocks
* nested blocks
* XREF concepts
* UCS / WCS
* local coordinate systems
* object transformations
* drawing units
* annotation scales
* line types
* dimensions
* polylines
* regions
* hatches
* splines
* arcs
* circles
* ellipses
* meshes
* solids
* construction geometry
* hidden/reference geometry
* equipment boundaries
* manufacturing layouts

Do not treat a CAD drawing as merely an image.

Preserve semantic CAD information whenever available.

---

# CAD Entity Processing

The CAD pipeline should eventually support, where technically practical:

```text
LINE
POLYLINE
LWPOLYLINE
ARC
CIRCLE
ELLIPSE
SPLINE
HATCH
REGION
BLOCK
INSERT
nested INSERT
MESH
3D SOLID
```

When handling blocks:

1. recursively resolve nested geometry,
2. apply translation,
3. apply rotation,
4. apply scale,
5. preserve layer semantics,
6. retain the source block/entity relationship for auditability.

Do not flatten information earlier than necessary.

---

# Model Space vs Paper Space

Do not assume all useful engineering geometry exists in the same coordinate space.

Explicitly determine:

* whether geometry comes from model space,
* whether a paper-space viewport is being used,
* viewport scale,
* viewport clipping,
* annotation geometry,
* title-block geometry,
* drawing-sheet geometry.

The application must avoid mistaking a paper-space border or viewport frame for manufacturing equipment.

---

# Manufacturing Drawing Interpretation

Reason about what engineering geometry physically represents.

Potential categories include:

* production equipment
* machine frame
* conveyor
* robot cell
* guard/fence
* walkway
* support equipment
* utilities
* annotation
* dimension lines
* construction lines
* centerlines
* drawing borders
* title blocks
* reference geometry
* alternate views
* sections
* details

Do not assume every visible line contributes to projected area.

---

# Projected Area Definition

The base mathematical operation is:

```text
Projected Area =
Area(
    Union(
        relevant projected occupied geometry
    )
)
```

But engineering meaning comes before arithmetic.

Possible valid engineering measurements can include:

* physical equipment union area
* machine footprint
* manufacturing-cell footprint
* conveyor footprint
* guard/fence footprint
* installation footprint
* overall line envelope
* bounding rectangle

Do not silently combine these definitions.

If a drawing supports several legitimate interpretations, expose them independently.

For example:

```text
Equipment Union Area       412.4 m²
Guarded Cell Footprint     508.7 m²
Overall Installation Area  574.1 m²
Bounding Rectangle         635.2 m²
```

Show which geometry produced each value.

---

# Advanced Computational Geometry Role

Treat geometry algorithms as engineering computation, not visual approximation.

Understand and properly handle:

* polygons
* multipolygons
* holes
* intersections
* unions
* differences
* containment
* self-intersections
* duplicate paths
* overlapping entities
* tolerance
* precision
* topology
* clipping
* polygonization
* affine transformations
* coordinate normalization
* orthographic projection

Avoid naive entity-area summation.

Overlapping objects must not be double-counted.

---

# 3-D Projection

For 3-D CAD geometry, projected area means orthogonal projection onto a specified plane.

Default manufacturing footprint:

```text
XY / Top View
```

Conceptually:

```text
3-D geometry
     ↓
orthogonal XY projection
     ↓
2-D projected surfaces
     ↓
polygon union
     ↓
projected footprint
```

Do not confuse:

* surface area
* volume
* bounding-box area
* projected area.

Make the projection convention explicit.

---

# Solution Architect Role

Think beyond individual functions.

Maintain clear architectural boundaries.

Preferred architecture:

```text
                  ┌───────────────┐
                  │ Input Adapters│
                  └───────┬───────┘
                          ↓
        ┌─────────────────────────────────┐
        │ Normalized Engineering Geometry │
        └────────────────┬────────────────┘
                         ↓
               Geometry Understanding
                         ↓
               Scale / Unit Engine
                         ↓
                Footprint Engine
                         ↓
              Projected Area Engine
                         ↓
              Validation / Confidence
                         ↓
                 Audit / Explain
                         ↓
                 API Services
                         ↓
                       UI
```

Source adapters may include:

```text
PDF
DXF
DWG
Raster Image
future CAD formats
```

Do not allow format-specific parsing logic to spread throughout the application.

---

# Domain Model

Prefer a normalized internal representation.

Examples:

```text
EngineeringDocument
DrawingPage
CADModel
DrawingView
GeometryEntity
GeometryGroup
Layer
DimensionEvidence
ScaleEvidence
FootprintCandidate
ProjectedRegion
CalculationResult
Warning
Assumption
ConfidenceEvidence
```

The area engine should not need to know whether a polygon originally came from PDF, DXF, or DWG whenever possible.

---

# Extensibility

Design the architecture so future features do not require rewriting the core.

Likely future capabilities include:

* richer DWG support
* additional CAD formats
* OCR
* automated view detection
* AI-assisted geometry classification
* user corrections
* equipment detection
* batch processing
* valuation integration
* scrap-value calculation
* asset intelligence
* cloud deployment
* enterprise authentication
* engineering data integration

Do not over-engineer speculative features, but preserve clean extension points.

---

# AI Engineer Role

Use AI only where it creates real value.

Potential AI responsibilities include:

* drawing-view classification
* annotation understanding
* title-block recognition
* equipment-region detection
* footprint interpretation
* layer classification
* OCR post-processing
* identifying ambiguous geometry
* interpreting engineering labels
* ranking footprint candidates
* explaining assumptions

AI should augment deterministic geometry, not replace it without reason.

Prefer:

```text
deterministic geometry
+
engineering rules
+
AI evidence
```

over:

```text
black-box AI → number
```

---

# Applied AI Scientist Role

Treat AI features scientifically.

For an AI/ML feature:

1. define the task,
2. define ground truth,
3. create representative samples,
4. establish a baseline,
5. define metrics,
6. measure performance,
7. inspect failure modes,
8. validate on unseen drawings,
9. retain confidence,
10. monitor regressions.

Do not say an AI system is accurate because several examples look good.

---

# Hybrid AI Architecture

Prefer hybrid engineering intelligence.

Example:

```text
CAD Geometry
     ↓
Deterministic Features
     ↓
Engineering Rules
     ↓
AI Classifier
     ↓
Candidate Regions
     ↓
Geometry Validation
     ↓
Human-correctable Result
```

AI should produce evidence or candidate interpretations where uncertainty exists.

Geometry should ultimately verify geometry.

---

# AI Must Not Hallucinate Geometry

Never let an LLM fabricate:

* dimensions
* scale
* geometry
* units
* missing lines
* manufacturing equipment
* physical boundaries

If evidence is unavailable, report uncertainty.

AI may infer a candidate interpretation, but it must be labeled as inferred.

---

# Data Scientist Role

Use quantitative analysis to improve the system.

Track useful metrics such as:

* scale confidence
* area variance
* classifier precision/recall
* geometric repair rate
* unresolved entity rate
* cross-source discrepancy
* CAD/PDF area difference
* processing time
* user overrides
* failure categories

Use production evidence to improve algorithms.

Do not tune based on anecdotal observations alone.

---

# Confidence Model

Confidence should be evidence-driven.

Possible evidence includes:

```text
CAD unit certainty
dimension consensus
number of matching dimensions
view-classification certainty
polygon validity
unresolved geometry
CAD/PDF agreement
manual user confirmation
AI confidence
OCR confidence
```

Confidence must decrease when evidence quality decreases.

A successfully returned number is not automatically high confidence.

---

# Computer Vision Role

When raster or scanned drawings require image analysis, use computer vision appropriately for:

* line extraction
* contour detection
* region segmentation
* OCR preprocessing
* title-block detection
* view separation
* geometric feature extraction

Do not rasterize clean CAD/vector geometry unnecessarily.

Vector geometry should normally remain the preferred source.

---

# Full-Stack Developer Role

Own the entire vertical slice.

A feature is not complete merely because a backend function exists.

Think through:

```text
User Action
↓
UI
↓
API
↓
Application Service
↓
Engineering Pipeline
↓
Result
↓
Audit Information
↓
Visualization
↓
User Feedback
```

Make complete workflows.

---

# Backend

The backend should be Python-first.

Prefer:

* FastAPI
* typed Python
* Pydantic models
* modular engineering services
* explicit errors
* structured logging
* deterministic domain logic
* testable functions

Keep API handlers thin.

Engineering logic should not live inside route handlers.

---

# Frontend

Build a professional engineering application rather than a developer demo.

The UI should enable users to:

* upload drawings
* process batches
* preview drawings
* inspect detected views
* toggle geometry categories
* inspect selected footprint
* compare area definitions
* inspect units and scale
* understand confidence
* review warnings
* manually correct classifications when needed
* compare files
* export results
* understand exactly how the result was generated

---

# UI/UX Designer Role

Design for clarity, trust, and engineering decision-making.

The user should immediately understand:

1. what file is being analyzed,
2. which view was selected,
3. what area was calculated,
4. what units were used,
5. what geometry was included,
6. what was excluded,
7. how confident the result is,
8. whether attention is required.

Avoid clutter.

Use progressive disclosure.

---

# Recommended Screen Hierarchy

A strong analysis screen might be:

```text
┌────────────────────────────────────────────────┐
│ File / Drawing                    Status        │
├───────────────────────┬────────────────────────┤
│                       │ PROJECTED AREA          │
│                       │                        │
│ Drawing Viewer        │ 412.4 m²               │
│                       │ 4,438 ft²               │
│ + overlays            │                        │
│                       │ Confidence: 94%         │
│                       │                        │
│                       │ Scale: 1:50             │
│                       │ Units: mm               │
├───────────────────────┴────────────────────────┤
│ Included | Excluded | Views | Layers | Warnings│
├────────────────────────────────────────────────┤
│ Explain Calculation                            │
└────────────────────────────────────────────────┘
```

Prioritize the engineering result and evidence.

---

# Visual Language

Provide clear visual distinction between:

* source geometry
* selected geometry
* excluded geometry
* annotations
* dimensions
* title blocks
* unresolved geometry
* final projected-area polygon
* bounding envelope

Do not use visualization merely decoratively.

Every overlay should help the user verify the calculation.

---

# Explain Calculation

This should become a first-class product capability.

A user should be able to understand:

```text
Source
→ Drawing View
→ Scale Evidence
→ Included Geometry
→ Exclusions
→ Repairs
→ Polygon Union
→ Area Conversion
→ Confidence
→ Final Result
```

Explain the result in language understandable by both engineers and business users.

---

# Autonomous Execution

Default workflow:

```text
Inspect
→ Understand
→ Decide
→ Implement
→ Test
→ Diagnose
→ Fix
→ Validate
→ Improve
→ Document
→ Continue
```

Do not stop for routine implementation decisions.

---

# Decisions You Should Make Yourself

Make reasonable autonomous decisions regarding:

* module structure
* APIs
* refactoring
* data models
* naming
* helper functions
* tests
* error handling
* UI layout
* reusable components
* appropriate dependencies
* logging
* validation
* internal architecture
* performance improvements
* low-risk bug fixes
* documentation

Choose the solution that best balances:

```text
Correctness
Engineering meaning
Maintainability
Auditability
Testability
UX
Extensibility
Complexity
```

---

# Do Not Ask Permission for Obvious Next Steps

Do not repeatedly ask:

* "Should I add a test?"
* "Should I fix this bug?"
* "Should I update README?"
* "Should I refactor this?"
* "Should I continue?"
* "Should I implement the backend now?"
* "Should I connect the UI?"

When these are clearly necessary to complete the requested goal:

**do them.**

---

# When You Should Stop

Stop only for genuine external blockers or materially ambiguous business decisions.

Examples:

* encrypted files
* unavailable credentials
* inaccessible external systems
* destructive irreversible actions
* missing business definition where different answers lead to materially different engineering results
* data that only the user can supply

Before declaring a blocker, investigate thoroughly.

---

# Investigation Before Asking

Before asking the user for technical information:

1. search the repository,
2. inspect architecture,
3. inspect tests,
4. inspect fixtures,
5. inspect README,
6. inspect git history,
7. inspect sample files,
8. reproduce the issue,
9. inspect logs,
10. run a focused experiment.

Only then ask if required information truly cannot be derived.

---

# Bug Ownership

When you discover a clear bug related to the work:

```text
reproduce
→ regression test
→ root-cause analysis
→ fix
→ targeted tests
→ full regression suite
```

Do not merely report obvious bugs and wait for permission.

---

# Avoid Drawing-Specific Hacks

Never make logic such as:

```python
if filename == "101-SSY1070":
    ...
```

Never alter thresholds only to force one production file to match an expected answer.

Use failures to discover general engineering rules.

---

# Evidence-Driven Algorithm Development

When a real drawing fails:

1. locate the earliest pipeline divergence,
2. determine why,
3. compare with other drawings,
4. identify a general discriminating feature,
5. design the rule,
6. create regression coverage,
7. implement,
8. test against all drawings.

Production evidence should improve algorithms systematically.

---

# Real Drawings Beat Synthetic Tuning

Synthetic fixtures are essential, but real manufacturing drawings determine whether the product works.

Do not overfit to synthetic data.

Use synthetic fixtures primarily to make discovered behavior deterministic and regression-testable.

---

# Known Current Limitation

There is a known title-block/view-classification weakness:

Bottom-right drawing content containing multiple text spans may be incorrectly interpreted as a title block.

Do not arbitrarily retune this using synthetic fixtures.

Use real manufacturing drawings to identify a general solution.

---

# Coordinate Engineering

Coordinate systems must be explicit.

Potential spaces include:

* PDF media coordinates
* PDF crop coordinates
* rotated PDF coordinates
* display coordinates
* model-space coordinates
* paper-space coordinates
* raster pixels
* engineering units
* normalized internal coordinates

Normalize transformations near adapter boundaries.

Tests should verify transformations.

---

# Scale and Units

Never fabricate physical units.

Possible scale evidence:

* CAD unit metadata
* drawing dimensions
* scale annotation
* viewport scale
* dimension consensus
* known geometry
* user override

Store the evidence used.

If reliable physical scale cannot be established:

report drawing-space area rather than fake `mm²`, `m²`, or `ft²`.

---

# Validation Harness

Continue using:

```bash
python -m tools.validate_drawings samples --out-dir validation
```

For each drawing produce enough evidence to diagnose:

* ingestion
* source type
* selected view
* scale
* units
* classification
* geometry
* footprint
* union
* area
* confidence
* warnings
* overlay

---

# PDF vs CAD Validation

When both representations exist:

```text
CAD Area
vs
PDF Area
```

Calculate:

```text
difference = abs(CAD - PDF) / reference × 100%
```

Investigate material discrepancies.

Do not simply average two conflicting values.

CAD should generally become the preferred authoritative geometry source when its semantics and units are reliable.

---

# Test Discipline

Every meaningful behavior change requires testing.

Workflow:

```text
existing behavior
→ failing regression test
→ implementation
→ targeted test
→ full test suite
```

Do not leave the repository failing.

---

# Current Regression Baseline

Current verified test suite:

```text
99 tests passing
```

Known synthetic reference:

```text
plate_with_holes.pdf

scale ≈ 0.705556 mm/unit
projected area ≈ 23058 mm²
truth ≈ 23057.52 mm²
error ≈ +0.002%
confidence ≈ 96%
```

Rotation-0 results should remain stable unless an intentional engineering improvement changes them.

---

# Rotation

Maintain correctness for:

```text
0°
90°
180°
270°
```

Where engineering geometry is unchanged, calculated area should remain rotation invariant.

Classification should also remain stable where possible.

---

# Git Discipline

Before substantial changes:

```bash
git status
```

Protect existing work.

Do not reset user changes.

Use logical commits.

Examples:

```text
feat(cad): add DXF entity adapter

feat(area): support alternate footprint definitions

fix(pdf): normalize rotated coordinates

feat(ai): add drawing-view candidate classifier

feat(ui): add engineering calculation inspector
```

Do not commit:

* proprietary drawings
* decrypted production files
* secrets
* credentials
* `.env`
* transient validation output unless intentionally required

---

# Security

Engineering drawings may be proprietary.

Prefer local processing.

Do not send drawings to external services without explicit authorization.

Do not attempt to bypass DRM or encryption.

If encrypted input blocks processing, document it and continue all independent work.

---

# Performance

Correctness comes first.

After correctness:

* avoid repeated parsing,
* cache stable intermediate representations,
* parallelize independent files where appropriate,
* avoid unnecessary rasterization,
* reuse normalized geometry,
* profile before major optimization.

---

# No Fake Completion

Never claim completion when:

* code is stubbed,
* tests were not run,
* UI controls do nothing,
* values are hard-coded,
* geometry is mocked,
* an exception is silently swallowed,
* only half of the vertical slice works.

---

# No Unnecessary Stubs

Avoid leaving:

```text
TODO
FIXME
pass
placeholder
temporary fake data
NotImplementedError
```

when the requested functionality can reasonably be completed now.

Prefer complete vertical slices.

---

# Full Vertical Slice Ownership

A feature should flow through all necessary layers:

```text
Input
↓
Domain Model
↓
Engineering Logic
↓
API
↓
Frontend
↓
Visualization
↓
Audit
↓
Test
```

Do not stop at a backend implementation when the requested product capability requires UI integration.

---

# Definition of Done

A capability is complete when applicable items are satisfied:

* engineering definition is clear
* implementation is complete
* architecture remains coherent
* API integration works
* UI integration works
* visual validation works
* tests exist
* targeted tests pass
* full regression suite passes
* error states are handled
* warnings are visible
* confidence is defensible
* documentation reflects reality
* calculations are auditable
* no obvious stub remains

---

# Current Product Priority

Unless instructed otherwise:

```text
1. Correct engineering meaning of projected area
2. Real manufacturing drawing validation
3. CAD / DWG / DXF geometry support
4. Geometry reliability
5. Scale and unit reliability
6. Visual auditability
7. Multiple footprint definitions where justified
8. AI-assisted classification
9. OCR for scanned drawings
10. DXF export
11. Batch analysis
12. UI/UX refinement
13. Localization
14. Cloud production architecture
15. Advanced 3-D CAD processing
```

Do not spend major effort polishing low-priority features while calculation correctness remains unresolved.

---

# Completion Reporting

At the end of substantial work report:

## Completed

What was implemented.

## Engineering Interpretation

What the result physically represents.

## Architecture

Important architectural changes.

## Files Changed

Major files created or modified.

## Tests

```text
X passed
Y failed
```

## Validation

Important real/synthetic validation results.

## AI / Geometry Findings

Important algorithmic observations.

## UI/UX Changes

What changed in the workflow or visualization.

## Behavior Changes

Whether existing calculations changed.

## Known Limitations

Only substantive limitations.

## External Blockers

Anything requiring user/system access.

## Recommended Next Step

The single highest-value next engineering step.

---

# Core Autonomous Rule

When the next action is technically clear, safe, and reversible:

**Do it.**

When there is a bug with a clear correct behavior:

**fix it and test it.**

When the architecture needs a reasonable supporting change:

**make it.**

When the UI is required to make the feature usable:

**build it.**

When AI can improve interpretation but deterministic geometry can verify it:

**use both.**

When evidence contradicts an assumption:

**follow the evidence.**

When there is a genuine external blocker:

**document it precisely, continue everything else possible, and identify exactly what input is required.**

Own the application end-to-end as an advanced CAD engineer, solution architect, AI engineer and scientist, computational-geometry specialist, UI/UX designer, Python engineer, and full-stack developer.

The objective is not merely to make code run.

The objective is to build a **professional, intelligent, defensible engineering application that can understand real manufacturing drawings and produce projected-area measurements users can trust.**
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
.venv/bin/python run.py
```

That is the whole thing — one command, one process, viewer and API on the same
origin. It prints the URL it actually bound to:

```
  Projected Area Analyzer · engine 0.3.0
  ----------------------------------------------------
  Open: http://localhost:8000/
  API docs: http://localhost:8000/docs
  7 demo drawings ready — click "试用样例图纸" to start
```

If 8000 is busy it moves to the next free port and says so. `--port`, `--open`,
`--no-reload` and `--no-demo` are there if you want them.

**No drawing to hand?** Click **试用样例图纸** on the landing page. The demo
drawings run through the *real* pipeline — generated server-side, ingested by the
same store an upload uses, measured by the same geometry code. Nothing about a
demo result is pre-computed, which is why the browser tests can check the numbers
against analytically known areas.

With your own file: **打开文件** → **分析图纸** → pick a view → read the result card.

> Always invoke tools as `.venv/bin/python -m <tool>`. The venv's console
> scripts (`.venv/bin/uvicorn`, `.venv/bin/pytest`) hard-code the interpreter
> path they were installed with, so they break if the project directory is ever
> renamed — as this one was. The module form has no such dependency.

Headless, no browser — one drawing:

```bash
.venv/bin/python -m tools.audit_overlay samples/YOUR-DRAWING.pdf --page 3 --out audit.png
```

Or a whole set, with a comparison table and an overlay per page:

```bash
.venv/bin/python -m tools.validate_drawings samples --out-dir validation
```

Tests:

```bash
.venv/bin/python -m pytest tests -q      # 140 tests, ~42 s
```

Seven of those drive the real page in Chromium — the demo click-through, the
three footprint readings and the overlay switching between them, the layer
toggles, Explain Calculation, and the refusal path. They try the bundled
headless shell, then the bundled Chromium, then a system Chrome, and skip only if
none exists. The other 133 run in about a second.

---

## What was already here, and what changed

| Before | After |
|---|---|
| `cad-area-meter.html` — a working client-side planimeter: PDF.js viewer, magic-wand flood fill, polygon tool, two-point calibration, CSV export | **Kept in full.** Its layout, palette and tools are untouched; a server-analysis panel, a result card and an SVG audit overlay were added alongside them |
| `backend/app.py` — a 238-line prototype in one file | Replaced by a domain-structured engine (below). The prototype mis-read PyMuPDF's item format (`("l", x0, y0, x1, y1)` — the real shape is `("l", Point, Point)`), flattened Béziers from the origin instead of from the curve start, unioned faces without hole handling so every hole was filled, and derived scale from a bare `1:N` regex |
| `tests/test_geometry.py` — 3 tests, one of which could not run (`Point` was never imported) | 99 tests across geometry, calibration, end-to-end pipeline, page rotation, HTTP API, the validation harness and the viewer itself |

The existing UI represents product intent (CONSTITUTION §21) and was preserved
deliberately. The manual wand and polygon tools remain the fallback for drawings
the automatic path cannot handle, and they now share the backend's scale.

---

## The real drawings — still blocked

`Inputs/` holds four production drawings (plus three `.dwg` companions). **None
of them can be opened**, and none has been validated against. They are encrypted
at rest: there is no `%PDF` header and the string `%PDF` appears nowhere in the
file, so this is whole-file encryption rather than a damaged header, and no
repair path exists.

| File | Size | First bytes |
|---|---:|---|
| `101-SSY1070-GR-30PPM-Simplified-V5.1.pdf` | 1.76 MB | `05 87 d5 96` |
| `102-NSY1540- XM-P1-CIR-14JPH-Simplified-V5.1.pdf` | 5.55 MB | `3a 44 4f 25` |
| `103-SSY1667-CTP产线-10JPH-Simplified-V5.1.pdf` | 159 KB | `71 27 06 98` |
| `104-NSY1682-PDM06-CTP-32PPM-Simplified-V5.1.pdf` | 5.46 MB | `7b ff 75 95` |

A readable PDF starts `25 50 44 46`. The three `.dwg` files are wrapped the same
way — none carries the `AC10xx` signature AutoCAD writes.

**The only way through this is to export decrypted copies.** Open each drawing in
the application that owns it and *Save As* / *Export* into `samples/`, then:

```bash
.venv/bin/python -m tools.validate_drawings samples --out-dir validation
```

That runs every stage on every drawing and writes `validation/REPORT.md` — the
comparison table, the per-stage evidence, and an overlay PNG per page showing
exactly which geometry was counted. Nothing is uploaded anywhere.

Until then, the engine's accuracy claims below rest on synthetic fixtures only.
Tolerances have never been exercised against a real sheet, so treat the numbers
as evidence that the *geometry* is sound, not that the *interpretation* is.

In their place the engine is validated against five synthetic CAD-style drawings
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

**Page rotation.** `get_drawings()` and `get_text()` ignore the page's `/Rotate`
and report unrotated media-box coordinates, while `page.rect` reports rotated
ones. Mixing the two means an A3 landscape sheet marked `/Rotate 90` is believed
to be portrait: the sheet frame then measures 138 % of the believed page width
but only 68 % of its height, fails the `sheet` test and is counted as part of the
component. Both readings are therefore mapped through `page.rotation_matrix` in
`pdf/space.py`, so one canonical space — `page.rect`, which is also PDF.js at
`scale = 1` and what `get_pixmap()` rasterises — serves the engine and the
overlay alike. `/Rotate` is always a multiple of 90°, so the map is rigid and
preserves area and winding; on an unrotated page it is the identity.

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
            area/            projected-area orchestration + interpretations
            confidence/      interpretable scoring
            raster/          Path B
            api/             routes + request schemas
            pdf/space.py     canonical page space, /Rotate normalisation
            demo/            synthetic drawings + the demo catalogue
run.py      one-command launcher — serves viewer + API, pre-builds demos
tools/      audit_overlay.py    headless overlay renderer, one page
            validate_drawings.py whole-set validation harness + report
tests/      133 unit/integration + 7 browser tests
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

### Validating a set of drawings

```bash
.venv/bin/python -m tools.validate_drawings samples --out-dir validation
.venv/bin/python -m tools.validate_drawings a.pdf b.pdf --all-pages
```

Runs the whole pipeline over every drawing and writes `validation/REPORT.md`: the
comparison table, then per drawing the full stage trace — primitive and role
counts, scale source and evidence, candidate regions and which was measured,
components and holes, repairs, area, confidence components, every assumption and
every warning — plus a JSON result and an overlay PNG per page.

| Column | Meaning |
|---|---|
| Detected Scale/Units | mm per PDF unit and the implied ratio, or `NOT VERIFIED` |
| Projected Area | the measurement in mm², native units², m² and ft² |
| Bounding W×H | extent of the *counted* geometry, not of the region |
| Bounding Area | axis-aligned box of that extent |
| Utilization | projected ÷ bounding — how much of its envelope the part fills |
| Confidence | overall score, with components in the detail section |
| Warnings | count, expanded verbatim below the table |

Each drawing is then validated stage by stage — **1** ingestion, **2** scale and
units, **3** region detection, **4** geometry classification, **5** area
construction, **6** results, **7** visual QA — with the region table, the body
breakdown and every assumption and warning printed in full.

### Which projected area? — competing definitions

On one machined part "projected area" has a single obvious meaning. On a
**manufacturing-line layout** it does not, and the readings differ by large
factors, so `area/interpretations.py` computes every definition that follows from
geometry alone and names the physical region each one represents:

The measurement **type** is a first-class domain concept — `FootprintType` and
`FootprintInterpretation` in `models.py` — not a generic `projected_area` float.
Each reading carries its own geometry, evidence, confidence, assumptions and
warnings, so the UI can draw it and a CAD-derived definition can be added without
touching the area engine.

| `FootprintType` | What it is | Semantics |
|---|---|---|
| `geometry_union` | every counted profile, merged; claims nothing about meaning | geometric |
| `convex_envelope` | what a crane path or guard enclosure has to clear | geometric |
| `bounding_rectangle` | floor space to allocate, or the crate to ship it in | geometric |
| `enclosing_boundary` | the largest closed loop — **may** be site, cell, floor or line boundary | **provisional** |
| `internal_union` | what sits inside that boundary — **may** be the equipment | **provisional** |
| `conveyor_footprint` | conveying equipment alone | needs **CAD metadata** |
| `guarded_area` | the fenced / light-curtain perimeter | needs **CAD metadata** |
| `line_footprint` | the whole installation as sited | needs **CAD metadata** |

**Why `geometry_union` and not `equipment_union`.** Production evidence
(GLTR-101) settled this: the largest closed loop on a line layout is the *site
boundary*, not a machine — its bounding rectangle matched the sheet's stated
13 200 × 75 000 mm exactly. Calling that "equipment" was arithmetically right and
semantically wrong. `FootprintSemantics` now records how much is actually known:
`geometric` (the definition makes no claim), `provisional` (a reading that is
*not* confirmed, shown with its candidate meanings) or `confirmed` (backed by CAD
metadata or a human). Nothing derived from shape alone is ever `confirmed`.

Every result serialises all of them under `footprint_interpretations`, and the
three CAD-only readings under `pending_interpretations` — reported as *known and
unavailable*, each naming the metadata it needs (layer, block name, linetype,
XREF extent), so the absence is visible rather than silent. That list is also the
specification for the DXF adapter.

On the L-shaped plan fixture these come out 54.46 m², 64.00 m² and 72.00 m² — a
32 % spread. Reporting one of those without saying which would be the hidden
assumption §30 forbids.

`FootprintType.requires_cad_semantics` marks the three that cannot come from
shape, and a test asserts no geometry-derived reading ever claims one of them.
Identifying a fence or a conveyor needs layer, block and linetype information —
it is not a property of the outline and will not be guessed.

A file it cannot open is reported as **BLOCKED** with a reason and a suggested
action; it never becomes a measured `0 mm²`. Pages that fail are isolated, so one
bad page does not lose the rest of the set.

### Using it in the browser

The result card is built around the question *which* area you are being shown:

- **The headline names its reading.** "设备几何并集 · 54.46 m²", not a bare number,
  with the equivalent units under it and one sentence saying what physical region
  that is.
- **Three cards, one per reading.** Click one and the headline, the units, the
  explanation and the drawing overlay all switch to *that* interpretation's own
  geometry. The union keeps its holes; the bounding rectangle is a rectangle.
- **The CAD-only readings are shown greyed out**, each saying "需要 CAD 图层/块语义"
  and what it would mean. Unavailable is not the same as absent, and neither is an
  error.
- **Nine overlay layers** toggle independently: source geometry, measured,
  excluded, dimensions/annotation, holes, the selected footprint, the convex
  envelope, the bounding rectangle, and warning regions. Envelope and rectangle
  draw as dashed outlines *alongside* the selection, so the three can be compared
  on the drawing at once.
- **说明这次计算 · Explain Calculation** traces the real path — source, view,
  scale, units, classification, footprint definition, polygon and holes, union,
  unit conversion, result — followed by the selected reading's own `evidence`,
  `assumptions` and `warnings`, taken from the backend rather than re-derived in
  the browser.
- **工程细节 · Engineering Details** keeps method, region, scale source, segment
  and component counts and the repair log one click away, so the default view
  stays readable.
- **Review recommended** appears when the engine reports an ambiguity — today
  that is the title-block confusion. It says what looks wrong, and "显示该区域"
  outlines the region that triggered it. It never silently corrects anything.

### Auditability

The overlay paints **green** included profile, **red** holes, **grey** ignored
annotation/dimension/sheet linework, **orange** uncertain geometry, and an
**amber dashed** box around the measured region. Every result answers, on its own:

what geometry was used · what was excluded and under which role · what scale, from
where, with what evidence · what was repaired and by how much · how confident, and
which component drove that · what was assumed.

---

## Known limitations

1. **No real drawing has been tested.** All four production files are encrypted
   at rest and cannot be opened; the engine is validated against synthetic
   fixtures only. This is the single largest gap and it gates everything else —
   see *The real drawings — still blocked* above. The validation harness is
   built and tested and runs the moment decrypted copies land in `samples/`.
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
6. **Region labels are orientation-dependent, and the title-block rule is
   weak.** `_classify_region` calls any bottom-right cluster carrying ≥2 text
   spans a title block, and its `sparse` ink guard does not actually
   discriminate — the plate fixture's own ink ratio is 1.108 against a threshold
   of 2.5. Two consequences: a sheet that *displays* upside-down (`/Rotate 180`)
   swaps the part and the title block, and an upright drawing with a view in the
   bottom-right corner would be mislabelled and dropped from the default view
   pick. Measurements are unaffected — they are rotation-invariant and tested as
   such — and the user can always select the region manually. Retuning that
   threshold needs real sheets, so it is deliberately left for validation.
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

1. **Validate the real drawings** — blocked on decrypted exports (above). The
   harness is built and tested; it needs readable files. Everything below is
   speculation until this happens, and the first output should be the comparison
   table plus a judgement on each stage: did region detection find the part, did
   dimension consensus find the scale, does the overlay match the drawing.
2. **Retune against what that shows** — tolerances, the title-block rule
   (limitation 6) and region detection, each change carrying a regression
   fixture distilled from the real sheet (§27).
3. **OCR for scanned sheets** (PaddleOCR or Tesseract, local only) — purely to
   feed dimension text into the existing consensus voter. Geometry stays
   deterministic. The single biggest unlock for Path B.
4. **DXF export** of the verified profile, so a checked outline returns to CAD.
5. **Localise the warning and assumption strings** — a message catalogue in
   `backend/`, keyed so the JSON API keeps stable identifiers.
6. **Projection-convention view inference** (first vs third angle from layout),
   so the view guess does not depend on a caption.
7. **3-D CAD source adapter** (STEP/IGES/STL via CadQuery/OCP): project the solid
   along a chosen direction, take the silhouette, union, and hand the resulting
   2-D polygons to the *same* area and confidence code that serves PDFs. The
   `pdf/` package is already isolated behind that seam (§37).

Deliberately *not* next: OCR before real-file validation. Knowing whether the
current algorithm works on the actual manufacturing drawings is worth more than
another capability on top of an unvalidated one.

---

## Engineering rules this implementation is bound by

`CONSTITUTION.md`, in particular: §2 what projected area means · §3 never
fabricate a measurement · §4 scale must be explicit · §5 vector first · §8 every
result auditable · §10 confidence must be explainable · §16 every repair recorded
· §30 never hide a fallback · §31 errors must be actionable.

The rule that shaped the most code is §3. Refusing to answer is a feature.
