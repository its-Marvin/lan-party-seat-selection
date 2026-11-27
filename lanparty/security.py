"""Security helpers: client IPs, rate limiting, admin sessions, audit log.

Everything is stored in the database so it works across several worker processes.
"""

import hashlib
import hmac
import ipaddress
import json
import secrets
import time

from flask import current_app, request

from . import appsettings
from .db import get_db, utc_now, write_transaction

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
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT DO NOTHING",
               (PEPPER_KEY, value))
    db.commit()
    return db.execute("SELECT value FROM settings WHERE key = ?", (PEPPER_KEY,)).fetchone()["value"]


IPV6_NETWORK_PREFIX = 64   # one IPv6 subscriber or LAN gets at least a /64
LOG_PREFIX = {4: 24, 6: 48}  # how much of an address the audit log keeps


def _parse_ip(ip: str):
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])   # drop an IPv6 zone id
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        return addr.ipv4_mapped   # ::ffff:1.2.3.4 is the IPv4 client 1.2.3.4
    return addr


def client_network(ip: str | None = None) -> str:
    """What rate limits count per: the IPv4 address, or the IPv6 /64 network.

    Anyone with IPv6 can pick any of the 2^64 addresses in their network, so
    counting single IPv6 addresses would let one device sidestep every limit."""
    ip = ip or client_ip()
    addr = _parse_ip(ip)
    if addr is None:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/{IPV6_NETWORK_PREFIX}", strict=False))
    return str(addr)


def anonymized_ip(ip: str | None = None) -> str:
    """The client's network with the host part zeroed (IPv4 /24, IPv6 /48), for the
    audit log: enough to recognise where logins came from, without the address."""
    ip = ip or client_ip()
    addr = _parse_ip(ip)
    if addr is None:
        return "unknown"
    return str(ipaddress.ip_network(f"{addr}/{LOG_PREFIX[addr.version]}", strict=False))


def ip_hash(ip: str | None = None) -> str:
    """Keyed hash of the client's network (see ``client_network``), so rate-limit
    tables hold no raw addresses."""
    return hmac.new(_pepper().encode(), client_network(ip).encode(), hashlib.sha256).hexdigest()


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
        return 0, db.insert("INSERT INTO rate_events (bucket, key, ts) VALUES (?, ?, ?)", (bucket, key, now))


def attempts(bucket: str, key: str, window: float) -> int:
    """How many attempts ``key`` has recorded in ``bucket`` within ``window`` seconds."""
    return get_db().execute(
        "SELECT COUNT(*) FROM rate_events WHERE bucket = ? AND key = ? AND ts >= ?",
        (bucket, key, time.time() - window)).fetchone()[0]


def mark_once(bucket: str, key: str, window: float) -> bool:
    """True the first time ``key`` is marked within ``window`` seconds, then False.
    Call inside ``db.write_transaction()`` so checking and marking are atomic."""
    now = time.time()
    db = get_db()
    db.execute("DELETE FROM rate_events WHERE bucket = ? AND ts < ?", (bucket, now - window))
    if db.execute("SELECT 1 FROM rate_events WHERE bucket = ? AND key = ?", (bucket, key)).fetchone():
        return False
    db.execute("INSERT INTO rate_events (bucket, key, ts) VALUES (?, ?, ?)", (bucket, key, now))
    return True


def release(event_id: int | None) -> None:
    """Give back an attempt recorded by ``consume`` (e.g. after a successful login)."""
    if event_id is None:
        return
    db = get_db()
    db.execute(f"DELETE FROM rate_events WHERE {db.rowid} = ?", (event_id,))
    db.commit()


# --- audit log -------------------------------------------------------------

def record_audit(actor: str, action: str, **details) -> None:
    """Add an audit entry without committing: use inside ``db.write_transaction()``
    so the entry describes exactly the change made under that lock."""
    get_db().execute(
        "INSERT INTO audit_log (actor, action, details) VALUES (?, ?, ?)",
        (actor, action, json.dumps(details, ensure_ascii=False) if details else ""),
    )


def audit(actor: str, action: str, **details) -> None:
    record_audit(actor, action, **details)
    get_db().commit()


def prune_audit_log() -> int:
    """Delete entries older than AUDIT_LOG_RETENTION_DAYS (0 = keep everything).
    The log names participants and their emails, so it must not grow forever."""
    days = appsettings.value("audit_log_retention_days")
    if days <= 0:
        return 0
    db = get_db()
    count = db.execute("DELETE FROM audit_log WHERE ts < ?", (utc_now(-24 * days),)).rowcount
    db.commit()
    return count


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

MIN_PASSWORD_LENGTH = 12
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                   maxmem=64 * 1024 * 1024, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


# Checked when a name has no account, so a wrong name takes as long as a wrong password.
_DUMMY_HASH = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


# --- admin accounts ------------------------------------------------------------------
#
# Admins log in with their own name and password (table admin_users). As long as no
# account exists, the app is in start mode: ADMIN_PASSWORD from the environment
# works with any name, so the first account can be created on /admin/security.
# Once there is an account, ADMIN_PASSWORD no longer works.
# ``python -m lanparty.manage reset-admin-password`` deletes every account.

def name_key(name: str) -> str:
    """Admin names are unique regardless of upper/lower case."""
    return name.casefold()


def admin_accounts() -> list:
    return get_db().execute("SELECT * FROM admin_users ORDER BY name_key").fetchall()


def accounts_exist() -> bool:
    return get_db().execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None


def admin_enabled() -> bool:
    return accounts_exist() or bool(current_app.config["ADMIN_PASSWORD"])


def authenticate_admin(name: str, password: str):
    """``(actor, admin_id)`` if the login is right, else None. In start mode (no
    accounts) the environment password works with any name, and ``admin_id`` is None."""
    if accounts_exist():
        row = get_db().execute("SELECT * FROM admin_users WHERE name_key = ?", (name_key(name),)).fetchone()
        ok = verify_password(password, row["password_hash"] if row else _dummy_hash())
        return (row["name"], row["id"]) if ok and row else None
    expected = current_app.config["ADMIN_PASSWORD"]
    # compare digests, so the comparison takes the same time whatever the lengths
    if expected and hmac.compare_digest(sha256(password), sha256(expected)):
        return name, None
    return None


def check_session_password(session, password: str) -> bool:
    """Re-check the password of the admin behind ``session`` (for sensitive changes)."""
    if session["admin_id"] is None:
        expected = current_app.config["ADMIN_PASSWORD"]
        return bool(expected) and not accounts_exist() and hmac.compare_digest(sha256(password), sha256(expected))
    row = get_db().execute("SELECT password_hash FROM admin_users WHERE id = ?", (session["admin_id"],)).fetchone()
    return row is not None and verify_password(password, row["password_hash"])


def create_admin_account(name: str, password: str, created_by: str) -> int:
    """Call inside a write transaction. Raises IntegrityError if the name is taken."""
    return get_db().insert(
        "INSERT INTO admin_users (name, name_key, password_hash, created_by, password_changed_at) "
        "VALUES (?, ?, ?, ?, ?)", (name, name_key(name), hash_password(password), created_by, utc_now()))


def set_admin_account_password(admin_id: int, password: str) -> None:
    """New password for one account; its sessions end (their tag no longer matches)."""
    get_db().execute("UPDATE admin_users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                     (hash_password(password), utc_now(), admin_id))


def remove_all_admin_accounts() -> int:
    db = get_db()
    db.execute("DELETE FROM admin_sessions")
    return db.execute("DELETE FROM admin_users").rowcount


def session_tag(admin_id: int | None) -> str | None:
    """What a valid session of this admin must carry: it changes with the password,
    so a password change (or a removed account, or leaving start mode) ends sessions."""
    if admin_id is None:
        source = current_app.config["ADMIN_PASSWORD"]
        if not source or accounts_exist():
            return None
        source = "env:" + source
    else:
        row = get_db().execute("SELECT password_hash FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
        if row is None:
            return None
        source = f"account:{admin_id}:{row['password_hash']}"
    return hmac.new(_pepper().encode(), source.encode(), hashlib.sha256).hexdigest()


def create_admin_session(actor: str, admin_id: int | None) -> str:
    token = new_secret()
    now = time.time()
    db = get_db()
    db.execute(
        "INSERT INTO admin_sessions (token_hash, actor, admin_id, password_tag, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha256(token), actor, admin_id, session_tag(admin_id), now, now),
    )
    if admin_id is not None:
        db.execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?", (utc_now(), admin_id))
    db.commit()
    return token


def current_admin_session(refresh: bool = True):
    """Return the valid admin session row for this request, or None.

    ``refresh`` counts the request as activity (extends the idle timeout);
    background polling passes False."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        return None
    now = time.time()
    db = get_db()
    db.execute(
        "DELETE FROM admin_sessions WHERE last_seen < ? OR created_at < ?",
        (now - appsettings.value("admin_session_idle_minutes") * 60,
         now - appsettings.value("admin_session_max_hours") * 3600),
    )
    row = db.execute("SELECT * FROM admin_sessions WHERE token_hash = ?", (sha256(token),)).fetchone()
    if row is not None:
        expected = session_tag(row["admin_id"])
        if expected is None or not hmac.compare_digest(expected, row["password_tag"]):
            db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (row["token_hash"],))
            row = None
    if row is not None and refresh:
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
