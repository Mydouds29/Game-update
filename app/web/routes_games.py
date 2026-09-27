"""Gestion des jeux actifs et page Réglages."""

from __future__ import annotations

import logging

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from .. import get_db
from ..db import repository as repo

log = logging.getLogger(__name__)

bp = Blueprint("games", __name__)


def _safe_next(default: str) -> str:
    target = request.form.get("next", "")
    # Uniquement des chemins locaux (pas de redirection ouverte).
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


@bp.get("/games")
def index() -> str:
    return render_template("games.html", games=repo.list_games(get_db()), nav="games")


@bp.post("/games/<slug>/active")
def set_active(slug: str) -> Response:
    value = request.form.get("active")
    if value not in {"0", "1"}:
        abort(400, description="Valeur invalide.")
    conn = get_db()
    if not repo.set_game_active(conn, slug, value == "1"):
        abort(404, description="Jeu inconnu.")
    log.info("game.toggled", extra={"game": slug, "active": value == "1"})
    return redirect(_safe_next(url_for("games.index")), code=303)


@bp.get("/settings")
def settings() -> str:
    cfg = current_app.config["GU"]
    topic = cfg.ntfy_topic or ""
    masked = (topic[:3] + "…" + topic[-2:]) if len(topic) > 8 else ("…" if topic else "")
    return render_template(
        "settings.html", sources=repo.source_status(get_db()), notifier=cfg.notifier,
        ntfy_server=cfg.ntfy_server, ntfy_topic_masked=masked, nav="settings",
    )
