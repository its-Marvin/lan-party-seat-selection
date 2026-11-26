# AGENTS.md – notes for coding agents

Handover notes for agents continuing work on this repo. `README.md` covers what the app does for
users and admins. This file covers how the code fits together and which rules must not break.
It also records how the owner likes to work and what was decided along the way.

## What this is

A web app for LAN-party seat registration (GitHub: `its-Marvin/lan-party-seat-selection`).
Participants register with name and email, unlock names on the seat plan with an **event
code**, and pick a seat. A popup then asks them to pay the fee to a PayPal address. Admins
confirm payments, move and remove people, edit the seat layout, and configure the event,
payment and access settings.

Requirements from the owner: **no Java**, tests and README required.

## Stack and commands

- Python 3.11+ (dev machine: 3.14), Flask 3.1, SQLite through stdlib `sqlite3`, plain
  HTML/CSS/JS with no build step and no framework. pytest for tests.
- The venv is at `.venv`. There is no system pip, and Node is not installed.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                                   # ~227 tests, ~3–4 s
ADMIN_PASSWORD='at-least-12-chars' .venv/bin/python -m lanparty   # dev server (HOST/PORT env)
```

- `.vscode/launch.json` has two configurations: "LAN Party: Server starten" and
  "LAN Party: Tests (pytest)". The server one uses `instance/dev.sqlite3`, with
  `ADMIN_PASSWORD=admin` and `ALLOW_WEAK_ADMIN_PASSWORD=1`.
- `instance/dev.sqlite3` holds **real personal data** (it is gitignored). Never commit it,
  print its contents, or send them anywhere. Test against a copy or a fresh temp DB.

## Code map

| Path | Role |
|---|---|
| `lanparty/__init__.py` | `create_app(config)`: reads config from env, adds security headers (strict CSP), JSON error handlers for `/api`, routes `/`, `/admin`, `/favicon.ico` (204) |
| `lanparty/api.py` | Blueprint `/api`: participant, login-link, event-code and admin endpoints; `assign_seat()`; reservation expiry in `before_request` |
| `lanparty/db.py` | Schema, `get_db()`, `write_transaction()` (BEGIN IMMEDIATE), `read_snapshot()` (deferred read txn), `_migrate()` |
| `lanparty/events.py` | Events and their status from dates (`local_now()` in `TIMEZONE`), `active_id()` (cached in `g`), `default_admin_id()`, overlap check, `create()`/`delete()` (no commit) |
| `lanparty/settings.py` | Per-event settings: `DEFAULTS`, `load(event_id)`, `validate_update(data, event_id)`, `save(values, event_id)` (does **not** commit) |
| `lanparty/layout.py` | Seat layout model: validation, `seat_ids`, `plan_seat_moves`, `load(event_id)`, `save(layout, moves, event_id)` (no commit) |
| `lanparty/security.py` | IP hashing (peppered), rate limits `consume()`/`release()`, audit log, server-side admin sessions |
| `lanparty/static/common.js` | `api()`, token and event-code storage, `el()`, `showMessage`, `buildArea()` (shared seat-map renderer, optional `view` crop), `contentBounds()` |
| `lanparty/static/app.js` | Public page: access cards, seat map (fit and crop on phones), payment dialog, login links, 15 s polling |
| `lanparty/static/admin.js` | Admin page: session, participants table, settings, audit log, unsaved-work handling |
| `lanparty/static/layout-editor.js` | Drag-and-drop layout editor (`window.LayoutEditor`) |
| `lanparty/i18n.py` | Server translations: `tr(template, **params)` (English template = key into `lanparty/locales/<lang>.json`), `N_()` marks constants; language from `X-Lang`, else `Accept-Language` |
| `lanparty/static/i18n.js` | Browser translations: `t(key, params)`, `tNodes()` (placeholders as DOM nodes), plural via `key.one/.other`, applies `data-i18n*` attributes on DOMContentLoaded; language switch reloads the page |
| `lanparty/static/locales/*.js` | Browser catalogs (`en-US`, `de-DE`), plain JSON inside `I18N.add(...)` |
| `lanparty/static/theme.js` | Auto/Light/Dark colour mode → `data-mode` on `<html>`, `?theme=<id>` preview; loaded **without defer** in `<head>` |
| `lanparty/themes.py` | Theme discovery (built-in `lanparty/themes/<id>/`, custom `THEMES_DIR`), validation (both `data-mode` blocks required), `get()` |
| `lanparty/themes/` | Built-in themes `default` (empty overrides) and `retro`; `THEMES.md` is the guide for theme authors |
| `tests/` | `conftest.py` (autouse `frozen_event_clock`: tests run at 2026-06-01 12:00), `helpers.py` (`EVENT_CODE`, `set_event_code`, `set_event_setting`), `test_app`, `test_events`, `test_layout`, `test_security`, `test_event_code`, `test_review_fixes`, `test_review_round2` |

## Rules that must not break

### Data and concurrency
- The app runs with **several workers on one SQLite file**. Any read-modify-write must run
  inside `db.write_transaction()`. Code inside it must not call `commit()` (`settings.save` and
  `layout.save` deliberately don't).
- `security.ip_hash()` can write (it creates the pepper on first use). Call it **before**
  opening a write transaction.
- Reads that must agree with each other (for example layout plus occupants in `/api/seats`) go
  in `db.read_snapshot()`. Do anything that writes, such as rate limits or admin session
  refresh, after it.
- Rate limits use `security.consume()`, which checks and records atomically. Call
  `release(event_id)` on success where attempts should only count failures (admin login,
  event code).
- Schema changes go in both `SCHEMA` and `_migrate()`. `_migrate()` takes the write lock
  **before** inspecting columns. Old DBs must keep working (tests cover this).

### Events
- Per-event data lives in `event_settings(event_id, key, value)` (settings **and** the layout under
  key `layout`). The global `settings` table only holds app-wide values such as the IP pepper.
- `users.event_id`: email and seat are unique **per event** (`UNIQUE(event_id, …)`). Every query on
  `users` must filter by event. Participants, tokens and login codes only count for the **active**
  event (`current_user()`, `lookup_login_code()`).
- The status comes from the dates; there is no stored "active" flag: draft (no `registration_opens`),
  upcoming, active, archived (`now >= event_end`). Registration windows `[registration_opens,
  event_end)` must not overlap (checked in `settings.validate_update` inside the write transaction).
- Anything that depends on the status and then writes (seat changes, registration, payments,
  layout saves) re-reads it **inside** `write_transaction()` via `ev.status(cfg.load(id))`. Don't
  rely on the per-request cache there.
- Archived events: participants and layout are read-only (409); settings stay editable so a wrong
  end date can be fixed; removing people is allowed (data deletion).
- Admin endpoints take `?event=<id>` (`admin_event_id()`); the admin UI always sends it.
- Migration: an old single-event DB becomes event 1 with `registration_opens` = migration time.
  The `users` table is rebuilt (with `PRAGMA foreign_keys = OFF` around the transaction).

### Seats and layout
- Seat id = group label + number (`A1`…). Table `t` holds seats `2t+1` and `2t+2`.
- Seat identity across layout edits is resolved through the **original group id**, not the
  label (`plan_seat_moves`, mirrored in `layout-editor.js` `seatTargets()`). Renames use a
  two-pass rename with a `~` temp prefix. A layout save that would drop occupied seats returns 409.
- Area items (door, window, sleeping, food, other) may overlap each other but not groups.
- Unpaid reservations expire after `reservation_hours` (default 48; 0 = never). The window
  counts from the **first** pick and is not reset by switching seats.
- Participants may switch seats **even after paying** (the payment moves with them; this is
  audited as `user.seat_switched`). Only an admin can release a **paid** seat, because that
  means a refund. The per-IP pending limit only counts new unpaid reservations.
- Confirming a payment stores `paid_amount_cents`. "Received" in the admin view sums those
  amounts, not the current fee. Confirming or revoking again when nothing changes is a no-op.

### Access and security
- **Secure by default:** with no event code set, registration is closed (403) and names are
  hidden. With a code (`[A-Z0-9]{4,6}`, normalised to uppercase), names show only to code
  holders (`X-Event-Code` header), participants and admins. Wrong codes are rate-limited on
  every endpoint. `/api/info` never contains the code.
- Admin auth uses a server-side session (`lp_admin` cookie: HttpOnly, SameSite=Strict,
  Path=/api) with 60 min idle and 12 h max. Changing `ADMIN_PASSWORD` invalidates all sessions.
  Passwords under 12 characters are refused unless `ALLOW_WEAK_ADMIN_PASSWORD=1` or TESTING.
- Participants use device tokens (`user_tokens`, stored hashed). One-time login links are
  `/#login=<code>`, kept in the URL fragment so they stay out of server logs. They are valid
  for `LOGIN_CODE_HOURS` (default **168 h = 7 days**, the owner wants at least a week).
- Strict JSON parsing (rejects NaN/Infinity and overflowing numbers), 256 KB body limit,
  `Cache-Control: no-store` on `/api`, `TRUSTED_PROXIES` for ProxyFix.
- Audit everything an admin would want to trace (`security.audit`).

### Translations
- **No user-visible string literals** in JS or HTML. Use `t("key")` / `tNodes()` in scripts and
  `data-i18n` / `data-i18n-html` / `data-i18n-attr` in HTML (wrap label text in a `<span>` so the
  input inside the label survives). Add every key to **both** `static/locales/*.js`.
- Server messages that reach users go through `tr("English template {x}", x=…)` (or `N_()` for
  constants). Add the German text to `lanparty/locales/de-DE.json`. Audit log entries stay English.
- Don't name a local variable `t` where `t()` is needed (it shadows the global translator).
- Dates and money use `window.I18N.lang` (`formatDateTime`, `formatMoney` in `common.js`).
- German uses informal "du". `tests/test_i18n.py` checks completeness, unused keys and placeholders.

### Frontend
- The CSP allows **no inline scripts or inline `style` attributes**. Set styles via CSSOM
  (`node.style.x = …`, `style.setProperty`), and put scripts in separate files.
- `[hidden]` is forced to `display:none !important`. Use `hidden` to toggle panels.
- **Colours come only from CSS custom properties** in `style.css`. There are two blocks: dark
  `:root` and `:root[data-mode="light"]` (`theme.js` always sets `data-mode`). Add every new
  colour token to both, **and to the `retro` theme's two blocks** (`tests/test_themes.py` checks
  this and the 4.5:1 text contrast of every built-in theme; add new text pairs to `TEXT_PAIRS`).
  Fonts and corner radii go through `--font-body`, `--font-mono` and `--radius-scale`.
- Themes: the public page links `/theme.css` (302 to the active event's theme folder). The admin
  page doesn't load event themes. Theme files are served only with allowlisted extensions.
- Public-page polling skips unchanged panels (fingerprints in `rendered`) and keeps focus via
  `data-focus-key` and `keepFocus()`. Keep this when re-rendering.
- Admin page: never show admin data without a session. Unsaved layout or settings changes stay
  in memory across a re-login (`LayoutEditor.isDirty()`, `settingsDirty`). Lock forms or
  snapshot drafts while a save is pending.
- Mobile: no horizontal page scroll down to 320 px. Admin tables use `table.stack` plus
  `labelCells()`.

## How the owner likes to work

- The owner writes in English and sometimes German. Answer in the language they use.
- Commit or push **only when asked**. Branch off `main` first, using `feature/...` names. End
  commit messages with the `Co-Authored-By` trailer the harness specifies.
- PR review loop (GitHub Copilot reviews the PRs):
  1. Fetch the comments (`gh api repos/its-Marvin/lan-party-seat-selection/pulls/<n>/comments`)
     **and the latest review body**. Copilot also lists "previously missed issues" only in the
     review summary, with no inline thread.
  2. Check each finding against the code. Fix it, and add a regression test that fails
     without the fix, where that is possible.
  3. When asked: commit, push, reply to each thread
     (`.../pulls/<n>/comments/<id>/replies`) with the fix and its test, answer summary-only
     findings in one PR comment (`gh pr comment`), then resolve threads through GraphQL
     `resolveReviewThread`.
- Check UI changes in a real browser, not only with unit tests. A Playwright venv lived in the
  session scratchpad, with Chromium cached at `~/.cache/ms-playwright/chromium-1228/`; recreate
  it if it is gone. Test at 1280, 375 and 320 px, in both themes.
- Keep the README up to date with behaviour and config changes.

## Current state (2026-11-26)

- PR #1 (the whole app plus three review rounds) is **merged** into `main`.
- Open PRs form a **stack**; merge them in order (each later PR's base then moves to `main`):
  1. #2 `feature/mobile-theme-self-service` → `main`: mobile layout, paid seat switching,
     Auto/Light/Dark, 7-day login links
  2. #3 `feature/multi-event` → #2: several events (draft/upcoming/active/archived), `TIMEZONE`
  3. #4 `feature/event-themes` → #3: per-event themes, `THEMES_DIR`, retro theme, `THEMES.md`
  4. `feature/i18n` → #4: English/German for both pages and server messages
- Copilot reviews are still pending for all of them.
- The local branch `feature/seat-registration-app` is stale (merged and deleted on the remote)
  and can be deleted.

## Ideas not done yet (only if asked)

- A cut-off for self-service seat switching (for example none after the event starts).
- Email delivery of login links. Currently admins copy the link manually.
- Translating event content (name, description) per language; translating audit log actions.
