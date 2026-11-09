"""Hash workers for files the scanner doesn't read (other files and JSON). Picklable, no SQL."""

from __future__ import annotations

import hashlib
import os

from imagory.thumbs import HASH_BYTES

HASH_CHUNK = 1024 * 1024


def hash_file(path: str) -> str:
    """BLAKE2b (16 bytes, as ``thumbs.hash_bytes``) of a file, streamed. Opens it ``rb``."""
    hasher = hashlib.blake2b(digest_size=HASH_BYTES)
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_batch(root: str, rels: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Worker entry point: ``(relative path, hash or None, error or None)`` per file."""
    out: list[tuple[str, str | None, str | None]] = []
    for rel in rels:
        try:
            out.append((rel, hash_file(os.path.join(root, *rel.split("/"))), None))
        except OSError as exc:
            out.append((rel, None, f"{type(exc).__name__}: {exc.strerror or exc}"))
    return out
