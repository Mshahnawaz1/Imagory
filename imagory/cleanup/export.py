"""Writers for the Takeout cleanup export: ``metadata.json`` and the cleanup report.

Both writers are atomic: the text goes to ``<path>.tmp`` first and is then moved into place with
``os.replace``. If writing fails, only that ``.tmp`` file is deleted and the error is re-raised.
Neither writer creates folders; the caller does. No SQL, no Qt, no photo files are touched.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import IO, Any

from imagory.cleanup.types import (
    CleanupReport,
    ExportAlbum,
    ExportFile,
    ExportOrigin,
    ExportRun,
    Rename,
)

METADATA_FORMAT = "imagory-takeout-cleanup"
METADATA_VERSION = 1

# Fields that only make sense for photos. They are left out of "other" files entirely.
_PHOTO_ONLY_FIELDS = frozenset({"width", "height", "format"})


def _atomic_write(path: Path, write: Callable[[IO[str]], None]) -> None:
    """Write through ``<path>.tmp`` and move it into place; delete the temp file on failure."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            write(handle)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _origin_dict(origin: ExportOrigin) -> dict[str, Any]:
    return {
        "run": origin.run,
        "path": origin.path,
        "sidecar": origin.sidecar,
        "album": origin.album,
    }


def _file_dict(item: ExportFile) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in fields(ExportFile):
        name = field.name
        value = getattr(item, name)
        if name in _PHOTO_ONLY_FIELDS and item.kind != "photo":
            continue
        if name == "people":
            out[name] = list(value)
        elif name == "origins":
            out[name] = [_origin_dict(origin) for origin in value]
        else:
            out[name] = value
    return out


def _run_dict(run: ExportRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "input_path": run.input_path,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "counts": dict(run.counts),
    }


def _album_dict(album: ExportAlbum) -> dict[str, Any]:
    return {"name": album.name, "files": list(album.files)}


def write_metadata_json(
    path: Path,
    runs: Sequence[ExportRun],
    albums: Sequence[ExportAlbum],
    files: Sequence[ExportFile],
    generated_at: str,
) -> None:
    """Write the cleanup ``metadata.json`` (format in docs/takeout-cleanup.md)."""
    document: dict[str, Any] = {
        "format": METADATA_FORMAT,
        "version": METADATA_VERSION,
        "generated_at": generated_at,
        "runs": [_run_dict(run) for run in runs],
        "albums": [_album_dict(album) for album in albums],
        "files": [_file_dict(item) for item in files],
    }

    def write(handle: IO[str]) -> None:
        json.dump(document, handle, ensure_ascii=False, indent=1)
        handle.write("\n")

    _atomic_write(path, write)


def _report_line(text: str) -> str:
    """Keep one entry on one line: escape control characters (for example a newline in a name).

    Backslashes and printable characters are left alone, so ordinary paths read as written.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(char)
    return "".join(out)


def _rename_line(rename: Rename) -> str:
    return f"{_report_line(rename.original)}  ->  {_report_line(rename.target)}  ({rename.reason})"


def _counts_lines(counts: Mapping[str, int]) -> list[str]:
    if not counts:
        return ["(none)"]
    return [f"{_report_line(str(key))}: {value}" for key, value in counts.items()]


def _path_lines(paths: Sequence[str]) -> list[str]:
    if not paths:
        return ["(none)"]
    return [_report_line(path) for path in paths]


def _duplicate_lines(pairs: Sequence[tuple[str, str]]) -> list[str]:
    if not pairs:
        return ["(none)"]
    return [f"{_report_line(kept)}  <-  {_report_line(skipped)}" for kept, skipped in pairs]


def _rename_lines(renames: Sequence[Rename]) -> list[str]:
    if not renames:
        return ["(none)"]
    return [_rename_line(rename) for rename in renames]


def render_report(report: CleanupReport) -> str:
    """Return the report text: sections in the fixed order, each with a heading."""
    sections: list[tuple[str, list[str]]] = [
        ("Counts", _counts_lines(report.counts)),
        ("Trash skipped", _path_lines(report.trash_skipped)),
        ("Duplicates", _duplicate_lines(report.duplicates)),
        ("Unmatched sidecars", _path_lines(report.unmatched_sidecars)),
        ("Unreadable files", _path_lines(report.unreadable)),
        ("Renames", _rename_lines(report.renames)),
        ("Not accounted for", _path_lines(report.unaccounted)),
    ]
    blocks: list[str] = []
    for heading, lines in sections:
        blocks.append("\n".join([heading, *lines]) + "\n")
    return "\n".join(blocks)


def write_report(path: Path, report: CleanupReport) -> None:
    """Write the plain-text cleanup report (UTF-8), atomically."""
    text = render_report(report)

    def write(handle: IO[str]) -> None:
        handle.write(text)

    _atomic_write(path, write)
