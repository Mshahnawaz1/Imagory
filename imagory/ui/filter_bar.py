"""The search and filter bar (PRD 8.7). It owns no database access.

The window feeds it the available sources and folders through setters or callables, listens to
``filterChanged``, and runs the filtered queries from ``imagory.db`` itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QDate, QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
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
ANY_PERSON = "Any person"
NO_DATE = QDate(1900, 1, 1)  # the minimum date stands for "not set" and shows "Any"
# The calendar of an unset date opens on this day. A fixed day for now; it was 1900-01-01.
CALENDAR_START = QDate(2026, 11, 1)

SourceOptions = Iterable[tuple[int, str]]  # (source id, label)
PersonOptions = Iterable[tuple[int, str, int]]  # (person id, name, photo count)


class FilterBar(QWidget):
    """Text, date range, source, folder, person, "Has duplicates" and "Missing" controls."""

    filterChanged = Signal(PhotoFilter)

    DEBOUNCE_MS = 250

    def __init__(
        self,
        parent: QWidget | None = None,
        sources: Callable[[], SourceOptions] | None = None,
        folders: Callable[[int], list[str]] | None = None,
        people: Callable[[], PersonOptions] | None = None,
    ) -> None:
        """``sources()`` returns ``(id, label)`` pairs; ``folders(source_id)`` returns
        relative folder paths (``db.folder_paths``); ``people()`` returns ``(id, name, count)``
        rows. All are optional and can be set later."""
        super().__init__(parent)
        self._sources_provider = sources
        self._folders_provider = folders
        self._people_provider = people
        self._sources: list[tuple[int, str]] = []
        self._last = PhotoFilter()
        self._pending_folder: str | None = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_if_changed)

        self._text = QLineEdit()
        self._text.setPlaceholderText("Search file names, folders, people and captions")
        self._text.setClearButtonEnabled(True)
        self._text.textChanged.connect(lambda _text: self._debounce.start())

        self._date_from = self._make_date_edit("From date")
        self._date_to = self._make_date_edit("To date")

        self._source_menu = QMenu(self)
        self._source_button = QToolButton()
        self._source_button.setText(SOURCE_LABEL)
        self._source_button.setObjectName("filter-source")
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

        self._person = QComboBox()
        self._person.setToolTip("Only photos tagged with this person (from Google Takeout)")
        self._person.setMinimumContentsLength(12)
        self._person.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._person.addItem(ANY_PERSON, None)
        self._person.setVisible(False)  # shown once there are people
        self._person.currentIndexChanged.connect(self._on_changed)

        self._duplicates = QCheckBox("Has duplicates")
        self._duplicates.toggled.connect(self._on_changed)
        self._missing = QCheckBox("Missing")
        self._missing.setToolTip("Show only photos whose files are all missing")
        self._missing.toggled.connect(self._on_changed)

        self._clear = QPushButton("Clear all")
        self._clear.setFlat(True)
        self._clear.clicked.connect(self.clear)

        top = QHBoxLayout()
        top.setContentsMargins(10, 8, 10, 8)
        top.setSpacing(8)
        top.addWidget(self._text, 1)
        top.addWidget(self._source_button)
        top.addWidget(self._folder)
        top.addWidget(self._person)
        top.addWidget(QLabel("Date"))
        top.addWidget(self._date_from)
        top.addWidget(QLabel("to"))
        top.addWidget(self._date_to)
        top.addWidget(self._duplicates)
        top.addWidget(self._missing)
        self._chips = QWidget()
        self._chip_row = QHBoxLayout(self._chips)
        self._chip_row.setContentsMargins(10, 0, 10, 8)
        self._chip_row.setSpacing(6)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self._chips)
        self.setObjectName("filter-bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

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
            person_id=self._person.currentData(),
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
        with QSignalBlocker(self._person):
            self._person.setCurrentIndex(max(self._person.findData(f.person_id), 0))
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

    def set_people(self, people: PersonOptions) -> None:
        """Replace the person choices. The chosen person stays if they are still listed;
        otherwise the person filter is dropped. The control is hidden when there are none."""
        keep = self._person.currentData()
        with QSignalBlocker(self._person):
            self._person.clear()
            self._person.addItem(ANY_PERSON, None)
            for person_id, name, count in people:
                self._person.addItem(f"{name}  ({count:,})", person_id)
            self._person.setCurrentIndex(max(self._person.findData(keep), 0))
        self._person.setVisible(self._person.count() > 1)
        self._update_widgets()
        self._emit_if_changed()

    def person_name(self, person_id: int | None) -> str | None:
        """The listed name of a person, without the count."""
        index = self._person.findData(person_id) if person_id is not None else -1
        if index < 0:
            return None
        return self._person.itemText(index).rsplit("  (", 1)[0]

    def set_providers(
        self,
        sources: Callable[[], SourceOptions] | None = None,
        folders: Callable[[int], list[str]] | None = None,
        people: Callable[[], PersonOptions] | None = None,
    ) -> None:
        if sources is not None:
            self._sources_provider = sources
        if folders is not None:
            self._folders_provider = folders
        if people is not None:
            self._people_provider = people
        self.refresh_options()

    def refresh_options(self) -> None:
        """Re-query the providers, e.g. after a scan or when sources change."""
        if self._sources_provider is not None:
            self.set_sources(self._sources_provider())
        else:
            self._reload_folders()
        if self._people_provider is not None:
            self.set_people(self._people_provider())

    # --- internals -----------------------------------------------------------------------

    def _make_date_edit(self, tooltip: str) -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setFixedWidth(116)
        edit.setDisplayFormat("yyyy-MM-dd")
        edit.setMinimumDate(NO_DATE)
        edit.setSpecialValueText("Any")
        edit.setDate(NO_DATE)
        edit.setToolTip(f"{tooltip} (inclusive). Choose the earliest date to clear it.")
        edit.dateChanged.connect(self._on_changed)
        edit.calendarWidget().installEventFilter(_CalendarStart(edit))
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
        self._rebuild_chips()

    def _rebuild_chips(self) -> None:
        """One removable chip per active filter, so the active state is visible at a glance."""
        while self._chip_row.count():
            widget = self._chip_row.takeAt(0).widget()
            if widget is not None and widget is not self._clear:
                widget.setParent(None)
                widget.deleteLater()
        for text, remove in self._active_chips():
            chip = QPushButton(f"{text}  ✕")
            chip.setObjectName("chip")
            chip.setToolTip("Remove this filter")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(remove)
            self._chip_row.addWidget(chip)
        self._chip_row.addStretch(1)
        self._chip_row.addWidget(self._clear)
        self._chips.setVisible(self._chip_row.count() > 2)

    def _active_chips(self) -> list[tuple[str, Callable[[], None]]]:
        chips: list[tuple[str, Callable[[], None]]] = []
        text = self._text.text().strip()
        if text:
            chips.append((f"“{text}”", self._text.clear))
        start, end = self._date_value(self._date_from), self._date_value(self._date_to)
        if start or end:
            chips.append((f"{start or 'Any'} – {end or 'Any'}", self._clear_dates))
        checked = [a.text() for a in self._source_menu.actions() if a.isChecked()]
        if checked:
            chips.append((", ".join(checked), self._uncheck_sources))
        if self._folder_data():
            chips.append((self._folder_data(), lambda: self._folder.setCurrentIndex(0)))
        person_id = self._person.currentData()
        if person_id is not None:
            chips.append(
                (self.person_name(person_id) or "Person", lambda: self._person.setCurrentIndex(0))
            )
        if self._duplicates.isChecked():
            chips.append(("Has duplicates", lambda: self._duplicates.setChecked(False)))
        if self._missing.isChecked():
            chips.append(("Missing", lambda: self._missing.setChecked(False)))
        return chips

    def _clear_dates(self) -> None:
        with QSignalBlocker(self._date_from), QSignalBlocker(self._date_to):
            self._date_from.setDate(NO_DATE)
            self._date_to.setDate(NO_DATE)
        self._on_changed()

    def _uncheck_sources(self) -> None:
        for action in self._source_menu.actions():
            with QSignalBlocker(action):
                action.setChecked(False)
        self._on_sources_changed()

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


class _CalendarStart(QObject):
    """Opens the calendar of an unset date edit on ``CALENDAR_START`` instead of 1900.

    Only the shown month and the highlighted day change; the edit keeps showing "Any" until
    the user picks a day (the calendar's signals are blocked while it is positioned).
    """

    def __init__(self, edit: QDateEdit) -> None:
        super().__init__(edit)
        self._edit = edit

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and self._edit.date() == NO_DATE:
            with QSignalBlocker(watched):
                watched.setSelectedDate(CALENDAR_START)
                watched.setCurrentPage(CALENDAR_START.year(), CALENDAR_START.month())
        return False
