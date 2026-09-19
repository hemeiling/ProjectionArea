"""Normalise primitives into a clean, noded segment network.

CONSTITUTION.md §14 (robust operations against imperfect CAD output) and §16
(every automatic repair recorded).

Pipeline stage boundary: in come classified primitives, out go de-duplicated,
endpoint-snapped, optionally gap-bridged segments plus a repair log. Nothing
here decides what an area *is* — that is :mod:`backend.geometry.contours`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from backend.models import GeometryRole, Point, Primitive, Repair

Segment = Tuple[Point, Point]


@dataclass
class SegmentNetwork:
    """A cleaned segment network with its provenance and repair log."""

    segments: List[Segment] = field(default_factory=list)
    #: Index of the source primitive for each segment, same order.
    sources: List[int] = field(default_factory=list)
    #: Closed filled primitives kept whole — direct evidence of solid regions.
    filled_rings: List[List[Point]] = field(default_factory=list)
    repairs: List[Repair] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.segments)


def primitives_to_segments(
    primitives: Iterable[Primitive],
    roles: Optional[Set[GeometryRole]] = None,
    min_segment: float = 0.05,
    role_of: Optional[Callable[[Primitive], GeometryRole]] = None,
) -> Tuple[List[Segment], List[int], List[List[Point]], int]:
    """Explode primitives into individual segments.

    Args:
        primitives: Primitives to convert.
        roles: Roles to accept. ``None`` accepts every role.
        min_segment: Segments shorter than this are dropped as zero-length.
        role_of: Effective-role lookup, so a caller can apply user overrides
            without mutating the shared page cache. Defaults to the primitive's
            own role.

    Returns:
        ``(segments, source_indices, filled_rings, dropped_count)``. Filled
        closed primitives are additionally returned whole, because a solid fill
        is direct evidence of an occupied region and should not have to be
        rediscovered by polygonization.
    """
    segments: List[Segment] = []
    sources: List[int] = []
    filled_rings: List[List[Point]] = []
    dropped = 0

    for prim in primitives:
        if roles is not None and (role_of(prim) if role_of else prim.role) not in roles:
            continue
        points = prim.points
        if prim.filled and prim.closed and len(points) >= 4:
            filled_rings.append(list(points))
        pairs = list(zip(points, points[1:]))
        if prim.closed and len(points) > 2 and points[0] != points[-1]:
            pairs.append((points[-1], points[0]))
        for a, b in pairs:
            if math.hypot(b[0] - a[0], b[1] - a[1]) < min_segment:
                dropped += 1
                continue
            segments.append((a, b))
            sources.append(prim.index)

    return segments, sources, filled_rings, dropped


class _PointSnapper:
    """Greedy spatial-hash clustering of nearby points onto representatives."""

    def __init__(self, tolerance: float) -> None:
        self.tolerance = max(tolerance, 1e-9)
        self._cells: Dict[Tuple[int, int], List[Point]] = defaultdict(list)
        self.moved = 0
        self.max_move = 0.0

    def _cell(self, p: Point) -> Tuple[int, int]:
        return (int(math.floor(p[0] / self.tolerance)), int(math.floor(p[1] / self.tolerance)))

    def snap(self, p: Point) -> Point:
        cx, cy = self._cell(p)
        best: Optional[Point] = None
        best_distance = self.tolerance
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for candidate in self._cells.get((cx + dx, cy + dy), ()):
                    distance = math.hypot(candidate[0] - p[0], candidate[1] - p[1])
                    if distance <= best_distance:
                        best, best_distance = candidate, distance
        if best is not None:
            if best_distance > 1e-12:
                self.moved += 1
                self.max_move = max(self.max_move, best_distance)
            return best
        self._cells[(cx, cy)].append(p)
        return p


def snap_segments(
    segments: Sequence[Segment], tolerance: float
) -> Tuple[List[Segment], Repair]:
    """Snap coincident-ish endpoints onto shared vertices.

    CAD exporters routinely emit ``(100.0, 50.0)`` and ``(99.9999, 50.0001)``
    for what the model says is one corner. Without snapping, Shapely's noder
    leaves a hairline gap and the contour never closes.

    Returns:
        The snapped segments and a repair record (count may be zero).
    """
    snapper = _PointSnapper(tolerance)
    snapped: List[Segment] = []
    for a, b in segments:
        sa, sb = snapper.snap(a), snapper.snap(b)
        if sa == sb:
            continue
        snapped.append((sa, sb))
    repair = Repair(
        type="snap_endpoints",
        count=snapper.moved,
        magnitude_units=round(snapper.max_move, 4) if snapper.moved else None,
        detail=f"snapped endpoints within {tolerance:.3f} PDF units onto shared vertices",
    )
    return snapped, repair


def dedupe_segments(
    segments: Sequence[Segment], tolerance: float
) -> Tuple[List[Segment], List[int], Repair]:
    """Remove duplicate and reversed-duplicate segments.

    Drawings frequently carry the same edge twice — once from the outline and
    once from a hatch boundary or a view border. Left in place they do not
    change a Shapely union, but they inflate segment counts, slow noding, and
    make repair statistics meaningless.

    Returns:
        ``(unique_segments, kept_indices, repair)``.
    """
    quantum = max(tolerance, 1e-9)
    seen: Set[Tuple[int, int, int, int]] = set()
    unique: List[Segment] = []
    kept: List[int] = []
    duplicates = 0

    for index, (a, b) in enumerate(segments):
        ka = (int(round(a[0] / quantum)), int(round(a[1] / quantum)))
        kb = (int(round(b[0] / quantum)), int(round(b[1] / quantum)))
        key = ka + kb if ka <= kb else kb + ka
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append((a, b))
        kept.append(index)

    repair = Repair(
        type="remove_duplicate_segments",
        count=duplicates,
        magnitude_units=round(quantum, 4) if duplicates else None,
        detail=f"dropped segments coincident within {quantum:.3f} PDF units",
    )
    return unique, kept, repair


def bridge_gaps(
    segments: Sequence[Segment], closure_tolerance: float, max_bridges: int = 400
) -> Tuple[List[Segment], Repair]:
    """Close small contour gaps by joining dangling endpoints.

    Only vertices of degree 1 are eligible — a true contour break leaves two
    loose ends facing each other. Each end may be used once, and the shortest
    candidate bridges are taken first, so a break is closed the way a
    draughtsman would close it rather than by fanning out to distant linework.

    Bridging is the single most dangerous automatic repair in the system, which
    is why it is capped, measured, and reported (§16). ``max_bridges`` prevents
    a shattered raster trace from being "repaired" into fiction.

    Args:
        segments: Snapped, de-duplicated segments.
        closure_tolerance: Largest gap that may be bridged, PDF units.
        max_bridges: Hard cap on the number of bridges added.

    Returns:
        ``(segments_plus_bridges, repair)``.
    """
    if closure_tolerance <= 0 or not segments:
        return list(segments), Repair(type="close_gap", count=0, detail="disabled")

    degree: Dict[Point, int] = defaultdict(int)
    for a, b in segments:
        degree[a] += 1
        degree[b] += 1
    loose = [p for p, count in degree.items() if count == 1]

    if len(loose) < 2:
        return list(segments), Repair(type="close_gap", count=0, detail="no loose ends")

    cell_size = max(closure_tolerance, 1e-9)
    grid: Dict[Tuple[int, int], List[Point]] = defaultdict(list)
    for p in loose:
        grid[(int(math.floor(p[0] / cell_size)), int(math.floor(p[1] / cell_size)))].append(p)

    candidates: List[Tuple[float, Point, Point]] = []
    for p in loose:
        cx, cy = int(math.floor(p[0] / cell_size)), int(math.floor(p[1] / cell_size))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in grid.get((cx + dx, cy + dy), ()):
                    if q <= p:
                        continue
                    distance = math.hypot(q[0] - p[0], q[1] - p[1])
                    if 0 < distance <= closure_tolerance:
                        candidates.append((distance, p, q))

    candidates.sort(key=lambda item: item[0])
    used: Set[Point] = set()
    bridges: List[Segment] = []
    largest = 0.0
    for distance, p, q in candidates:
        if len(bridges) >= max_bridges:
            break
        if p in used or q in used:
            continue
        used.add(p)
        used.add(q)
        bridges.append((p, q))
        largest = max(largest, distance)

    repair = Repair(
        type="close_gap",
        count=len(bridges),
        magnitude_units=round(largest, 4) if bridges else None,
        detail=(
            f"bridged {len(bridges)} contour gap(s) up to {largest:.3f} PDF units"
            if bridges
            else f"no gaps within {closure_tolerance:.3f} PDF units"
        ),
    )
    return list(segments) + bridges, repair


def build_network(
    primitives: Sequence[Primitive],
    tolerances,
    roles: Optional[Set[GeometryRole]] = None,
    close_gaps: bool = True,
    role_of: Optional[Callable[[Primitive], GeometryRole]] = None,
) -> SegmentNetwork:
    """Run the full normalise → snap → dedupe → bridge pipeline.

    Args:
        primitives: Classified primitives, already restricted to a region.
        tolerances: A :class:`backend.config.Tolerances`.
        roles: Roles allowed to contribute. Defaults to profile + uncertain.
        close_gaps: Whether to attempt gap bridging.
        role_of: Effective-role lookup; see :func:`primitives_to_segments`.

    Returns:
        A :class:`SegmentNetwork` with the repair log populated. Repairs with a
        zero count are kept out of the log so the audit trail stays readable.
    """
    if roles is None:
        roles = {GeometryRole.PROFILE, GeometryRole.UNCERTAIN}

    segments, _sources, filled_rings, dropped = primitives_to_segments(
        primitives, roles=roles, min_segment=tolerances.min_segment, role_of=role_of
    )

    repairs: List[Repair] = []
    if dropped:
        repairs.append(
            Repair(
                type="remove_zero_length",
                count=dropped,
                magnitude_units=tolerances.min_segment,
                detail="dropped segments shorter than the minimum segment tolerance",
            )
        )

    segments, snap_repair = snap_segments(segments, tolerances.snap)
    if snap_repair.count:
        repairs.append(snap_repair)

    segments, _kept, dup_repair = dedupe_segments(segments, tolerances.duplicate)
    if dup_repair.count:
        repairs.append(dup_repair)

    if close_gaps:
        segments, bridge_repair = bridge_gaps(segments, tolerances.closure)
        if bridge_repair.count:
            repairs.append(bridge_repair)

    return SegmentNetwork(
        segments=segments,
        sources=[],
        filled_rings=filled_rings,
        repairs=repairs,
    )
