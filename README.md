# LAN Party Seat Selection

Seat registration for your LAN party. Participants sign up, pick their seat on a map of your
venue and see straight away what to pay and where. You confirm the payments and keep everything
in one place, instead of in a group chat, a spreadsheet and a PayPal inbox.

![The public page: event details, your registration and the seat map](docs/screenshots/public-page.png)

## What it does

**For participants**

- Sign up with a name and email, then click a free seat on a map that shows the real venue:
  tables, doors, windows, the sleeping area and food & drinks.
- After picking a seat, a popup shows the fee, the PayPal address, a payment reference and the
  payment deadline.
- See who sits where (with the event code), switch seats later (a payment moves along), and
  log in on other devices with a one-time link. No passwords to remember.
- **With mail switched on:** confirm the email address with one click, get the payment details by
  mail, a reminder before the deadline, and after paying a seat confirmation with a personal link
  that logs you in on any device.
- Works on phones, in light and dark mode, in English and German (see [Languages](#languages)).

<p>
  <img src="docs/screenshots/payment-dialog.png" alt="Payment popup after picking a seat" width="62%">
  <img src="docs/screenshots/phone.png" alt="The seat map on a phone" width="30%">
</p>

**For organizers**

- **Participants and payments:** confirm payments, move people to other seats, remove them, or
  send someone a login link. The summary shows how many seats are taken and paid, and how much
  money came in and is still outstanding.
- **Unpaid reservations expire** after a set time (one week by default), so nobody blocks a seat
  without paying. Optionally, a reminder mail goes out 48 hours before.
- **Optional mail**, set up on its own admin page: confirmation of new registrations, payment
  requests, seat confirmations, reminders and messages to everyone, to open payments or to the
  paid participants. Every mail comes in the colours of its event's theme.
- **Seat layout editor:** drag groups of two-person tables and area elements into place on a grid.
  Seats are numbered automatically (A1, A2, …), and people keep their seats when you rearrange.
- **Several events:** prepare the next LAN in advance. Each event opens on its registration date
  and is archived when it ends.
- **Themes:** give each event its own look; a retro terminal theme is included, and you can add
  your own (see [THEMES.md](THEMES.md)).
- **Security area:** an account for each admin (own name and password), see and end admin
  sessions, void participants' links or log them out, adjust the rate limits, and a list of checks (HTTPS, proxy settings,
  event codes, mail encryption, failed logins).
- **Audit log:** every admin action is recorded with the admin's name, so a changed PayPal
  address or a confirmed payment can always be traced.

![Participants table in the admin area, with payment summary](docs/screenshots/admin-participants.png)

![The drag-and-drop seat layout editor](docs/screenshots/layout-editor.png)

![The public seat map in the included retro theme](docs/screenshots/retro-theme.png)

## Why use it

- **Made for LAN parties:** two-person tables, seat groups, a fee paid via PayPal, and the
  question everyone asks ("who sits next to me?") are built in, not bolted onto a generic
  booking tool.
- **Private by default:** registration stays closed and names stay hidden until you set an
  **event code**, which you share only with your guests. Email addresses are only shown to admins (and each participant sees their own).
- **Secure:** rate limits on every guessable code, server-side admin sessions, a strict
  content security policy and an audit log; reviewed against the OWASP Top 10
  (see [Security](#security)).
- **Easy to run:** one `docker compose up -d` with PostgreSQL, or just Python and SQLite on a
  laptop. No build step, no JavaScript framework, no external services: your participants' data
  stays on your server.
- **Free and open source** (see [License](#license)).

Stack: Python 3.11+ with Flask, SQLite (Python's built-in `sqlite3`) or PostgreSQL, and plain
HTML/CSS/JS with no build step. A Docker Compose setup with PostgreSQL is included
(see [Docker](#docker)). The screenshots show a fictional demo event.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

export ADMIN_PASSWORD='choose-a-strong-password'   # at least 12 characters
.venv/bin/python -m lanparty
```

Open <http://127.0.0.1:5000> for the public page and <http://127.0.0.1:5000/admin> for the admin area.
For a real event, use [Docker](#docker) instead. To get started, log in as admin with any name and
`ADMIN_PASSWORD`, create your own admin account under *Security* (after that, everyone logs in with
their own account), then fill in the event details, the PayPal address and the amount,
arrange the seat layout, and **set an event code**. Until there is a code, registration is closed.
The first event is created automatically and is open straight away; see [Events](#events) for
planning the next one.

## Configuration

Most settings are made on the admin pages (see [Admins](#admins)). Environment variables set up
the server; the ones marked ⚙ are only the **defaults** of settings that admins can change on
`/admin/settings` or `/admin/security` (a saved value wins). Mail is configured only on
`/admin/mail`.

| Variable                | Default                      | Purpose |
|-------------------------|------------------------------|---------|
| `ADMIN_PASSWORD`        | *(empty → admin disabled)*   | Start password for `/admin`, used until the first admin account exists (see [Admins](#admins)). Must be at least 12 characters, or the app refuses to start. Changing it logs out all start-mode sessions. `python -m lanparty.manage reset-admin-password` deletes all admin accounts, so it works again. |
| `ALLOW_WEAK_ADMIN_PASSWORD` | `0`                      | `1` allows a shorter password. **Local development only.** |
| `DATABASE`              | `instance/lanparty.sqlite3`  | Path to the SQLite file. It is created automatically. |
| `DATABASE_URL`          | *(empty → SQLite)*           | PostgreSQL connection URL, e.g. `postgresql://lanparty@db:5432/lanparty`. When set, it replaces `DATABASE`. The password can be part of the URL or come from `PGPASSWORD` (libpq's other `PG*` variables work too). The tables are created at startup. |
| `HOST` / `PORT`         | `127.0.0.1` / `5000`         | Address for the development server (`python -m lanparty`). |
| `SESSION_COOKIE_SECURE` | `0`                          | Set this to `1` when you serve the app over HTTPS. The admin cookie is then only sent over HTTPS. |
| `TRUSTED_PROXIES`       | `0`                          | Number of reverse proxies in front of the app. Needed so that rate limits see the real client IP (see [Deployment](#deployment-notes)). |
| `MAX_CONTENT_LENGTH`    | `262144` (256 KB)            | Maximum request size in bytes. |
| ⚙ `ADMIN_LOGIN_MAX_PER_IP` / `ADMIN_LOGIN_MAX_GLOBAL` | `5` / `50` | Failed admin logins allowed per IP / in total, per `ADMIN_LOGIN_WINDOW_MINUTES` (`15`). "Per IP" limits count IPv6 clients per `/64` network (see [Security](#security)). |
| ⚙ `REGISTER_MAX_PER_IP` | `10`                         | Registrations per IP per hour. Failed attempts count too. |
| ⚙ `MAX_PENDING_PER_IP` | `4`                          | Unpaid seat reservations made from one IP at the same time. |
| ⚙ `LOGIN_CODE_MAX_PER_IP` | `20`                         | Login link attempts per IP per 15 minutes. |
| ⚙ `EVENT_CODE_MAX_PER_IP` | `10`                         | Wrong event codes per IP per 15 minutes. |
| ⚙ `LOGIN_CODE_HOURS` | `168`                        | How long a one-time login link stays valid (default 7 days). |
| ⚙ `AUDIT_LOG_RETENTION_DAYS` | `365`                     | Audit log entries older than this are deleted (the log names participants and their emails). `0` keeps them forever. |
| ⚙ `ADMIN_SESSION_IDLE_MINUTES` / `ADMIN_SESSION_MAX_HOURS` | `60` / `12` | An admin session ends after this much inactivity, or after this total time at the latest. |
| `THEMES_DIR`            | `instance/themes`            | Folder with your own themes, one sub-folder per theme (see [THEMES.md](THEMES.md)). |
| ⚙ `PUBLIC_URL`          | *(empty)*                    | The site's address, e.g. `https://lan.example.com`; links in mails point there. Usually set on `/admin/settings`. |
| `MAIL_BACKEND`          | *(empty)*                    | Development only: `console` writes mails to the log instead of sending them (the SMTP server can then stay empty). |
| ⚙ `TIMEZONE` | *(server's local time)*      | IANA time zone of the event dates, e.g. `Europe/Berlin`. Decides when registration opens and when an event is archived. Set it when the server runs in UTC (e.g. in a container). |

You don't need a `SECRET_KEY`: admin sessions are stored in the database, not in signed cookies.

Admins change everything else in the admin UI, separately for each event:

- event name, when registration opens, start and end, location, description
- the **theme** of the public page (see [THEMES.md](THEMES.md))
- PayPal address (an email address or a `paypal.me/...` link) and the amount due in €
- how long unpaid reservations are held (default 168 hours = one week; 0 = forever), and whether
  a payment reminder is mailed before the deadline (only shown when mail is on)
- the **event code** (see below)
- the seat layout (see below)

## Events

The app can hold several events, for example last summer's party, the one that is open now, and
the next one that is already being planned. Each event has its own settings, seat layout, event
code and participants. People register again for each event; their email address may be used once
per event.

An event's status follows from its dates (in `TIMEZONE`):

| Status | When | What happens |
|---|---|---|
| **draft** | No "Registration opens" date yet | Only admins see it. |
| **upcoming** | Registration opens in the future | Listed on the public page under *Upcoming events* (name, dates, location, opening date). |
| **active** | Registration has opened and the event hasn't ended | The public page shows this event. Participants register (with its event code) and pick seats. |
| **archived** | The end date has passed | Kept for reference. Its participants and seat plan are read-only, but its settings can still be corrected; fixing a wrong end date brings the event back. |

Registration windows (from "Registration opens" to the end) of two events may not overlap, so
**registration can only open after the previous event has ended**. An event without an end date
blocks every later opening until an end is set. Between two events, the public page says that no
event is open and lists the upcoming ones.

On the admin page, the **Event** card at the top picks the event to show and edit:

- **+ New event** copies the selected event's settings and seat layout, but not its dates, event
  code or participants. The new event starts as a draft.
- **Delete event** deletes an event that isn't active, with all of its participants. Use it, for
  example, to remove old personal data once an event is long over.

Login links and device logins belong to one event. Once an event is archived, its participants
are logged out on the public page and register again for the next event.

Databases from versions before multi-event support are migrated automatically: everything becomes
the first event, which stays open.

## Event code

The event code is 4–6 letters or digits that you share with your guests, for example in the
invitation. **The app is secure by default: until an admin sets a code, registration is
closed and visitors see no names.** The admin page shows a warning banner while no code is set. *Generate* in the admin settings creates a random one without easily confused
characters (0/O, 1/I).

- **Without the code**, visitors see the seat plan, including which seats are free, reserved and
  paid, but **not who sits where**, and they **can't register**.
- **After entering the code** they see the names, so friends can find each other and sit
  together. This works before registering. The code is stored in the browser.
- Registered participants and admins always see the names.
- **Changing the code** locks out visitors who only had the old code. They are asked for the new
  one. People who are already registered aren't affected.
- **Removing the code** closes registration again. Codes visitors entered earlier stop working,
  and only participants and admins see names.
- Case doesn't matter.

Wrong codes are rate-limited: after 10 wrong attempts from one IP within 15 minutes, no codes
from that IP are checked at all, whether entered on the page, sent with the seat plan request or at
registration. Since the code is shared with every guest, treat it as a barrier against spam and
curious strangers, not as a password. Use 6 characters.

## Languages

Both pages are available in **English (en-US)** and **German (de-DE)**. The German texts address
participants informally (*du*). Visitors get the language of their browser. If it's neither
English nor German they get English. The selector in the header switches the language and
remembers the choice in that browser. If the browser doesn't allow storing it (privacy
settings), the choice is kept in the address instead (`?lang=de-DE`), and links between the
public and the admin page carry it along. Dates, times and amounts are formatted for the chosen
language.

Error messages from the server follow the same language: the browser sends it in the `X-Lang`
header, and other clients can use `Accept-Language`. Event content that admins type in (name,
description, location) is shown as entered. The audit log stays in English.

To change a text or add a language:

- Browser texts: `lanparty/static/locales/<lang>.js`, plain JSON inside `I18N.add(...)`. Keys like
  `pub.*` (public page), `admin.*`, `editor.*` (layout editor) and `common.*`. `key.one` / `key.other`
  are plural forms, and `{name}` is a placeholder. Static HTML refers to keys with `data-i18n`,
  `data-i18n-html` or `data-i18n-attr`. Scripts use `t("key", {…})`.
- Server messages: the English text in the code is the key (`tr("Seat {seat} is already taken.", seat=…)`),
  and `lanparty/locales/<lang>.json` maps it to the translation.
- A new language also needs an entry in `LANGUAGES` in `lanparty/i18n.py` and
  `lanparty/static/i18n.js`, a `<script>` tag for its catalog in both HTML pages, and an option
  in the language selector.

`tests/test_i18n.py` fails when a key or server message has no translation, when a translation is
no longer used, or when placeholders differ between languages.

## Seat layout

The layout editor in the admin area shows the event area as a grid. One grid square is the width
of one seat.

- **Tables** always seat two people. A horizontal table is 2 × 1 squares.
- **Groups** are blocks of tables. Each group has:
  - a **label** of 1–3 letters (`A`, `B`, `VIP`, …), which seat numbers start with
  - an optional **name**, e.g. "Clan corner", shown next to the label
  - the **number of tables**, and how many **tables per row** (e.g. 8 tables, 4 per row
    = two rows of four tables facing each other)
  - an **orientation**: horizontal (rows run left → right) or vertical (the same block turned
    sideways)
  - a **position** on the area
- Seats are numbered per group, left to right and row by row: table 1 has `A1`/`A2`, table 2 has `A3`/`A4`, and so on.
- To arrange groups, **drag** them, or select one and use the **arrow keys**. The X/Y fields
  in the side panel work too. *Add group* puts a new group in the next free spot,
  *Duplicate* copies the selected group next to it, and *Delete* (or the Del key) removes it.
- **Area elements** mark everything on the plan that isn't a table:
  🚪 door, 🪟 window, 🛏️ sleeping area, 🍕 food & drinks, and 📍 other (stage, toilets, …).
  Each has an optional label, e.g. "Bar & Pizza" or "Emergency exit". Add them with the buttons above
  the plan, drag them into place, and resize them with the handle in their bottom-right corner
  (or Shift + arrow keys, or the width/height fields). Elements may overlap each other, for example
  a window at the edge of the sleeping area, but not a table group.
- The area width and height are in grid squares (4–200 each). The editor warns
  you about groups that overlap or stick out of the area. Nothing changes until you
  click **Save layout**, and *Discard changes* goes back to the saved version.

When you save, the server checks the layout again and protects seats that are already taken:

- If you **change a group's label**, the people sitting in that group stay put and get new
  seat numbers (`A3` → `C3`). Swapping two labels works as well.
- Moving or rotating a group, or resizing the area, never affects anyone's seat.
- If the new layout **would remove an occupied seat** (fewer tables, or a deleted group), the save is
  refused and the message names the people affected. Move them first.

Limits: at most 60 groups, 60 tables per group, 1000 seats in total and 100 area elements.

## How it works

### Participants
1. Register with name and email. Each email address can only be registered once. With mail on,
   a confirmation link goes to that address; see [Mail](#mail).
2. The browser receives a random **device token** and keeps it in `localStorage`. The server
   stores only a SHA-256 hash of it.
3. Click a free seat → confirm → the payment popup shows the amount, the PayPal address, a
   payment reference (`<event> - Seat <seat> - <name>`) and the **payment deadline**.
4. Participants can **switch to another free seat themselves**, before and after paying.
   A confirmed payment moves with them (switches of paid participants appear in the audit log).
   Unpaid participants can also release their seat; giving up a *paid* seat means a refund, so
   only an admin can do that.
5. **Unpaid reservations expire.** If the payment isn't confirmed within the configured time
   (default one week), the seat is released automatically. The time counts from the first
   seat pick: switching seats, or releasing and picking again within that time, doesn't extend it.
6. **Logging in on another device:** *Create login link* gives a one-time link (`/#login=…`).
   It works once, stays valid for 7 days (`LOGIN_CODE_HOURS`), and gives that device its own token. The device the link
   came from stays logged in. If a browser that is already logged in as someone else opens a link,
   it asks before switching accounts. People who lost access can get a link from an admin.
7. **Logging out:** *Log out* ends the login on this device; the token stops working on the server
   too, not only in the browser. *Log out other devices* ends every other login of that person
   (for example on a lost phone) and voids their unused login links. With mail, their personal
   link is replaced too, and the new one is mailed to them.

Seat colours: free, reserved (payment pending), paid, your seat. The public seat map shows
the tables and groups exactly as the admin arranged them. Hovering over a seat (or clicking a taken
one) shows who sits there. The map never shows email addresses, and it refreshes every 15 seconds.

**Phones:** the pages work at phone widths. When the whole event area doesn't fit, the seat map
shows just the part with tables and area elements and shrinks the seats (down to 26 px); if it
still doesn't fit, it scrolls sideways and says so. On the admin page, the participant and audit
tables turn into one card per row.

**Light / dark mode:** both pages follow the browser/system setting (`prefers-color-scheme`).
The *Auto / Light / Dark* selector in the header overrides it; the choice is remembered in that
browser (`localStorage`). Every theme has both modes.

**Themes:** the public page uses the theme chosen for the active event. Admins pick it under
*Settings → Appearance* and can preview any theme with `/?theme=<id>`. Custom themes go in
`THEMES_DIR`; [THEMES.md](THEMES.md) explains how to build one.

### Admins
The admin area has one page per topic; all of them need an admin login.

**Admin accounts:** every admin logs in with their own name and password. As long as no account
exists (start mode, e.g. right after installing), `ADMIN_PASSWORD` works with any name; use it once
to create the first account on `/admin/security`. From then on `ADMIN_PASSWORD` no longer works,
every start-mode session ends, and further admins are added (or removed) there. Names are unique
regardless of upper/lower case; passwords need at least 12 characters and are stored as scrypt
hashes. Adding or removing an admin, and changing your own password, need your current password.
Nobody can remove their own account, so there is always at least one. If nobody can log in any
more, `python -m lanparty.manage reset-admin-password` on the server deletes every account and
session, and `ADMIN_PASSWORD` works again.

- **`/admin`**: login and an overview of the areas: the current event's numbers, mail status and
  the number of security warnings.
- **`/admin/events`**: everything about one event (pick it at the top):
  - **Event** card: switch between events, create the next one, delete old ones (see [Events](#events)).
  - **Participants:** *Confirm payment* / *Mark unpaid*, **move** a person to any free seat or to
    "no seat", **remove** a person (frees the seat; the email can register again), and create a
    one-time **login link** for someone who lost access. Unconfirmed registrations (mail on) are
    marked with their deadline. The summary line shows registrations, seats taken and paid, money
    received (the amounts actually confirmed) and outstanding (open reservations at the current fee).
  - **Message participants** (mail on): a mail to everyone, to open payments or to the paid
    participants, after showing how many people get it.
  - **Seat layout editor** (see [Seat layout](#seat-layout)) and the event's **settings**.
- **`/admin/settings`**: settings for all events: the site address (for links in mails), the time
  zone of the event dates and how long login links stay valid.
- **`/admin/mail`**: switch mail on and set the SMTP server, port, encryption, login and sender;
  how long new registrations have to confirm their address (default 120 minutes) and how long
  before the deadline reminders go out (default 48 hours). Shows the mail status, sends a test
  mail, and lists mails that are waiting or failed (with a button to try failed ones again).
- **`/admin/security`**:
  - **Checks:** HTTPS and the secure cookie flag, the proxy setting, start mode or the number of
    admin accounts (and passwords unchanged for a year), events without (or with short) event codes, mail encryption and failures, whether the
    scheduler runs, failed admin logins and event-code lockouts in the last 24 hours, audit retention.
  - **Admin accounts:** list (last login, created by whom), add, remove; **your password:** change
    it (your other sessions end, other admins aren't affected).
  - **Admin sessions:** who is logged in, since when, last activity; end single sessions. *Log out
    everywhere* (in the header) ends all of them.
  - **Participants' access** for one event: void unused login links, replace the personal links
    (optionally mailing new ones), or log every participant out on every device.
  - **Limits and timeouts:** rate limits, admin session lengths, audit log retention.
  - **Audit log:** admin logins (including failed attempts, with the network they came from),
    payments, moves, removals, login links, settings and configuration changes, messages sent,
    released reservations and expired registrations, reminders, and networks that used up their
    attempts at guessing the event code. Security-relevant entries are highlighted. Entries are
    deleted after 365 days by default.

## Mail

Mail is optional and off by default; without it the app works as described above. An admin turns
it on at `/admin/mail` (SMTP server, sender address, and the site address on `/admin/settings`,
because the links in mails point there). From then on:

1. **Confirming the address:** after registering, the participant gets a confirmation link. Their
   seat is held meanwhile, but the payment window hasn't started. Registrations not confirmed
   within 120 minutes are deleted and their seat becomes free. The page offers to send the mail
   again (up to 3 times an hour; the deadline stays the same). The link also logs in the device
   it is opened on, unless that browser belongs to someone else.
2. **Payment request:** once the address is confirmed and the participant has a seat, the payment
   window starts and a mail with event, seat, amount, PayPal address, payment reference and
   deadline goes out.
3. **Reminder** (if the event enables it): 48 hours before the deadline, once per payment window.
4. **Seat confirmation:** when an admin confirms the payment, the participant gets a confirmation
   with a **personal link** (`/#access=…`). Unlike login links it works on any number of devices,
   until the event ends, so nobody needs an admin to get back in. *Log out other devices* replaces
   it (and mails the new one); admins can replace all of them on `/admin/security`.
5. **Messages** from admins (see above). Only confirmed addresses get mails; registrations from
   before mail was switched on count as confirmed.

Mails are written to the database in the same transaction as the change they report, sent right
away in the background, and tried again by the **scheduler** (up to 8 times) if the mail server
was unreachable. Each mail is plain text plus HTML in the light colours and fonts of the event's
theme (see [THEMES.md](THEMES.md)), in the participant's language. Sent mails are deleted after
30 days. The SMTP password is stored in the database (it is needed to log in to the mail server)
and is never sent back to the browser.

**Scheduler:** `python -m lanparty.jobs` runs once a minute: it sends payment reminders, retries
failed mails, deletes unconfirmed registrations and expired reservations even when nobody visits
the site, and cleans up old mails and audit entries. In Docker it is the `scheduler` service. Without
Docker, run it as a service, or every few minutes from cron with `--once`. `--check` exits with 0 if
a pass finished recently (for health checks); the admin pages warn when it isn't running.

**Maintenance:** `python -m lanparty.manage test-mail you@example.com` sends a test mail with the
saved settings; `python -m lanparty.manage reset-admin-password` deletes every admin account and
session, so `ADMIN_PASSWORD` works again (start mode).

## API

All endpoints are under `/api` and use JSON.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET    | `/info` | – | The active event's settings, plus `active`, `seat_count`, `registration_open` (never the code itself) and `upcoming` (name, dates, location and opening date of each upcoming event) |
| GET    | `/seats` | – (token optional, marks `mine`) | `{layout, seats, names_visible}` of the active event (`layout` is `null` while none is active). Occupant names are only included for code holders (`X-Event-Code` header), participants and admins. |
| POST   | `/event-code` | – | `{code}` → checks the event code (rate-limited; 403 while no code is set) |
| POST   | `/register` | – | `{name, email, event_code}` → `{token, user, confirmation_required}` (rate-limited; with mail, `user.confirm_until` is the confirmation deadline) |
| GET    | `/me` | token | Current user and payment info (incl. `reserved_until`) |
| POST   | `/me/seat` | token | `{seat: "B3"}` → reserve or change seat; returns payment info |
| DELETE | `/me/seat` | token | Release your seat (only before payment is confirmed) |
| POST   | `/me/login-link` | token | → `{code, expires_at}`: a one-time login code |
| POST   | `/me/logout` | token | Invalidate this device's token |
| POST   | `/me/logout-others` | token | → `{devices, new_link_sent}`: invalidate all other tokens of this person, their unused login codes and their personal link (a new one is mailed) |
| POST   | `/me/confirmation` | token | Send the confirmation mail again (mail on; 3 per hour) |
| POST   | `/confirm` | – | `{code, login}` → `{user, payment, token}`: confirm the email address; `login: true` also returns a device token |
| POST   | `/access/preview` | – | `{code}` → `{user_id, name}` for a personal link |
| POST   | `/access` | – | `{code}` → `{token, user}`; personal links can be used again |
| POST   | `/login/preview` | – | `{code}` → `{user_id, name}`; doesn't use up the code |
| POST   | `/login` | – | `{code}` → `{token, user}`; uses up the code |
| POST   | `/admin/login` | – | `{name, password}` (rate-limited; `name` is required, 400 without it). With accounts, the name selects the account; one error message for a wrong name or password |
| POST   | `/admin/logout` | – | |
| POST   | `/admin/logout-all` | admin | End every admin session |
| GET    | `/admin/session` | – | `{admin: bool, actor}` |
| GET    | `/admin/audit` | admin | Latest 200 audit log entries |
| POST   | `/admin/users/<id>/login-link` | admin | One-time login code for that person |
| GET    | `/admin/themes` | admin | Usable themes `{id, name, description, builtin}`, theme folders with `problems`, and `themes_dir` |
| GET    | `/admin/events` | admin | All events with `status` and participant counts, plus `active_id`, `default_id` and `timezone` |
| POST   | `/admin/events` | admin | `{copy_from: <id> \| null}` → a new draft event |
| DELETE | `/admin/events/<id>` | admin | Delete an event that isn't active, with its participants |
| GET    | `/admin/seats` | admin | Like `/seats`, for any event and always with names |
| GET    | `/admin/users` | admin | All registrations of the event, including emails |
| POST   | `/admin/users/<id>/paid` | admin | `{paid: true \| false}` |
| POST   | `/admin/users/<id>/move` | admin | `{seat: "C4" \| null}` |
| DELETE | `/admin/users/<id>` | admin | Remove the person |
| GET/PUT| `/admin/settings` | admin | Read or update the event's settings (partial updates allowed; `amount` in €, `payment_reminders` true/false); also returns `mail_enabled` and `scheduler_last_run` |
| POST   | `/admin/messages` | admin | `{audience: all \| open \| paid, subject, body}` → `{recipients}`; with `dry_run: true` only counts (mail on) |
| GET    | `/admin/overview` | admin | Summary for `/admin`: current event, mail status, number of security warnings |
| GET/PUT| `/admin/config/<section>` | admin | App-wide settings of `general`, `mail` or `security`: `{fields: [{key, type, value, …}]}`; secrets only report `set`. `PUT` takes `{key: value}`; an empty secret keeps it, `<key>_clear: true` removes it |
| GET    | `/admin/mail/queue` | admin | Mail status and the mails that weren't sent yet |
| POST   | `/admin/mail/retry` | admin | Try given-up mails again |
| POST   | `/admin/mail/test` | admin | `{to}`: send a test mail now; 502 with the server's error if it fails |
| GET    | `/admin/admins` | admin | Admin accounts (`id`, `name`, `created_at`, `created_by`, `last_login_at`, `current`) and `start_mode` |
| POST   | `/admin/admins` | admin | `{name, password, current}`: add an admin (your `current` password confirms it); the first one ends start mode and every session |
| DELETE | `/admin/admins/<id>` | admin | `{current}`: remove another admin and end their sessions |
| POST   | `/admin/password` | admin | `{current, new}`: change your own password; your other sessions end |
| GET    | `/admin/sessions` | admin | Admin sessions (`id`, `actor`, times, `current`) and where the password comes from |
| DELETE | `/admin/sessions/<id>` | admin | End one admin session |
| POST   | `/admin/participant-access` | admin | `{action: login_links \| personal_links \| devices, resend}` for `?event=`: void login links, replace personal links (and mail new ones), or log out every device → `{count, mailed}` |
| GET    | `/admin/security/checks` | admin | The security checks `[{id, level: ok \| info \| warn, text}]` |
| GET/PUT| `/admin/layout` | admin | Read or replace the seat layout; `PUT` returns `{layout, moved}` |

The event-specific admin endpoints (`/admin/users`, `/admin/seats`, `/admin/settings`,
`/admin/layout`, `/admin/messages`, `/admin/participant-access`) take `?event=<id>`. Without it they use the active event, otherwise the next
upcoming one, otherwise the newest. Changing participants or the layout of an archived event
returns 409.

Layout format:

```json
{
  "area": {"width": 24, "height": 12},
  "groups": [
    {"id": "g-a", "label": "A", "name": "", "x": 2, "y": 2,
     "tables": 8, "per_row": 4, "orientation": "horizontal"}
  ],
  "items": [
    {"id": "i-1", "type": "door", "label": "Main entrance", "x": 0, "y": 5, "w": 1, "h": 2}
  ]
}
```

`type` is one of `door`, `window`, `sleeping`, `food`, `other`. You can leave out `items`.

"token" means the `X-User-Token` header. "admin" means the `lp_admin` cookie set by `/admin/login`.
Request bodies must be sent as `Content-Type: application/json`. Other content types get a 415 error,
malformed JSON gets a 400 error, and requests over the size limit get a 413 error. When a rate limit
is hit, the response is a 429 error with a `Retry-After` header. When the database is unreachable or
a write waits too long for its lock, the response is a 503 error with a `Retry-After` header.

## Security

What the app protects against, and how:

| Threat | Protection |
|--------|------------|
| Guessing admin passwords | At most 5 failed logins per IP and 50 overall per 15 minutes (adjustable on `/admin/security`). While locked, even the right password is refused. Passwords under 12 characters are rejected. Each admin has an account with a scrypt-hashed password; a wrong name and a wrong password take as long and get the same answer. Adding or removing admins and changing a password need the current password (failures count as failed logins), so a stolen session alone can't take over the admin area. |
| Stolen or forged admin cookies | Sessions are stored in the database, behind a random `HttpOnly`, `SameSite=Strict` cookie with `Path=/api`. They end after 60 minutes of inactivity or 12 hours at most, on logout, with *Log out everywhere* or *End session* on `/admin/security`, or when that admin's password changes or the account is removed. |
| Admin actions nobody can trace (e.g. a changed PayPal address) | The audit log records the admin's name for each action. |
| One person reserving every seat | Registrations limited per IP, at most 4 unpaid reservations per IP, unpaid reservations expire. Registration requires the event code. |
| Dodging per-IP limits with IPv6 | Every IPv6 user can choose among the 2^64 addresses of their `/64` network, so IPv6 clients are counted per `/64`. IPv4-mapped addresses count as the IPv4 address. |
| A lost phone that is still logged in | *Log out other devices* invalidates every other device token and unused login link of that person; admins can also remove the person. |
| Strangers seeing who attends | The seat plan shows names only to code holders, participants and admins. Guessing the code is rate-limited. Without a code (the default), nobody but participants and admins sees names. |
| Login tokens leaking via URLs or logs | Login, confirmation and personal links carry their code in the URL fragment (`#login=…`, `#confirm=…`, `#access=…`), which browsers never send to the server. Tokens and codes are stored only as SHA-256 hashes. Personal links end with the event, and participants or admins can replace them. |
| Being tricked into using someone else's account | A login link asks before replacing an existing login. |
| XSS / clickjacking | All user text is inserted with `textContent`. Strict CSP (`default-src 'self'`, no inline scripts, `object-src`/`base-uri 'none'`), `X-Frame-Options: DENY`. |
| CSRF | The admin cookie is `SameSite=Strict`, and the API only accepts `application/json` bodies. Participant requests use a custom header. |
| Oversized or malicious requests | 256 KB size limit. `NaN`/`Infinity`, deeply nested JSON, NUL characters and lone UTF-16 surrogates are rejected with a 400 error. |
| Email addresses that rewrite a mail link (`a@b.de?cc=…`) | Addresses with `?`, `#`, `%`, `<`, `>` or quotes are refused at registration, and the admin page escapes mail links. |
| Vulnerable or tampered dependencies | Dependabot proposes weekly updates for Python packages, base images and GitHub Actions. CI runs `pip-audit` on every push and weekly. Actions are pinned to commit SHAs and the Python base image to a digest. |
| Mails as an attack vector | Mail subjects are reduced to one line (no header injection); all text, including admin messages, is HTML-escaped; theme colours are only taken over when they are plain values. Confirmation mails need the event code to register and are rate-limited like registrations, resending to 3 an hour. Messages only go to confirmed addresses. |
| Mail server credentials | The SMTP password is write-only in the admin area (never sent back) and never logged; changes are audited as "(changed)". |
| Caching of personal data | `Cache-Control: no-store` on all API responses. |
| SQL injection | Only parameterised queries. |

The rate-limit tables store keyed hashes of IP addresses, not the addresses themselves. The
audit log keeps only the network of admin login attempts and event-code lockouts (IPv4 `/24`,
IPv6 `/48`): enough to see where an attack comes from, without storing addresses. Entries
written by older versions with full IPs are shortened the same way at startup.

Known limitations:
- The app never checks that an email address is real. Someone can register with another
  person's address (an admin can remove that registration), and the "already registered" message
  reveals whether an address is in use (rate-limited).
- The per-IP limits are best-effort: a determined attacker with many IP addresses (or several
  IPv6 networks) can still reserve several seats. Those seats are released when the reservation
  time runs out, and the admin sees all reservations. The event code is 4 to 6 characters; use
  6 for events you announce publicly.
- Admin accounts have no second factor. The overall limit on failed
  admin logins means that someone with many IP addresses can lock the admins out for 15 minutes.
- Participant device tokens stay valid until the participant logs out, is removed, or the event
  ends (tokens only work for the active event).
- The names of participants are visible to everyone who has the event code. Mention this in your
  event's privacy notice.

## Tests

```bash
.venv/bin/pytest
```

The tests use Flask's test client with a separate SQLite database for each test. To run them
against PostgreSQL, point `LANPARTY_TEST_DATABASE_URL` at an empty database. Each test then gets
its own schema, and the tests that migrate old SQLite files are skipped:

```bash
docker run -d --rm --name lanparty-pg-test -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine
LANPARTY_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/pytest
```

They cover registration, seat selection and conflicts, payment confirmation, moving and removing people,
settings validation and admin access control (`tests/test_app.py`). `tests/test_layout.py` covers
the layout: geometry, validation (overlaps, bounds, labels, limits), protection of occupied seats,
and renumbering when a group is renamed. `tests/test_security.py` covers the protections:
rate limits, session expiry and revocation, rejection of forged cookies, reservation expiry,
one-time login links, input hardening, the audit log, and migration of databases from older versions.
`tests/test_event_code.py` covers hiding names, the registration check and the limits on code guessing.
`tests/test_owasp.py` covers the fixes from the OWASP Top 10 review: limits per IPv6 network, the
audit log's privacy and retention, participant logout, input checks, 503 responses, and the
supply-chain and container settings.

## Docker

`docker-compose.yml` runs the app (gunicorn, 4 workers), the scheduler (`python -m lanparty.jobs`,
for mail retries, reminders and expiry) and PostgreSQL 17. You only
need the compose file and `.env.example`, not the code: the image is pulled from
`ghcr.io/its-marvin/lan-party-seat-selection` (amd64 and arm64).

```bash
cp .env.example .env      # set ADMIN_PASSWORD and POSTGRES_PASSWORD, check TIMEZONE
docker compose up -d
```

To build the image from a checkout instead, add the build override:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.

The app is then at <http://127.0.0.1:8000>. Settings in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `LANPARTY_VERSION` | `latest` | Image tag: `latest` (the newest release), or a release such as `1.2.3`, `1.2` or `1`. |
| `ADMIN_PASSWORD` | *(required)* | Start admin password (at least 12 characters), used until you create the first admin account on `/admin/security`. |
| `POSTGRES_PASSWORD` | *(required)* | Password of the database user. The database is only reachable inside the compose network; its port is not published. |
| `TIMEZONE` | `UTC` | Time zone of the event dates. Containers run in UTC, so set this (e.g. `Europe/Berlin`). |
| `BIND_ADDRESS` / `PORT` | `127.0.0.1` / `8000` | Where the app listens on the host. The default only lets a reverse proxy on the same machine in; use `0.0.0.0` to serve the local network directly. |
| `TRUSTED_PROXIES` / `SESSION_COOKIE_SECURE` | `0` / `0` | Set both to `1` behind an HTTPS reverse proxy (see [Deployment notes](#deployment-notes)). |
| `WEB_CONCURRENCY` | `4` | Number of gunicorn worker processes. |

Other variables from [Configuration](#configuration) can be added to the `app` service's
`environment` in `docker-compose.yml` (the scheduler shares it). Mail is set up afterwards on
`/admin/mail`, not in `.env`; test it there or with
`docker compose exec app python -m lanparty.manage test-mail you@example.com`.

- **Data** lives in the `db-data` volume and survives `docker compose down` and image rebuilds
  (`docker compose down -v` deletes it). Back it up with
  `docker compose exec db pg_dump -U lanparty lanparty > backup.sql`.
- **Custom themes** go into `./themes/<id>/` next to the compose file, which is mounted read-only
  as `THEMES_DIR`.
- **Updating:** `docker compose pull && docker compose up -d`. The schema is updated at startup.
  Pin `LANPARTY_VERSION` to a release to update only when you choose to.
- The containers run as an unprivileged user; the app has a health check on `/api/info`, the
  scheduler one on its last run (`python -m lanparty.jobs --check`). The compose
  file also makes its filesystem read-only (except a small `/tmp`), drops all Linux capabilities
  and sets `no-new-privileges`; the database container keeps only the capabilities its entrypoint
  needs.
- **Security updates** of the image come with releases (each release rebuilds it from the
  current base image). To check a running image yourself:
  `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image ghcr.io/its-marvin/lan-party-seat-selection:latest`.

## Continuous integration

GitHub Actions run the tests on every push, on SQLite (Python 3.11 and 3.13) and on PostgreSQL 17
(`.github/workflows/tests.yml`). The same workflow audits the image's Python dependencies
(`requirements-docker.txt`) with `pip-audit`, and runs weekly as well so new vulnerabilities show up
without a push. Dependabot (`.github/dependabot.yml`) opens weekly update PRs for Python
packages, the base image, the PostgreSQL image (no major upgrades) and the actions, which are
pinned to commit SHAs. `.github/workflows/docker.yml` builds the image for pull requests
without publishing it.

A new image is published only when a **GitHub release** is published, and only if its tag points to
a commit on `main`. The workflow runs the tests first. A release `v1.2.3` publishes `:1.2.3`.
Then, one run at a time, all alias tags are brought up to date: every `:X.Y` and `:X` points at
the newest stable release in its line, and `:latest` at the release GitHub marks as latest. A fix
for an older version line (`v1.1.5` after `v1.2.0`) or several releases published at once can't
move an alias backwards or leave one stale. A pre-release publishes only its own tag. Release tags must be
semantic versions (`v1.2.3`). Pushes to `main` publish
nothing.

## Deployment notes

- `python -m lanparty` runs Flask's development server. For a real event, use [Docker](#docker)
  or a WSGI server, e.g.:
  ```bash
  .venv/bin/pip install -r requirements-docker.txt
  .venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 'lanparty:create_app()'
  ```
- **Always serve it over HTTPS** in production, behind a reverse proxy such as Caddy, nginx or Traefik:
  - Set `SESSION_COOKIE_SECURE=1`. It stays `0` by default only because a LAN-only setup often has
    no HTTPS; then the admin cookie travels in plain text over the network.
  - Set `TRUSTED_PROXIES=1`, so the app takes the client IP from `X-Forwarded-For`. Without it,
    every visitor seems to come from the proxy's IP, and the per-IP limits apply to everyone together.
    Only set it when a proxy really is in front of the app; otherwise clients can fake their IP.
  - Let the proxy add `Strict-Transport-Security` and remove the `Server` header.
- Rate limits, sessions and the audit log are stored in the database, so they also work with
  several gunicorn workers. Both databases serialise writes: SQLite with its write lock,
  PostgreSQL with an advisory lock, so seat picks can't overlap.
- Back up the SQLite file (or the PostgreSQL database) to keep your data. SQLite databases from
  older versions of the app are upgraded automatically at startup, and existing participant
  tokens keep working. There is no automatic copy from SQLite to PostgreSQL: a new PostgreSQL
  database starts empty.
- Limitations: the app sends no emails, and PayPal payments are confirmed by hand (there is no
  PayPal API integration).

## License

MIT, see [LICENSE](LICENSE).
