"""JSON API for participants and admins."""

from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import json
import math
import re
import time
import unicodedata

from flask import Blueprint, current_app, g, jsonify, request

from . import appsettings
from . import events as ev
from . import layout as lay
from . import mail
from . import notify
from . import security as sec
from . import settings as cfg
from . import themes as th
from .i18n import N_, request_language, tr
from .db import IntegrityError, get_db, read_snapshot, utc_now, write_transaction

bp = Blueprint("api", __name__, url_prefix="/api")

# Not allowed: whitespace and the characters that mean something in a mailto: link
# (?, #, %), in HTML (<, >, ") or that real addresses (practically) never use.
_EMAIL_PART = r"[^@\s?#%<>\"(),;:\\\[\]]+"
_DOMAIN_PART = r"[^@\s?#%<>\"(),;:\\\[\]/]+"
EMAIL_RE = re.compile(rf"^{_EMAIL_PART}@{_DOMAIN_PART}\.{_DOMAIN_PART}$")


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


# NUL can't be stored in PostgreSQL text; a lone UTF-16 surrogate (\ud800 in JSON)
# is no character at all and can't be encoded as UTF-8 for either database
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _has_unstorable_text(value) -> bool:
    """Any string (or key) at any depth with NUL or a lone surrogate. Iterative:
    json.loads accepts nesting deeper than Python's recursion limit allows a recursive walk."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if _UNSTORABLE.search(item):
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def json_body() -> dict:
    """Strictly parse a JSON object body (rejects NaN/Infinity, deep nesting, non-JSON
    types, and text that can't be stored: NUL characters and lone surrogates)."""
    if not request.is_json:
        raise ApiError(415, tr("Expected a JSON request body (Content-Type: application/json)."))
    try:
        data = json.loads(request.get_data(cache=True) or b"null",
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise ApiError(400, tr("Malformed JSON.")) from None
    if not isinstance(data, dict):
        raise ApiError(400, tr("Expected a JSON object."))
    if _has_unstorable_text(data):
        raise ApiError(400, tr("Text must not contain NUL characters or invalid Unicode."))
    return data


def conf(key):
    """A setting: saved on the admin pages (appsettings), else app.config / environment."""
    return appsettings.config_value(key)


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


EVENT_CODE_WINDOW = 900  # seconds


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
    key = sec.ip_hash()
    limit = conf("EVENT_CODE_MAX_PER_IP")
    wait, attempt = sec.consume("event_code_fail", key, limit, EVENT_CODE_WINDOW)
    if wait:
        if raise_when_limited:
            raise ApiError(429, tr("Too many wrong event codes. Please try again in {wait}.", wait=human_wait(wait)),
                           retry_after=wait)
        return False
    if len(code) <= 20 and hmac.compare_digest(code.encode(), expected.encode()):
        sec.release(attempt)
        return True
    audit_event_code_lockout(key, limit)
    return False


def audit_event_code_lockout(key: str, limit: int) -> None:
    """Audit a network that used up its event-code attempts, once per lockout.
    Checked and recorded under the write lock: parallel final guesses all see
    the full count, but only the first one gets the marker."""
    if sec.attempts("event_code_fail", key, EVENT_CODE_WINDOW) < limit:
        return  # cheap check without the lock
    event_id = active_event_id()
    with write_transaction():
        if (sec.attempts("event_code_fail", key, EVENT_CODE_WINDOW) >= limit
                and sec.mark_once("event_code_lockout", key, EVENT_CODE_WINDOW)):
            sec.record_audit("anonymous", "event_code.limit_reached", network=sec.anonymized_ip(),
                             attempts=limit, event_id=event_id)


def is_admin_request() -> bool:
    return sec.admin_enabled() and sec.current_admin_session() is not None


# --- reservation expiry ----------------------------------------------------

@bp.before_request
def release_expired_reservations():
    # only the active event's reservations run out; archived events stay as they ended
    event_id = active_event_id()
    if event_id is None:
        return
    expire_unconfirmed(event_id)
    expire_reservations(event_id)


def expire_reservations(event_id: int) -> None:
    """Release unpaid seats whose payment window has run out (also run by jobs.py)."""
    hours = event_settings(event_id)["reservation_hours"]
    if hours <= 0:
        return
    expired_sql = ("SELECT id, name, email, seat FROM users WHERE event_id = ? AND paid = 0 "
                   "AND seat IS NOT NULL AND reserved_at IS NOT NULL AND reserved_at <= ?")
    window = (event_id, utc_now(-hours))
    if not get_db().execute(expired_sql, window).fetchone():
        return  # cheap check without taking the write lock
    with write_transaction() as db:
        # The event may have ended (or its settings changed) while this request
        # waited for the lock: decide again from fresh values.
        event = cfg.load(event_id)
        hours = event["reservation_hours"]
        if ev.status(event) != "active" or hours <= 0:
            return
        window = (event_id, utc_now(-hours))
        # read the rows (with their seats) and clear them under the same lock
        released = db.execute(expired_sql, window).fetchall()
        db.executemany(
            "UPDATE users SET seat = NULL, seat_selected_at = NULL, reserved_at = NULL, "
            "reserved_ip_hash = NULL WHERE id = ? AND paid = 0",
            [(r["id"],) for r in released],
        )
        for r in released:
            sec.record_audit("system", "reservation.expired", user_id=r["id"], name=r["name"],
                             email=r["email"], seat=r["seat"], after_hours=hours, event_id=event_id)


UNCONFIRMED_EXPIRED_SQL = (
    "SELECT id, name, email, seat FROM users WHERE event_id = ? AND paid = 0 "
    "AND email_confirmed_at IS NULL AND confirm_until IS NOT NULL AND confirm_until <= ?")


def expire_unconfirmed(event_id: int) -> None:
    """Delete registrations whose email address wasn't confirmed in time; their
    seats become free and the address can register again (also run by jobs.py)."""
    if not get_db().execute(UNCONFIRMED_EXPIRED_SQL, (event_id, utc_now())).fetchone():
        return
    with write_transaction() as db:
        for r in db.execute(UNCONFIRMED_EXPIRED_SQL, (event_id, utc_now())).fetchall():
            db.execute("DELETE FROM users WHERE id = ? AND email_confirmed_at IS NULL AND paid = 0", (r["id"],))
            sec.record_audit("system", "registration.unconfirmed_expired", user_id=r["id"], name=r["name"],
                             email=r["email"], seat=r["seat"], event_id=event_id)


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
        "email_confirmed": not pending_confirmation(row),
        "confirm_until": utc_iso(datetime.fromisoformat(row["confirm_until"]))
        if pending_confirmation(row) else None,
    }
    if with_email:
        data["email"] = row["email"]
    return data


def pending_confirmation(row) -> bool:
    """Registered with mail enabled and the address isn't confirmed yet."""
    return bool(row["confirm_until"]) and not row["email_confirmed_at"] and not row["paid"]


def payment_or_none(row) -> dict | None:
    """Payment details once there is a seat and (with mail) a confirmed address."""
    return payment_info(row) if row["seat"] and not pending_confirmation(row) else None


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
    """A new device token; call inside ``write_transaction()`` (does not commit)."""
    token = sec.new_secret()
    get_db().execute("INSERT INTO user_tokens (token_hash, user_id) VALUES (?, ?)",
                     (sec.sha256(token), user_id))
    return token


def revoke_tokens(user_id: int, *, only: str | None = None, keep: str | None = None) -> int:
    """Delete device tokens of a user (just ``only``, or all but ``keep``; hashes).
    Inside ``write_transaction()``. The legacy ``users.token_hash`` copy of a revoked
    token is replaced too, so no migration can ever bring it back."""
    db = get_db()
    if only is not None:
        hashes = [only]
    else:
        hashes = [r["token_hash"] for r in db.execute(
            "SELECT token_hash FROM user_tokens WHERE user_id = ? AND token_hash != ?",
            (user_id, keep or ""))]
    for token_hash in hashes:
        db.execute("DELETE FROM user_tokens WHERE token_hash = ? AND user_id = ?", (token_hash, user_id))
        # NOT NULL UNIQUE column: a fresh random value that no device holds
        db.execute("UPDATE users SET token_hash = ? WHERE id = ? AND token_hash = ?",
                   ("revoked:" + sec.new_secret(), user_id, token_hash))
    return len(hashes)


def current_token_hash() -> str:
    return sec.sha256(request.headers.get("X-User-Token", ""))


def token_still_valid(db, user_id: int) -> bool:
    """Re-check the caller's token under the write lock: another device may have
    logged this one out since ``require_user()``."""
    return db.execute("SELECT 1 FROM user_tokens WHERE token_hash = ? AND user_id = ?",
                      (current_token_hash(), user_id)).fetchone() is not None


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


def assign_seat(user_id: int, seat_value, *, by_participant: bool = False, admin: str | None = None):
    """Assign (or clear, if seat_value is None) a seat for a user; returns the updated row.

    Runs as one write transaction: the paid state, the layout, the per-IP
    pending limit and the seat itself are all read and changed under the same
    lock, so concurrent payment confirmations, layout saves or reservations
    cannot interleave. ``by_participant`` applies the participant rules (a paid
    seat can be switched but not released, new unpaid reservations count against
    the per-IP limit).

    The unpaid reservation window starts with the first seat pick and is *not*
    reset by switching seats or by releasing and re-picking within the window.

    The audit entry (``user.moved`` by ``admin``, or ``user.seat_switched`` when a
    paid participant switches) is written in the same transaction, so it records
    exactly the seats this call changed, even when other requests run concurrently.
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
        # reservations started before this have run out ("" = never: no timestamp sorts before it)
        cutoff = utc_now(-hours) if hours > 0 else ""
        if by_participant and user["paid"] and seat_value is None:
            # giving up a paid seat means a refund: that stays with the admins
            raise ApiError(409, tr(PAID_NO_RELEASE))
        if seat_value is None:
            db.execute("UPDATE users SET seat = NULL, seat_selected_at = NULL WHERE id = ?", (user_id,))
            return _audit_seat_change(db, user, by_participant, admin)
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
        now = utc_now()
        try:
            # an unconfirmed registration holds its seat until confirm_until; the
            # payment window only starts once the address is confirmed
            db.execute(
                "UPDATE users SET seat = ?, seat_selected_at = ?, "
                "reserved_at = CASE WHEN ? = 1 THEN NULL "
                "                   WHEN reserved_at IS NULL OR reserved_at <= ? THEN ? "
                "                   ELSE reserved_at END, "
                "reserved_ip_hash = COALESCE(?, reserved_ip_hash) "
                "WHERE id = ?",
                (seat, now, int(pending_confirmation(user)), cutoff, now, ip_key, user_id),
            )
        except IntegrityError:
            raise ApiError(409, tr("Seat {seat} is already taken.", seat=seat)) from None
        after = _audit_seat_change(db, user, by_participant, admin)
        if (mail.enabled() and after["seat"] and not after["paid"] and after["reserved_at"]
                and after["reserved_at"] != user["reserved_at"]):
            notify.payment_request(after, event)   # a new payment window has started
        return after


def _audit_seat_change(db, before, by_participant: bool, admin: str | None):
    """Audit a seat change inside assign_seat's transaction; returns the updated row."""
    after = db.execute("SELECT * FROM users WHERE id = ?", (before["id"],)).fetchone()
    if after["seat"] != before["seat"]:
        if admin is not None:
            sec.record_audit(admin, "user.moved", **user_summary(before), to=after["seat"])
        elif by_participant and before["paid"]:
            # the payment moves with the person; admins see it in the audit log
            sec.record_audit(f"participant:{before['id']}", "user.seat_switched",
                             **user_summary(before), to=after["seat"])
    return after


@bp.get("/info")
def info():
    event_id = active_event_id()
    data = public_settings(settings())
    data.pop("event_code")
    data["active"] = event_id is not None
    data["registration_open"] = registration_open()
    data["seat_count"] = len(lay.seat_ids(lay.load(event_id))) if event_id is not None else 0
    data["login_link_hours"] = conf("LOGIN_CODE_HOURS")
    data["mail_enabled"] = mail.enabled()
    # the theme /theme.css resolves to (falls back like it), so open pages can follow changes
    data["theme"] = (th.get(data["theme"]) or th.get(th.DEFAULT_THEME)).id
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
    # with mail, the address must be confirmed within EMAIL_CONFIRM_MINUTES
    confirm_code = sec.new_secret() if mail.enabled() else None
    try:
        with write_transaction() as db:
            event_id = require_active_event()
            event = cfg.load(event_id)
            if ev.status(event) != "active":  # dates changed in the meantime
                raise ApiError(403, tr("There is no event open for registration right now."))
            user_id = db.insert(
                "INSERT INTO users (event_id, name, email, token_hash, lang, confirm_code_hash, confirm_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, name, email, sec.sha256(token), request_language(),
                 sec.sha256(confirm_code) if confirm_code else None,
                 utc_now(conf("EMAIL_CONFIRM_MINUTES") / 60) if confirm_code else None),
            )
            db.execute("INSERT INTO user_tokens (token_hash, user_id) VALUES (?, ?)",
                       (sec.sha256(token), user_id))
            if confirm_code:
                notify.confirmation(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone(),
                                    event, confirm_code)
    except IntegrityError:
        raise ApiError(
            409, tr("This email address is already registered. Open a login link from a device "
                    "where you are logged in, or ask an admin for one.")
        ) from None
    mail.flush_soon()
    return jsonify(token=token, user=user_dict(get_user(user_id)), confirmation_required=bool(confirm_code)), 201


@bp.get("/me")
def me():
    user = require_user()
    return jsonify(user=user_dict(user), payment=payment_or_none(user))


@bp.post("/me/seat")
def choose_seat():
    user = require_user()
    seat = json_body().get("seat")
    if seat is None:
        raise ApiError(400, tr("This seat does not exist."))
    updated = assign_seat(user["id"], seat, by_participant=True)
    mail.flush_soon()
    return jsonify(user=user_dict(updated), payment=payment_or_none(updated))


@bp.delete("/me/seat")
def release_seat():
    user = require_user()
    assign_seat(user["id"], None, by_participant=True)
    return jsonify(user=user_dict(get_user(user["id"])))


# --- one-time login links ------------------------------------------------

def create_login_code(user_id: int) -> tuple[str, str]:
    """Call inside ``write_transaction()`` (does not commit)."""
    code = sec.new_secret()
    now = time.time()
    expires = now + conf("LOGIN_CODE_HOURS") * 3600
    db = get_db()
    db.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))
    db.execute("INSERT INTO login_codes (code_hash, user_id, expires_at) VALUES (?, ?, ?)",
               (sec.sha256(code), user_id, expires))
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
    with write_transaction() as db:
        # a link created by a device that was just logged out would let it back in
        if not token_still_valid(db, user["id"]):
            raise ApiError(401, tr("Please register or log in first."))
        code, expires = create_login_code(user["id"])
    return jsonify(code=code, expires_at=expires)


@bp.post("/me/logout")
def my_logout():
    """Log this device out: its token stops working (not only forgotten by the browser)."""
    user = require_user()
    with write_transaction():
        revoke_tokens(user["id"], only=current_token_hash())
    return jsonify(ok=True)


@bp.post("/me/logout-others")
def my_logout_others():
    """Log out every other device (e.g. a lost phone) and void unused login links,
    which such a device could have created."""
    user = require_user()
    # one transaction with the same lock as /login and /me/login-link, so a code
    # that is being redeemed or created right now can't outlive this
    with write_transaction() as db:
        if not token_still_valid(db, user["id"]):
            raise ApiError(401, tr("Please register or log in first."))
        devices = revoke_tokens(user["id"], keep=current_token_hash())
        links = db.execute("DELETE FROM login_codes WHERE user_id = ?", (user["id"],)).rowcount
        # the personal link from the payment mail is replaced, and the new one mailed
        new_link = False
        fresh = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if fresh["access_code_hash"]:
            code = sec.new_secret() if mail.enabled() else None
            db.execute("UPDATE users SET access_code_hash = ? WHERE id = ?",
                       (sec.sha256(code) if code else None, user["id"]))
            if code:
                notify.new_access_link(fresh, cfg.load(fresh["event_id"]), code)
                new_link = True
        sec.record_audit(f"participant:{user['id']}", "user.logged_out_elsewhere", **user_summary(user),
                         devices=devices, login_links=links, personal_link_replaced=new_link)
    mail.flush_soon()
    return jsonify(ok=True, devices=devices, new_link_sent=new_link)


# --- email confirmation and personal links (mail enabled) ------------------------------

CONFIRM_INVALID = N_("This confirmation link is invalid or has expired. Registrations that aren't confirmed "
                     "in time are deleted; you can simply register again.")


def _code_shape_ok(code) -> bool:
    return isinstance(code, str) and 10 <= len(code) <= 100


@bp.post("/confirm")
def confirm_email():
    """Confirm the address with the code from the confirmation mail. ``login``
    also logs this device in (when it isn't logged in as someone else)."""
    _login_code_attempt()
    data = json_body()
    code, event_id = data.get("code"), active_event_id()
    if not _code_shape_ok(code) or event_id is None:
        raise ApiError(400, tr(CONFIRM_INVALID))
    token = None
    with write_transaction() as db:
        user = db.execute(
            "SELECT * FROM users WHERE confirm_code_hash = ? AND event_id = ? AND email_confirmed_at IS NULL "
            "AND confirm_until > ?", (sec.sha256(code), event_id, utc_now())).fetchone()
        if user is None:
            raise ApiError(400, tr(CONFIRM_INVALID))
        now = utc_now()
        # the payment window starts now (a held seat had none so far)
        db.execute(
            "UPDATE users SET email_confirmed_at = ?, confirm_code_hash = NULL, "
            "reserved_at = CASE WHEN seat IS NOT NULL AND paid = 0 THEN ? ELSE reserved_at END WHERE id = ?",
            (now, now, user["id"]))
        user = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if user["seat"] and not user["paid"]:
            notify.payment_request(user, cfg.load(event_id))
        if data.get("login") is True:
            token = issue_token(user["id"])
    mail.flush_soon()
    return jsonify(user=user_dict(user), token=token, payment=payment_or_none(user))


@bp.post("/me/confirmation")
def resend_confirmation():
    """Send the confirmation mail again (same deadline, new link)."""
    user = require_user()
    if not pending_confirmation(user):
        raise ApiError(409, tr("Your email address is already confirmed."))
    if not mail.enabled():
        raise ApiError(503, tr("Sending mails is not configured on this server."))
    enforce_limit("confirm_mail", str(user["id"]), 3, 3600, tr("Too many confirmation mails requested."))
    code = sec.new_secret()
    with write_transaction() as db:
        updated = db.execute(
            "UPDATE users SET confirm_code_hash = ? WHERE id = ? AND email_confirmed_at IS NULL AND confirm_until > ?",
            (sec.sha256(code), user["id"], utc_now())).rowcount
        if not updated:
            raise ApiError(400, tr(CONFIRM_INVALID))
        row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        notify.confirmation(row, cfg.load(row["event_id"]), code)
    mail.flush_soon()
    return jsonify(ok=True, user=user_dict(row))


def lookup_access_code(code):
    """The participant of the active event whose personal link this is."""
    event_id = active_event_id()
    if not _code_shape_ok(code) or event_id is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE access_code_hash = ? AND event_id = ?",
                            (sec.sha256(code), event_id)).fetchone()


ACCESS_INVALID = N_("This personal link no longer works. It is replaced when you log out your other devices, "
                    "and it ends with the event.")


@bp.post("/access/preview")
def access_preview():
    _login_code_attempt()
    row = lookup_access_code(json_body().get("code"))
    if row is None:
        raise ApiError(400, tr(ACCESS_INVALID))
    return jsonify(user_id=row["id"], name=row["name"])


@bp.post("/access")
def access_login():
    """Log a device in with the personal link (reusable, unlike login links)."""
    _login_code_attempt()
    code = json_body().get("code")
    with write_transaction():
        row = lookup_access_code(code)   # under the lock: a concurrent logout-others replaces it
        if row is None:
            raise ApiError(400, tr(ACCESS_INVALID))
        token = issue_token(row["id"])
    return jsonify(token=token, user=user_dict(row))


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
    # using up the code and issuing the token happen under one lock: a concurrent
    # "log out other devices" either voids the code first or revokes the new token
    with write_transaction() as db:
        deleted = db.execute("DELETE FROM login_codes WHERE code_hash = ?", (row["code_hash"],)).rowcount
        if not deleted:  # used or voided concurrently
            raise ApiError(400, tr("This login link has already been used."))
        token = issue_token(row["id"])
    return jsonify(token=token, user=user_dict(get_user(row["id"])))


# --- admin ---------------------------------------------------------------

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not sec.admin_enabled():
            raise ApiError(401, tr("Admin login required."))
        # background polling (X-Background: 1) must not count as admin activity,
        # or an unattended tab would never reach the idle timeout
        session = sec.current_admin_session(refresh=request.headers.get("X-Background") != "1")
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
    """The admin's name for the audit log, without control characters ("" if none is left)."""
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch)[0] != "C").strip()
    return text[:40].strip()


@bp.post("/admin/login")
def admin_login():
    if not sec.admin_enabled():
        raise ApiError(503, tr("Admin login is disabled: ADMIN_PASSWORD is not set."))
    data = json_body()
    # every audit entry must say which admin did it; checked before the rate limit,
    # so a forgotten name costs no login attempt (and says nothing about the password)
    name = clean_actor_name(data.get("name"))
    if not name:
        raise ApiError(400, tr("Please enter your name. It shows in the audit log."))
    key = sec.ip_hash()
    network = sec.anonymized_ip()  # the audit log keeps no full addresses
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
    password = str(data.get("password") or "")
    if not sec.check_admin_password(password):
        sec.audit("anonymous", "admin.login_failed", network=network, name=name)
        raise ApiError(401, tr("Wrong password."))
    sec.release(ip_attempt)
    sec.release(global_attempt)
    token = sec.create_admin_session(name)
    sec.audit(name, "admin.login", network=network)
    sec.prune_audit_log()
    resp = jsonify(ok=True, actor=name)
    sec.set_admin_cookie(resp, token)
    return resp


@bp.post("/admin/logout")
def admin_logout():
    session = sec.current_admin_session() if sec.admin_enabled() else None
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
    session = sec.current_admin_session() if sec.admin_enabled() else None
    return jsonify(admin=session is not None, actor=session["actor"] if session else None)


@bp.get("/admin/audit")
@admin_required
def admin_audit():
    sec.prune_audit_log()
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
        event = cfg.load(user["event_id"])
        amount = event["amount_cents"]
        now = utc_now()
        # a confirmed payment also settles an unconfirmed address: the admins vouch for it
        changed = db.execute(
            "UPDATE users SET paid = ?, paid_at = ?, paid_amount_cents = ?, "
            "reserved_at = CASE WHEN ? = 1 THEN reserved_at "
            "                   WHEN seat IS NOT NULL THEN ? END, "
            "email_confirmed_at = CASE WHEN ? = 1 AND confirm_until IS NOT NULL "
            "                          THEN COALESCE(email_confirmed_at, ?) ELSE email_confirmed_at END, "
            "confirm_code_hash = CASE WHEN ? = 1 THEN NULL ELSE confirm_code_hash END "
            "WHERE id = ? AND paid != ?",
            (int(paid), now if paid else None, amount if paid else None,
             int(paid), now, int(paid), now, int(paid), user_id, int(paid)),
        ).rowcount
        if changed and paid and mail.enabled():
            # seat confirmation with a personal link, so nobody needs an admin to get back in
            code = sec.new_secret()
            db.execute("UPDATE users SET access_code_hash = ? WHERE id = ?", (sec.sha256(code), user_id))
            notify.payment_confirmed(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone(),
                                     event, code)
    mail.flush_soon()
    if changed:
        sec.audit(actor(), "payment.confirmed" if paid else "payment.revoked", **user_summary(user))
    return jsonify(user=user_dict(get_user(user_id)))


@bp.post("/admin/users/<int:user_id>/move")
@admin_required
def admin_move(user_id):
    data = json_body()
    if "seat" not in data:
        raise ApiError(400, tr("'seat' is required (use null to free the seat)."))
    updated = assign_seat(user_id, data["seat"], admin=actor())
    mail.flush_soon()
    return jsonify(user=user_dict(updated))


@bp.post("/admin/users/<int:user_id>/login-link")
@admin_required
def admin_login_link(user_id):
    user = get_user(user_id)
    if user["event_id"] != active_event_id():
        raise ApiError(409, tr("Login links only work for participants of the active event."))
    with write_transaction():
        code, expires = create_login_code(user_id)
        sec.record_audit(actor(), "user.login_link_created", **user_summary(user))
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


def admin_settings_payload(s: dict, event_id: int) -> dict:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (JOBS_HEARTBEAT,)).fetchone()
    return public_settings(s) | {
        "event_id": event_id,
        "payment_reminders": s["payment_reminders"],
        "mail_enabled": mail.enabled(),
        "scheduler_last_run": row["value"] if row else None,
        "reminder_hours_before": conf("REMINDER_HOURS_BEFORE"),
    }


JOBS_HEARTBEAT = "_jobs_last_run"   # written by jobs.py on every run


# --- messages to participants (mail enabled) ------------------------------------------

AUDIENCES = {
    # people whose address is confirmed (or who registered before confirmation existed)
    "all": "1 = 1",
    "open": "seat IS NOT NULL AND paid = 0",
    "paid": "paid = 1",
}
REACHABLE = "(confirm_until IS NULL OR email_confirmed_at IS NOT NULL OR paid = 1)"


@bp.post("/admin/messages")
@admin_required
def admin_send_message():
    """Mail a message to the event's participants: everyone, open payments, or paid.
    ``dry_run`` only counts the recipients."""
    if not mail.enabled():
        raise ApiError(409, tr("Sending mails is not configured on this server."))
    data = json_body()
    event_id = admin_event_id()
    audience = data.get("audience")
    if audience not in AUDIENCES:
        raise ApiError(400, tr("'audience' must be one of: all, open, paid."))
    sql = f"SELECT * FROM users WHERE event_id = ? AND {REACHABLE} AND {AUDIENCES[audience]} ORDER BY id"
    if data.get("dry_run") is True:
        return jsonify(recipients=len(get_db().execute(sql, (event_id,)).fetchall()))
    subject = " ".join(str(data.get("subject") or "").split())
    body = str(data.get("body") or "").strip()
    if not 1 <= len(subject) <= 150:
        raise ApiError(400, tr("The subject must be between 1 and 150 characters."))
    if not 1 <= len(body) <= 5000:
        raise ApiError(400, tr("The message must be between 1 and 5000 characters."))
    with write_transaction() as db:
        event = cfg.load(event_id)
        rows = db.execute(sql, (event_id,)).fetchall()
        for row in rows:
            notify.message(row, event, subject, body)
        sec.record_audit(actor(), "message.sent", event_id=event_id, audience=audience, subject=subject,
                         recipients=len(rows))
    mail.flush_soon()
    return jsonify(recipients=len(rows))


@bp.get("/admin/settings")
@admin_required
def admin_get_settings():
    event_id = admin_event_id()
    return jsonify(admin_settings_payload(event_settings(event_id), event_id))


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
    return jsonify(admin_settings_payload(after, event_id))


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
