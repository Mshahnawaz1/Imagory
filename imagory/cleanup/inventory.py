"""Stage 3 inventory: every file of the input, sorted into photos, other files and JSON.

Also used after a copy to check that the input is "safe to delete". Read only.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from imagory import takeout
from imagory.cleanup.checks import norm_path
from imagory.scanner import SUPPORTED_EXTENSIONS, WalkedFile, extension_of, is_skipped_dir

log = logging.getLogger(__name__)


# Takeout's year folders. Only the English name is confirmed (PRD §18 item 6).
_YEAR_FOLDER = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)


@dataclass
class Inventory:
    """Every file under the input, sorted for the cleanup. Paths are input-relative."""

    files: dict[str, WalkedFile] = field(default_factory=dict)  # every non-Trash file
    trash: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # not walked: links, hidden, unreadable
    images: list[str] = field(default_factory=list)  # supported image extensions
    others: list[str] = field(default_factory=list)  # neither an image nor JSON
    jsons: list[str] = field(default_factory=list)  # every JSON file
    sidecar_of: dict[str, str] = field(default_factory=dict)  # media path -> sidecar path
    used_sidecars: set[str] = field(default_factory=set)
    complete: bool = True

    @property
    def unused_json(self) -> list[str]:
        """JSON files that aren't any file's sidecar (copied to ``takeout-json/``)."""
        return sorted(j for j in self.jsons if j not in self.used_sidecars)

    @property
    def unmatched_sidecars(self) -> list[str]:
        """Unused JSON files that look like sidecars (not album or account metadata)."""
        return [
            j
            for j in self.unused_json
            if j.rpartition("/")[2].casefold() not in takeout.NON_SIDECAR_JSON
        ]


def _is_trash(rel_dir: str) -> bool:
    return takeout.is_trash_dir(rel_dir)


def walk_inventory(
    root: Path,
    exclude: Path | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[list[WalkedFile], list[str], list[str], bool]:
    """List every file under ``root`` with the scan's walk rules (PRD §7.2), all extensions.

    Returns ``(files, trash, skipped, complete)``: files outside Trash, files inside Takeout's
    Trash, and what was not walked (symlinks, junctions, hidden and system folders, the
    library folder, entries that can't be read; folders end in ``/``). Nothing is opened.
    """
    files: list[WalkedFile] = []
    trash: list[str] = []
    skipped: list[str] = []
    exclude_norm = norm_path(exclude) if exclude is not None else None
    stack: list[tuple[str, str, bool]] = [(str(root), "", False)]
    while stack:
        if cancelled():
            return files, trash, skipped, False
        dir_path, rel_dir, in_trash = stack.pop()
        try:
            with os.scandir(dir_path) as entries:
                children = sorted(entries, key=lambda e: e.name)
        except OSError as exc:
            log.warning("Cannot read folder %s: %s", dir_path, exc)
            skipped.append(f"{rel_dir}/" if rel_dir else "./")
            continue
        subdirs: list[tuple[str, str, bool]] = []
        for entry in children:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_symlink() or entry.is_junction():
                    skipped.append(rel)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if is_skipped_dir(entry.name) or (
                        exclude_norm is not None and norm_path(entry.path) == exclude_norm
                    ):
                        skipped.append(rel + "/")
                        continue
                    subdirs.append((entry.path, rel, in_trash or _is_trash(rel)))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped.append(rel)
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                log.warning("Cannot stat %s: %s", entry.path, exc)
                skipped.append(rel)
                continue
            if in_trash:
                trash.append(rel)
            else:
                files.append(WalkedFile(rel, st.st_size, st.st_mtime_ns))
        stack.extend(reversed(subdirs))
    return files, trash, skipped, True


def match_other_files(
    names: Iterable[str],
    json_names: Iterable[str],
    taken: Iterable[str] = (),
    read_title: Callable[[str], str | None] | None = None,
) -> dict[str, str | None]:
    """Map non-image file names (videos, RAW, …) in one folder to their sidecar, or None.

    The same rules as for images (``takeout.match_directory`` with ``is_media``): plain and
    ``.supplemental-metadata`` forms, truncation with title checks, moved counters, and
    ``-edited`` copies borrowing their original's sidecar. Sidecars in ``taken`` (already
    used by an image) are not offered.
    """
    files = set(names)
    taken_set = set(taken)
    offered = [j for j in json_names if j not in taken_set and j not in files]
    return takeout.match_directory(
        [*files, *offered], read_title, is_media=files.__contains__
    ).matches


def build_inventory(
    root: Path, exclude: Path | None = None, cancelled: Callable[[], bool] = lambda: False
) -> Inventory:
    """Walk the input (every extension) and match every file to its sidecar, folder by
    folder. Images use ``takeout.match_folder`` exactly as the scan does; other files use
    :func:`match_other_files`. Sidecar titles are read (read only) when matching needs them.
    """
    files, trash, skipped, complete = walk_inventory(root, exclude, cancelled)
    inv = Inventory(trash=trash, skipped=skipped, complete=complete)
    if not complete:
        return inv
    by_dir: dict[str, list[str]] = {}
    for f in files:
        inv.files[f.relative_path] = f
        rel_dir, _, name = f.relative_path.rpartition("/")
        by_dir.setdefault(rel_dir, []).append(name)
    for rel_dir in sorted(by_dir):
        if cancelled():
            inv.complete = False
            return inv
        names = by_dir[rel_dir]
        prefix = f"{rel_dir}/" if rel_dir else ""
        folder = root.joinpath(*rel_dir.split("/")) if rel_dir else root
        jsons = [n for n in names if n.casefold().endswith(takeout.SIDECAR_SUFFIX)]
        images = [n for n in names if extension_of(n) in SUPPORTED_EXTENSIONS]
        json_set, image_set = set(jsons), set(images)
        others = [n for n in names if n not in json_set and n not in image_set]
        inv.jsons += [prefix + n for n in jsons]
        inv.images += [prefix + n for n in images]
        inv.others += [prefix + n for n in others]
        image_match = takeout.match_folder(folder, images + jsons)
        used = {s for s in image_match.matches.values() if s is not None}
        other_match = match_other_files(
            others, jsons, used, lambda n, base=folder: takeout.read_title(base / n)
        )
        for name, side in [*image_match.matches.items(), *other_match.items()]:
            if side is not None:
                inv.sidecar_of[prefix + name] = prefix + side
                inv.used_sidecars.add(prefix + side)
    return inv


def album_dir_of(relative_path: str) -> str | None:
    """The input-relative album folder a file sits in, or None for a year folder, the root or
    a file directly in "Google Photos". The folder below "Google Photos" (or below the input
    root when there is none) is the album; ``Photos from YYYY`` is a year folder."""
    parts = relative_path.split("/")[:-1]
    base = next((i + 1 for i, p in enumerate(parts) if takeout.is_google_photos_name(p)), 0)
    if len(parts) <= base or _YEAR_FOLDER.match(parts[base]):
        return None
    return "/".join(parts[: base + 1])
