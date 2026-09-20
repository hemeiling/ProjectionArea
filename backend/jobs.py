"""Background ingestion for sources that take minutes rather than seconds.

CONSTITUTION.md §27 (the right amount of infrastructure) and §31 (say what is
happening). A PDF is ingested in under a second and can stay synchronous. A
production DWG cannot: the largest of the real drawings converts in 5 s and then
takes four minutes to parse 2.4 million entities out of a 381 MB intermediate.
Holding an HTTP request open for that, with the browser showing nothing, is not
a workable product.

So a slow ingest becomes a job: the upload returns immediately with an id, a
worker thread does the work, and the client polls for the stage it has reached.
This is a dictionary and a thread — not a queue, a broker or a task framework —
because one desktop-style process is what this tool is (§27).
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: Finished jobs are kept this long so a slow client can still collect them.
JOB_TTL_SECONDS = 1800


@dataclass
class Job:
    """One ingestion in progress, and what it has managed so far."""

    id: str
    file_name: str
    state: str = "running"          # running | done | failed
    stage: str = "queued"           # machine-readable stage key
    detail: str = ""                # human-readable note for that stage
    stages_done: List[Dict[str, str]] = field(default_factory=list)
    document_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "file_name": self.file_name,
            "state": self.state,
            "stage": self.stage,
            "detail": self.detail,
            "stages_done": list(self.stages_done),
            "document_id": self.document_id,
            "result": self.result,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed, 1),
        }


class JobStore:
    """Thread-safe register of running and recently finished jobs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}

    def start(self, file_name: str, work: Callable[["Job"], Dict[str, Any]]) -> Job:
        """Run ``work`` on a worker thread, reporting progress through the job.

        Args:
            file_name: What the user uploaded, for the progress display.
            work: Called with the job; should call :meth:`Job.advance` as it goes
                and return the payload the client will collect.
        """
        self.sweep()
        job = Job(id=uuid.uuid4().hex[:16], file_name=file_name)
        with self._lock:
            self._jobs[job.id] = job

        def run() -> None:
            try:
                job.result = work(job)
                job.state = "done"
                job.stage = "complete"
            except Exception as error:  # surfaced to the client, not swallowed
                job.state = "failed"
                job.error = _describe(error)
                job.detail = job.error.get("headline", str(error))
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, name=f"ingest-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

    def sweep(self) -> int:
        cutoff = time.time() - JOB_TTL_SECONDS
        with self._lock:
            stale = [
                key for key, job in self._jobs.items()
                if job.finished_at is not None and job.finished_at < cutoff
            ]
            for key in stale:
                del self._jobs[key]
        return len(stale)


def advance(job: Job, stage: str, detail: str = "") -> None:
    """Record that a job has reached a stage. Safe to call from the worker."""
    if job.stage and job.stage not in ("queued", stage):
        job.stages_done.append({"stage": job.stage, "detail": job.detail})
    job.stage = stage
    job.detail = detail


def _describe(error: Exception) -> Dict[str, Any]:
    """Turn an exception into the actionable shape the UI already renders."""
    from backend.cad.dwg import DwgConversionFailed, DwgConversionUnavailable

    if isinstance(error, DwgConversionUnavailable):
        return {
            "kind": "dwg_component_missing",
            "headline": "DWG support requires the local CAD conversion component.",
            "reason": str(error),
            "fix": f"Run: {error.setup_command}",
            "component": error.component,
        }
    if isinstance(error, DwgConversionFailed):
        return {
            "kind": "conversion_failed",
            "headline": "The drawing could not be converted.",
            "reason": str(error),
            "fix": "Check the file opens in AutoCAD, or export a DXF and upload that.",
        }
    if isinstance(error, ValueError):
        text = str(error)
        if "no readable geometry" in text:
            # Conversion worked; the drawing itself has nothing to measure. That
            # is a different thing from a failed conversion, and the user needs
            # to know which so they look in the right place.
            return {
                "kind": "empty_drawing",
                "headline": "The drawing contains no measurable geometry.",
                "reason": text,
                "fix": (
                    "Check the drawing has content in model space rather than only "
                    "in a paper-space layout."
                ),
            }
        return {"kind": "unreadable", "headline": "The drawing could not be read.",
                "reason": text}
    return {
        "kind": "error",
        "headline": "Processing failed.",
        "reason": f"{type(error).__name__}: {error}",
        "trace": traceback.format_exc(limit=4),
    }


JOBS = JobStore()
