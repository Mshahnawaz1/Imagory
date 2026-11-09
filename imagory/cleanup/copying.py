"""Copy one file and verify it (Takeout cleanup, plan pipeline stage 6).

Filesystem only: no SQL, no Qt. Safety rules:

* ``src`` is only ever opened ``"rb"`` (and ``stat``-ed). Nothing next to it is
  written, renamed or deleted.
* Every write, ``os.replace`` and delete targets ``dest`` or ``<dest>.part``.
* An existing ``dest`` is never overwritten: identical content is accepted as done,
  anything else is refused.
* Ordinary I/O failures (and cancel) never raise: the ``.part`` is removed and
  ``CopyResult(ok=False, ...)`` is returned.

``cancel`` follows the Scanner's convention: a ``Callable[[], bool]`` that is
polled once per chunk and returns True to stop.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

from imagory.cleanup.types import CopyResult
from imagory.thumbs import HASH_BYTES

CHUNK_SIZE = 1024 * 1024
PART_SUFFIX = ".part"
# NTFS stores mtimes in 100 ns steps, so a stored value can differ slightly.
MTIME_TOLERANCE_NS = 1_000


class _Cancelled(Exception):
    pass


def _new_hasher():
    return hashlib.blake2b(digest_size=HASH_BYTES)


def _fail(error: str) -> CopyResult:
    return CopyResult(ok=False, size=None, mtime_ns=None, error=error)


def copy_verified(
    src: Path,
    dest: Path,
    expected_hash: str,
    mtime_ns: int | None,
    cancel: Callable[[], bool] | None = None,
) -> CopyResult:
    """Copy ``src`` to ``dest`` through ``<dest>.part`` and verify it.

    The source is hashed while it is copied and must match ``expected_hash``
    (this catches a source that changed since analysis). The ``.part`` is then
    re-read and hashed, given ``mtime_ns`` (the source's mtime when None) and
    moved into place.
    """
    src, dest = Path(src), Path(dest)
    part = dest.with_name(dest.name + PART_SUFFIX)
    try:
        existing = _existing_dest(dest, expected_hash, cancel)
    except _Cancelled:
        return _fail("cancelled")
    except OSError as exc:
        return _fail(f"{type(exc).__name__}: {exc.strerror or exc}")
    if existing is not None:
        return existing
    try:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            hasher = _new_hasher()
            size = 0
            with open(src, "rb") as fin, open(part, "wb") as fout:
                while True:
                    if cancel is not None and cancel():
                        raise _Cancelled
                    chunk = fin.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    fout.write(chunk)
                    size += len(chunk)
                fout.flush()
                os.fsync(fout.fileno())
            if hasher.hexdigest() != expected_hash:
                raise ValueError("source changed since analysis (hash mismatch)")

            check = _new_hasher()
            with open(part, "rb") as fcheck:
                while chunk := fcheck.read(CHUNK_SIZE):
                    if cancel is not None and cancel():
                        raise _Cancelled
                    check.update(chunk)
            if check.hexdigest() != expected_hash:
                raise ValueError("copy does not match source (verify failed)")

            if mtime_ns is None:
                mtime_ns = os.stat(src).st_mtime_ns
            os.utime(part, ns=(mtime_ns, mtime_ns))
            os.replace(part, dest)
            return CopyResult(ok=True, size=size, mtime_ns=mtime_ns, error=None)
        except BaseException:
            _discard_part(part)
            raise
    except _Cancelled:
        return _fail("cancelled")
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"{type(exc).__name__}: {exc.strerror or exc}")


def _existing_dest(
    dest: Path, expected_hash: str, cancel: Callable[[], bool] | None
) -> CopyResult | None:
    """Never overwrite a file already in the output (an earlier run's copy may be the only one).

    Returns None when ``dest`` doesn't exist. When it holds the expected content (a copy whose
    ``done`` mark was lost), it is accepted as is, with its own size and mtime. Anything else
    at ``dest`` is refused.
    """
    try:
        st = os.stat(dest, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return _fail("destination exists and is not a regular file")
    hasher = _new_hasher()
    with open(dest, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            if cancel is not None and cancel():
                raise _Cancelled
            hasher.update(chunk)
    if hasher.hexdigest() != expected_hash:
        return _fail("destination exists with different content")
    return CopyResult(ok=True, size=st.st_size, mtime_ns=st.st_mtime_ns, error=None)


def _discard_part(part: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(part)


def already_done(dest: Path, size: int, mtime_ns: int) -> bool:
    """True when ``dest`` is a regular file with this size and mtime (resume)."""
    try:
        st = os.stat(dest)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_size == size
        and abs(st.st_mtime_ns - mtime_ns) <= MTIME_TOLERANCE_NS
    )
