"""Optional email: configuration, a database outbox, delivery, and the HTML frame
in the colours of the event's theme.

Mail is configured on /admin/mail (stored via appsettings) and is off until an
admin enables it; the app works the same as before without it. ``MAIL_BACKEND``
(environment) can replace SMTP for development: ``console`` logs mails instead. Mails are written to ``mail_outbox`` inside the same
transaction as the change they report, then sent right after the request (in a
background thread) and retried by the scheduler (``python -m lanparty.jobs``).

The wording of each mail lives in ``notify.py``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import html
import logging
from pathlib import Path
import re
import smtplib
import ssl
import threading
import time
from zoneinfo import ZoneInfo

from flask import current_app

from . import appsettings
from .db import get_db, utc_now

log = logging.getLogger(__name__)

BACKENDS = ("", "console", "memory")   # instead of SMTP; memory: tests keep sent mails in a list
MAX_ATTEMPTS = 8
RETRY_SECONDS = 300          # a failed (or abandoned) mail is tried again after this
KEEP_DAYS = 30               # sent mails hold personal data: deleted after this


# --- configuration ------------------------------------------------------------------

def configure(app) -> None:
    """Check the development override at startup (SMTP itself is set on /admin/mail)."""
    if app.config["MAIL_BACKEND"] not in BACKENDS or app.config["MAIL_BACKEND"] == "smtp":
        raise RuntimeError("MAIL_BACKEND may only be console (log mails instead of sending them).")


def problems() -> list[str]:
    """What keeps mail from working, as setting keys (empty = ready)."""
    missing = []
    if not (appsettings.value("smtp_host") or current_app.config["MAIL_BACKEND"]):
        missing.append("smtp_host")
    if not appsettings.value("mail_from"):
        missing.append("mail_from")
    if not appsettings.value("public_url"):
        missing.append("public_url")
    return missing


def enabled() -> bool:
    """An admin switched mail on and everything needed to send is there."""
    return bool(appsettings.value("mail_enabled")) and not problems()


def link(fragment: str = "") -> str:
    """An absolute link into the public page (codes go in the #fragment)."""
    return appsettings.value("public_url").rstrip("/") + "/" + (f"#{fragment}" if fragment else "")


# --- composing ------------------------------------------------------------------------

@dataclass
class Mail:
    """A mail as a list of blocks, rendered both as HTML and as plain text."""
    lang: str
    subject: str
    blocks: list = field(default_factory=list)

    def p(self, text: str):
        self.blocks.append(("p", text))
        return self

    def facts(self, rows: list[tuple[str, str]]):
        self.blocks.append(("facts", [(k, v) for k, v in rows if v]))
        return self

    def button(self, label: str, url: str, fallback: str):
        self.blocks.append(("button", label, url, fallback))
        return self

    def note(self, text: str):
        self.blocks.append(("note", text))
        return self


def _text(mail: Mail, header: str, footer: str) -> str:
    out = [header, ""]
    for block in mail.blocks:
        kind = block[0]
        if kind in ("p", "note"):
            out += [block[1], ""]
        elif kind == "facts":
            width = max((len(k) for k, _ in block[1]), default=0)
            out += [f"{k + ':':<{width + 1}} {v}" for k, v in block[1]] + [""]
        elif kind == "button":
            out += [f"{block[1]}:", block[2], ""]
    out += ["-- ", footer]
    return "\n".join(out)


def _html(mail: Mail, title: str, subtitle: str, footer: str, colors: dict) -> str:
    c = {k: html.escape(v, quote=True) for k, v in colors.items()}
    radius = colors.get("radius", "8")
    esc = lambda text: html.escape(text).replace("\n", "<br>")   # noqa: E731
    parts = []
    for block in mail.blocks:
        kind = block[0]
        if kind == "p":
            parts.append(f'<p style="margin:0 0 16px">{esc(block[1])}</p>')
        elif kind == "note":
            parts.append(f'<p style="margin:0 0 16px;color:{c["muted"]};font-size:13px">{esc(block[1])}</p>')
        elif kind == "facts":
            rows = "".join(
                f'<tr><td style="padding:4px 12px 4px 0;color:{c["muted"]};vertical-align:top;width:35%">'
                f'{esc(k)}</td><td style="padding:4px 0;font-weight:600;overflow-wrap:anywhere;word-break:break-word">'
                f'{esc(v)}</td></tr>'
                for k, v in block[1])
            parts.append(f'<table role="presentation" cellpadding="0" cellspacing="0" '
                         f'style="margin:0 0 16px;border-collapse:collapse;font-size:15px;width:100%;table-layout:fixed">{rows}</table>')
        elif kind == "button":
            _, label, url, fallback = block
            href = html.escape(url, quote=True)
            parts.append(
                f'<p style="margin:8px 0 16px"><a href="{href}" style="display:inline-block;'
                f'background:{c["accent"]};color:{c["on-accent"]};padding:12px 22px;border-radius:{radius}px;'
                f'text-decoration:none;font-weight:600">{esc(label)}</a></p>'
                f'<p style="margin:0 0 16px;color:{c["muted"]};font-size:12px">'
                f'{esc(fallback)}<br><a href="{href}" style="color:{c["accent"]};word-break:break-all">'
                f'{html.escape(url)}</a></p>')
    return (
        f'<!doctype html><html lang="{html.escape(mail.lang[:2])}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width"><title>{html.escape(mail.subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:{c["bg"]}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{c["bg"]}">'
        f'<tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;'
        f'background:{c["card"]};border:1px solid {c["border"]};border-radius:{radius}px;'
        f'font-family:{c["font"]};color:{c["text"]};font-size:15px;line-height:1.5">'
        f'<tr><td style="padding:20px 24px;background:{c["hero-from"]};'
        f'background-image:linear-gradient(135deg,{c["hero-from"]},{c["hero-to"]});'
        f'border-radius:{radius}px {radius}px 0 0">'
        f'<div style="font-size:20px;font-weight:700;color:{c["hero-title"]}">{html.escape(title)}</div>'
        + (f'<div style="font-size:13px;color:{c["hero-text"]};margin-top:4px">{html.escape(subtitle)}</div>'
           if subtitle else "")
        + f'</td></tr><tr><td style="padding:24px">{"".join(parts)}</td></tr>'
        f'<tr><td style="padding:14px 24px;border-top:1px solid {c["border"]};color:{c["muted"]};font-size:12px">'
        f'{esc(footer)}</td></tr></table></td></tr></table></body></html>'
    )


# --- theme colours ------------------------------------------------------------------

STATIC_CSS = Path(__file__).parent / "static" / "style.css"
_TOKEN_RE = re.compile(r"--([\w-]+)\s*:\s*([^;}]+);")
_SAFE_VALUE = re.compile(r"[#\w\s,.%()\"'+-]+")


def _blocks(css: str, selector: str) -> dict:
    """Custom properties of every ``selector { … }`` block in ``css``."""
    tokens = {}
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    for body in re.findall(pattern, re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        for name, value in _TOKEN_RE.findall(body):
            value = value.strip()
            # only plain values: no var(), url() or anything that could break out of a style attribute
            if _SAFE_VALUE.fullmatch(value) and "var(" not in value and "url(" not in value.lower():
                tokens[name] = value
    return tokens


def palette(theme_id: str) -> dict:
    """The light-mode colours and fonts of a theme (mail clients get no CSS variables,
    and light is what most of them show). Falls back to the default theme."""
    from . import themes

    base = STATIC_CSS.read_text(encoding="utf-8")
    tokens = _blocks(base, ":root") | _blocks(base, ':root[data-mode="light"]')
    theme = themes.get(theme_id) or themes.get(themes.DEFAULT_THEME)
    css_file = Path(theme.path) / "theme.css" if theme is not None else None
    if css_file is not None and css_file.is_file():
        css = css_file.read_text(encoding="utf-8")
        tokens |= _blocks(css, ":root") | _blocks(css, ':root[data-mode="light"]')
    try:
        radius = str(round(8 * float(tokens.get("radius-scale", "1"))))
    except ValueError:
        radius = "8"
    get = lambda name, default: tokens.get(name, default)   # noqa: E731
    return {
        "bg": get("bg", "#f3f4f9"), "card": get("card", "#ffffff"), "border": get("border", "#d3d8e8"),
        "text": get("text", "#1a1d2e"), "muted": get("muted", "#555c78"),
        "accent": get("accent", "#2f5fd6"), "on-accent": get("on-accent", "#ffffff"),
        "hero-from": get("hero-from", get("card", "#ffffff")), "hero-to": get("hero-to", get("card", "#ffffff")),
        "hero-title": get("hero-title", get("text", "#1a1d2e")), "hero-text": get("hero-text", get("muted", "#555c78")),
        "font": get("font-body", "system-ui, sans-serif"), "radius": radius,
    }


# --- dates ------------------------------------------------------------------------------

WEEKDAYS_DE = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")


def format_local(value: datetime, lang: str) -> str:
    """A local (event time zone) date and time, written the way the web page does."""
    if lang == "de-DE":
        return f"{WEEKDAYS_DE[value.weekday()]}, {value:%d.%m.%Y, %H:%M} Uhr"
    hour = value.hour % 12 or 12
    return f"{value:%a, %b} {value.day}, {value.year}, {hour}:{value:%M} {'AM' if value.hour < 12 else 'PM'}"


def utc_to_local(text: str, plus: timedelta = timedelta()) -> datetime:
    """A stored UTC timestamp (``utc_now()`` format), plus ``plus``, in the event's
    time zone (the one saved on /admin/settings, like the event clock). ``plus`` is
    added in UTC, so a window across a DST change ends when the app expires it."""
    from .events import timezone_name

    moment = datetime.fromisoformat(text).replace(tzinfo=timezone.utc) + plus
    name = timezone_name()
    return (moment.astimezone(ZoneInfo(name)) if name else moment.astimezone()).replace(tzinfo=None)


# --- outbox ---------------------------------------------------------------------------

def enqueue(user, mail: Mail, kind: str, *, event: dict) -> int:
    """Queue ``mail`` for ``user`` (a users row). Call inside the write transaction
    that makes the change the mail reports; it is sent after the commit."""
    from .i18n import tr_in

    when = ""
    if event.get("event_start"):
        when = format_local(datetime.fromisoformat(event["event_start"]), mail.lang)
    subtitle = " · ".join(part for part in (when, event.get("location", "")) if part)
    footer = tr_in(mail.lang, "You get this mail because you registered for {event} at {url}.",
                   event=event["event_name"], url=appsettings.value("public_url"))
    colors = palette(event.get("theme", "default"))
    return get_db().insert(
        "INSERT INTO mail_outbox (user_id, recipient, kind, subject, body_text, body_html) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], user["email"], kind, _one_line(mail.subject),
         _text(mail, event["event_name"], footer),
         _html(mail, event["event_name"], subtitle, footer, colors)),
    )


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:200]


def deliver_pending(limit: int = 100) -> int:
    """Send queued mails; returns how many were sent. Safe to run in several
    processes at once: each mail is claimed before it is sent."""
    if not enabled():
        return 0
    db = get_db()
    now = time.time()
    rows = db.execute(
        "SELECT id FROM mail_outbox WHERE sent_at IS NULL AND attempts < ? AND kind <> 'test' "
        "AND (claimed_at IS NULL OR claimed_at < ?) ORDER BY id LIMIT ?",
        (MAX_ATTEMPTS, now - RETRY_SECONDS, limit)).fetchall()
    db.commit()
    sent = 0
    for row in rows:
        claimed = db.execute(
            "UPDATE mail_outbox SET claimed_at = ?, attempts = attempts + 1 "
            "WHERE id = ? AND sent_at IS NULL AND (claimed_at IS NULL OR claimed_at < ?)",
            (time.time(), row["id"], time.time() - RETRY_SECONDS)).rowcount
        db.commit()   # never hold the write lock while talking to the mail server
        if not claimed:
            continue
        mail = db.execute("SELECT * FROM mail_outbox WHERE id = ?", (row["id"],)).fetchone()
        db.commit()
        if mail is None:
            continue
        try:
            _send(mail)
        except Exception as err:  # noqa: BLE001 - any failure is retried later
            log.warning("Sending mail %s (%s) failed: %s", mail["id"], mail["kind"], err)
            db.execute("UPDATE mail_outbox SET last_error = ?, claimed_at = ? WHERE id = ?",
                       (str(err)[:500], time.time(), mail["id"]))
        else:
            db.execute("UPDATE mail_outbox SET sent_at = ?, last_error = NULL WHERE id = ?",
                       (utc_now(), mail["id"]))
            sent += 1
        db.commit()
    return sent


def send_test(mail_id: int) -> str | None:
    """Send the test mail ``mail_id`` right now and delete it; returns the error, or
    None if the mail server took it. Test mails are never queued for others
    (``deliver_pending`` skips them), so the result is this mail's own."""
    db = get_db()
    row = db.execute("SELECT * FROM mail_outbox WHERE id = ? AND kind = 'test'", (mail_id,)).fetchone()
    db.commit()
    error = "?" if row is None else None
    if row is not None:
        try:
            _send(row)
        except Exception as err:  # noqa: BLE001 - reported to the admin
            log.warning("Sending the test mail failed: %s", err)
            error = str(err)[:500] or err.__class__.__name__
    db.execute("DELETE FROM mail_outbox WHERE id = ?", (mail_id,))
    db.commit()
    return error


def prune_outbox() -> int:
    """Delete sent (and given-up) mails after KEEP_DAYS: they contain personal data."""
    db = get_db()
    cutoff = utc_now(-24 * KEEP_DAYS)
    count = db.execute(
        "DELETE FROM mail_outbox WHERE created_at < ? AND (sent_at IS NOT NULL OR attempts >= ? OR kind = 'test')",
        (cutoff, MAX_ATTEMPTS)).rowcount
    db.commit()
    return count


def build_message(recipient: str, subject: str, text: str, body_html: str, sender_name: str) -> EmailMessage:
    sender = appsettings.value("mail_from")
    msg = EmailMessage()
    msg["From"] = formataddr((_one_line(sender_name), sender))
    msg["To"] = recipient
    msg["Subject"] = _one_line(subject)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    return msg


def _send(row) -> None:
    sender = row["body_text"].split("\n", 1)[0]   # the event name heads every mail
    transmit(build_message(row["recipient"], row["subject"], row["body_text"], row["body_html"], sender))


def transmit(msg: EmailMessage) -> None:
    backend = current_app.config["MAIL_BACKEND"]
    if backend == "memory":
        current_app.extensions.setdefault("sent_mail", []).append(msg)
        return
    if backend == "console":
        log.warning("Mail (MAIL_BACKEND=console, not sent) to %s: %s\n%s",
                    msg["To"], msg["Subject"], msg.get_body(("plain",)).get_content())
        return
    value = appsettings.value
    security, host = value("smtp_security"), value("smtp_host")
    port = value("smtp_port") or (465 if security == "ssl" else 587)
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if value("smtp_username"):
            server.login(value("smtp_username"), value("smtp_password"))
        server.send_message(msg)


# --- sending right after a request --------------------------------------------------

_wake = threading.Event()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def flush_soon() -> None:
    """Send what was just queued without making the request wait for the mail
    server. Tests (and MAIL_SEND_INLINE) send synchronously instead."""
    if not enabled():
        return
    app = current_app._get_current_object()
    if app.config.get("TESTING") or app.config.get("MAIL_SEND_INLINE"):
        deliver_pending()
        return
    global _worker
    with _worker_lock:
        _wake.set()
        # a worker that is still registered will see the wake: it only retires under
        # this lock, after finding no wake (see _drain)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, args=(app,), daemon=True, name="mail-sender")
            _worker.start()


def _drain(app) -> None:
    global _worker
    while True:
        with _worker_lock:
            if not _wake.is_set():
                _worker = None        # retire; the next flush_soon() starts a new worker
                return
            _wake.clear()
        try:
            with app.app_context():
                deliver_pending()
        except Exception:  # noqa: BLE001 - the scheduler retries
            log.exception("Background mail delivery failed")
