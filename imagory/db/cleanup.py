"""Takeout cleanup (PRD §8.10): runs, origins, extra files, copies and finalising."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from imagory.cleanup.types import ExportAlbum, ExportFile, ExportOrigin, ExportRun
from imagory.dates import SOURCE_RANK, DateResult, best_photo_date
from imagory.db.connection import transaction, utc_now
from imagory.db.photos import _photo_ids_of, refresh_photos

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
        # Kept rows already in the output (earlier runs) take the best date of this run's
        # duplicates too (lead decision, wave 2); the files stay where they are on disk.
        # Done before this run's rows move, so each row is handled once.
        for file_id, extra_id in conn.execute(
            "SELECT DISTINCT o.file_id, o.extra_file_id FROM file_origins o "
            "LEFT JOIN files f ON f.id = o.file_id "
            "LEFT JOIN extra_files e ON e.id = o.extra_file_id "
            "WHERE o.run_id = ? AND (f.source_id = ? OR e.source_id = ?)",
            (run_id, cleaned_source_id, cleaned_source_id),
        ).fetchall():
            if file_id is not None:
                photo_id = _keep_best_file_date(conn, file_id)
                if photo_id is not None:
                    photo_ids.add(photo_id)
            else:
                _merge_extra_duplicates(conn, extra_id, temp_source_id)

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


# Pipeline helpers (imagory/cleanup/pipeline.py).


@dataclass(frozen=True)
class CleanupFileRow:
    """What the pipeline needs to know about one ``files`` row."""

    id: int
    relative_path: str
    size: int
    modified_time: int
    content_hash: str | None
    status: str
    sidecar_relative_path: str | None
    taken_at_local: str | None
    taken_at_utc: str | None
    date_source: str | None


def list_cleanup_files(conn: sqlite3.Connection, source_id: int) -> list[CleanupFileRow]:
    """A source's file rows (any status), by path."""
    rows = conn.execute(
        "SELECT id, relative_path, size, modified_time, content_hash, status, "
        "sidecar_relative_path, taken_at_local, taken_at_utc, date_source FROM files "
        "WHERE source_id = ? ORDER BY relative_path, id",
        (source_id,),
    )
    return [CleanupFileRow(*r) for r in rows]


def delete_extra_files(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    """Delete extra-file rows (stale stage 3 rows of a temporary source). An origin or copy
    still pointing at one makes this fail. Returns how many were deleted."""
    return sum(
        conn.execute("DELETE FROM extra_files WHERE id = ?", (i,)).rowcount for i in list(ids)
    )


def best_real_dates(conn: sqlite3.Connection, source_id: int) -> dict[str, DateResult]:
    """The best non-mtime date (PRD §8.4, across copies) per content hash, over a source's
    present files and its extra files. Hashes with no such date are absent."""
    found: dict[str, list[DateResult]] = {}
    for table, extra in (("files", " AND status = 'ok'"), ("extra_files", "")):
        for h, local, utc, source in conn.execute(
            f"SELECT content_hash, taken_at_local, taken_at_utc, date_source FROM {table} "
            f"WHERE source_id = ? AND content_hash IS NOT NULL AND taken_at_local IS NOT NULL "
            f"AND date_source IS NOT NULL AND date_source <> 'mtime'{extra}",
            (source_id,),
        ):
            if source in SOURCE_RANK:
                found.setdefault(h, []).append(DateResult(local, utc, source))
    result: dict[str, DateResult] = {}
    for h, dates in found.items():
        best = best_photo_date(dates)
        if best is not None:
            result[h] = best
    return result


@dataclass(frozen=True)
class CopyJob:
    """A planned copy with what copying it needs, from its kept (input) row."""

    id: int
    file_id: int | None
    extra_file_id: int | None
    source_relative_path: str
    target_relative_path: str
    status: str
    error: str | None
    content_hash: str | None
    size: int
    modified_time: int


def copy_jobs(conn: sqlite3.Connection, run_id: int, status: str | None = None) -> list[CopyJob]:
    """A run's copies joined to their kept row (hash, size, input mtime), in plan order."""
    sql = (
        "SELECT c.id, c.file_id, c.extra_file_id, c.source_relative_path, "
        "c.target_relative_path, c.status, c.error, "
        "COALESCE(f.content_hash, e.content_hash), COALESCE(f.size, e.size, 0), "
        "COALESCE(f.modified_time, e.modified_time, 0) FROM cleanup_copies c "
        "LEFT JOIN files f ON f.id = c.file_id "
        "LEFT JOIN extra_files e ON e.id = c.extra_file_id WHERE c.run_id = ?"
    )
    params: tuple = (run_id,)
    if status is not None:
        sql += " AND c.status = ?"
        params = (run_id, status)
    return [CopyJob(*r) for r in conn.execute(sql + " ORDER BY c.id", params)]


def copy_status_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """How many of a run's copies are ``pending``, ``done`` and ``error`` (all keys present)."""
    counts = dict.fromkeys(("pending", "done", "error"), 0)
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM cleanup_copies WHERE run_id = ? GROUP BY status", (run_id,)
    ):
        counts[status] = n
    return counts


def count_origins(conn: sqlite3.Connection, run_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM file_origins WHERE run_id = ?", (run_id,)).fetchone()[
        0
    ]


def run_duplicates(
    conn: sqlite3.Connection, run_id: int, temp_source_id: int
) -> list[tuple[str, int]]:
    """``(input path, size)`` of a run's origins that are not copied themselves (the
    duplicates skipped), while the temporary source still exists. By path.

    Built in Python from three linear scans (origins, copy sources, the temporary source's
    sizes), with exact path comparison: two input files may differ only in case.
    """
    copied = {
        r[0]
        for r in conn.execute(
            "SELECT source_relative_path FROM cleanup_copies WHERE run_id = ?", (run_id,)
        )
    }
    sizes: dict[str, int] = {}
    for table in ("extra_files", "files"):  # a file row wins over an extra row
        sizes.update(
            conn.execute(
                f"SELECT relative_path, size FROM {table} WHERE source_id = ?", (temp_source_id,)
            ).fetchall()
        )
    rows = conn.execute(
        "SELECT original_relative_path FROM file_origins WHERE run_id = ? "
        "ORDER BY original_relative_path, id",
        (run_id,),
    )
    return [(r[0], sizes.get(r[0]) or 0) for r in rows if r[0] not in copied]
