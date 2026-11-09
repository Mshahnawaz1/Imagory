"""Folder checks for the cleanup wizard's input and output pages (PRD §8.10).

Read only: folders are listed and ``stat``-ed, and the main library and output databases are
only read.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path

from imagory import db, paths, takeout
from imagory.cleanup import planning
from imagory.cleanup.types import FolderCheck

# Output-relative prefix of the JSON files a cleanup copies under ``.imagory``.
_JSON_PREFIX = planning.JSON_FOLDER + "/"
# Files an operating system drops into folders; they don't make an output folder "used".
_IGNORABLE_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})
_CLOUD_NAMES = frozenset(
    {"onedrive", "dropbox", "google drive", "googledrive", "my drive", "icloud drive"}
)


def norm_path(path: Path | str) -> str:
    # Windows paths are case-insensitive; normcase is a no-op elsewhere, so fold explicitly.
    return os.path.normcase(os.path.abspath(path)).casefold()


def path_contains(outer: str, inner: str) -> bool:
    """Whether normalised path ``inner`` is ``outer`` or lies inside it."""
    if inner == outer:
        return True
    prefix = outer if outer.endswith(os.sep) else outer + os.sep
    return inner.startswith(prefix)


def check_input(input_root: Path) -> tuple[bool, str]:
    """``(looks like Takeout, message)`` for the wizard's input page. A folder that isn't
    detected as Takeout may still be used after the warning."""
    if not Path(input_root).is_dir():
        return False, "This folder doesn't exist or can't be read."
    detection = takeout.detect_source_type(input_root)
    if detection.type == "takeout":
        return True, f"Google Takeout export. {detection.reason}"
    return False, (
        f"This doesn't look like a Google Takeout export ({detection.reason}) "
        "You can continue anyway."
    )


def storage_warning(folder: Path) -> str | None:
    """A warning when ``folder`` is in a cloud-synced folder or on a network share, where
    SQLite databases are at risk (PRD §6.4)."""
    text = str(folder)
    if text.startswith(("\\\\", "//")):
        return "This folder is on a network share."
    for part in Path(os.path.abspath(folder)).parts:
        folded = part.casefold()
        if folded in _CLOUD_NAMES or folded.startswith("onedrive - "):
            return f"This folder is inside “{part}”, a cloud-synced folder."
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(folder))[0]
        if drive and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4:  # DRIVE_REMOTE
            return "This folder is on a network drive."
    return None


def _storage_note(folder: Path) -> str:
    warning = storage_warning(folder)
    if warning is None:
        return ""
    return (
        f" Warning: {warning} The output holds a database, which can be damaged by sync "
        "tools and network drives; a local folder is safer."
    )


def unfinished_run(runs: Iterable[db.CleanupRun], input_root: Path | str) -> db.CleanupRun | None:
    """The latest unfinished run of ``input_root`` among ``runs``. Paths are compared with
    :func:`_norm` (``normcase`` and full casefold), the one comparison used everywhere."""
    target = norm_path(input_root)
    mine = [r for r in runs if r.status != "done" and norm_path(r.input_path) == target]
    return max(mine, key=lambda r: r.id, default=None)


def _output_state(input_root: Path, output_root: Path) -> FolderCheck:
    """``new``, ``add``, ``resume`` or ``refused`` from what is in the output folder. An
    existing output database is opened with ``db.open_output_database`` (nothing written)."""
    if not output_root.exists():
        return FolderCheck("new", "New folder: a new cleanup will be created.")
    db_file = paths.cleanup_db_path(output_root)
    if db_file.is_file():
        try:
            with db.open_output_database(db_file) as conn:
                return _existing_output_state(conn, input_root)
        except (db.OutputDatabaseError, sqlite3.Error):
            return FolderCheck("refused", "This folder holds a database that can't be read.")
    try:
        entries = [e for e in os.listdir(output_root) if e.casefold() not in _IGNORABLE_NAMES]
        if entries == [paths.CLEANUP_DIR_NAME] and paths.cleanup_dir(output_root).is_dir():
            entries = [
                e
                for e in os.listdir(paths.cleanup_dir(output_root))
                if e.casefold() not in _IGNORABLE_NAMES
            ]
    except OSError:
        return FolderCheck("refused", "This folder can't be read.")
    if entries:
        return FolderCheck(
            "refused",
            "This folder isn't empty. Choose an empty folder or an existing cleanup output.",
        )
    return FolderCheck("new", "Empty folder: a new cleanup will be created.")


def _existing_output_state(conn: sqlite3.Connection, input_root: Path) -> FolderCheck:
    version = db.schema_version(conn)
    if version > db.SCHEMA_VERSION:
        return FolderCheck(
            "refused", "This cleanup was made by a newer Imagory. Please update Imagory."
        )
    if version < db.OUTPUT_MIN_VERSION:
        return FolderCheck("refused", "This folder is not an Imagory cleanup output.")
    runs = db.list_runs(conn)
    mine = unfinished_run(runs, input_root)
    if mine is not None:
        counts = db.copy_status_counts(conn, mine.id)
        total = sum(counts.values())
        done = f" ({counts['done']} of {total} files copied)" if total else ""
        message = f"A cancelled cleanup of this Takeout folder will resume{done}."
        return FolderCheck("resume", message)
    others = [r for r in runs if r.status != "done"]
    if others:
        return FolderCheck(
            "refused",
            "This output has an unfinished cleanup of another folder "
            f"({others[-1].input_path}). Choose that folder as the input to finish it first.",
        )
    files = sum(1 for _ in cleaned_paths(conn))
    message = f"Existing cleanup: {files} files. The new files will be added to it."
    return FolderCheck("add", message)


def cleaned_paths(conn: sqlite3.Connection) -> Iterable[str]:
    for source in db.list_sources(conn):
        if source.type == "cleaned":
            for row in db.list_cleanup_files(conn, source.id):
                yield row.relative_path
            for extra in db.list_extra_files(conn, source.id):
                if not extra.relative_path.startswith(_JSON_PREFIX):
                    yield extra.relative_path


def check_folders(
    main_conn: sqlite3.Connection, library_root: Path, input_root: Path, output_root: Path
) -> FolderCheck:
    """Check the input and output folders (PRD §8.10 folder rules) for the wizard.

    Refused: a missing input, an output that is a file, the two folders inside each other,
    either inside the library folder, an output that is already a ``plain`` or ``takeout``
    source, a non-empty folder that isn't a cleanup output, and an output with an unfinished
    run of another input. Otherwise ``new`` (empty or missing folder), ``add`` (an existing
    cleanup output, also when it is a ``cleaned`` source) or ``resume`` (an unfinished run of
    this input). A cloud or network folder adds a warning to the message.

    Reads ``main_conn`` (sources only) and, read only, an existing output database. Nothing is
    written anywhere.
    """
    return check_folders_for(db.list_sources(main_conn), library_root, input_root, output_root)


def check_folders_for(
    sources: Iterable[db.Source], library_root: Path, input_root: Path, output_root: Path
) -> FolderCheck:
    """:func:`check_folders` against a list of main-library sources. Needs no connection, so
    the cleanup thread can run it on a snapshot taken on the main thread."""
    input_root, output_root = Path(input_root), Path(output_root)
    if not input_root.is_dir():
        return FolderCheck("refused", "The Takeout folder doesn't exist.")
    if output_root.exists() and not output_root.is_dir():
        return FolderCheck("refused", "The output is a file, not a folder.")
    inp, out, lib = norm_path(input_root), norm_path(output_root), norm_path(library_root)
    if inp == out:
        return FolderCheck("refused", "The output folder must differ from the Takeout folder.")
    if path_contains(inp, out):
        return FolderCheck("refused", "The output folder is inside the Takeout folder.")
    if path_contains(out, inp):
        return FolderCheck("refused", "The Takeout folder is inside the output folder.")
    if path_contains(lib, inp) or path_contains(lib, out):
        return FolderCheck("refused", "A folder is inside Imagory's own library folder.")
    cleaned_note = ""
    for source in sources:
        if norm_path(source.root_path) != out:
            continue
        if source.type != "cleaned":
            return FolderCheck(
                "refused",
                f"The output folder is already added as the source “{source.label}”.",
            )
        cleaned_note = (
            f" It is the library source “{source.label}”, which is rescanned once "
            "this run is added."
        )
    state = _output_state(input_root, output_root)
    if state.mode == "refused":
        return state
    return FolderCheck(state.mode, state.message + cleaned_note + _storage_note(output_root))


def input_is_source(main_conn: sqlite3.Connection, input_root: Path) -> bool:
    """Whether the input folder is already a source in the main library (the swap offer)."""
    target = norm_path(input_root)
    return any(norm_path(s.root_path) == target for s in db.list_sources(main_conn))
