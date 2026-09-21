"""HTTP API.

CONSTITUTION.md §23: a small domain-shaped surface, added to only as the
vertical workflow needs it. §22: the backend is authoritative for every
engineering number, including the ones the user draws by hand.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from backend.api.schemas import (
    AreaRequest, PolygonMeasureRequest, RenameRequest, SaveAnalysisRequest,
    ScaleSpec,
)
from backend.area.projected import compute_projected_area
from backend.calibration.scale import scale_from_ratio, scale_from_two_points
from backend import diagnostics
from backend.config import ENGINE_VERSION, INTERPRETATION_VERSION
from backend.cad.dwg import DwgConversionUnavailable, converter_status
from backend.jobs import JOBS, advance, sink_for
from backend.progress import tracker_for
from backend.demo.catalogue import CATALOGUE, BY_ID, ensure_drawing, ground_truth
from backend.geometry.polygons import ring_to_polygon, union_polygons
from backend.geometry.regions import detect_title_block_ambiguity
from backend.models import BBox, GeometryRole, Region, Scale, ScaleSource, ViewSource
from backend.pdf.document import document_summary
from backend.pipeline import PreparedPage, region_scale
from backend.db import config as db_config
from backend.runtime import (
    UPLOAD_CHUNK_BYTES, instance_token, instance_uptime_seconds, max_upload_bytes,
)
from backend.config import DOCUMENT_TTL_SECONDS
from backend.store import STORE
from backend.supervisor import HOSTS
from backend.units import Area, to_mm
from backend import runtime

logger = logging.getLogger("projected_area.api")

router = APIRouter(prefix="/api")

#: How much of an upload is enough to identify it. ``_detect_kind`` looks at up
#: to 2 KB, so a 4 KB head is comfortably sufficient and is all that is ever
#: held in memory before the format is known.
HEAD_BYTES = 4096

#: Documents created from the built-in demo catalogue. Only these may be read
#: back over HTTP: the viewer needs the bytes to render a demo it did not choose
#: from disk, whereas a user's own drawing is proprietary and must not become
#: downloadable just because its id is known (§35).
_DEMO_DOCUMENTS: Set[str] = set()


@router.get("/health")
async def health() -> Dict[str, Any]:
    """Liveness plus what this instance can actually read.

    **async on purpose.** A sync ``def`` endpoint runs in FastAPI's threadpool, and
    that threadpool competes for the GIL with the analysis running on its own
    thread. Measured during a production DWG: ``/health`` took 6.3 seconds and a
    job poll 2.8 seconds, on a twelve-core machine — on a two-CPU instance that is
    long enough for a platform to decide the instance is dead and restart it, in
    the middle of an eight-minute measurement. The event loop itself stays
    responsive, so serving from the loop is what keeps these answerable.

    Everything here must therefore stay non-blocking: no query, no connection, no
    subprocess. The converter lookup is memoised and the database state is a cached
    value refreshed on another thread.

    Cheap on purpose: a health check runs every few seconds, so it opens no
    drawing and touches no disk beyond looking for the converter binary. It
    reports capability rather than configuration — no paths, no environment
    values, nothing about the host (§35) — because the question it answers is
    "can this instance do the job", and a deployment that silently lost DWG
    support is exactly the failure worth catching from outside.
    """
    dwg = converter_status()
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "interpretation_version": INTERPRETATION_VERSION,
        "pdf": True,
        "dxf": True,
        "dwg": bool(dwg["available"]),
        "dwg_converter": dwg.get("tool") if dwg["available"] else None,
        "dwg_converter_version": dwg.get("version") if dwg["available"] else None,
        "max_upload_mb": max_upload_bytes() // (1024 * 1024),
        # Configured, and actually answering. Both are useful and they differ: a
        # database that is set but unreachable is a different problem from one that
        # was never configured. Neither reports a host, a user or a URL (§35) —
        # a health endpoint is public, and a connection string is a credential.
        "database": db_config.is_enabled(),
        "persistence": _persistence_ready(),
        # Whether the link to the database is encrypted. A boolean, because the
        # question "is my password crossing the internet in clear text" deserves an
        # answer that does not require quoting the host it travels to.
        "database_tls": db_config.settings().tls,
        # Where measurements run: "process" means in a supervised child, so a
        # production DWG cannot stall this endpoint. No PID or host name (§35).
        "analysis_isolation": runtime.analysis_isolation(),
        "analysis_busy": HOSTS.busy(),
    }


def _persistence_ready() -> bool:
    """Whether a result could actually be saved right now. Never raises."""
    if not db_config.is_enabled():
        return False
    from backend.db import pool as db_pool

    return db_pool.probe()


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


def _detect_kind(head: bytes, file_name: str) -> str:
    """What this upload actually is, by signature rather than by extension.

    A drafter's file name is not evidence. The first bytes are.

    Args:
        head: The first :data:`HEAD_BYTES` of the file — never the whole of it,
            which for a production DWG is 93 MB.
    """
    first = head[:8]
    if first.startswith(b"%PDF") or b"%PDF" in head[:1024]:
        return "pdf"
    if first[:2] == b"AC" and first[2:6].isdigit():
        return "dwg"
    lowered = file_name.lower()
    if lowered.endswith(".dxf"):
        return "dxf"
    # An ASCII DXF opens with a SECTION group code; a binary one has a sentinel.
    if head[:22].startswith(b"AutoCAD Binary DXF"):
        return "dxf"
    probe = head[:512].lstrip()
    if probe.startswith(b"0") and b"SECTION" in head[:2048]:
        return "dxf"
    return "unknown"


def safe_file_name(raw: Optional[str]) -> str:
    """A display name derived from an upload, with no path in it.

    Browsers send a bare name, but a client is free to send anything, and this
    name reaches log lines, the UI and the converter's output basename. Only the
    final component survives, separators are stripped whatever the platform's
    convention, and the result is length-limited. It is never used to choose
    where anything is written — the store names files by document id — so this is
    defence in depth rather than the only barrier.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\x00')
    name = name.lstrip(".") or "drawing"
    return name[:120]


async def _spool_upload(file: UploadFile, suffix: str) -> Tuple[str, bytes, int, str]:
    """Stream an upload to a file in the store, enforcing the size limit.

    Returns ``(path, head, size, sha256)``. The caller owns the file and must
    delete it if it does not adopt it.

    The hash is computed from the same chunks already being written, so it costs
    one pass over data that is being read anyway — not a second read of a 97 MB
    drawing. It identifies the drawing for the analysis cache and is part of the
    audit record.

    The limit is checked *while* reading rather than after, because checking
    afterwards means having already accepted the whole thing: on a small instance
    a hostile or mistaken 2 GB upload would be fatal before the check ran. Reading
    in chunks also means peak memory is one chunk, not one drawing (§35).

    Raises:
        HTTPException: 400 if the upload is empty, 413 if it exceeds the limit.
    """
    limit = max_upload_bytes()
    path = STORE.spool_path(suffix)
    size = 0
    head = b""
    digest = hashlib.sha256()
    try:
        with open(path, "wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "kind": "too_large",
                            "headline": "That file is larger than this instance accepts.",
                            "reason": (
                                f"The limit is {limit // (1024 * 1024)} MB. Production DWGs "
                                "run to about 100 MB, so a file above the limit is usually "
                                "an archive or a rendering rather than a drawing."
                            ),
                            "fix": "Upload the drawing itself, or raise MAX_UPLOAD_MB.",
                        },
                    )
                if len(head) < HEAD_BYTES:
                    head += chunk[: HEAD_BYTES - len(head)]
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        STORE.discard(path)
        raise
    if size == 0:
        STORE.discard(path)
        raise HTTPException(status_code=400, detail="Empty upload")
    return path, head, size, digest.hexdigest()


def _refuse_missing_dwg_support(head: bytes, signature: str) -> HTTPException:
    """The 503 for a valid DWG in an environment that cannot convert one.

    Carries a real progress snapshot, because one stage genuinely did happen: the
    file was read far enough to confirm it is a DWG and which release wrote it.
    Presenting that stage as failed would blame the signature for the converter's
    absence, and the point of naming stages is to send the reader to the right
    place (§31). The snapshot comes from the same tracker the job would have used,
    so the weights live in one place.
    """
    status = converter_status()
    tracker = tracker_for("dwg")
    tracker.begin("validated")
    tracker.finish("validated", f"{signature} signature")
    snapshot = tracker.snapshot()
    snapshot["failed_stage"] = "converted"

    return HTTPException(status_code=503, detail={
        "kind": "dwg_component_missing",
        "headline": "DWG support requires the local CAD conversion component.",
        "version": signature,
        "reason": (
            "The drawing is a valid DWG. Reading one needs a converter, which is "
            "not available in this environment. Nothing is uploaded anywhere — the "
            "conversion runs here."
        ),
        "fix": status["fix"],
        "advice": status.get("advice"),
        "setup_command": status.get("setup_command", ""),
        "component": status["component"],
        "progress": snapshot,
    })


def _refuse_unreadable(head: bytes) -> HTTPException:
    """The 415 for something that is not a drawing this tool can read."""
    return HTTPException(status_code=415, detail={
        "kind": "unknown",
        "headline": "This file is not a readable drawing.",
        "reason": (
            f"It begins {head[:4].hex()}, which is neither a PDF (25504446) nor a "
            "DXF. Encrypted or rights-managed exports look like this."
        ),
        "fix": "Export an unprotected PDF or DXF from the application that owns it.",
    })


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Accept a drawing — PDF or DXF — and return its inventory.

    A DWG is detected and refused with the reason and the fix, not with a generic
    error: there is no pure-Python DWG reader, so it has to be exported to DXF.
    """
    file_name = safe_file_name(file.filename)
    spooled, head, size, source_sha256 = await _spool_upload(file, ".upload")
    kind = _detect_kind(head, file_name)

    if kind == "dwg" and not converter_status()["available"]:
        STORE.discard(spooled)
        raise _refuse_missing_dwg_support(head, head[:6].decode('ascii', 'replace'))
    if kind == "unknown":
        STORE.discard(spooled)
        raise _refuse_unreadable(head)

    HOSTS.sweep(DOCUMENT_TTL_SECONDS)
    if kind == "dwg":
        # Converting and parsing a production DWG takes minutes. Holding the
        # request open for that shows the user nothing; a job reports progress.
        job = JOBS.start(file_name, lambda j: _run_ingest(j, spooled, file_name, kind))
        return JSONResponse(status_code=202, content=job.as_dict())

    # Waiting on the analysis process blocks, so it happens on the threadpool:
    # this handler is async, and the event loop is what answers /health.
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_ingest_in_host, spooled, file_name, kind)


def _ingest_in_host(spooled: str, file_name: str, kind: str) -> Dict[str, Any]:
    from backend.supervisor import WorkerError, WorkerFailed

    host = _acquire_host_or_503()
    try:
        summary = host.request("ingest", {"spooled": spooled, "file_name": file_name,
                                          "kind": kind})
    except WorkerError as error:
        HOSTS.discard(host)
        STORE.discard(spooled)
        raise HTTPException(status_code=500, detail=error.described) from None
    except WorkerFailed as error:
        HOSTS.discard(host)
        STORE.discard(spooled)
        raise HTTPException(status_code=503, detail=error.outcome) from None
    except BaseException:
        HOSTS.discard(host)
        STORE.discard(spooled)
        raise
    finally:
        HOSTS.slot.release()
    HOSTS.bind(summary["document_id"], host)
    return summary


def _acquire_host_or_503():
    """The analysis slot and a fresh host, for a request that cannot wait."""
    if not HOSTS.slot.acquire(timeout=5):
        raise HTTPException(status_code=503, detail={
            "kind": "analysis_busy",
            "headline": "Another analysis is running.",
            "reason": "This server measures one drawing at a time.",
            "fix": "Try again when the current analysis has finished.",
        })
    try:
        return HOSTS.create()
    except BaseException:
        HOSTS.slot.release()
        raise


def _ingest(spooled: str, file_name: str, kind: str, mark=None) -> Dict[str, Any]:
    """Read an uploaded drawing into this process's store and describe it.

    Runs where the document will live — in the analysis process, normally.
    """
    if kind == "dwg":
        return _ingest_dwg(spooled, file_name, mark or (lambda stage, detail="": None))
    try:
        if kind == "dxf":
            stored = STORE.adopt_cad(spooled, file_name)
            summary = cad_document_summary(stored)
        else:
            stored = STORE.adopt_pdf(spooled, file_name)
            summary = document_summary(stored.doc, stored.file_name)
    except DwgConversionUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "dwg_component_missing",
                "headline": "DWG support requires the local CAD conversion component.",
                "reason": str(error),
                "fix": error.fix,
                "component": error.component,
            },
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    summary["document_id"] = stored.id
    summary["source_kind"] = kind
    return summary


def _run_ingest(job, spooled: str, file_name: str, kind: str) -> Dict[str, Any]:
    """The /documents DWG job: ingest in an analysis process, keep it there."""
    summary, host = _run_in_host(job, "ingest", {
        "spooled": spooled, "file_name": file_name, "kind": kind}, spooled)
    job.document_id = summary["document_id"]
    HOSTS.bind(summary["document_id"], host)
    return summary


def _ingest_dwg(spooled: str, file_name: str, mark) -> Dict[str, Any]:
    """Convert and read a spooled DWG, reporting each stage as it completes."""
    mark("validated", f"{_signature_of(spooled) or 'DWG'} signature")
    mark("converting", "converting locally to DXF")
    stored = STORE.adopt_dwg(spooled, file_name, on_stage=mark)
    mark("read", f"{len(stored.cad.primitives):,} primitives")

    summary = cad_document_summary(stored)
    summary["document_id"] = stored.id
    summary["source_kind"] = "dwg"

    layers = len([layer for layer in stored.cad.info.layers if layer.entity_count])
    blocks = len([b for b in stored.cad.info.blocks if b.insert_count])
    mark("analysed", f"{layers} layer(s), {blocks} block(s)")
    return summary


@router.post("/analyse")
async def analyse(file: UploadFile = File(...), reanalyse: bool = False) -> Any:
    """Ingest a drawing and measure it, reporting progress as it goes.

    Normally returns 202 with a job id. A production DWG takes minutes and a large
    PDF tens of seconds, and there is no useful difference between "slow" and
    "hung" to someone staring at a spinner — so every source takes the same path
    and the client follows the real stages.

    If this exact drawing has already been measured by this exact algorithm, the
    saved analysis is **offered** instead, as 200 with ``cached``. It is never
    substituted silently: reopening a previous result and running a new one are
    different acts, and only the operator knows which they meant. ``reanalyse=true``
    skips the offer.
    """
    file_name = safe_file_name(file.filename)
    spooled, head, size, source_sha256 = await _spool_upload(file, ".upload")
    kind = _detect_kind(head, file_name)

    if kind == "dwg" and not converter_status()["available"]:
        STORE.discard(spooled)
        raise _refuse_missing_dwg_support(head, head[:6].decode('ascii', 'replace'))
    if kind == "unknown":
        STORE.discard(spooled)
        raise _refuse_unreadable(head)

    if not reanalyse:
        previous = _find_saved(source_sha256)
        if previous is not None:
            # The upload is discarded: measuring it again is exactly what this
            # avoids, and keeping a customer's drawing on disk for an offer the
            # operator may decline would be the wrong default (§35).
            STORE.discard(spooled)
            return JSONResponse(status_code=200, content={
                "cached": previous.as_dict(),
                "source_sha256": source_sha256,
                "file_name": file_name,
            })

    HOSTS.sweep(DOCUMENT_TTL_SECONDS)
    # The tracker exists before the worker does, so the worker never has to wait
    # for it and the first poll already has a plan to render.
    job = JOBS.start(
        file_name,
        lambda j: _run_analysis(
            j, spooled, file_name, kind,
            source_sha256=source_sha256, source_size=size),
        tracker=tracker_for(kind),
    )
    job.source_sha256 = source_sha256
    job.source_size_bytes = size
    return JSONResponse(status_code=202, content=job.as_dict())


def _repository():
    """The analysis repository, or ``None`` when persistence is not configured.

    Every caller treats ``None`` as "cannot save", never as an error: an engine
    with nowhere to record a result still measures correctly, which is the whole
    reason persistence is optional.
    """
    if not db_config.is_enabled():
        return None
    try:
        from backend.db.analyses import AnalysisRepository

        return AnalysisRepository()
    except Exception as error:  # configuration problems must not break analysis
        logger.warning("persistence unavailable: %s", type(error).__name__)
        return None


def _find_saved(source_sha256: str):
    """A compatible saved analysis, or ``None``. Never raises."""
    repository = _repository()
    if repository is None:
        return None
    try:
        return repository.find_compatible(source_sha256)
    except Exception as error:
        logger.warning("cache lookup failed: %s", type(error).__name__)
        return None


def _signature_of(path: str) -> Optional[str]:
    """The DWG release code at the head of a file, read without loading it."""
    from backend.cad.dwg import dwg_signature

    with open(path, "rb") as handle:
        return dwg_signature(handle.read(8))


def _run_in_host(job, command: str, args: Dict[str, Any], spooled: str):
    """Run ``command`` in a fresh analysis process, one analysis at a time.

    The job reads as queued while it waits for the slot. Returns the value and the
    host, which keeps the measured document for calibration and recalculation. A
    host whose analysis failed holds nothing worth keeping and is retired.
    """
    job.queued = True
    with HOSTS.slot:
        job.queued = False
        host = HOSTS.create()
        try:
            value = host.request(command, args, sink=sink_for(job))
        except BaseException:
            HOSTS.discard(host)
            # The child may have died before adopting the upload; if so it is
            # still in this process's spool, and it is a customer drawing (§35).
            STORE.discard(spooled)
            raise
    return value, host


def _run_analysis(
    job, spooled: str, file_name: str, kind: str,
    source_sha256: str = "", source_size: int = 0,
) -> Dict[str, Any]:
    """Measure an upload in an analysis process, then record the result here.

    The measurement itself is :func:`_compute_analysis`, run in the child. Saving
    stays in this process, which owns the database connection: the child never
    connects to it, and persistence cannot change a number it did not compute.
    """
    payload, host = _run_in_host(job, "analyse", {
        "spooled": spooled, "file_name": file_name, "kind": kind,
        "source_sha256": source_sha256, "source_size": source_size,
    }, spooled)
    document_id = payload["document"]["document_id"]
    job.document_id = document_id
    HOSTS.bind(document_id, host, source_sha256)

    # Saving happens *after* the measurement is complete, from the finished
    # payload, and cannot alter it. A failure to save is reported as a failure to
    # save — the analysis it describes is exactly as correct either way.
    saved = _autosave(payload, source_sha256, file_name, source_size,
                      job.tracker.elapsed if job.tracker else None)
    if saved:
        payload["saved"] = saved
    return payload


def _compute_analysis(
    spooled: str, file_name: str, kind: str,
    source_sha256: str = "", source_size: int = 0,
    progress=None, mark=None,
) -> Dict[str, Any]:
    """Ingest, analyse and measure, reporting progress as it goes.

    Everything here is the ordinary pipeline. The only addition is that it says
    where it has got to (§31) — no stage does different work because someone is
    watching it. Runs in the analysis process; ``progress`` and ``mark`` carry its
    reports back to the job.
    """
    from backend.progress import NULL_PROGRESS

    progress = progress or NULL_PROGRESS
    mark = mark or (lambda stage, detail="": None)

    # Every stage boundary is logged with the process's and the container's memory
    # either side of it, so a run that is killed leaves a record of which stage it
    # was in and what it was holding (§32).
    diagnostics.note("analysis.start", kind=kind, size_bytes=source_size)

    progress.begin("validated" if kind != "pdf" else "loaded")
    if kind == "dwg":
        mark("validated", f"{_signature_of(spooled) or 'DWG'} signature")
        stored = STORE.adopt_dwg(
            spooled, file_name, on_stage=mark,
            progress=progress, source_sha256=source_sha256,
        )
        summary = cad_document_summary(stored)
    elif kind == "dxf":
        progress.begin("geometry")
        stored = STORE.adopt_cad(
            spooled, file_name, progress=progress, source_sha256=source_sha256)
        progress.finish("geometry", f"{len(stored.cad.primitives):,} primitives")
        summary = cad_document_summary(stored)
    else:
        stored = STORE.adopt_pdf(spooled, file_name, source_sha256=source_sha256)
        progress.finish("loaded", f"{stored.doc.page_count} page(s)")
        summary = document_summary(stored.doc, stored.file_name)

    summary["document_id"] = stored.id
    summary["source_kind"] = kind

    if kind != "pdf":
        info = stored.cad.info
        progress.finish("units", (
            f"{info.units_name} ($INSUNITS {info.insunits})" if info.units_declared
            else f"not declared ($INSUNITS {info.insunits})"))

    page_number = summary.get("suggested_page", 1)
    progress.begin("layers" if kind != "pdf" else "geometry")

    _stored, prepared = STORE.prepared_page(stored.id, page_number, progress=progress)
    if kind != "pdf":
        layers = len([l for l in stored.cad.info.layers if l.entity_count])
        blocks = len([b for b in stored.cad.info.blocks if b.insert_count])
        progress.finish("layers", f"{layers} layers · {blocks} blocks")
    else:
        progress.finish("geometry", f"{len(prepared.analysis.primitives):,} primitives")
        progress.begin("regions")
        views = len([r for r in prepared.regions if r.kind == "view"])
        progress.finish("regions", f"{len(prepared.regions)} regions · {views} views")
        progress.begin("scale")
        progress.finish("scale", (
            prepared.auto_scale.source.value.replace("_", " ")
            if prepared.auto_scale and prepared.auto_scale.verified
            else "not established"))

    analysis = _analyze_local(stored.id, page_number)

    try:
        diagnostics.note("stage.candidates.begin", primitives=(
            len(stored.cad.primitives) if stored.cad is not None
            else len(prepared.analysis.primitives)))
    except Exception as error:  # never fail a measurement over a log line
        logger.warning("diagnostics failed: %s", type(error).__name__)
    progress.begin("candidates")
    region = prepared.default_region()
    scale, warnings = _resolve_scale(prepared, region, ScaleSpec(), stored)
    result = compute_projected_area(
        analysis=prepared.analysis,
        fitz_page=(stored.doc.load_page(page_number - 1) if stored.doc is not None else None),
        document_id=stored.id,
        file_name=stored.file_name,
        scale=scale,
        region_bbox=region.bbox if region else None,
        view_source=ViewSource.AUTO_DETECTED if region else ViewSource.WHOLE_PAGE,
        view_label=region.label if region else "Whole page",
        extra_warnings=warnings,
    )
    progress.finish("candidates", f"{len(result.footprint_interpretations)} readings")

    progress.begin("area")
    roles = "dimension,annotation,centerline,hidden,sheet,hatch,uncertain"
    overlay = _geometry_local(stored.id, page_number, roles=roles, max_primitives=8000)
    progress.finish("area", f"{result.geometry.component_count} components")
    progress.complete()

    # Instrumentation must never be able to fail the measurement it observes. This
    # line once named a field that does not exist, and every analysis then failed
    # at 100 % — so the facts are read defensively and the whole call is guarded.
    try:
        watchdog = diagnostics.watchdog()
        diagnostics.note(
            "analysis.complete",
            **diagnostics.facts_of(
                result.geometry, "component_count", "holes", "face_count",
                "segment_count", "raw_primitive_count"),
            **(watchdog.summary() if watchdog else {}),
        )
    except Exception as error:  # pragma: no cover — defensive by design
        logger.warning("diagnostics failed: %s", type(error).__name__)

    return {
        "document": summary,
        "analysis": analysis,
        "area": result.as_dict(),
        "overlay": overlay,
    }


def _autosave(
    payload: Dict[str, Any], source_sha256: str, file_name: str,
    source_size: int, analysis_seconds: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Record a finished analysis, or explain why it was not recorded.

    Never raises: an eight-minute measurement must not be lost because a database
    was briefly unreachable, and the operator can save again from the workspace.
    """
    if not source_sha256:
        return None  # a demo drawing, or a path that did not stream an upload
    repository = _repository()
    if repository is None:
        return {"stored": False, "reason": "persistence_disabled"}
    try:
        stored = repository.save(
            payload, source_sha256=source_sha256, file_name=file_name,
            source_size_bytes=source_size, analysis_seconds=analysis_seconds,
        )
        return {"stored": True, **stored}
    except Exception as error:
        logger.warning("could not save analysis: %s", type(error).__name__)
        return {"stored": False, "reason": "save_failed",
                "error_kind": type(error).__name__}


# ── saved analyses ───────────────────────────────────────────────────────────


def _require_repository():
    """The repository, or a 503 that says persistence is off rather than broken."""
    repository = _repository()
    if repository is None:
        raise HTTPException(status_code=503, detail={
            "kind": "persistence_disabled",
            "headline": "Saved analyses are not available on this instance.",
            "reason": (
                "No database is configured, so results are measured but not kept. "
                "Everything else works exactly as it does with one."
            ),
            "fix": "Configure DATABASE_URL to enable saving and reopening.",
        })
    return repository


@router.post("/analyses")
def save_analysis(request: SaveAnalysisRequest) -> Dict[str, Any]:
    """Save a finished job's result, for when the automatic save failed.

    Takes a job id, not a result: the payload is read from the server's own copy of
    what it measured. A browser may ask for something to be saved; it may not say
    what the numbers were (§22).
    """
    repository = _require_repository()
    try:
        job = JOBS.get(request.job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={
            "kind": "job_lost",
            "headline": "That analysis is no longer held in memory.",
            "reason": (
                "A result can only be saved while the job that produced it is still "
                "in this process. The measurement itself was not affected."
            ),
            "fix": "Upload the drawing again to measure and save it.",
        }) from None
    if job.state != "done" or not job.result:
        raise HTTPException(status_code=409, detail="That job has no finished result")

    try:
        stored = repository.save(
            job.result,
            source_sha256=job.source_sha256,
            file_name=job.file_name,
            source_size_bytes=job.source_size_bytes,
            analysis_seconds=job.tracker.elapsed if job.tracker else None,
        )
    except Exception as error:
        raise _persistence_failed(error) from None
    return stored


@router.get("/analyses")
def list_analyses(limit: int = 20) -> Dict[str, Any]:
    """Recent completed analyses, newest first. Summaries only — no geometry."""
    repository = _require_repository()
    try:
        return {"analyses": [a.as_dict() for a in repository.recent(limit)]}
    except Exception as error:
        raise _persistence_failed(error) from None


@router.get("/analyses/{analysis_id}")
def open_analysis(analysis_id: str) -> Dict[str, Any]:
    """Everything needed to restore a saved analysis, without recomputing it."""
    repository = _require_repository()
    try:
        restored = repository.reopen(analysis_id)
    except Exception as error:
        raise _persistence_failed(error) from None
    if restored is None:
        raise HTTPException(status_code=404, detail={
            "kind": "analysis_not_found",
            "headline": "That saved analysis is no longer available.",
            "reason": "It may have been deleted, or its stored result removed.",
            "fix": "Upload the drawing again to measure it afresh.",
        })
    return restored


@router.patch("/analyses/{analysis_id}")
def rename_analysis(analysis_id: str, request: RenameRequest) -> Dict[str, Any]:
    """Give a saved analysis an operator-chosen name."""
    repository = _require_repository()
    try:
        summary = repository.rename(analysis_id, request.name)
    except Exception as error:
        raise _persistence_failed(error) from None
    if summary is None:
        raise HTTPException(status_code=404, detail="Unknown analysis")
    return {"analysis": summary.as_dict()}


@router.delete("/analyses/{analysis_id}")
def delete_analysis(analysis_id: str) -> Dict[str, Any]:
    """Remove a saved analysis, its artifact and its audit trail."""
    repository = _require_repository()
    try:
        removed = repository.delete(analysis_id)
    except Exception as error:
        raise _persistence_failed(error) from None
    if not removed:
        raise HTTPException(status_code=404, detail="Unknown analysis")
    return {"deleted": analysis_id}


def _persistence_failed(error: Exception) -> HTTPException:
    """A 503 that names the failure kind and never the connection string."""
    logger.warning("persistence operation failed: %s", type(error).__name__)
    return HTTPException(status_code=503, detail={
        "kind": "persistence_failed",
        "headline": "The saved-analysis store could not be reached.",
        "reason": f"The database did not respond ({type(error).__name__}).",
        "fix": "The measurement itself is unaffected. Try again shortly.",
    })


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> Dict[str, Any]:
    """Progress of a background ingestion.

    Also async, and for the same reason: this is the request the browser makes
    every 700 ms while a drawing is being measured, and it is the one whose failure
    the operator sees. It is an in-memory dictionary lookup — there is no reason
    for it to wait behind a threadpool that the analysis is starving.

    **A 404 here reports what this process knows, and nothing it does not.** Jobs
    live in the memory of the process that started them, so an unknown id means
    only that *this* process has no record. It does not mean the analysis stopped:
    during a rolling deployment the hosted 102 run was still measuring on the old
    instance while the new one answered 404. An earlier version of this message said
    the process had restarted or run out of memory — neither was true, and neither
    can be known from here.

    What can be known is which instance answered and how long it has been up. The
    client compares that with the instance that started the job, which turns "a
    different server answered" into an observation rather than a guess.
    """
    try:
        return JOBS.get(job_id).as_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail={
            "kind": "job_lost",
            "headline": "This server has no record of that analysis.",
            "reason": (
                "Each analysis is held inside the server instance that started it. "
                "This request reached an instance without that record. "
                "That is expected during a deployment, when requests move to a new "
                "instance while the old one may still be working. The analysis may "
                "have finished, may still be running elsewhere, or may have been "
                "interrupted — this instance cannot tell which."
            ),
            "fix": "Upload the drawing again to start a fresh analysis on this instance.",
            # Facts, not a diagnosis: which instance answered, and for how long it
            # has been running. Opaque — a hash, never a host name (§35).
            "instance": instance_token(),
            "instance_uptime_seconds": instance_uptime_seconds(),
        }) from None


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
    hosted = HOSTS.remove_document(document_id)
    if hosted is not None:
        return {"deleted": hosted}
    return {"deleted": STORE.remove(document_id)}


def _hosted_call(document_id: str, command: str, args: Dict[str, Any]) -> Any:
    """Send a document request to the analysis process holding it, if any.

    Returns ``_LOCAL`` when the document lives in this process (a demo drawing,
    or inline isolation). A child that dies mid-request is reported as what it was.
    """
    from backend.supervisor import WorkerError, WorkerFailed

    host = HOSTS.for_document(document_id)
    if host is None:
        return _LOCAL
    try:
        return host.request(command, {"document_id": document_id, **args})
    except WorkerError as error:
        raise HTTPException(status_code=500, detail=error.described) from None
    except WorkerFailed as error:
        HOSTS.discard(host)
        raise HTTPException(status_code=503, detail=error.outcome) from None


_LOCAL = object()


@router.get("/documents/{document_id}/pages/{page_number}/analyze")
def analyze(document_id: str, page_number: int) -> Dict[str, Any]:
    """Classify a page, detect candidate views, and attempt auto-calibration."""
    hosted = _hosted_call(document_id, "analyze", {"page_number": page_number})
    return _analyze_local(document_id, page_number) if hosted is _LOCAL else hosted


def _analyze_local(document_id: str, page_number: int) -> Dict[str, Any]:
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
    hosted = _hosted_call(document_id, "geometry", {
        "page_number": page_number, "roles": roles, "max_primitives": max_primitives})
    if hosted is not _LOCAL:
        return hosted
    return _geometry_local(document_id, page_number, roles, max_primitives)


def _geometry_local(
    document_id: str, page_number: int, roles: Optional[str] = None,
    max_primitives: int = 20000,
) -> Dict[str, Any]:
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
    """Calculate the projected area for a selected region and scale.

    The calculation runs where the document lives — normally the analysis
    process that measured it, because re-running the footprint geometry of a
    production drawing is as heavy as the first pass. Recording the new state is
    done here, afterwards, and cannot change it.
    """
    hosted = _hosted_call(document_id, "area", {
        "page_number": page_number,
        "request": request.model_dump(exclude={"analysis_id"}),
    })
    if hosted is _LOCAL:
        stored, body = _area_local(document_id, page_number, request)
        identity = _Identity(getattr(stored, "source_sha256", ""))
    else:
        body = hosted
        host = HOSTS.for_document(document_id)
        identity = _Identity(
            ((host.documents.get(document_id) or {}).get("source_sha256", "")) if host else "")
    if request.analysis_id:
        # Recording the new state costs a round trip to the database plus a
        # rewrite of the stored artifact — four seconds on the 101 PDF, and more
        # across regions. The operator is waiting on a calibration, not on a
        # write, and persistence is observational: it happens after the answer,
        # off the request, and cannot change it. What comes back says a save was
        # started, not that it finished.
        started = _persist_recalculation_async(
            request.analysis_id, identity, body,
            operator_scale=request.scale.mode != "auto")
        if started is not None:
            body = {**body, "saved": started}
    return body


def _area_local(document_id: str, page_number: int, request: AreaRequest):
    """The area calculation itself. Returns ``(stored, body)``."""
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
    return stored, result.as_dict()


def _persist_recalculation_async(
    analysis_id: str, stored, area_payload: Dict[str, Any], operator_scale: bool
) -> Optional[Dict[str, Any]]:
    """Save a recalculated result on a worker thread.

    Returns immediately with what is known now: that persistence is configured and
    a save was started, or that it was not. The outcome is visible afterwards in
    the analysis itself — a save that failed leaves the stored state as it was, and
    the measurement the operator is looking at is unaffected either way.

    Two recalculations of the same analysis in quick succession race, and the last
    write wins. The event trail records both, so what happened stays readable; a
    lock would serialise the interactive path again, which is what this exists to
    avoid.
    """
    repository = _repository()
    if repository is None:
        return {"stored": False, "reason": "persistence_disabled"}

    # The document's identity is read here, on the request thread, because the
    # store sweeps by TTL and the worker must not depend on it still being there.
    source_sha256 = getattr(stored, "source_sha256", "")
    payload = dict(area_payload)

    def work() -> None:
        try:
            _persist_recalculation(
                analysis_id, _Identity(source_sha256), payload,
                operator_scale=operator_scale, repository=repository)
        except Exception as error:  # a background save never breaks anything
            logger.warning("background save failed: %s", type(error).__name__)

    threading.Thread(target=work, name="pa-save", daemon=True).start()
    return {"stored": None, "reason": "saving"}


class _Identity:
    """Just the source hash, so the worker holds no reference to a stored document."""

    def __init__(self, source_sha256: str) -> None:
        self.source_sha256 = source_sha256


def _persist_recalculation(
    analysis_id: str, stored, area_payload: Dict[str, Any], operator_scale: bool,
    repository=None,
) -> Optional[Dict[str, Any]]:
    """Make a recalculated result the saved state of its analysis.

    Called after the measurement is complete and never able to change it. The new
    result replaces the saved one — a refresh or a reopen then shows the calibration
    the operator actually established — and the scale and area before and after are
    kept in the audit trail, so the change stays explainable.

    Refuses to write unless the document is provably the same drawing: a
    calibration measured on one drawing must never land on another's record.
    """
    repository = repository or _repository()
    if repository is None:
        return {"stored": False, "reason": "persistence_disabled"}
    try:
        summary = repository.get_summary(analysis_id)
        if summary is None:
            return {"stored": False, "reason": "analysis_not_found"}
        if not stored.source_sha256 or stored.source_sha256 != summary.source_sha256:
            return {"stored": False, "reason": "different_drawing"}

        restored = repository.reopen(analysis_id)
        if restored is None:
            return {"stored": False, "reason": "analysis_not_found"}
        payload = dict(restored["result"])
        payload["area"] = _stamp_calibration(area_payload, operator_scale)

        from backend.db.analyses import EVENT_RECALCULATED, EVENT_RECALIBRATED

        updated = repository.update_result(
            analysis_id, payload,
            event=EVENT_RECALIBRATED if operator_scale else EVENT_RECALCULATED,
        )
        return {"stored": True, **updated}
    except Exception as error:
        logger.warning("could not save recalculation: %s", type(error).__name__)
        return {"stored": False, "reason": "save_failed",
                "error_kind": type(error).__name__}


def _stamp_calibration(area_payload: Dict[str, Any], operator_scale: bool) -> Dict[str, Any]:
    """The result with its calibration's provenance and time made explicit.

    The engine records *what* the calibration was — two points, a length, a unit,
    the resulting scale. For the audit record it also matters *who* established it
    and *when*. Added around the engine's own record rather than into it: this is
    bookkeeping, and the engine's model is not changed for it.
    """
    from datetime import datetime, timezone

    area = dict(area_payload)
    scale = dict(area.get("scale") or {})
    calibration = scale.get("calibration")
    if calibration is not None or operator_scale:
        scale["calibration"] = {
            **(calibration or {}),
            "provenance": "operator_supplied" if operator_scale else scale.get("source"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "resulting_mm_per_unit": scale.get("mm_per_unit"),
        }
    area["scale"] = scale
    return area


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
