"""The grid's rows and month counts, unfiltered and filtered (search, sources, dates, albums)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple


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
