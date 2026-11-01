"""Custom virtualized photo grid with month headers (PRD §8.6).

``PhotoGrid`` paints only the headers and cells inside the viewport and asks for
thumbnails of visible items only. It knows nothing about the database: items come from a
``GridDataSource`` and pixmaps from a ``ThumbnailProvider``. The database-backed ones the
app uses are in ``grid.py``.
"""

from __future__ import annotations

import contextlib
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import QAbstractScrollArea, QToolTip, QWidget

from imagory.ui.grid_layout import GridLayout, MonthKey, Rect, build_sections

ICON_SIZE = 160
CELL_PADDING = 8
CELL_SIZE = ICON_SIZE + CELL_PADDING * 2
SPACING = 4
HEADER_HEIGHT = 40
CAPTION_GAP = 4  # between the thumbnail area and the caption line
SELECTION_WIDTH = 3
CORNER_RADIUS = 4
# Thumbnail levels: name -> icon size in px (thumbnails on disk are 320 px; smaller
# cells scale them down). "medium" is the default.
THUMBNAIL_SIZES = {"small": 112, "medium": ICON_SIZE, "large": 240}
CACHE_ITEMS = 1500  # about 100 KB each at 160 px


class GridDataSource(Protocol):
    """What the grid needs to know about its items.

    Items are addressed by a flat index in grid order, which must match the section
    order of ``month_counts``: newest month first, items without a date last.
    """

    def month_counts(self) -> Sequence[tuple[MonthKey, int]]:
        """Number of items per month; ``None`` is the "Unknown date" section."""
        ...

    def item(self, index: int) -> object:
        """The item at ``index``, passed unchanged to the thumbnail provider."""
        ...

    def tooltip(self, index: int) -> str:
        """Tooltip text for the item at ``index`` (empty for none)."""
        ...

    def caption(self, index: int) -> str:
        """File name shown under the thumbnail (empty for none). Optional: a source
        without this method gets no captions."""
        ...


class ThumbnailProvider(QObject):
    """Supplies thumbnails without blocking. Subclass and override ``thumbnail``.

    ``thumbnail`` returns a ready pixmap, or None after queueing a load; when the load
    finishes the provider emits ``ready`` with the index it was asked for, and the grid
    repaints that cell if it is still visible.
    """

    ready = Signal(int)  # index

    def thumbnail(self, index: int, item: object) -> QPixmap | None:
        return None

    def cancel_pending(self) -> None:
        """Drop queued loads; called when the visible area scrolls away."""

    def reset(self) -> None:
        """Forget cached and pending thumbnails, e.g. after the items changed."""


class _LoadSignals(QObject):
    loaded = Signal(int, int, object, QImage)  # generation, index, key, image


class _LoadTask(QRunnable):
    def __init__(self, signals, generation, index, key, item, load) -> None:
        super().__init__()
        self.signals = signals
        self.generation = generation
        self.index = index
        self.key = key
        self.item = item
        self.load = load

    def run(self) -> None:
        try:
            image = self.load(self.item)
        except Exception:  # one bad thumbnail never breaks the grid
            image = QImage()
        with contextlib.suppress(RuntimeError):  # provider destroyed during shutdown
            self.signals.loaded.emit(self.generation, self.index, self.key, image)


class PooledThumbnailProvider(ThumbnailProvider):
    """Loads thumbnails on a QThreadPool into a size-limited cache, like ui/images.py.

    ``load(item)`` runs on a pool thread and returns a QImage (null on failure);
    ``key(item)`` identifies the thumbnail in the cache (e.g. the photo id).
    """

    def __init__(
        self,
        load: Callable[[object], QImage],
        key: Callable[[object], Hashable],
        *,
        device_pixel_ratio: float = 1.0,
        cache_items: int = CACHE_ITEMS,
        threads: int = 2,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._key = key
        self.device_pixel_ratio = device_pixel_ratio
        self.cache_items = cache_items
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(threads)  # more threads add GIL contention with the UI
        self._cache: OrderedDict[Hashable, QPixmap] = OrderedDict()
        self._failed: set[Hashable] = set()
        self._pending: set[Hashable] = set()
        self._generation = 0
        self._broken = QPixmap(ICON_SIZE, ICON_SIZE)
        self._broken.fill(QColor(120, 120, 120, 80))
        self._signals = _LoadSignals()
        self._signals.loaded.connect(self._on_loaded)

    def thumbnail(self, index: int, item: object) -> QPixmap | None:
        key = self._key(item)
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
            return pix
        if key in self._failed:
            return self._broken
        if key not in self._pending:
            self._pending.add(key)
            self._pool.start(
                _LoadTask(self._signals, self._generation, index, key, item, self._load)
            )
        return None

    def cancel_pending(self) -> None:
        self._pool.clear()
        self._pending.clear()

    def cached(self, key: Hashable) -> QPixmap | None:
        """The cached pixmap for ``key``, without loading it or refreshing its age."""
        return self._cache.get(key)

    def cached_count(self) -> int:
        return len(self._cache)

    def reset(self) -> None:
        self._generation += 1
        self._pool.clear()
        self._cache.clear()
        self._failed.clear()
        self._pending.clear()

    def wait(self) -> None:
        self._pool.waitForDone()

    def _on_loaded(self, generation: int, index: int, key: Hashable, image: QImage) -> None:
        if generation != self._generation:
            return
        self._pending.discard(key)
        if image.isNull():
            self._failed.add(key)
        else:
            pix = QPixmap.fromImage(image)
            pix.setDevicePixelRatio(self.device_pixel_ratio)
            self._cache[key] = pix
            while len(self._cache) > self.cache_items:
                self._cache.popitem(last=False)
        self.ready.emit(index)


def _qrect(r: Rect, dy: int) -> QRect:
    return QRect(r.x, r.y - dy, r.width, r.height)


_ARROWS = {
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
}
_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier


class PhotoGrid(QAbstractScrollArea):
    """Thumbnail grid with month headers, painting and loading only what is visible."""

    activated = Signal(int)  # index; double-click or Enter
    current_changed = Signal(int)  # index, or -1
    selection_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        cell_size: int = CELL_SIZE,
        spacing: int = SPACING,
        header_height: int = HEADER_HEIGHT,
    ) -> None:
        super().__init__(parent)
        self.cell_size = cell_size
        self._thumbnail_size = "medium"
        self.selection_colour = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        self.spacing = spacing
        self.header_height = header_height
        self._source: GridDataSource | None = None
        self._provider: ThumbnailProvider | None = None
        self.caption_height = QFontMetrics(self.font()).height() + CAPTION_GAP
        self.grid_layout = GridLayout([], 1, cell_size, spacing, header_height, self.caption_height)
        self._selected = bytearray()
        self._current = -1
        self._anchor = -1  # where Shift-selection starts
        self._header_font = QFont(self.font())
        self._header_font.setBold(True)
        self._header_font.setPointSizeF(self._header_font.pointSizeF() * 1.25)
        self._placeholder = QColor(128, 128, 128, 40)
        self._empty_text = ""
        # Always on, so showing or hiding the bar never changes the width and reflows.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.verticalScrollBar().setSingleStep(cell_size // 4)

    # Data ------------------------------------------------------------------------

    def set_provider(self, provider: ThumbnailProvider | None) -> None:
        if self._provider is not None:
            self._provider.ready.disconnect(self._on_thumbnail_ready)
        self._provider = provider
        if provider is not None:
            provider.ready.connect(self._on_thumbnail_ready)
        self.viewport().update()

    def set_data_source(self, source: GridDataSource | None) -> None:
        """Show a new set of items from the top, with nothing selected."""
        self._source = source
        self._build_layout()
        self._selected = bytearray(self.count())
        self._set_current(-1)
        self._anchor = -1
        if self._provider is not None:
            self._provider.reset()
        self.verticalScrollBar().setValue(0)
        self.viewport().update()
        self.selection_changed.emit()

    def reload(self) -> None:
        """Re-read the counts after the items changed (e.g. a scan added photos).

        Keeps the scroll position on the same section; selection is cleared because the
        indexes may have shifted.
        """
        anchor = self.grid_layout.anchor(self.verticalScrollBar().value())
        self._build_layout()
        self._selected = bytearray(self.count())
        self._set_current(min(self._current, self.count() - 1))
        self._anchor = -1
        if self._provider is not None:
            self._provider.reset()
        self.verticalScrollBar().setValue(self.grid_layout.anchor_y(anchor))
        self.viewport().update()
        self.selection_changed.emit()

    def items_inserted(self, positions: Sequence[int]) -> None:
        """Items were inserted at ``positions`` (ascending indexes after the insertion).

        Re-reads the counts and keeps the selection, the current item, and the item at
        the top of the viewport (unless scrolled to the very top, which stays there).
        With no positions, only the counts changed: indexes stay where they were.
        """
        shifted = [q - j for j, q in enumerate(positions)]  # old index each goes before

        def remap(old: int) -> int:
            return old + bisect_right(shifted, old) if old >= 0 else old

        top_y = self.scroll_y()
        top_item, offset = -1, 0
        if top_y > 0 and self.count():
            top_item = self.grid_layout.visible(top_y, top_y + 1).first_item
            if top_item < self.count():
                offset = top_y - self.grid_layout.item_rect(top_item).y
            else:
                top_item = -1
        old = self._selected
        selected = bytearray()
        prev = 0
        for s in shifted:
            selected += old[prev:s]
            selected.append(0)
            prev = s
        selected += old[prev:]
        current, anchor = remap(self._current), remap(self._anchor)
        self._build_layout()
        n = self.count()
        self._selected = selected[:n] + bytearray(max(0, n - len(selected)))
        self._anchor = anchor if anchor < n else -1
        self._set_current(current if current < n else -1)
        if top_item >= 0:
            top_item = min(remap(top_item), n - 1)
            if top_item >= 0:
                y = self.grid_layout.item_rect(top_item).y + offset
                self.verticalScrollBar().setValue(y)
        self.viewport().update()
        if self._selected != old:
            self.selection_changed.emit()

    def count(self) -> int:
        return self.grid_layout.count

    @property
    def thumbnail_size(self) -> str:
        """Current level: "small", "medium" (default) or "large"."""
        return self._thumbnail_size

    def set_thumbnail_size(self, level: str) -> None:
        """Change the cell size to a level in ``THUMBNAIL_SIZES``, keeping the photo at
        the top of the viewport in view."""
        if level not in THUMBNAIL_SIZES:
            raise ValueError(f"unknown thumbnail size {level!r}")
        if level == self._thumbnail_size:
            return
        self._thumbnail_size = level
        self.cell_size = THUMBNAIL_SIZES[level] + 2 * CELL_PADDING
        old = self.grid_layout
        anchor = old.anchor(self.scroll_y())
        self._build_layout()
        self.verticalScrollBar().setSingleStep(self.cell_size // 4)
        self.verticalScrollBar().setValue(self.grid_layout.anchor_y(anchor))
        if self._current >= 0:
            self.scroll_to(self._current)
        self.viewport().update()

    def set_empty_text(self, text: str) -> None:
        """Shown in the middle of the grid while it has no items."""
        self._empty_text = text
        self.viewport().update()

    def _build_layout(self) -> None:
        counts = self._source.month_counts() if self._source is not None else []
        self.grid_layout = GridLayout(
            build_sections(counts),
            self.viewport().width(),
            self.cell_size,
            self.spacing,
            self.header_height,
            self.caption_height,
        )
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        bar = self.verticalScrollBar()
        page = self.viewport().height()
        bar.setRange(0, max(0, self.grid_layout.height - page))
        bar.setPageStep(page)

    # Geometry --------------------------------------------------------------------

    def scroll_y(self) -> int:
        return self.verticalScrollBar().value()

    def visible_range(self) -> tuple[int, int]:
        """``(first, end)`` indexes of the items intersecting the viewport."""
        top = self.scroll_y()
        vis = self.grid_layout.visible(top, top + self.viewport().height())
        return vis.first_item, vis.end_item

    def index_at(self, pos) -> int | None:
        """The item under a viewport point, or None for headers and gaps."""
        return self.grid_layout.item_at(int(pos.x()), int(pos.y()) + self.scroll_y())

    def visual_rect(self, index: int) -> QRect:
        return _qrect(self.grid_layout.item_rect(index), self.scroll_y())

    def scroll_to(self, index: int, at_top: bool = False) -> None:
        """Scroll the least amount that makes ``index`` fully visible, or put its row at
        the top of the viewport (with its month header if it is in the first row)."""
        r = self.grid_layout.item_rect(index)
        bar = self.verticalScrollBar()
        top, height = bar.value(), self.viewport().height()
        section = self.grid_layout.section_of(index)
        first_row = r.y == self.grid_layout.section_top(section) + self.header_height
        want_top = self.grid_layout.section_top(section) if first_row else r.y
        if at_top or want_top < top:
            bar.setValue(want_top)
        elif r.y + r.height > top + height:
            bar.setValue(r.y + r.height - height + self.spacing)

    def resizeEvent(self, event: QResizeEvent) -> None:
        anchor = self.grid_layout.anchor(self.scroll_y())
        self.grid_layout.reflow(self.viewport().width())
        self._update_scrollbar()
        self.verticalScrollBar().setValue(self.grid_layout.anchor_y(anchor))
        super().resizeEvent(event)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        if self._provider is not None:
            self._provider.cancel_pending()
        self.viewport().update()

    # Painting --------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self.viewport())
        palette = self.palette()
        clip = event.rect()
        painter.fillRect(clip, palette.color(QPalette.ColorRole.Base))
        top = self.scroll_y() + clip.top()
        bottom = self.scroll_y() + clip.bottom() + 1
        dy = self.scroll_y()
        layout = self.grid_layout
        if not layout.count and self._empty_text:
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)

        painter.setFont(self._header_font)
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        indent = layout.left + CELL_PADDING // 2
        for s, r in layout.visible_headers(top, bottom):
            text_rect = QRect(indent, r.y - dy, r.width - indent, r.height)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                layout.sections[s].label,
            )

        source, provider, selected = self._source, self._provider, self._selected
        accent = self.selection_colour
        placeholder = self._placeholder
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        normal = palette.color(QPalette.ColorRole.Text)
        font = self.font()
        bold = QFont(font)
        bold.setBold(True)
        metrics, bold_metrics = QFontMetrics(font), QFontMetrics(bold)
        inset = CELL_PADDING
        caption_of = getattr(source, "caption", None)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, r in layout.visible_cells(top, bottom):
            x, y = r.x, r.y - dy
            area = QRectF(x + inset, y + inset, r.width - 2 * inset, r.width - 2 * inset)
            pix = None
            if provider is not None and source is not None:
                pix = provider.thumbnail(index, source.item(index))
            if pix is None:
                target = area
            else:
                ratio = pix.devicePixelRatio()
                w, h = pix.width() / ratio, pix.height() / ratio
                scale = min(area.width() / w, area.height() / h) if w and h else 1.0
                w, h = w * scale, h * scale
                target = QRectF(
                    area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h
                )
            path = QPainterPath()
            path.addRoundedRect(target, CORNER_RADIUS, CORNER_RADIUS)
            if pix is None:
                painter.fillPath(path, placeholder)
            else:
                painter.save()
                painter.setClipPath(path)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(target, pix, QRectF(pix.rect()))
                painter.restore()
            if selected[index]:
                half = SELECTION_WIDTH / 2
                ring = QPainterPath()
                ring.addRoundedRect(
                    target.adjusted(-half, -half, half, half),
                    CORNER_RADIUS + half,
                    CORNER_RADIUS + half,
                )
                painter.setPen(QPen(accent, SELECTION_WIDTH))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(ring)
            text = caption_of(index) if caption_of is not None else ""
            if text:
                m = bold_metrics if selected[index] else metrics
                painter.setFont(bold if selected[index] else font)
                painter.setPen(normal if selected[index] else muted)
                width = r.width - 2 * inset // 2
                cap = QRect(x + inset // 2, y + r.width, width, r.height - r.width)
                painter.drawText(
                    cap,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    m.elidedText(text, Qt.TextElideMode.ElideRight, width),
                )
            if index == self._current and self.hasFocus():
                painter.setPen(QPen(accent, 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(x, y, r.width - 1, r.height - 1)
        painter.end()

    def _on_thumbnail_ready(self, index: int) -> None:
        first, end = self.visible_range()
        if first <= index < end:
            self.viewport().update(self.visual_rect(index))

    def _update_item(self, index: int) -> None:
        if 0 <= index < self.count():
            self.viewport().update(self.visual_rect(index))

    # Selection -------------------------------------------------------------------

    def current_index(self) -> int:
        return self._current

    def set_current_index(self, index: int) -> None:
        """Make ``index`` current and the only selected item, unless it already is
        current (then the selection is kept), and scroll it into view."""
        if not 0 <= index < self.count():
            return
        if index != self._current:
            self.click(index, Qt.KeyboardModifier.NoModifier)
        self.scroll_to(index)

    def is_selected(self, index: int) -> bool:
        return bool(self._selected[index])

    def selected_count(self) -> int:
        return self._selected.count(1)

    def selected_indexes(self) -> list[int]:
        return [i for i, v in enumerate(self._selected) if v]

    def select_all(self) -> None:
        n = self.count()
        if n:
            self._selected = bytearray(b"\x01") * n
            self.viewport().update()
            self.selection_changed.emit()

    def clear_selection(self) -> None:
        self._selected = bytearray(self.count())
        self.viewport().update()
        self.selection_changed.emit()

    def _set_current(self, index: int) -> None:
        if index != self._current:
            old, self._current = self._current, index
            self._update_item(old)
            self._update_item(index)
            self.current_changed.emit(index)

    def _select_range(self, a: int, b: int, keep: bool) -> None:
        lo, hi = min(a, b), max(a, b)
        if not keep:
            self._selected = bytearray(self.count())
        self._selected[lo : hi + 1] = b"\x01" * (hi - lo + 1)

    def click(self, index: int | None, modifiers: Qt.KeyboardModifier) -> None:
        """Apply a click with the usual file-manager rules (also used by keyboard moves)."""
        if index is None:
            if not modifiers & (_CTRL | _SHIFT):
                self.clear_selection()
            return
        if modifiers & _SHIFT and self._anchor >= 0:
            self._select_range(self._anchor, index, keep=bool(modifiers & _CTRL))
        elif modifiers & _CTRL:
            self._selected[index] ^= 1
            self._anchor = index
        else:
            self._selected = bytearray(self.count())
            self._selected[index] = 1
            self._anchor = index
        self._set_current(index)
        self.viewport().update()
        self.selection_changed.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.click(self.index_at(event.position()), event.modifiers())
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.index_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self.activated.emit(index)
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, mods = event.key(), event.modifiers()
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._current >= 0:
            self.activated.emit(self._current)
            return
        if self.count() and key in (*_ARROWS, Qt.Key.Key_Home, Qt.Key.Key_End):
            if self._current < 0 or key == Qt.Key.Key_Home:
                target = 0
            elif key == Qt.Key.Key_End:
                target = self.count() - 1
            else:
                target = self.grid_layout.step(self._current, _ARROWS[key])
            if mods & _CTRL and not mods & _SHIFT:
                self._set_current(target)  # move focus only, like Explorer
            else:
                if self._anchor < 0:
                    self._anchor = max(self._current, 0)
                self.click(target, mods & _SHIFT)
            self.scroll_to(target)
            return
        if key == Qt.Key.Key_Space and mods & _CTRL and self._current >= 0:
            self.click(self._current, _CTRL)
            return
        super().keyPressEvent(event)

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            index = self.index_at(event.pos())
            text = self._source.tooltip(index) if index is not None and self._source else ""
            if text:
                QToolTip.showText(event.globalPos(), text, self.viewport())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().viewportEvent(event)

    def focusInEvent(self, event) -> None:
        self._update_item(self._current)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._update_item(self._current)
        super().focusOutEvent(event)
