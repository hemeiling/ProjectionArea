"""Schema migrations, scoped strictly to this application's own schema.

Plain SQL applied in order and recorded in a table, rather than a migration
framework: there are three tables, the instance is shared with another
application, and a smaller mechanism is one whose blast radius can be read in a
minute (§27).

Every statement names ``{schema}.`` explicitly. That is not belt-and-braces with
the connection's ``search_path`` — it is what makes the isolation *checkable*:
:mod:`tests.test_persistence` asserts statically that no migration can name an
object outside the schema, which is only possible because every name is qualified.

The only unqualified statement permitted is ``CREATE SCHEMA``, which by definition
names the schema itself.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, NamedTuple, Optional

from backend.db import config, pool

logger = logging.getLogger("projected_area.db")


class Migration(NamedTuple):
    version: int
    name: str
    statements: List[str]


def migrations(schema: str) -> List[Migration]:
    """Every migration, rendered for a schema.

    Args:
        schema: Validated by :func:`backend.db.config.schema_name` before it
            reaches here, so interpolating it into SQL is safe.
    """
    return [
        Migration(1, "create schema and analyses", [
            f"CREATE SCHEMA IF NOT EXISTS {schema}",

            # What was applied, so a second run is a no-op.
            f"""CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
                    version      integer PRIMARY KEY,
                    name         text NOT NULL,
                    applied_at   timestamptz NOT NULL DEFAULT now()
                )""",

            # One row per completed analysis. Structured columns for what is
            # queried or filtered; JSONB for the engineering evidence, which is
            # read whole and never searched inside.
            f"""CREATE TABLE IF NOT EXISTS {schema}.analyses (
                    id                      uuid PRIMARY KEY,

                    -- identity: what was analysed, and by which algorithm
                    cache_key               char(64)     NOT NULL,
                    source_sha256           char(64)     NOT NULL,
                    original_filename       text         NOT NULL,
                    display_name            text,
                    source_type             text         NOT NULL,
                    source_size_bytes       bigint       NOT NULL,
                    engine_version          text         NOT NULL,
                    interpretation_version  text         NOT NULL,
                    config_fingerprint      char(64)     NOT NULL,

                    -- lifecycle
                    status                  text         NOT NULL,
                    created_at              timestamptz  NOT NULL DEFAULT now(),
                    completed_at            timestamptz,
                    updated_at              timestamptz  NOT NULL DEFAULT now(),

                    -- scale: how the measurement became physical, or why it did not
                    declared_units          text,
                    scale_source            text,
                    scale_mm_per_unit       double precision,
                    scale_verified          boolean      NOT NULL DEFAULT false,
                    calibration_json        jsonb,

                    -- the headline reading, queryable
                    primary_interpretation  text,
                    area_mm2                double precision,
                    area_m2                 double precision,
                    component_count         integer,
                    hole_count              integer,
                    primitive_count         integer,

                    -- the evidence, read whole
                    geometry_summary_json   jsonb,
                    cad_metadata_json       jsonb,
                    interpretations_json    jsonb,
                    warnings_json           jsonb,
                    assumptions_json        jsonb,
                    confidence_json         jsonb,

                    analysis_seconds        double precision
                )""",

            # Cache lookups are by key among completed rows only: a superseded row
            # must never be handed back as current.
            f"""CREATE INDEX IF NOT EXISTS analyses_cache_key_idx
                    ON {schema}.analyses (cache_key) WHERE status = 'completed'""",
            f"""CREATE INDEX IF NOT EXISTS analyses_recent_idx
                    ON {schema}.analyses (created_at DESC)""",
            f"""CREATE INDEX IF NOT EXISTS analyses_source_idx
                    ON {schema}.analyses (source_sha256)""",
        ]),

        Migration(2, "artifacts and events", [
            # The viewer payload, compressed. Tens of megabytes of polygon geometry
            # does not belong in JSONB: it is never queried, only fetched whole.
            f"""CREATE TABLE IF NOT EXISTS {schema}.artifacts (
                    id                    uuid PRIMARY KEY,
                    analysis_id           uuid NOT NULL
                        REFERENCES {schema}.analyses (id) ON DELETE CASCADE,
                    artifact_type         text        NOT NULL,
                    compression           text        NOT NULL,
                    payload               bytea       NOT NULL,
                    original_size_bytes   bigint      NOT NULL,
                    compressed_size_bytes bigint      NOT NULL,
                    sha256                char(64)    NOT NULL,
                    created_at            timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (analysis_id, artifact_type)
                )""",

            # Append-only audit. One table rather than separate calibration and
            # version tables: what is needed is the ability to answer "what changed
            # and when", and a typed event with a JSONB body does that without
            # three more joins (§27).
            f"""CREATE TABLE IF NOT EXISTS {schema}.analysis_events (
                    id           uuid PRIMARY KEY,
                    analysis_id  uuid NOT NULL
                        REFERENCES {schema}.analyses (id) ON DELETE CASCADE,
                    event        text        NOT NULL,
                    at           timestamptz NOT NULL DEFAULT now(),
                    detail_json  jsonb
                )""",
            f"""CREATE INDEX IF NOT EXISTS analysis_events_analysis_idx
                    ON {schema}.analysis_events (analysis_id, at)""",
        ]),
    ]


#: A statement may only name objects in this application's schema. ``CREATE SCHEMA``
#: is the one exception, since it names the schema itself.
_CREATE_SCHEMA = re.compile(r"^\s*CREATE\s+SCHEMA\b", re.IGNORECASE)
_INDEX_ON = re.compile(r"\bON\s+(\S+)", re.IGNORECASE)


def unqualified_objects(statement: str, schema: str) -> List[str]:
    """Object names in a statement that are not inside ``schema``.

    Used by the test suite to prove statically that no migration can touch another
    application's tables. Returns the offending names, so a failure says which.
    """
    if _CREATE_SCHEMA.match(statement):
        return []
    offenders: List[str] = []
    prefix = f"{schema}."
    for match in re.finditer(
        r"\b(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|DROP\s+TABLE"
        r"|INSERT\s+INTO|UPDATE|DELETE\s+FROM|REFERENCES|TRUNCATE)\s+([A-Za-z0-9_.\"]+)",
        statement, re.IGNORECASE,
    ):
        name = match.group(1)
        if not name.lower().startswith(prefix):
            offenders.append(name)
    # An index is created *on* a table, and that table must be ours. The index's
    # own name is schema-scoped by the table it indexes, so it needs no prefix.
    if re.match(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", statement, re.IGNORECASE):
        target = _INDEX_ON.search(statement)
        if target and not target.group(1).lower().startswith(prefix):
            offenders.append(target.group(1))
    return offenders


def applied_versions(conn: Any, schema: str) -> List[int]:
    """Versions already applied, or an empty list if nothing exists yet."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = 'schema_migrations')",
            (schema,),
        )
        if not cursor.fetchone()[0]:
            return []
        cursor.execute(f"SELECT version FROM {schema}.schema_migrations ORDER BY version")
        return [row[0] for row in cursor.fetchall()]


def migrate(url: Optional[str] = None, schema: Optional[str] = None) -> List[int]:
    """Apply any outstanding migrations. Returns the versions applied.

    Idempotent, and safe to run on every start: each migration is wrapped in its
    own transaction, so a failure leaves the schema at the last complete version
    rather than half-way through one.

    Raises:
        PersistenceUnavailable: If the database cannot be reached.
    """
    target_schema = schema or config.schema_name()
    applied: List[int] = []
    with pool.connection(url, target_schema) as conn:
        done = set(applied_versions(conn, target_schema))
        for migration in migrations(target_schema):
            if migration.version in done:
                continue
            for statement in migration.statements:
                offenders = unqualified_objects(statement, target_schema)
                if offenders:
                    # Refuse rather than run: a migration that could write outside
                    # this schema is a bug, and this instance is shared.
                    raise RuntimeError(
                        f"migration {migration.version} names objects outside "
                        f"{target_schema}: {offenders}"
                    )
                with conn.cursor() as cursor:
                    cursor.execute(statement)
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {target_schema}.schema_migrations (version, name) "
                    "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                    (migration.version, migration.name),
                )
            conn.commit()
            applied.append(migration.version)
            logger.info("applied migration %d (%s)", migration.version, migration.name)
    return applied
