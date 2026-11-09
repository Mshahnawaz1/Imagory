"""Manual albums and their photos (PRD §8.8)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from imagory.db.connection import transaction, utc_now

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
