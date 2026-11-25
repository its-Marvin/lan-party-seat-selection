import pytest

from tests.helpers import EVENT_CODE, set_event_code

from lanparty import create_app
from lanparty import layout as lay
from lanparty import settings as cfg

ADMIN_PW = "s3cret"


@pytest.fixture
def app(tmp_path):
    return set_event_code(create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "ADMIN_PASSWORD": ADMIN_PW,
    }))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    return c


def register(client, name="Alice", email="alice@example.com"):
    res = client.post("/api/register", json={"name": name, "email": email, "event_code": EVENT_CODE})
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()["user"]


# --- pages & info --------------------------------------------------------

def test_pages_are_served(client):
    assert b"seat-map" in client.get("/").data
    assert b"settings-form" in client.get("/admin").data
    assert client.get("/static/app.js").status_code == 200


def test_info_defaults(client):
    info = client.get("/api/info").get_json()
    assert info["event_name"] == "LAN Party"
    assert info["amount_cents"] == 2000
    assert info["seat_count"] == 32


def test_seat_map_initially_free(client):
    data = client.get("/api/seats").get_json()
    assert len(data["seats"]) == 32
    assert data["seats"][0] == {"id": "A1", "status": "free"}
    assert data["seats"][-1]["id"] == "B16"
    assert [g["label"] for g in data["layout"]["groups"]] == ["A", "B"]
    assert all(s["status"] == "free" for s in data["seats"])


# --- registration --------------------------------------------------------

def test_register_and_me(client):
    headers, user = register(client)
    assert user["name"] == "Alice" and user["seat"] is None and user["paid"] is False
    me = client.get("/api/me", headers=headers).get_json()
    assert me["user"]["email"] == "alice@example.com"
    assert me["payment"] is None


def test_register_normalises_email_and_rejects_duplicates(client):
    register(client, email="Alice@Example.com")
    res = client.post("/api/register", json={"name": "Other", "email": "alice@example.COM ", "event_code": EVENT_CODE})
    assert res.status_code == 409


@pytest.mark.parametrize("payload", [
    {"name": "A", "email": "a@example.com"},
    {"name": "x" * 61, "email": "a@example.com"},
    {"name": "Alice", "email": "not-an-email"},
    {"name": "Alice"},
    {},
])
def test_register_validation(client, payload):
    assert client.post("/api/register", json={**payload, "event_code": EVENT_CODE}).status_code == 400


def test_register_requires_json(client):
    assert client.post("/api/register", data="name=x").status_code == 415


def test_me_requires_valid_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"X-User-Token": "bogus"}).status_code == 401


# --- seat selection ------------------------------------------------------

def test_select_seat_returns_payment_info(client, admin):
    admin.put("/api/admin/settings", json={"paypal_address": "pay@lan.example", "amount": "15,50"})
    headers, _ = register(client)
    res = client.post("/api/me/seat", json={"seat": "b3"}, headers=headers)
    assert res.status_code == 200
    data = res.get_json()
    assert data["user"]["seat"] == "B3"
    assert data["payment"]["paypal_address"] == "pay@lan.example"
    assert data["payment"]["amount_cents"] == 1550
    assert "B3" in data["payment"]["reference"] and "Alice" in data["payment"]["reference"]

    seats = {s["id"]: s for s in client.get("/api/seats", headers=headers).get_json()["seats"]}
    assert seats["B3"] == {"id": "B3", "status": "reserved", "name": "Alice", "mine": True}


def test_seat_map_does_not_leak_emails(client):
    headers, _ = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    assert b"alice@example.com" not in client.get("/api/seats").data


def test_select_seat_requires_registration(client):
    assert client.post("/api/me/seat", json={"seat": "A1"}).status_code == 401


@pytest.mark.parametrize("seat", ["Z1", "A17", "A0", "AA1", "", None, 5, "A1; DROP"])
def test_select_invalid_seat(client, seat):
    headers, _ = register(client)
    assert client.post("/api/me/seat", json={"seat": seat}, headers=headers).status_code == 400


def test_seat_cannot_be_taken_twice(client):
    h1, _ = register(client, "Alice", "a@example.com")
    h2, _ = register(client, "Bob", "b@example.com")
    assert client.post("/api/me/seat", json={"seat": "A1"}, headers=h1).status_code == 200
    res = client.post("/api/me/seat", json={"seat": "A1"}, headers=h2)
    assert res.status_code == 409
    assert client.get("/api/me", headers=h2).get_json()["user"]["seat"] is None


def test_user_can_change_and_release_seat(client):
    headers, _ = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    client.post("/api/me/seat", json={"seat": "A2"}, headers=headers)
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["A1"] == "free" and seats["A2"] == "reserved"

    res = client.delete("/api/me/seat", headers=headers)
    assert res.status_code == 200 and res.get_json()["user"]["seat"] is None


def test_paid_user_cannot_change_seat(client, admin):
    headers, user = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    admin.post(f"/api/admin/users/{user['id']}/paid", json={"paid": True})
    assert client.post("/api/me/seat", json={"seat": "A2"}, headers=headers).status_code == 409
    assert client.delete("/api/me/seat", headers=headers).status_code == 409


# --- admin auth ----------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/admin/users"),
    ("get", "/api/admin/settings"),
    ("put", "/api/admin/settings"),
    ("post", "/api/admin/users/1/paid"),
    ("post", "/api/admin/users/1/move"),
    ("delete", "/api/admin/users/1"),
])
def test_admin_endpoints_require_login(client, method, path):
    assert getattr(client, method)(path, json={}).status_code == 401


def test_user_token_does_not_grant_admin(client):
    headers, _ = register(client)
    assert client.get("/api/admin/users", headers=headers).status_code == 401


def test_admin_login_wrong_password(client):
    assert client.post("/api/admin/login", json={"password": "nope"}).status_code == 401
    assert client.get("/api/admin/session").get_json()["admin"] is False


def test_admin_login_disabled_without_password(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "x.sqlite3"), "ADMIN_PASSWORD": "", "SECRET_KEY": "t"})
    res = app.test_client().post("/api/admin/login", json={"password": ""})
    assert res.status_code == 503


def test_admin_logout(admin):
    assert admin.get("/api/admin/session").get_json() == {"admin": True, "actor": "admin"}
    admin.post("/api/admin/logout")
    assert admin.get("/api/admin/users").status_code == 401


# --- admin actions -------------------------------------------------------

def test_admin_lists_users(client, admin):
    register(client, "Alice", "a@example.com")
    register(client, "Bob", "b@example.com")
    users = admin.get("/api/admin/users").get_json()["users"]
    assert [u["name"] for u in users] == ["Alice", "Bob"]
    assert users[0]["email"] == "a@example.com"
    assert "token_hash" not in users[0]


def test_admin_confirm_and_revoke_payment(client, admin):
    headers, user = register(client)
    client.post("/api/me/seat", json={"seat": "B4"}, headers=headers)

    res = admin.post(f"/api/admin/users/{user['id']}/paid", json={"paid": True})
    assert res.status_code == 200
    assert res.get_json()["user"]["paid"] is True
    assert res.get_json()["user"]["paid_at"]
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["B4"] == "paid"

    res = admin.post(f"/api/admin/users/{user['id']}/paid", json={"paid": False})
    assert res.get_json()["user"]["paid"] is False
    assert res.get_json()["user"]["paid_at"] is None


def test_admin_paid_validation(client, admin):
    _, user = register(client)
    assert admin.post(f"/api/admin/users/{user['id']}/paid", json={"paid": "yes"}).status_code == 400
    assert admin.post("/api/admin/users/999/paid", json={"paid": True}).status_code == 404


def test_admin_move_user(client, admin):
    h1, alice = register(client, "Alice", "a@example.com")
    h2, _ = register(client, "Bob", "b@example.com")
    client.post("/api/me/seat", json={"seat": "A1"}, headers=h1)
    client.post("/api/me/seat", json={"seat": "A2"}, headers=h2)
    admin.post(f"/api/admin/users/{alice['id']}/paid", json={"paid": True})

    # moving onto an occupied seat fails
    assert admin.post(f"/api/admin/users/{alice['id']}/move", json={"seat": "A2"}).status_code == 409
    # moving a paid user works and keeps the paid flag
    res = admin.post(f"/api/admin/users/{alice['id']}/move", json={"seat": "B16"})
    assert res.status_code == 200
    assert res.get_json()["user"]["seat"] == "B16" and res.get_json()["user"]["paid"] is True
    # free the seat without removing the person
    res = admin.post(f"/api/admin/users/{alice['id']}/move", json={"seat": None})
    assert res.get_json()["user"]["seat"] is None
    # invalid requests
    assert admin.post(f"/api/admin/users/{alice['id']}/move", json={}).status_code == 400
    assert admin.post(f"/api/admin/users/{alice['id']}/move", json={"seat": "Q99"}).status_code == 400
    assert admin.post("/api/admin/users/999/move", json={"seat": "A3"}).status_code == 404


def test_admin_remove_user(client, admin):
    headers, user = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    assert admin.delete(f"/api/admin/users/{user['id']}").status_code == 200
    assert admin.get("/api/admin/users").get_json()["users"] == []
    assert client.get("/api/me", headers=headers).status_code == 401
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["A1"] == "free"
    assert admin.delete(f"/api/admin/users/{user['id']}").status_code == 404
    # the email can register again afterwards
    register(client)


# --- settings ------------------------------------------------------------

def test_admin_update_settings(client, admin):
    res = admin.put("/api/admin/settings", json={
        "event_name": "Frag Fest",
        "event_start": "2026-12-12T14:00",
        "event_end": "2026-12-14T12:00",
        "location": "Community Hall",
        "description": "Bring your own PC.\nPower strips provided.",
        "paypal_address": "https://paypal.me/fragfest",
        "amount": 25,
    })
    assert res.status_code == 200, res.get_json()
    info = client.get("/api/info").get_json()
    assert info["event_name"] == "Frag Fest"
    assert info["location"] == "Community Hall"
    assert info["amount_cents"] == 2500
    assert info["paypal_address"] == "https://paypal.me/fragfest"


def test_partial_settings_update_keeps_other_values(admin):
    admin.put("/api/admin/settings", json={"location": "Garage"})
    admin.put("/api/admin/settings", json={"amount": "10"})
    s = admin.get("/api/admin/settings").get_json()
    assert s["location"] == "Garage" and s["amount_cents"] == 1000


@pytest.mark.parametrize("payload", [
    {"event_name": ""},
    {"amount": "abc"},
    {"amount": -1},
    {"amount": "1.234"},
    {"amount": None},
    {"event_start": "next friday"},
    {"event_start": "2026-12-12T14:00", "event_end": "2026-12-11T14:00"},
    {"paypal_address": "not an address"},
    {"description": "x" * 5001},
])
def test_settings_validation(admin, payload):
    res = admin.put("/api/admin/settings", json=payload)
    assert res.status_code == 400, payload


def test_security_headers(client):
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]


# --- unit tests for helpers ----------------------------------------------

@pytest.mark.parametrize("value,cents", [
    ("20", 2000), ("20.5", 2050), ("20,50", 2050), (" 7 € ", 700), (0, 0), (12.25, 1225),
])
def test_parse_amount(value, cents):
    assert cfg.parse_amount(value) == cents
