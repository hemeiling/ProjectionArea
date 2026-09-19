"""Competing definitions of "projected area", each computed and named.

CONSTITUTION.md §2 (what projected area *means*) and §30 (never hide a choice).

On a single machined part "projected area" has one obvious reading: the union of
the silhouette. On a **manufacturing line layout** it does not. The same sheet
can defensibly yield any of:

    union of equipment        the material actually occupied by machines
    bounding rectangle        the rectangle the line has to be shipped/sited in
    convex hull              the envelope a crane or guard has to clear
    largest single body       one machine, when the sheet holds several
    per-body list            so any subset can be summed

Those differ by large factors — a sparse line can fill under a third of its own
bounding box — so reporting one number without saying which definition produced
it is exactly the "hidden assumption" §30 forbids. This module computes every
definition that follows from geometry alone and labels each with the physical
region it represents, so the engineer chooses rather than the tool.

**What is deliberately not here.** "Conveyor footprint", "safety fence
perimeter" and "total line footprint" are *semantic* selections: they need to
know which linework is a fence and which is a conveyor. That information is not
in the geometry — it lives in CAD layers and linetypes, or in a human pick. Those
interpretations are therefore left to the layer-aware path and to manual region
selection, and are never guessed at from shape alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

from backend.models import AreaResult, Point

#: A ring needs at least this many distinct points to bound anything.
_MIN_RING = 3


@dataclass
class Interpretation:
    """One defensible reading of "the projected area of this drawing".

    Attributes:
        key: Stable identifier for the JSON record.
        label: Short human name.
        means: The physical region this number represents, in one sentence.
        area_units2: Area in the drawing's own squared units.
        area_mm2: Same area in mm², or ``None`` when the scale is unverified.
        basis: How it was computed, so the number is reproducible.
    """

    key: str
    label: str
    means: str
    area_units2: float
    area_mm2: Optional[float]
    basis: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "means": self.means,
            "area_units2": self.area_units2,
            "area_mm2": self.area_mm2,
            "basis": self.basis,
        }


def _polygon(outer: Sequence[Point], holes: Sequence[Sequence[Point]]) -> Optional[Polygon]:
    if len(outer) < _MIN_RING:
        return None
    try:
        polygon = Polygon(outer, [h for h in holes if len(h) >= _MIN_RING])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon if not polygon.is_empty else None
    except Exception:
        return None


def included_polygons(result: AreaResult) -> List[Polygon]:
    """Shapely polygons for every counted component, holes included."""
    polygons: List[Polygon] = []
    for component in result.components:
        if not component.included:
            continue
        polygon = _polygon(component.outer, component.holes)
        if polygon is not None:
            polygons.append(polygon)
    return polygons


def _to_mm2(area_units2: float, result: AreaResult) -> Optional[float]:
    if not result.scale.verified:
        return None
    return area_units2 * (result.scale.mm_per_unit ** 2)


def interpretations(result: AreaResult) -> List[Interpretation]:
    """Every geometry-derived reading of the result, strongest evidence first.

    Returns an empty list when nothing was reconstructed — there is then no
    region to interpret, and §3 forbids presenting zero as a measurement.
    """
    polygons = included_polygons(result)
    if not polygons:
        return []

    union = unary_union(polygons)
    points: List[Point] = []
    for component in result.components:
        if component.included:
            points.extend(component.outer)

    out: List[Interpretation] = [
        Interpretation(
            key="equipment_union",
            label="Union of equipment geometry",
            means=(
                "The material actually occupied by the bodies drawn in the selected "
                "view, overlaps counted once and enclosed holes removed."
            ),
            area_units2=result.area_units2,
            area_mm2=result.area_mm2,
            basis="union of validated closed profiles; the engine's primary result",
        )
    ]

    hull = MultiPoint(points).convex_hull if len(points) >= _MIN_RING else None
    if hull is not None and hasattr(hull, "area") and hull.area > 0:
        out.append(
            Interpretation(
                key="convex_hull",
                label="Convex envelope",
                means=(
                    "The smallest convex region containing every counted body — what "
                    "a gantry, crane path or guard enclosure has to clear."
                ),
                area_units2=hull.area,
                area_mm2=_to_mm2(hull.area, result),
                basis="convex hull of all included outer rings",
            )
        )

    bounds = union.bounds  # (minx, miny, maxx, maxy)
    box_area = max(bounds[2] - bounds[0], 0.0) * max(bounds[3] - bounds[1], 0.0)
    if box_area > 0:
        out.append(
            Interpretation(
                key="bounding_rectangle",
                label="Bounding rectangle",
                means=(
                    "The axis-aligned rectangle the whole arrangement sits in — the "
                    "floor space to allocate or the crate to ship it in."
                ),
                area_units2=box_area,
                area_mm2=_to_mm2(box_area, result),
                basis="axis-aligned bounding box of the counted geometry",
            )
        )

    parts = sorted(polygons, key=lambda p: p.area, reverse=True)
    if len(parts) > 1:
        out.append(
            Interpretation(
                key="largest_body",
                label="Largest single body",
                means=(
                    f"One machine or assembly of the {len(parts)} disconnected bodies "
                    "found, in case the view holds several and only one is the subject."
                ),
                area_units2=parts[0].area,
                area_mm2=_to_mm2(parts[0].area, result),
                basis="largest connected component of the union",
            )
        )

    return out


def body_breakdown(result: AreaResult) -> List[Dict[str, Any]]:
    """Per-body areas, so any subset can be summed by hand.

    Several disconnected silhouettes may be one part or several (§ multi-body);
    the tool lists them and lets the engineer decide rather than guessing.
    """
    rows: List[Dict[str, Any]] = []
    for component in result.components:
        polygon = _polygon(component.outer, component.holes)
        if polygon is None:
            continue
        bounds = polygon.bounds
        rows.append(
            {
                "id": component.id,
                "included": component.included,
                "area_units2": component.area_units2,
                "area_mm2": _to_mm2(component.area_units2, result),
                "hole_count": len(component.holes),
                "width_units": bounds[2] - bounds[0],
                "height_units": bounds[3] - bounds[1],
            }
        )
    return sorted(rows, key=lambda r: r["area_units2"], reverse=True)


def bounding_extent(result: AreaResult) -> Optional[Tuple[float, float]]:
    """``(width, height)`` of the counted geometry in drawing units."""
    polygons = included_polygons(result)
    if not polygons:
        return None
    bounds = unary_union(polygons).bounds
    return (bounds[2] - bounds[0], bounds[3] - bounds[1])
