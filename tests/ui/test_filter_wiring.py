"""The filter bar in the main window: filtered grid, counts, and scan rows (PRD §8.7)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from imagory.ui.grid import photo_matches, view_queries  # noqa: E402
from tests.imaging import make_image  # noqa: E402


def snapshot(folder):
    return {
        p.relative_to(folder): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
    }


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


def open_window(qtbot):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    window = mw.MainWindow(root, conn)
    qtbot.addWidget(window)
    window.resize(900, 700)
    window.show()
    return window, conn


@pytest.fixture
def photos_dir(tmp_path):
    src = tmp_path / "pics"
    for i in range(6):
        make_image(src / "beach" / f"b{i}.jpg", (32, 24), taken=f"2020:01:{i + 1:02} 10:00:00")
    for i in range(4):
        make_image(src / "city" / f"c{i}.jpg", (32, 24), taken=f"2021:05:{i + 1:02} 10:00:00")
    return src


def wait_loaded(qtbot, window):
    qtbot.waitUntil(lambda: window.grid_source.complete, timeout=10_000)


def test_filter_bar_drives_grid_counts_and_status(qtbot, photos_dir):
    before = snapshot(photos_dir)
    window, conn = open_window(qtbot)
    assert not window.filter_bar.isVisible()  # nothing to filter yet
    window.source_actions.add_source(photos_dir)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert window.filter_bar.isVisible()
    assert len(window.photos) == 10
    assert "10 photos in 1 source" in window.status_label.text()

    window.filter_bar._text.setText("beach")
    window.filter_bar._debounce.stop()
    window.filter_bar._emit_if_changed()
    wait_loaded(qtbot, window)
    assert len(window.photos) == 6
    assert all("beach" in p.relative_path for p in window.photos)
    assert [s.label for s in window.grid.grid_layout.sections] == ["January 2020"]
    assert window.status_label.text().startswith("6 photos match the filter")

    window.filter_bar._text.setText("nothing-like-this")
    window.filter_bar._emit_if_changed()
    assert window.photos == []
    assert "No photos match" in window.grid._empty_text

    window.filter_bar.clear()
    wait_loaded(qtbot, window)
    assert window.photos == db.grid_photos(conn)
    assert "10 photos in 1 source" in window.status_label.text()
    window.close()
    conn.close()
    assert snapshot(photos_dir) == before


def test_removing_a_filtered_source_resets_the_filter(qtbot, photos_dir):
    window, conn = open_window(qtbot)
    source = window.source_actions.add_source(photos_dir)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    window.filter_bar.set_filter(db.PhotoFilter(source_ids=frozenset({source.id})))
    assert len(window.photos) == 10
    window.source_actions.remove_source(source.id)
    assert window.photos == []
    assert window.current_filter().is_empty
    window.close()
    conn.close()


def test_scan_rows_follow_the_filter(qtbot, photos_dir, tmp_path):
    window, conn = open_window(qtbot)
    window.filter_bar.set_filter(db.PhotoFilter(text="city"))
    window.source_actions.add_source(photos_dir)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert len(window.photos) == 4
    assert all("city" in p.relative_path for p in window.photos)
    window.close()
    conn.close()


def photo(path, taken, source_id=1):
    return db.GridPhoto(1, source_id, path, 4, 3, "JPEG", taken, "exif")


def test_photo_matches_agrees_with_the_filter_fields():
    p = photo("Trips/Beach/IMG_1.jpg", "2020-01-05T10:00:00")
    assert photo_matches(db.PhotoFilter(), p)
    assert photo_matches(db.PhotoFilter(text="beach img"), p)
    assert not photo_matches(db.PhotoFilter(text="city"), p)
    assert photo_matches(db.PhotoFilter(date_from="2020-01-05", date_to="2020-01-05"), p)
    assert not photo_matches(db.PhotoFilter(date_from="2020-01-06"), p)
    assert not photo_matches(db.PhotoFilter(date_to="2020-01-04"), p)
    assert not photo_matches(db.PhotoFilter(date_from="2020-01-01"), photo("a.jpg", None))
    assert photo_matches(db.PhotoFilter(source_ids=frozenset({1})), p)
    assert not photo_matches(db.PhotoFilter(source_ids=frozenset({2})), p)
    assert photo_matches(db.PhotoFilter(folder=(1, "trips/beach")), p)
    assert not photo_matches(db.PhotoFilter(folder=(1, "Trips/Bea")), p)
    assert not photo_matches(db.PhotoFilter(missing=True), p)
    assert not photo_matches(db.PhotoFilter(has_duplicates=True), p)
    assert not photo_matches(db.PhotoFilter(album_id=1), p)


def test_view_queries_use_plain_queries_for_the_empty_filter():
    rows, counts, accepts = view_queries(db.PhotoFilter())
    assert counts is db.month_counts and accepts is None
    _rows, counts, accepts = view_queries(db.PhotoFilter(text="x"))
    assert counts is not db.month_counts and accepts is not None
