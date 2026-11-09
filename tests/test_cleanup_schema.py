"""The Takeout cleanup tables and the ``# --- cleanup ---`` SQL in ``db.py``."""

from __future__ import annotations

import sqlite3

import pytest

from imagory import db
from imagory.cleanup.types import ExportAlbum, ExportOrigin, ExportRun
from tests.db_helpers import album_ids_of

NEW_TABLES = ("cleanup_runs", "file_origins", "extra_files", "cleanup_copies")
NEW_INDEXES = (
    "idx_file_origins_file_id",
    "idx_file_origins_extra_file_id",
    "idx_extra_files_content_hash",
    "idx_cleanup_copies_run_status",
)


def _record(path, content_hash, taken="2020-01-01T00:00:00", source="exif", mtime=1_000):
    return db.FileRecord(path, 10, mtime, 4, 3, "JPEG", taken, source, content_hash)


def test_user_cannot_choose_cleaned_type(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    src = db.create_source(conn, "/p", "p")
    assert "cleaned" not in db.SOURCE_TYPES and "cleaned" in db.ALL_SOURCE_TYPES
    with pytest.raises(ValueError):
        db.set_source_type(conn, src.id, "cleaned")
    conn.close()


def test_new_database_has_cleanup_tables_and_cleaned_type(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert set(NEW_TABLES) <= names
    assert set(NEW_INDEXES) <= names
    cleaned = db.create_source(conn, "/out", "Clean", "cleaned")
    assert db.get_source(conn, cleaned.id).type == "cleaned"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sources (label, root_path, type, added_at) VALUES ('x', '/x', 'zip', 'n')"
        )
    conn.close()


# --- cleanup SQL ------------------------------------------------------------------------


@pytest.fixture
def out(tmp_path):
    """An output database with a temporary input source and the cleaned source."""
    conn = db.open_database(tmp_path / "out.db")
    temp = db.create_source(conn, "/in/Takeout", "Takeout", "takeout")
    cleaned = db.create_source(conn, "/out", "Clean", "cleaned")
    yield conn, temp.id, cleaned.id
    conn.close()


def _file(conn, source_id, path, content_hash, taken="2019-07-04T12:00:00", source="takeout_json"):
    return db.insert_file(conn, source_id, _record(path, content_hash, taken, source), db.utc_now())


def _photo_of(conn, file_id):
    return conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]


def _copy_id(conn, run_id, target):
    return next(c.id for c in db.list_copies(conn, run_id) if c.target_relative_path == target)


def test_runs_create_resume_status_and_counts(out):
    conn, _, _ = out
    first = db.create_run(conn, "D:/Takeout", "Takeout", "2026-11-07T10:00:00Z")
    run = db.get_run(conn, first)
    assert (run.status, run.input_label, run.started_at, run.finished_at) == (
        "analysing",
        "Takeout",
        "2026-11-07T10:00:00Z",
        None,
    )
    assert run.counts == dict.fromkeys(db.RUN_COUNT_FIELDS, 0)

    db.set_run_status(conn, first, "cancelled")
    assert db.get_run(conn, first).status == "cancelled"
    with pytest.raises(ValueError):
        db.set_run_status(conn, first, "done")  # only finalise_run finishes a run
    with pytest.raises(ValueError):
        db.set_run_status(conn, first, "bogus")

    db.set_run_counts(conn, first, {"photos": 3, "bytes_copied": 10})
    assert db.get_run(conn, first).counts["photos"] == 3
    with pytest.raises(ValueError):
        db.set_run_counts(conn, first, {"photos; DROP TABLE files": 1})
    second = db.create_run(conn, "D:/Takeout", "Takeout")
    assert [r.id for r in db.list_runs(conn)] == [first, second]
    assert db.get_run(conn, 999) is None


def test_extra_files_upsert_keeps_ids_and_derives_extension(out):
    conn, temp, _ = out
    rec = db.ExtraFileRecord("Trip/clip.MP4", 5, 7, "hv", people=("Ana",), favorite=True)
    ids = db.upsert_extra_files(conn, temp, [rec, db.ExtraFileRecord("README", 1, 1, "hr")])
    again = db.upsert_extra_files(
        conn, temp, [db.ExtraFileRecord("trip/clip.mp4", 6, 8, "hv2", ".mp4")]
    )
    assert again == ids[:1]
    rows = db.list_extra_files(conn, temp)
    assert [(e.relative_path, e.extension, e.size, e.content_hash) for e in rows] == [
        ("README", "", 1, "hr"),
        ("trip/clip.mp4", ".mp4", 6, "hv2"),
    ]
    assert db.upsert_extra_files(conn, temp, [rec]) == ids[:1]
    clip = db.list_extra_files(conn, temp)[1]
    assert (clip.extension, clip.people, clip.favorite) == (".mp4", ("Ana",), True)


def test_origins_and_copies_need_exactly_one_target(out):
    conn, temp, _ = out
    run = db.create_run(conn, "/in", "in")
    fid = _file(conn, temp, "a.jpg", "h1")
    with pytest.raises(ValueError):
        db.insert_origins(conn, run, [db.OriginRecord("a.jpg")])
    with pytest.raises(ValueError):
        db.insert_copies(conn, run, [db.CopyRecord("a.jpg", "x.jpg", fid, 1)])
    with pytest.raises(sqlite3.IntegrityError):  # the table checks it too
        conn.execute(
            "INSERT INTO file_origins (run_id, original_relative_path) VALUES (?, 'a.jpg')", (run,)
        )
    assert db.insert_origins(conn, run, [db.OriginRecord("a.jpg", fid, album_folder="Trip")]) == 1
    (origin,) = db.list_origins(conn, run)
    assert origin == db.FileOrigin(origin.id, run, fid, None, "a.jpg", None, "Trip")


def test_copies_lifecycle_and_unique_targets(out):
    conn, temp, _ = out
    run = db.create_run(conn, "/in", "in")
    a = _file(conn, temp, "a.jpg", "h1")
    b = _file(conn, temp, "b.jpg", "h2")
    db.insert_copies(
        conn,
        run,
        [db.CopyRecord("a.jpg", "2019/07/a.jpg", a), db.CopyRecord("b.jpg", "2019/07/b.jpg", b)],
    )
    other = db.create_run(conn, "/in2", "in2")
    with pytest.raises(sqlite3.IntegrityError):  # case-insensitive, across runs
        db.insert_copies(conn, other, [db.CopyRecord("x.jpg", "2019/07/A.JPG", a)])
    assert sorted(db.copy_targets(conn)) == ["2019/07/a.jpg", "2019/07/b.jpg"]

    first, second = (c.id for c in db.list_copies(conn, run, "pending"))
    db.mark_copy_done(conn, first)
    db.mark_copy_error(conn, second, "disk full")
    assert db.list_copies(conn, run, "pending") == []
    assert [c.error for c in db.list_copies(conn, run, "error")] == ["disk full"]
    db.reset_copy(conn, second)
    assert [(c.id, c.error) for c in db.list_copies(conn, run, "pending")] == [(second, None)]
    assert [c.id for c in db.list_copies(conn, run, "done")] == [first]


def _scenario(conn, temp):
    """One planned and copied run: a photo with three input copies (year folder, album,
    Archive), an mtime-only photo, a failed copy, a video in two places, an album JSON."""
    run = db.create_run(conn, "/in/Takeout", "Takeout")
    kept = _file(conn, temp, "Photos from 2019/a.jpg", "h1")
    in_album = _file(conn, temp, "Trip/a.jpg", "h1")
    archived = _file(conn, temp, "Archive/a(1).jpg", "h1", "2026-01-01T00:00:00", "mtime")
    db.set_photo_takeout_metadata(conn, in_album, ["Ana"], "beach", True, "2019-07-05T00:00:00Z")
    undated = _file(conn, temp, "Photos from 2020/b.jpg", "h2", "2026-11-07T09:00:00", "mtime")
    failed = _file(conn, temp, "Photos from 2019/c.jpg", "h3")
    video, video_dup, album_json = db.upsert_extra_files(
        conn,
        temp,
        [
            db.ExtraFileRecord(
                "Photos from 2019/v.mp4",
                50,
                1,
                "hv",
                taken_at_local="2019-07-04T13:00:00",
                date_source="takeout_json",
            ),
            db.ExtraFileRecord("Trip/v.mp4", 50, 1, "hv", people=("Ben",), favorite=True),
            db.ExtraFileRecord("Trip/metadata.json", 9, 1, "hj"),
        ],
    )
    sidecar = "Photos from 2019/a.jpg.json"
    origins = [
        db.OriginRecord("Photos from 2019/a.jpg", kept, sidecar_relative_path=sidecar),
        db.OriginRecord("Trip/a.jpg", kept, None, "Trip/a.jpg.json", "Trip"),
        db.OriginRecord("Archive/a(1).jpg", kept, album_folder="Archive"),
        db.OriginRecord("Photos from 2020/b.jpg", undated),
        db.OriginRecord("Photos from 2019/c.jpg", failed, album_folder="Trip"),
        db.OriginRecord("Photos from 2019/v.mp4", extra_file_id=video),
        db.OriginRecord("Trip/v.mp4", extra_file_id=video, album_folder="Trip"),
        db.OriginRecord("Trip/metadata.json", extra_file_id=album_json),
    ]
    json_target = ".imagory/takeout-json/Trip/metadata.json"
    copies = [
        db.CopyRecord("Photos from 2019/a.jpg", "2019/07/a.jpg", kept),
        db.CopyRecord("Photos from 2020/b.jpg", "Unknown date/b.jpg", undated),
        db.CopyRecord("Photos from 2019/c.jpg", "2019/07/c.jpg", failed),
        db.CopyRecord("Photos from 2019/v.mp4", "2019/07/v.mp4", extra_file_id=video),
        db.CopyRecord("Trip/metadata.json", json_target, extra_file_id=album_json),
    ]
    with db.transaction(conn):
        db.insert_origins(conn, run, origins)
        db.insert_copies(conn, run, copies)
        db.set_run_status(conn, run, "planned")
    copied = {}
    for c in db.list_copies(conn, run):
        if c.target_relative_path == "2019/07/c.jpg":
            db.mark_copy_error(conn, c.id, "source changed")
        else:
            db.mark_copy_done(conn, c.id)
            copied[c.id] = (100 + c.id, 5_000 + c.id)
    ids = {
        "kept": kept,
        "archived": archived,
        "undated": undated,
        "failed": failed,
        "video": video,
        "video_dup": video_dup,
        "album_json": album_json,
    }
    return run, ids, copied


def test_finalise_moves_kept_rows_and_keeps_every_origin(out):
    conn, temp, cleaned = out
    run, ids, copied = _scenario(conn, temp)
    photo_a = _photo_of(conn, ids["kept"])
    failed_photo = _photo_of(conn, ids["failed"])

    result = db.finalise_run(conn, run, temp, cleaned, copied, {"photos": 2, "duplicates": 2})
    assert result == db.FinaliseResult(
        files_moved=2,
        extra_files_moved=2,
        albums_created=2,
        failed_copies_dropped=1,
        leftover_rows_deleted=4,  # Trip/a.jpg, Archive/a(1).jpg, c.jpg, Trip/v.mp4
    )

    # The hash with three input copies: three origins, all on the surviving row.
    origins = [o for o in db.list_origins(conn, run) if o.file_id == ids["kept"]]
    assert sorted(o.original_relative_path for o in origins) == [
        "Archive/a(1).jpg",
        "Photos from 2019/a.jpg",
        "Trip/a.jpg",
    ]
    copy_a = _copy_id(conn, run, "2019/07/a.jpg")
    row = conn.execute(
        "SELECT source_id, relative_path, size, modified_time, sidecar_relative_path, "
        "date_source, photo_id FROM files WHERE id = ?",
        (ids["kept"],),
    ).fetchone()
    assert tuple(row) == (cleaned, "2019/07/a.jpg", *copied[copy_a], None, "takeout_json", photo_a)
    assert db.people_for_photo(conn, photo_a) == ["Ana"]
    photo = conn.execute(
        "SELECT primary_file_id, favorite FROM photos WHERE id = ?", (photo_a,)
    ).fetchone()
    assert tuple(photo) == (ids["kept"], 1)

    # The mtime-only file is undated, in its file row and its photo.
    undated = conn.execute(
        "SELECT f.taken_at_local, f.taken_at_utc, f.date_source, p.taken_at_local, "
        "p.taken_at_utc, p.date_source FROM files f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.id = ?",
        (ids["undated"],),
    ).fetchone()
    assert tuple(undated) == (None,) * 6

    # The failed copy is not in the output: row, photo, origins and copy are gone.
    assert conn.execute("SELECT 1 FROM files WHERE id = ?", (ids["failed"],)).fetchone() is None
    assert conn.execute("SELECT 1 FROM photos WHERE id = ?", (failed_photo,)).fetchone() is None
    assert all(o.file_id != ids["failed"] for o in db.list_origins(conn))
    assert db.list_copies(conn, run, "error") == []

    # The temporary source is gone entirely, and nothing dangles.
    assert db.get_source(conn, temp) is None
    for table in ("files", "extra_files", "scan_runs"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", (temp,))
        assert count.fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Extra files moved, the video merged with its duplicate; the JSON copy kept as is.
    extras = {e.id: e for e in db.list_extra_files(conn, cleaned)}
    assert set(extras) == {ids["video"], ids["album_json"]}
    video = extras[ids["video"]]
    assert (video.relative_path, video.people, video.favorite, video.date_source) == (
        "2019/07/v.mp4",
        ("Ben",),
        True,
        "takeout_json",
    )
    assert (video.size, video.modified_time) == copied[_copy_id(conn, run, "2019/07/v.mp4")]

    run_row = db.get_run(conn, run)
    assert run_row.status == "done" and run_row.finished_at.endswith("Z")
    assert (run_row.counts["photos"], run_row.counts["duplicates"]) == (2, 2)
    with pytest.raises(ValueError, match="already"):
        db.finalise_run(conn, run, temp, cleaned, copied)


def test_finalise_creates_albums_from_origins(out):
    conn, temp, cleaned = out
    run, ids, copied = _scenario(conn, temp)
    existing = db.create_album(conn, "trip")  # reused, ignoring case
    result = db.finalise_run(conn, run, temp, cleaned, copied)
    assert result.albums_created == 1
    albums = {a.name.casefold(): a for a in db.list_albums(conn)}
    assert set(albums) == {"trip", "archive"}
    assert albums["trip"].id == existing
    photo_a = _photo_of(conn, ids["kept"])
    assert album_ids_of(conn, photo_a) == sorted(a.id for a in albums.values())
    assert albums["trip"].photo_count == 1  # the failed c.jpg is not a member

    assert db.export_albums(conn, cleaned) == [
        ExportAlbum("Archive", ("2019/07/a.jpg",)),
        ExportAlbum("Trip", ("2019/07/a.jpg", "2019/07/v.mp4")),
    ]
    assert db.export_albums(conn, cleaned, run_id=run) == db.export_albums(conn, cleaned)
    assert db.export_albums(conn, cleaned, run_id=run + 1) == []


def test_export_files_and_runs(out):
    conn, temp, cleaned = out
    run, ids, copied = _scenario(conn, temp)
    db.finalise_run(conn, run, temp, cleaned, copied, {"photos": 2})
    files = db.export_files(conn, cleaned)
    assert [(f.path, f.kind) for f in files] == [
        ("2019/07/a.jpg", "photo"),
        ("2019/07/v.mp4", "other"),
        ("Unknown date/b.jpg", "photo"),
    ]
    a, v, b = files
    copy_a = _copy_id(conn, run, "2019/07/a.jpg")
    assert (a.content_hash, a.width, a.height, a.format) == ("h1", 4, 3, "JPEG")
    assert a.size == copied[copy_a][0]
    assert (a.taken_at_local, a.date_source, a.people, a.description, a.favorite) == (
        "2019-07-04T12:00:00",
        "takeout_json",
        ("Ana",),
        "beach",
        True,
    )
    assert a.uploaded_at_utc == "2019-07-05T00:00:00Z"
    assert a.origins == (
        ExportOrigin(run, "Archive/a(1).jpg", None, "Archive"),
        ExportOrigin(run, "Photos from 2019/a.jpg", "Photos from 2019/a.jpg.json", None),
        ExportOrigin(run, "Trip/a.jpg", "Trip/a.jpg.json", "Trip"),
    )
    assert (b.taken_at_local, b.taken_at_utc, b.date_source) == (None, None, None)
    assert (v.width, v.format, v.content_hash, v.people) == (None, None, "hv", ("Ben",))
    assert [o.path for o in v.origins] == ["Photos from 2019/v.mp4", "Trip/v.mp4"]

    (exported,) = db.export_runs(conn)
    finished = db.get_run(conn, run)
    assert exported == ExportRun(
        run, "/in/Takeout", finished.started_at, finished.finished_at, finished.counts
    )
    assert exported.counts["photos"] == 2
    db.create_run(conn, "/in/Other", "Other")  # unfinished runs are not exported
    assert len(db.export_runs(conn)) == 1


def test_finalise_refuses_pending_or_unmeasured_copies(out):
    conn, temp, cleaned = out
    run, ids, copied = _scenario(conn, temp)
    target = _copy_id(conn, run, "2019/07/a.jpg")
    db.reset_copy(conn, target)
    with pytest.raises(ValueError, match="pending"):
        db.finalise_run(conn, run, temp, cleaned, copied)
    db.mark_copy_done(conn, target)
    partial = {k: v for k, v in copied.items() if k != target}
    with pytest.raises(ValueError, match="size and mtime"):
        db.finalise_run(conn, run, temp, cleaned, partial)
    with pytest.raises(ValueError):
        db.finalise_run(conn, run, cleaned, cleaned, copied)
    with pytest.raises(ValueError):
        db.finalise_run(conn, run, temp, cleaned, copied, {"bogus": 1})
    assert db.get_source(conn, temp) is not None
    assert db.get_run(conn, run).status == "planned"


def test_finalise_rolls_back_if_an_origin_points_at_a_dropped_row(out):
    conn, temp, cleaned = out
    run, ids, copied = _scenario(conn, temp)
    # A caller bug: an origin on a duplicate row instead of the kept one.
    db.insert_origins(conn, run, [db.OriginRecord("Archive/a(1).jpg", ids["archived"])])
    before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.finalise_run(conn, run, temp, cleaned, copied)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == before
    assert db.get_source(conn, temp) is not None
    assert db.get_run(conn, run).status == "planned"


def test_kept_file_takes_the_best_date_of_its_duplicates(out):
    conn, temp, cleaned = out
    run = db.create_run(conn, "/in", "in")
    kept = _file(conn, temp, "Photos from 2019/a.jpg", "h1", "2019-07-04T14:00:00", "exif")
    dup = _file(conn, temp, "Trip/a.jpg", "h1", "2019-07-04T12:00:00", "takeout_json")
    db.insert_origins(
        conn,
        run,
        [db.OriginRecord("Photos from 2019/a.jpg", kept), db.OriginRecord("Trip/a.jpg", kept)],
    )
    db.insert_copies(conn, run, [db.CopyRecord("Photos from 2019/a.jpg", "2019/07/a.jpg", kept)])
    (copy,) = db.list_copies(conn, run, "pending")
    db.mark_copy_done(conn, copy.id)
    db.finalise_run(conn, run, temp, cleaned, {copy.id: (10, 20)})
    row = conn.execute("SELECT taken_at_local, date_source FROM files WHERE id = ?", (kept,))
    assert tuple(row.fetchone()) == ("2019-07-04T12:00:00", "takeout_json")
    assert conn.execute("SELECT 1 FROM files WHERE id = ?", (dup,)).fetchone() is None
    assert len(db.list_origins(conn, run)) == 2


def test_second_run_merges_into_existing_output(out):
    conn, temp, cleaned = out
    run1, ids, copied = _scenario(conn, temp)
    db.finalise_run(conn, run1, temp, cleaned, copied)
    photo_a = _photo_of(conn, ids["kept"])

    temp2 = db.create_source(conn, "/in/Takeout2", "Takeout 2", "takeout").id
    run2 = db.create_run(conn, "/in/Takeout2", "Takeout 2")
    same = _file(conn, temp2, "Photos from 2019/a.jpg", "h1")  # already in the output
    new = _file(conn, temp2, "Photos from 2021/d.jpg", "h4", "2021-01-01T00:00:00")
    assert _photo_of(conn, same) == photo_a
    db.insert_origins(
        conn,
        run2,
        [
            db.OriginRecord("Photos from 2019/a.jpg", ids["kept"], album_folder="Trip"),
            db.OriginRecord("Photos from 2021/d.jpg", new, album_folder="Trip"),
        ],
    )
    db.insert_copies(conn, run2, [db.CopyRecord("Photos from 2021/d.jpg", "2021/01/d.jpg", new)])
    (copy,) = db.list_copies(conn, run2, "pending")
    db.mark_copy_done(conn, copy.id)
    result = db.finalise_run(conn, run2, temp2, cleaned, {copy.id: (1, 2)})
    assert (result.files_moved, result.albums_created, result.leftover_rows_deleted) == (1, 0, 1)

    kept_origins = [o for o in db.list_origins(conn) if o.file_id == ids["kept"]]
    assert [(o.run_id, o.original_relative_path) for o in kept_origins] == [
        (run1, "Archive/a(1).jpg"),
        (run1, "Photos from 2019/a.jpg"),
        (run1, "Trip/a.jpg"),
        (run2, "Photos from 2019/a.jpg"),
    ]
    primary = conn.execute("SELECT primary_file_id FROM photos WHERE id = ?", (photo_a,))
    assert primary.fetchone()[0] == ids["kept"]
    assert [a.name for a in db.list_albums(conn)] == ["Archive", "Trip"]
    trip = next(a for a in db.export_albums(conn, cleaned) if a.name == "Trip")
    assert trip.files == ("2019/07/a.jpg", "2019/07/v.mp4", "2021/01/d.jpg")
    assert [r.id for r in db.export_runs(conn)] == [run1, run2]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
