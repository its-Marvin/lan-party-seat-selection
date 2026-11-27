"""Regression tests for the findings of the PR #1 code review."""

import copy
import threading

import pytest

from lanparty import api, create_app
from lanparty import layout as lay
from lanparty.db import get_db, utc_now
from tests.helpers import EVENT_CODE, set_event_code

ADMIN_PW = "correct horse battery"


def make_app(tmp_path, **overrides):
    config = {"TESTING": True, "DATABASE": str(tmp_path / "rf.sqlite3"), "ADMIN_PASSWORD": ADMIN_PW}
    config.update(overrides)
    return set_event_code(create_app(config))


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"name": "Test Admin", "password": ADMIN_PW}).status_code == 200
    return c


def ip(addr):
    return {"environ_base": {"REMOTE_ADDR": addr}}


def register(client, n, addr="10.1.0.1"):
    res = client.post("/api/register", json={"name": f"User{n}", "email": f"u{n}@example.com",
                                              "event_code": EVENT_CODE}, **ip(addr))
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()["user"]["id"]


def run_parallel(n, fn):
    """Run fn(i) in n threads that start at the same moment; return the results."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# --- seat identity across layout edits (deleted group, label reused) -------------

def deleted_b_renamed_a():
    new = copy.deepcopy(lay.DEFAULT_LAYOUT)
    new["groups"] = [dict(new["groups"][0], label="B")]   # delete B, rename A -> B
    return new


def test_plan_detects_deleted_group_whose_label_is_reused():
    old, new = lay.DEFAULT_LAYOUT, deleted_b_renamed_a()
    assert lay.plan_seat_moves(old, new, ["B1"]) == ({}, ["B1"])
    assert lay.plan_seat_moves(old, new, ["A1", "B1"]) == ({"A1": "B1"}, ["B1"])
    assert lay.plan_seat_moves(old, new, ["A1"]) == ({"A1": "B1"}, [])


def test_plan_ignores_label_order_for_legacy_seats():
    """Seats outside the old layout only keep their id if no group-mapped seat claims it."""
    old = {"area": {"width": 30, "height": 20}, "groups": [
        {"id": "g1", "label": "A", "name": "", "x": 0, "y": 0, "tables": 1, "per_row": 1, "orientation": "horizontal"}]}
    new = copy.deepcopy(old)
    new["groups"][0]["label"] = "C"
    # "C1" was a stale seat id before; now group A (renamed C) claims it
    assert lay.plan_seat_moves(old, new, ["C1", "A1"]) == ({"A1": "C1"}, ["C1"])


def test_api_refuses_to_hand_seats_of_a_deleted_group_to_another(app, admin):
    c = app.test_client()
    hb, _ = register(c, 1)
    c.post("/api/me/seat", json={"seat": "B1"}, headers=hb)
    res = admin.put("/api/admin/layout", json=deleted_b_renamed_a())
    assert res.status_code == 409 and "B1 (User1)" in res.get_json()["error"]
    assert c.get("/api/me", headers=hb).get_json()["user"]["seat"] == "B1"


def test_api_label_takeover_with_both_groups_occupied(app, admin):
    c = app.test_client()
    ha, _ = register(c, 1)
    hb, _ = register(c, 2)
    c.post("/api/me/seat", json={"seat": "A1"}, headers=ha)
    c.post("/api/me/seat", json={"seat": "B1"}, headers=hb)
    res = admin.put("/api/admin/layout", json=deleted_b_renamed_a())
    assert res.status_code == 409          # previously: 500 (UNIQUE constraint) or silent takeover


# --- input validation --------------------------------------------------------------

@pytest.mark.parametrize("path,method,raw", [
    ("/api/admin/layout", "put", b'{"area":{"width":1e400,"height":10},"groups":[]}'),
    ("/api/admin/settings", "put", b'{"amount":-1e999}'),
    ("/api/register", "post", b'{"name":"x","email":"a@b.cc","n":1e309}'),
])
def test_overflowing_numbers_are_400(admin, path, method, raw):
    res = getattr(admin, method)(path, data=raw, content_type="application/json")
    assert res.status_code == 400, res.get_json()


@pytest.mark.parametrize("payload", [
    {"event_start": "2026-12-12T14:00", "event_end": "2026-12-14T12:00Z"},
    {"event_start": "2026-12-12T14:00+01:00"},
])
def test_timezones_are_rejected_not_500(admin, payload):
    res = admin.put("/api/admin/settings", json=payload)
    assert res.status_code == 400 and "timezone" in res.get_json()["error"]


def test_partial_update_against_stored_value(admin):
    assert admin.put("/api/admin/settings", json={"event_start": "2026-12-12T14:00"}).status_code == 200
    assert admin.put("/api/admin/settings", json={"event_end": "2026-12-14T12:00Z"}).status_code == 400


# --- audit of expired reservations ---------------------------------------------------

def test_expiry_audit_keeps_the_seat(app, admin):
    c = app.test_client()
    h, _ = register(c, 1)
    c.post("/api/me/seat", json={"seat": "A7"}, headers=h)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET reserved_at = ?", (utc_now(-49),))
        db.commit()
    c.get("/api/info")
    entry = next(e for e in admin.get("/api/admin/audit").get_json()["entries"] if e["action"] == "reservation.expired")
    assert entry["details"]["seat"] == "A7"


# --- paid state vs. release ------------------------------------------------------------

def test_release_rechecks_paid_state_inside_transaction(app, admin, monkeypatch):
    """Simulate the race: the participant's row was read before the admin confirmed payment."""
    c = app.test_client()
    h, uid = register(c, 1)
    c.post("/api/me/seat", json={"seat": "A1"}, headers=h)
    with app.test_request_context(headers=h):
        stale = api.current_user()                       # paid = 0
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    monkeypatch.setattr(api, "require_user", lambda: stale)
    assert c.delete("/api/me/seat", headers=h).status_code == 409
    monkeypatch.undo()
    me = c.get("/api/me", headers=h).get_json()["user"]
    assert me["seat"] == "A1" and me["paid"] is True


# --- concurrency: limits cannot be exceeded by parallel requests -------------------------

def test_parallel_registrations_respect_limit(tmp_path):
    app = make_app(tmp_path, REGISTER_MAX_PER_IP=5)

    def attempt(i):
        return app.test_client().post("/api/register", json={
            "name": f"Racer{i}", "email": f"r{i}@example.com", "event_code": EVENT_CODE}, **ip("10.9.9.9")).status_code

    codes = run_parallel(20, attempt)
    assert codes.count(201) == 5 and codes.count(429) == 15


def test_parallel_reservations_respect_pending_limit(app):
    c = app.test_client()
    tokens = [register(c, n, addr=f"10.2.0.{n}")[0] for n in range(10)]

    def attempt(i):
        return app.test_client().post("/api/me/seat", json={"seat": f"A{i + 1}"}, headers=tokens[i],
                                      **ip("10.3.3.3")).status_code

    codes = run_parallel(10, attempt)
    assert codes.count(200) == 4 and codes.count(429) == 6


def test_parallel_code_guesses_respect_limit(app):
    def attempt(i):
        return app.test_client().post("/api/event-code", json={"code": f"GUES{i:02d}"}, **ip("10.4.4.4")).status_code

    codes = run_parallel(30, attempt)
    assert codes.count(400) == 10 and codes.count(429) == 20


def test_parallel_admin_guesses_respect_limit(app):
    def attempt(i):
        return app.test_client().post("/api/admin/login", json={"name": "Test Admin", "password": f"guess-{i}"}, **ip("10.5.5.5")).status_code

    codes = run_parallel(25, attempt)
    assert codes.count(401) == 5 and codes.count(429) == 20


def test_parallel_picks_of_one_seat(app):
    c = app.test_client()
    tokens = [register(c, n, addr=f"10.6.0.{n}")[0] for n in range(8)]

    def attempt(i):
        return app.test_client().post("/api/me/seat", json={"seat": "B5"}, headers=tokens[i],
                                      **ip(f"10.7.0.{i}")).status_code

    codes = run_parallel(8, attempt)
    assert codes.count(200) == 1 and codes.count(409) == 7


def test_layout_save_and_reservation_do_not_interleave(app, admin):
    """A seat reserved while the admin deletes its group must either block the save or fail itself."""
    c = app.test_client()
    h, _ = register(c, 1)
    shrunk = copy.deepcopy(lay.DEFAULT_LAYOUT)
    shrunk["groups"] = shrunk["groups"][:1]           # delete group B

    for _ in range(15):
        c.delete("/api/me/seat", headers=h)
        admin.put("/api/admin/layout", json=lay.DEFAULT_LAYOUT)

        def attempt(i):
            if i == 0:
                return app.test_client().post("/api/me/seat", json={"seat": "B3"}, headers=h).status_code
            return admin.put("/api/admin/layout", json=shrunk).status_code

        run_parallel(2, attempt)
        seat = c.get("/api/me", headers=h).get_json()["user"]["seat"]
        groups = [g["label"] for g in admin.get("/api/admin/layout").get_json()["groups"]]
        assert seat is None or "B" in groups       # never: seat B3 with group B deleted


# --- admins on the public page -------------------------------------------------------------

def test_admin_sees_names_but_still_needs_the_code(app, admin):
    data = admin.get("/api/seats").get_json()
    assert data["names_visible"] is True and data["event_code_ok"] is False
    data = admin.get("/api/seats", headers={"X-Event-Code": EVENT_CODE}).get_json()
    assert data["event_code_ok"] is True
    res = admin.post("/api/register", json={"name": "Admin", "email": "adm@example.com", "event_code": EVENT_CODE})
    assert res.status_code == 201
