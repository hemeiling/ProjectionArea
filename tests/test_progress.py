"""The progress interface: honest arithmetic, and no influence on the result.

CONSTITUTION.md §27 and §37. A progress bar is presentation, so two things have
to be true of it. It must never report a number it has not been told — no
invented denominators, no fraction that slides backwards — and observing a
measurement must not change it. Both are cheap to state and easy to break, so
they are tested directly rather than only through the browser.
"""

from __future__ import annotations

import fitz
import pytest

from backend.area.projected import compute_projected_area
from backend.models import ViewSource
from backend.pipeline import prepare_page, region_scale
from backend.progress import (
    DWG_STAGES,
    DXF_STAGES,
    NULL_PROGRESS,
    PDF_STAGES,
    STAGE_PLANS,
    ProgressTracker,
    Stage,
    tracker_for,
)


# ── the stage plans ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["pdf", "dxf", "dwg"])
def test_every_plan_has_distinct_stages_and_positive_weights(kind):
    stages = STAGE_PLANS[kind]
    keys = [s.key for s in stages]
    assert len(keys) == len(set(keys)), f"duplicate stage in the {kind} plan: {keys}"
    assert all(s.weight > 0 for s in stages), "a zero-weight stage can never advance"


def test_a_dxf_plan_omits_conversion_rather_than_ticking_it():
    """A DXF is read directly; claiming a conversion step is a small lie."""
    assert "converted" in [s.key for s in DWG_STAGES]
    assert "converted" not in [s.key for s in DXF_STAGES]
    assert "converted" not in [s.key for s in PDF_STAGES]


def test_reading_the_drawing_is_the_dominant_weight():
    """Measured cost, not 100 divided by the number of steps."""
    for stages in (DWG_STAGES, DXF_STAGES, PDF_STAGES):
        weights = {s.key: s.weight for s in stages}
        assert weights["geometry"] == max(weights.values()), (
            f"geometry should dominate: {weights}"
        )
        assert weights["geometry"] > 100.0 / len(stages), "an equal split would be arbitrary"


# ── the tracker's arithmetic ─────────────────────────────────────────────────


def test_the_fraction_follows_the_measured_weights():
    tracker = ProgressTracker(stages=(Stage("a", 10.0), Stage("b", 90.0)))
    tracker.begin("a")
    assert tracker.fraction == 0.0
    tracker.finish("a")
    assert tracker.fraction == pytest.approx(0.10), "the light stage is worth 10 %"
    tracker.begin("b")
    tracker.advance(1, 2)
    assert tracker.fraction == pytest.approx(0.55), "half of the heavy stage"
    tracker.finish("b")
    assert tracker.fraction == pytest.approx(1.0)


def test_the_fraction_never_decreases():
    """Out-of-order or re-entered stages must not rewind the bar."""
    tracker = tracker_for("pdf")
    seen = []
    for key in ("loaded", "geometry", "regions", "scale"):
        tracker.begin(key)
        seen.append(tracker.fraction)
        tracker.finish(key)
        seen.append(tracker.fraction)
    # A late report for an earlier stage, which is exactly what a retry looks like.
    tracker.begin("loaded")
    seen.append(tracker.fraction)
    tracker.advance(1, 1000)
    seen.append(tracker.fraction)
    assert all(b >= a for a, b in zip(seen, seen[1:])), seen


def test_an_unknown_total_is_reported_as_indeterminate():
    """No denominator means an animated section, not an invented one."""
    tracker = tracker_for("dxf")
    tracker.begin("geometry")
    tracker.advance(4321)
    snap = tracker.snapshot()
    assert snap["current"] == 4321
    assert snap["total"] is None
    assert snap["indeterminate"] is True


def test_a_stage_with_nothing_to_count_is_indeterminate_over_its_own_slice():
    """Polygonising a silhouette is one operation, not n of m. The bar animates
    the region that stage covers instead of freezing at its start."""
    tracker = tracker_for("pdf")
    tracker.begin("candidates")
    snap = tracker.snapshot()
    assert snap["indeterminate"] is True
    assert snap["current"] is None and snap["total"] is None
    # The span is the stage's measured share, so the band marks real uncertainty.
    weights = {stage.key: stage.weight for stage in PDF_STAGES}
    total = sum(weights.values())
    expected = (weights["loaded"] + weights["geometry"] + weights["regions"]
                + weights["scale"]) / total
    assert snap["stage_start"] == pytest.approx(expected, abs=1e-4)
    assert snap["stage_end"] == pytest.approx(
        expected + weights["candidates"] / total, abs=1e-4)


def test_a_stage_reporting_a_real_ratio_is_not_indeterminate():
    tracker = tracker_for("pdf")
    tracker.begin("geometry")
    tracker.advance(50_000, 163_691)
    snap = tracker.snapshot()
    assert snap["indeterminate"] is False
    assert snap["total"] == 163_691
    assert snap["stage_start"] <= snap["progress"] <= snap["stage_end"]


def test_a_count_that_overruns_its_estimate_becomes_indeterminate():
    """The DXF reader's floor is modelspace; block contents are walked on top of
    it, so "17 / 9" is not a ratio worth showing anyone."""
    tracker = tracker_for("dxf")
    tracker.begin("geometry")
    tracker.advance(17, 9)
    snap = tracker.snapshot()
    assert snap["total"] is None, "a broken ratio is withheld, not displayed"
    assert snap["indeterminate"] is True
    assert snap["current"] == 17
    assert 0.0 <= snap["progress"] <= 1.0


def test_beginning_a_stage_implicitly_completes_the_earlier_ones():
    tracker = tracker_for("pdf")
    tracker.begin("loaded")
    tracker.begin("geometry")
    tracker.begin("regions")
    keys = {r["stage"]: r["done"] for r in tracker.snapshot()["stages"]}
    assert keys["loaded"] is True and keys["geometry"] is True
    assert keys["regions"] is False, "the current stage is not finished yet"


def test_the_snapshot_carries_the_plan_so_the_client_need_not_guess():
    snap = tracker_for("dwg").snapshot()
    assert snap["plan"] == [s.key for s in DWG_STAGES]
    assert snap["total_stages"] == len(DWG_STAGES)
    assert snap["completed_stages"] == 0
    assert snap["progress"] == 0.0


def test_observers_are_notified_on_every_event():
    seen = []
    tracker = tracker_for("pdf", on_change=lambda tr: seen.append(tr.fraction))
    tracker.begin("loaded")
    tracker.finish("loaded")
    tracker.begin("geometry")
    tracker.advance(500, 1000)
    assert len(seen) == 4
    assert seen == sorted(seen), seen


def test_counts_reported_outside_any_stage_do_not_move_the_bar():
    """Stage boundaries belong to the caller. Until one is opened there is no
    slice to interpolate inside, so a stray count is recorded and nothing more —
    a bar that jumped on an unattributed count would be inventing the number."""
    tracker = tracker_for("pdf")
    tracker.advance(500, 1000)
    snap = tracker.snapshot()
    assert snap["progress"] == 0.0
    assert snap["current"] == 500 and snap["total"] == 1000
    assert snap["stage"] == ""


def test_the_null_progress_accepts_everything_and_does_nothing():
    """So no engine module needs a conditional around a progress call."""
    assert NULL_PROGRESS.begin("geometry") is None
    assert NULL_PROGRESS.advance(1, None, "detail") is None
    assert NULL_PROGRESS.finish("geometry") is None


# ── the measurement is unaffected ────────────────────────────────────────────


def _measure(path, progress):
    """The whole PDF pipeline, with whatever observer is passed.

    Stage boundaries belong to the caller — the API route, here this helper — so
    that no domain module has to know the names in a progress plan. The engine
    only ever reports counts inside whichever stage it was handed.
    """
    doc = fitz.open(path)
    progress.begin("geometry")
    prepared = prepare_page(doc, 1, progress)
    progress.finish("geometry")
    region = prepared.default_region()
    scale, warnings = region_scale(prepared, region)
    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=doc.load_page(0),
        document_id="test",
        file_name="test.pdf",
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.WHOLE_PAGE,
        view_label="Whole page",
        extra_warnings=warnings,
    )
    doc.close()
    return prepared, result


@pytest.mark.parametrize("name", ["plate_with_holes", "layout_1_100", "obround_with_slot"])
def test_being_watched_does_not_change_a_single_number(drawings, name):
    """The point of the neutral interface: geometry does not know it is observed."""
    path = drawings[name]["path"]
    quiet_prepared, quiet = _measure(path, NULL_PROGRESS)

    tracker = tracker_for("pdf")
    watched_prepared, watched = _measure(path, tracker)

    assert tracker.fraction > 0.0, "the tracker was in fact used"

    quiet_readings = quiet.footprint_interpretations
    watched_readings = watched.footprint_interpretations
    assert [r.type for r in quiet_readings] == [r.type for r in watched_readings]
    for reading_a, reading_b in zip(quiet_readings, watched_readings):
        assert reading_a.area_mm2 == reading_b.area_mm2, reading_a.type
        assert reading_a.confidence == reading_b.confidence, reading_a.type
    assert quiet.as_dict() == watched.as_dict(), "the whole result must be identical"
    assert quiet_prepared.role_counts == watched_prepared.role_counts
    assert len(quiet_prepared.analysis.primitives) == len(watched_prepared.analysis.primitives)
