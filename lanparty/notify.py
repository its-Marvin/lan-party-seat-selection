"""The mails the app sends, in the participant's language. Each function queues
one mail (``mail.enqueue``); call them inside the write transaction of the change
they report, and ``mail.flush_soon()`` after it.
"""

from datetime import datetime, timedelta

from flask import current_app

from . import appsettings
from . import mail
from .i18n import tr_in


def _money(cents: int, lang: str) -> str:
    amount = f"{cents / 100:,.2f}"
    if lang == "de-DE":
        return amount.replace(",", " ").replace(".", ",").replace(" ", ".") + " €"
    return "€" + amount


def _greeting(m: mail.Mail, user) -> None:
    m.p(tr_in(m.lang, "Hi {name},", name=user["name"]))


def _deadline(user, event: dict) -> datetime | None:
    """End of the payment window, in the event's time zone (None = no deadline)."""
    if not user["reserved_at"] or event["reservation_hours"] <= 0:
        return None
    return mail.utc_to_local(user["reserved_at"], timedelta(hours=event["reservation_hours"]))


def _event_facts(lang: str, event: dict, seat: str | None) -> list[tuple[str, str]]:
    when = ""
    if event["event_start"]:
        when = mail.format_local(datetime.fromisoformat(event["event_start"]), lang)
        if event["event_end"]:
            when += " – " + mail.format_local(datetime.fromisoformat(event["event_end"]), lang)
    return [(tr_in(lang, "Event"), event["event_name"]), (tr_in(lang, "When"), when),
            (tr_in(lang, "Where"), event["location"]), (tr_in(lang, "Seat"), seat or "")]


def _payment_facts(lang: str, user, event: dict) -> list[tuple[str, str]]:
    deadline = _deadline(user, event)
    reference = tr_in(lang, "{event} - Seat {seat} - {name}", event=event["event_name"], seat=user["seat"],
                      name=user["name"])
    return [(tr_in(lang, "Amount"), _money(event["amount_cents"], lang)),
            (tr_in(lang, "PayPal (friends & family)"), event["paypal_address"]),
            (tr_in(lang, "Payment reference"), reference),
            (tr_in(lang, "Pay by"), mail.format_local(deadline, lang) if deadline else "")]


def lang_of(user) -> str:
    return user["lang"] or "en-US"


def confirmation(user, event: dict, code: str) -> None:
    lang = lang_of(user)
    until = mail.format_local(mail.utc_to_local(user["confirm_until"]), lang)
    m = mail.Mail(lang, tr_in(lang, "Please confirm your registration for {event}", event=event["event_name"]))
    _greeting(m, user)
    m.p(tr_in(lang, "thanks for registering! Please confirm your email address to complete your registration."))
    if user["seat"]:
        m.p(tr_in(lang, "Seat {seat} is held for you until {until}. If you don't confirm by then, "
                        "the registration is deleted and the seat becomes free again.",
                  seat=user["seat"], until=until))
    else:
        m.p(tr_in(lang, "Please confirm by {until}; otherwise the registration is deleted.", until=until))
    m.button(tr_in(lang, "Confirm registration"), mail.link(f"confirm={code}"),
             tr_in(lang, "If the button doesn't work, open this link:"))
    m.note(tr_in(lang, "Didn't register? Then just ignore this mail; the registration is deleted automatically."))
    mail.enqueue(user, m, "confirmation", event=event)


def payment_request(user, event: dict) -> None:
    lang = lang_of(user)
    m = mail.Mail(lang, tr_in(lang, "{event}: please pay for seat {seat}", event=event["event_name"],
                              seat=user["seat"]))
    _greeting(m, user)
    m.p(tr_in(lang, "your seat is reserved. To confirm it, please send the fee via PayPal:"))
    m.facts(_event_facts(lang, event, user["seat"]) + _payment_facts(lang, user, event))
    if _deadline(user, event):
        m.p(tr_in(lang, "If the payment hasn't been confirmed by then, the seat is released automatically."))
    m.p(tr_in(lang, "As soon as an admin confirms your payment, you get a confirmation by mail."))
    m.button(tr_in(lang, "Open the seat plan"), mail.link(), tr_in(lang, "If the button doesn't work, open this link:"))
    mail.enqueue(user, m, "payment_request", event=event)


def payment_reminder(user, event: dict) -> None:
    lang = lang_of(user)
    deadline = _deadline(user, event)
    m = mail.Mail(lang, tr_in(lang, "Reminder: please pay for seat {seat} by {deadline}", seat=user["seat"],
                              deadline=mail.format_local(deadline, lang)))
    _greeting(m, user)
    m.p(tr_in(lang, "we haven't received your payment yet. Your seat is held until {deadline}; after that it is "
                    "released automatically.", deadline=mail.format_local(deadline, lang)))
    m.facts(_event_facts(lang, event, user["seat"]) + _payment_facts(lang, user, event))
    m.note(tr_in(lang, "Already paid? Then the admins just haven't confirmed it yet; no need to do anything."))
    mail.enqueue(user, m, "payment_reminder", event=event)


def payment_confirmed(user, event: dict, access_code: str) -> None:
    lang = lang_of(user)
    m = mail.Mail(lang, tr_in(lang, "{event}: your seat {seat} is confirmed", event=event["event_name"],
                              seat=user["seat"] or "–"))
    _greeting(m, user)
    m.p(tr_in(lang, "your payment has arrived: your seat is confirmed. See you there!"))
    m.facts(_event_facts(lang, event, user["seat"])
            + [(tr_in(lang, "Paid"), _money(user["paid_amount_cents"] or 0, lang))])
    _access_link(m, access_code)
    mail.enqueue(user, m, "payment_confirmed", event=event)


def new_access_link(user, event: dict, access_code: str, reason: str = "logout") -> None:
    """``reason``: the participant logged out other devices, or an admin reset the links."""
    lang = lang_of(user)
    m = mail.Mail(lang, tr_in(lang, "{event}: your new personal link", event=event["event_name"]))
    _greeting(m, user)
    if reason == "admin":
        m.p(tr_in(lang, "the organizers have replaced the personal links, so your previous link no longer works. "
                        "This is the new one."))
    else:
        m.p(tr_in(lang, "you logged out your other devices, so your previous personal link no longer works. "
                        "This is the new one."))
    _access_link(m, access_code)
    mail.enqueue(user, m, "access_link", event=event)


def _access_link(m: mail.Mail, code: str) -> None:
    lang = m.lang
    m.p(tr_in(lang, "Your personal link logs you in on any device, until the event ends. Keep it to yourself: "
                    "whoever opens it is logged in as you."))
    m.button(tr_in(lang, "Open my registration"), mail.link(f"access={code}"),
             tr_in(lang, "If the button doesn't work, open this link:"))
    m.note(tr_in(lang, "Shared the link by mistake? Choose \"Log out other devices\" on the site: the link stops "
                       "working and you get a new one."))


def message(user, event: dict, subject: str, body: str) -> None:
    """A message an admin wrote to participants (plain text, kept as written)."""
    lang = lang_of(user)
    m = mail.Mail(lang, subject)
    _greeting(m, user)
    for paragraph in body.replace("\r\n", "\n").split("\n\n"):
        if paragraph.strip():
            m.p(paragraph.strip("\n"))
    mail.enqueue(user, m, "message", event=event)


def test_mail(recipient: str, event: dict) -> int:
    """Queues a test mail (from /admin/mail or ``manage test-mail``); returns its outbox id."""
    lang = "en-US"
    m = mail.Mail(lang, f"Test mail from {event['event_name']}")
    m.p("This is a test mail from the LAN party seat registration. Mail works.")
    m.facts([("SMTP server", appsettings.value("smtp_host") or current_app.config["MAIL_BACKEND"]),
              ("Site address", appsettings.value("public_url")), ("Theme", event.get("theme", "default"))])
    m.button("Open the site", mail.link(), "If the button doesn't work, open this link:")
    fake_user = {"id": None, "email": recipient}
    return mail.enqueue(fake_user, m, "test", event=event)    # sent with mail.send_test()
