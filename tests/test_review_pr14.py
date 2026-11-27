"""Regression tests for the Copilot review of PR #14 (mail, admin areas, admin accounts).
The admin account races are in test_admins.py."""

import pytest

from lanparty import appsettings
from lanparty import security as sec
from lanparty.db import get_db
from tests.test_mail import ADMIN_PW, enable_mail, make_app, sql


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"name": "Alice", "password": ADMIN_PW}).status_code == 200
    return c


def stored(app, key):
    rows = sql(app, "SELECT value FROM settings WHERE key = ?", ("cfg." + key,))
    return rows[0]["value"] if rows else None


# --- app-wide settings -------------------------------------------------------------------

def test_config_is_validated_against_what_is_stored_under_the_lock(tmp_path, monkeypatch):
    app = make_app(tmp_path, mail=False)
    enable_mail(app, mail_enabled="0", smtp_host="smtp.example.com")
    admin = app.test_client()
    admin.post("/api/admin/login", json={"name": "Alice", "password": ADMIN_PW})
    real = sec.current_admin_session

    def sender_removed_meanwhile(*args, **kwargs):
        session = real(*args, **kwargs)          # has read (and cached) the app settings
        db = get_db()
        db.execute("DELETE FROM settings WHERE key = 'cfg.mail_from'")
        db.commit()
        return session
    monkeypatch.setattr(sec, "current_admin_session", sender_removed_meanwhile)
    res = admin.put("/api/admin/config/mail", json={"mail_enabled": True})
    assert res.status_code == 400 and "sender" in res.get_json()["error"]
    assert stored(app, "mail_enabled") == "0"


def test_smtp_passwords_are_stored_verbatim(app, admin):
    assert admin.put("/api/admin/config/mail", json={"smtp_password": "  spaced out  "}).status_code == 200
    assert stored(app, "smtp_password") == "  spaced out  "
    with app.app_context():
        assert appsettings.value("smtp_password") == "  spaced out  "


@pytest.mark.parametrize("given, kept", [
    ("[::1]", "::1"),
    ("::1", "::1"),
    ("[2001:DB8::25]", "2001:db8::25"),
    ("smtp.example.com", "smtp.example.com"),
    ("192.0.2.25", "192.0.2.25"),
])
def test_smtp_hosts_are_stored_the_way_smtplib_wants_them(app, admin, given, kept):
    assert admin.put("/api/admin/config/mail", json={"smtp_host": given}).status_code == 200
    assert stored(app, "smtp_host") == kept


@pytest.mark.parametrize("given", ["[::1", "[smtp.example.com]", "::g", "[::1]:25"])
def test_broken_smtp_hosts_are_refused(app, admin, given):
    assert admin.put("/api/admin/config/mail", json={"smtp_host": given}).status_code == 400


# --- test mails ------------------------------------------------------------------------

def _queue_other_mail(app):
    """A queued mail that is not the test mail (an older one, waiting for delivery)."""
    sql(app, "INSERT INTO mail_outbox (user_id, recipient, kind, subject, body_text, body_html) "
             "VALUES (NULL, 'other@example.com', 'message', 'Other', 'x', '<p>x</p>')")


def _fail_for(monkeypatch, recipient):
    from lanparty import mail as mailmod
    real = mailmod.transmit

    def transmit(msg):
        if msg["To"] == recipient:
            raise OSError("relay denied")
        real(msg)
    monkeypatch.setattr(mailmod, "transmit", transmit)


def test_a_failed_test_mail_is_reported_although_other_mails_go_out(app, admin, monkeypatch):
    _queue_other_mail(app)
    _fail_for(monkeypatch, "me@example.com")
    res = admin.post("/api/admin/mail/test", json={"to": "me@example.com"})
    assert res.status_code == 502 and "relay denied" in res.get_json()["error"]
    # the test mail neither sends nor deletes other queued mails
    assert [r["recipient"] for r in sql(app, "SELECT recipient FROM mail_outbox WHERE sent_at IS NULL")] \
        == ["other@example.com"]


def test_the_test_mail_is_sent_despite_a_backlog(app, admin, monkeypatch):
    from lanparty import mail as mailmod
    for _ in range(3):
        _queue_other_mail(app)
    real = mailmod.deliver_pending
    monkeypatch.setattr(mailmod, "deliver_pending", lambda limit=100: real(limit=1))   # a small batch
    from tests.test_mail import sent
    assert admin.post("/api/admin/mail/test", json={"to": "me@example.com"}).status_code == 200
    assert "me@example.com" in [m["To"] for m in sent(app)]


def test_queue_runs_leave_test_mails_to_their_request(app):
    with app.app_context():
        from lanparty import mail as mailmod
        sql(app, "INSERT INTO mail_outbox (user_id, recipient, kind, subject, body_text, body_html) "
                 "VALUES (NULL, 'me@example.com', 'test', 'Test', 'x', '<p>x</p>')")
        assert mailmod.deliver_pending() == 0


def test_manage_test_mail_reports_its_own_mail(app, monkeypatch, capsys):
    from lanparty import manage
    _queue_other_mail(app)
    _fail_for(monkeypatch, "me@example.com")
    assert manage.test_mail(app, "me@example.com") == 1
    assert "relay denied" in capsys.readouterr().out
    monkeypatch.undo()
    assert manage.test_mail(app, "me@example.com") == 0


# --- the event status is re-read under the lock ---------------------------------------------

def _event_ends_meanwhile(monkeypatch):
    """The request caches the active event, then another worker ends it."""
    from lanparty import api
    real = api.active_event_id

    def ended_after_lookup():
        event_id = real()
        db = get_db()
        db.execute("INSERT INTO event_settings (event_id, key, value) VALUES (?, 'event_end', '2026-06-01T11:00') "
                   "ON CONFLICT(event_id, key) DO UPDATE SET value = excluded.value", (event_id,))
        db.commit()
        return event_id
    monkeypatch.setattr(api, "active_event_id", ended_after_lookup)


def test_no_confirmation_after_the_event_ended_meanwhile(app, monkeypatch):
    from tests.test_mail import code_in, register, sent
    client = app.test_client()
    headers, _ = register(client)
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    code = code_in(sent(app)[-1], "confirm")
    _event_ends_meanwhile(monkeypatch)
    res = client.post("/api/confirm", json={"code": code, "login": True})
    assert res.status_code == 400
    [user] = sql(app, "SELECT email_confirmed_at, reserved_at FROM users")
    assert user["email_confirmed_at"] is None and user["reserved_at"] is None


def test_no_personal_link_login_after_the_event_ended_meanwhile(app, admin, monkeypatch):
    from tests.test_mail import code_in, confirmed_with_seat, sent
    client = app.test_client()
    confirmed_with_seat(app, client)
    uid = sql(app, "SELECT id FROM users")[0]["id"]
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    code = code_in(sent(app)[-1], "access")
    tokens = sql(app, "SELECT COUNT(*) AS n FROM user_tokens")[0]["n"]
    _event_ends_meanwhile(monkeypatch)
    assert client.post("/api/access", json={"code": code}).status_code == 400
    assert sql(app, "SELECT COUNT(*) AS n FROM user_tokens")[0]["n"] == tokens


# --- scheduler -----------------------------------------------------------------------------

def _end_event(app):
    from tests.helpers import set_event_setting
    set_event_setting(app, "event_end", "2026-06-01T11:00")      # the clock says 12:00: archived


@pytest.mark.parametrize("runner", ["scheduler", "request"])
def test_unconfirmed_registrations_expire_after_their_event_ended(app, runner):
    from lanparty import jobs
    from tests.test_mail import register
    client = app.test_client()
    register(client)
    _end_event(app)
    sql(app, "UPDATE users SET confirm_until = '2026-06-01 11:30:00'")     # deadline passed after the end
    if runner == "scheduler":
        jobs.run_once(app)
    else:
        client.get("/api/info")
    assert sql(app, "SELECT COUNT(*) AS n FROM users")[0]["n"] == 0
    [entry] = sql(app, "SELECT details FROM audit_log WHERE action = 'registration.unconfirmed_expired'")
    assert '"event_id": 1' in entry["details"]


def test_a_failed_one_shot_scheduler_run_exits_nonzero(app, monkeypatch):
    from lanparty import jobs

    def broken(_app):
        raise RuntimeError("database gone")
    monkeypatch.setattr(jobs, "create_app", lambda: app)
    monkeypatch.setattr(jobs, "run_once", broken)
    assert jobs.main(["--once"]) == 1
    monkeypatch.setattr(jobs, "run_once", lambda _app: {})
    assert jobs.main(["--once"]) == 0


# --- dates in mails -----------------------------------------------------------------------

def test_mail_dates_use_the_saved_time_zone_and_survive_dst(tmp_path):
    from datetime import datetime
    from lanparty import notify
    app = make_app(tmp_path, TIMEZONE="UTC")
    enable_mail(app, timezone="Europe/Berlin")           # saved on /admin/settings
    user = {"reserved_at": "2026-10-24 12:00:00"}        # 14:00 in Berlin (summer time)
    with app.app_context():
        # 48 h later is 2026-10-26 12:00 UTC; summer time ended on the 25th: 13:00 in Berlin
        assert notify._deadline(user, {"reservation_hours": 48}) == datetime(2026, 10, 26, 13, 0)


# --- background sending -----------------------------------------------------------------------

def test_the_mail_worker_retires_under_the_lock(app, monkeypatch):
    import threading
    from lanparty import mail as mailmod
    runs = []
    monkeypatch.setattr(mailmod, "deliver_pending", lambda limit=100: runs.append(1))
    monkeypatch.setattr(mailmod, "_worker", threading.current_thread())   # "this is the worker"
    mailmod._wake.set()
    mailmod._drain(app)
    assert runs == [1]
    # unregistered on exit, so a wake after its last check starts a new worker
    assert mailmod._worker is None


# --- admin pages ------------------------------------------------------------------------------

def test_polling_the_session_does_not_keep_it_alive(app, admin):
    import time
    ten_minutes_ago = time.time() - 600
    sql(app, "UPDATE admin_sessions SET last_seen = ?", (ten_minutes_ago,))
    assert admin.get("/api/admin/session", headers={"X-Background": "1"}).get_json()["admin"] is True
    assert sql(app, "SELECT last_seen FROM admin_sessions")[0]["last_seen"] == ten_minutes_ago
    assert admin.get("/api/admin/session").get_json()["admin"] is True      # a real visit does
    assert sql(app, "SELECT last_seen FROM admin_sessions")[0]["last_seen"] > ten_minutes_ago


def test_admin_pages_poll_the_session_in_the_background():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "lanparty/static/admin-common.js").read_text()
    assert 'api("GET", "/admin/session", undefined, { background: true })' in js
    assert "setInterval(() => this.poll()" in js


def test_the_message_confirmation_stops_when_the_event_changed():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "lanparty/static/admin-events.js").read_text()
    send = js[js.index("async function onSendMessage"):]
    assert send.index("if (currentEvent !== target)") < send.index("confirm(t(\"admin.message.confirm\"")
    assert "event: targetName" in send


def test_the_retry_button_follows_the_full_failure_count():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "lanparty/static/admin-mail.js").read_text()
    assert '$("retry-queue").disabled = !(status.failed > 0)' in js
