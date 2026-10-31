"""Offscreen smoke tests for the main window: choose folder, scan, grid, viewer, reopen."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths, thumbs  # noqa: E402
from imagory.ui import grid  # noqa: E402
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

    window.add_source(photos)
    wait_scan(qtbot, window)
    assert no_dialogs and no_dialogs[-1][0] == "Scan finished"
    assert "Added: 3" in no_dialogs[-1][1]
    assert window.pages.currentIndex() == mw.PAGE_GRID
    names = [p.relative_path for p in window.photos]
    assert names == ["new.jpg", "mid.png", "old.jpg"]

    # Thumbnails load lazily through the thread pool.
    qtbot.waitUntil(lambda: all(window.thumbnails.cached(p.photo_id) for p in window.photos))

    # Viewer: Enter opens, Right moves, Esc closes back to the same photo.
    window.grid.set_current_index(0)
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
    assert window.grid.current_index() == 1
    window.close()
    conn.close()

    # Later launch: grid comes straight from the database, no scan starts.
    window, conn = open_window(qtbot)
    assert not window.scanning
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert len(window.photos) == 3
    window.close()
    conn.close()


def test_rescan_and_missing_thumbnail_regenerates(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    window.add_source(photos)
    wait_scan(qtbot, window)
    thumbs.clear_thumbs(window.thumbs_root)
    (photos / "old.jpg").unlink()
    window.rescan()
    wait_scan(qtbot, window)
    assert "Missing: 1" in no_dialogs[-1][1]
    assert len(window.photos) == 2
    qtbot.waitUntil(lambda: all(window.thumbnails.cached(p.photo_id) for p in window.photos))
    folder = thumbs.source_thumbs_dir(window.thumbs_root, window.sources[0].id)
    for p in window.photos:
        assert thumbs.thumb_path(folder, p.relative_path).exists()
    window.close()
    conn.close()


def test_two_sources_remove_one(qtbot, photos, tmp_path, no_dialogs):
    other = tmp_path / "other"
    make_image(other / "x.jpg", taken="2010:01:01 00:00:00")
    window, conn = open_window(qtbot)
    window.add_source(photos, "Phone")
    wait_scan(qtbot, window)
    second = window.add_source(other, "Camera")
    wait_scan(qtbot, window)
    assert [s.label for s in window.sources] == ["Phone", "Camera"]
    names = [p.relative_path for p in window.photos]
    assert names == ["new.jpg", "mid.png", "x.jpg", "old.jpg"]
    assert "4 photos in 2 sources" in window.status_label.text()

    # The viewer finds each photo in its own source and names the source.
    window.open_viewer(2)
    qtbot.waitUntil(lambda: not window.viewer.canvas._image.isNull())
    assert "Camera" in window.viewer.status.text()

    # Rescan all reports each source under its label.
    window.rescan()
    wait_scan(qtbot, window)
    assert no_dialogs[-1][0] == "Scan finished"
    assert "Phone\nFound: 3" in no_dialogs[-1][1] and "Camera\nFound: 1" in no_dialogs[-1][1]

    window.remove_source(second.id)
    assert window.pages.currentIndex() == mw.PAGE_GRID
    assert [p.relative_path for p in window.photos] == ["new.jpg", "mid.png", "old.jpg"]
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert (other / "x.jpg").exists()  # files on disk are never touched
    window.remove_source(window.sources[0].id)
    assert window.pages.currentIndex() == mw.PAGE_EMPTY
    assert sorted(p.name for p in photos.iterdir()) == ["mid.png", "new.jpg", "old.jpg"]
    window.close()
    conn.close()


def test_unavailable_source_is_reported_and_others_scan(qtbot, photos, tmp_path, no_dialogs):
    other = tmp_path / "usb"
    make_image(other / "x.jpg")
    window, conn = open_window(qtbot)
    window.add_source(photos, "Phone")
    wait_scan(qtbot, window)
    window.add_source(other, "USB")
    wait_scan(qtbot, window)
    moved = tmp_path / "usb-moved"
    other.rename(moved)  # the drive letter changed
    make_image(photos / "later.jpg")
    window.rescan()
    wait_scan(qtbot, window)
    title, text = no_dialogs[-1]
    assert title == "Scan finished with problems"
    assert "USB\nNot scanned: Folder not found" in text and "Relocate" in text
    assert "Phone\nFound: 4" in text
    assert len(window.photos) == 5  # nothing was marked missing

    usb = next(s for s in window.sources if s.label == "USB")
    window.relocate_source(usb.id, moved)
    wait_scan(qtbot, window)
    assert window.sources_by_id[usb.id].root_path == str(moved)
    assert "Unchanged: 1" in no_dialogs[-1][1] and "Missing: 0" in no_dialogs[-1][1]
    window.close()
    conn.close()


def test_rename_and_change_type(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    source = window.add_source(photos)
    assert (source.label, source.type, source.type_overridden) == ("photos", "plain", False)
    wait_scan(qtbot, window)
    window.rename_source(source.id, "Holidays")
    window.change_source_type(source.id, "takeout")
    updated = window.sources_by_id[source.id]
    assert (updated.label, updated.type, updated.type_overridden) == ("Holidays", "takeout", True)
    window.close()
    conn.close()


def test_sources_dialog_lists_sources(qtbot, photos, no_dialogs):
    from imagory.ui.sources_dialog import SourcesDialog

    window, conn = open_window(qtbot)
    window.add_source(photos, "Phone")
    assert window.scanning
    dialog = SourcesDialog(window)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 1
    assert dialog.selected_source().label == "Phone"
    assert not dialog.remove_button.isEnabled()  # not while a scan writes
    wait_scan(qtbot, window)
    assert dialog.remove_button.isEnabled()
    assert dialog.table.item(0, 3).text() == "3"
    assert dialog.table.item(0, 5).text() != "Never"
    window.rename_source(window.sources[0].id, "Phone backup")
    assert dialog.table.item(0, 0).text() == "Phone backup"
    window.close()
    conn.close()


def test_add_source_dialog_shows_detection(qtbot, tmp_path):
    from imagory.takeout import Detection
    from imagory.ui.sources_dialog import AddSourceDialog

    folder = tmp_path / "Takeout"
    dialog = AddSourceDialog(folder, Detection("takeout", "It contains a folder."))
    qtbot.addWidget(dialog)
    assert dialog.label == "Takeout"
    assert dialog.source_type == "takeout"
    dialog.type_combo.setCurrentIndex(0)
    assert dialog.source_type == "plain"


def test_cancel_scan_from_ui(qtbot, tmp_path, no_dialogs):
    src = tmp_path / "many"
    for i in range(60):
        make_image(src / f"{i:03}.jpg", (64, 64))
    window, conn = open_window(qtbot)
    window.add_source(src)
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


def test_large_grid_loads_first_page_then_rest(qtbot, tmp_path, no_dialogs, monkeypatch):
    monkeypatch.setattr(grid, "FIRST_PAGE", 5)
    src = tmp_path / "many"
    for i in range(12):
        make_image(src / f"{i:03}.jpg", (16, 16), taken=f"2020:01:{i + 1:02} 00:00:00")
    window, conn = open_window(qtbot)
    window.add_source(src)
    wait_scan(qtbot, window)
    assert window.grid.count() == 12  # known from the month counts before the rows
    qtbot.waitUntil(lambda: window.grid_source.complete)
    expected = [p.relative_path for p in db.grid_photos(conn)]
    assert [p.relative_path for p in window.photos] == expected
    assert expected[0] == "011.jpg"
    window.close()
    conn.close()


def test_back_button_and_keys_close_viewer(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    window.add_source(photos)
    wait_scan(qtbot, window)
    for close in (
        lambda: qtbot.mouseClick(window.viewer.back_button, Qt.MouseButton.LeftButton),
        lambda: qtbot.keyClick(window.viewer, Qt.Key.Key_Backspace),
        lambda: qtbot.mouseClick(window.viewer.canvas, Qt.MouseButton.BackButton),
    ):
        window.open_viewer(2)
        assert window.pages.currentIndex() == mw.PAGE_VIEWER
        close()
        assert window.pages.currentIndex() == mw.PAGE_GRID
        assert window.grid.current_index() == 2
    window.close()
    conn.close()


def test_scan_batches_appear_in_grid_order_and_viewer_keeps_photo(
    qtbot, photos, no_dialogs, monkeypatch
):
    window, conn = open_window(qtbot)
    window.add_source(photos)
    wait_scan(qtbot, window)
    by_name = {p.relative_path: p for p in window.photos}
    # Pretend the scan had only committed mid.png so far, with the viewer open on it.
    monkeypatch.setattr(
        window.grid_source, "rows_query", lambda conn, limit, offset: [by_name["mid.png"]]
    )
    window.reload_grid()
    assert window.grid.count() == 1
    window.open_viewer(0)
    window._on_scan_added([by_name["old.jpg"], by_name["new.jpg"]])
    assert [p.relative_path for p in window.photos] == ["new.jpg", "mid.png", "old.jpg"]
    assert window.viewer.row == 1 and "mid.png" in window.viewer.status.text()
    assert "2 of 3" in window.viewer.status.text()
    window._on_scan_added([by_name["new.jpg"]])  # already shown: ignored
    assert window.grid.count() == 3
    window.close()
    conn.close()


def test_rescan_reload_keeps_open_photo(qtbot, photos, no_dialogs):
    window, conn = open_window(qtbot)
    window.add_source(photos)
    wait_scan(qtbot, window)
    window.open_viewer(1)
    window.rescan()
    wait_scan(qtbot, window)
    assert window.pages.currentIndex() == mw.PAGE_VIEWER
    assert window.viewer.photos is window.photos
    assert "mid.png" in window.viewer.status.text()
    window.close()
    conn.close()


def test_relocate_warns_when_no_photos_are_in_the_new_folder(
    qtbot, photos, tmp_path, monkeypatch, no_dialogs
):
    empty = tmp_path / "empty"
    empty.mkdir()
    window, conn = open_window(qtbot)
    source = window.add_source(photos, "Phone")
    wait_scan(qtbot, window)
    shown = []
    monkeypatch.setattr(mw.MainWindow, "_pick_folder", lambda self, title, start=None: empty)

    def answer(_parent, title, text, *args):
        shown.append((title, text))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", answer)
    window.relocate_source_interactive(source)
    assert shown[0][0] == "Relocate source" and "None of the 3 photos" in shown[0][1]
    assert window.sources_by_id[source.id].root_path == str(photos)  # cancelled: unchanged
    assert not window.scanning

    # A folder that has the photos is relocated to without asking, then rescanned.
    moved = tmp_path / "moved"
    photos.rename(moved)
    monkeypatch.setattr(mw.MainWindow, "_pick_folder", lambda self, title, start=None: moved)
    window.relocate_source_interactive(source)
    assert len(shown) == 1
    assert window.scanning
    wait_scan(qtbot, window)
    assert window.sources_by_id[source.id].root_path == str(moved)
    assert len(window.photos) == 3
    window.close()
    conn.close()


def test_remove_asks_first_and_says_files_are_kept(qtbot, photos, monkeypatch, no_dialogs):
    window, conn = open_window(qtbot)
    source = window.add_source(photos, "Phone")
    wait_scan(qtbot, window)
    asked = []

    def answer(_parent, title, text, *args):
        asked.append((title, text))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", answer)
    window.remove_source_interactive(source)
    title, text = asked[0]
    assert title == "Remove source" and "“Phone”" in text
    assert "3 photos" in text and "Your photos on disk are not touched" in text
    assert [s.id for s in window.sources] == [source.id]  # cancelled: kept
    assert len(window.photos) == 3
    window.close()
    conn.close()
