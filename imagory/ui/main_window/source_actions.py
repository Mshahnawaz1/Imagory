"""Source actions of the main window: add, rename, change type, relocate, and remove."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox

from imagory import db, sources
from imagory.ui.sources_dialog import TYPE_NAMES, AddSourceDialog, default_label

if TYPE_CHECKING:
    from imagory.ui.main_window.window import MainWindow

log = logging.getLogger(__name__)

_NAMES_SHOWN = 5  # album names listed in a confirmation before "and N more"


def emptied_albums_note(conn: sqlite3.Connection, source_id: int) -> str:
    """A confirmation paragraph naming the albums that removing the source deletes because
    none of their photos would be left, or "" when there are none."""
    names = [name for _id, name in db.source_emptied_albums(conn, source_id)]
    if not names:
        return ""
    shown = ", ".join(f"“{n}”" for n in names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        shown += f" and {len(names) - _NAMES_SHOWN:,} more"
    noun = "album" if len(names) == 1 else f"{len(names):,} albums"
    return f"\n\nThe {noun} {shown} would be left empty and will be deleted too."


class SourceActions:
    """Runs the source actions for the main window, asking the user where needed."""

    def __init__(self, window: MainWindow) -> None:
        self.window = window

    def source_stats(self) -> dict[int, db.SourceStats]:
        return db.source_stats(self.window.conn)

    def _pick_folder(self, title: str, start: str | None = None) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self.window, title, start or str(Path.home()))
        return Path(chosen).resolve() if chosen else None

    def check_folder(self, folder: Path, ignore_id: int | None = None) -> bool:
        """Tell the user why ``folder`` cannot be used, or ask about a warning."""
        check = sources.check_folder(
            folder, self.window.sources, self.window.library_root, ignore_id
        )
        if check.error:
            QMessageBox.warning(self.window, "Imagory", f"{folder}\n\n{check.error}")
            return False
        if check.warning:
            answer = QMessageBox.question(
                self.window,
                "Imagory",
                f"{folder}\n\n{check.warning}\n\nUse this folder anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return answer == QMessageBox.StandardButton.Yes
        return True

    def add_source_interactive(self) -> None:
        if self.window.busy:
            return
        folder = self._pick_folder("Add a photo folder or Takeout export")
        if folder is None or not self.check_folder(folder):
            return
        detection = sources.detect_source_type(folder)
        dialog = AddSourceDialog(folder, detection, self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.clean_up_first:
                self.window.cleanup.clean_up_and_add(folder, dialog.label)
            else:
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
            return self.window.cleanup.add_cleaned_output(folder, None, None, label)
        source = sources.add_source(
            self.window.conn,
            self.window.thumbs_root,
            folder,
            label or default_label(folder),
            source_type or detected_type,
            detected_type,
        )
        self.window.sources_updated()
        self.window.reload_grid()
        self.window.scan.start([source])
        return source

    def rename_source_interactive(self, source: db.Source) -> None:
        label, ok = QInputDialog.getText(self.window, "Rename source", "Label:", text=source.label)
        if ok and label.strip():
            self.rename_source(source.id, label.strip())

    def rename_source(self, source_id: int, label: str) -> None:
        db.rename_source(self.window.conn, source_id, label)
        self.window.sources_updated()
        self.window.refresh_viewer()  # the source label it shows

    def change_type_interactive(self, source: db.Source) -> None:
        names = [TYPE_NAMES[t] for t in db.SOURCE_TYPES]
        chosen, ok = QInputDialog.getItem(
            self.window,
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
                self.window,
                "Change source type",
                f"The folder of “{source.label}” is not available:\n{source.root_path}\n\n"
                "Imagory needs it to read the Takeout information. Connect the drive, or "
                "relocate the source, and try again. Nothing was changed.",
            )
            return
        QMessageBox.information(
            self.window,
            "Change source type",
            f"“{source.label}” is now {TYPE_NAMES[source_type]}. "
            f"Dates changed for {changed:,} file{'' if changed == 1 else 's'}.",
        )

    def change_source_type(self, source_id: int, source_type: str) -> int:
        """Set the type and recompute the source's dates (PRD §8.1). Returns how many files'
        dates changed. Raises ``FileNotFoundError`` when a Takeout source's folder is gone,
        and then nothing changes."""
        assert not self.window.busy  # the scan thread is the only writer while it runs
        changed = sources.change_source_type(self.window.conn, source_id, source_type)
        self.window.sources_updated()
        if changed:
            # Dates decide the order and the month headers; an open viewer stays on its
            # photo and is rebound to the reloaded list.
            self.window.reload_grid()
        return changed

    def relocate_source_interactive(self, source: db.Source) -> None:
        if self.window.busy:
            return
        parent = str(Path(source.root_path).parent)
        folder = self._pick_folder(f"New location of “{source.label}”", parent)
        if folder is None or not self.check_folder(folder, ignore_id=source.id):
            return
        found, checked = sources.relocation_matches(self.window.conn, source.id, folder)
        if checked and not found:
            answer = QMessageBox.question(
                self.window,
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
        db.relocate_source(self.window.conn, source_id, str(folder))
        self.window.sources_updated()
        self.window.reload_grid()
        self.window.scan.start([self.window.sources_by_id[source_id]])

    def remove_source_interactive(self, source: db.Source) -> None:
        if self.window.busy:
            return
        stats = self.source_stats().get(source.id, db.SourceStats())
        answer = QMessageBox.question(
            self.window,
            "Remove source",
            f"Remove “{source.label}” from Imagory?\n\n{source.root_path}\n\n"
            f"Imagory will forget its {stats.photos + stats.missing:,} photos and delete their "
            f"thumbnails.{emptied_albums_note(self.window.conn, source.id)}\n\n"
            "Your photos on disk are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.remove_source(source.id)

    def remove_source(self, source_id: int) -> None:
        """Delete the source's rows and thumbnails. Only after the user confirms."""
        assert not self.window.busy
        self.window.release_grid()
        sources.remove_source(self.window.conn, self.window.thumbs_root, source_id)
        self.window.sync_current_album()  # it may have been deleted with the source
        self.window.sources_updated()
        self.window.reload_grid()

    def remove_missing_items(self, source_id: int) -> db.RemovedMissing:
        """Forget the source's missing files and photos left without files. Only after the
        user confirms (in the sources dialog)."""
        assert not self.window.busy
        removed = sources.remove_missing(self.window.conn, self.window.thumbs_root, source_id)
        self.window.sources_updated()
        self.window.reload_grid()
        return removed
