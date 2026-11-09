"""The Takeout cleanup wizard (docs/takeout-cleanup.md, UI section).

A non-modal window with six pages: input, output, analyse, preview, copy, done. It never calls the
pipeline, the database or the file system itself: folder pickers only return paths, checks are
injected callables, and everything else leaves as a signal. Text that can come from sidecars or
paths is always shown as plain text.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from imagory.cleanup.types import DoneResult, FolderCheck, PreviewCounts

PAGE_INPUT, PAGE_OUTPUT, PAGE_ANALYSE, PAGE_PREVIEW, PAGE_COPY, PAGE_DONE = range(6)

MAX_LISTED = 50  # failures and unaccounted files shown on the done page


def format_bytes(n: int) -> str:
    """Bytes as B, KB, MB or GB (1024-based, one decimal above bytes)."""
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def safe_to_delete_message(copied: int) -> str:
    return (
        f"All {copied} files copied and verified. You can delete the original Takeout folder "
        "if you want; Imagory never does."
    )


def _label(text: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _confirm_cancel(parent: QWidget) -> bool:
    answer = QMessageBox.question(
        parent,
        "Cancel cleanup?",
        "A cleanup is running. Cancel it? Nothing is lost; you can resume later.",
    )
    return answer == QMessageBox.StandardButton.Yes


class CleanupWizard(QWidget):
    analyse_requested = Signal(Path, Path)
    copy_requested = Signal()
    cancel_requested = Signal()
    add_to_library_requested = Signal()
    swap_requested = Signal()
    open_folder_requested = Signal(Path)
    open_report_requested = Signal(Path)

    def __init__(
        self,
        check_input: Callable[[Path], tuple[bool, str]],
        check_output: Callable[[Path, Path], FolderCheck],
        parent: QWidget | None = None,
        *,
        pick_folder: Callable[[QWidget, str], str] | None = None,
        confirm_cancel: Callable[[QWidget], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clean up Takeout")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(560, 420)
        self._check_input = check_input
        self._check_output = check_output
        self._pick_folder = pick_folder or self._default_pick
        self._confirm_cancel = confirm_cancel or _confirm_cancel
        self._input: Path | None = None
        self._output: Path | None = None
        self._output_ok = False
        self._failed = False
        self._done: DoneResult | None = None

        self._stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self._stack)
        self._build_input()
        self._build_output()
        self._build_analyse()
        self._build_preview()
        self._build_copy()
        self._build_done()
        self._stack.setCurrentIndex(PAGE_INPUT)

    # -- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _default_pick(parent: QWidget, title: str) -> str:
        return QFileDialog.getExistingDirectory(parent, title)

    @property
    def page(self) -> int:
        return self._stack.currentIndex()

    @property
    def working(self) -> bool:
        """True while analysing or copying (closing then asks to cancel)."""
        return self.page in (PAGE_ANALYSE, PAGE_COPY) and not self._failed

    def _add_page(self, title: str) -> QVBoxLayout:
        page = QWidget()
        box = QVBoxLayout(page)
        heading = _label(title, wrap=False)
        font = heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        heading.setFont(font)
        box.addWidget(heading)
        self._stack.addWidget(page)
        return box

    @staticmethod
    def _buttons(*buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        for b in buttons:
            row.addWidget(b)
        return row

    @staticmethod
    def _primary(text: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("primary")
        return b

    # -- page 1: input ---------------------------------------------------------------------
    def _build_input(self) -> None:
        box = self._add_page("1. Choose the Takeout folder")
        box.addWidget(_label("Pick the folder you unzipped from Google Takeout. It is only read."))
        self.input_path_label = _label("No folder chosen")
        self.input_browse = QPushButton("Browse…")
        self.input_browse.clicked.connect(self._browse_input)
        self.input_message = _label()
        self.input_next = self._primary("Next")
        self.input_next.setEnabled(False)
        self.input_next.clicked.connect(lambda: self._stack.setCurrentIndex(PAGE_OUTPUT))
        box.addWidget(self.input_path_label)
        box.addWidget(self.input_browse)
        box.addWidget(self.input_message)
        box.addStretch(1)
        box.addLayout(self._buttons(self.input_next))

    def _browse_input(self) -> None:
        chosen = self._pick_folder(self, "Choose the Takeout folder")
        if chosen:
            self.set_input(Path(chosen))

    def set_input(self, folder: Path) -> None:
        self._input = folder
        self.input_path_label.setText(str(folder))
        ok, message = self._check_input(folder)
        self.input_message.setText(message if ok else f"Warning: {message}")
        self.input_next.setEnabled(True)
        self._refresh_output()

    # -- page 2: output --------------------------------------------------------------------
    def _build_output(self) -> None:
        box = self._add_page("2. Choose the output folder")
        box.addWidget(_label("Cleaned copies go here. Use a folder outside the Takeout folder."))
        self.output_path_label = _label("No folder chosen")
        self.output_browse = QPushButton("Browse…")
        self.output_browse.clicked.connect(self._browse_output)
        self.output_message = _label()
        self.output_back = QPushButton("Back")
        self.output_back.clicked.connect(lambda: self._stack.setCurrentIndex(PAGE_INPUT))
        self.output_next = self._primary("Analyse")
        self.output_next.setEnabled(False)
        self.output_next.clicked.connect(self._start_analyse)
        box.addWidget(self.output_path_label)
        box.addWidget(self.output_browse)
        box.addWidget(self.output_message)
        box.addStretch(1)
        box.addLayout(self._buttons(self.output_back, self.output_next))

    def _browse_output(self) -> None:
        chosen = self._pick_folder(self, "Choose the output folder")
        if chosen:
            self.set_output(Path(chosen))

    def set_output(self, folder: Path) -> None:
        self._output = folder
        self.output_path_label.setText(str(folder))
        self._refresh_output()

    def _refresh_output(self) -> None:
        if self._input is None or self._output is None:
            self._output_ok = False
        else:
            check = self._check_output(self._input, self._output)
            self.output_message.setText(check.message)
            self._output_ok = check.mode != "refused"
        self.output_next.setEnabled(self._output_ok)

    def _start_analyse(self) -> None:
        if self._input is None or self._output is None or not self._output_ok:
            return
        self._failed = False
        self.analyse_status.setText("Starting…")
        self.analyse_bar.setRange(0, 0)
        self.analyse_cancel.setText("Cancel")
        self._stack.setCurrentIndex(PAGE_ANALYSE)
        self.analyse_requested.emit(self._input, self._output)

    # -- pages 3 and 5: progress -----------------------------------------------------------
    def _build_progress(self, title: str) -> tuple[QLabel, QProgressBar, QPushButton]:
        box = self._add_page(title)
        status = _label()
        bar = QProgressBar()
        bar.setRange(0, 0)
        cancel = QPushButton("Cancel")
        box.addWidget(status)
        box.addWidget(bar)
        box.addStretch(1)
        box.addLayout(self._buttons(cancel))
        return status, bar, cancel

    def _build_analyse(self) -> None:
        self.analyse_status, self.analyse_bar, self.analyse_cancel = self._build_progress(
            "3. Analysing"
        )
        self.analyse_cancel.clicked.connect(self._cancel_clicked)

    def _build_copy(self) -> None:
        self.copy_status, self.copy_bar, self.copy_cancel = self._build_progress("5. Copying")
        self.copy_cancel.clicked.connect(self._cancel_clicked)

    def _cancel_clicked(self) -> None:
        if self._failed:  # the button reads "Back" after an error
            self._failed = False
            self._stack.setCurrentIndex(PAGE_OUTPUT)
            return
        status = self.copy_status if self.page == PAGE_COPY else self.analyse_status
        status.setText("Cancelling…")
        self.cancel_requested.emit()

    @Slot(str, int, int)
    def set_progress(self, stage: str, done: int, total: int) -> None:
        if self.page == PAGE_COPY:
            status, bar = self.copy_status, self.copy_bar
        else:
            status, bar = self.analyse_status, self.analyse_bar
        if total > 0:
            bar.setRange(0, total)
            bar.setValue(min(done, total))
            status.setText(f"{stage}: {done} of {total}")
        else:
            bar.setRange(0, 0)
            status.setText(stage)

    @Slot(str)
    def show_error(self, message: str) -> None:
        """Show a failure on the progress page; its button becomes Back."""
        self._failed = True
        if self.page not in (PAGE_ANALYSE, PAGE_COPY):
            self._stack.setCurrentIndex(PAGE_ANALYSE)
        if self.page == PAGE_COPY:
            status, bar, cancel = self.copy_status, self.copy_bar, self.copy_cancel
        else:
            status, bar, cancel = self.analyse_status, self.analyse_bar, self.analyse_cancel
        bar.setRange(0, 1)
        bar.setValue(0)
        status.setText(f"Something went wrong: {message}")
        cancel.setText("Back")

    @Slot()
    def show_cancelled(self) -> None:
        """The pipeline confirmed the cancel; return to the output page."""
        self._failed = False
        self._stack.setCurrentIndex(PAGE_OUTPUT)
        self._refresh_output()

    # -- page 4: preview -------------------------------------------------------------------
    def _build_preview(self) -> None:
        box = self._add_page("4. Preview")
        self.preview_text = _label()
        self.preview_reason = _label()
        self.preview_start = self._primary("Start copying")
        self.preview_start.clicked.connect(self._start_copy)
        back = QPushButton("Back")
        back.clicked.connect(lambda: self._stack.setCurrentIndex(PAGE_OUTPUT))
        box.addWidget(self.preview_text)
        box.addWidget(self.preview_reason)
        box.addStretch(1)
        box.addLayout(self._buttons(back, self.preview_start))

    @Slot(object)
    def show_preview(self, counts: PreviewCounts) -> None:
        c = counts
        lines = [
            f"Photos to copy: {c.photos}",
            f"Other files to copy: {c.other_files}",
            f"JSON files to keep: {c.json_files}",
            f"Duplicates skipped: {c.duplicates} ({format_bytes(c.duplicate_bytes)} saved)",
            f"Trash files skipped: {c.trash_skipped}",
            f"Sidecars without a photo: {c.unmatched_sidecars}",
            f"Albums found: {c.albums}",
            f"Size to copy: {format_bytes(c.bytes_to_copy)}",
            f"Free space: {format_bytes(c.free_bytes)}",
        ]
        self.preview_text.setText("\n".join(lines))
        enough = c.bytes_to_copy <= c.free_bytes
        self.preview_start.setEnabled(enough)
        self.preview_reason.setText(
            ""
            if enough
            else f"Not enough free space: {format_bytes(c.bytes_to_copy - c.free_bytes)} more "
            "is needed. Free some space or pick another output folder."
        )
        self._stack.setCurrentIndex(PAGE_PREVIEW)

    def _start_copy(self) -> None:
        self.copy_status.setText("Starting…")
        self.copy_bar.setRange(0, 0)
        self.copy_cancel.setText("Cancel")
        self._failed = False
        self._stack.setCurrentIndex(PAGE_COPY)
        self.copy_requested.emit()

    # -- page 6: done ----------------------------------------------------------------------
    def _build_done(self) -> None:
        box = self._add_page("6. Done")
        self.done_summary = _label()
        self.done_details = _label()
        self.swap_label = _label(
            "The Takeout folder is already a source in your library. Replace it with the "
            "cleaned copy? Only the library entry is removed; no files are touched."
        )
        self.swap_button = QPushButton("Replace old source…")
        self.swap_label.hide()
        self.swap_button.hide()
        self.swap_button.clicked.connect(self.swap_requested.emit)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        self.open_report_button = QPushButton("Open report")
        self.open_report_button.clicked.connect(self._open_report)
        self.add_button = self._primary("Add to library")
        self.add_button.clicked.connect(self._add_to_library)
        box.addWidget(self.done_summary)
        box.addWidget(self.done_details)
        box.addWidget(self.swap_label)
        box.addWidget(self.swap_button)
        box.addStretch(1)
        box.addLayout(
            self._buttons(self.open_folder_button, self.open_report_button, self.add_button)
        )

    @Slot(object)
    def show_done(self, result: DoneResult) -> None:
        self._done = result
        details: list[str] = []
        if result.safe_to_delete:
            self.done_summary.setText(safe_to_delete_message(result.copied))
        else:
            self.done_summary.setText(
                f"{result.copied} files copied, but some could not be verified. "
                "Keep the original Takeout folder."
            )
            for title, items in (
                ("Failed to copy", result.failed),
                ("Not accounted for", result.unaccounted),
            ):
                if items:
                    shown = "\n".join(items[:MAX_LISTED])
                    more = len(items) - MAX_LISTED
                    extra = f"\n…and {more} more (see the report)" if more > 0 else ""
                    details.append(f"{title} ({len(items)}):\n{shown}{extra}")
        self.done_details.setText("\n\n".join(details))
        self.add_button.setEnabled(True)
        self.swap_label.hide()
        self.swap_button.hide()
        self._stack.setCurrentIndex(PAGE_DONE)

    def _open_folder(self) -> None:
        if self._done:
            self.open_folder_requested.emit(self._done.output_path)

    def _open_report(self) -> None:
        if self._done:
            self.open_report_requested.emit(self._done.report_path)

    def _add_to_library(self) -> None:
        self.add_button.setEnabled(False)
        self.add_to_library_requested.emit()
        if self._done and self._done.input_is_source:
            self.swap_label.show()
            self.swap_button.show()

    # -- closing ---------------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.working:
            if self._confirm_cancel(self):
                self.cancel_requested.emit()
                event.accept()
            else:
                event.ignore()
            return
        event.accept()
