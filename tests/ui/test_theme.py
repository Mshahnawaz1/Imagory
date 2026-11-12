"""Theme mode: pure resolution, persistence, and the View > Theme menu."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from imagory import db, paths  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from imagory.ui import theme  # noqa: E402


@pytest.mark.parametrize(
    ("mode", "os_dark", "expected"),
    [
        ("system", False, False),
        ("system", True, True),
        ("light", True, False),
        ("dark", False, True),
        ("bogus", True, True),  # unknown falls back to System
    ],
)
def test_resolve_dark(mode, os_dark, expected):
    assert theme.resolve_dark(mode, os_dark) is expected


def test_normalize_mode():
    assert theme.normalize_mode(None) == "system"
    assert theme.normalize_mode("dark") == "dark"
    assert theme.normalize_mode("nope") == "system"


def test_mode_persists(tmp_path):
    conn = db.open_database(tmp_path / "t.db")
    assert theme.load_mode(conn) == "system"
    theme.save_mode(conn, "dark")
    assert theme.load_mode(conn) == "dark"
    conn.close()


def test_menu_applies_and_saves(qtbot):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    window = mw.MainWindow(root, conn)
    qtbot.addWidget(window)
    app = QApplication.instance()
    assert window.theme_actions["system"].isChecked()

    window.theme_actions["dark"].trigger()
    assert theme.current_mode(app) == "dark"
    assert theme.load_mode(conn) == "dark"
    assert window.theme_actions["dark"].isChecked()
    assert not window.theme_actions["system"].isChecked()
    assert app.palette().window().color().name().upper() == theme.DARK.paper

    # OS scheme changes are ignored unless System is selected.
    window.theme_actions["light"].trigger()
    theme.on_os_scheme_changed(app)
    assert app.palette().window().color().name().upper() == theme.LIGHT.paper
    window.theme_actions["system"].trigger()
    assert theme.load_mode(conn) == "system"
    conn.close()
