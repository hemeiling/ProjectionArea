"""What has to be true for this to run somewhere other than a laptop.

CONSTITUTION.md §35 (proprietary drawings) and §31 (errors that explain). The
failures these cover are the ones that do not appear locally: a port that is
assigned rather than chosen, a converter somewhere else, an upload too large to
hold in memory, a filename from a client that is not a browser, and a health
check that leaks where things live on the host.
"""

from __future__ import annotations

import io
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend import runtime
from backend.api.routes import HEAD_BYTES, _detect_kind, safe_file_name
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


# ── the environment ──────────────────────────────────────────────────────────


def test_the_port_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PORT", "10000")
    assert runtime.port() == 10000


def test_a_missing_or_nonsense_port_falls_back_rather_than_refusing_to_start(monkeypatch):
    """A broken PORT is a broken host, but not starting would be worse."""
    monkeypatch.delenv("PORT", raising=False)
    assert runtime.port() == 8000
    monkeypatch.setenv("PORT", "not-a-port")
    assert runtime.port() == 8000
    monkeypatch.setenv("PORT", "99999")
    assert runtime.port() == 8000


def test_binding_every_interface_only_where_the_platform_assigns_the_port(monkeypatch):
    """A container must bind 0.0.0.0 to be reachable; a laptop must not."""
    monkeypatch.delenv("PORT", raising=False)
    assert runtime.bind_host() == "127.0.0.1"
    monkeypatch.setenv("PORT", "10000")
    assert runtime.bind_host() == "0.0.0.0"


def test_the_upload_ceiling_is_large_enough_for_the_real_drawings(monkeypatch):
    """The production DWGs are 28, 30 and 93 MB. A 32 MB default would reject
    exactly the files this tool exists to measure."""
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    assert runtime.max_upload_bytes() >= 100 * 1024 * 1024
    monkeypatch.setenv("MAX_UPLOAD_MB", "10")
    assert runtime.max_upload_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("MAX_UPLOAD_MB", "")
    assert runtime.max_upload_bytes() == runtime.DEFAULT_MAX_UPLOAD_MB * 1024 * 1024


def test_nothing_requires_an_ai_key_to_start(monkeypatch):
    """AI is optional and interpretive; measurement never depends on it. The
    deployed service has to start with neither key present."""
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with TestClient(app) as fresh:
        assert fresh.get("/health").status_code == 200


# ── converter discovery ──────────────────────────────────────────────────────


def test_an_explicitly_configured_converter_is_honoured(monkeypatch, tmp_path):
    """A host that puts the binary somewhere unusual can say where."""
    from backend.cad import dwg as dwg_module

    fake = tmp_path / "dwg2dxf"
    fake.write_text("#!/bin/sh\necho 'dwg2dxf 9.9'\n")
    fake.chmod(0o755)
    monkeypatch.setenv(runtime.CONVERTER_ENV, str(fake))

    found = dwg_module.find_converter()
    assert found is not None
    assert found.path == str(fake)
    assert found.version == "9.9", "the configured binary is actually interrogated"


def test_the_hint_is_never_required(monkeypatch):
    """With nothing configured, discovery still works wherever the tool is."""
    from backend.cad import dwg as dwg_module

    monkeypatch.delenv(runtime.CONVERTER_ENV, raising=False)
    # Either it is present in this environment or it is not; both are valid.
    # What must not happen is an exception because no hint was given.
    assert dwg_module.find_converter() is None or dwg_module.find_converter().tool


def test_a_bad_hint_does_not_disable_a_working_converter(monkeypatch):
    from backend.cad import dwg as dwg_module

    monkeypatch.delenv(runtime.CONVERTER_ENV, raising=False)
    without_hint = dwg_module.find_converter()
    monkeypatch.setenv(runtime.CONVERTER_ENV, "/nonexistent/dwg2dxf")
    with_bad_hint = dwg_module.find_converter()
    if without_hint is None:
        assert with_bad_hint is None
    else:
        assert with_bad_hint is not None, (
            "an unusable hint must fall through to discovery, not disable DWG"
        )


def test_the_remedy_is_phrased_for_the_environment(monkeypatch):
    """Telling a deployed instance to run a local build script is noise."""
    from backend.cad.dwg import setup_advice

    monkeypatch.delenv("PORT", raising=False)
    local = setup_advice()
    assert local["case"] == "local_build"
    assert local["command"].endswith("tools.install_dwg_support")

    monkeypatch.setenv("PORT", "10000")
    hosted = setup_advice()
    assert hosted["case"] == "deploy_image"
    assert hosted["command"] == "", "there is no command for the operator to run"
    assert "container image" in hosted["text"]


# ── /health ──────────────────────────────────────────────────────────────────


def test_health_reports_capability_for_a_platform_check(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["pdf"] is True and body["dxf"] is True
    assert isinstance(body["dwg"], bool)
    assert body["engine_version"]
    assert body["max_upload_mb"] >= 100
    if body["dwg"]:
        assert body["dwg_converter"] == "libredwg"
        assert body["dwg_converter_version"]
    else:
        assert body["dwg_converter"] is None


def test_health_exposes_no_paths_secrets_or_configuration(client):
    """It is a public endpoint on a deployed service (§35)."""
    body = client.get("/health").json()
    rendered = json.dumps(body)
    assert "/" not in rendered.replace("://", ""), f"a path leaked: {rendered}"
    for forbidden in ("KEY", "TOKEN", "SECRET", "PASSWORD", "Users", "home", "tmp"):
        assert forbidden not in rendered, f"{forbidden} appears in /health"


def test_health_is_the_same_answer_at_both_paths(client):
    """One implementation, so the two cannot drift apart."""
    assert client.get("/health").json() == client.get("/api/health").json()


def test_health_does_not_open_any_drawing(client):
    """A check that runs every few seconds must stay cheap."""
    import time

    client.get("/health")  # warm the converter lookup
    started = time.perf_counter()
    for _ in range(5):
        assert client.get("/health").status_code == 200
    assert (time.perf_counter() - started) < 2.0


# ── uploads ──────────────────────────────────────────────────────────────────


def test_a_file_name_from_a_client_cannot_carry_a_path():
    """Browsers send a bare name; anything else may not."""
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("/absolute/path/drawing.dwg") == "drawing.dwg"
    # Backslashes are separators whatever platform this runs on: the name came
    # from a client, not from this filesystem.
    assert safe_file_name(r"..\..\windows\system32\cmd.exe") == "cmd.exe"
    assert safe_file_name(r"C:\Users\drafter\line.dwg") == "line.dwg"
    assert safe_file_name("nul\x00.pdf") == "nul.pdf", "control characters removed"
    assert safe_file_name("") == "drawing"
    assert safe_file_name(None) == "drawing"
    assert safe_file_name("....") == "drawing", "a name of only dots names nothing"
    assert safe_file_name("x" * 400) == "x" * 120, "bounded, so it cannot bloat a log line"
    # A real drawing's name survives intact, including Chinese station names.
    assert safe_file_name("103-SSY1667-CTP产线-10JPH.dwg") == (
        "103-SSY1667-CTP产线-10JPH.dwg")


def test_an_upload_over_the_limit_is_refused_with_the_limit_named(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    oversized = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
    response = client.post(
        "/api/analyse",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["kind"] == "too_large"
    assert "1 MB" in detail["reason"], "say what the limit is"
    assert detail["fix"]


def test_an_oversized_upload_leaves_nothing_behind(client, monkeypatch, tmp_path):
    """The partial file is deleted: a refused upload must not fill the disk."""
    from backend.store import STORE

    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    before = set(os.listdir(STORE.root)) if os.path.isdir(STORE.root) else set()
    client.post(
        "/api/analyse",
        files={"file": ("huge.pdf", b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024),
                       "application/pdf")},
    )
    after = set(os.listdir(STORE.root)) if os.path.isdir(STORE.root) else set()
    assert not [name for name in after - before if name.startswith("incoming-")], (
        "a spooled upload was abandoned on disk"
    )


def test_an_empty_upload_is_refused(client):
    response = client.post(
        "/api/analyse", files={"file": ("nothing.pdf", b"", "application/pdf")})
    assert response.status_code == 400


def test_a_kind_is_decided_from_the_head_alone():
    """Detection reads :data:`HEAD_BYTES`, never the whole 93 MB file."""
    assert _detect_kind(b"%PDF-1.7\n" + b"x" * 100, "anything.bin") == "pdf"
    assert _detect_kind(b"AC1032" + b"\x00" * 100, "anything.bin") == "dwg"
    assert _detect_kind(b"AutoCAD Binary DXF\r\n\x1a\x00", "x.bin") == "dxf"
    assert _detect_kind(b"  0\r\nSECTION\r\n", "x.dxf") == "dxf"
    assert _detect_kind(b"\x89PNG\r\n\x1a\n", "drawing.png") == "unknown"
    # A PDF whose header sits a little way in is still a PDF.
    assert _detect_kind(b"\n" * 200 + b"%PDF-1.4", "x.bin") == "pdf"
    assert HEAD_BYTES >= 2048, "detection looks at up to 2 KB"


def test_an_unreadable_upload_is_refused_without_starting_a_job(client):
    response = client.post(
        "/api/analyse",
        files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 4096, "image/png")},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["kind"] == "unknown"


def test_a_lost_job_reports_facts_not_a_diagnosis(client):
    """A 404 cannot know why this process has no record of a job. It used to guess
    a restart or memory exhaustion; during a rolling deployment neither was true.
    What it can report is which instance answered, which the client compares with
    the instance that started the job."""
    response = client.get("/api/jobs/0123456789abcdef")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["kind"] == "job_lost"
    assert detail["fix"]
    assert "deployment" in detail["reason"], "names the expected, benign case"
    said = (detail["headline"] + " " + detail["reason"]).lower()
    assert "memory" not in said and "restart" not in said


def test_running_out_of_memory_is_reported_as_a_sizing_problem():
    """Not as a fault in the drawing, which would send the reader to the wrong
    place. Where the kernel kills the process instead, the job simply vanishes
    and :func:`job_status` explains that case."""
    from backend.jobs import _describe

    described = _describe(MemoryError())
    assert described["kind"] == "out_of_memory"
    assert "memory" in described["headline"].lower()
    assert "instance" in described["fix"]


def test_a_real_upload_still_works_through_the_streamed_path(client, drawings):
    """The refactor that removed the in-memory copy must not change behaviour."""
    path = drawings["plate_with_holes"]["path"]
    with open(path, "rb") as handle:
        payload = handle.read()
    response = client.post(
        "/api/documents", files={"file": ("plate_with_holes.pdf", payload, "application/pdf")})
    assert response.status_code == 200
    body = response.json()
    assert body["source_kind"] == "pdf"
    assert body["page_count"] == 1
    assert body["document_id"]


# ── temporary directories ────────────────────────────────────────────────────


def test_a_store_marks_the_process_that_owns_it():
    from backend.store import OWNER_MARKER, DocumentStore

    store = DocumentStore()
    try:
        marker = os.path.join(store.root, OWNER_MARKER)
        assert os.path.exists(marker)
        assert int(open(marker).read().strip()) == os.getpid()
    finally:
        store.shutdown()
    assert not os.path.exists(store.root), "a normal stop removes the directory"


def test_a_store_left_by_a_dead_process_is_swept(tmp_path, monkeypatch):
    """A killed process never runs shutdown, and what it leaves is customer
    geometry (§35). On a small instance being killed part way through a large
    drawing is a likely ending, so the next start cleans up after it."""
    import tempfile as tempfile_module

    from backend.store import OWNER_MARKER, STORE_PREFIX, sweep_orphaned_stores

    monkeypatch.setattr(tempfile_module, "gettempdir", lambda: str(tmp_path))

    orphan = tmp_path / f"{STORE_PREFIX}dead"
    orphan.mkdir()
    (orphan / OWNER_MARKER).write_text("999999")   # a pid that cannot be running
    (orphan / "abc123.dxf").write_text("proprietary geometry")

    mine = tmp_path / f"{STORE_PREFIX}live"
    mine.mkdir()
    (mine / OWNER_MARKER).write_text(str(os.getpid()))
    (mine / "def456.dxf").write_text("in use right now")

    unmarked = tmp_path / f"{STORE_PREFIX}unmarked"
    unmarked.mkdir()
    (unmarked / "something.dxf").write_text("not ours to judge")

    assert sweep_orphaned_stores() == 1
    assert not orphan.exists(), "the abandoned drawing was not removed"
    assert mine.exists(), "a running instance's files must never be deleted"
    assert unmarked.exists(), "an unmarked directory is left alone"


def test_uploads_are_never_left_in_the_store_after_removal(client, drawings):
    """§35: a drawing is deleted when asked, file and all."""
    from backend.store import STORE

    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        payload = handle.read()
    document_id = client.post(
        "/api/documents",
        files={"file": ("plate.pdf", payload, "application/pdf")},
    ).json()["document_id"]

    stored_path = STORE.get(document_id).path
    assert os.path.exists(stored_path)
    assert client.delete(f"/api/documents/{document_id}").status_code in (200, 204)
    assert not os.path.exists(stored_path), "the file outlived the document"


def test_the_converter_version_is_asked_for_once_not_per_health_check(monkeypatch, tmp_path):
    """A platform polls /health every few seconds. Spawning a subprocess each
    time is waste normally and a hazard while a job holds twelve gigabytes:
    forking under that pressure is how a health check fails and takes the job
    with it."""
    import subprocess as subprocess_module

    from backend.cad import dwg as dwg_module

    fake = tmp_path / "dwg2dxf"
    fake.write_text("#!/bin/sh\necho 'dwg2dxf 0.14'\n")
    fake.chmod(0o755)

    calls = []
    real_run = subprocess_module.run

    def counting_run(args, **kwargs):
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(dwg_module.subprocess, "run", counting_run)
    dwg_module._VERSION_CACHE.clear()

    first = dwg_module._dwg2dxf_version(str(fake))
    for _ in range(20):
        dwg_module._dwg2dxf_version(str(fake))

    assert first == "0.14"
    assert len(calls) == 1, f"the binary was interrogated {len(calls)} times"

    # A rebuilt converter must still be noticed: the cache is keyed on the
    # binary's identity, not merely its path.
    fake.write_text("#!/bin/sh\necho 'dwg2dxf 0.15'\n")
    fake.chmod(0o755)
    os.utime(str(fake), (0, 0))
    assert dwg_module._dwg2dxf_version(str(fake)) == "0.15"
    assert len(calls) == 2


def test_health_never_spawns_a_subprocess_after_the_first_call(client, monkeypatch):
    """The same property, through the endpoint a platform actually calls."""
    import subprocess as subprocess_module

    from backend.cad import dwg as dwg_module

    client.get("/health")  # first call may interrogate the binary

    calls = []
    real_run = subprocess_module.run
    monkeypatch.setattr(
        dwg_module.subprocess, "run",
        lambda args, **kw: (calls.append(args), real_run(args, **kw))[1])

    for _ in range(10):
        assert client.get("/health").status_code == 200
    assert calls == [], f"/health spawned {len(calls)} subprocess(es)"


# ── jobs are process-local, and the messages must not pretend otherwise ──────


def test_every_job_snapshot_names_the_instance_that_holds_it(client, drawings):
    """So a later poll answered by a different instance is recognisable as such."""
    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        blob = handle.read()
    job = client.post(
        "/api/analyse", files={"file": ("p.pdf", blob, "application/pdf")}).json()
    assert job["instance"], "the 202 says which instance took the job"
    polled = client.get(f"/api/jobs/{job['job_id']}").json()
    assert polled["instance"] == job["instance"]


def test_the_instance_token_reveals_no_host_detail():
    """It is for telling instances apart, not for identifying the host (§35)."""
    import platform

    token = runtime.instance_token()
    assert len(token) == 10 and all(c in "0123456789abcdef" for c in token)
    assert platform.node().lower() not in token
    assert token == runtime.instance_token(), "stable for the process's lifetime"


def test_the_instance_token_follows_the_platform_instance_id(monkeypatch):
    """Two Render instances must not share a token, even with equal PIDs."""
    monkeypatch.setenv("RENDER_INSTANCE_ID", "srv-abc-ld89q")
    first = runtime.instance_token()
    monkeypatch.setenv("RENDER_INSTANCE_ID", "srv-abc-nkzg8")
    assert runtime.instance_token() != first
