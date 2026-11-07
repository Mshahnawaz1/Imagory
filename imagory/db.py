"""SQLite connection setup, schema migrations, and all SQL queries."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePath, PureWindowsPath
from typing import NamedTuple

from imagory.cleanup_types import ExportAlbum, ExportFile, ExportOrigin, ExportRun
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


def _migration_4(conn: sqlite3.Connection) -> None:
    """People and Takeout metadata (PRD §8.9). Photos already scanned keep empty values."""
    _execute_script(
        conn,
        """
        ALTER TABLE photos ADD COLUMN description TEXT;
        ALTER TABLE photos ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE photos ADD COLUMN uploaded_at_utc TEXT;

        CREATE TABLE people (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE photo_people (
            photo_id  INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            PRIMARY KEY (photo_id, person_id)
        );

        CREATE INDEX idx_photo_people_person_id ON photo_people(person_id);
        """,
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    """Takeout cleanup (PRD §8.10): the ``cleaned`` source type and the cleanup tables.

    SQLite can't change a CHECK constraint, so ``sources`` is rebuilt with the documented
    12-step procedure (https://sqlite.org/lang_altertable.html#otheralter). :func:`migrate`
    runs this step with foreign keys off and checks them before committing, because the
    old table is dropped while ``files`` and ``scan_runs`` still reference it. Indexes and
    triggers on ``sources`` (none in version 4) are recreated from their stored SQL.
    """
    extras = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = 'sources' "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
        )
    ]
    _execute_script(
        conn,
        """
        CREATE TABLE sources_new (
            id              INTEGER PRIMARY KEY,
            label           TEXT NOT NULL,
            root_path       TEXT NOT NULL,
            type            TEXT NOT NULL DEFAULT 'plain'
                            CHECK (type IN ('plain', 'takeout', 'cleaned')),
            type_overridden INTEGER NOT NULL DEFAULT 0,
            added_at        TEXT NOT NULL,
            last_scan_at    TEXT
        );

        INSERT INTO sources_new (id, label, root_path, type, type_overridden, added_at,
                                 last_scan_at)
            SELECT id, label, root_path, type, type_overridden, added_at, last_scan_at
            FROM sources;

        DROP TABLE sources;

        ALTER TABLE sources_new RENAME TO sources;
        """,
    )
    for sql in extras:
        conn.execute(sql)
    _execute_script(
        conn,
        """
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

        -- file_id / extra_file_id point at the kept row (the one that ends up in the
        -- cleaned source). No ON DELETE action: deleting a row that still has origins fails.
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

        CREATE INDEX idx_file_origins_file_id ON file_origins(file_id);
        CREATE INDEX idx_file_origins_extra_file_id ON file_origins(extra_file_id);
        CREATE INDEX idx_extra_files_content_hash ON extra_files(content_hash);
        CREATE INDEX idx_cleanup_copies_run_status ON cleanup_copies(run_id, status);
        """,
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
]
SCHEMA_VERSION = len(MIGRATIONS)
# V0 and development databases (versions 1 and 2) were never released and are replaced.
FIRST_SUPPORTED_VERSION = 3
# Migrations that rebuild a referenced table. ``PRAGMA foreign_keys`` can't change inside a
# transaction, so :func:`migrate` turns it off around these and runs ``foreign_key_check``
# before committing.
_REBUILD_MIGRATIONS = frozenset({5})


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
        if version in _REBUILD_MIGRATIONS:
            _migrate_without_foreign_keys(conn, version)
        else:
            with transaction(conn):
                MIGRATIONS[version - 1](conn)
                conn.execute(f"PRAGMA user_version = {version}")
    return SCHEMA_VERSION


def _migrate_without_foreign_keys(conn: sqlite3.Connection, version: int) -> None:
    """Run one table-rebuild migration with foreign keys off (SQLite's 12-step procedure).

    ``PRAGMA foreign_key_check`` runs before the commit: any violation rolls the step back
    and raises, so the database stays at the previous version (and the backup is kept).
    Foreign keys are turned back on whatever happens.
    """
    was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Could not turn foreign keys off (is a transaction open?)")
    try:
        with transaction(conn):
            MIGRATIONS[version - 1](conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                shown = ", ".join(f"{r[0]} rowid {r[1]} -> {r[2]}" for r in violations[:10])
                raise RuntimeError(
                    f"Migration {version} found {len(violations)} foreign key "
                    f"violation(s), rolled back: {shown}"
                )
            conn.execute(f"PRAGMA user_version = {version}")
    finally:
        if was_on:
            conn.execute("PRAGMA foreign_keys=ON")


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


# The types a user can choose for a folder (Add source, "Change type…").
SOURCE_TYPES = ("plain", "takeout")
# Every type the schema allows. A ``cleaned`` source is only ever created for a cleanup
# output (PRD §8.10) and can't be chosen or changed by the user.
ALL_SOURCE_TYPES = (*SOURCE_TYPES, "cleaned")
_SOURCE_COLUMNS = "id, label, root_path, type, type_overridden, added_at, last_scan_at"


@dataclass(frozen=True)
class Source:
    id: int
    label: str
    root_path: str
    type: str  # 'plain' | 'takeout' | 'cleaned'
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
    if type not in ALL_SOURCE_TYPES:
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
    content_hash: str | None = None  # confirms a cleaned source's output database match


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
            "sidecar_relative_path, content_hash FROM files WHERE source_id = ? "
            "ORDER BY relative_path, id",
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


# --- people (PRD §8.9) --------------------------------------------------------------------


def set_photo_takeout_metadata(
    conn: sqlite3.Connection,
    file_id: int,
    people: Iterable[str],
    description: str | None,
    favorite: bool,
    uploaded_at_utc: str | None,
) -> None:
    """Store Takeout people and metadata on the photo of a file, merging with what is there.

    People are only ever added (a photo found in several places has the union of the names).
    The first non-empty caption and upload time are kept; a photo is a favourite if any copy is.
    """
    row = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None or row[0] is None:
        return
    photo_id = row[0]
    for name in people:
        conn.execute("INSERT OR IGNORE INTO people (name) VALUES (?)", (name,))
        conn.execute(
            "INSERT OR IGNORE INTO photo_people (photo_id, person_id) "
            "SELECT ?, id FROM people WHERE name = ?",
            (photo_id, name),
        )
    conn.execute(
        "UPDATE photos SET description = COALESCE(NULLIF(description, ''), ?), "
        "favorite = MAX(favorite, ?), uploaded_at_utc = COALESCE(uploaded_at_utc, ?) "
        "WHERE id = ?",
        (description, 1 if favorite else 0, uploaded_at_utc, photo_id),
    )


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    photo_count: int  # photos with at least one copy that is ok


def list_people(conn: sqlite3.Connection) -> list[Person]:
    """People with at least one visible photo, most photos first, then by name."""
    rows = conn.execute(
        "SELECT pe.id, pe.name, COUNT(*) AS n FROM people pe "
        "JOIN photo_people pp ON pp.person_id = pe.id "
        "WHERE EXISTS (SELECT 1 FROM files k WHERE k.photo_id = pp.photo_id AND k.status = 'ok') "
        "GROUP BY pe.id ORDER BY n DESC, pe.name COLLATE NOCASE"
    )
    return [Person(r[0], r[1], r[2]) for r in rows]


def people_for_photo(conn: sqlite3.Connection, photo_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT pe.name FROM people pe JOIN photo_people pp ON pp.person_id = pe.id "
        "WHERE pp.photo_id = ? ORDER BY pe.name COLLATE NOCASE",
        (photo_id,),
    )
    return [r[0] for r in rows]


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
    person_id: int | None = None  # only photos tagged with this person (Takeout, PRD §8.9)

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
        # A word matches the path of this copy, or a person name or the caption of the photo.
        file_conds.append(
            "(c.relative_path LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM photo_people tp JOIN people tn ON tn.id = tp.person_id "
            "WHERE tp.photo_id = c.photo_id AND tn.name LIKE ? ESCAPE '\\') "
            "OR EXISTS (SELECT 1 FROM photos td WHERE td.id = c.photo_id "
            "AND td.description LIKE ? ESCAPE '\\'))"
        )
        like = f"%{_like_escape(word)}%"
        file_params.extend((like, like, like))
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
    if f.person_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM photo_people pq WHERE pq.person_id = ? AND pq.photo_id = p.id)"
        )
        params.append(f.person_id)
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
    people: tuple[str, ...] = ()  # Takeout people names (PRD §8.9)
    description: str | None = None
    favorite: bool = False
    uploaded_at_utc: str | None = None


def photo_info(conn: sqlite3.Connection, photo_id: int) -> PhotoInfo | None:
    """Everything the info panel shows for one photo; ``None`` if it no longer exists.

    The primary copy comes first, then the other copies ordered by source label and path.
    """
    row = conn.execute(
        "SELECT taken_at_local, date_source, latitude, longitude, width, height, format, "
        "primary_file_id, description, favorite, uploaded_at_utc FROM photos WHERE id = ?",
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
        tuple(people_for_photo(conn, photo_id)),
        row["description"],
        bool(row["favorite"]),
        row["uploaded_at_utc"],
    )


# --- cleanup ----------------------------------------------------------------------------
# Takeout cleanup (PRD §8.10, docs/takeout-cleanup-plan.md). Used only in a cleanup output
# database (``<output>/.imagory/imagory.db``), whose single writer is the cleanup thread. The
# input is scanned into a temporary ``takeout`` source; :func:`finalise_run` moves the kept
# rows to the output's ``cleaned`` source and deletes the rest. Origins and copies always
# reference the kept row, so they survive that.

RUN_STATUSES = ("analysing", "planned", "copying", "done", "cancelled")
RUN_COUNT_FIELDS = (
    "photos",
    "other_files",
    "duplicates",
    "trash_skipped",
    "errors",
    "bytes_copied",
)
_RUN_COLUMNS = "id, input_path, input_label, started_at, finished_at, status, " + ", ".join(
    RUN_COUNT_FIELDS
)
_JSON_COPY_PREFIX = ".imagory/"  # copied JSON files live here; not listed as output files


@dataclass(frozen=True)
class CleanupRun:
    id: int
    input_path: str
    input_label: str
    started_at: str
    finished_at: str | None
    status: str  # one of RUN_STATUSES
    counts: dict[str, int]  # RUN_COUNT_FIELDS -> value


def _cleanup_run(row: sqlite3.Row) -> CleanupRun:
    return CleanupRun(
        row["id"],
        row["input_path"],
        row["input_label"],
        row["started_at"],
        row["finished_at"],
        row["status"],
        {name: row[name] for name in RUN_COUNT_FIELDS},
    )


def create_run(
    conn: sqlite3.Connection, input_path: str, input_label: str, started_at: str | None = None
) -> int:
    """Start a cleanup run (status ``analysing``) and return its id."""
    return conn.execute(
        "INSERT INTO cleanup_runs (input_path, input_label, started_at) VALUES (?, ?, ?)",
        (input_path, input_label, started_at or utc_now()),
    ).lastrowid


def get_run(conn: sqlite3.Connection, run_id: int) -> CleanupRun | None:
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM cleanup_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return _cleanup_run(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[CleanupRun]:
    """Every run, oldest first."""
    rows = conn.execute(f"SELECT {_RUN_COLUMNS} FROM cleanup_runs ORDER BY id")
    return [_cleanup_run(r) for r in rows]


def resumable_run(conn: sqlite3.Connection, input_path: str) -> CleanupRun | None:
    """The latest unfinished run (any status but ``done``) for this input, if any.

    ``input_path`` is compared case-insensitively (ASCII only, like other paths here).
    """
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM cleanup_runs WHERE input_path = ? AND status <> 'done' "
        "ORDER BY id DESC LIMIT 1",
        (input_path,),
    ).fetchone()
    return _cleanup_run(row) if row else None


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    """Move a run to ``status``. Only :func:`finalise_run` sets ``done``."""
    if status not in RUN_STATUSES or status == "done":
        raise ValueError(f"Invalid run status: {status}")
    conn.execute("UPDATE cleanup_runs SET status = ? WHERE id = ?", (status, run_id))


def _check_counts(counts: Mapping[str, int]) -> None:
    unknown = set(counts) - set(RUN_COUNT_FIELDS)
    if unknown:
        raise ValueError(f"Unknown run counts: {sorted(unknown)}")


def set_run_counts(conn: sqlite3.Connection, run_id: int, counts: Mapping[str, int]) -> None:
    """Store some or all of a run's counts (keys from :data:`RUN_COUNT_FIELDS`)."""
    _check_counts(counts)
    if not counts:
        return
    names = list(counts)  # validated above, so safe to put in the SQL text
    assignments = ", ".join(f"{name} = ?" for name in names)
    conn.execute(
        f"UPDATE cleanup_runs SET {assignments} WHERE id = ?",
        [*(int(counts[n]) for n in names), run_id],
    )


@dataclass(frozen=True)
class ExtraFileRecord:
    """A non-image, an unreadable image, or a JSON file that isn't a used sidecar.

    ``extension`` defaults to the lower-cased suffix of ``relative_path`` (``""`` if none).
    """

    relative_path: str
    size: int
    modified_time: int
    content_hash: str | None
    extension: str | None = None
    taken_at_local: str | None = None
    taken_at_utc: str | None = None
    date_source: str | None = None  # never 'mtime' in a cleanup (PRD §8.4)
    latitude: float | None = None
    longitude: float | None = None
    description: str | None = None
    favorite: bool = False
    uploaded_at_utc: str | None = None
    people: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtraFile:
    id: int
    source_id: int
    relative_path: str
    size: int
    modified_time: int
    content_hash: str | None
    extension: str
    taken_at_local: str | None
    taken_at_utc: str | None
    date_source: str | None
    latitude: float | None
    longitude: float | None
    description: str | None
    favorite: bool
    uploaded_at_utc: str | None
    people: tuple[str, ...]


_EXTRA_COLUMNS = (
    "id, source_id, relative_path, size, modified_time, content_hash, extension, "
    "taken_at_local, taken_at_utc, date_source, latitude, longitude, description, favorite, "
    "uploaded_at_utc, people"
)


def _extension(relative_path: str) -> str:
    stem, dot, ext = relative_path.rsplit("/", 1)[-1].rpartition(".")
    return f".{ext.lower()}" if dot and stem else ""


def _extra_file(row: sqlite3.Row) -> ExtraFile:
    return ExtraFile(
        *tuple(row)[:13],
        bool(row["favorite"]),
        row["uploaded_at_utc"],
        tuple(json.loads(row["people"])),
    )


def upsert_extra_files(
    conn: sqlite3.Connection, source_id: int, records: Iterable[ExtraFileRecord]
) -> list[int]:
    """Insert extra files, or update the row with the same path (case-insensitive), so a
    resumed stage 3 doesn't duplicate rows and ids are kept. Returns the ids in input order."""
    ids: list[int] = []
    for rec in records:
        ext = rec.extension if rec.extension is not None else _extension(rec.relative_path)
        row = conn.execute(
            "INSERT INTO extra_files (source_id, relative_path, size, modified_time, "
            "content_hash, extension, taken_at_local, taken_at_utc, date_source, latitude, "
            "longitude, description, favorite, uploaded_at_utc, people) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_id, relative_path) DO UPDATE SET "
            "relative_path = excluded.relative_path, size = excluded.size, "
            "modified_time = excluded.modified_time, content_hash = excluded.content_hash, "
            "extension = excluded.extension, taken_at_local = excluded.taken_at_local, "
            "taken_at_utc = excluded.taken_at_utc, date_source = excluded.date_source, "
            "latitude = excluded.latitude, longitude = excluded.longitude, "
            "description = excluded.description, favorite = excluded.favorite, "
            "uploaded_at_utc = excluded.uploaded_at_utc, people = excluded.people "
            "RETURNING id",
            (
                source_id,
                rec.relative_path,
                rec.size,
                rec.modified_time,
                rec.content_hash,
                ext,
                rec.taken_at_local,
                rec.taken_at_utc,
                rec.date_source,
                rec.latitude,
                rec.longitude,
                rec.description,
                1 if rec.favorite else 0,
                rec.uploaded_at_utc,
                json.dumps(list(rec.people), ensure_ascii=False),
            ),
        ).fetchone()
        ids.append(row[0])
    return ids


def list_extra_files(conn: sqlite3.Connection, source_id: int) -> list[ExtraFile]:
    """A source's extra files, by path."""
    rows = conn.execute(
        f"SELECT {_EXTRA_COLUMNS} FROM extra_files WHERE source_id = ? ORDER BY relative_path, id",
        (source_id,),
    )
    return [_extra_file(r) for r in rows]


@dataclass(frozen=True)
class OriginRecord:
    """One input copy of a kept file. Exactly one of ``file_id`` / ``extra_file_id`` is set,
    and it is the **kept** row, also for the copies that are not copied."""

    original_relative_path: str  # this copy's own input path
    file_id: int | None = None
    extra_file_id: int | None = None
    sidecar_relative_path: str | None = None  # this copy's own sidecar, input-relative
    album_folder: str | None = None  # the album name this copy belongs to, if any


@dataclass(frozen=True)
class FileOrigin:
    id: int
    run_id: int
    file_id: int | None
    extra_file_id: int | None
    original_relative_path: str
    sidecar_relative_path: str | None
    album_folder: str | None


def _check_one_target(file_id: int | None, extra_file_id: int | None) -> None:
    if (file_id is None) == (extra_file_id is None):
        raise ValueError("Exactly one of file_id and extra_file_id must be set")


def insert_origins(conn: sqlite3.Connection, run_id: int, origins: Iterable[OriginRecord]) -> int:
    """Record input copies for a run. Returns how many were inserted.

    Not idempotent: group it with :func:`insert_copies` and ``set_run_status(conn, run_id,
    'planned')`` in one :func:`transaction`, so a cancelled plan leaves nothing behind.
    """
    rows = []
    for o in origins:
        _check_one_target(o.file_id, o.extra_file_id)
        rows.append(
            (
                run_id,
                o.file_id,
                o.extra_file_id,
                o.original_relative_path,
                o.sidecar_relative_path,
                o.album_folder,
            )
        )
    conn.executemany(
        "INSERT INTO file_origins (run_id, file_id, extra_file_id, original_relative_path, "
        "sidecar_relative_path, album_folder) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def list_origins(conn: sqlite3.Connection, run_id: int | None = None) -> list[FileOrigin]:
    """Origins of one run (or of every run), by run, then input path."""
    where, params = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
    rows = conn.execute(
        "SELECT id, run_id, file_id, extra_file_id, original_relative_path, "
        f"sidecar_relative_path, album_folder FROM file_origins {where} "
        "ORDER BY run_id, original_relative_path, id",
        params,
    )
    return [FileOrigin(*r) for r in rows]


@dataclass(frozen=True)
class CopyRecord:
    """One planned copy of a kept row. Exactly one of ``file_id`` / ``extra_file_id`` is set."""

    source_relative_path: str  # input-relative
    target_relative_path: str  # output-relative
    file_id: int | None = None
    extra_file_id: int | None = None


@dataclass(frozen=True)
class CleanupCopy:
    id: int
    run_id: int
    file_id: int | None
    extra_file_id: int | None
    source_relative_path: str
    target_relative_path: str
    status: str  # 'pending' | 'done' | 'error'
    error: str | None


_COPY_COLUMNS = (
    "id, run_id, file_id, extra_file_id, source_relative_path, target_relative_path, status, error"
)


def insert_copies(conn: sqlite3.Connection, run_id: int, copies: Iterable[CopyRecord]) -> int:
    """Record planned copies (status ``pending``). Returns how many were inserted. A target
    already used by any run (compared case-insensitively) raises ``sqlite3.IntegrityError``."""
    rows = []
    for c in copies:
        _check_one_target(c.file_id, c.extra_file_id)
        rows.append(
            (run_id, c.file_id, c.extra_file_id, c.source_relative_path, c.target_relative_path)
        )
    conn.executemany(
        "INSERT INTO cleanup_copies (run_id, file_id, extra_file_id, source_relative_path, "
        "target_relative_path) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def list_copies(
    conn: sqlite3.Connection, run_id: int, status: str | None = None
) -> list[CleanupCopy]:
    """A run's copies, optionally only those with ``status``, in plan order (by id)."""
    sql = f"SELECT {_COPY_COLUMNS} FROM cleanup_copies WHERE run_id = ?"
    params: tuple = (run_id,)
    if status is not None:
        sql += " AND status = ?"
        params = (run_id, status)
    return [CleanupCopy(*r) for r in conn.execute(sql + " ORDER BY id", params)]


def pending_copies(conn: sqlite3.Connection, run_id: int) -> list[CleanupCopy]:
    return list_copies(conn, run_id, "pending")


def copy_targets(conn: sqlite3.Connection) -> list[str]:
    """Every target path planned by any run, e.g. to check new names against the output."""
    return [r[0] for r in conn.execute("SELECT target_relative_path FROM cleanup_copies")]


def mark_copy_done(conn: sqlite3.Connection, copy_id: int) -> None:
    conn.execute("UPDATE cleanup_copies SET status = 'done', error = NULL WHERE id = ?", (copy_id,))


def mark_copy_error(conn: sqlite3.Connection, copy_id: int, error: str) -> None:
    conn.execute(
        "UPDATE cleanup_copies SET status = 'error', error = ? WHERE id = ?", (error, copy_id)
    )


def reset_copy(conn: sqlite3.Connection, copy_id: int) -> None:
    """Put a copy back to ``pending``: a ``done`` file that no longer checks out on resume,
    or an ``error`` to retry."""
    conn.execute(
        "UPDATE cleanup_copies SET status = 'pending', error = NULL WHERE id = ?", (copy_id,)
    )


@dataclass(frozen=True)
class FinaliseResult:
    files_moved: int
    extra_files_moved: int
    albums_created: int  # new album rows (an existing album with the same name is reused)
    failed_copies_dropped: int  # copies in 'error', removed with their kept row and origins
    leftover_rows_deleted: int  # file and extra-file rows deleted with the temporary source


def _best_real_date(rows: Iterable[tuple]) -> DateResult | None:
    """The best of ``(taken_at_local, taken_at_utc, date_source)`` rows, ignoring mtime."""
    return best_photo_date(
        DateResult(r[0], r[1], r[2])
        for r in rows
        if r[0] and r[2] in SOURCE_RANK and r[2] != "mtime"
    )


def finalise_run(
    conn: sqlite3.Connection,
    run_id: int,
    temp_source_id: int,
    cleaned_source_id: int,
    copied: Mapping[int, tuple[int, int]],
    counts: Mapping[str, int] | None = None,
) -> FinaliseResult:
    """Finish a run in one transaction (pipeline stage 7).

    ``copied`` maps every ``done`` copy id to that copy's own ``(size, mtime_ns)`` on disk.
    Raises ``ValueError`` and changes nothing if the run is missing or already done, a copy
    is still ``pending``, or a ``done`` copy is missing from ``copied``.

    1. Each kept row with a ``done`` copy first takes the best non-mtime date of all rows
       with its hash (a file: every file of its photo; an extra file: the same-hash rows of
       the temporary source), so nothing a duplicate knew is lost. A kept extra file also
       takes the union of their people, the first caption, upload time and location, and
       any favourite (photo-level metadata already lives on ``photos``).
    2. The kept rows move to ``cleaned_source_id`` with their target path and the copy's
       size and mtime. Ids are kept, so origins and copies still point at them. Files drop
       their input ``sidecar_relative_path`` (the origins keep each copy's sidecar).
    3. ``taken_at_local``, ``taken_at_utc`` and ``date_source`` are cleared wherever
       ``date_source = 'mtime'`` in the cleaned source (PRD §8.4).
    4. Each distinct ``album_folder`` of the run's origins becomes an album (an existing
       album with that name, ignoring case, is reused) holding the photos of its origins.
       Extra files can't be album members; :func:`export_albums` lists them from origins.
    5. Copies in ``error`` are deleted with the origins of their kept row; then every row of
       the temporary source goes: files, extra files, scan runs and the source itself. An
       origin still pointing at one of those rows makes the transaction fail.
    6. Affected photos get their primary copy and date recomputed (a photo left with no
       file is deleted), and the run is marked ``done`` with ``finished_at`` and ``counts``.
    """
    if temp_source_id == cleaned_source_id:
        raise ValueError("The temporary source and the cleaned source must differ")
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"No cleanup run {run_id}")
    if run.status == "done":
        raise ValueError(f"Cleanup run {run_id} is already finalised")
    if counts:
        _check_counts(counts)
    copies = list_copies(conn, run_id)
    if any(c.status == "pending" for c in copies):
        raise ValueError(f"Cleanup run {run_id} still has pending copies")
    done = [c for c in copies if c.status == "done"]
    unknown = [c.id for c in done if c.id not in copied]
    if unknown:
        raise ValueError(f"No size and mtime given for done copies {unknown[:10]}")

    with transaction(conn):
        photo_ids = set(_photo_ids_of(conn, "source_id = ?", (temp_source_id,)))
        files_moved = extras_moved = 0
        for c in done:
            size, mtime_ns = copied[c.id]
            if c.file_id is not None:
                photo_id = _keep_best_file_date(conn, c.file_id)
                if photo_id is not None:
                    photo_ids.add(photo_id)
                files_moved += conn.execute(
                    "UPDATE files SET source_id = ?, relative_path = ?, size = ?, "
                    "modified_time = ?, status = 'ok', sidecar_relative_path = NULL "
                    "WHERE id = ?",
                    (cleaned_source_id, c.target_relative_path, size, mtime_ns, c.file_id),
                ).rowcount
            else:
                _merge_extra_duplicates(conn, c.extra_file_id, temp_source_id)
                extras_moved += conn.execute(
                    "UPDATE extra_files SET source_id = ?, relative_path = ?, size = ?, "
                    "modified_time = ? WHERE id = ?",
                    (cleaned_source_id, c.target_relative_path, size, mtime_ns, c.extra_file_id),
                ).rowcount

        # PRD §8.4: a cleaned source never dates a file by its modified time.
        photo_ids.update(
            _photo_ids_of(conn, "source_id = ? AND date_source = 'mtime'", (cleaned_source_id,))
        )
        for table in ("files", "extra_files"):
            conn.execute(
                f"UPDATE {table} SET taken_at_local = NULL, taken_at_utc = NULL, "
                "date_source = NULL WHERE source_id = ? AND date_source = 'mtime'",
                (cleaned_source_id,),
            )

        albums_created = _create_run_albums(conn, run_id, cleaned_source_id)

        # A kept row whose copy failed is not in the output: it leaves with the temporary
        # source, together with its origins and its copy row.
        failed = "SELECT {} FROM cleanup_copies WHERE run_id = ? AND status = 'error'"
        conn.execute(
            "DELETE FROM file_origins WHERE run_id = ? AND ("
            f"file_id IN ({failed.format('file_id')}) OR "
            f"extra_file_id IN ({failed.format('extra_file_id')}))",
            (run_id, run_id, run_id),
        )
        dropped = conn.execute(
            "DELETE FROM cleanup_copies WHERE run_id = ? AND status = 'error'", (run_id,)
        ).rowcount
        leftovers = conn.execute(
            "DELETE FROM files WHERE source_id = ?", (temp_source_id,)
        ).rowcount
        leftovers += conn.execute(
            "DELETE FROM extra_files WHERE source_id = ?", (temp_source_id,)
        ).rowcount
        conn.execute("DELETE FROM scan_runs WHERE source_id = ?", (temp_source_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (temp_source_id,))
        refresh_photos(conn, photo_ids)

        conn.execute(
            "UPDATE cleanup_runs SET status = 'done', finished_at = ? WHERE id = ?",
            (utc_now(), run_id),
        )
        if counts:
            set_run_counts(conn, run_id, counts)
    return FinaliseResult(files_moved, extras_moved, albums_created, dropped, leftovers)


def _keep_best_file_date(conn: sqlite3.Connection, file_id: int) -> int | None:
    """Give a kept file the best non-mtime date of its photo's files. Returns the photo id."""
    row = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    best = _best_real_date(
        conn.execute(
            "SELECT taken_at_local, taken_at_utc, date_source FROM files WHERE photo_id = ?",
            (row[0],),
        )
    )
    if best is not None:
        conn.execute(
            "UPDATE files SET taken_at_local = ?, taken_at_utc = ?, date_source = ? WHERE id = ?",
            (best.taken_at_local, best.taken_at_utc, best.date_source, file_id),
        )
    return row[0]


def _merge_extra_duplicates(
    conn: sqlite3.Connection, extra_file_id: int, temp_source_id: int
) -> None:
    """Give a kept extra file what its same-hash rows in the temporary source knew.

    JSON files are not deduplicated (each is copied), so they are left alone.
    """
    kept = conn.execute(
        "SELECT content_hash, extension FROM extra_files WHERE id = ?", (extra_file_id,)
    ).fetchone()
    if kept is None or kept[0] is None or kept[1] == ".json":
        return
    rows = [
        _extra_file(r)
        for r in conn.execute(
            f"SELECT {_EXTRA_COLUMNS} FROM extra_files WHERE content_hash = ? "
            "AND (id = ? OR (source_id = ? AND extension <> '.json')) "
            "ORDER BY id <> ?, relative_path, id",  # the kept row first
            (kept[0], extra_file_id, temp_source_id, extra_file_id),
        )
    ]
    if len(rows) < 2:
        return
    best = _best_real_date((r.taken_at_local, r.taken_at_utc, r.date_source) for r in rows)
    first = rows[0]
    people: dict[str, str] = {}
    for r in rows:
        for name in r.people:
            people.setdefault(name.casefold(), name)
    located = next((r for r in rows if r.latitude is not None and r.longitude is not None), None)
    conn.execute(
        "UPDATE extra_files SET taken_at_local = ?, taken_at_utc = ?, date_source = ?, "
        "latitude = ?, longitude = ?, description = ?, favorite = ?, uploaded_at_utc = ?, "
        "people = ? WHERE id = ?",
        (
            best.taken_at_local if best else first.taken_at_local,
            best.taken_at_utc if best else first.taken_at_utc,
            best.date_source if best else first.date_source,
            located.latitude if located else None,
            located.longitude if located else None,
            next((r.description for r in rows if r.description), first.description),
            1 if any(r.favorite for r in rows) else 0,
            next((r.uploaded_at_utc for r in rows if r.uploaded_at_utc), None),
            json.dumps(list(people.values()), ensure_ascii=False),
            extra_file_id,
        ),
    )


def _create_run_albums(conn: sqlite3.Connection, run_id: int, cleaned_source_id: int) -> int:
    """Albums for a run's origins, holding the photos whose kept file is in the cleaned
    source. Returns how many album rows are new."""
    now = utc_now()
    created = 0
    names = conn.execute(
        "SELECT DISTINCT trim(album_folder) FROM file_origins WHERE run_id = ? "
        "AND trim(album_folder) <> '' ORDER BY 1",
        (run_id,),
    ).fetchall()
    for (name,) in names:
        created += conn.execute(
            "INSERT OR IGNORE INTO albums (name, created_at) VALUES (?, ?)", (name, now)
        ).rowcount
        conn.execute(
            "INSERT OR IGNORE INTO album_photos (album_id, photo_id, added_at) "
            "SELECT DISTINCT a.id, f.photo_id, ? FROM file_origins o "
            "JOIN files f ON f.id = o.file_id JOIN albums a ON a.name = ? "
            "WHERE o.run_id = ? AND trim(o.album_folder) = ? AND f.source_id = ? "
            "AND f.photo_id IS NOT NULL",
            (now, name, run_id, name, cleaned_source_id),
        )
    return created


def export_runs(conn: sqlite3.Connection) -> list[ExportRun]:
    """The finished (``done``) runs, oldest first, for ``metadata.json``."""
    return [
        ExportRun(r.id, r.input_path, r.started_at, r.finished_at, dict(r.counts))
        for r in list_runs(conn)
        if r.status == "done"
    ]


def export_albums(
    conn: sqlite3.Connection, cleaned_source_id: int, run_id: int | None = None
) -> list[ExportAlbum]:
    """Albums from the origins (of one run, or of all), each with the output paths of its
    kept files and extra files in the cleaned source, so videos and other files are members
    too. Albums by name ignoring case (names equal ignoring case are one album), paths sorted.
    """
    sql = (
        "SELECT trim(o.album_folder), COALESCE(f.relative_path, e.relative_path) "
        "FROM file_origins o "
        "LEFT JOIN files f ON f.id = o.file_id AND f.source_id = ? "
        "LEFT JOIN extra_files e ON e.id = o.extra_file_id AND e.source_id = ? "
        "WHERE trim(o.album_folder) <> '' "
        "AND COALESCE(f.relative_path, e.relative_path) IS NOT NULL"
    )
    params: list = [cleaned_source_id, cleaned_source_id]
    if run_id is not None:
        sql += " AND o.run_id = ?"
        params.append(run_id)
    albums: dict[str, tuple[str, set[str]]] = {}
    for name, path in conn.execute(sql + " ORDER BY o.run_id, o.id", params):
        albums.setdefault(name.casefold(), (name, set()))[1].add(path)
    return [
        ExportAlbum(name, tuple(sorted(paths, key=lambda p: (p.casefold(), p))))
        for name, paths in sorted(albums.values(), key=lambda a: (a[0].casefold(), a[0]))
    ]


def export_files(conn: sqlite3.Connection, cleaned_source_id: int) -> list[ExportFile]:
    """Every output file of the cleaned source for ``metadata.json``, by path: photos (a
    ``files`` row with its photo's metadata) and other files (``extra_files``, except the
    JSON copies under ``.imagory/``), each with its origins from every run."""
    origins_by_file: dict[int, list[ExportOrigin]] = {}
    origins_by_extra: dict[int, list[ExportOrigin]] = {}
    for run, file_id, extra_id, path, sidecar, album in conn.execute(
        "SELECT run_id, file_id, extra_file_id, original_relative_path, sidecar_relative_path, "
        "album_folder FROM file_origins ORDER BY run_id, original_relative_path, id"
    ):
        target = origins_by_file if file_id is not None else origins_by_extra
        target.setdefault(file_id if file_id is not None else extra_id, []).append(
            ExportOrigin(run, path, sidecar, album.strip() if album else None)
        )
    people: dict[int, list[str]] = {}
    for photo_id, name in conn.execute(
        "SELECT pp.photo_id, pe.name FROM photo_people pp JOIN people pe ON pe.id = pp.person_id "
        "ORDER BY pe.name COLLATE NOCASE, pe.name"
    ):
        people.setdefault(photo_id, []).append(name)

    def real(date_source: str | None) -> bool:
        return date_source is not None and date_source != "mtime"

    result: list[ExportFile] = []
    for r in conn.execute(
        "SELECT f.id, f.relative_path, f.size, COALESCE(f.content_hash, p.content_hash), "
        "p.id, p.width, p.height, p.format, p.taken_at_local, p.taken_at_utc, p.date_source, "
        "p.latitude, p.longitude, p.description, p.favorite, p.uploaded_at_utc "
        "FROM files f LEFT JOIN photos p ON p.id = f.photo_id WHERE f.source_id = ?",
        (cleaned_source_id,),
    ):
        dated = real(r[10])
        result.append(
            ExportFile(
                path=r[1],
                kind="photo",
                size=r[2],
                content_hash=r[3] or "",
                width=r[5],
                height=r[6],
                format=r[7],
                taken_at_local=r[8] if dated else None,
                taken_at_utc=r[9] if dated else None,
                date_source=r[10] if dated else None,
                latitude=r[11],
                longitude=r[12],
                people=tuple(people.get(r[4], ())),
                description=r[13],
                favorite=bool(r[14]),
                uploaded_at_utc=r[15],
                origins=tuple(origins_by_file.get(r[0], ())),
            )
        )
    for e in list_extra_files(conn, cleaned_source_id):
        if e.relative_path.startswith(_JSON_COPY_PREFIX):
            continue
        dated = real(e.date_source)
        result.append(
            ExportFile(
                path=e.relative_path,
                kind="other",
                size=e.size,
                content_hash=e.content_hash or "",
                taken_at_local=e.taken_at_local if dated else None,
                taken_at_utc=e.taken_at_utc if dated else None,
                date_source=e.date_source if dated else None,
                latitude=e.latitude,
                longitude=e.longitude,
                people=e.people,
                description=e.description,
                favorite=e.favorite,
                uploaded_at_utc=e.uploaded_at_utc,
                origins=tuple(origins_by_extra.get(e.id, ())),
            )
        )
    result.sort(key=lambda f: (f.path.casefold(), f.path))
    return result


# --- cleaned sources --------------------------------------------------------------------
# A ``cleaned`` source in the main library is a cleanup output folder (PRD §8.10). Its own
# database, ``<root>/.imagory/imagory.db``, lies inside a user folder, so it is only ever
# read: never written, never migrated, and opened so that no ``-wal`` or ``-shm`` file
# appears next to it. Everything is read in one go and the connection closed again.

OUTPUT_MIN_VERSION = 5  # the first schema with the cleanup tables
UNTITLED_ALBUM = "Untitled"


class OutputDatabaseError(RuntimeError):
    """A cleanup output database exists but cannot be read."""


@dataclass(frozen=True)
class OutputFileMeta:
    """What a cleanup output database knows about one of its files (photo or other file)."""

    relative_path: str  # output-relative, as stored
    content_hash: str | None
    taken_at_local: str | None  # None: undated (never an mtime date, PRD §8.4)
    taken_at_utc: str | None
    date_source: str | None
    latitude: float | None
    longitude: float | None
    description: str | None
    favorite: bool
    uploaded_at_utc: str | None
    people: tuple[str, ...]


@dataclass(frozen=True)
class OutputRun:
    id: int
    status: str  # one of RUN_STATUSES
    finished_at: str | None  # UTC ISO 8601 ending in ``Z``, set when ``done``


@dataclass(frozen=True)
class OutputSnapshot:
    """What is read from a cleanup output database in one go."""

    files: tuple[OutputFileMeta, ...]
    runs: tuple[OutputRun, ...]

    @property
    def done_runs(self) -> frozenset[int]:
        return frozenset(r.id for r in self.runs if r.status == "done")


def sqlite_file_uri(path: PurePath, query: str = "") -> str:
    """A SQLite ``file:`` URI for an absolute path, with an empty authority.

    The path is percent-encoded (``%``, ``?``, ``#``, spaces and non-ASCII as UTF-8), so it
    can't be read as part of the query. Windows paths use ``/``: ``C:\\x`` becomes
    ``file:///C:/x`` and the UNC path ``\\\\server\\share\\x`` becomes
    ``file:////server/share/x``. ``query`` is appended after ``?`` as given.
    """
    from urllib.parse import quote

    text = str(path)
    if isinstance(path, PureWindowsPath):
        text = text.replace("\\", "/")
        if not text.startswith("/"):
            text = "/" + text  # drive path: /C:/x
    uri = "file://" + quote(text, safe="/:")
    return f"{uri}?{query}" if query else uri


@contextmanager
def open_output_database(db_file: Path) -> Iterator[sqlite3.Connection]:
    """Open a cleanup output database for reading only, without writing anything beside it.

    With no ``-wal`` file it is opened ``immutable`` (read only, no locking, so SQLite
    creates no ``-wal``/``-shm``). A non-empty ``-wal`` (an earlier writer stopped before
    checkpointing) may hold committed rows that ``immutable`` would not see, so the database
    and its WAL are copied to a temporary folder outside the source and the copy is read.
    Raises :class:`OutputDatabaseError` when it can't be opened.
    """
    import shutil
    import tempfile

    db_file = Path(db_file).absolute()
    wal = Path(f"{db_file}-wal")
    try:
        has_wal = wal.is_file() and wal.stat().st_size > 0
    except OSError:
        has_wal = False
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if has_wal:
            tmp = tempfile.TemporaryDirectory(prefix="imagory-output-")
            copy = Path(tmp.name) / db_file.name
            shutil.copyfile(db_file, copy)  # reads the originals only
            shutil.copyfile(wal, Path(f"{copy}-wal"))
            conn = sqlite3.connect(str(copy), isolation_level=None)
        else:
            uri = sqlite_file_uri(db_file, "mode=ro&immutable=1")
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # fails if not a db
    except (OSError, sqlite3.Error) as exc:
        if tmp is not None:
            tmp.cleanup()
        raise OutputDatabaseError(f"Cannot open {db_file}: {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()
        if tmp is not None:
            tmp.cleanup()


def read_output_database(db_file: Path, *, files: bool = True) -> OutputSnapshot:
    """Read a cleanup output database: its runs, and (unless ``files`` is False) every file
    of its ``cleaned`` source(s) with date, location, people, caption, favourite and upload
    time. Photos take their photo's values (as ``metadata.json`` does); other files their
    ``extra_files`` row. JSON copies under ``.imagory/`` are left out. An ``mtime`` date is
    returned as undated.

    Raises :class:`OutputDatabaseError` if the database can't be opened or read.
    """
    with open_output_database(db_file) as out:
        try:
            version = schema_version(out)
            if version < OUTPUT_MIN_VERSION:
                raise OutputDatabaseError(
                    f"{db_file} is not a cleanup output (schema version {version})"
                )
            runs = tuple(
                OutputRun(r[0], r[1], r[2])
                for r in out.execute("SELECT id, status, finished_at FROM cleanup_runs ORDER BY id")
            )
            return OutputSnapshot(tuple(_read_output_rows(out)) if files else (), runs)
        except sqlite3.Error as exc:
            raise OutputDatabaseError(f"Cannot read {db_file}: {exc}") from exc


def _read_output_rows(out: sqlite3.Connection) -> list[OutputFileMeta]:
    cleaned = [r[0] for r in out.execute("SELECT id FROM sources WHERE type = 'cleaned'")]
    if not cleaned:
        return []
    marks = ",".join("?" * len(cleaned))
    people: dict[int, list[str]] = {}
    for photo_id, name in out.execute(
        "SELECT pp.photo_id, pe.name FROM photo_people pp JOIN people pe ON pe.id = pp.person_id "
        "ORDER BY pe.name COLLATE NOCASE, pe.name"
    ):
        people.setdefault(photo_id, []).append(name)

    def dated(local: str | None, utc: str | None, source: str | None) -> tuple:
        if not local or source is None or source == "mtime":
            return None, None, None
        return local, utc, source

    result: list[OutputFileMeta] = []
    for r in out.execute(
        "SELECT f.relative_path, COALESCE(f.content_hash, p.content_hash), p.id, "
        "COALESCE(p.taken_at_local, f.taken_at_local), "
        "CASE WHEN p.id IS NULL THEN f.taken_at_utc ELSE p.taken_at_utc END, "
        "COALESCE(p.date_source, f.date_source), "
        "p.latitude, p.longitude, p.description, p.favorite, p.uploaded_at_utc "
        f"FROM files f LEFT JOIN photos p ON p.id = f.photo_id WHERE f.source_id IN ({marks}) "
        "ORDER BY f.id",
        cleaned,
    ):
        result.append(
            OutputFileMeta(
                r[0],
                r[1],
                *dated(r[3], r[4], r[5]),
                r[6],
                r[7],
                r[8],
                bool(r[9]),
                r[10],
                tuple(people.get(r[2], ())),
            )
        )
    for r in out.execute(
        "SELECT relative_path, content_hash, taken_at_local, taken_at_utc, date_source, "
        "latitude, longitude, description, favorite, uploaded_at_utc, people FROM extra_files "
        f"WHERE source_id IN ({marks}) AND relative_path NOT LIKE ? ESCAPE '\\' ORDER BY id",
        [*cleaned, _like_escape(_JSON_COPY_PREFIX) + "%"],
    ):
        try:
            names = tuple(n for n in json.loads(r[10]) if isinstance(n, str))
        except (TypeError, ValueError):
            names = ()
        result.append(
            OutputFileMeta(
                r[0], r[1], *dated(r[2], r[3], r[4]), r[5], r[6], r[7], bool(r[8]), r[9], names
            )
        )
    return result


def photo_ids_at_paths(conn: sqlite3.Connection, source_id: int, paths: Iterable[str]) -> list[int]:
    """Distinct photo ids of a source's files at these relative paths (case-insensitive,
    like ``COLLATE NOCASE``), in the order of ``paths``. Paths with no file row are skipped."""
    from imagory.thumbs import fold_path

    paths = list(dict.fromkeys(paths))
    by_path: dict[str, int] = {}
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        for rel, photo_id in conn.execute(
            "SELECT relative_path, photo_id FROM files WHERE source_id = ? "
            f"AND relative_path IN ({placeholders}) AND photo_id IS NOT NULL",
            [source_id, *chunk],
        ):
            by_path[fold_path(rel)] = photo_id
    found: dict[int, None] = {}
    for path in paths:
        photo_id = by_path.get(fold_path(path))
        if photo_id is not None:
            found.setdefault(photo_id)
    return list(found)


def imported_album_name(name: str) -> str:
    """An imported album's name as the album INSERT stores it (:func:`create_album`'s
    normalisation), or :data:`UNTITLED_ALBUM` when nothing is left."""
    try:
        return _clean_album_name(name)
    except AlbumNameError:
        return UNTITLED_ALBUM


def create_albums_with_photos(
    conn: sqlite3.Connection,
    albums: Iterable[tuple[str, Iterable[int]]],
    setting: tuple[str, str] | None = None,
) -> list[int | None]:
    """Create new albums with their photos, and optionally store one setting, in one
    transaction. Existing albums are never touched. Each album has its own savepoint: one
    that fails (a taken name, for example) is logged and skipped, and the rest are still
    created and the setting stored. Returns the new album ids, None for a skipped album."""
    now = utc_now()
    ids: list[int | None] = []
    with transaction(conn):
        for name, photo_ids in albums:
            conn.execute("SAVEPOINT album")
            try:
                album_id = conn.execute(
                    "INSERT INTO albums (name, created_at) VALUES (?, ?)",
                    (_clean_album_name(name), now),
                ).lastrowid
                conn.executemany(
                    "INSERT OR IGNORE INTO album_photos (album_id, photo_id, added_at) "
                    "VALUES (?, ?, ?)",
                    ((album_id, pid, now) for pid in dict.fromkeys(photo_ids)),
                )
            except (sqlite3.Error, AlbumNameError) as exc:
                conn.execute("ROLLBACK TO album")
                conn.execute("RELEASE album")
                log.warning("Album %r not imported: %s", name, exc)
                ids.append(None)
                continue
            conn.execute("RELEASE album")
            ids.append(album_id)
        if setting is not None:
            set_setting(conn, *setting)
    return ids


@dataclass(frozen=True)
class LastScan:
    status: str  # 'running' | 'completed' | 'cancelled' | 'failed'
    started_at: str  # UTC ISO 8601 ending in ``Z``
    finished_at: str | None


def last_scan(conn: sqlite3.Connection, source_id: int) -> LastScan | None:
    """A source's latest scan run, or None if it was never scanned."""
    row = conn.execute(
        "SELECT status, started_at, finished_at FROM scan_runs WHERE source_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return LastScan(row[0], row[1], row[2]) if row else None


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
