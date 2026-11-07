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
    assert sources.recompute_dates(conn, source) == 0  # still resolves through the database
    summary = scan(conn, root, source)
    assert (summary.counts.unchanged, summary.counts.missing) == (3, 0)
    # Dates are read again from the moved database; the undated file stays undated.
    conn.execute("UPDATE files SET taken_at_local = 'x', date_source = 'mtime'")
    assert sources.recompute_dates(conn, source) == 3
    assert file_row(conn, source.id, "Unknown date/scan.jpg")[2] is None
    assert file_row(conn, source.id, "2019/07/IMG_1.jpg")[4] == "takeout_json"
    assert_unchanged(moved, before)
    no_side_files(moved)


def test_recompute_dates_reads_the_output_database(output, lib):
    root, conn = lib
    make_image(output / "Later" / "exif.jpg", color=(50, 50, 50), taken="2021:05:05 05:05:05")
    source = sources.add_cleaned_source(conn, output)
    scan(conn, root, source)
    conn.execute(
        "UPDATE files SET taken_at_local = '1999-01-01T00:00:00', taken_at_utc = NULL, "
        "date_source = 'mtime'"
    )
    before = snapshot(output)
    assert sources.recompute_dates(conn, db.get_source(conn, source.id)) == 4
    assert_unchanged(output, before)
    assert tuple(file_row(conn, source.id, "2019/07/IMG_1.jpg")[2:5]) == (
        LOCAL,
        UTC_ISO,
        "takeout_json",
    )
    assert tuple(file_row(conn, source.id, "Unknown date/scan.jpg")[2:7]) == (None,) * 5
    assert file_row(conn, source.id, "Later/exif.jpg")[2:5] == (
        "2021-05-05T05:05:05",
        None,
        "exif",
    )

    shutil.rmtree(paths.cleanup_dir(output))
    with pytest.raises(FileNotFoundError):
        sources.recompute_dates(conn, db.get_source(conn, source.id))


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
    build_run(
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
    output_before = snapshot(out)
    cleaned = sources.add_cleaned_source(conn, out)
    with pytest.raises(ValueError):  # not scanned yet
        sources.swap_source(conn, paths.thumbs_dir(root), old.id, cleaned.id)
    assert not sources.swap_ready(conn, cleaned.id)
    scan(conn, root, cleaned)
    assert sources.swap_ready(conn, cleaned.id)
    assert sources.swap_orphan_count(conn, old.id) == 0  # Trash was never imported
    with pytest.raises(ValueError):
        sources.swap_source(conn, paths.thumbs_dir(root), cleaned.id, cleaned.id)

    sources.swap_source(conn, paths.thumbs_dir(root), old.id, cleaned.id)
    assert db.get_source(conn, old.id) is None
    for name, before_row in ids.items():
        after_row = file_row(conn, cleaned.id, f"2019/07/{name}")
        assert after_row[1] == before_row[1]  # same photo id
        assert tuple(after_row[5:7]) == tuple(before_row[5:7])  # same photo date
        assert db.albums_for_photo(conn, after_row[1]) == [favourites]
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
    run_id = db.start_scan_run(conn, cleaned.id, db.utc_now())
    db.finish_scan_run(conn, run_id, cleaned.id, "cancelled", db.ScanCounts())
    assert not sources.swap_ready(conn, cleaned.id)
    with pytest.raises(ValueError):
        sources.swap_source(conn, paths.thumbs_dir(root), old.id, cleaned.id)
    assert db.get_source(conn, old.id) is not None


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
