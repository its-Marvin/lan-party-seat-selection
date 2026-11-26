"""Events: one active at a time, earlier ones archived, later ones upcoming.

An event's status follows from its dates (local time in ``TIMEZONE``):

* ``draft``     – no "registration opens" date yet; never visible publicly
* ``upcoming``  – registration opens in the future
* ``active``    – registration has opened and the event hasn't ended
* ``archived``  – the event has ended; participants and layout are read-only

Registration windows (``registration_opens`` up to ``event_end``) may not
overlap, so at most one event is active. Event settings and the layout are
stored per event in ``event_settings``; participants belong to one event.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g, has_app_context

from . import settings as cfg
from .db import get_db

STATUSES = ("draft", "upcoming", "active", "archived")
# what a new event copies from the one it is based on (dates and code are per event)
COPIED_KEYS = ("event_name", "location", "description", "paypal_address", "amount_cents",
               "reservation_hours", "layout")


class EventError(ValueError):
    pass


def timezone_name() -> str:
    return current_app.config.get("TIMEZONE", "") if has_app_context() else ""


def local_now() -> datetime:
    """Current wall-clock time where the event takes place (naive, like stored dates)."""
    name = timezone_name()
    if name:
        try:
            return datetime.now(ZoneInfo(name)).replace(tzinfo=None, microsecond=0)
        except ZoneInfoNotFoundError:
            raise RuntimeError(f"Unknown TIMEZONE {name!r} (use an IANA name like Europe/Berlin).") from None
    return datetime.now().replace(microsecond=0)


def _parse(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def status(s: dict, now: datetime | None = None) -> str:
    now = now or local_now()
    opens, end = _parse(s["registration_opens"]), _parse(s["event_end"])
    if opens is None:
        return "draft"
    if now < opens:
        return "upcoming"
    if end is not None and now >= end:
        return "archived"
    return "active"


def load_all() -> list[dict]:
    """Every event's settings plus ``id``, ``created_at`` and ``status``, oldest first."""
    db = get_db()
    events = db.execute("SELECT id, created_at FROM events ORDER BY id").fetchall()
    rows = db.execute("SELECT event_id, key, value FROM event_settings").fetchall()
    by_event: dict[int, list] = {}
    for r in rows:
        by_event.setdefault(r["event_id"], []).append(r)
    now = local_now()
    result = []
    for e in events:
        s = cfg.from_rows(by_event.get(e["id"], []))
        s.update(id=e["id"], created_at=e["created_at"], status=status(s, now))
        result.append(s)
    return result


def active_id() -> int | None:
    """The event that is open right now (cached per request)."""
    if "active_event_id" not in g:
        active = [e for e in load_all() if e["status"] == "active"]
        # windows can't overlap; should it happen anyway, the latest opening wins
        active.sort(key=lambda e: _parse(e["registration_opens"]))
        g.active_event_id = active[-1]["id"] if active else None
    return g.active_event_id


def default_admin_id() -> int:
    """The event the admin page starts with: active, else next upcoming, else newest."""
    events = load_all()
    for wanted in ("active", "upcoming"):
        matches = [e for e in events if e["status"] == wanted]
        if matches:
            return min(matches, key=lambda e: _parse(e["registration_opens"]))["id"]
    return events[-1]["id"]


def upcoming() -> list[dict]:
    events = [e for e in load_all() if e["status"] == "upcoming"]
    return sorted(events, key=lambda e: _parse(e["registration_opens"]))


def get(event_id: int) -> dict | None:
    return next((e for e in load_all() if e["id"] == event_id), None)


def _readable(value: str) -> str:
    return _parse(value).strftime("%Y-%m-%d %H:%M")


def describe_window(e: dict) -> str:
    end = _readable(e["event_end"]) if e["event_end"] else "never (no end set)"
    return f"registration opens {_readable(e['registration_opens'])}, ends {end}"


def overlapping(event_id: int, opens: str, end: str) -> dict | None:
    """Another scheduled event whose registration window overlaps ``opens``…``end``."""
    start, stop = _parse(opens), _parse(end) or datetime.max
    for e in load_all():
        if e["id"] == event_id or not e["registration_opens"]:
            continue
        other_start = _parse(e["registration_opens"])
        other_stop = _parse(e["event_end"]) or datetime.max
        if start < other_stop and other_start < stop:
            return e
    return None


def create(copy_from: int | None) -> int:
    """Create a draft event, copying settings and layout from ``copy_from``.

    Call inside ``db.write_transaction()``.
    """
    db = get_db()
    if copy_from is not None and db.execute("SELECT 1 FROM events WHERE id = ?", (copy_from,)).fetchone() is None:
        raise EventError("The event to copy from does not exist.")
    event_id = db.execute("INSERT INTO events DEFAULT VALUES").lastrowid
    if copy_from is not None:
        marks = ",".join("?" * len(COPIED_KEYS))
        db.execute(
            f"INSERT INTO event_settings (event_id, key, value) "
            f"SELECT ?, key, value FROM event_settings WHERE event_id = ? AND key IN ({marks})",
            (event_id, copy_from, *COPIED_KEYS),
        )
    return event_id


def delete(event_id: int) -> dict:
    """Delete an event with its participants; the active event can't be deleted.

    Call inside ``db.write_transaction()``. Returns the deleted event.
    """
    event = get(event_id)
    if event is None:
        raise EventError("Event not found.")
    if event["status"] == "active":
        raise EventError("The active event can't be deleted. Change its dates first.")
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM events").fetchone()[0] <= 1:
        raise EventError("The only event can't be deleted. Create another one first.")
    # users cascade to their tokens and login codes; settings cascade with the event
    event["participants"] = db.execute(
        "SELECT COUNT(*) FROM users WHERE event_id = ?", (event_id,)).fetchone()[0]
    db.execute("DELETE FROM users WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return event
