"""Production diagnostics: what they record, and what they must never record.

Written after a hosted run of a production DWG was restarted part way through with
memory at 1.45 GB of 16 GB — a failure external telemetry could not explain. These
pin the instruments that explain it: memory around every stage, how a child
process ended, and how long the event loop — which the health check depends on —
was blocked.

And the constraint that makes them safe to leave on in production: counts, sizes,
durations and exception *types*. Never drawing content, never an exception message
that might quote it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

import pytest

from backend import diagnostics


def test_a_memory_snapshot_reports_the_process_and_is_numeric():
    snapshot = diagnostics.memory()
    assert snapshot["pid"] > 0
    assert snapshot["peak_rss"] > 0, "the high-water mark is always available"
    for key in ("rss", "vms", "children_peak_rss", "container_used", "container_limit"):
        assert key in snapshot
        assert snapshot[key] is None or isinstance(snapshot[key], int)


def test_the_formatted_line_carries_sizes_and_nothing_else():
    line = diagnostics.format_memory({
        "pid": 1, "rss": 2_000_000_000, "peak_rss": 3_000_000_000,
        "vms": 5_000_000_000, "children_peak_rss": 441_000_000,
        "container_used": 2_500_000_000, "container_limit": 16_000_000_000,
    })
    assert "rss=2000MB" in line
    assert "peak=3000MB" in line
    assert "child_peak=441MB" in line
    assert "container=2500MB/16000MB" in line, "the platform's own view of memory"


def test_a_stage_logs_its_start_its_end_and_what_it_held(caplog):
    caplog.set_level(logging.INFO, logger="projected_area.diag")
    with diagnostics.stage("noding", segments=1688512) as facts:
        facts["faces"] = 35616

    messages = [r.getMessage() for r in caplog.records]
    assert any("stage noding begin" in m and "segments=1688512" in m for m in messages)
    ended = [m for m in messages if "stage noding end" in m]
    assert ended, "a stage that finished says so — its absence is the signal of a kill"
    assert "faces=35616" in ended[0], "counts added during the stage are reported"
    assert "peak=" in ended[0]


def test_a_failing_stage_records_the_exception_type_but_never_its_message(caplog):
    """A CAD library's exception can quote file contents, and this goes to a
    platform's log collector (§35)."""
    caplog.set_level(logging.INFO, logger="projected_area.diag")
    secret = "LAYER 'CUSTOMER-PROPRIETARY-PART-7731'"
    with pytest.raises(ValueError):
        with diagnostics.stage("dxf.parse"):
            raise ValueError(secret)

    failures = [r.getMessage() for r in caplog.records if "FAILED" in r.getMessage()]
    assert failures, "a failed stage must be marked as failed"
    assert "ValueError" in failures[0]
    assert "CUSTOMER-PROPRIETARY" not in " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("code,expected", [
    (-9, "SIGKILL"),     # what an out-of-memory killer leaves behind
    (-11, "SIGSEGV"),    # a segmentation fault
    (-15, "SIGTERM"),
])
def test_a_child_killed_by_a_signal_is_named_as_such(caplog, code, expected):
    """"Conversion failed" hides whether the converter was killed or crashed, and
    those are different diagnoses."""
    caplog.set_level(logging.INFO, logger="projected_area.diag")
    diagnostics.subprocess_outcome("dwg2dxf", code, 3.2)
    line = next(r.getMessage() for r in caplog.records if "dwg2dxf" in r.getMessage())
    assert f"killed by {expected}" in line


def test_a_child_that_exits_normally_reports_its_code(caplog):
    caplog.set_level(logging.INFO, logger="projected_area.diag")
    diagnostics.subprocess_outcome("dwg2dxf", 0, 1.5, dxf_bytes=120061394)
    line = next(r.getMessage() for r in caplog.records if "dwg2dxf" in r.getMessage())
    assert "exited 0" in line and "dxf_bytes=120061394" in line


def test_the_watchdog_sees_a_blocked_event_loop(caplog):
    """The instrument that matters most: from outside, a restarted instance looks
    the same whether it ran out of memory or simply stopped answering. This is how
    the second case becomes visible in a log."""
    caplog.set_level(logging.WARNING, logger="projected_area.diag")

    async def scenario():
        watchdog = diagnostics.EventLoopWatchdog(threshold=0.5)
        watchdog.start()
        await asyncio.sleep(0.6)       # let it take a baseline tick
        time.sleep(1.2)                # block the loop, as GIL-bound work does
        await asyncio.sleep(0.8)       # let it wake and report
        watchdog.stop()
        return watchdog.summary()

    summary = asyncio.run(scenario())
    assert summary["stalls"] >= 1, "a blocked loop went unreported"
    assert summary["worst_stall_seconds"] >= 0.5
    assert any("event loop blocked" in r.getMessage() for r in caplog.records)


def test_the_watchdog_is_quiet_when_nothing_blocks(caplog):
    caplog.set_level(logging.WARNING, logger="projected_area.diag")

    async def scenario():
        watchdog = diagnostics.EventLoopWatchdog(threshold=0.5)
        watchdog.start()
        await asyncio.sleep(1.2)
        watchdog.stop()
        return watchdog.summary()

    assert asyncio.run(scenario())["stalls"] == 0


def test_the_watchdog_is_harmless_without_a_running_loop():
    """Tests and CLI tools import this module; starting it must not fail there."""
    watchdog = diagnostics.EventLoopWatchdog()
    watchdog.start()
    watchdog.stop()


# ── the endpoints a platform polls must not wait behind the analysis ─────────


def test_the_health_check_is_served_from_the_event_loop():
    """A sync endpoint runs in a threadpool that competes with the analysis for the
    GIL. Measured on a production DWG: /health 6.3 s and a job poll 2.8 s as sync
    endpoints, on a twelve-core machine. Async keeps them off that threadpool."""
    from backend.api import routes
    from backend import main

    assert inspect.iscoroutinefunction(routes.health)
    assert inspect.iscoroutinefunction(main.health), (
        "the root /health is the path the platform polls")
    assert inspect.iscoroutinefunction(routes.job_status)


def test_the_root_health_check_actually_answers():
    """Found while making it async: the root /health delegated to the route and
    returned an un-awaited coroutine, which serialised as a 500 on every call."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        root = client.get("/health")
        api = client.get("/api/health")
    assert root.status_code == 200, root.text
    assert root.json() == api.json()


def test_the_database_probe_never_blocks_the_caller(monkeypatch):
    """Served on the event loop, the health check must not wait on a database round
    trip; a slow or absent database answers from the cached value at once.

    The slow database is simulated rather than reached. An earlier version pointed
    at an unroutable address, and the real connection attempt it left running kept
    the test process from exiting — a test must not depend on the network.
    """
    import threading

    from backend.db import config as db_config
    from backend.db import pool

    finished = threading.Event()

    def slow_refresh():
        time.sleep(1.0)                  # a database taking a second to answer
        pool._probe_refreshing.clear()
        finished.set()

    monkeypatch.setenv(db_config.URL_ENV, "postgresql://u:p@db.example/pa")
    monkeypatch.setattr(pool, "_refresh_probe", slow_refresh)
    pool.close()

    started = time.perf_counter()
    answer = pool.probe()
    elapsed = time.perf_counter() - started

    assert answer is False, "unknown means not ready"
    assert elapsed < 0.2, f"the probe blocked its caller for {elapsed:.2f}s"
    # And the refresh really did run, on its own thread — then let it finish so
    # nothing outlives the test.
    assert finished.wait(timeout=5), "the background refresh never ran"
    pool.close()


# ── a deploy must reach the browser ──────────────────────────────────────────


def test_the_page_versions_its_own_assets():
    """A browser keeps /static/app.js across a deploy otherwise, and a fixed bug
    keeps being reported from the cached copy — which is what happened with the
    fetch error handler."""
    import re

    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        page = client.get("/").text
    scripts = re.findall(r'/static/app\.js\?v=([0-9a-f]+)', page)
    styles = re.findall(r'/static/styles\.css\?v=([0-9a-f]+)', page)
    assert scripts and styles, "the assets are referenced without a version"
    assert scripts[0] == styles[0]
    assert len(scripts[0]) >= 8


def test_the_version_follows_the_assets_bytes(tmp_path, monkeypatch):
    """Changing app.js must change the version; an unchanged deploy must not."""
    from backend import main

    first = main._asset_version()
    assert main._asset_version() == first, "stable across calls"

    fake = tmp_path / "frontend"
    fake.mkdir()
    (fake / "app.js").write_text("one")
    (fake / "styles.css").write_text("css")
    monkeypatch.setattr(main, "FRONTEND_DIR", str(fake))
    before = main._asset_version()
    (fake / "app.js").write_text("two")
    assert main._asset_version() != before


# ── instrumentation must never fail a measurement ────────────────────────────


def test_an_analysis_completes_with_diagnostics_enabled(drawings):
    """Found in this work: a log line naming a field that does not exist made every
    analysis fail at 100 %. The instruments observe; they must not be able to break
    what they observe."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        with open(drawings["plate_with_holes"]["path"], "rb") as handle:
            blob = handle.read()
        job = client.post(
            "/api/analyse", files={"file": ("plate.pdf", blob, "application/pdf")},
        ).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = client.get(f"/api/jobs/{job['job_id']}").json()
            if snapshot["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
    assert snapshot["state"] == "done", snapshot.get("error")


def test_a_broken_diagnostic_cannot_fail_an_analysis(drawings, monkeypatch):
    """Belt and braces: even a diagnostic that raises must leave the job intact."""
    from fastapi.testclient import TestClient

    from backend import diagnostics as diag
    from backend.main import app

    def exploding(*args, **kwargs):
        raise RuntimeError("instrumentation bug")

    monkeypatch.setattr(diag, "facts_of", exploding)

    with TestClient(app) as client:
        with open(drawings["plate_with_holes"]["path"], "rb") as handle:
            blob = handle.read()
        job = client.post(
            "/api/analyse", files={"file": ("plate.pdf", blob, "application/pdf")},
        ).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = client.get(f"/api/jobs/{job['job_id']}").json()
            if snapshot["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
    assert snapshot["state"] == "done", (
        "a failing log line took the analysis down with it")


def test_facts_of_reads_a_missing_attribute_as_none():
    class Report:
        component_count = 3388
        holes = 3405

    facts = diagnostics.facts_of(Report(), "component_count", "holes", "hole_count")
    assert facts == {"component_count": 3388, "holes": 3405, "hole_count": None}


def test_note_never_raises(monkeypatch):
    monkeypatch.setattr(diagnostics, "memory", lambda: (_ for _ in ()).throw(OSError()))
    diagnostics.note("anything", count=1)   # must not raise
