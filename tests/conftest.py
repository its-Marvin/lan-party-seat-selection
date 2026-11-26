"""Event status depends on the clock: tests run at a fixed local time, so their
example dates (December 2026) stay in the future no matter when they run.

Set ``LANPARTY_TEST_DATABASE_URL=postgresql://…`` to run the suite against
PostgreSQL instead of SQLite: every database a test creates becomes its own
schema there, dropped afterwards. Tests marked ``sqlite_only`` (migration of old
SQLite files) are skipped in that mode."""

from datetime import datetime
import os
import secrets

import pytest

from lanparty import db as dbmod
from lanparty import events

FROZEN_NOW = datetime(2026, 6, 1, 12, 0)
POSTGRES_URL = os.environ.get("LANPARTY_TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def frozen_event_clock(monkeypatch):
    clock = {"now": FROZEN_NOW}
    monkeypatch.setattr(events, "local_now", lambda: clock["now"])
    return clock


@pytest.fixture(autouse=True)
def postgres_schemas(request):
    if not POSTGRES_URL:
        yield
        return
    if request.node.get_closest_marker("sqlite_only"):
        pytest.skip("needs SQLite")
    import psycopg

    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    schemas = {}

    def target(config):
        path = config.get("DATABASE_URL") or config["DATABASE"]
        if path not in schemas:
            schemas[path] = f"test_{secrets.token_hex(6)}"
            admin.execute(f'CREATE SCHEMA "{schemas[path]}"')
        sep = "&" if "?" in POSTGRES_URL else "?"
        return f"{POSTGRES_URL}{sep}options=-csearch_path%3D{schemas[path]}"

    # its own MonkeyPatch: some tests call monkeypatch.undo() halfway through
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dbmod, "database_target", target)
        yield
    for name in schemas.values():
        admin.execute(f'DROP SCHEMA "{name}" CASCADE')
    admin.close()
