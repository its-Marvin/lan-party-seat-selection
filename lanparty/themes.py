"""Event themes: folders with a ``theme.css`` (and optional assets) that restyle the public page.

Built-in themes live in ``lanparty/themes/<id>/``, custom ones in ``THEMES_DIR``
(default ``instance/themes/<id>/``). A theme folder holds:

* ``theme.css`` (required): overrides the colour tokens of ``static/style.css``
  for **both** colour modes, in ``:root[data-mode="dark"]`` and
  ``:root[data-mode="light"]`` blocks. A theme without both is not offered.
* ``theme.json`` (optional): ``{"name": "...", "description": "..."}``.
* fonts and images referenced from ``theme.css`` with relative URLs.

See THEMES.md for a step-by-step guide.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from flask import current_app

DEFAULT_THEME = "default"
THEME_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MAX_CSS_BYTES = 512 * 1024
# a rule whose selector targets the mode, e.g. :root[data-mode="dark"] { … }
MODE_BLOCK_RE = {mode: re.compile(r"\[\s*data-mode\s*=\s*([\"']?)" + mode + r"\1\s*\][^{};]*\{")
                 for mode in ("dark", "light")}
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# files a theme may serve besides theme.css
ASSET_TYPES = {
    ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".otf": "font/otf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    builtin: bool
    path: Path
    problem: str | None = None   # why the theme can't be used

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "builtin": self.builtin}


def builtin_dir() -> Path:
    return Path(__file__).parent / "themes"


def custom_dir() -> Path:
    return Path(current_app.config["THEMES_DIR"])


def _read_theme(folder: Path, builtin: bool) -> Theme:
    theme_id = folder.name
    name, description, problem = theme_id, "", None
    meta = folder / "theme.json"
    try:
        # inside the try: without search permission on the folder, even the
        # existence check raises (Python < 3.14)
        if meta.is_file():
            data = json.loads(meta.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            name = str(data.get("name") or theme_id)[:60]
            description = str(data.get("description") or "")[:300]
    except OSError as exc:
        problem = f"theme.json can't be read ({exc.strerror or exc})."
    except ValueError as exc:
        problem = f"theme.json is not valid JSON ({exc})."
    css = folder / "theme.css"
    if problem is None:
        try:
            if not css.is_file():
                problem = "theme.css is missing."
            elif css.stat().st_size > MAX_CSS_BYTES:
                problem = f"theme.css is larger than {MAX_CSS_BYTES // 1024} KB."
            else:
                text = css.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # e.g. wrong permissions: only this theme is unusable, the others keep working
            problem = f"theme.css can't be read ({exc.strerror or exc})."
    if problem is None:
        # a mode that only appears in a comment doesn't count
        text = CSS_COMMENT_RE.sub("", text)
        missing = [mode for mode, pattern in MODE_BLOCK_RE.items() if not pattern.search(text)]
        if missing:
            problem = ("theme.css must style both colour modes; missing "
                       + " and ".join(f':root[data-mode="{m}"]' for m in missing) + ".")
    return Theme(theme_id, name, description, builtin, folder, problem)


def discover() -> dict[str, Theme]:
    """All theme folders, built-in first. Custom themes can't replace built-in ones."""
    themes: dict[str, Theme] = {}
    for root, builtin in ((builtin_dir(), True), (custom_dir(), False)):
        try:
            if not root.is_dir():
                continue
            folders = sorted(root.iterdir())
        except OSError as exc:
            # e.g. wrong permissions on THEMES_DIR: report it, keep the built-in themes
            themes[f"unreadable:{root}"] = Theme(
                str(root), str(root), "", builtin, root,
                f"The themes folder can't be read ({exc.strerror or exc}).")
            continue
        for folder in folders:
            try:
                if not folder.is_dir() or folder.name.startswith("."):
                    continue
            except OSError as exc:
                themes[f"unreadable:{folder}"] = Theme(
                    folder.name, folder.name, "", builtin, folder,
                    f"The theme folder can't be read ({exc.strerror or exc}).")
                continue
            if not THEME_ID_RE.fullmatch(folder.name):
                themes[f"invalid:{folder.name}"] = Theme(
                    folder.name, folder.name, "", builtin, folder,
                    "Folder names may only use lowercase letters, digits, - and _ (max. 40).")
                continue
            if folder.name in themes:
                themes[f"conflict:{folder.name}"] = Theme(
                    folder.name, folder.name, "", builtin, folder,
                    "A built-in theme already has this name; rename the folder.")
                continue
            themes[folder.name] = _read_theme(folder, builtin)
    return themes


def available() -> dict[str, Theme]:
    return {tid: t for tid, t in discover().items() if t.problem is None}


def get(theme_id: str) -> Theme | None:
    if not isinstance(theme_id, str) or not THEME_ID_RE.fullmatch(theme_id):
        return None
    return available().get(theme_id)
