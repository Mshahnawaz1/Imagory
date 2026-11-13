"""Generating and locating cached thumbnails, and the single-pass worker job.

Thumbnails are disposable: any of them can be deleted and is regenerated on demand.
Worker code here must not import Qt or touch the database.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from imagory import dates, metadata

log = logging.getLogger(__name__)

THUMB_SIZE = 320
THUMB_QUALITY = 85
# Version of the cache layout below; a different stored value means the cache is cleared.
LAYOUT_VERSION = "3"  # 3: keyed by content hash, shared across sources
HASH_BYTES = 16
# Large panoramas are legitimate photos; keep a limit against decompression bombs.
Image.MAX_IMAGE_PIXELS = 300_000_000

_heif_registered = False


def register_heif() -> None:
    """Teach Pillow to open HEIC/HEIF files (once per process)."""
    global _heif_registered
    if not _heif_registered:
        # pi-heif is the decode-only build of pillow-heif: no x265 encoder, LGPL wheels (PRD §13).
        from pi_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True


def fold_path(relative_path: str) -> str:
    """Fold ASCII case only, matching SQLite ``COLLATE NOCASE``."""
    return relative_path.translate(_ASCII_LOWER)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def hash_bytes(data: bytes) -> str:
    """A file's content hash: BLAKE2b of the full contents, 128-bit, as 32 hex digits.

    This is the photo's identity (PRD §8.5) and its thumbnail key.
    """
    return hashlib.blake2b(data, digest_size=HASH_BYTES).hexdigest()


def thumb_path(thumbs_root: Path, content_hash: str) -> Path:
    """``thumbs/ab/cd/<hash>.jpg``, shared by every file of a photo in every source."""
    return thumbs_root / content_hash[0:2] / content_hash[2:4] / f"{content_hash}.jpg"


def clear_thumbs(thumbs_root: Path) -> None:
    """Delete the whole thumbnail cache (it lives in the library folder, never with photos)."""
    if thumbs_root.exists():
        shutil.rmtree(thumbs_root)
    thumbs_root.mkdir(parents=True, exist_ok=True)


def remove_thumbnails(thumbs_root: Path, hashes: Iterable[str]) -> None:
    """Delete the thumbnails of photos being removed. Leftovers are only disk space, so
    errors are logged; empty ``ab/cd`` folders are left in place."""
    for content_hash in hashes:
        try:
            thumb_path(thumbs_root, content_hash).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not delete thumbnail %s: %s", content_hash, exc)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def fit_within(size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
    """Size after scaling ``size`` down (never up) to fit in ``max_w`` x ``max_h``.

    Use it for ``Image.draft``, which only reduces while *both* sides stay at or above the
    requested size, so asking for a square box would skip the reduction for most photos.
    """
    width, height = size
    scale = min(1.0, max_w / width, max_h / height) if width and height else 1.0
    return max(1, round(width * scale)), max(1, round(height * scale))


def make_thumbnail(img: Image.Image, dest: Path) -> None:
    """Write a 320 px JPEG thumbnail of ``img`` with EXIF orientation applied.

    The file is written atomically, so a reader never sees a half-written thumbnail.
    """
    if img.format == "JPEG":
        # Decode at reduced size; several times faster for large JPEGs.
        img.draft("RGB", fit_within(img.size, THUMB_SIZE, THUMB_SIZE))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS, reducing_gap=3.0)
    img = _to_rgb(img)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.tmp")
    try:
        img.save(tmp, "JPEG", quality=THUMB_QUALITY)
        _replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _replace(src: Path, dest: Path, attempts: int = 5) -> None:
    # On Windows the replace fails while the grid is reading the old thumbnail; retry briefly.
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


@dataclass(frozen=True)
class FileJob:
    relative_path: str
    size: int
    modified_time: int
    # Takeout JSON ``photoTakenTime.timestamp`` (Unix seconds), filled in by the
    # coordinator from the matched sidecar for Takeout sources (wave 2). None for now.
    takeout_timestamp: int | None = None


@dataclass(frozen=True)
class FileResult:
    """Plain, picklable worker result. ``error`` is set when the file could not be read."""

    relative_path: str
    size: int
    modified_time: int
    width: int = 0
    height: int = 0
    format: str = ""
    taken_at_local: str = ""
    date_source: str = ""
    error: str | None = None
    content_hash: str = ""
    taken_at_utc: str | None = None
    exif_taken_at: str | None = None
    exif_offset: str | None = None


def process_file(
    source_root: str, thumbs_root: str, job: FileJob, source_type: str = "plain"
) -> FileResult:
    """Hash the file, read its metadata and best date, and write its thumbnail (unless
    a copy with the same contents already has one), all from a single read of the file.

    Never raises for a bad image: the error is returned in the result instead.
    The source file is only ever opened for reading.
    """
    register_heif()
    path = os.path.join(source_root, *job.relative_path.split("/"))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        content_hash = hash_bytes(data)
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            exif = metadata.read_exif(img)
            width, height = metadata.displayed_size(img.size, exif.orientation)
            dest = thumb_path(Path(thumbs_root), content_hash)
            if not dest.exists():
                make_thumbnail(img, dest)
        date = dates.resolve_date(
            source_type,
            mtime_ns=job.modified_time,
            takeout_timestamp=job.takeout_timestamp,
            exif_datetime=exif.taken,
            exif_offset=exif.offset,
            filename=job.relative_path.rsplit("/", 1)[-1],
        )
    except Exception as exc:  # one bad image must never stop a scan
        return FileResult(
            job.relative_path, job.size, job.modified_time, error=f"{type(exc).__name__}: {exc}"
        )
    return FileResult(
        job.relative_path,
        job.size,
        job.modified_time,
        width=width,
        height=height,
        format=fmt,
        taken_at_local=date.taken_at_local,
        date_source=date.date_source,
        content_hash=content_hash,
        taken_at_utc=date.taken_at_utc,
        exif_taken_at=exif.taken,
        exif_offset=exif.offset,
    )


def process_batch(
    source_root: str, thumbs_root: str, jobs: list[FileJob], source_type: str = "plain"
) -> list[FileResult]:
    """Worker-pool entry point: process several files per task to cut IPC overhead."""
    return [process_file(source_root, thumbs_root, job, source_type) for job in jobs]


def ensure_thumbnail(
    source_root: Path, thumbs_root: Path, relative_path: str, content_hash: str | None
) -> Path | None:
    """Find a photo's cached thumbnail from what a ``GridPhoto`` carries, regenerating it
    from the source file if it is missing.

    ``content_hash`` None (a row stored without a hash): the file is read and hashed to
    find (or write) its thumbnail. The source file is only ever opened for reading.
    """
    if content_hash is not None:
        dest = thumb_path(thumbs_root, content_hash)
        if dest.exists():
            return dest
    register_heif()
    src = source_root.joinpath(*relative_path.split("/"))
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        dest = thumb_path(thumbs_root, content_hash or hash_bytes(data))
        if not dest.exists():
            with Image.open(io.BytesIO(data)) as img:
                make_thumbnail(img, dest)
    except Exception as exc:
        log.warning("Could not regenerate thumbnail for %s: %s", relative_path, exc)
        return None
    return dest
