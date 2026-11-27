"""Admin accounts: start mode with ADMIN_PASSWORD, one account per admin,
password changes, removal, and resetting everything from the console."""

import pytest

from lanparty import create_app, manage
from lanparty.db import get_db

START_PW = "correct horse battery"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "admins.sqlite3"), "ADMIN_PASSWORD": START_PW})


def login(app, name, password):
    client = app.test_client()
    res = client.post("/api/admin/login", json={"name": name, "password": password})
    return client, res


def logged_in(client):
    return client.get("/api/admin/session").get_json()["admin"]


def sql(app, query, params=()):
    with app.app_context():
        db = get_db()
        rows = db.execute(query, params).fetchall()
        db.commit()
        return rows


@pytest.fixture
def first(app):
    """Start mode: log in with ADMIN_PASSWORD and create the first account."""
    starter, res = login(app, "Setup", START_PW)
    assert res.status_code == 200
    res = starter.post("/api/admin/admins", json={"name": "Alice", "password": "alice password 1", "current": START_PW})
    assert res.status_code == 201 and res.get_json()["start_mode_ended"] is True
    assert not logged_in(starter)            # start-mode sessions end with the first account
    alice, res = login(app, "alice", "alice password 1")     # names ignore case
    assert res.status_code == 200 and res.get_json()["actor"] == "Alice"
    return alice


def test_start_mode_accepts_the_environment_password_with_any_name(app):
    client, res = login(app, "Whoever", START_PW)
    assert res.status_code == 200
    data = client.get("/api/admin/admins").get_json()
    assert data == {"admins": [], "start_mode": True, "current_id": None}


def test_the_first_account_ends_the_environment_password(app, first):
    _, res = login(app, "Setup", START_PW)
    assert res.status_code == 401 and res.get_json()["error"] == "Wrong name or password."
    [row] = sql(app, "SELECT * FROM admin_users")
    assert row["name"] == "Alice" and row["password_hash"].startswith("scrypt$")
    assert "alice password 1" not in row["password_hash"] and row["created_by"] == "Setup"


def test_creating_an_admin_needs_name_password_and_your_own_password(app, first):
    def create(**data):
        return first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12",
                                                     "current": "alice password 1", **data})
    assert create(current="wrong").status_code == 403
    assert create(name="").status_code == 400
    assert create(name="B").status_code == 400
    assert create(password="").status_code == 400
    assert create(password="too short").status_code == 400
    assert create(password="bob").status_code == 400
    assert create(name="bob password 12").status_code == 400          # password = name
    assert create().status_code == 201
    assert create(name="BOB").status_code == 409                      # names are unique ignoring case
    bob, res = login(app, "Bob", "bob password 12")
    assert res.status_code == 200 and logged_in(bob)
    names = [a["name"] for a in first.get("/api/admin/admins").get_json()["admins"]]
    assert names == ["Alice", "Bob"]


def test_each_admin_logs_in_with_their_own_password(app, first):
    first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12", "current": "alice password 1"})
    assert login(app, "Bob", "alice password 1")[1].status_code == 401
    assert login(app, "Nobody", "alice password 1")[1].status_code == 401
    assert login(app, "Bob", "bob password 12")[1].status_code == 200


def test_changing_your_password_ends_only_your_other_sessions(app, first):
    first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12", "current": "alice password 1"})
    alice_laptop, _ = login(app, "Alice", "alice password 1")
    bob, _ = login(app, "Bob", "bob password 12")
    assert first.post("/api/admin/password", json={"current": "wrong", "new": "x" * 12}).status_code == 403
    assert first.post("/api/admin/password", json={"current": "alice password 1", "new": "alice password 1"}).status_code == 400
    assert first.post("/api/admin/password", json={"current": "alice password 1", "new": "alice password 2"}).status_code == 200
    assert logged_in(first)                  # this device stays logged in
    assert not logged_in(alice_laptop)       # Alice's other sessions end
    assert logged_in(bob)                    # other admins are not affected
    assert login(app, "Alice", "alice password 1")[1].status_code == 401
    assert login(app, "Alice", "alice password 2")[1].status_code == 200


def test_start_mode_has_no_password_to_change(app):
    client, _ = login(app, "Setup", START_PW)
    assert client.post("/api/admin/password", json={"current": START_PW, "new": "x" * 12}).status_code == 409


def test_removing_an_admin_ends_their_sessions(app, first):
    first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12", "current": "alice password 1"})
    bob, _ = login(app, "Bob", "bob password 12")
    admins = {a["name"]: a for a in first.get("/api/admin/admins").get_json()["admins"]}
    assert admins["Alice"]["current"] and not admins["Bob"]["current"]
    assert first.delete(f"/api/admin/admins/{admins['Bob']['id']}", json={"current": "wrong"}).status_code == 403
    assert first.delete(f"/api/admin/admins/{admins['Alice']['id']}",
                        json={"current": "alice password 1"}).status_code == 409    # not yourself
    assert first.delete(f"/api/admin/admins/{admins['Bob']['id']}",
                        json={"current": "alice password 1"}).status_code == 200
    assert not logged_in(bob)
    assert login(app, "Bob", "bob password 12")[1].status_code == 401


def test_reset_removes_every_admin_and_brings_back_start_mode(app, first):
    first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12", "current": "alice password 1"})
    assert manage.reset_admin_password(app) == 0
    assert not logged_in(first)
    assert sql(app, "SELECT COUNT(*) AS n FROM admin_users")[0]["n"] == 0
    assert login(app, "Alice", "alice password 1")[1].status_code == 401
    assert login(app, "Setup", START_PW)[1].status_code == 200


def test_failed_password_confirmations_count_as_failed_logins(app, first):
    for _ in range(app.config["ADMIN_LOGIN_MAX_PER_IP"]):
        first.post("/api/admin/password", json={"current": "wrong", "new": "x" * 12})
    res = first.post("/api/admin/password", json={"current": "alice password 1", "new": "alice password 2"})
    assert res.status_code == 429


def test_account_actions_are_audited(app, first):
    first.post("/api/admin/admins", json={"name": "Bob", "password": "bob password 12", "current": "alice password 1"})
    actions = [r["action"] for r in sql(app, "SELECT action FROM audit_log ORDER BY id")]
    assert "admin.account_created" in actions
    assert sql(app, "SELECT actor FROM audit_log WHERE action = 'admin.account_created' ORDER BY id")[-1]["actor"] == "Alice"
    secrets = " ".join(r["details"] for r in sql(app, "SELECT details FROM audit_log"))
    assert "password 1" not in secrets and "bob password" not in secrets


def test_security_check_reports_start_mode_and_accounts(app, first):
    checks = {c["id"]: c for c in first.get("/api/admin/security/checks").get_json()["checks"]}
    assert checks["accounts"]["level"] == "ok"


@pytest.mark.sqlite_only
def test_old_session_table_gets_the_admin_column(tmp_path):
    """Databases from before admin accounts: admin_sessions has no admin_id yet."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": START_PW})
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE admin_sessions")
    conn.execute("CREATE TABLE admin_sessions (token_hash TEXT PRIMARY KEY, actor TEXT NOT NULL, "
                 "password_tag TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL)")
    conn.commit()
    conn.close()
    app = create_app({"TESTING": True, "DATABASE": str(path), "ADMIN_PASSWORD": START_PW})
    client, res = login(app, "Setup", START_PW)
    assert res.status_code == 200 and logged_in(client)
