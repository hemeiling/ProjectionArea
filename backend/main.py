"""FastAPI entry point.

Run with::

    .venv/bin/uvicorn backend.main:app --reload --port 8000

then open http://localhost:8000/ — the viewer is served from the same origin,
so there is no CORS hop and no ``file://`` restrictions.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import health as api_health, router
from backend.config import ENGINE_VERSION
from backend.store import STORE

logging.basicConfig(
    level=os.environ.get("PROJECTED_AREA_LOG", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
logger = logging.getLogger("projected_area")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
APP_HTML = os.path.join(FRONTEND_DIR, "index.html")
#: The original planimeter. Its manual wand and polygon tools are the documented
#: fallback for drawings the automatic path cannot handle, so it stays reachable.
CLASSIC_HTML = os.path.join(PROJECT_ROOT, "cad-area-meter.html")

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: nothing to set up, everything to clean up.

    Uploaded drawings are proprietary (§35), so the temporary store is emptied
    when the process stops. The teardown sits after the ``yield``; the store is
    created lazily on first upload, so there is no startup half.
    """
    yield
    STORE.shutdown()


app = FastAPI(
    title="Projected Area Analyzer",
    version=ENGINE_VERSION,
    description=(
        "Engineering-drawing PDF to verified projected area. Vector geometry "
        "first, explicit scale, auditable overlay."
    ),
    lifespan=lifespan,
)

# The viewer is served from this origin; CORS is opened only for local
# development against a separately hosted page.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "null"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.middleware("http")
async def log_stage_timing(request: Request, call_next: Callable) -> Any:
    """Structured request logging. §32 — stages and durations, never content."""
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "%s %s -> %s in %.1f ms", request.method, request.url.path, response.status_code, duration_ms
    )
    return response


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/health", include_in_schema=False)
def health() -> Any:
    """The same report as ``/api/health``, at the path a platform expects.

    Render, and most other hosts, want a health check at the root. Rather than
    two implementations that can disagree, this is the same function.
    """
    return api_health()


@app.get("/", include_in_schema=False)
def viewer() -> Any:
    """Serve the analysis application."""
    if not os.path.exists(APP_HTML):
        # No path in the message: this is served to a browser, and where the
        # file was expected is a fact about the host (§35). The log line below
        # carries the detail for whoever can act on it.
        logger.error("front end missing at %s", APP_HTML)
        return JSONResponse(
            {"error": "The front end is not installed in this deployment."},
            status_code=404,
        )
    return FileResponse(APP_HTML, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Any:
    """A tiny inline mark, so the browser stops asking for one."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<rect width="16" height="16" fill="#1F6F63"/>'
        '<rect x="3.5" y="4.5" width="9" height="7" fill="none" stroke="#fff"/>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/classic", include_in_schema=False)
def classic_viewer() -> Any:
    """The original planimeter, kept for its manual wand and polygon tools."""
    if not os.path.exists(CLASSIC_HTML):
        return JSONResponse({"error": "Classic viewer not found"}, status_code=404)
    return FileResponse(CLASSIC_HTML, media_type="text/html")
