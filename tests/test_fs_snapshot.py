import os

import pytest

from tests.fs_snapshot import assert_unchanged, snapshot

MTIME = 1_600_000_000_000_000_000


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "src"
    (root / "nested" / "deeper").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a.jpg").write_bytes(b"aaaa")
    (root / "nested" / "b.png").write_bytes(b"bb")
    (root / "nested" / "deeper" / "c.gif").write_bytes(b"c")
    (root / ".hidden" / "secret.txt").write_bytes(b"12345")
    for p in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(p, ns=(MTIME, MTIME))
    return root


def test_snapshot_keys_and_values(tree):
    assert snapshot(tree) == {
        "a.jpg": (4, MTIME),
        "nested/b.png": (2, MTIME),
        "nested/deeper/c.gif": (1, MTIME),
        ".hidden/secret.txt": (5, MTIME),
        "nested/": (0, MTIME),
        "nested/deeper/": (0, MTIME),
        ".hidden/": (0, MTIME),
    }


def test_unchanged_passes(tree):
    before = snapshot(tree)
    assert_unchanged(tree, before)


def test_added_file_fails(tree):
    before = snapshot(tree)
    (tree / "new.jpg").write_bytes(b"x")
    with pytest.raises(AssertionError, match="added: new.jpg"):
        assert_unchanged(tree, before)


def test_added_file_in_dot_folder_fails(tree):
    before = snapshot(tree)
    (tree / ".hidden" / "extra.txt").write_bytes(b"x")
    with pytest.raises(AssertionError, match="added: .hidden/extra.txt"):
        assert_unchanged(tree, before)


def test_removed_file_fails(tree):
    before = snapshot(tree)
    (tree / "nested" / "b.png").unlink()
    with pytest.raises(AssertionError, match="removed: nested/b.png"):
        assert_unchanged(tree, before)


def test_resized_file_fails(tree):
    before = snapshot(tree)
    (tree / "a.jpg").write_bytes(b"aaaaaa")
    with pytest.raises(AssertionError, match="changed: a.jpg"):
        assert_unchanged(tree, before)


def test_mtime_change_fails(tree):
    before = snapshot(tree)
    target = tree / "nested" / "deeper" / "c.gif"
    os.utime(target, ns=(MTIME + 10**9, MTIME + 10**9))
    with pytest.raises(AssertionError, match="changed: nested/deeper/c.gif"):
        assert_unchanged(tree, before)


def test_new_empty_folder_fails(tree):
    before = snapshot(tree)
    (tree / "nested" / "empty").mkdir()
    with pytest.raises(AssertionError, match="added: nested/empty/"):
        assert_unchanged(tree, before)


def test_file_created_and_deleted_fails(tree):
    before = snapshot(tree)
    tmp = tree / "nested" / "deeper" / "x.part"
    tmp.write_bytes(b"x")
    tmp.unlink()
    with pytest.raises(AssertionError, match="changed: nested/deeper/"):
        assert_unchanged(tree, before)
