"""Geometry correctness, validated against analytically known areas.

CONSTITUTION.md §25. These tests exercise the silhouette engine directly, with
no PDF involved, so a geometry regression cannot hide behind a PDF-reading
change.
"""

from __future__ import annotations

import math

import pytest

from backend.geometry.contours import polygonize_network
from backend.geometry.polygons import to_components
from backend.geometry.segments import SegmentNetwork, bridge_gaps, dedupe_segments, snap_segments
from backend.geometry.silhouette import gap_closed_silhouette
from backend.config import SILHOUETTE
from backend.models import BBox

#: Circles are drawn as polygons here; 512 segments keeps the discretisation
#: error two orders of magnitude below the tolerances we care about.
CIRCLE_SEGMENTS = 512
TOLERANCE = 1e-3  # relative


def ring(points):
    """Closed segment list from a list of vertices."""
    return list(zip(points, points[1:] + [points[0]]))


def circle(cx, cy, r, n=CIRCLE_SEGMENTS):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def silhouette_area(segments, min_area=1e-6):
    result = polygonize_network(SegmentNetwork(segments=list(segments)), min_area)
    assert result.ok, "expected a closed silhouette"
    return result.geometry.area


def assert_close(actual, expected, relative=TOLERANCE):
    assert abs(actual - expected) <= relative * abs(expected), f"{actual} != {expected}"


# ── the required fixtures from CONSTITUTION §25 ──────────────────────────────


def test_rectangle():
    """100 x 50 -> 5000."""
    assert_close(silhouette_area(ring([(0, 0), (100, 0), (100, 50), (0, 50)])), 5000.0)


def test_circle():
    """Diameter 100 -> 7853.9816."""
    assert_close(silhouette_area(ring(circle(0, 0, 50))), math.pi * 50 ** 2)


def test_plate_with_hole():
    """100 x 100 less Ø20 -> 9685.8407, and the hole is reported as a hole."""
    segments = ring([(0, 0), (100, 0), (100, 100), (0, 100)]) + ring(circle(50, 50, 10))
    result = polygonize_network(SegmentNetwork(segments=segments), 1e-6)
    assert_close(result.geometry.area, 10000.0 - math.pi * 100)

    components, _repairs = to_components(result.geometry, min_area=1e-6)
    assert len(components) == 1
    assert len(components[0].holes) == 1
    assert_close(components[0].gross_area_units2, 10000.0)


def test_overlapping_rectangles_are_not_double_counted():
    """Union semantics: the shared strip counts once."""
    segments = ring([(0, 0), (60, 0), (60, 50), (0, 50)]) + ring([(40, 0), (100, 0), (100, 50), (40, 50)])
    assert_close(silhouette_area(segments), 100 * 50)


def test_overlapping_rectangles_offset_in_both_axes():
    segments = ring([(0, 0), (60, 0), (60, 60), (0, 60)]) + ring([(40, 40), (100, 40), (100, 100), (40, 100)])
    expected = 60 * 60 + 60 * 60 - 20 * 20
    assert_close(silhouette_area(segments), expected)


def test_multiple_holes():
    segments = ring([(0, 0), (100, 0), (100, 100), (0, 100)])
    for cx, cy, r in ((25, 25, 8), (75, 25, 8), (50, 75, 12)):
        segments += ring(circle(cx, cy, r))
    expected = 10000 - math.pi * (64 + 64 + 144)
    result = polygonize_network(SegmentNetwork(segments=segments), 1e-6)
    assert_close(result.geometry.area, expected)
    components, _ = to_components(result.geometry, min_area=1e-6)
    assert len(components[0].holes) == 3


def test_island_inside_hole_is_solid_again():
    """Nesting alternates: outline solid, hole void, island solid."""
    segments = (
        ring([(0, 0), (100, 0), (100, 100), (0, 100)])
        + ring(circle(50, 50, 30))
        + ring(circle(50, 50, 10))
    )
    expected = 10000 - math.pi * 900 + math.pi * 100
    assert_close(silhouette_area(segments), expected)


def test_duplicate_geometry_does_not_inflate_area():
    """The same outline drawn three times still measures once."""
    outline = ring([(0, 0), (100, 0), (100, 50), (0, 50)])
    assert_close(silhouette_area(outline * 3), 5000.0)


def test_duplicate_segments_are_removed():
    outline = ring([(0, 0), (100, 0), (100, 50), (0, 50)])
    unique, _kept, repair = dedupe_segments(outline + list(reversed(outline)), tolerance=0.01)
    assert len(unique) == 4
    assert repair.count == 4


def test_open_contour_alone_encloses_nothing():
    """A dangling polyline must not be invented into a polygon."""
    result = polygonize_network(
        SegmentNetwork(segments=[((0, 0), (100, 0)), ((100, 0), (100, 50))]), 1e-6
    )
    assert not result.ok


def test_dangling_lines_do_not_change_a_closed_profile():
    """Extension lines and leaders bound no face and must be ignored."""
    segments = ring([(0, 0), (100, 0), (100, 50), (0, 50)])
    strays = [((100, 0), (140, 0)), ((100, 50), (140, 50)), ((-30, 25), (0, 25))]
    assert_close(silhouette_area(segments + strays), 5000.0)


def test_internal_rib_line_does_not_split_the_profile():
    segments = ring([(0, 0), (100, 0), (100, 50), (0, 50)]) + [((50, 0), (50, 50))]
    result = polygonize_network(SegmentNetwork(segments=segments), 1e-6)
    components, _ = to_components(result.geometry, min_area=1e-6)
    assert len(components) == 1
    assert_close(components[0].area_units2, 5000.0)


def test_small_gap_is_bridged_and_recorded():
    """A 0.4-unit break closes, and the repair says so."""
    segments = [
        ((0, 0), (60, 0)), ((60.4, 0), (100, 0)),
        ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0)),
    ]
    bridged, repair = bridge_gaps(segments, closure_tolerance=1.0)
    assert repair.count == 1
    assert repair.magnitude_units == pytest.approx(0.4, abs=1e-6)
    assert_close(silhouette_area(bridged), 5000.0, relative=1e-3)


def test_gap_wider_than_tolerance_is_not_bridged():
    """Repairs must never quietly exceed the tolerance they were given."""
    segments = [((0, 0), (60, 0)), ((65, 0), (100, 0))]
    _bridged, repair = bridge_gaps(segments, closure_tolerance=1.0)
    assert repair.count == 0


def test_snapping_merges_near_coincident_endpoints():
    segments = [
        ((0, 0), (100, 0)), ((100.0001, 0.0001), (100, 50)),
        ((100, 50), (0, 50)), ((0, 50), (0, 0)),
    ]
    snapped, repair = snap_segments(segments, tolerance=0.3)
    assert repair.count >= 1
    assert_close(silhouette_area(snapped), 5000.0)


def test_gap_closed_fallback_recovers_a_broken_outline():
    """Strategy V2 on linework that will not close exactly."""
    segments = [
        ((0, 0), (60, 0)), ((60.6, 0), (100, 0)),
        ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0)),
    ] + ring(circle(50, 25, 10, n=180))
    geometry, repairs, _notes = gap_closed_silhouette(
        segments, BBox(0, 0, 100, 50), closure_units=1.0, min_polygon_area=0.5,
        settings=SILHOUETTE,
    )
    assert geometry is not None
    assert_close(geometry.area, 5000.0 - math.pi * 100, relative=3e-3)
    assert any(r.type == "morphological_gap_close" for r in repairs)


def test_speck_faces_are_discarded():
    """Line crossings produce slivers that are not geometry."""
    segments = ring([(0, 0), (100, 0), (100, 50), (0, 50)]) + ring([(10, 10), (10.2, 10), (10.2, 10.2), (10, 10.2)])
    result = polygonize_network(SegmentNetwork(segments=segments), min_polygon_area=1.0)
    components, repairs = to_components(result.geometry, min_area=1.0)
    assert len(components) == 1
    assert not components[0].holes
    assert all(r.count >= 0 for r in repairs)


def test_filled_path_contributes_directly():
    """A solid fill is evidence of occupied area without polygonization."""
    network = SegmentNetwork(segments=[], filled_rings=[[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]])
    result = polygonize_network(network, 1e-6)
    assert_close(result.geometry.area, 100.0)
