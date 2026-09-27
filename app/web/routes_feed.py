"""Accueil (menu des jeux et patchs du jeu choisi), feuille de style des tags, fichiers PWA, santé."""

from __future__ import annotations

from flask import (
    Blueprint, Response, abort, current_app, jsonify, render_template, request,
    send_from_directory,
)

from .. import get_db
from ..db import repository as repo

bp = Blueprint("feed", __name__)


PAGE_SIZE = 5  # patchs affichés en entier : pages courtes


@bp.get("/")
def index() -> str:
    """Menu déroulant des jeux ; le jeu choisi affiche ses patchs complets en dessous."""
    conn = get_db()
    games = sorted(repo.list_games(conn), key=lambda g: g["name"].casefold())
    game = None
    patches: list = []
    has_more = False
    slug = request.args.get("game")
    try:
        page = max(1, min(int(request.args.get("page", 1)), 500))
    except ValueError:
        page = 1
    if slug:
        game = repo.get_game(conn, slug)
        if game is None:
            abort(404, description="Jeu inconnu.")
        patches = repo.feed(conn, game_slug=slug, limit=PAGE_SIZE + 1,
                            offset=(page - 1) * PAGE_SIZE, only_active=False)
        has_more = len(patches) > PAGE_SIZE
        patches = [repo.get_patch(conn, p["id"]) for p in patches[:PAGE_SIZE]]
    return render_template("home.html", games=games, game=game, patches=patches,
                           page=page, has_more=has_more)


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
