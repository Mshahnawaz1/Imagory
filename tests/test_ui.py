"""Offscreen smoke tests for the main window: choose folder, scan, grid, viewer, reopen."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths, thumbs  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from tests.imaging import make_image  # noqa: E402


@pytest.fixture
def no_dialogs(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1:]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[1:]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    return shown


@pytest.fixture
def photos(tmp_path):
    src = tmp_path / "photos"
    make_image(src / "old.jpg", (400, 300), taken="2001:01:01 00:00:00")
    make_image(src / "new.jpg", (300, 400), taken="2024:06:01 12:00:00")
    make_image(src / "mid.png", (100, 100))
    os.utime(src / "mid.png", ns=(1_300_000_000 * 10**9,) * 2)  # 2011
    return src


def open_window(qtbot):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    window = mw.MainWindow(root, conn)
    qtbot.addWidget(window)
    window.show()
    return window, conn


def wait_scan(qtbot, window):
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)


def test_first_launch_scan_browse_and_reopen(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    assert window.pages.currentIndex() == mw.PAGE_EMPTY
    assert not window.rescan_action.isEnabled()

    window.set_folder(photos)
    wait_scan(qtbot, window)
    assert no_dialogs and no_dialogs[-1][0] == "Scan finished"
    assert "Added: 3" in no_dialogs[-1][1]
    assert window.pages.currentIndex() == mw.PAGE_GRID
    names = [p.relative_path for p in window.model.photos]
    assert names == ["new.jpg", "mid.png", "old.jpg"]

    # Thumbnails load lazily through the thread pool.
    qtbot.waitUntil(lambda: all(window.loader._cache.get(p.photo_id) for p in window.model.photos))

    # Viewer: Enter opens, Right moves, Esc closes back to the same photo.
    window.grid.setCurrentIndex(window.model.index(0))
    qtbot.keyClick(window.grid, Qt.Key.Key_Return)
    assert window.pages.currentIndex() == mw.PAGE_VIEWER
    qtbot.waitUntil(lambda: not window.viewer.canvas._image.isNull())
    assert window.viewer.canvas._image.size().toTuple() == (300, 400)
    qtbot.keyClick(window.viewer, Qt.Key.Key_Right)
    assert window.viewer.row == 1
    assert "mid.png" in window.viewer.status.text()
    assert "mtime" in window.viewer.status.text()
    qtbot.keyClick(window.viewer, Qt.Key.Key_Escape)
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert window.grid.currentIndex().row() == 1
    window.close()
    conn.close()

    # Later launch: grid comes straight from the database, no scan starts.
    window, conn = open_window(qtbot)
    assert not window.scanning
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert len(window.model.photos) == 3
    window.close()
    conn.close()


def test_rescan_and_missing_thumbnail_regenerates(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    window.set_folder(photos)
    wait_scan(qtbot, window)
    thumbs.clear_thumbs(window.thumbs_root)
    (photos / "old.jpg").unlink()
    window.rescan()
    wait_scan(qtbot, window)
    assert "Missing: 1" in no_dialogs[-1][1]
    assert len(window.model.photos) == 2
    qtbot.waitUntil(lambda: all(window.loader._cache.get(p.photo_id) for p in window.model.photos))
    for p in window.model.photos:
        assert thumbs.thumb_path(window.thumbs_root, p.relative_path).exists()
    window.close()
    conn.close()


def test_change_folder_replaces_source(qtbot, photos, tmp_path, no_dialogs, monkeypatch):
    other = tmp_path / "other"
    make_image(other / "x.jpg")
    window, conn = open_window(qtbot)
    window.set_folder(photos)
    wait_scan(qtbot, window)
    monkeypatch.setattr(window, "_pick_folder", lambda: other.resolve())
    window.change_folder()
    wait_scan(qtbot, window)
    assert [p.relative_path for p in window.model.photos] == ["x.jpg"]
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    assert sorted(p.name for p in photos.iterdir()) == ["mid.png", "new.jpg", "old.jpg"]
    window.close()
    conn.close()


def test_cancel_scan_from_ui(qtbot, tmp_path, no_dialogs):
    src = tmp_path / "many"
    for i in range(60):
        make_image(src / f"{i:03}.jpg", (64, 64))
    window, conn = open_window(qtbot)
    window.set_folder(src)
    window.cancel_scan()
    wait_scan(qtbot, window)
    assert no_dialogs[-1][0] in ("Scan cancelled", "Scan finished")
    window.rescan()
    wait_scan(qtbot, window)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 60
    window.close()
    conn.close()


def test_summary_text():
    summary = mw.ScanSummary(
        "completed", db.ScanCounts(10, 5, 1, 4, 2, 1, {".mp4": 3, ".txt": 1}), 1
    )
    text = mw.summary_text(summary)
    assert "Added: 5" in text and "Errors (unreadable files): 1" in text
    assert "Skipped (not supported): 4 (.mp4 3, .txt 1)" in text
