"""The correctness oracle's own correctness.

docs/CAD_PERFORMANCE_ROADMAP.md rests on one rule — the current implementation is
the reference, and an optimised one must reproduce it. The rule is worth nothing
if the comparison that enforces it is lax, so these check that it catches what it
must and tolerates only what it should.

The production baselines themselves are not in this repository, because the
drawings are not either. What is tested here is the machinery, on synthetic data.
"""

from __future__ import annotations

import json
import os

import pytest

from tools.baseline import (
    AREA_RELATIVE_TOLERANCE,
    EXACT_FIELDS,
    _bounds,
    _close,
    _diff,
)


def _record(**overrides):
    """A minimal baseline record, with the fields the comparison reads."""
    record = {
        "source_sha256": "a" * 64,
        "units": {"insunits": 4, "name": "millimeters", "mm_per_unit": 1.0,
                  "declared": True},
        "primitive_count": 421517,
        "profile_primitive_count": 400000,
        "ignored_primitive_count": 21517,
        "segment_count": 1200000,
        "face_count": 9000,
        "component_count": 3388,
        "outer_contour_count": 3388,
        "hole_count": 3405,
        "layer_count": 23,
        "layers_with_entities": 15,
        "block_count": 197,
        "blocks_inserted": 46,
        "entity_counts": {"LINE": 100, "ARC": 20},
        "unsupported_counts": {},
        "text_count": 1200,
        "dimension_count": 149,
        "layouts": ["Model"],
        "xrefs": [],
        "scale_source": "cad_units",
        "scale_verified": True,
        "semantic_status": ["geometric", "provisional"],
        "warnings": [],
        "repairs": [],
        "role_counts": {"counted": 400000},
        "footprint_types": ["geometry_union", "bounding_rectangle"],
        "engine_version": "0.3.0",
        "footprints": [
            {"type": "geometry_union", "semantics": "geometric",
             "area_units2": 2024.95, "area_mm2": 2024950000.0, "confidence": 0.8,
             "requires_cad_semantics": False, "warnings": []},
            {"type": "bounding_rectangle", "semantics": "geometric",
             "area_units2": 5000.0, "area_mm2": 5000000000.0, "confidence": 0.9,
             "requires_cad_semantics": False, "warnings": []},
        ],
        "bounds": {"x0": 0.0, "y0": 0.0, "x1": 74999.8, "y1": 32000.0},
        "conversion": {
            "dwg_signature": "AC1015", "dwg_version": "AutoCAD 2000/2000i/2002",
            "intermediate_dxf_sha256": "b" * 64, "intermediate_dxf_bytes": 120100000,
            "warnings": [], "metadata_warnings": [],
        },
    }
    record.update(overrides)
    return record


def test_an_unchanged_result_reports_no_differences():
    assert _diff(_record(), _record()) == []


@pytest.mark.parametrize("field,changed", [
    ("component_count", 3389),
    ("hole_count", 3404),
    ("primitive_count", 421518),
    ("face_count", 8999),
    ("layer_count", 24),
    ("blocks_inserted", 47),
    ("scale_verified", False),
    ("scale_source", "operator"),
    ("semantic_status", ["geometric"]),
    ("warnings", ["something new"]),
    ("engine_version", "0.4.0"),
    ("entity_counts", {"LINE": 101, "ARC": 20}),
])
def test_every_count_and_status_must_match_exactly(field, changed):
    """A component appearing or vanishing is a topology change, whatever it does
    to the area. None of these may be absorbed as a cost of speed."""
    problems = _diff(_record(), _record(**{field: changed}))
    assert problems, f"a change to {field} went unnoticed"
    assert any(field in problem for problem in problems), problems


def test_a_one_part_per_million_area_change_is_caught():
    """Well inside any engine tolerance, and still a real change in what was
    measured — this is the case a lax comparison would wave through."""
    baseline = _record()
    drifted = _record()
    drifted["footprints"] = [dict(f) for f in baseline["footprints"]]
    drifted["footprints"][0]["area_mm2"] *= 1.000001
    problems = _diff(baseline, drifted)
    assert problems
    assert "geometry_union.area_mm2" in problems[0]


def test_floating_point_reassociation_is_tolerated():
    """Reordering the same operations can move the last bits. That must not fail,
    or the comparison is unusable on any real refactor."""
    baseline = _record()
    reassociated = _record()
    reassociated["footprints"] = [dict(f) for f in baseline["footprints"]]
    nudge = 1.0 + AREA_RELATIVE_TOLERANCE / 10.0
    reassociated["footprints"][0]["area_mm2"] *= nudge
    assert _diff(baseline, reassociated) == []


def test_a_reading_that_appears_or_disappears_is_reported():
    baseline = _record()
    fewer = _record(footprint_types=["geometry_union"])
    fewer["footprints"] = [baseline["footprints"][0]]
    problems = _diff(baseline, fewer)
    assert any("bounding_rectangle" in problem for problem in problems), problems


def test_geometry_moving_at_an_edge_is_reported_even_when_the_area_barely_changes():
    """A small piece lost at one edge can hide inside a large total, which is why
    the extent is part of the fingerprint."""
    baseline = _record()
    shifted = _record(bounds={"x0": 0.0, "y0": 0.0, "x1": 74000.0, "y1": 32000.0})
    problems = _diff(baseline, shifted)
    assert any("bounds.x1" in problem for problem in problems), problems


def test_a_different_source_file_is_reported_rather_than_compared():
    problems = _diff(_record(), _record(source_sha256="c" * 64))
    assert any("source_sha256" in problem for problem in problems)


def test_the_converted_dxf_is_part_of_the_record():
    """A converter that starts producing different DXF is a change worth seeing,
    even when the measured area survives it."""
    baseline = _record()
    other = _record()
    other["conversion"] = dict(baseline["conversion"], intermediate_dxf_sha256="d" * 64)
    problems = _diff(baseline, other)
    assert any("intermediate_dxf_sha256" in problem for problem in problems)


def test_conversion_duration_is_not_compared():
    """Wall time is a property of the machine, not of the engineering result."""
    assert "duration_seconds" not in EXACT_FIELDS
    assert all("seconds" not in field for field in EXACT_FIELDS)


def test_close_treats_none_and_zero_distinctly():
    assert _close(None, None)
    assert not _close(None, 0.0)
    assert not _close(0.0, None)
    assert _close(0.0, 0.0)


def test_bounds_are_none_when_there_is_no_geometry():
    class Empty:
        footprint_interpretations: list = []

    assert _bounds(Empty()) is None


def test_capture_writes_a_record_the_comparison_accepts(tmp_path, drawings):
    """End to end on a synthetic drawing: capture, then compare against itself."""
    from tools import baseline as baseline_module

    monkeyed = str(tmp_path / "baselines")
    original = baseline_module.BASELINE_DIR
    baseline_module.BASELINE_DIR = monkeyed
    try:
        path = drawings["plate_with_holes"]["path"]
        assert baseline_module.capture([path]) == 0
        written = os.path.join(monkeyed, os.path.basename(path) + ".baseline.json")
        record = json.load(open(written))

        # The fields the roadmap names as the reference data must all be present.
        for field in ("source_sha256", "component_count", "hole_count",
                      "primitive_count", "footprints", "warnings", "bounds",
                      "semantic_status", "scale_source", "engine_version"):
            assert field in record, f"{field} missing from the baseline"
        assert record["component_count"] >= 1
        assert record["footprints"], "no readings recorded"

        assert baseline_module.compare([path]) == 0, "a fresh capture failed its own comparison"
    finally:
        baseline_module.BASELINE_DIR = original


def test_a_cad_baseline_carries_the_declared_scale(tmp_path):
    """The baseline must use the route's scale resolution, not its own.

    The first version of this tool resolved the scale itself and silently lost the
    CAD-declared units: two production DWGs that the application measures to the
    square metre were recorded as "scale not verified". A baseline that disagrees
    with the application is worse than no baseline, so this pins the behaviour to
    a drawing that declares millimetres.
    """
    ezdxf = pytest.importorskip("ezdxf")
    from tools import baseline as baseline_module

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4  # millimetres, declared
    doc.layers.add("EQUIPMENT", color=3)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (3000, 0), (3000, 2000), (0, 2000)],
                       close=True, dxfattribs={"layer": "EQUIPMENT"})
    path = str(tmp_path / "declared.dxf")
    doc.saveas(path)

    record = baseline_module._measure(path)

    assert record["units"]["declared"] is True
    assert record["units"]["name"] == "millimeters"
    assert record["scale_verified"] is True, "a declared unit is a verified scale"
    assert record["scale_source"] == "cad_declared_units"
    assert record["scale_mm_per_unit"] == pytest.approx(1.0)

    union = next(f for f in record["footprints"] if f["type"] == "geometry_union")
    assert union["area_mm2"] is not None, "a declared scale must yield a physical area"
    # 3000 x 2000 mm, within the engine's reconstruction tolerance.
    assert union["area_mm2"] == pytest.approx(6_000_000.0, rel=0.01)


def test_an_undeclared_cad_baseline_records_the_refusal(tmp_path):
    """$INSUNITS 0 is the 101 DWG's case: unitless, so no physical area exists
    and the baseline must record that rather than inventing one."""
    ezdxf = pytest.importorskip("ezdxf")
    from tools import baseline as baseline_module

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0  # unitless
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (500, 0), (500, 400), (0, 400)], close=True)
    path = str(tmp_path / "unitless.dxf")
    doc.saveas(path)

    record = baseline_module._measure(path)

    assert record["units"]["declared"] is False
    assert record["scale_verified"] is False
    union = next(f for f in record["footprints"] if f["type"] == "geometry_union")
    assert union["area_mm2"] is None, "no units means no physical area"
    assert union["area_units2"] is not None, "the drawing-unit area is still recorded"
    assert record["warnings"], "and the refusal is explained"
