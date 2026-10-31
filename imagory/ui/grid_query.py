"""Loads the rest of the grid in the background once the first screen is showing."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from imagory import db

log = logging.getLogger(__name__)


class _QuerySignals(QObject):
    loaded = Signal(int, list)  # token, list[GridPhoto]


Fetch = Callable[[sqlite3.Connection], list]


class _QueryTask(QRunnable):
    """Runs ``fetch`` with its own connection. Read-only."""

    def __init__(self, signals: _QuerySignals, db_file: Path, fetch: Fetch, token: int):
        super().__init__()
        self.signals = signals
        self.db_file = db_file
        self.fetch = fetch
        self.token = token

    def run(self) -> None:
        try:
            conn = db.connect(self.db_file)
            try:
                photos = self.fetch(conn)
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

    def start(self, fetch: Fetch, token: int) -> None:
        """Run ``fetch(conn)`` on the pool thread; ``loaded`` reports its rows with ``token``.

        ``fetch`` gets a fresh connection of its own and must only read.
        """
        self._pool.clear()  # a queued query for an older reload is no longer needed
        self._pool.start(_QueryTask(self._signals, self._db_file, fetch, token))

    def cancel(self) -> None:
        """Drop a query that has not started yet."""
        self._pool.clear()

    def wait(self) -> None:
        self._pool.waitForDone()
