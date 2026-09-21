"""CAD text is not UTF-8, and the application must not assume it is.

The 102 production DWG failed at ~3 % with ``'utf-8' codec can't decode byte 0xc3``.
The drawing declares ``$DWGCODEPAGE = ANSI_936`` — GBK, Simplified Chinese — and on
the Linux image the converter echoed raw GBK bytes on stderr. ``subprocess.run(...,
text=True)`` decoded that stream as UTF-8 and raised inside ``run()``, failing the
whole analysis over a diagnostic message, and the error was then reported as "the
drawing could not be read" — sending the operator to fix a drawing that was fine.

Everything here is synthetic: a stand-in converter that emits the same byte pattern,
and a DXF written with a GBK layer name. No proprietary data.

Two rules are pinned:

* converter output is captured as bytes and only ever decoded for display, safely;
* the DXF itself is never decoded by this application — ezdxf reads it and honours
  its declared codepage, so Chinese names arrive intact rather than stripped.
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap

import pytest

ezdxf = pytest.importorskip("ezdxf")

from backend.cad import dwg as dwg_module
from backend.cad.dxf import load_dxf
from backend.runtime import CONVERTER_ENV

#: GBK for "中文", and the exact 0xC3 lead-byte pattern the 102 DWG produced.
GBK_CHINESE = b"\xd6\xd0\xce\xc4"
BAD_UTF8 = b"\xc3\xfb"
LAYER = "中文层"          # 中文层 — a layer name that must survive intact


def _gbk_dxf(path) -> str:
    """An R2000 DXF declaring ANSI_936, with a Chinese layer and some geometry."""
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    doc.layers.add(LAYER, color=3)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 600), (0, 600)],
                       close=True, dxfattribs={"layer": LAYER})
    # ezdxf 1.4 writes $DWGCODEPAGE = ANSI_1252 whatever encoding it saves with, so
    # a file saved as cp936 declares the wrong codepage and reads back as mojibake —
    # correctly, per its own header. That inconsistency would make these tests
    # prove nothing. LibreDWG writes ANSI_936 over a GBK body, so the fixture does
    # exactly that: GBK bytes, then the header set to what they are.
    doc.saveas(str(path), encoding="cp936")
    raw = open(path, "rb").read()
    assert b"ANSI_1252" in raw, "ezdxf's header default changed; revisit this fixture"
    open(path, "wb").write(raw.replace(b"ANSI_1252", b"ANSI_936", 1))
    return str(path)


def _fake_converter(tmp_path, dxf_path: str, stderr: bytes, stdout: bytes = b"",
                    version_line: bytes = b"dwg2dxf 0.14\n") -> str:
    """A stand-in for dwg2dxf that writes a prepared DXF and chosen raw bytes."""
    script = tmp_path / "dwg2dxf"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import shutil, sys
        if "--version" in sys.argv:
            sys.stdout.buffer.write({version_line!r})
            sys.exit(0)
        out = sys.argv[sys.argv.index("-o") + 1]
        shutil.copyfile({dxf_path!r}, out)
        sys.stdout.buffer.write({stdout!r})
        sys.stderr.buffer.write({stderr!r})
        sys.exit(0)
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _fake_dwg(tmp_path) -> str:
    """Enough of a DWG to pass the signature check; the converter ignores it."""
    path = tmp_path / "gbk-drawing.dwg"
    path.write_bytes(b"AC1015" + b"\x00" * 512)
    return str(path)


@pytest.fixture
def gbk_setup(tmp_path, monkeypatch):
    dxf = _gbk_dxf(tmp_path / "gbk.dxf")
    return tmp_path, dxf


# ── the converter's output ───────────────────────────────────────────────────


def test_the_generated_dxf_really_is_gbk_and_says_so(gbk_setup):
    """Guards the fixture. It must be GBK *and* declare ANSI_936, as LibreDWG's
    output does — otherwise the tests below would pass for the wrong reason."""
    import re

    _tmp, dxf = gbk_setup
    raw = open(dxf, "rb").read()
    assert GBK_CHINESE in raw, "the layer name was not written as GBK"
    header = re.search(rb"\$DWGCODEPAGE\s*\n\s*3\s*\n([^\n]*)", raw)
    assert header and header.group(1).strip() == b"ANSI_936"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


def test_non_utf8_converter_stderr_no_longer_fails_the_conversion(
    gbk_setup, monkeypatch
):
    """The regression itself: exactly the byte pattern the 102 DWG produced."""
    tmp_path, dxf = gbk_setup
    stderr = (b"Warning: codepage 936 text layer " + GBK_CHINESE + b"\n"
              b"Warning: invalid string " + BAD_UTF8 + b" in table\n"
              b"some other diagnostic line\n")
    monkeypatch.setenv(CONVERTER_ENV, _fake_converter(tmp_path, dxf, stderr))
    dwg_module._VERSION_CACHE.clear()

    drawing, conversion = dwg_module.load_dwg(_fake_dwg(tmp_path))

    assert drawing.primitives, "the geometry was read"
    # Converter warnings survive, with the undecodable bytes shown, not erased.
    joined = " ".join(conversion.warnings)
    assert "\\xc3\\xfb" in joined, f"the raw bytes should be visible: {joined!r}"
    assert "\\xd6\\xd0" in joined
    assert "�" not in joined, "bytes must not be silently replaced"


def test_non_utf8_converter_stdout_is_harmless_too(gbk_setup, monkeypatch):
    tmp_path, dxf = gbk_setup
    monkeypatch.setenv(CONVERTER_ENV, _fake_converter(
        tmp_path, dxf, stderr=b"", stdout=b"progress " + BAD_UTF8 + GBK_CHINESE + b"\n"))
    dwg_module._VERSION_CACHE.clear()
    drawing, _conversion = dwg_module.load_dwg(_fake_dwg(tmp_path))
    assert drawing.primitives


def test_a_non_utf8_version_line_does_not_break_converter_discovery(
    gbk_setup, monkeypatch
):
    """The --version probe used text=True as well; /health depends on it."""
    tmp_path, dxf = gbk_setup
    converter = _fake_converter(tmp_path, dxf, stderr=b"",
                                version_line=b"dwg2dxf 0.14 " + BAD_UTF8 + b"\n")
    monkeypatch.setenv(CONVERTER_ENV, converter)
    dwg_module._VERSION_CACHE.clear()
    found = dwg_module.find_converter()
    assert found is not None
    assert found.version == "0.14"


@pytest.mark.parametrize("raw", [
    b"", b"plain ascii", BAD_UTF8, GBK_CHINESE, bytes(range(256)),
    b"\xff\xfe\xfd", "中文".encode("utf-8"),
])
def test_decoding_converter_output_never_raises(raw):
    text = dwg_module._decode_output(raw)
    assert isinstance(text, str)
    # Valid UTF-8 comes through untouched; everything else is shown, not lost.
    try:
        assert text == raw.decode("utf-8")
    except UnicodeDecodeError:
        assert "\\x" in text


# ── the DXF itself: decoded by ezdxf, by its declared codepage ───────────────


def test_a_gbk_layer_name_arrives_intact(gbk_setup):
    """Not stripped, not replaced, not mojibake: the declared codepage is honoured.
    This application never decodes the DXF itself."""
    _tmp, dxf = gbk_setup
    drawing = load_dxf(dxf)
    names = [layer.name for layer in drawing.info.layers]
    assert LAYER in names, names
    assert all("�" not in name for name in names), "a character was replaced"


def test_the_whole_dwg_path_keeps_the_chinese_layer_name(gbk_setup, monkeypatch):
    tmp_path, dxf = gbk_setup
    monkeypatch.setenv(CONVERTER_ENV, _fake_converter(
        tmp_path, dxf, stderr=b"Warning: " + GBK_CHINESE + BAD_UTF8 + b"\n"))
    dwg_module._VERSION_CACHE.clear()
    drawing, _conversion = dwg_module.load_dwg(_fake_dwg(tmp_path))
    assert LAYER in [layer.name for layer in drawing.info.layers]
    assert drawing.info.units_name == "millimeters"


# ── the API end to end, and how a failure of this kind is reported ───────────


def test_the_api_measures_a_dwg_whose_converter_speaks_gbk(gbk_setup, monkeypatch):
    """The hosted failure, through the same path a browser uses."""
    import time

    from fastapi.testclient import TestClient

    from backend.main import app

    tmp_path, dxf = gbk_setup
    monkeypatch.setenv(CONVERTER_ENV, _fake_converter(
        tmp_path, dxf,
        stderr=b"Warning: " + BAD_UTF8 + b" codepage text\n" * 40))
    dwg_module._VERSION_CACHE.clear()

    with TestClient(app) as client:
        with open(_fake_dwg(tmp_path), "rb") as handle:
            blob = handle.read()
        job = client.post(
            "/api/analyse",
            files={"file": ("gbk.dwg", blob, "application/octet-stream")},
        ).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = client.get(f"/api/jobs/{job['job_id']}").json()
            if snapshot["state"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert snapshot["state"] == "done", snapshot.get("error")
    area = snapshot["result"]["area"]
    union = next(f for f in area["footprint_interpretations"]
                 if f["type"] == "geometry_union")
    assert union["area_mm2"] == pytest.approx(1000 * 600, rel=0.01)


def test_the_original_failure_is_reported_at_its_exact_line(gbk_setup, monkeypatch):
    """Reproduces the 102 failure precisely — the strict UTF-8 decode that
    text=True performed, inside _run_libredwg — and checks the report now names
    the function and line instead of blaming the drawing.

    It was reported as "the drawing could not be read", which sent the operator
    to re-export a drawing that was fine.
    """
    from backend.jobs import _describe

    tmp_path, dxf = gbk_setup
    monkeypatch.setenv(CONVERTER_ENV, _fake_converter(
        tmp_path, dxf, stderr=b"Warning: " + BAD_UTF8 + b" in table\n"))
    dwg_module._VERSION_CACHE.clear()
    # The old behaviour: decode converter output strictly as UTF-8.
    monkeypatch.setattr(dwg_module, "_decode_output", lambda raw: raw.decode("utf-8"))

    with pytest.raises(UnicodeDecodeError) as caught:
        dwg_module.load_dwg(_fake_dwg(tmp_path))
    described = _describe(caught.value)

    assert "0xc3" in str(caught.value), "the same byte as the production failure"
    assert described["kind"] == "internal_error", "not blamed on the drawing"
    assert "not at fault" in described["reason"]
    assert described["error_type"] == "UnicodeDecodeError"
    assert described["raised_at"].startswith("backend/cad/dwg.py:_run_libredwg:"), (
        described["raised_at"])


def test_every_failure_report_carries_its_type_and_location():
    from backend.jobs import _describe

    try:
        from backend.cad.dxf import load_dxf as _load

        _load("/nonexistent/definitely-not-here.dxf")
    except Exception as error:   # noqa: BLE001 — any failure will do
        described = _describe(error)
    assert described["error_type"]
    assert described.get("raised_at", "").startswith("backend/cad/"), described
