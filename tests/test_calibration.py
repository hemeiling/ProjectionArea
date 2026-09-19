"""Scale recovery, unit handling and the refusal to fabricate.

CONSTITUTION.md §3 and §4.
"""

from __future__ import annotations

import math

import pytest

from backend.calibration.scale import (
    _Measurement,
    nearest_common_ratio,
    scale_from_ratio,
    scale_from_two_points,
    vote_for_scale,
)
from backend.config import MM_PER_PDF_UNIT_AT_1_1
from backend.models import ScaleSource
from backend.pdf.text import detect_scale_ratio, detect_sheet_unit, parse_dimension_texts
from backend.models import BBox, TextItem
from backend.units import Area, to_mm


def measurement(value_mm, units, text):
    return _Measurement(
        value_mm=value_mm, units=units, mm_per_unit=value_mm / units,
        text=text, kind="linear", at=(0.0, 0.0),
    )


def test_two_point_calibration():
    scale = scale_from_two_points((0.0, 0.0), (1204.73, 0.0), 425.0)
    assert scale.verified
    assert scale.mm_per_unit == pytest.approx(425.0 / 1204.73)
    assert scale.source is ScaleSource.USER_TWO_POINT
    assert scale.confidence > 0.9


def test_two_point_calibration_penalises_a_short_pick():
    long_pick = scale_from_two_points((0.0, 0.0), (400.0, 0.0), 100.0)
    short_pick = scale_from_two_points((0.0, 0.0), (12.0, 0.0), 3.0)
    assert short_pick.confidence < long_pick.confidence


def test_two_point_calibration_rejects_coincident_points():
    with pytest.raises(ValueError):
        scale_from_two_points((10.0, 10.0), (10.0, 10.0), 50.0)


def test_ratio_scale_is_reported_as_an_assumption():
    scale = scale_from_ratio(100.0)
    assert scale.mm_per_unit == pytest.approx(MM_PER_PDF_UNIT_AT_1_1 * 100.0)
    assert scale.source is ScaleSource.DRAWING_RATIO
    # Never presented as strong evidence: a fit-to-page print invalidates it.
    assert scale.confidence <= 0.55
    assert "fit-to-page" in scale.detail or "true sheet size" in scale.detail


def test_vote_finds_the_scale_the_majority_of_dimensions_agree_on():
    truth = 0.70556
    measurements = [
        measurement(200.0, 200.0 / truth, "200"),
        measurement(120.0, 120.0 / truth, "120"),
        measurement(45.0, 45.0 / truth, "45"),
        # Three mismatches, each latching onto unrelated linework.
        measurement(200.0, 137.0, "200"),
        measurement(120.0, 61.0, "120"),
        measurement(45.0, 999.0, "45"),
    ]
    candidate = vote_for_scale(measurements)
    assert candidate is not None
    assert candidate.mm_per_unit == pytest.approx(truth, rel=1e-4)
    assert candidate.source is ScaleSource.DIMENSION_CONSENSUS
    assert candidate.support == 3


def test_vote_reports_weak_support_as_a_single_dimension():
    candidate = vote_for_scale([measurement(200.0, 283.5, "200")])
    assert candidate is not None
    assert candidate.source is ScaleSource.SINGLE_DIMENSION


def test_vote_returns_nothing_without_measurements():
    assert vote_for_scale([]) is None


def test_vote_weights_longer_spans_more_heavily():
    """A long dimension pins the scale harder than a short, noisier one."""
    candidate = vote_for_scale([
        measurement(6000.0, 6000.0 / 35.2778, "6000"),
        measurement(30.0, 30.0 / 35.6, "30"),
    ])
    assert candidate is not None
    assert candidate.mm_per_unit == pytest.approx(35.2778, rel=5e-3)


def test_nearest_common_ratio():
    denominator, error = nearest_common_ratio(MM_PER_PDF_UNIT_AT_1_1 * 2.0)
    assert denominator == 2.0
    assert error < 1e-6


def test_dimension_token_parsing():
    def item(text):
        return TextItem(text, BBox(0, 0, 10, 10), 9.0)

    parsed = {d.raw: (d.value, d.kind, d.count) for d in parse_dimension_texts(
        [item(t) for t in ["425", "Ø18", "R25", "4-Ø12", "(425)", "M8x1.25", "6000 mm"]]
    )}
    assert parsed["425"] == (425.0, "linear", 1)
    assert parsed["Ø18"] == (18.0, "diameter", 1)
    assert parsed["R25"] == (25.0, "radius", 1)
    assert parsed["4-Ø12"] == (12.0, "diameter", 4)
    assert parsed["M8x1.25"][1] == "thread"


def test_metadata_numbers_are_not_treated_as_dimensions():
    def item(text):
        return TextItem(text, BBox(0, 0, 10, 10), 9.0)

    for text in ["REV 3", "SHEET 2 OF 5", "2026-08", "SCALE 1:2", "DWG NO 104"]:
        assert parse_dimension_texts([item(text)]) == [], text


def test_printed_scale_detection():
    def item(text):
        return TextItem(text, BBox(0, 0, 10, 10), 9.0)

    assert detect_scale_ratio([item("SCALE 1:2")])["denominator"] == 2.0
    assert detect_scale_ratio([item("比例 1:100")])["denominator"] == 100.0
    assert detect_scale_ratio([item("SCALE 2:1")])["denominator"] == 0.5
    assert detect_scale_ratio([item("no scale here")]) is None


def test_sheet_unit_detection():
    assert detect_sheet_unit("ALL DIMENSIONS IN MM UNLESS OTHERWISE SPECIFIED") == "mm"
    assert detect_sheet_unit("单位：mm") == "mm"
    assert detect_sheet_unit("nothing declared") is None


def test_unit_conversions_round_trip():
    assert to_mm(425.0, "mm") == 425.0
    assert to_mm(1.0, "in") == pytest.approx(25.4)
    area = Area(12548.39281731)
    assert area.display("cm2") == 125.48
    assert area.display("in2") == 19.45
    assert area.to("m2") == pytest.approx(0.01254839281731)
