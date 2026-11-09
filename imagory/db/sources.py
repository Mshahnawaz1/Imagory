"""Sources: adding, renaming, typing, relocating and removing them, and their statistics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from imagory.db.connection import transaction, utc_now
from imagory.db.photos import _photo_ids_of, refresh_photos

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

    Photos that keep files in other sources get their primary file and date recomputed.
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
    touched. Photos keeping other files get their primary file and date recomputed.
    """
    with transaction(conn):
        photo_ids = _photo_ids_of(conn, "source_id = ? AND status = 'missing'", (source_id,))
        files = conn.execute(
            "DELETE FROM files WHERE source_id = ? AND status = 'missing'", (source_id,)
        ).rowcount
        photos = refresh_photos(conn, photo_ids)
    return RemovedMissing(files, photos)
