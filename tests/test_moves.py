"""Moved files (PRD §8.2): a disappeared file and a new file with the same hash in one
source re-point the existing row instead of adding one."""

import os
import shutil
from pathlib import Path

import pytest

from imagory import db, paths
from imagory.scanner import Scanner
from tests.imaging import make_image
from tests.test_scan import snapshot


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def run_scan(conn, source, root, **kwargs):
    """Scan and check the source folder is unchanged: same files, sizes, mtimes."""
    folder = Path(source.root_path)
    before = snapshot(folder)
    scanner = Scanner(conn, source, paths.thumbs_dir(root), root, workers=2, **kwargs)
    summary = scanner.run()
    assert snapshot(folder) == before
    return summary


def scan(conn, source, root, **kwargs) -> db.ScanCounts:
    return run_scan(conn, source, root, **kwargs).counts


def rows(conn, source_id: int) -> dict[str, tuple]:
    return {
        r["relative_path"]: (r["id"], r["status"], r["first_seen_at"], r["photo_id"])
        for r in conn.execute("SELECT * FROM files WHERE source_id = ?", (source_id,))
    }


def move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)


@pytest.fixture
def photos(tmp_path):
    src = tmp_path / "photos"
    make_image(src / "a.jpg", color=(1, 0, 0), taken="2019:01:31 23:59:59")
    make_image(src / "b.jpg", color=(0, 1, 0))
    return src


def test_moved_file_keeps_its_row(photos, lib):
    root, conn = lib
    source = db.create_source(conn, str(photos), "photos")
    scan(conn, source, root)
    before = rows(conn, source.id)
    move(photos / "a.jpg", photos / "2019" / "renamed.jpg")

    c = scan(conn, source, root)
    assert (c.found, c.added, c.updated, c.moved, c.missing, c.unchanged) == (2, 0, 0, 1, 0, 1)
    after = rows(conn, source.id)
    assert set(after) == {"2019/renamed.jpg", "b.jpg"}
    assert after["2019/renamed.jpg"] == before["a.jpg"]  # same id, status ok, first seen, photo
    assert count_photos(conn) == 2
    assert sorted(p.relative_path for p in db.grid_photos(conn)) == ["2019/renamed.jpg", "b.jpg"]
    run = db.scan_runs(conn, source.id)[-1]
    assert (run["moved"], run["missing"], run["added"]) == (1, 0, 0)


def count_photos(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]


def test_move_is_found_after_a_cancelled_scan(photos, lib):
    root, conn = lib
    source = db.create_source(conn, str(photos), "photos")
    scan(conn, source, root)
    before = rows(conn, source.id)
    move(photos / "a.jpg", photos / "elsewhere" / "a.jpg")

    # Cancel once processing starts: the disappearance is committed, the new file is not.
    scanner_ref: list[Scanner] = []

    def stop(progress):
        if progress.phase == "processing":
            scanner_ref[0].cancel()

    scanner = Scanner(conn, source, paths.thumbs_dir(root), root, workers=1, progress=stop)
    scanner_ref.append(scanner)
    snap = snapshot(photos)
    first = scanner.run()
    assert snapshot(photos) == snap
    assert first.status == "cancelled"
    assert (first.counts.missing, first.counts.added, first.counts.moved) == (1, 0, 0)
    assert rows(conn, source.id)["a.jpg"][1] == "missing"

    # The next scan needs no saved state: the missing row is re-pointed.
    c = scan(conn, source, root)
    assert (c.moved, c.added, c.missing) == (1, 0, 0)
    after = rows(conn, source.id)
    assert after["elsewhere/a.jpg"] == (*before["a.jpg"][:1], "ok", *before["a.jpg"][2:])
    assert "a.jpg" not in after


def test_hidden_photo_reappears_in_grid_when_moved_back(photos, lib, tmp_path):
    root, conn = lib
    source = db.create_source(conn, str(photos), "photos")
    scan(conn, source, root)
    aside = tmp_path / "aside.jpg"
    move(photos / "a.jpg", aside)
    scan(conn, source, root)
    assert [p.relative_path for p in db.grid_photos(conn)] == ["b.jpg"]

    move(aside, photos / "new place" / "a.jpg")
    batches: list[list[db.GridPhoto]] = []
    c = scan(conn, source, root, on_added=batches.append)
    assert (c.moved, c.added) == (1, 0)
    assert [p.relative_path for batch in batches for p in batch] == ["new place/a.jpg"]


def test_two_new_copies_of_one_missing_file(photos, lib):
    root, conn = lib
    source = db.create_source(conn, str(photos), "photos")
    scan(conn, source, root)
    shutil.copy2(photos / "a.jpg", photos / "copy2.jpg")
    move(photos / "a.jpg", photos / "copy1.jpg")
    c = scan(conn, source, root)
    assert (c.moved, c.added, c.missing) == (1, 1, 0)
    after = rows(conn, source.id)
    assert after["copy1.jpg"][3] == after["copy2.jpg"][3]  # one photo
    assert all(r[1] == "ok" for r in after.values())
    assert count_photos(conn) == 2


def test_move_to_another_source_is_not_a_move(photos, lib, tmp_path):
    root, conn = lib
    other = tmp_path / "other"
    other.mkdir()
    first = db.create_source(conn, str(photos), "photos")
    second = db.create_source(conn, str(other), "other")
    scan(conn, first, root)
    scan(conn, second, root)
    move(photos / "a.jpg", other / "a.jpg")
    assert scan(conn, first, root).missing == 1
    c = scan(conn, second, root)
    assert (c.moved, c.added) == (0, 1)
    # Same photo, now with a missing copy in the first source and an ok one in the second.
    a_first, a_second = rows(conn, first.id)["a.jpg"], rows(conn, second.id)["a.jpg"]
    assert a_first[1] == "missing" and a_second[1] == "ok"
    assert a_first[3] == a_second[3]
    assert {(p.source_id, p.relative_path) for p in db.grid_photos(conn)} == {
        (second.id, "a.jpg"),
        (first.id, "b.jpg"),
    }


def test_changed_contents_at_a_new_path_is_not_a_move(photos, lib):
    root, conn = lib
    source = db.create_source(conn, str(photos), "photos")
    scan(conn, source, root)
    (photos / "a.jpg").unlink()
    make_image(photos / "other.jpg", color=(9, 9, 9))
    c = scan(conn, source, root)
    assert (c.moved, c.added, c.missing) == (0, 1, 1)
