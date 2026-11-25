"""Shared test helpers."""

from lanparty.db import get_db

# Registration is closed until an event code is set (secure by default), so
# most tests configure this code and send it when registering.
EVENT_CODE = "TEST42"


def set_event_code(app, code=EVENT_CODE):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('event_code', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (code,),
        )
        db.commit()
    return app
