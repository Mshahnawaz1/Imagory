"""Thumbnail grid: a QListView in icon mode with lazily loaded, cached thumbnails."""

from __future__ import annotations

import contextlib
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPointF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QKeyEvent,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from imagory import thumbs
from imagory.db import GridPhoto, Source

ICON_SIZE = 160
CELL_PADDING = 8
CACHE_ITEMS = 1500  # about 100 KB each at 160 px


def display_name(photo: GridPhoto) -> str:
    return photo.relative_path.rsplit("/", 1)[-1]


def format_date(taken_at_local: str | None) -> str:
    return taken_at_local.replace("T", " ") if taken_at_local else "Unknown date"


def sorts_before(a: GridPhoto, b: GridPhoto) -> bool:
    """Grid order, as in ``db.grid_photos``: newest first, no date last, then by path,
    then by source."""
    if a.taken_at_local != b.taken_at_local:
        if a.taken_at_local is None:
            return False
        if b.taken_at_local is None:
            return True
        return a.taken_at_local > b.taken_at_local
    # NOCASE in SQLite folds ASCII only; lower() can differ for other letters, and the
    # full reload after the scan settles any difference.
    a_path, b_path = a.relative_path.lower(), b.relative_path.lower()
    if a_path != b_path:
        return a_path < b_path
    return a.source_id < b.source_id


def insertion_index(photos: list[GridPhoto], photo: GridPhoto) -> int:
    """Where ``photo`` goes in the grid-ordered ``photos`` (after equal entries)."""
    lo, hi = 0, len(photos)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorts_before(photo, photos[mid]):
            hi = mid
        else:
            lo = mid + 1
    return lo


class _LoaderSignals(QObject):
    loaded = Signal(int, int, QImage)  # generation, photo id, image (null on failure)


class _LoadTask(QRunnable):
    def __init__(self, signals, generation, photo_id, source_root, thumbs_root, rel, size):
        super().__init__()
        self.signals = signals
        self.generation = generation
        self.photo_id = photo_id
        self.source_root = source_root
        self.thumbs_root = thumbs_root
        self.rel = rel
        self.size = size

    def run(self) -> None:
        # Regenerates the thumbnail from the source file if the cache entry was deleted.
        path = thumbs.ensure_thumbnail(self.source_root, self.thumbs_root, self.rel)
        image = load_thumbnail(path, self.size) if path else QImage()
        # The loader may have been destroyed while this task ran (shutdown).
        with contextlib.suppress(RuntimeError):
            self.signals.loaded.emit(self.generation, self.photo_id, image)


def load_thumbnail(path: Path, size: int) -> QImage:
    """Decode a cached thumbnail at display size, almost entirely in C++.

    Qt releases the GIL while reading, so loader threads barely stall the UI thread.
    """
    reader = QImageReader(str(path))
    original = reader.size()
    if original.isValid() and max(original.width(), original.height()) > size:
        reader.setScaledSize(original.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return image
    # RGB32 is the raster engine's native format, so painting needs no conversion.
    return image.convertToFormat(QImage.Format.Format_RGB32)


class ThumbnailLoader(QObject):
    """Loads thumbnails off the UI thread and keeps a size-limited in-memory cache."""

    ready = Signal(int)  # photo id

    def __init__(self, thumbs_root: Path, device_pixel_ratio: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self.thumbs_root = thumbs_root
        self.device_pixel_ratio = device_pixel_ratio
        self.pixel_size = round(ICON_SIZE * device_pixel_ratio)
        # Per source id: its folder and its thumbnail folder.
        self._folders: dict[int, tuple[Path, Path]] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)  # more threads only add GIL contention with the UI
        self._cache: OrderedDict[int, QPixmap] = OrderedDict()
        self._failed: set[int] = set()
        self._pending: set[int] = set()
        self._generation = 0
        self._signals = _LoaderSignals()
        self._signals.loaded.connect(self._on_loaded)

    def reset(self, sources: dict[int, Source]) -> None:
        """Forget everything, e.g. after a rescan changed thumbnails or sources changed."""
        self._generation += 1
        self._pool.clear()
        self._cache.clear()
        self._failed.clear()
        self._pending.clear()
        self._folders = {
            sid: (Path(s.root_path), thumbs.source_thumbs_dir(self.thumbs_root, sid))
            for sid, s in sources.items()
        }

    def drop_queued(self) -> None:
        """Discard queued (not yet started) loads, e.g. when the visible area scrolls away."""
        self._pool.clear()
        self._pending.clear()

    def pixmap(self, photo: GridPhoto) -> QPixmap | None:
        pix = self._cache.get(photo.photo_id)
        if pix is not None:
            self._cache.move_to_end(photo.photo_id)
            return pix
        if photo.photo_id not in self._pending and photo.photo_id not in self._failed:
            folders = self._folders.get(photo.source_id)
            if folders is None:
                return None
            self._pending.add(photo.photo_id)
            self._pool.start(
                _LoadTask(
                    self._signals,
                    self._generation,
                    photo.photo_id,
                    folders[0],
                    folders[1],
                    photo.relative_path,
                    self.pixel_size,
                )
            )
        return None

    def has_failed(self, photo_id: int) -> bool:
        return photo_id in self._failed

    def wait(self) -> None:
        self._pool.waitForDone()

    def _on_loaded(self, generation: int, photo_id: int, image: QImage) -> None:
        if generation != self._generation:
            return
        self._pending.discard(photo_id)
        if image.isNull():
            self._failed.add(photo_id)
        else:
            pix = QPixmap.fromImage(image)
            pix.setDevicePixelRatio(self.device_pixel_ratio)
            self._cache[photo_id] = pix
            while len(self._cache) > CACHE_ITEMS:
                self._cache.popitem(last=False)
        self.ready.emit(photo_id)


# Looked up once: data() and paint() run hundreds of times per frame while scrolling.
_DECORATION = Qt.ItemDataRole.DecorationRole
_TOOLTIP = Qt.ItemDataRole.ToolTipRole
_ACCESSIBLE = Qt.ItemDataRole.AccessibleTextRole
_SELECTED = QStyle.StateFlag.State_Selected
_FOCUSED = QStyle.StateFlag.State_HasFocus
_HIGHLIGHT = QPalette.ColorRole.Highlight


class PhotoGridModel(QAbstractListModel):
    def __init__(self, loader: ThumbnailLoader, parent=None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.photos: list[GridPhoto] = []
        self._row_index: dict[int, int] | None = {}  # photo id -> row; None until rebuilt
        self._broken = QPixmap(ICON_SIZE, ICON_SIZE)
        self._broken.fill(QColor(120, 120, 120, 80))
        loader.ready.connect(self._on_ready)

    def set_photos(self, photos: list[GridPhoto], sources: dict[int, Source]) -> None:
        self.beginResetModel()
        self.loader.reset(sources)
        self.photos = photos
        self._row_index = None
        self.endResetModel()

    @property
    def _rows(self) -> dict[int, int]:
        if self._row_index is None:
            self._row_index = {p.photo_id: i for i, p in enumerate(self.photos)}
        return self._row_index

    def append_photos(self, photos: list[GridPhoto]) -> None:
        """Add the rest of the library after the first screen has been shown."""
        # A scan may already have inserted some of these.
        photos = [p for p in photos if p.photo_id not in self._rows]
        if not photos:
            return
        first = len(self.photos)
        self.beginInsertRows(QModelIndex(), first, first + len(photos) - 1)
        # Extend in place: the viewer holds a reference to this list.
        self.photos.extend(photos)
        self._rows.update((p.photo_id, first + i) for i, p in enumerate(photos))
        self.endInsertRows()

    def insert_photos(self, photos: list[GridPhoto]) -> None:
        """Insert photos a running scan just added, each at its place in grid order."""
        photos = [p for p in photos if p.photo_id not in self._rows]
        if not photos:
            return
        for photo in photos:
            row = insertion_index(self.photos, photo)
            self.beginInsertRows(QModelIndex(), row, row)
            self.photos.insert(row, photo)  # in place: the viewer holds this list
            self.endInsertRows()
        self._row_index = None  # rows after each insertion point moved

    def row_of(self, photo_id: int) -> int | None:
        return self._rows.get(photo_id)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.photos)

    def thumbnail(self, row: int) -> QPixmap | None:
        """The cached thumbnail for ``row``, queueing a load if needed (None until ready)."""
        photo = self.photos[row]
        pix = self.loader.pixmap(photo)
        if pix is None and self.loader.has_failed(photo.photo_id):
            return self._broken
        return pix

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == _DECORATION:
            return self.thumbnail(index.row())
        if role == _TOOLTIP:
            photo = self.photos[index.row()]
            return f"{photo.relative_path}\n{format_date(photo.taken_at_local)}"
        if role == _ACCESSIBLE:
            return display_name(self.photos[index.row()])
        return None

    def _on_ready(self, photo_id: int) -> None:
        row = self._rows.get(photo_id)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index, [_DECORATION])


class ThumbnailDelegate(QStyledItemDelegate):
    """Paints the prepared thumbnail pixmap directly, centred in its cell.

    The default delegate turns every pixmap into a QIcon and rescales it on each paint,
    which is far too slow for smooth scrolling.
    """

    def __init__(self, model: PhotoGridModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        side = ICON_SIZE + CELL_PADDING * 2
        self._size = QSize(side, side)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        rect = option.rect
        state = option.state
        if state & _SELECTED:
            painter.fillRect(rect, option.palette.color(_HIGHLIGHT))
        pix = self._model.thumbnail(index.row())
        if pix is not None:
            ratio = pix.devicePixelRatio()
            x = rect.x() + (rect.width() - pix.width() / ratio) / 2
            y = rect.y() + (rect.height() - pix.height() / ratio) / 2
            painter.drawPixmap(QPointF(x, y), pix)
        if state & _FOCUSED:
            painter.setPen(option.palette.color(_HIGHLIGHT))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return self._size


class PhotoGridView(QListView):
    open_requested = Signal(int)  # row

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(2000)
        self.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.setGridSize(QSize(ICON_SIZE + CELL_PADDING * 2, ICON_SIZE + CELL_PADDING * 2))
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(ICON_SIZE // 4)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.doubleClicked.connect(lambda index: self.open_requested.emit(index.row()))
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    def setModel(self, model) -> None:
        super().setModel(model)
        if isinstance(model, PhotoGridModel):
            self.setItemDelegate(ThumbnailDelegate(model, self))

    def _on_scrolled(self) -> None:
        model = self.model()
        if isinstance(model, PhotoGridModel):
            model.loader.drop_queued()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.open_requested.emit(self.currentIndex().row())
            return
        super().keyPressEvent(event)
