"""Main window: empty state, thumbnail grid, viewer, scan controls, and Help menu."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QKeySequence,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from imagory import __version__, cleanup_run, db, paths, sources
from imagory.cleanup_types import DoneResult
from imagory.scanner import Progress, ScanSummary
from imagory.ui.albums import AddToAlbumDialog, AlbumsDialog
from imagory.ui.cleanup_thread import CleanupThread
from imagory.ui.cleanup_wizard import CleanupWizard
from imagory.ui.filter_bar import FilterBar
from imagory.ui.grid import PhotoDataSource, PhotoThumbnails, view_queries
from imagory.ui.icons import app_icon
from imagory.ui.info_panel import show_in_explorer
from imagory.ui.photo_grid import PhotoGrid
from imagory.ui.scan_thread import ScanThread, SourceScanResult
from imagory.ui.sidebar import Sidebar
from imagory.ui.sources_dialog import TYPE_NAMES, AddSourceDialog, SourcesDialog, default_label
from imagory.ui.theme import (
    DARK_MODE,
    LIGHT_MODE,
    SYSTEM,
    VIEWER_BACKGROUND,
    VIEWER_TEXT,
    apply_theme,
    load_mode,
    save_mode,
)
from imagory.ui.viewer import PhotoViewer

log = logging.getLogger(__name__)

PAGE_EMPTY, PAGE_GRID, PAGE_VIEWER = range(3)
SIDEBAR_SETTING = "sidebar_visible"
THUMBNAIL_LEVELS = ("small", "medium", "large")


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
        logo = QLabel(self)
        logo.setPixmap(app_icon().pixmap(72, 72))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("<h2>Welcome to Imagory</h2>", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(
            "Add a folder of photos, or an extracted Google Takeout export, to browse.",
            self,
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton("Add a photo folder…", self)
        self.button.setDefault(True)
        self.button.clicked.connect(on_choose)
        promise = QLabel(
            "Imagory only reads your photos. It never changes, moves, or deletes them, "
            "and nothing leaves this computer.",
            self,
        )
        promise.setWordWrap(True)
        promise.setAlignment(Qt.AlignmentFlag.AlignCenter)
        promise.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addSpacing(12)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(24)
        layout.addWidget(promise)
        layout.addStretch(2)


@dataclass(frozen=True)
class CleanedAdd:
    """A cleanup output being added to the library: scanned first, then its albums are
    imported and the swap offered."""

    source_id: int
    output: Path
    run_id: int | None  # None: import every finished run (plain Add source)
    input: Path | None  # the cleanup's input, for the swap offer


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
        self.cleanup_thread: CleanupThread | None = None
        self.cleanup_wizard: CleanupWizard | None = None
        # Extra keyword arguments for the cleanup pipelines (workers, pool_threshold); tests set it.
        self.cleanup_options: dict = {}
        self._cleanup_input: Path | None = None
        self._cleanup_output: Path | None = None
        self._cleanup_run_id: int | None = None
        self._pending_add: CleanedAdd | None = None
        self._queued_cleanup: tuple | None = None  # a job asked for while the last one ends
        self._swap_context: CleanedAdd | None = None
        self._anchor_id: int | None = None  # photo to keep in view across a grid reload
        self._scan_prefix = ""  # names the source being scanned when there are several
        self._closing = False
        self.album: db.Album | None = None  # the album being shown, if any

        self.setWindowTitle("Imagory")
        self.resize(1200, 800)

        # The grid reads the database through these two; it never touches it itself.
        self.grid_source = PhotoDataSource(conn, self.db_file, parent=self)
        self.grid_source.completed.connect(self._on_rest_of_grid)
        self.thumbnails = PhotoThumbnails(
            self.thumbs_root,
            self.sources_by_id,
            device_pixel_ratio=self.devicePixelRatioF(),
            parent=self,
        )
        self.grid = PhotoGrid(self)
        self.grid.set_provider(self.thumbnails)
        self.grid.set_data_source(self.grid_source)
        self.grid.activated.connect(self.open_viewer)
        self.grid.selection_changed.connect(self._show_photo_count)
        self.grid.selection_changed.connect(self._update_album_actions)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_grid_menu)
        self.viewer = PhotoViewer(self)
        self.viewer.set_connection(self.conn)
        viewer_palette = self.viewer.palette()  # photos stand out on a dark ground
        viewer_palette.setColor(QPalette.ColorRole.Window, QColor(VIEWER_BACKGROUND))
        viewer_palette.setColor(QPalette.ColorRole.WindowText, QColor(VIEWER_TEXT))
        viewer_palette.setColor(QPalette.ColorRole.Text, QColor(VIEWER_TEXT))
        self.viewer.setPalette(viewer_palette)
        self.viewer.closed.connect(self.close_viewer)
        self.empty = EmptyPage(self.add_source_interactive, self)

        self.pages = QStackedWidget(self)
        self.pages.addWidget(self.empty)
        self.pages.addWidget(self.grid)
        self.pages.addWidget(self.viewer)

        self.filter_bar = FilterBar(
            self,
            sources=lambda: [(s.id, s.label) for s in self.sources],
            folders=lambda source_id: db.folder_paths(self.conn, source_id),
            people=self._people_rows,
        )
        self.filter_bar.filterChanged.connect(self._on_filter_changed)
        self.album_bar = self._build_album_bar()
        self.sidebar = Sidebar(self)
        self.sidebar.allPhotosRequested.connect(self._sidebar_all_photos)
        self.sidebar.duplicatesRequested.connect(self._sidebar_duplicates)
        self.sidebar.albumRequested.connect(self._sidebar_album)
        self.sidebar.personRequested.connect(self._sidebar_person)
        self.sidebar.sourceRequested.connect(self._sidebar_source)
        self.sidebar.manageAlbumsRequested.connect(self.show_albums)
        self.sidebar.manageSourcesRequested.connect(self.show_sources)
        self.sources_changed.connect(self.refresh_sidebar)
        central = QWidget(self)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.sidebar)
        content = QWidget(central)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.filter_bar)
        column.addWidget(self.album_bar)
        column.addWidget(self.pages, 1)
        outer.addWidget(content, 1)
        self.setCentralWidget(central)

        self._build_actions()
        self._build_status_bar()
        self.refresh_sidebar()
        self.sidebar_action.setChecked(db.get_setting(self.conn, SIDEBAR_SETTING, "1") == "1")
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
        self.cleanup_action = QAction("Clean up Takeout…", self)
        self.cleanup_action.setStatusTip(
            "Copy a Google Takeout export into a tidy folder (the original is never changed)"
        )
        self.cleanup_action.triggered.connect(self.open_cleanup_wizard)
        self.sources_action = QAction("Sources…", self)
        self.sources_action.setStatusTip("Rename, rescan, relocate, or remove sources")
        self.sources_action.triggered.connect(self.show_sources)
        self.add_to_album_action = QAction("Add to album…", self)
        self.add_to_album_action.setStatusTip("Add the selected photos to an album")
        self.add_to_album_action.triggered.connect(self.add_selection_to_album)
        self.remove_from_album_action = QAction("Remove from album", self)
        self.remove_from_album_action.setStatusTip(
            "Take the selected photos out of this album (the photos stay)"
        )
        self.remove_from_album_action.triggered.connect(self.remove_selection_from_album)
        self.manage_albums_action = QAction("Manage albums…", self)
        self.manage_albums_action.setStatusTip("Create, rename, or delete albums")
        self.manage_albums_action.triggered.connect(self.show_albums)
        self.all_photos_action = QAction("All photos", self)
        self.all_photos_action.triggered.connect(lambda: self.show_album(None))

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.addAction(self.add_action)
        toolbar.addAction(self.cleanup_action)
        toolbar.addAction(self.rescan_action)
        toolbar.addAction(self.sources_action)
        toolbar.addSeparator()
        toolbar.addAction(self.add_to_album_action)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.add_action)
        file_menu.addAction(self.cleanup_action)
        file_menu.addAction(self.rescan_action)
        file_menu.addAction(self.sources_action)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("E&xit")
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        self.sidebar_action = QAction("Show sidebar", self)
        self.sidebar_action.setCheckable(True)
        self.sidebar_action.setShortcut(QKeySequence("Ctrl+B"))
        self.sidebar_action.setStatusTip("Show or hide the sidebar (Ctrl+B)")
        self.sidebar_action.toggled.connect(self.set_sidebar_visible)
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.sidebar_action)
        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("&Theme")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        saved_mode = load_mode(self.conn)
        for mode, label in ((SYSTEM, "&System"), (LIGHT_MODE, "&Light"), (DARK_MODE, "&Dark")):
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == saved_mode)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_theme(m))
            self.theme_group.addAction(action)
            self.theme_actions[mode] = action

        self.albums_menu = self.menuBar().addMenu("&Albums")
        self.albums_menu.aboutToShow.connect(self._fill_albums_menu)
        self._fill_albums_menu()

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Open log folder", self.open_log_folder)
        help_menu.addAction("About Imagory", self.show_about)

    def set_theme(self, mode: str) -> None:
        """Apply a theme mode to the whole app now and remember it."""
        save_mode(self.conn, mode)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, mode)

    def _build_album_bar(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        self.album_label = QLabel(bar)
        back = QPushButton("Show all photos", bar)
        back.clicked.connect(lambda: self.show_album(None))
        row.addWidget(self.album_label, 1)
        row.addWidget(back)
        bar.setVisible(False)
        return bar

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
        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setRange(0, len(THUMBNAIL_LEVELS) - 1)
        self.size_slider.setFixedWidth(90)
        self.size_slider.setToolTip("Thumbnail size")
        self.size_slider.setValue(THUMBNAIL_LEVELS.index(self.grid.thumbnail_size))
        self.size_slider.valueChanged.connect(
            lambda i: self.grid.set_thumbnail_size(THUMBNAIL_LEVELS[i])
        )
        bar.addPermanentWidget(self.size_slider)
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.cancel_button)

    # --- grid ---------------------------------------------------------------------------

    @property
    def photos(self) -> list[db.GridPhoto]:
        """The grid's loaded photos, in grid order."""
        return self.grid_source.photos

    def current_filter(self) -> db.PhotoFilter:
        """What the grid shows: the filter bar's filter, within the album if one is open."""
        return replace(self.filter_bar.filter(), album_id=self.album.id if self.album else None)

    def _apply_view(self) -> None:
        """Point the grid at the current filter and album, and show it from the top."""
        rows, counts, accepts = view_queries(self.current_filter())
        self.grid_source.rows_query = rows
        self.grid_source.counts_query = counts
        self.grid_source.accepts = accepts
        self.reload_grid(keep_position=False)

    def _on_filter_changed(self, _f: db.PhotoFilter) -> None:
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.close_viewer(-1)
        self._apply_view()

    def reload_grid(self, keep_position: bool = True) -> None:
        """Show the library straight from the database. Never starts a scan.

        The first screen is queried directly; the rest loads on a background thread.
        The open photo, or the photo at the top left of the grid, stays in view.
        """
        self._anchor_id = self._current_anchor() if keep_position else None
        scroll_y = self.grid.scroll_y() if keep_position else 0
        if not self.sources:
            self.grid_source.clear()
            self.grid.set_data_source(self.grid_source)
            self.pages.setCurrentIndex(PAGE_EMPTY)
            self._update_bars()
            self._update_actions()
            self.status_label.setText("")
            return
        self.grid_source.load()
        self.grid.set_data_source(self.grid_source)
        if not self._restore_anchor():
            # Until the anchor's row arrives, stay where the grid was rather than jump up.
            self.grid.verticalScrollBar().setValue(scroll_y)
        if self.grid_source.complete:
            self._anchor_id = None  # everything is loaded: a photo not found is gone
        self._update_empty_text()
        if self.pages.currentIndex() != PAGE_VIEWER:
            self.pages.setCurrentIndex(PAGE_GRID)
        self._update_bars()
        self._update_actions()
        self._show_photo_count()

    def _update_bars(self) -> None:
        in_grid = bool(self.sources) and self.pages.currentIndex() != PAGE_VIEWER
        self.filter_bar.setVisible(in_grid)
        self.sidebar.setVisible(self.sidebar_action.isChecked() and bool(self.sources))
        self._sync_sidebar_selection()
        self.album_bar.setVisible(in_grid and self.album is not None)
        if self.album is not None:
            self.album_label.setText(f"Album: <b>{html_escape(self.album.name)}</b>")

    def _on_rest_of_grid(self, positions: list[int]) -> None:
        if self._closing:
            return
        self._rebind_viewer(positions)
        self.grid.items_inserted(positions)  # the counts may have changed too
        self._restore_anchor()
        self._anchor_id = None  # the whole grid is loaded: a photo not found is gone
        self._show_photo_count()

    def _current_anchor(self) -> int | None:
        """The open photo, or the photo at the top left of the grid."""
        if self.pages.currentIndex() == PAGE_VIEWER:
            return self.viewer.current_photo_id()
        if self.pages.currentIndex() != PAGE_GRID or self.grid.scroll_y() == 0:
            return None  # at the top already: stay there
        first, end = self.grid.visible_range()
        photo = self.grid_source.item(first) if first < end else None
        return photo.photo_id if photo is not None else None

    def _restore_anchor(self) -> bool:
        """Bring the anchor photo back into view; False while it is not loaded yet."""
        if self._anchor_id is None:
            return True
        index = self.grid_source.index_of(self._anchor_id)
        if index is None:
            return False  # not loaded yet (or gone)
        self._anchor_id = None
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.viewer.rebind(self.photos, index)
            self.grid.set_current_index(index)
        else:
            self.grid.scroll_to(index, at_top=True)
        return True

    def _rebind_viewer(self, positions: list[int] | None = None) -> None:
        """Keep the viewer on its photo after photos were inserted at ``positions``
        (ascending indexes after the insertion) or appended."""
        if self.pages.currentIndex() != PAGE_VIEWER or self.viewer.photos is not self.photos:
            return
        row = self.viewer.row
        if row < 0:
            return
        for position in positions or ():
            if position <= row:
                row += 1
        self.viewer.rebind(self.photos, row)  # also refreshes "n of total"

    def _on_scan_added(self, photos: list) -> None:
        if self._closing:
            return
        positions = self.grid_source.insert(photos)
        self.grid.items_inserted(positions)
        if positions:
            self._rebind_viewer(positions)

    def _update_empty_text(self) -> None:
        if self.scanning:
            text = "Looking for photos…"
        elif self.album is not None:
            text = "This album has no photos to show. Select photos and use Add to album."
        elif not self.current_filter().is_empty:
            text = "No photos match the filter."
        else:
            text = "No photos to show. Add photos to your folders, then use Rescan all."
        self.grid.set_empty_text(text)

    def _show_photo_count(self) -> None:
        if self.sources and not self.scanning:
            count = len(self.sources)
            photos = self.grid_source.count()
            if self.album is not None:
                text = f"Album “{self.album.name}”: {photos:,} photos"
                if not self.filter_bar.filter().is_empty:
                    text += " match the filter"
            elif not self.current_filter().is_empty:
                text = f"{photos:,} photos match the filter"
            else:
                text = f"{photos:,} photos in {count} source{'s' if count != 1 else ''}"
            selected = self.grid.selected_count()
            if selected:
                text += f"  ·  {selected:,} selected"
            self.status_label.setText(text)

    # --- albums -------------------------------------------------------------------------

    def _fill_albums_menu(self) -> None:
        menu = self.albums_menu
        menu.clear()
        menu.addAction(self.manage_albums_action)
        menu.addAction(self.add_to_album_action)
        menu.addAction(self.remove_from_album_action)
        menu.addSeparator()
        menu.addAction(self.all_photos_action)
        for album in db.list_albums(self.conn):
            action = menu.addAction(f"{album.name}  ({album.photo_count:,})")
            action.setCheckable(True)
            action.setChecked(self.album is not None and self.album.id == album.id)
            action.triggered.connect(lambda _checked=False, a=album: self.show_album(a))

    def set_sidebar_visible(self, visible: bool) -> None:
        self.sidebar.setVisible(visible and bool(self.sources))
        value = "1" if visible else "0"
        if not self.busy and db.get_setting(self.conn, SIDEBAR_SETTING) != value:
            db.set_setting(self.conn, SIDEBAR_SETTING, value)
            self.conn.commit()

    def _people_rows(self) -> list[tuple[int, str, int]]:
        return [(p.id, p.name, p.photo_count) for p in db.list_people(self.conn)]

    def refresh_sidebar(self) -> None:
        people = self._people_rows()
        self.sidebar.set_people(people)
        self.sidebar.set_people_enabled(not self.busy)
        self.filter_bar.set_people(people)
        self.sidebar.set_albums((a.id, a.name, a.photo_count) for a in db.list_albums(self.conn))
        self.sidebar.set_sources((s.id, s.label) for s in self.sources)
        self._sync_sidebar_selection()

    def _sync_sidebar_selection(self) -> None:
        f = self.filter_bar.filter()
        if self.album is not None:
            self.sidebar.set_current("album", self.album.id)
        elif f.person_id is not None:
            self.sidebar.set_current("person", f.person_id)
        elif f.has_duplicates:
            self.sidebar.set_current("duplicates")
        elif len(f.source_ids) == 1:
            self.sidebar.set_current("source", next(iter(f.source_ids)))
        else:
            self.sidebar.set_current("all")

    def _sidebar_all_photos(self) -> None:
        self.filter_bar.clear()
        self.show_album(None)

    def _sidebar_duplicates(self) -> None:
        self.album = None
        self.filter_bar.set_filter(db.PhotoFilter(has_duplicates=True))
        self.show_album(None)

    def _sidebar_album(self, album_id: int) -> None:
        album = next((a for a in db.list_albums(self.conn) if a.id == album_id), None)
        self.show_album(album)

    def _sidebar_person(self, person_id: int) -> None:
        """Show one person's photos. Like an album, it combines with the other filters."""
        if self.busy:
            return
        self.filter_bar.set_filter(replace(self.filter_bar.filter(), person_id=person_id))
        self._sync_sidebar_selection()

    def _sidebar_source(self, source_id: int) -> None:
        self.album = None
        self.filter_bar.set_filter(db.PhotoFilter(source_ids=frozenset({source_id})))
        self.show_album(None)

    def show_album(self, album: db.Album | None) -> None:
        """Show one album in the grid, or all photos with ``None``. The filters still apply."""
        self.album = album
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.close_viewer(-1)
        self._apply_view()

    def show_albums(self) -> None:
        if self.busy:
            return
        dialog = AlbumsDialog(self.conn, self)
        dialog.exec()
        dialog.deleteLater()
        if self.album is not None:  # it may have been renamed or deleted
            current = next((a for a in db.list_albums(self.conn) if a.id == self.album.id), None)
            if current is None:
                self.show_album(None)
            elif current != self.album:
                self.album = current
                self._update_bars()
                self._show_photo_count()
        self.refresh_sidebar()

    def selected_photo_ids(self) -> list[int]:
        ids = (self.grid_source.item(i) for i in self.grid.selected_indexes())
        return [p.photo_id for p in ids if p is not None]

    def add_selection_to_album(self) -> None:
        ids = self.selected_photo_ids()
        if self.busy or not ids:
            return
        dialog = AddToAlbumDialog(self.conn, ids, self)
        dialog.exec()
        dialog.deleteLater()
        self.refresh_sidebar()

    def remove_selection_from_album(self) -> None:
        ids = self.selected_photo_ids()
        if self.busy or self.album is None or not ids:
            return
        db.remove_photos_from_album(self.conn, self.album.id, ids)
        self.album = next((a for a in db.list_albums(self.conn) if a.id == self.album.id), None)
        self.refresh_sidebar()
        self.reload_grid()

    def _update_album_actions(self) -> None:
        idle = not self.busy
        has_selection = idle and self.grid.selected_count() > 0
        self.add_to_album_action.setEnabled(has_selection)
        self.remove_from_album_action.setEnabled(has_selection and self.album is not None)
        self.manage_albums_action.setEnabled(idle)

    def build_grid_menu(self, index: int | None = None) -> QMenu:
        """The right-click menu of the grid for the current selection."""
        menu = QMenu(self)
        if index is not None:
            menu.addAction("Open in viewer").triggered.connect(lambda: self.open_viewer(index))
            menu.addSeparator()
        menu.addAction(self.add_to_album_action)
        if self.album is not None:
            menu.addAction(self.remove_from_album_action)
        return menu

    def _show_grid_menu(self, pos) -> None:
        index = self.grid.index_at(pos)
        if index is None:
            return
        if not self.grid.is_selected(index):
            self.grid.click(index, Qt.KeyboardModifier.NoModifier)
        menu = self.build_grid_menu(index)
        menu.exec(self.grid.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def open_viewer(self, index: int) -> None:
        if not 0 <= index < len(self.photos):
            return  # not loaded yet
        self.pages.setCurrentIndex(PAGE_VIEWER)
        self._update_bars()
        self.viewer.show_photo(self.photos, self.sources_by_id, index)

    def close_viewer(self, index: int) -> None:
        self.pages.setCurrentIndex(PAGE_GRID)
        self._update_bars()
        self.grid.set_current_index(index)
        self.grid.setFocus()

    # --- sources ------------------------------------------------------------------------

    def _load_sources(self) -> None:
        self.sources = db.list_sources(self.conn)
        self.sources_by_id.clear()
        self.sources_by_id.update((s.id, s) for s in self.sources)

    def _sources_updated(self) -> None:
        self._load_sources()
        self.filter_bar.refresh_options()  # may change the filter, which reloads the grid
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
        if self.busy:
            return
        folder = self._pick_folder("Add a photo folder or Takeout export")
        if folder is None or not self._check_folder(folder):
            return
        detection = sources.detect_source_type(folder)
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
    ) -> db.Source | None:
        """Create a source for ``folder`` and scan it. The type is detected if not given.

        A cleanup output (``cleaned``) goes through :meth:`add_cleaned_output`, so its albums
        are imported once the scan completes; None is returned if it can't be added."""
        if detected_type is None:
            detected_type = sources.detect_source_type(folder).type
        if (source_type or detected_type) == "cleaned":
            return self.add_cleaned_output(folder, None, None, label)
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
        if not ok:
            return
        source_type = db.SOURCE_TYPES[names.index(chosen)]
        if source_type == source.type and source.type_overridden:
            return
        try:
            changed = self.change_source_type(source.id, source_type)
        except FileNotFoundError:
            QMessageBox.warning(
                self,
                "Change source type",
                f"The folder of “{source.label}” is not available:\n{source.root_path}\n\n"
                "Imagory needs it to read the Takeout information. Connect the drive, or "
                "relocate the source, and try again. Nothing was changed.",
            )
            return
        QMessageBox.information(
            self,
            "Change source type",
            f"“{source.label}” is now {TYPE_NAMES[source_type]}. "
            f"Dates changed for {changed:,} file{'' if changed == 1 else 's'}.",
        )

    def change_source_type(self, source_id: int, source_type: str) -> int:
        """Set the type and recompute the source's dates (PRD §8.1). Returns how many files'
        dates changed. Raises ``FileNotFoundError`` when a Takeout source's folder is gone,
        and then nothing changes."""
        assert not self.busy  # the scan thread is the only writer while it runs
        changed = sources.change_source_type(self.conn, source_id, source_type)
        self._sources_updated()
        if changed:
            # Dates decide the order and the month headers; an open viewer stays on its
            # photo and is rebound to the reloaded list.
            self.reload_grid()
        return changed

    def relocate_source_interactive(self, source: db.Source) -> None:
        if self.busy:
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
        if self.busy:
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

    def _release_grid(self) -> None:
        """Let go of the photos on screen before a source's rows and thumbnails are removed."""
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.close_viewer(-1)  # the photo showing may belong to this source
        self.grid_source.clear()
        self.grid.set_data_source(self.grid_source)  # also drops queued thumbnail loads
        self.thumbnails.wait()  # no thumbnail is written into the folder being removed

    def remove_source(self, source_id: int) -> None:
        """Delete the source's rows and thumbnails. Only after the user confirms."""
        assert not self.busy
        self._release_grid()
        sources.remove_source(self.conn, self.thumbs_root, source_id)
        self._sources_updated()
        self.reload_grid()

    def remove_missing_items(self, source_id: int) -> db.RemovedMissing:
        """Forget the source's missing files and photos left without files. Only after the
        user confirms (in the sources dialog)."""
        assert not self.busy
        removed = sources.remove_missing(self.conn, self.thumbs_root, source_id)
        self._sources_updated()
        self.reload_grid()
        return removed

    # --- scanning -----------------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self.scan_thread is not None

    @property
    def cleaning(self) -> bool:
        return self.cleanup_thread is not None

    @property
    def busy(self) -> bool:
        """A scan or a cleanup is running: sources, albums and scanning are locked."""
        return self.scanning or self.cleaning

    def _update_actions(self) -> None:
        idle = not self.busy
        self.add_action.setEnabled(idle)
        self.cleanup_action.setEnabled(not self.scanning)
        self.rescan_action.setEnabled(bool(self.sources) and idle)
        self.sources_action.setEnabled(bool(self.sources))
        self.empty.button.setEnabled(idle)
        self._update_album_actions()
        self.sidebar.set_people_enabled(idle)

    def rescan(self) -> None:
        self.rescan_sources(self.sources)

    def rescan_sources(self, to_scan: list[db.Source]) -> None:
        if to_scan and not self.busy:
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
        self._update_empty_text()
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
        self._update_empty_text()
        self._show_photo_count()
        self.refresh_sidebar()
        pending, self._pending_add = self._pending_add, None
        if pending is not None and not self._closing:
            self._after_cleaned_scan(pending)

    # --- Takeout cleanup ----------------------------------------------------------------

    def open_cleanup_wizard(self) -> None:
        """Open the cleanup wizard, or raise the one that is already open."""
        if self.cleanup_wizard is not None:
            self.cleanup_wizard.show()
            self.cleanup_wizard.raise_()
            self.cleanup_wizard.activateWindow()
            return
        if self.scanning:
            return
        wizard = CleanupWizard(
            cleanup_run.check_input,
            # The check runs on the main thread, on the sources it already holds.
            lambda inp, out: cleanup_run.check_folders_for(
                self.sources, self.library_root, inp, out
            ),
            self,
        )
        wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        wizard.destroyed.connect(self._on_wizard_destroyed)
        wizard.analyse_requested.connect(self._start_analysis)
        wizard.copy_requested.connect(self._start_copy)
        wizard.cancel_requested.connect(self._cancel_cleanup)
        wizard.add_to_library_requested.connect(self._add_cleanup_to_library)
        wizard.swap_requested.connect(self._swap_requested)
        wizard.open_folder_requested.connect(show_in_explorer)
        wizard.open_report_requested.connect(show_in_explorer)
        self.cleanup_wizard = wizard
        wizard.show()

    def _on_wizard_destroyed(self, *_args) -> None:
        self.cleanup_wizard = None

    def _start_cleanup_thread(self, job, input_root: Path, output_root: Path) -> None:
        wizard = self.cleanup_wizard
        if self.cleaning:  # the previous job is just ending (its result is out, the thread isn't)
            self._queued_cleanup = (job, input_root, output_root)
            return
        if self.scanning:
            if wizard is not None:
                wizard.show_error("Wait for the scan to finish, then try again.")
            return
        self._cleanup_input, self._cleanup_output = input_root, output_root
        # Both are read here, on the main thread, and handed to the worker thread.
        thread = CleanupThread(
            job,
            input_root,
            output_root,
            self.library_root,
            db.list_sources(self.conn),
            cleanup_run.input_is_source(self.conn, input_root),
            self.cleanup_options,
            self,
        )
        if wizard is not None:
            thread.progressed.connect(wizard.set_progress)
            thread.preview_ready.connect(wizard.show_preview)
            thread.cancelled.connect(wizard.show_cancelled)
            thread.failed.connect(wizard.show_error)
        thread.finished_ok.connect(self._on_cleanup_done)
        thread.finished.connect(self._on_cleanup_thread_finished)
        self.cleanup_thread = thread
        self._update_actions()
        self.scan_state_changed.emit()
        thread.start()

    def _start_analysis(self, input_root: Path, output_root: Path) -> None:
        self._start_cleanup_thread("analysis", input_root, output_root)

    def _start_copy(self) -> None:
        if self._cleanup_input is not None and self._cleanup_output is not None:
            self._start_cleanup_thread("copy", self._cleanup_input, self._cleanup_output)

    def _cancel_cleanup(self) -> None:
        if self.cleanup_thread is not None:
            self.cleanup_thread.cancel()

    def _on_cleanup_done(self, result: DoneResult, run_id: int) -> None:
        self._cleanup_run_id = run_id
        if self.cleanup_wizard is not None:
            self.cleanup_wizard.show_done(result)

    def _on_cleanup_thread_finished(self) -> None:
        if self.cleanup_thread is not None:
            self.cleanup_thread.deleteLater()
        self.cleanup_thread = None
        queued, self._queued_cleanup = self._queued_cleanup, None
        if queued is not None and not self._closing:
            self._start_cleanup_thread(*queued)
        self._update_actions()
        self.scan_state_changed.emit()
        self._update_empty_text()
        self._show_photo_count()
        self.refresh_sidebar()

    def _add_cleanup_to_library(self) -> None:
        """Add to library: the output becomes a ``cleaned`` source and is scanned; then its
        albums are imported and the swap offered."""
        output, run_id = self._cleanup_output, self._cleanup_run_id
        if output is None or run_id is None:
            return
        if self.busy:
            self._refuse_add("Wait for the current scan or cleanup to finish, then try again.")
        elif self.add_cleaned_output(output, run_id, self._cleanup_input) is None:
            self._refuse_add(None)

    def _refuse_add(self, message: str | None) -> None:
        if message:
            QMessageBox.information(self, "Add to library", message)
        if self.cleanup_wizard is not None:
            self.cleanup_wizard.add_button.setEnabled(True)

    def add_cleaned_output(
        self,
        output: Path,
        run_id: int | None,
        input_root: Path | None,
        label: str | None = None,
    ) -> db.Source | None:
        """Add a cleanup output as a ``cleaned`` source and scan it. Returns None (after
        telling the user) when it can't be added. Albums and the swap offer follow the scan."""
        try:
            source = sources.add_cleaned_source(self.conn, output, label)
        except ValueError as exc:
            QMessageBox.warning(self, "Add source", str(exc))
            return None
        self._sources_updated()
        self.reload_grid()
        add = CleanedAdd(source.id, output, run_id, input_root)
        self._pending_add = add
        if run_id is not None and input_root is not None:
            self._swap_context = add
        self.start_scan([self.sources_by_id[source.id]])
        return source

    def _after_cleaned_scan(self, add: CleanedAdd) -> None:
        last = db.last_scan(self.conn, add.source_id)
        if last is None or last.status != "completed":
            return  # the scan said why
        output_db = paths.cleanup_db_path(add.output)
        try:
            sources.import_run_albums(self.conn, output_db, add.run_id, add.source_id)
        except (ValueError, db.OutputDatabaseError) as exc:
            log.warning("Album import for %s failed: %s", add.output, exc)
            QMessageBox.warning(self, "Albums", f"The albums could not be created:\n{exc}")
        self.refresh_sidebar()
        self._fill_albums_menu()
        self.reload_grid()
        if add.run_id is not None and add.input is not None:
            self._offer_swap(add, quiet=True)

    def _swap_requested(self) -> None:
        if self.busy:
            QMessageBox.information(
                self, "Replace old source", "Wait for the scan to finish first."
            )
        elif self._swap_context is not None:
            self._offer_swap(self._swap_context, quiet=False)

    def _offer_swap(self, add: CleanedAdd, *, quiet: bool) -> None:
        """Offer to replace the old source by the cleaned one. Only database rows go."""
        assert add.run_id is not None and add.input is not None
        output_db = paths.cleanup_db_path(add.output)
        old = sources.source_for_folder(self.conn, add.input)
        if old is None or old.type == "cleaned":
            if not quiet:
                QMessageBox.information(
                    self, "Replace old source", "The old source is no longer in your library."
                )
            return
        if not sources.swap_ready(self.conn, add.source_id, output_db, add.run_id):
            if not quiet:
                QMessageBox.information(
                    self,
                    "Replace old source",
                    "The cleaned folder hasn't been fully scanned yet. Wait for the scan to "
                    "finish, then try again.",
                )
            return
        orphans = sources.swap_orphan_count(self.conn, old.id)
        note = ""
        if orphans:
            note = (
                f"\n\n{orphans:,} photo(s) only exist in the old source (for example Trash "
                "files) and will no longer be listed."
            )
        answer = QMessageBox.question(
            self,
            "Replace old source",
            f"Remove “{old.label}” from Imagory and keep the cleaned copy instead?{note}\n\n"
            "Photos keep their albums and people. Only the library entry is removed; no files "
            "on disk are touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._swap(old.id, add, output_db)

    def _swap(self, old_id: int, add: CleanedAdd, output_db: Path) -> None:
        assert not self.busy and add.run_id is not None
        self._release_grid()
        try:
            sources.swap_source(
                self.conn, self.thumbs_root, old_id, add.source_id, output_db, add.run_id
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Replace old source", str(exc))
        self._sources_updated()
        self.reload_grid()
        self.refresh_sidebar()
        self._fill_albums_menu()
        if self.cleanup_wizard is not None:
            self.cleanup_wizard.swap_button.hide()
            self.cleanup_wizard.swap_label.hide()

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
        self._queued_cleanup = None
        if self.cleanup_thread is not None:
            self.cleanup_thread.cancel()
            self.cleanup_thread.wait()
        if self.scan_thread is not None:
            self.status_label.setText("Stopping scan…")
            self.scan_thread.cancel()
            self.scan_thread.wait()  # in-flight results are committed before it exits
        self.grid_source.wait()
        self.thumbnails.reset()
        self.thumbnails.wait()
        super().closeEvent(event)
