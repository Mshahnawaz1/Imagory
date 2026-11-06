"""The left sidebar: All photos, Duplicates, people, albums, and sources. No database access.

The window feeds it albums, people and sources through setters and listens to its signals.
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
SECTION_ROLE = Qt.ItemDataRole.UserRole + 2

# Collapsible sections and whether each starts open. People starts closed; it can be long.
SECTION_DEFAULTS = {"people": False, "albums": True, "sources": True}


class Sidebar(QFrame):
    allPhotosRequested = Signal()
    duplicatesRequested = Signal()
    albumRequested = Signal(int)  # album id
    personRequested = Signal(int)  # person id
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
        self._people: list[tuple[int, str, int]] = []
        self._people_enabled = True
        self._sources: list[tuple[int, str]] = []
        self._current: tuple[str, int | None] = ("all", None)
        self._expanded = dict(SECTION_DEFAULTS)
        self._pinned_section: str | None = None  # header the user last clicked, if any

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

    def set_people(self, people: Iterable[tuple[int, str, int]]) -> None:
        """``(person id, name, photo count)`` rows. The People label is always shown."""
        self._people = list(people)
        self._rebuild()

    def set_people_enabled(self, enabled: bool) -> None:
        """People are read-only data, but like albums they are locked while a scan runs."""
        if enabled != self._people_enabled:
            self._people_enabled = enabled
            self._rebuild()

    def set_sources(self, sources: Iterable[tuple[int, str]]) -> None:
        """``(source id, label)`` rows."""
        self._sources = list(sources)
        self._rebuild()

    def set_section_expanded(self, section: str, expanded: bool) -> None:
        """Open or close ``"people"``, ``"albums"`` or ``"sources"``."""
        if self._expanded.get(section) != expanded:
            self._expanded[section] = expanded
            self._rebuild()

    def is_section_expanded(self, section: str) -> bool:
        return self._expanded.get(section, True)

    def set_current(self, kind: str, value: int | None = None) -> None:
        """Highlight ``"all"``, ``"duplicates"``, or a kind (``"album"``, ``"person"``,
        ``"source"``) with its id."""
        self._current = (kind, value)
        self._pinned_section = None
        if kind == "person" and not self._expanded["people"]:  # never hide the highlighted row
            self._expanded["people"] = True
            self._rebuild()
        else:
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

    def _section(self, title: str, key: str) -> None:
        """A clickable header that opens or closes its section."""
        arrow = "▾" if self._expanded[key] else "▸"
        item = QListWidgetItem(f"{arrow}  {title.upper()}")
        item.setData(KIND_ROLE, "section")
        item.setData(SECTION_ROLE, key)
        font = item.font()
        font.setBold(True)
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 7.0))
        item.setFont(font)
        item.setForeground(self.palette().placeholderText())
        item.setSizeHint(QSize(0, 30))
        item.setToolTip("Click to collapse" if self._expanded[key] else "Click to expand")
        self._list.addItem(item)

    def _add(
        self,
        text: str,
        kind: str,
        value: int | None = None,
        muted: bool = False,
        enabled: bool = True,
    ) -> None:
        item = QListWidgetItem(text)
        item.setData(KIND_ROLE, kind)
        item.setData(VALUE_ROLE, value)
        if not enabled:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        if muted or not enabled:
            item.setForeground(self.palette().placeholderText())
        self._list.addItem(item)

    def _rebuild(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        self._heading("Library")
        self._add("All photos", "all")
        self._add("Duplicates", "duplicates")
        self._section("People", "people")
        if self._expanded["people"]:
            for person_id, name, count in self._people:
                self._add(f"{name}   {count:,}", "person", person_id, enabled=self._people_enabled)
            if not self._people:
                self._add("No people yet", "none", muted=True)
        self._section("Albums", "albums")
        if self._expanded["albums"]:
            for album_id, name, count in self._albums:
                self._add(f"{name}   {count:,}", "album", album_id)
            if not self._albums:
                self._add("No albums yet", "none", muted=True)
        self._section("Sources", "sources")
        if self._expanded["sources"]:
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
            if self._pinned_section is not None:
                match = item.data(SECTION_ROLE) == self._pinned_section
            else:
                match = item.data(KIND_ROLE) == kind and item.data(VALUE_ROLE) == value
            if match:
                item.setSelected(True)
                break
        self._list.blockSignals(False)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        kind, value = item.data(KIND_ROLE), item.data(VALUE_ROLE)
        if kind != "section":
            self._pinned_section = None
        if kind == "all":
            self.allPhotosRequested.emit()
        elif kind == "duplicates":
            self.duplicatesRequested.emit()
        elif kind == "album":
            self.albumRequested.emit(value)
        elif kind == "section":
            key = item.data(SECTION_ROLE)
            self._pinned_section = key  # the highlight stays on the header just clicked
            self._expanded[key] = not self._expanded[key]
            self._rebuild()
        elif kind == "person":
            self.personRequested.emit(value)
        elif kind == "source":
            self.sourceRequested.emit(value)
        else:
            self._select_current()
