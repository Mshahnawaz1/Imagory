"""What the viewer's info panel shows about one photo."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from imagory.db.people import people_for_photo

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
