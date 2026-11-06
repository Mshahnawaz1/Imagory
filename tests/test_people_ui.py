"""People in the sidebar and the filter bar (PRD §8.9)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QPushButton  # noqa: E402

from imagory import db, paths  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from imagory.ui.sidebar import KIND_ROLE, SECTION_ROLE, VALUE_ROLE  # noqa: E402
from tests.imaging import make_image  # noqa: E402


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


@pytest.fixture
def window(qtbot, tmp_path):
    src = tmp_path / "pics"
    for i in range(6):
        make_image(src / f"{i}.jpg", (32, 24), taken=f"2020:01:{i + 1:02} 10:00:00")
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


def tag(conn, relative_path: str, *names: str) -> None:
    file_id = conn.execute(
        "SELECT id FROM files WHERE relative_path = ?", (relative_path,)
    ).fetchone()[0]
    db.set_photo_takeout_metadata(conn, file_id, names, None, False, None)


def person_items(window):
    window.sidebar.set_section_expanded("people", True)
    lst = window.sidebar._list
    return [lst.item(i) for i in range(lst.count()) if lst.item(i).data(KIND_ROLE) == "person"]


def wait_loaded(qtbot, window):
    qtbot.waitUntil(lambda: window.grid_source.complete, timeout=10_000)


def test_people_section_hidden_without_people(window):
    assert person_items(window) == []
    assert not window.filter_bar._person.isVisible()


def test_sidebar_lists_people_and_click_filters_grid(window, qtbot):
    tag(window.conn, "0.jpg", "Anna", "Ben")
    tag(window.conn, "1.jpg", "Anna")
    window.refresh_sidebar()

    items = person_items(window)
    assert [i.text().split("   ")[0] for i in items] == ["Anna", "Ben"]  # most photos first
    assert items[0].text().endswith("2")
    assert window.filter_bar._person.isVisible()

    window.sidebar._on_clicked(items[0])
    wait_loaded(qtbot, window)
    assert len(window.photos) == 2
    assert window.filter_bar.filter().person_id == items[0].data(VALUE_ROLE)
    selected = window.sidebar._list.selectedItems()
    assert selected and selected[0].data(KIND_ROLE) == "person"
    assert window.status_label.text().startswith("2 photos match the filter")

    window.filter_bar.clear()
    wait_loaded(qtbot, window)
    assert len(window.photos) == 6


def test_person_filter_combines_with_other_filters(window, qtbot):
    tag(window.conn, "0.jpg", "Anna")
    tag(window.conn, "1.jpg", "Anna")
    window.refresh_sidebar()
    anna = person_items(window)[0].data(VALUE_ROLE)

    window.filter_bar.set_filter(db.PhotoFilter(person_id=anna, date_from="2020-01-02"))
    wait_loaded(qtbot, window)
    assert len(window.photos) == 1


def test_filter_bar_person_combo_chip_and_clear(window, qtbot):
    tag(window.conn, "2.jpg", "Cleo")
    window.refresh_sidebar()
    bar = window.filter_bar
    combo = bar._person
    assert combo.itemText(0) == "Any person"
    combo.setCurrentIndex(1)
    wait_loaded(qtbot, window)
    assert bar.filter().person_id is not None
    assert len(window.photos) == 1
    chips = [b for b in bar.findChildren(QPushButton) if b.objectName() == "chip"]
    assert [c.text() for c in chips] == ["Cleo  ✕"]
    assert window.sidebar._list.selectedItems()[0].data(KIND_ROLE) == "person"

    chips[0].click()
    wait_loaded(qtbot, window)
    assert bar.filter().person_id is None
    assert len(window.photos) == 6


def test_person_filter_drops_when_person_disappears(window, qtbot):
    tag(window.conn, "3.jpg", "Dee")
    window.refresh_sidebar()
    window.sidebar._on_clicked(person_items(window)[0])
    wait_loaded(qtbot, window)
    assert len(window.photos) == 1

    window.conn.execute("DELETE FROM photo_people")
    window.conn.commit()
    window.refresh_sidebar()
    wait_loaded(qtbot, window)
    assert window.filter_bar.filter().person_id is None
    assert person_items(window) == []
    assert len(window.photos) == 6


def test_people_locked_during_scan(window):
    tag(window.conn, "0.jpg", "Anna")
    window.refresh_sidebar()
    assert person_items(window)[0].flags() != Qt.ItemFlag.NoItemFlags
    window.sidebar.set_people_enabled(False)
    assert person_items(window)[0].flags() == Qt.ItemFlag.NoItemFlags
    window.sidebar.set_people_enabled(True)
    assert person_items(window)[0].flags() != Qt.ItemFlag.NoItemFlags


def test_photo_matches_rejects_person_filter(window):
    from imagory.ui.grid import photo_matches

    photo = window.photos[0]
    assert photo_matches(db.PhotoFilter(), photo)
    assert not photo_matches(db.PhotoFilter(person_id=1), photo)


def section_header(window, key):
    lst = window.sidebar._list
    for i in range(lst.count()):
        if lst.item(i).data(KIND_ROLE) == "section" and lst.item(i).data(SECTION_ROLE) == key:
            return lst.item(i)
    raise AssertionError(key)


def test_people_section_is_collapsible_and_starts_closed(window):
    tag(window.conn, "0.jpg", "Anna")
    window.refresh_sidebar()
    sidebar = window.sidebar
    lst = sidebar._list
    kinds = lambda: [lst.item(i).data(KIND_ROLE) for i in range(lst.count())]  # noqa: E731

    assert not sidebar.is_section_expanded("people")
    assert "person" not in kinds()

    sidebar._on_clicked(section_header(window, "people"))
    assert "person" in kinds()
    sidebar._on_clicked(section_header(window, "people"))
    assert "person" not in kinds()

    sidebar._on_clicked(section_header(window, "albums"))  # albums and sources collapse too
    assert not sidebar.is_section_expanded("albums")


def test_highlight_stays_on_clicked_section_header(window):
    sidebar = window.sidebar
    sidebar._on_clicked(section_header(window, "albums"))
    selected = sidebar._list.selectedItems()
    assert [i.data(SECTION_ROLE) for i in selected] == ["albums"]
    sidebar._on_clicked(section_header(window, "sources"))
    selected = sidebar._list.selectedItems()
    assert [i.data(SECTION_ROLE) for i in selected] == ["sources"]
    sidebar.set_current("all")  # a real view change moves the highlight back
    assert [i.data(KIND_ROLE) for i in sidebar._list.selectedItems()] == ["all"]
