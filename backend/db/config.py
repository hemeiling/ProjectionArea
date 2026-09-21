"""How to reach the database, and whether there is one at all.

CONSTITUTION.md §35 (proprietary data) and §31 (errors that explain). Two rules
shape this file:

**Persistence is optional.** A developer with no database must be able to run the
application, and the test suite must never need one. So an absent ``DATABASE_URL``
disables persistence and says so — it is not an error, and it is never a
traceback. Everything the engine does works without it; what is lost is only the
ability to reopen a finished analysis.

**A connection string is a credential.** It carries a password, a host and a
database name. It is never logged, never returned to a browser, never put in an
exception message, and never shown in ``/health``. Only ever redacted.

The schema is always explicit. The instance is shared with another application,
so relying on an ambient ``public`` default is exactly the mistake that would let
this application write where it has no business writing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

#: The connection string. Set by Render from the database's *internal* URL in
#: production; read from a local ``.env`` in development.
URL_ENV = "DATABASE_URL"

#: The schema every object lives in. Never ``public``.
SCHEMA_ENV = "DATABASE_SCHEMA"
DEFAULT_SCHEMA = "projection_area"

#: A deliberately separate variable for tests that need a real database. The test
#: suite must not pick up ``DATABASE_URL`` and start writing into a production
#: instance because someone happened to have a local ``.env``.
TEST_URL_ENV = "PROJECTED_AREA_TEST_DATABASE_URL"

#: Conservative on purpose: this instance also serves another application, and a
#: single-user engineering tool that holds ten connections open is taking them
#: from something that needs them.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4
POOL_TIMEOUT_SECONDS = 10.0
#: Recycle idle connections rather than holding them across a platform's network
#: idle timeout, which otherwise surfaces as a mysterious first-query failure.
POOL_MAX_IDLE_SECONDS = 300.0

#: A schema name reaches SQL as an identifier, so it is constrained rather than
#: quoted-and-hoped: lowercase letters, digits and underscores only.
_SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Names this application must never treat as its own.
_FORBIDDEN_SCHEMAS = frozenset({"public", "information_schema", "pg_catalog", "pg_toast"})


class ConfigurationError(RuntimeError):
    """The configuration is present but unusable. Never carries the URL."""


def load_local_env() -> None:
    """Read a local ``.env`` if one exists, without overriding the real environment.

    Development convenience only. On a platform the variables come from the
    service and there is no ``.env``, so this is a no-op there. ``override=False``
    matters: a value set in the environment always wins over a file on disk, which
    is what keeps a stale local file from quietly redirecting a deployed process.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(os.path.join(root, ".env"), override=False)


def schema_name(default: str = DEFAULT_SCHEMA) -> str:
    """The schema to use, validated.

    Raises:
        ConfigurationError: If the name could not be used safely as an identifier,
            or names a schema that belongs to PostgreSQL or to another
            application. Refusing is the only safe answer: the alternative is
            creating tables in ``public`` on a shared instance.
    """
    raw = (os.environ.get(SCHEMA_ENV) or "").strip() or default
    if not _SCHEMA_PATTERN.match(raw):
        raise ConfigurationError(
            f"{SCHEMA_ENV} must be a plain lowercase identifier; got {raw!r}"
        )
    if raw in _FORBIDDEN_SCHEMAS:
        raise ConfigurationError(
            f"{SCHEMA_ENV} may not be {raw!r}: this application shares its database "
            "instance and must own its own schema"
        )
    return raw


#: Hosts for which an unencrypted connection is reasonable, because the traffic
#: never leaves the machine.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _host_of(url: str) -> str:
    match = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:?]*)", url)
    return (match.group(1) if match else "").lower()


def ensure_tls(url: str) -> str:
    """The connection string with transport encryption required.

    An *internal* database URL stays inside the platform's network. An **external**
    one crosses the public internet carrying a password, and libpq will happily send
    it in clear text if the server allows it — so ``sslmode`` is not left to whoever
    pasted the URL. Absent, it becomes ``require``.

    A locally hosted database is exempt: its traffic never leaves the machine, and
    demanding TLS there would only break development against a plain local server.

    An explicit ``sslmode`` is respected rather than overridden — including
    ``verify-full``, which is stronger than this default because it authenticates
    the server as well as encrypting the link. It is not the default only because it
    needs a CA bundle configured, and a connection that fails closed at startup is
    a worse first experience than one that is encrypted but unauthenticated.
    """
    if not url:
        return url
    if "sslmode=" in url.lower():
        return url
    if _host_of(url) in _LOCAL_HOSTS:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}sslmode=require"


def database_url() -> Optional[str]:
    """The connection string, or ``None`` when persistence is not configured.

    Returned with TLS required for any non-local host: see :func:`ensure_tls`.
    """
    raw = (os.environ.get(URL_ENV) or "").strip()
    return ensure_tls(raw) if raw else None


def test_database_url() -> Optional[str]:
    """The connection string for database tests, which is never ``DATABASE_URL``."""
    raw = (os.environ.get(TEST_URL_ENV) or "").strip()
    return ensure_tls(raw) if raw else None


def is_enabled() -> bool:
    """Whether this process can persist anything."""
    return database_url() is not None


def redact(url: Optional[str]) -> str:
    """A connection string with everything sensitive removed.

    For logs and diagnostics. Keeps the driver and the database name, which are
    what someone debugging actually needs, and drops the user, the password, the
    host and the port. A password that reaches a log has leaked, whatever the log
    is for.
    """
    if not url:
        return "not configured"
    match = re.match(r"^(?P<scheme>[a-z+]+)://(?:[^@/]*@)?[^/?]*/?(?P<name>[^?]*)", url)
    if not match:
        return "configured"
    name = (match.group("name") or "").strip("/") or "?"
    return f"{match.group('scheme')}://<redacted>/{name}"


@dataclass(frozen=True)
class Settings:
    """Resolved persistence settings. Deliberately holds no URL."""

    enabled: bool
    schema: str
    redacted_url: str
    pool_max_size: int = POOL_MAX_SIZE

    #: Whether the link is encrypted. Reported so an operator can see it without
    #: seeing the URL.
    tls: bool = False

    @property
    def summary(self) -> str:
        if not self.enabled:
            return "persistence disabled (no DATABASE_URL); analyses will not be saved"
        transport = "TLS" if self.tls else "no TLS (local)"
        return (f"persistence enabled · schema {self.schema} · {transport} · "
                f"{self.redacted_url}")


def settings() -> Settings:
    """Read the configuration, without connecting to anything."""
    url = database_url()
    return Settings(
        enabled=url is not None,
        schema=schema_name(),
        redacted_url=redact(url),
        tls=bool(url) and "sslmode=disable" not in (url or "").lower()
        and _host_of(url or "") not in _LOCAL_HOSTS,
    )
