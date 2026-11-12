"""Offscreen tests for the virtualized PhotoGrid widget."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402

from imagory.ui.photo_grid import PhotoGrid, PooledThumbnailProvider, ThumbnailProvider  # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


class ListSource:
    def __init__(self, counts):
        self.counts = counts
        self.total = sum(c for _, c in counts)

    def month_counts(self):
        return self.counts

    def item(self, index):
        return f"item-{index}"

    def tooltip(self, index):
        return f"tip {index}"

    def caption(self, index):
        return f"IMG_{index:06d}_with_a_rather_long_name.jpg"


class RecordingProvider(ThumbnailProvider):
    def __init__(self):
        super().__init__()
        self.requested: set[int] = set()
        self.pix = QPixmap(160, 160)
        self.pix.fill(QColor("teal"))
        self.cancels = 0

    def thumbnail(self, index, item):
        assert item == f"item-{index}"
        self.requested.add(index)
        return self.pix

    def cancel_pending(self):
        self.cancels += 1


@pytest.fixture
def grid(qtbot):
    widget = PhotoGrid()
    qtbot.addWidget(widget)
    widget.resize(800, 600)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def big_source():
    counts = [((2000 + i // 12, i % 12 + 1), 400) for i in range(240)] + [(None, 1000)]
    return ListSource(counts)


def paint(grid):
    grid.viewport().repaint()


def click(qtbot, grid, index, modifiers=Qt.KeyboardModifier.NoModifier, double=False):
    grid.scroll_to(index)
    center = grid.visual_rect(index).center()
    action = qtbot.mouseDClick if double else qtbot.mouseClick
    action(grid.viewport(), Qt.MouseButton.LeftButton, modifiers, center)


def test_only_visible_items_are_requested(grid):
    provider = RecordingProvider()
    grid.set_provider(provider)
    grid.set_data_source(big_source())
    assert grid.count() == 97_000
    paint(grid)
    first, end = grid.visible_range()
    assert first == 0 and 0 < end < 60
    assert provider.requested == set(range(first, end))

    provider.requested.clear()
    bar = grid.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    assert provider.cancels >= 1
    paint(grid)
    first, end = grid.visible_range()
    assert first > 40_000
    assert provider.requested == set(range(first, end))

    provider.requested.clear()
    bar.setValue(bar.maximum())
    paint(grid)
    assert max(provider.requested) == grid.count() - 1
    assert len(provider.requested) < 60


def test_headers_and_empty(grid):
    grid.set_data_source(None)
    paint(grid)
    assert grid.count() == 0 and grid.visible_range() == (0, 0)
    grid.set_data_source(ListSource([((2026, 10), 3), (None, 2)]))
    labels = [s.label for s in grid.grid_layout.sections]
    assert labels == ["October 2026", "Unknown date"]
    paint(grid)
    assert grid.index_at(QPoint(20, 10)) is None  # on the header


def test_click_ctrl_shift_and_select_all(qtbot, grid):
    seen = []
    grid.selection_changed.connect(lambda: seen.append(grid.selected_indexes()))
    grid.set_data_source(ListSource([((2020, 2), 10), ((2020, 1), 10)]))
    click(qtbot, grid, 3)
    assert grid.selected_indexes() == [3] and grid.current_index() == 3
    click(qtbot, grid, 5, CTRL)
    assert grid.selected_indexes() == [3, 5]
    click(qtbot, grid, 3, CTRL)
    assert grid.selected_indexes() == [5]
    click(qtbot, grid, 12, SHIFT)  # from the last clicked item, across a header
    assert grid.selected_indexes() == list(range(3, 13))
    click(qtbot, grid, 8, SHIFT)  # Shift again replaces the range from the same anchor
    assert grid.selected_indexes() == list(range(3, 9))
    click(qtbot, grid, 15, CTRL | SHIFT)  # Ctrl+Shift adds the range
    assert grid.selected_indexes() == list(range(3, 16))
    click(qtbot, grid, 1)
    assert grid.selected_indexes() == [1]
    qtbot.keyClick(grid, Qt.Key.Key_A, CTRL)
    assert grid.selected_count() == 20
    # Clicking the empty header area clears the selection.
    grid.verticalScrollBar().setValue(0)
    qtbot.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    assert grid.selected_count() == 0
    assert seen[-1] == []


def test_select_all_on_100k_is_fast(grid):
    grid.set_data_source(big_source())
    grid.select_all()
    assert grid.selected_count() == 97_000
    grid.click(10, Qt.KeyboardModifier.NoModifier)
    grid.click(96_000, SHIFT)
    assert grid.selected_count() == 95_991


def test_activation_by_double_click_and_enter(qtbot, grid):
    activated = []
    grid.activated.connect(activated.append)
    grid.set_data_source(ListSource([((2020, 2), 10), ((2020, 1), 10)]))
    click(qtbot, grid, 4, double=True)
    assert activated == [4]
    click(qtbot, grid, 7)
    qtbot.keyClick(grid, Qt.Key.Key_Return)
    assert activated == [4, 7]


def test_arrow_keys_move_and_extend(qtbot, grid):
    grid.set_data_source(ListSource([((2020, 2), 10), ((2020, 1), 10)]))
    columns = grid.grid_layout.columns
    qtbot.keyClick(grid, Qt.Key.Key_Right)
    assert grid.current_index() == 0 and grid.selected_indexes() == [0]
    qtbot.keyClick(grid, Qt.Key.Key_Right)
    assert grid.selected_indexes() == [1]
    qtbot.keyClick(grid, Qt.Key.Key_Down, SHIFT)
    assert grid.selected_indexes() == list(range(1, 1 + columns + 1))
    qtbot.keyClick(grid, Qt.Key.Key_End)
    assert grid.current_index() == 19 and grid.selected_indexes() == [19]
    # The last item is scrolled into view.
    assert grid.visual_rect(19).bottom() <= grid.viewport().height()


def test_resize_keeps_scroll_anchor(grid):
    grid.set_data_source(big_source())
    bar = grid.verticalScrollBar()
    target = 50_000
    bar.setValue(grid.grid_layout.item_rect(target).y)
    first_before = grid.visible_range()[0]
    assert first_before <= target < first_before + grid.grid_layout.columns
    grid.resize(1300, 600)
    first_after, end_after = grid.visible_range()
    assert first_after <= target < end_after
    assert grid.grid_layout.columns > 4


def test_reload_keeps_section_and_clears_selection(grid):
    source = ListSource([((2020, 2), 100), ((2020, 1), 100)])
    grid.set_data_source(source)
    grid.select_all()
    grid.verticalScrollBar().setValue(grid.grid_layout.section_top(1))
    source.counts = [((2020, 3), 5), ((2020, 2), 100), ((2020, 1), 100)]
    grid.reload()
    assert grid.count() == 205 and grid.selected_count() == 0
    # The anchor kept section index 1, which is now February: the same pixel row.
    assert grid.scroll_y() == grid.grid_layout.section_top(1)


def test_pooled_provider_loads_off_thread_and_repaints(qtbot, grid):
    loaded = []

    def load(item):
        image = QImage(40, 30, QImage.Format.Format_RGB32)
        image.fill(QColor("orange"))
        loaded.append(item)
        if item == "item-2":
            raise OSError("corrupt")
        return image

    provider = PooledThumbnailProvider(load, key=lambda item: item)
    grid.set_provider(provider)
    grid.set_data_source(ListSource([((2020, 1), 5)]))
    paint(grid)
    qtbot.waitUntil(lambda: len(provider._cache) == 4 and "item-2" in provider._failed)
    provider.wait()
    assert sorted(loaded) == [f"item-{i}" for i in range(5)]
    assert provider.thumbnail(0, "item-0") is not None
    assert provider.thumbnail(2, "item-2") is provider._broken
    paint(grid)
    assert len(loaded) == 5  # cached: nothing loads twice


def test_captions_make_cells_taller_than_wide(grid):
    assert grid.caption_height > 0
    grid.set_data_source(ListSource([((2020, 1), 50)]))
    r = grid.grid_layout.item_rect(0)
    assert r.height == r.width + grid.caption_height
    grid.set_provider(RecordingProvider())
    paint(grid)  # captions are elided and drawn without error
    # the caption strip belongs to the cell for hit-testing
    pos = grid.visual_rect(0).center()
    assert grid.index_at(pos) == 0


def test_thumbnail_size_levels_keep_current_in_view(grid):
    grid.set_provider(RecordingProvider())
    grid.set_data_source(big_source())
    assert grid.thumbnail_size == "medium"
    medium = grid.grid_layout.cell_size
    grid.set_current_index(50_000)
    for level in ("large", "small", "medium"):
        grid.set_thumbnail_size(level)
        assert grid.thumbnail_size == level
        rect = grid.visual_rect(50_000)
        assert rect.top() >= 0 and rect.bottom() <= grid.viewport().height()
        paint(grid)
    assert grid.grid_layout.cell_size == medium
    grid.set_thumbnail_size("large")
    assert grid.grid_layout.cell_size > medium
    with pytest.raises(ValueError):
        grid.set_thumbnail_size("huge")


def test_selection_colour_defaults_to_highlight(grid):
    from PySide6.QtGui import QPalette

    assert grid.selection_colour == grid.palette().color(QPalette.ColorRole.Highlight)
    grid.set_provider(RecordingProvider())
    grid.set_data_source(ListSource([((2020, 1), 5)]))
    grid.click(1, Qt.KeyboardModifier.NoModifier)
    grid.selection_colour = QColor("red")
    paint(grid)
