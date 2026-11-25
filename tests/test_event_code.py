"""Event code: hides names on the seat plan and gates registration."""

import pytest

from lanparty import create_app

ADMIN_PW = "correct horse battery"
CODE = "LAN26"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "code.sqlite3"), "ADMIN_PASSWORD": ADMIN_PW})


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    return c


@pytest.fixture
def coded(app, admin):
    """App with an event code configured and one seated participant."""
    assert admin.put("/api/admin/settings", json={"event_code": CODE.lower()}).status_code == 200
    c = app.test_client()
    res = c.post("/api/register", json={"name": "Alice", "email": "alice@example.com", "event_code": CODE})
    assert res.status_code == 201, res.get_json()
    headers = {"X-User-Token": res.get_json()["token"]}
    c.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    return headers


def seat(client, seat_id, **kwargs):
    data = client.get("/api/seats", **kwargs).get_json()
    return data["names_visible"], next(s for s in data["seats"] if s["id"] == seat_id)


def ip(addr):
    return {"environ_base": {"REMOTE_ADDR": addr}}


# --- secure by default: without a code, registration is closed ------------------

def test_no_code_configured_means_closed(app):
    c = app.test_client()
    assert c.get("/api/info").get_json()["registration_open"] is False
    for code in (None, "", "ANY1"):
        body = {"name": "Bob", "email": "b@example.com"}
        if code is not None:
            body["event_code"] = code
        res = c.post("/api/register", json=body)
        assert res.status_code == 403 and "opened" in res.get_json()["error"]
    assert c.post("/api/event-code", json={"code": "WHATEVER"}).status_code == 403
    assert c.get("/api/seats").get_json()["names_visible"] is False


def test_closed_registration_does_not_count_as_guessing(app):
    c = app.test_client()
    for _ in range(15):
        c.post("/api/event-code", json={"code": "GUESS"})
    with app.app_context():
        from lanparty.db import get_db
        assert get_db().execute("SELECT COUNT(*) FROM rate_events WHERE bucket = 'event_code_fail'").fetchone()[0] == 0


def test_info_never_leaks_the_code(app, coded):
    info = app.test_client().get("/api/info").get_json()
    assert info["registration_open"] is True
    assert "event_code" not in info
    assert CODE not in app.test_client().get("/api/info").get_data(as_text=True)


# --- names on the seat plan -----------------------------------------------------

def test_names_hidden_without_code(app, coded):
    visible, a1 = seat(app.test_client(), "A1")
    assert visible is False
    assert a1 == {"id": "A1", "status": "reserved", "mine": False}   # status still visible


def test_names_visible_with_code(app, coded):
    visible, a1 = seat(app.test_client(), "A1", headers={"X-Event-Code": " lan26 "})
    assert visible is True and a1["name"] == "Alice"


def test_wrong_code_keeps_names_hidden(app, coded):
    visible, a1 = seat(app.test_client(), "A1", headers={"X-Event-Code": "NOPE1"})
    assert visible is False and "name" not in a1


def test_participants_and_admins_see_names_without_code(app, admin, coded):
    assert seat(app.test_client(), "A1", headers=coded)[0] is True
    assert seat(admin, "A1")[0] is True


def test_changed_code_locks_old_code_holders(app, admin, coded):
    admin.put("/api/admin/settings", json={"event_code": "NEW123"})
    assert seat(app.test_client(), "A1", headers={"X-Event-Code": CODE})[0] is False
    assert seat(app.test_client(), "A1", headers={"X-Event-Code": "new123"})[0] is True
    assert seat(app.test_client(), "A1", headers=coded)[0] is True   # registered users unaffected


def test_removing_code_closes_registration(app, admin, coded):
    admin.put("/api/admin/settings", json={"event_code": ""})
    c = app.test_client()
    assert seat(c, "A1")[0] is False
    assert seat(c, "A1", headers={"X-Event-Code": CODE})[0] is False      # old code is useless
    assert seat(c, "A1", headers=coded)[0] is True                        # participants still see names
    res = c.post("/api/register", json={"name": "Bob", "email": "b@example.com", "event_code": CODE})
    assert res.status_code == 403


# --- verification endpoint ------------------------------------------------------

def test_verify_endpoint(app, coded):
    c = app.test_client()
    assert c.post("/api/event-code", json={"code": "lan26"}).get_json() == {"ok": True}
    assert c.post("/api/event-code", json={"code": "WRONG"}).status_code == 400


@pytest.mark.parametrize("code", [None, 12345, "", ["LAN26"], "X" * 5000])
def test_verify_rejects_odd_input(app, coded, code):
    assert app.test_client().post("/api/event-code", json={"code": code}).status_code == 400


# --- registration -----------------------------------------------------------------

@pytest.mark.parametrize("payload_code", [None, "", "WRONG", "LAN2"])
def test_registration_requires_code(app, coded, payload_code):
    body = {"name": "Mallory", "email": "m@example.com"}
    if payload_code is not None:
        body["event_code"] = payload_code
    res = app.test_client().post("/api/register", json=body)
    assert res.status_code == 403


def test_login_link_works_without_code(app, coded):
    c = app.test_client()
    code = c.post("/api/me/login-link", headers=coded).get_json()["code"]
    token = c.post("/api/login", json={"code": code}).get_json()["token"]
    assert seat(c, "A1", headers={"X-User-Token": token})[0] is True


# --- brute force protection ----------------------------------------------------------

def test_code_guessing_is_rate_limited(app, coded):
    c = app.test_client()
    for n in range(10):
        assert c.post("/api/event-code", json={"code": f"GUES{n}"}, **ip("10.5.0.1")).status_code == 400
    res = c.post("/api/event-code", json={"code": CODE}, **ip("10.5.0.1"))   # right code, but locked
    assert res.status_code == 429 and "Retry-After" in res.headers
    # registration from that IP is blocked as well
    res = c.post("/api/register", json={"name": "Eve", "email": "e@example.com", "event_code": CODE}, **ip("10.5.0.1"))
    assert res.status_code == 429
    # other IPs are unaffected
    assert c.post("/api/event-code", json={"code": CODE}, **ip("10.5.0.2")).status_code == 200


def test_guessing_via_seat_plan_header_counts(app, coded):
    c = app.test_client()
    for n in range(10):
        c.get("/api/seats", headers={"X-Event-Code": f"GUES{n}"}, **ip("10.6.0.1"))
    # now even the correct code does not reveal names from this IP
    assert seat(c, "A1", headers={"X-Event-Code": CODE}, **ip("10.6.0.1"))[0] is False
    assert c.post("/api/event-code", json={"code": CODE}, **ip("10.6.0.1")).status_code == 429


def test_wrong_codes_at_registration_count(app, coded):
    c = app.test_client()
    for n in range(10):
        c.post("/api/register", json={"name": "Eve", "email": f"e{n}@example.com", "event_code": f"GUES{n}"},
               **ip("10.7.0.1"))
    assert c.post("/api/event-code", json={"code": CODE}, **ip("10.7.0.1")).status_code == 429


# --- admin settings ---------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "ABCDEFG", "AB-12", "äöü1", 123])
def test_event_code_validation(admin, bad):
    assert admin.put("/api/admin/settings", json={"event_code": bad}).status_code == 400


def test_event_code_normalised_and_audited(admin):
    assert admin.put("/api/admin/settings", json={"event_code": " ab12c "}).get_json()["event_code"] == "AB12C"
    entry = admin.get("/api/admin/audit").get_json()["entries"][0]
    assert entry["action"] == "settings.changed" and entry["details"]["event_code"] == ["", "AB12C"]
