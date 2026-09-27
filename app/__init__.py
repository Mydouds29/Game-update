"""Factory Flask de Game Update."""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
from datetime import timedelta

from flask import Flask, g, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .db.models import connect
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        from flask import current_app
        g.db = connect(current_app.config["GU"].database_path)
    return g.db


def _close_db(_exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def create_app(config: Config | None = None) -> Flask:
    if config is None:
        config = Config.from_env()
    setup_logging(config.log_level)

    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config.update(
        GU=config,
        SECRET_KEY=config.secret_key,
        TESTING=config.testing,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.is_production,
        SESSION_COOKIE_NAME="gu_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=64 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
        JSON_SORT_KEYS=False,
    )
    if config.trusted_proxies:
        n = config.trusted_proxies
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)  # type: ignore[method-assign]

    # Enregistré en premier : l'identifiant de requête sert aux autres hooks.
    @app.before_request
    def _start_timer() -> None:
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = (incoming if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", incoming)
                        else secrets.token_hex(8))
        g.t0 = time.monotonic()

    from .security import init_security
    init_security(app)

    from .web import filters
    filters.register(app)

    from .web.routes_feed import bp as feed_bp
    from .web.routes_games import bp as games_bp
    from .web.routes_patch import bp as patch_bp
    app.register_blueprint(feed_bp)
    app.register_blueprint(patch_bp)
    app.register_blueprint(games_bp)

    from .cli import register_cli
    register_cli(app)

    app.teardown_appcontext(_close_db)

    @app.after_request
    def _log_request(response):  # type: ignore[no-untyped-def]
        if request.endpoint != "static":
            log.info("http.request", extra={
                "request_id": g.get("request_id"),
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": int((time.monotonic() - g.get("t0", time.monotonic())) * 1000),
            })
        return response

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):  # type: ignore[no-untyped-def]
        if exc.code == 401:
            return exc
        return render_template("error.html", code=exc.code,
                               message=exc.description), exc.code

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):  # type: ignore[no-untyped-def]
        log.exception("http.unhandled", extra={"request_id": g.get("request_id"),
                                                "path": request.path})
        return render_template("error.html", code=500,
                               message="Erreur interne. Elle a été journalisée."), 500

    return app
