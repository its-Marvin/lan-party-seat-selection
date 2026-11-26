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
