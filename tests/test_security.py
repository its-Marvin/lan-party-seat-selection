"""Tests for the hardening measures (rate limits, sessions, expiry, login links, audit)."""

import sqlite3
import time

import pytest
from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from lanparty import create_app
from lanparty.db import get_db
from tests.helpers import EVENT_CODE, set_event_code

ADMIN_PW = "correct horse battery"


def make_app(tmp_path, **overrides):
    config = {"TESTING": True, "DATABASE": str(tmp_path / "sec.sqlite3"), "ADMIN_PASSWORD": ADMIN_PW}
    config.update(overrides)
    return set_event_code(create_app(config))


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return app.test_client()


def ip(addr):
    return {"environ_base": {"REMOTE_ADDR": addr}}


def login(client, password=ADMIN_PW, name="Alice Admin", addr="10.0.0.1"):
    return client.post("/api/admin/login", json={"password": password, "name": name}, **ip(addr))


def register(client, n, addr="10.1.0.1"):
    res = client.post("/api/register", json={"name": f"User{n}", "email": f"u{n}@example.com", "event_code": EVENT_CODE}, **ip(addr))
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()["user"]["id"]


def sql(app, query, params=()):
    with app.app_context():
        db = get_db()
        rows = db.execute(query, params).fetchall()
        db.commit()
        return rows


def audit_actions(app):
    return [r["action"] for r in sql(app, "SELECT action FROM audit_log ORDER BY id")]


# --- startup checks ------------------------------------------------------

def test_weak_admin_password_refused(tmp_path):
    with pytest.raises(RuntimeError, match="at least 12"):
        make_app(tmp_path, TESTING=False, ADMIN_PASSWORD="admin")


def test_weak_admin_password_allowed_explicitly(tmp_path):
    make_app(tmp_path, TESTING=False, ADMIN_PASSWORD="admin", ALLOW_WEAK_ADMIN_PASSWORD=True)
    make_app(tmp_path, TESTING=False, ADMIN_PASSWORD="a-long-enough-password")


# --- admin brute force protection ---------------------------------------

def test_admin_login_locked_per_ip(app, client):
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    res = login(client)                       # even the right password is refused now
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0
    assert login(client, addr="10.0.0.2").status_code == 200   # other IPs unaffected
    assert audit_actions(app).count("admin.login_failed") == 5


def test_admin_login_lockout_expires(app, client):
    for _ in range(5):
        login(client, "wrong")
    assert login(client).status_code == 429
    sql(app, "UPDATE rate_events SET ts = ts - 16 * 60")
    assert login(client).status_code == 200


def test_admin_login_global_limit(tmp_path):
    app = make_app(tmp_path, ADMIN_LOGIN_MAX_GLOBAL=8)
    c = app.test_client()
    for n in range(8):
        assert login(c, "wrong", addr=f"10.9.0.{n}").status_code == 401
    assert login(c, addr="10.9.1.1").status_code == 429


def test_successful_login_is_audited(app, client):
    assert login(client).status_code == 200
    entry = client.get("/api/admin/audit").get_json()["entries"][0]
    assert (entry["actor"], entry["action"], entry["details"]["ip"]) == ("Alice Admin", "admin.login", "10.0.0.1")


# --- server-side admin sessions -----------------------------------------

def test_admin_cookie_flags(client):
    cookie = login(client).headers["Set-Cookie"]
    assert cookie.startswith("lp_admin=")
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/api"):
        assert flag in cookie


def test_logout_invalidates_stolen_cookie(app):
    c = app.test_client()
    token = login(c).headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    c.post("/api/admin/logout")
    thief = app.test_client()
    thief.set_cookie("lp_admin", token, path="/api")
    assert thief.get("/api/admin/users").status_code == 401


def test_flask_session_cookie_cannot_grant_admin(app, client):
    forged_app = Flask("x")
    forged_app.secret_key = app.config["SECRET_KEY"]   # even with the real key
    cookie = SecureCookieSessionInterface().get_signing_serializer(forged_app).dumps({"admin": True})
    client.set_cookie("session", cookie)
    assert client.get("/api/admin/users").status_code == 401


def test_admin_session_idle_timeout(app, client):
    login(client)
    assert client.get("/api/admin/users").status_code == 200
    sql(app, "UPDATE admin_sessions SET last_seen = last_seen - 61 * 60")
    assert client.get("/api/admin/users").status_code == 401


def test_admin_session_absolute_lifetime(app, client):
    login(client)
    sql(app, "UPDATE admin_sessions SET created_at = created_at - 13 * 3600")
    assert client.get("/api/admin/users").status_code == 401


def test_password_change_ends_sessions(app, client):
    login(client)
    app.config["ADMIN_PASSWORD"] = "a brand new password"
    assert client.get("/api/admin/users").status_code == 401


def test_logout_all(app):
    a, b = app.test_client(), app.test_client()
    login(a)
    login(b, addr="10.0.0.2")
    assert a.post("/api/admin/logout-all").get_json()["sessions"] == 2
    assert b.get("/api/admin/users").status_code == 401
    assert "admin.logout_all" in audit_actions(app)


# --- registration abuse --------------------------------------------------

def test_registration_rate_limit_per_ip(client):
    for n in range(10):
        register(client, n)
    res = client.post("/api/register", json={"name": "Eleven", "email": "u11@example.com", "event_code": EVENT_CODE}, **ip("10.1.0.1"))
    assert res.status_code == 429 and "Retry-After" in res.headers
    register(client, 99, addr="10.1.0.2")


def test_failed_registrations_count_too(client):
    """Probing for registered emails is slowed down by the same limit."""
    register(client, 0)
    for _ in range(9):
        client.post("/api/register", json={"name": "Probe", "email": "u0@example.com", "event_code": EVENT_CODE}, **ip("10.1.0.1"))
    res = client.post("/api/register", json={"name": "Probe", "email": "u0@example.com", "event_code": EVENT_CODE}, **ip("10.1.0.1"))
    assert res.status_code == 429


def test_pending_reservations_per_ip(app, client):
    tokens = [register(client, n, addr=f"10.2.0.{n}")[0] for n in range(6)]
    for n, headers in enumerate(tokens[:4]):
        assert client.post("/api/me/seat", json={"seat": f"A{n + 1}"}, headers=headers, **ip("10.3.3.3")).status_code == 200
    res = client.post("/api/me/seat", json={"seat": "A5"}, headers=tokens[4], **ip("10.3.3.3"))
    assert res.status_code == 429
    # switching seats is not a new reservation
    assert client.post("/api/me/seat", json={"seat": "A9"}, headers=tokens[0], **ip("10.3.3.3")).status_code == 200
    # other networks are fine
    assert client.post("/api/me/seat", json={"seat": "A5"}, headers=tokens[4], **ip("10.4.4.4")).status_code == 200


def test_paid_reservations_do_not_count(app, client):
    admin = app.test_client()
    login(admin)
    tokens = [register(client, n, addr=f"10.2.0.{n}") for n in range(5)]
    for n, (headers, _) in enumerate(tokens[:4]):
        client.post("/api/me/seat", json={"seat": f"B{n + 1}"}, headers=headers, **ip("10.3.3.3"))
    admin.post(f"/api/admin/users/{tokens[0][1]}/paid", json={"paid": True})
    assert client.post("/api/me/seat", json={"seat": "B5"}, headers=tokens[4][0], **ip("10.3.3.3")).status_code == 200


# --- reservation expiry --------------------------------------------------

def test_unpaid_reservation_expires(app, client):
    headers, uid = register(client, 1)
    me = client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).get_json()
    assert me["user"]["reserved_until"].endswith("Z")
    assert me["payment"]["reserved_until"] == me["user"]["reserved_until"]

    sql(app, "UPDATE users SET reserved_at = datetime('now', '-49 hours')")
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["A1"] == "free"
    assert client.get("/api/me", headers=headers).get_json()["user"]["seat"] is None
    assert "reservation.expired" in audit_actions(app)
    # picking again starts a fresh window
    client.post("/api/me/seat", json={"seat": "A2"}, headers=headers)
    client.get("/api/seats")
    assert client.get("/api/me", headers=headers).get_json()["user"]["seat"] == "A2"


def test_switching_seat_keeps_deadline(app, client):
    headers, _ = register(client, 1)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    sql(app, "UPDATE users SET reserved_at = datetime('now', '-40 hours')")
    before = client.get("/api/me", headers=headers).get_json()["user"]["reserved_until"]
    client.post("/api/me/seat", json={"seat": "A2"}, headers=headers)
    client.delete("/api/me/seat", headers=headers)
    client.post("/api/me/seat", json={"seat": "A3"}, headers=headers)
    assert client.get("/api/me", headers=headers).get_json()["user"]["reserved_until"] == before


def test_paid_seats_never_expire(app, client):
    admin = app.test_client()
    login(admin)
    headers, uid = register(client, 1)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    sql(app, "UPDATE users SET reserved_at = datetime('now', '-400 hours')")
    user = client.get("/api/me", headers=headers).get_json()["user"]
    assert user["seat"] == "A1" and user["reserved_until"] is None


def test_revoking_payment_starts_new_window(app, client):
    admin = app.test_client()
    login(admin)
    headers, uid = register(client, 1)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    sql(app, "UPDATE users SET reserved_at = datetime('now', '-400 hours')")
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": False})
    assert client.get("/api/me", headers=headers).get_json()["user"]["seat"] == "A1"


def test_expiry_can_be_disabled_and_configured(app, client):
    admin = app.test_client()
    login(admin)
    assert admin.put("/api/admin/settings", json={"reservation_hours": 0}).status_code == 200
    headers, _ = register(client, 1)
    me = client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).get_json()
    assert me["user"]["reserved_until"] is None
    sql(app, "UPDATE users SET reserved_at = datetime('now', '-1000 hours')")
    assert client.get("/api/me", headers=headers).get_json()["user"]["seat"] == "A1"
    for bad in (-1, 1441, "abc", 1.5, True):
        assert admin.put("/api/admin/settings", json={"reservation_hours": bad}).status_code == 400


# --- one-time login links -------------------------------------------------

def test_login_link_flow(app, client):
    headers, uid = register(client, 1)
    link = client.post("/api/me/login-link", headers=headers).get_json()
    assert link["expires_at"].endswith("Z")

    assert client.post("/api/login/preview", json={"code": link["code"]}).get_json()["name"] == "User1"
    res = client.post("/api/login", json={"code": link["code"]})
    assert res.status_code == 200
    second_device = {"X-User-Token": res.get_json()["token"]}
    assert second_device != headers
    # both devices stay logged in
    assert client.get("/api/me", headers=second_device).get_json()["user"]["id"] == uid
    assert client.get("/api/me", headers=headers).get_json()["user"]["id"] == uid
    # the code works only once
    assert client.post("/api/login", json={"code": link["code"]}).status_code == 400
    assert client.post("/api/login/preview", json={"code": link["code"]}).status_code == 400


def test_login_link_expires(app, client):
    headers, _ = register(client, 1)
    code = client.post("/api/me/login-link", headers=headers).get_json()["code"]
    sql(app, "UPDATE login_codes SET expires_at = expires_at - 25 * 3600")
    assert client.post("/api/login", json={"code": code}).status_code == 400


def test_login_link_requires_login(client):
    assert client.post("/api/me/login-link").status_code == 401


def test_login_code_guessing_is_rate_limited(client):
    for _ in range(20):
        assert client.post("/api/login", json={"code": "x" * 43}).status_code == 400
    assert client.post("/api/login", json={"code": "x" * 43}).status_code == 429


@pytest.mark.parametrize("code", [None, 5, "", "short", "x" * 101, ["list"]])
def test_login_rejects_bad_codes(client, code):
    assert client.post("/api/login", json={"code": code}).status_code == 400


def test_admin_creates_login_link(app, client):
    _, uid = register(client, 1)
    admin = app.test_client()
    login(admin)
    code = admin.post(f"/api/admin/users/{uid}/login-link").get_json()["code"]
    assert client.post("/api/login", json={"code": code}).status_code == 200
    assert "user.login_link_created" in audit_actions(app)
    assert client.post(f"/api/admin/users/{uid}/login-link").status_code == 401


def test_removing_user_kills_tokens_and_codes(app, client):
    headers, uid = register(client, 1)
    code = client.post("/api/me/login-link", headers=headers).get_json()["code"]
    admin = app.test_client()
    login(admin)
    admin.delete(f"/api/admin/users/{uid}")
    assert client.get("/api/me", headers=headers).status_code == 401
    assert client.post("/api/login", json={"code": code}).status_code == 400
    assert sql(app, "SELECT COUNT(*) FROM user_tokens")[0][0] == 0


def test_tokens_are_stored_hashed(app, client):
    headers, _ = register(client, 1)
    token = headers["X-User-Token"]
    dump = "\n".join(str(tuple(r)) for t in ("users", "user_tokens") for r in sql(app, f"SELECT * FROM {t}"))
    assert token not in dump


# --- input hardening -------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b'{"area":{"width":Infinity,"height":10},"groups":[]}',
    b'{"area":{"width":NaN,"height":10},"groups":[]}',
    b"[" * 50000 + b"]" * 50000,
    b"{not json",
    b"\xff\xfe",
    b'["array"]',
])
def test_malformed_json_is_400(app, raw):
    admin = app.test_client()
    login(admin)
    res = admin.put("/api/admin/layout", data=raw, content_type="application/json")
    assert res.status_code == 400 and "error" in res.get_json()


def test_deep_nesting_unauthenticated(client):
    res = client.post("/api/register", data=b"[" * 50000 + b"]" * 50000, content_type="application/json")
    assert res.status_code == 400


def test_request_size_limit(client):
    res = client.post("/api/register", data=b'{"name":"' + b"A" * 300_000 + b'"}', content_type="application/json")
    assert res.status_code == 413
    assert "error" in res.get_json()


def test_non_json_content_type_rejected(app):
    admin = app.test_client()
    login(admin)
    res = admin.put("/api/admin/settings", data='{"location":"x"}', content_type="text/plain")
    assert res.status_code == 415


def test_api_responses_not_cached(client):
    assert client.get("/api/info").headers["Cache-Control"] == "no-store"
    csp = client.get("/").headers["Content-Security-Policy"]
    for directive in ("object-src 'none'", "base-uri 'none'", "form-action 'self'"):
        assert directive in csp


# --- audit log -------------------------------------------------------------

def test_admin_actions_are_audited(app, client):
    headers, uid = register(client, 1)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    admin = app.test_client()
    login(admin)
    admin.put("/api/admin/settings", json={"paypal_address": "evil@example.com"})
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    admin.post(f"/api/admin/users/{uid}/move", json={"seat": "B2"})
    admin.put("/api/admin/layout", json=admin.get("/api/admin/layout").get_json())
    admin.delete(f"/api/admin/users/{uid}")
    admin.post("/api/admin/logout")

    entries = {e["action"]: e for e in reversed(sql_entries(app))}
    assert entries["settings.changed"]["details"]["paypal_address"] == ["", "evil@example.com"]
    assert entries["payment.confirmed"]["details"]["email"] == "u1@example.com"
    assert entries["user.moved"]["details"]["to"] == "B2"
    assert entries["user.removed"]["details"]["paid"] is True
    assert "layout.saved" in entries and "admin.logout" in entries
    assert all(e["actor"] == "Alice Admin" for k, e in entries.items() if k != "admin.login_failed")


def sql_entries(app):
    with app.test_request_context():
        from lanparty.security import audit_entries
        return audit_entries()


def test_audit_requires_admin(client):
    assert client.get("/api/admin/audit").status_code == 401


def test_actor_name_is_sanitised(app, client):
    assert login(client, name="Bob‮\x00" + "x" * 100).get_json()["actor"] == "Bob" + "x" * 37


# --- migration of databases from older versions ------------------------------

def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE, seat TEXT UNIQUE,
            paid INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            seat_selected_at TEXT, paid_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    import hashlib
    db.execute("INSERT INTO users (name, email, token_hash, seat, seat_selected_at) VALUES "
               "('Old', 'old@example.com', ?, 'A1', datetime('now'))",
               (hashlib.sha256(b"old-token").hexdigest(),))
    db.commit()
    db.close()

    app = set_event_code(create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": ADMIN_PW}))
    me = app.test_client().get("/api/me", headers={"X-User-Token": "old-token"}).get_json()
    assert me["user"]["seat"] == "A1"
    assert me["user"]["reserved_until"] is not None
