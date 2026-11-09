"""The database-backed grid: month counts, data source, thumbnail provider, main window."""

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths, thumbs  # noqa: E402
from imagory.ui import grid  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from imagory.ui.grid import PhotoDataSource, PhotoThumbnails, sorts_before  # noqa: E402
from imagory.ui.grid_layout import build_sections  # noqa: E402
from imagory.ui.photo_grid import PhotoGrid  # noqa: E402
from tests.db_helpers import count  # noqa: E402
from tests.imaging import make_image  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


def snapshot(folder):
    return {
        p.relative_to(folder): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
    }


def add_photo(conn, source_id, path, taken):
    file_id = db.insert_file(
        conn, source_id, db.FileRecord(path, 10, 1, 4, 3, "JPEG", taken or "x", "exif"), NOW
    )
    if taken is None:
        conn.execute(
            "UPDATE photos SET taken_at_local = NULL, date_source = NULL WHERE primary_file_id = ?",
            (file_id,),
        )
    return file_id


@pytest.fixture
def library_db(tmp_path):
    db_file = tmp_path / "grid.db"
    conn = db.open_database(db_file)
    yield conn, db_file
    conn.close()


def fill(conn, n, seed=1):
    """``n`` photos over a few months and some without a date, in two sources."""
    rng = random.Random(seed)
    a = db.create_source(conn, "/a", "A")
    b = db.create_source(conn, "/b", "B")
    months = ["2019-03", "2019-12", "2020-01", "2024-07"]
    with db.transaction(conn):
        for i in range(n):
            taken = None if i % 7 == 0 else f"{rng.choice(months)}-{rng.randint(10, 28)}T12:00:00"
            add_photo(conn, rng.choice((a.id, b.id)), f"{rng.choice('aBc')}/{i}.jpg", taken)
    return a, b


# --- db.month_counts ---------------------------------------------------------------------


def test_month_counts_match_grid_rows_and_unknown_is_last(library_db):
    conn, _ = library_db
    a, _ = fill(conn, 200)
    # A missing file is hidden from the grid, so it is not counted either.
    missing = add_photo(conn, a.id, "gone.jpg", "2030-01-01T00:00:00")
    db.mark_missing(conn, [missing])
    # A photo without a primary file is not in the grid.
    conn.execute("INSERT INTO photos (taken_at_local) VALUES ('2031-01-01T00:00:00')")

    counts = db.month_counts(conn)
    rows = db.grid_photos(conn)
    expected: dict = {}
    for p in rows:
        key = p.taken_at_local[:7] if p.taken_at_local else None
        expected[key] = expected.get(key, 0) + 1
    assert dict(counts) == expected
    assert [m for m, _ in counts] == ["2024-07", "2020-01", "2019-12", "2019-03", None]
    assert sum(n for _, n in counts) == len(rows)
    # The flat grid order of the rows matches the section order of the counts.
    sections = build_sections(grid.parse_month_counts(counts).items())
    for section in sections:
        for p in rows[section.first : section.first + section.count]:
            assert grid.month_key(p.taken_at_local) == section.key


def test_month_counts_without_unknown_or_photos(library_db):
    conn, _ = library_db
    assert db.month_counts(conn) == []
    a = db.create_source(conn, "/a", "A")
    add_photo(conn, a.id, "x.jpg", "2020-05-01T00:00:00")
    add_photo(conn, a.id, "y.jpg", "2020-05-02T00:00:00")
    assert db.month_counts(conn) == [("2020-05", 2)]


def test_month_counts_leave_out_hidden_photos(library_db):
    conn, _ = library_db
    a = db.create_source(conn, "/a", "A")
    add_photo(conn, a.id, "keep.jpg", "2020-05-31T23:59:59")
    add_photo(conn, a.id, "first.jpg", "2020-05-01T00:00:00")
    add_photo(conn, a.id, "june.jpg", "2020-06-01T00:00:00")
    gone = [
        add_photo(conn, a.id, "gone-may.jpg", "2020-05-10T00:00:00"),
        add_photo(conn, a.id, "gone-july.jpg", "2020-07-01T00:00:00"),  # its only photo
        add_photo(conn, a.id, "gone-undated.jpg", None),
    ]
    db.mark_missing(conn, gone)
    add_photo(conn, a.id, "undated.jpg", None)
    conn.execute("INSERT INTO photos (taken_at_local) VALUES ('2020-06-02T00:00:00')")
    conn.execute("INSERT INTO photos (taken_at_local) VALUES (NULL)")
    assert db.month_counts(conn) == [("2020-06", 1), ("2020-05", 2), (None, 1)]
    db.mark_missing(conn, [f.id for f in db.load_file_states(conn, a.id)])
    assert db.month_counts(conn) == [] and db.grid_photos(conn) == []


# --- PhotoDataSource ---------------------------------------------------------------------


def make_source(conn, db_file, qtbot, first_page=None, **kwargs):
    source = PhotoDataSource(conn, db_file, first_page=first_page, **kwargs)
    source.load()
    if not source.complete:
        with qtbot.waitSignal(source.completed, timeout=10_000):
            pass
    return source


def section_keys(source):
    return [s.key for s in build_sections(source.month_counts())]


def test_data_source_first_page_then_rest(library_db, qtbot):
    conn, db_file = library_db
    fill(conn, 120)
    source = PhotoDataSource(conn, db_file, first_page=10)
    source.load()
    assert not source.complete and len(source.photos) == 10
    assert source.count() == 120  # known from the counts before the rest arrives
    assert source.item(50) is None and source.tooltip(50) == ""
    with qtbot.waitSignal(source.completed, timeout=10_000):
        pass
    assert source.photos == db.grid_photos(conn)
    assert source.count() == 120
    assert source.index_of(source.photos[77].photo_id) == 77
    tip = source.tooltip(0)
    assert source.photos[0].relative_path in tip
    source.wait()


def test_data_source_inserts_scan_rows_in_order_and_updates_counts(library_db, qtbot):
    conn, db_file = library_db
    fill(conn, 80)
    everything = db.grid_photos(conn)
    rng = random.Random(3)
    shown = set(rng.sample(range(len(everything)), 20))
    source = make_source(
        conn,
        db_file,
        qtbot,
        rows=lambda c, limit, offset: [p for i, p in enumerate(everything) if i in shown],
    )
    assert source.count() == 20
    rest = [p for i, p in enumerate(everything) if i not in shown]
    rng.shuffle(rest)
    for batch in (rest[:25], rest[25:]):
        before = list(source.photos)
        positions = source.insert(batch)
        assert positions == sorted(positions) and len(positions) == len(batch)
        assert sorted(source.photos[i].photo_id for i in positions) == sorted(
            p.photo_id for p in batch
        )
        assert [p for i, p in enumerate(source.photos) if i not in positions] == before
    assert source.photos == everything
    assert source.insert(everything[:5]) == []  # already shown: ignored
    assert source.count() == len(everything)
    assert sum(n for _, n in source.month_counts()) == len(everything)
    assert section_keys(source)[-1] is None
    for a, b in zip(source.photos, source.photos[1:], strict=False):
        assert not sorts_before(b, a)


def test_data_source_scan_rows_before_the_rest_arrives(library_db, qtbot):
    conn, db_file = library_db
    fill(conn, 60)
    everything = db.grid_photos(conn)
    late = [everything[2], everything[40], everything[59]]  # committed by a running scan
    present = [p for p in everything if p not in late]
    source = PhotoDataSource(
        conn, db_file, first_page=10, rows=lambda c, limit, offset: present[offset:][:limit]
    )
    source.load()
    assert source.count() == len(everything)  # the DB counts already include them
    positions = source.insert(late)
    assert positions == [2]  # the others belong past the loaded rows: held back
    with qtbot.waitSignal(source.completed, timeout=10_000) as blocker:
        pass
    assert blocker.args == [[40, 59]]  # where the held-back photos went
    assert source.photos == everything
    assert source.count() == len(everything)  # recounted from the rows
    source.wait()


@pytest.mark.parametrize("f", [db.PhotoFilter(), db.PhotoFilter(date_from="2020-01-01")])
def test_data_source_takes_the_filtered_queries(library_db, qtbot, f):
    conn, db_file = library_db
    fill(conn, 90)
    source = make_source(
        conn,
        db_file,
        qtbot,
        first_page=10,
        rows=lambda c, limit, offset: db.filtered_grid_photos(c, f, limit, offset),
        counts=lambda c: db.filtered_month_counts(c, f),
    )
    assert source.photos == db.filtered_grid_photos(conn, f)
    assert source.count() == count(conn, f) == len(source.photos)
    if f == db.PhotoFilter():
        assert db.filtered_month_counts(conn, f) == db.month_counts(conn)
    source.wait()


def test_data_source_accepts_filters_scan_rows(library_db, qtbot):
    conn, db_file = library_db
    fill(conn, 30)
    everything = db.grid_photos(conn)
    source = make_source(
        conn,
        db_file,
        qtbot,
        rows=lambda c, limit, offset: [],
        counts=lambda c: [],
        accepts=lambda p: p.taken_at_local is None,
    )
    source.insert(everything)
    assert source.photos and all(p.taken_at_local is None for p in source.photos)
    assert section_keys(source) == [None]


# --- PhotoThumbnails ---------------------------------------------------------------------


def test_thumbnails_cache_is_limited_and_regenerates_missing(tmp_path, qtbot):
    src = tmp_path / "photos"
    for i in range(6):
        make_image(src / f"{i}.jpg", (200, 100), taken=f"2020:01:0{i + 1} 00:00:00")
    before = snapshot(src)
    thumbs_root = tmp_path / "thumbs"
    source = db.Source(1, "A", str(src), "plain", False, NOW, None)
    photos = [
        db.GridPhoto(
            i,
            1,
            f"{i}.jpg",
            200,
            100,
            "JPEG",
            "2020-01-01T00:00:00",
            "exif",
            thumbs.hash_bytes((src / f"{i}.jpg").read_bytes()),
        )
        for i in range(6)
    ]
    provider = PhotoThumbnails(thumbs_root, {1: source}, cache_items=3)
    assert provider.thumbnail(0, None) is None  # not loaded yet: nothing to do
    for i, photo in enumerate(photos):
        with qtbot.waitSignal(provider.ready, timeout=10_000):
            assert provider.thumbnail(i, photo) is None
        pix = provider.thumbnail(i, photo)
        assert pix is not None and max(pix.width(), pix.height()) == mw_icon_size()
    assert provider.cached_count() == 3
    assert [provider.cached(i) is not None for i in range(6)] == [False] * 3 + [True] * 3
    assert all(thumbs.thumb_path(thumbs_root, p.content_hash).exists() for p in photos)
    assert snapshot(src) == before  # the source folder is never written to

    # A photo whose source is unknown (being removed) is never loaded.
    stray = db.GridPhoto(99, 2, "x.jpg", 1, 1, "JPEG", None, None)
    assert provider.thumbnail(0, stray) is None and provider.cached(99) is None
    # An unreadable photo gets the "broken" placeholder instead of a retry loop.
    bad = db.GridPhoto(50, 1, "missing.jpg", 1, 1, "JPEG", None, None)
    with qtbot.waitSignal(provider.ready, timeout=10_000):
        provider.thumbnail(0, bad)
    assert provider.thumbnail(0, bad) is not None and provider.cached(50) is None
    provider.wait()


def mw_icon_size():
    from imagory.ui.photo_grid import ICON_SIZE

    return ICON_SIZE


# --- PhotoGrid.items_inserted ------------------------------------------------------------


class ListSource:
    def __init__(self, n):
        self.n = n

    def month_counts(self):
        return [((2020, 1), self.n)]

    def item(self, index):
        return index

    def tooltip(self, index):
        return ""


def test_items_inserted_keeps_selection_current_and_top_item(qtbot):
    widget = PhotoGrid()
    qtbot.addWidget(widget)
    widget.resize(800, 600)
    widget.show()
    source = ListSource(500)
    widget.set_data_source(source)
    widget.click(10, Qt.KeyboardModifier.NoModifier)
    widget.click(300, Qt.KeyboardModifier.ControlModifier)
    widget.scroll_to(200, at_top=True)
    top = widget.visible_range()[0]
    assert top == 200 - 200 % widget.grid_layout.columns
    source.n = 503
    widget.items_inserted([0, 5, 250])  # two before item 10, three before 300
    assert widget.selected_indexes() == [12, 303]
    assert widget.current_index() == 303
    assert widget.visible_range()[0] == top + 3 - (top + 3) % widget.grid_layout.columns
    source.n = 504
    widget.items_inserted([])  # only the counts changed
    assert widget.selected_indexes() == [12, 303] and widget.count() == 504


# --- MainWindow --------------------------------------------------------------------------


@pytest.fixture
def no_dialogs(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1:]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[1:]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    return shown


def open_window(qtbot):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    window = mw.MainWindow(root, conn)
    qtbot.addWidget(window)
    window.resize(900, 700)
    window.show()
    return window, conn


@pytest.fixture
def many(tmp_path):
    src = tmp_path / "many"
    for i in range(40):
        month = 1 + i % 3
        make_image(src / f"{i:03}.jpg", (32, 24), taken=f"2020:{month:02}:{i % 28 + 1:02} 10:00:00")
    return src


def test_window_shows_month_sections_selection_and_viewer(qtbot, many, no_dialogs):
    before = snapshot(many)
    window, conn = open_window(qtbot)
    assert window.pages.currentIndex() == mw.PAGE_EMPTY
    window.source_actions.add_source(many)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert window.photos == db.grid_photos(conn)
    labels = [s.label for s in window.grid.grid_layout.sections]
    assert labels == ["March 2020", "February 2020", "January 2020"]
    assert "40 photos in 1 source" in window.status_label.text()

    # The visible thumbnails load from the cache.
    window.grid.viewport().repaint()
    first, end = window.grid.visible_range()
    qtbot.waitUntil(
        lambda: all(window.thumbnails.cached(p.photo_id) for p in window.photos[first:end])
    )

    # Selection: click, Ctrl-click, Shift-click, Ctrl+A; the status bar counts it.
    window.grid.setFocus()
    window.grid.click(2, Qt.KeyboardModifier.NoModifier)
    window.grid.click(4, Qt.KeyboardModifier.ShiftModifier)
    window.grid.click(9, Qt.KeyboardModifier.ControlModifier)
    assert window.grid.selected_indexes() == [2, 3, 4, 9]
    assert "4 selected" in window.status_label.text()
    qtbot.keyClick(window.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert window.grid.selected_count() == 40

    # Tooltip text names the file and date.
    assert window.photos[5].relative_path in window.grid_source.tooltip(5)

    # Double-click opens the viewer; Back returns to the same photo.
    window.grid.scroll_to(7)
    qtbot.mouseDClick(
        window.grid.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.grid.visual_rect(7).center(),
    )
    assert window.pages.currentIndex() == mw.PAGE_VIEWER
    assert window.viewer.row == 7
    qtbot.keyClick(window.viewer, Qt.Key.Key_Right)
    qtbot.keyClick(window.viewer, Qt.Key.Key_Escape)
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert window.grid.current_index() == 8
    assert window.grid.selected_indexes() == [8]

    # Enter opens the current photo.
    qtbot.keyClick(window.grid, Qt.Key.Key_Return)
    assert window.pages.currentIndex() == mw.PAGE_VIEWER and window.viewer.row == 8
    window.close()
    conn.close()
    assert snapshot(many) == before


def test_reload_keeps_top_photo_in_view(qtbot, many, no_dialogs, monkeypatch):
    monkeypatch.setattr(grid, "FIRST_PAGE", 5)  # the anchor arrives with the background rows
    window, conn = open_window(qtbot)
    window.source_actions.add_source(many)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    qtbot.waitUntil(lambda: window.grid_source.complete)
    window.grid.scroll_to(30, at_top=True)
    first = window.grid.visible_range()[0]
    anchor = window.photos[first].photo_id
    window.reload_grid()
    qtbot.waitUntil(lambda: window.grid_source.complete)
    assert window.photos[window.grid.visible_range()[0]].photo_id == anchor

    # At the very top it stays at the top.
    window.grid.verticalScrollBar().setValue(0)
    window.reload_grid()
    qtbot.waitUntil(lambda: window.grid_source.complete)
    assert window.grid.scroll_y() == 0
    window.close()
    conn.close()


def test_empty_source_shows_empty_text(qtbot, tmp_path, no_dialogs):
    empty = tmp_path / "empty"
    empty.mkdir()
    window, conn = open_window(qtbot)
    window.source_actions.add_source(empty)
    assert window.grid._empty_text == "Looking for photos…"
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert window.grid.count() == 0 and "No photos" in window.grid._empty_text
    assert "0 photos in 1 source" in window.status_label.text()
    window.close()
    conn.close()
