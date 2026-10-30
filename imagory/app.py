"""Application startup: library folder, logging, database, and the main window."""

from __future__ import annotations

import logging
import sys

from imagory import __version__, db, paths
from imagory.logs import setup_logging

log = logging.getLogger(__name__)


def _log_unhandled(exc_type, exc, tb) -> None:
    log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from imagory.ui.main_window import MainWindow

    library_root = paths.ensure_library_dir()
    setup_logging(paths.logs_dir(library_root))
    log.info("Imagory %s starting, library %s", __version__, library_root)
    sys.excepthook = _log_unhandled

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Imagory")
    app.setApplicationVersion(__version__)

    try:
        conn = db.open_database(paths.db_path(library_root))
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
