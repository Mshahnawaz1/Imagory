import os
import sys

import pytest

from imagory.scanner import extension_of, is_skipped_dir, walk_source


def _touch(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _paths(result):
    return sorted(f.relative_path for f in result.files)


def test_extensions_case_insensitive_and_skips_counted(tmp_path):
    for name in [
        "a.jpg",
        "b.JPEG",
        "c.Png",
        "d.gif",
        "e.HEIC",
        "f.heif",
        "g.mp4",
        "h.MP4",
        "i.txt",
        "noext",
    ]:
        _touch(tmp_path / name)
    result = walk_source(tmp_path)
    assert _paths(result) == ["a.jpg", "b.JPEG", "c.Png", "d.gif", "e.HEIC", "f.heif"]
    assert result.skipped_by_ext == {".mp4": 2, ".txt": 1, "(none)": 1}


def test_recursive_with_forward_slash_paths(tmp_path):
    _touch(tmp_path / "2019" / "Jan" / "x.jpg")
    result = walk_source(tmp_path)
    assert _paths(result) == ["2019/Jan/x.jpg"]
    f = result.files[0]
    st = (tmp_path / "2019" / "Jan" / "x.jpg").stat()
    assert (f.size, f.modified_time) == (st.st_size, st.st_mtime_ns)


def test_skips_hidden_and_system_folders(tmp_path):
    _touch(tmp_path / ".thumbs" / "a.jpg")
    _touch(tmp_path / "$RECYCLE.BIN" / "b.jpg")
    _touch(tmp_path / "System Volume Information" / "c.jpg")
    _touch(tmp_path / "keep" / "d.jpg")
    assert _paths(walk_source(tmp_path)) == ["keep/d.jpg"]


def test_is_skipped_dir():
    assert is_skipped_dir(".git")
    assert is_skipped_dir("$Recycle.Bin")
    assert is_skipped_dir("system volume information")
    assert not is_skipped_dir("Photos")


def test_skips_library_inside_source(tmp_path):
    _touch(tmp_path / "lib" / "thumbs" / "ab" / "cd" / "t.jpg")
    _touch(tmp_path / "photo.jpg")
    assert _paths(walk_source(tmp_path, exclude=tmp_path / "lib")) == ["photo.jpg"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    _touch(outside / "o.jpg")
    src = tmp_path / "src"
    _touch(src / "real.jpg")
    try:
        os.symlink(outside, src / "linkdir", target_is_directory=True)
        os.symlink(outside / "o.jpg", src / "link.jpg")
    except OSError:
        pytest.skip("symlinks not permitted")
    assert _paths(walk_source(src)) == ["real.jpg"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_does_not_follow_junctions(tmp_path):
    import subprocess

    outside = tmp_path / "outside"
    _touch(outside / "o.jpg")
    src = tmp_path / "src"
    _touch(src / "real.jpg")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "j"), str(outside)], check=True)
    assert _paths(walk_source(src)) == ["real.jpg"]


def test_cancel_marks_incomplete(tmp_path):
    _touch(tmp_path / "a.jpg")
    result = walk_source(tmp_path, cancelled=lambda: True)
    assert not result.complete


def test_extension_of():
    assert extension_of("A.JPG") == ".jpg"
    assert extension_of("README") == "(none)"
