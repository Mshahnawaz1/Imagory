"""Application startup: library folder, logging, database, and the main window."""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from imagory import __version__, db, paths, thumbs
from imagory.logs import setup_logging

log = logging.getLogger(__name__)


def _log_unhandled(exc_type, exc, tb) -> None:
    log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)


def upgrade_thumbnail_cache(conn: sqlite3.Connection, thumbs_root: Path) -> None:
    """Clear a cache written with another layout; thumbnails regenerate on demand."""
    if db.get_setting(conn, "thumbs_layout") != thumbs.LAYOUT_VERSION:
        log.info("Clearing the thumbnail cache for layout %s", thumbs.LAYOUT_VERSION)
        thumbs.clear_thumbs(thumbs_root)
        db.set_setting(conn, "thumbs_layout", thumbs.LAYOUT_VERSION)


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from imagory.ui.icons import app_icon
    from imagory.ui.main_window import MainWindow
    from imagory.ui.theme import apply_theme

    library_root = paths.ensure_library_dir()
    setup_logging(paths.logs_dir(library_root))
    log.info("Imagory %s starting, library %s", __version__, library_root)
    sys.excepthook = _log_unhandled

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Imagory")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    apply_theme(app)
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: apply_theme(app))

    try:
        conn = db.open_database(paths.db_path(library_root))
        upgrade_thumbnail_cache(conn, paths.thumbs_dir(library_root))
    except Exception as exc:
        log.exception("Could not open the library database")
        QMessageBox.critical(None, "Imagory", f"Could not open the library database:\n{exc}")
        return 1

    window = MainWindow(library_root, conn)
    window.show()
    try:
        return app.exec()
    finally:
        conn.close()
        log.info("Imagory exiting")
