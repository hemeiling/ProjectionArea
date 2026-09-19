"""Projected-area calculation — the stage that turns geometry into a number.

CONSTITUTION.md §2 (what projected area *means*), §11 (detection separate from
calculation), §30 (never hide a fallback), §41 (result structure).

Projected area is the area of the 2D silhouette of the selected view: the union
of the closed profile, so overlapping linework counts once, with genuine
enclosed voids subtracted when the caller asks for it. Both figures are always
returned — ``net`` with holes removed and ``gross`` with them filled — because
which one a manufacturing process cares about depends on the process, not on
the drawing.
"""

from __future__ import annotations

import datetime as _datetime
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Set

from shapely.geometry.base import BaseGeometry

from backend.config import ENGINE_VERSION, SILHOUETTE, Tolerances
from backend.confidence.model import evaluate as evaluate_confidence
from backend.geometry.contours import polygonize_network
from backend.geometry.polygons import ensure_valid, ring_to_polygon, to_components, union_polygons
from backend.geometry.regions import region_primitives
from backend.geometry.segments import build_network
from backend.geometry.silhouette import gap_closed_silhouette
from backend.models import (
    AreaResult,
    BBox,
    DrawingType,
    GeometryReport,
    GeometryRole,
    Method,
    Primitive,
    Point,
    ProfileComponent,
    Repair,
    Scale,
    ViewSource,
)
from backend.raster.trace import trace_page

#: Roles that may contribute to a silhouette unless the caller overrides it.
DEFAULT_INCLUDED_ROLES: Set[GeometryRole] = {GeometryRole.PROFILE, GeometryRole.UNCERTAIN}

#: A reconstructed profile must span at least this share of the linework's
#: bounding box. Area coverage is the wrong test — a thin annulus legitimately
#: fills very little of its box — but *extent* is reliable: a profile that
#: failed to close collapses onto whatever small loop did close, and its
#: bounding box shrinks with it.
_MIN_BBOX_FILL = 0.30

#: Closure tolerance multipliers tried in order when the profile will not close.
#: Escalating is what an engineer does by hand; doing it automatically is fine
#: only because every step is recorded and warned about (§16, §30).
_CLOSURE_ESCALATION = (1.0, 2.5, 6.0)


def _role_counts(primitives: Sequence[Primitive]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for prim in primitives:
        counts[prim.role.value] = counts.get(prim.role.value, 0) + 1
    return counts


def compute_projected_area(
    analysis,
    fitz_page,
    document_id: str,
    file_name: str,
    scale: Scale,
    region_bbox: Optional[BBox] = None,
    view_source: ViewSource = ViewSource.WHOLE_PAGE,
    view_label: str = "Whole page",
    subtract_holes: bool = True,
    include_roles: Optional[Set[GeometryRole]] = None,
    excluded_component_ids: Optional[Set[str]] = None,
    component_selection: str = "auto",
    role_overrides: Optional[Dict[int, GeometryRole]] = None,
    manual_add: Optional[Sequence[Sequence[Point]]] = None,
    manual_subtract: Optional[Sequence[Sequence[Point]]] = None,
    close_gaps: bool = True,
    force_raster: bool = False,
    extra_warnings: Optional[Sequence[str]] = None,
) -> AreaResult:
    """Run the full geometry -> silhouette -> area pipeline for one selection.

    Args:
        analysis: A prepared :class:`backend.pdf.document.PageAnalysis` whose
            primitives have already been role-classified.
        fitz_page: The underlying ``fitz.Page``, needed only by the raster path.
        document_id: Identifier of the stored upload, for traceability.
        file_name: Original file name, for the result header.
        scale: The scale to apply. May be unverified, in which case no physical
            area is reported (§3).
        region_bbox: The selected view rectangle in PDF units. ``None`` uses the
            whole page and lowers view confidence accordingly.
        view_source: Whether the region came from the user or from detection.
        view_label: Human-readable name for the region.
        subtract_holes: Subtract enclosed voids from the reported net area.
        include_roles: Override which primitive roles may contribute.
        excluded_component_ids: Silhouette components the user has switched off.
            When given, it overrides ``component_selection`` entirely.
        component_selection: ``auto`` keeps every component on the vector paths
            and only the dominant one on a raster trace; ``all`` and
            ``largest`` force the choice.
        role_overrides: Primitive index to role, applied last so the engineer
            can promote linework the classifier set aside, or demote linework it
            wrongly kept. Applied through a lookup rather than by mutation, so
            the shared page cache stays pristine and an override can always be
            taken back.
        manual_add: Rings, in PDF units, unioned into the silhouette — the
            "draw boundary" tool.
        manual_subtract: Rings removed from the silhouette — the "erase
            boundary" tool.
        close_gaps: Allow endpoint gap bridging during normalisation.
        force_raster: Skip the vector path entirely (used for scanned pages and
            for explicit user override).

    Returns:
        A fully populated :class:`AreaResult`, including the geometry report,
        repair log, confidence breakdown and warnings.
    """
    tolerances: Tolerances = analysis.tolerances
    warnings: List[str] = list(extra_warnings or [])
    assumptions: List[str] = []
    repairs: List[Repair] = []
    notes: List[str] = []

    roles = include_roles or DEFAULT_INCLUDED_ROLES

    selected = (
        region_primitives(analysis.primitives, region_bbox) if region_bbox else list(analysis.primitives)
    )
    if region_bbox and not selected and analysis.primitives:
        warnings.append(
            "The selected region contains no complete primitive. Primitives are "
            "included only when they lie entirely inside the selection — widen it."
        )

    overrides = dict(role_overrides or {})

    def effective_role(primitive: Primitive) -> GeometryRole:
        return overrides.get(primitive.index, primitive.role)

    if overrides:
        notes.append(f"{len(overrides)} primitive role(s) overridden by the user")

    contributing = [p for p in selected if effective_role(p) in roles]
    ignored = [p for p in selected if effective_role(p) not in roles]

    geometry: Optional[BaseGeometry] = None
    method = Method.VECTOR_EXACT
    face_count = 0
    segment_count = 0

    use_vector = not force_raster and analysis.drawing_type in (DrawingType.VECTOR, DrawingType.MIXED)

    if use_vector and contributing:
        ink_bbox = BBox.union_of([p.bbox for p in contributing]) or analysis.page_bbox
        geometry, method, face_count, segment_count, vector_repairs, vector_notes, vector_warnings = (
            _reconstruct_vector(contributing, tolerances, roles, close_gaps, ink_bbox, effective_role)
        )
        repairs.extend(vector_repairs)
        notes.extend(vector_notes)
        warnings.extend(vector_warnings)

    if geometry is None:
        if not use_vector:
            notes.append(
                f"page classified {analysis.drawing_type.value}; the vector path was not applicable"
            )
        raster_geometry, raster_repairs, raster_notes = trace_page(
            fitz_page, region_bbox, tolerances.min_polygon_area
        )
        notes.extend(raster_notes)
        if raster_geometry is not None:
            geometry = raster_geometry
            method = Method.RASTER_TRACE
            repairs.extend(raster_repairs)
            warnings.append(
                "Geometry was traced from a rendered raster image, not from native "
                "vector paths. Edge positions are limited by render resolution and "
                "thresholding; confidence is reduced accordingly."
            )

    geometry, manual_notes = _apply_manual_boundary(geometry, manual_add, manual_subtract)
    if manual_notes:
        assumptions.extend(manual_notes)
        if method is Method.VECTOR_EXACT and geometry is not None and not contributing:
            method = Method.USER_POLYGON

    components: List[ProfileComponent] = []
    if geometry is not None:
        components, component_repairs = to_components(
            geometry,
            min_area=tolerances.min_polygon_area,
            simplify=tolerances.simplify,
            min_hole_area=tolerances.min_polygon_area,
        )
        repairs.extend(component_repairs)

    excluded = set(excluded_component_ids or ())
    if not excluded and components:
        auto_excluded, selection_warning = _auto_select_components(
            components, method, component_selection
        )
        excluded |= auto_excluded
        if selection_warning:
            warnings.append(selection_warning)
    for component in components:
        component.included = component.id not in excluded

    included = [c for c in components if c.included]
    net_units2 = sum(c.area_units2 for c in included)
    gross_units2 = sum(c.gross_area_units2 for c in included)
    hole_units2 = max(0.0, gross_units2 - net_units2)
    reported_units2 = net_units2 if subtract_holes else gross_units2

    report = GeometryReport(
        raw_primitive_count=len(selected),
        profile_primitive_count=len(contributing),
        ignored_primitive_count=len(ignored),
        segment_count=segment_count,
        face_count=face_count,
        component_count=len(components),
        outer_contours=len(included),
        holes=sum(len(c.holes) for c in included),
        repairs=[r for r in repairs if r.count > 0],
        role_counts=_role_counts(selected),
    )

    if not components:
        excluded_roles = sorted({p.role.value for p in ignored}) or ["none"]
        warnings.append(_no_profile_message(report, analysis.drawing_type, excluded_roles))

    if analysis.sheet_unit is None:
        assumptions.append(
            "The sheet does not declare its dimension unit; dimension values were "
            "read as millimetres."
        )
    else:
        assumptions.append(f"Sheet declares dimensions in {analysis.sheet_unit}.")

    assumptions.append(
        "Projected area is the union of the closed profile in the selected view; "
        + ("enclosed holes are subtracted." if subtract_holes else "enclosed holes are NOT subtracted.")
    )
    if ignored:
        assumptions.append(
            f"{len(ignored)} primitive(s) were excluded as "
            + ", ".join(sorted({p.role.value for p in ignored}))
            + "."
        )
    if region_bbox is None:
        warnings.append(
            "No drawing region was selected, so the whole page was used. On a sheet "
            "with several views this over-counts. Select a single view."
        )

    confidence = evaluate_confidence(
        drawing_type=analysis.drawing_type,
        method=method,
        scale=scale,
        view_source=view_source,
        primitives=contributing,
        component_count=len(included),
        repairs=report.repairs,
        extra_notes=notes,
        user_verified=bool(overrides or manual_notes),
    )

    result = AreaResult(
        document_id=document_id,
        file_name=file_name,
        page=analysis.page_number,
        method=method,
        drawing_type=analysis.drawing_type,
        view_source=view_source,
        view_label=view_label,
        region_bbox=region_bbox,
        scale=scale,
        geometry=report,
        confidence=confidence,
        components=components,
        area_units2=reported_units2,
        gross_area_units2=gross_units2,
        hole_area_units2=hole_units2,
        subtract_holes=subtract_holes,
        warnings=warnings,
        assumptions=assumptions,
        engine_version=ENGINE_VERSION,
        timestamp=_datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds"),
    )

    # The competing readings are attached after construction because each is
    # derived from the finished silhouette. They are additive: "projected_area"
    # still carries the primary union, and nothing here changes a number (§2).
    from backend.area.interpretations import interpretations, pending_cad_interpretations

    result.footprint_interpretations = interpretations(result)
    result.pending_interpretations = pending_cad_interpretations()
    return result


#: On a raster trace, a component smaller than this share of the largest is
#: almost always a dimension band or a note block rather than part of the
#: silhouette — there is no vector role information to tell them apart.
_RASTER_DOMINANT_FRACTION = 0.98


def _auto_select_components(
    components: List[ProfileComponent], method: Method, selection: str
) -> tuple:
    """Decide which silhouette components are included by default.

    The vector paths carry role classification, so every component they return
    has already survived annotation filtering and all of them count. A raster
    trace has no such information: dimension lines, extension lines and the part
    edge enclose regions that are indistinguishable from the profile itself. So
    a raster trace defaults to the dominant silhouette and says so, rather than
    quietly summing a part with the annotation around it (§30).

    Returns:
        ``(excluded_ids, warning_or_none)``.
    """
    if selection == "all" or not components:
        return set(), None
    force_largest = selection == "largest"
    if not force_largest and method is not Method.RASTER_TRACE:
        return set(), None
    if len(components) == 1:
        return set(), None

    dominant = max(components, key=lambda c: c.area_units2)
    excluded = {
        c.id
        for c in components
        if c.id != dominant.id and c.area_units2 < _RASTER_DOMINANT_FRACTION * dominant.area_units2
    }
    if not excluded:
        return set(), None
    reason = (
        "Traced geometry cannot be separated from dimension and annotation linework, "
        "so only the dominant silhouette is included by default. "
        if method is Method.RASTER_TRACE
        else "Only the dominant silhouette was included, as requested. "
    )
    return excluded, (
        reason
        + f"{len(excluded)} smaller component(s) were excluded: "
        + ", ".join(sorted(excluded))
        + ". Switch them back on if the view really contains several bodies."
    )


def profile_is_plausible(geometry: Optional[BaseGeometry], ink_bbox: BBox) -> bool:
    """Does this silhouette actually span the linework it was built from?

    See :data:`_MIN_BBOX_FILL` for why extent rather than area is the test.
    """
    if geometry is None or geometry.is_empty or ink_bbox.area <= 0:
        return False
    min_x, min_y, max_x, max_y = geometry.bounds
    return ((max_x - min_x) * (max_y - min_y)) / ink_bbox.area >= _MIN_BBOX_FILL


def _apply_manual_boundary(geometry, manual_add, manual_subtract):
    """Union in hand-drawn boundary rings and remove hand-erased ones.

    The engineer's boundary is authoritative where it is given: it is unioned
    with whatever was detected rather than replacing it, so a mostly-correct
    automatic profile can be patched at one corner instead of redrawn whole.

    Returns:
        ``(geometry, assumption_notes)``.
    """
    notes: List[str] = []
    added = union_polygons([p for p in (ring_to_polygon(r) for r in (manual_add or [])) if p])
    removed = union_polygons([p for p in (ring_to_polygon(r) for r in (manual_subtract or [])) if p])

    if added is not None:
        geometry = added if geometry is None else union_polygons([geometry, added])
        notes.append(
            f"{len(manual_add)} boundary region(s) drawn by hand were added to the profile."
        )
    if removed is not None and geometry is not None:
        geometry = geometry.difference(removed)
        geometry, _ = ensure_valid(geometry)
        if geometry.is_empty:
            geometry = None
        notes.append(
            f"{len(manual_subtract)} region(s) erased by hand were removed from the profile."
        )
    return geometry, notes


def _reconstruct_vector(contributing, tolerances, roles, close_gaps, ink_bbox, role_of=None):
    """Recover a silhouette from vector linework, escalating only as needed.

    Order of attempts, stopping at the first plausible profile:

    1. Exact polygonization at the page's normal closure tolerance.
    2. Exact polygonization with the closure tolerance widened, once per step in
       :data:`_CLOSURE_ESCALATION`.
    3. Scan-converted gap-closed silhouette at the same widened tolerances.

    Returns:
        ``(geometry, method, face_count, segment_count, repairs, notes, warnings)``.
    """
    repairs: List[Repair] = []
    notes: List[str] = []
    warnings: List[str] = []

    best_exact = None
    face_count = 0
    segment_count = 0
    last_network = None

    for step, multiplier in enumerate(_CLOSURE_ESCALATION):
        widened = replace(tolerances, closure=tolerances.closure * multiplier)
        network = build_network(
            contributing, widened, roles=roles, close_gaps=close_gaps, role_of=role_of
        )
        last_network = network
        exact = polygonize_network(network, widened.min_polygon_area)
        if step == 0:
            segment_count = len(network.segments)
            repairs.extend(network.repairs)
            repairs.extend(exact.repairs)
            notes.extend(exact.notes)
        face_count = max(face_count, exact.face_count)
        if best_exact is None and exact.ok:
            best_exact = exact.geometry

        if exact.ok and profile_is_plausible(exact.geometry, ink_bbox):
            if step > 0:
                repairs.extend(network.repairs)
                repairs.append(
                    Repair(
                        type="widen_closure_tolerance",
                        count=1,
                        magnitude_units=round(widened.closure, 4),
                        detail=(
                            f"closure tolerance raised {multiplier:g}x to "
                            f"{widened.closure:.3f} PDF units before the profile closed"
                        ),
                    )
                )
                warnings.append(
                    f"The profile did not close at the normal tolerance. The contour "
                    f"closure tolerance was raised {multiplier:g}x (to "
                    f"{widened.closure:.3f} PDF units) to close it. Check the "
                    f"highlighted boundary before trusting the number."
                )
            return (
                exact.geometry, Method.VECTOR_EXACT, exact.face_count,
                segment_count or len(network.segments), repairs, notes, warnings,
            )

    # Nothing closed exactly. Fall back to scan conversion, widening the same way.
    warnings.append(
        "Exact vector polygonization could not produce a profile spanning the "
        "linework, even with a widened closure tolerance. Fell back to gap-closed "
        "silhouette reconstruction, which is approximate — verify the highlighted "
        "boundary."
    )
    for multiplier in _CLOSURE_ESCALATION:
        if last_network is None:
            break
        closure = tolerances.closure * multiplier
        geometry, fallback_repairs, fallback_notes = gap_closed_silhouette(
            last_network.segments, ink_bbox, closure, tolerances.min_polygon_area, SILHOUETTE
        )
        notes.extend(fallback_notes)
        if geometry is not None and profile_is_plausible(geometry, ink_bbox):
            repairs.extend(fallback_repairs)
            return (
                geometry, Method.VECTOR_GAP_CLOSED, face_count, segment_count,
                repairs, notes, warnings,
            )

    # Return whatever the exact pass found, even if implausible: reporting a
    # small profile with a loud warning beats reporting nothing at all.
    if best_exact is not None:
        warnings.append(
            "The recovered profile does not span the linework in the selection. "
            "It is very likely a fragment rather than the component outline."
        )
        return best_exact, Method.VECTOR_EXACT, face_count, segment_count, repairs, notes, warnings
    return None, Method.VECTOR_EXACT, face_count, segment_count, repairs, notes, warnings


def _median_text_height(analysis) -> float:
    heights = [t.glyph_height for t in analysis.text_items if t.glyph_height > 0.1]
    if not heights:
        return 7.0
    heights.sort()
    return heights[len(heights) // 2]


def _no_profile_message(
    report: GeometryReport, drawing_type: DrawingType, excluded_roles: Sequence[str]
) -> str:
    """An actionable failure message. §31 — never just 'processing failed'."""
    return (
        "No valid closed profile could be reconstructed.\n\n"
        f"Detected:\n"
        f"  {report.raw_primitive_count} primitives in the selection "
        f"({drawing_type.value} page)\n"
        f"  {report.profile_primitive_count} classified as profile linework\n"
        f"  {report.ignored_primitive_count} excluded as "
        f"{', '.join(excluded_roles)}\n"
        f"  {report.segment_count} segments after cleaning\n"
        f"  {report.face_count} candidate faces\n"
        f"  0 valid closed profiles remained\n\n"
        "Suggested action:\n"
        "  Select a tighter region around one view, draw the boundary manually, "
        "or raise the contour closure tolerance if the outline has visible breaks."
    )
