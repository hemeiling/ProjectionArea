"""What the process was doing and what it cost, at each stage.

CONSTITUTION.md §32 (log stages and durations, never content) and §31 (errors that
explain). Written for a failure that could not be diagnosed from outside: a hosted
run of a production DWG reached about 70 % of geometry extraction, then the
instance returned 502 and was restarted, with memory telemetry showing 1.45 GB
against a 16 GB limit thirty seconds earlier.

Two candidate causes fit that shape, and they need different fixes:

* **a memory spike** faster than the platform's 30-second sampling, or
* **the process ceasing to answer HTTP**, so the platform's health check failed
  and restarted a perfectly healthy instance.

So this records both: memory around every stage, and how long the event loop was
blocked. The second is the one no external telemetry can see.

Nothing here records drawing content. Counts, sizes, durations and exception types
only — a stage log that leaked geometry would be a §35 problem of its own.

No new dependency: ``/proc`` on Linux, ``resource`` elsewhere. Adding psutil to
read four numbers would be the wrong trade for a container this small.
"""

from __future__ import annotations

import logging
import os
import resource
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("projected_area.diag")

#: How long the event loop may stall before it is worth a line in the log. A
#: platform health check usually allows a few seconds; anything past this is
#: heading towards a restart.
LOOP_STALL_WARN_SECONDS = 2.0

#: How often the watchdog checks. Short enough to catch a stall that matters,
#: cheap enough to leave running in production.
LOOP_TICK_SECONDS = 0.5


def _linux_status() -> Dict[str, int]:
    """``VmRSS``, ``VmSize`` and ``VmPeak`` in bytes, on Linux."""
    out: Dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(("VmRSS:", "VmSize:", "VmPeak:", "VmHWM:")):
                    key, value = line.split(":", 1)
                    parts = value.split()
                    if parts and parts[0].isdigit():
                        out[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return out


def _cgroup_memory() -> Dict[str, Optional[int]]:
    """Container memory usage and limit, in bytes, if the kernel exposes them.

    This is what the *platform* is measuring when it decides to kill a container,
    and it can differ from the process's own RSS — page cache and any child
    process count against it too. A container killed while the Python process
    looks small is exactly the case worth being able to see.
    """
    pairs = (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),          # v2
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),                          # v1
    )
    for usage_path, limit_path in pairs:
        try:
            with open(usage_path, encoding="ascii") as handle:
                usage = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        limit: Optional[int] = None
        try:
            with open(limit_path, encoding="ascii") as handle:
                raw = handle.read().strip()
            limit = None if raw == "max" else int(raw)
            # cgroup v1 reports "no limit" as a number near 2^63.
            if limit is not None and limit > (1 << 62):
                limit = None
        except (OSError, ValueError):
            limit = None
        return {"container_used": usage, "container_limit": limit}
    return {"container_used": None, "container_limit": None}


def _safe_memory() -> Dict[str, Any]:
    """:func:`memory`, or an empty snapshot if reading it fails. Never raises."""
    try:
        return memory()
    except Exception:
        return {}


def memory() -> Dict[str, Any]:
    """A memory snapshot: this process, its children, and the container.

    Peak values come from ``getrusage`` and never decrease, which is what makes
    them useful after the fact: a spike between two samples still shows up.
    """
    snapshot: Dict[str, Any] = {"pid": os.getpid()}

    scale = 1 if sys.platform == "darwin" else 1024
    snapshot["peak_rss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    children_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
    snapshot["children_peak_rss"] = children_peak or None

    status = _linux_status()
    if status:
        snapshot["rss"] = status.get("VmRSS:")
        snapshot["vms"] = status.get("VmSize:")
        # VmHWM is this process's own high-water mark, which getrusage also gives;
        # keeping both makes a disagreement visible rather than silent.
        snapshot["peak_rss"] = status.get("VmHWM:", snapshot["peak_rss"])
    else:
        # No /proc (macOS): the kernel's task counters give the current figure
        # without shelling out, which on a busy box is not worth a child process.
        snapshot["rss"] = current_rss()
        snapshot["vms"] = None

    snapshot.update(_cgroup_memory())
    return snapshot


def _mb(value: Optional[int]) -> str:
    return f"{value / 1e6:.0f}MB" if value else "-"


def format_memory(snapshot: Dict[str, Any]) -> str:
    """One compact line: what this process holds and what the container holds."""
    parts = [f"rss={_mb(snapshot.get('rss'))}", f"peak={_mb(snapshot.get('peak_rss'))}"]
    if snapshot.get("vms"):
        parts.append(f"vms={_mb(snapshot['vms'])}")
    if snapshot.get("children_peak_rss"):
        parts.append(f"child_peak={_mb(snapshot['children_peak_rss'])}")
    if snapshot.get("container_used"):
        limit = snapshot.get("container_limit")
        share = (f"/{_mb(limit)}" if limit else "")
        parts.append(f"container={_mb(snapshot['container_used'])}{share}")
    return " ".join(parts)


def _log(level: int, message: str, name: str, snapshot: Dict[str, Any],
         facts: Dict[str, Any]) -> None:
    try:
        logger.log(level, message, name, format_memory(snapshot), _facts(facts))
    except Exception:  # a log line must not become the failure
        pass


@contextmanager
def stage(name: str, **facts: Any) -> Iterator[Dict[str, Any]]:
    """Mark the start and end of an expensive stage, with its cost.

    Logs before and after, so a stage that never finishes is identifiable by the
    absence of its closing line — which is precisely what a killed container
    leaves behind.

    Yields a dict the caller can add counts to; they are logged at the end, so
    "how many segments were we holding when it died" has an answer.

    The stage's *own* peak comes from the sampler, not from the process peak: a
    process high-water mark only ever rises, so after one expensive stage every
    later stage would appear to cost as much. Each stage is also reported to the
    registered reporter, which is how the numbers reach the job and the browser.

    Args:
        name: Stage identifier, matching the progress plan where there is one.
        **facts: Anything known up front — counts, sizes. Never drawing content.
    """
    facts_out: Dict[str, Any] = dict(facts)
    started = time.perf_counter()
    before = _safe_memory()
    token = _sampler_open()
    _log(logging.INFO, "stage %s begin · %s%s", name, before, facts_out)
    _report(lambda: _stage_record(name, "running", 0.0, before, None, None, facts_out))

    try:
        yield facts_out
    except BaseException as error:
        elapsed = time.perf_counter() - started
        after = _safe_memory()
        peaks = _sampler_close(token)
        # The type, not the message: an exception from a CAD library can quote
        # file contents, and this line goes to a platform's log collector.
        try:
            logger.error(
                "stage %s FAILED after %.1fs · %s · %s%s",
                name, elapsed, type(error).__name__, format_memory(after),
                _facts(facts_out),
            )
        except Exception:
            pass
        error_type = type(error).__name__
        _report(lambda: {**_stage_record(name, "failed", elapsed, before, after, peaks, facts_out),
                         "error_type": error_type})
        raise
    else:
        elapsed = time.perf_counter() - started
        after = _safe_memory()
        peaks = _sampler_close(token)
        try:
            logger.info(
                "stage %s end %.1fs · %s%s%s",
                name, elapsed, format_memory(after),
                f" stage_peak={_mb(peaks.get('rss'))}" if peaks and peaks.get("rss") else "",
                _facts(facts_out),
            )
        except Exception:
            pass
        _report(lambda: _stage_record(name, "done", elapsed, before, after, peaks, facts_out))


def _stage_record(
    name: str, state: str, elapsed: float, before: Dict[str, Any],
    after: Optional[Dict[str, Any]], peaks: Optional[Dict[str, Optional[int]]],
    facts: Dict[str, Any],
) -> Dict[str, Any]:
    """One stage's measurements, in the shape the job and the browser receive.

    Numbers only. No PID, host name or path: this leaves the process (§35).
    """
    def rss(snapshot: Optional[Dict[str, Any]]) -> Optional[int]:
        if not snapshot:
            return None
        return snapshot.get("rss") or None

    end = after or {}
    stage_peak = (peaks or {}).get("rss")
    # A stage too short for the sampler to have visited still has a true lower
    # bound on its peak: the larger of its two ends.
    ends = [v for v in (rss(before), rss(end)) if v]
    if ends:
        stage_peak = max([stage_peak or 0] + ends) or None
    return {
        "stage": name,
        "state": state,
        "elapsed_seconds": round(elapsed, 2),
        "memory_start_bytes": rss(before),
        "memory_end_bytes": rss(end) if after else None,
        "memory_peak_bytes": stage_peak,
        "container_memory_bytes": end.get("container_used") or before.get("container_used"),
        "container_peak_bytes": (peaks or {}).get("container"),
        "container_limit_bytes": end.get("container_limit") or before.get("container_limit"),
        "process_peak_bytes": end.get("peak_rss") or before.get("peak_rss"),
        "facts": {k: v for k, v in facts.items()
                  if isinstance(v, (int, float, str)) and not isinstance(v, bool)},
    }


def _facts(facts: Dict[str, Any]) -> str:
    if not facts:
        return ""
    return " · " + " ".join(f"{key}={value}" for key, value in facts.items() if value is not None)


def facts_of(obj: Any, *names: str) -> Dict[str, Any]:
    """Named attributes of an object, reading absent ones as ``None``.

    For call sites that log counts off a result object. An attribute renamed later
    must cost a missing number in a log line, not a failed analysis — which is
    exactly what happened when one of these read ``hole_count`` instead of
    ``holes``.
    """
    return {name: getattr(obj, name, None) for name in names}


def note(event: str, **facts: Any) -> None:
    """A single marker, for something that is not a span. Never raises."""
    try:
        logger.info("%s · %s%s", event, format_memory(memory()), _facts(facts))
    except Exception:  # a diagnostic must not become the failure
        pass


def subprocess_outcome(name: str, returncode: int, seconds: float, **facts: Any) -> None:
    """Record how a child process ended, including a signal.

    A negative return code is a signal: ``-9`` is ``SIGKILL``, which is what an
    out-of-memory killer leaves behind, and ``-11`` is a segmentation fault. Those
    are different diagnoses with different fixes, and neither is visible from a
    generic "conversion failed".
    """
    if returncode < 0:
        import signal as signal_module

        try:
            label = signal_module.Signals(-returncode).name
        except ValueError:
            label = f"signal {-returncode}"
        logger.error(
            "subprocess %s killed by %s after %.1fs · %s%s",
            name, label, seconds, format_memory(memory()), _facts(facts),
        )
    else:
        logger.info(
            "subprocess %s exited %d after %.1fs · %s%s",
            name, returncode, seconds, format_memory(memory()), _facts(facts),
        )


# ── current memory, cheaply, and per-stage peaks ─────────────────────────────
#
# A stage's peak is what it actually needed; the process's high-water mark is the
# most any stage so far has needed. After `polygonize` reaches 6.7 GB every later
# stage would read 6.7 GB from the high-water mark, which says nothing about them.
# So a sampler reads current memory a few times a second while any stage is open,
# and each stage keeps the largest value seen during its own lifetime.

#: Seconds between samples. Reading two small /proc files costs microseconds;
#: four a second is well below anything measurable against a minutes-long stage.
SAMPLE_SECONDS = 0.25


def _mach_rss() -> Optional[int]:
    """Current resident size on macOS, from ``task_info``. ``None`` if unavailable."""
    import ctypes

    class TimeValue(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]

    class MachTaskBasicInfo(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64), ("user_time", TimeValue),
            ("system_time", TimeValue), ("policy", ctypes.c_int),
            ("suspend_count", ctypes.c_int),
        ]

    libc = ctypes.CDLL(None)
    task = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
    info = MachTaskBasicInfo()
    count = ctypes.c_uint(ctypes.sizeof(info) // 4)
    MACH_TASK_BASIC_INFO = 20
    if libc.task_info(task, MACH_TASK_BASIC_INFO, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return int(info.resident_size) or None


_PAGE_SIZE: Optional[int] = None


def current_rss() -> Optional[int]:
    """This process's resident memory now, in bytes, or ``None``. Never raises."""
    global _PAGE_SIZE
    try:
        if sys.platform.startswith("linux"):
            if _PAGE_SIZE is None:
                _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
            with open("/proc/self/statm", encoding="ascii") as handle:
                return int(handle.read().split()[1]) * _PAGE_SIZE
        if sys.platform == "darwin":
            return _mach_rss()
    except Exception:  # a measurement that fails is unavailable, never an error
        return None
    return None


def ram_snapshot() -> Dict[str, Any]:
    """What the operator sees as "RAM": the container where it is measurable.

    ``scope`` says which: ``container`` is what the platform limits and kills on;
    ``process`` is only this process's resident memory and must not be presented as
    the container's. Values are ``None`` when unavailable, never estimated.
    """
    try:
        container = _cgroup_memory()
    except Exception:
        container = {"container_used": None, "container_limit": None}
    rss = current_rss()
    if container.get("container_used"):
        return {"scope": "container", "used_bytes": container["container_used"],
                "limit_bytes": container.get("container_limit"), "process_bytes": rss}
    return {"scope": "process", "used_bytes": rss, "limit_bytes": None, "process_bytes": rss}


class MemorySampler:
    """Samples memory on a daemon thread; keeps each open stage's maximum.

    Runs only while at least one stage is open, plus whatever a listener asks for.
    Every failure inside it is swallowed: it observes the analysis and must never
    be able to stop it.
    """

    def __init__(self, interval: float = SAMPLE_SECONDS) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._open: Dict[int, Dict[str, Optional[int]]] = {}
        self._next = 0
        self._thread: Optional[threading.Thread] = None
        self._listener: Optional[Any] = None
        self.peak_used: Optional[int] = None      # over the sampler's lifetime
        self.last: Optional[Dict[str, Any]] = None

    def open(self) -> int:
        with self._lock:
            self._next += 1
            token = self._next
            self._open[token] = {"rss": None, "container": None}
        self._ensure_running()
        self.sample()
        return token

    def close(self, token: int) -> Dict[str, Optional[int]]:
        self.sample()
        with self._lock:
            return self._open.pop(token, {"rss": None, "container": None})

    def listen(self, listener: Any) -> None:
        """Call ``listener(snapshot)`` on every sample, and keep sampling."""
        self._listener = listener
        self._ensure_running()

    def sample(self) -> Optional[Dict[str, Any]]:
        try:
            snap = ram_snapshot()
            rss, used = snap.get("process_bytes"), snap.get("used_bytes")
            with self._lock:
                for peaks in self._open.values():
                    if rss and (peaks["rss"] is None or rss > peaks["rss"]):
                        peaks["rss"] = rss
                    if snap["scope"] == "container" and used and (
                            peaks["container"] is None or used > peaks["container"]):
                        peaks["container"] = used
                if used and (self.peak_used is None or used > self.peak_used):
                    self.peak_used = used
            snap["peak_bytes"] = self.peak_used
            self.last = snap
            if self._listener is not None:
                self._listener(snap)
            return snap
        except Exception:
            return None

    def _ensure_running(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="pa-mem-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            time.sleep(self.interval)
            with self._lock:
                idle = not self._open
            if idle and self._listener is None:
                return
            self.sample()


SAMPLER = MemorySampler()


def _sampler_open() -> Optional[int]:
    try:
        return SAMPLER.open()
    except Exception:
        return None


def _sampler_close(token: Optional[int]) -> Optional[Dict[str, Optional[int]]]:
    if token is None:
        return None
    try:
        return SAMPLER.close(token)
    except Exception:
        return None


# ── where stage records go ───────────────────────────────────────────────────
#
# In the analysis child, a reporter sends each record to the parent, which keeps it
# on the job. Anywhere else there is none and records only reach the log.

_reporter: Optional[Any] = None


def set_reporter(reporter: Optional[Any]) -> None:
    global _reporter
    _reporter = reporter


def _report(record: Any) -> None:
    if _reporter is None:
        return
    try:
        if callable(record):
            record = record()
        _reporter(record)
    except Exception:  # a diagnostic must not become the failure
        pass


class EventLoopWatchdog:
    """Reports how far behind the event loop fell, which is what a health check rides.

    The analysis runs on a worker thread, which is supposed to leave the event loop
    free to serve HTTP. It does not: reading two million CAD entities and building
    the segment network is Python-level work that holds the GIL for long stretches,
    so the loop gets scheduled late and requests that should take milliseconds take
    seconds. A platform restarts an instance whose health check stops answering, and
    from outside that is indistinguishable from a crash — the process is alive,
    busy, and well inside its memory limit.

    Measured as an asyncio task rather than a thread, deliberately. A plain thread
    is scheduled promptly under GIL contention and reports nothing wrong; the event
    loop is what starves, and the loop is what the health check depends on. That
    difference is why the first version of this watchdog saw a healthy process while
    an external prober was waiting six seconds for ``/health``.
    """

    def __init__(self, threshold: float = LOOP_STALL_WARN_SECONDS) -> None:
        self.threshold = threshold
        self.worst = 0.0
        self.total_stalled = 0.0
        self.stalls = 0
        self._task: Optional[Any] = None
        self._stopping = False

    async def _watch(self) -> None:
        import asyncio

        while not self._stopping:
            expected = time.monotonic() + LOOP_TICK_SECONDS
            await asyncio.sleep(LOOP_TICK_SECONDS)
            late = time.monotonic() - expected
            if late >= self.threshold:
                self.stalls += 1
                self.total_stalled += late
                self.worst = max(self.worst, late)
                logger.warning(
                    "event loop blocked %.1fs (worst %.1fs, %d stalls) · %s · "
                    "HTTP, including the health check, was not served for that long",
                    late, self.worst, self.stalls, format_memory(memory()),
                )

    def start(self) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a test, or a CLI run. There is nothing to watch.
            return
        self._task = loop.create_task(self._watch(), name="pa-loop-watchdog")

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def summary(self) -> Dict[str, Any]:
        return {"stalls": self.stalls, "worst_stall_seconds": round(self.worst, 2),
                "total_stalled_seconds": round(self.total_stalled, 2)}


_watchdog: Optional[EventLoopWatchdog] = None


def start_watchdog() -> EventLoopWatchdog:
    """Begin watching, once per process. Safe to call without a running loop."""
    global _watchdog
    if _watchdog is None:
        _watchdog = EventLoopWatchdog()
        _watchdog.start()
    return _watchdog


def watchdog() -> Optional[EventLoopWatchdog]:
    return _watchdog


def stop_watchdog() -> None:
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None
