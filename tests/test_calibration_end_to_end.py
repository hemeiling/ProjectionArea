"""Manual calibration, from the click to the stored result.

Reported as "Apply Calibration does nothing". It did work — in 7.7 seconds, with
no feedback, which is indistinguishable from a dead button. These pin both halves:
the arithmetic is exactly right, and the path that produces it is exercised end to
end rather than assumed.

The deterministic case throughout is the operator's own:

    drawing span      16.39 units
    known distance    16.39 mm
    expected scale    1.0 mm/unit

A span equal to the distance makes the scale exactly 1, which means
``area_mm2 == area_units2`` and every physical number can be checked by eye.
"""

from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

from backend.calibration.scale import scale_from_two_points
from backend.main import app
from backend.models import ScaleSource

SPAN = 16.39
KNOWN_MM = 16.39
EXPECTED_SCALE = 1.0


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_the_scale_arithmetic_is_exact():
    """16.39 mm across 16.39 units is one millimetre per unit, and nothing else."""
    scale = scale_from_two_points((100.0, 200.0), (100.0 + SPAN, 200.0), KNOWN_MM)
    assert scale.mm_per_unit == pytest.approx(EXPECTED_SCALE, abs=1e-12)
    assert scale.source is ScaleSource.USER_TWO_POINT
    assert scale.verified is True
    assert scale.operator_supplied is True
    assert scale.calibration is not None
    assert scale.calibration.known_length == KNOWN_MM
    assert scale.calibration.known_unit == "mm"
    assert scale.calibration.span_units == pytest.approx(SPAN, abs=1e-9)


def test_a_diagonal_span_uses_the_real_distance_not_an_axis():
    """The span is the hypotenuse; using dx alone would silently halve a scale."""
    leg = SPAN / math.sqrt(2)
    scale = scale_from_two_points((0.0, 0.0), (leg, leg), KNOWN_MM)
    assert scale.mm_per_unit == pytest.approx(EXPECTED_SCALE, rel=1e-9)


def _upload(client, drawings, name="plate_with_holes"):
    with open(drawings[name]["path"], "rb") as handle:
        blob = handle.read()
    response = client.post(
        "/api/documents", files={"file": (f"{name}.pdf", blob, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def _calibrate(client, document_id, span=SPAN, known=KNOWN_MM, unit="mm"):
    """Exactly what the browser sends when Apply Calibration is clicked."""
    payload = {
        "scale": {
            "mode": "two_point",
            "points": [[100.0, 200.0], [100.0 + span, 200.0]],
            "known_length": known,
            "known_unit": unit,
        },
    }
    response = client.post(
        f"/api/documents/{document_id}/pages/1/area", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_applying_a_calibration_replaces_the_scale(client, drawings):
    """On a scanned drawing, which carries no readable dimension — the situation
    the production PDFs are in, and the reason calibration exists."""
    document_id = _upload(client, drawings, "raster_plate")

    before = client.post(f"/api/documents/{document_id}/pages/1/area", json={}).json()
    assert before["scale"]["verified"] is False, "this drawing has no derivable scale"
    assert before["projected_area"]["verified"] is False

    after = _calibrate(client, document_id)
    scale = after["scale"]

    assert scale["source"] == "user_two_point_calibration"
    assert scale["verified"] is True
    assert scale["operator_supplied"] is True
    assert scale["mm_per_unit"] == pytest.approx(EXPECTED_SCALE, abs=1e-9)
    assert scale["calibration"]["known_length"] == KNOWN_MM
    assert scale["calibration"]["span_units"] == pytest.approx(SPAN, abs=1e-6)


def test_an_operator_calibration_overrides_a_scale_the_drawing_stated(
    client, drawings
):
    """A drafter who calibrates has a reason. Their number wins over the one the
    engine derived, and the result says whose it is."""
    document_id = _upload(client, drawings, "plate_with_holes")
    before = client.post(f"/api/documents/{document_id}/pages/1/area", json={}).json()
    assert before["scale"]["verified"] is True, "this fixture states its dimensions"
    assert before["scale"]["operator_supplied"] is not True

    after = _calibrate(client, document_id)
    assert after["scale"]["source"] == "user_two_point_calibration"
    assert after["scale"]["operator_supplied"] is True
    assert after["scale"]["mm_per_unit"] == pytest.approx(EXPECTED_SCALE, abs=1e-9)
    assert after["scale"]["mm_per_unit"] != before["scale"]["mm_per_unit"]


def test_a_physical_area_becomes_available_and_equals_the_drawing_area(
    client, drawings
):
    """At one millimetre per unit the two must agree, which makes a wrong scale
    factor — or a squared one — impossible to miss."""
    document_id = _upload(client, drawings)
    after = _calibrate(client, document_id)

    assert after["projected_area"]["verified"] is True
    area_mm2 = after["projected_area"]["net"]["mm2"]
    area_units2 = after["area_pdf_units2"]["net"]
    assert area_mm2 == pytest.approx(area_units2, rel=1e-9), (
        "area_mm2 must equal area_units2 when the scale is exactly 1 mm/unit"
    )

    for reading in after["footprint_interpretations"]:
        if reading["area_units2"] is not None and reading["area_mm2"] is not None:
            assert reading["area_mm2"] == pytest.approx(reading["area_units2"], rel=1e-9)


def test_calibration_changes_no_geometry(client, drawings):
    """It is a unit conversion. Every count, hole and component must survive it
    untouched — a calibration that moved a boundary would be a geometry bug."""
    document_id = _upload(client, drawings)
    before = client.post(f"/api/documents/{document_id}/pages/1/area", json={}).json()
    after = _calibrate(client, document_id)

    assert after["geometry"] == before["geometry"], "geometry changed under a rescale"
    for key in ("components", "holes", "outer_contours", "faces", "segments",
                "raw_primitives", "profile_primitives", "ignored_primitives"):
        assert after["geometry"][key] == before["geometry"][key], key
    assert len(after["components"]) == len(before["components"])
    assert [i["type"] for i in after["footprint_interpretations"]] == \
           [i["type"] for i in before["footprint_interpretations"]]
    # The drawing-unit areas are the same measurement; only their physical
    # expression changed.
    assert after["area_pdf_units2"] == before["area_pdf_units2"]


def test_the_warnings_change_to_say_the_scale_came_from_the_operator(
    client, drawings
):
    """An operator-supplied scale is not a verified one, and the result has to keep
    saying so after calibration (§3)."""
    document_id = _upload(client, drawings, "raster_plate")
    before = client.post(f"/api/documents/{document_id}/pages/1/area", json={}).json()
    after = _calibrate(client, document_id)

    joined_before = " ".join(before["warnings"]).lower()
    joined_after = " ".join(after["warnings"]).lower()
    assert "scale" in joined_before, (
        f"before: the missing scale is the warning — {before['warnings']}")
    assert "calibrat" in joined_after or "operator" in joined_after, (
        f"after: the operator's calibration must be surfaced — {after['warnings']}"
    )


def test_the_result_carries_the_calibration_as_evidence(client, drawings):
    """Explain Calculation and the JSON export both read from this."""
    document_id = _upload(client, drawings)
    after = _calibrate(client, document_id)

    evidence = " ".join(after["scale"]["evidence"]).lower()
    assert "16.39" in evidence, f"the measured span and distance: {evidence}"
    assert "unit" in evidence
    assert "16.39 mm" in after["scale"]["detail"] or "16.39" in after["scale"]["detail"]

    exported = json.dumps(after)
    assert "user_two_point_calibration" in exported
    assert '"known_length": 16.39' in exported or '"known_length":16.39' in exported


@pytest.mark.parametrize("unit,factor", [
    ("mm", 1.0), ("cm", 10.0), ("m", 1000.0), ("in", 25.4), ("ft", 304.8),
])
def test_every_offered_unit_converts_correctly(client, drawings, unit, factor):
    """The dropdown offers five units; each must scale by its own factor."""
    document_id = _upload(client, drawings)
    after = _calibrate(client, document_id, known=1.0, unit=unit)
    assert after["scale"]["mm_per_unit"] == pytest.approx(factor / SPAN, rel=1e-9)


def test_coincident_points_are_refused_with_a_reason(client, drawings):
    """A zero span would divide by zero. The API must say so rather than 500."""
    document_id = _upload(client, drawings)
    response = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={"scale": {"mode": "two_point", "points": [[10.0, 10.0], [10.0, 10.0]],
                        "known_length": 5.0, "known_unit": "mm"}},
    )
    assert response.status_code == 400
    assert "coincident" in json.dumps(response.json()).lower()


def test_a_second_calibration_replaces_the_first(client, drawings):
    """Recalibrating is an ordinary operation, not an error."""
    document_id = _upload(client, drawings)
    first = _calibrate(client, document_id, span=SPAN, known=KNOWN_MM)
    second = _calibrate(client, document_id, span=SPAN, known=KNOWN_MM * 2)

    assert first["scale"]["mm_per_unit"] == pytest.approx(1.0, abs=1e-9)
    assert second["scale"]["mm_per_unit"] == pytest.approx(2.0, abs=1e-9)
    assert second["geometry"] == first["geometry"], "still no geometry change"


def test_naming_a_saved_analysis_does_not_delay_the_answer(client, drawings, monkeypatch):
    """Persisting a recalculation used to happen on the request, adding four
    seconds to an interactive calibration. It now happens after the answer."""
    import threading

    from backend.api import routes

    started = threading.Event()
    release = threading.Event()

    class _SlowRepository:
        def get_summary(self, analysis_id):
            started.set()
            release.wait(timeout=5)     # stand-in for a slow cross-region write
            return None

    monkeypatch.setattr(routes, "_repository", lambda: _SlowRepository())
    document_id = _upload(client, drawings)

    import time

    begin = time.perf_counter()
    response = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={"analysis_id": "11111111-1111-1111-1111-111111111111"},
    )
    elapsed = time.perf_counter() - begin
    release.set()

    assert response.status_code == 200
    assert started.wait(timeout=5), "the save must actually have been attempted"
    assert elapsed < 4.0, (
        f"the answer waited {elapsed:.1f}s on a write it should not wait for"
    )
    # And the reply says a save is under way rather than claiming one happened.
    assert response.json()["saved"] == {"stored": None, "reason": "saving"}
