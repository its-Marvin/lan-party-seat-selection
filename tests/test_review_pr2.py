"""Regression tests for the Copilot review of PR #2."""

from contextlib import contextmanager
import json
from pathlib import Path
import re

import pytest

from lanparty import api as api_module
from tests.helpers import other_connection
from tests.test_review_fixes import ADMIN_PW, admin, app, register  # noqa: F401

ROOT = Path(__file__).parent.parent


def entries(admin, action):
    return [e for e in admin.get("/api/admin/audit").get_json()["entries"] if e["action"] == action]


def move_right_after_commit(app, monkeypatch, user_id):
    """From now on, right after each write transaction commits, another worker
    moves the person to B8 (a concurrent change the audit entry must not pick up)."""
    real = api_module.write_transaction

    @contextmanager
    def then_concurrent_move():
        with real() as db:
            yield db
        other = other_connection(app)
        other.execute("UPDATE users SET seat = 'B8' WHERE id = ?", (user_id,))
        other.commit()
        other.close()

    monkeypatch.setattr(api_module, "write_transaction", then_concurrent_move)


# --- the seat-change audit entry describes the change of that request -------------

def test_paid_switch_audit_is_not_mixed_up_with_concurrent_changes(app, admin, monkeypatch):
    headers, uid = register(app.test_client(), 1)
    client = app.test_client()
    client.post("/api/me/seat", json={"seat": "A1"}, headers=headers)
    admin.post(f"/api/admin/users/{uid}/paid", json={"paid": True})
    move_right_after_commit(app, monkeypatch, uid)
    assert client.post("/api/me/seat", json={"seat": "A2"}, headers=headers).status_code == 200
    switched = entries(admin, "user.seat_switched")
    assert len(switched) == 1
    assert switched[0]["details"]["seat"] == "A1" and switched[0]["details"]["to"] == "A2"


def test_admin_move_audit_is_not_mixed_up_with_concurrent_changes(app, admin, monkeypatch):
    headers, uid = register(app.test_client(), 1)
    move_right_after_commit(app, monkeypatch, uid)
    res = admin.post(f"/api/admin/users/{uid}/move", json={"seat": "A3"})
    assert res.status_code == 200
    moved = entries(admin, "user.moved")
    assert len(moved) == 1 and moved[0]["details"]["seat"] is None and moved[0]["details"]["to"] == "A3"


def test_seat_audit_is_written_in_the_seat_transaction(app, admin, monkeypatch):
    """If the transaction fails after the change, neither the seat nor the entry is stored."""
    headers, uid = register(app.test_client(), 1)
    real_record = api_module.sec.record_audit

    def record_then_fail(*args, **kwargs):
        real_record(*args, **kwargs)
        raise RuntimeError("worker died")

    monkeypatch.setattr(api_module.sec, "record_audit", record_then_fail)
    assert admin.post(f"/api/admin/users/{uid}/move", json={"seat": "A3"}).status_code == 500
    monkeypatch.undo()
    users = admin.get("/api/admin/users").get_json()["users"]
    assert users[0]["seat"] is None and entries(admin, "user.moved") == []


def test_unchanged_seat_is_not_audited(app, admin):
    headers, uid = register(app.test_client(), 1)
    admin.post(f"/api/admin/users/{uid}/move", json={"seat": "A3"})
    admin.post(f"/api/admin/users/{uid}/move", json={"seat": "A3"})
    admin.post(f"/api/admin/users/{uid}/move", json={"seat": None})
    moved = entries(admin, "user.moved")
    assert [(e["details"]["seat"], e["details"]["to"]) for e in reversed(moved)] == [(None, "A3"), ("A3", None)]


# --- the LAN launch profile must not expose the real data -------------------------

def launch_configs():
    text = (ROOT / ".vscode" / "launch.json").read_text()
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)          # JSONC comments
    text = re.sub(r",(\s*[}\]])", r"\1", text)                 # trailing commas
    return json.loads(text)


def test_lan_profile_uses_its_own_database_and_a_prompted_password():
    data = launch_configs()
    lan = [c for c in data["configurations"] if "0.0.0.0" in c.get("args", [])]
    assert lan, "LAN profile not found"
    for config in lan:
        env = config["env"]
        assert "dev.sqlite3" not in env["DATABASE"]
        assert env["ADMIN_PASSWORD"].startswith("${input:"), "password must be asked for, not stored"
        assert env.get("ALLOW_WEAK_ADMIN_PASSWORD", "0") != "1"
    inputs = {i["id"]: i for i in data.get("inputs", [])}
    for config in lan:
        input_id = config["env"]["ADMIN_PASSWORD"][len("${input:"):-1]
        assert inputs[input_id]["password"] is True


# --- hover state of primary buttons keeps 4.5:1 ------------------------------------

def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_primary_button_hover_keeps_text_contrast():
    css = (ROOT / "lanparty" / "static" / "style.css").read_text()
    # no brightness filter on primary buttons; they use --accent-hover instead
    assert re.search(r"button\.primary:hover[^{]*\{[^}]*--accent-hover", css)
    assert re.search(r"button:hover:not\(:disabled\):not\(\.primary\)", css)
    blocks = re.findall(r"\{([^{}]*--accent-hover[^{}]*)\}", css)
    assert len(blocks) >= 2
    for body in blocks:
        tokens = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", body))
        if "on-accent" in tokens:
            assert contrast(tokens["on-accent"], tokens["accent-hover"]) >= 4.5


# --- stacked tables on phones: no cell keeps nowrap ---------------------------------

def test_stacked_cells_wrap_even_when_marked_nowrap():
    """Audit timestamps use .nowrap; in the 320 px card layout they must wrap anyway."""
    css = (ROOT / "lanparty" / "static" / "style.css").read_text()
    narrow = css[css.index("table.stack thead { display: none; }"):]
    rule = re.search(r"^\s*table\.stack td \{([^}]*)\}", narrow, re.M).group(1)
    assert re.search(r"white-space:\s*normal", rule)
    # the stacked rule wins over .nowrap: same element, higher specificity (0,1,2 vs 0,1,0)
    assert ".nowrap { white-space: nowrap; }" in css


# --- links on the header gradient keep 4.5:1 ----------------------------------------

def css_blocks(css):
    return {" ".join(sel.split()): dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", body))
            for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))}


def test_header_links_are_readable_on_the_gradient():
    css = (ROOT / "lanparty" / "static" / "style.css").read_text()
    assert re.search(r"\.hero a \{[^}]*color:\s*var\(--hero-text\)", css)
    for tokens in css_blocks(css).values():
        if {"hero-text", "hero-from", "hero-to"} <= set(tokens):
            for bg in ("hero-from", "hero-to"):
                assert contrast(tokens["hero-text"], tokens[bg]) >= 4.5, bg
