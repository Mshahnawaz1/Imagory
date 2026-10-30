"""Integration tests: scan generated fixture folders into a temporary library (PRD §7.5)."""

import os
from pathlib import Path

import pytest

from imagory import db, paths, thumbs
from imagory.scanner import Scanner
from tests.imaging import make_image


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file and folder under ``root`` with its size and modified time."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            snap[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return snap


@pytest.fixture
def source_dir(tmp_path):
    src = tmp_path / "photos"
    make_image(src / "2019" / "a.jpg", (300, 200), taken="2019:01:31 23:59:59")
    make_image(src / "2019" / "b.JPG", (200, 300), taken="2019:02:01 10:00:00", orientation=6)
    make_image(src / "2020" / "c.png", (120, 80))
    make_image(src / "2020" / "deep" / "d.gif", (50, 50))
    make_image(src / "e.heic", (64, 64))
    (src / "notes.txt").write_text("not a photo")
    (src / "clip.mp4").write_bytes(b"\x00" * 10)
    make_image(src / ".hidden" / "skip.jpg")
    return src


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def scan(conn, source, root, **kwargs):
    scanner = Scanner(conn, source, paths.thumbs_dir(root), root, workers=2, **kwargs)
    return scanner, scanner.run()


def test_full_scan_rescan_modify_delete(source_dir, lib):
    root, conn = lib
    before = snapshot(source_dir)
    source = db.create_source(conn, str(source_dir), source_dir.name)

    _, summary = scan(conn, source, root)
    c = summary.counts
    assert summary.status == "completed"
    assert (c.found, c.added, c.updated, c.unchanged, c.missing, c.errors) == (5, 5, 0, 0, 0, 0)
    assert c.skipped_by_ext == {".mp4": 1, ".txt": 1}

    rows = {r["relative_path"]: r for r in conn.execute("SELECT * FROM files")}
    assert set(rows) == {"2019/a.jpg", "2019/b.JPG", "2020/c.png", "2020/deep/d.gif", "e.heic"}
    assert all(r["status"] == "ok" and r["content_hash"] is None for r in rows.values())
    assert rows["2019/a.jpg"]["taken_at_local"] == "2019-01-31T23:59:59"
    assert rows["2019/a.jpg"]["date_source"] == "exif"
    assert rows["2020/c.png"]["date_source"] == "mtime"
    photo = conn.execute(
        "SELECT p.* FROM photos p JOIN files f ON f.id = p.primary_file_id "
        "WHERE f.relative_path = '2019/b.JPG'"
    ).fetchone()
    assert (photo["width"], photo["height"], photo["format"]) == (300, 200, "JPEG")
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5
    for rel in rows:
        assert thumbs.thumb_path(paths.thumbs_dir(root), rel).exists()
    grid = db.grid_photos(conn, source.id)
    # Newest first: mtime-dated files (today) come before the 2019 EXIF dates.
    assert [p.relative_path for p in grid][-2:] == ["2019/b.JPG", "2019/a.jpg"]
    assert snapshot(source_dir) == before

    # A second scan with no changes processes nothing.
    _, summary = scan(conn, source, root)
    c = summary.counts
    assert (c.added, c.updated, c.unchanged, c.missing, c.errors) == (0, 0, 5, 0, 0)

    # Modify one file and delete another: 1 updated and 1 missing.
    make_image(source_dir / "2020" / "c.png", (240, 160), color=(0, 0, 255))
    (source_dir / "2019" / "a.jpg").unlink()
    _, summary = scan(conn, source, root)
    c = summary.counts
    assert (c.added, c.updated, c.unchanged, c.missing, c.errors) == (0, 1, 3, 1, 0)
    status = dict(conn.execute("SELECT relative_path, status FROM files").fetchall())
    assert status["2019/a.jpg"] == "missing"
    assert "2019/a.jpg" not in [p.relative_path for p in db.grid_photos(conn, source.id)]
    width = conn.execute(
        "SELECT p.width FROM photos p JOIN files f ON f.id = p.primary_file_id "
        "WHERE f.relative_path = '2020/c.png'"
    ).fetchone()[0]
    assert width == 240

    # A missing file that reappears goes back to ok without being reprocessed.
    make_image(source_dir / "2019" / "a.jpg", (300, 200), taken="2019:01:31 23:59:59")
    _, summary = scan(conn, source, root)
    c = summary.counts
    # The re-created file has a new mtime, so it counts as updated.
    assert (c.added, c.updated, c.missing) == (0, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM files WHERE status = 'ok'").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5

    runs = db.scan_runs(conn, source.id)
    assert [r["status"] for r in runs] == ["completed"] * 4
    assert runs[2]["updated"] == 1 and runs[2]["missing"] == 1


def test_reappearing_unchanged_file_returns_to_ok(source_dir, lib, tmp_path):
    root, conn = lib
    source = db.create_source(conn, str(source_dir), "photos")
    scan(conn, source, root)
    moved = tmp_path / "aside.jpg"
    os.replace(source_dir / "2019" / "a.jpg", moved)
    assert scan(conn, source, root)[1].counts.missing == 1
    os.replace(moved, source_dir / "2019" / "a.jpg")  # same size and mtime
    c = scan(conn, source, root)[1].counts
    assert (c.added, c.updated, c.unchanged, c.missing) == (0, 0, 5, 0)
    assert len(db.grid_photos(conn, source.id)) == 5


def test_corrupt_image_is_counted_and_scan_continues(source_dir, lib):
    root, conn = lib
    (source_dir / "broken.jpg").write_bytes(b"\xff\xd8\xff garbage")
    (source_dir / "empty.png").write_bytes(b"")
    before = snapshot(source_dir)
    source = db.create_source(conn, str(source_dir), "photos")
    _, summary = scan(conn, source, root)
    c = summary.counts
    assert summary.status == "completed"
    assert (c.found, c.added, c.errors) == (7, 5, 2)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5
    assert snapshot(source_dir) == before


def test_cancel_then_resume(tmp_path, lib):
    root, conn = lib
    src = tmp_path / "many"
    for i in range(40):
        make_image(src / f"img{i:03}.jpg", (32, 32), color=(i * 5, 0, 0))
    before = snapshot(src)
    source = db.create_source(conn, str(src), "many")

    scanner = Scanner(
        conn, source, paths.thumbs_dir(root), root, workers=1, commit_batch=4, worker_batch=2
    )

    def stop_early(progress):
        if progress.phase == "processing" and progress.processed >= 8:
            scanner.cancel()

    scanner._progress = stop_early
    first = scanner.run()
    assert first.status == "cancelled"
    committed = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert committed == first.counts.added
    assert 8 <= committed < 40

    _, second = scan(conn, source, root)
    assert second.status == "completed"
    assert second.counts.added == 40 - committed
    assert second.counts.unchanged == committed
    assert second.counts.updated == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 40
    assert conn.execute("SELECT COUNT(DISTINCT relative_path) FROM files").fetchone()[0] == 40
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 40
    statuses = [r["status"] for r in db.scan_runs(conn, source.id)]
    assert statuses == ["cancelled", "completed"]
    assert snapshot(src) == before


def test_library_inside_source_is_skipped(tmp_path, monkeypatch):
    src = tmp_path / "photos"
    make_image(src / "a.jpg")
    monkeypatch.setenv("IMAGORY_LIBRARY", str(src / "ImagoryLib"))
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    source = db.create_source(conn, str(src), "photos")
    scan(conn, source, root)
    # Thumbnails now exist inside the source; a rescan must not pick them up.
    c = scan(conn, source, root)[1].counts
    assert (c.found, c.added, c.unchanged) == (1, 0, 1)
    conn.close()


def test_missing_source_folder_fails_cleanly(tmp_path, lib):
    root, conn = lib
    source = db.create_source(conn, str(tmp_path / "nope"), "nope")
    with pytest.raises(FileNotFoundError):
        scan(conn, source, root)
    assert [r["status"] for r in db.scan_runs(conn, source.id)] == ["failed"]


def test_change_folder_deletes_rows_and_thumbnails(source_dir, lib):
    root, conn = lib
    before = snapshot(source_dir)
    source = db.create_source(conn, str(source_dir), "photos")
    scan(conn, source, root)
    db.delete_source(conn, source.id)
    thumbs.clear_thumbs(paths.thumbs_dir(root))
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert not any(p.is_file() for p in paths.thumbs_dir(root).rglob("*"))
    assert snapshot(source_dir) == before


def test_on_added_reports_each_committed_photo(source_dir, lib):
    root, conn = lib
    source = db.create_source(conn, str(source_dir), source_dir.name)
    batches: list[list[db.GridPhoto]] = []
    scan(conn, source, root, commit_batch=2, worker_batch=1, on_added=batches.append)
    assert len(batches) > 1  # reported per commit, not only at the end
    reported = sorted(p.relative_path for batch in batches for p in batch)
    assert reported == sorted(p.relative_path for p in db.grid_photos(conn, source.id))

    # A rescan with nothing new reports nothing.
    batches.clear()
    scan(conn, source, root, on_added=batches.append)
    assert batches == []
