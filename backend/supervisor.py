"""The parent's side of the analysis child: start it, talk to it, see how it ended.

See :mod:`backend.analysis_host` for why the measurement runs in another process.
This module is what the web server holds instead of the measurement: a handle per
child ("host"), a register of which host holds which document, and a single slot
that lets one heavy analysis run at a time.

No broker, no queue service. One analysis at a time is this service's contract,
and a pipe to one supervised process is all that contract needs (§27).

How a child ended is read from the operating system, not guessed:

======================  =====================================================
``exitcode == 0``       it stopped normally (after being asked to)
Python exception        reported by the child over the pipe; it stays alive
``exitcode > 0``        the interpreter exited with an error status
``-SIGKILL``            killed from outside: an out-of-memory kill looks like
                        this, but so does any other SIGKILL
``-SIGSEGV`` et al.     a native crash inside a C/C++ library
timeout                 the parent gave up and killed it; recorded as such
======================  =====================================================
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing
import os
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from backend import runtime

logger = logging.getLogger("projected_area.supervisor")

Sink = Callable[[tuple], None]

#: Signals that mean a native library crashed, as opposed to being killed.
_CRASH_SIGNALS = {"SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE", "SIGABRT"}


class WorkerFailed(Exception):
    """The child could not deliver an answer. ``outcome`` says why, from evidence."""

    def __init__(self, outcome: Dict[str, Any]) -> None:
        super().__init__(outcome.get("headline", "The analysis process failed."))
        self.outcome = outcome


class WorkerError(Exception):
    """The child raised a Python exception and reported it. The child is alive."""

    def __init__(self, described: Dict[str, Any]) -> None:
        super().__init__(described.get("headline", "Processing failed."))
        self.described = described


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def classify_exit(exitcode: Optional[int], reason: Optional[str],
                  evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Describe how a child ended, saying only what the exit status supports.

    ``evidence`` is what the child last reported — the stage it was in and its
    memory — so a ``SIGKILL`` can be read alongside how close the container was to
    its limit, without the message itself deciding that it was memory.
    """
    base: Dict[str, Any] = {"exit_code": exitcode, "signal": None, **evidence}
    if reason == "timeout":
        return {**base, "kind": "worker_timeout",
                "headline": "The analysis did not finish within the time allowed.",
                "reason": (
                    f"The analysis process was still working after "
                    f"{runtime.analysis_timeout_seconds():.0f} seconds and was stopped "
                    "by the server. Nothing was measured, so no partial result is "
                    "reported."),
                "fix": "Try a smaller export of the drawing, or raise ANALYSIS_TIMEOUT_SECONDS."}
    if exitcode is None:
        return {**base, "kind": "worker_lost",
                "headline": "The analysis process stopped responding.",
                "reason": "The connection to the analysis process closed without an exit status.",
                "fix": "Upload the drawing again."}
    if exitcode < 0:
        name = _signal_name(-exitcode)
        base["signal"] = name
        if name == "SIGKILL":
            return {**base, "kind": "worker_killed",
                    "headline": "The analysis process was killed (SIGKILL).",
                    "reason": (
                        "The analysis process was terminated from outside with SIGKILL. "
                        "The kernel's out-of-memory killer ends processes this way, but so "
                        "does any other SIGKILL; compare the memory figures recorded with "
                        "this error against the container limit before concluding which."),
                    "fix": "Upload the drawing again. If it recurs at the same stage with "
                           "memory near the limit, the instance needs more memory for this drawing."}
        if name in _CRASH_SIGNALS:
            return {**base, "kind": "worker_crashed",
                    "headline": f"The analysis process crashed ({name}).",
                    "reason": (
                        f"A native library inside the analysis process crashed with {name}. "
                        "This is a fault in compiled code (the geometry or CAD libraries), "
                        "not in the drawing's contents as such, and the web server was not affected."),
                    "fix": "Report this with the stage below. Re-exporting the drawing "
                           "to DXF may avoid the code path that crashed."}
        return {**base, "kind": "worker_signal",
                "headline": f"The analysis process was stopped by {name}.",
                "reason": f"The analysis process ended on {name}.",
                "fix": "Upload the drawing again."}
    if exitcode == 0:
        return {**base, "kind": "worker_exited",
                "headline": "The analysis process exited before returning a result.",
                "reason": "The process ended normally but had not answered.",
                "fix": "Upload the drawing again."}
    return {**base, "kind": "worker_failed",
            "headline": f"The analysis process exited with status {exitcode}.",
            "reason": "The analysis process ended with an error status before answering.",
            "fix": "Upload the drawing again. If it recurs, report the stage below."}


class _Pending:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.status: Optional[str] = None
        self.value: Any = None


class ProcessHost:
    """One analysis child and the documents it holds."""

    isolation = "process"
    _ids = itertools.count(1)
    _context: Optional[Any] = None
    _context_lock = threading.Lock()

    @classmethod
    def context(cls) -> Any:
        """A forkserver context whose server has the pipeline already imported.

        ``fork`` from a threaded web server is unsafe — a lock held by another
        thread at the moment of the fork stays held forever in the child. The
        forkserver is a clean, single-threaded process started once; children are
        forked from it, with the heavy imports already done.
        """
        with cls._context_lock:
            if cls._context is None:
                methods = multiprocessing.get_all_start_methods()
                ctx = multiprocessing.get_context(
                    "forkserver" if "forkserver" in methods else "spawn")
                if ctx.get_start_method() == "forkserver":
                    ctx.set_forkserver_preload(["backend.analysis_host"])
                cls._context = ctx
            return cls._context

    def __init__(self) -> None:
        from backend import analysis_host

        ctx = self.context()
        parent_end, child_end = ctx.Pipe(duplex=True)
        self._conn = parent_end
        self.process = ctx.Process(
            target=analysis_host.child_main, args=(child_end, dict(os.environ)),
            name="pa-analysis", daemon=True)
        self.process.start()
        child_end.close()
        self.label = f"host-{next(self._ids)}"
        self.documents: Dict[str, Dict[str, Any]] = {}
        self.last_used = time.time()
        self.sink: Optional[Sink] = None
        self.evidence: Dict[str, Any] = {}
        self.outcome: Optional[Dict[str, Any]] = None
        self._request_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}
        self._next_request = itertools.count(1)
        self._kill_reason: Optional[str] = None
        self._reader = threading.Thread(
            target=self._read, name=f"pa-{self.label}-reader", daemon=True)
        self._reader.start()
        logger.info("%s started", self.label)

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.outcome is None and self.process.is_alive()

    def _note_evidence(self, message: tuple) -> None:
        kind = message[0]
        if kind == "diag":
            record = message[1] or {}
            self.evidence["last_stage"] = record.get("stage")
            self.evidence["last_stage_state"] = record.get("state")
        elif kind == "mem":
            snap = message[1] or {}
            self.evidence["memory_scope"] = snap.get("scope")
            self.evidence["memory_used_bytes"] = snap.get("used_bytes")
            self.evidence["memory_limit_bytes"] = snap.get("limit_bytes")
            self.evidence["memory_peak_bytes"] = snap.get("peak_bytes")
        elif kind == "progress" and message[1] == "begin":
            self.evidence["last_progress_stage"] = message[2][0]

    # ── the pipe ─────────────────────────────────────────────────────────────

    def _read(self) -> None:
        while True:
            try:
                message = self._conn.recv()
            except (EOFError, OSError):
                break
            except Exception as error:  # an unreadable message: say so, keep reading
                logger.warning("%s sent an unreadable message: %s",
                               self.label, type(error).__name__)
                continue
            if message[0] == "reply":
                _tag, request_id, status, value = message
                pending = self._pending.pop(request_id, None)
                if pending is not None:
                    pending.status, pending.value = status, value
                    pending.done.set()
                continue
            self._note_evidence(message)
            sink = self.sink
            if sink is not None:
                try:
                    sink(message)
                except Exception as error:  # observational: never fatal
                    logger.warning("%s: progress relay failed: %s",
                                   self.label, type(error).__name__)
        self._on_exit()

    def _on_exit(self) -> None:
        self.process.join(timeout=10)
        exitcode = self.process.exitcode
        if self._kill_reason == "retired":
            self.outcome = {"kind": "retired", "exit_code": exitcode}
        else:
            self.outcome = classify_exit(exitcode, self._kill_reason, dict(self.evidence))
            level = logging.INFO if exitcode == 0 else logging.ERROR
            logger.log(level, "%s ended · %s · exit=%s signal=%s last_stage=%s memory=%s/%s",
                       self.label, self.outcome["kind"], exitcode, self.outcome.get("signal"),
                       self.evidence.get("last_stage") or self.evidence.get("last_progress_stage"),
                       self.evidence.get("memory_used_bytes"),
                       self.evidence.get("memory_limit_bytes"))
        for pending in list(self._pending.values()):
            pending.status, pending.value = "died", self.outcome
            pending.done.set()
        self._pending.clear()
        # A killed child never ran its own cleanup, so its directory — with the
        # customer's drawing in it — is removed here (§35).
        try:
            from backend.store import sweep_orphaned_stores

            sweep_orphaned_stores()
        except Exception:
            pass

    def request(self, command: str, args: Dict[str, Any], sink: Optional[Sink] = None,
                timeout: Optional[float] = None) -> Any:
        """Run one command in the child and return its value.

        Raises:
            WorkerError: the child raised; it described the exception.
            HTTPException: the route code in the child refused the request.
            WorkerFailed: the child died, or did not answer in time.
        """
        timeout = runtime.analysis_timeout_seconds() if timeout is None else timeout
        with self._request_lock:
            if not self.alive:
                raise WorkerFailed(self.outcome or classify_exit(
                    self.process.exitcode, None, dict(self.evidence)))
            self.last_used = time.time()
            self.sink = sink
            request_id = next(self._next_request)
            pending = _Pending()
            self._pending[request_id] = pending
            try:
                with self._send_lock:
                    self._conn.send(("call", request_id, command, args))
                if not pending.done.wait(timeout):
                    self.kill("timeout")
                    pending.done.wait(15)
                    if pending.status is None:
                        raise WorkerFailed(classify_exit(None, "timeout", dict(self.evidence)))
            finally:
                self.sink = None
                self.last_used = time.time()
        if pending.status == "ok":
            return pending.value
        if pending.status == "http":
            raise HTTPException(status_code=pending.value["status_code"],
                                detail=pending.value["detail"])
        if pending.status == "error":
            raise WorkerError(pending.value)
        raise WorkerFailed(pending.value or {"kind": "worker_lost"})

    # ── ending ───────────────────────────────────────────────────────────────

    def kill(self, reason: str) -> None:
        """Stop the child now. ``reason`` is recorded, so a timeout reads as one."""
        self._kill_reason = reason
        try:
            self.process.kill()
        except Exception:
            pass

    def retire(self) -> None:
        """Ask the child to clean up and stop; insist if it does not."""
        if not self.process.is_alive():
            return
        self._kill_reason = "retired"
        try:
            with self._send_lock:
                self._conn.send(("shutdown",))
        except Exception:
            pass
        self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=5)


class InlineHost:
    """The same commands, run in this process. For tests that patch the pipeline,
    and for a platform where child processes are unavailable. Documents stay in the
    parent's own store, exactly as before the child existed."""

    isolation = "inline"
    alive = True
    outcome = None
    evidence: Dict[str, Any] = {}

    def __init__(self) -> None:
        self.documents: Dict[str, Dict[str, Any]] = {}
        self.last_used = time.time()

    def request(self, command: str, args: Dict[str, Any], sink: Optional[Sink] = None,
                timeout: Optional[float] = None) -> Any:
        from backend import analysis_host, diagnostics

        emit = sink or (lambda message: None)

        def safe(message: tuple) -> None:
            try:
                emit(message)
            except Exception:
                pass

        previous = diagnostics._reporter
        diagnostics.set_reporter(lambda record: safe(("diag", record)))
        try:
            return analysis_host.dispatch(command, args, emit)
        except (HTTPException, WorkerFailed):
            raise
        except Exception as error:
            raise WorkerError(analysis_host._describe(error)) from error
        finally:
            diagnostics.set_reporter(previous)

    def retire(self) -> None:
        pass

    def kill(self, reason: str) -> None:
        pass


class HostRegistry:
    """Which host holds which document, and the one-analysis-at-a-time slot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hosts: List[Any] = []
        self._by_document: Dict[str, Any] = {}
        #: One heavy analysis at a time. A second waits here, visibly queued.
        self.slot = threading.BoundedSemaphore(1)

    @staticmethod
    def isolation() -> str:
        return runtime.analysis_isolation()

    def create(self) -> Any:
        """A new host, after retiring the oldest ones beyond the retention limit.

        Retiring *before* starting keeps two production drawings from sitting in
        memory together: the finished one's host is stopped before the next begins.
        """
        if self.isolation() == "inline":
            return InlineHost()
        keep = max(0, runtime.max_document_hosts() - 1)
        with self._lock:
            live = [h for h in self._hosts if h.alive]
            excess = live[:max(0, len(live) - keep)]
        for host in excess:
            self.discard(host)
        host = ProcessHost()
        with self._lock:
            self._hosts.append(host)
        return host

    def bind(self, document_id: str, host: Any, source_sha256: str = "") -> None:
        if isinstance(host, InlineHost):
            return  # the parent's own store already holds it
        with self._lock:
            host.documents[document_id] = {"source_sha256": source_sha256}
            self._by_document[document_id] = host

    def for_document(self, document_id: str) -> Optional[Any]:
        with self._lock:
            host = self._by_document.get(document_id)
        if host is None:
            return None
        if not host.alive:
            self.discard(host, wait=False)
            raise HTTPException(status_code=410, detail={
                "kind": "document_host_gone",
                "headline": "The analysis process holding this drawing has stopped.",
                "reason": (host.outcome or {}).get("headline")
                or "The process that measured this drawing is no longer running.",
                "fix": "Open the analysis from History, or upload the drawing again.",
            })
        return host

    def discard(self, host: Any, wait: bool = True) -> None:
        """Forget a host and stop it. ``wait=False`` stops it in the background,
        for callers on the event loop that must not wait for a process to exit."""
        with self._lock:
            if host in self._hosts:
                self._hosts.remove(host)
            for document_id in list(host.documents):
                self._by_document.pop(document_id, None)

        def retire() -> None:
            try:
                host.retire()
            except Exception as error:
                logger.warning("could not retire %s: %s",
                               getattr(host, "label", "host"), type(error).__name__)

        if wait:
            retire()
        else:
            threading.Thread(target=retire, name="pa-retire", daemon=True).start()

    def remove_document(self, document_id: str) -> Optional[bool]:
        """Delete a hosted document. ``None`` when no host holds it."""
        with self._lock:
            host = self._by_document.get(document_id)
        if host is None:
            return None
        with self._lock:
            host.documents.pop(document_id, None)
            self._by_document.pop(document_id, None)
            empty = not host.documents
        if empty:
            self.discard(host)  # its store is emptied as it stops
            return True
        try:
            return bool(host.request("remove", {"document_id": document_id}))
        except Exception:
            return True

    def sweep(self, idle_seconds: float) -> int:
        """Retire hosts idle for longer than ``idle_seconds``, and dead ones."""
        cutoff = time.time() - idle_seconds
        with self._lock:
            stale = [h for h in self._hosts if not h.alive or h.last_used < cutoff]
        for host in stale:
            self.discard(host, wait=False)
        return len(stale)

    def busy(self) -> bool:
        acquired = self.slot.acquire(blocking=False)
        if acquired:
            self.slot.release()
        return not acquired

    def count(self) -> int:
        with self._lock:
            return sum(1 for h in self._hosts if h.alive)

    def shutdown(self) -> None:
        with self._lock:
            hosts = list(self._hosts)
        for host in hosts:
            self.discard(host)


HOSTS = HostRegistry()
