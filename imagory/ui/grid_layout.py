"""Geometry of the month-sectioned photo grid (PRD §8.6), free of Qt so it is unit-testable.

The grid shows items in sections, one per calendar month, newest first, with photos that
have no date in a final "Unknown date" section. Items are addressed by a flat index in
grid order: section 0's items come first, then section 1's, and so on.

Everything here is computed from per-month counts, never from the items themselves, so a
layout for 100,000 photos costs only as much as its few hundred sections. Lookups for a
scroll offset or a point use ``bisect`` over the section tops: O(log sections).

Coordinates are content coordinates in pixels: y = 0 is the top of the first header.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

MonthKey = tuple[int, int] | None  # (year, month), or None for "Unknown date"

UNKNOWN_LABEL = "Unknown date"
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def month_key(taken_at_local: str | None) -> MonthKey:
    """The section of an ISO 8601 local date such as ``2019-01-31T23:59:59``."""
    if not taken_at_local or len(taken_at_local) < 7:
        return None
    try:
        year, month = int(taken_at_local[0:4]), int(taken_at_local[5:7])
    except ValueError:
        return None
    return (year, month) if 1 <= month <= 12 else None


def month_label(key: MonthKey) -> str:
    """Header text: "October 2026", or "Unknown date". English, like the rest of the UI."""
    if key is None:
        return UNKNOWN_LABEL
    year, month = key
    return f"{_MONTHS[month - 1]} {year}"


@dataclass(frozen=True, slots=True)
class Section:
    key: MonthKey
    label: str
    first: int  # flat index of the section's first item
    count: int


def build_sections(month_counts: Iterable[tuple[MonthKey, int]]) -> list[Section]:
    """Sections newest first with "Unknown date" last; empty months are dropped.

    Repeated keys are added together. The data source must order its items the same way.
    """
    totals: dict[MonthKey, int] = {}
    for key, count in month_counts:
        if count < 0:
            raise ValueError(f"negative count for {key!r}")
        totals[key] = totals.get(key, 0) + count
    unknown = totals.pop(None, 0)
    keys: list[MonthKey] = sorted((k for k in totals if totals[k]), reverse=True)
    if unknown:
        keys.append(None)
        totals[None] = unknown
    sections = []
    first = 0
    for key in keys:
        sections.append(Section(key, month_label(key), first, totals[key]))
        first += totals[key]
    return sections


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Visible:
    """What intersects a vertical band: sections whose header or rows show, and items."""

    first_section: int
    end_section: int  # exclusive
    first_item: int
    end_item: int  # exclusive; may include a few items just outside the band horizontally

    @property
    def empty(self) -> bool:
        return self.first_item >= self.end_item and self.first_section >= self.end_section


@dataclass(frozen=True, slots=True)
class Anchor:
    """A scroll position expressed independently of the column count, for reflow."""

    section: int
    local_index: int | None  # item within the section whose row is at the top, or None
    offset: int  # pixels from that row's (or the section's) top to the scroll position


class GridLayout:
    """Header and cell positions for one viewport width.

    Each section is a header ``header_height`` high followed by its rows. A row is
    ``cell_size + caption_height`` high with ``spacing`` below it; cells are ``cell_size``
    wide. Columns are centred horizontally.
    """

    def __init__(
        self,
        sections: list[Section],
        width: int,
        cell_size: int,
        spacing: int = 4,
        header_height: int = 40,
        caption_height: int = 0,
    ) -> None:
        if cell_size <= 0 or spacing < 0 or header_height < 0 or caption_height < 0:
            raise ValueError("cell_size must be positive, other sizes >= 0")
        self.sections = sections
        self.cell_size = cell_size
        self.spacing = spacing
        self.header_height = header_height
        self.caption_height = caption_height
        self.cell_height = cell_size + caption_height
        self.pitch = cell_size + spacing  # column pitch
        self.row_pitch = self.cell_height + spacing
        self.count = sections[-1].first + sections[-1].count if sections else 0
        self._firsts = [s.first for s in sections]
        self.reflow(width)

    def reflow(self, width: int) -> None:
        """Recompute positions for a new viewport width: O(sections)."""
        self.width = width
        self.columns = max(1, (width - self.spacing) // self.pitch)
        used = self.columns * self.pitch - self.spacing
        self.left = max(self.spacing, (width - used) // 2)
        tops = []
        rows = []
        y = 0
        for section in self.sections:
            tops.append(y)
            n = -(-section.count // self.columns)
            rows.append(n)
            y += self.header_height + n * self.row_pitch
        self._tops = tops
        self._rows = rows
        self.height = y + self.spacing if self.sections else 0

    # Sections ----------------------------------------------------------------------

    def section_top(self, section: int) -> int:
        return self._tops[section]

    def section_rows(self, section: int) -> int:
        return self._rows[section]

    def section_at_y(self, y: int) -> int:
        """The section whose header or rows contain ``y`` (clamped to the first and last)."""
        return max(0, bisect_right(self._tops, y) - 1)

    def section_of(self, index: int) -> int:
        if not 0 <= index < self.count:
            raise IndexError(index)
        return bisect_right(self._firsts, index) - 1

    def header_rect(self, section: int) -> Rect:
        return Rect(0, self._tops[section], self.width, self.header_height)

    # Items -------------------------------------------------------------------------

    def item_rect(self, index: int) -> Rect:
        s = self.section_of(index)
        row, col = divmod(index - self._firsts[s], self.columns)
        y = self._tops[s] + self.header_height + row * self.row_pitch
        return Rect(self.left + col * self.pitch, y, self.cell_size, self.cell_height)

    def item_at(self, x: int, y: int) -> int | None:
        """Hit-test a content point. Headers, gaps and margins hit nothing."""
        if not self.sections or y < 0 or y >= self.height:
            return None
        s = self.section_at_y(y)
        dy = y - self._tops[s] - self.header_height
        dx = x - self.left
        if dy < 0 or dx < 0:
            return None
        row, in_row = divmod(dy, self.row_pitch)
        col, in_col = divmod(dx, self.pitch)
        if in_row >= self.cell_height or in_col >= self.cell_size or col >= self.columns:
            return None
        local = row * self.columns + col
        section = self.sections[s]
        return section.first + local if local < section.count else None

    def visible(self, top: int, bottom: int) -> Visible:
        """Sections and items intersecting the band ``top <= y < bottom``."""
        if not self.sections or bottom <= top or bottom <= 0 or top >= self.height:
            return Visible(0, 0, 0, 0)
        s0 = self.section_at_y(top)
        s1 = self.section_at_y(bottom - 1) + 1
        first_item = self._first_item_from(s0, top)
        end_item = self._end_item_before(s1 - 1, bottom)
        return Visible(s0, s1, first_item, max(first_item, end_item))

    def _first_item_from(self, s: int, top: int) -> int:
        """First item whose cell reaches below ``top``, starting in section ``s``."""
        rows_top = self._tops[s] + self.header_height
        # Row r's cell spans [rows_top + r * row_pitch, ... + cell_height).
        row = max(0, (top - rows_top - self.cell_height) // self.row_pitch + 1)
        if row >= self._rows[s]:  # band starts below this section's rows
            return self.sections[s].first + self.sections[s].count
        return self.sections[s].first + row * self.columns

    def _end_item_before(self, s: int, bottom: int) -> int:
        """End (exclusive) of the items whose row starts above ``bottom``, in section ``s``."""
        section = self.sections[s]
        rows_top = self._tops[s] + self.header_height
        if bottom <= rows_top:
            return section.first
        rows = min(self._rows[s], -(-(bottom - rows_top) // self.row_pitch))
        return section.first + min(section.count, rows * self.columns)

    def visible_headers(self, top: int, bottom: int) -> Iterator[tuple[int, Rect]]:
        vis = self.visible(top, bottom)
        for s in range(vis.first_section, vis.end_section):
            y = self._tops[s]
            if y < bottom and y + self.header_height > top:
                yield s, Rect(0, y, self.width, self.header_height)

    def visible_cells(self, top: int, bottom: int) -> Iterator[tuple[int, Rect]]:
        """``(index, rect)`` for each cell intersecting the band, in grid order."""
        vis = self.visible(top, bottom)
        cols, pitch, left = self.columns, self.pitch, self.left
        rpitch, cw, ch = self.row_pitch, self.cell_size, self.cell_height
        for s in range(vis.first_section, vis.end_section):
            section = self.sections[s]
            start = max(vis.first_item, section.first)
            end = min(vis.end_item, section.first + section.count)
            rows_top = self._tops[s] + self.header_height
            for index in range(start, end):
                row, col = divmod(index - section.first, cols)
                yield index, Rect(left + col * pitch, rows_top + row * rpitch, cw, ch)

    # Keyboard navigation -----------------------------------------------------------

    def step(self, index: int, direction: str) -> int:
        """The item reached from ``index`` by an arrow key ("left", "right", "up", "down").

        Up and down keep the column where possible and cross section boundaries.
        """
        if direction == "left":
            return max(0, index - 1)
        if direction == "right":
            return min(self.count - 1, index + 1)
        s = self.section_of(index)
        section = self.sections[s]
        row, col = divmod(index - section.first, self.columns)
        if direction == "up":
            if row > 0:
                return index - self.columns
            if s == 0:
                return index
            prev = self.sections[s - 1]
            last_row = (prev.count - 1) // self.columns
            return prev.first + min(last_row * self.columns + col, prev.count - 1)
        if direction == "down":
            if row < self._rows[s] - 1:
                return min(index + self.columns, section.first + section.count - 1)
            if s == len(self.sections) - 1:
                return index
            nxt = self.sections[s + 1]
            return nxt.first + min(col, nxt.count - 1)
        raise ValueError(direction)

    # Scroll anchoring --------------------------------------------------------------

    def anchor(self, y: int) -> Anchor:
        """Describe scroll position ``y`` so it can be restored after a reflow."""
        if not self.sections:
            return Anchor(0, None, 0)
        s = self.section_at_y(y)
        rows_top = self._tops[s] + self.header_height
        if y < rows_top or self._rows[s] == 0:
            return Anchor(s, None, y - self._tops[s])
        row = min((y - rows_top) // self.row_pitch, self._rows[s] - 1)
        return Anchor(s, row * self.columns, y - rows_top - row * self.row_pitch)

    def anchor_y(self, anchor: Anchor) -> int:
        """The scroll position for ``anchor`` in this layout (same sections, any width)."""
        if not self.sections:
            return 0
        s = min(anchor.section, len(self.sections) - 1)
        if anchor.local_index is None:
            return self._tops[s] + min(anchor.offset, self.header_height)
        row = min(anchor.local_index, self.sections[s].count - 1) // self.columns
        offset = min(anchor.offset, self.row_pitch - 1)
        return self._tops[s] + self.header_height + row * self.row_pitch + offset
