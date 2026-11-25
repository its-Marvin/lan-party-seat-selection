"""Event / payment / layout settings stored as key-value pairs."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from .db import get_db

DEFAULTS = {
    "event_name": "LAN Party",
    "event_start": "",
    "event_end": "",
    "location": "",
    "description": "",
    "paypal_address": "",
    "amount_cents": "2000",
    "reservation_hours": "48",
    "event_code": "",
}

EVENT_CODE_RE = re.compile(r"[A-Z0-9]{4,6}")


def normalize_event_code(value) -> str:
    return str(value or "").strip().upper()

MAX_RESERVATION_HOURS = 24 * 60

TEXT_LIMITS = {
    "event_name": 120,
    "location": 200,
    "description": 5000,
    "paypal_address": 200,
}


class SettingsError(ValueError):
    pass


def load() -> dict:
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    raw = {k: stored.get(k, v) for k, v in DEFAULTS.items()}
    return {
        "event_name": raw["event_name"],
        "event_start": raw["event_start"],
        "event_end": raw["event_end"],
        "location": raw["location"],
        "description": raw["description"],
        "paypal_address": raw["paypal_address"],
        "amount_cents": int(raw["amount_cents"]),
        "reservation_hours": int(raw["reservation_hours"]),
        "event_code": raw["event_code"],
    }


def parse_amount(value) -> int:
    """Parse '20', '20.5', '20,50' or 20.5 into cents."""
    if isinstance(value, bool) or value is None:
        raise SettingsError("Amount is required.")
    text = str(value).strip().replace("€", "").replace(",", ".").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise SettingsError("Amount must be a number.") from None
    if not amount.is_finite() or amount < 0 or amount > 10000:
        raise SettingsError("Amount must be between 0 and 10000.")
    if amount != amount.quantize(Decimal("0.01")):
        raise SettingsError("Amount can have at most two decimal places.")
    return int(amount * 100)


def _parse_datetime(value, name) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SettingsError(f"{name} must be an ISO date/time, e.g. 2026-12-12T14:00.") from None
    if parsed.tzinfo is not None:
        raise SettingsError(f"{name} must be a local date/time without a timezone, e.g. 2026-12-12T14:00.")
    return text


def validate_update(data: dict) -> dict:
    """Validate a partial update; returns the values to store (as strings)."""
    if not isinstance(data, dict):
        raise SettingsError("Expected a JSON object.")
    out = {}
    for key, limit in TEXT_LIMITS.items():
        if key in data:
            text = str(data[key] or "").strip()
            if len(text) > limit:
                raise SettingsError(f"{key} must be at most {limit} characters.")
            out[key] = text
    if "event_name" in out and not out["event_name"]:
        raise SettingsError("Event name must not be empty.")
    if "paypal_address" in out and out["paypal_address"] and not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+|(https?://)?(www\.)?paypal\.me/\S+", out["paypal_address"]
    ):
        raise SettingsError("PayPal address must be an email address or a paypal.me link.")
    for key, label in (("event_start", "Start"), ("event_end", "End")):
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
    if "event_code" in data:
        code = normalize_event_code(data["event_code"])
        if code and not EVENT_CODE_RE.fullmatch(code):
            raise SettingsError("The event code must be 4–6 letters or digits (or empty for no code).")
        out["event_code"] = code

    current = load()
    start = out.get("event_start", current["event_start"])
    end = out.get("event_end", current["event_end"])
    if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
        raise SettingsError("End must not be before start.")
    return out


def save(values: dict) -> None:
    """Store validated values. Call inside ``db.write_transaction()`` together
    with the ``validate_update()`` that produced them; this does not commit."""
    db = get_db()
    db.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )
