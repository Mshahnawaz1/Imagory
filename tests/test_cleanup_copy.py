import hashlib
import os
from pathlib import Path

import pytest

from imagory import cleanup_copy
from imagory.cleanup_copy import already_done, copy_verified
from imagory.thumbs import hash_bytes
from tests.fs_snapshot import assert_unchanged, snapshot


@pytest.fixture
def src_dir(tmp_path: Path) -> Path:
    d = tmp_path / "input"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"photo-bytes" * 1000)
    os.utime(d / "a.jpg", ns=(1_600_000_000_123_456_000,) * 2)
    return d


@pytest.fixture
def out(tmp_path: Path) -> Path:
    d = tmp_path / "output"
    d.mkdir()
    return d


def test_success(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    dest = out / "sub" / "deep" / "a.jpg"
    mtime = 1_500_000_000_000_000_000
    r = copy_verified(src_dir / "a.jpg", dest, hash_bytes(data), mtime)
    assert r.ok and r.error is None and r.size == len(data)
    assert dest.read_bytes() == data
    assert dest.stat().st_mtime_ns == mtime
    assert not Path(str(dest) + ".part").exists()
    assert already_done(dest, len(data), r.mtime_ns)
    assert_unchanged(src_dir, before)


def test_mtime_none_uses_source(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    r = copy_verified(src_dir / "a.jpg", out / "a.jpg", hash_bytes(data), None)
    assert r.ok and r.mtime_ns == 1_600_000_000_123_456_000
    assert_unchanged(src_dir, before)


def test_hash_mismatch(src_dir, out):
    before = snapshot(src_dir)
    r = copy_verified(src_dir / "a.jpg", out / "a.jpg", hash_bytes(b"old"), None)
    assert not r.ok and "hash mismatch" in r.error
    assert list(out.iterdir()) == []
    assert_unchanged(src_dir, before)


def test_missing_source(src_dir, out):
    before = snapshot(src_dir)
    r = copy_verified(src_dir / "nope.jpg", out / "x.jpg", "00", None)
    assert not r.ok and r.error
    assert list(out.iterdir()) == []
    assert_unchanged(src_dir, before)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores permissions"
)
def test_dest_folder_read_only(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    out.chmod(0o500)
    try:
        r = copy_verified(src_dir / "a.jpg", out / "a.jpg", hash_bytes(data), None)
    finally:
        out.chmod(0o700)
    assert not r.ok and r.error
    assert list(out.iterdir()) == []
    assert_unchanged(src_dir, before)


def test_cancel_mid_file(src_dir, out, monkeypatch):
    monkeypatch.setattr(cleanup_copy, "CHUNK_SIZE", 1000)
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 3

    r = copy_verified(src_dir / "a.jpg", out / "a.jpg", hash_bytes(data), None, cancel)
    assert not r.ok and r.error == "cancelled"
    assert list(out.iterdir()) == []
    assert_unchanged(src_dir, before)


def test_leftover_part_overwritten(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    (out / "a.jpg.part").write_bytes(b"junk" * 5000)
    r = copy_verified(src_dir / "a.jpg", out / "a.jpg", hash_bytes(data), None)
    assert r.ok
    assert (out / "a.jpg").read_bytes() == data
    assert not (out / "a.jpg.part").exists()
    assert_unchanged(src_dir, before)


def test_large_file_streamed(src_dir, out, monkeypatch):
    big = src_dir / "big.bin"
    block = os.urandom(4096)
    with open(big, "wb") as f:
        for _ in range(1024):  # 4 MB
            f.write(block)
    expected = hashlib.blake2b(big.read_bytes(), digest_size=16).hexdigest()
    chunk = 64 * 1024
    monkeypatch.setattr(cleanup_copy, "CHUNK_SIZE", chunk)
    before = snapshot(src_dir)

    sizes: list[int] = []
    real_open = open

    class Spy:
        def __init__(self, f):
            self._f = f

        def read(self, n=-1):
            sizes.append(n)
            return self._f.read(n)

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()

    def spy_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        return Spy(f) if "r" in mode else f

    monkeypatch.setattr(cleanup_copy, "open", spy_open, raising=False)
    r = copy_verified(big, out / "big.bin", expected, None)
    assert r.ok and (out / "big.bin").stat().st_size == 4096 * 1024
    assert sizes and all(n == chunk for n in sizes)
    assert len(sizes) >= 2 * 64  # source pass and verify pass, chunk by chunk
    assert_unchanged(src_dir, before)


def test_already_done(out):
    f = out / "x.jpg"
    f.write_bytes(b"abc")
    os.utime(f, ns=(5_000_000_000, 5_000_000_000))
    assert already_done(f, 3, 5_000_000_000)
    assert not already_done(f, 4, 5_000_000_000)
    assert not already_done(f, 3, 6_000_000_000)
    assert not already_done(out / "missing", 3, 5_000_000_000)
    assert not already_done(out, 3, 5_000_000_000)


def test_existing_dest_with_other_content_is_never_overwritten(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    dest = out / "a.jpg"
    dest.write_bytes(b"an earlier run's copy")
    os.utime(dest, ns=(1_400_000_000_000_000_000,) * 2)
    out_before = snapshot(out)
    r = copy_verified(src_dir / "a.jpg", dest, hash_bytes(data), 1_500_000_000_000_000_000)
    assert not r.ok and "different content" in r.error
    assert_unchanged(out, out_before)
    assert_unchanged(src_dir, before)


def test_existing_dest_with_same_content_is_accepted(src_dir, out):
    before = snapshot(src_dir)
    data = (src_dir / "a.jpg").read_bytes()
    dest = out / "a.jpg"
    dest.write_bytes(data)
    os.utime(dest, ns=(1_400_000_000_000_000_000,) * 2)
    out_before = snapshot(out)
    r = copy_verified(src_dir / "a.jpg", dest, hash_bytes(data), 1_500_000_000_000_000_000)
    assert r.ok and r.size == len(data) and r.mtime_ns == 1_400_000_000_000_000_000
    assert_unchanged(out, out_before)
    assert_unchanged(src_dir, before)
