"""People read from Takeout sidecars and their links to photos (PRD §8.9)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


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
