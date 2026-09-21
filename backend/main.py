"""FastAPI entry point.

Run with::

    .venv/bin/uvicorn backend.main:app --reload --port 8000

then open http://localhost:8000/ — the viewer is served from the same origin,
so there is no CORS hop and no ``file://`` restrictions.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import diagnostics
from backend.api.routes import health as api_health, router
from backend.config import ENGINE_VERSION
from backend.db import config as db_config
from backend.db import pool as db_pool_module
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
    """Prepare persistence if it is configured; clean up the store on the way out.

    Uploaded drawings are proprietary (§35), so the temporary store is emptied
    when the process stops.

    The startup half brings this application's own schema up to date. It is
    deliberately not fatal: with no database the engine measures exactly as well,
    and an instance that cannot reach one should serve drawings rather than refuse
    to boot. Either way the state is stated in the log, so "why was nothing saved"
    has an answer on the first line rather than after an investigation.
    """
    # Watching from inside: a platform cannot see that a process stopped being
    # able to answer, only that it stopped answering. This reports how long the
    # Python threads were stalled, which is the difference between "the instance
    # ran out of memory" and "the instance was restarted while it was working".
    diagnostics.start_watchdog()
    db_config.load_local_env()
    # Before anything connects: the driver logs its own connection failures, and
    # those messages name the host.
    db_pool_module.install_log_redaction()
    settings = db_config.settings()
    if settings.enabled:
        try:
            from backend.db.migrations import migrate

            applied = migrate()
            logger.info(
                "%s%s", settings.summary,
                f" · applied migrations {applied}" if applied else " · schema current",
            )
        except Exception as error:
            # The type only: a driver error can carry the host and the user.
            logger.error(
                "persistence configured but unavailable (%s); "
                "analyses will be measured but not saved", type(error).__name__,
            )
    else:
        logger.info("%s", settings.summary)
    yield
    watchdog = diagnostics.watchdog()
    if watchdog and watchdog.stalls:
        logger.info("thread stalls over this process's life: %s", watchdog.summary())
    diagnostics.stop_watchdog()
    if settings.enabled:
        from backend.db import pool as db_pool

        db_pool.close()
    # Analysis processes first: each empties its own store as it stops.
    from backend.supervisor import HOSTS

    HOSTS.shutdown()
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
async def health() -> Any:
    """The same report as ``/api/health``, at the path a platform expects.

    Render, and most other hosts, want a health check at the root. Rather than
    two implementations that can disagree, this is the same function — awaited, and
    async itself, because this is the path the platform actually polls and it must
    be served from the event loop rather than the threadpool the analysis starves.
    """
    return await api_health()


#: Cache-busting token for the page's own assets, computed once at start from the
#: bytes actually being served. A browser keeps /static/app.js across a deploy
#: otherwise, so a fixed bug keeps being reported from a cached copy — which is
#: exactly what happened with the fetch error handler.
def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        path = os.path.join(FRONTEND_DIR, name)
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:12]


_ASSET_VERSION = _asset_version()


@app.get("/", include_in_schema=False)
def viewer() -> Any:
    """Serve the analysis application, with its assets versioned.

    The version is a hash of the assets themselves, so it changes exactly when
    they do: a deploy invalidates the browser's copy, and an unchanged deploy does
    not.
    """
    if os.path.exists(APP_HTML):
        with open(APP_HTML, encoding="utf-8") as handle:
            page = handle.read()
        page = page.replace('"/static/app.js"', f'"/static/app.js?v={_ASSET_VERSION}"')
        page = page.replace('"/static/styles.css"', f'"/static/styles.css?v={_ASSET_VERSION}"')
        return Response(content=page, media_type="text/html")
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
