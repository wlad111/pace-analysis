-- Pace Analysis storage schema (SQLite).
--
-- Conventions:
--   * every duration is an INTEGER number of milliseconds, NULL means "no time";
--   * every timestamp is stored as ISO-8601 TEXT;
--     `session.started_at` is a *naive* local time at the venue ('YYYY-MM-DDTHH:MM:SS'),
--     bookkeeping timestamps (`imported_at`, `created_at`) are UTC with a trailing 'Z';
--   * every statement is idempotent (IF NOT EXISTS) so the file can be replayed
--     on every connection.
--
-- Natural keys that contain nullable columns are enforced through expression
-- indexes with COALESCE(): in SQLite a plain UNIQUE constraint treats NULLs as
-- distinct, which would let duplicates through (e.g. a session without a code).

-- --------------------------------------------------------------------------
-- Reference entities
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS club (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    external_id TEXT UNIQUE,          -- Apex Timing "center" id, e.g. '51'
    website     TEXT,
    email       TEXT
);

CREATE TABLE IF NOT EXISTS driver (
    id          INTEGER PRIMARY KEY,
    nickname    TEXT NOT NULL UNIQUE,
    external_id TEXT UNIQUE           -- Apex "client" id; known only for email recipients
);

-- --------------------------------------------------------------------------
-- Sessions and results
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY,
    club_id    INTEGER NOT NULL REFERENCES club (id) ON DELETE CASCADE,
    name       TEXT NOT NULL,         -- 'PRIMO GARA - Final A'
    code       TEXT,                  -- 'FA'
    started_at TEXT,                  -- naive local time at the venue
    track      TEXT,
    category   TEXT,                  -- kart class, e.g. 'SR5'
    tz_name    TEXT,
    created_at TEXT NOT NULL
);

-- Session identity is (club, name, code, started_at) -- never the message id:
-- the same race is mailed to every recipient in a separate email.
CREATE UNIQUE INDEX IF NOT EXISTS ux_session_key
    ON session (club_id, name, COALESCE(code, ''), COALESCE(started_at, ''));

CREATE TABLE IF NOT EXISTS session_entry (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    driver_id   INTEGER NOT NULL REFERENCES driver (id) ON DELETE CASCADE,
    position    INTEGER,
    kart        TEXT,
    laps_count  INTEGER,
    gap_ms      INTEGER,              -- gap to the leader, NULL for the leader
    gap_laps    INTEGER,              -- set instead of gap_ms when the gap is 'N Laps'
    best_lap_ms INTEGER,
    UNIQUE (session_id, driver_id)
);

CREATE INDEX IF NOT EXISTS ix_session_entry_driver ON session_entry (driver_id);

CREATE TABLE IF NOT EXISTS lap (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    driver_id  INTEGER NOT NULL REFERENCES driver (id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL,      -- 1-based, kept even when the time is NULL
    time_ms    INTEGER,
    is_best    INTEGER NOT NULL DEFAULT 0 CHECK (is_best IN (0, 1)),
    UNIQUE (session_id, driver_id, lap_number)
);

CREATE INDEX IF NOT EXISTS ix_lap_session ON lap (session_id);
CREATE INDEX IF NOT EXISTS ix_lap_driver ON lap (driver_id);

-- Sectors live in their own table rather than in a JSON column: their count is
-- track-dependent, they arrive incrementally (Apex only sends them to the
-- recipient of a given email, so a second email may add sectors to laps that
-- already exist), and per-sector merging / "theoretical best" queries stay plain SQL.
CREATE TABLE IF NOT EXISTS lap_sector (
    lap_id       INTEGER NOT NULL REFERENCES lap (id) ON DELETE CASCADE,
    sector_index INTEGER NOT NULL,    -- 1-based: S1, S2, ...
    time_ms      INTEGER,
    PRIMARY KEY (lap_id, sector_index)
);

CREATE TABLE IF NOT EXISTS ranking_entry (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('weekly_best', 'track_record')),
    rank        INTEGER NOT NULL,
    driver_id   INTEGER NOT NULL REFERENCES driver (id) ON DELETE CASCADE,
    best_lap_ms INTEGER,
    category    TEXT,
    UNIQUE (session_id, kind, rank)
);

CREATE INDEX IF NOT EXISTS ix_ranking_entry_session ON ranking_entry (session_id);

CREATE TABLE IF NOT EXISTS history_entry (
    id          INTEGER PRIMARY KEY,
    driver_id   INTEGER NOT NULL REFERENCES driver (id) ON DELETE CASCADE,
    date        TEXT,                 -- 'YYYY-MM-DD', no time of day in the email
    position    INTEGER,
    best_lap_ms INTEGER,
    laps_count  INTEGER,
    category    TEXT
);

-- Several sessions can happen on the same day, so the whole row is the key.
CREATE UNIQUE INDEX IF NOT EXISTS ux_history_entry_key ON history_entry (
    driver_id,
    COALESCE(date, ''),
    COALESCE(position, -1),
    COALESCE(best_lap_ms, -1),
    COALESCE(laps_count, -1),
    COALESCE(category, '')
);

-- --------------------------------------------------------------------------
-- Lap annotations: what a human said ('manual') and what the joker/pit
-- detector proposed ('auto').  Both rows are kept side by side so the UI can
-- show the suggestion *and* the human decision that overrode it; the effective
-- tag set of a lap is derived (SPEC 10.3): as soon as a lap carries a single
-- manual row, every automatic row of that lap is ignored.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lap_annotation (
    id         INTEGER PRIMARY KEY,
    lap_id     INTEGER NOT NULL REFERENCES lap (id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'auto')),
    UNIQUE (lap_id, tag, source)
);

CREATE INDEX IF NOT EXISTS ix_lap_annotation_lap ON lap_annotation (lap_id);
CREATE INDEX IF NOT EXISTS ix_lap_annotation_source ON lap_annotation (source, lap_id);

-- --------------------------------------------------------------------------
-- Provenance
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS email_import (
    id                 INTEGER PRIMARY KEY,
    message_id         TEXT UNIQUE,   -- nullable: HTML-only input has no headers
    sha256             TEXT UNIQUE,   -- sha256 of the raw .eml
    content_sha256     TEXT UNIQUE,   -- sha256 of the parsed payload, provenance excluded
    source_path        TEXT,
    raw_path           TEXT,          -- copy under data/raw_emails/<sha256>.eml
    subject            TEXT,
    sent_at            TEXT,
    recipient_email    TEXT,
    recipient_nickname TEXT,
    session_id         INTEGER REFERENCES session (id) ON DELETE SET NULL,
    imported_at        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'imported',
    warnings           TEXT NOT NULL DEFAULT '[]'   -- JSON array of parser warnings
);

CREATE INDEX IF NOT EXISTS ix_email_import_session ON email_import (session_id);

-- Durable log of everything the merge refused to overwrite.
CREATE TABLE IF NOT EXISTS import_conflict (
    id              INTEGER PRIMARY KEY,
    email_import_id INTEGER REFERENCES email_import (id) ON DELETE CASCADE,
    session_id      INTEGER REFERENCES session (id) ON DELETE CASCADE,
    entity          TEXT NOT NULL,    -- 'lap' | 'session_entry' | 'session' | ...
    ref             TEXT,             -- human readable row reference
    field           TEXT NOT NULL,
    stored_value    TEXT,
    incoming_value  TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_import_conflict_import ON import_conflict (email_import_id);
