"""Albums (PRD §8.8): schema, queries, cascades, and dialogs."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QInputDialog, QMessageBox  # noqa: E402

from imagory import db  # noqa: E402
from imagory.ui import albums as ui  # noqa: E402
from tests.db_helpers import album_ids_of, album_photos  # noqa: E402

NOW = "2024-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "imagory.db")
    yield c
    c.close()


def add_photo(conn, source_id, path, taken="2020-01-01T00:00:00") -> int:
    """Insert a file and its photo; return the photo id."""
    rec = db.FileRecord(path, 10, 1_000, 4, 3, "JPEG", taken, "exif")
    with db.transaction(conn):
        file_id = db.insert_file(conn, source_id, rec, NOW)
    return conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]


@pytest.fixture
def src(conn):
    return db.create_source(conn, "/photos", "Photos")


def test_album_crud_and_names(conn):
    a = db.create_album(conn, "  Holiday ")
    assert [(x.name, x.photo_count) for x in db.list_albums(conn)] == [("Holiday", 0)]
    with pytest.raises(db.AlbumNameError):
        db.create_album(conn, "holiday")  # unique, case-insensitive
    with pytest.raises(db.AlbumNameError):
        db.create_album(conn, "   ")
    b = db.create_album(conn, "Alpha")
    assert [x.name for x in db.list_albums(conn)] == ["Alpha", "Holiday"]
    db.rename_album(conn, a, "Zed")
    assert [x.name for x in db.list_albums(conn)] == ["Alpha", "Zed"]
    with pytest.raises(db.AlbumNameError):
        db.rename_album(conn, a, "ALPHA")
    db.rename_album(conn, b, "alpha")  # changing only the case of its own name is fine
    assert not conn.in_transaction


def test_add_is_idempotent_and_remove(conn, src):
    p1, p2, p3 = (add_photo(conn, src.id, f"{n}.jpg") for n in "abc")
    album = db.create_album(conn, "A")
    assert db.add_photos_to_album(conn, album, [p1, p2, p2]) == 2
    assert db.add_photos_to_album(conn, album, [p2, p3]) == 1
    assert db.add_photos_to_album(conn, album, []) == 0
    assert db.list_albums(conn)[0].photo_count == 3
    assert db.remove_photos_from_album(conn, album, [p1, p1, 999]) == 1
    assert db.remove_photos_from_album(conn, album, [p1]) == 0
    assert {p.photo_id for p in album_photos(conn, album)} == {p2, p3}


def test_album_membership(conn, src):
    p1, p2 = add_photo(conn, src.id, "a.jpg"), add_photo(conn, src.id, "b.jpg")
    a, b = db.create_album(conn, "A"), db.create_album(conn, "B")
    db.add_photos_to_album(conn, a, [p1, p2])
    db.add_photos_to_album(conn, b, [p1])
    assert album_ids_of(conn, p1) == [a, b]
    assert album_ids_of(conn, p2) == [a]


def test_delete_album_keeps_photos(conn, src):
    p = add_photo(conn, src.id, "a.jpg")
    album = db.create_album(conn, "A")
    db.add_photos_to_album(conn, album, [p])
    db.delete_album(conn, album)
    assert db.list_albums(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM album_photos").fetchone()[0] == 0
    assert [x.photo_id for x in db.grid_photos(conn)] == [p]


def test_delete_photo_removes_memberships_only(conn, src):
    p1, p2 = add_photo(conn, src.id, "a.jpg"), add_photo(conn, src.id, "b.jpg")
    album = db.create_album(conn, "A")
    db.add_photos_to_album(conn, album, [p1, p2])
    with db.transaction(conn):
        conn.execute("UPDATE files SET photo_id = NULL WHERE photo_id = ?", (p1,))
        conn.execute("DELETE FROM photos WHERE id = ?", (p1,))
    assert [x.photo_id for x in album_photos(conn, album)] == [p2]
    assert len(db.list_albums(conn)) == 1


def test_delete_source_cascades_memberships(conn):
    s1 = db.create_source(conn, "/one", "One")
    s2 = db.create_source(conn, "/two", "Two")
    only = add_photo(conn, s1.id, "only.jpg")
    shared = add_photo(conn, s1.id, "shared.jpg")
    keep = add_photo(conn, s2.id, "keep.jpg")
    with db.transaction(conn):  # a second file of "shared" lives in source 2
        conn.execute(
            "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, "
            "first_seen_at, last_seen_at) VALUES (?, ?, 'copy.jpg', 10, 1, ?, ?)",
            (s2.id, shared, NOW, NOW),
        )
    album = db.create_album(conn, "A")
    db.add_photos_to_album(conn, album, [only, shared, keep])

    db.delete_source(conn, s1.id)

    members = {r[0] for r in conn.execute("SELECT photo_id FROM album_photos")}
    assert members == {shared, keep}  # "only" was deleted; "shared" still has a file
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(db.list_albums(conn)) == 1


def test_delete_source_deletes_albums_it_empties(conn):
    s1 = db.create_source(conn, "/one", "One")
    s2 = db.create_source(conn, "/two", "Two")
    only = add_photo(conn, s1.id, "only.jpg")
    other = add_photo(conn, s2.id, "other.jpg")
    emptied = db.create_album(conn, "Only one")  # every photo is in source 1
    mixed = db.create_album(conn, "Mixed")  # keeps a photo of source 2
    elsewhere = db.create_album(conn, "Elsewhere")  # nothing in source 1
    already_empty = db.create_album(conn, "Empty")  # never had photos: not this removal's doing
    db.add_photos_to_album(conn, emptied, [only])
    db.add_photos_to_album(conn, mixed, [only, other])
    db.add_photos_to_album(conn, elsewhere, [other])

    assert db.source_emptied_albums(conn, s1.id) == [(emptied, "Only one")]
    db.delete_source(conn, s1.id)

    assert {a.id for a in db.list_albums(conn)} == {mixed, elsewhere, already_empty}
    assert {r[0] for r in conn.execute("SELECT photo_id FROM album_photos")} == {other}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_source_keeps_album_whose_photo_has_a_copy_elsewhere(conn):
    s1 = db.create_source(conn, "/one", "One")
    s2 = db.create_source(conn, "/two", "Two")
    shared = add_photo(conn, s1.id, "shared.jpg")
    with db.transaction(conn):  # a second file of "shared" lives in source 2
        conn.execute(
            "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, "
            "first_seen_at, last_seen_at) VALUES (?, ?, 'copy.jpg', 10, 1, ?, ?)",
            (s2.id, shared, NOW, NOW),
        )
    album = db.create_album(conn, "Shared")
    db.add_photos_to_album(conn, album, [shared])

    assert db.source_emptied_albums(conn, s1.id) == []
    db.delete_source(conn, s1.id)
    assert [(a.id, a.photo_count) for a in db.list_albums(conn)] == [(album, 1)]


def test_album_grid_order_and_missing(conn, src):
    new = add_photo(conn, src.id, "z.jpg", "2024-06-01T00:00:00")
    old_b = add_photo(conn, src.id, "b.jpg", "2001-01-01T00:00:00")
    old_a = add_photo(conn, src.id, "a.jpg", "2001-01-01T00:00:00")
    gone = add_photo(conn, src.id, "gone.jpg", "2010-01-01T00:00:00")
    other = add_photo(conn, src.id, "other.jpg", "2020-01-01T00:00:00")
    album = db.create_album(conn, "A")
    db.add_photos_to_album(conn, album, [new, old_b, old_a, gone])
    with db.transaction(conn):
        conn.execute("UPDATE files SET status = 'missing' WHERE photo_id = ?", (gone,))

    rows = album_photos(conn, album)
    assert [r.photo_id for r in rows] == [new, old_a, old_b]
    assert other not in [r.photo_id for r in rows]
    # Same columns and order as the library grid, restricted to the album.
    expected = [r for r in db.grid_photos(conn) if r.photo_id in {new, old_a, old_b}]
    assert rows == expected
    assert album_photos(conn, album, limit=1, offset=1) == rows[1:2]
    assert db.list_albums(conn)[0].photo_count == 3  # missing photo not counted


def test_album_month_counts(conn, src):
    ids = [
        add_photo(conn, src.id, "a.jpg", "2024-06-01T00:00:00"),
        add_photo(conn, src.id, "b.jpg", "2024-06-15T00:00:00"),
        add_photo(conn, src.id, "c.jpg", "2023-12-31T23:59:59"),
        add_photo(conn, src.id, "d.jpg", None),
        add_photo(conn, src.id, "e.jpg", "2024-07-01T00:00:00"),  # not in album
    ]
    album = db.create_album(conn, "A")
    db.add_photos_to_album(conn, album, ids[:4])
    assert db.filtered_month_counts(conn, db.PhotoFilter(album_id=album)) == [
        ("2024-06", 2),
        ("2023-12", 1),
        (None, 1),
    ]
    assert (
        db.filtered_month_counts(conn, db.PhotoFilter(album_id=db.create_album(conn, "Empty")))
        == []
    )


# --- dialogs ----------------------------------------------------------------------------


@pytest.fixture
def prompts(monkeypatch):
    """Replace blocking Qt dialogs: ``name`` is the text typed, ``yes`` the answer."""

    class Prompts:
        name = ""
        yes = True
        messages: list = []

    p = Prompts()
    p.messages = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (p.name, bool(p.name)))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: (
            p.messages.append(a[2]) or QMessageBox.StandardButton.Yes
            if p.yes
            else QMessageBox.StandardButton.No
        ),
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: p.messages.append(a[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: p.messages.append(a[2]))
    return p


def test_albums_dialog_create_rename_delete(qtbot, conn, src, prompts):
    p = add_photo(conn, src.id, "a.jpg")
    dialog = ui.AlbumsDialog(conn)
    qtbot.addWidget(dialog)
    assert not dialog.delete_button.isEnabled()

    prompts.name = "Trips"
    dialog.create_album()
    assert dialog.list.count() == 1 and dialog.list.currentItem().text().startswith("Trips")
    assert "0 photos" in dialog.list.currentItem().text()
    album_id = db.list_albums(conn)[0].id
    db.add_photos_to_album(conn, album_id, [p])

    prompts.name = "Trips 2024"
    dialog.rename_selected()
    assert [a.name for a in db.list_albums(conn)] == ["Trips 2024"]
    assert "1 photo)" in dialog.list.currentItem().text()

    prompts.name = "Trips 2024"
    dialog.create_album()  # duplicate: warns, nothing created
    assert "already exists" in prompts.messages[-1]
    assert len(db.list_albums(conn)) == 1

    prompts.yes = False
    dialog.delete_selected()
    assert len(db.list_albums(conn)) == 1
    prompts.yes = True
    dialog.delete_selected()
    assert "Your photos are not affected" in prompts.messages[-1]
    assert db.list_albums(conn) == []
    assert [x.photo_id for x in db.grid_photos(conn)] == [p]
    assert not dialog.empty_label.isHidden()


def test_add_to_album_dialog(qtbot, conn, src, prompts):
    p1, p2, p3 = (add_photo(conn, src.id, f"{n}.jpg") for n in "abc")
    existing = db.create_album(conn, "Existing")
    db.add_photos_to_album(conn, existing, [p1])

    dialog = ui.AddToAlbumDialog(conn, [p1, p2, p3])
    qtbot.addWidget(dialog)
    assert not dialog.ok_button.isEnabled()
    dialog.list.setCurrentRow(0)
    dialog.accept()
    assert (dialog.album_id, dialog.added, dialog.already_present) == (existing, 2, 1)
    assert "Added 2 photos" in prompts.messages[-1]
    assert "1 photo already" in prompts.messages[-1]
    assert len(album_photos(conn, existing)) == 3

    dialog = ui.AddToAlbumDialog(conn, [p1])
    qtbot.addWidget(dialog)
    prompts.name = "Brand new"
    dialog.create_album()  # selects the new album
    assert dialog.ok_button.isEnabled()
    dialog.accept()
    new_id = next(a.id for a in db.list_albums(conn) if a.name == "Brand new")
    assert dialog.album_id == new_id and dialog.added == 1
    assert album_ids_of(conn, p1) == [existing, new_id]
