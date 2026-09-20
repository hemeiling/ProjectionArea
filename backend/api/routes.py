"""HTTP API.

CONSTITUTION.md §23: a small domain-shaped surface, added to only as the
vertical workflow needs it. §22: the backend is authoritative for every
engineering number, including the ones the user draws by hand.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from backend.api.schemas import AreaRequest, PolygonMeasureRequest, ScaleSpec
from backend.area.projected import compute_projected_area
from backend.calibration.scale import scale_from_ratio, scale_from_two_points
from backend.config import ENGINE_VERSION
from backend.cad.dwg import DwgConversionUnavailable, converter_status
from backend.jobs import JOBS, advance
from backend.demo.catalogue import CATALOGUE, BY_ID, ensure_drawing, ground_truth
from backend.geometry.polygons import ring_to_polygon, union_polygons
from backend.geometry.regions import detect_title_block_ambiguity
from backend.models import BBox, GeometryRole, Region, Scale, ScaleSource, ViewSource
from backend.pdf.document import document_summary
from backend.pipeline import PreparedPage, region_scale
from backend.store import STORE
from backend.units import Area, to_mm

router = APIRouter(prefix="/api")

#: Refuse implausibly large uploads rather than exhausting memory.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Documents created from the built-in demo catalogue. Only these may be read
#: back over HTTP: the viewer needs the bytes to render a demo it did not choose
#: from disk, whereas a user's own drawing is proprietary and must not become
#: downloadable just because its id is known (§35).
_DEMO_DOCUMENTS: Set[str] = set()


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@router.get("/capabilities")
def capabilities() -> Dict[str, Any]:
    """Which source formats this installation can actually read.

    The UI asks so it can offer DWG honestly: as a supported input when the
    local converter is present, and with the exact setup step when it is not.
    """
    dwg = converter_status()
    return {
        "formats": {
            "pdf": {"supported": True, "note": "vector or scanned"},
            "dxf": {"supported": True, "note": "read directly"},
            "dwg": {
                "supported": bool(dwg["available"]),
                "note": (
                    f"converted locally by {dwg['tool']} {dwg['version']}"
                    if dwg["available"]
                    else "requires the local CAD conversion component"
                ),
                **dwg,
            },
        },
        "engine_version": ENGINE_VERSION,
    }


def _detect_kind(data: bytes, file_name: str) -> str:
    """What this upload actually is, by signature rather than by extension.

    A drafter's file name is not evidence. The first bytes are.
    """
    head = data[:8]
    if head.startswith(b"%PDF") or b"%PDF" in data[:1024]:
        return "pdf"
    if head[:2] == b"AC" and head[2:6].isdigit():
        return "dwg"
    lowered = file_name.lower()
    if lowered.endswith(".dxf"):
        return "dxf"
    # An ASCII DXF opens with a SECTION group code; a binary one has a sentinel.
    if data[:22].startswith(b"AutoCAD Binary DXF"):
        return "dxf"
    probe = data[:512].lstrip()
    if probe.startswith(b"0") and b"SECTION" in data[:2048]:
        return "dxf"
    return "unknown"


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Accept a drawing — PDF or DXF — and return its inventory.

    A DWG is detected and refused with the reason and the fix, not with a generic
    error: there is no pure-Python DWG reader, so it has to be exported to DXF.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )

    file_name = file.filename or "drawing"
    kind = _detect_kind(data, file_name)

    if kind == "dwg" and not converter_status()["available"]:
        status = converter_status()
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "dwg_component_missing",
                "headline": "DWG support requires the local CAD conversion component.",
                "version": data[:6].decode("ascii", "replace"),
                "reason": (
                    "The drawing is a valid DWG. Reading one needs a local converter, "
                    "which is not installed on this machine. Nothing is uploaded "
                    "anywhere — the conversion runs here."
                ),
                "fix": f"Run: {status['setup_command']}",
                "component": status["component"],
            },
        )

    if kind == "unknown":
        preview = data[:4].hex()
        raise HTTPException(
            status_code=415,
            detail={
                "kind": "unknown",
                "headline": "This file is not a readable drawing.",
                "reason": (
                    f"It begins {preview}, which is neither a PDF (25504446) nor a "
                    "DXF. Encrypted or rights-managed exports look like this."
                ),
                "fix": "Export an unprotected PDF or DXF from the application that owns it.",
            },
        )

    if kind == "dwg":
        # Converting and parsing a production DWG takes minutes. Holding the
        # request open for that shows the user nothing; a job reports progress.
        job = JOBS.start(file_name, lambda j: _ingest_dwg(j, data, file_name))
        return JSONResponse(status_code=202, content=job.as_dict())

    try:
        if kind == "dxf":
            stored = STORE.add_cad(data, file_name)
            summary = cad_document_summary(stored)
        else:
            stored = STORE.add(data, file_name)
            summary = document_summary(stored.doc, stored.file_name)
    except DwgConversionUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "dwg_component_missing",
                "headline": "DWG support requires the local CAD conversion component.",
                "reason": str(error),
                "fix": f"Run: {error.setup_command}",
                "component": error.component,
            },
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    summary["document_id"] = stored.id
    summary["source_kind"] = kind
    return summary


def _ingest_dwg(job, data: bytes, file_name: str) -> Dict[str, Any]:
    """Convert and read a DWG, reporting each stage as it completes."""
    from backend.cad.dwg import dwg_signature

    advance(job, "validated", f"{dwg_signature(data) or 'DWG'} signature")
    advance(job, "converting", "converting locally to DXF")
    stored = STORE.add_dwg(
        data, file_name, on_stage=lambda stage, detail: advance(job, stage, detail)
    )
    advance(job, "read", f"{len(stored.cad.primitives):,} primitives")

    summary = cad_document_summary(stored)
    summary["document_id"] = stored.id
    summary["source_kind"] = "dwg"
    job.document_id = stored.id

    layers = len([layer for layer in stored.cad.info.layers if layer.entity_count])
    blocks = len([b for b in stored.cad.info.blocks if b.insert_count])
    advance(job, "analysed", f"{layers} layer(s), {blocks} block(s)")
    return summary


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    """Progress of a background ingestion."""
    try:
        return JOBS.get(job_id).as_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id!r}") from None


def cad_document_summary(stored) -> Dict[str, Any]:
    """The CAD equivalent of ``document_summary``, in the same shape."""
    drawing = stored.cad
    info = drawing.summary()
    if stored.conversion is not None:
        # The source is a DWG; conversion is how it got here, not what it is.
        info["conversion"] = stored.conversion.as_dict()
    box = drawing.bbox
    return {
        "file_name": stored.file_name,
        "page_count": 1,
        "metadata": {"dxf_version": info["dxf_version"], "layouts": info["layouts"]},
        "is_encrypted": False,
        "overall_drawing_type": "vector",
        "suggested_page": 1,
        "pages": [
            {
                "page": 1,
                "width_pt": round(box.width, 2) if box else 0.0,
                "height_pt": round(box.height, 2) if box else 0.0,
                "rotation": 0,
                "path_count": len(drawing.primitives),
                "image_count": 0,
                "image_coverage": 0.0,
                "text_length": sum(len(t.text) for t in drawing.texts),
                "drawing_type": "vector",
            }
        ],
        "cad": info,
    }


@router.get("/demo")
def list_demo_drawings() -> Dict[str, Any]:
    """The demo drawings the UI offers, so nobody has to find a file first.

    Each runs through the real pipeline when opened; nothing is pre-computed.
    ``expected`` records what the drawing was *constructed* to contain, for
    comparison against the measurement — it is never fed to the engine (§3).
    """
    return {"drawings": [d.as_dict() for d in CATALOGUE]}


@router.post("/demo/{demo_id}")
def open_demo_drawing(demo_id: str) -> Dict[str, Any]:
    """Ingest a demo drawing exactly as if it had been uploaded.

    Generates the PDF once, caches it, then hands it to the same store and the
    same ``document_summary`` an upload goes through — so a demo result is
    produced by the real engine or not at all.
    """
    demo = BY_ID.get(demo_id)
    if demo is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown demo drawing {demo_id!r}; available: {sorted(BY_ID)}",
        )

    path = ensure_drawing(demo_id)
    with open(path, "rb") as handle:
        data = handle.read()
    stored = STORE.add(data, demo.as_dict()["file_name"])
    _DEMO_DOCUMENTS.add(stored.id)

    summary = document_summary(stored.doc, stored.file_name)
    summary["document_id"] = stored.id
    summary["demo"] = demo.as_dict()
    summary["demo"]["ground_truth"] = {
        key: value for key, value in ground_truth(demo_id).items() if key != "path"
    }
    return summary


@router.get("/documents/{document_id}/file")
def document_file(document_id: str):
    """Serve a **demo** document's bytes back, so the viewer can render it.

    Deliberately refuses anything else. An uploaded drawing is proprietary; the
    browser already holds the copy it uploaded, so there is no reason for this
    endpoint to hand one out and every reason not to (§35).
    """
    if document_id not in _DEMO_DOCUMENTS:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only built-in demo drawings can be read back. An uploaded drawing "
                "stays on the server and is never served over HTTP."
            ),
        )
    stored = STORE.get(document_id)
    return FileResponse(stored.path, media_type="application/pdf", filename=stored.file_name)


@router.delete("/documents/{document_id}")
def delete_document(document_id: str) -> Dict[str, Any]:
    """Delete an upload and its temporary file immediately."""
    return {"deleted": STORE.remove(document_id)}


@router.get("/documents/{document_id}/pages/{page_number}/analyze")
def analyze(document_id: str, page_number: int) -> Dict[str, Any]:
    """Classify a page, detect candidate views, and attempt auto-calibration."""
    _stored, prepared = _prepared(document_id, page_number)
    summary = prepared.summary()
    # Surfaced so the UI can say "review recommended" and point at the region.
    # Reported, never corrected (§7) — see detect_title_block_ambiguity.
    if stored_kind_is_cad(_stored):
        # A CAD drawing that does not declare its units can still be pinned in
        # one click, using the dimensions it does state.
        from backend.cad.dxf import dimension_evidence

        summary["cad_dimension_evidence"] = dimension_evidence(_stored.cad)
        if _stored.conversion is not None:
            summary["conversion"] = _stored.conversion.as_dict()

    summary["ambiguities"] = [
        a
        for a in [
            detect_title_block_ambiguity(
                prepared.regions,
                prepared.analysis.primitives,
                prepared.analysis.page_bbox,
                measured_label=None,
            )
        ]
        if a
    ]
    return summary


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

    scale, scale_warnings = _resolve_scale(prepared, region, request.scale, stored)

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
        # Only the raster fallback needs the page; a DXF has none.
        fitz_page=(stored.doc.load_page(page_number - 1) if stored.doc is not None else None),
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


def stored_kind_is_cad(stored) -> bool:
    return getattr(stored, "cad", None) is not None


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


def _resolve_scale(
    prepared: PreparedPage, region: Optional[Region], spec: ScaleSpec, stored=None
):
    """Turn a scale request into a :class:`Scale` plus any warnings."""
    if spec.mode == "cad_unit":
        if stored is None or stored.cad is None:
            raise HTTPException(
                status_code=400,
                detail="cad_unit mode applies only to a CAD drawing (DWG or DXF)",
            )
        from backend.cad.dxf import scale_from_stated_unit

        try:
            return scale_from_stated_unit(stored.cad.info, spec.known_unit), []
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    if spec.mode == "auto":
        # A CAD drawing states its units, so there is nothing to re-derive: the
        # prepared page already carries a scale read from the file header, which
        # is stronger than anything measuring the drawing could produce.
        declared = prepared.auto_scale
        if declared is not None and declared.verified and declared.source is ScaleSource.CAD_UNITS:
            return declared, list(prepared.scale_warnings)
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
