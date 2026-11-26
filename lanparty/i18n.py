"""Translations of the messages the API sends (errors mostly).

Messages are written in English in the code, wrapped in ``tr()``:

    raise ApiError(409, tr("Seat {seat} is already taken.", seat=seat))

The English template is the key into ``locales/<lang>.json``. The language
comes from the ``X-Lang`` header (sent by the browser app), else from
``Accept-Language``; it falls back to English. ``tests/test_i18n.py`` checks
that every template has a translation with the same placeholders.

The audit log and server logs stay in English on purpose.
"""

from functools import cache
import json
from pathlib import Path
import string

from flask import has_request_context, request

LANGUAGES = ("en-US", "de-DE")
DEFAULT_LANGUAGE = "en-US"
LOCALES = Path(__file__).parent / "locales"


@cache
def catalog(lang: str) -> dict[str, str]:
    path = LOCALES / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def request_language() -> str:
    if not has_request_context():
        return DEFAULT_LANGUAGE
    wanted = request.headers.get("X-Lang", "")
    if wanted in LANGUAGES:
        return wanted
    best = request.accept_languages.best_match(["en-US", "en", "de-DE", "de"])
    return "de-DE" if best and best.startswith("de") else DEFAULT_LANGUAGE


def tr(template: str, **params) -> str:
    """Translate ``template`` into the request's language and fill in ``params``."""
    lang = request_language()
    text = template if lang == DEFAULT_LANGUAGE else catalog(lang).get(template, template)
    return text.format(**params) if params else text


def N_(template: str) -> str:
    """Mark a template for translation without translating it yet (e.g. a constant
    that is passed to ``tr()`` later), so tests/test_i18n.py finds it."""
    return template


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}
