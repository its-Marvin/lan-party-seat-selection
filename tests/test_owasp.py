"""Regression tests for the OWASP Top 10 review: rate limits per IPv6 network, audit
log privacy and retention, participant logout, unstorable text, mailto-safe emails,
503 on database outages, and the supply-chain and container settings."""

import json
from pathlib import Path
import re
import sqlite3

import pytest

from lanparty import create_app
from lanparty import events as ev
from lanparty import security as sec
from tests.helpers import EVENT_CODE, set_event_code

ROOT = Path(__file__).parent.parent
ADMIN_PW = "correct horse battery"


def make_app(tmp_path, **overrides):
    config = {"TESTING": True, "DATABASE": str(tmp_path / "owasp.sqlite3"), "ADMIN_PASSWORD": ADMIN_PW}
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


def register(client, n, addr="10.1.0.1"):
    res = client.post("/api/register", json={"name": f"User{n}", "email": f"u{n}@example.com",
                                             "event_code": EVENT_CODE}, **ip(addr))
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()["user"]["id"]


def admin_login(client, addr="10.0.0.1"):
    res = client.post("/api/admin/login", json={"name": "Alice Admin", "password": ADMIN_PW}, **ip(addr))
    assert res.status_code == 200
    return res


def sql(app, query, params=()):
    with app.app_context():
        db = sec.get_db()
        cur = db.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        db.commit()
        return rows


def audit(app, action):
    return [json.loads(r["details"]) for r in
            sql(app, "SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,))]


# --- A06/A07: rate limits count per IPv6 /64, not per address -------------------

@pytest.mark.parametrize("addr, network", [
    ("192.0.2.7", "192.0.2.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("::ffff:192.0.2.7", "192.0.2.7"),           # IPv4-mapped: the IPv4 client
    ("fe80::1%eth0", "fe80::/64"),
    ("unknown", "unknown"),
])
def test_client_network(addr, network):
    assert sec.client_network(addr) == network


def test_wrong_event_codes_are_limited_per_ipv6_network(app, client):
    """Rotating through addresses of one /64 used to give unlimited guesses."""
    limit = app.config["EVENT_CODE_MAX_PER_IP"]
    for n in range(limit):
        res = client.post("/api/event-code", json={"code": "WRONG1"}, **ip(f"2001:db8:0:1::{n + 1:x}"))
        assert res.status_code == 400
    res = client.post("/api/event-code", json={"code": "WRONG1"}, **ip("2001:db8:0:1:dead:beef::99"))
    assert res.status_code == 429
    # even the right code is not checked any more from that network ...
    res = client.post("/api/event-code", json={"code": EVENT_CODE}, **ip("2001:db8:0:1::abcd"))
    assert res.status_code == 429
    # ... while the neighbouring /64 is unaffected
    res = client.post("/api/event-code", json={"code": EVENT_CODE}, **ip("2001:db8:0:2::1"))
    assert res.status_code == 200


def test_pending_reservations_are_limited_per_ipv6_network(app, client):
    limit = app.config["MAX_PENDING_PER_IP"]
    tokens = [register(client, n, addr=f"10.2.0.{n + 1}")[0] for n in range(limit + 1)]
    for n, headers in enumerate(tokens[:limit]):
        res = client.post("/api/me/seat", json={"seat": f"A{n + 1}"}, headers=headers, **ip(f"2001:db8:5:5::{n + 1}"))
        assert res.status_code == 200
    res = client.post("/api/me/seat", json={"seat": "A9"}, headers=tokens[limit], **ip("2001:db8:5:5:1234::1"))
    assert res.status_code == 429


def test_registrations_are_limited_per_ipv6_network(app, client):
    limit = app.config["REGISTER_MAX_PER_IP"]
    for n in range(limit):
        register(client, n, addr=f"2001:db8:7:7::{n + 1}")
    res = client.post("/api/register", json={"name": "One more", "email": "more@example.com",
                                             "event_code": EVENT_CODE}, **ip("2001:db8:7:7:ffff::1"))
    assert res.status_code == 429


def test_admin_logins_are_limited_per_ipv6_network(app, client):
    limit = app.config["ADMIN_LOGIN_MAX_PER_IP"]
    for n in range(limit):
        res = client.post("/api/admin/login", json={"name": "Mallory", "password": "wrong"},
                          **ip(f"2001:db8:9:9::{n + 1}"))
        assert res.status_code == 401
    res = client.post("/api/admin/login", json={"name": "Mallory", "password": ADMIN_PW}, **ip("2001:db8:9:9::ff"))
    assert res.status_code == 429


# --- A09: the audit log keeps no full IPs, notices code guessing, and expires -----

def test_admin_logins_log_only_the_network(app, client):
    admin_login(client, addr="2001:db8:abcd:12:1:2:3:4")
    client.post("/api/admin/login", json={"name": "Mallory", "password": "wrong"}, **ip("198.51.100.23"))
    assert audit(app, "admin.login") == [{"network": "2001:db8:abcd::/48"}]
    assert audit(app, "admin.login_failed") == [{"network": "198.51.100.0/24", "name": "Mallory"}]
    everything = " ".join(r["details"] for r in sql(app, "SELECT details FROM audit_log"))
    assert "198.51.100.23" not in everything and "1:2:3:4" not in everything


def test_full_ips_logged_by_older_versions_are_anonymised(tmp_path):
    app = make_app(tmp_path)
    sql(app, "INSERT INTO audit_log (actor, action, details) VALUES (?, ?, ?)",
        ("anonymous", "admin.login_failed", json.dumps({"ip": "203.0.113.77", "name": "Old"})))
    sql(app, "INSERT INTO audit_log (actor, action, details) VALUES (?, ?, ?)",
        ("Bob", "admin.login", json.dumps({"ip": "2001:db8:1:2::5"})))
    make_app(tmp_path)   # restart: migration runs
    assert audit(app, "admin.login_failed") == [{"name": "Old", "network": "203.0.113.0/24"}]
    assert audit(app, "admin.login") == [{"network": "2001:db8:1::/48"}]


def test_reaching_the_event_code_limit_is_audited_once(app, client):
    limit = app.config["EVENT_CODE_MAX_PER_IP"]
    for _ in range(limit + 3):
        client.post("/api/event-code", json={"code": "WRONG1"}, **ip("203.0.113.9"))
    entries = audit(app, "event_code.limit_reached")
    assert len(entries) == 1
    assert entries[0]["network"] == "203.0.113.0/24" and entries[0]["attempts"] == limit
    # the admin page highlights it
    assert '"event_code.limit_reached"' in (ROOT / "lanparty/static/admin-security.js").read_text()


def test_old_audit_entries_are_deleted(app, client):
    sql(app, "INSERT INTO audit_log (ts, actor, action, details) VALUES (?, ?, ?, ?)",
        ("2020-01-01 00:00:00", "system", "ancient.entry", ""))
    admin_login(client)
    actions = [e["action"] for e in client.get("/api/admin/audit").get_json()["entries"]]
    assert "ancient.entry" not in actions and "admin.login" in actions


def test_audit_retention_can_be_switched_off(tmp_path):
    app = make_app(tmp_path, AUDIT_LOG_RETENTION_DAYS=0)
    client = app.test_client()
    sql(app, "INSERT INTO audit_log (ts, actor, action, details) VALUES (?, ?, ?, ?)",
        ("2020-01-01 00:00:00", "system", "ancient.entry", ""))
    admin_login(client)
    actions = [e["action"] for e in client.get("/api/admin/audit").get_json()["entries"]]
    assert "ancient.entry" in actions


# --- A07: participants can log devices out --------------------------------------

def test_logout_invalidates_the_token_on_the_server(client):
    headers, _ = register(client, 1)
    assert client.post("/api/me/logout", headers=headers).status_code == 200
    assert client.get("/api/me", headers=headers).status_code == 401
    assert client.post("/api/me/logout", headers=headers).status_code == 401


def test_logout_others_ends_other_devices_and_unused_login_links(app, client):
    phone, user_id = register(client, 1)
    code = client.post("/api/me/login-link", headers=phone).get_json()["code"]
    laptop = {"X-User-Token": client.post("/api/login", json={"code": code}).get_json()["token"]}
    unused = client.post("/api/me/login-link", headers=phone).get_json()["code"]

    # the phone is lost: log everything else out from the laptop
    res = client.post("/api/me/logout-others", headers=laptop)
    assert res.status_code == 200 and res.get_json()["devices"] == 1
    assert client.get("/api/me", headers=phone).status_code == 401
    assert client.get("/api/me", headers=laptop).status_code == 200
    assert client.post("/api/login", json={"code": unused}).status_code == 400
    [entry] = audit(app, "user.logged_out_elsewhere")
    assert entry["user_id"] == user_id and entry["devices"] == 1 and entry["login_links"] == 1


def test_logout_cannot_touch_other_participants(client):
    alice, _ = register(client, 1)
    bob, _ = register(client, 2)
    client.post("/api/me/logout-others", headers=alice)
    assert client.get("/api/me", headers=bob).status_code == 200


def test_logout_buttons_are_on_the_page():
    page = (ROOT / "lanparty/static/index.html").read_text()
    assert 'id="logout"' in page and 'id="logout-others"' in page
    script = (ROOT / "lanparty/static/app.js").read_text()
    assert '"/me/logout"' in script and '"/me/logout-others"' in script


# --- A05/A10: input that could misbehave -----------------------------------------

@pytest.mark.parametrize("body", [
    '{"name": "Alice", "password": "\\ud800"}',                # lone surrogate: used to be a 500
    '{"name": "Alice", "password": "x", "extra": ["\\udfff"]}',
])
def test_lone_surrogates_are_rejected(client, body):
    res = client.post("/api/admin/login", data=body, content_type="application/json")
    assert res.status_code == 400
    assert "invalid Unicode" in res.get_json()["error"]


def test_proper_surrogate_pairs_are_fine(client):
    res = client.post("/api/register", data=json.dumps(
        {"name": "Gamer \U0001F3AE", "email": "emoji@example.com", "event_code": EVENT_CODE}),
        content_type="application/json")
    assert res.status_code == 201
    assert res.get_json()["user"]["name"] == "Gamer \U0001F3AE"


@pytest.mark.parametrize("email", [
    "a@example.com?subject=Refund&body=Please",   # the old check accepted this
    "a@example.com#x",
    "a%20b@example.com",
    'a"b@example.com',
    "<a@example.com>",
    "a@example.com/x",
])
def test_emails_cannot_carry_mailto_parameters(client, email):
    res = client.post("/api/register", json={"name": "Eve", "email": email, "event_code": EVENT_CODE})
    assert res.status_code == 400


@pytest.mark.parametrize("email", ["o'brien&co@example.ie", "max.müller+lan@straße.de"])
def test_unusual_but_valid_emails_still_work(client, email):
    res = client.post("/api/register", json={"name": "Valid", "email": email, "event_code": EVENT_CODE})
    assert res.status_code == 201


def test_admin_mail_links_escape_the_address():
    script = (ROOT / "lanparty/static/admin-events.js").read_text()
    assert "mailtoHref(u.email)" in script and "`mailto:${" not in script


def test_admin_password_of_another_length_is_simply_wrong(client):
    for password in ("", "x", ADMIN_PW + "x", ADMIN_PW[:-1], ADMIN_PW * 20):
        res = client.post("/api/admin/login", json={"name": "Alice", "password": password})
        assert res.status_code == 401


def test_database_unavailable_is_503(app, monkeypatch):
    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ev, "active_id", locked)
    res = app.test_client().get("/api/info")
    assert res.status_code == 503 and res.headers["Retry-After"] == "5"
    assert "try again" in res.get_json()["error"]


def test_postgres_outage_is_503(app, monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    def down():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(ev, "active_id", down)
    assert app.test_client().get("/api/info").status_code == 503


def test_a_broken_query_is_still_a_500(app, monkeypatch):
    def bug():
        raise sqlite3.OperationalError("no such table: nonsense")

    monkeypatch.setattr(ev, "active_id", bug)
    assert app.test_client().get("/api/info").status_code == 500


# --- A03/A02: supply chain and container hardening ------------------------------

def test_workflow_actions_are_pinned_to_commits():
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for ref in re.findall(r"uses:\s*(\S+)", workflow.read_text()):
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{workflow.name}: {ref}"


def test_dependabot_watches_every_ecosystem():
    config = (ROOT / ".github/dependabot.yml").read_text()
    for ecosystem in ("pip", "docker", "docker-compose", "github-actions"):
        assert f"package-ecosystem: {ecosystem}\n" in config


def test_dependencies_are_audited_in_ci():
    tests = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "pip-audit -r requirements-docker.txt" in tests
    assert "schedule:" in tests          # also when nothing is pushed
    dockerfile = (ROOT / "Dockerfile").read_text()
    # the image installs exactly what is audited
    assert "pip install -r requirements-docker.txt" in dockerfile
    assert "-r requirements.txt" in (ROOT / "requirements-docker.txt").read_text()
    assert re.search(r"^FROM python:[\w.-]+@sha256:[0-9a-f]{64}$", dockerfile, re.M)


def test_compose_drops_privileges():
    compose = (ROOT / "docker-compose.yml").read_text()
    app_service = compose.split("\n  app:\n", 1)[1]
    db_service = compose.split("\n  db:\n", 1)[1].split("\n  app:\n", 1)[0]
    for needed in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "tmpfs:"):
        assert needed in app_service
    assert "cap_drop: [ALL]" in db_service and "no-new-privileges:true" in db_service


# --- PR review: revocation must stick ----------------------------------------------

def test_logged_out_token_stays_out_after_a_restart(tmp_path):
    """The legacy users.token_hash copy used to be put back into user_tokens at startup."""
    app = make_app(tmp_path)
    client = app.test_client()
    headers, _ = register(client, 1)
    assert client.post("/api/me/logout", headers=headers).status_code == 200
    restarted = make_app(tmp_path)
    assert restarted.test_client().get("/api/me", headers=headers).status_code == 401


def test_devices_logged_out_elsewhere_stay_out_after_a_restart(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    phone, _ = register(client, 1)      # the registering device: its hash is also in users.token_hash
    code = client.post("/api/me/login-link", headers=phone).get_json()["code"]
    laptop = {"X-User-Token": client.post("/api/login", json={"code": code}).get_json()["token"]}
    assert client.post("/api/me/logout-others", headers=laptop).get_json()["devices"] == 1
    restarted = make_app(tmp_path).test_client()
    assert restarted.get("/api/me", headers=phone).status_code == 401
    assert restarted.get("/api/me", headers=laptop).status_code == 200


def _while_creating_a_secret(monkeypatch, action):
    """Run ``action`` in another thread the first time the current request creates a
    secret (a token or a login code), i.e. in the middle of that request. Waits
    briefly: a request that holds the write lock makes the other one wait for it."""
    import threading

    real = sec.new_secret
    main = threading.current_thread()
    state = {"thread": None}

    def hooked():
        if threading.current_thread() is main and state["thread"] is None:
            state["thread"] = threading.Thread(target=action)
            state["thread"].start()
            state["thread"].join(timeout=0.5)
        return real()

    monkeypatch.setattr(sec, "new_secret", hooked)
    return state


def test_login_racing_with_logout_others_ends_logged_out(app, client, monkeypatch):
    """Redeeming a code while the owner logs out other devices: the new device must
    not end up logged in."""
    owner, _ = register(client, 1)
    code = client.post("/api/me/login-link", headers=owner).get_json()["code"]
    results = {}
    state = _while_creating_a_secret(monkeypatch, lambda: results.setdefault(
        "logout", app.test_client().post("/api/me/logout-others", headers=owner).status_code))
    res = client.post("/api/login", json={"code": code})
    state["thread"].join()
    monkeypatch.undo()
    assert results["logout"] == 200
    if res.status_code == 200:
        assert client.get("/api/me", headers={"X-User-Token": res.get_json()["token"]}).status_code == 401
    else:
        assert res.status_code == 400


def test_login_link_racing_with_logout_others_is_void(app, client, monkeypatch):
    """A device that is being logged out must not get a working login link out of it."""
    phone, _ = register(client, 1)
    code = client.post("/api/me/login-link", headers=phone).get_json()["code"]
    laptop = {"X-User-Token": client.post("/api/login", json={"code": code}).get_json()["token"]}
    results = {}
    state = _while_creating_a_secret(monkeypatch, lambda: results.setdefault(
        "logout", app.test_client().post("/api/me/logout-others", headers=laptop).status_code))
    res = client.post("/api/me/login-link", headers=phone)
    state["thread"].join()
    monkeypatch.undo()
    assert results["logout"] == 200
    if res.status_code == 200:
        assert client.post("/api/login", json={"code": res.get_json()["code"]}).status_code == 400
    else:
        assert res.status_code == 401
    assert client.get("/api/me", headers=phone).status_code == 401


def test_a_logged_out_device_cannot_create_login_links(client):
    phone, _ = register(client, 1)
    client.post("/api/me/logout", headers=phone)
    assert client.post("/api/me/login-link", headers=phone).status_code == 401


def test_parallel_final_guesses_audit_the_lockout_once(app):
    """Both of two parallel last wrong guesses see the full count; only one may audit."""
    from lanparty import api

    limit = app.config["EVENT_CODE_MAX_PER_IP"]
    with app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.50"}):
        key = sec.ip_hash()
        for _ in range(limit):
            assert sec.consume("event_code_fail", key, limit, api.EVENT_CODE_WINDOW)[0] == 0
        api.audit_event_code_lockout(key, limit)
        api.audit_event_code_lockout(key, limit)
    assert len(audit(app, "event_code.limit_reached")) == 1


def test_logout_hides_the_card_before_refreshing():
    """If the refresh after logging out fails, name, email and login link must
    already be gone."""
    script = (ROOT / "lanparty/static/app.js").read_text()
    body = script.split("async function logout()", 1)[1].split("async function logoutOthers()", 1)[0]
    assert body.index("renderMe()") < body.index("await refresh()")
