"""Generate test images in code; real photos are never committed."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def make_image(
    path: Path,
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (200, 30, 30),
    *,
    taken: str | None = None,
    orientation: int | None = None,
    fmt: str | None = None,
) -> Path:
    """Write an image; ``taken`` is an EXIF ``YYYY:MM:DD HH:MM:SS`` DateTimeOriginal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    fmt = fmt or {".png": "PNG", ".gif": "GIF", ".heic": "HEIF"}.get(path.suffix.lower(), "JPEG")
    kwargs = {}
    if taken or orientation:
        exif = Image.Exif()
        if orientation:
            exif[0x0112] = orientation
        if taken:
            exif.get_ifd(0x8769)[0x9003] = taken
        kwargs["exif"] = exif.tobytes()
    if fmt == "HEIF":
        from pillow_heif import register_heif_opener

        register_heif_opener()
    img.save(path, fmt, **kwargs)
    return path
