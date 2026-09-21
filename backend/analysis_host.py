"""The analysis child process: where every heavy measurement runs.

Why a separate process at all. The hosted 102 run on 7b2fbbc was restarted at
~80 % with no deployment in progress. Immediately before, the event loop that
serves ``/health`` and ``/api/jobs`` had been blocked for 9.8, 10.4, 13.0, 13.8
and finally 17.5 seconds: reading and normalising two million CAD entities is
Python-level work that holds the GIL, and a thread cannot give up a lock it does
not know anyone is waiting for. No amount of tuning inside one process fixes that,
because the web server and the analysis share one interpreter.

A child process has its own interpreter, GIL and address space. Whatever it does,
the parent keeps answering. And when it ends, the parent sees *how* it ended —
exit code 0, a Python exception it reported, ``SIGKILL``, ``SIGSEGV`` — which is
the evidence the previous restarts never left behind.

What runs here is the existing pipeline, called through the existing functions in
:mod:`backend.api.routes`, unchanged: nothing is re-implemented for the child, so
nothing can measure differently in it. The child also *keeps* the document it
measured. Calibrating or changing a reading re-runs the footprint geometry, which
for a production DWG is as heavy as the first pass, so those requests are sent
here too rather than rebuilding the drawing in the parent.

The same :func:`dispatch` runs in-process when isolation is ``inline`` — for tests
that need to reach inside the pipeline — so there is one code path, with two ways
of carrying its messages.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

# Imported at module level on purpose: the forkserver preloads this module, so the
# heavy libraries (PyMuPDF, Shapely, ezdxf, OpenCV) are imported once and every
# child starts already holding them, in milliseconds rather than seconds.
from fastapi import HTTPException

from backend import diagnostics
from backend.api import routes
from backend.api.schemas import AreaRequest
from backend.progress import Progress
from backend.store import STORE

logger = logging.getLogger("projected_area.worker")

Emit = Callable[[tuple], None]

#: The least time between two relayed ``advance`` events. Stage boundaries are
#: always sent; counts inside a stage are sampled, because the DXF reader reports
#: every few thousand entities and the parent needs a moving bar, not every tick.
ADVANCE_RELAY_SECONDS = 0.2

#: How often the child reports its memory to the parent while it is alive.
MEMORY_RELAY_SECONDS = 1.0


class RelayProgress(Progress):
    """A progress sink that forwards stage events to the parent's tracker.

    The parent replays them on the job's own :class:`ProgressTracker`, so the
    browser sees exactly the progress it saw when the work ran in a thread.
    """

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._last_advance = 0.0

    def begin(self, key: str, detail: str = "") -> None:
        self._emit(("progress", "begin", (key, detail)))

    def advance(self, current: int, total: Optional[int] = None, detail: str = "") -> None:
        now = time.monotonic()
        if now - self._last_advance < ADVANCE_RELAY_SECONDS:
            return
        self._last_advance = now
        self._emit(("progress", "advance", (current, total, detail)))

    def finish(self, key: str, detail: str = "") -> None:
        self._emit(("progress", "finish", (key, detail)))

    def complete(self) -> None:
        self._emit(("progress", "complete", ()))


def _selftest(args: Dict[str, Any], emit: Emit) -> Dict[str, Any]:
    """Deliberate failure modes, so the supervisor's handling of each is tested
    against a real process rather than a mock. Reachable only from the parent's own
    code — there is no HTTP route to it."""
    import signal

    action = args.get("action")
    if action == "spin":
        # Pure-Python work that holds this process's GIL, as ezdxf does.
        started = time.time()
        deadline = time.monotonic() + float(args.get("seconds", 1.0))
        progress = RelayProgress(emit)
        progress.begin(args.get("stage", "geometry"))
        count = 0
        while time.monotonic() < deadline:
            count += sum(i * i for i in range(2000))
        progress.finish(args.get("stage", "geometry"), "spun")
        return {"spun": True, "started": started, "ended": time.time()}
    if action == "raise":
        raise ValueError("selftest: a controlled failure inside the analysis")
    if action == "segv":
        os.kill(os.getpid(), signal.SIGSEGV)
    if action == "exit":
        os._exit(int(args.get("code", 3)))
    if action == "stage":
        with diagnostics.stage("selftest.stage", items=3):
            block = bytearray(int(args.get("bytes", 50_000_000)))
            time.sleep(float(args.get("hold", 0.6)))
            del block
        return {"staged": True}
    raise ValueError(f"unknown selftest action {action!r}")


def dispatch(command: str, args: Dict[str, Any], emit: Emit) -> Any:
    """Run one command against this process's document store.

    Every command is a call into the existing route implementation. Raises
    whatever that raises; the caller decides how to carry it back.
    """
    if command == "analyse":
        return routes._compute_analysis(
            args["spooled"], args["file_name"], args["kind"],
            source_sha256=args.get("source_sha256", ""),
            source_size=args.get("source_size", 0),
            progress=RelayProgress(emit),
            mark=lambda stage, detail="": emit(("mark", stage, detail)),
        )
    if command == "ingest":
        return routes._ingest(
            args["spooled"], args["file_name"], args["kind"],
            mark=lambda stage, detail="": emit(("mark", stage, detail)),
        )
    if command == "analyze":
        return routes._analyze_local(args["document_id"], args["page_number"])
    if command == "geometry":
        return routes._geometry_local(
            args["document_id"], args["page_number"],
            roles=args.get("roles"), max_primitives=args.get("max_primitives", 20000))
    if command == "area":
        _stored, body = routes._area_local(
            args["document_id"], args["page_number"], AreaRequest(**args["request"]))
        return body
    if command == "remove":
        return STORE.remove(args["document_id"])
    if command == "selftest":
        return _selftest(args, emit)
    raise ValueError(f"unknown command {command!r}")


def _describe(error: BaseException) -> Dict[str, Any]:
    from backend.jobs import _describe as describe

    try:
        return describe(error)  # type: ignore[arg-type]
    except Exception:
        return {"kind": "error", "headline": "Processing failed.",
                "error_type": type(error).__name__}


def child_main(conn: Any, environ: Dict[str, str]) -> None:
    """Entry point of the child. Serves commands until told to stop.

    Args:
        conn: This end of the pipe to the parent.
        environ: The parent's environment at the moment the child was started. A
            forkserver's own environment is fixed when it starts, so without this
            a setting changed afterwards would silently not reach the analysis.
    """
    os.environ.clear()
    os.environ.update(environ)
    logging.basicConfig(
        level=os.environ.get("PROJECTED_AREA_LOG", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    # A directory of this child's own, marked with this child's pid: if it is
    # killed, the parent's orphan sweep recognises the files as abandoned and
    # deletes them — they are customer drawings (§35).
    STORE.reinitialise()

    send_lock = threading.Lock()

    def emit(message: tuple) -> None:
        with send_lock:
            conn.send(message)

    def safe_emit(message: tuple) -> None:
        # Observational messages: losing one must never cost the analysis.
        try:
            emit(message)
        except Exception:
            pass

    diagnostics.set_reporter(lambda record: safe_emit(("diag", record)))
    last_memory = [0.0]

    def relay_memory(snapshot: Dict[str, Any]) -> None:
        now = time.monotonic()
        if now - last_memory[0] >= MEMORY_RELAY_SECONDS:
            last_memory[0] = now
            safe_emit(("mem", snapshot))

    diagnostics.SAMPLER.listen(relay_memory)
    logger.info("analysis worker ready")

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            break  # the parent is gone; nothing left to serve
        if message[0] == "shutdown":
            break
        _tag, request_id, command, args = message
        try:
            value = dispatch(command, args, emit)
            reply = ("reply", request_id, "ok", value)
        except HTTPException as error:
            reply = ("reply", request_id, "http",
                     {"status_code": error.status_code, "detail": error.detail})
        except Exception as error:
            reply = ("reply", request_id, "error", _describe(error))
        try:
            emit(reply)
        except Exception as error:  # an unpicklable value, most likely
            emit(("reply", request_id, "error", {
                "kind": "error", "headline": "The analysis result could not be returned.",
                "error_type": type(error).__name__}))

    STORE.shutdown()
