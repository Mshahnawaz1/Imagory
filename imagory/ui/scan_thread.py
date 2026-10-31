"""The scan coordinator thread: the only database writer while a scan runs.

It scans the given sources one after another. A source that cannot be scanned (for
example an unplugged drive) is reported and the others still run.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from imagory import db
from imagory.scanner import Progress, Scanner, ScanSummary

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceScanResult:
    source: db.Source
    summary: ScanSummary | None  # None when the source could not be scanned
    error: str | None = None


class ScanThread(QThread):
    source_started = Signal(object, int, int)  # Source, position (1-based), total
    progressed = Signal(object)  # Progress
    succeeded = Signal(list)  # list[SourceScanResult], one per source scanned
    added = Signal(list)  # list[GridPhoto] just committed

    def __init__(
        self,
        db_file: Path,
        sources: list[db.Source],
        thumbs_root: Path,
        library_root: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_file = db_file
        self._sources = sources
        self._thumbs_root = thumbs_root
        self._library_root = library_root
        self._cancel = threading.Event()

    @property
    def sources(self) -> list[db.Source]:
        return self._sources

    def cancel(self) -> None:
        """Stop the current source after its in-flight files and skip the rest."""
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        results: list[SourceScanResult] = []
        conn = db.connect(self._db_file)  # sqlite connections belong to one thread
        try:
            for position, source in enumerate(self._sources, 1):
                if self._cancel.is_set():
                    break
                self.source_started.emit(source, position, len(self._sources))
                results.append(self._scan(conn, source))
        finally:
            conn.close()
            self.succeeded.emit(results)

    def _scan(self, conn, source: db.Source) -> SourceScanResult:
        scanner = Scanner(
            conn,
            source,
            self._thumbs_root,
            self._library_root,
            progress=self._emit_progress,
            cancel_event=self._cancel,
            on_added=self.added.emit,
        )
        try:
            return SourceScanResult(source, scanner.run())
        except FileNotFoundError:
            return SourceScanResult(source, None, f"Folder not found: {source.root_path}")
        except Exception as exc:
            log.exception("Scan of %s failed", source.root_path)
            return SourceScanResult(source, None, str(exc))

    def _emit_progress(self, progress: Progress) -> None:
        self.progressed.emit(progress)
