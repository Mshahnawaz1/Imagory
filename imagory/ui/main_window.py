"""Main window: empty state, thumbnail grid, viewer, scan controls, and Help menu."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
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

from imagory import __version__, db, paths, sources, takeout
from imagory.scanner import Progress, ScanSummary
from imagory.ui.grid import PhotoGridModel, PhotoGridView, ThumbnailLoader
from imagory.ui.grid_query import GridQuery
from imagory.ui.scan_thread import ScanThread, SourceScanResult
from imagory.ui.sources_dialog import TYPE_NAMES, AddSourceDialog, SourcesDialog, default_label
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
        f"Moved: {c.moved:,}",
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


def results_text(results: list[SourceScanResult]) -> str:
    """The scan summary of each source, headed by its label when there are several."""
    parts = []
    for result in results:
        if result.summary is None:
            body = f"Not scanned: {result.error}"
            if result.error and result.error.startswith("Folder not found"):
                body += "\nIf the drive letter changed or the folder moved, use Sources > Relocate."
        else:
            body = summary_text(result.summary)
            if result.summary.status == "cancelled":
                body += "\n(cancelled)"
        parts.append(body if len(results) == 1 else f"{result.source.label}\n{body}")
    return "\n\n".join(parts)


class EmptyPage(QWidget):
    def __init__(self, on_choose, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        title = QLabel("<h2>Welcome to Imagory</h2>", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(
            "Add a folder of photos, or an extracted Google Takeout export, to browse. "
            "Imagory only reads your photos: it never changes, moves, or deletes them, "
            "and nothing leaves this computer.",
            self,
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton("Add a folder…", self)
        self.button.setDefault(True)
        self.button.clicked.connect(on_choose)
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addSpacing(12)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)


class MainWindow(QMainWindow):
    sources_changed = Signal()
    scan_state_changed = Signal()

    def __init__(self, library_root: Path, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.library_root = library_root
        self.db_file = paths.db_path(library_root)
        self.thumbs_root = paths.thumbs_dir(library_root)
        self.conn = conn
        self.sources: list[db.Source] = []
        # Shared with the grid loader and the viewer, so it is only ever updated in place.
        self.sources_by_id: dict[int, db.Source] = {}
        self._load_sources()
        self.scan_thread: ScanThread | None = None
        self._grid_token = 0
        self.grid_query = GridQuery(self.db_file, self)
        self.grid_query.loaded.connect(self._on_rest_of_grid)
        self._anchor_id: int | None = None  # photo to keep in view across a grid reload
        self._scan_prefix = ""  # names the source being scanned when there are several
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
        self.empty = EmptyPage(self.add_source_interactive, self)

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
        self.add_action = QAction("Add source…", self)
        self.add_action.setShortcut(QKeySequence.StandardKey.Open)
        self.add_action.setStatusTip("Add a photo folder or an extracted Google Takeout export")
        self.add_action.triggered.connect(self.add_source_interactive)
        self.rescan_action = QAction("Rescan all", self)
        self.rescan_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.rescan_action.setStatusTip("Look for new, changed, and missing photos in all sources")
        self.rescan_action.triggered.connect(self.rescan)
        self.sources_action = QAction("Sources…", self)
        self.sources_action.setStatusTip("Rename, rescan, relocate, or remove sources")
        self.sources_action.triggered.connect(self.show_sources)

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.addAction(self.add_action)
        toolbar.addAction(self.rescan_action)
        toolbar.addAction(self.sources_action)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.add_action)
        file_menu.addAction(self.rescan_action)
        file_menu.addAction(self.sources_action)
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
        if not self.sources:
            self.model.set_photos([], self.sources_by_id)
            self.pages.setCurrentIndex(PAGE_EMPTY)
            self._update_actions()
            self.status_label.setText("")
            return
        photos = db.grid_photos(self.conn, limit=FIRST_PAGE)
        self.model.set_photos(photos, self.sources_by_id)
        self._restore_anchor()
        if len(photos) == FIRST_PAGE:
            self.grid_query.start(len(photos), self._grid_token)
        if self.pages.currentIndex() != PAGE_VIEWER:
            self.pages.setCurrentIndex(PAGE_GRID)
        self._update_actions()
        self._show_photo_count()

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
        if self.sources and not self.scanning:
            count = len(self.sources)
            self.status_label.setText(
                f"{self.model.rowCount():,} photos in {count} source{'s' if count != 1 else ''}"
            )

    def open_viewer(self, row: int) -> None:
        if not 0 <= row < len(self.model.photos):
            return
        self.pages.setCurrentIndex(PAGE_VIEWER)
        self.viewer.show_photo(self.model.photos, self.sources_by_id, row)

    def close_viewer(self, row: int) -> None:
        self.pages.setCurrentIndex(PAGE_GRID)
        if 0 <= row < self.model.rowCount():
            index = self.model.index(row)
            self.grid.setCurrentIndex(index)
            self.grid.scrollTo(index)
        self.grid.setFocus()

    # --- sources ------------------------------------------------------------------------

    def _load_sources(self) -> None:
        self.sources = db.list_sources(self.conn)
        self.sources_by_id.clear()
        self.sources_by_id.update((s.id, s) for s in self.sources)

    def _sources_updated(self) -> None:
        self._load_sources()
        self.sources_changed.emit()

    def source_stats(self) -> dict[int, db.SourceStats]:
        return db.source_stats(self.conn)

    def show_sources(self) -> None:
        dialog = SourcesDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _pick_folder(self, title: str, start: str | None = None) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self, title, start or str(Path.home()))
        return Path(chosen).resolve() if chosen else None

    def _check_folder(self, folder: Path, ignore_id: int | None = None) -> bool:
        """Tell the user why ``folder`` cannot be used, or ask about a warning."""
        check = sources.check_folder(folder, self.sources, self.library_root, ignore_id)
        if check.error:
            QMessageBox.warning(self, "Imagory", f"{folder}\n\n{check.error}")
            return False
        if check.warning:
            answer = QMessageBox.question(
                self,
                "Imagory",
                f"{folder}\n\n{check.warning}\n\nUse this folder anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return answer == QMessageBox.StandardButton.Yes
        return True

    def add_source_interactive(self) -> None:
        if self.scanning:
            return
        folder = self._pick_folder("Add a photo folder or Takeout export")
        if folder is None or not self._check_folder(folder):
            return
        detection = takeout.detect_source_type(folder)
        dialog = AddSourceDialog(folder, detection, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_source(folder, dialog.label, dialog.source_type, detection.type)
        dialog.deleteLater()

    def add_source(
        self,
        folder: Path,
        label: str | None = None,
        source_type: str | None = None,
        detected_type: str | None = None,
    ) -> db.Source:
        """Create a source for ``folder`` and scan it. The type is detected if not given."""
        if detected_type is None:
            detected_type = takeout.detect_source_type(folder).type
        source = sources.add_source(
            self.conn,
            self.thumbs_root,
            folder,
            label or default_label(folder),
            source_type or detected_type,
            detected_type,
        )
        self._sources_updated()
        self.reload_grid()
        self.start_scan([source])
        return source

    def rename_source_interactive(self, source: db.Source) -> None:
        label, ok = QInputDialog.getText(self, "Rename source", "Label:", text=source.label)
        if ok and label.strip():
            self.rename_source(source.id, label.strip())

    def rename_source(self, source_id: int, label: str) -> None:
        db.rename_source(self.conn, source_id, label)
        self._sources_updated()
        if self.pages.currentIndex() == PAGE_VIEWER and self.viewer.photos:
            self.viewer.rebind(self.viewer.photos, self.viewer.row)  # refresh the label

    def change_type_interactive(self, source: db.Source) -> None:
        names = [TYPE_NAMES[t] for t in db.SOURCE_TYPES]
        chosen, ok = QInputDialog.getItem(
            self,
            "Change source type",
            f"Type of “{source.label}”:",
            names,
            db.SOURCE_TYPES.index(source.type),
            False,
        )
        if ok:
            self.change_source_type(source.id, db.SOURCE_TYPES[names.index(chosen)])

    def change_source_type(self, source_id: int, source_type: str) -> None:
        # Dates do not depend on the type until Takeout dates exist (PRD §8.3, §8.4); the
        # recompute for this source belongs here then.
        db.set_source_type(self.conn, source_id, source_type)
        self._sources_updated()

    def relocate_source_interactive(self, source: db.Source) -> None:
        if self.scanning:
            return
        parent = str(Path(source.root_path).parent)
        folder = self._pick_folder(f"New location of “{source.label}”", parent)
        if folder is None or not self._check_folder(folder, ignore_id=source.id):
            return
        found, checked = sources.relocation_matches(self.conn, source.id, folder)
        if checked and not found:
            answer = QMessageBox.question(
                self,
                "Relocate source",
                f"None of the {checked} photos checked from “{source.label}” are in\n"
                f"{folder}\n\nRelocate anyway? A rescan will then mark them missing.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.relocate_source(source.id, folder)

    def relocate_source(self, source_id: int, folder: Path) -> None:
        """Point the source at ``folder`` and rescan it, so its photos are found again."""
        log.info("Relocating source %d to %s", source_id, folder)
        db.relocate_source(self.conn, source_id, str(folder))
        self._sources_updated()
        self.reload_grid()
        self.start_scan([self.sources_by_id[source_id]])

    def remove_source_interactive(self, source: db.Source) -> None:
        if self.scanning:
            return
        stats = self.source_stats().get(source.id, db.SourceStats())
        answer = QMessageBox.question(
            self,
            "Remove source",
            f"Remove “{source.label}” from Imagory?\n\n{source.root_path}\n\n"
            f"Imagory will forget its {stats.photos + stats.missing:,} photos and delete their "
            "thumbnails. Your photos on disk are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.remove_source(source.id)

    def remove_source(self, source_id: int) -> None:
        """Delete the source's rows and thumbnails. Only after the user confirms."""
        assert not self.scanning
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.close_viewer(-1)  # the photo showing may belong to this source
        self.model.set_photos([], self.sources_by_id)
        self.loader.wait()  # no thumbnail is written into the folder being removed
        sources.remove_source(self.conn, self.thumbs_root, source_id)
        self._sources_updated()
        self.reload_grid()

    def remove_missing_items(self, source_id: int) -> db.RemovedMissing:
        """Forget the source's missing files and photos left without files. Only after the
        user confirms (in the sources dialog)."""
        assert not self.scanning
        removed = sources.remove_missing(self.conn, self.thumbs_root, source_id)
        self._sources_updated()
        self.reload_grid()
        return removed

    # --- scanning -----------------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self.scan_thread is not None

    def _update_actions(self) -> None:
        idle = not self.scanning
        self.add_action.setEnabled(idle)
        self.rescan_action.setEnabled(bool(self.sources) and idle)
        self.sources_action.setEnabled(bool(self.sources))
        self.empty.button.setEnabled(idle)

    def rescan(self) -> None:
        self.rescan_sources(self.sources)

    def rescan_sources(self, to_scan: list[db.Source]) -> None:
        if to_scan and not self.scanning:
            self.start_scan(to_scan)

    def start_scan(self, to_scan: list[db.Source]) -> None:
        assert to_scan and self.scan_thread is None
        thread = ScanThread(self.db_file, to_scan, self.thumbs_root, self.library_root, self)
        thread.source_started.connect(self._on_source_started)
        thread.progressed.connect(self._on_progress)
        thread.added.connect(self._on_scan_added)
        thread.succeeded.connect(self._on_scan_succeeded)
        thread.finished.connect(self._on_scan_thread_finished)
        self.scan_thread = thread
        self._scan_prefix = ""
        self._update_actions()
        self.scan_state_changed.emit()
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

    def _on_source_started(self, source: db.Source, position: int, total: int) -> None:
        self._scan_prefix = f"{source.label} ({position} of {total}): " if total > 1 else ""

    def _on_progress(self, progress: Progress) -> None:
        if self.scan_thread is None or self.scan_thread.cancel_requested:
            return
        if progress.phase == "walking":
            self.progress.setRange(0, 0)
            self.status_label.setText(f"{self._scan_prefix}Looking for photos…")
        elif progress.phase == "processing":
            self.progress.setRange(0, max(1, progress.to_process))
            self.progress.setValue(progress.processed)
            self.status_label.setText(
                f"{self._scan_prefix}Processing {progress.processed:,} of "
                f"{progress.to_process:,} new or changed files ({progress.found:,} found)"
            )

    def _on_scan_succeeded(self, results: list[SourceScanResult]) -> None:
        if self._closing:
            return
        self._sources_updated()  # last scan times
        self.reload_grid()
        if not results:  # cancelled before the first source started
            QMessageBox.information(self, "Scan cancelled", "Nothing was scanned.")
            return
        text = results_text(results)
        if any(r.summary is None for r in results):
            QMessageBox.warning(
                self,
                "Scan finished with problems",
                text + "\n\nDetails are in the log (Help > Open log folder).",
            )
            return
        cancelled = self.scan_thread is not None and self.scan_thread.cancel_requested
        cancelled = cancelled or any(r.summary.status == "cancelled" for r in results)
        title = "Scan cancelled" if cancelled else "Scan finished"
        note = "\n\nRescan to pick up where it stopped." if cancelled else ""
        QMessageBox.information(self, title, text + note)

    def _on_scan_thread_finished(self) -> None:
        if self.scan_thread is not None:
            self.scan_thread.deleteLater()
        self.scan_thread = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self._update_actions()
        self.scan_state_changed.emit()
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
        self.grid_query.wait()
        self.loader.reset({})
        self.loader.wait()
        super().closeEvent(event)
