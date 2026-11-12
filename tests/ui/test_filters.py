"""Search and filter queries (PRD 8.7) and the filter bar widget."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imagory import db  # noqa: E402
from imagory.db import PhotoFilter  # noqa: E402
from tests.db_helpers import count  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "imagory.db")
    yield c
    c.close()


def add_source(conn, label="S") -> int:
    return conn.execute(
        "INSERT INTO sources (label, root_path, added_at) VALUES (?, ?, '2020-01-01T00:00:00Z')",
        (label, f"/x/{label}"),
    ).lastrowid


def add_photo(conn, copies, taken=None) -> int:
    """copies: list of (source_id, relative_path, status); the first 'ok' one is primary."""
    n = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    pid = conn.execute(
        "INSERT INTO photos (content_hash, width, height, format, taken_at_local, date_source) "
        "VALUES (?, 4, 3, 'JPEG', ?, 'exif')",
        (f"h{n}", taken),
    ).lastrowid
    primary = None
    for source_id, path, status in copies:
        fid = conn.execute(
            "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, status, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, 1, ?, 'z', 'z')",
            (source_id, pid, path, status),
        ).lastrowid
        if primary is None or (status == "ok" and primary[1] != "ok"):
            primary = (fid, status)
    conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (primary[0], pid))
    return pid


@pytest.fixture
def lib(conn):
    s1, s2 = add_source(conn, "A"), add_source(conn, "B")
    ids = {
        "beach": add_photo(conn, [(s1, "Trips/Beach/IMG_1.jpg", "ok")], "2020-07-01T10:00:00"),
        "dup": add_photo(
            conn,
            [(s1, "Trips/Beach/IMG_2.jpg", "ok"), (s2, "Takeout/IMG_2.jpg", "ok")],
            "2020-07-31T23:59:59",
        ),
        "alone_missing": add_photo(conn, [(s1, "Old/gone.jpg", "missing")], "2019-01-01T00:00:00"),
        "partly_missing": add_photo(
            conn,
            [(s1, "Old/half.jpg", "missing"), (s2, "Keep/half.jpg", "ok")],
            "2021-03-05T08:00:00",
        ),
        "all_missing_dup": add_photo(
            conn,
            [(s1, "Old/x.jpg", "missing"), (s2, "Old/x2.jpg", "missing")],
            "2018-05-05T00:00:00",
        ),
        "undated": add_photo(conn, [(s2, "50%_off/we_ird.jpg", "ok")], None),
        "upper": add_photo(conn, [(s2, "trips/BEACH/Sunset.PNG", "ok")], "2022-12-25T18:00:00"),
    }
    conn.commit()
    return {"s1": s1, "s2": s2, **ids}


def paths(conn, f):
    return [r.relative_path for r in db.filtered_grid_photos(conn, f)]


def pids(conn, f):
    return {r.photo_id for r in db.filtered_grid_photos(conn, f)}


def test_empty_filter_equals_grid_photos(conn, lib):
    assert db.filtered_grid_photos(conn, PhotoFilter()) == db.grid_photos(conn)
    assert db.filtered_grid_photos(conn, PhotoFilter(), 2, 1) == db.grid_photos(conn, 2, 1)
    assert PhotoFilter().is_empty and not PhotoFilter(text="a").is_empty


def test_missing_hidden_by_default_and_only_with_flag(conn, lib):
    shown = pids(conn, PhotoFilter())
    assert lib["alone_missing"] not in shown and lib["all_missing_dup"] not in shown
    assert lib["partly_missing"] in shown  # one copy is still ok
    assert pids(conn, PhotoFilter(missing=True)) == {lib["alone_missing"], lib["all_missing_dup"]}
    assert count(conn, PhotoFilter(missing=True)) == 2


def test_missing_photo_with_unset_primary_still_listed(conn, lib):
    conn.execute("UPDATE photos SET primary_file_id = NULL WHERE id = ?", (lib["all_missing_dup"],))
    rows = db.filtered_grid_photos(conn, PhotoFilter(missing=True))
    assert {r.photo_id for r in rows} == {lib["alone_missing"], lib["all_missing_dup"]}
    assert all(r.relative_path for r in rows)


def test_text_matches_name_and_folder_case_insensitively_words_anded(conn, lib):
    assert set(paths(conn, PhotoFilter(text="beach"))) == {
        "Trips/Beach/IMG_1.jpg",
        "Trips/Beach/IMG_2.jpg",
        "trips/BEACH/Sunset.PNG",
    }
    assert paths(conn, PhotoFilter(text="  sunset   BEACH ")) == ["trips/BEACH/Sunset.PNG"]
    assert paths(conn, PhotoFilter(text="img_2")) == ["Trips/Beach/IMG_2.jpg"]
    assert paths(conn, PhotoFilter(text="nothing-like-this")) == []


def test_text_escapes_like_wildcards(conn, lib):
    assert paths(conn, PhotoFilter(text="%")) == ["50%_off/we_ird.jpg"]
    assert len(paths(conn, PhotoFilter(text="_"))) == 3  # IMG_1, IMG_2, we_ird
    assert paths(conn, PhotoFilter(text="e_i")) == ["50%_off/we_ird.jpg"]
    assert paths(conn, PhotoFilter(text="w_ird")) == []  # "_" is not a wildcard
    assert paths(conn, PhotoFilter(text="we_ird")) == ["50%_off/we_ird.jpg"]
    assert paths(conn, PhotoFilter(text="\\")) == []


def test_text_matches_any_copy_but_row_shows_primary(conn, lib):
    rows = db.filtered_grid_photos(conn, PhotoFilter(text="takeout"))
    assert [(r.photo_id, r.relative_path) for r in rows] == [(lib["dup"], "Trips/Beach/IMG_2.jpg")]


def test_date_range_inclusive_and_excludes_unknown(conn, lib):
    f = PhotoFilter(date_from="2020-07-01", date_to="2020-07-31")
    assert pids(conn, f) == {lib["beach"], lib["dup"]}  # 23:59:59 on the last day counts
    assert pids(conn, PhotoFilter(date_from="2022-01-01")) == {lib["upper"]}
    assert pids(conn, PhotoFilter(date_to="2020-07-01")) == {lib["beach"]}
    assert lib["undated"] not in pids(conn, PhotoFilter(date_from="1900-01-01"))
    assert lib["undated"] not in pids(conn, PhotoFilter(date_to="2999-01-01"))
    assert lib["undated"] in pids(conn, PhotoFilter())


def test_source_filter(conn, lib):
    only_b = pids(conn, PhotoFilter(source_ids=frozenset({lib["s2"]})))
    assert only_b == {lib["dup"], lib["partly_missing"], lib["undated"], lib["upper"]}
    both = pids(conn, PhotoFilter(source_ids=frozenset({lib["s1"], lib["s2"]})))
    assert both == pids(conn, PhotoFilter())
    # a missing-only copy in the source does not make a visible photo match
    assert lib["partly_missing"] not in pids(conn, PhotoFilter(source_ids=frozenset({lib["s1"]})))
    # ... but the Missing filter looks at all copies
    f = PhotoFilter(source_ids=frozenset({lib["s2"]}), missing=True)
    assert pids(conn, f) == {lib["all_missing_dup"]}


def test_folder_filter_prefix_subfolders_nocase(conn, lib):
    s1, s2 = lib["s1"], lib["s2"]
    assert pids(conn, PhotoFilter(folder=(s1, "Trips"))) == {lib["beach"], lib["dup"]}
    assert pids(conn, PhotoFilter(folder=(s1, "trips/beach/"))) == {lib["beach"], lib["dup"]}
    assert pids(conn, PhotoFilter(folder=(s2, "TRIPS"))) == {lib["upper"]}
    assert pids(conn, PhotoFilter(folder=(s1, "Trip"))) == set()  # whole path segments only
    assert pids(conn, PhotoFilter(folder=(s2, "50%_off"))) == {lib["undated"]}
    assert pids(conn, PhotoFilter(folder=(s2, "5%_off"))) == set()
    assert pids(conn, PhotoFilter(folder=(s1, ""))) == pids(
        conn, PhotoFilter(source_ids=frozenset({s1}))
    )


def test_has_duplicates(conn, lib):
    f = PhotoFilter(has_duplicates=True)
    assert pids(conn, f) == {lib["dup"], lib["partly_missing"]}
    assert pids(conn, PhotoFilter(has_duplicates=True, missing=True)) == {lib["all_missing_dup"]}


def test_filters_combine_with_and(conn, lib):
    f = PhotoFilter(text="beach", date_from="2020-01-01", date_to="2020-12-31", has_duplicates=True)
    assert pids(conn, f) == {lib["dup"]}
    f = PhotoFilter(text="img", source_ids=frozenset({lib["s2"]}))
    assert pids(conn, f) == {lib["dup"]}
    # the same copy must satisfy source and text together
    f = PhotoFilter(text="beach", source_ids=frozenset({lib["s2"]}))
    assert pids(conn, f) == {lib["upper"]}


def test_month_counts_match_rows(conn, lib):
    filters = [
        PhotoFilter(),
        PhotoFilter(missing=True),
        PhotoFilter(text="beach"),
        PhotoFilter(date_from="2020-01-01"),
        PhotoFilter(has_duplicates=True),
        PhotoFilter(source_ids=frozenset({lib["s2"]})),
    ]
    for f in filters:
        rows = db.filtered_grid_photos(conn, f)
        counts = db.filtered_month_counts(conn, f)
        assert sum(n for _, n in counts) == len(rows) == count(conn, f)
        expected: dict = {}
        for r in rows:
            m = r.taken_at_local[:7] if r.taken_at_local else None
            expected[m] = expected.get(m, 0) + 1
        assert dict(counts) == expected
        months = [m for m, _ in counts if m is not None]
        assert months == sorted(months, reverse=True)
        assert all(m is not None for m, _ in counts[:-1])
    full = db.filtered_month_counts(conn, PhotoFilter())
    assert full[0] == ("2022-12", 1) and full[-1] == (None, 1)


def test_folder_paths(conn, lib):
    assert db.folder_paths(conn, lib["s1"]) == ["Old", "Trips", "Trips/Beach"]
    assert db.folder_paths(conn, lib["s2"]) == [
        "50%_off",
        "Keep",
        "Old",
        "Takeout",
        "trips",
        "trips/BEACH",
    ]


def test_bad_dates_raise(conn):
    with pytest.raises(ValueError):
        db.filtered_grid_photos(conn, PhotoFilter(date_from="nope"))


def test_no_sort_step_without_text(conn, lib):
    """Without text the walk is in taken_at_local order, so a LIMIT can stop early."""
    for f in (PhotoFilter(), PhotoFilter(date_from="2000-01-01"), PhotoFilter(has_duplicates=True)):
        sql, params = db.grid._filter_sql(f, ordered=True)
        order = "ORDER BY p.taken_at_local DESC, f.relative_path, f.source_id LIMIT 200"
        query = f"EXPLAIN QUERY PLAN SELECT p.id {sql} {order}"
        plan = " ".join(r[3] for r in conn.execute(query, params))
        assert "INDEX idx_photos_taken_at_local" in plan, plan
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan  # only the tie-break is sorted


# --- filter bar widget ---------------------------------------------------------------------


@pytest.fixture
def bar(qtbot):
    from imagory.ui.filter_bar import FilterBar

    widget = FilterBar(
        sources=lambda: [(1, "Phone"), (2, "Takeout")],
        folders=lambda sid: ["2020", "2020/Trip"] if sid == 1 else ["Google Photos"],
    )
    qtbot.addWidget(widget)
    widget.show()
    return widget


def collect(bar):
    seen: list[PhotoFilter] = []
    bar.filterChanged.connect(seen.append)
    return seen


def test_bar_starts_empty(bar):
    assert bar.filter() == PhotoFilter()


def test_bar_text_is_debounced(bar, qtbot):
    seen = collect(bar)
    for text in ("b", "be", " beach "):
        bar._text.setText(text)
    assert seen == []
    qtbot.waitUntil(lambda: len(seen) == 1, timeout=2000)
    assert seen == [PhotoFilter(text="beach")]


def test_bar_toggles_emit_at_once_and_flush_text(bar):
    seen = collect(bar)
    bar._text.setText("abc")
    bar._duplicates.setChecked(True)
    assert seen == [PhotoFilter(text="abc", has_duplicates=True)]
    bar._missing.setChecked(True)
    assert seen[-1] == PhotoFilter(text="abc", has_duplicates=True, missing=True)
    assert len(seen) == 2


def test_bar_dates(bar):
    from PySide6.QtCore import QDate

    seen = collect(bar)
    bar._date_from.setDate(QDate(2020, 1, 31))
    bar._date_to.setDate(QDate(2020, 2, 29))
    assert seen[-1] == PhotoFilter(date_from="2020-01-31", date_to="2020-02-29")
    bar._date_from.setDate(bar._date_from.minimumDate())  # clearing a date
    assert seen[-1] == PhotoFilter(date_to="2020-02-29")


def test_bar_sources_and_folders(bar):
    seen = collect(bar)
    assert not bar._folder.isEnabled()  # several sources, none picked
    bar._source_menu.actions()[0].setChecked(True)
    assert seen[-1] == PhotoFilter(source_ids=frozenset({1}))
    assert bar._folder.isEnabled()
    assert [bar._folder.itemData(i) for i in range(bar._folder.count())] == [
        "",
        "2020",
        "2020/Trip",
    ]
    bar._folder.setCurrentIndex(2)
    assert seen[-1] == PhotoFilter(source_ids=frozenset({1}), folder=(1, "2020/Trip"))
    bar._source_menu.actions()[1].setChecked(True)  # two sources: folder scope is gone
    assert seen[-1] == PhotoFilter(source_ids=frozenset({1, 2}))
    assert not bar._folder.isEnabled()


def test_bar_set_filter_roundtrip_and_clear(bar):
    seen = collect(bar)
    f = PhotoFilter(
        text="x y",
        date_from="2019-05-06",
        source_ids=frozenset({1}),
        folder=(1, "2020/Trip"),
        has_duplicates=True,
        missing=True,
    )
    bar.set_filter(f)
    assert bar.filter() == f
    assert seen == [f]
    bar.set_filter(f)  # unchanged: no second signal
    assert seen == [f]
    bar._clear.click()
    assert bar.filter() == PhotoFilter() and seen[-1] == PhotoFilter()
    assert not bar._clear.isEnabled()


def test_bar_folder_pins_source(bar):
    bar.set_filter(PhotoFilter(folder=(2, "Google Photos")))
    assert bar.filter() == PhotoFilter(source_ids=frozenset({2}), folder=(2, "Google Photos"))


def test_bar_refresh_keeps_checked_sources(bar):
    bar._source_menu.actions()[1].setChecked(True)
    bar.set_providers(sources=lambda: [(2, "Takeout"), (3, "New")])
    assert bar.filter().source_ids == frozenset({2})
    bar.set_providers(sources=lambda: [(3, "New")])
    assert bar.filter() == PhotoFilter()


# --- album filter and the real scanner -----------------------------------------------------


def test_album_filter(conn, lib):
    album = db.create_album(conn, "Trip")
    other = db.create_album(conn, "Empty")
    db.add_photos_to_album(conn, album, [lib["beach"], lib["dup"], lib["alone_missing"]])
    assert pids(conn, PhotoFilter(album_id=album)) == {lib["beach"], lib["dup"]}
    assert pids(conn, PhotoFilter(album_id=other)) == set()
    assert pids(conn, PhotoFilter(album_id=album, missing=True)) == {lib["alone_missing"]}
    f = PhotoFilter(album_id=album, has_duplicates=True)
    assert pids(conn, f) == {lib["dup"]}


def test_real_scan_with_duplicates_and_missing(tmp_path):
    from imagory import paths
    from imagory.scanner import Scanner
    from tests.imaging import make_image

    root = paths.ensure_library_dir()
    c = db.open_database(paths.db_path(root))
    a, b = tmp_path / "a", tmp_path / "b"
    img = make_image(a / "Trip" / "x.jpg", (40, 30), taken="2020:05:05 10:00:00")
    (b / "Copy").mkdir(parents=True)
    (b / "Copy" / "x.jpg").write_bytes(img.read_bytes())
    make_image(a / "y.jpg", (30, 40), taken="2021:01:01 10:00:00")
    make_image(a / "gone.jpg", (20, 20), taken="2019:01:01 10:00:00")
    sa = db.create_source(c, str(a), "A")
    sb = db.create_source(c, str(b), "B")
    for s in (sa, sb):
        Scanner(c, s, paths.thumbs_dir(root), root, workers=2).run()
    (a / "gone.jpg").unlink()
    Scanner(c, sa, paths.thumbs_dir(root), root, workers=2).run()

    assert db.filtered_grid_photos(c, PhotoFilter()) == db.grid_photos(c)
    assert len(db.grid_photos(c)) == 2
    assert db.filtered_month_counts(c, PhotoFilter()) == [("2021-01", 1), ("2020-05", 1)]
    dup = db.filtered_grid_photos(c, PhotoFilter(has_duplicates=True))
    assert [r.relative_path for r in dup] == ["Trip/x.jpg"]
    gone = db.filtered_grid_photos(c, PhotoFilter(missing=True))
    assert [r.relative_path for r in gone] == ["gone.jpg"]
    assert count(c, PhotoFilter(folder=(sb.id, "copy"))) == 1
    c.close()


def test_calendar_of_unset_date_opens_on_start_day(bar, qtbot):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from imagory.ui.filter_bar import CALENDAR_START, NO_DATE

    cal = bar._date_from.calendarWidget()
    QApplication.sendEvent(cal, QEvent(QEvent.Type.Show))
    assert (cal.yearShown(), cal.monthShown()) == (CALENDAR_START.year(), CALENDAR_START.month())
    assert bar._date_from.date() == NO_DATE  # still "Any"
    assert bar.filter().date_from is None


def test_chips_show_and_remove_active_filters(bar, qtbot):
    from PySide6.QtWidgets import QPushButton

    assert bar._chips.isHidden()
    bar._missing.setChecked(True)
    assert not bar._chips.isHidden()
    chips = [b for b in bar._chips.findChildren(QPushButton) if b.objectName() == "chip"]
    assert [c.text() for c in chips] == ["Missing  ✕"]
    chips[0].click()
    assert not bar._missing.isChecked() and bar._chips.isHidden()
