"""The demo drawings offered in the UI, and where they are cached.

CONSTITUTION.md §3 and §30. A demo exists so the product can be exercised
without hunting for a file — it must therefore run the **real** pipeline. Nothing
here returns a stored answer: :func:`ensure_drawing` writes a PDF and the caller
ingests it exactly as it would an upload, so a demo result is produced by the
same geometry, calibration and confidence code as any other.

``expected`` is documentation, never a value the engine is given. It records what
the drawing was *constructed* to contain so a user can tell at a glance whether
the measurement agrees, which is the whole point of a demo on known geometry.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from backend.demo import drawings as builders


@dataclass(frozen=True)
class DemoDrawing:
    """One drawing offered on the landing page.

    Attributes:
        id: Stable identifier used in the URL.
        title: Short name shown on the button.
        summary: What the drawing is.
        exercises: What it puts the pipeline through — why it is worth trying.
        expected: Human-readable truth, for comparison against the measurement.
        build: Writes the PDF to a path and returns its ground-truth dict.
        page: Page to open on.
    """

    id: str
    title: str
    summary: str
    exercises: str
    expected: str
    build: Callable[[str], Dict[str, object]]
    page: int = 1

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "exercises": self.exercises,
            "expected": self.expected,
            "page": self.page,
            "file_name": f"{self.id}.pdf",
        }


def _plate(path: str) -> Dict[str, object]:
    return builders.build_plate_with_holes(path)


def _two_views(path: str) -> Dict[str, object]:
    return builders.build_two_views(path)


def _layout(path: str) -> Dict[str, object]:
    return builders.build_layout_1_100(path)


def _obround(path: str) -> Dict[str, object]:
    return builders.build_obround_with_slot(path)


def _broken(path: str) -> Dict[str, object]:
    return builders.build_broken_contour(path)


def _rotated(path: str) -> Dict[str, object]:
    source = ensure_drawing("plate_with_holes")
    return builders.build_rotated_plate(path, source, 90)


def _scanned(path: str) -> Dict[str, object]:
    source = ensure_drawing("plate_with_holes")
    return builders.build_raster_plate(path, source)


#: Ordered as a tour: start with the clean case, end with the refusal.
CATALOGUE: List[DemoDrawing] = [
    DemoDrawing(
        id="plate_with_holes",
        title="Plate with three holes",
        summary="A 200 × 120 mm plate with three Ø20 holes, drawn 1:2 on A3 landscape.",
        exercises=(
            "The clean case: native vector geometry, scale recovered from the dimension "
            "annotations, holes subtracted, sheet frame and title block ignored."
        ),
        expected="23 057.52 mm² net (24 000 mm² gross, three Ø20 holes), scale 1:2",
        build=_plate,
    ),
    DemoDrawing(
        id="layout_1_100",
        title="Line layout, 1:100",
        summary="An L-shaped cell footprint on a 1:100 plan — the closest fixture to a real line layout.",
        exercises=(
            "Metre-scale calibration and the case where the footprint definition matters: "
            "union, convex envelope and bounding rectangle differ by 32 %."
        ),
        expected="54.46 m² union · 64.00 m² convex envelope · 72.00 m² bounding rectangle",
        build=_layout,
    ),
    DemoDrawing(
        id="two_views",
        title="Two views on one sheet",
        summary="A top view and a hatched front view of the same part, side by side.",
        exercises=(
            "Region detection has to separate the two views and offer a choice; section "
            "hatching must be classified out rather than measured."
        ),
        expected="14 982.12 mm² (top view) · 6 400.00 mm² (front view)",
        build=_two_views,
    ),
    DemoDrawing(
        id="obround_with_slot",
        title="Curved profile, dimensions through the part",
        summary="An obround with Bézier arcs whose dimension lines cross the face itself.",
        exercises=(
            "The deliberately nasty one: dimension linework drawn across the part would "
            "slice the face into fragments if it were not demoted first."
        ),
        expected="17 354.87 mm², within about 0.05 % after curve flattening",
        build=_obround,
    ),
    DemoDrawing(
        id="broken_contour",
        title="Broken outline (0.9 mm gap)",
        summary="A profile whose outline does not quite close, as CAD exports often emit.",
        exercises=(
            "Gap closure and escalation. The result is reported with a lower confidence "
            "and a warning naming the repair — it is never silently patched."
        ),
        expected="12 793.14 mm², gap-closed and warned about",
        build=_broken,
    ),
    DemoDrawing(
        id="rotated_plate_90",
        title="Rotated sheet (/Rotate 90)",
        summary="The same plate re-issued with a page rotation, as a rotated export or Acrobat gives you.",
        exercises=(
            "Coordinate normalisation. The measurement must be identical to the upright "
            "sheet; the drawing simply displays sideways."
        ),
        expected="23 057.52 mm² — identical to the upright plate",
        build=_rotated,
    ),
    DemoDrawing(
        id="raster_plate",
        title="Scanned page (no text layer)",
        summary="The plate drawing flattened to a bitmap, with no extractable dimension text.",
        exercises=(
            "The refusal. With no text there is no scale, so the tool reports no "
            "millimetres at all rather than guessing — calibrate two points to measure it."
        ),
        expected="Refuses to report mm² until you calibrate. This is correct behaviour.",
        build=_scanned,
    ),
]

BY_ID: Dict[str, DemoDrawing] = {d.id: d for d in CATALOGUE}

#: Generated PDFs are cached here for the process's lifetime. They are derived
#: files, cheap to rebuild, and never mixed in with the user's own uploads.
_CACHE_DIR: Optional[str] = None

#: Ground truth captured when a drawing was built, keyed by id.
_TRUTH: Dict[str, Dict[str, object]] = {}


def cache_dir() -> str:
    """Directory holding generated demo PDFs, created on first use."""
    global _CACHE_DIR
    if _CACHE_DIR is None or not os.path.isdir(_CACHE_DIR):
        _CACHE_DIR = tempfile.mkdtemp(prefix="projected-area-demo-")
    return _CACHE_DIR


def ensure_drawing(demo_id: str) -> str:
    """Path to the demo PDF, generating it once and caching it.

    Args:
        demo_id: An id from :data:`CATALOGUE`.

    Returns:
        Absolute path to a readable PDF.

    Raises:
        KeyError: If ``demo_id`` is not in the catalogue.
    """
    demo = BY_ID[demo_id]
    path = os.path.join(cache_dir(), f"{demo.id}.pdf")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _TRUTH[demo.id] = demo.build(path)
    return path


def ground_truth(demo_id: str) -> Dict[str, object]:
    """What the drawing was constructed to contain. Documentation, not input."""
    if demo_id not in _TRUTH:
        ensure_drawing(demo_id)
    return dict(_TRUTH.get(demo_id, {}))


def build_all(directory: Optional[str] = None) -> Dict[str, str]:
    """Generate every demo drawing up front, so the first click is instant.

    Args:
        directory: Where to write them. Defaults to the process cache.

    Returns:
        ``{id: path}`` for every drawing in the catalogue.
    """
    global _CACHE_DIR
    if directory:
        os.makedirs(directory, exist_ok=True)
        _CACHE_DIR = directory
    return {demo.id: ensure_drawing(demo.id) for demo in CATALOGUE}
