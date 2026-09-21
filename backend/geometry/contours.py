"""Strategy V1 — exact silhouette by planar polygonization.

CONSTITUTION.md §2 and §5. The preferred path: it works directly on native
vector linework, with no rasterization anywhere.

The method
----------
1. **Node** the segment network. ``unary_union`` over the lines splits every
   crossing into shared vertices, turning loose linework into a planar graph.
2. **Polygonize** that graph. The result is the set of atomic *faces* of the
   arrangement. Dangling linework — extension lines, leaders, stray strokes —
   bounds no face and simply disappears, which is exactly right.
3. **Decide solid vs void** per face by containment depth (below).
4. **Union** the solid faces. Overlapping outlines therefore count once (§2),
   and the voids left behind become interior rings — the holes.

Solid or void: the containment-depth rule
-----------------------------------------
A drawing reads by nesting. The outer profile is solid; a loop drawn inside it
is a hole; a loop inside that hole is an island, and so on. So for each face we
count how many *other* faces' outer rings enclose it:

    depth(f) = #{ g != f : outer_ring(g) strictly contains a point of f }

and call the face solid when ``depth`` is even.

This is deliberately **not** the even-odd rule applied to raw edge crossings.
Edge parity would XOR two partially overlapping outlines and punch a false hole
through their intersection. Counting enclosing *rings* instead treats nested
loops as alternating solid/void while treating merely overlapping loops as both
solid — which is what §2 requires.

Worked example, a 100x50 plate with a Ø20 hole. Polygonize yields two faces: the
annulus and the disc. The annulus is enclosed by no other face's outer ring
(depth 0, solid); the disc sits inside the annulus's outer ring (depth 1, void).
The union is the annulus: one exterior ring and one interior ring, 4685.84 units²
rather than the 5000 a plain union would have reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from shapely.geometry import MultiLineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from backend import diagnostics

from backend.geometry.polygons import ensure_valid, ring_to_polygon, union_polygons
from backend.geometry.segments import SegmentNetwork
from backend.models import Repair


@dataclass
class ContourResult:
    """Outcome of one silhouette reconstruction attempt."""

    geometry: Optional[BaseGeometry]
    face_count: int
    solid_face_count: int = 0
    void_face_count: int = 0
    repairs: List[Repair] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.geometry is not None and not self.geometry.is_empty


def solid_faces_by_depth(faces: Sequence[Polygon]) -> Tuple[List[Polygon], List[Polygon]]:
    """Split polygonized faces into solids and voids by containment depth.

    See the module docstring for why depth is counted over enclosing outer
    rings rather than over edge crossings.

    Args:
        faces: Atomic faces from :func:`shapely.ops.polygonize`.

    Returns:
        ``(solid_faces, void_faces)``.
    """
    if not faces:
        return [], []

    outer_rings = [Polygon(face.exterior) for face in faces]
    points = [face.representative_point() for face in faces]
    diagnostics.note("strtree.build", rings=len(outer_rings))
    tree = STRtree(outer_rings)

    solids: List[Polygon] = []
    voids: List[Polygon] = []
    for index, (face, point) in enumerate(zip(faces, points)):
        depth = 0
        for candidate in tree.query(point):
            other = int(candidate)
            if other == index:
                continue
            if outer_rings[other].contains(point):
                depth += 1
        (solids if depth % 2 == 0 else voids).append(face)
    return solids, voids


def polygonize_network(network: SegmentNetwork, min_polygon_area: float) -> ContourResult:
    """Reconstruct the silhouette from a cleaned segment network.

    Args:
        network: Snapped, de-duplicated, optionally gap-bridged segments, plus
            any filled rings lifted straight from the PDF.
        min_polygon_area: Faces below this area (PDF units²) are discarded as
            line-crossing specks before the depth analysis runs.

    Returns:
        A :class:`ContourResult`. ``geometry`` is ``None`` when the linework
        bounds no face at all — the honest answer for an unclosed drawing, and
        the trigger for the gap-closed fallback.
    """
    repairs: List[Repair] = []
    notes: List[str] = []

    faces: List[Polygon] = []
    if network.segments:
        # The two calls below are where a production drawing spends its time and
        # its memory: noding 1.7 million segments and polygonizing the result are
        # single native operations that hold the GIL, so nothing else in the
        # process runs while they do — including the health check a platform uses
        # to decide the instance is alive. Marked so a log shows which one a
        # restart interrupted (§32).
        with diagnostics.stage("noding", segments=len(network.segments)) as facts:
            lines = MultiLineString([list(segment) for segment in network.segments])
            noded = unary_union(lines)
            facts["noded_parts"] = getattr(noded, "geom_type", "?")
        with diagnostics.stage("polygonize") as facts:
            faces = [face for face in polygonize(noded) if face.area >= min_polygon_area]
            facts["faces"] = len(faces)

    if not faces and not network.filled_rings:
        return ContourResult(
            geometry=None,
            face_count=0,
            repairs=repairs,
            notes=notes + ["linework bounds no closed face"],
        )

    solids, voids = solid_faces_by_depth(faces)

    # A solid fill in the PDF is direct evidence of an occupied region and needs
    # no parity reasoning; filled arrowheads were classified away earlier.
    for ring in network.filled_rings:
        polygon = ring_to_polygon(ring)
        if polygon is not None and polygon.area >= min_polygon_area:
            solids.append(polygon)
    if network.filled_rings:
        notes.append(f"{len(network.filled_rings)} filled path(s) contributed directly")

    merged = union_polygons(solids)
    if merged is None:
        return ContourResult(
            geometry=None,
            face_count=len(faces),
            solid_face_count=0,
            void_face_count=len(voids),
            repairs=repairs,
            notes=notes + ["every face resolved to void; no solid region remains"],
        )

    repaired, was_repaired = ensure_valid(merged)
    if was_repaired:
        repairs.append(
            Repair(
                type="repair_self_intersection",
                count=1,
                detail="unioned faces were self-intersecting and were rebuilt",
            )
        )
        merged = repaired

    if voids:
        notes.append(f"{len(voids)} face(s) resolved to enclosed voids (holes)")

    return ContourResult(
        geometry=merged,
        face_count=len(faces),
        solid_face_count=len(solids),
        void_face_count=len(voids),
        repairs=repairs,
        notes=notes,
    )


def coverage_ratio(geometry: Optional[BaseGeometry], reference_bbox) -> float:
    """How much of the linework's bounding box the silhouette fills.

    A healthy closed profile fills a substantial share of its own bounding box.
    A value near zero means polygonization found only slivers — a strong signal
    that the outline never closed and the fallback should run.
    """
    if geometry is None or geometry.is_empty or reference_bbox is None:
        return 0.0
    reference_area = reference_bbox.area
    if reference_area <= 0:
        return 0.0
    return float(geometry.area) / reference_area
