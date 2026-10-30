"""Decoding images for display with Pillow (HEIC support, EXIF orientation applied)."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtGui import QImage

from imagory.thumbs import fit_within, register_heif


def load_qimage(path: Path, max_size: tuple[int, int] | None = None) -> QImage:
    """Read an image file (read-only) into a QImage, oriented as the camera intended.

    ``max_size`` lets JPEGs decode at reduced size when the full resolution is not needed.
    """
    register_heif()
    with open(path, "rb") as fh:
        data = fh.read()
    with Image.open(io.BytesIO(data)) as img:
        if max_size and img.format == "JPEG":
            img.draft("RGB", fit_within(img.size, *max_size))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA")
        width, height = img.size
        raw = img.tobytes("raw", "RGBA")
    return QImage(raw, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
