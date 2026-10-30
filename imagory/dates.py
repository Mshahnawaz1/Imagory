"""Choosing a file's best date and a photo's date across copies (PRD §8.4).

Pure functions with no Qt, database or file access, so they run anywhere (workers,
the scan coordinator, tests). "Now" and the computer's time zone are injectable.

Conventions (PRD §9): ``taken_at_local`` is ISO 8601 with no offset
(``2019-01-31T23:59:59``); ``taken_at_utc`` is ISO 8601 ending in ``Z`` or None.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Literal

from imagory.metadata import mtime_to_local, parse_exif_datetime

DateSource = Literal["takeout_json", "exif", "filename", "mtime"]
SourceType = Literal["plain", "takeout"]

# Lower rank wins when choosing a photo's date across its files.
SOURCE_RANK: dict[str, int] = {"takeout_json": 0, "exif": 1, "filename": 2, "mtime": 3}

# Filename dates before this are rejected as implausible (PRD §8.4).
MIN_FILENAME_YEAR = 1990

# EXIF wall-clock and Takeout JSON UTC closer than this are treated as a time zone offset.
TZ_OFFSET_WINDOW = timedelta(hours=24)

# Each pattern is matched at the start of the filename, case-insensitively, and must
# capture year, month, day and optionally hour, minute, second. The trailing
# ``(?!\d)`` stops a longer digit run from matching a shorter pattern.
_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # IMG_20190131_235959, PXL_20190131_235959123 (milliseconds ignored)
    re.compile(r"(?:IMG|PXL)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:\d{3})?(?!\d)", re.I),
    # IMG-20190131-WA0001 (WhatsApp, date only)
    re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+", re.I),
    # Screenshot_20190131-235959
    re.compile(r"Screenshot_(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?!\d)", re.I),
    # 2019-01-31 23.59.59
    re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})(?!\d)"),
)

_EXIF_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")


@dataclass(frozen=True, slots=True)
class DateResult:
    """One chosen date and where it came from."""

    taken_at_local: str
    taken_at_utc: str | None
    date_source: DateSource


def parse_filename_date(name: str, now: datetime | None = None) -> str | None:
    """Parse a recognised filename pattern into local ISO 8601, or None.

    ``name`` may include the extension. Date-only patterns (WhatsApp) give midnight.
    Invalid calendar values, years before 1990 and dates after ``now`` (naive local
    time, default the current time) are rejected.
    """
    for pattern in _FILENAME_PATTERNS:
        match = pattern.match(name)
        if match is None:
            continue
        parts = [int(p) for p in match.groups()]
        try:
            parsed = datetime(*parts)
        except ValueError:
            return None
        if parsed.year < MIN_FILENAME_YEAR:
            return None
        if now is None:
            now = datetime.now()
        if parsed > now:
            return None
        return parsed.isoformat(timespec="seconds")
    return None


def parse_exif_offset(value: object) -> timezone | None:
    """Parse an EXIF ``OffsetTimeOriginal`` value (``+02:00``) into a fixed time zone."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not isinstance(value, str):
        return None
    match = _EXIF_OFFSET.fullmatch(value.strip().rstrip("\x00").strip())
    if match is None:
        return None
    sign, hours, minutes = match.groups()
    if int(minutes) >= 60:
        return None
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if delta > timedelta(hours=14):
        return None
    return timezone(-delta if sign == "-" else delta)


def to_utc_iso(moment: datetime) -> str:
    """Format an aware datetime as ISO 8601 UTC ending in ``Z``."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def exif_to_utc(exif_local: str, offset: object) -> str | None:
    """UTC ISO for an EXIF wall-clock time when ``OffsetTimeOriginal`` is usable."""
    zone = parse_exif_offset(offset)
    if zone is None:
        return None
    return to_utc_iso(datetime.fromisoformat(exif_local).replace(tzinfo=zone))


def timestamp_to_local(seconds: float, tz: tzinfo | None = None) -> str:
    """Wall-clock ISO for a Unix timestamp in ``tz`` (default: the computer's zone)."""
    moment = datetime.fromtimestamp(seconds, UTC).astimezone(tz)
    return moment.replace(tzinfo=None).isoformat(timespec="seconds")


def resolve_date(
    source_type: SourceType,
    *,
    mtime_ns: int,
    takeout_timestamp: int | None = None,
    exif_datetime: object = None,
    exif_offset: object = None,
    filename: str | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> DateResult:
    """Pick a file's best date by the per-source-type priority in PRD §8.4.

    - ``takeout``: Takeout JSON, then EXIF, then filename, then modified time.
    - ``plain``: EXIF, then filename, then modified time (``takeout_timestamp`` ignored).

    ``takeout_timestamp`` is the JSON ``photoTakenTime.timestamp`` (Unix seconds, UTC).
    ``exif_datetime`` is the raw or ISO ``DateTimeOriginal``; ``exif_offset`` the raw
    ``OffsetTimeOriginal``. ``tz`` is the computer's time zone (None: system zone),
    used for JSON-to-local and modified-time conversion. ``now`` bounds filename dates.
    """
    if source_type not in ("plain", "takeout"):
        raise ValueError(f"unknown source type: {source_type!r}")

    exif_local = _exif_local(exif_datetime)

    if source_type == "takeout" and takeout_timestamp is not None:
        json_utc = datetime.fromtimestamp(takeout_timestamp, UTC)
        utc_iso = to_utc_iso(json_utc)
        if exif_local is not None:
            # EXIF is wall-clock with no zone: compare it to the JSON moment as if UTC.
            exif_naive = datetime.fromisoformat(exif_local)
            if abs(exif_naive - json_utc.replace(tzinfo=None)) < TZ_OFFSET_WINDOW:
                return DateResult(exif_local, utc_iso, "takeout_json")
        return DateResult(timestamp_to_local(takeout_timestamp, tz), utc_iso, "takeout_json")

    if exif_local is not None:
        return DateResult(exif_local, exif_to_utc(exif_local, exif_offset), "exif")

    if filename is not None:
        from_name = parse_filename_date(filename, now)
        if from_name is not None:
            return DateResult(from_name, None, "filename")

    if tz is None:
        local = mtime_to_local(mtime_ns)
    else:
        local = timestamp_to_local(mtime_ns // 1_000_000_000, tz)
    return DateResult(local, None, "mtime")


def best_photo_date(candidates: Iterable[DateResult | None]) -> DateResult | None:
    """Choose a photo's date from its files' dates (PRD §8.4, "across copies").

    The best-ranked source wins (takeout_json, exif, filename, mtime). Ties break,
    in order, on: a known UTC time first, then the earliest ``taken_at_local``, then the
    earliest ``taken_at_utc``. The result does not depend on the input order.
    None entries (files with no date) are skipped; returns None if nothing is left.
    """
    dated = [c for c in candidates if c is not None]
    if not dated:
        return None
    return min(
        dated,
        key=lambda c: (
            SOURCE_RANK[c.date_source],
            c.taken_at_utc is None,
            c.taken_at_local,
            c.taken_at_utc or "",
        ),
    )


def _exif_local(value: object) -> str | None:
    """Accept an already-parsed ISO value or a raw EXIF value."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            pass
    return parse_exif_datetime(value)
