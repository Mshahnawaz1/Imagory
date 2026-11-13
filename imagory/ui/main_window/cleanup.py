"""Takeout cleanup from the main window: the wizard, its jobs, Add to library, and the swap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from imagory import db, paths, sources
from imagory.cleanup import checks as cleanup_checks
from imagory.cleanup.types import DoneResult
from imagory.ui.cleanup_thread import CleanupThread
from imagory.ui.cleanup_wizard import CleanupWizard
from imagory.ui.info_panel import show_in_explorer
from imagory.ui.main_window.source_actions import emptied_albums_note

if TYPE_CHECKING:
    from imagory.ui.main_window.window import MainWindow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanedAdd:
    """A cleanup output being added to the library: scanned first, then its albums are
    imported and the swap offered."""

    source_id: int
    output: Path
    run_id: int | None  # None: import every finished run (plain Add source)
    input: Path | None  # the cleanup's input, for the swap offer


class CleanupController:
    """Runs the cleanup wizard's jobs on a thread and adds finished outputs to the library."""

    def __init__(self, window: MainWindow) -> None:
        self.window = window
        self.thread: CleanupThread | None = None
        self.wizard: CleanupWizard | None = None
        # Extra keyword arguments for the cleanup pipelines (workers, pool_threshold); tests set it.
        self.options: dict = {}
        self._input: Path | None = None
        self._output: Path | None = None
        self._run_id: int | None = None
        self._pending_add: CleanedAdd | None = None
        # (job, input, output, wizard): a job asked for while the last one is ending
        self._queued: tuple | None = None
        self._swap_context: CleanedAdd | None = None
        # (Takeout folder, label) from Add source's "Clean up first": a finished run of that
        # input is added to the library straight away, under that label.
        self._from_add_source: tuple[Path, str] | None = None
        self._auto_add: CleanupWizard | None = None  # added once its job's thread has ended

    def stop(self) -> None:
        """Cancel a running cleanup job and wait for it (the window is closing)."""
        self._queued = None
        if self.thread is not None:
            self.window.status_label.setText("Stopping cleanup…")
            self.thread.cancel()
            self.thread.wait()

    def after_scan(self, scanned: list[db.Source]) -> None:
        """After every scan: finish a pending Add to library, then import the albums of
        cleaned sources that were scanned to completion."""
        pending, self._pending_add = self._pending_add, None
        if self.window.closing:
            return
        if pending is not None:
            self._after_cleaned_scan(pending)
        self._import_pending_albums(scanned)

    def open_wizard(self) -> None:
        """Open the cleanup wizard, or raise the one that is already open."""
        if self.wizard is not None:
            self.wizard.show()
            self.wizard.raise_()
            self.wizard.activateWindow()
            return
        if self.window.scanning:
            return
        wizard = CleanupWizard(
            cleanup_checks.check_input,
            # The check runs on the main thread, on the sources it already holds.
            lambda inp, out: cleanup_checks.check_folders_for(
                self.window.sources, self.window.library_root, inp, out
            ),
            self.window,
        )
        wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        wizard.destroyed.connect(self._on_wizard_destroyed)
        wizard.analyse_requested.connect(self._start_analysis)
        wizard.copy_requested.connect(self._start_copy)
        wizard.cancel_requested.connect(self.cancel)
        wizard.add_to_library_requested.connect(self._add_to_library)
        wizard.swap_requested.connect(self._swap_requested)
        wizard.open_folder_requested.connect(self._open_folder)
        wizard.open_report_requested.connect(show_in_explorer)
        self.wizard = wizard
        wizard.show()

    def clean_up_and_add(self, folder: Path, label: str) -> None:
        """Add source's "Clean up first" for a Takeout folder: open the wizard with ``folder``
        as input, on the output page. When the run is done, the output is added to the
        library as a ``cleaned`` source called ``label``, so its albums come in."""
        if self.wizard is not None and (self.window.cleaning or self.wizard.working):
            QMessageBox.information(
                self.window,
                "Clean up Takeout",
                "A cleanup is already running. Let it finish, then add this folder again.",
            )
            self.open_wizard()
            return
        self.open_wizard()
        if self.wizard is None:  # a scan is running
            return
        self._from_add_source = (folder, label)
        self.wizard.start_with_input(folder)

    def _added_from_source(self, input_root: Path | None) -> tuple[Path, str] | None:
        """The Add source request this run answers, if its input is that folder."""
        pending = self._from_add_source
        if pending is None or input_root is None or Path(input_root) != pending[0]:
            return None
        return pending

    @staticmethod
    def _open_folder(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_wizard_destroyed(self, *_args) -> None:
        self.wizard = None
        self._from_add_source = None

    def _start_thread(
        self, job, input_root: Path, output_root: Path, wizard: CleanupWizard | None
    ) -> None:
        if (
            self.window.cleaning
        ):  # the previous job is just ending (its result is out, the thread isn't)
            self._queued = (job, input_root, output_root, wizard)
            return
        if self.window.scanning:
            if wizard is not None and shiboken6.isValid(wizard):
                wizard.show_error("Wait for the scan to finish, then try again.")
            return
        if wizard is None or not shiboken6.isValid(wizard):
            return  # its wizard was closed; nobody is waiting for this job
        # The sources were read on the main thread; so is input_is_source.
        thread = CleanupThread(
            job,
            input_root,
            output_root,
            self.window.library_root,
            list(self.window.sources),
            cleanup_checks.input_is_source(self.window.conn, input_root),
            self.options,
            self.window,
        )
        # Results go only to the wizard that started this job, and only while it exists.
        thread.progressed.connect(lambda *a: self._to_wizard(wizard, "set_progress", *a))
        thread.preview_ready.connect(lambda c: self._to_wizard(wizard, "show_preview", c))
        thread.cancelled.connect(lambda: self._to_wizard(wizard, "show_cancelled"))
        thread.failed.connect(lambda m: self._to_wizard(wizard, "show_error", m))
        thread.finished_ok.connect(
            lambda result, run_id: self._on_done(wizard, input_root, output_root, result, run_id)
        )
        thread.finished.connect(self._on_thread_finished)
        self.thread = thread
        self.window.update_actions()
        self.window.scan_state_changed.emit()
        thread.start()

    @staticmethod
    def _to_wizard(wizard: CleanupWizard, slot: str, *args) -> None:
        if shiboken6.isValid(wizard):
            getattr(wizard, slot)(*args)

    def _start_analysis(self, input_root: Path, output_root: Path) -> None:
        self._input, self._output = input_root, output_root
        self._run_id = None
        self._swap_context = None
        self._start_thread("analysis", input_root, output_root, self.wizard)

    def _start_copy(self) -> None:
        if self._input is not None and self._output is not None:
            self._start_thread("copy", self._input, self._output, self.wizard)

    def cancel(self) -> None:
        self._queued = None
        if self.thread is not None:
            self.thread.cancel()

    def _on_done(
        self,
        wizard: CleanupWizard,
        input_root: Path,
        output_root: Path,
        result: DoneResult,
        run_id: int,
    ) -> None:
        if not shiboken6.isValid(wizard) or wizard is not self.wizard:
            return  # that wizard is gone; Add to library has nothing to offer
        self._input, self._output = input_root, output_root
        self._run_id = run_id
        wizard.show_done(result)
        if self._added_from_source(input_root) is not None:
            self._auto_add = wizard  # the user already asked for it in Add source

    def _on_thread_finished(self) -> None:
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = None
        queued, self._queued = self._queued, None
        if queued is not None and not self.window.closing:
            job, input_root, output_root, wizard = queued
            if wizard is not None and shiboken6.isValid(wizard):
                self._start_thread(job, input_root, output_root, wizard)
        self.window.update_actions()
        self.window.scan_state_changed.emit()
        auto_add, self._auto_add = self._auto_add, None
        if (
            auto_add is not None
            and shiboken6.isValid(auto_add)
            and auto_add is self.wizard
            and self.thread is None
            and not self.window.closing
        ):
            auto_add.add_to_library()

    def _add_to_library(self) -> None:
        """Add to library: the output becomes a ``cleaned`` source and is scanned; then its
        albums are imported and the swap offered."""
        output, run_id = self._output, self._run_id
        if output is None or run_id is None:
            return
        if self.window.busy:
            self._refuse_add("Wait for the current scan or cleanup to finish, then try again.")
            return
        existing = sources.source_for_folder(self.window.conn, output)
        if (
            existing is None or existing.type != "cleaned"
        ) and not self.window.source_actions.check_folder(output):
            self._refuse_add(None)  # the usual overlap warning was shown or declined
        else:
            pending = self._added_from_source(self._input)
            self._from_add_source = None
            label = pending[1] if pending is not None else None
            if self.add_cleaned_output(output, run_id, self._input, label) is None:
                self._refuse_add(None)

    def _refuse_add(self, message: str | None) -> None:
        if message:
            QMessageBox.information(self.window, "Add to library", message)
        self._swap_context = None
        wizard = self.wizard
        if wizard is not None:
            wizard.add_button.setEnabled(True)
            wizard.swap_button.hide()
            wizard.swap_label.hide()

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
            source = sources.add_cleaned_source(self.window.conn, output, label)
        except ValueError as exc:
            QMessageBox.warning(self.window, "Add source", str(exc))
            return None
        self.window.sources_updated()
        self.window.reload_grid()
        add = CleanedAdd(source.id, output, run_id, input_root)
        self._pending_add = add
        if run_id is not None and input_root is not None:
            self._swap_context = add
        self.window.scan.start([self.window.sources_by_id[source.id]])
        return source

    def _after_cleaned_scan(self, add: CleanedAdd) -> None:
        last = db.last_scan(self.window.conn, add.source_id)
        if last is None or last.status != "completed":
            return  # the scan said why
        output_db = paths.cleanup_db_path(add.output)
        try:
            sources.import_run_albums(self.window.conn, output_db, add.run_id, add.source_id)
        except (ValueError, db.OutputDatabaseError) as exc:
            log.warning("Album import for %s failed: %s", add.output, exc)
            QMessageBox.warning(self.window, "Albums", f"The albums could not be created:\n{exc}")
        self.window.refresh_sidebar()
        self.window.fill_albums_menu()
        self.window.reload_grid()
        if add.run_id is not None and add.input is not None:
            self._offer_swap(add, quiet=True)

    def _import_pending_albums(self, scanned: list[db.Source]) -> None:
        """After a scan: import albums of every finished run of each cleaned source that was
        scanned to completion. Idempotent, so albums a cancelled scan missed arrive with the
        next completed one. One bad output database never blocks the others."""
        imported = False
        for source in scanned:
            current = self.window.sources_by_id.get(source.id)
            if current is None or current.type != "cleaned":
                continue
            last = db.last_scan(self.window.conn, current.id)
            if last is None or last.status != "completed":
                continue
            try:
                done = sources.import_run_albums(
                    self.window.conn,
                    paths.cleanup_db_path(Path(current.root_path)),
                    None,
                    current.id,
                )
            except Exception as exc:  # a bad or missing output database
                log.warning("Album import for %s failed: %s", current.root_path, exc)
                continue
            imported = imported or bool(done)
        if imported:
            self.window.refresh_sidebar()
            self.window.fill_albums_menu()

    def _swap_requested(self) -> None:
        if self.window.busy:
            QMessageBox.information(
                self.window,
                "Replace old source",
                "Wait for the current scan or cleanup to finish first.",
            )
        elif self._swap_context is not None:
            self._offer_swap(self._swap_context, quiet=False)

    def _offer_swap(self, add: CleanedAdd, *, quiet: bool) -> None:
        """Offer to replace the old source by the cleaned one. Only database rows go."""
        assert add.run_id is not None and add.input is not None
        output_db = paths.cleanup_db_path(add.output)
        old = sources.source_for_folder(self.window.conn, add.input)
        if old is None or old.type == "cleaned":
            if not quiet:
                QMessageBox.information(
                    self.window,
                    "Replace old source",
                    "The old source is no longer in your library.",
                )
            return
        if not sources.swap_ready(self.window.conn, add.source_id, output_db, add.run_id):
            if not quiet:
                QMessageBox.information(
                    self.window,
                    "Replace old source",
                    "The cleaned folder hasn't been fully scanned yet. Wait for the scan to "
                    "finish, then try again.",
                )
            return
        orphans = sources.swap_orphan_count(self.window.conn, old.id)
        note = ""
        if orphans:
            note = (
                f"\n\n{orphans:,} photo(s) only exist in the old source (for example Trash "
                "files) and will no longer be listed."
            )
        note += emptied_albums_note(self.window.conn, old.id)
        answer = QMessageBox.question(
            self.window,
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
        assert not self.window.busy and add.run_id is not None
        self.window.release_grid()
        try:
            sources.swap_source(
                self.window.conn,
                self.window.thumbs_root,
                old_id,
                add.source_id,
                output_db,
                add.run_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self.window, "Replace old source", str(exc))
            swapped = False
        else:
            swapped = True
        self.window.sync_current_album()
        self.window.sources_updated()
        self.window.reload_grid()
        self.window.refresh_sidebar()
        self.window.fill_albums_menu()
        if swapped and self.wizard is not None:
            self.wizard.swap_button.hide()
            self.wizard.swap_label.hide()
