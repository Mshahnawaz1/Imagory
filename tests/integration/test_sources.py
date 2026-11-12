"""Source folder checks, relocation sampling, and the thumbnail cache upgrade."""

from pathlib import Path

from imagory import app, db, paths, sources, thumbs


def make_source(root_path: Path, label: str = "s", source_id: int = 1) -> db.Source:
    return db.Source(source_id, label, str(root_path), "plain", False, "2026-01-01T00:00:00Z", None)


def test_new_folder_is_fine(tmp_path):
    check = sources.check_folder(tmp_path / "b", [make_source(tmp_path / "a")], tmp_path / "lib")
    assert check == sources.FolderCheck()


def test_same_folder_is_rejected_ignoring_case_on_windows(tmp_path):
    existing = [make_source(tmp_path / "Photos", "Phone")]
    check = sources.check_folder(tmp_path / "PHOTOS", existing, tmp_path / "lib")
    assert check.error and "Phone" in check.error


def test_overlap_warns(tmp_path):
    existing = [make_source(tmp_path / "photos", "Phone")]
    inside = sources.check_folder(tmp_path / "photos" / "2019", existing, tmp_path / "lib")
    around = sources.check_folder(tmp_path, existing, tmp_path / "lib")
    assert inside.warning and around.warning and not inside.error
    # A sibling whose name only starts the same does not overlap.
    assert sources.check_folder(tmp_path / "photos2", existing, tmp_path / "lib").warning is None


def test_relocating_ignores_its_own_old_folder(tmp_path):
    existing = [make_source(tmp_path / "photos", source_id=7)]
    check = sources.check_folder(tmp_path / "photos", existing, tmp_path / "lib", ignore_id=7)
    assert check == sources.FolderCheck()


def test_folder_inside_library_is_rejected(tmp_path):
    check = sources.check_folder(tmp_path / "lib" / "thumbs", [], tmp_path / "lib")
    assert check.error


def test_thumbnail_cache_upgrade_clears_once(library):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    old = paths.thumbs_dir(root) / "source-1" / "ab" / "cd" / "abcd.jpg"  # layout 2
    old.parent.mkdir(parents=True)
    old.write_bytes(b"v0")
    app.upgrade_thumbnail_cache(conn, paths.thumbs_dir(root))
    assert not old.exists() and paths.thumbs_dir(root).is_dir()
    kept = thumbs.thumb_path(paths.thumbs_dir(root), thumbs.hash_bytes(b"x"))
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"v1")
    app.upgrade_thumbnail_cache(conn, paths.thumbs_dir(root))
    assert kept.exists()
    conn.close()
