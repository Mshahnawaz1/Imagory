"""The baseline schema, numbered migrations, and opening (creating or migrating) a database."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from imagory.db.connection import connect, transaction

log = logging.getLogger(__name__)


# A new database is created from _SCHEMA at BASELINE_VERSION. Every later change is a step
# appended to MIGRATIONS that moves the schema up by one version inside one transaction.
# Never edit a step once it has shipped; append a new one instead.


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Run several statements without ``executescript``, which would commit the transaction."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    if buffer.strip():
        raise ValueError(f"Incomplete SQL statement: {buffer.strip()[:80]}")


# Column order matches the databases built during development, so every database at the
# baseline has the same layout.
_SCHEMA = """
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    root_path       TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'plain'
                    CHECK (type IN ('plain', 'takeout', 'cleaned')),
    type_overridden INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL,
    last_scan_at    TEXT
);

CREATE TABLE photos (
    id              INTEGER PRIMARY KEY,
    content_hash    TEXT UNIQUE,
    primary_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    width           INTEGER,
    height          INTEGER,
    format          TEXT,
    taken_at_local  TEXT,
    taken_at_utc    TEXT,
    date_source     TEXT,
    latitude        REAL,
    longitude       REAL,
    description     TEXT,
    favorite        INTEGER NOT NULL DEFAULT 0,
    uploaded_at_utc TEXT
);

-- exif_taken_at and exif_offset keep the raw date inputs, so dates can be recomputed
-- (e.g. on a source type change) without rescanning.
CREATE TABLE files (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    photo_id              INTEGER REFERENCES photos(id) ON DELETE SET NULL,
    relative_path         TEXT NOT NULL COLLATE NOCASE,
    size                  INTEGER NOT NULL,
    modified_time         INTEGER NOT NULL,
    content_hash          TEXT,
    status                TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'missing')),
    taken_at_local        TEXT,
    taken_at_utc          TEXT,
    date_source           TEXT,
    sidecar_relative_path TEXT,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    exif_taken_at         TEXT,
    exif_offset           TEXT,
    UNIQUE (source_id, relative_path)
);

CREATE TABLE scan_runs (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    found          INTEGER NOT NULL DEFAULT 0,
    added          INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    unchanged      INTEGER NOT NULL DEFAULT 0,
    missing        INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    skipped_by_ext TEXT NOT NULL DEFAULT '{}',
    moved          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Deleting an album or a photo removes memberships only (PRD §8.8).
CREATE TABLE albums (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);

CREATE TABLE album_photos (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (album_id, photo_id)
);

CREATE TABLE people (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE photo_people (
    photo_id  INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (photo_id, person_id)
);

-- Takeout cleanup (PRD §8.10). Present in every database, used only in output databases.
CREATE TABLE cleanup_runs (
    id            INTEGER PRIMARY KEY,
    input_path    TEXT NOT NULL COLLATE NOCASE,
    input_label   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'analysing'
                  CHECK (status IN ('analysing', 'planned', 'copying', 'done',
                                    'cancelled')),
    photos        INTEGER NOT NULL DEFAULT 0,
    other_files   INTEGER NOT NULL DEFAULT 0,
    duplicates    INTEGER NOT NULL DEFAULT 0,
    trash_skipped INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    bytes_copied  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE extra_files (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    relative_path   TEXT NOT NULL COLLATE NOCASE,
    size            INTEGER NOT NULL,
    modified_time   INTEGER NOT NULL,
    content_hash    TEXT,
    extension       TEXT NOT NULL,
    taken_at_local  TEXT,
    taken_at_utc    TEXT,
    date_source     TEXT,
    latitude        REAL,
    longitude       REAL,
    description     TEXT,
    favorite        INTEGER NOT NULL DEFAULT 0,
    uploaded_at_utc TEXT,
    people          TEXT NOT NULL DEFAULT '[]',
    UNIQUE (source_id, relative_path)
);

-- file_id / extra_file_id point at the kept row (the one that ends up in the cleaned
-- source). No ON DELETE action: deleting a row that still has origins fails.
CREATE TABLE file_origins (
    id                     INTEGER PRIMARY KEY,
    run_id                 INTEGER NOT NULL REFERENCES cleanup_runs(id),
    file_id                INTEGER REFERENCES files(id),
    extra_file_id          INTEGER REFERENCES extra_files(id),
    original_relative_path TEXT NOT NULL COLLATE NOCASE,
    sidecar_relative_path  TEXT,
    album_folder           TEXT,
    CHECK ((file_id IS NULL) <> (extra_file_id IS NULL))
);

CREATE TABLE cleanup_copies (
    id                   INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL REFERENCES cleanup_runs(id),
    file_id              INTEGER REFERENCES files(id),
    extra_file_id        INTEGER REFERENCES extra_files(id),
    source_relative_path TEXT NOT NULL COLLATE NOCASE,
    target_relative_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'done', 'error')),
    error                TEXT,
    CHECK ((file_id IS NULL) <> (extra_file_id IS NULL))
);

CREATE INDEX idx_photos_taken_at_local ON photos(taken_at_local);
CREATE INDEX idx_photos_primary_file_id ON photos(primary_file_id);
CREATE INDEX idx_files_photo_id ON files(photo_id);
CREATE INDEX idx_files_content_hash ON files(content_hash);
CREATE INDEX idx_files_source_status ON files(source_id, status);
CREATE INDEX idx_files_source_hash ON files(source_id, content_hash);
CREATE INDEX idx_scan_runs_source_id ON scan_runs(source_id);
CREATE INDEX idx_album_photos_photo_id ON album_photos(photo_id);
CREATE INDEX idx_photo_people_person_id ON photo_people(person_id);
CREATE INDEX idx_file_origins_file_id ON file_origins(file_id);
CREATE INDEX idx_file_origins_extra_file_id ON file_origins(extra_file_id);
CREATE INDEX idx_extra_files_content_hash ON extra_files(content_hash);
CREATE INDEX idx_cleanup_copies_run_status ON cleanup_copies(run_id, status);
"""

# The version _SCHEMA creates. Databases below it come from development builds and are
# replaced by :func:`open_database` instead of being migrated.
BASELINE_VERSION = 5
# MIGRATIONS[i] moves the schema from BASELINE_VERSION + i to BASELINE_VERSION + i + 1.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = []
SCHEMA_VERSION = BASELINE_VERSION + len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, db_file: Path | None = None) -> int:
    """Create or upgrade the schema to :data:`SCHEMA_VERSION` and return the new version.

    An empty database gets :data:`_SCHEMA` directly. An existing database is backed up to
    ``<db_file>.bak`` before the first migration step.
    """
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this Imagory "
            f"(supports {SCHEMA_VERSION}). Please update Imagory."
        )
    if current == SCHEMA_VERSION:
        return current
    if current == 0:
        log.info("Creating database schema version %d", BASELINE_VERSION)
        with transaction(conn):
            _execute_script(conn, _SCHEMA)
            conn.execute(f"PRAGMA user_version = {BASELINE_VERSION}")
        current = BASELINE_VERSION
    elif current < BASELINE_VERSION:
        raise RuntimeError(f"Database schema version {current} is too old to migrate")
    elif db_file is not None:
        backup = Path(str(db_file) + ".bak")
        log.info("Backing up database to %s before migrating", backup)
        dest = sqlite3.connect(str(backup))
        try:
            conn.backup(dest)
        finally:
            dest.close()
    for version in range(current + 1, SCHEMA_VERSION + 1):
        log.info("Migrating database to schema version %d", version)
        with transaction(conn):
            MIGRATIONS[version - BASELINE_VERSION - 1](conn)
            conn.execute(f"PRAGMA user_version = {version}")
    return SCHEMA_VERSION


def open_database(db_file: Path) -> sqlite3.Connection:
    """Connect to the library database, creating or migrating it as needed.

    A database older than :data:`BASELINE_VERSION` comes from a development build and is
    replaced by a fresh one instead of being migrated.
    """
    conn = connect(db_file)
    if 0 < schema_version(conn) < BASELINE_VERSION:
        log.warning("Replacing the development database %s with a new one", db_file)
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_file) + suffix).unlink(missing_ok=True)
        conn = connect(db_file)
    migrate(conn, db_file)
    return conn
