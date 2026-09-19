"""The real-drawing validation harness.

CONSTITUTION.md §26/§31: the harness that will judge the real sheets has to be
trustworthy itself. In particular it must report a file it cannot open as
*blocked*, with an actionable reason, and never as a measurement of zero.
"""

from __future__ import annotations

import json
import os

import pytest

from tools.validate_drawings import (
    build_report,
    collect_pdfs,
    comparison_table,
    profile_bbox,
    readable_pdf,
    validate_file,
)


def _validate(path, tmp_path, **kwargs):
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    return validate_file(
        str(path),
        str(out_dir),
        kwargs.pop("dpi", 72),
        kwargs.pop("page", 1),
        kwargs.pop("all_pages", False),
        kwargs.pop("whole_page", False),
    )


# ── blocked inputs ───────────────────────────────────────────────────────────


def test_encrypted_at_rest_file_is_blocked_with_an_actionable_reason(tmp_path):
    """The four production drawings are whole-file encrypted, not damaged PDFs.

    Their first bytes are not ``%PDF`` and the string appears nowhere in them.
    The harness must say what is wrong and what to do, name no area, and not
    pretend a repair is possible.
    """
    wrapped = tmp_path / "encrypted.pdf"
    wrapped.write_bytes(bytes.fromhex("7bff7595ff4d6046") + os.urandom(2048))

    reason = readable_pdf(str(wrapped))
    assert reason is not None
    assert "25504446" in reason, "the reason must show what a PDF header looks like"
    assert "export" in reason.lower() or "save as" in reason.lower()

    rows = _validate(wrapped, tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "blocked"
    assert row.area_mm2 is None
    assert row.confidence is None
    assert "%PDF" in row.detail or "25504446" in row.detail


def test_blocked_rows_never_report_a_zero_area(tmp_path):
    """§3: an unreadable file must not appear as a measured 0 mm²."""
    wrapped = tmp_path / "encrypted.pdf"
    wrapped.write_bytes(os.urandom(4096))
    table = comparison_table(_validate(wrapped, tmp_path))
    assert "BLOCKED" in table
    assert "0.0 mm²" not in table
    assert "0 %" not in table


def test_a_real_pdf_is_not_mistaken_for_an_encrypted_one(drawings):
    assert readable_pdf(drawings["plate_with_holes"]["path"]) is None


def test_missing_file_is_reported_not_raised(tmp_path):
    rows = _validate(tmp_path / "nope.pdf", tmp_path)
    assert rows[0].status == "blocked"
    assert rows[0].area_mm2 is None


# ── measured pages ───────────────────────────────────────────────────────────


def test_harness_measures_a_known_drawing_and_writes_its_evidence(drawings, tmp_path):
    """Every stage of the requested workflow appears in the record."""
    truth = drawings["plate_with_holes"]
    rows = _validate(truth["path"], tmp_path)
    assert len(rows) == 1
    row = rows[0]

    assert row.status == "measured"
    assert row.area_mm2 == pytest.approx(truth["net_area_mm2"], rel=2e-3)
    assert row.confidence is not None and row.confidence > 0.5

    # source -> geometry -> scale -> footprint -> union -> area -> confidence
    for stage in (
        "source",
        "geometry_extraction",
        "scale",
        "candidate_footprint",
        "union_polygon",
        "projected_area",
        "confidence",
    ):
        assert stage in row.stages, f"missing stage {stage!r}"

    assert row.stages["scale"]["verified"] is True
    assert row.stages["union_polygon"]["holes"] == truth["hole_count"]
    assert row.stages["geometry_extraction"]["role_counts"].get("sheet", 0) >= 1

    # The overlay is the auditability guarantee (§8) — it must exist and be real.
    assert row.overlay_path and os.path.exists(row.overlay_path)
    assert os.path.getsize(row.overlay_path) > 1000
    with open(row.json_path) as handle:
        payload = json.load(handle)
    assert payload["result"]["projected_area"]["verified"] is True


def test_bounding_area_and_utilization_are_consistent(drawings, tmp_path):
    """Utilization is area / bounding box, and a plate with holes is close to 1."""
    truth = drawings["plate_with_holes"]
    row = _validate(truth["path"], tmp_path)[0]

    assert row.bounding_area_mm2 == pytest.approx(truth["gross_area_mm2"], rel=5e-3)
    assert row.utilization == pytest.approx(row.area_mm2 / row.bounding_area_mm2, rel=1e-9)
    assert 0.0 < row.utilization <= 1.0
    # Three Ø20 holes in a 200 x 120 plate remove about 4 %.
    assert row.utilization == pytest.approx(0.961, abs=0.01)


def test_utilization_is_lower_for_a_shape_that_does_not_fill_its_envelope(
    drawings, tmp_path
):
    """The L-shaped floor plan must score well below the rectangular plate."""
    plan = _validate(drawings["layout_1_100"]["path"], tmp_path)[0]
    plate = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    assert plan.utilization < plate.utilization
    assert plan.utilization == pytest.approx(0.756, abs=0.02)


def test_a_scanned_page_is_refused_not_guessed(drawings, tmp_path):
    """§3: no text layer means no scale, which means no millimetres."""
    row = _validate(drawings["raster_plate"]["path"], tmp_path)[0]
    assert row.status == "refused"
    assert row.area_mm2 is None
    assert row.bounding_area_mm2 is None
    assert row.utilization is None
    assert "not verified" in row.scale_text.lower()
    assert row.overlay_path and os.path.exists(row.overlay_path)


def test_rotated_and_upright_sheets_agree_through_the_harness(drawings, tmp_path):
    """The harness is the thing that will judge real sheets — including rotated
    ones — so the rotation invariant is asserted at this level too."""
    upright = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    rotated = _validate(drawings["rotated_plate_90"]["path"], tmp_path)[0]
    assert rotated.area_mm2 == pytest.approx(upright.area_mm2, rel=1e-9)
    assert rotated.utilization == pytest.approx(upright.utilization, rel=1e-9)


# ── report shape ─────────────────────────────────────────────────────────────


def test_report_carries_every_requested_column_and_the_evidence(drawings, tmp_path):
    rows = _validate(drawings["plate_with_holes"]["path"], tmp_path)
    rows += _validate(drawings["raster_plate"]["path"], tmp_path)

    out_dir = tmp_path / "report"
    out_dir.mkdir()
    report = build_report(rows, str(out_dir))

    for column in (
        "Drawing",
        "Detected Scale/Units",
        "Projected Area",
        "Bounding Area",
        "Utilization",
        "Confidence",
        "Warnings",
    ):
        assert column in report, f"missing column {column!r}"

    for stage in ("geometry extraction", "scale", "candidate footprint", "union polygon"):
        assert stage in report
    assert os.path.exists(out_dir / "REPORT.md")


def test_collect_pdfs_expands_a_directory_and_ignores_other_files(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "b.PDF").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "c.dwg").write_bytes(b"AC1027")
    (tmp_path / "notes.txt").write_text("ignore me")

    found = [os.path.basename(p) for p in collect_pdfs([str(tmp_path)])]
    assert sorted(found) == ["a.pdf", "b.PDF"]
    assert "c.dwg" not in found, "DWG needs a CAD adapter, not the PDF pipeline"
