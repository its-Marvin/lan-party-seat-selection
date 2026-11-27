"""Scheduled jobs: ``python -m lanparty.jobs``.

Runs every ``--interval`` seconds (default 60) until stopped; in Docker it is the
``scheduler`` service. ``--once`` runs a single pass (for a system cron job).
Each pass:

* deletes registrations whose address wasn't confirmed in time,
* releases unpaid seats whose payment window ran out,
* mails payment reminders (when an event enables them),
* sends queued mails that couldn't be sent right away, and retries failed ones,
* deletes old sent mails and old audit log entries,
* records a heartbeat, shown on the admin pages (``--check`` tests it, for health checks).

Maintenance commands (password reset, test mail) are in ``python -m lanparty.manage``.

Several schedulers at once are harmless: everything runs under the database's
write lock or claims its work first.
"""

import argparse
from datetime import datetime, timezone
import logging
import sys
import time

from . import create_app
from . import api
from . import appsettings
from . import events as ev
from . import mail
from . import notify
from . import security as sec
from . import settings as cfg
from .db import get_db, utc_now, write_transaction

log = logging.getLogger("lanparty.jobs")


def send_payment_reminders(event_id: int) -> int:
    """Remind unpaid participants REMINDER_HOURS_BEFORE their payment deadline,
    once per payment window. Only for events that enabled reminders."""
    if not mail.enabled():
        return 0
    lead = appsettings.value("reminder_hours_before")
    with write_transaction() as db:
        event = cfg.load(event_id)
        hours = event["reservation_hours"]
        # no deadline, or a window too short to need a reminder (the payment mail says it all)
        if not event["payment_reminders"] or ev.status(event) != "active" or hours <= lead:
            return 0
        due = db.execute(
            "SELECT * FROM users WHERE event_id = ? AND paid = 0 AND seat IS NOT NULL "
            "AND reserved_at IS NOT NULL AND reserved_at <= ? AND reserved_at > ? "
            "AND (reminder_sent_at IS NULL OR reminder_sent_at < reserved_at) "
            f"AND {api.REACHABLE}",
            (event_id, utc_now(lead - hours), utc_now(-hours))).fetchall()
        for user in due:
            notify.payment_reminder(user, event)
            db.execute("UPDATE users SET reminder_sent_at = ? WHERE id = ?", (utc_now(), user["id"]))
            sec.record_audit("system", "payment.reminder_sent", **api.user_summary(user))
    return len(due)


def run_once(app) -> dict:
    with app.app_context():
        result = {"reminders": 0}
        api.expire_unconfirmed()           # every event
        event_id = ev.active_id()
        if event_id is not None:
            api.expire_reservations(event_id)
            result["reminders"] = send_payment_reminders(event_id)
        result["sent"] = mail.deliver_pending(limit=500)
        result["pruned_mails"] = mail.prune_outbox()
        result["pruned_audit"] = sec.prune_audit_log()
        db = get_db()
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (api.JOBS_HEARTBEAT, utc_now()))
        db.commit()
        return result


def check(app, max_age: int) -> bool:
    """Did a pass finish within ``max_age`` seconds?"""
    with app.app_context():
        row = get_db().execute("SELECT value FROM settings WHERE key = ?", (api.JOBS_HEARTBEAT,)).fetchone()
    if not row:
        return False
    last = datetime.fromisoformat(row["value"]).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() <= max_age


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lanparty.jobs", description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between passes (default 60)")
    parser.add_argument("--check", action="store_true",
                        help="exit 0 if a pass finished within 3 intervals (health check)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    if args.check:
        return 0 if check(app, 3 * max(args.interval, 1)) else 1
    while True:
        try:
            result = run_once(app)
            if any(result.values()):
                log.info("pass: %s", result)
        except Exception:  # noqa: BLE001 - keep running; the next pass retries
            log.exception("Scheduled jobs failed")
            if args.once:
                return 1               # a one-shot run (cron, an operator) must see the failure
        if args.once:
            return 0
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    sys.exit(main())
