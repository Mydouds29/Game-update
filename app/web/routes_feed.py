"""Fil chronologique, feuille de style des tags, fichiers PWA, santé."""

from __future__ import annotations

from flask import (
    Blueprint, Response, abort, current_app, jsonify, render_template, request,
    send_from_directory,
)

from .. import get_db
from ..db import repository as repo

bp = Blueprint("feed", __name__)

PAGE_SIZE = 20


@bp.get("/")
def index() -> str:
    conn = get_db()
    game_slug = request.args.get("game") or None
    games = repo.list_games(conn)
    active_games = [g for g in games if g["active"]]
    if game_slug and not any(g["slug"] == game_slug for g in active_games):
        abort(404, description="Jeu inconnu ou désactivé.")
    try:
        page = max(1, min(int(request.args.get("page", 1)), 500))
    except ValueError:
        page = 1
    patches = repo.feed(conn, game_slug=game_slug, limit=PAGE_SIZE + 1,
                        offset=(page - 1) * PAGE_SIZE)
    has_more = len(patches) > PAGE_SIZE
    patches = patches[:PAGE_SIZE]
    latest, older = (patches[0], patches[1:]) if patches and page == 1 else (None, patches)
    return render_template(
        "feed.html", games=active_games, game_slug=game_slug, latest=latest,
        older=older, page=page, has_more=has_more, nav="feed",
    )


@bp.get("/tags.css")
def tags_css() -> Response:
    # Couleurs des jeux en CSS servi par l'app : la CSP interdit les styles inline.
    rules = [
        f".tag-{g['slug']}{{--tag:{g['tag_color']}}}" for g in repo.list_games(get_db())
    ]
    resp = Response("\n".join(rules) + "\n", mimetype="text/css")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@bp.get("/manifest.webmanifest")
def manifest() -> Response:
    resp = send_from_directory(current_app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")
    return resp


@bp.get("/sw.js")
def service_worker() -> Response:
    # Servi à la racine pour que la portée du service worker couvre toute l'app.
    resp = send_from_directory(current_app.static_folder, "sw.js",
                               mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/healthz")
def healthz() -> tuple[Response, int]:
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001
        return jsonify(status="error"), 503
    return jsonify(status="ok"), 200
