"""Photo identity by content hash, and recomputing each photo's primary file and date."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING

from imagory.dates import SOURCE_RANK, DateResult, best_photo_date

if TYPE_CHECKING:
    from imagory.db.files import FileRecord


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
