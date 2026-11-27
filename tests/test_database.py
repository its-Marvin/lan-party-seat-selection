"""The two database backends (SQLite, PostgreSQL) and the Docker deployment.

These checks run without a PostgreSQL server; the whole suite runs against one
with LANPARTY_TEST_DATABASE_URL (see conftest.py)."""

from pathlib import Path
import re
import sqlite3

import pytest

from lanparty import db as dbmod

ROOT = Path(__file__).parent.parent
# the real one: in PostgreSQL test runs, conftest redirects dbmod.database_target
database_target = dbmod.database_target


# --- PostgreSQL adapter -----------------------------------------------------------

@pytest.mark.parametrize("sql, expected", [
    ("SELECT * FROM users WHERE id = ?", "SELECT * FROM users WHERE id = %s"),
    ("UPDATE t SET a = ?, b = ? WHERE c = ?", "UPDATE t SET a = %s, b = %s WHERE c = %s"),
    # literal % must not be read as a placeholder by psycopg
    ("SELECT 10 % 3, ?", "SELECT 10 %% 3, %s"),
    # quoted text stays as it is
    # quoted ? stays text; psycopg reads % even inside quotes, so it is doubled there too
    ("SELECT '?', \"a?b\", ? FROM t WHERE x = 'it''s ? %'", "SELECT '?', \"a?b\", %s FROM t WHERE x = 'it''s ? %%'"),
    ("SELECT '100%', ?", "SELECT '100%%', %s"),
])
def test_placeholders_are_translated_for_psycopg(sql, expected):
    assert dbmod.pg_sql(sql) == expected


def test_pg_rows_behave_like_sqlite_rows():
    row = dbmod.PgRow({"id": 0, "name": 1}, (7, "Ada"))
    assert row["name"] == "Ada" and row[0] == 7 and row[-1] == "Ada"
    assert dict(row) == {"id": 7, "name": "Ada"}
    assert list(row) == [7, "Ada"] and len(row) == 2
    assert dbmod.PgRow({"id": 0}, (1,)).keys() == ["id"]


def test_postgres_is_chosen_by_url():
    assert dbmod.is_postgres("postgresql://u@db/lanparty")
    assert dbmod.is_postgres("postgres://u@db/lanparty")
    assert not dbmod.is_postgres("/data/lanparty.sqlite3")
    assert database_target({"DATABASE": "x.sqlite3", "DATABASE_URL": ""}) == "x.sqlite3"
    assert database_target({"DATABASE": "x.sqlite3", "DATABASE_URL": "postgresql://db/x"}) == "postgresql://db/x"


def test_utc_now_matches_sqlite_datetime_format():
    sqlite_now = sqlite3.connect(":memory:").execute("SELECT datetime('now')").fetchone()[0]
    ours = dbmod.utc_now()
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", ours)
    assert abs(int(ours[-2:]) - int(sqlite_now[-2:])) in (0, 1, 59)  # same second, give or take
    assert dbmod.utc_now(-49) < ours < dbmod.utc_now(1)


# --- both schemas describe the same tables ----------------------------------------

def tables(schema: str) -> dict[str, set[str]]:
    out = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", schema, re.S):
        out[name] = {line.split()[0] for line in body.strip().splitlines()
                     if line.strip() and not line.strip().startswith(("--", "UNIQUE", "PRIMARY KEY"))}
    return out


def test_sqlite_and_postgres_schemas_have_the_same_columns():
    sqlite, pg = tables(dbmod.SCHEMA), tables(dbmod.PG_SCHEMA)
    assert sqlite.keys() == pg.keys() and len(sqlite) == 9
    # SQLite addresses rate_events by its implicit rowid, PostgreSQL by an id column
    pg["rate_events"].discard("id")
    assert sqlite == pg


def test_postgres_compares_text_like_sqlite():
    """Timestamps are compared as text; the "C" collation keeps that byte-wise."""
    for line in dbmod.PG_SCHEMA.splitlines():
        if re.search(r"\bTEXT\b", line):
            assert 'TEXT COLLATE "C"' in line, line
    assert "REAL" not in dbmod.PG_SCHEMA  # float4 would round time.time() to minutes


def test_app_sql_is_portable():
    """SQLite-only SQL would only fail on PostgreSQL, so keep it out of the app."""
    forbidden = re.compile(r"datetime\('now|OR IGNORE|OR REPLACE|lastrowid|PRAGMA|(?<!\.)\browid\b|julianday")
    for path in (ROOT / "lanparty").glob("*.py"):
        if path.name == "db.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            assert not forbidden.search(line), f"{path.name}:{number}: {line.strip()}"


# --- Docker deployment --------------------------------------------------------------

def test_compose_requires_secrets_and_keeps_the_database_private():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "${ADMIN_PASSWORD:?" in compose and "${POSTGRES_PASSWORD:?" in compose
    db_service = compose.split("\n  db:\n", 1)[1].split("\n  app:\n", 1)[0]
    assert "ports:" not in db_service  # PostgreSQL is reachable only inside the compose network
    # the app is published on localhost unless BIND_ADDRESS says otherwise
    assert '"${BIND_ADDRESS:-127.0.0.1}:' in compose
    # the password is passed separately, never inside the URL
    url = re.search(r"DATABASE_URL: (\S+)", compose).group(1)
    assert url.startswith("postgresql://") and ":" not in url.split("//", 1)[1].split("@", 1)[0]
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^ADMIN_PASSWORD=$", example, re.M) and re.search(r"^POSTGRES_PASSWORD=$", example, re.M)


def test_compose_pulls_the_published_image_and_the_override_builds_it():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "build:" not in compose
    assert "image: ghcr.io/its-marvin/lan-party-seat-selection:${LANPARTY_VERSION:-latest}" in compose
    override = (ROOT / "docker-compose.build.yml").read_text()
    assert "build: ." in override and "image: lanparty-seat-selection:local" in override
    # the workflow publishes to the image the compose file pulls
    workflow = (ROOT / ".github/workflows/docker.yml").read_text()
    assert "ghcr.io/${GITHUB_REPOSITORY,,}" in workflow
    assert "needs: tests" in workflow and "push: ${{ github.event_name == 'push' }}" in workflow


def test_image_runs_unprivileged_and_leaves_private_files_out():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^USER (?!root)\w+", dockerfile, re.M)
    ignored = (ROOT / ".dockerignore").read_text().split()
    for private in ("instance", ".env", "*.sqlite3", ".venv", ".git"):
        assert private in ignored
    # only the top-level themes folder is left out, not the built-in lanparty/themes
    assert "themes" in ignored and "**/themes" not in ignored
    assert ".env" in (ROOT / ".gitignore").read_text().split()


# --- isolation and input PostgreSQL can't store -------------------------------------

def test_inherited_database_url_does_not_override_an_explicit_sqlite_path(tmp_path, monkeypatch):
    """A DATABASE_URL in the shell must never send a test's writes to a real database."""
    from lanparty import create_app

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@nowhere.invalid/real-data")
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite3")})
    assert app.config["DATABASE_URL"] == ""
    assert database_target(app.config) == str(tmp_path / "t.sqlite3")


@pytest.fixture()
def coded_app(tmp_path):
    from lanparty import create_app
    from tests.helpers import set_event_code

    return set_event_code(create_app({"TESTING": True, "DATABASE": str(tmp_path / "nul.sqlite3"),
                                      "ADMIN_PASSWORD": "admin-password-123"}))


@pytest.mark.parametrize("field", ["name", "email"])
def test_registration_rejects_nul_characters(coded_app, field):
    from tests.helpers import EVENT_CODE

    data = {"name": "Ada Lovelace", "email": "ada@example.com", "event_code": EVENT_CODE}
    data[field] = data[field][:3] + "\x00" + data[field][3:]
    resp = coded_app.test_client().post("/api/register", json=data)
    assert resp.status_code == 400 and "NUL" in resp.get_json()["error"]


def test_admin_input_rejects_nested_nul_characters(coded_app):
    admin = coded_app.test_client()
    assert admin.post("/api/admin/login", json={"password": "admin-password-123"}).status_code == 200
    assert admin.put("/api/admin/settings", json={"event_name": "LAN\x00"}).status_code == 400
    layout = admin.get("/api/admin/layout").get_json()
    layout["groups"][0]["label"] = "\x00"
    assert admin.put("/api/admin/layout", json=layout).status_code == 400
    assert admin.put("/api/admin/settings", json={"event_name": "LAN"}).status_code == 200
