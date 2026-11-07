"""Takeout cleanup pipeline (PRD §8.10, docs/takeout-cleanup-plan.md, stages 1 to 7).

Ties together the planner (:mod:`imagory.cleanup`), the copy primitive
(:mod:`imagory.cleanup_copy`), the export writers (:mod:`imagory.cleanup_export`) and the
cleanup SQL in :mod:`imagory.db`. No Qt: a ``CleanupThread`` runs it on a worker thread and
turns the ``progress`` callback into signals.

Safety (PRD principle 1):

* The **input** folder is only listed, ``stat``-ed and opened ``"rb"`` (by the Scanner's
  workers, the hash workers, sidecar reads and :func:`cleanup_copy.copy_verified`). Nothing
  here writes, renames, moves or deletes anything in it.
* Everything written goes under the **output** folder: copies through
  :meth:`CleanupPipeline._output_file` (which refuses any path that could leave the output),
  and the database, thumbnails, ``metadata.json``, reports and ``takeout-json/`` under
  ``<output>/.imagory``. :meth:`CleanupPipeline.open` refuses an output inside the input (and
  the other way round) before anything is created.
* The main library database is only read, by :func:`check_folders` and
  :func:`input_is_source`.

Cancel and resume need no saved state beyond the output database (as in PRD §6.3): every
stage picks up from what is committed there. One output database has one writer, the thread
running the pipeline; all stage methods must be called from the thread that called
:meth:`CleanupPipeline.open`.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import re
import shutil
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from imagory import cleanup, cleanup_copy, cleanup_export, dates, db, paths, takeout
from imagory.cleanup_types import (
    Candidate,
    CleanupReport,
    CopyResult,
    DoneResult,
    FolderCheck,
    Plan,
    PreviewCounts,
    Rename,
)
from imagory.scanner import (
    SUPPORTED_EXTENSIONS,
    Progress,
    Scanner,
    WalkedFile,
    default_workers,
    extension_of,
    is_skipped_dir,
)
from imagory.thumbs import HASH_BYTES, fold_path

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]  # (stage text, done, total)

COMMIT_BATCH = 200  # stage 3 rows per transaction
COPY_COMMIT_BATCH = 50  # copy results per transaction
HASH_BATCH = 16  # files per hash-worker task
HASH_CHUNK = 1024 * 1024
DEFAULT_COPY_THREADS = 4  # copying is I/O bound
DEFAULT_POOL_THRESHOLD = 64  # fewer files than this are hashed without a process pool

# Takeout's year folders. Only the English name is confirmed (PRD §18 item 6).
_YEAR_FOLDER = re.compile(r"^Photos from \d{4}$", re.IGNORECASE)
ARCHIVE_ALBUM = "Archive"
_ARCHIVE_NAMES = frozenset({"archive"})
ALBUM_METADATA_NAME = "metadata.json"
# Files an operating system drops into folders; they don't make an output folder "used".
_IGNORABLE_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})
_CLOUD_NAMES = frozenset(
    {"onedrive", "dropbox", "google drive", "googledrive", "my drive", "icloud drive"}
)
_JSON_PREFIX = cleanup.JSON_FOLDER + "/"


class CleanupError(Exception):
    """A cleanup cannot go on; the message is meant for the user."""


class NotEnoughSpace(CleanupError):
    """The output drive has less free space than the copy needs (bytes plus 5%)."""


# --- folder checks (wizard pages 1 and 2) -----------------------------------------------------


def _norm(path: Path | str) -> str:
    # Windows paths are case-insensitive; normcase is a no-op elsewhere, so fold explicitly.
    return os.path.normcase(os.path.abspath(path)).casefold()


def _contains(outer: str, inner: str) -> bool:
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


def _output_state(input_root: Path, output_root: Path) -> FolderCheck:
    """``new``, ``add``, ``resume`` or ``refused`` from what is in the output folder."""
    if not output_root.exists():
        return FolderCheck("new", "New folder: a new cleanup will be created.")
    db_file = paths.cleanup_db_path(output_root)
    if db_file.is_file():
        try:
            conn = db.connect_readonly(db_file)
        except sqlite3.Error:
            return FolderCheck("refused", "This folder holds a database that can't be read.")
        try:
            version = db.schema_version(conn)
            if version > db.SCHEMA_VERSION:
                return FolderCheck(
                    "refused", "This cleanup was made by a newer Imagory. Please update Imagory."
                )
            if version < 5:  # cleanup tables arrived with migration 5
                return FolderCheck("refused", "This folder is not an Imagory cleanup output.")
            unfinished = [r for r in db.list_runs(conn) if r.status != "done"]
            mine = [r for r in unfinished if _norm(r.input_path) == _norm(input_root)]
            if mine:
                counts = db.copy_status_counts(conn, mine[-1].id)
                total = sum(counts.values())
                done = f" ({counts['done']} of {total} files copied)" if total else ""
                return FolderCheck(
                    "resume", f"A cancelled cleanup of this Takeout folder will resume{done}."
                )
            if unfinished:
                return FolderCheck(
                    "refused",
                    "This output has an unfinished cleanup of another folder "
                    f"({unfinished[-1].input_path}). Choose that folder as the input to "
                    "finish it first.",
                )
            files = sum(1 for _ in _cleaned_paths(conn))
        except sqlite3.Error:
            return FolderCheck("refused", "This folder holds a database that can't be read.")
        finally:
            conn.close()
        return FolderCheck(
            "add", f"Existing cleanup: {files} files. The new files will be added to it."
        )
    try:
        entries = [e for e in os.listdir(output_root) if e.casefold() not in _IGNORABLE_NAMES]
    except OSError:
        return FolderCheck("refused", "This folder can't be read.")
    if entries == [paths.CLEANUP_DIR_NAME]:
        entries = [
            e
            for e in os.listdir(paths.cleanup_dir(output_root))
            if e.casefold() not in _IGNORABLE_NAMES
        ]
    if entries:
        return FolderCheck(
            "refused",
            "This folder isn't empty. Choose an empty folder or an existing cleanup output.",
        )
    return FolderCheck("new", "Empty folder: a new cleanup will be created.")


def _cleaned_paths(conn: sqlite3.Connection) -> Iterable[str]:
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
    input_root, output_root = Path(input_root), Path(output_root)
    if not input_root.is_dir():
        return FolderCheck("refused", "The Takeout folder doesn't exist.")
    if output_root.exists() and not output_root.is_dir():
        return FolderCheck("refused", "The output is a file, not a folder.")
    inp, out, lib = _norm(input_root), _norm(output_root), _norm(library_root)
    if inp == out:
        return FolderCheck("refused", "The output folder must differ from the Takeout folder.")
    if _contains(inp, out):
        return FolderCheck("refused", "The output folder is inside the Takeout folder.")
    if _contains(out, inp):
        return FolderCheck("refused", "The Takeout folder is inside the output folder.")
    if _contains(lib, inp) or _contains(lib, out):
        return FolderCheck("refused", "A folder is inside Imagory's own library folder.")
    cleaned_note = ""
    for source in db.list_sources(main_conn):
        if _norm(source.root_path) != out:
            continue
        if source.type != "cleaned":
            return FolderCheck(
                "refused",
                f"The output folder is already added as the source “{source.label}”.",
            )
        cleaned_note = (
            f" It is the library source “{source.label}”, which is rescanned once this run "
            "is added."
        )
    state = _output_state(input_root, output_root)
    if state.mode == "refused":
        return state
    return FolderCheck(state.mode, state.message + cleaned_note + _storage_note(output_root))


def input_is_source(main_conn: sqlite3.Connection, input_root: Path) -> bool:
    """Whether the input folder is already a source in the main library (the swap offer)."""
    target = _norm(input_root)
    return any(_norm(s.root_path) == target for s in db.list_sources(main_conn))


# --- inventory (stage 3, also used to check "safe to delete") ---------------------------------


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
    exclude_norm = _norm(exclude) if exclude is not None else None
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
                        exclude_norm is not None and _norm(entry.path) == exclude_norm
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

    ``takeout.match_directory`` only matches supported images, so this applies the same
    name rules (plain and ``.supplemental-metadata`` forms, truncation, moved counters,
    reused read only from :mod:`imagory.takeout`) to the other files. Sidecars in ``taken``
    (already used by an image) are not offered. A sidecar goes to at most one file, best
    pair first: readable title match, then exact before truncated names, then the
    counter-moved reading, then by name. A truncated match whose title names another file is
    rejected. A ``-edited`` copy with no sidecar of its own borrows its original's.
    """
    files = sorted(set(names), key=lambda n: (n.casefold(), n))
    jsons = list(json_names)
    taken_set = set(taken)
    index = takeout._SidecarIndex(j for j in jsons if j not in taken_set)
    titles: dict[str, str | None] = {}

    def title_of(name: str) -> str | None:
        if name not in titles:
            title = None
            if read_title is not None:
                try:
                    title = read_title(name)
                except Exception:  # a bad sidecar must never stop matching
                    log.warning("Cannot read title of %s", name, exc_info=True)
            titles[name] = title.casefold() if isinstance(title, str) else None
        return titles[name]

    def ranked(
        idx: takeout._SidecarIndex, name: str, readings: list[tuple[str, int]]
    ) -> list[tuple[tuple, str, str]]:
        wanted = {name.casefold(), *(base for base, _ in readings)}
        pairs = []
        for i, kind, rank in idx.candidates(readings):
            side = idx.sidecars[i].name
            title = title_of(side)
            if title is None:
                penalty = 1
            elif title in wanted:
                penalty = 0
            elif kind == 1:
                continue  # a truncated prefix of some other file
            else:
                penalty = 2
            pairs.append(((penalty, kind, rank, name.casefold(), side), name, side))
        return pairs

    pairs = []
    for name in files:
        pairs += ranked(index, name, takeout._interpretations(name.casefold()))
    pairs.sort()
    result: dict[str, str | None] = dict.fromkeys(files)
    claimed: set[str] = set()
    for _key, name, side in pairs:
        if result[name] is None and side not in claimed:
            claimed.add(side)
            result[name] = side

    everything = takeout._SidecarIndex(jsons)
    for name in files:
        readings = takeout._edited_interpretations(name.casefold())
        if result[name] is None and readings:
            options = ranked(everything, name, readings)
            if options:
                result[name] = min(options)[2]
    return result


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


# --- album folders -----------------------------------------------------------------------------


def album_dir_of(relative_path: str) -> str | None:
    """The input-relative album folder a file sits in, or None for a year folder, the root or
    a file directly in "Google Photos". The folder below "Google Photos" (or below the input
    root when there is none) is the album; ``Photos from YYYY`` is a year folder."""
    parts = relative_path.split("/")[:-1]
    base = next((i + 1 for i, p in enumerate(parts) if takeout.is_google_photos_name(p)), 0)
    if len(parts) <= base or _YEAR_FOLDER.match(parts[base]):
        return None
    return "/".join(parts[: base + 1])


# --- workers ----------------------------------------------------------------------------------


def hash_file(path: str) -> str:
    """BLAKE2b (16 bytes, as ``thumbs.hash_bytes``) of a file, streamed. Opens it ``rb``."""
    hasher = hashlib.blake2b(digest_size=HASH_BYTES)
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_batch(root: str, rels: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Worker entry point: ``(relative path, hash or None, error or None)`` per file."""
    out: list[tuple[str, str | None, str | None]] = []
    for rel in rels:
        try:
            out.append((rel, hash_file(os.path.join(root, *rel.split("/"))), None))
        except OSError as exc:
            out.append((rel, None, f"{type(exc).__name__}: {exc.strerror or exc}"))
    return out


def format_bytes(n: int) -> str:
    """``1.2 GB`` style sizes for progress text."""
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"  # pragma: no cover


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_text(text: str) -> str:
    """Text that encodes as UTF-8: lone surrogates (undecodable names) become escapes."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --- the pipeline -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Analysis:
    """What stage 3 sorted out, for planning and the report (input-relative paths)."""

    unreadable: tuple[str, ...]
    case_clashes: tuple[str, ...]
    trash: tuple[str, ...]
    unmatched_sidecars: tuple[str, ...]


class CleanupPipeline:
    """One cleanup run of one input into one output folder.

    Typical use, on one worker thread::

        with CleanupPipeline(input_root, output_root, library_root=lib) as p:
            preview = p.run_analysis()      # stages 1-5; None when cancelled
        ...                                 # the user checks the preview
        with CleanupPipeline(input_root, output_root, library_root=lib) as p:
            done = p.run_copy()             # stages 1, 6, 7; None when cancelled

    Or stage by stage (benchmarks): ``open()``, ``analyse_photos()``,
    ``analyse_other_files()``, ``plan()``, ``preview()``, ``copy()``, ``finalise()``,
    ``close()``. Each stage returns False when it was cancelled; calling it again (in this or
    a new pipeline) carries on from what is committed. ``progress(stage, done, total)`` is
    called from the running thread; ``total`` 0 means "unknown".
    """

    def __init__(
        self,
        input_root: Path,
        output_root: Path,
        *,
        library_root: Path | None = None,
        workers: int | None = None,
        copy_threads: int = DEFAULT_COPY_THREADS,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        tz: tzinfo | None = None,
        pool_threshold: int = DEFAULT_POOL_THRESHOLD,
    ) -> None:
        self.input = Path(os.path.abspath(input_root))
        self.output = Path(os.path.abspath(output_root))
        self.library_root = library_root
        self.workers = workers or default_workers()
        self.copy_threads = max(1, copy_threads)
        self._progress = progress or (lambda stage, done, total: None)
        self._cancel = cancel_event or threading.Event()
        self.tz = tz
        self.pool_threshold = pool_threshold
        self._conn: sqlite3.Connection | None = None
        self.run_id: int | None = None
        self._scanner: Scanner | None = None
        self._inventory: Inventory | None = None
        self._sidecars: dict[str, takeout.Sidecar | None] = {}
        self._album_names: dict[str, str] = {}
        self._renames: tuple[Rename, ...] | None = None

    # -- lifecycle -----------------------------------------------------------------------------

    def __enter__(self) -> CleanupPipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def cancel(self) -> None:
        """Ask the running stage to stop; what is finished stays committed."""
        self._cancel.set()
        if self._scanner is not None:
            self._scanner.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise CleanupError("The cleanup is not open.")
        return self._conn

    def open(self) -> int:
        """Stage 1: check the folders again, open or create ``<output>/.imagory/imagory.db``
        (normal schema, migrated) and create or resume this input's run. Returns its id."""
        if self._conn is not None:
            assert self.run_id is not None
            return self.run_id
        if not self.input.is_dir():
            raise CleanupError(f"The Takeout folder doesn't exist: {self.input}")
        inp, out = _norm(self.input), _norm(self.output)
        if _contains(inp, out) or _contains(out, inp):
            raise CleanupError("The input and output folders must not contain each other.")
        self._progress("Opening the output folder", 0, 0)
        # Everything created here is under the output folder.
        paths.cleanup_dir(self.output).mkdir(parents=True, exist_ok=True)
        conn = db.open_database(paths.cleanup_db_path(self.output))
        self._conn = conn
        try:
            unfinished = [r for r in db.list_runs(conn) if r.status != "done"]
            others = [r for r in unfinished if _norm(r.input_path) != inp]
            run = db.resumable_run(conn, str(self.input))
            if run is not None and _norm(run.input_path) != inp:
                run = None
            if run is None and others:
                raise CleanupError(
                    f"This output has an unfinished cleanup of {others[-1].input_path}."
                )
            with db.transaction(conn):
                if run is None:
                    self.run_id = db.create_run(conn, str(self.input), self.input.name)
                    log.info("Cleanup run %d of %s started", self.run_id, self.input)
                else:
                    self.run_id = run.id
                    log.info("Cleanup run %d of %s resumed (%s)", run.id, self.input, run.status)
                    if run.status == "cancelled":
                        db.set_run_status(conn, run.id, self._resume_status())
                self._ensure_sources()
        except BaseException:
            self.close()
            raise
        assert self.run_id is not None
        return self.run_id

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _resume_status(self) -> str:
        if not self.has_plan():
            return "analysing"
        counts = db.copy_status_counts(self.conn, self._run())
        return "copying" if counts["done"] or counts["error"] else "planned"

    def _run(self) -> int:
        if self.run_id is None:
            raise CleanupError("The cleanup is not open.")
        return self.run_id

    def _ensure_sources(self) -> None:
        """The output's ``cleaned`` source and this run's temporary ``takeout`` source."""
        sources = db.list_sources(self.conn)
        if not any(s.type == "cleaned" for s in sources):
            db.create_source(self.conn, str(self.output), self.output.name, "cleaned")
        if self._find_temp_source(sources) is None:
            db.create_source(
                self.conn,
                str(self.input),
                f"Cleanup input (run {self._run()})",
                "takeout",
                type_overridden=True,
            )

    def _find_temp_source(self, sources: Sequence[db.Source] | None = None) -> db.Source | None:
        inp = _norm(self.input)
        for s in sources if sources is not None else db.list_sources(self.conn):
            if s.type == "takeout" and _norm(s.root_path) == inp:
                return s
        return None

    def temp_source(self) -> db.Source:
        source = self._find_temp_source()
        if source is None:
            raise CleanupError("This run's input is no longer in the output database.")
        return source

    def cleaned_source(self) -> db.Source:
        for s in db.list_sources(self.conn):
            if s.type == "cleaned":
                return s
        raise CleanupError("The output database has no cleaned source.")

    def has_plan(self) -> bool:
        """Whether stage 4 has run (its origins, copies and status are one transaction)."""
        run_id = self._run()
        run = db.get_run(self.conn, run_id)
        return (
            (run is not None and run.status in ("planned", "copying"))
            or sum(db.copy_status_counts(self.conn, run_id).values()) > 0
            or db.count_origins(self.conn, run_id) > 0
        )

    def _stop(self) -> bool:
        """Record a cancel; always returns False (the stage's result)."""
        with db.transaction(self.conn):
            db.set_run_status(self.conn, self._run(), "cancelled")
        log.info("Cleanup run %d cancelled", self._run())
        return False

    # -- stage 2 -------------------------------------------------------------------------------

    def analyse_photos(self) -> bool:
        """Stage 2: scan the input as the temporary ``takeout`` source with the normal
        :class:`Scanner` (walk, sidecars, hash, metadata, dates, people, location), with
        thumbnails in ``<output>/.imagory/thumbs``. Trash is skipped by the scan's rule."""
        self.open()
        if self.cancelled:
            return self._stop()

        def on_progress(p: Progress) -> None:
            if p.phase == "walking":
                self._progress("Finding photos", 0, 0)
            elif p.phase == "processing":
                self._progress("Analysing photos", p.processed, p.to_process)

        self._scanner = Scanner(
            self.conn,
            self.temp_source(),
            paths.cleanup_thumbs_dir(self.output),
            self.library_root,
            workers=self.workers,
            progress=on_progress,
            cancel_event=self._cancel,
        )
        try:
            summary = self._scanner.run()
        finally:
            self._scanner = None
        if summary.status != "completed" or self.cancelled:
            return self._stop()
        self._inventory = None  # the photo rows changed
        return True

    # -- stage 3 -------------------------------------------------------------------------------

    def inventory(self) -> Inventory:
        """The input's full inventory (walk and sidecar matching, no hashing), cached."""
        if self._inventory is None or not self._inventory.complete:
            self._progress("Finding other files", 0, 0)
            self._inventory = build_inventory(self.input, self.library_root, lambda: self.cancelled)
        return self._inventory

    def _photo_rows(self, source_id: int) -> dict[str, db.CleanupFileRow]:
        return {
            r.relative_path: r
            for r in db.list_cleanup_files(self.conn, source_id)
            if r.status == "ok" and r.content_hash
        }

    def _sidecar(self, rel: str | None) -> takeout.Sidecar | None:
        if rel is None:
            return None
        if rel not in self._sidecars:
            self._sidecars[rel] = takeout.read_sidecar(self.input.joinpath(*rel.split("/")))
        return self._sidecars[rel]

    def _analysis(self, inv: Inventory, photo_paths: Iterable[str]) -> _Analysis:
        stored = set(photo_paths)
        folded = {fold_path(p) for p in stored}
        not_stored = [p for p in inv.images if p not in stored]
        return _Analysis(
            # An image whose name differs only in case from a stored one was skipped by the
            # scan (it can't exist on Windows); it is copied as-is but isn't "unreadable".
            unreadable=tuple(sorted(p for p in not_stored if fold_path(p) not in folded)),
            case_clashes=tuple(sorted(p for p in not_stored if fold_path(p) in folded)),
            trash=tuple(sorted(inv.trash)),
            unmatched_sidecars=tuple(inv.unmatched_sidecars),
        )

    def _extra_paths(self, inv: Inventory, analysis: _Analysis) -> list[str]:
        """Files stored as ``extra_files``: unreadable images, other files and unused JSON.
        Paths differing only in ASCII case can't both be stored; the first is kept."""
        seen: set[str] = set()
        result = []
        everything = [*analysis.unreadable, *analysis.case_clashes, *inv.others, *inv.unused_json]
        for rel in sorted(everything):
            folded = fold_path(rel)
            if folded in seen or not _encodable(rel):
                log.warning("Not copying %r: its name can't be stored", rel)
                continue
            seen.add(folded)
            result.append(rel)
        return result

    def _extra_record(
        self, rel: str, walked: WalkedFile, content_hash: str | None, inv: Inventory
    ) -> db.ExtraFileRecord:
        """An extra file's row: date from its sidecar, else its filename, never mtime."""
        is_json = rel.casefold().endswith(takeout.SIDECAR_SUFFIX)
        sidecar = None if is_json else self._sidecar(inv.sidecar_of.get(rel))
        taken_local = taken_utc = source = None
        if sidecar is not None and sidecar.taken_timestamp is not None:
            taken_local = dates.timestamp_to_local(sidecar.taken_timestamp, self.tz)
            taken_utc, source = sidecar.taken_utc, "takeout_json"
        elif not is_json:
            from_name = dates.parse_filename_date(rel.rpartition("/")[2])
            if from_name is not None:
                taken_local, source = from_name, "filename"
        return db.ExtraFileRecord(
            rel,
            walked.size,
            walked.modified_time,
            content_hash,
            taken_at_local=taken_local,
            taken_at_utc=taken_utc,
            date_source=source,
            latitude=sidecar.latitude if sidecar else None,
            longitude=sidecar.longitude if sidecar else None,
            description=sidecar.description if sidecar else None,
            favorite=sidecar.favorite if sidecar else False,
            uploaded_at_utc=sidecar.uploaded_utc if sidecar else None,
            people=sidecar.people if sidecar else (),
        )

    def analyse_other_files(self) -> bool:
        """Stage 3: walk the input again with every extension and store, as ``extra_files``
        of the temporary source, every file that is neither a stored image nor a used
        sidecar: other files (videos, RAW, …), images the scan couldn't read, and JSON files
        that aren't sidecars. Hashing runs in the worker pool; a file whose row already has
        a hash for the same size and mtime is not hashed again."""
        self.open()
        if self.cancelled:
            return self._stop()
        source = self.temp_source()
        inv = self.inventory()
        if not inv.complete:
            return self._stop()
        analysis = self._analysis(inv, self._photo_rows(source.id))
        wanted = self._extra_paths(inv, analysis)
        existing = {e.relative_path: e for e in db.list_extra_files(self.conn, source.id)}
        hashes: dict[str, str | None] = {}
        to_hash: list[str] = []
        for rel in wanted:
            walked, row = inv.files[rel], existing.get(rel)
            if (
                row is not None
                and row.content_hash
                and row.size == walked.size
                and row.modified_time == walked.modified_time
            ):
                hashes[rel] = row.content_hash
            else:
                to_hash.append(rel)
        wanted_set = set(wanted)
        stale = [e.id for p, e in existing.items() if p not in wanted_set]
        if stale:
            with db.transaction(self.conn):
                db.delete_extra_files(self.conn, stale)

        total = len(wanted)
        done = len(hashes)
        buffer: list[db.ExtraFileRecord] = []

        def flush() -> None:
            if buffer:
                with db.transaction(self.conn):
                    db.upsert_extra_files(self.conn, source.id, buffer)
                buffer.clear()

        for rel, h in hashes.items():
            buffer.append(self._extra_record(rel, inv.files[rel], h, inv))
            if len(buffer) >= COMMIT_BATCH:
                flush()
        self._progress("Analysing other files", done, total)
        for results in self._hash_all(to_hash):
            for rel, h, error in results:
                if error is not None:
                    log.warning("Cannot read %s: %s", rel, error)
                buffer.append(self._extra_record(rel, inv.files[rel], h, inv))
                done += 1
            if len(buffer) >= COMMIT_BATCH:
                flush()
            self._progress("Analysing other files", done, total)
        flush()
        if self.cancelled or done < total:
            return self._stop()
        return True

    def _hash_all(self, rels: list[str]) -> Iterable[list[tuple[str, str | None, str | None]]]:
        """Hash files in batches, in the worker pool unless there are only a few. Stops
        handing out work when cancelled; finished batches are still yielded."""
        root = str(self.input)
        batches = [rels[i : i + HASH_BATCH] for i in range(0, len(rels), HASH_BATCH)]
        if len(rels) < self.pool_threshold:
            for batch in batches:
                if self.cancelled:
                    return
                yield hash_batch(root, batch)
            return
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=self.workers, mp_context=context) as pool:
            pending = iter(batches)
            in_flight: set[Future] = set()
            while True:
                while not self.cancelled and len(in_flight) < self.workers * 2:
                    batch = next(pending, None)
                    if batch is None:
                        break
                    in_flight.add(pool.submit(hash_batch, root, batch))
                if not in_flight:
                    return
                finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    yield fut.result()

    # -- stage 4 -------------------------------------------------------------------------------

    def _album_name(self, album_dir: str) -> str:
        """The album's display name: ``Archive``, else the album JSON's title, else the
        folder name (PRD §8.10)."""
        if album_dir not in self._album_names:
            folder = album_dir.rpartition("/")[2]
            name = folder
            if folder.casefold() in _ARCHIVE_NAMES:
                name = ARCHIVE_ALBUM
            else:
                meta = self.input.joinpath(*album_dir.split("/"), ALBUM_METADATA_NAME)
                if meta.is_file():
                    title = takeout.read_title(meta)
                    if isinstance(title, str) and title.strip():
                        name = " ".join(title.split())
            self._album_names[album_dir] = name
        return self._album_names[album_dir]

    def _album_of(self, rel: str) -> str | None:
        album_dir = album_dir_of(rel)
        return self._album_name(album_dir) if album_dir is not None else None

    def _existing_output_paths(self, exclude: Iterable[str] = ()) -> set[str]:
        """Output-relative paths already taken: every run's copy targets (``takeout-json``
        ones included), the cleaned source's rows and the files on disk (``.imagory`` aside
        from ``takeout-json``). ``exclude`` (casefolded) is left out."""
        taken = set(db.copy_targets(self.conn))
        taken.update(_cleaned_paths(self.conn))
        for files in (self._disk_files(""), self._disk_files(cleanup.JSON_FOLDER)):
            taken.update(files)
        skip = {e.casefold() for e in exclude}
        return {t for t in taken if t.casefold() not in skip}

    def _disk_files(self, rel_root: str) -> list[str]:
        start = self.output.joinpath(*rel_root.split("/")) if rel_root else self.output
        found: list[str] = []
        if not start.is_dir():
            return found
        for dirpath, dirnames, filenames in os.walk(start):
            rel_dir = Path(dirpath).relative_to(self.output).as_posix()
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d != paths.CLEANUP_DIR_NAME]
                rel_dir = ""
            prefix = f"{rel_dir}/" if rel_dir else ""
            found += [prefix + name for name in filenames]
        return found

    def _candidates(self, inv: Inventory) -> tuple[list[Candidate], dict[int, tuple]]:
        """Candidates for the planner and, by key, ``(kind, row, sidecar path, album)``.

        Keys are unique across kinds: a ``files`` id, or a negated ``extra_files`` id. Every
        copy of a hash gets that hash's best non-mtime date (PRD §8.4 across copies).
        """
        temp, cleaned = self.temp_source(), self.cleaned_source()
        best = db.best_real_dates(self.conn, temp.id)
        cands: list[Candidate] = []
        info: dict[int, tuple] = {}

        def taken(h: str) -> str | None:
            date = best.get(h)
            return date.taken_at_local if date else None

        for row in self._photo_rows(temp.id).values():
            sidecar = self._sidecar(row.sidecar_relative_path)
            album = self._album_of(row.relative_path)
            cands.append(
                Candidate(
                    key=row.id,
                    kind="photo",
                    relative_path=row.relative_path,
                    content_hash=row.content_hash or "",
                    size=row.size,
                    title=sidecar.title if sidecar else None,
                    has_sidecar=row.sidecar_relative_path is not None,
                    taken_at_local=taken(row.content_hash or ""),
                    album_folder=album,
                )
            )
            info[row.id] = ("photo", row, row.sidecar_relative_path, album)

        # A JSON file already copied by an earlier run to the same place is not copied again.
        done_json = {
            (e.relative_path.casefold(), e.content_hash): e
            for e in db.list_extra_files(self.conn, cleaned.id)
            if e.relative_path.startswith(_JSON_PREFIX)
        }
        for e in db.list_extra_files(self.conn, temp.id):
            if not e.content_hash:
                log.warning("Not copying %s: it could not be read", e.relative_path)
                continue
            key = -e.id
            if e.extension == ".json":
                earlier = done_json.get(
                    (f"{_JSON_PREFIX}{e.relative_path}".casefold(), e.content_hash)
                )
                info[key] = ("json", e, None, None, earlier)
                if earlier is None:
                    cands.append(
                        Candidate(key, "json", e.relative_path, e.content_hash, e.size, None,
                                  False, None, None)
                    )  # fmt: skip
                continue
            side_rel = inv.sidecar_of.get(e.relative_path)
            sidecar = self._sidecar(side_rel)
            album = self._album_of(e.relative_path)
            cands.append(
                Candidate(
                    key=key,
                    kind="other",
                    relative_path=e.relative_path,
                    content_hash=e.content_hash,
                    size=e.size,
                    title=sidecar.title if sidecar else None,
                    has_sidecar=side_rel is not None,
                    taken_at_local=taken(e.content_hash),
                    album_folder=album,
                )
            )
            info[key] = ("other", e, side_rel, album)

        for row in db.list_cleanup_files(self.conn, cleaned.id):
            if row.status == "ok" and row.content_hash:
                cands.append(
                    Candidate(row.id, "photo", row.relative_path, row.content_hash, row.size,
                              None, False, row.taken_at_local, None, in_output=True)
                )  # fmt: skip
        for e in db.list_extra_files(self.conn, cleaned.id):
            if e.content_hash and not e.relative_path.startswith(_JSON_PREFIX):
                cands.append(
                    Candidate(-e.id, "other", e.relative_path, e.content_hash, e.size, None,
                              False, e.taken_at_local, None, in_output=True)
                )  # fmt: skip
        return cands, info

    def _build_plan(self, exclude: Iterable[str] = ()) -> tuple[Plan, list[Candidate], dict]:
        inv = self.inventory()
        if not inv.complete:
            raise CleanupError("cancelled")
        cands, info = self._candidates(inv)
        existing = self._existing_output_paths(exclude)
        plan = cleanup.build_plan(cands, existing, len(str(self.output)))
        return plan, cands, info

    def plan(self) -> bool:
        """Stage 4: choose the kept copy per hash, name and place every copy
        (:func:`cleanup.build_plan`), and record every input copy as an origin of its kept
        row plus the ``pending`` copies, in one transaction with the ``planned`` status. A
        run that is already planned is left as it is."""
        self.open()
        if self.has_plan():
            return True
        if self.cancelled:
            return self._stop()
        self._progress("Planning", 0, 0)
        try:
            plan, cands, info = self._build_plan()
        except CleanupError:
            if self.cancelled:
                return self._stop()
            raise
        run_id = self._run()
        origins: list[db.OriginRecord] = []
        for key, item in sorted(info.items(), key=lambda kv: kv[1][1].relative_path):
            kind, row = item[0], item[1]
            if kind == "json":
                earlier = item[4]
                target = -earlier.id if earlier is not None else key
            else:
                target = plan.kept_key_by_hash[row.content_hash]
            origins.append(
                db.OriginRecord(
                    row.relative_path,
                    file_id=target if target > 0 else None,
                    extra_file_id=-target if target < 0 else None,
                    sidecar_relative_path=item[2],
                    album_folder=item[3],
                )
            )
        copies = []
        for pc in plan.copies:
            row = info[pc.key][1]
            copies.append(
                db.CopyRecord(
                    row.relative_path,
                    pc.target_relative_path,
                    file_id=pc.key if pc.key > 0 else None,
                    extra_file_id=-pc.key if pc.key < 0 else None,
                )
            )
        media_copies = sum(1 for c in copies if not c.target_relative_path.startswith(_JSON_PREFIX))
        media_origins = sum(1 for item in info.values() if item[0] != "json")
        with db.transaction(self.conn):
            db.insert_origins(self.conn, run_id, origins)
            db.insert_copies(self.conn, run_id, copies)
            db.set_run_status(self.conn, run_id, "planned")
            db.set_run_counts(
                self.conn,
                run_id,
                {
                    "photos": sum(1 for c in copies if c.file_id is not None),
                    "other_files": media_copies - sum(1 for c in copies if c.file_id is not None),
                    "duplicates": media_origins - media_copies,
                    "trash_skipped": len(self.inventory().trash),
                },
            )
        self._renames = plan.renames
        log.info(
            "Cleanup run %d planned: %d copies, %d origins, %d renames",
            run_id,
            len(copies),
            len(origins),
            len(plan.renames),
        )
        return True

    # -- stage 5 -------------------------------------------------------------------------------

    def preview(self) -> PreviewCounts:
        """Stage 5: the counts for the wizard's preview page, from the committed plan.
        ``bytes_to_copy`` is what the remaining copies need, plus 5% for thumbnails and the
        database (the free-space rule), so the wizard can compare it with ``free_bytes``."""
        self.open()
        run_id, temp = self._run(), self.temp_source()
        jobs = db.copy_jobs(self.conn, run_id)
        photos = sum(1 for j in jobs if j.file_id is not None)
        json_files = sum(1 for j in jobs if j.target_relative_path.startswith(_JSON_PREFIX))
        duplicates = self._duplicates(run_id, temp.id)
        inv = self.inventory()
        albums = {
            o.album_folder.strip().casefold()
            for o in db.list_origins(self.conn, run_id)
            if o.album_folder and o.album_folder.strip()
        }
        pending = sum(j.size for j in jobs if j.status == "pending")
        return PreviewCounts(
            photos=photos,
            other_files=len(jobs) - photos - json_files,
            json_files=json_files,
            duplicates=len(duplicates),
            duplicate_bytes=sum(size for _, size in duplicates),
            trash_skipped=len(inv.trash),
            unmatched_sidecars=len(inv.unmatched_sidecars),
            albums=len(albums),
            bytes_to_copy=_with_margin(pending),
            free_bytes=self.free_bytes(),
        )

    def _duplicates(self, run_id: int, temp_id: int) -> list[tuple[str, int]]:
        """Media origins of this run not copied themselves (a JSON file already copied by an
        earlier run is not a duplicate)."""
        return [
            d
            for d in db.run_duplicates(self.conn, run_id, temp_id)
            if not d[0].casefold().endswith(takeout.SIDECAR_SUFFIX)
        ]

    def free_bytes(self) -> int:
        target = self.output
        while not target.exists() and target != target.parent:
            target = target.parent
        return shutil.disk_usage(target).free

    # -- stage 6 -------------------------------------------------------------------------------

    def _output_file(self, rel: str) -> Path:
        """The absolute path of an output-relative path. Refuses anything that could point
        outside the output folder (absolute paths, drives, ``..``), so every write of the
        copy stage stays under the output."""
        parts = rel.split("/")
        if (
            not rel
            or rel.startswith("/")
            or "\\" in rel
            or any(p in ("", ".", "..") for p in parts)
            or os.path.splitdrive(rel)[0]
            or os.path.isabs(rel)
        ):
            raise CleanupError(f"Unsafe output path: {rel!r}")
        dest = self.output.joinpath(*parts)
        if not _contains(_norm(self.output), _norm(dest)) or _contains(
            _norm(self.input), _norm(dest)
        ):
            raise CleanupError(f"Unsafe output path: {rel!r}")
        return dest

    def _input_file(self, rel: str) -> Path:
        return self.input.joinpath(*rel.split("/"))

    def _mtimes(self) -> Callable[[db.CopyJob], int]:
        """The modified time each copy gets: its taken time (best non-mtime date of its
        hash, local wall clock), or the input file's own mtime when it has no date."""
        best = db.best_real_dates(self.conn, self.temp_source().id)

        def mtime(job: db.CopyJob) -> int:
            date = best.get(job.content_hash or "")
            if date is None or job.target_relative_path.startswith(_JSON_PREFIX):
                return job.modified_time
            try:
                return cleanup.taken_mtime_ns(date.taken_at_local, self.tz)
            except (ValueError, OverflowError, OSError):
                return job.modified_time

        return mtime

    def copy(self) -> bool:
        """Stage 6: copy every ``pending`` copy through :func:`cleanup_copy.copy_verified` in a
        small thread pool (``.part`` file, source hash check, re-read check, rename, mtime).
        This thread is the only database writer: results are marked ``done`` or ``error`` in
        batches. A ``done`` copy whose file no longer has its size and mtime goes back to
        ``pending``, and leftover ``.part`` files in the target folders are removed first.
        Raises :class:`NotEnoughSpace` before copying if the drive is too full."""
        self.open()
        if not self.has_plan():
            raise CleanupError("The cleanup has not been planned yet.")
        if self.cancelled:
            return self._stop()
        run_id = self._run()
        mtime_of = self._mtimes()
        with db.transaction(self.conn):
            db.set_run_status(self.conn, run_id, "copying")
            for job in db.copy_jobs(self.conn, run_id, "done"):
                try:
                    dest = self._output_file(job.target_relative_path)
                except CleanupError:
                    continue
                if not cleanup_copy.already_done(dest, job.size, mtime_of(job)):
                    log.info("Copying %s again: the copy changed", job.target_relative_path)
                    db.reset_copy(self.conn, job.id)
        jobs = db.copy_jobs(self.conn, run_id)
        pending = [j for j in jobs if j.status == "pending"]
        done_bytes = sum(j.size for j in jobs if j.status == "done")
        total_bytes = done_bytes + sum(j.size for j in pending)
        done_files, total_files = len(jobs) - len(pending), len(jobs)
        if not pending:
            return True

        needed, free = _with_margin(sum(j.size for j in pending)), self.free_bytes()
        if needed > free:
            raise NotEnoughSpace(
                f"Not enough free space: the copy needs {format_bytes(needed)} and the "
                f"output drive has {format_bytes(free)}."
            )
        self._remove_stale_parts(pending)

        def report() -> None:
            text = f"Copying {format_bytes(done_bytes)} of {format_bytes(total_bytes)}"
            self._progress(text, done_files, total_files)

        report()
        buffer: list[tuple[db.CopyJob, CopyResult]] = []

        def flush() -> None:
            if not buffer:
                return
            with db.transaction(self.conn):
                for job, result in buffer:
                    if result.ok:
                        db.mark_copy_done(self.conn, job.id)
                    elif result.error != "cancelled":
                        log.warning("Copy of %s failed: %s", job.source_relative_path, result.error)
                        db.mark_copy_error(self.conn, job.id, result.error or "copy failed")
            buffer.clear()

        cancel = self._cancel.is_set
        with ThreadPoolExecutor(max_workers=self.copy_threads) as pool:
            queue = iter(pending)
            in_flight: dict[Future[CopyResult], db.CopyJob] = {}
            try:
                while True:
                    while not self.cancelled and len(in_flight) < self.copy_threads * 2:
                        job = next(queue, None)
                        if job is None:
                            break
                        in_flight[pool.submit(self._copy_one, job, mtime_of(job), cancel)] = job
                    if not in_flight:
                        break
                    finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        job = in_flight.pop(fut)
                        result = fut.result()
                        buffer.append((job, result))
                        if result.ok:
                            done_bytes += job.size
                        if result.ok or result.error != "cancelled":
                            done_files += 1
                    if len(buffer) >= COPY_COMMIT_BATCH:
                        flush()
                    report()
            finally:
                for fut in in_flight:  # only after an unexpected error
                    fut.cancel()
                flush()
        if self.cancelled or db.copy_status_counts(self.conn, run_id)["pending"]:
            return self._stop()
        return True

    def _copy_one(self, job: db.CopyJob, mtime_ns: int, cancel: Callable[[], bool]) -> CopyResult:
        """Copy one file (runs in a copy thread; no database access)."""
        try:
            dest = self._output_file(job.target_relative_path)
            if not job.content_hash:
                return CopyResult(False, None, None, "no content hash")
            return cleanup_copy.copy_verified(
                self._input_file(job.source_relative_path), dest, job.content_hash, mtime_ns,
                cancel,
            )  # fmt: skip
        except Exception as exc:  # one bad file never stops the run
            return CopyResult(False, None, None, f"{type(exc).__name__}: {exc}")

    def _remove_stale_parts(self, jobs: Iterable[db.CopyJob]) -> None:
        """Delete leftover ``*.part`` files (from a cancelled copy) in the target folders.
        Only folders under the output are visited."""
        folders: set[Path] = set()
        for job in jobs:
            try:
                folders.add(self._output_file(job.target_relative_path).parent)
            except CleanupError:
                continue
        removed = 0
        for folder in sorted(folders):
            if folder.is_dir() and _contains(_norm(self.output), _norm(folder)):
                removed += cleanup_copy.remove_stale_parts(folder)
        if removed:
            log.info("Removed %d leftover .part files", removed)

    # -- stage 7 -------------------------------------------------------------------------------

    def finalise(self, input_is_source: bool = False) -> DoneResult:
        """Stage 7: move the run's rows to the cleaned source with the copies' own size and
        mtime (:func:`db.finalise_run`, one transaction), then check "safe to delete"
        against the full inventory and write ``metadata.json`` and the report. A failed
        writer is logged and the run still finishes.

        ``input_is_source`` (from :func:`input_is_source`) is passed through to the result.
        """
        self.open()
        run_id = self._run()
        if db.copy_status_counts(self.conn, run_id)["pending"]:
            raise CleanupError("Some files are not copied yet.")
        self._progress("Finishing", 0, 0)
        temp, cleaned = self.temp_source(), self.cleaned_source()
        inv = self.inventory()
        if not inv.complete:
            raise CleanupError("cancelled")
        analysis = self._analysis(inv, self._photo_rows(temp.id))
        renames = self._renames
        if renames is None:  # a new process: replan against the output as it was
            mine = [j.target_relative_path for j in db.copy_jobs(self.conn, run_id)]
            plan = self._build_plan(exclude=mine)[0]
            targets = {t.casefold() for t in mine}
            renames = tuple(r for r in plan.renames if r.target.casefold() in targets)

        copied: dict[int, tuple[int, int]] = {}
        jobs = db.copy_jobs(self.conn, run_id)
        with db.transaction(self.conn):
            for job in jobs:
                if job.status != "done":
                    continue
                try:
                    st = os.stat(self._output_file(job.target_relative_path))
                    copied[job.id] = (st.st_size, st.st_mtime_ns)
                except (OSError, CleanupError) as exc:
                    db.mark_copy_error(self.conn, job.id, f"copy missing before finishing: {exc}")
        jobs = db.copy_jobs(self.conn, run_id)
        failed = tuple(f"{j.source_relative_path}: {j.error}" for j in jobs if j.status == "error")
        done = [j for j in jobs if j.status == "done"]
        duplicates = self._duplicates(run_id, temp.id)
        counts = {
            "photos": sum(1 for j in done if j.file_id is not None),
            "other_files": sum(
                1
                for j in done
                if j.file_id is None and not j.target_relative_path.startswith(_JSON_PREFIX)
            ),
            "duplicates": len(duplicates),
            "trash_skipped": len(inv.trash),
            "errors": len(failed),
            "bytes_copied": sum(copied[j.id][0] for j in done),
        }
        db.finalise_run(self.conn, run_id, temp.id, cleaned.id, copied, counts)
        log.info("Cleanup run %d finalised: %s", run_id, counts)

        unaccounted = self._unaccounted(inv)
        report_counts = {
            **counts,
            "json_files": sum(1 for j in done if j.target_relative_path.startswith(_JSON_PREFIX)),
            "unreadable": len(analysis.unreadable),
            "unmatched_sidecars": len(analysis.unmatched_sidecars),
            "unaccounted": len(unaccounted),
        }
        report = CleanupReport(
            counts=report_counts,
            trash_skipped=analysis.trash,
            duplicates=self._duplicate_pairs(cleaned.id),
            unmatched_sidecars=analysis.unmatched_sidecars,
            unreadable=analysis.unreadable,
            renames=renames,
            unaccounted=unaccounted,
        )
        self._write_metadata(cleaned.id)
        report_path = self._write_report(report)
        return DoneResult(
            copied=len(done),
            failed=failed,
            unaccounted=unaccounted,
            safe_to_delete=not unaccounted and not failed,
            report_path=report_path,
            output_path=self.output,
            input_is_source=input_is_source,
        )

    def _unaccounted(self, inv: Inventory) -> tuple[str, ...]:
        """Input files (Trash aside) that the output doesn't account for. After
        :func:`db.finalise_run` the run's origins are exactly the files with a verified copy
        or a duplicate of one (failed copies lost theirs), and each origin's sidecar is
        stored; everything else under the input, and anything the walk couldn't list, is
        unaccounted. Paths are compared exactly."""
        accounted: set[str] = set()
        for o in db.list_origins(self.conn, self._run()):
            accounted.add(o.original_relative_path)
            if o.sidecar_relative_path:
                accounted.add(o.sidecar_relative_path)
        missing = [rel for rel in inv.files if rel not in accounted]
        return tuple(sorted(missing) + sorted(inv.skipped))

    def _duplicate_pairs(self, cleaned_id: int) -> tuple[tuple[str, str], ...]:
        """``(kept output path, skipped input path)`` for every origin of this run that was
        not copied itself."""
        files = {r.id: r.relative_path for r in db.list_cleanup_files(self.conn, cleaned_id)}
        extras = {e.id: e.relative_path for e in db.list_extra_files(self.conn, cleaned_id)}
        copied = {j.source_relative_path for j in db.copy_jobs(self.conn, self._run())}
        pairs = []
        for o in db.list_origins(self.conn, self._run()):
            if o.original_relative_path in copied:
                continue
            kept = files.get(o.file_id) if o.file_id is not None else extras.get(o.extra_file_id)
            if kept is not None:
                pairs.append((kept, o.original_relative_path))
        return tuple(sorted(pairs))

    def _write_metadata(self, cleaned_id: int) -> None:
        """``<output>/.imagory/metadata.json``, rewritten in full (temp file, then rename)."""
        path = paths.metadata_json_path(self.output)
        runs = db.export_runs(self.conn)
        albums = db.export_albums(self.conn, cleaned_id)
        files = db.export_files(self.conn, cleaned_id)
        stamp = _utc_stamp()
        try:
            cleanup_export.write_metadata_json(path, runs, albums, files, stamp)
        except UnicodeEncodeError:
            log.warning("metadata.json: some names can't be written as UTF-8; escaping them")
            try:
                cleanup_export.write_metadata_json(
                    path,
                    [replace(r, input_path=_safe_text(r.input_path)) for r in runs],
                    [
                        replace(a, name=_safe_text(a.name), files=tuple(map(_safe_text, a.files)))
                        for a in albums
                    ],
                    [_safe_export_file(f) for f in files],
                    stamp,
                )
            except Exception:
                log.exception("Could not write %s", path)
        except Exception:
            log.exception("Could not write %s", path)

    def _write_report(self, report: CleanupReport) -> Path:
        """``<output>/.imagory/reports/cleanup-<UTC timestamp>.txt``."""
        folder = paths.cleanup_reports_dir(self.output)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"cleanup-{stamp}.txt"
        n = 1
        while path.exists():
            path = folder / f"cleanup-{stamp}-{n}.txt"
            n += 1
        try:
            folder.mkdir(parents=True, exist_ok=True)
            cleanup_export.write_report(path, report)
        except UnicodeEncodeError:
            log.warning("Cleanup report: some names can't be written as UTF-8; escaping them")
            try:
                cleanup_export.write_report(path, _safe_report(report))
            except Exception:
                log.exception("Could not write %s", path)
        except Exception:
            log.exception("Could not write %s", path)
        return path

    # -- whole runs ----------------------------------------------------------------------------

    def run_analysis(self) -> PreviewCounts | None:
        """Stages 1 to 5. Returns the preview, or None when cancelled. A run that is
        already planned goes straight to the preview."""
        self.open()
        if not self.has_plan() and not (
            self.analyse_photos() and self.analyse_other_files() and self.plan()
        ):
            return None
        return self.preview()

    def run_copy(self, input_is_source: bool = False) -> DoneResult | None:
        """Stages 1, 6 and 7. Returns the result, or None when cancelled."""
        self.open()
        if not self.copy():
            return None
        return self.finalise(input_is_source)


def _with_margin(total: int) -> int:
    return -(-total * (100 + cleanup.SPACE_MARGIN_PERCENT) // 100)


def _safe_export_file(f: object) -> object:
    from imagory.cleanup_types import ExportFile, ExportOrigin

    assert isinstance(f, ExportFile)
    return replace(
        f,
        path=_safe_text(f.path),
        description=_safe_text(f.description) if f.description else f.description,
        people=tuple(map(_safe_text, f.people)),
        origins=tuple(
            ExportOrigin(
                o.run,
                _safe_text(o.path),
                _safe_text(o.sidecar) if o.sidecar else None,
                _safe_text(o.album) if o.album else None,
            )
            for o in f.origins
        ),
    )


def _safe_report(report: CleanupReport) -> CleanupReport:
    def many(items: Sequence[str]) -> tuple[str, ...]:
        return tuple(_safe_text(i) for i in items)

    return CleanupReport(
        counts=report.counts,
        trash_skipped=many(report.trash_skipped),
        duplicates=tuple((_safe_text(a), _safe_text(b)) for a, b in report.duplicates),
        unmatched_sidecars=many(report.unmatched_sidecars),
        unreadable=many(report.unreadable),
        renames=tuple(
            Rename(_safe_text(r.original), _safe_text(r.target), r.reason) for r in report.renames
        ),
        unaccounted=many(report.unaccounted),
    )
