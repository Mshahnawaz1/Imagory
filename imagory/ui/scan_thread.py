"""The scan coordinator thread: the only database writer while a scan runs."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from imagory import db
from imagory.scanner import Progress, Scanner, ScanSummary

log = logging.getLogger(__name__)


class ScanThread(QThread):
    progressed = Signal(object)  # Progress
    succeeded = Signal(object)  # ScanSummary
    failed = Signal(str)
    added = Signal(list)  # list[GridPhoto] just committed

    def __init__(
        self, db_file: Path, source: db.Source, thumbs_root: Path, library_root: Path, parent=None
    ) -> None:
        super().__init__(parent)
        self._db_file = db_file
        self._source = source
        self._thumbs_root = thumbs_root
        self._library_root = library_root
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        conn = db.connect(self._db_file)  # sqlite connections belong to one thread
        try:
            scanner = Scanner(
                conn,
                self._source,
                self._thumbs_root,
                self._library_root,
                progress=self._emit_progress,
                cancel_event=self._cancel,
                on_added=self.added.emit,
            )
            summary: ScanSummary = scanner.run()
        except Exception as exc:
            log.exception("Scan failed")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(summary)
        finally:
            conn.close()

    def _emit_progress(self, progress: Progress) -> None:
        self.progressed.emit(progress)
