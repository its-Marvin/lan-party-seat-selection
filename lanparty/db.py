"""SQLite storage helpers."""

from contextlib import contextmanager
import sqlite3

from flask import current_app, g

SCHEMA = """
-- one row per event; its settings and layout live in event_settings
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS event_settings (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (event_id, key)
);

-- participants register per event: email and seat are unique within one event
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    email            TEXT NOT NULL,
    token_hash       TEXT NOT NULL UNIQUE,   -- token of the registering device (legacy)
    seat             TEXT,
    paid             INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    seat_selected_at TEXT,
    paid_at          TEXT,
    paid_amount_cents INTEGER,               -- amount confirmed with the payment
    reserved_at      TEXT,                   -- start of the unpaid reservation window
    reserved_ip_hash TEXT,                   -- keyed hash of the IP that reserved the seat
    UNIQUE (event_id, email),
    UNIQUE (event_id, seat)
);

-- global key-value store (e.g. the IP hash pepper); per-event values are in event_settings
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- one row per logged-in participant device
CREATE TABLE IF NOT EXISTS user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- one-time login codes ("log in on another device" links)
CREATE TABLE IF NOT EXISTS login_codes (
    code_hash  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash       TEXT PRIMARY KEY,
    actor            TEXT NOT NULL,
    password_tag     TEXT NOT NULL,          -- invalidates sessions when ADMIN_PASSWORD changes
    created_at       REAL NOT NULL,
    last_seen        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL DEFAULT (datetime('now')),
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rate_events (
    bucket TEXT NOT NULL,
    key    TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_lookup ON rate_events (bucket, key, ts);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@contextmanager
def write_transaction():
    """Run a read-modify-write sequence atomically across worker processes.

    ``BEGIN IMMEDIATE`` takes SQLite's write lock up front, so everything read
    inside the block is still current when the block commits. Code inside must
    not call ``commit()`` itself.
    """
    db = get_db()
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    db.commit()


@contextmanager
def read_snapshot():
    """Run several reads against one consistent database state.

    The deferred transaction takes SQLite's shared lock at the first read and
    keeps it until the block ends, so no writer can commit in between. Keep the
    block short and free of writes.
    """
    db = get_db()
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN")
    try:
        yield db
    finally:
        db.commit()


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _migrate(db: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to date.

    Several workers may start at once: the write lock is taken before the
    schema is inspected, so a worker that waited sees the migrated schema.
    """
    from .settings import DEFAULTS

    if db.in_transaction:
        db.commit()
    # rebuilding the users table must not cascade into user_tokens/login_codes;
    # this pragma cannot change inside a transaction
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "reserved_at" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN reserved_at TEXT")
            db.execute(
                "UPDATE users SET reserved_at = seat_selected_at WHERE seat IS NOT NULL AND paid = 0"
            )
        if "reserved_ip_hash" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN reserved_ip_hash TEXT")
        if "paid_amount_cents" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN paid_amount_cents INTEGER")
            # best guess for payments confirmed before amounts were stored
            db.execute(
                "UPDATE users SET paid_amount_cents = COALESCE("
                "  (SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'amount_cents'), ?) "
                "WHERE paid = 1",
                (int(DEFAULTS["amount_cents"]),),
            )
        # every user's original device token keeps working
        db.execute(
            "INSERT OR IGNORE INTO user_tokens (token_hash, user_id, created_at) "
            "SELECT token_hash, id, created_at FROM users"
        )
        _migrate_to_events(db, DEFAULTS, columns)
        problems = db.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Migration left broken references: {problems[:5]}")
    except BaseException:
        db.rollback()
        db.execute("PRAGMA foreign_keys = ON")
        raise
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")


PER_EVENT_KEYS = ("layout",)  # besides settings.DEFAULTS


def _migrated_opening(db: sqlite3.Connection, event_id: int) -> str:
    """When registration "opened" for a migrated event.

    Registration has been open so far, so normally that is now and the event
    stays open. An event that has already ended is archived instead, with a
    valid window: registration opens at its start (or a day before its end).
    """
    from datetime import datetime, timedelta

    from .events import local_now

    def stored(key):
        row = db.execute("SELECT value FROM event_settings WHERE event_id = ? AND key = ?",
                         (event_id, key)).fetchone()
        try:
            return datetime.fromisoformat(row["value"]) if row and row["value"] else None
        except ValueError:
            return None

    now = local_now().replace(second=0)
    end = stored("event_end")
    if end is None or end > now:
        return now.strftime("%Y-%m-%dT%H:%M")
    start = stored("event_start")
    opens = start if start is not None and start < end else end - timedelta(days=1)
    return opens.strftime("%Y-%m-%dT%H:%M")


def _migrate_to_events(db: sqlite3.Connection, defaults: dict, user_columns: set) -> None:
    """Single-event databases: everything becomes event 1, which stays open.

    A fresh database gets its first event the same way, so the app works
    right after installation (registration still needs an event code).
    """
    if db.execute("SELECT 1 FROM events LIMIT 1").fetchone() is None:
        cur = db.execute("INSERT INTO events DEFAULT VALUES")
        event_id = cur.lastrowid
        keys = [*defaults, *PER_EVENT_KEYS]
        marks = ",".join("?" * len(keys))
        db.execute(
            f"INSERT INTO event_settings (event_id, key, value) "
            f"SELECT ?, key, value FROM settings WHERE key IN ({marks})",
            (event_id, *keys),
        )
        db.execute(f"DELETE FROM settings WHERE key IN ({marks})", keys)
        db.execute(
            "INSERT OR IGNORE INTO event_settings (event_id, key, value) VALUES (?, 'registration_opens', ?)",
            (event_id, _migrated_opening(db, event_id)),
        )
    if "event_id" not in user_columns:
        first = db.execute("SELECT MIN(id) FROM events").fetchone()[0]
        # SQLite cannot change UNIQUE constraints in place: rebuild the table
        create = SCHEMA.split("CREATE TABLE IF NOT EXISTS users", 1)[1].split(";", 1)[0]
        db.execute("CREATE TABLE users_new" + create)
        copied = ("id, name, email, token_hash, seat, paid, created_at, seat_selected_at, "
                  "paid_at, paid_amount_cents, reserved_at, reserved_ip_hash")
        db.execute(f"INSERT INTO users_new (event_id, {copied}) SELECT ?, {copied} FROM users", (first,))
        db.execute("DROP TABLE users")
        db.execute("ALTER TABLE users_new RENAME TO users")


def init_db(app) -> None:
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        _migrate(db)
    app.teardown_appcontext(close_db)
