"""Pure planning for Takeout cleanup (PRD §8.10, docs/takeout-cleanup.md).

Decides which copy of each photo is kept and where it goes in the output folder, with Windows-safe,
clash-free, length-limited names. No Qt, no SQL and no file I/O: everything here takes plain values
and returns plain values, and the result never depends on the order of the input.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from imagory.cleanup.types import Candidate, Plan, PlannedCopy, Rename
from imagory.takeout import EDITED_SUFFIXES

UNKNOWN_DATE_FOLDER = "Unknown date"
JSON_FOLDER = ".imagory/takeout-json"
MAX_PATH_CHARS = 250  # full output path, PRD §8.10
SPACE_MARGIN_PERCENT = 5  # thumbnails and database
UNNAMED = "unnamed"

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_TITLE_EXT = re.compile(r"^(.*?)(\.[A-Za-z0-9]{1,5})$", re.DOTALL)
_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})")
_EXT_ALIASES = {".jpeg": ".jpg"}


def _ident(c: Candidate) -> tuple[bool, str, int]:
    """Identity of a candidate. ``key`` alone may repeat across file and extra-file rows."""
    return (c.in_output, c.kind, c.key)


# --- kept copy -------------------------------------------------------------------------------


def _rank(c: Candidate) -> tuple:
    """Sort key for the kept copy. The plan's "then a non-album folder" step is covered by
    ``album_folder is None``: a file in an album folder or Archive always has a folder name."""
    return (
        not c.in_output,
        c.album_folder is not None,
        len(c.relative_path),
        c.relative_path,
        c.kind,
        c.key,
    )


def choose_kept(candidates: Iterable[Candidate]) -> dict[str, Candidate]:
    """One kept copy per content hash: in the output already, then a year folder, then the shortest
    path, then path order (kind and key last, so ties never depend on input order)."""
    kept: dict[str, Candidate] = {}
    for c in candidates:
        best = kept.get(c.content_hash)
        if best is None or _rank(c) < _rank(best):
            kept[c.content_hash] = c
    return kept


# --- folder and names ------------------------------------------------------------------------


def target_folder(taken_at_local: str | None) -> str:
    """``YYYY/MM`` from a local ISO date, or ``Unknown date`` when there is none."""
    if taken_at_local:
        m = _DATE_PREFIX.match(taken_at_local)
        if m and int(m.group(1)) >= 1 and 1 <= int(m.group(2)) <= 12:
            return f"{m.group(1)}/{m.group(2)}"
    return UNKNOWN_DATE_FOLDER


def _clean_stem(stem: str) -> str:
    """Invalid characters to ``_``, trailing dots and spaces dropped, reserved names escaped.
    Empty means nothing usable was left."""
    stem = _INVALID_CHARS.sub("_", stem).rstrip(" .")
    if stem.split(".")[0].rstrip().upper() in _RESERVED:
        head, dot, rest = stem.partition(".")
        stem = f"{head}_{dot}{rest}"
    return stem


def _clean_ext(ext: str) -> str:
    ext = _INVALID_CHARS.sub("_", ext).rstrip(" .")
    return ext if ext != "." else ""


def sanitize_name(name: str) -> str:
    """Make a file name valid on Windows: invalid and control characters become ``_``, trailing
    dots and spaces go, reserved names (``CON`` becomes ``CON_``) are escaped, and an empty stem
    becomes ``unnamed``."""
    stem, ext = os.path.splitext(_INVALID_CHARS.sub("_", name))
    return (_clean_stem(stem) or UNNAMED) + _clean_ext(ext)


@dataclass(frozen=True)
class _Name:
    raw: str  # before sanitizing (the title itself when it was unusable)
    final: str  # sanitized
    title_ext_changed: bool
    title: str | None


def _same_ext(a: str, b: str) -> bool:
    a, b = a.casefold(), b.casefold()
    return _EXT_ALIASES.get(a, a) == _EXT_ALIASES.get(b, b)


def _edited_suffix(disk_stem: str) -> str:
    folded = disk_stem.casefold()
    for suffix in EDITED_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            return suffix
    return ""


def _disk_name(disk_stem: str, disk_ext: str) -> str:
    return (_clean_stem(disk_stem) or UNNAMED) + _clean_ext(disk_ext)


def _name_parts(c: Candidate) -> _Name:
    disk = c.relative_path.rpartition("/")[2]
    disk_stem, disk_ext = os.path.splitext(disk)
    title = c.title.strip() if c.title else ""
    if not title:
        return _Name(disk, _disk_name(disk_stem, disk_ext), False, None)
    m = _TITLE_EXT.match(title)
    if m and any(ch.isalpha() for ch in m.group(2)):
        t_stem, t_ext = m.group(1), m.group(2)
    else:
        t_stem, t_ext = title, ""
    if not _clean_stem(t_stem):
        # Nothing usable in the title: the on-disk name is used.
        return _Name(title, _disk_name(disk_stem, disk_ext), False, title)
    if t_ext and _same_ext(t_ext, disk_ext):
        ext, changed = t_ext, False
    else:
        ext, changed = disk_ext, bool(t_ext or disk_ext)
    suffix = _edited_suffix(disk_stem)
    if suffix and not t_stem.casefold().endswith(suffix):
        t_stem += suffix
    raw = t_stem + ext
    return _Name(raw, sanitize_name(raw), changed, title)


def name_for(candidate: Candidate) -> str:
    """The output file name: the sidecar title (with the file's real extension when the title's
    differs, and an ``-edited`` suffix kept), else the on-disk name. Always sanitized."""
    return _name_parts(candidate).final


# --- clashes and length ----------------------------------------------------------------------


def _join(folder: str, stem: str, ext: str) -> str:
    return f"{folder}/{stem}{ext}" if folder else f"{stem}{ext}"


def _fit_stem(output_root_len: int, folder: str, stem: str, tail_len: int) -> str:
    """Shorten ``stem`` so root, folder, stem and ``tail_len`` more characters fit the limit."""
    prefix = len(folder) + 1 if folder else 0
    budget = MAX_PATH_CHARS - output_root_len - 1 - prefix - tail_len
    if len(stem) <= budget:
        return stem
    return stem[: max(budget, 1)].rstrip(" .") or "_"


def shorten_for_max_path(output_root_len: int, rel: str) -> str:
    """Shorten the stem of ``rel`` (folder and extension kept) so the full path, with an output
    root of ``output_root_len`` characters and a separator, is at most 250 characters."""
    folder, _, name = rel.rpartition("/")
    stem, ext = os.path.splitext(name)
    return _join(folder, _fit_stem(output_root_len, folder, stem, len(ext)), ext)


def _first_free(taken: Collection[str], folder: str, stem: str, ext: str, root_len: int) -> str:
    for n in itertools.count():
        suffix = f" ({n})" if n else ""
        fitted = _fit_stem(root_len, folder, stem, len(ext) + len(suffix))
        path = _join(folder, fitted + suffix, ext)
        if path.casefold() not in taken:
            return path
    raise AssertionError("unreachable")


def _claim_unit(
    taken: set[str], folder: str, stem: str, exts: Sequence[str], root_len: int
) -> tuple[str, list[str]]:
    """Find the smallest ``n`` for which every file of a unit (a photo and its companions share
    one stem) is free, claim the paths in ``taken`` (casefolded), and return the fitted stem
    (before the ` (n)` suffix) and the paths."""
    tail = max((len(e) for e in exts), default=0)
    for n in itertools.count():
        suffix = f" ({n})" if n else ""
        fitted = _fit_stem(root_len, folder, stem, tail + len(suffix))
        paths = [_join(folder, fitted + suffix, e) for e in exts]
        if all(p.casefold() not in taken for p in paths):
            break
    out: list[str] = []
    for ext, path in zip(exts, paths, strict=True):
        if path.casefold() in taken:  # two files of the unit differ only in case
            path = _first_free(taken, folder, fitted + suffix, ext, root_len)
        taken.add(path.casefold())
        out.append(path)
    return fitted, out


def resolve_clashes(
    wanted: Sequence[str], taken: Collection[str] = (), output_root_len: int = 0
) -> list[str]:
    """Give each wanted output-relative path, in order, a free path. Names are compared with
    casefold against ``taken`` (existing output paths) and the earlier results; a clash gets
    ` (1)`, ` (2)`, ... before the extension, with the stem shortened to stay within the 250
    character limit. ``taken`` is not modified."""
    seen = {t.casefold() for t in taken}
    result = []
    for rel in wanted:
        folder, _, name = rel.rpartition("/")
        stem, ext = os.path.splitext(name)
        path = _first_free(seen, folder, stem, ext, output_root_len)
        seen.add(path.casefold())
        result.append(path)
    return result


# --- companions ------------------------------------------------------------------------------


def _is_companion_like(c: Candidate) -> bool:
    return not c.has_sidecar and c.taken_at_local is None


def _leader_by_companion(media: Iterable[Candidate]) -> dict[tuple, Candidate]:
    """Map the identity of each companion (same input folder and stem as a photo, no sidecar, no
    date) to that photo. A group with no dated or sidecar file uses its ``photo`` files."""
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for c in media:
        if c.in_output:
            continue
        folder, _, name = c.relative_path.rpartition("/")
        key = (folder.casefold(), os.path.splitext(name)[0].casefold())
        groups.setdefault(key, []).append(c)
    result: dict[tuple, Candidate] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        leaders = [c for c in members if not _is_companion_like(c)]
        if not leaders:
            leaders = [c for c in members if c.kind == "photo"]
        primary = min(
            leaders,
            key=lambda c: (
                c.kind != "photo",
                os.path.splitext(c.relative_path)[1].casefold(),
                c.relative_path,
                c.key,
            ),
            default=None,
        )
        if primary is None:
            continue
        leader_ids = {_ident(c) for c in leaders}
        for c in members:
            if _ident(c) not in leader_ids and _is_companion_like(c):
                result[_ident(c)] = primary
    return result


def _companion_ext(c: Candidate) -> str:
    return _clean_ext(os.path.splitext(c.relative_path.rpartition("/")[2])[1])


# --- the plan --------------------------------------------------------------------------------


def build_plan(
    candidates: Iterable[Candidate],
    existing_output_paths: Collection[str],
    output_root_len: int,
) -> Plan:
    """Plan every copy. ``existing_output_paths`` are the output-relative paths already taken
    (including ``.imagory/takeout-json/...`` ones from earlier runs); ``output_root_len`` is the
    length of the output folder path, for the 250 character limit. Photos and other files are
    deduplicated by hash (``Plan.kept_key_by_hash``); JSON files are not. Clashes are resolved in
    kept-copy order (the ranking of ``choose_kept``), so input order never matters."""
    cands = sorted(candidates, key=lambda c: (c.relative_path, c.kind, c.key))
    media = [c for c in cands if c.kind != "json"]
    kept = choose_kept(media)
    taken = {p.casefold() for p in existing_output_paths}
    taken.update(c.relative_path.casefold() for c in cands if c.in_output)

    new_kept = sorted((c for c in kept.values() if not c.in_output), key=_rank)
    new_ids = {_ident(c) for c in new_kept}

    # A companion follows the kept copy of its photo, unless that copy is a companion itself.
    leader_of = _leader_by_companion(media)
    attached: dict[tuple, Candidate] = {}
    for c in new_kept:
        leader = leader_of.get(_ident(c))
        if leader is not None:
            attached[_ident(c)] = kept[leader.content_hash]
    attached = {
        i: lead
        for i, lead in attached.items()
        if _ident(lead) != i and _ident(lead) not in attached
    }
    companions_of: dict[tuple, list[Candidate]] = {}
    for c in new_kept:
        if _ident(c) in attached:
            companions_of.setdefault(_ident(attached[_ident(c)]), []).append(c)
    output_path_of = {_ident(c): c.relative_path for c in kept.values() if c.in_output}

    copies: list[PlannedCopy] = []
    renames: list[Rename] = []

    def claim(folder: str, stem: str, files: list[tuple[Candidate, str, _Name | None]]) -> None:
        fitted, paths = _claim_unit(
            taken, folder, stem, [ext for _, ext, _ in files], output_root_len
        )
        for (cand, ext, parts), path in zip(files, paths, strict=True):
            copies.append(PlannedCopy(cand.key, cand.content_hash, path))
            if parts is not None and parts.title_ext_changed and parts.title:
                renames.append(Rename(_join(folder, parts.title, ""), path, "title_extension"))
            if parts is not None and parts.final != parts.raw:
                renames.append(Rename(_join(folder, parts.raw, ""), path, "sanitized"))
            if fitted != stem:
                renames.append(Rename(_join(folder, stem, ext), path, "long_path"))
            if path != _join(folder, fitted, ext):
                renames.append(Rename(_join(folder, fitted, ext), path, "clash"))

    for lead in new_kept:
        if _ident(lead) in attached:
            continue
        parts = _name_parts(lead)
        stem, ext = os.path.splitext(parts.final)
        files: list[tuple[Candidate, str, _Name | None]] = [(lead, ext, parts)]
        files += [
            (c, _companion_ext(c), None)
            for c in sorted(
                companions_of.get(_ident(lead), []), key=lambda c: (c.relative_path, c.key)
            )
        ]
        claim(target_folder(lead.taken_at_local), stem, files)

    # Companions of a photo already in the output follow its existing folder and stem.
    for lead_id, members in companions_of.items():
        if lead_id in new_ids:
            continue
        folder, _, name = output_path_of[lead_id].rpartition("/")
        members.sort(key=lambda c: (c.relative_path, c.key))
        claim(folder, os.path.splitext(name)[0], [(c, _companion_ext(c), None) for c in members])

    for c in (c for c in cands if c.kind == "json" and not c.in_output):
        wanted = f"{JSON_FOLDER}/{c.relative_path}"
        short = shorten_for_max_path(output_root_len, wanted)
        if short != wanted:
            renames.append(Rename(wanted, short, "long_path"))
        final = resolve_clashes([short], taken, output_root_len)[0]
        taken.add(final.casefold())
        if final != short:
            renames.append(Rename(short, final, "clash"))
        copies.append(PlannedCopy(c.key, c.content_hash, final))

    copies.sort(key=lambda p: (p.target_relative_path.casefold(), p.target_relative_path, p.key))
    return Plan(
        copies=tuple(copies),
        kept_key_by_hash={h: c.key for h, c in sorted(kept.items())},
        renames=tuple(renames),
    )


# --- space and times -------------------------------------------------------------------------


def estimate_space(plan: Plan, candidates: Iterable[Candidate]) -> int:
    """Bytes the plan will write plus 5% for thumbnails and the database, rounded up. A planned
    copy is matched to its candidate by ``(key, content_hash)``."""
    size_by_key = {(c.key, c.content_hash): c.size for c in candidates}
    total = sum(size_by_key[p.key, p.content_hash] for p in plan.copies)
    return -(-total * (100 + SPACE_MARGIN_PERCENT) // 100)


def taken_mtime_ns(taken_at_local: str, tz: tzinfo | None = None) -> int:
    """Unix nanoseconds for a local wall-clock ISO time read in ``tz`` (None: the computer's
    zone, as ``dates.timestamp_to_local`` does)."""
    moment = datetime.fromisoformat(taken_at_local)
    aware = moment.replace(tzinfo=tz) if tz is not None else moment.astimezone()
    delta = aware - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
