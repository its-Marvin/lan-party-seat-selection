"""Regression tests for the Copilot review of PR #3 (multi-event)."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import sqlite3
import time

import pytest

from lanparty import api as api_module
from lanparty import create_app
from lanparty.db import get_db, utc_now
from tests.test_events import ADMIN_PW, admin, app, register, schedule_second_event  # noqa: F401


# --- reservation expiry re-checks the event under the write lock --------------------

def test_expiry_does_not_touch_an_event_that_ended_while_waiting(app, admin, frozen_event_clock, monkeypatch):
    headers, _ = register(app)
    app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    first, _ = schedule_second_event(admin, frozen_event_clock)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET reserved_at = ?", (utc_now(-1000),))
        db.commit()

    real = api_module.write_transaction

    @contextmanager
    def event_ends_while_waiting_for_lock():
        frozen_event_clock["now"] = datetime(2026, 7, 1, 18, 0)   # event 1 is over now
        with real() as db:
            yield db

    monkeypatch.setattr(api_module, "write_transaction", event_ends_while_waiting_for_lock)
    app.test_client().get("/api/info")         # starts while event 1 is still active
    monkeypatch.undo()
    users = admin.get(f"/api/admin/users?event={first}").get_json()["users"]
    assert users[0]["seat"] == "A1"
    actions = [e["action"] for e in admin.get("/api/admin/audit").get_json()["entries"]]
    assert "reservation.expired" not in actions


def test_expiry_still_releases_while_the_event_is_active(app, admin):
    headers, _ = register(app)
    app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET reserved_at = ?", (utc_now(-1000),))
        db.commit()
    app.test_client().get("/api/info")
    assert admin.get("/api/admin/users").get_json()["users"][0]["seat"] is None
    expired = [e for e in admin.get("/api/admin/audit").get_json()["entries"] if e["action"] == "reservation.expired"]
    assert len(expired) == 1 and expired[0]["details"]["seat"] == "A1"


# --- migrating a database whose event has already ended -----------------------------

def legacy_db(path, settings):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE, seat TEXT UNIQUE,
            paid INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            seat_selected_at TEXT, paid_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db.execute("INSERT INTO users (name, email, token_hash, seat, paid) VALUES ('Old', 'old@example.com', ?, 'A1', 1)",
               (hashlib.sha256(b"old-token").hexdigest(),))
    db.executemany("INSERT INTO settings VALUES (?, ?)", settings.items())
    db.commit()
    db.close()


def boot(path):
    app = create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW})
    client = app.test_client()
    client.post("/api/admin/login", json={"password": ADMIN_PW})
    return app, client


@pytest.mark.sqlite_only
def test_migrated_event_that_already_ended_is_archived_with_a_valid_window(tmp_path):
    # the frozen clock says 2026-06-01 12:00
    path = tmp_path / "ended.sqlite3"
    legacy_db(path, {"event_start": "2026-05-01T14:00", "event_end": "2026-05-03T12:00"})
    _, admin = boot(path)
    event = admin.get("/api/admin/events").get_json()["events"][0]
    assert event["status"] == "archived"
    assert event["registration_opens"] == "2026-05-01T14:00"
    # the event stays editable: settings validation accepts the migrated window
    assert admin.put("/api/admin/settings", json={"location": "Hall 1"}).status_code == 200
    # and the next event can be scheduled right after it
    new = admin.post("/api/admin/events", json={"copy_from": event["id"]}).get_json()["event"]["id"]
    assert admin.put(f"/api/admin/settings?event={new}",
                     json={"registration_opens": "2026-06-01T12:00"}).status_code == 200


@pytest.mark.sqlite_only
def test_migrated_event_without_start_opens_a_day_before_its_end(tmp_path):
    path = tmp_path / "no-start.sqlite3"
    legacy_db(path, {"event_end": "2026-05-03T12:00"})
    _, admin = boot(path)
    event = admin.get("/api/admin/events").get_json()["events"][0]
    assert event["status"] == "archived" and event["registration_opens"] == "2026-05-02T12:00"


@pytest.mark.sqlite_only
def test_migrated_event_that_has_not_ended_stays_open(tmp_path):
    path = tmp_path / "running.sqlite3"
    legacy_db(path, {"event_start": "2026-05-01T14:00", "event_end": "2026-12-03T12:00"})
    app, admin = boot(path)
    event = admin.get("/api/admin/events").get_json()["events"][0]
    assert event["status"] == "active" and event["registration_opens"] == "2026-06-01T12:00"
    me = app.test_client().get("/api/me", headers={"X-User-Token": "old-token"}).get_json()
    assert me["user"]["seat"] == "A1"


# --- background polling does not keep an admin session alive ------------------------

def last_seen(app):
    with app.app_context():
        return get_db().execute("SELECT last_seen FROM admin_sessions").fetchone()[0]


def test_background_requests_do_not_count_as_activity(app, admin):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_sessions SET last_seen = last_seen - 600")
        db.commit()
    before = last_seen(app)
    assert admin.get("/api/admin/events", headers={"X-Background": "1"}).status_code == 200
    assert last_seen(app) == before
    assert admin.get("/api/admin/events").status_code == 200
    assert last_seen(app) > before


def test_idle_session_expires_despite_background_polling(app, admin):
    idle = app.config["ADMIN_SESSION_IDLE_MINUTES"] * 60
    for _ in range(3):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE admin_sessions SET last_seen = last_seen - ?", (idle / 3 + 1,))
            db.commit()
        status = admin.get("/api/admin/events", headers={"X-Background": "1"}).status_code
    assert status == 401
