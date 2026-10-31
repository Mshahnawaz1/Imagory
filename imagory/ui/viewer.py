"""Photo viewer: fit to window, EXIF orientation applied, Left/Right to move.

Back (button, Esc, Backspace, or the mouse back button) returns to the grid.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from imagory.db import GridPhoto, Source
from imagory.ui.grid import display_name, format_date
from imagory.ui.images import load_qimage

log = logging.getLogger(__name__)


class _Signals(QObject):
    loaded = Signal(int, QImage, str)  # request token, image, error message


class _LoadTask(QRunnable):
    def __init__(self, signals: _Signals, token: int, path: Path, max_size: tuple[int, int]):
        super().__init__()
        self.signals, self.token, self.path, self.max_size = signals, token, path, max_size

    def run(self) -> None:
        try:
            image = load_qimage(self.path, self.max_size)
        except Exception as exc:
            log.warning("Viewer could not open %s: %s", self.path, exc)
            self.signals.loaded.emit(self.token, QImage(), str(exc))
        else:
            self.signals.loaded.emit(self.token, image, "")


class ImageCanvas(QWidget):
    """Paints one image scaled to fit, centred, never enlarged beyond its own size."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image = QImage()
        self._scaled: QPixmap | None = None
        self.message = ""

    def set_image(self, image: QImage, message: str = "") -> None:
        self._image = image
        self._scaled = None
        self.message = message
        self.update()

    def resizeEvent(self, event) -> None:
        self._scaled = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        if self._image.isNull():
            if self.message:
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        dpr = self.devicePixelRatioF()
        if self._scaled is None:
            target = QSize(round(self.width() * dpr), round(self.height() * dpr))
            size = self._image.size()
            if size.width() > target.width() or size.height() > target.height():
                size = size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
            scaled = self._image.scaled(
                size,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled = QPixmap.fromImage(scaled)
            self._scaled.setDevicePixelRatio(dpr)
        w = self._scaled.width() / dpr
        h = self._scaled.height() / dpr
        rect = QRect(
            round((self.width() - w) / 2), round((self.height() - h) / 2), round(w), round(h)
        )
        painter.drawPixmap(rect, self._scaled)


class PhotoViewer(QWidget):
    closed = Signal(int)  # row that was showing

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas = ImageCanvas(self)
        self.back_button = QPushButton("\u2190 Back to photos", self)
        self.back_button.setToolTip("Back to the grid (Esc)")
        self.back_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep arrow keys here
        self.back_button.clicked.connect(self.close_viewer)
        self.status = QLabel(self)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top = QHBoxLayout()
        top.setContentsMargins(8, 4, 8, 4)
        top.addWidget(self.back_button)
        top.addWidget(self.status, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.canvas, 1)
        self._photos: list[GridPhoto] = []
        self._sources: dict[int, Source] = {}
        self._row = -1
        self._token = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _Signals()
        self._signals.loaded.connect(self._on_loaded)

    @property
    def row(self) -> int:
        return self._row

    @property
    def photos(self) -> list[GridPhoto]:
        return self._photos

    def current_photo_id(self) -> int | None:
        return self._photos[self._row].photo_id if 0 <= self._row < len(self._photos) else None

    def rebind(self, photos: list[GridPhoto], row: int) -> None:
        """Point at a reloaded grid list, where the photo showing is now at ``row``."""
        self._photos = photos
        self._row = row
        self._update_status(photos[row])

    def close_viewer(self) -> None:
        self.closed.emit(self._row)

    def rows_inserted(self, first: int, count: int) -> None:
        """The grid list grew (a scan is running): keep showing the same photo."""
        if not self._photos or self._row < 0:
            return
        if first <= self._row:
            self._row += count
        self._update_status(self._photos[self._row])

    def show_photo(self, photos: list[GridPhoto], sources: dict[int, Source], row: int) -> None:
        self._photos = photos
        self._sources = sources
        self._go(row)
        self.setFocus()

    def _go(self, row: int) -> None:
        if not self._photos:
            return
        self._row = max(0, min(row, len(self._photos) - 1))
        photo = self._photos[self._row]
        self._update_status(photo)
        self._token += 1
        self._pool.clear()
        source = self._sources.get(photo.source_id)
        if source is None:
            self.canvas.set_image(QImage(), "This photo's source was removed.")
            return
        path = Path(source.root_path).joinpath(*photo.relative_path.split("/"))
        screen = (
            self.screen().size() * self.devicePixelRatioF() if self.screen() else QSize(4096, 4096)
        )
        self.canvas.set_image(QImage(), "Loading…")
        self._pool.start(
            _LoadTask(self._signals, self._token, path, (screen.width(), screen.height()))
        )

    def _update_status(self, photo: GridPhoto) -> None:
        dims = f"{photo.width} × {photo.height}" if photo.width else "unknown size"
        source = self._sources.get(photo.source_id)
        label = source.label if source else "removed source"
        self.status.setText(
            f"{display_name(photo)}  ·  {dims}  ·  {format_date(photo.taken_at_local)} "
            f"({photo.date_source or 'unknown'})  ·  {label}  ·  "
            f"{self._row + 1} of {len(self._photos)}"
        )
        self.status.setToolTip(photo.relative_path)

    def _on_loaded(self, token: int, image: QImage, error: str) -> None:
        if token != self._token:
            return  # the user already moved on
        if image.isNull():
            self.canvas.set_image(QImage(), f"Cannot open this photo.\n{error}")
        else:
            self.canvas.set_image(image)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace, Qt.Key.Key_Back):
            self.close_viewer()
        elif key == Qt.Key.Key_Right:
            if self._row < len(self._photos) - 1:
                self._go(self._row + 1)
        elif key == Qt.Key.Key_Left:
            if self._row > 0:
                self._go(self._row - 1)
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self.close_viewer()
        else:
            super().mouseReleaseEvent(event)
