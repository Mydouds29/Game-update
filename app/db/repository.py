"""Accès aux données. Toutes les requêtes SQL de l'application sont ici."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from ..parsing.normalizer import Patch
from .models import utcnow_iso


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction explicite (la connexion est en autocommit)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# -- jeux -------------------------------------------------------------------

def upsert_game(conn: sqlite3.Connection, *, slug: str, name: str, short_name: str,
                tag_color: str, sort_order: int, active: bool = True) -> int:
    """Crée ou met à jour un jeu. L'état actif n'est fixé qu'à la création :
    le choix fait dans l'interface prime ensuite sur le catalogue."""
    conn.execute(
        "INSERT INTO games (slug, name, short_name, tag_color, sort_order, active)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(slug) DO UPDATE SET name = excluded.name,"
        " short_name = excluded.short_name, tag_color = excluded.tag_color,"
        " sort_order = excluded.sort_order",
        (slug, name, short_name, tag_color, sort_order, int(active)),
    )
    return conn.execute("SELECT id FROM games WHERE slug = ?", (slug,)).fetchone()[0]


def list_games(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT g.*, (SELECT group_concat(s.label, ' + ') FROM sources s"
        "   WHERE s.game_id = g.id AND s.enabled = 1) AS source_labels,"
        " (SELECT max(s.last_success_at) FROM sources s WHERE s.game_id = g.id)"
        "   AS last_success_at,"
        " (SELECT count(*) FROM patches p WHERE p.game_id = g.id) AS patch_count"
        " FROM games g ORDER BY g.sort_order, g.name"
    ).fetchall()


def get_game(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM games WHERE slug = ?", (slug,)).fetchone()


def set_game_active(conn: sqlite3.Connection, slug: str, active: bool) -> bool:
    cur = conn.execute("UPDATE games SET active = ? WHERE slug = ?", (int(active), slug))
    return cur.rowcount == 1


# -- sources ----------------------------------------------------------------

@dataclass
class SourceRow:
    id: int
    game_id: int
    game_slug: str
    game_name: str
    game_active: bool
    key: str
    type: str
    label: str
    params: dict[str, Any]
    enabled: bool
    fetch_interval: int | None
    consecutive_failures: int
    etag: str | None
    last_modified: str | None
    next_fetch_at: str | None
    last_success_at: str | None


_SOURCE_SELECT = (
    "SELECT s.*, g.slug AS game_slug, g.name AS game_name, g.active AS game_active"
    " FROM sources s JOIN games g ON g.id = s.game_id"
)


def _source(row: sqlite3.Row) -> SourceRow:
    return SourceRow(
        id=row["id"], game_id=row["game_id"], game_slug=row["game_slug"],
        game_name=row["game_name"], game_active=bool(row["game_active"]),
        key=row["key"], type=row["type"], label=row["label"],
        params=json.loads(row["params"]), enabled=bool(row["enabled"]),
        fetch_interval=row["fetch_interval"],
        consecutive_failures=row["consecutive_failures"], etag=row["etag"],
        last_modified=row["last_modified"], next_fetch_at=row["next_fetch_at"],
        last_success_at=row["last_success_at"],
    )


def upsert_source(conn: sqlite3.Connection, *, game_id: int, key: str, type: str,
                  label: str, params: dict[str, Any], enabled: bool,
                  fetch_interval: int | None) -> int:
    params_json = json.dumps(params, sort_keys=True)
    existing = conn.execute(
        "SELECT id, params, type FROM sources WHERE key = ?", (key,)
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO sources (game_id, key, type, label, params, enabled, fetch_interval)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (game_id, key, type, label, params_json, int(enabled), fetch_interval),
        )
        return cur.lastrowid
    reset_cache = existing["params"] != params_json or existing["type"] != type
    conn.execute(
        "UPDATE sources SET game_id = ?, type = ?, label = ?, params = ?, enabled = ?,"
        " fetch_interval = ?"
        + (", etag = NULL, last_modified = NULL, next_fetch_at = NULL,"
           " consecutive_failures = 0" if reset_cache else "")
        + " WHERE id = ?",
        (game_id, type, label, params_json, int(enabled), fetch_interval, existing["id"]),
    )
    return existing["id"]


def get_source(conn: sqlite3.Connection, key: str) -> SourceRow | None:
    row = conn.execute(_SOURCE_SELECT + " WHERE s.key = ?", (key,)).fetchone()
    return _source(row) if row else None


def list_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    return [_source(r) for r in conn.execute(_SOURCE_SELECT + " ORDER BY g.sort_order, s.key")]


def sources_due(conn: sqlite3.Connection, now: str) -> list[SourceRow]:
    rows = conn.execute(
        _SOURCE_SELECT + " WHERE s.enabled = 1 AND g.active = 1"
        " AND (s.next_fetch_at IS NULL OR s.next_fetch_at <= ?)"
        " ORDER BY s.next_fetch_at IS NOT NULL, s.next_fetch_at",
        (now,),
    ).fetchall()
    return [_source(r) for r in rows]


def known_keys(conn: sqlite3.Connection, source_id: int) -> frozenset[str]:
    return frozenset(r[0] for r in conn.execute(
        "SELECT source_key FROM patches WHERE source_id = ?", (source_id,)))


def update_source_state(conn: sqlite3.Connection, source_id: int, *, success: bool,
                        now: str, next_fetch_at: str, etag: str | None = None,
                        last_modified: str | None = None) -> None:
    if success:
        conn.execute(
            "UPDATE sources SET last_fetch_at = ?, last_success_at = ?,"
            " consecutive_failures = 0, next_fetch_at = ?, etag = ?, last_modified = ?"
            " WHERE id = ?",
            (now, now, next_fetch_at, etag, last_modified, source_id),
        )
    else:
        conn.execute(
            "UPDATE sources SET last_fetch_at = ?, consecutive_failures ="
            " consecutive_failures + 1, next_fetch_at = ? WHERE id = ?",
            (now, next_fetch_at, source_id),
        )


def record_fetch(conn: sqlite3.Connection, *, source_id: int, started_at: str,
                 status: str, duration_ms: int, new_count: int = 0,
                 updated_count: int = 0, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO fetch_log (source_id, started_at, status, duration_ms, new_count,"
        " updated_count, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, started_at, status, duration_ms, new_count, updated_count,
         error[:2000] if error else None),
    )


def prune_fetch_log(conn: sqlite3.Connection, keep_per_source: int = 200) -> None:
    conn.execute(
        "DELETE FROM fetch_log WHERE id IN (SELECT id FROM (SELECT id, row_number()"
        " OVER (PARTITION BY source_id ORDER BY started_at DESC) AS rn FROM fetch_log)"
        " WHERE rn > ?)",
        (keep_per_source,),
    )


def source_status(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.key, s.label, s.type, s.enabled, s.last_fetch_at, s.last_success_at,"
        " s.consecutive_failures, s.next_fetch_at, g.name AS game_name, g.active,"
        " (SELECT status FROM fetch_log f WHERE f.source_id = s.id"
        "  ORDER BY f.started_at DESC, f.id DESC LIMIT 1) AS last_status,"
        " (SELECT error FROM fetch_log f WHERE f.source_id = s.id"
        "  ORDER BY f.started_at DESC, f.id DESC LIMIT 1) AS last_error"
        " FROM sources s JOIN games g ON g.id = s.game_id ORDER BY g.sort_order, s.key"
    ).fetchall()


# -- patchs -----------------------------------------------------------------

def find_same_content(conn: sqlite3.Connection, game_id: int, content_hash: str) -> bool:
    """Même contenu déjà publié pour ce jeu par une autre source (ex. Steam + Blizzard)."""
    return conn.execute(
        "SELECT 1 FROM patches WHERE game_id = ? AND content_hash = ? LIMIT 1",
        (game_id, content_hash),
    ).fetchone() is not None


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", title.casefold()).split())


def find_same_title(conn: sqlite3.Connection, game_id: int, title: str,
                    published_at: datetime | None, days: int = 3) -> bool:
    """Même note publiée par une autre source (ex. article de news + sujet du
    forum) : même jeu, même titre (casse et ponctuation ignorées), dates à
    quelques jours près. Le contenu peut différer légèrement d'une source à
    l'autre, d'où ce test en plus du hash de contenu."""
    if published_at is None:
        return False
    low, high = published_at - timedelta(days=days), published_at + timedelta(days=days)
    key = _title_key(title)
    return any(
        _title_key(row["title"]) == key
        for row in conn.execute(
            "SELECT title FROM patches WHERE game_id = ? AND published_at BETWEEN ? AND ?",
            (game_id, _iso(low), _iso(high)),
        )
    )


def find_patch(conn: sqlite3.Connection, source_id: int, source_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, content_hash, revision FROM patches WHERE source_id = ? AND source_key = ?",
        (source_id, source_key),
    ).fetchone()


def _write_sections(conn: sqlite3.Connection, patch_id: int, patch: Patch) -> None:
    conn.execute("DELETE FROM sections WHERE patch_id = ?", (patch_id,))
    for s_pos, section in enumerate(patch.sections):
        section_id = conn.execute(
            "INSERT INTO sections (patch_id, title, position) VALUES (?, ?, ?)",
            (patch_id, section.title, s_pos),
        ).lastrowid
        conn.executemany(
            "INSERT INTO items (section_id, subgroup, kind, text, position)"
            " VALUES (?, ?, ?, ?, ?)",
            [(section_id, it.subgroup, it.kind, it.text, i_pos)
             for i_pos, it in enumerate(section.items)],
        )


def insert_patch(conn: sqlite3.Connection, *, game_id: int, source_id: int,
                 patch: Patch, now: str) -> int:
    patch_id = conn.execute(
        "INSERT INTO patches (game_id, source_id, source_key, version, build, title,"
        " published_at, published_at_raw, platforms, source_url, content_hash,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, source_id, patch.source_key, patch.version, patch.build, patch.title,
         _iso(patch.published_at), patch.published_raw, patch.platforms,
         patch.source_url, patch.content_hash, now, now),
    ).lastrowid
    _write_sections(conn, patch_id, patch)
    return patch_id


def update_patch(conn: sqlite3.Connection, patch_id: int, patch: Patch, now: str) -> None:
    conn.execute(
        "UPDATE patches SET version = ?, build = ?, title = ?, published_at = ?,"
        " published_at_raw = ?, platforms = ?, source_url = ?, content_hash = ?,"
        " updated_at = ?, revision = revision + 1 WHERE id = ?",
        (patch.version, patch.build, patch.title, _iso(patch.published_at),
         patch.published_raw, patch.platforms, patch.source_url, patch.content_hash,
         now, patch_id),
    )
    _write_sections(conn, patch_id, patch)


_FEED_ORDER = " ORDER BY coalesce(p.published_at, p.created_at) DESC, p.id DESC"


def feed(conn: sqlite3.Connection, *, game_slug: str | None, limit: int,
         offset: int, only_active: bool = True) -> list[dict[str, Any]]:
    sql = (
        "SELECT p.id, p.title, p.version, p.build, p.platforms, p.published_at,"
        " p.published_at_raw, p.created_at, p.updated_at, p.revision,"
        " g.slug AS game_slug, g.name AS game_name, g.short_name AS game_short"
        " FROM patches p JOIN games g ON g.id = p.game_id"
        + (" WHERE g.active = 1" if only_active else " WHERE 1 = 1")
    )
    args: list[Any] = []
    if game_slug:
        sql += " AND g.slug = ?"
        args.append(game_slug)
    sql += _FEED_ORDER + " LIMIT ? OFFSET ?"
    args += [limit, offset]
    patches = [dict(r) for r in conn.execute(sql, args)]
    if not patches:
        return patches
    ids = [p["id"] for p in patches]
    marks = ",".join("?" * len(ids))
    counts: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in conn.execute(
        "SELECT s.patch_id, s.title, count(i.id) AS n FROM sections s"
        " LEFT JOIN items i ON i.section_id = s.id AND i.kind != 'note'"
        f" WHERE s.patch_id IN ({marks}) GROUP BY s.id ORDER BY s.patch_id, s.position",
        ids,
    ):
        counts[row["patch_id"]].append({"title": row["title"], "count": row["n"]})
    for p in patches:
        p["sections"] = [s for s in counts[p["id"]] if s["count"]]
        p["item_count"] = sum(s["count"] for s in counts[p["id"]])
    return patches


def get_patch(conn: sqlite3.Connection, patch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT p.*, g.slug AS game_slug, g.name AS game_name, g.short_name AS game_short,"
        " g.active AS game_active, s.label AS source_label, s.type AS source_type"
        " FROM patches p JOIN games g ON g.id = p.game_id"
        " JOIN sources s ON s.id = p.source_id WHERE p.id = ?",
        (patch_id,),
    ).fetchone()
    if row is None:
        return None
    patch = dict(row)
    sections: list[dict[str, Any]] = []
    by_id: dict[int, dict[str, Any]] = {}
    for s in conn.execute(
        "SELECT id, title FROM sections WHERE patch_id = ? ORDER BY position", (patch_id,)
    ):
        section = {"id": s["id"], "title": s["title"], "items": []}
        sections.append(section)
        by_id[s["id"]] = section
    if by_id:
        marks = ",".join("?" * len(by_id))
        for it in conn.execute(
            f"SELECT section_id, subgroup, kind, text FROM items WHERE section_id IN ({marks})"
            " ORDER BY section_id, position",
            list(by_id),
        ):
            by_id[it["section_id"]]["items"].append(dict(it))
    for s in sections:
        s["count"] = sum(1 for i in s["items"] if i["kind"] != "note")
    patch["sections"] = sections
    patch["item_count"] = sum(s["count"] for s in sections)
    return patch


# -- notifications ----------------------------------------------------------

def queue_notification(conn: sqlite3.Connection, patch_id: int, channel: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO notifications (patch_id, channel) VALUES (?, ?)",
        (patch_id, channel),
    )


def pending_notifications(conn: sqlite3.Connection, channel: str,
                          max_attempts: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT n.id, n.patch_id, n.attempts, p.title, p.version, p.source_url,"
        " g.name AS game_name, g.active AS game_active"
        " FROM notifications n JOIN patches p ON p.id = n.patch_id"
        " JOIN games g ON g.id = p.game_id"
        " WHERE n.channel = ? AND n.sent_at IS NULL AND n.attempts < ? ORDER BY n.id",
        (channel, max_attempts),
    ).fetchall()


def mark_notification(conn: sqlite3.Connection, notification_id: int, *,
                      sent: bool, error: str | None = None) -> None:
    conn.execute(
        "UPDATE notifications SET attempts = attempts + 1, sent_at = ?, last_error = ?"
        " WHERE id = ?",
        (utcnow_iso() if sent else None, error[:1000] if error else None, notification_id),
    )
