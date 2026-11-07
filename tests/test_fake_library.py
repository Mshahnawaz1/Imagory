"""The fake library generator (PRD §10), mainly its synthetic Takeout export."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_fake_library.py"
GP = "Takeout/Google Photos"


def _load_module():
    spec = importlib.util.spec_from_file_location("make_fake_library", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_module()


def _run(out: Path, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPT), str(out), *args], check=True, capture_output=True)


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def takeout(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("takeout")
    _run(out, "--takeout", "--count", "300", "--seed", "7")
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("title", "style", "counter", "expected"),
    [
        ("IMG_1234.jpg", "plain", 0, "IMG_1234.jpg.json"),
        ("IMG_1234.jpg", "supplemental", 0, "IMG_1234.jpg.supplemental-metadata.json"),
        ("IMG_1234.jpg", "plain", 1, "IMG_1234.jpg(1).json"),
        ("IMG_1234.jpg", "supplemental", 2, "IMG_1234.jpg.supplemental-metadata(2).json"),
        ("Screenshot_20190131-235959.png", "supplemental", 0,
         "Screenshot_20190131-235959.png.supplemental-me.json"),
        ("PXL_20190131_235959123.jpg", "supplemental", 0,
         "PXL_20190131_235959123.jpg.supplemental-metada.json"),
        ("IMG_20190131_235959_123.jpg", "supplemental", 0,
         "IMG_20190131_235959_123.jpg.supplemental-metad.json"),
        ("Holiday in the mountains with the whole family 2019.jpg", "plain", 0,
         "Holiday in the mountains with the whole family.json"),
    ],
)  # fmt: skip
def test_sidecar_name(title, style, counter, expected):
    assert gen.sidecar_name(title, style, counter) == expected
    assert len(expected) - len(f"({counter})" if counter else "") <= 51


def test_layout(takeout):
    out, manifest = takeout
    gp = out / GP
    assert (out / "manifest.json").is_file()
    assert not list(gp.rglob("manifest.json"))
    assert list(gp.glob("Photos from 20??"))
    for folder in ("Trash", "Archive"):
        assert list((gp / folder).glob("*.jpg"))
    for meta in manifest["album_metadata"]:
        assert json.loads((out / meta).read_text(encoding="utf-8"))["title"] in meta
    kinds = {e["folder_kind"] for e in manifest["images"].values()}
    assert kinds == {"year", "album", "trash", "archive"}


def test_every_file_is_in_the_manifest(takeout):
    out, manifest = takeout
    images = manifest["images"]
    sidecars = {e["sidecar"] for e in images.values() if e["sidecar"]}
    known_json = sidecars | set(manifest["orphan_sidecars"]) | set(manifest["album_metadata"])
    known_json |= set(manifest["other_json"])
    files = set(_tree(out)) - {"manifest.json", "Takeout/archive_browser.html"}
    assert {f for f in files if not f.endswith(".json")} == set(images)
    assert {f for f in files if f.endswith(".json")} == known_json
    assert len({f.lower() for f in files}) == len(files)  # no clashes on Windows


def test_album_copies_are_exact(takeout):
    out, manifest = takeout
    copies = {p: e for p, e in manifest["images"].items() if e["folder_kind"] == "album"}
    assert copies
    for path, entry in copies.items():
        src = manifest["images"][entry["copy_of"]]
        assert (out / path).read_bytes() == (out / entry["copy_of"]).read_bytes()
        assert entry["expected"] == src["expected"]
        if entry["sidecar"]:
            assert (out / entry["sidecar"]).read_bytes() == (out / src["sidecar"]).read_bytes()


def test_sidecar_contents_match_manifest(takeout):
    out, manifest = takeout
    geo_zero = geo_real = 0
    for path, entry in manifest["images"].items():
        if not entry["sidecar"]:
            continue
        data = json.loads((out / entry["sidecar"]).read_text(encoding="utf-8"))
        assert data["title"] == entry["title"]
        if entry["quirk"] not in ("numbered", "edited"):
            assert data["title"] == path.rsplit("/", 1)[1]
        stamp = data["photoTakenTime"]["timestamp"]
        assert isinstance(stamp, str)
        utc = datetime(1970, 1, 1) + timedelta(seconds=int(stamp))
        assert f"{utc:%Y-%m-%dT%H:%M:%S}Z" == entry["json_time_utc"]
        assert data["photoTakenTime"]["formatted"].endswith(" UTC")
        assert int(data["creationTime"]["timestamp"]) > int(stamp)
        assert {"geoData", "geoDataExif"} <= data.keys()
        geo = data["geoData"]
        if geo["latitude"] == 0.0 and geo["longitude"] == 0.0:
            geo_zero += 1
            assert entry["expected"]["geo"] is None
        else:
            geo_real += 1
            assert entry["expected"]["geo"] == {k: geo[k] for k in ("latitude", "longitude")}
        if entry["folder_kind"] == "trash":
            assert data["trashed"] is True
    assert geo_zero > 20 and geo_real > 20


def test_naming_quirks(takeout):
    _, manifest = takeout
    images = manifest["images"]
    counts = manifest["counts"]
    for quirk in gen.QUIRK_WEIGHTS:
        assert counts[f"quirk:{quirk}"] >= 5, quirk
    assert counts["orphan_sidecars"] >= 2

    def side(entry):
        return entry["sidecar"].rsplit("/", 1)[1]

    for path, entry in images.items():
        name = path.rsplit("/", 1)[1]
        quirk = entry["quirk"]
        if quirk == "plain":
            assert side(entry) == f"{name}.json"
        elif quirk == "supplemental":
            assert side(entry) == f"{name}.supplemental-metadata.json"
        elif quirk == "supplemental_truncated":
            assert side(entry).startswith(f"{name}.s")
            assert ".supplemental-metadata" not in side(entry)
            assert len(side(entry)) == 51
        elif quirk == "long_name":
            assert len(name) > 46
            assert side(entry) == f"{name[:46]}.json"
        elif quirk == "numbered":
            stem, ext = name.rsplit(".", 1)
            counter = stem[stem.rindex("(") :]
            assert entry["title"] == f"{stem[: stem.rindex('(')]}.{ext}"
            assert side(entry).endswith(f"{counter}.json")
            assert side(entry).startswith(entry["title"])
        elif quirk == "edited":
            assert "-edited." in name
            original = entry["edited_from"]
            assert entry["sidecar"] == images[original]["sidecar"]
            assert original.rsplit("/", 1)[1] == name.replace("-edited", "")
        elif quirk == "no_sidecar":
            assert entry["sidecar"] is None


def test_orphan_sidecars_have_no_image(takeout):
    out, manifest = takeout
    names = {p.rsplit("/", 1)[1].lower() for p in manifest["images"]}
    for orphan in manifest["orphan_sidecars"]:
        title = json.loads((out / orphan).read_text(encoding="utf-8"))["title"]
        assert title.lower() not in names


def test_dates(takeout):
    out, manifest = takeout
    counts = manifest["counts"]
    for case in ("tz_offset", "over_24h", "no_exif"):
        assert counts[f"date:{case}"] >= 10, case
    sources = set()
    for path, entry in manifest["images"].items():
        exif = Image.open(out / path).getexif().get_ifd(0x8769).get(0x9003)
        expected_exif = entry["exif_time"]
        assert exif == (
            expected_exif.replace("-", ":").replace("T", " ") if expected_exif else None
        )
        expected = entry["expected"]
        sources.add(expected["date_source"])
        if expected_exif and entry["json_time_utc"]:
            diff = abs(
                datetime.fromisoformat(expected_exif)
                - datetime.fromisoformat(entry["json_time_utc"][:-1])
            )
            if entry["date_case"] == "tz_offset":
                assert timedelta(0) < diff < timedelta(hours=24)
                assert expected["taken_at_local"] == expected_exif
            elif entry["date_case"] == "over_24h":
                assert diff > timedelta(hours=24)
                assert expected["taken_at_local"] is None
        mtime = datetime(1970, 1, 1) + timedelta(seconds=(out / path).stat().st_mtime_ns // 10**9)
        assert f"{mtime:%Y-%m-%dT%H:%M:%S}Z" == entry["mtime_utc"]
    assert sources == {"takeout_json", "exif", "filename", "mtime"}


def test_filename_patterns(takeout):
    _, manifest = takeout
    patterns = {
        "IMG_YYYYMMDD_HHMMSS": r"IMG_\d{8}_\d{6}(_\d{3})?\.jpg",
        "PXL": r"PXL_\d{8}_\d{9}\.jpg",
        "WhatsApp": r"IMG-\d{8}-WA\d{4}\.jpg",
        "Screenshot": r"Screenshot_\d{8}-\d{6}\.png",
        "dotted": r"\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2}\.jpg",
    }
    names = {p.rsplit("/", 1)[1]: e for p, e in manifest["images"].items()}
    for label, pattern in patterns.items():
        matches = [e for n, e in names.items() if re.fullmatch(pattern, n)]
        assert len(matches) >= 3, label
        for entry in matches:
            assert entry["filename_time"]
            digits = re.sub(r"\D", "", entry["filename_time"])
            name = next(n for n, e in names.items() if e is entry)
            assert digits in re.sub(r"\D", "", name)


def test_deterministic_by_seed(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _run(a, "--takeout", "--count", "60", "--seed", "3")
    _run(b, "--takeout", "--count", "60", "--seed", "3")
    _run(c, "--takeout", "--count", "60", "--seed", "4")
    assert _tree(a) == _tree(b)
    assert _tree(a) != _tree(c)


def test_plain_mode_unchanged(tmp_path):
    out = tmp_path / "plain"
    _run(out, "--count", "40")
    files = _tree(out)
    assert not (out / "manifest.json").exists()
    assert not (out / "Takeout").exists()
    assert len([f for f in files if f.startswith("Copies/")]) == 2
    assert len([f for f in files if f.startswith("Broken/")]) == 10
    assert len([f for f in files if f.startswith("Videos/")]) == 20
    photos = [f for f in files if f[:2] in ("19", "20")]
    assert len(photos) == 38
    assert all(f.split("/")[0].isdigit() and f.split("/")[1].isdigit() for f in photos)


# --- Takeout cleanup fixtures (--cleanup, --second-account) ------------------------------------

YEAR04 = f"{GP}/Photos from 2004"
ALBUM = f"{GP}/Cleanup album"


@pytest.fixture(scope="module")
def cleanup(tmp_path_factory) -> tuple[Path, dict, dict]:
    out = tmp_path_factory.mktemp("cleanup")
    _run(out, "--takeout", "--cleanup", "--count", "300", "--seed", "7")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return out, manifest, manifest["cleanup"]


@pytest.fixture(scope="module")
def second(cleanup, tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("second")
    _run(out, "--second-account", str(cleanup[0]))
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _jtitle(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["title"]


def _all_files(out: Path) -> set[str]:
    return {p.relative_to(out).as_posix() for p in (out / "Takeout").rglob("*") if p.is_file()}


def test_cleanup_is_opt_in_and_changes_nothing_else(cleanup, tmp_path):
    out, manifest, _ = cleanup
    plain = tmp_path / "plain"
    _run(plain, "--takeout", "--count", "300", "--seed", "7")
    plain_manifest = json.loads((plain / "manifest.json").read_text(encoding="utf-8"))
    assert "cleanup" not in plain_manifest
    assert {k: v for k, v in manifest.items() if k != "cleanup"} == plain_manifest
    with_cleanup = _tree(out)
    for rel, digest in _tree(plain).items():
        if rel != "manifest.json":
            assert with_cleanup[rel] == digest


def test_cleanup_cases_exist(cleanup):
    out, _, block = cleanup
    for rel in (
        f"{YEAR04}/VID_20040401_120000.mp4",
        f"{YEAR04}/VID_20040401_120000.mp4.json",
        f"{YEAR04}/MOV_0007.mov",
        f"{YEAR04}/MOV_0007.mov.supplemental-metadata.json",
        f"{YEAR04}/MOTION_0001.HEIC",
        f"{YEAR04}/MOTION_0001.MOV",
        f"{YEAR04}/corrupt_cleanup.jpg",
        f"{YEAR04}/UNUSED_cleanup.jpg.json",
        f"{YEAR04}/Edited_Test-edited.jpg",
        f"{YEAR04}/a_b_c.jpg",
        f"{YEAR04}/Case_Pair.jpg",
        f"{ALBUM}/case_pair.JPG",
        f"{ALBUM}/metadata.json",
        f"{GP}/Archive/CLN_archived.jpg",
        f"{GP}/Trash/CLN_trashed.jpg",
    ):
        assert (out / rel).is_file(), rel
    assert block["albums"]["Cleanup album"] == "Lisbon trip 2004"
    assert _jtitle(out / ALBUM / "metadata.json") == "Lisbon trip 2004"
    files = block["files"]
    assert files[f"{YEAR04}/a_b_c.jpg"]["title"] == "a/b:c.jpg"
    assert files[f"{YEAR04}/Trip_ day 1.jpg"]["title"] == "Trip: day 1.jpg"
    long_name = f"{YEAR04}/Scanned slides from the old shoebox in.jpg"
    long_title = files[long_name]["title"]
    assert len(long_title) > 60 and long_title.startswith(long_name.rsplit("/", 1)[1][:-4])
    upper, lower = files[f"{YEAR04}/Case_Pair.jpg"], files[f"{ALBUM}/case_pair.JPG"]
    assert upper["title"] != lower["title"]
    assert upper["title"].lower() == lower["title"].lower()


def test_cleanup_block_lists_every_file_with_true_hashes(cleanup):
    from imagory.thumbs import hash_bytes

    out, _, block = cleanup
    files = block["files"]
    assert set(files) == _all_files(out)
    for rel, entry in files.items():
        data = (out / rel).read_bytes()
        assert entry["hash"] == hash_bytes(data)
        assert entry["size"] == len(data)
        assert entry["kind"] in ("photo", "other", "json", "sidecar", "trash")
    groups = block["groups"]
    assert {e["hash_group"] for e in files.values()} == set(groups)
    assert len({g["hash"] for g in groups.values()}) == len(groups)  # one group per content
    for gid, group in groups.items():
        first = (out / group["members"][0]).read_bytes()
        assert all((out / m).read_bytes() == first for m in group["members"])
        assert all(files[m]["hash_group"] == gid for m in group["members"])
    # P1 has four members: itself, a renamed copy, an album copy and a Trash copy.
    p1 = files[f"{YEAR04}/IMG_20040315_101010.jpg"]
    group = groups[p1["hash_group"]]
    assert len(group["members"]) == 4
    assert {files[m]["kind"] for m in group["members"]} == {"photo", "trash"}
    assert group["albums"] == ["Lisbon trip 2004"]
    video = files[f"{YEAR04}/VID_20040401_120000.mp4"]  # its album copy is exact too
    assert len(groups[video["hash_group"]]["members"]) == 2


def test_cleanup_kinds_and_sidecars(cleanup):
    out, _, block = cleanup
    files = block["files"]
    for rel, entry in files.items():
        assert (entry["kind"] == "trash") == rel.startswith(f"{GP}/Trash/")
        if entry["sidecar"]:
            assert (out / entry["sidecar"]).is_file()
            assert entry["title"] == _jtitle(out / entry["sidecar"])
            if not entry["edited_of"]:
                assert files[entry["sidecar"]]["sidecar_of"] == rel
        is_sidecar_json = rel.endswith(".json") and not rel.endswith(
            ("metadata.json", "print-subscriptions.json", "user-generated-memory-titles.json")
        )
        if is_sidecar_json:
            assert entry["kind"] in ("sidecar", "trash")
            assert entry["title"] == _jtitle(out / rel)
    assert {r.rsplit("/", 1)[1] for r, e in files.items() if e["kind"] == "json"} == {
        "metadata.json",
        "print-subscriptions.json",
        "user-generated-memory-titles.json",
    }
    unused = files[f"{YEAR04}/UNUSED_cleanup.jpg.json"]
    assert unused["kind"] == "sidecar" and unused["sidecar_of"] is None
    used = {e["sidecar"] for e in files.values() if e["sidecar"]}
    assert f"{YEAR04}/UNUSED_cleanup.jpg.json" not in used
    mov = files[f"{YEAR04}/MOTION_0001.MOV"]
    assert mov["companion_of"] == f"{YEAR04}/MOTION_0001.HEIC" and mov["sidecar"] is None
    edited = files[f"{YEAR04}/Edited_Test-edited.jpg"]
    assert edited["edited_of"] == f"{YEAR04}/Edited_Test.jpg"
    assert edited["sidecar"] == files[edited["edited_of"]]["sidecar"]
    mp4 = files[f"{YEAR04}/VID_20040401_120000.mp4"]
    assert mp4["kind"] == "other" and mp4["title"] == "VID_20040401_120000.mp4"


def test_cleanup_readable_flags_and_dates(cleanup):
    import pillow_heif

    pillow_heif.register_heif_opener()
    out, _, block = cleanup
    files = block["files"]
    bad = f"{YEAR04}/corrupt_cleanup.jpg"
    assert files[bad]["readable"] is False and files[bad]["kind"] == "photo"
    with pytest.raises(OSError), Image.open(out / bad) as img:
        img.load()
    for rel, entry in files.items():
        if rel != bad and entry["readable"] is not None:
            assert entry["readable"] is True
            with Image.open(out / rel) as img:
                img.load()
    # Dates come from the sidecar, EXIF or the filename, never from the file time.
    for rel in (f"{YEAR04}/clip_nodate.mp4", f"{YEAR04}/MOTION_0001.MOV"):
        assert files[rel]["expected_date"] is None
    assert files[f"{YEAR04}/VID_20040402_080000.mp4"]["expected_date"]["source"] == "filename"
    p1 = files[f"{YEAR04}/IMG_20040315_101010.jpg"]["expected_date"]
    assert p1["taken_at_local"] == "2004-03-15T11:10:10"
    assert p1["taken_at_utc"] == "2004-03-15T10:10:10Z"
    bare = files[f"{YEAR04}/IMG_20040315_101010 copy.jpg"]
    assert bare["sidecar"] is None and bare["expected_date"]["source"] == "exif"
    for entry in files.values():
        date = entry["expected_date"]
        assert date is None or date["source"] != "mtime"
        if entry["kind"] in ("json", "sidecar"):
            assert date is None


def test_cleanup_album_memberships(cleanup):
    _, _, block = cleanup
    for rel, entry in block["files"].items():
        if entry["folder_kind"] == "album" and not rel.endswith(".json"):
            assert entry["albums"] == [block["albums"][rel.split("/")[2]]]
        else:
            assert entry["albums"] == []


def test_cleanup_sidecars_match_the_real_matcher(cleanup):
    """The ground truth agrees with imagory's own sidecar matcher for every image."""
    from imagory import takeout

    out, _, block = cleanup
    by_folder: dict[str, list[str]] = {}
    for rel, entry in block["files"].items():
        if entry["folder_kind"] != "root":
            by_folder.setdefault(rel.rsplit("/", 1)[0], []).append(rel)
    for folder, rels in by_folder.items():
        names = [r.rsplit("/", 1)[1] for r in rels]
        matches = takeout.match_directory(names, lambda n, f=folder: _jtitle(out / f / n)).matches
        for rel in rels:
            name = rel.rsplit("/", 1)[1]
            side = block["files"][rel]["sidecar"]
            if name in matches:
                assert matches[name] == (side.rsplit("/", 1)[1] if side else None), rel


def test_cleanup_is_deterministic(cleanup, tmp_path):
    out, _, _ = cleanup
    again = tmp_path / "again"
    _run(again, "--takeout", "--cleanup", "--count", "300", "--seed", "7")
    assert _tree(again) == _tree(out)


def test_second_account_overlaps_and_clashes(cleanup, second):
    first_out, _, first = cleanup
    out, manifest = second
    info = manifest["second_account"]
    block = manifest["cleanup"]
    files = block["files"]
    assert set(files) == _all_files(out) and len(files) < len(first["files"]) // 10
    for rel in info["shared"]:
        assert (out / rel).read_bytes() == (first_out / rel).read_bytes()
        assert files[rel]["hash"] == first["files"][rel]["hash"]
    for rel in info["clashing"]:  # same path, same sidecar name, different content
        assert (out / rel).read_bytes() != (first_out / rel).read_bytes()
        assert files[rel]["sidecar"] == first["files"][rel]["sidecar"]
        assert files[rel]["title"] == first["files"][rel]["title"]
    assert block["albums"][info["album_clash"]] == first["albums"][info["album_clash"]]
    common = {e["hash"] for e in files.values()} & {e["hash"] for e in first["files"].values()}
    assert set(info["shared_hashes"]) == common
    p1 = f"{YEAR04}/IMG_20040315_101010.jpg"
    assert files[p1]["expected_date"] == first["files"][p1]["expected_date"]
    for entry in files.values():
        if entry["sidecar"]:
            assert entry["title"] == _jtitle(out / entry["sidecar"])
