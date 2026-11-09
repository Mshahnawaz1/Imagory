"""Shared types for Takeout cleanup (docs/takeout-cleanup-plan.md).

Plain frozen dataclasses with no logic, shared by the planner, the copy step, the export writers,
the pipeline and the wizard. Paths are ``/``-separated relative strings (PRD §9) unless typed as
``Path``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FileKind = Literal["photo", "other", "json"]  # json = a JSON file that isn't a used sidecar


@dataclass(frozen=True)
class Candidate:
    """One file found in the input, or already in the output."""

    key: int  # caller's id (file / extra_file row), opaque to the planner
    kind: FileKind
    relative_path: str  # input-relative; output-relative when in_output
    content_hash: str
    size: int
    title: str | None  # sidecar title, if any
    has_sidecar: bool
    taken_at_local: str | None  # already chosen: sidecar, EXIF, filename; never mtime
    album_folder: str | None  # Takeout album folder name, "Archive", or None (year folder)
    in_output: bool = False  # copied by an earlier run


@dataclass(frozen=True)
class Rename:
    original: str  # name it would have had
    target: str  # output-relative path it got
    reason: Literal["sanitized", "clash", "long_path", "title_extension"]


@dataclass(frozen=True)
class PlannedCopy:
    key: int
    content_hash: str
    target_relative_path: str  # output-relative; JSON goes under .imagory/takeout-json/


@dataclass(frozen=True)
class Plan:
    copies: tuple[PlannedCopy, ...]  # one per new hash; JSON files are not deduplicated
    kept_key_by_hash: Mapping[str, int]  # includes in-output files
    renames: tuple[Rename, ...]


@dataclass(frozen=True)
class CopyResult:
    ok: bool
    size: int | None
    mtime_ns: int | None
    error: str | None


@dataclass(frozen=True)
class ExportOrigin:
    run: int
    path: str
    sidecar: str | None
    album: str | None


@dataclass(frozen=True)
class ExportFile:
    path: str
    kind: Literal["photo", "other"]
    size: int
    content_hash: str
    width: int | None = None
    height: int | None = None
    format: str | None = None
    taken_at_local: str | None = None
    taken_at_utc: str | None = None
    date_source: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    people: tuple[str, ...] = ()
    description: str | None = None
    favorite: bool = False
    uploaded_at_utc: str | None = None
    origins: tuple[ExportOrigin, ...] = ()


@dataclass(frozen=True)
class ExportRun:
    id: int
    input_path: str
    started_at: str
    finished_at: str | None
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ExportAlbum:
    name: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class CleanupReport:
    counts: Mapping[str, int]
    trash_skipped: tuple[str, ...]
    duplicates: tuple[tuple[str, str], ...]  # (kept output path, skipped input path)
    unmatched_sidecars: tuple[str, ...]
    unreadable: tuple[str, ...]
    renames: tuple[Rename, ...]
    unaccounted: tuple[str, ...]  # non-empty means "safe to delete" is not shown


@dataclass(frozen=True)
class FolderCheck:
    mode: Literal["new", "add", "resume", "refused"]
    message: str


@dataclass(frozen=True)
class PreviewCounts:
    photos: int
    other_files: int
    json_files: int
    duplicates: int
    duplicate_bytes: int
    trash_skipped: int
    unmatched_sidecars: int
    albums: int
    bytes_to_copy: int
    free_bytes: int


@dataclass(frozen=True)
class DoneResult:
    copied: int
    failed: tuple[str, ...]
    unaccounted: tuple[str, ...]
    safe_to_delete: bool
    report_path: Path
    output_path: Path
    input_is_source: bool  # the input is already a source in the main library


class CleanupError(Exception):
    """A cleanup cannot go on; the message is meant for the user."""


class NotEnoughSpace(CleanupError):
    """The output drive has less free space than the copy needs (bytes plus 5%)."""
