"""The search and filter bar (PRD 8.7). It owns no database access.

The window feeds it the available sources and folders through setters or callables, listens to
``filterChanged``, and runs the filtered queries from ``imagory.db`` itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagory.db import PhotoFilter

SOURCE_LABEL = "Sources"
ALL_FOLDERS = "All folders"
NO_DATE = QDate(1900, 1, 1)  # the minimum date stands for "not set" and shows "Any"

SourceOptions = Iterable[tuple[int, str]]  # (source id, label)


class FilterBar(QWidget):
    """Text, date range, source, folder, "Has duplicates" and "Missing" controls."""

    filterChanged = Signal(PhotoFilter)

    DEBOUNCE_MS = 250

    def __init__(
        self,
        parent: QWidget | None = None,
        sources: Callable[[], SourceOptions] | None = None,
        folders: Callable[[int], list[str]] | None = None,
    ) -> None:
        """``sources()`` returns ``(id, label)`` pairs; ``folders(source_id)`` returns
        relative folder paths (``db.folder_paths``). Both are optional and can be set later."""
        super().__init__(parent)
        self._sources_provider = sources
        self._folders_provider = folders
        self._sources: list[tuple[int, str]] = []
        self._last = PhotoFilter()
        self._pending_folder: str | None = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_if_changed)

        self._text = QLineEdit()
        self._text.setPlaceholderText("Search file names and folders")
        self._text.setClearButtonEnabled(True)
        self._text.textChanged.connect(lambda _text: self._debounce.start())

        self._date_from = self._make_date_edit("From date")
        self._date_to = self._make_date_edit("To date")

        self._source_menu = QMenu(self)
        self._source_button = QToolButton()
        self._source_button.setText(SOURCE_LABEL)
        self._source_button.setToolTip("Only photos found in the checked sources")
        self._source_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._source_button.setMenu(self._source_menu)

        self._folder = QComboBox()
        self._folder.setToolTip("Only photos in this folder and its subfolders")
        self._folder.setMinimumContentsLength(16)
        self._folder.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._folder.currentIndexChanged.connect(self._on_changed)

        self._duplicates = QCheckBox("Has duplicates")
        self._duplicates.toggled.connect(self._on_changed)
        self._missing = QCheckBox("Missing")
        self._missing.setToolTip("Show only photos whose files are all missing")
        self._missing.toggled.connect(self._on_changed)

        self._clear = QPushButton("Clear filters")
        self._clear.clicked.connect(self.clear)

        top = QHBoxLayout()
        top.addWidget(self._text, 1)
        top.addWidget(self._source_button)
        top.addWidget(self._folder)
        top.addWidget(self._duplicates)
        top.addWidget(self._missing)
        top.addWidget(self._clear)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Date:"))
        bottom.addWidget(self._date_from)
        bottom.addWidget(QLabel("to"))
        bottom.addWidget(self._date_to)
        bottom.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addLayout(bottom)

        self.refresh_options()
        self._update_widgets()

    # --- public API ----------------------------------------------------------------------

    def filter(self) -> PhotoFilter:
        """The filter the controls currently show (the text is not debounced here)."""
        folder = self._folder_data()
        source_ids = self._checked_source_ids()
        return PhotoFilter(
            text=self._text.text().strip(),
            date_from=self._date_value(self._date_from),
            date_to=self._date_value(self._date_to),
            source_ids=frozenset(source_ids),
            folder=(source_ids[0], folder) if folder and len(source_ids) == 1 else None,
            has_duplicates=self._duplicates.isChecked(),
            missing=self._missing.isChecked(),
        )

    def set_filter(self, f: PhotoFilter) -> None:
        """Show ``f`` in the controls and emit ``filterChanged`` if it differs from the last
        filter emitted. A folder pins the source picker to the folder's source (same results)."""
        self._debounce.stop()
        source_ids = set(f.source_ids)
        if f.folder is not None:
            source_ids = {f.folder[0]}
        with QSignalBlocker(self._text):
            self._text.setText(f.text)
        for edit, value in ((self._date_from, f.date_from), (self._date_to, f.date_to)):
            with QSignalBlocker(edit):
                edit.setDate(QDate.fromString(value, Qt.DateFormat.ISODate) if value else NO_DATE)
        for action in self._source_menu.actions():
            with QSignalBlocker(action):
                action.setChecked(action.data() in source_ids)
        with QSignalBlocker(self._duplicates):
            self._duplicates.setChecked(f.has_duplicates)
        with QSignalBlocker(self._missing):
            self._missing.setChecked(f.missing)
        self._pending_folder = f.folder[1] if f.folder else None
        self._reload_folders()
        self._update_widgets()
        self._emit_if_changed()

    def clear(self) -> None:
        self.set_filter(PhotoFilter())

    def set_sources(self, sources: SourceOptions) -> None:
        """Replace the source choices, keeping the checked ones that still exist."""
        keep = {i for i in self._checked_source_ids()}
        self._sources = list(sources)
        self._source_menu.clear()
        for source_id, label in self._sources:
            action = self._source_menu.addAction(label)
            action.setCheckable(True)
            action.setData(source_id)
            action.setChecked(source_id in keep)
            action.toggled.connect(self._on_sources_changed)
        self._update_source_text()
        self._reload_folders()
        self._emit_if_changed()

    def set_providers(
        self,
        sources: Callable[[], SourceOptions] | None = None,
        folders: Callable[[int], list[str]] | None = None,
    ) -> None:
        if sources is not None:
            self._sources_provider = sources
        if folders is not None:
            self._folders_provider = folders
        self.refresh_options()

    def refresh_options(self) -> None:
        """Re-query the providers, e.g. after a scan or when sources change."""
        if self._sources_provider is not None:
            self.set_sources(self._sources_provider())
        else:
            self._reload_folders()

    # --- internals -----------------------------------------------------------------------

    def _make_date_edit(self, tooltip: str) -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.setMinimumDate(NO_DATE)
        edit.setSpecialValueText("Any")
        edit.setDate(NO_DATE)
        edit.setToolTip(f"{tooltip} (inclusive). Choose the earliest date to clear it.")
        edit.dateChanged.connect(self._on_changed)
        return edit

    @staticmethod
    def _date_value(edit: QDateEdit) -> str | None:
        return None if edit.date() == NO_DATE else edit.date().toString(Qt.DateFormat.ISODate)

    def _checked_source_ids(self) -> list[int]:
        return sorted(a.data() for a in self._source_menu.actions() if a.isChecked())

    def _folder_data(self) -> str:
        return self._folder.currentData() or ""

    def _scope_source(self) -> int | None:
        """The one source the folder list belongs to, if there is exactly one."""
        checked = self._checked_source_ids()
        if len(checked) == 1:
            return checked[0]
        if not checked and len(self._sources) == 1:
            return self._sources[0][0]
        return None

    def _reload_folders(self) -> None:
        wanted = self._pending_folder if self._pending_folder is not None else self._folder_data()
        self._pending_folder = None
        source_id = self._scope_source()
        with QSignalBlocker(self._folder):
            self._folder.clear()
            self._folder.addItem(ALL_FOLDERS, "")
            if source_id is not None and self._folders_provider is not None:
                for path in self._folders_provider(source_id):
                    depth = path.count("/")
                    self._folder.addItem("    " * depth + path.rsplit("/", 1)[-1], path)
            index = self._folder.findData(wanted) if wanted else 0
            self._folder.setCurrentIndex(max(index, 0))
        self._folder.setEnabled(source_id is not None and self._folders_provider is not None)

    def _update_source_text(self) -> None:
        names = [a.text() for a in self._source_menu.actions() if a.isChecked()]
        if not names:
            self._source_button.setText(SOURCE_LABEL)
        elif len(names) == 1:
            self._source_button.setText(names[0])
        else:
            self._source_button.setText(f"{len(names)} sources")

    def _update_widgets(self) -> None:
        self._clear.setEnabled(self._text.text() != "" or not self.filter().is_empty)

    def _on_sources_changed(self, _checked: bool = False) -> None:
        self._update_source_text()
        self._reload_folders()
        self._on_changed()

    def _on_changed(self, *_args) -> None:
        self._debounce.stop()  # a pending text change is included in this emit
        self._update_widgets()
        self._emit_if_changed()

    def _emit_if_changed(self) -> None:
        self._update_widgets()
        current = self.filter()
        if current != self._last:
            self._last = current
            self.filterChanged.emit(current)
