"""The left sidebar: All photos, Duplicates, albums, and sources. It owns no database access.

The window feeds it albums and sources through setters and listens to its signals.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

KIND_ROLE = Qt.ItemDataRole.UserRole
VALUE_ROLE = Qt.ItemDataRole.UserRole + 1


class Sidebar(QFrame):
    allPhotosRequested = Signal()
    duplicatesRequested = Signal()
    albumRequested = Signal(int)  # album id
    sourceRequested = Signal(int)  # source id
    manageAlbumsRequested = Signal()
    manageSourcesRequested = Signal()

    WIDTH = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(self.WIDTH)
        self._list = QListWidget(self)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.itemClicked.connect(self._on_clicked)
        self._albums: list[tuple[int, str, int]] = []
        self._sources: list[tuple[int, str]] = []
        self._current: tuple[str, int | None] = ("all", None)

        manage_albums = QPushButton("Manage albums…", self)
        manage_albums.clicked.connect(self.manageAlbumsRequested)
        manage_sources = QPushButton("Manage sources…", self)
        manage_sources.clicked.connect(self.manageSourcesRequested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._list, 1)
        layout.addWidget(manage_albums)
        layout.addWidget(manage_sources)
        self._rebuild()

    # --- public API ----------------------------------------------------------------------

    def set_albums(self, albums: Iterable[tuple[int, str, int]]) -> None:
        """``(album id, name, photo count)`` rows."""
        self._albums = list(albums)
        self._rebuild()

    def set_sources(self, sources: Iterable[tuple[int, str]]) -> None:
        """``(source id, label)`` rows."""
        self._sources = list(sources)
        self._rebuild()

    def set_current(self, kind: str, value: int | None = None) -> None:
        """Highlight ``"all"``, ``"duplicates"``, ``("album", id)`` or ``("source", id)``."""
        self._current = (kind, value)
        self._select_current()

    # --- internals -----------------------------------------------------------------------

    def _heading(self, text: str) -> None:
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self._list.addItem(item)
        label = QLabel(text.upper())
        label.setObjectName("sidebar-heading")
        item.setSizeHint(QSize(0, 30))
        self._list.setItemWidget(item, label)

    def _add(self, text: str, kind: str, value: int | None = None, muted: bool = False) -> None:
        item = QListWidgetItem(text)
        item.setData(KIND_ROLE, kind)
        item.setData(VALUE_ROLE, value)
        if muted:
            item.setForeground(self.palette().placeholderText())
        self._list.addItem(item)

    def _rebuild(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        self._heading("Library")
        self._add("All photos", "all")
        self._add("Duplicates", "duplicates")
        self._heading("Albums")
        for album_id, name, count in self._albums:
            self._add(f"{name}   {count:,}", "album", album_id)
        if not self._albums:
            self._add("No albums yet", "none", muted=True)
        self._heading("Sources")
        for source_id, label in self._sources:
            self._add(label, "source", source_id)
        self._list.blockSignals(False)
        self._select_current()

    def _select_current(self) -> None:
        kind, value = self._current
        self._list.blockSignals(True)
        self._list.clearSelection()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(KIND_ROLE) == kind and item.data(VALUE_ROLE) == value:
                item.setSelected(True)
                break
        self._list.blockSignals(False)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        kind, value = item.data(KIND_ROLE), item.data(VALUE_ROLE)
        if kind == "all":
            self.allPhotosRequested.emit()
        elif kind == "duplicates":
            self.duplicatesRequested.emit()
        elif kind == "album":
            self.albumRequested.emit(value)
        elif kind == "source":
            self.sourceRequested.emit(value)
        else:
            self._select_current()
