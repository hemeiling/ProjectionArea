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


# ── the isolation check ──────────────────────────────────────────────────────
#
# This database instance also holds Email Drafter's production data, in `public`.
# Every statement a migration runs is therefore checked before it runs, and a
# statement that could reach any other schema is refused, not executed.
#
# The check is deliberately conservative. It understands the handful of forms a
# migration here legitimately uses; anything it cannot vouch for — a statement
# kind it does not know, a DO block it cannot read into, a GRANT, an extension —
# is refused outright. A false refusal costs a developer a minute; a false pass
# could cost another application its data.

#: Statements that are never acceptable in a migration here, whatever they name.
#: Extensions and grants are database-wide; DO blocks and dynamic SQL cannot be
#: checked; changing search_path or role would undo the isolation the connection
#: sets up; dropping or altering a schema is destructive at the wrong scale.
_FORBIDDEN_STATEMENTS = (
    (re.compile(r"^\s*(GRANT|REVOKE)\b", re.I), "GRANT/REVOKE"),
    (re.compile(r"^\s*(CREATE|ALTER|DROP)\s+EXTENSION\b", re.I), "extensions are database-wide"),
    (re.compile(r"^\s*(DROP|ALTER)\s+SCHEMA\b", re.I), "altering or dropping a schema"),
    (re.compile(r"^\s*DO\b", re.I), "DO blocks cannot be checked"),
    (re.compile(r"\bEXECUTE\b", re.I), "dynamic SQL cannot be checked"),
    (re.compile(r"^\s*(SET|RESET)\b", re.I), "changing session settings (search_path, role)"),
    (re.compile(r"^\s*(CREATE|ALTER|DROP)\s+(ROLE|USER|GROUP|DATABASE|TABLESPACE)\b", re.I),
     "cluster-wide objects"),
    (re.compile(r"^\s*ALTER\s+(SYSTEM|DEFAULT\s+PRIVILEGES)\b", re.I), "cluster-wide settings"),
    (re.compile(r"^\s*(COPY|SECURITY\s+LABEL|REASSIGN|VACUUM|CLUSTER|LOCK)\b", re.I),
     "not a migration statement"),
)

#: The statement kinds a migration here may use. Anything else is refused.
_KNOWN_STATEMENT = re.compile(
    r"^\s*(CREATE|ALTER|DROP|COMMENT|INSERT|UPDATE|DELETE|TRUNCATE)\b", re.I)

#: One SQL identifier, quoted or not, optionally qualified: `a`, `"A"`, `s.t`,
#: `"public"."x"`, `s.t.c` (a column, for COMMENT ON COLUMN).
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"

#: Object kinds that live inside a schema and so must be named with ours.
_KINDS = (r"TABLE|INDEX|SEQUENCE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|TYPE|DOMAIN"
          r"|TRIGGER|RULE|POLICY|STATISTICS|AGGREGATE|OPERATOR|COLLATION|FOREIGN\s+TABLE")

#: Where an object name follows. Each pattern captures the name (or a comma list).
_NAME_SITES = [
    re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?"
               rf"(?:{_KINDS})\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_NAME})", re.I),
    re.compile(rf"\bALTER\s+(?:{_KINDS})\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<name>{_NAME})", re.I),
    re.compile(rf"\bDROP\s+(?:{_KINDS})\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?"
               rf"(?P<list>{_NAME}(?:\s*,\s*{_NAME})*)", re.I),
    re.compile(rf"\bCOMMENT\s+ON\s+(?:COLUMN|CONSTRAINT\s+{_IDENT}\s+ON|{_KINDS})\s+(?P<name>{_NAME})", re.I),
    re.compile(rf"\bINSERT\s+INTO\s+(?P<name>{_NAME})", re.I),
    re.compile(rf"^\s*UPDATE\s+(?:ONLY\s+)?(?P<name>{_NAME})", re.I),
    re.compile(rf"\bDELETE\s+FROM\s+(?:ONLY\s+)?(?P<name>{_NAME})", re.I),
    re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?(?P<list>{_NAME}(?:\s*,\s*{_NAME})*)", re.I),
    re.compile(rf"\bREFERENCES\s+(?P<name>{_NAME})", re.I),
    # Reading another application's data is not modifying it, but a migration
    # here has no reason to, so it is refused too.
    re.compile(rf"\b(?:FROM|JOIN|USING)\s+(?:ONLY\s+)?(?P<name>{_NAME})", re.I),
    # The table an index or trigger is created on.
    re.compile(rf"\bON\s+(?:ONLY\s+)?(?P<name>{_NAME})\s*(?:\(|USING\b|FOR\b|$)", re.I),
]

_CREATE_SCHEMA = re.compile(
    rf"^\s*CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_IDENT})\s*(?P<rest>.*)$",
    re.I | re.S)
_COMMENT_ON_SCHEMA = re.compile(rf"^\s*COMMENT\s+ON\s+SCHEMA\s+(?P<name>{_IDENT})", re.I)


def _parts(name: str) -> List[str]:
    """Split a possibly quoted, qualified name into its identifiers, as PostgreSQL
    resolves them: unquoted parts fold to lower case, quoted parts keep theirs."""
    parts = re.findall(r'"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_$]*)', name)
    return [quoted.replace('""', '"') if quoted else bare.lower() for quoted, bare in parts]


def _in_schema(name: str, schema: str, allow_bare: bool = False) -> bool:
    parts = _parts(name)
    if len(parts) >= 2:
        return parts[0] == schema
    return allow_bare


def _strip_comments_and_literals(statement: str) -> str:
    """The statement without comments or string literals, so neither can hide or
    fake a name. Literals become '' — their content never names an object."""
    text = re.sub(r"--[^\n]*", " ", statement)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", text)


def unqualified_objects(statement: str, schema: str) -> List[str]:
    """Object names, or refusals, in a statement that are not inside ``schema``.

    Returns an empty list only for a statement this check can vouch for. Anything
    else comes back as a list of reasons, so a failure says what it objected to.
    """
    from backend.db.config import ConfigurationError, validate_schema

    try:
        validate_schema(schema)
    except ConfigurationError as error:
        return [f"schema {schema!r} refused: {error}"]

    text = _strip_comments_and_literals(statement)

    create_schema = _CREATE_SCHEMA.match(text)
    if create_schema:
        named = _parts(create_schema.group("name"))[0]
        offenders = [] if named == schema else [create_schema.group("name")]
        if create_schema.group("rest").strip():
            # CREATE SCHEMA ... CREATE TABLE ... would run nested statements.
            offenders.append("CREATE SCHEMA with embedded statements")
        return offenders
    comment_schema = _COMMENT_ON_SCHEMA.match(text)
    if comment_schema:
        named = _parts(comment_schema.group("name"))[0]
        return [] if named == schema else [comment_schema.group("name")]

    for pattern, why in _FORBIDDEN_STATEMENTS:
        if pattern.search(text):
            return [f"forbidden: {why}"]
    if not _KNOWN_STATEMENT.match(text):
        return ["forbidden: statement kind not recognised by the isolation check"]

    offenders: List[str] = []
    is_create_index = re.match(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", text, re.I)
    for site in _NAME_SITES:
        for match in site.finditer(text):
            names = ([match.group("name")] if "name" in site.groupindex and match.group("name")
                     else re.findall(_NAME, match.group("list")))
            for name in names:
                name = name.strip()
                # An index created *on* one of our tables lives in that table's
                # schema, so the index's own name may be bare. Nothing else may be.
                bare_ok = bool(is_create_index) and site is _NAME_SITES[0]
                if not _in_schema(name, schema, allow_bare=bare_ok):
                    offenders.append(name)
    return list(dict.fromkeys(offenders))


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
    target_schema = config.validate_schema(schema) if schema else config.schema_name()
    applied: List[int] = []
    with pool.connection(url, target_schema) as conn:
        # Before anything is created: this connection's schema is ours and its
        # database is the one configured. A mismatch is refused, not corrected.
        pool.verify_target(conn, target_schema, url)
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
