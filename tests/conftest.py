"""Shared pytest fixtures.

The synthetic drawings are regenerated into a temporary directory for each test
session rather than committed as binaries, so the fixtures and the code that
reads them can never drift apart.

This file also keeps the suite away from a real database. A developer's ``.env``
holds ``DATABASE_URL`` for the *production* instance, which is shared with another
application; a test that picked it up would write rows into live data. So
``DATABASE_URL`` is removed from the environment for every test, and the tests that
genuinely need PostgreSQL opt in through a separate variable that no local ``.env``
sets by accident.
"""

from __future__ import annotations

import os
import sys
from typing import Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.demo.drawings import build_all  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _no_production_database_for_the_session():
    """The same guard, for the whole session.

    The per-test fixture below cannot cover a server started by a module-scoped
    fixture: that server's lifespan runs outside any test's patches, reads ``.env``
    and puts the production ``DATABASE_URL`` back into the process environment —
    where every later test would find it. On a machine whose ``DATABASE_URL`` is
    reachable, the browser tests would then write into live data.

    Session-scoped and autouse, so there is no ordering to get right.
    """
    from backend.db import config as db_config

    patch = pytest.MonkeyPatch()
    patch.delenv(db_config.URL_ENV, raising=False)
    patch.setattr(db_config, "load_local_env", lambda: None)
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def _no_production_database(monkeypatch) -> None:
    """Make every test run as though no database were configured.

    Autouse and unconditional: forgetting it in one test file is exactly how a
    suite ends up writing to production. A test that wants a real database asks
    for the ``database`` fixture, which reads
    ``PROJECTED_AREA_TEST_DATABASE_URL`` — a variable that has to be set on
    purpose.
    """
    from backend.db import config as db_config
    from backend.db import pool as db_pool

    monkeypatch.delenv(db_config.URL_ENV, raising=False)
    # And stop anything re-reading the developer's .env behind the test's back.
    # The application loads it at startup by design, which would put
    # DATABASE_URL — pointing at production — straight back into the environment
    # the moment a TestClient starts its lifespan. Deleting the variable is not
    # enough on its own; this is the other half of the same guard.
    monkeypatch.setattr(db_config, "load_local_env", lambda: None)
    db_pool.close()
    yield
    db_pool.close()


@pytest.fixture
def database():
    """A real PostgreSQL schema for this test, dropped afterwards.

    Skipped unless ``PROJECTED_AREA_TEST_DATABASE_URL`` is set. The schema name is
    unique per test run, so a shared instance is never disturbed and nothing
    outside that schema is touched.
    """
    import uuid

    from backend.db import config as db_config
    from backend.db import migrations, pool as db_pool

    url = db_config.test_database_url()
    if not url:
        pytest.skip(
            f"set {db_config.TEST_URL_ENV} to run the database tests "
            "(deliberately not DATABASE_URL, which points at production)"
        )
    schema = f"pa_test_{uuid.uuid4().hex[:12]}"
    migrations.migrate(url=url, schema=schema)
    try:
        yield {"url": url, "schema": schema}
    finally:
        try:
            # The one destructive statement in the suite: checked twice before it
            # runs — the name is this fixture's own shape, and the connection
            # really is scoped to it and to the test database.
            import re as _re

            assert _re.fullmatch(r"pa_test_[0-9a-f]{12}", schema), schema
            with db_pool.connection(url, schema) as conn:
                db_pool.verify_target(conn, schema, url)
                with conn.cursor() as cursor:
                    cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                conn.commit()
        finally:
            db_pool.close()


@pytest.fixture(scope="session")
def drawings(tmp_path_factory) -> Dict[str, Dict[str, object]]:
    """Every synthetic drawing plus its analytically known ground truth."""
    directory = tmp_path_factory.mktemp("drawings")
    return build_all(str(directory))
