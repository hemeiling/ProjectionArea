"""The analysis child process, tested against real processes.

The hosted 102 run was restarted with the event loop blocked for 17.5 seconds by
CAD parsing in the same interpreter. These tests hold the properties that the move
to a supervised child exists to provide, each against a real child — a real GIL
held, a real SIGKILL, a real SIGSEGV — rather than a mock of one:

* the web server keeps answering while the child is CPU-bound;
* progress, diagnostics and results cross the process boundary intact, and the
  engineering result is the one the in-process engine produces;
* every way a child can end is told apart, and none of them takes the server down;
* observing the work can never fail it;
* one heavy analysis runs at a time.
"""

from __future__ import annotations

import glob
import os
import signal
import sys
import tempfile
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import routes
from backend.jobs import JOBS, Job
from backend.progress import tracker_for
from backend.store import OWNER_MARKER, STORE_PREFIX
from backend.supervisor import HOSTS
from tests.test_viewer import viewer_url  # noqa: F401  (fixture reuse)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANALYSIS_ISOLATION", "process")
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def _selftest_job(args, kind="dwg") -> Job:
    return JOBS.start(
        "selftest",
        lambda job: routes._run_in_host(job, "selftest", args, spooled="")[0],
        tracker=tracker_for(kind),
    )


def _wait(job: Job, timeout: float = 60.0) -> Job:
    deadline = time.time() + timeout
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert job.state != "running", "job did not finish in time"
    return job


def _wait_until(predicate, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, "condition not reached in time"
        time.sleep(0.05)


def _newest_host():
    with HOSTS._lock:
        return HOSTS._hosts[-1] if HOSTS._hosts else None


def _analyse(client, path, timeout: float = 120.0):
    with open(path, "rb") as handle:
        response = client.post("/api/analyse?reanalyse=true",
                               files={"file": (os.path.basename(path), handle,
                                               "application/octet-stream")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.time() + timeout
    while True:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] != "running":
            return snap
        assert time.time() < deadline
        time.sleep(0.1)


def _engineering(area_payload):
    """The engineering content of an area result, without per-run identifiers."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if k not in ("document_id", "timestamp")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(area_payload)


# ── the server stays responsive ──────────────────────────────────────────────


def test_health_and_job_polls_stay_fast_while_the_child_holds_its_gil(viewer_url, monkeypatch):
    """The failure this exists to fix: /health unanswered for 17.5 s on the hosted
    102 run. A child spinning in pure Python must cost the server nothing."""
    monkeypatch.setenv("ANALYSIS_ISOLATION", "process")
    job = _selftest_job({"action": "spin", "seconds": 6})
    _wait_until(lambda: job.tracker.current_key == "geometry")

    health, polls = [], []
    deadline = time.time() + 4
    with httpx.Client(base_url=viewer_url, timeout=10) as http:
        while time.time() < deadline:
            started = time.perf_counter()
            assert http.get("health").status_code == 200
            health.append(time.perf_counter() - started)
            started = time.perf_counter()
            snap = http.get(f"api/jobs/{job.id}").json()
            polls.append(time.perf_counter() - started)
            assert snap["state"] == "running", "the child was still working throughout"
            time.sleep(0.1)
    _wait(job)

    assert job.state == "done"
    assert len(health) > 10
    assert max(health) < 0.5, f"/health stalled: worst {max(health):.2f}s"
    assert max(polls) < 0.5, f"job poll stalled: worst {max(polls):.2f}s"


# ── what crosses the process boundary ────────────────────────────────────────


def test_progress_and_diagnostics_cross_the_process_boundary(client, drawings):
    snap = _analyse(client, drawings["plate_with_holes"]["path"])

    assert snap["state"] == "done", snap.get("error")
    assert snap["progress"] == 1.0
    assert [s["stage"] for s in snap["stages"]] == snap["plan"]
    assert all(s["done"] for s in snap["stages"])
    stages = {d["stage"]: d for d in snap["diagnostics"]}
    for name in ("segments", "noding", "polygonize", "union"):
        record = stages[name]
        assert record["state"] == "done"
        assert record["elapsed_seconds"] >= 0
        assert isinstance(record["memory_peak_bytes"], int) and record["memory_peak_bytes"] > 0
        assert record["memory_peak_bytes"] >= (record["memory_start_bytes"] or 0)
    # Numbers only: nothing that identifies the process or the machine.
    text = str(snap["diagnostics"]) + str(snap.get("resources"))
    assert "pid" not in text and "/tmp" not in text and "host" not in text.lower()


def test_a_stage_peak_is_the_stage_s_own_not_the_process_high_water_mark():
    """A stage that allocates and frees 200 MB must report it — and a later,
    quieter stage must not inherit it, as a process-wide peak would make it."""
    from backend.supervisor import ProcessHost

    host = ProcessHost()
    records = []
    try:
        host.request("selftest", {"action": "stage", "bytes": 200_000_000, "hold": 0.8},
                     sink=lambda m: records.append(m[1]) if m[0] == "diag" else None)
        host.request("selftest", {"action": "stage", "bytes": 1_000_000, "hold": 0.3},
                     sink=lambda m: records.append(m[1]) if m[0] == "diag" else None)
    finally:
        host.retire()
    done = [r for r in records if r["state"] == "done"]
    big, small = done[0], done[1]
    # The 200 MB was held only inside the stage, and the sampler saw it.
    assert big["memory_peak_bytes"] - big["memory_start_bytes"] > 150_000_000
    # The quiet stage's own growth is its own, not the earlier stage's.
    assert small["memory_peak_bytes"] - small["memory_start_bytes"] < 50_000_000
    if sys.platform.startswith("linux"):
        # Linux returns a large freed block at once, so here the absolute peaks
        # differ as well — which a process high-water mark would never show.
        assert small["memory_peak_bytes"] < big["memory_peak_bytes"] - 100_000_000
        assert small["process_peak_bytes"] >= big["memory_peak_bytes"]


def test_the_child_s_result_is_identical_to_the_in_process_engine(drawings, monkeypatch):
    """Moving the work changed where it runs, not what it computes."""
    from backend.main import app

    names = ["plate_with_holes", "layout_1_100", "two_views", "curved_profile"]
    results = {}
    for isolation in ("inline", "process"):
        monkeypatch.setenv("ANALYSIS_ISOLATION", isolation)
        with TestClient(app) as test_client:
            for name in names:
                if name not in drawings:
                    continue
                snap = _analyse(test_client, drawings[name]["path"])
                assert snap["state"] == "done", (isolation, name, snap.get("error"))
                results[(isolation, name)] = _engineering(snap["result"]["area"])
    compared = [name for name in names if ("inline", name) in results]
    assert len(compared) >= 2
    for name in compared:
        assert results[("process", name)] == results[("inline", name)], name


def test_recalculation_runs_in_the_child_and_matches_the_engine(client, drawings, monkeypatch):
    snap = _analyse(client, drawings["plate_with_holes"]["path"])
    document_id = snap["document_id"]
    assert HOSTS.for_document(document_id) is not None, "the child keeps the drawing"
    request = {"scale": {"mode": "ratio", "ratio_denominator": 2.0}}
    hosted = client.post(f"/api/documents/{document_id}/pages/1/area", json=request)
    assert hosted.status_code == 200, hosted.text

    monkeypatch.setenv("ANALYSIS_ISOLATION", "inline")
    local = _analyse(client, drawings["plate_with_holes"]["path"])
    inline = client.post(f"/api/documents/{local['document_id']}/pages/1/area", json=request)
    assert inline.status_code == 200, inline.text
    assert _engineering(hosted.json()) == _engineering(inline.json())


# ── every ending, told apart ─────────────────────────────────────────────────


def test_a_python_exception_in_the_child_is_reported_safely(client):
    job = _wait(_selftest_job({"action": "raise"}))

    assert job.state == "failed"
    assert job.error["error_type"] == "ValueError"
    assert job.error["raised_at"].startswith("backend/analysis_host.py:_selftest:")
    assert "trace" not in job.error or "Traceback" not in str(job.error.get("reason"))
    assert client.get("/health").status_code == 200


def _store_dirs_owned_by(pid: int):
    owned = []
    for directory in glob.glob(os.path.join(tempfile.gettempdir(), STORE_PREFIX + "*")):
        try:
            with open(os.path.join(directory, OWNER_MARKER)) as handle:
                if int(handle.read().strip()) == pid:
                    owned.append(directory)
        except (OSError, ValueError):
            continue
    return owned


def test_sigkill_is_distinguished_and_the_server_survives_it(client, drawings):
    job = _selftest_job({"action": "spin", "seconds": 60})
    _wait_until(lambda: job.tracker.current_key == "geometry")
    host = _newest_host()
    pid = host.pid
    assert _store_dirs_owned_by(pid), "the child keeps its own store directory"

    os.kill(pid, signal.SIGKILL)
    _wait(job, timeout=30)

    assert job.state == "failed"
    assert job.error["kind"] == "worker_killed"
    assert job.error["signal"] == "SIGKILL"
    assert job.error["exit_code"] == -signal.SIGKILL
    assert job.error["last_progress_stage"] == "geometry"
    assert "memory" not in job.error["headline"].lower(), "SIGKILL alone does not prove OOM"
    assert _store_dirs_owned_by(pid) == [], "a killed child's files were left behind"

    # The server is untouched, and the next analysis works.
    assert client.get("/health").status_code == 200
    snap = _analyse(client, drawings["plate_with_holes"]["path"])
    assert snap["state"] == "done"


def test_sigsegv_is_distinguished(client):
    job = _wait(_selftest_job({"action": "segv"}))

    assert job.state == "failed"
    assert job.error["kind"] == "worker_crashed"
    assert job.error["signal"] == "SIGSEGV"
    assert client.get("/health").status_code == 200


def test_an_exit_status_is_distinguished(client):
    job = _wait(_selftest_job({"action": "exit", "code": 3}))

    assert job.error["kind"] == "worker_failed"
    assert job.error["exit_code"] == 3
    assert job.error["signal"] is None


def test_a_timeout_is_distinguished(client, monkeypatch):
    monkeypatch.setenv("ANALYSIS_TIMEOUT_SECONDS", "1")
    started = time.time()
    job = _wait(_selftest_job({"action": "spin", "seconds": 30}), timeout=30)

    assert job.error["kind"] == "worker_timeout"
    assert time.time() - started < 15
    assert client.get("/health").status_code == 200


def test_a_recalculation_on_a_dead_child_is_refused_clearly(client, drawings):
    snap = _analyse(client, drawings["plate_with_holes"]["path"])
    host = HOSTS.for_document(snap["document_id"])
    os.kill(host.pid, signal.SIGKILL)
    _wait_until(lambda: not host.alive)

    response = client.post(f"/api/documents/{snap['document_id']}/pages/1/area",
                           json={"scale": {"mode": "auto"}})
    assert response.status_code == 410
    assert response.json()["detail"]["kind"] == "document_host_gone"


# ── observation never fails the work ─────────────────────────────────────────


def test_a_failing_diagnostic_relay_cannot_fail_the_analysis(client, drawings, monkeypatch):
    reference = _analyse(client, drawings["plate_with_holes"]["path"])

    def broken(self, record):
        raise RuntimeError("diagnostics are broken")

    monkeypatch.setattr(Job, "record_diagnostic", broken)
    snap = _analyse(client, drawings["plate_with_holes"]["path"])

    assert snap["state"] == "done"
    assert _engineering(snap["result"]["area"]) == _engineering(reference["result"]["area"])


def test_failing_memory_measurement_cannot_fail_the_analysis(drawings, monkeypatch):
    from backend import diagnostics
    from backend.main import app

    monkeypatch.setenv("ANALYSIS_ISOLATION", "inline")
    with TestClient(app) as test_client:
        reference = _analyse(test_client, drawings["plate_with_holes"]["path"])

        def broken(*args, **kwargs):
            raise OSError("memory unreadable")

        monkeypatch.setattr(diagnostics, "memory", broken)
        monkeypatch.setattr(diagnostics, "current_rss", broken)
        monkeypatch.setattr(diagnostics, "_cgroup_memory", broken)
        snap = _analyse(test_client, drawings["plate_with_holes"]["path"])

    assert snap["state"] == "done", snap.get("error")
    assert _engineering(snap["result"]["area"]) == _engineering(reference["result"]["area"])


# ── one at a time ────────────────────────────────────────────────────────────


def test_only_one_heavy_analysis_runs_at_a_time(client):
    first = _selftest_job({"action": "spin", "seconds": 2})
    _wait_until(lambda: first.tracker.current_key == "geometry")
    second = _selftest_job({"action": "spin", "seconds": 1})
    time.sleep(0.5)
    queued_while_first_ran = second.queued and first.state == "running"
    snapshot = second.as_dict()
    _wait(first)
    _wait(second)

    assert queued_while_first_ran
    assert snapshot["queued"] is True
    assert first.state == second.state == "done"
    assert second.result["started"] >= first.result["ended"], "the two runs overlapped"
    assert second.queued is False
