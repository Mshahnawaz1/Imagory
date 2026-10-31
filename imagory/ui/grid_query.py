"""Loads the rest of the grid in the background once the first screen is showing."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from imagory import db

log = logging.getLogger(__name__)


class GridQueryThread(QThread):
    """Reads grid rows after ``offset`` with its own connection. Read-only."""

    loaded = Signal(int, list)  # token, list[GridPhoto]

    def __init__(self, db_file: Path, offset: int, token: int, parent=None):
        super().__init__(parent)
        self._db_file = db_file
        self._offset = offset
        self._token = token

    def run(self) -> None:
        try:
            conn = db.connect(self._db_file)
            try:
                photos = db.grid_photos(conn, offset=self._offset)
            finally:
                conn.close()
        except Exception:
            log.exception("Could not load the photo grid")
            photos = []
        self.loaded.emit(self._token, photos)
