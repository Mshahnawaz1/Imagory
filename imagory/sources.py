"""Source management that is more than one query: folder checks, relocation, removal.

Nothing here touches the user's photo folders except to list or stat files.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from imagory import dates, db, paths, takeout, thumbs
from imagory.thumbs import fold_path

log = logging.getLogger(__name__)

RELOCATE_SAMPLE = 50  # known files checked in a relocation target


def _norm(path: Path | str) -> str:
    # Windows paths are case-insensitive; normcase is a no-op elsewhere, so fold explicitly.
    return os.path.normcase(os.path.abspath(path)).casefold()


def _contains(outer: str, inner: str) -> bool:
    """Whether normalized path ``inner`` is ``outer`` or lies inside it."""
    if inner == outer:
        return True
    prefix = outer if outer.endswith(os.sep) else outer + os.sep
    return inner.startswith(prefix)


@dataclass(frozen=True)
class FolderCheck:
    error: str | None = None  # the folder cannot be used
    warning: str | None = None  # the folder can be used after the user confirms


def check_folder(
    folder: Path, sources: list[db.Source], library_root: Path, ignore_id: int | None = None
) -> FolderCheck:
    """Check a folder about to be added (or relocated to) against the existing sources.

    ``ignore_id`` is the source being relocated, which may keep or overlap its old folder.
    """
    target = _norm(folder)
    if _contains(_norm(library_root), target):
        return FolderCheck(error="This folder is inside Imagory's own library folder.")
    for source in sources:
        if source.id == ignore_id:
            continue
        existing = _norm(source.root_path)
        if existing == target:
            return FolderCheck(error=f"This folder is already added as “{source.label}”.")
        if _contains(existing, target) or _contains(target, existing):
            return FolderCheck(
                warning=f"This folder overlaps the source “{source.label}” "
                f"({source.root_path}). Photos in both will appear twice until duplicate "
                "detection is available."
            )
    return FolderCheck()


def relocation_matches(conn: sqlite3.Connection, source_id: int, folder: Path) -> tuple[int, int]:
    """How many of a sample of the source's files exist under ``folder``: (found, checked)."""
    sample = db.source_file_paths(conn, source_id, RELOCATE_SAMPLE)
    found = sum(folder.joinpath(*rel.split("/")).is_file() for rel in sample)
    return found, len(sample)


def add_source(
    conn: sqlite3.Connection,
    thumbs_root: Path,
    folder: Path,
    label: str,
    type: str,
    detected_type: str,
) -> db.Source:
    # Thumbnails are keyed by content hash, not source, so a reused id inherits nothing.
    source = db.create_source(conn, str(folder), label, type, type_overridden=type != detected_type)
    log.info("Source %d added: %s (%s, %s)", source.id, folder, label, type)
    return source


def remove_source(conn: sqlite3.Connection, thumbs_root: Path, source_id: int) -> None:
    """Forget a source: the thumbnails of photos no other source has first (they
    regenerate if this stops halfway), then its rows. Only called after the user
    confirms. Files on disk are never touched."""
    thumbs.remove_thumbnails(thumbs_root, db.source_orphan_hashes(conn, source_id))
    db.delete_source(conn, source_id)
    log.info("Source %d removed", source_id)


def remove_missing(
    conn: sqlite3.Connection, thumbs_root: Path, source_id: int
) -> db.RemovedMissing:
    """The "Remove missing items" action (PRD §8.2): forget a source's missing files, and the photos
    left with no files with their thumbnails. Only called after the user confirms; never
    automatic. Files on disk are never touched."""
    thumbs.remove_thumbnails(thumbs_root, db.missing_orphan_hashes(conn, source_id))
    removed = db.delete_missing_files(conn, source_id)
    log.info(
        "Source %d: removed %d missing files and %d photos",
        source_id,
        removed.files,
        removed.photos,
    )
    return removed


def _list_names(folder: Path) -> list[str] | None:
    """File names in a folder (read-only), or None if it cannot be read."""
    try:
        with os.scandir(folder) as entries:
            return [e.name for e in entries if e.is_file(follow_symlinks=False)]
    except OSError:
        return None


def recompute_dates(conn: sqlite3.Connection, source: db.Source) -> int:
    """Recompute every file date of a source from the stored inputs (PRD §8.1, §8.4): EXIF
    time and offset, file name, modified time, and for Takeout the sidecar. Used when the
    user changes the source type. Returns the number of files whose date changed.

    A ``cleaned`` source reads its output database instead (see
    :func:`_compute_cleaned_dates`) and raises ``FileNotFoundError`` if it is unavailable.

    For a Takeout source the sidecars are matched again from the folders on disk (read
    only). A folder that cannot be read keeps the sidecar path stored earlier. Photos are
    not re-read, and Trash files already imported stay. The database is only written when
    everything was computed, so a failure changes nothing.
    """
    if source.type == "cleaned":
        updates = _compute_cleaned_dates(conn, source)
    else:
        updates = _compute_dates(conn, source)
    with db.transaction(conn):
        changed = db.apply_recomputed_dates(conn, source.id, updates)
    log.info("Source %d: recomputed dates as %s, %d changed", source.id, source.type, changed)
    return changed


def _compute_dates(conn: sqlite3.Connection, source: db.Source) -> list[db.RecomputedDate]:
    """The recomputed date of every file of ``source``, as its type says. Reads only.
    Raises ``FileNotFoundError`` for a Takeout source whose folder is unavailable."""
    is_takeout = source.type == "takeout"
    root = Path(source.root_path)
    if is_takeout and not root.is_dir():
        # Sidecars cannot be read, and guessing would downgrade stored Takeout dates.
        raise FileNotFoundError(f"Source folder not found: {root}")
    rows = db.date_inputs(conn, source.id)
    matched: dict[str, str] = {}  # image path -> sidecar path
    if is_takeout:
        by_dir: dict[str, list[str]] = {}
        for row in rows:
            rel_dir, _, name = row.relative_path.rpartition("/")
            by_dir.setdefault(rel_dir, []).append(name)
        for rel_dir, images in by_dir.items():
            folder = root.joinpath(*rel_dir.split("/")) if rel_dir else root
            listed = _list_names(folder)
            if listed is None:
                continue
            prefix = f"{rel_dir}/" if rel_dir else ""
            result = takeout.match_folder(folder, listed)
            for image in images:
                sidecar = result.matches.get(image)
                if sidecar is not None:
                    matched[prefix + image] = prefix + sidecar
    updates: list[db.RecomputedDate] = []
    for row in rows:
        sidecar_path = matched.get(row.relative_path) or (
            row.sidecar_relative_path if is_takeout else None
        )
        sidecar = None
        if sidecar_path is not None:
            sidecar = takeout.read_sidecar(root.joinpath(*sidecar_path.split("/")))
        result = dates.resolve_date(
            source.type,
            mtime_ns=row.modified_time,
            takeout_timestamp=sidecar.taken_timestamp if sidecar else None,
            exif_datetime=row.exif_taken_at,
            exif_offset=row.exif_offset,
            filename=row.relative_path.rsplit("/", 1)[-1],
        )
        updates.append(
            db.RecomputedDate(
                row.id,
                result.taken_at_local,
                result.taken_at_utc,
                result.date_source,
                sidecar_path,
                sidecar.latitude if sidecar else None,
                sidecar.longitude if sidecar else None,
            )
        )
    return updates


def change_source_type(conn: sqlite3.Connection, source_id: int, type: str) -> int:
    """Set a source's type (the user's choice) and recompute its dates, in one transaction.
    Returns how many files' dates changed. Run it on the thread that owns the connection,
    never during a scan of the same library.

    Raises ``FileNotFoundError`` when the new type is Takeout and the source folder is
    unavailable; the type and the dates then stay as they were.
    """
    if type not in db.SOURCE_TYPES:
        raise ValueError(f"Unknown source type: {type}")
    source = db.get_source(conn, source_id)
    assert source is not None
    if source.type == "cleaned":
        raise ValueError("The type of a cleaned source can't be changed")
    updates = _compute_dates(conn, replace(source, type=type))
    with db.transaction(conn):
        db.set_source_type(conn, source_id, type)
        changed = db.apply_recomputed_dates(conn, source_id, updates)
    log.info("Source %d: type set to %s, %d dates changed", source_id, type, changed)
    return changed


# --- cleaned sources (PRD §8.10) -----------------------------------------------------------
# A cleanup output folder added to the main library. Its ``.imagory/imagory.db`` is only
# read (see ``db.open_output_database``); nothing is ever written into the folder.

CLEANED_ALBUM_SUFFIX = "Takeout"
_ALBUMS_IMPORTED_KEY = "cleaned_albums_imported:{id}:{added_at}"


def is_cleanup_output(folder: Path) -> bool:
    """Whether ``folder`` holds a cleanup output database (``.imagory/imagory.db``)."""
    return paths.cleanup_db_path(Path(folder)).is_file()


def detect_source_type(folder: Path) -> takeout.Detection:
    """The Add source detection: a cleanup output is ``cleaned``, otherwise Takeout or plain
    as :func:`takeout.detect_source_type` says."""
    if is_cleanup_output(folder):
        return takeout.Detection(
            "cleaned", "It is a Takeout cleanup made by Imagory (it holds .imagory/imagory.db)."
        )
    return takeout.detect_source_type(folder)


def source_for_folder(conn: sqlite3.Connection, folder: Path) -> db.Source | None:
    """The source whose root is ``folder`` (compared like Windows paths), if any."""
    target = _norm(folder)
    return next((s for s in db.list_sources(conn) if _norm(s.root_path) == target), None)


def add_cleaned_source(
    conn: sqlite3.Connection, folder: Path, label: str | None = None
) -> db.Source:
    """Add a cleanup output folder as a ``cleaned`` source, or return the cleaned source it
    already is (a later run into the same output is rescanned, not added twice).

    Raises ``ValueError`` if the folder is not a cleanup output, or is already a source of
    another type. The caller scans the source, then calls :func:`import_run_albums`.
    """
    existing = source_for_folder(conn, folder)
    if existing is not None:
        if existing.type != "cleaned":
            raise ValueError(
                f"This folder is already added as “{existing.label}” ({existing.type})."
            )
        return existing
    if not is_cleanup_output(folder):
        raise ValueError(f"{folder} is not a Takeout cleanup output")
    label = label or Path(folder).name or str(folder)
    source = db.create_source(conn, str(folder), label, "cleaned")
    log.info("Cleaned source %d added: %s", source.id, folder)
    return source


def takeout_album_name(name: str, taken: set[str]) -> str:
    """The name for an imported album: ``name`` if no album has it (ignoring case), else
    ``<name> (Takeout)``, then ``<name> (Takeout 2)`` and so on. ``taken`` is casefolded."""
    if name.casefold() not in taken:
        return name
    candidate = f"{name} ({CLEANED_ALBUM_SUFFIX})"
    n = 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{name} ({CLEANED_ALBUM_SUFFIX} {n})"
    return candidate


@dataclass(frozen=True)
class ImportedAlbum:
    album_id: int
    name: str  # as created in the main library
    output_name: str  # the album's name in the output database
    photos: int


def import_run_albums(
    main_conn: sqlite3.Connection,
    output_db: Path,
    run_id: int | None,
    source_id: int,
) -> list[ImportedAlbum]:
    """Create the albums of a cleanup run (or, with ``run_id`` None, of every finished run)
    in the main library, once per run. Call it after the cleaned source ``source_id`` has
    been scanned, since members are found through its file rows.

    Runs already imported for this source are skipped, so calling it again does nothing.
    Albums of the runs imported together are merged by name (ignoring case). A name already
    used in the main library gets ``(Takeout)``, ``(Takeout 2)``, …; existing albums are
    never changed. Members are photos only (videos and other files are not in the grid), and
    an album with no photo in the library is not created. The output database is opened
    read only. Raises ``ValueError`` if the source is not a scanned cleaned source, and
    ``db.OutputDatabaseError`` if the output database can't be read.
    """
    source = db.get_source(main_conn, source_id)
    if source is None or source.type != "cleaned":
        raise ValueError(f"Source {source_id} is not a cleaned source")
    if source.last_scan_at is None:
        raise ValueError(f"Scan the cleaned source “{source.label}” before importing albums")
    key = _ALBUMS_IMPORTED_KEY.format(id=source.id, added_at=source.added_at)
    done_runs: set[int] = set(json.loads(db.get_setting(main_conn, key) or "[]"))

    with db.open_output_database(output_db) as out:
        try:
            finished = [r.id for r in db.list_runs(out) if r.status == "done"]
            cleaned_ids = [s.id for s in db.list_sources(out) if s.type == "cleaned"]
            wanted = [r for r in finished if r not in done_runs and (run_id is None or r == run_id)]
            by_name: dict[str, tuple[str, list[str]]] = {}
            for rid in wanted:
                for cid in cleaned_ids:
                    for album in db.export_albums(out, cid, rid):
                        entry = by_name.setdefault(album.name.casefold(), (album.name, []))
                        entry[1].extend(album.files)
        except sqlite3.Error as exc:
            raise db.OutputDatabaseError(f"Cannot read {output_db}: {exc}") from exc
    if not wanted:
        return []

    taken = {a.name.casefold() for a in db.list_albums(main_conn)}
    planned: list[tuple[str, str, list[int]]] = []  # (new name, output name, photo ids)
    for output_name, files in sorted(by_name.values(), key=lambda a: (a[0].casefold(), a[0])):
        photo_ids = db.photo_ids_at_paths(main_conn, source_id, files)
        if not photo_ids:
            log.info("Album %r has no photos in the library; not created", output_name)
            continue
        name = takeout_album_name(output_name, taken)
        taken.add(name.casefold())
        planned.append((name, output_name, photo_ids))
    marker = (key, json.dumps(sorted(done_runs | set(wanted))))
    ids = db.create_albums_with_photos(main_conn, [(n, p) for n, _, p in planned], marker)
    log.info("Imported %d albums from runs %s of %s", len(ids), wanted, output_db)
    return [
        ImportedAlbum(album_id, name, output_name, len(photo_ids))
        for album_id, (name, output_name, photo_ids) in zip(ids, planned, strict=True)
    ]


def swap_ready(conn: sqlite3.Connection, cleaned_source_id: int) -> bool:
    """Whether the old Takeout source may be swapped out: the cleaned source exists and its
    latest scan completed (not cancelled, failed or still running)."""
    source = db.get_source(conn, cleaned_source_id)
    return (
        source is not None
        and source.type == "cleaned"
        and db.last_scan_status(conn, cleaned_source_id) == "completed"
    )


def swap_orphan_count(conn: sqlite3.Connection, old_source_id: int) -> int:
    """How many photos the swap would remove because no other source has them (for
    example Trash files, or files whose copy failed). For the confirmation message."""
    return len(db.source_orphan_hashes(conn, old_source_id))


def swap_source(
    conn: sqlite3.Connection, thumbs_root: Path, old_source_id: int, cleaned_source_id: int
) -> None:
    """Remove the old Takeout source after its cleanup output was added and scanned (the
    swap offer). Only called after the user confirms. Uses :func:`remove_source`, so only
    database rows go; files on disk are never touched. Photos also in the cleaned source
    keep their ids, albums, people and metadata, because a photo is its content hash.

    Raises ``ValueError`` when the cleaned source isn't ready (:func:`swap_ready`), or the
    old source is missing, the cleaned source itself, or another cleaned source.
    """
    if old_source_id == cleaned_source_id:
        raise ValueError("The old source and the cleaned source are the same")
    old = db.get_source(conn, old_source_id)
    if old is None or old.type == "cleaned":
        raise ValueError(f"Source {old_source_id} can't be swapped out")
    if not swap_ready(conn, cleaned_source_id):
        raise ValueError("The cleaned source has not finished scanning yet")
    remove_source(conn, thumbs_root, old_source_id)
    log.info("Swapped source %d for cleaned source %d", old_source_id, cleaned_source_id)


def _compute_cleaned_dates(conn: sqlite3.Connection, source: db.Source) -> list[db.RecomputedDate]:
    """Recomputed dates of a cleaned source: a file in its output database (same path,
    same hash) takes the stored date and location, which may be no date at all; any other
    file follows the plain-folder rules. Reads only. Raises ``FileNotFoundError`` when the
    folder or its database is unavailable, since guessing would downgrade stored dates."""
    root = Path(source.root_path)
    db_file = paths.cleanup_db_path(root)
    if not root.is_dir() or not db_file.is_file():
        raise FileNotFoundError(f"Cleanup output database not found: {db_file}")
    meta = {fold_path(m.relative_path): m for m in db.read_output_metadata(db_file)}
    updates: list[db.RecomputedDate] = []
    for row in db.cleaned_date_inputs(conn, source.id):
        m = meta.get(fold_path(row.relative_path))
        if m is not None and m.content_hash == row.content_hash:
            # The stored date may be None: an undated cleaned file stays undated.
            updates.append(
                db.RecomputedDate(
                    row.id,
                    m.taken_at_local,
                    m.taken_at_utc,
                    m.date_source,
                    None,
                    m.latitude,
                    m.longitude,
                )
            )
            continue
        result = dates.resolve_date(
            "plain",
            mtime_ns=row.modified_time,
            exif_datetime=row.exif_taken_at,
            exif_offset=row.exif_offset,
            filename=row.relative_path.rsplit("/", 1)[-1],
        )
        updates.append(
            db.RecomputedDate(
                row.id, result.taken_at_local, result.taken_at_utc, result.date_source, None
            )
        )
    return updates
