"""Source management that is more than one query: folder checks, relocation, removal.

Nothing here touches the user's photo folders except to list or stat files.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from imagory import db, thumbs

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
    source = db.create_source(conn, str(folder), label, type, type_overridden=type != detected_type)
    # SQLite can reuse the id of the last removed source; never inherit its thumbnails.
    thumbs.remove_source_thumbs(thumbs_root, source.id)
    log.info("Source %d added: %s (%s, %s)", source.id, folder, label, type)
    return source


def remove_source(conn: sqlite3.Connection, thumbs_root: Path, source_id: int) -> None:
    """Forget a source: its thumbnails first (they regenerate if this stops halfway), then
    its rows. Only called after the user confirms. Files on disk are never touched."""
    thumbs.remove_source_thumbs(thumbs_root, source_id)
    db.delete_source(conn, source_id)
    log.info("Source %d removed", source_id)
