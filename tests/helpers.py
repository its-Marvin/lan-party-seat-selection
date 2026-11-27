"""Shared test helpers."""

from lanparty import db as dbmod
from lanparty.db import get_db

# Registration is closed until an event code is set (secure by default), so
# most tests configure this code and send it when registering.
EVENT_CODE = "TEST42"


def other_connection(app, timeout: float = 10):
    """A second database connection, as another worker process would open it."""
    conn = dbmod.connect(dbmod.database_target(app.config))
    if conn.dialect == "sqlite":
        conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    else:
        conn.execute(f"SET lock_timeout = '{int(timeout * 1000)}ms'")
    return conn


def first_event_id(db) -> int:
    return db.execute("SELECT MIN(id) FROM events").fetchone()[0]


def set_event_setting(app, key, value, event_id=None):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO event_settings (event_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(event_id, key) DO UPDATE SET value = excluded.value",
            (event_id or first_event_id(db), key, value),
        )
        db.commit()
    return app


def set_event_code(app, code=EVENT_CODE):
    return set_event_setting(app, "event_code", code)
