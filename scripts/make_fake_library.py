"""Generate a fake photo library for performance and import testing (PRD §10).

Plain mode (the default) writes small JPEGs with varied EXIF dates into year/month folders,
a share of exact duplicate copies, some PNGs without EXIF, a few corrupt files, and
non-image files.

Takeout mode (``--takeout``) writes a synthetic Google Takeout export instead:

    OUT/manifest.json                    ground truth, outside the photo tree
    OUT/Takeout/archive_browser.html
    OUT/Takeout/Google Photos/
        Photos from YYYY/                photos with JSON sidecars, plus orphan sidecars
        <album>/                         exact copies of year-folder photos, metadata.json
        Archive/  Trash/                 their own photos with sidecars
        print-subscriptions.json, user-generated-memory-titles.json

Sidecars carry ``title``, ``photoTakenTime``, ``creationTime``, ``geoData`` and
``geoDataExif`` (often 0.0/0.0), and use every naming quirk of PRD §8.3: the plain
``IMG_1234.jpg.json``, ``.supplemental-metadata.json``, truncated forms such as
``.supplemental-metad.json``, long names cut to 46 characters before ``.json``, numbered
duplicates (``IMG_1234(1).jpg`` with ``IMG_1234.jpg(1).json``), ``-edited`` copies without
a sidecar, images without any sidecar, and orphan sidecars. EXIF DateTimeOriginal is
usually the JSON time shifted by a time zone offset (under 24 hours), sometimes more than
24 hours off, sometimes missing; many names follow the PRD §8.4 filename date patterns.
``--duplicates`` is the share of album copies here.

``manifest.json`` maps every image path (relative to OUT, ``/`` separators) to its
expected sidecar (or null), the raw EXIF/JSON/filename times, and the expected
``date_source``, ``taken_at_local``, ``taken_at_utc`` and ``geo`` per PRD §8.4. An
expected ``taken_at_local`` of null means "``taken_at_utc`` converted with the computer's
time zone", which the generator cannot know. Output is deterministic for a given seed.

Usage: uv run python scripts/make_fake_library.py OUT_DIR [--count 100000] [--takeout]
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

START = datetime(2005, 1, 1)
SPAN_SECONDS = int((datetime(2026, 1, 1) - START).total_seconds())
EPOCH = datetime(1970, 1, 1)
SIZES = [(640, 480), (480, 640), (800, 600), (600, 400)]


def _canvas(rng: random.Random, w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        box = (x0, y0, x0 + rng.randrange(20, 200), y0 + rng.randrange(20, 200))
        draw.ellipse(box, fill=tuple(rng.randrange(256) for _ in range(3)))
    return img


def _make_one(args: tuple[str, int, int]) -> str:
    out, index, seed = args
    rng = random.Random(seed)
    taken = START + timedelta(seconds=rng.randrange(SPAN_SECONDS))
    folder = Path(out) / f"{taken:%Y}" / f"{taken:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    w, h = rng.choice(SIZES)
    img = _canvas(rng, w, h)
    ImageDraw.Draw(img).text((10, 10), f"#{index} {taken:%Y-%m-%d %H:%M}", fill=(255, 255, 255))
    if rng.random() < 0.05:
        path = folder / f"Screenshot_{taken:%Y%m%d-%H%M%S}_{index}.png"
        img.save(path, "PNG")
    else:
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = f"{taken:%Y:%m:%d %H:%M:%S}"
        if rng.random() < 0.1:
            exif[0x0112] = rng.choice([3, 6, 8])
        path = folder / f"IMG_{taken:%Y%m%d_%H%M%S}_{index}.jpg"
        img.save(path, "JPEG", quality=80, exif=exif.tobytes())
    stamp = taken.timestamp()
    os.utime(path, (stamp, stamp))
    return str(path)


def _run_pool(fn: Callable, jobs: list, chunksize: int) -> list:
    workers = max(1, (os.cpu_count() or 2) - 1)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
        return list(pool.map(fn, jobs, chunksize=chunksize))


def make_plain(out: Path, count: int, duplicates: float, seed: int) -> None:
    """Write the plain library: year/month folders, copies, corrupt files and videos."""
    unique = int(count * (1 - duplicates))
    jobs = [(str(out), i, seed * 1_000_003 + i) for i in range(unique)]
    made = _run_pool(_make_one, jobs, chunksize=256)

    rng = random.Random(seed)
    copies = out / "Copies"
    copies.mkdir(exist_ok=True)
    for n in range(count - unique):
        src = Path(rng.choice(made))
        shutil.copy2(src, copies / f"copy{n}_{src.name}")

    broken = out / "Broken"
    broken.mkdir(exist_ok=True)
    for n in range(10):
        (broken / f"corrupt_{n}.jpg").write_bytes(os.urandom(2048))
    for n in range(20):
        (out / "Videos").mkdir(exist_ok=True)
        (out / "Videos" / f"VID_{n}.mp4").write_bytes(b"\x00" * 1024)


# --- Takeout mode -------------------------------------------------------------------------

SIDECAR_STEM_LIMIT = 46  # Takeout cuts the name before ".json" to 46 chars (51 in total)
SUPPLEMENTAL = ".supplemental-metadata"
GP = "Takeout/Google Photos"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ALBUMS = ["Summer holiday 2019", "Family", "Best of", "Wedding - Anna & Tom"]
TZ_OFFSETS_MIN = [-480, -420, -300, -240, -180, 60, 120, 180, 330, 480, 540, 570, 600, 780]
LONG_APPS = ["Google Play Services", "Samsung Internet Browser", "WhatsApp Messenger Beta"]
LONG_PHRASES = [
    "Holiday in the mountains with the whole family",
    "Grandmas eightieth birthday party at the lake house",
    "Scanned slides from the old shoebox in the attic",
]
# Kinds of year-folder photo groups and their share of groups.
QUIRK_WEIGHTS = {
    "plain": 0.28,
    "supplemental": 0.18,
    "supplemental_truncated": 0.14,
    "long_name": 0.10,
    "numbered": 0.12,
    "edited": 0.10,
    "no_sidecar": 0.08,
}
DATE_CASE_WEIGHTS = {"tz_offset": 0.55, "same": 0.10, "over_24h": 0.12, "no_exif": 0.23}
SHORT_NAMES = ["img_date", "dated", "whatsapp", "img_num", "dsc"]  # at most 24 chars
NAME_POOLS = {
    "plain": [*SHORT_NAMES, "pxl", "img_burst", "screenshot"],
    "supplemental": SHORT_NAMES,
    # 26, 27 and 30 chars: the suffix is cut, e.g. to ".supplemental-metad.json"
    "supplemental_truncated": ["pxl", "img_burst", "screenshot"],
    "long_name": ["long"],
    "numbered": ["img_num", "dsc"],
    "edited": [*SHORT_NAMES, "pxl", "img_burst", "screenshot"],
    "no_sidecar": ["img_num", "screenshot", "dated", "dsc"],
}


def sidecar_name(title: str, style: str, counter: int = 0) -> str:
    """Sidecar file name Takeout writes for a photo titled ``title`` (PRD §8.3).

    ``style`` is ``"plain"`` (``IMG_1.jpg.json``) or ``"supplemental"``
    (``IMG_1.jpg.supplemental-metadata.json``). The stem is cut to 46 characters, then a
    numbered duplicate's counter is appended: ``IMG_1.jpg(1).json``.
    """
    stem = title + (SUPPLEMENTAL if style == "supplemental" else "")
    stem = stem[:SIDECAR_STEM_LIMIT]
    return f"{stem}({counter}).json" if counter else f"{stem}.json"


def _epoch(dt: datetime) -> int:
    return int((dt - EPOCH).total_seconds())


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _timestamp(dt: datetime) -> dict:
    """A Takeout ``{"timestamp", "formatted"}`` pair for a naive UTC datetime."""
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    formatted = (
        f"{MONTHS[dt.month - 1]} {dt.day}, {dt.year}, "
        f"{hour}:{dt.minute:02d}:{dt.second:02d} {ampm} UTC"
    )
    return {"timestamp": str(_epoch(dt)), "formatted": formatted}


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _geo(lat: float, lon: float, alt: float) -> dict:
    return {
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    }


PEOPLE_POOL = ["Anna", "Ben", "Chloé", "Dev", "Emma", "Farid", "Grace", "Hiro", "Ines", "Jo"]
CAPTIONS = ["Birthday party", "Beach day", "Road trip", "Family dinner", "Hiking", "Graduation"]


def _make_meta(seed: int, rel: str) -> dict:
    """People, caption and favourite flag for one photo. Uses its own random stream, so the
    rest of the generated library does not change when this is tuned."""
    rng = random.Random(f"{seed}:{rel}")
    return {
        "people": [{"name": n} for n in rng.sample(PEOPLE_POOL, rng.choice([0, 0, 1, 1, 2, 3]))],
        "description": rng.choice(["", "", *CAPTIONS]),
        "favorited": rng.random() < 0.1,
    }


def _make_geo(rng: random.Random) -> tuple[dict, dict]:
    """(geoData, geoDataExif): both real, both 0/0, or only one of them real."""
    real = _geo(
        round(rng.uniform(-60, 70), 7), round(rng.uniform(-180, 180), 7), round(rng.random(), 2)
    )
    zero = _geo(0.0, 0.0, 0.0)
    r = rng.random()
    if r < 0.45:
        return real, dict(real)
    if r < 0.85:
        return zero, dict(zero)
    if r < 0.95:
        return zero, real
    return real, zero


def _media_name(kind: str, local: datetime, index: int, rng: random.Random) -> tuple[str, str]:
    """Return (file name, date the name encodes or "") for a name kind."""
    if kind == "img_date":
        return f"IMG_{local:%Y%m%d_%H%M%S}.jpg", _iso(local)
    if kind == "img_burst":
        return f"IMG_{local:%Y%m%d_%H%M%S}_{index % 1000:03d}.jpg", _iso(local)
    if kind == "pxl":
        return f"PXL_{local:%Y%m%d_%H%M%S}{index % 1000:03d}.jpg", _iso(local)
    if kind == "whatsapp":
        return f"IMG-{local:%Y%m%d}-WA{index % 10000:04d}.jpg", f"{local:%Y-%m-%d}"
    if kind == "screenshot":
        return f"Screenshot_{local:%Y%m%d-%H%M%S}.png", _iso(local)
    if kind == "dated":
        return f"{local:%Y-%m-%d %H.%M.%S}.jpg", _iso(local)
    if kind == "img_num":
        return f"IMG_{index:04d}.jpg", ""
    if kind == "dsc":
        return f"DSC{index:05d}.JPG", ""
    if kind == "long":
        if rng.random() < 0.5:
            app = rng.choice(LONG_APPS)
            return f"Screenshot_{local:%Y%m%d-%H%M%S}_{app}.png", _iso(local)
        return f"{index:06d} {rng.choice(LONG_PHRASES)}.jpg", ""
    raise ValueError(kind)


def _expected(json_utc: str | None, exif: datetime | None, utc: datetime, ftime: str) -> dict:
    """Expected best date per PRD §8.4 (Takeout order: JSON, EXIF, filename, mtime)."""
    if json_utc:
        local = _iso(exif) if exif and abs(exif - utc) < timedelta(hours=24) else None
        return {"date_source": "takeout_json", "taken_at_local": local, "taken_at_utc": json_utc}
    if exif:
        return {"date_source": "exif", "taken_at_local": _iso(exif), "taken_at_utc": None}
    if len(ftime) == 19:
        return {"date_source": "filename", "taken_at_local": ftime, "taken_at_utc": None}
    return {"date_source": "mtime", "taken_at_local": None, "taken_at_utc": _iso(utc) + "Z"}


def _make_takeout_one(spec: dict) -> None:
    """Worker: render one image and write its sidecar, exactly as planned."""
    rng = random.Random(spec["seed"])
    w, h = rng.choice(SIZES)
    img = _canvas(rng, w, h)
    if spec["edited"]:
        img = ImageOps.autocontrast(ImageOps.mirror(img), cutoff=2)
    ImageDraw.Draw(img).text((10, 10), spec["label"], fill=(255, 255, 255))
    path = Path(spec["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec["fmt"] == "PNG":
        img.save(path, "PNG")
    else:
        kwargs = {}
        if spec["exif"]:
            exif = Image.Exif()
            exif.get_ifd(0x8769)[0x9003] = spec["exif"]
            if spec["orientation"]:
                exif[0x0112] = spec["orientation"]
            kwargs["exif"] = exif.tobytes()
        img.save(path, "JPEG", quality=80, **kwargs)
    stamp = spec["mtime"]
    os.utime(path, (stamp, stamp))
    if spec["sidecar_path"]:
        side = Path(spec["sidecar_path"])
        side.write_text(json.dumps(spec["sidecar"], indent=2), encoding="utf-8")
        os.utime(side, (stamp, stamp))


class _TakeoutPlan:
    """Decides every file of the export in the main process; workers only render."""

    def __init__(self, out: Path, seed: int) -> None:
        self.out = out
        self.rng = random.Random(seed)
        self.seed = seed
        self.index = 0
        self.specs: list[dict] = []
        self.images: dict[str, dict] = {}
        self.taken: set[str] = set()  # lower-case relative paths, as Windows compares them

    @staticmethod
    def rel(folder: str, name: str) -> str:
        return f"{GP}/{folder}/{name}"

    def free(self, folder: str, *names: str) -> bool:
        return all(self.rel(folder, n).lower() not in self.taken for n in names)

    def image_seed(self, salt: int = 0) -> int:
        return self.seed * 1_000_003 + self.index + 500_000_000 * salt

    def times(self, kind: str, utc: datetime | None = None) -> tuple[datetime, datetime, str]:
        """Pick a UTC moment, the camera's wall clock and the date case for one photo."""
        rng = self.rng
        if utc is None:
            utc = START + timedelta(seconds=rng.randrange(SPAN_SECONDS))
        case = _pick(rng, DATE_CASE_WEIGHTS)
        if kind in ("whatsapp", "screenshot"):
            case = "no_exif"  # WhatsApp strips EXIF; screenshots are PNGs
        offset = timedelta(minutes=rng.choice(TZ_OFFSETS_MIN))
        if case == "same":
            offset = timedelta(0)
        elif case == "over_24h":  # camera clock wrong by days, or JSON is the upload time
            offset = -timedelta(days=rng.randrange(2, 400), minutes=rng.choice(TZ_OFFSETS_MIN))
        return utc, utc + offset, case

    def add(
        self,
        folder: str,
        name: str,
        quirk: str,
        utc: datetime,
        exif: datetime | None,
        ftime: str,
        date_case: str,
        style: str | None = None,
        sidecar: str | None = None,
        title: str | None = None,
        edited_from: str | None = None,
        extra: dict | None = None,
        seed: int | None = None,
    ) -> str:
        """Plan one image (and its own sidecar unless it is an edited copy)."""
        rng = self.rng
        rel = self.rel(folder, name)
        side_rel = self.rel(folder, sidecar) if sidecar else None
        self.taken.add(rel.lower())
        json_utc = _iso(utc) + "Z" if sidecar else None
        geo, geo_exif = _make_geo(rng)
        meta = _make_meta(self.seed, rel)
        uploaded = None
        payload = None
        if edited_from:
            geo = self.images[edited_from]["_geo"]
            meta, uploaded = self.images[edited_from]["_meta"]
        elif sidecar:
            self.taken.add(side_rel.lower())
            created = utc + timedelta(seconds=rng.randrange(60, 90 * 86400))
            payload = {
                "title": title,
                "description": meta["description"],
                "imageViews": str(rng.randrange(40)),
                "creationTime": _timestamp(created),
                "photoTakenTime": _timestamp(utc),
                "geoData": geo,
                "geoDataExif": geo_exif,
                **({"people": meta["people"]} if meta["people"] else {}),
                **({"favorited": True} if meta["favorited"] else {}),
                "url": f"https://photos.google.com/photo/AF1Qip{rng.getrandbits(64):016x}",
                "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}},
                **(extra or {}),
            }
        expected = _expected(json_utc, exif, utc, ftime)
        has_geo = sidecar and not (geo["latitude"] == 0.0 and geo["longitude"] == 0.0)
        expected["geo"] = (
            {"latitude": geo["latitude"], "longitude": geo["longitude"]} if has_geo else None
        )
        if payload:
            uploaded = _iso(created)
        has_meta = bool(sidecar or edited_from)
        expected["people"] = sorted((p["name"] for p in meta["people"]), key=str.casefold)
        expected["description"] = meta["description"] or None
        expected["favorite"] = meta["favorited"]
        expected["uploaded_utc"] = uploaded + "Z" if has_meta and uploaded else None
        if not has_meta:
            expected["people"], expected["description"], expected["favorite"] = [], None, False
        self.specs.append(
            {
                "path": str(self.out / rel),
                "seed": self.image_seed() if seed is None else seed,
                "label": f"#{self.index} {utc:%Y-%m-%d %H:%M}Z",
                "edited": bool(edited_from),
                "fmt": "PNG" if name.lower().endswith(".png") else "JPEG",
                "exif": f"{exif:%Y:%m:%d %H:%M:%S}" if exif else None,
                "orientation": rng.choice([3, 6, 8]) if exif and rng.random() < 0.1 else None,
                "mtime": _epoch(utc),
                "sidecar_path": str(self.out / side_rel) if payload else None,
                "sidecar": payload,
            }
        )
        self.images[rel] = {
            "folder_kind": {"Trash": "trash", "Archive": "archive"}.get(folder, "year"),
            "quirk": quirk,
            "sidecar": side_rel,
            "sidecar_style": style,
            "title": title,
            "copy_of": None,
            "edited_from": edited_from,
            "date_case": date_case,
            "exif_time": _iso(exif) if exif else None,
            "json_time_utc": json_utc,
            "filename_time": ftime or None,
            "mtime_utc": _iso(utc) + "Z",
            "expected": expected,
            "_geo": geo,
            "_meta": (meta, uploaded),
        }
        return rel

    def group(self, quirk: str) -> int:
        """Plan one group of year-folder photos; returns how many images it added."""
        rng = self.rng
        style = "supplemental" if rng.random() < 0.45 else "plain"
        if quirk in ("plain", "edited"):
            style = "plain"
        elif quirk in ("supplemental", "supplemental_truncated"):
            style = "supplemental"
        for _attempt in range(20):
            self.index += 1
            kind = rng.choice(NAME_POOLS[quirk])
            utc, local, case = self.times(kind)
            if quirk == "no_sidecar":  # one photo each for EXIF, filename and mtime dates
                case = "exif_only" if kind == "img_num" else "no_exif"
            name, ftime = _media_name(kind, local, self.index, rng)
            exif = None if case == "no_exif" or name.endswith(".png") else local
            folder = f"Photos from {utc:%Y}"
            if quirk == "no_sidecar":
                if not self.free(folder, name):
                    continue
                self.add(folder, name, quirk, utc, exif, ftime, case)
                return 1
            side = sidecar_name(name, style)
            if quirk == "numbered":
                size = 2 if rng.random() < 0.75 else 3
                stem, ext = os.path.splitext(name)
                names = [name] + [f"{stem}({n}){ext}" for n in range(1, size)]
                sides = [sidecar_name(name, style, n) for n in range(size)]
                if not self.free(folder, *names, *sides):
                    continue
                year_start = datetime(utc.year, 1, 1)
                year_span = int((datetime(utc.year + 1, 1, 1) - year_start).total_seconds())
                for n in range(size):
                    if n:  # a different photo with the same title, from the same year
                        moment = year_start + timedelta(seconds=rng.randrange(year_span))
                        utc, local, case = self.times(kind, moment)
                        exif = None if case == "no_exif" else local
                    label = "numbered" if n else style
                    self.add(
                        folder,
                        names[n],
                        label,
                        utc,
                        exif,
                        "",
                        case,
                        style,
                        sides[n],
                        name,
                        seed=self.image_seed(n),
                    )
                return size
            stem, ext = os.path.splitext(name)
            edited = f"{stem}-edited{ext}"
            if not self.free(folder, name, side, edited):
                continue
            label = quirk if quirk in ("supplemental_truncated", "long_name") else style
            original = self.add(folder, name, label, utc, exif, ftime, case, style, side, name)
            if quirk != "edited":
                return 1
            self.add(
                folder,
                edited,
                "edited",
                utc,
                exif,
                ftime,
                case,
                style,
                side,
                name,
                edited_from=original,
                seed=self.image_seed(),
            )
            return 2
        raise RuntimeError("could not find a free file name")

    def special(self, folder: str, flag: str) -> None:
        """Plan one photo with a sidecar in the Archive or Trash folder."""
        self.index += 1
        utc, local, case = self.times("img_num")
        name, _ = _media_name("img_num", local, self.index, self.rng)
        style = "supplemental" if self.rng.random() < 0.5 else "plain"
        exif = None if case == "no_exif" else local
        side = sidecar_name(name, style)
        self.add(folder, name, style, utc, exif, "", case, style, side, name, extra={flag: True})


def make_takeout(out: Path, count: int, duplicates: float, seed: int) -> dict:
    """Write a synthetic Takeout export under ``out/Takeout`` and return the manifest."""
    plan = _TakeoutPlan(out, seed)
    rng = plan.rng
    unique = max(1, int(count * (1 - duplicates)))
    n_special = max(2, unique // 100) if unique >= 40 else 1
    year_target = max(1, unique - 2 * n_special)
    quirks = list(QUIRK_WEIGHTS)
    made = groups = 0
    while made < year_target:  # the first groups cover every quirk, then by weight
        quirk = quirks[groups] if groups < len(quirks) else _pick(rng, QUIRK_WEIGHTS)
        if quirk in ("numbered", "edited") and year_target - made < 3:
            quirk = "plain"
        made += plan.group(quirk)
        groups += 1
    for _ in range(n_special):
        plan.special("Archive", "archived")
        plan.special("Trash", "trashed")

    _run_pool(_make_takeout_one, plan.specs, chunksize=64)

    gp = out / GP
    sources = [
        r for r, e in plan.images.items() if e["folder_kind"] == "year" and not e["edited_from"]
    ]
    albums = ALBUMS[: min(len(ALBUMS), max(1, (count - unique) // 3 + 1))]
    for album in albums:
        (gp / album).mkdir(parents=True, exist_ok=True)
        meta = {
            "title": album,
            "description": "",
            "access": "protected",
            "date": _timestamp(START + timedelta(seconds=rng.randrange(SPAN_SECONDS))),
        }
        (gp / album / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for _ in range(count - unique):
        src = rng.choice(sources)
        album = rng.choice(albums)
        entry = plan.images[src]
        name = src.rsplit("/", 1)[1]
        if not plan.free(album, name):
            continue
        dst = plan.rel(album, name)
        plan.taken.add(dst.lower())
        shutil.copy2(out / src, out / dst)
        side = None
        if entry["sidecar"]:
            side = plan.rel(album, entry["sidecar"].rsplit("/", 1)[1])
            plan.taken.add(side.lower())
            shutil.copy2(out / entry["sidecar"], out / side)
        plan.images[dst] = {**entry, "folder_kind": "album", "sidecar": side, "copy_of": src}

    orphans = []
    for _ in range(max(2, unique // 200)):
        plan.index += 1
        utc = START + timedelta(seconds=rng.randrange(SPAN_SECONDS))
        title = f"IMG_{plan.index:04d}.jpg"  # no image was ever planned under this index
        rel = plan.rel(f"Photos from {utc:%Y}", sidecar_name(title, "plain"))
        payload = {"title": title, "photoTakenTime": _timestamp(utc), "geoData": _geo(0, 0, 0)}
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        orphans.append(rel)

    other = [f"{GP}/print-subscriptions.json", f"{GP}/user-generated-memory-titles.json"]
    for rel in other:
        (out / rel).write_text("{}\n", encoding="utf-8")
    (out / "Takeout" / "archive_browser.html").write_text(
        "<!doctype html><title>Takeout</title>\n", encoding="utf-8"
    )

    images = {
        r: {k: v for k, v in e.items() if k not in ("_geo", "_meta")}
        for r, e in plan.images.items()
    }
    counts: dict[str, int] = {"orphan_sidecars": len(orphans)}
    for e in images.values():
        for key in (f"quirk:{e['quirk']}", f"folder:{e['folder_kind']}", f"date:{e['date_case']}"):
            counts[key] = counts.get(key, 0) + 1
    manifest = {
        "version": 1,
        "seed": seed,
        "count": count,
        "source_root": "Takeout",
        "images": dict(sorted(images.items())),
        "orphan_sidecars": sorted(orphans),
        "album_metadata": [plan.rel(a, "metadata.json") for a in albums],
        "other_json": other,
        "counts": dict(sorted(counts.items())),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--duplicates", type=float, default=0.05, help="share of exact copies")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--takeout", action="store_true", help="write a Google Takeout export")
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.takeout:
        manifest = make_takeout(out, args.count, args.duplicates, args.seed)
        elapsed = time.perf_counter() - started
        print(
            f"Wrote a Takeout export with {len(manifest['images']):,} images and "
            f"{len(manifest['orphan_sidecars'])} orphan sidecars to {out} in {elapsed:.0f}s"
        )
        return
    make_plain(out, args.count, args.duplicates, args.seed)
    elapsed = time.perf_counter() - started
    print(f"Wrote {args.count:,} images (+10 corrupt, 20 videos) to {out} in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
