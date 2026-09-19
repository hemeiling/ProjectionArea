# CONSTITUTION.md

## Project Mission

Build a reliable engineering tool that can estimate or calculate the **projected area of a manufactured component from engineering drawing PDFs**, with future support for native CAD formats.

The system must prioritize:

**Accuracy → Explainability → Deterministic Engineering → User Verification → Simplicity → Automation**

The application must never present an engineering measurement as reliable unless the geometry and scale supporting that measurement can be explained.

---

# 1. Core Product Principle

The product is not:

> Upload PDF → AI guesses an area.

The product is:

> Upload engineering drawing → identify relevant geometry → establish physical scale → reconstruct projected profile → calculate area deterministically → visually show what was calculated → allow engineer verification.

Every implementation decision must support this principle.

---

# 2. Projected Area Definition

Projected area is the **2D union of the component silhouette/profile along a selected projection direction or engineering drawing view**.

Do NOT calculate projected area by:

* adding surface areas
* adding all closed shapes on a page
* counting overlapping geometry multiple times
* counting annotation geometry
* counting dimensions
* counting centerlines
* counting title blocks
* counting drawing borders

The geometry engine must distinguish, whenever possible:

* outer profile
* holes
* cutouts
* islands
* overlapping geometry
* construction geometry
* annotation geometry
* dimensions
* centerlines
* hidden lines

Boolean geometry operations must be used where appropriate.

---

# 3. Never Fabricate Measurements

This is a non-negotiable rule.

If physical scale cannot be reliably established:

**DO NOT output a physical projected area such as mm², cm², or in² as if it were valid.**

Instead return something like:

> Scale not verified. Area calculation requires calibration.

The system may calculate:

* PDF-coordinate area
* pixel area
* normalized geometry area

internally, but these values must not be presented as manufacturing dimensions.

---

# 4. Scale Must Be Explicit

Never assume that PDF page coordinates equal manufacturing dimensions.

Physical scale must come from one or more of:

1. verified drawing dimensions
2. drawing scale
3. known reference geometry
4. reliable CAD metadata
5. manual user calibration

Preferred strategy:

**Known drawing dimension → measured PDF distance → derive physical scale**

Example:

```text
Known dimension: 425 mm
PDF distance: 1204.73 units

scale = 425 / 1204.73
      = 0.35277 mm/PDF-unit
```

When possible, verify scale against multiple dimensions.

If dimensions disagree beyond tolerance, flag the drawing.

---

# 5. Vector First

Engineering geometry must be extracted using the highest-quality source available.

Processing priority:

```text
Native CAD geometry
        ↓
Native PDF vector geometry
        ↓
Mixed PDF vector + raster
        ↓
Raster / scanned drawing
```

For PDFs:

**Never rasterize a vector PDF unnecessarily.**

If vector primitives exist, preserve them.

Preferred initial vector pipeline:

```text
PDF
 ↓
PyMuPDF
 ↓
Vector primitive extraction
 ↓
Geometry normalization
 ↓
Contour reconstruction
 ↓
Shapely
 ↓
Polygon operations
 ↓
Area calculation
```

---

# 6. OCR Is Not Geometry

OCR must never be treated as the primary geometry engine.

OCR may be used for:

* dimensions
* view names
* drawing scale
* part numbers
* drawing notes
* units
* title blocks
* revision metadata

Examples:

```text
TOP VIEW
SCALE 1:2
425
Ø18
R25
DIMENSIONS IN mm
```

Geometry should come from vector primitives or computer-vision geometry extraction whenever possible.

---

# 7. AI Is for Understanding, Not Arithmetic

LLMs and vision models may help with:

* understanding drawing structure
* identifying likely engineering views
* identifying title blocks
* interpreting drawing notes
* identifying relevant dimensions
* classifying drawing elements
* assisting with ambiguous geometry

LLMs must NOT be trusted as the authoritative geometry calculator.

Area calculation must use deterministic geometry.

Preferred responsibility separation:

```text
AI
 ↓
"What does this drawing mean?"

Geometry Engine
 ↓
"What geometry exists?"

Math Engine
 ↓
"What is the area?"
```

---

# 8. Every Result Must Be Auditable

A projected-area result must never be a black box.

Whenever possible, visually display:

* geometry included in the area
* geometry excluded from the area
* holes
* ignored dimensions
* ignored annotations
* uncertain geometry

Recommended overlay semantics:

```text
GREEN
Included projected-area geometry

RED
Excluded holes / cutouts

GRAY
Ignored annotations / dimensions

ORANGE
Uncertain geometry requiring review
```

The user must be able to understand:

> Why did the system calculate this area?

---

# 9. Human Verification Is a Feature

Do not try to eliminate the engineer from difficult drawings.

When uncertainty exists, expose it.

Allow users to:

* select drawing view
* select region
* include geometry
* exclude geometry
* add boundary
* remove boundary
* repair contour
* identify hole
* calibrate using two points
* enter known dimension
* choose units
* recalculate

A 90% automated system with excellent verification tools is preferable to a 99% claimed automated system that silently produces incorrect measurements.

---

# 10. Confidence Must Be Explainable

Do not generate arbitrary confidence percentages.

Confidence must be derived from identifiable evidence.

Potential factors include:

```text
Geometry source
Vector > raster

Scale verification
Multiple verified dimensions > one dimension > drawing scale > inference

Contour quality
Closed > repaired > heavily reconstructed

View confidence
Explicit selection > detected view > inferred region

Geometry ambiguity
Low ambiguity > high ambiguity

User verification
Verified > unverified
```

Internally store confidence components.

Example:

```json
{
  "overall_confidence": 0.92,
  "geometry_confidence": 0.97,
  "scale_confidence": 0.95,
  "view_confidence": 0.91,
  "repair_confidence": 0.86
}
```

The UI may display:

```text
Confidence: High
92%
```

with an explanation.

---

# 11. Separate Detection From Calculation

Never mix drawing interpretation and mathematical calculation into one opaque function.

Use a pipeline.

Recommended architecture:

```text
Document
 ↓
Document Classification
 ↓
Page Detection
 ↓
Drawing Region Detection
 ↓
View Detection
 ↓
Geometry Extraction
 ↓
Geometry Classification
 ↓
Scale Detection
 ↓
Geometry Reconstruction
 ↓
Polygon Validation
 ↓
Area Calculation
 ↓
Confidence Evaluation
 ↓
Visualization
```

Each stage should produce inspectable intermediate output.

---

# 12. Preserve Intermediate Geometry

Do not immediately convert everything into a final area number.

Maintain an internal geometry model.

Example:

```text
Document
Page
DrawingRegion
EngineeringView
GeometryPrimitive
Contour
Polygon
Hole
Dimension
Calibration
AreaResult
```

Geometry primitives may include:

```text
Line
Polyline
Arc
Circle
Bezier
Spline
Rectangle
Path
Text
Image
```

This enables debugging and later CAD support.

---

# 13. Geometry Must Be Unit-Aware

Never pass naked numbers through the system when physical units matter.

Prefer explicit structures such as:

```json
{
  "value": 425,
  "unit": "mm"
}
```

and:

```json
{
  "area": 12548,
  "unit": "mm2"
}
```

Supported display conversions should eventually include:

```text
mm²
cm²
m²
in²
ft²
```

The calculation should maintain one canonical internal unit.

Recommended:

```text
millimeters
square millimeters
```

---

# 14. Geometry Operations Must Be Robust

Engineering PDFs frequently contain imperfect geometry.

Expect:

* tiny gaps
* duplicate lines
* overlapping paths
* coincident geometry
* broken arcs
* nearly closed contours
* repeated vectors
* inconsistent path direction
* floating-point errors

Use configurable tolerances.

Do not scatter hardcoded tolerance values throughout the code.

Create centralized geometry settings such as:

```python
GEOMETRY_TOLERANCE
SNAP_TOLERANCE
CLOSURE_TOLERANCE
MIN_CONTOUR_AREA
DUPLICATE_LINE_TOLERANCE
```

---

# 15. Never Destroy Source Geometry

Original extracted geometry must remain available.

Processing should create transformations such as:

```text
raw geometry
     ↓
normalized geometry
     ↓
classified geometry
     ↓
cleaned geometry
     ↓
polygonized geometry
```

Do not overwrite the source geometry.

This allows comparison and debugging.

---

# 16. Every Automatic Repair Must Be Recorded

If the system repairs geometry, record the operation.

Examples:

```text
Closed 0.21 mm contour gap
Removed 17 duplicate segments
Snapped 4 endpoints
Merged 3 overlapping contours
Removed 2 zero-length lines
Repaired self-intersecting polygon
```

The result should retain an audit trail.

Example:

```json
{
  "repairs": [
    {
      "type": "close_gap",
      "distance": 0.21
    }
  ]
}
```

---

# 17. Do Not Treat the Entire Page as One Drawing

Engineering drawings often contain:

* front view
* side view
* top view
* sections
* details
* isometric view
* BOM
* dimensions
* title block

The system must support multiple drawing regions per page.

Preferred object hierarchy:

```text
PDF
 └── Page
      ├── Region
      │    └── View
      ├── Region
      │    └── View
      └── TitleBlock
```

Area calculation must operate on a selected region/view.

---

# 18. Prefer Deterministic Algorithms

If a problem can be solved reliably using computational geometry, do not replace it with AI.

Examples:

Use:

```text
Shapely polygon union
```

instead of:

```text
Ask LLM whether two polygons overlap
```

Use:

```text
distance formula
```

instead of:

```text
Ask vision model how long a line appears
```

Use:

```text
vector path extraction
```

instead of:

```text
OCR image tracing
```

AI should complement engineering algorithms, not replace them.

---

# 19. PDF Processing Strategy

When a PDF is uploaded, first classify it.

Expected classification:

```text
VECTOR
RASTER
MIXED
UNKNOWN
```

For vector PDFs:

```text
PyMuPDF
→ Page.get_drawings()
→ extract paths
```

For raster PDFs:

```text
PDF renderer
→ high-resolution image
→ OpenCV
→ contour/vector extraction
```

For mixed PDFs:

Use the strongest available source for each feature.

---

# 20. Initial Technology Direction

Unless repository constraints strongly justify something else:

## Backend

```text
Python
FastAPI
```

## PDF

```text
PyMuPDF
```

## Geometry

```text
Shapely
```

## Computer Vision

```text
OpenCV
```

## Frontend PDF Viewer

```text
PDF.js
```

## Geometry Overlay

```text
SVG
or
Canvas
```

## Future CAD

```text
CadQuery
OCP / OpenCascade
```

Avoid introducing large frameworks unless they solve a demonstrated requirement.

---

# 21. Existing UI Must Be Respected

The existing mock HTML represents product intent.

Do not unnecessarily replace it.

When adding functionality:

1. inspect existing design
2. preserve layout where reasonable
3. reuse components
4. add capabilities incrementally
5. avoid visual redesign unless required

Engineering functionality takes priority over framework preference.

---

# 22. Backend Must Own Engineering Calculations

The frontend may visualize and interact with geometry.

However, authoritative engineering calculations should occur on the backend.

Do not make browser JavaScript the only implementation of critical geometry calculations.

Recommended:

```text
Frontend
    ↓
interaction

Backend
    ↓
geometry processing
    ↓
authoritative result
```

---

# 23. API Design

Prefer small domain-specific APIs.

Possible endpoints:

```text
POST /api/documents
GET  /api/documents/{id}

GET  /api/documents/{id}/pages

POST /api/pages/{id}/analyze

GET  /api/pages/{id}/regions

POST /api/regions/{id}/calibrate

POST /api/regions/{id}/calculate-area

GET  /api/results/{id}
```

Do not create dozens of endpoints before the first vertical workflow works.

---

# 24. Preserve Processing Metadata

Every area calculation should retain:

```text
source file
page
drawing region
selected view
geometry source
scale source
units
calibration
geometry modifications
calculation method
software version
timestamp
confidence
warnings
```

This is necessary for reproducibility.

---

# 25. Testing Is Mandatory

Geometry calculations must have automated tests.

Minimum test fixtures:

### Rectangle

```text
100 mm × 50 mm

Expected:
5000 mm²
```

### Circle

```text
Diameter: 100 mm

Expected:
7853.9816 mm²
```

### Plate with hole

```text
100 × 100 mm

Hole:
Ø20 mm

Expected:
9685.8407 mm²
```

### Overlapping geometry

Union must not double-count overlap.

### Multiple holes

All valid holes must be subtracted.

### Broken contour

Repair behavior must be tested.

### Duplicate geometry

Duplicate vectors must not inflate area.

Tests should use tolerances appropriate for geometry operations.

---

# 26. Real Drawing Testing Is Mandatory

Synthetic tests are necessary but insufficient.

The project must maintain a set of representative engineering PDFs.

For every meaningful algorithm change:

```text
run synthetic tests
+
run representative PDF tests
```

Where ground truth is available, compare results numerically.

---

# 27. Never Optimize Before Measuring

Do not prematurely introduce:

* distributed processing
* queues
* microservices
* GPU infrastructure
* vector databases
* complex agent frameworks
* Kubernetes
* unnecessary cloud services

First prove:

```text
one PDF
→ one correct view
→ correct geometry
→ correct scale
→ correct area
```

Then scale.

---

# 28. Build Vertical Slices

Development should proceed through working vertical slices.

## Milestone 1

```text
Upload PDF
→ render PDF
```

## Milestone 2

```text
Upload PDF
→ detect vector/raster
```

## Milestone 3

```text
Extract vector geometry
→ overlay geometry
```

## Milestone 4

```text
Select drawing region
```

## Milestone 5

```text
Calibrate scale
```

## Milestone 6

```text
Reconstruct polygon
```

## Milestone 7

```text
Calculate area
```

## Milestone 8

```text
Display confidence + warnings
```

Do not build twenty unfinished features simultaneously.

---

# 29. Development Autonomy

The coding agent should operate as a senior engineer.

Do NOT stop for permission for routine engineering decisions.

The agent may autonomously:

* inspect repository
* read code
* run tests
* install reasonable dependencies
* create modules
* refactor code
* fix bugs
* add tests
* add logging
* improve error handling
* update documentation
* simplify architecture

Ask the user only when:

* the requested behavior is genuinely ambiguous
* an irreversible/destructive action is required
* credentials or external access are required
* competing product decisions materially change the product

Otherwise:

**Make the best engineering decision and proceed.**

---

# 30. Do Not Hide Failures

Never silently fall back to a weaker calculation method.

Example:

If vector extraction fails and raster analysis is used, return:

```text
Method:
Raster geometry extraction

Warning:
Native vector geometry could not be extracted.
```

If scale is inferred instead of verified, say so.

Transparency is more important than appearing successful.

---

# 31. Errors Must Be Actionable

Avoid generic errors such as:

```text
Processing failed.
```

Prefer:

```text
No closed component contour could be reconstructed.

Detected:
847 vector segments
13 candidate contours
0 valid closed component profiles

Suggested action:
Select the component boundary manually or increase contour closure tolerance.
```

---

# 32. Logging

Engineering processing stages should produce structured logs.

Useful fields:

```text
document_id
page_id
region_id
processing_stage
primitive_count
contour_count
polygon_count
scale_source
confidence
duration
warnings
errors
```

Do not log sensitive documents unnecessarily.

---

# 33. Performance Principle

Accuracy comes before speed.

But avoid obviously inefficient processing.

Examples:

Do not:

* OCR every page if text extraction works
* rasterize vector drawings unnecessarily
* repeatedly reprocess unchanged files
* perform geometry calculations on irrelevant page regions

Cache deterministic intermediate results where appropriate.

---

# 34. User Data Preservation

Application updates must never delete user-generated data unless explicitly required.

Database migrations should:

* preserve existing records
* be backward compatible where practical
* use migrations
* avoid destructive reset operations
* never drop production tables casually

Never solve a schema problem by deleting the database.

---

# 35. Security

Uploaded engineering drawings may contain proprietary information.

Design accordingly.

Do not:

* send documents to external AI APIs without explicit architectural justification
* expose uploads publicly
* log raw document content unnecessarily
* store temporary files indefinitely

Where AI services are eventually used, make external transmission explicit and configurable.

---

# 36. Future CAD Compatibility

Do not over-engineer CAD support now, but avoid architectural decisions that make it impossible.

Future formats may include:

```text
STEP
STP
IGES
IGS
DXF
STL
DWG
```

Future CAD pipeline may become:

```text
CAD file
 ↓
OpenCascade / OCP
 ↓
3D solid
 ↓
projection direction
 ↓
silhouette
 ↓
2D polygon union
 ↓
projected area
```

PDF remains the MVP priority.

---

# 37. Source Adapter Pattern

Document formats should eventually enter through format-specific adapters.

Conceptually:

```text
DocumentSource
 ├── PdfSource
 ├── StepSource
 ├── DxfSource
 └── StlSource
```

Each adapter should normalize data into a common geometry model.

Do not deeply couple the entire application to PyMuPDF.

---

# 38. Engineering Calculation Boundary

The application must maintain a clear boundary between:

```text
Document interpretation
Geometry reconstruction
Engineering calculation
UI presentation
```

These should not become one giant function.

Avoid:

```python
process_pdf_and_calculate_everything()
```

Prefer domain-oriented modules.

Example:

```text
pdf/
geometry/
calibration/
views/
area/
confidence/
api/
```

---

# 39. Code Quality

Prefer:

* readable code
* explicit names
* typed interfaces
* small domain-focused functions
* unit tests
* deterministic behavior
* useful comments for mathematical logic

Avoid:

* giant files
* magic numbers
* duplicated geometry logic
* deeply nested conditionals
* hidden global state
* unnecessary abstractions
* speculative architecture

---

# 40. Mathematical Precision

Do not round during internal calculations.

Maintain full numerical precision.

Round only for display.

Example:

Internal:

```text
12548.39281731 mm²
```

Display:

```text
12,548.39 mm²
125.48 cm²
19.45 in²
```

---

# 41. Result Structure

A projected-area result should eventually resemble:

```json
{
  "projected_area": {
    "value": 12548.39281731,
    "unit": "mm2"
  },
  "view": {
    "type": "top",
    "source": "user_selected"
  },
  "scale": {
    "value": 0.35277,
    "unit": "mm_per_pdf_unit",
    "source": "dimension_calibration"
  },
  "geometry": {
    "outer_contours": 1,
    "holes": 3,
    "ignored_objects": 84,
    "repairs": 1
  },
  "confidence": {
    "overall": 0.92,
    "geometry": 0.95,
    "scale": 0.97,
    "view": 1.0
  },
  "warnings": []
}
```

Exact implementation may evolve, but calculations must remain inspectable.

---

# 42. Definition of Done

A feature is NOT done because:

* code compiles
* API returns 200
* UI renders
* AI produced an answer

For engineering calculation features, "done" requires:

```text
implementation
+
visual verification
+
automated test
+
known limitations
+
error handling
```

For projected-area calculation specifically:

A user must be able to answer:

1. What geometry was used?
2. What geometry was excluded?
3. What scale was used?
4. Where did the scale come from?
5. What area was calculated?
6. How confident is the system?
7. What assumptions were made?

If these cannot be answered, the feature is not complete.

---

# 43. First Success Criterion

Before expanding the platform:

Take one real engineering PDF and successfully demonstrate:

```text
PDF upload
        ↓
page rendering
        ↓
vector/raster classification
        ↓
drawing region selection
        ↓
geometry extraction
        ↓
geometry overlay
        ↓
scale calibration
        ↓
profile reconstruction
        ↓
projected-area calculation
        ↓
visible verification
        ↓
result + confidence + warnings
```

This working end-to-end example is more valuable than a large unfinished architecture.

---

# 44. Agent Operating Rule

Whenever there is a conflict between:

**making the demo appear successful**

and

**maintaining engineering correctness**

choose engineering correctness.

Whenever there is a conflict between:

**complex architecture**

and

**a simpler deterministic solution**

choose the simpler solution.

Whenever there is a conflict between:

**AI inference**

and

**reliable geometric computation**

choose geometric computation.

Whenever there is uncertainty:

**Expose it. Do not hide it.**

---

# 45. Guiding Principle

The goal is not simply to calculate a number.

The goal is to produce an engineering measurement that a user can:

**see, understand, verify, reproduce, and trust.**
