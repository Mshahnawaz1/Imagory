"""Viewer zoom and pan, the info panel, Show in Explorer, and "Open in viewer"."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from imagory import db  # noqa: E402
from imagory.ui import info_panel  # noqa: E402
from imagory.ui.viewer import MAX_ZOOM, ImageCanvas, PhotoViewer  # noqa: E402
from tests.imaging import make_image  # noqa: E402

NOW = "2026-01-01T00:00:00Z"


def snapshot(folder: Path):
    return {
        p.relative_to(folder): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
    }


def record(path, content_hash="h1", **kw):
    fields = dict(
        relative_path=path,
        size=1234,
        modified_time=1,
        width=64,
        height=48,
        format="JPEG",
        taken_at_local="2020-05-01T10:00:00",
        date_source="exif",
        content_hash=content_hash,
    )
    fields.update(kw)
    return db.FileRecord(**fields)


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "t.db")
    yield c
    c.close()


# --- db.photo_info -----------------------------------------------------------------------


def test_photo_info_lists_primary_and_other_copies(conn):
    a = db.create_source(conn, "/photos/a", "Phone")
    b = db.create_source(conn, "/photos/b", "Backup")
    first = db.insert_file(conn, a.id, record("2020/x.jpg"), NOW)
    db.insert_file(conn, b.id, record("copy/x.jpg"), NOW)
    photo_id = conn.execute("SELECT photo_id FROM files WHERE id = ?", (first,)).fetchone()[0]
    conn.execute("UPDATE photos SET latitude = 51.5, longitude = -0.12 WHERE id = ?", (photo_id,))

    info = db.photo_info(conn, photo_id)
    assert info.primary.source_label == "Phone"
    assert info.primary.relative_path == "2020/x.jpg"
    assert info.primary.size == 1234
    assert [(o.source_label, o.relative_path) for o in info.others] == [("Backup", "copy/x.jpg")]
    assert (info.latitude, info.longitude) == (51.5, -0.12)
    assert info.date_source == "exif" and info.taken_at_local == "2020-05-01T10:00:00"
    assert db.photo_info(conn, 9999) is None


def test_photo_info_without_files(conn):
    conn.execute("INSERT INTO photos (taken_at_local) VALUES ('2020-01-01T00:00:00')")
    photo_id = conn.execute("SELECT id FROM photos").fetchone()[0]
    info = db.photo_info(conn, photo_id)
    assert info.primary is None and info.others == ()


# --- info panel text ---------------------------------------------------------------------


def test_describe_shows_every_field(conn):
    a = db.create_source(conn, "/photos/a", "Phone")
    b = db.create_source(conn, "/photos/b", "Backup")
    first = db.insert_file(conn, a.id, record("2020/x.jpg", date_source="takeout_json"), NOW)
    db.insert_file(conn, b.id, record("copy/x.jpg"), NOW)
    photo_id = conn.execute("SELECT photo_id FROM files WHERE id = ?", (first,)).fetchone()[0]
    conn.execute("UPDATE photos SET latitude = -33.5, longitude = 151.25 WHERE id = ?", (photo_id,))
    text = info_panel.describe(db.photo_info(conn, photo_id))
    assert "2020-05-01 10:00:00 (from Takeout JSON)" in text
    assert "33.50000\u00b0 S, 151.25000\u00b0 E" in text
    # the full path uses the OS separator (backslashes on Windows)
    assert "Phone" in text and os.path.join("2020", "x.jpg") in text
    assert "1.2 KB" in text
    assert "64 \u00d7 48 (JPEG)" in text
    assert "Also found in" in text and "Backup: copy/x.jpg" in text


def test_describe_unknowns_and_escaping():
    assert "No details" in info_panel.describe(None)
    info = db.PhotoInfo(1, None, None, None, None, None, None, None, None, ())
    text = info_panel.describe(info)
    assert "Unknown date" in text and "Not known" in text and "Unknown" in text
    assert "Also found in" not in text
    loc = db.PhotoLocation(1, "<b>x</b>", "/r", "a&b.jpg", 5, "missing")
    text = info_panel.describe(db.PhotoInfo(1, None, None, None, None, 1, 1, None, loc, (loc,)))
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "a&amp;b.jpg" in text
    assert "(missing)" in text and "missing from its source" in text


def test_format_size():
    assert info_panel.format_size(10) == "10 bytes"
    assert info_panel.format_size(2048) == "2.0 KB"
    assert info_panel.format_size(5 * 1024**2) == "5.0 MB"


# --- show in explorer --------------------------------------------------------------------


def test_reveal_argv_per_platform():
    p = Path("/x/y.jpg")
    # Windows gets a command line with only the path quoted; a quoted "/select,..." is
    # ignored by Explorer, which then opens a default folder instead.
    spaced = Path("/Users/a b/Google Photos/IMG_1.jpg")
    assert info_panel.reveal_argv(spaced, "win32") == (
        f'explorer /select,"{os.path.normpath(spaced)}"'
    )
    assert info_panel.reveal_argv(p, "darwin") == ["open", "-R", str(p)]
    assert info_panel.reveal_argv(p, "linux", wsl_path=r"\\wsl$\d\x\y.jpg") == [
        "explorer.exe",
        r"/select,\\wsl$\d\x\y.jpg",
    ]
    assert info_panel.reveal_argv(p, "linux") is None


def test_show_in_explorer_falls_back_to_the_folder(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(info_panel, "is_wsl", lambda: False)
    monkeypatch.setattr(info_panel.sys, "platform", "linux")
    monkeypatch.setattr(
        info_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
    )
    photo = tmp_path / "a.jpg"
    make_image(photo)
    before = snapshot(tmp_path)
    assert info_panel.show_in_explorer(photo)
    assert opened == [str(tmp_path)]
    assert not info_panel.show_in_explorer(tmp_path / "nope" / "b.jpg")
    assert snapshot(tmp_path) == before


# --- canvas zoom and pan -----------------------------------------------------------------


def canvas_with_image(qtbot, size=(400, 300), widget=(200, 200)):
    canvas = ImageCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(*widget)
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    canvas.set_image(image)
    return canvas


def test_zoom_is_limited_and_pan_is_clamped(qtbot):
    canvas = canvas_with_image(qtbot)
    assert canvas.zoom == 1.0
    canvas.zoom_by(0.5)
    assert canvas.zoom == 1.0  # never below fit
    canvas.zoom_by(1000)
    assert canvas.zoom == MAX_ZOOM
    canvas.pan_by(QPointF(10_000, -10_000))
    off = canvas.offset
    scale = canvas._fit_scale() * canvas.zoom
    assert off.x() == pytest.approx((400 * scale - 200) / 2)
    assert off.y() == pytest.approx(-(300 * scale - 200) / 2)
    canvas.reset_view()
    assert canvas.zoom == 1.0 and canvas.offset == QPointF(0, 0)


def test_zoom_keeps_the_point_under_the_cursor(qtbot):
    canvas = canvas_with_image(qtbot, size=(800, 800), widget=(400, 400))
    anchor = QPointF(300, 100)
    centre = QPointF(200, 200)
    before = (anchor - centre - canvas.offset) / canvas._fit_scale()
    canvas.zoom_by(2.0, anchor)
    after = (anchor - centre - canvas.offset) / (canvas._fit_scale() * canvas.zoom)
    assert after.x() == pytest.approx(before.x(), abs=1e-6)
    assert after.y() == pytest.approx(before.y(), abs=1e-6)


def test_zoomed_in_signal_only_when_leaving_fit(qtbot):
    canvas = canvas_with_image(qtbot)
    seen = []
    canvas.zoomed_in.connect(lambda: seen.append(1))
    canvas.zoom_by(2)
    canvas.zoom_by(2)
    assert seen == [1]
    canvas.reset_view()
    canvas.zoom_by(2)
    assert seen == [1, 1]


def test_canvas_without_image_ignores_zoom(qtbot):
    canvas = ImageCanvas()
    qtbot.addWidget(canvas)
    canvas.zoom_by(3)
    assert canvas.zoom == 1.0
    canvas.grab()  # paints the empty state without error


# --- viewer ------------------------------------------------------------------------------


def make_viewer(qtbot, conn, tmp_path, count=2):
    root = tmp_path / "photos"
    root.mkdir()
    src = db.create_source(conn, str(root), "Phone")
    for i in range(count):
        make_image(root / f"p{i}.jpg", (80, 60))
        db.insert_file(
            conn, src.id, record(f"p{i}.jpg", content_hash=f"h{i}", width=80, height=60), NOW
        )
    photos = db.grid_photos(conn)
    viewer = PhotoViewer()
    qtbot.addWidget(viewer)
    viewer.resize(600, 400)
    viewer.set_connection(conn)
    viewer.show_photo(photos, {src.id: src}, 0)
    qtbot.waitUntil(lambda: not viewer.canvas._image.isNull(), timeout=5000)
    return viewer, root


def test_viewer_zoom_loads_full_resolution_and_keeps_the_view(qtbot, conn, tmp_path):
    viewer, root = make_viewer(qtbot, conn, tmp_path)
    before = snapshot(root)
    first = viewer.canvas._image
    viewer.canvas.zoom_by(2)
    qtbot.waitUntil(lambda: viewer.canvas._image is not first, timeout=5000)
    assert viewer.canvas.zoom == 2.0
    # Moving to the next photo goes back to fit.
    qtbot.keyClick(viewer, Qt.Key.Key_Right)
    assert viewer.row == 1 and viewer.canvas.zoom == 1.0
    assert snapshot(root) == before


def test_viewer_keys_zoom_and_toggle_info(qtbot, conn, tmp_path):
    viewer, _ = make_viewer(qtbot, conn, tmp_path)
    assert viewer.info_panel.isHidden()
    qtbot.keyClick(viewer, Qt.Key.Key_Plus)
    assert viewer.canvas.zoom > 1.0
    qtbot.keyClick(viewer, Qt.Key.Key_0)
    assert viewer.canvas.zoom == 1.0
    qtbot.keyClick(viewer, Qt.Key.Key_I)
    assert not viewer.info_panel.isHidden() and viewer.info_button.isChecked()
    assert "p0.jpg" in viewer.info_panel.details.text()
    assert viewer.info_panel.reveal_button.isEnabled()
    qtbot.keyClick(viewer, Qt.Key.Key_Right)
    assert "p1.jpg" in viewer.info_panel.details.text()
    qtbot.keyClick(viewer, Qt.Key.Key_I)
    assert viewer.info_panel.isHidden() and not viewer.info_button.isChecked()


def test_info_panel_reveal_uses_the_full_path(qtbot, conn, tmp_path, monkeypatch):
    viewer, root = make_viewer(qtbot, conn, tmp_path)
    shown = []
    monkeypatch.setattr("imagory.ui.viewer.show_in_explorer", shown.append)
    viewer.set_info_visible(True)
    viewer.info_panel.reveal_button.click()
    assert shown == [root / "p0.jpg"]


def test_viewer_without_connection_still_works(qtbot, conn, tmp_path):
    viewer, _ = make_viewer(qtbot, conn, tmp_path)
    viewer.set_connection(None)
    viewer.set_info_visible(True)
    assert "No details" in viewer.info_panel.details.text()
    assert not viewer.info_panel.reveal_button.isEnabled()


# --- main window -------------------------------------------------------------------------


def test_open_in_viewer_from_the_grid_menu(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from imagory import paths
    from imagory.ui import main_window as mw

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    root = tmp_path / "photos"
    make_image(root / "a.jpg", (80, 60), taken="2020:05:01 10:00:00")
    library = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(library))
    window = mw.MainWindow(library, conn)
    qtbot.addWidget(window)
    window.resize(900, 700)
    window.show()
    window.add_source(root)
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert window.grid.count() == 1
    before = snapshot(root)

    menu = window.build_grid_menu(0)
    open_action = next(a for a in menu.actions() if a.text() == "Open in viewer")
    open_action.trigger()
    assert window.pages.currentWidget() is window.viewer
    assert window.viewer.row == 0
    assert snapshot(root) == before
    conn.close()
