"""JSON API for participants and admins."""

from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import json
import math
import re
import sqlite3
import time
import unicodedata

from flask import Blueprint, current_app, g, jsonify, request

from . import events as ev
from . import layout as lay
from . import security as sec
from . import settings as cfg
from . import themes as th
from .i18n import N_, tr
from .db import get_db, read_snapshot, write_transaction

bp = Blueprint("api", __name__, url_prefix="/api")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


@bp.errorhandler(ApiError)
def _api_error(err: ApiError):
    resp = jsonify(error=err.message)
    resp.status_code = err.status
    if err.retry_after:
        resp.headers["Retry-After"] = str(err.retry_after)
    return resp


@bp.errorhandler(cfg.SettingsError)
@bp.errorhandler(lay.LayoutError)
@bp.errorhandler(ev.EventError)
def _validation_error(err: ValueError):
    return jsonify(error=str(err)), 400


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):  # e.g. 1e400 overflows to infinity
        raise ValueError("number out of range")
    return value


def json_body() -> dict:
    """Strictly parse a JSON object body (rejects NaN/Infinity, deep nesting, non-JSON types)."""
    if not request.is_json:
        raise ApiError(415, tr("Expected a JSON request body (Content-Type: application/json)."))
    try:
        data = json.loads(request.get_data(cache=True) or b"null",
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise ApiError(400, tr("Malformed JSON.")) from None
    if not isinstance(data, dict):
        raise ApiError(400, tr("Expected a JSON object."))
    return data


def conf(key):
    return current_app.config[key]


def human_wait(seconds: int) -> str:
    return tr("{n} seconds", n=seconds) if seconds < 120 else tr("{n} minutes", n=(seconds + 59) // 60)


def enforce_limit(bucket: str, key: str, limit: int, window: float, message: str) -> None:
    """Count this request against a limit (atomically) or refuse it with 429."""
    wait, _ = sec.consume(bucket, key, limit, window)
    if wait:
        raise ApiError(429, tr("{message} Please try again in {wait}.", message=message, wait=human_wait(wait)),
                       retry_after=wait)


def event_settings(event_id: int) -> dict:
    cache = g.setdefault("settings_by_event", {})
    if event_id not in cache:
        cache[event_id] = cfg.load(event_id)
    return cache[event_id]


def active_event_id() -> int | None:
    return ev.active_id()


def settings() -> dict:
    """Settings of the active event (defaults while no event is active)."""
    event_id = active_event_id()
    return event_settings(event_id) if event_id is not None else cfg.from_rows([])


def require_active_event() -> int:
    event_id = active_event_id()
    if event_id is None:
        raise ApiError(403, tr("There is no event open for registration right now."))
    return event_id


def utc_iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- event code ----------------------------------------------------------------

def registration_open() -> bool:
    """Secure by default: without an active event that has an event code, names
    stay hidden and registration is closed."""
    return active_event_id() is not None and bool(settings()["event_code"])


def check_event_code(value, *, raise_when_limited: bool) -> bool:
    """Compare a submitted event code; wrong codes count towards the per-IP limit.

    Once the limit is reached no code is checked at all (so guessing via any
    endpoint stops), and ``raise_when_limited`` decides between a 429 error and
    simply treating the code as wrong. Without a configured code nothing matches.
    """
    expected = settings()["event_code"]
    if not expected:
        return False
    code = cfg.normalize_event_code(value) if isinstance(value, str) else ""
    if not code:
        return False
    # Reserve an attempt first (atomically) and give it back if the code is right,
    # so parallel guesses can never exceed the limit.
    wait, attempt = sec.consume("event_code_fail", sec.ip_hash(), conf("EVENT_CODE_MAX_PER_IP"), 900)
    if wait:
        if raise_when_limited:
            raise ApiError(429, tr("Too many wrong event codes. Please try again in {wait}.", wait=human_wait(wait)),
                           retry_after=wait)
        return False
    if len(code) <= 20 and hmac.compare_digest(code.encode(), expected.encode()):
        sec.release(attempt)
        return True
    return False


def is_admin_request() -> bool:
    return bool(conf("ADMIN_PASSWORD")) and sec.current_admin_session() is not None


# --- reservation expiry ----------------------------------------------------

@bp.before_request
def release_expired_reservations():
    # only the active event's reservations run out; archived events stay as they ended
    event_id = active_event_id()
    if event_id is None:
        return
    hours = settings()["reservation_hours"]
    if hours <= 0:
        return
    expired_sql = ("SELECT id, name, email, seat FROM users WHERE event_id = ? AND paid = 0 "
                   "AND seat IS NOT NULL AND reserved_at IS NOT NULL AND reserved_at <= datetime('now', ?)")
    window = (event_id, f"-{hours} hours")
    if not get_db().execute(expired_sql, window).fetchone():
        return  # cheap check without taking the write lock
    with write_transaction() as db:
        # read the rows (with their seats) and clear them under the same lock
        released = db.execute(expired_sql, window).fetchall()
        db.executemany(
            "UPDATE users SET seat = NULL, seat_selected_at = NULL, reserved_at = NULL, "
            "reserved_ip_hash = NULL WHERE id = ? AND paid = 0",
            [(r["id"],) for r in released],
        )
    for r in released:
        sec.audit("system", "reservation.expired", user_id=r["id"], name=r["name"],
                  email=r["email"], seat=r["seat"], after_hours=hours, event_id=event_id)


# --- serialisation -------------------------------------------------------

def public_settings(s: dict) -> dict:
    return {
        "event_name": s["event_name"],
        "registration_opens": s["registration_opens"],
        "event_start": s["event_start"],
        "event_end": s["event_end"],
        "location": s["location"],
        "description": s["description"],
        "paypal_address": s["paypal_address"],
        "amount_cents": s["amount_cents"],
        "reservation_hours": s["reservation_hours"],
        "event_code": s["event_code"],
        "theme": s["theme"],
    }


def reserved_until(row) -> str | None:
    # reservations only run out while their event is active
    if row["event_id"] != active_event_id():
        return None
    hours = event_settings(row["event_id"])["reservation_hours"]
    if row["paid"] or not row["seat"] or not row["reserved_at"] or hours <= 0:
        return None
    return utc_iso(datetime.fromisoformat(row["reserved_at"]) + timedelta(hours=hours))


def user_dict(row, with_email=True) -> dict:
    data = {
        "id": row["id"],
        "event_id": row["event_id"],
        "name": row["name"],
        "seat": row["seat"],
        "paid": bool(row["paid"]),
        "created_at": row["created_at"],
        "seat_selected_at": row["seat_selected_at"],
        "paid_at": row["paid_at"],
        "paid_amount_cents": row["paid_amount_cents"],
        "reserved_until": reserved_until(row),
    }
    if with_email:
        data["email"] = row["email"]
    return data


def payment_info(user_row) -> dict:
    s = event_settings(user_row["event_id"])
    return {
        "paypal_address": s["paypal_address"],
        "amount_cents": s["amount_cents"],
        "reference": tr("{event} - Seat {seat} - {name}", event=s["event_name"], seat=user_row["seat"],
                        name=user_row["name"]),
        "reserved_until": reserved_until(user_row),
    }


def user_summary(row) -> dict:
    """Identifying fields for audit entries."""
    return {"user_id": row["id"], "name": row["name"], "email": row["email"], "seat": row["seat"],
            "event_id": row["event_id"]}


# --- participants --------------------------------------------------------

def issue_token(user_id: int) -> str:
    token = sec.new_secret()
    db = get_db()
    db.execute("INSERT INTO user_tokens (token_hash, user_id) VALUES (?, ?)", (sec.sha256(token), user_id))
    db.commit()
    return token


def current_user():
    """The participant this device is logged in as, for the active event only:
    tokens from earlier events no longer log anyone in."""
    token = request.headers.get("X-User-Token", "")
    event_id = active_event_id()
    if not token or event_id is None:
        return None
    return get_db().execute(
        "SELECT users.* FROM user_tokens JOIN users ON users.id = user_tokens.user_id "
        "WHERE user_tokens.token_hash = ? AND users.event_id = ?",
        (sec.sha256(token), event_id),
    ).fetchone()


def require_user():
    user = current_user()
    if user is None:
        raise ApiError(401, tr("Please register or log in first."))
    return user


def get_user(user_id: int):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ApiError(404, tr("User not found."))
    return row


ARCHIVED_READ_ONLY = N_("This event is archived; its participants and seat plan can't be changed any more.")

PAID_NO_RELEASE = N_("Your payment is already confirmed, so the seat can't be given up here. "
                      "You can switch to another free seat, or ask an admin to cancel.")


def assign_seat(user_id: int, seat_value, *, by_participant: bool = False) -> None:
    """Assign (or clear, if seat_value is None) a seat for a user.

    Runs as one write transaction: the paid state, the layout, the per-IP
    pending limit and the seat itself are all read and changed under the same
    lock, so concurrent payment confirmations, layout saves or reservations
    cannot interleave. ``by_participant`` applies the participant rules (a paid
    seat can be switched but not released, new unpaid reservations count against
    the per-IP limit).

    The unpaid reservation window starts with the first seat pick and is *not*
    reset by switching seats or by releasing and re-picking within the window.
    """
    ip_key = sec.ip_hash() if by_participant else None  # may write; keep outside the transaction
    with write_transaction() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise ApiError(404, tr("User not found."))
        event_id = user["event_id"]
        # read fresh under the lock: the dates may just have changed
        event = cfg.load(event_id)
        state = ev.status(event)
        if by_participant and state != "active":
            raise ApiError(403, tr("Registration for this event is closed."))
        if state == "archived":
            raise ApiError(409, tr(ARCHIVED_READ_ONLY))
        hours = event["reservation_hours"]
        window = f"-{hours} hours" if hours > 0 else "-1000 years"
        if by_participant and user["paid"] and seat_value is None:
            # giving up a paid seat means a refund: that stays with the admins
            raise ApiError(409, tr(PAID_NO_RELEASE))
        if seat_value is None:
            db.execute("UPDATE users SET seat = NULL, seat_selected_at = NULL WHERE id = ?", (user_id,))
            return
        seat = lay.normalize_seat(seat_value)
        if seat is None or seat not in set(lay.seat_ids(lay.load(event_id))):
            raise ApiError(400, tr("This seat does not exist."))
        if by_participant and user["seat"] is None and not user["paid"]:
            # new unpaid reservation (switching seats does not count)
            pending = db.execute(
                "SELECT COUNT(*) FROM users WHERE event_id = ? AND paid = 0 AND seat IS NOT NULL "
                "AND reserved_ip_hash = ?",
                (event_id, ip_key),
            ).fetchone()[0]
            if pending >= conf("MAX_PENDING_PER_IP"):
                raise ApiError(429, tr("Too many unpaid reservations from your network. "
                                       "Please wait until earlier reservations are paid or released."))
        else:
            ip_key = None  # keep the IP of the original reservation
        try:
            db.execute(
                "UPDATE users SET seat = ?, seat_selected_at = datetime('now'), "
                "reserved_at = CASE WHEN reserved_at IS NULL OR reserved_at <= datetime('now', ?) "
                "                   THEN datetime('now') ELSE reserved_at END, "
                "reserved_ip_hash = COALESCE(?, reserved_ip_hash) "
                "WHERE id = ?",
                (seat, window, ip_key, user_id),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, tr("Seat {seat} is already taken.", seat=seat)) from None


@bp.get("/info")
def info():
    event_id = active_event_id()
    data = public_settings(settings())
    data.pop("event_code")
    data["active"] = event_id is not None
    data["registration_open"] = registration_open()
    data["seat_count"] = len(lay.seat_ids(lay.load(event_id))) if event_id is not None else 0
    data["login_link_hours"] = conf("LOGIN_CODE_HOURS")
    data["upcoming"] = [
        {key: e[key] for key in ("event_name", "registration_opens", "event_start", "event_end", "location")}
        for e in ev.upcoming()
    ]
    return jsonify(data)


@bp.get("/seats")
def seats():
    # Layout, occupants and the participant must come from the same state: a
    # layout save renames seats, and mixing old layout with new seat ids would
    # show occupied seats as free or people in the wrong group. The checks below
    # write (rate limits, session refresh), so they run after the snapshot.
    with read_snapshot() as db:
        event_id = active_event_id()
        if event_id is None:
            return jsonify(layout=None, seats=[], names_visible=False, event_code_ok=False)
        layout = lay.load(event_id)
        rows = db.execute("SELECT seat, name, paid FROM users WHERE event_id = ? AND seat IS NOT NULL",
                          (event_id,)).fetchall()
        me = current_user()
    # Having a valid registration code and being allowed to see names are
    # different things: admins see names but still need the code to register.
    header = request.headers.get("X-Event-Code")
    code_ok = me is None and bool(header) and check_event_code(header, raise_when_limited=False)
    show_names = me is not None or code_ok or is_admin_request()
    result = seat_list(layout, rows, me["seat"] if me is not None else None, show_names)
    return jsonify(layout=layout, seats=result, names_visible=show_names, event_code_ok=code_ok)


def seat_list(layout: dict, occupants, my_seat: str | None, show_names: bool) -> list[dict]:
    taken = {r["seat"]: r for r in occupants}
    result = []
    for seat in lay.seat_ids(layout):
        occupant = taken.get(seat)
        if occupant is None:
            result.append({"id": seat, "status": "free"})
        else:
            entry = {
                "id": seat,
                "status": "paid" if occupant["paid"] else "reserved",
                "mine": my_seat == seat,
            }
            if show_names:
                entry["name"] = occupant["name"]
            result.append(entry)
    return result


@bp.post("/event-code")
def verify_event_code():
    """Check the event code (to unlock names on the seat plan before registering)."""
    if not registration_open():
        raise ApiError(403, tr("Registration hasn't opened yet."))
    if not check_event_code(json_body().get("code"), raise_when_limited=True):
        raise ApiError(400, tr("Wrong event code."))
    return jsonify(ok=True)


@bp.post("/register")
def register():
    data = json_body()
    key = sec.ip_hash()
    enforce_limit("register", key, conf("REGISTER_MAX_PER_IP"), 3600,
                  tr("Too many registrations from your network."))
    if not registration_open():
        raise ApiError(403, tr("Registration hasn't opened yet."))
    if not check_event_code(data.get("event_code"), raise_when_limited=True):
        raise ApiError(403, tr("Please enter the correct event code to register."))

    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    if not 2 <= len(name) <= 60:
        raise ApiError(400, tr("Name must be between 2 and 60 characters."))
    if len(email) > 200 or not EMAIL_RE.match(email):
        raise ApiError(400, tr("Please enter a valid email address."))
    token = sec.new_secret()
    try:
        with write_transaction() as db:
            event_id = require_active_event()
            if ev.status(cfg.load(event_id)) != "active":  # dates changed in the meantime
                raise ApiError(403, tr("There is no event open for registration right now."))
            cur = db.execute(
                "INSERT INTO users (event_id, name, email, token_hash) VALUES (?, ?, ?, ?)",
                (event_id, name, email, sec.sha256(token)),
            )
            db.execute("INSERT INTO user_tokens (token_hash, user_id) VALUES (?, ?)",
                       (sec.sha256(token), cur.lastrowid))
    except sqlite3.IntegrityError:
        raise ApiError(
            409, tr("This email address is already registered. Open a login link from a device "
                    "where you are logged in, or ask an admin for one.")
        ) from None
    return jsonify(token=token, user=user_dict(get_user(cur.lastrowid))), 201


@bp.get("/me")
def me():
    user = require_user()
    return jsonify(user=user_dict(user), payment=payment_info(user) if user["seat"] else None)


@bp.post("/me/seat")
def choose_seat():
    user = require_user()
    seat = json_body().get("seat")
    if seat is None:
        raise ApiError(400, tr("This seat does not exist."))
    assign_seat(user["id"], seat, by_participant=True)
    updated = get_user(user["id"])
    if updated["paid"] and updated["seat"] != user["seat"]:
        # the payment moves with the person; admins see it in the audit log
        sec.audit(f"participant:{user['id']}", "user.seat_switched",
                  **user_summary(user), to=updated["seat"])
    return jsonify(user=user_dict(updated), payment=payment_info(updated))


@bp.delete("/me/seat")
def release_seat():
    user = require_user()
    assign_seat(user["id"], None, by_participant=True)
    return jsonify(user=user_dict(get_user(user["id"])))


# --- one-time login links ------------------------------------------------

def create_login_code(user_id: int) -> tuple[str, str]:
    code = sec.new_secret()
    now = time.time()
    expires = now + conf("LOGIN_CODE_HOURS") * 3600
    db = get_db()
    db.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))
    db.execute("INSERT INTO login_codes (code_hash, user_id, expires_at) VALUES (?, ?, ?)",
               (sec.sha256(code), user_id, expires))
    db.commit()
    return code, utc_iso(datetime.fromtimestamp(expires, timezone.utc).replace(tzinfo=None))


def lookup_login_code(code):
    """A valid login code of a participant of the active event."""
    event_id = active_event_id()
    if not isinstance(code, str) or not 10 <= len(code) <= 100 or event_id is None:
        return None
    return get_db().execute(
        "SELECT users.*, login_codes.code_hash FROM login_codes "
        "JOIN users ON users.id = login_codes.user_id "
        "WHERE login_codes.code_hash = ? AND login_codes.expires_at >= ? AND users.event_id = ?",
        (sec.sha256(code), time.time(), event_id),
    ).fetchone()


def _login_code_attempt():
    key = sec.ip_hash()
    enforce_limit("login_code", key, conf("LOGIN_CODE_MAX_PER_IP"), 900, tr("Too many login attempts."))


@bp.post("/me/login-link")
def my_login_link():
    user = require_user()
    enforce_limit("login_link", str(user["id"]), 10, 3600, tr("Too many login links requested."))
    code, expires = create_login_code(user["id"])
    return jsonify(code=code, expires_at=expires)


@bp.post("/login/preview")
def login_preview():
    """Tell the browser whose account a login code belongs to, without using it up."""
    _login_code_attempt()
    row = lookup_login_code(json_body().get("code"))
    if row is None:
        raise ApiError(400, tr("This login link is invalid, expired or has already been used."))
    return jsonify(user_id=row["id"], name=row["name"])


@bp.post("/login")
def login_with_code():
    _login_code_attempt()
    row = lookup_login_code(json_body().get("code"))
    if row is None:
        raise ApiError(400, tr("This login link is invalid, expired or has already been used."))
    db = get_db()
    deleted = db.execute("DELETE FROM login_codes WHERE code_hash = ?", (row["code_hash"],)).rowcount
    db.commit()
    if not deleted:  # used concurrently
        raise ApiError(400, tr("This login link has already been used."))
    token = issue_token(row["id"])
    return jsonify(token=token, user=user_dict(get_user(row["id"])))


# --- admin ---------------------------------------------------------------

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not conf("ADMIN_PASSWORD"):
            raise ApiError(401, tr("Admin login required."))
        session = sec.current_admin_session()
        if session is None:
            raise ApiError(401, tr("Admin login required."))
        g.admin = session
        return view(*args, **kwargs)
    return wrapper


def actor() -> str:
    return g.admin["actor"]


def admin_event_id() -> int:
    """The event an admin request is about: ``?event=<id>``, else the default one."""
    raw = request.args.get("event")
    if raw is None:
        return ev.default_admin_id()
    if not raw.isdigit() or len(raw) > 9:
        raise ApiError(400, tr("'event' must be an event id."))
    if ev.get(int(raw)) is None:
        raise ApiError(404, tr("Event not found."))
    return int(raw)


def require_not_archived(event_id: int) -> None:
    """Call inside a write transaction (the status is read fresh)."""
    if ev.status(cfg.load(event_id)) == "archived":
        raise ApiError(409, tr(ARCHIVED_READ_ONLY))


def event_summary(e: dict) -> dict:
    db = get_db()
    counts = db.execute(
        "SELECT COUNT(*) AS registered, COUNT(seat) AS seated, COALESCE(SUM(paid), 0) AS paid "
        "FROM users WHERE event_id = ?", (e["id"],)).fetchone()
    return {
        "id": e["id"],
        "status": e["status"],
        **{key: e[key] for key in ("event_name", "registration_opens", "event_start", "event_end", "location")},
        "registered": counts["registered"],
        "seated": counts["seated"],
        "paid": counts["paid"],
    }


def clean_actor_name(value) -> str:
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch)[0] != "C").strip()
    return text[:40] or "admin"


@bp.post("/admin/login")
def admin_login():
    expected = conf("ADMIN_PASSWORD")
    if not expected:
        raise ApiError(503, tr("Admin login is disabled: ADMIN_PASSWORD is not set."))
    data = json_body()
    ip = sec.client_ip()
    key = sec.ip_hash(ip)
    window = conf("ADMIN_LOGIN_WINDOW_MINUTES") * 60
    # Reserve an attempt in both buckets before checking the password (atomic, so
    # parallel guesses cannot exceed the limits); a successful login gives them back.
    wait, ip_attempt = sec.consume("admin_login_fail", key, conf("ADMIN_LOGIN_MAX_PER_IP"), window)
    global_attempt = None
    if not wait:
        wait, global_attempt = sec.consume("admin_login_fail", "*", conf("ADMIN_LOGIN_MAX_GLOBAL"), window)
        if wait:
            sec.release(ip_attempt)
    if wait:
        raise ApiError(429, tr("Too many failed logins. Please try again in {wait}.", wait=human_wait(wait)),
                       retry_after=wait)
    name = clean_actor_name(data.get("name"))
    password = str(data.get("password") or "")
    if not hmac.compare_digest(password.encode(), expected.encode()):
        sec.audit("anonymous", "admin.login_failed", ip=ip, name=name)
        raise ApiError(401, tr("Wrong password."))
    sec.release(ip_attempt)
    sec.release(global_attempt)
    token = sec.create_admin_session(name)
    sec.audit(name, "admin.login", ip=ip)
    resp = jsonify(ok=True, actor=name)
    sec.set_admin_cookie(resp, token)
    return resp


@bp.post("/admin/logout")
def admin_logout():
    session = sec.current_admin_session() if conf("ADMIN_PASSWORD") else None
    if session is not None:
        sec.end_admin_session()
        sec.audit(session["actor"], "admin.logout")
    resp = jsonify(ok=True)
    sec.set_admin_cookie(resp, None)
    return resp


@bp.post("/admin/logout-all")
@admin_required
def admin_logout_all():
    count = sec.end_all_admin_sessions()
    sec.audit(actor(), "admin.logout_all", sessions=count)
    resp = jsonify(ok=True, sessions=count)
    sec.set_admin_cookie(resp, None)
    return resp


@bp.get("/admin/session")
def admin_session():
    session = sec.current_admin_session() if conf("ADMIN_PASSWORD") else None
    return jsonify(admin=session is not None, actor=session["actor"] if session else None)


@bp.get("/admin/audit")
@admin_required
def admin_audit():
    return jsonify(entries=sec.audit_entries())


@bp.get("/admin/users")
@admin_required
def admin_users():
    event_id = admin_event_id()
    rows = get_db().execute("SELECT * FROM users WHERE event_id = ? ORDER BY created_at, id",
                            (event_id,)).fetchall()
    return jsonify(event_id=event_id, users=[user_dict(r) for r in rows])


@bp.get("/admin/seats")
@admin_required
def admin_seats():
    """Seat plan of any event, with names (the admin counterpart of /api/seats)."""
    event_id = admin_event_id()
    with read_snapshot() as db:
        layout = lay.load(event_id)
        rows = db.execute("SELECT seat, name, paid FROM users WHERE event_id = ? AND seat IS NOT NULL",
                          (event_id,)).fetchall()
    return jsonify(event_id=event_id, layout=layout, seats=seat_list(layout, rows, None, True),
                   names_visible=True)


@bp.get("/admin/themes")
@admin_required
def admin_themes():
    found = th.discover().values()
    return jsonify(
        themes=[t.public() for t in found if t.problem is None],
        problems=[{"id": t.id, "builtin": t.builtin, "problem": t.problem} for t in found if t.problem],
        themes_dir=str(th.custom_dir()),
    )


@bp.get("/admin/events")
@admin_required
def admin_events():
    with read_snapshot():
        events = [event_summary(e) for e in ev.load_all()]
    return jsonify(events=events, active_id=next((e["id"] for e in events if e["status"] == "active"), None),
                   default_id=ev.default_admin_id(), timezone=ev.timezone_name() or None)


@bp.post("/admin/events")
@admin_required
def admin_create_event():
    copy_from = json_body().get("copy_from")
    if copy_from is not None and (isinstance(copy_from, bool) or not isinstance(copy_from, int)):
        raise ApiError(400, tr("'copy_from' must be an event id or null."))
    with write_transaction():
        event_id = ev.create(copy_from)
    sec.audit(actor(), "event.created", event_id=event_id, copied_from=copy_from)
    return jsonify(event=event_summary(ev.get(event_id))), 201


@bp.delete("/admin/events/<int:event_id>")
@admin_required
def admin_delete_event(event_id):
    with write_transaction():
        deleted = ev.delete(event_id)
    sec.audit(actor(), "event.deleted", event_id=event_id, name=deleted["event_name"],
              status=deleted["status"], participants=deleted["participants"])
    return jsonify(ok=True)


@bp.post("/admin/users/<int:user_id>/paid")
@admin_required
def admin_set_paid(user_id):
    user = get_user(user_id)
    paid = json_body().get("paid")
    if not isinstance(paid, bool):
        raise ApiError(400, tr("'paid' must be true or false."))
    # The confirmed amount is stored with the payment, so later fee changes don't
    # rewrite what was actually received. Revoking a payment starts a fresh
    # reservation window. Only a real state change is applied (and audited).
    with write_transaction() as db:
        require_not_archived(user["event_id"])
        amount = cfg.load(user["event_id"])["amount_cents"]
        changed = db.execute(
            "UPDATE users SET paid = ?, paid_at = CASE WHEN ? THEN datetime('now') END, "
            "paid_amount_cents = CASE WHEN ? THEN ? END, "
            "reserved_at = CASE WHEN ? THEN reserved_at "
            "                   WHEN seat IS NOT NULL THEN datetime('now') END "
            "WHERE id = ? AND paid != ?",
            (int(paid), int(paid), int(paid), amount, int(paid), user_id, int(paid)),
        ).rowcount
    if changed:
        sec.audit(actor(), "payment.confirmed" if paid else "payment.revoked", **user_summary(user))
    return jsonify(user=user_dict(get_user(user_id)))


@bp.post("/admin/users/<int:user_id>/move")
@admin_required
def admin_move(user_id):
    user = get_user(user_id)
    data = json_body()
    if "seat" not in data:
        raise ApiError(400, tr("'seat' is required (use null to free the seat)."))
    assign_seat(user_id, data["seat"])
    updated = get_user(user_id)
    if updated["seat"] != user["seat"]:
        sec.audit(actor(), "user.moved", **user_summary(user), to=updated["seat"])
    return jsonify(user=user_dict(updated))


@bp.post("/admin/users/<int:user_id>/login-link")
@admin_required
def admin_login_link(user_id):
    user = get_user(user_id)
    if user["event_id"] != active_event_id():
        raise ApiError(409, tr("Login links only work for participants of the active event."))
    code, expires = create_login_code(user_id)
    sec.audit(actor(), "user.login_link_created", **user_summary(user))
    return jsonify(code=code, expires_at=expires)


@bp.delete("/admin/users/<int:user_id>")
@admin_required
def admin_remove(user_id):
    user = get_user(user_id)
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    sec.audit(actor(), "user.removed", **user_summary(user), paid=bool(user["paid"]))
    return jsonify(ok=True)


@bp.get("/admin/settings")
@admin_required
def admin_get_settings():
    event_id = admin_event_id()
    return jsonify(public_settings(event_settings(event_id)) | {"event_id": event_id})


@bp.put("/admin/settings")
@admin_required
def admin_put_settings():
    data = json_body()
    event_id = admin_event_id()
    # Validation reads stored values (e.g. the start date when only the end
    # changes) and the other events' dates; hold the write lock from that read
    # until the save. Archived events stay editable here, so a wrong end date
    # can be corrected.
    with write_transaction():
        before = cfg.load(event_id)
        cfg.save(cfg.validate_update(data, event_id), event_id)
        after = cfg.load(event_id)
    g.pop("settings_by_event", None)
    g.pop("active_event_id", None)
    changes = {}
    for key, old in before.items():
        new = after[key]
        if old != new:
            changes[key] = "(changed)" if key == "description" else [old, new]
    if changes:
        sec.audit(actor(), "settings.changed", event_id=event_id, **changes)
    return jsonify(public_settings(after) | {"event_id": event_id})


@bp.get("/admin/layout")
@admin_required
def admin_get_layout():
    return jsonify(lay.load(admin_event_id()))


@bp.put("/admin/layout")
@admin_required
def admin_put_layout():
    new = lay.validate(json_body())
    event_id = admin_event_id()
    # Read the current layout and occupants and save under one write lock, so no
    # seat can be reserved in between (participants' seat changes use the same lock).
    with write_transaction() as db:
        require_not_archived(event_id)
        occupants = db.execute(
            "SELECT seat, name FROM users WHERE event_id = ? AND seat IS NOT NULL ORDER BY seat",
            (event_id,),
        ).fetchall()
        moves, lost = lay.plan_seat_moves(lay.load(event_id), new, [r["seat"] for r in occupants])
        if lost:
            who = ", ".join(f"{r['seat']} ({r['name']})" for r in occupants if r["seat"] in lost)
            raise ApiError(
                409, tr("This layout would remove occupied seats: {who}. Move those people first.", who=who)
            )
        lay.save(new, moves, event_id)
    sec.audit(actor(), "layout.saved", event_id=event_id, groups=len(new["groups"]),
              items=len(new["items"]), seats=len(lay.seat_ids(new)), renumbered=moves)
    return jsonify(event_id=event_id, layout=lay.load(event_id), moved=moves)
