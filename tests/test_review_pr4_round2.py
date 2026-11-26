"""Regression tests for the second Copilot review round of PR #4 (themes)."""

import re
from pathlib import Path

from tests.test_themes import (ADMIN_PW, BUILTIN, TEXT_PAIRS, add_theme, admin, app,  # noqa: F401
                               contrast, theme_palette, themes_dir)


def test_vanished_stored_theme_does_not_block_other_settings(admin, themes_dir):
    add_theme(themes_dir, "gone")
    assert admin.put("/api/admin/settings", json={"theme": "gone"}).status_code == 200
    (themes_dir / "gone" / "theme.css").unlink()
    # the admin form sends the stored (now missing) theme along with every save
    res = admin.put("/api/admin/settings", json={"theme": "gone", "location": "Hall 3"})
    assert res.status_code == 200, res.get_json()
    assert admin.get("/api/admin/settings").get_json()["theme"] == "gone"
    # choosing an unavailable theme anew is still refused
    add_theme(themes_dir, "half", css=':root[data-mode="dark"] {}')
    assert admin.put("/api/admin/settings", json={"theme": "half"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"theme": "nope"}).status_code == 400


# --- unreadable custom themes don't break built-in ones ----------------------------

def test_unreadable_themes_dir_keeps_built_in_themes_working(app, admin, themes_dir):
    add_theme(themes_dir, "neon")
    themes_dir.chmod(0)
    try:
        client = app.test_client()
        assert client.get("/api/info").status_code == 200
        css = client.get("/theme.css")
        assert css.status_code == 200 and b'"default"' in css.data
        assert client.get("/themes/retro/theme.css").status_code == 200
        data = admin.get("/api/admin/themes").get_json()
        assert {"default", "retro"} <= {t["id"] for t in data["themes"]}
        assert any("can't be read" in p["problem"] for p in data["problems"])
    finally:
        themes_dir.chmod(0o755)


def test_theme_folder_without_search_permission_is_only_a_problem(app, admin, themes_dir, monkeypatch):
    add_theme(themes_dir, "locked", meta='{"name": "Locked"}')
    real_is_file = Path.is_file

    def is_file(self, *args, **kwargs):
        # what Python 3.11–3.13 do for a folder without x permission
        if self.parent.name == "locked":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert app.test_client().get("/api/info").status_code == 200
    assert app.test_client().get("/themes/retro/theme.css").status_code == 200
    data = admin.get("/api/admin/themes").get_json()
    assert "locked" not in {t["id"] for t in data["themes"]}
    problem = next(p for p in data["problems"] if p["id"] == "locked")["problem"]
    assert "can't be read" in problem


def test_unlistable_theme_entry_is_only_a_problem(app, admin, themes_dir, monkeypatch):
    add_theme(themes_dir, "neon")
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        if self.name == "neon":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert app.test_client().get("/theme.css").status_code == 200
    data = admin.get("/api/admin/themes").get_json()
    assert {"default", "retro"} <= {t["id"] for t in data["themes"]}
    assert any(p["id"] == "neon" for p in data["problems"])
