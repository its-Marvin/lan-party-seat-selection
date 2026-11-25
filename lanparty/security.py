"""Security helpers: client IPs, rate limiting, admin sessions, audit log.

Everything is stored in SQLite so it works across several worker processes.
"""

import hashlib
import hmac
import json
import secrets
import time

from flask import current_app, request

from .db import get_db, write_transaction

PEPPER_KEY = "_ip_pepper"
ADMIN_COOKIE = "lp_admin"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# --- client identity -------------------------------------------------------

def client_ip() -> str:
    # request.remote_addr is corrected by ProxyFix when TRUSTED_PROXIES is set
    return request.remote_addr or "unknown"


def _pepper() -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (PEPPER_KEY,)).fetchone()
    if row:
        return row["value"]
    value = secrets.token_hex(32)
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (PEPPER_KEY, value))
    db.commit()
    return db.execute("SELECT value FROM settings WHERE key = ?", (PEPPER_KEY,)).fetchone()["value"]


def ip_hash(ip: str | None = None) -> str:
    """Keyed hash of the client IP, so rate-limit tables hold no raw addresses."""
    return hmac.new(_pepper().encode(), (ip or client_ip()).encode(), hashlib.sha256).hexdigest()


# --- rate limiting ---------------------------------------------------------

def consume(bucket: str, key: str, limit: int, window: float) -> tuple[int, int | None]:
    """Atomically check a limit and record one attempt.

    Returns ``(retry_after, event_id)``: ``retry_after`` is 0 when the attempt
    is allowed (and was recorded as ``event_id``), otherwise the number of
    seconds until the next attempt is allowed. Checking and recording happen in
    one write transaction, so parallel requests cannot exceed the limit.
    """
    now = time.time()
    with write_transaction() as db:
        db.execute("DELETE FROM rate_events WHERE bucket = ? AND ts < ?", (bucket, now - window))
        events = [r["ts"] for r in db.execute(
            "SELECT ts FROM rate_events WHERE bucket = ? AND key = ? ORDER BY ts", (bucket, key))]
        if len(events) >= limit:
            return max(1, int(events[-limit] + window - now) + 1), None
        cur = db.execute("INSERT INTO rate_events (bucket, key, ts) VALUES (?, ?, ?)", (bucket, key, now))
        return 0, cur.lastrowid


def release(event_id: int | None) -> None:
    """Give back an attempt recorded by ``consume`` (e.g. after a successful login)."""
    if event_id is None:
        return
    db = get_db()
    db.execute("DELETE FROM rate_events WHERE rowid = ?", (event_id,))
    db.commit()


# --- audit log -------------------------------------------------------------

def audit(actor: str, action: str, **details) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (actor, action, details) VALUES (?, ?, ?)",
        (actor, action, json.dumps(details, ensure_ascii=False) if details else ""),
    )
    db.commit()


def audit_entries(limit: int = 200) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, ts, actor, action, details FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
         "details": json.loads(r["details"]) if r["details"] else {}}
        for r in rows
    ]


# --- admin sessions --------------------------------------------------------

def password_tag() -> str:
    """Fingerprint of the configured admin password; changing it ends all sessions."""
    return hmac.new(_pepper().encode(), current_app.config["ADMIN_PASSWORD"].encode(),
                    hashlib.sha256).hexdigest()


def create_admin_session(actor: str) -> str:
    token = new_secret()
    now = time.time()
    db = get_db()
    db.execute(
        "INSERT INTO admin_sessions (token_hash, actor, password_tag, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (sha256(token), actor, password_tag(), now, now),
    )
    db.commit()
    return token


def current_admin_session():
    """Return the valid admin session row for this request (refreshing it), or None."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        return None
    cfg = current_app.config
    now = time.time()
    db = get_db()
    db.execute(
        "DELETE FROM admin_sessions WHERE last_seen < ? OR created_at < ? OR password_tag != ?",
        (now - cfg["ADMIN_SESSION_IDLE_MINUTES"] * 60,
         now - cfg["ADMIN_SESSION_MAX_HOURS"] * 3600,
         password_tag()),
    )
    row = db.execute("SELECT * FROM admin_sessions WHERE token_hash = ?", (sha256(token),)).fetchone()
    if row is not None:
        db.execute("UPDATE admin_sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
    db.commit()
    return row


def end_admin_session() -> None:
    token = request.cookies.get(ADMIN_COOKIE)
    if token:
        db = get_db()
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (sha256(token),))
        db.commit()


def end_all_admin_sessions() -> int:
    db = get_db()
    count = db.execute("DELETE FROM admin_sessions").rowcount
    db.commit()
    return count


def set_admin_cookie(resp, token: str | None) -> None:
    secure = current_app.config["SESSION_COOKIE_SECURE"]
    if token is None:
        resp.delete_cookie(ADMIN_COOKIE, path="/api", secure=secure, httponly=True, samesite="Strict")
    else:
        resp.set_cookie(ADMIN_COOKIE, token, path="/api", secure=secure, httponly=True, samesite="Strict")
