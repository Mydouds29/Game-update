"""Connexion SQLite et migrations du schéma."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "app.db.migrations"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    """Ouvre une connexion configurée (clés étrangères, WAL, Row factory)."""
    path_str = str(path)
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path_str, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path_str != ":memory:":
        # WAL : lectures web concurrentes pendant l'écriture du worker.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _available_migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in resources.files(_MIGRATIONS_PACKAGE).iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        number = int(name.split("_", 1)[0])
        found.append((number, name, entry.read_text(encoding="utf-8")))
    found.sort()
    return found


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Applique les migrations manquantes, chacune dans sa transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for number, name, sql in _available_migrations():
        if number in applied:
            continue
        log.info("migration.apply", extra={"migration": name})
        # executescript fait un COMMIT implicite : on encapsule nous-mêmes.
        try:
            conn.executescript(
                "BEGIN;\n"
                + sql
                + f"\nINSERT INTO schema_migrations (version, name, applied_at)"
                f" VALUES ({number}, '{name}', '{utcnow_iso()}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.exception("migration.failed", extra={"migration": name})
            raise
        done.append(name)
    return done
