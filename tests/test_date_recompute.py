"""Changing a source's type recomputes its dates from the stored inputs (PRD §8.1)."""

import shutil

import pytest

from imagory import db, paths, sources
from imagory.scanner import Scanner
from tests.imaging import make_image
from tests.test_scan import snapshot
from tests.test_takeout_scan import TS, YEAR, file_row, write_sidecar


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def make_source(tmp_path):
    folder = tmp_path / "Takeout" / YEAR
    make_image(folder / "IMG_1.jpg", color=(1, 2, 3))
    write_sidecar(folder / "IMG_1.jpg.json", lat=10.0, lon=20.0)
    make_image(folder / "IMG_20170305_101500.jpg", color=(4, 4, 4))
    make_image(folder / "IMG_9.jpg", color=(5, 5, 5), taken="2016:01:02 03:04:05")
    write_sidecar(folder / "IMG_9.jpg.json", when=TS)
    return tmp_path / "Takeout"


def test_type_change_round_trip(tmp_path, lib):
    root, conn = lib
    src_dir = make_source(tmp_path)
    before = snapshot(src_dir)
    source = db.create_source(conn, str(src_dir), "T", "plain")
    Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()
    assert file_row(conn, source, f"{YEAR}/IMG_1.jpg")[2] == "mtime"
    assert file_row(conn, source, f"{YEAR}/IMG_9.jpg")[2] == "exif"

    # IMG_1 and IMG_9 now use the JSON; the filename-dated photo is unchanged
    assert sources.change_source_type(conn, source.id, "takeout") == 2
    local, utc, how, sidecar, lat, lon = file_row(conn, source, f"{YEAR}/IMG_1.jpg")
    assert (how, utc, sidecar) == ("takeout_json", "2018-06-15T12:00:00Z", f"{YEAR}/IMG_1.jpg.json")
    assert (lat, lon) == (10.0, 20.0)
    assert file_row(conn, source, f"{YEAR}/IMG_9.jpg")[2] == "takeout_json"
    assert file_row(conn, source, f"{YEAR}/IMG_20170305_101500.jpg")[:3] == (
        "2017-03-05T10:15:00",
        None,
        "filename",
    )
    (photo_source,) = conn.execute(
        "SELECT p.date_source FROM photos p JOIN files f ON f.photo_id = p.id "
        "WHERE f.relative_path LIKE '%IMG_1.jpg'"
    ).fetchone()
    assert photo_source == "takeout_json"

    assert sources.change_source_type(conn, source.id, "plain") == 2
    local, utc, how, sidecar, *_ = file_row(conn, source, f"{YEAR}/IMG_9.jpg")
    assert (local, how, sidecar) == ("2016-01-02T03:04:05", "exif", None)
    assert file_row(conn, source, f"{YEAR}/IMG_1.jpg")[2] == "mtime"
    assert db.get_source(conn, source.id).type == "plain"
    assert snapshot(src_dir) == before


def test_recompute_takeout_with_source_folder_gone(tmp_path, lib):
    root, conn = lib
    src_dir = make_source(tmp_path)
    source = db.create_source(conn, str(src_dir), "T", "takeout")
    Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()
    shutil.rmtree(src_dir)  # unplugged drive: sidecars unreadable, stored dates must stay
    with pytest.raises(FileNotFoundError):
        sources.recompute_dates(conn, db.get_source(conn, source.id))
    assert file_row(conn, source, f"{YEAR}/IMG_9.jpg")[2] == "takeout_json"
