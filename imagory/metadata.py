"""Reading image metadata: dimensions, format, EXIF date and orientation.

Runs inside worker processes, so it must not import Qt or touch the database.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image

EXIF_IFD = 0x8769
TAG_ORIENTATION = 0x0112
TAG_DATETIME_ORIGINAL = 0x9003

# Orientations 5-8 rotate by 90 degrees, so width and height swap when displayed.
_SWAPS_AXES = {5, 6, 7, 8}


def parse_exif_datetime(value: object) -> str | None:
    """Parse an EXIF ``YYYY:MM:DD HH:MM:SS`` value into ISO 8601 without offset."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("\x00").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            parsed = datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        return parsed.isoformat(timespec="seconds")
    return None


def mtime_to_local(mtime_ns: int) -> str:
    """Convert a file modified time (ns since epoch) to local wall-clock ISO 8601."""
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000).isoformat(timespec="seconds")


def best_date(exif_date: str | None, mtime_ns: int) -> tuple[str, str]:
    """V0 date fallback: EXIF ``DateTimeOriginal``, else the file's modified time."""
    if exif_date:
        return exif_date, "exif"
    return mtime_to_local(mtime_ns), "mtime"


def read_exif(img: Image.Image) -> tuple[str | None, int]:
    """Return (DateTimeOriginal as ISO text or None, orientation 1-8)."""
    try:
        exif = img.getexif()
    except Exception:
        return None, 1
    orientation = exif.get(TAG_ORIENTATION, 1)
    if not isinstance(orientation, int) or not 1 <= orientation <= 8:
        orientation = 1
    try:
        taken = parse_exif_datetime(exif.get_ifd(EXIF_IFD).get(TAG_DATETIME_ORIGINAL))
    except Exception:
        taken = None
    return taken, orientation


def displayed_size(size: tuple[int, int], orientation: int) -> tuple[int, int]:
    """Width and height as shown after applying EXIF orientation."""
    width, height = size
    return (height, width) if orientation in _SWAPS_AXES else (width, height)
