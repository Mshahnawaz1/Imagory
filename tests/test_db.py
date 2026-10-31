import sqlite3

import pytest

from imagory import db


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "imagory.db")
    yield c
    c.close()


def _record(path: str, taken: str = "2020-01-01T00:00:00") -> db.FileRecord:
    return db.FileRecord(path, 10, 1_000, 4, 3, "JPEG", taken, "exif")


def test_new_database_is_at_current_version(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"sources", "files", "photos", "scan_runs", "settings"} <= tables


def test_connection_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_migrate_is_idempotent_and_skips_backup_when_current(tmp_path):
    path = tmp_path / "imagory.db"
    db.open_database(path).close()
    db.open_database(path).close()
    assert not (tmp_path / "imagory.db.bak").exists()


def test_backup_before_migrating_existing_db(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    conn = db.open_database(path)
    db.set_setting(conn, "marker", "v1")
    conn.close()

    current = db.SCHEMA_VERSION

    def _migration_next(c):
        c.execute("CREATE TABLE extra (x INTEGER)")

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, _migration_next])
    monkeypatch.setattr(db, "SCHEMA_VERSION", current + 1)
    conn = db.open_database(path)
    assert db.schema_version(conn) == current + 1
    conn.close()

    backup = sqlite3.connect(tmp_path / "imagory.db.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == current
    assert backup.execute("SELECT value FROM settings WHERE key = 'marker'").fetchone()[0] == "v1"
    backup.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    db.open_database(path).close()

    def _bad(c):
        c.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")

    current = db.SCHEMA_VERSION
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, _bad])
    monkeypatch.setattr(db, "SCHEMA_VERSION", current + 1)
    with pytest.raises(RuntimeError):
        db.open_database(path)
    conn = db.connect(path)
    assert db.schema_version(conn) == current
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in names


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "imagory.db"
    conn = db.open_database(path)
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate(conn)


def test_relative_path_is_case_insensitive(conn):
    src = db.create_source(conn, "/photos", "photos")
    db.insert_file(conn, src.id, _record("A/IMG.jpg"), db.utc_now())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_file(conn, src.id, _record("a/img.JPG"), db.utc_now())


def test_insert_creates_photo_and_grid_order(conn):
    src = db.create_source(conn, "/photos", "photos")
    now = db.utc_now()
    db.insert_file(conn, src.id, _record("b.jpg", "2020-01-01T00:00:00"), now)
    db.insert_file(conn, src.id, _record("a.jpg", "2020-01-01T00:00:00"), now)
    newest = db.insert_file(conn, src.id, _record("c.jpg", "2021-05-05T00:00:00"), now)
    grid = db.grid_photos(conn)
    assert [p.relative_path for p in grid] == ["c.jpg", "a.jpg", "b.jpg"]
    db.mark_missing(conn, [newest])
    assert [p.relative_path for p in db.grid_photos(conn)] == ["a.jpg", "b.jpg"]


def test_delete_source_removes_rows(conn):
    src = db.create_source(conn, "/photos", "photos")
    db.insert_file(conn, src.id, _record("a.jpg"), db.utc_now())
    run = db.start_scan_run(conn, src.id, db.utc_now())
    db.finish_scan_run(conn, run, src.id, "completed", db.ScanCounts(found=1, added=1))
    db.delete_source(conn, src.id)
    for table in ("sources", "files", "photos", "scan_runs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_settings_roundtrip(conn):
    assert db.get_setting(conn, "k", "d") == "d"
    db.set_setting(conn, "k", "1")
    db.set_setting(conn, "k", "2")
    assert db.get_setting(conn, "k") == "2"


def test_grid_photos_paging(conn):
    src = db.create_source(conn, "/photos", "photos")
    now = db.utc_now()
    for i in range(5):
        db.insert_file(conn, src.id, _record(f"{i}.jpg", f"2020-01-0{i + 1}T00:00:00"), now)
    full = db.grid_photos(conn)
    assert db.grid_photos(conn, limit=2) + db.grid_photos(conn, offset=2) == full


def test_source_create_list_rename_type_relocate(conn):
    first = db.create_source(conn, "/photos", "Phone")
    second = db.create_source(conn, "/takeout", "Account A", "takeout", type_overridden=True)
    assert (first.type, first.type_overridden, first.last_scan_at) == ("plain", False, None)
    assert first.added_at.endswith("Z")
    assert (second.type, second.type_overridden) == ("takeout", True)
    assert [s.id for s in db.list_sources(conn)] == [first.id, second.id]

    db.rename_source(conn, first.id, "Phone backup")
    db.set_source_type(conn, first.id, "takeout")
    db.relocate_source(conn, first.id, "/mnt/e/photos")
    moved = db.get_source(conn, first.id)
    assert (moved.label, moved.type, moved.type_overridden) == ("Phone backup", "takeout", True)
    assert moved.root_path == "/mnt/e/photos"
    assert db.get_source(conn, 999) is None
    with pytest.raises(ValueError):
        db.set_source_type(conn, first.id, "zip")
    with pytest.raises(ValueError):
        db.create_source(conn, "/x", "x", "zip")


def test_source_stats_and_sample_paths(conn):
    src = db.create_source(conn, "/photos", "photos")
    empty = db.create_source(conn, "/empty", "empty")
    now = db.utc_now()
    ids = [db.insert_file(conn, src.id, _record(f"{i}.jpg"), now) for i in range(3)]
    db.mark_missing(conn, ids[:1])
    stats = db.source_stats(conn)
    assert stats[src.id] == db.SourceStats(photos=2, missing=1)
    assert empty.id not in stats
    assert db.source_file_paths(conn, src.id, 2) == ["0.jpg", "1.jpg"]


def test_grid_spans_sources_with_path_then_source_ties(conn):
    a = db.create_source(conn, "/a", "a")
    b = db.create_source(conn, "/b", "b")
    now = db.utc_now()
    db.insert_file(conn, b.id, _record("x.jpg"), now)
    db.insert_file(conn, a.id, _record("x.jpg"), now)
    db.insert_file(conn, b.id, _record("new.jpg", "2024-01-01T00:00:00"), now)
    grid = db.grid_photos(conn)
    assert [(p.relative_path, p.source_id) for p in grid] == [
        ("new.jpg", b.id),
        ("x.jpg", a.id),
        ("x.jpg", b.id),
    ]


def test_delete_source_keeps_other_sources(conn):
    a = db.create_source(conn, "/a", "a")
    b = db.create_source(conn, "/b", "b")
    now = db.utc_now()
    db.insert_file(conn, a.id, _record("x.jpg"), now)
    db.insert_file(conn, b.id, _record("x.jpg"), now)
    db.delete_source(conn, a.id)
    assert [(p.source_id, p.relative_path) for p in db.grid_photos(conn)] == [(b.id, "x.jpg")]
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
