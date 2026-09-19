"""Interpretable confidence.

CONSTITUTION.md §10: no arbitrary percentages. Every component below is a
number the pipeline actually measured, and every component carries a note
explaining what drove it. The UI shows the components, not just the headline.

The overall figure is a **weighted geometric mean**, not an average. A weak
link should drag the result down rather than be masked by strong neighbours:
perfect vector geometry measured against an unverified scale is not an 80%
result, and the geometric mean says so.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from backend.models import (
    ConfidenceBreakdown,
    DrawingType,
    GeometryRole,
    Method,
    Primitive,
    Repair,
    Scale,
    ViewSource,
)

#: How much each component matters. Scale carries the most weight because it is
#: the only component that can make the number wrong by a factor rather than a
#: percent.
_WEIGHTS: Dict[str, float] = {
    "source": 0.15,
    "geometry": 0.28,
    "scale": 0.35,
    "view": 0.10,
    "repair": 0.12,
}

_SOURCE_SCORES = {
    DrawingType.VECTOR: 0.97,
    DrawingType.MIXED: 0.86,
    DrawingType.RASTER: 0.62,
    DrawingType.UNKNOWN: 0.40,
}

_METHOD_SCORES = {
    Method.VECTOR_EXACT: 0.96,
    Method.USER_POLYGON: 0.90,
    Method.VECTOR_GAP_CLOSED: 0.78,
    Method.RASTER_TRACE: 0.58,
}

_VIEW_SCORES = {
    ViewSource.USER_SELECTED: 1.00,
    ViewSource.AUTO_DETECTED: 0.85,
    ViewSource.WHOLE_PAGE: 0.55,
}


def geometry_confidence(
    method: Method,
    primitives: Sequence[Primitive],
    component_count: int,
    notes: List[str],
) -> float:
    """Score the reconstruction itself.

    Penalises two measurable things: linework the classifier could not place
    with confidence, and a silhouette that came back as many disconnected
    components when a single part was expected.
    """
    score = _METHOD_SCORES.get(method, 0.6)
    notes.append(f"reconstruction method: {method.value.replace('_', ' ')}")

    considered = [p for p in primitives if p.role in (GeometryRole.PROFILE, GeometryRole.UNCERTAIN)]
    if considered:
        uncertain = sum(1 for p in considered if p.role is GeometryRole.UNCERTAIN)
        fraction = uncertain / len(considered)
        if fraction > 0:
            score *= 1.0 - min(0.35, fraction)
            notes.append(f"{uncertain} of {len(considered)} profile primitives were ambiguous")

    if component_count > 1:
        # Several disconnected silhouettes may be legitimate (a multi-part view)
        # but more often means the profile fragmented. Penalise gently and say so.
        penalty = min(0.25, 0.05 * (component_count - 1))
        score *= 1.0 - penalty
        notes.append(f"{component_count} disconnected silhouette components")

    return max(0.0, min(1.0, score))


def repair_confidence(repairs: Sequence[Repair], notes: List[str]) -> float:
    """Score how much the geometry had to be altered to make it usable.

    Cosmetic repairs — dropping zero-length segments, removing duplicates — cost
    nothing. Bridging gaps and rebuilding self-intersections mean the drawing
    did not close on its own, and that is a real reason to trust the number
    less.
    """
    if not repairs:
        return 1.0

    cost = 0.0
    for repair in repairs:
        if repair.count <= 0:
            continue
        if repair.type in ("remove_zero_length", "remove_duplicate_segments", "drop_speck_polygon"):
            continue  # tidy-up, not reconstruction
        if repair.type == "close_gap":
            cost += min(0.22, 0.02 + 0.012 * repair.count)
            notes.append(f"{repair.count} contour gap(s) were bridged automatically")
        elif repair.type == "morphological_gap_close":
            cost += 0.14
            notes.append("the profile was closed morphologically rather than exactly")
        elif repair.type == "repair_self_intersection":
            cost += 0.05
            notes.append("self-intersecting linework was rebuilt")
        elif repair.type == "fill_pinhole":
            cost += min(0.06, 0.01 * repair.count)
            notes.append(f"{repair.count} sub-tolerance interior ring(s) were filled")
        else:
            cost += 0.03

    return max(0.25, 1.0 - cost)


def evaluate(
    drawing_type: DrawingType,
    method: Method,
    scale: Scale,
    view_source: ViewSource,
    primitives: Sequence[Primitive],
    component_count: int,
    repairs: Sequence[Repair],
    extra_notes: Optional[Sequence[str]] = None,
    user_verified: bool = False,
) -> ConfidenceBreakdown:
    """Combine the components into an overall confidence.

    Args:
        drawing_type: Page classification.
        method: How the silhouette was reconstructed.
        scale: The scale used, carrying its own confidence.
        view_source: Whether the region was chosen by the user or guessed.
        primitives: Primitives that fed the reconstruction.
        component_count: Number of disconnected silhouette components.
        repairs: Every repair applied along the way.
        extra_notes: Additional notes to surface verbatim.
        user_verified: True when an engineer corrected the geometry by hand —
            overriding a role or drawing a boundary. §10 counts human
            verification as evidence, so it lifts the geometry score modestly.
            It is deliberately a small bonus: a hand-drawn boundary is checked,
            not necessarily precise.

    Returns:
        A :class:`ConfidenceBreakdown`. When the scale is unverified the overall
        figure is forced to zero: there is no physical number to be confident
        about (§3).
    """
    notes: List[str] = []

    source = _SOURCE_SCORES.get(drawing_type, 0.4)
    notes.append(f"source: {drawing_type.value} PDF page")

    geometry = geometry_confidence(method, primitives, component_count, notes)
    if user_verified:
        geometry = min(1.0, geometry * 1.06)
        notes.append("geometry was reviewed and corrected by the user")
    repair = repair_confidence(repairs, notes)
    view = _VIEW_SCORES.get(view_source, 0.55)
    notes.append(f"view selection: {view_source.value.replace('_', ' ')}")

    scale_score = scale.confidence if scale.verified else 0.0
    notes.append(
        f"scale: {scale.source.value.replace('_', ' ')}"
        + ("" if scale.verified else " — not verified, no physical area reported")
    )

    if extra_notes:
        notes.extend(extra_notes)

    if component_count <= 0:
        notes.append("no closed profile was reconstructed, so there is nothing to score")
        return ConfidenceBreakdown(
            overall=0.0, source=source, geometry=0.0, scale=scale_score, view=view,
            repair=repair, notes=notes,
        )

    if not scale.verified:
        return ConfidenceBreakdown(
            overall=0.0, source=source, geometry=geometry, scale=0.0, view=view,
            repair=repair, notes=notes,
        )

    components = {"source": source, "geometry": geometry, "scale": scale_score, "view": view, "repair": repair}
    log_sum = sum(_WEIGHTS[key] * math.log(max(value, 1e-6)) for key, value in components.items())
    overall = math.exp(log_sum / sum(_WEIGHTS.values()))

    return ConfidenceBreakdown(
        overall=max(0.0, min(1.0, overall)),
        source=source,
        geometry=geometry,
        scale=scale_score,
        view=view,
        repair=repair,
        notes=notes,
    )
