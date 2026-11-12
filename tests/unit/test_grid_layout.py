"""Unit tests for the Qt-free month-sectioned grid layout."""

import time

import pytest

from imagory.ui.grid_layout import (
    Anchor,
    GridLayout,
    Rect,
    build_sections,
    month_key,
    month_label,
)

CELL, GAP, HEAD = 100, 10, 30
PITCH = CELL + GAP


def make(counts, width=340):
    # 340 px: (340 - 10) // 110 = 3 columns, 320 px used, left margin 10.
    return GridLayout(build_sections(counts), width, CELL, GAP, HEAD)


def test_month_key_and_label():
    assert month_key("2019-01-31T23:59:59") == (2019, 1)
    assert month_key("2026-10") == (2026, 10)
    assert month_key(None) is None
    assert month_key("") is None
    assert month_key("2019-13-01") is None
    assert month_key("garbage") is None
    assert month_label((2026, 10)) == "October 2026"
    assert month_label((1999, 1)) == "January 1999"
    assert month_label(None) == "Unknown date"


def test_sections_newest_first_unknown_last_and_merged():
    sections = build_sections(
        [((2020, 1), 2), (None, 4), ((2021, 12), 3), ((2020, 5), 0), ((2020, 1), 1)]
    )
    assert [(s.key, s.first, s.count) for s in sections] == [
        ((2021, 12), 0, 3),
        ((2020, 1), 3, 3),
        (None, 6, 4),
    ]
    assert [s.label for s in sections] == ["December 2021", "January 2020", "Unknown date"]
    with pytest.raises(ValueError):
        build_sections([((2020, 1), -1)])


def test_empty_layout():
    layout = make([])
    assert layout.count == 0 and layout.height == 0
    assert layout.visible(0, 500).empty
    assert list(layout.visible_cells(0, 500)) == []
    assert layout.item_at(50, 50) is None
    assert layout.anchor(0) == Anchor(0, None, 0)
    assert layout.anchor_y(Anchor(3, 2, 5)) == 0


def test_only_unknown_dates():
    layout = make([(None, 5)])
    assert [s.label for s in layout.sections] == ["Unknown date"]
    assert layout.item_rect(4) == Rect(10 + PITCH, HEAD + PITCH, CELL, CELL)


def test_positions():
    layout = make([((2021, 2), 4), ((2021, 1), 3)])
    assert (layout.columns, layout.left) == (3, 10)
    # Section 0: header 0-30, rows at 30 and 140; section 1 starts at 250.
    assert layout.section_top(0) == 0 and layout.section_rows(0) == 2
    assert layout.section_top(1) == HEAD + 2 * PITCH == 250
    assert layout.item_rect(0) == Rect(10, 30, CELL, CELL)
    assert layout.item_rect(2) == Rect(10 + 2 * PITCH, 30, CELL, CELL)
    assert layout.item_rect(3) == Rect(10, 30 + PITCH, CELL, CELL)
    assert layout.item_rect(4) == Rect(10, 280, CELL, CELL)  # new section, new row
    assert layout.height == 250 + HEAD + PITCH + GAP
    with pytest.raises(IndexError):
        layout.item_rect(7)


def test_columns_are_centred_and_at_least_one():
    layout = make([(None, 3)], width=400)
    assert layout.columns == 3 and layout.left == (400 - 320) // 2
    narrow = make([(None, 3)], width=50)
    assert narrow.columns == 1
    assert narrow.item_rect(2).y == HEAD + 2 * PITCH


def test_hit_test():
    layout = make([((2021, 2), 4), ((2021, 1), 3)])
    assert layout.item_at(15, 35) == 0
    assert layout.item_at(10 + PITCH + 99, 30 + 99) == 1
    assert layout.item_at(15, 10) is None  # header
    assert layout.item_at(5, 35) is None  # left margin
    assert layout.item_at(10 + CELL + 5, 35) is None  # horizontal gap
    assert layout.item_at(15, 30 + CELL + 5) is None  # vertical gap
    assert layout.item_at(10 + PITCH, 30 + PITCH + 5) is None  # past the partial row
    assert layout.item_at(10 + 3 * PITCH, 35) is None  # right of the last column
    assert layout.item_at(15, 260) is None  # second header
    assert layout.item_at(10 + 2 * PITCH + 1, 281) == 6
    assert layout.item_at(15, -1) is None
    assert layout.item_at(15, layout.height) is None
    for index in range(layout.count):
        r = layout.item_rect(index)
        assert layout.item_at(r.x + r.width // 2, r.y + r.height // 2) == index


def test_visible_range():
    layout = make([((2021, 2), 4), ((2021, 1), 3)])
    vis = layout.visible(0, 100)
    assert (vis.first_section, vis.end_section, vis.first_item, vis.end_item) == (0, 1, 0, 3)
    # A band covering only the second row of section 0 and the next header.
    vis = layout.visible(150, 270)
    assert (vis.first_section, vis.end_section, vis.first_item, vis.end_item) == (0, 2, 3, 4)
    assert [s for s, _ in layout.visible_headers(150, 270)] == [1]
    assert [i for i, _ in layout.visible_cells(150, 270)] == [3]
    vis = layout.visible(0, layout.height)
    assert (vis.first_item, vis.end_item) == (0, 7)
    assert layout.visible(layout.height, layout.height + 100).empty
    assert layout.visible(100, 100).empty


def test_visible_cells_match_brute_force():
    layout = make([((2021, m), m * 3 % 7) for m in range(1, 13)] + [(None, 5)], width=560)
    for top in range(-50, layout.height + 50, 17):
        bottom = top + 230
        expected = [
            i
            for i in range(layout.count)
            if (r := layout.item_rect(i)).y < bottom and r.y + r.height > top
        ]
        got = [i for i, _ in layout.visible_cells(top, bottom)]
        assert got == expected, top
        for s, r in layout.visible_headers(top, bottom):
            assert r.y < bottom and r.y + r.height > top
            assert r == layout.header_rect(s)


def test_reflow_and_anchor():
    layout = make([((2021, 2), 30), ((2021, 1), 30)], width=340)  # 3 columns
    y = layout.item_rect(40).y + 20  # inside the row of item 40 (local 10, row 3)
    anchor = layout.anchor(y)
    assert anchor == Anchor(1, 9, 20)
    layout.reflow(560)  # 5 columns
    assert layout.columns == 5
    new_y = layout.anchor_y(anchor)
    assert new_y == layout.item_rect(39).y + 20
    assert layout.item_at(layout.left + 5, new_y) is not None
    # Anchored on a header.
    anchor = layout.anchor(layout.section_top(1) + 7)
    assert anchor == Anchor(1, None, 7)
    layout.reflow(200)
    assert layout.anchor_y(anchor) == layout.section_top(1) + 7


def test_keyboard_steps_cross_sections():
    layout = make([((2021, 2), 5), ((2021, 1), 2)])  # rows: [0 1 2] [3 4] | [5 6]
    assert layout.step(0, "left") == 0
    assert layout.step(6, "right") == 6
    assert layout.step(2, "down") == 4  # clamped to the partial row
    assert layout.step(4, "down") == 6
    assert layout.step(3, "down") == 5
    assert layout.step(6, "up") == 4
    assert layout.step(5, "up") == 3
    assert layout.step(1, "up") == 1
    assert layout.step(6, "down") == 6


def test_100k_items_is_cheap():
    counts = [((2006 + i // 12, i % 12 + 1), 416) for i in range(240)] + [(None, 160)]
    started = time.perf_counter()
    layout = GridLayout(build_sections(counts), 1280, 176, 4, 40)
    for width in range(600, 2000, 50):
        layout.reflow(width)
    for y in range(0, layout.height, layout.height // 1000):
        list(layout.visible_cells(y, y + 800))
    assert layout.count == 100_000
    assert time.perf_counter() - started < 1.0


def test_caption_height_separates_row_and_column_pitch():
    cap = 20
    layout = GridLayout(build_sections([((2020, 1), 7), ((2019, 1), 4)]), 340, CELL, GAP, HEAD, cap)
    assert layout.columns == 3
    assert layout.row_pitch == CELL + cap + GAP
    r4 = layout.item_rect(4)  # row 1, col 1
    assert r4 == Rect(10 + PITCH, HEAD + layout.row_pitch, CELL, CELL + cap)
    # the caption strip hits; the gap below the cell does not
    assert layout.item_at(r4.x + 5, r4.y + CELL + cap - 1) == 4
    assert layout.item_at(r4.x + 5, r4.y + CELL + cap) is None
    assert layout.section_top(1) == HEAD + 3 * layout.row_pitch
    # visible band covering only row 1 yields exactly row 1 (cells reach into the caption)
    band_top = r4.y + CELL + cap - 1
    vis = layout.visible(band_top, band_top + 1)
    assert (vis.first_item, vis.end_item) == (3, 6)
    cells = dict(layout.visible_cells(0, layout.height))
    assert len(cells) == 11 and cells[4] == r4
    # anchors round-trip through a reflow
    a = layout.anchor(r4.y + 7)
    layout.reflow(450)
    layout.reflow(340)
    assert layout.anchor_y(a) == r4.y + 7
