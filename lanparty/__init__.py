"""LAN party seat registration web app."""

import os
import secrets
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .api import bp as api_bp
from .db import database_target, init_db, is_postgres
from . import events, themes

MIN_ADMIN_PASSWORD_LENGTH = 12

CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    instance = Path(app.instance_path)

    app.config.update(
        DATABASE=os.environ.get("DATABASE", str(instance / "lanparty.sqlite3")),
        # PostgreSQL connection URL (postgresql://user:password@host/db); overrides DATABASE
        DATABASE_URL=os.environ.get("DATABASE_URL", "").strip(),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        ALLOW_WEAK_ADMIN_PASSWORD=os.environ.get("ALLOW_WEAK_ADMIN_PASSWORD", "0") == "1",
        # Flask's signed session is not used for authentication; admin sessions
        # live in the database, so this key does not need to be configured.
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=_env_int("MAX_CONTENT_LENGTH", 256 * 1024),
        TRUSTED_PROXIES=_env_int("TRUSTED_PROXIES", 0),
        # rate limits
        ADMIN_LOGIN_MAX_PER_IP=_env_int("ADMIN_LOGIN_MAX_PER_IP", 5),
        ADMIN_LOGIN_MAX_GLOBAL=_env_int("ADMIN_LOGIN_MAX_GLOBAL", 50),
        ADMIN_LOGIN_WINDOW_MINUTES=_env_int("ADMIN_LOGIN_WINDOW_MINUTES", 15),
        REGISTER_MAX_PER_IP=_env_int("REGISTER_MAX_PER_IP", 10),
        MAX_PENDING_PER_IP=_env_int("MAX_PENDING_PER_IP", 4),
        LOGIN_CODE_MAX_PER_IP=_env_int("LOGIN_CODE_MAX_PER_IP", 20),
        EVENT_CODE_MAX_PER_IP=_env_int("EVENT_CODE_MAX_PER_IP", 10),
        # sessions / login links
        ADMIN_SESSION_IDLE_MINUTES=_env_int("ADMIN_SESSION_IDLE_MINUTES", 60),
        ADMIN_SESSION_MAX_HOURS=_env_int("ADMIN_SESSION_MAX_HOURS", 12),
        LOGIN_CODE_HOURS=_env_int("LOGIN_CODE_HOURS", 7 * 24),
        # IANA time zone of the event dates (e.g. Europe/Berlin); empty = server time
        TIMEZONE=os.environ.get("TIMEZONE", "").strip(),
        # folder with custom themes (one sub-folder per theme, see THEMES.md)
        THEMES_DIR=os.environ.get("THEMES_DIR", str(instance / "themes")),
    )
    if config:
        app.config.update(config)

    password = app.config["ADMIN_PASSWORD"]
    if not password:
        app.logger.warning("ADMIN_PASSWORD not set; the admin area is disabled.")
    elif (len(password) < MIN_ADMIN_PASSWORD_LENGTH
          and not app.config["ALLOW_WEAK_ADMIN_PASSWORD"] and not app.config.get("TESTING")):
        raise RuntimeError(
            f"ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters long "
            "(set ALLOW_WEAK_ADMIN_PASSWORD=1 for local development only)."
        )

    # absolute, so listing themes (relative to the working directory) and serving
    # their files (send_from_directory, relative to the app) use the same folder
    app.config["THEMES_DIR"] = str(Path(app.config["THEMES_DIR"]).resolve())

    if app.config["TIMEZONE"]:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(app.config["TIMEZONE"])
        except (ZoneInfoNotFoundError, ValueError):
            raise RuntimeError(
                f"Unknown TIMEZONE {app.config['TIMEZONE']!r} (use an IANA name like Europe/Berlin)."
            ) from None

    if app.config["TRUSTED_PROXIES"] > 0:
        n = app.config["TRUSTED_PROXIES"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    if not is_postgres(database_target(app.config)):
        Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    init_db(app)
    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/admin")
    def admin():
        return send_from_directory(app.static_folder, "admin.html")

    @app.get("/theme.css")
    def event_theme():
        """The active event's theme. It is imported (not inlined or redirected to),
        so relative URLs in it (fonts, images) resolve inside the theme folder,
        and --lp-theme tells app.js which theme this page actually got."""
        from .settings import load

        event_id = events.active_id()
        wanted = load(event_id)["theme"] if event_id is not None else themes.DEFAULT_THEME
        theme = themes.get(wanted) or themes.get(themes.DEFAULT_THEME)
        # theme ids are [a-z0-9_-] only, safe inside a CSS string
        css = f'@import url("/themes/{theme.id}/theme.css");\n:root {{ --lp-theme: "{theme.id}"; }}\n'
        resp = app.response_class(css, mimetype="text/css")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/themes/<theme_id>/<path:filename>")
    def theme_file(theme_id, filename):
        theme = themes.get(theme_id)
        mimetype = themes.ASSET_TYPES.get(Path(filename).suffix.lower())
        if theme is None or mimetype is None:
            abort(404)
        # send_from_directory refuses paths outside the theme folder
        resp = send_from_directory(theme.path, filename, mimetype=mimetype, max_age=0)
        resp.headers["Cache-Control"] = "no-cache"   # revalidate: themes can be edited in place
        return resp

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.errorhandler(HTTPException)
    def http_error(err: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=err.description), err.code
        return err

    @app.errorhandler(Exception)
    def unexpected_error(err: Exception):
        if isinstance(err, HTTPException):
            return http_error(err)
        app.logger.exception("Unhandled error")
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error."), 500
        return "Internal server error", 500

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
