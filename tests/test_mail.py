"""Optional mail: confirmation, payment and reminder mails, personal links, admin
messages, the outbox and the scheduler. Mails go to an in-memory list
(MAIL_BACKEND=memory)."""

import json
import re

import pytest

from lanparty import create_app, jobs
from lanparty import mail as mailmod
from lanparty.db import get_db, utc_now
from tests.helpers import EVENT_CODE, set_event_code, set_event_setting

ADMIN_PW = "correct horse battery"


def make_app(tmp_path, mail=True, **config):
    app = set_event_code(create_app({"TESTING": True, "DATABASE": str(tmp_path / "mail.sqlite3"),
                                     "ADMIN_PASSWORD": ADMIN_PW, "MAIL_BACKEND": "memory", **config}))
    set_event_setting(app, "paypal_address", "orga@example.com")
    if mail:
        enable_mail(app)
    return app


def enable_mail(app, **extra):
    values = {"mail_enabled": "1", "mail_from": "lan@example.com", "public_url": "https://lan.example.com", **extra}
    with app.app_context():
        db = get_db()
        for key, value in values.items():
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                       "value = excluded.value", ("cfg." + key, value))
        db.commit()


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"name": "Alice", "password": ADMIN_PW}).status_code == 200
    return c


def sent(app):
    return app.extensions.setdefault("sent_mail", [])


def text_of(msg):
    return msg.get_body(("plain",)).get_content()


def html_of(msg):
    return msg.get_body(("html",)).get_content()


def code_in(msg, kind):
    return re.search(rf"#{kind}=([A-Za-z0-9_-]+)", text_of(msg)).group(1)


def sql(app, query, params=()):
    with app.app_context():
        db = get_db()
        cur = db.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        db.commit()
        return rows


def register(client, n=1, lang="en-US"):
    res = client.post("/api/register", json={"name": f"User{n}", "email": f"u{n}@example.com",
                                             "event_code": EVENT_CODE}, headers={"X-Lang": lang})
    assert res.status_code == 201, res.get_json()
    return {"X-User-Token": res.get_json()["token"]}, res.get_json()


def confirmed_with_seat(app, client, n=1, seat="A1"):
    headers, _ = register(client, n)
    client.post("/api/me/seat", json={"seat": seat}, headers=headers)
    code = code_in(sent(app)[-1], "confirm")
    assert client.post("/api/confirm", json={"code": code}).status_code == 200
    return headers


# --- without mail nothing changes ----------------------------------------------------

def test_without_mail_registration_needs_no_confirmation(tmp_path):
    app = make_app(tmp_path, mail=False)
    client = app.test_client()
    headers, data = register(client)
    assert data["confirmation_required"] is False and data["user"]["email_confirmed"] is True
    me = client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).get_json()
    assert me["payment"] is not None and me["user"]["reserved_until"]
    assert sent(app) == []
    assert client.get("/api/info").get_json()["mail_enabled"] is False


def test_mail_switched_on_but_incomplete_counts_as_off(tmp_path):
    app = make_app(tmp_path, mail=False)
    enable_mail(app, mail_from="")
    _, data = register(app.test_client())
    assert data["confirmation_required"] is False


# --- confirmation ---------------------------------------------------------------------

def test_registration_sends_a_confirmation_and_holds_the_seat(app, client):
    headers, data = register(client)
    assert data["confirmation_required"] is True and data["user"]["confirm_until"].endswith("Z")
    [msg] = sent(app)
    assert msg["To"] == "u1@example.com" and "confirm" in msg["Subject"].lower()
    assert msg["From"] == "LAN Party <lan@example.com>"
    assert "https://lan.example.com/#confirm=" in text_of(msg)
    me = client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).get_json()
    # held, but no payment window and no payment details yet
    assert me["payment"] is None and me["user"]["reserved_until"] is None and me["user"]["seat"] == "A1"
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["A1"] == "reserved"


def test_confirming_starts_the_payment_window_and_mails_the_details(app, client):
    headers, _ = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    code = code_in(sent(app)[0], "confirm")
    phone = app.test_client()                     # the mail is opened on another device
    res = phone.post("/api/confirm", json={"code": code, "login": True}).get_json()
    assert res["user"]["email_confirmed"] is True and res["user"]["reserved_until"]
    assert res["payment"]["amount_cents"] == 2000
    assert phone.get("/api/me", headers={"X-User-Token": res["token"]}).status_code == 200
    payment = sent(app)[-1]
    assert "seat A1" in payment["Subject"]
    body = text_of(payment)
    assert "orga@example.com" in body and "€20.00" in body and "Pay by" in body
    # the code works once
    assert client.post("/api/confirm", json={"code": code}).status_code == 400


def test_confirming_without_a_seat_mails_the_details_at_the_first_pick(app, client):
    headers, _ = register(client)
    client.post("/api/confirm", json={"code": code_in(sent(app)[0], "confirm")})
    assert len(sent(app)) == 1
    client.post("/api/me/seat", json={"seat": "B2"}, headers=headers)
    assert len(sent(app)) == 2 and "seat B2" in sent(app)[-1]["Subject"]
    client.post("/api/me/seat", json={"seat": "B3"}, headers=headers)   # switching: same window, no mail
    assert len(sent(app)) == 2


def test_unconfirmed_registrations_expire(app, client):
    headers, data = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    sql(app, "UPDATE users SET confirm_until = ?", (utc_now(-0.01),))
    seats = {s["id"]: s["status"] for s in client.get("/api/seats").get_json()["seats"]}
    assert seats["A1"] == "free"
    assert client.get("/api/me", headers=headers).status_code == 401
    assert [r["action"] for r in sql(app, "SELECT action FROM audit_log")] == ["registration.unconfirmed_expired"]
    register(client)   # the address is free again


def test_confirmation_can_be_resent_with_the_same_deadline(app, client):
    headers, data = register(client)
    old = code_in(sent(app)[0], "confirm")
    res = client.post("/api/me/confirmation", headers=headers)
    assert res.status_code == 200 and res.get_json()["user"]["confirm_until"] == data["user"]["confirm_until"]
    new = code_in(sent(app)[-1], "confirm")
    assert client.post("/api/confirm", json={"code": old}).status_code == 400
    assert client.post("/api/confirm", json={"code": new}).status_code == 200
    assert client.post("/api/me/confirmation", headers=headers).status_code == 409


def test_resending_is_rate_limited(app, client):
    headers, _ = register(client)
    codes = [client.post("/api/me/confirmation", headers=headers).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_mails_are_in_the_participants_language(app, client):
    register(client, lang="de-DE")
    msg = sent(app)[0]
    assert msg["Subject"].startswith("Bitte bestätige") and "Hallo User1," in text_of(msg)


# --- payment confirmation and the personal link ----------------------------------------

def test_confirmed_payment_mails_a_reusable_personal_link(app, client, admin):
    confirmed_with_seat(app, client)
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    msg = sent(app)[-1]
    assert "is confirmed" in msg["Subject"]
    code = code_in(msg, "access")
    for _ in range(2):   # works more than once
        other = app.test_client()
        assert other.post("/api/access/preview", json={"code": code}).get_json()["name"] == "User1"
        token = other.post("/api/access", json={"code": code}).get_json()["token"]
        assert other.get("/api/me", headers={"X-User-Token": token}).status_code == 200


def test_admin_payment_settles_an_unconfirmed_address(app, client, admin):
    headers, _ = register(client)
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    sql(app, "UPDATE users SET confirm_until = ?", (utc_now(-1),))
    me = client.get("/api/me", headers=headers).get_json()["user"]     # not deleted
    assert me["paid"] and me["email_confirmed"]


def test_logging_out_other_devices_replaces_the_personal_link(app, client, admin):
    headers = confirmed_with_seat(app, client)
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    old = code_in(sent(app)[-1], "access")
    res = client.post("/api/me/logout-others", headers=headers).get_json()
    assert res["new_link_sent"] is True
    new = code_in(sent(app)[-1], "access")
    assert client.post("/api/access", json={"code": old}).status_code == 400
    assert client.post("/api/access", json={"code": new}).status_code == 200


def test_personal_links_only_work_for_the_active_event(app, client, admin, frozen_event_clock):
    from datetime import datetime

    confirmed_with_seat(app, client)
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    code = code_in(sent(app)[-1], "access")
    set_event_setting(app, "event_end", "2026-06-02T00:00")
    frozen_event_clock["now"] = datetime(2026, 6, 3, 12, 0)    # event over
    assert client.post("/api/access", json={"code": code}).status_code == 400


# --- messages from admins --------------------------------------------------------------

def test_admin_messages_reach_the_chosen_group(app, client, admin):
    confirmed_with_seat(app, client, 1, "A1")                 # open payment
    confirmed_with_seat(app, client, 2, "A2")
    register(client, 3)                                       # unconfirmed: never messaged
    uid2 = sql(app, "SELECT id FROM users WHERE email = 'u2@example.com'")[0]["id"]
    admin.post(f"/api/admin/users/{uid2}/paid", json={"paid": True})
    count = lambda audience: admin.post("/api/admin/messages", json={"audience": audience, "dry_run": True}).get_json()["recipients"]   # noqa: E731
    assert (count("open"), count("paid"), count("all")) == (1, 1, 2)
    before = len(sent(app))
    res = admin.post("/api/admin/messages", json={
        "audience": "all", "subject": "Doors\r\nBcc: evil@example.com open at 6", "body": "Bring <b>cables</b>!\n\nSee you."})
    assert res.get_json()["recipients"] == 2
    new = sent(app)[before:]
    assert sorted(m["To"] for m in new) == ["u1@example.com", "u2@example.com"]
    assert new[0]["Subject"] == "Doors Bcc: evil@example.com open at 6" and new[0]["Bcc"] is None
    assert "&lt;b&gt;cables&lt;/b&gt;" in html_of(new[0]) and "<b>cables" not in html_of(new[0])
    assert [r["action"] for r in sql(app, "SELECT action FROM audit_log WHERE action = 'message.sent'")]


def test_admin_messages_need_mail(tmp_path):
    app = make_app(tmp_path, mail=False)
    c = app.test_client()
    c.post("/api/admin/login", json={"name": "Alice", "password": ADMIN_PW})
    assert c.post("/api/admin/messages", json={"audience": "all", "dry_run": True}).status_code == 409


# --- reminders and the scheduler -------------------------------------------------------

def test_reminders_are_sent_once_before_the_deadline(app, client):
    confirmed_with_seat(app, client)
    sql(app, "UPDATE users SET reserved_at = ?", (utc_now(-100),))    # 68 of 168 hours left
    with app.app_context():
        assert jobs.send_payment_reminders(1) == 0                   # reminders are off by default
    set_event_setting(app, "payment_reminders", "1")
    with app.app_context():
        assert jobs.send_payment_reminders(1) == 0                   # not within 48 hours yet
    sql(app, "UPDATE users SET reserved_at = ?", (utc_now(-130),))    # 38 hours left
    with app.app_context():
        assert jobs.send_payment_reminders(1) == 1
        assert jobs.send_payment_reminders(1) == 0                   # once per window
        mailmod.deliver_pending()                                    # queued; the scheduler sends it
    assert sent(app)[-1]["Subject"].startswith("Reminder")


def test_scheduler_pass_expires_sends_and_records_a_heartbeat(app, client):
    headers, _ = register(client)
    sql(app, "UPDATE users SET confirm_until = ?", (utc_now(-1),))
    assert not jobs.check(app, 60)
    result = jobs.run_once(app)
    assert result["sent"] == 0 and jobs.check(app, 60)
    assert sql(app, "SELECT COUNT(*) AS n FROM users")[0]["n"] == 0


def test_failed_mails_are_kept_and_retried(app, client, monkeypatch):
    def broken(msg):
        raise OSError("connection refused")

    monkeypatch.setattr(mailmod, "transmit", broken)
    register(client)                      # registration works anyway
    [row] = sql(app, "SELECT * FROM mail_outbox")
    assert row["sent_at"] is None and row["attempts"] == 1 and "refused" in row["last_error"]
    monkeypatch.undo()
    sql(app, "UPDATE mail_outbox SET claimed_at = 0")   # retry time has come
    jobs.run_once(app)
    assert sql(app, "SELECT sent_at FROM mail_outbox")[0]["sent_at"] is not None


def test_mails_use_the_events_theme(app, client):
    set_event_setting(app, "theme", "retro")
    register(client)
    html = html_of(sent(app)[0])
    assert "#0a7a2f" in html                    # retro's light-mode accent
    assert "var(" not in html and "<script" not in html


def test_theme_values_cannot_break_out_of_the_style_attribute(tmp_path):
    from lanparty.mail import _blocks

    css = ':root[data-mode="light"] { --accent: red" onload="x; --bg: url(evil); --text: #123456; }'
    assert _blocks(css, ':root[data-mode="light"]') == {"text": "#123456"}


# --- app-wide settings (/admin/settings, /admin/mail) -----------------------------------

def test_config_sections_never_return_secrets(app, admin):
    res = admin.put("/api/admin/config/mail", json={"smtp_host": "smtp.example.com", "smtp_password": "hunter22"})
    assert res.status_code == 200
    fields = {f["key"]: f for f in res.get_json()["fields"]}
    assert fields["smtp_password"] == {"key": "smtp_password", "type": "secret", "set": True, "max_length": 500}
    assert "hunter22" not in res.get_data(as_text=True)
    # empty = keep, _clear = remove
    admin.put("/api/admin/config/mail", json={"smtp_password": ""})
    assert {f["key"]: f for f in admin.get("/api/admin/config/mail").get_json()["fields"]}["smtp_password"]["set"]
    admin.put("/api/admin/config/mail", json={"smtp_password_clear": True})
    assert not {f["key"]: f for f in admin.get("/api/admin/config/mail").get_json()["fields"]}["smtp_password"]["set"]
    audit = [json.loads(r["details"]) for r in sql(app, "SELECT details FROM audit_log WHERE action = 'config.changed'")]
    assert audit[0]["smtp_password"] == "(changed)" and "hunter22" not in json.dumps(audit)


@pytest.mark.parametrize("section, data", [
    ("general", {"public_url": "javascript:alert(1)"}),
    ("general", {"timezone": "Mars/Olympus"}),
    ("general", {"login_code_hours": 0}),
    ("mail", {"smtp_security": "maybe"}),
    ("mail", {"mail_from": "not an address"}),
    ("mail", {"smtp_host": "evil host\r\n"}),
    ("mail", {"public_url": "https://x.example"}),       # belongs to "general"
    ("security", {"nonsense": 1}),
])
def test_config_validation(app, admin, section, data):
    assert admin.put(f"/api/admin/config/{section}", json=data).status_code == 400


def test_enabling_mail_needs_the_site_address(tmp_path):
    app = make_app(tmp_path, mail=False)
    c = app.test_client()
    c.post("/api/admin/login", json={"name": "Alice", "password": ADMIN_PW})
    res = c.put("/api/admin/config/mail", json={"mail_enabled": True, "smtp_host": "smtp.example.com",
                                                "mail_from": "lan@example.com"})
    assert res.status_code == 400 and "site address" in res.get_json()["error"]
    c.put("/api/admin/config/general", json={"public_url": "https://lan.example.com/"})
    res = c.put("/api/admin/config/mail", json={"mail_enabled": True, "smtp_host": "smtp.example.com",
                                                "mail_from": "lan@example.com"})
    assert res.status_code == 200 and res.get_json()["enabled"] is True


def test_saved_settings_override_the_environment_defaults(app, admin, client):
    headers, _ = register(client)
    admin.put("/api/admin/config/general", json={"login_code_hours": 2})
    link = client.post("/api/me/login-link", headers=headers).get_json()
    assert link["expires_at"] <= utc_now(2.01).replace(" ", "T") + "Z"
    admin.put("/api/admin/config/security", json={"register_max_per_ip": 1})
    res = client.post("/api/register", json={"name": "Two", "email": "two@example.com", "event_code": EVENT_CODE})
    assert res.status_code == 429


def test_config_needs_an_admin(client):
    assert client.get("/api/admin/config/mail").status_code == 401
    assert client.put("/api/admin/config/general", json={}).status_code == 401


def test_test_mail_reports_the_result(app, admin, monkeypatch):
    assert admin.post("/api/admin/mail/test", json={"to": "me@example.com"}).status_code == 200
    assert sent(app)[-1]["To"] == "me@example.com"

    def broken(msg):
        raise OSError("auth failed")

    monkeypatch.setattr(mailmod, "transmit", broken)
    res = admin.post("/api/admin/mail/test", json={"to": "me@example.com"})
    assert res.status_code == 502 and "auth failed" in res.get_json()["error"]
    assert sql(app, "SELECT COUNT(*) AS n FROM mail_outbox WHERE kind = 'test'")[0]["n"] == 0


def test_given_up_mails_can_be_retried(app, admin, client):
    register(client)
    sql(app, "UPDATE mail_outbox SET sent_at = NULL, attempts = ?", (mailmod.MAX_ATTEMPTS,))
    queue = admin.get("/api/admin/mail/queue").get_json()
    assert queue["status"]["failed"] == 1 and queue["pending"][0]["given_up"]
    assert admin.post("/api/admin/mail/retry").get_json()["retried"] == 1
    assert admin.get("/api/admin/mail/queue").get_json()["pending"] == []


# --- security area -----------------------------------------------------------------------

def test_admins_see_and_end_sessions(app, admin):
    other = app.test_client()
    other.post("/api/admin/login", json={"name": "Bob", "password": ADMIN_PW})
    sessions = admin.get("/api/admin/sessions").get_json()["sessions"]
    assert {(s["actor"], s["current"]) for s in sessions} == {("Alice", True), ("Bob", False)}
    assert all(len(s["id"]) == 12 for s in sessions)
    bob = next(s for s in sessions if s["actor"] == "Bob")
    assert admin.delete(f"/api/admin/sessions/{bob['id']}").status_code == 200
    assert other.get("/api/admin/session").get_json()["admin"] is False
    assert admin.delete(f"/api/admin/sessions/{bob['id']}").status_code == 404
    assert admin.delete("/api/admin/sessions/../../x").status_code == 404


def test_admins_can_void_participants_access(app, client, admin):
    headers = confirmed_with_seat(app, client)
    link = client.post("/api/me/login-link", headers=headers).get_json()["code"]
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    personal = code_in(sent(app)[-1], "access")
    assert admin.post("/api/admin/participant-access", json={"action": "login_links"}).get_json()["count"] == 1
    assert client.post("/api/login", json={"code": link}).status_code == 400
    res = admin.post("/api/admin/participant-access", json={"action": "personal_links", "resend": True}).get_json()
    assert res == {"count": 1, "mailed": True}
    assert client.post("/api/access", json={"code": personal}).status_code == 400
    new = code_in(sent(app)[-1], "access")
    assert admin.post("/api/admin/participant-access", json={"action": "devices"}).get_json()["count"] >= 1
    assert client.get("/api/me", headers=headers).status_code == 401
    assert client.post("/api/access", json={"code": new}).status_code == 200    # the way back in
    assert admin.post("/api/admin/participant-access", json={"action": "everything"}).status_code == 400


def test_security_checks_and_overview(app, admin):
    checks = {c["id"]: c for c in admin.get("/api/admin/security/checks").get_json()["checks"]}
    assert checks["https"]["level"] == "warn"            # the test client uses http
    assert checks["accounts"]["level"] == "warn"         # start mode: no admin accounts yet
    assert checks["scheduler"]["level"] == "warn"        # mail on, scheduler never ran
    overview = admin.get("/api/admin/overview").get_json()
    assert overview["warnings"] >= 2 and overview["mail"]["enabled"] is True
    assert overview["event"]["registered"] == 0


def test_timezone_setting_moves_the_event_clock(app, admin):
    from lanparty import events

    admin.put("/api/admin/config/general", json={"timezone": "Pacific/Kiritimati"})
    with app.test_request_context():
        assert events.timezone_name() == "Pacific/Kiritimati"


def test_compose_runs_the_scheduler_like_the_app():
    from pathlib import Path

    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    scheduler = compose.split("\n  scheduler:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert 'command: ["python", "-m", "lanparty.jobs"]' in scheduler
    assert "environment: *app-environment" in scheduler and "environment: &app-environment" in compose
    for needed in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "lanparty.jobs\", \"--check"):
        assert needed in scheduler
    assert "ports:" not in scheduler
