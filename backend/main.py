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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend.api.routes import router
from backend.config import ENGINE_VERSION
from backend.store import STORE

logging.basicConfig(
    level=os.environ.get("PROJECTED_AREA_LOG", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
logger = logging.getLogger("projected_area")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER_HTML = os.path.join(PROJECT_ROOT, "cad-area-meter.html")

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


@app.get("/", include_in_schema=False)
def viewer() -> Any:
    """Serve the drawing viewer."""
    if not os.path.exists(VIEWER_HTML):
        return JSONResponse({"error": f"Viewer not found at {VIEWER_HTML}"}, status_code=404)
    return FileResponse(VIEWER_HTML, media_type="text/html")
