"""This application must never create, alter or remove anything outside its schema.

The PostgreSQL instance is shared. Email Drafter's production data lives in
``public``; this application owns ``projection_area`` and nothing else. These tests
fail if any path could change that:

* the static check refuses every statement form that could reach another schema;
* no configuration, default or override can make ``public`` (or a system schema)
  this application's schema;
* every connection is built with ``search_path`` set to exactly the schema;
* destructive operations verify the live connection's schema and database first;
* against a real, scratch database, running the migrations leaves ``public``'s
  catalog exactly as it was.

The last group needs ``PROJECTED_AREA_TEST_DATABASE_URL`` — a scratch database,
never the shared production one — and is skipped without it.
"""

from __future__ import annotations

import re

import pytest

from backend.db import config as db_config
from backend.db import migrations
from backend.db import pool as db_pool

SCHEMA = "projection_area"


# ── the static check ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("statement", [
    # the peer review's list, and then some
    "ALTER TABLE users ADD COLUMN x int",
    "ALTER TABLE public.users ADD COLUMN x int",
    "ALTER INDEX users_pkey RENAME TO y",
    "ALTER INDEX public.users_pkey RENAME TO y",
    "DROP TABLE users",
    "DROP TABLE IF EXISTS projection_area.analyses, public.users",
    "DROP INDEX public.some_idx",
    "DROP VIEW public.v",
    "DROP SEQUENCE users_id_seq",
    "COMMENT ON TABLE public.users IS 'x'",
    "COMMENT ON COLUMN public.users.email IS 'x'",
    "COMMENT ON SCHEMA public IS 'x'",
    "GRANT SELECT ON projection_area.analyses TO someone",
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    "CREATE EXTENSION pgcrypto SCHEMA projection_area",
    "CREATE INDEX i ON users (email)",
    "CREATE UNIQUE INDEX i ON public.users (email)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON ONLY public.users USING btree (email)",
    'CREATE TABLE "public"."sneaky" (id int)',
    'CREATE TABLE "public".sneaky (id int)',
    'INSERT INTO "public"."users" VALUES (1)',
    'CREATE TABLE "Projection_Area".x (id int)',  # quoted: a different schema
    "CREATE TABLE pg_catalog.x (id int)",
    "CREATE TABLE information_schema.x (id int)",
    "CREATE SCHEMA public",
    "CREATE SCHEMA IF NOT EXISTS other_app",
    "CREATE SCHEMA projection_area CREATE TABLE public.x (id int)",
    "DROP SCHEMA projection_area CASCADE",
    "ALTER SCHEMA projection_area RENAME TO x",
    "DO $$ BEGIN EXECUTE 'DROP TABLE public.users'; END $$",
    "SET search_path = public",
    "RESET search_path",
    "CREATE ROLE intruder",
    "ALTER DATABASE postgres SET search_path = public",
    "CREATE VIEW v AS SELECT 1",
    "CREATE OR REPLACE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql",
    "CREATE TRIGGER t AFTER INSERT ON public.users FOR EACH ROW EXECUTE FUNCTION f()",
    "TRUNCATE projection_area.analyses, users",
    "TRUNCATE TABLE ONLY public.users",
    "UPDATE ONLY public.users SET x = 1",
    "DELETE FROM ONLY public.users",
    "INSERT INTO projection_area.analyses SELECT * FROM public.users",
    "UPDATE projection_area.analyses SET x = u.x FROM public.users u",
    "CREATE TABLE projection_area.x AS SELECT * FROM users",
    "VACUUM public.users",
    "LOCK TABLE public.users",
    "SELECT 1",
])
def test_the_check_refuses_every_statement_that_could_reach_outside(statement):
    assert migrations.unqualified_objects(statement, SCHEMA), (
        f"not refused: {statement}")


@pytest.mark.parametrize("statement", [
    "CREATE SCHEMA IF NOT EXISTS projection_area",
    'CREATE SCHEMA IF NOT EXISTS "projection_area"',
    "CREATE TABLE IF NOT EXISTS projection_area.t (id uuid PRIMARY KEY)",
    "CREATE TABLE projection_area.t (a uuid REFERENCES projection_area.u (id) ON DELETE CASCADE)",
    "CREATE INDEX IF NOT EXISTS t_idx ON projection_area.t (created_at DESC)",
    "CREATE INDEX t_idx ON projection_area.t (c) WHERE status = 'completed'",
    "ALTER TABLE projection_area.t ADD COLUMN IF NOT EXISTS c jsonb",
    "COMMENT ON TABLE projection_area.t IS 'mentions public.users only in a literal'",
    "INSERT INTO projection_area.schema_migrations (version, name) VALUES (1, 'x')",
    "-- a comment naming public.users\nCREATE TABLE projection_area.t (id int)",
])
def test_the_check_accepts_what_a_migration_here_legitimately_does(statement):
    assert migrations.unqualified_objects(statement, SCHEMA) == []


def test_every_real_migration_passes_the_stricter_check():
    for migration in migrations.migrations(SCHEMA):
        for statement in migration.statements:
            assert migrations.unqualified_objects(statement, SCHEMA) == [], statement


def test_the_check_refuses_everything_when_the_schema_itself_is_forbidden():
    for schema in ("public", "pg_catalog", "information_schema", "pg_toast", "pg_temp_1"):
        assert migrations.unqualified_objects(
            f"CREATE TABLE {schema}.t (id int)", schema), schema


# ── configuration cannot point this application at public ────────────────────


def test_the_default_schema_is_not_public():
    assert db_config.DEFAULT_SCHEMA == SCHEMA
    assert db_config.validate_schema(db_config.DEFAULT_SCHEMA) == SCHEMA


@pytest.mark.parametrize("name", [
    "public", "pg_catalog", "information_schema", "pg_toast", "pg_temp", "pg_anything",
    "PUBLIC", '"public"', "public;", "projection_area,public", "",
])
def test_no_configured_or_explicit_schema_can_be_public_or_system(monkeypatch, name):
    monkeypatch.setenv(db_config.SCHEMA_ENV, name)
    if name:
        with pytest.raises(db_config.ConfigurationError):
            db_config.schema_name()
    with pytest.raises(db_config.ConfigurationError):
        db_config.validate_schema(name)


def test_migrate_refuses_an_explicit_public_schema_before_connecting(monkeypatch):
    def must_not_connect(*args, **kwargs):
        raise AssertionError("connected before refusing the schema")

    monkeypatch.setattr(db_pool, "connection", must_not_connect)
    with pytest.raises(db_config.ConfigurationError):
        migrations.migrate(url="postgresql://scratch/db", schema="public")


# ── every connection is pinned to the schema ─────────────────────────────────


def test_every_pooled_connection_is_built_with_search_path_set_to_the_schema(monkeypatch):
    captured = {}

    class FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, **kwargs):
            captured.update(kwargs)

    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    db_pool._build_pool("postgresql://scratch/db", SCHEMA)
    assert captured["kwargs"]["options"] == f"-c search_path={SCHEMA}"


# ── destructive operations verify the live target first ──────────────────────


class _Conn:
    def __init__(self, search_path, current_schema, database):
        self.row = (search_path, current_schema, database)
        self.statements = []

    def cursor(self):
        conn = self

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False

            def execute(self, statement, params=None):
                conn.statements.append(statement)

            def fetchone(self):
                return conn.row

            rowcount = 1

        return Cursor()

    def commit(self):
        pass


@pytest.mark.parametrize("row", [
    ("public", "public", "db"),
    ("projection_area, public", "projection_area", "db"),
    ('"$user", public', "public", "db"),
    ("projection_area", "public", "db"),
    ("projection_area", "projection_area", "some_other_database"),
])
def test_the_target_check_refuses_a_connection_that_is_not_ours(row):
    with pytest.raises(db_pool.TargetMismatch):
        db_pool.verify_target(_Conn(*row), SCHEMA, "postgresql://scratch/db")


def test_the_target_check_accepts_our_schema_before_and_after_it_exists():
    db_pool.verify_target(_Conn("projection_area", None, "db"), SCHEMA, "postgresql://h/db")
    db_pool.verify_target(_Conn("projection_area", "projection_area", "db"), SCHEMA,
                          "postgresql://h/db")


def test_deleting_an_analysis_verifies_the_target_before_the_delete(monkeypatch):
    from contextlib import contextmanager

    from backend.db.analyses import AnalysisRepository

    conn = _Conn("public", "public", "db")

    @contextmanager
    def fake_connection(url=None, schema=None):
        yield conn

    monkeypatch.setattr(db_pool, "connection", fake_connection)
    repository = AnalysisRepository.__new__(AnalysisRepository)
    repository.url, repository.schema = "postgresql://scratch/db", SCHEMA
    with pytest.raises(db_pool.TargetMismatch):
        repository.delete("00000000-0000-0000-0000-000000000000")
    assert not any("DELETE" in s for s in conn.statements), "deleted before verifying"


def test_every_sql_statement_in_the_persistence_layer_is_schema_qualified():
    """Statically: every table named in a SQL string in backend/db is written as
    ``{schema}.``-qualified. A bare name would rely on search_path alone."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "backend" / "db"
    offenders = []
    table_site = re.compile(
        r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE(?:\s+IF\s+NOT\s+EXISTS)?|REFERENCES|ON)"
        r"\s+([A-Za-z_{][\w.{}]*)", re.I)
    for path in root.glob("*.py"):
        for literal in re.findall(r'f?"""(.*?)"""|f"((?:[^"\\]|\\.)*)"', path.read_text(), re.S):
            sql = literal[0] or literal[1]
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", sql):
                continue
            for name in table_site.findall(sql):
                qualified = re.match(r"\{(self\.)?_?schema\}\.|\{target_schema\}\.|"
                                     r"information_schema\.", name)
                allowed_bare = name.lower() in {"conflict", "delete", "update", "only", "set"}  # ON CONFLICT … DO UPDATE SET
                if not qualified and not allowed_bare:
                    offenders.append(f"{path.name}: {name!r} in {sql.strip()[:80]!r}")
    assert offenders == [], "\n".join(offenders)


# ── against a real scratch database ──────────────────────────────────────────


def _public_catalog(conn):
    """Everything in public, and every schema's name and owner."""
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT c.relname, c.relkind, c.relnatts
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
             ORDER BY c.relname""")
        relations = cursor.fetchall()
        cursor.execute("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public' ORDER BY p.proname""")
        functions = cursor.fetchall()
        cursor.execute("SELECT nspname FROM pg_namespace ORDER BY nspname")
        schemas = [row[0] for row in cursor.fetchall()]
    return relations, functions, schemas


def test_running_the_migrations_leaves_public_exactly_as_it_was():
    """The real thing: migrate a fresh scratch schema on a scratch database and
    compare public's catalog before and after."""
    import uuid

    url = db_config.test_database_url()
    if not url:
        pytest.skip(f"set {db_config.TEST_URL_ENV} to a scratch database "
                    "(never the shared production one)")
    import psycopg

    schema = f"pa_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url) as observer:
        before = _public_catalog(observer)
    try:
        migrations.migrate(url=url, schema=schema)
        migrations.migrate(url=url, schema=schema)   # idempotent, still nothing outside
        with psycopg.connect(url) as observer:
            after = _public_catalog(observer)
            with observer.cursor() as cursor:
                cursor.execute("""
                    SELECT n.nspname, count(*) FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = %s GROUP BY n.nspname""", (schema,))
                created = cursor.fetchall()
        with db_pool.connection(url, schema) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW search_path")
                search_path = cursor.fetchone()[0]
    finally:
        with db_pool.connection(url, schema) as conn:
            db_pool.verify_target(conn, schema, url)
            with conn.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        db_pool.close()

    assert after[0] == before[0], "a relation in public was created, changed or removed"
    assert after[1] == before[1], "a function in public was created or removed"
    assert set(after[2]) - set(before[2]) == {schema}, "only the scratch schema is new"
    assert created and created[0][1] >= 3, "the migrations created their tables"
    assert search_path == schema
