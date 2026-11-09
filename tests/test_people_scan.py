"""Takeout people and metadata (PRD §8.9) through the scan, and parse_sidecar's odd shapes."""

import json
import os
from datetime import UTC, datetime

import pytest

from imagory import db, paths, takeout
from imagory.scanner import Scanner
from tests.db_helpers import count
from tests.imaging import make_image
from tests.test_scan import snapshot

TS = datetime(2018, 6, 15, 12, 0, 0, tzinfo=UTC)
A = "Google Photos/Photos from 2018"
B = "Google Photos/Trip"


def write_sidecar(path, **extra):
    data = {
        "title": path.name.split(".json")[0],
        "photoTakenTime": {"timestamp": str(int(TS.timestamp()))},
        "geoData": {"latitude": 0.0, "longitude": 0.0},
        **extra,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def names(*people):
    return [{"name": n} for n in people]


@pytest.fixture
def takeout_dir(tmp_path):
    root = tmp_path / "Takeout"
    make_image(root / A / "IMG_1.jpg", color=(1, 2, 3))
    write_sidecar(
        root / A / "IMG_1.jpg.json",
        people=names("Anna", "Ben"),
        description="Beach day",
        favorited=True,
        creationTime={"timestamp": "1530000000"},
    )
    make_image(root / A / "IMG_1-edited.jpg", color=(9, 9, 9))  # no sidecar of its own
    # the same photo in another folder, tagged differently
    make_image(root / B / "IMG_1.jpg", color=(1, 2, 3))
    write_sidecar(root / B / "IMG_1.jpg.json", people=names("ben", "Chloé"), description="Trip")
    make_image(root / A / "IMG_2.jpg", color=(4, 5, 6))
    write_sidecar(root / A / "IMG_2.jpg.json")  # nobody tagged
    return root


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


def scan(conn, root, source):
    return Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()


def photo_id(conn, source, rel):
    return conn.execute(
        "SELECT photo_id FROM files WHERE source_id = ? AND relative_path = ?", (source.id, rel)
    ).fetchone()[0]


def test_people_stored_merged_and_inherited(takeout_dir, lib):
    root, conn = lib
    before = snapshot(takeout_dir)
    source = db.create_source(conn, str(takeout_dir), "T", "takeout")
    scan(conn, root, source)
    assert snapshot(takeout_dir) == before

    original = photo_id(conn, source, f"{A}/IMG_1.jpg")
    assert original == photo_id(conn, source, f"{B}/IMG_1.jpg")  # exact duplicates
    assert db.people_for_photo(conn, original) == ["Anna", "Ben", "Chloé"]  # union, case folded

    info = db.photo_info(conn, original)
    assert info.people == ("Anna", "Ben", "Chloé")
    assert info.description in ("Beach day", "Trip")  # the first non-empty caption stays
    assert info.favorite is True
    assert info.uploaded_at_utc == "2018-06-26T08:00:00Z"

    # the edited copy is its own photo and takes the original's sidecar data
    edited = photo_id(conn, source, f"{A}/IMG_1-edited.jpg")
    assert edited != original
    assert db.people_for_photo(conn, edited) == ["Anna", "Ben"]
    assert db.photo_info(conn, edited).description == "Beach day"

    untagged = db.photo_info(conn, photo_id(conn, source, f"{A}/IMG_2.jpg"))
    assert untagged.people == () and untagged.description is None and not untagged.favorite

    assert [(p.name, p.photo_count) for p in db.list_people(conn)] == [
        ("Anna", 2),
        ("Ben", 2),
        ("Chloé", 1),
    ]


def test_rescan_never_removes_people(takeout_dir, lib):
    root, conn = lib
    source = db.create_source(conn, str(takeout_dir), "T", "takeout")
    scan(conn, root, source)
    original = photo_id(conn, source, f"{A}/IMG_1.jpg")

    write_sidecar(takeout_dir / A / "IMG_1.jpg.json", people=names("Dev"))
    image = takeout_dir / A / "IMG_1.jpg"
    os.utime(image, (image.stat().st_atime, image.stat().st_mtime + 100))  # force a re-read
    summary = scan(conn, root, source)
    assert summary.counts.updated >= 1
    assert db.people_for_photo(conn, original) == ["Anna", "Ben", "Chloé", "Dev"]


def test_plain_source_reads_no_people(takeout_dir, lib):
    root, conn = lib
    source = db.create_source(conn, str(takeout_dir), "P", "plain")
    scan(conn, root, source)
    assert db.list_people(conn) == []


def test_person_filter_and_text_search(takeout_dir, lib):
    root, conn = lib
    source = db.create_source(conn, str(takeout_dir), "T", "takeout")
    scan(conn, root, source)
    anna = next(p for p in db.list_people(conn) if p.name == "Anna")
    assert count(conn, db.PhotoFilter(person_id=anna.id)) == 2
    assert count(conn, db.PhotoFilter(text="chlo")) == 1  # person name
    assert count(conn, db.PhotoFilter(text="beach")) == 2  # caption (+ edited copy)
    assert count(conn, db.PhotoFilter(text="IMG_2")) == 1  # path still works
    assert count(conn, db.PhotoFilter(text="anna", person_id=anna.id)) == 2
    assert count(conn, db.PhotoFilter(text="nobody")) == 0


# --- parse_sidecar ---------------------------------------------------------------------


def parse(**data):
    return takeout.parse_sidecar({"title": "x.jpg", **data})


def test_parse_people_trims_dedupes_and_drops_blanks():
    side = parse(people=names("  Anna  ", "anna", "", "   ", "Ben  Lee"))
    assert side.people == ("Anna", "Ben Lee")


@pytest.mark.parametrize(
    "people",
    [None, "Anna", 5, {}, [], [None, 3, "Anna", {"nom": "x"}, {"name": 7}, {"name": None}]],
)
def test_parse_people_odd_shapes(people):
    assert parse(people=people).people == ()


def test_parse_people_caps():
    side = parse(people=names(*[f"P{i}" for i in range(500)], "x" * 1000))
    assert len(side.people) == takeout.MAX_PEOPLE
    long = parse(people=names("y" * 1000)).people[0]
    assert len(long) == takeout.MAX_NAME_LENGTH


def test_parse_description_favorite_uploaded():
    side = parse(description="  hi  ", favorited=True, creationTime={"timestamp": "1530000000"})
    assert (side.description, side.favorite, side.uploaded_utc) == (
        "hi",
        True,
        "2018-06-26T08:00:00Z",
    )


@pytest.mark.parametrize("value", [None, "", "  ", 5, ["a"]])
def test_parse_description_odd(value):
    assert parse(description=value).description is None


@pytest.mark.parametrize("value", [None, "true", 1, "yes", False])
def test_parse_favorite_only_true_counts(value):
    assert parse(favorited=value).favorite is False


@pytest.mark.parametrize("value", [None, {}, {"timestamp": "abc"}, {"timestamp": "0"}, "x", 5])
def test_parse_uploaded_odd(value):
    assert parse(creationTime=value).uploaded_utc is None


def test_new_database_has_people_tables_and_takeout_columns(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"people", "photo_people"} <= tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
        assert {"description", "favorite", "uploaded_at_utc"} <= cols
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(photo_people)")}
        assert "idx_photo_people_person_id" in indexes
    finally:
        conn.close()
