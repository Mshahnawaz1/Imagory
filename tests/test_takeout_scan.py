"""Takeout sidecars wired into the scan: dates, location, Trash, edited copies, report."""

import json
import logging
from datetime import UTC, datetime

import pytest

from imagory import db, paths
from imagory.scanner import Scanner
from tests.imaging import make_image
from tests.test_scan import snapshot

TS = datetime(2018, 6, 15, 12, 0, 0, tzinfo=UTC)
YEAR = "Google Photos/Photos from 2018"


def write_sidecar(path, when=TS, title=None, lat=0.0, lon=0.0):
    data = {
        "title": title or path.name.split(".json")[0],
        "photoTakenTime": {"timestamp": str(int(when.timestamp()))},
        "geoData": {"latitude": lat, "longitude": lon},
    }
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def takeout_dir(tmp_path):
    year = tmp_path / "Takeout" / YEAR
    make_image(year / "IMG_1.jpg", color=(1, 2, 3))
    write_sidecar(year / "IMG_1.jpg.supplemental-metadata.json", lat=48.5, lon=2.25)
    make_image(year / "IMG_1-edited.jpg", color=(9, 9, 9))  # no sidecar of its own
    make_image(year / "IMG_2.jpg", color=(4, 5, 6), taken="2018:06:15 14:30:00")
    write_sidecar(year / "IMG_2.jpg.json")  # JSON 12:00Z, EXIF 14:30: a zone offset
    make_image(year / "IMG_3.jpg", color=(7, 8, 9))  # no sidecar: mtime
    write_sidecar(year / "orphan.jpg.json")
    trash = tmp_path / "Takeout" / "Google Photos" / "Trash"
    make_image(trash / "gone.jpg", color=(50, 60, 70))
    write_sidecar(trash / "gone.jpg.json")
    return tmp_path / "Takeout"


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def run_scan(conn, root, src_dir, type="takeout"):
    source = db.create_source(conn, str(src_dir), "T", type)
    Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()
    return source


def file_row(conn, source, rel):
    return tuple(
        conn.execute(
            "SELECT f.taken_at_local, f.taken_at_utc, f.date_source, f.sidecar_relative_path, "
            "p.latitude, p.longitude FROM files f JOIN photos p ON p.id = f.photo_id "
            "WHERE f.source_id = ? AND f.relative_path = ?",
            (source.id, rel),
        ).fetchone()
    )


STABLE_COLUMNS = (
    "SELECT id, relative_path, taken_at_local, taken_at_utc, date_source, "
    "sidecar_relative_path FROM files ORDER BY id"
)


def count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_takeout_scan_dates_location_trash(takeout_dir, lib, caplog):
    root, conn = lib
    before = snapshot(takeout_dir)
    with caplog.at_level(logging.INFO):
        source = run_scan(conn, root, takeout_dir)
    assert snapshot(takeout_dir) == before

    local, utc, how, sidecar, lat, lon = file_row(conn, source, f"{YEAR}/IMG_1.jpg")
    assert how == "takeout_json" and utc == "2018-06-15T12:00:00Z"
    assert sidecar == f"{YEAR}/IMG_1.jpg.supplemental-metadata.json"
    assert (lat, lon) == (48.5, 2.25)

    # edited copy: separate photo, dated and located from the original's sidecar
    local, utc, how, sidecar, lat, lon = file_row(conn, source, f"{YEAR}/IMG_1-edited.jpg")
    assert how == "takeout_json" and utc == "2018-06-15T12:00:00Z"
    assert sidecar == f"{YEAR}/IMG_1.jpg.supplemental-metadata.json"
    assert (lat, lon) == (48.5, 2.25)

    # JSON and EXIF within 24 h: EXIF wall clock kept, JSON UTC kept
    local, utc, how, *_ = file_row(conn, source, f"{YEAR}/IMG_2.jpg")
    assert (local, utc, how) == ("2018-06-15T14:30:00", "2018-06-15T12:00:00Z", "takeout_json")

    row = file_row(conn, source, f"{YEAR}/IMG_3.jpg")
    assert row[2] == "mtime" and row[3] is None

    # Trash is skipped
    assert count(conn, "SELECT COUNT(*) FROM files WHERE relative_path LIKE '%Trash%'") == 0
    assert count(conn, "SELECT COUNT(*) FROM photos") == 4

    assert "Takeout sidecars: 3 matched, 1 images without a sidecar, 1 unused sidecars" in (
        caplog.text
    )
    assert "orphan.jpg.json" in caplog.text


def test_plain_source_ignores_sidecars(takeout_dir, lib):
    root, conn = lib
    source = run_scan(conn, root, takeout_dir, type="plain")
    row = file_row(conn, source, f"{YEAR}/IMG_1.jpg")
    assert row[2] == "mtime" and row[3] is None and row[4] is None
    # plain sources do not skip Trash
    assert count(conn, "SELECT COUNT(*) FROM files WHERE relative_path LIKE '%Trash%'") == 1


def test_rescan_unchanged_keeps_sidecar_data(takeout_dir, lib):
    root, conn = lib
    source = run_scan(conn, root, takeout_dir)
    before = [tuple(r) for r in conn.execute(STABLE_COLUMNS)]
    summary = Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()
    assert summary.counts.unchanged == 4 and summary.counts.added == 0
    assert [tuple(r) for r in conn.execute(STABLE_COLUMNS)] == [tuple(r) for r in before]
