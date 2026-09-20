"""The DWG adapter: local conversion, and the provenance that survives it.

CONSTITUTION.md §35 (proprietary drawings stay on this machine), §31 (errors are
actionable) and §37 (source-adapter seam).

The production DWGs are gitignored, so the tests that need one skip unless the
real files are present. Everything that does *not* need a DWG — signature
parsing, provenance, converter discovery, the unavailable path, metadata checks,
the stated-unit scale — is covered unconditionally.
"""

from __future__ import annotations

import glob
import os

import pytest

from backend.cad.dwg import (
    DWG_VERSIONS,
    DwgConversion,
    DwgConversionFailed,
    DwgConversionUnavailable,
    converter_status,
    dwg_signature,
    find_converter,
    is_dwg,
    load_dwg,
)

#: The real drawings, when they happen to be on this machine.
PRODUCTION_DWGS = sorted(glob.glob("Inputs/*.dwg"))

#: Conversion of a 90 MB production DWG takes minutes, so the real-file
#: acceptance runs only when asked for.
ACCEPTANCE = os.environ.get("PROJECTED_AREA_ACCEPTANCE") == "1"

requires_converter = pytest.mark.skipif(
    find_converter() is None, reason="no local DWG converter installed"
)


# ── signatures ───────────────────────────────────────────────────────────────


def test_dwg_signature_is_read_from_the_bytes_not_the_name():
    """A drafter's file extension is not evidence; the header is."""
    assert dwg_signature(b"AC1015\x00\x00") == "AC1015"
    assert dwg_signature(b"AC1032abcd") == "AC1032"
    assert is_dwg(b"AC1015" + bytes(100))

    assert dwg_signature(b"%PDF-1.7") is None
    assert dwg_signature(b"ACXXXX") is None, "the version must be numeric"
    assert dwg_signature(b"AC10") is None, "a truncated header is not a signature"
    assert not is_dwg(b"0\nSECTION\n")


def test_known_releases_are_named():
    """The UI shows the release, not a four-digit code nobody remembers."""
    assert DWG_VERSIONS["AC1015"] == "AutoCAD 2000/2000i/2002"
    assert DWG_VERSIONS["AC1032"] == "AutoCAD 2018+"
    assert "AC1027" in DWG_VERSIONS


def test_a_file_without_a_dwg_signature_is_refused_before_conversion(tmp_path):
    """No point running a converter on something that is not a DWG."""
    impostor = tmp_path / "not-really.dwg"
    impostor.write_bytes(b"%PDF-1.7\n" + bytes(64))
    with pytest.raises(DwgConversionFailed) as error:
        load_dwg(str(impostor))
    message = str(error.value)
    assert "does not carry a DWG signature" in message
    assert "25504446" in message or "2550" in message, "name the bytes actually found"


# ── the converter, present or not ────────────────────────────────────────────


def test_converter_status_is_honest_either_way():
    status = converter_status()
    assert isinstance(status["available"], bool)
    if status["available"]:
        assert status["tool"] in ("libredwg", "oda_file_converter")
        assert status["path"] and os.path.exists(status["path"])
        assert status["version"]
    else:
        assert "setup_command" in status, "say exactly how to fix it"
        assert "install_dwg_support" in status["setup_command"]
        assert status["searched"], "say where it looked"


def test_the_unavailable_error_carries_the_setup_command():
    """§31: 'install something' is not an actionable message."""
    error = DwgConversionUnavailable()
    assert "local CAD conversion component" in str(error)
    assert error.setup_command == ".venv/bin/python -m tools.install_dwg_support"
    assert error.component


def test_conversion_record_keeps_the_source_identity():
    """Converting to DXF internally must not rewrite what the source *was*."""
    conversion = DwgConversion(
        source_name="line.dwg",
        source_sha256="a" * 64,
        source_bytes=1234,
        dwg_signature="AC1015",
        dwg_version="AutoCAD 2000/2000i/2002",
        tool="libredwg",
        tool_version="0.14",
        tool_path="/somewhere/dwg2dxf",
        dxf_sha256="b" * 64,
        dxf_bytes=9999,
        duration_seconds=1.234,
        warnings=["Warning: something"],
    )
    payload = conversion.as_dict()

    assert payload["source_type"] == "dwg", "the source is a DWG, not a DXF"
    assert payload["source_name"] == "line.dwg"
    assert payload["source_sha256"] == "a" * 64
    assert payload["dwg_signature"] == "AC1015"
    assert payload["dwg_version"].startswith("AutoCAD 2000")
    assert payload["tool"] == "libredwg" and payload["tool_version"] == "0.14"
    assert payload["intermediate_dxf_sha256"] == "b" * 64
    assert payload["intermediate_dxf_bytes"] == 9999
    assert payload["duration_seconds"] == 1.23
    assert payload["warnings"] == ["Warning: something"]
    assert payload["path"] == "DWG → local DXF → CAD geometry"


# ── metadata checks ──────────────────────────────────────────────────────────


def test_missing_metadata_is_surfaced_rather_than_passed_over(tmp_path):
    """Conversion that strips CAD meaning must not go unmentioned."""
    ezdxf = pytest.importorskip("ezdxf")
    from backend.cad.dwg import _check_metadata
    from backend.cad.dxf import load_dxf

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0          # undeclared units
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
    path = tmp_path / "thin.dxf"
    doc.saveas(str(path))

    warnings = _check_metadata(load_dxf(str(path)))
    joined = " ".join(warnings)
    assert "does not declare its units" in joined
    assert "property of the drawing, not of the conversion" in joined, (
        "do not blame the converter for what the drawing never said"
    )


def test_a_drawing_with_no_geometry_is_called_out(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from backend.cad.dwg import _check_metadata
    from backend.cad.dxf import load_dxf

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    path = tmp_path / "empty.dxf"
    doc.saveas(str(path))

    warnings = " ".join(_check_metadata(load_dxf(str(path))))
    assert "no geometry" in warnings


# ── a CAD drawing that does not declare its units ────────────────────────────


def test_an_operator_can_state_the_unit_a_drawing_omits(tmp_path):
    """The production DWGs leave $INSUNITS at 0 but still state dimensions.

    Asking which unit those dimensions are in is a far better question than
    asking someone to pick two points on a picture, and the answer is marked as
    the operator's, not the drawing's.
    """
    ezdxf = pytest.importorskip("ezdxf")
    from backend.cad.dxf import dimension_evidence, load_dxf, scale_from_stated_unit

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (75000, 0), (75000, 13200), (0, 13200)], close=True)
    msp.add_linear_dim(base=(0, -900), p1=(0, 0), p2=(75000, 0)).render()
    path = tmp_path / "unitless.dxf"
    doc.saveas(str(path))

    drawing = load_dxf(str(path))
    assert drawing.info.units_declared is False

    evidence = dimension_evidence(drawing)
    assert evidence["units_declared"] is False
    assert evidence["insunits"] == 0
    assert evidence["largest"] == pytest.approx(75000.0, rel=1e-3), (
        "the drawing's own dimension is the evidence an engineer recognises"
    )
    assert evidence["extent_units"]

    scale = scale_from_stated_unit(drawing.info, "mm")
    assert scale.verified
    assert scale.mm_per_unit == 1.0
    assert scale.operator_supplied is True, "the unit was stated, not measured"
    assert "$INSUNITS = 0" in " ".join(scale.evidence)
    assert "only the unit was supplied" in " ".join(scale.evidence)

    metres = scale_from_stated_unit(drawing.info, "m")
    assert metres.mm_per_unit == 1000.0

    with pytest.raises(ValueError):
        scale_from_stated_unit(drawing.info, "furlongs")


def test_a_declared_unit_is_not_marked_as_operator_supplied(tmp_path):
    """When the file says it, the file gets the credit."""
    ezdxf = pytest.importorskip("ezdxf")
    from backend.cad.dxf import load_dxf, scale_from_cad_units

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
    path = tmp_path / "declared.dxf"
    doc.saveas(str(path))

    scale = scale_from_cad_units(load_dxf(str(path)).info)
    assert scale.verified and scale.mm_per_unit == 1.0
    assert scale.operator_supplied is False


# ── real production files ────────────────────────────────────────────────────


@pytest.mark.skipif(not PRODUCTION_DWGS, reason="no production DWG on this machine")
@pytest.mark.skipif(not ACCEPTANCE, reason="set PROJECTED_AREA_ACCEPTANCE=1 to run")
@requires_converter
@pytest.mark.parametrize("path", PRODUCTION_DWGS)
def test_production_dwg_converts_and_yields_cad_semantics(path):
    """The acceptance set: every real DWG must reach measurable geometry."""
    drawing, conversion = load_dwg(path)

    assert conversion.source_type == "dwg"
    assert conversion.dwg_signature.startswith("AC")
    assert conversion.source_sha256 and conversion.dxf_sha256
    assert conversion.duration_seconds > 0

    assert drawing.primitives, "conversion produced no measurable geometry"
    assert [layer for layer in drawing.info.layers if layer.entity_count], (
        "no layer carries geometry — the CAD advantage did not survive"
    )
    assert drawing.bbox is not None
