"""Maintenance commands: ``python -m lanparty.manage <command>``.

* ``reset-admin-password``: delete every admin account and session, so ADMIN_PASSWORD
  from the environment works again (start mode) and new accounts can be created.
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
    """Delete every admin account (and session): back to start mode with ADMIN_PASSWORD."""
    with app.app_context():
        with write_transaction():
            count = sec.remove_all_admin_accounts()
            sec.record_audit("console", "admin.accounts_reset", accounts=count)
        if not app.config["ADMIN_PASSWORD"]:
            print(f"Removed {count} admin accounts. ADMIN_PASSWORD isn't set, so the admin area stays "
                  "disabled until you set it.")
            return 1
    print(f"Removed {count} admin accounts and ended every admin session. Log in with ADMIN_PASSWORD "
          "and create new accounts on /admin/security.")
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
    commands.add_parser("reset-admin-password", help="delete all admin accounts; ADMIN_PASSWORD works again")
    send = commands.add_parser("test-mail", help="send a test mail")
    send.add_argument("address")
    args = parser.parse_args(argv)
    app = create_app()
    if args.command == "reset-admin-password":
        return reset_admin_password(app)
    return test_mail(app, args.address)


if __name__ == "__main__":
    sys.exit(main())
