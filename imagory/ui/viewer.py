"""Photo viewer: fit to window, zoom and pan, info panel, Left/Right to move.

Zoom with the wheel, double-click or +/-; drag to pan; 0 returns to fit; I toggles the
info panel. EXIF orientation is applied. The viewer only ever reads photo files.

Back (button, Esc, Backspace, or the mouse back button) returns to the grid.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from imagory import db
from imagory.db import GridPhoto, Source
from imagory.ui.grid import display_name, format_date
from imagory.ui.images import load_qimage
from imagory.ui.info_panel import InfoPanel, full_path, show_in_explorer

log = logging.getLogger(__name__)


class _Signals(QObject):
    loaded = Signal(int, QImage, str, bool)  # request token, image, error message, full size


class _LoadTask(QRunnable):
    def __init__(self, signals: _Signals, token: int, path: Path, max_size: tuple[int, int] | None):
        super().__init__()
        self.signals, self.token, self.path, self.max_size = signals, token, path, max_size

    def run(self) -> None:
        try:
            image = load_qimage(self.path, self.max_size)
        except Exception as exc:
            log.warning("Viewer could not open %s: %s", self.path, exc)
            self.signals.loaded.emit(self.token, QImage(), str(exc), self.max_size is None)
        else:
            self.signals.loaded.emit(self.token, image, "", self.max_size is None)


MAX_ZOOM = 16.0


class ImageCanvas(QWidget):
    """Paints one image centred and fitted, never enlarged beyond its own size.

    Zoom is relative to the fitted size: 1.0 is "fit to window", larger values zoom in
    (wheel, double-click, +/-), and a left-button drag pans a zoomed image.
    """

    zoomed_in = Signal()  # the view went past fit: a full-resolution image is useful now

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image = QImage()
        self._scaled: QPixmap | None = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)  # of the image centre from the widget centre
        self._drag_from: QPointF | None = None
        self.message = ""

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def offset(self) -> QPointF:
        return QPointF(self._offset)

    def set_image(self, image: QImage, message: str = "", keep_view: bool = False) -> None:
        self._image = image
        self._scaled = None
        self.message = message
        if not keep_view:
            self._zoom = 1.0
            self._offset = QPointF(0, 0)
        self.update()

    def _fit_scale(self) -> float:
        """Logical pixels per image pixel when fitted (at most 1 device pixel per pixel)."""
        if self._image.isNull() or self.width() <= 0 or self.height() <= 0:
            return 1.0
        dpr = self.devicePixelRatioF()
        fit = min(self.width() / self._image.width(), self.height() / self._image.height())
        return min(fit, 1 / dpr)

    def _clamp(self) -> None:
        scale = self._fit_scale() * self._zoom
        limit_x = max(0.0, (self._image.width() * scale - self.width()) / 2)
        limit_y = max(0.0, (self._image.height() * scale - self.height()) / 2)
        self._offset = QPointF(
            max(-limit_x, min(limit_x, self._offset.x())),
            max(-limit_y, min(limit_y, self._offset.y())),
        )

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Zoom to ``zoom`` (1.0 = fit), keeping the image point under ``anchor`` fixed."""
        if self._image.isNull():
            return
        zoom = max(1.0, min(MAX_ZOOM, zoom))
        old_scale = self._fit_scale() * self._zoom
        new_scale = self._fit_scale() * zoom
        centre = QPointF(self.width() / 2, self.height() / 2)
        anchor = anchor if anchor is not None else centre
        # The image point under the anchor, then where the centre must go to keep it there.
        point = (anchor - centre - self._offset) / old_scale
        self._offset = anchor - centre - point * new_scale
        was_fit = self._zoom <= 1.0
        self._zoom = zoom
        self._clamp()
        self.update()
        if was_fit and zoom > 1.0:
            self.zoomed_in.emit()

    def zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        self.set_zoom(self._zoom * factor, anchor)

    def reset_view(self) -> None:
        self.set_zoom(1.0)
        self._offset = QPointF(0, 0)
        self.update()

    def pan_by(self, delta: QPointF) -> None:
        self._offset += delta
        self._clamp()
        self.update()

    def resizeEvent(self, event) -> None:
        self._scaled = None
        self._clamp()
        super().resizeEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom_by(1.25**steps, event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._zoom > 1.0:
            self._drag_from = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_from is not None:
            self.pan_by(event.position() - self._drag_from)
            self._drag_from = event.position()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_from is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = None
            self.unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._zoom > 1.0:
                self.reset_view()
            else:
                self.set_zoom(3.0, event.position())
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        if self._image.isNull():
            if self.message:
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        if self._zoom > 1.0:
            scale = self._fit_scale() * self._zoom
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(
                self.width() / 2 + self._offset.x(), self.height() / 2 + self._offset.y()
            )
            painter.scale(scale, scale)
            painter.drawImage(
                QPointF(-self._image.width() / 2, -self._image.height() / 2), self._image
            )
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
        self.info_button = QPushButton("Info", self)
        self.info_button.setCheckable(True)
        self.info_button.setToolTip("Show or hide the photo details (I)")
        self.info_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.info_button.toggled.connect(self.set_info_visible)
        self.info_panel = InfoPanel(self)
        self.info_panel.hide()
        self.info_panel.reveal_requested.connect(lambda loc: show_in_explorer(full_path(loc)))
        self.status = QLabel(self)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top = QHBoxLayout()
        top.setContentsMargins(8, 4, 8, 4)
        top.addWidget(self.back_button)
        top.addWidget(self.status, 1)
        top.addWidget(self.info_button)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.canvas, 1)
        body.addWidget(self.info_panel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addLayout(body, 1)
        self._conn: sqlite3.Connection | None = None
        self._full_loaded = False
        self._path: Path | None = None
        self.canvas.zoomed_in.connect(self._load_full_resolution)
        self._photos: list[GridPhoto] = []
        self._sources: dict[int, Source] = {}
        self._row = -1
        self._token = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _Signals()
        self._signals.loaded.connect(self._on_loaded)

    def set_connection(self, conn: sqlite3.Connection | None) -> None:
        """The database the info panel reads from (read-only use)."""
        self._conn = conn

    def set_info_visible(self, visible: bool) -> None:
        self.info_panel.setVisible(visible)
        if self.info_button.isChecked() != visible:
            self.info_button.setChecked(visible)
        if visible:
            self._refresh_info()

    def _refresh_info(self) -> None:
        if not self.info_panel.isHidden() and 0 <= self._row < len(self._photos):
            info = None
            if self._conn is not None:
                info = db.photo_info(self._conn, self._photos[self._row].photo_id)
            self.info_panel.set_info(info)

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
        self._refresh_info()

    def close_viewer(self) -> None:
        self.closed.emit(self._row)

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
        self._refresh_info()
        self._token += 1
        self._pool.clear()
        self._full_loaded = False
        source = self._sources.get(photo.source_id)
        if source is None:
            self.canvas.set_image(QImage(), "This photo's source was removed.")
            return
        path = self._path = Path(source.root_path).joinpath(*photo.relative_path.split("/"))
        screen = (
            self.screen().size() * self.devicePixelRatioF() if self.screen() else QSize(4096, 4096)
        )
        self.canvas.set_image(QImage(), "Loading…")
        self._pool.start(
            _LoadTask(self._signals, self._token, path, (screen.width(), screen.height()))
        )

    def _load_full_resolution(self) -> None:
        """Zoomed past fit: decode the whole image so zooming shows real detail."""
        path = self._path
        if self._full_loaded or path is None or not 0 <= self._row < len(self._photos):
            return
        self._full_loaded = True
        self._pool.start(_LoadTask(self._signals, self._token, path, None))

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

    def _on_loaded(self, token: int, image: QImage, error: str, full: bool) -> None:
        if token != self._token:
            return  # the user already moved on
        if full:
            if not image.isNull():
                self.canvas.set_image(image, keep_view=True)
        elif image.isNull():
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
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.canvas.zoom_by(1.25)
        elif key == Qt.Key.Key_Minus:
            self.canvas.zoom_by(0.8)
        elif key == Qt.Key.Key_0:
            self.canvas.reset_view()
        elif key == Qt.Key.Key_I:
            self.set_info_visible(self.info_panel.isHidden())
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self.close_viewer()
        else:
            super().mouseReleaseEvent(event)
