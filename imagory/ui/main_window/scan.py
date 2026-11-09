"""Scanning from the main window: starting, progress, cancelling, and the summary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from imagory import db
from imagory.scanner import Progress, ScanSummary
from imagory.ui.scan_thread import ScanThread, SourceScanResult

if TYPE_CHECKING:
    from imagory.ui.main_window.window import MainWindow


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


class ScanController:
    """Runs one scan thread at a time over the chosen sources."""

    def __init__(self, window: MainWindow) -> None:
        self.window = window
        self.thread: ScanThread | None = None
        self._prefix = ""  # names the source being scanned when there are several

    def stop(self) -> None:
        """Cancel a running scan and wait for it (the window is closing)."""
        if self.thread is not None:
            self.window.status_label.setText("Stopping scan…")
            self.thread.cancel()
            self.thread.wait()  # in-flight results are committed before it exits

    def rescan(self) -> None:
        self.rescan_sources(self.window.sources)

    def rescan_sources(self, to_scan: list[db.Source]) -> None:
        if to_scan and not self.window.busy:
            self.start(to_scan)

    def start(self, to_scan: list[db.Source]) -> None:
        assert to_scan and self.thread is None
        thread = ScanThread(
            self.window.db_file,
            to_scan,
            self.window.thumbs_root,
            self.window.library_root,
            self.window,
        )
        thread.source_started.connect(self._on_source_started)
        thread.progressed.connect(self._on_progress)
        thread.added.connect(self.window._on_scan_added)
        thread.succeeded.connect(self._on_succeeded)
        thread.finished.connect(self._on_thread_finished)
        self.thread = thread
        self._prefix = ""
        self.window.update_actions()
        self.window.scan_state_changed.emit()
        self.window.progress.setRange(0, 0)
        self.window.progress.setVisible(True)
        self.window.cancel_button.setEnabled(True)
        self.window.cancel_button.setVisible(True)
        self.window.status_label.setText("Looking for photos…")
        self.window.update_empty_text()
        thread.start()

    def cancel(self) -> None:
        if self.thread is not None:
            self.thread.cancel()
            self.window.cancel_button.setEnabled(False)
            self.window.status_label.setText("Stopping after the current files…")

    def _on_source_started(self, source: db.Source, position: int, total: int) -> None:
        self._prefix = f"{source.label} ({position} of {total}): " if total > 1 else ""

    def _on_progress(self, progress: Progress) -> None:
        if self.thread is None or self.thread.cancel_requested:
            return
        if progress.phase == "walking":
            self.window.progress.setRange(0, 0)
            self.window.status_label.setText(f"{self._prefix}Looking for photos…")
        elif progress.phase == "processing":
            self.window.progress.setRange(0, max(1, progress.to_process))
            self.window.progress.setValue(progress.processed)
            self.window.status_label.setText(
                f"{self._prefix}Processing {progress.processed:,} of "
                f"{progress.to_process:,} new or changed files ({progress.found:,} found)"
            )

    def _on_succeeded(self, results: list[SourceScanResult]) -> None:
        if self.window.closing:
            return
        self.window.sources_updated()  # last scan times
        self.window.reload_grid()
        if not results:  # cancelled before the first source started
            QMessageBox.information(self.window, "Scan cancelled", "Nothing was scanned.")
            return
        text = results_text(results)
        if any(r.summary is None for r in results):
            QMessageBox.warning(
                self.window,
                "Scan finished with problems",
                text + "\n\nDetails are in the log (Help > Open log folder).",
            )
            return
        cancelled = self.thread is not None and self.thread.cancel_requested
        cancelled = cancelled or any(r.summary.status == "cancelled" for r in results)
        title = "Scan cancelled" if cancelled else "Scan finished"
        note = "\n\nRescan to pick up where it stopped." if cancelled else ""
        QMessageBox.information(self.window, title, text + note)

    def _on_thread_finished(self) -> None:
        scanned = list(self.thread.sources) if self.thread is not None else []
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = None
        self.window.progress.setVisible(False)
        self.window.cancel_button.setVisible(False)
        self.window.update_actions()
        self.window.scan_state_changed.emit()
        self.window.update_empty_text()
        self.window.show_photo_count()
        self.window.refresh_sidebar()
        self.window.cleanup.after_scan(scanned)
