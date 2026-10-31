"""Google Takeout support: detecting Takeout sources.

Sidecar matching and reading (PRD §8.3) come later; detection only needs folder and file
names, so it never opens a file.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from imagory.scanner import SUPPORTED_EXTENSIONS, extension_of, is_skipped_dir

log = logging.getLogger(__name__)

# Casefolded names of the "Google Photos" folder in Takeout exports. Only the English name
# is confirmed; the localized ones still need checking on real exports (PRD §18 item 6).
GOOGLE_PHOTOS_NAMES = frozenset(
    name.casefold()
    for name in (
        "Google Photos",
        "Google Fotos",
        "Google Foto",
        "Zdjęcia Google",
        "Google Фото",
        "Google フォト",
        "Google 포토",
        "Google 相簿",
        "Google 相册",
    )
)
SIDECAR_SHARE = 0.5  # share of sampled images with a sidecar that makes a folder Takeout
MAX_SAMPLE_DIRS = 200
MAX_SAMPLE_IMAGES = 2000
# Takeout truncates long sidecar names; a shorter prefix match would be too loose.
MIN_TRUNCATED_PREFIX = 30

_COUNTER = re.compile(r"^(?P<stem>.*)\((?P<n>\d+)\)(?P<ext>\.[^.]+)$")
_EDITED = "-edited"


@dataclass(frozen=True)
class Detection:
    type: str  # 'plain' | 'takeout'
    reason: str  # shown in the Add source dialog


def is_google_photos_name(name: str) -> bool:
    return name.casefold() in GOOGLE_PHOTOS_NAMES


def _sidecar_prefixes(image_name: str) -> list[str]:
    """Names a sidecar of ``image_name`` starts with, covering Takeout's naming quirks."""
    name = image_name.casefold()
    prefixes = [name]
    match = _COUNTER.match(name)
    if match:  # IMG_1234(1).jpg uses IMG_1234.jpg(1).json
        prefixes.append(match["stem"] + match["ext"])
    stem, ext = os.path.splitext(name)
    if stem.endswith(_EDITED):  # edited copies use the original's sidecar
        prefixes.append(stem[: -len(_EDITED)] + ext)
    return prefixes


class SidecarNames:
    """The JSON file names of one folder, indexed for prefix lookups."""

    def __init__(self, json_names: list[str]) -> None:
        self._sorted = sorted(n.casefold() for n in json_names)
        self._stems = {n[: -len(".json")] for n in self._sorted}

    def _has_prefix(self, prefix: str) -> bool:
        i = bisect.bisect_left(self._sorted, prefix)
        return i < len(self._sorted) and self._sorted[i].startswith(prefix)

    def has_sidecar(self, image_name: str) -> bool:
        """Whether a JSON file here looks like the sidecar of ``image_name``.

        A detection heuristic, not the matching used when importing (PRD §8.3).
        """
        for prefix in _sidecar_prefixes(image_name):
            if self._has_prefix(prefix):
                return True
            # Truncated long names: the sidecar's name (minus .json) is a prefix of the image's.
            for length in range(MIN_TRUNCATED_PREFIX, len(prefix)):
                if prefix[:length] in self._stems:
                    return True
        return False


def detect_source_type(root: Path | str) -> Detection:
    """Guess whether ``root`` is a Google Takeout export (PRD §8.1).

    Takeout if the folder or a direct child is named "Google Photos" (or a localized name),
    or if JSON sidecars sit next to at least half of a bounded sample of its images.
    """
    root = Path(root)
    if is_google_photos_name(root.name):
        return Detection("takeout", f"The folder is named “{root.name}”.")
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if is_google_photos_name(entry.name) and entry.is_dir(follow_symlinks=False):
                    return Detection("takeout", f"It contains a “{entry.name}” folder.")
    except OSError as exc:
        log.warning("Cannot read %s for type detection: %s", root, exc)
        return Detection("plain", "The folder could not be read.")

    images, with_sidecar = _sample_sidecars(root)
    if images and with_sidecar / images >= SIDECAR_SHARE:
        share = round(100 * with_sidecar / images)
        return Detection("takeout", f"JSON sidecar files sit next to {share}% of its photos.")
    return Detection("plain", "No Google Takeout layout was found.")


def _sample_sidecars(root: Path) -> tuple[int, int]:
    """Count sampled images and those with a sidecar, breadth first, without opening files."""
    images = with_sidecar = dirs = 0
    queue: deque[str] = deque([str(root)])
    while queue and dirs < MAX_SAMPLE_DIRS and images < MAX_SAMPLE_IMAGES:
        path = queue.popleft()
        dirs += 1
        image_names: list[str] = []
        json_names: list[str] = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not is_skipped_dir(entry.name):
                            queue.append(entry.path)
                    elif entry.name.casefold().endswith(".json"):
                        json_names.append(entry.name)
                    elif extension_of(entry.name) in SUPPORTED_EXTENSIONS:
                        image_names.append(entry.name)
        except OSError as exc:
            log.warning("Cannot read %s for type detection: %s", path, exc)
            continue
        sidecars = SidecarNames(json_names)
        for name in image_names[: MAX_SAMPLE_IMAGES - images]:
            images += 1
            with_sidecar += sidecars.has_sidecar(name)
    return images, with_sidecar
