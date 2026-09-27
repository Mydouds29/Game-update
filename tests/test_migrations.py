import sqlite3

import pytest

from app.db.models import _available_migrations, connect, migrate


def _db_at_version_1(tmp_path):
    """Base dans l'état de la migration 001, avec un patch complet."""
    conn = connect(tmp_path / "old.sqlite3")
    number, name, sql = _available_migrations()[0]
    assert number == 1
    conn.executescript(sql + "\nCREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                       " name TEXT NOT NULL, applied_at TEXT NOT NULL);"
                       f"\nINSERT INTO schema_migrations VALUES (1, '{name}', 'x');")
    conn.executescript("""
        INSERT INTO games (id, slug, name, short_name) VALUES (1, 'diablo-4', 'Diablo IV', 'D4');
        INSERT INTO sources (id, game_id, key, type, label, etag)
            VALUES (7, 1, 'diablo-4-blizzard', 'blizzard', 'Blizzard News', '"abc"');
        INSERT INTO patches (id, game_id, source_id, source_key, title, source_url,
            content_hash, created_at, updated_at)
            VALUES (3, 1, 7, 'anchor:3.2.2', 'Patch 3.2.2', 'https://x', 'h', 'd', 'd');
        INSERT INTO sections (id, patch_id, title, position) VALUES (5, 3, 'Bug Fixes', 0);
        INSERT INTO items (section_id, kind, text, position) VALUES (5, 'fix', 'Fixed it.', 0);
    """)
    return conn


def test_002_keeps_existing_patches_and_allows_discourse(tmp_path):
    conn = _db_at_version_1(tmp_path)
    assert "002_discourse_source.sql" in migrate(conn)
    # Rien n'a été effacé en cascade par la reconstruction de `sources`.
    assert conn.execute("SELECT count(*) FROM patches").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    src = conn.execute("SELECT * FROM sources WHERE id = 7").fetchone()
    assert src["key"] == "diablo-4-blizzard" and src["etag"] == '"abc"'
    conn.execute("INSERT INTO sources (game_id, key, type, label)"
                 " VALUES (1, 'diablo-4-forum', 'discourse', 'Forum')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sources (game_id, key, type, label)"
                     " VALUES (1, 'x', 'inconnu', 'X')")
    # Clés étrangères réactivées, avec la cascade vers les patchs.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("DELETE FROM sources WHERE id = 7")
    assert conn.execute("SELECT count(*) FROM patches").fetchone()[0] == 0


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "new.sqlite3")
    assert len(migrate(conn)) == len(_available_migrations())
    assert migrate(conn) == []
