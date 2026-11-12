"""Capture marketing screenshots of Imagory over a synthetic library.

Usage: uv run python frontend/tools/screenshots/capture.py WORK_DIR
WORK_DIR must hold ``data/`` made by make_scenes.py. A scratch library is created in
WORK_DIR/library (IMAGORY_LIBRARY), so the real library is never touched. The app only
reads WORK_DIR/data.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

WORK = Path(sys.argv[1]).resolve()
REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "frontend" / "src" / "assets" / "screenshots"
DATA = WORK / "data"
SIZE = (1600, 1000)
FAKE_ROOTS = {
    str(DATA / "Photos"): r"C:\Users\Sam\Pictures\Photos",
    str(DATA / "Takeout"): r"C:\Users\Sam\Downloads\Takeout",
}

os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
os.environ["IMAGORY_LIBRARY"] = str(WORK / "library")
sys.path.insert(0, str(REPO))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

from imagory import db, paths  # noqa: E402
from imagory.app import upgrade_thumbnail_cache  # noqa: E402
from imagory.ui.icons import app_icon  # noqa: E402
from imagory.ui.main_window import MainWindow  # noqa: E402
from imagory.ui.sources_dialog import SourcesDialog  # noqa: E402
from imagory.ui.theme import apply_theme  # noqa: E402


def pump(seconds: float = 0.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def wait_for(cond, timeout=180.0, what="condition") -> None:
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            raise TimeoutError(what)
        pump(0.1)


def save(widget, name: str) -> None:
    from PIL import Image

    pump(0.5)
    tmp = WORK / name
    widget.grab().save(str(tmp))
    img = Image.open(tmp).convert("RGB")
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / name, optimize=True)
    size = (OUT / name).stat().st_size
    if size > 1_500_000:
        img.quantize(256, method=Image.Quantize.MEDIANCUT).save(OUT / name, optimize=True)
    print(f"{name}: {img.size}, {(OUT / name).stat().st_size / 1024:.0f} KB")


def hide_paths(widget) -> None:
    """Replace the scratch path in any label with a neutral Windows-style one."""
    for label in widget.findChildren(QLabel):
        text = label.text()
        for real, fake in FAKE_ROOTS.items():
            if real in text:
                text = text.replace(real, fake).replace(real.replace("/", "\\"), fake)
        label.setText(text.replace("/", "\\") if "C:\\Users" in text else text)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Imagory")
    app.setWindowIcon(app_icon())
    f = QFont("DejaVu Sans", 10)
    app.setFont(f)
    # Never let a modal popup block the run.
    for name in ("information", "warning", "question", "critical"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    library = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(library))
    upgrade_thumbnail_cache(conn, paths.thumbs_dir(library))
    db.set_setting(conn, "theme", "light")
    apply_theme(app, "light")
    window = MainWindow(library, conn)
    window.resize(*SIZE)
    window.show()
    pump(0.8)
    save(window, "welcome-light.png")

    # Sources
    window.source_actions.add_source(DATA / "Photos", "Photos", "plain", "plain")
    wait_for(lambda: window.scan.thread is None, what="scan 1")
    window.source_actions.add_source(DATA / "Takeout", "Google Takeout", "takeout", "takeout")
    wait_for(lambda: window.scan.thread is None, what="scan 2")
    pump(1.0)

    # Albums
    rows = conn.execute("SELECT id FROM photos ORDER BY id").fetchall()
    ids = [r[0] for r in rows]
    trips = db.create_album(conn, "Alps trip")
    db.add_photos_to_album(conn, trips, ids[-30:-8])
    fav = db.create_album(conn, "Favourites")
    db.add_photos_to_album(conn, fav, ids[10:200:9])
    sun = db.create_album(conn, "Golden hours")
    db.add_photos_to_album(conn, sun, ids[40:120:4])
    window.refresh_sidebar()
    window.fill_albums_menu()

    def settle() -> None:
        window.reload_grid()
        wait_for(lambda: len(window.photos) > 0, what="grid")
        pump(1.5)
        window.thumbnails.wait()
        pump(1.5)

    window.sidebar.set_section_expanded("people", True) if hasattr(
        window.sidebar, "set_section_expanded"
    ) else None
    settle()
    # scroll a little so month headers show mid-grid
    window.grid.verticalScrollBar().setValue(0)
    pump(0.5)
    save(window, "main-light.png")

    window.set_theme("dark")
    pump(1.0)
    save(window, "main-dark.png")

    # Viewer on a nice photo, dark, info panel open (prefer a Takeout photo: it has people)
    idx = 0
    for i, p in enumerate(window.photos):
        info = db.photo_info(conn, p.photo_id)
        if info and info.people and info.latitude is not None:
            idx = i
            break
    window.open_viewer(idx)
    window.viewer.set_info_visible(True)
    pump(2.0)
    hide_paths(window.viewer.info_panel)
    pump(0.3)
    save(window, "viewer-dark.png")
    window.close_viewer(idx)

    # Light-theme dialogs
    window.set_theme("light")
    pump(0.8)
    dlg = SourcesDialog(window)
    dlg.resize(1000, 300)
    dlg.show()
    pump(0.5)
    for row in range(dlg.table.rowCount()):
        item = dlg.table.item(row, 2)
        real = item.text()
        item.setText(FAKE_ROOTS.get(real, real))
    pump(0.5)
    save(dlg, "sources-light.png")
    dlg.close()

    # Cleanup wizard on a real analysis of the synthetic Takeout
    window.cleanup.open_wizard()
    wiz = window.cleanup.wizard
    out = WORK / "cleaned"
    wiz.set_input(DATA / "Takeout")
    wiz.set_output(out)
    wiz._start_analyse()
    from imagory.ui.cleanup_wizard import PAGE_PREVIEW

    wait_for(lambda: wiz.page == PAGE_PREVIEW, 120, "analysis")
    wiz.resize(640, 460)
    pump(0.5)
    hide_paths(wiz)
    save(wiz, "cleanup-light.png")
    wiz._failed = True  # let it close without the cancel question
    wiz.close()
    pump(0.3)

    window.closing = True
    window.close()
    conn.close()


if __name__ == "__main__":
    main()
