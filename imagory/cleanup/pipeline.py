"""Takeout cleanup pipeline (PRD §8.10, docs/takeout-cleanup-plan.md, stages 1 to 7).

Ties together the folder checks (:mod:`.checks`), the inventory (:mod:`.inventory`), the
planner (:mod:`.planning`), the copy primitive (:mod:`.copying`), the export writers
(:mod:`.export`) and the cleanup SQL in :mod:`imagory.db.cleanup`. No Qt: a ``CleanupThread``
runs it on a worker thread and turns the ``progress`` callback into signals.

Safety (PRD principle 1):

* The **input** folder is only listed, ``stat``-ed and opened ``"rb"`` (by the Scanner's
  workers, the hash workers, sidecar reads and :func:`.copying.copy_verified`). Nothing
  here writes, renames, moves or deletes anything in it.
* Everything written goes under the **output** folder: copies through
  :meth:`CleanupPipeline._output_file` (which refuses any path that could leave the output),
  and the database, thumbnails, ``metadata.json``, reports and ``takeout-json/`` under
  ``<output>/.imagory``. :meth:`CleanupPipeline.open` refuses an output inside the input (and
  the other way round) before anything is created.
* The main library database is only read, by :func:`.checks.check_folders` and
  :func:`.checks.input_is_source`.

Cancel and resume need no saved state beyond the output database (as in PRD §6.3): every
stage picks up from what is committed there. One output database has one writer, the thread
running the pipeline; all stage methods must be called from the thread that called
:meth:`CleanupPipeline.open`.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from imagory import dates, db, paths, takeout
from imagory.cleanup import copying, export, planning
from imagory.cleanup.checks import (
    check_folders_for,
    cleaned_paths,
    norm_path,
    path_contains,
    unfinished_run,
)
from imagory.cleanup.hashing import hash_batch
from imagory.cleanup.inventory import Inventory, album_dir_of, build_inventory
from imagory.cleanup.types import (
    Candidate,
    CleanupError,
    CleanupReport,
    CopyResult,
    DoneResult,
    NotEnoughSpace,
    Plan,
    PreviewCounts,
    Rename,
)
from imagory.scanner import Progress, Scanner, WalkedFile, default_workers
from imagory.thumbs import fold_path

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]  # (stage text, done, total)

COMMIT_BATCH = 200  # stage 3 rows per transaction
COPY_COMMIT_BATCH = 50  # copy results per transaction
HASH_BATCH = 16  # files per hash-worker task
DEFAULT_COPY_THREADS = 4  # copying is I/O bound
DEFAULT_POOL_THRESHOLD = 64  # fewer files than this are hashed without a process pool

ARCHIVE_ALBUM = "Archive"
_ARCHIVE_NAMES = frozenset({"archive"})
ALBUM_METADATA_NAME = "metadata.json"
_JSON_PREFIX = planning.JSON_FOLDER + "/"


# --- helpers ----------------------------------------------------------------------------------


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

    ``main_sources`` is a snapshot of the main library's sources (``db.list_sources``, taken
    on the main thread) so :meth:`open` can refuse an output that is a ``plain`` or
    ``takeout`` source; ``library_root`` defaults to ``paths.library_dir()``.

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
        main_sources: Sequence[db.Source] = (),
    ) -> None:
        self.input = Path(os.path.abspath(input_root))
        self.output = Path(os.path.abspath(output_root))
        self.library_root = Path(library_root) if library_root else paths.library_dir()
        self.main_sources = tuple(main_sources)
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
        # The safety boundary: every folder rule is enforced again here, before anything is
        # created (nesting, library folder, main-library sources, a non-empty folder that is not
        # a cleanup output, an unfinished run of another input).
        check = check_folders_for(self.main_sources, self.library_root, self.input, self.output)
        if check.mode == "refused":
            raise CleanupError(check.message)
        self._progress("Opening the output folder", 0, 0)
        # Everything created here is under the output folder.
        paths.cleanup_dir(self.output).mkdir(parents=True, exist_ok=True)
        conn = db.open_database(paths.cleanup_db_path(self.output))
        self._conn = conn
        try:
            runs = db.list_runs(conn)
            run = unfinished_run(runs, self.input)
            others = [r for r in runs if r.status != "done" and r is not run]
            if others:  # checked above too; the database is the final word
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
        inp = norm_path(self.input)
        for s in sources if sources is not None else db.list_sources(self.conn):
            if s.type == "takeout" and norm_path(s.root_path) == inp:
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
        taken.update(cleaned_paths(self.conn))
        for files in (self._disk_files(""), self._disk_files(planning.JSON_FOLDER)):
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

    def _build_plan(self) -> tuple[Plan, list[Candidate], dict]:
        inv = self.inventory()
        if not inv.complete:
            raise CleanupError("cancelled")
        cands, info = self._candidates(inv)
        existing = self._existing_output_paths()
        plan = planning.build_plan(cands, existing, len(str(self.output)))
        return plan, cands, info

    def plan(self) -> bool:
        """Stage 4: choose the kept copy per hash, name and place every copy
        (:func:`planning.build_plan`), and record every input copy as an origin of its kept
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
            db.set_setting(self.conn, _renames_key(run_id), _renames_json(plan.renames))
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
        if not path_contains(norm_path(self.output), norm_path(dest)) or path_contains(
            norm_path(self.input), norm_path(dest)
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
                return planning.taken_mtime_ns(date.taken_at_local, self.tz)
            except (ValueError, OverflowError, OSError):
                return job.modified_time

        return mtime

    def copy(self) -> bool:
        """Stage 6: copy every ``pending`` copy through :func:`copying.copy_verified` in a
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
                if not copying.already_done(dest, job.size, mtime_of(job)):
                    log.info("Copying %s again: the copy changed", job.target_relative_path)
                    db.reset_copy(self.conn, job.id)
            # A failure may have been transient (a drive unplugged): try every error again.
            for job in db.copy_jobs(self.conn, run_id, "error"):
                log.info("Retrying %s (%s)", job.source_relative_path, job.error)
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
            return copying.copy_verified(
                self._input_file(job.source_relative_path), dest, job.content_hash, mtime_ns,
                cancel,
            )  # fmt: skip
        except Exception as exc:  # one bad file never stops the run
            return CopyResult(False, None, None, f"{type(exc).__name__}: {exc}")

    def _remove_stale_parts(self, jobs: Iterable[db.CopyJob]) -> None:
        """Delete ``<target>.part`` (left by a cancelled copy) for each of this run's pending
        targets, and nothing else: any other ``*.part`` file, even in the same folder, may be
        a real copied file. Each path comes from :meth:`_output_file`, so it is under the
        output folder."""
        removed = 0
        for job in jobs:
            try:
                dest = self._output_file(job.target_relative_path)
            except CleanupError:
                continue
            part = dest.with_name(dest.name + copying.PART_SUFFIX)
            try:
                if part.is_symlink() or not part.is_file():
                    continue
                part.unlink()  # under the output: <output>/<target>.part
                removed += 1
            except OSError as exc:
                log.warning("Cannot remove %s: %s", part, exc)
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
        renames = _renames_from(db.get_setting(self.conn, _renames_key(run_id)))
        # Extra files that could not be read have no hash, so they were not copied (item 11).
        unhashed = tuple(
            e.relative_path for e in db.list_extra_files(self.conn, temp.id) if not e.content_hash
        )

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
        counts = {
            "photos": sum(1 for j in done if j.file_id is not None),
            "other_files": sum(
                1
                for j in done
                if j.file_id is None and not j.target_relative_path.startswith(_JSON_PREFIX)
            ),
            "trash_skipped": len(inv.trash),
            "errors": len(failed),
            "bytes_copied": sum(copied[j.id][0] for j in done),
        }
        db.finalise_run(self.conn, run_id, temp.id, cleaned.id, copied, counts)
        # The duplicates are counted from the same rows as the report's list, after finalise
        # dropped the origins of failed copies.
        pairs = self._duplicate_pairs(cleaned.id)
        with db.transaction(self.conn):
            db.set_run_counts(self.conn, run_id, {"duplicates": len(pairs)})
        counts["duplicates"] = len(pairs)
        log.info("Cleanup run %d finalised: %s", run_id, counts)

        unaccounted = self._unaccounted(inv)
        unreadable = tuple(sorted({*analysis.unreadable, *unhashed}))
        report_counts = {
            **counts,
            "json_files": sum(1 for j in done if j.target_relative_path.startswith(_JSON_PREFIX)),
            "unreadable": len(unreadable),
            "unmatched_sidecars": len(analysis.unmatched_sidecars),
            "unaccounted": len(unaccounted),
        }
        report = CleanupReport(
            counts=report_counts,
            trash_skipped=analysis.trash,
            duplicates=pairs,
            unmatched_sidecars=analysis.unmatched_sidecars,
            unreadable=unreadable,
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
            if o.original_relative_path in copied or o.original_relative_path.casefold().endswith(
                takeout.SIDECAR_SUFFIX
            ):
                continue  # copied itself, or a JSON file an earlier run already copied
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
            export.write_metadata_json(path, runs, albums, files, stamp)
        except UnicodeEncodeError:
            log.warning("metadata.json: some names can't be written as UTF-8; escaping them")
            try:
                export.write_metadata_json(
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
            export.write_report(path, report)
        except UnicodeEncodeError:
            log.warning("Cleanup report: some names can't be written as UTF-8; escaping them")
            try:
                export.write_report(path, _safe_report(report))
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


def _renames_key(run_id: int) -> str:
    return f"cleanup_renames:{run_id}"


def _renames_json(renames: Iterable[Rename]) -> str:
    return json.dumps([[r.original, r.target, r.reason] for r in renames], ensure_ascii=False)


def _renames_from(text: str | None) -> tuple[Rename, ...]:
    if not text:
        return ()
    try:
        return tuple(Rename(o, t, reason) for o, t, reason in json.loads(text))
    except (ValueError, TypeError):
        log.warning("Stored renames can't be read; the report lists none")
        return ()


def _with_margin(total: int) -> int:
    return -(-total * (100 + planning.SPACE_MARGIN_PERCENT) // 100)


def _safe_export_file(f: object) -> object:
    from imagory.cleanup.types import ExportFile, ExportOrigin

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
