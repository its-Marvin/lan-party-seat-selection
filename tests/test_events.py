"""Several events: one active, earlier ones archived, later ones upcoming."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import sqlite3

import pytest

from lanparty import create_app
from lanparty.db import get_db, utc_now
from tests.helpers import EVENT_CODE, set_event_code

ADMIN_PW = "s3cret"


@pytest.fixture
def app(tmp_path):
    return set_event_code(create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "ADMIN_PASSWORD": ADMIN_PW,
    }))


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"name": "Test Admin", "password": ADMIN_PW}).status_code == 200
    return c


def register(app, name="Alice", email="alice@example.com"):
    res = app.test_client().post("/api/register", json={"name": name, "email": email, "event_code": EVENT_CODE})
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()["user"]


def events(admin):
    return admin.get("/api/admin/events").get_json()


def schedule_second_event(admin, frozen_event_clock):
    """Event 1 ends on 2026-07-01; event 2 opens on 2026-07-02 with its own code."""
    first = events(admin)["events"][0]["id"]
    assert admin.put(f"/api/admin/settings?event={first}", json={
        "event_start": "2026-06-20T14:00", "event_end": "2026-07-01T12:00"}).status_code == 200
    res = admin.post("/api/admin/events", json={"copy_from": first})
    assert res.status_code == 201
    second = res.get_json()["event"]["id"]
    res = admin.put(f"/api/admin/settings?event={second}", json={
        "event_name": "LAN Party 2", "registration_opens": "2026-07-02T00:00",
        "event_start": "2026-12-12T14:00", "event_end": "2026-12-14T12:00", "event_code": "NEXT1"})
    assert res.status_code == 200, res.get_json()
    return first, second


# --- defaults and migration ----------------------------------------------------

def test_fresh_database_has_one_active_event(app, admin):
    data = events(admin)
    assert [e["status"] for e in data["events"]] == ["active"]
    assert data["active_id"] == data["default_id"] == data["events"][0]["id"]
    info = app.test_client().get("/api/info").get_json()
    assert info["active"] is True and info["registration_open"] is True and info["upcoming"] == []


@pytest.mark.sqlite_only
def test_single_event_database_is_migrated_into_event_one(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE, seat TEXT UNIQUE,
            paid INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            seat_selected_at TEXT, paid_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE user_tokens (token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
    """)
    db.execute("INSERT INTO users (name, email, token_hash, seat, paid) VALUES "
               "('Old', 'old@example.com', ?, 'A1', 1)", (hashlib.sha256(b"old-token").hexdigest(),))
    db.executemany("INSERT INTO settings VALUES (?, ?)", [
        ("event_name", "Winter LAN"), ("amount_cents", "1500"), ("event_code", "OLD1"),
        ("ip_pepper", "keep-me")])
    db.commit()
    db.close()

    app = create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW})
    me = app.test_client().get("/api/me", headers={"X-User-Token": "old-token"}).get_json()
    assert me["user"]["seat"] == "A1" and me["user"]["paid"] is True
    info = app.test_client().get("/api/info").get_json()
    assert info["active"] and info["event_name"] == "Winter LAN" and info["amount_cents"] == 1500
    with app.app_context():
        conn = get_db()
        # per-event values moved, global ones stayed
        assert {r[0] for r in conn.execute("SELECT key FROM settings")} == {"ip_pepper", "_legacy_tokens_copied"}
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # the same email may now register for another event
        conn.execute("INSERT INTO events DEFAULT VALUES")
        conn.execute("INSERT INTO users (event_id, name, email, token_hash) "
                     "VALUES ((SELECT MAX(id) FROM events), 'Old', 'old@example.com', 'x')")
        conn.commit()
    # a second start does not migrate again
    app = create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_invalid_timezone_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="TIMEZONE"):
        create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite3"), "TIMEZONE": "Mars/Base"})


# --- creating and scheduling ---------------------------------------------------

def test_new_event_copies_settings_and_layout_but_not_dates_or_code(admin):
    first = events(admin)["events"][0]["id"]
    admin.put("/api/admin/settings", json={"event_name": "Summer LAN", "amount": "15",
                                           "event_start": "2026-06-20T14:00"})
    layout = admin.get("/api/admin/layout").get_json()
    layout["groups"][0]["label"] = "Q"
    assert admin.put("/api/admin/layout", json=layout).status_code == 200

    second = admin.post("/api/admin/events", json={"copy_from": first}).get_json()["event"]
    assert second["status"] == "draft"
    copied = admin.get(f"/api/admin/settings?event={second['id']}").get_json()
    assert copied["event_name"] == "Summer LAN" and copied["amount_cents"] == 1500
    assert copied["event_start"] == "" and copied["registration_opens"] == "" and copied["event_code"] == ""
    assert admin.get(f"/api/admin/layout?event={second['id']}").get_json()["groups"][0]["label"] == "Q"

    blank = admin.post("/api/admin/events", json={"copy_from": None}).get_json()["event"]
    assert admin.get(f"/api/admin/settings?event={blank['id']}").get_json()["event_name"] == "LAN Party"
    assert admin.post("/api/admin/events", json={"copy_from": 999}).status_code == 400
    assert admin.post("/api/admin/events", json={"copy_from": "1"}).status_code == 400


def test_drafts_are_not_public(app, admin):
    admin.post("/api/admin/events", json={"copy_from": None})
    assert app.test_client().get("/api/info").get_json()["upcoming"] == []


def test_registration_windows_may_not_overlap(admin):
    first = events(admin)["events"][0]["id"]
    second = admin.post("/api/admin/events", json={"copy_from": first}).get_json()["event"]["id"]
    # event 1 has no end yet: it blocks every later opening
    res = admin.put(f"/api/admin/settings?event={second}", json={"registration_opens": "2026-08-01T00:00"})
    assert res.status_code == 400 and "previous event has ended" in res.get_json()["error"]

    admin.put(f"/api/admin/settings?event={first}", json={"event_end": "2026-07-01T12:00"})
    res = admin.put(f"/api/admin/settings?event={second}", json={"registration_opens": "2026-07-01T11:00"})
    assert res.status_code == 400
    assert admin.put(f"/api/admin/settings?event={second}",
                     json={"registration_opens": "2026-07-01T12:00"}).status_code == 200
    # and the earlier event can't be extended into the next one now
    res = admin.put(f"/api/admin/settings?event={first}", json={"event_end": "2026-07-05T12:00"})
    assert res.status_code == 400
    res = admin.put(f"/api/admin/settings?event={second}",
                    json={"event_end": "2026-07-01T10:00"})
    assert res.status_code == 400 and "before the event ends" in res.get_json()["error"]


def test_upcoming_events_are_listed_publicly(app, admin, frozen_event_clock):
    schedule_second_event(admin, frozen_event_clock)
    info = app.test_client().get("/api/info").get_json()
    assert info["active"] and [u["event_name"] for u in info["upcoming"]] == ["LAN Party 2"]
    assert "event_code" not in info["upcoming"][0]
    assert [e["status"] for e in events(admin)["events"]] == ["active", "upcoming"]


# --- moving from one event to the next ------------------------------------------

def test_event_lifecycle(app, admin, frozen_event_clock):
    headers, alice = register(app)
    assert app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers).status_code == 200
    first, second = schedule_second_event(admin, frozen_event_clock)

    # between the events: nothing is active
    frozen_event_clock["now"] = datetime(2026, 7, 1, 18, 0)
    client = app.test_client()
    info = client.get("/api/info").get_json()
    assert info["active"] is False and info["registration_open"] is False
    assert client.get("/api/seats").get_json() == {
        "layout": None, "seats": [], "names_visible": False, "event_code_ok": False}
    assert client.get("/api/me", headers=headers).status_code == 401
    res = client.post("/api/register", json={"name": "Bob", "email": "bob@example.com", "event_code": EVENT_CODE})
    assert res.status_code == 403
    assert [e["status"] for e in events(admin)["events"]] == ["archived", "upcoming"]
    assert events(admin)["default_id"] == second

    # the next event opens with its own code and participants
    frozen_event_clock["now"] = datetime(2026, 7, 2, 9, 0)
    res = client.post("/api/register", json={"name": "Alice", "email": "alice@example.com", "event_code": "NEXT1"})
    assert res.status_code == 201
    new_headers = {"X-User-Token": res.get_json()["token"]}
    seats = client.get("/api/seats", headers=new_headers).get_json()["seats"]
    assert next(s for s in seats if s["id"] == "A1")["status"] == "free"
    assert client.post("/api/me/seat", json={"seat": "A1"}, headers=new_headers).status_code == 200
    # the old token still doesn't work, the archive keeps the old seat
    assert client.get("/api/me", headers=headers).status_code == 401
    old = admin.get(f"/api/admin/users?event={first}").get_json()["users"]
    assert [(u["name"], u["seat"]) for u in old] == [("Alice", "A1")]
    new = admin.get("/api/admin/users").get_json()
    assert new["event_id"] == second and len(new["users"]) == 1
    archived_seats = admin.get(f"/api/admin/seats?event={first}").get_json()["seats"]
    assert next(s for s in archived_seats if s["id"] == "A1")["name"] == "Alice"
    assert alice["event_id"] == first


def test_archived_events_are_read_only(app, admin, frozen_event_clock):
    headers, alice = register(app)
    _, bob = register(app, "Bob", "bob@example.com")
    first, _ = schedule_second_event(admin, frozen_event_clock)
    frozen_event_clock["now"] = datetime(2026, 7, 1, 18, 0)

    assert admin.post(f"/api/admin/users/{alice['id']}/move", json={"seat": "A2"}).status_code == 409
    assert admin.post(f"/api/admin/users/{alice['id']}/paid", json={"paid": True}).status_code == 409
    assert admin.post(f"/api/admin/users/{alice['id']}/login-link").status_code == 409
    layout = admin.get(f"/api/admin/layout?event={first}").get_json()
    assert admin.put(f"/api/admin/layout?event={first}", json=layout).status_code == 409
    # settings stay editable (a wrong end date can be fixed) and people can be deleted
    assert admin.put(f"/api/admin/settings?event={first}", json={"location": "Hall 3"}).status_code == 200
    assert admin.delete(f"/api/admin/users/{bob['id']}").status_code == 200


def test_reservations_of_archived_events_do_not_expire(app, admin, frozen_event_clock):
    headers, _ = register(app)
    app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    first, _ = schedule_second_event(admin, frozen_event_clock)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET reserved_at = ?", (utc_now(-1000),))
        db.commit()
    frozen_event_clock["now"] = datetime(2026, 7, 1, 18, 0)
    app.test_client().get("/api/info")
    users = admin.get(f"/api/admin/users?event={first}").get_json()["users"]
    assert users[0]["seat"] == "A1" and users[0]["reserved_until"] is None


def test_login_links_of_earlier_events_stop_working(app, admin, frozen_event_clock):
    headers, _ = register(app)
    code = app.test_client().post("/api/me/login-link", headers=headers).get_json()["code"]
    schedule_second_event(admin, frozen_event_clock)
    frozen_event_clock["now"] = datetime(2026, 7, 2, 9, 0)
    assert app.test_client().post("/api/login", json={"code": code}).status_code == 400


def test_layout_changes_only_move_seats_of_their_own_event(app, admin, frozen_event_clock):
    headers, _ = register(app)
    app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    first, second = schedule_second_event(admin, frozen_event_clock)
    frozen_event_clock["now"] = datetime(2026, 7, 2, 9, 0)
    res = app.test_client().post("/api/register", json={"name": "Bob", "email": "bob@example.com",
                                                        "event_code": "NEXT1"})
    bob = {"X-User-Token": res.get_json()["token"]}
    app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=bob)

    layout = admin.get(f"/api/admin/layout?event={second}").get_json()
    layout["groups"][0]["label"] = "Z"
    res = admin.put(f"/api/admin/layout?event={second}", json=layout)
    assert res.status_code == 200 and res.get_json()["moved"] == {"A1": "Z1"}
    assert admin.get(f"/api/admin/users?event={first}").get_json()["users"][0]["seat"] == "A1"
    assert admin.get(f"/api/admin/users?event={second}").get_json()["users"][0]["seat"] == "Z1"


def test_seat_change_rechecks_event_status_under_lock(app, admin, frozen_event_clock, monkeypatch):
    headers, _ = register(app)
    schedule_second_event(admin, frozen_event_clock)
    import lanparty.api as api_module
    real_transaction = api_module.write_transaction

    @contextmanager
    def event_ends_while_waiting_for_lock():
        # the request saw an active event; by the time it holds the lock it has ended
        frozen_event_clock["now"] = datetime(2026, 7, 1, 18, 0)
        with real_transaction() as db:
            yield db

    monkeypatch.setattr(api_module, "write_transaction", event_ends_while_waiting_for_lock)
    res = app.test_client().post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    assert res.status_code == 403
    res = app.test_client().post("/api/register", json={"name": "Bob", "email": "bob@example.com",
                                                        "event_code": EVENT_CODE})
    assert res.status_code == 403


# --- admin event parameter and deleting -----------------------------------------

def test_admin_event_parameter_is_validated(admin):
    assert admin.get("/api/admin/users?event=abc").status_code == 400
    assert admin.get("/api/admin/users?event=999").status_code == 404
    assert admin.get("/api/admin/settings?event=-1").status_code == 400


def test_event_endpoints_need_admin(app):
    client = app.test_client()
    assert client.get("/api/admin/events").status_code == 401
    assert client.post("/api/admin/events", json={}).status_code == 401
    assert client.delete("/api/admin/events/1").status_code == 401
    assert client.get("/api/admin/seats").status_code == 401


def test_deleting_events(app, admin, frozen_event_clock):
    headers, _ = register(app)
    only = events(admin)["events"][0]["id"]
    res = admin.delete(f"/api/admin/events/{only}")
    assert res.status_code == 400 and "active" in res.get_json()["error"]

    first, second = schedule_second_event(admin, frozen_event_clock)
    frozen_event_clock["now"] = datetime(2026, 7, 2, 9, 0)
    # the archived event goes with its participants, tokens and settings
    assert admin.delete(f"/api/admin/events/{first}").status_code == 200
    assert [e["id"] for e in events(admin)["events"]] == [second]
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM user_tokens").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM event_settings WHERE event_id = ?", (first,)).fetchone()[0] == 0
    assert admin.delete(f"/api/admin/events/{second}").status_code == 400  # active
    assert admin.delete("/api/admin/events/999").status_code == 400
    actions = [e["action"] for e in admin.get("/api/admin/audit").get_json()["entries"]]
    assert "event.deleted" in actions and "event.created" in actions
