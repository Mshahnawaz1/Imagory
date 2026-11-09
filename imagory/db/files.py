"""File rows written by a scan (insert, update, move, missing) and the scan run records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from imagory.db.connection import transaction, utc_now
from imagory.db.photos import (
    _create_photo,
    _photo_for_hash,
    _photo_ids_of_files,
    _refresh_primaries,
    refresh_photos,
)


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
    """Mark files missing; a missing primary file hands over to the next file."""
    file_ids = list(file_ids)
    conn.executemany(
        "UPDATE files SET status = 'missing' WHERE id = ?", ((fid,) for fid in file_ids)
    )
    _refresh_primaries(conn, _photo_ids_of_files(conn, file_ids))


def mark_ok(conn: sqlite3.Connection, file_ids: Iterable[int]) -> None:
    """Mark files present again; the primary file is recomputed."""
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

    Returns the file id. The photo's primary file and date are recomputed.
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
    """Photos with these hashes that the grid shows now (their primary file is ok)."""
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
