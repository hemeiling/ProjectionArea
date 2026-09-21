"""Shapely helpers shared by the contour strategies.

CONSTITUTION.md §18: polygon predicates and boolean operations are done with
computational geometry, never inferred.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from backend import diagnostics
from shapely.validation import make_valid

from backend.models import Point, ProfileComponent, Repair


def ensure_valid(geometry: BaseGeometry) -> Tuple[BaseGeometry, bool]:
    """Repair a self-intersecting or otherwise invalid geometry.

    Self-intersection is normal in drawing linework (a bowtie where two paths
    cross). ``make_valid`` resolves it without the silent area changes that
    ``buffer(0)`` can introduce on degenerate input.

    Returns:
        ``(geometry, was_repaired)``.
    """
    if geometry.is_empty or geometry.is_valid:
        return geometry, False
    return make_valid(geometry), True


def polygonal_parts(geometry: BaseGeometry) -> List[Polygon]:
    """Extract only the polygonal parts of a possibly mixed geometry.

    ``make_valid`` can return a GeometryCollection holding stray lines where
    linework touched at a point. Those carry no area and must be discarded.
    """
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    parts: List[Polygon] = []
    for part in getattr(geometry, "geoms", []):
        parts.extend(polygonal_parts(part))
    return parts


def union_polygons(polygons: Sequence[Polygon], min_area: float = 0.0) -> Optional[BaseGeometry]:
    """Union polygons, dropping specks, returning ``None`` when nothing remains.

    The union is what makes overlapping paths count once (§2).
    """
    kept = [p for p in polygons if not p.is_empty and p.area >= min_area]
    if not kept:
        return None
    # A boolean union over every component. On the largest production drawing this
    # is the single most expensive native call in the pipeline.
    with diagnostics.stage("union", parts=len(kept)):
        merged = unary_union(kept)
    merged, _ = ensure_valid(merged)
    return merged if not merged.is_empty else None


def to_components(
    geometry: BaseGeometry,
    min_area: float,
    simplify: float = 0.0,
    min_hole_area: Optional[float] = None,
) -> Tuple[List[ProfileComponent], List[Repair]]:
    """Split a polygonal geometry into reportable silhouette components.

    Each connected polygon becomes one component: its exterior ring is the
    outer silhouette and its interior rings are genuine enclosed voids.

    Args:
        geometry: Polygon or MultiPolygon in PDF units.
        min_area: Components below this area are discarded as specks.
        simplify: Douglas-Peucker tolerance applied to the *rendered* rings
            only. Areas are always taken from the unsimplified polygon so the
            overlay can never disagree with the number by more than a pixel.
        min_hole_area: Interior rings below this area are treated as line-width
            artefacts rather than holes, and are filled back in. Defaults to
            ``min_area``: a void too small to have been kept as a face cannot be
            a real opening either.

    Returns:
        ``(components, repairs)``.
    """
    if min_hole_area is None:
        min_hole_area = min_area

    components: List[ProfileComponent] = []
    repairs: List[Repair] = []
    dropped_specks = 0
    filled_pinholes = 0

    for index, polygon in enumerate(sorted(polygonal_parts(geometry), key=lambda p: -p.area)):
        if polygon.area < min_area:
            dropped_specks += 1
            continue

        holes = [ring for ring in polygon.interiors if Polygon(ring).area >= min_hole_area]
        filled_pinholes += len(polygon.interiors) - len(holes)
        clean = Polygon(polygon.exterior, holes)
        clean, _ = ensure_valid(clean)
        parts = polygonal_parts(clean)
        if not parts:
            continue
        clean = max(parts, key=lambda p: p.area)

        gross = Polygon(clean.exterior).area
        rendered = clean.simplify(simplify, preserve_topology=True) if simplify > 0 else clean
        rendered_parts = polygonal_parts(rendered)
        rendered = max(rendered_parts, key=lambda p: p.area) if rendered_parts else clean

        components.append(
            ProfileComponent(
                id=f"c{index + 1}",
                outer=[(float(x), float(y)) for x, y in rendered.exterior.coords],
                holes=[[(float(x), float(y)) for x, y in ring.coords] for ring in rendered.interiors],
                area_units2=float(clean.area),
                gross_area_units2=float(gross),
            )
        )

    if dropped_specks:
        repairs.append(
            Repair(
                type="drop_speck_polygon",
                count=dropped_specks,
                magnitude_units=min_area,
                detail="discarded faces below the minimum polygon area",
            )
        )
    if filled_pinholes:
        repairs.append(
            Repair(
                type="fill_pinhole",
                count=filled_pinholes,
                magnitude_units=min_hole_area,
                detail="filled interior rings too small to be real openings",
            )
        )
    return components, repairs


def ring_to_polygon(points: Sequence[Point]) -> Optional[Polygon]:
    """Build a valid polygon from a ring, or ``None`` if degenerate."""
    if len(points) < 4:
        if len(points) == 3:
            points = list(points) + [points[0]]
        else:
            return None
    try:
        polygon = Polygon(points)
    except Exception:
        return None
    if polygon.is_empty:
        return None
    polygon, _ = ensure_valid(polygon)
    parts = polygonal_parts(polygon)
    if not parts:
        return None
    return max(parts, key=lambda p: p.area)
