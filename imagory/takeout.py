"""Google Takeout support: detection, sidecar matching and reading (PRD §8.1, §8.3).

Detection and matching only need folder and file names, so they never open a file. Matching
is a pure function over the names in one folder (``match_directory``); only the optional
title confirmation and ``read_sidecar`` read files, and only for reading. Nothing here
touches the database or the scan; the scan wires these functions in separately.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from imagory.dates import to_utc_iso
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

SUPPLEMENTAL = ".supplemental-metadata"
SIDECAR_SUFFIX = ".json"
MAX_SIDECAR_BYTES = 1_000_000  # real sidecars are about 1 KB; refuse anything absurd

# Edited-copy suffixes, casefolded. "-edited" is confirmed (English Takeout). The others are
# localized forms still to be checked on real exports (PRD §18 item 6); a miss is harmless
# because the edited copy then just shows up in the matching report.
EDITED_SUFFIXES = (
    "-edited",
    "-bearbeitet",  # de, unconfirmed
    "-modifié",  # fr, unconfirmed
    "-modificato",  # it, unconfirmed
    "-editado",  # es and pt, unconfirmed
    "-bewerkt",  # nl, unconfirmed
    "-edytowane",  # pl, unconfirmed
    "-отредактировано",  # ru, unconfirmed
    "-編集済み",  # ja, unconfirmed
)

# Casefolded names of the Takeout "Trash" folder. Only the English name is confirmed; the
# localized ones are guesses to check on real exports (PRD §18 item 6).
TRASH_NAMES = frozenset(
    name.casefold()
    for name in (
        "Trash",
        "Papierkorb",
        "Corbeille",
        "Papelera",
        "Cestino",
        "Lixeira",
        "Prullenbak",
        "Kosz",
        "Корзина",
        "ごみ箱",
    )
)

# JSON files Takeout writes that are not photo sidecars.
NON_SIDECAR_JSON = frozenset(
    {
        "metadata.json",
        "print-subscriptions.json",
        "shared_album_comments.json",
        "user-generated-memory-titles.json",
    }
)

_COUNTER = re.compile(r"^(?P<stem>.*)\((?P<n>\d+)\)(?P<ext>\.[^.]+)$")
_STEM_COUNTER = re.compile(r"^(?P<core>.*)\((?P<n>\d+)\)$")


@dataclass(frozen=True)
class Detection:
    type: str  # 'plain' | 'takeout'
    reason: str  # shown in the Add source dialog


def is_google_photos_name(name: str) -> bool:
    return name.casefold() in GOOGLE_PHOTOS_NAMES


def is_trash_dir(relative_dir: str) -> bool:
    """Whether a folder is Takeout's Trash (path relative to the source root, ``/`` separators).

    True when the last component is a Trash name and its parent is the source root or a
    "Google Photos" folder, so a user's own "Trash" folder deeper down is still imported.
    Archive is not skipped. Case-insensitive. Only meaningful for Takeout sources.
    """
    parts = [p for p in relative_dir.replace("\\", "/").split("/") if p]
    if not parts or parts[-1].casefold() not in TRASH_NAMES:
        return False
    return len(parts) == 1 or is_google_photos_name(parts[-2])


# --- sidecar matching ---------------------------------------------------------------------


def _interpretations(name: str) -> list[tuple[str, int]]:
    """``(base name, counter)`` readings of a casefolded image name, best first.

    ``img_1(1).jpg`` is ``("img_1.jpg", 1)``, because Takeout moves the counter after the
    extension in the sidecar (``IMG_1.jpg(1).json``), or else literally ``("img_1(1).jpg", 0)``.
    """
    match = _COUNTER.match(name)
    if match:
        return [(match["stem"] + match["ext"], int(match["n"])), (name, 0)]
    return [(name, 0)]


def _strip_edited(name: str) -> str | None:
    stem, ext = os.path.splitext(name)
    for suffix in EDITED_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)] + ext
    return None


def _edited_interpretations(name: str) -> list[tuple[str, int]]:
    """Readings of an edited copy's casefolded name that point at the original's sidecar."""
    readings = []
    for base, counter in _interpretations(name):
        original = _strip_edited(base)
        if original is not None:
            readings.append((original, counter))
    return readings


@dataclass(frozen=True)
class _Sidecar:
    name: str  # as on disk
    core: str  # casefolded, without ".json" and the "(n)" counter
    counter: int


class _SidecarIndex:
    """The sidecars of one folder keyed by ``(counter, core)``, in a stable order."""

    def __init__(self, json_names: Iterable[str]) -> None:
        self.sidecars: list[_Sidecar] = []
        self._by_key: dict[tuple[int, str], list[int]] = {}
        for name in sorted(set(json_names), key=lambda n: (n.casefold(), n)):
            folded = name.casefold()
            if not folded.endswith(SIDECAR_SUFFIX) or folded in NON_SIDECAR_JSON:
                continue
            stem = folded[: -len(SIDECAR_SUFFIX)]
            counter = 0
            match = _STEM_COUNTER.match(stem)
            if match:
                stem, counter = match["core"], int(match["n"])
            self._by_key.setdefault((counter, stem), []).append(len(self.sidecars))
            self.sidecars.append(_Sidecar(name, stem, counter))

    def candidates(self, readings: list[tuple[str, int]]) -> list[tuple[int, int, int]]:
        """``(sidecar index, kind, reading rank)`` for sidecars that can belong to an image.

        Kind 0: the sidecar name starts with the full image name, optionally followed by a
        (possibly truncated) ``.supplemental-metadata``. Kind 1: Takeout cut a long name, so
        the sidecar's name is a prefix of the image's, at least ``MIN_TRUNCATED_PREFIX`` long.
        """
        found = []
        for rank, (base, counter) in enumerate(readings):
            full = base + SUPPLEMENTAL
            for length in range(len(base), len(full) + 1):
                found += [(i, 0, rank) for i in self._by_key.get((counter, full[:length]), ())]
            for length in range(MIN_TRUNCATED_PREFIX, len(base)):
                found += [(i, 1, rank) for i in self._by_key.get((counter, base[:length]), ())]
        return found


@dataclass(frozen=True)
class DirectoryMatch:
    """Matching result for one folder."""

    matches: dict[str, str | None]  # image name -> sidecar name or None, as on disk
    unused_sidecars: tuple[str, ...]  # sidecar names no image used, sorted

    @property
    def matched(self) -> int:
        return sum(1 for s in self.matches.values() if s is not None)

    @property
    def unmatched_images(self) -> list[str]:
        return sorted(n for n, s in self.matches.items() if s is None)


def match_directory(
    names: Iterable[str], read_title: Callable[[str], str | None] | None = None
) -> DirectoryMatch:
    """Map each supported image name in one folder to its sidecar JSON name, or None.

    ``names`` are the file names (not paths) in the folder. ``read_title(json_name)`` may
    return the sidecar's ``title`` (the original file name). It is called lazily, only to
    confirm truncated matches or to break ties, and compared case-insensitively.

    Rules (PRD §8.3), all case-insensitive and deterministic:

    1. A sidecar is claimed by at most one non-edited image. Candidate pairs are sorted by
       title (match, unknown, mismatch), then exact-length before truncated matches, then
       non-edited before edited, then the counter-moved reading before the literal one, then
       by name; assignment is greedy in that order.
    2. A truncated match whose readable title names a different file is rejected.
    3. An edited copy (``-edited`` and the localized suffixes) with no sidecar of its own
       uses its original's sidecar. It does not claim it exclusively.
    """
    all_names = list(names)
    images = sorted(n for n in all_names if extension_of(n) in SUPPORTED_EXTENSIONS)
    index = _SidecarIndex(n for n in all_names if n.casefold().endswith(SIDECAR_SUFFIX))
    titles: dict[int, str | None] = {}

    def title_of(i: int) -> str | None:
        if i not in titles:
            title = None
            if read_title is not None:
                try:
                    title = read_title(index.sidecars[i].name)
                except Exception:  # a bad sidecar must never stop matching
                    log.warning("Cannot read title of %s", index.sidecars[i].name, exc_info=True)
            titles[i] = title.casefold() if isinstance(title, str) else None
        return titles[i]

    def rank_pairs(
        name: str, readings: list[tuple[str, int]], borrowed: bool, allow: Callable[[int], bool]
    ) -> list[tuple[tuple, str, int]]:
        cands = [c for c in index.candidates(readings) if allow(c[0])]
        wanted = {name.casefold(), *(base for base, _ in readings)}
        edited = _strip_edited(name.casefold()) is not None
        pairs = []
        for i, kind, rank in cands:
            if borrowed or kind == 1 or len(cands) > 1 or len(sharers.get(i, ())) > 1:
                title = title_of(i)  # only where the title can decide something
            else:
                title = None
            if title is None:
                penalty = 1
            elif title in wanted:
                penalty = 0
            elif kind == 1:
                continue  # a truncated prefix of some other file
            else:
                penalty = 2
            key = (penalty, kind, edited, rank, name.casefold(), index.sidecars[i].name)
            pairs.append((key, name, i))
        return pairs

    sharers: dict[int, set[str]] = {}
    for name in images:
        for i, _kind, _rank in index.candidates(_interpretations(name.casefold())):
            sharers.setdefault(i, set()).add(name)

    pairs = []
    for name in images:
        pairs += rank_pairs(name, _interpretations(name.casefold()), False, lambda i: True)
    pairs.sort(key=lambda p: (p[0], p[1]))

    result: dict[str, str | None] = dict.fromkeys(images)
    claimed: dict[int, str] = {}
    for _key, name, i in pairs:
        if result[name] is None and i not in claimed:
            claimed[i] = name
            result[name] = index.sidecars[i].name
    used = set(claimed)

    # Edited copies still without a sidecar borrow the original's (or an unclaimed one).
    for name in images:
        readings = _edited_interpretations(name.casefold())
        if result[name] is not None or not readings:
            continue

        def free_or_original(i: int, readings: list[tuple[str, int]] = readings) -> bool:
            owner = claimed.get(i)
            return owner is None or any(r in readings for r in _interpretations(owner.casefold()))

        ranked = rank_pairs(name, readings, True, free_or_original)
        if ranked:
            i = min(ranked, key=lambda p: p[0])[2]
            result[name] = index.sidecars[i].name
            used.add(i)

    unused = tuple(sorted(s.name for i, s in enumerate(index.sidecars) if i not in used))
    return DirectoryMatch(result, unused)


def sidecar_for(
    image_name: str,
    json_names: Iterable[str],
    read_title: Callable[[str], str | None] | None = None,
) -> str | None:
    """The sidecar of one image among ``json_names`` (convenience over ``match_directory``)."""
    return match_directory([image_name, *json_names], read_title).matches.get(image_name)


class SidecarNames:
    """The JSON file names of one folder, for the detection heuristic."""

    def __init__(self, json_names: list[str]) -> None:
        self._index = _SidecarIndex(json_names)

    def has_sidecar(self, image_name: str) -> bool:
        """Whether a JSON file here looks like the sidecar of ``image_name``.

        Uses the same name rules as ``match_directory`` but looks at one image alone, with no
        claiming and no title check.
        """
        folded = image_name.casefold()
        readings = _interpretations(folded) + _edited_interpretations(folded)
        return bool(self._index.candidates(readings))


# --- reading sidecars ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sidecar:
    """The fields Imagory reads from a Takeout sidecar (PRD §8.3)."""

    title: str | None  # the original file name
    taken_timestamp: int | None  # photoTakenTime.timestamp, Unix seconds UTC
    taken_utc: str | None  # the same moment as ISO 8601 ending in Z
    latitude: float | None  # None when geoData is missing or 0/0
    longitude: float | None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _taken(data: dict) -> tuple[int | None, str | None]:
    taken = data.get("photoTakenTime")
    raw = taken.get("timestamp") if isinstance(taken, dict) else None
    seconds = _number(raw)
    if seconds is None or seconds <= 0:
        return None, None
    try:
        return int(seconds), to_utc_iso(datetime.fromtimestamp(int(seconds), UTC))
    except (OverflowError, OSError, ValueError):
        return None, None


def _location(data: dict) -> tuple[float | None, float | None]:
    # Only geoData is used. geoDataExif holds the location from the photo's EXIF and is
    # often set when geoData is 0/0; geoData is what Google Photos shows, including a
    # location the user removed or edited, so falling back would bring back a removed one.
    geo = data.get("geoData")
    if not isinstance(geo, dict):
        return None, None
    lat, lon = _number(geo.get("latitude")), _number(geo.get("longitude"))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def parse_sidecar(data: object) -> Sidecar | None:
    """Pull the fields Imagory needs out of decoded sidecar JSON; None if it is not an object."""
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    seconds, utc = _taken(data)
    lat, lon = _location(data)
    return Sidecar(title if isinstance(title, str) else None, seconds, utc, lat, lon)


def read_sidecar(path: Path | str) -> Sidecar | None:
    """Read a sidecar file (read-only). Unreadable or malformed JSON gives None and is logged."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SIDECAR_BYTES + 1)
        if len(raw) > MAX_SIDECAR_BYTES:
            log.warning("Sidecar %s is too large; ignoring it", path)
            return None
        return parse_sidecar(json.loads(raw.decode("utf-8-sig")))
    except (OSError, ValueError, RecursionError) as exc:  # ValueError covers JSON and Unicode
        log.warning("Cannot read sidecar %s: %s", path, exc)
        return None


def read_title(path: Path | str) -> str | None:
    """Just the sidecar's title, for title confirmation while matching."""
    sidecar = read_sidecar(path)
    return sidecar.title if sidecar else None


def match_folder(folder: Path | str, names: Iterable[str]) -> DirectoryMatch:
    """``match_directory`` for a real folder: sidecar titles are read (read-only) on demand."""
    base = Path(folder)
    return match_directory(names, lambda json_name: read_title(base / json_name))


# --- matching report ----------------------------------------------------------------------


@dataclass
class MatchReport:
    """Matched, unmatched and unused counts over a whole Takeout scan (PRD §8.3)."""

    matched: int = 0
    unmatched_images: list[str] = field(default_factory=list)  # relative paths
    unused_sidecars: list[str] = field(default_factory=list)  # relative paths

    def add(self, relative_dir: str, result: DirectoryMatch) -> None:
        """Add one folder's result; ``relative_dir`` is "" for the source root."""
        prefix = f"{relative_dir}/" if relative_dir else ""
        self.matched += result.matched
        self.unmatched_images += [prefix + n for n in result.unmatched_images]
        self.unused_sidecars += [prefix + n for n in result.unused_sidecars]

    @classmethod
    def from_directories(cls, results: Iterable[tuple[str, DirectoryMatch]]) -> MatchReport:
        report = cls()
        for relative_dir, result in results:
            report.add(relative_dir, result)
        return report


def log_match_report(report: MatchReport, logger: logging.Logger = log, sample: int = 10) -> None:
    """Log the counts, then up to ``sample`` names each of unmatched images and unused sidecars."""
    logger.info(
        "Takeout sidecars: %d matched, %d images without a sidecar, %d unused sidecars",
        report.matched,
        len(report.unmatched_images),
        len(report.unused_sidecars),
    )
    for label, items in (
        ("Images without a sidecar", report.unmatched_images),
        ("Unused sidecars", report.unused_sidecars),
    ):
        if items:
            more = f" (and {len(items) - sample} more)" if len(items) > sample else ""
            logger.info("%s: %s%s", label, ", ".join(sorted(items)[:sample]), more)


# --- detection ----------------------------------------------------------------------------


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
