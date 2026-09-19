"""Extract native PDF vector primitives with PyMuPDF and normalise them.

CONSTITUTION.md §5 (vector first) and §15 (never destroy source geometry).

``Page.get_drawings()`` hands back one dict per *path*, each holding a list of
items already resolved into page space:

===========  ========================================================
``("l",  p1, p2)``          straight segment
``("c",  p0, p1, p2, p3)``  cubic Bezier, ``p0`` start and ``p3`` end
``("re", rect, orient)``    axis-aligned rectangle, its own closed subpath
``("qu", quad)``            quadrilateral, its own closed subpath
===========  ========================================================

A single path may contain several disconnected subpaths (a ``m`` operator
starts a new one). PyMuPDF does not mark those boundaries, so we recover them
geometrically: an item whose start point does not coincide with the previous
item's end point begins a new subpath. Getting this wrong is the classic bug —
it welds unrelated linework into one polygon and silently inflates area.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.config import Tolerances
from backend.models import Point, Primitive, PrimitiveKind

#: Tolerance for deciding that two path items are actually connected. This is
#: about PDF coordinate round-tripping, not about drawing gaps, so it is a tiny
#: absolute value rather than a page-scaled one.
_JOIN_EPSILON = 1e-6


def _pt(p: Any) -> Point:
    """Convert a PyMuPDF Point (or any 2-sequence) to a plain tuple."""
    return (float(p.x), float(p.y)) if hasattr(p, "x") else (float(p[0]), float(p[1]))


def _same(a: Point, b: Point, eps: float = _JOIN_EPSILON) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def flatten_cubic(
    p0: Point, p1: Point, p2: Point, p3: Point, flatness: float, min_points: int
) -> List[Point]:
    """Flatten one cubic Bezier into a polyline, excluding the start point.

    Adaptive subdivision on the standard control-polygon flatness test: the
    curve is accepted as a straight chord once both control points lie within
    ``flatness`` of the chord. ``min_points`` forces a floor on the sample count
    so a small circular hole never collapses into a triangle.

    Args:
        p0: Curve start.
        p1: First control point.
        p2: Second control point.
        p3: Curve end.
        flatness: Maximum allowed chord deviation, PDF units.
        min_points: Minimum samples to emit for this curve.

    Returns:
        Points from just after ``p0`` through ``p3`` inclusive.
    """
    points: List[Point] = []

    def recurse(a: Point, b: Point, c: Point, d: Point, depth: int) -> None:
        if depth >= 16 or _flat_enough(a, b, c, d, flatness):
            points.append(d)
            return
        # de Casteljau split at t = 0.5
        ab = _mid(a, b)
        bc = _mid(b, c)
        cd = _mid(c, d)
        abc = _mid(ab, bc)
        bcd = _mid(bc, cd)
        abcd = _mid(abc, bcd)
        recurse(a, ab, abc, abcd, depth + 1)
        recurse(abcd, bcd, cd, d, depth + 1)

    recurse(p0, p1, p2, p3, 0)

    if len(points) < min_points:
        # Uniform resample; cheap and only hit on short, tightly-curved spans.
        points = [_cubic_at(p0, p1, p2, p3, i / min_points) for i in range(1, min_points + 1)]
        points[-1] = p3
    return points


def _mid(a: Point, b: Point) -> Point:
    return (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))


def _cubic_at(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    w0, w1, w2, w3 = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (
        w0 * p0[0] + w1 * p1[0] + w2 * p2[0] + w3 * p3[0],
        w0 * p0[1] + w1 * p1[1] + w2 * p2[1] + w3 * p3[1],
    )


def _flat_enough(p0: Point, p1: Point, p2: Point, p3: Point, flatness: float) -> bool:
    """Distance of both control points from the chord p0->p3."""
    dx, dy = p3[0] - p0[0], p3[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        # Degenerate chord (loop): fall back to control-point spread.
        return max(_dist(p0, p1), _dist(p0, p2)) <= flatness
    d1 = abs((p1[0] - p0[0]) * dy - (p1[1] - p0[1]) * dx) / chord
    d2 = abs((p2[0] - p0[0]) * dy - (p2[1] - p0[1]) * dx) / chord
    return max(d1, d2) <= flatness


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _is_dashed(dashes: Optional[str]) -> bool:
    """PyMuPDF reports the dash array as a string; ``'[] 0'`` means solid."""
    if not dashes:
        return False
    inside = dashes.split("]")[0].lstrip("[").strip()
    if not inside:
        return False
    try:
        return any(float(token) > 0 for token in inside.replace(",", " ").split())
    except ValueError:
        return False


class _SubpathBuilder:
    """Accumulates connected items into subpaths."""

    def __init__(self) -> None:
        self.subpaths: List[Tuple[List[Point], bool, PrimitiveKind]] = []
        self._points: List[Point] = []
        self._kinds: set = set()

    def start(self, first: Point) -> None:
        self.flush()
        self._points = [first]
        self._kinds = set()

    def extend(self, points: Sequence[Point], kind: PrimitiveKind) -> None:
        self._points.extend(points)
        self._kinds.add(kind)

    def add_closed(self, points: Sequence[Point], kind: PrimitiveKind) -> None:
        self.flush()
        self.subpaths.append((list(points), True, kind))

    def flush(self) -> None:
        if len(self._points) >= 2:
            closed = _same(self._points[0], self._points[-1], 1e-9)
            if PrimitiveKind.BEZIER in self._kinds and len(self._kinds) > 1:
                kind = PrimitiveKind.CURVE_CHAIN
            elif PrimitiveKind.BEZIER in self._kinds:
                kind = PrimitiveKind.BEZIER
            elif len(self._points) == 2:
                kind = PrimitiveKind.LINE
            else:
                kind = PrimitiveKind.POLYLINE
            self._points = _dedupe_consecutive(self._points)
            if len(self._points) >= 2:
                self.subpaths.append((self._points, closed, kind))
        self._points = []
        self._kinds = set()

    @property
    def current_end(self) -> Optional[Point]:
        return self._points[-1] if self._points else None


def _ring_area(points: Sequence[Point]) -> float:
    """Absolute shoelace area of a ring, used to spot degenerate closures."""
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) * 0.5


def normalise_degenerate_closure(
    points: List[Point], closed: bool, kind: PrimitiveKind
) -> Tuple[List[Point], bool, PrimitiveKind]:
    """Reopen a "closed" path that encloses no area.

    Many producers close every subpath unconditionally, so a plain two-point
    line arrives as ``A -> B -> A``: flagged closed, three points, zero area.
    Left alone it fails every open-line test downstream — hatch detection stops
    seeing hatching, dimension-line matching stops seeing dimension lines — and
    it contributes a doubled segment to the network.

    Returns:
        ``(points, closed, kind)`` with the closure undone when it was spurious.
    """
    if not closed or len(points) < 3:
        return points, closed, kind
    if _ring_area(points) > 1e-9:
        return points, closed, kind
    if _same(points[0], points[-1], 1e-9):
        points = points[:-1]
    # Collapse the out-and-back into the outward leg only.
    half = (len(points) + 1) // 2
    if len(points) >= 3 and _dedupe_consecutive(points) == _dedupe_consecutive(
        points[:half] + list(reversed(points[:half]))[1:]
    ):
        points = points[:half]
    if len(points) < 2:
        return points, False, kind
    return points, False, (PrimitiveKind.LINE if len(points) == 2 else PrimitiveKind.POLYLINE)


def _dedupe_consecutive(points: Sequence[Point], eps: float = 1e-9) -> List[Point]:
    out: List[Point] = [points[0]]
    for p in points[1:]:
        if not _same(out[-1], p, eps):
            out.append(p)
    return out


def path_to_primitives(
    path: Dict[str, Any], tolerances: Tolerances, path_index: int, start_index: int
) -> List[Primitive]:
    """Convert one ``get_drawings()`` path dict into normalised primitives.

    Args:
        path: One entry from ``Page.get_drawings()``.
        tolerances: Page-scaled tolerances; supplies the Bezier flatness.
        path_index: Index of this path within the page, kept for traceability.
        start_index: Running primitive counter, so indices are page-unique.

    Returns:
        One :class:`Primitive` per subpath. Empty if the path carries no usable
        geometry.
    """
    fill_type = path.get("type") or ""
    stroked = "s" in fill_type
    filled = "f" in fill_type
    line_width = float(path.get("width") or 0.0)
    dashed = _is_dashed(path.get("dashes"))
    color = tuple(path["color"]) if path.get("color") else None
    fill_color = tuple(path["fill"]) if path.get("fill") else None
    layer = path.get("layer") or None
    force_close = bool(path.get("closePath"))

    builder = _SubpathBuilder()

    for item in path.get("items", []):
        op = item[0]
        if op == "l":
            a, b = _pt(item[1]), _pt(item[2])
            if builder.current_end is None or not _same(builder.current_end, a):
                builder.start(a)
            builder.extend([b], PrimitiveKind.LINE)
        elif op == "c":
            p0, p1, p2, p3 = (_pt(item[i]) for i in range(1, 5))
            if builder.current_end is None or not _same(builder.current_end, p0):
                builder.start(p0)
            builder.extend(
                flatten_cubic(p0, p1, p2, p3, tolerances.bezier_flatness, tolerances.arc_min_points),
                PrimitiveKind.BEZIER,
            )
        elif op == "re":
            rect = item[1]
            x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
            orientation = item[2] if len(item) > 2 else 1
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if orientation == -1:
                corners.reverse()
            builder.add_closed(corners + [corners[0]], PrimitiveKind.RECT)
        elif op == "qu":
            quad = item[1]
            corners = [_pt(quad.ul), _pt(quad.ur), _pt(quad.lr), _pt(quad.ll)]
            builder.add_closed(corners + [corners[0]], PrimitiveKind.QUAD)
        # Unknown operators are ignored rather than guessed at.

    builder.flush()

    primitives: List[Primitive] = []
    for points, closed, kind in builder.subpaths:
        points, closed, kind = normalise_degenerate_closure(points, closed, kind)
        if len(points) < 2:
            continue
        if force_close and not closed and len(points) > 2:
            points = points + [points[0]]
            closed = True
        primitives.append(
            Primitive(
                index=start_index + len(primitives),
                kind=kind,
                points=points,
                closed=closed,
                stroked=stroked,
                filled=filled,
                line_width=line_width,
                dashed=dashed,
                color=color,
                fill_color=fill_color,
                layer=layer,
                path_index=path_index,
            )
        )
    return primitives


def extract_primitives(page: Any, tolerances: Tolerances) -> List[Primitive]:
    """Extract every vector primitive on a page, in page coordinates.

    Coordinates come back in PyMuPDF page space: origin top-left, y increasing
    downwards, units of 1/72 inch. This matches PDF.js viewport coordinates at
    ``scale = 1``, so overlays align with no axis flip.

    Args:
        page: A ``fitz.Page``.
        tolerances: Page-scaled tolerances.

    Returns:
        Normalised primitives in drawing order.
    """
    primitives: List[Primitive] = []
    for path_index, path in enumerate(page.get_drawings()):
        primitives.extend(path_to_primitives(path, tolerances, path_index, len(primitives)))
    return primitives
