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

    # The seven milestone stages are each reported, and in order.
    stages = (
        "1 ingestion",
        "2 scale / units",
        "3 region detection",
        "4 classification",
        "5 area construction",
        "6 results",
        "7 visual QA",
    )
    for stage in stages:
        assert stage in report, f"missing stage {stage!r}"
    first_drawing = report[report.index("### "):]
    positions = [first_drawing.index(stage) for stage in stages]
    assert positions == sorted(positions), f"stages out of order: {positions}"
    assert os.path.exists(out_dir / "REPORT.md")


def test_collect_pdfs_expands_a_directory_and_ignores_other_files(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "b.PDF").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "c.dwg").write_bytes(b"AC1027")
    (tmp_path / "notes.txt").write_text("ignore me")

    found = [os.path.basename(p) for p in collect_pdfs([str(tmp_path)])]
    assert sorted(found) == ["a.pdf", "b.PDF"]
    assert "c.dwg" not in found, "DWG needs a CAD adapter, not the PDF pipeline"


# ── footprint interpretations ────────────────────────────────────────────────
#
# Exercises backend/area/interpretations.py through real results. On a line
# layout "projected area" has several defensible readings, and §30 forbids
# picking one silently — so all of them are computed and named.


def test_interpretations_are_ordered_union_then_hull_then_box(drawings, tmp_path):
    """For an L-shaped plan the three readings must differ, and in this order.

    Union ⊆ convex hull ⊆ bounding rectangle is a geometric fact; if the code
    ever reports otherwise, something is wrong with the hull or the bounds.
    """
    row = _validate(drawings["layout_1_100"]["path"], tmp_path)[0]
    by_key = {i["key"]: i for i in row.interpretations}

    assert set(by_key) >= {"equipment_union", "convex_hull", "bounding_rectangle"}
    union = by_key["equipment_union"]["area_units2"]
    hull = by_key["convex_hull"]["area_units2"]
    box = by_key["bounding_rectangle"]["area_units2"]
    assert union < hull < box, (union, hull, box)

    # The L-shape is the point: a sparse footprint must not be reported as its box.
    from backend.units import Area

    assert Area(by_key["equipment_union"]["area_mm2"]).to("m2") == pytest.approx(54.46, abs=0.05)
    assert Area(by_key["bounding_rectangle"]["area_mm2"]).to("m2") == pytest.approx(72.0, abs=0.05)

    # Every reading must say what physical region it is — that is the deliverable.
    for item in row.interpretations:
        assert item["means"].strip()
        assert item["basis"].strip()


def test_a_rectangular_part_fills_its_hull_and_its_box(drawings, tmp_path):
    """The sanity case: for a rectangle, hull and bounding box coincide."""
    row = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    by_key = {i["key"]: i for i in row.interpretations}
    assert by_key["convex_hull"]["area_units2"] == pytest.approx(
        by_key["bounding_rectangle"]["area_units2"], rel=1e-3
    )
    # Holes mean the union is strictly smaller than the envelope.
    assert by_key["equipment_union"]["area_units2"] < by_key["bounding_rectangle"]["area_units2"]


def test_no_geometry_means_no_interpretations(drawings, tmp_path):
    """§3: nothing reconstructed is not a region of area zero."""
    row = _validate(drawings["raster_plate"]["path"], tmp_path)[0]
    assert row.area_mm2 is None
    for item in row.interpretations:
        assert item["area_mm2"] is None, "an unverified scale cannot yield mm²"


def test_bounding_extent_is_reported_in_millimetres(drawings, tmp_path):
    """The 200 × 120 mm plate must report exactly that, not its page extent."""
    row = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    assert row.bounding_width_mm == pytest.approx(200.0, abs=1.0)
    assert row.bounding_height_mm == pytest.approx(120.0, abs=1.0)
    assert row.bounding_width_mm * row.bounding_height_mm == pytest.approx(
        row.bounding_area_mm2, rel=1e-6
    )


def test_bodies_are_listed_largest_first_with_their_own_extents(drawings, tmp_path):
    row = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    assert row.bodies
    areas = [b["area_units2"] for b in row.bodies]
    assert areas == sorted(areas, reverse=True)
    assert row.bodies[0]["hole_count"] == 3
    assert row.bodies[0]["width_units"] > 0 and row.bodies[0]["height_units"] > 0


# ── the title-block ambiguity, reported and not corrected ────────────────────


def test_title_block_ambiguity_is_flagged_when_a_dense_cluster_is_labelled_one(
    drawings, tmp_path
):
    """A /Rotate 180 sheet displays upside-down, so the part lands bottom-right.

    `_classify_region` then calls the part a title block. The harness must *say
    so* loudly rather than quietly measuring the wrong region — the classifier
    itself is deliberately left untouched until real drawings say how to fix it.
    """
    from tests.fixtures import build_rotated_plate

    upside_down = build_rotated_plate(
        str(tmp_path / "rot180.pdf"), drawings["plate_with_holes"]["path"], 180
    )
    row = _validate(upside_down["path"], tmp_path)[0]

    ambiguity = row.stages["candidate_footprint"]["title_block_ambiguity"]
    assert ambiguity is not None, "the known title-block confusion went unreported"
    assert "corner" in ambiguity["reason"] or "title block" in ambiguity["reason"]
    dense = [r for r in ambiguity["regions"] if r["primitives_inside"] >= 20]
    assert dense, "the part-sized cluster labelled a title block was not identified"
    assert "not corrected" in ambiguity["note"]

    # It must reach the summary table, where a reader will actually see it.
    from tools.validate_drawings import summary_table

    assert "title-block" in summary_table([row]) or "title block" in summary_table([row])


def test_an_ordinary_sheet_reports_no_ambiguity(drawings, tmp_path):
    """The flag must not cry wolf on a normal drawing."""
    row = _validate(drawings["plate_with_holes"]["path"], tmp_path)[0]
    assert row.stages["candidate_footprint"]["title_block_ambiguity"] is None


# ── the deliverable table ────────────────────────────────────────────────────


def test_summary_table_has_exactly_the_requested_columns(drawings, tmp_path):
    from tools.validate_drawings import summary_table

    rows = _validate(drawings["plate_with_holes"]["path"], tmp_path)
    table = summary_table(rows)
    for column in (
        "Drawing", "Source", "Selected View", "Scale/Units", "Projected Area m²",
        "Bounding Area m²", "Utilization", "Confidence", "Major Warning",
    ):
        assert column in table, f"missing column {column!r}"
    assert "PDF" in table, "the measurement path must be named"


def test_comparison_table_carries_native_units_m2_and_ft2(drawings, tmp_path):
    from tools.validate_drawings import comparison_table

    rows = _validate(drawings["plate_with_holes"]["path"], tmp_path)
    table = comparison_table(rows)
    assert "native units²" in table
    assert "m²" in table and "ft²" in table
    assert "Bounding W×H" in table
    # 23 057.52 mm² is 0.0231 m² and 0.25 ft²; both must appear, not just mm².
    assert "0.0231" in table
    assert "0.25" in table


def test_report_answers_what_physical_region_the_number_is(drawings, tmp_path):
    out_dir = tmp_path / "rep"
    out_dir.mkdir()
    rows = _validate(drawings["layout_1_100"]["path"], tmp_path)
    report = build_report(rows, str(out_dir))

    assert "What physical region is this?" in report
    assert "Union of equipment geometry" in report
    assert "Bounding rectangle" in report
    assert "Convex envelope" in report
    # The overlay legend is what makes stage 7 checkable.
    assert "Overlay legend" in report
    assert "included" in report and "excluded" in report
