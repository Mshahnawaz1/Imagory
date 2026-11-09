"""Reading a cleanup output database for a cleaned source, and its metadata export."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from imagory.db.albums import AlbumNameError, _clean_album_name
from imagory.db.cleanup import _JSON_COPY_PREFIX
from imagory.db.connection import transaction, utc_now
from imagory.db.grid import _like_escape
from imagory.db.schema import schema_version
from imagory.db.settings import set_setting

log = logging.getLogger(__name__)


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
