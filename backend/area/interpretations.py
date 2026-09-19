"""Competing definitions of "projected area", each computed, named and drawn.

CONSTITUTION.md §2 (what projected area *means*) and §30 (never hide a choice).

On a single machined part "projected area" has one obvious reading: the union of
the silhouette. On a **manufacturing line layout** it does not. The same sheet
can defensibly yield the material actually occupied by machines, the envelope a
crane has to clear, or the floor space the line has to be given — and those
differ by large factors. Reporting one of them as *the* projected area is exactly
the hidden assumption §30 forbids.

This module produces one :class:`~backend.models.FootprintInterpretation` per
reading, each carrying its own geometry so the UI can draw it and the engineer
can *see* which physical region a number refers to.

**Shape-derived only.** Every reading here follows from geometry alone.
``conveyor_footprint``, ``guarded_area`` and ``line_footprint`` are declared in
:class:`~backend.models.FootprintType` but are never produced here: identifying a
fence or a conveyor is CAD semantics — layer, block name, linetype — not a
property of the shape. :func:`pending_cad_interpretations` reports them as known
but unavailable, so the absence is visible rather than silent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

from backend.models import (
    AreaResult,
    FootprintInterpretation,
    FootprintSemantics,
    FootprintType,
    Point,
)

#: A ring needs at least this many distinct points to bound anything.
_MIN_RING = 3

#: Readings that exist as a concept but need a semantics-aware source, with the
#: CAD metadata each one actually depends on. Reported, never guessed.
_PENDING_CAD: Tuple[Tuple[FootprintType, str, str, str], ...] = (
    (
        FootprintType.CONVEYOR_FOOTPRINT,
        "Conveyor footprint",
        "The floor area occupied by conveying equipment alone, excluding stations and cells.",
        "conveyor layer or block name, plus centreline linetype and declared width",
    ),
    (
        FootprintType.GUARDED_AREA,
        "Safety / guarded footprint",
        "The area enclosed by the safety fence or light-curtain perimeter, which is "
        "what floor-space and access arguments usually turn on.",
        "fence or guarding layer, and a closed perimeter polyline on it",
    ),
    (
        FootprintType.LINE_FOOTPRINT,
        "Total production-line footprint",
        "The whole installation as sited, including aisles and access reserved to it.",
        "cell or line boundary layer, or an XREF/model-space extent for the installation",
    ),
)


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


def _rings(geometry: Any) -> Tuple[List[List[Point]], List[List[Point]]]:
    """Split a Shapely polygon or multipolygon into outer and hole rings."""
    outer: List[List[Point]] = []
    holes: List[List[Point]] = []
    parts = getattr(geometry, "geoms", None) or [geometry]
    for part in parts:
        if not isinstance(part, Polygon) or part.is_empty:
            continue
        outer.append([(float(x), float(y)) for x, y in part.exterior.coords])
        for interior in part.interiors:
            holes.append([(float(x), float(y)) for x, y in interior.coords])
    return outer, holes


def _derived_confidence(result: AreaResult) -> Optional[float]:
    """Confidence that a derived reading *is* the region it names.

    Every reading rests on the same detected geometry and the same scale, and the
    derivations themselves (hull, bounding box) are exact arithmetic that adds no
    uncertainty of its own. So the engine's confidence carries straight through —
    deliberately not inflated because the maths is exact (§10: confidence comes
    from evidence, and the evidence here is the geometry underneath).
    """
    return result.confidence.overall if result.confidence else None


def _encloses(outer: Polygon, others: Sequence[Polygon]) -> List[Polygon]:
    """Which of ``others`` lie inside ``outer``'s exterior ring."""
    shell = Polygon(outer.exterior)
    return [o for o in others if o is not outer and shell.contains(o.representative_point())]


def interpretations(result: AreaResult) -> List[FootprintInterpretation]:
    """Every geometry-derived reading of this result, primary first.

    Returns an empty list when nothing was reconstructed — there is then no
    region to interpret, and §3 forbids presenting zero as a measurement.
    """
    polygons = included_polygons(result)
    if not polygons:
        return []

    confidence = _derived_confidence(result)
    shared_assumptions = list(result.assumptions)
    shared_warnings = list(result.warnings)

    union = unary_union(polygons)
    union_outer, union_holes = _rings(union)

    out: List[FootprintInterpretation] = [
        FootprintInterpretation(
            id="f1",
            type=FootprintType.GEOMETRY_UNION,
            name="Union of counted geometry",
            means=(
                "Every closed profile that was counted, overlaps merged and enclosed "
                "holes removed. A measurement of the linework, making no claim about "
                "what the linework depicts."
            ),
            semantics=FootprintSemantics.GEOMETRIC,
            outer=union_outer,
            holes=union_holes,
            area_units2=result.area_units2,
            area_mm2=result.area_mm2,
            evidence=[
                f"union of {len(polygons)} validated closed profile(s)",
                f"reconstruction method {result.method.value}",
                "the engine's primary result",
            ],
            confidence=confidence,
            assumptions=shared_assumptions,
            warnings=shared_warnings,
        )
    ]

    # ── the enclosing boundary, and what sits inside it ──────────────────────
    #
    # Production evidence (GLTR-101): the largest closed loop on a line layout is
    # the site boundary, not a machine — its bounding rectangle matched the
    # sheet's stated 13 200 x 75 000 mm exactly. Labelling that "equipment" was
    # arithmetically right and semantically wrong, so both the boundary and its
    # interior are offered as *candidates* with their possible meanings listed.
    by_area = sorted(polygons, key=lambda p: p.area, reverse=True)
    boundary = None
    contained: List[Polygon] = []
    for candidate in by_area:
        inside = _encloses(candidate, polygons)
        if inside:
            boundary, contained = candidate, inside
            break

    if boundary is not None:
        b_outer, b_holes = _rings(boundary)
        out.append(
            FootprintInterpretation(
                id="f2",
                type=FootprintType.ENCLOSING_BOUNDARY,
                name="Enclosing boundary (semantics unconfirmed)",
                means=(
                    f"The largest closed loop, which encloses {len(contained)} other "
                    "bodies. On a line layout this is usually the site or cell "
                    "boundary rather than equipment — but that cannot be told from "
                    "shape, so it is offered as a candidate, not a fact."
                ),
                semantics=FootprintSemantics.PROVISIONAL,
                candidate_meanings=[
                    "site allocation",
                    "cell envelope",
                    "floor boundary",
                    "line boundary",
                    "a single large machine",
                ],
                outer=b_outer,
                holes=b_holes,
                area_units2=boundary.area,
                area_mm2=_to_mm2(boundary.area, result),
                evidence=[
                    f"largest closed loop; strictly encloses {len(contained)} other bodies",
                    f"its bounding rectangle is "
                    f"{boundary.bounds[2] - boundary.bounds[0]:.2f} x "
                    f"{boundary.bounds[3] - boundary.bounds[1]:.2f} drawing units",
                ],
                confidence=confidence,
                assumptions=shared_assumptions,
                warnings=shared_warnings
                + [
                    "What this boundary represents is unconfirmed. Confirm it against "
                    "CAD layer or block metadata, or by selecting it by hand, before "
                    "using this number for anything."
                ],
            )
        )

        internal = unary_union(contained)
        i_outer, i_holes = _rings(internal)
        share = internal.area / boundary.area if boundary.area > 0 else 0.0
        internal_warnings = list(shared_warnings)
        if share < 0.05:
            internal_warnings.append(
                f"The geometry inside the boundary totals only {share * 100:.1f} % of it. "
                "On a drawing whose equipment outlines touch or merge into the boundary, "
                "they are absorbed into that face and cannot be separated here — this "
                "figure is then a floor, not the equipment area."
            )
        out.append(
            FootprintInterpretation(
                id="f3",
                type=FootprintType.INTERNAL_UNION,
                name="Union of geometry inside that boundary",
                means=(
                    f"The {len(contained)} bodies enclosed by the boundary above, merged. "
                    "Often the equipment, but only CAD metadata can confirm that."
                ),
                semantics=FootprintSemantics.PROVISIONAL,
                candidate_meanings=[
                    "equipment and machinery",
                    "internal fixtures",
                    "annotation drawn inside the boundary",
                ],
                outer=i_outer,
                holes=i_holes,
                area_units2=internal.area,
                area_mm2=_to_mm2(internal.area, result),
                evidence=[
                    f"union of the {len(contained)} bodies strictly inside the enclosing boundary",
                    f"{share * 100:.1f} % of the boundary's own area",
                ],
                confidence=confidence,
                assumptions=shared_assumptions,
                warnings=internal_warnings,
            )
        )

    # ── purely geometric envelopes ───────────────────────────────────────────
    points: List[Point] = []
    for component in result.components:
        if component.included:
            points.extend(component.outer)

    hull = MultiPoint(points).convex_hull if len(points) >= _MIN_RING else None
    if isinstance(hull, Polygon) and hull.area > 0:
        hull_outer, _ = _rings(hull)
        out.append(
            FootprintInterpretation(
                id="f4",
                type=FootprintType.CONVEX_ENVELOPE,
                name="Convex envelope",
                means=(
                    "The smallest convex region containing every counted body — what "
                    "a gantry, crane path or guard enclosure has to clear."
                ),
                semantics=FootprintSemantics.GEOMETRIC,
                outer=hull_outer,
                area_units2=hull.area,
                area_mm2=_to_mm2(hull.area, result),
                evidence=["convex hull of all included outer rings"],
                confidence=confidence,
                assumptions=shared_assumptions
                + ["Voids between bodies are treated as part of the envelope."],
                warnings=shared_warnings,
            )
        )

    minx, miny, maxx, maxy = union.bounds
    box_area = max(maxx - minx, 0.0) * max(maxy - miny, 0.0)
    if box_area > 0:
        out.append(
            FootprintInterpretation(
                id="f5",
                type=FootprintType.BOUNDING_RECTANGLE,
                name="Bounding rectangle",
                means=(
                    "The axis-aligned rectangle the whole arrangement sits in — the "
                    "floor space to allocate or the crate to ship it in."
                ),
                semantics=FootprintSemantics.GEOMETRIC,
                outer=[[(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]],
                area_units2=box_area,
                area_mm2=_to_mm2(box_area, result),
                evidence=[
                    "axis-aligned bounding box of the counted geometry",
                    f"extent {maxx - minx:.2f} x {maxy - miny:.2f} drawing units",
                ],
                confidence=confidence,
                assumptions=shared_assumptions
                + [
                    "Axis-aligned to the sheet; a line drawn at an angle will report a "
                    "larger rectangle than its true minimum.",
                    "Bounds everything counted, including any annotation that was not "
                    "demoted — on a sheet whose text is stroked into geometry this "
                    "inflates the rectangle.",
                ],
                warnings=shared_warnings,
            )
        )

    return out


def pending_cad_interpretations() -> List[Dict[str, Any]]:
    """Readings the product intends to support but geometry cannot supply.

    Returned so the UI and the report can show them as *known and unavailable*
    rather than omitting them. Each names the CAD metadata it needs, which is
    also the specification for the DXF adapter when real files arrive.
    """
    return [
        {
            "type": footprint_type.value,
            "name": name,
            "means": means,
            "available": False,
            "requires": requires,
            "reason": (
                "Needs CAD semantics to identify the linework; it cannot be inferred "
                "from shape alone and will not be guessed."
            ),
        }
        for footprint_type, name, means, requires in _PENDING_CAD
    ]


def body_breakdown(result: AreaResult) -> List[Dict[str, Any]]:
    """Per-body areas, so any subset can be summed by hand.

    Several disconnected silhouettes may be one installation or several; the tool
    lists them and lets the engineer decide rather than guessing.
    """
    rows: List[Dict[str, Any]] = []
    for component in result.components:
        polygon = _polygon(component.outer, component.holes)
        if polygon is None:
            continue
        minx, miny, maxx, maxy = polygon.bounds
        rows.append(
            {
                "id": component.id,
                "included": component.included,
                "area_units2": component.area_units2,
                "area_mm2": _to_mm2(component.area_units2, result),
                "hole_count": len(component.holes),
                "width_units": maxx - minx,
                "height_units": maxy - miny,
            }
        )
    return sorted(rows, key=lambda r: r["area_units2"], reverse=True)


def bounding_extent(result: AreaResult) -> Optional[Tuple[float, float]]:
    """``(width, height)`` of the counted geometry in drawing units."""
    polygons = included_polygons(result)
    if not polygons:
        return None
    minx, miny, maxx, maxy = unary_union(polygons).bounds
    return (maxx - minx, maxy - miny)
