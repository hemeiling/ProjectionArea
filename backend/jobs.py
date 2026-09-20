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

from backend.progress import ProgressTracker

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
    #: Progress reporting for this job, when the work is instrumented.
    #: Set when the work has a stage plan; drives the progress bar.
    tracker: Optional[ProgressTracker] = None
    #: Identity of what was uploaded. Kept so a save can be retried from the
    #: server's own copy of the result rather than from numbers a browser sends
    #: back (§22: the backend is authoritative for every engineering value).
    source_sha256: str = ""
    source_size_bytes: int = 0
    #: The stage that was in flight when the job failed, so the bar can keep the
    #: progress already earned and say where it stopped.
    failed_stage: Optional[str] = None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "job_id": self.id,
            "file_name": self.file_name,
            "state": self.state,
            "stage": self.stage,
            "detail": self.detail,
            "stages_done": list(self.stages_done),
            "document_id": self.document_id,
            "result": self.result,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "elapsed_seconds": round(self.elapsed, 1),
        }
        if self.tracker is not None:
            snapshot = self.tracker.snapshot()
            # A finished job is at 100 %; a failed one keeps whatever it earned,
            # so the bar shows how far it got rather than resetting.
            if self.state == "done":
                snapshot["progress"] = 1.0
                snapshot["completed_stages"] = snapshot["total_stages"]
            payload.update(snapshot)
            payload["elapsed_seconds"] = round(self.elapsed, 1)
        return payload


class JobStore:
    """Thread-safe register of running and recently finished jobs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}

    def start(
        self,
        file_name: str,
        work: Callable[["Job"], Dict[str, Any]],
        tracker: Optional[ProgressTracker] = None,
    ) -> Job:
        """Run ``work`` on a worker thread, reporting progress through the job.

        Args:
            file_name: What the user uploaded, for the progress display.
            work: Called with the job; should call :meth:`Job.advance` as it goes
                and return the payload the client will collect.
            tracker: Attached before the thread starts, so the worker never has
                to wait for it and the client's first poll already knows the
                stage plan.
        """
        self.sweep()
        job = Job(id=uuid.uuid4().hex[:16], file_name=file_name, tracker=tracker)
        with self._lock:
            self._jobs[job.id] = job

        def run() -> None:
            try:
                job.result = work(job)
                job.state = "done"
                job.stage = "complete"
            except Exception as error:  # surfaced to the client, not swallowed
                job.state = "failed"
                job.failed_stage = job.tracker.current_key if job.tracker else job.stage
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
            "fix": error.fix,
            "advice": error.case,
            "setup_command": error.setup_command,
            "component": error.component,
        }
    if isinstance(error, DwgConversionFailed):
        return {
            "kind": "conversion_failed",
            "headline": "The drawing could not be converted.",
            "reason": str(error),
            "fix": "Check the file opens in AutoCAD, or export a DXF and upload that.",
        }
    if isinstance(error, MemoryError):
        # Where Python sees the allocation fail rather than the kernel killing the
        # process outright, say plainly what happened. A production DWG needs
        # gigabytes to read: this is a sizing fact about the instance, not a fault
        # in the drawing, and blaming the drawing would send the user looking in
        # the wrong place (§31).
        return {
            "kind": "out_of_memory",
            "headline": "This instance ran out of memory reading the drawing.",
            "reason": (
                "The drawing is larger than this instance can hold. Reading a "
                "production DWG of two million entities needs several gigabytes, "
                "and nothing was measured, so no partial result is reported."
            ),
            "fix": (
                "Run this on an instance with more memory, or upload a smaller "
                "export — a single view, or a DXF of the relevant layers."
            ),
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
