"""People storage, merge rules, filtering, and search (PRD §8.7, §8.9)."""

from __future__ import annotations

import pytest

from imagory import db
from imagory.db import PhotoFilter
from tests.db_helpers import count


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "imagory.db")
    yield c
    c.close()


def add_source(conn, label="S") -> int:
    return conn.execute(
        "INSERT INTO sources (label, root_path, added_at) VALUES (?, ?, 'z')",
        (label, f"/x/{label}"),
    ).lastrowid


def add_photo(conn, source_id, copies, taken=None):
    """copies: list of (relative_path, status). Returns (photo id, first file id)."""
    n = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    pid = conn.execute(
        "INSERT INTO photos (content_hash, taken_at_local, date_source) VALUES (?, ?, 'exif')",
        (f"h{n}", taken),
    ).lastrowid
    file_ids = []
    for path, status in copies:
        file_ids.append(
            conn.execute(
                "INSERT INTO files (source_id, photo_id, relative_path, size, modified_time, "
                "status, first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, 1, ?, 'z', 'z')",
                (source_id, pid, path, status),
            ).lastrowid
        )
    conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (file_ids[0], pid))
    return pid, file_ids[0]


def person_id(conn, name):
    return conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()[0]


def test_names_unioned_and_unique_ignoring_case(conn):
    s = add_source(conn)
    pid, f1 = add_photo(conn, s, [("a.jpg", "ok"), ("b/a.jpg", "ok")])
    f2 = conn.execute("SELECT id FROM files WHERE relative_path = 'b/a.jpg'").fetchone()[0]
    db.set_photo_takeout_metadata(conn, f1, ["Anna", "Bo"], None, False, None)
    db.set_photo_takeout_metadata(conn, f2, ["anna", "Cy"], None, False, None)
    assert db.people_for_photo(conn, pid) == ["Anna", "Bo", "Cy"]
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3


def test_rescan_never_removes_names(conn):
    s = add_source(conn)
    pid, f1 = add_photo(conn, s, [("a.jpg", "ok")])
    db.set_photo_takeout_metadata(conn, f1, ["Anna", "Bo"], None, False, None)
    db.set_photo_takeout_metadata(conn, f1, ["Anna"], None, False, None)
    assert db.people_for_photo(conn, pid) == ["Anna", "Bo"]


def test_caption_favourite_and_upload_merge_rules(conn):
    s = add_source(conn)
    pid, f1 = add_photo(conn, s, [("a.jpg", "ok")])
    db.set_photo_takeout_metadata(conn, f1, [], None, False, None)
    db.set_photo_takeout_metadata(conn, f1, [], "First", True, "2020-01-01T00:00:00Z")
    db.set_photo_takeout_metadata(conn, f1, [], "Second", False, "2021-01-01T00:00:00Z")
    info = db.photo_info(conn, pid)
    assert info.description == "First"
    assert info.favorite is True
    assert info.uploaded_at_utc == "2020-01-01T00:00:00Z"


def test_unknown_file_is_ignored(conn):
    db.set_photo_takeout_metadata(conn, 999, ["Anna"], "x", True, None)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


def test_list_people_counts_visible_photos_only(conn):
    s = add_source(conn)
    a, fa = add_photo(conn, s, [("a.jpg", "ok")])
    b, fb = add_photo(conn, s, [("b.jpg", "ok")])
    c, fc = add_photo(conn, s, [("c.jpg", "missing")])
    db.set_photo_takeout_metadata(conn, fa, ["Anna", "Bo"], None, False, None)
    db.set_photo_takeout_metadata(conn, fb, ["Anna"], None, False, None)
    db.set_photo_takeout_metadata(conn, fc, ["Ghost"], None, False, None)
    people = db.list_people(conn)
    assert [(p.name, p.photo_count) for p in people] == [("Anna", 2), ("Bo", 1)]


def test_deleting_a_photo_removes_links_but_not_people(conn):
    s = add_source(conn)
    pid, f1 = add_photo(conn, s, [("a.jpg", "ok")])
    db.set_photo_takeout_metadata(conn, f1, ["Anna"], None, False, None)
    conn.execute("DELETE FROM files WHERE photo_id = ?", (pid,))
    conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
    assert conn.execute("SELECT COUNT(*) FROM photo_people").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
    assert db.list_people(conn) == []


@pytest.fixture
def lib(conn):
    s = add_source(conn)
    a, fa = add_photo(conn, s, [("Trips/a.jpg", "ok")], "2020-07-01T10:00:00")
    b, fb = add_photo(conn, s, [("Home/b.jpg", "ok")], "2020-08-01T10:00:00")
    c, fc = add_photo(conn, s, [("Home/c.jpg", "ok")], "2021-01-05T10:00:00")
    d, fd = add_photo(conn, s, [("Home/d.jpg", "missing")], "2021-02-05T10:00:00")
    db.set_photo_takeout_metadata(conn, fa, ["Anna"], "100% fun", False, None)
    db.set_photo_takeout_metadata(conn, fb, ["Anna", "Bo_b"], "snow_day", False, None)
    db.set_photo_takeout_metadata(conn, fc, ["Bo_b"], None, False, None)
    db.set_photo_takeout_metadata(conn, fd, ["Anna"], None, False, None)
    return {"a": a, "b": b, "c": c, "d": d}


def ids(conn, f):
    return {g.photo_id for g in db.filtered_grid_photos(conn, f)}


def test_person_filter(conn, lib):
    anna = person_id(conn, "Anna")
    assert ids(conn, PhotoFilter(person_id=anna)) == {lib["a"], lib["b"]}
    assert ids(conn, PhotoFilter(person_id=anna, missing=True)) == {lib["d"]}


def test_person_filter_combines_with_other_filters(conn, lib):
    anna = person_id(conn, "Anna")
    assert ids(conn, PhotoFilter(person_id=anna, date_from="2020-08-01")) == {lib["b"]}
    assert ids(conn, PhotoFilter(person_id=anna, text="trips")) == {lib["a"]}
    assert ids(conn, PhotoFilter(person_id=anna, text="home")) == {lib["b"]}


def test_count_months_and_grid_agree(conn, lib):
    f = PhotoFilter(person_id=person_id(conn, "Bo_b"))
    rows = db.filtered_grid_photos(conn, f)
    assert count(conn, f) == len(rows) == 2
    assert db.filtered_month_counts(conn, f) == [("2021-01", 1), ("2020-08", 1)]


def test_text_matches_person_names_and_captions(conn, lib):
    assert ids(conn, PhotoFilter(text="anna")) == {lib["a"], lib["b"]}
    assert ids(conn, PhotoFilter(text="snow")) == {lib["b"]}
    assert ids(conn, PhotoFilter(text="anna snow")) == {lib["b"]}
    assert ids(conn, PhotoFilter(text="nobody")) == set()


def test_text_wildcards_are_literal(conn, lib):
    assert ids(conn, PhotoFilter(text="%")) == {lib["a"]}  # only "100% fun"
    assert ids(conn, PhotoFilter(text="_")) == {lib["b"], lib["c"]}  # "Bo_b" and "snow_day"
    assert ids(conn, PhotoFilter(text="o_b")) == {lib["b"], lib["c"]}
