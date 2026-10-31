"""Source management that is more than one query: folder checks, relocation, removal.

Nothing here touches the user's photo folders except to list or stat files.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from imagory import dates, db, takeout, thumbs

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

    For a Takeout source the sidecars are matched again from the folders on disk (read
    only). A folder that cannot be read keeps the sidecar path stored earlier. Photos are
    not re-read, and Trash files already imported stay. The database is only written when
    everything was computed, so a failure changes nothing.
    """
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
    updates = _compute_dates(conn, replace(source, type=type))
    with db.transaction(conn):
        db.set_source_type(conn, source_id, type)
        changed = db.apply_recomputed_dates(conn, source_id, updates)
    log.info("Source %d: type set to %s, %d dates changed", source_id, type, changed)
    return changed
