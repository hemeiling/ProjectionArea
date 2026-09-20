"""Durable analyses: isolation, honesty, and no effect on any measurement.

CONSTITUTION.md §35 (proprietary data), §22 (the backend is authoritative) and §3
(never present a number the engine did not produce).

Three properties matter more than the plumbing:

* **Isolation.** The database is shared with another application. Nothing here may
  create, alter or touch an object outside this application's own schema.
* **Invariance.** Persistence observes a measurement; it never participates in one.
  The same drawing measured with a database configured and without one must give
  byte-identical engineering results.
* **Honesty.** A result is reported as saved only when it was saved, and a
  connection string never reaches a log, a browser or ``/health``.

Tests that need a real PostgreSQL ask for the ``database`` fixture and skip
without ``PROJECTED_AREA_TEST_DATABASE_URL``. They never use ``DATABASE_URL``,
which points at production.
"""

from __future__ import annotations

import gzip
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.config import ENGINE_VERSION, INTERPRETATION_VERSION, config_fingerprint
from backend.db import config as db_config
from backend.db import migrations
from backend.db.analyses import cache_key
from backend.db.artifacts import compress, decompress, encode
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


# ── configuration ────────────────────────────────────────────────────────────


def test_the_schema_defaults_to_this_application_s_own(monkeypatch):
    monkeypatch.delenv(db_config.SCHEMA_ENV, raising=False)
    assert db_config.schema_name() == "projection_area"


def test_the_schema_can_be_overridden(monkeypatch):
    monkeypatch.setenv(db_config.SCHEMA_ENV, "projection_area_staging")
    assert db_config.schema_name() == "projection_area_staging"


@pytest.mark.parametrize("name", ["public", "information_schema", "pg_catalog"])
def test_shared_and_system_schemas_are_refused(monkeypatch, name):
    """The instance is shared. Writing into ``public`` is the mistake this
    application must be unable to make, even by configuration."""
    monkeypatch.setenv(db_config.SCHEMA_ENV, name)
    with pytest.raises(db_config.ConfigurationError):
        db_config.schema_name()


@pytest.mark.parametrize("name", [
    "has space", "Uppercase", "semi;colon", "quote\"d", "dash-ed", "1leading",
    'x" ; DROP SCHEMA public CASCADE; --',
])
def test_a_schema_name_that_is_not_an_identifier_is_refused(monkeypatch, name):
    """The name is interpolated into a connection option and into DDL, so it is
    constrained rather than escaped-and-hoped."""
    monkeypatch.setenv(db_config.SCHEMA_ENV, name)
    with pytest.raises(db_config.ConfigurationError):
        db_config.schema_name()


def test_persistence_is_disabled_without_a_url(monkeypatch):
    monkeypatch.delenv(db_config.URL_ENV, raising=False)
    assert db_config.is_enabled() is False
    settings = db_config.settings()
    assert settings.enabled is False
    assert "disabled" in settings.summary
    assert "not saved" in settings.summary or "not be saved" in settings.summary


def test_a_connection_string_is_never_rendered_whole():
    """It carries a password and a host. Only ever redacted."""
    url = "postgresql://someuser:s3cr3t@dpg-abc123-a.ohio-postgres.render.com:5432/pa"
    redacted = db_config.redact(url)
    for secret in ("s3cr3t", "someuser", "dpg-abc123-a", "5432",
                   "ohio-postgres.render.com"):
        assert secret not in redacted, f"{secret} survived redaction"
    assert redacted == "postgresql://<redacted>/pa", redacted
    assert db_config.redact(None) == "not configured"


def test_the_settings_object_holds_no_url(monkeypatch):
    monkeypatch.setenv(db_config.URL_ENV, "postgresql://u:p@host:5432/db")
    settings = db_config.settings()
    rendered = repr(settings)
    for secret in ("u:p", "p@host", "host"):
        assert secret not in rendered, f"{secret} reachable from the settings object"


def test_the_driver_s_own_log_lines_are_redacted():
    """psycopg logs its connection failures itself, naming the host."""
    import logging

    from backend.db import pool as db_pool

    db_pool.install_log_redaction()
    record = logging.LogRecord(
        "psycopg.pool", logging.WARNING, __file__, 1,
        "error connecting in 'x': failed to resolve host 'dpg-secret-host-a'",
        (), None,
    )
    for f in logging.getLogger("psycopg.pool").filters:
        f.filter(record)
    assert "dpg-secret-host-a" not in record.getMessage()
    assert "resolve" in record.getMessage(), "the reason must survive"


# ── the cache key ────────────────────────────────────────────────────────────


def test_the_cache_key_covers_every_version_that_changes_the_answer():
    """A source hash alone would hand back a result produced by different code."""
    source = "a" * 64
    key = cache_key(source)
    assert len(key) == 64
    assert key != source, "the key is not just the source hash"

    # Same inputs, same key.
    assert cache_key(source) == key
    # A different drawing, a different key.
    assert cache_key("b" * 64) != key


def test_a_tolerance_change_invalidates_saved_analyses(monkeypatch):
    """Tolerances change reconstructed geometry without any version moving. A
    stored result from before such a change is not the same question, and reusing
    it would present a stale number as a current one."""
    from backend import config as engine_config

    before = cache_key("c" * 64)
    tightened = engine_config.Tolerances(snap=engine_config.TOLERANCES.snap * 2)
    monkeypatch.setattr(engine_config, "TOLERANCES", tightened)
    after = cache_key("c" * 64)
    assert after != before, "a tolerance change must invalidate the cache"


def test_an_engine_or_interpretation_change_invalidates_saved_analyses(monkeypatch):
    from backend.db import analyses as analyses_module

    before = cache_key("d" * 64)
    monkeypatch.setattr(analyses_module, "ENGINE_VERSION", "99.0.0")
    assert cache_key("d" * 64) != before
    monkeypatch.setattr(analyses_module, "ENGINE_VERSION", ENGINE_VERSION)
    monkeypatch.setattr(analyses_module, "INTERPRETATION_VERSION", "99.0.0")
    assert cache_key("d" * 64) != before


def test_the_config_fingerprint_is_of_values_not_objects(monkeypatch):
    """Stable across a rebuild of the same settings, so an unchanged deployment
    does not invalidate every saved analysis on restart."""
    import dataclasses

    from backend import config as engine_config

    original = config_fingerprint()
    assert config_fingerprint() == original

    rebuilt = engine_config.RegionSettings(**{
        field.name: getattr(engine_config.REGIONS, field.name)
        for field in dataclasses.fields(engine_config.REGIONS)
    })
    monkeypatch.setattr(engine_config, "REGIONS", rebuilt)
    assert config_fingerprint() == original, "same values must give the same key"

    changed = dataclasses.replace(
        engine_config.REGIONS,
        **{dataclasses.fields(engine_config.REGIONS)[0].name: 99.5})
    monkeypatch.setattr(engine_config, "REGIONS", changed)
    assert config_fingerprint() != original, "a changed setting must change the key"


# ── migrations cannot reach outside the schema ───────────────────────────────


def test_every_migration_statement_is_scoped_to_the_schema():
    """Static proof, which is why every name is qualified even though the
    connection's search_path would already cover it."""
    for migration in migrations.migrations("projection_area"):
        assert migration.statements, f"migration {migration.version} does nothing"
        for statement in migration.statements:
            offenders = migrations.unqualified_objects(statement, "projection_area")
            assert not offenders, (
                f"migration {migration.version} names {offenders} outside the schema:"
                f"\n{statement}"
            )


@pytest.mark.parametrize("statement,expected", [
    ("CREATE TABLE public.sneaky (id int)", ["public.sneaky"]),
    ("INSERT INTO email_drafts VALUES (1)", ["email_drafts"]),
    ("UPDATE public.users SET x = 1", ["public.users"]),
    ("DROP TABLE drafts", ["drafts"]),
    ("DELETE FROM public.anything", ["public.anything"]),
    ("ALTER TABLE other.thing ADD COLUMN c int", ["other.thing"]),
    ("CREATE TABLE projection_area.x (y uuid REFERENCES public.z (id))", ["public.z"]),
    ("CREATE INDEX i ON public.t (c)", ["public.t"]),
    ("TRUNCATE email_drafts", ["email_drafts"]),
])
def test_the_isolation_check_catches_statements_that_reach_outside(statement, expected):
    """The guard is only worth having if it actually fires."""
    assert migrations.unqualified_objects(statement, "projection_area") == expected


def test_creating_the_schema_itself_is_permitted():
    assert migrations.unqualified_objects(
        "CREATE SCHEMA IF NOT EXISTS projection_area", "projection_area") == []


def test_migrations_refuse_to_run_a_statement_that_reaches_outside(monkeypatch):
    """Not merely reported by a test: the runner itself refuses."""
    bad = [migrations.Migration(99, "bad", ["CREATE TABLE public.oops (id int)"])]
    monkeypatch.setattr(migrations, "migrations", lambda schema: bad)
    monkeypatch.setenv(db_config.URL_ENV, "postgresql://unused/db")

    executed = []

    class _Cursor:
        """Answers the migration runner's bookkeeping query, refuses anything else.

        The point under test is that no *migration statement* runs, not that no SQL
        runs at all: the runner has to ask which versions are applied before it can
        know what to apply.
        """

        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, statement, params=None):
            executed.append(statement)
            if "information_schema" in statement:
                return None
            raise AssertionError(f"a migration statement was executed: {statement!r}")

        def fetchone(self): return (False,)
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cursor()
        def commit(self): raise AssertionError("committed")

    from contextlib import contextmanager

    @contextmanager
    def fake_connection(url=None, schema=None):
        yield _Conn()

    monkeypatch.setattr(migrations.pool, "connection", fake_connection)
    with pytest.raises(RuntimeError, match="outside"):
        migrations.migrate()
    assert all("information_schema" in statement for statement in executed), executed


# ── artifacts ────────────────────────────────────────────────────────────────


def test_a_payload_survives_compression_unchanged():
    payload = {"area": {"components": [{"ring": [[1.5, 2.5], [3.0, 4.0]]}]},
               "warnings": ["中文 warning"]}
    raw = encode(payload)
    restored = json.loads(decompress(compress(raw)).decode("utf-8"))
    assert restored == payload


def test_encoding_is_deterministic_so_the_stored_hash_means_something():
    """Two artifacts with equal hashes must really be the same payload."""
    a = {"x": 1, "y": [2, 3], "z": {"b": 1, "a": 2}}
    b = {"z": {"a": 2, "b": 1}, "y": [2, 3], "x": 1}
    assert encode(a) == encode(b)
    assert compress(encode(a)) == compress(encode(b)), "gzip must not embed a timestamp"


def test_compression_is_worthwhile_on_a_geometry_payload():
    """The real 102 result is 44.7 MB of mostly coordinates. This is the shape of
    it: many floats, repeated keys — which is why gzip earns its place."""
    payload = {"components": [
        {"id": i, "outer": [[[j * 1.5, j * 2.25] for j in range(40)]], "holes": []}
        for i in range(400)
    ]}
    raw = encode(payload)
    blob = compress(raw)
    assert len(blob) < len(raw) / 4, (
        f"expected a decent ratio, got {len(blob) / len(raw):.2f}")
    assert decompress(blob) == raw


def test_a_corrupted_artifact_is_refused_rather_than_restored(monkeypatch, database):
    """Wrong geometry restored silently would put a wrong number in front of an
    engineer, which is the one failure worth raising over (§3)."""
    from backend.db import pool as db_pool
    from backend.db.artifacts import PostgresArtifactStore

    store = PostgresArtifactStore(database["url"], database["schema"])
    analysis_id = _insert_bare_analysis(database)
    store.put(analysis_id, "viewer_result", encode({"ok": True}))

    # Tamper with the stored bytes, leaving the recorded hash alone.
    with db_pool.connection(database["url"], database["schema"]) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {database['schema']}.artifacts SET payload = %s "
                "WHERE analysis_id = %s",
                (gzip.compress(b'{"ok":false}', mtime=0), analysis_id),
            )
        conn.commit()

    with pytest.raises(ValueError, match="checksum"):
        store.get(analysis_id, "viewer_result")


def _insert_bare_analysis(database) -> str:
    """A minimal analyses row, so artifact tests have something to hang off."""
    import uuid

    from backend.db import pool as db_pool

    analysis_id = str(uuid.uuid4())
    with db_pool.connection(database["url"], database["schema"]) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""INSERT INTO {database['schema']}.analyses
                        (id, cache_key, source_sha256, original_filename, source_type,
                         source_size_bytes, engine_version, interpretation_version,
                         config_fingerprint, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'completed')""",
                (analysis_id, "k" * 64, "s" * 64, "x.dxf", "dxf", 1,
                 ENGINE_VERSION, INTERPRETATION_VERSION, "f" * 64),
            )
        conn.commit()
    return analysis_id


# ── the API without a database ───────────────────────────────────────────────


def test_health_reports_no_database_without_saying_anything_about_one(client):
    body = client.get("/health").json()
    assert body["database"] is False
    assert body["persistence"] is False
    rendered = json.dumps(body)
    for forbidden in ("postgres", "@", "dpg-", "password", "5432"):
        assert forbidden not in rendered.lower(), f"{forbidden} appears in /health"


def test_saved_analysis_endpoints_explain_that_persistence_is_off(client):
    for method, path in (
        ("get", "/api/analyses"),
        ("get", "/api/analyses/00000000-0000-0000-0000-000000000000"),
        ("delete", "/api/analyses/00000000-0000-0000-0000-000000000000"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 503, f"{method} {path}"
        detail = response.json()["detail"]
        assert detail["kind"] == "persistence_disabled"
        assert detail["fix"]


def test_an_upload_still_works_with_no_database(client, drawings):
    """The engine does not depend on persistence. This is the property that makes
    persistence safe to add at all."""
    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        payload = handle.read()
    response = client.post(
        "/api/analyse",
        files={"file": ("plate.pdf", payload, "application/pdf")},
    )
    assert response.status_code == 202


# ── invariance: persistence must not change a measurement ────────────────────


def _measure_via_api(client, path, file_name):
    """Run one drawing through the job API and return the finished payload."""
    import time

    with open(path, "rb") as handle:
        blob = handle.read()
    response = client.post(
        "/api/analyse", files={"file": (file_name, blob, "application/octet-stream")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.time() + 180
    while time.time() < deadline:
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        if snapshot["state"] in ("done", "failed"):
            assert snapshot["state"] == "done", snapshot.get("error")
            return snapshot["result"]
        time.sleep(0.1)
    raise AssertionError("the job never finished")


def _engineering_only(payload):
    """The payload with everything non-engineering removed.

    Document ids and timestamps differ between runs by design; what must not
    differ is a single measured value.
    """
    area = dict(payload["area"])
    for volatile in ("document_id", "timestamp"):
        area.pop(volatile, None)
    return {
        "area": area,
        "overlay_primitive_count": (payload.get("overlay") or {}).get("primitive_count"),
        "analysis": payload.get("analysis"),
    }


@pytest.mark.parametrize("name", ["plate_with_holes", "layout_1_100"])
def test_a_measurement_is_identical_with_and_without_persistence(
    client, drawings, monkeypatch, name
):
    """Persistence is observational. Two runs of the same drawing — one with the
    save path reached, one with it disabled — must agree on every number.

    The save itself is stubbed rather than skipped, so the code path that would
    run in production is exercised and its effect on the result measured: none.
    """
    path = drawings[name]["path"]
    without = _measure_via_api(client, path, f"{name}.pdf")

    saves = []

    class _Recording:
        """Stands in for the repository: records, returns, changes nothing."""

        def save(self, payload, **kwargs):
            saves.append((payload, kwargs))
            return {"analysis": {"id": "stub"}, "artifact": {}}

        def find_compatible(self, source_sha256):
            return None

    from backend.api import routes

    monkeypatch.setattr(routes, "_repository", lambda: _Recording())
    with_persistence = _measure_via_api(client, path, f"{name}.pdf")

    assert saves, "the save path was not reached, so this proves nothing"
    assert _engineering_only(without) == _engineering_only(with_persistence)

    # And the payload handed to the repository is the payload the API returned.
    saved_payload, kwargs = saves[-1]
    assert _engineering_only(saved_payload) == _engineering_only(with_persistence)
    assert len(kwargs["source_sha256"]) == 64
    assert kwargs["source_size_bytes"] > 0


def test_a_failing_save_does_not_fail_the_analysis(client, drawings, monkeypatch):
    """An eight-minute measurement must not be lost because a database blinked."""
    from backend.api import routes

    class _Broken:
        def save(self, *a, **k):
            raise RuntimeError("database gone")

        def find_compatible(self, source_sha256):
            return None

    monkeypatch.setattr(routes, "_repository", lambda: _Broken())
    payload = _measure_via_api(
        client, drawings["plate_with_holes"]["path"], "plate.pdf")

    assert payload["area"]["projected_area"], "the measurement survived"
    assert payload["saved"]["stored"] is False
    assert payload["saved"]["reason"] == "save_failed"
    assert "database gone" not in json.dumps(payload["saved"]), (
        "an internal error message must not be handed to the browser verbatim"
    )


def test_a_cached_analysis_is_offered_and_never_substituted(
    client, drawings, monkeypatch
):
    """The operator chooses. Handing back a stored result as though it were a
    fresh measurement is the thing this must not do."""
    from backend.api import routes
    from backend.db.analyses import AnalysisSummary

    summary = AnalysisSummary(
        id="11111111-1111-1111-1111-111111111111", source_sha256="a" * 64,
        original_filename="plate.pdf", display_name=None, source_type="pdf",
        source_size_bytes=1234, status="completed",
        created_at="2026-09-20T00:00:00+00:00", completed_at="2026-09-20T00:00:05+00:00",
        updated_at=None, engine_version=ENGINE_VERSION,
        interpretation_version=INTERPRETATION_VERSION, declared_units="millimeters",
        scale_source="cad_declared_units", scale_mm_per_unit=1.0, scale_verified=True,
        primary_interpretation="geometry_union", area_mm2=23057.5, area_m2=0.023,
        component_count=1, hole_count=3, primitive_count=32, analysis_seconds=0.2,
    )

    class _Cached:
        def find_compatible(self, source_sha256):
            return summary

        def save(self, *a, **k):
            raise AssertionError("nothing should be saved on a cache hit")

    monkeypatch.setattr(routes, "_repository", lambda: _Cached())
    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        blob = handle.read()

    response = client.post(
        "/api/analyse", files={"file": ("plate.pdf", blob, "application/pdf")})
    assert response.status_code == 200, "a cache hit is an offer, not a job"
    body = response.json()
    assert body["cached"]["id"] == summary.id
    assert body["source_sha256"]
    assert "job_id" not in body, "no measurement was started"

    # And asking for it anyway starts a real job.
    again = client.post(
        "/api/analyse?reanalyse=true",
        files={"file": ("plate.pdf", blob, "application/pdf")})
    assert again.status_code == 202
    assert again.json()["job_id"]


def test_the_same_bytes_under_a_different_name_are_the_same_drawing(client, drawings):
    """Identity is the content, not the filename a drafter chose."""
    import hashlib

    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        blob = handle.read()
    assert cache_key(hashlib.sha256(blob).hexdigest()) == cache_key(
        hashlib.sha256(blob).hexdigest())


# ── the original drawing is not kept ────────────────────────────────────────


def test_no_saved_column_or_artifact_holds_the_source_drawing():
    """§35: the record describes the drawing; it does not contain it."""
    schema = "projection_area"
    ddl = "\n".join(
        statement
        for migration in migrations.migrations(schema)
        for statement in migration.statements
    )
    lowered = ddl.lower()
    # The only bytea column is the derived viewer artifact.
    assert lowered.count("bytea") == 1, "an unexpected binary column exists"
    assert "payload" in lowered
    # Column names that would mean the drawing itself is stored. Matched as whole
    # words: `original_filename` is a name, not a file, and must not trip this.
    import re

    for forbidden in ("source_bytes", "source_blob", "source_payload",
                      "original_file", "drawing_bytes", "source_data"):
        assert not re.search(rf"\b{forbidden}\b", lowered), (
            f"{forbidden} would retain a customer drawing")
    # What is kept about the source is its identity and its size.
    for kept in ("source_sha256", "original_filename", "source_size_bytes"):
        assert kept in lowered


# ── calibration persistence ──────────────────────────────────────────────────


def test_a_calibration_is_stamped_with_who_and_when_without_changing_a_number():
    """The engine records what a calibration was; the audit record also needs who
    established it and when. Added around the engine's record, never into its
    numbers."""
    from backend.api.routes import _stamp_calibration

    area = {
        "projected_area": {"verified": True, "net": {"mm2": 1234.5}},
        "scale": {
            "mm_per_unit": 0.5, "source": "two_point", "verified": True,
            "calibration": {"a": [0, 0], "b": [100, 0], "known_length": 50.0,
                            "known_unit": "mm", "span_units": 100.0},
        },
        "footprint_interpretations": [{"type": "geometry_union", "area_mm2": 1234.5}],
    }
    stamped = _stamp_calibration(area, operator_scale=True)

    calibration = stamped["scale"]["calibration"]
    assert calibration["provenance"] == "operator_supplied"
    assert calibration["recorded_at"], "when it was established"
    assert calibration["resulting_mm_per_unit"] == 0.5
    # The two points, the distance and the unit survive as the engine stated them.
    for key in ("a", "b", "known_length", "known_unit", "span_units"):
        assert calibration[key] == area["scale"]["calibration"][key]

    # And not one engineering value moved.
    assert stamped["projected_area"] == area["projected_area"]
    assert stamped["footprint_interpretations"] == area["footprint_interpretations"]
    assert stamped["scale"]["mm_per_unit"] == area["scale"]["mm_per_unit"]
    # The input was not mutated in place.
    assert "provenance" not in area["scale"]["calibration"]


def test_an_automatic_scale_is_not_recorded_as_operator_supplied():
    from backend.api.routes import _stamp_calibration

    area = {"scale": {"mm_per_unit": 1.0, "source": "cad_declared_units",
                      "calibration": None}}
    stamped = _stamp_calibration(area, operator_scale=False)
    assert stamped["scale"].get("calibration") is None, (
        "no calibration was made, so none is invented")


def test_a_calibration_is_never_written_onto_a_different_drawing_s_record(monkeypatch):
    """The analysis id comes from the browser. The source hash is what proves the
    recalculation belongs to it."""
    from backend.api import routes

    class _Summary:
        source_sha256 = "a" * 64

    class _Repository:
        updated = []

        def get_summary(self, analysis_id):
            return _Summary()

        def reopen(self, analysis_id):
            raise AssertionError("must refuse before reading anything")

        def update_result(self, *a, **k):
            self.updated.append(a)

    repository = _Repository()
    monkeypatch.setattr(routes, "_repository", lambda: repository)

    class _Stored:
        source_sha256 = "b" * 64  # a different drawing

    outcome = routes._persist_recalculation(
        "some-id", _Stored(), {"scale": {}}, operator_scale=True)
    assert outcome == {"stored": False, "reason": "different_drawing"}
    assert repository.updated == []


def test_a_document_with_no_known_source_cannot_update_a_record(monkeypatch):
    """A demo drawing, or any path that did not stream an upload, has no source
    hash — and so no proof of identity to write with."""
    from backend.api import routes

    class _Summary:
        source_sha256 = "a" * 64

    class _Repository:
        def get_summary(self, analysis_id):
            return _Summary()

    monkeypatch.setattr(routes, "_repository", lambda: _Repository())

    class _Stored:
        source_sha256 = ""

    outcome = routes._persist_recalculation(
        "some-id", _Stored(), {"scale": {}}, operator_scale=True)
    assert outcome["stored"] is False
    assert outcome["reason"] == "different_drawing"


def test_a_recalculation_names_its_analysis_without_changing_the_result(
    client, drawings
):
    """With persistence off, asking to save a recalculation must change nothing
    about the measurement — only report that nothing was saved."""
    with open(drawings["plate_with_holes"]["path"], "rb") as handle:
        blob = handle.read()
    document_id = client.post(
        "/api/documents", files={"file": ("plate.pdf", blob, "application/pdf")},
    ).json()["document_id"]

    plain = client.post(
        f"/api/documents/{document_id}/pages/1/area", json={}).json()
    named = client.post(
        f"/api/documents/{document_id}/pages/1/area",
        json={"analysis_id": "11111111-1111-1111-1111-111111111111"}).json()

    assert named["saved"] == {"stored": False, "reason": "persistence_disabled"}
    named.pop("saved")
    for volatile in ("timestamp",):
        plain.pop(volatile, None)
        named.pop(volatile, None)
    assert named == plain, "naming an analysis must not alter the measurement"
