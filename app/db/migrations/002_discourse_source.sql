-- Nouveau type de source « discourse » (hotfixes publiés sur un forum).
-- SQLite ne sait pas modifier une contrainte CHECK : la table est reconstruite
-- (clés étrangères coupées par migrate(), les patchs existants sont conservés).
CREATE TABLE sources_new (
    id                   INTEGER PRIMARY KEY,
    game_id              INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    key                  TEXT    NOT NULL UNIQUE,          -- identifiant stable issu du catalogue
    type                 TEXT    NOT NULL CHECK (type IN ('rss', 'steam', 'blizzard', 'html', 'discourse')),
    label                TEXT    NOT NULL,
    params               TEXT    NOT NULL DEFAULT '{}',    -- JSON
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    fetch_interval       INTEGER,                          -- secondes ; NULL = défaut global
    last_fetch_at        TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_fetch_at        TEXT,
    etag                 TEXT,
    last_modified        TEXT
);
INSERT INTO sources_new SELECT * FROM sources;
DROP TABLE sources;
ALTER TABLE sources_new RENAME TO sources;
CREATE INDEX idx_sources_game ON sources(game_id);
