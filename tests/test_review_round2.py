"""Regression tests for the second round of PR #1 review findings."""

import hashlib
import sqlite3
import threading
import time
from datetime import datetime

from lanparty import create_app
from lanparty import settings as cfg
from tests.helpers import set_event_code
from tests.test_review_fixes import ADMIN_PW, admin, app, make_app, register, run_parallel  # noqa: F401


def login(client):
    assert client.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    return client


# --- settings: validation and save are one transaction -------------------------

def test_concurrent_partial_date_updates_stay_consistent(app, admin, monkeypatch):
    assert admin.put("/api/admin/settings", json={
        "event_start": "2026-12-12T14:00", "event_end": "2026-12-14T12:00"}).status_code == 200

    original = cfg.validate_update

    def slow_validate(data, event_id):
        out = original(data, event_id)
        time.sleep(0.2)            # widen the window between validation and save
        return out

    monkeypatch.setattr(cfg, "validate_update", slow_validate)
    clients = [login(app.test_client()), login(app.test_client())]
    bodies = [{"event_start": "2026-12-13T10:00"}, {"event_end": "2026-12-12T20:00"}]
    codes = run_parallel(2, lambda i: clients[i].put("/api/admin/settings", json=bodies[i]).status_code)

    assert sorted(codes) == [200, 400]
    stored = admin.get("/api/admin/settings").get_json()
    assert datetime.fromisoformat(stored["event_end"]) >= datetime.fromisoformat(stored["event_start"])


def test_failed_settings_update_changes_nothing(admin):
    before = admin.get("/api/admin/settings").get_json()
    res = admin.put("/api/admin/settings", json={"location": "Hall 2", "amount": "-5"})
    assert res.status_code == 400
    assert admin.get("/api/admin/settings").get_json() == before


# --- migration with several workers starting at once ---------------------------

def legacy_db(path, paid=0, amount_cents=None):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE, seat TEXT UNIQUE,
            paid INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            seat_selected_at TEXT, paid_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db.execute("INSERT INTO users (name, email, token_hash, seat, paid, seat_selected_at) VALUES "
               "('Old', 'old@example.com', ?, 'A1', ?, datetime('now'))",
               (hashlib.sha256(b"old-token").hexdigest(), paid))
    if amount_cents is not None:
        db.execute("INSERT INTO settings VALUES ('amount_cents', ?)", (str(amount_cents),))
    db.commit()
    db.close()


def test_parallel_startup_migrates_legacy_database_once(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_db(path)
    config = {"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW}
    errors = []

    def boot(_):
        try:
            return create_app(config)
        except Exception as exc:          # pragma: no cover - only on failure
            errors.append(exc)

    apps = run_parallel(6, boot)
    assert errors == []
    me = apps[0].test_client().get("/api/me", headers={"X-User-Token": "old-token"}).get_json()
    assert me["user"]["seat"] == "A1"


def test_migration_backfills_amount_of_old_payments(tmp_path):
    path = tmp_path / "legacy-paid.sqlite3"
    legacy_db(path, paid=1, amount_cents=1500)
    app = set_event_code(create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW}))
    users = login(app.test_client()).get("/api/admin/users").get_json()["users"]
    assert users[0]["paid_amount_cents"] == 1500


# --- payments keep the amount that was confirmed -------------------------------

def test_fee_change_does_not_rewrite_received_amount(app, admin):
    _, uid = register(app.test_client(), 1)
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    admin.put("/api/admin/settings", json={"amount": "25"})
    user = admin.get("/api/admin/users").get_json()["users"][0]
    assert user["paid"] is True and user["paid_amount_cents"] == 2000

    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": False})
    assert admin.get("/api/admin/users").get_json()["users"][0]["paid_amount_cents"] is None
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    assert admin.get("/api/admin/users").get_json()["users"][0]["paid_amount_cents"] == 2500


def test_repeated_confirmation_is_a_no_op(app, admin):
    _, uid = register(app.test_client(), 1)
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    first = admin.get("/api/admin/users").get_json()["users"][0]
    admin.put("/api/admin/settings", json={"amount": "30"})
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    again = admin.get("/api/admin/users").get_json()["users"][0]
    assert again["paid_amount_cents"] == first["paid_amount_cents"] == 2000
    assert again["paid_at"] == first["paid_at"]
    actions = [e["action"] for e in admin.get("/api/admin/audit").get_json()["entries"]]
    assert actions.count("payment.confirmed") == 1


# --- /seats reads layout and occupants from one snapshot -----------------------

def test_seat_plan_is_one_consistent_snapshot(app, monkeypatch):
    from lanparty import api as api_module
    from lanparty import layout as lay

    headers, _ = register(app.test_client(), 1)
    client = app.test_client()
    assert client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).status_code == 200

    real_load = lay.load
    outcome = {}

    def load_then_concurrent_write(event_id):
        layout = real_load(event_id)
        # another worker tries to move the reservation between the two reads
        other = sqlite3.connect(app.config["DATABASE"], timeout=0.3)
        try:
            other.execute("UPDATE users SET seat = 'B1' WHERE seat = 'A1'")
            other.commit()
            outcome["write"] = "committed"
        except sqlite3.OperationalError:
            outcome["write"] = "blocked"
        finally:
            other.close()
        return layout

    monkeypatch.setattr(api_module.lay, "load", load_then_concurrent_write)
    seats = {s["id"]: s for s in client.get("/api/seats").get_json()["seats"]}
    assert outcome["write"] == "blocked"
    assert seats["A1"]["status"] == "reserved" and seats["B1"]["status"] == "free"
