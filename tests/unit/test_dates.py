from datetime import UTC, datetime, timedelta, timezone

import pytest

from imagory import dates, metadata
from imagory.dates import DateResult, best_photo_date, parse_filename_date, resolve_date

NOW = datetime(2026, 10, 31, 12, 0, 0)
BERLIN_WINTER = timezone(timedelta(hours=1))
NEW_YORK_WINTER = timezone(timedelta(hours=-5))

# 2019-01-31T22:59:59Z, i.e. 23:59:59 in UTC+1.
TS = int(datetime(2019, 1, 31, 22, 59, 59, tzinfo=UTC).timestamp())
MTIME_NS = int(datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC).timestamp()) * 1_000_000_000


# --- filename patterns -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_20190131_235959.jpg", "2019-01-31T23:59:59"),
        ("img_20190131_235959_1.JPG", "2019-01-31T23:59:59"),
        ("PXL_20190131_235959123.jpg", "2019-01-31T23:59:59"),
        ("PXL_20190131_235959123.MP.jpg", "2019-01-31T23:59:59"),
        ("VID_20190131_235959.mp4", "2019-01-31T23:59:59"),
        ("vid_20190131_235959.MOV", "2019-01-31T23:59:59"),
        ("PXL_20190131_235959123.mp4", "2019-01-31T23:59:59"),
        ("IMG-20190131-WA0001.jpg", "2019-01-31T00:00:00"),
        ("IMG-20190131-WA12345.jpeg", "2019-01-31T00:00:00"),
        ("Screenshot_20190131-235959.png", "2019-01-31T23:59:59"),
        ("Screenshot_20190131-235959_Chrome.png", "2019-01-31T23:59:59"),
        ("2019-01-31 23.59.59.jpg", "2019-01-31T23:59:59"),
        ("2019-01-31 23.59.59-1.heic", "2019-01-31T23:59:59"),
    ],
)
def test_filename_patterns(name, expected):
    assert parse_filename_date(name, NOW) == expected


@pytest.mark.parametrize(
    "name",
    [
        "DSC_0001.jpg",
        "holiday.jpg",
        "IMG_2019013_235959.jpg",  # too few date digits
        "IMG_20190131_2359591.jpg",  # 7 time digits is neither pattern
        "IMG_20190131.jpg",
        "IMG-20190131-0001.jpg",  # WhatsApp needs the WA marker
        "Screenshot_20190131_235959.png",  # wrong separator
        "2019-01-31 23-59-59.jpg",
        "copy of IMG_20190131_235959.jpg",  # patterns match at the start only
    ],
)
def test_filename_unrecognised(name):
    assert parse_filename_date(name, NOW) is None


@pytest.mark.parametrize(
    "name",
    [
        "IMG_20190230_120000.jpg",  # 30 February
        "IMG_20191301_120000.jpg",  # month 13
        "IMG_20190131_250000.jpg",  # hour 25
        "IMG_20190131_236000.jpg",  # minute 60
        "IMG-20190000-WA0001.jpg",  # day 0
        "2019-02-29 10.00.00.jpg",  # not a leap year
    ],
)
def test_filename_invalid_calendar_values(name):
    assert parse_filename_date(name, NOW) is None


def test_filename_leap_day_is_valid():
    assert parse_filename_date("IMG_20200229_120000.jpg", NOW) == "2020-02-29T12:00:00"


def test_filename_rejects_before_1990():
    assert parse_filename_date("IMG_19891231_235959.jpg", NOW) is None
    assert parse_filename_date("IMG-19890101-WA0001.jpg", NOW) is None
    assert parse_filename_date("IMG_19900101_000000.jpg", NOW) == "1990-01-01T00:00:00"


def test_filename_rejects_future():
    assert parse_filename_date("IMG_20261031_120001.jpg", NOW) is None
    assert parse_filename_date("IMG_20261031_120000.jpg", NOW) == "2026-10-31T12:00:00"
    assert parse_filename_date("IMG-20261101-WA0001.jpg", NOW) is None
    assert parse_filename_date("IMG-20261031-WA0001.jpg", NOW) == "2026-10-31T00:00:00"


def test_filename_default_now_rejects_far_future():
    assert parse_filename_date("IMG_29990101_000000.jpg") is None
    assert parse_filename_date("IMG_20190131_235959.jpg") == "2019-01-31T23:59:59"


# --- EXIF offset -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "hours"),
    [("+02:00", 2), ("-05:00", -5), (b"+05:30\x00", 5.5), (" +00:00 ", 0), ("+14:00", 14)],
)
def test_parse_exif_offset(value, hours):
    zone = dates.parse_exif_offset(value)
    assert zone is not None
    assert zone.utcoffset(None) == timedelta(hours=hours)


@pytest.mark.parametrize("value", [None, "", "   :  ", "+2:00", "02:00", "+15:00", "+01:60", 3])
def test_parse_exif_offset_rejects(value):
    assert dates.parse_exif_offset(value) is None


# --- plain source priority ---------------------------------------------------


def test_plain_prefers_exif_over_filename_and_mtime():
    result = resolve_date(
        "plain",
        mtime_ns=MTIME_NS,
        exif_datetime="2018:06:01 10:00:00",
        filename="IMG_20190131_235959.jpg",
        now=NOW,
    )
    assert result == DateResult("2018-06-01T10:00:00", None, "exif")


def test_plain_accepts_parsed_exif_iso():
    result = resolve_date("plain", mtime_ns=MTIME_NS, exif_datetime="2018-06-01T10:00:00")
    assert result == DateResult("2018-06-01T10:00:00", None, "exif")


def test_plain_ignores_takeout_timestamp():
    result = resolve_date("plain", mtime_ns=MTIME_NS, takeout_timestamp=TS, tz=UTC)
    assert result.date_source == "mtime"


def test_plain_falls_back_to_filename():
    result = resolve_date(
        "plain",
        mtime_ns=MTIME_NS,
        exif_datetime="0000:00:00 00:00:00",  # blank EXIF date counts as missing
        filename="PXL_20190131_235959123.jpg",
        now=NOW,
    )
    assert result == DateResult("2019-01-31T23:59:59", None, "filename")


def test_plain_falls_back_to_mtime_in_given_zone():
    result = resolve_date("plain", mtime_ns=MTIME_NS, filename="holiday.jpg", tz=BERLIN_WINTER)
    assert result == DateResult("2024-05-06T08:08:09", None, "mtime")


def test_mtime_without_zone_matches_metadata_helper():
    result = resolve_date("plain", mtime_ns=MTIME_NS)
    assert result == DateResult(metadata.mtime_to_local(MTIME_NS), None, "mtime")


def test_rejected_filename_date_falls_back_to_mtime():
    result = resolve_date("plain", mtime_ns=MTIME_NS, filename="IMG_19800101_000000.jpg", tz=UTC)
    assert result.date_source == "mtime"


def test_unknown_source_type():
    with pytest.raises(ValueError):
        resolve_date("zip", mtime_ns=0)  # type: ignore[arg-type]


# --- EXIF time zones ---------------------------------------------------------


def test_exif_offset_gives_utc():
    result = resolve_date(
        "plain", mtime_ns=0, exif_datetime="2019:01:31 23:59:59", exif_offset="+01:00"
    )
    assert result == DateResult("2019-01-31T23:59:59", "2019-01-31T22:59:59Z", "exif")


def test_exif_negative_offset_crosses_midnight():
    result = resolve_date(
        "plain", mtime_ns=0, exif_datetime="2019:01:31 23:00:00", exif_offset=b"-05:00\x00"
    )
    assert result == DateResult("2019-01-31T23:00:00", "2019-02-01T04:00:00Z", "exif")


def test_exif_bad_offset_leaves_utc_unknown():
    result = resolve_date(
        "plain", mtime_ns=0, exif_datetime="2019:01:31 23:59:59", exif_offset="   :  "
    )
    assert result == DateResult("2019-01-31T23:59:59", None, "exif")


# --- Takeout priority and the JSON/EXIF rule ---------------------------------


def test_takeout_json_only_converts_with_computer_zone():
    result = resolve_date("takeout", mtime_ns=MTIME_NS, takeout_timestamp=TS, tz=NEW_YORK_WINTER)
    assert result == DateResult("2019-01-31T17:59:59", "2019-01-31T22:59:59Z", "takeout_json")


def test_takeout_json_beats_filename_and_mtime():
    result = resolve_date(
        "takeout",
        mtime_ns=MTIME_NS,
        takeout_timestamp=TS,
        filename="IMG_20100101_000000.jpg",
        now=NOW,
        tz=UTC,
    )
    assert result == DateResult("2019-01-31T22:59:59", "2019-01-31T22:59:59Z", "takeout_json")


def test_takeout_exif_within_24h_keeps_exif_wall_clock_and_json_utc():
    # Photo taken at 23:59:59 in UTC+1; the computer is in New York.
    result = resolve_date(
        "takeout",
        mtime_ns=MTIME_NS,
        takeout_timestamp=TS,
        exif_datetime="2019:01:31 23:59:59",
        tz=NEW_YORK_WINTER,
    )
    assert result == DateResult("2019-01-31T23:59:59", "2019-01-31T22:59:59Z", "takeout_json")


def test_takeout_exif_just_under_24h_still_counts_as_offset():
    exif = datetime(2019, 1, 31, 22, 59, 59) + timedelta(hours=23, minutes=59, seconds=59)
    result = resolve_date(
        "takeout",
        mtime_ns=0,
        takeout_timestamp=TS,
        exif_datetime=exif.isoformat(),
        tz=UTC,
    )
    assert result.taken_at_local == exif.isoformat()


@pytest.mark.parametrize("hours", [24, -24, 24 * 400])
def test_takeout_exif_24h_or_more_apart_uses_json(hours):
    exif = datetime(2019, 1, 31, 22, 59, 59) + timedelta(hours=hours)
    result = resolve_date(
        "takeout",
        mtime_ns=MTIME_NS,
        takeout_timestamp=TS,
        exif_datetime=exif.isoformat(),
        tz=NEW_YORK_WINTER,
    )
    assert result == DateResult("2019-01-31T17:59:59", "2019-01-31T22:59:59Z", "takeout_json")


def test_takeout_json_utc_wins_over_exif_offset():
    result = resolve_date(
        "takeout",
        mtime_ns=0,
        takeout_timestamp=TS,
        exif_datetime="2019:01:31 23:59:59",
        exif_offset="+05:00",  # disagrees with the JSON; the JSON moment is kept
        tz=UTC,
    )
    assert result == DateResult("2019-01-31T23:59:59", "2019-01-31T22:59:59Z", "takeout_json")


def test_takeout_without_json_uses_plain_order():
    result = resolve_date(
        "takeout",
        mtime_ns=MTIME_NS,
        exif_datetime="2018:06:01 10:00:00",
        exif_offset="+02:00",
        filename="IMG_20190131_235959.jpg",
        now=NOW,
    )
    assert result == DateResult("2018-06-01T10:00:00", "2018-06-01T08:00:00Z", "exif")
    result = resolve_date("takeout", mtime_ns=MTIME_NS, filename="IMG_20190131_235959.jpg", now=NOW)
    assert result.date_source == "filename"
    assert resolve_date("takeout", mtime_ns=MTIME_NS, tz=UTC).date_source == "mtime"


# --- photo date across copies -------------------------------------------------


def test_best_photo_date_prefers_ranked_source():
    plain = DateResult("2019-01-31T23:59:59", None, "exif")
    takeout = DateResult("2019-01-31T23:59:58", "2019-01-31T22:59:58Z", "takeout_json")
    named = DateResult("2019-01-31T23:59:59", None, "filename")
    mtime = DateResult("2001-01-01T00:00:00", None, "mtime")
    assert best_photo_date([mtime, named, plain, takeout]) == takeout
    assert best_photo_date([mtime, named, plain]) == plain
    assert best_photo_date([mtime, named]) == named


def test_best_photo_date_tie_break_is_order_independent():
    with_utc = DateResult("2019-06-01T12:00:00", "2019-06-01T10:00:00Z", "exif")
    earlier = DateResult("2019-01-01T00:00:00", None, "exif")
    later = DateResult("2019-02-01T00:00:00", None, "exif")
    candidates = [later, earlier, with_utc]
    assert best_photo_date(candidates) == with_utc  # known UTC beats unknown
    assert best_photo_date(reversed(candidates)) == with_utc
    assert best_photo_date([later, earlier]) == earlier  # then earliest local time
    assert best_photo_date([earlier, later]) == earlier
    a = DateResult("2019-01-01T00:00:00", "2019-01-01T05:00:00Z", "exif")
    b = DateResult("2019-01-01T00:00:00", "2019-01-01T03:00:00Z", "exif")
    assert best_photo_date([a, b]) == best_photo_date([b, a]) == b  # then earliest UTC


def test_best_photo_date_skips_missing():
    only = DateResult("2019-01-01T00:00:00", None, "mtime")
    assert best_photo_date([None, only, None]) == only
    assert best_photo_date([]) is None
    assert best_photo_date([None]) is None
