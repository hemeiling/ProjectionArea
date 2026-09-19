"""HTTP surface tests.

CONSTITUTION.md §23 and §31: a small API whose errors are actionable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.store import STORE


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def uploaded(client, drawings):
    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        response = client.post(
            "/api/documents",
            files={"file": ("plate_with_holes.pdf", handle.read(), "application/pdf")},
        )
    assert response.status_code == 200
    payload = response.json()
    yield payload
    client.delete(f"/api/documents/{payload['document_id']}")


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_viewer_is_served_from_the_same_origin(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_upload_returns_page_inventory(uploaded):
    assert uploaded["page_count"] == 1
    assert uploaded["overall_drawing_type"] == "vector"
    assert uploaded["pages"][0]["path_count"] > 0
    assert uploaded["suggested_page"] == 1


def test_upload_rejects_non_pdf(client):
    response = client.post(
        "/api/documents", files={"file": ("notes.txt", b"not a pdf at all", "text/plain")}
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_analyze_reports_regions_and_scale(client, uploaded):
    payload = client.get(
        f"/api/documents/{uploaded['document_id']}/pages/1/analyze"
    ).json()
    assert payload["drawing_type"] == "vector"
    assert payload["regions"]
    assert payload["default_region_id"]
    assert payload["scale"]["verified"] is True
    assert payload["scale"]["source"] == "dimension_consensus"
    assert payload["scale"]["evidence"]


def test_geometry_endpoint_feeds_the_audit_overlay(client, uploaded):
    payload = client.get(
        f"/api/documents/{uploaded['document_id']}/pages/1/geometry",
        params={"roles": "profile,dimension,centerline,hidden,sheet"},
    ).json()
    assert payload["primitive_count"] > 0
    roles = {p["role"] for p in payload["primitives"]}
    assert "profile" in roles
    assert all(p["points"] for p in payload["primitives"])


def test_area_with_auto_scale(client, uploaded, drawings):
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    response = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["projected_area"]["verified"] is True
    assert payload["projected_area"]["net"]["mm2"] == pytest.approx(
        drawings["plate_with_holes"]["net_area_mm2"], rel=2e-3
    )
    assert payload["confidence"]["percent"] > 80
    assert payload["method"] == "vector_exact_polygonization"


def test_area_with_two_point_calibration(client, uploaded, drawings):
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    # The plate is 200 mm wide and drawn from x = 140 at 1:2.
    span = 200.0 / drawings["plate_with_holes"]["mm_per_unit"]
    response = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={
            "region_id": analysis["default_region_id"],
            "scale": {
                "mode": "two_point",
                "points": [[140.0, 150.0], [140.0 + span, 150.0]],
                "known_length": 200.0,
                "known_unit": "mm",
            },
        },
    )
    payload = response.json()
    assert payload["scale"]["source"] == "user_two_point_calibration"
    assert payload["projected_area"]["net"]["mm2"] == pytest.approx(
        drawings["plate_with_holes"]["net_area_mm2"], rel=2e-3
    )


def test_area_without_scale_refuses_to_report_millimetres(client, uploaded):
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    payload = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={"region_id": analysis["default_region_id"], "scale": {"mode": "none"}},
    ).json()
    assert payload["projected_area"]["verified"] is False
    assert payload["projected_area"]["net"] is None
    assert payload["area_pdf_units2"]["net"] > 0
    assert payload["confidence"]["percent"] == 0


def test_excluding_a_component_changes_the_answer(client, uploaded):
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    full = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()
    component_id = full["components"][0]["id"]
    reduced = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "exclude_components": [component_id]},
    ).json()
    assert reduced["area_pdf_units2"]["net"] < full["area_pdf_units2"]["net"]


def test_unknown_document_is_a_clear_404(client):
    response = client.get("/api/documents/doesnotexist/pages/1/analyze")
    assert response.status_code == 404
    assert "re-upload" in response.json()["detail"]


def test_page_out_of_range_is_actionable(client, uploaded):
    response = client.get(f"/api/documents/{uploaded['document_id']}/pages/99/analyze")
    assert response.status_code == 400
    assert "out of range" in response.json()["detail"]


def test_bad_calibration_request_is_explained(client, uploaded):
    response = client.post(
        f"/api/documents/{uploaded['document_id']}/pages/1/area",
        json={"scale": {"mode": "two_point", "points": [[0, 0]]}},
    )
    assert response.status_code == 400
    assert "two points" in response.json()["detail"]


def test_manual_polygon_measurement_is_backend_authoritative(client):
    payload = client.post(
        "/api/measure/polygon",
        json={
            "add": [[[0, 0], [100, 0], [100, 50], [0, 50]]],
            "subtract": [[[10, 10], [30, 10], [30, 30], [10, 30]]],
            "scale": {"mode": "mm_per_unit", "mm_per_unit": 1.0},
            "output_unit": "mm2",
        },
    ).json()
    assert payload["area_pdf_units2"]["net"] == pytest.approx(5000 - 400)
    assert payload["projected_area"]["requested_value"] == pytest.approx(4600.0)


def test_manual_polygon_union_does_not_double_count(client):
    payload = client.post(
        "/api/measure/polygon",
        json={
            "add": [
                [[0, 0], [60, 0], [60, 50], [0, 50]],
                [[40, 0], [100, 0], [100, 50], [40, 50]],
            ],
            "scale": {"mode": "mm_per_unit", "mm_per_unit": 1.0},
        },
    ).json()
    assert payload["area_pdf_units2"]["net"] == pytest.approx(5000.0)


def test_delete_removes_the_upload(client, drawings):
    with open(drawings["broken_contour"]["path"], "rb") as handle:
        document_id = client.post(
            "/api/documents", files={"file": ("x.pdf", handle.read(), "application/pdf")}
        ).json()["document_id"]
    assert client.delete(f"/api/documents/{document_id}").json()["deleted"] is True
    assert client.get(f"/api/documents/{document_id}/pages/1/analyze").status_code == 404


def _profile_indices(client, document_id):
    payload = client.get(
        f"/api/documents/{document_id}/pages/1/geometry", params={"roles": "profile"}
    ).json()
    return [p["index"] for p in payload["primitives"]], payload["primitives"]


def test_role_override_can_demote_detected_linework(client, uploaded):
    """§9: the engineer overrules the classifier, and it is recorded."""
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    baseline = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()

    _indices, primitives = _profile_indices(client, document_id)
    outline = max(primitives, key=lambda p: (p["bbox"]["x1"] - p["bbox"]["x0"])
                                            * (p["bbox"]["y1"] - p["bbox"]["y0"]))
    demoted = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "role_overrides": {str(outline["index"]): "annotation"}},
    ).json()

    assert demoted["area_pdf_units2"]["net"] < baseline["area_pdf_units2"]["net"]
    assert demoted["geometry"]["role_counts"], "roles must still be reported"


def test_role_override_is_not_persisted_between_requests(client, uploaded):
    """Overrides must not mutate the cached page, or they could not be undone."""
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    first = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()

    _indices, primitives = _profile_indices(client, document_id)
    outline = max(primitives, key=lambda p: (p["bbox"]["x1"] - p["bbox"]["x0"])
                                            * (p["bbox"]["y1"] - p["bbox"]["y0"]))
    client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "role_overrides": {str(outline["index"]): "annotation"}},
    )
    restored = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()
    assert restored["area_pdf_units2"]["net"] == pytest.approx(first["area_pdf_units2"]["net"])


def test_unknown_role_override_is_rejected_with_the_valid_list(client, uploaded):
    response = client.post(
        f"/api/documents/{uploaded['document_id']}/pages/1/area",
        json={"scale": {"mode": "none"}, "role_overrides": {"3": "not_a_role"}},
    )
    assert response.status_code == 400
    assert "profile" in response.json()["detail"]


def test_hand_drawn_boundary_is_added_and_recorded(client, uploaded):
    """The 'draw boundary' tool, measured by the backend like everything else."""
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    baseline = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()

    patched = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "manual_add": [[[600, 200], [700, 200], [700, 300], [600, 300]]]},
    ).json()

    added = patched["area_pdf_units2"]["net"] - baseline["area_pdf_units2"]["net"]
    assert added == pytest.approx(100 * 100, rel=1e-6)
    assert any("drawn by hand" in a for a in patched["assumptions"])


def test_hand_erased_region_only_removes_material_that_was_counted(client, uploaded):
    """Erasing over a hole must not subtract area that was never included."""
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    baseline = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()

    # A square placed away from the holes removes exactly its own area.
    erased = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "manual_subtract": [[[300, 170], [340, 170], [340, 210], [300, 210]]]},
    ).json()
    removed = baseline["area_pdf_units2"]["net"] - erased["area_pdf_units2"]["net"]
    assert removed == pytest.approx(40 * 40, rel=1e-6)
    assert any("erased by hand" in a for a in erased["assumptions"])


def test_manual_correction_counts_as_user_verification(client, uploaded):
    """§10 lists human verification as confidence evidence."""
    document_id = uploaded["document_id"]
    analysis = client.get(f"/api/documents/{document_id}/pages/1/analyze").json()
    body = {"region_id": analysis["default_region_id"], "scale": {"mode": "auto"}}
    baseline = client.post(f"/api/documents/{document_id}/pages/1/area", json=body).json()
    corrected = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={**body, "manual_add": [[[600, 200], [700, 200], [700, 300], [600, 300]]]},
    ).json()
    assert corrected["confidence"]["percent"] >= baseline["confidence"]["percent"]
    assert any("reviewed and corrected" in n for n in corrected["confidence"]["notes"])


def test_uploads_are_cleaned_up_when_the_app_shuts_down(monkeypatch):
    """The lifespan teardown must actually run.

    Uploaded drawings are proprietary and are deleted when the process stops
    (§35). That guarantee is one decorator away from silently disappearing —
    swapping `@app.on_event("shutdown")` for a lifespan handler is exactly the
    kind of change that can drop it without a single test noticing — so the
    teardown is asserted rather than assumed.
    """
    calls = []

    class RecordingStore:
        def shutdown(self) -> None:
            calls.append("shutdown")

    monkeypatch.setattr("backend.main.STORE", RecordingStore())

    with TestClient(app) as probe:
        assert probe.get("/api/health").status_code == 200
        assert calls == [], "the store must survive while the app is serving"

    assert calls == ["shutdown"], "lifespan teardown did not empty the upload store"
