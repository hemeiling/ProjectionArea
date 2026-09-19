"""HTTP API.

CONSTITUTION.md §23: a small domain-shaped surface, added to only as the
vertical workflow needs it. §22: the backend is authoritative for every
engineering number, including the ones the user draws by hand.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from backend.api.schemas import AreaRequest, PolygonMeasureRequest, ScaleSpec
from backend.area.projected import compute_projected_area
from backend.calibration.scale import scale_from_ratio, scale_from_two_points
from backend.config import ENGINE_VERSION
from backend.geometry.polygons import ring_to_polygon, union_polygons
from backend.models import BBox, GeometryRole, Region, Scale, ScaleSource, ViewSource
from backend.pdf.document import document_summary
from backend.pipeline import PreparedPage, region_scale
from backend.store import STORE
from backend.units import Area, to_mm

router = APIRouter(prefix="/api")

#: Refuse implausibly large uploads rather than exhausting memory.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Accept a PDF and return its page inventory and classification."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )
    try:
        stored = STORE.add(data, file.filename or "drawing.pdf")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    summary = document_summary(stored.doc, stored.file_name)
    summary["document_id"] = stored.id
    return summary


@router.delete("/documents/{document_id}")
def delete_document(document_id: str) -> Dict[str, Any]:
    """Delete an upload and its temporary file immediately."""
    return {"deleted": STORE.remove(document_id)}


@router.get("/documents/{document_id}/pages/{page_number}/analyze")
def analyze(document_id: str, page_number: int) -> Dict[str, Any]:
    """Classify a page, detect candidate views, and attempt auto-calibration."""
    _stored, prepared = _prepared(document_id, page_number)
    return prepared.summary()


@router.get("/documents/{document_id}/pages/{page_number}/geometry")
def geometry(
    document_id: str,
    page_number: int,
    roles: Optional[str] = None,
    max_primitives: int = 20000,
) -> Dict[str, Any]:
    """Return classified primitives for the audit overlay.

    Args:
        roles: Comma-separated role filter, e.g. ``profile,dimension``.
        max_primitives: Cap on returned primitives; the response says when it bit.
    """
    _stored, prepared = _prepared(document_id, page_number)
    wanted: Optional[Set[str]] = (
        {r.strip() for r in roles.split(",") if r.strip()} if roles else None
    )
    selected = [
        p for p in prepared.analysis.primitives if wanted is None or p.role.value in wanted
    ]
    truncated = len(selected) > max_primitives
    return {
        "page": page_number,
        "primitive_count": len(selected),
        "truncated": truncated,
        "primitives": [p.as_dict() for p in selected[:max_primitives]],
        "text_items": [t.as_dict() for t in prepared.analysis.text_items],
    }


@router.post("/documents/{document_id}/pages/{page_number}/area")
def area(document_id: str, page_number: int, request: AreaRequest) -> Dict[str, Any]:
    """Calculate the projected area for a selected region and scale."""
    stored, prepared = _prepared(document_id, page_number)

    region_bbox, view_source, view_label = _resolve_region(prepared, request)
    region = prepared.region_by_id(request.region_id) if request.region_id else None

    scale, scale_warnings = _resolve_scale(prepared, region, request.scale)

    include_roles: Optional[Set[GeometryRole]] = None
    if request.include_roles:
        try:
            include_roles = {GeometryRole(value) for value in request.include_roles}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"Unknown geometry role: {error}") from error

    overrides: Dict[int, GeometryRole] = {}
    for key, value in (request.role_overrides or {}).items():
        try:
            overrides[int(key)] = GeometryRole(value)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Bad role override {key!r}: {value!r} is not one of "
                    f"{[r.value for r in GeometryRole]}"
                ),
            ) from error

    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=stored.doc.load_page(page_number - 1),
        document_id=document_id,
        file_name=stored.file_name,
        scale=scale,
        region_bbox=region_bbox,
        view_source=view_source,
        view_label=view_label,
        subtract_holes=request.subtract_holes,
        include_roles=include_roles,
        excluded_component_ids=set(request.exclude_components),
        component_selection=request.component_selection,
        role_overrides=overrides,
        manual_add=request.manual_add,
        manual_subtract=request.manual_subtract,
        close_gaps=request.close_gaps,
        force_raster=request.force_raster,
        extra_warnings=scale_warnings,
    )
    return result.as_dict()


@router.post("/measure/polygon")
def measure_polygon(request: PolygonMeasureRequest) -> Dict[str, Any]:
    """Authoritative area for hand-drawn rings. §22.

    Added rings are unioned so overlapping traces count once; subtracted rings
    are then removed. This is the backend counterpart of the manual polygon
    tool in the viewer, so a hand measurement is computed by the same engine as
    an automatic one.
    """
    scale = _scale_from_spec(request.scale)

    added = [p for p in (ring_to_polygon(ring) for ring in request.add) if p is not None]
    removed = [p for p in (ring_to_polygon(ring) for ring in request.subtract) if p is not None]
    if not added:
        raise HTTPException(status_code=400, detail="At least one valid ring of 3+ points is required")

    geometry = union_polygons(added)
    if geometry is None:
        raise HTTPException(status_code=400, detail="Rings enclose no area")
    hole_geometry = union_polygons(removed)
    net = geometry.difference(hole_geometry) if hole_geometry is not None else geometry

    payload: Dict[str, Any] = {
        "area_pdf_units2": {"net": float(net.area), "gross": float(geometry.area)},
        "scale": scale.as_dict(),
        "method": "user_drawn_polygon",
    }
    if scale.verified:
        factor = scale.mm_per_unit ** 2
        payload["projected_area"] = {
            "verified": True,
            "net": Area(net.area * factor).as_dict(),
            "gross": Area(geometry.area * factor).as_dict(),
            "requested_unit": request.output_unit,
            "requested_value": Area(net.area * factor).to(request.output_unit),
        }
    else:
        payload["projected_area"] = {
            "verified": False,
            "message": "Scale not verified. Physical projected area cannot yet be calculated.",
        }
    return payload


# ── helpers ──────────────────────────────────────────────────────────────────


def _prepared(document_id: str, page_number: int):
    try:
        return STORE.prepared_page(document_id, page_number)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown document; re-upload the file")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _resolve_region(prepared: PreparedPage, request: AreaRequest):
    """Pick the region to measure and record how it was chosen."""
    if request.region_bbox is not None:
        box = request.region_bbox
        return (
            BBox(box.x0, box.y0, box.x1, box.y1),
            ViewSource.USER_SELECTED,
            "User-selected region",
        )
    if request.region_id:
        region = prepared.region_by_id(request.region_id)
        if region is None:
            raise HTTPException(status_code=400, detail=f"Unknown region '{request.region_id}'")
        return region.bbox, ViewSource.USER_SELECTED, region.label or region.id
    return None, ViewSource.WHOLE_PAGE, "Whole page"


def _resolve_scale(prepared: PreparedPage, region: Optional[Region], spec: ScaleSpec):
    """Turn a scale request into a :class:`Scale` plus any warnings."""
    if spec.mode == "auto":
        return region_scale(prepared, region)
    return _scale_from_spec(spec), []


def _scale_from_spec(spec: ScaleSpec) -> Scale:
    if spec.mode == "two_point":
        if not spec.points or len(spec.points) != 2 or spec.known_length is None:
            raise HTTPException(
                status_code=400,
                detail="two_point calibration needs exactly two points and a known length",
            )
        try:
            length_mm = to_mm(float(spec.known_length), spec.known_unit)
            return scale_from_two_points(
                (float(spec.points[0][0]), float(spec.points[0][1])),
                (float(spec.points[1][0]), float(spec.points[1][1])),
                length_mm,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    if spec.mode == "ratio":
        if not spec.ratio_denominator or spec.ratio_denominator <= 0:
            raise HTTPException(status_code=400, detail="ratio mode needs a positive ratio_denominator")
        return scale_from_ratio(float(spec.ratio_denominator))

    if spec.mode == "mm_per_unit":
        if not spec.mm_per_unit or spec.mm_per_unit <= 0:
            raise HTTPException(status_code=400, detail="mm_per_unit mode needs a positive value")
        return Scale(
            mm_per_unit=float(spec.mm_per_unit),
            source=ScaleSource.USER_TWO_POINT,
            confidence=0.90,
            detail="Scale supplied directly by the caller.",
            evidence=[f"{spec.mm_per_unit:.6f} mm per PDF unit"],
        )

    return Scale(
        mm_per_unit=None,
        source=ScaleSource.NONE,
        confidence=0.0,
        detail="Caller requested no scale.",
    )
