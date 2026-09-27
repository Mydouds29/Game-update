"""Authentification Basic optionnelle, protection CSRF et en-têtes de sécurité."""

from __future__ import annotations

import hmac
import logging
import secrets

from flask import Flask, Response, abort, current_app, g, request, session
from werkzeug.security import check_password_hash

log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

PUBLIC_ENDPOINTS = {"feed.healthz"}


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _check_basic_auth() -> Response | None:
    cfg = current_app.config["GU"]
    if not cfg.basic_auth_user or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    auth = request.authorization
    if (
        auth is not None
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", cfg.basic_auth_user)
        and check_password_hash(cfg.basic_auth_password_hash, auth.password or "")
    ):
        return None
    if auth is not None:
        log.warning("auth.failed", extra={"remote": request.remote_addr})
    return Response(
        "Authentification requise", 401,
        {"WWW-Authenticate": 'Basic realm="Game Update", charset="UTF-8"'},
    )


def _check_csrf() -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        log.warning("csrf.rejected", extra={"path": request.path})
        abort(400, description="Jeton CSRF invalide ou expiré. Recharge la page.")


def _headers(response: Response) -> Response:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"
    )
    if current_app.config["GU"].is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    # Contenu privé : jamais en cache partagé.
    if "Cache-Control" not in response.headers and not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-cache"
    if getattr(g, "request_id", None):
        response.headers["X-Request-ID"] = g.request_id
    return response


def init_security(app: Flask) -> None:
    app.before_request(_check_basic_auth)
    app.before_request(_check_csrf)
    app.after_request(_headers)
    app.jinja_env.globals["csrf_token"] = csrf_token
