"""Admin endpoints of the app-wide areas: /admin/settings, /admin/mail, /admin/security
and the overview on /admin. They extend the ``/api`` blueprint from api.py; the
event pages' endpoints stay there."""

from datetime import datetime, timezone
import re
import time

from flask import current_app, g, jsonify, request

from . import appsettings
from . import events as ev
from . import mail
from . import notify
from . import security as sec
from . import settings as cfg
from .api import (ApiError, JOBS_HEARTBEAT, actor, admin_event_id, admin_required, bp, enforce_limit,
                  human_wait, json_body, revoke_tokens)
from .db import IntegrityError, get_db, utc_now, write_transaction
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
            # validate against what is stored now, not what this request read before the lock
            appsettings.clear_cache()
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
        mail_id = notify.test_mail(to, cfg.load(event_id))
        sec.record_audit(actor(), "mail.test_sent", to=to)
    error = mail.send_test(mail_id)       # this mail only, never retried
    if error is not None:
        raise ApiError(502, tr("Sending failed: {error}", error=error))
    return jsonify(ok=True)


# --- security (/admin/security) -----------------------------------------------------

def _reauthenticate(password) -> str:
    """Sensitive changes need the acting admin's password again (a stolen session
    alone isn't enough); failures count like failed logins. Returns the verified
    credential's tag: pass it to ``_still_authorized()`` inside the write transaction."""
    key = sec.ip_hash()
    wait, attempt = sec.consume("admin_login_fail", key, appsettings.value("admin_login_max_per_ip"),
                                appsettings.value("admin_login_window_minutes") * 60)
    if wait:
        raise ApiError(429, tr("Too many failed logins. Please try again in {wait}.", wait=human_wait(wait)),
                       retry_after=wait)
    verified = sec.check_session_password(g.admin, str(password or ""))
    if verified is None:
        sec.audit(actor(), "admin.reauth_failed", network=sec.anonymized_ip())
        raise ApiError(403, tr("Your current password is wrong."))
    sec.release(attempt)
    return verified


def _still_authorized(verified: str) -> None:
    """Inside the write transaction: the session and the password checked before the
    lock must still be current (another admin may have changed or removed them)."""
    if not sec.session_still_valid(g.admin, verified):
        raise ApiError(401, tr("Admin login required."))


def _check_new_password(password, name: str) -> str:
    password = str(password or "")
    if len(password) < sec.MIN_PASSWORD_LENGTH:
        raise ApiError(400, tr("The new password must be at least {n} characters long.", n=sec.MIN_PASSWORD_LENGTH))
    if len(password) > 200:
        raise ApiError(400, tr("The new password must be at most 200 characters long."))
    if sec.name_key(password) == sec.name_key(name):
        raise ApiError(400, tr("The password must not be the name."))
    return password


def admin_dict(row) -> dict:
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "created_by": row["created_by"],
            "last_login_at": row["last_login_at"], "password_changed_at": row["password_changed_at"],
            "current": row["id"] == g.admin["admin_id"]}


@bp.get("/admin/admins")
@admin_required
def admin_list_admins():
    return jsonify(admins=[admin_dict(r) for r in sec.admin_accounts()],
                   start_mode=g.admin["admin_id"] is None, current_id=g.admin["admin_id"])


@bp.post("/admin/admins")
@admin_required
def admin_create_admin():
    """Add an admin account (name and password). The first one ends start mode:
    from then on ADMIN_PASSWORD no longer works and everyone logs in with an account."""
    from .api import clean_actor_name

    data = json_body()
    verified = _reauthenticate(data.get("current"))
    name = clean_actor_name(data.get("name"))
    if len(name) < 2:
        raise ApiError(400, tr("The name must be between 2 and 40 characters."))
    password_hash = sec.hash_password(_check_new_password(data.get("password"), name))  # before the lock
    try:
        with write_transaction() as db:
            _still_authorized(verified)     # e.g. a parallel request created the first account
            first = not sec.accounts_exist()
            admin_id = sec.create_admin_account(name, password_hash, actor())
            if first:
                # start mode is over: its sessions end, everyone logs in with an account
                db.execute("DELETE FROM admin_sessions WHERE admin_id IS NULL")
            sec.record_audit(actor(), "admin.account_created", name=name, admin_id=admin_id, first=first)
    except IntegrityError:
        raise ApiError(409, tr("There already is an admin called {name}.", name=name)) from None
    return jsonify(id=admin_id, name=name, start_mode_ended=first), 201


@bp.delete("/admin/admins/<int:admin_id>")
@admin_required
def admin_remove_admin(admin_id):
    """Remove another admin (their sessions end with it). Nobody removes themselves,
    so there is always at least one account once there was one."""
    verified = _reauthenticate(json_body().get("current"))
    if admin_id == g.admin["admin_id"]:
        raise ApiError(409, tr("You can't remove your own account."))
    with write_transaction() as db:
        # two admins removing each other: the second finds its own account gone
        _still_authorized(verified)
        row = db.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
        if row is None:
            raise ApiError(404, tr("Not found."))
        db.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        db.execute("DELETE FROM admin_users WHERE id = ?", (admin_id,))
        sec.record_audit(actor(), "admin.account_removed", name=row["name"], admin_id=admin_id)
    return jsonify(ok=True)


@bp.post("/admin/password")
@admin_required
def admin_change_password():
    """Change your own password (needs the current one). Your other sessions end;
    this one continues. Not available in start mode (create an account first)."""
    if g.admin["admin_id"] is None:
        raise ApiError(409, tr("Create an admin account first; ADMIN_PASSWORD itself is changed on the server."))
    data = json_body()
    verified = _reauthenticate(data.get("current"))
    new = _check_new_password(data.get("new"), g.admin["actor"])
    if sec.check_session_password(g.admin, new):
        raise ApiError(400, tr("The new password must differ from the current one."))
    new_hash = sec.hash_password(new)       # scrypt outside the lock
    with write_transaction() as db:
        _still_authorized(verified)         # a parallel change already replaced it
        sec.set_admin_account_password(g.admin["admin_id"], new_hash)
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (g.admin["token_hash"],))
        # this device stays logged in, with a session for the new password
        token = sec.insert_admin_session(g.admin["actor"], g.admin["admin_id"], sec.session_tag(g.admin["admin_id"]))
        sec.record_audit(actor(), "admin.password_changed", network=sec.anonymized_ip())
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
         "last_seen": _iso(r["last_seen"]), "current": r["token_hash"] == mine} for r in rows])


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
    accounts = sec.admin_accounts()
    if not accounts:
        add("accounts", "warn", tr("There are no admin accounts yet: anyone with ADMIN_PASSWORD can log in under "
                                   "any name. Create an account for each admin below."))
        if len(config["ADMIN_PASSWORD"]) < 16:
            add("password-length", "warn", tr("The admin password is shorter than 16 characters."))
    else:
        add("accounts", "ok", tr("Admin accounts: {count}. ADMIN_PASSWORD no longer works.", count=len(accounts)))
        year_ago = datetime.fromisoformat(utc_now(-24 * 365))
        stale = [a["name"] for a in accounts
                 if a["password_changed_at"] and datetime.fromisoformat(a["password_changed_at"]) < year_ago]
        if stale:
            add("password-age", "info", tr("Passwords unchanged for over a year: {names}.", names=", ".join(stale)))
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

