"""The CAD validation harness.

The harness exists to answer, from a real DXF: what does this file actually
declare, which of its metadata can separate the drawing, what does each group
measure on its own, and how does that compare with the PDF path.

These tests assert it reports those facts and — just as importantly — that it
never turns a layer *name* into a meaning. `FENCE` must come out as a group
called "FENCE" with an area, not as a safety perimeter (§7).
"""

from __future__ import annotations

import os

import pytest

ezdxf = pytest.importorskip("ezdxf")

from backend.cad.analysis import analysis_from_cad, cad_tolerances
from backend.cad.dxf import load_dxf
from backend.models import FootprintSemantics
from tools.validate_cad import (
    _stem_key,
    build_report,
    discriminability,
    find_pdf_for,
    group_footprints,
    measure_cad,
    validate_dxf,
    verdicts,
)


@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    """A DXF shaped like the production layouts: fence, equipment, conveyor."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name, colour, linetype in [
        ("EQUIPMENT", 3, "CONTINUOUS"),
        ("FENCE", 1, "DASHED"),
        ("CONVEYOR", 5, "CENTER"),
    ]:
        doc.layers.add(name, color=colour, linetype=linetype)

    motor = doc.blocks.new("MOTOR")
    motor.add_lwpolyline(
        [(0, 0), (400, 0), (400, 300), (0, 300)], close=True, dxfattribs={"layer": "EQUIPMENT"}
    )
    machine = doc.blocks.new("MACHINE")
    machine.add_lwpolyline(
        [(0, 0), (2000, 0), (2000, 1500), (0, 1500)], close=True, dxfattribs={"layer": "EQUIPMENT"}
    )
    machine.add_blockref("MOTOR", (800, 600))
    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (1000, 1000))
    msp.add_blockref("MACHINE", (5000, 1000))
    msp.add_lwpolyline(
        [(0, 0), (12000, 0), (12000, 6000), (0, 6000)], close=True, dxfattribs={"layer": "FENCE"}
    )
    msp.add_lwpolyline([(500, 3000), (11500, 3000)], dxfattribs={"layer": "CONVEYOR"})

    path = tmp_path_factory.mktemp("cadval") / "line.dxf"
    doc.saveas(str(path))
    return str(path)


@pytest.fixture(scope="module")
def drawing(layout):
    return load_dxf(layout)


# ── tolerances ───────────────────────────────────────────────────────────────


def test_cad_tolerances_are_stated_in_millimetres_not_scaled_to_extent():
    """A 12 m drawing in mm and the same drawing in metres must snap the same.

    Scaling tolerances to the drawing extent would make snapping 1000x coarser
    on the metric copy of an identical drawing, for no engineering reason.
    """
    in_mm = cad_tolerances(1.0)
    in_m = cad_tolerances(1000.0)
    assert in_mm.snap == pytest.approx(0.5)
    assert in_m.snap == pytest.approx(0.0005)
    # Both are 0.5 mm of real part.
    assert in_mm.snap * 1.0 == pytest.approx(in_m.snap * 1000.0)


def test_undeclared_units_fall_back_rather_than_assume_millimetres():
    fallback = cad_tolerances(None)
    assert fallback.snap == pytest.approx(0.30), "the engine's generic default"


# ── what the file declares ───────────────────────────────────────────────────


def test_metadata_axes_are_scored_for_whether_they_separate_anything(drawing):
    """Structural question only: can this axis split the drawing?"""
    report = discriminability(drawing)
    assert set(report) >= {"layer", "block", "linetype", "color", "entity_type", "space"}

    assert report["layer"]["useful"] is True
    assert report["layer"]["group_count"] == 3

    # Everything is in model space, so that axis separates nothing and says so.
    assert report["space"]["useful"] is False
    assert "separates nothing" in report["space"]["why_not"]

    for axis in report.values():
        for group in axis["groups"]:
            assert set(group) == {"value", "primitives", "ink_share", "bbox", "closed_loops"}


def test_the_harness_names_groups_but_never_interprets_them(drawing):
    """`FENCE` is a group called FENCE. It is not a safety perimeter."""
    groups = group_footprints(drawing, "line.dxf", "layer")
    values = {g.value for g in groups}
    assert {"EQUIPMENT", "FENCE", "CONVEYOR"} <= values

    for group in groups:
        assert group.semantics == FootprintSemantics.PROVISIONAL.value, (
            "a group's meaning is never settled by its name"
        )
        payload = group.as_dict()
        for forbidden in ("meaning", "footprint_type", "is_fence", "role"):
            assert forbidden not in payload


def test_measuring_a_layer_alone_recovers_area_the_whole_drawing_absorbs(drawing):
    """The central advantage of CAD metadata, demonstrated numerically.

    Measured whole, the machines sit inside the fence rectangle and are absorbed
    into that face — the interior union collapses to almost nothing. Measured on
    its own layer, the equipment is 2 x 2000 x 1500 mm = 6 m^2.
    """
    whole = measure_cad(drawing, "line.dxf")
    internal = next(
        i for i in whole.footprint_interpretations if i.type.value == "internal_union"
    )
    from backend.units import Area

    absorbed = Area(internal.area_mm2).to("m2")

    groups = {g.value: g for g in group_footprints(drawing, "line.dxf", "layer")}
    equipment = groups["EQUIPMENT"].area_m2
    fence = groups["FENCE"].area_m2

    # Two 2000 x 1500 machines (3.00 m^2 each), each with a 400 x 300 motor
    # outline inside it. The motor ring nests one level down, so it reads as a
    # hole in its machine face: 2 x (3.00 - 0.12) = 5.76 m^2. That is the
    # nesting-parity rule doing exactly what it should on nested CAD blocks.
    assert equipment == pytest.approx(5.76, rel=0.01), equipment
    assert fence == pytest.approx(72.0, rel=0.01), fence
    assert absorbed < 0.5, (
        f"measured whole, the machines are absorbed into the fence face "
        f"(interior union {absorbed} m2)"
    )
    assert equipment > absorbed * 5, (
        "per-layer measurement must recover what whole-drawing measurement absorbs: "
        f"equipment {equipment} m2 vs absorbed interior {absorbed} m2"
    )
    # An open centreline bounds no area, and must not be given one.
    assert groups["CONVEYOR"].area_m2 is None


def test_a_layer_footprint_is_measured_by_the_same_engine(drawing):
    """No separate maths for CAD — the group goes through compute_projected_area."""
    analysis = analysis_from_cad(drawing)
    assert analysis.drawing_type.value == "vector"
    assert analysis.primitives, "the adapter must hand over the CAD primitives"
    assert analysis.tolerances.snap == pytest.approx(0.5), "mm-based CAD tolerances"


# ── verdicts ─────────────────────────────────────────────────────────────────


def test_nothing_is_promoted_to_confirmed_without_a_human(drawing):
    """§7. Confirmation is a statement by a person, not an inference."""
    result = measure_cad(drawing, "line.dxf")
    rows = verdicts(result, discriminability(drawing))
    assert rows

    for row in rows:
        assert row["current"] in ("geometric", "provisional")
        assert row["current"] != "confirmed"
        if row["current"] == "provisional":
            assert row["verdict"] == "stays provisional"
            assert "person confirming" in row["to_become_confirmed"]
        else:
            assert row["verdict"] == "stays geometric"

    by_type = {r["type"]: r for r in rows}
    assert by_type["enclosing_boundary"]["current"] == "provisional"
    assert by_type["internal_union"]["current"] == "provisional"
    assert by_type["geometry_union"]["current"] == "geometric"
    assert by_type["bounding_rectangle"]["current"] == "geometric"


def test_verdict_says_what_is_missing_when_no_axis_separates(tmp_path):
    """A single-layer drawing must say the metadata cannot help."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], close=True)
    msp.add_lwpolyline([(200, 200), (800, 200), (800, 800), (200, 800)], close=True)
    msp.add_lwpolyline([(300, 300), (500, 300), (500, 500), (300, 500)], close=True)
    path = tmp_path / "flat.dxf"
    doc.saveas(str(path))

    drawing = load_dxf(str(path))
    rows = verdicts(measure_cad(drawing, "flat.dxf"), discriminability(drawing))
    provisional = [r for r in rows if r["current"] == "provisional"]
    assert provisional
    assert any("none of the" in r["to_become_confirmed"] for r in provisional)


# ── pairing and reporting ────────────────────────────────────────────────────


def test_pdf_pairing_survives_case_and_version_suffix_differences(tmp_path):
    """Real exports differ: `…-V3.6.dwg` beside `…-v3.6.pdf`."""
    (tmp_path / "101-SSY1070-GR-30PPM-Simplified-v3.6.pdf").write_bytes(b"%PDF-1.7\n")
    dxf = tmp_path / "101-SSY1070-GR-30PPM-Simplified-V3.6.dxf"
    dxf.write_text("")

    found = find_pdf_for(str(dxf), [str(tmp_path)])
    assert found is not None and found.endswith("v3.6.pdf")
    assert _stem_key("A-B_C.dxf") == _stem_key("a b c.PDF")


def test_full_run_writes_a_report_and_an_overlay(layout, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    record = validate_dxf(str(layout), str(out), None, None, 90)

    assert "error" not in record
    assert record["info"]["units"]["declared"] is True
    assert record["interpretations"]
    assert record["group_footprints"]
    assert record["verdicts"]
    assert record["overlay"] and os.path.exists(record["overlay"])
    assert os.path.getsize(record["overlay"]) > 1000
    assert os.path.exists(out / "line_cad.json")

    report = build_report([record], str(out))
    for section in (
        "Ingestion and units",
        "Inventory",
        "Which metadata can actually separate this drawing",
        "Footprint interpretations",
        "Footprint per layer",
        "Verdict per reading",
    ):
        assert section in report, f"missing section {section!r}"
    assert "$INSUNITS" in report
    assert "scale read from the header, not measured" in report
    assert "No layer or block name is given a meaning" in report
    assert os.path.exists(out / "CAD_REPORT.md")


def test_a_dwg_in_the_directory_is_reported_not_crashed(tmp_path):
    fake = tmp_path / "real.dwg"
    fake.write_bytes(b"AC1015" + b"\x00" * 256)
    record = validate_dxf(str(fake), str(tmp_path), None, None, 90)
    assert "error" in record
    assert "DWG" in record["error"] and "not a DXF" in record["error"]
