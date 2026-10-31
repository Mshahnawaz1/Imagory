"""Exact duplicates, photo identity by hash, primary copy, missing photos, remove missing,
date wiring, and the V0 to V1 upgrade (PRD §8.2, §8.4, §8.5, §9)."""

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # for the sources dialog test

from imagory import db, paths, sources, thumbs
from imagory.scanner import Scanner
from tests.imaging import make_image
from tests.test_scan import snapshot


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def scan(conn, source, root, **kwargs):
    """Scan and check the source folder is unchanged: same files, sizes, mtimes."""
    folder = Path(source.root_path)
    before = snapshot(folder)
    summary = Scanner(conn, source, paths.thumbs_dir(root), root, workers=2, **kwargs).run()
    assert snapshot(folder) == before
    return summary.counts


def copy(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)  # same bytes, same mtime
    return dest


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def file_row(conn, source_id: int, rel: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM files WHERE source_id = ? AND relative_path = ?", (source_id, rel)
    ).fetchone()


def photo_of(conn, source_id: int, rel: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT p.* FROM photos p JOIN files f ON f.photo_id = p.id "
        "WHERE f.source_id = ? AND f.relative_path = ?",
        (source_id, rel),
    ).fetchone()


def primary_of(conn, photo_id: int) -> tuple[int, str]:
    return tuple(
        conn.execute(
            "SELECT f.source_id, f.relative_path FROM photos p JOIN files f "
            "ON f.id = p.primary_file_id WHERE p.id = ?",
            (photo_id,),
        ).fetchone()
    )


def test_duplicates_in_one_source_collapse_to_one_photo(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    a = make_image(src / "a.jpg", (300, 200), taken="2019:01:31 23:59:59")
    copy(a, src / "backup" / "a copy.jpg")
    make_image(src / "b.jpg", color=(0, 0, 200))
    source = db.create_source(conn, str(src), "photos")

    c = scan(conn, source, root)
    assert (c.found, c.added, c.errors) == (3, 3, 0)
    assert count(conn, "files") == 3 and count(conn, "photos") == 2
    dup = photo_of(conn, source.id, "a.jpg")
    assert dup["id"] == photo_of(conn, source.id, "backup/a copy.jpg")["id"]
    assert dup["content_hash"] == file_row(conn, source.id, "a.jpg")["content_hash"]
    assert (dup["width"], dup["height"]) == (300, 200)
    grid = db.grid_photos(conn)
    assert len(grid) == 2
    shown = next(p for p in grid if p.photo_id == dup["id"])
    assert shown.content_hash == dup["content_hash"]
    assert thumbs.thumb_path(paths.thumbs_dir(root), dup["content_hash"]).exists()
    # One thumbnail per photo, not per file.
    assert len(list(paths.thumbs_dir(root).rglob("*.jpg"))) == 2


def test_duplicates_across_sources_and_primary_by_source_added_date(tmp_path, lib):
    root, conn = lib
    first_dir, second_dir = tmp_path / "phone", tmp_path / "takeout"
    a = make_image(first_dir / "a.jpg", taken="2019:01:31 23:59:59")
    copy(a, second_dir / "2019" / "a.jpg")
    first = db.create_source(conn, str(first_dir), "Phone")
    second = db.create_source(conn, str(second_dir), "Takeout")

    # Scanning the later source first makes it primary until the earlier one is scanned.
    batches: list[list[db.GridPhoto]] = []
    scan(conn, second, root, on_added=batches.append)
    pid = photo_of(conn, second.id, "2019/a.jpg")["id"]
    assert primary_of(conn, pid) == (second.id, "2019/a.jpg")
    scan(conn, first, root, on_added=batches.append)
    assert photo_of(conn, first.id, "a.jpg")["id"] == pid
    assert primary_of(conn, pid) == (first.id, "a.jpg")
    assert count(conn, "photos") == 1
    assert [(p.photo_id, p.source_id) for p in db.grid_photos(conn)] == [(pid, first.id)]
    # The second copy was not reported to the grid again: it already showed the photo.
    assert [p.photo_id for batch in batches for p in batch] == [pid]


def test_primary_copy_hands_over_and_missing_photo_rule(tmp_path, lib):
    root, conn = lib
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    img = make_image(first_dir / "x.jpg")
    copy(img, second_dir / "x.jpg")
    aside = tmp_path / "aside.jpg"
    first = db.create_source(conn, str(first_dir), "A")
    second = db.create_source(conn, str(second_dir), "B")
    scan(conn, first, root)
    scan(conn, second, root)
    pid = photo_of(conn, first.id, "x.jpg")["id"]
    assert primary_of(conn, pid) == (first.id, "x.jpg")

    # The primary copy goes missing: the next copy takes over and the photo stays visible.
    os.replace(first_dir / "x.jpg", aside)
    assert scan(conn, first, root).missing == 1
    assert primary_of(conn, pid) == (second.id, "x.jpg")
    assert [(p.photo_id, p.source_id) for p in db.grid_photos(conn)] == [(pid, second.id)]

    # All copies missing: the photo is missing, hidden, and its rows are kept.
    (second_dir / "x.jpg").unlink()
    assert scan(conn, second, root).missing == 1
    assert db.grid_photos(conn) == []
    assert count(conn, "photos") == 1 and count(conn, "files") == 2

    # The first copy reappears unchanged: it is primary again.
    os.replace(aside, first_dir / "x.jpg")
    c = scan(conn, first, root)
    assert (c.unchanged, c.updated, c.added) == (1, 0, 0)
    assert primary_of(conn, pid) == (first.id, "x.jpg")
    assert [p.photo_id for p in db.grid_photos(conn)] == [pid]


def test_primary_within_a_source_is_the_first_seen_copy(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    img = make_image(src / "z" / "late.jpg")
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)
    copy(img, src / "a" / "early-name.jpg")
    scan(conn, source, root)
    pid = photo_of(conn, source.id, "z/late.jpg")["id"]
    assert primary_of(conn, pid) == (source.id, "z/late.jpg")


def test_changed_file_moves_between_photos(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    a = make_image(src / "a.jpg", color=(10, 10, 10))
    copy(a, src / "a2.jpg")
    make_image(src / "solo.jpg", color=(20, 20, 20))
    b = make_image(src / "b.jpg", color=(30, 30, 30))
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)
    shared = photo_of(conn, source.id, "a.jpg")["id"]
    solo = photo_of(conn, source.id, "solo.jpg")["id"]
    b_photo = photo_of(conn, source.id, "b.jpg")["id"]
    assert count(conn, "photos") == 3

    # A copy that is edited leaves its duplicate's photo for a new one.
    make_image(src / "a2.jpg", (80, 80), color=(40, 40, 40))
    # A sole file edited in place keeps its photo id (re-keyed to the new hash).
    make_image(src / "solo.jpg", (90, 90), color=(50, 50, 50))
    c = scan(conn, source, root)
    assert (c.updated, c.added) == (2, 0)
    assert photo_of(conn, source.id, "a.jpg")["id"] == shared
    edited = photo_of(conn, source.id, "a2.jpg")
    assert edited["id"] != shared and edited["width"] == 80
    assert edited["content_hash"] == file_row(conn, source.id, "a2.jpg")["content_hash"]
    rekeyed = photo_of(conn, source.id, "solo.jpg")
    assert rekeyed["id"] == solo and rekeyed["width"] == 90
    assert rekeyed["content_hash"] == file_row(conn, source.id, "solo.jpg")["content_hash"]
    assert count(conn, "photos") == 4

    # A file edited into an exact copy of another photo merges into it; its old photo,
    # left with no files, is deleted.
    shutil.copyfile(b, src / "solo.jpg")
    scan(conn, source, root)
    assert photo_of(conn, source.id, "solo.jpg")["id"] == b_photo
    assert conn.execute("SELECT 1 FROM photos WHERE id = ?", (solo,)).fetchone() is None
    assert count(conn, "photos") == 3
    assert len(db.grid_photos(conn)) == 3


def test_photo_with_only_missing_files_is_never_deleted_by_a_scan(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    a = make_image(src / "a.jpg")
    copy(a, src / "b.jpg")
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)
    (src / "a.jpg").unlink()
    make_image(src / "b.jpg", color=(0, 255, 0))  # the other copy changes
    c = scan(conn, source, root)
    assert (c.missing, c.updated) == (1, 1)
    # a.jpg's photo keeps its (missing) file row, so it is kept.
    assert count(conn, "photos") == 2
    old = photo_of(conn, source.id, "a.jpg")
    assert old is not None and old["primary_file_id"] == file_row(conn, source.id, "a.jpg")["id"]
    assert [p.relative_path for p in db.grid_photos(conn)] == ["b.jpg"]


def test_remove_missing_items(tmp_path, lib):
    root, conn = lib
    src, other = tmp_path / "photos", tmp_path / "other"
    shared = make_image(src / "shared.jpg", color=(1, 2, 3))
    copy(shared, other / "shared.jpg")
    copy(shared, src / "shared-copy.jpg")
    make_image(src / "gone.jpg", color=(4, 5, 6))
    make_image(src / "kept.jpg", color=(7, 8, 9))
    make_image(other / "gone-too.jpg", color=(9, 9, 9))
    source = db.create_source(conn, str(src), "photos")
    second = db.create_source(conn, str(other), "other")
    scan(conn, source, root)
    scan(conn, second, root)
    gone_hash = photo_of(conn, source.id, "gone.jpg")["content_hash"]
    shared_photo = photo_of(conn, source.id, "shared.jpg")
    for name in ("shared.jpg", "shared-copy.jpg", "gone.jpg"):
        (src / name).unlink()
    (other / "gone-too.jpg").unlink()
    scan(conn, source, root)
    scan(conn, second, root)
    assert count(conn, "files") == 6 and count(conn, "photos") == 4
    before = snapshot(src), snapshot(other)

    removed = sources.remove_missing(conn, paths.thumbs_dir(root), source.id)
    assert removed == db.RemovedMissing(files=3, photos=1)
    assert (snapshot(src), snapshot(other)) == before
    # The source's missing rows are gone; its ok files and the other source's missing
    # file are kept.
    rows = conn.execute("SELECT source_id, relative_path, status FROM files ORDER BY id")
    assert sorted(tuple(r) for r in rows) == sorted(
        [
            (source.id, "kept.jpg", "ok"),
            (second.id, "shared.jpg", "ok"),
            (second.id, "gone-too.jpg", "missing"),
        ]
    )
    # The photo with another copy is kept and the copy is its primary.
    assert primary_of(conn, shared_photo["id"]) == (second.id, "shared.jpg")
    assert thumbs.thumb_path(paths.thumbs_dir(root), shared_photo["content_hash"]).exists()
    # The photo left with no files is deleted with its thumbnail.
    assert (
        conn.execute("SELECT 1 FROM photos WHERE content_hash = ?", (gone_hash,)).fetchone() is None
    )
    assert not thumbs.thumb_path(paths.thumbs_dir(root), gone_hash).exists()
    assert count(conn, "photos") == 3
    # Nothing left to remove: a second run is a no-op.
    again = sources.remove_missing(conn, paths.thumbs_dir(root), source.id)
    assert again == db.RemovedMissing(0, 0)


def test_remove_source_keeps_shared_photos_and_their_thumbnails(tmp_path, lib):
    root, conn = lib
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    img = make_image(first_dir / "x.jpg", color=(3, 3, 3))
    copy(img, second_dir / "y.jpg")
    make_image(first_dir / "only.jpg", color=(6, 6, 6))
    first = db.create_source(conn, str(first_dir), "A")
    second = db.create_source(conn, str(second_dir), "B")
    scan(conn, first, root)
    scan(conn, second, root)
    shared = photo_of(conn, first.id, "x.jpg")
    only_hash = photo_of(conn, first.id, "only.jpg")["content_hash"]

    sources.remove_source(conn, paths.thumbs_dir(root), first.id)
    assert count(conn, "photos") == 1
    assert primary_of(conn, shared["id"]) == (second.id, "y.jpg")
    assert [(p.photo_id, p.relative_path) for p in db.grid_photos(conn)] == [
        (shared["id"], "y.jpg")
    ]
    assert thumbs.thumb_path(paths.thumbs_dir(root), shared["content_hash"]).exists()
    assert not thumbs.thumb_path(paths.thumbs_dir(root), only_hash).exists()


def test_dates_from_exif_offset_filename_and_across_copies(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    src.mkdir()
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2020:06:01 12:00:00"
    exif.get_ifd(0x8769)[0x9011] = "-05:00"
    Image.new("RGB", (30, 20), (9, 9, 9)).save(src / "zoned.jpg", exif=exif.tobytes())
    named = make_image(src / "IMG_20190131_235959.jpg", color=(1, 1, 1))
    copy(named, src / "plain-name.jpg")  # same bytes: dated by mtime, filename wins
    make_image(src / "IMG-20180704-WA0001.jpg", color=(2, 2, 2))
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)

    zoned = file_row(conn, source.id, "zoned.jpg")
    assert (zoned["taken_at_local"], zoned["taken_at_utc"], zoned["date_source"]) == (
        "2020-06-01T12:00:00",
        "2020-06-01T17:00:00Z",
        "exif",
    )
    assert (zoned["exif_taken_at"], zoned["exif_offset"]) == ("2020-06-01T12:00:00", "-05:00")
    photo = photo_of(conn, source.id, "zoned.jpg")
    assert (photo["taken_at_utc"], photo["date_source"]) == ("2020-06-01T17:00:00Z", "exif")

    assert file_row(conn, source.id, "plain-name.jpg")["date_source"] == "mtime"
    by_name = photo_of(conn, source.id, "plain-name.jpg")
    assert (by_name["taken_at_local"], by_name["date_source"]) == (
        "2019-01-31T23:59:59",
        "filename",
    )
    whatsapp = photo_of(conn, source.id, "IMG-20180704-WA0001.jpg")
    assert (whatsapp["taken_at_local"], whatsapp["date_source"]) == (
        "2018-07-04T00:00:00",
        "filename",
    )
    # The grid shows each photo's best date.
    grid = {p.photo_id: (p.taken_at_local, p.date_source) for p in db.grid_photos(conn)}
    assert grid[by_name["id"]] == ("2019-01-31T23:59:59", "filename")

    # The better-dated copy going missing keeps the photo's date (all files count).
    (src / "IMG_20190131_235959.jpg").unlink()
    scan(conn, source, root)
    assert photo_of(conn, source.id, "plain-name.jpg")["date_source"] == "filename"


def _make_v0_library(db_file: Path, src: Path) -> None:
    """A schema-1 (V0) database: one photo per file, no hashes, as V0 wrote it."""
    conn = db.connect(db_file)
    with db.transaction(conn):
        db._migration_1(conn)
        conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO sources (id, label, root_path, added_at) VALUES (1, 'photos', ?, ?)",
        (str(src), "2026-01-01T00:00:00Z"),
    )
    for i, name in enumerate(sorted(p.name for p in src.iterdir()), 1):
        st = (src / name).stat()
        conn.execute(
            "INSERT INTO photos (id, width, height, format, taken_at_local, date_source) "
            "VALUES (?, 64, 48, 'JPEG', '2001-01-01T00:00:00', 'mtime')",
            (i,),
        )
        conn.execute(
            "INSERT INTO files (id, source_id, photo_id, relative_path, size, modified_time, "
            "taken_at_local, date_source, first_seen_at, last_seen_at) "
            "VALUES (?, 1, ?, ?, ?, ?, '2001-01-01T00:00:00', 'mtime', ?, ?)",
            (i, i, name, st.st_size, st.st_mtime_ns, f"2026-01-0{i}T00:00:00Z", "2026-01-09Z"),
        )
        conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (i, i))
    conn.close()


def test_v0_library_upgrades_then_rescan_merges_duplicates(tmp_path, library):
    src = tmp_path / "photos"
    a = make_image(src / "a.jpg", taken="2019:01:31 23:59:59")
    copy(a, src / "b.jpg")
    make_image(src / "c.jpg", color=(0, 0, 9))
    root = paths.ensure_library_dir()
    db_file = paths.db_path(root)
    _make_v0_library(db_file, src)

    conn = db.open_database(db_file)
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 3
    assert Path(str(db_file) + ".bak").exists()
    # Schema only: the V0 rows are untouched and still browsable.
    assert count(conn, "photos") == 3
    assert len(db.grid_photos(conn)) == 3
    assert all(p.content_hash is None for p in db.grid_photos(conn))
    assert all(s.needs_hash for s in db.load_file_states(conn, 1))

    source = db.get_source(conn, 1)
    c = scan(conn, source, root)
    assert (c.updated, c.added, c.unchanged, c.errors) == (3, 0, 0, 0)
    assert count(conn, "files") == 3  # file rows keep their ids
    assert {r[0] for r in conn.execute("SELECT id FROM files")} == {1, 2, 3}
    assert count(conn, "photos") == 2
    merged = photo_of(conn, 1, "a.jpg")
    assert merged["id"] == photo_of(conn, 1, "b.jpg")["id"]
    assert merged["content_hash"] and merged["date_source"] == "exif"
    assert primary_of(conn, merged["id"]) == (1, "a.jpg")  # first seen
    assert not any(s.needs_hash for s in db.load_file_states(conn, 1))
    assert all(p.content_hash for p in db.grid_photos(conn))

    # Hashed now: the next scan processes nothing.
    c = scan(conn, source, root)
    assert (c.updated, c.unchanged) == (0, 3)
    conn.close()


def test_v0_unhashed_missing_rows_are_not_reprocessed(tmp_path, library):
    src = tmp_path / "photos"
    make_image(src / "a.jpg")
    make_image(src / "b.jpg", color=(5, 5, 5))
    root = paths.ensure_library_dir()
    _make_v0_library(paths.db_path(root), src)
    (src / "b.jpg").unlink()
    conn = db.open_database(paths.db_path(root))
    c = scan(conn, db.get_source(conn, 1), root)
    assert (c.updated, c.missing) == (1, 1)
    # The missing V0 file keeps its V0 photo (it still has a file row).
    assert count(conn, "photos") == 2
    assert len(db.grid_photos(conn)) == 1
    conn.close()


def test_remove_missing_from_sources_dialog_asks_first(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from imagory.ui.sources_dialog import SourcesDialog
    from tests.test_ui import open_window, wait_scan

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    src = tmp_path / "photos"
    make_image(src / "a.jpg", color=(1, 1, 1))
    make_image(src / "b.jpg", color=(2, 2, 2))
    window, conn = open_window(qtbot)
    window.add_source(src, "Phone")
    wait_scan(qtbot, window)
    (src / "a.jpg").unlink()
    window.rescan()
    wait_scan(qtbot, window)
    dialog = SourcesDialog(window)
    assert dialog.remove_missing_button.isEnabled()
    before = snapshot(src)

    asked = []

    def answer(_parent, title, text, *args):
        asked.append((title, text))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", answer)
    dialog.remove_missing_button.click()
    assert asked[0][0] == "Remove missing items" and "1 missing file" in asked[0][1]
    assert count(conn, "files") == 2  # cancelled: kept

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.remove_missing_button.click()
    assert count(conn, "files") == 1 and count(conn, "photos") == 1
    assert not dialog.remove_missing_button.isEnabled()  # nothing missing any more
    assert [p.relative_path for p in window.model.photos] == ["b.jpg"]
    assert snapshot(src) == before
    dialog.close()
    window.close()
    conn.close()


def test_merged_photo_keeps_its_album_memberships(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "photos"
    b = make_image(src / "b.jpg", color=(30, 30, 30))
    make_image(src / "solo.jpg", color=(20, 20, 20))
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)
    solo = photo_of(conn, source.id, "solo.jpg")["id"]
    album = db.create_album(conn, "Holiday")
    db.add_photos_to_album(conn, album, [solo])

    # solo.jpg becomes an exact copy of b.jpg: its photo merges into b's and is deleted,
    # and the album membership follows the file.
    shutil.copyfile(b, src / "solo.jpg")
    scan(conn, source, root)
    b_photo = photo_of(conn, source.id, "b.jpg")["id"]
    assert conn.execute("SELECT 1 FROM photos WHERE id = ?", (solo,)).fetchone() is None
    assert db.albums_for_photo(conn, b_photo) == [album]
