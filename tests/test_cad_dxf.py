"""The DXF source adapter.

CONSTITUTION.md §15/§24/§37. The point of this path is *provenance*: the
production PDFs carry geometry and nothing else, so the engine can measure their
linework but cannot tell a site boundary from a machine. The DXF still holds
layers, blocks, declared units and real dimension values, and these tests exist
to make sure none of that is flattened away on the way in.

They assert what is preserved, never what anything *means*. No test here says a
layer called FENCE is a safety perimeter — that is a production rule, and
production rules come from real drawings with evidence, not from a fixture.
"""

from __future__ import annotations

import math

import pytest

ezdxf = pytest.importorskip("ezdxf")

from backend.cad.dxf import CadDrawing, DxfReadError, load_dxf, scale_from_cad_units
from backend.models import ScaleSource


@pytest.fixture(scope="module")
def layout_dxf(tmp_path_factory):
    """A small line-layout DXF exercising everything the adapter must keep.

    Deliberately shaped like the production drawings: a fenced site rectangle,
    equipment inside it as nested blocks, a conveyor centreline, annotation and a
    real linear dimension.
    """
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres

    for name, colour, linetype in [
        ("EQUIPMENT", 3, "CONTINUOUS"),
        ("FENCE", 1, "DASHED"),
        ("CONVEYOR", 5, "CENTER"),
        ("TEXT", 7, "CONTINUOUS"),
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
    machine.add_circle((1000, 750), 250, dxfattribs={"layer": "EQUIPMENT"})
    machine.add_blockref("MOTOR", (1400, 1000))

    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (1000, 1000))
    msp.add_blockref("MACHINE", (5000, 1000), dxfattribs={"rotation": 90})
    msp.add_lwpolyline(
        [(0, 0), (12000, 0), (12000, 6000), (0, 6000)], close=True, dxfattribs={"layer": "FENCE"}
    )
    msp.add_lwpolyline([(500, 3000), (11500, 3000)], dxfattribs={"layer": "CONVEYOR"})
    msp.add_arc((6000, 4500), 800, 0, 180, dxfattribs={"layer": "EQUIPMENT"})
    msp.add_text("OP-01 Cell Loading", height=120, dxfattribs={"layer": "TEXT"}).set_placement(
        (800, 5200)
    )
    msp.add_mtext(
        "LINE FOOTPRINT 12000 x 6000", dxfattribs={"layer": "TEXT", "char_height": 150}
    ).set_location((800, 5600))
    msp.add_linear_dim(
        base=(0, -400), p1=(0, 0), p2=(12000, 0), dxfattribs={"layer": "TEXT"}
    ).render()

    path = tmp_path_factory.mktemp("cad") / "layout.dxf"
    doc.saveas(str(path))
    return str(path)


@pytest.fixture(scope="module")
def drawing(layout_dxf) -> CadDrawing:
    return load_dxf(layout_dxf)


# ── the file's own statements ────────────────────────────────────────────────


def test_declared_units_give_a_scale_without_measuring_anything(drawing):
    """The central advantage of the CAD path: scale is read, not inferred."""
    info = drawing.info
    assert info.units_declared
    assert info.insunits == 4
    assert info.units_name == "millimeters"
    assert info.mm_per_unit == 1.0

    scale = scale_from_cad_units(info)
    assert scale.verified
    assert scale.source is ScaleSource.CAD_UNITS
    assert scale.mm_per_unit == 1.0
    assert "$INSUNITS" in " ".join(scale.evidence)
    assert "nothing was measured" in " ".join(scale.evidence)


def test_undeclared_units_refuse_rather_than_assume_millimetres(tmp_path):
    """§3: an unset $INSUNITS is not a licence to assume mm."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
    path = tmp_path / "unitless.dxf"
    doc.saveas(str(path))

    info = load_dxf(str(path)).info
    assert info.units_declared is False
    assert info.mm_per_unit is None
    assert any("does not declare its units" in n for n in info.notes)

    scale = scale_from_cad_units(info)
    assert scale.verified is False
    assert scale.source is ScaleSource.NONE


def test_layers_linetypes_and_colours_are_preserved_verbatim(drawing):
    """Layer names are the raw material of every future semantic rule."""
    by_name = {layer.name: layer for layer in drawing.info.layers}
    assert {"EQUIPMENT", "FENCE", "CONVEYOR", "TEXT"} <= set(by_name)

    assert by_name["FENCE"].linetype == "DASHED"
    assert by_name["CONVEYOR"].linetype == "CENTER"
    assert by_name["EQUIPMENT"].color == 3
    assert by_name["FENCE"].color == 1

    # And the geometry actually carries its layer through.
    groups = drawing.by_layer()
    assert set(groups) >= {"EQUIPMENT", "FENCE", "CONVEYOR"}
    assert len(groups["FENCE"]) == 1
    assert len(groups["CONVEYOR"]) == 1
    assert len(groups["EQUIPMENT"]) >= 6


def test_no_semantic_meaning_is_assigned_to_any_layer(drawing):
    """The adapter reads; it does not interpret.

    A layer called FENCE must arrive as the string "FENCE" and nothing more. The
    moment the adapter starts deciding what layers mean, it is inventing a
    production rule ahead of the evidence.
    """
    for prov in drawing.provenance.values():
        assert not hasattr(prov, "semantic_role")
        assert not hasattr(prov, "footprint_type")
    for primitive in drawing.primitives:
        # role stays at the engine's default; the adapter never pre-classifies.
        assert primitive.role.value in ("profile", "uncertain")


# ── structure that the PDF export loses ──────────────────────────────────────


def test_nested_blocks_keep_their_hierarchy_and_transforms(drawing):
    """MOTOR inside MACHINE must arrive knowing it is inside MACHINE."""
    nested = [p for p in drawing.provenance.values() if p.block_depth >= 2]
    assert nested, "no nested-block geometry survived"
    assert all(p.block_path[:2] == ["MACHINE", "MOTOR"] for p in nested)
    assert all(p.owner_block == "MOTOR" for p in nested)
    assert all(len(p.insert_handles) == p.block_depth for p in nested)
    assert all(p.transform is not None for p in nested), "the placement must be reproducible"

    groups = drawing.by_block()
    assert groups["MOTOR"], "the inner block must be addressable on its own"
    assert groups["MACHINE"], "and so must the outer one"


def test_a_block_placed_twice_yields_two_sets_of_geometry(drawing):
    """The second MACHINE is rotated 90 degrees; both placements must exist."""
    machine_rects = [
        prim
        for prim in drawing.primitives
        if drawing.provenance[prim.index].owner_block == "MACHINE"
        and drawing.provenance[prim.index].entity_type == "LWPOLYLINE"
    ]
    assert len(machine_rects) == 2, "one rectangle per placement"

    def extent(prim):
        xs = [p[0] for p in prim.points]
        ys = [p[1] for p in prim.points]
        return (round(max(xs) - min(xs)), round(max(ys) - min(ys)))

    extents = sorted(extent(p) for p in machine_rects)
    # 2000 x 1500 placed once upright and once rotated a quarter turn.
    assert extents == [(1500, 2000), (2000, 1500)], extents


def test_every_primitive_records_its_handle_and_original_coordinates(drawing):
    """§15: the raw coordinates are never overwritten by the placed ones."""
    for prim in drawing.primitives:
        prov = drawing.provenance[prim.index]
        assert prov.handle, "a primitive with no handle cannot be traced back"
        assert prov.entity_type
        assert prov.source_file.endswith(".dxf")
        assert prov.raw_points, "original coordinates must be kept"
        assert len(prov.raw_points) == len(prim.points)

    # For geometry inside a placed block the two must actually differ.
    moved = [
        (drawing.provenance[p.index], p)
        for p in drawing.primitives
        if drawing.provenance[p.index].block_depth > 0
    ]
    assert moved
    assert any(
        prov.raw_points[0] != prim.points[0] for prov, prim in moved
    ), "placed geometry must differ from its block-local original"


def test_text_and_dimensions_survive_with_their_layers(drawing):
    """Exactly what the PDF export destroyed."""
    texts = {t.text.strip(): t for t in drawing.texts}
    assert any("OP-01" in t for t in texts)
    assert any("LINE FOOTPRINT" in t for t in texts)
    for text in drawing.texts:
        assert text.provenance.layer == "TEXT"
        assert text.height > 0

    assert drawing.dimensions, "dimension entities must be read"
    measurement = drawing.dimensions[0].measurement
    assert measurement == pytest.approx(12000.0, rel=1e-6), (
        "a DXF states what a dimension measures — no matching, no OCR"
    )


def test_curved_geometry_is_flattened_finely_not_dropped(drawing):
    """A circle must arrive as a circle, not a triangle or nothing."""
    circles = [
        prim
        for prim in drawing.primitives
        if drawing.provenance[prim.index].entity_type == "CIRCLE"
    ]
    assert len(circles) == 2, "one per MACHINE placement"
    for circle in circles:
        xs = [p[0] for p in circle.points]
        ys = [p[1] for p in circle.points]
        assert len(circle.points) >= 40, "too coarse to measure"
        assert max(xs) - min(xs) == pytest.approx(500.0, rel=1e-3)
        assert max(ys) - min(ys) == pytest.approx(500.0, rel=1e-3)

    arcs = [
        prim
        for prim in drawing.primitives
        if drawing.provenance[prim.index].entity_type == "ARC"
    ]
    assert len(arcs) == 1 and len(arcs[0].points) >= 20


def test_polyline_bulges_become_arcs_not_chords(tmp_path):
    """A bulge is how LWPOLYLINE stores a rounded corner; ignoring it loses area."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    # A half-circle of radius 500 expressed as a single bulged segment.
    doc.modelspace().add_lwpolyline(
        [(0, 0, 0, 0, 1.0), (1000, 0, 0, 0, 0)], format="xyseb"
    )
    path = tmp_path / "bulge.dxf"
    doc.saveas(str(path))

    prims = load_dxf(str(path)).primitives
    assert prims
    points = prims[0].points
    assert len(points) > 10, "a bulge flattened to a chord has two points"
    peak = max(abs(p[1]) for p in points)
    assert peak == pytest.approx(500.0, rel=0.02), "the arc must bulge to its radius"


# ── robustness ───────────────────────────────────────────────────────────────


def test_geometry_lands_where_the_drawing_says(drawing):
    """The fence rectangle is the drawing extent; nothing should escape it."""
    box = drawing.bbox
    assert box is not None
    assert box.x0 == pytest.approx(0.0, abs=1.0)
    assert box.y0 == pytest.approx(0.0, abs=1.0)
    assert box.x1 == pytest.approx(12000.0, abs=1.0)
    assert box.y1 == pytest.approx(6000.0, abs=1.0)


def test_entity_counts_and_unsupported_types_are_both_reported(drawing):
    """§30: what the adapter could not read must be visible, not silent."""
    counts = drawing.info.entity_counts
    assert counts.get("INSERT", 0) >= 2
    assert counts.get("LWPOLYLINE", 0) >= 3
    assert counts.get("CIRCLE", 0) >= 1
    assert isinstance(drawing.info.unsupported_counts, dict)
    assert drawing.info.unsupported_counts == {}, (
        f"nothing in this fixture should be unreadable: "
        f"{drawing.info.unsupported_counts}"
    )


def test_a_dwg_is_refused_with_an_actionable_message(tmp_path):
    """DWG needs converting first, and the error must say so."""
    fake = tmp_path / "drawing.dwg"
    fake.write_bytes(b"AC1015" + b"\x00" * 512)
    with pytest.raises(DxfReadError) as error:
        load_dxf(str(fake))
    message = str(error.value)
    assert "DWG" in message and "AC1015" in message
    assert "not a DXF" in message
    assert "ODA File Converter" in message or "Save As" in message


def test_summary_is_json_shaped_for_the_api(drawing):
    payload = drawing.summary()
    for key in ("file_name", "dxf_version", "units", "extents", "layers", "blocks",
                "linetypes", "xrefs", "entity_counts", "geometry"):
        assert key in payload, f"missing {key}"
    assert payload["units"]["declared"] is True
    assert payload["geometry"]["primitive_count"] == len(drawing.primitives)
    assert "EQUIPMENT" in payload["geometry"]["layers_with_geometry"]
    assert "MOTOR" in payload["geometry"]["blocks_with_geometry"]
