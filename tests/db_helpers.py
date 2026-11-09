"""Small read-only queries the tests use to check database state."""

from __future__ import annotations

import sqlite3

from imagory import db


def album_ids_of(conn: sqlite3.Connection, photo_id: int) -> list[int]:
    """Ids of the albums containing a photo, in id order."""
    rows = conn.execute(
        "SELECT album_id FROM album_photos WHERE photo_id = ? ORDER BY album_id", (photo_id,)
    )
    return [r[0] for r in rows]


def album_photos(conn: sqlite3.Connection, album_id: int, **kw) -> list[db.GridPhoto]:
    """The grid rows the album view shows."""
    return db.filtered_grid_photos(conn, db.PhotoFilter(album_id=album_id), **kw)


def count(conn: sqlite3.Connection, f: db.PhotoFilter) -> int:
    """How many photos the grid shows for a filter."""
    return len(db.filtered_grid_photos(conn, f))
