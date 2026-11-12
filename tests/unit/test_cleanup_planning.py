"""Planning functions for Takeout cleanup (imagory/cleanup/planning.py): pure, no files involved."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone

from imagory.cleanup.planning import (
    build_plan,
    choose_kept,
    estimate_space,
    name_for,
    resolve_clashes,
    sanitize_name,
    shorten_for_max_path,
    taken_mtime_ns,
    target_folder,
)
from imagory.cleanup.types import Candidate

DATE = "2019-01-31T23:59:59"


def cand(
    key: int,
    path: str,
    *,
    hash: str | None = None,
    kind: str = "photo",
    title: str | None = None,
    sidecar: bool | None = None,
    date: str | None = DATE,
    album: str | None = None,
    in_output: bool = False,
    size: int = 100,
) -> Candidate:
    return Candidate(
        key=key,
        kind=kind,  # type: ignore[arg-type]
        relative_path=path,
        content_hash=hash or f"h{key}",
        size=size,
        title=title,
        has_sidecar=title is not None if sidecar is None else sidecar,
        taken_at_local=date,
        album_folder=album,
        in_output=in_output,
    )


def targets(plan) -> dict[int, str]:
    return {p.key: p.target_relative_path for p in plan.copies}


def plan_of(cands, existing=(), root_len=10):
    return build_plan(cands, existing, root_len)


# --- choose_kept -----------------------------------------------------------------------------


def test_kept_prefers_output_then_year_folder_then_short_path():
    in_out = cand(1, "2019/01/a.jpg", hash="x", in_output=True)
    album = cand(2, "Album/a.jpg", hash="x", album="Album")
    year = cand(3, "Photos from 2019/very/long/a.jpg", hash="x")
    assert choose_kept([album, year, in_out])["x"].key == 1
    assert choose_kept([album, year])["x"].key == 3
    short_album = cand(4, "B/a.jpg", hash="y", album="B")
    long_album = cand(5, "Longer album/a.jpg", hash="y", album="Longer album")
    assert choose_kept([long_album, short_album])["y"].key == 4


def test_kept_path_order_breaks_ties():
    a = cand(1, "A/x.jpg", hash="x", album="A")
    b = cand(2, "B/x.jpg", hash="x", album="B")
    assert choose_kept([b, a])["x"].key == 1
    assert choose_kept([a, b])["x"].key == 1


# --- target_folder ---------------------------------------------------------------------------


def test_target_folder():
    assert target_folder("2019-01-31T23:59:59") == "2019/01"
    assert target_folder("2005-12-01T00:00:00") == "2005/12"
    assert target_folder(None) == "Unknown date"
    assert target_folder("") == "Unknown date"
    assert target_folder("0000-01-01T00:00:00") == "Unknown date"
    assert target_folder("2019-13-01T00:00:00") == "Unknown date"


# --- Expected names, one test per row ---------------------------------------------------------


def test_name_plain():
    assert name_for(cand(1, "Photos from 2019/IMG_1234.jpg", sidecar=False)) == "IMG_1234.jpg"
    assert name_for(cand(1, "x/IMG_1234.jpg", title="IMG_1234.jpg")) == "IMG_1234.jpg"


def test_name_clash_same_month_folder():
    a = cand(1, "Photos from 2019/IMG_1234.jpg", title="IMG_1234.jpg")
    b = cand(2, "Album/IMG_1234.jpg", title="IMG_1234.jpg", album="Album")
    plan = plan_of([b, a])
    assert targets(plan) == {1: "2019/01/IMG_1234.jpg", 2: "2019/01/IMG_1234 (1).jpg"}
    assert [(r.target, r.reason) for r in plan.renames] == [("2019/01/IMG_1234 (1).jpg", "clash")]
    assert plan.renames[0].original == "2019/01/IMG_1234.jpg"


def test_name_case_only_clash():
    # The kept-copy order puts the year folder first.
    a = cand(1, "Photos from 2019/img_1234.JPG", sidecar=False)
    b = cand(2, "Album/IMG_1234.jpg", sidecar=False, album="Album")
    plan = plan_of([b, a])
    assert targets(plan) == {1: "2019/01/img_1234.JPG", 2: "2019/01/IMG_1234 (1).jpg"}


def test_name_reserved():
    assert sanitize_name("CON.jpg") == "CON_.jpg"
    assert sanitize_name("con.jpg") == "con_.jpg"
    assert sanitize_name("COM3.tar.gz") == "COM3_.tar.gz"
    assert sanitize_name("NUL") == "NUL_"
    assert sanitize_name("CONSOLE.jpg") == "CONSOLE.jpg"
    assert name_for(cand(1, "x/a.jpg", title="CON.jpg")) == "CON_.jpg"


def test_name_invalid_characters():
    assert sanitize_name("a/b:c.jpg") == "a_b_c.jpg"
    assert sanitize_name('a\\b*c?d"e<f>g|h.jpg') == "a_b_c_d_e_f_g_h.jpg"
    assert sanitize_name("a\x00b\x1fc\n.jpg") == "a_b_c_.jpg"
    assert name_for(cand(1, "x/a.jpg", title="a/b:c.jpg")) == "a_b_c.jpg"
    plan = plan_of([cand(1, "x/a.jpg", title="a/b:c.jpg")])
    assert [(r.original, r.target, r.reason) for r in plan.renames] == [
        ("2019/01/a/b:c.jpg", "2019/01/a_b_c.jpg", "sanitized")
    ]


def test_name_trailing_dot_and_space():
    assert sanitize_name("trip. .jpg") == "trip.jpg"
    assert sanitize_name("trip.") == "trip"
    assert sanitize_name("trip .jpg") == "trip.jpg"
    assert name_for(cand(1, "x/a.jpg", title="trip. .jpg")) == "trip.jpg"


def test_name_nothing_usable():
    assert name_for(cand(1, "x/disk.jpg", title="...")) == "disk.jpg"
    assert name_for(cand(1, "x/disk.jpg", title="")) == "disk.jpg"
    assert name_for(cand(1, "x/disk.jpg", title="  ")) == "disk.jpg"
    assert name_for(cand(1, "x/... .jpg", title="...")) == "unnamed.jpg"
    assert name_for(cand(1, "x/...", sidecar=False)) == "unnamed"
    assert sanitize_name("....") == "unnamed"
    assert sanitize_name("") == "unnamed"


def test_name_title_extension_differs():
    c = cand(1, "x/disk.jpg", title="IMG_1.HEIC")
    assert name_for(c) == "IMG_1.jpg"
    plan = plan_of([c])
    assert [(r.original, r.target, r.reason) for r in plan.renames] == [
        ("2019/01/IMG_1.HEIC", "2019/01/IMG_1.jpg", "title_extension")
    ]


def test_name_title_extension_equal():
    c = cand(1, "x/disk.jpg", title="IMG_1.jpeg")
    assert name_for(c) == "IMG_1.jpeg"
    assert plan_of([c]).renames == ()
    assert name_for(cand(1, "x/disk.jpeg", title="IMG_1.JPG")) == "IMG_1.JPG"


def test_name_title_without_extension():
    assert name_for(cand(1, "x/disk.jpg", title="Holiday 2.5")) == "Holiday 2.5.jpg"


def test_name_edited_copy():
    c = cand(1, "x/IMG_1234-edited.jpg", title="IMG_1234.jpg")
    assert name_for(c) == "IMG_1234-edited.jpg"
    assert name_for(cand(1, "x/IMG_1-edited.jpg", title="IMG_1.HEIC")) == "IMG_1-edited.jpg"
    # A title that already carries the suffix isn't doubled.
    assert name_for(cand(1, "x/a-edited.jpg", title="a-edited.jpg")) == "a-edited.jpg"
    # No title: the on-disk name.
    assert name_for(cand(1, "x/IMG_1234-edited.jpg", sidecar=False)) == "IMG_1234-edited.jpg"


def pair(key: int, folder: str, stem: str = "IMG_1", date: str | None = DATE, album=None):
    heic = cand(key, f"{folder}/{stem}.HEIC", sidecar=True, date=date, album=album)
    mov = cand(key + 1, f"{folder}/{stem}.MOV", kind="other", sidecar=False, date=None, album=album)
    return heic, mov


def test_companion_follows_clashed_photo():
    other = cand(1, "Photos from 2019/IMG_1.HEIC", sidecar=True)
    heic, mov = pair(10, "Album", album="Album")
    plan = plan_of([mov, heic, other])
    assert targets(plan) == {
        1: "2019/01/IMG_1.HEIC",
        10: "2019/01/IMG_1 (1).HEIC",
        11: "2019/01/IMG_1 (1).MOV",
    }


def test_two_clashing_pairs_stay_paired():
    a1, a2 = pair(1, "Photos from 2019")
    b1, b2 = pair(10, "Album", album="Album")
    plan = plan_of([b2, a2, b1, a1])
    assert targets(plan) == {
        1: "2019/01/IMG_1.HEIC",
        2: "2019/01/IMG_1.MOV",
        10: "2019/01/IMG_1 (1).HEIC",
        11: "2019/01/IMG_1 (1).MOV",
    }


def test_companion_follows_leader_folder_and_title_stem():
    heic = cand(1, "Photos from 2019/IMG_9.HEIC", title="Holiday.HEIC", date="2018-06-01T10:00:00")
    mov = cand(2, "Photos from 2019/IMG_9.MOV", kind="other", sidecar=False, date=None)
    assert targets(plan_of([heic, mov])) == {1: "2018/06/Holiday.HEIC", 2: "2018/06/Holiday.MOV"}


def test_companion_not_paired_across_folders_or_with_sidecar():
    heic = cand(1, "A/IMG_1.HEIC", date="2018-06-01T10:00:00")
    far = cand(2, "B/IMG_1.MOV", kind="other", sidecar=False, date=None)
    own = cand(3, "A/IMG_1.MP4", kind="other", title="IMG_1.MP4", date="2017-03-01T10:00:00")
    t = targets(plan_of([heic, far, own]))
    assert t[2] == "Unknown date/IMG_1.MOV"
    assert t[3] == "2017/03/IMG_1.MP4"


def test_companion_follows_photo_already_in_output():
    out = cand(1, "2019/01/IMG_1 (1).HEIC", hash="hh", in_output=True)
    heic = cand(2, "Album/IMG_1.HEIC", hash="hh", album="Album")
    mov = cand(3, "Album/IMG_1.MOV", kind="other", sidecar=False, date=None, album="Album")
    plan = plan_of([out, heic, mov], existing=["2019/01/IMG_1 (1).HEIC"])
    assert targets(plan) == {3: "2019/01/IMG_1 (1).MOV"}


def test_both_undated_pair_stays_paired():
    heic = cand(1, "A/IMG_1.HEIC", sidecar=False, date=None)
    mov = cand(2, "A/IMG_1.MOV", kind="other", sidecar=False, date=None)
    taken_by_other = cand(3, "B/IMG_1.MOV", kind="other", sidecar=False, date=None)
    t = targets(plan_of([heic, mov, taken_by_other]))
    assert t[1].replace(".HEIC", "") == t[2].replace(".MOV", "")


def test_long_title_is_shortened():
    root_len = 30
    c = cand(1, "x/a.jpg", title="a" * 300 + ".jpg")
    plan = plan_of([c], root_len=root_len)
    path = plan.copies[0].target_relative_path
    assert root_len + 1 + len(path) <= 250
    assert path.startswith("2019/01/aaa") and path.endswith(".jpg")
    assert [r.reason for r in plan.renames] == ["long_path"]


# --- sanitize and shorten ---------------------------------------------------------------------


def test_sanitize_keeps_unicode_and_valid_names():
    assert sanitize_name("Ferien \U0001f600 Zürich.jpg") == "Ferien \U0001f600 Zürich.jpg"
    assert sanitize_name("IMG_1234.jpg") == "IMG_1234.jpg"


def test_shorten_for_max_path():
    rel = "2019/01/" + "x" * 300 + ".jpeg"
    short = shorten_for_max_path(40, rel)
    assert short.startswith("2019/01/x") and short.endswith(".jpeg")
    assert 40 + 1 + len(short) == 250
    assert shorten_for_max_path(40, "2019/01/a.jpg") == "2019/01/a.jpg"
    assert shorten_for_max_path(0, "a.jpg") == "a.jpg"


def test_shorten_never_leaves_empty_or_trailing_dot_stem():
    rel = "f/" + "a." * 200 + "x.jpg"
    short = shorten_for_max_path(0, rel)
    stem = short.rpartition("/")[2][: -len(".jpg")]
    assert stem and not stem.endswith((" ", "."))


# --- resolve_clashes ---------------------------------------------------------------------------


def test_resolve_clashes_planned_and_existing():
    out = resolve_clashes(["a/x.jpg", "a/X.JPG", "a/x.jpg", "b/x.jpg"], taken=["a/x (1).jpg"])
    assert out == ["a/x.jpg", "a/X (2).JPG", "a/x (3).jpg", "b/x.jpg"]


def test_resolve_clashes_first_is_kept_and_taken_untouched():
    taken = {"a/x.jpg"}
    assert resolve_clashes(["a/y.jpg", "a/y.jpg"], taken) == ["a/y.jpg", "a/y (1).jpg"]
    assert taken == {"a/x.jpg"}
    assert resolve_clashes(["a/x.jpg"], ["A/X.JPG"]) == ["a/x (1).jpg"]


def test_resolve_clashes_suffix_fits_length_limit():
    rel = "a/" + "x" * 300 + ".jpg"
    first, second = resolve_clashes([rel, rel], output_root_len=20)
    assert first != second
    assert 20 + 1 + len(first) <= 250 and 20 + 1 + len(second) <= 250
    assert second.endswith(" (1).jpg")


def test_existing_output_names_are_avoided():
    c = cand(1, "x/a.jpg", title="IMG.jpg")
    plan = plan_of([c], existing=["2019/01/img.JPG"])
    assert targets(plan) == {1: "2019/01/IMG (1).jpg"}


# --- build_plan ---------------------------------------------------------------------------------


def test_duplicates_are_planned_once_and_recorded_in_kept_map():
    year = cand(1, "Photos from 2019/a.jpg", hash="same")
    album = cand(2, "Trip/a.jpg", hash="same", album="Trip")
    plan = plan_of([album, year])
    assert targets(plan) == {1: "2019/01/a.jpg"}
    assert plan.kept_key_by_hash == {"same": 1}


def test_in_output_file_wins_and_is_not_copied():
    out = cand(7, "2019/01/a.jpg", hash="same", in_output=True)
    new = cand(2, "Photos from 2019/a.jpg", hash="same")
    plan = plan_of([new, out], existing=["2019/01/a.jpg"])
    assert plan.copies == ()
    assert plan.kept_key_by_hash == {"same": 7}


def test_undated_goes_to_unknown_date():
    plan = plan_of([cand(1, "x/a.jpg", sidecar=False, date=None)])
    assert targets(plan) == {1: "Unknown date/a.jpg"}


def test_json_files_not_deduplicated_and_go_to_takeout_json():
    a = cand(1, "Album/metadata.json", kind="json", hash="j", sidecar=False, date=None)
    b = cand(2, "Other/metadata.json", kind="json", hash="j", sidecar=False, date=None)
    plan = plan_of([b, a])
    assert targets(plan) == {
        1: ".imagory/takeout-json/Album/metadata.json",
        2: ".imagory/takeout-json/Other/metadata.json",
    }
    assert plan.kept_key_by_hash == {}


def test_json_clash_with_earlier_run():
    a = cand(1, "Album/metadata.json", kind="json", sidecar=False, date=None)
    plan = plan_of([a], existing=[".imagory/takeout-json/Album/metadata.json"])
    assert targets(plan) == {1: ".imagory/takeout-json/Album/metadata (1).json"}
    assert [r.reason for r in plan.renames] == ["clash"]


def test_keys_may_repeat_across_kinds():
    photo = cand(1, "x/a.jpg", hash="p")
    other = cand(1, "x/b.mp4", kind="other", hash="o", sidecar=False, date=None)
    plan = plan_of([photo, other])
    assert sorted(p.target_relative_path for p in plan.copies) == [
        "2019/01/a.jpg",
        "Unknown date/b.mp4",
    ]
    assert estimate_space(plan, [photo, other]) == 210


def test_plan_does_not_depend_on_input_order():
    cands = []
    for i in range(6):
        cands.append(cand(i, f"Photos from 2019/IMG_{i % 3}.jpg", title=f"IMG_{i % 3}.jpg"))
    for i in range(6, 10):
        cands.append(
            cand(i, f"Album{i}/IMG_{i % 3}.jpg", title=f"IMG_{i % 3}.jpg", album=f"Album{i}")
        )
    cands.append(cand(20, "x/a.jpg", hash="dup"))
    cands.append(cand(21, "A/b.jpg", hash="dup", album="A"))
    cands += list(pair(30, "P"))
    cands += list(pair(40, "Q", album="Q"))
    cands.append(cand(50, "x/c/metadata.json", kind="json", sidecar=False, date=None))
    cands.append(cand(51, "y/c/metadata.json", kind="json", sidecar=False, date=None))
    expected = plan_of(cands, existing=["2019/01/IMG_0.jpg"])
    rng = random.Random(1)
    for _ in range(20):
        shuffled = cands[:]
        rng.shuffle(shuffled)
        assert plan_of(shuffled, existing=["2019/01/IMG_0.jpg"]) == expected


def test_every_rename_has_a_reason_and_target_in_the_plan():
    cands = [
        cand(1, "A/disk.jpg", title="a/b.HEIC"),
        cand(2, "B/disk.jpg", title="a/b.HEIC"),
        cand(3, "C/x.jpg", title="x" * 300 + ".jpg"),
    ]
    plan = plan_of(cands)
    planned = {p.target_relative_path for p in plan.copies}
    assert {r.reason for r in plan.renames} == {
        "title_extension",
        "sanitized",
        "clash",
        "long_path",
    }
    assert all(r.target in planned for r in plan.renames)


# --- space and mtime -----------------------------------------------------------------------------


def test_estimate_space_adds_five_percent_rounded_up():
    a = cand(1, "x/a.jpg", size=1000)
    b = cand(2, "x/b.jpg", size=1)
    dup = cand(3, "y/a.jpg", hash="h1", size=1000, album="y")
    plan = plan_of([a, b, dup])
    assert estimate_space(plan, [a, b, dup]) == 1052  # 1001 * 1.05 = 1051.05
    assert estimate_space(plan_of([]), []) == 0


def test_taken_mtime_ns_with_injected_zone():
    assert taken_mtime_ns("1970-01-01T00:00:00", UTC) == 0
    assert taken_mtime_ns("2019-01-31T23:59:59", UTC) == 1548979199 * 10**9
    plus2 = timezone(timedelta(hours=2))
    assert taken_mtime_ns("2019-01-31T23:59:59", plus2) == (1548979199 - 7200) * 10**9
    assert taken_mtime_ns("2019-01-31T23:59:59.5", UTC) == 1548979199 * 10**9 + 500_000_000
    assert isinstance(taken_mtime_ns("2019-01-31T23:59:59", UTC), int)


def test_taken_mtime_ns_defaults_to_system_zone():
    local = datetime(2019, 6, 1, 12, 0, 0)
    assert taken_mtime_ns("2019-06-01T12:00:00") == int(local.astimezone().timestamp()) * 10**9
