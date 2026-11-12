from imagory.db import FileState
from imagory.scanner import WalkedFile, diff_files


def test_diff_categories():
    existing = [
        FileState(1, "same.jpg", 10, 100, "ok"),
        FileState(2, "size.jpg", 10, 100, "ok"),
        FileState(3, "mtime.jpg", 10, 100, "ok"),
        FileState(4, "gone.jpg", 10, 100, "ok"),
        FileState(5, "back.jpg", 10, 100, "missing"),
        FileState(6, "back_changed.jpg", 10, 100, "missing"),
        FileState(7, "still_gone.jpg", 10, 100, "missing"),
        FileState(8, "Dir/Case.jpg", 10, 100, "ok"),
    ]
    walked = [
        WalkedFile("same.jpg", 10, 100),
        WalkedFile("size.jpg", 11, 100),
        WalkedFile("mtime.jpg", 10, 101),
        WalkedFile("back.jpg", 10, 100),
        WalkedFile("back_changed.jpg", 12, 100),
        WalkedFile("dir/CASE.JPG", 10, 100),
        WalkedFile("new.jpg", 1, 1),
    ]
    d = diff_files(walked, existing)
    assert [f.relative_path for f in d.new] == ["new.jpg"]
    assert sorted(fid for fid, _ in d.changed) == [2, 3, 6]
    assert d.unchanged == 3
    assert d.reappeared == [5]
    assert d.case_renamed == [(8, "dir/CASE.JPG")]
    assert d.disappeared == [4]  # already-missing rows are not counted again


def test_empty_database_everything_new():
    walked = [WalkedFile("a.jpg", 1, 1), WalkedFile("b.jpg", 1, 1)]
    d = diff_files(walked, [])
    assert len(d.new) == 2 and not d.changed and d.unchanged == 0 and not d.disappeared


def test_new_paths_differing_only_in_case_are_deduplicated():
    d = diff_files([WalkedFile("a.jpg", 1, 1), WalkedFile("A.JPG", 1, 1)], [])
    assert [f.relative_path for f in d.new] == ["a.jpg"]


def test_non_ascii_case_is_not_folded():
    # SQLite NOCASE folds ASCII only, so the diff must not treat these as the same path.
    d = diff_files([WalkedFile("Ä.jpg", 1, 1)], [FileState(1, "ä.jpg", 1, 1, "ok")])
    assert [f.relative_path for f in d.new] == ["Ä.jpg"]
    assert d.disappeared == [1]
