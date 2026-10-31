"""Albums in the main window: menu, add and remove, album view, scan lock (PRD §8.8)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from tests.imaging import make_image  # noqa: E402


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


@pytest.fixture
def window(qtbot, tmp_path):
    src = tmp_path / "pics"
    for i in range(8):
        make_image(src / f"{i}.jpg", (32, 24), taken=f"2020:0{1 + i % 2}:{i + 1:02} 10:00:00")
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    win = mw.MainWindow(root, conn)
    qtbot.addWidget(win)
    win.resize(900, 700)
    win.show()
    win.add_source(src)
    qtbot.waitUntil(lambda: not win.scanning, timeout=60_000)
    qtbot.waitUntil(lambda: win.grid_source.complete, timeout=10_000)
    yield win
    win.close()
    conn.close()


def test_add_show_and_remove_selection(window, qtbot):
    conn = window.conn
    album_id = db.create_album(conn, "Best")
    assert not window.add_to_album_action.isEnabled()  # nothing selected

    window.grid.click(0, Qt.KeyboardModifier.NoModifier)
    window.grid.click(2, Qt.KeyboardModifier.ShiftModifier)
    assert window.add_to_album_action.isEnabled()
    chosen = window.selected_photo_ids()
    assert len(chosen) == 3

    # Same effect as the dialog: the window passes the selected photo ids to it.
    db.add_photos_to_album(conn, album_id, chosen)
    album = next(a for a in db.list_albums(conn) if a.id == album_id)
    window.show_album(album)
    qtbot.waitUntil(lambda: window.grid_source.complete, timeout=10_000)
    assert sorted(p.photo_id for p in window.photos) == sorted(chosen)
    assert window.album_bar.isVisible()
    assert window.status_label.text().startswith("Album “Best”: 3 photos")

    window.grid.click(0, Qt.KeyboardModifier.NoModifier)
    assert window.remove_from_album_action.isEnabled()
    removed = window.photos[0].photo_id
    window.remove_selection_from_album()
    qtbot.waitUntil(lambda: window.grid_source.complete, timeout=10_000)
    assert removed not in {p.photo_id for p in window.photos}
    assert len(window.photos) == 2
    assert len(db.grid_photos(conn)) == 8  # the photo itself stays

    window.show_album(None)
    qtbot.waitUntil(lambda: window.grid_source.complete, timeout=10_000)
    assert len(window.photos) == 8
    assert not window.album_bar.isVisible()


def test_album_view_combines_with_the_filter(window, qtbot):
    conn = window.conn
    album_id = db.create_album(conn, "Mix")
    db.add_photos_to_album(conn, album_id, [p.photo_id for p in window.photos])
    window.show_album(next(a for a in db.list_albums(conn) if a.id == album_id))
    window.filter_bar.set_filter(db.PhotoFilter(date_from="2020-02-01"))
    assert window.current_filter().album_id == album_id
    assert window.photos and all(p.taken_at_local >= "2020-02" for p in window.photos)
    assert len(window.photos) < 8


def test_context_menu_and_albums_menu(window):
    window.grid.click(1, Qt.KeyboardModifier.NoModifier)
    menu = window.build_grid_menu()
    assert [a.text() for a in menu.actions()] == ["Add to album…"]
    album_id = db.create_album(window.conn, "Trip")
    window.show_album(next(a for a in db.list_albums(window.conn) if a.id == album_id))
    texts = [a.text() for a in window.build_grid_menu().actions()]
    assert texts == ["Add to album…", "Remove from album"]
    window._fill_albums_menu()
    assert any(a.text().startswith("Trip") and a.isChecked() for a in window.albums_menu.actions())


def test_album_actions_are_disabled_while_scanning(window, monkeypatch):
    window.grid.click(0, Qt.KeyboardModifier.NoModifier)
    assert window.add_to_album_action.isEnabled()
    assert window.manage_albums_action.isEnabled()
    monkeypatch.setattr(mw.MainWindow, "scanning", property(lambda self: True))
    window._update_actions()
    assert not window.add_to_album_action.isEnabled()
    assert not window.remove_from_album_action.isEnabled()
    assert not window.manage_albums_action.isEnabled()
    monkeypatch.undo()
    window._update_actions()
    assert window.add_to_album_action.isEnabled()
