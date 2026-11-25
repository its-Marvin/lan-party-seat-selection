"""SQLite storage helpers."""

from contextlib import contextmanager
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    email            TEXT NOT NULL UNIQUE,
    token_hash       TEXT NOT NULL UNIQUE,   -- token of the registering device (legacy)
    seat             TEXT UNIQUE,
    paid             INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    seat_selected_at TEXT,
    paid_at          TEXT,
    reserved_at      TEXT,                   -- start of the unpaid reservation window
    reserved_ip_hash TEXT                    -- keyed hash of the IP that reserved the seat
);

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


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _migrate(db: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to date."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "reserved_at" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN reserved_at TEXT")
        db.execute(
            "UPDATE users SET reserved_at = seat_selected_at WHERE seat IS NOT NULL AND paid = 0"
        )
    if "reserved_ip_hash" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN reserved_ip_hash TEXT")
    # every user's original device token keeps working
    db.execute(
        "INSERT OR IGNORE INTO user_tokens (token_hash, user_id, created_at) "
        "SELECT token_hash, id, created_at FROM users"
    )
    db.commit()


def init_db(app) -> None:
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        _migrate(db)
    app.teardown_appcontext(close_db)
