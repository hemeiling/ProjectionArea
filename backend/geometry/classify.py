"""Assign an engineering *role* to every extracted primitive.

CONSTITUTION.md §2: a projected area must not include dimension lines,
centrelines, hatching, annotation or the sheet frame. §30: nothing is thrown
away silently — every primitive keeps its role and a human-readable reason, and
the overlay shows the excluded linework in grey.

The rules here are deliberately conservative. When a primitive is ambiguous it
is marked :attr:`GeometryRole.UNCERTAIN` and *still counted*, but the result
carries a warning and the UI paints it orange so an engineer can overrule it.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from backend.models import BBox, GeometryRole, Point, Primitive, TextItem

#: A primitive counts as "inside text" when this fraction of its bounding box
#: overlaps a padded text box.
_TEXT_OVERLAP_FRACTION = 0.72

#: Arrowheads and dimension terminators are filled shapes no larger than this
#: multiple of the sheet's median text height.
_ARROWHEAD_TEXT_MULTIPLE = 2.2

#: A path whose bounding box spans at least this fraction of the page in both
#: directions is the sheet frame, not the component.
_SHEET_FRAME_SPAN = 0.86

#: Hatching: at least this many parallel, evenly spaced, thin segments sharing
#: one direction inside one neighbourhood.
_HATCH_MIN_LINES = 6
_HATCH_ANGLE_BIN_DEGREES = 3.0
_HATCH_SPACING_CV = 0.30  # coefficient of variation of spacings


def _median_text_height(text_items: Sequence[TextItem]) -> float:
    """Typical annotation size, used as the yardstick for 'small'."""
    heights = [t.bbox.height for t in text_items if t.bbox.height > 0.1]
    if not heights:
        return 7.0
    return float(statistics.median(heights))


def _overlap_fraction(box: BBox, boxes: Sequence[BBox]) -> float:
    """Largest single-box overlap fraction of ``box``.

    Uses the max rather than the sum so two adjacent text boxes cannot combine
    to falsely swallow a long profile line.
    """
    area = box.area
    if area <= 1e-9:
        # Degenerate (a perfectly horizontal/vertical line): use containment.
        for other in boxes:
            if other.contains_point(box.center):
                return 1.0
        return 0.0
    best = 0.0
    for other in boxes:
        if box.intersects(other):
            best = max(best, box.intersection_area(other) / area)
    return best


def _segment_angle(a: Point, b: Point) -> float:
    """Undirected segment angle in degrees, folded into [0, 180)."""
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    return angle


def _detect_hatch(primitives: Sequence[Primitive]) -> set:
    """Indices of primitives that look like section hatching.

    Hatching is a family of parallel, evenly spaced, open, thin segments. The
    test groups candidate segments by direction, projects them onto the shared
    normal, and requires the resulting spacings to be regular.
    """
    candidates: List[Tuple[int, float, Point, Point]] = []
    for prim in primitives:
        if prim.closed or prim.filled or len(prim.points) != 2:
            continue
        a, b = prim.points[0], prim.points[-1]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            continue
        candidates.append((prim.index, _segment_angle(a, b), a, b))

    by_angle: Dict[int, List[Tuple[int, Point, Point]]] = defaultdict(list)
    for index, angle, a, b in candidates:
        by_angle[int(angle // _HATCH_ANGLE_BIN_DEGREES)].append((index, a, b))

    hatched: set = set()
    for bin_key, members in by_angle.items():
        if len(members) < _HATCH_MIN_LINES:
            continue
        angle_rad = math.radians((bin_key + 0.5) * _HATCH_ANGLE_BIN_DEGREES)
        # Normal direction for this family.
        nx, ny = -math.sin(angle_rad), math.cos(angle_rad)
        projected = sorted(
            (0.5 * ((a[0] + b[0]) * nx + (a[1] + b[1]) * ny), index) for index, a, b in members
        )
        run: List[Tuple[float, int]] = [projected[0]]
        for offset, index in projected[1:]:
            spacing_ok = True
            if len(run) >= 2:
                spacings = [run[i][0] - run[i - 1][0] for i in range(1, len(run))]
                mean = statistics.fmean(spacings)
                if mean > 1e-6:
                    gap = offset - run[-1][0]
                    spacing_ok = abs(gap - mean) <= max(_HATCH_SPACING_CV * mean, 1e-6)
            if spacing_ok:
                run.append((offset, index))
                continue
            if len(run) >= _HATCH_MIN_LINES:
                hatched.update(i for _, i in run)
            run = [(offset, index)]
        if len(run) >= _HATCH_MIN_LINES:
            hatched.update(i for _, i in run)
    return hatched


def _polygon_area(points: Sequence[Point]) -> float:
    """Absolute shoelace area of a closed ring."""
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) * 0.5


def classify_primitives(
    primitives: List[Primitive],
    text_items: Sequence[TextItem],
    page_bbox: BBox,
    text_boxes: Optional[Sequence[BBox]] = None,
) -> Dict[str, int]:
    """Set :attr:`Primitive.role` on every primitive, in place.

    Args:
        primitives: Normalised primitives for one page.
        text_items: Text spans, used to size annotation features.
        page_bbox: The page rectangle in PDF units.
        text_boxes: Padded text bounding boxes; computed from ``text_items``
            when omitted.

    Returns:
        A count per role, suitable for the geometry report.
    """
    from backend.pdf.text import text_mask_boxes

    boxes = list(text_boxes) if text_boxes is not None else text_mask_boxes(list(text_items))
    text_height = _median_text_height(text_items)
    hatched = _detect_hatch(primitives)

    page_w = max(page_bbox.width, 1e-6)
    page_h = max(page_bbox.height, 1e-6)

    for prim in primitives:
        box = prim.bbox
        role, reason = GeometryRole.PROFILE, ""

        if prim.dashed:
            # Dashed linework is never part of a silhouette: it is a centreline
            # (long, thin, crossing the part) or a hidden edge behind it.
            if box.diagonal > 6 * text_height:
                role, reason = GeometryRole.CENTERLINE, "dashed long span"
            else:
                role, reason = GeometryRole.HIDDEN, "dashed short span"

        elif box.width / page_w >= _SHEET_FRAME_SPAN and box.height / page_h >= _SHEET_FRAME_SPAN:
            role, reason = GeometryRole.SHEET, "spans the whole sheet (border/frame)"

        elif prim.index in hatched:
            role, reason = GeometryRole.HATCH, "parallel evenly spaced fill lines"

        elif prim.filled and not prim.stroked and box.diagonal <= _ARROWHEAD_TEXT_MULTIPLE * text_height:
            role, reason = GeometryRole.DIMENSION, "small solid marker (arrowhead/terminator)"

        elif _overlap_fraction(box, boxes) >= _TEXT_OVERLAP_FRACTION:
            role, reason = GeometryRole.ANNOTATION, "lies inside a text bounding box"

        elif prim.closed and _polygon_area(prim.points) < 1e-9 and not prim.filled:
            role, reason = GeometryRole.ANNOTATION, "degenerate zero-area path"

        prim.role = role
        prim.role_reason = reason

    counts: Dict[str, int] = defaultdict(int)
    for prim in primitives:
        counts[prim.role.value] += 1
    return dict(counts)


def mark_dimension_linework(
    primitives: List[Primitive],
    dimension_texts: Sequence,
    text_height: float,
    reach_factor: float = 3.0,
) -> int:
    """Demote linework that clearly belongs to a dimension callout.

    A dimension line is a thin open segment whose midpoint sits next to a
    numeric annotation. Extension lines are the short strokes running from the
    part edge out to it. Both are demoted to :attr:`GeometryRole.DIMENSION`.

    This runs *after* :func:`classify_primitives`, once per page, and only
    touches primitives still marked ``PROFILE``, so it can never resurrect
    discarded geometry. It is a page-level judgement — a dimension line is a
    dimension line whichever region is selected — so it belongs in page
    preparation, not in each area calculation.

    Args:
        primitives: Classified primitives.
        dimension_texts: Parsed dimension annotations for the page.
        text_height: Median annotation height, the local length yardstick.
        reach_factor: How far from the text a dimension line may sit, as a
            multiple of ``text_height``.

    Returns:
        Number of primitives demoted.
    """
    if not dimension_texts:
        return 0

    reach = max(reach_factor * text_height, 1.0)
    demoted = 0
    for prim in primitives:
        if prim.role is not GeometryRole.PROFILE or prim.closed or prim.filled:
            continue
        if len(prim.points) > 3:
            continue
        midpoint = prim.bbox.center
        for dim in dimension_texts:
            centre = dim.bbox.center
            if math.hypot(midpoint[0] - centre[0], midpoint[1] - centre[1]) <= reach:
                # Only demote if the segment actually passes near the text, i.e.
                # the text sits on the line rather than merely nearby.
                if _point_to_segment_distance(centre, prim.points[0], prim.points[-1]) <= reach:
                    prim.role = GeometryRole.DIMENSION
                    prim.role_reason = f"dimension line for '{dim.raw}'"
                    demoted += 1
                    break
    return demoted


def _point_to_segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length_sq))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))
