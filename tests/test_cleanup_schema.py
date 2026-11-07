"""Migration 5 (Takeout cleanup schema) and the ``# --- cleanup ---`` SQL in ``db.py``."""

from __future__ import annotations

import sqlite3

import pytest

from imagory import db

OLD_TABLES = (
    "sources",
    "photos",
    "files",
    "scan_runs",
    "settings",
    "albums",
    "album_photos",
    "people",
    "photo_people",
)
NEW_TABLES = ("cleanup_runs", "file_origins", "extra_files", "cleanup_copies")
NEW_INDEXES = (
    "idx_file_origins_file_id",
    "idx_file_origins_extra_file_id",
    "idx_extra_files_content_hash",
    "idx_cleanup_copies_run_status",
)


def _open_at(path, version, monkeypatch):
    """Open a database migrated only up to ``version``."""
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:version])
        m.setattr(db, "SCHEMA_VERSION", version)
        return db.open_database(path)


def _record(path, content_hash, taken="2020-01-01T00:00:00", source="exif", mtime=1_000):
    return db.FileRecord(path, 10, mtime, 4, 3, "JPEG", taken, source, content_hash)


def _dump(conn):
    dump = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in OLD_TABLES
    }
    assert all(dump[t] for t in OLD_TABLES)  # every table has data to compare
    return dump


def _populate_v4(conn):
    now = db.utc_now()
    plain = db.create_source(conn, "/photos", "Phone")
    takeout = db.create_source(conn, "/takeout", "Account", "takeout", type_overridden=True)
    a = db.insert_file(conn, plain.id, _record("a.jpg", "h1"), now)
    db.insert_file(conn, takeout.id, _record("Photos from 2020/a.jpg", "h1"), now)
    b = db.insert_file(conn, takeout.id, _record("b.jpg", "h2", "2021-02-03T04:05:06"), now)
    run = db.start_scan_run(conn, takeout.id, now)
    db.finish_scan_run(conn, run, takeout.id, "completed", db.ScanCounts(found=2, added=2))
    db.set_photo_takeout_metadata(conn, b, ["Ana", "Ben"], "caption", True, "2021-02-04T00:00:00Z")
    album = db.create_album(conn, "Trip")
    photo_a = conn.execute("SELECT photo_id FROM files WHERE id = ?", (a,)).fetchone()[0]
    photo_b = conn.execute("SELECT photo_id FROM files WHERE id = ?", (b,)).fetchone()[0]
    db.add_photos_to_album(conn, album, [photo_a, photo_b])
    db.set_setting(conn, "marker", "v4")
    return plain, takeout


def test_version_4_database_upgrades_with_every_row_intact(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    conn = _open_at(path, 4, monkeypatch)
    plain, takeout = _populate_v4(conn)
    before = _dump(conn)
    conn.close()

    conn = db.open_database(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 5
    assert _dump(conn) == before
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [(s.id, s.type) for s in db.list_sources(conn)] == [
        (plain.id, "plain"),
        (takeout.id, "takeout"),
    ]

    backup = sqlite3.connect(tmp_path / "imagory.db.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
    assert backup.execute("SELECT value FROM settings WHERE key = 'marker'").fetchone()[0] == "v4"
    backup.close()
    conn.close()


def test_rebuilt_sources_accepts_cleaned_and_keeps_foreign_keys(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    conn = _open_at(path, 4, monkeypatch)
    plain, takeout = _populate_v4(conn)
    conn.close()
    conn = db.open_database(path)

    cleaned = db.create_source(conn, "/out", "Clean", "cleaned")
    assert db.get_source(conn, cleaned.id).type == "cleaned"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sources (label, root_path, type, added_at) VALUES ('x', '/x', 'zip', 'n')"
        )
    # References in files and scan_runs resolve to the rebuilt table.
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_file(conn, 999, _record("x.jpg", "hx"), db.utc_now())
    conn.execute("DELETE FROM sources WHERE id = ?", (takeout.id,))  # cascades
    assert (
        conn.execute("SELECT COUNT(*) FROM files WHERE source_id = ?", (takeout.id,)).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sources'").fetchone()[0]
    assert "'cleaned'" in sql and "sources_new" not in sql
    conn.close()


def test_user_cannot_choose_cleaned_type(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    src = db.create_source(conn, "/p", "p")
    assert "cleaned" not in db.SOURCE_TYPES and "cleaned" in db.ALL_SOURCE_TYPES
    with pytest.raises(ValueError):
        db.set_source_type(conn, src.id, "cleaned")
    conn.close()


def test_fresh_database_is_at_version_5_with_cleanup_tables(tmp_path):
    conn = db.open_database(tmp_path / "imagory.db")
    assert db.schema_version(conn) == 5
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert set(NEW_TABLES) <= names
    assert set(NEW_INDEXES) <= names
    assert "sources_new" not in names
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not (tmp_path / "imagory.db.bak").exists()
    conn.close()


def test_foreign_key_violation_rolls_migration_back(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    conn = _open_at(path, 4, monkeypatch)
    _populate_v4(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO files (source_id, relative_path, size, modified_time, first_seen_at, "
        "last_seen_at) VALUES (999, 'orphan.jpg', 1, 1, 'n', 'n')"
    )
    conn.close()

    with pytest.raises(RuntimeError, match="foreign key"):
        db.open_database(path)
    conn = db.connect(path)
    assert db.schema_version(conn) == 4
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not set(NEW_TABLES) & names and "sources_new" not in names
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sources'").fetchone()[0]
    assert "'cleaned'" not in sql
    assert (tmp_path / "imagory.db.bak").exists()
    conn.close()


def test_foreign_keys_back_on_after_failed_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "imagory.db"
    conn = _open_at(path, 4, monkeypatch)

    def _boom(c):
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:4], _boom])
    with pytest.raises(RuntimeError, match="boom"):
        db.migrate(conn, path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.schema_version(conn) == 4
    conn.close()
