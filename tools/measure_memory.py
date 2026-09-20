"""Measure peak memory and wall time for one drawing, end to end.

    .venv/bin/python -m tools.measure_memory path/to/drawing.dwg [more...]

Answers a deployment question that cannot be answered by reading the code: how
much memory does measuring a real drawing actually need? A 512 MB instance is
either enough or it is not, and the difference between those is a process that
disappears mid-job.

Each drawing runs in a **fresh subprocess**, because peak resident size is a
high-water mark that never comes down — measuring several in one process would
report the largest, attributed to whichever ran last.

The converter runs as a child process, so its peak is collected separately. The
two are reported side by side and the *larger* is what an instance has to
provide, not the sum: conversion finishes and `dwg2dxf` exits before a single
entity is parsed, so the two peaks never coincide. On the 93 MB production DWG
that is the difference between 8.2 GB and 9.7 GB, and quoting the sum would
recommend a larger instance than the work needs.

CONSTITUTION.md §36 applies to this as much as to areas: the numbers printed here
are measured on the machine that runs it, and the platform and architecture are
printed with them. A figure from a laptop is evidence about a laptop.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import subprocess
import sys
import time
from typing import Any, Dict


def _peak_bytes(who: int) -> int:
    """Peak resident size in bytes, correcting for the platform's unit.

    ``ru_maxrss`` is bytes on macOS and kilobytes on Linux. Reporting one as the
    other is a factor of 1024, which is the difference between "fits in a free
    instance" and "needs 2 GB".
    """
    raw = resource.getrusage(who).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def _measure(path: str) -> Dict[str, Any]:
    """Ingest and measure one drawing in this process, reporting the cost."""
    from backend.api.routes import _detect_kind
    from backend.store import DocumentStore

    with open(path, "rb") as handle:
        head = handle.read(4096)
    kind = _detect_kind(head, os.path.basename(path))

    store = DocumentStore()
    started = time.time()
    # Spool exactly as the upload endpoint does, so the measurement includes the
    # copy on disk but not a copy in memory.
    spooled = store.spool_path(".upload")
    with open(path, "rb") as source, open(spooled, "wb") as target:
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            target.write(chunk)

    file_name = os.path.basename(path)
    if kind == "dwg":
        stored = store.adopt_dwg(spooled, file_name)
    elif kind == "dxf":
        stored = store.adopt_cad(spooled, file_name)
    else:
        stored = store.adopt_pdf(spooled, file_name)
    ingested = time.time()

    page = stored.doc.page_count if stored.doc is not None else 1
    _stored, prepared = store.prepared_page(stored.id, 1)
    prepared_at = time.time()

    primitives = (
        len(stored.cad.primitives) if stored.cad is not None
        else len(prepared.analysis.primitives)
    )
    result: Dict[str, Any] = {
        "file": file_name,
        "kind": kind,
        "size_mb": round(os.path.getsize(path) / 1e6, 1),
        "pages": page,
        "primitives": primitives,
        "ingest_seconds": round(ingested - started, 1),
        "prepare_seconds": round(prepared_at - ingested, 1),
        "total_seconds": round(prepared_at - started, 1),
        "peak_self_mb": round(_peak_bytes(resource.RUSAGE_SELF) / 1e6, 1),
        "peak_children_mb": round(_peak_bytes(resource.RUSAGE_CHILDREN) / 1e6, 1),
    }
    if stored.conversion is not None:
        result["converted_dxf_mb"] = round(stored.conversion.dxf_bytes / 1e6, 1)
        result["convert_seconds"] = round(stored.conversion.duration_seconds, 1)
    # The larger of the two, not their sum: the converter has exited before the
    # parse begins, so an instance never has to hold both at once.
    result["peak_required_mb"] = max(
        result["peak_self_mb"], result["peak_children_mb"])
    store.shutdown()
    return result


def _child(path: str) -> int:
    """Run one measurement and print it as JSON, for the parent to collect."""
    try:
        print("__RESULT__" + json.dumps(_measure(path)))
        return 0
    except Exception as error:  # a drawing that cannot be read is still a result
        print("__RESULT__" + json.dumps({
            "file": os.path.basename(path), "error": f"{type(error).__name__}: {error}"}))
        return 1


def main(argv: "list[str]") -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--child":
        return _child(argv[1])

    print(f"  platform: {platform.platform()} · {platform.machine()} · "
          f"Python {platform.python_version()}")
    print(f"  {'drawing':<44} {'MB':>6} {'prims':>9} {'app':>8} {'conv':>8} "
          f"{'needs':>8} {'time':>8}")
    print("  " + "-" * 96)

    rows = []
    for path in argv:
        proc = subprocess.run(
            [sys.executable, "-m", "tools.measure_memory", "--child", path],
            capture_output=True, text=True,
        )
        payload = next(
            (line[len("__RESULT__"):] for line in proc.stdout.splitlines()
             if line.startswith("__RESULT__")),
            None,
        )
        if payload is None:
            # A missing result means the child did not survive: on a memory-bound
            # run that is itself the finding, so it is reported, not hidden.
            killed = proc.returncode < 0
            print(f"  {os.path.basename(path):<44} "
                  f"{'KILLED (signal %d)' % -proc.returncode if killed else 'no result'}")
            rows.append({"file": os.path.basename(path),
                         "error": f"exit {proc.returncode}"})
            continue
        row = json.loads(payload)
        rows.append(row)
        if "error" in row:
            print(f"  {row['file']:<44} {row['error']}")
            continue
        print(f"  {row['file']:<44} {row['size_mb']:>6.1f} {row['primitives']:>9,} "
              f"{row['peak_self_mb']:>7.0f}M {row['peak_children_mb']:>7.0f}M "
              f"{row['peak_required_mb']:>7.0f}M {row['total_seconds']:>7.1f}s")

    print()
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
