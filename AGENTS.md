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

- Python 3.11+ (dev machine: 3.14), Flask 3.1, plain HTML/CSS/JS with no build step and no
  framework. pytest for tests.
- Database: SQLite through stdlib `sqlite3` (default, dev and tests) **or** PostgreSQL through
  psycopg 3 when `DATABASE_URL` is set (Docker deployment). Same queries for both, see below.
  A `DATABASE` passed in `create_app(config)` without `DATABASE_URL` clears an inherited
  `DATABASE_URL`, so tests can never write to a real database from the shell environment.
- Docker: `Dockerfile` (python:3.13-slim, gunicorn 27 with `--no-control-socket`, non-root) and
  `docker-compose.yml` (pulls `ghcr.io/its-marvin/lan-party-seat-selection:${LANPARTY_VERSION}`
  + postgres:17-alpine, secrets from `.env`, see `.env.example`). `docker-compose.build.yml` is the
  override that builds from the checkout instead.
- CI: `.github/workflows/tests.yml` (every branch push; SQLite on 3.11/3.13 and Postgres 17) and
  `docker.yml` (PRs: build only; a **published release** whose tag is on `main`: checks that,
  calls tests.yml, then pushes a multi-arch image `:X.Y.Z` to GHCR. A separate `aliases` job in
  the global `docker-aliases` concurrency group runs `.github/scripts/update-image-aliases.sh`:
  every `:X`/`:X.Y` = newest stable release in that line, `:latest` = GitHub's
  `releases/latest`. Each run reconciles **all** lines from the full release list, because GitHub
  cancels older *pending* jobs in a concurrency group even with `cancel-in-progress: false`. `tests/test_ci.py` runs the script with stub
  `gh`/`docker`.
  The owner wants images published **only on releases**, never on pushes to main. Keep actions
  on their Node 24 majors (Node 20 actions trigger deprecation warnings). Lint workflows with
  `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest`. The repo is public; after
  the first publish, check that the GHCR package is public too (new packages can start private).
- The venv is at `.venv`. There is no system pip, and Node is not installed.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                                   # ~305 tests, ~5 s
# the same suite on PostgreSQL (~30 s; a schema per test, sqlite_only tests skipped):
docker run -d --rm --name lanparty-pg-test -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine
LANPARTY_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/pytest -q
ADMIN_PASSWORD='at-least-12-chars' .venv/bin/python -m lanparty   # dev server (HOST/PORT env)
```

- `.vscode/launch.json` has three configurations. "LAN Party: Server starten" (127.0.0.1 only)
  uses `instance/dev.sqlite3` with `ADMIN_PASSWORD=admin` and `ALLOW_WEAK_ADMIN_PASSWORD=1`.
  "Server im lokalen Netz" listens on 0.0.0.0 for phone tests: it must use its own throwaway DB
  (`instance/lan-test.sqlite3`) and a prompted password (`${input:…}`), never the real data or
  a known password (`tests/test_review_pr2.py` checks this). Plus "LAN Party: Tests (pytest)".
- `instance/dev.sqlite3` holds **real personal data** (it is gitignored). Never commit it,
  print its contents, or send them anywhere. Test against a copy or a fresh temp DB.

## Code map

| Path | Role |
|---|---|
| `lanparty/__init__.py` | `create_app(config)`: reads config from env, adds security headers (strict CSP), JSON error handlers for `/api`, routes `/`, `/admin`, `/favicon.ico` (204) |
| `lanparty/api.py` | Blueprint `/api`: participant, login-link, event-code and admin endpoints; `assign_seat()`; reservation expiry in `before_request` |
| `lanparty/db.py` | `SCHEMA` (SQLite) and `PG_SCHEMA`, `SqliteConnection` / `PgConnection` (sqlite3-style adapter over psycopg), `get_db()`, `write_transaction()`, `read_snapshot()`, `utc_now()`, `IntegrityError`, `_migrate()` |
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
| `tests/` | `conftest.py` (autouse `frozen_event_clock`: tests run at 2026-06-01 12:00), `helpers.py` (`EVENT_CODE`, `set_event_code`, `set_event_setting`), `test_app`, `test_events`, `test_layout`, `test_security`, `test_event_code`, `test_review_fixes`, `test_review_round2`, `test_themes`, `test_i18n`, `test_database` (adapter, schema parity, portable SQL, Docker files), `test_review_pr2`…`test_review_pr4_round2` (regression tests per Copilot review) |

## Rules that must not break

### Data and concurrency
- **SQL must run on SQLite and PostgreSQL.** Write it SQLite-style (`?` placeholders;
  `PgConnection` translates them) but portable: timestamps come from `utc_now(hours)` as a
  parameter (no `datetime('now')`), `ON CONFLICT DO NOTHING` (no `INSERT OR IGNORE`; after an
  `INSERT … SELECT` SQLite needs `WHERE true`), `db.insert(sql)` for new ids (no `lastrowid`),
  `db.rowid` for `rate_events`, `CASE WHEN ? = 1` (Postgres wants booleans there), catch
  `db.IntegrityError`. `test_database.py::test_app_sql_is_portable` checks the obvious cases;
  run the suite on Postgres after SQL changes.
- Postgres text columns use `COLLATE "C"`, so timestamp text compares and `ORDER BY` match SQLite.
  Use `DOUBLE PRECISION`, never `REAL` (float4).
- `PgConnection` mimics `sqlite3`: autocommit, and a write statement outside a transaction opens
  one **and takes the global advisory lock** until `commit()`. `write_transaction()` = `BEGIN` +
  `pg_advisory_xact_lock`; `read_snapshot()` = `REPEATABLE READ` (a concurrent writer is not
  blocked but stays invisible). In Postgres, an error aborts the transaction: don't keep using it
  after a failed statement.
- The app runs with **several workers on one database**. Any read-modify-write must run
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
- Schema changes go in `SCHEMA`, `PG_SCHEMA` **and** `_migrate()` (`db.columns()` works for both;
  `test_sqlite_and_postgres_schemas_have_the_same_columns` checks parity). `_migrate()` takes the
  write lock **before** inspecting columns (Postgres also creates its tables under it). Old DBs
  must keep working (tests cover this). The legacy SQLite rebuild steps never run on Postgres.

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
- Admin UI: the settings form and the layout draft are bound to the event they were loaded from
  (`settingsEventId`, `LayoutEditor.eventId()`), and saves go there, never to `currentEvent`.
  While another event loads, the form and layout saving are locked; a failed load keeps them
  locked. An event deleted elsewhere triggers a normal (unsaved-work-aware) switch, once.
- Background polls send `X-Background: 1` (`api(…, {background: true})`); `admin_required` then
  doesn't refresh the session, so the idle timeout still works with an open tab.
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
- Admins must give a name at login (400 otherwise, checked before the rate limit so it costs no
  attempt); it is the `actor` in the audit log. There is no `"admin"` fallback.
- Admin auth uses a server-side session (`lp_admin` cookie: HttpOnly, SameSite=Strict,
  Path=/api) with 60 min idle and 12 h max. Changing `ADMIN_PASSWORD` invalidates all sessions.
  Passwords under 12 characters are refused unless `ALLOW_WEAK_ADMIN_PASSWORD=1` or TESTING.
- Participants use device tokens (`user_tokens`, stored hashed). One-time login links are
  `/#login=<code>`, kept in the URL fragment so they stay out of server logs. They are valid
  for `LOGIN_CODE_HOURS` (default **168 h = 7 days**, the owner wants at least a week).
- Strict JSON parsing (rejects NaN/Infinity, overflowing numbers and NUL characters anywhere,
  which PostgreSQL can't store), 256 KB body limit,
  `Cache-Control: no-store` on `/api`, `TRUSTED_PROXIES` for ProxyFix.
- Audit everything an admin would want to trace (`security.audit`). Inside a write transaction
  use `security.record_audit()` (no commit), so the entry describes exactly the change made under
  that lock; `assign_seat()` audits seat moves and paid switches that way.

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
  Hover states count too: primary buttons use `--accent-hover`, not the brightness filter that
  other buttons get (it pushed white-on-accent below 4.5:1 in light mode).
- Themes: the public page links `/theme.css` (`@import` of the active event's theme plus
  `--lp-theme: "<id>"`, which `app.js` reads to know what it got). The admin
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

- Everything is merged into `main`: #1 (the app), #2 (mobile layout, paid seat switching,
  Auto/Light/Dark, 7-day login links), #3 (several events, `TIMEZONE`), #4 (per-event themes,
  `THEMES_DIR`, retro theme, `THEMES.md`) and #5 (English/German). After the stack went wrong
  (see below), #3 and #4 reached `main` through #7, and #5 through #8. All Copilot review
  threads are resolved.
- No feature branches are left; `main` is the only branch.
- Stacked PRs, lessons learned: merge them bottom-up, and delete each head branch right after
  its merge, so GitHub moves the next PR's base to `main`. A PR merged into an already-merged
  base never reaches `main`. PRs in a GitHub *stack* can't have their base changed, and
  `gh pr merge` / the normal merge API refuse them; use
  `PUT /repos/{owner}/{repo}/pulls/{n}/merge-async` (with `sha` pinned) or the stack view on GitHub.

## Ideas not done yet (only if asked)

- A tool to copy an existing SQLite database into PostgreSQL. New Postgres databases start empty.
- A cut-off for self-service seat switching (for example none after the event starts).
- Email delivery of login links. Currently admins copy the link manually.
- Translating event content (name, description) per language; translating audit log actions.
