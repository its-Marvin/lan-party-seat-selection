"""Per-event settings (event, payment, access) stored as key-value pairs in event_settings."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from .db import get_db
from .i18n import N_, tr

DEFAULTS = {
    "event_name": "LAN Party",
    "registration_opens": "",   # empty = draft: never opens until a date is set
    "event_start": "",
    "event_end": "",
    "location": "",
    "description": "",
    "paypal_address": "",
    "amount_cents": "2000",
    "reservation_hours": "48",
    "event_code": "",
    "theme": "default",         # folder name of the theme (see themes.py)
}

EVENT_CODE_RE = re.compile(r"[A-Z0-9]{4,6}")


def normalize_event_code(value) -> str:
    return str(value or "").strip().upper()

MAX_RESERVATION_HOURS = 24 * 60

# field names in messages (translated with tr())
TEXT_LABELS = {"event_name": N_("Event name"), "location": N_("Location"), "description": N_("Description"),
               "paypal_address": N_("PayPal address")}

TEXT_LIMITS = {
    "event_name": 120,
    "location": 200,
    "description": 5000,
    "paypal_address": 200,
}


class SettingsError(ValueError):
    pass


DATE_KEYS = (("registration_opens", N_("Registration opens")), ("event_start", N_("Start")), ("event_end", N_("End")))


def from_rows(rows) -> dict:
    stored = {r["key"]: r["value"] for r in rows}
    raw = {k: stored.get(k, v) for k, v in DEFAULTS.items()}
    return {
        "event_name": raw["event_name"],
        "registration_opens": raw["registration_opens"],
        "event_start": raw["event_start"],
        "event_end": raw["event_end"],
        "location": raw["location"],
        "description": raw["description"],
        "paypal_address": raw["paypal_address"],
        "amount_cents": int(raw["amount_cents"]),
        "reservation_hours": int(raw["reservation_hours"]),
        "event_code": raw["event_code"],
        "theme": raw["theme"],
    }


def load(event_id: int) -> dict:
    rows = get_db().execute(
        "SELECT key, value FROM event_settings WHERE event_id = ?", (event_id,)
    ).fetchall()
    return from_rows(rows)


def parse_amount(value) -> int:
    """Parse '20', '20.5', '20,50' or 20.5 into cents."""
    if isinstance(value, bool) or value is None:
        raise SettingsError(tr("Amount is required."))
    text = str(value).strip().replace("€", "").replace(",", ".").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise SettingsError(tr("Amount must be a number.")) from None
    if not amount.is_finite() or amount < 0 or amount > 10000:
        raise SettingsError(tr("Amount must be between 0 and 10000."))
    if amount != amount.quantize(Decimal("0.01")):
        raise SettingsError(tr("Amount can have at most two decimal places."))
    return int(amount * 100)


def _parse_datetime(value, name) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SettingsError(tr("{name} must be an ISO date/time, e.g. 2026-12-12T14:00.", name=tr(name))) from None
    if parsed.tzinfo is not None:
        raise SettingsError(tr("{name} must be a local date/time without a timezone, e.g. 2026-12-12T14:00.", name=tr(name)))
    return text


def validate_update(data: dict, event_id: int) -> dict:
    """Validate a partial update; returns the values to store (as strings).

    Reads the event's stored values and the other events' dates, so call it
    inside the same ``db.write_transaction()`` as ``save()``.
    """
    if not isinstance(data, dict):
        raise SettingsError(tr("Expected a JSON object."))
    out = {}
    for key, limit in TEXT_LIMITS.items():
        if key in data:
            text = str(data[key] or "").strip()
            if len(text) > limit:
                raise SettingsError(tr("{field} must be at most {limit} characters.", field=tr(TEXT_LABELS[key]), limit=limit))
            out[key] = text
    if "event_name" in out and not out["event_name"]:
        raise SettingsError(tr("Event name must not be empty."))
    if "paypal_address" in out and out["paypal_address"] and not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+|(https?://)?(www\.)?paypal\.me/\S+", out["paypal_address"]
    ):
        raise SettingsError(tr("PayPal address must be an email address or a paypal.me link."))
    for key, label in DATE_KEYS:
        if key in data:
            out[key] = _parse_datetime(data[key], label)
    if "amount" in data:
        out["amount_cents"] = str(parse_amount(data["amount"]))
    if "reservation_hours" in data:
        hours = data["reservation_hours"]
        if isinstance(hours, str) and hours.strip().isdigit():
            hours = int(hours)
        if isinstance(hours, bool) or not isinstance(hours, int) or not 0 <= hours <= MAX_RESERVATION_HOURS:
            raise SettingsError(
                f"Reservation time must be a whole number of hours between 0 and {MAX_RESERVATION_HOURS}."
            )
        out["reservation_hours"] = str(hours)
    if "theme" in data:
        from . import themes

        theme = themes.get(data["theme"])
        if theme is not None:
            out["theme"] = theme.id
        elif data["theme"] != load(event_id)["theme"]:
            raise SettingsError(tr("This theme does not exist (or has a problem, see the theme list)."))
        # else: the stored theme was removed or broke later. Saving other changes
        # keeps it (the public page falls back to the default meanwhile).
    if "event_code" in data:
        code = normalize_event_code(data["event_code"])
        if code and not EVENT_CODE_RE.fullmatch(code):
            raise SettingsError(tr("The event code must be 4–6 letters or digits (or empty for no code)."))
        out["event_code"] = code

    current = load(event_id)
    opens = out.get("registration_opens", current["registration_opens"])
    start = out.get("event_start", current["event_start"])
    end = out.get("event_end", current["event_end"])
    if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
        raise SettingsError(tr("End must not be before start."))
    if opens and end and datetime.fromisoformat(end) <= datetime.fromisoformat(opens):
        raise SettingsError(tr("Registration must open before the event ends."))
    if opens:
        from . import events

        clash = events.overlapping(event_id, opens, end)
        if clash is not None:
            raise SettingsError(tr(
                "Registration can only open after the previous event has ended: this overlaps "
                "with \"{event}\" ({window}).",
                event=clash["event_name"], window=events.describe_window(clash),
            ))
    return out


def save(values: dict, event_id: int) -> None:
    """Store validated values. Call inside ``db.write_transaction()`` together
    with the ``validate_update()`` that produced them; this does not commit."""
    db = get_db()
    db.executemany(
        "INSERT INTO event_settings (event_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(event_id, key) DO UPDATE SET value = excluded.value",
        [(event_id, k, v) for k, v in values.items()],
    )
