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
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from imagory import metadata

log = logging.getLogger(__name__)

THUMB_SIZE = 320
THUMB_QUALITY = 85
# Large panoramas are legitimate photos; keep a limit against decompression bombs.
Image.MAX_IMAGE_PIXELS = 300_000_000

_heif_registered = False


def register_heif() -> None:
    """Teach Pillow to open HEIC/HEIF files (once per process)."""
    global _heif_registered
    if not _heif_registered:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True


def fold_path(relative_path: str) -> str:
    """Fold ASCII case only, matching SQLite ``COLLATE NOCASE``."""
    return relative_path.translate(_ASCII_LOWER)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def thumb_key(relative_path: str) -> str:
    """V0 key: BLAKE2b of the case-folded relative path (V1 will key by content hash)."""
    return hashlib.blake2b(fold_path(relative_path).encode("utf-8"), digest_size=16).hexdigest()


def thumb_path(thumbs_root: Path, relative_path: str) -> Path:
    key = thumb_key(relative_path)
    return thumbs_root / key[0:2] / key[2:4] / f"{key}.jpg"


def clear_thumbs(thumbs_root: Path) -> None:
    """Delete the whole thumbnail cache (it lives in the library folder, never with photos)."""
    if thumbs_root.exists():
        shutil.rmtree(thumbs_root)
    thumbs_root.mkdir(parents=True, exist_ok=True)


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


def process_file(source_root: str, thumbs_root: str, job: FileJob) -> FileResult:
    """Read metadata and write the thumbnail from a single read of the file.

    Never raises for a bad image: the error is returned in the result instead.
    The source file is only ever opened for reading.
    """
    register_heif()
    path = os.path.join(source_root, *job.relative_path.split("/"))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            taken_exif, orientation = metadata.read_exif(img)
            width, height = metadata.displayed_size(img.size, orientation)
            make_thumbnail(img, thumb_path(Path(thumbs_root), job.relative_path))
    except Exception as exc:  # one bad image must never stop a scan
        return FileResult(
            job.relative_path, job.size, job.modified_time, error=f"{type(exc).__name__}: {exc}"
        )
    taken, source = metadata.best_date(taken_exif, job.modified_time)
    return FileResult(
        job.relative_path,
        job.size,
        job.modified_time,
        width=width,
        height=height,
        format=fmt,
        taken_at_local=taken,
        date_source=source,
    )


def process_batch(source_root: str, thumbs_root: str, jobs: list[FileJob]) -> list[FileResult]:
    """Worker-pool entry point: process several files per task to cut IPC overhead."""
    return [process_file(source_root, thumbs_root, job) for job in jobs]


def ensure_thumbnail(source_root: Path, thumbs_root: Path, relative_path: str) -> Path | None:
    """Return the cached thumbnail, regenerating it from the source file if it is missing."""
    dest = thumb_path(thumbs_root, relative_path)
    if dest.exists():
        return dest
    register_heif()
    src = source_root.joinpath(*relative_path.split("/"))
    try:
        with open(src, "rb") as fh, Image.open(io.BytesIO(fh.read())) as img:
            make_thumbnail(img, dest)
    except Exception as exc:
        log.warning("Could not regenerate thumbnail for %s: %s", relative_path, exc)
        return None
    return dest
