"""Sorted insertion for photos a running scan adds must match the database grid order."""

import random

from imagory.db import GridPhoto
from imagory.ui.grid import insertion_index, sorts_before


def photo(pid: int, date: str | None, path: str) -> GridPhoto:
    return GridPhoto(pid, path, None, None, None, date, None)


def test_insertion_matches_full_sort():
    rng = random.Random(1)
    dates = [None, "2019-01-01T00:00:00", "2020-06-01T12:00:00", "2024-12-31T23:59:59"]
    items = [photo(i, rng.choice(dates), f"{rng.choice('aBcD')}/{i}.jpg") for i in range(300)]
    grid: list[GridPhoto] = []
    for item in items:
        grid.insert(insertion_index(grid, item), item)
    for a, b in zip(grid, grid[1:], strict=False):
        assert not sorts_before(b, a)
    assert grid[0].taken_at_local == dates[-1]
    assert grid[-1].taken_at_local is None


def test_ties_break_by_path_case_insensitively():
    a, b = photo(1, "2020-01-01T00:00:00", "B.jpg"), photo(2, "2020-01-01T00:00:00", "a.jpg")
    assert sorts_before(b, a) and not sorts_before(a, b)
