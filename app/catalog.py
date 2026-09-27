"""Chargement et application du catalogue curaté (catalog.toml)."""

from __future__ import annotations

import logging
import re
import sqlite3
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from .collectors.base import CollectorConfigError
from .collectors.registry import get_collector
from .db import repository as repo

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CatalogError(ValueError):
    pass


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        text = resources.files("app").joinpath("catalog.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"catalogue TOML invalide : {exc}") from exc
    validate_catalog(data)
    return data


def validate_catalog(data: dict[str, Any]) -> None:
    games = data.setdefault("games", [])
    if not isinstance(games, list):
        raise CatalogError("'games' doit être une liste de [[games]]")
    slugs: set[str] = set()
    keys: set[str] = set()
    for game in games:
        slug = game.get("slug", "")
        if not SLUG_RE.match(slug):
            raise CatalogError(f"slug invalide : {slug!r}")
        if slug in slugs:
            raise CatalogError(f"slug en double : {slug}")
        slugs.add(slug)
        for field in ("name", "short_name"):
            if not isinstance(game.get(field), str) or not game[field].strip():
                raise CatalogError(f"{slug} : '{field}' manquant")
        if not COLOR_RE.match(game.get("tag_color", "")):
            raise CatalogError(f"{slug} : tag_color doit être au format #RRGGBB")
        for source in game.get("sources", []):
            key = source.get("key", "")
            if not SLUG_RE.match(key) or key in keys:
                raise CatalogError(f"{slug} : clé de source invalide ou en double : {key!r}")
            keys.add(key)
            interval = source.get("fetch_interval")
            if interval is not None and (not isinstance(interval, int) or interval < 300):
                raise CatalogError(f"{key} : fetch_interval doit être un entier >= 300")
            try:
                get_collector(source.get("type", "")).validate_params(source.get("params", {}))
            except (KeyError, CollectorConfigError) as exc:
                raise CatalogError(f"{key} : {exc}") from exc


def sync_catalog(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, int]:
    """Applique le catalogue. Les jeux/sources absents du fichier sont
    désactivés (jamais supprimés : l'historique des patchs est conservé)."""
    stats = {"games": 0, "sources": 0, "disabled_sources": 0}
    with repo.transaction(conn):
        seen_sources: list[str] = []
        for position, game in enumerate(data["games"]):
            game_id = repo.upsert_game(
                conn, slug=game["slug"], name=game["name"].strip(),
                short_name=game["short_name"].strip(), tag_color=game["tag_color"],
                sort_order=position, active=bool(game.get("active", True)),
            )
            stats["games"] += 1
            for source in game.get("sources", []):
                repo.upsert_source(
                    conn, game_id=game_id, key=source["key"], type=source["type"],
                    label=source.get("label", source["type"]),
                    params=source.get("params", {}),
                    enabled=bool(source.get("enabled", True)),
                    fetch_interval=source.get("fetch_interval"),
                )
                seen_sources.append(source["key"])
                stats["sources"] += 1
        marks = ",".join("?" * len(seen_sources)) or "''"
        cur = conn.execute(
            f"UPDATE sources SET enabled = 0 WHERE enabled = 1 AND key NOT IN ({marks})",
            seen_sources,
        )
        stats["disabled_sources"] = cur.rowcount
    log.info("catalog.synced", extra=stats)
    return stats
