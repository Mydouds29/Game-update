-- Schéma initial Game Update.

CREATE TABLE games (
    id          INTEGER PRIMARY KEY,
    slug        TEXT    NOT NULL UNIQUE CHECK (slug GLOB '[a-z0-9]*' AND slug NOT GLOB '*[^a-z0-9-]*'),
    name        TEXT    NOT NULL,
    short_name  TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    tag_color   TEXT    NOT NULL DEFAULT '#A7ADB5' CHECK (tag_color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'),
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sources (
    id                   INTEGER PRIMARY KEY,
    game_id              INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    key                  TEXT    NOT NULL UNIQUE,          -- identifiant stable issu du catalogue
    type                 TEXT    NOT NULL CHECK (type IN ('steam', 'blizzard', 'html')),
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
CREATE INDEX idx_sources_game ON sources(game_id);

CREATE TABLE patches (
    id                 INTEGER PRIMARY KEY,
    game_id            INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    source_id          INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_key         TEXT    NOT NULL,       -- identifiant côté source (gid Steam, ancre Blizzard, URL…)
    version            TEXT,
    build              TEXT,
    title              TEXT    NOT NULL,
    published_at       TEXT,                   -- date de la source normalisée en ISO 8601 UTC si parsable
    published_at_raw   TEXT,                   -- date de la source telle quelle
    platforms          TEXT,
    source_url         TEXT    NOT NULL,
    content_hash       TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,       -- date de détection
    updated_at         TEXT    NOT NULL,
    revision           INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_id, source_key)
);
CREATE INDEX idx_patches_feed ON patches(game_id, published_at DESC, created_at DESC);
CREATE INDEX idx_patches_order ON patches(published_at DESC, created_at DESC);

CREATE TABLE sections (
    id        INTEGER PRIMARY KEY,
    patch_id  INTEGER NOT NULL REFERENCES patches(id) ON DELETE CASCADE,
    title     TEXT    NOT NULL,
    position  INTEGER NOT NULL
);
CREATE INDEX idx_sections_patch ON sections(patch_id, position);

CREATE TABLE items (
    id          INTEGER PRIMARY KEY,
    section_id  INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    subgroup    TEXT,
    kind        TEXT    NOT NULL DEFAULT 'change' CHECK (kind IN ('fix', 'change', 'new', 'balance', 'note')),
    text        TEXT    NOT NULL,
    position    INTEGER NOT NULL
);
CREATE INDEX idx_items_section ON items(section_id, position);

CREATE TABLE fetch_log (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at   TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('ok', 'not_modified', 'error', 'skipped')),
    duration_ms  INTEGER NOT NULL,
    new_count    INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX idx_fetch_log_source ON fetch_log(source_id, started_at DESC);

CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY,
    patch_id   INTEGER NOT NULL REFERENCES patches(id) ON DELETE CASCADE,
    channel    TEXT    NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (patch_id, channel)
);
