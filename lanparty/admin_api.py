"""Admin endpoints of the app-wide areas: /admin/settings, /admin/mail, /admin/security
and the overview on /admin. They extend the ``/api`` blueprint from api.py; the
event pages' endpoints stay there."""

from datetime import datetime, timezone
import re
import time

from flask import current_app, jsonify, request

from . import appsettings
from . import events as ev
from . import mail
from . import notify
from . import security as sec
from . import settings as cfg
from .api import (ApiError, JOBS_HEARTBEAT, actor, admin_event_id, admin_required, bp, enforce_limit,
                  human_wait, json_body, revoke_tokens)
from .db import get_db, utc_now, write_transaction
from .i18n import tr


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def scheduler_last_run() -> str | None:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (JOBS_HEARTBEAT,)).fetchone()
    return row["value"] if row else None


def scheduler_running() -> bool:
    last = scheduler_last_run()
    return bool(last) and datetime.fromisoformat(last) >= datetime.fromisoformat(utc_now(-0.25))


# --- app-wide settings --------------------------------------------------------------

@bp.get("/admin/config/<name>")
@admin_required
def admin_get_config(name):
    if name not in appsettings.SECTIONS:
        raise ApiError(404, tr("Not found."))
    data = {"section": name, "fields": appsettings.section(name)}
    if name == "general":
        data["server_time"] = ev.local_now().isoformat(timespec="minutes")
        data["suggested_public_url"] = request.host_url.rstrip("/")
    if name == "mail":
        data.update(mail_status())
    return jsonify(data)


@bp.put("/admin/config/<name>")
@admin_required
def admin_put_config(name):
    if name not in appsettings.SECTIONS:
        raise ApiError(404, tr("Not found."))
    data = json_body()
    try:
        with write_transaction():
            changes = appsettings.save(appsettings.validate_update(name, data))
            if changes:
                sec.record_audit(actor(), "config.changed", section=name, **changes)
    except appsettings.ConfigError as err:
        raise ApiError(400, str(err)) from None
    return admin_get_config(name)


# --- overview (/admin) ------------------------------------------------------------------

@bp.get("/admin/overview")
@admin_required
def admin_overview():
    events = ev.load_all()
    active = next((e for e in events if e["status"] == "active"), None)
    upcoming = ev.upcoming()
    shown = active or (upcoming[0] if upcoming else None)
    summary = None
    if shown is not None:
        counts = get_db().execute(
            "SELECT COUNT(*) AS registered, COUNT(seat) AS seated, COALESCE(SUM(paid), 0) AS paid "
            "FROM users WHERE event_id = ?", (shown["id"],)).fetchone()
        summary = {"id": shown["id"], "name": shown["event_name"], "status": shown["status"],
                   "registered": counts["registered"], "seated": counts["seated"], "paid": counts["paid"]}
    checks = security_checks()
    return jsonify(
        event=summary, events=len(events),
        mail=mail_status(),
        warnings=sum(1 for c in checks if c["level"] == "warn"),
        settings={"public_url": appsettings.value("public_url"), "timezone": appsettings.value("timezone")},
        scheduler_last_run=scheduler_last_run(),
    )


# --- mail (/admin/mail) ---------------------------------------------------------------

def mail_status() -> dict:
    db = get_db()
    counts = db.execute(
        "SELECT SUM(CASE WHEN sent_at IS NULL AND attempts < ? THEN 1 ELSE 0 END) AS queued, "
        "SUM(CASE WHEN sent_at IS NULL AND attempts >= ? THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN sent_at IS NOT NULL AND sent_at >= ? THEN 1 ELSE 0 END) AS sent_day "
        "FROM mail_outbox", (mail.MAX_ATTEMPTS, mail.MAX_ATTEMPTS, utc_now(-24))).fetchone()
    return {
        "enabled": mail.enabled(),
        "switched_on": bool(appsettings.value("mail_enabled")),
        "problems": mail.problems(),
        "queued": counts["queued"] or 0, "failed": counts["failed"] or 0, "sent_day": counts["sent_day"] or 0,
        "scheduler_last_run": scheduler_last_run(),
        "dev_backend": current_app.config["MAIL_BACKEND"] or None,
    }


@bp.get("/admin/mail/queue")
@admin_required
def admin_mail_queue():
    rows = get_db().execute(
        "SELECT id, recipient, kind, subject, attempts, last_error, created_at, sent_at FROM mail_outbox "
        "WHERE sent_at IS NULL ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify(status=mail_status(), pending=[
        {"id": r["id"], "recipient": r["recipient"], "kind": r["kind"], "subject": r["subject"],
         "attempts": r["attempts"], "last_error": r["last_error"], "created_at": r["created_at"],
         "given_up": r["attempts"] >= mail.MAX_ATTEMPTS} for r in rows])


@bp.post("/admin/mail/retry")
@admin_required
def admin_mail_retry():
    """Try the mails again that were given up (e.g. after fixing the SMTP settings)."""
    with write_transaction() as db:
        count = db.execute("UPDATE mail_outbox SET attempts = 0, claimed_at = NULL "
                           "WHERE sent_at IS NULL AND attempts >= ?", (mail.MAX_ATTEMPTS,)).rowcount
        sec.record_audit(actor(), "mail.retried", mails=count)
    mail.flush_soon()
    return jsonify(retried=count)


@bp.post("/admin/mail/test")
@admin_required
def admin_mail_test():
    """Send a test mail now and report the mail server's answer."""
    to = str(json_body().get("to") or "").strip()
    if not appsettings.EMAIL_RE.fullmatch(to) or len(to) > 200:
        raise ApiError(400, tr("Please enter a valid email address."))
    if not mail.enabled():
        raise ApiError(409, tr("Mail isn't ready yet: switch it on and fill in the missing settings first."))
    enforce_limit("mail_test", "*", 20, 3600, tr("Too many test mails."))
    event_id = ev.active_id() or ev.default_admin_id()
    with write_transaction():
        notify.test_mail(to, cfg.load(event_id))
        sec.record_audit(actor(), "mail.test_sent", to=to)
    db = get_db()
    row = db.execute("SELECT id FROM mail_outbox WHERE kind = 'test' ORDER BY id DESC LIMIT 1").fetchone()
    mail.deliver_pending()
    result = db.execute("SELECT sent_at, last_error FROM mail_outbox WHERE id = ?", (row["id"],)).fetchone()
    # a test mail is not retried: delete it either way
    db.execute("DELETE FROM mail_outbox WHERE id = ?", (row["id"],))
    db.commit()
    if result["sent_at"] is None:
        raise ApiError(502, tr("Sending failed: {error}", error=result["last_error"] or "?"))
    return jsonify(ok=True)


# --- security (/admin/security) -----------------------------------------------------

@bp.post("/admin/password")
@admin_required
def admin_change_password():
    """Set a new admin password. The current one is required (a stolen session alone
    can't take over the admin area); every other session ends."""
    data = json_body()
    current, new = str(data.get("current") or ""), str(data.get("new") or "")
    key = sec.ip_hash()
    wait, attempt = sec.consume("admin_login_fail", key, appsettings.value("admin_login_max_per_ip"),
                                appsettings.value("admin_login_window_minutes") * 60)
    if wait:
        raise ApiError(429, tr("Too many failed logins. Please try again in {wait}.", wait=human_wait(wait)),
                       retry_after=wait)
    if not sec.check_admin_password(current):
        sec.audit(actor(), "admin.password_change_failed", network=sec.anonymized_ip())
        raise ApiError(403, tr("The current password is wrong."))
    sec.release(attempt)
    if len(new) < sec.MIN_PASSWORD_LENGTH:
        raise ApiError(400, tr("The new password must be at least {n} characters long.", n=sec.MIN_PASSWORD_LENGTH))
    if len(new) > 200:
        raise ApiError(400, tr("The new password must be at most 200 characters long."))
    if new == current:
        raise ApiError(400, tr("The new password must differ from the current one."))
    with write_transaction():
        sec.set_admin_password(new)
        sec.record_audit(actor(), "admin.password_changed", network=sec.anonymized_ip())
    sec.end_all_admin_sessions()
    token = sec.create_admin_session(actor())   # this admin stays logged in
    resp = jsonify(ok=True)
    sec.set_admin_cookie(resp, token)
    return resp


SESSION_ID_RE = re.compile(r"[0-9a-f]{12}")


def _session_id(token_hash: str) -> str:
    return token_hash[:12]


@bp.get("/admin/sessions")
@admin_required
def admin_sessions():
    mine = sec.sha256(request.cookies.get(sec.ADMIN_COOKIE, ""))
    rows = get_db().execute("SELECT * FROM admin_sessions ORDER BY last_seen DESC").fetchall()
    return jsonify(sessions=[
        {"id": _session_id(r["token_hash"]), "actor": r["actor"], "created_at": _iso(r["created_at"]),
         "last_seen": _iso(r["last_seen"]), "current": r["token_hash"] == mine} for r in rows],
        password=sec.admin_password_info())


@bp.delete("/admin/sessions/<session_id>")
@admin_required
def admin_end_session(session_id):
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ApiError(404, tr("Not found."))
    with write_transaction() as db:
        rows = db.execute("SELECT token_hash, actor FROM admin_sessions").fetchall()
        match = [r for r in rows if _session_id(r["token_hash"]) == session_id]
        if len(match) != 1:
            raise ApiError(404, tr("This session has already ended."))
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (match[0]["token_hash"],))
        sec.record_audit(actor(), "admin.session_ended", session_of=match[0]["actor"])
    return jsonify(ok=True)


ACCESS_ACTIONS = ("login_links", "personal_links", "devices")


@bp.post("/admin/participant-access")
@admin_required
def admin_reset_participant_access():
    """Void participants' ways in, for one event: unused one-time login links, the
    personal links from payment mails (optionally mailing new ones), or every
    logged-in device."""
    data = json_body()
    action = data.get("action")
    if action not in ACCESS_ACTIONS:
        raise ApiError(400, tr("'action' must be one of: login_links, personal_links, devices."))
    event_id = admin_event_id()
    resend = data.get("resend") is True and action == "personal_links" and mail.enabled()
    with write_transaction() as db:
        users = db.execute("SELECT * FROM users WHERE event_id = ?", (event_id,)).fetchall()
        count = 0
        if action == "login_links":
            for u in users:
                count += db.execute("DELETE FROM login_codes WHERE user_id = ?", (u["id"],)).rowcount
        elif action == "devices":
            for u in users:
                count += revoke_tokens(u["id"])
        else:
            event = cfg.load(event_id)
            for u in (u for u in users if u["access_code_hash"]):
                code = sec.new_secret() if resend else None
                db.execute("UPDATE users SET access_code_hash = ? WHERE id = ?",
                           (sec.sha256(code) if code else None, u["id"]))
                if code:
                    notify.new_access_link(u, event, code, reason="admin")
                count += 1
        sec.record_audit(actor(), "participants.access_reset", event_id=event_id, what=action, count=count,
                         new_links_mailed=resend)
    mail.flush_soon()
    return jsonify(count=count, mailed=resend)


def security_checks() -> list[dict]:
    """A list of things worth checking, each ``{id, level: ok|info|warn, text}``."""
    checks = []

    def add(check_id, level, text):
        checks.append({"id": check_id, "level": level, "text": text})

    config = current_app.config
    if request.is_secure:
        add("https", "ok", tr("The admin area is served over HTTPS."))
        if not config["SESSION_COOKIE_SECURE"]:
            add("cookie", "warn", tr("HTTPS works, but SESSION_COOKIE_SECURE is off: set it to 1 so the admin "
                                     "cookie is never sent unencrypted."))
    else:
        add("https", "warn", tr("This page was loaded without HTTPS: passwords and the admin cookie travel "
                                "unencrypted. Put the app behind an HTTPS reverse proxy (or keep it inside a "
                                "LAN you trust)."))
    if request.headers.get("X-Forwarded-For") and not config["TRUSTED_PROXIES"]:
        add("proxy", "warn", tr("Requests come through a proxy, but TRUSTED_PROXIES is 0: every visitor counts "
                                "as the proxy, so the rate limits apply to everyone together."))
    info = sec.admin_password_info()
    if info["source"] == "environment":
        add("password", "info", tr("The admin password comes from ADMIN_PASSWORD. Set your own below; it is "
                                   "stored as a hash, not in a configuration file."))
        if len(config["ADMIN_PASSWORD"]) < 16:
            add("password-length", "warn", tr("The admin password is shorter than 16 characters."))
    elif info["changed_at"] and datetime.fromisoformat(info["changed_at"]) < datetime.fromisoformat(utc_now(-24 * 365)):
        add("password-age", "info", tr("The admin password hasn't been changed for over a year."))
    if config["ALLOW_WEAK_ADMIN_PASSWORD"]:
        add("weak", "warn", tr("ALLOW_WEAK_ADMIN_PASSWORD is on: only use it for local development."))
    for e in ev.load_all():
        if e["status"] not in ("active", "upcoming"):
            continue
        if not e["event_code"]:
            add(f"code-{e['id']}", "warn", tr("\"{event}\" has no event code: registration stays closed.",
                                              event=e["event_name"]))
        elif len(e["event_code"]) < 6:
            add(f"code-{e['id']}", "info", tr("The event code of \"{event}\" has fewer than 6 characters; "
                                              "longer codes are harder to guess.", event=e["event_name"]))
    if appsettings.value("mail_enabled"):
        if appsettings.value("smtp_security") == "none" and not config["MAIL_BACKEND"]:
            add("mail-tls", "warn", tr("Mails are sent without encryption (SMTP encryption: none)."))
        status = mail_status()
        if status["problems"]:
            add("mail-config", "warn", tr("Mail is switched on but not fully configured, so nothing is sent."))
        if status["failed"]:
            add("mail-failed", "warn", tr("{count} mails could not be sent; see the mail page.",
                                          count=status["failed"]))
        if not scheduler_running():
            add("scheduler", "warn", tr("The scheduler isn't running (python -m lanparty.jobs): reminders and "
                                        "retries of failed mails need it."))
    since = utc_now(-24)
    db = get_db()
    failed = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'admin.login_failed' AND ts >= ?",
                        (since,)).fetchone()[0]
    add("failed-logins", "warn" if failed >= 10 else "ok",
        tr("Failed admin logins in the last 24 hours: {count}.", count=failed))
    lockouts = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'event_code.limit_reached' AND ts >= ?",
                          (since,)).fetchone()[0]
    add("code-lockouts", "warn" if lockouts else "ok",
        tr("Networks locked out for guessing the event code in the last 24 hours: {count}.", count=lockouts))
    if appsettings.value("audit_log_retention_days") == 0:
        add("retention", "info", tr("Audit log entries are kept forever. They contain names and email addresses; "
                                    "consider a retention period."))
    sessions = db.execute("SELECT COUNT(*) FROM admin_sessions WHERE last_seen >= ?",
                          (time.time() - appsettings.value("admin_session_idle_minutes") * 60,)).fetchone()[0]
    add("sessions", "info", tr("Active admin sessions: {count}.", count=sessions))
    return checks


@bp.get("/admin/security/checks")
@admin_required
def admin_security_checks():
    return jsonify(checks=security_checks())

