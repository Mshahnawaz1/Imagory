"""A cleanup output added to the main library as a ``cleaned`` source (PRD §8.10).

Output databases are built here with the cleanup SQL (a run, a temporary source, files,
origins, copies, ``finalise_run``) and code-generated images. The output folder is a user
folder for the main library: every test asserts it, ``.imagory/imagory.db`` included, is
unchanged by scans, and that no ``-wal``/``-shm`` file appears next to that database.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imagory import db, paths, sources, thumbs
from imagory.scanner import Scanner
from tests.db_helpers import album_ids_of
from tests.fs_snapshot import assert_unchanged, snapshot
from tests.imaging import make_image

TS = datetime(2019, 7, 4, 10, 0, 0, tzinfo=UTC)
LOCAL = "2019-07-04T12:00:00"
UTC_ISO = "2019-07-04T10:00:00Z"


@dataclass
class Item:
    """One kept output file and its input copies."""

    target: str  # output-relative
    inputs: tuple[str, ...]  # input-relative copies; the first is the kept one
    color: tuple[int, int, int] = (10, 20, 30)
    exif: str | None = None  # EXIF DateTimeOriginal written into the image
    taken: str | None = LOCAL  # date stored in the output (None: undated)
    taken_utc: str | None = UTC_ISO
    date_source: str | None = "takeout_json"
    people: tuple[str, ...] = ()
    description: str | None = None
    favorite: bool = False
    uploaded: str | None = None
    location: tuple[float, float] | None = None
    albums: dict[str, str] = field(default_factory=dict)  # input path -> album name
    data: bytes | None = None  # exact file contents (e.g. the bytes of an input file)
    video: bool = False  # an "other" file (extra_files), not a photo


def _write(out: Path, item: Item) -> bytes:
    dest = out.joinpath(*item.target.split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if item.data is not None:
        dest.write_bytes(item.data)
    elif item.video:
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(item.color) * 50)
    else:
        make_image(dest, color=item.color, taken=item.exif)
    return dest.read_bytes()


def build_run(out: Path, items: list[Item], *, input_path: str = "/in/Takeout") -> int:
    """Record one finished cleanup run into ``out``'s database, as G's pipeline would.

    Items whose hash is already in the output only get origins (they are not copied).
    Returns the run id. The connection is closed, so no ``-wal`` is left behind.
    """
    db_file = paths.cleanup_db_path(out)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(db_file)
    try:
        cleaned = next((s for s in db.list_sources(conn) if s.type == "cleaned"), None)
        if cleaned is None:
            cleaned = db.create_source(conn, str(out), out.name, "cleaned")
        temp = db.create_source(conn, input_path, "Takeout", "takeout")
        run = db.create_run(conn, input_path, "Takeout")
        now = db.utc_now()
        origins: list[db.OriginRecord] = []
        copies: list[db.CopyRecord] = []
        in_output = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT content_hash, id, 'file' FROM files WHERE source_id = ? UNION ALL "
                "SELECT content_hash, id, 'extra' FROM extra_files WHERE source_id = ?",
                (cleaned.id, cleaned.id),
            )
        }
        for item in items:
            data = _write(out, item) if item.target else item.data
            assert data is not None
            content_hash = thumbs.hash_bytes(data)
            kind_extra = item.video
            if content_hash in in_output:
                kept_id, kind = in_output[content_hash]
                kind_extra = kind == "extra"
                new_copy = False
                if not kind_extra and item.people:
                    db.set_photo_takeout_metadata(conn, kept_id, item.people, None, False, None)
            elif kind_extra:
                (kept_id,) = db.upsert_extra_files(
                    conn,
                    temp.id,
                    [
                        db.ExtraFileRecord(
                            item.inputs[0],
                            len(data),
                            1,
                            content_hash,
                            taken_at_local=item.taken,
                            taken_at_utc=item.taken_utc,
                            date_source=item.date_source,
                            people=item.people,
                        )
                    ],
                )
                new_copy = True
            else:
                record = db.FileRecord(
                    item.inputs[0],
                    len(data),
                    1,
                    64,
                    48,
                    "JPEG",
                    item.taken or "2026-11-07T09:00:00",
                    item.date_source or "mtime",
                    content_hash=content_hash,
                    taken_at_utc=item.taken_utc if item.taken else None,
                )
                kept_id = db.insert_file(conn, temp.id, record, now)
                db.set_photo_takeout_metadata(
                    conn, kept_id, item.people, item.description, item.favorite, item.uploaded
                )
                if item.location:
                    db.set_photo_location(conn, kept_id, *item.location)
                new_copy = True
            target = {"extra_file_id": kept_id} if kind_extra else {"file_id": kept_id}
            for path in item.inputs:
                origins.append(db.OriginRecord(path, album_folder=item.albums.get(path), **target))
            if new_copy:
                copies.append(db.CopyRecord(item.inputs[0], item.target, **target))
        with db.transaction(conn):
            db.insert_origins(conn, run, origins)
            db.insert_copies(conn, run, copies)
            db.set_run_status(conn, run, "planned")
        copied = {}
        for c in db.list_copies(conn, run):
            db.mark_copy_done(conn, c.id)
            st = out.joinpath(*c.target_relative_path.split("/")).stat()
            copied[c.id] = (st.st_size, st.st_mtime_ns)
        db.finalise_run(conn, run, temp.id, cleaned.id, copied)
    finally:
        conn.close()
    return run


def standard_items() -> list[Item]:
    return [
        Item(
            "2019/07/IMG_1.jpg",
            ("Google Photos/Photos from 2019/IMG_1.jpg", "Google Photos/Trip/IMG_1.jpg"),
            color=(1, 2, 3),
            exif="2001:01:01 00:00:00",  # the stored Takeout date must win over EXIF
            people=("Ana", "Ben"),
            description="Beach day",
            favorite=True,
            uploaded="2019-07-05T08:00:00Z",
            location=(48.85, 2.35),
            albums={"Google Photos/Trip/IMG_1.jpg": "Trip"},
        ),
        Item(
            "Unknown date/scan.jpg",
            ("Google Photos/Photos from 2020/scan.jpg", "Google Photos/Archive/scan.jpg"),
            color=(4, 5, 6),
            taken=None,
            taken_utc=None,
            date_source=None,  # stored as mtime, so finalise leaves it undated
            albums={"Google Photos/Archive/scan.jpg": "Archive"},
        ),
        Item(
            "2018/03/IMG_2.jpg",
            ("Google Photos/Photos from 2018/IMG_2.jpg",),
            color=(7, 8, 9),
            exif="2018:03:01 10:00:00",
            taken="2018-03-01T10:00:00",
            taken_utc=None,
            date_source="exif",
            albums={"Google Photos/Photos from 2018/IMG_2.jpg": "Trip"},
        ),
        Item(
            "2019/07/clip.mp4",
            ("Google Photos/Clips/clip.mp4",),
            color=(9, 9, 9),
            video=True,
            albums={"Google Photos/Clips/clip.mp4": "Clips"},
        ),
    ]


@pytest.fixture
def lib(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    yield root, conn
    conn.close()


@pytest.fixture
def output(tmp_path):
    out = tmp_path / "Cleaned"
    build_run(out, standard_items())
    return out


def scan(conn, root, source):
    return Scanner(conn, source, paths.thumbs_dir(root), root, workers=2).run()


def file_row(conn, source_id, rel):
    return conn.execute(
        "SELECT f.id, f.photo_id, f.taken_at_local, f.taken_at_utc, f.date_source, "
        "p.taken_at_local, p.date_source, p.description, p.favorite, p.uploaded_at_utc, "
        "p.latitude, p.longitude FROM files f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.source_id = ? AND f.relative_path = ?",
        (source_id, rel),
    ).fetchone()


def no_side_files(out: Path) -> None:
    db_file = paths.cleanup_db_path(out)
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{db_file}{suffix}").exists(), suffix


# --- scanning ----------------------------------------------------------------------------


def test_walk_skips_the_imagory_folder():
    from imagory.scanner import is_skipped_dir

    assert is_skipped_dir(paths.CLEANUP_DIR_NAME)


def test_scan_carries_metadata_from_the_output_database(output, lib):
    root, conn = lib
    no_side_files(output)
    # Added after the cleanup: one with EXIF, one without any date.
    make_image(output / "Later" / "exif.jpg", color=(50, 50, 50), taken="2021:05:05 05:05:05")
    make_image(output / "Later" / "plain.jpg", color=(60, 60, 60))
    os.utime(output / "Later" / "plain.jpg", ns=(1_600_000_000 * 10**9,) * 2)
    before = snapshot(output)

    source = sources.add_cleaned_source(conn, output, "Clean")
    summary = scan(conn, root, source)
    assert (summary.status, summary.counts.added, summary.counts.errors) == ("completed", 5, 0)
    assert_unchanged(output, before)
    no_side_files(output)

    row = file_row(conn, source.id, "2019/07/IMG_1.jpg")
    assert tuple(row[2:5]) == (LOCAL, UTC_ISO, "takeout_json")  # not the EXIF 2001 date
    assert tuple(row[5:]) == (
        LOCAL,
        "takeout_json",
        "Beach day",
        1,
        "2019-07-05T08:00:00Z",
        48.85,
        2.35,
    )
    assert db.people_for_photo(conn, row[1]) == ["Ana", "Ben"]
    # The EXIF inputs are still stored for later recomputation.
    exif = conn.execute("SELECT exif_taken_at FROM files WHERE id = ?", (row[0],)).fetchone()
    assert exif[0] == "2001-01-01T00:00:00"

    undated = file_row(conn, source.id, "Unknown date/scan.jpg")
    assert tuple(undated[2:7]) == (None,) * 5  # no mtime fallback

    exif_row = file_row(conn, source.id, "2018/03/IMG_2.jpg")
    assert tuple(exif_row[2:5]) == ("2018-03-01T10:00:00", None, "exif")

    # Not in the output database: the plain-folder rules, mtime included.
    assert file_row(conn, source.id, "Later/exif.jpg")[4] == "exif"
    assert file_row(conn, source.id, "Later/plain.jpg")[4] == "mtime"

    # The grid puts the undated photo under "Unknown date".
    assert (None, 1) in db.month_counts(conn)


def test_changed_file_follows_plain_rules(output, lib):
    """Same path but different contents than the output database says: not confirmed."""
    root, conn = lib
    make_image(output / "2019" / "07" / "IMG_1.jpg", color=(200, 0, 0), taken="2015:01:01 00:00:00")
    source = sources.add_cleaned_source(conn, output)
    before = snapshot(output)
    scan(conn, root, source)
    assert_unchanged(output, before)
    row = file_row(conn, source.id, "2019/07/IMG_1.jpg")
    assert tuple(row[2:5]) == ("2015-01-01T00:00:00", None, "exif")
    assert db.people_for_photo(conn, row[1]) == []


def test_rescan_is_incremental_and_output_untouched(output, lib):
    root, conn = lib
    source = sources.add_cleaned_source(conn, output)
    scan(conn, root, source)
    before = snapshot(output)
    again = scan(conn, root, db.get_source(conn, source.id))
    assert (again.counts.added, again.counts.unchanged, again.counts.missing) == (0, 3, 0)
    assert_unchanged(output, before)
    no_side_files(output)


def test_leftover_wal_is_read_from_a_copy(output, lib, tmp_path):
    """A writer that stopped before checkpointing leaves committed rows in ``-wal``.
    ``immutable`` would miss them, so the reader works on a copy outside the folder."""
    root, conn = lib
    crashed = tmp_path / "Crashed"
    shutil.copytree(output, crashed)
    db_file = paths.cleanup_db_path(output)
    writer = db.connect(db_file)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE photos SET description = 'from the wal'")
    shutil.copyfile(db_file, paths.cleanup_db_path(crashed))
    shutil.copyfile(f"{db_file}-wal", f"{paths.cleanup_db_path(crashed)}-wal")
    writer.close()
    assert Path(f"{paths.cleanup_db_path(crashed)}-wal").stat().st_size > 0

    before = snapshot(crashed)
    source = sources.add_cleaned_source(conn, crashed)
    scan(conn, root, source)
    assert_unchanged(crashed, before)
    assert not Path(f"{paths.cleanup_db_path(crashed)}-shm").exists()
    assert file_row(conn, source.id, "2019/07/IMG_1.jpg")[7] == "from the wal"


def test_unreadable_output_database_stops_the_scan(output, lib):
    root, conn = lib
    paths.cleanup_db_path(output).write_bytes(b"not a database" * 100)
    source = sources.add_cleaned_source(conn, output)
    before = snapshot(output)
    with pytest.raises(db.OutputDatabaseError):
        scan(conn, root, source)
    assert_unchanged(output, before)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_missing_output_database_uses_plain_rules(output, lib):
    root, conn = lib
    source = sources.add_cleaned_source(conn, output)
    shutil.rmtree(paths.cleanup_dir(output))
    before = snapshot(output)
    summary = scan(conn, root, source)
    assert summary.status == "completed"
    assert_unchanged(output, before)
    assert file_row(conn, source.id, "Unknown date/scan.jpg")[4] == "mtime"


def test_relocated_output_keeps_its_metadata(output, lib, tmp_path):
    root, conn = lib
    source = sources.add_cleaned_source(conn, output)
    scan(conn, root, source)
    moved = tmp_path / "Elsewhere" / "Renamed"
    moved.parent.mkdir()
    shutil.move(output, moved)
    db.relocate_source(conn, source.id, str(moved))
    before = snapshot(moved)

    source = db.get_source(conn, source.id)
    summary = scan(conn, root, source)
    assert (summary.counts.unchanged, summary.counts.missing) == (3, 0)
    assert file_row(conn, source.id, "2019/07/IMG_1.jpg")[4] == "takeout_json"
    assert file_row(conn, source.id, "Unknown date/scan.jpg")[2] is None
    assert_unchanged(moved, before)
    no_side_files(moved)


def test_type_of_cleaned_source_cannot_change(output, lib):
    _, conn = lib
    source = sources.add_cleaned_source(conn, output)
    with pytest.raises(ValueError):
        sources.change_source_type(conn, source.id, "plain")
    with pytest.raises(ValueError):
        sources.change_source_type(conn, source.id, "cleaned")
    assert db.get_source(conn, source.id).type == "cleaned"


# --- adding ------------------------------------------------------------------------------


def test_detect_and_add_cleaned_source(output, lib, tmp_path):
    _, conn = lib
    before = snapshot(output)
    assert sources.is_cleanup_output(output)
    assert sources.detect_source_type(output).type == "cleaned"
    plain = tmp_path / "plain"
    make_image(plain / "a.jpg")
    assert sources.detect_source_type(plain).type == "plain"

    source = sources.add_cleaned_source(conn, output)
    assert (source.type, source.label, source.type_overridden) == ("cleaned", "Cleaned", False)
    assert sources.add_cleaned_source(conn, output).id == source.id  # a rerun rescans it
    assert sources.source_for_folder(conn, output).id == source.id
    with pytest.raises(ValueError):
        sources.add_cleaned_source(conn, plain)  # not a cleanup output
    db.create_source(conn, str(plain), "Plain", "plain")
    make_image(plain / ".imagory" / "x.jpg")
    db.open_database(paths.cleanup_db_path(plain)).close()
    with pytest.raises(ValueError):
        sources.add_cleaned_source(conn, plain)  # already a plain source
    assert_unchanged(output, before)


def test_second_run_rescans_cleanly(output, lib):
    root, conn = lib
    source = sources.add_cleaned_source(conn, output)
    scan(conn, root, source)
    first_photo = file_row(conn, source.id, "2019/07/IMG_1.jpg")[1]
    img1 = (output / "2019" / "07" / "IMG_1.jpg").read_bytes()
    run2 = build_run(
        output,
        [
            Item(
                "",  # a duplicate of a file already in the output: origin only
                ("Account 2/Photos from 2019/IMG_1.jpg",),
                data=img1,
                albums={"Account 2/Photos from 2019/IMG_1.jpg": "Trip"},
            ),
            Item(
                "2020/01/IMG_3.jpg",
                ("Account 2/Photos from 2020/IMG_3.jpg",),
                color=(100, 1, 1),
                taken="2020-01-01T10:00:00",
                taken_utc="2020-01-01T09:00:00Z",
                people=("Cleo",),
                albums={"Account 2/Photos from 2020/IMG_3.jpg": "Trip"},
            ),
        ],
        input_path="/in/Account 2",
    )
    before = snapshot(output)
    summary = scan(conn, root, db.get_source(conn, source.id))
    assert (summary.counts.added, summary.counts.unchanged, summary.counts.errors) == (1, 3, 0)
    assert (summary.counts.missing, summary.counts.updated) == (0, 0)
    assert_unchanged(output, before)
    no_side_files(output)
    assert file_row(conn, source.id, "2019/07/IMG_1.jpg")[1] == first_photo
    new = file_row(conn, source.id, "2020/01/IMG_3.jpg")
    assert new[4] == "takeout_json" and db.people_for_photo(conn, new[1]) == ["Cleo"]

    imported = sources.import_run_albums(conn, paths.cleanup_db_path(output), None, source.id)
    names = sorted(a.name for a in imported)
    # Both runs imported together: one "Trip" holding the members of both runs.
    assert names == ["Archive", "Trip"]
    trip = next(a for a in imported if a.name == "Trip")
    assert trip.photos == 3
    assert sources.import_run_albums(conn, paths.cleanup_db_path(output), run2, source.id) == []


# --- albums ------------------------------------------------------------------------------


def test_import_run_albums_clash_rule(output, lib):
    root, conn = lib
    source = sources.add_cleaned_source(conn, output)
    output_db = paths.cleanup_db_path(output)
    with pytest.raises(ValueError):  # members come from the scanned file rows
        sources.import_run_albums(conn, output_db, None, source.id)
    scan(conn, root, source)
    cancelled = db.start_scan_run(conn, source.id, db.utc_now())
    db.finish_scan_run(conn, cancelled, source.id, "cancelled", db.ScanCounts())
    settings_before = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()
    with pytest.raises(ValueError, match="did not complete"):
        sources.import_run_albums(conn, output_db, None, source.id)
    assert conn.execute("SELECT * FROM settings ORDER BY key").fetchall() == settings_before
    assert db.list_albums(conn) == []
    scan(conn, root, db.get_source(conn, source.id))

    # Existing albums, one already using the first suffix; neither may change.
    own = conn.execute("SELECT id FROM photos ORDER BY id LIMIT 1").fetchone()[0]
    trip = db.create_album(conn, "trip")
    trip_takeout = db.create_album(conn, "Trip (Takeout)")
    db.add_photos_to_album(conn, trip, [own])
    before_albums = {a.id: (a.name, a.photo_count) for a in db.list_albums(conn)}
    before = snapshot(output)

    with db.open_output_database(output_db) as out:
        (run_id,) = [r.id for r in db.list_runs(out)]
    imported = sources.import_run_albums(conn, output_db, run_id, source.id)
    assert_unchanged(output, before)
    no_side_files(output)
    by_output = {a.output_name: a for a in imported}
    # "Clips" holds only a video, which is not in the grid: not created.
    assert set(by_output) == {"Trip", "Archive"}
    assert by_output["Trip"].name == "Trip (Takeout 2)"
    assert by_output["Archive"].name == "Archive"
    trip_photos = {
        r[0]
        for r in conn.execute(
            "SELECT photo_id FROM album_photos WHERE album_id = ?", (by_output["Trip"].album_id,)
        )
    }
    expected = {
        file_row(conn, source.id, "2019/07/IMG_1.jpg")[1],
        file_row(conn, source.id, "2018/03/IMG_2.jpg")[1],
    }
    assert trip_photos == expected
    after = {a.id: (a.name, a.photo_count) for a in db.list_albums(conn)}
    for album_id, value in before_albums.items():
        assert after[album_id] == value  # existing albums unchanged
    assert trip_takeout in after

    # Once per run: a second call creates nothing.
    assert sources.import_run_albums(conn, output_db, run_id, source.id) == []
    assert sources.import_run_albums(conn, output_db, None, source.id) == []


def test_takeout_album_name():
    assert sources.takeout_album_name("Trip", set()) == "Trip"
    assert sources.takeout_album_name("Trip", {"trip"}) == "Trip (Takeout)"
    taken = {"trip", "trip (takeout)", "trip (takeout 2)"}
    assert sources.takeout_album_name("Trip", taken) == "Trip (Takeout 3)"
    assert sources.takeout_album_name("Straße", {"strasse"}) == "Straße (Takeout)"  # casefold
    assert sources.takeout_album_name("ÉTÉ", {"été"}) == "ÉTÉ (Takeout)"


# --- swap --------------------------------------------------------------------------------


def write_sidecar(path: Path, **extra) -> None:
    data = {
        "title": path.name.removesuffix(".json"),
        "photoTakenTime": {"timestamp": str(int(TS.timestamp()))},
        **extra,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def test_swap_keeps_photo_ids_dates_people_and_albums(lib, tmp_path):
    root, conn = lib
    takeout_dir = tmp_path / "Takeout"
    year = takeout_dir / "Google Photos" / "Photos from 2019"
    make_image(year / "IMG_1.jpg", color=(1, 2, 3))
    write_sidecar(year / "IMG_1.jpg.json", people=[{"name": "Ana"}], description="Beach")
    make_image(year / "IMG_2.jpg", color=(4, 5, 6))
    write_sidecar(year / "IMG_2.jpg.json")
    trash = takeout_dir / "Google Photos" / "Trash"
    make_image(trash / "gone.jpg", color=(7, 7, 7))
    input_before = snapshot(takeout_dir)

    old = db.create_source(conn, str(takeout_dir), "Takeout", "takeout")
    scan(conn, root, old)
    ids = {
        rel.rsplit("/", 1)[-1]: file_row(conn, old.id, rel)
        for rel in (
            "Google Photos/Photos from 2019/IMG_1.jpg",
            "Google Photos/Photos from 2019/IMG_2.jpg",
        )
    }
    favourites = db.create_album(conn, "Favourites")
    db.add_photos_to_album(conn, favourites, [ids["IMG_1.jpg"][1], ids["IMG_2.jpg"][1]])

    out = tmp_path / "Cleaned"
    out_db = paths.cleanup_db_path(out)
    run1 = build_run(
        out,
        [
            Item(
                "2019/07/IMG_1.jpg",
                ("Google Photos/Photos from 2019/IMG_1.jpg",),
                data=(year / "IMG_1.jpg").read_bytes(),
                taken=ids["IMG_1.jpg"][5],
                taken_utc=UTC_ISO,
                people=("Ana",),
                description="Beach",
            ),
            Item(
                "2019/07/IMG_2.jpg",
                ("Google Photos/Photos from 2019/IMG_2.jpg",),
                data=(year / "IMG_2.jpg").read_bytes(),
                taken=ids["IMG_2.jpg"][5],
                taken_utc=UTC_ISO,
            ),
        ],
        input_path=str(takeout_dir),
    )
    cleaned = sources.add_cleaned_source(conn, out)
    thumbs_root = paths.thumbs_dir(root)
    with pytest.raises(ValueError):  # not scanned yet
        sources.swap_source(conn, thumbs_root, old.id, cleaned.id, out_db, run1)
    assert not sources.swap_ready(conn, cleaned.id, out_db, run1)
    scan(conn, root, cleaned)
    assert sources.swap_ready(conn, cleaned.id, out_db, run1)
    assert not sources.swap_ready(conn, cleaned.id, out_db, run1 + 5)  # no such run

    # A later run finalised after that scan: the swap waits for a rescan.
    make_image(year / "IMG_3.jpg", color=(8, 8, 8))
    run2 = build_run(
        out,
        [
            Item(
                "2019/07/IMG_3.jpg",
                ("Google Photos/Photos from 2019/IMG_3.jpg",),
                data=(year / "IMG_3.jpg").read_bytes(),
            )
        ],
        input_path=str(takeout_dir),
    )
    input_before = snapshot(takeout_dir)
    scan(conn, root, db.get_source(conn, old.id))
    assert not sources.swap_ready(conn, cleaned.id, out_db, run2)
    with pytest.raises(ValueError):
        sources.swap_source(conn, thumbs_root, old.id, cleaned.id, out_db, run2)
    assert db.get_source(conn, old.id) is not None
    output_before = snapshot(out)
    scan(conn, root, db.get_source(conn, cleaned.id))
    assert sources.swap_ready(conn, cleaned.id, out_db, run2)
    assert sources.swap_orphan_count(conn, old.id) == 0  # Trash was never imported
    with pytest.raises(ValueError):
        sources.swap_source(conn, thumbs_root, cleaned.id, cleaned.id, out_db, run2)

    sources.swap_source(conn, thumbs_root, old.id, cleaned.id, out_db, run2)
    assert db.get_source(conn, old.id) is None
    for name, before_row in ids.items():
        after_row = file_row(conn, cleaned.id, f"2019/07/{name}")
        assert after_row[1] == before_row[1]  # same photo id
        assert tuple(after_row[5:7]) == tuple(before_row[5:7])  # same photo date
        assert album_ids_of(conn, after_row[1]) == [favourites]
    assert db.people_for_photo(conn, ids["IMG_1.jpg"][1]) == ["Ana"]
    assert file_row(conn, cleaned.id, "2019/07/IMG_1.jpg")[7] == "Beach"
    assert_unchanged(takeout_dir, input_before)
    assert_unchanged(out, output_before)
    no_side_files(out)


def test_swap_refuses_a_cancelled_scan(output, lib, tmp_path):
    root, conn = lib
    old_dir = tmp_path / "old"
    make_image(old_dir / "a.jpg")
    old = db.create_source(conn, str(old_dir), "Old", "takeout")
    cleaned = sources.add_cleaned_source(conn, output)
    out_db = paths.cleanup_db_path(output)
    scan_id = db.start_scan_run(conn, cleaned.id, db.utc_now())
    db.finish_scan_run(conn, scan_id, cleaned.id, "cancelled", db.ScanCounts())
    assert not sources.swap_ready(conn, cleaned.id, out_db, 1)
    with pytest.raises(ValueError):
        sources.swap_source(conn, paths.thumbs_dir(root), old.id, cleaned.id, out_db, 1)
    assert db.get_source(conn, old.id) is not None
    # An unfinished cleanup run is never ready either.
    writer = db.connect(out_db)
    pending = db.create_run(writer, "/in/x", "x")
    writer.close()
    scan(conn, root, cleaned)
    assert sources.swap_ready(conn, cleaned.id, out_db, 1)
    assert not sources.swap_ready(conn, cleaned.id, out_db, pending)


# --- dialogs -----------------------------------------------------------------------------


def test_dialogs_show_cleaned_type_without_change_type(qtbot, output, lib, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from imagory.ui import main_window as mw
    from imagory.ui.sources_dialog import TYPE_NAMES, AddSourceDialog, SourcesDialog

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    root, conn = lib
    before = snapshot(output)

    add = AddSourceDialog(output, sources.detect_source_type(output))
    qtbot.addWidget(add)
    assert add.source_type == "cleaned"
    assert add.type_combo.isHidden()

    window = mw.MainWindow(root, conn)
    qtbot.addWidget(window)
    window.show()
    plain = output.parent / "plain"
    make_image(plain / "a.jpg")
    window.add_source(plain, "Plain")
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    source = window.add_source(output, "Clean")  # detected as cleaned
    assert source.type == "cleaned"
    qtbot.waitUntil(lambda: not window.scanning, timeout=60_000)
    assert_unchanged(output, before)
    assert len(window.photos) == 4

    dialog = SourcesDialog(window)
    qtbot.addWidget(dialog)
    rows = {dialog.table.item(r, 0).text(): r for r in range(dialog.table.rowCount())}
    assert dialog.table.item(rows["Clean"], 1).text() == TYPE_NAMES["cleaned"]
    dialog.table.selectRow(rows["Clean"])
    assert dialog.type_button.isHidden()
    dialog.table.selectRow(rows["Plain"])
    assert not dialog.type_button.isHidden()
    window.close()


def test_album_names_are_normalised_before_the_clash_rule(lib, tmp_path):
    root, conn = lib
    out = tmp_path / "Cleaned"
    items = []
    names = {
        "a": "\u00a0Trip",  # NBSP
        "b": "Trip\t",  # tab
        "c": " \t ",  # whitespace only (spaces alone never leave the output database)
        "d": "Bad",  # made to fail below
        "e": "Fine",
    }
    for i, (key, album) in enumerate(names.items()):
        items.append(
            Item(
                f"2019/07/{key}.jpg",
                (f"Google Photos/{key}/{key}.jpg",),
                color=(i, 99, 99),
                albums={f"Google Photos/{key}/{key}.jpg": album},
            )
        )
    run = build_run(out, items)
    source = sources.add_cleaned_source(conn, out)
    scan(conn, root, source)
    db.create_album(conn, "trip")
    conn.execute(
        "CREATE TEMP TRIGGER no_bad BEFORE INSERT ON albums WHEN NEW.name = 'Bad' "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    imported = sources.import_run_albums(conn, paths.cleanup_db_path(out), run, source.id)
    by_name = {a.name: a for a in imported}
    # NBSP and tab are trimmed, so both are "Trip", one album, clashing with "trip".
    assert set(by_name) == {"Trip (Takeout)", "Untitled", "Fine"}
    assert by_name["Trip (Takeout)"].photos == 2
    assert by_name["Untitled"].photos == 1
    # "Bad" failed alone; the others exist and the run is still recorded as imported.
    assert {a.name for a in db.list_albums(conn)} == {"trip", "Trip (Takeout)", "Untitled", "Fine"}
    assert sources.import_run_albums(conn, paths.cleanup_db_path(out), run, source.id) == []


def test_photo_ids_at_paths_chunks_and_keeps_order(lib):
    _, conn = lib
    source = db.create_source(conn, "/x", "X", "plain")
    now = db.utc_now()
    ids = {}
    for i in range(3):
        rec = db.FileRecord(f"A/p{i}.jpg", 1, 1, 1, 1, "JPEG", LOCAL, "exif", content_hash=f"h{i}")
        file_id = db.insert_file(conn, source.id, rec, now)
        ids[i] = conn.execute("SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    wanted = [f"none/{n}.jpg" for n in range(1200)]
    wanted[700:700] = ["a/P2.JPG", "A/p0.jpg"]  # case-insensitive, across chunks
    wanted.append("A/p0.jpg")  # repeated
    assert db.photo_ids_at_paths(conn, source.id, wanted) == [ids[2], ids[0]]
    assert db.photo_ids_at_paths(conn, source.id, []) == []


def test_sqlite_file_uri():
    from pathlib import PurePosixPath, PureWindowsPath

    uri = db.sqlite_file_uri
    assert uri(PureWindowsPath(r"C:\Users\Ana\My Photos\.imagory\imagory.db")) == (
        "file:///C:/Users/Ana/My%20Photos/.imagory/imagory.db"
    )
    assert uri(PureWindowsPath(r"\\nas\share\Fotos #1\imagory.db"), "mode=ro") == (
        "file:////nas/share/Fotos%20%231/imagory.db?mode=ro"
    )
    assert uri(PureWindowsPath(r"D:\100%\why?\été.db")) == (
        "file:///D:/100%25/why%3F/%C3%A9t%C3%A9.db"
    )
    assert uri(PurePosixPath("/home/ana/a b#?%/x.db"), "mode=ro&immutable=1") == (
        "file:///home/ana/a%20b%23%3F%25/x.db?mode=ro&immutable=1"
    )


def test_output_folder_with_odd_characters_opens(lib, tmp_path):
    root, conn = lib
    out = tmp_path / "Fotos #1 100% why?"
    build_run(out, standard_items())
    before = snapshot(out)
    source = sources.add_cleaned_source(conn, out)
    scan(conn, root, source)
    assert file_row(conn, source.id, "2019/07/IMG_1.jpg")[4] == "takeout_json"
    assert_unchanged(out, before)
    no_side_files(out)


def test_rescan_after_a_later_run_refreshes_unchanged_files(output, lib):
    root, conn = lib
    out_db = paths.cleanup_db_path(output)
    source = sources.add_cleaned_source(conn, output)
    scan(conn, root, source)
    img1 = file_row(conn, source.id, "2019/07/IMG_1.jpg")
    assert db.people_for_photo(conn, img1[1]) == ["Ana", "Ben"]

    # Run 2 knows more about two files already in the output: a new person for IMG_1 and
    # (as a later run's sidecar would give) a real date for the undated scan.
    build_run(
        output,
        [
            Item(
                "",
                ("Account 2/IMG_1.jpg",),
                data=(output / "2019" / "07" / "IMG_1.jpg").read_bytes(),
                people=("Dana",),
            )
        ],
        input_path="/in/Account 2",
    )
    writer = db.connect(out_db)
    writer.execute(
        "UPDATE photos SET taken_at_local = '2020-02-02T02:02:02', date_source = 'takeout_json' "
        "WHERE id = (SELECT photo_id FROM files WHERE relative_path = 'Unknown date/scan.jpg')"
    )
    writer.close()
    no_side_files(output)

    # A cancelled scan changes nothing and leaves the marker as it was.
    cancelled = Scanner(conn, db.get_source(conn, source.id), paths.thumbs_dir(root), root)
    cancelled.cancel()
    assert cancelled.run().status == "cancelled"
    assert db.people_for_photo(conn, img1[1]) == ["Ana", "Ben"]

    before = snapshot(output)
    summary = scan(conn, root, db.get_source(conn, source.id))
    assert (summary.counts.added, summary.counts.updated, summary.counts.unchanged) == (0, 0, 3)
    assert_unchanged(output, before)
    no_side_files(output)
    assert db.people_for_photo(conn, img1[1]) == ["Ana", "Ben", "Dana"]
    undated = file_row(conn, source.id, "Unknown date/scan.jpg")
    assert (undated[2], undated[4], undated[5]) == (
        "2020-02-02T02:02:02",
        "takeout_json",
        "2020-02-02T02:02:02",
    )

    # No new run: a rescan doesn't overwrite what the library holds.
    writer = db.connect(out_db)
    writer.execute(
        "UPDATE photos SET taken_at_local = '2021-01-01T00:00:00' "
        "WHERE id = (SELECT photo_id FROM files WHERE relative_path = 'Unknown date/scan.jpg')"
    )
    writer.close()
    scan(conn, root, db.get_source(conn, source.id))
    assert file_row(conn, source.id, "Unknown date/scan.jpg")[2] == "2020-02-02T02:02:02"
