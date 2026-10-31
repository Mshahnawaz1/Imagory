"""Dialogs for adding sources and managing the library's sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from imagory import db
from imagory.takeout import Detection

if TYPE_CHECKING:
    from imagory.ui.main_window import MainWindow

_NUMBER_ALIGN = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
TYPE_NAMES = {"plain": "Plain folder", "takeout": "Google Takeout export"}


def default_label(folder: Path) -> str:
    return folder.name or str(folder)


def format_scan_time(utc: str | None) -> str:
    """``2026-10-31T09:15:02.123456Z`` as ``2026-10-31 09:15`` UTC, or "Never"."""
    if not utc:
        return "Never"
    return utc[:16].replace("T", " ") + " UTC"


class TypeCombo(QComboBox):
    def __init__(self, current: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        for value in db.SOURCE_TYPES:
            self.addItem(TYPE_NAMES[value], value)
        self.setCurrentIndex(db.SOURCE_TYPES.index(current))

    @property
    def value(self) -> str:
        return self.currentData()


class AddSourceDialog(QDialog):
    """Name the new source and confirm or change its detected type."""

    def __init__(self, folder: Path, detection: Detection, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add source")
        self.detection = detection
        self.label_edit = QLineEdit(default_label(folder), self)
        self.type_combo = TypeCombo(detection.type, self)
        detected = QLabel(f"Detected: {TYPE_NAMES[detection.type]}. {detection.reason}", self)
        detected.setWordWrap(True)
        folder_label = QLabel(str(folder), self)
        folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow("Folder:", folder_label)
        form.addRow("Label:", self.label_edit)
        form.addRow("Type:", self.type_combo)
        form.addRow("", detected)
        note = QLabel("Imagory only reads this folder. Your photos are never changed.", self)
        note.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add and scan")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.label_edit.textChanged.connect(
            lambda text: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                bool(text.strip())
            )
        )
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.resize(520, self.sizeHint().height())

    def _accept(self) -> None:
        if self.label:
            self.accept()

    @property
    def label(self) -> str:
        return self.label_edit.text().strip()

    @property
    def source_type(self) -> str:
        return self.type_combo.value


class SourcesDialog(QDialog):
    """Lists the sources; its buttons call back into the main window, which owns the work."""

    COLUMNS = ("Label", "Type", "Folder", "Photos", "Missing", "Last scan")

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Sources")
        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self._rename())

        self.add_button = QPushButton("Add…", self)
        self.rescan_button = QPushButton("Rescan", self)
        self.rename_button = QPushButton("Rename…", self)
        self.type_button = QPushButton("Change type…", self)
        self.relocate_button = QPushButton("Relocate…", self)
        self.remove_button = QPushButton("Remove…", self)
        close_button = QPushButton("Close", self)
        self.add_button.clicked.connect(window.add_source_interactive)
        self.rescan_button.clicked.connect(self._rescan)
        self.rename_button.clicked.connect(self._rename)
        self.type_button.clicked.connect(self._change_type)
        self.relocate_button.clicked.connect(self._relocate)
        self.remove_button.clicked.connect(self._remove)
        close_button.clicked.connect(self.accept)

        buttons = QVBoxLayout()
        for button in (
            self.add_button,
            self.rescan_button,
            self.rename_button,
            self.type_button,
            self.relocate_button,
            self.remove_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout = QHBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.resize(900, 360)

        window.sources_changed.connect(self.refresh)
        window.scan_state_changed.connect(self._update_buttons)
        self.refresh()

    def refresh(self) -> None:
        selected = self.selected_source()
        stats = self.window_.source_stats()
        sources = self.window_.sources
        self.table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            counts = stats.get(source.id, db.SourceStats())
            type_name = TYPE_NAMES[source.type] + (" (chosen)" if source.type_overridden else "")
            values = (
                source.label,
                type_name,
                source.root_path,
                f"{counts.photos:,}",
                f"{counts.missing:,}",
                format_scan_time(source.last_scan_at),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, source.id)
                if column in (3, 4):
                    item.setTextAlignment(_NUMBER_ALIGN)
                self.table.setItem(row, column, item)
            if selected is not None and source.id == selected.id:
                self.table.selectRow(row)
        if self.table.currentRow() < 0 and sources:
            self.table.selectRow(0)
        self._update_buttons()

    def selected_source(self) -> db.Source | None:
        items = self.table.selectedItems()
        if not items:
            return None
        source_id = items[0].data(Qt.ItemDataRole.UserRole)
        return next((s for s in self.window_.sources if s.id == source_id), None)

    def _update_buttons(self) -> None:
        has = self.selected_source() is not None
        idle = not self.window_.scanning
        self.add_button.setEnabled(idle)
        # The scan thread is the only database writer while a scan runs.
        for button in (
            self.rescan_button,
            self.rename_button,
            self.type_button,
            self.relocate_button,
            self.remove_button,
        ):
            button.setEnabled(has and idle)

    def _rescan(self) -> None:
        if (source := self.selected_source()) is not None:
            self.window_.rescan_sources([source])

    def _rename(self) -> None:
        if (source := self.selected_source()) is not None:
            self.window_.rename_source_interactive(source)

    def _change_type(self) -> None:
        if (source := self.selected_source()) is not None:
            self.window_.change_type_interactive(source)

    def _relocate(self) -> None:
        if (source := self.selected_source()) is not None:
            self.window_.relocate_source_interactive(source)

    def _remove(self) -> None:
        if (source := self.selected_source()) is not None:
            self.window_.remove_source_interactive(source)
