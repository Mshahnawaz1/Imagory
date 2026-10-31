"""Scan pipeline: walk a source, diff it against the database, process, apply, record.

The coordinator (:class:`Scanner`) is the only database writer during a scan. Worker
processes read files and write thumbnails, but never touch the database. Cancel and resume
need no saved state: every finished file is committed, and a rescan skips it.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from imagory import db
from imagory.thumbs import FileJob, FileResult, fold_path, process_batch

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif"})
SKIPPED_DIR_NAMES = frozenset({"system volume information"})
COMMIT_BATCH = 200
WORKER_BATCH = 16


# --- walk -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkedFile:
    relative_path: str
    size: int
    modified_time: int


@dataclass
class WalkResult:
    files: list[WalkedFile] = field(default_factory=list)
    skipped_by_ext: Counter[str] = field(default_factory=Counter)
    complete: bool = True


def is_skipped_dir(name: str) -> bool:
    return name.startswith((".", "$")) or name.casefold() in SKIPPED_DIR_NAMES


def extension_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return ext or "(none)"


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def walk_source(
    root: Path | str,
    exclude: Path | str | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> WalkResult:
    """List supported image files under ``root`` without opening any of them.

    Symlinks and junctions are never followed, hidden and system folders are skipped,
    and ``exclude`` (the library folder) is skipped if it lies inside the source.
    """
    result = WalkResult()
    exclude_str = str(exclude) if exclude is not None else None
    stack: list[tuple[str, str]] = [(str(root), "")]
    while stack:
        if cancelled():
            result.complete = False
            return result
        dir_path, rel_dir = stack.pop()
        try:
            with os.scandir(dir_path) as entries:
                children = list(entries)
        except OSError as exc:
            log.warning("Cannot read folder %s: %s", dir_path, exc)
            continue
        subdirs: list[tuple[str, str]] = []
        for entry in children:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if is_skipped_dir(entry.name):
                        continue
                    if exclude_str is not None and _same_path(entry.path, exclude_str):
                        continue
                    subdirs.append((entry.path, rel))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = extension_of(entry.name)
                if ext not in SUPPORTED_EXTENSIONS:
                    result.skipped_by_ext[ext] += 1
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                log.warning("Cannot stat %s: %s", entry.path, exc)
                continue
            result.files.append(WalkedFile(rel, st.st_size, st.st_mtime_ns))
        # Reverse so folders are visited in on-disk order when popped from the stack.
        stack.extend(reversed(subdirs))
    return result


# --- diff -------------------------------------------------------------------------------


@dataclass
class Diff:
    new: list[WalkedFile] = field(default_factory=list)
    changed: list[tuple[int, WalkedFile]] = field(default_factory=list)
    unchanged: int = 0
    reappeared: list[int] = field(default_factory=list)  # unchanged files back from missing
    case_renamed: list[tuple[int, str]] = field(default_factory=list)
    disappeared: list[int] = field(default_factory=list)  # ok files no longer on disk


def diff_files(walked: list[WalkedFile], existing: list[db.FileState]) -> Diff:
    """Compare a walk with the stored file rows. Paths match case-insensitively (NOCASE).

    A row without a content hash (from V0) counts as changed, so it is hashed once.
    """
    by_path = {fold_path(f.relative_path): f for f in existing}
    seen: set[int] = set()
    new_paths: set[str] = set()
    result = Diff()
    for item in walked:
        folded = fold_path(item.relative_path)
        row = by_path.get(folded)
        if row is None:
            # Paths differing only in case cannot coexist on Windows; keep the first one.
            if folded in new_paths:
                log.warning("Skipping %s: another file differs only in case", item.relative_path)
                continue
            new_paths.add(folded)
            result.new.append(item)
            continue
        seen.add(row.id)
        if row.needs_hash or row.size != item.size or row.modified_time != item.modified_time:
            result.changed.append((row.id, item))
            continue
        result.unchanged += 1
        if row.status != "ok":
            result.reappeared.append(row.id)
        if row.relative_path != item.relative_path:
            result.case_renamed.append((row.id, item.relative_path))
    result.disappeared = [f.id for f in existing if f.id not in seen and f.status == "ok"]
    return result


# --- coordinator ------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    phase: str  # 'walking' | 'processing' | 'done'
    found: int = 0
    processed: int = 0
    to_process: int = 0


@dataclass
class ScanSummary:
    status: str  # 'completed' | 'cancelled' | 'failed'
    counts: db.ScanCounts
    run_id: int


ProgressCallback = Callable[[Progress], None]
AddedCallback = Callable[[list[db.GridPhoto]], None]


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


class Scanner:
    """Runs one scan of one source. Construct and call :meth:`run` on the scan thread."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        source: db.Source,
        thumbs_root: Path,  # the library's thumbnail cache, shared by all sources
        library_root: Path | None = None,
        *,
        workers: int | None = None,
        commit_batch: int = COMMIT_BATCH,
        worker_batch: int = WORKER_BATCH,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        on_added: AddedCallback | None = None,
    ) -> None:
        self.conn = conn
        self.source = source
        self.thumbs_root = thumbs_root
        self.library_root = library_root
        self.workers = workers or default_workers()
        self.commit_batch = commit_batch
        self.worker_batch = worker_batch
        self._progress = progress or (lambda p: None)
        self._cancel = cancel_event or threading.Event()
        self._on_added = on_added

    def cancel(self) -> None:
        """Stop queueing new work; in-flight results are still committed."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> ScanSummary:
        started = db.utc_now()
        run_id = db.start_scan_run(self.conn, self.source.id, started)
        counts = db.ScanCounts()
        status = "failed"
        try:
            status = self._run(counts, started)
        except Exception:
            log.exception("Scan of %s failed", self.source.root_path)
            raise
        finally:
            db.finish_scan_run(self.conn, run_id, self.source.id, status, counts)
            log.info(
                "Scan %s of %s: found=%d added=%d updated=%d moved=%d unchanged=%d "
                "missing=%d errors=%d skipped=%s",
                status,
                self.source.root_path,
                counts.found,
                counts.added,
                counts.updated,
                counts.moved,
                counts.unchanged,
                counts.missing,
                counts.errors,
                counts.skipped_by_ext,
            )
            self._progress(Progress("done", counts.found))
        return ScanSummary(status, counts, run_id)

    def _run(self, counts: db.ScanCounts, started: str) -> str:
        root = Path(self.source.root_path)
        if not root.is_dir():
            raise FileNotFoundError(f"Source folder not found: {root}")
        self._progress(Progress("walking"))
        walk = walk_source(root, self.library_root, lambda: self.cancelled)
        counts.found = len(walk.files)
        counts.skipped_by_ext = dict(sorted(walk.skipped_by_ext.items()))
        if not walk.complete:
            return "cancelled"

        diff = diff_files(walk.files, db.load_file_states(self.conn, self.source.id))
        counts.unchanged = diff.unchanged
        counts.missing = len(diff.disappeared)
        # The walk is complete, so missing files are known before any processing; recording
        # them now keeps the result correct even if processing is cancelled.
        with db.transaction(self.conn):
            db.mark_missing(self.conn, diff.disappeared)
            db.mark_ok(self.conn, diff.reappeared)
            db.update_relative_paths(self.conn, diff.case_renamed)
            db.touch_seen(self.conn, self.source.id, started)

        jobs: list[tuple[int | None, WalkedFile]] = [(None, f) for f in diff.new]
        jobs.extend(diff.changed)
        if not jobs:
            return "cancelled" if self.cancelled else "completed"
        self._process(jobs, counts, found=counts.found, disappeared=set(diff.disappeared))
        return "cancelled" if self.cancelled else "completed"

    def _process(
        self,
        jobs: list[tuple[int | None, WalkedFile]],
        counts: db.ScanCounts,
        found: int,
        disappeared: set[int],
    ) -> None:
        file_ids = {item.relative_path: fid for fid, item in jobs}
        total = len(jobs)
        processed = 0
        pending: list[FileResult] = []
        self._progress(Progress("processing", found, 0, total))

        def flush() -> None:
            if not pending:
                return
            now = db.utc_now()
            good = [r for r in pending if not r.error]
            # A copy of a photo the grid already shows must not be reported to it again.
            visible = db.visible_photo_ids(self.conn, (r.content_hash for r in good))
            outcomes: list[tuple[str, int]] = []
            with db.transaction(self.conn):
                for res in good:
                    outcomes.append(self._apply(res, file_ids[res.relative_path], now))
            for res in pending:
                if res.error:
                    counts.errors += 1
                    log.warning("Unreadable image %s: %s", res.relative_path, res.error)
            for outcome, fid in outcomes:
                if outcome == "added":
                    counts.added += 1
                elif outcome == "moved":
                    counts.moved += 1
                    if fid in disappeared:
                        counts.missing -= 1  # counted as missing by this scan's diff
                else:
                    counts.updated += 1
            pending.clear()
            shown = [fid for outcome, fid in outcomes if outcome != "updated"]
            if self._on_added is not None and shown:
                # Committed, so the grid can show these while the scan carries on.
                rows = db.grid_photos_for_files(self.conn, shown)
                rows = [p for p in rows if p.photo_id not in visible]
                if rows:
                    self._on_added(rows)

        batches = _chunks(
            [FileJob(f.relative_path, f.size, f.modified_time) for _, f in jobs], self.worker_batch
        )
        max_in_flight = self.workers * 2
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=self.workers, mp_context=context) as pool:
            in_flight: set[Future[list[FileResult]]] = set()
            root = str(self.source.root_path)
            thumbs_root = str(self.thumbs_root)
            source_type = self.source.type
            exhausted = False
            try:
                while True:
                    while not exhausted and not self.cancelled and len(in_flight) < max_in_flight:
                        batch = next(batches, None)
                        if batch is None:
                            exhausted = True
                            break
                        in_flight.add(
                            pool.submit(process_batch, root, thumbs_root, batch, source_type)
                        )
                    if not in_flight:
                        break
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        results = fut.result()
                        pending.extend(results)
                        processed += len(results)
                    if len(pending) >= self.commit_batch:
                        flush()
                    self._progress(Progress("processing", found, processed, total))
            finally:
                # Whatever finished is kept, even if the scan stops on an unexpected error.
                flush()

    def _apply(self, res: FileResult, file_id: int | None, now: str) -> tuple[str, int]:
        """Store one processed file: ('added' | 'updated' | 'moved', file id).

        A new file whose hash matches a missing file of this source is a move: the old
        row is re-pointed, keeping its id and first-seen date (PRD §8.2).
        """
        record = db.FileRecord(
            res.relative_path,
            res.size,
            res.modified_time,
            res.width,
            res.height,
            res.format,
            res.taken_at_local,
            res.date_source,
            content_hash=res.content_hash,
            taken_at_utc=res.taken_at_utc,
            exif_taken_at=res.exif_taken_at,
            exif_offset=res.exif_offset,
        )
        if file_id is not None:
            db.update_file(self.conn, file_id, record, now)
            return "updated", file_id
        moved = db.find_moved_file(self.conn, self.source.id, res.content_hash)
        if moved is not None:
            db.update_file(self.conn, moved, record, now)
            return "moved", moved
        return "added", db.insert_file(self.conn, self.source.id, record, now)


def _chunks(items: list[FileJob], size: int) -> Iterator[list[FileJob]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
