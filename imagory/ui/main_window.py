"""Main window: empty state, thumbnail grid, viewer, scan controls, and Help menu."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from imagory import __version__, db, paths, thumbs
from imagory.scanner import Progress, ScanSummary
from imagory.ui.grid import PhotoGridModel, PhotoGridView, ThumbnailLoader
from imagory.ui.grid_query import GridQueryThread
from imagory.ui.scan_thread import ScanThread
from imagory.ui.viewer import PhotoViewer

log = logging.getLogger(__name__)

PAGE_EMPTY, PAGE_GRID, PAGE_VIEWER = range(3)
# Rows loaded synchronously at startup: more than a 4K screen of thumbnails holds.
FIRST_PAGE = 500
CELL_PROBE = 4  # pixels into the grid, to find the top-left cell


def summary_text(summary: ScanSummary) -> str:
    c = summary.counts
    lines = [
        f"Found: {c.found:,}",
        f"Added: {c.added:,}",
        f"Updated: {c.updated:,}",
        f"Unchanged: {c.unchanged:,}",
        f"Missing: {c.missing:,}",
        f"Errors (unreadable files): {c.errors:,}",
    ]
    skipped = c.skipped_by_ext or {}
    if skipped:
        total = sum(skipped.values())
        detail = ", ".join(f"{ext} {n:,}" for ext, n in sorted(skipped.items()))
        lines.append(f"Skipped (not supported): {total:,} ({detail})")
    else:
        lines.append("Skipped (not supported): 0")
    return "\n".join(lines)


class EmptyPage(QWidget):
    def __init__(self, on_choose, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        title = QLabel("<h2>Welcome to Imagory</h2>", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(
            "Choose a folder of photos to browse. Imagory only reads your photos: "
            "it never changes, moves, or deletes them, and nothing leaves this computer.",
            self,
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton("Choose folder…", self)
        self.button.setDefault(True)
        self.button.clicked.connect(on_choose)
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addSpacing(12)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)


class MainWindow(QMainWindow):
    def __init__(self, library_root: Path, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.library_root = library_root
        self.db_file = paths.db_path(library_root)
        self.thumbs_root = paths.thumbs_dir(library_root)
        self.conn = conn
        self.source: db.Source | None = db.get_source(conn)
        self.scan_thread: ScanThread | None = None
        self._grid_token = 0
        self._grid_threads: set[GridQueryThread] = set()
        self._anchor_id: int | None = None  # photo to keep in view across a grid reload
        self._closing = False

        self.setWindowTitle("Imagory")
        self.resize(1200, 800)

        self.loader = ThumbnailLoader(self.thumbs_root, self.devicePixelRatioF(), self)
        self.model = PhotoGridModel(self.loader, self)
        self.grid = PhotoGridView(self)
        self.grid.setModel(self.model)
        self.model.rowsInserted.connect(self._on_rows_inserted)
        self.grid.open_requested.connect(self.open_viewer)
        self.viewer = PhotoViewer(self)
        self.viewer.closed.connect(self.close_viewer)
        self.empty = EmptyPage(self.choose_folder, self)

        self.pages = QStackedWidget(self)
        self.pages.addWidget(self.empty)
        self.pages.addWidget(self.grid)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        self._build_actions()
        self._build_status_bar()
        self.reload_grid()

    # --- construction -------------------------------------------------------------------

    def _build_actions(self) -> None:
        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.rescan_action.setStatusTip("Look for new, changed, and missing photos")
        self.rescan_action.triggered.connect(self.rescan)
        self.change_action = QAction("Change folder…", self)
        self.change_action.setStatusTip("Browse a different folder instead")
        self.change_action.triggered.connect(self.change_folder)

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.addAction(self.rescan_action)
        toolbar.addAction(self.change_action)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.rescan_action)
        file_menu.addAction(self.change_action)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("E&xit")
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Open log folder", self.open_log_folder)
        help_menu.addAction("About Imagory", self.show_about)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self.status_label = QLabel(self)
        self.progress = QProgressBar(self)
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        bar.addWidget(self.status_label, 1)
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.cancel_button)

    # --- grid ---------------------------------------------------------------------------

    def reload_grid(self) -> None:
        """Show the library straight from the database. Never starts a scan.

        The first screen is queried directly; the rest loads on a background thread.
        """
        self._grid_token += 1
        self._anchor_id = self._current_anchor()
        if self.source is None:
            self.model.set_photos([], None)
            self.pages.setCurrentIndex(PAGE_EMPTY)
            self._update_actions()
            self.status_label.setText("")
            return
        photos = db.grid_photos(self.conn, self.source.id, limit=FIRST_PAGE)
        self.model.set_photos(photos, Path(self.source.root_path))
        self._restore_anchor()
        if len(photos) == FIRST_PAGE:
            self._load_rest_of_grid(len(photos))
        if self.pages.currentIndex() != PAGE_VIEWER:
            self.pages.setCurrentIndex(PAGE_GRID)
        self.setWindowTitle(f"Imagory - {self.source.root_path}")
        self._update_actions()
        self._show_photo_count()

    def _load_rest_of_grid(self, offset: int) -> None:
        assert self.source is not None
        thread = GridQueryThread(self.db_file, self.source.id, offset, self._grid_token, self)
        thread.loaded.connect(self._on_rest_of_grid)
        thread.finished.connect(lambda: self._grid_thread_done(thread))
        self._grid_threads.add(thread)
        thread.start()

    def _grid_thread_done(self, thread: GridQueryThread) -> None:
        self._grid_threads.discard(thread)
        thread.deleteLater()

    def _on_rest_of_grid(self, token: int, photos: list) -> None:
        if token != self._grid_token or self._closing:
            return  # the grid was reloaded in the meantime
        self.model.append_photos(photos)
        self._restore_anchor()
        self._anchor_id = None  # the whole grid is loaded: a photo not found is gone
        self._show_photo_count()

    def _current_anchor(self) -> int | None:
        """The open photo, or the photo at the top left of the grid."""
        if self.pages.currentIndex() == PAGE_VIEWER:
            return self.viewer.current_photo_id()
        if self.pages.currentIndex() != PAGE_GRID or self.model.rowCount() == 0:
            return None
        index = self.grid.indexAt(QPoint(CELL_PROBE, CELL_PROBE))
        if not index.isValid() or index.row() == 0:
            return None  # at the top already: stay there
        return self.model.photos[index.row()].photo_id

    def _restore_anchor(self) -> None:
        if self._anchor_id is None:
            return
        row = self.model.row_of(self._anchor_id)
        if row is None:
            return  # not loaded yet (or gone)
        self._anchor_id = None
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.viewer.rebind(self.model.photos, row)
        else:
            self.grid.scrollTo(self.model.index(row), PhotoGridView.ScrollHint.PositionAtTop)

    def _on_rows_inserted(self, parent: QModelIndex, first: int, last: int) -> None:
        if self.pages.currentIndex() == PAGE_VIEWER and self.viewer.photos is self.model.photos:
            self.viewer.rows_inserted(first, last - first + 1)

    def _on_scan_added(self, photos: list) -> None:
        if not self._closing:
            self.model.insert_photos(photos)

    def _show_photo_count(self) -> None:
        if self.source is not None and not self.scanning:
            self.status_label.setText(
                f"{self.model.rowCount():,} photos in {self.source.root_path}"
            )

    def open_viewer(self, row: int) -> None:
        if self.source is None or not 0 <= row < len(self.model.photos):
            return
        self.pages.setCurrentIndex(PAGE_VIEWER)
        self.viewer.show_photo(self.model.photos, Path(self.source.root_path), row)

    def close_viewer(self, row: int) -> None:
        self.pages.setCurrentIndex(PAGE_GRID)
        if 0 <= row < self.model.rowCount():
            index = self.model.index(row)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self.grid.setFocus()

    # --- folder and scanning ------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self.scan_thread is not None

    def _update_actions(self) -> None:
        self.rescan_action.setEnabled(self.source is not None and not self.scanning)
        self.change_action.setEnabled(self.source is not None and not self.scanning)
        self.empty.button.setEnabled(not self.scanning)

    def _pick_folder(self) -> Path | None:
        start = self.source.root_path if self.source else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose a photo folder", start)
        return Path(chosen).resolve() if chosen else None

    def choose_folder(self) -> None:
        if self.scanning or self.source is not None:
            return
        folder = self._pick_folder()
        if folder is not None:
            self.set_folder(folder)

    def set_folder(self, folder: Path) -> None:
        """Create the source for ``folder`` and scan it (first launch)."""
        self.source = db.create_source(self.conn, str(folder), folder.name or str(folder))
        log.info("Source created for %s", folder)
        self.reload_grid()
        self.start_scan()

    def change_folder(self) -> None:
        if self.scanning or self.source is None:
            return
        folder = self._pick_folder()
        if folder is None:
            return
        if Path(self.source.root_path) == folder:
            self.rescan()
            return
        answer = QMessageBox.question(
            self,
            "Change folder",
            f"Switch Imagory to:\n{folder}\n\n"
            f"Imagory will forget everything it knows about\n{self.source.root_path}\n"
            "and delete its thumbnails. Your photos on disk are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.replace_folder(folder)

    def replace_folder(self, folder: Path) -> None:
        """Delete the current source's rows and thumbnails, then scan ``folder``."""
        assert self.source is not None
        log.info("Changing folder from %s to %s", self.source.root_path, folder)
        self.model.set_photos([], None)
        self.loader.wait()
        db.delete_source(self.conn, self.source.id)
        thumbs.clear_thumbs(self.thumbs_root)
        self.source = None
        self.set_folder(folder)

    def rescan(self) -> None:
        if self.source is not None and not self.scanning:
            self.start_scan()

    def start_scan(self) -> None:
        assert self.source is not None and self.scan_thread is None
        thread = ScanThread(self.db_file, self.source, self.thumbs_root, self.library_root, self)
        thread.progressed.connect(self._on_progress)
        thread.added.connect(self._on_scan_added)
        thread.succeeded.connect(self._on_scan_succeeded)
        thread.failed.connect(self._on_scan_failed)
        thread.finished.connect(self._on_scan_thread_finished)
        self.scan_thread = thread
        self._update_actions()
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        self.status_label.setText("Looking for photos…")
        thread.start()

    def cancel_scan(self) -> None:
        if self.scan_thread is not None:
            self.scan_thread.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Stopping after the current files…")

    def _on_progress(self, progress: Progress) -> None:
        if self.scan_thread is None or self.scan_thread.cancel_requested:
            return
        if progress.phase == "walking":
            self.progress.setRange(0, 0)
            self.status_label.setText("Looking for photos…")
        elif progress.phase == "processing":
            self.progress.setRange(0, max(1, progress.to_process))
            self.progress.setValue(progress.processed)
            self.status_label.setText(
                f"Processing {progress.processed:,} of {progress.to_process:,} new or changed "
                f"files ({progress.found:,} found)"
            )

    def _on_scan_succeeded(self, summary: ScanSummary) -> None:
        if self._closing:
            return
        self.reload_grid()
        title = "Scan cancelled" if summary.status == "cancelled" else "Scan finished"
        note = "\n\nRescan to pick up where it stopped." if summary.status == "cancelled" else ""
        QMessageBox.information(self, title, summary_text(summary) + note)

    def _on_scan_failed(self, message: str) -> None:
        if self._closing:
            return
        self.reload_grid()
        QMessageBox.warning(
            self,
            "Scan failed",
            f"The scan stopped because of an error:\n{message}\n\n"
            "Details are in the log (Help > Open log folder).",
        )

    def _on_scan_thread_finished(self) -> None:
        if self.scan_thread is not None:
            self.scan_thread.deleteLater()
        self.scan_thread = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self._update_actions()
        self._show_photo_count()

    # --- help ---------------------------------------------------------------------------

    def open_log_folder(self) -> None:
        folder = paths.logs_dir(self.library_root)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Imagory",
            f"<h3>Imagory {__version__}</h3>"
            "<p>A free, offline photo browser. Imagory only reads your photos and never "
            "changes, moves, or deletes them. It makes no network connections.</p>"
            "<p>Released under the MIT licence. Bundled components keep their own licences "
            "(see THIRD_PARTY_LICENSES.txt).</p>"
            f"<p>Library folder:<br>{self.library_root}</p>",
        )

    # --- shutdown -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        if self.scan_thread is not None:
            self.status_label.setText("Stopping scan…")
            self.scan_thread.cancel()
            self.scan_thread.wait()  # in-flight results are committed before it exits
        for thread in list(self._grid_threads):
            thread.wait()
        self.loader.reset(None)
        self.loader.wait()
        super().closeEvent(event)
