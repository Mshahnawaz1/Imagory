"""SQLite connection setup, schema migrations, and all SQL queries."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)


# --- connection -------------------------------------------------------------------------


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the library's standard settings.

    The connection is in autocommit mode; group writes with :func:`transaction`.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def utc_now() -> str:
    """Current UTC time as ISO 8601 text ending in ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- migrations -------------------------------------------------------------------------
# Each migration moves the schema from version N-1 to N inside one transaction. Never edit a
# released migration; append a new one instead.


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


def _migration_1(conn: sqlite3.Connection) -> None:
    """V1 core schema, used by V0 with one source and empty hash columns."""
    _execute_script(
        conn,
        """
        CREATE TABLE sources (
            id              INTEGER PRIMARY KEY,
            label           TEXT NOT NULL,
            root_path       TEXT NOT NULL,
            type            TEXT NOT NULL DEFAULT 'plain' CHECK (type IN ('plain', 'takeout')),
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
            longitude       REAL
        );

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
            skipped_by_ext TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX idx_photos_taken_at_local ON photos(taken_at_local);
        CREATE INDEX idx_photos_primary_file_id ON photos(primary_file_id);
        CREATE INDEX idx_files_photo_id ON files(photo_id);
        CREATE INDEX idx_files_content_hash ON files(content_hash);
        CREATE INDEX idx_files_source_status ON files(source_id, status);
        CREATE INDEX idx_scan_runs_source_id ON scan_runs(source_id);
        """,
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migration_1]
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, db_file: Path | None = None) -> int:
    """Bring the database up to :data:`SCHEMA_VERSION` and return the new version.

    An existing database is backed up to ``<db_file>.bak`` before the first step.
    """
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this Imagory "
            f"(supports {SCHEMA_VERSION}). Please update Imagory."
        )
    if current == SCHEMA_VERSION:
        return current
    if current > 0 and db_file is not None:
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
            MIGRATIONS[version - 1](conn)
            conn.execute(f"PRAGMA user_version = {version}")
    return SCHEMA_VERSION


def open_database(db_file: Path) -> sqlite3.Connection:
    """Connect to the library database, creating or migrating it as needed."""
    conn = connect(db_file)
    migrate(conn, db_file)
    return conn


# --- sources ----------------------------------------------------------------------------


SOURCE_TYPES = ("plain", "takeout")
_SOURCE_COLUMNS = "id, label, root_path, type, type_overridden, added_at, last_scan_at"


@dataclass(frozen=True)
class Source:
    id: int
    label: str
    root_path: str
    type: str  # 'plain' | 'takeout'
    type_overridden: bool  # the user chose a type other than the detected one
    added_at: str
    last_scan_at: str | None


def _source(row: sqlite3.Row) -> Source:
    return Source(
        row["id"],
        row["label"],
        row["root_path"],
        row["type"],
        bool(row["type_overridden"]),
        row["added_at"],
        row["last_scan_at"],
    )


def list_sources(conn: sqlite3.Connection) -> list[Source]:
    """All sources in the order they were added."""
    rows = conn.execute(f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY added_at, id")
    return [_source(r) for r in rows]


def get_source(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute(
        f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    return _source(row) if row else None


def create_source(
    conn: sqlite3.Connection,
    root_path: str,
    label: str,
    type: str = "plain",
    type_overridden: bool = False,
) -> Source:
    if type not in SOURCE_TYPES:
        raise ValueError(f"Unknown source type: {type}")
    cur = conn.execute(
        "INSERT INTO sources (label, root_path, type, type_overridden, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (label, root_path, type, int(type_overridden), utc_now()),
    )
    source = get_source(conn, cur.lastrowid)
    assert source is not None
    return source


def rename_source(conn: sqlite3.Connection, source_id: int, label: str) -> None:
    conn.execute("UPDATE sources SET label = ? WHERE id = ?", (label, source_id))


def set_source_type(conn: sqlite3.Connection, source_id: int, type: str) -> None:
    """Record a type the user chose, so it is not replaced by detection."""
    if type not in SOURCE_TYPES:
        raise ValueError(f"Unknown source type: {type}")
    conn.execute("UPDATE sources SET type = ?, type_overridden = 1 WHERE id = ?", (type, source_id))


def relocate_source(conn: sqlite3.Connection, source_id: int, root_path: str) -> None:
    """Point a source at its new folder. Paths are relative, so nothing else changes."""
    conn.execute("UPDATE sources SET root_path = ? WHERE id = ?", (root_path, source_id))


@dataclass(frozen=True)
class SourceStats:
    photos: int = 0  # files present on disk at the last scan
    missing: int = 0


def source_stats(conn: sqlite3.Connection) -> dict[int, SourceStats]:
    """File counts per source; sources without files are absent from the result."""
    rows = conn.execute(
        "SELECT source_id, SUM(status = 'ok'), SUM(status = 'missing') FROM files "
        "GROUP BY source_id"
    )
    return {r[0]: SourceStats(r[1], r[2]) for r in rows}


def source_file_paths(conn: sqlite3.Connection, source_id: int, limit: int) -> list[str]:
    """A sample of a source's relative paths, e.g. to check a relocation target."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT relative_path FROM files WHERE source_id = ? ORDER BY id LIMIT ?",
            (source_id, limit),
        )
    ]


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Delete a source with its files, scan runs, and photos left without files.

    Only ever called after the user confirms. Files on disk are not touched.
    """
    # Albums (PRD §8.8) do not exist yet; their memberships must go with the photos here.
    with transaction(conn):
        photo_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT photo_id FROM files WHERE source_id = ? AND photo_id IS NOT NULL",
                (source_id,),
            )
        ]
        conn.execute("DELETE FROM files WHERE source_id = ?", (source_id,))
        conn.executemany(
            "DELETE FROM photos WHERE id = ? AND NOT EXISTS "
            "(SELECT 1 FROM files WHERE files.photo_id = photos.id)",
            ((pid,) for pid in photo_ids),
        )
        conn.execute("DELETE FROM scan_runs WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# --- files (scan) -----------------------------------------------------------------------


@dataclass(frozen=True)
class FileState:
    """What the scan diff needs to know about a file row."""

    id: int
    relative_path: str
    size: int
    modified_time: int
    status: str


def load_file_states(conn: sqlite3.Connection, source_id: int) -> list[FileState]:
    return [
        FileState(*row)
        for row in conn.execute(
            "SELECT id, relative_path, size, modified_time, status FROM files WHERE source_id = ?",
            (source_id,),
        )
    ]


def mark_missing(conn: sqlite3.Connection, file_ids: Iterable[int]) -> None:
    conn.executemany(
        "UPDATE files SET status = 'missing' WHERE id = ?", ((fid,) for fid in file_ids)
    )


def mark_ok(conn: sqlite3.Connection, file_ids: Iterable[int]) -> None:
    conn.executemany("UPDATE files SET status = 'ok' WHERE id = ?", ((fid,) for fid in file_ids))


def update_relative_paths(conn: sqlite3.Connection, changes: Iterable[tuple[int, str]]) -> None:
    """Record a new on-disk case for paths that only changed case."""
    conn.executemany(
        "UPDATE files SET relative_path = ? WHERE id = ?", ((path, fid) for fid, path in changes)
    )


def touch_seen(conn: sqlite3.Connection, source_id: int, seen_at: str) -> None:
    conn.execute(
        "UPDATE files SET last_seen_at = ? WHERE source_id = ? AND status = 'ok'",
        (seen_at, source_id),
    )


@dataclass(frozen=True)
class FileRecord:
    """A processed file ready to store. Mirrors the worker's result."""

    relative_path: str
    size: int
    modified_time: int
    width: int
    height: int
    format: str
    taken_at_local: str
    date_source: str


def insert_file(conn: sqlite3.Connection, source_id: int, rec: FileRecord, now: str) -> int:
    """Insert a new file and its photo (one photo per file in V0). Returns the file id."""
    photo_id = conn.execute(
        "INSERT INTO photos (width, height, format, taken_at_local, date_source) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec.width, rec.height, rec.format, rec.taken_at_local, rec.date_source),
    ).lastrowid
    file_id = conn.execute(
        "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, status, "
        "taken_at_local, date_source, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)",
        (
            source_id,
            photo_id,
            rec.relative_path,
            rec.size,
            rec.modified_time,
            rec.taken_at_local,
            rec.date_source,
            now,
            now,
        ),
    ).lastrowid
    conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (file_id, photo_id))
    return file_id


def update_file(conn: sqlite3.Connection, file_id: int, rec: FileRecord, now: str) -> None:
    """Store reprocessed results for a changed (or reappeared and changed) file."""
    conn.execute(
        "UPDATE files SET relative_path = ?, size = ?, modified_time = ?, status = 'ok', "
        "taken_at_local = ?, date_source = ?, last_seen_at = ? WHERE id = ?",
        (
            rec.relative_path,
            rec.size,
            rec.modified_time,
            rec.taken_at_local,
            rec.date_source,
            now,
            file_id,
        ),
    )
    photo_id = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    if photo_id is None:
        photo_id = conn.execute("INSERT INTO photos (primary_file_id) VALUES (?)", (file_id,))
        photo_id = photo_id.lastrowid
        conn.execute("UPDATE files SET photo_id = ? WHERE id = ?", (photo_id, file_id))
    conn.execute(
        "UPDATE photos SET width = ?, height = ?, format = ?, taken_at_local = ?, "
        "date_source = ?, primary_file_id = ? WHERE id = ?",
        (
            rec.width,
            rec.height,
            rec.format,
            rec.taken_at_local,
            rec.date_source,
            file_id,
            photo_id,
        ),
    )


# --- scan runs --------------------------------------------------------------------------


@dataclass
class ScanCounts:
    found: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: int = 0
    skipped_by_ext: dict[str, int] | None = None


def start_scan_run(conn: sqlite3.Connection, source_id: int, started_at: str) -> int:
    return conn.execute(
        "INSERT INTO scan_runs (source_id, started_at, status) VALUES (?, ?, 'running')",
        (source_id, started_at),
    ).lastrowid


def finish_scan_run(
    conn: sqlite3.Connection, run_id: int, source_id: int, status: str, counts: ScanCounts
) -> None:
    finished = utc_now()
    with transaction(conn):
        conn.execute(
            "UPDATE scan_runs SET finished_at = ?, status = ?, found = ?, added = ?, updated = ?, "
            "unchanged = ?, missing = ?, errors = ?, skipped_by_ext = ? WHERE id = ?",
            (
                finished,
                status,
                counts.found,
                counts.added,
                counts.updated,
                counts.unchanged,
                counts.missing,
                counts.errors,
                json.dumps(counts.skipped_by_ext or {}, sort_keys=True),
                run_id,
            ),
        )
        conn.execute("UPDATE sources SET last_scan_at = ? WHERE id = ?", (finished, source_id))


def scan_runs(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scan_runs WHERE source_id = ? ORDER BY id", (source_id,)
    ).fetchall()


# --- browsing ---------------------------------------------------------------------------


class GridPhoto(NamedTuple):
    # A NamedTuple, not a dataclass: building 100,000 of them must stay fast.
    photo_id: int
    source_id: int
    relative_path: str
    width: int | None
    height: int | None
    format: str | None
    taken_at_local: str | None
    date_source: str | None


def grid_photos(
    conn: sqlite3.Connection, limit: int | None = None, offset: int = 0
) -> list[GridPhoto]:
    """Visible photos of all sources, newest first, ties broken by relative path, then
    source. Missing files are hidden.

    ``limit`` and ``offset`` let the grid show its first screen before loading the rest.
    """
    cur = conn.cursor()
    cur.row_factory = None  # plain tuples are much cheaper than sqlite3.Row here
    cur.execute(
        "SELECT p.id, f.source_id, f.relative_path, p.width, p.height, p.format, "
        # CROSS JOIN makes SQLite walk photos in taken_at_local order, so a LIMIT stops early.
        "p.taken_at_local, p.date_source FROM photos p CROSS JOIN files f "
        "ON f.id = p.primary_file_id WHERE f.status = 'ok' "
        "ORDER BY p.taken_at_local DESC, f.relative_path, f.source_id LIMIT ? OFFSET ?",
        (-1 if limit is None else limit, offset),
    )
    return list(map(GridPhoto._make, cur))


def grid_photos_for_files(conn: sqlite3.Connection, file_ids: list[int]) -> list[GridPhoto]:
    """Grid rows for specific files, e.g. the ones a scan batch just added (unordered)."""
    if not file_ids:
        return []
    cur = conn.cursor()
    cur.row_factory = None
    placeholders = ",".join("?" * len(file_ids))
    cur.execute(
        "SELECT p.id, f.source_id, f.relative_path, p.width, p.height, p.format, "
        "p.taken_at_local, p.date_source FROM photos p JOIN files f ON f.id = p.primary_file_id "
        f"WHERE f.id IN ({placeholders}) AND f.status = 'ok'",
        file_ids,
    )
    return list(map(GridPhoto._make, cur))


# --- settings ---------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
