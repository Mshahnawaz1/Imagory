"""Shared helpers for proving an input folder was never modified (PRD principle 1)."""

from __future__ import annotations

import os
from pathlib import Path


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Map every file under ``root`` (dot folders included) to ``(size, mtime_ns)``.

    Symlinks are not followed, and a symlink's own metadata is recorded.
    Keys are ``/``-separated paths relative to ``root``.
    """
    result: dict[str, tuple[int, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            st = os.stat(path, follow_symlinks=False)
            result[path.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return result


def assert_unchanged(root: Path, before: dict[str, tuple[int, int]]) -> None:
    """Fail if ``root`` no longer matches the ``before`` snapshot.

    The assertion message lists added, removed and changed paths, sorted.
    """
    after = snapshot(root)
    if after == before:
        return
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    lines = [f"folder changed: {root}"]
    lines += [f"  added: {p}" for p in added]
    lines += [f"  removed: {p}" for p in removed]
    lines += [f"  changed: {p} {before[p]} -> {after[p]}" for p in changed]
    raise AssertionError("\n".join(lines))
