"""Translations: server messages (tr) and browser catalogs (static/locales)."""

import ast
import json
from pathlib import Path
import re

import pytest

from lanparty import create_app
from lanparty.i18n import LANGUAGES, catalog, placeholders
from tests.helpers import EVENT_CODE, set_event_code

ROOT = Path(__file__).parent.parent / "lanparty"
STATIC = ROOT / "static"
ADMIN_PW = "s3cret"


@pytest.fixture
def app(tmp_path):
    return set_event_code(create_app({
        "TESTING": True, "DATABASE": str(tmp_path / "test.sqlite3"), "ADMIN_PASSWORD": ADMIN_PW,
    }))


# --- server side -------------------------------------------------------------------

def server_templates() -> set[str]:
    found = set()
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            # tr("…") and N_("…"); tr_in(lang, "…") for emails in a participant's language
            position = {"tr": 0, "N_": 0, "tr_in": 1}.get(node.func.id)
            if (position is not None and len(node.args) > position
                    and isinstance(node.args[position], ast.Constant) and isinstance(node.args[position].value, str)):
                found.add(node.args[position].value)
    return found


def test_server_templates_are_found():
    templates = server_templates()
    assert "Seat {seat} is already taken." in templates and len(templates) > 80


@pytest.mark.parametrize("lang", [lang for lang in LANGUAGES if lang != "en-US"])
def test_every_server_message_is_translated(lang):
    templates = server_templates()
    messages = catalog(lang)
    assert sorted(templates - set(messages)) == [], "missing translations"
    assert sorted(set(messages) - templates) == [], "translations nobody uses"
    wrong = [t for t in templates if placeholders(t) != placeholders(messages[t])]
    assert wrong == [], "placeholders differ"


def test_api_errors_follow_the_requested_language(app):
    client = app.test_client()
    body = {"name": "X", "email": "x@example.com", "event_code": EVENT_CODE}
    assert client.post("/api/register", json=body).get_json()["error"] == \
        "Name must be between 2 and 60 characters."
    assert client.post("/api/register", json=body, headers={"X-Lang": "de-DE"}).get_json()["error"] == \
        "Der Name muss 2 bis 60 Zeichen lang sein."
    assert client.post("/api/register", json=body, headers={"Accept-Language": "de-AT,de;q=0.9"}).get_json()["error"] == \
        "Der Name muss 2 bis 60 Zeichen lang sein."
    # an unknown X-Lang falls back to Accept-Language, then English
    assert client.post("/api/register", json=body, headers={"X-Lang": "xx", "Accept-Language": "fr"}).get_json()["error"] == \
        "Name must be between 2 and 60 characters."


def test_formatted_messages_are_translated(app):
    admin = app.test_client()
    admin.post("/api/admin/login", json={"name": "Test Admin", "password": ADMIN_PW})
    layout = admin.get("/api/admin/layout").get_json()
    layout["groups"][1]["y"] = layout["groups"][0]["y"]
    res = admin.put("/api/admin/layout", json=layout, headers={"X-Lang": "de-DE"})
    assert res.get_json()["error"] == "Die Gruppen A und B überlappen sich."
    res = admin.put("/api/admin/settings", json={"event_name": "x" * 200}, headers={"X-Lang": "de-DE"})
    assert res.get_json()["error"] == "Eventname darf höchstens 120 Zeichen lang sein."
    res = admin.put("/api/admin/layout", json={"area": {"width": 2, "height": 10}, "groups": []},
                    headers={"X-Lang": "de-DE"})
    assert res.get_json()["error"] == "Flächenbreite muss zwischen 4 und 200 liegen."


def test_payment_reference_uses_the_language(app):
    client = app.test_client()
    res = client.post("/api/register", json={"name": "Alice", "email": "a@example.com", "event_code": EVENT_CODE})
    headers = {"X-User-Token": res.get_json()["token"], "X-Lang": "de-DE"}
    data = client.post("/api/me/seat", json={"seat": "A1"}, headers=headers).get_json()
    assert data["payment"]["reference"] == "LAN Party - Platz A1 - Alice"


# --- browser catalogs ---------------------------------------------------------------

def js_catalog(lang: str) -> dict[str, str]:
    text = (STATIC / "locales" / f"{lang}.js").read_text(encoding="utf-8")
    match = re.search(r'I18N\.add\("' + re.escape(lang) + r'", (\{.*\})\);\s*$', text, re.S)
    assert match, f"{lang}.js must call I18N.add with a JSON object"
    return json.loads(match.group(1))


def js_placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


KEY_RE = re.compile(r"^(common|pub|admin|editor|item|itemOption)\.[A-Za-z0-9.]+$")
# keys built at runtime: t(`admin.status.${status}`), describeUnsaved(), ITEM_TYPES[...].key
DYNAMIC_FAMILIES = ("admin.status.", "admin.unsaved.", "item.")


def used_keys() -> set[str]:
    keys = set()
    for path in STATIC.glob("*.js"):
        for literal in re.findall(r'"([^"\\]*)"', path.read_text(encoding="utf-8")):
            if KEY_RE.match(literal):
                keys.add(literal)
    for path in STATIC.glob("*.html"):
        html = path.read_text(encoding="utf-8")
        keys.update(re.findall(r'data-i18n(?:-html)?="([^"]+)"', html))
        for attr in re.findall(r'data-i18n-attr="([^"]+)"', html):
            keys.update(pair.split(":")[1].strip() for pair in attr.split(";"))
    return keys


def test_browser_catalogs_have_the_same_keys_and_placeholders():
    english, german = js_catalog("en-US"), js_catalog("de-DE")
    assert sorted(set(english) ^ set(german)) == []
    wrong = [k for k in english if js_placeholders(english[k]) != js_placeholders(german[k])]
    assert wrong == []
    plural_bases = {k.rsplit(".", 1)[0] for k in english if k.endswith((".one", ".other"))}
    assert all(f"{base}.other" in english for base in plural_bases)


def test_every_used_key_exists_and_every_key_is_used():
    english = js_catalog("en-US")
    resolvable = set(english) | {k.rsplit(".", 1)[0] for k in english if k.endswith((".one", ".other"))}
    used = used_keys()
    assert sorted(k for k in used if k not in resolvable) == [], "keys without a translation"
    unused = [k for k in english
              if k not in used and k.rsplit(".", 1)[0] not in used and not k.startswith(DYNAMIC_FAMILIES)]
    assert unused == [], "translations nobody uses"


def test_dynamic_key_families_are_complete():
    english = js_catalog("en-US")
    assert {f"admin.status.{s}" for s in ("draft", "upcoming", "active", "archived")} <= set(english)
    assert {f"admin.unsaved.{s}" for s in ("layout", "settings", "both")} <= set(english)
    item_types = re.findall(r'key: "(item\.\w+)"', (STATIC / "common.js").read_text())
    assert len(item_types) == 5 and set(item_types) <= set(english)


def test_pages_load_the_translation_scripts_before_the_app(app):
    client = app.test_client()
    pages = (("/", "app.js"), ("/admin", "admin-overview.js"), ("/admin/events", "admin-events.js"),
             ("/admin/settings", "admin-settings.js"), ("/admin/mail", "admin-mail.js"),
             ("/admin/security", "admin-security.js"))
    for page, script in pages:
        html = client.get(page).get_data(as_text=True)
        order = [html.index(name) for name in ("i18n.js", "locales/en-US.js", "locales/de-DE.js", "common.js", script)]
        assert order == sorted(order)
        assert 'id="lang-select"' in html
