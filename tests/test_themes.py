"""Event themes: discovery, validation, serving, per-event choice, colour contrast."""

from pathlib import Path
import re

import pytest

from lanparty import create_app
from lanparty import themes as th
from tests.helpers import set_event_code

ADMIN_PW = "s3cret"
STATIC = Path(__file__).parent.parent / "lanparty" / "static"
BUILTIN = Path(__file__).parent.parent / "lanparty" / "themes"

VALID_CSS = ':root[data-mode="dark"] { --bg: #000000; }\n:root[data-mode="light"] { --bg: #ffffff; }\n'


@pytest.fixture
def themes_dir(tmp_path):
    path = tmp_path / "themes"
    path.mkdir()
    return path


@pytest.fixture
def app(tmp_path, themes_dir):
    return set_event_code(create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "ADMIN_PASSWORD": ADMIN_PW,
        "THEMES_DIR": str(themes_dir),
    }))


@pytest.fixture
def admin(app):
    c = app.test_client()
    assert c.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    return c


def add_theme(themes_dir, name, css=VALID_CSS, meta=None):
    folder = themes_dir / name
    folder.mkdir()
    if css is not None:
        (folder / "theme.css").write_text(css)
    if meta is not None:
        (folder / "theme.json").write_text(meta)
    return folder


# --- discovery and validation ---------------------------------------------------

def test_builtin_themes_are_listed(admin):
    data = admin.get("/api/admin/themes").get_json()
    ids = [t["id"] for t in data["themes"]]
    assert ids[:2] == ["default", "retro"] and data["problems"] == []
    retro = next(t for t in data["themes"] if t["id"] == "retro")
    assert retro["name"] == "Retro terminal" and retro["builtin"] is True


def test_custom_themes_are_validated(admin, themes_dir):
    add_theme(themes_dir, "neon", meta='{"name": "Neon", "description": "Bright"}')
    add_theme(themes_dir, "half", css=':root[data-mode="dark"] { --bg: #000; }')
    add_theme(themes_dir, "single-quotes", css=":root[data-mode='dark']{} :root[data-mode=light]{}")
    add_theme(themes_dir, "nocss", css=None)
    add_theme(themes_dir, "badmeta", meta="{not json")
    add_theme(themes_dir, "Upper Case")
    add_theme(themes_dir, "retro")                      # clashes with the built-in theme
    data = admin.get("/api/admin/themes").get_json()
    custom = {t["id"]: t for t in data["themes"] if not t["builtin"]}
    assert set(custom) == {"neon", "single-quotes"}
    assert custom["neon"]["name"] == "Neon" and custom["neon"]["description"] == "Bright"
    problems = {p["id"]: p["problem"] for p in data["problems"]}
    assert 'data-mode="light"' in problems["half"]
    assert "theme.css is missing" in problems["nocss"]
    assert "theme.json" in problems["badmeta"]
    assert "lowercase" in problems["Upper Case"]
    assert "built-in" in problems["retro"]
    # the built-in retro theme is still there
    assert any(t["id"] == "retro" and t["builtin"] for t in data["themes"])


def test_themes_endpoint_needs_admin(app):
    assert app.test_client().get("/api/admin/themes").status_code == 401


def served_theme(client):
    """Theme id /theme.css imports; it must also announce it as --lp-theme."""
    css = client.get("/theme.css").get_data(as_text=True)
    imported = re.fullmatch(r'@import url\("/themes/([a-z0-9_-]+)/theme\.css"\);\n:root \{ --lp-theme: "([a-z0-9_-]+)"; \}\n', css)
    assert imported and imported.group(1) == imported.group(2), css
    return imported.group(1)


# --- choosing a theme per event ---------------------------------------------------

def test_theme_is_a_per_event_setting(admin, themes_dir):
    assert admin.get("/api/admin/settings").get_json()["theme"] == "default"
    assert admin.put("/api/admin/settings", json={"theme": "retro"}).status_code == 200
    assert admin.get("/api/admin/settings").get_json()["theme"] == "retro"
    for bad in ("nope", "../retro", "", None, 5):
        assert admin.put("/api/admin/settings", json={"theme": bad}).status_code == 400
    add_theme(themes_dir, "half", css=':root[data-mode="dark"] {}')
    assert admin.put("/api/admin/settings", json={"theme": "half"}).status_code == 400
    # a new event takes over the theme
    first = admin.get("/api/admin/events").get_json()["events"][0]["id"]
    new = admin.post("/api/admin/events", json={"copy_from": first}).get_json()["event"]["id"]
    assert admin.get(f"/api/admin/settings?event={new}").get_json()["theme"] == "retro"


def test_theme_css_follows_the_active_event(app, admin, themes_dir):
    client = app.test_client()
    res = client.get("/theme.css")
    assert res.status_code == 200 and res.mimetype == "text/css"
    assert served_theme(client) == "default"
    assert res.headers["Cache-Control"] == "no-store"
    admin.put("/api/admin/settings", json={"theme": "retro"})
    assert served_theme(client) == "retro"
    # a theme that disappears later falls back to the default one
    add_theme(themes_dir, "gone")
    admin.put("/api/admin/settings", json={"theme": "gone"})
    (themes_dir / "gone" / "theme.css").unlink()
    assert served_theme(client) == "default"


def test_theme_files_are_served_safely(app, themes_dir):
    client = app.test_client()
    res = client.get("/themes/retro/theme.css")
    assert res.status_code == 200 and res.mimetype == "text/css" and b"data-mode" in res.data
    assert res.headers["Cache-Control"] == "no-cache"
    folder = add_theme(themes_dir, "assets")
    (folder / "font.woff2").write_bytes(b"wOF2")
    (folder / "notes.txt").write_text("secret")
    (folder / "script.js").write_text("alert(1)")
    assert client.get("/themes/assets/font.woff2").mimetype == "font/woff2"
    assert client.get("/themes/assets/notes.txt").status_code == 404     # not a theme asset type
    assert client.get("/themes/assets/script.js").status_code == 404
    assert client.get("/themes/assets/../retro/theme.css").status_code == 404
    assert client.get("/themes/retro/..%2f..%2f__init__.py").status_code == 404
    assert client.get("/themes/nope/theme.css").status_code == 404
    assert client.get("/themes/assets/missing.css").status_code == 404


def test_public_page_loads_the_event_theme_and_admin_page_does_not(app):
    client = app.test_client()
    assert b'href="/theme.css"' in client.get("/").data
    assert b"/theme.css" not in client.get("/admin").data


# --- colours: every built-in theme, both modes ------------------------------------

# (foreground, background) pairs that carry text; both must reach 4.5:1
TEXT_PAIRS = [
    ("text", "bg"), ("text", "card"), ("text", "input-bg"), ("text", "button-bg"), ("text", "free"),
    ("muted", "card"), ("muted", "bg"), ("accent", "card"), ("on-accent", "accent"),
    ("on-accent", "accent-hover"),
    ("on-reserved", "reserved"), ("on-paid", "paid"), ("on-mine", "mine"),
    ("danger-text", "card"), ("success-text", "card"), ("warning-text", "card"),
    ("hero-text", "hero-from"), ("hero-text", "hero-to"),
]


def blocks(css: str) -> dict[str, dict[str, str]]:
    """Custom properties per selector block (only the top-level blocks we care about)."""
    out = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        props = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", body))
        out.setdefault(" ".join(selector.split()), {}).update(props)
    return out


def base_palette(mode: str) -> dict[str, str]:
    b = blocks((STATIC / "style.css").read_text())
    dark = b[":root"]
    return dark if mode == "dark" else {**dark, **b[':root[data-mode="light"]']}


def theme_palette(theme: str, mode: str) -> dict[str, str]:
    b = blocks((BUILTIN / theme / "theme.css").read_text())
    return {**base_palette(mode), **b.get(f':root[data-mode="{mode}"]', {})}


def luminance(hex_colour: str) -> float:
    h = hex_colour.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("theme", sorted(p.name for p in BUILTIN.iterdir() if p.is_dir()))
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_builtin_themes_have_readable_text(theme, mode):
    palette = theme_palette(theme, mode)
    failures = [f"--{fg} on --{bg}: {contrast(palette[fg], palette[bg]):.2f}"
                for fg, bg in TEXT_PAIRS if contrast(palette[fg], palette[bg]) < 4.5]
    assert failures == []


def darken(hex_colour: str, alpha: float) -> str:
    """The colour seen through a black overlay with this opacity."""
    h = hex_colour.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + "".join(f"{round(int(h[i:i + 2], 16) * (1 - alpha)):02x}" for i in (0, 2, 4))


@pytest.mark.parametrize("theme", sorted(p.name for p in BUILTIN.iterdir() if p.is_dir()))
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_text_stays_readable_under_a_scanline_overlay(theme, mode):
    """A black overlay (the retro theme's --scanline rows) darkens text and background
    alike, which lowers their contrast; the covered rows must still reach 4.5:1."""
    palette = theme_palette(theme, mode)
    overlay = re.fullmatch(r"rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*([\d.]+)\s*\)", palette.get("scanline", "").strip())
    if not overlay:
        return
    alpha = float(overlay.group(1))
    failures = [f"--{fg} on --{bg}: {contrast(darken(palette[fg], alpha), darken(palette[bg], alpha)):.2f}"
                for fg, bg in TEXT_PAIRS
                if contrast(darken(palette[fg], alpha), darken(palette[bg], alpha)) < 4.5]
    assert failures == []


def test_retro_defines_every_colour_token_in_both_modes():
    colour_tokens = {k for k, v in base_palette("dark").items()
                     if v.strip().startswith(("#", "rgb"))}
    b = blocks((BUILTIN / "retro" / "theme.css").read_text())
    for mode in ("dark", "light"):
        missing = colour_tokens - set(b[f':root[data-mode="{mode}"]'])
        assert missing == set(), (mode, missing)


def test_base_css_has_both_modes_for_every_token():
    b = blocks((STATIC / "style.css").read_text())
    colour_tokens = {k for k, v in b[":root"].items() if v.strip().startswith(("#", "rgb"))}
    assert colour_tokens <= set(b[':root[data-mode="light"]'])


def test_mode_block_detection():
    assert th.MODE_BLOCK_RE["dark"].search(':root[data-mode="dark"] {')
    assert th.MODE_BLOCK_RE["light"].search(":root[ data-mode = 'light' ] body {")
    assert not th.MODE_BLOCK_RE["light"].search(':root[data-mode="lightish"] {')
    assert not th.MODE_BLOCK_RE["light"].search('content: \'[data-mode="light"]\';')
