"""Maintenance commands: ``python -m lanparty.manage <command>``.

* ``reset-admin-password``: forget the admin password set on /admin/security, so
  ADMIN_PASSWORD from the environment works again (ends every admin session).
* ``test-mail ADDRESS``: send a test mail with the settings from /admin/mail.
"""

import argparse
import sys

from . import create_app
from . import events as ev
from . import mail
from . import notify
from . import security as sec
from . import settings as cfg
from .db import get_db, write_transaction


def reset_admin_password(app) -> int:
    with app.app_context():
        with write_transaction():
            sec.set_admin_password(None)
            sec.record_audit("console", "admin.password_reset")
        sec.end_all_admin_sessions()
        if not app.config["ADMIN_PASSWORD"]:
            print("The stored admin password was removed, but ADMIN_PASSWORD isn't set: "
                  "the admin area stays disabled until you set it.")
            return 1
    print("The admin password from the admin area was removed; ADMIN_PASSWORD applies again.")
    return 0


def test_mail(app, recipient: str) -> int:
    with app.app_context():
        if not mail.enabled():
            print("Mail is not ready; missing on /admin/mail or /admin/settings: "
                  + (", ".join(mail.problems()) or "mail is switched off"))
            return 1
        event_id = ev.active_id() or ev.default_admin_id()
        with write_transaction():
            notify.test_mail(recipient, cfg.load(event_id))
        if not mail.deliver_pending():
            row = get_db().execute("SELECT last_error FROM mail_outbox WHERE kind = 'test' "
                                   "ORDER BY id DESC LIMIT 1").fetchone()
            print(f"Sending failed: {row['last_error'] if row else 'unknown error'}")
            return 1
    print(f"Test mail sent to {recipient}.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lanparty.manage", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reset-admin-password", help="use ADMIN_PASSWORD from the environment again")
    send = commands.add_parser("test-mail", help="send a test mail")
    send.add_argument("address")
    args = parser.parse_args(argv)
    app = create_app()
    if args.command == "reset-admin-password":
        return reset_admin_password(app)
    return test_mail(app, args.address)


if __name__ == "__main__":
    sys.exit(main())
