"""Album dialogs (PRD §8.8): manage albums and add photos to one.

The dialogs work on a database connection and photo ids only. They never touch photo files.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from imagory import db

_ID_ROLE = Qt.ItemDataRole.UserRole


def _plural(n: int) -> str:
    return f"{n} photo" if n == 1 else f"{n} photos"


def _label(album: db.Album) -> str:
    return f"{album.name}  ({_plural(album.photo_count)})"


def _ask_name(parent: QWidget, title: str, text: str = "") -> str | None:
    name, ok = QInputDialog.getText(parent, title, "Album name:", text=text)
    return name.strip() if ok and name.strip() else None


class AlbumsDialog(QDialog):
    """List albums with photo counts; create, rename, and delete them."""

    def __init__(self, conn: sqlite3.Connection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Albums")
        self.resize(420, 360)

        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.rename_selected())
        self.empty_label = QLabel("No albums yet.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.new_button = QPushButton("New...")
        self.rename_button = QPushButton("Rename...")
        self.delete_button = QPushButton("Delete...")
        self.new_button.clicked.connect(self.create_album)
        self.rename_button.clicked.connect(self.rename_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.list)
        layout.addLayout(buttons)
        layout.addWidget(close)
        self.refresh()

    def refresh(self, select_id: int | None = None) -> None:
        self.list.clear()
        albums = db.list_albums(self.conn)
        for album in albums:
            item = QListWidgetItem(_label(album))
            item.setData(_ID_ROLE, album.id)
            self.list.addItem(item)
            if album.id == select_id:
                self.list.setCurrentItem(item)
        self.empty_label.setVisible(not albums)
        self.list.setVisible(bool(albums))
        self._update_buttons()

    def selected_album(self) -> db.Album | None:
        item = self.list.currentItem()
        if item is None:
            return None
        album_id = item.data(_ID_ROLE)
        return next((a for a in db.list_albums(self.conn) if a.id == album_id), None)

    def _update_buttons(self) -> None:
        has = self.list.currentItem() is not None
        self.rename_button.setEnabled(has)
        self.delete_button.setEnabled(has)

    def create_album(self) -> None:
        name = _ask_name(self, "New album")
        if name is None:
            return
        try:
            album_id = db.create_album(self.conn, name)
        except db.AlbumNameError as exc:
            QMessageBox.warning(self, "New album", str(exc))
            return
        self.refresh(select_id=album_id)

    def rename_selected(self) -> None:
        album = self.selected_album()
        if album is None:
            return
        name = _ask_name(self, "Rename album", album.name)
        if name is None or name == album.name:
            return
        try:
            db.rename_album(self.conn, album.id, name)
        except db.AlbumNameError as exc:
            QMessageBox.warning(self, "Rename album", str(exc))
            return
        self.refresh(select_id=album.id)

    def delete_selected(self) -> None:
        album = self.selected_album()
        if album is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete album",
            f"Delete the album '{album.name}'?\n\n"
            "Only the album is removed. Your photos are not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        db.delete_album(self.conn, album.id)
        self.refresh()


class AddToAlbumDialog(QDialog):
    """Pick an existing album, or create one, and add the given photos to it.

    After the dialog is accepted, ``album_id``, ``added`` and ``already_present`` hold the
    result.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        photo_ids: Sequence[int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.conn = conn
        self.photo_ids = list(dict.fromkeys(photo_ids))
        self.album_id: int | None = None
        self.added = 0
        self.already_present = 0
        self.setWindowTitle("Add to album")
        self.resize(380, 340)

        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.new_button = QPushButton("New album...")
        self.new_button.clicked.connect(self.create_album)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Add")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Add {_plural(len(self.photo_ids))} to:"))
        layout.addWidget(self.list)
        layout.addWidget(self.new_button)
        layout.addWidget(self.buttons)
        self.refresh()

    def refresh(self, select_id: int | None = None) -> None:
        self.list.clear()
        for album in db.list_albums(self.conn):
            item = QListWidgetItem(_label(album))
            item.setData(_ID_ROLE, album.id)
            self.list.addItem(item)
            if album.id == select_id:
                self.list.setCurrentItem(item)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.ok_button.setEnabled(bool(self.photo_ids) and self.list.currentItem() is not None)

    def create_album(self) -> None:
        name = _ask_name(self, "New album")
        if name is None:
            return
        try:
            album_id = db.create_album(self.conn, name)
        except db.AlbumNameError as exc:
            QMessageBox.warning(self, "New album", str(exc))
            return
        self.refresh(select_id=album_id)

    def accept(self) -> None:
        item = self.list.currentItem()
        if item is None or not self.photo_ids:
            return
        self.album_id = item.data(_ID_ROLE)
        self.added = db.add_photos_to_album(self.conn, self.album_id, self.photo_ids)
        self.already_present = len(self.photo_ids) - self.added
        text = f"Added {_plural(self.added)} to '{item.text().rsplit('  (', 1)[0]}'."
        if self.already_present:
            text += f"\n{_plural(self.already_present)} already in the album."
        QMessageBox.information(self, "Add to album", text)
        super().accept()
