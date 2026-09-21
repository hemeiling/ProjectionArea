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
        # No /proc: the peak is all that is available without shelling out, and
        # shelling out per stage on a busy box is not worth the child process.
        snapshot["rss"] = None
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


@contextmanager
def stage(name: str, **facts: Any) -> Iterator[Dict[str, Any]]:
    """Mark the start and end of an expensive stage, with its cost.

    Logs before and after, so a stage that never finishes is identifiable by the
    absence of its closing line — which is precisely what a killed container
    leaves behind.

    Yields a dict the caller can add counts to; they are logged at the end, so
    "how many segments were we holding when it died" has an answer.

    Args:
        name: Stage identifier, matching the progress plan where there is one.
        **facts: Anything known up front — counts, sizes. Never drawing content.
    """
    facts_out: Dict[str, Any] = dict(facts)
    started = time.perf_counter()
    before = memory()
    logger.info("stage %s begin · %s%s", name, format_memory(before),
                _facts(facts_out))
    try:
        yield facts_out
    except BaseException as error:
        elapsed = time.perf_counter() - started
        after = memory()
        # The type, not the message: an exception from a CAD library can quote
        # file contents, and this line goes to a platform's log collector.
        logger.error(
            "stage %s FAILED after %.1fs · %s · %s%s",
            name, elapsed, type(error).__name__, format_memory(after),
            _facts(facts_out),
        )
        raise
    else:
        elapsed = time.perf_counter() - started
        after = memory()
        logger.info(
            "stage %s end %.1fs · %s%s",
            name, elapsed, format_memory(after), _facts(facts_out),
        )


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
