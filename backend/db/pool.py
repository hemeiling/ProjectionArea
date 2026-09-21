"""The connection pool, with this application's schema pinned to every session.

The instance is shared with another application. Isolation therefore cannot rest
on remembering to qualify each statement — one forgotten table name in one query
is enough. Instead every connection is opened with

    options = -c search_path=<schema>

so the schema is a property of the connection, decided once, before any statement
runs. Statements are schema-qualified *as well*, which is belt and braces rather
than duplication: the ``search_path`` protects a name someone forgot to qualify,
and the qualification protects against a pool handing back a connection whose
``search_path`` was changed by something else.

``public`` is deliberately absent from the path. A statement naming an unqualified
table that does not exist in this schema fails loudly instead of silently finding,
or creating, something in ``public``.

The pool is conservative (see :mod:`backend.db.config`) and opened lazily: a
process that never saves anything never connects.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from backend.db import config

logger = logging.getLogger("projected_area.db")


class _RedactingFilter(logging.Filter):
    """Strips hosts and connection strings out of the driver's own log records.

    psycopg's pool logs its connection failures itself, and those messages name the
    host: ``failed to resolve host 'dpg-...'``. That is infrastructure detail this
    application has undertaken not to emit, and it is emitted by a library rather
    than by code here — so it is filtered at the logger instead of hoped about.
    The reason for the failure survives; the address does not.
    """

    _PATTERNS = (
        re.compile(r"(?i)\b(host|hostaddr)\s*[=:]?\s*'?[\w.\-]+'?"),
        re.compile(r"(?i)[a-z+]+://[^\s'\"]+"),
        re.compile(r"(?i)\buser\s*[=:]\s*'?[\w.\-]+'?"),
        re.compile(r"(?i)\bpassword\s*[=:]\s*\S+"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = message
        for pattern in self._PATTERNS:
            cleaned = pattern.sub("<redacted>", cleaned)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


def install_log_redaction() -> None:
    """Attach the redacting filter to the driver's loggers. Idempotent."""
    for name in ("psycopg", "psycopg.pool"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactingFilter) for f in target.filters):
            target.addFilter(_RedactingFilter())


_pool: Optional[Any] = None
_pool_url: Optional[str] = None
_pool_schema: Optional[str] = None
_lock = threading.Lock()


class PersistenceUnavailable(RuntimeError):
    """The database cannot be reached, or is not configured.

    Raised instead of a driver exception so that nothing carrying a connection
    string, a host name or a password can reach a caller — and therefore a log
    line or a browser.
    """


def _build_pool(url: str, schema: str) -> Any:
    from psycopg_pool import ConnectionPool

    # `-c search_path=<schema>` is applied by the server at connect time. The
    # schema name is validated by config.schema_name() before it gets here, which
    # is what makes it safe to interpolate into a connection option.
    return ConnectionPool(
        conninfo=url,
        min_size=config.POOL_MIN_SIZE,
        max_size=config.POOL_MAX_SIZE,
        timeout=config.POOL_TIMEOUT_SECONDS,
        max_idle=config.POOL_MAX_IDLE_SECONDS,
        kwargs={"options": f"-c search_path={schema}", "autocommit": False},
        # Checked out connections are verified cheaply, so a connection the
        # platform dropped while idle fails here rather than mid-transaction.
        check=ConnectionPool.check_connection,
        open=False,
        name="projected-area",
    )


def get_pool(url: Optional[str] = None, schema: Optional[str] = None) -> Any:
    """The process-wide pool, opened on first use.

    Args:
        url: Override, for tests against a database that is not the configured one.
        schema: Override, likewise.

    Raises:
        PersistenceUnavailable: If no database is configured, or the pool cannot
            be opened. The message never contains the connection string.
    """
    global _pool, _pool_url, _pool_schema

    target_url = url or config.database_url()
    target_schema = schema or config.schema_name()
    if not target_url:
        raise PersistenceUnavailable(
            "No database is configured, so nothing can be saved or reopened. "
            f"Set {config.URL_ENV} to enable persistence."
        )

    with _lock:
        if _pool is not None and (target_url, target_schema) != (_pool_url, _pool_schema):
            # A different target than the open pool: tests do this deliberately.
            _close_locked()
        if _pool is None:
            pool = _build_pool(target_url, target_schema)
            try:
                pool.open(wait=True, timeout=config.POOL_TIMEOUT_SECONDS)
            except Exception as error:
                # Deliberately not `from error`: a driver exception can carry the
                # host and the user, and this message may be logged.
                raise PersistenceUnavailable(
                    f"Could not connect to the database "
                    f"({type(error).__name__}). Persistence is unavailable."
                ) from None
            _pool, _pool_url, _pool_schema = pool, target_url, target_schema
            logger.info(
                "database pool opened · schema %s · %s",
                target_schema, config.redact(target_url),
            )
    return _pool


@contextmanager
def connection(url: Optional[str] = None, schema: Optional[str] = None) -> Iterator[Any]:
    """A pooled connection, committed on success and rolled back on failure.

    Raises:
        PersistenceUnavailable: If the database is unreachable.
    """
    pool = get_pool(url, schema)
    try:
        with pool.connection() as conn:
            yield conn
    except PersistenceUnavailable:
        raise
    except Exception as error:
        raise PersistenceUnavailable(
            f"Database operation failed ({type(error).__name__})."
        ) from None


class TargetMismatch(RuntimeError):
    """A connection is not pointed where this application's writes must go."""


def verify_target(conn: Any, schema: str, url: Optional[str] = None) -> None:
    """Refuse unless ``conn`` resolves names in ``schema``, in the configured database.

    Called before anything destructive — migrations, deletes. Every statement is
    already schema-qualified; this is the independent check that the connection
    itself agrees, so a pool built with the wrong options, or a search_path changed
    underneath it, is caught before a row is removed rather than after.

    ``current_schema()`` is NULL until the schema exists, so the check reads the
    session's ``search_path`` itself.

    Raises:
        TargetMismatch: naming what disagreed, never the host or the credentials.
    """
    config.validate_schema(schema)
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('search_path'), current_schema(), current_database()")
        search_path, current_schema, database = cursor.fetchone()
    entries = [part.strip().strip('"') for part in (search_path or "").split(",") if part.strip()]
    if entries != [schema]:
        raise TargetMismatch(
            f"refusing: this connection's search_path is not exactly {schema!r}")
    if current_schema not in (None, schema):
        raise TargetMismatch(f"refusing: this connection resolves names outside {schema!r}")
    expected = config.database_name(url or config.database_url())
    if expected and database != expected:
        raise TargetMismatch(
            "refusing: this connection is not to the configured database")


#: How long a probe result is trusted. A health check runs every few seconds; the
#: answer to "is the database up" does not change that fast, and asking every time
#: is what makes a health check expensive.
PROBE_CACHE_SECONDS = 15.0

_probe_result: Optional[bool] = None
_probe_at: float = 0.0
_probe_refreshing = threading.Event()


def probe(force: bool = False) -> bool:
    """Whether the database answers, for ``/health``. Never raises, never slow.

    Two properties matter more than freshness here. It must not block: a platform
    restarts an instance whose health check times out, and a restart mid-analysis
    destroys an eight-minute job — so an unreachable database must make ``/health``
    report ``false`` quickly rather than wait on connection retries. And it must not
    connect per call: the result is cached briefly, so a healthy instance answers
    from the already-open pool.
    """
    global _probe_result, _probe_at

    if not config.is_enabled():
        return False
    now = time.monotonic()
    if _probe_result is not None and (now - _probe_at) < PROBE_CACHE_SECONDS and not force:
        return _probe_result

    # Refresh off the caller's thread. /health is served on the event loop, and a
    # database round trip there would block every other request — the opposite of
    # what a health check is for. A stale answer for a few seconds is the right
    # trade; an unavailable database is not an emergency for a tool whose
    # measurements do not depend on it.
    if not _probe_refreshing.is_set():
        _probe_refreshing.set()
        threading.Thread(target=_refresh_probe, name="pa-db-probe", daemon=True).start()
    return _probe_result if _probe_result is not None else False


def _refresh_probe() -> None:
    """Ask the database whether it is there. Runs on its own thread, never raises."""
    global _probe_result, _probe_at

    result = False
    try:
        # Only use the pool when it is already open. Opening it here would wait on
        # the connection attempts this function exists to avoid.
        if _pool is not None:
            with _pool.connection(timeout=2.0) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()[0] == 1
        else:
            import psycopg

            url = config.database_url() or ""
            with psycopg.connect(url, connect_timeout=2) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()[0] == 1
    except Exception:
        result = False
    finally:
        _probe_result, _probe_at = result, time.monotonic()
        _probe_refreshing.clear()


def _close_locked() -> None:
    global _pool, _pool_url, _pool_schema
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
    _pool, _pool_url, _pool_schema = None, None, None


def close() -> None:
    """Close the pool. Called on shutdown, and between tests."""
    global _probe_result, _probe_at
    with _lock:
        _close_locked()
    _probe_result, _probe_at = None, 0.0
