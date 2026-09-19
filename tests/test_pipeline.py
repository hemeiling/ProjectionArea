"""End-to-end validation against synthetic drawings with known areas.

CONSTITUTION.md §26: synthetic geometry tests are not enough — the pipeline has
to read a drawing that carries a sheet frame, a title block, dimension lines,
arrowheads, centrelines and hatching, and still measure only the part.
"""

from __future__ import annotations

import math

import fitz
import pytest

from backend.area.projected import compute_projected_area
from backend.models import (
    DrawingType,
    GeometryRole,
    Method,
    Scale,
    ScaleSource,
    ViewSource,
    median_glyph_height,
)
from backend.pdf.space import map_point, page_space_matrix
from backend.pdf.text import extract_text_items
from backend.pipeline import prepare_page, region_scale
from backend.demo.drawings import build_rotated_plate


def measure(path, region_label=None, **kwargs):
    """Run the whole pipeline on page 1 and return (result, prepared, region)."""
    doc = fitz.open(path)
    prepared = prepare_page(doc, 1)
    region = None
    if region_label:
        matches = [r for r in prepared.regions if region_label in r.label]
        assert matches, f"no region labelled {region_label!r} in {[r.label for r in prepared.regions]}"
        region = matches[0]
    else:
        region = prepared.default_region()

    scale = kwargs.pop("scale", None)
    warnings = []
    if scale is None:
        scale, warnings = region_scale(prepared, region)

    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=doc.load_page(0),
        document_id="test",
        file_name=path.split("/")[-1],
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.USER_SELECTED if region else ViewSource.WHOLE_PAGE,
        view_label=region.label if region else "Whole page",
        extra_warnings=warnings,
        **kwargs,
    )
    doc.close()
    return result, prepared, region


# ── Path A: native vector drawings ───────────────────────────────────────────


def test_plate_with_holes_end_to_end(drawings):
    truth = drawings["plate_with_holes"]
    result, prepared, _region = measure(truth["path"], "TOP VIEW")

    assert prepared.analysis.drawing_type is DrawingType.VECTOR
    assert result.method is Method.VECTOR_EXACT
    assert result.scale.verified
    assert result.scale.source is ScaleSource.DIMENSION_CONSENSUS
    assert result.scale.mm_per_unit == pytest.approx(truth["mm_per_unit"], rel=1e-3)
    assert result.geometry.holes == truth["hole_count"]
    assert result.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    assert result.gross_area_mm2 == pytest.approx(truth["gross_area_mm2"], rel=2e-3)
    assert result.confidence.band == "high"


def test_holes_can_be_kept_in_the_area(drawings):
    truth = drawings["plate_with_holes"]
    result, _prepared, _region = measure(truth["path"], "TOP VIEW", subtract_holes=False)
    assert result.area_mm2 == pytest.approx(truth["gross_area_mm2"], rel=2e-3)


def test_title_block_and_sheet_frame_are_not_the_part(drawings):
    """The sheet border must never be offered as a view."""
    _result, prepared, _region = measure(drawings["plate_with_holes"]["path"], "TOP VIEW")
    kinds = {r.kind for r in prepared.regions}
    assert "title_block" in kinds
    assert prepared.default_region().kind == "view"
    assert prepared.role_counts.get("sheet", 0) >= 1


def test_centrelines_and_dimensions_are_excluded(drawings):
    """Dashed centrelines, arrowheads and dimension lines are classified out."""
    result, prepared, _region = measure(drawings["plate_with_holes"]["path"], "TOP VIEW")
    dashed_roles = {GeometryRole.CENTERLINE.value, GeometryRole.HIDDEN.value}
    assert dashed_roles & set(prepared.role_counts), prepared.role_counts
    assert prepared.role_counts.get("dimension", 0) >= 6
    assert result.geometry.ignored_primitive_count > 0


def test_hatching_is_classified_out(drawings):
    """A hatched section view measures its outline, not its fill lines."""
    truth = drawings["two_views"]
    result, prepared, _region = measure(truth["path"], "FRONT VIEW")
    assert prepared.role_counts.get("hatch", 0) >= 10
    assert result.area_mm2 == pytest.approx(truth["front_area_mm2"], rel=2e-3)


def test_two_views_are_measured_separately(drawings):
    """Selecting a view must not drag in its neighbour."""
    truth = drawings["two_views"]
    top, _p, _r = measure(truth["path"], "TOP VIEW")
    front, _p, _r = measure(truth["path"], "FRONT VIEW")
    assert top.area_mm2 == pytest.approx(truth["top_net_area_mm2"], rel=2e-3)
    assert front.area_mm2 == pytest.approx(truth["front_area_mm2"], rel=2e-3)
    assert top.geometry.holes == 1
    assert front.geometry.holes == 0


def test_large_scale_layout_drawing(drawings):
    """1:100 floor plan: same engine, dimensions in metres, area in m²."""
    truth = drawings["layout_1_100"]
    result, _prepared, _region = measure(truth["path"])
    assert result.scale.mm_per_unit == pytest.approx(truth["mm_per_unit"], rel=1e-3)
    assert result.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    from backend.units import Area
    assert Area(result.area_mm2).to("m2") == pytest.approx(54.46, rel=5e-3)


def test_curved_profile_with_dimensions_drawn_through_it(drawings):
    """Bezier arcs, and dimensions crossing the face rather than sitting outside.

    Both are routine on a crowded sheet and both break naive pipelines: the arcs
    have to be flattened accurately, and the dimension linework has to be
    demoted or it slices the face into pieces.
    """
    truth = drawings["obround_with_slot"]
    result, prepared, _region = measure(truth["path"])

    assert result.method is Method.VECTOR_EXACT
    assert result.geometry.outer_contours == 1, "the profile must not be sliced up"
    assert result.geometry.holes == 1, "the slot must read as a hole, not a second body"
    assert result.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    assert result.gross_area_mm2 == pytest.approx(truth["gross_area_mm2"], rel=2e-3)
    assert prepared.role_counts.get("dimension", 0) >= 4


def test_bezier_flattening_is_accurate_enough_to_ignore(drawings):
    """Discretisation error must stay far below any drawing tolerance."""
    truth = drawings["obround_with_slot"]
    result, _prepared, _region = measure(
        truth["path"],
        scale=Scale(mm_per_unit=truth["mm_per_unit"], source=ScaleSource.USER_TWO_POINT,
                    confidence=0.97, detail="exact fixture scale"),
    )
    error = abs(result.area_mm2 - truth["net_area_mm2"]) / truth["net_area_mm2"]
    assert error < 5e-4, f"curve flattening error {error * 100:.4f}% is too large"


def test_broken_contour_is_repaired_and_the_repair_is_reported(drawings):
    """§16/§30: recovery is allowed, silence is not."""
    truth = drawings["broken_contour"]
    result, _prepared, _region = measure(truth["path"])
    assert result.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=5e-3)
    assert result.method in (Method.VECTOR_GAP_CLOSED, Method.VECTOR_EXACT)
    assert result.geometry.repairs, "a repaired contour must leave an audit trail"
    assert result.warnings, "an approximate reconstruction must warn"
    assert result.confidence.overall < 0.95


# ── Path B: raster drawings ──────────────────────────────────────────────────


def test_raster_page_is_classified_and_traced(drawings):
    truth = drawings["raster_plate"]
    result, prepared, _region = measure(
        truth["path"],
        scale=Scale(mm_per_unit=0.7055555555, source=ScaleSource.USER_TWO_POINT,
                    confidence=0.95, detail="test calibration"),
    )
    assert prepared.analysis.drawing_type is DrawingType.RASTER
    assert result.method is Method.RASTER_TRACE
    assert result.area_mm2 == pytest.approx(23057.52, rel=0.02)
    assert result.confidence.overall < 0.85, "a trace must never score like exact vectors"
    assert any("raster" in w.lower() for w in result.warnings)


def test_raster_page_offers_regions(drawings):
    """A scanned sheet still needs a view to select."""
    _result, prepared, region = measure(
        drawings["raster_plate"]["path"],
        scale=Scale(mm_per_unit=0.7, source=ScaleSource.USER_TWO_POINT, confidence=0.9, detail=""),
    )
    assert prepared.regions
    assert region is not None


# ── the refusal to fabricate ─────────────────────────────────────────────────


def test_no_scale_means_no_physical_area(drawings):
    """§3: the single most important behaviour in the system."""
    result, _prepared, _region = measure(
        drawings["plate_with_holes"]["path"], "TOP VIEW",
        scale=Scale(mm_per_unit=None, source=ScaleSource.NONE, confidence=0.0, detail="none"),
    )
    assert result.area_mm2 is None
    assert result.area_units2 > 0, "geometry is still computed, just not converted"
    assert result.confidence.overall == 0.0

    payload = result.as_dict()
    assert payload["projected_area"]["verified"] is False
    assert payload["projected_area"]["net"] is None
    assert "Scale not verified" in payload["projected_area"]["message"]


def test_whole_page_selection_is_warned_about(drawings):
    doc = fitz.open(drawings["two_views"]["path"])
    prepared = prepare_page(doc, 1)
    scale, _warnings = region_scale(prepared, None)
    result = compute_projected_area(
        analysis=prepared.analysis, fitz_page=doc.load_page(0), document_id="t",
        file_name="t", scale=scale, region_bbox=None, view_source=ViewSource.WHOLE_PAGE,
        view_label="Whole page",
    )
    doc.close()
    assert any("whole page" in w.lower() for w in result.warnings)
    assert result.confidence.view < 0.6


def test_result_is_reproducible_and_self_describing(drawings):
    """§24/§42: a result must answer every audit question on its own."""
    result, _prepared, _region = measure(drawings["plate_with_holes"]["path"], "TOP VIEW")
    payload = result.as_dict()
    for key in ("document_id", "file_name", "page", "method", "drawing_type", "view",
                "projected_area", "scale", "geometry", "confidence", "components",
                "warnings", "assumptions", "engine_version", "timestamp"):
        assert key in payload, key
    assert payload["scale"]["evidence"], "the scale must show its working"
    assert payload["confidence"]["components"]
    assert payload["components"][0]["outer"], "the measured boundary must be returned for overlay"


def test_repeated_measurement_is_deterministic(drawings):
    first, _p, _r = measure(drawings["plate_with_holes"]["path"], "TOP VIEW")
    second, _p, _r = measure(drawings["plate_with_holes"]["path"], "TOP VIEW")
    assert first.area_units2 == pytest.approx(second.area_units2, rel=1e-12)
    assert first.scale.mm_per_unit == pytest.approx(second.scale.mm_per_unit, rel=1e-12)


def test_nothing_reconstructed_is_not_a_verified_zero():
    """§3: 'no profile found' must never render as a measured 0.00 mm²."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    shape = page.new_shape()
    for i in range(20):  # parallel open linework only — classified as hatching
        shape.draw_line(fitz.Point(50 + i * 20, 100), fitz.Point(60 + i * 20, 300))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
    shape.commit()

    prepared = prepare_page(doc, 1)
    result = compute_projected_area(
        analysis=prepared.analysis, fitz_page=doc.load_page(0), document_id="t",
        file_name="open.pdf",
        scale=Scale(mm_per_unit=0.35, source=ScaleSource.USER_TWO_POINT,
                    confidence=0.9, detail="test"),
        view_source=ViewSource.WHOLE_PAGE, view_label="Whole page",
    )
    doc.close()

    payload = result.as_dict()
    assert result.has_profile is False
    assert result.area_mm2 is None
    assert payload["projected_area"]["verified"] is False
    assert "No closed profile" in payload["projected_area"]["message"]
    assert payload["confidence"]["percent"] == 0

    failure = next(w for w in result.warnings if "No valid closed profile" in w)
    assert "Suggested action" in failure
    assert "hatch" in failure, "the message must name the roles actually excluded"


def test_parallel_evenly_spaced_lines_are_read_as_hatching():
    """The classifier that produced the case above, asserted directly."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    shape = page.new_shape()
    for i in range(20):
        shape.draw_line(fitz.Point(50 + i * 20, 100), fitz.Point(60 + i * 20, 300))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
    shape.commit()
    prepared = prepare_page(doc, 1)
    doc.close()
    assert prepared.role_counts.get("hatch", 0) >= 18


# ── page rotation (/Rotate) ──────────────────────────────────────────────────
#
# `get_drawings()` and `get_text()` report unrotated media-box coordinates while
# `page.rect` reports rotated ones. Mixing the two silently reinterprets an A3
# landscape sheet as portrait: the sheet frame then spans 138 % of the believed
# page width but only 68 % of its height, fails the "spans most of the page"
# test, keeps the `profile` role and is measured as if it were the part. The
# annotation-height yardstick breaks the same way, because a rotated span's
# `bbox.height` is the length of the string rather than the height of its
# letters. backend/pdf/space.py normalises both at the source-adapter seam.
#
# The invariant asserted here: rotating a sheet must not move a single number.


def _rotated(tmp_path, drawings, rotation):
    """The plate drawing re-issued with ``/Rotate rotation``."""
    target = tmp_path / f"rotated_{rotation}.pdf"
    return build_rotated_plate(str(target), drawings["plate_with_holes"]["path"], rotation)


def measure_densest_region(path, **kwargs):
    """Measure the region holding the most linework, ignoring its label.

    Region *labels* are deliberately orientation-dependent — a title block is
    recognised by sitting at the bottom-right of the displayed sheet — and a
    ``/Rotate 180`` page displays upside-down, which legitimately puts the part
    where the title block belongs. The measurement must not depend on that, so
    these tests pick the part by geometry and assert only the numbers.
    """
    doc = fitz.open(path)
    prepared = prepare_page(doc, 1)

    def linework(region):
        return sum(
            1
            for prim in prepared.analysis.primitives
            if region.bbox.x0 <= prim.bbox.center[0] <= region.bbox.x1
            and region.bbox.y0 <= prim.bbox.center[1] <= region.bbox.y1
        )

    region = max(prepared.regions, key=linework)
    scale, warnings = region_scale(prepared, region)
    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=doc.load_page(0),
        document_id="test",
        file_name=path.split("/")[-1],
        scale=scale,
        region_bbox=region.bbox,
        view_source=ViewSource.USER_SELECTED,
        view_label=region.label,
        extra_warnings=warnings,
        **kwargs,
    )
    doc.close()
    return result, prepared, region


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_measures_the_same_area(drawings, tmp_path, rotation):
    """Scale, geometry and area are invariant under /Rotate. 90° is the case
    that actually regressed; 180/270 come free, and 180 keeps the page portrait
    so it isolates the coordinate fix from the width/height swap."""
    truth = _rotated(tmp_path, drawings, rotation)
    result, prepared, _region = measure_densest_region(truth["path"])

    assert prepared.analysis.rotation == rotation
    assert prepared.analysis.drawing_type is DrawingType.VECTOR
    assert result.method is Method.VECTOR_EXACT
    assert result.scale.verified
    assert result.scale.source is ScaleSource.DIMENSION_CONSENSUS
    assert result.scale.mm_per_unit == pytest.approx(truth["mm_per_unit"], rel=1e-3)
    assert result.geometry.holes == truth["hole_count"]
    assert result.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    assert result.gross_area_mm2 == pytest.approx(truth["gross_area_mm2"], rel=2e-3)
    assert result.confidence.band == "high"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_is_bit_identical_to_the_unrotated_sheet(drawings, tmp_path, rotation):
    """The strongest form of the invariant: same area, same scale, same roles.

    Rotation by a multiple of 90° is rigid, so there is no floating-point excuse
    for the numbers to drift — they must match exactly, not approximately.
    """
    upright, upright_prepared, _ = measure_densest_region(drawings["plate_with_holes"]["path"])
    rotated, rotated_prepared, _ = measure_densest_region(
        _rotated(tmp_path, drawings, rotation)["path"]
    )

    assert rotated.area_mm2 == upright.area_mm2
    assert rotated.area_units2 == upright.area_units2
    assert rotated.scale.mm_per_unit == upright.scale.mm_per_unit
    assert rotated.geometry.holes == upright.geometry.holes
    assert rotated.geometry.component_count == upright.geometry.component_count
    assert rotated.geometry.face_count == upright.geometry.face_count
    assert rotated.confidence.overall == upright.confidence.overall
    # Role classification must not drift either: the sheet frame stays demoted
    # and the dimension yardstick stays the height of the letters.
    assert rotated_prepared.role_counts == upright_prepared.role_counts
    assert rotated_prepared.role_counts.get("sheet", 0) >= 1, (
        "the sheet frame must still be recognised on a rotated page, or it gets "
        "measured as if it were the part"
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_geometry_is_normalised_into_the_displayed_page_box(
    drawings, tmp_path, rotation
):
    """Coordinates must land in `page.rect` space — the space PDF.js paints.

    Without this the audit overlay is drawn in a different coordinate system
    than the viewer, so every green/red region on a rotated sheet is misplaced
    and the result stops being auditable at all.
    """
    if rotation == 0:
        path = drawings["plate_with_holes"]["path"]
    else:
        path = _rotated(tmp_path, drawings, rotation)["path"]

    doc = fitz.open(path)
    page = doc.load_page(0)
    analysis = prepare_page(doc, 1).analysis

    assert (analysis.width, analysis.height) == (page.rect.width, page.rect.height)

    points = [p for prim in analysis.primitives for p in prim.points]
    assert points
    pad = 1.0
    assert min(p[0] for p in points) >= -pad
    assert min(p[1] for p in points) >= -pad
    assert max(p[0] for p in points) <= analysis.width + pad
    assert max(p[1] for p in points) <= analysis.height + pad

    # Text must share that space, or dimension matching pairs an annotation with
    # linework that is nowhere near it and the scale is recovered from nonsense.
    boxes = [t.bbox for t in analysis.text_items]
    assert boxes
    assert max(b.x1 for b in boxes) <= analysis.width + pad
    assert max(b.y1 for b in boxes) <= analysis.height + pad

    doc.close()


def test_rotated_coordinates_are_exactly_the_rotation_of_the_originals(drawings, tmp_path):
    """Not merely in-bounds: each point is the /Rotate image of its original."""
    upright = fitz.open(drawings["plate_with_holes"]["path"])
    rotated = fitz.open(_rotated(tmp_path, drawings, 90)["path"])
    matrix = page_space_matrix(rotated.load_page(0))

    a = prepare_page(upright, 1).analysis
    b = prepare_page(rotated, 1).analysis
    assert len(a.primitives) == len(b.primitives)

    for original, moved in zip(a.primitives, b.primitives):
        assert original.kind is moved.kind
        assert len(original.points) == len(moved.points)
        for src, dst in zip(original.points, moved.points):
            expected = map_point(src, matrix)
            assert dst[0] == pytest.approx(expected[0], abs=1e-9)
            assert dst[1] == pytest.approx(expected[1], abs=1e-9)

    upright.close()
    rotated.close()


def test_annotation_height_yardstick_is_rotation_invariant():
    """The median glyph height must not become the length of the string.

    A 90°-rotated span has a tall, narrow bounding box. Reading its height as
    the annotation size inflated the dimension-matching reach by the text's
    aspect ratio — on the plate fixture from 33 to 147 units — which swept the
    dimension *extension* lines into the dimension role and out of the profile.
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text(fitz.Point(50, 100), "DIMENSION 200", fontsize=9)
    horizontal = extract_text_items(page)
    doc.close()

    rotated_doc = fitz.open()
    rotated_page = rotated_doc.new_page(width=400, height=400)
    rotated_page.insert_text(fitz.Point(50, 100), "DIMENSION 200", fontsize=9)
    rotated_page.set_rotation(90)
    rotated = extract_text_items(rotated_page)
    rotated_doc.close()

    assert horizontal and rotated
    span, rotated_span = horizontal[0], rotated[0]

    # The bounding box genuinely flips: that is the trap being guarded.
    assert rotated_span.bbox.height == pytest.approx(span.bbox.width, abs=1e-6)
    assert rotated_span.bbox.width == pytest.approx(span.bbox.height, abs=1e-6)
    # The yardstick does not.
    assert rotated_span.glyph_height == pytest.approx(span.glyph_height, abs=1e-6)
    assert median_glyph_height(rotated) == pytest.approx(median_glyph_height(horizontal), abs=1e-6)


def test_rotate_90_sheet_still_offers_the_captioned_view_and_its_title_block(
    drawings, tmp_path
):
    """On the required 90° case the caption and the sheet furniture survive.

    A `/Rotate 90` sheet displays sideways but stays the right way up, so view
    captions still attach to their linework and the part is still selectable by
    name — the ordinary path a user takes through the UI.
    """
    result, prepared, region = measure(_rotated(tmp_path, drawings, 90)["path"], "TOP VIEW")

    assert region.kind == "view"
    assert prepared.role_counts.get("sheet", 0) >= 1
    assert result.geometry.holes == 3
    assert result.area_mm2 == pytest.approx(
        drawings["plate_with_holes"]["net_area_mm2"], rel=2e-3
    )
