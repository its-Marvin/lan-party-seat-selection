"""Regression tests for the Copilot review of PR #4 (themes)."""

from pathlib import Path

from lanparty import create_app
from tests.helpers import set_event_code
from tests.test_themes import ADMIN_PW, VALID_CSS, add_theme, admin, app, served_theme, themes_dir  # noqa: F401


def test_relative_themes_dir_is_listed_and_served_from_the_same_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mythemes" / "neon").mkdir(parents=True)
    (tmp_path / "mythemes" / "neon" / "theme.css").write_text(VALID_CSS)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite3"),
                      "ADMIN_PASSWORD": ADMIN_PW, "THEMES_DIR": "mythemes"})
    assert Path(app.config["THEMES_DIR"]).is_absolute()
    admin = app.test_client()
    admin.post("/api/admin/login", json={"password": ADMIN_PW})
    assert "neon" in [t["id"] for t in admin.get("/api/admin/themes").get_json()["themes"]]
    res = app.test_client().get("/themes/neon/theme.css")
    assert res.status_code == 200 and b"data-mode" in res.data


def test_commented_out_mode_does_not_count(admin, themes_dir):
    add_theme(themes_dir, "half", css=':root[data-mode="dark"] { --bg: #000; }\n'
                                      '/* :root[data-mode="light"] { --bg: #fff; } */\n')
    data = admin.get("/api/admin/themes").get_json()
    assert "half" not in [t["id"] for t in data["themes"]]
    assert any(p["id"] == "half" and 'data-mode="light"' in p["problem"] for p in data["problems"])
    assert admin.put("/api/admin/settings", json={"theme": "half"}).status_code == 400


def test_unreadable_theme_css_only_disables_that_theme(app, admin, themes_dir, monkeypatch):
    add_theme(themes_dir, "locked")
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.parent.name == "locked" and self.name == "theme.css":
            raise PermissionError(13, "Permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    data = admin.get("/api/admin/themes")
    assert data.status_code == 200
    problems = {p["id"]: p["problem"] for p in data.get_json()["problems"]}
    assert "can't be read" in problems["locked"]
    client = app.test_client()
    assert client.get("/theme.css").status_code == 200
    assert client.get("/themes/retro/theme.css").status_code == 200
    assert client.get("/themes/locked/theme.css").status_code == 404


def test_info_reports_the_theme_open_pages_should_show(app, admin, themes_dir):
    client = app.test_client()
    assert client.get("/api/info").get_json()["theme"] == "default"
    admin.put("/api/admin/settings", json={"theme": "retro"})
    assert client.get("/api/info").get_json()["theme"] == "retro"
    add_theme(themes_dir, "gone")
    admin.put("/api/admin/settings", json={"theme": "gone"})
    (themes_dir / "gone" / "theme.css").unlink()
    # same fallback as /theme.css
    assert client.get("/api/info").get_json()["theme"] == "default"
    assert served_theme(client) == "default"
