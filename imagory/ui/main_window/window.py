"""The main window: layout, menus, the grid and viewer, the sidebar, albums, and Help.

Sources, scanning and Takeout cleanup are run by the controllers in this package
(:mod:`.source_actions`, :mod:`.scan`, :mod:`.cleanup`), reached as ``window.source_actions``,
``window.scan`` and ``window.cleanup``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
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
    QHBoxLayout,
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

from imagory import __version__, db, paths
from imagory.ui.albums import AddToAlbumDialog, AlbumsDialog
from imagory.ui.filter_bar import FilterBar
from imagory.ui.grid import PhotoDataSource, PhotoThumbnails, view_queries
from imagory.ui.icons import app_icon
from imagory.ui.main_window.cleanup import CleanupController
from imagory.ui.main_window.scan import ScanController
from imagory.ui.main_window.source_actions import SourceActions
from imagory.ui.photo_grid import PhotoGrid
from imagory.ui.sidebar import Sidebar
from imagory.ui.sources_dialog import SourcesDialog
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

PAGE_EMPTY, PAGE_GRID, PAGE_VIEWER = range(3)
SIDEBAR_SETTING = "sidebar_visible"
THUMBNAIL_LEVELS = ("small", "medium", "large")


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
        # The work the window starts: managing sources, scanning, and Takeout cleanup.
        self.source_actions = SourceActions(self)
        self.scan = ScanController(self)
        self.cleanup = CleanupController(self)
        self._anchor_id: int | None = None  # photo to keep in view across a grid reload
        self.closing = False
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
        self.grid.selection_changed.connect(self.show_photo_count)
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
        self.empty = EmptyPage(self.source_actions.add_source_interactive, self)

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
        self.add_action.triggered.connect(self.source_actions.add_source_interactive)
        self.rescan_action = QAction("Rescan all", self)
        self.rescan_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.rescan_action.setStatusTip("Look for new, changed, and missing photos in all sources")
        self.rescan_action.triggered.connect(self.scan.rescan)
        self.cleanup_action = QAction("Clean up Takeout…", self)
        self.cleanup_action.setStatusTip(
            "Copy a Google Takeout export into a tidy folder (the original is never changed)"
        )
        self.cleanup_action.triggered.connect(self.cleanup.open_wizard)
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
        self.albums_menu.aboutToShow.connect(self.fill_albums_menu)
        self.fill_albums_menu()

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
        self.cancel_button.clicked.connect(self.scan.cancel)
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
            self.update_actions()
            self.status_label.setText("")
            return
        self.grid_source.load()
        self.grid.set_data_source(self.grid_source)
        if not self._restore_anchor():
            # Until the anchor's row arrives, stay where the grid was rather than jump up.
            self.grid.verticalScrollBar().setValue(scroll_y)
        if self.grid_source.complete:
            self._anchor_id = None  # everything is loaded: a photo not found is gone
        self.update_empty_text()
        if self.pages.currentIndex() != PAGE_VIEWER:
            self.pages.setCurrentIndex(PAGE_GRID)
        self._update_bars()
        self.update_actions()
        self.show_photo_count()

    def _update_bars(self) -> None:
        in_grid = bool(self.sources) and self.pages.currentIndex() != PAGE_VIEWER
        self.filter_bar.setVisible(in_grid)
        self.sidebar.setVisible(self.sidebar_action.isChecked() and bool(self.sources))
        self._sync_sidebar_selection()
        self.album_bar.setVisible(in_grid and self.album is not None)
        if self.album is not None:
            self.album_label.setText(f"Album: <b>{html_escape(self.album.name)}</b>")

    def _on_rest_of_grid(self, positions: list[int]) -> None:
        if self.closing:
            return
        self._rebind_viewer(positions)
        self.grid.items_inserted(positions)  # the counts may have changed too
        self._restore_anchor()
        self._anchor_id = None  # the whole grid is loaded: a photo not found is gone
        self.show_photo_count()

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
        if self.closing:
            return
        positions = self.grid_source.insert(photos)
        self.grid.items_inserted(positions)
        if positions:
            self._rebind_viewer(positions)

    def update_empty_text(self) -> None:
        if self.scanning:
            text = "Looking for photos…"
        elif self.album is not None:
            text = "This album has no photos to show. Select photos and use Add to album."
        elif not self.current_filter().is_empty:
            text = "No photos match the filter."
        else:
            text = "No photos to show. Add photos to your folders, then use Rescan all."
        self.grid.set_empty_text(text)

    def show_photo_count(self) -> None:
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

    def fill_albums_menu(self) -> None:
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
        if not self.scanning and db.get_setting(self.conn, SIDEBAR_SETTING) != value:
            db.set_setting(self.conn, SIDEBAR_SETTING, value)
            self.conn.commit()

    def _people_rows(self) -> list[tuple[int, str, int]]:
        return [(p.id, p.name, p.photo_count) for p in db.list_people(self.conn)]

    def refresh_sidebar(self) -> None:
        people = self._people_rows()
        self.sidebar.set_people(people)
        self.sidebar.set_people_enabled(not self.scanning)
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
        if self.scanning:
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
                self.show_photo_count()
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

    def refresh_viewer(self) -> None:
        """Redraw the open photo's details, e.g. after its source was renamed."""
        if self.pages.currentIndex() == PAGE_VIEWER and self.viewer.photos:
            self.viewer.rebind(self.viewer.photos, self.viewer.row)

    # --- sources ------------------------------------------------------------------------

    def _load_sources(self) -> None:
        self.sources = db.list_sources(self.conn)
        self.sources_by_id.clear()
        self.sources_by_id.update((s.id, s) for s in self.sources)

    def sources_updated(self) -> None:
        self._load_sources()
        self.filter_bar.refresh_options()  # may change the filter, which reloads the grid
        self.sources_changed.emit()

    def show_sources(self) -> None:
        dialog = SourcesDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def release_grid(self) -> None:
        """Let go of the photos on screen before a source's rows and thumbnails are removed."""
        if self.pages.currentIndex() == PAGE_VIEWER:
            self.close_viewer(-1)  # the photo showing may belong to this source
        self.grid_source.clear()
        self.grid.set_data_source(self.grid_source)  # also drops queued thumbnail loads
        self.thumbnails.wait()  # no thumbnail is written into the folder being removed

    # --- state --------------------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self.scan.thread is not None

    @property
    def cleaning(self) -> bool:
        return self.cleanup.thread is not None

    @property
    def busy(self) -> bool:
        """A scan or a cleanup is running: sources, albums and scanning are locked."""
        return self.scanning or self.cleaning

    def update_actions(self) -> None:
        idle = not self.busy
        self.add_action.setEnabled(idle)
        self.cleanup_action.setEnabled(not self.scanning)
        self.rescan_action.setEnabled(bool(self.sources) and idle)
        self.sources_action.setEnabled(bool(self.sources))
        self.empty.button.setEnabled(idle)
        self._update_album_actions()
        self.sidebar.set_people_enabled(not self.scanning)

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
        self.closing = True
        self.cleanup.stop()
        self.scan.stop()
        self.grid_source.wait()
        self.thumbnails.reset()
        self.thumbnails.wait()
        super().closeEvent(event)
