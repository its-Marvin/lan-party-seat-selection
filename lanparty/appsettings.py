"""App-wide settings that admins change on /admin/settings, /admin/mail and
/admin/security (per-event settings are in settings.py).

Values live in the global ``settings`` table under ``cfg.<key>``. Settings that
existed as environment variables before keep them as defaults: a value saved in
the admin area wins, an unset one falls back to ``app.config`` (and so to the
environment). Mail is configured only here.
"""

from dataclasses import dataclass
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g, has_app_context

from .db import get_db
from .i18n import N_, tr

PREFIX = "cfg."


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    section: str                 # general | mail | security
    kind: str                    # int | bool | text | choice | secret
    config: str | None = None    # app.config key that provides the default
    default: object = None
    minimum: int = 0
    maximum: int = 0
    max_length: int = 200
    choices: tuple = ()
    label: str = ""              # for messages (translated with tr)

    def public(self, value) -> dict:
        data = {"key": self.key, "type": self.kind}
        if self.kind == "secret":
            data["set"] = bool(value)            # write-only: never sent back
        else:
            data["value"] = value
        if self.kind == "int":
            data.update(min=self.minimum, max=self.maximum)
        if self.kind == "choice":
            data["choices"] = list(self.choices)
        if self.kind in ("text", "secret"):
            data["max_length"] = self.max_length
        return data


FIELDS = {f.key: f for f in (
    # general
    Field("public_url", "general", "text", config="PUBLIC_URL", default="", max_length=200,
          label=N_("Site address")),
    Field("timezone", "general", "text", config="TIMEZONE", default="", max_length=64, label=N_("Time zone")),
    Field("login_code_hours", "general", "int", config="LOGIN_CODE_HOURS", default=168, minimum=1,
          maximum=24 * 60, label=N_("Login link validity")),
    # mail
    Field("mail_enabled", "mail", "bool", default=False, label=N_("Send mails")),
    Field("smtp_host", "mail", "text", default="", max_length=200, label=N_("SMTP server")),
    Field("smtp_port", "mail", "int", default=0, minimum=0, maximum=65535, label=N_("SMTP port")),
    Field("smtp_security", "mail", "choice", default="starttls", choices=("starttls", "ssl", "none"),
          label=N_("Encryption")),
    Field("smtp_username", "mail", "text", default="", max_length=200, label=N_("SMTP user name")),
    Field("smtp_password", "mail", "secret", default="", max_length=500, label=N_("SMTP password")),
    Field("mail_from", "mail", "text", default="", max_length=200, label=N_("Sender address")),
    Field("email_confirm_minutes", "mail", "int", config="EMAIL_CONFIRM_MINUTES", default=120, minimum=10,
          maximum=24 * 60, label=N_("Time to confirm the email address")),
    Field("reminder_hours_before", "mail", "int", config="REMINDER_HOURS_BEFORE", default=48, minimum=1,
          maximum=24 * 14, label=N_("Payment reminder before the deadline")),
    # security
    Field("admin_session_idle_minutes", "security", "int", config="ADMIN_SESSION_IDLE_MINUTES", default=60,
          minimum=5, maximum=24 * 60, label=N_("Admin session idle timeout")),
    Field("admin_session_max_hours", "security", "int", config="ADMIN_SESSION_MAX_HOURS", default=12,
          minimum=1, maximum=24 * 7, label=N_("Admin session maximum length")),
    Field("admin_login_max_per_ip", "security", "int", config="ADMIN_LOGIN_MAX_PER_IP", default=5,
          minimum=1, maximum=100, label=N_("Failed admin logins per network")),
    Field("admin_login_max_global", "security", "int", config="ADMIN_LOGIN_MAX_GLOBAL", default=50,
          minimum=1, maximum=10000, label=N_("Failed admin logins in total")),
    Field("admin_login_window_minutes", "security", "int", config="ADMIN_LOGIN_WINDOW_MINUTES", default=15,
          minimum=1, maximum=24 * 60, label=N_("Admin login lockout window")),
    Field("register_max_per_ip", "security", "int", config="REGISTER_MAX_PER_IP", default=10, minimum=1,
          maximum=10000, label=N_("Registrations per network and hour")),
    Field("max_pending_per_ip", "security", "int", config="MAX_PENDING_PER_IP", default=4, minimum=1,
          maximum=10000, label=N_("Unpaid reservations per network")),
    Field("login_code_max_per_ip", "security", "int", config="LOGIN_CODE_MAX_PER_IP", default=20, minimum=1,
          maximum=10000, label=N_("Login link attempts per network")),
    Field("event_code_max_per_ip", "security", "int", config="EVENT_CODE_MAX_PER_IP", default=10, minimum=1,
          maximum=10000, label=N_("Wrong event codes per network")),
    Field("audit_log_retention_days", "security", "int", config="AUDIT_LOG_RETENTION_DAYS", default=365,
          minimum=0, maximum=3650, label=N_("Keep audit log entries")),
)}
SECTIONS = ("general", "mail", "security")
BY_CONFIG = {f.config: f.key for f in FIELDS.values() if f.config}


def _stored() -> dict:
    cache = g.get("app_settings") if has_app_context() else None
    if cache is None:
        rows = get_db().execute("SELECT key, value FROM settings WHERE key LIKE ?", (PREFIX + "%",)).fetchall()
        cache = {r["key"][len(PREFIX):]: r["value"] for r in rows}
        if has_app_context():
            g.app_settings = cache
    return cache


def _parse(field: Field, text: str):
    if field.kind == "int":
        return int(text)
    if field.kind == "bool":
        return text == "1"
    return text


def _default(field: Field):
    if field.config and field.config in current_app.config:
        return current_app.config[field.config]
    return field.default


def value(key: str):
    """The effective value: saved in the admin area, else the configured default."""
    field = FIELDS[key]
    stored = _stored().get(key)
    return _parse(field, stored) if stored is not None else _default(field)


def config_value(config_key: str):
    """``app.config[config_key]``, unless an admin saved a value for it."""
    key = BY_CONFIG.get(config_key)
    return value(key) if key else current_app.config[config_key]


def section(name: str) -> list[dict]:
    return [f.public(value(f.key)) for f in FIELDS.values() if f.section == name]


def clear_cache() -> None:
    if has_app_context():
        g.pop("app_settings", None)


# --- validation -----------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s/?#]+(/[^\s?#]*)?")
EMAIL_RE = re.compile(r"[^@\s<>\"(),;:]+@[^@\s<>\"(),;:]+\.[^@\s<>\"(),;:]+")
HOST_RE = re.compile(r"[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]")


def _validate_field(field: Field, raw) -> str:
    name = tr(field.label)
    if field.kind == "bool":
        if not isinstance(raw, bool):
            raise ConfigError(tr("{field} must be true or false.", field=name))
        return "1" if raw else "0"
    if field.kind == "int":
        if isinstance(raw, str) and raw.strip().isdigit():
            raw = int(raw)
        if isinstance(raw, bool) or not isinstance(raw, int) or not field.minimum <= raw <= field.maximum:
            raise ConfigError(tr("{field} must be a whole number between {min} and {max}.", field=name,
                                 min=field.minimum, max=field.maximum))
        return str(raw)
    text = str(raw if raw is not None else "").strip()
    if len(text) > field.max_length:
        raise ConfigError(tr("{field} must be at most {limit} characters.", field=name, limit=field.max_length))
    if field.kind == "choice" and text not in field.choices:
        raise ConfigError(tr("{field} must be one of: {choices}.", field=name, choices=", ".join(field.choices)))
    if field.key == "public_url" and text:
        text = text.rstrip("/")
        if not URL_RE.fullmatch(text):
            raise ConfigError(tr("The site address must start with https:// or http://, e.g. https://lan.example.com."))
    if field.key == "timezone" and text:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(tr("Unknown time zone {zone} (use a name like Europe/Berlin).", zone=text)) from None
    if field.key == "mail_from" and text and not EMAIL_RE.fullmatch(text):
        raise ConfigError(tr("The sender address must be an email address."))
    if field.key == "smtp_host" and text and not HOST_RE.fullmatch(text):
        raise ConfigError(tr("The SMTP server must be a host name or an IP address."))
    return text


def validate_update(name: str, data: dict) -> dict:
    """Values to store for section ``name`` (as text). Keys of other sections are
    refused; an empty secret means "keep the current one"."""
    if name not in SECTIONS or not isinstance(data, dict):
        raise ConfigError(tr("Expected a JSON object."))
    out = {}
    for key, raw in data.items():
        secret_of = FIELDS.get(str(key).removesuffix("_clear"))
        if str(key).endswith("_clear") and secret_of is not None and secret_of.kind == "secret":
            if raw is True and secret_of.section == name:
                out[secret_of.key] = ""        # "<secret>_clear": true removes a stored secret
            continue
        field = FIELDS.get(key)
        if field is None or field.section != name:
            raise ConfigError(tr("Unknown setting: {key}.", key=str(key)[:40]))
        if field.kind == "secret" and raw in ("", None):
            continue                           # empty = keep the current secret
        if field.kind == "secret" and data.get(f"{key}_clear") is True:
            continue
        out[key] = _validate_field(field, raw)
    if name == "mail":
        merged = {k: (out[k] if k in out else _stored().get(k)) for k in FIELDS if FIELDS[k].section == "mail"}
        enabled = (merged.get("mail_enabled") or ("1" if value("mail_enabled") else "0")) == "1"
        if enabled:
            if not (merged.get("smtp_host") or current_app.config.get("MAIL_BACKEND")):
                raise ConfigError(tr("To send mails, enter the SMTP server."))
            if not merged.get("mail_from"):
                raise ConfigError(tr("To send mails, enter the sender address."))
            if not value("public_url"):
                raise ConfigError(tr("To send mails, first enter the site address under Settings: "
                                     "the links in the mails point there."))
    return out


def save(values: dict) -> dict:
    """Store validated values; returns ``{key: [old, new]}`` for the audit log
    (secrets as "(changed)"). Call inside ``db.write_transaction()``."""
    db = get_db()
    changes = {}
    for key, text in values.items():
        field = FIELDS[key]
        old = value(key)
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (PREFIX + key, text))
        new = _parse(field, text)
        if old != new:
            changes[key] = "(changed)" if field.kind == "secret" else [old, new]
    clear_cache()
    return changes
