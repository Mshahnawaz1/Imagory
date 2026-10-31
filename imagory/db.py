"""SQLite connection setup, schema migrations, and all SQL queries."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from imagory.dates import SOURCE_RANK, DateResult, best_photo_date

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
    """Core schema: sources, photos, files, scan runs, settings."""
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


def _migration_2(conn: sqlite3.Connection) -> None:
    """Manual albums (PRD §8.8). Deleting an album or a photo removes memberships only."""
    _execute_script(
        conn,
        """
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

        CREATE INDEX idx_album_photos_photo_id ON album_photos(photo_id);
        """,
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    """V1 duplicates, moves, and dates (PRD §8.2, §8.4, §8.5). Schema only.

    The EXIF columns keep the raw date inputs, so dates can be recomputed (e.g. on a
    source type change) without rescanning.
    """
    _execute_script(
        conn,
        """
        ALTER TABLE files ADD COLUMN exif_taken_at TEXT;
        ALTER TABLE files ADD COLUMN exif_offset TEXT;
        ALTER TABLE scan_runs ADD COLUMN moved INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX idx_files_source_hash ON files(source_id, content_hash);
        """,
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migration_1, _migration_2, _migration_3]
SCHEMA_VERSION = len(MIGRATIONS)
# V0 and development databases (versions 1 and 2) were never released and are replaced.
FIRST_SUPPORTED_VERSION = 3


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
    """Connect to the library database, creating or migrating it as needed.

    A database older than :data:`FIRST_SUPPORTED_VERSION` (V0 and development builds, which
    were never released) is replaced by a fresh one instead of being migrated.
    """
    conn = connect(db_file)
    if 0 < schema_version(conn) < FIRST_SUPPORTED_VERSION:
        log.warning("Replacing the pre-release database %s with a new one", db_file)
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_file) + suffix).unlink(missing_ok=True)
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

    Photos that keep files in other sources get their primary copy and date recomputed.
    Only ever called after the user confirms. Files on disk are not touched.
    """
    # Album memberships (PRD §8.8) go with the photos via ON DELETE CASCADE.
    with transaction(conn):
        photo_ids = _photo_ids_of(conn, "source_id = ?", (source_id,))
        conn.execute("DELETE FROM files WHERE source_id = ?", (source_id,))
        refresh_photos(conn, photo_ids)
        conn.execute("DELETE FROM scan_runs WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def source_orphan_hashes(conn: sqlite3.Connection, source_id: int) -> list[str]:
    """Hashes of photos whose files are all in this source, i.e. the photos (and
    thumbnails) that removing the source would leave unreferenced."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT p.content_hash FROM photos p WHERE p.content_hash IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM files f WHERE f.photo_id = p.id AND f.source_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.photo_id = p.id "
            "AND f.source_id <> ?)",
            (source_id, source_id),
        )
    ]


def missing_orphan_hashes(conn: sqlite3.Connection, source_id: int) -> list[str]:
    """Hashes of photos whose only files are this source's missing files, i.e. the
    photos (and thumbnails) that :func:`delete_missing_files` would remove."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT p.content_hash FROM photos p WHERE p.content_hash IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM files f WHERE f.photo_id = p.id AND f.source_id = ? "
            "AND f.status = 'missing') "
            "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.photo_id = p.id "
            "AND (f.source_id <> ? OR f.status <> 'missing'))",
            (source_id, source_id),
        )
    ]


@dataclass(frozen=True)
class RemovedMissing:
    files: int
    photos: int


def delete_missing_files(conn: sqlite3.Connection, source_id: int) -> RemovedMissing:
    """Delete a source's missing file rows and the photos left with no files (PRD §8.2).

    Only ever called after the user confirms; never automatic. Files on disk are not
    touched. Photos keeping other files get their primary copy and date recomputed.
    """
    with transaction(conn):
        photo_ids = _photo_ids_of(conn, "source_id = ? AND status = 'missing'", (source_id,))
        files = conn.execute(
            "DELETE FROM files WHERE source_id = ? AND status = 'missing'", (source_id,)
        ).rowcount
        photos = refresh_photos(conn, photo_ids)
    return RemovedMissing(files, photos)


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
    """Mark files missing; a missing primary copy hands over to the next copy."""
    file_ids = list(file_ids)
    conn.executemany(
        "UPDATE files SET status = 'missing' WHERE id = ?", ((fid,) for fid in file_ids)
    )
    _refresh_primaries(conn, _photo_ids_of_files(conn, file_ids))


def mark_ok(conn: sqlite3.Connection, file_ids: Iterable[int]) -> None:
    """Mark files present again; the primary copy is recomputed."""
    file_ids = list(file_ids)
    conn.executemany("UPDATE files SET status = 'ok' WHERE id = ?", ((fid,) for fid in file_ids))
    _refresh_primaries(conn, _photo_ids_of_files(conn, file_ids))


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
    """A processed file ready to store. Mirrors the worker's result.

    ``content_hash`` None (tests only) always creates a new photo.
    """

    relative_path: str
    size: int
    modified_time: int
    width: int
    height: int
    format: str
    taken_at_local: str
    date_source: str
    content_hash: str | None = None
    taken_at_utc: str | None = None
    exif_taken_at: str | None = None  # raw date inputs, kept so dates can be recomputed
    exif_offset: str | None = None
    sidecar_relative_path: str | None = None  # Takeout only (wave 2)


def find_moved_file(conn: sqlite3.Connection, source_id: int, content_hash: str) -> int | None:
    """A missing file of this source with the same contents as a new file, if any.

    A new file matching one is a move (PRD §8.2): the old row is re-pointed. Any missing
    row counts, not only ones that disappeared in this scan, so a move is still found
    after a cancelled scan without saved state. Oldest row first, for determinism.
    """
    row = conn.execute(
        "SELECT id FROM files WHERE source_id = ? AND content_hash = ? AND status = 'missing' "
        "ORDER BY first_seen_at, id LIMIT 1",
        (source_id, content_hash),
    ).fetchone()
    return row[0] if row else None


def insert_file(conn: sqlite3.Connection, source_id: int, rec: FileRecord, now: str) -> int:
    """Insert a new file and attach it to the photo with its hash (created if needed).

    Returns the file id. The photo's primary copy and date are recomputed.
    """
    photo_id = _photo_for_hash(conn, rec.content_hash)
    if photo_id is None:
        photo_id = _create_photo(conn, rec)
    file_id = conn.execute(
        "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, "
        "content_hash, status, taken_at_local, taken_at_utc, date_source, exif_taken_at, "
        "exif_offset, sidecar_relative_path, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            photo_id,
            rec.relative_path,
            rec.size,
            rec.modified_time,
            rec.content_hash,
            rec.taken_at_local,
            rec.taken_at_utc,
            rec.date_source,
            rec.exif_taken_at,
            rec.exif_offset,
            rec.sidecar_relative_path,
            now,
            now,
        ),
    ).lastrowid
    refresh_photos(conn, [photo_id])
    return file_id


def update_file(conn: sqlite3.Connection, file_id: int, rec: FileRecord, now: str) -> None:
    """Store reprocessed results for a changed, reappeared, or moved file.

    The file keeps its id and ``first_seen_at``. If its hash changed it moves to the
    photo with the new hash. When it was its photo's only file and no photo has the new
    hash yet, that photo is re-keyed instead, so it keeps its id (and album memberships).
    A photo left with no files is deleted.
    """
    row = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()
    old_photo = row[0] if row else None
    photo_id = _photo_for_hash(conn, rec.content_hash)
    if photo_id is None:
        sole = (
            old_photo is not None
            and not conn.execute(
                "SELECT 1 FROM files WHERE photo_id = ? AND id <> ? LIMIT 1", (old_photo, file_id)
            ).fetchone()
        )
        if sole:
            photo_id = old_photo
            conn.execute(
                "UPDATE photos SET content_hash = ?, width = ?, height = ?, format = ? "
                "WHERE id = ?",
                (rec.content_hash, rec.width, rec.height, rec.format, photo_id),
            )
        else:
            photo_id = _create_photo(conn, rec)
    conn.execute(
        "UPDATE files SET photo_id = ?, relative_path = ?, size = ?, modified_time = ?, "
        "content_hash = ?, status = 'ok', taken_at_local = ?, taken_at_utc = ?, "
        "date_source = ?, exif_taken_at = ?, exif_offset = ?, sidecar_relative_path = ?, "
        "last_seen_at = ? WHERE id = ?",
        (
            photo_id,
            rec.relative_path,
            rec.size,
            rec.modified_time,
            rec.content_hash,
            rec.taken_at_local,
            rec.taken_at_utc,
            rec.date_source,
            rec.exif_taken_at,
            rec.exif_offset,
            rec.sidecar_relative_path,
            now,
            file_id,
        ),
    )
    if (
        old_photo is not None
        and old_photo != photo_id
        and not conn.execute(
            "SELECT 1 FROM files WHERE photo_id = ? LIMIT 1", (old_photo,)
        ).fetchone()
    ):
        # The old photo is about to be deleted; its album memberships follow the file.
        conn.execute(
            "INSERT OR IGNORE INTO album_photos (album_id, photo_id, added_at) "
            "SELECT album_id, ?, added_at FROM album_photos WHERE photo_id = ?",
            (photo_id, old_photo),
        )
    refresh_photos(conn, [pid for pid in dict.fromkeys((photo_id, old_photo)) if pid is not None])


def set_photo_location(
    conn: sqlite3.Connection, file_id: int, latitude: float | None, longitude: float | None
) -> None:
    """Store a Takeout location on the photo of a file. No location never clears one."""
    if latitude is None or longitude is None:
        return
    conn.execute(
        "UPDATE photos SET latitude = ?, longitude = ? "
        "WHERE id = (SELECT photo_id FROM files WHERE id = ?)",
        (latitude, longitude, file_id),
    )


class DateInputs(NamedTuple):
    """What a file's date is computed from (PRD §8.4), as stored."""

    id: int
    relative_path: str
    modified_time: int
    exif_taken_at: str | None
    exif_offset: str | None
    sidecar_relative_path: str | None


class RecomputedDate(NamedTuple):
    file_id: int
    taken_at_local: str
    taken_at_utc: str | None
    date_source: str
    sidecar_relative_path: str | None
    latitude: float | None = None
    longitude: float | None = None


def date_inputs(conn: sqlite3.Connection, source_id: int) -> list[DateInputs]:
    """Every file row of a source (missing ones too) with its stored date inputs."""
    return [
        DateInputs(*r)
        for r in conn.execute(
            "SELECT id, relative_path, modified_time, exif_taken_at, exif_offset, "
            "sidecar_relative_path FROM files WHERE source_id = ? ORDER BY relative_path, id",
            (source_id,),
        )
    ]


def apply_recomputed_dates(
    conn: sqlite3.Connection, source_id: int, updates: Iterable[RecomputedDate]
) -> int:
    """Store recomputed file dates and refresh the dates of their photos. Returns the
    number of files whose date, date source or sidecar changed."""
    changed = 0
    photo_ids: set[int] = set()
    for u in updates:
        row = conn.execute(
            "SELECT photo_id, taken_at_local, taken_at_utc, date_source, sidecar_relative_path "
            "FROM files WHERE id = ? AND source_id = ?",
            (u.file_id, source_id),
        ).fetchone()
        if row is None:
            continue
        if tuple(row[1:]) != (
            u.taken_at_local,
            u.taken_at_utc,
            u.date_source,
            u.sidecar_relative_path,
        ):
            changed += 1
            conn.execute(
                "UPDATE files SET taken_at_local = ?, taken_at_utc = ?, date_source = ?, "
                "sidecar_relative_path = ? WHERE id = ?",
                (
                    u.taken_at_local,
                    u.taken_at_utc,
                    u.date_source,
                    u.sidecar_relative_path,
                    u.file_id,
                ),
            )
        if row[0] is not None:
            photo_ids.add(row[0])
            set_photo_location(conn, u.file_id, u.latitude, u.longitude)
    refresh_photos(conn, photo_ids)
    return changed


def visible_photo_ids(conn: sqlite3.Connection, hashes: Iterable[str]) -> set[int]:
    """Photos with these hashes that the grid shows now (their primary copy is ok)."""
    hashes = list(set(hashes))
    found: set[int] = set()
    for start in range(0, len(hashes), 500):
        chunk = hashes[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        found.update(
            r[0]
            for r in conn.execute(
                "SELECT p.id FROM photos p JOIN files f ON f.id = p.primary_file_id "
                f"WHERE p.content_hash IN ({placeholders}) AND f.status = 'ok'",
                chunk,
            )
        )
    return found


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
    moved: int = 0  # new paths re-pointed to a missing row with the same hash (PRD §8.2)


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
            "unchanged = ?, missing = ?, errors = ?, skipped_by_ext = ?, moved = ? WHERE id = ?",
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
                counts.moved,
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
    content_hash: str | None = None  # thumbnail key; None for a row stored without one


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
        "p.taken_at_local, p.date_source, p.content_hash FROM photos p CROSS JOIN files f "
        "ON f.id = p.primary_file_id WHERE f.status = 'ok' "
        "ORDER BY p.taken_at_local DESC, f.relative_path, f.source_id LIMIT ? OFFSET ?",
        (-1 if limit is None else limit, offset),
    )
    return list(map(GridPhoto._make, cur))


def month_counts(conn: sqlite3.Connection) -> list[tuple[str | None, int]]:
    """Photos per month over exactly the rows of :func:`grid_photos`, in grid order:
    ``("YYYY-MM", count)`` newest first, then ``(None, count)`` for "Unknown date" when
    there are photos without a date. Feeds the grid's month headers (PRD §8.6).

    Counts all photos per month with seeks on the ``taken_at_local`` index (one range per
    month, no table rows read), then subtracts the photos the grid hides: those without a
    primary file or whose primary file is missing. About 25 ms at 100,000 photos, where
    grouping the joined rows takes about 185 ms.
    """
    cur = conn.cursor()
    cur.row_factory = None
    # Walk the months newest first: each step seeks to the latest date before the month
    # just counted. '~' sorts after every character of an ISO date.
    cur.execute(
        """
        WITH RECURSIVE m(month) AS (
            SELECT substr(MAX(taken_at_local), 1, 7) FROM photos
            UNION ALL
            SELECT (SELECT substr(MAX(taken_at_local), 1, 7) FROM photos
                    WHERE taken_at_local < m.month)
            FROM m WHERE m.month IS NOT NULL
        )
        SELECT month, (SELECT COUNT(*) FROM photos
                       WHERE taken_at_local >= month AND taken_at_local < month || '~')
        FROM m WHERE month IS NOT NULL
        UNION ALL
        SELECT NULL, COUNT(*) FROM photos WHERE taken_at_local IS NULL
        """
    )
    counts = cur.fetchall()
    # Hidden photos, by index too: few in a healthy library (the files of an unplugged
    # drive are not marked missing).
    cur.execute(
        "SELECT substr(taken_at_local, 1, 7), COUNT(*) FROM photos "
        "WHERE primary_file_id IS NULL GROUP BY 1 "
        "UNION ALL "
        "SELECT substr(p.taken_at_local, 1, 7), COUNT(*) FROM files f "
        "CROSS JOIN photos p ON p.primary_file_id = f.id "
        "WHERE f.source_id IN (SELECT id FROM sources) AND f.status = 'missing' GROUP BY 1"
    )
    hidden: dict[str | None, int] = {}
    for month, n in cur:
        hidden[month] = hidden.get(month, 0) + n
    result = [(month, n - hidden.get(month, 0)) for month, n in counts]
    return [(month, n) for month, n in result if n > 0]


def grid_photos_for_files(conn: sqlite3.Connection, file_ids: list[int]) -> list[GridPhoto]:
    """Grid rows for specific files, e.g. the ones a scan batch just added (unordered)."""
    if not file_ids:
        return []
    cur = conn.cursor()
    cur.row_factory = None
    placeholders = ",".join("?" * len(file_ids))
    cur.execute(
        "SELECT p.id, f.source_id, f.relative_path, p.width, p.height, p.format, "
        "p.taken_at_local, p.date_source, p.content_hash FROM photos p "
        "JOIN files f ON f.id = p.primary_file_id "
        f"WHERE f.id IN ({placeholders}) AND f.status = 'ok'",
        file_ids,
    )
    return list(map(GridPhoto._make, cur))


# --- photos (identity, primary copy, dates) -----------------------------------------
# A photo is identified by its content hash (PRD §8.5): all files with that hash belong
# to it. Its primary copy is the first ok file by source added date, then first seen, then
# id. If all its files are missing the first missing one is kept as primary, so the photo
# stays hidden from the grid but can still be shown by a "Missing" filter. Its date is the
# best date across all its files, missing ones included (PRD §8.4).

_PRIMARY_SQL = (
    "UPDATE photos SET primary_file_id = (SELECT f.id FROM files f "
    "JOIN sources s ON s.id = f.source_id WHERE f.photo_id = photos.id "
    "ORDER BY f.status <> 'ok', s.added_at, s.id, f.first_seen_at, f.id LIMIT 1) WHERE id = ?"
)


def _photo_for_hash(conn: sqlite3.Connection, content_hash: str | None) -> int | None:
    if content_hash is None:
        return None
    row = conn.execute("SELECT id FROM photos WHERE content_hash = ?", (content_hash,)).fetchone()
    return row[0] if row else None


def _create_photo(conn: sqlite3.Connection, rec: FileRecord) -> int:
    return conn.execute(
        "INSERT INTO photos (content_hash, width, height, format) VALUES (?, ?, ?, ?)",
        (rec.content_hash, rec.width, rec.height, rec.format),
    ).lastrowid


def _photo_ids_of(conn: sqlite3.Connection, where: str, params: tuple) -> list[int]:
    """Distinct photo ids of the files matching ``where`` (a fixed SQL fragment)."""
    return [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT photo_id FROM files WHERE {where} AND photo_id IS NOT NULL", params
        )
    ]


def _photo_ids_of_files(conn: sqlite3.Connection, file_ids: list[int]) -> set[int]:
    found: set[int] = set()
    for start in range(0, len(file_ids), 500):
        chunk = file_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        found.update(
            r[0]
            for r in conn.execute(
                f"SELECT photo_id FROM files WHERE id IN ({placeholders}) AND photo_id IS NOT NULL",
                chunk,
            )
        )
    return found


def _refresh_primaries(conn: sqlite3.Connection, photo_ids: Iterable[int]) -> None:
    conn.executemany(_PRIMARY_SQL, ((pid,) for pid in photo_ids))


def _refresh_date(conn: sqlite3.Connection, photo_id: int) -> None:
    candidates = [
        DateResult(r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT taken_at_local, taken_at_utc, date_source FROM files WHERE photo_id = ?",
            (photo_id,),
        )
        if r[0] and r[2] in SOURCE_RANK
    ]
    best = best_photo_date(candidates)
    conn.execute(
        "UPDATE photos SET taken_at_local = ?, taken_at_utc = ?, date_source = ? WHERE id = ?",
        (
            best.taken_at_local if best else None,
            best.taken_at_utc if best else None,
            best.date_source if best else None,
            photo_id,
        ),
    )


def refresh_photos(conn: sqlite3.Connection, photo_ids: Iterable[int]) -> int:
    """Recompute the primary copy and date of photos whose files changed, and delete those
    left with no file rows at all (missing rows count as files). Returns how many were
    deleted. Called while a scan applies results and by confirmed user removals."""
    deleted = 0
    for photo_id in photo_ids:
        if not conn.execute(
            "SELECT 1 FROM files WHERE photo_id = ? LIMIT 1", (photo_id,)
        ).fetchone():
            conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
            deleted += 1
            continue
        conn.execute(_PRIMARY_SQL, (photo_id,))
        _refresh_date(conn, photo_id)
    return deleted


# --- albums -----------------------------------------------------------------------------
# Manual albums (PRD §8.8). Albums are a user action outside scans. Membership rows go away
# through ON DELETE CASCADE when an album or a photo is deleted; photos are never touched.


class AlbumNameError(ValueError):
    """The album name is empty or already used (names are unique, case-insensitively)."""


@dataclass(frozen=True)
class Album:
    id: int
    name: str
    created_at: str
    photo_count: int  # visible photos only (same rows the album view shows)


def _clean_album_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise AlbumNameError("The album name cannot be empty.")
    return name


def list_albums(conn: sqlite3.Connection) -> list[Album]:
    """All albums by name, with the number of visible (not missing) photos in each."""
    rows = conn.execute(
        "SELECT a.id, a.name, a.created_at, COUNT(f.id) FROM albums a "
        "LEFT JOIN album_photos ap ON ap.album_id = a.id "
        "LEFT JOIN photos p ON p.id = ap.photo_id "
        "LEFT JOIN files f ON f.id = p.primary_file_id AND f.status = 'ok' "
        "GROUP BY a.id ORDER BY a.name COLLATE NOCASE, a.id"
    )
    return [Album(r[0], r[1], r[2], r[3]) for r in rows]


def create_album(conn: sqlite3.Connection, name: str) -> int:
    name = _clean_album_name(name)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO albums (name, created_at) VALUES (?, ?)", (name, utc_now())
            )
    except sqlite3.IntegrityError as exc:
        raise AlbumNameError(f"An album named '{name}' already exists.") from exc
    assert cur.lastrowid is not None
    return cur.lastrowid


def rename_album(conn: sqlite3.Connection, album_id: int, name: str) -> None:
    name = _clean_album_name(name)
    try:
        with transaction(conn):
            conn.execute("UPDATE albums SET name = ? WHERE id = ?", (name, album_id))
    except sqlite3.IntegrityError as exc:
        raise AlbumNameError(f"An album named '{name}' already exists.") from exc


def delete_album(conn: sqlite3.Connection, album_id: int) -> None:
    """Delete an album and its memberships. Photos and files are not affected."""
    with transaction(conn):
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))


def add_photos_to_album(conn: sqlite3.Connection, album_id: int, photo_ids: Iterable[int]) -> int:
    """Add photos to an album; photos already in it are skipped. Returns how many are new."""
    now = utc_now()
    with transaction(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO album_photos (album_id, photo_id, added_at) VALUES (?, ?, ?)",
            ((album_id, pid, now) for pid in dict.fromkeys(photo_ids)),
        )
        return conn.total_changes - before


def remove_photos_from_album(
    conn: sqlite3.Connection, album_id: int, photo_ids: Iterable[int]
) -> int:
    """Remove photos from an album (the photos themselves stay). Returns how many were in it."""
    with transaction(conn):
        before = conn.total_changes
        conn.executemany(
            "DELETE FROM album_photos WHERE album_id = ? AND photo_id = ?",
            ((album_id, pid) for pid in dict.fromkeys(photo_ids)),
        )
        return conn.total_changes - before


def albums_for_photo(conn: sqlite3.Connection, photo_id: int) -> list[int]:
    """Ids of the albums containing a photo."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT album_id FROM album_photos WHERE photo_id = ? ORDER BY album_id", (photo_id,)
        )
    ]


def album_ids_for_photos(conn: sqlite3.Connection, photo_ids: Iterable[int]) -> dict[int, set[int]]:
    """``{album_id: {photo_ids from the given ones that are in it}}``; empty albums omitted."""
    result: dict[int, set[int]] = {}
    ids = list(dict.fromkeys(photo_ids))
    for start in range(0, len(ids), 500):  # stay under SQLite's variable limit
        chunk = ids[start : start + 500]
        rows = conn.execute(
            "SELECT album_id, photo_id FROM album_photos "
            f"WHERE photo_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        for album_id, photo_id in rows:
            result.setdefault(album_id, set()).add(photo_id)
    return result


def album_grid_photos(
    conn: sqlite3.Connection, album_id: int, limit: int | None = None, offset: int = 0
) -> list[GridPhoto]:
    """Visible photos of one album, with the columns and order of :func:`grid_photos`."""
    cur = conn.cursor()
    cur.row_factory = None
    cur.execute(
        "SELECT p.id, f.source_id, f.relative_path, p.width, p.height, p.format, "
        "p.taken_at_local, p.date_source, p.content_hash FROM album_photos ap "
        "JOIN photos p ON p.id = ap.photo_id CROSS JOIN files f "
        "ON f.id = p.primary_file_id WHERE ap.album_id = ? AND f.status = 'ok' "
        "ORDER BY p.taken_at_local DESC, f.relative_path, f.source_id LIMIT ? OFFSET ?",
        (album_id, -1 if limit is None else limit, offset),
    )
    return list(map(GridPhoto._make, cur))


def album_month_counts(conn: sqlite3.Connection, album_id: int) -> list[tuple[str | None, int]]:
    """``("YYYY-MM", n)`` newest first, then ``(None, n)`` for unknown dates, over the same
    rows as :func:`album_grid_photos`."""
    rows = conn.execute(
        "SELECT substr(p.taken_at_local, 1, 7) AS month, COUNT(*) FROM album_photos ap "
        "JOIN photos p ON p.id = ap.photo_id JOIN files f ON f.id = p.primary_file_id "
        "WHERE ap.album_id = ? AND f.status = 'ok' GROUP BY month ORDER BY month DESC",
        (album_id,),
    ).fetchall()
    dated = [(m, n) for m, n in rows if m is not None]
    undated = [(None, n) for m, n in rows if m is None]
    return dated + undated


# --- search and filters -----------------------------------------------------------------


@dataclass(frozen=True)
class PhotoFilter:
    """What the grid shows. ``PhotoFilter()`` means no filter: the rows of ``grid_photos``.

    All fields combine with AND. The file-level fields (``text``, ``source_ids``,
    ``folder``) must all be satisfied by the *same* copy of a photo.
    """

    text: str = ""  # whitespace-separated words, all must be in the file's relative path
    date_from: str | None = None  # inclusive local date, "YYYY-MM-DD"
    date_to: str | None = None  # inclusive local date, "YYYY-MM-DD"
    source_ids: frozenset[int] = frozenset()  # empty = all sources
    folder: tuple[int, str] | None = None  # (source id, folder prefix with "/" separators)
    has_duplicates: bool = False  # more than one file row
    missing: bool = False  # show only photos whose files are all missing
    album_id: int | None = None  # only photos in this album

    @property
    def is_empty(self) -> bool:
        return self == PhotoFilter()


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


# The row file of a photo: its primary copy, or any copy if the primary is unset.
_ROW_FILE = (
    "f.id = COALESCE(p.primary_file_id, (SELECT MIN(m.id) FROM files m WHERE m.photo_id = p.id))"
)


def _filter_sql(f: PhotoFilter, ordered: bool = False) -> tuple[str, list]:
    """The ``FROM ... WHERE ...`` part shared by the filtered queries, and its parameters.

    CROSS JOIN makes SQLite walk photos in ``taken_at_local`` order, so a LIMIT can stop early.
    A photo is missing when no copy is ``ok``; it is shown by default unless missing, and
    ``missing=True`` flips that. File-level filters are one uncorrelated IN subquery over
    ``files``, so that a single copy has to satisfy them all (and be ``ok``, unless showing
    missing photos).
    """
    where: list[str] = []
    params: list = []

    file_conds: list[str] = []
    file_params: list = []
    if f.source_ids:
        ids = sorted(f.source_ids)
        file_conds.append(f"c.source_id IN ({','.join('?' * len(ids))})")
        file_params.extend(ids)
    if f.folder is not None:
        source_id, prefix = f.folder
        prefix = prefix.replace("\\", "/").strip("/")
        file_conds.append("c.source_id = ?")
        file_params.append(source_id)
        if prefix:
            file_conds.append("c.relative_path LIKE ? ESCAPE '\\'")
            file_params.append(_like_escape(prefix) + "/%")
    for word in f.text.split():
        file_conds.append("c.relative_path LIKE ? ESCAPE '\\'")
        file_params.append(f"%{_like_escape(word)}%")
    if file_conds:
        if not f.missing:
            file_conds.append("c.status = 'ok'")
        where.append(f"p.id IN (SELECT c.photo_id FROM files c WHERE {' AND '.join(file_conds)})")
        params.extend(file_params)

    if f.missing:
        where.append(
            "NOT EXISTS (SELECT 1 FROM files k WHERE k.photo_id = p.id AND k.status = 'ok')"
        )
    elif not file_conds:
        where.append("EXISTS (SELECT 1 FROM files k WHERE k.photo_id = p.id AND k.status = 'ok')")

    if f.date_from:
        where.append("p.taken_at_local >= ?")
        params.append(date.fromisoformat(f.date_from).isoformat())
    if f.date_to:
        where.append("p.taken_at_local < ?")
        params.append(_next_day(f.date_to))
    if f.album_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM album_photos ap WHERE ap.album_id = ? AND ap.photo_id = p.id)"
        )
        params.append(f.album_id)
    if f.has_duplicates:
        where.append("EXISTS (SELECT 1 FROM files d WHERE d.photo_id = p.id AND d.id <> f.id)")

    # With EXISTS in the WHERE clause the planner stops using the date index and sorts
    # everything; ``ordered`` pins it so a LIMIT stops early (counts do better without it).
    index = " INDEXED BY idx_photos_taken_at_local" if ordered else ""
    sql = f"FROM photos p{index} CROSS JOIN files f ON {_ROW_FILE}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params


def filtered_grid_photos(
    conn: sqlite3.Connection, f: PhotoFilter, limit: int | None = None, offset: int = 0
) -> list[GridPhoto]:
    """Like ``grid_photos`` (same columns and order), restricted by ``f``."""
    sql, params = _filter_sql(f, ordered=True)
    cur = conn.cursor()
    cur.row_factory = None
    cur.execute(
        "SELECT p.id, f.source_id, f.relative_path, p.width, p.height, p.format, "
        f"p.taken_at_local, p.date_source, p.content_hash {sql} "
        "ORDER BY p.taken_at_local DESC, f.relative_path, f.source_id LIMIT ? OFFSET ?",
        [*params, -1 if limit is None else limit, offset],
    )
    return list(map(GridPhoto._make, cur))


def filtered_month_counts(conn: sqlite3.Connection, f: PhotoFilter) -> list[tuple[str | None, int]]:
    """``("YYYY-MM", n)`` newest first, then ``(None, n)`` for unknown dates if any."""
    sql, params = _filter_sql(f)
    rows = conn.execute(
        f"SELECT substr(p.taken_at_local, 1, 7) AS m, COUNT(*) {sql} GROUP BY m", params
    ).fetchall()
    dated = sorted(((m, n) for m, n in rows if m is not None), reverse=True)
    unknown = sum(n for m, n in rows if m is None)
    return [*dated, (None, unknown)] if unknown else dated


def filtered_count(conn: sqlite3.Connection, f: PhotoFilter) -> int:
    sql, params = _filter_sql(f)
    return conn.execute(f"SELECT COUNT(*) {sql}", params).fetchone()[0]


def folder_paths(conn: sqlite3.Connection, source_id: int) -> list[str]:
    """Every folder of a source that holds files, plus their ancestors, without a trailing
    slash and sorted case-insensitively. The source root itself ("") is not listed."""
    # rtrim(path, path-without-slashes) strips the file name, leaving "a/b/".
    rows = conn.execute(
        "SELECT DISTINCT rtrim(relative_path, replace(relative_path, '/', '')) "
        "FROM files WHERE source_id = ?",
        (source_id,),
    )
    folders: set[str] = set()
    for (directory,) in rows:
        parts = directory.rstrip("/").split("/") if directory else []
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
    return sorted(folders, key=str.casefold)


# --- photo info -------------------------------------------------------------------------
# Read-only queries behind the viewer's info panel (PRD §8.6).


@dataclass(frozen=True)
class PhotoLocation:
    """One file of a photo: where it lives and whether it is still on disk."""

    source_id: int
    source_label: str
    source_root: str
    relative_path: str
    size: int
    status: str  # 'ok' | 'missing'


@dataclass(frozen=True)
class PhotoInfo:
    photo_id: int
    taken_at_local: str | None
    date_source: str | None
    latitude: float | None
    longitude: float | None
    width: int | None
    height: int | None
    format: str | None
    primary: PhotoLocation | None  # None when the photo has no file rows at all
    others: tuple[PhotoLocation, ...]  # the other copies ("Also found in...")


def photo_info(conn: sqlite3.Connection, photo_id: int) -> PhotoInfo | None:
    """Everything the info panel shows for one photo; ``None`` if it no longer exists.

    The primary copy comes first, then the other copies ordered by source label and path.
    """
    row = conn.execute(
        "SELECT taken_at_local, date_source, latitude, longitude, width, height, format, "
        "primary_file_id FROM photos WHERE id = ?",
        (photo_id,),
    ).fetchone()
    if row is None:
        return None
    locations = conn.execute(
        "SELECT f.id, f.source_id, s.label, s.root_path, f.relative_path, f.size, f.status "
        "FROM files f JOIN sources s ON s.id = f.source_id WHERE f.photo_id = ? "
        "ORDER BY s.label COLLATE NOCASE, f.relative_path, f.id",
        (photo_id,),
    ).fetchall()
    primary: PhotoLocation | None = None
    others: list[PhotoLocation] = []
    for loc in locations:
        place = PhotoLocation(loc[1], loc[2], loc[3], loc[4], loc[5], loc[6])
        if loc[0] == row["primary_file_id"] and primary is None:
            primary = place
        else:
            others.append(place)
    return PhotoInfo(
        photo_id,
        row["taken_at_local"],
        row["date_source"],
        row["latitude"],
        row["longitude"],
        row["width"],
        row["height"],
        row["format"],
        primary,
        tuple(others),
    )


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
