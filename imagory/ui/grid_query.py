"""Loads the rest of the grid in the background once the first screen is showing."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from imagory import db

log = logging.getLogger(__name__)


class _QuerySignals(QObject):
    loaded = Signal(int, list)  # token, list[GridPhoto]


class _QueryTask(QRunnable):
    """Reads grid rows after ``offset`` with its own connection. Read-only."""

    def __init__(self, signals: _QuerySignals, db_file: Path, offset: int, token: int):
        super().__init__()
        self.signals = signals
        self.db_file = db_file
        self.offset = offset
        self.token = token

    def run(self) -> None:
        try:
            conn = db.connect(self.db_file)
            try:
                photos = db.grid_photos(conn, offset=self.offset)
            finally:
                conn.close()
        except Exception:
            log.exception("Could not load the photo grid")
            photos = []
        # The query object may have been destroyed while this task ran (shutdown).
        with contextlib.suppress(RuntimeError):
            self.signals.loaded.emit(self.token, photos)


class GridQuery(QObject):
    """Runs grid queries on a pool thread and reports each result through ``loaded``.

    Pool threads are reused rather than created and deleted per query: deleting a
    finished ``QThread`` subclass crashed now and then ("pure virtual method called",
    see docs/issues.md).
    """

    loaded = Signal(int, list)  # token, list[GridPhoto]

    def __init__(self, db_file: Path, parent=None) -> None:
        super().__init__(parent)
        self._db_file = db_file
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _QuerySignals()
        self._signals.loaded.connect(self.loaded)

    def start(self, offset: int, token: int) -> None:
        self._pool.clear()  # a queued query for an older reload is no longer needed
        self._pool.start(_QueryTask(self._signals, self._db_file, offset, token))

    def wait(self) -> None:
        self._pool.waitForDone()
